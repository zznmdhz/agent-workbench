"""Single-owner HTTP service and same-origin web application."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import (
    check_rate,
    clear_failures,
    initialize_owner,
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
from .codec import day_bounds_utc_ms, sha256
from .db import Database
from .filecheck import check_local, create_job, jobs_for_collector, record_result
from .handoff import create_handoff, get_handoff, link_continuation
from .ingest import receive_batch, resolve_quarantine, server_epoch
from .local import (
    content_backfill_status,
    preview_content_backfill,
    purge_deleted_content,
    request_content_backfill,
    set_local_source_policy,
)
from .models import Batch
from .resources import record_samples
from .stats import calculate, metric_contributors, timeline


class Credentials(BaseModel):
    password: str


class SetupCredentials(BaseModel):
    password: str
    confirmation: str


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


class LocalPolicyRequest(BaseModel):
    content_policy: str


class ContentBackfillRequest(BaseModel):
    source_ids: list[UUID] = Field(min_length=1, max_length=20)
    start_at: str | None = None
    end_at: str | None = None
    native_session_ids: list[str] | None = Field(default=None, max_length=100)
    cwd_prefix: str | None = Field(default=None, max_length=1000)


class SessionTitleRequest(BaseModel):
    title: str | None = Field(default=None, max_length=120)


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


def _cursor(sort: str, ident: str, filters: dict, direction: str | None = None) -> str:
    value = {"sort": sort, "id": ident, "filters": sha256(filters), "projection_version": 1}
    if direction:
        value["direction"] = direction
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def _event_cursor(value: str | None, filters: dict) -> tuple[str, str, str]:
    sort, ident = _page_cursor(value, filters)
    if not value:
        return sort, ident, "newer"
    decoded = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    direction = decoded.get("direction", "newer")
    if direction not in {"older", "newer"}:
        raise HTTPException(422, "Invalid cursor direction")
    return sort, ident, direction


def _session_identity(conn: sqlite3.Connection, row: dict) -> dict:
    """Present a useful name without confusing a working-directory basename with a title."""
    sid = row["id"]
    custom = conn.execute("SELECT user_title FROM session_titles WHERE session_id=?", (sid,)).fetchone()
    first = conn.execute("""SELECT body FROM messages WHERE session_id=? AND role='user'
        AND input_origin='human' AND body IS NOT NULL AND body<>''
        ORDER BY COALESCE(occurred_at,''),source_order,id LIMIT 1""", (sid,)).fetchone()
    preview = " ".join(first[0].split())[:120] if first else None
    if custom:
        display, basis = custom[0], "user_title"
    elif row.get("title"):
        display, basis = row["title"], "source_title"
    elif preview:
        display, basis = preview[:80], "first_user_message"
    else:
        when = (row.get("created_at") or row.get("last_activity") or "")[:16].replace("T", " ")
        display = f"{row['agent'].capitalize()} · {when or '时间未知'} · {row['native_id'][:8]}"
        basis = "agent_time_id"
    coverage = conn.execute("""SELECT COUNT(*) AS total,
        SUM(CASE WHEN body IS NOT NULL THEN 1 ELSE 0 END) AS readable
        FROM messages WHERE session_id=?""", (sid,)).fetchone()
    readable, total = coverage["readable"] or 0, coverage["total"]
    return {**row, "display_title": display, "title_basis": basis, "preview": preview,
            "content_coverage": {"readable": readable, "total": total, "missing": total - readable,
                                 "state": "complete" if total and readable == total else (
                                     "partial" if readable else "uncollected" if total else "no_messages")}}


def _file_view(row: sqlite3.Row | dict, run_ids: dict[str, str] | None = None) -> dict:
    item = dict(row)
    item["detail_status"] = "not_checked"
    item["evidence_status"] = "recorded_operation" if item.get("operation_status") == "succeeded" else "unverified"
    item["run_id"] = (run_ids or {}).get(item.get("run_ref"))
    return item


def create_app(db_path: str | Path | None = None, *, desktop_mode: bool = False) -> FastAPI:
    path = Path(db_path or os.environ.get("AWB_DB_PATH", "./data/agent-workbench.db"))
    db = Database(path)
    db.initialize()
    app = FastAPI(title="Agent Workbench", version="0.2.4")
    app.state.db = db
    app.state.desktop_mode = desktop_mode
    app.state.shutdown_callback = None

    def owner_exists() -> bool:
        with db.read() as conn:
            return conn.execute("SELECT 1 FROM owner WHERE id=1").fetchone() is not None

    def require_local_desktop(request: Request) -> None:
        if not app.state.desktop_mode or not request.client or request.client.host not in {"127.0.0.1", "::1"}:
            raise HTTPException(403, "Available only in the local desktop app")
        if request.url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise HTTPException(403, "Invalid local host")
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Invalid origin")

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
        return {"status": "ready", "schema_version": version, "app_version": "0.2.4"}

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

    @app.get("/auth/setup-status")
    def setup_status():
        return {"needs_setup": not owner_exists(), "web_setup_available": desktop_mode}

    @app.post("/auth/setup")
    def auth_setup(body: SetupCredentials, request: Request, response: Response):
        require_local_desktop(request)
        if owner_exists():
            raise HTTPException(409, "Owner account already exists")
        if body.password != body.confirmation:
            raise HTTPException(422, "Passwords do not match")
        if len(body.password) < 12:
            raise HTTPException(422, "Password must have at least 12 characters")
        try:
            initialize_owner(db, body.password)
        except ValueError as exc:
            raise HTTPException(409, "Owner account already exists") from exc
        token, csrf = login(db, body.password)
        set_owner_cookie(response, token, request)
        return {"authenticated": True, "csrf": csrf}

    @app.post("/auth/cancel-setup")
    def cancel_setup(request: Request, background_tasks: BackgroundTasks):
        require_local_desktop(request)
        if owner_exists():
            raise HTTPException(409, "Owner account already exists")
        if app.state.shutdown_callback is None:
            raise HTTPException(503, "Desktop shutdown unavailable")
        background_tasks.add_task(app.state.shutdown_callback)
        return {"stopping": True}

    @app.post("/auth/close-local")
    def close_local(request: Request, background_tasks: BackgroundTasks):
        require_local_desktop(request)
        if app.state.shutdown_callback is None:
            raise HTTPException(503, "Desktop shutdown unavailable")
        background_tasks.add_task(app.state.shutdown_callback)
        return {"stopping": True}

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

    @app.post("/v1/local/shutdown")
    def local_shutdown(request: Request, background_tasks: BackgroundTasks,
                       _: None = Depends(require_owner_write)):
        require_local_desktop(request)
        if app.state.shutdown_callback is None:
            raise HTTPException(503, "Desktop shutdown unavailable")
        background_tasks.add_task(app.state.shutdown_callback)
        return {"stopping": True}

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

    @app.post("/v1/local/sources/{source_id}/policy")
    def local_source_policy(source_id: UUID, body: LocalPolicyRequest,
                            _: None = Depends(require_owner_write)):
        try:
            set_local_source_policy(db, str(source_id), body.content_policy)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"source_id": str(source_id), "content_policy": body.content_policy,
                "notice": "Policy changes apply to future source records only."}

    @app.get("/v1/local/content-backfills")
    def local_content_backfills(request: Request, _: str = Depends(require_owner)):
        require_local_desktop(request)
        return {"items": content_backfill_status(db)}

    @app.post("/v1/local/content-backfills/preview")
    def local_content_backfill_preview(body: ContentBackfillRequest, request: Request,
                                       _: None = Depends(require_owner_write)):
        require_local_desktop(request)
        try:
            return preview_content_backfill(db, [str(x) for x in body.source_ids],
                                            start_at=body.start_at, end_at=body.end_at,
                                            native_session_ids=body.native_session_ids,
                                            cwd_prefix=body.cwd_prefix)
        except FileNotFoundError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/v1/local/content-backfills")
    def local_content_backfill(body: ContentBackfillRequest, request: Request,
                               _: None = Depends(require_owner_write)):
        require_local_desktop(request)
        try:
            return request_content_backfill(db, [str(x) for x in body.source_ids],
                                            start_at=body.start_at, end_at=body.end_at,
                                            native_session_ids=body.native_session_ids,
                                            cwd_prefix=body.cwd_prefix)
        except FileNotFoundError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

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
            rows = conn.execute("""SELECT s.*,d.name AS device_name,d.last_heartbeat,d.pending_count,
                (SELECT COUNT(*) FROM messages m JOIN sessions se ON se.id=m.session_id WHERE se.source_id=s.id) AS message_count,
                (SELECT COUNT(*) FROM messages m JOIN sessions se ON se.id=m.session_id WHERE se.source_id=s.id AND m.body IS NOT NULL) AS readable_count,
                (SELECT COUNT(*) FROM file_events f JOIN sessions se ON se.id=f.session_id WHERE se.source_id=s.id) AS file_evidence_count
                FROM sources s JOIN devices d ON d.id=s.device_id ORDER BY s.created_at""").fetchall()
            quarantines = conn.execute("SELECT COUNT(*) FROM deliveries WHERE status='quarantined_pending'").fetchone()[0]
        items = []
        for row in rows:
            item = dict(row)
            item["scan_state"] = "error" if item["last_error"] else "scanned" if item["last_scan"] else "pending"
            item["content_coverage"] = {"readable": item["readable_count"], "total": item["message_count"]}
            items.append(item)
        return {"items": items, "quarantined_count": quarantines,
                "status_note": "Service connectivity, source scan, and stored content coverage are separate states."}

    @app.get("/v1/stats")
    def stats(day: str, through: str | None = None, tz: str = "Asia/Hong_Kong", device_ids: list[str] = Query(default=[]),
              agent_ids: list[str] = Query(default=[]), model_ids: list[str] = Query(default=[]),
              _: str = Depends(require_owner)):
        try:
            return calculate(db, day, tz, device_ids, agent_ids, model_ids, through)
        except (ValueError, KeyError) as exc:
            raise HTTPException(422, "Invalid day or timezone") from exc

    @app.get("/v1/timeline")
    def daily_timeline(day: str, tz: str = "Asia/Hong_Kong", device_ids: list[str] = Query(default=[]),
                       agent_ids: list[str] = Query(default=[]), model_ids: list[str] = Query(default=[]),
                       _: str = Depends(require_owner)):
        try:
            result = timeline(db, day, tz, device_ids, agent_ids, model_ids)
        except (ValueError, KeyError) as exc:
            raise HTTPException(422, "Invalid day or timezone") from exc
        with db.read() as conn:
            identity_cache: dict[str, dict] = {}
            for item in result["items"]:
                sid = item["session_id"]
                if sid not in identity_cache:
                    row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
                    identity_cache[sid] = _session_identity(conn, dict(row)) if row else {}
                identity = identity_cache[sid]
                item["display_title"] = identity.get("display_title")
                item["title_basis"] = identity.get("title_basis")
                first = conn.execute("""SELECT body FROM messages WHERE session_id=? AND turn_id=?
                    AND role='user' AND input_origin='human' AND body IS NOT NULL
                    ORDER BY COALESCE(occurred_at,''),id LIMIT 1""", (sid, item["native_id"])).fetchone()
                item["run_preview"] = " ".join(first[0].split())[:100] if first else None
        return result

    @app.get("/v1/models")
    def models(_: str = Depends(require_owner)):
        with db.read() as conn:
            rows = conn.execute("""SELECT DISTINCT model FROM runs WHERE model IS NOT NULL
                UNION SELECT DISTINCT model FROM usage_observations WHERE model IS NOT NULL
                ORDER BY model""").fetchall()
        return {"items": [r[0] for r in rows]}

    @app.get("/v1/stats/contributors")
    def stats_contributors(metric_id: str, day: str, through: str | None = None, tz: str = "Asia/Hong_Kong",
                           device_ids: list[str] = Query(default=[]), agent_ids: list[str] = Query(default=[]),
                           model_ids: list[str] = Query(default=[]), _: str = Depends(require_owner)):
        try:
            return metric_contributors(db, metric_id, day, tz, through, device_ids, agent_ids, model_ids)
        except (ValueError, KeyError) as exc:
            raise HTTPException(422, "Invalid metric, day or timezone") from exc

    @app.get("/v1/sessions")
    def sessions(activity_from: str | None = None, activity_to: str | None = None,
                 activity_day: str | None = None, activity_through: str | None = None,
                 tz: str = "Asia/Hong_Kong",
                 device_id: str | None = None, agent: str | None = None, model: str | None = None,
                 cursor: str | None = None, limit: int = Query(50, ge=1, le=200),
                 _: str = Depends(require_owner)):
        if activity_day:
            try:
                lower_ms, _ = day_bounds_utc_ms(activity_day, tz)
                _, upper_ms = day_bounds_utc_ms(activity_through or activity_day, tz)
                activity_from = datetime.fromtimestamp(lower_ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                activity_to = datetime.fromtimestamp(upper_ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            except (ValueError, KeyError) as exc:
                raise HTTPException(422, "Invalid activity date or timezone") from exc
        filters = {"from": activity_from, "to": activity_to, "device": device_id, "agent": agent, "model": model}
        sort, anchor = _page_cursor(cursor, filters)
        clauses = ["s.archived=0"]
        params: list = []
        matching_params: list = []
        matching_expr = "s.last_activity"
        fallback_sort = ""
        if activity_from or activity_to:
            clauses.append("""EXISTS (SELECT 1 FROM runs ar WHERE ar.session_id=s.id AND ar.start_at<?
                AND (ar.end_at>? OR (ar.end_at IS NULL AND ar.start_at>=?)))
                OR EXISTS (SELECT 1 FROM messages am WHERE am.session_id=s.id AND am.occurred_at>=? AND am.occurred_at<?)""")
            clauses[-1] = "(" + clauses[-1] + ")"
            lower, upper = activity_from or "0000-01-01T00:00:00Z", activity_to or "9999-12-31T23:59:59Z"
            params.extend((upper, lower, lower, lower, upper))
            matching_expr = """(SELECT MAX(at) FROM (
                SELECT occurred_at AS at FROM messages WHERE session_id=s.id AND occurred_at>=? AND occurred_at<?
                UNION ALL SELECT start_at FROM runs WHERE session_id=s.id AND start_at>=? AND start_at<?
                UNION ALL SELECT end_at FROM runs WHERE session_id=s.id AND end_at>=? AND end_at<?))"""
            matching_params.extend((lower, upper, lower, upper, lower, upper))
            fallback_sort = lower
        for field, value, op in (("s.device_id", device_id, "="), ("s.agent", agent, "=")):
            if value:
                clauses.append(f"{field}{op}?")
                params.append(value)
        if model:
            clauses.append("EXISTS (SELECT 1 FROM runs mr WHERE mr.session_id=s.id AND mr.model=? AND mr.model_attribution IN ('single','reported'))")
            params.append(model)
        cursor_clause = "(sort_at<? OR (sort_at=? AND id<?))" if cursor else "1=1"
        cursor_params = [sort, sort, anchor] if cursor else []
        with db.read() as conn:
            rows = conn.execute(f"""WITH base AS (SELECT s.*,src.profile,d.name AS device_name,
                (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS message_count,
                (SELECT COUNT(*) FROM runs r WHERE r.session_id=s.id) AS run_count,
                (SELECT COUNT(*) FROM file_events f WHERE f.session_id=s.id) AS file_evidence_count,
                {matching_expr} AS matched_at
                FROM sessions s JOIN sources src ON src.id=s.source_id JOIN devices d ON d.id=src.device_id
                WHERE {' AND '.join(clauses)}),
                ranked AS (SELECT base.*,COALESCE(matched_at,?) AS sort_at FROM base)
                SELECT * FROM ranked WHERE {cursor_clause} ORDER BY sort_at DESC,id DESC LIMIT ?""",
                [*matching_params, *params, fallback_sort, *cursor_params, limit + 1]).fetchall()
            items = []
            for row in rows[:limit]:
                item = _session_identity(conn, dict(row))
                if activity_from or activity_to:
                    matched = row["matched_at"]
                    item["latest_matching_activity"] = matched
                    item["matching_activity_basis"] = "observed_event" if matched else "overlapping_run_no_event"
                else:
                    item["latest_matching_activity"] = item["last_activity"]
                    item["matching_activity_basis"] = "session_latest"
                items.append(item)
        next_cursor = _cursor(items[-1]["sort_at"], items[-1]["id"], filters) if len(rows) > limit else None
        return {"items": items, "next_cursor": next_cursor, "projection_version": 1}

    @app.patch("/v1/sessions/{session_id}/title")
    def set_session_title(session_id: str, body: SessionTitleRequest,
                          _: None = Depends(require_owner_write)):
        value = " ".join(body.title.split()) if body.title is not None else ""
        if len(value) > 120:
            raise HTTPException(422, "Title must have at most 120 characters")
        with db.tx() as conn:
            session = conn.execute("SELECT * FROM sessions WHERE id=? AND archived=0", (session_id,)).fetchone()
            if not session:
                raise HTTPException(404, "Session not found")
            if value:
                conn.execute("""INSERT INTO session_titles(session_id,user_title,updated_at) VALUES(?,?,?)
                    ON CONFLICT(session_id) DO UPDATE SET user_title=excluded.user_title,updated_at=excluded.updated_at""",
                    (session_id, value, now_iso()))
            else:
                conn.execute("DELETE FROM session_titles WHERE session_id=?", (session_id,))
            return {"session": _session_identity(conn, dict(session))}

    @app.get("/v1/sessions/{session_id}/events")
    def session_events(session_id: str, focus: str = "latest", activity_day: str | None = None,
                       tz: str = "Asia/Hong_Kong", run_id: str | None = None,
                       message_id: str | None = None, cursor: str | None = None,
                       limit: int = Query(50, ge=1, le=200),
                       _: str = Depends(require_owner)):
        if focus not in {"latest", "date", "run", "message", "all"}:
            raise HTTPException(422, "Invalid focus")
        if focus == "date" and not activity_day or focus == "run" and not run_id or focus == "message" and not message_id:
            raise HTTPException(422, "Missing focus target")
        filters = {"session_id": session_id, "focus": focus, "activity_day": activity_day,
                   "tz": tz, "run_id": run_id, "message_id": message_id}
        sort, anchor, direction = _event_cursor(cursor, filters)
        with db.read() as conn:
            session = conn.execute("""SELECT s.*,src.profile,d.name AS device_name,
                (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS message_count,
                (SELECT COUNT(*) FROM runs r WHERE r.session_id=s.id) AS run_count,
                (SELECT COUNT(*) FROM file_events f WHERE f.session_id=s.id) AS file_evidence_count,
                src.last_scan,src.last_event,src.last_error,src.capability_json
                FROM sessions s JOIN sources src ON src.id=s.source_id
                LEFT JOIN devices d ON d.id=s.device_id WHERE s.id=?""", (session_id,)).fetchone()
            if not session:
                raise HTTPException(404, "Session not found")
            target_at = None
            run_predicate = None
            run_params: list = []
            focus_mapping_counts = None
            if focus == "run":
                target = conn.execute("SELECT native_id,start_at,end_at FROM runs WHERE id=? AND session_id=?", (run_id, session_id)).fetchone()
                if not target:
                    raise HTTPException(404, "Run not found in session")
                first_turn_message = conn.execute("""SELECT occurred_at FROM messages WHERE session_id=? AND turn_id=?
                    ORDER BY COALESCE(occurred_at,''),id LIMIT 1""", (session_id, target["native_id"])).fetchone()
                target_at = first_turn_message[0] if first_turn_message else target["start_at"]
                # Native turn membership is authoritative. A legacy message without a
                # matching turn may be inferred only inside this single closed run.
                run_predicate = """(turn_id=? OR ((turn_id IS NULL OR NOT EXISTS
                    (SELECT 1 FROM runs matched WHERE matched.session_id=messages.session_id
                     AND matched.native_id=messages.turn_id))
                    AND occurred_at>=? AND occurred_at<=? AND
                    (SELECT COUNT(*) FROM runs window_run WHERE window_run.session_id=messages.session_id
                     AND window_run.start_at IS NOT NULL AND window_run.end_at IS NOT NULL
                     AND messages.occurred_at BETWEEN window_run.start_at AND window_run.end_at)=1))"""
                run_params = [target["native_id"], target["start_at"], target["end_at"]]
                mapped = conn.execute(f"""SELECT COUNT(*) AS total,
                    SUM(CASE WHEN turn_id=? THEN 1 ELSE 0 END) AS direct,
                    SUM(CASE WHEN body IS NOT NULL THEN 1 ELSE 0 END) AS readable
                    FROM messages WHERE session_id=? AND {run_predicate}""",
                    (target["native_id"], session_id, *run_params)).fetchone()
                focus_mapping_counts = {"total": mapped["total"], "direct": mapped["direct"] or 0,
                                        "inferred": mapped["total"] - (mapped["direct"] or 0),
                                        "readable": mapped["readable"] or 0}
            elif focus == "message":
                target = conn.execute("SELECT occurred_at FROM messages WHERE id=? AND session_id=?", (message_id, session_id)).fetchone()
                if not target:
                    raise HTTPException(404, "Message not found in session")
                target_at = target[0]
            elif focus == "date":
                try:
                    lower_ms, upper_ms = day_bounds_utc_ms(activity_day, tz)
                    target_at = datetime.fromtimestamp(lower_ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                    date_end = datetime.fromtimestamp(upper_ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                except (ValueError, KeyError) as exc:
                    raise HTTPException(422, "Invalid activity date or timezone") from exc
            clauses = ["session_id=?"]
            params: list = [session_id]
            if focus == "date":
                clauses.extend(["occurred_at>=?", "occurred_at<?"])
                params.extend([target_at, date_end])
            elif focus == "run":
                clauses.append(run_predicate)
                params.extend(run_params)
            if cursor:
                op = "<" if direction == "older" else ">"
                clauses.append(f"(COALESCE(occurred_at,''){op}? OR (COALESCE(occurred_at,'')=? AND id{op}?))")
                params.extend((sort, sort, anchor))
            elif focus == "message" and target_at:
                clauses.append("(COALESCE(occurred_at,'')>? OR (COALESCE(occurred_at,'')=? AND id>=?))")
                params.extend((target_at, target_at, message_id))
            elif focus == "message" and target_at:
                clauses.append("COALESCE(occurred_at,'')>=?")
                params.append(target_at)
            order = "DESC" if direction == "older" or (not cursor and focus == "latest") else "ASC"
            rows = conn.execute(f"""SELECT * FROM messages WHERE {' AND '.join(clauses)}
                ORDER BY COALESCE(occurred_at,'') {order},id {order} LIMIT ?""", (*params, limit + 1)).fetchall()
            if order == "DESC":
                page = list(reversed(rows[:limit]))
            else:
                page = list(rows[:limit])
            runs = conn.execute("SELECT * FROM runs WHERE session_id=? ORDER BY start_at,id", (session_id,)).fetchall()
            files = conn.execute("""SELECT f.*,e.occurred_at AS operation_at FROM file_events f
                LEFT JOIN events e ON e.event_id=f.event_id WHERE f.session_id=?
                ORDER BY COALESCE(e.occurred_at,f.checked_at),f.event_id""", (session_id,)).fetchall()
            direct_counts = {r["turn_id"]: (r["n"], r["readable"]) for r in conn.execute("""SELECT turn_id,COUNT(*) AS n,
                SUM(CASE WHEN body IS NOT NULL THEN 1 ELSE 0 END) AS readable
                FROM messages WHERE session_id=? AND turn_id IS NOT NULL GROUP BY turn_id""", (session_id,))}
            unassigned = conn.execute("""SELECT COUNT(*) FROM messages m WHERE m.session_id=? AND
                (m.turn_id IS NULL OR NOT EXISTS (SELECT 1 FROM runs r WHERE r.session_id=m.session_id AND r.native_id=m.turn_id))""", (session_id,)).fetchone()[0]
            enriched_session = _session_identity(conn, dict(session))
        run_views = []
        native_run_ids = {}
        for row in runs:
            item = dict(row)
            count, readable = direct_counts.get(item["native_id"], (0, 0))
            item.update({"message_count_basis": "native_turn_id_partial" if unassigned else "native_turn_id",
                         "file_count": sum(f["run_ref"] == item["native_id"] for f in files)})
            if focus == "run" and item["id"] == run_id:
                count = focus_mapping_counts["total"]
                readable = focus_mapping_counts["readable"]
                item["message_count_basis"] = ("native_turn_and_unique_time_window"
                                               if focus_mapping_counts["inferred"] else "native_turn_id")
            if count or not unassigned:
                item["message_count"] = count
                item["readable_count"] = readable or 0
            run_views.append(item)
            native_run_ids[item["native_id"]] = item["id"]
        items = []
        for row in page:
            item = dict(row)
            native_turn = item["turn_id"]
            if native_turn in native_run_ids:
                item["run_id"] = native_run_ids[native_turn]
                item["turn_mapping_basis"] = "native_turn_id"
            else:
                # Timestamp-only attribution is safe only when exactly one closed run contains it.
                at = item["occurred_at"]
                candidates = [r["id"] for r in run_views if at and r["start_at"] and r["end_at"]
                              and r["start_at"] <= at <= r["end_at"]]
                item["run_id"] = candidates[0] if len(candidates) == 1 else None
                item["turn_mapping_basis"] = "unique_time_window" if len(candidates) == 1 else "unassigned"
            items.append(item)
        has_more = len(rows) > limit
        prev_cursor = (_cursor(items[0]["occurred_at"] or "", items[0]["id"], filters, "older")
                       if items and ((order == "DESC" and has_more) or (direction == "older" and has_more)) else None)
        next_cursor = (_cursor(items[-1]["occurred_at"] or "", items[-1]["id"], filters, "newer")
                       if items and order == "ASC" and has_more else None)
        return {"session": enriched_session, "items": items, "runs": run_views,
                "files": [_file_view(f, native_run_ids) for f in files], "next_cursor": next_cursor,
                "prev_cursor": prev_cursor, "page_direction": "older" if order == "DESC" else "newer",
                "focus": {"mode": focus, "activity_day": activity_day, "run_id": run_id,
                           "message_id": message_id, "target_at": target_at,
                           "mapping_counts": focus_mapping_counts},
                "unassigned_message_count": unassigned,
                "file_evidence_state": "recorded" if files else "not_collected",
                "projection_version": 1}

    @app.get("/v1/search")
    def search(q: str = Query(min_length=1, max_length=200), cursor: str | None = None,
               limit: int = Query(50, ge=1, le=200), _: str = Depends(require_owner)):
        q = q.strip()
        if not q:
            raise HTTPException(422, "Search term must not be empty")
        if len(q) == 1:
            limit = min(limit, 50)
        filters = {"q": q}
        sort, anchor = _page_cursor(cursor, filters)
        search_sort, search_anchor = (sort, anchor) if cursor else ("~", "~")
        pattern = "%" + q.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        with db.read() as conn:
            rows = conn.execute("""SELECT s.* FROM sessions s
                LEFT JOIN session_titles st ON st.session_id=s.id
                WHERE s.archived=0 AND (s.title LIKE ? ESCAPE '!' OR st.user_title LIKE ? ESCAPE '!'
                    OR s.cwd LIKE ? ESCAPE '!' OR EXISTS (SELECT 1 FROM messages m
                    WHERE m.session_id=s.id AND m.body LIKE ? ESCAPE '!'))
                AND (COALESCE(s.last_activity,'')<? OR (COALESCE(s.last_activity,'')=? AND s.id<?))
                ORDER BY COALESCE(s.last_activity,'') DESC,s.id DESC LIMIT ?""",
                (pattern, pattern, pattern, pattern, search_sort, search_sort, search_anchor, limit+1)).fetchall()
            items = []
            for row in rows[:limit]:
                item = _session_identity(conn, dict(row))
                match = conn.execute("""SELECT id,body,occurred_at FROM messages WHERE session_id=?
                    AND body LIKE ? ESCAPE '!' ORDER BY occurred_at DESC,id DESC LIMIT 1""", (item["id"], pattern)).fetchone()
                if match:
                    body = match["body"]
                    position = body.casefold().find(q.casefold())
                    start = max(0, position - 80)
                    item["excerpt"] = ("…" if start else "") + body[start:start + 240] + (
                        "…" if start + 240 < len(body) else "")
                    item["matched_message_id"] = match["id"]
                    item["match_type"] = "stored_message"
                    item["match_at"] = match["occurred_at"]
                else:
                    item["excerpt"] = item["cwd"] if item["cwd"] and q.casefold() in item["cwd"].casefold() else item["display_title"]
                    item["matched_message_id"] = None
                    item["match_type"] = "working_directory" if item["excerpt"] == item["cwd"] else "title"
                    item["match_at"] = item["last_activity"]
                item["session_id"] = item["id"]
                items.append(item)
        next_cursor = _cursor(items[-1]["last_activity"] or "", items[-1]["id"], filters) if len(rows) > limit else None
        return {"items": items, "next_cursor": next_cursor,
                "search_scope": "titles, working directories and authorized stored message bodies",
                "short_query_limit": 50 if len(q) == 1 else None}

    @app.get("/v1/files/{file_id}")
    def file_detail(file_id: str, _: str = Depends(require_owner)):
        with db.read() as conn:
            row = conn.execute("""SELECT f.*,e.occurred_at AS operation_at FROM file_events f
                LEFT JOIN events e ON e.event_id=f.event_id WHERE f.event_id=?""", (file_id,)).fetchone()
            if not row:
                raise HTTPException(404, "File fact not found")
            maps = conn.execute("SELECT * FROM path_maps WHERE root_id=?", (row["logical_root"],)).fetchall()
            run = conn.execute("SELECT id FROM runs WHERE session_id=? AND native_id=?", (row["session_id"], row["run_ref"])).fetchone()
            check = conn.execute("""SELECT status,created_at,result_json FROM file_check_jobs WHERE root_id=? AND relative_path=?
                ORDER BY created_at DESC LIMIT 1""", (row["logical_root"], row["relative_path"])).fetchone()
        item = _file_view(row, {row["run_ref"]: run[0]} if run else None)
        if check:
            item["detail_status"] = check["status"]
        return {"file": item, "maps": [dict(r) for r in maps],
                "current_access": {"state": check["status"], "checked_at": check["created_at"],
                                   "result": json.loads(check["result_json"]) if check["result_json"] else None}
                if check else {"state": "not_checked", "checked_at": None, "result": None},
                "size_note": "size_bytes is an observed value at capture time, not a live filesystem check"}

    @app.post("/v1/local/files/{file_id}/check")
    def local_file_check(file_id: str, request: Request, _: None = Depends(require_owner_write)):
        """Check the current file explicitly clicked by the local owner."""
        require_local_desktop(request)
        with db.read() as conn:
            row = conn.execute("""SELECT f.*,s.source_id,e.content_json,src.device_id
                FROM file_events f JOIN sessions s ON s.id=f.session_id
                JOIN sources src ON src.id=s.source_id
                JOIN events e ON e.event_id=f.event_id WHERE f.event_id=?""", (file_id,)).fetchone()
        if not row:
            raise HTTPException(404, "File fact not found")
        config_path = db.path.parent / "collector.json"
        if not config_path.is_file():
            raise HTTPException(403, "No local collector")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if (row["device_id"] != config.get("collector_id") or
                row["source_id"] not in {s.get("id") for s in config.get("sources", [])}):
            raise HTTPException(403, "File source is not on this device")
        payload = (json.loads(row["content_json"]).get("payload") or {})
        cwd = payload.get("cwd")
        if row["operation_status"] != "succeeded" or not cwd:
            raise HTTPException(422, "This file has no verified local working directory")
        root = Path(cwd)
        native_path = Path(row["native_path"])
        if not root.is_absolute() or not native_path.is_absolute():
            raise HTTPException(422, "File evidence must contain absolute local paths")
        try:
            relative = row["relative_path"] or native_path.resolve().relative_to(root.resolve()).as_posix()
            if (root / relative).resolve() != native_path.resolve():
                raise ValueError("File evidence path does not match working directory")
        except (OSError, ValueError) as exc:
            raise HTTPException(422, "File evidence path is not under its working directory") from exc
        result = check_local(root, relative, "stat")
        return {"file_id": file_id,
                "current_access": {"state": result["status"], "checked_at": result.get("checked_at"),
                                   "result": result},
                "historical_size_bytes": row["size_bytes"]}

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
            conn.execute("DELETE FROM session_titles WHERE session_id=?", (session_id,))
            conn.execute("UPDATE sessions SET title=NULL,cwd=NULL WHERE id=?", (session_id,))
            conn.execute("DELETE FROM file_events WHERE session_id=?", (session_id,))
            try:
                conn.execute("DELETE FROM messages_fts WHERE message_id IN (SELECT id FROM messages WHERE session_id=?)", (session_id,))
            except sqlite3.OperationalError:
                pass
            conn.execute("""UPDATE deliveries SET raw_json=NULL WHERE event_id IN
                (SELECT event_id FROM events WHERE source_id=? AND native_session_id=?
                 AND fact_kind IN ('session.observed','message.observed','file.observed'))""", (row["source_id"], row["native_id"]))
            # Revisions keep their own immutable event IDs. Remove every message
            # revision, not only the event currently referenced by the projection.
            conn.execute("""DELETE FROM events WHERE source_id=? AND native_session_id=?
                AND fact_kind IN ('session.observed','message.observed','file.observed')""", (row["source_id"], row["native_id"]))
            if not body.keep_statistics:
                conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
                conn.execute("DELETE FROM usage_observations WHERE session_id=?", (session_id,))
                conn.execute("DELETE FROM runs WHERE session_id=?", (session_id,))
                conn.execute("""UPDATE deliveries SET raw_json=NULL WHERE event_id IN
                    (SELECT event_id FROM events WHERE source_id=? AND native_session_id=?)""",
                             (row["source_id"], row["native_id"]))
                conn.execute("DELETE FROM events WHERE source_id=? AND native_session_id=?", (row["source_id"], row["native_id"]))
            archives = [Path(r[0]) for r in conn.execute("SELECT archive_path FROM handoffs WHERE source_session_id=?", (session_id,))]
            conn.execute("UPDATE handoffs SET revoked_at=? WHERE source_session_id=? AND revoked_at IS NULL", (now_iso(), session_id))
        purge_result = None
        try:
            config_path = db.path.parent / "collector.json"
            if config_path.is_file():
                config = json.loads(config_path.read_text(encoding="utf-8"))
                if row["source_id"] in {source.get("id") for source in config.get("sources", [])}:
                    purge_result = purge_deleted_content(db, row["source_id"], row["native_id"])
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise HTTPException(500, "Content was deleted, but local collector cleanup failed") from exc
        finally:
            for archive in archives:
                archive.unlink(missing_ok=True)
        return {"deleted": True, "statistics_retained": body.keep_statistics,
                "local_collector_cleanup": purge_result,
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
