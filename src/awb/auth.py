"""Single-owner browser login and scoped collector credentials."""

from __future__ import annotations

import hashlib
import secrets
import time
from datetime import datetime, timezone
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import HTTPException, Request, Response

from .db import Database

passwords = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def check_rate(db: Database, scope: str, subject: str, limit: int = 5, window: int = 600) -> None:
    key = token_hash(subject)
    with db.tx() as conn:
        row = conn.execute("SELECT attempts,expires_at FROM auth_attempts WHERE scope=? AND subject_hash=?", (scope, key)).fetchone()
        if row and row["expires_at"] > int(time.time()) and row["attempts"] >= limit:
            raise HTTPException(429, "Too many attempts", headers={"Retry-After": str(row["expires_at"] - int(time.time()))})


def record_failure(db: Database, scope: str, subject: str, window: int = 600) -> None:
    key = token_hash(subject)
    now = int(time.time())
    with db.tx() as conn:
        row = conn.execute("SELECT attempts,expires_at FROM auth_attempts WHERE scope=? AND subject_hash=?", (scope, key)).fetchone()
        attempts = row["attempts"] + 1 if row and row["expires_at"] > now else 1
        conn.execute("INSERT OR REPLACE INTO auth_attempts VALUES(?,?,?,?)", (scope, key, attempts, now+window))
        if scope == "pair":
            conn.execute("UPDATE pairing_codes SET attempts=attempts+1 WHERE consumed_at IS NULL")
            conn.execute("DELETE FROM pairing_codes WHERE attempts>=5 AND consumed_at IS NULL")


def clear_failures(db: Database, scope: str, subject: str) -> None:
    with db.tx() as conn:
        conn.execute("DELETE FROM auth_attempts WHERE scope=? AND subject_hash=?", (scope, token_hash(subject)))


def initialize_owner(db: Database, password: str) -> None:
    if len(password) < 12:
        raise ValueError("Owner password must have at least 12 characters")
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM owner WHERE id=1").fetchone():
            raise ValueError("Owner already exists")
        conn.execute("INSERT INTO owner(id,password_hash,created_at) VALUES(1,?,?)", (passwords.hash(password), now_iso()))


def login(db: Database, password: str) -> tuple[str, str]:
    with db.tx() as conn:
        row = conn.execute("SELECT password_hash FROM owner WHERE id=1").fetchone()
        if row is None:
            raise HTTPException(503, "Owner account has not been initialized")
        try:
            valid = passwords.verify(row[0], password)
        except VerifyMismatchError:
            valid = False
        if not valid:
            raise HTTPException(401, "Invalid credentials")
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        conn.execute("INSERT INTO web_sessions(token_hash,csrf,expires_at) VALUES(?,?,?)", (token_hash(token), csrf, int(time.time()) + 86400))
        return token, csrf


def require_owner(request: Request) -> str:
    value = request.cookies.get("awb_session")
    if not value:
        raise HTTPException(401, "Login required")
    db: Database = request.app.state.db
    with db.read() as conn:
        row = conn.execute("SELECT csrf,expires_at FROM web_sessions WHERE token_hash=?", (token_hash(value),)).fetchone()
    if row is None or row["expires_at"] <= int(time.time()):
        raise HTTPException(401, "Session expired")
    return row["csrf"]


def require_owner_write(request: Request) -> None:
    csrf = require_owner(request)
    if request.headers.get("x-awb-csrf") != csrf:
        raise HTTPException(403, "CSRF token required")
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "Origin mismatch")


def set_owner_cookie(response: Response, token: str, request: Request) -> None:
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie("awb_session", token, httponly=True, secure=secure, samesite="strict", max_age=86400, path="/")


def issue_pair_code(db: Database) -> str:
    code = f"{secrets.randbelow(10**9):09d}"
    with db.tx() as conn:
        conn.execute("DELETE FROM pairing_codes WHERE expires_at<? OR consumed_at IS NOT NULL", (int(time.time()),))
        conn.execute("INSERT INTO pairing_codes(code_hash,expires_at) VALUES(?,?)", (token_hash(code), int(time.time()) + 600))
    return code


def pair_device(db: Database, code: str, name: str, os_name: str, environment: str) -> tuple[str, str]:
    if not name.strip() or len(name) > 100:
        raise HTTPException(422, "Device name is required")
    with db.tx() as conn:
        row = conn.execute("SELECT expires_at,attempts,consumed_at FROM pairing_codes WHERE code_hash=?", (token_hash(code),)).fetchone()
        if row is None or row["expires_at"] < int(time.time()) or row["consumed_at"] is not None or row["attempts"] >= 5:
            raise HTTPException(401, "Invalid pairing code")
        conn.execute("UPDATE pairing_codes SET consumed_at=? WHERE code_hash=?", (int(time.time()), token_hash(code)))
        device_id = str(uuid4())
        token = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO devices(id,name,os,environment,token_hash,registered_at) VALUES(?,?,?,?,?,?)", (device_id, name.strip(), os_name, environment, token_hash(token), now_iso()))
        return device_id, token


def require_collector(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(401, "Collector token required")
    value = header[7:]
    db: Database = request.app.state.db
    with db.read() as conn:
        row = conn.execute("SELECT id FROM devices WHERE token_hash=? AND revoked_at IS NULL", (token_hash(value),)).fetchone()
    if row is None:
        raise HTTPException(401, "Collector token invalid")
    return row[0]
