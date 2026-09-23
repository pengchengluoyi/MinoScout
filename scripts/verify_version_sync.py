#!/usr/bin/env python3
"""pyproject.toml version 必须与 mino_scout/core.py SCOUT_VERSION 一致。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_CORE = ROOT / "mino_scout" / "core.py"
_PYPROJECT = ROOT / "pyproject.toml"
_LINE = re.compile(r'^SCOUT_VERSION\s*=\s*"([^"]+)"', re.M)


def _pyproject_version() -> str:
    for line in _PYPROJECT.read_text(encoding="utf-8").splitlines():
        if line.startswith("version"):
            _, _, rest = line.partition("=")
            return rest.strip().strip('"').strip("'")
    raise SystemExit("could not read version from pyproject.toml")


def main() -> int:
    pkg = _pyproject_version()
    text = _CORE.read_text(encoding="utf-8")
    m = _LINE.search(text)
    if not m:
        print("FAIL: SCOUT_VERSION not found in mino_scout/core.py")
        return 1
    core = m.group(1).strip()
    if core != pkg:
        print(f"FAIL: pyproject {pkg} != core SCOUT_VERSION {core}")
        return 1
    print(f"OK verify_version_sync — {pkg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
