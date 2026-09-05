#!/usr/bin/env python3
"""Merge per-OS item-*.json fragments into releases/scout/manifest.json.

## 分层字段

每个 item 除了原有的顶层字段（`url` / `sha256` / `filename` —— 指向**合并包**），
多一个 `layers`，每层带自己的 `key` / `url` / `sha256` / `bytes`：

    {"version": "0.1.7", "items": [{
      "os": "darwin", "arch": "arm64",
      "url": ".../MinoScout-0.1.7-darwin-arm64.zip",     ← 合并包，全新安装用
      "sha256": "...", "filename": "...", "bytes": 460599937,
      "layers": {
        "runtime": {"key": "rt-...", "url": "...", "bytes": 86400810},
        "app":     {"key": "0.1.7",  "url": "...", "bytes": 89522,
                    "requires_runtime": "rt-...", "requires_browser": "bw-..."},
        "browser": {"key": "bw-...", "url": "...", "bytes": 374147780, "optional": true}
      }
    }]}

**顶层字段刻意一个不动**：分层前的 Studio 只读 `url` / `sha256` / `filename`，
它会继续下合并包、装全量，行为与以前完全一致。新客户端才去看 `layers`，
拿本机 `<bin>/layers.txt` 的指纹逐层比对，只下不一致的层。

`bytes` 是给客户端在下载前告诉用户"这次要下 87 KB 还是 439 MB"用的。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 层内需要原样带到 manifest 的字段。url 由 prefix + filename 拼出，不在这里。
_LAYER_PASSTHROUGH = (
    "key", "sha256", "bytes", "filename",
    "requires_runtime", "requires_browser", "optional", "dirs",
)


def load_items(src: Path) -> list[dict]:
    rows = []
    for path in sorted(src.rglob("item-*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("filename"):
            rows.append(data)
    return rows


def _build_layers(raw: dict, prefix: str) -> dict:
    out: dict[str, dict] = {}
    for layer, info in (raw or {}).items():
        if not isinstance(info, dict):
            continue
        filename = str(info.get("filename") or "")
        if not filename:
            continue
        entry: dict = {"url": str(info.get("url") or f"{prefix}/{filename}")}
        for field in _LAYER_PASSTHROUGH:
            if field in info:
                entry[field] = info[field]
        out[layer] = entry
    return out


def build_manifest(items: list[dict], *, version: str, prefix: str) -> dict:
    prefix = prefix.rstrip("/")
    out = []
    for row in items:
        filename = str(row["filename"])
        item = {
            "os": row.get("os") or "",
            "arch": row.get("arch") or "",
            "url": str(row.get("url") or f"{prefix}/{filename}"),
            "sha256": row.get("sha256") or "",
            "installer": row.get("installer") or "zip",
            "filename": filename,
        }
        if row.get("version"):
            item["version"] = row["version"]
        if row.get("payload"):
            item["payload"] = row["payload"]
        if row.get("bytes"):
            item["bytes"] = row["bytes"]
        layers = _build_layers(row.get("layers") or {}, prefix)
        if layers:
            item["layers"] = layers
        out.append(item)
    return {"version": version, "items": out}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Write Scout release manifest.json")
    ap.add_argument("--dist", default="", help="directory containing item-*.json and zips")
    ap.add_argument("--version", default="", help="manifest version (no v prefix)")
    ap.add_argument("--prefix", default="", help="download URL prefix, .../releases/download/v0.1.0")
    ap.add_argument("--out", default="", help="output path (default <dist>/manifest.json)")
    args = ap.parse_args(argv)

    dist = Path(args.dist or os.environ.get("SCOUT_DIST") or ROOT / "dist").expanduser()
    version = str(args.version or os.environ.get("SCOUT_VERSION") or "").strip().lstrip("v")
    prefix = str(args.prefix or os.environ.get("SCOUT_RELEASE_PREFIX") or "").strip()
    if not version:
        raise SystemExit("SCOUT_VERSION / --version is required")
    if not prefix:
        raise SystemExit("SCOUT_RELEASE_PREFIX / --prefix is required")

    items = load_items(dist)
    if not items:
        raise SystemExit(f"no item-*.json under {dist}")

    manifest = build_manifest(items, version=version, prefix=prefix)
    dest = Path(args.out).expanduser() if args.out else dist / "manifest.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
