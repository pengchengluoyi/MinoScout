"""adb 通道端到端：Router → AdbExecutor → (假的) adb 子进程。

不需要真设备 —— 把 subprocess.run 换成脚本化的假 adb，验证：
  · 命令拼得对不对
  · 五态映射对不对
  · low_level-only 能力（没有 Python 分支）能不能跑通  ← 「加 YAML 就多一个能力」

    python tests/test_adb_channel.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mino_scout.executors.adb_executor import AdbExecutor  # noqa: E402
from mino_scout.executors.base import DeviceRef, EventStatus  # noqa: E402
from mino_scout.router import Router  # noqa: E402
from mino_scout.schemas import PlanEvent  # noqa: E402

failures: list[str] = []
CMDS: list[list[str]] = []
SCRIPT: dict[str, tuple[int, str, str]] = {}


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{' — ' + detail if detail else ''}")
        failures.append(name)


class _Proc:
    def __init__(self, rc: int, out: str, err: str):
        self.returncode, self.stdout, self.stderr = rc, out, err


def fake_run(cmd, **kwargs):
    """假 adb。按命令里出现的关键词查 SCRIPT，默认成功。"""
    CMDS.append(list(cmd))
    joined = " ".join(str(c) for c in cmd)
    for key, (rc, out, err) in SCRIPT.items():
        if key in joined:
            return _Proc(rc, out, err)
    return _Proc(0, "", "")


subprocess.run = fake_run  # type: ignore[assignment]

DEV = DeviceRef(sn="R5CT30xxxx", adb_serial="R5CT30xxxx")
ROUTER = Router({"adb": AdbExecutor()})


def dispatch(cap: str, params: dict, low_level: dict | None = None):
    CMDS.clear()
    ev = PlanEvent(
        seq=1, capability_id=cap, params=params,
        executor_order=["adb"], low_level=low_level or {},
    )
    return ROUTER.dispatch(ev, DEV, run_id="run_test", step_idx=1)


def last_cmd() -> str:
    return " ".join(CMDS[-1]) if CMDS else ""


print("== tap_element：坐标直接给，不该去 dump 层级 ==")
SCRIPT.clear()
r = dispatch("tap_element", {"x": 512, "y": 830})
check("pass", r.status is EventStatus.PASS, f"{r.status} {r.error}")
check("命令是 input tap 512 830", "input tap 512 830" in last_cmd(), last_cmd())
check("带了 -s serial", "-s R5CT30xxxx" in last_cmd(), last_cmd())
check("executor_used=adb", r.executor_used == "adb")

print("== tap_element 无坐标 → fail，且原因说清 ==")
r = dispatch("tap_element", {})
check("fail", r.status is EventStatus.FAIL)
check("原因提到无可用坐标", "无可用坐标" in r.summary, r.summary)

print("== press_key：keycode 映射 ==")
r = dispatch("press_key", {"key": "BACK"})
check("pass", r.status is EventStatus.PASS)
check("BACK → keyevent 4", "input keyevent 4" in last_cmd(), last_cmd())
r = dispatch("press_key", {"key": "HOME"})
check("HOME → keyevent 3", "input keyevent 3" in last_cmd(), last_cmd())

print("== launch_app：要 monkey 且看 'Events injected' ==")
SCRIPT.clear()
SCRIPT["monkey"] = (0, "Events injected: 1", "")
r = dispatch("launch_app", {"package": "com.example.app"})
check("pass", r.status is EventStatus.PASS, r.error)
check("用了 monkey -p", "monkey -p com.example.app" in last_cmd(), last_cmd())
SCRIPT["monkey"] = (0, "no events", "")
r = dispatch("launch_app", {"package": "com.example.app"})
check("没有 Events injected → fail", r.status is EventStatus.FAIL, str(r.status))

print("== close_app ==")
SCRIPT.clear()
r = dispatch("close_app", {"package": "com.example.app"})
check("pass", r.status is EventStatus.PASS)
check("am force-stop", "am force-stop com.example.app" in last_cmd(), last_cmd())

print("== swipe_direction：先读 wm size 再算坐标 ==")
SCRIPT.clear()
SCRIPT["wm size"] = (0, "Physical size: 1080x2340", "")
r = dispatch("swipe_direction", {"direction": "up"})
check("pass", r.status is EventStatus.PASS, r.error)
check("按 1080x2340 算的落点", "input swipe 540 1755 540 585" in last_cmd(), last_cmd())

print("== adb 不在 PATH：要 fail，不是崩 ==")
SCRIPT.clear()


def missing_adb(cmd, **kwargs):
    CMDS.append(list(cmd))
    raise FileNotFoundError("adb")


_saved = subprocess.run
subprocess.run = missing_adb  # type: ignore[assignment]
r = dispatch("press_key", {"key": "BACK"})
subprocess.run = _saved  # type: ignore[assignment]
check("fail 而非异常穿透", r.status is EventStatus.FAIL, str(r.status))
check("原因提到 PATH", "PATH" in (r.error or ""), r.error)

print("== exec_script 的 not_supported → skipped（不是 fail）==")
# 字段名是 language（不是 type）—— 与 ClawNode 的 EXEC_SCRIPT 协议对齐。
# js 走 claw.* API，adb 没有对应原语，所以是 not_supported → skipped，让位给 remote。
SCRIPT.clear()
r = dispatch("exec_script", {"language": "js", "script": "claw.tap(1,2)"})
check("js 在 adb 上是 skipped", r.status is EventStatus.SKIPPED, f"{r.status} {r.summary}")
check("原因指向 remote", "remote" in (r.summary or ""), r.summary)
r = dispatch("exec_script", {"language": "shell", "script": "getprop ro.product.model"})
check("shell 脚本能跑", r.status is EventStatus.PASS, f"{r.status} {r.error}")

print("== low_level-only 能力：没有 Python 分支也能跑（加 YAML 就多一个能力）==")
SCRIPT.clear()
r = dispatch(
    "dismiss_keyguard",                                  # 不在 _SUPPORTED_CAPS 里
    {},
    low_level={"kind": "shell", "shell": "wm dismiss-keyguard"},
)
check("pass", r.status is EventStatus.PASS, f"{r.status} {r.error}")
check("跑了声明的命令", "wm dismiss-keyguard" in last_cmd(), last_cmd())
check("executor_used=adb", r.executor_used == "adb")

print("== 既无 Python 分支也无 low_level → Router 判无路可走 ==")
r = dispatch("some_unknown_cap", {})
check("fail", r.status is EventStatus.FAIL)
check("是 Router 拦下的", r.executor_used == "router", r.executor_used)
check("提示目录不同步", "不同步" in (r.error or ""), r.error)

print("== low_level 的安全边界在 executor 里同样生效 ==")
SCRIPT.clear()
r = dispatch("danger_cap", {}, low_level={"kind": "shell", "shell": "rm -rf /data"})
check("被拒", r.status is EventStatus.FAIL, str(r.status))
check("没真的执行", not any("rm" in " ".join(c) for c in CMDS), str(CMDS))

print()
if failures:
    print(f"FAILED {len(failures)}: {', '.join(failures)}")
    sys.exit(1)
print("ALL OK — adb 通道端到端")
