from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from awb.api import create_app
from awb.auth import initialize_owner
from awb.backup import create_backup, restore_backup
from awb.codec import ack_prefix, cumulative_deltas, union_ms
from awb.collector import Outbox, make_event, scan_codex
from awb.filecheck import check_local, safe_relative
from awb.handoff import create_handoff, link_continuation


def owner_client(tmp_path: Path) -> tuple[TestClient, str]:
    app = create_app(tmp_path / "server.db")
    initialize_owner(app.state.db, "correct-horse-battery-staple")
    client = TestClient(app)
    response = client.post("/auth/login", json={"password": "correct-horse-battery-staple"})
    assert response.status_code == 200
    return client, response.json()["csrf"]


def test_batch_retry_conflict_gap_and_tombstone(tmp_path: Path):
    client, csrf = owner_client(tmp_path)
    code = client.post("/v1/pairing-codes", headers={"x-awb-csrf": csrf}).json()["code"]
    pair = client.post("/v1/devices/pair", json={"code": code, "name": "fixture", "os": "Windows", "environment": "test"}).json()
    collector = pair["collector_id"]
    token = pair["token"]
    source = str(uuid4())
    response = client.post("/v1/sources/register", headers={"x-awb-csrf": csrf}, json={
        "id": source, "device_id": collector, "agent": "codex", "profile": "test", "content_policy": "full_content"})
    assert response.status_code == 200, response.text
    event = make_event(source, "s-1", "message.observed", "m-1", "1", 1, "2026-09-23T01:00:00Z",
                       {"native_message_id": "m-1", "role": "user", "input_origin": "human",
                        "content_state": "full", "body": "hello 世界", "source_text_char_count": 8,
                        "finalized": True}, collector, None)
    epoch = str(uuid4())
    batch = {"batch_id": str(uuid4()), "collector_id": collector, "outbox_epoch": epoch,
             "entries": [{"seq": 1, "observation": {"observed_at": "2026-09-23T01:01:00Z",
                                            "adapter_version": "test", "source_locator": {}}, "event": event}]}
    headers = {"Authorization": "Bearer " + token}
    first = client.post("/v1/ingest/batches", headers=headers, json=batch)
    assert first.status_code == 200, first.text
    assert first.json()["durable_ack_seq"] == 1
    retry = client.post("/v1/ingest/batches", headers=headers, json=batch)
    assert retry.status_code == 200
    assert client.get("/v1/search?q=hello").json()["items"][0]["excerpt"] == "hello 世界"
    session = client.get("/v1/sessions").json()["items"][0]
    delete = client.request("DELETE", f"/v1/sessions/{session['id']}/content", headers={"x-awb-csrf": csrf},
                            json={"confirmation": session["id"]})
    assert delete.status_code == 200, delete.text
    assert client.get("/v1/search?q=hello").json()["items"] == []
    another = make_event(source, "s-1", "message.observed", "m-2", "1", 2, "2026-09-23T01:02:00Z",
                         {"native_message_id": "m-2", "role": "user", "input_origin": "human",
                          "content_state": "full", "body": "resurrect", "source_text_char_count": 9,
                          "finalized": True}, collector, None)
    batch["batch_id"] = str(uuid4())
    batch["entries"] = [{"seq": 2, "observation": batch["entries"][0]["observation"], "event": another}]
    ignored = client.post("/v1/ingest/batches", headers=headers, json=batch)
    assert ignored.json()["receipts"][0]["status"] == "ignored_tombstoned"
    assert ignored.json()["durable_ack_seq"] == 2
    batch["batch_id"] = str(uuid4())
    batch["entries"][0]["seq"] = 2
    batch["entries"][0]["event"] = event
    assert client.post("/v1/ingest/batches", headers=headers, json=batch).status_code == 409


def test_outbox_repeated_scan_has_no_sequence_holes(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    log = sessions / "sample.jsonl"
    with log.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "session_meta", "timestamp": "2026-09-23T01:00:00Z",
                            "payload": {"id": "s1", "cwd": "/tmp/test"}}) + "\n")
        f.write(json.dumps({"type": "response_item", "timestamp": "2026-09-23T01:00:01Z",
                            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}}) + "\n")
    queue = Outbox(tmp_path / "outbox.db")
    assert Outbox(tmp_path / "outbox.db").epoch() == queue.epoch()
    source = {"id": str(uuid4()), "agent": "codex", "root": str(sessions), "content_policy": "stats_only"}
    device = str(uuid4())
    assert scan_codex(source, queue, device) == 2
    assert scan_codex(source, queue, device) == 0
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "response_item", "timestamp": "2026-09-23T01:00:02Z",
                            "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "world"}]}}) + "\n")
    assert scan_codex(source, queue, device) == 1
    assert [row["seq"] for row in queue.pending()] == [1, 2, 3]
    assert all("hello" not in row["event_json"] for row in queue.pending())


