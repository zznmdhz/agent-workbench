"""Durable, device-owned facts and immutable exchange packets.

The native Agent stores are discovery inputs.  The archive is the source of
truth for the Workbench UI, including after a native store disappears.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import platform
import sqlite3
import sys
import time
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

from .db import Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS archive_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS archive_devices(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, os TEXT NOT NULL,
 last_seen TEXT NOT NULL, last_imported TEXT
);
CREATE TABLE IF NOT EXISTS archive_facts(
 device_id TEXT NOT NULL, kind TEXT NOT NULL, agent TEXT NOT NULL,
 native_id TEXT NOT NULL, fact_id TEXT NOT NULL, occurred_at TEXT,
 payload_json TEXT NOT NULL, checksum TEXT NOT NULL, origin_seq INTEGER NOT NULL,
 PRIMARY KEY(device_id,kind,agent,native_id,fact_id)
);
CREATE INDEX IF NOT EXISTS ix_archive_kind_time ON archive_facts(kind,occurred_at,device_id,agent);
CREATE INDEX IF NOT EXISTS ix_archive_session ON archive_facts(device_id,agent,native_id,kind);
CREATE TABLE IF NOT EXISTS archive_log(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL,
 kind TEXT NOT NULL, agent TEXT NOT NULL, native_id TEXT NOT NULL,
 fact_id TEXT NOT NULL, occurred_at TEXT, payload_json TEXT NOT NULL,
 checksum TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS archive_imports(
 packet_name TEXT PRIMARY KEY, device_id TEXT NOT NULL,
 imported_at TEXT NOT NULL, fact_count INTEGER NOT NULL
);
"""

_LOCK = Lock()
_LAST_RUN: dict[str, float] = {}
_PACKET_LIMIT = 512 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def default_sync_root() -> Path | None:
    override = os.environ.get('AWB_SYNC_ROOT')
    if override:
        return Path(override).expanduser()
    candidates = ([Path.home() / 'Sync_AI'] if sys.platform == 'darwin'
                  else [Path('B:/Sync_AI'), Path.home() / 'Sync_AI'])
    return next((path for path in candidates if (path / '.stfolder').is_dir()), None)


