"""Bounded, mapped-root file checks on the target collector."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4

from fastapi import HTTPException

from .auth import now_iso
from .db import Database


def safe_relative(value: str) -> str:
    if not value or "\x00" in value or "\\" in value:
        raise ValueError("Invalid relative path")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute() or windows.drive or any(x in {"..", "."} for x in posix.parts):
        raise ValueError("Invalid relative path")
    return str(posix)


def check_local(root: Path, relative: str, operation: str, byte_budget: int = 20 * 1024 * 1024) -> dict:
    try:
        relative = safe_relative(relative)
        root = root.resolve(strict=True)
        target = (root / relative).resolve(strict=True)
        if not target.is_relative_to(root):
            return {"status": "inaccessible", "reason": "outside_registered_root"}
        if not target.is_file():
            return {"status": "inaccessible", "reason": "not_regular_file"}
        st = target.stat()
        result = {"status": "exists", "size_bytes": st.st_size,
                  "mtime_ns": st.st_mtime_ns, "checked_at": now_iso(), "version_status": "unverified"}
        if operation == "sha256":
            if st.st_size > byte_budget:
                result.update({"status": "inaccessible", "reason": "byte_budget_exceeded"})
                return result
            h = hashlib.sha256()
            with target.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    h.update(chunk)
            result["sha256"] = h.hexdigest()
            result["version_status"] = "hash_observed"
        return result
    except FileNotFoundError:
        return {"status": "missing", "checked_at": now_iso()}
    except (OSError, ValueError):
        return {"status": "inaccessible", "checked_at": now_iso()}


def create_job(db: Database, device_id: str, root_id: str, relative_path: str,
               operation: str) -> dict:
    if operation not in {"stat", "sha256"}:
        raise HTTPException(422, "Unsupported file operation")
    try:
        relative_path = safe_relative(relative_path)
    except ValueError as exc:
        raise HTTPException(422, "Invalid relative path") from exc
    with db.tx() as conn:
        device = conn.execute("SELECT environment FROM devices WHERE id=? AND revoked_at IS NULL", (device_id,)).fetchone()
        if not device:
            raise HTTPException(404, "Target device not found")
        mapping = conn.execute("SELECT map_version FROM path_maps WHERE root_id=? AND environment_id=?", (root_id, device_id)).fetchone()
        if not mapping:
            raise HTTPException(422, "Target root not mapped")
        from datetime import datetime, timedelta, timezone

        expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        ident = str(uuid4())
        conn.execute("""INSERT INTO file_check_jobs(id,target_device_id,target_environment_id,root_id,
            relative_path,operation,map_version,status,created_at,expires_at)
            VALUES(?,?,?,?,?,?,?,'pending',?,?)""",
            (ident, device_id, device["environment"], root_id, relative_path, operation,
             mapping["map_version"], now_iso(), expires))
    return {"id": ident, "status": "pending", "expires_at": expires}


def jobs_for_collector(db: Database, device_id: str) -> list[dict]:
    with db.read() as conn:
        rows = conn.execute("""SELECT id,root_id,relative_path,operation,map_version,expires_at
            FROM file_check_jobs WHERE target_device_id=? AND status='pending'
            AND expires_at>? ORDER BY created_at LIMIT 50""", (device_id, now_iso())).fetchall()
    return [dict(r) for r in rows]


def record_result(db: Database, device_id: str, job_id: str, result: dict) -> dict:
    allowed = {"status", "reason", "size_bytes", "mtime_ns", "checked_at", "version_status", "sha256"}
    if set(result) - allowed or result.get("status") not in {"exists", "missing", "inaccessible", "unmapped"}:
        raise HTTPException(422, "Invalid file check result")
    import json

    with db.tx() as conn:
        job = conn.execute("SELECT target_device_id,status,expires_at FROM file_check_jobs WHERE id=?", (job_id,)).fetchone()
        if not job or job["target_device_id"] != device_id:
            raise HTTPException(404, "Job not found")
        if job["status"] == "complete":
            return {"id": job_id, "status": "complete"}
        if job["expires_at"] <= now_iso():
            raise HTTPException(409, "Job expired")
        conn.execute("UPDATE file_check_jobs SET status='complete',result_json=? WHERE id=?",
                     (json.dumps(result), job_id))
    return {"id": job_id, "status": "complete"}
