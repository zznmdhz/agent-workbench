"""Read-only source adapters and durable local outbox.

The collector never modifies Codex or Hermes files. Its own SQLite file is
separate from both source stores. A scan commits facts and cursor advancement
in one transaction, then transport sends committed outbox rows.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from threading import RLock
from typing import Any, Iterable
from uuid import uuid4

import httpx

from .codec import event_id, iso_utc, sha256, unicode_codepoints, utc_ms
from .filecheck import check_local, safe_relative
from .resources import sample_local

OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cursors(source_id TEXT NOT NULL, locator TEXT NOT NULL,
  offset INTEGER NOT NULL, fingerprint TEXT, session_id TEXT, turn_id TEXT,cwd TEXT,
  patch_calls_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(source_id,locator));
CREATE TABLE IF NOT EXISTS outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE NOT NULL,
  source_id TEXT NOT NULL, event_json TEXT NOT NULL, observation_json TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending', receipt_id TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS deadletters(seq INTEGER PRIMARY KEY,event_id TEXT NOT NULL,
  event_json TEXT NOT NULL, reason TEXT NOT NULL, receipt_id TEXT NOT NULL,
  retained_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fact_revisions(
  source_id TEXT NOT NULL,native_session_id TEXT NOT NULL,fact_kind TEXT NOT NULL,
  native_fact_id TEXT NOT NULL,revision_seq INTEGER NOT NULL,
  semantic_hash TEXT NOT NULL,event_id TEXT NOT NULL,
  PRIMARY KEY(source_id,native_session_id,fact_kind,native_fact_id));
CREATE TABLE IF NOT EXISTS content_backfills(
  source_id TEXT PRIMARY KEY,agent TEXT NOT NULL,requested_at TEXT NOT NULL,
  status TEXT NOT NULL,units_total INTEGER NOT NULL DEFAULT 0,
  units_done INTEGER NOT NULL DEFAULT 0,facts_queued INTEGER NOT NULL DEFAULT 0,
  finished_at TEXT,error TEXT,scope_json TEXT NOT NULL DEFAULT '{}',last_locator TEXT);
CREATE TABLE IF NOT EXISTS blocked_sessions(
  source_id TEXT NOT NULL,native_session_id TEXT NOT NULL,blocked_at TEXT NOT NULL,
  PRIMARY KEY(source_id,native_session_id));
"""
MAX_BODY_CHARS = 16_000
_OUTBOX_TRANSPORT_LOCK = RLock()


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


def _backfill_scope(start_at: str | None, end_at: str | None,
                    native_session_ids: list[str] | None, cwd_prefix: str | None) -> dict:
    """Dates bound message time; cwd is the full, source-reported working directory."""
    start = iso_utc(start_at) if start_at else None
    end = iso_utc(end_at) if end_at else None
    if (start_at and not start) or (end_at and not end):
        raise ValueError("Backfill dates must include a timezone")
    if start and end and utc_ms(start) >= utc_ms(end):
        raise ValueError("Backfill end must be after start")
    if cwd_prefix and not Path(cwd_prefix).is_absolute():
        raise ValueError("Backfill working directory must be an absolute path")
    ids = sorted(set(native_session_ids or []))
    if any(not isinstance(value, str) or not value for value in ids):
        raise ValueError("Native session IDs must be nonempty strings")
    return {"start_at": start, "end_at": end,
            "native_session_ids": ids, "cwd_prefix": cwd_prefix}


def _in_backfill_scope(scope: dict, session_id: str | None, cwd: str | None,
                       at: str | None) -> bool:
    if not session_id or not at:
        return False
    ids = scope.get("native_session_ids") or []
    if ids and session_id not in ids:
        return False
    if session_id in (scope.get("blocked_native_session_ids") or []):
        return False
    prefix = scope.get("cwd_prefix")
    if prefix:
        if not cwd:
            return False
        native = cwd.replace("\\", "/").rstrip("/").casefold()
        wanted = prefix.replace("\\", "/").rstrip("/").casefold()
        if native != wanted and not native.startswith(wanted + "/"):
            return False
    ms = utc_ms(at)
    if ms is None:
        return False
    start = scope.get("start_at")
    end = scope.get("end_at")
    return not ((start and ms < utc_ms(start)) or (end and ms >= utc_ms(end)))


def _patch_intent_paths(patch_input: str) -> list[tuple[str, str]]:
    result = []
    for line in patch_input.splitlines():
        match = re.fullmatch(r"\*\*\* (Add|Update) File: (.+)", line.strip())
        if match:
            result.append(("created" if match[1] == "Add" else "modified", match[2]))
    return result


