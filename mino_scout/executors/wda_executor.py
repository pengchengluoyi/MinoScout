"""iOS 通道：直接请求本机 WDA 会话。清应用数据没有对应接口，不改写 App 沙盒。"""
from __future__ import annotations

import time
from typing import Any

from mino_scout.executors.base import make_event_result, now_iso
from mino_scout.tools.keep_alive.log import SLog
from mino_scout.schemas import EventStatus
from mino_scout.platform.ios.wda_session import (
    WdaError, app_version, client_for, install_app, uninstall_app,
)

TAG = "WdaExecutor"
_CAPS = frozenset({
    "launch_app", "close_app", "kill_app", "tap_element", "tap_xy", "exec_script",
    "input_text", "press_key", "install", "uninstall_app", "screenshot", "hierarchy",
    "swipe_direction", "swipe_element_to_element", "long_press_element",
    "get_foreground_app", "set_clipboard", "get_app_version",
})


class WdaExecutor:
    id = "ios_wda"
    provides = sorted(_CAPS)

    def probe(self):
        from mino_scout.platform.ios.wda_session import WdaClient, list_attached_devices

        if not list_attached_devices() and not WdaClient("").reachable():
            return False, "没有连接的 iOS 设备，或本机 WDA 未监听"
        if not WdaClient("").reachable():
            return False, "iOS 设备在线，但 WDA 未监听"
        return True, ""

    def supports(self, capability_id: str, low_level: dict[str, Any] | None = None) -> bool:
        del low_level
        cap = str(capability_id or "")
        # 不对外声明清数据，但收到时要明确拒绝，避免无路由失败被记成写结果未知。
        return cap in _CAPS or cap in {"clear_app_cache", "install_app"}

    def execute(self, event, ctx):
        started, tick = now_iso(), time.monotonic()
        if str(event.capability_id or "") == "clear_app_cache":
            return make_event_result(
                event, status=EventStatus.DECLINED, executor_used=self.id,
                started_at=started, elapsed_ms=0,
                summary="iOS 没有清应用数据的通道",
                error="not_supported",
                raw_response={"execution": {"execution_started": "no", "phase": "route", "reason_code": "not_supported"}},
            )
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
        if cap == "launch_app":
            if not bundle:
                raise WdaError("启动应用缺少 bundle id")
            client.activate(bundle)
            return {"summary": f"已启动 {bundle}", "bundle_id": bundle}
        if cap in {"close_app", "kill_app"}:
            if not bundle:
                raise WdaError("关闭应用缺少 bundle id")
            client.terminate(bundle)
            return {"summary": f"已关闭 {bundle}", "bundle_id": bundle}
        if cap == "exec_script":
            from mino_scout.platform.ios.script import run_command

            result = run_command(str(params.get("script") or ""), udid=udid, timeout_ms=_timeout_ms(params))
            if result["status"] != "success":
                raise WdaError(result.get("stderr") or result.get("message") or "脚本失败")
            return {"summary": result.get("message") or "已执行脚本", "stdout": result.get("stdout") or ""}
        if cap == "tap_xy":
            client.tap_xy(int(params.get("x") or 0), int(params.get("y") or 0))
            return {"summary": "已点击坐标"}
        if cap == "tap_element":
            policy = str(params.get("point_policy") or "").strip().lower()
            target = _target_text(params)
            has_xy = params.get("x") is not None and params.get("y") is not None
            if target and policy != "coordinate":
                client.tap_target(target)
                return {"summary": "已点击", "tap_strategy": "anchor", "target": target}
            if has_xy:
                client.tap_xy(int(params["x"]), int(params["y"]))
                return {"summary": "已点击", "tap_strategy": "xy", "x": int(params["x"]), "y": int(params["y"])}
            raise WdaError("tap_element 缺少 target 或坐标")
        if cap == "long_press_element":
            x, y = _point(client, params)
            duration = max(0.2, int(params.get("duration_ms") or 600) / 1000)
            client.touch_and_hold(x, y, duration)
            return {"summary": "已长按"}
        if cap == "swipe_direction":
            x1, y1, x2, y2 = _swipe_points(client, params)
            client.drag(x1, y1, x2, y2, max(0.05, int(params.get("duration_ms") or 300) / 1000))
            return {"summary": "已滑动"}
        if cap == "swipe_element_to_element":
            if None in (params.get("from_x"), params.get("from_y"), params.get("to_x"), params.get("to_y")):
                raise WdaError("拖拽需要 from_x/from_y/to_x/to_y")
            client.drag(int(params["from_x"]), int(params["from_y"]), int(params["to_x"]), int(params["to_y"]),
                        max(0.05, int(params.get("duration_ms") or 400) / 1000))
            return {"summary": "已拖拽"}
        if cap == "input_text":
            client.keys(str(params.get("text") or ""))
            return {"summary": "已输入"}
        if cap == "press_key":
            button = {"home": "home", "volume_up": "volumeUp", "volumeup": "volumeUp",
                      "volume_down": "volumeDown", "volumedown": "volumeDown"}.get(str(params.get("key") or "").strip().lower())
            if not button:
                raise WdaError("iOS 只支持 home、volume_up、volume_down")
            if button == "home":
                client.press_button("home")
                return {"summary": "已回主屏"}
            client.press_button(button)
            return {"summary": f"已按 {button}"}
        if cap == "get_foreground_app":
            info = client.active_app()
            bundle_id = str(info.get("bundleId") or "")
            expect = bundle or str(params.get("package") or "")
            return {"summary": bundle_id, "bundle_id": bundle_id, "name": info.get("name") or "",
                    "match": (bundle_id == expect) if expect else None}
        if cap == "set_clipboard":
            text = str(params.get("text") or "")
            if not text:
                raise WdaError("set_clipboard 缺 text")
            client.set_clipboard(text)
            return {"summary": f"剪贴板写入 {len(text)} 字"}
        if cap == "get_app_version":
            info = app_version(udid, bundle)
            return {"summary": info["version"], **info}
        if cap in {"install", "install_app"}:
            install_app(udid, str(params.get("path") or ""))
            return {"summary": "已安装"}
        if cap == "uninstall_app":
            uninstall_app(udid, bundle or str(params.get("bundle_id") or ""))
            return {"summary": "已卸载"}
        raise WdaError(f"iOS 通道未实现 {cap}")


