from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest

from awb.collector import Outbox, make_event
from awb.db import Database
from awb.identity_repair import apply_staged_codex_identity_repair, stage_codex_identity_repair
from awb.projector import project


def _write_jsonl(path: Path, records: list[dict]) -> list[int]:
    offsets = []
    with path.open("wb") as stream:
        for record in records:
            offsets.append(stream.tell())
            stream.write((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
    return offsets


def test_staged_identity_repair_separates_child_and_preserves_existing_body(tmp_path: Path):
    root = tmp_path / "codex"
    root.mkdir()
    file = root / "child.jsonl"
    records = [
        {"timestamp": "2026-09-23T01:00:00Z", "type": "session_meta", "payload": {"id": "child", "cwd": "C:/child"}},
        {"timestamp": "2026-09-23T01:01:00Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "child first"}]}},
        {"timestamp": "2026-09-23T01:02:00Z", "type": "session_meta", "payload": {"id": "parent", "cwd": "C:/parent"}},
        {"timestamp": "2026-09-23T01:03:00Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "child second"}]}},
    ]
    offsets = _write_jsonl(file, records)
    db_path, outbox_path, config_path = tmp_path / "server.db", tmp_path / "outbox.db", tmp_path / "collector.json"
    db = Database(db_path)
    db.initialize()
    original_outbox = Outbox(outbox_path)
    device, source, hermes_source = str(uuid4()), str(uuid4()), str(uuid4())
    config_path.write_text(json.dumps({"collector_id": device, "sources": [
        {"id": source, "agent": "codex", "root": str(root), "content_policy": "full_content"},
        {"id": hermes_source, "agent": "hermes", "root": str(tmp_path / "hermes.db"),
         "content_policy": "stats_only"}]}), encoding="utf-8")
    with db.tx() as conn:
        conn.execute("INSERT INTO devices(id,name,os,environment,token_hash,registered_at) VALUES(?,?,?,?,?,?)",
                     (device, "Desk", "Windows", "local", str(uuid4()), "2026-09-20T00:00:00Z"))
        conn.execute("INSERT INTO sources(id,device_id,agent,profile,execution_surface,capability_json,created_at) VALUES(?,?,?,?,?,?,?)",
                     (source, device, "codex", "default", "local", '{"content_policy":"full_content"}', "2026-09-20T00:00:00Z"))
        conn.execute("INSERT INTO sources(id,device_id,agent,profile,execution_surface,capability_json,created_at) VALUES(?,?,?,?,?,?,?)",
                     (hermes_source, device, "hermes", "default", "local", '{"content_policy":"stats_only"}', "2026-09-20T00:00:00Z"))
        conn.execute("INSERT INTO sessions(id,source_id,native_id,agent,device_id) VALUES(?,?,?,?,?)",
                     ("hermes-session", hermes_source, "hermes-native", "hermes", device))
        conn.execute("""INSERT INTO messages(id,session_id,native_id,role,input_origin,body,content_state,event_id)
            VALUES(?,?,?,?,?,?,?,?)""", ("hermes-message", "hermes-session", "msg", "user", "human",
            "Hermes retained", "full", "hermes-event"))
        conn.execute("INSERT INTO streams(collector_id,epoch,durable_ack,projected_ack,server_epoch) VALUES(?,?,?,?,?)",
                     (device, original_outbox.epoch(), 1, 1, "server-epoch"))
    with closing(original_outbox.connect()) as conn:
        conn.execute("INSERT INTO cursors(source_id,locator,offset) VALUES(?,?,?)", (hermes_source, "state.db", 77))
        conn.execute("""INSERT INTO outbox(seq,event_id,source_id,event_json,observation_json,state)
            VALUES(?,?,?,?,?,'acked')""", (1, "hermes-event", hermes_source, "{}", "{}"))
        conn.commit()
    # Simulate the legacy parser assigning the child's later message to parent.
    polluted = make_event(source, "parent", "message.observed", str(offsets[3]), "1", offsets[3],
                          "2026-09-23T01:03:00Z", {"native_message_id": str(offsets[3]), "role": "user",
                          "input_origin": "human", "body": "child second", "content_state": "full",
                          "source_text_char_count": 12, "finalized": True}, device, None)
    with db.tx() as conn:
        conn.execute("""INSERT INTO events(event_id,body_hash,source_id,native_session_id,fact_kind,
            native_fact_id,revision_key,source_order,occurred_at,content_json,received_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (polluted["event_id"], polluted["body_hash"], source,
            "parent", "message.observed", str(offsets[3]), "1", offsets[3],
            "2026-09-23T01:03:00Z", json.dumps(polluted["content"]), "2026-09-23T01:03:00Z"))
        project(conn, polluted)
        parent_id = conn.execute("SELECT id FROM sessions WHERE native_id='parent'").fetchone()[0]
        conn.execute("INSERT INTO session_titles(session_id,user_title,updated_at) VALUES(?,?,?)",
                     (parent_id, "My saved title", "2026-09-23T01:03:00Z"))
    report = stage_codex_identity_repair(db_path, outbox_path, config_path, tmp_path / "stage", tmp_path / "backup")
    assert report["status"] == "staged_not_applied"
    assert report["body_salvage"]["restored"] == 1
    assert report["old_counts"]["messages"] == 1
    assert report["new_counts"]["messages"] == 2
    assert report["orphan_session_count"] == 1
    assert report["orphan_sessions"][0]["native_id"] == "parent"
    assert report["original_fingerprints"]["database"]["sha256"]
    with db.read() as original:
        assert original.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    with Database(Path(report["staged_database"])).read() as staged:
        rows = staged.execute("""SELECT s.native_id,m.body FROM messages m JOIN sessions s ON s.id=m.session_id
            WHERE s.agent='codex' ORDER BY m.source_order""").fetchall()
        assert [(row[0], row[1]) for row in rows] == [("child", None), ("child", "child second")]
        assert staged.execute("SELECT user_title FROM session_titles WHERE session_id=?", (parent_id,)).fetchone()[0] == "My saved title"
        assert staged.execute("SELECT archived FROM sessions WHERE id=?", (parent_id,)).fetchone()[0] == 1
        assert staged.execute("SELECT archived FROM sessions WHERE native_id='child'").fetchone()[0] == 0
        assert staged.execute("SELECT body FROM messages WHERE id='hermes-message'").fetchone()[0] == "Hermes retained"
    with closing(Outbox(Path(report["staged_outbox"])).connect()) as outbox:
        assert outbox.execute("SELECT COUNT(*) FROM cursors WHERE source_id=?", (source,)).fetchone()[0] == 1
    stale_wal = Path(str(db_path) + "-wal")
    stale_wal.write_bytes(b"uncheckpointed")
    with pytest.raises(ValueError, match="WAL/SHM"):
        apply_staged_codex_identity_repair(db_path, outbox_path, config_path,
                                           tmp_path / "stage", tmp_path / "backup")
    stale_wal.unlink()
    applied = apply_staged_codex_identity_repair(db_path, outbox_path, config_path,
                                                 tmp_path / "stage", tmp_path / "backup")
    assert applied["status"] == "applied"
    with db.read() as live:
        assert live.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3
    with Database(Path(applied["rollback_database"])).read() as rollback:
        assert rollback.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    with closing(Outbox(outbox_path).connect()) as outbox:
        assert outbox.execute("SELECT offset FROM cursors WHERE source_id=?", (hermes_source,)).fetchone()[0] == 77

    # A readable legacy body with no unique corrected source fact must stop
    # staging rather than silently dropping the owner's existing content.
    with db.tx() as conn:
        conn.execute("""INSERT INTO messages(id,session_id,native_id,role,input_origin,body,
            content_state,event_id,source_order,occurred_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""", ("unmatched", parent_id, "missing-raw-offset", "user",
            "human", "important old text", "full", "legacy-unmatched", 999,
            "2026-09-23T02:00:00Z"))
    with pytest.raises(ValueError, match="uniquely preserve"):
        stage_codex_identity_repair(db_path, outbox_path, config_path,
                                    tmp_path / "stage-unmatched", tmp_path / "backup-unmatched")
