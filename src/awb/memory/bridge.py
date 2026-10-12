"""Read-only archive bridge, using snapshot keys rather than remote sequence watermarks."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .extraction import ENVELOPE, MAX_SOURCE_BYTES, SECRET

BRIDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_bridge_seen(
 device_id TEXT NOT NULL,agent TEXT NOT NULL,native_id TEXT NOT NULL,
 fact_id TEXT NOT NULL,checksum TEXT NOT NULL,result TEXT NOT NULL,
 PRIMARY KEY(device_id,agent,native_id,fact_id,checksum)
);
"""


def _readonly(path: Path) -> str:
    return path.resolve().as_uri() + '?mode=ro'


class ArchiveBridge:
    def __init__(self, store, archive_db, *, scope='personal'):
        self.store = store
        self.archive_path = Path(getattr(archive_db, 'path', archive_db))
        self.scope = scope
        with store.transaction() as conn:
            conn.execute(BRIDGE_SCHEMA)

    def run_once(self, limit: int = 20) -> dict:
        """Fetch at most 20 unseen snapshots. No mutations or collector locks on archive."""
        limit = max(1, min(int(limit), 20))
        result = {'scanned': 0, 'ingested': 0, 'skipped': 0, 'has_more': False,
                  'coverage_gaps': {}, 'processed_total': 0}
        if not self.archive_path.exists():
            result['coverage_gaps']['archive_unavailable'] = 1
            return result
        with closing(sqlite3.connect(_readonly(self.archive_path), uri=True, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('ATTACH DATABASE ? AS memory_seen', (_readonly(self.store.path),))
            try:
                device_row = conn.execute("SELECT value FROM archive_settings WHERE key='local_device_id'").fetchone()
                local_id = device_row[0] if device_row else None
                rows = conn.execute('''SELECT f.* FROM archive_facts f
                    WHERE f.kind='message' AND NOT EXISTS (
                      SELECT 1 FROM memory_seen.memory_bridge_seen s
                      WHERE s.device_id=f.device_id AND s.agent=f.agent AND s.native_id=f.native_id
                        AND s.fact_id=f.fact_id AND s.checksum=f.checksum)
                    ORDER BY f.rowid LIMIT ?''', (limit + 1,)).fetchall()
            except sqlite3.OperationalError as exc:
                if 'no such table' not in str(exc):
                    raise
                result['coverage_gaps']['archive_not_initialized'] = 1
                return result
        result['has_more'] = len(rows) > limit
        for row in rows[:limit]:
            result['scanned'] += 1
            rejection = None
            try:
                payload = json.loads(row['payload_json'])
                if not isinstance(payload, dict):
                    raise ValueError('invalid_payload')
                body = payload.get('body')
                if not isinstance(body, str) or not body.strip():
                    rejection = 'missing_body'
                elif SECRET.search(body):
                    rejection = 'credential_pattern'
                elif ENVELOPE.search(body):
                    rejection = 'instruction_envelope'
                # A source too large to store intact must not become fabricated
                # complete evidence. Archive keeps its full original independently.
                elif len(body.encode('utf-8')) > 64 * 1024:
                    rejection = 'source_exceeds_snapshot_limit'
                if not rejection:
                    role = str(payload.get('role') or 'unknown')
                    complete = not any(payload.get(flag) for flag in ('truncated', 'redacted', 'omitted'))
                    complete = complete and payload.get('capture_state', 'complete') == 'complete'
                    eligible = complete and role == 'user' and len(body.encode('utf-8')) <= MAX_SOURCE_BYTES
                    source = {'agent': row['agent'], 'native_id': row['native_id'],
                              'message_id': str(payload.get('message_id') or row['fact_id']),
                              'device_id': row['device_id'], 'role': role, 'body': body,
                              'scope': self.scope, 'occurred_at': row['occurred_at'],
                              'checksum': hashlib.sha256(body.encode('utf-8')).hexdigest(),
                              'complete': complete, 'capture_state': 'complete' if complete else 'incomplete'}
                    self.store.ingest_source(source, local=bool(row['device_id'] == local_id))
                    result['ingested'] += 1
                    if not eligible:
                        gap = ('non_user_source' if role != 'user' else
                               'incomplete_source' if not complete else 'source_exceeds_extraction_window')
                        result['coverage_gaps'][gap] = result['coverage_gaps'].get(gap, 0) + 1
            except (ValueError, TypeError, KeyError):
                # No body or provider exception details are persisted in diagnostics.
                rejection = 'invalid_source' if not rejection else rejection
            if rejection:
                result['skipped'] += 1
                result['coverage_gaps'][rejection] = result['coverage_gaps'].get(rejection, 0) + 1
            with self.store.transaction() as memory:
                memory.execute('INSERT OR IGNORE INTO memory_bridge_seen VALUES(?,?,?,?,?,?)',
                               (row['device_id'], row['agent'], row['native_id'], row['fact_id'],
                                row['checksum'], rejection or 'ingested'))
        with self.store.connect() as memory:
            result['processed_total'] = memory.execute('SELECT count(*) FROM memory_bridge_seen').fetchone()[0]
        return result