def test_codex_token_components_backfill_without_duplicate_totals(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    log = sessions / "sample.jsonl"
    with log.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "session_meta", "timestamp": "2026-09-23T01:00:00Z",
                            "payload": {"id": "s1", "cwd": "/tmp/test"}}) + "\n")
        for index, (input_tokens, output_tokens) in enumerate(((100, 20), (130, 35))):
            f.write(json.dumps({"type": "event_msg", "timestamp": f"2026-09-23T01:00:0{index+1}Z",
                                "payload": {"type": "token_count", "info": {"total_token_usage": {
                                    "input_tokens": input_tokens, "output_tokens": output_tokens,
                                    "total_tokens": input_tokens + output_tokens}}}}) + "\n")
    queue = Outbox(tmp_path / "outbox.db")
    source = {"id": str(uuid4()), "agent": "codex", "root": str(sessions), "content_policy": "stats_only"}
    device = str(uuid4())
    assert scan_codex(source, queue, device) == 7
    queue.backfill_codex_token_components_once([source["id"]])
    assert scan_codex(source, queue, device) == 0
    assert len(queue.pending()) == 7
    queue.backfill_codex_token_components_once([source["id"]])
    assert scan_codex(source, queue, device) == 0


def test_stat_primitives_do_not_invent_daily_usage():
    assert union_ms([(0, 10), (5, 20), (30, 40)]) == 30
    assert ack_prefix(0, {1: "accepted", 2: "quarantined_pending", 3: "accepted"}) == 1
    dated, loose = cumulative_deltas([
        {"source_order": 1, "source_time": "2026-09-23T23:00:00+08:00", "total_tokens": 100},
        {"source_order": 2, "source_time": "2026-09-24T01:00:00+08:00", "total_tokens": 130},
    ], "Asia/Hong_Kong")
    assert dated == []
    assert {x["reason"] for x in loose} == {"opening_balance", "cross_day_interval"}


def test_file_check_stays_inside_registered_root_and_backup_restores(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "inside.txt").write_text("hello", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    assert check_local(root, "inside.txt", "sha256")["status"] == "exists"
    assert check_local(root, "../outside.txt", "sha256")["status"] == "inaccessible"
    try:
        safe_relative("C:/outside.txt")
    except ValueError:
        pass
    else:
        raise AssertionError("Windows absolute path accepted")
    client, _ = owner_client(tmp_path)
    original = client.app.state.db.path
    archive = tmp_path / "backup.zip"
    create_backup(original, archive)
    restored = restore_backup(archive, tmp_path / "restored")
    assert restored.is_file()
    assert create_app(restored).state.db.path == restored


def test_handoff_is_frozen_and_link_requires_target_device(tmp_path: Path):
    client, _ = owner_client(tmp_path)
    db = client.app.state.db
    source_device, target_device = str(uuid4()), str(uuid4())
    source_id, target_source = str(uuid4()), str(uuid4())
    source_session, target_session = str(uuid4()), str(uuid4())
    with db.tx() as conn:
        for ident in (source_device, target_device):
            conn.execute("INSERT INTO devices(id,name,os,environment,token_hash,registered_at) VALUES(?,?,?,?,?,?)",
                         (ident, ident, "test", ident, ident, "2026-09-23T00:00:00Z"))
        for ident, device in ((source_id, source_device), (target_source, target_device)):
            conn.execute("""INSERT INTO sources(id,device_id,agent,profile,capability_json,created_at)
                VALUES(?,?,?,?,?,?)""", (ident, device, "codex", "default",
                                          '{"content_policy":"full_content"}', "2026-09-23T00:00:00Z"))
        for ident, origin, device in ((source_session, source_id, source_device),
                                       (target_session, target_source, target_device)):
            conn.execute("INSERT INTO sessions(id,source_id,native_id,agent,device_id) VALUES(?,?,?,?,?)",
                         (ident, origin, ident, "codex", device))
        conn.execute("""INSERT INTO messages(id,session_id,native_id,role,input_origin,body,
            content_state,event_id) VALUES(?,?,?,?,?,?,?,?)""",
                     (str(uuid4()), source_session, "m1", "user", "human", "Please continue",
                      "full", "fixture-event"))
    result = create_handoff(db, source_session, target_device, tmp_path / "handoffs")
    archive = tmp_path / "handoffs" / (result["handoff_id"] + ".zip")
    assert archive.is_file()
    assert link_continuation(db, result["handoff_id"], target_session)["status"] == "linked"
