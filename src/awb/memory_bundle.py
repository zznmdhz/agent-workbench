"""Versioned, credential-free memory snapshots, verified before atomic restoration."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path
from uuid import uuid4


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def backup_memory(database: Path, output: Path) -> dict:
    from .memory.store import MemoryStore

    if not database.is_file():
        raise FileNotFoundError('Memory database not found')
    store = MemoryStore(database)
    store.initialize()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='awb-memory-backup-') as temporary:
        copy = Path(temporary) / 'memory.db'
        with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(copy)) as target:
                source.backup(target)
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('Memory integrity check failed')
        status = store.status()
        manifest = {'format': 'awb-memory-bundle', 'version': 1,
                    'schema_version': 1, 'space_id': status.get('space_id'),
                    'files': {'memory.db': {'size': copy.stat().st_size, 'sha256': digest(copy)}},
                    'credentials_included': False}
        pending = output.with_suffix(output.suffix + '.tmp')
        try:
            with zipfile.ZipFile(pending, 'w', compression=zipfile.ZIP_DEFLATED) as package:
                package.write(copy, 'memory.db')
                package.writestr('manifest.json', json.dumps(manifest, indent=2))
            pending.replace(output)
        finally:
            pending.unlink(missing_ok=True)
    return {'path': str(output), 'sha256': digest(output), 'space_id': manifest['space_id']}


def restore_memory(package: Path, target: Path) -> Path:
    """Restore into a new local replica; keys are rejoined separately, never copied by default."""
    from .memory.store import MemoryStore

    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise FileExistsError('Restore target must be empty')
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.awb-memory-restore-', dir=target.parent))
    try:
        with zipfile.ZipFile(package) as archive:
            if set(archive.namelist()) != {'manifest.json', 'memory.db'}:
                raise ValueError('Unexpected memory bundle files')
            if archive.getinfo('manifest.json').file_size > 64000:
                raise ValueError('Manifest too large')
            manifest = json.loads(archive.read('manifest.json'))
            if (manifest.get('format') != 'awb-memory-bundle' or manifest.get('version') != 1
                    or manifest.get('schema_version') != 1):
                raise ValueError('Unsupported memory bundle')
            spec = manifest['files']['memory.db']
            if archive.getinfo('memory.db').file_size > 1024 * 1024 * 1024:
                raise ValueError('Memory bundle too large')
            copy = staging / 'memory.db'
            with archive.open('memory.db') as source, copy.open('wb') as destination:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
            if copy.stat().st_size != spec['size'] or digest(copy) != spec['sha256']:
                raise ValueError('Memory checksum mismatch')
        with closing(sqlite3.connect(copy)) as conn:
            if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Memory integrity check failed')
        store = MemoryStore(copy)
        store.initialize()
        if store.status().get('space_id') != manifest['space_id']:
            raise ValueError('Memory space mismatch')
        # Explicit core method clears writer-local export state and disables processing/sync.
        store.reset_replica_identity(str(uuid4()))
        if target.exists():
            if any(target.iterdir()):
                raise FileExistsError('Restore target changed')
            target.rmdir()
        staging.replace(target)
        return target / 'memory.db'
    finally:
        if staging.exists():
            shutil.rmtree(staging)
