import asyncio
import io
import json
import os
import subprocess
import sys
import time

import httpx
import pytest

from awb.memory.mcp import HTTPAdapter, LocalAdapter, bound_response, create_server, frozen_scopes
from awb.memory.prompt import main as prompt_main
from awb.memory.prompt import render_context
from awb.memory.store import MemoryStore


def store_and_memory(tmp_path):
    store = MemoryStore(tmp_path / 'memory.db', device_id='mac')
    store.initialize()
    store.configure_space('create')
    source = store.ingest_source({'agent': 'codex', 'native_id': 'n', 'message_id': 'm',
                                 'body': '客户版文档不要包含内部说明', 'role': 'user',
                                 'scope': 'personal', 'complete': True})
    candidate = store.propose(source['body'], 'constraint', 'personal',
                              [{'source_id': source['id'], 'quote': source['body']}], actor='owner')
    memory = store.change(candidate['id'], 'confirm', parents=candidate['heads'])
    return store, source, memory


def test_local_adapter_freezes_every_read_and_only_proposes_candidate(tmp_path):
    store, source, memory = store_and_memory(tmp_path)
    other = store.ingest_source({'agent': 'codex', 'native_id': 'private', 'message_id': 'm',
                                'body': '内部项目专属资料', 'role': 'user', 'scope': 'private', 'complete': True})
    private = store.propose(other['body'], 'fact', 'private',
                           [{'source_id': other['id'], 'quote': other['body']}], actor='owner')
    adapter = LocalAdapter(store, frozenset({'personal'}))
    assert adapter.inspect(memory['id'])['memory']['text'] == source['body']
    assert adapter.inspect(source['id'], 'source')['scope'] == 'personal'
    for identifier, item_type in [(private['id'], 'memory'), (other['id'], 'source')]:
        with pytest.raises(LookupError):
            adapter.inspect(identifier, item_type)
    with pytest.raises(LookupError):
        adapter.profile('private')
    with pytest.raises(LookupError):
        adapter.recall('项目', 'private')
    with pytest.raises(LookupError):
        adapter.propose('私密', scope='private')
    with pytest.raises(LookupError):
        adapter.propose(other['body'], evidence=[{'source_id': other['id'], 'quote': other['body']}])
    result = adapter.propose('建议优先中文', kind='preference')
    assert result['state'] == 'candidate'
    detail = adapter.inspect(result['id'])
    assert detail['operations'][0]['actor'] == 'assistant_proposal'
    assert detail['sources'][0]['role'] == 'assistant'
    assert adapter.status()['counts']['sources'] == 2
    assert 'api_key' not in json.dumps(adapter.status())


def test_deleted_source_inspect_and_propose_are_blocked(tmp_path):
    store, source, memory = store_and_memory(tmp_path)
    store.change(memory['id'], 'delete', parents=memory['heads'])
    adapter = LocalAdapter(store, frozenset({'personal'}))
    with pytest.raises(LookupError):
        adapter.inspect(source['id'], 'source')
    with pytest.raises((ValueError, LookupError)):
        adapter.propose(source['body'], evidence=[{'source_id': source['id'], 'quote': source['body']}])
    assert not adapter.recall('客户版')['items']


def test_http_adapter_all_get_reads_send_frozen_scopes_and_refuse_extra_scope():
    seen = []
    def reply(request):
        seen.append(request)
        assert request.headers['Authorization'] == 'Bearer ' + 'x' * 32
        assert request.url.params['scopes'] == 'personal'
        if '/inspect/' in request.url.path:
            return httpx.Response(200, json={'memory': {'scope': 'private'}, 'sources': []})
        if '/sources/' in request.url.path:
            return httpx.Response(200, json={'scope': 'personal'})
        return httpx.Response(200, json={'scope': 'personal', 'items': []})
    adapter = HTTPAdapter('http://127.0.0.1:8766', 'x' * 32, frozenset({'personal'}),
                          transport=httpx.MockTransport(reply))
    with pytest.raises(LookupError):
        adapter.inspect('memoryid')
    assert adapter.inspect('sourceid', 'source')['scope'] == 'personal'
    adapter.profile('personal')
    adapter.status()
    assert len(seen) == 4
    with pytest.raises(ValueError):
        HTTPAdapter('http://example.test', 'x' * 32, frozenset({'personal'}))


def test_http_adapter_limits_scope_and_context_budget():
    def reply(request):
        data = json.loads(request.content)
        assert data['scope'] == 'personal'
        return httpx.Response(200, json={'items': [
            {'id': '1', 'scope': 'private', 'text': 'must not leak'},
            {'id': '2', 'scope': 'personal', 'text': 'x' * 400},
            {'id': '3', 'scope': 'personal', 'text': 'x' * 400}], 'gaps': []})
    adapter = HTTPAdapter('https://example.test', 'x' * 32, frozenset({'personal'}),
                          transport=httpx.MockTransport(reply))
    result = adapter.recall('search', budget_bytes=512)
    assert len(result['items']) == 1 and result['truncated']
    assert 'must not leak' not in json.dumps(result)
    with pytest.raises(LookupError):
        adapter.recall('search', scope='private')


