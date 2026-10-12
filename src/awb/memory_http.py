"""Portable read/propose service. It never exposes desktop owner administration."""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import __version__


class AgentRecall(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=2000)
    scope: str = 'personal'
    limit: int = Field(default=6, ge=1, le=30)
    include_sources: bool = True
    budget_bytes: int = Field(default=12000, ge=64, le=32000)


class AgentProposal(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=1, max_length=4000)
    kind: str = 'fact'
    scope: str = 'personal'
    evidence: list[dict] = Field(default_factory=list, max_length=8)


def create_memory_service(database: Path, token_file: Path,
                          scopes: frozenset[str], public_url: str | None = None) -> FastAPI:
    from .memory.embeddings import hybrid_recall
    from .memory.store import MemoryStore

    token = token_file.read_text().strip()
    if len(token) < 32 or not scopes:
        raise ValueError('A private token of at least 32 characters and scopes are required')
    store = MemoryStore(database)
    store.initialize()
    app = FastAPI(title='Agent Workbench Memory', version=__version__,
                  docs_url=None, redoc_url=None)
    app.state.memory_store = store

    @app.middleware('http')
    async def bounds(request: Request, call_next):
        length = request.headers.get('content-length', '0')
        if not length.isdigit() or int(length) > 256 * 1024:
            return JSONResponse({'detail': 'Request too large'}, status_code=413)
        origin = request.headers.get('origin')
        allowed = (public_url or str(request.base_url)).rstrip('/')
        if origin and origin.rstrip('/') != allowed:
            return JSONResponse({'detail': 'Origin rejected'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Cache-Control'] = 'no-store'
        return response

    def auth(request: Request):
        header = request.headers.get('authorization', '')
        if not secrets.compare_digest(header, 'Bearer ' + token):
            raise HTTPException(401, 'Authentication required')

    def narrowed(raw: str | None):
        return scopes if raw is None else scopes.intersection(
            value.strip() for value in raw.split(',') if value.strip())

    def scope_allowed(scope: str):
        if scope not in scopes:
            raise HTTPException(404, 'Memory or evidence unavailable')

    def call(method, *args, receipt=False, **kwargs):
        try:
            value = method(*args, **kwargs)
            import json
            if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > 65536:
                if receipt:
                    return {key: value[key] for key in ('id', 'state', 'heads', 'scope')
                            if key in value} | {'truncated': True, 'gaps': ['response_budget']}
                raise HTTPException(413, 'Response exceeds budget; use a narrower recall')
            return value
        except LookupError as exc:
            raise HTTPException(404, 'Memory or evidence unavailable') from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(422, 'Memory request rejected') from exc

    @app.get('/health/live')
    def live():
        return {'status': 'live', 'version': __version__}

    @app.get('/v1/memory/status', dependencies=[Depends(auth)])
    def status(scopes: str | None = None):
        return call(store.status, scopes=narrowed(scopes))

    @app.post('/v1/memory/recall', dependencies=[Depends(auth)])
    def recall(body: AgentRecall):
        scope_allowed(body.scope)
        return call(hybrid_recall, store, body.query, scopes=frozenset({body.scope}),
                    limit=body.limit, budget_bytes=body.budget_bytes, include_sources=body.include_sources)

    @app.get('/v1/memory/inspect/{memory_id}', dependencies=[Depends(auth)])
    def inspect(memory_id: str, scopes: str | None = None):
        return call(store.inspect, memory_id, scopes=narrowed(scopes))

    @app.get('/v1/memory/sources/{source_id}', dependencies=[Depends(auth)])
    def source(source_id: str, scopes: str | None = None):
        return call(store.get_source, source_id, scopes=narrowed(scopes))

    @app.get('/v1/memory/profile', dependencies=[Depends(auth)])
    def profile(scope: str = 'personal', limit: int = Query(default=30, ge=1, le=30),
                scopes: str | None = None):
        scope_allowed(scope)
        return call(store.profile, scope, scopes=narrowed(scopes), limit=limit)

    @app.post('/v1/memory/propose', dependencies=[Depends(auth)])
    def propose(body: AgentProposal):
        scope_allowed(body.scope)
        return call(store.propose, body.text, body.kind, body.scope, body.evidence,
                    actor='assistant_proposal', scopes=scopes, receipt=True)

    return app
