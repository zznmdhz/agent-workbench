"""Authenticated, bounded immutable event packets on a NAS file exchange root.

The root is always explicit. It carries JSON records, never SQLite/WAL files or keys.
Packets are durable before export progress advances; replay is an idempotent no-op.
"""
from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import os
import re
import tempfile
import zlib
from pathlib import Path

from .events import MemoryValidationError, canonical, identifier, utc_now

MAX_COMPRESSED = 2 * 1024 * 1024
MAX_DECOMPRESSED = 8 * 1024 * 1024
MAX_RECORDS = 1000
_FILENAME = re.compile(r"^(\d{12})-([0-9a-f]{64})\.json\.gz$")


def packet_bytes(payload, key):
    raw = canonical(payload)
    if len(raw) > MAX_DECOMPRESSED - 1024:
        raise MemoryValidationError("packet decompressed size exceeds limit")
    sha = hashlib.sha256(raw).hexdigest()
    envelope = {"payload": payload, "digest": sha, "hmac": hmac.new(key, raw, hashlib.sha256).hexdigest()}
    compressed = gzip.compress(canonical(envelope), mtime=0)
    if len(compressed) > MAX_COMPRESSED:
        raise MemoryValidationError("packet compressed size exceeds limit")
    return sha, compressed


def decode_packet(path, key, *, space_id):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > MAX_COMPRESSED:
        raise MemoryValidationError("packet file type or size rejected")
    match = _FILENAME.fullmatch(path.name)
    if not match:
        raise MemoryValidationError("invalid packet filename")
    with gzip.open(path, "rb") as stream:
        data = stream.read(MAX_DECOMPRESSED + 1)
    if len(data) > MAX_DECOMPRESSED:
        raise MemoryValidationError("packet decompressed size exceeds limit")
    try:
        envelope = json.loads(data)
        if not isinstance(envelope, dict) or set(envelope) != {"payload", "digest", "hmac"}:
            raise ValueError("envelope")
        payload = envelope["payload"]
        required = {"version", "space_id", "device_id", "packet_seq", "operations", "sources"}
        if not isinstance(payload, dict) or not required <= set(payload) or set(payload) - required - {"source_through"}:
            raise ValueError("payload")
        raw = canonical(payload)
        sha = hashlib.sha256(raw).hexdigest()
        if envelope["digest"] != sha or match[2] != sha:
            raise ValueError("digest")
        if not isinstance(envelope["hmac"], str) or not hmac.compare_digest(envelope["hmac"], hmac.new(key, raw, hashlib.sha256).hexdigest()):
            raise ValueError("authentication")
        if payload["version"] != 1 or payload["space_id"] != space_id:
            raise ValueError("space/version")
        identifier(payload["device_id"], "device_id")
        if type(payload["packet_seq"]) is not int or payload["packet_seq"] != int(match[1]):
            raise ValueError("sequence")
        if "source_through" in payload and (type(payload["source_through"]) is not int or not 0 <= payload["source_through"] <= 2**63 - 1):
            raise ValueError("source cursor")
        if not isinstance(payload["operations"], list) or not isinstance(payload["sources"], list) or len(payload["operations"]) > MAX_RECORDS or len(payload["sources"]) > MAX_RECORDS:
            raise ValueError("record count")
        if path.parent.name != "packets" or path.parent.parent.name != payload["device_id"]:
            raise ValueError("writer directory")
    except (ValueError, TypeError, KeyError) as error:
        # Stable categories without recording message text or credentials.
        raise MemoryValidationError(f"packet validation failed: {error}") from error
    return envelope


