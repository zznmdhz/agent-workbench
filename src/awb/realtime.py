"""Bounded realtime metadata scan/transport, independent of artifact backfills."""
from __future__ import annotations
from contextlib import closing
from pathlib import Path
import datetime,json,sqlite3,time
from collections import defaultdict
from .collector import (HERMES_MESSAGE_FIELDS,HERMES_SESSION_FIELDS,make_event,
    native_payload,_redact,iso_utc,sha256,unicode_codepoints,send_pending,hermes_usage_payload,
    hermes_content_size_event)

USAGE_NATIVE={'session_id','model','billing_provider','billing_mode','task','api_call_count',
 'input_tokens','output_tokens','cache_read_tokens','cache_write_tokens','reasoning_tokens',
 'estimated_cost_usd','actual_cost_usd','cost_status','cost_source','first_seen','last_seen'}

def message_fact(source,sid,m,device,available):
    policy=source.get('content_policy','stats_only');body=m['content'] or ''
    body=body if isinstance(body,str) else '';chars=unicode_codepoints(body)
    if policy=='stats_only':state,reason,body='stats_only','source_policy',None
    else:
        body,changed=_redact(body);state,reason=('redacted' if changed else 'full'),None
    p={'native_message_id':str(m['id']),'role':m['role'],
       'input_origin':'human' if m['role']=='user' else 'agent','content_state':state,
       'omission_reason':reason,'body':body,'source_text_char_count':chars,'finalized':True}
    if policy=='full_content':p['native']={**native_payload(dict(m),_redact),'omitted_native_columns':sorted(available-HERMES_MESSAGE_FIELDS)}
    return make_event(source['id'],sid,'message.observed',str(m['id']),'1',int(m['id']),iso_utc(m['timestamp']),p,device,source.get('environment_id'))

def scan(source,outbox,device,limit=500):
    if source.get('agent')!='hermes' or source.get('content_policy')=='excluded':return {'queued':0}
    root=Path(source['root']).expanduser().resolve();root=root/'state.db' if root.is_dir() else root
    locator='realtime-messages-v1';prior=outbox.cursor(source['id'],locator)
    with closing(sqlite3.connect(root.as_uri()+'?mode=ro',uri=True,timeout=3)) as c:
        c.row_factory=sqlite3.Row
        c.execute('BEGIN')  # Native rows and the cursor watermark share one snapshot.
        mc={r[1] for r in c.execute('PRAGMA table_info(messages)')};sc={r[1] for r in c.execute('PRAGMA table_info(sessions)')}
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if prior:offset=int(prior['offset'])
        else:
            midnight=datetime.datetime.now().astimezone().replace(hour=0,minute=0,second=0,microsecond=0).timestamp()-21600
            offset=(c.execute('SELECT min(id) FROM messages WHERE timestamp>=?',(midnight,)).fetchone()[0] or 1)-1
        names=','.join('m.'+x for x in sorted(mc&HERMES_MESSAGE_FIELDS))
        rows=c.execute('SELECT '+names+" FROM messages m JOIN sessions s ON s.id=m.session_id WHERE m.id>? AND m.role IN ('user','assistant') AND coalesce(s.source,'')<>'tool' ORDER BY m.id LIMIT ?",(max(0,offset-64),limit)).fetchall()
        grouped=defaultdict(list)
        for row in rows:grouped[row['session_id']].append(row)
        queued=0;latest=None
        for sid,msgs in grouped.items():
            if outbox.is_blocked(source['id'],sid):continue
            s=c.execute('SELECT '+','.join(sorted(sc&HERMES_SESSION_FIELDS))+' FROM sessions WHERE id=?',(sid,)).fetchone()
            at=iso_utc(s['started_at']);p={'title':s['title'],'cwd':s['cwd'],'created_at':at}
            if source.get('content_policy')=='full_content':
                p['native']={**native_payload(dict(s),_redact),'omitted_native_columns':sorted(sc-HERMES_SESSION_FIELDS),'unavailable_native_tables':[] if 'session_model_usage' in tables else ['session_model_usage']}
            facts=[make_event(source['id'],sid,'session.observed','session',sha256(p),0,at,p,device,source.get('environment_id'))]
            if max(m['id'] for m in msgs)>offset:
                size_fact=hermes_content_size_event(c,source,sid,device,mc)
                if size_fact:facts.append(size_fact)
            facts.extend(message_fact(source,sid,m,device,mc) for m in msgs)
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='session_model_usage'").fetchone():
                available={r[1] for r in c.execute('PRAGMA table_info(session_model_usage)')}
                for u in c.execute('SELECT * FROM session_model_usage WHERE session_id=?',(sid,)):
                    if source.get('content_policy')=='full_content':
                        data={k:u[k] for k in sorted(available&USAGE_NATIVE)};native=native_payload({'type':'session_model_usage','payload':data},_redact)
                        nid='usage:'+sha256([data.get(k) for k in ('model','billing_provider','billing_mode','task')])
                        facts.append(make_event(source['id'],sid,'native.observed',nid,sha256(native),None,None,native,device,source.get('environment_id')))
                    p=hermes_usage_payload(dict(u))
                    facts.append(make_event(source['id'],sid,'usage.observed',p['usage_key'],sha256(p),None,None,p,device,source.get('environment_id'),{'certainty':'recorded','rule_id':'hermes_session_model_aggregate','reason':'Native model counters; only attribute to buckets containing their complete native bounds'}))
            queued+=outbox.append(source['id'],'realtime-session:'+sid,max(m['id'] for m in msgs),'realtime',sid,facts)
            times=[iso_utc(m['timestamp']) for m in msgs if m['timestamp'] is not None]
            if times:latest=max([latest or '',*times])
        cursor=max([offset,*[int(m['id']) for m in rows]])
        native_max=c.execute('SELECT coalesce(max(id),0) FROM messages').fetchone()[0]
        if len(rows)<limit:cursor=max(cursor,native_max)
        outbox.append(source['id'],locator,cursor,'realtime',None,[])
        return {'queued':queued,'scanned_message_id':cursor,'native_message_at':latest,'scan_backlog':cursor<native_max,'native_highwatermark':native_max}

