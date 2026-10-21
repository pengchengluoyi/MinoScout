"""运行版本固定于进程启动；磁盘版本用于说明重启后将加载的代码。

源码只读取当前模块所在的源码目录，不能把另一份安装的版本当作源码版本。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

_SCOUT_VERSION_LINE = re.compile(r'^SCOUT_VERSION\s*=\s*"([^"]+)"', re.M)
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+([\-+].*)?$")


def stamp_scout_version_in_core(text: str, semver: str) -> str:
    """旧包仍可能把版本写在 core.py。新包不再改这个文件。"""
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
        root = Path(__file__).resolve().parents[1]
        return (root / "mino_scout" / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _core_py_candidates() -> list[Path]:
    mod = sys.modules.get("mino_scout.core")
    file_path = getattr(mod, "__file__", None) if mod else None
    out = [Path(file_path).resolve() if file_path else Path(__file__).with_name("core.py").resolve()]
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
        out.append(base / "app" / "mino_scout" / "core.py")
    return out


def _read_version_file(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def read_app_semver() -> str:
    """当前 app 层 VERSION。旧安装仍可能只在 core.py 里写着 SCOUT_VERSION。"""
    for path in _core_py_candidates():
        found = _read_version_file(path.with_name("VERSION"))
        if found:
            return found
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
    """上报当前进程已加载的版本；磁盘版本只在本地安装状态中展示。"""
    return imported_scout_version()


def runtime_version_facts() -> dict[str, Any]:
    """同时报告运行版本和本地源码版本，不用磁盘版本重标记旧进程。"""
    from mino_scout.config import development

    source = development()
    running = report_scout_version()
    disk = read_app_semver() if source else ""
    changed = source and process_build_identity().get("source_unchanged") is False
    return {"runtime_environment": "development" if source else "production",
            "running_version": running, "source_version": disk or None,
            "source_changed": bool(changed),
            "restart_required": bool(source and (changed or disk and disk != running))}


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
    if running and isinstance(sidecar, dict):
        out["running_build"] = dict(sidecar.get("process_build") or {})
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


from mino_scout.source_build import ProcessBuild

_PROCESS_BUILD = ProcessBuild(Path(__file__).parent)

def process_build_identity() -> dict[str, Any]:
    return _PROCESS_BUILD.identity()
