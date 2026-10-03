# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Web 执行通道：Playwright，和 AdbExecutor 平级。禁止 page.evaluate 改界面。"""
from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import urlparse

from mino_scout.log import SLog

from mino_scout.schemas import EventResult, EventStatus, PlanEvent
from mino_scout.executors.base import (
    ExecutorContext,
    _now_iso,
    make_event_result,
)
from mino_scout.playwright_hub import get_hub, goto_url, headed_from_hint, pick_goto_url
from mino_scout.playwright_context import resolve_locator_scope
from mino_scout.playwright_coords import resolve_viewport_xy, to_viewport_xy
from mino_scout.playwright_locators import (
    css_selector_from_params,
    locator_from_params,
)
from mino_scout.web_focus import locate_editable_at

TAG = "PlaywrightExecutor"

_CSRF_NAME = re.compile(r"csrf|xsrf", re.I)
_AUTH_TOKEN_NAME = re.compile(
    r"^(access_token|refresh_token|id_token|auth_token|authorization)$",
    re.I,
)


def _host_of(url: str) -> str:
    return (urlparse(str(url or "")).hostname or "").lower().rstrip(".")


def _page_matches_expect(current: str, expect: str) -> bool:
    """当前页与目标地址：主机必须相关，路径按前缀比，不用整段 URL 子串。"""
    cu = urlparse(str(current or ""))
    eu = urlparse(str(expect or ""))
    if not _hosts_related(cu.hostname or "", eu.hostname or ""):
        return False
    ep = (eu.path or "/").rstrip("/") or "/"
    cp = (cu.path or "/").rstrip("/") or "/"
    if ep == "/":
        return True
    return cp == ep or cp.startswith(ep + "/")


def _hosts_related(left: str, right: str) -> bool:
    a = str(left or "").lower().rstrip(".")
    b = str(right or "").lower().rstrip(".")
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def _cookie_for_host(domain: str, host: str) -> bool:
    d = str(domain or "").lstrip(".").lower().rstrip(".")
    return _hosts_related(d, host)


def _web_auth_evidence(name: str, value: str) -> str:
    """当前站点上的 JWT 或明确令牌名才算凭据。普通 session/sid/csrf 不算。"""
    key = str(name or "")
    raw = str(value or "")
    if not raw or _CSRF_NAME.search(key):
        return ""
    parts = raw.split(".")
    if len(parts) == 3 and parts[0].startswith("eyJ") and all(parts) and len(raw) >= 20:
        return "jwt"
    if _AUTH_TOKEN_NAME.fullmatch(key) and len(raw) >= 24:
        return "auth_token"
    return ""

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
    "get_foreground_app",
    "read_web_auth",
    "wait_screen_ready",
    "switch_tab",
    "open_tab",
    "upload_file",
    "reload_page",
}


def _input_summary_snippet(text: str, *, max_len: int = 40) -> str:
    """结果摘要展示用，避免 24 字截断把 gmail.com 切成 gmail.co 误导上游。"""
    t = str(text or "").strip()
    if len(t) <= max_len:
        return t
    if "@" in t:
        local, domain = t.split("@", 1)
        head = local[:14] if len(local) > 14 else local
        tail = domain if len(domain) <= 16 else f"…{domain[-12:]}"
        return f"{head}@{tail}"
    return f"{t[: max_len - 1]}…"


def _name_from_params(params: dict) -> str:
    target = params.get("target") if isinstance(params.get("target"), dict) else {}
    for key in ("selector_text", "description", "label", "text"):
        val = params.get(key) or target.get(key if key != "selector_text" else "text")
        s = str(val or "").strip().strip("「」\"'")
        if s:
            return s
    return str(target.get("text") or "").strip()


def _anchor_name(params: dict) -> str:
    """DOM 锚点。不读 input 的 text，那是要输入的内容。"""
    target = params.get("target") if isinstance(params.get("target"), dict) else {}
    for key in ("text", "content_desc", "label"):
        s = str(target.get(key) or "").strip().strip("「」\"'")
        if s:
            return s
    return str(params.get("selector_text") or "").strip()


