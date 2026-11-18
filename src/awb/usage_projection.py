"""Single-writer incremental usage projection; HTTP reads never maintain it.

Groups span ALL devices for one (agent,native_id), preserving counter chains,
aggregate replacement and copied-request semantics of the existing reducers.
Source facts+journal and projection rows+watermark commit in their own databases.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from threading import Event, Lock, Thread
import time
from uuid import uuid4

from .query_model import _cache_dir

RULES = 'usage-existing-reducer-v1'
_REGISTRY = {}
_REGISTRY_LOCK = Lock()
_CURRENT = ContextVar('awb_usage_snapshot', default=None)
_AGENTS = ('codex','claude','hermes')
_STATS = ('counter_intervals','unallocated_rows','missing_cache_read','missing_cache_write','unknown_call_count_rows')


class ProjectionInitializing(RuntimeError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _connect(path, readonly=False):
    c = sqlite3.connect(path.resolve().as_uri()+'?mode=ro' if readonly else str(path),
                        uri=readonly, timeout=2, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA cache_size=-16384')
    if readonly:
        c.execute('PRAGMA query_only=ON')
    return c


def install(db):
    """Additive CDC only. UPDATE captures BOTH dependency keys; no journal prune."""
    with db.tx() as c:
        c.execute('CREATE TABLE IF NOT EXISTS awb_usage_origin(id INTEGER PRIMARY KEY CHECK(id=1),epoch TEXT NOT NULL)')
        c.execute('INSERT OR IGNORE INTO awb_usage_origin VALUES(1,?)',(str(uuid4()),))
        c.execute('CREATE TABLE IF NOT EXISTS awb_usage_changes(seq INTEGER PRIMARY KEY AUTOINCREMENT,agent TEXT NOT NULL,native_id TEXT NOT NULL)')
        c.execute("CREATE INDEX IF NOT EXISTS ix_awb_usage_group ON archive_facts(agent,native_id,device_id) WHERE kind='usage'")
        for action, refs in [('INSERT',['NEW']),('UPDATE',['OLD','NEW']),('DELETE',['OLD'])]:
            body=''.join(f"INSERT INTO awb_usage_changes(agent,native_id) SELECT {r}.agent,{r}.native_id WHERE {r}.kind='usage';" for r in refs)
            c.execute(f'CREATE TRIGGER IF NOT EXISTS awb_usage_fact_{action.lower()} AFTER {action} ON archive_facts BEGIN {body} END')
            c.execute(f"CREATE TRIGGER IF NOT EXISTS awb_usage_alias_{action.lower()} AFTER {action} ON raw_archive_device_aliases BEGIN INSERT INTO awb_usage_changes(agent,native_id) VALUES('*','*'); END")


def load_source(c, groups):
    """Materialize exactly one source snapshot, then release it before reduction."""
    aliases=dict(c.execute('SELECT alias_id,canonical_id FROM raw_archive_device_aliases'))
    columns='SELECT rowid AS source_rowid,* FROM archive_facts WHERE kind=\'usage\''
    if groups is None:
        cursors=[c.execute(columns+' ORDER BY occurred_at,device_id,agent,rowid')]
    else:
        # Explicit group index avoids SQLite preferring the kind/time index for
        # ORDER BY and scanning every usage fact for a six-row dirty group.
        grouped_sql=columns.replace('FROM archive_facts','FROM archive_facts INDEXED BY ix_awb_usage_group')
        cursors=[c.execute(grouped_sql+' AND agent=? AND native_id=? ORDER BY occurred_at,device_id,agent,rowid',key)
                 for key in sorted(groups)]
    rows=[]
    for cursor in cursors:
        for r in cursor:
            p=json.loads(r['payload_json'])
            rows.append({**p,'device_id':aliases.get(r['device_id'],r['device_id']),
                         'agent':r['agent'],'native_id':r['native_id'],'fact_id':r['fact_id'],
                         'occurred_at':r['occurred_at'],'_origin_device_id':r['device_id']})
    return rows,aliases


def _reduce(rows):
    from .raw_archive import _usage, _copied_usage
    reduced=_usage(rows)
    output=[('*',_copied_usage(reduced)),('unfiltered',reduced)]
    by_device=defaultdict(list)
    for row in rows:by_device[row['device_id']].append(row)
    output.extend(('device:'+device,_usage(sorted(group,key=lambda r:
        (r['_origin_device_id'],r['agent'],r['native_id'],r['fact_id'])))) for device,group in by_device.items())
    return output


def _encode(generation,key,scope,rows):
    from .archive_views import _hermes_bounds
    data=[]
    stats={name:{'times':[],**dict.fromkeys(_STATS,0),'exact':False} for name in _AGENTS}
    for ordinal,row in enumerate(rows):
        name=row['agent'];first=last=point=None;stat=stats.get(name)
        if stat is not None:
            stat['counter_intervals']+=row.get('precision')=='counter_interval'
            stat['unallocated_rows']+=row.get('precision')=='unallocated'
            stat['unknown_call_count_rows']+=bool(row.get('raw_archive_version') and row.get('api_call_count') is None)
            stat['exact']|=row.get('precision') not in {'counter_interval','unallocated'}
            if name=='hermes':
                stat['missing_cache_read']+=row.get('cache_read_tokens') is None
                stat['missing_cache_write']+=row.get('cache_write_tokens') is None
        try:
            if name=='hermes':
                bounds=_hermes_bounds(row)
                if bounds is not None:
                    low,high=bounds;first,last,point=low.timestamp(),high.timestamp(),high.timestamp()
                    if stat is not None:stat['times'].extend((low.isoformat(),high.isoformat()))
            elif row.get('occurred_at'):
                if stat is not None:stat['times'].append(row['occurred_at'])
                point=datetime.fromisoformat(row['occurred_at'].replace('Z','+00:00')).timestamp()
        except (ValueError,TypeError,KeyError,OverflowError):pass
        data.append((generation,scope,*key,ordinal,name,row['native_id'],point,first,last,
                     row.get('precision'),row.get('model'),json.dumps(row,ensure_ascii=False,separators=(',',':'))))
    coverage=[]
    for name,stat in stats.items():
        times=stat.pop('times')
        coverage.append((generation,scope,*key,name,min(times) if times else None,max(times) if times else None,
                         *[stat[k] for k in _STATS],int(stat['exact'])))
    return data,coverage


class UsageProjection:
    def __init__(self,db):
        self.db=db
        key=hashlib.sha256(str(db.path.resolve()).encode()).hexdigest()[:24]
        self.path=_cache_dir(db)/f'usage-projection-v2-{key}.sqlite3'
        self.lock_path=self.path.with_suffix('.lock')
        self.stop_event=Event();self.wake=Event();self.thread=None;self.enabled=False

    def prepare(self):
        install(self.db)
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with closing(_connect(self.path)) as c:
            c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA synchronous=NORMAL')
            c.executescript('''
              CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS usage_rows(
                generation INTEGER,scope TEXT,group_agent TEXT,group_native TEXT,ordinal INTEGER,
                agent TEXT,native_id TEXT,point REAL,first_seen REAL,last_seen REAL,precision TEXT,model TEXT,payload_json TEXT,
                PRIMARY KEY(generation,scope,group_agent,group_native,ordinal));
              CREATE INDEX IF NOT EXISTS ix_up_time ON usage_rows(generation,scope,agent,point);
              CREATE INDEX IF NOT EXISTS ix_up_precision ON usage_rows(generation,scope,precision);
              CREATE INDEX IF NOT EXISTS ix_up_bounds ON usage_rows(generation,scope,agent,first_seen,last_seen);
              CREATE INDEX IF NOT EXISTS ix_up_group ON usage_rows(generation,group_agent,group_native);
              CREATE TABLE IF NOT EXISTS coverage(
                generation INTEGER,scope TEXT,group_agent TEXT,group_native TEXT,agent TEXT,earliest TEXT,latest TEXT,
                counter_intervals INTEGER,unallocated_rows INTEGER,missing_cache_read INTEGER,missing_cache_write INTEGER,
                unknown_call_count_rows INTEGER,exact INTEGER,
                PRIMARY KEY(generation,scope,group_agent,group_native,agent));
              CREATE INDEX IF NOT EXISTS ix_up_coverage_group ON coverage(generation,group_agent,group_native);
              CREATE TABLE IF NOT EXISTS aliases(alias_id TEXT PRIMARY KEY,canonical_id TEXT);
            ''')
            c.execute("INSERT OR IGNORE INTO meta VALUES('read_model_id',?)",(str(uuid4()),))
        os.chmod(self.path,0o600)

    def _job(self,value):
        with closing(_connect(self.path)) as c:
            c.execute("INSERT OR REPLACE INTO meta VALUES('job',?)",(json.dumps(value),))

    def status(self):
        if not self.path.exists():return {'has_published_result':False,'build_status':'initializing'}
        with closing(_connect(self.path,True)) as c:
            state=c.execute("SELECT value FROM meta WHERE key='state'").fetchone()
            job=c.execute("SELECT value FROM meta WHERE key='job'").fetchone()
        return {**(json.loads(state[0]) if state else {'has_published_result':False}),
                **(json.loads(job[0]) if job else {'build_status':'idle'})}

    def _insert(self,c,data,coverage):
        c.executemany('INSERT INTO usage_rows VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',data)
        c.executemany('INSERT INTO coverage VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',coverage)

    def step(self,force=False):
        self.lock_path.touch(mode=0o600,exist_ok=True)
        with self.lock_path.open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return False
            return self._step(force)

    def _step(self,force):
        started=time.monotonic();old=self.status();identity=str((self.db.path.stat().st_dev,self.db.path.stat().st_ino))
        with self.db.read() as source:
            source.execute('BEGIN')
            epoch=source.execute('SELECT epoch FROM awb_usage_origin WHERE id=1').fetchone()[0]
            high=source.execute('SELECT coalesce(max(seq),0) FROM awb_usage_changes').fetchone()[0]
            cursor=old.get('projected_seq',0)
            full=(force or not old.get('has_published_result') or old.get('source_epoch')!=epoch
                  or old.get('source_identity')!=identity or high<cursor or old.get('rules_version')!=RULES)
            if not full and high==cursor:return False
            dirty=None if full else {tuple(r) for r in source.execute(
                'SELECT DISTINCT agent,native_id FROM awb_usage_changes WHERE seq>? AND seq<=?',(cursor,high))}
            if dirty is not None and (('*','*') in dirty or len(dirty)>500):full=True;dirty=None
            inputs,aliases=load_source(source,dirty)
        grouped=defaultdict(list)
        for row in inputs:grouped[(row['agent'],row['native_id'])].append(row)
        keys=set(grouped) if full else dirty
        generation=old.get('generation',0)+1 if full else old['generation']
        revision=1 if full else old['revision']+1
        self._job({'build_status':'building','target_seq':high,'started_at':_now()})
        data=[];coverage=[];written=0
        with closing(_connect(self.path)) as c:
            if full:
                c.execute('BEGIN IMMEDIATE')
                # Remove only an unpublished attempt at this generation.
                c.execute('DELETE FROM usage_rows WHERE generation=?',(generation,))
                c.execute('DELETE FROM coverage WHERE generation=?',(generation,));c.execute('COMMIT')
            for key in sorted(keys):
                if self.stop_event.is_set():raise InterruptedError('projection stopped before publication')
                for scope,rows in _reduce(grouped.get(key,[])):
                    a,b=_encode(generation,key,scope,rows);data.extend(a);coverage.extend(b)
                if full and len(data)>=3000:
                    c.execute('BEGIN IMMEDIATE')
                    try:self._insert(c,data,coverage);c.execute('COMMIT')
                    except BaseException:
                        if c.in_transaction:c.execute('ROLLBACK')
                        raise
                    written+=len(data);data=[];coverage=[]
            c.execute('BEGIN IMMEDIATE')
            try:
                if not full:
                    for key in keys:
                        c.execute('DELETE FROM usage_rows WHERE generation=? AND group_agent=? AND group_native=?',(generation,*key))
                        c.execute('DELETE FROM coverage WHERE generation=? AND group_agent=? AND group_native=?',(generation,*key))
                self._insert(c,data,coverage);written+=len(data)
                c.execute('DELETE FROM aliases');c.executemany('INSERT INTO aliases VALUES(?,?)',aliases.items())
                mid=c.execute("SELECT value FROM meta WHERE key='read_model_id'").fetchone()[0]
                state={'has_published_result':True,'read_model_id':mid,'source_epoch':epoch,'source_identity':identity,
                       'generation':generation,'revision':revision,'projected_seq':high,'rules_version':RULES,
                       'last_success_at':_now(),'last_run':{'full_rebuild':full,'groups':len(keys),'input_rows':len(inputs),
                       'rows_written':written,'seconds':round(time.monotonic()-started,4)}}
                c.execute("INSERT OR REPLACE INTO meta VALUES('state',?)",(json.dumps(state),))
                c.execute("INSERT OR REPLACE INTO meta VALUES('job',?)",(json.dumps({'build_status':'idle'}),))
                c.execute('COMMIT')
            except BaseException:
                if c.in_transaction:c.execute('ROLLBACK')
                raise
            # Only derived, inactive generations; current and immediate rollback
            # generation stay. Old readers retain their SQLite snapshot.
            stale=[r[0] for r in c.execute('SELECT DISTINCT generation FROM usage_rows WHERE generation<?',(generation-1,))]
            for gen in stale:
                while c.execute('DELETE FROM usage_rows WHERE rowid IN (SELECT rowid FROM usage_rows WHERE generation=? LIMIT 3000)',(gen,)).rowcount:pass
                c.execute('DELETE FROM coverage WHERE generation=?',(gen,))
        return True

    @contextmanager
    def snapshot(self):
        if not self.path.exists():raise ProjectionInitializing('usage projection initializing')
        with closing(_connect(self.path,True)) as c:
            c.execute('BEGIN')
            found=c.execute("SELECT value FROM meta WHERE key='state'").fetchone()
            if not found:raise ProjectionInitializing('usage projection initializing')
            state=json.loads(found[0])
            if state['source_identity']!=str((self.db.path.stat().st_dev,self.db.path.stat().st_ino)):
                raise ProjectionInitializing('source identity changed; rebuilding projection')
            with self.db.read() as source:
                source.execute('BEGIN')
                epoch=source.execute('SELECT epoch FROM awb_usage_origin WHERE id=1').fetchone()[0]
                latest=source.execute('SELECT coalesce(max(seq),0) FROM awb_usage_changes').fetchone()[0]
            if epoch!=state['source_epoch'] or latest<state['projected_seq']:
                raise ProjectionInitializing('source epoch changed; rebuilding projection')
            job=c.execute("SELECT value FROM meta WHERE key='job'").fetchone()
            state.update(json.loads(job[0]) if job else {'build_status':'idle'})
            state.update(source_latest_seq=latest,freshness='current' if latest==state['projected_seq'] else 'catching_up')
            deadline=time.monotonic()+8
            c.set_progress_handler(lambda:int(time.monotonic()>deadline),10000)
            try:yield _Snapshot(self.db,c,state)
            finally:
                c.set_progress_handler(None,0)
                if c.in_transaction:c.execute('ROLLBACK')

    def start(self):
        if self.thread is not None:return
        self.prepare();self.enabled=True
        self.thread=Thread(target=self._loop,name='awb-usage-projection',daemon=True);self.thread.start()

    def _loop(self):
        while not self.stop_event.is_set():
            try:worked=self.step();delay=0.5 if worked else 2
            except Exception as exc:
                self._job({'build_status':'failed','error':type(exc).__name__+': '+str(exc)[:200]});delay=5
            self.wake.wait(delay);self.wake.clear()

    def stop(self):
        self.stop_event.set();self.wake.set()
        if self.thread:self.thread.join(timeout=3)


class _Snapshot:
    def __init__(self,db,conn,state):
        self.db,self.conn,self.state=db,conn,state
        self._ranges={}

    def session_tokens(self, keys):
        """Read lifetime quantities only for the current page, from per-device reductions."""
        from .archive_views import _usage_values

        totals = {}
        partial = set()
        rows = self.conn.execute('''SELECT k.key,u.precision,u.payload_json
            FROM json_each(?) AS k
            LEFT JOIN aliases AS a ON a.alias_id=json_extract(k.value,'$[0]')
            CROSS JOIN usage_rows AS u
            WHERE u.generation=? AND u.scope='device:'||coalesce(a.canonical_id,json_extract(k.value,'$[0]'))
              AND u.group_agent=json_extract(k.value,'$[1]') AND u.group_native=json_extract(k.value,'$[2]')''',
            (json.dumps(keys), self.state['generation']))
        for index, precision, payload in rows:
            row = json.loads(payload)
            if precision == 'unallocated':
                reason = row.get('omission_reason')
                if reason != 'overlap_with_exact_requests' and not (
                        reason == 'opening_balance' and row.get('total_tokens') == 0):
                    partial.add(index)
                continue
            if not any(row.get(field) is not None for field in (
                    'input_tokens', 'output_tokens', 'cached_input_tokens', 'cache_creation_tokens',
                    'cache_read_tokens', 'cache_write_tokens')):
                partial.add(index)
                continue
            totals[index] = totals.get(index, 0) + _usage_values(row)['total_tokens']
        return {key: (totals.get(index), index in partial) for index, key in enumerate(keys)}

    def usage(self,start,end,device_id=None,agent=None,native_id=None):
        key=(start,end,device_id,agent,native_id)
        if key in self._ranges:return self._ranges[key]
        base=(start,end,device_id,None,None)
        if base in self._ranges:
            rows,info=self._ranges[base]
            result=([r for r in rows if (agent is None or r['agent']==agent)
                     and (native_id is None or r['native_id']==native_id)],info)
            self._ranges[key]=result
            return result
        c=self.conn;gen=self.state['generation']
        alias=c.execute('SELECT canonical_id FROM aliases WHERE alias_id=?',(device_id,)).fetchone()
        canonical=alias[0] if alias else device_id
        scope='*' if device_id is None else 'unfiltered' if device_id=='' else 'device:'+canonical
        extra='';tail=[]
        if agent is not None:extra+=' AND agent=?';tail.append(agent)
        if native_id is not None:extra+=' AND native_id=?';tail.append(native_id)
        # UNION small rowids, not large JSON payloads. Range semantics match v1.
        branches=[("agent='hermes' AND first_seen<? AND last_seen>=?",[end.timestamp(),start.timestamp()]),
                  ("agent='hermes' AND first_seen>=? AND last_seen<?",[start.timestamp(),end.timestamp()]),
                  ("agent<>'hermes' AND point>=? AND point<?",[start.timestamp(),end.timestamp()])]
        diagnostic_count=c.execute("SELECT count(*) FROM usage_rows WHERE generation=? AND scope=? AND precision='unallocated'"+extra,
                                   [gen,scope,*tail]).fetchone()[0]
        # Unallocated observations have no additive contribution. Coverage keeps
        # their exact count; HTTP returns at most 100 diagnostic examples rather
        # than every historical counter opening balance on every day query.
        parts=["SELECT rowid FROM (SELECT rowid FROM usage_rows WHERE generation=? AND scope=? AND precision='unallocated'"+extra+' LIMIT 100)']
        params=[gen,scope,*tail]
        for condition,bounds in branches:
            parts.append('SELECT rowid FROM usage_rows WHERE generation=? AND scope=? AND '+condition+
                         " AND (precision IS NULL OR precision<>'unallocated')"+extra)
            params.extend([gen,scope,*bounds,*tail])
        sql='SELECT payload_json FROM usage_rows WHERE rowid IN ('+' UNION '.join(parts)+') ORDER BY group_agent,group_native,ordinal'
        rows=[json.loads(r[0]) for r in c.execute(sql,params)]
        info={name:{'status':'source_missing','earliest':None,'latest':None,**dict.fromkeys(_STATS,0),'exact':False} for name in _AGENTS}
        aggregate='SELECT agent,min(earliest),max(latest),'+','.join('sum('+s+')' for s in _STATS)+',max(exact) FROM coverage WHERE generation=? AND scope=? GROUP BY agent'
        for r in c.execute(aggregate,(gen,scope)):
            if r[0] in info:info[r[0]]={'status':'archived' if r[1] is not None else 'source_missing','earliest':r[1],
                'latest':r[2],**dict(zip(_STATS,r[3:8])),'exact':bool(r[8])}
        info['_projection']=self.state
        info['_diagnostics']={'unallocated_total':diagnostic_count,'sample_limit':100,
                              'has_more':diagnostic_count>100,'count_scope':'selected device/agent, all historical models'}
        self._ranges[key]=(rows,info)
        return rows,info


def projection(db):
    key=str(db.path.resolve())
    with _REGISTRY_LOCK:
        if key not in _REGISTRY:_REGISTRY[key]=UsageProjection(db)
        return _REGISTRY[key]


@contextmanager
def use_snapshot(db):
    with projection(db).snapshot() as s:
        token=_CURRENT.set(s)
        try:yield s
        finally:_CURRENT.reset(token)


def usage_range(db,start,end,*,device_id=None,agent=None,native_id=None):
    current=_CURRENT.get()
    if current is not None and current.db.path==db.path:
        return current.usage(start,end,device_id,agent,native_id)
    with projection(db).snapshot() as s:return s.usage(start,end,device_id,agent,native_id)


def enabled(db):
    return _CURRENT.get() is not None or projection(db).enabled
