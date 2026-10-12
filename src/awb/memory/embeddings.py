"""Optional bounded semantic index. SQLite state/scopes remain the final authority."""
from __future__ import annotations

import hashlib
import json
import math
import time
from uuid import uuid4

from .extraction import MAX_SOURCE_BYTES, SECRET, ModelCallError, bounded_post, route_url

VECTOR_SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS memory_vector_cache(
       cache_key TEXT PRIMARY KEY,descriptor TEXT NOT NULL,checksum TEXT NOT NULL,
       vector_json TEXT NOT NULL,created_at REAL NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS memory_vector_claims(
       cache_key TEXT PRIMARY KEY,lease_token TEXT NOT NULL,lease_until REAL NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS memory_vector_failures(
       cache_key TEXT PRIMARY KEY,attempts INTEGER NOT NULL,retry_after REAL NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS memory_vectors(
       item_type TEXT NOT NULL,item_id TEXT NOT NULL,descriptor TEXT NOT NULL,
       scope TEXT NOT NULL,checksum TEXT NOT NULL,vector_json TEXT NOT NULL,
       PRIMARY KEY(item_type,item_id,descriptor))''',
    '''CREATE TABLE IF NOT EXISTS memory_vector_progress(
       descriptor TEXT PRIMARY KEY,source_cursor INTEGER NOT NULL DEFAULT 0,
       memory_offset INTEGER NOT NULL DEFAULT 0)''',
)


def initialize_vectors(store) -> None:
    with store.transaction() as conn:
        for statement in VECTOR_SCHEMA:
            conn.execute(statement)


def descriptor(route: dict) -> str:
    route_url(route, 'embeddings')
    dimensions = route.get('dimensions') or None
    if dimensions is not None and (not isinstance(dimensions, int) or not 1 <= dimensions <= 4096):
        raise ModelCallError('invalid_embedding_dimensions')
    return json.dumps({'endpoint': str(route['endpoint']).rstrip('/'),
                       'model': str(route.get('model') or ''), 'dimensions': dimensions},
                      sort_keys=True, separators=(',', ':'))


def _vector(raw: object, expected: int | None = None) -> list[float]:
    if (not isinstance(raw, list) or not 1 <= len(raw) <= 4096
            or expected and len(raw) != expected):
        raise ModelCallError('invalid_embedding_dimensions')
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in raw):
        raise ModelCallError('invalid_embedding_vector')
    values = [float(v) for v in raw]
    if not all(math.isfinite(v) for v in values) or not sum(v*v for v in values):
        raise ModelCallError('invalid_embedding_vector')
    return values


def embed_text(store, text: str, route: dict, *, kind='query_embedding', transport=None) -> list[float]:
    if not isinstance(text, str) or not text.strip() or SECRET.search(text):
        raise ModelCallError('invalid_embedding_input')
    if len(text.encode('utf-8')) > MAX_SOURCE_BYTES:
        raise ModelCallError('embedding_input_too_large')
    space = descriptor(route)
    if not route.get('model'):
        raise ModelCallError('missing_model')
    initialize_vectors(store)
    checksum = hashlib.sha256(text.encode('utf-8')).hexdigest()
    cache_key = hashlib.sha256((space + '\0' + checksum).encode()).hexdigest()
    token, now = str(uuid4()), time.time()
    with store.transaction() as conn:
        cached = conn.execute('SELECT vector_json FROM memory_vector_cache WHERE cache_key=?', (cache_key,)).fetchone()
        if cached:
            return _vector(json.loads(cached[0]), route.get('dimensions'))
        failure = conn.execute('SELECT attempts,retry_after FROM memory_vector_failures WHERE cache_key=?', (cache_key,)).fetchone()
        if failure and failure[0] >= 3:
            raise ModelCallError('embedding_retry_exhausted')
        if failure and failure[1] > now:
            raise ModelCallError('embedding_retry_wait')
        claim = conn.execute('SELECT lease_until FROM memory_vector_claims WHERE cache_key=?', (cache_key,)).fetchone()
        if claim and claim[0] > now:
            raise ModelCallError('embedding_in_progress')
        conn.execute('INSERT OR REPLACE INTO memory_vector_claims VALUES(?,?,?)', (cache_key, token, now + 65))
    call_id, usage_finished = None, False
    try:
        call_id = store.reserve_model_call(kind, str(route['model']), int(route.get('daily_call_limit', 50)))
        body = {'model': route['model'], 'input': text}
        if route.get('dimensions'):
            body['dimensions'] = route['dimensions']
        response = bounded_post(route, 'embeddings', body, transport=transport)
        rows = response.get('data')
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise ModelCallError('invalid_embedding_response')
        values = _vector(rows[0].get('embedding'), route.get('dimensions'))
        usage = response.get('usage') or {}
        inputs = usage.get('prompt_tokens') if isinstance(usage, dict) else None
        if not isinstance(inputs, int) or isinstance(inputs, bool) or inputs < 0:
            inputs = None
        store.finish_model_call(call_id, input_tokens=inputs, output_tokens=0 if inputs is not None else None)
        usage_finished = True
        with store.transaction() as conn:
            lease = conn.execute('SELECT lease_token,lease_until FROM memory_vector_claims WHERE cache_key=?', (cache_key,)).fetchone()
            if not lease or lease[0] != token or lease[1] <= time.time():
                raise ModelCallError('stale_embedding_lease')
            conn.execute('INSERT OR REPLACE INTO memory_vector_cache VALUES(?,?,?,?,?)',
                         (cache_key, space, checksum, json.dumps(values), time.time()))
            conn.execute('DELETE FROM memory_vector_failures WHERE cache_key=?', (cache_key,))
        return values
    except Exception as exc:
        if call_id is not None and not usage_finished:
            error = str(exc) if isinstance(exc, ModelCallError) else 'embedding_failed'
            store.finish_model_call(call_id, error=error)
        if call_id is not None:
            with store.transaction() as conn:
                failure = conn.execute('SELECT attempts FROM memory_vector_failures WHERE cache_key=?', (cache_key,)).fetchone()
                attempts = (failure[0] if failure else 0) + 1
                conn.execute('INSERT OR REPLACE INTO memory_vector_failures VALUES(?,?,?)',
                             (cache_key, attempts, time.time() + min(3600, 30 * (2 ** (attempts - 1)))))
        raise
    finally:
        with store.transaction() as conn:
            conn.execute('DELETE FROM memory_vector_claims WHERE cache_key=? AND lease_token=?', (cache_key, token))


def index_once(store, *, limit=2, transport=None) -> dict:
    """Visit bounded pages; cache by complete model space, and spend <=limit requests."""
    route = store.settings(private=True).get('embedding', {})
    result = {'indexed': 0, 'scanned': 0, 'errors': 0}
    if not route.get('enabled'):
        return result
    space = descriptor(route)
    initialize_vectors(store)
    with store.connect() as conn:
        progress = conn.execute('SELECT source_cursor,memory_offset FROM memory_vector_progress WHERE descriptor=?', (space,)).fetchone()
    source_cursor, memory_offset = tuple(progress) if progress else (0, 0)
    sources = store.sources(limit=20, after=source_cursor)
    memories_result = store.list_memories(state='active', limit=20, offset=memory_offset)
    memories = memories_result.get('items', []) if isinstance(memories_result, dict) else memories_result
    records = ([('memory', row) for row in memories] + [('source', row) for row in sources])
    calls = 0
    for item_type, item in records:
        result['scanned'] += 1
        text = item.get('text') if item_type == 'memory' else item.get('body')
        identifier = item['id']
        if not text or len(text.encode('utf-8')) > MAX_SOURCE_BYTES or SECRET.search(text):
            continue
        if item_type == 'source':
            try:
                store.get_source(identifier, scopes=frozenset({item['scope']}))
            except LookupError:
                continue
        checksum = hashlib.sha256(text.encode('utf-8')).hexdigest()
        with store.connect() as conn:
            exists = conn.execute('SELECT 1 FROM memory_vectors WHERE item_type=? AND item_id=? AND descriptor=? AND checksum=?',
                                  (item_type, identifier, space, checksum)).fetchone()
        if exists:
            continue
        if calls >= max(0, min(int(limit), 2)):
            # Do not advance past work that could not be attempted.
            break
        calls += 1
        try:
            values = embed_text(store, text, route, kind='source_embedding', transport=transport)
            if item_type == 'source':
                try:
                    store.get_source(identifier, scopes=frozenset({item['scope']}))
                except LookupError:
                    continue
            if item_type == 'memory':
                current = store.inspect(identifier, scopes=frozenset({item['scope']}))['memory']
                if current['state'] != 'active' or current['text'] != text:
                    continue
            with store.transaction() as conn:
                conn.execute('INSERT OR REPLACE INTO memory_vectors VALUES(?,?,?,?,?,?)',
                             (item_type, identifier, space, item['scope'], checksum, json.dumps(values)))
            result['indexed'] += 1
        except ModelCallError as exc:
            if str(exc) in {'embedding_retry_wait', 'embedding_retry_exhausted', 'embedding_in_progress'}:
                calls -= 1  # no external request was made; allow later items to progress
            result['errors'] += 1
        except Exception:
            result['errors'] += 1
    else:
        # End of a page advances the inexpensive traversal. New or changed items
        # before the cursor are picked up when the finite pass wraps around.
        source_cursor = int(sources[-1].get('cursor', 0)) if sources else 0
        memory_offset = memory_offset + len(memories) if len(memories) == 20 else 0
        with store.transaction() as conn:
            conn.execute('INSERT OR REPLACE INTO memory_vector_progress VALUES(?,?,?)', (space, source_cursor, memory_offset))
    return result


def rebuild_index(store) -> None:
    """Owner-only explicit retry/index reset; usage reservations are never erased."""
    initialize_vectors(store)
    with store.transaction() as conn:
        for table in ('memory_vectors', 'memory_vector_progress', 'memory_vector_failures'):
            conn.execute('DELETE FROM ' + table)


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return -1
    den = math.sqrt(sum(v*v for v in left) * sum(v*v for v in right))
    return sum(a*b for a, b in zip(left, right)) / den if den else -1


def _hydrate(store, row, scopes: frozenset[str]) -> dict | None:
    try:
        if row['item_type'] == 'memory':
            item = store.inspect(row['item_id'], scopes=scopes)['memory']
            if item['state'] != 'active':
                return None
            text = item['text']
            output = {'type': 'memory', **item}
        else:
            item = store.get_source(row['item_id'], scopes=scopes)
            text = item['body']
            output = {'type': 'source', 'id': item['id'], 'scope': item['scope'],
                      'role': item['role'], 'body': text[:1200], 'agent': item['agent'],
                      'occurred_at': item.get('occurred_at'), 'complete': item['complete'],
                      'truncated': len(text) > 1200}
        if hashlib.sha256(text.encode('utf-8')).hexdigest() != row['checksum']:
            return None
        return output
    except (LookupError, ValueError):
        return None


def hybrid_recall(store, query: str, *, scopes: frozenset[str], limit=6,
                  budget_bytes=12000, include_sources=True, transport=None) -> dict:
    if scopes is None:
        raise ValueError('allowed_scopes_required')
    scopes = frozenset(scopes)
    limit = max(1, min(int(limit), 30))
    budget_bytes = max(512, min(int(budget_bytes), 32000))
    lexical = store.recall(query, scopes=scopes, limit=limit, budget_bytes=budget_bytes,
                           include_sources=include_sources)
    if not isinstance(lexical, dict):
        lexical = {'items': lexical}
    result = dict(lexical)
    result.setdefault('gaps', [])
    route = store.settings(private=True).get('embedding', {})
    if not scopes or not route.get('enabled'):
        result['retrieval'] = 'lexical'
        return result
    try:
        query_vector = embed_text(store, query, route, transport=transport)
        space = descriptor(route)
        marks = ','.join('?' for _ in scopes)
        with store.connect() as conn:
            rows = conn.execute(f'''SELECT * FROM memory_vectors WHERE descriptor=?
                AND scope IN ({marks}) ORDER BY item_type,item_id LIMIT 2001''', (space, *sorted(scopes))).fetchall()
        threshold = max(0.0, min(1.0, float(route.get('threshold', .35))))
        ranked = []
        for row in rows[:2000]:
            if row['item_type'] == 'source' and not include_sources:
                continue
            score = _cosine(query_vector, _vector(json.loads(row['vector_json'])))
            if score >= threshold:
                ranked.append((score, row))
        ranked.sort(key=lambda item: (-item[0], item[1]['item_id']))
        vector_items = []
        for _, row in ranked[:60]:
            item = _hydrate(store, row, scopes)
            if item:
                vector_items.append(item)
        scores, items = {}, {}
        for channel in (lexical.get('items', []), vector_items):
            for rank, item in enumerate(channel, 1):
                key = (item.get('type', 'memory'), item.get('id', item.get('source_id')))
                scores[key] = scores.get(key, 0.0) + 1 / (60 + rank)
                items.setdefault(key, item)
        merged, used = [], 0
        for key in sorted(scores, key=lambda key: (-scores[key], str(key))):
            item = items[key]
            # Re-check final hydration after model latency; tombstones can land
            # while the query embedding is in flight.
            try:
                if item.get('type') == 'source':
                    store.get_source(item.get('id', item.get('source_id')), scopes=scopes)
                else:
                    item = store.hydrate('memory', item['id'], scopes=scopes)
                    if not item:
                        continue
            except (LookupError, ValueError):
                continue
            size = len(json.dumps(item, ensure_ascii=False).encode('utf-8'))
            if used + size > budget_bytes or len(merged) >= limit:
                result['truncated'] = True
                continue
            merged.append(item)
            used += size
        result.update(items=merged, retrieval='hybrid', returned_bytes=used)
        if len(rows) > 2000:
            result['gaps'] = [*result['gaps'], 'vector_scan_truncated']
        return result
    except Exception:
        # Query model, cache or index problems do not break the local read path.
        # Lexical results fetched before a slow failed request can be stale.
        # Hydrate fresh after the failure so intervening deletion is respected.
        result = store.recall(query, scopes=scopes, limit=limit, budget_bytes=budget_bytes,
                              include_sources=include_sources)
        result['retrieval'] = 'lexical_fallback'
        result['gaps'] = [*result.get('gaps', []), 'semantic_retrieval_unavailable']
        return result
