"""Elapsed-time overlap, day clipping and local conversation evidence."""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from awb.activity import conversation, dashboard, sync_activity
from awb.db import Database


def _hermes(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript('''CREATE TABLE sessions(id TEXT,title TEXT,display_name TEXT);
            CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,timestamp REAL,
                                  finish_reason TEXT,content TEXT);''')
        conn.execute('INSERT INTO sessions VALUES(?,?,?)', ('hermes-1', 'Hermes sample', None))
        for ident, role, stamp, reason in (
            (1, 'user', '2026-09-28T10:10:00+00:00', None),
            (2, 'assistant', '2026-09-28T10:40:00+00:00', 'stop'),
        ):
            conn.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)',
                         (ident, 'hermes-1', role,
                          datetime.fromisoformat(stamp).timestamp(), reason, f'{role} body'))


def test_parallel_union_and_hermes_estimate(tmp_path: Path) -> None:
    db = Database(tmp_path / 'app.db')
    db.initialize()
    with db.tx() as conn:
        conn.execute('INSERT INTO mvp_activity_runs VALUES(?,?,?,?,?,?)',
                     ('codex', 'codex-1', 'turn-1', '2026-09-28T10:00:00Z',
                      '2026-09-28T10:20:00Z', 'source.jsonl'))
    hermes = tmp_path / 'hermes.db'
    _hermes(hermes)
    result = dashboard(db, '2026-09-28', '2026-09-28', 'UTC', sync=False,
                       heatmap_view='day', hermes_path=hermes,
                       codex_root=tmp_path / 'codex', claude_root=tmp_path / 'claude')
    assert result['summary'] == {
        'agent_ms': 50 * 60_000, 'wall_ms': 40 * 60_000,
        'verified_ms': 20 * 60_000, 'runs': 2,
    }
    assert result['by_agent']['hermes']['wall_ms'] == 30 * 60_000
    assert result['heatmap'][10]['wall_ms'] == 40 * 60_000
    assert {row['agent'] for row in result['sessions']} == {'codex', 'hermes'}
    daily = conversation(db, 'hermes', 'hermes-1', day='2026-09-28', tz='UTC',
                         hermes_path=hermes)
    assert daily['count'] == 2 and [row['role'] for row in daily['items']] == ['user', 'assistant']


def test_cross_midnight_interval_is_split_by_local_day(tmp_path: Path) -> None:
    db = Database(tmp_path / 'app.db')
    db.initialize()
    with db.tx() as conn:
        conn.execute('INSERT INTO mvp_activity_runs VALUES(?,?,?,?,?,?)',
                     ('codex', 'codex-1', 'turn-1', '2026-09-28T15:50:00Z',
                      '2026-09-28T16:10:00Z', 'source.jsonl'))
    first = dashboard(db, '2026-09-28', '2026-09-28', 'Asia/Hong_Kong', sync=False,
                      hermes_path=tmp_path / 'missing.db', codex_root=tmp_path,
                      claude_root=tmp_path)
    second = dashboard(db, '2026-09-29', '2026-09-29', 'Asia/Hong_Kong', sync=False,
                       hermes_path=tmp_path / 'missing.db', codex_root=tmp_path,
                       claude_root=tmp_path)
    assert first['summary']['agent_ms'] == second['summary']['agent_ms'] == 10 * 60_000
    assert first['summary']['wall_ms'] + second['summary']['wall_ms'] == 20 * 60_000


def test_codex_source_duration_and_message_body_on_demand(tmp_path: Path) -> None:
    root = tmp_path / 'codex'
    file = root / 'sessions' / '2026' / '09' / '28' / 'rollout.jsonl'
    file.parent.mkdir(parents=True)
    rows = [
        {'type': 'session_meta', 'payload': {'id': 'session-1'}},
        {'type': 'event_msg', 'timestamp': '2026-09-28T10:00:00Z',
         'payload': {'type': 'task_started', 'turn_id': 'turn-1',
                     'root_turn_id': 'turn-1', 'started_at': 1790590000}},
        {'type': 'response_item', 'timestamp': '2026-09-28T10:00:01Z',
         'payload': {'type': 'message', 'id': 'msg-1', 'role': 'user',
                     'content': [{'type': 'input_text', 'text': 'Hello local'}]}},
        {'type': 'event_msg', 'timestamp': '2026-09-28T10:00:10.500Z',
         'payload': {'type': 'task_complete', 'turn_id': 'turn-1',
                     'duration_ms': 10500, 'started_at': 1790590000,
                     'completed_at': 1790590010}},
    ]
    file.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    db = Database(tmp_path / 'app.db')
    db.initialize()
    state = sync_activity(db, codex_root=root, claude_root=tmp_path / 'claude')
    assert state['updated_files'] == 1 and state['read_errors'] == 0
    result = dashboard(db, '2026-09-28', '2026-09-28', 'UTC', sync=False,
                       hermes_path=tmp_path / 'missing.db', codex_root=root,
                       claude_root=tmp_path / 'claude')
    assert result['summary']['verified_ms'] == 10_500
    assert conversation(db, 'codex', 'session-1', day='2026-09-28', tz='UTC')['items'][0]['body'] == 'Hello local'
    with db.read() as conn:
        assert 'body' not in {row[1] for row in conn.execute('PRAGMA table_info(mvp_activity_messages)')}
    file.unlink()
    assert sync_activity(db, codex_root=root, claude_root=tmp_path / 'claude')['removed_files'] == 1
    assert conversation(db, 'codex', 'session-1')['count'] == 0
