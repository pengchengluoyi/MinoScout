"""App 层 semver：REGISTER/心跳必须读磁盘，不能只用 import 缓存。

分层更新只替换 `bin/app/mino_scout/*.py`，不会卸载已 import 的 `mino_scout.core`。
进程内 `SCOUT_VERSION` 常停留在启动时的旧值，而磁盘 `core.py` 已是新版本。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

_SCOUT_VERSION_LINE = re.compile(r'^SCOUT_VERSION\s*=\s*"([^"]+)"', re.M)
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+([\-+].*)?$")


def stamp_scout_version_in_core(text: str, semver: str) -> str:
    """打包 app 层时写入与 pyproject / layers.txt 一致的 SCOUT_VERSION。"""
    ver = str(semver or "").strip().lstrip("v")
    if not ver:
        return text
    if _SCOUT_VERSION_LINE.search(text):
        return _SCOUT_VERSION_LINE.sub(f'SCOUT_VERSION = "{ver}"', text, count=1)
    return text


def layer_app_semver(layers: dict[str, str] | None) -> str:
    """layers.txt 的 app 指纹；仅当形如 semver 时当作版本号。"""
    key = str((layers or {}).get("app") or "").strip()
    if key and _SEMVER_RE.match(key):
        return key
    return ""


def package_version_from_pyproject() -> str:
    try:
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        for line in (root / "pyproject.toml").read_text(encoding="utf-8").splitlines():
            if line.startswith("version"):
                _, _, rest = line.partition("=")
                return rest.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


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


def resolve_status_versions(
    *,
    config_version: str = "",
    layers: dict[str, str] | None = None,
    sidecar: dict[str, Any] | None = None,
    running: bool = False,
) -> dict[str, Any]:
    """`mino-scout status` 唯一版本真源：磁盘 app 层 SCOUT_VERSION。

    config.version / layers.app 只在磁盘读不到时作回退；若 layers 与磁盘 semver 不一致，
    标记 install_skew（常见于 app 层指纹已写但 core.py 未随包更新）。
    """
    from mino_scout.stale_process import read_disk_app_semver

    disk = read_disk_app_semver() or read_app_semver()
    layer_sem = layer_app_semver(layers)
    cfg = str(config_version or "").strip()
    version = disk or layer_sem or cfg

    running_ver = ""
    if running and isinstance(sidecar, dict):
        running_ver = str(sidecar.get("imported_version") or "").strip()

    install_skew = bool(layer_sem and disk and layer_sem != disk)
    process_stale = bool(running_ver and disk and running_ver != disk)
    if install_skew and not disk:
        install_skew = False

    out: dict[str, Any] = {
        "version": version,
        "process_stale": process_stale if running else None,
    }
    if install_skew:
        out["install_skew"] = True
        out["layer_app"] = layer_sem
        if disk:
            out["disk_app"] = disk
    if running and running_ver and running_ver != version:
        out["running_version"] = running_ver
        out["process_stale"] = True
    elif running and process_stale:
        out["running_version"] = running_ver
    return out


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
