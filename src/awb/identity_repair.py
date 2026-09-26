"""Stage an offline, source-replay repair of legacy Codex session attribution.

No live database or outbox is mutated here. The caller stops the desktop service,
reviews the staged report, and swaps the two staged files together only after
validation. Hermes cursor state and user-owned metadata stay in the copies.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from .backup import create_backup
from .codec import event_id, sha256
from .collector import Outbox, codex_primary_session_meta, scan_codex
from .db import Database
from .ingest import receive_batch
from .models import Batch
from .projector import project


def _copy_database(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source)) as original, closing(sqlite3.connect(target)) as copy:
        original.backup(copy)


def _fingerprint(path: Path) -> dict:
    """Detect writes to either original file while an offline stage is built."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = path.stat()
    return {"size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": digest.hexdigest()}


def _original_fingerprints(db_path: Path, outbox_path: Path, config_path: Path) -> dict:
    paths = {"database": db_path, "outbox": outbox_path, "config": config_path,
             "database_wal": Path(str(db_path) + "-wal"),
             "database_shm": Path(str(db_path) + "-shm"),
             "outbox_wal": Path(str(outbox_path) + "-wal"),
             "outbox_shm": Path(str(outbox_path) + "-shm")}
    return {name: _fingerprint(path) if path.exists() else None for name, path in paths.items()}


def _assert_no_sqlite_sidecars(*paths: Path) -> None:
    stale = [str(Path(str(path) + suffix)) for path in paths for suffix in ("-wal", "-shm")
             if Path(str(path) + suffix).exists()]
    if stale:
        raise ValueError("SQLite WAL/SHM sidecars remain; stop all processes and checkpoint offline before staging/applying: "
                         + ", ".join(stale))


def _count(conn: sqlite3.Connection, table: str, source_ids: list[str]) -> int:
    marks = ",".join("?" for _ in source_ids)
    if table in {"messages", "runs", "usage_observations", "file_events"}:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE session_id IN "
                            f"(SELECT id FROM sessions WHERE source_id IN ({marks}))", source_ids).fetchone()[0]
    if table == "events":
        return conn.execute(f"SELECT COUNT(*) FROM events WHERE source_id IN ({marks})", source_ids).fetchone()[0]
    raise ValueError("Unsupported count table")


def _old_bodies(conn: sqlite3.Connection, source_ids: list[str]) -> list[dict]:
    marks = ",".join("?" for _ in source_ids)
    rows = conn.execute(f"""SELECT s.source_id,m.native_id,m.role,m.input_origin,m.source_order,
        m.occurred_at,m.source_char_count,m.body,m.content_state,m.omission_reason
        FROM messages m JOIN sessions s ON s.id=m.session_id
        WHERE s.source_id IN ({marks}) AND m.body IS NOT NULL""", source_ids).fetchall()
    return [dict(row) for row in rows]


def _message_signatures(conn: sqlite3.Connection, source_ids: list[str],
                        *, exclude_tombstoned: bool) -> set[tuple]:
    marks = ",".join("?" for _ in source_ids)
    exclusion = """AND NOT EXISTS (SELECT 1 FROM tombstones t WHERE t.source_id=s.source_id
        AND t.native_session_id=s.native_id)""" if exclude_tombstoned else ""
    rows = conn.execute(f"""SELECT s.source_id,m.native_id,m.role,m.input_origin,m.source_order,m.occurred_at
        FROM messages m JOIN sessions s ON s.id=m.session_id
        WHERE s.source_id IN ({marks}) {exclusion}""", source_ids).fetchall()
    return {tuple(row) for row in rows}


def _propagate_tombstones(conn: sqlite3.Connection, codex_sources: list[dict]) -> int:
    """A deleted polluted parent must not reappear as a newly separated child."""
    inherited = 0
    for source in codex_sources:
        source_id = source["id"]
        deleted = {row[0] for row in conn.execute(
            "SELECT native_session_id FROM tombstones WHERE source_id=?", (source_id,))}
        if not deleted:
            continue
        root = Path(source["root"]).expanduser().resolve()
        for file in sorted(root.rglob("*.jsonl")):
            primary = codex_primary_session_meta(file)
            if not primary or primary["id"] in deleted:
                continue
            found_deleted_parent = False
            with file.open("rb") as stream:
                for line in stream:
                    if b"session_meta" not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except (UnicodeError, json.JSONDecodeError):
                        continue
                    if record.get("type") == "session_meta" and (record.get("payload") or {}).get("id") in deleted:
                        found_deleted_parent = True
                        break
            if found_deleted_parent:
                conn.execute("""INSERT OR IGNORE INTO tombstones(source_id,native_session_id,deleted_at,policy_version)
                    VALUES(?,?,?,1)""", (source_id, primary["id"], datetime.now(timezone.utc).isoformat()))
                inherited += 1
    return inherited


