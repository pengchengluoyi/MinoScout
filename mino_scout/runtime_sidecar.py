"""Process-owned version and connection diagnostics. Never persists tokens."""
from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from mino_scout.config import config_dir

_lock = threading.RLock()
_connection: dict[str, Any] = {"state": "starting", "connected": False, "registered": False, "last_error": ""}


def sidecar_path() -> Path:
    return config_dir() / "scout_runtime.json"


def write_runtime_sidecar(*, pid: int | None = None) -> None:
    from mino_scout.app_version import imported_scout_version, read_app_semver, report_scout_version
    from mino_scout.stale_process import read_disk_app_semver
    with _lock:
        body = {"pid": int(pid or os.getpid()), "reported_version": report_scout_version(),
                "imported_version": imported_scout_version(),
                "disk_app_semver": read_disk_app_semver() or read_app_semver(),
                "updated_at": time.time(), "connection": dict(_connection)}
        path = sidecar_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


def note_connection(state: str, *, error: str = "", connected=False, registered=False) -> None:
    with _lock:
        _connection.update(state=state, connected=bool(connected), registered=bool(registered), updated_at=time.time())
        if error:
            _connection["last_error"] = error[:300]
        if state == "registered":
            _connection.update(last_registered_at=time.time(), last_error="")
        if state == "registered" or state == "heartbeat":
            _connection["last_heartbeat_at"] = time.time()
        write_runtime_sidecar()


def note_plugin_access(ready: bool, warning: str = "") -> None:
    with _lock:
        _connection["plugins_ready"] = bool(ready)
        _connection["plugin_warning"] = warning[:200] if not ready else ""
        write_runtime_sidecar()


def read_runtime_sidecar(*, expect_pid: int = 0) -> dict[str, Any]:
    try:
        data = json.loads(sidecar_path().read_text(encoding="utf-8"))
        if not isinstance(data, dict) or (expect_pid and int(data.get("pid") or 0) != int(expect_pid)):
            return {}
        return data
    except (OSError, ValueError, TypeError):
        return {}


def connection_status(side: dict, *, running: bool) -> dict:
    state = dict(side.get("connection") or {}) if isinstance(side.get("connection"), dict) else {}
    if not running:
        return {**state, "state": "stopped", "connected": False, "registered": False}
    if not state:
        return {"state": "unknown", "connected": None, "registered": None,
                "last_error": "运行进程未上报连接状态，请升级并重启 Scout"}
    try:
        updated = float(state.get("updated_at") or 0)
    except (TypeError, ValueError):
        updated = 0
    if not math.isfinite(updated) or time.time() - updated > 90:
        state.update(state="stale", connected=False, registered=False)
    return state


def read_update_status() -> dict:
    try:
        data = json.loads((config_dir() / "scout_update.json").read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            return {}
        return {key: data[key] for key in ("state", "version", "updated_at", "error", "backup") if key in data}
    except (OSError, ValueError, TypeError):
        return {}
