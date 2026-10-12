"""Restart the same installed Scout after its current process exits."""
from __future__ import annotations

import base64
import os
import shlex
import subprocess
import sys
from pathlib import Path

from mino_scout.config import config_dir
from mino_scout.log import SLog

TAG = "ReexecSpawn"


def schedule_reexec(*, delay_sec: float = 1.5) -> dict[str, object]:
    if str(os.environ.get("MINO_SCOUT_NO_REEXEC") or "").strip():
        return {"ok": True, "skipped": True}
    from mino_scout.process_control import run_argv
    argv = run_argv()
    env = {**os.environ, "MINO_SCOUT_HOME": str(config_dir())}
    delay = max(0.4, float(delay_sec))
    if os.name == "nt":
        literal = lambda value: "'" + str(value).replace("'", "''") + "'"
        args = ", ".join(literal(arg) for arg in argv[1:])
        script = f"""$ErrorActionPreference = 'Stop'
$old = Get-Process -Id {os.getpid()} -ErrorAction SilentlyContinue
if ($old -and -not $old.WaitForExit(120000)) {{ throw 'Scout did not exit; restart cancelled' }}
Start-Sleep -Milliseconds {int(delay * 1000)}
Set-Location -LiteralPath {literal(config_dir())}
& {literal(argv[0])} @({args})
"""
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        subprocess.Popen(["powershell.exe", "-NoProfile", "-EncodedCommand", encoded], env=env,
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS, close_fds=True)
    else:
        script = f"sleep {delay}; cd {shlex.quote(str(config_dir()))}; exec {shlex.join(argv)}"
        subprocess.Popen(["/bin/sh", "-c", script], env=env, start_new_session=True, close_fds=True)
        _kickstart_launchd(delay_sec=delay + 0.8)
    SLog.i(TAG, "已安排重启以加载新版本")
    return {"ok": True, "mode": "frozen" if getattr(sys, "frozen", False) or len(argv) == 2 else "python"}


def _kickstart_launchd(*, delay_sec: float) -> None:
    if sys.platform != "darwin":
        return
    target = f"gui/{os.getuid()}/com.mino.scout"
    plist = Path.home() / "Library/LaunchAgents/com.mino.scout.plist"
    if not plist.is_file():
        return
    script = (f"sleep {max(0.5, float(delay_sec))}; "
              f"launchctl kickstart -k {shlex.quote(target)} 2>/dev/null || "
              f"launchctl bootstrap gui/{os.getuid()} {shlex.quote(str(plist))} 2>/dev/null")
    try:
        subprocess.Popen(["/bin/sh", "-c", script], start_new_session=True, close_fds=True)
    except OSError as exc:
        SLog.w(TAG, f"launchd restart failed: {type(exc).__name__}")
