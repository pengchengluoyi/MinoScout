# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""进程内 Playwright 会话：和 adb 一样由 MiniOrangeServer 调起，Chromium 是子进程。

Playwright 的 sync API 必须在创建它的线程里用。CaseRunner 每个 sn 一条 worker
线程，因此 Hub 按线程保存 Playwright / Browser，按 sn 保存当前 Context/Page。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from mino_scout.log import SLog

TAG = "PlaywrightHub"

# Old global name. Kept so is_web_slot still recognizes leftover REGISTER payloads.
LEGACY_WEB_SLOT_SN = "web-local"
VIEWPORT_W = 1280
VIEWPORT_H = 800

_URL_SCHEMES = ("http://", "https://", "file://", "about:", "data:")
# 带点的主机才当网址；末段是常见 TLD。Android 包名 com.foo.bar 的末段不是 TLD。
_WEB_TLDS = {
    "com", "cn", "net", "org", "io", "app", "dev", "cc", "co", "xyz", "me", "ai",
    "test", "local", "edu", "gov", "info", "top", "shop",
}

_probe_lock = threading.Lock()
_probe_cache: tuple[float, str, dict] = (0.0, "", {})
_PROBE_TTL_SEC = 30.0


def browsers_dir() -> Path:
    frozen = getattr(sys, "frozen", False)
    if frozen:
        return Path(sys.executable).resolve().parent / "ms-playwright"
    try:
        from mino_scout.config import config_dir

        return config_dir() / "bin" / "ms-playwright"
    except Exception:
        return Path.home() / ".cache" / "ms-playwright"


