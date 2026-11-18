"""Cheap transport freshness, separate from activity estimates and heartbeats."""
from pathlib import Path
from datetime import datetime,timezone
import os,sys,json,sqlite3
from contextlib import closing

def status(db,device_id=None):
    result={'checked_at':datetime.now(timezone.utc).isoformat(),'coverage':'received_sources_only',
            'status':'unknown','duration_basis':'estimated_message_intervals'}
    with db.read() as c:
        if device_id:
            ids=[device_id]
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='raw_archive_device_aliases'").fetchone():
                row=c.execute('SELECT canonical_id FROM raw_archive_device_aliases WHERE alias_id=?',(device_id,)).fetchone()
                if row:ids=[row[0]]
                ids += [r[0] for r in c.execute('SELECT alias_id FROM raw_archive_device_aliases WHERE canonical_id=?',(ids[0],))]
            where=' AND device_id IN ('+','.join('?' for _ in ids)+')'
        else:where='';ids=[]
        row=c.execute("SELECT occurred_at FROM archive_message_summaries WHERE json_extract(payload_json,'$.role') IN ('user','assistant')"+where+' ORDER BY occurred_at DESC LIMIT 1',ids).fetchone()
        result['latest_received_message_at']=row[0] if row else None
    prefix=Path(sys.prefix);runtime=Path(os.environ.get('AWB_RUNTIME_ROOT',str(prefix.parent)))
    p=runtime/'logs/realtime-status.json'
    if p.is_file() and not p.is_symlink() and p.stat().st_size<128000:
        try:
            live=json.loads(p.read_text());at=datetime.fromisoformat(live['finished_at'].replace('Z','+00:00'));age=(datetime.now(timezone.utc)-at).total_seconds()
            local_ids={live.get('collector_device_id')}
            with db.read() as c:
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='raw_archive_sources'").fetchone():
                    for scan in live.get('scans',[]):
                        mapped=c.execute('SELECT archive_device_id FROM raw_archive_sources WHERE source_id=?',(scan.get('source_id'),)).fetchone()
                        if mapped:local_ids.add(mapped[0])
            if device_id and ids[0] not in local_ids:
                result['status']='remote_source_freshness_unavailable'
                return result
            result.update({k:live.get(k) for k in ['realtime_pending','history_pending','received_waiting_ack','native_message_at','error_class','scan_backlog']});result['local_transport_status']=live.get('status');result['local_transport_age_seconds']=round(age,1)
            result['status']=('catching_up' if (live.get('realtime_pending') or 0)>0 or live.get('scan_backlog',False) else 'local_source_current') if live.get('status')=='ok' and age<30 else 'transport_stale_or_error'
            if device_id:result['local_queue_scope']='Mini only; not a remote-device completeness guarantee'
        except (ValueError,KeyError,OSError):result['status']='unknown'
    return result
