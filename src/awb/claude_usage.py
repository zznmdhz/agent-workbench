"""Read request-level usage from Claude Code's local session transcripts.

The assistant message-id deduplication follows CC Switch's MIT-licensed
session_usage.rs algorithm (2026-09-26 checkout). See the bundled third-party
license. Message bodies are not retained.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class ClaudeRequest:
    request_id: str
    native_session_id: str
    occurred_at: str
    model: str
    input_tokens: int
    cached_input_tokens: int
    cache_creation_tokens: int
    output_tokens: int
    cache_read_known: bool
    cache_creation_known: bool
    source_file: str
    source_offset: int
    project: str
    finalized: bool


def claude_usage_files(root: Path) -> list[Path]:
    projects = root / "projects"
    return sorted(projects.rglob("*.jsonl")) if projects.is_dir() else []


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _iso(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if parsed.tzinfo else None


def scan_claude_requests(paths: list[Path]) -> tuple[list[ClaudeRequest], dict[str, str]]:
    """Keep one final/best usage snapshot per Claude message, across all files."""
    chosen: dict[str, ClaudeRequest] = {}
    status: dict[str, str] = {}
    for path in paths:
        try:
            with path.open("rb") as stream:
                while line := stream.readline():
                    offset = stream.tell()
                    try:
                        row = json.loads(line)
                    except (UnicodeError, ValueError):
                        continue  # live writer may leave a partial final line
                    if not isinstance(row, dict) or row.get("type") != "assistant":
                        continue
                    message = row.get("message")
                    if not isinstance(message, dict):
                        continue
                    message_id = message.get("id")
                    usage = message.get("usage")
                    at = _iso(row.get("timestamp"))
                    if not isinstance(message_id, str) or not message_id or not isinstance(usage, dict) or not at:
                        continue
                    session_id = row.get("sessionId") or path.stem
                    if not isinstance(session_id, str):
                        continue
                    fresh = _count(usage.get("input_tokens"))
                    cache_read = _count(usage.get("cache_read_input_tokens"))
                    cache_write = _count(usage.get("cache_creation_input_tokens"))
                    output = _count(usage.get("output_tokens"))
                    model = message.get("model")
                    model = model.strip() if isinstance(model, str) else ""
                    cwd = row.get("cwd")
                    project = Path(cwd).name if isinstance(cwd, str) and cwd else ""
                    candidate = ClaudeRequest(
                        request_id=f"claude:{message_id}",
                        native_session_id=session_id, occurred_at=at, model=model or "unknown",
                        input_tokens=fresh, cached_input_tokens=cache_read,
                        cache_creation_tokens=cache_write, output_tokens=output,
                        cache_read_known="cache_read_input_tokens" in usage,
                        cache_creation_known="cache_creation_input_tokens" in usage,
                        source_file=str(path), source_offset=offset, project=project,
                        finalized=message.get("stop_reason") is not None,
                    )
                    previous = chosen.get(candidate.request_id)
                    if previous is None or (candidate.finalized and not previous.finalized) or (
                            candidate.finalized == previous.finalized
                            and candidate.output_tokens > previous.output_tokens):
                        chosen[candidate.request_id] = candidate
            status[str(path)] = "ok"
        except OSError:
            status[str(path)] = "read_error"
    return [request for request in chosen.values() if any((request.input_tokens,
            request.cached_input_tokens, request.cache_creation_tokens,
            request.output_tokens))], status
