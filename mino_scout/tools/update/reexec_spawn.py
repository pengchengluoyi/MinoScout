"""Restart the same installed Scout after its current process exits."""
from __future__ import annotations

import base64
import os
import re
import shlex
import subprocess
import sys
import threading

from mino_scout.config import config_dir
from mino_scout.tools.keep_alive.log import SLog

TAG = "ReexecSpawn"
_lock = threading.Lock()
_scheduled = False


def schedule_reexec(*, delay_sec: float = 1.5) -> dict[str, object]:
    global _scheduled
    if str(os.environ.get("MINO_SCOUT_NO_REEXEC") or "").strip():
        return {"ok": True, "skipped": True}
    with _lock:
        if _scheduled:
            return {"ok": True, "already_scheduled": True}
        result = _schedule_reexec(delay_sec=delay_sec)
        _scheduled = True
        return result


def _schedule_reexec(*, delay_sec: float) -> dict[str, object]:
    from mino_scout.tools.keep_alive.process_control import run_argv
    argv = run_argv()
    env = {**os.environ, "MINO_SCOUT_HOME": str(config_dir())}
    delay = max(0.4, float(delay_sec))
    if os.name == "nt":
        literal = lambda value: "'" + str(value).replace("'", "''") + "'"
        script = f"""$ErrorActionPreference = 'Stop'
$old = Get-Process -Id {os.getpid()} -ErrorAction SilentlyContinue
if ($old -and -not $old.WaitForExit(120000)) {{ throw 'Scout did not exit; restart cancelled' }}
Start-Sleep -Milliseconds {int(delay * 1000)}
Set-Location -LiteralPath {literal(config_dir())}
Start-Process -FilePath {literal(argv[0])} -ArgumentList {literal(subprocess.list2cmdline(argv[1:]))} -WindowStyle Hidden -Wait
"""
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        subprocess.Popen(["powershell.exe", "-NoProfile", "-EncodedCommand", encoded], env=env,
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW, close_fds=True)
    else:
        target = _launchd_target()
        command = ["launchctl", "kickstart", target] if target else argv
        script = (
            f"attempt=0; while kill -0 {os.getpid()} 2>/dev/null; do "
            '[ "$attempt" -lt 600 ] || exit 1; sleep 0.2; attempt=$((attempt + 1)); done; '
            f"sleep {delay}; cd {shlex.quote(str(config_dir()))} || exit 1; exec {shlex.join(command)}"
        )
        subprocess.Popen(["/bin/sh", "-c", script], env=env, start_new_session=True, close_fds=True)
    SLog.i(TAG, "已安排重启以加载新版本")
    return {"ok": True, "mode": "frozen" if getattr(sys, "frozen", False) or len(argv) == 2 else "python"}


def _launchd_target() -> str:
    if sys.platform != "darwin":
        return ""
    for target in (f"gui/{os.getuid()}/com.mino.scout", "system/com.mino.scout"):
        try:
            result = subprocess.run(["launchctl", "print", target], capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            continue
        match = re.search(r"(?m)^\s*pid = (\d+)\s*$", result.stdout)
        if result.returncode == 0 and match and int(match.group(1)) == os.getpid():
            return target
    return ""
