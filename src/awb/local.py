"""Set up a same-machine collector without a pairing-code round trip."""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path
from uuid import uuid4

from .auth import issue_pair_code, now_iso, pair_device
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
