"""Consistent SQLite backups and verified restore into a new directory."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .db import Database


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def create_backup(db_path: Path, output: Path) -> dict:
    if not db_path.is_file():
        raise FileNotFoundError("Server database not found")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        copy = Path(temp) / "agent-workbench.db"
        with closing(sqlite3.connect(db_path)) as source, closing(sqlite3.connect(copy)) as target:
            source.backup(target)
        with closing(sqlite3.connect(copy)) as check:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup integrity check failed")
            version = check.execute("SELECT version FROM schema_version").fetchone()[0]
        manifest = {"format": 1, "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "schema_version": version, "files": {"agent-workbench.db": {
                        "size": copy.stat().st_size, "sha256": _file_hash(copy)}}}
        temporary_output = output.with_suffix(output.suffix + ".tmp")
        with zipfile.ZipFile(temporary_output, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6) as archive:
            archive.write(copy, "agent-workbench.db")
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        temporary_output.replace(output)
    return {"path": str(output), "sha256": _file_hash(output), "schema_version": version}


def restore_backup(archive_path: Path, target_dir: Path) -> Path:
    if target_dir.exists() and any(target_dir.iterdir()):
        raise FileExistsError("Restore target directory must be empty")
    with zipfile.ZipFile(archive_path) as archive:
        if set(archive.namelist()) != {"manifest.json", "agent-workbench.db"}:
            raise ValueError("Unexpected backup entries")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("format") != 1 or manifest.get("schema_version") != 1:
            raise ValueError("Unsupported backup format or schema")
        spec = manifest["files"]["agent-workbench.db"]
        target_dir.mkdir(parents=True, exist_ok=True)
        restored = target_dir / "agent-workbench.db"
        with archive.open("agent-workbench.db") as source, restored.open("wb") as target:
            h = hashlib.sha256()
            length = 0
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                target.write(chunk)
                h.update(chunk)
                length += len(chunk)
        if length != spec["size"] or h.hexdigest() != spec["sha256"]:
            restored.unlink(missing_ok=True)
            raise ValueError("Backup checksum mismatch")
    with closing(sqlite3.connect(restored)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Restored database integrity check failed")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Restored database foreign key check failed")
        db.execute("INSERT OR REPLACE INTO app_meta(key,value) VALUES('server_epoch',?)", (str(uuid4()),))
        db.commit()
    Database(restored).initialize()
    return restored
