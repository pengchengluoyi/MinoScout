"""App 层热更后进程内仍是旧 import —— 必须 reexec 才能加载新代码并正确 REGISTER。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from mino_scout.config import config_dir

_SCOUT_VERSION_LINE = re.compile(r'^SCOUT_VERSION\s*=\s*"([^"]+)"', re.M)


def disk_app_core_path() -> Path:
    return config_dir() / "bin" / "app" / "mino_scout" / "core.py"


def read_disk_app_semver() -> str:
    path = disk_app_core_path()
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = _SCOUT_VERSION_LINE.search(text)
    return m.group(1).strip() if m else ""


def imported_app_semver() -> str:
    try:
        from mino_scout.app_version import imported_scout_version

        return imported_scout_version()
    except Exception:
        mod = sys.modules.get("mino_scout.core")
        if mod is not None:
            return str(getattr(mod, "SCOUT_VERSION", "") or "").strip()
        return ""


def process_stale_vs_disk() -> bool:
    disk = read_disk_app_semver()
    if not disk:
        return False
    imp = imported_app_semver()
    if not imp:
        return False
    return disk != imp


def ensure_process_matches_app_layer(*, log_tag: str = "StaleProcess") -> bool:
    """磁盘 app semver ≠ 内存 import 时 schedule reexec。返回 True 表示已安排重启，调用方应尽快退出。"""
    if not process_stale_vs_disk():
        return False
    from mino_scout.log import SLog
    from mino_scout.service import schedule_reexec

    disk = read_disk_app_semver()
    imp = imported_app_semver()
    SLog.w(
        log_tag,
        f"app 层已更新（磁盘 v{disk}，进程 v{imp}），正在 reexec 以加载新版本",
    )
    schedule_reexec()
    return True
