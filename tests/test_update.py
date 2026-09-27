"""Regression checks for selecting and authorizing verified desktop updates."""

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from awb.api import create_app
from awb.update import UpdateManager, select_release


def release(version: str, digest: str = "sha256:" + "a" * 64, size: int = 42) -> dict:
    tag = f"v{version}-mvp.1"
    name = f"AgentWorkbench-Setup-{version}-Windows-x64.exe"
    return {"tag_name": tag, "draft": False, "assets": [{"name": name, "state": "uploaded",
            "digest": digest, "size": size, "browser_download_url":
            f"https://github.com/zznmdhz/agent-workbench/releases/download/{tag}/{name}"}]}


def test_only_newer_published_and_hashed_installers_are_selected():
    candidates = [release("0.4.0"), release("0.4.1", digest=""), release("0.4.2"),
                  {**release("0.4.3"), "draft": True}, release("0.4.4", size=0)]
    result = select_release(candidates, "0.4.0")
    assert result is not None
    assert result["version"] == "0.4.2"
    assert result["sha256"] == "a" * 64
    assert select_release([release("0.4.0")], "0.4.0") is None


def test_download_verifies_bytes_before_launching_installer(monkeypatch, tmp_path: Path):
    content = b"test installer bytes"
    selected = select_release([release("0.4.2", digest="sha256:" + hashlib.sha256(content).hexdigest(),
                                       size=len(content))], "0.4.1")
    assert selected is not None
    launched = []
    stopped = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def raise_for_status(self):
            pass

        def iter_bytes(self, _):
            yield content

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def stream(self, *_):
            return Response()

    monkeypatch.setattr("awb.update.httpx.Client", lambda **_: Client())
    monkeypatch.setattr("awb.update.subprocess.Popen", lambda *args, **kwargs: launched.append((args, kwargs)))
    manager = UpdateManager(tmp_path / "AgentWorkbench.exe", tmp_path / "updates")
    manager._download_and_install(selected, lambda: stopped.append(True))
    assert manager.status()["state"] == "installing"
    assert stopped == [True]
    assert len(launched) == 1
    assert (tmp_path / "updates" / selected["name"]).read_bytes() == content


def test_corrupt_download_never_starts_installer(monkeypatch, tmp_path: Path):
    selected = select_release([release("0.4.2", size=3)], "0.4.1")
    assert selected is not None

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def raise_for_status(self):
            pass

        def iter_bytes(self, _):
            yield b"bad"

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def stream(self, *_):
            return Response()

    monkeypatch.setattr("awb.update.httpx.Client", lambda **_: Client())
    monkeypatch.setattr("awb.update.subprocess.Popen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("launch")))
    manager = UpdateManager(tmp_path / "AgentWorkbench.exe", tmp_path / "updates")
    manager._download_and_install(selected, lambda: (_ for _ in ()).throw(AssertionError("shutdown")))
    assert manager.status()["state"] == "error"
    assert not (tmp_path / "updates" / selected["name"]).exists()


def test_update_api_is_owner_and_local_desktop_only(tmp_path: Path):
    app = create_app(tmp_path / "workbench.db", desktop_mode=True)
    app.state.shutdown_callback = lambda: None
    local = TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    assert local.get("/v1/local/update").status_code == 401
    setup = local.post("/auth/setup", json={"password": "long-enough-123",
                                            "confirmation": "long-enough-123"})
    csrf = setup.json()["csrf"]
    assert local.get("/v1/local/update").status_code == 200
    assert local.post("/v1/local/update").status_code == 403
    assert local.post("/v1/local/update", headers={"x-awb-csrf": csrf}).status_code == 409
    service = create_app(tmp_path / "service.db")
    remote = TestClient(service, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    remote.post("/auth/setup", json={"password": "long-enough-123",
                                     "confirmation": "long-enough-123"})
    assert remote.get("/v1/local/update").status_code == 401
