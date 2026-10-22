"""Read-only, independently checkpointed Hermes default/profile archives."""
from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

CHECKPOINT = 'hermes_scan_signatures_v2'


def native_key(profile: str, session_id: str) -> str:
    # Default IDs remain unchanged for existing archives and cross-device dedup.
    return f'profile:{quote(profile, safe="")}:{session_id}' if profile else session_id


def sources(primary: Path, *, errors: list[dict] | None = None) -> list[tuple[str, Path]]:
    candidates = [('', primary)]
    # An explicit database override is an intentional single-source boundary.
    if not os.environ.get('HERMES_STATE_DB'):
        try:
            candidates.extend((path.parent.name, path)
                              for path in sorted((primary.parent / 'profiles').glob('*/state.db')))
        except (OSError, RuntimeError) as exc:
            if errors is not None:
                errors.append({'profile': 'profiles', 'error': type(exc).__name__})
    result, seen = [], set()
    for profile, path in candidates:
        try:
            resolved = path.resolve()
            if path.is_file() and resolved not in seen:
                seen.add(resolved)
                result.append((profile, path))
        except (OSError, RuntimeError) as exc:
            if errors is not None:
                errors.append({'profile': profile or 'default', 'error': type(exc).__name__})
    return result


def signature(path: Path) -> str:
    def stamp(target):
        try:
            stat = target.stat()
            return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
        except FileNotFoundError:
            return None
    resolved = path.resolve()
    return json.dumps([str(resolved), stamp(resolved), stamp(Path(str(resolved) + '-wal'))])


def read_source(path: Path, profile: str) -> dict:
    from .activity import _hermes_file_events, _iso

    sessions = defaultdict(lambda: {'title': '', 'cwd': None, 'sources': [str(path)],
                                   'record_bytes': None, 'payload_bytes': None,
                                   'first_at': None, 'last_at': None})
    messages, usage, files = [], [], []
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')  # One coherent WAL snapshot for session/message/usage reads.
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'sessions' not in tables:
            raise ValueError('missing_sessions_table')
        columns = {r[1] for r in conn.execute('PRAGMA table_info(sessions)')}

        def field(name, available):
            return name if name in available else f'NULL AS {name}'

        query = ','.join(['id', *(field(name, columns) for name in ['title', 'display_name', 'cwd'])])
        for row in conn.execute('SELECT ' + query + ' FROM sessions'):
            raw_id = str(row['id'])
            item = sessions[raw_id]
            title = row['title'] or row['display_name'] or f'hermes · {raw_id[:8]}'
            item['title'] = f'[{profile}] {title}' if profile else title
            item['cwd'] = row['cwd']
        message_columns = ({r[1] for r in conn.execute('PRAGMA table_info(messages)')}
                           if 'messages' in tables else set())
        if message_columns:
            query = ','.join(['id', 'session_id', 'role', 'timestamp', *(
                field(name, message_columns) for name in ['finish_reason', 'content', 'tool_calls'])])
            for row in conn.execute('SELECT ' + query + ' FROM messages ORDER BY timestamp,id'):
                raw_id = str(row['session_id'])
                at = _iso(row['timestamp'])
                if not at:
                    continue
                item = sessions[raw_id]
                item['first_at'] = min(item['first_at'] or at, at)
                item['last_at'] = max(item['last_at'] or at, at)
                item['payload_bytes'] = (item['payload_bytes'] or 0) + len(
                    (row['content'] or '').encode('utf-8')) + len((row['tool_calls'] or '').encode('utf-8'))
                if row['role'] in {'user', 'assistant'}:
                    messages.append({'agent': 'hermes', 'native_id': native_key(profile, raw_id),
                                     'message_id': str(row['id']), 'occurred_at': at, 'role': row['role'],
                                     'final': int(row['finish_reason'] == 'stop'),
                                     'preview': (row['content'] or '')[:160], 'body': row['content'] or '',
                                     'source_file': str(path), 'source_offset': 0})
        if 'session_model_usage' in tables:
            for row in conn.execute('''SELECT session_id,model,task,api_call_count,input_tokens,
                output_tokens,cache_read_tokens,cache_write_tokens,first_seen,last_seen
                FROM session_model_usage'''):
                usage.append(dict(row) | {'session_id': native_key(profile, str(row['session_id']))})
        if {'tool_calls', 'tool_call_id', 'tool_name', 'content'} <= message_columns:
            for raw_id, item in sessions.items():
                for index, event in enumerate(_hermes_file_events(conn, raw_id, item['cwd'], str(path))):
                    files.append({'agent': 'hermes', 'native_id': native_key(profile, raw_id),
                                  'event_key': f"{event['occurred_at']}:{index}:{event['native_path']}", **event})
    result = {}
    for raw_id, item in sessions.items():
        if profile:
            item.update(hermes_profile=profile, hermes_session_id=raw_id)
        result[('hermes', native_key(profile, raw_id))] = item
    return {'sessions': result, 'messages': messages, 'usage': usage, 'files': files}


def collect(db, primary: Path) -> dict:
    with db.read() as conn:
        row = conn.execute('SELECT value FROM archive_settings WHERE key=?', (CHECKPOINT,)).fetchone()
    saved = json.loads(row[0]) if row else {}
    result = {'sessions': {}, 'messages': [], 'usage': [], 'files': [], 'signatures': dict(saved),
              'sources': 0, 'scanned': 0, 'unchanged': 0, 'read_errors': 0, 'errors': []}
    discovered = sources(primary, errors=result['errors'])
    result['read_errors'] = len(result['errors'])
    for profile, path in discovered:
        result['sources'] += 1
        try:
            key = json.dumps([profile, str(path.resolve())])
            current = signature(path)
            if saved.get(key) == current:
                result['unchanged'] += 1
                continue
            snapshot = read_source(path, profile)
            result['sessions'].update(snapshot['sessions'])
            for name in ['messages', 'usage', 'files']:
                result[name].extend(snapshot[name])
            result['signatures'][key] = current
            result['scanned'] += 1
        except (OSError, sqlite3.Error, ValueError, TypeError, RuntimeError) as exc:
            # Other profiles remain available. Failed sources are never checkpointed.
            result['read_errors'] += 1
            result['errors'].append({'profile': profile or 'default', 'error': type(exc).__name__})
    return result