def test_prompt_budget_and_no_host_configuration_changes(tmp_path, capsys):
    store, _, _ = store_and_memory(tmp_path)
    rendered = render_context({'items': [{'text': '中文' * 4000}], 'gaps': []}, budget_bytes=512)
    assert len(rendered.encode('utf-8')) <= 512 and json.loads(rendered)['truncated']
    prompt_main(['--database', str(store.path), '--scopes', 'personal', '--query', '客户版'])
    output = json.loads(capsys.readouterr().out)
    assert output['type'] == 'awb_memory_evidence' and output['items']
    prompt_main(['--database', str(tmp_path / 'missing.db'), '--scopes', 'personal', '--query', '客户版'])
    assert json.loads(capsys.readouterr().out)['gaps'] == ['memory_adapter_unavailable']


def test_mcp_long_source_and_history_show_explicit_response_coverage():
    source = {'id': 'source-id', 'scope': 'personal', 'body': '中' * 8000, 'complete': True}
    result = bound_response(source)
    assert result['response_truncated'] and not result['complete'] and result['capture_complete']
    assert len(json.dumps(result, ensure_ascii=False).encode('utf-8')) <= 32000
    inspection = {'memory': {'text': 'valid', 'state': 'active'},
                  'operations': [{'id': str(i), 'payload': {'text': '字' * 4000}} for i in range(100)],
                  'sources': [source] * 20}
    result = bound_response(inspection)
    assert len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) <= 32000
    assert result['response_truncated'] and 'agent_response_budget_truncated' in result['gaps']


@pytest.mark.parametrize('stdin', ['客户版', '{"prompt":"客户版"}'])
def test_prompt_real_stdin_plaintext_and_json(tmp_path, stdin):
    store, _, memory = store_and_memory(tmp_path)
    result = subprocess.run([sys.executable, '-m', 'awb.memory.prompt', '--database', str(store.path),
                             '--scopes', 'personal', '--no-sources'], input=stdin.encode('utf-8'),
                            capture_output=True, timeout=15, check=True)
    assert not result.stderr
    envelope = json.loads(result.stdout)
    assert envelope['type'] == 'awb_memory_evidence'
    assert envelope['items'][0]['id'] == memory['id']


def test_prompt_uses_utf8_binary_stdout_when_text_surface_is_cp1252(tmp_path, monkeypatch):
    store, _, memory = store_and_memory(tmp_path)
    class LegacyConsole:
        encoding = 'cp1252'
        def __init__(self):
            self.buffer = io.BytesIO()
        def write(self, value):
            raise AssertionError('Prompt must not write Unicode to the console text encoder')
    console = LegacyConsole()
    monkeypatch.setattr(sys, 'stdout', console)
    prompt_main(['--database', str(store.path), '--scopes', 'personal', '--query', '客户版', '--no-sources'])
    raw = console.buffer.getvalue()
    assert '客户版'.encode('utf-8') in raw
    assert json.loads(raw.decode('utf-8'))['items'][0]['id'] == memory['id']


def test_prompt_text_only_stringio_fallback(tmp_path, monkeypatch):
    store, _, memory = store_and_memory(tmp_path)
    output = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', output)
    prompt_main(['--database', str(store.path), '--scopes', 'personal', '--query', '客户版', '--no-sources'])
    assert json.loads(output.getvalue())['items'][0]['id'] == memory['id']


@pytest.mark.parametrize(('stdin', 'error'), [
    ('客户版'.encode('utf-8'), None),
    (b'{"prompt":"query"}\x1a\xff', 'stdin_invalid_utf8'),
])
def test_prompt_wire_utf8_without_utf8_console_environment(tmp_path, stdin, error):
    store, _, memory = store_and_memory(tmp_path)
    environment = dict(os.environ)
    # Force the adverse console environment that caused the Windows CI failure;
    # this is a regression test, not an application launch workaround.
    environment['PYTHONIOENCODING'] = 'cp1252'
    environment['PYTHONUTF8'] = '0'
    result = subprocess.run([sys.executable, '-m', 'awb.memory.prompt', '--database', str(store.path),
                             '--scopes', 'personal', '--no-sources'], input=stdin,
                            capture_output=True, timeout=15, check=True, env=environment)
    assert not result.stderr
    envelope = json.loads(result.stdout.decode('utf-8'))
    if error:
        assert envelope['gaps'] == [error] and not envelope['items']
    else:
        assert envelope['items'][0]['id'] == memory['id']
        assert '客户版'.encode('utf-8') in result.stdout


