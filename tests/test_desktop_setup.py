from pathlib import Path

from fastapi.testclient import TestClient

from awb.api import create_app
from awb.auth import initialize_owner
from awb.desktop import default_db_path, reset_owner_password


def local_client(app):
    return TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))


def test_local_desktop_opens_without_password(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-source"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-source"))
    monkeypatch.setenv("HERMES_STATE_DB", str(tmp_path / "hermes-source.db"))
    app = create_app(tmp_path / "workbench.db", desktop_mode=True)
    client = local_client(app)
    assert client.get("/auth/setup-status").json() == {
        "needs_setup": False, "web_setup_available": False}
    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["authenticated"] is True
    assert me.json()["csrf"]
    assert client.get("/v1/mvp/usage", params={"day": "2026-09-26", "through": "2026-09-26",
                                                "tz": "UTC"}).status_code == 200
    assert client.post("/auth/setup", json={"password": "long-enough-123",
                                             "confirmation": "long-enough-123"}).status_code == 410
    assert client.post("/auth/login", json={"password": "long-enough-123"}).status_code == 410


def test_passwordless_access_is_local_only(tmp_path: Path):
    app = create_app(tmp_path / "workbench.db", desktop_mode=True)
    assert TestClient(app, base_url="http://example.com").get("/auth/me").status_code == 403
    assert TestClient(app, base_url="http://127.0.0.1:8765",
                      client=("192.0.2.10", 50000)).get("/auth/me").status_code == 403
    client = local_client(app)
    assert client.get("/auth/me", headers={"origin": "http://evil.example"}).status_code == 403


def test_shutdown_still_requires_page_csrf(tmp_path: Path):
    app = create_app(tmp_path / "workbench.db", desktop_mode=True)
    called = []
    app.state.shutdown_callback = lambda: called.append(True)
    client = local_client(app)
    assert client.post("/v1/local/shutdown").status_code == 403
    csrf = client.get("/auth/me").json()["csrf"]
    assert client.post("/v1/local/shutdown", headers={"x-awb-csrf": csrf}).json() == {
        "stopping": True}
    assert called == [True]


def test_existing_password_does_not_block_local_dashboard_or_reset_data(tmp_path: Path):
    db_path = tmp_path / "workbench.db"
    app = create_app(db_path, desktop_mode=True)
    initialize_owner(app.state.db, "long-enough-123")
    with app.state.db.tx() as conn:
        conn.execute("INSERT INTO devices(id,name,os,environment,token_hash,registered_at) "
                     "VALUES(?,?,?,?,?,?)", ("device-test", "Test", "Windows", "test", "hash",
                                             "2026-09-26T00:00:00Z"))
    assert local_client(app).get("/auth/me").status_code == 200
    reset_owner_password(db_path)
    with app.state.db.read() as conn:
        assert conn.execute("SELECT 1 FROM owner").fetchone() is None
        assert conn.execute("SELECT 1 FROM devices WHERE id='device-test'").fetchone() is not None


def test_portable_default_path(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("sys.executable", str(tmp_path / "AgentWorkbench.exe"))
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    assert default_db_path() == tmp_path / "appdata" / "AgentWorkbench" / "data" / "agent-workbench.db"
    (tmp_path / "portable.flag").touch()
    assert default_db_path() == tmp_path / "data" / "agent-workbench.db"


def test_macos_default_path(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("sys.executable", str(tmp_path / "python"))
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert default_db_path() == (tmp_path / "Library" / "Application Support" /
                                 "AgentWorkbench" / "data" / "agent-workbench.db")
