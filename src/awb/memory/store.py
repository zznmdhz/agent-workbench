"""Independent local SQLite authority for memory, evidence and event projections.

NAS transports immutable records; this database and its WAL must stay on local disk.
Every read used by agents accepts a frozen scope set. None is the local owner's view.
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import platform
import re
import secrets
import sqlite3
import subprocess
import tempfile
import time
import uuid
from functools import lru_cache
from pathlib import Path

from .events import (
    SCHEMA_VERSION,
    BudgetExceededError,
    MemoryValidationError,
    StaleRevisionError,
    canonical,
    digest,
    identifier,
    lexical_terms,
    message_key,
    normalize_operation,
    normalize_payload,
    normalize_source,
    source_authority,
    utc_now,
)


class IncompatibleSchemaError(MemoryValidationError):
    pass


@lru_cache(maxsize=1)
def machine_binding():
    """Private clone guard; it is not an authentication credential or logical ID."""
    system = platform.system()
    machine_id = ""
    if system == "Darwin":
        try:
            output = subprocess.run(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True, text=True, timeout=2, check=True).stdout
            match = re.search(r'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]+)"', output)
            machine_id = match[1].lower() if match else ""
        except (OSError, subprocess.SubprocessError):
            pass
    elif system == "Windows":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                machine_id = str(winreg.QueryValueEx(key, "MachineGuid")[0]).lower()
        except (ImportError, OSError):
            pass
    else:
        for filename in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                machine_id = Path(filename).read_text().strip()
                if machine_id:
                    break
            except OSError:
                pass
    # uuid.getnode() can silently generate a random value on each process. Never
    # use it as a clone guard; the hostname fallback stays stable across processes.
    return digest({"platform": system, "machine": machine_id or platform.node()})


_DEFAULTS = {
    "enabled": False, "sync_root": "", "model_daily_call_limit": 50,
    "llm": {"enabled": False, "endpoint": "", "model": "", "api_key": "", "key_env": "", "daily_call_limit": 50},
    "embedding": {"enabled": False, "endpoint": "", "model": "", "api_key": "", "key_env": "", "dimensions": 0, "threshold": 0.35, "daily_call_limit": 50},
}
_DDL = """
CREATE TABLE IF NOT EXISTS memory_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_sources(
 source_order INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
 scope TEXT NOT NULL, message_key TEXT NOT NULL, local INTEGER NOT NULL,
 data TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS memory_sources_scope ON memory_sources(scope);
CREATE TABLE IF NOT EXISTS memory_operations(
 id TEXT PRIMARY KEY, device_id TEXT NOT NULL, seq INTEGER NOT NULL,
 memory_id TEXT NOT NULL, data TEXT NOT NULL, validity TEXT NOT NULL DEFAULT 'pending',
 gap TEXT, UNIQUE(device_id,seq));
CREATE INDEX IF NOT EXISTS memory_operations_lineage ON memory_operations(memory_id);
CREATE TABLE IF NOT EXISTS memory_projections(
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_tombstones(memory_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS memory_source_blocks(
 source_id TEXT NOT NULL, memory_id TEXT NOT NULL, permanent INTEGER NOT NULL,
 PRIMARY KEY(source_id,memory_id));
CREATE TABLE IF NOT EXISTS memory_message_blocks(
 message_key TEXT NOT NULL, memory_id TEXT NOT NULL, PRIMARY KEY(message_key,memory_id));
CREATE TABLE IF NOT EXISTS memory_terms(
 entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, term TEXT NOT NULL,
 PRIMARY KEY(entity_type,entity_id,term));
CREATE INDEX IF NOT EXISTS memory_terms_lookup ON memory_terms(term);
CREATE TABLE IF NOT EXISTS memory_jobs(
 source_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 lease_token TEXT, lease_until REAL, next_run REAL NOT NULL DEFAULT 0, error TEXT, checksum TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_model_calls(
 id TEXT PRIMARY KEY, device_id TEXT NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL,
 model TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
 input_tokens INTEGER, output_tokens INTEGER, error TEXT);
CREATE INDEX IF NOT EXISTS memory_model_day ON memory_model_calls(device_id,day);
CREATE TABLE IF NOT EXISTS memory_packets(
 digest TEXT PRIMARY KEY, device_id TEXT NOT NULL, packet_seq INTEGER NOT NULL,
 imported_at TEXT NOT NULL, UNIQUE(device_id,packet_seq));
CREATE TABLE IF NOT EXISTS memory_sync_lease(id INTEGER PRIMARY KEY CHECK(id=1), token TEXT, until REAL);
"""


class MemoryStore:
    def __init__(self, path: str | Path, *, device_id: str | None = None):
        self.path = Path(path).expanduser().absolute()
        self.config_path = self.path.with_name(self.path.name + ".private.json")
        self._device_hint = identifier(device_id, "device_id") if device_id else None

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise IncompatibleSchemaError("memory schema is newer than this application")
            version_info = sqlite3.sqlite_version_info
            patched = version_info >= (3, 51, 3) or version_info in {(3, 50, 7), (3, 44, 6)}
            conn.execute("PRAGMA journal_mode=" + ("WAL" if patched else "DELETE"))
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript(_DDL)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.execute("INSERT OR IGNORE INTO memory_metadata VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
            conn.execute("INSERT OR IGNORE INTO memory_metadata VALUES('device_id',?)", (self._device_hint or str(uuid.uuid4()),))
            if self._device_hint and self._meta(conn, "device_id") != self._device_hint:
                raise MemoryValidationError("replica_identity_mismatch_use_restore: do not copy a live writer database")
            conn.execute("INSERT OR IGNORE INTO memory_metadata VALUES('writer_seq','0')")
            conn.execute("INSERT OR IGNORE INTO memory_metadata VALUES('export_seq','0')")
            conn.execute("INSERT OR IGNORE INTO memory_metadata VALUES('packet_seq','0')")
            conn.commit()
            if not self._meta(conn, "space_id") and not conn.execute("SELECT 1 FROM memory_operations LIMIT 1").fetchone():
                config = self._load_config()
                if not config.get("writer_binding"):
                    config["writer_binding"] = machine_binding()
                    self._write_config(config)
        return self

    def connect_raw(self):
        conn = sqlite3.connect(str(self.path), timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=20000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextlib.contextmanager
    def connect(self):
        conn = self.connect_raw()
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def transaction(self):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def _meta(conn, key, default=None):
        row = conn.execute("SELECT value FROM memory_metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    @staticmethod
    def _set_meta(conn, key, value):
        conn.execute("INSERT INTO memory_metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    @property
    def device_id(self):
        with self.connect() as conn:
            return self._meta(conn, "device_id")

    @property
    def space_id(self):
        with self.connect() as conn:
            return self._meta(conn, "space_id")

    def _load_config(self):
        values = json.loads(json.dumps(_DEFAULTS))
        if self.config_path.exists():
            supplied = json.loads(self.config_path.read_text(encoding="utf-8"))
            for key in ("enabled", "sync_root", "model_daily_call_limit", "space_id", "shared_key", "writer_binding"):
                if key in supplied:
                    values[key] = supplied[key]
            for key in ("llm", "embedding"):
                values[key].update(supplied.get(key, {}))
        return values

    def _write_config(self, values):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".memory-private-", dir=self.config_path.parent)
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(canonical(values))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.config_path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def settings(self, *, private=False):
        values = self._load_config()
        if private:
            return values
        values.pop("shared_key", None)
        values.pop("space_id", None)
        values.pop("writer_binding", None)
        for key in ("llm", "embedding"):
            route = values[key]
            route["has_key"] = bool(route.pop("api_key", "") or route.pop("key", "")) or bool(route.get("key_env") and os.environ.get(route["key_env"]))
            route.pop("key", None)
        values["has_join_key"] = self._has_key()
        return values

    def private_settings(self):
        return self.settings(private=True)

    def update_settings(self, supplied: dict):
        if not isinstance(supplied, dict) or set(supplied) - set(_DEFAULTS):
            raise MemoryValidationError("unknown memory setting")
        values = self._load_config()
        for key, value in supplied.items():
            if key in {"llm", "embedding"}:
                if not isinstance(value, dict):
                    raise MemoryValidationError("invalid model settings")
                allowed = set(_DEFAULTS[key]) | {"key"}
                if set(value) - allowed:
                    raise MemoryValidationError("unknown model setting")
                for field, item in value.items():
                    if field == "key":
                        field = "api_key"
                    if field == "enabled" and type(item) is not bool:
                        raise MemoryValidationError("enabled must be boolean")
                    if field in {"daily_call_limit", "dimensions"} and (type(item) is not int or not 0 <= item <= 100000):
                        raise MemoryValidationError("invalid model limit")
                    if field == "threshold" and (not isinstance(item, (int, float)) or not 0 <= item <= 1):
                        raise MemoryValidationError("invalid threshold")
                    if field in {"endpoint", "model", "api_key", "key_env"} and (not isinstance(item, str) or len(item) > 4096):
                        raise MemoryValidationError("invalid model string")
                    values[key][field] = item
            elif key == "enabled":
                if type(value) is not bool:
                    raise MemoryValidationError("enabled must be boolean")
                values[key] = value
            elif key == "model_daily_call_limit":
                if type(value) is not int or not 0 <= value <= 100000:
                    raise MemoryValidationError("invalid daily budget")
                values[key] = value
            elif key == "sync_root":
                if not isinstance(value, str) or len(value) > 4096:
                    raise MemoryValidationError("invalid sync root")
                values[key] = value
        self._write_config(values)
        return self.settings()

    def _has_key(self):
        values = self._load_config()
        return bool(values.get("shared_key") and values.get("space_id") == self.space_id)

    def writer_available(self):
        return self._load_config().get("writer_binding") == machine_binding()

    def _require_writer(self):
        if not self.writer_available():
            raise MemoryValidationError("replica_binding_mismatch_use_restore: this copy is read-only; restore to a new writer")

    def shared_key(self):
        self._require_writer()
        values = self._load_config()
        if not self._has_key():
            raise MemoryValidationError("private join credential required; restored copy is offline-only")
        try:
            key = base64.urlsafe_b64decode(values["shared_key"])
        except Exception as error:
            raise MemoryValidationError("invalid private join credential") from error
        if len(key) != 32:
            raise MemoryValidationError("invalid shared key")
        return key

    def configure_space(self, mode, join_code=None):
        self._require_writer()
        if mode not in {"create", "join"}:
            raise MemoryValidationError("space mode must be create or join")
        values = self._load_config()
        if mode == "create":
            space_id, key = str(uuid.uuid4()), secrets.token_bytes(32)
        else:
            try:
                credential = json.loads(base64.urlsafe_b64decode(str(join_code).strip()).decode("utf-8"))
                if set(credential) != {"version", "space_id", "key"} or credential["version"] != 1:
                    raise ValueError("credential version")
                space_id = str(uuid.UUID(credential["space_id"]))
                key = base64.urlsafe_b64decode(credential["key"])
                if len(key) != 32:
                    raise ValueError("key length")
            except Exception as error:
                raise MemoryValidationError("invalid private join credential") from error
        with self.transaction() as conn:
            prior = self._meta(conn, "space_id")
            if mode == "join" and prior == space_id and not self._has_key():
                values.update(space_id=space_id, shared_key=base64.urlsafe_b64encode(key).decode("ascii"))
                self._write_config(values)
                return self._status_conn(conn)
            if self._meta(conn, "space_id") or conn.execute("SELECT 1 FROM memory_sources UNION SELECT 1 FROM memory_operations LIMIT 1").fetchone():
                raise MemoryValidationError("joining or creating requires an empty unconfigured space")
            # Write credential before commit. If commit fails, _has_key checks the DB identity.
            values.update(space_id=space_id, shared_key=base64.urlsafe_b64encode(key).decode("ascii"))
            self._write_config(values)
            self._set_meta(conn, "space_id", space_id)
        return self.status()

    def reset_replica_identity(self, new_device_id=None):
        """A restored replica keeps logical records but starts a new writer stream."""
        new_device_id = identifier(new_device_id or str(uuid.uuid4()), "device_id")
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM memory_operations WHERE device_id=? LIMIT 1", (new_device_id,)).fetchone():
                raise MemoryValidationError("replica writer identity has already been used")
            self._set_meta(conn, "device_id", new_device_id)
            for key in ("writer_seq", "export_seq", "packet_seq", "export_source_cursor"):
                self._set_meta(conn, key, "0")
            conn.execute("UPDATE memory_sources SET local=0")
            conn.execute("UPDATE memory_jobs SET status='suppressed',lease_token=NULL,lease_until=NULL")
            conn.execute("DELETE FROM memory_sync_lease")
            self._write_config(json.loads(json.dumps(_DEFAULTS)) | {"writer_binding": machine_binding()})
        return self.status()

    def join_code(self):
        return base64.urlsafe_b64encode(canonical({"version": 1, "space_id": self.space_id, "key": base64.urlsafe_b64encode(self.shared_key()).decode("ascii")})).decode("ascii")

    @staticmethod
    def _scopes(scopes):
        if scopes is not None and not isinstance(scopes, frozenset):
            raise MemoryValidationError("agent scopes must be a frozen set")
        return scopes

    @classmethod
    def _visible(cls, scope, scopes):
        cls._scopes(scopes)
        return scopes is None or scope in scopes

    @staticmethod
    def _blocked(conn, source_id):
        return bool(conn.execute("SELECT 1 FROM memory_source_blocks WHERE source_id=? UNION SELECT 1 FROM memory_sources s WHERE s.id=? AND EXISTS(SELECT 1 FROM memory_message_blocks b WHERE b.message_key=s.message_key) LIMIT 1", (source_id, source_id)).fetchone())

    def source_available(self, source_id):
        with self.connect() as conn:
            row = conn.execute("SELECT local,data FROM memory_sources WHERE id=?", (source_id,)).fetchone()
            data = json.loads(row["data"]) if row else {}
            return bool(row and row["local"] and data["complete"] and data["role"] == "user" and not self._blocked(conn, source_id))

    @staticmethod
    def _source_row(row):
        if row is None:
            raise LookupError("source unavailable")
        return json.loads(row["data"]) | {"local": bool(row["local"]), "cursor": row["source_order"]}

    def _get_source(self, conn, source_id, *, scopes=None):
        row = conn.execute("SELECT * FROM memory_sources WHERE id=?", (source_id,)).fetchone()
        if row is None or not self._visible(row["scope"], scopes) or (scopes is not None and self._blocked(conn, source_id)):
            raise LookupError("source unavailable")
        return self._source_row(row)

    def get_source(self, source_id, *, scopes=None):
        self._scopes(scopes)
        with self.connect() as conn:
            return self._get_source(conn, source_id, scopes=scopes)

    def sources(self, *, limit=50, after=0, scopes=None):
        self._scopes(scopes)
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM memory_sources WHERE source_order>? ORDER BY source_order", (int(after or 0),))
            result = []
            for row in rows:
                if self._visible(row["scope"], scopes) and (scopes is None or not self._blocked(conn, row["id"])):
                    result.append(self._source_row(row))
                if len(result) >= min(max(int(limit), 1), 1000):
                    break
            return result

    def _ingest_source(self, conn, source, local):
        source = normalize_source(source, device_id=self._meta(conn, "device_id"))
        existing = conn.execute("SELECT * FROM memory_sources WHERE id=?", (source["id"],)).fetchone()
        if existing:
            if source_authority(json.loads(existing["data"])) != source_authority(source):
                raise MemoryValidationError("same source ID has different authoritative data")
            # Original local ownership never changes because an imported copy is encountered.
            return self._source_row(existing), False
        conn.execute("INSERT INTO memory_sources(id,scope,message_key,local,data,created_at) VALUES(?,?,?,?,?,?)", (source["id"], source["scope"], message_key(source), int(local), canonical(source).decode(), utc_now()))
        # A tombstone can arrive before its evidence snapshot. Once that snapshot
        # arrives, bind the permanent block to the whole logical message lineage.
        for row in conn.execute("SELECT memory_id FROM memory_source_blocks WHERE source_id=? AND permanent=1", (source["id"],)).fetchall():
            conn.execute("INSERT OR IGNORE INTO memory_message_blocks VALUES(?,?)", (message_key(source), row[0]))
        conn.executemany("INSERT OR IGNORE INTO memory_terms VALUES('source',?,?)", [(source["id"], term) for term in lexical_terms(source["body"])])
        if local and source["complete"] and source["role"] == "user" and not self._blocked(conn, source["id"]):
            conn.execute("INSERT OR IGNORE INTO memory_jobs(source_id,checksum) VALUES(?,?)", (source["id"], source["checksum"]))
        return self._get_source(conn, source["id"]), True

    def ingest_source(self, source, local=True):
        self._require_writer()
        with self.transaction() as conn:
            result, _ = self._ingest_source(conn, source, local)
            # Pending operations may become valid when their independently arriving evidence arrives.
            affected = {row[0] for row in conn.execute("SELECT DISTINCT memory_id FROM memory_operations WHERE validity='pending'")}
            for memory_id in affected:
                self._rebuild_lineage(conn, memory_id)
            return result

    def _validate_evidence(self, conn, payload, *, scopes=None, allow_blocked=False, allow_missing=False, processor=False):
        for evidence in payload["evidence"]:
            row = conn.execute("SELECT * FROM memory_sources WHERE id=?", (evidence["source_id"],)).fetchone()
            if row is None:
                if allow_missing:
                    return False
                raise LookupError("source unavailable")
            source = self._source_row(row)
            if not self._visible(source["scope"], scopes):
                raise LookupError("source unavailable")
            if source["scope"] != payload["scope"]:
                raise MemoryValidationError("memory and all evidence scopes must match")
            if not source["complete"] or evidence["quote"] not in source["body"]:
                raise MemoryValidationError("evidence must quote a complete immutable source")
            if not allow_blocked and self._blocked(conn, source["id"]):
                raise MemoryValidationError("source is suppressed by a disabled/deleted lineage")
            if processor and (not source["local"] or source["role"] != "user"):
                raise MemoryValidationError("automatic processing requires a local user source")
        return True

    def _new_operation(self, conn, memory_id, action, actor, payload, parents):
        seq = int(self._meta(conn, "writer_seq", "0")) + 1
        self._set_meta(conn, "writer_seq", seq)
        return normalize_operation({"id": str(uuid.uuid4()), "device_id": self._meta(conn, "device_id"), "seq": seq, "memory_id": memory_id, "parents": parents, "action": action, "actor": actor, "occurred_at": utc_now(), "payload": payload})

    def _insert_operation(self, conn, operation):
        operation = normalize_operation(operation)
        existing = conn.execute("SELECT data FROM memory_operations WHERE id=? OR (device_id=? AND seq=?)", (operation["id"], operation["device_id"], operation["seq"])).fetchall()
        encoded = canonical(operation).decode()
        if existing:
            if any(row[0] != encoded for row in existing):
                raise MemoryValidationError("operation ID or writer sequence was rewritten")
            return False
        for parent in operation["parents"]:
            row = conn.execute("SELECT memory_id FROM memory_operations WHERE id=?", (parent,)).fetchone()
            if row and row[0] != operation["memory_id"]:
                raise MemoryValidationError("parent belongs to another lineage")
        # Check prospective graph, including children that arrived before their parent.
        adjacency = {row["id"]: json.loads(row["data"])["parents"] for row in conn.execute("SELECT id,data FROM memory_operations")}
        adjacency[operation["id"]] = operation["parents"]
        stack, visited = [(operation["id"], False)], set()
        walking = set()
        while stack:
            current, leaving = stack.pop()
            if leaving:
                walking.discard(current)
                visited.add(current)
                continue
            if current in walking:
                raise MemoryValidationError("operation graph cycle")
            if current in visited:
                continue
            walking.add(current)
            stack.append((current, True))
            stack.extend((parent, False) for parent in adjacency.get(current, []))
        conn.execute("INSERT INTO memory_operations(id,device_id,seq,memory_id,data) VALUES(?,?,?,?,?)", (operation["id"], operation["device_id"], operation["seq"], operation["memory_id"], encoded))
        if operation["action"] == "delete":
            conn.execute("INSERT OR IGNORE INTO memory_tombstones VALUES(?)", (operation["memory_id"],))
        return True

    def _propose(self, conn, text, kind, scope, evidence, actor, memory_id, scopes):
        self._require_writer()
        if not self._meta(conn, "space_id"):
            raise MemoryValidationError("create or join a memory space first")
        if scopes is not None and (actor != "assistant_proposal" or memory_id is not None):
            raise MemoryValidationError("agents can only propose a new candidate")
        if not self._visible(scope, scopes):
            raise LookupError("scope unavailable")
        if not evidence and actor == "assistant_proposal":
            native = str(uuid.uuid4())
            source, _ = self._ingest_source(conn, {"agent": "assistant_proposal", "native_id": native, "message_id": native, "role": "assistant", "body": text, "scope": scope, "complete": True}, False)
            evidence = [{"source_id": source["id"], "quote": text}]
        payload = normalize_payload({"text": text, "kind": kind, "scope": scope, "evidence": evidence})
        self._validate_evidence(conn, payload, scopes=scopes, processor=actor == "processor")
        memory_id = identifier(memory_id, "memory_id") if memory_id else (digest(payload) if actor == "processor" else str(uuid.uuid4()))
        old = conn.execute("SELECT data FROM memory_projections WHERE id=?", (memory_id,)).fetchone()
        if old:
            projection = json.loads(old[0])
            if actor == "processor" and all(projection.get(key) == payload[key] for key in payload):
                return projection
            raise MemoryValidationError("propose requires a new memory lineage")
        operation = self._new_operation(conn, memory_id, "propose", actor, payload, [])
        self._insert_operation(conn, operation)
        return self._rebuild_lineage(conn, memory_id)

    def propose(self, text, kind, scope, evidence=None, actor="processor", memory_id=None, *, scopes=None):
        self._scopes(scopes)
        with self.transaction() as conn:
            return self._propose(conn, text, kind, scope, evidence, actor, memory_id, scopes)

    def propose_from_job(self, source_id, lease_token, candidates):
        if not isinstance(candidates, list) or len(candidates) > 8:
            raise MemoryValidationError("too many candidates")
        with self.transaction() as conn:
            self._validate_lease(conn, source_id, lease_token)
            source = self._get_source(conn, source_id)
            if self._blocked(conn, source_id):
                raise MemoryValidationError("source no longer available")
            results = []
            for candidate in candidates:
                if any(item.get("source_id") != source_id for item in candidate.get("evidence", [])) or candidate.get("scope", source["scope"]) != source["scope"]:
                    raise MemoryValidationError("job candidate escaped its fixed source")
                results.append(self._propose(conn, candidate["text"], candidate["kind"], source["scope"], candidate["evidence"], "processor", None, None))
            return results

    def change(self, memory_id, action, payload=None, *, parents):
        self._require_writer()
        if not isinstance(parents, list) or not parents:
            raise MemoryValidationError("parents must be the heads shown to the owner")
        with self.transaction() as conn:
            return self._change(conn, memory_id, action, payload, parents)

    def _change(self, conn, memory_id, action, payload, parents):
        row = conn.execute("SELECT data FROM memory_projections WHERE id=?", (memory_id,)).fetchone()
        if not row:
            raise LookupError("memory unavailable")
        current = json.loads(row[0])
        if sorted(parents) != sorted(current["heads"]):
            raise StaleRevisionError("stale_revision: reload memory before changing it")
        if current["state"] == "deleted":
            raise MemoryValidationError("deleted lineage is permanent; create a new owner lineage")
        if action not in {"confirm", "edit", "disable", "enable", "reject", "resolve", "delete"}:
            raise MemoryValidationError("invalid owner action")
        if action in {"edit", "resolve"}:
            payload = normalize_payload(payload or {}) | ({"state": payload.get("state")} if action == "resolve" else {})
            if payload["scope"] != current["scope"]:
                raise MemoryValidationError("scope changes require a new owner source and lineage")
            self._validate_evidence(conn, payload, allow_blocked=action == "resolve")
        elif action in {"delete", "disable"}:
            # A parent may be delivered later. Carry the fixed evidence needed
            # to suppress the associated source immediately on every replica.
            payload = {key: current[key] for key in ("text", "kind", "scope", "evidence")}
            blocked_sources = {item["source_id"] for item in current["evidence"]}
            for row in conn.execute("SELECT data FROM memory_operations WHERE memory_id=?", (memory_id,)):
                old_payload = json.loads(row[0])["payload"]
                blocked_sources.update(item["source_id"] for item in old_payload.get("evidence", []))
                blocked_sources.update(old_payload.get("blocked_sources", []))
            payload["blocked_sources"] = sorted(blocked_sources)
        else:
            payload = payload or {}
        operation = self._new_operation(conn, memory_id, action, "owner", payload, parents)
        self._insert_operation(conn, operation)
        result = self._rebuild_lineage(conn, memory_id)
        validity = conn.execute("SELECT validity,gap FROM memory_operations WHERE id=?", (operation["id"],)).fetchone()
        if validity["validity"] != "valid":
            raise MemoryValidationError(validity["gap"] or "invalid state transition")
        return result


    def change_owner_text(self, memory_id, action, text, kind, *, parents, state=None):
        """Atomically verify heads, create owner evidence and apply a revision.

        A rejected/stale owner request leaves no orphan source discoverable by agents.
        """
        self._require_writer()
        if action not in {"edit", "resolve"}:
            raise MemoryValidationError("owner text revision must edit or resolve")
        if not isinstance(parents, list) or not parents:
            raise MemoryValidationError("parents must be the heads shown to the owner")
        with self.transaction() as conn:
            row = conn.execute("SELECT data FROM memory_projections WHERE id=?", (memory_id,)).fetchone()
            if not row:
                raise LookupError("memory unavailable")
            current = json.loads(row[0])
            if sorted(parents) != sorted(current["heads"]):
                raise StaleRevisionError("stale_revision: reload memory before changing it")
            native = str(uuid.uuid4())
            source, _ = self._ingest_source(conn, {"agent": "owner", "native_id": native,
                "message_id": native, "role": "user", "body": text,
                "scope": current["scope"], "complete": True}, False)
            payload = {"text": text, "kind": kind, "scope": current["scope"],
                "evidence": [{"source_id": source["id"], "quote": text}]}
            if action == "resolve":
                payload["state"] = state
            return self._change(conn, memory_id, action, payload, parents)

    @staticmethod
    def _node_signature(node):
        return canonical({"state": node["state"], "resume_state": node.get("resume_state"), "payload": node["payload"]})

    def _rebuild_lineage(self, conn, memory_id):
        rows = conn.execute("SELECT data FROM memory_operations WHERE memory_id=?", (memory_id,)).fetchall()
        ops = {json.loads(row[0])["id"]: json.loads(row[0]) for row in rows}
        pending, valid, invalid = set(ops), {}, {}
        while pending:
            progress = False
            for op_id in sorted(pending):
                op = ops[op_id]
                parents = op["parents"]
                foreign = [parent for parent in parents if parent not in ops and conn.execute("SELECT 1 FROM memory_operations WHERE id=?", (parent,)).fetchone()]
                if foreign or any(parent in invalid for parent in parents):
                    invalid[op_id] = "invalid parent lineage or parent operation"
                elif any(parent not in valid for parent in parents):
                    continue
                else:
                    try:
                        action = op["action"]
                        if action in {"propose", "edit", "resolve", "delete", "disable"} and not self._validate_evidence(conn, op["payload"], allow_missing=True, allow_blocked=True):
                            continue
                        if action == "propose":
                            node = {"payload": op["payload"], "state": "candidate"}
                        else:
                            nodes = [valid[parent] for parent in parents]
                            if action == "resolve":
                                if any(node["state"] == "deleted" for node in nodes):
                                    raise MemoryValidationError("deleted lineage cannot be resolved or restored")
                                if any(node["payload"]["scope"] != op["payload"]["scope"] for node in nodes):
                                    raise MemoryValidationError("scope changes require new lineage")
                                node = {"payload": {key: value for key, value in op["payload"].items() if key != "state"}, "state": op["payload"]["state"]}
                            elif action == "delete":
                                if any(node["state"] == "deleted" for node in nodes):
                                    raise MemoryValidationError("deleted lineage is already permanent")
                                if any(node["payload"]["scope"] != op["payload"]["scope"] for node in nodes):
                                    raise MemoryValidationError("scope changes require new lineage")
                                node = {"payload": nodes[0]["payload"], "state": "deleted"}
                            else:
                                if len({self._node_signature(item) for item in nodes}) != 1:
                                    raise MemoryValidationError("conflicting parents require resolve")
                                base = nodes[0]
                                state = base["state"]
                                node = {"payload": base["payload"], "state": state}
                                if action == "confirm" and state == "candidate":
                                    node["state"] = "active"
                                elif action == "edit" and state in {"active", "candidate"}:
                                    if op["payload"]["scope"] != base["payload"]["scope"]:
                                        raise MemoryValidationError("scope changes require new lineage")
                                    node["payload"] = op["payload"]
                                elif action == "disable" and state in {"active", "candidate"}:
                                    if op["payload"]["scope"] != base["payload"]["scope"]:
                                        raise MemoryValidationError("scope changes require new lineage")
                                    node.update(state="disabled", resume_state=state)
                                elif action == "enable" and state == "disabled":
                                    node["state"] = base.get("resume_state", "candidate")
                                elif action == "reject" and state == "candidate":
                                    node["state"] = "rejected"
                                else:
                                    raise MemoryValidationError("invalid state transition")
                        valid[op_id] = node
                    except (MemoryValidationError, LookupError) as error:
                        invalid[op_id] = str(error)
                pending.remove(op_id)
                progress = True
            if not progress:
                break
        for op_id in ops:
            state = "valid" if op_id in valid else "invalid" if op_id in invalid else "pending"
            conn.execute("UPDATE memory_operations SET validity=?,gap=? WHERE id=?", (state, invalid.get(op_id) or ("missing parent or source evidence" if state == "pending" else None), op_id))
        parent_ids = {parent for op_id in valid for parent in ops[op_id]["parents"] if parent in valid}
        heads = sorted(set(valid) - parent_ids)
        tombstoned = bool(conn.execute("SELECT 1 FROM memory_tombstones WHERE memory_id=?", (memory_id,)).fetchone())
        all_payloads = [op["payload"] for op in ops.values() if op["payload"].get("evidence")]
        sample = valid[heads[0]]["payload"] if heads else all_payloads[0] if all_payloads else {"text": "", "kind": "fact", "scope": "", "evidence": []}
        signatures = {self._node_signature(valid[head]) for head in heads}
        state = "deleted" if tombstoned else "conflict" if len(signatures) > 1 else valid[heads[0]]["state"] if heads else "pending"
        if tombstoned:
            # A missing-parent delete immediately dominates and becomes a head for owner display.
            heads = sorted(set(heads) | {op_id for op_id, op in ops.items() if op["action"] == "delete" and op_id in pending})
        result = dict(sample) | {"id": memory_id, "state": state, "heads": heads, "updated_at": max((op["occurred_at"] for op in ops.values()), default=""), "gaps": [{"op_id": op_id, "reason": invalid.get(op_id, "missing parent or source evidence")} for op_id in sorted(pending | set(invalid))]}
        if state == "conflict":
            result["branches"] = [dict(valid[head]["payload"]) | {"head": head, "state": valid[head]["state"]} for head in heads]
        if state == "disabled" and heads:
            result["resume_state"] = valid[heads[0]].get("resume_state", "candidate")
        conn.execute("INSERT INTO memory_projections VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET scope=excluded.scope,state=excluded.state,data=excluded.data", (memory_id, sample["scope"], state, canonical(result).decode()))
        conn.execute("DELETE FROM memory_source_blocks WHERE memory_id=? AND permanent=0", (memory_id,))
        suppress = tombstoned or state == "disabled" or any(valid[head]["state"] == "disabled" for head in heads if head in valid) or any(ops[op_id]["action"] == "disable" for op_id in pending)
        if suppress:
            for payload in all_payloads:
                suppressed_ids = {item["source_id"] for item in payload["evidence"]} | set(payload.get("blocked_sources", []))
                for source_id in suppressed_ids:
                    conn.execute("INSERT INTO memory_source_blocks VALUES(?,?,?) ON CONFLICT(source_id,memory_id) DO UPDATE SET permanent=max(permanent,excluded.permanent)", (source_id, memory_id, int(tombstoned)))
                    if tombstoned:
                        row = conn.execute("SELECT message_key FROM memory_sources WHERE id=?", (source_id,)).fetchone()
                        if row:
                            conn.execute("INSERT OR IGNORE INTO memory_message_blocks VALUES(?,?)", (row[0], memory_id))
        conn.execute("DELETE FROM memory_terms WHERE entity_type='memory' AND entity_id=?", (memory_id,))
        if state == "active":
            conn.executemany("INSERT OR IGNORE INTO memory_terms VALUES('memory',?,?)", [(memory_id, term) for term in lexical_terms(sample["text"])])
        return result

    def rebuild(self):
        with self.transaction() as conn:
            conn.execute("DELETE FROM memory_terms")
            for row in conn.execute("SELECT id,data FROM memory_sources").fetchall():
                conn.executemany("INSERT OR IGNORE INTO memory_terms VALUES('source',?,?)", [(row["id"], term) for term in lexical_terms(json.loads(row["data"])["body"])])
            for row in conn.execute("SELECT DISTINCT memory_id FROM memory_operations").fetchall():
                self._rebuild_lineage(conn, row[0])
        return self.status()

    def _memory_visible(self, conn, memory, scopes, *, management=False):
        if not self._visible(memory["scope"], scopes):
            return False
        if management and scopes is None:
            return True
        if memory["state"] in {"disabled", "deleted"}:
            return False
        return all(not self._blocked(conn, item["source_id"]) for item in memory["evidence"])

    def list_memories(self, query="", state=None, scope=None, limit=50, offset=0, *, scopes=None):
        self._scopes(scopes)
        with self.connect() as conn:
            items = []
            for row in conn.execute("SELECT data FROM memory_projections ORDER BY id"):
                item = json.loads(row[0])
                if (state and item["state"] != state) or (scope and item["scope"] != scope) or (query and query.lower() not in item["text"].lower()):
                    continue
                if self._memory_visible(conn, item, scopes, management=True):
                    items.append(item)
            total = len(items)
            return {"items": items[max(int(offset), 0):max(int(offset), 0) + min(max(int(limit), 1), 500)], "total": total}

    def inspect(self, memory_id, *, scopes=None):
        self._scopes(scopes)
        with self.connect() as conn:
            row = conn.execute("SELECT data FROM memory_projections WHERE id=?", (memory_id,)).fetchone()
            if not row:
                raise LookupError("memory unavailable")
            memory = json.loads(row[0])
            if not self._memory_visible(conn, memory, scopes, management=True):
                raise LookupError("memory unavailable")
            operations = [json.loads(row["data"]) | {"validity": row["validity"], "gap": row["gap"]} for row in conn.execute("SELECT data,validity,gap FROM memory_operations WHERE memory_id=? ORDER BY id", (memory_id,))]
            evidence_ids = {item["source_id"] for op in operations for item in op["payload"].get("evidence", [])}
            if scopes is not None and any(self._blocked(conn, source_id) for source_id in evidence_ids):
                raise LookupError("memory unavailable")
            sources = [self._get_source(conn, source_id, scopes=scopes) for source_id in sorted(evidence_ids) if conn.execute("SELECT 1 FROM memory_sources WHERE id=?", (source_id,)).fetchone()]
            return {"memory": memory, "operations": operations, "sources": sources}

    def _recall_item(self, conn, entity_type, entity_id, scopes):
        if entity_type == "source":
            if self._blocked(conn, entity_id):
                return None
            try:
                source = self._get_source(conn, entity_id, scopes=scopes)
            except LookupError:
                return None
            excerpt = source["body"][:1000]
            summary = {key: value for key, value in source.items() if key not in {"body", "cursor"}}
            return {"type": "source", "id": entity_id, "scope": source["scope"], "state": "source_evidence", "text": excerpt, "source": summary, "evidence": [{"source_id": entity_id, "quote": excerpt}], "truncated": len(excerpt) != len(source["body"])}
        row = conn.execute("SELECT data FROM memory_projections WHERE id=? AND state='active'", (entity_id,)).fetchone()
        if not row:
            return None
        memory = json.loads(row[0])
        if not self._memory_visible(conn, memory, scopes):
            return None
        return memory | {"type": "memory"}

    def hydrate(self, entity_type, entity_id, *, scopes):
        self._scopes(scopes)
        with self.connect() as conn:
            return self._recall_item(conn, entity_type, entity_id, scopes)

    def recall(self, query, scopes, limit=6, budget_bytes=12000, include_sources=True):
        self._scopes(scopes)
        terms = set(sorted(lexical_terms(str(query)[:16384]))[:128])
        count = min(max(int(limit), 1), 30)
        budget = min(max(int(budget_bytes), 512), 65536)
        items, used, truncated = [], 0, False
        with self.connect() as conn:
            if not terms:
                ranked = [("memory", row[0], 0) for row in conn.execute("SELECT id FROM memory_projections WHERE state='active' ORDER BY id")]
            else:
                placeholders = ",".join("?" for _ in terms)
                ranked = [(row["entity_type"], row["entity_id"], row["score"]) for row in conn.execute(f"SELECT entity_type,entity_id,COUNT(*) AS score FROM memory_terms WHERE term IN ({placeholders}) GROUP BY entity_type,entity_id ORDER BY score DESC,entity_type,entity_id LIMIT 2000", tuple(sorted(terms)))]
            for entity_type, entity_id, score in ranked:
                if entity_type == "source" and not include_sources:
                    continue
                item = self._recall_item(conn, entity_type, entity_id, scopes)
                if not item:
                    continue
                item["score"] = score
                size = len(canonical(item))
                if used + size > budget:
                    truncated = True
                    continue
                items.append(item)
                used += size
                if len(items) >= count:
                    truncated = len(ranked) > len(items)
                    break
        return {"items": items, "gaps": ["Only locally captured and imported evidence is covered; candidate memories are not confirmed facts."], "truncated": truncated, "bytes": used, "updated_at": utc_now()}

    def profile(self, scope, *, scopes=None, limit=30):
        if not self._visible(scope, scopes):
            raise LookupError("scope unavailable")
        with self.connect() as conn:
            items = []
            for row in conn.execute("SELECT id FROM memory_projections WHERE scope=? AND state='active' ORDER BY id", (scope,)):
                item = self._recall_item(conn, "memory", row[0], scopes)
                if item:
                    items.append(item)
                if len(items) >= min(max(int(limit), 1), 100):
                    break
            return {"scope": scope, "items": items, "gaps": ["Confirmed local replica only."]}

    def status(self, *, scopes=None):
        self._scopes(scopes)
        with self.connect() as conn:
            return self._status_conn(conn, scopes=scopes)

    def _status_conn(self, conn, *, scopes=None):
        counts = {key: 0 for key in ("sources", "candidate", "active", "disabled", "deleted", "rejected", "conflict", "pending")}
        for row in conn.execute("SELECT id,scope FROM memory_sources"):
            if self._visible(row["scope"], scopes) and (scopes is None or not self._blocked(conn, row["id"])):
                counts["sources"] += 1
        for row in conn.execute("SELECT data FROM memory_projections"):
            memory = json.loads(row[0])
            if self._memory_visible(conn, memory, scopes, management=True):
                counts[memory["state"]] += 1
        data = {"configured": bool(self._meta(conn, "space_id")), "space_id": self._meta(conn, "space_id"), "device_id": self._meta(conn, "device_id"), "schema_version": SCHEMA_VERSION, "counts": counts, "has_join_key": self._has_key(), "writer_available": self.writer_available(), "credentials_missing": bool(self._meta(conn, "space_id")) and not self._has_key(), "processing_allowed": bool(self._meta(conn, "space_id")) and self._has_key() and self.writer_available() and self._load_config()["enabled"], "last_import": self._meta(conn, "last_import"), "last_export": self._meta(conn, "last_export"), "updated_at": utc_now()}
        if scopes is None:
            data["jobs"] = {row[0]: row[1] for row in conn.execute("SELECT status,COUNT(*) FROM memory_jobs GROUP BY status")}
            data["pending_export"] = conn.execute("SELECT COUNT(*) FROM memory_operations WHERE device_id=? AND seq>?", (self._meta(conn, "device_id"), int(self._meta(conn, "export_seq", "0")))).fetchone()[0]
        return data

    def claim_jobs(self, limit=2, *, lease_seconds=120):
        if not self._has_key() or not self.writer_available():
            return []
        limit = min(max(int(limit), 1), 2)
        now = time.time()
        with self.transaction() as conn:
            conn.execute("UPDATE memory_jobs SET status='failed',lease_token=NULL,lease_until=NULL,error='job lease expired after maximum attempts' WHERE status='running' AND lease_until<? AND attempts>=3", (now,))
            rows = conn.execute("SELECT * FROM memory_jobs WHERE attempts<3 AND next_run<=? AND (status='pending' OR (status='running' AND lease_until<?)) ORDER BY source_id LIMIT ?", (now, now, limit)).fetchall()
            result = []
            for row in rows:
                if self._blocked(conn, row["source_id"]):
                    conn.execute("UPDATE memory_jobs SET status='suppressed',lease_token=NULL WHERE source_id=?", (row["source_id"],))
                    continue
                token = secrets.token_urlsafe(24)
                conn.execute("UPDATE memory_jobs SET status='running',attempts=attempts+1,lease_token=?,lease_until=? WHERE source_id=?", (token, now + min(max(lease_seconds, 1), 300), row["source_id"]))
                source = self._get_source(conn, row["source_id"])
                result.append(source | {"lease_token": token, "attempts": row["attempts"] + 1})
            return result

    def _validate_lease(self, conn, source_id, lease_token):
        row = conn.execute("SELECT * FROM memory_jobs WHERE source_id=?", (source_id,)).fetchone()
        if not row or row["status"] != "running" or not lease_token or row["lease_token"] != lease_token or row["lease_until"] < time.time():
            raise MemoryValidationError("stale job lease")
        source = self._get_source(conn, source_id)
        if source["checksum"] != row["checksum"] or self._blocked(conn, source_id):
            raise MemoryValidationError("leased source no longer available")
        return row

    def finish_job(self, source_id, lease_token, error=None, retry=False):
        with self.transaction() as conn:
            row = self._validate_lease(conn, source_id, lease_token)
            state = "pending" if error and retry and row["attempts"] < 3 else "failed" if error else "done"
            conn.execute("UPDATE memory_jobs SET status=?,lease_token=NULL,lease_until=NULL,next_run=?,error=? WHERE source_id=?", (state, time.time() + 30 * 2**row["attempts"] if state == "pending" else 0, str(error)[:256] if error else None, source_id))
            return {"source_id": source_id, "status": state}

    def defer_job(self, source_id, lease_token, *, next_run, error="daily_model_budget_exhausted"):
        if not isinstance(next_run, (int, float)) or next_run <= time.time():
            raise MemoryValidationError("deferred job requires a future retry time")
        with self.transaction() as conn:
            self._validate_lease(conn, source_id, lease_token)
            conn.execute("UPDATE memory_jobs SET status='pending',attempts=max(attempts-1,0),lease_token=NULL,lease_until=NULL,next_run=?,error=? WHERE source_id=?", (float(next_run), str(error)[:256], source_id))
        return {"source_id": source_id, "status": "pending", "next_run": next_run}

    def retry_failed_jobs(self, limit=100):
        """Explicit owner recovery of transient failures; budgets are untouched."""
        self.shared_key()  # writer binding and private space credential checks
        with self.transaction() as conn:
            rows = conn.execute("""SELECT j.source_id FROM memory_jobs j
                JOIN memory_sources s ON s.id=j.source_id WHERE j.status='failed'
                AND s.local=1
                AND NOT EXISTS(SELECT 1 FROM memory_source_blocks b WHERE b.source_id=s.id)
                AND NOT EXISTS(SELECT 1 FROM memory_message_blocks b WHERE b.message_key=s.message_key)
                ORDER BY j.source_id LIMIT ?""", (min(max(int(limit), 1), 100),)).fetchall()
            for row in rows:
                conn.execute("UPDATE memory_jobs SET status='pending',attempts=0,error=NULL,next_run=0,lease_token=NULL,lease_until=NULL WHERE source_id=?", (row[0],))
            remaining = conn.execute("SELECT COUNT(*) FROM memory_jobs WHERE status='failed'").fetchone()[0]
            return {"retried": len(rows), "remaining": remaining}

    def reserve_model_call(self, kind, model, daily_limit):
        self._require_writer()
        if kind not in {"llm", "source_embedding", "query_embedding", "embedding", "extraction"}:
            raise MemoryValidationError("unknown model request kind")
        if type(daily_limit) is not int or daily_limit < 0:
            raise MemoryValidationError("invalid request limit")
        limit = min(daily_limit, self._load_config()["model_daily_call_limit"])
        day = utc_now()[:10]
        with self.transaction() as conn:
            device_id = self._meta(conn, "device_id")
            count = conn.execute("SELECT COUNT(*) FROM memory_model_calls WHERE device_id=? AND day=?", (device_id, day)).fetchone()[0]
            if count >= limit:
                raise BudgetExceededError("daily memory model request limit reached (per device, UTC day)")
            call_id = str(uuid.uuid4())
            conn.execute("INSERT INTO memory_model_calls(id,device_id,day,kind,model,started_at) VALUES(?,?,?,?,?,?)", (call_id, device_id, day, kind, str(model)[:512], utc_now()))
            return call_id

    def finish_model_call(self, call_id, input_tokens=None, output_tokens=None, error=None):
        for value in (input_tokens, output_tokens):
            if value is not None and (type(value) is not int or value < 0):
                raise MemoryValidationError("invalid token usage")
        with self.transaction() as conn:
            row = conn.execute("SELECT finished_at FROM memory_model_calls WHERE id=?", (call_id,)).fetchone()
            if not row:
                raise LookupError("request unavailable")
            if row[0]:
                raise MemoryValidationError("request usage already recorded")
            conn.execute("UPDATE memory_model_calls SET finished_at=?,input_tokens=?,output_tokens=?,error=? WHERE id=?", (utc_now(), input_tokens, output_tokens, str(error)[:256] if error else None, call_id))

    def record_model_call(self, kind="llm", model="", daily_limit=50, input_tokens=None, output_tokens=None, error=None):
        call_id = self.reserve_model_call(kind, model, daily_limit)
        self.finish_model_call(call_id, input_tokens, output_tokens, error)
        return call_id

    def model_usage(self):
        with self.connect() as conn:
            rows = [dict(row) for row in conn.execute("SELECT day,kind,COUNT(*) AS calls,SUM(COALESCE(input_tokens,0)) AS input_tokens,SUM(COALESCE(output_tokens,0)) AS output_tokens,SUM(input_tokens IS NULL OR output_tokens IS NULL) AS unknown_usage,SUM(error IS NOT NULL) AS failed_calls FROM memory_model_calls WHERE device_id=? GROUP BY day,kind ORDER BY day DESC,kind LIMIT 100", (self._meta(conn, "device_id"),))]
            return {"items": rows, "daily_limit": self._load_config()["model_daily_call_limit"], "budget_scope": "per-device UTC day; every external attempt counts"}

    def export_events(self, *, after=None, limit=100):
        with self.connect() as conn:
            cursor = int(self._meta(conn, "export_seq", "0")) if after is None else int(after)
            device = self._meta(conn, "device_id")
            operations = [json.loads(row[0]) for row in conn.execute("SELECT data FROM memory_operations WHERE device_id=? AND seq>? ORDER BY seq LIMIT ?", (device, cursor, min(max(limit, 1), 1000)))]
            source_cursor = int(self._meta(conn, "export_source_cursor", "0"))
            source_rows = conn.execute("SELECT * FROM memory_sources WHERE local=1 AND source_order>? ORDER BY source_order LIMIT ?", (source_cursor, min(max(limit, 1), 100))).fetchall()
            sources = {row["id"]: json.loads(row["data"]) for row in source_rows}
            for operation in operations:
                for item in operation["payload"].get("evidence", []):
                    source = self._get_source(conn, item["source_id"])
                    sources[source["id"]] = {key: value for key, value in source.items() if key not in {"local", "cursor"}}
            return {"operations": operations, "sources": list(sources.values()), "after": cursor, "source_through": max((row["source_order"] for row in source_rows), default=source_cursor), "packet_seq": int(self._meta(conn, "packet_seq", "0")) + 1}

    def mark_exported(self, through, packet_seq, source_through=None):
        with self.transaction() as conn:
            self._set_meta(conn, "export_seq", max(int(through), int(self._meta(conn, "export_seq", "0"))))
            self._set_meta(conn, "packet_seq", max(int(packet_seq), int(self._meta(conn, "packet_seq", "0"))))
            self._set_meta(conn, "last_export", utc_now())
            if source_through is not None:
                self._set_meta(conn, "export_source_cursor", max(int(source_through), int(self._meta(conn, "export_source_cursor", "0"))))

    def claim_sync(self, lease_seconds=60):
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM memory_sync_lease WHERE id=1").fetchone()
            if row and row["until"] > time.time():
                return None
            token = secrets.token_urlsafe(24)
            conn.execute("INSERT INTO memory_sync_lease VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET token=excluded.token,until=excluded.until", (token, time.time() + lease_seconds))
            return token

    def release_sync(self, token):
        with self.transaction() as conn:
            conn.execute("DELETE FROM memory_sync_lease WHERE id=1 AND token=?", (token,))

    def packet_known(self, sha):
        with self.connect() as conn:
            return bool(conn.execute("SELECT 1 FROM memory_packets WHERE digest=?", (sha,)).fetchone())

    def scan_cursor(self, *, advance=None):
        if advance is None:
            with self.connect() as conn:
                return int(self._meta(conn, "sync_scan_cursor", "0"))
        with self.transaction() as conn:
            self._set_meta(conn, "sync_scan_cursor", advance)

    def import_events(self, operations, sources, *, packet_digest=None, device_id=None, packet_seq=None):
        self._require_writer()
        if len(operations) > 1000 or len(sources) > 1000:
            raise MemoryValidationError("packet record limit exceeded")
        normalized = [normalize_operation(op) for op in operations]
        with self.transaction() as conn:
            if packet_digest:
                existing = conn.execute("SELECT digest FROM memory_packets WHERE digest=? OR (device_id=? AND packet_seq=?)", (packet_digest, device_id, packet_seq)).fetchall()
                if existing:
                    if any(row[0] != packet_digest for row in existing):
                        raise MemoryValidationError("packet writer sequence was rewritten")
                    return {"operations": 0, "sources": 0, "replayed": True}
            source_count = 0
            for source in sources:
                _, added = self._ingest_source(conn, source, False)
                source_count += added
            changed, affected = 0, set()
            for op in normalized:
                if device_id and op["device_id"] != device_id:
                    raise MemoryValidationError("packet contains a different writer")
                if op["device_id"] == self._meta(conn, "device_id"):
                    old = conn.execute("SELECT data FROM memory_operations WHERE id=?", (op["id"],)).fetchone()
                    if not old or old[0] != canonical(op).decode():
                        raise MemoryValidationError("another copy is writing this device stream; assign a new device ID")
                if op["action"] in {"propose", "edit", "resolve", "delete", "disable"}:
                    self._validate_evidence(conn, op["payload"], allow_blocked=True, allow_missing=True)
                changed += self._insert_operation(conn, op)
                affected.add(op["memory_id"])
            affected.update(row[0] for row in conn.execute("SELECT DISTINCT memory_id FROM memory_operations WHERE validity='pending'"))
            for memory_id in sorted(affected):
                self._rebuild_lineage(conn, memory_id)
            if packet_digest:
                conn.execute("INSERT INTO memory_packets VALUES(?,?,?,?)", (packet_digest, device_id, packet_seq, utc_now()))
            self._set_meta(conn, "last_import", utc_now())
            return {"operations": changed, "sources": source_count, "replayed": False}
