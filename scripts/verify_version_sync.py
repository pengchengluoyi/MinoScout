#!/usr/bin/env python3
"""版本号只允许写在 mino_scout/VERSION。pyproject 与 core 都读这个文件。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_VERSION = ROOT / "mino_scout" / "VERSION"
_PYPROJECT = ROOT / "pyproject.toml"
_CORE = ROOT / "mino_scout" / "core.py"
_SEMVER = re.compile(r"^\d+\.\d+\.\d+([\-+].*)?$")
_HARDCODED = re.compile(r'(?m)^(?:version|SCOUT_VERSION)\s*=\s*"\d+\.\d+\.\d+')


def main() -> int:
    ver = _VERSION.read_text(encoding="utf-8").strip()
    if not _SEMVER.match(ver):
        print(f"FAIL: mino_scout/VERSION is not semver: {ver!r}")
        return 1
    pyproject = _PYPROJECT.read_text(encoding="utf-8")
    if _HARDCODED.search(pyproject):
        print("FAIL: pyproject.toml still hardcodes version")
        return 1
    if 'file = "mino_scout/VERSION"' not in pyproject:
        print("FAIL: pyproject.toml does not read mino_scout/VERSION")
        return 1
    core = _CORE.read_text(encoding="utf-8")
    if _HARDCODED.search(core):
        print("FAIL: mino_scout/core.py still hardcodes SCOUT_VERSION")
        return 1
    if "VERSION" not in core:
        print("FAIL: mino_scout/core.py does not read VERSION")
        return 1
    print(f"OK verify_version_sync — {ver}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
