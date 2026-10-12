"""Cua desktop adapter. Does not select another driver after refusal."""
from __future__ import annotations

import time
from mino_scout.desktop import DESKTOP_CAPS, DesktopUnavailable, get_desktop_runtime
from mino_scout.executors.base import make_event_result, now_iso
from mino_scout.log import SLog
from mino_scout.schemas import EventStatus


class DesktopExecutor:
    id = "desktop"
    provides = ["ui_native_input", "ui_input_text", "ui_screenshot", "app_launch_native", "key_event"]

    def probe(self):
        state = get_desktop_runtime().state()
        return state["state"] == "ready", state["blocked_reason"]

    def supports(self, capability_id, low_level=None):
        return capability_id in DESKTOP_CAPS

    def execute(self, event, ctx):
        started, tick = now_iso(), time.monotonic()
        status, error, data = EventStatus.PASS, "", {}
        try:
            if ctx.device.platform != "desktop":
                raise DesktopUnavailable("不是 Desktop 设备")
            data = get_desktop_runtime().action(event.capability_id, dict(event.params or {}), ctx.device.extra, ctx.run_id)
            effect = (data.get("cua_action") or {}).get("effect")
            if effect == "refused":
                status, error = EventStatus.BLOCKED, "桌面驱动拒绝操作，请查看 Session Log 中的动作证据"
            elif effect in ("suspected_noop", "partial"):
                status, error = EventStatus.FAIL, f"桌面动作效果：{effect}，需要重新观察"
        except DesktopUnavailable as exc:
            status, error = EventStatus.BLOCKED, str(exc)
        except Exception as exc:
            SLog.e("DesktopExecutor", f"run={ctx.run_id} cap={event.capability_id} error={type(exc).__name__}")
            status, error = EventStatus.FAIL, "Cua 桌面执行失败，请查看节点日志并重新观察"
        return make_event_result(event, status=status, executor_used=self.id, started_at=started,
                                 elapsed_ms=int((time.monotonic() - tick) * 1000),
                                 summary=error or "桌面动作已执行；业务结果需后续回看", error=error, raw_response=data)
