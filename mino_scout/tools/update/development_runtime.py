"""Source-only process entry. Never manages an installed Scout service."""
from __future__ import annotations
import os
import subprocess
import sys
import time
from pathlib import Path


def spawn() -> subprocess.Popen:
    from mino_scout.config import config_dir
    root = Path(__file__).resolve().parents[3]
    env = dict(os.environ)
    env.pop("MINO_SCOUT_HOME", None)
    env["MINO_SCOUT_DEV_HOME"] = str(config_dir())
    kwargs = {"cwd": str(root), "env": env, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen([sys.executable, "-m", "mino_scout.cli", "run"], **kwargs)


def start(*, wait_sec: float = 4.0) -> int:
    from mino_scout.service import pid_alive, read_pid, cmd_public_status
    if pid_alive(read_pid()):
        return cmd_public_status()
    process = spawn()
    deadline = time.monotonic() + max(0.5, wait_sec)
    while time.monotonic() < deadline:
        if pid_alive(read_pid()):
            return cmd_public_status()
        if process.poll() is not None:
            break
        time.sleep(0.25)
    cmd_public_status()
    return 1
