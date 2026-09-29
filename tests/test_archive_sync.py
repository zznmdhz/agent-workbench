from pathlib import Path

from awb import archive, archive_views
from awb.db import Database


def test_two_device_exchange_and_retained_conversation(tmp_path: Path, monkeypatch):
    root = tmp_path / 'Sync_AI'
    (root / '.stfolder').mkdir(parents=True)
    monkeypatch.setenv('AWB_SYNC_ROOT', str(root))
    mac, win = Database(tmp_path / 'mac.db'), Database(tmp_path / 'win.db')
    for db in (mac, win):
        db.initialize()
    mac_id, win_id = archive.initialize(mac), archive.initialize(win)
    with mac.tx() as conn:
        archive._upsert_local(conn, mac_id, 'session', 'codex', 'thread-1', 'thread-1',
                              '2026-09-28T10:01:00Z', {'title': '写脚本', 'sources': ['/missing/log.jsonl'],
                              'record_bytes': 300, 'cwd': '/missing'})
        archive._upsert_local(conn, mac_id, 'message', 'codex', 'thread-1', 'msg-1',
                              '2026-09-28T10:00:00Z', {'message_id': 'msg-1', 'role': 'user',
                              'body': '请写脚本', 'preview': '请写脚本', 'final': 1})
        archive._upsert_local(conn, mac_id, 'message', 'codex', 'thread-1', 'msg-2',
                              '2026-09-28T10:01:00Z', {'message_id': 'msg-2', 'role': 'assistant',
                              'body': '已完成脚本', 'preview': '已完成脚本', 'final': 1})
        archive._upsert_local(conn, mac_id, 'file', 'codex', 'thread-1', 'file-1',
                              '2026-09-28T10:01:00Z', {'native_path': '/missing/script.py',
                              'relation': 'created', 'evidence': 'write', 'source_file': '/missing/log.jsonl'})
    assert archive.export_packets(mac, mac_id) == 1
    assert archive.import_packets(win, win_id) == 1
    assert archive.import_packets(win, win_id) == 0
    result = archive_views.conversation(win, 'codex', 'thread-1', device_id=mac_id)
    assert [row['body'] for row in result['items']] == ['请写脚本', '已完成脚本']
    detail = archive_views.inspector(win, 'codex', 'thread-1', device_id=mac_id)
    assert detail['file_events'][0]['native_path'] == '/missing/script.py'
    assert detail['file_events'][0]['current_state'] == 'remote'
    assert archive.status(win)['devices'][0]['facts'] == 4 or any(
        row['id'] == mac_id and row['facts'] == 4 for row in archive.status(win)['devices'])
    with win.tx() as conn:
        archive._upsert_local(conn, win_id, 'message', 'claude', 'win-thread', 'win-msg',
                              '2026-09-28T11:00:00Z', {'message_id': 'win-msg', 'role': 'user',
                              'body': 'Windows 对话', 'preview': 'Windows 对话', 'final': 1})
    assert archive.export_packets(win, win_id) == 1
    assert archive.import_packets(mac, mac_id) == 1
    assert archive_views.conversation(mac, 'claude', 'win-thread', device_id=win_id)['items'][0][
        'body'] == 'Windows 对话'


def test_all_device_view_deduplicates_copied_native_history(tmp_path: Path):
    db = Database(tmp_path / 'view.db')
    db.initialize()
    local = archive.initialize(db)
    with db.tx() as conn:
        for device in (local, 'other-device'):
            archive._upsert_local(conn, device, 'usage', 'codex', 'same-thread', 'same-request',
                                  '2026-09-28T10:00:00Z', {'native_session_id': 'same-thread',
                                  'request_id': 'same-request', 'model': 'm', 'input_tokens': 10,
                                  'cached_input_tokens': 0, 'output_tokens': 1,
                                  'occurred_at': '2026-09-28T10:00:00Z'})
    all_view = archive_views.usage(db, '2026-09-28', '2026-09-28', 'UTC')
    other_view = archive_views.usage(db, '2026-09-28', '2026-09-28', 'UTC',
                                     device_id='other-device')
    assert all_view['summary']['total_tokens'] == other_view['summary']['total_tokens'] == 11


def test_copied_database_gets_distinct_writer_id(tmp_path: Path):
    db = Database(tmp_path / 'copied.db')
    db.initialize()
    first = archive.initialize(db)
    with db.tx() as conn:
        conn.execute("UPDATE archive_settings SET value='OtherOS:other-host' WHERE key='local_host'")
    second = archive.initialize(db)
    assert first != second
    assert archive.local_device_id(db) == second


def test_model_report_keeps_hermes_off_dated_charts(tmp_path: Path):
    db = Database(tmp_path / 'report.db')
    db.initialize()
    device = archive.initialize(db)
    with db.tx() as conn:
        archive._upsert_local(conn, device, 'usage', 'codex', 'c1', 'r1',
                              '2026-09-28T10:00:00Z', {'request_id': 'r1', 'model': 'sol',
                              'input_tokens': 100, 'cached_input_tokens': 40,
                              'output_tokens': 20, 'occurred_at': '2026-09-28T10:00:00Z'})
        archive._upsert_local(conn, device, 'usage', 'hermes', 'h1', 'h1/m',
                              '2026-09-28T11:00:00Z', {'model': 'm', 'first_seen': 1790593200,
                              'last_seen': 1790593260, 'input_tokens': 80,
                              'cache_read_tokens': 20, 'cache_write_tokens': 0,
                              'output_tokens': 10, 'api_call_count': 2})
    report = archive_views.model_report(db, '2026-09-28', '2026-09-28', 'UTC')
    assert report['models'][0]['model'] == 'sol'
    assert report['models'][0]['cache_hit_rate'] == 0.4
    assert all(row['agent'] != 'hermes' for row in report['series'] + report['hours'])
    assert len(report['points']) == 1


def test_session_browser_advanced_filters(tmp_path: Path):
    db = Database(tmp_path / 'sessions.db')
    db.initialize()
    device = archive.initialize(db)
    with db.tx() as conn:
        for native, body, minute in [('short', '简短', '00'), ('long', '很长的会话内容' * 20, '10')]:
            archive._upsert_local(conn, device, 'session', 'codex', native, native,
                                  f'2026-09-28T10:{minute}:00Z',
                                  {'title': native, 'sources': [], 'record_bytes': len(body)})
            archive._upsert_local(conn, device, 'message', 'codex', native, native + '-msg',
                                  f'2026-09-28T10:{minute}:00Z',
                                  {'message_id': native + '-msg', 'role': 'user',
                                   'body': body, 'preview': body, 'final': 1})
        archive._upsert_local(conn, device, 'file', 'codex', 'long', 'file-1',
                              '2026-09-28T10:10:00Z', {'native_path': '/tmp/result.md',
                              'relation': 'created', 'evidence': 'write', 'source_file': '/tmp/log'})
    result = archive_views.browser(db, '2026-09-28', '2026-09-28', 'UTC',
                                   sort='text_desc', min_text=100, has_files=True)
    assert [row['native_id'] for row in result['sessions']] == ['long']
    assert result['sessions'][0]['file_count'] == 1
    assert archive_views.browser(db, '2026-09-28', '2026-09-28', 'UTC',
                                 query='result.md', search_in='file')['session_count'] == 1
