"""渠道程序的本机文件、版本和是否可用。

只回答下载中心里的安装项。不读用例仓库，不执行 run(case)，也不在业务运行里下载。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import uuid
from pathlib import Path

_REGISTRY = "https://registry.npmjs.org/appium/latest"
_TARBALL_PREFIX = "https://registry.npmjs.org/"
_MAX_BYTES = 80 * 1024 * 1024


def missing_item(platform: str) -> str:
    """本趟平台缺哪一项渠道程序。空字符串表示开跑前的安装项已就绪。"""
    if platform == "android" and shutil.which("adb") is None:
        return "adb"
    if platform == "web" and not _chromium_ready():
        return "chromium"
    # iOS 走本机已有的 WDA 通道，但 Appium 是这一批要先装好的渠道程序。
    if platform == "ios" and not appium_installed():
        return "appium"
    return ""


def appium_installed() -> bool:
    marker = _marker_path()
    package = _package_json()
    if not marker.is_file() or not package.is_file():
        return False
    try:
        recorded = json.loads(marker.read_text(encoding="utf-8"))
        installed = json.loads(package.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    version = str(recorded.get("version") or "")
    digest = str(recorded.get("sha512") or "")
    return bool(version) and version == str(installed.get("version") or "") and digest.startswith("sha512-")


def appium_item(ready: bool) -> dict:
    present = appium_installed() if ready else False
    npm = shutil.which("npm") is not None
    version = None
    if present:
        try:
            version = str(json.loads(_marker_path().read_text(encoding="utf-8")).get("version") or "") or None
        except (OSError, ValueError):
            version = None
    if not ready:
        state = "unknown"
    elif present:
        state = "installed"
    elif npm:
        state = "missing"
    else:
        state = "missing_npm"
    return dict(
        tool_id="appium",
        label="Appium",
        purpose="iOS 渠道程序",
        state=state,
        version=version,
        installable=ready and npm and not present,
        updatable=ready and npm and present,
        detail="安装官方 npm 包并校验摘要。业务开跑不会自动安装。",
    )


def install_appium(action: str) -> str:
    """只从官方 registry 安装。不接受调用方传入的地址，也不执行包内安装脚本。"""
    from mino_scout.plugins import progress as PP

    if action == "install" and appium_installed():
        return "本机已有已校验的 Appium，无需重复下载"
    if shutil.which("npm") is None:
        raise ValueError("缺少 npm，不能安装 Appium")
    PP.emit("download", kind="tool", plugin_id="appium", label="读取 Appium 官方版本", percent=10)
    release = _official_release()
    blob = _download(release["url"])
    _verify(blob, release["sha512"])
    dest = _root()
    if dest.is_symlink():
        raise ValueError("Appium 安装目录不能是外部链接")
    staging = dest.with_name(dest.name + ".staging-" + uuid.uuid4().hex)
    archive = staging.with_name(staging.name + ".tgz")
    try:
        staging.mkdir(parents=True, exist_ok=False)
        archive.write_bytes(blob)
        PP.emit("verify", kind="tool", plugin_id="appium", label="校验 Appium 摘要", percent=70)
        _verify(archive.read_bytes(), release["sha512"])
        PP.emit("install", kind="tool", plugin_id="appium", label="安装已校验的 Appium", percent=85)
        _npm_install(staging, archive)
        package = staging / "node_modules" / "appium" / "package.json"
        installed = json.loads(package.read_text(encoding="utf-8"))
        if str(installed.get("version") or "") != release["version"]:
            raise ValueError("Appium 安装后的版本与官方发布不一致")
        marker = staging / ".installed"
        marker.write_text(json.dumps({"version": release["version"], "sha512": release["sha512"]}, ensure_ascii=False), encoding="utf-8")
        previous = dest.with_name(dest.name + ".previous-" + uuid.uuid4().hex)
        if dest.exists():
            os.replace(dest, previous)
        try:
            os.replace(staging, dest)
        except OSError:
            if previous.exists() and not dest.exists():
                os.replace(previous, dest)
            raise
        if previous.exists():
            shutil.rmtree(previous, ignore_errors=True)
    finally:
        archive.unlink(missing_ok=True)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    PP.emit("done", kind="tool", plugin_id="appium", label="Appium 已安装", percent=100, done=True)
    return "Appium 已安装"


def _chromium_ready() -> bool:
    try:
        from mino_scout.plugins.components import chromium_ready
        return bool(chromium_ready())
    except Exception:
        return False


def _root() -> Path:
    from mino_scout.config import config_dir
    return config_dir() / "tools" / "appium"


def _marker_path() -> Path:
    return _root() / ".installed"


def _package_json() -> Path:
    return _root() / "node_modules" / "appium" / "package.json"


def _official_release() -> dict[str, str]:
    from mino_scout.tools.update.http_fetch import fetch_json

    meta = fetch_json(_REGISTRY, timeout=60)
    dist = meta.get("dist") if isinstance(meta.get("dist"), dict) else {}
    url = str(dist.get("tarball") or "")
    integrity = str(dist.get("integrity") or "")
    version = str(meta.get("version") or "")
    if not url.startswith(_TARBALL_PREFIX) or not integrity.startswith("sha512-") or not version:
        raise ValueError("Appium 官方发布对象无效")
    return {"url": url, "sha512": integrity, "version": version}


def _download(url: str) -> bytes:
    from mino_scout.tools.update.http_fetch import fetch_bytes

    blob = fetch_bytes(url, timeout=180)
    if len(blob) > _MAX_BYTES:
        raise ValueError("Appium 安装包超过 80MB")
    return blob


def _verify(blob: bytes, integrity: str) -> None:
    expect = base64.b64decode(integrity.split("-", 1)[1])
    if hashlib.sha512(blob).digest() != expect:
        raise ValueError("Appium 摘要不一致")


def _npm_install(dest: Path, archive: Path) -> None:
    npm = shutil.which("npm")
    if not npm:
        raise ValueError("缺少 npm，不能安装 Appium")
    with tarfile.open(archive, "r:gz") as packed:
        for member in packed.getmembers():
            name = str(member.name or "")
            if not name or name.startswith("/") or ".." in Path(name).parts:
                raise ValueError("Appium 安装包包含目录外路径")
    result = subprocess.run(
        [npm, "install", "--ignore-scripts", "--no-audit", "--no-fund", "--prefix", str(dest), str(archive)],
        cwd=dest,
        capture_output=True,
        text=True,
        timeout=600,
        env={**os.environ, "npm_config_ignore_scripts": "true"},
    )
    if result.returncode != 0:
        raise RuntimeError("Appium 安装失败")
