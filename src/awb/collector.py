"""Read-only source adapters and durable local outbox.

The collector never modifies Codex or Hermes files. Its own SQLite file is
separate from both source stores. A scan commits facts and cursor advancement
in one transaction, then transport sends committed outbox rows.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

import httpx

from .codec import event_id, iso_utc, sha256, unicode_codepoints
from .filecheck import check_local
from .resources import sample_local

OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cursors(source_id TEXT NOT NULL, locator TEXT NOT NULL,
  offset INTEGER NOT NULL, fingerprint TEXT, session_id TEXT,
  PRIMARY KEY(source_id,locator));
CREATE TABLE IF NOT EXISTS outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE NOT NULL,
  source_id TEXT NOT NULL, event_json TEXT NOT NULL, observation_json TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending', receipt_id TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS deadletters(seq INTEGER PRIMARY KEY,event_id TEXT NOT NULL,
  event_json TEXT NOT NULL, reason TEXT NOT NULL, receipt_id TEXT NOT NULL,
  retained_at TEXT NOT NULL);
"""
MAX_BODY_CHARS = 16_000


def _now() -> str:
    return iso_utc(time.time()) or ""


def _redact(value: str) -> tuple[str, bool]:
    patterns = [
        r"(?i)(?:sk|ghp|gho|ghu|github_pat)_[A-Za-z0-9_-]{16,}",
        r"(?i)(?:api[_-]?key|token|password|secret)\s*[:=]\s*['\"]?[^\s'\";,]{8,}",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    ]
    changed = False
    for pattern in patterns:
        value, n = re.subn(pattern, "[REDACTED]", value)
        changed |= n > 0
    return value, changed


def make_event(source_id: str, native_session_id: str, kind: str, native_fact_id: str,
               revision: str, order: int | None, occurred_at: str | None,
               payload: dict[str, Any], device_id: str | None, environment_id: str | None,
               quality: dict[str, str | None] | None = None) -> dict:
    content = {
        "source_instance_id": source_id, "native_session_id": native_session_id,
        "fact_kind": kind, "native_fact_id": native_fact_id,
        "revision_key": revision, "source_order": order, "occurred_at": occurred_at,
        "supersedes_event_id": None,
        "quality": quality or {"certainty": "recorded", "rule_id": None, "reason": None},
        "execution": {"physical_device_id": device_id, "environment_id": environment_id, "basis": "source_metadata" if device_id else "unknown"},
        "payload": payload,
    }
    return {"schema_version": 1, "event_id": event_id(content), "body_hash": sha256(content), "content": content}


