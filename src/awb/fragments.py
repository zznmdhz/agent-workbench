"""Durable reassembly for canonical events larger than an ingest request."""
import base64
import json
from .archive_payload_codec import pack as pack_cold_payload
from .models import Envelope


def project_fragment(conn, outer):
    c = outer['content']
    p = c['payload']
    target, index, count = p['target_event_id'], p['fragment_index'], p['fragment_count']
    length = p['byte_length']
    if not (isinstance(index, int) and isinstance(count, int) and 0 <= index < count
            and isinstance(length, int) and 0 < length and count == (length + 262143) // 262144):
        raise ValueError('Invalid fragment manifest')
    data = base64.b64decode(p['data_base64'], validate=True)
    if len(data) > 262144:
        raise ValueError('Invalid fragment size')
    old = conn.execute('SELECT data,fragment_count,byte_length,source_id,native_session_id FROM capture_fragments WHERE target_event_id=? AND fragment_index=?', (target,index)).fetchone()
    expected = (data,count,length,c['source_instance_id'],c['native_session_id'])
    if old and (bytes(old[0]),*tuple(old)[1:]) != expected:
        raise ValueError('Fragment conflict')
    conn.execute('INSERT OR IGNORE INTO capture_fragments VALUES(?,?,?,?,?,?,?)',
                 (target,index,count,length,data,c['source_instance_id'],c['native_session_id']))
    rows = conn.execute('SELECT fragment_index,data,fragment_count,byte_length,source_id,native_session_id FROM capture_fragments WHERE target_event_id=? ORDER BY fragment_index',(target,)).fetchall()
    if len(rows) != count:
        return
    if [r[0] for r in rows] != list(range(count)) or any(tuple(r)[2:] != expected[1:] for r in rows):
        raise ValueError('Fragment identity mismatch')
    raw = b''.join(bytes(r[1]) for r in rows)
    if len(raw) != length:
        raise ValueError('Fragment length mismatch')
    event = Envelope.model_validate(json.loads(raw)).model_dump(mode='json')
    content = event['content']
    if (event['event_id'] != target or event['body_hash'] != p['target_body_hash']
            or any(content[k] != c[k] for k in ('source_instance_id','native_session_id','execution'))
            or content['fact_kind'] == 'capture.fragment'):
        raise ValueError('Fragment target mismatch')
    old = conn.execute('SELECT body_hash FROM events WHERE event_id=?',(target,)).fetchone()
    if old:
        if old[0] != event['body_hash']:
            raise ValueError('Reassembled event conflicts with immutable history')
        return
    conn.execute('''INSERT INTO events(event_id,body_hash,source_id,native_session_id,fact_kind,
        native_fact_id,revision_key,source_order,occurred_at,content_json,received_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))''',
        (target,event['body_hash'],content['source_instance_id'],content['native_session_id'],
         content['fact_kind'],content['native_fact_id'],content['revision_key'],content['source_order'],
         content['occurred_at'],pack_cold_payload(json.dumps(content,ensure_ascii=False))))
    from .projector import project
    project(conn,event)
