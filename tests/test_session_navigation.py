"""Key-message selection and safe local folder navigation."""

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from awb.activity import conversation, sync_activity
from awb.api import create_app
from awb.db import Database
from awb.reveal import open_folder, session_folder


def test_key_view_keeps_user_requests_and_last_answers(tmp_path: Path) -> None:
    source = tmp_path / 'codex' / 'sessions' / 'rollout.jsonl'
    source.parent.mkdir(parents=True)
    records = [{'type': 'session_meta', 'payload': {'id': 'session-1'}}]
    for number, role, body in (
        (1, 'user', '# AGENTS.md instructions\n<context>'),
        (2, 'user', '真实问题'),
        (3, 'assistant', '处理中'),
        (4, 'assistant', '第一轮结论'),
        (5, 'user', '继续检查'),
        (6, 'assistant', '第二轮结论'),
    ):
        records.append({'type': 'response_item',
                        'timestamp': f'2026-09-28T10:00:0{number}Z',
                        'payload': {'type': 'message', 'id': f'msg-{number}', 'role': role,
                                    'content': [{'type': 'input_text' if role == 'user'
                                                 else 'output_text', 'text': body}]}})
    source.write_text(''.join(json.dumps(row) + '\n' for row in records))
    db = Database(tmp_path / 'app.db')
    db.initialize()
    sync_activity(db, codex_root=source.parent.parent, claude_root=tmp_path / 'claude')
    assert conversation(db, 'codex', 'session-1', view='full')['count'] == 6
    key = conversation(db, 'codex', 'session-1', view='key')
    assert [row['body'] for row in key['items']] == [
        '真实问题', '第一轮结论', '继续检查', '第二轮结论']


def test_hermes_key_view_prefers_completed_answer(tmp_path: Path) -> None:
    source = tmp_path / 'state.db'
    with sqlite3.connect(source) as conn:
        conn.executescript('''CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,
            timestamp REAL,finish_reason TEXT,content TEXT);''')
        conn.executemany('INSERT INTO messages VALUES(?,?,?,?,?,?)', [
            (1, 'h1', 'user', 1, None, '问题'),
            (2, 'h1', 'assistant', 2, 'tool_calls', '检查中'),
            (3, 'h1', 'assistant', 3, 'stop', '最终答复'),
            (4, 'h1', 'assistant', 4, 'tool_calls', '迟到的过程记录'),
        ])
    db = Database(tmp_path / 'app.db')
    db.initialize()
    assert conversation(db, 'hermes', 'h1', hermes_path=source, view='full')['count'] == 4
    key = conversation(db, 'hermes', 'h1', hermes_path=source, view='key')
    assert [row['body'] for row in key['items']] == ['问题', '最终答复']


def test_folder_navigation_accepts_only_session_paths(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / 'logs' / 'session.jsonl'
    output = tmp_path / 'delivery' / 'article.md'
    source.parent.mkdir()
    output.parent.mkdir()
    source.touch()
    output.touch()
    detail = {'cwd': str(tmp_path), 'sources': [{'path': str(source)}],
              'file_events': [{'native_path': str(output)}]}
    assert session_folder(detail, 'source') == source.parent
    assert session_folder(detail, 'file', str(output)) == output.parent
    assert session_folder(detail, 'workspace') == tmp_path
    with pytest.raises(ValueError):
        session_folder(detail, 'file', str(tmp_path / 'unrelated.txt'))
    with pytest.raises(FileNotFoundError):
        session_folder({**detail, 'cwd': str(tmp_path / 'missing')}, 'workspace')
    launched = []
    monkeypatch.setattr('awb.reveal.subprocess.Popen', lambda command, **kwargs: launched.append(command))
    monkeypatch.setattr('awb.reveal.sys.platform', 'darwin')
    open_folder(output.parent)
    monkeypatch.setattr('awb.reveal.sys.platform', 'win32')
    open_folder(output.parent)
    assert launched == [['open', str(output.parent)], ['explorer.exe', str(output.parent)]]


def test_reveal_endpoint_checks_csrf_and_session_evidence(tmp_path: Path, monkeypatch) -> None:
    app = create_app(tmp_path / 'app.db', desktop_mode=True)
    folder = tmp_path / 'delivery'
    folder.mkdir()
    output = folder / 'article.md'
    output.touch()
    monkeypatch.setattr('awb.api.mvp_session_inspector', lambda *_args: {
        'cwd': str(tmp_path), 'sources': [], 'file_events': [{'native_path': str(output)}]})
    opened = []
    monkeypatch.setattr('awb.api.open_folder', opened.append)
    client = TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 50000))
    endpoint = '/v1/mvp/activity/sessions/codex/test/reveal'
    assert client.post(endpoint, json={'kind': 'file', 'path': str(output)}).status_code == 403
    csrf = client.get('/auth/me').json()['csrf']
    headers = {'x-awb-csrf': csrf}
    assert client.post(endpoint, json={'kind': 'file', 'path': str(tmp_path / 'other')},
                       headers=headers).status_code == 422
    response = client.post(endpoint, json={'kind': 'file', 'path': str(output)}, headers=headers)
    assert response.json() == {'opened': True, 'folder': str(folder)}
    assert opened == [folder]
