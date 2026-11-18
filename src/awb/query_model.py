"""Disposable, demand-populated read projections over a read-only archive.

A source data_version change expires scope checks, not the whole fact mirror.
Reconcile only the requested kind/range/session; decode only changed payloads.
Usage alone needs complete historical normalization (counters/aliases/copies),
and remains lazy until a usage consumer asks for it. Source diagnostics retain
all-history coverage. Source snapshots span metadata and page body retrieval.
"""
from __future__ import annotations

from collections import OrderedDict, defaultdict
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from threading import RLock

_VERSION = 1
_POOL = OrderedDict()
_POOL_LOCK = RLock()
_MAX_MODELS = 3


def _cache_dir(db):
    configured = os.environ.get('AWB_QUERY_CACHE_DIR')
    if configured:
        return Path(configured)
    # The service's dedicated venv lives directly inside its runtime root.
    prefix = Path(sys.prefix)
    return (prefix.parent if prefix.name in {'venv', '.venv'} else db.path.parent) / 'cache' / 'queries'


class _Model:
    def __init__(self, db):
        self.path = db.path.resolve()
        stat = self.path.stat()
        self.identity = (stat.st_dev, stat.st_ino)
        directory = _cache_dir(db)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = hashlib.sha256(str(self.path).encode()).hexdigest()[:32]
        target = directory / f'archive-query-v{_VERSION}-{key}.sqlite3'
        # Cache payloads still contain private previews/metadata.
        fd = os.open(target, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.conn = sqlite3.connect(target, uri=True, timeout=60, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA busy_timeout=60000')
        self.conn.execute('PRAGMA temp_store=MEMORY')
        # Durability trade-off applies ONLY to the disposable main cache.
        self.conn.execute('PRAGMA main.journal_mode=WAL')
        self.conn.execute('PRAGMA main.synchronous=NORMAL')
        self.conn.execute('PRAGMA main.cache_size=-32768')
        self.conn.execute('ATTACH DATABASE ? AS source', (self.path.as_uri() + '?mode=ro',))
        self.conn.execute('PRAGMA source.cache_size=-16384')
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS facts(
              device_id TEXT NOT NULL,kind TEXT NOT NULL,agent TEXT NOT NULL,
              native_id TEXT NOT NULL,fact_id TEXT NOT NULL,occurred_at TEXT,
              checksum TEXT NOT NULL,payload_json TEXT NOT NULL,source_rowid INTEGER,
              PRIMARY KEY(device_id,kind,agent,native_id,fact_id));
            CREATE INDEX IF NOT EXISTS ix_query_time ON facts(kind,occurred_at,device_id,agent);
            CREATE INDEX IF NOT EXISTS ix_query_session ON facts(device_id,agent,native_id,kind);
            CREATE TABLE IF NOT EXISTS aliases(alias_id TEXT PRIMARY KEY,canonical_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT OR IGNORE INTO meta VALUES('generation','0');
            INSERT OR IGNORE INTO meta VALUES('usage_revision','0');
            CREATE TABLE IF NOT EXISTS usage_rows(
              scope TEXT NOT NULL,ordinal INTEGER NOT NULL,agent TEXT NOT NULL,native_id TEXT NOT NULL,
              point REAL,first_seen REAL,last_seen REAL,precision TEXT,model TEXT,payload_json TEXT NOT NULL,
              PRIMARY KEY(scope,ordinal));
            CREATE INDEX IF NOT EXISTS ix_query_usage_time ON usage_rows(scope,point);
            CREATE INDEX IF NOT EXISTS ix_query_usage_precision ON usage_rows(scope,precision);
            CREATE INDEX IF NOT EXISTS ix_query_usage_bounds ON usage_rows(scope,agent,first_seen,last_seen);
            CREATE TABLE IF NOT EXISTS usage_sources(scope TEXT PRIMARY KEY,payload_json TEXT NOT NULL);
        ''')
        self.lock = RLock()
        self.version = None  # data_version values are connection-local: never persist them.
        self.scopes = set()

    def synchronize(self):
        c = self.conn
        version = c.execute('PRAGMA source.data_version').fetchone()[0]
        if self.version == version:
            return
        tables = {r[0] for r in c.execute("SELECT name FROM source.sqlite_master WHERE type='table'")}
        aliases = list(c.execute('SELECT alias_id,canonical_id FROM source.raw_archive_device_aliases')) if 'raw_archive_device_aliases' in tables else []
        alias_values = sorted(tuple(r) for r in aliases)
        aliases_changed = alias_values != sorted(tuple(r) for r in c.execute('SELECT * FROM aliases'))
        if aliases_changed:
            c.execute('DELETE FROM aliases')
            c.executemany('INSERT INTO aliases VALUES(?,?)', alias_values)
            c.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='usage_revision'")
        # No archive_facts traversal here. An external import/delete may bypass
        # archive_log, so expire scope checks on EVERY commit, including restart.
        # Browser invalidation is intentionally conservative without a durable
        # source-side change journal; usage is invalidated only by actual deltas.
        self.scopes.clear()
        c.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='generation'")
        self.version = version

    def _refresh(self, where, params, session_keys=None):
        """Reconcile one demanded scope, including deletes and time/key moves.

        Compact cached rows outside the scope may remain stale, but cannot be
        returned until their own scope has been reconciled against this snapshot.
        Never use archive_log as a complete change stream: imports can bypass it.
        """
        scope = (' AND '.join(where), tuple(params), session_keys)
        if scope in self.scopes:
            return
        c = self.conn
        changed = c.total_changes
        condition = ' AND '.join(where)
        identity = '''f.device_id=q.device_id AND f.kind=q.kind AND f.agent=q.agent
            AND f.native_id=q.native_id AND f.fact_id=q.fact_id'''
        # Remove stale rows using their OLD scope before fetching the new source
        # scope. This catches a revision moving OUT of the requested date range.
        c.execute('''DELETE FROM facts WHERE rowid IN (
            SELECT q.rowid FROM facts q LEFT JOIN aliases a ON a.alias_id=q.device_id
            WHERE ''' + condition + ''' AND NOT EXISTS (
                SELECT 1 FROM source.archive_facts f WHERE ''' + identity + '''
                AND f.checksum=q.checksum AND f.occurred_at IS q.occurred_at
                AND f.rowid IS q.source_rowid))''', params)
        cursor = c.execute('''SELECT q.rowid AS source_rowid,q.*
            FROM source.archive_facts q LEFT JOIN aliases a ON a.alias_id=q.device_id
            LEFT JOIN facts cached ON cached.device_id=q.device_id AND cached.kind=q.kind
                AND cached.agent=q.agent AND cached.native_id=q.native_id AND cached.fact_id=q.fact_id
            WHERE ''' + condition + ''' AND (cached.checksum IS NULL OR cached.checksum<>q.checksum
                OR cached.occurred_at IS NOT q.occurred_at OR cached.source_rowid IS NOT q.rowid)''', params)
        for row in cursor:
            payload = json.loads(row['payload_json'])
            if row['kind'] == 'message':
                body = payload.pop('body', None)
                payload.pop('search_body', None)
                # Preserve the existing compact reader's dedupe/body semantics.
                payload['_query_body_chars'] = len((body or '').split('\x00', 1)[0])
                payload['_query_body_present'] = bool(body)
                payload['body'] = True if body else body
            c.execute('''INSERT OR REPLACE INTO facts VALUES(?,?,?,?,?,?,?,?,?)''',
                      (row['device_id'],row['kind'],row['agent'],row['native_id'],row['fact_id'],
                       row['occurred_at'],row['checksum'],json.dumps(payload,ensure_ascii=False),row['source_rowid']))
        if c.total_changes != changed and params[0] == 'usage':
            c.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='usage_revision'")
        # Bound watcher bookkeeping. Eviction only causes an extra checksum
        # reconciliation; it never marks an unchecked scope as ready.
        if len(self.scopes) >= 128:
            self.scopes.clear()
        self.scopes.add(scope)

    def read(self, kind, device_id=None, start_at=None, end_at=None, summaries=False,
             agent=None, native_id=None, session_keys=None):
        c = self.conn
        aliases = dict(c.execute('SELECT * FROM aliases').fetchall())
        where, params = ['q.kind=?'], [kind]
        if device_id:
            canonical = aliases.get(device_id,device_id)
            devices = [canonical] + [a for a,v in aliases.items() if v == canonical]
            where.append('q.device_id IN ('+','.join('?' for _ in devices)+')')
            params.extend(devices)
        for field,value,op in [('occurred_at',start_at,'>='),('occurred_at',end_at,'<'),
                               ('agent',agent,'='),('native_id',native_id,'=')]:
            if value is not None:
                where.append('q.'+field+op+'?')
                params.append(value)
        scope_keys = None
        if session_keys is not None:
            scope_keys = tuple(sorted(set(session_keys)))
            c.execute('CREATE TEMP TABLE IF NOT EXISTS requested_sessions(device_id,agent,native_id,PRIMARY KEY(device_id,agent,native_id))')
            c.execute('DELETE FROM requested_sessions')
            c.executemany('INSERT OR IGNORE INTO requested_sessions VALUES(?,?,?)', scope_keys)
            where.append('EXISTS(SELECT 1 FROM requested_sessions k WHERE k.device_id=COALESCE(a.canonical_id,q.device_id) AND k.agent=q.agent AND k.native_id=q.native_id)')
        self._refresh(where, params, scope_keys)
        result = []
        # Mirror the source reader's index order. It breaks equal-rank ties in
        # the existing reducer and keeps preview/match ordering deterministic.
        if device_id and agent is not None and native_id is not None:
            order = 'q.device_id,q.agent,q.native_id,q.kind,q.source_rowid'
        elif device_id and start_at is None and end_at is None:
            order = 'q.device_id,q.kind,q.agent,q.native_id,q.fact_id'
        else:
            order = 'q.occurred_at,q.device_id,q.agent,q.source_rowid'
        for row in c.execute('''SELECT q.*,a.canonical_id FROM facts q LEFT JOIN aliases a
            ON a.alias_id=q.device_id WHERE '''+' AND '.join(where)+' ORDER BY '+order,params):
            payload = json.loads(row['payload_json'])
            if kind == 'message' and summaries:
                payload = {'message_id':payload.get('message_id'), 'role':payload.get('role') or '',
                           'final':payload.get('final') or 0,'preview':payload.get('preview') or '',
                           'body_chars':payload['_query_body_chars'],'source_offset':payload.get('source_offset'),
                           **{k:payload.get(k) for k in ('raw_archive_version','content_state','source_id','profile','observed_at')}}
            result.append({**payload,'device_id':row['canonical_id'] or row['device_id'],
                           'agent':row['agent'],'native_id':row['native_id'],'fact_id':row['fact_id'],
                           'occurred_at':row['occurred_at'],'_origin_device_id':row['device_id']})
        return result

    def bodies(self, rows):
        result = []
        for row in rows:
            found = self.conn.execute('''SELECT payload_json FROM source.archive_facts
                WHERE device_id=? AND kind='message' AND agent=? AND native_id=? AND fact_id=?''',
                (row['_origin_device_id'],row['agent'],row['native_id'],row['fact_id'])).fetchone()
            result.append({**json.loads(found[0]),**{k:row[k] for k in
                ('device_id','agent','native_id','fact_id','occurred_at','_origin_device_id')}})
        return result

    def ensure_usage(self, include_unfiltered=False):
        c = self.conn
        # Counter epochs, replacement bounds and copied requests need ALL usage
        # history. Pay that cost only here, never on a message/day-browser read.
        # Reconcile checksums after a source commit BEFORE trusting usage_ready.
        self._refresh(['q.kind=?'], ['usage'])
        revision = c.execute("SELECT value FROM meta WHERE key='usage_revision'").fetchone()[0]
        prior = c.execute("SELECT value FROM meta WHERE key='usage_ready'").fetchone()
        ready = prior and prior[0] == revision
        unfiltered = c.execute("SELECT value FROM meta WHERE key='usage_unfiltered_ready'").fetchone()
        if ready and (not include_unfiltered or unfiltered and unfiltered[0] == revision):
            return
        from .raw_archive import _usage, _copied_usage
        from .archive_views import AGENTS, _hermes_bounds
        rows = self.read('usage')
        groups = defaultdict(list)
        for row in rows:
            groups[row['device_id']].append(row)
        # The all-device request-copy policy is deliberately different from
        # device-scoped views. Normalize both, never range-filter raw counters.
        scopes = [('*',_copied_usage(_usage(rows)))]
        scopes.extend(('device:'+device,_usage(sorted(group, key=lambda r:
            (r['_origin_device_id'],r['agent'],r['native_id'],r['fact_id']))))
            for device,group in groups.items())
        if ready:
            scopes = []
        else:
            c.execute('DELETE FROM usage_rows')
            c.execute('DELETE FROM usage_sources')
        if include_unfiltered:
            scopes.append(('unfiltered',_usage(rows)))
        for scope, normalized in scopes:
            info = {name:{'times':[],'counter_intervals':0,'unallocated_rows':0,
                'missing_cache_read':0,'missing_cache_write':0,'unknown_call_count_rows':0,
                'exact':False} for name in AGENTS}
            inserts = []
            for ordinal,row in enumerate(normalized):
                first = last = point = None
                name = row['agent']
                stat = info.get(name)
                if stat is not None:
                    stat['counter_intervals'] += row.get('precision') == 'counter_interval'
                    stat['unallocated_rows'] += row.get('precision') == 'unallocated'
                    stat['unknown_call_count_rows'] += bool(row.get('raw_archive_version') and row.get('api_call_count') is None)
                    stat['exact'] |= row.get('precision') not in {'counter_interval','unallocated'}
                    if name == 'hermes':
                        stat['missing_cache_read'] += row.get('cache_read_tokens') is None
                        stat['missing_cache_write'] += row.get('cache_write_tokens') is None
                try:
                    if name == 'hermes':
                        bounds = _hermes_bounds(row)
                        if bounds is not None:
                            begin,at = bounds
                            first,last,point = begin.timestamp(),at.timestamp(),at.timestamp()
                            if stat is not None:
                                stat['times'].extend((begin.isoformat(),at.isoformat()))
                    elif row.get('occurred_at'):
                        # Coverage strings keep their original spelling/order.
                        if stat is not None:
                            stat['times'].append(row['occurred_at'])
                        from datetime import datetime
                        point = datetime.fromisoformat(row['occurred_at'].replace('Z','+00:00')).timestamp()
                except (ValueError,TypeError,KeyError,OverflowError):
                    pass
                inserts.append((scope,ordinal,name,row['native_id'],point,first,last,
                                row.get('precision'),row.get('model'),json.dumps(row,ensure_ascii=False)))
            c.executemany('INSERT INTO usage_rows VALUES(?,?,?,?,?,?,?,?,?,?)',inserts)
            for stat in info.values():
                times = stat.pop('times')
                stat.update(earliest=min(times) if times else None,latest=max(times) if times else None,
                            status='archived' if times else 'source_missing')
            c.execute('INSERT INTO usage_sources VALUES(?,?)',(scope,json.dumps(info)))
        c.execute("INSERT OR REPLACE INTO meta VALUES('usage_ready',?)",(revision,))
        if include_unfiltered:
            c.execute("INSERT OR REPLACE INTO meta VALUES('usage_unfiltered_ready',?)",(revision,))

    def usage(self, start, end, device_id=None, agent=None, native_id=None):
        self.ensure_usage(include_unfiltered=device_id == '')
        canonical = dict(self.conn.execute('SELECT * FROM aliases')).get(device_id,device_id)
        scope = '*' if device_id is None else 'unfiltered' if device_id == '' else 'device:'+canonical
        where, params = [], []
        for field,value in [('agent',agent),('native_id',native_id)]:
            if value is not None:
                where.append(field+'=?')
                params.append(value)
        # Separate indexed branches avoid a full scope scan for a narrow date.
        # Keep both the legacy containment rule and overlap rule: inverted old
        # bounds are not silently repaired/dropped by a performance refactor.
        sql = '''WITH candidates AS (
            SELECT ordinal,agent,native_id,payload_json FROM usage_rows
                WHERE scope=? AND precision='unallocated'
            UNION SELECT ordinal,agent,native_id,payload_json FROM usage_rows
                WHERE scope=? AND agent='hermes' AND first_seen<? AND last_seen>=?
            UNION SELECT ordinal,agent,native_id,payload_json FROM usage_rows
                WHERE scope=? AND agent='hermes' AND first_seen>=? AND last_seen<?
            UNION SELECT ordinal,agent,native_id,payload_json FROM usage_rows
                WHERE scope=? AND agent<>'hermes' AND point>=? AND point<?)
            SELECT payload_json FROM candidates'''
        if where:
            sql += ' WHERE '+' AND '.join(where)
        sql += ' ORDER BY ordinal'
        bounds = [scope,scope,end.timestamp(),start.timestamp(),
                  scope,start.timestamp(),end.timestamp(),scope,start.timestamp(),end.timestamp()]
        rows = [json.loads(r[0]) for r in self.conn.execute(sql, bounds+params)]
        found = self.conn.execute('SELECT payload_json FROM usage_sources WHERE scope=?',(scope,)).fetchone()
        return rows, json.loads(found[0]) if found else None


@contextmanager
def model(db):
    key = str(db.path.resolve())
    # A reentrant process lock protects the bounded connection pool and allows
    # a browser projection to reuse the SAME source snapshot in nested readers.
    # SQLite's cache write transaction already serializes refreshes cross-process.
    with _POOL_LOCK:
        current = _POOL.get(key)
        stat = db.path.stat()
        if current and current.identity != (stat.st_dev,stat.st_ino):
            current.conn.close()
            del _POOL[key]
            current = None
        if current is None:
            current = _Model(db)
            _POOL[key] = current
        _POOL.move_to_end(key)
        for old_key in list(_POOL):
            if len(_POOL) <= _MAX_MODELS:
                break
            old = _POOL[old_key]
            if old is not current and not old.conn.in_transaction:
                old.conn.close()
                del _POOL[old_key]
        if current.conn.in_transaction:
            yield current
            return
        # Only the disposable cache is writable; source is attached mode=ro.
        current.conn.execute('BEGIN IMMEDIATE')
        try:
            current.synchronize()
            yield current
            current.conn.execute('COMMIT')
        except BaseException:
            if current.conn.in_transaction:
                current.conn.execute('ROLLBACK')
            current.version = None
            raise


def close(db):
    """Release a watcher (tests/shutdown); cached projections remain disposable."""
    with _POOL_LOCK:
        current = _POOL.pop(str(db.path.resolve()),None)
        if current:
            with current.lock:
                current.conn.close()
