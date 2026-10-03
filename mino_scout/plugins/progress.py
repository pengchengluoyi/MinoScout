"""插件安装进度。和 Scout 自更新的 update_progress 分开。"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

_lock = threading.Lock()
_state: dict[str, Any] = {"active": False}
_hook: Callable[[dict[str, Any]], None] | None = None


def set_progress_hook(fn: Callable[[dict[str, Any]], None] | None) -> None:
    global _hook
    with _lock:
        _hook = fn


def snapshot() -> dict[str, Any]:
    with _lock:
        return dict(_state)


def emit(
    stage: str,
    *,
    kind: str,
    plugin_id: str,
    label: str = "",
    percent: int = 0,
    bytes_received: int = 0,
    bytes_total: int = 0,
    error: str = "",
    done: bool = False,
) -> None:
    payload = {
        "active": not done,
        "stage": str(stage or ""),
        "label": str(label or ""),
        "percent": max(0, min(100, int(percent))),
        "class": str(kind or ""),
        "id": str(plugin_id or ""),
        "bytes_received": int(bytes_received),
        "bytes_total": int(bytes_total),
        "error": str(error or ""),
        "updated_at": time.time(),
    }
    with _lock:
        _state.clear()
        _state.update(payload)
        hook = _hook
    if hook is not None:
        hook(dict(payload))
