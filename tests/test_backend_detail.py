from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from awb.api import create_app
from awb.auth import initialize_owner
from awb.codec import sha256
from awb.collector import Outbox, make_event
from awb.projector import project
from awb.stats import calculate, timeline


def _fixture(tmp_path: Path):
    app = create_app(tmp_path / "workbench.db")
    db = app.state.db
    initialize_owner(db, "correct-horse-battery-staple")
    device, source = str(uuid4()), str(uuid4())
    sid = sha256([source, "native-abcdef1234"])
    with db.tx() as conn:
        conn.execute("INSERT INTO devices(id,name,os,environment,token_hash,registered_at) VALUES(?,?,?,?,?,?)",
                     (device, "Desk", "Windows", "local", str(uuid4()), "2026-09-20T00:00:00Z"))
        conn.execute("INSERT INTO sources(id,device_id,agent,profile,created_at) VALUES(?,?,?,?,?)",
                     (source, device, "codex", "default", "2026-09-20T00:00:00Z"))
        conn.execute("""INSERT INTO sessions(id,source_id,native_id,agent,device_id,cwd,created_at,last_activity)
            VALUES(?,?,?,?,?,?,?,?)""", (sid, source, "native-abcdef1234", "codex", device,
            "C:/Documents/github", "2026-09-22T23:00:00.000Z", "2026-09-23T12:30:00.000Z"))
    client = TestClient(app)
    csrf = client.post("/auth/login", json={"password": "correct-horse-battery-staple"}).json()["csrf"]
    return app, db, client, csrf, device, source, sid


def test_session_identity_coverage_title_and_short_chinese_search(tmp_path: Path):
    _, db, client, csrf, _, _, sid = _fixture(tmp_path)
    with db.tx() as conn:
        conn.execute("""INSERT INTO messages(id,session_id,native_id,role,input_origin,body,content_state,
            source_char_count,occurred_at,event_id,source_order)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""", ("m1", sid, "native-m1", "user", "human", "设计 一个看板",
            "full", 7, "2026-09-22T23:01:00.000Z", "e1", 1))
        conn.execute("""INSERT INTO messages(id,session_id,native_id,role,input_origin,body,content_state,
            source_char_count,occurred_at,event_id,source_order)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""", ("m2", sid, "native-m2", "assistant", "ai", None,
            "stats_only", 4, "2026-09-22T23:02:00.000Z", "e2", 2))
    item = client.get("/v1/sessions").json()["items"][0]
    assert item["display_title"] == "设计 一个看板"
    assert item["title_basis"] == "first_user_message"
    assert item["content_coverage"] == {"readable": 1, "total": 2, "missing": 1, "state": "partial"}
    result = client.get("/v1/search", params={"q": "看"}).json()
    assert result["items"][0]["matched_message_id"] == "m1"
    assert result["short_query_limit"] == 50
    renamed = client.patch(f"/v1/sessions/{sid}/title", headers={"x-awb-csrf": csrf},
                           json={"title": "我的设计项目"})
    assert renamed.status_code == 200
    assert renamed.json()["session"]["title_basis"] == "user_title"
    assert client.get("/v1/sessions").json()["items"][0]["display_title"] == "我的设计项目"
    assert client.patch(f"/v1/sessions/{sid}/title", headers={"x-awb-csrf": csrf},
                        json={"title": None}).json()["session"]["title_basis"] == "first_user_message"
    # New additive title table is safe to initialize again with existing v1 data.
    create_app(db.path)
    assert client.get("/v1/sessions").json()["items"][0]["id"] == sid