def _successful_patch_files(intended_paths: list[tuple[str, str]], tool_output: str) -> list[tuple[str, str]]:
    """Trust only successful apply_patch results paired with matching patch intent."""
    if not re.match(r"\AExit code: 0\s*(?:\r?\n)", tool_output):
        return []
    if "Success. Updated the following files:" not in tool_output:
        return []
    intended = {(relation, path.replace("\\", "/").casefold())
                for relation, path in intended_paths}
    result = []
    success_section = tool_output.split("Success. Updated the following files:", 1)[1]
    for line in success_section.splitlines():
        match = re.fullmatch(r"([AM]) (.+)", line.strip())
        if not match:
            continue
        relation = "created" if match[1] == "A" else "modified"
        path = match[2]
        if (relation, path.replace("\\", "/").casefold()) in intended:
            result.append((relation, path))
    return result


def _source_cwd_file_ref(source_id: str, cwd: str | None, native_path: str) -> tuple[str | None, str | None]:
    """Map only a lexically contained patch path; do not inspect today's file."""
    if not cwd or not os.path.isabs(cwd) or not os.path.isabs(native_path):
        return None, None
    root = os.path.normpath(cwd)
    target = os.path.normpath(native_path)
    try:
        if os.path.normcase(os.path.commonpath((root, target))) != os.path.normcase(root):
            return None, None
        relative = os.path.relpath(target, root).replace("\\", "/")
        if relative == ".":
            return None, None
        relative = safe_relative(relative)
    except (OSError, ValueError):
        return None, None
    return "cwd:" + sha256([source_id, os.path.normcase(root)]), relative


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
            if "turn_id" not in {row[1] for row in db.execute("PRAGMA table_info(cursors)")}:
                db.execute("ALTER TABLE cursors ADD COLUMN turn_id TEXT")
            if "cwd" not in {row[1] for row in db.execute("PRAGMA table_info(cursors)")}:
                db.execute("ALTER TABLE cursors ADD COLUMN cwd TEXT")
            if "patch_calls_json" not in {row[1] for row in db.execute("PRAGMA table_info(cursors)")}:
                db.execute("ALTER TABLE cursors ADD COLUMN patch_calls_json TEXT NOT NULL DEFAULT '{}' ")
            if "scope_json" not in {row[1] for row in db.execute("PRAGMA table_info(content_backfills)")}:
                db.execute("ALTER TABLE content_backfills ADD COLUMN scope_json TEXT NOT NULL DEFAULT '{}' ")
            if "last_locator" not in {row[1] for row in db.execute("PRAGMA table_info(content_backfills)")}:
                db.execute("ALTER TABLE content_backfills ADD COLUMN last_locator TEXT")
            if not db.execute("SELECT 1 FROM meta WHERE key='epoch'").fetchone():
                db.execute("INSERT INTO meta VALUES('epoch',?)", (str(uuid4()),))
            self._compact_acked(db)
            db.commit()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.execute("PRAGMA secure_delete=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def epoch(self) -> str:
        with closing(self.connect()) as db:
            return db.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()[0]

    def has_marker(self, key: str) -> bool:
        with closing(self.connect()) as db:
            return db.execute("SELECT 1 FROM meta WHERE key=?", (key,)).fetchone() is not None

    def mark_once(self, key: str) -> None:
        with closing(self.connect()) as db:
            db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)", (key, _now()))
            db.commit()

    @staticmethod
    def _semantic_hash(fact: dict) -> str:
        content = dict(fact["content"])
        content.pop("revision_key", None)
        content.pop("supersedes_event_id", None)
        payload = dict(content["payload"])
        payload.pop("content_revision", None)
        content["payload"] = payload
        return sha256(content)

    @staticmethod
    def _revise(fact: dict, revision_seq: int, previous_event_id: str) -> dict:
        content = dict(fact["content"])
        content["revision_key"] = f"collector:{revision_seq}"
        content["supersedes_event_id"] = previous_event_id
        content["payload"] = {**content["payload"], "content_revision": revision_seq}
        return {"schema_version": 1, "event_id": event_id(content),
                "body_hash": sha256(content), "content": content}

    def _compact_acked(self, db: sqlite3.Connection) -> None:
        """Keep dedupe identities but remove acknowledged local event bodies.

        Old outboxes predate the revision registry. Register their latest
        message fingerprint before erasing the acknowledged payload.
        """
        rows = db.execute("""SELECT seq,event_id,event_json FROM outbox
            WHERE state='acked' AND event_json<>'{}' ORDER BY seq""").fetchall()
        for row in rows:
            try:
                fact = json.loads(row["event_json"])
                c = fact["content"]
            except (ValueError, KeyError, TypeError):
                continue
            if c.get("fact_kind") == "message.observed":
                key = (c["source_instance_id"], c["native_session_id"],
                       c["fact_kind"], c["native_fact_id"])
                revision_key = c.get("revision_key", "1")
                suffix = revision_key.split(":", 1)[1] if revision_key.startswith("collector:") else ""
                revision_seq = int(suffix) if suffix.isdigit() else 1
                previous = db.execute("""SELECT revision_seq FROM fact_revisions WHERE
                    source_id=? AND native_session_id=? AND fact_kind=? AND native_fact_id=?""", key).fetchone()
                if previous is None or revision_seq >= previous[0]:
                    db.execute("""INSERT INTO fact_revisions VALUES(?,?,?,?,?,?,?)
                        ON CONFLICT(source_id,native_session_id,fact_kind,native_fact_id)
                        DO UPDATE SET revision_seq=excluded.revision_seq,
                        semantic_hash=excluded.semantic_hash,event_id=excluded.event_id""",
                               (*key, revision_seq, self._semantic_hash(fact), fact["event_id"]))
        db.execute("UPDATE outbox SET event_json='{}',observation_json='{}' WHERE state='acked'")

    def _versioned_message(self, db: sqlite3.Connection, fact: dict) -> dict | None:
        """Register immutable message revisions in the same transaction as the outbox row.

        Existing v0.2 outboxes have no revision registry. Their original event is
        used as the baseline on the first re-scan, so a newly allowed body never
        reuses an old event ID with different bytes.
        """
        c = fact["content"]
        key = (c["source_instance_id"], c["native_session_id"],
               c["fact_kind"], c["native_fact_id"])
        incoming_hash = self._semantic_hash(fact)
        previous = db.execute("""SELECT revision_seq,semantic_hash,event_id FROM fact_revisions
            WHERE source_id=? AND native_session_id=? AND fact_kind=? AND native_fact_id=?""", key).fetchone()
        if previous is None:
            original = db.execute("SELECT event_json FROM outbox WHERE event_id=?", (fact["event_id"],)).fetchone()
            if original:
                old_fact = json.loads(original["event_json"])
                previous = (1, self._semantic_hash(old_fact) if "content" in old_fact else "",
                            fact["event_id"])
                db.execute("""INSERT INTO fact_revisions VALUES(?,?,?,?,?,?,?)""",
                           (*key, *previous))
        if previous:
            if previous[1] == incoming_hash:
                return None
            revision_seq = int(previous[0]) + 1
            fact = self._revise(fact, revision_seq, previous[2])
            db.execute("""UPDATE fact_revisions SET revision_seq=?,semantic_hash=?,event_id=?
                WHERE source_id=? AND native_session_id=? AND fact_kind=? AND native_fact_id=?""",
                       (revision_seq, incoming_hash, fact["event_id"], *key))
        else:
            db.execute("INSERT INTO fact_revisions VALUES(?,?,?,?,?,?,?)",
                       (*key, 1, incoming_hash, fact["event_id"]))
        return fact

    def append(self, source_id: str, locator: str, offset: int, fingerprint: str,
               session_id: str | None, facts: Iterable[dict], turn_id: str | None = None,
               advance_cursor: bool = True, patch_calls: dict | None = None,
               cwd: str | None = None) -> int:
        count = 0
        with closing(self.connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                for fact in facts:
                    native_session_id = fact["content"]["native_session_id"]
                    if db.execute("SELECT 1 FROM blocked_sessions WHERE source_id=? AND native_session_id=?",
                                  (source_id, native_session_id)).fetchone():
                        continue
                    if fact["content"]["fact_kind"] == "message.observed":
                        fact = self._versioned_message(db, fact)
                        if fact is None:
                            continue
                    if db.execute("SELECT 1 FROM outbox WHERE event_id=?", (fact["event_id"],)).fetchone():
                        continue
                    observation = {"observed_at": _now(), "adapter_version": "0.1.0",
                                   "source_locator": {"kind": "source_cursor", "value": sha256([source_id, locator])}}
                    cursor = db.execute("INSERT INTO outbox(event_id,source_id,event_json,observation_json) VALUES(?,?,?,?)",
                                        (fact["event_id"], source_id, json.dumps(fact, ensure_ascii=False), json.dumps(observation))).rowcount
                    count += cursor
                if advance_cursor:
                    db.execute("""INSERT INTO cursors(source_id,locator,offset,fingerprint,session_id,turn_id,cwd,patch_calls_json)
                        VALUES(?,?,?,?,?,?,?,?)
                        ON CONFLICT(source_id,locator) DO UPDATE SET offset=excluded.offset,
                        fingerprint=excluded.fingerprint,session_id=excluded.session_id,
                        turn_id=excluded.turn_id,cwd=excluded.cwd,patch_calls_json=excluded.patch_calls_json""",
                        (source_id, locator, offset, fingerprint, session_id, turn_id,cwd,
                         json.dumps(patch_calls or {}, ensure_ascii=False)))
                db.commit()
            except Exception:
                db.rollback()
                raise
        return count

    def is_blocked(self, source_id: str, native_session_id: str) -> bool:
        with closing(self.connect()) as db:
            return db.execute("SELECT 1 FROM blocked_sessions WHERE source_id=? AND native_session_id=?",
                              (source_id, native_session_id)).fetchone() is not None

    def block_session(self, source_id: str, native_session_id: str) -> dict:
        """Prevent new local facts and scrub unsent facts after owner deletion.

        Sequence numbers stay in place so the durable stream can still advance.
        A queued row becomes a harmless tombstoned session observation; the
        server-side tombstone ignores it. Quarantined rows remain quarantined
        because their old sequence may already be recorded by the server.
        """
        rewritten = quarantined = 0
        with _OUTBOX_TRANSPORT_LOCK, closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO blocked_sessions VALUES(?,?,?)",
                       (source_id, native_session_id, _now()))
            rows = db.execute("SELECT seq,event_json,state FROM outbox WHERE source_id=? AND state<>'acked'",
                              (source_id,)).fetchall()
            for row in rows:
                try:
                    prior = json.loads(row["event_json"])
                    content = prior["content"]
                except (ValueError, KeyError, TypeError):
                    continue
                if content.get("native_session_id") != native_session_id:
                    continue
                execution = content.get("execution") or {}
                safe = make_event(source_id, native_session_id, "session.observed",
                                  f"owner_deleted:{row['seq']}", "1", None, _now(),
                                  {"title": None, "cwd": None},
                                  execution.get("physical_device_id"),
                                  execution.get("environment_id"))
                db.execute("""UPDATE outbox SET event_id=?,event_json=?,reason='owner_deleted'
                    WHERE seq=?""", (safe["event_id"], json.dumps(safe, ensure_ascii=False), row["seq"]))
                db.execute("DELETE FROM deadletters WHERE seq=?", (row["seq"],))
                if row["state"] == "quarantined":
                    quarantined += 1
                else:
                    rewritten += 1
            db.commit()
        return {"rewritten_pending": rewritten, "quarantined_needs_repair": quarantined}

    def cursor(self, source_id: str, locator: str) -> sqlite3.Row | None:
        with closing(self.connect()) as db:
            return db.execute("SELECT * FROM cursors WHERE source_id=? AND locator=?", (source_id, locator)).fetchone()

    def backfill_codex_token_components_once(self, source_ids: list[str]) -> None:
        """Retain the old marker API without rewinding live message cursors.

        The targeted metadata migration in run_cycle now handles old counters
        and patch evidence without replaying private message bodies.
        """
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            if source_ids:
                db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('codex_token_components_v1',?)", (_now(),))
            db.commit()

    def preview_content_backfill(self, sources: list[dict], *, start_at: str | None = None,
                                 end_at: str | None = None,
                                 native_session_ids: list[str] | None = None,
                                 cwd_prefix: str | None = None,
                                 blocked_native_session_ids: dict[str, list[str]] | None = None) -> dict:
        scope = _backfill_scope(start_at, end_at, native_session_ids, cwd_prefix)
        blocked_native_session_ids = blocked_native_session_ids or {}
        if not sources:
            raise ValueError("Select at least one source")
        if len({source["id"] for source in sources}) != len(sources):
            raise ValueError("Duplicate source selection")
        items = []
        for source in sources:
            source_scope = {**scope, "blocked_native_session_ids": sorted(set(
                blocked_native_session_ids.get(source["id"], [])))}
            if source.get("content_policy") != "full_content":
                raise ValueError("Historical text requires full_content policy for every selected source")
            agent = source.get("agent")
            if agent == "codex":
                root = Path(source["root"]).expanduser().resolve()
                if not root.is_dir():
                    raise FileNotFoundError("Codex session root unavailable")
                units = 0
                eligible = 0
                for file in sorted(root.rglob("*.jsonl")):
                    primary = codex_primary_session_meta(file)
                    if primary is None:
                        continue
                    units += 1
                    native_id = primary["id"]
                    cwd = primary["cwd"]
                    with file.open("rb") as stream:
                        for line in stream:
                            if not line.endswith(b"\n"):
                                break
                            try:
                                record = json.loads(line)
                            except (UnicodeError, json.JSONDecodeError):
                                continue
                            payload = record.get("payload") or {}
                            if record.get("type") == "response_item" and payload.get("type") == "message":
                                if payload.get("role") not in {"user", "assistant"} or not _in_backfill_scope(
                                        source_scope, native_id, cwd, iso_utc(record.get("timestamp"))):
                                    continue
                                role, body = _codex_text(payload)
                                if role in {"user", "assistant"} and body is not None:
                                    eligible += 1
            elif agent == "hermes":
                path = Path(source["root"]).expanduser().resolve()
                if path.is_dir():
                    path /= "state.db"
                if not path.is_file():
                    raise FileNotFoundError("Hermes state database unavailable")
                units = eligible = 0
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
                    db.row_factory = sqlite3.Row
                    for session in db.execute("SELECT id,cwd FROM sessions ORDER BY id"):
                        units += 1
                        for message in db.execute("SELECT role,timestamp FROM messages WHERE session_id=?", (session["id"],)):
                            if message["role"] in {"user", "assistant"} and _in_backfill_scope(
                                    source_scope, session["id"], session["cwd"], iso_utc(message["timestamp"])):
                                eligible += 1
            else:
                raise ValueError("Unsupported source agent")
            items.append({"source_id": source["id"], "agent": agent,
                          "units": units, "eligible_messages": eligible,
                          "blocked_sessions": len(source_scope["blocked_native_session_ids"])})
        return {"scope": scope, "items": items,
                "eligible_messages": sum(item["eligible_messages"] for item in items),
                "warning": "Only selected native source records are read; retained source logs are never changed. "
                           "Previously deleted sessions remain blocked by server tombstones."}

    def request_content_backfill(self, sources: list[dict], *, start_at: str | None = None,
                                 end_at: str | None = None,
                                 native_session_ids: list[str] | None = None,
                                 cwd_prefix: str | None = None,
                                 blocked_native_session_ids: dict[str, list[str]] | None = None) -> dict:
        preview = self.preview_content_backfill(
            sources, start_at=start_at, end_at=end_at,
            native_session_ids=native_session_ids, cwd_prefix=cwd_prefix,
            blocked_native_session_ids=blocked_native_session_ids)
        queued = []
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            for item in preview["items"]:
                existing = db.execute("SELECT status FROM content_backfills WHERE source_id=?",
                                      (item["source_id"],)).fetchone()
                if existing and existing["status"] in {"queued", "running"}:
                    raise ValueError("A historical content backfill is already running for a selected source")
                source_scope = {**preview["scope"], "blocked_native_session_ids": sorted(set(
                    (blocked_native_session_ids or {}).get(item["source_id"], [])))}
                db.execute("""INSERT INTO content_backfills(source_id,agent,requested_at,status,
                    units_total,scope_json) VALUES(?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET
                    agent=excluded.agent,requested_at=excluded.requested_at,status='queued',
                    units_total=excluded.units_total,units_done=0,facts_queued=0,
                    finished_at=NULL,error=NULL,scope_json=excluded.scope_json,last_locator=NULL""",
                           (item["source_id"], item["agent"], _now(), "queued", item["units"],
                            json.dumps(source_scope, ensure_ascii=False)))
                queued.append(item["source_id"])
            db.commit()
        return {**preview, "queued_source_ids": queued}

    def content_backfill_status(self) -> list[dict]:
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute("""SELECT source_id,agent,requested_at,status,
                units_total,units_done,facts_queued,finished_at,error,scope_json
                FROM content_backfills ORDER BY requested_at DESC""")]

    def pending_content_backfills(self) -> list[dict]:
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute("""SELECT * FROM content_backfills
                WHERE status IN ('queued','running') ORDER BY requested_at""")]

    def advance_content_backfill(self, source_id: str, locator: str, added: int) -> None:
        with closing(self.connect()) as db:
            db.execute("""UPDATE content_backfills SET status='running',last_locator=?,
                units_done=units_done+1,units_total=MAX(units_total,units_done+1),
                facts_queued=facts_queued+? WHERE source_id=?
                AND status IN ('queued','running')""", (locator, added, source_id))
            db.commit()

    def finish_content_backfill(self, source_id: str, error: str | None = None) -> None:
        with closing(self.connect()) as db:
            db.execute("""UPDATE content_backfills SET status=?,finished_at=?,error=?
                WHERE source_id=? AND status IN ('queued','running')""",
                       ("failed" if error else "completed", _now(), error, source_id))
            db.commit()

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
                if ((r["status"] in {"accepted", "duplicate", "ignored_tombstoned"}
                     and r["seq"] <= durable_ack) or r["status"] == "quarantined_resolved"):
                    db.execute("UPDATE outbox SET state='acked',receipt_id=? WHERE seq=?", (r["receipt_id"], r["seq"]))
                    db.execute("DELETE FROM deadletters WHERE seq=?", (r["seq"],))
                elif r["status"] == "quarantined_pending":
                    row = db.execute("SELECT event_id,event_json FROM outbox WHERE seq=?", (r["seq"],)).fetchone()
                    if row:
                        db.execute("INSERT OR IGNORE INTO deadletters VALUES(?,?,?,?,?,?)",
                                   (r["seq"], row["event_id"], row["event_json"], r.get("reason") or "unknown", r["receipt_id"], _now()))
                        db.execute("UPDATE outbox SET state='quarantined',receipt_id=?,reason=? WHERE seq=?",
                                   (r["receipt_id"], r.get("reason"), r["seq"]))
            self._compact_acked(db)
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