def _timeout_ms(params: dict) -> int:
    try:
        value = int(params.get("timeout_ms") or 30000)
    except (TypeError, ValueError):
        value = 30000
    return max(1000, min(value, 60000))


def _target_text(params: dict) -> str:
    raw_target = params.get("target")
    if isinstance(raw_target, dict):
        return str(raw_target.get("name") or raw_target.get("text") or raw_target.get("label") or raw_target.get("content_desc") or "").strip()
    return str(raw_target or params.get("name") or "").strip()


def _point(client, params: dict) -> tuple[int, int]:
    target = _target_text(params)
    policy = str(params.get("point_policy") or "").strip().lower()
    if target and policy != "coordinate":
        return client.element_center(target)
    if params.get("x") is None or params.get("y") is None:
        raise WdaError("缺少目标或坐标")
    return int(params["x"]), int(params["y"])


def _swipe_points(client, params: dict) -> tuple[int, int, int, int]:
    if None not in (params.get("from_x"), params.get("from_y"), params.get("to_x"), params.get("to_y")):
        return int(params["from_x"]), int(params["from_y"]), int(params["to_x"]), int(params["to_y"])
    width, height = client.window_size()
    direction = str(params.get("direction") or "up").lower()
    cx, cy = width // 2, height // 2
    steps = {
        "up": (cx, int(height * 0.75), cx, int(height * 0.25)),
        "down": (cx, int(height * 0.25), cx, int(height * 0.75)),
        "left": (int(width * 0.75), cy, int(width * 0.25), cy),
        "right": (int(width * 0.25), cy, int(width * 0.75), cy),
    }
    if direction not in steps:
        raise WdaError(f"不支持的滑动方向 {direction}")
    return steps[direction]
