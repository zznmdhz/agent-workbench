"""Read projections over durable Workbench facts, independent of Agent stores."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import archive
from .activity import (
    MAX_ESTIMATE_SECONDS,
    _boilerplate,
    _bucket_bounds,
    _dt,
    _estimated,
    _key_messages,
    _union_ms,
)
from .db import Database
from .multi_usage import _add, _empty, _heatmap_keys, _range, _trend_dates, _values

AGENTS = ('codex', 'claude', 'hermes')


def _rows(db: Database, kind: str, device_id: str | None,
          start_at: str | None = None, end_at: str | None = None, *, summaries: bool = False,
          agent: str | None = None, native_id: str | None = None) -> list[dict]:
    return archive.facts(db, kind, device_id, start_at, end_at, summaries=summaries,
                         agent=agent, native_id=native_id)


def _utc_key(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


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
                          local.replace(day=1).isoformat() if grain == 'month' else
                          (local.toordinal() - local.weekday()))
                if grain == 'week':
                    period = date.fromordinal(period).isoformat()
                _add(trend[period], values)
                heat_key = (f'{local.isoformat()}T{at.astimezone(zone).hour:02d}'
                            if heat_grain == 'hour' else local.replace(day=1).isoformat()
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


def model_report(db: Database, day: str, through: str, tz: str, *, agent: str | None = None,
                 model: str | None = None, device_id: str | None = None) -> dict:
    """Small, chart-ready aggregates; never assign Hermes session totals to a day."""
    if agent not in (*AGENTS, None):
        raise ValueError('Invalid agent')
    first, last, start, end, zone = _range(day, through, tz)
    grain = 'day' if (last-first).days < 45 else 'week' if (last-first).days < 180 else 'month'
    models = defaultdict(_empty)
    series = defaultdict(_empty)
    hours = defaultdict(_empty)
    points = []
    for row in _rows(db, 'usage', device_id):
        name = row['agent']
        model_name = row.get('model') or 'unknown'
        if name not in AGENTS or agent not in (None, name) or (model and model != model_name):
            continue
        try:
            if name == 'hermes':
                seen = datetime.fromtimestamp(float(row['first_seen']), timezone.utc)
                at = datetime.fromtimestamp(float(row['last_seen']), timezone.utc)
                if seen < start or at >= end:
                    continue
                values = _values(row['input_tokens'], row['cache_read_tokens'],
                                 row['cache_write_tokens'], row['output_tokens'], row['api_call_count'])
            else:
                at = _date(row['occurred_at'])
                if not start <= at < end:
                    continue
                if name == 'codex':
                    original = max(0, row['input_tokens'])
                    read = min(original, max(0, row['cached_input_tokens']))
                    values = _values(original-read, read, 0, row['output_tokens'])
                else:
                    values = _values(row['input_tokens'], row['cached_input_tokens'],
                                     row['cache_creation_tokens'], row['output_tokens'])
            key = (name, model_name)
            _add(models[key], values)
            if name == 'hermes':
                continue
            local = at.astimezone(zone)
            day_key = local.date()
            period = (day_key.isoformat() if grain == 'day' else
                      day_key.replace(day=1).isoformat() if grain == 'month' else
                      date.fromordinal(day_key.toordinal()-day_key.weekday()).isoformat())
            _add(series[(period, name, model_name)], values)
            _add(hours[(local.hour, name, model_name)], values)
            points.append({'agent': name, 'model': model_name, 'at': row['occurred_at'],
                           'input_tokens': values['input_tokens'], 'output_tokens': values['output_tokens'],
                           'cached_input_tokens': values['cached_input_tokens'],
                           'total_tokens': values['total_tokens']})
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    # Even sampling preserves the whole selected period while keeping the response small.
    points.sort(key=lambda item: item['at'])
    if len(points) > 400:
        points = [points[i*(len(points)-1)//399] for i in range(400)]
    return {'grain': grain, 'periods': _trend_dates(first, last, grain), 'models': [
        {'agent': name, 'model': model_name, **value,
         'cache_hit_rate': value['cached_input_tokens']/value['input_tokens']
         if value['input_tokens'] else None}
        for (name, model_name), value in sorted(models.items(), key=lambda item: -item[1]['total_tokens'])],
        'series': [{'period': period, 'agent': name, 'model': model_name, **value}
                   for (period, name, model_name), value in sorted(series.items())],
        'hours': [{'hour': hour, 'agent': name, 'model': model_name, **value}
                  for (hour, name, model_name), value in sorted(hours.items())],
        'points': points, 'point_count': sum(value['requests'] for key, value in models.items()
                                            if key[0] != 'hermes'),
        'note': 'Hermes 只有完整会话的模型汇总，参与总量比较，不进入逐日、逐时和单次请求图。'}


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


def _activity_data(db: Database, device_id: str | None,
                   start: datetime, end: datetime):
    # Estimates can cross a boundary by at most six hours. Keep those nearby
    # messages while avoiding a full-history decode for a one-day view.
    low = _utc_key(start - timedelta(seconds=MAX_ESTIMATE_SECONDS + 1))
    high = _utc_key(end + timedelta(seconds=MAX_ESTIMATE_SECONDS + 1))
    messages = _rows(db, 'message', device_id, low, high, summaries=True)
    # Interval calculations need a short preview, never the complete transcript.
    for row in messages:
        row.pop('body', None)
        row.pop('search_body', None)
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
    from .activity import trusted_run
    for row in runs:
        if trusted_run(row) and row['end_at'] > row['start_at']:
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
    messages, intervals, meta = _activity_data(db, device_id, start, end)
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
        if item['preview'].lstrip().startswith('<external_'):
            item['preview'] = ''
        item['title'] = meta.get(key, {}).get('title') or item['preview'][:80] or f'{key[1]} · {key[2][:8]}'
        identity = f'{key[0]}/{key[2]}'
        item.update(measure([r for r in focus_runs if r['agent'] == key[1]
                             and r['native_id'] == identity], low, high))
        sessions.append(item)
    sessions.sort(key=lambda row: row['last_at'], reverse=session_range)
    return {'summary': summary, 'by_agent': by_agent, 'heatmap': heatmap,
            'heatmap_granularity': grain, 'focus_day': focus.isoformat(),
            'sessions': sessions, 'timeline': [], 'source': {'hermes_ready': True},
            'note': '时间来自 Workbench 底库；并行任务在自然经过时间中去重。原生可信任务事件为已证实，导入合成边界不计入，其余按消息间隔估算。'}


def browser(db: Database, day: str, through: str, tz: str, *, agent: str | None = None,
            query: str = '', device_id: str | None = None, search_in: str = 'all',
            sort: str = 'recent', min_text: int = 0, min_duration: int = 0,
            has_files: bool = False, limit: int = 300) -> dict:
    sort_keys = {'recent', 'oldest', 'text_desc', 'text_asc', 'duration_desc',
                 'duration_asc', 'storage_desc', 'storage_asc', 'messages_desc',
                 'messages_asc', 'files_desc'}
    if sort not in sort_keys or search_in not in {'all', 'title', 'content', 'file'}:
        raise ValueError('Invalid session filter')
    if min_text < 0 or min_duration < 0 or not 1 <= limit <= 1000:
        raise ValueError('Invalid session filter')
    _, _, low, high, _ = _range(day, through, tz)
    result = activity(db, day, through, tz, agent=agent, device_id=device_id, session_range=True,
                      heatmap_view='custom')
    sessions = result['sessions']
    needle = query.strip().casefold()
    if len(needle) > 120:
        raise ValueError('Search term too long')
    session_keys = {_session_key(row) for row in sessions}
    text_sizes = defaultdict(int)
    matches = {}
    for row in _rows(db, 'message', device_id, _utc_key(low), _utc_key(high),
                     summaries=not needle or search_in not in {'all', 'content'}):
        key = _session_key(row)
        if key not in session_keys or not low <= _date(row['occurred_at']) < high:
            continue
        body = row.get('body') or ''
        text_sizes[key] += row.get('body_chars', len(body))
        if needle and search_in in {'all', 'content'} and needle in body.casefold():
            matches.setdefault(key, {'type': '对话内容', 'excerpt': body[:160]})
    file_paths = defaultdict(set)
    for row in _rows(db, 'file', device_id):
        key = _session_key(row)
        if key not in session_keys:
            continue
        path = row.get('native_path') or ''
        if row.get('relation') != 'referenced':
            file_paths[key].add(path)
        if needle and search_in in {'all', 'file'} and needle in path.casefold():
            matches.setdefault(key, {'type': '文件路径', 'excerpt': path[:160]})
    meta = {_session_key(row): row for row in _rows(db, 'session', device_id)}
    local_id = archive.local_device_id(db)
    for row in sessions:
        key = _session_key(row)
        row['text_chars'] = text_sizes[key]
        row['file_count'] = len(file_paths[key])
        if needle:
            title_match = search_in in {'all', 'title'} and needle in row['title'].casefold()
            row['match'] = matches.get(key) or ({'type': '标题', 'excerpt': row['title']}
                                                 if title_match else None)
        item = meta.get(_session_key(row), {})
        row['storage_bytes'] = item.get('payload_bytes') if row['agent'] == 'hermes' else item.get('record_bytes')
        row['storage_kind'] = 'payload' if row['agent'] == 'hermes' else 'record'
        row['can_open_folder'] = row['device_id'] == local_id and any(
            Path(raw).parent.is_dir() for raw in item.get('sources', []))
    sessions = [row for row in sessions if (not needle or row.get('match'))
                and row['text_chars'] >= min_text and row['agent_ms'] >= min_duration*60000
                and (not has_files or row['file_count'] > 0)]
    counts = {name: sum(row['agent'] == name for row in sessions) for name in AGENTS}
    sort_field = {'recent': 'last_at', 'oldest': 'last_at', 'text_desc': 'text_chars',
                  'text_asc': 'text_chars', 'duration_desc': 'agent_ms',
                  'duration_asc': 'agent_ms', 'storage_desc': 'storage_bytes',
                  'storage_asc': 'storage_bytes', 'messages_desc': 'messages',
                  'messages_asc': 'messages', 'files_desc': 'file_count'}[sort]
    descending = sort not in {'oldest', 'text_asc', 'duration_asc', 'storage_asc', 'messages_asc'}
    sessions.sort(key=lambda row: (row[sort_field] if row[sort_field] is not None else
                                   (-1 if descending else float('inf')), row['last_at']),
                  reverse=descending)
    return {'day': day, 'through': through, 'query': query, 'counts': counts,
            'session_count': len(sessions), 'sessions': sessions[:limit], 'sort': sort}


def conversation(db: Database, agent: str, native_id: str, *, device_id: str,
                 day: str | None = None, through: str | None = None,
                 tz: str = 'Asia/Hong_Kong', view: str = 'full',
                 offset: int = 0, limit: int = 100) -> dict:
    if view not in {'full', 'key'}:
        raise ValueError('Invalid conversation view')
    window = _range(day, through or day, tz) if day else None
    rows = [row for row in _rows(db, 'message', device_id,
                                _utc_key(window[2]) if window else None,
                                _utc_key(window[3]) if window else None,
                                agent=agent, native_id=native_id)
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
    for row in _rows(db, 'file', device_id, agent=agent, native_id=native_id):
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
