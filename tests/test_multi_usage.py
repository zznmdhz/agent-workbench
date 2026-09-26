import json
import sqlite3
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from awb.api import create_app
from awb.db import Database
from awb.multi_usage import dashboard, session_requests, sync_claude_usage


def _claude(path: Path, message_id: str, *, output: int, final: bool, at: str = "2026-01-05T12:00:00Z"):
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"type": "assistant", "timestamp": at, "sessionId": "claude-session", "cwd": "C:/work/example",
           "message": {"id": message_id, "model": "claude-test", "stop_reason": "end_turn" if final else None,
                       "usage": {"input_tokens": 2, "cache_read_input_tokens": 5,
                                 "cache_creation_input_tokens": 3, "output_tokens": output}}}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row) + "\n")


def _epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _hermes(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY,title TEXT,cwd TEXT)")
        conn.execute("""CREATE TABLE session_model_usage(session_id TEXT,model TEXT,task TEXT,
            api_call_count INTEGER,input_tokens INTEGER,output_tokens INTEGER,
            cache_read_tokens INTEGER,cache_write_tokens INTEGER,first_seen REAL,last_seen REAL)""")
        conn.execute("INSERT INTO sessions VALUES('h1','Hermes project','C:/work/hermes')")
        conn.executemany("INSERT INTO session_model_usage VALUES(?,?,?,?,?,?,?,?,?,?)", [
            ("h1", "hermes-test", "", 2, 10, 7, 20, 5,
             _epoch("2026-01-05T00:00:00Z"), _epoch("2026-01-05T00:02:00Z")),
            ("h1", "hermes-test", "approval", 3, 20, 10, 30, 0,
             _epoch("2026-01-31T23:00:00Z"), _epoch("2026-02-02T01:00:00Z")),
        ])


def test_claude_dedup_and_hermes_boundary_in_long_range(tmp_path: Path):
    codex = tmp_path / "codex"
    (codex / "sessions").mkdir(parents=True)
    claude = tmp_path / "claude"
    transcript = claude / "projects" / "project" / "session.jsonl"
    _claude(transcript, "msg-1", output=1, final=False)
    _claude(transcript, "msg-1", output=4, final=True)
    _claude(transcript, "msg-1", output=2, final=True)
    hermes = tmp_path / "hermes" / "state.db"
    _hermes(hermes)
    db = Database(tmp_path / "workbench.db")
    db.initialize()

    january = dashboard(db, "2026-01-01", "2026-01-31", "UTC", codex_root=codex,
                        claude_root=claude, hermes_path=hermes)
    assert january["summary"]["requests"] == 3  # one Claude request, two Hermes calls
    assert january["summary"]["total_tokens"] == 56  # Claude 14 + Hermes 42
    assert january["summary"]["input_tokens"] == 45
    assert january["sources"]["claude"]["requests"] == 1
    assert january["sources"]["hermes"]["partial_rows"] == 1
    assert sum(row["total_tokens"] for row in january["trend"]) == 14
    assert january["unattributed_tokens"] == 42
    assert january["trend_granularity"] == "day"

    long_range = dashboard(db, "2026-01-01", "2026-09-26", "UTC", codex_root=codex,
                           claude_root=claude, hermes_path=hermes)
    assert long_range["summary"]["total_tokens"] == 116
    assert long_range["sources"]["hermes"]["partial_rows"] == 0
    assert long_range["trend_granularity"] == "month"
    assert len(long_range["trend"]) == 9
    assert long_range["trend"][0]["period"] == "2026-01-01"

    filtered = dashboard(db, "2026-01-01", "2026-09-26", "UTC", agent="claude",
                         codex_root=codex, claude_root=claude, hermes_path=hermes)
    assert filtered["summary"]["total_tokens"] == 14
    detail = session_requests(db, "claude", "claude-session", "2026-01-01", "2026-09-26", "UTC")
    assert detail["count"] == 1
    assert detail["items"][0]["cache_creation_tokens"] == 3
    assert session_requests(db, "hermes", "h1", "2026-01-01", "2026-01-31", "UTC",
                            hermes_path=hermes)["count"] == 1


