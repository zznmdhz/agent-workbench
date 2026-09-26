"""Evidence-aware statistics from projected facts."""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

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
              agents: list[str] | None = None, models: list[str] | None = None,
              through: str | None = None) -> dict:
    start, _ = day_bounds_utc_ms(day, tz)
    _, end = day_bounds_utc_ms(through or day, tz)
    if end <= start or end - start > 32 * 86_400_000:
        raise ValueError("Invalid date range")
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
    settled_duration_ms = 0
    complete_durations = []
    finished_count = 0
    mixed = []
    daily: dict[str, dict[str, int]] = defaultdict(lambda: {"runs": 0, "completed": 0, "human_chars": 0, "counter_tokens": 0})
    device_summary: dict[str, dict[str, Any]] = defaultdict(lambda: {"runs": 0, "settled_duration_ms": 0, "human_chars": 0, "counter_tokens": 0, "intervals": []})
    zone = ZoneInfo(tz)
    for r in rows:
        if r["parent_run_ref"]:
            continue
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
        if start <= a < end:
            daily[datetime.fromtimestamp(a / 1000, zone).date().isoformat()]["runs"] += 1
            device_summary[r["device_id"] or "unknown"]["runs"] += 1
        if (b is not None and b >= a and r["status"] in {"completed", "failed", "cancelled"}
                and r["duration_basis"] in {"source", "start_end"}):
            clipped = (max(a, start), min(b, end))
            if clipped[1] > clipped[0]:
                intervals.append(clipped)
                settled_duration_ms += clipped[1] - clipped[0]
                device_summary[r["device_id"] or "unknown"]["settled_duration_ms"] += clipped[1] - clipped[0]
                device_summary[r["device_id"] or "unknown"]["intervals"].append(clipped)
        if start <= a < end and r["status"] == "completed":
            finished_count += 1
            daily[datetime.fromtimestamp(a / 1000, zone).date().isoformat()]["completed"] += 1
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
            daily[datetime.fromtimestamp(at / 1000, zone).date().isoformat()]["human_chars"] += int(m["source_char_count"])
            device_summary[m["device_id"] or "unknown"]["human_chars"] += int(m["source_char_count"])
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
            key = (u["session_id"], u["counter_id"], u["epoch_id"], u["model"], u["provider"], u["usage_key"])
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
        in_range = [x for x in dated if day <= x["day"] <= (through or day)]
        field = {"codex_input": "counter_input_tokens", "codex_output": "counter_output_tokens"}.get(
            snapshots[0]["usage_key"], "total_tokens")
        totals[field] += sum(int(x["amount"]) for x in in_range)
        usage_counts[field] += len(in_range)
        if field == "total_tokens":
            for item in in_range:
                daily[item["day"]]["counter_tokens"] += int(item["amount"])
                device_summary[snapshots[0]["device_id"] or "unknown"]["counter_tokens"] += int(item["amount"])
        unallocated.extend({"counter": key[:3], **x} for x in loose)
    durations_sorted = sorted(complete_durations)
    med = statistics.median(durations_sorted) if durations_sorted else None
    metrics = [
        _metric("active_wall_ms", union_ms(intervals), "ms", len(intervals), len(eligible)-len(intervals), "并行轮次区间取并集"),
        _metric("settled_duration_ms", settled_duration_ms, "ms", len(intervals), len(eligible)-len(intervals), "已结算顶层轮次累计时间；并行时间分别计入"),
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
        _metric("counter_input_tokens", totals["counter_input_tokens"] if usage_counts["counter_input_tokens"] else None, "tokens", usage_counts["counter_input_tokens"], 0, "Codex 会话累计计数的同日差值；不含无法归属的开头余额"),
        _metric("counter_output_tokens", totals["counter_output_tokens"] if usage_counts["counter_output_tokens"] else None, "tokens", usage_counts["counter_output_tokens"], 0, "Codex 会话累计计数的同日差值；不含无法归属的开头余额"),
    ]
    return {"filters": {"day": day, "through": through or day, "tz": tz, "device_ids": list(devices), "agent_ids": list(agents_set), "model_ids": list(models_set)},
            "projection_version": 1, "as_of": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "metrics": metrics, "sources": [dict(r) for r in source_rows], "unallocated_usage": unallocated,
            "mixed_model_runs": mixed,
            "daily_trend": [{"day": (date.fromisoformat(day) + timedelta(days=i)).isoformat(),
                             **daily[(date.fromisoformat(day) + timedelta(days=i)).isoformat()]}
                            for i in range((date.fromisoformat(through or day) - date.fromisoformat(day)).days + 1)],
            "device_comparison": [{"device_id": device_id,
                                   "active_wall_ms": union_ms(item["intervals"]),
                                   **{k: v for k, v in item.items() if k != "intervals"}}
                                  for device_id, item in device_summary.items()],
            "warnings": ["仅显示已采集证据；未发现的历史轮次不计入覆盖率"]}


