import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from awb.api import create_app
from awb.db import Database
from awb.multi_usage import (
    _codex_titles,
    _heatmap_keys,
    dashboard,
    hermes_db_path,
    session_requests,
    sync_claude_usage,
)


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
    assert sum(row["total_tokens"] for row in january["heatmap"]) == 14
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
    assert client.get("/auth/me").status_code == 200
    response = client.get("/v1/mvp/usage", params={"day": "2026-01-01", "through": "2026-09-26",
                                                     "tz": "UTC"})
    assert response.status_code == 200
    assert response.json()["trend_granularity"] == "month"
    assert response.json()["summary"]["total_tokens"] == 116
    assert client.get("/v1/mvp/usage", params={"day": "2026-01-01", "through": "2026-01-01",
                                                  "heatmap_view": "invalid"}).status_code == 422
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


def test_heatmap_periods_cover_leap_year_and_custom_months():
    assert len(_heatmap_keys(date(2028, 6, 1), date(2028, 6, 1), "year")[0]) == 366
    assert len(_heatmap_keys(date(2027, 2, 1), date(2027, 2, 1), "month")[0]) == 28
    assert len(_heatmap_keys(date(2028, 2, 1), date(2028, 2, 1), "month")[0]) == 29
    assert len(_heatmap_keys(date(2026, 4, 1), date(2026, 4, 1), "month")[0]) == 30
    assert len(_heatmap_keys(date(2026, 1, 1), date(2026, 1, 1), "month")[0]) == 31
    week, grain = _heatmap_keys(date(2026, 12, 31), date(2026, 12, 31), "week")
    assert (len(week), week[0], week[-1], grain) == (7, "2026-12-28", "2027-01-03", "day")
    hours, grain = _heatmap_keys(date(2026, 1, 1), date(2026, 1, 1), "day")
    assert (len(hours), hours[0], hours[-1], grain) == (24, "2026-01-01T00", "2026-01-01T23", "hour")
    months, grain = _heatmap_keys(date(2026, 1, 15), date(2027, 2, 2), "custom")
    assert (len(months), months[0], months[-1], grain) == (14, "2026-01-01", "2027-02-01", "month")


def test_heatmap_hong_kong_midnight_and_agent_model_filter(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    with db.tx() as conn:
        conn.executemany("""INSERT INTO mvp_usage_requests
            (request_id,native_session_id,occurred_at,model,input_tokens,cached_input_tokens,
             output_tokens,source_file,source_offset) VALUES(?,?,?,?,?,?,?,?,?)""", [
            ("before", "a", "2026-12-31T15:59:00Z", "model-a", 4, 0, 1, "sample", 1),
            ("after", "a", "2026-12-31T16:00:00Z", "model-a", 10, 3, 2, "sample", 2),
            ("other", "b", "2026-12-31T16:01:00Z", "model-b", 7, 0, 3, "sample", 3),
        ])
    result = dashboard(db, "2027-01-01", "2027-01-01", "Asia/Hong_Kong", agent="codex",
                       model="model-a", heatmap_view="day", sync=False,
                       codex_root=tmp_path / "missing", hermes_path=tmp_path / "missing.db")
    assert result["summary"]["total_tokens"] == 12
    assert result["session_count"] == 1
    assert len(result["heatmap"]) == 24
    assert result["heatmap"][0]["total_tokens"] == 12
    assert sum(row["total_tokens"] for row in result["heatmap"]) == 12
    assert result["sessions"][0]["title"] == "未命名会话（a）"


def test_codex_titles_use_state_name_and_keep_complete_title(tmp_path: Path):
    (tmp_path / "session_index.jsonl").write_text(
        json.dumps({"id": "a", "thread_name": "Index title"}) + "\n[]\ninvalid\n", encoding="utf-8")
    title = "中文标题" * 40
    with sqlite3.connect(tmp_path / "state_5.sqlite") as conn:
        conn.execute("CREATE TABLE threads(id TEXT,name TEXT)")
        conn.execute("INSERT INTO threads VALUES(?,?)", ("a", title))
    assert _codex_titles(tmp_path)["a"] == title


def test_hermes_default_path_for_mac_and_windows(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("HERMES_STATE_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("awb.multi_usage.sys.platform", "darwin")
    assert hermes_db_path() == tmp_path / ".hermes" / "state.db"
    monkeypatch.setattr("awb.multi_usage.sys.platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    assert hermes_db_path() == tmp_path / "appdata" / "hermes" / "state.db"
    monkeypatch.setenv("HERMES_STATE_DB", str(tmp_path / "override.db"))
    assert hermes_db_path() == tmp_path / "override.db"
