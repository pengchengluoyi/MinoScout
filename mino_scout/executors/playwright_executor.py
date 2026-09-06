# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Web 执行通道：Playwright，和 AdbExecutor 平级。禁止 page.evaluate 改界面。"""
from __future__ import annotations

import re
import time

from mino_scout.log import SLog

from mino_scout.schemas import EventResult, EventStatus, PlanEvent
from mino_scout.executors.base import (
    ExecutorContext,
    _now_iso,
    make_event_result,
)
from mino_scout.playwright_hub import get_hub, headed_from_hint, pick_goto_url

TAG = "PlaywrightExecutor"

_SUPPORTED_CAPS: set[str] = {
    "launch_app",
    "close_app",
    "press_key",
    "wait_ms",
    "tap_element",
    "multi_tap",
    "long_press_element",
    "input_text",
    "swipe_direction",
    "swipe_element_to_element",
}


def _name_from_params(params: dict) -> str:
    target = params.get("target") if isinstance(params.get("target"), dict) else {}
    for key in ("selector_text", "description", "label", "text"):
        val = params.get(key) or target.get(key if key != "selector_text" else "text")
        s = str(val or "").strip().strip("「」\"'")
        if s:
            return s
    return str(target.get("text") or "").strip()


class PlaywrightExecutor:
    id = "playwright"

    # 与 MiniOrangeServer 的 plugins/executors/playwright.yaml 保持一致。
    # Web 端按名字点/填，不先 VLM locate —— 所以 cost 比 VLM 路径低。
    provides = ("ui_native_input", "ui_input_text", "ui_screenshot")

    def probe(self) -> tuple[bool, str]:
        from mino_scout.playwright_hub import PROBE_OK_STATE, probe_playwright

        # 注意：probe_playwright() 成功时返回 "available"（= 包和 Chromium 都在，
        # 但没有长期占着浏览器），**不是 "connected"**。写成 connected 会让
        # playwright 永远被上报为不可用 —— 这个坑踩过一次。
        state, detail = probe_playwright()
        if state == PROBE_OK_STATE:
            return True, ""
        return False, str(detail.get("reason") or "playwright 不可用")

    def supports(self, capability_id: str, low_level=None) -> bool:
        # 签名带 low_level 是 Protocol 要求（base.Executor.supports）。
        # Playwright 侧刻意**不**支持 low_level —— 那套是 shell 命令契约（adb 专属），
        # Web 端的等价物是按名字点/填，没有"跑一条 shell"的概念。
        return capability_id in _SUPPORTED_CAPS

    def execute(self, event: PlanEvent, ctx: ExecutorContext) -> EventResult:
        started_at = _now_iso()
        t0 = time.time()
        cap = event.capability_id
        sn = str(ctx.device.sn or "")
        if not ctx.device.is_web:
            return make_event_result(
                event, status=EventStatus.DECLINED, executor_used=self.id,
                started_at=started_at, elapsed_ms=0,
                summary="playwright 只服务 web 槽，让位给真机通道",
            )
        hub = get_hub()
        headed = headed_from_hint(ctx.device.extra)
        try:
            if cap == "wait_ms":
                p = event.params or {}
                ms = int(p.get("duration_ms") or p.get("ms") or 0)
                time.sleep(max(0, ms) / 1000.0)
                return self._ok(event, started_at, t0, f"等待 {ms}ms")
            if cap == "launch_app":
                p = event.params or {}
                url = pick_goto_url(
                    p.get("url"),
                    ctx.device.extra.get("target_package"),
                    p.get("package"),
                )
                page = hub.current_page(sn)
                if page is None:
                    page = hub.open_case(sn, base_url=url, headed=headed)
                elif url:
                    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                return self._ok(event, started_at, t0, f"打开 {url or page.url}")
            if cap == "close_app":
                p = event.params or {}
                if p.get("shutdown_browser") or p.get("shutdown"):
                    hub.shutdown_thread()
                    return self._ok(event, started_at, t0, "关闭 Chromium")
                hub.close_case(sn)
                return self._ok(event, started_at, t0, "关闭页面")
            page = hub.current_page(sn)
            if page is None:
                page = hub.open_case(
                    sn,
                    base_url=pick_goto_url(ctx.device.extra.get("target_package")),
                    headed=headed,
                )
            if cap == "tap_element":
                return self._tap(event, page, started_at, t0)
            if cap == "multi_tap":
                return self._multi_tap(event, page, started_at, t0)
            if cap == "long_press_element":
                return self._long_press(event, page, started_at, t0)
            if cap == "input_text":
                return self._input(event, page, started_at, t0)
            if cap == "press_key":
                key = str((event.params or {}).get("key") or (event.params or {}).get("keycode") or "Escape")
                mapped = {"back": "Escape", "home": "Home", "enter": "Enter", "esc": "Escape"}.get(key.lower(), key)
                page.keyboard.press(mapped)
                return self._ok(event, started_at, t0, f"按键 {mapped}")
            if cap == "swipe_direction":
                direction = str((event.params or {}).get("direction") or "down").lower()
                box = page.viewport_size or {"width": 1280, "height": 800}
                w, h = int(box["width"]), int(box["height"])
                cx, cy = w // 2, h // 2
                delta = {
                    "up": (cx, int(h * 0.75), cx, int(h * 0.25)),
                    "down": (cx, int(h * 0.25), cx, int(h * 0.75)),
                    "left": (int(w * 0.75), cy, int(w * 0.25), cy),
                    "right": (cx, cy, int(w * 0.75), cy),
                }.get(direction, (cx, int(h * 0.75), cx, int(h * 0.25)))
                page.mouse.move(delta[0], delta[1])
                page.mouse.down()
                page.mouse.move(delta[2], delta[3], steps=12)
                page.mouse.up()
                return self._ok(event, started_at, t0, f"滑动 {direction}")
            if cap == "swipe_element_to_element":
                p = event.params or {}
                x1, y1 = int(p.get("from_x") or 0), int(p.get("from_y") or 0)
                x2, y2 = int(p.get("to_x") or 0), int(p.get("to_y") or 0)
                page.mouse.move(x1, y1)
                page.mouse.down()
                page.mouse.move(x2, y2, steps=12)
                page.mouse.up()
                return self._ok(event, started_at, t0, f"拖拽 ({x1},{y1})→({x2},{y2})")
            return self._fail(event, started_at, t0, f"PlaywrightExecutor 不处理 {cap}")
        except Exception as exc:
            SLog.e(TAG, f"execute exception cap={cap} sn={sn}: {exc}")
            return self._fail(event, started_at, t0, f"exception: {exc}")

    def _tap(self, event: PlanEvent, page, started_at: str, t0: float) -> EventResult:
        name = _name_from_params(event.params or {})
        if name and self._click_by_name(page, name):
            self._settle_page(page)
            return self._ok(event, started_at, t0, f"点击「{name[:40]}」")
        try:
            x, y = self._xy(event, page)
        except ValueError:
            return self._fail(
                event, started_at, t0,
                f"未找到可点目标「{name[:40]}」且没有坐标" if name else "未找到可点目标，且没有坐标",
            )
        page.mouse.click(x, y)
        self._settle_page(page)
        label = f"「{name[:40]}」({x},{y})" if name else f"({x},{y})"
        return self._ok(event, started_at, t0, f"点击 {label}")

    def _multi_tap(self, event: PlanEvent, page, started_at: str, t0: float) -> EventResult:
        from mino_scout.executors.multi_tap import parse_multi_tap

        parsed, err = parse_multi_tap(event.params)
        if err:
            return self._fail(event, started_at, t0, err)
        _, _, count, interval = parsed
        try:
            x, y = self._xy(event, page)
        except ValueError:
            return self._fail(event, started_at, t0, "multi_tap 缺坐标")
        for i in range(count):
            page.mouse.click(x, y)
            if i + 1 < count:
                time.sleep(interval / 1000.0)
        return self._ok(event, started_at, t0, f"连点 ({x},{y}) ×{count} 间隔{interval}ms")

    def _long_press(self, event: PlanEvent, page, started_at: str, t0: float) -> EventResult:
        name = _name_from_params(event.params or {})
        if name:
            locators = (
                page.get_by_role("button", name=name),
                page.get_by_text(name, exact=True),
                page.get_by_text(name),
            )
            for loc in locators:
                try:
                    if loc.count() == 0:
                        continue
                    box = loc.first.bounding_box()
                    if not box:
                        continue
                    x = int(box["x"] + box["width"] / 2)
                    y = int(box["y"] + box["height"] / 2)
                    page.mouse.move(x, y)
                    page.mouse.down()
                    time.sleep(0.8)
                    page.mouse.up()
                    return self._ok(event, started_at, t0, f"长按「{name[:40]}」")
                except Exception:
                    continue
        try:
            x, y = self._xy(event, page)
        except ValueError:
            return self._fail(
                event, started_at, t0,
                f"未找到可长按目标「{name[:40]}」且没有坐标" if name else "未找到可长按目标，且没有坐标",
            )
        page.mouse.move(x, y)
        page.mouse.down()
        time.sleep(0.8)
        page.mouse.up()
        return self._ok(event, started_at, t0, f"长按 ({x},{y})")

    def _click_by_name(self, page, name: str) -> bool:
        locators = (
            page.get_by_role("button", name=name),
            page.get_by_role("link", name=name),
            page.get_by_role("tab", name=name),
            page.get_by_role("menuitem", name=name),
            page.get_by_role("textbox", name=name),
            page.get_by_placeholder(name),
            page.get_by_label(name),
            page.get_by_text(name, exact=True),
            page.get_by_text(name),
        )
        for loc in locators:
            try:
                if loc.count() == 0:
                    continue
                loc.first.click(timeout=4000)
                return True
            except Exception:
                continue
        return False

    def _find_input(self, page, name: str, text: str):
        candidates = []
        if name:
            candidates.extend((
                page.get_by_role("textbox", name=name),
                page.get_by_placeholder(name),
                page.get_by_label(name),
            ))
        raw = (text or "").strip()
        if re.fullmatch(r"1\d{10}", raw):
            candidates.extend((
                page.get_by_placeholder(re.compile(r"手机")),
                page.locator("input[type='tel']"),
                page.locator("input[name*='phone' i], input[name*='mobile' i]"),
            ))
        elif re.fullmatch(r"\d{4,8}", raw):
            candidates.extend((
                page.get_by_placeholder(re.compile(r"验证码")),
                page.locator("input[autocomplete='one-time-code']"),
            ))
        for loc in candidates:
            try:
                if loc.count() > 0:
                    return loc.first
            except Exception:
                continue
        try:
            boxes = page.get_by_role("textbox")
            n = min(boxes.count(), 6)
            for i in range(n):
                el = boxes.nth(i)
                if not el.is_visible():
                    continue
                val = ""
                try:
                    val = str(el.input_value() or "")
                except Exception:
                    val = ""
                if not val:
                    return el
        except Exception:
            pass
        return None

    def _input(self, event: PlanEvent, page, started_at: str, t0: float) -> EventResult:
        text = str((event.params or {}).get("text") or "")
        name = _name_from_params(event.params or {})
        loc = self._find_input(page, name, text)
        if loc is not None:
            loc.fill(text, timeout=5000)
            self._settle_page(page)
            return self._ok(event, started_at, t0, f"输入 {text[:24]}")
        try:
            x, y = self._xy(event, page)
            page.mouse.click(x, y)
        except ValueError:
            pass
        page.keyboard.type(text, delay=20)
        self._settle_page(page)
        return self._ok(event, started_at, t0, f"输入 {text[:24]}")

    @staticmethod
    def _settle_page(page, ms: int = 300) -> None:
        """DOM 变更后短等，避免下一帧截图卡在动画/布局抖动上。"""
        try:
            page.wait_for_timeout(max(0, int(ms)))
        except Exception:
            pass

    def _xy(self, event: PlanEvent, page) -> tuple[int, int]:
        p = event.params or {}
        if p.get("x") is None or p.get("y") is None:
            raise ValueError("缺坐标 x/y")
        x, y = int(p["x"]), int(p["y"])
        box = page.viewport_size or {"width": 1280, "height": 800}
        w, h = int(box["width"]), int(box["height"])
        if 0 <= x <= 1000 and 0 <= y <= 1000 and w > 1000:
            x = int(round(x / 1000.0 * w))
            y = int(round(y / 1000.0 * h))
        return max(0, min(w - 1, x)), max(0, min(h - 1, y))

    def _ok(self, event, started_at, t0, summary: str) -> EventResult:
        return make_event_result(
            event, status=EventStatus.PASS, executor_used=self.id,
            started_at=started_at, elapsed_ms=int((time.time() - t0) * 1000),
            summary=summary,
        )

    def _fail(self, event, started_at, t0, msg: str) -> EventResult:
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id,
            started_at=started_at, elapsed_ms=int((time.time() - t0) * 1000),
            summary=msg, error=msg,
        )
