"""Deterministic identities and small, source-independent accounting rules."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def event_id(content: dict[str, Any]) -> str:
    return sha256([1, content["source_instance_id"], content["native_session_id"], content["fact_kind"], content["native_fact_id"], content["revision_key"]])


def utc_ms(value: str | float | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, (float, int)):
        # Hermes uses epoch seconds; Codex may supply ISO dates or epoch ms.
        return int(value if value > 10**11 else value * 1000)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return int(parsed.timestamp() * 1000)
    except ValueError:
        return None


def iso_utc(value: str | float | int | None) -> str | None:
    ms = utc_ms(value)
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def day_bounds_utc_ms(day: str, zone: str) -> tuple[int, int]:
    local = datetime.fromisoformat(day).date()
    tz = ZoneInfo(zone)
    start = datetime.combine(local, datetime.min.time(), tzinfo=tz)
    following = datetime.combine(local.fromordinal(local.toordinal() + 1), datetime.min.time(), tzinfo=tz)
    return int(start.timestamp() * 1000), int(following.timestamp() * 1000)


def interval_overlap(start: int, end: int, window_start: int, window_end: int) -> int:
    return max(0, min(end, window_end) - max(start, window_start))


def union_ms(intervals: Iterable[tuple[int, int]]) -> int:
    ordered = sorted((a, b) for a, b in intervals if b > a)
    if not ordered:
        return 0
    total = 0
    left, right = ordered[0]
    for start, end in ordered[1:]:
        if start > right:
            total += right - left
            left, right = start, end
        else:
            right = max(right, end)
    return total + right - left


def percentile_nearest_rank(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def unicode_codepoints(text: str) -> int:
    return len(text.replace("\r\n", "\n"))


def cumulative_deltas(snapshots: Iterable[dict[str, Any]], zone: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Classify cumulative consumption into day facts and unallocated ranges.

    The first snapshot is a baseline unless a verified zero origin precedes it.
    Caller must group by counter identity and epoch; this function never guesses a reset.
    """
    rows = sorted(snapshots, key=lambda r: (r["source_order"], r.get("source_time") or ""))
    dated: list[dict[str, Any]] = []
    unallocated: list[dict[str, Any]] = []
    prior: dict[str, Any] | None = None
    tainted = False
    for row in rows:
        current = int(row["total_tokens"])
        if tainted:
            unallocated.append({"amount": None, "reason": "counter_epoch_untrusted", "until": row.get("source_time")})
            continue
        if prior is None:
            if row.get("verified_zero_origin") and row.get("origin_time") and row.get("source_time"):
                delta = current
                begin = utc_ms(row["origin_time"])
            else:
                unallocated.append({"amount": current, "reason": "opening_balance", "until": row.get("source_time")})
                prior = row
                continue
        else:
            delta = current - int(prior["total_tokens"])
            begin = utc_ms(prior.get("source_time"))
        if delta < 0:
            unallocated.append({"amount": None, "reason": "counter_regression", "from": prior.get("source_time") if prior else None, "until": row.get("source_time")})
            tainted = True
            continue
        finish = utc_ms(row.get("source_time"))
        if begin is None or finish is None or finish < begin:
            unallocated.append({"amount": delta, "reason": "undated_interval", "from": prior.get("source_time") if prior else row.get("origin_time"), "until": row.get("source_time")})
        else:
            start_day = datetime.fromtimestamp(begin / 1000, ZoneInfo(zone)).date()
            # A right-closed counter interval ending exactly at midnight is not fully within either day.
            end_day = datetime.fromtimestamp(finish / 1000, ZoneInfo(zone)).date()
            if start_day == end_day:
                dated.append({"day": start_day.isoformat(), "amount": delta, "quality": "counter_interval"})
            else:
                unallocated.append({"amount": delta, "reason": "cross_day_interval", "from": prior.get("source_time") if prior else row.get("origin_time"), "until": row.get("source_time")})
        prior = row
    return dated, unallocated


def ack_prefix(current: int, receipts: dict[int, str]) -> int:
    settled = {"accepted", "duplicate", "ignored_tombstoned", "quarantined_resolved"}
    while receipts.get(current + 1) in settled:
        current += 1
    return current
