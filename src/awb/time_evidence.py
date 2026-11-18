"""Rebuildable turn-boundary evidence; never changes messages.finalized or events."""
from __future__ import annotations
import json
from collections import defaultdict

SCHEMA = '''CREATE TABLE IF NOT EXISTS message_turn_flags(
 source_id TEXT NOT NULL,native_message_id TEXT NOT NULL,event_id TEXT NOT NULL,
 turn_final INTEGER,finish_reason TEXT,basis TEXT NOT NULL,
 PRIMARY KEY(source_id,native_message_id));'''

def ensure(conn):
    conn.execute(SCHEMA)

def record(conn, event, agent, *, ensure_table=True):
    c=event['content'];p=c['payload']
    if agent!='hermes' or c['fact_kind']!='message.observed' or p.get('role')!='assistant':
        return False
    native=p.get('native') or {};raw=native.get('native_json')
    try: original=json.loads(raw) if isinstance(raw,str) else {}
    except (ValueError,TypeError):original={}
    known='finish_reason' in original
    reason=original.get('finish_reason')
    final=int(reason=='stop') if known else None
    if ensure_table:ensure(conn)
    conn.execute('''INSERT INTO message_turn_flags VALUES(?,?,?,?,?,?)
      ON CONFLICT(source_id,native_message_id) DO UPDATE SET
      event_id=excluded.event_id,turn_final=excluded.turn_final,
      finish_reason=excluded.finish_reason,basis=excluded.basis''',
      (c['source_instance_id'],str(p['native_message_id']),event['event_id'],final,
       str(reason) if reason is not None else None,
       'native_finish_reason' if known else 'native_finish_reason_unavailable'))
    return True

def apply(db, messages):
    """Overlay only derived time markers using indexed native-message keys."""
    groups=defaultdict(set);keys={}
    for i,row in enumerate(messages):
        if row.get('agent')!='hermes' or row.get('role')!='assistant':continue
        source=row.get('source_id');mid=str(row.get('message_id',''))
        fact=row.get('fact_id','')
        if fact.startswith('raw-message:'):
            _,source,mid=fact.split(':',2)
        if source:
            keys[i]=(source,mid);groups[source].add(mid)
    flags={}
    if groups:
        with db.read() as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='message_turn_flags'").fetchone():
                for source,mids in groups.items():
                    values=sorted(mids)
                    for at in range(0,len(values),500):
                        part=values[at:at+500]
                        for r in c.execute('SELECT source_id,native_message_id,turn_final,basis FROM message_turn_flags WHERE source_id=? AND native_message_id IN ('+','.join('?' for _ in part)+')',[source,*part]):
                            flags[(r[0],r[1])]=(r[2],r[3])
    unknown=0
    for i,key in keys.items():
        final,basis=flags.get(key,(None,'native_finish_reason_unavailable'))
        messages[i]['turn_final']=bool(final) if final is not None else False
        messages[i]['turn_final_basis']=basis
        unknown+=final is None
    return {'native_boundary_messages':len(keys),'unknown_boundary_messages':unknown}
