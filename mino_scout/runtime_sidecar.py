"""运行中 daemon 写入的版本侧车 —— `mino-scout status` 不能 import 进另一进程的 sys.modules。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from mino_scout.config import config_dir


def sidecar_path() -> Path:
    return config_dir() / "scout_runtime.json"


def write_runtime_sidecar(*, pid: int | None = None) -> None:
    from mino_scout.app_version import imported_scout_version, read_app_semver, report_scout_version
    from mino_scout.stale_process import read_disk_app_semver

    p = int(pid or os.getpid())
    body: dict[str, Any] = {
        "pid": p,
        "reported_version": report_scout_version(),
        "imported_version": imported_scout_version(),
        "disk_app_semver": read_disk_app_semver() or read_app_semver(),
        "updated_at": time.time(),
    }
    path = sidecar_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def read_runtime_sidecar(*, expect_pid: int = 0) -> dict[str, Any]:
    path = sidecar_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    if expect_pid and int(data.get("pid") or 0) != int(expect_pid):
        return {}
    return data
