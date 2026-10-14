"""Atomic request identity and run stop latch. Payloads stay in memory only."""
from __future__ import annotations

import copy
import threading
from dataclasses import asdict
from typing import Any


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
                    return "new", None
                return "cached", copy.deepcopy(old["result"])
            if req.device_hint.get("execution_mode") == "v5" and req.step_idx > 0:
                if any(rid == req.run_id and idx > 0 and not entry["done"]
                       for (rid, idx), entry in self.requests.items()):
                    return "request_inflight", None
            self.requests[key] = {"content": copy.deepcopy(content), "done": False}
            return "new", None

    def finish(self, req, result) -> None:
        with self.lock:
            entry = self.requests[(req.run_id, req.step_idx)]
            entry.update(done=True, result=copy.deepcopy(result))

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
