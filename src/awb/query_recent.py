"""Default recent browser: SQL keys/count/order first, page-only Python activity.

No persistent mirror or whole-range browser payload cache is built. SQL scans
compact message summaries in the requested +/-6h window and small session/run
facts. Duplicate-message rank alone may seek the original JSON observation.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3


from .activity import MAX_ESTIMATE_SECONDS, _boilerplate, _dt, trusted_run
from .codec import utc_ms

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _us(value):
    delta = _dt(value) - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds


def _identity(agent, offset, at, role, message_id):
    if agent == 'codex' and offset not in (None, 0):
        if isinstance(offset, float) and offset.is_integer():
            offset = int(offset)
        return json.dumps(('offset', offset, utc_ms(at), role), ensure_ascii=False)
    return json.dumps(('id', str(message_id)), ensure_ascii=False)


def _dedup_messages(c, name, bounds, strict=False):
    condition = 'WHERE occurred_at>=:low_key AND occurred_at<:high_key' if strict else ''
    # Original reducer keeps the FIRST insertion position even when a later
    # observation wins. Timestamp strings, not normalized datetimes, break ties.
    c.execute(f'''CREATE TEMP TABLE {name} AS
        WITH ranked AS (
          SELECT *,min(ord) OVER (PARTITION BY device_id,agent,native_id,identity) AS first_ord,
            row_number() OVER (PARTITION BY device_id,agent,native_id,identity ORDER BY
              body_chars<>0 DESC,coalesce(ms,-1) DESC,
              coalesce(json_extract(aux,'$.observed'),'') DESC,
              b_bool(json_extract(aux,'$.raw')) DESC,origin=device_id DESC,ord) AS winner
          FROM b_messages {condition})
        SELECT * FROM ranked WHERE winner=1''', bounds)


def _prepare(c, low, high, device_id):
    from .archive_views import _utc_key
    c.create_function('b_us', 1, _us, deterministic=True)
    c.create_function('b_ms', 1, utc_ms, deterministic=True)
    c.create_function('b_bool', 1, lambda v: int(bool(v)), deterministic=True)
    c.create_function('b_identity', 5, _identity, deterministic=True)
    c.create_function('b_skip', 1, lambda v: int(_boilerplate(v or '')), deterministic=True)
    c.create_function('b_trusted', 1, lambda v: int(trusted_run({'run_id': v or ''})), deterministic=True)
    c.create_function('b_until', 1, lambda v: min(_dt(v), high).isoformat(), deterministic=True)
    aliases = (dict(c.execute('SELECT alias_id,canonical_id FROM raw_archive_device_aliases'))
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='raw_archive_device_aliases' AND type='table'").fetchone() else {})
    canonical = aliases.get(device_id, device_id)
    c.execute('CREATE TEMP TABLE b_alias(alias_id PRIMARY KEY,canonical_id)')
    c.executemany('INSERT INTO b_alias VALUES(?,?)', aliases.items())
    bounds = {'low_key': _utc_key(low), 'high_key': _utc_key(high),
              'expanded_low': _utc_key(low-timedelta(seconds=MAX_ESTIMATE_SECONDS+1)),
              'expanded_high': _utc_key(high+timedelta(seconds=MAX_ESTIMATE_SECONDS+1)),
              'lo': _us(low.isoformat()), 'hi': _us(high.isoformat()), 'device': canonical,
              'scoped': bool(device_id)}
    # The PK lookup is covering: only source rowid, never message payload, is
    # needed to reproduce the source-index tie order.
    c.execute('''CREATE TEMP TABLE b_messages AS
      WITH base AS (
        SELECT s.device_id AS origin,coalesce(a.canonical_id,s.device_id) AS device_id,
          s.agent,s.native_id,s.fact_id,s.occurred_at,s.payload_json,
          row_number() OVER (ORDER BY s.occurred_at,s.device_id,s.agent,f.rowid) AS ord,
          b_identity(s.agent,json_extract(s.payload_json,'$.source_offset'),s.occurred_at,
            json_extract(s.payload_json,'$.role'),json_extract(s.payload_json,'$.message_id')) AS identity,
          json_extract(s.payload_json,'$.body_chars') AS body_chars,
          b_ms(s.occurred_at) AS ms,b_us(s.occurred_at) AS ts
        FROM archive_message_summaries s LEFT JOIN b_alias a ON a.alias_id=s.device_id
        CROSS JOIN archive_facts f ON f.device_id=s.device_id AND f.kind='message'
          AND f.agent=s.agent AND f.native_id=s.native_id AND f.fact_id=s.fact_id
        WHERE s.occurred_at>=:expanded_low AND s.occurred_at<:expanded_high
          AND (NOT :scoped OR coalesce(a.canonical_id,s.device_id)=:device)),
      copies AS (SELECT *,count(*) OVER (PARTITION BY device_id,agent,native_id,identity) AS n FROM base)
      SELECT *,CASE WHEN n>1 THEN (SELECT json_object(
          'observed',json_extract(f.payload_json,'$.observed_at'),
          'raw',json_extract(f.payload_json,'$.raw_archive_version'))
        FROM archive_facts f WHERE f.device_id=origin AND f.kind='message'
          AND f.agent=copies.agent AND f.native_id=copies.native_id AND f.fact_id=copies.fact_id)
        ELSE '{}' END AS aux FROM copies''', bounds)
    _dedup_messages(c, 'b_dedup', bounds)
    c.execute('CREATE INDEX b_message_session ON b_dedup(device_id,agent,native_id)')
    c.execute('CREATE INDEX b_raw_message_session ON b_messages(device_id,agent,native_id)')
    c.execute('''CREATE TEMP TABLE b_message_keys AS
        SELECT device_id,agent,native_id,max(occurred_at) AS last_at,0 AS category,
          0 AS subcategory,min(first_ord) AS position,0 AS seq FROM b_dedup
        WHERE ts>=:lo AND ts<:hi GROUP BY device_id,agent,native_id''', bounds)
    c.execute('CREATE UNIQUE INDEX b_message_key ON b_message_keys(device_id,agent,native_id)')
    # Run/session metadata is small; reduction stays in SQLite. Even an invalid
    # or out-of-range observation must participate BEFORE overlap filtering.
    for kind in ('session', 'run'):
        order = ('f.device_id,f.kind,f.agent,f.native_id,f.fact_id' if device_id else
                 'f.occurred_at,f.device_id,f.agent,f.rowid')
        identity = "'session'" if kind == 'session' else "CASE WHEN json_type(f.payload_json,'$.run_id') IS NULL THEN f.fact_id ELSE json_extract(f.payload_json,'$.run_id') END"
        sql = f'''CREATE TEMP TABLE b_{kind} AS
          WITH base AS (
            SELECT f.device_id AS origin,coalesce(a.canonical_id,f.device_id) AS device_id,
              f.agent,f.native_id,f.fact_id,f.occurred_at,f.payload_json,{identity} AS identity,
              row_number() OVER (ORDER BY {order}) AS ord
            FROM archive_facts f LEFT JOIN b_alias a ON a.alias_id=f.device_id
            WHERE f.kind=:kind AND (NOT :scoped OR coalesce(a.canonical_id,f.device_id)=:device)),
          ranked AS (
            SELECT *,min(ord) OVER (PARTITION BY device_id,agent,native_id,identity) AS first_ord,
              row_number() OVER (PARTITION BY device_id,agent,native_id,identity ORDER BY
                (b_bool(json_extract(payload_json,'$.body')) OR b_bool(json_extract(payload_json,'$.body_chars'))) DESC,
                coalesce(nullif(b_ms(json_extract(payload_json,'$.last_seen')),0),
                         nullif(b_ms(occurred_at),0),-1) DESC,
                coalesce(json_extract(payload_json,'$.observed_at'),'') DESC,
                b_bool(json_extract(payload_json,'$.raw_archive_version')) DESC,
                origin=device_id DESC,ord) AS winner FROM base)
          SELECT * FROM ranked WHERE winner=1'''
        c.execute(sql, {**bounds, 'kind': kind})
    c.execute('''CREATE TEMP TABLE b_intervals AS
      SELECT device_id,agent,native_id,json_extract(payload_json,'$.start_at') AS start_at,
        json_extract(payload_json,'$.end_at') AS end_at,0 AS category,first_ord AS group_ord,0 AS seq
      FROM b_run WHERE b_trusted(json_extract(payload_json,'$.run_id'))
        AND b_bool(json_extract(payload_json,'$.start_at'))
        AND b_bool(json_extract(payload_json,'$.end_at'))
        AND json_extract(payload_json,'$.end_at')>json_extract(payload_json,'$.start_at')
        AND NOT b_bool(json_extract(payload_json,'$.parent_run_ref'))
        AND (NOT b_bool(json_extract(payload_json,'$.raw_archive_version')) OR
          (json_extract(payload_json,'$.status') IN ('completed','failed','cancelled') AND
           json_extract(payload_json,'$.duration_basis') IN ('source','start_end')))''')
    # SQL equivalent of _estimated: latest non-boilerplate user resets pending;
    # first final closes it, otherwise last assistant closes an unfinished turn.
    # Codex suppression intentionally matches the legacy cross-agent identity set.
    c.execute('''INSERT INTO b_intervals
      WITH eligible AS (
        SELECT *,json_extract(payload_json,'$.role') AS role,
          b_bool(json_extract(payload_json,'$.final')) AS final
        FROM b_dedup m WHERE agent IN ('codex','claude','hermes')
          AND NOT EXISTS (SELECT 1 FROM b_message_keys k WHERE k.device_id=m.device_id
              AND k.agent=m.agent AND k.native_id=m.native_id)
          AND NOT (agent='codex' AND b_skip(json_extract(payload_json,'$.preview'))
                   AND json_extract(payload_json,'$.role')='user')
          AND NOT (agent='codex' AND EXISTS (SELECT 1 FROM b_intervals i
              WHERE i.device_id=m.device_id AND i.native_id=m.native_id))),
      numbered AS (
        SELECT *,row_number() OVER (PARTITION BY device_id,agent,native_id ORDER BY occurred_at,first_ord) AS seq,
          (SELECT min(d.first_ord) FROM b_dedup d WHERE d.device_id=eligible.device_id
             AND d.agent=eligible.agent AND d.native_id=eligible.native_id) AS group_ord,
          sum(role='user') OVER (PARTITION BY device_id,agent,native_id ORDER BY occurred_at,first_ord
            ROWS UNBOUNDED PRECEDING) AS turn FROM eligible),
      turns AS (
        SELECT device_id,agent,native_id,turn,group_ord,
          max(CASE WHEN role='user' THEN occurred_at END) AS start_at,
          coalesce(min(CASE WHEN role<>'user' AND final THEN seq END),
                   max(CASE WHEN role<>'user' THEN seq END)) AS end_seq
        FROM numbered WHERE turn>0 GROUP BY device_id,agent,native_id,turn),
      spans AS (
        SELECT t.*,m.occurred_at AS end_at FROM turns t JOIN numbered m
          ON m.device_id=t.device_id AND m.agent=t.agent AND m.native_id=t.native_id AND m.seq=t.end_seq)
      SELECT device_id,agent,native_id,start_at,end_at,
        CASE agent WHEN 'codex' THEN 1 WHEN 'claude' THEN 2 ELSE 3 END,group_ord,end_seq
      FROM spans WHERE b_us(end_at)>b_us(start_at)
        AND b_us(end_at)-b_us(start_at)<=:estimate_us''', {'estimate_us': MAX_ESTIMATE_SECONDS*1000000})
    c.execute('''CREATE TEMP TABLE b_candidates AS
      WITH message_keys AS (SELECT * FROM b_message_keys),
      overlapping AS (
        SELECT *,row_number() OVER (PARTITION BY device_id,agent,native_id
          ORDER BY category,group_ord,seq) AS chosen FROM b_intervals
        WHERE b_us(start_at)<:hi AND b_us(end_at)>:lo),
      run_keys AS (
        SELECT i.device_id,i.agent,i.native_id,b_until(i.end_at) AS last_at,
          1 AS category,i.category AS subcategory,i.group_ord AS position,i.seq
        FROM overlapping i WHERE chosen=1 AND NOT EXISTS (
          SELECT 1 FROM message_keys m WHERE m.device_id=i.device_id AND m.agent=i.agent AND m.native_id=i.native_id)),
      active_keys AS (SELECT * FROM message_keys UNION ALL SELECT * FROM run_keys)
      SELECT * FROM active_keys
      UNION ALL SELECT s.device_id,s.agent,s.native_id,json_extract(s.payload_json,'$.last_at'),
        2,0,s.first_ord,0 FROM b_session s
      WHERE b_bool(json_extract(s.payload_json,'$.first_at')) AND b_bool(json_extract(s.payload_json,'$.last_at'))
        AND b_us(json_extract(s.payload_json,'$.first_at'))<:hi
        AND b_us(json_extract(s.payload_json,'$.last_at'))>=:lo
        AND NOT EXISTS (SELECT 1 FROM active_keys k WHERE k.device_id=s.device_id
          AND k.agent=s.agent AND k.native_id=s.native_id)''', bounds)
    return bounds


def browser_recent(db, day, through, tz, *, agent, device_id, query, limit, offset=0):
    from .archive_views import AGENTS, _browser_uncached
    from .multi_usage import _range
    from .raw_archive import _dedupe
    _, _, low, high, _ = _range(day, through, tz)
    # Source is read-only. All temporary structures die with this connection;
    # count, keys and page detail share one consistent SQLite read snapshot.
    with closing(sqlite3.connect(db.path.resolve().as_uri()+'?mode=ro', uri=True, timeout=30)) as c:
        c.row_factory = sqlite3.Row

        c.execute('PRAGMA temp_store=MEMORY')
        c.execute('PRAGMA cache_size=-16384')
        c.execute('BEGIN')
        try:
            bounds = _prepare(c, low, high, device_id)
            condition, params = ('agent=?', [agent]) if agent is not None else ('1', [])
            counts = dict.fromkeys(AGENTS, 0)
            total = 0
            for r in c.execute('SELECT agent,count(*) FROM b_candidates WHERE '+condition+' GROUP BY agent',params):
                total += r[1]
                if r[0] in counts:
                    counts[r[0]] = r[1]
            keys = [tuple(r) for r in c.execute('SELECT device_id,agent,native_id FROM b_candidates WHERE '+condition+
                ' ORDER BY last_at DESC,category,subcategory,position,seq LIMIT ? OFFSET ?',params+[limit,offset])]
            c.execute('CREATE TEMP TABLE b_page(device_id,agent,native_id,PRIMARY KEY(device_id,agent,native_id))')
            c.executemany('INSERT INTO b_page VALUES(?,?,?)', keys)
            selected = set(keys)
            def read(_db, kind, _device, start_at=None, end_at=None, **kw):
                if kind == 'message':
                    rows = c.execute('''SELECT m.* FROM b_dedup m JOIN b_page p USING(device_id,agent,native_id)
                        ORDER BY m.first_ord''')
                elif kind in ('run','session'):
                    # Runs of other agents with the same identity still determine
                    # the legacy Codex verified-set suppression.
                    join = ('EXISTS(SELECT 1 FROM b_page p WHERE p.device_id=m.device_id AND p.native_id=m.native_id)'
                            if kind == 'run' else 'EXISTS(SELECT 1 FROM b_page p WHERE p.device_id=m.device_id AND p.agent=m.agent AND p.native_id=m.native_id)')
                    rows = c.execute(f'SELECT m.* FROM b_{kind} m WHERE '+join+' ORDER BY m.first_ord')
                else:
                    # Session index seeks, not an all-history file JSON scan.
                    result = []
                    for device,name,native in keys:
                        origins = [device]+[a[0] for a in c.execute('SELECT alias_id FROM b_alias WHERE canonical_id=?',(device,))]
                        for origin in origins:
                            for r in c.execute("SELECT rowid,* FROM archive_facts WHERE device_id=? AND agent=? AND native_id=? AND kind='file'",(origin,name,native)):
                                result.append({**json.loads(r['payload_json']),'device_id':device,'agent':name,
                                    'native_id':native,'fact_id':r['fact_id'],'occurred_at':r['occurred_at'],
                                    '_origin_device_id':origin,'_source_rowid':r['rowid']})
                    result.sort(key=(lambda r:(r['_origin_device_id'],r['agent'],r['native_id'],r['fact_id'])) if device_id else
                                (lambda r:(r['occurred_at'] or '',r['_origin_device_id'],r['agent'],r['_source_rowid'])))
                    return _dedupe(result,kind)
                return [{**json.loads(r['payload_json']),'device_id':r['device_id'],'agent':r['agent'],
                    'native_id':r['native_id'],'fact_id':r['fact_id'],'occurred_at':r['occurred_at'],
                    '_origin_device_id':r['origin']} for r in rows]
            def windows():
                expanded = read(db,'message',device_id)
                # Original strict-window reducer is INDEPENDENT of expanded
                # dedupe; a body-bearing revision just outside cannot erase text.
                _dedup_messages(c, 'b_strict', bounds, strict=True)
                strict = [{**json.loads(r['payload_json']),'device_id':r['device_id'],'agent':r['agent'],
                    'native_id':r['native_id'],'fact_id':r['fact_id'],'occurred_at':r['occurred_at']}
                    for r in c.execute('SELECT m.* FROM b_strict m JOIN b_page p USING(device_id,agent,native_id) ORDER BY first_ord')]
                return expanded,strict
            result = _browser_uncached(db,day,through,tz,agent=agent,device_id=device_id,
                query=query,limit=limit,_session_keys=selected,_reader=read,_windows=windows)
            by_key = {(r['device_id'],r['agent'],r['native_id']):r for r in result['sessions']}
            if set(by_key) != selected:
                raise RuntimeError('SQL recent candidate/detail mismatch')
            result.update(counts=counts,session_count=total,sessions=[by_key[k] for k in keys])
            return result
        finally:
            c.execute('ROLLBACK')
