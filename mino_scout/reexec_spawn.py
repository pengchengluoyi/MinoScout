"""进程内热更后拉起新 Scout —— 必须从未 import 过的模块加载，否则会用到内存里的旧 service.py。

分层只换磁盘上的 app/*.py，已 import 的 `mino_scout.service` 仍是旧版 schedule_reexec（mino-scout -c 会失败）。
"""
from __future__ import annotations

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

    delay = max(0.4, float(delay_sec))
    home = config_dir()
    frozen_bin = home / "bin" / "mino-scout"
    if not frozen_bin.is_file():
        run_argv = [sys.executable, "-m", "mino_scout", "run"]
        helper = (
            "import time,subprocess,sys;"
            f"time.sleep({delay});"
            "subprocess.Popen(sys.argv[1:], start_new_session=True, close_fds=True, cwd=sys.argv[2])"
        )
        subprocess.Popen(
            [sys.executable, "-c", helper, *run_argv, str(home)],
            start_new_session=True,
            close_fds=True,
        )
        return {"ok": True, "mode": "python"}

    quoted = shlex.quote(str(frozen_bin))
    cwd = shlex.quote(str(home))
    if os.name == "nt":
        cmd = f'timeout /t {max(1, int(delay))} /nobreak >nul & cd /d {cwd} & "{frozen_bin}" run'
        subprocess.Popen(
            ["cmd.exe", "/c", cmd],
            start_new_session=True,
            close_fds=True,
        )
    else:
        script = f"sleep {delay}; cd {cwd}; exec {quoted} run"
        subprocess.Popen(
            ["/bin/sh", "-c", script],
            start_new_session=True,
            close_fds=True,
        )

    _kickstart_launchd(delay_sec=delay + 0.8)
    SLog.i(TAG, f"已安排 reexec（{delay}s 后 exec mino-scout run + launchd kickstart）")
    return {"ok": True, "mode": "frozen"}


def _kickstart_launchd(*, delay_sec: float) -> None:
    if sys.platform != "darwin":
        return
    uid = os.getuid()
    target = f"gui/{uid}/com.mino.scout"
    plist = Path.home() / "Library/LaunchAgents/com.mino.scout.plist"
    if not plist.is_file():
        return
    script = (
        f"sleep {max(0.5, float(delay_sec))}; "
        f"launchctl kickstart -k {shlex.quote(target)} 2>/dev/null || "
        f"launchctl bootstrap gui/{uid} {shlex.quote(str(plist))} 2>/dev/null"
    )
    try:
        subprocess.Popen(
            ["/bin/sh", "-c", script],
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        SLog.w(TAG, f"launchd kickstart 安排失败: {exc}")
