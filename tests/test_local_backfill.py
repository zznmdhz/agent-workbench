"""Owner-selected local history must stay inside authorized source policy."""

import json
from pathlib import Path

import pytest

from awb.collector import Outbox
from awb.db import Database
from awb.local import preview_content_backfill, request_content_backfill


def _configured_db(tmp_path: Path, policy: str = "full_content") -> tuple[Database, str]:
    db = Database(tmp_path / "agent-workbench.db")
    db.initialize()
    device_id = "test-device"
    source_id = "test-source"
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO devices(id,name,os,environment,token_hash,registered_at) "
            "VALUES(?,?,?,?,?,?)",
            (device_id, "Test", "Windows", "test", "hash", "2026-09-26T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO sources(id,device_id,agent,profile,execution_surface,capability_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (source_id, device_id, "codex", "default", "local",
             json.dumps({"content_policy": policy}), "2026-09-26T00:00:00Z"),
        )
    (tmp_path / "collector.json").write_text(json.dumps({
        "collector_id": device_id,
        "sources": [{"id": source_id, "agent": "codex", "root": str(tmp_path),
                     "content_policy": policy}],
    }), encoding="utf-8")
    return db, source_id


def test_local_history_preview_and_request_pass_exact_scope(monkeypatch, tmp_path: Path):
    db, source_id = _configured_db(tmp_path)
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO tombstones(source_id,native_session_id,deleted_at,policy_version) "
            "VALUES(?,?,?,1)",
            (source_id, "deleted-native", "2026-09-26T00:00:00Z"),
        )
    calls = []

    def fake_preview(self, sources, **scope):
        calls.append(("preview", sources, scope))
        return {"eligible_messages": 4}

    def fake_request(self, sources, **scope):
        calls.append(("request", sources, scope))
        return {"status": "queued"}

    monkeypatch.setattr(Outbox, "preview_content_backfill", fake_preview, raising=False)
    monkeypatch.setattr(Outbox, "request_content_backfill", fake_request, raising=False)
    scope = {"start_at": "2026-09-01T00:00:00Z", "end_at": "2026-09-02T00:00:00Z",
             "native_session_ids": ["native-1"], "cwd_prefix": None}
    assert preview_content_backfill(db, [source_id], **scope)["eligible_messages"] == 4
    assert request_content_backfill(db, [source_id], **scope)["status"] == "queued"
    assert [call[0] for call in calls] == ["preview", "request"]
    expected = {**scope, "blocked_native_session_ids": {source_id: ["deleted-native"]}}
    assert all(call[1][0]["id"] == source_id and call[2] == expected for call in calls)


def test_local_history_rejects_unselected_or_stats_only_source(tmp_path: Path):
    db, source_id = _configured_db(tmp_path, policy="stats_only")
    with pytest.raises(ValueError, match="full-content"):
        request_content_backfill(db, [source_id])
    with pytest.raises(ValueError, match="full-content"):
        preview_content_backfill(db, [source_id])
    with pytest.raises(ValueError, match="distinct"):
        request_content_backfill(db, [source_id, source_id])
    with pytest.raises(ValueError, match="full-content"):
        request_content_backfill(db, ["unknown-source"])
