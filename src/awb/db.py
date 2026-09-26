"""SQLite storage. The NAS service is the sole writer to its own database."""

from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
INSERT INTO schema_version(version) SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM schema_version);

CREATE TABLE IF NOT EXISTS owner(
  id INTEGER PRIMARY KEY CHECK(id=1), password_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS web_sessions(
  token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pairing_codes(
  code_hash TEXT PRIMARY KEY, expires_at INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  consumed_at INTEGER
);
CREATE TABLE IF NOT EXISTS auth_attempts(
  scope TEXT NOT NULL, subject_hash TEXT NOT NULL, attempts INTEGER NOT NULL,
  expires_at INTEGER NOT NULL, PRIMARY KEY(scope,subject_hash)
);
CREATE TABLE IF NOT EXISTS devices(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, os TEXT NOT NULL, environment TEXT NOT NULL,
  token_hash TEXT UNIQUE NOT NULL, registered_at TEXT NOT NULL, last_heartbeat TEXT,
  last_scan TEXT, pending_count INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS sources(
  id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(id),
  agent TEXT NOT NULL CHECK(agent IN ('codex','hermes')),
  profile TEXT NOT NULL, execution_surface TEXT NOT NULL DEFAULT 'unknown',
  capability_json TEXT NOT NULL DEFAULT '{}', last_scan TEXT, last_event TEXT, last_error TEXT,
  created_at TEXT NOT NULL, UNIQUE(device_id,agent,profile)
);
CREATE TABLE IF NOT EXISTS streams(
  collector_id TEXT NOT NULL REFERENCES devices(id), epoch TEXT NOT NULL,
  durable_ack INTEGER NOT NULL DEFAULT 0, projected_ack INTEGER NOT NULL DEFAULT 0,
  server_epoch TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
  PRIMARY KEY(collector_id,epoch)
);
CREATE TABLE IF NOT EXISTS events(
  event_id TEXT PRIMARY KEY, body_hash TEXT NOT NULL, source_id TEXT NOT NULL,
  native_session_id TEXT NOT NULL, fact_kind TEXT NOT NULL, native_fact_id TEXT NOT NULL,
  revision_key TEXT NOT NULL, source_order INTEGER, occurred_at TEXT,
  content_json TEXT NOT NULL, received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_source_order ON events(source_id,source_order);
CREATE INDEX IF NOT EXISTS ix_events_native ON events(source_id,native_session_id,fact_kind,native_fact_id);
CREATE TABLE IF NOT EXISTS deliveries(
  collector_id TEXT NOT NULL, epoch TEXT NOT NULL, seq INTEGER NOT NULL,
  event_id TEXT, body_hash TEXT, status TEXT NOT NULL, receipt_id TEXT NOT NULL,
  reason TEXT, raw_json TEXT, received_at TEXT NOT NULL,
  PRIMARY KEY(collector_id,epoch,seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_delivery_receipt ON deliveries(receipt_id);
CREATE TABLE IF NOT EXISTS ingest_batches(
  batch_id TEXT PRIMARY KEY, collector_id TEXT NOT NULL, epoch TEXT NOT NULL,
  body_hash TEXT NOT NULL, received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
  native_id TEXT NOT NULL, agent TEXT NOT NULL, device_id TEXT,
  title TEXT, cwd TEXT, created_at TEXT, last_activity TEXT,
  content_policy TEXT NOT NULL DEFAULT 'full_content', archived INTEGER NOT NULL DEFAULT 0,
  UNIQUE(source_id,native_id)
);
CREATE INDEX IF NOT EXISTS ix_sessions_activity ON sessions(last_activity);
CREATE TABLE IF NOT EXISTS session_titles(
  session_id TEXT PRIMARY KEY REFERENCES sessions(id),
  user_title TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages(
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
  native_id TEXT NOT NULL, role TEXT NOT NULL, input_origin TEXT NOT NULL,
  body TEXT, content_state TEXT NOT NULL, omission_reason TEXT,
  source_char_count INTEGER, occurred_at TEXT, turn_id TEXT, event_id TEXT NOT NULL,
  source_order INTEGER, finalized INTEGER NOT NULL DEFAULT 1,
  UNIQUE(session_id,native_id)
);
CREATE INDEX IF NOT EXISTS ix_messages_session_time ON messages(session_id,occurred_at,id);
CREATE INDEX IF NOT EXISTS ix_messages_session_turn ON messages(session_id,turn_id,occurred_at,id);
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
  native_id TEXT NOT NULL, device_id TEXT, model TEXT,
  model_attribution TEXT NOT NULL DEFAULT 'unknown', status TEXT NOT NULL,
  start_at TEXT, end_at TEXT, duration_ms INTEGER, duration_basis TEXT,
  parent_run_ref TEXT, event_id TEXT NOT NULL, source_order INTEGER,
  UNIQUE(session_id,native_id)
);
CREATE INDEX IF NOT EXISTS ix_runs_time ON runs(start_at,end_at);
CREATE INDEX IF NOT EXISTS ix_runs_session_native ON runs(session_id,native_id);
CREATE INDEX IF NOT EXISTS ix_runs_device ON runs(device_id);
CREATE TABLE IF NOT EXISTS usage_observations(
  event_id TEXT PRIMARY KEY REFERENCES events(event_id), session_id TEXT NOT NULL,
  usage_key TEXT NOT NULL, semantics TEXT NOT NULL, coverage_scope TEXT NOT NULL,
  model TEXT, provider TEXT, counter_id TEXT, epoch_id TEXT,
  source_order INTEGER, source_time TEXT, origin_time TEXT, verified_zero_origin INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER, output_tokens INTEGER, cached_input_tokens INTEGER,
  reasoning_output_tokens INTEGER, total_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS ix_usage_session ON usage_observations(session_id,source_order);
CREATE TABLE IF NOT EXISTS file_events(
  event_id TEXT PRIMARY KEY REFERENCES events(event_id), session_id TEXT NOT NULL,
  native_path TEXT NOT NULL, logical_root TEXT, relative_path TEXT,
  relation TEXT NOT NULL, operation_status TEXT NOT NULL,
  run_ref TEXT, size_bytes INTEGER, mtime TEXT, checked_at TEXT, evidence_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS path_maps(
  root_id TEXT NOT NULL, environment_id TEXT NOT NULL, native_root TEXT NOT NULL,
  case_sensitive INTEGER NOT NULL DEFAULT 1, map_version INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(root_id,environment_id)
);
CREATE TABLE IF NOT EXISTS tombstones(
  source_id TEXT NOT NULL, native_session_id TEXT NOT NULL,
  deleted_at TEXT NOT NULL, policy_version INTEGER NOT NULL,
  PRIMARY KEY(source_id,native_session_id)
);
CREATE TABLE IF NOT EXISTS handoffs(
  id TEXT PRIMARY KEY, source_session_id TEXT NOT NULL, target_environment_id TEXT NOT NULL,
  created_at TEXT NOT NULL, source_event_count INTEGER NOT NULL,
  manifest_json TEXT NOT NULL, archive_path TEXT NOT NULL, archive_hash TEXT NOT NULL,
  target_session_id TEXT, revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS session_links(
  handoff_id TEXT NOT NULL REFERENCES handoffs(id), source_session_id TEXT NOT NULL,
  target_session_id TEXT NOT NULL, linked_at TEXT NOT NULL,
  PRIMARY KEY(handoff_id,target_session_id)
);
CREATE TABLE IF NOT EXISTS process_samples(
  id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL,
  environment_id TEXT NOT NULL, pid INTEGER NOT NULL, create_time_ms INTEGER NOT NULL,
  sampled_at TEXT NOT NULL, rss_bytes INTEGER, cpu_percent TEXT, process_name TEXT
);
CREATE INDEX IF NOT EXISTS ix_samples_device_time ON process_samples(device_id,sampled_at);
CREATE TABLE IF NOT EXISTS file_check_jobs(
  id TEXT PRIMARY KEY, target_device_id TEXT NOT NULL, target_environment_id TEXT NOT NULL,
  root_id TEXT NOT NULL, relative_path TEXT NOT NULL, operation TEXT NOT NULL,
  map_version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  result_json TEXT
);
CREATE TABLE IF NOT EXISTS blobs(
  hash TEXT PRIMARY KEY, byte_length INTEGER NOT NULL, stored_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blob_intents(
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL, native_session_id TEXT NOT NULL,
  policy_version INTEGER NOT NULL, hash TEXT NOT NULL, byte_length INTEGER NOT NULL,
  expires_at TEXT NOT NULL, fulfilled_at TEXT
);
CREATE TABLE IF NOT EXISTS app_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def initialize(self) -> None:
        with closing(self.connect()) as db:
            # SQLite WAL-reset fixes are present in these official release lines.
            version = sqlite3.sqlite_version_info
            patched = version >= (3, 51, 3) or version in {(3, 50, 7), (3, 44, 6)}
            db.execute("PRAGMA journal_mode=" + ("WAL" if patched else "DELETE"))
            db.execute("PRAGMA synchronous=FULL")
            db.executescript(SCHEMA)
            try:
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(message_id UNINDEXED, body, tokenize='trigram')")
            except sqlite3.OperationalError:
                # Query code uses bounded LIKE when FTS5/trigram is absent.
                pass
            version_row = db.execute("SELECT version FROM schema_version").fetchone()
            if version_row[0] != 1:
                raise RuntimeError(f"Unsupported database schema {version_row[0]}; use a matching app version")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        db = self.connect()
        try:
            yield db
        finally:
            db.close()
