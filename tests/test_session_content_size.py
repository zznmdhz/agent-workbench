"""Focused native byte-count and metadata-only ingestion regression."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from awb import archive_views
from awb.collector import make_event
from awb.db import Database
from awb.ingest import receive_batch
from awb.models import Batch


class SessionContentSizeTests(unittest.TestCase):
    def test_numeric_updates_preserve_identity_and_ignore_stale_snapshot(self):
        from awb.collector import hermes_content_size_event
        at = '2026-11-17T12:00:00Z'
        device, source, epoch = (str(uuid4()) for _ in range(3))
        native = 'synthetic-session'
        with tempfile.TemporaryDirectory() as tmp, sqlite3.connect(':memory:') as local:
            db = Database(Path(tmp)/'isolated.db')
            db.initialize()
            with db.tx() as c:
                c.execute('INSERT INTO devices(id,name,os,environment,token_hash,registered_at) VALUES(?,?,?,?,?,?)',
                          (device, 'Synthetic', 'Darwin', 'test', 'synthetic-hash', at))
                c.execute('INSERT INTO sources(id,device_id,agent,profile,capability_json,created_at) VALUES(?,?,?,?,?,?)',
                          (source, device, 'hermes', 'default', json.dumps({'content_policy':'stats_only'}), at))
            local.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,content TEXT,tool_calls TEXT)')
            local.execute('INSERT INTO messages VALUES(?,?,?,?)', (1,native,'你好','[]'))
            older = hermes_content_size_event(local, {'id':source}, native, device, {'content','tool_calls'})
            local.execute('INSERT INTO messages VALUES(?,?,?,?)', (2,native,'x',None))
            latest = hermes_content_size_event(local, {'id':source}, native, device, {'content','tool_calls'})
            self.assertEqual(latest['content']['payload'], {'payload_bytes':9})
            self.assertIsNone(hermes_content_size_event(local, {'id':source}, native, device, {'content'}))
            session = make_event(source,native,'session.observed','session','1',0,at,
                                 {'title':'Synthetic','created_at':at,'session_source':'desktop'},device,None)
            entries = [{'seq':i,'event':event,'observation':{'observed_at':at,'adapter_version':'test','source_locator':{}}}
                       for i,event in enumerate([session,latest,older],1)]
            result = receive_batch(db,device,Batch.model_validate({'batch_id':str(uuid4()),'collector_id':device,
                                          'outbox_epoch':epoch,'entries':entries}))
            self.assertEqual(result['projected_ack_seq'],3)
            self.assertEqual(archive_views.inspector(db,'hermes',native,device_id=device)['payload_bytes'],9)
            with db.read() as c:
                fact=json.loads(c.execute("SELECT payload_json FROM archive_facts WHERE kind='session'").fetchone()[0])
                self.assertEqual((fact['session_source'],fact['created_at']),('desktop',at))
                self.assertEqual(c.execute('SELECT count(*) FROM messages').fetchone()[0],0)
