"""Owner management, scoped remote reads and restoration without dashboard regressions."""

import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from awb.api import create_app
from awb.memory.store import MemoryStore
from awb.memory_bundle import backup_memory, restore_memory
from awb.memory_http import create_memory_service


@pytest.fixture
def owner(tmp_path):
    app = create_app(tmp_path / 'dashboard.db', desktop_mode=True)
    client = TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 8000))
    csrf = client.get('/auth/me').json()['csrf']
    headers = {'x-awb-csrf': csrf}
    assert client.post('/v1/memory/space', json={'mode': 'create'}, headers=headers).status_code == 200
    return app, client, headers


def propose(client, headers, text='客户版保留原件', scope='personal'):
    result = client.post('/v1/memory/propose', headers=headers,
                         json={'text': text, 'kind': 'constraint', 'scope': scope})
    assert result.status_code == 200, result.text
    return result.json()


def change(client, headers, memory, action, payload=None):
    return client.post(f"/v1/memory/items/{memory['id']}/change", headers=headers,
                       json={'action': action, 'parents': memory['heads'], 'payload': payload})


def test_owner_review_edit_stale_delete_and_existing_health(owner):
    app, client, headers = owner
    candidate = propose(client, headers)
    assert candidate['state'] == 'candidate'
    assert change(client, {}, candidate, 'confirm').status_code == 403
    active = change(client, headers, candidate, 'confirm').json()
    assert active['state'] == 'active'
    result = client.post('/v1/memory/recall', json={'query': '客户版', 'scope': 'personal'})
    assert result.status_code == 200 and '保留原件' in result.text
    edited = change(client, headers, active, 'edit', {'text': '客户版必须保留原始文件'}).json()
    assert edited['state'] == 'active'
    assert change(client, headers, active, 'disable').status_code == 409
    deleted = change(client, headers, edited, 'delete').json()
    assert deleted['state'] == 'deleted'
    recalled = client.post('/v1/memory/recall', json={'query': '客户版', 'scope': 'personal'})
    assert '客户版必须保留原始文件' not in recalled.text
    assert client.get('/health/ready').status_code == 200
    with app.state.db.read() as conn:
        assert conn.execute('SELECT version FROM schema_version').fetchone()[0] == 1


def test_owner_cross_scope_change_rejected_and_join_not_public(owner):
    _, client, headers = owner
    memory = propose(client, headers)
    assert change(client, headers, memory, 'edit', {'text': '新内容', 'scope': 'private-project'}).status_code == 422
    assert client.post('/v1/memory/join-credential', json={}).status_code == 403
    assert client.get('/v1/memory/settings').json()['enabled'] is False
    assert client.post('/v1/memory/settings', json={'enabled': True}, headers={}).status_code == 403


def test_remote_agent_scope_and_write_authority(owner, tmp_path):
    app, client, headers = owner
    personal = change(client, headers, propose(client, headers), 'confirm').json()
    secret = change(client, headers, propose(client, headers, '项目隐私要求甲乙丙', 'private-project'), 'confirm').json()
    token_file = tmp_path / 'token'
    token_file.write_text('a' * 40)
    service = create_memory_service(app.state.memory_store.path, token_file,
                                    frozenset({'personal', 'private-project'}))
    remote = TestClient(service)
    auth = {'authorization': 'Bearer ' + 'a' * 40}
    assert remote.get('/v1/memory/status').status_code == 401
    assert remote.get('/v1/memory/status?scopes=personal', headers=auth).json()['counts']['active'] == 1
    assert remote.get(f"/v1/memory/inspect/{secret['id']}?scopes=personal", headers=auth).status_code == 404
    source = secret['evidence'][0]['source_id']
    assert remote.get(f'/v1/memory/sources/{source}?scopes=personal', headers=auth).status_code == 404
    assert remote.get(f"/v1/memory/inspect/{personal['id']}?scopes=personal", headers=auth).status_code == 200
    result = remote.post('/v1/memory/recall', headers=auth,
                         json={'query': '客户版', 'scope': 'personal', 'budget_bytes': 12000})
    assert result.status_code == 200
    assert remote.post('/v1/memory/propose', headers=auth,
                       json={'text': '提议', 'actor': 'owner'}).status_code == 422
    assert remote.post('/v1/memory/propose', headers=auth,
                       json={'text': '范围越界', 'scope': 'personal', 'evidence': [{'source_id': source, 'quote': '项目隐私要求甲乙丙'}]}).status_code == 422
    assert remote.post(f"/v1/memory/items/{personal['id']}/change", headers=auth,
                       json={'action': 'delete'}).status_code == 404
    assert remote.get('/v1/memory/status', headers={**auth, 'Origin': 'https://evil.invalid'}).status_code == 403