def test_overlap_filter_latest_paging_and_run_focus(tmp_path: Path):
    _, db, client, _, device, source, sid = _fixture(tmp_path)
    with db.tx() as conn:
        conn.execute("""INSERT INTO runs(id,session_id,native_id,status,start_at,end_at,event_id)
            VALUES(?,?,?,?,?,?,?)""", ("run1", sid, "turn1", "completed",
            "2026-09-22T23:00:00.000Z", "2026-09-23T01:00:00.000Z", "run-event"))
        for n in range(5):
            conn.execute("""INSERT INTO messages(id,session_id,native_id,role,input_origin,body,content_state,
                occurred_at,turn_id,event_id,source_order) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (f"m{n}", sid, f"native-m{n}", "user", "human", f"content {n}", "full",
                 f"2026-09-23T00:0{n}:00.000Z", "turn1", f"event-{n}", n))
    listed = client.get("/v1/sessions", params={"activity_day": "2026-09-23", "tz": "UTC"}).json()["items"]
    assert listed and listed[0]["latest_matching_activity"] == "2026-09-23T01:00:00.000Z"
    latest = client.get(f"/v1/sessions/{sid}/events", params={"limit": 2}).json()
    assert [x["id"] for x in latest["items"]] == ["m3", "m4"]
    assert latest["next_cursor"] is None and latest["prev_cursor"]
    older = client.get(f"/v1/sessions/{sid}/events", params={"limit": 2, "cursor": latest["prev_cursor"]}).json()
    assert [x["id"] for x in older["items"]] == ["m1", "m2"]
    assert all(x["run_id"] == "run1" and x["turn_mapping_basis"] == "native_turn_id" for x in latest["items"])
    focused = client.get(f"/v1/sessions/{sid}/events", params={"focus": "run", "run_id": "run1", "limit": 2}).json()
    assert [x["id"] for x in focused["items"]] == ["m0", "m1"]
    assert focused["next_cursor"] and focused["runs"][0]["message_count"] == 5
    at_message = client.get(f"/v1/sessions/{sid}/events", params={"focus": "message", "message_id": "m2", "limit": 2}).json()
    assert [x["id"] for x in at_message["items"]] == ["m2", "m3"]
    fact = make_event(source, "native-abcdef1234", "file.observed", "file1", "1", 10,
                      "2026-09-23T00:01:00Z", {"native_path": "C:/Documents/github/result.txt",
                      "relation": "created", "operation_status": "succeeded", "run_ref": "turn1",
                      "evidence_refs": [{"kind": "codex_apply_patch", "call_id": "call1"}]}, device, None)
    with db.tx() as conn:
        conn.execute("""INSERT INTO events(event_id,body_hash,source_id,native_session_id,fact_kind,
            native_fact_id,revision_key,source_order,occurred_at,content_json,received_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (fact["event_id"], fact["body_hash"], source,
            "native-abcdef1234", "file.observed", "file1", "1", 10,
            "2026-09-23T00:01:00Z", json.dumps(fact["content"]), "2026-09-23T00:01:00Z"))
        project(conn, fact)
    detail = client.get(f"/v1/files/{fact['event_id']}").json()
    assert detail["file"]["run_id"] == "run1"
    assert detail["file"]["operation_at"] == "2026-09-23T00:01:00Z"
    assert detail["current_access"]["state"] == "not_checked"
    events = client.get(f"/v1/sessions/{sid}/events").json()
    assert events["files"][0]["run_id"] == "run1"
    assert events["files"][0]["operation_at"] == "2026-09-23T00:01:00Z"
    assert events["runs"][0]["file_count"] == 1


def test_run_focus_pages_only_members_and_marks_inferred_evidence(tmp_path: Path):
    _, db, client, _, _, _, sid = _fixture(tmp_path)
    with db.tx() as conn:
        for rid, native, start, end in (
            ("run1", "turn1", "2026-09-23T00:00:00.000Z", "2026-09-23T00:20:00.000Z"),
            ("run2", "turn2", "2026-09-23T00:10:00.000Z", "2026-09-23T00:30:00.000Z"),
        ):
            conn.execute("""INSERT INTO runs(id,session_id,native_id,status,start_at,end_at,event_id)
                VALUES(?,?,?,?,?,?,?)""", (rid, sid, native, "completed", start, end, f"event-{rid}"))
        for mid, at, turn in (
            ("m1", "2026-09-23T00:01:00.000Z", "turn1"),
            ("m2", "2026-09-23T00:02:00.000Z", "turn1"),
            ("m3", "2026-09-23T00:03:00.000Z", None),  # One closed run contains this.
            ("m4", "2026-09-23T00:11:00.000Z", None),  # Two runs overlap here.
            ("m5", "2026-09-23T00:12:00.000Z", "turn1"),
            ("m6", "2026-09-23T00:21:00.000Z", "turn2"),
        ):
            conn.execute("""INSERT INTO messages(id,session_id,native_id,role,input_origin,body,
                content_state,occurred_at,turn_id,event_id) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (mid, sid, mid, "user", "human", mid, "full", at, turn, f"event-{mid}"))
    seen, cursor = [], None
    while True:
        params = {"focus": "run", "run_id": "run1", "limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = client.get(f"/v1/sessions/{sid}/events", params=params).json()
        seen.extend((item["id"], item["turn_mapping_basis"]) for item in page["items"])
        assert page["focus"]["mapping_counts"] == {"total": 4, "direct": 3, "inferred": 1, "readable": 4}
        assert page["runs"][0]["message_count"] == 4
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == [("m1", "native_turn_id"), ("m2", "native_turn_id"),
                    ("m3", "unique_time_window"), ("m5", "native_turn_id")]


def test_explicit_local_file_check_reports_current_stat_only(tmp_path: Path):
    app, db, _, _, device, source, sid = _fixture(tmp_path)
    app.state.desktop_mode = True
    workdir = tmp_path / "project"
    workdir.mkdir()
    file = workdir / "result.txt"
    file.write_text("current content", encoding="utf-8")
    (db.path.parent / "collector.json").write_text(json.dumps({
        "collector_id": device, "sources": [{"id": source, "agent": "codex"}]}), encoding="utf-8")
    fact = make_event(source, "native-abcdef1234", "file.observed", "result", "1", 10,
                      "2026-09-23T00:01:00Z", {"native_path": str(file), "cwd": str(workdir),
                      "relative_path": "result.txt", "relation": "created", "operation_status": "succeeded",
                      "evidence_refs": [{"kind": "codex_apply_patch", "call_id": "c1"}]}, device, None)
    with db.tx() as conn:
        conn.execute("""INSERT INTO events(event_id,body_hash,source_id,native_session_id,fact_kind,
            native_fact_id,revision_key,source_order,occurred_at,content_json,received_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (fact["event_id"], fact["body_hash"], source,
            "native-abcdef1234", "file.observed", "result", "1", 10,
            "2026-09-23T00:01:00Z", json.dumps(fact["content"]), "2026-09-23T00:01:00Z"))
        project(conn, fact)
    client = TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    csrf = client.post("/auth/login", json={"password": "correct-horse-battery-staple"}).json()["csrf"]
    result = client.post(f"/v1/local/files/{fact['event_id']}/check", headers={"x-awb-csrf": csrf})
    assert result.status_code == 200
    current = result.json()["current_access"]
    assert current["state"] == "exists"
    assert current["result"]["size_bytes"] == file.stat().st_size
    assert result.json()["historical_size_bytes"] is None


def test_message_revision_upgrades_body_without_later_stats_downgrade(tmp_path: Path):
    _, db, _, _, device, source, _ = _fixture(tmp_path)
    def event(revision: str, order: int, body: str | None, supersedes: str | None = None):
        payload = {"native_message_id": "m", "role": "user", "input_origin": "human",
                   "body": body, "content_state": "full" if body is not None else "stats_only",
                   "source_text_char_count": 7, "native_turn_id": "turn1", "finalized": True}
        item = make_event(source, "native-abcdef1234", "message.observed", "m", revision, order,
                          "2026-09-23T00:00:00Z", payload, device, None)
        if supersedes:
            item["content"]["supersedes_event_id"] = supersedes
        return item
    initial = event("1", 20, None)
    upgraded = event("2", 20, "原始内容", initial["event_id"])
    later_stats = event("3", 21, None)
    with db.tx() as conn:
        project(conn, initial)
        project(conn, upgraded)
        project(conn, later_stats)
        row = conn.execute("SELECT body,content_state,turn_id FROM messages WHERE native_id='m'").fetchone()
    assert tuple(row) == ("原始内容", "full", "turn1")


def test_cached_input_is_reported_as_subset_not_added_to_input(tmp_path: Path):
    _, db, client, _, device, source, _ = _fixture(tmp_path)
    for index, (at, values) in enumerate((
        ("2026-09-23T01:00:00Z", (110, 100, 10, 80)),
        ("2026-09-23T01:05:00Z", (212, 200, 12, 180)),
    )):
        for key, value in zip(("codex_total", "codex_input", "codex_output", "codex_cached_input"), values):
            item = make_event(source, "native-abcdef1234", "usage.observed", f"{key}:{index}", "1", index,
                              at, {"usage_key": key, "quantity_semantics": "cumulative_snapshot",
                                   "coverage_scope": "session", "counter_id": "session", "epoch_id": "session",
                                   "source_time": at, "total_tokens": value}, device, None)
            with db.tx() as conn:
                conn.execute("""INSERT INTO events(event_id,body_hash,source_id,native_session_id,fact_kind,
                    native_fact_id,revision_key,source_order,occurred_at,content_json,received_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (item["event_id"], item["body_hash"], source,
                    "native-abcdef1234", "usage.observed", f"{key}:{index}", "1", index, at,
                    json.dumps(item["content"]), at))
                project(conn, item)
    result = calculate(db, "2026-09-23", "UTC")
    metrics = {item["id"]: item for item in result["metrics"]}
    assert metrics["counter_input_tokens"]["value"] == 100
    assert metrics["counter_cached_input_tokens"]["value"] == 100
    assert metrics["counter_output_tokens"]["value"] == 2
    assert metrics["counter_total_tokens"]["value"] == 102
    assert result["usage_explanation"]["counter_arithmetic_gap"] == 0
    assert result["usage_explanation"]["cached_input_share_of_input"] == 1
    assert result["usage_explanation"]["non_cached_input_tokens"] == 0
    assert metrics["counter_input_tokens"]["verification_state"] == "not_source_reconciled"
    contributors = client.get("/v1/stats/contributors", params={"metric_id": "counter_cached_input_tokens",
                                                          "day": "2026-09-23", "tz": "UTC"}).json()
    assert contributors["items"][0]["amount"] == 100
    assert contributors["items"][0]["evidence"][0]["basis"] == "counter_snapshot_not_turn_attributed"


def test_token_contributors_keep_incremental_and_counter_sources_separate(tmp_path: Path):
    _, db, client, _, device, source, _ = _fixture(tmp_path)
    facts = [
        ("codex_input", "cumulative_snapshot", "2026-09-23T01:00:00Z", 100, None, None),
        ("codex_output", "cumulative_snapshot", "2026-09-23T01:00:00Z", 20, None, None),
        ("codex_input", "cumulative_snapshot", "2026-09-23T01:05:00Z", 150, None, None),
        ("codex_output", "cumulative_snapshot", "2026-09-23T01:05:00Z", 30, None, None),
        ("request", "incremental", "2026-09-23T01:06:00Z", None, 7, 3),
    ]
    for index, (key, semantics, at, total, inp, out) in enumerate(facts):
        item = make_event(source, "native-abcdef1234", "usage.observed", f"usage-{index}", "1", index,
                          at, {"usage_key": key, "quantity_semantics": semantics,
                               "coverage_scope": "session" if total is not None else "request",
                               "counter_id": "session", "epoch_id": "session", "source_time": at,
                               "total_tokens": total, "input_tokens": inp, "output_tokens": out},
                          device, None)
        with db.tx() as conn:
            conn.execute("""INSERT INTO events(event_id,body_hash,source_id,native_session_id,fact_kind,
                native_fact_id,revision_key,source_order,occurred_at,content_json,received_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (item["event_id"], item["body_hash"], source,
                "native-abcdef1234", "usage.observed", f"usage-{index}", "1", index, at,
                json.dumps(item["content"]), at))
            project(conn, item)
    metrics = {item["id"]: item["value"] for item in calculate(db, "2026-09-23", "UTC")["metrics"]}
    assert (metrics["input_tokens"], metrics["output_tokens"]) == (7, 3)
    assert (metrics["counter_input_tokens"], metrics["counter_output_tokens"]) == (50, 10)
    for metric_id in ("input_tokens", "output_tokens", "counter_input_tokens", "counter_output_tokens"):
        evidence = client.get("/v1/stats/contributors", params={
            "metric_id": metric_id, "day": "2026-09-23", "tz": "UTC"}).json()
        assert sum(item["amount"] for item in evidence["items"]) == metrics[metric_id]


def test_unknown_end_run_only_appears_on_start_day_and_duration_is_unknown(tmp_path: Path):
    _, db, _, _, _, _, sid = _fixture(tmp_path)
    with db.tx() as conn:
        conn.execute("""INSERT INTO runs(id,session_id,native_id,status,start_at,end_at,event_id)
            VALUES(?,?,?,?,?,?,?)""", ("older-open", sid, "older-open", "running",
            "2026-09-22T01:00:00.000Z", None, "run-older"))
        conn.execute("""INSERT INTO runs(id,session_id,native_id,status,start_at,end_at,event_id)
            VALUES(?,?,?,?,?,?,?)""", ("today-open", sid, "today-open", "running",
            "2026-09-23T01:00:00.000Z", None, "run-today"))
    assert [item["id"] for item in timeline(db, "2026-09-23", "UTC")["items"]] == ["today-open"]
    assert not timeline(db, "2026-09-24", "UTC")["items"]
    metrics = {item["id"]: item["value"] for item in calculate(db, "2026-09-23", "UTC")["metrics"]}
    assert metrics["run_count"] == 1
    assert metrics["active_wall_ms"] is None and metrics["settled_duration_ms"] is None


def test_filtered_sessions_order_by_activity_within_range_and_page_consistently(tmp_path: Path):
    _, db, client, _, device, source, sid = _fixture(tmp_path)
    other = sha256([source, "other-native-session"])
    with db.tx() as conn:
        conn.execute("UPDATE sessions SET last_activity=? WHERE id=?", ("2026-09-26T10:00:00.000Z", sid))
        conn.execute("""INSERT INTO sessions(id,source_id,native_id,agent,device_id,last_activity)
            VALUES(?,?,?,?,?,?)""", (other, source, "other-native-session", "codex", device,
            "2026-09-24T10:00:00.000Z"))
        for ident, session, at in (("old", sid, "2026-09-23T01:00:00.000Z"),
                                   ("new", other, "2026-09-23T22:00:00.000Z")):
            conn.execute("""INSERT INTO messages(id,session_id,native_id,role,input_origin,body,
                content_state,occurred_at,event_id) VALUES(?,?,?,?,?,?,?,?,?)""",
                (ident, session, ident, "user", "human", ident, "full", at, f"event-{ident}"))
    params = {"activity_day": "2026-09-23", "tz": "UTC", "limit": 1}
    first = client.get("/v1/sessions", params=params).json()
    assert [item["id"] for item in first["items"]] == [other]
    second = client.get("/v1/sessions", params={**params, "cursor": first["next_cursor"]}).json()
    assert [item["id"] for item in second["items"]] == [sid]
    assert second["next_cursor"] is None


def test_content_delete_removes_all_revision_text_and_identity(tmp_path: Path):
    _, db, client, csrf, device, source, sid = _fixture(tmp_path)
    with db.tx() as conn:
        conn.execute("INSERT INTO session_titles(session_id,user_title,updated_at) VALUES(?,?,?)",
                     (sid, "Private title", "2026-09-23T00:00:00Z"))
        for n in (1, 2):
            conn.execute("""INSERT INTO events(event_id,body_hash,source_id,native_session_id,fact_kind,
                native_fact_id,revision_key,source_order,occurred_at,content_json,received_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (f"e{n}", f"hash{n}", source, "native-abcdef1234",
                "message.observed", "m", str(n), 1, "2026-09-23T00:00:00Z",
                json.dumps({"body": f"secret revision {n}"}), "2026-09-23T00:00:00Z"))
        conn.execute("""INSERT INTO messages(id,session_id,native_id,role,input_origin,body,content_state,
            event_id) VALUES(?,?,?,?,?,?,?,?)""", ("message", sid, "m", "user", "human", "secret revision 2", "full", "e2"))
    (db.path.parent / "collector.json").write_text(json.dumps({
        "collector_id": device, "sources": [{"id": source, "agent": "codex"}]}), encoding="utf-8")
    outbox = Outbox(db.path.parent / "outbox.db")
    pending = make_event(source, "native-abcdef1234", "message.observed", "pending", "1", 20,
                         "2026-09-23T00:01:00Z", {"native_message_id": "pending", "role": "user",
                         "input_origin": "human", "body": "private pending text", "content_state": "full"}, device, None)
    outbox.append(source, "pending.jsonl", 20, "hash", "native-abcdef1234", [pending])
    result = client.request("DELETE", f"/v1/sessions/{sid}/content", headers={"x-awb-csrf": csrf},
                            json={"confirmation": sid, "keep_statistics": True})
    assert result.status_code == 200
    assert result.json()["local_collector_cleanup"]["rewritten_pending"] == 1
    assert outbox.is_blocked(source, "native-abcdef1234")
    with outbox.connect() as conn:
        assert "private pending text" not in conn.execute("SELECT event_json FROM outbox").fetchone()[0]
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE source_id=?", (source,)).fetchone()[0] == 0
        assert conn.execute("SELECT body FROM messages WHERE id='message'").fetchone()[0] is None
        assert tuple(conn.execute("SELECT title,cwd FROM sessions WHERE id=?", (sid,)).fetchone()) == (None, None)
        assert conn.execute("SELECT COUNT(*) FROM session_titles WHERE session_id=?", (sid,)).fetchone()[0] == 0
