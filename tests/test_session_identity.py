"""Focused synthetic regressions; never open native user stores."""
import json
import unittest
import tempfile
from pathlib import Path
from fastapi.testclient import TestClient
from awb.api import create_app
from awb.projector import project
from awb.auth import token_hash

from awb import archive, archive_views, raw_archive
from awb.collector import make_event
def source(db):
    device, sid, at = 'synthetic-device', 'synthetic-source', '2026-11-05T01:00:00Z'
    with db.tx() as c:
        c.execute('INSERT INTO devices(id,name,os,environment,token_hash,registered_at) VALUES(?,?,?,?,?,?)',
                  (device, 'synthetic', 'Darwin', 'test', token_hash('synthetic-token'), at))
        c.execute('INSERT INTO sources(id,device_id,agent,profile,created_at) VALUES(?,?,?,?,?)',
                  (sid, device, 'hermes', 'default', at))
    return device, sid, None


def seed_raw(db, events):
    with db.tx() as c:
        for ev in events:
            v = ev['content']
            c.execute('''INSERT INTO events(event_id,body_hash,source_id,native_session_id,fact_kind,
                native_fact_id,revision_key,source_order,occurred_at,content_json,received_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''', (ev['event_id'], ev['body_hash'], v['source_instance_id'],
                v['native_session_id'], v['fact_kind'], v['native_fact_id'], v['revision_key'],
                v['source_order'], v['occurred_at'], json.dumps(v), '2026-11-05T01:00:00Z'))
            project(c, ev)


def _check_lineage_bridge_usage_and_browser(rig):
    _, db, client = rig
    device, sid, _ = source(db)
    specs = [('chat', 'desktop', None), ('child', 'desktop', 'chat'),
             ('batch', 'tool', None), ('probe', 'oneshot', None), ('unknown', None, None)]
    events = []
    at = '2026-11-05T01:00:00Z'
    for native, origin, parent in specs:
        payload = {'created_at': at, 'title': native, 'native': {'native_json': json.dumps(
            {'source': origin, 'parent_session_id': parent})}}
        events.append(make_event(sid, native, 'session.observed', 'session', '1', 0, at, payload, device, None))
    seed_raw(db, events)
    raw_archive.project_raw(db)
    meta = {r['native_id']: r for r in raw_archive.facts(db, 'session')}
    assert meta['chat']['session_kind'] == 'interactive'
    assert meta['child']['session_kind'] == 'internal'
    assert meta['batch']['session_kind'] == 'internal'
    assert meta['unknown']['session_kind'] == 'unknown'
    response = client.get('/v1/mvp/session-counts', params={'day':'2026-11-05','tz':'UTC'})
    assert response.status_code == 200
    assert response.json()['created'] == {'interactive':1,'internal':3,'unknown':1,'total':5}
    assert response.json()['interactive_sources'] == {'desktop':1}
    assert client.get('/v1/mvp/session-counts',params={'day':'2026-11-06','tz':'UTC'}).json()['created']['total'] == 0
    with db.tx() as c:
        for native, _, _ in specs:
            archive._upsert_local(c, device, 'usage', 'hermes', native, 'usage', at,
                {'model': 'synthetic', 'precision': 'session_model_aggregate',
                 'first_seen': 1793840400, 'last_seen': 1793840401,
                 'input_tokens': 100, 'output_tokens': 10, 'api_call_count': 1})
    result = archive_views.usage(db, '2026-11-05', '2026-11-05', 'UTC')
    assert result['summary']['total_tokens'] == 550
    assert result['session_counts'] == {'interactive': 1, 'internal': 3, 'unknown': 1, 'total': 5}
    for scope, expected in [('conversation', {'chat', 'unknown'}), ('internal', {'child', 'batch', 'probe'}),
                            ('all', {s[0] for s in specs})]:
        # Cold/non-recent path must filter before pagination too.
        result = archive_views.browser(db, '2026-11-05', '2026-11-05', 'UTC',
            session_scope=scope, sort='oldest', limit=1)
        assert result['session_count'] == len(expected)
        assert result['sessions'][0]['native_id'] in expected
        result = client.get('/v1/mvp/activity/sessions', params={
            'day': '2026-11-05', 'tz': 'UTC', 'session_scope': scope, 'sort': 'oldest'}).json()
        assert {r['native_id'] for r in result['sessions']} == expected
    from awb.session_catalog import catalog, install
    install(db)
    recent = catalog(db)
    recent.register('2026-11-05', '2026-11-05', 'UTC', None)
    for _ in range(4):
        recent.step(max_keys=32)
    for scope, expected in [('conversation', 2), ('internal', 3), ('all', 5)]:
        pages = []
        for offset in range(expected):
            result = recent.query('2026-11-05', '2026-11-05', 'UTC', None,
                agent=None, query='', limit=1, offset=offset, session_scope=scope)
            assert result['session_count'] == expected
            pages += [r['native_id'] for r in result['sessions']]
        assert len(set(pages)) == expected


def _check_explicit_evidence_only():
    from awb.session_identity import identity
    assert identity('hermes', {'title': 'Subagent: a normal user title'})['session_kind'] == 'unknown'
    assert identity('hermes', {'native': {'native_json': json.dumps(
        {'source': 'desktop', 'parent_session_id': 'parent'})}})['session_kind'] == 'internal'
    assert identity('hermes', {'native': {'native_json': json.dumps(
        {'source': 'desktop'})}})['session_kind'] == 'interactive'
    assert identity('codex', {'native': {'native_json': json.dumps({'type': 'session_meta',
        'payload': {'source': {'subagent': {'thread_spawn': {'parent_thread_id': 'p'}}}}})}})['session_kind'] == 'internal'


class SessionIdentityTests(unittest.TestCase):
    def test_bridge_usage_and_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(Path(tmp) / 'synthetic.db')
            _check_lineage_bridge_usage_and_browser((app, app.state.db, TestClient(app)))

    def test_explicit_evidence_only(self):
        _check_explicit_evidence_only()


if __name__ == '__main__':
    unittest.main()
