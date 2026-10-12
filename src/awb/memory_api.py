"""Owner UI for the isolated memory service; existing dashboard routes stay unchanged."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import archive
from .auth import require_owner, require_owner_write


class SpaceRequest(BaseModel):
    mode: str
    join_code: str | None = Field(default=None, max_length=4096)


class MemoryProposal(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    kind: str = 'fact'
    scope: str = Field(default='personal', min_length=1, max_length=120)


class MemoryChange(BaseModel):
    action: str
    parents: list[str] = Field(max_length=32)
    payload: dict[str, Any] | None = None


class RecallQuery(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    scope: str = Field(default='personal', min_length=1, max_length=120)
    limit: int = Field(default=6, ge=1, le=30)


def install_memory_routes(app, archive_db) -> None:
    """Memory failures are reported within this feature, never during dashboard startup."""
    router = APIRouter(prefix='/v1/memory', tags=['memory'])
    store, worker, setup_error = None, None, None
    try:
        from .memory.store import MemoryStore
        from .memory.worker import MemoryWorker

        with archive_db.read() as conn:
            row = conn.execute(
                "SELECT value FROM archive_settings WHERE key='local_device_id'"
            ).fetchone()
        memory_path = archive_db.path.parent / 'memory' / 'memory.db'
        store = MemoryStore(memory_path,
                            device_id=row[0] if row and not memory_path.exists() else None)
        store.initialize()
        worker = MemoryWorker(store, archive_db,
                              lambda: store.settings().get('sync_root') or
                              archive.sync_root(archive_db))
    except Exception as exc:
        # Do not expose exceptions that can contain a provider response or a credential.
        setup_error = type(exc).__name__
    app.state.memory_store = store
    app.state.memory_worker = worker

    def available():
        if setup_error or store is None:
            raise HTTPException(503, {'code': 'memory_unavailable', 'reason': setup_error})
        return store

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except LookupError as exc:
            raise HTTPException(404, 'Memory or evidence unavailable') from exc
        except (ValueError, RuntimeError) as exc:
            # Core messages are stable error labels, not model output.
            label = str(exc)
            code = 409 if any(word in label.lower() for word in
                              ('stale', 'conflict', 'space', 'parent', 'already')) else 422
            raise HTTPException(code, label[:160]) from exc

    @router.get('/status')
    def status(_: str = Depends(require_owner)):
        if setup_error:
            return {'available': False, 'reason': setup_error, 'dashboard_unaffected': True}
        value = call(available().status)
        return {**value, 'available': True, 'worker': worker.snapshot() if worker else {}}

    @router.post('/space')
    def configure(body: SpaceRequest, _: None = Depends(require_owner_write)):
        value = call(available().configure_space, body.mode, join_code=body.join_code)
        return value

    @router.post('/join-credential')
    def credential(_: None = Depends(require_owner_write)):
        return {'join_code': call(available().join_code)}

    @router.get('/settings')
    def settings(_: str = Depends(require_owner)):
        return available().settings()

    @router.post('/settings')
    def set_settings(body: dict[str, Any], _: None = Depends(require_owner_write)):
        if len(json.dumps(body).encode()) > 16000:
            raise HTTPException(413, 'Settings too large')
        if body.get('sync_root'):
            path = Path(body['sync_root']).expanduser()
            if not path.is_absolute() or not path.is_dir():
                raise HTTPException(422, 'Select an existing absolute NAS sync folder')
            body['sync_root'] = str(path)
        value = call(available().update_settings, body)
        if worker:
            worker.start()
            worker.request()
        return value

    @router.post('/refresh')
    def refresh(_: None = Depends(require_owner_write)):
        if worker:
            worker.start()
            worker.request()
        return {'queued': bool(worker), 'worker': worker.snapshot() if worker else {}}

    @router.post('/retry-failed')
    def retry_failed(_: None = Depends(require_owner_write)):
        result = call(available().retry_failed_jobs, limit=100)
        if worker:
            worker.start()
            worker.request()
        return result

    @router.get('/items')
    def items(query: str = '', state: str | None = None, scope: str | None = None,
              limit: int = 50, offset: int = 0, _: str = Depends(require_owner)):
        if len(query) > 2000 or not 1 <= limit <= 100 or not 0 <= offset <= 100000:
            raise HTTPException(422, 'Invalid pagination')
        return call(available().list_memories, query=query, state=state, scope=scope,
                    limit=limit, offset=offset)

    @router.get('/items/{memory_id}')
    def inspect(memory_id: str, _: str = Depends(require_owner)):
        return call(available().inspect, memory_id)

    @router.get('/sources/{source_id}')
    def source(source_id: str, _: str = Depends(require_owner)):
        return call(available().get_source, source_id)

    @router.post('/propose')
    def propose(body: MemoryProposal, _: None = Depends(require_owner_write)):
        current = available()
        import hashlib
        from uuid import uuid4

        text = body.text.strip()
        evidence_id = 'owner-' + str(uuid4())
        owner_source = {'id': evidence_id, 'agent': 'owner', 'native_id': evidence_id,
                        'message_id': evidence_id, 'device_id': current.status()['device_id'],
                        'role': 'user', 'body': text, 'occurred_at': archive.utc_now(),
                        'scope': body.scope, 'complete': True,
                        'checksum': hashlib.sha256(text.encode()).hexdigest()}
        owner_source.pop('id')
        saved = call(current.ingest_source, owner_source, local=False)
        evidence_id = saved['id']
        result = call(current.propose, text, body.kind, body.scope,
                      [{'source_id': evidence_id, 'quote': text}], actor='owner')
        return result

    @router.post('/items/{memory_id}/change')
    def change(memory_id: str, body: MemoryChange, _: None = Depends(require_owner_write)):
        current = available()
        payload = body.payload
        if body.action in {'edit', 'resolve'} and payload and 'text' in payload:
            # A correction is the owner's new statement; do not fabricate a quote in an old source.
            prior = call(current.inspect, memory_id)
            memory = prior.get('memory', prior)
            if set(memory['heads']) != set(body.parents):
                raise HTTPException(409, 'stale_revision')
            scope = memory['scope']
            if payload.get('scope', scope) != scope:
                raise HTTPException(422, 'Create a new scoped memory to change visibility')
            text = str(payload['text']).strip()
            if not 1 <= len(text) <= 4000:
                raise HTTPException(422, 'Invalid memory text')
            return call(current.change_owner_text, memory_id, body.action, text,
                        payload.get('kind', memory['kind']), parents=body.parents,
                        state=payload.get('state'))
        return call(current.change, memory_id, body.action, payload=payload, parents=body.parents)

    @router.post('/recall')
    def recall(body: RecallQuery, _: str = Depends(require_owner)):
        from .memory.embeddings import hybrid_recall

        return call(hybrid_recall, available(), body.query, scopes=frozenset({body.scope}),
                    limit=body.limit, budget_bytes=12000)

    @router.get('/model-usage')
    def usage(_: str = Depends(require_owner)):
        return call(available().model_usage)

    @router.get('/agent-config')
    def agent_config(scope: str = 'personal', _: str = Depends(require_owner)):
        if not scope or len(scope) > 120:
            raise HTTPException(422, 'Invalid scope')
        current = available()
        command = sys.executable
        if getattr(sys, 'frozen', False):
            cli = (Path(sys.executable).parent.parent / 'Resources' / 'cli' / 'AgentWorkbenchCLI'
                   if sys.platform == 'darwin' else Path(sys.executable).parent / 'AgentWorkbenchCLI.exe')
            if not cli.is_file():
                raise HTTPException(503, 'This build has no console MCP launcher')
            command = str(cli)
            args = ['memory-mcp']
        else:
            args = ['-m', 'awb.memory.mcp']
        args += ['--database', str(current.path), '--scopes', scope]
        return {'mcpServers': {'agent-workbench-memory': {'command': command, 'args': args}},
                'note': 'Agent reads this computer’s replica. Only reviewed memory is authoritative.'}

    app.include_router(router)
