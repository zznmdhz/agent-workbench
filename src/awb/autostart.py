"""Current-user background registration, never a SYSTEM-account collector."""
from __future__ import annotations

import base64
import getpass
import hashlib
import os
import plistlib
import subprocess
import sys
from pathlib import Path

TASK_NAME = 'AgentWorkbench-' + hashlib.sha256(str(Path.home()).lower().encode()).hexdigest()[:8]
MAC_LABEL = 'com.agentworkbench.background'


def mac_config(executable: Path) -> dict:
    return {'Label': MAC_LABEL, 'ProgramArguments': [str(executable.resolve()), '--background'],
            'RunAtLoad': True, 'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval': 30,
            'WorkingDirectory': str(executable.resolve().parent),
            'StandardOutPath': str(Path.home()/'Library/Logs/AgentWorkbench-background.log'),
            'StandardErrorPath': str(Path.home()/'Library/Logs/AgentWorkbench-background-error.log')}


def mac_path() -> Path:
    return Path.home()/'Library/LaunchAgents'/f'{MAC_LABEL}.plist'


def mac_target() -> str:
    return f'gui/{os.getuid()}/{MAC_LABEL}'


def powershell(script: str, timeout: float = 30) -> subprocess.CompletedProcess:
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    return subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
                           '-EncodedCommand', encoded], capture_output=True, text=True, timeout=timeout,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def register(executable: Path) -> None:
    if sys.platform == 'darwin':
        path = mac_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['launchctl', 'bootout', mac_target()], capture_output=True, timeout=15)
        path.write_bytes(plistlib.dumps(mac_config(executable)))
        result = subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)],
                                capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise RuntimeError('Unable to register user LaunchAgent: '+result.stderr[-240:])
        return
    if os.name != 'nt':
        raise RuntimeError('Automatic background registration is currently available on Windows')
    script = f"""$ErrorActionPreference = 'Stop'
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$action = New-ScheduledTaskAction -Execute {quote(str(executable.resolve()))} -Argument '--background' -WorkingDirectory {quote(str(executable.resolve().parent))}
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $sid
$trigger.Delay = 'PT10S'
$principal = New-ScheduledTaskPrincipal -UserId $sid -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName {quote(TASK_NAME)} -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Agent Workbench user background collector and tray icon' -Force | Out-Null
"""
    result = powershell(script)
    if result.returncode:
        raise RuntimeError('无法注册登录启动任务：' + result.stderr.strip()[-300:])


def registered() -> bool:
    if sys.platform == 'darwin':
        return mac_path().is_file() and subprocess.run(['launchctl', 'print', mac_target()],
                  capture_output=True, timeout=15).returncode == 0
    if os.name != 'nt':
        return False
    result = powershell(f"$task = Get-ScheduledTask -TaskName {quote(TASK_NAME)} -ErrorAction SilentlyContinue; if ($task) {{ exit 0 }} else {{ exit 1 }}", timeout=15)
    return result.returncode == 0


def start() -> None:
    if sys.platform == 'darwin':
        result = subprocess.run(['launchctl', 'kickstart', mac_target()], capture_output=True,
                                text=True, timeout=15)
        if result.returncode:
            raise RuntimeError('Unable to start user LaunchAgent: '+result.stderr[-240:])
        return
    result = powershell(f"$ErrorActionPreference='Stop'; Start-ScheduledTask -TaskName {quote(TASK_NAME)}")
    if result.returncode:
        raise RuntimeError('无法启动后台任务：' + result.stderr.strip()[-240:])


def unregister() -> None:
    if sys.platform == 'darwin':
        subprocess.run(['launchctl', 'bootout', mac_target()], capture_output=True, timeout=15)
        mac_path().unlink(missing_ok=True)
        return
    if os.name == 'nt':
        result = powershell(f"$ErrorActionPreference='Stop'; Unregister-ScheduledTask -TaskName {quote(TASK_NAME)} -Confirm:$false -ErrorAction SilentlyContinue")
        if result.returncode:
            raise RuntimeError('无法移除登录启动任务')


def user_name() -> str:
    return getpass.getuser()