def _login_field_from_params(params: dict | None) -> str:
    """Nexus Web 渠道：input_text.params.field / login_field（安卓 adb 路径可忽略）。"""
    p = params or {}
    for key in ("login_field", "field"):
        raw = str(p.get(key) or "").strip().lower()
        if raw in ("email", "login_email"):
            return "email"
        if raw == "phone":
            return "phone"
        if raw in ("sms_code", "验证码", "otp"):
            return "sms_code"
        if raw in ("password", "密码"):
            return "password"
    return ""


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
        run_id = str(ctx.run_id or "")
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
                try:
                    ms = int(p.get("duration_ms") or p.get("ms") or 0)
                except (TypeError, ValueError):
                    return self._fail(event, started_at, t0, "wait_ms 时长无效")
                ms = max(0, min(ms, 120_000))
                time.sleep(ms / 1000.0)
                return self._ok(event, started_at, t0, f"等待 {ms}ms")
            if cap == "launch_app":
                p = event.params or {}
                url = pick_goto_url(
                    p.get("url"),
                    ctx.device.extra.get("target_package"),
                    p.get("package"),
                )
                page = hub.current_page(sn, run_id=run_id)
                if page is None:
                    page = hub.open_case(
                        sn,
                        run_id=run_id,
                        base_url=url,
                        headed=headed,
                        goto_params=p,
                    )
                elif url:
                    goto_url(page, url, params=p)
                return self._ok(event, started_at, t0, f"打开 {url or page.url}")
            if cap == "close_app":
                p = event.params or {}
                if p.get("shutdown_browser") or p.get("shutdown"):
                    hub.close_run(sn, run_id, shutdown_browser=True)
                    return self._ok(event, started_at, t0, "关闭本任务 Chromium")
                hub.close_case(sn, run_id=run_id)
                return self._ok(event, started_at, t0, "关闭页面")
            if cap == "read_web_auth":
                return self._read_web_auth(
                    event, hub.current_page(sn, run_id=run_id), started_at, t0
                )
            page = hub.current_page(sn, run_id=run_id)
            if page is None:
                page = hub.open_case(
                    sn,
                    run_id=run_id,
                    base_url=pick_goto_url(ctx.device.extra.get("target_package")),
                    headed=headed,
                )
            if cap == "get_foreground_app":
                return self._get_foreground_app(event, hub, sn, run_id, started_at, t0)
            if cap == "wait_screen_ready":
                return self._wait_screen_ready(event, page, started_at, t0)
            if cap == "switch_tab":
                return self._switch_tab(event, hub, sn, run_id, started_at, t0)
            if cap == "open_tab":
                return self._open_tab(event, hub, sn, run_id, started_at, t0)
            if cap == "upload_file":
                return self._upload_file(event, page, started_at, t0)
            if cap == "reload_page":
                try:
                    page.reload(wait_until="domcontentloaded", timeout=15_000)
                    return self._ok(event, started_at, t0, "已刷新当前页面")
                except Exception as exc:
                    return self._fail(event, started_at, t0, f"刷新页面失败: {exc}")
            if cap == "tap_element":
                return self._tap(event, page, started_at, t0)
            if cap == "multi_tap":
                return self._multi_tap(event, page, started_at, t0)
            if cap == "long_press_element":
                return self._long_press(event, page, started_at, t0)
            if cap == "input_text":
                return self._input(event, page, started_at, t0)
            if cap == "press_key":
                p = event.params or {}
                key = str(p.get("key") or p.get("keycode") or "Escape")
                low = key.lower()
                if low in ("back", "browser_back") and p.get("browser_back", True):
                    try:
                        page.go_back(wait_until="domcontentloaded", timeout=10_000)
                        return self._ok(event, started_at, t0, "浏览器后退")
                    except Exception as exc:
                        return self._fail(event, started_at, t0, f"浏览器后退失败: {exc}")
                mapped = {"back": "Escape", "home": "Home", "enter": "Enter", "esc": "Escape"}.get(low, key)
                page.keyboard.press(mapped)
                return self._ok(event, started_at, t0, f"按键 {mapped}")
            if cap == "swipe_direction":
                p = event.params or {}
                direction = str(p.get("direction") or "down").lower()
                box = page.viewport_size or {"width": 1280, "height": 800}
                w, h = int(box["width"]), int(box["height"])
                fx, fy, tx, ty = p.get("from_x"), p.get("from_y"), p.get("to_x"), p.get("to_y")
                if None not in (fx, fy, tx, ty):
                    x1, y1 = to_viewport_xy(fx, fy, page)
                    x2, y2 = to_viewport_xy(tx, ty, page)
                    delta = (x1, y1, x2, y2)
                    summary = f"滑动 ({fx},{fy})→({tx},{ty})"
                else:
                    cx, cy = w // 2, h // 2
                    delta = {
                        "up": (cx, int(h * 0.75), cx, int(h * 0.25)),
                        "down": (cx, int(h * 0.25), cx, int(h * 0.75)),
                        "left": (int(w * 0.75), cy, int(w * 0.25), cy),
                        "right": (int(w * 0.25), cy, int(w * 0.75), cy),
                    }.get(direction, (cx, int(h * 0.75), cx, int(h * 0.25)))
                    summary = f"滑动 {direction}"
                page.mouse.move(delta[0], delta[1])
                page.mouse.down()
                page.mouse.move(delta[2], delta[3], steps=12)
                page.mouse.up()
                return self._ok(event, started_at, t0, summary)
            if cap == "swipe_element_to_element":
                p = event.params or {}
                if None in (p.get("from_x"), p.get("from_y"), p.get("to_x"), p.get("to_y")):
                    return self._fail(event, started_at, t0, "拖拽需要 from_x/from_y/to_x/to_y")
                x1, y1 = to_viewport_xy(p.get("from_x"), p.get("from_y"), page)
                x2, y2 = to_viewport_xy(p.get("to_x"), p.get("to_y"), page)
                page.mouse.move(x1, y1)
                page.mouse.down()
                page.mouse.move(x2, y2, steps=12)
                page.mouse.up()
                return self._ok(event, started_at, t0, f"拖拽 ({x1},{y1})→({x2},{y2})")
            return self._fail(event, started_at, t0, f"PlaywrightExecutor 不处理 {cap}")
        except Exception as exc:
            SLog.e(TAG, f"execute exception cap={cap} sn={sn}: {exc}")
            return self._fail(event, started_at, t0, f"exception: {exc}")

    def _auth_body(
        self,
        *,
        session: str,
        evidence: str = "",
        host: str = "",
        expected_host: str = "",
        cookie_count: int = 0,
        storage_keys: int = 0,
        reason: str = "",
    ) -> str:
        return json.dumps(
            {
                "session": session,
                "evidence": evidence,
                "host": host,
                "expected_host": expected_host,
                "cookie_count": cookie_count,
                "storage_keys": storage_keys,
                "reason": reason,
            },
            ensure_ascii=False,
        )

    def _read_web_auth(self, event, page, started_at: str, t0: float) -> EventResult:
        """只读当前被测网页域名上的 Cookie / 本地存储。别的站点的令牌不算已登录。不返回值。"""
        if page is None:
            return self._ok(event, started_at, t0, self._auth_body(session="guest", reason="no_page"))
        try:
            page_url = str(page.url or "")
        except Exception:
            page_url = ""
        page_host = _host_of(page_url)
        params = event.params or {}
        expect_host = ""
        for key in ("url", "origin", "package"):
            raw = str(params.get(key) or "").strip()
            if raw.startswith("http://") or raw.startswith("https://"):
                expect_host = _host_of(raw)
                break
        if expect_host and page_host and not _hosts_related(page_host, expect_host):
            return self._ok(
                event,
                started_at,
                t0,
                self._auth_body(
                    session="guest",
                    host=page_host,
                    expected_host=expect_host,
                    reason="page_not_app",
                ),
            )
        try:
            cookies = page.context.cookies([page_url]) if page_url else []
        except Exception:
            try:
                cookies = page.context.cookies()
            except Exception:
                cookies = []
        cookies = [
            item
            for item in (cookies or [])
            if isinstance(item, dict) and _cookie_for_host(str(item.get("domain") or ""), page_host)
        ]
        evidence = ""
        for item in cookies or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            value = str(item.get("value") or "")
            hit = _web_auth_evidence(name, value)
            if hit == "jwt" or (hit and not evidence):
                evidence = hit
            if evidence == "jwt":
                break
        storage = {
            "localJwt": False,
            "localToken": False,
            "sessionJwt": False,
            "sessionToken": False,
            "localCount": 0,
            "sessionCount": 0,
        }
        try:
            raw = page.evaluate(
                """() => {
                  const csrf = /csrf|xsrf/i;
                  const authName = /^(access_token|refresh_token|id_token|auth_token|authorization)$/i;
                  const jwt = (v) => {
                    if (!v || v.length < 20) return false;
                    const p = String(v).split('.');
                    return p.length === 3 && p[0].indexOf('eyJ') === 0 && p[1] && p[2];
                  };
                  const scan = (store) => {
                    let jwtHit = false;
                    let tokenHit = false;
                    for (let i = 0; i < store.length; i++) {
                      const k = store.key(i) || '';
                      const v = store.getItem(k) || '';
                      if (!v || csrf.test(k)) continue;
                      if (jwt(v)) jwtHit = true;
                      else if (authName.test(k) && v.length >= 24) tokenHit = true;
                    }
                    return { jwtHit, tokenHit, count: store.length };
                  };
                  const local = scan(localStorage);
                  const sess = scan(sessionStorage);
                  return {
                    localJwt: local.jwtHit,
                    localToken: local.tokenHit,
                    sessionJwt: sess.jwtHit,
                    sessionToken: sess.tokenHit,
                    localCount: local.count,
                    sessionCount: sess.count,
                  };
                }"""
            )
            if isinstance(raw, dict):
                storage = raw
        except Exception:
            storage = {
                "localJwt": False,
                "localToken": False,
                "sessionJwt": False,
                "sessionToken": False,
                "localCount": 0,
                "sessionCount": 0,
            }
        if storage.get("localJwt") or storage.get("sessionJwt"):
            evidence = "jwt"
        elif not evidence and (storage.get("localToken") or storage.get("sessionToken")):
            evidence = "auth_token"
        local_n = int(storage.get("localCount") or 0)
        session_n = int(storage.get("sessionCount") or 0)
        # 只有当前页域名上的 JWT / 明确令牌名才是已登录。空存储、无关 cookie、对不上的站点都是 guest。
        associated = bool(evidence) and bool(page_host) and (
            not expect_host or _hosts_related(page_host, expect_host)
        )
        return self._ok(
            event,
            started_at,
            t0,
            self._auth_body(
                session="logged_in" if associated else "guest",
                evidence=evidence if associated else "",
                host=page_host,
                expected_host=expect_host,
                cookie_count=len(cookies or []),
                storage_keys=local_n + session_n,
                reason="associated" if associated else "no_credential",
            ),
        )

    def _get_foreground_app(
        self,
        event: PlanEvent,
        hub: Any,
        sn: str,
        run_id: str,
        started_at: str,
        t0: float,
    ) -> EventResult:
        url = hub.current_url(sn, run_id=run_id)
        if not url:
            return self._fail(event, started_at, t0, "无打开页面，无法读取当前 URL")
        expect = pick_goto_url(
            (event.params or {}).get("url"),
            (event.params or {}).get("package"),
        )
        match = None
        if expect:
            match = _page_matches_expect(url, expect)
        summary = url[:120]
        if expect:
            summary = f"{summary}（目标{'命中' if match else '未命中'}）"
        return make_event_result(
            event,
            status=EventStatus.PASS,
            executor_used=self.id,
            started_at=started_at,
            elapsed_ms=int((time.time() - t0) * 1000),
            summary=summary,
            raw_response={
                "page_url": url,
                "package": url,
                "platform": "web",
                "expected_url": expect,
                "match": match,
            },
        )

    def _switch_tab(
        self,
        event: PlanEvent,
        hub: Any,
        sn: str,
        run_id: str,
        started_at: str,
        t0: float,
    ) -> EventResult:
        p = event.params or {}
        try:
            page = hub.switch_tab(
                sn,
                run_id=run_id,
                tab_index=p.get("tab_index") if p.get("tab_index") is not None else None,
                url_contains=str(p.get("url_contains") or p.get("url") or ""),
                title_contains=str(p.get("title_contains") or p.get("title") or ""),
            )
            return self._ok(event, started_at, t0, f"切换 Tab {str(page.url or '')[:80]}")
        except Exception as exc:
            return self._fail(event, started_at, t0, f"切换 Tab 失败: {exc}")

    def _open_tab(
        self,
        event: PlanEvent,
        hub: Any,
        sn: str,
        run_id: str,
        started_at: str,
        t0: float,
    ) -> EventResult:
        p = event.params or {}
        try:
            page = hub.open_tab(
                sn,
                run_id=run_id,
                url=str(p.get("url") or pick_goto_url(p.get("package")) or ""),
            )
            return self._ok(event, started_at, t0, f"新开 Tab {str(page.url or '')[:80]}")
        except Exception as exc:
            return self._fail(event, started_at, t0, f"新开 Tab 失败: {exc}")

    def _upload_file(self, event: PlanEvent, page, started_at: str, t0: float) -> EventResult:
        p = event.params or {}
        paths = p.get("file_paths") or p.get("files")
        if not paths:
            one = str(p.get("file_path") or p.get("path") or "").strip()
            paths = [one] if one else []
        if isinstance(paths, str):
            paths = [paths]
        files = [str(x).strip() for x in paths if str(x).strip()]
        if not files:
            return self._fail(event, started_at, t0, "upload_file 需要 file_path 或 file_paths")
        loc = locator_from_params(page, p)
        if loc is None:
            css = css_selector_from_params(p)
            if css:
                loc = resolve_locator_scope(page, p).locator(css).first
            else:
                loc = resolve_locator_scope(page, p).locator("input[type='file']").first
        try:
            loc.set_input_files(files)
            return self._ok(event, started_at, t0, f"上传 {len(files)} 个文件")
        except Exception as exc:
            return self._fail(event, started_at, t0, f"上传失败: {exc}")

    def _wait_screen_ready(self, event: PlanEvent, page, started_at: str, t0: float) -> EventResult:
        p = event.params or {}
        scope = resolve_locator_scope(page, p)
        timeout = max(1000, min(int(p.get("timeout_ms") or 20_000), 120_000))
        sel = css_selector_from_params(p) or str(p.get("wait_selector") or "").strip()
        try:
            if sel:
                scope.locator(sel).first.wait_for(state="visible", timeout=timeout)
                return self._ok(event, started_at, t0, f"已出现 {sel[:48]}")
            state = str(p.get("load_state") or p.get("wait_until") or "domcontentloaded").strip()
            if state not in ("commit", "domcontentloaded", "load", "networkidle"):
                state = "domcontentloaded"
            page.wait_for_load_state(state, timeout=timeout)
            return self._ok(event, started_at, t0, f"页面 {state}")
        except Exception as exc:
            return self._fail(event, started_at, t0, f"等待就绪超时: {exc}")

    def _tap(self, event: PlanEvent, page, started_at: str, t0: float) -> EventResult:
        params = event.params or {}
        policy = str(params.get("point_policy") or "").strip().lower()
        if policy == "node":
            return self._tap_node(event, page, started_at, t0, params)
        name = _name_from_params(params)
        try:
            x, y = resolve_viewport_xy(params, page)
        except ValueError:
            return self._fail(
                event,
                started_at,
                t0,
                f"Web 点击需要坐标 x/y「{name[:40]}」" if name else "Web 点击需要坐标 x/y",
            )
        page.mouse.click(x, y)
        self._settle_page(page)
        label = f"「{name[:40]}」({x},{y})" if name else f"({x},{y})"
        return self._ok(event, started_at, t0, f"点击 {label}")

    def _tap_node(self, event, page, started_at, t0, params) -> EventResult:
        name = _anchor_name(params)
        if not name:
            return self._fail(event, started_at, t0, "DOM 点击缺少锚点")
        try:
            loc = page.get_by_text(name, exact=True)
            count = int(loc.count())
        except Exception as exc:
            return self._fail(event, started_at, t0, f"DOM 点击锚点未命中：{exc}")
        if count != 1:
            return self._fail(
                event,
                started_at,
                t0,
                f"DOM 点击锚点未唯一命中「{name[:40]}」（{count}）",
            )
        loc.click()
        self._settle_page(page)
        return self._ok(event, started_at, t0, f"点击「{name[:40]}」")

    def _multi_tap(self, event: PlanEvent, page, started_at: str, t0: float) -> EventResult:
        params = event.params or {}
        if str(params.get("point_policy") or "").strip().lower() == "node":
            return self._repeat_node(event, page, started_at, t0, params)
        from mino_scout.executors.multi_tap import parse_multi_tap

        parsed, err = parse_multi_tap(params)
        if err:
            return self._fail(event, started_at, t0, err)
        _, _, count, interval = parsed
        try:
            x, y = resolve_viewport_xy(params, page)
        except ValueError:
            return self._fail(event, started_at, t0, "multi_tap 缺坐标")
        for i in range(count):
            page.mouse.click(x, y)
            if i + 1 < count:
                time.sleep(interval / 1000.0)
        return self._ok(event, started_at, t0, f"连点 ({x},{y}) ×{count} 间隔{interval}ms")

    def _repeat_node(self, event, page, started_at, t0, params) -> EventResult:
        from mino_scout.executors.multi_tap import parse_multi_tap

        parsed, err = parse_multi_tap({**params, "x": params.get("x") or 0, "y": params.get("y") or 0})
        if err:
            return self._fail(event, started_at, t0, err)
        _, _, count, interval = parsed
        name = _anchor_name(params)
        if not name:
            return self._fail(event, started_at, t0, "DOM 连点缺少锚点")
        try:
            loc = page.get_by_text(name, exact=True)
            if int(loc.count()) != 1:
                return self._fail(event, started_at, t0, f"DOM 连点锚点未唯一命中「{name[:40]}」")
        except Exception as exc:
            return self._fail(event, started_at, t0, f"DOM 连点锚点未命中：{exc}")
        for i in range(count):
            loc.click()
            if i + 1 < count:
                time.sleep(interval / 1000.0)
        return self._ok(event, started_at, t0, f"连点「{name[:40]}」×{count}")

    def _long_press(self, event: PlanEvent, page, started_at: str, t0: float) -> EventResult:
        params = event.params or {}
        if str(params.get("point_policy") or "").strip().lower() == "node":
            name = _anchor_name(params)
            if not name:
                return self._fail(event, started_at, t0, "DOM 长按缺少锚点")
            try:
                loc = page.get_by_text(name, exact=True)
                if int(loc.count()) != 1:
                    return self._fail(event, started_at, t0, f"DOM 长按锚点未唯一命中「{name[:40]}」")
                box = loc.bounding_box()
            except Exception as exc:
                return self._fail(event, started_at, t0, f"DOM 长按锚点未命中：{exc}")
            if not box:
                return self._fail(event, started_at, t0, "DOM 长按锚点没有位置")
            x = int(box["x"] + box["width"] / 2)
            y = int(box["y"] + box["height"] / 2)
            page.mouse.move(x, y)
            page.mouse.down()
            time.sleep(0.8)
            page.mouse.up()
            return self._ok(event, started_at, t0, f"长按「{name[:40]}」")
        name = _name_from_params(params)
        try:
            x, y = resolve_viewport_xy(event.params or {}, page)
        except ValueError:
            return self._fail(
                event,
                started_at,
                t0,
                f"Web 长按需要坐标 x/y「{name[:40]}」" if name else "Web 长按需要坐标 x/y",
            )
        page.mouse.move(x, y)
        page.mouse.down()
        time.sleep(0.8)
        page.mouse.up()
        return self._ok(event, started_at, t0, f"长按 ({x},{y})")

    def _input(self, event: PlanEvent, page, started_at: str, t0: float) -> EventResult:
        params = event.params or {}
        text = str(params.get("text") or "")
        login_field = _login_field_from_params(params)
        tag = login_field or "textbox"
        policy = str(params.get("point_policy") or "").strip().lower()
        if policy == "node":
            name = _anchor_name(params)
            if not name:
                return self._fail(event, started_at, t0, "DOM 输入缺少锚点")
            try:
                loc = None
                for cand in (
                    page.get_by_role("textbox", name=name, exact=True),
                    page.get_by_label(name, exact=True),
                    page.get_by_placeholder(name, exact=True),
                ):
                    if int(cand.count()) == 1:
                        loc = cand
                        break
                if loc is None:
                    return self._fail(event, started_at, t0, f"DOM 输入锚点未唯一命中「{name[:40]}」")
            except Exception as exc:
                return self._fail(event, started_at, t0, f"DOM 输入锚点未命中：{exc}")
            loc.fill(text)
            return self._ok(event, started_at, t0, f"输入({tag})「{name[:40]}」")
        try:
            x, y = resolve_viewport_xy(params, page)
        except ValueError:
            return self._fail(event, started_at, t0, f"Web 输入({tag}) 需要坐标 x/y")
        target = locate_editable_at(
            page, x, y, field=login_field, allow_near=policy != "coordinate"
        )
        if target is None:
            return self._fail(
                event,
                started_at,
                t0,
                f"输入({tag})@({x},{y}) {_input_summary_snippet(text)}（焦点未确认）",
            )
        landed = self._commit_input_value(
            page, target, text, x=x, y=y, field=login_field
        )
        if landed:
            return self._ok(
                event,
                started_at,
                t0,
                f"输入({tag})@({x},{y}) {_input_summary_snippet(text)}",
            )
        return self._fail(
            event,
            started_at,
            t0,
            f"输入({tag})@({x},{y}) {_input_summary_snippet(text)}（焦点未确认）",
        )

    @staticmethod
    def _read_editable_value(target) -> str:
        try:
            raw = target.evaluate(
                """el => {
                  if (!el || !el.tagName) return '';
                  const tag = el.tagName.toLowerCase();
                  if (tag === 'input' || tag === 'textarea') return String(el.value || '');
                  return String(el.innerText || el.textContent || '').trim();
                }"""
            )
        except Exception:
            return ""
        return str(raw or "")

    @staticmethod
    def _value_has_expected(got: str, expected: str) -> bool:
        exp = str(expected or "").strip()
        val = str(got or "").strip()
        if not exp or not val:
            return False
        return exp in val

    def _commit_input_value(
        self,
        page,
        target,
        text: str,
        *,
        x: int,
        y: int,
        field: str,
    ) -> bool:
        """把字写进定位到的控件本身。成功只认框里包含完整文本。"""
        expected = str(text or "")
        if not expected.strip():
            return False
        try:
            target.fill(expected, timeout=2000)
        except Exception:
            try:
                target.click(timeout=2000, force=True)
            except Exception:
                try:
                    target.focus()
                except Exception:
                    return False
            try:
                page.keyboard.press("Control+a")
            except Exception:
                pass
            try:
                page.keyboard.press("Meta+a")
            except Exception:
                pass
            try:
                page.keyboard.press("Backspace")
            except Exception:
                pass
            try:
                page.keyboard.type(expected, delay=15)
            except Exception:
                return False
        self._settle_page(page, ms=80)
        if self._value_has_expected(self._read_editable_value(target), expected):
            return True
        fresh = locate_editable_at(page, x, y, field=field)
        if fresh is None:
            return False
        return self._value_has_expected(self._read_editable_value(fresh), expected)

    @staticmethod
    def _settle_page(page, ms: int = 300) -> None:
        """DOM 变更后短等，避免下一帧截图卡在动画/布局抖动上。"""
        try:
            page.wait_for_timeout(max(0, int(ms)))
        except Exception:
            pass

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
