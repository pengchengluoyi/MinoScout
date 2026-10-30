"""Evidence that the Scout process which issued an old write no longer runs.

Only proves the executing process (and its runtime children) are gone; it never
proves the write outcome. Nexus still keeps the original unknown verdict.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _runtime_root() -> str:
    # Installed builds keep every runtime child (playwright driver, browsers) under the bundle dir.
    if not getattr(sys, "frozen", False):
        return ""
    return str(Path(sys.executable).resolve().parent) + os.sep


def _device_adb(argv: list[str], sn: str) -> bool:
    if not argv or not Path(argv[0]).name.startswith("adb") or "server" in argv or "fork-server" in argv:
        return False
    return any(arg == "-s" and i + 1 < len(argv) and argv[i + 1] == sn for i, arg in enumerate(argv))


def foreign_runtime_processes(sn: str) -> int:
    """Count live runtime processes not owned by this Scout; -1 when it cannot be determined."""
    root = _runtime_root()
    if not root:
        return -1
    try:
        import psutil
        me = psutil.Process()
        own = {me.pid, *(child.pid for child in me.children(recursive=True))}
        count = 0
        for proc in psutil.process_iter(["pid", "cmdline", "exe"]):
            if proc.info["pid"] in own:
                continue
            argv = [str(arg) for arg in (proc.info.get("cmdline") or [])]
            exe = str(proc.info.get("exe") or "")
            if exe.startswith(root) or (argv and argv[0].startswith(root)) or _device_adb(argv, sn):
                count += 1
        return count
    except Exception:
        return -1


def process_started_at() -> float:
    try:
        import psutil
        return float(psutil.Process().create_time())
    except Exception:
        return 0.0


def retired_evidence(guard, run_id: str, step_idx: int, sn: str) -> dict:
    if guard.owns(run_id, step_idx):
        # This process executed the action itself; only same-process proof applies.
        return {**guard.stop_evidence(run_id, step_idx, sn), "mode": "same_process", "action_present": True}
    guard.stop(run_id, "recovery_review", {}, terminal=True)
    from mino_scout.service import run_lock_held
    facts = {"mode": "retired_generation", "action_present": False, "single_instance_lock": run_lock_held(),
             "process_started_at": process_started_at(), "foreign_processes": foreign_runtime_processes(sn)}
    confirmed = facts["single_instance_lock"] is True and facts["foreign_processes"] == 0 and facts["process_started_at"] > 0
    return {**facts, "stop_confirmed": confirmed,
            "reason_code": "predecessor_retired" if confirmed else "predecessor_unconfirmed"}
