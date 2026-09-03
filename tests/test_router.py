"""Router 的五态 fallback 语义。

这些断言直接对标上游 `server/services/regression/router.py:150-170` —— 如果哪天
有人把 `fail` 改成"中断 fallback"，或把 `blocked` 改成"继续试"，这里就会红。

    python tests/test_router.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mino_scout.executors.base import (  # noqa: E402
    DeviceRef,
    ExecutorContext,
    EventStatus,
    make_event_result,
    now_iso,
)
from mino_scout.router import Router  # noqa: E402
from mino_scout.schemas import EventResult, PlanEvent  # noqa: E402

failures: list[str] = []
CALLS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{' — ' + detail if detail else ''}")
        failures.append(name)


class FakeExecutor:
    """按脚本返回状态的假 executor。"""

    def __init__(self, ex_id: str, status: EventStatus, *, supports_it: bool = True, raises: bool = False):
        self.id = ex_id
        self._status = status
        self._supports = supports_it
        self._raises = raises

    def supports(self, capability_id: str, low_level=None) -> bool:
        # 签名带 low_level：Protocol 要求（base.Executor.supports），
        # 因为「没有 Python 分支但 Nexus 给了 low_level」也算支持
        return self._supports

    def execute(self, event: PlanEvent, ctx: ExecutorContext) -> EventResult:
        CALLS.append(self.id)
        if self._raises:
            raise RuntimeError("boom")
        return make_event_result(
            event, status=self._status, executor_used=self.id,
            started_at=now_iso(), elapsed_ms=10,
            summary=f"{self.id}->{self._status.value}",
            error="" if self._status is EventStatus.PASS else f"{self.id} err",
        )


DEV = DeviceRef(sn="R5CT30xxxx", adb_serial="R5CT30xxxx")


def ev(order: list[str], cap: str = "tap_element") -> PlanEvent:
    return PlanEvent(seq=1, capability_id=cap, params={"x": 500, "y": 500}, executor_order=order)


def run(execs: dict, order: list[str], cap: str = "tap_element"):
    CALLS.clear()
    return Router(execs).dispatch(ev(order, cap), DEV, run_id="r1", step_idx=7)


print("== pass 立即返回，不试后面的 ==")
r = run({"remote": FakeExecutor("remote", EventStatus.PASS),
         "adb": FakeExecutor("adb", EventStatus.PASS)}, ["remote", "adb"])
check("状态 pass", r.status is EventStatus.PASS)
check("只调了 remote", CALLS == ["remote"], str(CALLS))
check("executor_used=remote", r.executor_used == "remote")
check("attempts 1 条", len(r.attempts) == 1, str(r.attempts))

print("== declined 让位，试下一个 ==")
r = run({"remote": FakeExecutor("remote", EventStatus.DECLINED),
         "adb": FakeExecutor("adb", EventStatus.PASS)}, ["remote", "adb"])
check("最终 pass", r.status is EventStatus.PASS)
check("两个都调了", CALLS == ["remote", "adb"], str(CALLS))
check("executor_used=adb", r.executor_used == "adb")
check("attempts 记了 declined", [a["status"] for a in r.attempts] == ["declined", "pass"], str(r.attempts))

print("== fail 也走 fallback（上游 router.py:165 的行为，别改成中断）==")
r = run({"remote": FakeExecutor("remote", EventStatus.FAIL),
         "adb": FakeExecutor("adb", EventStatus.PASS)}, ["remote", "adb"])
check("最终 pass", r.status is EventStatus.PASS)
check("fail 后继续试了 adb", CALLS == ["remote", "adb"], str(CALLS))

print("== skipped 也走 fallback ==")
r = run({"remote": FakeExecutor("remote", EventStatus.SKIPPED),
         "adb": FakeExecutor("adb", EventStatus.PASS)}, ["remote", "adb"])
check("最终 pass", r.status is EventStatus.PASS)
check("skipped 后继续试", CALLS == ["remote", "adb"], str(CALLS))

print("== blocked 立即返回，中断 fallback（换 executor 不能让人凭空出现）==")
r = run({"remote": FakeExecutor("remote", EventStatus.BLOCKED),
         "adb": FakeExecutor("adb", EventStatus.PASS)}, ["remote", "adb"])
check("状态 blocked", r.status is EventStatus.BLOCKED)
check("没有试 adb", CALLS == ["remote"], str(CALLS))

print("== 全失败：返回最后一次的结果，attempts 完整 ==")
r = run({"remote": FakeExecutor("remote", EventStatus.DECLINED),
         "adb": FakeExecutor("adb", EventStatus.FAIL)}, ["remote", "adb"])
check("状态 fail", r.status is EventStatus.FAIL)
check("executor_used=adb（最后一个）", r.executor_used == "adb")
check("attempts 两条", [a["status"] for a in r.attempts] == ["declined", "fail"], str(r.attempts))

print("== 严格按 Nexus 给的顺序，不自行改序 ==")
r = run({"remote": FakeExecutor("remote", EventStatus.PASS),
         "adb": FakeExecutor("adb", EventStatus.PASS)}, ["adb", "remote"])
check("先调 adb", CALLS == ["adb"], str(CALLS))

print("== supports()=False 的被跳过，不计入 attempts ==")
r = run({"playwright": FakeExecutor("playwright", EventStatus.PASS, supports_it=False),
         "adb": FakeExecutor("adb", EventStatus.PASS)}, ["playwright", "adb"])
check("跳过 playwright", CALLS == ["adb"], str(CALLS))
check("attempts 只有 adb", [a["executor"] for a in r.attempts] == ["adb"], str(r.attempts))

print("== executor 违反契约抛异常：兜住并继续 fallback ==")
r = run({"remote": FakeExecutor("remote", EventStatus.PASS, raises=True),
         "adb": FakeExecutor("adb", EventStatus.PASS)}, ["remote", "adb"])
check("最终 pass", r.status is EventStatus.PASS)
check("异常没穿透", CALLS == ["remote", "adb"], str(CALLS))
check("attempts 记了 fail", r.attempts[0]["status"] == "fail", str(r.attempts))
check("error 里有异常类型", "RuntimeError" in r.attempts[0]["error"], r.attempts[0]["error"])

print("== 无路可走时，原因要说清是哪一种 ==")
r = run({"adb": FakeExecutor("adb", EventStatus.PASS)}, [])
check("空 executor_order → fail", r.status is EventStatus.FAIL)
check("点名 executor_order 缺失", "未携带 executor_order" in r.error, r.error)
check("executor_used=router", r.executor_used == "router")

r = run({"adb": FakeExecutor("adb", EventStatus.PASS)}, ["ios_wda"])
check("本机没注册 → fail", r.status is EventStatus.FAIL)
check("点名缺哪个", "ios_wda" in r.error and "未注册" in r.error, r.error)

r = run({"adb": FakeExecutor("adb", EventStatus.PASS, supports_it=False)}, ["adb"])
check("都不 supports → fail", r.status is EventStatus.FAIL)
check("提示目录不同步", "不同步" in r.error, r.error)

print()
if failures:
    print(f"FAILED {len(failures)}: {', '.join(failures)}")
    sys.exit(1)
print("ALL OK — router 五态 fallback 语义")
