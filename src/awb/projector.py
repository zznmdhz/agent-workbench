"""Project immutable accepted facts into rebuildable query tables."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .codec import iso_utc, sha256, utc_ms


def _session_id(source_id: str, native_session_id: str) -> str:
    return sha256([source_id, native_session_id])


def _newer(old: str | None, candidate: str | None) -> str | None:
    if candidate is None:
        return old
    if old is None or (utc_ms(candidate) or 0) > (utc_ms(old) or 0):
        return candidate
    return old


def _ensure_session(db: sqlite3.Connection, c: dict[str, Any], agent: str) -> str:
    source_id = c["source_instance_id"]
    session_id = _session_id(source_id, c["native_session_id"])
    db.execute(
        "INSERT OR IGNORE INTO sessions(id,source_id,native_id,agent,device_id) VALUES(?,?,?,?,?)",
        (session_id, source_id, c["native_session_id"], agent, c["execution"]["physical_device_id"]),
    )
    return session_id


def project(db: sqlite3.Connection, event: dict[str, Any]) -> None:
    c = event["content"]
    p = c["payload"]
    agent_row = db.execute("SELECT agent FROM sources WHERE id=?", (c["source_instance_id"],)).fetchone()
    if agent_row is None:
        raise ValueError("source not registered")
    session_id = _ensure_session(db, c, agent_row[0])
    at = iso_utc(c["occurred_at"])
    if c["fact_kind"] == "session.observed":
        old = db.execute("SELECT created_at,last_activity,title,cwd FROM sessions WHERE id=?", (session_id,)).fetchone()
        created_at = iso_utc(p.get("created_at"))
        db.execute(
            "UPDATE sessions SET title=?,cwd=?,created_at=?,last_activity=? WHERE id=?",
            (p.get("title") or old["title"], p.get("cwd") or old["cwd"],
             min(filter(None, (old["created_at"], created_at)), default=None),
             _newer(old["last_activity"], at or created_at), session_id),
        )
    elif c["fact_kind"] == "message.observed":
        msg_id = sha256([session_id, p["native_message_id"]])
        old = db.execute("SELECT source_order FROM messages WHERE id=?", (msg_id,)).fetchone()
        incoming = c.get("source_order")
        if old and incoming is not None and old[0] is not None and incoming < old[0]:
            return
        body = p.get("body") if p["content_state"] in {"full", "redacted"} else None
        db.execute(
            """INSERT INTO messages(id,session_id,native_id,role,input_origin,body,content_state,
                omission_reason,source_char_count,occurred_at,turn_id,event_id,source_order,finalized)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                role=excluded.role,input_origin=excluded.input_origin,body=excluded.body,
                content_state=excluded.content_state,omission_reason=excluded.omission_reason,
                source_char_count=excluded.source_char_count,occurred_at=excluded.occurred_at,
                turn_id=excluded.turn_id,event_id=excluded.event_id,source_order=excluded.source_order,
                finalized=excluded.finalized""",
            (msg_id, session_id, p["native_message_id"], p["role"], p["input_origin"], body,
             p["content_state"], p.get("omission_reason"), p.get("source_text_char_count"),
             at, p.get("native_turn_id"), event["event_id"], incoming, int(bool(p["finalized"]))),
        )
        try:
            db.execute("DELETE FROM messages_fts WHERE message_id=?", (msg_id,))
            if body:
                db.execute("INSERT INTO messages_fts(message_id,body) VALUES(?,?)", (msg_id, body))
        except sqlite3.OperationalError:
            pass
    elif c["fact_kind"] == "run.observed":
        rid = sha256([session_id, p["native_turn_id"]])
        old = db.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
        incoming = c.get("source_order")
        if old and incoming is not None and old["source_order"] is not None and incoming < old["source_order"]:
            return
        old_status = old["status"] if old else "unknown"
        terminal = {"completed", "failed", "cancelled"}
        status = p["status"]
        if old_status in terminal and status not in terminal:
            status = old_status
        attr = p.get("model_attribution") or {}
        model = attr.get("reported_model") or attr.get("configured_model")
        model_kind = attr.get("kind", "unknown")
        start = iso_utc(p.get("start_at")) or (old["start_at"] if old else None)
        end = iso_utc(p.get("end_at")) or (old["end_at"] if old else None)
        duration = p.get("duration_ms")
        if duration is None and old:
            duration = old["duration_ms"]
        db.execute(
            """INSERT INTO runs(id,session_id,native_id,device_id,model,model_attribution,status,
                start_at,end_at,duration_ms,duration_basis,parent_run_ref,event_id,source_order)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                device_id=excluded.device_id,model=excluded.model,model_attribution=excluded.model_attribution,
                status=excluded.status,start_at=excluded.start_at,end_at=excluded.end_at,
                duration_ms=excluded.duration_ms,duration_basis=excluded.duration_basis,
                parent_run_ref=excluded.parent_run_ref,event_id=excluded.event_id,
                source_order=excluded.source_order""",
            (rid, session_id, p["native_turn_id"], c["execution"]["physical_device_id"] or (old["device_id"] if old else None),
             model or (old["model"] if old else None), model_kind if model else (old["model_attribution"] if old else "unknown"),
             status, start, end, duration, p.get("duration_basis") or (old["duration_basis"] if old else None),
             p.get("parent_run_ref") or (old["parent_run_ref"] if old else None), event["event_id"], incoming),
        )
    elif c["fact_kind"] == "usage.observed":
        db.execute(
            """INSERT OR IGNORE INTO usage_observations(event_id,session_id,usage_key,semantics,
                coverage_scope,model,provider,counter_id,epoch_id,source_order,source_time,
                origin_time,verified_zero_origin,input_tokens,output_tokens,cached_input_tokens,
                reasoning_output_tokens,total_tokens) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event["event_id"], session_id, p["usage_key"], p["quantity_semantics"],
             p["coverage_scope"], p.get("model"), p.get("provider"), p.get("counter_id"),
             p.get("epoch_id"), c.get("source_order"), iso_utc(p.get("source_time")),
             iso_utc(p.get("origin_time")), int(bool(p.get("verified_zero_origin"))),
             p.get("input_tokens"), p.get("output_tokens"), p.get("cached_input_tokens"),
             p.get("reasoning_output_tokens"), p.get("total_tokens")),
        )
    elif c["fact_kind"] == "file.observed":
        db.execute(
            """INSERT OR IGNORE INTO file_events(event_id,session_id,native_path,logical_root,
                relative_path,relation,operation_status,run_ref,size_bytes,mtime,checked_at,evidence_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event["event_id"], session_id, p["native_path"], p.get("root_ref"),
             p.get("relative_path"), p["relation"], p.get("operation_status", "unknown"),
             p.get("run_ref"), p.get("size_bytes"), p.get("mtime"), p.get("checked_at"),
             json.dumps(p.get("evidence_refs", []), ensure_ascii=False)),
        )
    # gap.observed remains in immutable events and health summaries.
    old_activity = db.execute("SELECT last_activity FROM sessions WHERE id=?", (session_id,)).fetchone()[0]
    last_activity = _newer(old_activity, at)
    if last_activity != old_activity:
        db.execute("UPDATE sessions SET last_activity=? WHERE id=?", (last_activity, session_id))
    if at:
        old_source = db.execute("SELECT last_event FROM sources WHERE id=?", (c["source_instance_id"],)).fetchone()[0]
        if _newer(old_source, at) != old_source:
            db.execute("UPDATE sources SET last_event=? WHERE id=?", (at, c["source_instance_id"]))
