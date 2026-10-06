"""OS-released single-instance locks, scoped to one user's database."""
from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def lock(path: Path):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        name = 'Local\\AgentWorkbench-' + hashlib.sha256(str(path.resolve()).lower().encode()).hexdigest()
        ctypes.set_last_error(0)
        handle = kernel.CreateMutexW(None, False, name)
        if not handle:
            raise OSError(ctypes.get_last_error(), 'Cannot create background lock')
        held = ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
        try:
            yield held
        finally:
            kernel.CloseHandle(handle)
    else:
        import fcntl
        with path.with_suffix('.lock').open('a') as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
            except BlockingIOError:
                held = False
            yield held
