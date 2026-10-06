"""Lifecycle and data-trust regressions for the windowless v0.8 release."""

from fastapi.testclient import TestClient

from awb import archive, archive_views
from awb.api import create_app
from awb.autostart import mac_config
from awb.background import Background
from awb.db import Database
from awb.instance import lock


def database(tmp_path):
    db = Database(tmp_path/'app.db')
    db.initialize()
    archive.initialize(db)
    return db


def test_collector_failure_does_not_hide_http_liveness_and_recovers(tmp_path, monkeypatch):
    db = database(tmp_path)
    worker = Background(db)
    monkeypatch.setattr(archive, 'refresh', lambda *a, **k: (_ for _ in ()).throw(OSError('source unavailable')))
    worker.scan_once()
    assert worker.snapshot()['collector_state'] == 'error'
    assert worker.snapshot()['last_success'] is None
    monkeypatch.setattr(archive, 'refresh', lambda *a, **k: {'scan': {'activity': {'read_errors': 0}}})
    worker.scan_once()
    assert worker.snapshot()['collector_state'] == 'running'
    assert worker.snapshot()['error'] is None
    assert Background(db).snapshot()['last_success'] == worker.snapshot()['last_success']


def test_pause_survives_scan_completion(tmp_path, monkeypatch):
    worker = Background(database(tmp_path))
    def scan(*a, **k):
        worker.pause(True)
        return {}
    monkeypatch.setattr(archive, 'refresh', scan)
    worker.scan_once()
    assert worker.snapshot()['collector_state'] == 'paused'
    assert worker.snapshot()['paused']


def test_isolated_databases_do_not_share_collection_status(tmp_path, monkeypatch):
    first = Background(database(tmp_path))
    monkeypatch.setattr(archive, 'refresh', lambda *a, **k: {})
    first.scan_once()
    second = Background(Database(tmp_path/'another.db'))
    assert first.snapshot()['last_success']
    assert second.snapshot()['last_success'] is None


def test_single_instance_lock_is_released_after_exit(tmp_path):
    with lock(tmp_path/'app.db') as first:
        assert first
        with lock(tmp_path/'app.db') as second:
            assert not second
    with lock(tmp_path/'app.db') as again:
        assert again


def test_background_status_does_not_trigger_a_scan_and_writes_require_csrf(tmp_path, monkeypatch):
    app = create_app(tmp_path/'app.db', desktop_mode=True)
    monkeypatch.setattr(archive, 'refresh', lambda *a, **k: (_ for _ in ()).throw(AssertionError('scan')))
    client = TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 50000))
    assert client.get('/v1/local/background').status_code == 200
    assert client.post('/v1/local/background/pause').status_code == 403
    csrf = client.get('/auth/me').json()['csrf']
    assert client.post('/v1/local/background/pause', headers={'x-awb-csrf': csrf}).json()['paused']
    assert not client.post('/v1/local/background/resume', headers={'x-awb-csrf': csrf}).json()['paused']
    assert client.post('/v1/local/background/unknown', headers={'x-awb-csrf': csrf}).status_code == 422


def test_first_import_serves_pending_data_without_waiting_for_worker(tmp_path, monkeypatch):
    app = create_app(tmp_path/'app.db', desktop_mode=True)
    app.state.background.thread = object()  # A running worker owns first scan.
    monkeypatch.setattr(archive, 'refresh_if_empty', lambda *a: (_ for _ in ()).throw(AssertionError('blocking')))
    client = TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 50000))
    for path in ('usage', 'activity', 'activity/sessions'):
        assert client.get('/v1/mvp/'+path, params={'day': '2026-10-06'}).status_code == 200


def test_imported_synthetic_spans_do_not_inflate_verified_execution(tmp_path):
    db = database(tmp_path)
    device = archive.local_device_id(db)
    with db.tx() as conn:
        for ident, start in [('external-import-turn-1', '2026-01-01T00:00:00Z'),
                             ('native-task', '2026-09-28T10:00:00Z')]:
            archive._upsert_local(conn, device, 'run', 'codex', 'session', ident, start,
                {'run_id': ident, 'start_at': start, 'end_at': '2026-09-28T10:20:00Z'})
    data = archive_views.activity(db, '2026-01-01', '2026-10-01', 'UTC')
    assert data['summary']['verified_ms'] == 20*60_000
    assert data['summary']['agent_ms'] == 20*60_000


