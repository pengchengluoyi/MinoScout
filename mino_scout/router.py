"""执行 Nexus 明确选择的唯一执行器；任何结果均返回，不降级换路。"""
from __future__ import annotations

import time
from typing import Any, Optional

from mino_scout.executors.base import (
    DeviceRef,
    Executor,
    ExecutorContext,
    EventStatus,
    make_event_result,
    now_iso,
)
from mino_scout.tools.keep_alive.log import SLog
from mino_scout.runtime.execution_boundary import normalize_ui_result, ui_write
from mino_scout.schemas import CapturedScreen, EventResult, PlanEvent

TAG = "Router"




class Router:
    """Scout 侧的动作分发。

    与上游 `CapabilityRouter` 的关键区别：**不做选路决策**。
    `executor_order` 由 Nexus 算好塞进 `EXECUTE` 载荷（CLAUDE.md §1 约束 3），
    Router 只执行唯一指定项，不自行改序、不自行追加或降级。
    """

    def __init__(self, executors: dict[str, Executor]):
        self.executors = executors

    # ---------- 对外 ----------

    def dispatch(
        self,
        event: PlanEvent,
        device: DeviceRef,
        *,
        run_id: str = "",
        step_idx: int = -1,
        screen: Optional[CapturedScreen] = None,
        capture_prefer: tuple[str, ...] = (),
        shared: Optional[dict[str, Any]] = None,
    ) -> EventResult:
        started = now_iso()
        t0 = time.time()

        if len(event.executor_order or []) != 1:
            return self._synthetic_fail(event, "executor_order_must_select_one", started_at=started)
        if (event.selected_impl or {}).get("executor") != event.executor_order[0]:
            return self._synthetic_fail(event, "selected_impl_executor_mismatch", started_at=started)
        ordered = self._resolve_order(event)
        compatible = device.compatible_executors
        skipped = [ex for ex in ordered if ex not in compatible]
        if skipped:
            SLog.w(
                TAG,
                f"忽略与 sn={device.sn} platform={device.platform} 不符的 executor: {skipped}",
            )
        ordered = [ex for ex in ordered if ex in compatible]
        if not ordered:
            reason = (
                f"router: executor {skipped} 不服务 sn={device.sn} "
                f"（platform={device.platform}，该设备只接受 {sorted(compatible)}）"
                if skipped
                else self._no_route_reason(event)
            )
            return self._synthetic_fail(event, reason, started_at=started)

        ctx = ExecutorContext(
            device=device,
            run_id=run_id,
            step_idx=step_idx,
            screen=screen,
            capture_prefer=capture_prefer,
            shared=shared if shared is not None else {},
            selected_impl=event.selected_impl or None,
        )

        attempts: list[dict[str, Any]] = []
        last: Optional[EventResult] = None

        for ex_id in ordered:
            stop_check = ctx.shared.get("write_stopped")
            if ui_write(event.capability_id) and callable(stop_check) and stop_check():
                result = make_event_result(event, status=EventStatus.BLOCKED, executor_used="router",
                    started_at=started, elapsed_ms=int((time.time() - t0) * 1000),
                    summary="运行已停止后续写动作，未调用后备执行器",
                    raw_response={"execution": {"execution_started": "no", "phase": "admission", "reason_code": "run_write_stopped"},
                                  "execution_control": {"stop_run": True, "reason_code": "run_write_stopped", "resource_isolated": True}})
                result.attempts = attempts
                return result
            executor = self.executors[ex_id]  # _resolve_order 已保证存在且 supports
            a_t0 = time.time()
            try:
                result = executor.execute(event, ctx)
            except Exception as exc:
                # executor 契约是「永不抛」。真抛了说明它有 bug —— 兜住，
                # 缺少写边界证明时停止 fallback，保留未知结果。
                SLog.e(TAG, f"executor {ex_id} 违反契约抛了异常 cap={event.capability_id}: {exc!r}")
                result = make_event_result(
                    event,
                    status=EventStatus.FAIL,
                    executor_used=ex_id,
                    started_at=started,
                    elapsed_ms=int((time.time() - a_t0) * 1000),
                    summary=f"executor {ex_id} raised (契约违规)",
                    error=f"{type(exc).__name__}: {exc}",
                )

            attempts.append(
                {
                    "executor": ex_id,
                    "status": _status_value(result.status),
                    "elapsed_ms": result.elapsed_ms or int((time.time() - a_t0) * 1000),
                    "error": (result.error or "")[:200],
                }
            )
            last = result

            result = normalize_ui_result(result, capability_id=event.capability_id,
                run_id=run_id, step_idx=step_idx)
            result.attempts = attempts
            result.elapsed_ms = result.elapsed_ms or int((time.time() - t0) * 1000)
            return result

        # 全试完都没成
        if last is None:  # pragma: no cover —— ordered 非空时不可能到这
            return self._synthetic_fail(event, "router: 没有任何 executor 被实际调用", started_at=started)
        last.attempts = attempts
        last.elapsed_ms = last.elapsed_ms or int((time.time() - t0) * 1000)
        return last

    # ---------- 内部 ----------

    def _resolve_order(self, event: PlanEvent) -> list[str]:
        """按 Nexus 给的顺序过滤出「本机确实注册了、且 supports 这条 cap」的 executor。

        **不排序、不补充。** 顺序是 Nexus 的决定（它才有能力目录和连通性）。
        """
        out: list[str] = []
        for ex_id in event.executor_order or []:
            if ex_id in out:
                continue
            executor = self.executors.get(ex_id)
            if executor is None:
                SLog.w(TAG, f"Nexus 指定了本机没有的 executor: {ex_id}")
                continue
            try:
                # low_level 一起传进去：没有 Python 分支但 Nexus 给了可执行的
                # low_level 声明时，executor 也应答"支持"（见 base.Executor.supports）
                if not executor.supports(event.capability_id, event.low_level or None):
                    continue
            except Exception as exc:  # supports() 也不该抛
                SLog.e(TAG, f"{ex_id}.supports() 抛异常: {exc!r}")
                continue
            out.append(ex_id)
        return out

    def _no_route_reason(self, event: PlanEvent) -> str:
        """把「无路可走」的原因说清楚 —— 这是排查 Nexus/Scout 不同步的第一现场。"""
        want = list(event.executor_order or [])
        if not want:
            return (
                f"router: EXECUTE 未携带 executor_order（cap={event.capability_id}）。"
                "Nexus 必须算好选路再下发，Scout 不自行推断"
            )
        have = sorted(self.executors)
        missing = [e for e in want if e not in self.executors]
        if missing:
            return (
                f"router: Nexus 指定的 executor {missing} 本机未注册"
                f"（cap={event.capability_id}，本机有 {have}）"
            )
        return (
            f"router: executor {want} 都不 supports cap={event.capability_id}"
            "（Nexus 的能力目录与 Scout 实现不同步？）"
        )

    def _synthetic_fail(self, event: PlanEvent, msg: str, *, started_at: str = "") -> EventResult:
        SLog.w(TAG, msg)
        return make_event_result(
            event,
            status=EventStatus.FAIL,
            executor_used="router",
            started_at=started_at or now_iso(),
            elapsed_ms=0,
            summary=msg,
            error=msg,
            raw_response={"execution":{"execution_started":"no","phase":"admission","reason_code":"executor_route_invalid"}},
        )


def _status_value(status: Any) -> str:
    return status.value if isinstance(status, EventStatus) else str(status)
