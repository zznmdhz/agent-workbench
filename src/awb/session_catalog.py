"""Incremental, rebuildable session-list projections. No request replays history.

Source facts/receipts are untouched. Transactional triggers journal every relevant
write (including raw imports). A single bounded background writer reuses the
canonical Python reducer over compact metadata; readers use their own snapshots.
"""
from __future__ import annotations
from collections import defaultdict
from contextlib import closing, contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from threading import Event, Lock, Thread
import time

from .query_model import _cache_dir

_VERSION = 1
_MAX_SCOPES = 4
_REGISTRY = {}
_REGISTRY_LOCK = Lock()


def install(db):
    """Add derivative journal/indexes only; never update original fact values."""
    from .time_evidence import ensure
    with db.tx() as c:
        ensure(c)
        c.execute('''CREATE TABLE IF NOT EXISTS recent_catalog_changes(
          seq INTEGER PRIMARY KEY AUTOINCREMENT,device_id TEXT NOT NULL,
          agent TEXT NOT NULL,native_id TEXT NOT NULL)''')
        c.execute('''CREATE INDEX IF NOT EXISTS ix_catalog_message_fact
          ON archive_message_summaries(fact_id,device_id,agent,native_id)''')
        for action, refs in [('INSERT', ['NEW']), ('UPDATE', ['OLD', 'NEW']), ('DELETE', ['OLD'])]:
            statements = ''.join(f"INSERT INTO recent_catalog_changes(device_id,agent,native_id) SELECT {r}.device_id,{r}.agent,{r}.native_id WHERE {r}.kind IN ('message','session','run','file');" for r in refs)
            c.execute(f'CREATE TRIGGER IF NOT EXISTS catalog_fact_{action.lower()} AFTER {action} ON archive_facts BEGIN {statements} END')
            c.execute(f'''CREATE TRIGGER IF NOT EXISTS catalog_alias_{action.lower()} AFTER {action} ON raw_archive_device_aliases
              BEGIN INSERT INTO recent_catalog_changes(device_id,agent,native_id) VALUES('*','*','*'); END''')
        # Boundary-only corrections may leave the bridge's old `final` unchanged.
        for action in ('INSERT', 'UPDATE'):
            condition = "WHEN OLD.turn_final IS NOT NEW.turn_final OR OLD.basis<>NEW.basis" if action == 'UPDATE' else ''
            c.execute(f'''CREATE TRIGGER IF NOT EXISTS catalog_boundary_{action.lower()} AFTER {action} ON message_turn_flags {condition}
              BEGIN INSERT INTO recent_catalog_changes(device_id,agent,native_id)
                SELECT device_id,agent,native_id FROM archive_message_summaries
                WHERE fact_id='raw-message:'||NEW.source_id||':'||NEW.native_message_id; END''')