def test_claude_changed_file_rebuilds_without_double_counting(tmp_path: Path):
    root = tmp_path / "claude"
    transcript = root / "projects" / "p" / "session.jsonl"
    _claude(transcript, "msg-1", output=4, final=True)
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    assert sync_claude_usage(db, root)["updated_files"] == 1
    assert sync_claude_usage(db, root)["updated_files"] == 0
    _claude(transcript, "msg-2", output=6, final=True)
    assert sync_claude_usage(db, root)["updated_files"] == 1
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mvp_claude_requests").fetchone()[0] == 2


def test_claude_zero_final_snapshot_does_not_keep_provisional_usage(tmp_path: Path):
    root = tmp_path / "claude"
    transcript = root / "projects" / "p" / "session.jsonl"
    _claude(transcript, "msg-1", output=4, final=False)
    row = {"type": "assistant", "timestamp": "2026-01-05T12:00:01Z", "sessionId": "claude-session",
           "message": {"id": "msg-1", "model": "claude-test", "stop_reason": "end_turn",
                       "usage": {"input_tokens": 0, "output_tokens": 0}}}
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row) + "\n")
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    sync_claude_usage(db, root)
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mvp_claude_requests").fetchone()[0] == 0


def test_web_usage_query_accepts_january_to_september_and_agent_filter(tmp_path: Path, monkeypatch):
    codex = tmp_path / "codex"
    (codex / "sessions").mkdir(parents=True)
    claude = tmp_path / "claude"
    _claude(claude / "projects" / "p" / "session.jsonl", "msg-1", output=4, final=True)
    hermes = tmp_path / "hermes" / "state.db"
    _hermes(hermes)
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("HERMES_STATE_DB", str(hermes))
    client = TestClient(create_app(tmp_path / "web.db", desktop_mode=True),
                        base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    assert client.post("/auth/setup", json={"password": "long-enough-123",
                                             "confirmation": "long-enough-123"}).status_code == 200
    response = client.get("/v1/mvp/usage", params={"day": "2026-01-01", "through": "2026-09-26",
                                                     "tz": "UTC"})
    assert response.status_code == 200
    assert response.json()["trend_granularity"] == "month"
    assert response.json()["summary"]["total_tokens"] == 116
    hermes_only = client.get("/v1/mvp/usage", params={"day": "2026-01-01", "through": "2026-09-26",
                                                       "tz": "UTC", "agent": "hermes"}).json()
    assert hermes_only["summary"]["total_tokens"] == 102
    assert hermes_only["sources"]["claude"]["total_tokens"] == 14
    detail = client.get("/v1/mvp/usage/sessions/hermes/h1/requests",
                        params={"day": "2026-01-01", "through": "2026-09-26", "tz": "UTC"})
    assert detail.status_code == 200
    assert detail.json()["precision"] == "session_model_aggregate"


def test_missing_claude_cache_field_is_reported_not_invented(tmp_path: Path):
    codex = tmp_path / "codex"
    (codex / "sessions").mkdir(parents=True)
    claude = tmp_path / "claude"
    transcript = claude / "projects" / "p" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    with transcript.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "assistant", "timestamp": "2026-01-05T12:00:00Z",
                                 "sessionId": "s", "message": {"id": "m", "model": "claude-test",
                                 "usage": {"input_tokens": 3, "cache_read_input_tokens": 5,
                                           "output_tokens": 7}}}) + "\n")
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    result = dashboard(db, "2026-01-01", "2026-01-31", "UTC", codex_root=codex,
                       claude_root=claude, hermes_path=tmp_path / "missing.db")
    assert result["sources"]["claude"]["missing_cache_read"] == 0
    assert result["sources"]["claude"]["missing_cache_write"] == 1
    assert result["summary"]["total_tokens"] == 15
