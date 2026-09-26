"""Comparable Codex, Claude and Hermes usage with explicit source precision."""

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

from .claude_usage import claude_usage_files, scan_claude_requests
from .codec import day_bounds_utc_ms
from .db import Database
from .mvp_usage import sync_codex_usage

_CLAUDE_LOCK = Lock()
AGENTS = {"codex", "claude", "hermes"}
CLAUDE_PARSER_VERSION = "claude-message-v3"
FIELDS = ("requests", "input_tokens", "fresh_input_tokens", "cached_input_tokens",
          "cache_creation_tokens", "output_tokens", "total_tokens")


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser()


def hermes_db_path() -> Path:
    return Path(os.environ.get("HERMES_STATE_DB", Path.home() / "AppData/Local/hermes/state.db")).expanduser()


def _empty() -> dict:
    return {key: 0 for key in FIELDS}


def _add(bucket: dict, values: dict) -> None:
    for field in FIELDS:
        bucket[field] += values[field]


def _values(fresh: int, read: int, write: int, output: int, calls: int = 1) -> dict:
    fresh, read, write, output = (max(0, int(value or 0)) for value in (fresh, read, write, output))
    total_input = fresh + read + write
    return {"requests": max(0, int(calls or 0)), "fresh_input_tokens": fresh,
            "cached_input_tokens": read, "cache_creation_tokens": write,
            "input_tokens": total_input, "output_tokens": output,
            "total_tokens": total_input + output}


def _range(day: str, through: str, tz: str) -> tuple[date, date, datetime, datetime, ZoneInfo]:
    first, last = date.fromisoformat(day), date.fromisoformat(through)
    if last < first or (last - first).days > 3660:
        raise ValueError("Date range must be ascending and at most ten years")
    start_ms, _ = day_bounds_utc_ms(day, tz)
    _, end_ms = day_bounds_utc_ms(through, tz)
    return (first, last, datetime.fromtimestamp(start_ms / 1000, timezone.utc),
            datetime.fromtimestamp(end_ms / 1000, timezone.utc), ZoneInfo(tz))


