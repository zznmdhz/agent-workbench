"""Local desktop launcher for Windows and macOS."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from threading import Event, Thread

import httpx
import uvicorn

from . import __version__
from .db import Database


def default_db_path() -> Path:
    executable_dir = Path(sys.executable).resolve().parent
    if (executable_dir / "portable.flag").exists():
        return executable_dir / "data" / "agent-workbench.db"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "AgentWorkbench" / "data" / "agent-workbench.db"
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
        return result.status_code == 200 and body.get("status") == "ready" and body.get("app_version") == __version__
    except (httpx.HTTPError, ValueError):
        return False


def run_desktop(db_path: Path, port: int = 8765, browser: bool = True,
                reset_password: bool = False, tray: bool = True) -> None:
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
            # A just-stopped desktop server may leave this port in TIME_WAIT.
            # Uvicorn can reuse it; the preflight probe must use the same rule.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
        except OSError:
            raise RuntimeError(f"端口 {port} 已被其他程序占用，请检查后台状态或重启工作台。")

    if reset_password:
        if not _confirm_reset():
            return
        reset_owner_password(db_path)

    from .instance import lock
    with lock(db_path) as acquired:
        if not acquired:
            for _ in range(100):
                if _is_workbench(url):
                    if browser:
                        webbrowser.open(url)
                    return
                time.sleep(0.1)
            raise RuntimeError('后台正在启动但尚未就绪，请稍后重试')
        _serve(db_path, port, browser, tray)


def _serve(db_path: Path, port: int, browser: bool, tray: bool) -> None:
    url = f'http://127.0.0.1:{port}'
    log_path = db_path.parent / 'desktop.log'
    if log_path.exists() and log_path.stat().st_size > 5 * 1024 * 1024:
        log_path.replace(log_path.with_suffix('.previous.log'))

    log_file = open(log_path, "a", encoding="utf-8", buffering=1)
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
    background = service.state.background
    background.start()
    from . import autostart
    background.set(autostart=autostart.registered())

    def restart():
        if getattr(sys, 'frozen', False):
            subprocess.Popen([sys.executable, '--restart', '--no-browser'],
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        else:
            background.set(error='开发环境请由启动命令重启')
    service.state.restart_callback = restart
    icon = None
    if tray:
        from .tray import start_tray
        icon = start_tray(background, url, service.state.shutdown_callback, restart)

    def check_updates():
        if stop.wait(60):
            return
        while not stop.is_set():
            state = service.state.updater.check()
            if state['state'] == 'available':
                try:
                    service.state.updater.start(service.state.shutdown_callback)
                except ValueError:
                    pass
            stop.wait(6 * 3600)
    Thread(target=check_updates, name='awb-update-check', daemon=True).start()

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
        background.stop()
        if icon:
            icon.stop()
        sys.stdout, sys.stderr = previous_stdout, previous_stderr
        log_file.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Workbench desktop launcher")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--reset-password", action="store_true")
    parser.add_argument('--background', action='store_true')
    parser.add_argument('--register-background', action='store_true')
    parser.add_argument('--unregister-background', action='store_true')
    parser.add_argument('--restart', action='store_true')
    parser.add_argument('--no-tray', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        _show_error("端口号无效。")
        return
    try:
        from . import autostart
        if args.register_background:
            autostart.register(Path(sys.executable))
            return
        if args.unregister_background:
            autostart.unregister()
            try:
                with httpx.Client(timeout=3) as client:
                    client.post(f'http://127.0.0.1:{args.port}/auth/close-local')
            except httpx.HTTPError:
                pass
            return
        maintenance = default_db_path().parent.parent/'updates'/'maintenance.json'
        if args.background and maintenance.is_file():
            try:
                pending = json.loads(maintenance.read_text(encoding='utf-8'))
                if time.time()-pending['started_at'] < 15*60:
                    return  # The verified updater owns restart during this window.
            except (OSError, ValueError, KeyError, TypeError):
                pass
        url = f'http://127.0.0.1:{args.port}'
        if args.restart:
            with httpx.Client(timeout=5) as client:
                try:
                    csrf = client.get(url+'/auth/me').json()['csrf']
                    client.post(url+'/v1/local/shutdown', headers={'x-awb-csrf': csrf}).raise_for_status()
                except httpx.ConnectError:
                    pass
            for _ in range(150):
                if not _is_workbench(url):
                    break
                time.sleep(0.2)
        installed = ((os.name == 'nt' or sys.platform == 'darwin') and getattr(sys, 'frozen', False)
                     and args.db is None and args.port == 8765
                     and not (Path(sys.executable).parent/'portable.flag').exists())
        if installed and not args.background and not args.reset_password:
            if not _is_workbench(url):
                if not autostart.registered():
                    autostart.register(Path(sys.executable))
                autostart.start()
                for attempt in range(300):
                    if _is_workbench(url):
                        break
                    if attempt and attempt % 30 == 0:
                        autostart.start()
                    time.sleep(0.1)
                else:
                    raise RuntimeError('后台未能在 30 秒内启动，请查看数据目录中的 desktop.log')
            if not args.no_browser:
                webbrowser.open(url)
            return
        run_desktop(args.db or default_db_path(), args.port, not (args.no_browser or args.background),
                    args.reset_password, not args.no_tray)
    except Exception as exc:
        if not args.background:
            _show_error(f"工作台启动失败：{exc}\n请查看数据目录中的 desktop.log。")
        else:
            error_path = (args.db or default_db_path()).parent/'startup-error.log'
            error_path.parent.mkdir(parents=True, exist_ok=True)
            error_path.write_text(str(exc), encoding='utf-8')
        raise SystemExit(1) from exc
