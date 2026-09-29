"""Read projections over durable Workbench facts, independent of Agent stores."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from . import archive
from .activity import _boilerplate, _bucket_bounds, _dt, _estimated, _key_messages, _union_ms
from .db import Database
from .multi_usage import _add, _empty, _heatmap_keys, _range, _trend_dates, _values

AGENTS = ('codex', 'claude', 'hermes')


def _rows(db: Database, kind: str, device_id: str | None) -> list[dict]:
    return archive.facts(db, kind, device_id)


def _session_key(row: dict) -> tuple[str, str, str]:
    return row['device_id'], row['agent'], row['native_id']


def _date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def usage(db: Database, day: str, through: str, tz: str, *, agent: str | None = None,
          model: str | None = None, heatmap_view: str = 'year', device_id: str | None = None) -> dict:
    if agent not in (*AGENTS, None):
        raise ValueError('Invalid agent')
    first, last, start, end, zone = _range(day, through, tz)
    heat_keys, heat_grain = _heatmap_keys(first, last, heatmap_view)
    grain = 'day' if (last-first).days < 31 else 'week' if (last-first).days < 180 else 'month'
    sessions_meta = {_session_key(r): r for r in _rows(db, 'session', device_id)}
    all_usage = _rows(db, 'usage', device_id)
    sources = {name: _empty() for name in AGENTS}
    source_times: dict[str, list[str]] = defaultdict(list)
    trend = defaultdict(_empty)
    heatmap = defaultdict(_empty)
    models = defaultdict(_empty)
    sessions = defaultdict(lambda: {**_empty(), 'title': '', 'last_request': '', 'agent': '', 'device_id': ''})
    summary = _empty()
    hermes_partial = 0
    for row in all_usage:
        name = row['agent']
        if name not in AGENTS:
            continue
        try:
            if name == 'hermes':
                seen = datetime.fromtimestamp(float(row['first_seen']), timezone.utc)
                at = datetime.fromtimestamp(float(row['last_seen']), timezone.utc)
                source_times[name].extend((seen.isoformat(), at.isoformat()))
                if seen < start or at >= end:
                    if seen < end and at >= start:
                        hermes_partial += 1
                    continue
                values = _values(row['input_tokens'], row['cache_read_tokens'],
                                 row['cache_write_tokens'], row['output_tokens'], row['api_call_count'])
            else:
                at = _date(row['occurred_at'])
                source_times[name].append(row['occurred_at'])
                if not start <= at < end:
                    continue
                if name == 'codex':
                    original = max(0, row['input_tokens'])
                    read = min(original, max(0, row['cached_input_tokens']))
                    values = _values(original-read, read, 0, row['output_tokens'])
                else:
                    values = _values(row['input_tokens'], row['cached_input_tokens'],
                                     row['cache_creation_tokens'], row['output_tokens'])
            model_name = row.get('model') or 'unknown'
            if model and model_name != model:
                continue
            _add(sources[name], values)
            if agent not in (None, name):
                continue
            _add(summary, values)
            _add(models[(name, model_name)], values)
            key = _session_key(row)
            session = sessions[key]
            _add(session, values)
            session.update(agent=name, device_id=row['device_id'])
            meta = sessions_meta.get(key, {})
            session['title'] = meta.get('title') or f'{name} · {row["native_id"][:8]}'
            session['last_request'] = max(session['last_request'], at.isoformat())
            if name != 'hermes':
                local = at.astimezone(zone).date()
                period = (local.isoformat() if grain == 'day' else
                          local.strftime('%Y-%m') if grain == 'month' else
                          (local.toordinal() - local.weekday()))
                if grain == 'week':
                    period = date.fromordinal(period).isoformat()
                _add(trend[period], values)
                heat_key = (f'{local.isoformat()}T{at.astimezone(zone).hour:02d}'
                            if heat_grain == 'hour' else local.strftime('%Y-%m')
                            if heat_grain == 'month' else local.isoformat())
                _add(heatmap[heat_key], values)
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    source_meta = {}
    for name in AGENTS:
        times = source_times[name]
        source_meta[name] = {**sources[name], 'status': 'archived' if times else 'source_missing',
                             'precision': 'session_model_aggregate' if name == 'hermes' else 'request',
                             'files': 0, 'deferred': hermes_partial if name == 'hermes' else 0,
                             'earliest': min(times) if times else None,
                             'latest': max(times) if times else None,
                             'partial_rows': hermes_partial if name == 'hermes' else 0,
                             'missing_cache_read': 0, 'missing_cache_write': 0}
    result_sessions = [{'native_id': key[2], **value} for key, value in
                       sorted(sessions.items(), key=lambda item: item[1]['last_request'], reverse=True)]
    summary['cache_hit_rate'] = (summary['cached_input_tokens'] / summary['input_tokens']
                                 if summary['input_tokens'] else None)
    return {'status': 'ready', 'summary': summary, 'sources': source_meta,
            'models': [{'agent': name, 'model': model_name, **value} for (name, model_name), value in
                       sorted(models.items(), key=lambda item: -item[1]['total_tokens'])],
            'sessions': result_sessions[:100], 'session_count': len(result_sessions),
            'trend': [{'period': key, **trend[key]} for key in _trend_dates(first, last, grain)],
            'trend_granularity': grain, 'heatmap': [{'period': key, **heatmap[key]} for key in heat_keys],
            'heatmap_view': heatmap_view, 'heatmap_granularity': heat_grain,
            'unattributed_tokens': sources['hermes']['total_tokens'] if agent in (None, 'hermes') else 0,
            'note': '统计来自 Workbench 底库；Codex/Claude 按请求时间归属，Hermes 按完整会话汇总。'}


def requests(db: Database, agent: str, native_id: str, day: str, through: str, tz: str,
             *, model: str | None = None, limit: int = 100, device_id: str | None = None) -> dict:
    _, _, start, end, _ = _range(day, through, tz)
    rows = []
    for row in _rows(db, 'usage', device_id):
        if row['agent'] != agent or row['native_id'] != native_id:
            continue
        if model and row.get('model') != model:
            continue
        if agent == 'hermes':
            first = datetime.fromtimestamp(float(row['first_seen']), timezone.utc)
            at = datetime.fromtimestamp(float(row['last_seen']), timezone.utc)
            if first < start or at >= end:
                continue
            value = _values(row['input_tokens'], row['cache_read_tokens'],
                            row['cache_write_tokens'], row['output_tokens'], row['api_call_count'])
            rows.append({'request_id': row['fact_id'], 'occurred_at': at.isoformat(),
                         'first_seen': first.isoformat(), 'model': row.get('model') or 'unknown',
                         'precision': 'session_model_aggregate', **value})
        else:
            at = _date(row['occurred_at'])
            if not start <= at < end:
                continue
            original = row['input_tokens']
            read = row['cached_input_tokens']
            value = (_values(max(0, original-read), min(original, read), 0, row['output_tokens'])
                     if agent == 'codex' else _values(original, read,
                     row['cache_creation_tokens'], row['output_tokens']))
            rows.append({'request_id': row['request_id'], 'occurred_at': row['occurred_at'],
                         'model': row['model'], 'precision': 'request', **value})
    rows.sort(key=lambda row: row['occurred_at'], reverse=True)
    return {'items': rows[:limit], 'count': len(rows),
            'precision': 'session_model_aggregate' if agent == 'hermes' else 'request'}


def _activity_data(db: Database, device_id: str | None):
    messages = _rows(db, 'message', device_id)
    runs = _rows(db, 'run', device_id)
    meta = {_session_key(row): row for row in _rows(db, 'session', device_id)}
    # Make the existing interval estimator distinguish two devices with the
    # same native ID. The UI still receives the original native ID.
    for row in messages:
        row['_native_id'] = row['native_id']
        row['native_id'] = f'{row["device_id"]}/{row["native_id"]}'
    for row in runs:
        row['_native_id'] = row['native_id']
        row['native_id'] = f'{row["device_id"]}/{row["native_id"]}'
    intervals = []
    for row in runs:
        if row['end_at'] > row['start_at']:
            intervals.append({'agent': row['agent'], 'native_id': row['native_id'],
                              'start': _dt(row['start_at']), 'end': _dt(row['end_at']),
                              'precision': 'verified'})
    verified = {row['native_id'] for row in intervals}
    intervals += _estimated([row for row in messages if row['agent'] == 'codex'
                             and row['native_id'] not in verified], 'codex')
    for name in ('claude', 'hermes'):
        intervals += _estimated([row for row in messages if row['agent'] == name], name)
    return messages, intervals, meta


def activity(db: Database, day: str, through: str, tz: str, *, heatmap_view: str = 'year',
             focus_day: str | None = None, agent: str | None = None,
             device_id: str | None = None, session_range: bool = False) -> dict:
    first, last, start, end, zone = _range(day, through, tz)
    focus = date.fromisoformat(focus_day or day)
    if focus < first or focus > last:
        focus = first
    keys, grain = _heatmap_keys(first, last, heatmap_view)
    messages, intervals, meta = _activity_data(db, device_id)
    intervals = [row for row in intervals if agent in (None, row['agent'])]
    selected = [row for row in intervals if row['start'] < end and row['end'] > start]

    def measure(rows: list[dict], low: datetime, high: datetime) -> dict:
        slices = [(max(r['start'], low), min(r['end'], high)) for r in rows
                  if r['start'] < high and r['end'] > low]
        return {'agent_ms': sum(int((b-a).total_seconds()*1000) for a, b in slices),
                'wall_ms': _union_ms(slices), 'runs': len(slices),
                'verified_ms': sum(int((b-a).total_seconds()*1000) for r in rows
                                   if r['precision'] == 'verified' and r['start'] < high
                                   and r['end'] > low for a, b in
                                   [(max(r['start'], low), min(r['end'], high))])}

    summary = measure(selected, start, end)
    by_agent = {name: measure([r for r in selected if r['agent'] == name], start, end)
                for name in AGENTS}
    heatmap = []
    for key in keys:
        low, high = _bucket_bounds(key, grain, zone)
        heatmap.append({'period': key, **measure(selected, max(low, start), min(high, end))})
    low, high = ((start, end) if session_range else _bucket_bounds(focus.isoformat(), 'day', zone))
    focus_runs = [r for r in intervals if r['start'] < high and r['end'] > low]
    focus_messages = [r for r in messages if low <= _dt(r['occurred_at']) < high
                      and agent in (None, r['agent'])]
    grouped = {}
    for row in focus_messages:
        key = (row['device_id'], row['agent'], row['_native_id'])
        item = grouped.setdefault(key, {'device_id': key[0], 'agent': key[1], 'native_id': key[2],
                                        'title': '', 'first_at': row['occurred_at'],
                                        'last_at': row['occurred_at'], 'messages': 0,
                                        'user_turns': 0, 'preview': ''})
        item['first_at'] = min(item['first_at'], row['occurred_at'])
        item['last_at'] = max(item['last_at'], row['occurred_at'])
        item['messages'] += 1
        if row['role'] == 'user' and not _boilerplate(row.get('preview') or ''):
            item['user_turns'] += 1
            item['preview'] = item['preview'] or row.get('preview', '')
    for row in focus_runs:
        device, native_id = row['native_id'].split('/', 1)
        key = (device, row['agent'], native_id)
        at, until = max(row['start'], low).isoformat(), min(row['end'], high).isoformat()
        grouped.setdefault(key, {'device_id': device, 'agent': row['agent'], 'native_id': native_id,
                                 'title': '', 'first_at': at, 'last_at': until,
                                 'messages': 0, 'user_turns': 0, 'preview': ''})
    sessions = []
    for key, item in grouped.items():
        item['title'] = meta.get(key, {}).get('title') or item['preview'][:80] or f'{key[1]} · {key[2][:8]}'
        identity = f'{key[0]}/{key[2]}'
        item.update(measure([r for r in focus_runs if r['agent'] == key[1]
                             and r['native_id'] == identity], low, high))
        sessions.append(item)
    sessions.sort(key=lambda row: row['last_at'], reverse=session_range)
    return {'summary': summary, 'by_agent': by_agent, 'heatmap': heatmap,
            'heatmap_granularity': grain, 'focus_day': focus.isoformat(),
            'sessions': sessions, 'timeline': [], 'source': {'hermes_ready': True},
            'note': '时间来自 Workbench 底库；并行任务在自然经过时间中去重。完整任务事件为已证实，其余按消息间隔估算。'}


def browser(db: Database, day: str, through: str, tz: str, *, agent: str | None = None,
            query: str = '', device_id: str | None = None) -> dict:
    result = activity(db, day, through, tz, agent=agent, device_id=device_id, session_range=True,
                      heatmap_view='custom')
    sessions = result['sessions']
    needle = query.strip().casefold()
    if len(needle) > 120:
        raise ValueError('Search term too long')
    if needle:
        matches = {}
        for kind, field, label in [('message', 'body', '对话内容'), ('file', 'native_path', '文件路径')]:
            for row in _rows(db, kind, device_id):
                body = row.get(field) or ''
                if needle in body.casefold():
                    matches.setdefault(_session_key(row), {'type': label, 'excerpt': body[:160]})
        sessions = [{**row, 'match': matches.get(_session_key(row),
                     {'type': '标题', 'excerpt': row['title']})} for row in sessions
                    if _session_key(row) in matches or needle in row['title'].casefold()
                    or needle in row['preview'].casefold()]
    counts = {name: sum(row['agent'] == name for row in sessions) for name in AGENTS}
    meta = {_session_key(row): row for row in _rows(db, 'session', device_id)}
    local_id = archive.local_device_id(db)
    for row in sessions:
        item = meta.get(_session_key(row), {})
        row['storage_bytes'] = item.get('payload_bytes') if row['agent'] == 'hermes' else item.get('record_bytes')
        row['storage_kind'] = 'payload' if row['agent'] == 'hermes' else 'record'
        row['can_open_folder'] = row['device_id'] == local_id and any(
            Path(raw).parent.is_dir() for raw in item.get('sources', []))
    return {'day': day, 'through': through, 'query': query, 'counts': counts,
            'session_count': len(sessions), 'sessions': sessions}


def conversation(db: Database, agent: str, native_id: str, *, device_id: str,
                 day: str | None = None, through: str | None = None,
                 tz: str = 'Asia/Hong_Kong', view: str = 'full',
                 offset: int = 0, limit: int = 100) -> dict:
    if view not in {'full', 'key'}:
        raise ValueError('Invalid conversation view')
    rows = [row for row in _rows(db, 'message', device_id)
            if row['agent'] == agent and row['native_id'] == native_id]
    if day:
        _, _, start, end, _ = _range(day, through or day, tz)
        rows = [row for row in rows if start <= _date(row['occurred_at']) < end]
    rows.sort(key=lambda row: (row['occurred_at'], row.get('source_offset') or 0))
    if view == 'key':
        rows = _key_messages(rows, skip_boilerplate=agent == 'codex')
    return {'count': len(rows), 'items': [{'id': row['message_id'], 'role': row['role'],
            'occurred_at': row['occurred_at'], 'body': row.get('body') or ''}
            for row in rows[offset:offset+limit]]}


def inspector(db: Database, agent: str, native_id: str, *, device_id: str) -> dict:
    meta = next((row for row in _rows(db, 'session', device_id)
                 if row['agent'] == agent and row['native_id'] == native_id), {})
    local = device_id == archive.local_device_id(db)
    sources = []
    for raw in meta.get('sources', []):
        path = Path(raw)
        size = path.stat().st_size if local and path.is_file() else None
        sources.append({'path': raw, 'bytes': size,
                        'status': 'present' if size is not None else 'remote' if not local else 'missing'})
    events = []
    for row in _rows(db, 'file', device_id):
        if row['agent'] != agent or row['native_id'] != native_id:
            continue
        path = Path(row['native_path'])
        size = path.stat().st_size if local and path.is_file() else None
        events.append({**row, 'current_bytes': size,
                       'current_state': 'present' if size is not None else
                       'remote' if not local else 'missing'})
    events.sort(key=lambda row: (row['occurred_at'], row['fact_id']))
    confirmed = [row for row in events if row['relation'] != 'referenced']
    possible = [row for row in events if row['relation'] == 'referenced']
    return {'agent': agent, 'native_id': native_id, 'device_id': device_id,
            'cwd': meta.get('cwd'), 'sources': sources,
            'record_bytes': meta.get('record_bytes'), 'payload_bytes': meta.get('payload_bytes'),
            'file_events': events,
            'unique_file_count': len({row['native_path'] for row in confirmed}),
            'possible_file_count': len({row['native_path'] for row in possible}),
            'confirmed_event_count': len(confirmed),
            'coverage': '文件仅存索引，不备份文件内容。远端路径需在原电脑打开。'}
