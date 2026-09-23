"""Evidence-aware statistics from projected facts."""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .codec import (
    cumulative_deltas,
    day_bounds_utc_ms,
    percentile_nearest_rank,
    union_ms,
)
from .db import Database


def _metric(metric_id: str, value: Any, unit: str, included: int, excluded: int, note: str = "") -> dict:
    return {"id": metric_id, "value": value, "unit": unit, "included_count": included,
            "excluded_count": excluded, "quality_note": note}


def calculate(db: Database, day: str, tz: str, device_ids: list[str] | None = None,
              agents: list[str] | None = None, models: list[str] | None = None) -> dict:
    start, end = day_bounds_utc_ms(day, tz)
    devices = set(device_ids or [])
    agents_set = set(agents or [])
    models_set = set(models or [])
    with db.read() as conn:
        rows = conn.execute("""SELECT r.*,s.agent,s.source_id FROM runs r
            JOIN sessions s ON s.id=r.session_id WHERE s.archived=0""").fetchall()
        messages = conn.execute("""SELECT m.*,s.agent,s.device_id FROM messages m
            JOIN sessions s ON s.id=m.session_id WHERE s.archived=0""").fetchall()
        usage = conn.execute("""SELECT u.*,s.agent,s.device_id FROM usage_observations u
            JOIN sessions s ON s.id=u.session_id WHERE s.archived=0""").fetchall()
        source_rows = conn.execute("SELECT id,device_id,agent,last_scan,last_event,last_error FROM sources").fetchall()
    eligible = []
    intervals = []
    complete_durations = []
    finished_count = 0
    mixed = []
    for r in rows:
        if devices and r["device_id"] not in devices:
            continue
        if agents_set and r["agent"] not in agents_set:
            continue
        if models_set and (r["model"] not in models_set or r["model_attribution"] not in {"single", "reported"}):
            mixed.append({"run_id": r["id"], "model": r["model"], "basis": r["model_attribution"]})
            continue
        a = _ms(r["start_at"])
        b = _ms(r["end_at"])
        if a is None or a >= end or (b is not None and b <= start):
            continue
        eligible.append(r)
        if b is not None and b > a and r["status"] == "completed":
            intervals.append((max(a, start), min(b, end)))
        if start <= a < end and r["status"] == "completed":
            finished_count += 1
            if b is not None and b >= a and r["duration_ms"] is not None and r["duration_basis"] in {"source", "start_end"}:
                complete_durations.append(int(r["duration_ms"]))
    chars = 0
    msg_count = 0
    for m in messages:
        if devices and m["device_id"] not in devices:
            continue
        if agents_set and m["agent"] not in agents_set:
            continue
        at = _ms(m["occurred_at"])
        if at is None or not start <= at < end:
            continue
        if models_set:
            # No proven message-to-unique-model attribution is stored.
            continue
        if m["role"] == "user" and m["input_origin"] == "human" and m["source_char_count"] is not None:
            chars += int(m["source_char_count"])
            msg_count += 1
    totals = defaultdict(int)
    usage_counts = defaultdict(int)
    unallocated = []
    cumulative = defaultdict(list)
    for u in usage:
        if devices and u["device_id"] not in devices:
            continue
        if agents_set and u["agent"] not in agents_set:
            continue
        if models_set and u["model"] not in models_set:
            continue
        semantics = u["semantics"]
        if semantics == "cumulative_snapshot":
            key = (u["session_id"], u["counter_id"], u["epoch_id"], u["model"], u["provider"])
            cumulative[key].append(dict(u))
            continue
        if semantics != "incremental":
            continue
        at = _ms(u["source_time"])
        if at is None or not start <= at < end:
            unallocated.append({"event_id": u["event_id"], "reason": "undated_usage"})
            continue
        for field in ("input_tokens", "output_tokens"):
            if u[field] is not None:
                totals[field] += int(u[field])
                usage_counts[field] += 1
    for key, snapshots in cumulative.items():
        dated, loose = cumulative_deltas(snapshots, tz)
        totals["total_tokens"] += sum(int(x["amount"]) for x in dated if x["day"] == day)
        usage_counts["total_tokens"] += sum(1 for x in dated if x["day"] == day)
        unallocated.extend({"counter": key[:3], **x} for x in loose)
    durations_sorted = sorted(complete_durations)
    med = statistics.median(durations_sorted) if durations_sorted else None
    metrics = [
        _metric("active_wall_ms", union_ms(intervals), "ms", len(intervals), len(eligible)-len(intervals), "并行轮次区间取并集"),
        _metric("run_count", len([r for r in eligible if start <= (_ms(r["start_at"]) or -1) < end]), "runs", len(eligible), 0),
        _metric("completed_count", finished_count, "runs", finished_count, 0),
        _metric("duration_mean_ms", round(statistics.mean(durations_sorted)) if durations_sorted else None, "ms", len(durations_sorted), finished_count-len(durations_sorted)),
        _metric("duration_median_ms", med, "ms", len(durations_sorted), finished_count-len(durations_sorted)),
        _metric("duration_p95_ms", percentile_nearest_rank(durations_sorted, .95), "ms", len(durations_sorted), finished_count-len(durations_sorted)),
        _metric("duration_coverage", len(durations_sorted)/finished_count if finished_count else None, "ratio", len(durations_sorted), finished_count-len(durations_sorted)),
        _metric("human_input_chars", chars if not models_set else None, "codepoints", msg_count, 0, "模型筛选时无法证明整轮归属" if models_set else "仅人类输入，按 Unicode code point"),
        _metric("input_tokens", totals["input_tokens"] if usage_counts["input_tokens"] else None, "tokens", usage_counts["input_tokens"], 0),
        _metric("output_tokens", totals["output_tokens"] if usage_counts["output_tokens"] else None, "tokens", usage_counts["output_tokens"], 0),
        _metric("counter_total_tokens", totals["total_tokens"] if usage_counts["total_tokens"] else None, "tokens", usage_counts["total_tokens"], len(unallocated), "与增量使用量可能重叠，不合并"),
    ]
    return {"filters": {"day": day, "tz": tz, "device_ids": list(devices), "agent_ids": list(agents_set), "model_ids": list(models_set)},
            "projection_version": 1, "as_of": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "metrics": metrics, "sources": [dict(r) for r in source_rows], "unallocated_usage": unallocated,
            "mixed_model_runs": mixed, "warnings": ["仅显示已采集证据；未发现的历史轮次不计入覆盖率"]}


def _ms(value: str | None) -> int | None:
    if value is None:
        return None
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def timeline(db: Database, day: str, tz: str) -> dict:
    start, end = day_bounds_utc_ms(day, tz)
    with db.read() as conn:
        rows = conn.execute("""SELECT r.id,r.session_id,r.status,r.start_at,r.end_at,
            s.agent,s.title,d.name AS device_name FROM runs r JOIN sessions s ON s.id=r.session_id
            LEFT JOIN devices d ON d.id=r.device_id WHERE s.archived=0 AND r.start_at IS NOT NULL
            ORDER BY r.start_at LIMIT 10000""").fetchall()
    items = []
    for row in rows:
        a, b = _ms(row["start_at"]), _ms(row["end_at"])
        if a is None or a >= end or (b is not None and b <= start):
            continue
        items.append({**dict(row), "visible_start_ms": max(a, start),
                      "visible_end_ms": min(b, end) if b is not None else None,
                      "boundary_quality": "complete" if b is not None and b >= a else "unknown_end"})
    return {"day": day, "tz": tz, "window_start_ms": start, "window_end_ms": end,
            "items": items[:1000], "truncated": len(items) > 1000}
