"""Codex request-level usage extraction for the Windows MVP.

Adapted from the MIT-licensed CC Switch session usage algorithm:
https://github.com/farion1231/cc-switch/blob/e0f70019b2758f5b6b9a04dd60e4689481a0c0ac/src-tauri/src/services/session_usage_codex.rs
Copyright (c) 2025 Jason Young. See docs/project/CC_SWITCH_MVP.md and LICENSE.

This parser reads source logs only. It does not read CC Switch's database,
requests, credentials, or message bodies. Ambiguous fork replay is deferred.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator
from uuid import UUID

TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens",
                "reasoning_output_tokens", "total_tokens")
UUID_SUFFIX = re.compile(r"([0-9a-fA-F-]{36})$")


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _counter(value: object) -> tuple | None:
    if not isinstance(value, dict):
        return None
    return tuple(value.get(key) if isinstance(value.get(key), int) else None for key in TOKEN_FIELDS)


def _signature(info: dict) -> tuple | None:
    total, last = _counter(info.get("total_token_usage")), _counter(info.get("last_token_usage"))
    return (total, last) if total is not None or last is not None else None


def _values(value: dict | None) -> tuple[int, int, int] | None:
    if not isinstance(value, dict) or not any(key in value for key in TOKEN_FIELDS):
        return None
    return (max(0, int(value.get("input_tokens") or 0)),
            max(0, int(value.get("cached_input_tokens", value.get("cache_read_input_tokens")) or 0)),
            max(0, int(value.get("output_tokens") or 0)))


def _model(value: str) -> str:
    value = value.lower().rsplit("/", 1)[-1]
    return re.sub(r"-(?:\d{4}-\d{2}-\d{2}|\d{8})$", "", value)


def _rollout_id(path: Path) -> str | None:
    match = UUID_SUFFIX.search(path.stem)
    if not match:
        return None
    try:
        return str(UUID(match.group(1)))
    except ValueError:
        return None


def _parent_id(meta: dict) -> str | None:
    if isinstance(meta.get("forked_from_id"), str):
        return meta["forked_from_id"]
    source = meta.get("source")
    if not isinstance(source, dict):
        return None
    subagent = source.get("subagent")
    if not isinstance(subagent, dict):
        return None
    spawn = subagent.get("thread_spawn")
    return spawn.get("parent_thread_id") if isinstance(spawn, dict) else None


def _records(path: Path) -> Iterator[tuple[int, dict]]:
    with path.open("rb") as stream:
        while line := stream.readline():
            if not line.endswith(b"\n"):
                try:
                    row = json.loads(line)
                except (UnicodeError, ValueError):
                    return  # live writer's incomplete suffix
            else:
                try:
                    row = json.loads(line)
                except (UnicodeError, ValueError):
                    continue
            if isinstance(row, dict):
                yield stream.tell(), row


@dataclass(frozen=True)
class UsageRequest:
    request_id: str
    native_session_id: str
    occurred_at: str
    model: str
    input_tokens: int  # Codex total input, including cache read
    cached_input_tokens: int
    output_tokens: int
    source_file: str
    source_offset: int


@dataclass
class _Parsed:
    path: Path
    rollout_id: str | None
    native_session_id: str | None
    parent_id: str | None
    fork_time: datetime | None
    events: list[tuple[tuple, tuple[int, int, int], str, str | None, int, int]]
    reason: str | None = None


def _parse(path: Path) -> _Parsed:
    rollout_id = _rollout_id(path)
    native_id = None
    parent_id = None
    fork_time = None
    reason = None
    current_model = "unknown"
    high_water: tuple[int, int, int] | None = None
    last_signature_by_source: dict[str | None, tuple] = {}
    previous_signature = None
    event_index = 0
    events = []
    for offset, record in _records(path):
        typ = record.get("type")
        payload = record.get("payload") or {}
        if typ == "session_meta" and native_id is None:
            native_id = payload.get("id") or payload.get("thread_id") or payload.get("threadId")
            parent_id = _parent_id(payload)
            fork_time = _timestamp(record.get("timestamp"))
            if rollout_id is None:
                reason = "missing_rollout_id"
            elif native_id != rollout_id and not path.stem.endswith(f"{native_id}_{rollout_id}"):
                reason = "rollout_identity_mismatch"
            continue
        if typ == "turn_context":
            model = payload.get("model") or (payload.get("info") or {}).get("model")
            if isinstance(model, str):
                current_model = _model(model)
            continue
        if typ != "event_msg" or payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        if not isinstance(info, dict):
            continue
        signature = _signature(info)
        if signature is None:
            continue
        model = info.get("model") or info.get("model_name") or payload.get("model")
        if isinstance(model, str):
            current_model = _model(model)
        source = (payload.get("rate_limits") or {}).get("limit_id")
        total = _values(info.get("total_token_usage"))
        last = _values(info.get("last_token_usage"))
        duplicate = total is not None and (last_signature_by_source.get(source) == signature
                                            or previous_signature == signature)
        if total is not None:
            last_signature_by_source[source] = signature
        previous_signature = signature
        if duplicate:
            delta = (0, 0, 0)
        elif last is not None:
            delta = last
        elif total is not None:
            delta = tuple(max(0, value - prior) for value, prior in
                          zip(total, high_water or (0, 0, 0)))
        else:
            continue
        if total is not None:
            high_water = tuple(max(a, b) for a, b in zip(high_water or (0, 0, 0), total))
        delta = (delta[0], min(delta[0], delta[1]), delta[2])
        if any(delta):
            event_index += 1
        events.append((signature, delta, current_model, record.get("timestamp"), offset,
                       event_index if any(delta) else 0))
    if native_id is None:
        reason = reason or "missing_session_meta"
    return _Parsed(path, rollout_id, native_id, parent_id, fork_time, events, reason)


def _parent_signatures(path: Path, cutoff: datetime) -> list[tuple] | None:
    values = []
    maximum = None
    for _, record in _records(path):
        stamp = _timestamp(record.get("timestamp"))
        if stamp is not None:
            maximum = max(maximum, stamp) if maximum else stamp
        payload = record.get("payload") or {}
        if record.get("type") != "event_msg" or payload.get("type") != "token_count":
            continue
        signature = _signature(payload.get("info") or {})
        if signature is None:
            continue
        if stamp is None:
            return None
        if stamp <= cutoff:
            values.append(signature)
    return values if maximum is not None and maximum >= cutoff else None


def codex_usage_files(root: Path) -> list[Path]:
    return sorted([*root.joinpath("sessions").rglob("*.jsonl"),
                   *root.joinpath("archived_sessions").glob("*.jsonl")])


def scan_codex_requests(root: Path, *, only_paths: set[Path] | None = None,
                        file_status: dict[Path, str] | None = None
                        ) -> tuple[list[UsageRequest], dict[str, int]]:
    """Return trustworthy request records and explicit exclusion counts.

    A changed file can be rescanned: request_id derives from physical rollout
    UUID and nonzero token event index, not the current database cursor.
    """
    paths = codex_usage_files(root)
    index: dict[str, list[Path]] = {}
    for path in paths:
        rollout_id = _rollout_id(path)
        if rollout_id:
            index.setdefault(rollout_id, []).append(path)
    requests: list[UsageRequest] = []
    exclusions: dict[str, int] = {}
    for path in paths:
        if only_paths is not None and path not in only_paths:
            continue
        parsed = _parse(path)
        if parsed.reason:
            exclusions[parsed.reason] = exclusions.get(parsed.reason, 0) + 1
            if file_status is not None:
                file_status[path] = parsed.reason
            continue
        replay_prefix = 0
        if parsed.parent_id:
            if parsed.fork_time is None:
                exclusions["undated_fork"] = exclusions.get("undated_fork", 0) + 1
                if file_status is not None:
                    file_status[path] = "undated_fork"
                continue
            parent_paths = index.get(parsed.parent_id, [])
            candidates = [_parent_signatures(parent, parsed.fork_time) for parent in parent_paths]
            if not candidates or any(candidate is None or candidate != candidates[0] for candidate in candidates):
                exclusions["unresolved_parent"] = exclusions.get("unresolved_parent", 0) + 1
                if file_status is not None:
                    file_status[path] = "unresolved_parent"
                continue
            parent = candidates[0] or []
            cursor = 0
            for signature, *_ in parsed.events:
                try:
                    at = parent.index(signature, cursor)
                except ValueError:
                    break
                replay_prefix += 1
                cursor = at + 1
        for offset, (_, delta, model, at, source_offset, event_index) in enumerate(parsed.events):
            if offset < replay_prefix or event_index == 0 or _timestamp(at) is None:
                continue
            requests.append(UsageRequest(
                request_id=f"codex_session:thread-v1:{parsed.rollout_id}:{event_index}",
                native_session_id=parsed.native_session_id or "", occurred_at=at or "",
                model=model, input_tokens=delta[0], cached_input_tokens=delta[1],
                output_tokens=delta[2], source_file=str(path), source_offset=source_offset))
        if file_status is not None:
            file_status[path] = "ok"
    return requests, exclusions