def test_monthly_long_range_heatmaps_and_models_reconcile(tmp_path):
    db = database(tmp_path)
    device = archive.local_device_id(db)
    with db.tx() as conn:
        for ident, at in [('one', '2025-02-12T10:00:00Z'), ('two', '2026-09-28T10:00:00Z')]:
            archive._upsert_local(conn, device, 'usage', 'codex', 'session', ident, at,
                {'request_id': ident, 'input_tokens': 100, 'cached_input_tokens': 20,
                 'output_tokens': 10, 'model': 'test'})
    result = archive_views.usage(db, '2025-01-01', '2026-12-31', 'UTC', heatmap_view='custom')
    assert sum(r['total_tokens'] for r in result['heatmap']) == 220
    assert sum(r['total_tokens'] for r in result['trend']) == 220
    report = archive_views.model_report(db, '2025-01-01', '2026-12-31', 'UTC')
    assert sum(r['total_tokens'] for r in report['series']) == 220
    assert all(r['period'] in report['periods'] for r in report['series'])
    archive_views.activity(db, '2025-01-01', '2026-12-31', 'UTC', heatmap_view='custom')


def test_message_projection_preserves_unicode_counts_without_decoding_bodies(tmp_path):
    db = database(tmp_path)
    device = archive.local_device_id(db)
    with db.tx() as conn:
        archive._upsert_local(conn, device, 'message', 'codex', 'session', 'one', '2026-09-28T10:00:00Z',
            {'message_id': 'one', 'body': '你好 🌏', 'preview': '你好', 'role': 'user', 'final': 1})
    row = archive.facts(db, 'message', device, summaries=True)[0]
    assert row['body_chars'] == len('你好 🌏')
    assert 'body' not in row and 'payload_json' not in row
    result = archive_views.browser(db, '2026-09-28', '2026-09-28', 'UTC')
    assert result['sessions'][0]['text_chars'] == len('你好 🌏')


def test_mac_launchagent_uses_logged_in_user_and_does_not_open_browser(tmp_path):
    config = mac_config(tmp_path/'AgentWorkbench')
    assert config['ProgramArguments'][-1] == '--background'
    assert config['KeepAlive'] == {'SuccessfulExit': False}
    assert config['RunAtLoad']


def test_message_summary_backfills_existing_archive_and_tracks_edits(tmp_path):
    db = database(tmp_path)
    device = archive.local_device_id(db)
    with db.tx() as conn:
        for name in ('insert', 'update', 'delete'):
            conn.execute('DROP TRIGGER archive_summary_'+name)
        conn.execute('DROP TABLE archive_message_summaries')
        conn.execute("DELETE FROM archive_settings WHERE key='message_summary_v1'")
        archive._upsert_local(conn, device, 'message', 'codex', 'session', 'one',
                              '2026-09-28T10:00:00Z', {'body': 'old', 'role': 'user'})
    archive.initialize(db)
    assert archive.facts(db, 'message', summaries=True)[0]['body_chars'] == 3
    with db.tx() as conn:
        archive._upsert_local(conn, device, 'message', 'codex', 'session', 'one',
                              '2026-09-28T10:00:00Z', {'body': 'new text', 'role': 'user'})
    assert archive.facts(db, 'message', summaries=True)[0]['body_chars'] == 8
    with db.tx() as conn:
        conn.execute("DELETE FROM archive_facts WHERE kind='message'")
    assert archive.facts(db, 'message', summaries=True) == []


def test_incremental_archive_retains_unchanged_sources_and_updates_changed_source(tmp_path, monkeypatch):
    db = database(tmp_path)
    device = archive.local_device_id(db)
    monkeypatch.setenv('CODEX_HOME', str(tmp_path/'empty'))
    monkeypatch.setenv('HERMES_STATE_DB', str(tmp_path/'missing.db'))
    with db.tx() as conn:
        for ident in ('one', 'two'):
            conn.execute('INSERT INTO mvp_activity_files VALUES(?,?,?,?,?,?)',
                         ('codex', ident, 100, 1, 'ok', '3'))
            conn.execute('INSERT INTO mvp_activity_messages VALUES(?,?,?,?,?,?,?,?,?,?)',
                         ('codex', ident, ident, '2026-09-28T10:00:00Z', 'user', 1, ident, ident, 0, ident))
    archive.archive_local(db, device)
    assert archive.archive_local(db, device)['changed_facts'] == 0
    with db.tx() as conn:
        conn.execute("UPDATE mvp_activity_files SET modified_ns=2 WHERE source_file='one'")
        conn.execute("UPDATE mvp_activity_messages SET search_body='updated' WHERE source_file='one'")
    assert archive.archive_local(db, device)['changed_facts'] == 1
    bodies = {r['native_id']: r['body'] for r in archive.facts(db, 'message')}
    assert bodies == {'one': 'updated', 'two': 'two'}


def test_isolated_package_runs_never_register_real_user_autostart(tmp_path, monkeypatch):
    import sys

    from awb import autostart, desktop
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'argv', ['AgentWorkbench', '--db', str(tmp_path/'test.db'), '--port', '8767', '--no-browser'])
    monkeypatch.setattr(autostart, 'register', lambda *a: (_ for _ in ()).throw(AssertionError('register')))
    started = []
    monkeypatch.setattr(desktop, 'run_desktop', lambda *args: started.append(args))
    desktop.main()
    assert started and started[0][1] == 8767
