"""Android 应用重置：`pm clear` 清数据 + 缓存（等价于退出登录/回到首启初态）。

2026-10-22 口径变更（用户决策）：以前这里只清缓存，
起因是 2026-10-19 一次事故——能力名写「清缓存」，实际下发 `pm clear`，清掉了应用本地数据。
当时的应对是收窄到只清缓存。现在明确改为**就按 `pm clear` 的语义来用**：

- 清缓存和清数据对用例前置没有差别：两者都要求重新登录；
- 安卓非 root 没有跨应用的登录态 API，`pm clear` 是唯一确定性的未登录重置；
- 因此不再保留「仅清缓存」的选项检测，也不再保留旧的清数据别名。

副作用仍然要在结果里说清楚，不能把「清掉了」写成「清缓存了」：
调用方必须按「应用数据已重置」处理，登录态观察、形象、权限授权都要重新取证。
"""
from __future__ import annotations

import re
from typing import Callable

_PACKAGE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")


def clear_app_cache(package: str, shell: Callable[..., tuple[int, str, str]]) -> dict:
    """清除目标应用的全部数据与缓存。名字保留，语义是应用重置。"""

    def result(ok, reason, message, *, boundary="no", stdout="", stderr=""):
        return {"status": "success" if ok else "error", "message": message,
                "stdout": stdout, "stderr": stderr or ("" if ok else message),
                "execution": {"execution_started": boundary,
                              "phase": "completed" if ok else "resolve" if boundary == "no" else "action",
                              "reason_code": reason, "source": "adb.pm_clear.v1"}}

    if not _PACKAGE.fullmatch(str(package or "")):
        return result(False, "clear_package_invalid", "清数据需要准确的应用包名")
    rc, stdout, stderr = shell("pm", "clear", "--user", "current", package)
    out = stdout.strip()
    if rc == 0 and out.lower().startswith("success"):
        return result(True, "", "已清除应用数据与缓存；应用回到未登录初态，登录态须重新观察",
                      boundary="yes", stdout=stdout)
    # 结果未知（超时/设备断开）与明确失败分开表达：两种都不能当作「已重置」。
    boundary = "no" if rc == -2 else "unknown"
    return result(False, "clear_failed", "清除应用数据未确认成功；未回退为其他清理方式",
                  boundary=boundary, stdout=stdout, stderr=stderr or stdout)
