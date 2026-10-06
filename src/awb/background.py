"""One collector worker with cheap, independent status and explicit pause semantics."""
from __future__ import annotations

import json
import os
import time
from threading import Event, Lock, Thread

from . import __version__, archive
from .db import Database


class Background:
    def __init__(self, db: Database, interval: float = 120):
        self.db, self.interval = db, interval
        self.stop_event, self.wake = Event(), Event()
        self.lock = Lock()
        self.started = time.monotonic()
        self.status_file = db.path.parent / 'background-status.json'
        try:
            saved = json.loads(self.status_file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            saved = {}
        self.state = {'collector_state': 'starting', 'last_success': saved.get('last_success'),
                      'last_attempt': None, 'error': None, 'scan_errors': 0, 'paused': False,
                      'tray_visible': False, 'autostart': False, 'scan_count': 0}
        self.thread: Thread | None = None

    def snapshot(self) -> dict:
        with self.lock:
            state = self.state.copy()
        state.update(version=__version__, pid=os.getpid(), uptime_seconds=int(time.monotonic()-self.started),
                     scan_interval_seconds=self.interval, managed=self.thread is not None)
        from .build_info import info
        state['build'] = info()
        return state

    def set(self, **values) -> None:
        with self.lock:
            self.state.update(values)

    def start(self) -> None:
        if self.thread is not None:
            return
        self.thread = Thread(target=self._loop, name='awb-collector', daemon=True)
        self.thread.start()

    def request(self) -> dict:
        self.wake.set()
        return self.snapshot()

    def pause(self, value: bool) -> dict:
        current = self.snapshot()
        self.set(paused=value, collector_state='paused' if value else
                 'scanning' if current.get('scan_active') else 'waiting')
        self.wake.set()
        return self.snapshot()

    def scan_once(self) -> None:
        self.set(collector_state='scanning', scan_active=True, last_attempt=archive.utc_now(), error=None)
        try:
            result = archive.refresh(self.db, force=True)
            errors = sum(int(value.get('read_errors', 0)) for value in result.get('scan', {}).values())
            now = archive.utc_now()
            self.set(collector_state='paused' if self.snapshot()['paused'] else
                     'warning' if errors else 'running', scan_errors=errors,
                     last_success=now, scan_count=self.snapshot()['scan_count']+1,
                     error='部分原始记录读取失败' if errors else None)
            temporary = self.status_file.with_suffix('.tmp')
            temporary.write_text(json.dumps({'last_success': now}), encoding='utf-8')
            temporary.replace(self.status_file)
        except Exception as exc:
            # A damaged input does not terminate the HTTP server or discard its archive.
            self.set(collector_state='error', error=str(exc)[:240])
        finally:
            self.set(scan_active=False)

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            self.wake.clear()
            if not self.snapshot()['paused']:
                self.scan_once()
            self.wake.wait(self.interval)

    def stop(self) -> None:
        self.stop_event.set()
        self.wake.set()


def status_label(state: dict) -> str:
    return {'starting': '后台已启动 · 首次采集中', 'scanning': '后台运行中 · 正在采集',
            'running': '后台运行中', 'waiting': '后台运行中 · 等待采集',
            'paused': '后台运行中 · 采集已暂停', 'warning': '后台运行中 · 部分来源异常',
            'error': '后台运行中 · 采集异常'}.get(state.get('collector_state'), '后台运行中')
