"""本机守护进程的 pid / status / stop。

Studio 和运维都走这三件事，不要再让人去 `launchctl unload`。
`run` 在 `scout.lock` 上 flock 后写 pid 再 dial Nexus，避免 kickstart 与旧进程双 REGISTER；
`status` 看进程在不在；`stop` 发 SIGTERM，Scout 自己发
`EXECUTE node.shutting_down` 再退出。launchd / systemd 的 KeepAlive 配的是
Crashed / on-failure，正常退出不会被拉起来。
"""
from __future__ import annotations

import json
import os
import signal
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from mino_scout.config import config_dir, config_path, load_config, resolve_runtime, resolve_scout_id
from mino_scout.log import SLog

TAG = "Service"

_STALE_LOCK_WAIT_SEC = 5.0
_STALE_LOCK_POLL_SEC = 0.12


def pid_path() -> Path:
    return config_dir() / "scout.pid"


def lock_path() -> Path:
    """`mino-scout run` 单实例锁；持有期间才允许 dial Nexus。"""
    return config_dir() / "scout.lock"


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


def _pid_from_lock_file(fh: Any) -> int:
    try:
        fh.seek(0)
        raw = (fh.read() or b"").decode("utf-8", errors="ignore").strip()
    except OSError:
        return read_pid()
    if not raw:
        return read_pid()
    try:
        return int(raw.split()[0])
    except ValueError:
        return read_pid()


class _RunLockHold:
    def __init__(self) -> None:
        self._fh: Any = None

    def try_acquire(self, *, stale_wait_sec: float = _STALE_LOCK_WAIT_SEC) -> tuple[bool, str]:
        path = lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + max(0.5, float(stale_wait_sec))
        last_holder = 0
        while True:
            fh = open(path, "a+b")
            if self._try_flock_nb(fh):
                self._fh = fh
                pid = os.getpid()
                fh.seek(0)
                fh.truncate()
                fh.write(f"{pid}\n".encode("utf-8"))
                fh.flush()
                return True, ""
            holder = _pid_from_lock_file(fh)
            last_holder = holder or last_holder
            fh.close()
            if holder and pid_alive(holder) and holder != os.getpid():
                return False, f"已有 Scout 在运行（pid={holder}），本进程跳过 dial"
            if time.time() >= deadline:
                if holder and pid_alive(holder):
                    return False, f"无法取得单实例锁（pid={holder} 仍存活）"
                return False, "无法取得单实例锁（等待旧进程释放超时）"
            time.sleep(_STALE_LOCK_POLL_SEC)

    @staticmethod
    def _try_flock_nb(fh: Any) -> bool:
        if os.name == "nt":
            import msvcrt

            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        import fcntl

        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
        except OSError:
            return False

    def release(self) -> None:
        fh = self._fh
        self._fh = None
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass


@contextmanager
def daemon_run_lock(*, stale_wait_sec: float = _STALE_LOCK_WAIT_SEC) -> Iterator[bool]:
    """Yield True 表示已取得 flock，可 dial Nexus；False 时调用方应直接退出 run。"""
    hold = _RunLockHold()
    ok, msg = hold.try_acquire(stale_wait_sec=stale_wait_sec)
    if not ok:
        if msg:
            SLog.i(TAG, msg)
        yield False
        return
    try:
        write_pid()
        yield True
    finally:
        hold.release()


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
    layers: dict[str, str] | None = None
    layers_file = config_dir() / "bin" / "layers.txt"
    if layers_file.is_file():
        try:
            from mino_scout.install_plan import parse_layers_txt

            layers = parse_layers_txt(layers_file.read_text(encoding="utf-8"))
        except Exception:
            layers = None

    side: dict[str, Any] = {}
    if running:
        try:
            from mino_scout.runtime_sidecar import read_runtime_sidecar

            side = read_runtime_sidecar(expect_pid=pid) or {}
        except Exception:
            side = {}

    try:
        from mino_scout.app_version import resolve_status_versions

        ver_bundle = resolve_status_versions(
            config_version=str(cfg.get("version") or ""),
            layers=layers,
            sidecar=side,
            running=running,
        )
    except Exception:
        ver_bundle = {"version": str(cfg.get("version") or "")}

    out: dict[str, Any] = {
        "running": running,
        "pid": pid if running else 0,
        "pid_file": str(pid_path()),
        "config": str(config_path()),
        "nexus": ws,
        "has_token": bool(token),
        "scout_id": resolve_scout_id(),
        **ver_bundle,
    }
    # 与旧 Studio / 脚本兼容：三字段与 version 对齐，避免 0.1.42 vs 0.1.43 混显。
    canon = str(out.get("version") or "")
    out["app_semver"] = canon
    out["reported_version"] = canon
    out["process_version"] = str(out.get("running_version") or canon)
    bin_path = config_dir() / "bin" / "mino-scout"
    if bin_path.is_file():
        out["binary"] = str(bin_path)
    if layers:
        out["installed_layers"] = layers
    if running:
        try:
            from mino_scout.power import get_guard

            out["power"] = get_guard().status()
        except Exception:
            out["power"] = {}
    return out


def schedule_reexec(*, delay_sec: float = 1.5) -> dict[str, Any]:
    """向后兼容：委托 reexec_spawn（热更后请优先 import reexec_spawn）。"""
    from importlib import import_module

    return import_module("mino_scout.reexec_spawn").schedule_reexec(delay_sec=delay_sec)


def _kickstart_service_after_reexec(*, delay_sec: float = 2.0) -> None:
    from importlib import import_module

    mod = import_module("mino_scout.reexec_spawn")
    if hasattr(mod, "_kickstart_launchd"):
        mod._kickstart_launchd(delay_sec=delay_sec)


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
