"""The focused Windows MVP usage ledger and dashboard query."""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

from .codec import day_bounds_utc_ms, iso_utc
from .codex_usage import codex_usage_files, scan_codex_requests
from .db import Database

_SYNC_LOCK = Lock()


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()


def sync_codex_usage(db: Database, root: Path | None = None) -> dict:
    """Reparse only changed rollouts; swap each file's ledger rows atomically."""
    root = root or codex_home()
    if not root.joinpath("sessions").is_dir():
        return {"status": "source_missing", "files": 0, "updated_files": 0, "deferred_files": 0}
    with _SYNC_LOCK:
        paths = codex_usage_files(root)
        with db.read() as conn:
            cursors = {row["source_file"]: row for row in conn.execute("SELECT * FROM mvp_usage_files")}
        changed = set()
        for path in paths:
            stat = path.stat()
            cursor = cursors.get(str(path))
            if (cursor is None or cursor["status"] != "ok" or cursor["size_bytes"] != stat.st_size
                    or cursor["modified_ns"] != stat.st_mtime_ns):
                changed.add(path)
        if not changed:
            return {"status": "ready", "files": len(paths), "updated_files": 0,
                    "deferred_files": sum(row["status"] != "ok" for row in cursors.values())}
        status: dict[Path, str] = {}
        requests, _ = scan_codex_requests(root, only_paths=changed, file_status=status)
        grouped: dict[str, list] = defaultdict(list)
        for item in requests:
            grouped[item.source_file].append(item)
        with db.tx() as conn:
            for path in changed:
                stat = path.stat()
                conn.execute("DELETE FROM mvp_usage_requests WHERE source_file=?", (str(path),))
                entries = grouped.get(str(path), [])
                conn.executemany("""INSERT OR IGNORE INTO mvp_usage_requests
                    (request_id,native_session_id,occurred_at,model,input_tokens,cached_input_tokens,
                     output_tokens,source_file,source_offset) VALUES(?,?,?,?,?,?,?,?,?)""",
                    [(item.request_id, item.native_session_id, iso_utc(item.occurred_at), item.model,
                      item.input_tokens, item.cached_input_tokens, item.output_tokens,
                      item.source_file, item.source_offset) for item in entries])
                conn.execute("""INSERT INTO mvp_usage_files
                    (source_file,size_bytes,modified_ns,scanned_at,record_count,status)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(source_file) DO UPDATE SET
                    size_bytes=excluded.size_bytes,modified_ns=excluded.modified_ns,
                    scanned_at=excluded.scanned_at,record_count=excluded.record_count,
                    status=excluded.status""",
                    (str(path), stat.st_size, stat.st_mtime_ns,
                     datetime.now(timezone.utc).isoformat(), len(entries), status.get(path, "error")))
        return {"status": "ready", "files": len(paths), "updated_files": len(changed),
                "deferred_files": sum(value != "ok" for value in status.values())}


