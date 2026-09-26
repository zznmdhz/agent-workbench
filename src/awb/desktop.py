"""Windowless Windows launcher for the local workbench."""

from __future__ import annotations

import argparse
import os
import socket
import sys
import webbrowser
from pathlib import Path
from threading import Event, Thread

import httpx
import uvicorn

from .db import Database


def default_db_path() -> Path:
    executable_dir = Path(sys.executable).resolve().parent
    if (executable_dir / "portable.flag").exists():
        return executable_dir / "data" / "agent-workbench.db"
    app_data = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    return app_data / "AgentWorkbench" / "data" / "agent-workbench.db"


def _show_error(message: str) -> None:
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "Agent Workbench", 0x10)
    else:
        print(message, file=sys.stderr)


def _confirm_reset() -> bool:
    if os.name == "nt":
        import ctypes
        return ctypes.windll.user32.MessageBoxW(
            None, "将清除旧管理员密码和网页登录状态，保留所有工作台数据。是否继续？",
            "重设 Agent Workbench 密码", 0x24) == 6
    return False


def reset_owner_password(db_path: Path) -> None:
    store = Database(db_path)
    store.initialize()
    with store.tx() as conn:
        conn.execute("DELETE FROM web_sessions")
        conn.execute("DELETE FROM owner")


def _is_workbench(url: str) -> bool:
    try:
        with httpx.Client(timeout=1) as client:
            result = client.get(url + "/health/ready")
        body = result.json()
        return result.status_code == 200 and body.get("status") == "ready" and body.get("app_version") == "0.3.0"
    except (httpx.HTTPError, ValueError):
        return False


def run_desktop(db_path: Path, port: int = 8765, browser: bool = True,
                reset_password: bool = False) -> None:
    db_path = db_path.resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"http://127.0.0.1:{port}"
    if _is_workbench(url):
        if reset_password:
            _show_error("请先在网页登录页点击“关闭工作台”，再从开始菜单重设密码。")
            return
        if browser:
            webbrowser.open(url + "/")
        return
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            _show_error(f"端口 {port} 已被其他程序占用。请先关闭旧版工作台或占用该端口的程序，再重试。")
            return

    if reset_password:
        if not _confirm_reset():
            return
        reset_owner_password(db_path)

    log_file = open(db_path.parent / "desktop.log", "a", encoding="utf-8", buffering=1)
    previous_stdout, previous_stderr = sys.stdout, sys.stderr
    sys.stdout = log_file
    sys.stderr = log_file

    os.environ["AWB_DB_PATH"] = str(db_path)
    from .api import create_app

    service = create_app(db_path, desktop_mode=True)
    server = uvicorn.Server(uvicorn.Config(service, host="127.0.0.1", port=port,
                                          workers=1, access_log=False, log_level="warning"))
    service.state.shutdown_callback = lambda: setattr(server, "should_exit", True)
    stop = Event()

    if browser:
        def open_when_ready() -> None:
            for _ in range(100):
                if stop.wait(0.1):
                    return
                if _is_workbench(url):
                    webbrowser.open(url + "/")
                    return
        Thread(target=open_when_ready, name="awb-browser", daemon=True).start()
    try:
        server.run()
    finally:
        stop.set()
        sys.stdout, sys.stderr = previous_stdout, previous_stderr
        log_file.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Workbench desktop launcher")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--reset-password", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        _show_error("端口号无效。")
        return
    try:
        run_desktop(args.db or default_db_path(), args.port, not args.no_browser,
                    args.reset_password)
    except Exception as exc:
        _show_error(f"工作台启动失败：{exc}\n请查看数据目录中的 desktop.log。")
