"""Set up a same-machine collector without a pairing-code round trip."""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path
from uuid import uuid4

from .auth import issue_pair_code, now_iso, pair_device
from .collector import Outbox
from .db import Database


def discover_sources(home: Path | None = None, local_app_data: Path | None = None) -> list[tuple[str, Path]]:
    home = home or Path.home()
    candidates = [("codex", home / ".codex" / "sessions")]
    if local_app_data is None:
        local_app_data = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    candidates.append(("hermes", local_app_data / "hermes" / "state.db"))
    return [(agent, path.resolve()) for agent, path in candidates if path.exists()]


def prepare_local_collector(db: Database, config_path: Path, port: int,
                            candidates: list[tuple[str, Path]] | None = None) -> dict:
    """Reuse a saved local identity, adding newly discovered read-only sources."""
    server = f"http://127.0.0.1:{port}"
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if config.get("server") != server:
            raise ValueError(f"Collector is paired to {config.get('server')}; expected {server}")
        with db.read() as conn:
            row = conn.execute("SELECT token_hash,revoked_at FROM devices WHERE id=?",
                               (config.get("collector_id"),)).fetchone()
        from .auth import token_hash

        if row is None or row["revoked_at"] or row["token_hash"] != token_hash(config.get("token", "")):
            raise ValueError("Saved local collector identity is not valid for this database")
    else:
        code = issue_pair_code(db)
        device_id, token = pair_device(db, code, platform.node() or "This PC",
                                       platform.system(), platform.platform())
        config = {"server": server, "collector_id": device_id, "token": token, "sources": []}
    known = {(s["agent"], str(Path(s["root"]).resolve())) for s in config.get("sources", [])}
    for agent, root in candidates if candidates is not None else discover_sources():
        root = root.resolve()
        if (agent, str(root)) in known:
            continue
        source = {"id": str(uuid4()), "agent": agent, "profile": "default",
                  "root": str(root), "content_policy": "stats_only", "environment_id": None}
        with db.tx() as conn:
            existing = conn.execute("SELECT id FROM sources WHERE device_id=? AND agent=? AND profile=?",
                                    (config["collector_id"], agent, "default")).fetchone()
            if existing:
                source["id"] = existing["id"]
            else:
                conn.execute("""INSERT INTO sources(id,device_id,agent,profile,execution_surface,
                    capability_json,created_at) VALUES(?,?,?,'default','local',?,?)""",
                             (source["id"], config["collector_id"], agent,
                              json.dumps({"content_policy": "stats_only"}), now_iso()))
        config.setdefault("sources", []).append(source)
        known.add((agent, str(root)))
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temp = config_path.with_suffix(config_path.suffix + ".tmp")
    temp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if os.name != "nt":
        temp.chmod(0o600)
    temp.replace(config_path)
    return config


def set_local_source_policy(db: Database, source_id: str, policy: str) -> None:
    if policy not in {"stats_only", "full_content"}:
        raise ValueError("Unsupported local content policy")
    config_path = db.path.parent / "collector.json"
    if not config_path.is_file():
        raise ValueError("Local collector is not configured")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    source = next((item for item in config.get("sources", []) if item["id"] == source_id), None)
    if source is None:
        raise ValueError("Source is not managed by this local collector")
    source["content_policy"] = policy
    temp = config_path.with_suffix(config_path.suffix + ".tmp")
    temp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if os.name != "nt":
        temp.chmod(0o600)
    temp.replace(config_path)
    with db.tx() as conn:
        conn.execute("UPDATE sources SET capability_json=? WHERE id=? AND device_id=?",
                     (json.dumps({"content_policy": policy}), source_id, config["collector_id"]))


def _local_backfill_sources(db: Database, source_ids: list[str]) -> list[dict]:
    if not source_ids or len(source_ids) > 20 or len(source_ids) != len(set(source_ids)):
        raise ValueError("Select between 1 and 20 distinct local sources")
    config_path = db.path.parent / "collector.json"
    if not config_path.is_file():
        raise ValueError("Local collector is not configured")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    configured = {source["id"]: source for source in config.get("sources", [])}
    selected = []
    with db.read() as conn:
        for source_id in source_ids:
            source = configured.get(source_id)
            if source is None or source.get("content_policy") != "full_content":
                raise ValueError("Historical content requires a local source with full-content policy")
            row = conn.execute(
                "SELECT 1 FROM sources WHERE id=? AND device_id=? AND execution_surface='local'",
                (source_id, config["collector_id"]),
            ).fetchone()
            if row is None:
                raise ValueError("Selected source is not managed by this local collector")
            selected.append(source)
    return selected


def _blocked_backfill_sessions(db: Database, source_ids: list[str]) -> dict[str, list[str]]:
    """Mirror content-deletion tombstones before text enters the local outbox."""
    blocked: dict[str, list[str]] = {}
    with db.read() as conn:
        for source_id in source_ids:
            rows = conn.execute(
                "SELECT native_session_id FROM tombstones WHERE source_id=?",
                (source_id,),
            ).fetchall()
            blocked[source_id] = [row[0] for row in rows]
    return blocked


def preview_content_backfill(db: Database, source_ids: list[str], *, start_at: str | None = None,
                             end_at: str | None = None, native_session_ids: list[str] | None = None,
                             cwd_prefix: str | None = None) -> dict:
    """Count recoverable historical text in selected local sources without storing it."""
    selected = _local_backfill_sources(db, source_ids)
    return Outbox(db.path.parent / "outbox.db").preview_content_backfill(
        selected, start_at=start_at, end_at=end_at,
        native_session_ids=native_session_ids, cwd_prefix=cwd_prefix,
        blocked_native_session_ids=_blocked_backfill_sessions(db, source_ids),
    )


def request_content_backfill(db: Database, source_ids: list[str], *, start_at: str | None = None,
                             end_at: str | None = None, native_session_ids: list[str] | None = None,
                             cwd_prefix: str | None = None) -> dict:
    """Queue explicit, scoped historical content recovery for the local collector.

    Native stores are read by the collector, never by the web process. Changing
    a content policy alone does not backfill history or alter normal cursors.
    """
    selected = _local_backfill_sources(db, source_ids)
    return Outbox(db.path.parent / "outbox.db").request_content_backfill(
        selected, start_at=start_at, end_at=end_at,
        native_session_ids=native_session_ids, cwd_prefix=cwd_prefix,
        blocked_native_session_ids=_blocked_backfill_sessions(db, source_ids),
    )


def content_backfill_status(db: Database) -> list[dict]:
    """Return local collector progress without exposing native source paths."""
    return Outbox(db.path.parent / "outbox.db").content_backfill_status()


def purge_deleted_content(db: Database, source_id: str, native_session_id: str) -> dict:
    """Block a locally deleted session and scrub unsent collector payloads.

    Call only after the server tombstone has committed. The collector's append
    path checks this durable local block inside its write transaction.
    """
    return Outbox(db.path.parent / "outbox.db").block_session(source_id, native_session_id)
