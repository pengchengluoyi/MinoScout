"""系统睡眠抑制。

## 为什么需要

区分三种状态 —— 这是最容易搞混的地方：

| 状态 | Scout / adb 还能不能工作 |
|---|---|
| **显示器熄屏**（display sleep） | **能。** 最常见的情况，其实不用管 |
| **系统睡眠**（system sleep / suspend） | **不能。** WS 断、USB 断、在途任务中断 |
| 用户注销 / 未登录 | 取决于守护形态（LaunchDaemon 能，LaunchAgent 不能） |

所以"熄屏了怎么办"的答案是：**熄屏本身不是问题，系统睡眠才是。**

## 铁律：只在有在途任务时抑制

一直抑制会让笔记本永不休眠、发烫掉电，一定被投诉。所以挂在
`ScoutCore.heartbeat().active_runs` 上：有 run 就 acquire，跑完就 release。

专机（机房常插电）通常在系统设置里就关掉了睡眠，这个模块对它是冗余的兜底；
真正需要它的是"用户日常笔记本兼作执行节点"的场景。

## 各平台手段

| 平台 | 手段 | 说明 |
|---|---|---|
| macOS | `caffeinate -i -w <我们的 pid>` | `-i` 只防系统 idle sleep，**不阻止显示器熄屏**（那是用户的事）；`-w` 让它随我们退出而退出，进程被 kill -9 也不会留下孤儿 |
| Windows | SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) | 同样不带 ES_DISPLAY_REQUIRED |
| Linux | `systemd-inhibit --what=idle:sleep` | 无 systemd 时降级为不抑制并记一条 warn |

**都刻意不阻止显示器熄屏** —— 屏幕黑掉不影响 adb，没有理由让用户的屏幕一直亮着。
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

# Windows SetThreadExecutionState 标志
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


class PowerGuard:
    """引用计数式的睡眠抑制。线程安全，可重入。

    用法：

        guard = PowerGuard()
        guard.acquire("run-123")      # 有任务了
        ...
        guard.release("run-123")      # 跑完了

    同一个 run 重复 acquire 只算一次（协议允许重发，幂等命中时不该多计数）。
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

    def release(self, holder: str) -> None:
        with self._lock:
            self._holders.discard(holder)
            if not self._holders:
                self._disengage()

    def sync(self, holders: list[str]) -> None:
        """按当前在途 run 列表对齐 —— 给心跳循环用，比手工配对 acquire/release 稳。

        进程被 kill 后重启、或某条 run 的 release 丢了，都靠这个收敛。
        """
        with self._lock:
            want = set(holders or [])
            if want == self._holders:
                return
            had = bool(self._holders)
            self._holders = want
            if want and not had:
                self._engage()
            elif not want and had:
                self._disengage()

    def status(self) -> dict:
        with self._lock:
            return {
                "active": bool(self._holders),
                "holders": sorted(self._holders),
                "platform": platform.system().lower(),
                "unavailable_reason": self._unavailable_reason,
            }

    # ---------------- 各平台实现 ----------------

    def _engage(self) -> None:
        if self._noop:
            return
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
            return
        try:
            if self._proc is not None:
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
        exe = shutil.which("caffeinate")
        if not exe:
            self._unavailable_reason = "caffeinate 不在 PATH"
            SLog.w(TAG, self._unavailable_reason)
            return
        # -i 只防系统 idle sleep，不阻止显示器熄屏
        # -w <pid> 绑定我们的生命周期：被 kill -9 也不会留下孤儿 caffeinate
        self._proc = subprocess.Popen(
            [exe, "-i", "-w", str(os.getpid())],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        SLog.i(TAG, f"已抑制系统睡眠（caffeinate -i，pid={self._proc.pid}；屏幕仍可熄）")

    def _engage_windows(self) -> None:
        rc = ctypes.windll.kernel32.SetThreadExecutionState(  # type: ignore[attr-defined]
            _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED
        )
        if rc == 0:
            self._unavailable_reason = "SetThreadExecutionState 返回 0"
            SLog.w(TAG, self._unavailable_reason)
            return
        self._win_active = True
        SLog.i(TAG, "已抑制系统睡眠（ES_SYSTEM_REQUIRED；屏幕仍可熄）")

    def _engage_linux(self) -> None:
        exe = shutil.which("systemd-inhibit")
        if not exe:
            self._unavailable_reason = "systemd-inhibit 不在 PATH"
            SLog.w(TAG, f"{self._unavailable_reason}，不抑制睡眠")
            return
        # 抱一个长睡的子进程，它活着期间抑制生效
        self._proc = subprocess.Popen(
            [exe, "--what=idle:sleep", "--who=MinoScout", "--why=running a test",
             "--mode=block", "sleep", "infinity"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        SLog.i(TAG, f"已抑制系统睡眠（systemd-inhibit，pid={self._proc.pid}）")


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
