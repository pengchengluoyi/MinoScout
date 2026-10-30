"""Local process control without signals on Windows or interpreter flags on frozen apps."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from mino_scout.config import config_dir


def run_argv() -> list[str]:
    from mino_scout.config import development
    if development():
        return [sys.executable, "-m", "mino_scout.cli", "run"]
    if getattr(sys, "frozen", False):
        return [sys.executable, "run"]
    installed = config_dir() / "bin" / ("mino-scout.exe" if os.name == "nt" else "mino-scout")
    if installed.is_file():
        return [str(installed), "run"]
    return [sys.executable, "-m", "mino_scout", "run"]


def spawn_run() -> subprocess.Popen:
    from mino_scout.config import development
    if development():
        from mino_scout.tools.update.development_runtime import spawn
        return spawn()
    env = {**os.environ, "MINO_SCOUT_HOME": str(config_dir())}
    kwargs = {"cwd": str(config_dir()), "env": env, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(run_argv(), **kwargs)


def stop_path() -> Path:
    return config_dir() / "scout_stop.json"


def request_local_stop(pid: int) -> None:
    import psutil
    started = psutil.Process(pid).create_time()
    path = stop_path()
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps({"pid": pid, "created_at": started, "requested_at": time.time()}), encoding="utf-8")
    tmp.replace(path)


def local_stop_requested() -> bool:
    try:
        import psutil
        data = json.loads(stop_path().read_text(encoding="utf-8-sig"))
        return (data.get("pid") == os.getpid()
                and abs(float(data.get("created_at", 0)) - psutil.Process().create_time()) < 0.01)
    except (OSError, ValueError, TypeError, psutil.Error):
        return False


def terminate_windows(pid: int, created_at: float) -> None:
    """Last resort targets the exact process, never every process named Scout."""
    import psutil
    try:
        process = psutil.Process(pid)
        if abs(process.create_time() - created_at) < 0.01:
            process.terminate()
    except psutil.NoSuchProcess:
        pass
