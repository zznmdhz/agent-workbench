"""Independent review scenarios for cross-module persistence and concurrency contracts."""

import json
import sqlite3
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from awb.api import create_app
from awb.memory import MemoryStore
from awb.memory.embeddings import hybrid_recall
from awb.memory.events import BudgetExceededError
from awb.memory.mcp import HTTPAdapter
from awb.memory.worker import MemoryWorker
from awb.memory_bundle import backup_memory, restore_memory
from awb.memory_http import create_memory_service


def owner_client(path):
    app = create_app(path, desktop_mode=True)
    client = TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 8200))
    headers = {'x-awb-csrf': client.get('/auth/me').json()['csrf']}
    return app, client, headers


def owner_memory(client, headers, text='客户版保留原件'):
    candidate = client.post('/v1/memory/propose', headers=headers,
                            json={'text': text, 'kind': 'constraint', 'scope': 'personal'}).json()
    response = client.post(f"/v1/memory/items/{candidate['id']}/change", headers=headers,
                           json={'action': 'confirm', 'parents': candidate['heads']})
    assert response.status_code == 200, response.text
    return response.json()


def make_store(path, device='review'):
    store = MemoryStore(path, device_id=device).initialize()
    store.configure_space('create')
    return store


def add_memory(store, text='我偏好中文交付', message='m'):
    source = store.ingest_source({'agent': 'codex', 'native_id': 'n', 'message_id': message,
                                 'body': text, 'role': 'user', 'scope': 'personal', 'complete': True})
    candidate = store.propose(text, 'preference', 'personal',
                              [{'source_id': source['id'], 'quote': text}], actor='owner')
    return source, store.change(candidate['id'], 'confirm', parents=candidate['heads'])


def test_review_stale_owner_correction_has_no_raw_recall_side_effect(tmp_path):
    app, client, headers = owner_client(tmp_path / 'dashboard.db')
    assert client.post('/v1/memory/space', json={'mode': 'create'}, headers=headers).status_code == 200
    original = owner_memory(client, headers)
    path = f"/v1/memory/items/{original['id']}/change"
    corrected = client.post(path, headers=headers, json={
        'action': 'edit', 'parents': original['heads'], 'payload': {'text': '客户版必须保留原始文件'}})
    assert corrected.status_code == 200
    rejected_text = '过期修改不应进入原文证据'
    stale = client.post(path, headers=headers, json={
        'action': 'edit', 'parents': original['heads'], 'payload': {'text': rejected_text}})
    assert stale.status_code == 409
    assert rejected_text not in json.dumps(app.state.memory_store.sources(), ensure_ascii=False)
    assert not app.state.memory_store.recall('过期修改', frozenset({'personal'}))['items']


def test_review_restored_memory_can_load_in_dashboard(tmp_path):
    original_app, client, headers = owner_client(tmp_path / 'original' / 'dashboard.db')
    assert client.post('/v1/memory/space', json={'mode': 'create'}, headers=headers).status_code == 200
    memory = owner_memory(client, headers)
    package = tmp_path / 'memory.zip'
    backup_memory(original_app.state.memory_store.path, package)
    new_root = tmp_path / 'new-computer'
    restore_memory(package, new_root / 'memory')
    restored_app, restored_client, _ = owner_client(new_root / 'dashboard.db')
    status = restored_client.get('/v1/memory/status').json()
    assert status['available'], status
    assert status['credentials_missing']
    assert restored_app.state.memory_store.inspect(memory['id'])['memory']['state'] == 'active'


def test_review_delete_before_dependencies_and_then_late_sources(tmp_path):
    sender = make_store(tmp_path / 'sender.db', 'sender')
    source, memory = add_memory(sender)
    sender.change(memory['id'], 'delete', parents=memory['heads'])
    records = sender.export_events(after=0)
    receiver = MemoryStore(tmp_path / 'receiver.db', device_id='receiver').initialize()
    receiver.configure_space('join', sender.join_code())
    delete = next(op for op in records['operations'] if op['action'] == 'delete')
    receiver.import_events([delete], [])
    receiver.import_events([], records['sources'])
    assert not receiver.recall('中文', frozenset({'personal'}))['items']
    with pytest.raises(LookupError):
        receiver.get_source(source['id'], scopes=frozenset({'personal'}))
    receiver.import_events([op for op in records['operations'] if op['action'] != 'delete'], [])
    assert receiver.inspect(memory['id'])['memory']['state'] == 'deleted'
    changed = dict(source, body=source['body'] + '，版本已更新')
    for key in ('id', 'checksum', 'cursor', 'local'):
        changed.pop(key, None)
    changed = receiver.ingest_source(changed, local=True)
    assert not receiver.source_available(changed['id'])


