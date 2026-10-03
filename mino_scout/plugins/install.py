"""按发布清单安装 CLI / MCP。不接受调用方临时给的下载地址。"""
from __future__ import annotations

import hashlib
import io
import shutil
import threading
import zipfile
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from mino_scout.log import SLog
from mino_scout.plugins import progress as PP
from mino_scout.plugins.host_exec import HostExecError, run_entry
from mino_scout.plugins.state import clear_plugin_secrets, plugin_dir, spec

TAG = "PluginInstall"
_MAX_BYTES = 512 * 1024 * 1024
_lock = threading.Lock()


class PluginInstallError(Exception):
    pass


def _manifest() -> dict[str, Any]:
    from mino_scout.self_update import fetch_json, manifest_url_from_config

    url = manifest_url_from_config()
    if not url:
        raise PluginInstallError("节点没有 manifest 地址，不能安装插件")
    data = fetch_json(url)
    if not isinstance(data, dict):
        raise PluginInstallError("发布清单不是对象")
    return data


def _release(manifest: dict[str, Any], kind: str, plugin_id: str) -> dict[str, Any]:
    rows = manifest.get("plugins")
    if not isinstance(rows, list) or not rows:
        raise PluginInstallError("发布清单里还没有插件。CLI 不打进 Scout 安装包")
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("class") or "") == kind and str(row.get("id") or "") == plugin_id:
            return row
    raise PluginInstallError(f"发布清单里没有 {kind}/{plugin_id}")


def _safe_extract(blob: bytes, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for info in zf.infolist():
            name = str(info.filename or "")
            if name.startswith("/") or ".." in Path(name).parts:
                raise PluginInstallError("插件压缩包含目录外路径")
        zf.extractall(dest)


def _download(url: str, kind: str, plugin_id: str) -> bytes:
    PP.emit("download", kind=kind, plugin_id=plugin_id, label=f"下载 {plugin_id}", percent=10)
    try:
        resp = urlopen(url, timeout=60)  # noqa: S310 — 地址来自发布清单，不是调用方填写
    except OSError as exc:
        raise PluginInstallError(f"下载失败: {exc}") from exc
    with resp:
        total = int(resp.headers.get("Content-Length") or 0)
        if total > _MAX_BYTES:
            raise PluginInstallError("插件包超过 512MB")
        chunks: list[bytes] = []
        got = 0
        while True:
            piece = resp.read(64 * 1024)
            if not piece:
                break
            got += len(piece)
            if got > _MAX_BYTES:
                raise PluginInstallError("插件包超过 512MB")
            chunks.append(piece)
            percent = 10 + int(60 * got / total) if total else 10
            PP.emit(
                "download",
                kind=kind,
                plugin_id=plugin_id,
                label=f"下载 {plugin_id}",
                percent=min(percent, 70),
                bytes_received=got,
                bytes_total=total,
            )
    return b"".join(chunks)


def install(node_id: str, kind: str, plugin_id: str) -> str:
    row = spec(kind, plugin_id)
    if row is None:
        raise PluginInstallError(f"未知插件 {kind}/{plugin_id}")
    if not row.get("needs_install"):
        raise PluginInstallError("这个插件不需要单独安装")
    if kind not in ("cli", "mcp"):
        raise PluginInstallError("只有 CLI 和 MCP 按需安装")
    from mino_scout import update_progress as UP

    if UP.snapshot().get("active"):
        raise PluginInstallError("Scout 正在更新，等它结束再安装插件")
    if not _lock.acquire(blocking=False):
        raise PluginInstallError("已有插件正在安装")
    dest = plugin_dir(kind, plugin_id)
    staging = dest.with_name(dest.name + ".staging")
    try:
        PP.emit("plan", kind=kind, plugin_id=plugin_id, label="对照清单", percent=5)
        release = _release(_manifest(), kind, plugin_id)
        url = str(release.get("url") or "").strip()
        expect = str(release.get("sha256") or "").strip().lower()
        if not url.startswith("https://") or len(expect) != 64:
            raise PluginInstallError("清单里的插件地址或哈希不完整")
        blob = _download(url, kind, plugin_id)
        PP.emit("verify", kind=kind, plugin_id=plugin_id, label="校验哈希", percent=80)
        digest = hashlib.sha256(blob).hexdigest()
        if digest != expect:
            raise PluginInstallError("插件包哈希不一致")
        if staging.exists():
            shutil.rmtree(staging)
        _safe_extract(blob, staging)
        entry = "install.ps1" if os_name_is_windows() else "install.sh"
        script = staging / entry
        if script.is_file():
            PP.emit("install", kind=kind, plugin_id=plugin_id, label="安装", percent=90)
            try:
                ran = run_entry(
                    staging, entry, [], timeout_sec=600,
                    node_id=node_id, kind=kind, plugin_id=plugin_id,
                )
            except HostExecError as exc:
                raise PluginInstallError(str(exc)) from exc
            if int(ran.get("code") or 0) != 0:
                err = str(ran.get("stderr") or ran.get("stdout") or "安装入口失败")
                raise PluginInstallError(err)
        if dest.exists():
            shutil.rmtree(dest)
        staging.rename(dest)
        (dest / ".installed").write_text("ok\n", encoding="utf-8")
        PP.emit("done", kind=kind, plugin_id=plugin_id, label="安装完成", percent=100, done=True)
        SLog.i(TAG, f"installed {kind}/{plugin_id}")
        return "安装完成"
    except Exception as exc:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        text = str(exc).strip() or "安装失败"
        PP.emit(
            "done", kind=kind, plugin_id=plugin_id,
            label=text[:80], percent=100, error=text[:300], done=True,
        )
        raise
    finally:
        _lock.release()


def remove(node_id: str, kind: str, plugin_id: str) -> str:
    row = spec(kind, plugin_id)
    if row is None:
        raise PluginInstallError(f"未知插件 {kind}/{plugin_id}")
    dest = plugin_dir(kind, plugin_id)
    if dest.is_dir():
        entry = "uninstall.ps1" if os_name_is_windows() else "uninstall.sh"
        if (dest / entry).is_file():
            try:
                run_entry(
                    dest, entry, [], timeout_sec=120,
                    node_id=node_id, kind=kind, plugin_id=plugin_id,
                )
            except HostExecError as exc:
                SLog.w(TAG, f"uninstall {kind}/{plugin_id}: {exc}")
        shutil.rmtree(dest, ignore_errors=True)
    clear_plugin_secrets(node_id, kind, plugin_id)
    return "已移除"


def os_name_is_windows() -> bool:
    import os
    return os.name == "nt"
