"""PowerGuard 引用计数、pid/status/stop、ScoutCore 在途 run 对齐。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mino-scout-svc-")
os.environ["MINO_SCOUT_HOME"] = _TMP

from mino_scout.core import ScoutCore  # noqa: E402
from mino_scout.executors.base import DeviceRef, ExecutorContext, make_event_result, now_iso  # noqa: E402
from mino_scout.power import PowerGuard, reset_guard  # noqa: E402
from mino_scout.protocol import Execute  # noqa: E402
from mino_scout.schemas import EventStatus, PlanEvent  # noqa: E402
from mino_scout.service import (  # noqa: E402
    collect_status,
    pid_alive,
    pid_path,
    read_pid,
    write_pid,
    clear_pid,
)

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{' — ' + detail if detail else ''}")
        failures.append(name)


print("== PowerGuard acquire / release / sync ==")
g = PowerGuard(noop=True)
g.acquire("run-a")
g.acquire("run-a")
check("idempotent holder", g.holders == ["run-a"])
check("active after acquire", g.active is True)
g.acquire("run-b")
check("two holders", g.holders == ["run-a", "run-b"])
g.release("run-a")
check("still active", g.active is True)
g.release("run-b")
check("released", g.active is False)
g.sync(["run-c", "run-d"])
check("sync replace", g.holders == ["run-c", "run-d"])
g.sync([])
check("sync empty", g.active is False)
st = g.status()
check("status keys", st["active"] is False and st["holders"] == [])

print("== service pid ==")
me = os.getpid()
write_pid(me)
check("pid file", pid_path().is_file())
check("read pid", read_pid() == me)
check("alive self", pid_alive(me) is True)
check("dead pid", pid_alive(1_000_000_001) is False)
st = collect_status()
check("status running", st["running"] is True and st["pid"] == me)
clear_pid(only_if=me)
check("cleared", read_pid() == 0)
st = collect_status()
check("status stopped", st["running"] is False)
print(json.dumps({"config": st["config"]}, ensure_ascii=False))

print("== ScoutCore active_runs + power ==")
guard = reset_guard(noop=True)


class FakeAdb:
    id = "adb"
    provides = ["tap"]

    def probe(self):
        return True, ""

    def supports(self, capability_id, low_level=None):
        return capability_id == "tap"

    def execute(self, event: PlanEvent, ctx: ExecutorContext):
        return make_event_result(
            event, status=EventStatus.PASS, executor_used="adb",
            started_at=now_iso(), elapsed_ms=1, summary="ok",
        )


core = ScoutCore({"adb": FakeAdb()}, node_id="test-node")
req = Execute(
    run_id="run-1", step_idx=0, sn="R5", capability_id="tap",
    params={"x": 1, "y": 2}, executor_order=["adb"],
)
res = core.execute(req)
check("execute pass", res.status is EventStatus.PASS)
hb = core.heartbeat()
check("busy", hb.busy is True and hb.active_runs == ["run-1"])
check("power held", guard.active is True and "run-1" in guard.holders)
core.cancel_run("run-1")
hb = core.heartbeat()
check("cancel clears", hb.busy is False and hb.active_runs == [])
check("power released", guard.active is False)

print()
if failures:
    print(f"FAILED {len(failures)}: {', '.join(failures)}")
    sys.exit(1)
print("ALL OK — power/service")
