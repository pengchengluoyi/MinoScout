"""地基冒烟测试：schemas + base + low_level 串起来能跑。

刻意不引入 pytest —— 与 scripts/verify_*.py 一样，裸 python 就能跑：

    python tests/test_foundation.py

需要 pydantic（schemas.py 用）。低层的 low_level.py 与 protocol.py 是零依赖的。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mino_scout.executors.base import (  # noqa: E402
    DeviceRef,
    ExecutorContext,
    make_event_result,
    now_iso,
)
from mino_scout.executors.low_level import (  # noqa: E402
    LowLevelError,
    assert_command_allowed,
    render_template,
    run_low_level,
)
from mino_scout.log import redact  # noqa: E402
from mino_scout.protocol import EventStatus  # noqa: E402
from mino_scout.schemas import CapturedScreen, EventResult, PlanEvent  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{' — ' + detail if detail else ''}")
        failures.append(name)


print("== 五态取值与上游一致（小写，pass 不是 OK） ==")
check("五个值", [s.value for s in EventStatus] == ["pass", "fail", "skipped", "blocked", "declined"],
      str([s.value for s in EventStatus]))
check("PASS 是 'pass'", EventStatus.PASS.value == "pass")
check("没有 OK", not hasattr(EventStatus, "OK"))

print("== schemas 能容忍 Nexus 加字段（协议 §7）==")
e = PlanEvent(
    seq=1,
    capability_id="tap_element",
    params={"x": 512, "y": 830},
    executor_order=["remote", "adb"],
    low_level={"shell": "input tap {x} {y}"},
    device_hint={"adb_serial": "R5CT30xxxx", "password": "hunter2"},
    some_future_field="Nexus 以后加的",
)
check("未知字段不炸", getattr(e, "some_future_field", None) == "Nexus 以后加的")

print("== low_level 模板渲染与安全边界 ==")
check("占位符填充", render_template("input tap {x} {y}", {"x": 512, "y": 830}) == "input tap 512 830")
check("白名单放行 input", assert_command_allowed("input tap 1 2") is None)
for bad in ("rm -rf /data", "input tap 1 2 > /sdcard/x", "pm list && reboot", "cat /x | sh"):
    try:
        assert_command_allowed(bad)
        check(f"拒绝 {bad!r}", False, "没有拒绝")
    except LowLevelError:
        check(f"拒绝 {bad!r}", True)
try:
    render_template("input text {t}", {"t": "a; rm -rf /"})
    check("参数值非法字符被拒", False, "没有拒绝")
except LowLevelError:
    check("参数值非法字符被拒", True)

print("== low_level 用假 ShellRunner 跑一条 shell ==")
calls: list[str] = []


def fake_shell(cmd: str):
    calls.append(cmd)
    return 0, "ok", ""


out = run_low_level({"kind": "shell", "shell": "input tap {x} {y}"}, {"x": 512, "y": 830}, fake_shell)
check("执行了一条命令", calls == ["input tap 512 830"], str(calls))
check("outcome.ok", bool(getattr(out, "ok", False)), repr(out))

print("== make_event_result 字段填法 ==")
ctx = ExecutorContext(
    device=DeviceRef(sn="R5CT30xxxx", adb_serial="R5CT30xxxx", password="hunter2"),
    run_id="run_20260902_153001_R5CT30",
    step_idx=7,
    screen=CapturedScreen(ok=True, source="adb", image_base64="x", width=1080, height=2340),
)
r = make_event_result(
    e, status=EventStatus.DECLINED, executor_used="remote",
    started_at=now_iso(), elapsed_ms=18, summary="requires device_owner",
    attempts=[{"executor": "remote", "status": "declined", "elapsed_ms": 18}],
)
check("是 EventResult", isinstance(r, EventResult))
check("status 透传", r.status is EventStatus.DECLINED)
check("event_kind 缺省取 capability_id", r.event_kind == "tap_element")
check("executor_used 不为空", r.executor_used == "remote")
check("attempts 带上了", len(r.attempts) == 1)
check("thumb 留空（缩略图归 Nexus）", r.thumb == "")
check("plan_event 回填", r.plan_event.get("capability_id") == "tap_element")

print("== 凭据脱敏（CONVENTIONS.md §1）==")
red = redact({"adb_serial": "R5CT30xxxx", "password": "hunter2",
              "nested": {"auth_token": "t"}})
check("password 被打码", red["password"] == "***")
check("嵌套 token 被打码", red["nested"]["auth_token"] == "***")
check("非敏感字段保留", red["adb_serial"] == "R5CT30xxxx")
check("空值不打码成 ***", redact({"password": ""})["password"] == "")

print("== ctx.screen 可用 ==")
check("has_image", ctx.screen.has_image())
check("device.is_web=False", ctx.device.is_web is False)
check("web-local 判定", DeviceRef(sn="web-local", platform="web").is_web is True)
check("web+scout_id 判定", DeviceRef(sn="web3f8a1c0e9b2d4f71").is_web is True)

print()
if failures:
    print(f"FAILED {len(failures)}: {', '.join(failures)}")
    sys.exit(1)
print("ALL OK — foundation smoke test")
