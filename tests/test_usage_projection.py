"""Focused projection contracts; synthetic data stays in an isolated directory."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

from awb import archive, raw_archive
from awb.db import Database


class UsageProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.previous = os.environ.get('AWB_QUERY_CACHE_DIR')
        os.environ['AWB_QUERY_CACHE_DIR'] = str(self.root/'cache')
        self.db = Database(self.root/'synthetic.sqlite')
        self.db.initialize()
        raw_archive.initialize(self.db)
        from awb.usage_projection import UsageProjection
        self.worker = UsageProjection(self.db)
        self.worker.prepare()

    def tearDown(self):
        self.worker.stop()
        if self.previous is None: os.environ.pop('AWB_QUERY_CACHE_DIR', None)
        else: os.environ['AWB_QUERY_CACHE_DIR'] = self.previous
        self.tmp.cleanup()

    def put(self, native, fid, value, device='a', agent='codex', at='2026-11-15T10:00:00Z'):
        with self.db.tx() as c:
            archive._upsert_local(c, device, 'usage', agent, native, fid, at, value)

    def rows(self, scope=None, low='2026-11-01T00:00:00+00:00', high='2026-12-01T00:00:00+00:00'):
        with self.worker.snapshot() as s:
            return s.usage(datetime.fromisoformat(low),datetime.fromisoformat(high),scope)[0]

    def test_session_card_metadata(self):
        from awb.archive_views import _add_session_metadata
        with self.db.tx() as c:
            c.executemany('INSERT INTO archive_devices(id,name,os,last_seen) VALUES(?,?,?,?)',
                          [('a', 'Mini', 'Darwin', ''), ('b', 'Air', 'Darwin', '')])
        raw_archive.bind_device_alias(self.db, 'a-old', 'a', evidence='fixture alias')
        self.put('same', 'r1', {'request_id':'r1','input_tokens':10,'cached_input_tokens':4,'output_tokens':3})
        self.put('same', 'r1', {'request_id':'r1','input_tokens':10,'cached_input_tokens':4,'output_tokens':3}, device='a-old')
        self.put('same', 'r2', {'request_id':'r2','input_tokens':5,'output_tokens':1}, at='2026-10-31T10:00:00Z')
        self.put('same', 'r3', {'request_id':'r3','input_tokens':30,'output_tokens':2}, device='b')
        self.put('zero', 'z', {'request_id':'z','input_tokens':0,'output_tokens':0})
        self.put('unallocated', 'u', {'raw_archive_version':1,'quantity_semantics':'cumulative_snapshot',
                 'counter_id':'c','epoch_id':'e','usage_key':'codex_input','total_tokens':100})
        self.assertTrue(self.worker.step())
        rows=[{'device_id':device,'agent':'codex','native_id':native} for device,native in
              [('a','same'), ('b','same'), ('a','zero'), ('other','missing'), ('a','unallocated')]]
        _add_session_metadata(self.db, rows)
        self.assertEqual([(r['device_name'],r['session_total_tokens'],r['session_tokens_partial']) for r in rows],
                         [('Mini',19,False),('Air',32,False),('Mini',0,False),(None,None,False),('Mini',None,True)])

    def test_incremental_revisions_copies_and_counter_predecessor(self):
        exact={'request_id':'same','model':'test','input_tokens':10,'cached_input_tokens':2,'output_tokens':3,'precision':'request'}
        self.put('copied','same',exact,'a');self.put('copied','same',exact,'b')
        for seq,amount in [(1,100),(2,180),(3,250)]:
            self.put('counter',str(seq),{'raw_archive_version':1,'quantity_semantics':'cumulative_snapshot',
                'counter_id':'c','epoch_id':'e','usage_key':'codex_input','model':'test',
                'source_order':seq,'total_tokens':amount,'source_time':f'2026-11-15T1{seq}:00:00Z'})
        self.put('other','unrelated',{'request_id':'other','model':'test','input_tokens':4,'output_tokens':1})
        self.assertTrue(self.worker.step())
        self.assertEqual(len([r for r in self.rows() if r['native_id']=='copied']),1)
        self.assertEqual(len([r for r in self.rows('a') if r['native_id']=='copied']),1)
        before=self.worker.status()['revision']
        self.put('counter','2',{'raw_archive_version':1,'quantity_semantics':'cumulative_snapshot',
            'counter_id':'c','epoch_id':'e','usage_key':'codex_input','model':'test',
            'source_order':2,'total_tokens':200,'source_time':'2026-11-15T12:00:00Z'})
        self.assertTrue(self.worker.step())
        status=self.worker.status()
        self.assertEqual(status['revision'],before+1)
        self.assertEqual(status['last_run']['groups'],1)
        self.assertEqual(status['last_run']['input_rows'],3)
        self.assertFalse(status['last_run']['full_rebuild'])
        self.assertFalse(self.worker.step())
        self.assertEqual(self.worker.status()['revision'],status['revision'])
        with self.db.tx() as c:
            c.execute("DELETE FROM archive_facts WHERE native_id='copied' AND device_id='a'")
        self.worker.step()
        self.assertEqual(len([r for r in self.rows() if r['native_id']=='copied']),1)
        self.assertEqual(len([r for r in self.rows('a') if r['native_id']=='copied']),0)
        from awb.usage_projection import load_source
        with self.db.read() as c:
            c.execute('BEGIN');source=load_source(c,None)[0]
        expected=raw_archive._copied_usage(raw_archive._usage(source))
        canonical=lambda rows:sorted(json.dumps(r,sort_keys=True) for r in rows)
        self.assertEqual(canonical(self.rows()),canonical(expected))

    def test_snapshot_is_readonly_and_failed_build_keeps_published_result(self):
        self.put('chat','one',{'request_id':'one','model':'test','input_tokens':100,'output_tokens':20})
        self.worker.step();before=self.worker.status()
        self.put('chat','one',{'request_id':'one','model':'test','input_tokens':130,'output_tokens':20})
        self.assertEqual(self.rows()[0]['input_tokens'],100)
        with patch.object(self.worker,'_insert',side_effect=RuntimeError('synthetic interrupted build')):
            with self.assertRaises(RuntimeError):self.worker.step(force=True)
        self.assertEqual(self.rows()[0]['input_tokens'],100)
        self.assertEqual(self.worker.status()['projected_seq'],before['projected_seq'])
        self.worker.step()
        self.assertEqual(self.rows()[0]['input_tokens'],130)
        with self.worker.snapshot() as s:
            with self.assertRaises(Exception):s.conn.execute('DELETE FROM usage_rows')
        with patch.object(self.worker,'step',side_effect=AssertionError('GET must not build')):
            self.assertEqual(self.rows()[0]['input_tokens'],130)

    def test_hermes_boundaries_and_alias_rebuild(self):
        at=lambda s:datetime.fromisoformat(s).timestamp()
        self.put('hermes','h',{'model':'test','first_seen':at('2026-11-15T23:50:00+00:00'),
            'last_seen':at('2026-11-16T00:10:00+00:00'),'input_tokens':10000,'output_tokens':0},agent='hermes')
        self.worker.step()
        from awb.archive_views import _usage_point
        low=datetime.fromisoformat('2026-11-15T00:00:00+00:00');high=datetime.fromisoformat('2026-11-16T00:00:00+00:00')
        self.assertIsNone(_usage_point(self.rows()[0],low,high))
        raw_archive.bind_device_alias(self.db,'b','a',evidence='synthetic proven alias')
        self.worker.step()
        self.assertTrue(self.worker.status()['last_run']['full_rebuild'])

    def test_overview_uses_one_read_snapshot_without_legacy_maintenance(self):
        from awb.api import create_app
        from fastapi.testclient import TestClient
        self.put('chat','one',{'request_id':'one','model':'test','input_tokens':100,'output_tokens':20})
        self.worker.step()
        app=create_app(self.db.path)
        before=self.worker.status()['revision']
        with patch('awb.query_model._Model.ensure_usage',side_effect=AssertionError('legacy maintenance called')):
            with patch.object(self.worker,'step',side_effect=AssertionError('GET started a build')):
                response=TestClient(app).get('/v1/mvp/overview',params={'day':'2026-11-15','tz':'UTC'})
        self.assertEqual(response.status_code,200,response.text)
        value=response.json()
        self.assertEqual(value['usage']['summary']['total_tokens'],120)
        self.assertEqual(value['report']['models'][0]['total_tokens'],120)
        self.assertEqual(value['usage']['projection']['revision'],value['projection']['revision'])
        self.assertEqual(self.worker.status()['revision'],before)

    def test_overview_reuses_candidate_query_and_busy_is_not_500(self):
        from awb.usage_projection import use_snapshot
        from awb import archive_views
        from awb.api import create_app
        from fastapi.testclient import TestClient
        import sqlite3
        self.put('chat','one',{'request_id':'one','model':'test','input_tokens':100,'output_tokens':20})
        self.worker.step()
        sql=[]
        with use_snapshot(self.db) as s:
            s.conn.set_trace_callback(sql.append)
            archive_views.usage(self.db,'2026-11-15','2026-11-15','UTC')
            archive_views.model_report(self.db,'2026-11-15','2026-11-15','UTC')
        self.assertEqual(sum(x.startswith('SELECT payload_json FROM usage_rows') for x in sql),1)
        app=create_app(self.db.path)
        with patch('awb.archive_views.usage',side_effect=sqlite3.OperationalError('interrupted')):
            response=TestClient(app).get('/v1/mvp/overview',params={'day':'2026-11-15','tz':'UTC'})
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.json()['code'],'query_budget_exceeded')

    def test_session_scope_registration_does_not_take_a_request_write_lock(self):
        from awb.session_catalog import SessionCatalog, _connect
        from contextlib import closing
        catalog=SessionCatalog(self.db)
        with closing(_connect(catalog.path)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            result=catalog.query('2026-11-15','2026-11-15','UTC',None,
                agent=None,query='',limit=100,offset=0)
            self.assertEqual(result['projection']['state'],'building')
            writer.execute('ROLLBACK')


if __name__=='__main__':unittest.main()
