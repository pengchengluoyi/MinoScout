"""iOS 通道：直接请求本机 WDA 会话。清应用数据没有对应接口，不改写 App 沙盒。"""
from __future__ import annotations

import time
from typing import Any

from mino_scout.executors.base import make_event_result, now_iso
from mino_scout.log import SLog
from mino_scout.schemas import EventStatus
from mino_scout.wda_session import WdaError, client_for, install_app, uninstall_app

TAG = "WdaExecutor"
_CAPS = frozenset({
    "launch_app", "open_app", "close_app", "kill_app", "tap_element", "tap_xy",
    "input_text", "press_key", "install_app", "uninstall_app", "clear_app_cache",
})


class WdaExecutor:
    id = "ios_wda"
    provides = sorted(_CAPS)

    def probe(self):
        from mino_scout.wda_session import WdaClient, list_attached_devices

        if not list_attached_devices() and not WdaClient("").reachable():
            return False, "没有连接的 iOS 设备，或本机 WDA 未监听"
        if not WdaClient("").reachable():
            return False, "iOS 设备在线，但 WDA 未监听"
        return True, ""

    def supports(self, capability_id: str, low_level: dict[str, Any] | None = None) -> bool:
        del low_level
        return str(capability_id or "") in _CAPS

    def execute(self, event, ctx):
        started, tick = now_iso(), time.monotonic()
        status, error, data = EventStatus.PASS, "", {}
        try:
            if str(ctx.device.platform or "") != "ios":
                return make_event_result(
                    event, status=EventStatus.DECLINED, executor_used=self.id,
                    started_at=started, elapsed_ms=0, summary="不是 iOS 设备", error="platform_mismatch",
                )
            data = self._run(event, ctx)
        except WdaError as exc:
            status, error = EventStatus.FAIL, str(exc)
        except Exception as exc:
            SLog.e(TAG, f"run={ctx.run_id} cap={event.capability_id} error={type(exc).__name__}")
            status, error = EventStatus.FAIL, f"{type(exc).__name__}: {exc}"
        summary = str(data.get("summary") or "") or ("完成" if status == EventStatus.PASS else error)
        return make_event_result(
            event, status=status, executor_used=self.id, started_at=started,
            elapsed_ms=int((time.monotonic() - tick) * 1000), summary=summary[:240],
            error=error[:240], raw_response=data,
        )

    def _run(self, event, ctx) -> dict[str, Any]:
        cap = str(event.capability_id or "")
        params = dict(event.params or {})
        udid = str(ctx.device.udid or ctx.device.sn or "")
        client = client_for(udid)
        bundle = str(params.get("bundle") or params.get("bundle_id") or params.get("package")
                     or (ctx.device.extra or {}).get("bundle_id") or (ctx.device.extra or {}).get("target_package") or "")
        if cap in {"launch_app", "open_app"}:
            if not bundle:
                raise WdaError("启动应用缺少 bundle id")
            client.activate(bundle)
            return {"summary": f"已启动 {bundle}", "bundle_id": bundle}
        if cap in {"close_app", "kill_app"}:
            if not bundle:
                raise WdaError("关闭应用缺少 bundle id")
            client.terminate(bundle)
            return {"summary": f"已关闭 {bundle}", "bundle_id": bundle}
        if cap == "tap_xy":
            client.tap_xy(int(params.get("x") or 0), int(params.get("y") or 0))
            return {"summary": "已点击坐标"}
        if cap == "tap_element":
            if params.get("x") is not None and params.get("y") is not None:
                client.tap_xy(int(params["x"]), int(params["y"]))
            else:
                client.tap_target(str(params.get("target") or params.get("name") or ""))
            return {"summary": "已点击"}
        if cap == "input_text":
            client.keys(str(params.get("text") or ""))
            return {"summary": "已输入"}
        if cap == "press_key" and str(params.get("key") or "") == "home":
            client._json("POST", client._path("/wda/homescreen"), {})
            return {"summary": "已回主屏"}
        if cap == "install_app":
            install_app(udid, str(params.get("path") or ""))
            return {"summary": "已安装"}
        if cap == "uninstall_app":
            uninstall_app(udid, bundle or str(params.get("bundle_id") or ""))
            return {"summary": "已卸载"}
        if cap == "clear_app_cache":
            raise WdaError("iOS 没有清应用数据的通道；系统权限只能卸载后重装")
        raise WdaError(f"iOS 通道未实现 {cap}")
