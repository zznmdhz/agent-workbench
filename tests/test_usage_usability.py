"""Bounded regressions for native call counts and truthful heatmap attribution."""
import os,json,tempfile,unittest
from pathlib import Path
from datetime import datetime
from awb.db import Database
from awb import archive,raw_archive,archive_views
from awb.collector import make_event
from test_session_identity import source,seed_raw


class UsageUsabilityTests(unittest.TestCase):
    def test_native_counter_fields_survive_capture_bridge_and_day_heatmap(self):
        from awb.collector import hermes_usage_payload
        from awb.usage_projection import UsageProjection,use_snapshot
        with tempfile.TemporaryDirectory() as tmp:
            old=os.environ.get('AWB_QUERY_CACHE_DIR');os.environ['AWB_QUERY_CACHE_DIR']=str(Path(tmp)/'cache')
            try:
                db=Database(Path(tmp)/'synthetic.sqlite');db.initialize();raw_archive.initialize(db)
                device,sid,_=source(db)
                native={'model':'test','billing_provider':'synthetic','billing_base_url':None,'billing_mode':'test','task':'',
                        'input_tokens':100,'output_tokens':20,'cache_read_tokens':30,'cache_write_tokens':0,
                        'reasoning_tokens':0,'api_call_count':3,
                        'first_seen':datetime.fromisoformat('2026-11-15T01:00:00+00:00').timestamp(),
                        'last_seen':datetime.fromisoformat('2026-11-15T01:15:00+00:00').timestamp()}
                payload=hermes_usage_payload(native)
                self.assertEqual(payload['api_call_count'],3);self.assertEqual(payload['cache_write_tokens'],0)
                self.assertEqual(payload['first_seen'],'2026-11-15T01:00:00.000Z')
                event=make_event(sid,'synthetic-session','usage.observed',payload['usage_key'],'1',None,None,payload,device,None)
                seed_raw(db,[event]);raw_archive.project_raw(db)
                worker=UsageProjection(db);worker.prepare();worker.step()
                with use_snapshot(db):
                    result=archive_views.usage(db,'2026-11-15','2026-11-15','UTC',heatmap_view='day')
                    report=archive_views.model_report(db,'2026-11-15','2026-11-15','UTC')
                self.assertEqual(result['summary']['requests'],3)
                self.assertEqual(result['summary']['request_count_unknown_rows'],0)
                self.assertEqual(result['trend'][0]['total_tokens'],result['summary']['total_tokens'])
                hour=next(r for r in result['heatmap'] if r['period']=='2026-11-15T01')
                self.assertEqual(hour['requests'],3)
                self.assertEqual(hour['total_tokens'],result['summary']['total_tokens'])
                self.assertEqual(result['unattributed_tokens'],0)
                self.assertEqual(report['series'][0]['total_tokens'],result['summary']['total_tokens'])
                self.assertEqual(report['hours'][0]['requests'],3)
                self.assertEqual(report['points'],[])
            finally:
                if old is None:os.environ.pop('AWB_QUERY_CACHE_DIR',None)
                else:os.environ['AWB_QUERY_CACHE_DIR']=old

    def test_cross_hour_is_daily_only_and_missing_counter_remains_unknown(self):
        row={'agent':'hermes','first_seen':datetime.fromisoformat('2026-11-15T01:00:00+00:00').timestamp(),
             'last_seen':datetime.fromisoformat('2026-11-15T03:00:00+00:00').timestamp()}
        from zoneinfo import ZoneInfo
        zone=ZoneInfo('UTC')
        self.assertTrue(archive_views._bucket_attributed(row,zone,'day'))
        self.assertFalse(archive_views._bucket_attributed(row,zone,'hour'))
        self.assertTrue(archive_views._request_count_unknown(row))
        self.assertFalse(archive_views._request_count_unknown({**row,'api_call_count':0}))
        unknown={'agent':'hermes','range_first_seen':row['first_seen'],'range_last_seen':row['last_seen']}
        self.assertFalse(archive_views._bucket_attributed(unknown,zone,'day'))

    def test_cross_day_is_not_faked_into_a_day(self):
        from zoneinfo import ZoneInfo
        row={'agent':'hermes','first_seen':datetime.fromisoformat('2026-11-15T23:50:00+00:00').timestamp(),
             'last_seen':datetime.fromisoformat('2026-11-16T00:10:00+00:00').timestamp()}
        self.assertFalse(archive_views._bucket_attributed(row,ZoneInfo('UTC'),'day'))
        self.assertTrue(archive_views._bucket_attributed(row,ZoneInfo('UTC'),'month'))

    def test_realtime_does_not_overwrite_native_counters_with_unknown(self):
        import sqlite3
        from awb.collector import Outbox
        from awb.realtime import scan
        with tempfile.TemporaryDirectory() as tmp:
            native=Path(tmp)/'native.sqlite'
            with sqlite3.connect(native) as c:
                c.executescript('''CREATE TABLE sessions(id TEXT,title TEXT,cwd TEXT,started_at REAL,source TEXT);
                    CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT,timestamp REAL);
                    CREATE TABLE session_model_usage(session_id TEXT,model TEXT,billing_provider TEXT,billing_base_url TEXT,
                    billing_mode TEXT,task TEXT,input_tokens INTEGER,output_tokens INTEGER,cache_read_tokens INTEGER,
                    reasoning_tokens INTEGER,api_call_count INTEGER,cache_write_tokens INTEGER,first_seen REAL,last_seen REAL);''')
                c.execute("INSERT INTO sessions VALUES('s','synthetic',NULL,1,'desktop')")
                c.execute("INSERT INTO messages VALUES(1,'s','user','synthetic',1)")
                c.execute("INSERT INTO session_model_usage VALUES('s','test','test',NULL,'test','',100,20,30,0,3,0,1,2)")
            outbox=Outbox(Path(tmp)/'outbox.sqlite')
            outbox.append('synthetic-source','realtime-messages-v1',0,'synthetic',None,[])
            config={'id':'synthetic-source','agent':'hermes','root':str(native),'content_policy':'stats_only'}
            scan(config,outbox,'synthetic-device')
            with outbox.connect() as c:
                events=[json.loads(r[0]) for r in c.execute('SELECT event_json FROM outbox')]
            payload=next(e['content']['payload'] for e in events if e['content']['fact_kind']=='usage.observed')
            self.assertEqual(payload['api_call_count'],3)
            self.assertIsNotNone(payload['first_seen'])
            self.assertEqual(payload['cache_write_tokens'],0)
            self.assertEqual(scan(config,outbox,'synthetic-device')['queued'],0)


if __name__=='__main__':unittest.main()
