"""Local conversation timeline and elapsed-time ledger for Codex, Claude and Hermes.

Bodies remain in their original files/databases; this index stores only metadata and
short previews. Claude/Hermes message-to-final spans are explicitly estimates.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

from .claude_usage import claude_usage_files
from .codex_usage import codex_usage_files
from .db import Database
from .multi_usage import _codex_titles, _heatmap_keys, _range, claude_home, hermes_db_path
from .mvp_usage import codex_home

_LOCK = Lock()
MAX_ESTIMATE_SECONDS = 6 * 3600


def _iso(value: object) -> str | None:
    try:
        if isinstance(value, (int, float)):
            stamp = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, str):
            stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        else:
            return None
        return stamp.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z') if stamp.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _text(parts: object) -> str:
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        return ''
    return '\n'.join(str(x.get('text') or '') for x in parts if isinstance(x, dict)
                     and x.get('type') in {'input_text', 'output_text', 'text'})


def _preview(value: str) -> str:
    return ' '.join(value.split())[:160]


def _boilerplate(value: str) -> bool:
    return value.startswith(('# AGENTS.md instructions', '<environment_context>',
                             '[IMPORTANT: You are running as a scheduled cron job',
                             '[IMPORTANT: The user has invoked'))


def _scan_codex(path: Path) -> tuple[list[tuple], list[tuple]]:
    messages, runs = [], []
    native_id = ''
    starts: dict[str, str] = {}
    with path.open('rb') as stream:
        while line := stream.readline():
            offset = stream.tell() - len(line)
            if not any(marker in line for marker in (b'"session_meta"', b'"task_started"',
                                                     b'"task_complete"', b'"response_item"')):
                continue
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            payload = row.get('payload') or {}
            kind = row.get('type')
            if kind == 'session_meta' and not native_id:
                native_id = str(payload.get('id') or '')
            if not native_id:
                continue
            at = _iso(row.get('timestamp'))
            if kind == 'event_msg':
                event = payload.get('type')
                run_id = payload.get('turn_id')
                if not isinstance(run_id, str):
                    continue
                if event == 'task_started' and payload.get('root_turn_id', run_id) == run_id:
                    start = _iso(payload.get('started_at')) or at
                    if start:
                        starts[run_id] = start
                elif event == 'task_complete' and run_id in starts:
                    start = _iso(payload.get('started_at')) or starts.get(run_id)
                    end = _iso(payload.get('completed_at')) or at
                    duration_ms = payload.get('duration_ms')
                    if isinstance(duration_ms, (int, float)) and duration_ms > 0 and at:
                        end = at
                        start = _iso(_dt(end).timestamp() - duration_ms / 1000)
                    if start and end and end > start:
                        runs.append(('codex', native_id, run_id, start, end, str(path)))
            elif kind == 'response_item' and payload.get('type') == 'message' and at:
                role = payload.get('role')
                if role not in {'user', 'assistant'}:
                    continue
                body = _text(payload.get('content'))
                if body.strip():
                    messages.append(('codex', native_id, str(payload.get('id') or offset), at,
                                     role, int(role == 'assistant'), _preview(body), str(path), offset))
    return messages, runs


def _scan_claude(path: Path) -> tuple[list[tuple], list[tuple]]:
    messages = []
    with path.open('rb') as stream:
        while line := stream.readline():
            offset = stream.tell() - len(line)
            if b'"type":"user"' not in line and b'"type":"assistant"' not in line:
                # Some Claude writers include spaces; the normal JSONL serializer does not.
                if b'"type": "user"' not in line and b'"type": "assistant"' not in line:
                    continue
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            role = row.get('type')
            if role not in {'user', 'assistant'} or row.get('isSidechain'):
                continue
            at = _iso(row.get('timestamp'))
            message = row.get('message') or {}
            if not at or not isinstance(message, dict):
                continue
            parts = message.get('content')
            if role == 'user' and isinstance(parts, list) and not any(
                    isinstance(p, dict) and p.get('type') == 'text' for p in parts):
                continue  # tool_result is a continuation, not a human request
            body = _text(parts)
            if not body.strip():
                continue
            native_id = str(row.get('sessionId') or path.stem)
            message_id = str(message.get('id') or row.get('uuid') or offset)
            final = int(role == 'assistant' and message.get('stop_reason') in {'end_turn', 'stop_sequence'})
            messages.append(('claude', native_id, message_id, at, role, final,
                             _preview(body), str(path), offset))
    return messages, []


def sync_activity(db: Database, *, codex_root: Path | None = None,
                  claude_root: Path | None = None) -> dict:
    roots = {'codex': codex_root or codex_home(), 'claude': claude_root or claude_home()}
    paths = {'codex': codex_usage_files(roots['codex']),
             'claude': claude_usage_files(roots['claude'])}
    with _LOCK:
        with db.read() as conn:
            previous = {(r['agent'], r['source_file']): r for r in conn.execute(
                'SELECT * FROM mvp_activity_files')}
        changed = 0
        errors = 0
        removed = 0
        for agent, files in paths.items():
            source_root = roots[agent] / ('sessions' if agent == 'codex' else 'projects')
            if not source_root.is_dir():
                continue
            present = {str(path) for path in files}
            missing = [source_file for name, source_file in previous
                       if name == agent and source_file not in present]
            if missing:
                with db.tx() as conn:
                    for source_file in missing:
                        conn.execute('DELETE FROM mvp_activity_messages WHERE agent=? AND source_file=?',
                                     (agent, source_file))
                        conn.execute('DELETE FROM mvp_activity_runs WHERE agent=? AND source_file=?',
                                     (agent, source_file))
                        conn.execute('DELETE FROM mvp_activity_files WHERE agent=? AND source_file=?',
                                     (agent, source_file))
                removed += len(missing)
        for agent, files in paths.items():
            for path in files:
                try:
                    stat = path.stat()
                    old = previous.get((agent, str(path)))
                    if (old and old['status'] == 'ok' and old['size_bytes'] == stat.st_size
                            and old['modified_ns'] == stat.st_mtime_ns):
                        continue
                    messages, runs = (_scan_codex(path) if agent == 'codex' else _scan_claude(path))
                    with db.tx() as conn:
                        conn.execute('DELETE FROM mvp_activity_messages WHERE agent=? AND source_file=?',
                                     (agent, str(path)))
                        conn.execute('DELETE FROM mvp_activity_runs WHERE agent=? AND source_file=?',
                                     (agent, str(path)))
                        conn.executemany('''INSERT INTO mvp_activity_messages
                            (agent,native_id,message_id,occurred_at,role,final,preview,source_file,source_offset)
                            VALUES(?,?,?,?,?,?,?,?,?)''', messages)
                        conn.executemany('''INSERT INTO mvp_activity_runs
                            (agent,native_id,run_id,start_at,end_at,source_file)
                            VALUES(?,?,?,?,?,?)''', runs)
                        conn.execute('''INSERT INTO mvp_activity_files VALUES(?,?,?,?,?)
                            ON CONFLICT(agent,source_file) DO UPDATE SET
                            size_bytes=excluded.size_bytes,modified_ns=excluded.modified_ns,
                            status=excluded.status''',
                                     (agent, str(path), stat.st_size, stat.st_mtime_ns, 'ok'))
                    changed += 1
                except (OSError, sqlite3.Error):
                    errors += 1
        return {'updated_files': changed, 'removed_files': removed, 'read_errors': errors,
                'files': {name: len(items) for name, items in paths.items()}}


def _hermes(path: Path) -> tuple[list[dict], dict[str, str], bool]:
    if not path.is_file():
        return [], {}, False
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            titles = {str(r['id']): r['title'] or r['display_name'] or '' for r in conn.execute(
                'SELECT id,title,display_name FROM sessions')}
            rows = [dict(r) for r in conn.execute('''SELECT id,session_id,role,timestamp,finish_reason,
                substr(content,1,160) AS preview FROM messages
                WHERE role IN ('user','assistant') AND timestamp IS NOT NULL ORDER BY timestamp,id''')]
        return rows, titles, True
    except (OSError, sqlite3.Error):
        return [], {}, False


def _union_ms(intervals: list[tuple[datetime, datetime]]) -> int:
    merged: list[list[datetime]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return sum(int((end - start).total_seconds() * 1000) for start, end in merged)


def _estimated(messages: list[dict], agent: str) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in messages:
        grouped[row['native_id']].append(row)
    result = []
    for native_id, group in grouped.items():
        pending: dict | None = None
        last: dict | None = None
        for row in sorted(group, key=lambda x: x['occurred_at']):
            if row['role'] == 'user':
                if agent == 'codex' and _boilerplate(row['preview']):
                    continue
                pending, last = row, None
            elif pending:
                last = row
                if row['final']:
                    start, end = _dt(pending['occurred_at']), _dt(row['occurred_at'])
                    seconds = (end - start).total_seconds()
                    if 0 < seconds <= MAX_ESTIMATE_SECONDS:
                        result.append({'agent': agent, 'native_id': native_id,
                                       'start': start, 'end': end, 'precision': 'estimated'})
                    pending, last = None, None
        if pending and last:
            start, end = _dt(pending['occurred_at']), _dt(last['occurred_at'])
            seconds = (end - start).total_seconds()
            if 0 < seconds <= MAX_ESTIMATE_SECONDS:
                result.append({'agent': agent, 'native_id': native_id,
                               'start': start, 'end': end, 'precision': 'estimated'})
    return result


def _bucket_bounds(key: str, grain: str, zone: ZoneInfo) -> tuple[datetime, datetime]:
    if grain == 'hour':
        start = datetime.fromisoformat(key + ':00:00').replace(tzinfo=zone)
        return start.astimezone(timezone.utc), (start + timedelta(hours=1)).astimezone(timezone.utc)
    if grain == 'month':
        year, month = map(int, key.split('-'))
        start = datetime(year, month, 1, tzinfo=zone)
        end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=zone)
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    start = datetime.fromisoformat(key).replace(tzinfo=zone)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def dashboard(db: Database, day: str, through: str, tz: str, *,
              heatmap_view: str = 'year', focus_day: str | None = None,
              agent: str | None = None, sync: bool = True, session_range: bool = False,
              codex_root: Path | None = None, claude_root: Path | None = None,
              hermes_path: Path | None = None) -> dict:
    if agent not in {None, 'codex', 'claude', 'hermes'}:
        raise ValueError('Invalid agent')
    first, last, range_start, range_end, zone = _range(day, through, tz)
    focus = date.fromisoformat(focus_day or day)
    if focus < first or focus > last:
        focus = first
    keys, grain = _heatmap_keys(first, last, heatmap_view)
    state = sync_activity(db, codex_root=codex_root, claude_root=claude_root) if sync else {}
    with db.read() as conn:
        messages = [dict(row) for row in conn.execute('SELECT * FROM mvp_activity_messages')]
        runs = [dict(row) for row in conn.execute('SELECT * FROM mvp_activity_runs')]
    hermes_rows, hermes_titles, hermes_ok = _hermes(hermes_path or hermes_db_path())
    for row in hermes_rows:
        at = _iso(row['timestamp'])
        if at:
            messages.append({'agent': 'hermes', 'native_id': str(row['session_id']),
                             'message_id': str(row['id']), 'occurred_at': at, 'role': row['role'],
                             'final': int(row['finish_reason'] == 'stop'),
                             'preview': _preview(row['preview'] or '')})
    # Streaming Claude transcripts can contain multiple snapshots of one assistant message.
    unique = {}
    for row in messages:
        key = (row['agent'], row['native_id'], row['message_id'])
        previous = unique.get(key)
        if previous is None or (row['final'], row['occurred_at']) >= (previous['final'], previous['occurred_at']):
            unique[key] = row
    messages = list(unique.values())
    intervals = [{'agent': 'codex', 'native_id': row['native_id'],
                  'start': _dt(row['start_at']), 'end': _dt(row['end_at']), 'precision': 'verified'}
                 for row in runs if row['end_at'] > row['start_at']]
    verified_sessions = {r['native_id'] for r in intervals}
    intervals.extend(_estimated([r for r in messages if r['agent'] == 'codex'
                                 and r['native_id'] not in verified_sessions], 'codex'))
    for name in ('claude', 'hermes'):
        intervals.extend(_estimated([r for r in messages if r['agent'] == name], name))
    intervals = [r for r in intervals if agent in (None, r['agent'])]
    selected = [r for r in intervals if r['start'] < range_end and r['end'] > range_start]
    def measure(rows: list[dict], start: datetime, end: datetime) -> dict:
        slices = [(max(r['start'], start), min(r['end'], end)) for r in rows
                  if r['start'] < end and r['end'] > start]
        return {'agent_ms': sum(int((b-a).total_seconds() * 1000) for a, b in slices),
                'wall_ms': _union_ms(slices), 'runs': len(slices),
                'verified_ms': sum(int((min(r['end'], end)-max(r['start'], start)).total_seconds() * 1000)
                                   for r in rows if r['precision'] == 'verified'
                                   and r['start'] < end and r['end'] > start)}
    summary = measure(selected, range_start, range_end)
    by_agent = {name: measure([r for r in selected if r['agent'] == name], range_start, range_end)
                for name in ('codex', 'claude', 'hermes')}
    heatmap = []
    for key in keys:
        bucket_start, bucket_end = _bucket_bounds(key, grain, zone)
        heatmap.append({'period': key, **measure(selected, max(bucket_start, range_start),
                                                 min(bucket_end, range_end))})
    focus_start, focus_end = ((range_start, range_end) if session_range else
                              _bucket_bounds(focus.isoformat(), 'day', zone))
    focus_runs = [r for r in intervals if r['start'] < focus_end and r['end'] > focus_start]
    focus_messages = [r for r in messages if focus_start <= _dt(r['occurred_at']) < focus_end
                      and agent in (None, r['agent'])]
    titles = _codex_titles(codex_root or codex_home())
    titles.update(hermes_titles)
    grouped: dict[tuple[str, str], dict] = {}
    for row in focus_messages:
        key = (row['agent'], row['native_id'])
        item = grouped.setdefault(key, {'agent': key[0], 'native_id': key[1], 'title': '',
                                        'first_at': row['occurred_at'], 'last_at': row['occurred_at'],
                                        'messages': 0, 'preview': ''})
        item['first_at'] = min(item['first_at'], row['occurred_at'])
        item['last_at'] = max(item['last_at'], row['occurred_at'])
        item['messages'] += 1
        if row['role'] == 'user' and not item['preview'] and not _boilerplate(row['preview']):
            item['preview'] = row['preview']
    for run in focus_runs:
        key = (run['agent'], run['native_id'])
        at = _iso(max(run['start'], focus_start).timestamp())
        end_at = _iso(min(run['end'], focus_end).timestamp())
        item = grouped.setdefault(key, {'agent': key[0], 'native_id': key[1], 'title': '',
                                        'first_at': at, 'last_at': end_at,
                                        'messages': 0, 'preview': ''})
        item['first_at'] = min(item['first_at'], at)
        item['last_at'] = max(item['last_at'], end_at)
    sessions = []
    for key, item in grouped.items():
        item['title'] = titles.get(key[1]) or item['preview'][:80] or f'{key[0]} · {key[1][:8]}'
        item.update(measure([r for r in focus_runs if (r['agent'], r['native_id']) == key],
                            focus_start, focus_end))
        sessions.append(item)
    sessions.sort(key=lambda x: x['last_at'], reverse=session_range)
    timeline = [] if session_range else sorted([{'agent': r['agent'], 'native_id': r['native_id'],
                        'start_at': max(r['start'], focus_start).isoformat(),
                        'end_at': min(r['end'], focus_end).isoformat(),
                        'precision': r['precision']} for r in focus_runs], key=lambda x: x['start_at'])
    return {'summary': summary, 'by_agent': by_agent, 'heatmap': heatmap,
            'heatmap_granularity': grain, 'focus_day': focus.isoformat(),
            'sessions': sessions, 'timeline': timeline, 'source': {**state, 'hermes_ready': hermes_ok},
            'note': 'Codex 有完整任务事件时用源记录时间，缺事件的会话仅按消息估算；Claude/Hermes 按用户输入至最终回复估算。超过六小时或未结束的估算片段不计入。'}


def session_browser(db: Database, day: str, through: str, tz: str, *,
                    agent: str | None = None, sync: bool = True,
                    codex_root: Path | None = None, claude_root: Path | None = None,
                    hermes_path: Path | None = None) -> dict:
    if agent not in {None, 'codex', 'claude', 'hermes'}:
        raise ValueError('Invalid agent')
    result = dashboard(db, day, through, tz, heatmap_view='custom',
                       session_range=True, sync=sync, codex_root=codex_root,
                       claude_root=claude_root, hermes_path=hermes_path)
    sessions = result['sessions']
    counts = {name: sum(row['agent'] == name for row in sessions)
              for name in ('codex', 'claude', 'hermes')}
    return {'day': day, 'through': through, 'counts': counts,
            'session_count': len(sessions),
            'sessions': [row for row in sessions if agent in (None, row['agent'])]}


def conversation(db: Database, agent: str, native_id: str, *, offset: int = 0,
                 limit: int = 100, day: str | None = None, through: str | None = None,
                 tz: str = 'Asia/Hong_Kong',
                 codex_root: Path | None = None,
                 claude_root: Path | None = None, hermes_path: Path | None = None) -> dict:
    if agent not in {'codex', 'claude', 'hermes'}:
        raise ValueError('Invalid agent')
    start = end = None
    if day:
        _, _, start, end, _ = _range(day, through or day, tz)
    if agent == 'hermes':
        path = hermes_path or hermes_db_path()
        if not path.is_file():
            return {'items': [], 'count': 0}
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            condition = "session_id=? AND role IN ('user','assistant')"
            params: list = [native_id]
            if start and end:
                condition += ' AND timestamp>=? AND timestamp<?'
                params.extend((start.timestamp(), end.timestamp()))
            count = conn.execute(f'SELECT count(*) FROM messages WHERE {condition}', params).fetchone()[0]
            rows = [dict(r) for r in conn.execute('''SELECT id,role,content,timestamp FROM messages
                WHERE ''' + condition + ' ORDER BY timestamp,id LIMIT ? OFFSET ?', (*params, limit, offset))]
        return {'count': count, 'items': [{'id': str(r['id']), 'role': r['role'],
                'occurred_at': _iso(r['timestamp']), 'body': r['content'] or ''} for r in rows]}
    with db.read() as conn:
        condition = 'agent=? AND native_id=?'
        params = [agent, native_id]
        if start and end:
            condition += ' AND occurred_at>=? AND occurred_at<?'
            params.extend((_iso(start.timestamp()), _iso(end.timestamp())))
        rows = [dict(r) for r in conn.execute('SELECT * FROM mvp_activity_messages WHERE '
                + condition + ' ORDER BY occurred_at,source_offset', params)]
    unique = {}
    for row in rows:
        key = row['message_id']
        previous = unique.get(key)
        if previous is None or (row['final'], row['occurred_at']) >= (previous['final'], previous['occurred_at']):
            unique[key] = row
    rows = sorted(unique.values(), key=lambda x: (x['occurred_at'], x['source_offset']))
    items = []
    for row in rows[offset:offset + limit]:
        try:
            with Path(row['source_file']).open('rb') as stream:
                stream.seek(row['source_offset'])
                record = json.loads(stream.readline())
            payload = record.get('payload') or {} if agent == 'codex' else record.get('message') or {}
            body = _text(payload.get('content'))
        except (OSError, ValueError, UnicodeError):
            body = '[原始记录当前无法读取]'
        items.append({'id': row['message_id'], 'role': row['role'],
                      'occurred_at': row['occurred_at'], 'body': body})
    return {'count': len(rows), 'items': items}