def test_memory_bundle_keeps_ids_changes_writer_and_omits_credentials(owner, tmp_path):
    app, client, headers = owner
    memory = change(client, headers, propose(client, headers), 'confirm').json()
    original = app.state.memory_store
    original.update_settings({'llm': {'api_key': 'private-model-credential'}})
    package = tmp_path / 'memory.zip'
    result = backup_memory(original.path, package)
    restored_path = restore_memory(package, tmp_path / 'restored')
    replica = MemoryStore(restored_path)
    replica.initialize()
    assert replica.status()['space_id'] == result['space_id']
    assert replica.status()['device_id'] != original.status()['device_id']
    assert replica.inspect(memory['id'])['memory']['state'] == 'active'
    assert replica.status()['credentials_missing']
    assert not replica.settings()['enabled']
    with zipfile.ZipFile(package) as zip_file:
        assert set(zip_file.namelist()) == {'memory.db', 'manifest.json'}
        assert b'private-model-credential' not in zip_file.read('memory.db')
    replica.configure_space('join', join_code=original.join_code())
    assert replica.status()['has_join_key']


def test_bad_bundle_leaves_empty_target_and_future_schema_does_not_break_dashboard(owner, tmp_path):
    app, client, _ = owner
    package = tmp_path / 'bad.zip'
    backup_memory(app.state.memory_store.path, package)
    with zipfile.ZipFile(package) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        data = archive.read('memory.db')
    manifest['files']['memory.db']['sha256'] = '0' * 64
    with zipfile.ZipFile(package, 'w') as archive:
        archive.writestr('manifest.json', json.dumps(manifest))
        archive.writestr('memory.db', data)
    target = tmp_path / 'empty'
    target.mkdir()
    with pytest.raises(ValueError, match='checksum'):
        restore_memory(package, target)
    assert not list(target.iterdir())
    with app.state.memory_store.connect() as conn:
        conn.execute('PRAGMA user_version=999')
    new_app = create_app(app.state.db.path, desktop_mode=True)
    safe = TestClient(new_app, base_url='http://127.0.0.1', client=('127.0.0.1', 9000))
    assert safe.get('/health/ready').status_code == 200
    assert safe.get('/v1/memory/status').json()['available'] is False
    assert client.get('/health/live').status_code == 200


def test_large_remote_proposal_returns_success_receipt_after_commit(owner, tmp_path):
    app, _, _ = owner
    store = app.state.memory_store
    evidence = []
    for index in range(8):
        body = '合成证据' * 1250
        source = store.ingest_source({'agent': 'owner', 'native_id': f'large-{index}',
                                     'message_id': f'large-message-{index}',
                                     'role': 'user', 'body': body, 'scope': 'personal',
                                     'device_id': store.device_id, 'complete': True}, local=False)
        evidence.append({'source_id': source['id'], 'quote': body})
    token = tmp_path / 'large-token'
    token.write_text('b' * 40)
    remote = TestClient(create_memory_service(store.path, token, frozenset({'personal'})))
    result = remote.post('/v1/memory/propose', headers={'authorization': 'Bearer ' + 'b' * 40},
                         json={'text': '合成提议需人工审核', 'kind': 'fact', 'scope': 'personal',
                               'evidence': evidence})
    assert result.status_code == 200, result.text
    receipt = result.json()
    assert receipt['truncated'] and receipt['state'] == 'candidate'
    assert store.inspect(receipt['id'])['memory']['state'] == 'candidate'
    assert 'evidence' not in receipt