def _ms(value: str | None) -> int | None:
    if value is None:
        return None
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def timeline(db: Database, day: str, tz: str, device_ids: list[str] | None = None,
             agents: list[str] | None = None, models: list[str] | None = None) -> dict:
    start, end = day_bounds_utc_ms(day, tz)
    start_iso = datetime.fromtimestamp(start / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
    end_iso = datetime.fromtimestamp(end / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
    with db.read() as conn:
        rows = conn.execute("""SELECT r.id,r.session_id,r.status,r.start_at,r.end_at,
            s.agent,s.title,d.name AS device_name,r.device_id,r.model,r.model_attribution,r.parent_run_ref FROM runs r JOIN sessions s ON s.id=r.session_id
            LEFT JOIN devices d ON d.id=r.device_id WHERE s.archived=0 AND r.start_at IS NOT NULL
            AND r.start_at<? AND (r.end_at IS NULL OR r.end_at>?)
            ORDER BY r.start_at LIMIT 10000""", (end_iso, start_iso)).fetchall()
    items = []
    for row in rows:
        if row["parent_run_ref"]:
            continue
        if device_ids and row["device_id"] not in device_ids:
            continue
        if agents and row["agent"] not in agents:
            continue
        if models and (row["model"] not in models or row["model_attribution"] not in {"single", "reported"}):
            continue
        a, b = _ms(row["start_at"]), _ms(row["end_at"])
        if a is None or a >= end or (b is not None and b <= start):
            continue
        items.append({**dict(row), "visible_start_ms": max(a, start),
                      "visible_end_ms": min(b, end) if b is not None else None,
                      "boundary_quality": "complete" if b is not None and b >= a else "unknown_end"})
    return {"day": day, "tz": tz, "window_start_ms": start, "window_end_ms": end,
            "items": items[:1000], "truncated": len(items) > 1000}


def metric_contributors(db: Database, metric_id: str, day: str, tz: str,
                        through: str | None = None, device_ids: list[str] | None = None,
                        agents: list[str] | None = None, models: list[str] | None = None) -> dict:
    """Bounded, source-linked evidence for the six overview cards."""
    supported = {"run_count", "settled_duration_ms", "active_wall_ms", "human_input_chars",
                 "input_tokens", "output_tokens"}
    if metric_id not in supported:
        raise ValueError("Unsupported metric")
    start, _ = day_bounds_utc_ms(day, tz)
    _, end = day_bounds_utc_ms(through or day, tz)
    if end <= start or end - start > 32 * 86_400_000:
        raise ValueError("Invalid date range")
    device_set, agent_set, model_set = set(device_ids or []), set(agents or []), set(models or [])
    with db.read() as conn:
        session_rows = conn.execute("""SELECT s.id,s.title,s.cwd,s.agent,s.device_id,d.name AS device_name
            FROM sessions s LEFT JOIN devices d ON d.id=s.device_id WHERE s.archived=0""").fetchall()
        sessions = {r["id"]: dict(r) for r in session_rows}
        table = "runs" if metric_id in {"run_count", "settled_duration_ms", "active_wall_ms"} else (
            "messages" if metric_id == "human_input_chars" else "usage_observations")
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
    def allowed(session_id: str, model: str | None = None, attribution: str | None = None) -> bool:
        s = sessions.get(session_id)
        return bool(s and (not device_set or s["device_id"] in device_set)
                    and (not agent_set or s["agent"] in agent_set)
                    and (not model_set or (model in model_set and attribution in {"single", "reported"})))
    amounts: dict[str, int] = defaultdict(int)
    counts: dict[str, int] = defaultdict(int)
    evidence: dict[str, list[dict]] = defaultdict(list)
    if table == "runs":
        for row in rows:
            sid = row["session_id"]
            if row["parent_run_ref"] or not allowed(sid, row["model"], row["model_attribution"]):
                continue
            a, b = _ms(row["start_at"]), _ms(row["end_at"])
            if a is None or a >= end or (b is not None and b <= start):
                continue
            if metric_id == "run_count":
                if not start <= a < end:
                    continue
                amount = 1
            else:
                if b is None or b < a or row["status"] not in {"completed", "failed", "cancelled"} or row["duration_basis"] not in {"source", "start_end"}:
                    continue
                amount = max(0, min(b, end) - max(a, start))
                if amount == 0:
                    continue
            amounts[sid] += amount
            counts[sid] += 1
            if len(evidence[sid]) < 5:
                evidence[sid].append({"run_id": row["id"], "at": row["start_at"], "amount": amount})
    elif table == "messages":
        for row in rows:
            sid, at = row["session_id"], _ms(row["occurred_at"])
            if not allowed(sid) or model_set or at is None or not start <= at < end:
                continue
            if row["role"] == "user" and row["input_origin"] == "human" and row["source_char_count"] is not None:
                amounts[sid] += int(row["source_char_count"])
                counts[sid] += 1
    else:
        component = "input_tokens" if metric_id == "input_tokens" else "output_tokens"
        for row in rows:
            sid, at = row["session_id"], _ms(row["source_time"])
            if not allowed(sid, row["model"], "reported") or row["semantics"] != "incremental" or at is None or not start <= at < end:
                continue
            if row[component] is not None:
                amounts[sid] += int(row[component])
                counts[sid] += 1
        if not amounts:
            counter_key = "codex_input" if metric_id == "input_tokens" else "codex_output"
            groups: dict[tuple, list[dict]] = defaultdict(list)
            for row in rows:
                sid = row["session_id"]
                if row["semantics"] == "cumulative_snapshot" and row["usage_key"] == counter_key and allowed(sid) and not model_set:
                    groups[(sid, row["counter_id"], row["epoch_id"], row["model"], row["provider"])].append(row)
            for key, snapshots in groups.items():
                dated, _ = cumulative_deltas(snapshots, tz)
                sid = key[0]
                for item in dated:
                    local_day = date.fromisoformat(item["day"])
                    if date.fromisoformat(day) <= local_day <= date.fromisoformat(through or day):
                        amounts[sid] += int(item["amount"])
                        counts[sid] += 1
    ordered = sorted(amounts, key=lambda sid: (-amounts[sid], sid))
    return {"metric_id": metric_id, "basis": "counter_interval" if table == "usage_observations" and not any(r["semantics"] == "incremental" for r in rows) else "recorded",
            "unit": "ms" if metric_id.endswith("_ms") else "count",
            "items": [{"session_id": sid, "title": sessions[sid]["title"], "cwd": sessions[sid]["cwd"],
                       "agent": sessions[sid]["agent"], "device_name": sessions[sid]["device_name"],
                       "amount": amounts[sid], "evidence_count": counts[sid], "evidence": evidence[sid]}
                      for sid in ordered[:100]], "total_sessions": len(ordered), "truncated": len(ordered) > 100,
            "note": "活动时间按轮次并集计算，列表中的时长不可相加" if metric_id == "active_wall_ms" else None}
