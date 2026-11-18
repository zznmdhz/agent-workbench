"""Transactional content-addressed artifact chunks; never executable file responses."""
import base64
import hashlib
from .db import logical_payload

SCHEMA = '''CREATE TABLE IF NOT EXISTS capture_blob_chunks(
  hash TEXT NOT NULL,chunk_index INTEGER NOT NULL,chunk_count INTEGER NOT NULL,
  byte_length INTEGER NOT NULL,data BLOB NOT NULL,PRIMARY KEY(hash,chunk_index));'''


def project_chunk(conn, payload):
    if payload.get('status') != 'captured':
        return
    digest = payload['sha256']
    index, count, length = payload['chunk_index'], payload['chunk_count'], payload['byte_length']
    if (len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest)
            or not 0 <= index < count <= 80 or not 0 <= length <= 20 * 1024 * 1024):
        raise ValueError('Invalid artifact manifest')
    data = base64.b64decode(payload['data_base64'], validate=True)
    if len(data) > 262144:
        raise ValueError('Artifact chunk exceeds limit')
    old = conn.execute('SELECT data,chunk_count,byte_length FROM capture_blob_chunks WHERE hash=? AND chunk_index=?', (digest, index)).fetchone()
    if old and (bytes(old[0]), old[1], old[2]) != (data, count, length):
        raise ValueError('Conflicting artifact chunk')
    conn.execute('INSERT OR IGNORE INTO capture_blob_chunks VALUES(?,?,?,?,?)', (digest, index, count, length, data))
    rows = conn.execute('SELECT chunk_index,data,chunk_count,byte_length FROM capture_blob_chunks WHERE hash=? ORDER BY chunk_index', (digest,)).fetchall()
    if len(rows) == count:
        content = b''.join(bytes(r[1]) for r in rows)
        if ([r[0] for r in rows] != list(range(count)) or any((r[2], r[3]) != (count, length) for r in rows)
                or len(content) != length or hashlib.sha256(content).hexdigest() != digest):
            raise ValueError('Artifact integrity mismatch')
        conn.execute("INSERT OR IGNORE INTO blobs(hash,byte_length,stored_at) VALUES(?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))", (digest, length))


def read_blob(conn, digest):
    row = conn.execute('SELECT byte_length FROM blobs WHERE hash=?', (digest,)).fetchone()
    if not row:
        raise ValueError('Artifact incomplete or unavailable')
    content = b''.join(bytes(r[0]) for r in conn.execute('SELECT data FROM capture_blob_chunks WHERE hash=? ORDER BY chunk_index', (digest,)))
    if len(content) != row[0] or hashlib.sha256(content).hexdigest() != digest:
        raise ValueError('Artifact integrity mismatch')
    return content


def purge_unreferenced(conn):
    # Called only by explicit owner content deletion. References are immutable facts.
    # Offline callers may supply an ordinary sqlite3 connection, not Database.
    conn.create_function('awb_payload_text', 1, logical_payload, deterministic=True)
    hashes = [r[0] for r in conn.execute('SELECT DISTINCT hash FROM capture_blob_chunks')]
    for digest in hashes:
        ref = conn.execute("SELECT 1 FROM events WHERE fact_kind='artifact.observed' AND json_extract(awb_payload_text(content_json),'$.payload.sha256')=? LIMIT 1", (digest,)).fetchone()
        if not ref:
            conn.execute('DELETE FROM capture_blob_chunks WHERE hash=?', (digest,))
            conn.execute('DELETE FROM blobs WHERE hash=?', (digest,))
