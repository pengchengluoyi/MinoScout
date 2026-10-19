"""Cache-only Android cleanup. Never fall back to clearing application data."""
from __future__ import annotations

import re
from typing import Callable

_PACKAGE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")


def clear_app_cache(package: str, shell: Callable[..., tuple[int, str, str]]) -> dict:
    def result(ok, reason, message, *, boundary="no", stdout="", stderr=""):
        return {"status": "success" if ok else "error", "message": message,
                "stdout": stdout, "stderr": stderr or ("" if ok else message),
                "cleanup_scope": "cache_only",
                "execution": {"execution_started": boundary,
                              "phase": "completed" if ok else "resolve" if boundary == "no" else "action",
                              "reason_code": reason, "source": "adb.cache_only.v1"}}

    if not _PACKAGE.fullmatch(str(package or "")):
        return result(False, "cache_package_invalid", "清缓存需要准确的应用包名")
    rc, help_text, help_error = shell("pm", "help")
    # Unsupported systems must fail before cleanup. No unflagged `pm clear`,
    # Settings-page fallback, or all-app cache eviction is permitted here.
    if rc != 0 or not re.search(r"^\s*clear\s+[^\n]*\[--cache-only\]", help_text, re.M):
        return result(False, "cache_only_unsupported", "设备未确认支持仅清缓存，未清理应用数据")
    rc, stdout, stderr = shell("pm", "clear", "--cache-only", "--user", "current", package)
    if rc == 0 and stdout.strip().lower() == "success":
        return result(True, "", "已清除应用缓存，保留应用数据", boundary="yes", stdout=stdout)
    boundary = "no" if rc == -2 else "unknown"
    return result(False, "cache_only_failed", "仅清缓存未确认成功；未回退清除应用数据",
                  boundary=boundary, stdout=stdout, stderr=stderr or stdout)
