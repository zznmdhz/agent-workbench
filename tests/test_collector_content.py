from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from awb.collector import (
    Outbox,
    backfill_codex_cached_input_history_once,
    codex_primary_session_meta,
    make_event,
    process_content_backfill,
    scan_codex,
    scan_hermes,
)
from awb.models import Envelope


def _write_records(path: Path, records: list[dict], append: bool = False) -> None:
    with path.open("a" if append else "w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def _record(typ: str, at: str, payload: dict) -> dict:
    return {"type": typ, "timestamp": at, "payload": payload}


def _message(role: str, text: str) -> dict:
    return {"type": "message", "role": role,
            "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}


def _messages(outbox: Outbox) -> list[dict]:
    return [json.loads(row["event_json"]) for row in outbox.pending(100)
            if json.loads(row["event_json"])["content"]["fact_kind"] == "message.observed"]


def test_scoped_backfill_revises_only_selected_message_without_moving_cursor(tmp_path: Path):
    root = tmp_path / "sessions"
    root.mkdir()
    log = root / "one.jsonl"
    _write_records(log, [
        _record("session_meta", "2026-09-20T00:00:00Z", {"id": "one", "cwd": str(tmp_path / "project")}),
        _record("event_msg", "2026-09-20T00:00:01Z", {"type": "task_started", "turn_id": "turn-one"}),
        _record("response_item", "2026-09-20T00:00:02Z", _message("user", "older request")),
        _record("event_msg", "2026-09-20T00:00:03Z", {"type": "task_complete", "turn_id": "turn-one"}),
        _record("event_msg", "2026-09-21T00:00:01Z", {"type": "task_started", "turn_id": "turn-two"}),
        _record("response_item", "2026-09-21T00:00:02Z", _message("assistant", "selected answer")),
    ])
    source = {"id": str(uuid4()), "agent": "codex", "root": str(root),
              "content_policy": "stats_only"}
    device_id = str(uuid4())
    outbox = Outbox(tmp_path / "outbox.db")
    scan_codex(source, outbox, device_id)
    initial = _messages(outbox)
    assert len(initial) == 2
    assert all(item["content"]["payload"]["body"] is None for item in initial)
    original_cursor = outbox.cursor(source["id"], "one.jsonl")["offset"]

    with pytest.raises(ValueError, match="full_content"):
        outbox.request_content_backfill([source])
    source["content_policy"] = "full_content"
    kwargs = {"start_at": "2026-09-21T00:00:00Z", "end_at": "2026-09-22T00:00:00Z",
              "native_session_ids": ["one"], "cwd_prefix": str(tmp_path / "project")}
    preview = outbox.preview_content_backfill([source], **kwargs)
    assert preview["eligible_messages"] == 1
    request = outbox.request_content_backfill([source], **kwargs)
    assert request["queued_source_ids"] == [source["id"]]
    assert process_content_backfill({"collector_id": device_id, "sources": [source]}, outbox,
                                    max_units=1)["queued"] == 1
    assert outbox.content_backfill_status()[0]["status"] == "completed"
    assert outbox.cursor(source["id"], "one.jsonl")["offset"] == original_cursor
    revised = _messages(outbox)
    assert len(revised) == 3
    answer = next(item for item in revised if item["content"]["payload"].get("body") == "selected answer")
    assert answer["content"]["revision_key"] == "collector:2"
    assert answer["content"]["supersedes_event_id"] == initial[1]["event_id"]
    assert answer["content"]["payload"]["native_turn_id"] == "turn-two"
    assert not any(item["content"]["payload"].get("body") == "older request" for item in revised)
    assert process_content_backfill({"collector_id": device_id, "sources": [source]}, outbox)["queued"] == 0


def test_revisions_are_monotonic_even_when_content_returns_to_original(tmp_path: Path):
    root = tmp_path / "sessions"
    root.mkdir()
    log = root / "one.jsonl"
    source = {"id": str(uuid4()), "agent": "codex", "root": str(root),
              "content_policy": "full_content"}
    device_id = str(uuid4())
    outbox = Outbox(tmp_path / "outbox.db")

    def set_text(text: str) -> None:
        _write_records(log, [
            _record("session_meta", "2026-09-20T00:00:00Z", {"id": "one", "cwd": str(tmp_path)}),
            _record("response_item", "2026-09-20T00:00:01Z", _message("user", text)),
        ])

    set_text("version A")
    assert scan_codex(source, outbox, device_id) == 2
    for text in ("version B", "version A"):
        set_text(text)
        outbox.request_content_backfill([source])
        process_content_backfill({"collector_id": device_id, "sources": [source]}, outbox)
    versions = _messages(outbox)
    assert [v["content"]["revision_key"] for v in versions] == ["1", "collector:2", "collector:3"]
    assert [v["content"]["payload"]["body"] for v in versions] == ["version A", "version B", "version A"]
    assert versions[2]["content"]["supersedes_event_id"] == versions[1]["event_id"]
    assert len({v["event_id"] for v in versions}) == 3


def test_codex_turn_cursor_and_cached_input_component(tmp_path: Path):
    root = tmp_path / "sessions"
    root.mkdir()
    log = root / "one.jsonl"
    _write_records(log, [
        _record("session_meta", "2026-09-20T00:00:00Z", {"id": "one", "cwd": str(tmp_path)}),
        _record("event_msg", "2026-09-20T00:00:01Z", {"type": "task_started", "turn_id": "turn-one"}),
        _record("response_item", "2026-09-20T00:00:02Z", _message("user", "prompt")),
    ])
    source = {"id": str(uuid4()), "agent": "codex", "root": str(root),
              "content_policy": "full_content"}
    device_id = str(uuid4())
    outbox = Outbox(tmp_path / "outbox.db")
    scan_codex(source, outbox, device_id)
    assert outbox.cursor(source["id"], "one.jsonl")["turn_id"] == "turn-one"
    _write_records(log, [
        _record("response_item", "2026-09-20T00:00:03Z", _message("assistant", "reply")),
        _record("event_msg", "2026-09-20T00:00:04Z", {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 100, "output_tokens": 10,
                                  "cached_input_tokens": 80, "total_tokens": 110}}}),
        _record("event_msg", "2026-09-20T00:00:05Z", {"type": "task_complete", "turn_id": "turn-one"}),
    ], append=True)
    scan_codex(source, outbox, device_id)
    assert [m["content"]["payload"]["native_turn_id"] for m in _messages(outbox)] == ["turn-one", "turn-one"]
    assert outbox.cursor(source["id"], "one.jsonl")["turn_id"] is None
    usage = [json.loads(row["event_json"])["content"]["payload"] for row in outbox.pending(100)
             if json.loads(row["event_json"])["content"]["fact_kind"] == "usage.observed"]
    cached = next(item for item in usage if item["usage_key"] == "codex_cached_input")
    assert cached["quantity_semantics"] == "cumulative_snapshot"
    assert cached["total_tokens"] == 80


