"""Bridge regressions. Every DB and exchange directory is a pytest tmp_path.

Fixtures are synthetic and deliberately named; no native Agent files are opened.
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from awb import archive, archive_views, raw_archive
from awb.api import create_app
from awb.auth import token_hash
from awb.collector import make_event
from awb.codec import utc_ms
from awb.ingest import receive_batch
from awb.models import Batch
from awb.projector import project

AT = '2026-11-05T01:00:00Z'


@pytest.fixture
def rig(tmp_path):
    app = create_app(tmp_path / 'synthetic-bridge.db')
    return app, app.state.db, TestClient(app)


def source(db, agent='hermes', profile='default', device=None, policy='full_content'):
    device = device or str(uuid4())
    sid, token = str(uuid4()), str(uuid4())
    with db.tx() as conn:
        conn.execute('''INSERT OR IGNORE INTO devices(id,name,os,environment,token_hash,registered_at)
            VALUES(?,?,?,?,?,?)''', (device, 'SYNTHETIC same hostname', 'Darwin', 'synthetic', token_hash(token), AT))
        conn.execute('''INSERT INTO sources(id,device_id,agent,profile,capability_json,created_at)
            VALUES(?,?,?,?,?,?)''', (sid, device, agent, profile, json.dumps({'content_policy': policy}), AT))
    return device, sid, token


def message(device, sid, *, native='synthetic-session', mid='1', body='synthetic body', at=AT, order=1, revision='1'):
    return make_event(sid, native, 'message.observed', mid, revision, order, at,
                      {'native_message_id': mid, 'role': 'user', 'input_origin': 'human',
                       'body': body, 'content_state': 'full' if body is not None else 'stats_only',
                       'omission_reason': None if body is not None else 'source_policy',
                       'source_text_char_count': len(body) if body else 500, 'finalized': True}, device, None)


def usage(device, sid, payload, *, fid='synthetic-usage', at=AT, order=1, revision='1'):
    return make_event(sid, 'synthetic-session', 'usage.observed', fid, revision, order, at, payload, device, None)


def batch(device, events, epoch=None, first=1):
    return Batch.model_validate({'batch_id': str(uuid4()), 'collector_id': device,
        'outbox_epoch': epoch or str(uuid4()), 'entries': [
            {'seq': first + i, 'observation': {'observed_at': AT, 'adapter_version': 'synthetic-test',
             'source_locator': {}}, 'event': ev} for i, ev in enumerate(events)]})


def seed_raw(db, events):
    """Historical ingest state, intentionally bypassing the new bridge."""
    with db.tx() as conn:
        for ev in events:
            c = ev['content']
            conn.execute('''INSERT INTO events(event_id,body_hash,source_id,native_session_id,fact_kind,
                native_fact_id,revision_key,source_order,occurred_at,content_json,received_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''', (ev['event_id'], ev['body_hash'], c['source_instance_id'],
                c['native_session_id'], c['fact_kind'], c['native_fact_id'], c['revision_key'],
                c['source_order'], c['occurred_at'], json.dumps(c), AT))
            project(conn, ev)


def snapshot(db, table):
    with db.read() as conn:
        return [tuple(r) for r in conn.execute('SELECT * FROM ' + table + ' ORDER BY 1')]


def get_usage(db, device=None, day='2026-11-05', through=None):
    return archive_views.usage(db, day, through or day, 'UTC', device_id=device)


def test_backfill_idempotent_preserves_raw_and_existing_facts(rig):
    _, db, _ = rig
    device, sid, _ = source(db)
    seed_raw(db, [message(device, sid)])
    with db.tx() as conn:
        archive._upsert_local(conn, device, 'message', 'hermes', 'synthetic-other', 'original', AT,
                              {'message_id': 'original', 'body': 'preserve me', 'role': 'assistant'})
    before_raw = {t: snapshot(db, t) for t in ['events', 'sessions', 'messages', 'usage_observations']}
    before_fact = snapshot(db, 'archive_facts')[0]
    first = raw_archive.project_raw(db)
    assert first == {'sessions': 1, 'changed_facts': 2}
    log = snapshot(db, 'archive_log')
    assert raw_archive.project_raw(db)['changed_facts'] == 0
    assert snapshot(db, 'archive_log') == log
    assert before_fact in snapshot(db, 'archive_facts')
    assert before_raw == {t: snapshot(db, t) for t in before_raw}
    assert raw_archive.project_raw(db, [])['sessions'] == 0


@pytest.mark.parametrize('agent,profile,policy', [('hermes', 'default', 'full_content'),
    ('hermes', 'worker', 'full_content'), ('codex', 'default', 'stats_only')])
def test_real_http_ingest_visible_in_mvp_and_detail_without_cookie(rig, monkeypatch, agent, profile, policy):
    _, db, client = rig
    device, sid, token = source(db, agent, profile, policy=policy)
    monkeypatch.setattr(archive, 'refresh', lambda *a, **k: pytest.fail('GET must not scan native stores'))
    ev = message(device, sid, body=None if policy == 'stats_only' else 'HTTP synthetic hello')
    b = batch(device, [ev])
    response = client.post('/v1/ingest/batches', json=b.model_dump(mode='json'), headers={'authorization': 'Bearer ' + token})
    assert response.status_code == 200, response.text
    assert response.json()['projected_ack_seq'] == 1
    assert response.json()['receipts'][0]['status'] == 'accepted'
    items = client.get('/v1/mvp/activity/sessions', params={'day': '2026-11-05', 'tz': 'UTC', 'device_id': device}).json()
    assert items['session_count'] == 1
    item = items['sessions'][0]
    assert item['source_id'] == sid and item['profile'] == profile
    detail = client.get(f"/v1/mvp/activity/sessions/{agent}/{item['native_id']}/conversation", params={'device_id': device})
    assert detail.status_code == 200, detail.text
    row = detail.json()['items'][0]
    assert row['body'] == ('' if policy == 'stats_only' else 'HTTP synthetic hello')
    assert row['content_state'] == ('stats_only' if policy == 'stats_only' else 'full')
    assert row['omission_reason'] == ('source_policy' if policy == 'stats_only' else None)
    assert not client.cookies
    retry = client.post('/v1/ingest/batches', json=b.model_dump(mode='json'), headers={'authorization': 'Bearer ' + token})
    assert retry.status_code == 200 and retry.json()['projected_ack_seq'] == 1
    assert len(raw_archive.facts(db, 'message')) == 1


def test_empty_read_never_collects_or_writes_archive(rig, monkeypatch):
    _, db, client = rig
    monkeypatch.setattr(archive, 'refresh', lambda *a, **k: pytest.fail('unexpected scan'))
    before = snapshot(db, 'archive_facts')
    for path in ['/v1/mvp/usage', '/v1/mvp/activity', '/v1/mvp/activity/sessions']:
        response = client.get(path, params={'day': '2026-11-05'})
        assert response.status_code == 200, response.text
    assert snapshot(db, 'archive_facts') == before


def test_bridge_failure_rolls_back_raw_and_ack(rig, monkeypatch):
    _, db, _ = rig
    device, sid, _ = source(db)
    def fail(*a, **k):
        raise RuntimeError('synthetic bridge failure')
    monkeypatch.setattr(raw_archive, 'project_raw_conn', fail)
    with pytest.raises(RuntimeError, match='synthetic bridge failure'):
        receive_batch(db, device, batch(device, [message(device, sid)]))
    for table in ['events', 'messages', 'deliveries', 'ingest_batches', 'streams', 'archive_facts']:
        assert snapshot(db, table) == []


def test_multi_profile_native_collision_and_distinct_devices_not_hostname_merged(rig):
    _, db, client = rig
    device, default, _ = source(db)
    _, worker, _ = source(db, profile='worker', device=device)
    other, other_sid, _ = source(db)
    for dev, sid in [(device, default), (device, worker), (other, other_sid)]:
        receive_batch(db, dev, batch(dev, [message(dev, sid)]))
    rows = raw_archive.facts(db, 'message')
    assert len(rows) == 3
    assert len({(r['device_id'], r['native_id']) for r in rows}) == 3
    browser = client.get('/v1/mvp/activity/sessions', params={'day': '2026-11-05', 'tz': 'UTC'}).json()
    assert browser['session_count'] == 3
    assert len({r['profile'] for r in browser['sessions']}) == 2
    filtered = client.get('/v1/mvp/activity/sessions', params={
        'day': '2026-11-05', 'tz': 'UTC', 'source_id': worker, 'profile': 'worker'}).json()
    assert filtered['session_count'] == 1
    assert filtered['sessions'][0]['source_id'] == worker


def aggregate(**overrides):
    return {'usage_key': 'model-task', 'quantity_semantics': 'aggregate', 'coverage_scope': 'session_model',
            'model': 'synthetic-model', 'task': '', 'input_tokens': 100, 'output_tokens': 20,
            'cached_input_tokens': 30, 'cache_write_tokens': 5,
            'first_seen': int(utc_ms(AT)/1000), 'last_seen': int(utc_ms(AT)/1000),
            'api_call_count': 2, **overrides}


def test_hermes_aggregate_update_and_reset_replace_not_sum(rig):
    _, db, _ = rig
    device, sid, _ = source(db)
    for revision, value in [('1', 100), ('2', 150), ('3', 12)]:
        receive_batch(db, device, batch(device, [usage(device, sid, aggregate(input_tokens=value), revision=revision)]))
        got = get_usage(db)
        assert got['summary']['total_tokens'] == value + 30 + 5 + 20
        assert got['summary']['requests'] == 2
        assert sum(x['total_tokens'] for x in got['trend']) == 0
    rows = raw_archive.facts(db, 'usage')
    assert len(rows) == 1 and rows[0]['input_tokens'] == 12
    assert len(snapshot(db, 'usage_observations')) == 3
    assert raw_archive.project_raw(db)['changed_facts'] == 0


def test_actual_old_air_aggregate_missing_bounds_and_calls_remains_honest(rig):
    _, db, _ = rig
    device, sid, _ = source(db, profile='worker')
    p = aggregate()
    for key in ['first_seen', 'last_seen', 'api_call_count', 'cache_write_tokens']:
        p.pop(key)
    receive_batch(db, device, batch(device, [message(device, sid), usage(device, sid, p, at=None, order=None)]))
    got = get_usage(db)
    assert got['summary']['total_tokens'] == 150
    assert got['summary']['requests'] == 0
    assert got['sources']['hermes']['unknown_call_count_rows'] == 1
    row = raw_archive.facts(db, 'usage')[0]
    assert row['time_basis'] == 'session_bounds' and row['api_call_count'] is None
    assert row['first_seen'] is None and row['last_seen'] is None
    assert row['cache_write_tokens'] is None


def test_hermes_cross_day_aggregate_not_split_and_bounds_api_count_preserved(rig):
    _, db, _ = rig
    device, sid, _ = source(db)
    p = aggregate(first_seen=int(utc_ms('2026-11-04T23:00:00Z')/1000), api_call_count=9)
    receive_batch(db, device, batch(device, [usage(device, sid, p)]))
    assert get_usage(db)['summary']['total_tokens'] == 0
    got = get_usage(db, day='2026-11-04', through='2026-11-05')
    assert got['summary']['total_tokens'] == 155 and got['summary']['requests'] == 9
    assert sum(x['total_tokens'] for x in got['trend']) == 0


def counters(device, sid, index, values, epoch='e1', at=None):
    events = []
    for key, value in zip(['codex_input', 'codex_output', 'codex_cached_input', 'codex_total'], values):
        events.append(usage(device, sid, {'usage_key': key, 'quantity_semantics': 'cumulative_snapshot',
            'coverage_scope': 'session', 'counter_id': 'counter', 'epoch_id': epoch,
            'source_time': at or f'2026-11-05T01:{index:02d}:00Z', 'total_tokens': value},
            fid=f'{epoch}:{key}:{index}', order=index, at=at or f'2026-11-05T01:{index:02d}:00Z'))
    return events


def test_cumulative_baseline_repeat_growth_regression_and_explicit_epoch_reset(rig):
    _, db, _ = rig
    device, sid, _ = source(db, agent='codex')
    for i, amounts in enumerate([(100, 20, 40, 120), (100, 20, 40, 120), (140, 30, 50, 170),
                                 (5, 2, 1, 7), (20, 3, 2, 23)]):
        receive_batch(db, device, batch(device, counters(device, sid, i, amounts)))
    got = get_usage(db)
    assert got['summary']['total_tokens'] == 50
    assert got['summary']['cached_input_tokens'] == 10
    assert got['summary']['requests'] == 0
    receive_batch(db, device, batch(device, counters(device, sid, 5, (0, 0, 0, 0), 'e2')))
    receive_batch(db, device, batch(device, counters(device, sid, 6, (10, 2, 3, 12), 'e2')))
    assert get_usage(db)['summary']['total_tokens'] == 62
    reasons = {r.get('omission_reason') for r in raw_archive.facts(db, 'usage')}
    assert {'opening_balance', 'counter_regression', 'counter_epoch_untrusted'} <= reasons
    assert raw_archive.project_raw(db)['changed_facts'] == 0


def test_counter_cross_day_interval_not_assigned_to_one_day(rig):
    _, db, _ = rig
    device, sid, _ = source(db, agent='codex')
    receive_batch(db, device, batch(device, counters(device, sid, 1, (100, 20, 0, 120), at='2026-11-04T23:59:00Z')))
    receive_batch(db, device, batch(device, counters(device, sid, 2, (150, 30, 0, 180), at='2026-11-05T00:01:00Z')))
    assert get_usage(db)['summary']['total_tokens'] == 0
    got = get_usage(db, day='2026-11-04', through='2026-11-05')
    assert got['summary']['total_tokens'] == 60
    assert sum(x['total_tokens'] for x in got['trend']) == 0


def test_explicit_archive_mapping_dedupes_native_messages_and_usage_preserving_history(rig):
    _, db, _ = rig
    device, sid, _ = source(db)
    historical = str(uuid4())
    raw_archive.bind_source(db, sid, historical, evidence='synthetic audited source identity')
    with db.tx() as conn:
        archive._upsert_local(conn, historical, 'message', 'hermes', 'synthetic-session', '1', AT,
            {'message_id': '1', 'role': 'user', 'body': 'synthetic body', 'source_offset': 0})
        archive._upsert_local(conn, historical, 'usage', 'hermes', 'synthetic-session', 'synthetic-model:', AT,
            {**aggregate(), 'cache_read_tokens': 30})
    originals = snapshot(db, 'archive_facts')
    receive_batch(db, device, batch(device, [message(device, sid), usage(device, sid, aggregate(input_tokens=200))]))
    assert all(row in snapshot(db, 'archive_facts') for row in originals)
    assert len(raw_archive.facts(db, 'message')) == 1
    assert get_usage(db)['summary']['total_tokens'] == 255
    assert len(raw_archive.facts(db, 'usage')) == 1
    with pytest.raises(ValueError, match='already bound'):
        raw_archive.bind_source(db, sid, str(uuid4()), evidence='incompatible synthetic rebind')


def test_proven_device_alias_dedupes_but_unbound_devices_do_not(rig):
    _, db, _ = rig
    canonical, alias = str(uuid4()), str(uuid4())
    with db.tx() as conn:
        for device in [canonical, alias]:
            archive._upsert_local(conn, device, 'message', 'hermes', 'synthetic-session', '1', AT,
                                 {'message_id': '1', 'role': 'user', 'body': 'same native copy'})
    assert len(raw_archive.facts(db, 'message')) == 2
    raw_archive.bind_device_alias(db, alias, canonical, evidence='synthetic verified copy of same native store')
    assert len(raw_archive.facts(db, 'message')) == 1
    assert len(raw_archive.facts(db, 'message', alias)) == 1
    assert len(snapshot(db, 'archive_facts')) == 2


def test_stats_only_does_not_erase_existing_authorized_body_and_revisions_update(rig):
    _, db, _ = rig
    device, sid, _ = source(db)
    receive_batch(db, device, batch(device, [message(device, sid)]))
    receive_batch(db, device, batch(device, [message(device, sid, body=None, order=2, revision='2')]))
    assert raw_archive.facts(db, 'message')[0]['body'] == 'synthetic body'
    receive_batch(db, device, batch(device, [message(device, sid, body='new body', order=3, revision='3')]))
    assert raw_archive.facts(db, 'message')[0]['body'] == 'new body'


def test_codex_archived_requests_and_raw_counters_not_added_twice(rig):
    _, db, _ = rig
    device, sid, _ = source(db, agent='codex')
    at = '2026-11-05T01:02:00Z'
    with db.tx() as conn:
        archive._upsert_local(conn, device, 'usage', 'codex', 'synthetic-session', 'native-request', at,
            {'request_id': 'native-request', 'model': 'unknown', 'input_tokens': 40,
             'cached_input_tokens': 10, 'output_tokens': 10})
    receive_batch(db, device, batch(device, counters(device, sid, 1, (100, 20, 40, 120))))
    receive_batch(db, device, batch(device, counters(device, sid, 2, (140, 30, 50, 170))))
    assert get_usage(db)['summary']['total_tokens'] == 50
    receive_batch(db, device, batch(device, counters(device, sid, 3, (160, 35, 55, 195))))
    assert get_usage(db)['summary']['total_tokens'] == 75


def test_incomplete_run_does_not_crash_browser_and_child_not_verified(rig):
    _, db, client = rig
    device, sid, _ = source(db, agent='codex')
    def run(turn, start, end=None, parent=None):
        return make_event(sid, 'synthetic-session', 'run.observed', turn, '1', 1, end or start,
            {'native_turn_id': turn, 'status': 'completed' if end else 'running',
             'start_at': start, 'end_at': end, 'parent_run_ref': parent}, device, None)
    events = [run('native-incomplete', AT),
        run('native-parent', '2026-11-05T02:00:00Z', '2026-11-05T02:05:00Z'),
        run('native-child', '2026-11-05T02:01:00Z', '2026-11-05T02:04:00Z', 'native-parent')]
    receive_batch(db, device, batch(device, events))
    response = client.get('/v1/mvp/activity/sessions', params={'day': '2026-11-05', 'tz': 'UTC'})
    assert response.status_code == 200, response.text
    activity = archive_views.activity(db, '2026-11-05', '2026-11-05', 'UTC')
    assert activity['summary']['verified_ms'] == 300000


def test_two_proven_sources_for_same_native_copy_remain_idempotent(rig):
    _, db, _ = rig
    device1, sid1, _ = source(db)
    device2, sid2, _ = source(db)
    canonical = str(uuid4())
    for sid in [sid1, sid2]:
        raw_archive.bind_source(db, sid, canonical, evidence='synthetic verified native store copy')
    for device, sid in [(device1, sid1), (device2, sid2)]:
        seed_raw(db, [message(device, sid), usage(device, sid, aggregate())])
    assert raw_archive.project_raw(db)['changed_facts'] == 6
    assert raw_archive.project_raw(db)['changed_facts'] == 0
    assert len(raw_archive.facts(db, 'message')) == 1
    assert get_usage(db)['summary']['total_tokens'] == 155


def test_codex_cross_parser_offset_identity_not_extra_message(rig):
    _, db, _ = rig
    device, sid, _ = source(db, agent='codex')
    with db.tx() as conn:
        archive._upsert_local(conn, device, 'message', 'codex', 'synthetic-session', 'native-msg-uuid', AT,
            {'message_id': 'native-msg-uuid', 'source_offset': 512, 'role': 'user', 'body': 'authorized legacy'})
    receive_batch(db, device, batch(device, [message(device, sid, mid='512', order=512, body=None)]))
    rows = raw_archive.facts(db, 'message')
    assert len(rows) == 1 and rows[0]['body'] == 'authorized legacy'


def test_tombstoned_session_never_reappears(rig):
    _, db, _ = rig
    device, sid, _ = source(db)
    with db.tx() as conn:
        conn.execute('INSERT INTO tombstones VALUES(?,?,?,?)', (sid, 'synthetic-session', AT, 1))
    receipt = receive_batch(db, device, batch(device, [message(device, sid)]))
    assert receipt['receipts'][0]['status'] == 'ignored_tombstoned'
    assert raw_archive.facts(db, 'message') == []
    assert raw_archive.project_raw(db)['changed_facts'] == 0


def test_session_only_metadata_is_browsable_without_fabricating_messages(rig):
    _, db, client = rig
    device, sid, _ = source(db, profile='worker')
    ev = make_event(sid, 'metadata-only', 'session.observed', 'metadata-only', '1', 1, AT,
                    {'title': 'Synthetic session metadata', 'created_at': AT}, device, None)
    receive_batch(db, device, batch(device, [ev]))
    response = client.get('/v1/mvp/activity/sessions', params={'day': '2026-11-05', 'tz': 'UTC'})
    assert response.status_code == 200, response.text
    assert response.json()['session_count'] == 1
    item = response.json()['sessions'][0]
    assert item['messages'] == 0 and item['agent_ms'] == 0
    assert item['title'] == 'Synthetic session metadata'
    detail = client.get(f"/v1/mvp/activity/sessions/hermes/{item['native_id']}/conversation", params={'device_id': device})
    assert detail.json()['count'] == 0


def test_counter_copies_with_different_paths_not_double_added(rig):
    _, db, _ = rig
    canonical = str(uuid4())
    for copy in range(2):
        device, sid, _ = source(db, agent='codex')
        raw_archive.bind_source(db, sid, canonical, evidence='synthetic manifest proves identical native rollout')
        events = []
        for i, values in [(1, (100, 20, 40, 120)), (2, (140, 30, 50, 170))]:
            for ev in counters(device, sid, i, values, epoch='path-'+str(copy)):
                events.append(ev)
        seed_raw(db, events)
    raw_archive.project_raw(db)
    assert get_usage(db)['summary']['total_tokens'] == 50
    assert raw_archive.project_raw(db)['changed_facts'] == 0
