"""Conversation identity from explicit native provenance, never titles or roles.

`interactive` means top-level interactive source, not proof of a manual click.
Unknown historical provenance remains unknown; token totals never depend on kind.
"""
from __future__ import annotations
import json

KINDS = ('interactive', 'internal', 'unknown')
SCOPES = ('conversation', 'internal', 'all')
HERMES_SIZE_FACT = 'hermes-content-bytes-v1'
_INTERNAL = {'tool', 'oneshot', 'subagent', 'cron', 'kanban', 'workflow', 'background'}
_INTERACTIVE = {'desktop', 'gui', 'cli', 'local', 'discord', 'telegram', 'slack',
                'whatsapp', 'imessage', 'signal', 'matrix', 'teams', 'qqbot', 'web', 'tui', 'vscode'}


def identity(agent, payload):
    native = payload.get('native', {}).get('native_json')
    try:
        record = json.loads(native) if isinstance(native, str) else {}
    except (ValueError, TypeError):
        record = {}
    if not isinstance(record, dict):
        record = {}
    if agent == 'codex':
        record = record.get('payload', {}) if record.get('type') == 'session_meta' else record
    if not isinstance(record, dict):
        record = {}
    source = payload.get('session_source', record.get('source'))
    parent = payload.get('parent_session_id') or record.get('parent_session_id') or record.get('parent_id')
    if isinstance(source, dict):
        sub = source.get('subagent')
        if sub:
            parent = parent or (sub.get('thread_spawn', {}).get('parent_thread_id') if isinstance(sub, dict) else None)
            source = 'subagent'
        else:
            source = None
    source = source.casefold() if isinstance(source, str) else None
    kind = ('internal' if parent or source in _INTERNAL else
            'interactive' if source in _INTERACTIVE else 'unknown')
    return {'session_kind': kind, 'session_source': source, 'parent_session_id': parent,
            'created_at': payload.get('created_at'), 'session_identity_version': 1}


def observed_identity(conn, source_id, native_id, agent):
    row = conn.execute('''SELECT content_json FROM events
        WHERE source_id=? AND native_session_id=? AND fact_kind='session.observed' AND native_fact_id<>?
        ORDER BY rowid DESC LIMIT 1''', (source_id, native_id, HERMES_SIZE_FACT)).fetchone()
    return identity(agent, json.loads(row['content_json'])['payload']) if row else identity(agent, {})


def kind_counts(rows):
    counts = dict.fromkeys(KINDS, 0)
    for row in rows:
        kind = row.get('session_kind') or 'unknown'
        counts[kind if kind in KINDS else 'unknown'] += 1
    return {**counts, 'total': sum(counts.values())}


def session_metadata(db):
    """Read small provenance facts without rebuilding transcript-derived rows.

    Existing recent catalogs keep their valid activity values. Identity overlays
    use the same canonical-device/deduplication policy as archive readers.
    """
    from .raw_archive import _dedupe
    with db.read() as c:
        aliases = dict(c.execute('SELECT alias_id,canonical_id FROM raw_archive_device_aliases'))
        rows = []
        for r in c.execute("SELECT rowid,* FROM archive_facts WHERE kind='session' ORDER BY occurred_at,device_id,agent,rowid"):
            p = json.loads(r['payload_json'])
            rows.append({**p, 'device_id': aliases.get(r['device_id'], r['device_id']),
                'agent': r['agent'], 'native_id': r['native_id'], 'fact_id': r['fact_id'],
                'occurred_at': r['occurred_at'], '_origin_device_id': r['device_id']})
    return {(r['device_id'],r['agent'],r['native_id']):
            {**r,'session_kind':r.get('session_kind','unknown')}
            for r in _dedupe(rows, 'session')}


def classifications(db):
    return {key:{field:row.get(field) for field in ('session_kind','session_source','created_at')}
            for key,row in session_metadata(db).items()}


def created_counts(db, day, through, tz, *, agent=None, device_id=None):
    """Window creation is independent of usage attribution and model filters."""
    from .multi_usage import _range, AGENTS
    from datetime import datetime
    if agent not in (*AGENTS, None):
        raise ValueError('Invalid agent')
    _, _, start, end, _ = _range(day, through, tz)
    with db.read() as c:
        aliases = dict(c.execute('SELECT alias_id,canonical_id FROM raw_archive_device_aliases'))
    selected, by_source, undated = [], {}, 0
    canonical = aliases.get(device_id, device_id)
    for (device, name, _), item in classifications(db).items():
        if (device_id and device != canonical) or agent not in (None, name):
            continue
        try:
            at = datetime.fromisoformat((item.get('created_at') or '').replace('Z', '+00:00'))
            within = start <= at < end
        except (ValueError, TypeError):
            undated += 1
            continue
        if within:
            selected.append(item)
            if item['session_kind'] == 'interactive':
                source = item.get('session_source') or 'unknown'
                by_source[source] = by_source.get(source, 0) + 1
    return {'created':kind_counts(selected), 'interactive_sources':by_source,
            'undated_historical_records':undated,
            'note':'按原生创建时间统计顶层对话；不是有用量/活跃会话数，不按模型筛选。后台与未知单列，Token不受影响。'}


def scope_sql(scope, column='payload_json'):
    if scope not in SCOPES:
        raise ValueError('Invalid session scope')
    kind = f"coalesce(json_extract({column},'$.session_kind'),'unknown')"
    return f"{kind}='internal'" if scope == 'internal' else f"{kind}<>'internal'" if scope == 'conversation' else '1'