def codex_primary_session_meta(file: Path) -> dict | None:
    """A JSONL file belongs to its first native session, even with inherited meta.

    Spawned Codex logs can embed a later parent `session_meta`. Treating that
    record as an identity switch incorrectly credits child work to the parent.
    """
    with file.open("rb") as stream:
        while True:
            before = stream.tell()
            line = stream.readline()
            if not line or not line.endswith(b"\n"):
                return None
            if b"session_meta" not in line:
                continue
            try:
                record = json.loads(line)
            except (UnicodeError, json.JSONDecodeError):
                continue
            if record.get("type") != "session_meta":
                continue
            payload = record.get("payload") or {}
            native_id = payload.get("id")
            if isinstance(native_id, str) and native_id:
                return {"id": native_id, "cwd": payload.get("cwd"),
                        "offset": before, "created_at": iso_utc(record.get("timestamp"))}


def scan_codex(source: dict, outbox: Outbox, device_id: str, *,
               backfill_scope: dict | None = None, after_locator: str | None = None,
               max_units: int | None = None, progress: dict | None = None,
               migration_only: bool = False) -> int:
    root = Path(source["root"]).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError("Codex session root unavailable")
    sid = source["id"]
    policy = source.get("content_policy", "stats_only")
    count = 0
    backfill = backfill_scope is not None or migration_only
    files = [file for file in sorted(root.rglob("*.jsonl")) if file.is_file()
             and (after_locator is None or str(file.relative_to(root)) > after_locator)]
    if progress is not None:
        progress["has_more"] = max_units is not None and len(files) > max_units
        progress["units"] = min(len(files), max_units) if max_units is not None else len(files)
    if max_units is not None:
        files = files[:max_units]
    for file in files:
        if not file.is_file():
            continue
        primary = codex_primary_session_meta(file)
        if primary is None:
            continue
        locator = str(file.relative_to(root))
        stat = file.stat()
        fingerprint = f"{stat.st_dev}:{stat.st_ino}"
        cursor = None if backfill else outbox.cursor(sid, locator)
        offset = cursor["offset"] if cursor and cursor["fingerprint"] == fingerprint and stat.st_size >= cursor["offset"] else 0
        session_id = primary["id"]
        if outbox.is_blocked(sid, session_id):
            if backfill_scope is not None:
                outbox.advance_content_backfill(sid, locator, 0)
            continue
        previous_identity_matches = bool(offset and cursor["session_id"] == session_id)
        turn_id = cursor["turn_id"] if previous_identity_matches else None
        patch_calls = json.loads(cursor["patch_calls_json"]) if previous_identity_matches else {}
        cwd = primary["cwd"]
        counter_id = f"{session_id}:{sha256([sid, locator])}"
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
                    if before == primary["offset"] and not backfill:
                        facts.append(make_event(sid, session_id, "session.observed", "session", "1", before, at,
                                                {"title": None, "cwd": cwd, "created_at": at}, device_id, source.get("environment_id")))
                if not session_id or policy == "excluded":
                    continue
                if typ == "event_msg" and payload.get("type") == "task_started":
                    turn_id = payload.get("turn_id") or None
                elif typ == "event_msg" and payload.get("type") == "task_complete":
                    completed_turn_id = payload.get("turn_id")
                else:
                    completed_turn_id = None
                if typ == "response_item" and payload.get("type") == "message":
                    if migration_only:
                        continue
                    if backfill_scope is not None and (payload.get("role") not in {"user", "assistant"}
                                                       or not _in_backfill_scope(
                                                           backfill_scope, session_id, cwd, at)):
                        continue
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
                                             "source_text_char_count": original_chars, "finalized": True,
                                             "native_turn_id": turn_id},
                                            device_id, source.get("environment_id")))
                elif typ == "response_item" and payload.get("type") == "custom_tool_call" and payload.get("name") == "apply_patch":
                    call_id = payload.get("call_id")
                    patch_input = payload.get("input")
                    paths = _patch_intent_paths(patch_input) if isinstance(patch_input, str) else []
                    if isinstance(call_id, str) and paths:
                        patch_calls[call_id] = {"paths": paths, "session_id": session_id,
                                                "cwd": cwd, "turn_id": turn_id}
                        if len(patch_calls) > 100:
                            patch_calls.pop(next(iter(patch_calls)))
                elif typ == "response_item" and payload.get("type") == "custom_tool_call_output":
                    call_id = payload.get("call_id")
                    patch_call = patch_calls.pop(call_id, None) if isinstance(call_id, str) else None
                    output = payload.get("output")
                    if patch_call and isinstance(output, str) and (backfill_scope is None or _in_backfill_scope(
                            backfill_scope, patch_call["session_id"], patch_call["cwd"], at)):
                        for relation, native_path in _successful_patch_files(patch_call["paths"], output):
                            path = Path(native_path)
                            if not path.is_absolute():
                                if not patch_call["cwd"]:
                                    continue
                                path = Path(patch_call["cwd"]) / path
                            # Preserve the source-reported path rather than consulting today's
                            # filesystem (which might have moved since the patch completed).
                            resolved = os.path.normpath(str(path))
                            root_ref, relative_path = _source_cwd_file_ref(
                                sid, patch_call["cwd"], resolved)
                            native_file_id = f"apply_patch:{call_id}:{sha256([relation, resolved])}"
                            facts.append(make_event(sid, patch_call["session_id"], "file.observed",
                                                    native_file_id, "1", before, at,
                                                    {"native_file_event_id": native_file_id,
                                                     "native_path": resolved, "cwd": patch_call["cwd"],
                                                     "root_ref": root_ref, "relative_path": relative_path,
                                                     "relation": relation, "operation_status": "succeeded",
                                                     "run_ref": patch_call["turn_id"],
                                                     "evidence_refs": [{"kind": "codex_apply_patch",
                                                                        "call_id": call_id,
                                                                        "source_order": before}]},
                                                    device_id, source.get("environment_id"),
                                                    {"certainty": "derived",
                                                     "rule_id": "codex_apply_patch_success_v1",
                                                     "reason": "Matched a successful apply_patch result to its call"}))
                elif not backfill and typ == "event_msg" and payload.get("type") in {"task_started", "task_complete"}:
                    status = "running" if payload["type"] == "task_started" else "completed"
                    native_turn_id = payload.get("turn_id")
                    if native_turn_id:
                        facts.append(make_event(sid, session_id, "run.observed", native_turn_id, status, before, at,
                                                {"native_turn_id": native_turn_id, "status": status,
                                                 "start_at": iso_utc(payload.get("started_at")) or (at if status == "running" else None),
                                                 "end_at": (iso_utc(payload.get("completed_at")) or at) if status == "completed" else None,
                                                 "duration_ms": payload.get("duration_ms"),
                                                 "duration_basis": "source" if payload.get("duration_ms") is not None else None,
                                                 "model_attribution": {"kind": "unknown"}},
                                                device_id, source.get("environment_id")))
                elif (not backfill or migration_only) and typ == "event_msg" and payload.get("type") == "token_count":
                    usage = payload.get("info") or {}
                    totals = usage.get("total_token_usage") or {}
                    total = totals.get("total_tokens")
                    if isinstance(total, int):
                        facts.append(make_event(sid, session_id, "usage.observed", f"counter:{before}", "1", before, at,
                                                {"usage_key": "codex_total", "quantity_semantics": "cumulative_snapshot",
                                                 "coverage_scope": "session", "counter_id": counter_id, "epoch_id": counter_id,
                                                 "source_time": at, "total_tokens": total}, device_id, source.get("environment_id")))
                    components = (("input_tokens", "codex_input"),
                                  ("output_tokens", "codex_output"),
                                  ("cached_input_tokens", "codex_cached_input"))
                    for source_field, usage_key in components:
                        component = totals.get(source_field)
                        if isinstance(component, int) and component >= 0:
                            facts.append(make_event(sid, session_id, "usage.observed", f"{usage_key}:{before}", "1", before, at,
                                                    {"usage_key": usage_key, "quantity_semantics": "cumulative_snapshot",
                                                     "coverage_scope": "session", "counter_id": counter_id, "epoch_id": counter_id,
                                                     "source_time": at, "total_tokens": component},
                                                    device_id, source.get("environment_id")))
                if completed_turn_id and completed_turn_id == turn_id:
                    turn_id = None
        added = outbox.append(sid, locator, offset, fingerprint, session_id, facts,
                              turn_id=turn_id, advance_cursor=not backfill,
                              patch_calls=patch_calls, cwd=cwd)
        count += added
        if backfill_scope is not None:
            outbox.advance_content_backfill(sid, locator, added)
    return count