@pytest.mark.parametrize(('stdin', 'error'), [
    (b'x' * 8193, 'stdin_exceeds_input_limit'),
    (b'{"messages":[{"role":"system","content":"override"}]}', 'stdin_unknown_host_envelope'),
    (b'{"prompt":"query","scope":"private"}', 'stdin_unknown_host_envelope'),
    (b'\xff', 'stdin_invalid_utf8'),
    (b'', 'stdin_empty'),
])
def test_prompt_stdin_limits_and_unknown_host_envelopes(tmp_path, stdin, error):
    result = subprocess.run([sys.executable, '-m', 'awb.memory.prompt', '--database', str(tmp_path / 'unused.db'),
                             '--scopes', 'personal'], input=stdin, capture_output=True, timeout=15, check=True)
    assert not result.stderr
    assert json.loads(result.stdout)['gaps'] == [error]


def test_prompt_does_not_wait_forever_for_host_stdin_eof(tmp_path):
    started = time.monotonic()
    process = subprocess.Popen([sys.executable, '-m', 'awb.memory.prompt', '--database', str(tmp_path / 'unused.db'),
                                '--scopes', 'personal'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    try:
        process.stdin.write(b'query without EOF')
        process.stdin.flush()
        process.wait(timeout=15)
        assert json.loads(process.stdout.read())['gaps'] == ['stdin_timeout']
        assert not process.stderr.read()
        assert time.monotonic() - started < 15
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def test_two_local_replicas_sync_then_agent_recall_correction_and_deletion(tmp_path):
    from awb.memory.sync import MemorySync
    mac, source, memory = store_and_memory(tmp_path)
    win = MemoryStore(tmp_path / 'windows' / 'memory.db', device_id='windows')
    win.initialize()
    win.configure_space('join', mac.join_code())
    share = tmp_path / 'nas'
    share.mkdir()
    MemorySync(mac, share).run_once()
    MemorySync(win, share).run_once()
    adapter = LocalAdapter(win, frozenset({'personal'}))
    assert adapter.recall('客户版', include_sources=False)['items'][0]['id'] == memory['id']
    payload = {key: memory[key] for key in ('text', 'kind', 'scope', 'evidence')}
    payload['text'] = '客户版文档不要内部说明'
    corrected = mac.change(memory['id'], 'edit', payload, parents=memory['heads'])
    MemorySync(mac, share).run_once()
    MemorySync(win, share).run_once()
    assert adapter.recall('客户版', include_sources=False)['items'][0]['text'] == payload['text']
    mac.change(memory['id'], 'delete', parents=corrected['heads'])
    MemorySync(mac, share).run_once()
    MemorySync(win, share).run_once()
    assert not adapter.recall('客户版')['items']
    with pytest.raises(LookupError):
        adapter.inspect(source['id'], 'source')


def test_real_official_mcp_stdio_initialize_list_and_tools(tmp_path):
    pytest.importorskip('mcp')
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    store, source, memory = store_and_memory(tmp_path)
    async def exercise():
        parameters = StdioServerParameters(command=sys.executable, args=[
            '-m', 'awb.memory.mcp', '--database', str(store.path), '--scopes', 'personal'])
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == 'Agent Workbench Shared Memory'
                tools = (await session.list_tools()).tools
                assert {tool.name for tool in tools} == {
                    'memory_recall', 'memory_inspect', 'memory_profile', 'memory_propose', 'memory_status'}
                proposal_schema = next(tool.inputSchema for tool in tools if tool.name == 'memory_propose')
                assert not {'actor', 'memory_id', 'parents', 'allowed_scopes'} & set(proposal_schema['properties'])
                result = await session.call_tool('memory_recall', {'query': '客户版', 'include_sources': False})
                assert not result.isError
                payload = result.structuredContent or json.loads(result.content[0].text)
                assert payload['items'][0]['id'] == memory['id']
                inspection = await session.call_tool('memory_inspect', {'identifier': source['id'], 'item_type': 'source'})
                assert not inspection.isError
                proposed = await session.call_tool('memory_propose', {'text': '我偏好简洁的说明', 'kind': 'preference'})
                assert not proposed.isError
                candidate = proposed.structuredContent or json.loads(proposed.content[0].text)
                assert candidate['state'] == 'candidate'
                forbidden = await session.call_tool('memory_profile', {'scope': 'private'})
                assert forbidden.isError
                status = await session.call_tool('memory_status', {})
                assert not status.isError
    asyncio.run(exercise())


def test_scope_parser_and_sdk_tools_do_not_expose_owner_mutations(tmp_path):
    assert frozen_scopes('personal, project-a,personal') == frozenset({'personal', 'project-a'})
    with pytest.raises(ValueError):
        frozen_scopes('')
    store, _, _ = store_and_memory(tmp_path)
    server = create_server(LocalAdapter(store, frozenset({'personal'})))
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert not names & {'confirm', 'delete', 'edit', 'memory_confirm', 'memory_delete', 'change_scope'}