class Outbox:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with closing(self.connect()) as db:
            db.executescript(OUTBOX_SCHEMA)
            if not db.execute("SELECT 1 FROM meta WHERE key='epoch'").fetchone():
                db.execute("INSERT INTO meta VALUES('epoch',?)", (str(uuid4()),))

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def epoch(self) -> str:
        with closing(self.connect()) as db:
            return db.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()[0]

    def append(self, source_id: str, locator: str, offset: int, fingerprint: str,
               session_id: str | None, facts: Iterable[dict]) -> int:
        count = 0
        with closing(self.connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                for fact in facts:
                    if db.execute("SELECT 1 FROM outbox WHERE event_id=?", (fact["event_id"],)).fetchone():
                        continue
                    observation = {"observed_at": _now(), "adapter_version": "0.1.0",
                                   "source_locator": {"kind": "source_cursor", "value": sha256([source_id, locator])}}
                    cursor = db.execute("INSERT INTO outbox(event_id,source_id,event_json,observation_json) VALUES(?,?,?,?)",
                                        (fact["event_id"], source_id, json.dumps(fact, ensure_ascii=False), json.dumps(observation))).rowcount
                    count += cursor
                db.execute("""INSERT INTO cursors(source_id,locator,offset,fingerprint,session_id) VALUES(?,?,?,?,?)
                    ON CONFLICT(source_id,locator) DO UPDATE SET offset=excluded.offset,
                    fingerprint=excluded.fingerprint,session_id=excluded.session_id""",
                    (source_id, locator, offset, fingerprint, session_id))
                db.commit()
            except Exception:
                db.rollback()
                raise
        return count

    def cursor(self, source_id: str, locator: str) -> sqlite3.Row | None:
        with closing(self.connect()) as db:
            return db.execute("SELECT * FROM cursors WHERE source_id=? AND locator=?", (source_id, locator)).fetchone()

    def pending(self, limit: int = 100) -> list[dict]:
        with closing(self.connect()) as db:
            rows = db.execute("SELECT * FROM outbox WHERE state='pending' ORDER BY seq LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def count_pending(self) -> int:
        with closing(self.connect()) as db:
            return db.execute("SELECT COUNT(*) FROM outbox WHERE state='pending'").fetchone()[0]

    def settle(self, receipts: list[dict], durable_ack: int) -> None:
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            for r in receipts:
                if r["status"] in {"accepted", "duplicate", "ignored_tombstoned"} and r["seq"] <= durable_ack:
                    db.execute("UPDATE outbox SET state='acked',receipt_id=? WHERE seq=?", (r["receipt_id"], r["seq"]))
                elif r["status"] == "quarantined_pending":
                    row = db.execute("SELECT event_id,event_json FROM outbox WHERE seq=?", (r["seq"],)).fetchone()
                    if row:
                        db.execute("INSERT OR IGNORE INTO deadletters VALUES(?,?,?,?,?,?)",
                                   (r["seq"], row["event_id"], row["event_json"], r.get("reason") or "unknown", r["receipt_id"], _now()))
                        db.execute("UPDATE outbox SET state='quarantined',receipt_id=?,reason=? WHERE seq=?",
                                   (r["receipt_id"], r.get("reason"), r["seq"]))
            db.commit()


def _codex_text(item: dict) -> tuple[str | None, str | None]:
    role = item.get("role")
    if role not in {"user", "assistant", "system", "developer"}:
        return None, None
    chunks = []
    for content in item.get("content", []):
        if isinstance(content, dict) and content.get("type") in {"input_text", "output_text", "text"}:
            chunks.append(content.get("text", ""))
    return role, "\n".join(chunks) if chunks else None


def scan_codex(source: dict, outbox: Outbox, device_id: str) -> int:
    root = Path(source["root"]).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError("Codex session root unavailable")
    sid = source["id"]
    policy = source.get("content_policy", "stats_only")
    count = 0
    for file in sorted(root.rglob("*.jsonl")):
        if not file.is_file():
            continue
        locator = str(file.relative_to(root))
        stat = file.stat()
        fingerprint = f"{stat.st_dev}:{stat.st_ino}"
        cursor = outbox.cursor(sid, locator)
        offset = cursor["offset"] if cursor and cursor["fingerprint"] == fingerprint and stat.st_size >= cursor["offset"] else 0
        session_id = cursor["session_id"] if offset else None
        facts = []
        with file.open("rb") as stream:
            stream.seek(offset)
            while True:
                before = stream.tell()
                line = stream.readline()
                if not line or not line.endswith(b"\n"):
                    stream.seek(before)
                    break
                offset = stream.tell()
                try:
                    record = json.loads(line)
                except (UnicodeError, json.JSONDecodeError):
                    continue
                typ = record.get("type")
                payload = record.get("payload") or {}
                at = iso_utc(record.get("timestamp"))
                if typ == "session_meta":
                    session_id = payload.get("id") or session_id
                    if session_id:
                        facts.append(make_event(sid, session_id, "session.observed", "session", "1", before, at,
                                                {"title": None, "cwd": payload.get("cwd"), "created_at": at}, device_id, source.get("environment_id")))
                if not session_id or policy == "excluded":
                    continue
                if typ == "response_item" and payload.get("type") == "message":
                    role, body = _codex_text(payload)
                    if role is None or body is None:
                        continue
                    original_chars = unicode_codepoints(body)
                    if policy == "stats_only" or role not in {"user", "assistant"}:
                        state, reason, body = "stats_only", "source_policy", None
                    else:
                        body, changed = _redact(body)
                        if len(body) > MAX_BODY_CHARS:
                            body = body[:MAX_BODY_CHARS] + "\n[TRUNCATED BY COLLECTOR]"
                            changed = True
                        state, reason = ("redacted" if changed else "full"), None
                    facts.append(make_event(sid, session_id, "message.observed", str(before), "1", before, at,
                                            {"native_message_id": str(before), "role": role,
                                             "input_origin": "human" if role == "user" else "agent",
                                             "content_state": state, "omission_reason": reason, "body": body,
                                             "source_text_char_count": original_chars, "finalized": True},
                                            device_id, source.get("environment_id")))
                elif typ == "event_msg" and payload.get("type") in {"task_started", "task_complete"}:
                    status = "running" if payload["type"] == "task_started" else "completed"
                    turn_id = payload.get("turn_id")
                    if turn_id:
                        facts.append(make_event(sid, session_id, "run.observed", turn_id, status, before, at,
                                                {"native_turn_id": turn_id, "status": status,
                                                 "start_at": iso_utc(payload.get("started_at")) or (at if status == "running" else None),
                                                 "end_at": iso_utc(payload.get("completed_at")) if status == "completed" else None,
                                                 "duration_ms": payload.get("duration_ms"),
                                                 "duration_basis": "source" if payload.get("duration_ms") is not None else None,
                                                 "model_attribution": {"kind": "unknown"}},
                                                device_id, source.get("environment_id")))
                elif typ == "event_msg" and payload.get("type") == "token_count":
                    usage = payload.get("info") or {}
                    totals = usage.get("total_token_usage") or {}
                    total = totals.get("total_tokens")
                    if isinstance(total, int):
                        facts.append(make_event(sid, session_id, "usage.observed", f"counter:{before}", "1", before, at,
                                                {"usage_key": "codex_total", "quantity_semantics": "cumulative_snapshot",
                                                 "coverage_scope": "session", "counter_id": session_id, "epoch_id": session_id,
                                                 "source_time": at, "total_tokens": total}, device_id, source.get("environment_id")))
        count += outbox.append(sid, locator, offset, fingerprint, session_id, facts)
    return count


def scan_hermes(source: dict, outbox: Outbox, device_id: str) -> int:
    path = Path(source["root"]).expanduser().resolve()
    if path.is_dir():
        path = path / "state.db"
    if not path.is_file():
        raise FileNotFoundError("Hermes state database unavailable")
    sid = source["id"]
    policy = source.get("content_policy", "stats_only")
    count = 0
    uri = path.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as source_db:
        source_db.row_factory = sqlite3.Row
        sessions = source_db.execute("SELECT id,source,started_at,ended_at,cwd,model,title FROM sessions ORDER BY started_at,id").fetchall()
        for s in sessions:
            locator = "session:" + str(s["id"])
            cursor = outbox.cursor(sid, locator)
            order = int(cursor["offset"]) if cursor else 0
            facts = []
            at = iso_utc(s["started_at"])
            session_payload = {"title": s["title"], "cwd": s["cwd"], "created_at": at}
            facts.append(make_event(sid, s["id"], "session.observed", "session", sha256(session_payload), 0, at,
                                    session_payload, device_id, source.get("environment_id")))
            if policy != "excluded":
                messages = source_db.execute("SELECT id,role,content,timestamp FROM messages WHERE session_id=? AND id>? ORDER BY id", (s["id"], order)).fetchall()
                for m in messages:
                    order = int(m["id"])
                    body = m["content"] or ""
                    if not isinstance(body, str):
                        body = ""
                    original_chars = unicode_codepoints(body)
                    if policy == "stats_only" or m["role"] not in {"user", "assistant"}:
                        state, reason, body = "stats_only", "source_policy", None
                    else:
                        body, changed = _redact(body)
                        if len(body) > MAX_BODY_CHARS:
                            body = body[:MAX_BODY_CHARS] + "\n[TRUNCATED BY COLLECTOR]"
                            changed = True
                        state, reason = ("redacted" if changed else "full"), None
                    facts.append(make_event(sid, s["id"], "message.observed", str(m["id"]), "1", order,
                                            iso_utc(m["timestamp"]),
                                            {"native_message_id": str(m["id"]), "role": m["role"],
                                             "input_origin": "human" if m["role"] == "user" else "agent",
                                             "content_state": state, "omission_reason": reason, "body": body,
                                             "source_text_char_count": original_chars, "finalized": True},
                                            device_id, source.get("environment_id")))
                usage_rows = source_db.execute("""SELECT model,billing_provider,billing_base_url,billing_mode,task,
                    input_tokens,output_tokens,cache_read_tokens,reasoning_tokens
                    FROM session_model_usage WHERE session_id=?""", (s["id"],)).fetchall()
                for u in usage_rows:
                    identity = [u["model"], u["billing_provider"], u["billing_base_url"], u["billing_mode"], u["task"]]
                    totals = [u["input_tokens"], u["output_tokens"], u["cache_read_tokens"], u["reasoning_tokens"]]
                    usage_key = sha256(identity)
                    payload = {"usage_key": usage_key, "quantity_semantics": "aggregate",
                               "coverage_scope": "session_model", "model": u["model"],
                               "provider": u["billing_provider"], "input_tokens": u["input_tokens"],
                               "output_tokens": u["output_tokens"], "cached_input_tokens": u["cache_read_tokens"],
                               "reasoning_output_tokens": u["reasoning_tokens"],
                               "total_tokens": sum(totals[:2]) if all(x is not None for x in totals[:2]) else None}
                    facts.append(make_event(sid, s["id"], "usage.observed", usage_key,
                                            sha256(totals), None, None, payload, device_id,
                                            source.get("environment_id"),
                                            {"certainty": "recorded", "rule_id": "hermes_session_model_aggregate",
                                             "reason": "Session aggregate; no verified daily attribution"}))
            count += outbox.append(sid, locator, order, "hermes-db", s["id"], facts)
    return count


def send_pending(config: dict, outbox: Outbox) -> dict:
    pending = outbox.pending(100)
    if not pending:
        return {"sent": 0, "pending": 0}
    body = {"batch_id": str(uuid4()), "collector_id": config["collector_id"],
            "outbox_epoch": outbox.epoch(), "entries": []}
    for row in pending:
        body["entries"].append({"seq": row["seq"], "observation": json.loads(row["observation_json"]),
                                "event": json.loads(row["event_json"])})
    with httpx.Client(base_url=config["server"], timeout=30, verify=config.get("verify_tls", True)) as client:
        response = client.post("/v1/ingest/batches", headers={"Authorization": "Bearer " + config["token"]}, json=body)
        response.raise_for_status()
        result = response.json()
        outbox.settle(result["receipts"], result["durable_ack_seq"])
        for receipt in result["receipts"]:
            if receipt["status"] != "quarantined_pending":
                continue
            row = next(x for x in pending if x["seq"] == receipt["seq"])
            resolved = client.post(f"/v1/ingest/quarantines/{receipt['receipt_id']}/resolve",
                                   headers={"Authorization": "Bearer " + config["token"]},
                                   json={"body_hash": json.loads(row["event_json"])["body_hash"]})
            resolved.raise_for_status()
            outbox.settle([{"seq": receipt["seq"], "status": "quarantined_resolved",
                            "receipt_id": receipt["receipt_id"]}], resolved.json()["durable_ack_seq"])
    return {"sent": len(body["entries"]), "pending": outbox.count_pending(), "server_epoch": result["server_epoch"]}


def run_cycle(config: dict, outbox: Outbox) -> dict:
    added = 0
    errors = {}
    for source in config.get("sources", []):
        try:
            added += (scan_codex if source["agent"] == "codex" else scan_hermes)(source, outbox, config["collector_id"])
        except (OSError, sqlite3.DatabaseError, ValueError) as exc:
            errors[source["id"]] = type(exc).__name__
    sent = send_pending(config, outbox)
    with httpx.Client(base_url=config["server"], timeout=15, verify=config.get("verify_tls", True)) as client:
        headers = {"Authorization": "Bearer " + config["token"]}
        heartbeat = client.post("/v1/collectors/heartbeat", headers=headers,
                                json={"last_scan": _now(), "pending_count": outbox.count_pending(),
                                      "last_error": ",".join(errors.values()) or None,
                                      "sources": {s["id"]: {"last_scan": _now(), "last_error": errors.get(s["id"])} for s in config.get("sources", [])}})
        heartbeat.raise_for_status()
        jobs_response = client.get("/v1/collectors/jobs", headers=headers)
        jobs_response.raise_for_status()
        maps = {m["root_id"]: m for m in config.get("root_maps", [])}
        for job in jobs_response.json()["items"]:
            mapping = maps.get(job["root_id"])
            if not mapping or mapping.get("map_version") != job["map_version"]:
                result = {"status": "unmapped", "reason": "local_map_missing_or_version_mismatch", "checked_at": _now()}
            else:
                result = check_local(Path(mapping["native_root"]), job["relative_path"], job["operation"])
            delivered = client.post(f"/v1/collectors/jobs/{job['id']}/result", headers=headers, json=result)
            delivered.raise_for_status()
        if config.get("sample_resources"):
            sampled = client.post("/v1/collectors/process-samples", headers=headers, json=sample_local())
            sampled.raise_for_status()
    return {"added": added, "errors": errors, **sent}
