"""Sanitize copies at output boundaries; never transform execution inputs."""
from __future__ import annotations

import threading
from typing import Any

_lock = threading.RLock()
_values: set[str] = set()
_sensitive_runs: set[str] = set()
_keys = {"password", "passwd", "secret", "secret_value", "api_key", "access_token", "authorization"}


def remember(value: Any) -> None:
    if isinstance(value, str) and value:
        from mino_scout.log import current_run_id
        with _lock:
            _values.add(value)
            _sensitive_runs.add(current_run_id.get())


def has_sensitive_inputs(run_id):
    with _lock:
        return run_id in _sensitive_runs


def register_inputs(params: dict) -> None:
    for key, value in params.items():
        if str(key).lower() in _keys and not isinstance(value, dict):
            remember(value)
        elif isinstance(value, dict):
            register_inputs(value)
    if params.get("sensitive") or (params.get("control") or {}).get("sensitive") or params.get("field") in {"password", "otp"} or params.get("login_field") in {"password", "otp"}:
        remember(params.get("text"))
        remember(params.get("value"))
        remember((params.get("payload") or {}).get("text"))
        remember((params.get("payload") or {}).get("value"))


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        with _lock:
            secrets = sorted(_values, key=len, reverse=True)
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {k: ("" if str(k).lower() in _keys and not isinstance(v, (dict, bool))
                    else sanitize(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, tuple):
        return tuple(sanitize(v) for v in value)
    return value
