"""Verified, local-only Windows self update for the installed desktop app."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from threading import Lock, Thread

import httpx

from . import __version__

RELEASES_URL = "https://api.github.com/repos/zznmdhz/agent-workbench/releases?per_page=20"
RELEASE_URL = "https://github.com/zznmdhz/agent-workbench/releases/download/"
VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-.*)?$")
MAX_INSTALLER_BYTES = 200 * 1024 * 1024


def _version(value: str) -> tuple[int, int, int] | None:
    match = VERSION.fullmatch(value)
    return tuple(map(int, match.groups())) if match else None


def select_release(releases: list[dict], current: str) -> dict | None:
    """Choose the newest published installer with a GitHub SHA-256 digest."""
    current_number = _version(current)
    if current_number is None:
        return None
    candidates = []
    for release in releases:
        tag = release.get("tag_name", "")
        number = _version(tag)
        if release.get("draft") or release.get('prerelease') or number is None or number <= current_number:
            continue
        name = f"AgentWorkbench-Setup-{'.'.join(map(str, number))}-Windows-x64.exe"
        for asset in release.get("assets", []):
            url = asset.get("browser_download_url", "")
            digest = asset.get("digest", "")
            size = asset.get("size", 0)
            if (asset.get("name") == name and asset.get("state") == "uploaded"
                    and url == RELEASE_URL + tag + "/" + name
                    and re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
                    and isinstance(size, int) and 0 < size <= MAX_INSTALLER_BYTES):
                candidates.append((number, {"version": '.'.join(map(str, number)), "tag": tag,
                                            "name": name, "url": url, "sha256": digest[7:],
                                            "size": size}))
                break
    return max(candidates, default=((), None), key=lambda item: item[0])[1]


UPDATER_SCRIPT = r"""param([string]$Installer, [string]$TargetExe, [int]$ParentPid, [string]$LogPath, [string]$FailurePath, [string]$Rollback, [string]$DbPath, [string]$Maintenance, [string]$ExpectedVersion)
$ErrorActionPreference = 'Stop'
try {
    for ($i = 0; $i -lt 120; $i++) {
        if (-not (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 500
    }
    if (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) {
        throw 'Old Agent Workbench process did not exit within 60 seconds.'
    }
    $process = Start-Process -FilePath $Installer -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CLOSEAPPLICATIONS', '/FORCECLOSEAPPLICATIONS', '/NORESTARTAPPLICATIONS') -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -ne 0) { throw "Installer exited with code $($process.ExitCode)." }
    if (-not (Test-Path -LiteralPath $TargetExe)) { throw 'Installed executable is missing.' }
    Remove-Item -LiteralPath $Maintenance -Force -ErrorAction SilentlyContinue
    Start-Process -FilePath $TargetExe -ArgumentList '--no-browser' -WorkingDirectory (Split-Path -Parent $TargetExe) -WindowStyle Hidden
    $healthy = $false
    for ($j = 0; $j -lt 90; $j++) {
        try {
            $ready = Invoke-RestMethod 'http://127.0.0.1:8765/health/ready' -TimeoutSec 2
            if ($ready.app_version -eq $ExpectedVersion) { $healthy = $true; break }
        } catch {}
        Start-Sleep -Seconds 1
    }
    if (-not $healthy) { throw 'Updated background did not become healthy.' }
    Remove-Item -LiteralPath $FailurePath -Force -ErrorAction SilentlyContinue
    "Update succeeded at $(Get-Date -Format o)" | Set-Content -LiteralPath $LogPath -Encoding UTF8
} catch {
    "Update failed at $(Get-Date -Format o): $_" | Set-Content -LiteralPath $LogPath -Encoding UTF8
    "$_" | Set-Content -LiteralPath $FailurePath -Encoding UTF8
    if ($Rollback -and (Test-Path -LiteralPath (Join-Path $Rollback 'program'))) {
        # Close only processes belonging to this exact installation before restoration.
        $targetDirectory = [IO.Path]::GetFullPath((Split-Path -Parent $TargetExe))
        Get-Process -Name AgentWorkbench -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $TargetExe } | Stop-Process -Force
        Copy-Item -Path (Join-Path $Rollback 'program\*') -Destination $targetDirectory -Recurse -Force
        if ($DbPath -and (Test-Path -LiteralPath (Join-Path $Rollback 'database.db'))) {
            foreach ($suffix in @('-wal', '-shm')) {
                Remove-Item -LiteralPath ($DbPath + $suffix) -Force -ErrorAction SilentlyContinue
            }
            Copy-Item -LiteralPath (Join-Path $Rollback 'database.db') -Destination $DbPath -Force
        }
    }
    Remove-Item -LiteralPath $Maintenance -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $TargetExe) {
        Start-Process -FilePath $TargetExe -ArgumentList '--no-browser' -WorkingDirectory (Split-Path -Parent $TargetExe) -WindowStyle Hidden
    }
    exit 1
}
"""


class UpdateManager:
    def __init__(self, executable: Path | None = None, data_root: Path | None = None,
                 db_path: Path | None = None):
        self.executable = (executable or Path(sys.executable)).resolve()
        app_data = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        self.data_root = (data_root or app_data / "AgentWorkbench" / "updates").resolve()
        self.available = (os.name == "nt" and getattr(sys, "frozen", False)
                          and self.executable.name.lower() == "agentworkbench.exe"
                          and not (self.executable.parent / "portable.flag").exists())
        self._lock = Lock()
        self._release: dict | None = None
        self._state = "idle"
        self._error: str | None = None
        self._progress = 0
        self.db_path = db_path

    def backup(self) -> Path | None:
        if not self.executable.is_file():
            return None  # Isolated tests do not have an installed application.
        destination = self.data_root / f'rollback-{__version__}-{time.time_ns()}'
        shutil.copytree(self.executable.parent, destination/'program')
        if self.db_path and self.db_path.is_file():
            with sqlite3.connect(self.db_path.resolve().as_uri()+'?mode=ro', uri=True) as source:
                with sqlite3.connect(destination/'database.db') as target:
                    source.backup(target)
        return destination

    def status(self) -> dict:
        with self._lock:
            return {"available": self.available, "current_version": __version__,
                    "state": self._state, "latest_version": self._release["version"] if self._release else None,
                    "progress": self._progress, "error": self._error}

    def check(self) -> dict:
        if not self.available:
            return self.status()
        with self._lock:
            if self._state in {"downloading", "installing"}:
                busy = True
            else:
                busy = False
        if busy:
            return self.status()
        try:
            with httpx.Client(timeout=12, follow_redirects=True,
                              headers={"Accept": "application/vnd.github+json",
                                       "User-Agent": "AgentWorkbench-Updater"}) as client:
                response = client.get(RELEASES_URL)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, list):
                    raise ValueError("Unexpected GitHub release response")
                release = select_release(payload, __version__)
            with self._lock:
                self._release = release
                failure = self.data_root / f"failed-{release['version']}.txt" if release else None
                if failure and failure.exists():
                    self._state = "error"
                    self._error = "上次自动安装失败。可点击重试，或从 GitHub Releases 下载安装包。"
                else:
                    self._state = "available" if release else "current"
                    self._error = None
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            with self._lock:
                self._state = "error"
                self._error = f"更新检查失败：{exc}"
        return self.status()

    def start(self, shutdown_callback) -> dict:
        if not self.available:
            raise ValueError("Automatic updates require the installed Windows app")
        with self._lock:
            if self._state not in {"available", "error"} or self._release is None:
                raise ValueError("No verified update is available")
            release = self._release.copy()
            self._state = "downloading"
            self._progress = 0
        (self.data_root / f"failed-{release['version']}.txt").unlink(missing_ok=True)
        Thread(target=self._download_and_install, args=(release, shutdown_callback),
               name="awb-updater", daemon=True).start()
        return self.status()

    def _download_and_install(self, release: dict, shutdown_callback) -> None:
        try:
            self.data_root.mkdir(parents=True, exist_ok=True)
            destination = self.data_root / release["name"]
            temporary = destination.with_suffix(".download")
            digest = hashlib.sha256()
            received = 0
            try:
                with httpx.Client(timeout=httpx.Timeout(30, read=60), follow_redirects=True,
                                  headers={"User-Agent": "AgentWorkbench-Updater"}) as client:
                    with client.stream("GET", release["url"]) as response:
                        response.raise_for_status()
                        with temporary.open("wb") as stream:
                            for chunk in response.iter_bytes(1024 * 1024):
                                received += len(chunk)
                                if received > release["size"]:
                                    raise ValueError("Installer exceeds published size")
                                digest.update(chunk)
                                stream.write(chunk)
                                with self._lock:
                                    self._progress = min(99, int(received * 100 / release["size"]))
                if received != release["size"] or digest.hexdigest() != release["sha256"]:
                    raise ValueError("Installer SHA-256 or size did not match GitHub release")
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
            script = self.data_root / "run-update.ps1"
            rollback = self.backup()
            maintenance = self.data_root / 'maintenance.json'
            maintenance.write_text(json.dumps({'started_at': time.time(), 'parent_pid': os.getpid()}),
                                   encoding='utf-8')
            script.write_text(UPDATER_SCRIPT, encoding="utf-8-sig")
            log = self.data_root / "last-update.log"
            failure = self.data_root / f"failed-{release['version']}.txt"
            subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                              "-WindowStyle", "Hidden", "-File", str(script),
                              "-Installer", str(destination), "-TargetExe", str(self.executable),
                              "-ParentPid", str(os.getpid()), "-LogPath", str(log),
                              "-FailurePath", str(failure), '-Rollback', str(rollback or ''),
                              '-DbPath', str(self.db_path or ''), '-Maintenance', str(maintenance),
                              '-ExpectedVersion', release['version']],
                            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                           | getattr(subprocess, "DETACHED_PROCESS", 0)),
                             close_fds=True)
            with self._lock:
                self._state = "installing"
                self._progress = 100
            shutdown_callback()
        except (OSError, ValueError, sqlite3.Error, httpx.HTTPError) as exc:
            (self.data_root/'maintenance.json').unlink(missing_ok=True)
            with self._lock:
                self._state = "error"
                self._error = f"自动更新失败：{exc}"