class MemorySync:
    def __init__(self, store, sync_root):
        self.store = store
        self.sync_root = Path(sync_root).expanduser().absolute() if sync_root else None

    def _space_root(self):
        if self.sync_root is None:
            raise MemoryValidationError("memory sync directory is not configured")
        # Reject a database placed in the proposed file exchange tree.
        try:
            self.store.path.resolve().relative_to(self.sync_root.resolve())
        except ValueError:
            pass
        else:
            raise MemoryValidationError("memory database must remain outside the NAS exchange root")
        if not self.store.space_id:
            raise MemoryValidationError("memory space is not configured")
        return self.sync_root / "AgentWorkbench-memory-exchange" / self.store.space_id

    def _export_one(self, root, key):
        # Recover the rename-before-progress crash window from the immutable file.
        # Re-generating that sequence from today's larger queue would change its
        # digest and permanently collide with the already durable packet.
        initial = self.store.export_events(limit=1)
        destination = root / "devices" / self.store.device_id / "packets"
        if any(part.is_symlink() for part in (root, root / "devices", destination.parent, destination)):
            raise MemoryValidationError("symlink exchange path rejected")
        prior = sorted(destination.glob(f"{initial['packet_seq']:012d}-*.json.gz")) if destination.exists() else []
        if prior:
            if len(prior) != 1:
                raise MemoryValidationError("writer packet sequence collision")
            recovered = decode_packet(prior[0], key, space_id=self.store.space_id)["payload"]
            if recovered["device_id"] != self.store.device_id or "source_through" not in recovered:
                raise MemoryValidationError("incomplete export checkpoint requires recovery")
            self.store.import_events(recovered["operations"], recovered["sources"])
            through = max((op["seq"] for op in recovered["operations"]), default=initial["after"])
            self.store.mark_exported(through, recovered["packet_seq"], recovered["source_through"])
            return 1
        # Adapt batch size to the byte caps while ensuring an item is never lost.
        for limit in (50, 10, 1):
            records = self.store.export_events(limit=limit)
            if not records["operations"] and not records["sources"]:
                return 0
            payload = {"version": 1, "space_id": self.store.space_id, "device_id": self.store.device_id, "packet_seq": records["packet_seq"], "operations": records["operations"], "sources": records["sources"], "source_through": records["source_through"]}
            try:
                sha, compressed = packet_bytes(payload, key)
                break
            except MemoryValidationError:
                if limit == 1:
                    raise
        destination.mkdir(parents=True, exist_ok=True)
        if any(part.is_symlink() for part in (root, root / "devices", destination.parent, destination)):
            raise MemoryValidationError("symlink exchange path rejected")
        final = destination / f"{records['packet_seq']:012d}-{sha}.json.gz"
        clashes = list(destination.glob(f"{records['packet_seq']:012d}-*.json.gz"))
        if any(path.name != final.name for path in clashes):
            raise MemoryValidationError("writer packet sequence collision")
        if final.exists():
            if final.read_bytes() != compressed:
                raise MemoryValidationError("immutable packet content changed")
        else:
            fd, temporary = tempfile.mkstemp(prefix=".memory-packet-", dir=destination)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(compressed)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, final)
                if os.name != "nt":
                    directory = os.open(destination, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        through = max((op["seq"] for op in records["operations"]), default=records["after"])
        self.store.mark_exported(through, records["packet_seq"], records["source_through"])
        return 1

    def run_once(self):
        result = {"exported": 0, "imported": 0, "operations": 0, "sources": 0, "errors": [], "last_sync": utc_now()}
        token = None
        try:
            root = self._space_root()
            key = self.store.shared_key()
            token = self.store.claim_sync()
            if not token:
                result["busy"] = True
                return result
            # Bound work per tick; remaining records are picked up by the next tick.
            for _ in range(4):
                count = self._export_one(root, key)
                result["exported"] += count
                if not count:
                    break
            files = sorted(root.glob("devices/*/packets/*.json.gz"))
            offset = self.store.scan_cursor() % max(len(files), 1)
            scan = (files[offset:] + files[:offset])[:500]
            self.store.scan_cursor(advance=offset + len(scan))
            for path in scan:
                try:
                    if path.parent.parent.name == self.store.device_id:
                        continue
                    match = _FILENAME.fullmatch(path.name)
                    if match and self.store.packet_known(match[2]):
                        continue
                    if any(parent.is_symlink() for parent in (path.parent, path.parent.parent, path.parent.parent.parent)):
                        raise MemoryValidationError("symlink packet path rejected")
                    packet = decode_packet(path, key, space_id=self.store.space_id)
                    payload = packet["payload"]
                    changes = self.store.import_events(payload["operations"], payload["sources"], packet_digest=packet["digest"], device_id=payload["device_id"], packet_seq=payload["packet_seq"])
                    if not changes["replayed"]:
                        result["imported"] += 1
                        result["operations"] += changes["operations"]
                        result["sources"] += changes["sources"]
                except (ValueError, LookupError, OSError, EOFError, RecursionError, zlib.error) as error:
                    result["errors"].append({"packet": path.name, "error": type(error).__name__, "reason": str(error)[:160]})
        except (ValueError, LookupError, OSError) as error:
            result["errors"].append({"error": type(error).__name__, "reason": str(error)[:160]})
        finally:
            if token:
                self.store.release_sync(token)
        return result
