"""Native capture helpers. Never enumerate env/config or infer file references from prose."""
from __future__ import annotations

import json
import math
import re
import os
import stat
import base64
import hashlib
from pathlib import Path
from urllib.parse import urlparse, unquote

CAPTURE_VERSION = 'full-capture-v1'
_SECRET = re.compile(r'(?i)(?:^|[_-])(?:env|environment|credentials?|authorization|cookies?|password|secret|api[_-]?key|access[_-]?token|refresh[_-]?token)(?:$|[_-])')


def sanitized(value, redact):
    """Preserve native numeric JSON exactly in a string, outside canonical no-float facts."""
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {key: ('[OMITTED: sensitive field]' if _SECRET.search(key) or key.lower() == "token" else sanitized(item, redact))
                for key, item in value.items()}
    if isinstance(value, list):
        return [sanitized(item, redact) for item in value]
    if isinstance(value, str):
        if len(re.findall(r"(?m)^[A-Z_][A-Z0-9_]*=", value)) >= 3:
            return '[OMITTED: environment dump]'
        try:
            structured = json.loads(value)
        except (ValueError, TypeError):
            structured = None
        if isinstance(structured, (dict, list)):
            return json.dumps(sanitized(structured, redact), ensure_ascii=False, separators=(',', ':'))
        return redact(value)[0]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def native_payload(record, redact):
    clean = sanitized(record, redact)
    timestamp = record.get('timestamp')
    return {'capture_version': CAPTURE_VERSION,
            'native_type': str(record.get('type', 'unknown')),
            'native_timestamp': timestamp if isinstance(timestamp, (str, int)) else str(timestamp) if timestamp is not None else None,
            'native_json': json.dumps(clean, ensure_ascii=False, separators=(',', ':'), allow_nan=False),
            'content_state': 'redacted' if clean != record else 'full'}


def explicit_references(value):
    """Typed refs, native delivery markers and known file-tool arguments only."""
    found = []
    def visit(item, collection=False):
        if isinstance(item, str):
            try:
                parsed = json.loads(item)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                visit(parsed, collection)
            elif collection:
                found.append(item)
            else:
                # Native Hermes attachments/deliveries are protocol markers, not
                # arbitrary prose paths or executable shell output.
                for match in re.finditer(r'(?m)^(?:@image:|MEDIA:)(/[^\r\n]+)', item):
                    found.append(match.group(1).strip())
            return
        if isinstance(item, list):
            for nested in item:
                visit(nested, collection)
        elif isinstance(item, dict):
            typed = isinstance(item.get('type'), str) and item['type'] in {'input_image', 'image', 'image_url', 'input_file', 'file', 'attachment', 'audio', 'video'}
            if typed or collection:
                for key in ('path', 'file_path', 'local_path', 'image_url', 'url'):
                    ref = item.get(key)
                    if isinstance(ref, str):
                        found.append(ref)
                    elif isinstance(ref, dict) and isinstance(ref.get('url'), str):
                        found.append(ref['url'])
            function = item.get('function') if isinstance(item.get('function'), dict) else item
            if isinstance(function.get('name'), str) and function['name'] in {'write_file', 'patch', 'functions.write_file', 'functions.patch'}:
                arguments = function.get('arguments', {})
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        arguments = {}
                if isinstance(arguments, dict) and isinstance(arguments.get('path'), str):
                    found.append(arguments['path'])
            for key, nested in item.items():
                if key in {'attachments', 'output_files', 'files'}:
                    visit(nested, True)
                elif key in {'content', 'text', 'tool_calls', 'output'} or isinstance(nested, (dict, list)):
                    visit(nested)
    visit(value)
    return list(dict.fromkeys(found))


def artifact_payloads(reference, cwd, source, redact):
    """Snapshot a named local artifact, never claim these are historical-at-event bytes."""
    result = {'capture_version': CAPTURE_VERSION, 'native_path': reference,
              'status': 'unavailable', 'snapshot_basis': 'current_file_at_collection',
              'historical_version_verified': False}
    def unavailable(reason):
        return [{**result, 'unavailable_reason': reason}]
    parsed = urlparse(reference)
    if parsed.scheme and parsed.scheme != 'file':
        return unavailable('remote_or_embedded_reference_not_fetched')
    if parsed.scheme == 'file' and parsed.netloc not in {'', 'localhost'}:
        return unavailable('remote_file_reference')
    path = Path(unquote(parsed.path) if parsed.scheme == 'file' else reference).expanduser()
    if not path.is_absolute():
        if not cwd:
            return unavailable('relative_path_without_cwd')
        path = Path(cwd) / path
    target = path.resolve()
    roots = [Path(p).expanduser().resolve() for p in source.get('artifact_roots', [])]
    if cwd and Path(cwd).is_absolute():
        roots.append(Path(cwd).resolve())
    if not any(target.is_relative_to(root) for root in roots):
        return unavailable('outside_authorized_roots')
    if any(p.lower() in {'.ssh', '.gnupg', '.aws', '.azure', '.kube', 'credentials', 'secrets'}
           or re.search(r'(?i)(^\.env($|\.)|credentials|id_rsa|id_ed25519|\.pem$|\.key$|collector\.json$|confirmed-cursors\.json$|state\.db$|config\.ya?ml$|auth\.json$)', p)
           for p in target.parts):
        return unavailable('sensitive_path')
    limit = min(int(source.get('artifact_max_bytes', 20 * 1024 * 1024)), 20 * 1024 * 1024)
    try:
        fd = os.open(target, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        try:
            stream = os.fdopen(fd, 'rb')
        except BaseException:
            os.close(fd)  # fdopen rejects directories before taking ownership.
            raise
        with stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                return unavailable('not_regular_file')
            if before.st_size > limit:
                return unavailable('size_limit')
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            if len(data) > limit:
                return unavailable('size_limit')
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                return unavailable('changed_during_read')
    except FileNotFoundError:
        return unavailable('not_found')
    except PermissionError:
        return unavailable('permission_denied')
    except OSError:
        return unavailable('read_error')
    original_hash = hashlib.sha256(data).hexdigest()
    try:
        text = data.decode('utf-8')
    except UnicodeError:
        redacted = False
    else:
        cleaned = sanitized(text, redact)
        redacted = cleaned != text
        data = cleaned.encode('utf-8')
    digest = hashlib.sha256(data).hexdigest()
    count = max(1, (len(data) + 262143) // 262144)
    return [{**result, 'status': 'captured', 'sha256': digest, 'source_sha256': original_hash,
             'byte_length': len(data), 'source_byte_length': before.st_size,
             'mtime_ns': str(before.st_mtime_ns), 'content_state': 'redacted' if redacted else 'full',
             'chunk_index': i, 'chunk_count': count,
             'data_base64': base64.b64encode(data[i*262144:(i+1)*262144]).decode('ascii')}
            for i in range(count)]
