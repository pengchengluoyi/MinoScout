"""本机拉 GitHub manifest/zip 并执行 install.sh（bootstrap / update 共用）。"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from mino_scout.config import config_dir, load_config, save_config
from mino_scout.install_plan import parse_layers_txt, plan_scout_update
from mino_scout.log import SLog

TAG = "SelfUpdate"

DEFAULT_MANIFEST = (
    "https://github.com/pengchengluoyi/MinoScout/releases/latest/download/manifest.json"
)


def _scout_os_arch() -> tuple[str, str]:
    os_name = {"Darwin": "darwin", "Windows": "win32", "Linux": "linux"}.get(platform.system(), "linux")
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return os_name, arch


def fetch_json(url: str) -> dict[str, Any]:
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "MinoScout"})
    with urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data if isinstance(data, dict) else {}


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


def _download(url: str, dest: Path) -> None:
    req = Request(url, headers={"User-Agent": "MinoScout"})
    with urlopen(req, timeout=600) as resp, dest.open("wb") as out:
        shutil.copyfileobj(resp, out, length=1024 * 1024)


def _verify_sha256(path: Path, expected: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if expected and actual != expected:
        raise RuntimeError(f"sha256 不匹配：期望 {expected}，得到 {actual}")


def _install_zip(zip_path: Path) -> None:
    work = Path(tempfile.mkdtemp(prefix="mino-scout-update-"))
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(work)
        install_sh = next(work.rglob("install.sh"), None)
        if install_sh is None:
            raise RuntimeError("zip 内找不到 install.sh")
        subprocess.run(
            [str(install_sh)],
            cwd=str(install_sh.parent),
            check=True,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


def apply_plan_step(step: dict[str, Any]) -> None:
    url = str(step.get("url") or "")
    if not url:
        raise RuntimeError("更新步骤缺少 url")
    sha = str(step.get("sha256") or "")
    tmp = Path(tempfile.mktemp(suffix=".zip"))
    try:
        SLog.i(TAG, f"下载 {step.get('filename') or url}")
        _download(url, tmp)
        if sha:
            _verify_sha256(tmp, sha)
        _install_zip(tmp)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def run_update(*, manifest_url: str = "") -> dict[str, Any]:
    from mino_scout.service import request_stop

    prefix = config_dir()
    bin_dir = prefix / "bin"
    url = str(manifest_url or manifest_url_from_config()).strip()
    manifest = fetch_json(url)
    item = pick_manifest_item(manifest)
    installed = installed_layers(prefix)
    plan = plan_scout_update(item, installed, bin_dir=bin_dir if bin_dir.is_dir() else None)

    if plan.get("mode") == "up-to-date":
        ver = str(item.get("version") or "")
        cfg = load_config()
        if ver:
            cfg["version"] = ver
            save_config(cfg)
        return {"ok": True, "mode": "up-to-date", "plan": plan}

    request_stop()
    steps = plan.get("steps") or []
    for step in steps:
        apply_plan_step(step)

    ver = str(item.get("version") or "")
    if ver:
        cfg = load_config()
        cfg["version"] = ver
        save_config(cfg)

    return {"ok": True, "mode": plan.get("mode"), "plan": plan, "layers": [s.get("layer") for s in steps]}