def test_hermes_scoped_backfill_revises_historical_body(tmp_path: Path):
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE sessions(id TEXT,source TEXT,started_at TEXT,ended_at TEXT,cwd TEXT,model TEXT,title TEXT)")
        db.execute("CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT,timestamp TEXT)")
        db.execute("""CREATE TABLE session_model_usage(session_id TEXT,model TEXT,billing_provider TEXT,
            billing_base_url TEXT,billing_mode TEXT,task TEXT,input_tokens INTEGER,output_tokens INTEGER,
            cache_read_tokens INTEGER,reasoning_tokens INTEGER)""")
        db.execute("INSERT INTO sessions VALUES('h-one','test','2026-09-20T00:00:00Z',NULL,?,'test','Named')",
                   (str(tmp_path),))
        db.execute("INSERT INTO sessions VALUES('h-blocked','test','2026-09-20T00:00:00Z',NULL,?,'test','Deleted')",
                   (str(tmp_path),))
        db.execute("INSERT INTO messages VALUES(1,'h-one','user','historical Hermes text','2026-09-20T00:00:01Z')")
        db.execute("INSERT INTO messages VALUES(2,'h-blocked','user','must not enter outbox','2026-09-20T00:00:02Z')")
        db.commit()
    source = {"id": str(uuid4()), "agent": "hermes", "root": str(db_path),
              "content_policy": "stats_only"}
    device_id = str(uuid4())
    outbox = Outbox(tmp_path / "outbox.db")
    scan_hermes(source, outbox, device_id)
    old_id = next(message["event_id"] for message in _messages(outbox)
                  if message["content"]["native_session_id"] == "h-one")
    source["content_policy"] = "full_content"
    blocked = {source["id"]: ["h-blocked"]}
    assert outbox.preview_content_backfill([source], blocked_native_session_ids=blocked)["eligible_messages"] == 1
    outbox.request_content_backfill([source], blocked_native_session_ids=blocked)
    assert process_content_backfill({"collector_id": device_id, "sources": [source]}, outbox)["queued"] == 1
    messages = _messages(outbox)
    assert len(messages) == 3
    revised = next(message for message in messages
                   if message["content"]["native_session_id"] == "h-one"
                   and message["content"]["payload"].get("body"))
    assert revised["content"]["supersedes_event_id"] == old_id
    assert revised["content"]["payload"]["body"] == "historical Hermes text"
    assert not any(message["content"]["payload"].get("body") == "must not enter outbox" for message in messages)
    assert outbox.cursor(source["id"], "session:h-one")["offset"] == 1


