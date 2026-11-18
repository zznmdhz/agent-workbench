"""Small synthetic work-count regressions; not a timing benchmark."""
from collections import Counter
import json

from awb import archive, raw_archive
from awb.collector import make_event
from awb.ingest import receive_batch
from test_raw_archive import rig, source, message, usage, aggregate, batch, snapshot, seed_raw, AT


def test_existing_session_refresh_only_changed_facts(rig, monkeypatch):
    _, db, _ = rig
    device, sid, _ = source(db)
    events = [message(device, sid, mid=str(i), order=i) for i in range(12)]
    for i in range(2):
        events.append(make_event(sid, 'synthetic-session', 'run.observed', f'r{i}', '1', i, AT,
            {'native_turn_id': f'r{i}', 'status': 'completed', 'start_at': AT, 'end_at': AT}, device, None))
        events.append(make_event(sid, 'synthetic-session', 'file.observed', f'f{i}', '1', i, AT,
            {'native_path': f'/synthetic/{i}', 'relation': 'output'}, device, None))
    events.append(usage(device, sid, aggregate()))
    receive_batch(db, device, batch(device, events))
    old = snapshot(db, 'archive_facts')
    calls, writes, reads = Counter(), Counter(), Counter()
    real_bridge = raw_archive.project_raw_conn
    class Cursor:
        def __init__(self, cursor, kind):
            self.cursor, self.kind = cursor, kind
        def fetchall(self):
            rows = self.cursor.fetchall()
            reads[self.kind] += len(rows)
            return rows
        def __iter__(self):
            for row in self.cursor:
                reads[self.kind] += 1
                yield row
    class Connection:
        def __init__(self, conn):
            self.conn = conn
        def execute(self, sql, params=()):
            cursor = self.conn.execute(sql, params)
            for marker, kind in [('SELECT m.*', 'message'), ('SELECT r.*', 'run'),
                                 ('SELECT * FROM runs WHERE', 'run'), ('SELECT f.*', 'file'), ('SELECT u.*', 'usage')]:
                if sql.lstrip().startswith(marker):
                    return Cursor(cursor, kind)
            return cursor
    def measured(conn, **kwargs):
        return real_bridge(Connection(conn), **kwargs)
    monkeypatch.setattr(raw_archive, 'project_raw_conn', measured)
    real = archive._upsert_local
    def counted(conn, device, kind, *args):
        calls[kind] += 1
        changed = real(conn, device, kind, *args)
        writes[kind] += changed
        return changed
    monkeypatch.setattr(archive, '_upsert_local', counted)
    receive_batch(db, device, batch(device, [message(device, sid, mid='5', order=5, revision='2', body='revised')]))
    print('revision_fact_attempts=' + json.dumps(dict(calls), sort_keys=True))
    print('revision_rows_read=' + json.dumps(dict(reads), sort_keys=True))
    print('revision_facts_changed=' + json.dumps(dict(writes), sort_keys=True))
    assert calls == Counter(session=1, message=1)
    assert reads == Counter(message=1)
    assert len(snapshot(db, 'archive_facts')) == len(old)
    assert next(r for r in raw_archive.facts(db, 'message') if r['message_id'] == '5')['body'] == 'revised'
    assert raw_archive.project_raw(db)['changed_facts'] == 0


def test_legacy_bounds_and_replay_and_backfill_remain_equivalent(rig, monkeypatch):
    _, db, _ = rig
    device, sid, _ = source(db)
    p = aggregate()
    p.pop('first_seen'); p.pop('last_seen')
    b = batch(device, [message(device, sid), usage(device, sid, p)])
    receive_batch(db, device, b)
    later = '2026-11-05T03:00:00Z'
    receive_batch(db, device, batch(device, [message(device, sid, mid='later', at=later, order=2)]))
    u = raw_archive.facts(db, 'usage')[0]
    assert u['range_last_seen'] > u['range_first_seen']
    assert raw_archive.project_raw(db)['changed_facts'] == 0
    before = snapshot(db, 'archive_facts')
    calls = Counter()
    real = archive._upsert_local
    def counted(conn, device, kind, *args):
        calls[kind] += 1
        return real(conn, device, kind, *args)
    monkeypatch.setattr(archive, '_upsert_local', counted)
    receive_batch(db, device, b)
    assert not calls
    assert snapshot(db, 'archive_facts') == before


def test_native_only_new_bound_does_not_refresh_native_bounded_usage(rig, monkeypatch):
    _, db, _ = rig
    device, sid, _ = source(db)
    receive_batch(db, device, batch(device, [message(device, sid), usage(device, sid, aggregate())]))
    calls = Counter()
    real = archive._upsert_local
    def counted(conn, device, kind, *args):
        calls[kind] += 1
        return real(conn, device, kind, *args)
    monkeypatch.setattr(archive, '_upsert_local', counted)
    ev = make_event(sid, 'synthetic-session', 'native.observed', 'n1', '1', 2,
        '2026-11-05T03:00:00Z', {'native_json': '{}'}, device, None)
    receive_batch(db, device, batch(device, [ev]))
    assert calls == Counter(session=1)
    assert raw_archive.project_raw(db)['changed_facts'] == 0


def test_first_touch_backfills_unbridged_raw_without_losing_old_messages(rig):
    _, db, _ = rig
    device, sid, _ = source(db)
    seed_raw(db, [message(device, sid, mid='old')])
    receive_batch(db, device, batch(device, [message(device, sid, mid='new')]))
    assert {m['message_id'] for m in raw_archive.facts(db, 'message')} == {'old', 'new'}
    assert raw_archive.project_raw(db)['changed_facts'] == 0


def test_earlier_run_start_refreshes_merged_run_not_only_latest_event_id(rig):
    _, db, _ = rig
    device, sid, _ = source(db)
    def run(order, start, end):
        return make_event(sid, 'synthetic-session', 'run.observed', 'r', str(order), order, end or start,
            {'native_turn_id': 'r', 'status': 'completed' if end else 'running',
             'start_at': start, 'end_at': end}, device, None)
    receive_batch(db, device, batch(device, [run(2, None, '2026-11-05T01:02:00Z')]))
    receive_batch(db, device, batch(device, [run(1, AT, None)]))
    assert raw_archive.facts(db, 'run')[0]['duration_ms'] == 120000
    assert raw_archive.project_raw(db)['changed_facts'] == 0
