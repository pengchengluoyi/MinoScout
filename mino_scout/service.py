"""本机守护进程的 pid / status / stop。

Studio 和运维都走这三件事，不要再让人去 `launchctl unload`。
`run` 写 pid；`status` 看进程在不在；`stop` 发 SIGTERM，Scout 自己发
`EXECUTE node.shutting_down` 再退出。launchd / systemd 的 KeepAlive 配的是
Crashed / on-failure，正常退出不会被拉起来。
"""
from __future__ import annotations

import json
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


def public_status() -> dict[str, Any]:
    """不含 token 明文，供裸 `mino-scout` 默认输出。"""
    st = collect_status()
    st.pop("has_token", None)
    return st


def cmd_public_status() -> int:
    print(json.dumps(public_status(), ensure_ascii=False, indent=2))
    return 0


def try_start_service(*, wait_sec: float = 4.0) -> int:
    """拉起 launchd / systemd；未注册服务时在后台 exec `mino-scout run`。"""
    import subprocess
    import sys

    if pid_alive(read_pid()):
        return cmd_public_status()

    started = False
    if sys.platform == "darwin":
        uid = os.getuid()
        target = f"gui/{uid}/com.mino.scout"
        for cmd in (
            ["launchctl", "kickstart", "-k", target],
            ["launchctl", "bootstrap", f"gui/{uid}", str(Path.home() / "Library/LaunchAgents/com.mino.scout.plist")],
        ):
            try:
                subprocess.run(cmd, capture_output=True, timeout=15)
                started = True
            except (OSError, subprocess.TimeoutExpired):
                continue
    elif sys.platform.startswith("linux"):
        for cmd in (
            ["systemctl", "--user", "start", "mino-scout.service"],
            ["systemctl", "start", "mino-scout.service"],
        ):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=15)
                if r.returncode == 0:
                    started = True
                    break
            except (OSError, subprocess.TimeoutExpired):
                continue

    deadline = time.time() + max(0.5, wait_sec)
    while time.time() < deadline:
        if pid_alive(read_pid()):
            return cmd_public_status()
        time.sleep(0.25)

    if not started:
        SLog.i(TAG, "未找到系统服务，后台启动 mino-scout run")
        subprocess.Popen(
            [sys.executable, "-m", "mino_scout", "run"],
            start_new_session=True,
            close_fds=True,
        )
        while time.time() < deadline:
            if pid_alive(read_pid()):
                return cmd_public_status()
            time.sleep(0.25)

    st = public_status()
    print(json.dumps(st, ensure_ascii=False, indent=2))
    return 0 if st.get("running") else 1


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
            from mino_scout.app_version import imported_scout_version, read_app_semver

            out["process_version"] = imported_scout_version()
            out["app_semver"] = read_app_semver()
        except Exception:
            pass
    bin_path = config_dir() / "bin" / "mino-scout"
    if bin_path.is_file():
        out["binary"] = str(bin_path)
    layers_file = config_dir() / "bin" / "layers.txt"
    if layers_file.is_file():
        try:
            from mino_scout.install_plan import parse_layers_txt

            layers = parse_layers_txt(layers_file.read_text(encoding="utf-8"))
            if layers:
                out["installed_layers"] = layers
                if layers.get("app"):
                    out["version"] = out.get("version") or str(cfg.get("version") or "")
        except Exception:
            pass
    if running:
        try:
            from mino_scout.power import get_guard

            out["power"] = get_guard().status()
        except Exception:
            out["power"] = {}
    return out


def schedule_reexec(*, delay_sec: float = 1.5) -> dict[str, Any]:
    """NODE 指令 restart/update：等本进程退出后再拉起 bin/mino-scout run。"""
    if str(os.environ.get("MINO_SCOUT_NO_REEXEC") or "").strip():
        return {"ok": True, "skipped": True}
    import subprocess
    import sys

    from mino_scout.config import config_dir

    frozen_bin = config_dir() / "bin" / "mino-scout"
    if frozen_bin.is_file():
        argv = [str(frozen_bin), "run"]
    else:
        argv = [a for a in list(sys.argv) if a]
        if not argv:
            argv = [sys.executable, "-m", "mino_scout", "run"]
        elif len(argv) == 1 or argv[-1] not in ("run",):
            argv = [*argv[:1], "run"] if argv[0].endswith("mino-scout") else [sys.executable, "-m", "mino_scout", "run"]
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
