from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from awb.api import create_app
from awb.auth import initialize_owner
from awb.collector import make_event
from awb.local import prepare_local_collector
from awb.projector import project
from awb.stats import calculate


def test_local_setup_discovers_sources_once_and_defaults_to_stats_only(tmp_path: Path):
    db = create_app(tmp_path / "server.db").state.db
    codex = tmp_path / "sessions"
    codex.mkdir()
    hermes = tmp_path / "state.db"
    hermes.write_bytes(b"")
    config_path = tmp_path / "collector.json"
    first = prepare_local_collector(db, config_path, 8765, [("codex", codex), ("hermes", hermes)])
    second = prepare_local_collector(db, config_path, 8765, [("codex", codex), ("hermes", hermes)])
    assert first == second
    assert len(second["sources"]) == 2
    assert all(source["content_policy"] == "stats_only" for source in second["sources"])
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 2
    initialize_owner(db, "correct-horse-battery-staple")
    client = TestClient(create_app(tmp_path / "server.db"))
    csrf = client.post("/auth/login", json={"password": "correct-horse-battery-staple"}).json()["csrf"]
    source_id = first["sources"][0]["id"]
    changed = client.post(f"/v1/local/sources/{source_id}/policy",
                          headers={"x-awb-csrf": csrf}, json={"content_policy": "full_content"})
    assert changed.status_code == 200
    assert json.loads(config_path.read_text(encoding="utf-8"))["sources"][0]["content_policy"] == "full_content"
    with db.read() as conn:
        policy = conn.execute("SELECT capability_json FROM sources WHERE id=?", (source_id,)).fetchone()[0]
    assert json.loads(policy)["content_policy"] == "full_content"


def test_terminal_before_start_backfills_duration_and_stats_exclude_child(tmp_path: Path):
    db = create_app(tmp_path / "server.db").state.db
    device, source = str(uuid4()), str(uuid4())
    with db.tx() as conn:
        conn.execute("INSERT INTO devices(id,name,os,environment,token_hash,registered_at) VALUES(?,?,?,?,?,?)",
                     (device, "Test", "Windows", "test", str(uuid4()), "2026-09-23T00:00:00Z"))
        conn.execute("INSERT INTO sources(id,device_id,agent,profile,created_at) VALUES(?,?,?,?,?)",
                     (source, device, "codex", "default", "2026-09-23T00:00:00Z"))
    def run(turn: str, status: str, order: int, start: str | None, end: str | None,
            parent: str | None = None):
        payload = {"native_turn_id": turn, "status": status, "start_at": start, "end_at": end,
                   "parent_run_ref": parent, "model_attribution": {"kind": "unknown"}}
        event = make_event(source, "session", "run.observed", turn, status, order, end or start,
                           payload, device, None)
        with db.tx() as conn:
            project(conn, event)

    run("a", "completed", 2, None, "2026-09-23T01:10:00Z")
    run("a", "running", 1, "2026-09-23T01:00:00Z", None)
    run("child", "completed", 3, "2026-09-23T01:02:00Z", "2026-09-23T01:07:00Z", "a")
    run("b", "failed", 4, "2026-09-23T01:05:00Z", "2026-09-23T01:15:00Z")
    result = calculate(db, "2026-09-23", "Asia/Hong_Kong")
    metrics = {m["id"]: m["value"] for m in result["metrics"]}
    assert metrics["settled_duration_ms"] == 1_200_000
    assert metrics["active_wall_ms"] == 900_000
    assert metrics["duration_p95_ms"] == 600_000
    assert result["daily_trend"][0]["runs"] == 2
    assert result["device_comparison"][0]["active_wall_ms"] == 900_000
    assert calculate(db, "2026-09-22", "Asia/Hong_Kong", through="2026-09-23")["metrics"][2]["value"] == 2
    initialize_owner(db, "correct-horse-battery-staple")
    client = TestClient(create_app(tmp_path / "server.db"))
    assert client.post("/auth/login", json={"password": "correct-horse-battery-staple"}).status_code == 200
    assert client.get("/v1/stats", params={"day": "2026-09-23", "through": "2026-09-23"}).status_code == 200
    assert client.get("/v1/timeline", params={"day": "2026-09-23", "device_ids": device}).json()["items"]
    evidence = client.get("/v1/stats/contributors", params={"metric_id": "settled_duration_ms", "day": "2026-09-23"}).json()
    assert evidence["items"][0]["amount"] == 1_200_000
    assert client.get("/v1/sessions", params={"activity_day": "2026-09-23", "device_id": device}).json()["items"]
    assert not client.get("/v1/sessions", params={"activity_day": "2026-09-22", "device_id": device}).json()["items"]
    with db.read() as conn:
        a = conn.execute("SELECT start_at,end_at,status,duration_ms FROM runs WHERE native_id='a'").fetchone()
    assert tuple(a) == ("2026-09-23T01:00:00.000Z", "2026-09-23T01:10:00.000Z", "completed", 600_000)


