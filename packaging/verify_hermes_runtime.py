"""Verify default/profile capture through the actual frozen desktop HTTP path."""
from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path


def seed(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as conn:
        conn.executescript('''
            CREATE TABLE sessions(id TEXT PRIMARY KEY,title TEXT,display_name TEXT,cwd TEXT);
            CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,
                timestamp REAL,content TEXT,finish_reason TEXT);
            INSERT INTO sessions VALUES('same-id','fixture',NULL,NULL);
        ''')
        conn.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)',
                     (1, 'same-id', 'user', 1792650000, text, None))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    executable = args.executable.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='frozen-hermes-', dir=args.output.parent) as raw:
        root = Path(raw)
        home = root / 'hermes'
        seed(home / 'state.db', '默认配置中文对话')
        seed(home / 'profiles/worker/state.db', '工作配置中文对话')
        sync = root / 'unused-synthetic-nas'
        sync.mkdir()
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() in {'PATH', 'HOME', 'USERPROFILE', 'SYSTEMROOT', 'SYSTEMDRIVE',
                                          'WINDIR', 'TEMP', 'TMP', 'LANG', 'LC_ALL'}}
        environment.update(HERMES_HOME=str(home), CODEX_HOME=str(root / 'no-codex'),
                           CLAUDE_CONFIG_DIR=str(root / 'no-claude'), AWB_SYNC_ROOT=str(sync))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = f'http://127.0.0.1:{port}'

        def get(path, params=None):
            url = base + path + ('?' + urllib.parse.urlencode(params) if params else '')
            with opener.open(url, timeout=3) as response:
                return json.load(response)

        with (root / 'child.log').open('wb') as log:
            child = subprocess.Popen([str(executable), '--db', str(root / 'local/agent-workbench.db'),
                                      '--port', str(port), '--no-browser', '--no-tray'],
                                     cwd=root, env=environment, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 45
                sessions = None
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        raise RuntimeError('Frozen desktop exited before profile verification')
                    try:
                        value = get('/v1/mvp/activity/sessions', {'day': '2026-10-22',
                                    'through': '2026-10-22', 'tz': 'Asia/Shanghai', 'agent': 'hermes'})
                        if value.get('session_count') == 2:
                            sessions = value['sessions']
                            break
                    except (OSError, ValueError):
                        pass
                    time.sleep(0.2)
                if sessions is None:
                    raise RuntimeError('Frozen desktop did not archive both profile sessions')
                ids = {item['native_id'] for item in sessions}
                if ids != {'same-id', 'profile:worker:same-id'}:
                    raise RuntimeError('Frozen desktop profile identity mismatch')
                archive = get('/v1/archive/status')
                device = archive['local_device_id']
                with sqlite3.connect(root / 'local/agent-workbench.db') as conn:
                    bodies = {row[0]: json.loads(row[1])['body'] for row in conn.execute(
                        "SELECT native_id,payload_json FROM archive_facts WHERE kind='message' AND agent='hermes'")}
                if bodies != {'same-id': '默认配置中文对话', 'profile:worker:same-id': '工作配置中文对话'}:
                    raise RuntimeError('Frozen desktop profile body mismatch')
                packets = list((sync / 'AgentWorkbench-data-exchange/devices' / device / 'packets').glob('*.json.gz'))
                if not packets:
                    raise RuntimeError('Frozen desktop did not export synthetic exchange packets')
                result = {'success': True, 'default_and_profile_sessions': True,
                          'chinese_bodies': True, 'distinct_session_ids': True,
                          'synthetic_exchange_exported': True, 'real_user_data_accessed': False}
                args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
                print(json.dumps(result))
            finally:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)


if __name__ == '__main__':
    main()
