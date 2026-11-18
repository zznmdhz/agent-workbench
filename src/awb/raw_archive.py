"""Raw HTTP -> durable MVP archive, without opening a native Agent store.

Identity is explicit: a source belongs to its registered collector, not to a
hostname. Non-default profiles get an opaque, stable session namespace. Historical
archive IDs may be bound by the migration caller, with evidence, BEFORE backfill.
Raw tables and existing non-bridge archive facts are never rewritten or deleted.
"""
from __future__ import annotations

import json
import sqlite3
from bisect import bisect_right
from collections import defaultdict
from contextlib import closing

from . import archive
from .codec import iso_utc, sha256, utc_ms
from .db import Database

_SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS raw_archive_sources(
        source_id TEXT PRIMARY KEY, archive_device_id TEXT NOT NULL,
        namespace TEXT NOT NULL, evidence TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS raw_archive_device_aliases(
        alias_id TEXT PRIMARY KEY, canonical_id TEXT NOT NULL, evidence TEXT NOT NULL)''',
)


def initialize(db: Database) -> None:
    """Schema only; unlike archive.initialize this never changes local identity."""
    with db.read() as conn:
        ready = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('archive_facts','archive_log','archive_message_summaries','archive_settings')").fetchone()[0] == 4
    if not ready:
        with closing(db.connect()) as conn:
            conn.executescript(archive.SCHEMA)
        with db.tx() as conn:
            # Historical import/rehearsal DBs may have only archive_facts. Build
            # the derived summary cache without touching their fact rows/identity.
            conn.execute('''INSERT OR REPLACE INTO archive_message_summaries
                SELECT device_id,kind,agent,native_id,fact_id,occurred_at,
                json_object('message_id',json_extract(payload_json,'$.message_id'),
                'role',COALESCE(json_extract(payload_json,'$.role'),''),
                'final',COALESCE(json_extract(payload_json,'$.final'),0),
                'preview',COALESCE(json_extract(payload_json,'$.preview'),''),
                'body_chars',length(COALESCE(json_extract(payload_json,'$.body'),'')),
                'source_offset',json_extract(payload_json,'$.source_offset'))
                FROM archive_facts WHERE kind='message' ''')
    with db.tx() as conn:
        for statement in _SCHEMA:
            conn.execute(statement)


def bind_source(db: Database, source_id: str, archive_device_id: str, *, evidence: str,
                namespace: str | None = None) -> None:
    """Declare proven raw-source/archive identity; fail rather than move facts.

    Default profiles retain native IDs. Other profiles use source UUID namespaces
    unless an audited historical namespace is explicitly supplied. Call this before
    projection; an incompatible rebind after projection is deliberately rejected.
    """
    if not evidence.strip():
        raise ValueError('Identity binding requires evidence')
    initialize(db)
    with db.tx() as conn:
        source = conn.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()
        if not source:
            raise ValueError('Unknown source')
        ns = namespace if namespace is not None else ('' if source['profile'] == 'default' else f'profile-{source_id}:')
        if '/' in ns:
            raise ValueError('Namespace must be URL path segment safe')
        old = conn.execute('SELECT * FROM raw_archive_sources WHERE source_id=?', (source_id,)).fetchone()
        if old and (old['archive_device_id'], old['namespace']) != (archive_device_id, ns):
            raise ValueError('Source already bound; migrate its projected facts explicitly')
        conn.execute('INSERT OR REPLACE INTO raw_archive_sources VALUES(?,?,?,?)',
                     (source_id, archive_device_id, ns, evidence))


def bind_device_alias(db: Database, alias_id: str, canonical_id: str, *, evidence: str) -> None:
    """Read-time alias for a *proven* historical archive copy; retain all originals."""
    if not evidence.strip() or alias_id == canonical_id:
        raise ValueError('Distinct devices and evidence required')
    initialize(db)
    with db.tx() as conn:
        # One-hop mappings only, so cycles/chains cannot silently change identity.
        if conn.execute('SELECT 1 FROM raw_archive_device_aliases WHERE alias_id=? OR canonical_id=?',
                        (canonical_id, alias_id)).fetchone():
            raise ValueError('Alias chains are not supported')
        old = conn.execute('SELECT canonical_id FROM raw_archive_device_aliases WHERE alias_id=?', (alias_id,)).fetchone()
        if old and old[0] != canonical_id:
            raise ValueError('Conflicting device identity')
        conn.execute('INSERT OR REPLACE INTO raw_archive_device_aliases VALUES(?,?,?)',
                     (alias_id, canonical_id, evidence))


def _mapping(conn, source):
    row = conn.execute('SELECT * FROM raw_archive_sources WHERE source_id=?', (source['id'],)).fetchone()
    if not row:
        ns = '' if source['profile'] == 'default' else f"profile-{source['id']}:"
        conn.execute('INSERT INTO raw_archive_sources VALUES(?,?,?,?)',
                     (source['id'], source['device_id'], ns, 'registered source.device_id; profile namespace v1'))
        row = conn.execute('SELECT * FROM raw_archive_sources WHERE source_id=?', (source['id'],)).fetchone()
    return row


def project_raw(db: Database, source_ids: list[str] | None = None) -> dict:
    """Backfill existing raw projections, atomically and idempotently.

    For historical snapshots, use an isolated Database with raw tables populated;
    this function never reads another SQLite file, discovers sources, or scans disk.
    """
    initialize(db)
    with db.tx() as conn:
        return project_raw_conn(conn, source_ids=source_ids)


def project_raw_conn(conn: sqlite3.Connection, *, source_ids=None, session_ids=None, after_rowid=None) -> dict:
    """Project before ACK; an optional transaction watermark limits hot refreshes.

    No watermark retains the complete historical backfill behavior. A session
    without a bridge session fact is backfilled once, never silently orphaning
    pre-existing raw facts when incremental ingestion first reaches it.
    """
    changes = None
    if after_rowid is not None:
        from .projector import _session_id
        changes = defaultdict(list)
        for row in conn.execute('SELECT event_id,content_json,received_at FROM events WHERE rowid>? ORDER BY rowid', (after_rowid,)):
            content = json.loads(row['content_json'])
            changes[_session_id(content['source_instance_id'], content['native_session_id'])].append((row['event_id'], content, row['received_at']))
        session_ids = sorted(changes) if session_ids is None else [s for s in session_ids if s in changes]
    for statement in _SCHEMA:
        conn.execute(statement)
    where, params = ['s.archived=0'], []
    if source_ids is not None:
        if not source_ids:
            return {'sessions': 0, 'changed_facts': 0}
        where.append('s.source_id IN (' + ','.join('?' for _ in source_ids) + ')')
        params.extend(source_ids)
    if session_ids is not None:
        if not session_ids:
            return {'sessions': 0, 'changed_facts': 0}
        where.append('s.id IN (' + ','.join('?' for _ in session_ids) + ')')
        params.extend(session_ids)
    sessions = conn.execute('SELECT s.* FROM sessions s WHERE ' + ' AND '.join(where), params).fetchall()
    changed = 0
    for session in sessions:
        source = conn.execute('SELECT * FROM sources WHERE id=?', (session['source_id'],)).fetchone()
        if not source or conn.execute('SELECT 1 FROM tombstones WHERE source_id=? AND native_session_id=?',
                                     (source['id'], session['native_id'])).fetchone():
            continue
        mapping = _mapping(conn, source)
        device = mapping['archive_device_id']
        native = mapping['namespace'] + session['native_id']
        agent = source['agent']
        dev = conn.execute('SELECT * FROM devices WHERE id=?', (source['device_id'],)).fetchone()
        previous = conn.execute("SELECT payload_json FROM archive_facts WHERE device_id=? AND agent=? AND native_id=? AND kind='session' AND fact_id=?",
                                (device, agent, native, f"raw-session:{source['id']}")).fetchone()
        prior_meta = json.loads(previous[0]) if previous else {}
        delta = changes[session['id']] if changes is not None and previous else None
        if delta is None:
            latest = conn.execute('SELECT max(received_at) FROM events WHERE source_id=? AND native_session_id=?',
                                  (source['id'], session['native_id'])).fetchone()[0]
        else:
            latest = max([r[2] for r in delta] + [prior_meta.get('observed_at') or ''])
        # A replay must not mutate last_seen/archive_log merely because time passed.
        stamp = latest or source['last_event'] or source['created_at']
        conn.execute('''INSERT INTO archive_devices(id,name,os,last_seen) VALUES(?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET last_seen=max(last_seen,excluded.last_seen)''',
                     (device, dev['name'] if dev else device, dev['os'] if dev else 'unknown', stamp))
        common = {'raw_archive_version': 1, 'source_id': source['id'], 'profile': source['profile'],
                  'collector_device_id': source['device_id'], 'native_session_id': session['native_id']}
        def put(kind, fact_id, at, value):
            nonlocal changed
            changed += archive._upsert_local(conn, device, kind, agent, native, fact_id, at, {**common, **value})
        existing_meta = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload_json FROM archive_facts WHERE device_id=? AND agent=? AND native_id=? AND kind='session'",
            (device, agent, native))]
        meta = next((v for v in existing_meta if not v.get('raw_archive_version')), {})
        def selected(alias, column, kind, payload_key=None):
            if delta is None:
                return '', []
            ids = sorted({c['payload'][payload_key] if payload_key else eid
                          for eid, c, _ in delta if c['fact_kind'] == kind})
            return (' AND ' + alias + '.' + column + ' IN (' + ','.join('?' for _ in ids) + ')', ids) if ids else (' AND 0', [])
        message_filter, message_params = selected('m', 'native_id', 'message.observed', 'native_message_id')
        messages = conn.execute('''SELECT m.*,e.received_at AS observed_at FROM messages m
            LEFT JOIN events e ON e.event_id=m.event_id WHERE m.session_id=?''' + message_filter + '''
            ORDER BY m.occurred_at,m.source_order,m.id''', [session['id'], *message_params]).fetchall()
        # Two index seeks retain exact bounds, even when a revision moves the
        # earliest/latest message inward. Never load old transcript bodies here.
        times = []
        for direction in ('ASC', 'DESC'):
            bound = conn.execute('SELECT occurred_at FROM messages WHERE session_id=? AND occurred_at IS NOT NULL ORDER BY occurred_at ' + direction + ' LIMIT 1', (session['id'],)).fetchone()
            if bound:
                times.append(bound[0])
        if session['created_at']:
            times.append(session['created_at'])
        if session['last_activity']:
            times.append(session['last_activity'])
        first = min(times) if times else None
        last = max(times) if times else None
        from .session_identity import HERMES_SIZE_FACT, observed_identity
        lineage = observed_identity(conn, source['id'], session['native_id'], agent)
        size_fields = {}
        if agent == 'hermes':
            size_event = conn.execute('''SELECT content_json FROM events WHERE source_id=? AND native_session_id=?
                AND fact_kind='session.observed' AND native_fact_id=?
                ORDER BY source_order DESC,rowid DESC LIMIT 1''',
                (source['id'], session['native_id'], HERMES_SIZE_FACT)).fetchone()
            if size_event:
                size = json.loads(size_event[0])['payload'].get('payload_bytes')
                if type(size) is int and size >= 0:
                    size_fields['payload_bytes'] = size
        put('session', f"raw-session:{source['id']}", last,
            {**meta, 'title': session['title'] or meta.get('title') or f'{agent} · {session["native_id"][:8]}',
             'cwd': session['cwd'] or meta.get('cwd'), 'sources': meta.get('sources', []),
             'first_at': first or meta.get('first_at'), 'last_at': last or meta.get('last_at'), 'observed_at': stamp,
             **lineage, **size_fields})
        for m in messages:
            # Projected raw messages already implement revision order/body policy.
            # Keep metadata-only observations metadata-only; do not turn previews into bodies.
            put('message', 'raw-message:' + source['id'] + ':' + m['native_id'], m['occurred_at'],
                {'message_id': m['native_id'], 'role': m['role'], 'body': m['body'],
                 'content_state': m['content_state'], 'omission_reason': m['omission_reason'],
                 'source_text_char_count': m['source_char_count'], 'preview': (m['body'] or '')[:160],
                 'final': m['finalized'], 'source_offset': m['source_order'], 'event_id': m['event_id'],
                 'turn_id': m['turn_id'], 'input_origin': m['input_origin'], 'observed_at': m['observed_at']})
        run_filter, run_params = selected('r', 'native_id', 'run.observed', 'native_turn_id')
        for r in conn.execute('SELECT r.* FROM runs r WHERE r.session_id=?' + run_filter, [session['id'], *run_params]):
            put('run', 'raw-run:' + source['id'] + ':' + r['native_id'], r['start_at'] or r['end_at'],
                {'run_id': r['native_id'], 'start_at': r['start_at'], 'end_at': r['end_at'],
                 'status': r['status'], 'duration_basis': r['duration_basis'], 'duration_ms': r['duration_ms'],
                 'parent_run_ref': r['parent_run_ref'], 'event_id': r['event_id'], 'model': r['model'],
                 'evidence': 'native_task_event' if r['duration_basis'] in {'source', 'start_end'} else 'unknown'})
        file_filter, file_params = selected('f', 'event_id', 'file.observed')
        for f in conn.execute('''SELECT f.*,e.occurred_at FROM file_events f
                JOIN events e USING(event_id) WHERE f.session_id=?''' + file_filter, [session['id'], *file_params]):
            put('file', 'raw-file:' + f['event_id'], f['occurred_at'],
                {'event_key': f['event_id'], 'native_path': f['native_path'], 'relation': f['relation'],
                 'operation_status': f['operation_status'], 'evidence': json.loads(f['evidence_json'])})
        # rowid is ingestion order, including multiple observations in one transaction.
        # Aggregate snapshots replace the same usage key; summing rescans is incorrect.
        latest_usage = {}
        usage_filter, usage_params = selected('u', 'event_id', 'usage.observed')
        if delta is not None and agent == 'hermes' and (first, last) != (prior_meta.get('first_at'), prior_meta.get('last_at')):
            # Legacy Hermes aggregates derive missing bounds from the session;
            # these facts depend on changed bounds even without new usage events.
            usage_filter = ''' AND ((u.semantics='aggregate' AND u.usage_key IN (
                SELECT json_extract(payload_json,'$.usage_key') FROM archive_facts
                WHERE device_id=? AND agent=? AND native_id=? AND kind='usage'
                  AND json_extract(payload_json,'$.source_id')=?
                  AND json_extract(payload_json,'$.time_basis')='session_bounds'))
                OR (1=1''' + usage_filter + '))'
            usage_params = [device, agent, native, source['id'], *usage_params]
        for u in conn.execute('''SELECT u.*,e.content_json,e.received_at,e.native_fact_id,e.rowid AS arrival
                FROM usage_observations u JOIN events e USING(event_id)
                WHERE u.session_id=?''' + usage_filter + ' ORDER BY e.rowid', [session['id'], *usage_params]):
            p = json.loads(u['content_json'])['payload']
            key = (u['semantics'], u['usage_key'] if u['semantics'] == 'aggregate' else u['native_fact_id'])
            latest_usage[key] = (u, p)
        for (semantics, identity), (u, p) in latest_usage.items():
            value = {**p, 'quantity_semantics': semantics, 'usage_key': u['usage_key'],
                     'event_id': u['event_id'], 'native_fact_id': u['native_fact_id'],
                     'source_order': u['source_order'], 'arrival': u['arrival'],
                     'observed_at': u['received_at'], 'model': u['model'] or 'unknown'}
            at = u['source_time'] or json.loads(u['content_json']).get('occurred_at')
            if agent == 'hermes' and semantics == 'aggregate':
                # Old collector versions omit native usage bounds and request counts.
                # Session bounds are conservative, explicitly not per-model timestamps.
                native_first_ms = utc_ms(p.get('first_seen'))
                native_last_ms = utc_ms(p.get('last_seen'))
                start_ms = native_first_ms if native_first_ms is not None else utc_ms(first)
                end_ms = native_last_ms if native_last_ms is not None else utc_ms(last)
                value.update(first_seen=native_first_ms / 1000 if native_first_ms is not None else None,
                             last_seen=native_last_ms / 1000 if native_last_ms is not None else None,
                             range_first_seen=start_ms / 1000 if start_ms is not None else None,
                             range_last_seen=end_ms / 1000 if end_ms is not None else None,
                             time_basis='native_usage_bounds' if native_first_ms is not None and native_last_ms is not None else 'session_bounds',
                             api_call_count=p.get('api_call_count'), cache_read_tokens=p.get('cache_read_tokens', p.get('cached_input_tokens')),
                             cache_write_tokens=p.get('cache_write_tokens'), precision='session_model_aggregate')
                if start_ms is None or end_ms is None:
                    value.update(precision='unallocated', omission_reason='missing_usage_bounds')
                at = iso_utc(value['range_last_seen'])
            put('usage', 'raw-usage:' + source['id'] + ':' + sha256([semantics, identity]), at, value)
    return {'sessions': len(sessions), 'changed_facts': changed}


def _read(db, kind, device_id, start_at, end_at, summaries, agent, native_id, session_keys=None):
    if kind != 'message' or summaries:
        from .query_model import model
        with model(db) as projection:
            return projection.read(kind, device_id, start_at, end_at, summaries, agent, native_id, session_keys)
    with db.read() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        aliases = {r[0]: r[1] for r in conn.execute('SELECT alias_id,canonical_id FROM raw_archive_device_aliases')} if 'raw_archive_device_aliases' in tables else {}
        condition, params = ['f.kind=?'], [kind]
        if device_id:
            canonical_device = aliases.get(device_id, device_id)
            ids = [canonical_device] + [key for key, value in aliases.items() if value == canonical_device]
            condition.append('f.device_id IN (' + ','.join('?' for _ in ids) + ')')
            params.extend(ids)
        for field, value, op in [('occurred_at', start_at, '>='), ('occurred_at', end_at, '<'),
                                 ('agent', agent, '='), ('native_id', native_id, '=')]:
            if value is not None:
                condition.append('f.' + field + op + '?')
                params.append(value)
        # Full payload reads remain for explicit content searches. List and
        # conversation page paths use compact projections instead.
        select = 'SELECT f.* FROM archive_facts f'
        result = []
        for row in conn.execute(select + ' WHERE ' + ' AND '.join(condition), params):
            payload = json.loads(row['payload_json'])
            result.append({**payload, 'device_id': aliases.get(row['device_id'], row['device_id']),
                           'agent': row['agent'], 'native_id': row['native_id'], 'fact_id': row['fact_id'],
                           'occurred_at': row['occurred_at'], '_origin_device_id': row['device_id']})
        return result


def _message_identity(row):
    # Different parsers use message IDs vs transcript offsets. Offset + native
    # session + role + exact timestamp is a documented cross-parser identity.
    offset = row.get('source_offset')
    return ('offset', offset, utc_ms(row.get('occurred_at')), row.get('role')) if offset not in (None, 0) and row['agent'] == 'codex' else ('id', str(row.get('message_id', row['fact_id'])))


def _dedupe(rows, kind):
    unique = {}
    for row in rows:
        session = (row['device_id'], row['agent'], row['native_id'])
        identity = (_message_identity(row) if kind == 'message' else
                    ('session',) if kind == 'session' else
                    (row.get('run_id', row['fact_id']),) if kind == 'run' else
                    (row.get('native_path'), row.get('relation'), utc_ms(row.get('occurred_at'))) if kind == 'file' else
                    (row['fact_id'],))
        key = (*session, *identity)
        old = unique.get(key)
        def rank(r):
            return (bool(r.get('body') or r.get('body_chars')),
                    utc_ms(r.get('last_seen')) or utc_ms(r.get('occurred_at')) or -1,
                    r.get('observed_at') or '', bool(r.get('raw_archive_version')),
                    r['_origin_device_id'] == r['device_id'])
        if old is None or rank(row) > rank(old):
            unique[key] = row
    return list(unique.values())


def _hermes_usage_relation(left, right):
    """Prove same/disjoint coverage, otherwise leave the evidence unreconciled.

    usage_key is the collector's opaque full billing identity. Older archives
    often have only model/task, which cannot prove the omitted provider/billing
    dimensions. Missing is NOT the same as an explicitly recorded empty value.
    """
    fields = ('model', 'task', 'provider', 'billing_base_url', 'billing_mode')
    if any(left.get(k) is not None and right.get(k) is not None
           and left[k] != right[k] for k in fields):
        return 'disjoint'
    if (left.get('usage_key') and left.get('usage_key') == right.get('usage_key')
            and left.get('coverage_scope', 'session_model') == 'session_model'
            and right.get('coverage_scope') == 'session_model'):
        return 'same'
    if all(left.get(k) is not None and right.get(k) is not None for k in fields):
        return 'same'
    return 'unknown'


def _hermes_replacement_reason(raw, previous):
    # Only native usage bounds prove complete coverage. Session ranges and
    # transport/occurred_at timestamps are presentation metadata, not watermarks.
    bounds = [utc_ms(r.get(k)) for r in (raw, previous) for k in ('first_seen', 'last_seen')]
    if any(value is None for value in bounds):
        return 'missing_native_usage_bounds'
    start, until, old_start, old_until = bounds
    if start > until or old_start > old_until:
        return 'invalid_native_usage_bounds'
    if until < old_until:
        return 'older_than_archived_aggregate'
    if start > old_start:
        return 'incomplete_native_usage_coverage'
    return None


def _usage(rows):
    """Reduce observations; never add cumulative snapshots or aggregate revisions."""
    groups = defaultdict(list)
    for row in rows:
        groups[(row['device_id'], row['agent'], row['native_id'])].append(row)
    result = []
    for _, session in groups.items():
        legacy = [r for r in session if not r.get('raw_archive_version')]
        raw = [r for r in session if r.get('raw_archive_version')]
        aggregate_keys = {}
        for r in raw:
            if r.get('quantity_semantics') != 'aggregate':
                continue
            identity = tuple(r.get(k) for k in
                             ('model', 'task', 'provider', 'billing_base_url', 'billing_mode'))
            # Full explicit identity takes precedence over parser-specific keys:
            # two keys for the same billing aggregate are not additive snapshots.
            key = (('hermes_identity', *identity) if r['agent'] == 'hermes'
                   and all(value is not None for value in identity) else
                   ('usage_key', r.get('usage_key'), *identity))
            old = aggregate_keys.get(key)
            rank = lambda x: (utc_ms(x.get('last_seen')) if x.get('last_seen') is not None else -1,
                              x.get('observed_at') or '', x.get('arrival') or 0)
            if old is None or rank(r) > rank(old):
                aggregate_keys[key] = r
        aggregates = list(aggregate_keys.values())
        replaced = set()
        retained_aggregates = []
        for row in aggregates:
            if row['agent'] != 'hermes':
                retained_aggregates.append(row)
                continue
            relations = [(i, old, _hermes_usage_relation(row, old))
                         for i, old in enumerate(legacy) if old['agent'] == 'hermes']
            matches = [(i, old) for i, old, relation in relations if relation == 'same']
            reason = row.get('omission_reason') if row.get('precision') == 'unallocated' else None
            if any(relation == 'unknown' for _, _, relation in relations):
                reason = reason or 'unproven_usage_identity'
            for _, old in matches:
                reason = reason or _hermes_replacement_reason(row, old)
            if reason or row.get('precision') == 'unallocated':
                retained_aggregates.append({**row, 'precision': 'unallocated',
                    'omission_reason': reason or 'missing_usage_bounds', 'additive': False})
            else:
                # New raw-only/disjoint identities are still displayable. A
                # matching identity supersedes ONLY its proven covered rows.
                replaced.update(i for i, _ in matches)
                retained_aggregates.append(row)
        result.extend(_dedupe([r for i, r in enumerate(legacy) if i not in replaced], 'usage'))
        result.extend(retained_aggregates)
        incremental = sorted((r for r in raw if r.get('quantity_semantics') == 'incremental'),
                             key=lambda r: (r.get('observed_at') or '', r.get('arrival') or 0), reverse=True)
        known = {(utc_ms(r.get('occurred_at')), r.get('model'), r.get('input_tokens'), r.get('output_tokens')) for r in legacy}
        seen_incremental = set()
        for r in incremental:
            identity = (utc_ms(r.get('source_time') or r.get('occurred_at')), r.get('model'), r.get('input_tokens'), r.get('output_tokens'))
            native_identity = (r.get('native_fact_id'), r.get('usage_key'))
            if identity in known or native_identity in seen_incremental:
                continue
            seen_incremental.add(native_identity)
            result.append({**r, 'request_id': r.get('native_fact_id', r['fact_id']),
                           'occurred_at': r.get('source_time') or r.get('occurred_at'),
                           'cached_input_tokens': r.get('cached_input_tokens') or 0,
                           'precision': 'request'})
        counters = defaultdict(list)
        for r in raw:
            if r.get('quantity_semantics') == 'cumulative_snapshot':
                counters[(r.get('counter_id'), r.get('epoch_id'), r.get('usage_key'), r.get('model'))].append(r)
        intervals = {}
        loose = []
        for counter, snapshots in counters.items():
            prior = None
            tainted = False
            for r in sorted(snapshots, key=lambda r: (r.get('source_order') or 0, r.get('source_time') or '', r.get('arrival') or 0)):
                amount = r.get('total_tokens')
                at = r.get('source_time') or r.get('occurred_at')
                if not isinstance(amount, int) or amount < 0:
                    continue
                if tainted:
                    loose.append({**r, 'precision': 'unallocated', 'omission_reason': 'counter_epoch_untrusted'})
                    continue
                if prior is None:
                    if r.get('verified_zero_origin') and r.get('origin_time'):
                        begin, delta = r['origin_time'], amount
                    else:
                        loose.append({**r, 'precision': 'unallocated', 'omission_reason': 'opening_balance'})
                        prior = r
                        continue
                else:
                    begin, delta = prior.get('source_time') or prior.get('occurred_at'), amount - prior['total_tokens']
                if delta < 0:
                    loose.append({**r, 'precision': 'unallocated', 'omission_reason': 'counter_regression'})
                    tainted = True
                    continue
                if utc_ms(begin) is None or utc_ms(at) is None or utc_ms(at) < utc_ms(begin):
                    loose.append({**r, 'precision': 'unallocated', 'omission_reason': 'undated_interval'})
                    prior = r
                    continue
                key = (counter[:2], r.get('source_order'), at, begin, counter[3])
                item = intervals.setdefault(key, {**r, 'input_tokens': 0, 'output_tokens': 0,
                    'cached_input_tokens': 0, 'precision': 'counter_interval', 'api_call_count': None,
                    'request_id': 'counter:' + sha256(key), 'interval_start': begin, 'occurred_at': at,
                    'counter_components': [], 'total_tokens': None})
                field = {'codex_input': 'input_tokens', 'codex_output': 'output_tokens',
                         'codex_cached_input': 'cached_input_tokens', 'codex_total': 'total_tokens'}.get(counter[2])
                if field:
                    item[field] = delta
                    item['counter_components'].append(field)
                prior = r
        exact_times = sorted(at for x in [*legacy, *incremental]
                             if (at := utc_ms(x.get('source_time') or x.get('occurred_at'))) is not None)
        seen_intervals = set()
        for r in intervals.values():
            native_interval = (r['interval_start'], r['occurred_at'], r.get('model'),
                               r['input_tokens'], r['output_tokens'], r['cached_input_tokens'])
            if native_interval in seen_intervals:
                continue
            seen_intervals.add(native_interval)
            # Exact archived requests in an interval supersede an overlapping
            # counter delta. Never present both collection paths as extra spend.
            if bisect_right(exact_times, utc_ms(r['occurred_at'])) > bisect_right(exact_times, utc_ms(r['interval_start'])):
                loose.append({**r, 'precision': 'unallocated', 'omission_reason': 'overlap_with_exact_requests'})
                continue
            if not {'input_tokens', 'output_tokens'} <= set(r['counter_components']):
                loose.append({**r, 'precision': 'unallocated', 'omission_reason': 'missing_counter_components'})
            elif r['input_tokens'] or r['output_tokens']:
                result.append(r)
        result.extend(loose)
    return result


def facts(db: Database, kind: str, device_id=None, start_at=None, end_at=None, *,
          summaries=False, agent=None, native_id=None, session_keys=None) -> list[dict]:
    """Read-only MVP query: explicit identity aliases, never hostname/UUID guessing."""
    rows = _read(db, kind, device_id, start_at, end_at, summaries, agent, native_id, session_keys)
    if kind == 'usage':
        result = _usage(rows)
        return _copied_usage(result) if device_id is None else result
    return _dedupe(rows, kind)


def _copied_usage(result):
    """Legacy copied-request policy, unchanged and applied before range filters."""
    seen, unique = set(), []
    for row in result:
        if row['agent'] == 'codex' and not row.get('raw_archive_version'):
            key = tuple(row.get(k) for k in ('agent', 'native_id', 'request_id', 'occurred_at',
                        'model', 'input_tokens', 'cached_input_tokens', 'output_tokens'))
            if key in seen:
                continue
            seen.add(key)
        unique.append(row)
    return unique


def usage_range(db, start, end, *, device_id=None, agent=None, native_id=None):
    """SQL range candidates from complete, reconciled historical observations."""
    from .usage_projection import enabled, usage_range as published_usage
    if enabled(db):
        return published_usage(db,start,end,device_id=device_id,agent=agent,native_id=native_id)
    from .query_model import model
    with model(db) as projection:
        return projection.usage(start, end, device_id, agent, native_id)


def message_page(db, agent, native_id, *, device_id, start_at=None, end_at=None,
                 start=None, end=None, view='full', offset=0, limit=100):
    """Resolve identity/turns with compact metadata, fetch only page bodies."""
    from .activity import _key_messages
    from .query_model import model
    from datetime import datetime
    with model(db) as projection:
        rows = _dedupe(projection.read('message', device_id, start_at, end_at,
                       False, agent, native_id), 'message')
        if start is not None:
            rows = [r for r in rows if start <= datetime.fromisoformat(r['occurred_at'].replace('Z','+00:00')) < end]
        rows.sort(key=lambda r: (r['occurred_at'], r.get('source_offset') or 0))
        if view == 'key':
            rows = _key_messages(rows, skip_boilerplate=agent == 'codex')
        return len(rows), projection.bodies(rows[offset:offset+limit])


def search_messages(db, device_id, start_at, end_at, needle, session_keys):
    """Unicode-exact substring search, retaining at most one full body at a time."""
    from .query_model import model
    with model(db) as projection:
        rows = _dedupe(projection.read('message', device_id, start_at, end_at,
                       False, session_keys=session_keys), 'message')
        for row in rows:
            full = projection.bodies([row])[0]
            body = full.get('body') or ''
            yield {**{k: row[k] for k in ('device_id','agent','native_id','occurred_at')},
                   'body_chars': full.get('body_chars', len(body)),
                   '_content_match': body[:160] if needle in body.casefold() else None}


def message_windows(db, device_id, low, high, range_low, range_high):
    """One compact read; retain each window's original independent dedupe."""
    from .query_model import model
    with model(db) as projection:
        rows = projection.read('message', device_id, low, high, True)
        strict = [r.copy() for r in rows if r['occurred_at'] is not None
                  and range_low <= r['occurred_at'] < range_high]
        return _dedupe(rows, 'message'), _dedupe(strict, 'message')