def _clear_codex_projection(conn: sqlite3.Connection, source_ids: list[str]) -> None:
    marks = ",".join("?" for _ in source_ids)
    sessions = f"SELECT id FROM sessions WHERE source_id IN ({marks})"
    for table in ("file_events", "usage_observations", "messages", "runs"):
        conn.execute(f"DELETE FROM {table} WHERE session_id IN ({sessions})", source_ids)
    try:
        conn.execute("DELETE FROM messages_fts WHERE message_id NOT IN (SELECT id FROM messages)")
    except sqlite3.OperationalError:
        pass
    conn.execute(f"UPDATE sessions SET title=NULL,cwd=NULL,created_at=NULL,last_activity=NULL WHERE source_id IN ({marks})",
                 source_ids)
    conn.execute(f"UPDATE sources SET last_event=NULL WHERE id IN ({marks})", source_ids)
    conn.execute(f"DELETE FROM deliveries WHERE event_id IN (SELECT event_id FROM events WHERE source_id IN ({marks}))",
                 source_ids)
    conn.execute(f"DELETE FROM events WHERE source_id IN ({marks})", source_ids)


def _restore_existing_bodies(conn: sqlite3.Connection, old_bodies: list[dict]) -> dict:
    restored = ambiguous = unavailable = 0
    for old in old_bodies:
        candidates = conn.execute("""SELECT m.*,s.native_id AS native_session_id FROM messages m
            JOIN sessions s ON s.id=m.session_id WHERE s.source_id=? AND m.native_id=?
            AND m.role=? AND m.input_origin=? AND m.source_order IS ? AND m.occurred_at IS ?""",
            (old["source_id"], old["native_id"], old["role"], old["input_origin"],
             old["source_order"], old["occurred_at"])).fetchall()
        if old["source_char_count"] is not None:
            candidates = [row for row in candidates if row["source_char_count"] == old["source_char_count"]]
        if len(candidates) != 1:
            ambiguous += len(candidates) > 1
            unavailable += not candidates
            continue
        target = candidates[0]
        current_event = conn.execute("SELECT content_json FROM events WHERE event_id=?", (target["event_id"],)).fetchone()
        if not current_event:
            unavailable += 1
            continue
        content = json.loads(current_event[0])
        content["revision_key"] = f"identity_repair:{sha256([target['id'], old['body']])[:16]}"
        content["supersedes_event_id"] = target["event_id"]
        content["payload"] = {**content["payload"], "body": old["body"],
                              "content_state": old["content_state"],
                              "omission_reason": old["omission_reason"]}
        event = {"schema_version": 1, "event_id": event_id(content),
                 "body_hash": sha256(content), "content": content}
        conn.execute("""INSERT OR IGNORE INTO events(event_id,body_hash,source_id,native_session_id,fact_kind,
            native_fact_id,revision_key,source_order,occurred_at,content_json,received_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (event["event_id"], event["body_hash"], content["source_instance_id"],
             content["native_session_id"], content["fact_kind"], content["native_fact_id"],
             content["revision_key"], content.get("source_order"), content.get("occurred_at"),
             json.dumps(content, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
        project(conn, event)
        restored += 1
    return {"restored": restored, "ambiguous": ambiguous, "unavailable": unavailable,
            "old_readable": len(old_bodies)}


def _archive_orphan_sessions(conn: sqlite3.Connection, source_ids: list[str]) -> list[dict]:
    """Hide legacy identity rows that have no facts after the corrected replay."""
    marks = ",".join("?" for _ in source_ids)
    rows = conn.execute(f"""SELECT s.id,s.source_id,s.native_id FROM sessions s
        WHERE s.source_id IN ({marks}) AND s.archived=0
        AND NOT EXISTS (SELECT 1 FROM events e WHERE e.source_id=s.source_id
            AND e.native_session_id=s.native_id)
        ORDER BY s.source_id,s.native_id""", source_ids).fetchall()
    if rows:
        conn.executemany("UPDATE sessions SET archived=1 WHERE id=?", [(row["id"],) for row in rows])
    return [dict(row) for row in rows]


def stage_codex_identity_repair(db_path: Path, outbox_path: Path, config_path: Path,
                                stage_dir: Path, backup_dir: Path) -> dict:
    """Prepare verified replacement files while leaving the originals untouched.

    The service and collector must already be stopped. Any pending/quarantined
    outbox rows abort staging, so the existing stream's sequence remains valid.
    """
    db_path, outbox_path, config_path = Path(db_path), Path(outbox_path), Path(config_path)
    stage_dir, backup_dir = Path(stage_dir), Path(backup_dir)
    if stage_dir.exists() and any(stage_dir.iterdir()):
        raise FileExistsError("Stage directory must be empty")
    if backup_dir.exists() and any(backup_dir.iterdir()):
        raise FileExistsError("Backup directory must be empty")
    if not all(path.is_file() for path in (db_path, outbox_path, config_path)):
        raise FileNotFoundError("Database, outbox, and collector configuration are required")
    _assert_no_sqlite_sidecars(db_path, outbox_path)
    original_fingerprints = _original_fingerprints(db_path, outbox_path, config_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    codex_sources = [source for source in config.get("sources", []) if source.get("agent") == "codex"]
    if not codex_sources:
        raise ValueError("No local Codex source is configured")
    source_ids = [source["id"] for source in codex_sources]
    for source in codex_sources:
        root = Path(source["root"]).expanduser().resolve()
        if not root.is_dir() or not any(root.rglob("*.jsonl")):
            raise FileNotFoundError("A configured Codex source has no retained JSONL files")
    with closing(sqlite3.connect(outbox_path)) as old_outbox:
        old_outbox.row_factory = sqlite3.Row
        incomplete = old_outbox.execute("SELECT COUNT(*) FROM outbox WHERE state<>'acked'").fetchone()[0]
        running = old_outbox.execute("""SELECT COUNT(*) FROM content_backfills
            WHERE source_id IN (""" + ",".join("?" for _ in source_ids) + ") AND status IN ('queued','running')", source_ids).fetchone()[0]
        if incomplete or running:
            raise ValueError("Drain pending/quarantined outbox entries and content backfills before repair")
        epoch = old_outbox.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()[0]
        seq_row = old_outbox.execute("SELECT seq FROM sqlite_sequence WHERE name='outbox'").fetchone()
        high_water = seq_row[0] if seq_row else 0
    with closing(sqlite3.connect(db_path)) as original:
        original.row_factory = sqlite3.Row
        ack = original.execute("SELECT durable_ack,projected_ack FROM streams WHERE collector_id=? AND epoch=?",
                               (config["collector_id"], epoch)).fetchone()
        if not ack or ack["durable_ack"] != high_water or ack["projected_ack"] != high_water:
            raise ValueError("Collector sequence and server acknowledgements differ; sync before repair")
        old_counts = {table: _count(original, table, source_ids) for table in
                      ("messages", "runs", "usage_observations", "file_events", "events")}
        old_signatures = _message_signatures(original, source_ids, exclude_tombstoned=True)
        old_replay_eligible_messages = original.execute("""SELECT COUNT(*) FROM messages m
            JOIN sessions s ON s.id=m.session_id WHERE s.source_id IN (""" + ",".join("?" for _ in source_ids) + """ )
            AND NOT EXISTS (SELECT 1 FROM tombstones t WHERE t.source_id=s.source_id
                AND t.native_session_id=s.native_id)""", source_ids).fetchone()[0]
        old_bodies = _old_bodies(original, source_ids)
        hermes_counts = {table: original.execute(f"""SELECT COUNT(*) FROM {table} WHERE session_id IN
            (SELECT id FROM sessions WHERE source_id IN (SELECT id FROM sources WHERE agent='hermes'))""").fetchone()[0]
                         for table in ("messages", "runs", "usage_observations", "file_events")}
    stage_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)
    create_backup(db_path, backup_dir / "before-codex-identity-repair.zip")
    _copy_database(outbox_path, backup_dir / "outbox.db")
    staged_db = stage_dir / "agent-workbench.db"
    staged_outbox = stage_dir / "outbox.db"
    _copy_database(db_path, staged_db)
    _copy_database(outbox_path, staged_outbox)
    staged = Database(staged_db)
    staged.initialize()
    with staged.tx() as conn:
        inherited_tombstones = _propagate_tombstones(conn, codex_sources)
        _clear_codex_projection(conn, source_ids)
    with closing(sqlite3.connect(staged_outbox)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        marks = ",".join("?" for _ in source_ids)
        conn.execute(f"DELETE FROM deadletters WHERE seq IN (SELECT seq FROM outbox WHERE source_id IN ({marks}))", source_ids)
        conn.execute(f"DELETE FROM outbox WHERE source_id IN ({marks})", source_ids)
        conn.execute(f"DELETE FROM cursors WHERE source_id IN ({marks})", source_ids)
        conn.execute(f"DELETE FROM fact_revisions WHERE source_id IN ({marks})", source_ids)
        conn.commit()
    outbox = Outbox(staged_outbox)
    queued = 0
    for source in codex_sources:
        queued += scan_codex({**source, "content_policy": "stats_only"}, outbox, config["collector_id"])
    projected = 0
    while pending := outbox.pending(100):
        body = {"batch_id": str(uuid4()), "collector_id": config["collector_id"],
                "outbox_epoch": outbox.epoch(), "entries": [
                    {"seq": row["seq"], "observation": json.loads(row["observation_json"]),
                     "event": json.loads(row["event_json"])} for row in pending]}
        result = receive_batch(staged, config["collector_id"], Batch.model_validate(body))
        if any(item["status"] not in {"accepted", "duplicate", "ignored_tombstoned"} for item in result["receipts"]):
            raise ValueError("Staged replay produced quarantined events")
        outbox.settle(result["receipts"], result["durable_ack_seq"])
        projected += len(pending)
    with staged.tx() as conn:
        salvage = _restore_existing_bodies(conn, old_bodies)
        if salvage["ambiguous"] or salvage["unavailable"]:
            raise ValueError("Staged replay could not uniquely preserve every existing readable body")
        orphan_sessions = _archive_orphan_sessions(conn, source_ids)
    with staged.read() as conn:
        new_counts = {table: _count(conn, table, source_ids) for table in old_counts}
        new_signatures = _message_signatures(conn, source_ids, exclude_tombstoned=False)
        new_hermes = {table: conn.execute(f"""SELECT COUNT(*) FROM {table} WHERE session_id IN
            (SELECT id FROM sessions WHERE source_id IN (SELECT id FROM sources WHERE agent='hermes'))""").fetchone()[0]
                      for table in hermes_counts}
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        visible_orphans = conn.execute("""SELECT COUNT(*) FROM sessions s WHERE s.source_id IN ("""
            + ",".join("?" for _ in source_ids) + """ ) AND s.archived=0
            AND NOT EXISTS (SELECT 1 FROM events e WHERE e.source_id=s.source_id
                AND e.native_session_id=s.native_id)""", source_ids).fetchone()[0]
    if integrity != "ok" or foreign_keys:
        raise ValueError("Staged database failed integrity validation")
    if visible_orphans:
        raise ValueError("Staged database still has visible orphan Codex sessions")
    if new_hermes != hermes_counts:
        raise ValueError("Staged repair unexpectedly changed Hermes facts")
    if new_counts["messages"] < old_replay_eligible_messages:
        raise ValueError("Staged replay recovered fewer messages than the existing projection")
    missing_signatures = len(old_signatures - new_signatures)
    if missing_signatures:
        raise ValueError(f"Staged replay cannot account for {missing_signatures} existing message signatures")
    if outbox.count_pending():
        raise ValueError("Staged outbox still contains pending facts")
    if original_fingerprints != _original_fingerprints(db_path, outbox_path, config_path):
        raise ValueError("Original database, outbox, or config changed during staging; stop the application and retry")
    report = {"status": "staged_not_applied", "codex_sources": source_ids,
              "backup_database": str(backup_dir / "before-codex-identity-repair.zip"),
              "backup_outbox": str(backup_dir / "outbox.db"),
              "staged_database": str(staged_db), "staged_outbox": str(staged_outbox),
              "old_counts": old_counts, "new_counts": new_counts,
              "old_replay_eligible_messages": old_replay_eligible_messages,
              "old_message_signatures_verified": len(old_signatures),
              "preserved_hermes_counts": hermes_counts, "queued": queued, "projected": projected,
              "body_salvage": salvage, "inherited_tombstones": inherited_tombstones,
              "orphan_session_count": len(orphan_sessions), "orphan_sessions": orphan_sessions,
              "original_fingerprints": original_fingerprints,
              "privacy": "Historical replay used stats_only; original source policies were not changed.",
              "swap_requirement": "Stop server and collector, verify this report, then replace database and outbox together."}
    (stage_dir / "repair-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def apply_staged_codex_identity_repair(db_path: Path, outbox_path: Path, config_path: Path,
                                       stage_dir: Path, backup_dir: Path) -> dict:
    """Apply a verified offline stage, restoring original files if either swap fails."""
    db_path, outbox_path, config_path = Path(db_path), Path(outbox_path), Path(config_path)
    stage_dir, backup_dir = Path(stage_dir), Path(backup_dir)
    report = json.loads((stage_dir / "repair-report.json").read_text(encoding="utf-8"))
    if report.get("status") != "staged_not_applied":
        raise ValueError("A successful staged repair report is required")
    if (report["body_salvage"]["restored"] != report["body_salvage"]["old_readable"]
            or report["body_salvage"]["ambiguous"] or report["body_salvage"]["unavailable"]):
        raise ValueError("Staged repair did not preserve every readable body")
    staged_db, staged_outbox = stage_dir / "agent-workbench.db", stage_dir / "outbox.db"
    if Path(report["staged_database"]).resolve() != staged_db.resolve() or Path(
            report["staged_outbox"]).resolve() != staged_outbox.resolve():
        raise ValueError("Staged file paths do not match the report")
    if not staged_db.is_file() or not staged_outbox.is_file():
        raise FileNotFoundError("Both staged SQLite files are required")
    _assert_no_sqlite_sidecars(db_path, outbox_path, staged_db, staged_outbox)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    server_url = urlparse(config.get("server", ""))
    if server_url.hostname in {"127.0.0.1", "localhost", "::1"}:
        try:
            with socket.create_connection((server_url.hostname, server_url.port or 8765), timeout=1):
                pass
        except OSError:
            pass
        else:
            raise ValueError("Stop the local desktop service before applying the repair")
    if _original_fingerprints(db_path, outbox_path, config_path) != report["original_fingerprints"]:
        raise ValueError("Original files changed after staging; stage again from a stopped application")
    with closing(sqlite3.connect(staged_db)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or conn.execute(
                "PRAGMA foreign_key_check").fetchall():
            raise ValueError("Staged database integrity check failed")
    original_db_copy = backup_dir / "pre-apply-original.db"
    original_outbox_copy = backup_dir / "pre-apply-outbox.db"
    if original_db_copy.exists() or original_outbox_copy.exists():
        raise FileExistsError("Pre-apply rollback files already exist")
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db_path, original_db_copy)
    shutil.copy2(outbox_path, original_outbox_copy)
    applied = []
    try:
        for kind, staged, target in (("database", staged_db, db_path),
                                     ("outbox", staged_outbox, outbox_path)):
            os.replace(staged, target)
            applied.append(kind)
    except Exception:
        if "database" in applied:
            shutil.copy2(original_db_copy, db_path)
        if "outbox" in applied:
            shutil.copy2(original_outbox_copy, outbox_path)
        raise
    return {"status": "applied", "database_sha256": _fingerprint(db_path)["sha256"],
            "outbox_sha256": _fingerprint(outbox_path)["sha256"],
            "rollback_database": str(original_db_copy),
            "rollback_outbox": str(original_outbox_copy)}


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Stage or apply an offline Codex identity repair")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--outbox", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--stage-dir", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--offline-confirmed", action="store_true",
                        help="Confirm desktop server and collector have both been stopped")
    parser.add_argument("--apply", action="store_true",
                        help="Apply a previously reviewed stage with rollback copies")
    args = parser.parse_args()
    if not args.offline_confirmed:
        parser.error("Stop the desktop server and collector, then pass --offline-confirmed")
    if args.apply:
        result = apply_staged_codex_identity_repair(args.db, args.outbox, args.config,
                                                    args.stage_dir, args.backup_dir)
    else:
        result = stage_codex_identity_repair(args.db, args.outbox, args.config,
                                             args.stage_dir, args.backup_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