def initialize(db: Database) -> str:
    with closing(db.connect()) as conn:
        conn.executescript(SCHEMA)
    with db.tx() as conn:
        row = conn.execute("SELECT value FROM archive_settings WHERE key='local_device_id'").fetchone()
        device_id = row[0] if row else str(uuid4())
        host = f'{platform.system()}:{platform.node()}'
        marker = conn.execute("SELECT value FROM archive_settings WHERE key='local_host'").fetchone()
        if marker and marker[0] != host:
            # A database copied to another computer must not make that computer
            # write into the source computer's packet stream.
            device_id = str(uuid4())
            conn.execute("DELETE FROM archive_settings WHERE key IN ('last_exported_seq','hermes_scan_signature')")
        if not row or (marker and marker[0] != host):
            conn.execute("INSERT INTO archive_settings(key,value) VALUES('local_device_id',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (device_id,))
        conn.execute("INSERT INTO archive_settings(key,value) VALUES('local_host',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (host,))
        conn.execute('''INSERT INTO archive_devices(id,name,os,last_seen) VALUES(?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name,os=excluded.os,last_seen=excluded.last_seen''',
            (device_id, platform.node() or 'This computer', platform.system(), utc_now()))
    return device_id


def local_device_id(db: Database) -> str:
    try:
        with db.read() as conn:
            row = conn.execute("SELECT value FROM archive_settings WHERE key='local_device_id'").fetchone()
    except sqlite3.OperationalError:
        row = None
    return row[0] if row else initialize(db)


def sync_root(db: Database) -> Path | None:
    with db.read() as conn:
        row = conn.execute("SELECT value FROM archive_settings WHERE key='sync_root'").fetchone()
    return Path(row[0]).expanduser() if row and row[0] else default_sync_root()


def set_sync_root(db: Database, raw_path: str) -> Path:
    path = Path(raw_path).expanduser().resolve()
    if not path.is_dir():
        raise ValueError('Select an existing folder synchronized by your NAS client')
    with db.tx() as conn:
        existing = conn.execute("SELECT value FROM archive_settings WHERE key='sync_root'").fetchone()
        previous = Path(existing[0]).expanduser().resolve() if existing else default_sync_root()
        conn.execute("INSERT INTO archive_settings(key,value) VALUES('sync_root',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(path),))
        if previous != path:
            conn.execute("DELETE FROM archive_settings WHERE key='last_exported_seq'")
    return path


def _encode(value: dict) -> tuple[str, str]:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return data, hashlib.sha256(data.encode('utf-8')).hexdigest()


def _upsert_local(conn: sqlite3.Connection, device_id: str, kind: str, agent: str,
                  native_id: str, fact_id: str, occurred_at: str | None, value: dict) -> bool:
    payload, checksum = _encode(value)
    old = conn.execute('''SELECT checksum FROM archive_facts WHERE device_id=? AND kind=?
        AND agent=? AND native_id=? AND fact_id=?''',
        (device_id, kind, agent, native_id, fact_id)).fetchone()
    if old and old[0] == checksum:
        return False
    cursor = conn.execute('''INSERT INTO archive_log(device_id,kind,agent,native_id,fact_id,
        occurred_at,payload_json,checksum) VALUES(?,?,?,?,?,?,?,?)''',
        (device_id, kind, agent, native_id, fact_id, occurred_at, payload, checksum))
    conn.execute('''INSERT INTO archive_facts(device_id,kind,agent,native_id,fact_id,
        occurred_at,payload_json,checksum,origin_seq) VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(device_id,kind,agent,native_id,fact_id) DO UPDATE SET
        occurred_at=excluded.occurred_at,payload_json=excluded.payload_json,
        checksum=excluded.checksum,origin_seq=excluded.origin_seq''',
        (device_id, kind, agent, native_id, fact_id, occurred_at, payload, checksum, cursor.lastrowid))
    return True


def archive_local(db: Database, device_id: str) -> dict:
    """Copy all discovered local facts; missing source files never purge facts."""
    from .activity import _hermes_file_events, _iso, _source_cwd
    from .multi_usage import _codex_titles, hermes_db_path
    from .mvp_usage import codex_home

    changed = 0
    with db.read() as conn:
        usage = [dict(r) for r in conn.execute('SELECT * FROM mvp_usage_requests')]
        claude = [dict(r) for r in conn.execute('SELECT * FROM mvp_claude_requests')]
        messages = [dict(r) for r in conn.execute('SELECT * FROM mvp_activity_messages')]
        runs = [dict(r) for r in conn.execute('SELECT * FROM mvp_activity_runs')]
        files = [dict(r) for r in conn.execute('SELECT * FROM mvp_activity_file_events')]
    titles = _codex_titles(codex_home())
    sessions: dict[tuple[str, str], dict] = defaultdict(lambda: {
        'title': '', 'cwd': None, 'sources': [], 'record_bytes': None,
        'payload_bytes': None, 'first_at': None, 'last_at': None})
    sources: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in messages:
        key = (row['agent'], row['native_id'])
        item = sessions[key]
        item['first_at'] = min(item['first_at'] or row['occurred_at'], row['occurred_at'])
        item['last_at'] = max(item['last_at'] or row['occurred_at'], row['occurred_at'])
        if row['role'] == 'user' and not item['title']:
            item['title'] = row['preview'][:80]
        sources[key].add(row['source_file'])
    for row in runs:
        sources[(row['agent'], row['native_id'])].add(row['source_file'])
    for row in files:
        sources[(row['agent'], row['native_id'])].add(row['source_file'])
    for key, paths in sources.items():
        item = sessions[key]
        item['sources'] = sorted(paths)
        sizes = []
        for raw in paths:
            try:
                sizes.append(Path(raw).stat().st_size)
            except OSError:
                pass
            item['cwd'] = item['cwd'] or _source_cwd(Path(raw), key[0], key[1])
        item['record_bytes'] = sum(sizes) if sizes else None
    for (agent, native_id), item in sessions.items():
        if agent == 'codex':
            item['title'] = titles.get(native_id) or item['title']
        elif agent == 'claude' and not item['title']:
            item['title'] = f'Claude · {native_id[:8]}'

    hermes_path = hermes_db_path()
    hermes_messages: list[dict] = []
    hermes_usage: list[dict] = []
    hermes_files: list[dict] = []
    if hermes_path.is_file():
        stat = hermes_path.stat()
        signature = f'{stat.st_size}:{stat.st_mtime_ns}'
        with db.read() as conn:
            old = conn.execute("SELECT value FROM archive_settings WHERE key='hermes_scan_signature'").fetchone()
            if not old or old[0] != signature:
                with closing(sqlite3.connect(hermes_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as conn:
                    conn.row_factory = sqlite3.Row
                    tables = {row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")}
                    session_columns = {column[1] for column in conn.execute('PRAGMA table_info(sessions)')}
                    display = 'display_name' if 'display_name' in session_columns else 'NULL'
                    cwd_column = 'cwd' if 'cwd' in session_columns else 'NULL AS cwd'
                    for row in conn.execute(f'SELECT id,title,{display} AS display_name,'
                                            f'{cwd_column} FROM sessions'):
                        native_id = str(row['id'])
                        item = sessions[('hermes', native_id)]
                        item['title'] = row['title'] or row['display_name'] or item['title']
                        item['cwd'] = row['cwd']
                        item['sources'] = [str(hermes_path)]
                    message_columns = ({column[1] for column in conn.execute('PRAGMA table_info(messages)')}
                                       if 'messages' in tables else set())
                    def message_field(name: str) -> str:
                        return name if name in message_columns else f'NULL AS {name}'
                    message_sql = (f"SELECT id,session_id,role,timestamp,{message_field('finish_reason')},"
                                   f"{message_field('content')},{message_field('tool_calls')},"
                                   f"{message_field('tool_name')} FROM messages ORDER BY timestamp,id")
                    for row in conn.execute(message_sql) if message_columns else []:
                        native_id = str(row['session_id'])
                        at = _iso(row['timestamp'])
                        if not at:
                            continue
                        item = sessions[('hermes', native_id)]
                        item['first_at'] = min(item['first_at'] or at, at)
                        item['last_at'] = max(item['last_at'] or at, at)
                        item['payload_bytes'] = (item['payload_bytes'] or 0) + len(
                            (row['content'] or '').encode('utf-8')) + len((row['tool_calls'] or '').encode('utf-8'))
                        if row['role'] in {'user', 'assistant'}:
                            hermes_messages.append({'agent': 'hermes', 'native_id': native_id,
                                'message_id': str(row['id']), 'occurred_at': at, 'role': row['role'],
                                'final': int(row['finish_reason'] == 'stop'), 'preview': (row['content'] or '')[:160],
                                'body': row['content'] or '', 'source_file': str(hermes_path), 'source_offset': 0})
                    for row in (conn.execute('''SELECT u.session_id,u.model,u.task,u.api_call_count,
                        u.input_tokens,u.output_tokens,u.cache_read_tokens,u.cache_write_tokens,
                        u.first_seen,u.last_seen FROM session_model_usage u''')
                                if 'session_model_usage' in tables else []):
                        hermes_usage.append(dict(row))
                    for (agent, native_id), item in sessions.items():
                        if agent == 'hermes' and {'tool_calls', 'tool_call_id', 'tool_name'} <= message_columns:
                            hermes_files.extend({'agent': agent, 'native_id': native_id,
                                'event_key': f"{event['occurred_at']}:{index}:{event['native_path']}", **event}
                                for index, event in enumerate(_hermes_file_events(
                                    conn, native_id, item['cwd'], str(hermes_path))))
    with db.tx() as conn:
        for row in usage:
            changed += _upsert_local(conn, device_id, 'usage', 'codex', row['native_session_id'],
                                     row['request_id'], row['occurred_at'], row)
        for row in claude:
            changed += _upsert_local(conn, device_id, 'usage', 'claude', row['native_session_id'],
                                     row['request_id'], row['occurred_at'], row)
        for row in hermes_usage:
            native_id = str(row['session_id'])
            fact_id = f"{row['model']}:{row['task']}"
            changed += _upsert_local(conn, device_id, 'usage', 'hermes', native_id,
                                     fact_id, _iso(row['last_seen']), row)
        for row in messages:
            value = {**row, 'body': row['search_body']}
            changed += _upsert_local(conn, device_id, 'message', row['agent'], row['native_id'],
                                     row['message_id'], row['occurred_at'], value)
        for row in hermes_messages:
            changed += _upsert_local(conn, device_id, 'message', 'hermes', row['native_id'],
                                     row['message_id'], row['occurred_at'], row)
        for row in runs:
            changed += _upsert_local(conn, device_id, 'run', row['agent'], row['native_id'],
                                     row['run_id'], row['start_at'], row)
        for row in [*files, *hermes_files]:
            changed += _upsert_local(conn, device_id, 'file', row['agent'], row['native_id'],
                                     row['event_key'], row['occurred_at'], row)
        for (agent, native_id), item in sessions.items():
            if not item['title']:
                item['title'] = f'{agent} · {native_id[:8]}'
            changed += _upsert_local(conn, device_id, 'session', agent, native_id,
                                     native_id, item['last_at'], item)
        if hermes_path.is_file() and hermes_messages:
            conn.execute("INSERT INTO archive_settings(key,value) VALUES('hermes_scan_signature',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (signature,))
    return {'changed_facts': changed, 'local_device_id': device_id}


def _exchange_dir(db: Database) -> Path | None:
    root = sync_root(db)
    if root and root.is_dir():
        return root / 'AgentWorkbench-data-exchange'
    return None


def export_packets(db: Database, device_id: str) -> int:
    directory = _exchange_dir(db)
    if directory is None:
        return 0
    output = directory / 'devices' / device_id / 'packets'
    output.mkdir(parents=True, exist_ok=True)
    with db.read() as conn:
        row = conn.execute("SELECT value FROM archive_settings WHERE key='last_exported_seq'").fetchone()
        after = int(row[0]) if row else 0
        device = dict(conn.execute('SELECT id,name,os FROM archive_devices WHERE id=?', (device_id,)).fetchone())
        rows = [dict(r) for r in conn.execute(
            'SELECT * FROM archive_log WHERE seq>? AND device_id=? ORDER BY seq', (after, device_id))]
    written = 0
    batch: list[dict] = []
    size = 0
    def flush() -> None:
        nonlocal batch, size, written
        if not batch:
            return
        packet = {'schema': 1, 'device': device, 'facts': batch}
        raw = json.dumps(packet, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        digest = hashlib.sha256(raw).hexdigest()
        name = f"{batch[0]['seq']:012d}-{batch[-1]['seq']:012d}-{digest[:16]}.json.gz"
        target = output / name
        if not target.exists():
            temp = output / (name + '.tmp')
            with temp.open('wb') as handle:
                handle.write(gzip.compress(raw, mtime=0))
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(target)
        with db.tx() as conn:
            conn.execute("INSERT INTO archive_settings(key,value) VALUES('last_exported_seq',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(batch[-1]['seq']),))
        written += 1
        batch, size = [], 0
    for row in rows:
        size += len(row['payload_json'].encode('utf-8'))
        batch.append(row)
        if size >= _PACKET_LIMIT:
            flush()
    flush()
    return written


def import_packets(db: Database, local_id: str) -> int:
    directory = _exchange_dir(db)
    if directory is None or not (directory / 'devices').is_dir():
        return 0
    imported = 0
    with db.read() as conn:
        known = {r[0] for r in conn.execute('SELECT packet_name FROM archive_imports')}
    for path in sorted((directory / 'devices').glob('*/packets/*.json.gz')):
        origin = path.parent.parent.name
        if origin == local_id or path.name in known:
            continue
        try:
            raw = gzip.decompress(path.read_bytes())
            if len(raw) > 64 * 1024 * 1024:
                continue
            packet = json.loads(raw)
            if (packet.get('schema') != 1 or packet.get('device', {}).get('id') != origin
                    or hashlib.sha256(raw).hexdigest()[:16] != path.stem.split('-')[-1].removesuffix('.json')):
                continue
            rows = packet['facts']
            if not isinstance(rows, list) or len(rows) > 2000:
                continue
            with db.tx() as conn:
                for row in rows:
                    if row['device_id'] != origin or row['kind'] not in {'usage', 'message', 'run', 'file', 'session'}:
                        raise ValueError('Invalid archive packet')
                    key = (origin, row['kind'], row['agent'], row['native_id'], row['fact_id'])
                    old = conn.execute('''SELECT origin_seq FROM archive_facts WHERE device_id=?
                        AND kind=? AND agent=? AND native_id=? AND fact_id=?''', key).fetchone()
                    if old and old[0] >= row['seq']:
                        continue
                    conn.execute('''INSERT INTO archive_facts(device_id,kind,agent,native_id,fact_id,
                        occurred_at,payload_json,checksum,origin_seq) VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(device_id,kind,agent,native_id,fact_id) DO UPDATE SET
                        occurred_at=excluded.occurred_at,payload_json=excluded.payload_json,
                        checksum=excluded.checksum,origin_seq=excluded.origin_seq''',
                        (*key, row['occurred_at'], row['payload_json'], row['checksum'], row['seq']))
                device = packet['device']
                conn.execute('''INSERT INTO archive_devices(id,name,os,last_seen,last_imported)
                    VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                    os=excluded.os,last_imported=excluded.last_imported''',
                    (origin, device['name'], device['os'], utc_now(), utc_now()))
                conn.execute('INSERT INTO archive_imports(packet_name,device_id,imported_at,fact_count) VALUES(?,?,?,?)',
                             (path.name, origin, utc_now(), len(rows)))
            imported += 1
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            continue
    return imported


def refresh(db: Database, *, force: bool = False) -> dict:
    key = str(db.path.resolve())
    with _LOCK:
        if not force and time.monotonic() - _LAST_RUN.get(key, 0) < 30:
            return status(db)
        device_id = initialize(db)
        from .activity import sync_activity
        from .multi_usage import sync_claude_usage
        from .mvp_usage import sync_codex_usage
        # Preserve rows from older Workbench versions before their source-mirror
        # scanners can remove an absent native file.
        before = archive_local(db, device_id)
        codex = sync_codex_usage(db)
        claude = sync_claude_usage(db)
        activity = sync_activity(db)
        local = archive_local(db, device_id)
        exported = export_packets(db, device_id)
        imported = import_packets(db, device_id)
        _LAST_RUN[key] = time.monotonic()
    return {**status(db), 'scan': {'codex': codex, 'claude': claude, 'activity': activity},
            'changed_facts': before['changed_facts'] + local['changed_facts'],
            'exported_packets': exported,
            'imported_packets': imported}


def status(db: Database) -> dict:
    device_id = local_device_id(db)
    with db.read() as conn:
        devices = [dict(r) for r in conn.execute('''SELECT id,name,os,last_seen,last_imported
            FROM archive_devices ORDER BY name,id''')]
        counts = {r['device_id']: r['count'] for r in conn.execute(
            'SELECT device_id,count(*) AS count FROM archive_facts GROUP BY device_id')}
    for row in devices:
        row['local'] = row['id'] == device_id
        row['facts'] = counts.get(row['id'], 0)
    root = sync_root(db)
    return {'local_device_id': device_id, 'sync_root': str(root) if root else None,
            'sync_ready': _exchange_dir(db) is not None, 'devices': devices}


def facts(db: Database, kind: str, device_id: str | None = None) -> list[dict]:
    condition = 'kind=?'
    params: list[str] = [kind]
    if device_id:
        condition += ' AND device_id=?'
        params.append(device_id)
    with db.read() as conn:
        rows = conn.execute('''SELECT device_id,agent,native_id,fact_id,occurred_at,payload_json
            FROM archive_facts WHERE ''' + condition, params).fetchall()
    result = [{**dict(row), **json.loads(row['payload_json'])} for row in rows]
    if device_id:
        return result
    # A copied native Agent history can be discovered on both computers. Its
    # native UUID and fact ID identify one observation; count it once in All.
    local = local_device_id(db)
    result.sort(key=lambda row: row['device_id'] != local)
    unique = {}
    for row in result:
        unique.setdefault((row['kind'] if 'kind' in row else kind,
                           row['agent'], row['native_id'], row['fact_id']), row)
    return list(unique.values())
