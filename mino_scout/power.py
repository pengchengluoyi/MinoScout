"""系统睡眠抑制 —— Scout 是持续在线节点，机器必须醒着。

挂在 `NodeTransport.run_forever` 上：开始接 Nexus 就 acquire，进程退出才
release。重连窗口也压住。不绑 `active_runs`。

| 平台 | 手段 | 说明 |
|---|---|---|
| macOS | `caffeinate -dims -w <pid>` | `-d` 屏幕不熄（否则不少 Mac USB 掉电）；`-i` 系统 idle；`-m` 磁盘 idle；`-s` 合盖（**仅插电有效**，Apple 限制）；电池合盖仍会睡 |
| Windows | ES_CONTINUOUS \\| SYSTEM_REQUIRED \\| DISPLAY_REQUIRED | 心跳在长寿线程上重申 |
| Linux | `idle:sleep:handle-lid-switch` | 无 systemd 时记 warn，不抑制 |

`sync()` / `keepalive()` 发现子进程死了会重新拉起。
"""
from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import threading
from typing import Optional

from mino_scout.log import SLog

TAG = "PowerGuard"

# transport 生命周期持有。名称稳定，测试和 status 都认它。
HOLDER_NEXUS = "nexus"

# Windows SetThreadExecutionState 标志
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002


def macos_caffeinate_args(pid: int) -> list[str]:
    """持续在线：屏幕、系统、磁盘、合盖（插电）一起压。"""
    return ["-d", "-i", "-m", "-s", "-w", str(int(pid))]


def macos_on_ac_power() -> bool:
    """合盖断言只在 AC 上生效。查不到时按插电处理，仍然传 `-s`。"""
    try:
        out = subprocess.run(
            ["pmset", "-g", "batt"],
            capture_output=True, text=True, timeout=2,
        )
        blob = (out.stdout or "") + (out.stderr or "")
        if "Battery Power" in blob:
            return False
        if "AC Power" in blob:
            return True
    except Exception:
        pass
    return True


