"""Migration-specific reductions: a receipt timestamp is not a usage watermark."""
from awb import raw_archive
from awb.codec import utc_ms

def fact(fid, at, amount, bridge=False):
    r={'device_id':'canonical','_origin_device_id':'old-copy','agent':'hermes',
       'native_id':'synthetic-s','fact_id':fid,'model':'synthetic-m','task':'',
       'occurred_at':at,'first_seen':utc_ms('2026-09-01T00:00:00Z')/1000,
       'last_seen':utc_ms(at)/1000,'input_tokens':amount,'output_tokens':0,
       # These fixtures assert substitution of a PROVEN complete identity;
       # missing billing identity is independently tested as nonadditive P1 evidence.
       'provider':'synthetic-provider','billing_base_url':'https://synthetic.invalid',
       'billing_mode':'api','coverage_scope':'session_model',
       'cache_read_tokens':0,'cache_write_tokens':0,'api_call_count':1}
    if bridge:r.update(raw_archive_version=1,quantity_semantics='aggregate',usage_key='same-model',observed_at='2026-11-05T00:00:00Z')
    return r

def test_old_raw_import_does_not_replace_newer_archive_aggregate():
    newer=fact('legacy','2026-10-01T00:00:00Z',500)
    older=fact('raw','2026-09-26T00:00:00Z',300,True)
    rows=raw_archive._usage([newer,older])
    counted=[r for r in rows if r.get('precision')!='unallocated']
    assert sum(r['input_tokens'] for r in counted)==500
    assert any(r.get('omission_reason')=='older_than_archived_aggregate' for r in rows)

def test_newer_raw_snapshot_supersedes_legacy_not_adds():
    old=fact('legacy','2026-09-26T00:00:00Z',300)
    new=fact('raw','2026-10-01T00:00:00Z',500,True)
    rows=raw_archive._usage([old,new])
    assert sum(r['input_tokens'] for r in rows if r.get('precision')!='unallocated')==500

def test_alias_legacy_revisions_choose_native_watermark():
    old=fact('model:task','2026-09-26T00:00:00Z',300)
    new=fact('model:task','2026-10-01T00:00:00Z',500)
    assert raw_archive._dedupe([old,new],'usage')[0]['input_tokens']==500
    assert raw_archive._dedupe([new,old],'usage')[0]['input_tokens']==500

def test_same_snapshot_across_sources_not_added():
    a=fact('raw-a','2026-10-01T00:00:00Z',500,True)
    b={**a,'fact_id':'raw-b','source_id':'second-observer'}
    assert sum(r['input_tokens'] for r in raw_archive._usage([a,b]))==500
