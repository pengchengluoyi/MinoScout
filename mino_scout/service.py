"""本机守护进程的 pid / status / stop。

Studio 和运维都走这三件事，不要再让人去 `launchctl unload`。
`run` 写 pid；`status` 看进程在不在；`stop` 发 SIGTERM，Scout 自己发
`NODE_EVENT shutting_down` 再退出。launchd / systemd 的 KeepAlive 配的是
Crashed / on-failure，正常退出不会被拉起来。
"""
from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from typing import Any

from mino_scout.config import config_dir, config_path, load_config, resolve_runtime, resolve_scout_id
from mino_scout.log import SLog

TAG = "Service"


def pid_path() -> Path:
    return config_dir() / "scout.pid"


def write_pid(pid: int | None = None) -> Path:
    path = pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(int(pid or os.getpid())), encoding="utf-8")
    return path


def read_pid() -> int:
    path = pid_path()
    if not path.is_file():
        return 0
    try:
        return int((path.read_text(encoding="utf-8") or "").strip() or "0")
    except (OSError, ValueError):
        return 0


def clear_pid(*, only_if: int = 0) -> None:
    path = pid_path()
    if only_if:
        if read_pid() != only_if:
            return
    try:
        path.unlink()
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    if os.name == "posix":
        # 僵尸 / 复用：/proc 不存在就当死了（Linux）；macOS kill(0) 已够用
        proc = Path(f"/proc/{pid}")
        if proc.exists() and proc.is_dir():
            try:
                stat = (proc / "stat").read_text(encoding="utf-8")
                # state Z = zombie
                parts = stat.split()
                if len(parts) > 2 and parts[2] == "Z":
                    return False
            except OSError:
                pass
    return True


def collect_status() -> dict[str, Any]:
    pid = read_pid()
    running = pid_alive(pid)
    ws, token = resolve_runtime()
    cfg = load_config()
    out: dict[str, Any] = {
        "running": running,
        "pid": pid if running else 0,
        "pid_file": str(pid_path()),
        "config": str(config_path()),
        "nexus": ws,
        "has_token": bool(token),
        "version": str(cfg.get("version") or ""),
        "scout_id": resolve_scout_id(),
    }
    if running:
        try:
            from mino_scout.power import get_guard

            out["power"] = get_guard().status()
        except Exception:
            out["power"] = {}
    return out


def schedule_reexec(*, delay_sec: float = 1.5) -> dict[str, Any]:
    """NODE_COMMAND restart：等本进程退出后再拉起同一条命令行。"""
    if str(os.environ.get("MINO_SCOUT_NO_REEXEC") or "").strip():
        return {"ok": True, "skipped": True}
    import subprocess
    import sys

    argv = [a for a in list(sys.argv) if a]
    if not argv:
        argv = [sys.executable, "-m", "mino_scout"]
    helper = (
        "import time,subprocess,sys;"
        f"time.sleep({max(0.4, float(delay_sec))});"
        "subprocess.Popen(sys.argv[1:], start_new_session=True)"
    )
    subprocess.Popen(
        [sys.executable, "-c", helper, *argv],
        start_new_session=True,
        close_fds=True,
    )
    return {"ok": True, "skipped": False}


def request_stop(*, timeout_sec: float = 15.0) -> dict[str, Any]:
    pid = read_pid()
    if not pid_alive(pid):
        clear_pid()
        return {"ok": True, "already": True, "running": False, "pid": 0}

    SLog.i(TAG, f"停止 pid={pid}")
    _signal(pid, signal.SIGTERM)
    deadline = time.time() + max(1.0, timeout_sec)
    while time.time() < deadline:
        if not pid_alive(pid):
            clear_pid(only_if=pid)
            return {"ok": True, "already": False, "running": False, "pid": pid}
        time.sleep(0.2)

    SLog.w(TAG, f"SIGTERM 超时，改发 SIGKILL pid={pid}")
    _signal(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    time.sleep(0.4)
    alive = pid_alive(pid)
    if not alive:
        clear_pid(only_if=pid)
    return {"ok": not alive, "already": False, "running": alive, "pid": pid, "killed": True}


def _signal(pid: int, sig: int) -> None:
    if os.name == "nt" and sig != signal.SIGTERM:
        # Windows 没有 SIGKILL，用 taskkill
        import subprocess

        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F", "/T"],
            capture_output=True, timeout=8,
        )
        return
    try:
        os.kill(pid, sig)
    except OSError as exc:
        SLog.w(TAG, f"kill({pid}, {sig}) 失败: {exc}")