class PowerGuard:
    """引用计数式的睡眠抑制。线程安全，可重入。

    用法：

        guard = PowerGuard()
        guard.acquire(HOLDER_NEXUS)   # 开始接 Nexus
        ...
        guard.release(HOLDER_NEXUS)   # 进程退出
    """

    def __init__(self, *, noop: bool = False) -> None:
        self._lock = threading.RLock()
        self._holders: set[str] = set()
        self._proc: Optional[subprocess.Popen] = None
        self._win_active = False
        self._unavailable_reason = ""
        self._noop = noop

    # ---------------- 对外 ----------------

    @property
    def active(self) -> bool:
        with self._lock:
            return bool(self._holders)

    @property
    def holders(self) -> list[str]:
        with self._lock:
            return sorted(self._holders)

    def acquire(self, holder: str) -> None:
        with self._lock:
            first = not self._holders
            self._holders.add(holder)
            if first:
                self._engage()
            else:
                self._ensure_alive()

    def release(self, holder: str) -> None:
        with self._lock:
            self._holders.discard(holder)
            if not self._holders:
                self._disengage()

    def sync(self, holders: list[str]) -> None:
        """对齐 holder 集合；有 holder 时若子进程已死则重新拉起。

        holder 集合没变也要做存活检查 —— 旧实现在这里直接 return，
        caffeinate 被杀之后会静默失去抑制。
        """
        with self._lock:
            want = set(holders or [])
            had = bool(self._holders)
            self._holders = want
            if want:
                if not self._inhibition_alive():
                    self._clear_dead_child()
                    self._engage()
            elif had or self._proc is not None or self._win_active:
                self._disengage()

    def keepalive(self) -> None:
        """心跳 / 重连循环调用：有 holder 就保证抑制还在。"""
        with self._lock:
            if not self._holders:
                return
            self._ensure_alive()

    def status(self) -> dict:
        with self._lock:
            return {
                "active": bool(self._holders),
                "holders": sorted(self._holders),
                "platform": platform.system().lower(),
                "unavailable_reason": self._unavailable_reason,
                "child_alive": self._inhibition_alive(),
            }

    # ---------------- 存活 ----------------

    def _inhibition_alive(self) -> bool:
        if self._noop:
            return bool(self._holders)
        if self._proc is not None:
            return self._proc.poll() is None
        return self._win_active

    def _clear_dead_child(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.poll() is None:
                return
        except Exception:
            pass
        self._proc = None

    def _ensure_alive(self) -> None:
        if self._noop:
            return
        system = platform.system()
        if system == "Windows":
            # 断言跟线程走。心跳在 asyncio 长寿线程上重申，避免 worker 退出带走。
            try:
                self._engage_windows(refresh=True)
            except Exception as exc:
                self._unavailable_reason = f"{type(exc).__name__}: {exc}"
                SLog.w(TAG, f"刷新睡眠抑制失败: {self._unavailable_reason}")
            return
        if self._inhibition_alive():
            return
        SLog.w(TAG, "睡眠抑制子进程已退出，重新拉起")
        self._clear_dead_child()
        self._engage()

    # ---------------- 各平台实现 ----------------

    def _engage(self) -> None:
        if self._noop:
            return
        self._clear_dead_child()
        system = platform.system()
        try:
            if system == "Darwin":
                self._engage_macos()
            elif system == "Windows":
                self._engage_windows()
            else:
                self._engage_linux()
        except Exception as exc:  # 抑制失败不该影响执行
            self._unavailable_reason = f"{type(exc).__name__}: {exc}"
            SLog.w(TAG, f"睡眠抑制失败（任务照常跑，但机器可能睡）: {self._unavailable_reason}")

    def _disengage(self) -> None:
        if self._noop:
            self._proc = None
            self._win_active = False
            return
        try:
            if self._proc is not None:
                if self._proc.poll() is None:
                    self._proc.terminate()
                    try:
                        self._proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self._proc.kill()
                self._proc = None
                SLog.i(TAG, "已释放睡眠抑制")
            if self._win_active:
                ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)  # type: ignore[attr-defined]
                self._win_active = False
                SLog.i(TAG, "已释放睡眠抑制")
        except Exception as exc:  # pragma: no cover
            SLog.w(TAG, f"释放睡眠抑制失败: {exc}")

    def _engage_macos(self) -> None:
        exe = shutil.which("caffeinate") or "/usr/bin/caffeinate"
        if not os.path.isfile(exe):
            self._unavailable_reason = "caffeinate 不在 PATH"
            SLog.w(TAG, self._unavailable_reason)
            return
        if not macos_on_ac_power():
            SLog.w(TAG, "当前用电池：合盖仍会睡（caffeinate -s 只在插电时有效），USB/屏幕抑制照常")
        self._proc = subprocess.Popen(
            [exe, *macos_caffeinate_args(os.getpid())],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._unavailable_reason = ""
        SLog.i(TAG, f"已抑制睡眠（caffeinate -dims，pid={self._proc.pid}；屏幕不熄、插电合盖不睡）")

    def _engage_windows(self, *, refresh: bool = False) -> None:
        rc = ctypes.windll.kernel32.SetThreadExecutionState(  # type: ignore[attr-defined]
            _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED
        )
        if rc == 0:
            self._unavailable_reason = "SetThreadExecutionState 返回 0"
            SLog.w(TAG, self._unavailable_reason)
            return
        self._win_active = True
        self._unavailable_reason = ""
        if not refresh:
            SLog.i(TAG, "已抑制睡眠（ES_SYSTEM_REQUIRED|ES_DISPLAY_REQUIRED）")

    def _engage_linux(self) -> None:
        exe = shutil.which("systemd-inhibit")
        if not exe:
            self._unavailable_reason = "systemd-inhibit 不在 PATH"
            SLog.w(TAG, f"{self._unavailable_reason}，不抑制睡眠")
            return
        self._proc = subprocess.Popen(
            [exe, "--what=idle:sleep:handle-lid-switch", "--who=MinoScout",
             "--why=keeping node online", "--mode=block", "sleep", "infinity"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._unavailable_reason = ""
        SLog.i(TAG, f"已抑制睡眠（systemd-inhibit idle:sleep:handle-lid-switch，pid={self._proc.pid}）")


_guard: Optional[PowerGuard] = None
_guard_lock = threading.Lock()


def get_guard() -> PowerGuard:
    global _guard
    with _guard_lock:
        if _guard is None:
            _guard = PowerGuard()
    return _guard


def reset_guard(*, noop: bool = False) -> PowerGuard:
    """测试用。把单例换成干净的（默认不真的 caffeinate）。"""
    global _guard
    with _guard_lock:
        if _guard is not None:
            try:
                _guard.sync([])
            except Exception:
                pass
        _guard = PowerGuard(noop=noop)
    return _guard