def test_cumulative_components_are_separate_and_date_attributed(tmp_path: Path):
    db = create_app(tmp_path / "server.db").state.db
    device, source = str(uuid4()), str(uuid4())
    with db.tx() as conn:
        conn.execute("INSERT INTO devices(id,name,os,environment,token_hash,registered_at) VALUES(?,?,?,?,?,?)",
                     (device, "Test", "Windows", "test", str(uuid4()), "2026-09-23T00:00:00Z"))
        conn.execute("INSERT INTO sources(id,device_id,agent,profile,created_at) VALUES(?,?,?,?,?)",
                     (source, device, "codex", "default", "2026-09-23T00:00:00Z"))
    for index, (at, total, inp, out) in enumerate((
        ("2026-09-23T01:00:00Z", 120, 100, 20),
        ("2026-09-23T01:05:00Z", 165, 130, 35),
        ("2026-09-24T01:00:00Z", 200, 150, 50),
    )):
        for key, amount in (("codex_total", total), ("codex_input", inp), ("codex_output", out)):
            event = make_event(source, "session", "usage.observed", f"{key}:{index}", "1", index, at,
                               {"usage_key": key, "quantity_semantics": "cumulative_snapshot",
                                "coverage_scope": "session", "counter_id": "session", "epoch_id": "session",
                                "source_time": at, "total_tokens": amount}, device, None)
            with db.tx() as conn:
                conn.execute("""INSERT INTO events(event_id,body_hash,source_id,native_session_id,fact_kind,
                    native_fact_id,revision_key,source_order,occurred_at,content_json,received_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (event["event_id"], event["body_hash"], source, "session", "usage.observed",
                     f"{key}:{index}", "1", index, at, json.dumps(event["content"]), at))
                project(conn, event)
    day = {m["id"]: m["value"] for m in calculate(db, "2026-09-23", "Asia/Hong_Kong")["metrics"]}
    assert day["counter_total_tokens"] == 45
    assert day["counter_input_tokens"] == 30
    assert day["counter_output_tokens"] == 15
    assert day["input_tokens"] is None
    next_day = {m["id"]: m["value"] for m in calculate(db, "2026-09-24", "Asia/Hong_Kong")["metrics"]}
    assert next_day["counter_total_tokens"] is None


def test_search_finds_stats_only_session_by_title_and_path(tmp_path: Path):
    app = create_app(tmp_path / "server.db")
    initialize_owner(app.state.db, "correct-horse-battery-staple")
    device, source, session = str(uuid4()), str(uuid4()), str(uuid4())
    with app.state.db.tx() as conn:
        conn.execute("INSERT INTO devices(id,name,os,environment,token_hash,registered_at) VALUES(?,?,?,?,?,?)",
                     (device, "Test", "Windows", "test", str(uuid4()), "2026-09-23T00:00:00Z"))
        conn.execute("INSERT INTO sources(id,device_id,agent,profile,created_at) VALUES(?,?,?,?,?)",
                     (source, device, "codex", "default", "2026-09-23T00:00:00Z"))
        conn.execute("INSERT INTO sessions(id,source_id,native_id,agent,device_id,title,cwd,last_activity) VALUES(?,?,?,?,?,?,?,?)",
                     (session, source, "native", "codex", device, "设计看板", "B:/Sync_AI/AgentWorkbench", "2026-09-23T01:00:00Z"))
    client = TestClient(app)
    assert client.post("/auth/login", json={"password": "correct-horse-battery-staple"}).status_code == 200
    for term in ("看板", "AgentWorkbench"):
        response = client.get("/v1/search", params={"q": term})
        assert response.status_code == 200
        assert response.json()["items"][0]["session_id"] == session
