import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from awb import archive, archive_views
from awb.db import Database
from awb.hermes_archive import CHECKPOINT, native_key, sources
from awb.multi_usage import hermes_db_path

AT = 1792650000


def source(path: Path, label='default', *, messages=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript('''
            CREATE TABLE sessions(id TEXT PRIMARY KEY,title TEXT,display_name TEXT,cwd TEXT);
            CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,
                timestamp REAL,content TEXT,finish_reason TEXT,tool_calls TEXT,
                tool_call_id TEXT,tool_name TEXT);
            CREATE TABLE session_model_usage(session_id TEXT,model TEXT,task TEXT,
                api_call_count INTEGER,input_tokens INTEGER,output_tokens INTEGER,
                cache_read_tokens INTEGER,cache_write_tokens INTEGER,first_seen REAL,last_seen REAL);
        ''')
        conn.execute('INSERT INTO sessions VALUES(?,?,?,?)', ('same-id', label, None, '/project'))
        if messages:
            conn.executemany('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)', [
                (1, 'same-id', 'user', AT, label + ' question', None, None, None, None),
                (2, 'same-id', 'assistant', AT + 1, label + ' answer', 'stop',
                 json.dumps([{'id': 'call-1', 'function': {'name': 'write_file',
                             'arguments': json.dumps({'path': 'result.txt'})}}]), None, None),
                (3, 'same-id', 'tool', AT + 2, '{"verified":true}', None, None, 'call-1', 'write_file'),
            ])
            conn.execute('INSERT INTO session_model_usage VALUES(?,?,?,?,?,?,?,?,?,?)',
                         ('same-id', 'model', 'chat', 1, 10, 1, 0, 0, AT, AT + 2))
        conn.commit()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home = tmp_path / 'hermes'
    monkeypatch.delenv('HERMES_STATE_DB', raising=False)
    monkeypatch.setenv('HERMES_HOME', str(home))
    db = Database(tmp_path / 'archive.db')
    db.initialize()
    device = archive.initialize(db)
    return home, db, device


def test_profiles_archive_all_fact_types_without_id_collisions_and_replay(setup, tmp_path):
    home, db, device = setup
    for profile in ['', 'worker', 'analyst']:
        source(home / ('profiles/' + profile if profile else '') / 'state.db', profile or 'default')
    first = archive.archive_local(db, device)
    assert first['hermes']['scanned'] == 3
    assert first['hermes']['read_errors'] == 0
    assert first['changed_facts'] == 15
    browser = archive_views.browser(db, '2026-10-22', '2026-10-22', 'Asia/Shanghai', agent='hermes')
    assert {row['native_id'] for row in browser['sessions']} == {
        'same-id', 'profile:worker:same-id', 'profile:analyst:same-id'}
    with db.read() as conn:
        assert conn.execute("SELECT count(*) FROM archive_facts WHERE kind='file'").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM archive_facts WHERE kind='usage'").fetchone()[0] == 3
    second = archive.archive_local(db, device)
    assert second['changed_facts'] == 0
    assert second['hermes']['unchanged'] == 3
    assert archive.export_packets(db, device) > 0
    other = Database(tmp_path / 'other.db')
    other.initialize()
    other_device = archive.initialize(other)
    assert archive.import_packets(other, other_device) > 0
    assert archive.import_packets(other, other_device) == 0
    for profile in ['', 'worker', 'analyst']:
        detail = archive_views.conversation(other, 'hermes', native_key(profile, 'same-id'), device_id=device)
        assert [item['body'] for item in detail['items']] == [
            (profile or 'default') + ' question', (profile or 'default') + ' answer']
    usage = archive_views.usage(other, '2026-10-22', '2026-10-22', 'Asia/Shanghai')
    assert usage['summary']['total_tokens'] == 33


def test_new_profile_is_discovered_when_default_is_unchanged(setup):
    home, db, device = setup
    source(home / 'state.db')
    archive.archive_local(db, device)
    source(home / 'profiles/worker/state.db', 'worker')
    result = archive.archive_local(db, device)
    assert result['hermes']['scanned'] == result['hermes']['unchanged'] == 1
    assert archive_views.conversation(db, 'hermes', 'profile:worker:same-id', device_id=device)['count'] == 2


def test_profile_wal_updates_are_captured_with_unchanged_database_file(setup):
    home, db, device = setup
    path = home / 'profiles/worker/state.db'
    source(path, 'worker')
    with closing(sqlite3.connect(path)) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        archive.archive_local(db, device)
        before = path.stat().st_mtime_ns
        writer.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)',
                       (4, 'same-id', 'user', AT + 10, 'new WAL question', None, None, None, None))
        writer.commit()
        assert path.stat().st_mtime_ns == before
        result = archive.archive_local(db, device)
        assert result['hermes']['scanned'] == 1
        assert archive_views.conversation(db, 'hermes', 'profile:worker:same-id', device_id=device)['count'] == 3


