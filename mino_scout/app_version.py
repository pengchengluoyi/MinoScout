"""App 层 semver：REGISTER/心跳必须读磁盘，不能只用 import 缓存。

分层更新只替换 `bin/app/mino_scout/*.py`，不会卸载已 import 的 `mino_scout.core`。
进程内 `SCOUT_VERSION` 常停留在启动时的旧值，而磁盘 `core.py` 已是新版本。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_SCOUT_VERSION_LINE = re.compile(r'^SCOUT_VERSION\s*=\s*"([^"]+)"', re.M)


def _core_py_candidates() -> list[Path]:
    out: list[Path] = []
    try:
        from mino_scout.config import config_dir

        out.append(config_dir() / "bin" / "app" / "mino_scout" / "core.py")
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
        out.append(base / "app" / "mino_scout" / "core.py")
    mod = sys.modules.get("mino_scout.core")
    file_path = getattr(mod, "__file__", None) if mod else None
    if file_path:
        out.append(Path(file_path).resolve())
    return out


def read_app_semver() -> str:
    """当前 app 层 `core.py` 里的 SCOUT_VERSION（与 layers.txt 的 app 指纹应对齐）。"""
    for path in _core_py_candidates():
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        m = _SCOUT_VERSION_LINE.search(text)
        if m:
            return m.group(1).strip()
    from mino_scout.core import SCOUT_VERSION

    return SCOUT_VERSION


def imported_scout_version() -> str:
    """本进程启动时 import 进内存的 semver（未必等于磁盘）。"""
    from mino_scout.core import SCOUT_VERSION

    return SCOUT_VERSION


def report_scout_version() -> str:
    """上报 Nexus / UI 用的版本：以磁盘 app 层为准。"""
    return read_app_semver()


def needs_process_reload(*, app_layer_key: str = "") -> bool:
    """磁盘 app 层或 layers 指纹与内存 import 不一致 → 必须 reexec。"""
    imp = imported_scout_version()
    disk = read_app_semver()
    key = str(app_layer_key or "").strip()
    if key and key != imp:
        return True
    if disk and disk != imp:
        return True
    return False
