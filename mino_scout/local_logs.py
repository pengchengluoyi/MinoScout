"""本机 Scout 日志路径与 tail（launchd 重定向的 scout.log）。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from mino_scout.config import config_dir


def log_paths() -> dict[str, str]:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Logs" / "MinoScout"
        return {
            "stdout": str(base / "scout.log"),
            "stderr": str(base / "scout.err.log"),
            "dir": str(base),
        }
    prefix = config_dir()
    logs = prefix / "logs"
    return {
        "stdout": str(logs / "scout.log"),
        "stderr": str(logs / "scout.err.log"),
        "dir": str(logs),
    }


def _tail_file(path: Path, *, lines: int) -> list[str]:
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows = text.splitlines()
    if lines <= 0:
        return rows
    return rows[-lines:]


def tail_logs(*, lines: int = 200) -> dict[str, Any]:
    n = max(1, min(int(lines or 200), 2000))
    paths = log_paths()
    out: dict[str, Any] = {"paths": paths, "lines": n, "files": {}}
    for key in ("stdout", "stderr"):
        p = Path(paths[key])
        out["files"][key] = {
            "path": str(p),
            "exists": p.is_file(),
            "tail": _tail_file(p, lines=n),
        }
    return out
