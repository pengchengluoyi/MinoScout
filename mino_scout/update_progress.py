"""节点自更新进度（线程安全）。供 transport 上报 node.update_progress。"""
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


def clear() -> None:
    with _lock:
        _state.clear()
        _state["active"] = False


def emit(
    stage: str,
    *,
    label: str = "",
    percent: int = 0,
    layer: str = "",
    step_index: int = 0,
    step_count: int = 0,
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
        "layer": str(layer or ""),
        "step_index": int(step_index),
        "step_count": int(step_count),
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
        try:
            hook(dict(payload))
        except Exception:
            pass
