"""本机拉 GitHub manifest/zip 并执行 install.sh（bootstrap / update 共用）。"""
from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from mino_scout.config import config_dir, load_config, save_config
from mino_scout.http_fetch import download_file, fetch_json
from mino_scout.install_plan import parse_layers_txt, plan_scout_update
from mino_scout.log import SLog
from mino_scout import update_progress as UP

TAG = "SelfUpdate"

DEFAULT_MANIFEST = (
    "https://github.com/pengchengluoyi/MinoScout/releases/latest/download/manifest.json"
)


def _scout_os_arch() -> tuple[str, str]:
    os_name = {"Darwin": "darwin", "Windows": "win32", "Linux": "linux"}.get(platform.system(), "linux")
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return os_name, arch


def pick_manifest_item(manifest: dict[str, Any]) -> dict[str, Any]:
    want_os, want_arch = _scout_os_arch()
    items = manifest.get("items")
    if isinstance(items, list):
        for row in items:
            if not isinstance(row, dict) or not row.get("url"):
                continue
            if str(row.get("os") or "").lower() == want_os and str(row.get("arch") or "").lower() in (
                want_arch,
                "x64" if want_arch == "x64" else want_arch,
            ):
                return row
        raise RuntimeError(f"manifest 中没有 {want_os}-{want_arch} 安装包")
    if manifest.get("url"):
        return manifest
    raise RuntimeError("manifest 格式无法识别")


def installed_layers(prefix: Path | None = None) -> dict[str, str] | None:
    base = prefix or config_dir()
    path = base / "bin" / "layers.txt"
    if not path.is_file():
        return None
    try:
        return parse_layers_txt(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def manifest_url_from_config() -> str:
    cfg = load_config()
    url = str(cfg.get("manifest_url") or os.environ.get("MINO_SCOUT_MANIFEST_URL") or "").strip()
    return url or DEFAULT_MANIFEST


def _verify_sha256(path: Path, expected: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if expected and actual != expected:
        raise RuntimeError(f"sha256 不匹配：期望 {expected}，得到 {actual}")


def _install_env(*, inplace: bool) -> dict[str, str]:
    env = os.environ.copy()
    if inplace:
        env["MINO_SCOUT_UPDATING"] = "1"
    return env


def _install_zip(zip_path: Path, *, inplace: bool) -> None:
    work = Path(tempfile.mkdtemp(prefix="mino-scout-update-"))
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(work)
        install_sh = next(work.rglob("install.sh"), None)
        if install_sh is None:
            install_ps1 = next(work.rglob("install.ps1"), None)
            if install_ps1 is None:
                raise RuntimeError("zip 内找不到 install.sh / install.ps1")
            install_sh = install_ps1
        try:
            install_sh.chmod(install_sh.stat().st_mode | 0o111)
        except OSError:
            os.chmod(install_sh, 0o755)
        for root, _dirs, files in os.walk(work):
            for name in files:
                if name.endswith(".sh") or name in ("mino-scout", "install.sh"):
                    p = Path(root) / name
                    try:
                        p.chmod(p.stat().st_mode | 0o111)
                    except OSError:
                        pass
        if install_sh.suffix.lower() == ".ps1":
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(install_sh)],
                cwd=str(install_sh.parent),
                check=True,
                env=_install_env(inplace=inplace),
            )
        else:
            subprocess.run(
                [str(install_sh)],
                cwd=str(install_sh.parent),
                check=True,
                env=_install_env(inplace=inplace),
            )
    finally:
        shutil.rmtree(work, ignore_errors=True)


def apply_plan_step(
    step: dict[str, Any],
    *,
    step_index: int = 0,
    step_count: int = 0,
    inplace: bool = False,
) -> None:
    url = str(step.get("url") or "")
    if not url:
        raise RuntimeError("更新步骤缺少 url")
    sha = str(step.get("sha256") or "")
    layer = str(step.get("layer") or "")
    bytes_total = int(step.get("bytes") or 0)
    tmp = Path(tempfile.mktemp(suffix=".zip"))
    try:
        UP.emit(
            "download",
            label=f"下载 {step.get('filename') or layer or '包'}",
            percent=0,
            layer=layer,
            step_index=step_index,
            step_count=step_count,
            bytes_total=bytes_total,
        )

        def on_progress(received: int, total: int) -> None:
            tot = total or bytes_total or 0
            pct = int(received * 100 / tot) if tot else 0
            UP.emit(
                "download",
                label=f"下载 {layer or '包'}",
                percent=pct,
                layer=layer,
                step_index=step_index,
                step_count=step_count,
                bytes_received=received,
                bytes_total=tot,
            )

        SLog.i(TAG, f"下载 {step.get('filename') or url}")
        download_file(url, tmp, on_progress=on_progress)
        if sha:
            UP.emit(
                "verify",
                label="校验 sha256",
                percent=50,
                layer=layer,
                step_index=step_index,
                step_count=step_count,
            )
            _verify_sha256(tmp, sha)
        UP.emit(
            "install",
            label=f"安装 {layer or '层'}",
            percent=80,
            layer=layer,
            step_index=step_index,
            step_count=step_count,
        )
        _install_zip(tmp, inplace=inplace)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def run_update(*, manifest_url: str = "", inplace: bool | None = None) -> dict[str, Any]:
    from mino_scout.service import pid_alive, read_pid

    prefix = config_dir()
    bin_dir = prefix / "bin"
    if inplace is None:
        inplace = pid_alive(read_pid())
    url = str(manifest_url or manifest_url_from_config()).strip()
    UP.emit("plan", label="读取 manifest", percent=5)
    manifest = fetch_json(url)
    item = pick_manifest_item(manifest)
    installed = installed_layers(prefix)
    plan = plan_scout_update(item, installed, bin_dir=bin_dir if bin_dir.is_dir() else None)

    if plan.get("mode") == "up-to-date":
        ver = str(item.get("version") or "")
        cfg = load_config()
        if ver:
            cfg["version"] = ver
            _sync_config_version(cfg, installed_layers(prefix))
            save_config(cfg)
        UP.emit("done", label="已是最新", percent=100, done=True)
        return {"ok": True, "mode": "up-to-date", "plan": plan}

    steps = plan.get("steps") or []
    step_count = len(steps)
    UP.emit(
        "plan",
        label=f"计划 {plan.get('mode')}（{step_count} 步）",
        percent=10,
        step_count=step_count,
    )
    for idx, step in enumerate(steps):
        apply_plan_step(step, step_index=idx + 1, step_count=step_count, inplace=inplace)

    ver = str(item.get("version") or "")
    if ver:
        cfg = load_config()
        cfg["version"] = ver
        _sync_config_version(cfg, installed_layers(prefix))
        save_config(cfg)

    UP.emit(
        "done",
        label=f"已更新 ({plan.get('mode')})",
        percent=100,
        done=True,
        step_count=step_count,
    )
    return {
        "ok": True,
        "mode": plan.get("mode"),
        "plan": plan,
        "layers": [s.get("layer") for s in steps],
        "inplace": inplace,
    }


def _sync_config_version(cfg: dict[str, Any], layers: dict[str, str] | None) -> None:
    if layers and layers.get("app"):
        cfg["app_layer"] = layers["app"]
    if layers:
        cfg["installed_layers"] = dict(layers)
