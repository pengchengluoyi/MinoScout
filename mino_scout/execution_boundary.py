"""Adapter for current UI execution receipts; no device or policy decisions."""
from __future__ import annotations
from typing import Any

# Only these framework/read implementations are known not to mutate UI.
READS = frozenset({"write_stop_status", "probe", "probe_device_state", "screenshot", "get_foreground_app", "get_app_version",
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


def normalize_ui_result(result, *, capability_id: str, run_id: str, step_idx: int):
    """Consume explicit producer receipts; an arbitrary PASS proves no boundary."""
    if not ui_write(capability_id):
        return result
    from mino_scout.schemas import EventStatus
    detail = dict(result.raw_response or {})
    boundary = started(detail)
    unsafe = boundary == "unknown" or (result.status is not EventStatus.PASS and boundary != "no")
    if unsafe:
        result.status = EventStatus.BLOCKED
        result.summary = "写动作结果未核验，已停止后备执行并保留资源隔离"
        detail["execution_control"] = {
            **(detail.get("execution_control") if isinstance(detail.get("execution_control"), dict) else {}),
            "stop_run": True, "state": "write_stopped", "reason_code": "write_outcome_unknown",
            "stop_confirmed": False, "resource_isolated": True,
            "action_ref": {"run_id": run_id, "step_idx": step_idx},
        }
    receipt = detail.get("execution") if isinstance(detail.get("execution"), dict) else {}
    detail["execution"] = {**receipt, "execution_started": boundary,
        "phase": receipt.get("phase") or "executor",
        "reason_code": "write_outcome_unknown" if unsafe else receipt.get("reason_code", "")}
    result.raw_response = detail
    return result
