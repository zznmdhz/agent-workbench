"""Bounded, canonical records for the private shared-memory event stream.

These are data validators, never executors. Hashes are over UTF-8 canonical JSON;
source identity also includes the logical scope, independent of host filesystem paths.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1
MAX_BODY_BYTES = 128 * 1024
MAX_TEXT_BYTES = 16 * 1024
MAX_QUOTE_BYTES = 16 * 1024
MAX_EVIDENCE = 8
MAX_PARENTS = 64
KINDS = frozenset({"fact", "preference", "constraint", "decision", "task"})
ACTIONS = frozenset({"propose", "confirm", "edit", "disable", "enable", "reject", "resolve", "delete"})
ACTORS = frozenset({"owner", "processor", "assistant_proposal"})
_CREDENTIAL = re.compile(
    r"\bsk-[a-zA-Z0-9_-]{16,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\b(?:api[_ -]?key|access[_ -]?token|password|secret)\s*[:=]\s*['\"]?[A-Za-z0-9_/+.-]{16,}",
    re.I,
)


class MemoryValidationError(ValueError):
    pass


class StaleRevisionError(MemoryValidationError):
    """The owner's submitted heads no longer equal the current heads."""


class BudgetExceededError(MemoryValidationError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def bounded_text(value: Any, name: str, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise MemoryValidationError(f"invalid {name}")
    if len(value.encode("utf-8")) > maximum or "\x00" in value:
        raise MemoryValidationError(f"{name} exceeds limit")
    return value


def identifier(value: Any, name: str = "id") -> str:
    value = bounded_text(value, name, 256)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", value) or ".." in value:
        raise MemoryValidationError(f"invalid {name}")
    return value


def scope_value(value: Any) -> str:
    return bounded_text(value, "scope", 256)


def source_id(source: dict) -> str:
    checksum = hashlib.sha256(str(source.get("body", "")).encode("utf-8")).hexdigest()
    return digest({key: str(source.get(key, "")) for key in ("agent", "native_id", "message_id", "scope")} | {"checksum": checksum})


def message_key(source: dict) -> str:
    return digest({key: source[key] for key in ("agent", "native_id", "message_id", "scope")})


def normalize_source(source: dict, *, device_id: str) -> dict:
    if not isinstance(source, dict):
        raise MemoryValidationError("source must be an object")
    result = {
        "agent": bounded_text(source.get("agent"), "agent", 128),
        "native_id": bounded_text(source.get("native_id"), "native_id", 1024),
        "message_id": bounded_text(source.get("message_id"), "message_id", 1024),
        "device_id": identifier(source.get("device_id") or device_id, "device_id"),
        "role": bounded_text(source.get("role"), "role", 32),
        "body": bounded_text(source.get("body"), "body", MAX_BODY_BYTES),
        "occurred_at": bounded_text(source.get("occurred_at") or utc_now(), "occurred_at", 128),
        "scope": scope_value(source.get("scope", "personal")),
        "complete": source.get("complete", True),
    }
    if type(result["complete"]) is not bool:
        raise MemoryValidationError("complete must be boolean")
    if _CREDENTIAL.search(result["body"]):
        raise MemoryValidationError("source contains a probable credential")
    checksum = hashlib.sha256(result["body"].encode("utf-8")).hexdigest()
    if source.get("checksum", checksum) != checksum:
        raise MemoryValidationError("source checksum mismatch")
    result["checksum"] = checksum
    result["id"] = source_id(result)
    if source.get("id", result["id"]) != result["id"]:
        raise MemoryValidationError("source identity mismatch")
    result["capture_state"] = bounded_text(source.get("capture_state", "complete" if result["complete"] else "incomplete"), "capture_state", 128)
    return result


def source_authority(source: dict) -> dict:
    return {key: source[key] for key in ("id", "agent", "native_id", "message_id", "role", "body", "scope", "complete", "checksum")}


def normalize_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise MemoryValidationError("payload must be an object")
    text = bounded_text(payload.get("text"), "text", MAX_TEXT_BYTES)
    if _CREDENTIAL.search(text):
        raise MemoryValidationError("memory contains a probable credential")
    kind = payload.get("kind")
    if kind not in KINDS:
        raise MemoryValidationError("unknown memory kind")
    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= MAX_EVIDENCE:
        raise MemoryValidationError("memory requires bounded evidence")
    normalized = []
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"source_id", "quote"}:
            raise MemoryValidationError("invalid evidence")
        quote = bounded_text(item["quote"], "quote", MAX_QUOTE_BYTES)
        normalized.append({"source_id": identifier(item["source_id"], "source_id"), "quote": quote})
    return {"text": text, "kind": kind, "scope": scope_value(payload.get("scope")), "evidence": normalized}


def normalize_operation(operation: dict) -> dict:
    if not isinstance(operation, dict):
        raise MemoryValidationError("operation must be an object")
    keys = {"id", "device_id", "seq", "memory_id", "parents", "action", "actor", "occurred_at", "payload"}
    if set(operation) != keys:
        raise MemoryValidationError("invalid operation fields")
    op = {key: operation[key] for key in keys}
    for key in ("id", "device_id", "memory_id"):
        op[key] = identifier(op[key], key)
    if type(op["seq"]) is not int or not 1 <= op["seq"] <= 2**63 - 1:
        raise MemoryValidationError("invalid writer sequence")
    if not isinstance(op["parents"], list) or len(op["parents"]) > MAX_PARENTS:
        raise MemoryValidationError("invalid parents")
    op["parents"] = sorted(identifier(parent, "parent") for parent in op["parents"])
    if len(set(op["parents"])) != len(op["parents"]) or op["id"] in op["parents"]:
        raise MemoryValidationError("duplicate or self parent")
    if op["action"] not in ACTIONS or op["actor"] not in ACTORS:
        raise MemoryValidationError("unknown action or actor")
    if op["actor"] != "owner" and op["action"] != "propose":
        raise MemoryValidationError("only owner can change memory")
    op["occurred_at"] = bounded_text(op["occurred_at"], "occurred_at", 128)
    if op["action"] in {"propose", "edit", "resolve", "delete", "disable"}:
        original = op["payload"]
        op["payload"] = normalize_payload(original)
        if op["action"] == "resolve":
            state = original.get("state")
            if state not in {"candidate", "active"} or len(op["parents"]) < 2:
                raise MemoryValidationError("resolve requires all heads and explicit state")
            op["payload"]["state"] = state
        if op["action"] in {"delete", "disable"}:
            blocked = original.get("blocked_sources", [item["source_id"] for item in op["payload"]["evidence"]])
            if not isinstance(blocked, list) or len(blocked) > 4096:
                raise MemoryValidationError("suppression source list exceeds limit")
            op["payload"]["blocked_sources"] = sorted({identifier(item, "blocked_source") for item in blocked})
    elif op["payload"] != {}:
        raise MemoryValidationError("action payload must be empty")
    if op["action"] == "propose" and op["parents"]:
        raise MemoryValidationError("proposal cannot have parents")
    if op["action"] != "propose" and not op["parents"]:
        raise MemoryValidationError("change requires parents")
    return op


def lexical_terms(text: str) -> set[str]:
    """Keep paths and identifiers, and use Chinese bigrams without dependencies."""
    result = set(re.findall(r"[a-z0-9_./:@+\-]+", text.lower()))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        if len(run) == 1:
            result.add(run)
        else:
            result.update(run[index:index + 2] for index in range(len(run) - 1))
    return result
