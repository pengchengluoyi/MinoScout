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
import hashlib
import re
import os
from pathlib import Path
from urllib.parse import urlsplit

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


def validated_rollback_evidence(items, evidence):
    """Validate explicit release-operator attestations; never infer compatibility.

    Every declared pair must cover all shipped platforms. Evidence references
    remain auditable in the manifest; this does not execute the actual drill.
    """
    if not isinstance(evidence, list):
        raise ValueError("rollback evidence must be a JSON array")
    builds = {row.get("build_id") for row in items}
    platforms = {(row.get("os"), row.get("arch")) for row in items}
    seen = set()
    for record in evidence:
        if not isinstance(record, dict):
            raise ValueError("rollback evidence must contain objects")
        previous = record.get("previous_build_id", "")
        if (not isinstance(previous, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", previous)
                or previous.lower() == "latest" or previous in builds or previous in seen
                or builds != {record.get("build_id")}):
            raise ValueError("rollback evidence has wrong/mixed/duplicate build pair")
        checks = record.get("platforms")
        if not isinstance(checks, list) or not checks:
            raise ValueError("rollback evidence requires platform drill records")
        checked = set()
        for check in checks:
            if not isinstance(check, dict):
                raise ValueError("invalid rollback platform evidence")
            platform = (check.get("os"), check.get("arch"))
            reference = check.get("evidence_url", "")
            parsed = urlsplit(reference) if isinstance(reference, str) else None
            if (platform in checked or platform not in platforms
                    or check.get("update_passed") is not True
                    or check.get("rollback_passed") is not True
                    or check.get("config_preserved") is not True
                    or not parsed or parsed.scheme != "https" or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or any(c.isspace() for c in reference)):
                raise ValueError("rollback evidence needs successful update/rollback/config checks and HTTPS evidence")
            checked.add(platform)
        if checked != platforms:
            raise ValueError("rollback evidence does not cover all shipped platforms")
        seen.add(previous)
    return sorted(seen)


def build_manifest(items: list[dict], *, version: str, prefix: str, rollback_build_ids: list[str] | None = None, rollback_evidence: list[dict] | None = None) -> dict:
    evidence = rollback_evidence if rollback_evidence is not None else []
    verified_ids = validated_rollback_evidence(items, evidence)
    if rollback_build_ids and set(rollback_build_ids) != set(verified_ids):
        raise ValueError("rollback build IDs require matching explicit drill evidence")
    rollback_build_ids = verified_ids
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
    commits = {str(row.get("source_commit") or "") for row in items}
    builds = {str(row.get("build_id") or "") for row in items}
    if len(commits) != 1 or len(builds) != 1 or not re.fullmatch(r"[0-9a-f]{40}", next(iter(commits))):
        raise ValueError("missing/mixed source commit or build identity")
    build = next(iter(builds))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", build) or build.lower() == "latest":
        raise ValueError("invalid fixed build identity")
    platforms = [(row.get("os"), row.get("arch")) for row in items]
    if len(platforms) != len(set(platforms)) or any(
            os_name not in {"darwin", "linux", "win32"} or arch not in {"arm64", "x64"}
            for os_name, arch in platforms):
        raise ValueError("duplicate or unsupported platform")
    if any(row.get("dirty") is not False or row.get("validated") is not True or row.get("payload") != "binary" for row in items):
        raise ValueError("only successfully probed clean binary packages are publishable")
    return {"manifest_schema_version":1, "component":"scout", "version":version,
            "source_commit":next(iter(commits)), "build_id":next(iter(builds)),
            "compatibility":{'runtime': {'bundled': True, 'python': '3.12'}, 'protocol': {'node': 2, 'mcp': '2025-03-26'}, 'api': {'admission': 'explicit-tested-build-pair', 'tested_build_pairs': []}, 'schema': {'required': False}, 'automatic_rollback': bool(rollback_build_ids), 'installation': 'scout-layers', 'scout_update_contract': 1, 'rollback_build_ids': list(rollback_build_ids or []), 'rollback_evidence': evidence}, "items":out}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Write Scout release manifest.json")
    ap.add_argument("--dist", default="", help="directory containing item-*.json and zips")
    ap.add_argument("--version", default="", help="manifest version (no v prefix)")
    ap.add_argument("--prefix", default="", help="download URL prefix, .../releases/download/v0.1.0")
    ap.add_argument("--out", default="", help="output path (default <dist>/manifest.json)")
    ap.add_argument("--rollback-build-id", action="append", default=[],
                    help="可选旧 build 校对列表，可重复；必须与 rollback evidence 的已实测列表一致")
    ap.add_argument("--rollback-evidence-json", default=None,
                    help="已实测的新旧 build 与逐平台证据 JSON；也可用 SCOUT_ROLLBACK_EVIDENCE_JSON")
    args = ap.parse_args(argv)
    evidence = json.loads(args.rollback_evidence_json if args.rollback_evidence_json is not None
                          else os.environ.get("SCOUT_ROLLBACK_EVIDENCE_JSON") or "[]")
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", value) for value in args.rollback_build_id):
        raise SystemExit("invalid rollback build identity")

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

    if "/releases/download/" not in prefix or "/latest" in prefix or not prefix.startswith("https://github.com/"):
        raise SystemExit("fixed GitHub release tag required")
    for row in items:
        for item in [row, *(row.get("layers") or {}).values()]:
            p = dist / str(item["filename"])
            if p.resolve().parent != dist.resolve() or p.stat().st_size != item["bytes"] or hashlib.sha256(p.read_bytes()).hexdigest() != item["sha256"]:
                raise SystemExit("package bytes/digest mismatch")
    manifest = build_manifest(items, version=version, prefix=prefix, rollback_build_ids=args.rollback_build_id, rollback_evidence=evidence)
    dest = Path(args.out).expanduser() if args.out else dist / "manifest.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
