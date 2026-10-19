"""本机 Scout 日志路径与 tail（launchd 重定向的 scout.log）。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from mino_scout.config import config_dir, development


def log_paths() -> dict[str, str]:
    if (sys.platform == "darwin" and not development()
            and config_dir() == Path.home() / "Library/Application Support/MinoScout"):

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
    try:
        if lines <= 0:
            return path.read_text(encoding="utf-8", errors="replace").splitlines()
        # Read backwards until there is an extra line: the leading partial line
        # (and a possibly split UTF-8 character) must never enter the result.
        with path.open("rb") as stream:
            stream.seek(0, 2)
            position = stream.tell()
            chunks = []
            size = 8192
            while position:
                count = min(position, size)
                position -= count
                stream.seek(position)
                chunks.insert(0, stream.read(count))
                rows = b"".join(chunks).decode("utf-8", errors="replace").splitlines()
                if position == 0 or len(rows) > lines:
                    return rows[-lines:]
                size = min(size * 2, 1024 * 1024)
            return []
    except OSError:
        return []



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