def dashboard(db: Database, day: str, through: str, tz: str, *, model: str | None = None,
              sync: bool = True, root: Path | None = None) -> dict:
    start, _ = day_bounds_utc_ms(day, tz)
    _, end = day_bounds_utc_ms(through, tz)
    first, last = date.fromisoformat(day), date.fromisoformat(through)
    if last < first or end - start > 32 * 86_400_000:
        raise ValueError("Invalid date range")
    sync_state = sync_codex_usage(db, root) if sync else {"status": "not_synced"}
    lower = datetime.fromtimestamp(start / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
    upper = datetime.fromtimestamp(end / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
    params: list = [lower, upper]
    condition = "occurred_at>=? AND occurred_at<?"
    if model:
        condition += " AND model=?"
        params.append(model)
    with db.read() as conn:
        rows = conn.execute(f"SELECT * FROM mvp_usage_requests WHERE {condition} ORDER BY occurred_at", params).fetchall()
        sessions = {row["native_id"]: dict(row) for row in conn.execute(
            "SELECT id,native_id,title,cwd,last_activity FROM sessions WHERE agent='codex' AND archived=0")}
        files = [dict(row) for row in conn.execute("SELECT status,record_count FROM mvp_usage_files")]
    zone = ZoneInfo(tz)
    summary = {"requests": 0, "input_tokens": 0, "cached_input_tokens": 0,
               "fresh_input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    daily: dict[str, dict] = defaultdict(lambda: {"requests": 0, "input_tokens": 0,
            "cached_input_tokens": 0, "fresh_input_tokens": 0, "output_tokens": 0,
            "total_tokens": 0})
    models: dict[str, dict] = defaultdict(lambda: {"requests": 0, "input_tokens": 0,
            "cached_input_tokens": 0, "fresh_input_tokens": 0, "output_tokens": 0,
            "total_tokens": 0})
    per_session: dict[str, dict] = defaultdict(lambda: {"requests": 0, "total_tokens": 0,
                                                        "last_request": None})
    for row in rows:
        at = datetime.fromisoformat(row["occurred_at"].replace("Z", "+00:00"))
        key = at.astimezone(zone).date().isoformat()
        input_tokens = int(row["input_tokens"])
        cached = min(input_tokens, int(row["cached_input_tokens"]))
        values = {"requests": 1, "input_tokens": input_tokens, "cached_input_tokens": cached,
                  "fresh_input_tokens": input_tokens-cached,
                  "output_tokens": int(row["output_tokens"]),
                  "total_tokens": input_tokens+int(row["output_tokens"])}
        for bucket in (summary, daily[key], models[row["model"]]):
            for field, value in values.items():
                bucket[field] += value
        item = per_session[row["native_session_id"]]
        item["requests"] += 1
        item["total_tokens"] += values["total_tokens"]
        item["last_request"] = row["occurred_at"]
    cacheable = summary["input_tokens"]
    summary["cache_hit_rate"] = summary["cached_input_tokens"] / cacheable if cacheable else None
    result_sessions = []
    for native_id, metrics in sorted(per_session.items(), key=lambda entry:entry[1]["last_request"] or "", reverse=True):
        original = sessions.get(native_id) or {}
        result_sessions.append({"id": original.get("id"), "native_id": native_id,
                                "title": original.get("title") or f"Codex 会话 · {native_id[:8]}",
                                "last_request": metrics["last_request"],
                                "requests": metrics["requests"], "total_tokens": metrics["total_tokens"]})
    return {"status": sync_state["status"], "sync": sync_state,
            "filters": {"day": day, "through": through, "tz": tz, "model": model},
            "summary": summary,
            "daily": [{"day": (first+timedelta(days=i)).isoformat(), **daily[(first+timedelta(days=i)).isoformat()]}
                      for i in range((last-first).days+1)],
            "models": [{"model": name, **values} for name, values in sorted(models.items(),
                        key=lambda item: -item[1]["total_tokens"])],
            "sessions": result_sessions[:100], "session_count": len(result_sessions),
            "coverage": {"files_scanned": len(files),
                         "files_deferred": sum(row["status"] != "ok" for row in files),
                         "unlinked_sessions": sum(native_id not in sessions for native_id in per_session)},
            "source": "codex_jsonl_request_usage_v1",
            "note": "请求级 Codex 日志；输入包含缓存读取，处理总量=输入+输出。未知来源不并入，费用尚未计价。"}


def session_requests(db: Database, native_id: str, day: str, through: str, tz: str,
                     *, model: str | None = None, limit: int = 100) -> dict:
    start, _ = day_bounds_utc_ms(day, tz)
    _, end = day_bounds_utc_ms(through, tz)
    if end <= start or end-start > 32 * 86_400_000:
        raise ValueError("Invalid date range")
    lower = datetime.fromtimestamp(start/1000, timezone.utc).isoformat().replace("+00:00", "Z")
    upper = datetime.fromtimestamp(end/1000, timezone.utc).isoformat().replace("+00:00", "Z")
    condition = "native_session_id=? AND occurred_at>=? AND occurred_at<?"
    params: list = [native_id, lower, upper]
    if model:
        condition += " AND model=?"
        params.append(model)
    with db.read() as conn:
        count = conn.execute(f"SELECT COUNT(*) FROM mvp_usage_requests WHERE {condition}", params).fetchone()[0]
        rows = conn.execute(f"""SELECT request_id,occurred_at,model,input_tokens,cached_input_tokens,
            output_tokens FROM mvp_usage_requests WHERE {condition}
            ORDER BY occurred_at DESC LIMIT ?""", [*params, limit]).fetchall()
    return {"native_session_id": native_id, "count": count,
            "items": [{**dict(row), "fresh_input_tokens": row["input_tokens"]-row["cached_input_tokens"]}
                      for row in rows]}