def _connect(path, readonly=False):
    c = sqlite3.connect(path.resolve().as_uri()+'?mode=ro' if readonly else str(path), uri=readonly,
                        timeout=5, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA cache_size=-16384')
    c.execute('PRAGMA foreign_keys=ON')
    return c


class _CompactReader:
    """Bounded source-index seeks, no transcript bodies in Python."""
    def __init__(self, db, conn, keys, aliases):
        self.db, self.conn, self.keys, self.aliases = db, conn, set(keys), aliases
        self.loaded = {}

    def _load(self, kind):
        from .raw_archive import _message_identity
        rows = []
        # Other-agent runs with the same identity retain Codex suppression rules.
        targets = set(self.keys)
        if kind == 'run':
            targets |= {(d, a, n) for d, _, n in self.keys for a in ('codex', 'claude', 'hermes')}
        for device, agent, native in sorted(targets):
            origins = [device]+[a for a, canonical in self.aliases.items() if canonical == device]
            for origin in origins:
                if kind == 'message':
                    cursor = self.conn.execute('''SELECT s.*,f.rowid AS source_rowid
                      FROM archive_message_summaries s CROSS JOIN archive_facts f
                      ON f.device_id=s.device_id AND f.agent=s.agent AND f.native_id=s.native_id
                        AND f.kind='message' AND f.fact_id=s.fact_id
                      WHERE s.device_id=? AND s.agent=? AND s.native_id=?''', (origin, agent, native))
                else:
                    cursor = self.conn.execute('''SELECT rowid AS source_rowid,* FROM archive_facts
                      WHERE device_id=? AND agent=? AND native_id=? AND kind=?''', (origin, agent, native, kind))
                for r in cursor:
                    p = json.loads(r['payload_json'])
                    rows.append({**p, 'device_id': device, 'agent': agent, 'native_id': native,
                                 'fact_id': r['fact_id'], 'occurred_at': r['occurred_at'],
                                 '_origin_device_id': origin, '_source_rowid': r['source_rowid']})
        if kind == 'message':
            groups = defaultdict(list)
            for r in rows:
                groups[(r['device_id'],r['agent'],r['native_id'],_message_identity(r))].append(r)
            for group in groups.values():
                if len(group) > 1:
                    for r in group:
                        aux = self.conn.execute('''SELECT json_extract(payload_json,'$.observed_at'),
                          json_extract(payload_json,'$.raw_archive_version') FROM archive_facts
                          WHERE device_id=? AND kind='message' AND agent=? AND native_id=? AND fact_id=?''',
                          (r['_origin_device_id'],r['agent'],r['native_id'],r['fact_id'])).fetchone()
                        if aux:r.update(observed_at=aux[0],raw_archive_version=aux[1])
        self.loaded[kind] = rows
        return rows

    def read(self, _db, kind, device_id, start_at=None, end_at=None, **kw):
        from .raw_archive import _dedupe
        rows = [dict(r) for r in self.loaded.get(kind, [])] if kind in self.loaded else [dict(r) for r in self._load(kind)]
        rows = [r for r in rows if (start_at is None or (r['occurred_at'] or '')>=start_at)
                and (end_at is None or (r['occurred_at'] or '')<end_at)]
        if device_id and start_at is None and end_at is None:
            order = lambda r:(r['_origin_device_id'],kind,r['agent'],r['native_id'],r['fact_id'])
        else:
            order = lambda r:(r['occurred_at'] or '',r['_origin_device_id'],r['agent'],r['_source_rowid'])
        rows.sort(key=order)
        return _dedupe(rows, kind)

    def windows(self, low, high, strict_low, strict_high, device):
        return (self.read(self.db,'message',device,low,high),
                self.read(self.db,'message',device,strict_low,strict_high))


class _SnapshotDB:
    def __init__(self, db, conn):
        self.path, self.conn = db.path, conn

    @contextmanager
    def read(self):
        # Turn evidence and compact facts must share the worker snapshot.
        yield self.conn


class SessionCatalog:
    def __init__(self, db):
        self.db = db
        directory = _cache_dir(db);directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        key = hashlib.sha256(str(db.path.resolve()).encode()).hexdigest()[:24]
        self.path = directory/f'session-catalog-v{_VERSION}-{key}.sqlite3'
        self.lock_path = self.path.with_suffix('.lock')
        self.stop_event, self.wake = Event(), Event()
        self.thread = None
        self._registrations = {}
        self._registration_lock = Lock()
        self.rebuilt_keys = 0
        with closing(_connect(self.path)) as c:
            c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA synchronous=NORMAL')
            c.executescript('''
              CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
              INSERT OR IGNORE INTO meta VALUES('seen','0');
              CREATE TABLE IF NOT EXISTS scopes(id INTEGER PRIMARY KEY,spec TEXT UNIQUE NOT NULL,
                state TEXT NOT NULL DEFAULT 'queued',total INTEGER NOT NULL DEFAULT 0,
                last_use REAL NOT NULL,applied INTEGER NOT NULL DEFAULT 0,error TEXT);
              CREATE TABLE IF NOT EXISTS pending(scope INTEGER REFERENCES scopes(id) ON DELETE CASCADE,
                device_id TEXT,agent TEXT,native_id TEXT,PRIMARY KEY(scope,device_id,agent,native_id));
              CREATE TABLE IF NOT EXISTS rows(scope INTEGER REFERENCES scopes(id) ON DELETE CASCADE,
                device_id TEXT,agent TEXT,native_id TEXT,last_at TEXT NOT NULL,payload_json TEXT NOT NULL,
                PRIMARY KEY(scope,device_id,agent,native_id));
              CREATE INDEX IF NOT EXISTS ix_catalog_page ON rows(scope,last_at DESC,device_id,agent,native_id);
            ''')
            identity = str((db.path.stat().st_dev, db.path.stat().st_ino))
            prior = c.execute("SELECT value FROM meta WHERE key='source_identity'").fetchone()
            if prior and prior[0] != identity:
                c.execute('DELETE FROM scopes');c.execute("UPDATE meta SET value='0' WHERE key='seen'")
            c.execute("INSERT OR REPLACE INTO meta VALUES('source_identity',?)",(identity,))
        os.chmod(self.path,0o600)

    def register(self, day, through, tz, device_id):
        from .multi_usage import _range
        _range(day,through,tz)
        spec = json.dumps((day,through,tz,device_id),separators=(',',':'))
        # GET only queues demand. The one background writer owns persistent
        # scope registration/LRU, so readers never wait for its write lock.
        with self._registration_lock:
            self._registrations[spec] = time.time()
        self.wake.set()
        return spec

    def _flush_registrations(self):
        with self._registration_lock:
            pending=self._registrations
            self._registrations={}
        if not pending:return
        try:
            with closing(_connect(self.path)) as c:
                c.execute('BEGIN IMMEDIATE')
                c.executemany('INSERT INTO scopes(spec,last_use) VALUES(?,?) ON CONFLICT(spec) DO UPDATE SET last_use=excluded.last_use',pending.items())
                c.execute('DELETE FROM scopes WHERE id IN (SELECT id FROM scopes ORDER BY last_use DESC LIMIT -1 OFFSET ?)',(_MAX_SCOPES,))
                c.execute('COMMIT')
        except BaseException:
            with self._registration_lock:
                for spec,stamp in pending.items():
                    self._registrations[spec]=max(stamp,self._registrations.get(spec,stamp))
            raise

    def query(self, day, through, tz, device_id, *, agent, query, limit, offset, session_scope='conversation'):
        from .archive_views import AGENTS
        from .session_identity import scope_sql, classifications
        scope_sql(session_scope)
        identities = classifications(self.db)
        spec = self.register(day,through,tz,device_id)
        with closing(_connect(self.path,True)) as c:
            c.execute('CREATE TEMP TABLE identity_kind(device_id,agent,native_id,session_kind,PRIMARY KEY(device_id,agent,native_id))')
            c.executemany('INSERT INTO identity_kind VALUES(?,?,?,?)',
                          [(*key,value['session_kind']) for key,value in identities.items()])
            c.execute('BEGIN')
            s = c.execute('SELECT * FROM scopes WHERE spec=?',(spec,)).fetchone()
            if s is None:
                return {'projection':{'state':'building','phase':'queued','done':0,'total':0}}
            pending = c.execute('SELECT count(*) FROM pending WHERE scope=?',(s['id'],)).fetchone()[0]
            projection = {'state':'ready' if s['state']=='ready' else 'building',
                          'done':max(0,s['total']-pending),'total':s['total'],
                          'source_checkpoint':s['applied'],'pending_sessions':pending}
            if s['error']:projection['error']=s['error']
            if s['state']!='ready':return {'projection':projection}
            where, params = ('scope=? AND agent=?',[s['id'],agent]) if agent else ('scope=?',[s['id']])
            kind = "coalesce((SELECT session_kind FROM identity_kind i WHERE i.device_id=rows.device_id AND i.agent=rows.agent AND i.native_id=rows.native_id),'unknown')"
            where += ' AND ' + (f"{kind}='internal'" if session_scope == 'internal' else
                                f"{kind}<>'internal'" if session_scope == 'conversation' else '1')
            counts = dict.fromkeys(AGENTS,0)
            for r in c.execute('SELECT agent,count(*) FROM rows WHERE '+where+' GROUP BY agent',params):
                if r[0] in counts:counts[r[0]]=r[1]
            total = c.execute('SELECT count(*) FROM rows WHERE '+where,params).fetchone()[0]
            items = [json.loads(r[0]) for r in c.execute('SELECT payload_json FROM rows WHERE '+where+
                ' ORDER BY last_at DESC,device_id,agent,native_id LIMIT ? OFFSET ?',params+[limit,offset])]
        # Validate local folder presence only for the selected page, as before.
        from . import archive
        local_id = archive.local_device_id(self.db)
        for r in items:
            r.update(identities.get((r['device_id'],r['agent'],r['native_id']),
                                   {'session_kind':'unknown','session_source':None}))
            sources=r.pop('_sources',[])
            r['can_open_folder']=r['device_id']==local_id and any(
                not str(p).startswith('/Volumes/Data') and Path(p).parent.is_dir() for p in sources)
        return {'day':day,'through':through,'query':query,'counts':counts,'session_count':total,
                'sessions':items,'sort':'recent','projection':projection}

    def step(self, max_keys=32):
        """One bounded source snapshot and one atomic projection publication."""
        self.lock_path.touch(mode=0o600,exist_ok=True)
        with self.lock_path.open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return False
            return self._step(max_keys)

    def _step(self, max_keys):
        self._flush_registrations()
        from .archive_views import _browser_uncached, _utc_key
        from .activity import MAX_ESTIMATE_SECONDS
        from .multi_usage import _range
        from datetime import timedelta
        seen = 0
        with self.db.read() as source, closing(_connect(self.path)) as cache:
            source.execute('BEGIN')
            aliases = dict(source.execute('SELECT alias_id,canonical_id FROM raw_archive_device_aliases'))
            old_seen = int(cache.execute("SELECT value FROM meta WHERE key='seen'").fetchone()[0])
            high = source.execute("SELECT coalesce(max(seq),0) FROM sqlite_sequence WHERE name='recent_catalog_changes'").fetchone()[0]
            changes = source.execute('SELECT * FROM recent_catalog_changes WHERE seq>? ORDER BY seq LIMIT 2000',(old_seen,)).fetchall()
            seen = changes[-1]['seq'] if changes else old_seen
            reset = old_seen>high or any(r['device_id']=='*' for r in changes)
            cache.execute('BEGIN IMMEDIATE')
            if reset:
                cache.execute("UPDATE scopes SET state='queued',error=NULL")
                cache.execute('DELETE FROM rows');cache.execute('DELETE FROM pending')
                if old_seen>high:seen=high
            else:
                dirty = {(aliases.get(r['device_id'],r['device_id']),r['agent'],r['native_id']) for r in changes}
                # A verified run of another agent suppresses a same-identity
                # Codex estimate. Invalidate that dependent row when it exists.
                for d, a, n in list(dirty):
                    if a != 'codex' and any(source.execute(
                        "SELECT 1 FROM archive_facts WHERE device_id=? AND agent='codex' AND native_id=? LIMIT 1",(origin,n)).fetchone()
                        for origin in [d]+[x for x,v in aliases.items() if v==d]):
                        dirty.add((d,'codex',n))
                for s in cache.execute("SELECT id,spec FROM scopes WHERE state<>'queued'").fetchall():
                    device = json.loads(s['spec'])[3];canonical=aliases.get(device,device)
                    cache.executemany('INSERT OR IGNORE INTO pending VALUES(?,?,?,?)',
                        [(s['id'],*key) for key in dirty if device is None or key[0]==canonical])
            cache.execute("UPDATE meta SET value=? WHERE key='seen'",(str(seen),))
            queued = cache.execute("SELECT * FROM scopes WHERE state='queued' ORDER BY last_use DESC LIMIT 1").fetchone()
            if queued:
                device=json.loads(queued['spec'])[3];canonical=aliases.get(device,device)
                keys={(aliases.get(r[0],r[0]),r[1],r[2]) for r in source.execute(
                    "SELECT device_id,agent,native_id FROM archive_facts GROUP BY device_id,agent,native_id")
                    if device is None or aliases.get(r[0],r[0])==canonical}
                cache.executemany('INSERT OR IGNORE INTO pending VALUES(?,?,?,?)',[(queued['id'],*key) for key in keys])
                cache.execute("UPDATE scopes SET state=?,total=?,error=NULL WHERE id=?",('building' if keys else 'ready',len(keys),queued['id']))
            cache.execute('COMMIT')
            scope=cache.execute('''SELECT s.* FROM scopes s WHERE EXISTS(SELECT 1 FROM pending p WHERE p.scope=s.id)
              ORDER BY CASE s.state WHEN 'building' THEN 0 ELSE 1 END,s.last_use DESC LIMIT 1''').fetchone()
            if scope:
                keys=[tuple(r) for r in cache.execute('SELECT device_id,agent,native_id FROM pending WHERE scope=? LIMIT ?',(scope['id'],max_keys))]
                day,through,tz,device=json.loads(scope['spec']);_,_,low,high_dt,_=_range(day,through,tz)
                reader=_CompactReader(self.db,source,keys,aliases)
                windows=lambda:reader.windows(_utc_key(low-timedelta(seconds=MAX_ESTIMATE_SECONDS+1)),
                    _utc_key(high_dt+timedelta(seconds=MAX_ESTIMATE_SECONDS+1)),_utc_key(low),_utc_key(high_dt),device)
                rows=_browser_uncached(_SnapshotDB(self.db,source),day,through,tz,device_id=device,_base=True,
                    _session_keys=set(keys),_reader=reader.read,_windows=windows)
                cache.execute('BEGIN IMMEDIATE')
                if cache.execute('SELECT 1 FROM scopes WHERE id=?',(scope['id'],)).fetchone():
                    cache.executemany('DELETE FROM rows WHERE scope=? AND device_id=? AND agent=? AND native_id=?',[(scope['id'],*k) for k in keys])
                    cache.executemany('INSERT OR REPLACE INTO rows VALUES(?,?,?,?,?,?)',
                        [(scope['id'],r['device_id'],r['agent'],r['native_id'],r['last_at'],json.dumps(r,ensure_ascii=False)) for r in rows])
                    cache.executemany('DELETE FROM pending WHERE scope=? AND device_id=? AND agent=? AND native_id=?',[(scope['id'],*k) for k in keys])
                    remaining=cache.execute('SELECT count(*) FROM pending WHERE scope=?',(scope['id'],)).fetchone()[0]
                    cache.execute('UPDATE scopes SET error=NULL,state=?,applied=CASE WHEN ?=0 THEN ? ELSE applied END WHERE id=?',
                        ('ready' if not remaining else scope['state'],remaining,seen,scope['id']))
                cache.execute('COMMIT');self.rebuilt_keys+=len(keys)
        # Prune ONLY this derived journal after the queue is durable. A wiped
        # cache always seeds from the complete source, never from retained deltas.
        if changes:
            with self.db.tx() as c:c.execute('DELETE FROM recent_catalog_changes WHERE seq<=?',(seen,))
        return bool(scope or queued or changes)

    def start(self):
        if self.thread is not None:return
        self.thread=Thread(target=self._loop,name='awb-session-projection',daemon=True);self.thread.start()

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                worked=self.step()
                delay=0.02 if worked else 1
            except Exception as exc:
                with closing(_connect(self.path)) as c:
                    c.execute('UPDATE scopes SET error=?',(type(exc).__name__+': '+str(exc)[:160],))
                delay=2
            self.wake.wait(delay);self.wake.clear()

    def stop(self):
        self.stop_event.set();self.wake.set()
        if self.thread:self.thread.join(timeout=3)


def catalog(db):
    key=str(db.path.resolve())
    with _REGISTRY_LOCK:
        if key not in _REGISTRY:_REGISTRY[key]=SessionCatalog(db)
        return _REGISTRY[key]