def test_file_evidence_requires_paired_successful_apply_patch(tmp_path: Path):
    root = tmp_path / "sessions"
    root.mkdir()
    log = root / "one.jsonl"
    _write_records(log, [
        _record("session_meta", "2026-09-20T00:00:00Z", {"id": "one", "cwd": str(tmp_path)}),
        _record("event_msg", "2026-09-20T00:00:01Z", {"type": "task_started", "turn_id": "turn-one"}),
        _record("response_item", "2026-09-20T00:00:02Z", {
            "type": "custom_tool_call", "name": "apply_patch", "call_id": "call-good",
            "input": "*** Begin Patch\n*** Add File: report.txt\n+report\n*** End Patch"}),
    ])
    source = {"id": str(uuid4()), "agent": "codex", "root": str(root),
              "content_policy": "stats_only"}
    device_id = str(uuid4())
    outbox = Outbox(tmp_path / "outbox.db")
    scan_codex(source, outbox, device_id)
    assert json.loads(outbox.cursor(source["id"], "one.jsonl")["patch_calls_json"])["call-good"]["turn_id"] == "turn-one"
    _write_records(log, [
        _record("response_item", "2026-09-20T00:00:03Z", {
            "type": "custom_tool_call_output", "call_id": "call-good",
            "output": "Exit code: 0\nOutput:\nSuccess. Updated the following files:\nA report.txt\n"}),
        _record("response_item", "2026-09-20T00:00:04Z", {
            "type": "custom_tool_call_output", "call_id": "unpaired",
            "output": "Exit code: 0\nOutput:\nSuccess. Updated the following files:\nA fake.txt\n"}),
        _record("response_item", "2026-09-20T00:00:05Z", {
            "type": "custom_tool_call", "name": "apply_patch", "call_id": "call-failed",
            "input": "*** Begin Patch\n*** Update File: fake.txt\n@@\n-old\n+new\n*** End Patch"}),
        _record("response_item", "2026-09-20T00:00:06Z", {
            "type": "custom_tool_call_output", "call_id": "call-failed",
            "output": "Exit code: 1\nOutput:\nPatch failed\n"}),
    ], append=True)
    scan_codex(source, outbox, device_id)
    files = [json.loads(row["event_json"])["content"] for row in outbox.pending(100)
             if json.loads(row["event_json"])["content"]["fact_kind"] == "file.observed"]
    assert len(files) == 1
    assert files[0]["payload"]["native_path"] == str(tmp_path / "report.txt")
    assert files[0]["payload"]["relation"] == "created"
    assert files[0]["payload"]["operation_status"] == "succeeded"
    assert files[0]["payload"]["run_ref"] == "turn-one"
    assert files[0]["payload"].get("size_bytes") is None
    assert files[0]["payload"]["relative_path"] == "report.txt"
    assert files[0]["payload"]["root_ref"].startswith("cwd:")


def test_deleting_during_queued_backfill_scrubs_outbox_and_blocks_later_units(tmp_path: Path):
    root = tmp_path / "sessions"
    root.mkdir()
    for name, at, body in (("a.jsonl", "2026-09-20T00:00:00Z", "private first"),
                           ("b.jsonl", "2026-09-21T00:00:00Z", "private second")):
        _write_records(root / name, [
            _record("session_meta", at, {"id": "deleted-session", "cwd": str(tmp_path)}),
            _record("response_item", at, _message("user", body)),
        ])
    source = {"id": str(uuid4()), "agent": "codex", "root": str(root),
              "content_policy": "full_content"}
    device_id = str(uuid4())
    outbox = Outbox(tmp_path / "outbox.db")
    outbox.request_content_backfill([source])
    process_content_backfill({"collector_id": device_id, "sources": [source]}, outbox, max_units=1)
    assert any("private first" in row["event_json"] for row in outbox.pending())

    purged = outbox.block_session(source["id"], "deleted-session")
    assert purged["rewritten_pending"] == 1
    assert all("private" not in row["event_json"] for row in outbox.pending())
    for row in outbox.pending():
        Envelope.model_validate(json.loads(row["event_json"]))
    process_content_backfill({"collector_id": device_id, "sources": [source]}, outbox, max_units=1)
    assert all("private" not in row["event_json"] for row in outbox.pending())
    assert scan_codex(source, outbox, device_id) == 0
    assert outbox.is_blocked(source["id"], "deleted-session")
    assert outbox.content_backfill_status()[0]["status"] == "completed"


