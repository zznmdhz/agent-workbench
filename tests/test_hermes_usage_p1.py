"""P1: aggregate substitution needs native bounds and complete coverage identity."""
from __future__ import annotations

import pytest

from awb import archive, raw_archive
from awb.api import create_app

AT = '2026-11-05T01:00:00Z'


@pytest.fixture
def ledger(tmp_path):
    return create_app(tmp_path / 'p1-synthetic.db').state.db


def observation(*, raw=False, task='t1', amount=500, first=10, last=100, **extra):
    value = {'model': 'p1-model', 'task': task, 'usage_key': 'key-' + task,
             'coverage_scope': 'session_model', 'provider': 'p1-provider',
             'billing_base_url': 'https://synthetic.invalid/v1', 'billing_mode': 'api',
             'first_seen': first, 'last_seen': last, 'input_tokens': amount,
             'output_tokens': 0, 'cache_read_tokens': 0, 'cache_write_tokens': 0,
             'api_call_count': 1}
    if raw:
        value.update(raw_archive_version=1, quantity_semantics='aggregate',
                     precision='session_model_aggregate', range_first_seen=first,
                     range_last_seen=last, observed_at='2026-11-06T00:00:00Z')
    value.update(extra)
    return value


def reduce(ledger, *values):
    with ledger.tx() as conn:
        for i, value in enumerate(values):
            archive._upsert_local(conn, 'p1-device', 'usage', 'hermes',
                                 'p1-session', str(i), AT, value)
    return raw_archive.facts(ledger, 'usage')


def counted(rows):
    return sum(r['input_tokens'] for r in rows if r.get('precision') != 'unallocated')


def test_missing_native_watermark_cannot_use_session_range(ledger):
    rows = reduce(ledger, observation(), observation(raw=True, amount=300,
                  last=None, range_last_seen=200))
    assert counted(rows) == 500
    assert len(rows) == 2 and rows[1]['precision'] == 'unallocated'


def test_one_task_raw_cannot_swallow_uncovered_legacy_task(ledger):
    rows = reduce(ledger, observation(), observation(task='t2', amount=200),
                  observation(raw=True, amount=600, last=300))
    assert counted(rows) == 800
    assert {r['task'] for r in rows if r.get('precision') != 'unallocated'} == {'t1', 't2'}


@pytest.mark.parametrize('last,expected', [(300, 600), (50, 500), (100, 600)])
def test_same_identity_native_coverage(ledger, last, expected):
    rows = reduce(ledger, observation(), observation(raw=True, amount=600, last=last))
    assert counted(rows) == expected
    assert sum(r.get('precision') == 'unallocated' for r in rows) == (last < 100)


def test_later_end_does_not_prove_complete_coverage_if_start_is_later(ledger):
    rows = reduce(ledger, observation(), observation(raw=True, amount=600, first=20, last=300))
    assert counted(rows) == 500


def test_first_raw_with_no_legacy_still_visible_without_native_bounds(ledger):
    rows = reduce(ledger, observation(raw=True, amount=300, first=None, last=None,
                  range_first_seen=10, range_last_seen=200))
    assert counted(rows) == 300


@pytest.mark.parametrize('field', ['provider', 'task', 'billing_base_url', 'billing_mode'])
def test_proven_distinct_identity_does_not_replace_other_legacy(ledger, field):
    new = observation(raw=True, amount=600, last=300, **{field: 'different'})
    new['usage_key'] = 'different-complete-identity'
    rows = reduce(ledger, observation(), new)
    assert counted(rows) == 1100


@pytest.mark.parametrize('field', ['provider', 'task', 'billing_base_url', 'billing_mode'])
def test_missing_identity_is_pending_not_added_or_replaced(ledger, field):
    old, new = observation(), observation(raw=True, amount=600, last=300)
    old.pop('usage_key')
    new.pop(field)
    rows = reduce(ledger, old, new)
    assert counted(rows) == 500
    assert len(rows) == 2 and rows[1]['precision'] == 'unallocated'


def test_explicit_shared_usage_key_proves_opaque_billing_identity(ledger):
    old, new = observation(), observation(raw=True, amount=600, last=300)
    for value in (old, new):
        for field in ['provider', 'billing_base_url', 'billing_mode']:
            value.pop(field)
    assert counted(reduce(ledger, old, new)) == 600


def test_conflicting_explicit_dimension_blocks_shared_key_substitution(ledger):
    rows = reduce(ledger, observation(), observation(raw=True, provider='different', amount=600, last=300))
    assert counted(rows) == 1100


def test_missing_legacy_watermark_is_not_replaced_by_transport_time(ledger):
    rows = reduce(ledger, observation(last=None), observation(raw=True, amount=600, last=300))
    assert counted(rows) == 500


def test_two_raw_keys_same_complete_identity_are_not_added(ledger):
    rows = reduce(ledger, observation(),
                  observation(raw=True, usage_key='parser-a', amount=600, last=200),
                  observation(raw=True, usage_key='parser-b', amount=700, last=300))
    assert counted(rows) == 700 and len(rows) == 1


def test_raw_only_two_keys_same_complete_identity_are_not_added(ledger):
    rows = reduce(ledger,
                  observation(raw=True, usage_key='parser-a', amount=600, last=200),
                  observation(raw=True, usage_key='parser-b', amount=700, last=300))
    assert counted(rows) == 700 and len(rows) == 1


def test_two_raw_usage_keys_same_model_never_erase_legacy_other_task(ledger):
    rows = reduce(ledger, observation(), observation(task='t2', amount=200),
                  observation(raw=True, amount=600, last=300),
                  observation(raw=True, task='t3', amount=30, last=300))
    assert counted(rows) == 830
