#!/usr/bin/env python3
"""Merge per-OS item-*.json fragments into releases/scout/manifest.json."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_items(src: Path) -> list[dict]:
    rows = []
    for path in sorted(src.rglob("item-*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("filename"):
            rows.append(data)
    return rows


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
