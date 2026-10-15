"""Adapter for current UI execution receipts; no device or policy decisions."""
from __future__ import annotations
from typing import Any

# Only these framework/read implementations are known not to mutate UI.
READS = frozenset({"probe", "screenshot", "get_foreground_app", "get_app_version",
                   "read_web_auth", "read_web_target_facts", "read_device_data"})


def ui_write(cap: str) -> bool:
    return bool(cap) and cap not in READS and cap != "cancel_run" and not cap.startswith(("node.", "plugin."))


def started(raw: Any) -> str:
    data = raw if isinstance(raw, dict) else {}
    receipt = data.get("execution")
    value = receipt.get("execution_started") if isinstance(receipt, dict) else data.get("execution_started")
    if value is True or type(value) is str and value == "yes":
        return "yes"
    if value is False or type(value) is str and value == "no":
        return "no"
    return "unknown"