def test_bad_profile_does_not_hide_healthy_sources_or_advance_its_checkpoint(setup):
    home, db, device = setup
    source(home / 'state.db')
    bad = home / 'profiles/broken/state.db'
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b'not SQLite')
    result = archive.archive_local(db, device)
    assert result['hermes']['read_errors'] == 1
    assert result['hermes']['scanned'] == 1
    assert archive_views.conversation(db, 'hermes', 'same-id', device_id=device)['count'] == 2
    with db.read() as conn:
        saved = json.loads(conn.execute('SELECT value FROM archive_settings WHERE key=?', (CHECKPOINT,)).fetchone()[0])
    assert len(saved) == 1
    bad.unlink()
    source(bad, 'recovered')
    assert archive.archive_local(db, device)['hermes']['scanned'] == 1
    assert archive_views.conversation(db, 'hermes', 'profile:broken:same-id', device_id=device)['count'] == 2


def test_empty_profile_is_checkpointed_and_metadata_is_retained(setup):
    home, db, device = setup
    path = home / 'profiles/empty/state.db'
    source(path, messages=False)
    assert archive.archive_local(db, device)['hermes']['scanned'] == 1
    assert archive.archive_local(db, device)['hermes']['unchanged'] == 1
    path.unlink()
    with db.read() as conn:
        assert conn.execute("SELECT count(*) FROM archive_facts WHERE native_id='profile:empty:same-id'").fetchone()[0] == 1


def test_explicit_database_override_remains_single_source(setup, monkeypatch):
    home, db, device = setup
    source(home / 'state.db')
    source(home / 'profiles/worker/state.db')
    monkeypatch.setenv('HERMES_STATE_DB', str(home / 'state.db'))
    assert hermes_db_path() == home / 'state.db'
    assert archive.archive_local(db, device)['hermes']['sources'] == 1


def test_profile_symlink_alias_is_not_counted_twice(setup):
    home, db, device = setup
    path = home / 'state.db'
    source(path)
    alias = home / 'profiles/alias'
    alias.parent.mkdir(parents=True)
    try:
        alias.symlink_to(home, target_is_directory=True)
    except OSError:
        pytest.skip('OS does not allow unprivileged directory symlinks')
    assert len(sources(path)) == 1
    assert archive.archive_local(db, device)['hermes']['sources'] == 1


def test_profile_names_are_encoded_in_session_identity():
    assert native_key('worker:team', 's') == 'profile:worker%3Ateam:s'
    assert native_key('', 's') == 's'


def test_custom_hermes_home_and_windows_legacy_path_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv('HERMES_STATE_DB', raising=False)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'custom'))
    assert hermes_db_path() == tmp_path / 'custom/state.db'
    monkeypatch.delenv('HERMES_HOME')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr('awb.multi_usage.sys.platform', 'win32')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'legacy'))
    home = tmp_path / '.hermes/state.db'
    source(home)
    assert hermes_db_path() == home
    legacy = tmp_path / 'legacy/hermes/state.db'
    source(legacy)
    assert hermes_db_path() == legacy


def test_failed_profile_is_visible_in_background_scan_errors(setup, monkeypatch):
    home, db, device = setup
    source(home / 'state.db')
    bad = home / 'profiles/broken/state.db'
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b'invalid SQLite')
    monkeypatch.setenv('CODEX_HOME', str(home / 'no-codex'))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(home / 'no-claude'))
    result = archive.refresh(db, force=True)
    assert result['scan']['hermes']['read_errors'] == 1
    assert result['scan']['hermes']['errors'][0]['profile'] == 'broken'


def test_device_identity_rotation_rescans_default_and_profiles(setup):
    home, db, original_device = setup
    source(home / 'state.db')
    source(home / 'profiles/worker/state.db', 'worker')
    archive.archive_local(db, original_device)
    with db.tx() as conn:
        conn.execute("UPDATE archive_settings SET value='OtherOS:other-host' WHERE key='local_host'")
    new_device = archive.initialize(db)
    assert new_device != original_device
    result = archive.archive_local(db, new_device)
    assert result['hermes']['scanned'] == 2
    assert archive.export_packets(db, new_device) > 0
    with db.read() as conn:
        for device in [original_device, new_device]:
            rows = conn.execute("SELECT native_id FROM archive_facts WHERE kind='session' AND agent='hermes' AND device_id=?", (device,)).fetchall()
            assert {row[0] for row in rows} == {'same-id', 'profile:worker:same-id'}


def test_circular_profile_symlink_does_not_prevent_healthy_archive(setup):
    home, db, device = setup
    source(home / 'state.db')
    loop = home / 'profiles/broken/state.db'
    loop.parent.mkdir(parents=True)
    try:
        loop.symlink_to('state.db')
    except OSError:
        pytest.skip('OS does not allow unprivileged file symlinks')
    result = archive.archive_local(db, device)
    assert result['hermes']['read_errors'] == 1
    assert result['hermes']['errors'][0]['profile'] == 'broken'
    assert result['hermes']['scanned'] == 1
    assert archive_views.conversation(db, 'hermes', 'same-id', device_id=device)['count'] == 2
