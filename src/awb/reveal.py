"""Open a local folder only for a path established by session evidence."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def session_folder(detail: dict, kind: str, path: str | None = None) -> Path:
    if kind == 'workspace':
        candidate = detail.get('cwd')
        if path is not None:
            raise ValueError('Unexpected path')
        folder = Path(candidate) if isinstance(candidate, str) else None
    elif kind == 'source':
        sources = [row['path'] for row in detail.get('sources', [])]
        candidate = path or next((item for item in sources if Path(item).parent.is_dir()), None)
        if candidate not in sources:
            raise ValueError('Source path is not part of this session')
        folder = Path(candidate).parent
    elif kind == 'file':
        paths = {row['native_path'] for row in detail.get('file_events', [])}
        if path not in paths:
            raise ValueError('File path is not part of this session')
        folder = Path(path).parent
    else:
        raise ValueError('Unknown folder kind')
    if folder is None or not folder.is_absolute() or not folder.is_dir():
        raise FileNotFoundError('The local folder is unavailable')
    return folder.resolve()


def open_folder(folder: Path) -> None:
    if sys.platform == 'darwin':
        command = ['open', str(folder)]
    elif sys.platform == 'win32':
        command = ['explorer.exe', str(folder)]
    else:
        command = ['xdg-open', str(folder)]
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