def sync_claude_usage(db: Database, root: Path | None = None) -> dict:
    root = root or claude_home()
    if not (root / "projects").is_dir():
        return {"status": "source_missing", "files": 0, "updated_files": 0, "deferred_files": 0}
    with _CLAUDE_LOCK:
        paths = claude_usage_files(root)
        snapshot = {}
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            snapshot[str(path)] = (stat.st_size, stat.st_mtime_ns)
        with db.read() as conn:
            prior = {row["source_file"]: (row["size_bytes"], row["modified_ns"], row["status"], row["parser_version"])
                     for row in conn.execute("SELECT * FROM mvp_claude_files")}
        if snapshot.keys() == prior.keys() and all(
                prior[path] == (*values, "ok", CLAUDE_PARSER_VERSION) for path, values in snapshot.items()):
            return {"status": "ready", "files": len(snapshot), "updated_files": 0, "deferred_files": 0}
        requests, status = scan_claude_requests([Path(path) for path in snapshot])
        now = datetime.now(timezone.utc).isoformat()
        with db.tx() as conn:
            conn.execute("DELETE FROM mvp_claude_requests")
            conn.execute("DELETE FROM mvp_claude_files")
            conn.executemany("""INSERT INTO mvp_claude_requests
                (request_id,native_session_id,occurred_at,model,input_tokens,cached_input_tokens,
                 cache_creation_tokens,output_tokens,cache_read_known,cache_creation_known,
                 source_file,source_offset,project)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(r.request_id, r.native_session_id, r.occurred_at, r.model, r.input_tokens,
                  r.cached_input_tokens, r.cache_creation_tokens, r.output_tokens,
                  int(r.cache_read_known), int(r.cache_creation_known),
                  r.source_file, r.source_offset, r.project) for r in requests])
            conn.executemany("""INSERT INTO mvp_claude_files
                (source_file,size_bytes,modified_ns,scanned_at,status,parser_version) VALUES(?,?,?,?,?,?)""",
                [(path, size, modified, now, status.get(path, "read_error"), CLAUDE_PARSER_VERSION)
                 for path, (size, modified) in snapshot.items()])
        return {"status": "ready", "files": len(snapshot), "updated_files": len(snapshot),
                "deferred_files": sum(value != "ok" for value in status.values())}


def _hermes_rows(path: Path) -> tuple[list[dict], dict]:
    if not path.is_file():
        return [], {"status": "source_missing", "path": str(path)}
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(row) for row in conn.execute("""SELECT u.session_id,u.model,u.task,u.api_call_count,
                u.input_tokens,u.output_tokens,u.cache_read_tokens,u.cache_write_tokens,
                u.first_seen,u.last_seen,s.title,s.cwd
                FROM session_model_usage u LEFT JOIN sessions s ON s.id=u.session_id""")]
    except (OSError, sqlite3.Error):
        return [], {"status": "read_error", "path": str(path)}
    return rows, {"status": "ready", "path": str(path)}


def _bucket(day: date, grain: str) -> str:
    if grain == "month":
        return day.replace(day=1).isoformat()
    if grain == "week":
        return (day - timedelta(days=day.weekday())).isoformat()
    return day.isoformat()


def _trend_dates(first: date, last: date, grain: str) -> list[str]:
    values = []
    current = first
    while current <= last:
        key = _bucket(current, grain)
        if not values or values[-1] != key:
            values.append(key)
        current += timedelta(days=1)
    return values


def dashboard(db: Database, day: str, through: str, tz: str, *, agent: str | None = None,
              model: str | None = None, sync: bool = True, codex_root: Path | None = None,
              claude_root: Path | None = None, hermes_path: Path | None = None) -> dict:
    if agent and agent not in AGENTS:
        raise ValueError("Unsupported agent")
    first, last, start, end, zone = _range(day, through, tz)
    sync_codex = sync_codex_usage(db, codex_root) if sync else {"status": "not_synced", "updated_files": 0}
    sync_claude = sync_claude_usage(db, claude_root) if sync else {"status": "not_synced", "updated_files": 0}
    hermes, hermes_state = _hermes_rows(hermes_path or hermes_db_path())
    lower, upper = start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")
    condition = "occurred_at>=? AND occurred_at<?"
    params: list = [lower, upper]
    if model:
        condition += " AND model=?"
        params.append(model)
    with db.read() as conn:
        codex = [dict(row) for row in conn.execute(
            f"SELECT * FROM mvp_usage_requests WHERE {condition} ORDER BY occurred_at", params)]
        claude = [dict(row) for row in conn.execute(
            f"SELECT * FROM mvp_claude_requests WHERE {condition} ORDER BY occurred_at", params)]
        codex_titles = {row["native_id"]: row["title"] for row in conn.execute(
            "SELECT native_id,title FROM sessions WHERE agent='codex' AND archived=0")}
        codex_meta = dict(conn.execute("SELECT MIN(occurred_at) AS earliest,MAX(occurred_at) AS latest FROM mvp_usage_requests").fetchone())
        claude_meta = dict(conn.execute("SELECT MIN(occurred_at) AS earliest,MAX(occurred_at) AS latest FROM mvp_claude_requests").fetchone())
        claude_missing = conn.execute(f"""SELECT COALESCE(SUM(cache_read_known=0),0),
            COALESCE(SUM(cache_creation_known=0),0) FROM mvp_claude_requests WHERE {condition}""", params).fetchone()
        codex_files = [row[0] for row in conn.execute("SELECT status FROM mvp_usage_files")]
        claude_files = [row[0] for row in conn.execute("SELECT status FROM mvp_claude_files")]

    span = (last - first).days + 1
    grain = "day" if span <= 31 else "week" if span <= 180 else "month"
    trend = defaultdict(_empty)
    summary = _empty()
    sources = {name: _empty() for name in AGENTS}
    models = defaultdict(_empty)
    sessions = defaultdict(lambda: {**_empty(), "last_request": "", "title": "", "agent": ""})

    def record(name: str, native_id: str, title: str, model_name: str, at: datetime,
               values: dict, *, daily: bool) -> None:
        _add(sources[name], values)
        if agent not in (None, name):
            return
        _add(summary, values)
        _add(models[(name, model_name)], values)
        key = (name, native_id)
        _add(sessions[key], values)
        sessions[key]["agent"] = name
        sessions[key]["title"] = title
        sessions[key]["last_request"] = max(sessions[key]["last_request"], at.isoformat())
        if daily:
            _add(trend[_bucket(at.astimezone(zone).date(), grain)], values)

    for row in codex:
        original = max(0, row["input_tokens"])
        read = min(original, max(0, row["cached_input_tokens"]))
        at = datetime.fromisoformat(row["occurred_at"].replace("Z", "+00:00"))
        native_id = row["native_session_id"]
        record("codex", native_id, codex_titles.get(native_id) or f"Codex · {native_id[:8]}",
               row["model"], at, _values(original - read, read, 0, row["output_tokens"]), daily=True)
    for row in claude:
        at = datetime.fromisoformat(row["occurred_at"].replace("Z", "+00:00"))
        native_id = row["native_session_id"]
        title = f"{row['project']} · {native_id[:8]}" if row["project"] else f"Claude · {native_id[:8]}"
        record("claude", native_id, title, row["model"], at,
               _values(row["input_tokens"], row["cached_input_tokens"],
                       row["cache_creation_tokens"], row["output_tokens"]), daily=True)

    hermes_partial = 0
    hermes_undated = 0
    hermes_dates = []
    for row in hermes:
        try:
            seen = datetime.fromtimestamp(float(row["first_seen"]), timezone.utc)
            last_seen = datetime.fromtimestamp(float(row["last_seen"]), timezone.utc)
        except (TypeError, ValueError, OverflowError):
            hermes_undated += 1
            continue
        hermes_dates.extend((seen, last_seen))
        if model and (row["model"] or "unknown") != model:
            continue
        if seen < start or last_seen >= end:
            if seen < end and last_seen >= start:
                hermes_partial += 1
            continue
        native_id = str(row["session_id"])
        cwd = row["cwd"]
        title = row["title"] or (Path(cwd).name if isinstance(cwd, str) and cwd else "") or f"Hermes · {native_id[:8]}"
        record("hermes", native_id, title, row["model"] or "unknown", last_seen,
               _values(row["input_tokens"], row["cache_read_tokens"],
                       row["cache_write_tokens"], row["output_tokens"], row["api_call_count"]), daily=False)

    source_meta = {
        "codex": {"status": sync_codex["status"], "precision": "request", "files": len(codex_files),
                  "deferred": sum(status != "ok" for status in codex_files),
                  "earliest": codex_meta["earliest"], "latest": codex_meta["latest"],
                  "updated_files": sync_codex.get("updated_files", 0)},
        "claude": {"status": sync_claude["status"], "precision": "request", "files": len(claude_files),
                   "deferred": sum(status != "ok" for status in claude_files),
                   "earliest": claude_meta["earliest"], "latest": claude_meta["latest"],
                   "missing_cache_read": claude_missing[0], "missing_cache_write": claude_missing[1],
                   "updated_files": sync_claude.get("updated_files", 0)},
        "hermes": {**hermes_state, "precision": "session_model_aggregate", "files": 1 if hermes_state["status"] == "ready" else 0,
                   "deferred": hermes_undated + hermes_partial,
                   "earliest": min(hermes_dates).isoformat() if hermes_dates else None,
                   "latest": max(hermes_dates).isoformat() if hermes_dates else None,
                   "partial_rows": hermes_partial, "undated_rows": hermes_undated},
    }
    for name, values in sources.items():
        source_meta[name].update(values)
    results = [{"agent": name, "model": name_model, **values}
               for (name, name_model), values in sorted(models.items(), key=lambda item: -item[1]["total_tokens"])]
    result_sessions = [{"native_id": native_id, **values} for (_, native_id), values in
                       sorted(sessions.items(), key=lambda item: item[1]["last_request"], reverse=True)]
    summary["cache_hit_rate"] = (summary["cached_input_tokens"] / summary["input_tokens"]
                                  if summary["input_tokens"] else None)
    return {"status": "ready", "filters": {"day": day, "through": through, "tz": tz, "agent": agent, "model": model},
            "summary": summary, "sources": source_meta, "models": results,
            "sessions": result_sessions[:100], "session_count": len(result_sessions),
            "trend_granularity": grain,
            "trend": [{"period": period, **trend[period]} for period in _trend_dates(first, last, grain)],
            "unattributed_tokens": sources["hermes"]["total_tokens"] if agent in (None, "hermes") else 0,
            "note": "Codex/Claude 按请求时间归属；Hermes 仅纳入完整落在所选时段的会话模型汇总，无法按天拆分。"}


def session_requests(db: Database, agent: str, native_id: str, day: str, through: str, tz: str,
                     *, model: str | None = None, limit: int = 100,
                     hermes_path: Path | None = None) -> dict:
    if agent not in AGENTS:
        raise ValueError("Unsupported agent")
    _, _, start, end, _ = _range(day, through, tz)
    if agent == "hermes":
        rows, _ = _hermes_rows(hermes_path or hermes_db_path())
        results = []
        for row in rows:
            if str(row["session_id"]) != native_id or (model and (row["model"] or "unknown") != model):
                continue
            try:
                first = datetime.fromtimestamp(float(row["first_seen"]), timezone.utc)
                last = datetime.fromtimestamp(float(row["last_seen"]), timezone.utc)
            except (ValueError, TypeError, OverflowError):
                continue
            if first < start or last >= end:
                continue
            values = _values(row["input_tokens"], row["cache_read_tokens"],
                             row["cache_write_tokens"], row["output_tokens"], row["api_call_count"])
            results.append({"request_id": f"hermes:{native_id}:{row['model']}:{row['task']}",
                            "occurred_at": last.isoformat(), "first_seen": first.isoformat(),
                            "model": row["model"] or "unknown", "precision": "session_model_aggregate", **values})
        return {"items": results[:limit], "count": len(results), "precision": "session_model_aggregate"}
    table = "mvp_usage_requests" if agent == "codex" else "mvp_claude_requests"
    condition = "native_session_id=? AND occurred_at>=? AND occurred_at<?"
    params: list = [native_id, start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")]
    if model:
        condition += " AND model=?"
        params.append(model)
    with db.read() as conn:
        count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {condition}", params).fetchone()[0]
        fields = "request_id,occurred_at,model,input_tokens,cached_input_tokens,output_tokens"
        if agent == "claude":
            fields += ",cache_creation_tokens"
        rows = conn.execute(f"SELECT {fields} FROM {table} WHERE {condition} "
                            "ORDER BY occurred_at DESC LIMIT ?", [*params, limit]).fetchall()
    items = []
    for row in rows:
        raw = dict(row)
        original = raw["input_tokens"]
        read = min(original, raw["cached_input_tokens"]) if agent == "codex" else raw["cached_input_tokens"]
        values = _values(original - read if agent == "codex" else original,
                         read, raw.get("cache_creation_tokens", 0), raw["output_tokens"])
        items.append({"request_id": raw["request_id"], "occurred_at": raw["occurred_at"],
                      "model": raw["model"], "precision": "request", **values})
    return {"items": items, "count": count, "precision": "request"}