def queue_state(outbox):
    with closing(outbox.connect()) as c:
        rows=c.execute("SELECT traffic_class,receipt_id IS NOT NULL,count(*) FROM outbox WHERE state='pending' GROUP BY traffic_class,receipt_id IS NOT NULL").fetchall()
    return {'realtime_pending':sum(n for lane,received,n in rows if not received and (lane or 100)<=20),
            'history_pending':sum(n for lane,received,n in rows if not received and (lane or 100)>20),
            'received_waiting_ack':sum(n for _,received,n in rows if received)}

def run(config,outbox,runtime,guard,blocked):
    counter=0;total=0;last_native=None
    while True:
        start=time.time();record={'started_at':iso_utc(start),'status':'ok','collector_device_id':config['collector_id']}
        try:
            guard();scans=[]
            for source in config['sources']:
                if blocked(source['root']):raise RuntimeError('Retired source forbidden')
                result=scan(source,outbox,config['collector_id']);scans.append({'source_id':source['id'],**result})
                if result.get('native_message_at'):last_native=max(last_native or '',result['native_message_at'])
            counter+=1
            sent=send_pending(config,outbox,prefer_realtime=counter%5!=0);total+=sent['sent']
            record.update(scans=scans,scan_backlog=any(s.get('scan_backlog',False) for s in scans),sent=sent['sent'],sent_total=total,native_message_at=last_native,**queue_state(outbox))
        except Exception as exc:record.update(status='error',error_class=type(exc).__name__)
        record['finished_at']=iso_utc(time.time());record['seconds']=round(time.time()-start,3)
        target=Path(runtime)/'logs/realtime-status.json';tmp=target.with_suffix('.json.tmp');tmp.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n');tmp.replace(target)
        time.sleep(1 if record.get('realtime_pending') else 3)
