from pathlib import Path

from fastapi.testclient import TestClient

from awb.api import create_app
from awb.desktop import default_db_path, reset_owner_password


def test_web_first_run_creates_owner_and_signs_in(tmp_path: Path):
    app = create_app(tmp_path / "workbench.db", desktop_mode=True)
    client = TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    assert client.get("/auth/setup-status").json() == {"needs_setup": True, "web_setup_available": True}
    assert client.post("/auth/setup", json={"password": "short", "confirmation": "short"}).status_code == 422
    assert client.post("/auth/setup", json={"password": "long-enough-123", "confirmation": "different"}).status_code == 422
    response = client.post("/auth/setup", headers={"origin": "http://127.0.0.1:8765"},
                           json={"password": "long-enough-123", "confirmation": "long-enough-123"})
    assert response.status_code == 200, response.text
    assert response.json()["authenticated"] is True
    assert client.get("/auth/me").status_code == 200
    assert client.get("/auth/setup-status").json()["needs_setup"] is False
    assert client.post("/auth/setup", json={"password": "another-long-123", "confirmation": "another-long-123"}).status_code == 409
    assert client.post("/auth/logout", headers={"x-awb-csrf": response.json()["csrf"]}).status_code == 200
    assert client.post("/auth/login", json={"password": "long-enough-123"}).status_code == 200


def test_web_setup_is_local_desktop_only(tmp_path: Path):
    payload = {"password": "long-enough-123", "confirmation": "long-enough-123"}
    remote = TestClient(create_app(tmp_path / "remote.db", desktop_mode=True), base_url="http://example.com")
    assert remote.post("/auth/setup", json=payload).status_code == 403
    service = TestClient(create_app(tmp_path / "service.db"), base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    assert service.post("/auth/setup", json=payload).status_code == 403
    local = TestClient(create_app(tmp_path / "local.db", desktop_mode=True), base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    assert local.post("/auth/setup", headers={"origin": "http://evil.example"}, json=payload).status_code == 403


def test_shutdown_requires_session_and_csrf(tmp_path: Path):
    app = create_app(tmp_path / "workbench.db", desktop_mode=True)
    called = []
    app.state.shutdown_callback = lambda: called.append(True)
    client = TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    assert client.post("/v1/local/shutdown").status_code == 401
    csrf = client.post("/auth/setup", json={"password": "long-enough-123", "confirmation": "long-enough-123"}).json()["csrf"]
    assert client.post("/v1/local/shutdown").status_code == 403
    assert client.post("/v1/local/shutdown", headers={"x-awb-csrf": csrf}).json() == {"stopping": True}
    assert called == [True]


def test_first_run_can_close_without_creating_password(tmp_path: Path):
    app = create_app(tmp_path / "workbench.db", desktop_mode=True)
    called = []
    app.state.shutdown_callback = lambda: called.append(True)
    client = TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    assert client.post("/auth/cancel-setup").json() == {"stopping": True}
    assert called == [True]
    client.post("/auth/setup", json={"password": "long-enough-123", "confirmation": "long-enough-123"})
    assert client.post("/auth/cancel-setup").status_code == 409


def test_login_page_can_close_and_local_reset_preserves_data(tmp_path: Path):
    db_path = tmp_path / "workbench.db"
    app = create_app(db_path, desktop_mode=True)
    called = []
    app.state.shutdown_callback = lambda: called.append(True)
    client = TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    setup = client.post("/auth/setup", json={"password": "long-enough-123", "confirmation": "long-enough-123"})
    assert setup.status_code == 200
    with app.state.db.tx() as conn:
        conn.execute("INSERT INTO devices(id,name,os,environment,token_hash,registered_at) VALUES(?,?,?,?,?,?)",
                     ("device-test", "Test", "Windows", "test", "hash", "2026-09-26T00:00:00Z"))
    assert client.post("/auth/close-local").json() == {"stopping": True}
    assert called == [True]
    reset_owner_password(db_path)
    with app.state.db.read() as conn:
        assert conn.execute("SELECT 1 FROM owner").fetchone() is None
        assert conn.execute("SELECT 1 FROM web_sessions").fetchone() is None
        assert conn.execute("SELECT 1 FROM devices WHERE id='device-test'").fetchone() is not None


def test_portable_default_path(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("sys.executable", str(tmp_path / "AgentWorkbench.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    assert default_db_path() == tmp_path / "appdata" / "AgentWorkbench" / "data" / "agent-workbench.db"
    (tmp_path / "portable.flag").touch()
    assert default_db_path() == tmp_path / "data" / "agent-workbench.db"
