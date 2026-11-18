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
          agent: str | None = None, native_id: str | None = None, session_keys=None) -> list[dict]:
    from .raw_archive import facts
    return facts(db, kind, device_id, start_at, end_at, summaries=summaries,
                 agent=agent, native_id=native_id, session_keys=session_keys)


def _utc_key(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _session_key(row: dict) -> tuple[str, str, str]:
    return row['device_id'], row['agent'], row['native_id']


def _date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _hermes_bounds(row):
    first = row.get('first_seen') if row.get('first_seen') is not None else row.get('range_first_seen')
    last = row.get('last_seen') if row.get('last_seen') is not None else row.get('range_last_seen')
    if first is None or last is None:
        return None
    return (datetime.fromtimestamp(float(first), timezone.utc),
            datetime.fromtimestamp(float(last), timezone.utc))


def _usage_point(row: dict, start: datetime, end: datetime):
    """Keep session aggregates and counter intervals out of false day buckets."""
    if row.get('precision') == 'unallocated':
        return None
    if row['agent'] == 'hermes':
        bounds = _hermes_bounds(row)
        if bounds is None:
            return None
        seen, at = bounds
        if seen < start or at >= end:
            return None
        return at, _usage_values(row)
    at = _date(row['occurred_at'])
    if not start <= at < end:
        return None
    if row.get('precision') == 'counter_interval' and _date(row['interval_start']) < start:
        return None
    return at, _usage_values(row)


def _usage_values(row: dict) -> dict:
    if row['agent'] == 'hermes':
        return _values(row.get('input_tokens'), row.get('cache_read_tokens'),
                       row.get('cache_write_tokens'), row.get('output_tokens'), row.get('api_call_count'))
    calls = 0 if row.get('precision') == 'counter_interval' else 1
    if row['agent'] == 'codex':
        original = max(0, row.get('input_tokens') or 0)
        read = min(original, max(0, row.get('cached_input_tokens') or 0))
        return _values(original-read, read, 0, row.get('output_tokens'), calls)
    return _values(row.get('input_tokens'), row.get('cached_input_tokens'),
                   row.get('cache_creation_tokens'), row.get('output_tokens'), calls)


def _bucket_attributed(row: dict, zone, grain: str) -> bool:
    """An aggregate belongs to a bucket only when BOTH native bounds fit it."""
    from .multi_usage import _bucket
    try:
        if row['agent']=='hermes':
            if row.get('first_seen') is None or row.get('last_seen') is None:return False
            low,high=_hermes_bounds(row)
        elif row.get('precision')=='counter_interval':
            low,high=_date(row['interval_start']),_date(row['occurred_at'])
        else:return True
        if high<low:return False
        def bucket(at):
            local=at.astimezone(zone)
            return local.replace(minute=0,second=0,microsecond=0).isoformat() if grain=='hour' else _bucket(local.date(),grain)
        return bucket(low)==bucket(high)
    except (ValueError,TypeError,KeyError,OverflowError):return False


def _daily_attributed(row: dict, zone) -> bool:
    return _bucket_attributed(row,zone,'day')


def _request_count_unknown(row):
    return (row['agent']=='hermes' and row.get('api_call_count') is None) or row.get('precision')=='counter_interval'


def usage(db: Database, day: str, through: str, tz: str, *, agent: str | None = None,
          model: str | None = None, heatmap_view: str = 'year', device_id: str | None = None) -> dict:
    if agent not in (*AGENTS, None):
        raise ValueError('Invalid agent')
    first, last, start, end, zone = _range(day, through, tz)
    heat_keys, heat_grain = _heatmap_keys(first, last, heatmap_view)
    grain = 'day' if (last-first).days < 31 else 'week' if (last-first).days < 180 else 'month'
    from .raw_archive import usage_range
    all_usage, global_sources = usage_range(db, start, end, device_id=device_id)
    sources = {name: _empty() for name in AGENTS}
    trend = defaultdict(_empty)
    heatmap = defaultdict(_empty)
    models = defaultdict(_empty)
    sessions = defaultdict(lambda: {**_empty(), 'title': '', 'last_request': '', 'agent': '', 'device_id': ''})
    summary = _empty()
    hermes_partial = 0
    unknown_requests=defaultdict(int)
    unattributed_heat=0
    for row in all_usage:
        name = row['agent']
        if name not in AGENTS:
            continue
        try:
            model_name = row.get('model') or 'unknown'
            if model and model_name != model:continue
            if name == 'hermes':
                bounds = _hermes_bounds(row)
                if bounds is not None:
                    seen, at = bounds
                    if (seen < start or at >= end) and seen < end and at >= start:
                        hermes_partial += 1
                        unknown_requests[name] += 1
            point = _usage_point(row, start, end)
            if point is None:
                if row.get('precision')=='counter_interval' and start<=_date(row['occurred_at'])<end:
                    unknown_requests[name]+=1
                continue
            at, values = point
            count_unknown=int(_request_count_unknown(row))
            unknown_requests[name] += count_unknown
            _add(sources[name], values)
            if agent not in (None, name):
                continue
            _add(summary, values)
            _add(models[(name, model_name)], values)
            key = _session_key(row)
            session = sessions[key]
            _add(session, values)
            session.update(agent=name, device_id=row['device_id'])
            session['last_request'] = max(session['last_request'], at.isoformat())
            if _bucket_attributed(row, zone, grain):
                local = at.astimezone(zone).date()
                period = (local.isoformat() if grain == 'day' else
                          local.replace(day=1).isoformat() if grain == 'month' else
                          (local.toordinal() - local.weekday()))
                if grain == 'week':
                    period = date.fromordinal(period).isoformat()
                _add(trend[period], values)
                trend[period]['request_count_unknown_rows']=trend[period].get('request_count_unknown_rows',0)+count_unknown
            if _bucket_attributed(row, zone, heat_grain):
                local = at.astimezone(zone).date()
                heat_key = (f'{local.isoformat()}T{at.astimezone(zone).hour:02d}'
                            if heat_grain == 'hour' else local.replace(day=1).isoformat()
                            if heat_grain == 'month' else local.isoformat())
                _add(heatmap[heat_key], values)
                heatmap[heat_key]['request_count_unknown_rows']=heatmap[heat_key].get('request_count_unknown_rows',0)+count_unknown
            else:unattributed_heat+=values['total_tokens']
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    source_meta = {}
    for name in AGENTS:
        info = (global_sources or {}).get(name, {})
        intervals = info.get('counter_intervals', 0)
        exact = info.get('exact', False)
        source_meta[name] = {**sources[name], 'status': info.get('status', 'source_missing'),
                             'request_count_unknown_rows':unknown_requests[name],
                             'precision': 'session_model_aggregate' if name == 'hermes' else
                                          'mixed_request_and_counter_interval' if intervals and exact else
                                          'counter_interval' if intervals else 'request',
                             'files': 0, 'deferred': hermes_partial if name == 'hermes' else 0,
                             'earliest': info.get('earliest'), 'latest': info.get('latest'),
                             'partial_rows': hermes_partial if name == 'hermes' else 0,
                             'missing_cache_read': info.get('missing_cache_read', 0) if name == 'hermes' else 0,
                             'missing_cache_write': info.get('missing_cache_write', 0) if name == 'hermes' else 0,
                             'unknown_call_count_rows': info.get('unknown_call_count_rows', 0),
                             'counter_intervals': intervals, 'unallocated_rows': info.get('unallocated_rows', 0)}
    result_sessions = [{'native_id': key[2], **value} for key, value in
                       sorted(sessions.items(), key=lambda item: item[1]['last_request'], reverse=True)]
    from .session_identity import session_metadata, kind_counts
    identities = session_metadata(db)
    for row in result_sessions:
        meta = identities.get(_session_key(row), {})
        row['title'] = meta.get('title') or f'{row["agent"]} · {row["native_id"][:8]}'
        row.update({key:meta.get(key) for key in ('session_kind','session_source','created_at')})
        row['session_kind'] = row['session_kind'] or 'unknown'
    summary['cache_hit_rate'] = (summary['cached_input_tokens'] / summary['input_tokens']
                                  if summary['input_tokens'] else None)
    summary['request_count_unknown_rows']=sum(v for name,v in unknown_requests.items() if agent in (None,name))
    return {'status': 'ready', 'summary': summary, 'sources': source_meta,
            'projection': (global_sources or {}).get('_projection'),
            'diagnostics': (global_sources or {}).get('_diagnostics'),
            'models': [{'agent': name, 'model': model_name, **value} for (name, model_name), value in
                       sorted(models.items(), key=lambda item: -item[1]['total_tokens'])],
            'sessions': result_sessions[:100], 'session_count': len(result_sessions),
            'session_counts': kind_counts(result_sessions),
            'trend': [{'period': key, **trend[key]} for key in _trend_dates(first, last, grain)],
            'trend_granularity': grain, 'heatmap': [{'period': key, **heatmap[key]} for key in heat_keys],
            'heatmap_view': heatmap_view, 'heatmap_granularity': heat_grain,
            'unallocated_usage': [{'device_id': r['device_id'], 'agent': r['agent'],
                'native_id': r['native_id'], 'source_id': r.get('source_id'),
                'usage_key': r.get('usage_key'), 'amount': r.get('total_tokens'),
                'reason': r.get('omission_reason'), 'until': r.get('source_time') or r.get('occurred_at')}
                for r in all_usage if r.get('precision') == 'unallocated'
                and agent in (None, r['agent']) and (not model or r.get('model') == model)],
            'unattributed_tokens': unattributed_heat,
            'note': '统计来自 Workbench 底库；请求数为已知API调用，不含无法确定的计数器调用数。Hermes按原生模型汇总计入，只有完整落在同一时间格内才进入该格，跨格不摊分。'}


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
    from .raw_archive import usage_range
    candidates, _ = usage_range(db, start, end, device_id=device_id, agent=agent)
    for row in candidates:
        name = row['agent']
        model_name = row.get('model') or 'unknown'
        if name not in AGENTS or agent not in (None, name) or (model and model != model_name):
            continue
        try:
            point = _usage_point(row, start, end)
            if point is None:
                continue
            at, values = point
            key = (name, model_name)
            _add(models[key], values)
            models[key]['request_count_unknown_rows']=models[key].get('request_count_unknown_rows',0)+int(_request_count_unknown(row))
            local = at.astimezone(zone)
            day_key = local.date()
            period = (day_key.isoformat() if grain == 'day' else
                      day_key.replace(day=1).isoformat() if grain == 'month' else
                      date.fromordinal(day_key.toordinal()-day_key.weekday()).isoformat())
            if _bucket_attributed(row,zone,grain):_add(series[(period, name, model_name)], values)
            if _bucket_attributed(row,zone,'hour'):_add(hours[(local.hour, name, model_name)], values)
            if name!='hermes' and row.get('precision')!='counter_interval':
                points.append({'agent': name, 'model': model_name, 'at': at.isoformat(),
                               'precision': row.get('precision') or 'request',
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
        'note': '原生汇总完整落在同一日/月/小时内才进入对应分布；跨格不摊分。单次请求图只含请求级记录，不把Hermes汇总或计数器差分当作一笔请求。'}


def requests(db: Database, agent: str, native_id: str, day: str, through: str, tz: str,
             *, model: str | None = None, limit: int = 100, device_id: str | None = None) -> dict:
    _, _, start, end, _ = _range(day, through, tz)
    rows = []
    from .raw_archive import usage_range
    candidates, _ = usage_range(db, start, end, device_id=device_id, agent=agent, native_id=native_id)
    for row in candidates:
        if row['agent'] != agent or row['native_id'] != native_id:
            continue
        if model and row.get('model') != model:
            continue
        point = _usage_point(row, start, end)
        if point is None:
            continue
        at, value = point
        rows.append({'request_id': row.get('request_id', row['fact_id']),
                     'occurred_at': at.isoformat(), 'model': row.get('model') or 'unknown',
                     'first_seen': row.get('first_seen'), 'time_basis': row.get('time_basis'),
                     'api_call_count': row.get('api_call_count'),
                     'precision': row.get('precision') or ('session_model_aggregate' if agent == 'hermes' else 'request'),
                     **value})
    rows.sort(key=lambda row: row['occurred_at'], reverse=True)
    return {'items': rows[:limit], 'count': len(rows),
            'precision': 'session_model_aggregate' if agent == 'hermes' else 'request'}


def _activity_data(db: Database, device_id: str | None,
                   start: datetime, end: datetime, *, include_range_messages=False,
                   session_keys=None, _reader=None, _windows=None):
    # Estimates can cross a boundary by at most six hours. Keep those nearby
    # messages while avoiding a full-history decode for a one-day view.
    low = _utc_key(start - timedelta(seconds=MAX_ESTIMATE_SECONDS + 1))
    high = _utc_key(end + timedelta(seconds=MAX_ESTIMATE_SECONDS + 1))
    reader = _reader or _rows
    if include_range_messages:
        if _windows is not None:
            messages, range_messages = _windows()
        else:
            from .raw_archive import message_windows
            messages, range_messages = message_windows(db, device_id, low, high, _utc_key(start), _utc_key(end))
    else:
        messages = reader(db, 'message', device_id, low, high, summaries=True)
    from .time_evidence import apply as apply_turn_boundaries
    apply_turn_boundaries(db, messages)
    # Interval calculations need a short preview, never the complete transcript.
    for row in messages:
        row.pop('body', None)
        row.pop('search_body', None)
    runs = reader(db, 'run', device_id)
    meta = {_session_key(row): row for row in reader(db, 'session', device_id)}
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
        if (trusted_run(row) and row.get('start_at') and row.get('end_at')
                and row['end_at'] > row['start_at'] and not row.get('parent_run_ref')
                and (not row.get('raw_archive_version') or
                     (row.get('status') in {'completed', 'failed', 'cancelled'}
                      and row.get('duration_basis') in {'source', 'start_end'}))):
            intervals.append({'agent': row['agent'], 'native_id': row['native_id'],
                              'start': _dt(row['start_at']), 'end': _dt(row['end_at']),
                              'precision': 'verified'})
    verified = {row['native_id'] for row in intervals}
    intervals += _estimated([row for row in messages if row['agent'] == 'codex'
                             and row['native_id'] not in verified], 'codex')
    for name in ('claude', 'hermes'):
        intervals += _estimated([row for row in messages if row['agent'] == name], name)
    return (messages, intervals, meta, range_messages) if include_range_messages else (messages, intervals, meta)


def _measure(rows: list[dict], low: datetime, high: datetime) -> dict:
    slices = [(max(r['start'], low), min(r['end'], high)) for r in rows
              if r['start'] < high and r['end'] > low]
    return {'agent_ms': sum(int((b-a).total_seconds()*1000) for a, b in slices),
            'wall_ms': _union_ms(slices), 'runs': len(slices),
            'verified_ms': sum(int((b-a).total_seconds()*1000) for r in rows
                               if r['precision'] == 'verified' and r['start'] < high
                               and r['end'] > low for a, b in
                               [(max(r['start'], low), min(r['end'], high))])}


def _activity_sessions(messages, intervals, meta, low, high, agent, *, reverse):
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
    # Session observations and aggregate-only sessions are real metadata even
    # when no message bodies/rows were supplied. Keep them browsable, never invent
    # a message or activity duration to make a session appear.
    for key, item in meta.items():
        if agent not in (None, key[1]) or key in grouped:
            continue
        first_at, last_at = item.get('first_at'), item.get('last_at')
        if not first_at or not last_at:
            continue
        if _dt(first_at) < high and _dt(last_at) >= low:
            grouped[key] = {'device_id': key[0], 'agent': key[1], 'native_id': key[2],
                'title': '', 'first_at': first_at, 'last_at': last_at,
                'messages': 0, 'user_turns': 0, 'preview': ''}
    by_session = defaultdict(list)
    for row in focus_runs:
        by_session[(row['agent'], row['native_id'])].append(row)
    sessions = []
    for key, item in grouped.items():
        if item['preview'].lstrip().startswith('<external_'):
            item['preview'] = ''
        item['title'] = meta.get(key, {}).get('title') or item['preview'][:80] or f'{key[1]} · {key[2][:8]}'
        item['session_kind'] = meta.get(key, {}).get('session_kind', 'unknown')
        item['session_source'] = meta.get(key, {}).get('session_source')
        identity = f'{key[0]}/{key[2]}'
        item.update(_measure(by_session[(key[1], identity)], low, high))
        sessions.append(item)
    sessions.sort(key=lambda row: row['last_at'], reverse=reverse)
    return sessions


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

    summary = _measure(selected, start, end)
    by_agent = {name: _measure([r for r in selected if r['agent'] == name], start, end)
                for name in AGENTS}
    heatmap = []
    for key in keys:
        low, high = _bucket_bounds(key, grain, zone)
        heatmap.append({'period': key, **_measure(selected, max(low, start), min(high, end))})
    low, high = ((start, end) if session_range else _bucket_bounds(focus.isoformat(), 'day', zone))
    sessions = _activity_sessions(messages, intervals, meta, low, high, agent, reverse=session_range)
    return {'summary': summary, 'by_agent': by_agent, 'heatmap': heatmap,
            'heatmap_granularity': grain, 'focus_day': focus.isoformat(),
            'sessions': sessions, 'timeline': [], 'source': {'hermes_ready': True,
                'unknown_turn_boundaries':sum(r.get('turn_final_basis')=='native_finish_reason_unavailable' for r in messages)},
            'note': '时间来自已入库记录；并行任务合并重叠区间。Hermes仅按用户到最终助手答复估算，工具返回不结束轮次，消息保存完成不等于任务结束。缺失原生结束信息的记录不冒充已证实；未同步数据不计入。'}


def _browser_uncached(db: Database, day: str, through: str, tz: str, *, agent: str | None = None,
            query: str = '', device_id: str | None = None, search_in: str = 'all',
            sort: str = 'recent', min_text: int = 0, min_duration: int = 0,
            has_files: bool = False, limit: int = 300,
            source_id: str | None = None, profile: str | None = None, _base=False,
            _session_keys=None, _reader=None, _windows=None, _offset=0, session_scope='all') -> dict:
    sort_keys = {'recent', 'oldest', 'text_desc', 'text_asc', 'duration_desc',
                 'duration_asc', 'storage_desc', 'storage_asc', 'messages_desc',
                 'messages_asc', 'files_desc'}
    if sort not in sort_keys or search_in not in {'all', 'title', 'content', 'file'}:
        raise ValueError('Invalid session filter')
    if min_text < 0 or min_duration < 0 or not 1 <= limit <= 1000:
        raise ValueError('Invalid session filter')
    _, _, low, high, _ = _range(day, through, tz)
    messages, intervals, meta, range_messages = _activity_data(db, device_id, low, high, include_range_messages=True,
        **({'session_keys': _session_keys, '_reader': _reader, '_windows': _windows}
           if _session_keys is not None else {}))
    intervals = [r for r in intervals if agent in (None, r['agent'])
        and (_session_keys is None or (r['native_id'].split('/', 1)[0], r['agent'],
                                      r['native_id'].split('/', 1)[1]) in _session_keys)]
    sessions = _activity_sessions(messages, intervals, meta, low, high, agent, reverse=True)
    needle = query.strip().casefold()
    if len(needle) > 120:
        raise ValueError('Search term too long')
    session_keys = {_session_key(row) for row in sessions}
    text_sizes = defaultdict(int)
    matches = {}
    if needle and search_in in {'all', 'content'}:
        from .raw_archive import search_messages
        text_rows = search_messages(db, device_id, _utc_key(low), _utc_key(high), needle, session_keys)
    else:
        text_rows = range_messages
    for row in text_rows:
        key = _session_key(row)
        if key not in session_keys or not low <= _date(row['occurred_at']) < high:
            continue
        text_sizes[key] += row['body_chars']
        if row.get('_content_match') is not None:
            matches.setdefault(key, {'type': '对话内容', 'excerpt': row['_content_match']})
    file_paths = defaultdict(set)
    for row in (_reader or _rows)(db, 'file', device_id, session_keys=session_keys):
        key = _session_key(row)
        if key not in session_keys:
            continue
        path = row.get('native_path') or ''
        if row.get('relation') != 'referenced':
            file_paths[key].add(path)
        if needle and search_in in {'all', 'file'} and needle in path.casefold():
            matches.setdefault(key, {'type': '文件路径', 'excerpt': path[:160]})
    for row in sessions:
        key = _session_key(row)
        row['text_chars'] = text_sizes[key]
        row['file_count'] = len(file_paths[key])
        if needle:
            title_match = search_in in {'all', 'title'} and needle in row['title'].casefold()
            row['match'] = matches.get(key) or ({'type': '标题', 'excerpt': row['title']}
                                                 if title_match else None)
        item = meta.get(_session_key(row), {})
        row.update(source_id=item.get('source_id'), profile=item.get('profile'),
                   native_session_id=item.get('native_session_id', row['native_id']))
        row['storage_bytes'] = item.get('payload_bytes') if row['agent'] == 'hermes' else item.get('record_bytes')
        row['storage_kind'] = 'payload' if row['agent'] == 'hermes' else 'record'
        row['_sources'] = item.get('sources', [])
    from .session_identity import scope_sql
    scope_sql(session_scope)
    sessions = [row for row in sessions if (session_scope == 'all' or
                (row['session_kind'] == 'internal' if session_scope == 'internal' else row['session_kind'] != 'internal'))
                and (not needle or row.get('match'))
                and (not source_id or row.get('source_id') == source_id)
                and (profile is None or row.get('profile') == profile)
                and row['text_chars'] >= min_text and row['agent_ms'] >= min_duration*60000
                and (not has_files or row['file_count'] > 0)]
    if _base:
        return sessions
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
    count = len(sessions)
    sessions = sessions[_offset:_offset+limit]
    local_id = archive.local_device_id(db)
    for row in sessions:
        sources = row.pop('_sources', [])
        row['can_open_folder'] = row['device_id'] == local_id and any(
            Path(raw).parent.is_dir() for raw in sources)
    return {'day': day, 'through': through, 'query': query, 'counts': counts,
            'session_count': count, 'sessions': sessions, 'sort': sort}


def conversation(db: Database, agent: str, native_id: str, *, device_id: str,
                 day: str | None = None, through: str | None = None,
                 tz: str = 'Asia/Hong_Kong', view: str = 'full',
                 offset: int = 0, limit: int = 100) -> dict:
    if view not in {'full', 'key'}:
        raise ValueError('Invalid conversation view')
    window = _range(day, through or day, tz) if day else None
    from .raw_archive import message_page
    count, rows = message_page(db, agent, native_id, device_id=device_id,
        start_at=_utc_key(window[2]) if window else None,
        end_at=_utc_key(window[3]) if window else None,
        start=window[2] if window else None, end=window[3] if window else None,
        view=view, offset=offset, limit=limit)
    return {'count': count, 'items': [{'id': row['message_id'], 'role': row['role'],
            'occurred_at': row['occurred_at'], 'body': row.get('body') or '',
            'content_state': row.get('content_state') or ('full' if row.get('body') is not None else 'missing'),
            'omission_reason': row.get('omission_reason'), 'source_id': row.get('source_id'),
            'profile': row.get('profile')}
            for row in rows]}


def inspector(db: Database, agent: str, native_id: str, *, device_id: str) -> dict:
    meta = next((row for row in _rows(db, 'session', device_id, agent=agent, native_id=native_id)
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


def _add_session_metadata(db: Database, rows: list[dict]) -> None:
    if not rows:
        return
    from .usage_projection import ProjectionInitializing, projection

    keys = [_session_key(row) for row in rows]
    with db.read() as conn:
        names = dict(conn.execute('SELECT id,name FROM archive_devices'))
    try:
        with projection(db).snapshot() as snapshot:
            totals = snapshot.session_tokens(keys)
    except ProjectionInitializing:
        totals = {}
    for row, key in zip(rows, keys):
        name = names.get(row['device_id'])
        row['device_name'] = (name or '').strip() or None
        row['session_total_tokens'], row['session_tokens_partial'] = totals.get(key, (None, False))


def browser(db: Database, day: str, through: str, tz: str, *, agent: str | None = None,
            query: str = '', device_id: str | None = None, search_in: str = 'all',
            sort: str = 'recent', min_text: int = 0, min_duration: int = 0,
            has_files: bool = False, limit: int = 300,
            source_id: str | None = None, profile: str | None = None, offset: int = 0,
            session_scope: str = 'conversation') -> dict:
    from .query_browser import browser as query_browser
    result = query_browser(db, day, through, tz, agent=agent, query=query,
        device_id=device_id, search_in=search_in, sort=sort, min_text=min_text,
        min_duration=min_duration, has_files=has_files, limit=limit, offset=offset,
        source_id=source_id, profile=profile, session_scope=session_scope)
    _add_session_metadata(db, result.get('sessions', []))
    return result
