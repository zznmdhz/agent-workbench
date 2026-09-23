"""Opt-in process snapshots; never infer per-session resource ownership."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psutil

from .auth import now_iso
from .db import Database


def sample_local() -> list[dict]:
    samples = []
    for process in psutil.process_iter(["pid", "name", "create_time"]):
        try:
            name = (process.info["name"] or "").lower()
            if not any(label in name for label in ("codex", "hermes")):
                continue
            samples.append({"pid": process.pid, "create_time_ms": int(process.info["create_time"] * 1000),
                            "rss_bytes": process.memory_info().rss,
                            "cpu_percent": str(process.cpu_percent(interval=0.05)),
                            "process_name": name[:100]})
        except (psutil.AccessDenied, psutil.NoSuchProcess, TypeError):
            continue
    return samples


def record_samples(db: Database, device_id: str, samples: list[dict]) -> int:
    if len(samples) > 200:
        raise ValueError("Too many process samples")
    at = now_iso()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat().replace("+00:00", "Z")
    with db.tx() as conn:
        environment = conn.execute("SELECT environment FROM devices WHERE id=?", (device_id,)).fetchone()[0]
        for s in samples:
            conn.execute("""INSERT INTO process_samples(device_id,environment_id,pid,create_time_ms,
                sampled_at,rss_bytes,cpu_percent,process_name) VALUES(?,?,?,?,?,?,?,?)""",
                (device_id, environment, s["pid"], s["create_time_ms"], at,
                 s.get("rss_bytes"), s.get("cpu_percent"), s.get("process_name")))
        conn.execute("DELETE FROM process_samples WHERE sampled_at<?", (cutoff,))
    return len(samples)