def apply_browsers_path() -> Path:
    """Point Playwright at the onedir copy (or Studio-installed) Chromium."""
    existing = str(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    if existing:
        return Path(existing)
    dest = browsers_dir()
    try:
        if dest.is_dir() and any(dest.iterdir()):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(dest)
            return dest
    except OSError:
        pass
    return dest


def web_slot_sn(scout_id: str) -> str:
    """Per-node Playwright sn: `web` + alphanumeric scout_id. Fixed for that install."""
    sid = "".join(c for c in str(scout_id or "").lower() if c.isalnum())
    return f"web{sid}" if sid else LEGACY_WEB_SLOT_SN


def is_web_slot(sn: str = "", platform: str = "") -> bool:
    s = str(sn or "").strip().lower()
    plat = str(platform or "").strip().lower()
    if plat in ("web", "browser", "playwright"):
        return True
    if s in (LEGACY_WEB_SLOT_SN, "web") or s.startswith("web-"):
        return True
    return s.startswith("web") and len(s) > 3 and s[3:].isalnum()


def _session_key(sn: str) -> str:
    return str(sn or "").strip() or "web"


def looks_like_url(value: str) -> bool:
    """是网址才给 Playwright goto；Android 包名 / iOS bundle 一律不算。"""
    s = str(value or "").strip()
    if not s or any(ch.isspace() for ch in s):
        return False
    low = s.lower()
    if low.startswith(_URL_SCHEMES):
        return True
    if low.startswith("localhost") or low.startswith("127.0.0.1"):
        return True
    host = s.split("/", 1)[0].split(":", 1)[0]
    if host.count(".") >= 1:
        last = host.rsplit(".", 1)[-1].lower()
        if last in _WEB_TLDS:
            return True
        if last.isdigit():
            return False
    return False


def normalize_goto_url(value: str) -> str:
    s = str(value or "").strip()
    if not looks_like_url(s):
        return ""
    if s.lower().startswith(_URL_SCHEMES):
        return s
    return "https://" + s


def pick_goto_url(*candidates: Any) -> str:
    for raw in candidates:
        url = normalize_goto_url("" if raw is None else str(raw))
        if url:
            return url
    return ""


def headed_from_env() -> bool:
    raw = str(os.environ.get("MINIORANGE_PLAYWRIGHT_HEADED", "1") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


# probe_playwright() 的"可用"状态名。上游用 "available" 而不是 "connected" ——
# 语义是"包和 Chromium 都在"，不是"此刻正驱动着浏览器"。
# 导成常量，避免调用方各写一遍字面量再写错（已经踩过一次）。
PROBE_OK_STATE = "available"


def probe_playwright() -> tuple[str, dict]:
    """不长期占浏览器：只确认 Python 包和 Chromium 可执行文件在。"""
    global _probe_cache
    apply_browsers_path()
    now = time.time()
    with _probe_lock:
        ts, state, meta = _probe_cache
        if state and now - ts < _PROBE_TTL_SEC:
            return state, dict(meta)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        result = ("disconnected", {"reason": "未安装 playwright：pip install playwright && playwright install chromium"})
        with _probe_lock:
            _probe_cache = (now, result[0], result[1])
        return result
    pw = None
    try:
        pw = sync_playwright().start()
        exe = str(getattr(pw.chromium, "executable_path", "") or "")
        if not exe or not os.path.isfile(exe):
            result = (
                "disconnected",
                {"reason": "未安装 Chromium：在 Server 环境执行 playwright install chromium"},
            )
        else:
            result = ("available", {"browser": "chromium", "path": exe, "headed": headed_from_env()})
    except Exception as exc:
        result = ("disconnected", {"reason": str(exc)[:240]})
    finally:
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass
    with _probe_lock:
        _probe_cache = (now, result[0], dict(result[1]))
    return result


class PlaywrightHub:
    def __init__(self) -> None:
        self._local = threading.local()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _playwright(self):
        pw = getattr(self._local, "pw", None)
        if pw is None:
            from playwright.sync_api import sync_playwright

            pw = sync_playwright().start()
            self._local.pw = pw
            self._local.browsers = {}
            SLog.i(TAG, f"playwright started thread={threading.get_ident()}")
        return pw

    def _browser(self, sn: str, *, headed: Optional[bool] = None):
        key = _session_key(sn)
        browsers: dict = getattr(self._local, "browsers", None) or {}
        browser = browsers.get(key)
        if browser is not None:
            try:
                if browser.is_connected():
                    return browser
            except Exception:
                pass
        head = headed_from_env() if headed is None else bool(headed)
        pw = self._playwright()
        browser = pw.chromium.launch(
            headless=not head,
            args=[
                "--disable-dev-shm-usage",
                "--force-device-scale-factor=1",
                f"--window-size={VIEWPORT_W},{VIEWPORT_H}",
                "--window-position=80,40",
            ],
        )
        browsers[key] = browser
        self._local.browsers = browsers
        SLog.i(TAG, f"chromium launched sn={key} headed={head}")
        return browser

    def open_case(self, sn: str, *, base_url: str = "", headed: Optional[bool] = None) -> Any:
        key = _session_key(sn)
        self.close_case(key)
        browser = self._browser(key, headed=headed)
        context = browser.new_context(
            viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
            screen={"width": VIEWPORT_W, "height": VIEWPORT_H},
            device_scale_factor=1,
            ignore_https_errors=True,
        )
        page = context.new_page()
        url = normalize_goto_url(base_url)
        if url:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        with self._lock:
            self._sessions[key] = {"context": context, "page": page, "base_url": url}
        SLog.i(TAG, f"case context ready sn={key} url={url or 'about:blank'}")
        return page

    def current_page(self, sn: str = "") -> Any:
        key = _session_key(sn)
        with self._lock:
            row = self._sessions.get(key) or {}
        return row.get("page")

    def current_url(self, sn: str = "") -> str:
        page = self.current_page(sn)
        if page is None:
            return ""
        try:
            return str(page.url or "")
        except Exception:
            return ""

    def screenshot_png(self, sn: str = "", *, timeout_ms: int = 15_000, base_url: str = "") -> bytes:
        page = self.current_page(sn)
        if page is None:
            page = self.open_case(sn, base_url=base_url)
        if page is None:
            raise RuntimeError("playwright 当前没有打开的页面")
        return page.screenshot(
            type="png",
            full_page=False,
            scale="css",
            animations="disabled",
            caret="hide",
            timeout=timeout_ms,
        )

    def a11y_text(self, sn: str = "", *, max_chars: int = 4000) -> str:
        page = self.current_page(sn)
        if page is None:
            return ""
        try:
            snap = page.locator("body").aria_snapshot()
        except Exception:
            try:
                snap = page.inner_text("body")
            except Exception as exc:
                return f"(a11y 失败: {exc})"
        text = str(snap or "").strip()
        if len(text) > max_chars:
            return text[:max_chars]
        return text

    def close_case(self, sn: str = "") -> None:
        key = _session_key(sn)
        with self._lock:
            row = self._sessions.pop(key, None)
        if not row:
            return
        ctx = row.get("context")
        if ctx is None:
            return
        try:
            ctx.close()
        except Exception as exc:
            SLog.w(TAG, f"close context sn={key}: {exc}")

    def shutdown_thread(self) -> None:
        with self._lock:
            sns = list(self._sessions.keys())
        for sn in sns:
            self.close_case(sn)
        browsers: dict = getattr(self._local, "browsers", None) or {}
        for sn, browser in list(browsers.items()):
            try:
                browser.close()
            except Exception:
                pass
        self._local.browsers = {}
        pw = getattr(self._local, "pw", None)
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass
            self._local.pw = None
            SLog.i(TAG, f"playwright stopped thread={threading.get_ident()}")


_HUB: Optional[PlaywrightHub] = None
_HUB_LOCK = threading.Lock()


def get_hub() -> PlaywrightHub:
    global _HUB
    with _HUB_LOCK:
        if _HUB is None:
            _HUB = PlaywrightHub()
        return _HUB
