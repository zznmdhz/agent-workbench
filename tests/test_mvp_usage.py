import json
from pathlib import Path
from uuid import uuid4

from awb.codex_usage import scan_codex_requests
from awb.db import Database
from awb.mvp_usage import dashboard, sync_codex_usage


def _write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for row in records:
            stream.write(json.dumps(row) + "\n")


def _token(at: str, total: int, last: int, cached: int, output: int) -> dict:
    return {"type": "event_msg", "timestamp": at, "payload": {"type": "token_count", "info": {
        "total_token_usage": {"input_tokens": total, "cached_input_tokens": cached,
                              "output_tokens": output, "total_tokens": total+output},
        "last_token_usage": {"input_tokens": last, "cached_input_tokens": cached,
                             "output_tokens": output}}}}


def test_request_usage_fork_replay_and_repeated_scan(tmp_path: Path):
    root = tmp_path / ".codex"
    parent_id, child_id = str(uuid4()), str(uuid4())
    parent = root / "sessions" / "2026" / "09" / "26" / f"rollout-2026-09-26T00-00-00-{parent_id}.jsonl"
    child = root / "sessions" / "2026" / "09" / "26" / f"rollout-2026-09-26T00-01-00-{child_id}.jsonl"
    first = _token("2026-09-26T00:00:10Z", 100, 100, 60, 10)
    second = _token("2026-09-26T00:00:20Z", 150, 50, 20, 15)
    _write(parent, [
        {"type": "session_meta", "timestamp": "2026-09-26T00:00:00Z",
         "payload": {"id": parent_id}},
        {"type": "turn_context", "timestamp": "2026-09-26T00:00:01Z",
         "payload": {"model": "openai/GPT-6-SOL"}},
        first, second,
        {"type": "event_msg", "timestamp": "2026-09-26T00:01:01Z", "payload": {"type": "heartbeat"}},
    ])
    _write(child, [
        {"type": "session_meta", "timestamp": "2026-09-26T00:01:00Z",
         "payload": {"id": child_id, "forked_from_id": parent_id}},
        first, second,
        _token("2026-09-26T00:01:20Z", 180, 30, 10, 20),
    ])
    requests, exclusions = scan_codex_requests(root)
    assert exclusions == {}
    assert len(requests) == 3
    assert [r.input_tokens for r in requests if r.native_session_id == child_id] == [30]

    db = Database(tmp_path / "workbench.db")
    db.initialize()
    first_sync = sync_codex_usage(db, root)
    assert first_sync["updated_files"] == 2
    assert sync_codex_usage(db, root)["updated_files"] == 0
    result = dashboard(db, "2026-09-26", "2026-09-26", "UTC", sync=False)
    assert result["summary"]["requests"] == 3
    assert result["summary"]["input_tokens"] == 180
    assert result["summary"]["cached_input_tokens"] == 90
    assert result["summary"]["fresh_input_tokens"] == 90
    assert result["session_count"] == 2

    _write(parent, [_token("2026-09-26T00:02:00Z", 200, 50, 20, 25)])
    assert sync_codex_usage(db, root)["updated_files"] == 1
    assert dashboard(db, "2026-09-26", "2026-09-26", "UTC", sync=False)["summary"]["requests"] == 4
