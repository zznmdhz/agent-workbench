"""Single-owner HTTP service and same-origin web application."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys
from pathlib import Path
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import (
    check_rate,
    clear_failures,
    issue_pair_code,
    login,
    now_iso,
    pair_device,
    record_failure,
    require_collector,
    require_owner,
    require_owner_write,
    set_owner_cookie,
    token_hash,
)
from .codec import sha256
from .db import Database
from .filecheck import create_job, jobs_for_collector, record_result
from .handoff import create_handoff, get_handoff, link_continuation
from .ingest import receive_batch, resolve_quarantine, server_epoch
from .models import Batch
from .resources import record_samples
from .stats import calculate, timeline


class Credentials(BaseModel):
    password: str


class PairRequest(BaseModel):
    code: str = Field(pattern=r"^\d{9}$")
    name: str
    os: str
    environment: str


class SourceRequest(BaseModel):
    id: UUID
    device_id: UUID
    agent: str
    profile: str
    execution_surface: str = "unknown"
    content_policy: str = "full_content"


class ResolveRequest(BaseModel):
    body_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class Heartbeat(BaseModel):
    last_scan: str | None = None
    pending_count: int = Field(ge=0)
    last_error: str | None = None
    sources: dict[str, dict] = Field(default_factory=dict)


class RootMap(BaseModel):
    native_root: str
    case_sensitive: bool = True


class DeleteContent(BaseModel):
    confirmation: str
    keep_statistics: bool = True


class HandoffRequest(BaseModel):
    source_session_id: str
    target_device_id: UUID


class ContinuationRequest(BaseModel):
    target_session_id: str


class FileCheckRequest(BaseModel):
    target_device_id: UUID
    root_id: str
    relative_path: str
    operation: str = "stat"


class ProcessSample(BaseModel):
    pid: int = Field(ge=1)
    create_time_ms: int = Field(ge=0)
    rss_bytes: int | None = Field(default=None, ge=0)
    cpu_percent: str | None = None
    process_name: str | None = None


def _page_cursor(value: str | None, filters: dict) -> tuple[str, str]:
    if not value:
        return "", ""
    try:
        decoded = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(422, "Invalid cursor") from exc
    if decoded.get("projection_version") != 1 or decoded.get("filters") != sha256(filters):
        raise HTTPException(409, "cursor_stale")
    return decoded["sort"], decoded["id"]


def _cursor(sort: str, ident: str, filters: dict) -> str:
    value = {"sort": sort, "id": ident, "filters": sha256(filters), "projection_version": 1}
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def create_app(db_path: str | Path | None = None) -> FastAPI:
    path = Path(db_path or os.environ.get("AWB_DB_PATH", "./data/agent-workbench.db"))
    db = Database(path)
    db.initialize()
    app = FastAPI(title="Agent Workbench", version="0.1.0")
    app.state.db = db

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > 4 * 1024 * 1024:
            return JSONResponse(status_code=413, content={"error": {"code": "payload_too_large", "message": "Request exceeds 4 MiB"}})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'"
        return response

    @app.get("/health/live")
    def live():
        return {"status": "live"}

    @app.get("/health/ready")
    def ready():
        with db.read() as conn:
            version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        return {"status": "ready", "schema_version": version}

    @app.post("/auth/login")
    def auth_login(body: Credentials, request: Request, response: Response):
        subject = request.client.host if request.client else "unknown"
        check_rate(db, "login", subject)
        try:
            token, csrf = login(db, body.password)
        except HTTPException:
            record_failure(db, "login", subject)
            raise
        clear_failures(db, "login", subject)
        set_owner_cookie(response, token, request)
        return {"authenticated": True, "csrf": csrf}

    @app.post("/auth/logout")
    def auth_logout(request: Request, response: Response, _: None = Depends(require_owner_write)):
        token = request.cookies.get("awb_session", "")
        with db.tx() as conn:
            conn.execute("DELETE FROM web_sessions WHERE token_hash=?", (token_hash(token),))
        response.delete_cookie("awb_session", path="/")
        return {"authenticated": False}

    @app.get("/auth/me")
    def auth_me(csrf: str = Depends(require_owner)):
        return {"authenticated": True, "csrf": csrf}

    @app.post("/v1/pairing-codes")
    def pairing_code(_: None = Depends(require_owner_write)):
        return {"code": issue_pair_code(db), "expires_in_seconds": 600}

    @app.post("/v1/devices/pair")
    def pair(body: PairRequest, request: Request):
        subject = request.client.host if request.client else "unknown"
        check_rate(db, "pair", subject)
        try:
            device_id, token = pair_device(db, body.code, body.name, body.os, body.environment)
        except HTTPException:
            record_failure(db, "pair", subject)
            raise
        clear_failures(db, "pair", subject)
        return {"collector_id": device_id, "token": token, "server_epoch": server_epoch(db)}

    @app.get("/v1/devices")
    def devices(_: str = Depends(require_owner)):
        with db.read() as conn:
            rows = conn.execute("SELECT id,name,os,environment,registered_at,last_heartbeat,last_scan,pending_count,last_error,revoked_at FROM devices ORDER BY registered_at,id").fetchall()
        return {"items": [dict(r) for r in rows]}

    @app.post("/v1/sources/register")
    def register_source(body: SourceRequest, _: None = Depends(require_owner_write)):
        if body.agent not in {"codex", "hermes"} or body.content_policy not in {"full_content", "stats_only", "excluded"}:
            raise HTTPException(422, "Unsupported agent or content policy")
        with db.tx() as conn:
            device = conn.execute("SELECT 1 FROM devices WHERE id=? AND revoked_at IS NULL", (str(body.device_id),)).fetchone()
            if not device:
                raise HTTPException(404, "Device not found")
            existing = conn.execute("SELECT device_id,agent,profile FROM sources WHERE id=?", (str(body.id),)).fetchone()
            if existing and (existing[0], existing[1], existing[2]) != (str(body.device_id), body.agent, body.profile):
                raise HTTPException(409, "Source identity already bound")
            conn.execute("""INSERT OR IGNORE INTO sources(id,device_id,agent,profile,execution_surface,capability_json,created_at)
                VALUES(?,?,?,?,?,'{}',?)""", (str(body.id), str(body.device_id), body.agent, body.profile, body.execution_surface, now_iso()))
            conn.execute("UPDATE sources SET capability_json=? WHERE id=?", (json.dumps({"content_policy": body.content_policy}), str(body.id)))
        return {"source_instance_id": str(body.id), "content_policy": body.content_policy}

    @app.get("/v1/sources")
    def sources(_: str = Depends(require_owner)):
        with db.read() as conn:
            rows = conn.execute("SELECT * FROM sources ORDER BY created_at,id").fetchall()
        return {"items": [dict(r) for r in rows]}

    @app.post("/v1/ingest/batches")
    def ingest(body: Batch, collector: str = Depends(require_collector)):
        return receive_batch(db, collector, body)

    @app.post("/v1/ingest/quarantines/{receipt_id}/resolve")
    def quarantine_resolve(receipt_id: str, body: ResolveRequest, collector: str = Depends(require_collector)):
        return resolve_quarantine(db, collector, receipt_id, body.body_hash)

    @app.get("/v1/ingest/checkpoint")
    def checkpoint(outbox_epoch: UUID, collector: str = Depends(require_collector)):
        with db.read() as conn:
            row = conn.execute("SELECT durable_ack,projected_ack FROM streams WHERE collector_id=? AND epoch=?", (collector, str(outbox_epoch))).fetchone()
        return {"durable_ack_seq": row[0] if row else 0, "projected_ack_seq": row[1] if row else 0,
                "server_epoch": server_epoch(db)}

    @app.post("/v1/collectors/heartbeat")
    def heartbeat(body: Heartbeat, collector: str = Depends(require_collector)):
        with db.tx() as conn:
            conn.execute("UPDATE devices SET last_heartbeat=?,last_scan=?,pending_count=?,last_error=? WHERE id=?",
                         (now_iso(), body.last_scan, body.pending_count, body.last_error, collector))
            for source_id, state in body.sources.items():
                conn.execute("UPDATE sources SET last_scan=?,last_error=? WHERE id=? AND device_id=?",
                             (state.get("last_scan"), state.get("last_error"), source_id, collector))
        return {"server_time": now_iso(), "server_epoch": server_epoch(db)}

    @app.post("/v1/collectors/process-samples")
    def process_samples(body: list[ProcessSample], collector: str = Depends(require_collector)):
        return {"recorded": record_samples(db, collector, [x.model_dump() for x in body])}

    @app.get("/v1/resources")
    def resources(device_id: str, limit: int = Query(200, ge=1, le=1000), _: str = Depends(require_owner)):
        with db.read() as conn:
            rows = conn.execute("SELECT * FROM process_samples WHERE device_id=? ORDER BY sampled_at DESC,id DESC LIMIT ?",
                                (device_id, limit)).fetchall()
        return {"items": [dict(r) for r in rows], "notice": "Samples are point-in-time process RSS/CPU, not session attribution or continuous peaks."}

    @app.get("/v1/health/sources")
    def source_health(_: str = Depends(require_owner)):
        with db.read() as conn:
            rows = conn.execute("""SELECT s.*,d.name AS device_name,d.last_heartbeat,d.pending_count
                FROM sources s JOIN devices d ON d.id=s.device_id ORDER BY s.created_at""").fetchall()
            quarantines = conn.execute("SELECT COUNT(*) FROM deliveries WHERE status='quarantined_pending'").fetchone()[0]
        return {"items": [dict(r) for r in rows], "quarantined_count": quarantines}

    @app.get("/v1/stats")
    def stats(day: str, tz: str = "Asia/Hong_Kong", device_ids: list[str] = Query(default=[]),
              agent_ids: list[str] = Query(default=[]), model_ids: list[str] = Query(default=[]),
              _: str = Depends(require_owner)):
        try:
            return calculate(db, day, tz, device_ids, agent_ids, model_ids)
        except (ValueError, KeyError) as exc:
            raise HTTPException(422, "Invalid day or timezone") from exc

    @app.get("/v1/timeline")
    def daily_timeline(day: str, tz: str = "Asia/Hong_Kong", _: str = Depends(require_owner)):
        try:
            return timeline(db, day, tz)
        except (ValueError, KeyError) as exc:
            raise HTTPException(422, "Invalid day or timezone") from exc

    @app.get("/v1/sessions")
    def sessions(activity_from: str | None = None, activity_to: str | None = None,
                 device_id: str | None = None, agent: str | None = None,
                 cursor: str | None = None, limit: int = Query(50, ge=1, le=200),
                 _: str = Depends(require_owner)):
        filters = {"from": activity_from, "to": activity_to, "device": device_id, "agent": agent}
        sort, anchor = _page_cursor(cursor, filters)
        clauses = ["s.archived=0"]
        params: list = []
        for field, value, op in (("s.last_activity", activity_from, ">="), ("s.last_activity", activity_to, "<"),
                                 ("s.device_id", device_id, "="), ("s.agent", agent, "=")):
            if value:
                clauses.append(f"{field}{op}?")
                params.append(value)
        if cursor:
            clauses.append("(COALESCE(s.last_activity,'')<? OR (COALESCE(s.last_activity,'')=? AND s.id<?))")
            params.extend((sort, sort, anchor))
        params.append(limit + 1)
        with db.read() as conn:
            rows = conn.execute(f"""SELECT s.*,src.profile,d.name AS device_name,
                (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS message_count,
                (SELECT COUNT(*) FROM runs r WHERE r.session_id=s.id) AS run_count
                FROM sessions s JOIN sources src ON src.id=s.source_id JOIN devices d ON d.id=src.device_id
                WHERE {' AND '.join(clauses)} ORDER BY COALESCE(s.last_activity,'') DESC,s.id DESC LIMIT ?""", params).fetchall()
        items = [dict(r) for r in rows[:limit]]
        next_cursor = _cursor(items[-1]["last_activity"] or "", items[-1]["id"], filters) if len(rows) > limit else None
        return {"items": items, "next_cursor": next_cursor, "projection_version": 1}

    @app.get("/v1/sessions/{session_id}/events")
    def session_events(session_id: str, cursor: str | None = None, limit: int = Query(50, ge=1, le=200),
                       _: str = Depends(require_owner)):
        filters = {"session_id": session_id}
        sort, anchor = _page_cursor(cursor, filters)
        with db.read() as conn:
            session = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not session:
                raise HTTPException(404, "Session not found")
            if cursor:
                rows = conn.execute("""SELECT * FROM messages WHERE session_id=? AND
                    (COALESCE(occurred_at,'')>? OR (COALESCE(occurred_at,'')=? AND id>?))
                    ORDER BY COALESCE(occurred_at,''),id LIMIT ?""", (session_id, sort, sort, anchor, limit+1)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM messages WHERE session_id=? ORDER BY COALESCE(occurred_at,''),id LIMIT ?", (session_id, limit+1)).fetchall()
            runs = conn.execute("SELECT * FROM runs WHERE session_id=? ORDER BY start_at,id", (session_id,)).fetchall()
            files = conn.execute("SELECT * FROM file_events WHERE session_id=? ORDER BY checked_at,event_id", (session_id,)).fetchall()
        items = [dict(r) for r in rows[:limit]]
        next_cursor = _cursor(items[-1]["occurred_at"] or "", items[-1]["id"], filters) if len(rows) > limit else None
        return {"session": dict(session), "items": items, "runs": [dict(r) for r in runs],
                "files": [dict(r) for r in files], "next_cursor": next_cursor, "projection_version": 1}

    @app.get("/v1/search")
    def search(q: str = Query(min_length=2, max_length=200), cursor: str | None = None,
               limit: int = Query(50, ge=1, le=200), _: str = Depends(require_owner)):
        filters = {"q": q}
        sort, anchor = _page_cursor(cursor, filters)
        with db.read() as conn:
            fts = conn.execute("SELECT 1 FROM sqlite_master WHERE name='messages_fts'").fetchone()
            predicate = ("m.id IN (SELECT message_id FROM messages_fts WHERE body LIKE ? ESCAPE '!')"
                         if fts and len(q) >= 3 else "m.body LIKE ? ESCAPE '!'")
            rows = conn.execute(f"""SELECT m.id,m.session_id,m.role,m.occurred_at,
                substr(m.body,1,320) AS excerpt,s.title,s.agent FROM messages m
                JOIN sessions s ON s.id=m.session_id WHERE s.archived=0 AND {predicate}
                AND (COALESCE(m.occurred_at,'')>? OR (COALESCE(m.occurred_at,'')=? AND m.id>?))
                ORDER BY COALESCE(m.occurred_at,''),m.id LIMIT ?""",
                ("%" + q.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%", sort, sort, anchor, limit+1)).fetchall()
        items = [dict(r) for r in rows[:limit]]
        next_cursor = _cursor(items[-1]["occurred_at"] or "", items[-1]["id"], filters) if len(rows) > limit else None
        return {"items": items, "next_cursor": next_cursor, "search_scope": "stored message bodies; first 320 characters shown"}

    @app.get("/v1/files/{file_id}")
    def file_detail(file_id: str, _: str = Depends(require_owner)):
        with db.read() as conn:
            row = conn.execute("SELECT * FROM file_events WHERE event_id=?", (file_id,)).fetchone()
            if not row:
                raise HTTPException(404, "File fact not found")
            maps = conn.execute("SELECT * FROM path_maps WHERE root_id=?", (row["logical_root"],)).fetchall()
        return {"file": dict(row), "maps": [dict(r) for r in maps]}

    @app.put("/v1/roots/{root_id}/maps/{environment_id}")
    def set_path_map(root_id: str, environment_id: str, body: RootMap, _: None = Depends(require_owner_write)):
        if not body.native_root or "\x00" in body.native_root:
            raise HTTPException(422, "Invalid root")
        with db.tx() as conn:
            conn.execute("""INSERT INTO path_maps(root_id,environment_id,native_root,case_sensitive,map_version,updated_at)
                VALUES(?,?,?,?,1,?) ON CONFLICT(root_id,environment_id) DO UPDATE SET
                native_root=excluded.native_root,case_sensitive=excluded.case_sensitive,
                map_version=path_maps.map_version+1,updated_at=excluded.updated_at""",
                (root_id, environment_id, body.native_root, int(body.case_sensitive), now_iso()))
        return {"root_id": root_id, "environment_id": environment_id, "updated_at": now_iso()}

    @app.post("/v1/file-checks")
    def file_check_create(body: FileCheckRequest, _: None = Depends(require_owner_write)):
        return create_job(db, str(body.target_device_id), body.root_id, body.relative_path, body.operation)

    @app.get("/v1/file-checks/{job_id}")
    def file_check_detail(job_id: str, _: str = Depends(require_owner)):
        with db.read() as conn:
            row = conn.execute("SELECT * FROM file_check_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "File check not found")
        return dict(row)

    @app.get("/v1/collectors/jobs")
    def collector_jobs(collector: str = Depends(require_collector)):
        return {"items": jobs_for_collector(db, collector)}

    @app.post("/v1/collectors/jobs/{job_id}/result")
    def collector_job_result(job_id: str, body: dict, collector: str = Depends(require_collector)):
        return record_result(db, collector, job_id, body)

    @app.post("/v1/handoffs")
    def handoff_create(body: HandoffRequest, _: None = Depends(require_owner_write)):
        return create_handoff(db, body.source_session_id, str(body.target_device_id), db.path.parent / "handoffs")

    @app.get("/v1/handoffs/{handoff_id}")
    def handoff_detail(handoff_id: str, _: str = Depends(require_owner)):
        return get_handoff(db, handoff_id)

    @app.get("/v1/handoffs/{handoff_id}/download")
    def handoff_download(handoff_id: str, _: str = Depends(require_owner)):
        with db.read() as conn:
            row = conn.execute("SELECT archive_path,revoked_at FROM handoffs WHERE id=?", (handoff_id,)).fetchone()
        if not row or row["revoked_at"] or not Path(row["archive_path"]).is_file():
            raise HTTPException(404, "Handoff archive unavailable")
        return FileResponse(row["archive_path"], media_type="application/zip", filename=f"handoff-{handoff_id}.zip")

    @app.post("/v1/handoffs/{handoff_id}/continuations")
    def handoff_link(handoff_id: str, body: ContinuationRequest, _: None = Depends(require_owner_write)):
        return link_continuation(db, handoff_id, body.target_session_id)

    @app.delete("/v1/sessions/{session_id}/content")
    def delete_content(session_id: str, body: DeleteContent, _: None = Depends(require_owner_write)):
        if body.confirmation != session_id:
            raise HTTPException(422, "Confirmation must equal session ID")
        with db.tx() as conn:
            row = conn.execute("SELECT source_id,native_id FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Session not found")
            conn.execute("INSERT OR REPLACE INTO tombstones(source_id,native_session_id,deleted_at,policy_version) VALUES(?,?,?,1)",
                         (row["source_id"], row["native_id"], now_iso()))
            conn.execute("UPDATE messages SET body=NULL,content_state='missing',omission_reason='owner_deleted' WHERE session_id=?", (session_id,))
            try:
                conn.execute("DELETE FROM messages_fts WHERE message_id IN (SELECT id FROM messages WHERE session_id=?)", (session_id,))
            except sqlite3.OperationalError:
                pass
            conn.execute("""UPDATE deliveries SET raw_json=NULL WHERE event_id IN
                (SELECT event_id FROM messages WHERE session_id=?)""", (session_id,))
            conn.execute("DELETE FROM events WHERE event_id IN (SELECT event_id FROM messages WHERE session_id=?)", (session_id,))
            if not body.keep_statistics:
                conn.execute("DELETE FROM usage_observations WHERE session_id=?", (session_id,))
                conn.execute("DELETE FROM runs WHERE session_id=?", (session_id,))
            archives = [Path(r[0]) for r in conn.execute("SELECT archive_path FROM handoffs WHERE source_session_id=?", (session_id,))]
            conn.execute("UPDATE handoffs SET revoked_at=? WHERE source_session_id=? AND revoked_at IS NULL", (now_iso(), session_id))
        for archive in archives:
            archive.unlink(missing_ok=True)
        return {"deleted": True, "statistics_retained": body.keep_statistics,
                "backup_notice": "Older backups may retain the content until their retention period expires."}

    bundle_root = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
    web_dist = bundle_root / "web" / "dist"
    if web_dist.exists():
        app.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="assets")

        @app.get("/")
        def index():
            return FileResponse(web_dist / "index.html")

    return app


app = create_app()