def test_metadata_migration_recovers_cached_and_patch_without_replaying_text(tmp_path: Path):
    root = tmp_path / "sessions"
    root.mkdir()
    log = root / "old.jsonl"
    _write_records(log, [
        _record("session_meta", "2026-09-20T00:00:00Z", {"id": "old", "cwd": str(tmp_path)}),
        _record("response_item", "2026-09-20T00:00:01Z", _message("user", "secret history")),
        _record("event_msg", "2026-09-20T00:00:02Z", {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 100, "output_tokens": 10,
                                  "cached_input_tokens": 80, "total_tokens": 110}}}),
        _record("response_item", "2026-09-20T00:00:03Z", {
            "type": "custom_tool_call", "name": "apply_patch", "call_id": "old-call",
            "input": "*** Begin Patch\n*** Add File: old.txt\n+data\n*** End Patch"}),
        _record("response_item", "2026-09-20T00:00:04Z", {
            "type": "custom_tool_call_output", "call_id": "old-call",
            "output": "Exit code: 0\nOutput:\nSuccess. Updated the following files:\nA old.txt\n"}),
    ])
    source = {"id": str(uuid4()), "agent": "codex", "root": str(root),
              "content_policy": "full_content"}
    device_id = str(uuid4())
    outbox = Outbox(tmp_path / "outbox.db")
    old_message = make_event(source["id"], "old", "message.observed", "legacy", "1", 100,
                             "2026-09-20T00:00:01Z", {"native_message_id": "legacy", "role": "user",
                             "input_origin": "human", "content_state": "stats_only",
                             "omission_reason": "source_policy", "body": None,
                             "source_text_char_count": 14, "finalized": True}, device_id, None)
    outbox.append(source["id"], "old.jsonl", log.stat().st_size,
                  f"{log.stat().st_dev}:{log.stat().st_ino}", "old", [old_message])
    prior_offset = outbox.cursor(source["id"], "old.jsonl")["offset"]
    assert backfill_codex_cached_input_history_once(source, outbox, device_id) == 5
    assert backfill_codex_cached_input_history_once(source, outbox, device_id) == 0
    assert outbox.cursor(source["id"], "old.jsonl")["offset"] == prior_offset
    events = [json.loads(row["event_json"]) for row in outbox.pending(100)]
    assert sorted(event["content"]["fact_kind"] for event in events) == [
        "file.observed", "message.observed", "usage.observed", "usage.observed",
        "usage.observed", "usage.observed"]
    assert all("secret history" not in row["event_json"] for row in outbox.pending(100))


def test_acked_body_compacts_but_fact_revision_dedupe_survives(tmp_path: Path):
    source_id = str(uuid4())
    device_id = str(uuid4())
    outbox_path = tmp_path / "outbox.db"
    outbox = Outbox(outbox_path)

    def message(text: str) -> dict:
        return make_event(source_id, "session", "message.observed", "message-1", "1", 1,
                          "2026-09-20T00:00:00Z", {"native_message_id": "message-1",
                          "role": "user", "input_origin": "human", "content_state": "full",
                          "body": text, "source_text_char_count": len(text), "finalized": True},
                          device_id, None)

    assert outbox.append(source_id, "log.jsonl", 100, "fingerprint", "session", [message("private old")]) == 1
    assert "private old" in outbox.pending()[0]["event_json"]
    outbox.settle([{"seq": 1, "status": "accepted", "receipt_id": "receipt"}], durable_ack=1)
    with outbox.connect() as db:
        row = db.execute("SELECT event_id,event_json,observation_json,state FROM outbox WHERE seq=1").fetchone()
        assert row["state"] == "acked"
        assert row["event_json"] == "{}"
        assert row["observation_json"] == "{}"
        first_id = row["event_id"]
    outbox = Outbox(outbox_path)
    assert outbox.append(source_id, "log.jsonl", 100, "fingerprint", "session", [message("private old")]) == 0
    assert outbox.append(source_id, "log.jsonl", 100, "fingerprint", "session", [message("private new")]) == 1
    revised = json.loads(outbox.pending()[0]["event_json"])
    assert revised["content"]["revision_key"] == "collector:2"
    assert revised["content"]["supersedes_event_id"] == first_id
    assert "private old" not in revised["content"]["payload"]["body"]
    outbox.settle([{"seq": 2, "status": "quarantined_pending", "receipt_id": "review",
                    "reason": "fixture"}], durable_ack=1)
    outbox.settle([{"seq": 2, "status": "quarantined_resolved", "receipt_id": "review"}],
                  durable_ack=1)
    with outbox.connect() as db:
        assert db.execute("SELECT event_json FROM outbox WHERE seq=2").fetchone()[0] == "{}"
        assert db.execute("SELECT COUNT(*) FROM deadletters").fetchone()[0] == 0


