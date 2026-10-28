"""iOS WDA channel: environment self-prove only.

Other iOS actions are still unmigrated. This executor does not branch on Appium;
Appium is only an endpoint already present on device_hint.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from mino_scout.executors.base import (
    ExecutorContext,
    _now_iso,
    make_event_result,
)
from mino_scout.log import SLog
from mino_scout.schemas import EventResult, EventStatus, PlanEvent

TAG = "IosWdaExecutor"


class IosWdaExecutor:
    id = "ios_wda"
    provides = ("self_prove",)

    def probe(self) -> tuple[bool, str]:
        return True, ""

    def supports(self, capability_id: str, low_level: Optional[dict[str, Any]] = None) -> bool:
        return capability_id == "self_prove"

    def execute(self, event: PlanEvent, ctx: ExecutorContext) -> EventResult:
        started_at = _now_iso()
        t0 = time.time()
        try:
            if ctx.device.platform != "ios":
                return make_event_result(
                    event, status=EventStatus.DECLINED, executor_used=self.id,
                    started_at=started_at, elapsed_ms=0,
                    summary=f"ios_wda 不服务 platform={ctx.device.platform or '-'} sn={ctx.device.sn}",
                )
            if event.capability_id != "self_prove":
                return make_event_result(
                    event, status=EventStatus.DECLINED, executor_used=self.id,
                    started_at=started_at, elapsed_ms=0,
                    summary=f"ios_wda 不处理 {event.capability_id}",
                )
            from mino_scout.probe.ios_proof import prove_ios

            package = str((event.params or {}).get("package") or ctx.device.extra.get("target_package") or "").strip()
            proof = prove_ios(
                udid=str(ctx.device.udid or ""),
                package=package,
                node_id=str(ctx.device.extra.get("scout_node_id") or ""),
                hint=ctx.device.extra,
            )
            return make_event_result(
                event,
                status=EventStatus.PASS if proof.ok else EventStatus.FAIL,
                executor_used=self.id,
                started_at=started_at,
                elapsed_ms=int((time.time() - t0) * 1000),
                summary=proof.summary,
                error=proof.error,
                raw_response=proof.data,
            )
        except Exception as exc:  # noqa: BLE001 — executor 契约：永不抛。
            SLog.e(TAG, f"execute exception cap={event.capability_id}: {type(exc).__name__}")
            return make_event_result(
                event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
                elapsed_ms=int((time.time() - t0) * 1000),
                summary=f"exception: {type(exc).__name__}",
                error=f"exception: {type(exc).__name__}",
            )