def test_review_future_schema_bundle_rejected_without_partial_target(tmp_path):
    original = make_store(tmp_path / 'original.db')
    source, _ = add_memory(original)
    package = tmp_path / 'memory.zip'
    backup_memory(original.path, package)
    with zipfile.ZipFile(package) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        db_bytes = archive.read('memory.db')
    future_db = tmp_path / 'future.db'
    future_db.write_bytes(db_bytes)
    with sqlite3.connect(future_db) as conn:
        conn.execute('PRAGMA user_version=999')
    import hashlib
    data = future_db.read_bytes()
    manifest['files']['memory.db'] = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    with zipfile.ZipFile(package, 'w') as archive:
        archive.writestr('manifest.json', json.dumps(manifest))
        archive.writestr('memory.db', data)
    target = tmp_path / 'restore'
    target.mkdir()
    with pytest.raises(ValueError):
        restore_memory(package, target)
    assert not list(target.iterdir())
    assert original.get_source(source['id'])['body']


def test_review_http_adapter_real_service_budget_and_narrow_status(tmp_path):
    store = make_store(tmp_path / 'memory.db')
    _, memory = add_memory(store)
    private_source = store.ingest_source({'agent': 'codex', 'native_id': 'secret', 'message_id': 'm',
                                         'body': '项目机密要求', 'role': 'user', 'scope': 'private',
                                         'complete': True}, local=False)
    secret = store.propose(private_source['body'], 'fact', 'private',
                           [{'source_id': private_source['id'], 'quote': private_source['body']}], actor='owner')
    store.change(secret['id'], 'confirm', parents=secret['heads'])
    token_path = tmp_path / 'token'
    token_path.write_text('x' * 40)
    service = TestClient(create_memory_service(store.path, token_path, frozenset({'personal', 'private'})))
    def dispatch(request):
        response = service.request(request.method, request.url.path,
                                   params=request.url.params, headers=dict(request.headers),
                                   content=request.content)
        return httpx.Response(response.status_code, content=response.content,
                              headers=dict(response.headers))
    adapter = HTTPAdapter('http://127.0.0.1:8766', 'x' * 40, frozenset({'personal'}),
                          transport=httpx.MockTransport(dispatch))
    assert adapter.status()['counts']['active'] == 1
    assert adapter.inspect(memory['id'])['memory']['id'] == memory['id']
    with pytest.raises(LookupError):
        adapter.inspect(secret['id'])
    assert adapter.recall('中文', budget_bytes=32000, include_sources=False)['items']


def test_review_slow_embedding_correction_uses_fresh_confirmed_payload(tmp_path):
    store = make_store(tmp_path / 'memory.db')
    _, memory = add_memory(store)
    store.update_settings({'embedding': {'enabled': True, 'endpoint': 'https://model.invalid/v1',
                                        'model': 'embedding', 'dimensions': 2}})
    def reply(_request):
        text = '我偏好双语交付'
        source = store.ingest_source({'agent': 'owner', 'native_id': 'edit', 'message_id': 'm',
                                     'body': text, 'role': 'user', 'scope': 'personal', 'complete': True}, local=False)
        payload = {'text': text, 'kind': 'preference', 'scope': 'personal',
                   'evidence': [{'source_id': source['id'], 'quote': text}]}
        store.change(memory['id'], 'edit', payload, parents=memory['heads'])
        return httpx.Response(200, json={'data': [{'embedding': [1., 0.]}],
                                         'usage': {'prompt_tokens': 2}})
    result = hybrid_recall(store, '中文', scopes=frozenset({'personal'}), include_sources=False,
                           transport=httpx.MockTransport(reply))
    assert result['retrieval'] == 'hybrid'
    assert result['items'][0]['text'] == '我偏好双语交付'


def test_review_budget_exhaustion_defers_without_permanent_failure(tmp_path, monkeypatch):
    store = make_store(tmp_path / 'memory.db')
    store.update_settings({'enabled': True, 'llm': {'enabled': True, 'endpoint': 'https://model.invalid/v1',
                                                   'model': 'model', 'daily_call_limit': 1}})
    source = store.ingest_source({'agent': 'codex', 'native_id': 'n', 'message_id': 'm',
                                 'body': '请记住：我偏好中文交付', 'role': 'user', 'scope': 'personal',
                                 'complete': True})
    worker = MemoryWorker(store, tmp_path / 'missing-archive.db')
    def exhausted(*_args, **_kwargs):
        raise BudgetExceededError('daily memory model request limit reached')
    monkeypatch.setattr('awb.memory.worker.llm_candidates', exhausted)
    worker.run_once()
    with store.connect() as conn:
        job = dict(conn.execute('SELECT * FROM memory_jobs WHERE source_id=?', (source['id'],)).fetchone())
    assert job['status'] != 'failed'
    assert job['attempts'] == 0
    assert store.list_memories(state='candidate')['items']