def test_startup_compacts_legacy_acked_body_before_backfill(tmp_path: Path):
    source_id = str(uuid4())
    device_id = str(uuid4())
    outbox_path = tmp_path / "outbox.db"
    outbox = Outbox(outbox_path)
    old = make_event(source_id, "session", "message.observed", "message-1", "1", 1,
                     "2026-09-20T00:00:00Z", {"native_message_id": "message-1",
                     "role": "user", "input_origin": "human", "content_state": "stats_only",
                     "omission_reason": "source_policy", "body": None,
                     "source_text_char_count": 10, "finalized": True}, device_id, None)
    outbox.append(source_id, "log.jsonl", 100, "fingerprint", "session", [old])
    with outbox.connect() as db:
        db.execute("DELETE FROM fact_revisions")  # simulate a pre-revision outbox
        db.execute("UPDATE outbox SET state='acked'")  # simulate ack before compaction existed
        db.commit()
    reopened = Outbox(outbox_path)
    with reopened.connect() as db:
        assert db.execute("SELECT event_json FROM outbox WHERE seq=1").fetchone()[0] == "{}"
    upgraded = make_event(source_id, "session", "message.observed", "message-1", "1", 1,
                          "2026-09-20T00:00:00Z", {"native_message_id": "message-1",
                          "role": "user", "input_origin": "human", "content_state": "full",
                          "body": "newly allowed", "source_text_char_count": 13,
                          "finalized": True}, device_id, None)
    assert reopened.append(source_id, "log.jsonl", 100, "fingerprint", "session", [upgraded]) == 1
    revision = json.loads(reopened.pending()[0]["event_json"])
    assert revision["content"]["supersedes_event_id"] == old["event_id"]


def test_codex_inherited_parent_meta_never_reassigns_child_facts(tmp_path: Path):
    root = tmp_path / "sessions"
    root.mkdir()
    child_cwd = tmp_path / "child-project"
    parent_cwd = tmp_path / "parent-project"
    log = root / "child.jsonl"
    _write_records(log, [
        _record("session_meta", "2026-09-20T00:00:00Z", {"id": "child", "cwd": str(child_cwd),
                                                         "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}}}),
        _record("session_meta", "2026-09-20T00:00:01Z", {"id": "parent", "cwd": str(parent_cwd)}),
        _record("event_msg", "2026-09-20T00:00:02Z", {"type": "task_started", "turn_id": "child-turn"}),
        _record("response_item", "2026-09-20T00:00:03Z", _message("assistant", "child answer")),
        _record("event_msg", "2026-09-20T00:00:04Z", {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 100, "output_tokens": 10,
                                  "cached_input_tokens": 80, "total_tokens": 110}}}),
    ])
    source = {"id": str(uuid4()), "agent": "codex", "root": str(root),
              "content_policy": "full_content"}
    device_id = str(uuid4())
    outbox = Outbox(tmp_path / "outbox.db")
    assert codex_primary_session_meta(log)["id"] == "child"
    assert outbox.preview_content_backfill([source], native_session_ids=["parent"])["eligible_messages"] == 0
    assert outbox.preview_content_backfill([source], native_session_ids=["child"])["eligible_messages"] == 1
    scan_codex(source, outbox, device_id)
    events = [json.loads(row["event_json"])["content"] for row in outbox.pending(100)]
    assert all(event["native_session_id"] == "child" for event in events)
    assert next(event for event in events if event["fact_kind"] == "session.observed")["payload"]["cwd"] == str(child_cwd)
    assert next(event for event in events if event["fact_kind"] == "message.observed")["payload"]["native_turn_id"] == "child-turn"
    counters = [event["payload"] for event in events if event["fact_kind"] == "usage.observed"]
    assert all(item["counter_id"] != "child" and item["counter_id"] == item["epoch_id"] for item in counters)
