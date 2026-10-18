#!/usr/bin/env python3
"""Launch the source Scout with its own environment from any IDE Python SDK."""
import os
from pathlib import Path
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        print("请先创建 MinoScout/.venv 并安装本项目依赖", file=sys.stderr)
        return 2
    os.chdir(root)
    # Replace this bootstrap interpreter; do not import Scout in the IDE SDK.
    os.execv(str(python), [str(python), "-m", "mino_scout.cli", *(sys.argv[1:] or ["run"])])


if __name__ == "__main__":
    raise SystemExit(main())
