"""Atomic request identity and run stop latch. Payloads stay in memory only."""
from __future__ import annotations

import copy
import threading
from dataclasses import asdict
from typing import Any

from mino_scout.execution_boundary import ui_write, started


def _same(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same(value, right[key]) for key, value in left.items())
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


class RequestGuard:
    def __init__(self):
        self.lock = threading.RLock()
        self.requests: dict[tuple[str, int], dict] = {}
        self.runs: dict[str, dict] = {}

    def begin(self, req) -> tuple[str, Any]:
        key = (req.run_id, req.step_idx)
        content = asdict(req)
        with self.lock:
            old = self.requests.get(key)
            if old is not None:
                if not _same(old["content"], content):
                    return "request_key_conflict", None
                if not old["done"]:
                    return "request_inflight", None
                if req.step_idx < 0 and req.capability_id in {"read_web_target_facts", "screenshot", "probe"}:
                    old["done"] = False
                    old["worker_active"] = True
                    return "new", None
                return "cached", copy.deepcopy(old["result"])
            if req.step_idx > 0 and (req.device_hint.get("execution_mode") == "v5" or ui_write(req.capability_id)):
                if any(rid == req.run_id and idx > 0 and entry.get("worker_active", not entry["done"])
                       and (req.device_hint.get("execution_mode") == "v5"
                            or ui_write(entry["content"]["capability_id"]))
                       for (rid, idx), entry in self.requests.items()):
                    return "request_inflight", None
            self.requests[key] = {"content": copy.deepcopy(content), "done": False, "worker_active": True}
            return "new", None

    def active_requests(self, run_id: str) -> list[dict]:
        """Bounded admission evidence, never request parameters or credentials."""
        with self.lock:
            return [{"action_ref": {"run_id": rid, "step_idx": idx},
                     "capability_id": entry["content"]["capability_id"],
                     "effect_class": "write" if ui_write(entry["content"]["capability_id"]) else "read"}
                    for (rid, idx), entry in self.requests.items()
                    if rid == run_id and entry.get("worker_active")][:16]

    def same_write_active(self, req) -> bool:
        with self.lock:
            entry = self.requests.get((req.run_id, req.step_idx))
            return bool(req.step_idx > 0 and ui_write(req.capability_id) and entry
                        and entry.get("worker_active") and _same(entry["content"], asdict(req)))

    def finish(self, req, result, *, seal=False, worker_done=True) -> None:
        with self.lock:
            entry = self.requests[(req.run_id, req.step_idx)]
            if worker_done:
                entry["worker_active"] = False
                boundary = started(result.raw_response)
                status = str(getattr(result.status, "value", result.status))
                entry["completion_proven"] = entry.get("completion_proven") is True or boundary == "no" or (boundary == "yes" and status == "pass")
                entry["completion_boundary"] = boundary
            if entry.get("sealed"):
                # 晚结果只证明 worker 已返回，不能替换已返回的超时结论。
                entry["late_status"] = str(getattr(result.status, "value", result.status))
                return
            entry.update(done=True, result=copy.deepcopy(result), sealed=seal)

    def active(self, run_id: str) -> bool:
        with self.lock:
            return any(rid == run_id and entry.get("worker_active")
                       for (rid, idx), entry in self.requests.items() if idx >= 0)

    def stop(self, run_id: str, reason: str, action_ref: dict, *, terminal=False) -> dict:
        with self.lock:
            state = self.runs.setdefault(run_id, {"state": "running"})
            if state["state"] == "running":
                state.update(state="write_stopped", reason_code=reason, action_ref=action_ref)
            if terminal:
                state["state"] = "terminal"
            return dict(state)

    def stopped(self, run_id: str) -> dict:
        with self.lock:
            state = self.runs.get(run_id, {})
            return dict(state) if state.get("state") in {"write_stopped", "terminal"} else {}

    def owns(self, run_id, step_idx) -> bool:
        with self.lock:
            return (run_id, step_idx) in self.requests

    def stop_evidence(self, run_id, step_idx, sn):
        """Seal the old run; never reset its verdict, identity cache or stop latch."""
        with self.lock:
            entry = self.requests.get((run_id, step_idx))
            if not entry or entry["content"].get("sn") != sn:
                return {"stop_confirmed": False, "reason_code": "action_evidence_missing"}
            self.stop(run_id, "recovery_review", {}, terminal=True)
            active = any(rid == run_id and row.get("worker_active")
                         and row["content"].get("capability_id") != "write_stop_status"
                         for (rid, _), row in self.requests.items())
            confirmed = not active and entry.get("completion_proven") is True
            return {"stop_confirmed": confirmed, "reason_code": "worker_completed" if confirmed else "stop_unconfirmed",
                    "completion_boundary": entry.get("completion_boundary", "unknown")}
