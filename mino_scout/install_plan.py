"""分层更新计划（与 MinoStudio electron/scoutLayers.cjs 对齐，并加固 browser/runtime）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

SCOUT_LAYERS = ("runtime", "app", "browser")
BOOTSTRAP_LAYERS = ("runtime", "app")
HEAVY_LAYERS = ("browser",)


def parse_layers_txt(text: str) -> dict[str, str] | None:
    out: dict[str, str] = {}
    for line in str(text or "").splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue
        parts = trimmed.split()
        if len(parts) >= 2:
            out[parts[0]] = parts[1]
    return out or None


def browser_dir_names(browsers_root: Path) -> list[str]:
    if not browsers_root.is_dir():
        return []
    return sorted(
        p.name for p in browsers_root.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )


def runtime_chromium_revision(bin_dir: Path) -> str:
    import json

    for rel in (
        "_internal/playwright/driver/package/browsers.json",
        "playwright/driver/package/browsers.json",
    ):
        path = bin_dir / rel
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for row in data.get("browsers") or []:
            if str(row.get("name") or "") == "chromium":
                return str(row.get("revision") or "")
    return ""


def _layer_step(name: str, layer: dict[str, Any]) -> dict[str, Any]:
    return {
        "layer": name,
        "key": str(layer.get("key") or ""),
        "url": str(layer.get("url") or ""),
        "sha256": str(layer.get("sha256") or ""),
        "filename": str(layer.get("filename") or ""),
        "bytes": int(layer.get("bytes") or 0),
    }


def _combined_plan(item: dict[str, Any], reason: str) -> dict[str, Any]:
    combined_bytes = int(item.get("bytes") or 0)
    return {
        "mode": "combined",
        "reason": reason,
        "bytes": combined_bytes,
        "steps": [{
            "layer": "",
            "key": str(item.get("version") or ""),
            "url": str(item.get("url") or ""),
            "sha256": str(item.get("sha256") or ""),
            "filename": str(item.get("filename") or "scout-installer"),
            "bytes": combined_bytes,
        }],
    }


def _normalize_dirs(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(str(x) for x in value if str(x).strip())


def _refine_browser_steps(
    steps: list[dict[str, Any]],
    *,
    manifest_layers: dict[str, Any],
    installed: dict[str, str],
    bin_dir: Path,
) -> list[dict[str, Any]]:
    """无 Chromium 目录变化时不拉 browser；要拉 browser 时必须带上 runtime。"""
    if not steps:
        return steps
    names = {str(s.get("layer") or "") for s in steps}
    if "browser" not in names:
        return steps

    manifest_browser = manifest_layers.get("browser") if isinstance(manifest_layers, dict) else None
    manifest_dirs = _normalize_dirs((manifest_browser or {}).get("dirs"))
    local_dirs = browser_dir_names(bin_dir / "ms-playwright")

    if manifest_dirs and local_dirs == manifest_dirs:
        steps = [s for s in steps if s.get("layer") != "browser"]
        names.discard("browser")

    if "browser" in {s.get("layer") for s in steps} and "runtime" not in names:
        manifest_runtime = manifest_layers.get("runtime") if isinstance(manifest_layers, dict) else None
        if isinstance(manifest_runtime, dict) and manifest_runtime.get("url"):
            steps.insert(0, _layer_step("runtime", manifest_runtime))
        else:
            return steps  # 无法安全只更 browser

    return steps


def _steps_for_layers(
    layers: dict[str, Any],
    names: tuple[str, ...],
    installed: dict[str, str] | None,
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for name in names:
        layer = layers.get(name)
        if not isinstance(layer, dict) or not layer.get("url"):
            continue
        if installed and installed.get(name) == str(layer.get("key") or ""):
            continue
        steps.append(_layer_step(name, layer))
    return steps


def plan_scout_bootstrap(
    item: dict[str, Any],
    installed: dict[str, str] | None = None,
) -> dict[str, Any]:
    layers = item.get("layers") if isinstance(item.get("layers"), dict) else None
    if not layers:
        return _combined_plan(item, "manifest 没有分层字段（旧版发布）")
    steps = _steps_for_layers(layers, BOOTSTRAP_LAYERS, installed)
    if not steps:
        return {"mode": "up-to-date", "reason": "runtime+app 已就绪", "bytes": 0, "steps": []}
    bytes_total = sum(int(s.get("bytes") or 0) for s in steps)
    return {
        "mode": "layers",
        "reason": "首次安装：仅 runtime+app，Chromium/ADB 由 Scout 启动后自动安装",
        "bytes": bytes_total,
        "steps": steps,
    }


def plan_heavy_deps(
    item: dict[str, Any],
    installed: dict[str, str] | None,
    *,
    bin_dir: Path | None = None,
) -> dict[str, Any]:
    layers = item.get("layers") if isinstance(item.get("layers"), dict) else None
    if not layers:
        return {"mode": "skip", "reason": "无 manifest 分层", "bytes": 0, "steps": []}
    steps = _steps_for_layers(layers, HEAVY_LAYERS, installed)
    if bin_dir is not None:
        steps = _refine_browser_steps(steps, manifest_layers=layers, installed=installed or {}, bin_dir=bin_dir)
    if not steps:
        return {"mode": "up-to-date", "reason": "Chromium 已就绪", "bytes": 0, "steps": []}
    bytes_total = sum(int(s.get("bytes") or 0) for s in steps)
    return {"mode": "layers", "reason": "后台安装 Chromium", "bytes": bytes_total, "steps": steps}


def plan_scout_update(
    item: dict[str, Any],
    installed: dict[str, str] | None,
    *,
    bin_dir: Path | None = None,
) -> dict[str, Any]:
    combined_bytes = int(item.get("bytes") or 0)
    layers = item.get("layers") if isinstance(item.get("layers"), dict) else None
    if not layers:
        return _combined_plan(item, "manifest 没有分层字段（旧版发布）")
    if not installed or not installed.get("runtime"):
        return plan_scout_bootstrap(item, installed)

    steps: list[dict[str, Any]] = []
    for name in SCOUT_LAYERS:
        layer = layers.get(name)
        if not isinstance(layer, dict) or not layer.get("url"):
            continue
        if installed.get(name) == str(layer.get("key") or ""):
            continue
        steps.append(_layer_step(name, layer))

    if bin_dir is not None:
        steps = _refine_browser_steps(steps, manifest_layers=layers, installed=installed, bin_dir=bin_dir)

    if not steps:
        return {"mode": "up-to-date", "reason": "所有层指纹一致", "bytes": 0, "steps": []}

    bytes_total = sum(int(s.get("bytes") or 0) for s in steps)
    if combined_bytes and bytes_total >= combined_bytes:
        return _combined_plan(item, "增量比合并包还大")
    return {"mode": "layers", "reason": "", "bytes": bytes_total, "steps": steps}
