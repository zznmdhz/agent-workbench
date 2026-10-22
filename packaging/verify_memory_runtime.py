"""Verify a frozen console launcher using isolated synthetic memory and real MCP IO.

Run with the source project's Python environment (including memory-mcp extra):
  python packaging/verify_memory_runtime.py --executable /path/to/AgentWorkbenchCLI

No Agent history, application defaults, production database or NAS share is opened.
Every fixture and joining credential is temporary. JSON summaries contain only
whitelisted check names and booleans, never credentials or conversation text.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

TIMEOUT_SECONDS = 30
TOOLS = frozenset({'memory_recall', 'memory_inspect', 'memory_profile',
                   'memory_propose', 'memory_status'})


class RuntimeCheckError(ValueError):
    def __init__(self, stage: str, reason: str):
        self.stage, self.reason = stage, reason
        super().__init__(reason)


def require(condition: bool, stage: str, reason: str) -> None:
    if not condition:
        raise RuntimeCheckError(stage, reason)


def isolated_environment(root: Path) -> dict[str, str]:
    # Do not forward model, NAS, Agent or inherited application configuration.
    allowed = {'PATH', 'SYSTEMROOT', 'SYSTEMDRIVE', 'WINDIR', 'COMSPEC', 'PATHEXT',
               'TEMP', 'TMP', 'HOME', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH',
               'LANG', 'LC_ALL', 'LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH'}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment['AWB_SYNC_ROOT'] = str(root / 'unused-synthetic-nas')
    return environment


def tool_payload(result, stage: str) -> dict:
    require(not result.isError, stage, 'mcp_tool_error')
    value = result.structuredContent
    if value is None:
        blocks = [block.text for block in result.content if getattr(block, 'type', None) == 'text']
        require(bool(blocks), stage, 'missing_tool_json')
        try:
            value = json.loads(blocks[0])
        except (ValueError, TypeError) as exc:
            raise RuntimeCheckError(stage, 'invalid_tool_json') from exc
    require(isinstance(value, dict), stage, 'invalid_tool_payload')
    return value


async def verify_mcp(executable: Path, store, memory: dict, environment: dict, root: Path) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(command=str(executable), args=[
        'memory-mcp', '--database', str(store.path), '--scopes', 'personal'],
        env=environment, cwd=str(root))
    # Child SDK diagnostics are isolated too; failed verification never copies
    # uncontrolled process output or source data into its summary.
    with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as diagnostics:
        async with stdio_client(parameters, errlog=diagnostics) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=10)) as client:
                initialized = await client.initialize()
                require(initialized.serverInfo.name == 'Agent Workbench Shared Memory',
                        'mcp_initialize', 'unexpected_server')
                tools = await client.list_tools()
                require({tool.name for tool in tools.tools} == TOOLS, 'mcp_list_tools', 'unexpected_tools')
                recalled = tool_payload(await client.call_tool('memory_recall', {
                    'query': '客户版', 'scope': 'personal', 'include_sources': False}), 'mcp_recall')
                require(any(item.get('id') == memory['id'] and item.get('text') == memory['text']
                            for item in recalled.get('items', [])), 'mcp_recall', 'synthetic_memory_missing')
                proposed = tool_payload(await client.call_tool('memory_propose', {
                    'text': '我偏好简洁的中文说明', 'kind': 'preference', 'scope': 'personal'}), 'mcp_propose')
                require(proposed.get('state') == 'candidate', 'mcp_propose', 'proposal_not_candidate')
                detail = store.inspect(proposed['id'])
                require(all(op['actor'] == 'assistant_proposal' for op in detail['operations']),
                        'mcp_propose', 'incorrect_proposal_authority')
                status = tool_payload(await client.call_tool('memory_status', {}), 'mcp_status')
                counts = status.get('counts', {})
                require(counts.get('active') == 1 and counts.get('candidate') == 1,
                        'mcp_status', 'unexpected_fixture_counts')
    return {'initialize': True, 'list_tools': True, 'recall_chinese': True,
            'propose_candidate_only': True, 'status': True, 'tool_count': len(TOOLS)}


def verify_prompt(executable: Path, store, memory: dict, environment: dict, root: Path) -> dict:
    adverse = dict(environment)
    # This deliberately reproduces the adverse console setting. The launcher
    # must emit UTF-8 itself rather than relying on a UTF-8 environment workaround.
    adverse['PYTHONIOENCODING'] = 'cp1252'
    adverse['PYTHONUTF8'] = '0'
    for label, payload in [('plaintext', '客户版'), ('json', '{"prompt":"客户版"}')]:
        try:
            result = subprocess.run([str(executable), 'memory-prompt', '--database', str(store.path),
                                     '--scopes', 'personal', '--no-sources'],
                                    input=payload.encode('utf-8'), capture_output=True,
                                    env=adverse, cwd=root, timeout=TIMEOUT_SECONDS, check=False)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeCheckError('prompt_' + label, 'process_timeout') from exc
        require(result.returncode == 0, 'prompt_' + label, 'launcher_failed')
        require(len(result.stdout) <= 12001, 'prompt_' + label, 'context_budget_exceeded')
        try:
            envelope = json.loads(result.stdout.decode('utf-8'))
        except (ValueError, UnicodeError) as exc:
            raise RuntimeCheckError('prompt_' + label, 'invalid_utf8_json') from exc
        require(envelope.get('type') == 'awb_memory_evidence', 'prompt_' + label, 'unexpected_envelope')
        require(any(item.get('id') == memory['id'] and item.get('text') == memory['text']
                    for item in envelope.get('items', [])), 'prompt_' + label, 'synthetic_memory_missing')
        require('客户版'.encode('utf-8') in result.stdout, 'prompt_' + label, 'unicode_not_preserved')
    return {'utf8_stdout_under_cp1252': True, 'plaintext_stdin': True, 'json_stdin': True,
            'bounded_context': True}


def verify(executable: Path) -> dict:
    from awb.memory.store import MemoryStore

    executable = executable.expanduser().resolve()
    require(executable.is_file(), 'setup', 'executable_not_found')
    with tempfile.TemporaryDirectory(prefix='awb-frozen-memory-check-') as directory:
        root = Path(directory)
        store = MemoryStore(root / 'memory.db', device_id='synthetic-runtime-check')
        store.initialize()
        store.configure_space('create')
        source = store.ingest_source({'agent': 'synthetic-runtime-check', 'native_id': 'fixture-session',
                                      'message_id': 'fixture-message', 'role': 'user',
                                      'body': '客户版文档不要包含内部说明', 'scope': 'personal',
                                      'complete': True})
        candidate = store.propose(source['body'], 'constraint', 'personal',
                                  [{'source_id': source['id'], 'quote': source['body']}], actor='owner')
        memory = store.change(candidate['id'], 'confirm', parents=candidate['heads'])
        environment = isolated_environment(root)
        try:
            mcp = asyncio.run(asyncio.wait_for(
                verify_mcp(executable, store, memory, environment, root), timeout=TIMEOUT_SECONDS))
        except TimeoutError as exc:
            raise RuntimeCheckError('mcp', 'protocol_timeout') from exc
        prompt = verify_prompt(executable, store, memory, environment, root)
        require(not store.model_usage()['items'], 'isolation', 'unexpected_model_request')
        require(not (root / 'unused-synthetic-nas').exists(), 'isolation', 'unexpected_nas_access')
        desktop = (executable.parent / 'AgentWorkbench.exe' if sys.platform == 'win32'
                   else executable.parents[2] / 'MacOS/AgentWorkbench')
        require(desktop.is_file(), 'hermes_profiles', 'desktop_binary_missing')
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('verify_hermes_runtime.py')),
                                 '--executable', str(desktop), '--output', str(root / 'hermes.json')],
                                capture_output=True, timeout=60)
        require(result.returncode == 0, 'hermes_profiles', 'frozen_profile_capture_failed')
        hermes = json.loads((root / 'hermes.json').read_text(encoding='utf-8'))
    with executable.open('rb') as binary:
        checksum = hashlib.file_digest(binary, 'sha256').hexdigest()
    return {'schema': 1, 'success': True, 'executable_sha256': checksum,
            'mcp': mcp, 'prompt': prompt, 'hermes_profiles': hermes,
            'synthetic_data_only': True, 'model_requests': 0}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', type=Path, required=True, help='Frozen console CLI executable')
    parser.add_argument('--output', type=Path, help='Optional UTF-8 JSON summary output')
    args = parser.parse_args(argv)
    try:
        summary = verify(args.executable)
    except RuntimeCheckError as exc:
        summary = {'schema': 1, 'success': False, 'stage': exc.stage, 'error': exc.reason}
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        summary = {'schema': 1, 'success': False, 'stage': 'runtime', 'error': type(exc).__name__}
    rendered = json.dumps(summary, ensure_ascii=True, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding='utf-8')
    sys.stdout.write(rendered)
    return 0 if summary['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