def scan_hermes(source: dict, outbox: Outbox, device_id: str, *,
                backfill_scope: dict | None = None, after_locator: str | None = None,
                max_units: int | None = None, progress: dict | None = None) -> int:
    path = Path(source["root"]).expanduser().resolve()
    if path.is_dir():
        path = path / "state.db"
    if not path.is_file():
        raise FileNotFoundError("Hermes state database unavailable")
    sid = source["id"]
    policy = source.get("content_policy", "stats_only")
    backfill = backfill_scope is not None
    count = 0
    uri = path.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as source_db:
        source_db.row_factory = sqlite3.Row
        sessions = source_db.execute("SELECT id,source,started_at,ended_at,cwd,model,title FROM sessions ORDER BY started_at,id").fetchall()
        if backfill:
            sessions = sorted((s for s in sessions if after_locator is None or "session:" + str(s["id"]) > after_locator),
                              key=lambda s: "session:" + str(s["id"]))
        if progress is not None:
            progress["has_more"] = max_units is not None and len(sessions) > max_units
            progress["units"] = min(len(sessions), max_units) if max_units is not None else len(sessions)
        if max_units is not None:
            sessions = sessions[:max_units]
        for s in sessions:
            locator = "session:" + str(s["id"])
            if outbox.is_blocked(sid, s["id"]):
                if backfill:
                    outbox.advance_content_backfill(sid, locator, 0)
                continue
            cursor = None if backfill else outbox.cursor(sid, locator)
            order = int(cursor["offset"]) if cursor else 0
            facts = []
            at = iso_utc(s["started_at"])
            session_payload = {"title": s["title"], "cwd": s["cwd"], "created_at": at}
            if not backfill:
                facts.append(make_event(sid, s["id"], "session.observed", "session", sha256(session_payload), 0, at,
                                        session_payload, device_id, source.get("environment_id")))
            if policy != "excluded":
                messages = source_db.execute("SELECT id,role,content,timestamp FROM messages WHERE session_id=? AND id>? ORDER BY id",
                                             (s["id"], order)).fetchall()
                for m in messages:
                    order = int(m["id"])
                    occurred_at = iso_utc(m["timestamp"])
                    if backfill and (m["role"] not in {"user", "assistant"} or not _in_backfill_scope(
                            backfill_scope, s["id"], s["cwd"], occurred_at)):
                        continue
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
                                            occurred_at,
                                            {"native_message_id": str(m["id"]), "role": m["role"],
                                             "input_origin": "human" if m["role"] == "user" else "agent",
                                             "content_state": state, "omission_reason": reason, "body": body,
                                             "source_text_char_count": original_chars, "finalized": True},
                                            device_id, source.get("environment_id")))
                usage_rows = [] if backfill else source_db.execute("""SELECT model,billing_provider,billing_base_url,billing_mode,task,
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
            added = outbox.append(sid, locator, order, "hermes-db", s["id"], facts,
                                  advance_cursor=not backfill)
            count += added
            if backfill:
                outbox.advance_content_backfill(sid, locator, added)
    return count


def process_content_backfill(config: dict, outbox: Outbox, max_units: int = 4) -> dict:
    """Advance explicitly requested historical text recovery without moving live cursors."""
    sources = {source["id"]: source for source in config.get("sources", [])}
    queued = 0
    processed = 0
    errors = {}
    for job in outbox.pending_content_backfills():
        if processed >= max_units:
            break
        sid = job["source_id"]
        source = sources.get(sid)
        if not source or source.get("content_policy") != "full_content":
            outbox.finish_content_backfill(sid, "policy_or_source_changed")
            errors[sid] = "policy_or_source_changed"
            continue
        if source.get("agent") != job["agent"]:
            outbox.finish_content_backfill(sid, "agent_changed")
            errors[sid] = "agent_changed"
            continue
        try:
            progress = {}
            scope = json.loads(job["scope_json"])
            scan = scan_codex if job["agent"] == "codex" else scan_hermes
            queued += scan(source, outbox, config["collector_id"], backfill_scope=scope,
                           after_locator=job["last_locator"],
                           max_units=max_units - processed, progress=progress)
            processed += progress["units"]
            if not progress["has_more"]:
                outbox.finish_content_backfill(sid)
        except (OSError, sqlite3.DatabaseError, ValueError) as exc:
            outbox.finish_content_backfill(sid, type(exc).__name__)
            errors[sid] = type(exc).__name__
    return {"queued": queued, "units_processed": processed, "errors": errors}


def backfill_codex_cached_input_history_once(source: dict, outbox: Outbox,
                                             device_id: str) -> int:
    """Recover new counter/file fact types from old logs; never rewind live cursors.

    This is a metadata parser migration, not an opt-in text backfill. It emits
    only cached counters and paired successful patch operations; messages never
    enter the queue. An interrupted pass retries safely from the beginning.
    """
    sid = source["id"]
    marker = f"codex_metadata_history_v2:{sid}"
    if outbox.has_marker(marker) or source.get("content_policy") == "excluded":
        return 0
    added = scan_codex(source, outbox, device_id, migration_only=True)
    outbox.mark_once(marker)
    return added


def send_pending(config: dict, outbox: Outbox) -> dict:
    # Deletion must not rewrite a sequence while it is being transmitted.
    with _OUTBOX_TRANSPORT_LOCK:
        return _send_pending_locked(config, outbox)


def _send_pending_locked(config: dict, outbox: Outbox) -> dict:
    candidates = outbox.pending(500)
    pending = []
    byte_budget = 3 * 1024 * 1024
    used_bytes = 0
    for row in candidates:
        row_bytes = len(row["event_json"].encode("utf-8")) + len(row["observation_json"].encode("utf-8")) + 256
        if pending and used_bytes + row_bytes > byte_budget:
            break
        pending.append(row)
        used_bytes += row_bytes
    if not pending:
        return {"sent": 0, "pending": 0}
    body = {"batch_id": str(uuid4()), "collector_id": config["collector_id"],
            "outbox_epoch": outbox.epoch(), "entries": []}
    for row in pending:
        body["entries"].append({"seq": row["seq"], "observation": json.loads(row["observation_json"]),
                                "event": json.loads(row["event_json"])})
    with httpx.Client(base_url=config["server"], timeout=120, verify=config.get("verify_tls", True)) as client:
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
            if source["agent"] == "codex":
                added += backfill_codex_cached_input_history_once(
                    source, outbox, config["collector_id"])
            added += (scan_codex if source["agent"] == "codex" else scan_hermes)(source, outbox, config["collector_id"])
        except (OSError, sqlite3.DatabaseError, ValueError) as exc:
            errors[source["id"]] = type(exc).__name__
    backfill = process_content_backfill(config, outbox, max_units=8)
    errors.update(backfill["errors"])
    added += backfill["queued"]
    sent = {"sent": 0, "pending": outbox.count_pending()}
    deadline = time.monotonic() + 25
    while sent["pending"] and time.monotonic() < deadline:
        batch = send_pending(config, outbox)
        sent["sent"] += batch["sent"]
        sent["pending"] = batch["pending"]
        if "server_epoch" in batch:
            sent["server_epoch"] = batch["server_epoch"]
        if batch["sent"] == 0:
            break
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
    return {"added": added, "errors": errors, "backfill": backfill, **sent}
