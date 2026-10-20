"""ScoutCore：纯本地、零网络、可单测。

`transport/` 负责收发协议消息，把载荷交给这里；`core` 只认
`Execute → EventResult`，**内部不出现任何连接形态的分支**
（CLAUDE.md §2.2）。

统一入口：`execute(Execute)` 是能力调用与框架指令的唯一 dispatch。
截图 / 探活 / 节点 stop·restart·update / 取消 run 都走这里。
`observe()` 是给本地测试用的薄包装，内部仍构造 Execute。

幂等（协议 §4.4 / CONVENTIONS.md §5）也在这一层：`(run_id, step_idx)` 是唯一键，
重复的 EXECUTE 直接回缓存，不重新操作设备。`step_idx < 0` 不做幂等。
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from dataclasses import replace
from typing import Any, Optional

from mino_scout import protocol as P
from mino_scout import screen as SCREEN
from mino_scout.executors.base import DeviceRef, Executor, make_event_result, now_iso
from mino_scout.log import SLog, current_run_id, current_step_idx
from mino_scout.router import Router
from mino_scout.execution_boundary import ui_write, started as execution_started
from mino_scout.schemas import CapturedScreen, EventResult, EventStatus, PlanEvent

TAG = "ScoutCore"

SCOUT_VERSION = Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip()
# 单节点 Web 槽 Playwright 并行路数（与 Nexus WEB_PLAYWRIGHT_PARALLEL_LANES 一致）
PLAYWRIGHT_PARALLEL_LANES = 4

# 幂等缓存保留时长。CONVENTIONS.md §5：该 run 结束或 10 分钟，取先到者。
_IDEMPOTENT_TTL_SEC = 600.0

# adb 已连接但默认输入法被改回系统键盘时，心跳里复检（手机重启后常见）。
_ADB_IME_DRIFT_CHECK_SEC = 300.0

_NODE_COMMANDS = {
    "node.stop": "stop",
    "node.restart": "restart",
    "node.log_tail": "log_tail",
    "node.sleep": "sleep",
    "node.wake": "wake",
    "node.plugin_install": "plugin_install",
    "node.plugin_remove": "plugin_remove",
    "node.plugin_config": "plugin_config",
}


def _adb_channel_state(dev: P.DeviceManifest) -> str:
    return str(dict(dev.channels or {}).get("adb") or "").strip().lower()


def connected_android_adb_serials(devices: list[P.DeviceManifest]) -> list[str]:
    out: list[str] = []
    for dev in devices:
        if str(dev.platform or "").lower() != "android":
            continue
        if _adb_channel_state(dev) != "connected":
            continue
        sn = str(dev.sn or "").strip()
        if sn:
            out.append(sn)
    return out


def serials_adb_just_connected(
    prev: dict[str, P.DeviceManifest],
    now: list[P.DeviceManifest],
) -> list[str]:
    """adb 从非 connected → connected（含重连后出现的新 serial）。"""
    current = {d.sn: d for d in now if str(d.sn or "").strip()}
    serials: list[str] = []
    for sn, dev in current.items():
        if str(dev.platform or "").lower() != "android":
            continue
        if _adb_channel_state(dev) != "connected":
            continue
        old = prev.get(sn)
        if old is None or _adb_channel_state(old) != "connected":
            serials.append(sn)
    return serials


class ScoutCore:
    def __init__(self, executors: dict[str, Executor], *, node_id: str = ""):
        from mino_scout.config import resolve_scout_id, sanitize_scout_id

        # scout_id == Register.node_id. One alphanumeric id; do not add a second.
        self.node_id = sanitize_scout_id(node_id) or resolve_scout_id()
        self.scout_id = self.node_id
        self.executors = executors
        self._plugins_ready = False
        self._plugin_warning = ""
        self.router = Router(executors)
        self._started = time.time()
        self._lock = threading.Lock()
        # (run_id, step_idx) -> (完成时刻, EventResult)
        self._done: dict[tuple[str, int], tuple[float, EventResult]] = {}
        self._active_runs: set[str] = set()
        from mino_scout.fixed_update import loaded_build
        self._loaded_build = loaded_build()
        self._update_busy = False
        self._update_io = 0
        self._run_seen: dict[str, float] = {}
        self._asleep = False
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="scout-exec")
        # 已取消的 run。随后到的截图不能再把它写回在途名单，否则休眠会误判还有任务。
        self._cancelled_runs: dict[str, float] = {}
        # Playwright sync API 绑定线程：按 run_id 分片，同 run 固定在同一条 worker；路数固定 4。
        self._pw_shard_count = PLAYWRIGHT_PARALLEL_LANES
        self._pw_shards = [
            ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"scout-pw-{i}")
            for i in range(self._pw_shard_count)
        ]
        self._pw_lock = threading.Lock()
        self._pw_worker = threading.local()
        self._adb_ime_last_check: dict[str, float] = {}
        self._device_workload: dict[str, P.DeviceWorkload] = {}
        from mino_scout.request_guard import RequestGuard
        from mino_scout.web_v5 import WebV5Runtime
        self._requests = RequestGuard()
        from mino_scout.execution_binding import BindingGuard
        self.binding_guard = BindingGuard()
        self._web_v5 = WebV5Runtime()

    # ---------------- EXECUTE（唯一 dispatch） ----------------

    def execute(self, req: P.Execute) -> EventResult:
        if req.capability_id == "node.source_read":
            from mino_scout.case_sources import dispatch
            return dispatch(self, req)
        if req.capability_id in {"node.update", "update"}:
            return make_event_result(_event_from_execute(req), status=EventStatus.BLOCKED,
                executor_used="core", started_at=now_iso(), elapsed_ms=0,
                summary="升级与回退功能已取消", raw_response={"execution": {
                    "execution_started": "no", "reason_code": "component_updates_removed"}})
        with self._lock:
            if self._update_busy:
                return make_event_result(_event_from_execute(req), status=EventStatus.BLOCKED,
                    executor_used="core", started_at=now_iso(), elapsed_ms=0,
                    summary="更新安装已开始，节点不再接受设备或 IDE 操作",
                    raw_response={"execution": {"execution_started": "no", "reason_code": "blocked_by_update"}})
            self._update_io += 1
        try:
            return self._execute_with_development(req)
        finally:
            with self._lock:
                self._update_io -= 1


    def _execute_with_development(self, req: P.Execute) -> EventResult:
        if req.capability_id == "node.ide_prepare":
            from mino_scout.ide_prepare import dispatch
            try:
                return dispatch(self, req)
            except Exception as exc:
                return make_event_result(_event_from_execute(req), status=EventStatus.BLOCKED,
                    executor_used="ide", started_at=now_iso(), elapsed_ms=0,
                    summary="开发准备回执不可确认", error=type(exc).__name__)
        with self._lock:
            if getattr(self, "_development_busy", False) and req.capability_id != "cancel_run":
                return make_event_result(_event_from_execute(req), status=EventStatus.BLOCKED,
                    executor_used="core", started_at=now_iso(), elapsed_ms=0,
                    summary="开发环境正在准备，稍后再运行测试",
                    raw_response={"execution": {"execution_started": "no", "reason_code": "development_busy"}})
            self._development_io = getattr(self, "_development_io", 0) + 1
        try:
            return self._execute_guarded(req)
        finally:
            with self._lock:
                self._development_io -= 1

    def _execute_guarded(self, req: P.Execute) -> EventResult:
        from mino_scout.secret_redaction import register_inputs, sanitize
        from mino_scout.web_v5 import CLEANUP, READS, WRITES
        register_inputs(req.params)
        register_inputs(req.device_hint)
        if req.capability_id == "write_stop_status":
            # Administrative recheck: no device I/O, no business result replacement.
            ref = req.params.get("action_ref") or {}
            generation = self.binding_guard.process_generation
            claimed = req.params.get("scout_process_generation")
            shaped = ref.get("run_id") == req.run_id and type(ref.get("step_idx")) is int
            if shaped and claimed == generation:
                evidence = {**self._requests.stop_evidence(req.run_id, ref["step_idx"], req.sn), "mode": "same_process"}
                if req.params.get("execution_mode") == "v5" and evidence.get("action_present"):
                    # A strict write ran inside a lane worker; stop means that process tree is gone.
                    terminated = evidence.get("worker_active") is False and self._web_v5.close(req.run_id, time.monotonic() + 2)
                    evidence.update(lane_terminated=terminated, stop_confirmed=terminated,
                                    reason_code="lane_process_terminated" if terminated else "lane_quarantined")
            elif shaped and req.params.get("mode") == "retired_generation":
                from mino_scout.process_retirement import retired_evidence
                evidence = retired_evidence(self._requests, req.run_id, ref["step_idx"], req.sn)
            else:
                evidence = {"stop_confirmed": False, "reason_code": "process_or_action_mismatch"}
            return make_event_result(_event_from_execute(req), status=EventStatus.PASS,
                executor_used="core", started_at=now_iso(), elapsed_ms=0, summary=evidence["reason_code"],
                raw_response={"stop_evidence": {**evidence, "contract": "write_stop.v1",
                    "action_ref": ref, "sn": req.sn, "scout_process_generation": generation}})
        state, cached = self._requests.begin(req)
        if state == "cached":
            return cached
        if state != "new":
            # Same-key retry can refer to a write already running. It is not proof
            # that the original action never started; seal uncertainty monotonically.
            duplicate_write = state == "request_inflight" and self._requests.same_write_active(req)
            raw = {"execution": {"execution_started": "unknown" if duplicate_write else "no",
                                  "phase": "admission", "reason_code": state},
                   "admission": {"active_requests": self._requests.active_requests(req.run_id)}}
            if duplicate_write:
                stopped = self._requests.stop(req.run_id, "write_inflight_unknown", {"run_id": req.run_id, "step_idx": req.step_idx})
                raw["execution_control"] = {"stop_run": True, "resource_isolated": True,
                                            "stop_confirmed": False, **stopped}
            result = make_event_result(_event_from_execute(req), status=EventStatus.FAIL if req.device_hint.get("execution_mode") == "v5" else EventStatus.BLOCKED,
                executor_used="core", started_at=now_iso(), elapsed_ms=0, summary=state,
                raw_response=raw)
            if duplicate_write:
                self._requests.finish(req, result, seal=True, worker_done=False)
            return result
        key = (req.run_id, req.step_idx)
        rejected = self._binding_rejection(req)
        if rejected is not None:
            self._requests.finish(req, rejected)
            return rejected
        if req.device_hint.get("execution_mode", "legacy") == "v5":
            stopped = self._requests.stopped(req.run_id)
            if stopped and req.capability_id not in READS | CLEANUP:
                output = {"status": "fail", "data": {"execution": {"execution_started": "no",
                    "phase": "admission", "reason_code": "run_write_stopped"}, "execution_control": stopped}}
            elif req.sn != f"web{self.node_id}" or req.platform != "web":
                output = {"status": "fail", "data": {"execution": {"execution_started": "no",
                    "phase": "admission", "reason_code": "node_pin_mismatch"}}}
            else:
                output = self._web_v5.execute(req, admit=lambda: self.binding_guard.validate(req))
            raw = output["data"]
            reason = raw.get("execution", {}).get("reason_code", "")
            with self._requests.lock:
                late_stop = self._requests.stopped(req.run_id)
                if late_stop and req.capability_id in WRITES:
                    output["status"] = "fail"
                    reason = late_stop.get("reason_code", "run_write_stopped")
                    raw["execution_control"] = {"stop_run": True, **late_stop}
                if reason and req.capability_id in WRITES:
                    stop = self._requests.stop(req.run_id, reason, {"run_id": req.run_id, "step_idx": req.step_idx})
                    raw["execution_control"] = {"stop_run": True, **stop}
                elif req.capability_id in CLEANUP:
                    self._requests.stop(req.run_id, "run_closed", {}, terminal=True)
                result = make_event_result(_event_from_execute(req), status=EventStatus(output["status"]),
                    executor_used="playwright_v5", started_at=now_iso(), elapsed_ms=0,
                    summary=reason or req.capability_id, error=output.get("error", reason), raw_response=raw)
                self._requests.finish(req, result)
            return result

        stopped = self._requests.stopped(req.run_id)
        if stopped and ui_write(req.capability_id):
            result = make_event_result(_event_from_execute(req), status=EventStatus.BLOCKED,
                executor_used="core", started_at=now_iso(), elapsed_ms=0,
                summary="当前运行写入已停止，原动作结果未核验",
                raw_response={"execution": {"execution_started": "no", "phase": "admission", "reason_code": "run_write_stopped"},
                              "execution_control": {"stop_run": True, **stopped}})
            self._requests.finish(req, result)
            return result
        timeout = float(req.timeout_sec or 0) or 30.0
        on_playwright = _use_playwright_thread(req)
        pool = self._playwright_executor(req) if on_playwright else self._pool
        retired = threading.Event() if on_playwright else None
        holder: dict[str, int] = {}
        if on_playwright:
            fut = pool.submit(self._execute_playwright, req, retired, holder)
        else:
            fut = pool.submit(self._execute_body, req)
        # Only actual worker completion releases the in-flight identity. A caller
        # timing out does not prove that the action stopped.
        def completed(future):
            try:
                value = future.result()
                if ui_write(req.capability_id) and value.status is not EventStatus.PASS and execution_started(value.raw_response) != "no":
                    self._requests.stop(req.run_id, "write_outcome_unknown", {"run_id": req.run_id, "step_idx": req.step_idx})
                self._requests.finish(req, value)
            except Exception:
                pass
        fut.add_done_callback(completed)
        try:
            result = fut.result(timeout=timeout)
        except TimeoutError:
            if retired is not None:
                retired.set()
                self._retire_playwright_shard(req, holder.get("ident"))
            result = _timeout_result(req, timeout, lane_reset=retired is not None)
            if ui_write(req.capability_id):
                stop = self._requests.stop(req.run_id, "write_timeout_unknown", {"run_id": req.run_id, "step_idx": req.step_idx})
                result.raw_response["execution_control"] = {"stop_run": True, **stop,
                    "stop_confirmed": False, "resource_isolated": True}
            self._requests.finish(req, result, seal=True, worker_done=fut.done())
            SLog.w(TAG, f"execute timeout {timeout}s cap={req.capability_id} key={key}")
            return result
        # Future.result may wake the caller before its completion callback finishes.
        # Publish the same stop/cache state before acknowledging completion.
        if ui_write(req.capability_id) and result.status is not EventStatus.PASS and execution_started(result.raw_response) != "no":
            self._requests.stop(req.run_id, "write_outcome_unknown", {"run_id": req.run_id, "step_idx": req.step_idx})
        self._requests.finish(req, result)
        return result

    def _execute_playwright(
        self, req: P.Execute, retired: threading.Event, holder: dict[str, int]
    ) -> EventResult:
        """Playwright 同步 API 绑在这条线程上。超时后调用方会换一条线程，这里只关自己的浏览器。"""
        from mino_scout.playwright_hub import get_hub

        holder["ident"] = threading.get_ident()
        get_hub().note_lane(str(req.run_id or ""))
        previous_run = getattr(self._pw_worker, "run_id", None)
        self._pw_worker.run_id = str(req.run_id or "")
        try:
            return self._execute_body(req)
        finally:
            self._pw_worker.run_id = previous_run
            if retired.is_set():
                try:
                    get_hub().release_this_lane()
                except Exception as exc:
                    SLog.w(TAG, f"超时后关闭本线程浏览器: {exc}")

    def _retire_playwright_shard(self, req: P.Execute, owner: int | None) -> None:
        """卡住的 worker 不能杀。换一个空池，后续指令不再排在它后面空等。"""
        from mino_scout.playwright_hub import get_hub

        rid = str(req.run_id or "").strip()
        n = len(self._pw_shards) or 1
        idx = (hash(rid) & 0x7FFFFFFF) % n if rid else 0
        with self._pw_lock:
            self._pw_shards[idx] = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=f"scout-pw-{idx}"
            )
        if owner:
            get_hub().retire_owner(int(owner))
        else:
            get_hub().retire_lane(rid)
        SLog.w(
            TAG,
            f"playwright shard {idx} 超时后已换新线程 run={rid or '-'} cap={req.capability_id}",
        )

    def _playwright_executor(self, req: P.Execute) -> ThreadPoolExecutor:
        rid = str(req.run_id or "").strip()
        with self._pw_lock:
            n = len(self._pw_shards) or 1
            idx = (hash(rid) & 0x7FFFFFFF) % n if rid else 0
            return self._pw_shards[idx]

    def _execute_body(self, req: P.Execute) -> EventResult:
        token_run = current_run_id.set(req.run_id)
        token_step = current_step_idx.set(req.step_idx)
        if req.run_id:
            self._remember_run(req.run_id)
        wl_sn = ""
        if req.run_id and req.step_idx >= 0:
            cap_preview = _canonical_cap(req.capability_id)
            if cap_preview not in _NODE_COMMANDS and not cap_preview.startswith("node."):
                wl_sn = str(req.sn or req.device_id or "").strip()
                if wl_sn:
                    with self._lock:
                        self._device_workload[wl_sn] = P.DeviceWorkload(
                            sn=wl_sn,
                            run_id=req.run_id,
                            step_idx=req.step_idx,
                            capability_id=cap_preview,
                        )
        try:
            return self._dispatch(req)
        except Exception as exc:
            SLog.e(TAG, f"execute 内部异常 cap={req.capability_id}: {type(exc).__name__}")
            return make_event_result(
                _event_from_execute(req),
                status=EventStatus.FAIL,
                executor_used="core",
                started_at=now_iso(),
                elapsed_ms=0,
                summary=f"scout 内部错误：{type(exc).__name__}",
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            if wl_sn:
                with self._lock:
                    cur = self._device_workload.get(wl_sn)
                    if (
                        cur is not None
                        and cur.run_id == req.run_id
                        and cur.step_idx == req.step_idx
                    ):
                        self._device_workload.pop(wl_sn, None)
            current_run_id.reset(token_run)
            current_step_idx.reset(token_step)

    def _binding_rejection(self, req: P.Execute):
        reason = self.binding_guard.validate(req)
        if not reason:
            return None
        return make_event_result(_event_from_execute(req), status=EventStatus.BLOCKED,
            executor_used="core", started_at=now_iso(), elapsed_ms=0, summary=reason,
            raw_response={"execution": {"execution_started": "no", "phase": "binding", "reason_code": reason},
                "execution_binding": {"binding_id": req.binding_id, "binding_revision": req.binding_revision,
                    "operation_ref": req.operation_ref}})

    def _dispatch(self, req: P.Execute) -> EventResult:
        # Recheck after the executor queue, immediately before native dispatch.
        rejected = self._binding_rejection(req)
        if rejected is not None:
            return rejected
        cap = _canonical_cap(req.capability_id)
        req = replace(req, capability_id=cap)

        if cap == "cancel_run":
            return self._dispatch_cancel(req)
        if cap.startswith("plugin."):
            from mino_scout.plugins.service import handle_plugin_cap

            return handle_plugin_cap(self, req, cap)
        if cap.startswith("node."):
            if cap in _NODE_COMMANDS:
                return self._dispatch_node(req, cap)
            cmd = cap.split(".", 1)[-1] or cap
            msg = f"不支持的节点指令 {cmd}"
            return make_event_result(
                _event_from_execute(req),
                status=EventStatus.FAIL,
                executor_used="core",
                started_at=now_iso(),
                elapsed_ms=0,
                summary=msg,
                error=msg,
                raw_response={"command": cmd},
            )
        if req.platform in {"mac", "win"} or req.sn.startswith(("mac:", "win:")):
            import platform
            from mino_scout.platform_contract import native_request_platform
            try:
                native_request_platform({"platform": req.platform, "sn": req.sn, "device_hint": req.device_hint},
                                        node_id=self.node_id, host_system=platform.system())
            except ValueError as exc:
                return make_event_result(_event_from_execute(req), status=EventStatus.BLOCKED, executor_used="core",
                    started_at=now_iso(), elapsed_ms=0, error=str(exc), summary="原生平台或设备身份不匹配",
                    raw_response={"execution": {"execution_started": "no", "phase": "resolve", "reason_code": "native_identity_conflict"}})
        if req.sn.startswith("desktop:") and (req.sn != f"desktop:{self.node_id}:console" or req.platform != "desktop"):
            return make_event_result(_event_from_execute(req), status=EventStatus.BLOCKED, executor_used="core", started_at=now_iso(), elapsed_ms=0, error="Desktop 目标或平台不匹配", summary="Desktop 目标或平台不匹配")
        if req.platform in {"desktop", "mac", "win"} and cap == "hierarchy":
            return make_event_result(_event_from_execute(req), status=EventStatus.BLOCKED, executor_used="desktop", started_at=now_iso(), elapsed_ms=0, error="Desktop 首版仅开放 visual 执行", summary="Desktop 首版仅开放 visual 执行")
        if cap == "probe":
            return self._dispatch_probe(req)
        if cap == "screenshot":
            return self._dispatch_screenshot(req)
        if cap == "hierarchy":
            return self._dispatch_hierarchy(req)

        try:
            device = _device_from_execute(req, node_id=self.node_id)
        except ValueError as exc:
            return make_event_result(_event_from_execute(req), status=EventStatus.BLOCKED, executor_used="core",
                started_at=now_iso(), elapsed_ms=0, error=str(exc), summary="执行平台合同无效",
                raw_response={"execution":{"execution_started":"no","phase":"admission","reason_code":"platform_contract_invalid"}})
        event = _event_from_execute(req)
        return self.router.dispatch(
            event,
            device,
            run_id=req.run_id,
            step_idx=req.step_idx,
            capture_prefer=_prefer_for(device),
            shared={"write_stopped": lambda: self._requests.stopped(req.run_id)},
        )

    def _dispatch_cancel(self, req: P.Execute) -> EventResult:
        dropped = self.cancel_run(req.run_id)
        return make_event_result(
            _event_from_execute(req),
            status=EventStatus.PASS,
            executor_used="core",
            started_at=now_iso(),
            elapsed_ms=0,
            summary=f"已阻止 run 的后续写动作（清 {dropped} 条缓存）；底层停止需独立确认",
            raw_response={"execution": {"execution_started": "no", "phase": "cancel", "reason_code": "task_cancelled"},
                          "executor_stopped": False, "stop_confirmation_required": True},
        )

    def _dispatch_node(self, req: P.Execute, cap: str) -> EventResult:
        cmd = _NODE_COMMANDS[cap]
        event = _event_from_execute(req)
        started = now_iso()
        if cmd == "sleep":
            ok, summary = self.enter_sleep()
            return make_event_result(
                event,
                status=EventStatus.PASS if ok else EventStatus.FAIL,
                executor_used="core",
                started_at=started,
                elapsed_ms=0,
                summary=summary,
                error="" if ok else summary,
                raw_response={"command": cmd, "mode": "asleep" if ok else "running"},
            )
        if cmd == "wake":
            ok, summary = self.enter_wake()
            return make_event_result(
                event,
                status=EventStatus.PASS if ok else EventStatus.FAIL,
                executor_used="core",
                started_at=started,
                elapsed_ms=0,
                summary=summary,
                error="" if ok else summary,
                raw_response={"command": cmd, "mode": "running"},
            )
        if cmd in ("plugin_install", "plugin_remove", "plugin_config"):
            from mino_scout.plugins.service import handle_node_plugin

            return handle_node_plugin(self, req, cmd, event, started)
        if cmd == "log_tail":
            from mino_scout.local_logs import tail_logs

            lines = int((req.params or {}).get("lines") or 200)
            body = tail_logs(lines=lines)
            summary = f"日志 tail（stdout {len(body.get('files', {}).get('stdout', {}).get('tail') or [])} 行）"
            return make_event_result(
                event,
                status=EventStatus.PASS,
                executor_used="core",
                started_at=started,
                elapsed_ms=0,
                summary=summary,
                raw_response={"command": cmd, "logs": body},
            )
        extra: dict[str, Any] = {"command": cmd, "_scout_shutdown": True}
        if cmd == "restart":
            extra["_scout_reexec"] = True
        return make_event_result(
            event, status=EventStatus.PASS, executor_used="core",
            started_at=started, elapsed_ms=0, summary=f"已接受 {cmd}",
            raw_response=extra,
        )

    def _dispatch_probe(self, req: P.Execute) -> EventResult:
        t0 = time.time()
        execs, devices = self.manifest()
        sn = str(req.sn or req.device_id or "").strip()
        channels: dict[str, str] = {}
        for d in devices:
            if d.sn == sn:
                channels = dict(d.channels)
                break
        elapsed = int((time.time() - t0) * 1000)
        summary = f"probe {sn}: {channels or '未发现该设备'}"
        payload = {
            "channels": channels,
            "contracts": __import__("mino_scout.web_v5", fromlist=["CONTRACTS"]).CONTRACTS,
            "executors": {e.id: e.available for e in execs},
            "extra": {"channels": channels, "executors": {e.id: e.available for e in execs}},
        }
        return make_event_result(
            _event_from_execute(req),
            status=EventStatus.PASS,
            executor_used="core",
            started_at=now_iso(),
            elapsed_ms=elapsed,
            summary=summary,
            raw_response=payload,
        )

    def _dispatch_screenshot(self, req: P.Execute) -> EventResult:
        device = _device_from_execute(req, node_id=self.node_id)
        params = dict(req.params or {})
        ratio = params.get("compress_ratio")
        compress = float(ratio if ratio is not None else (1.0 if device.platform in {"desktop", "mac", "win"} else 2.0))
        shot = SCREEN.capture(
            device,
            timeout_sec=float(req.timeout_sec or 15.0),
            compress_ratio=compress,
            screenshot_params=params,
        )
        status, fields = _screen_to_result(shot)
        return make_event_result(
            _event_from_execute(req),
            status=status,
            executor_used=str(fields.get("source") or "screen"),
            started_at=now_iso(),
            elapsed_ms=int(fields.get("elapsed_ms") or 0),
            summary=str(fields.get("summary") or ""),
            error=str(fields.get("error") or ""),
            raw_response=fields,
        )

    def _dispatch_hierarchy(self, req: P.Execute) -> EventResult:
        device = _device_from_execute(req, node_id=self.node_id)
        status, fields = self._hierarchy_fields(
            device,
            run_id=str(req.run_id or ""),
            hierarchy_params=dict(req.params or {}),
        )
        extra = dict(fields.get("extra") or {})
        raw = dict(fields)
        if extra:
            raw.update(extra)
        return make_event_result(
            _event_from_execute(req),
            status=status,
            executor_used=str(fields.get("source") or "core"),
            started_at=now_iso(),
            elapsed_ms=int(fields.get("elapsed_ms") or 0),
            summary=str(fields.get("summary") or ""),
            error=str(fields.get("error") or ""),
            raw_response=raw,
        )

    def observe(
        self,
        *,
        run_id: str,
        sn: str,
        kind: str = "screenshot",
        prefer: Optional[list[str]] = None,
        force_fresh: bool = True,
        timeout_sec: float = 15.0,
        compress_ratio: float = 2.0,
        device_id: str = "",
        platform: str = "",
    ) -> tuple[EventStatus, dict[str, Any]]:
        """本地薄包装。内部转 Execute，step_idx=-1，不做幂等。"""
        cap = P.OBSERVE_CAPS.get(kind, kind)
        ev = self.execute(
            P.Execute(
                run_id=run_id,
                step_idx=-1,
                sn=sn,
                capability_id=cap,
                params={"force_fresh": force_fresh, "compress_ratio": compress_ratio},
                executor_order=list(prefer or []),
                timeout_sec=float(timeout_sec or 15.0),
                device_id=device_id or sn,
                platform=platform,
            )
        )
        return ev.status, _event_result_to_observe_fields(ev)

    def _hierarchy_fields(
        self,
        device: DeviceRef,
        *,
        run_id: str = "",
        hierarchy_params: dict[str, Any] | None = None,
    ) -> tuple[EventStatus, dict[str, Any]]:
        from mino_scout import hierarchy as H

        if device.is_web:
            from mino_scout.dom_hierarchy import dump_dom_nodes

            dump = dump_dom_nodes(
                sn=str(device.sn or ""),
                run_id=str(run_id or ""),
                params=dict(hierarchy_params or {}),
            )
            if not dump.ok:
                return EventStatus.FAIL, {
                    "error": dump.error or "web DOM 采集失败",
                    "source": "playwright",
                }
            nodes = list(dump.nodes or [])
            extra: dict[str, Any] = {"nodes": nodes, "observation": dict(dump.observation or {})}
            if isinstance(dump.web_focus, dict) and dump.web_focus:
                extra["web_focus"] = dump.web_focus
            if dump.aria_snapshot:
                extra["aria_snapshot"] = dump.aria_snapshot
            if dump.tabs:
                extra["tabs"] = dump.tabs
            return EventStatus.PASS, {
                "source": "playwright",
                "hierarchy_format": "accessibility_json",
                "summary": f"DOM 层级 {len(nodes)} 节点",
                "extra": extra,
                "nodes": nodes,
                "web_focus": dump.web_focus or {},
                "aria_snapshot": dump.aria_snapshot or "",
                "observation": dict(dump.observation or {}),
                "tabs": dump.tabs or [],
                "elapsed_ms": int(dump.elapsed_ms or 0),
            }
        if not device.adb_serial or device.adb_serial.startswith("claw-"):
            return EventStatus.FAIL, {
                "error": "hierarchy 目前只支持 adb 通道（remote/ios_wda 待 E3 搬迁）",
                "source": "adb",
            }
        dump = H.dump_ui_nodes(device.adb_serial, force_fresh=True)
        if not dump.ok:
            return EventStatus.FAIL, {"error": dump.error, "source": "adb"}
        import uuid
        semantic = [n for n in dump.nodes if n.visible and n.area > 0 and
                    (n.text or n.content_desc or n.resource_id or n.hint or n.clickable or n.editable or n.checkable)]
        nodes = [n.to_brief() for n in semantic[:320]]
        observation = {"version": 1, "snapshot_id": "native:" + uuid.uuid4().hex,
                       "source": "adb", "scope": "visible_tree", "node_count": len(nodes),
                       "node_limit": 320, "truncated": len(semantic) > 320,
                       "missing": ["node_limit"] if len(semantic) > 320 else []}
        return EventStatus.PASS, {
            "source": "adb",
            "summary": f"UI 层级 {len(dump)} 节点",
            "extra": {"nodes": nodes, "observation": observation},
            "nodes": nodes,
            "observation": observation,
            "elapsed_ms": getattr(dump, "elapsed_ms", 0),
        }

    # ---------------- PROBE / manifest ----------------

    def manifest(self) -> tuple[list[P.ExecutorManifest], list[P.DeviceManifest]]:
        """探一遍本机，产出 REGISTER 要上报的东西。

        **provides 由各 executor 自报**，core 不硬编码能力表 —— 加一个 executor
        不该回来改这里（docs/EXECUTORS.md §4）。
        """
        return self.executor_manifest(), self.discover_devices()

    def executor_manifest(self) -> list[P.ExecutorManifest]:
        """Full current executor snapshot; no duplicate device discovery."""
        execs: list[P.ExecutorManifest] = []
        for ex_id, ex in sorted(self.executors.items()):
            try:
                available, provides, reason = _probe_executor(ex_id, ex)
            except Exception as exc:
                available, provides, reason = False, [], f"probe 抛异常: {exc!r}"
            execs.append(
                P.ExecutorManifest(
                    id=ex_id, available=available,
                    provides=list(provides) if available else [],
                    reason="" if available else reason,
                )
            )
        from mino_scout.plugins.service import executor_manifest as plugin_executor_manifest
        execs.append(plugin_executor_manifest(ready=self._plugins_ready))
        return execs

    def discover_devices(self) -> list[P.DeviceManifest]:
        """只重探设备，不重跑 executor probe。心跳热插拔走这里。"""
        from mino_scout.config import android_device_allowed
        return [d for d in _discover_devices(self.executors, scout_id=self.node_id)
                if d.platform != "android" or android_device_allowed(d.sn)]

    def ensure_android_adb_keyboard_on_startup(self) -> list[dict[str, Any]]:
        """进程启动后（REGISTER 前）检查已连接的 Android 设备默认输入法。"""
        if "adb" not in self.executors:
            return []
        serials = connected_android_adb_serials(self.discover_devices())
        return self.ensure_adb_keyboard_for_serials(serials, reason="startup")

    def ensure_adb_keyboard_for_serials(
        self,
        serials: list[str],
        *,
        reason: str = "",
    ) -> list[dict[str, Any]]:
        from mino_scout.adb_ime import ensure_adb_keyboard

        if "adb" not in self.executors:
            return []
        outcomes: list[dict[str, Any]] = []
        tag = f"{reason} " if reason else ""
        now = time.time()
        for serial in serials:
            sid = str(serial or "").strip()
            from mino_scout.config import android_device_allowed
            if not sid or not android_device_allowed(sid):
                continue
            self._adb_ime_last_check[sid] = now
            outcome = ensure_adb_keyboard(sid)
            outcomes.append(outcome)
            if not outcome.get("ok"):
                SLog.w(TAG, f"{tag}输入法检查 {sid}: {outcome.get('error')}")
        return outcomes

    def ensure_adb_keyboard_drift_check(self, devices: list[P.DeviceManifest]) -> list[dict[str, Any]]:
        """adb 仍在线但 default IME 被改走时拉回 ADB Keyboard（节流）。"""
        from mino_scout.adb_ime import ADB_KEYBOARD_IME_ID, current_default_ime, ensure_adb_keyboard

        if "adb" not in self.executors:
            return []
        now = time.time()
        outcomes: list[dict[str, Any]] = []
        for sid in connected_android_adb_serials(devices):
            from mino_scout.config import android_device_allowed
            if not android_device_allowed(sid):
                continue
            last = self._adb_ime_last_check.get(sid, 0.0)
            if now - last < _ADB_IME_DRIFT_CHECK_SEC:
                continue
            self._adb_ime_last_check[sid] = now
            try:
                current = current_default_ime(sid)
            except Exception as exc:
                SLog.w(TAG, f"读取默认输入法 {sid}: {exc}")
                continue
            if current == ADB_KEYBOARD_IME_ID:
                continue
            outcome = ensure_adb_keyboard(sid)
            outcomes.append(outcome)
            if outcome.get("ok"):
                SLog.i(TAG, f"心跳复检输入法 {sid}: {outcome.get('summary') or '已校正'}")
            elif not outcome.get("ok"):
                SLog.w(TAG, f"心跳复检输入法 {sid}: {outcome.get('error')}")
        return outcomes

    # ---------------- CANCEL ----------------

    def cancel_run(self, run_id: str) -> int:
        """丢掉该 run 的幂等缓存并标记不再继续。

        协议 §4.8：语义是"不再继续"，**不是回滚** —— 已经发给设备的动作撤不回来。
        """
        self._requests.stop(run_id, "task_cancelled", {}, terminal=True)
        if self._requests.active(run_id):
            SLog.w(TAG, f"cancel run={run_id}：仍有底层动作运行，停止未确认，保留资源")
            return 0
        self._web_v5.close(run_id, time.monotonic() + 2)
        with self._lock:
            keys = [k for k in self._done if k[0] == run_id]
            for k in keys:
                self._done.pop(k, None)
            self._active_runs.discard(run_id)
            self._run_seen.pop(run_id, None)
            if run_id:
                self._cancelled_runs[str(run_id)] = time.time()
        SLog.i(TAG, f"cancel run={run_id}，清掉 {len(keys)} 条幂等缓存")
        from mino_scout.desktop import get_desktop_runtime
        get_desktop_runtime().cancel(run_id)
        rid = str(run_id or "").strip()
        if rid:
            fake = P.Execute(run_id=rid, step_idx=-1, sn="", capability_id="cancel_run", platform="web")
            try:
                if getattr(self._pw_worker, "run_id", None) == rid:
                    # Already on this run's single-worker lane. Submitting to
                    # that same lane and waiting blocks the cleanup behind us.
                    _release_playwright_run(rid)
                else:
                    self._playwright_executor(fake).submit(lambda: _release_playwright_run(rid)).result(timeout=12.0)
            except Exception as exc:
                SLog.w(TAG, f"cancel run playwright release: {exc!r}")
        return len(keys)

    def shutdown(self) -> None:
        """进程退出：必须在 playwright 那条线程上停 Chromium。

        Hub 用 thread-local，主线程调 shutdown_thread 关不到浏览器，
        进程一死就会刷 TargetClosedError。
        """
        for lane in self._web_v5.lanes:
            if lane["run"]:
                self._web_v5.close(lane["run"], time.monotonic() + 2)
        try:
            with self._pw_lock:
                shards = list(self._pw_shards)
            for shard in shards:
                shard.submit(_stop_playwright_hub).result(timeout=8.0)
        except Exception as exc:
            SLog.w(TAG, f"playwright 退出清理: {exc}")
        from mino_scout.desktop import get_desktop_runtime
        get_desktop_runtime().shutdown()

    # ---------------- 状态 ----------------

    def host_mode(self) -> str:
        with self._lock:
            return "asleep" if self._asleep else "running"

    def enter_sleep(self) -> tuple[bool, str]:
        """放开睡眠抑制并关掉 Chromium。进程和心跳留着，网页才能再唤醒。"""
        with self._lock:
            if self._active_runs:
                return False, "有任务在跑，先停任务再休眠"
            if self._asleep:
                return True, "已休眠"
            self._asleep = True
        from mino_scout.power import HOLDER_NEXUS, get_guard

        get_guard().release(HOLDER_NEXUS)
        threading.Thread(
            target=self._close_browsers_for_sleep,
            name="scout-sleep-browser",
            daemon=True,
        ).start()
        return True, "已休眠"

    def _close_browsers_for_sleep(self) -> None:
        """关浏览器不堵住这条指令。堵住的话网页要等关完才改状态，心跳也会显得断了。"""
        try:
            self.shutdown()
        except Exception as exc:
            SLog.w(TAG, f"休眠时关闭浏览器: {exc}")

    def enter_wake(self) -> tuple[bool, str]:
        """重新抑制睡眠。浏览器等下一次任务再开。"""
        with self._lock:
            self._asleep = False
        from mino_scout.power import HOLDER_NEXUS, get_guard

        get_guard().acquire(HOLDER_NEXUS)
        return True, "已启动"

    def heartbeat(self) -> P.Heartbeat:
        from mino_scout.app_version import report_scout_version

        self._evict_idle_runs()
        with self._lock:
            active = sorted(self._active_runs)
            workload = sorted(
                self._device_workload.values(),
                key=lambda w: (w.sn, w.run_id, w.step_idx),
            )
        capacity = self._web_v5.capacity()
        workload.append(P.DeviceWorkload(sn=f"web{self.node_id}", **capacity))
        from mino_scout.input_contract import runtime_input_capabilities
        from mino_scout.plugins.state import status_list
        from mino_scout.config import development

        runtime_caps = runtime_input_capabilities()
        executors = self.executor_manifest()
        epoch = self.binding_guard.announce(executors, runtime_caps)
        return P.Heartbeat(
            node_id=self.node_id,
            node_connection_generation=self.binding_guard.connection_generation,
            **epoch,
            platform_contract_version=1,
            executor_snapshot_version=1,
            executors=executors,
            uptime_sec=int(time.time() - self._started),
            busy=bool(active) or getattr(self, "_development_busy", False),
            active_runs=active,
            device_workload=workload,
            scout_version=report_scout_version(),
            plugins=status_list(self.node_id) if self._plugins_ready else [],
            plugins_ready=self._plugins_ready,
            plugin_warning=self._plugin_warning,
            runtime_environment="development" if development() else "production",
            runtime_capabilities=runtime_caps,
        )

    def _remember_run(self, run_id: str) -> None:
        """记入在途。取消之后的指令不再记，避免休眠一直看到旧任务。"""
        rid = str(run_id or "").strip()
        if not rid:
            return
        now = time.time()
        with self._lock:
            self._evict_cancelled(now)
            if rid in self._cancelled_runs:
                return
            self._active_runs.add(rid)
            self._run_seen[rid] = now

    def _evict_cancelled(self, now: float | None = None) -> None:
        """调用方已持有 self._lock。"""
        cutoff = (now if now is not None else time.time()) - _IDEMPOTENT_TTL_SEC
        for rid, ts in list(self._cancelled_runs.items()):
            if ts < cutoff:
                self._cancelled_runs.pop(rid, None)

    def _evict_idle_runs(self) -> None:
        """Nexus 不会发 RUN_DONE。超过幂等 TTL 没再来 EXECUTE 的 run 视为结束。"""
        cutoff = time.time() - _IDEMPOTENT_TTL_SEC
        with self._lock:
            self._evict_cancelled()
            for rid, ts in list(self._run_seen.items()):
                if ts < cutoff:
                    self._active_runs.discard(rid)
                    self._run_seen.pop(rid, None)

    # ---------------- 幂等缓存 ----------------

    def _get_cached(self, key: tuple[str, int]) -> Optional[EventResult]:
        if key[1] < 0:  # 截图/探活/框架事件每次都要发生
            return None
        with self._lock:
            self._evict_expired()
            hit = self._done.get(key)
        return hit[1] if hit else None

    def _put_cached(self, key: tuple[str, int], result: EventResult) -> None:
        if key[1] < 0:
            return
        with self._lock:
            self._done[key] = (time.time(), result)
            self._evict_expired()

    def _evict_expired(self) -> None:
        cutoff = time.time() - _IDEMPOTENT_TTL_SEC
        for k in [k for k, (ts, _) in self._done.items() if ts < cutoff]:
            self._done.pop(k, None)


def _release_playwright_run(run_id: str) -> None:
    from mino_scout.playwright_hub import get_hub

    get_hub().release_run(run_id)


# ---------------- 载荷 ↔ 内部模型 ----------------


def _canonical_cap(capability_id: str) -> str:
    return str(capability_id or "").strip()


def _normalize_platform(raw: str) -> str:
    from mino_scout.platform_contract import target_platform
    return target_platform(raw)


def _event_from_execute(req: P.Execute) -> PlanEvent:
    return PlanEvent(
        seq=req.step_idx,
        capability_id=req.capability_id,
        params=dict(req.params or {}),
        executor_order=list(req.executor_order or []),
        low_level=dict(req.low_level or {}),
        selected_impl=dict(req.selected_impl or {}),
        device_hint=dict(req.device_hint or {}),
    )


def _device_from_execute(req: P.Execute, *, node_id: str = "") -> DeviceRef:
    hint = dict(req.device_hint or {})
    if req.platform in {"mac", "win"} or str(req.sn).startswith(("mac:", "win:")):
        import platform as host_platform
        from mino_scout.platform_contract import native_request_platform
        native_request_platform({"platform": req.platform, "sn": req.sn, "device_hint": hint},
                                node_id=node_id, host_system=host_platform.system())
    from mino_scout.platform_contract import execute_platform
    platform = execute_platform({"platform":req.platform,"device_hint":hint})
    sn = str(req.sn or req.device_id or hint.get("sn") or hint.get("device_id") or "").strip()
    device_id = str(req.device_id or sn).strip()

    if platform == "web" and sn != f"web{node_id}":
        raise ValueError("web_identity_conflict")

    adb_serial = str(hint.get("adb_serial") or "")
    if not adb_serial and platform == "android":
        adb_serial = _adb_serial_guess(device_id or sn)

    extra = {k: v for k, v in hint.items()
             if k not in {"platform", "adb_serial", "udid", "password", "model", "sn", "device_id"}}
    if req.run_id:
        extra["run_id"] = str(req.run_id)

    return DeviceRef(
        sn=sn or device_id,
        platform=platform,
        adb_serial=adb_serial,
        udid=str(hint.get("udid") or (device_id if platform == "ios" else "")),
        password=str(hint.get("password") or ""),
        model=str(hint.get("model") or ""),
        extra=extra,
    )





def _timeout_result(req: P.Execute, timeout: float, *, lane_reset: bool = False) -> EventResult:
    if lane_reset:
        detail = "（原 Playwright 线程可能仍在运行，本 run 禁止后续写入）"
    else:
        detail = "（Python 线程无法强杀，底层 adb 可能仍在跑）"
    return make_event_result(
        _event_from_execute(req),
        status=EventStatus.FAIL,
        executor_used="core",
        started_at=now_iso(),
        elapsed_ms=int(timeout * 1000),
        summary=f"timeout after {timeout}s",
        error=f"动作超过 timeout_sec={timeout}s，已停止等待，底层停止未确认{detail}",
        raw_response={"timeout": True, "timeout_sec": timeout, "lane_reset": lane_reset,
            "execution": {"execution_started": "unknown", "phase": "wait", "reason_code": "write_timeout_unknown"},
            "stop_confirmed": False},
    )


def _screen_to_result(shot: CapturedScreen) -> tuple[EventStatus, dict[str, Any]]:
    if not shot.has_image():
        return EventStatus.FAIL, {"error": shot.error, "source": shot.source,
                                  "elapsed_ms": shot.elapsed_ms}
    return EventStatus.PASS, {
        "source": shot.source,
        "summary": f"{shot.source} {shot.width}x{shot.height}",
        "image_base64": shot.image_base64,
        "image_mime": shot.image_mime,
        "width": shot.width,
        "height": shot.height,
        "elapsed_ms": shot.elapsed_ms,
        "extra": dict(shot.remote_detail or {}),
    }


def _event_result_to_observe_fields(ev: EventResult) -> dict[str, Any]:
    raw = dict(ev.raw_response or {})
    return {
        "summary": ev.summary or str(raw.get("summary") or ""),
        "error": ev.error or str(raw.get("error") or ""),
        "source": ev.executor_used or str(raw.get("source") or ""),
        "elapsed_ms": ev.elapsed_ms,
        "image_base64": str(raw.get("image_base64") or ""),
        "image_mime": str(raw.get("image_mime") or ""),
        "width": int(raw.get("width") or 0),
        "height": int(raw.get("height") or 0),
        "extra": dict(raw.get("extra") or {}),
    }


# ---------------- 小工具 ----------------


def _stop_playwright_hub() -> None:
    from mino_scout.playwright_hub import get_hub

    get_hub().shutdown_thread()





def _adb_serial_guess(sn: str) -> str:
    """claw-* 是 ClawNode 伪 serial，adb 认不出来；web 槽不是设备。

    真实映射由 Nexus 在 `EXECUTE.device_hint.adb_serial` 里给。这里只是
    没带 hint 时的兜底。
    """
    from mino_scout.playwright_hub import is_web_slot

    if not sn or sn.startswith("claw-") or is_web_slot(sn):
        return ""
    return sn


def _prefer_for(device: DeviceRef) -> tuple[str, ...]:
    return tuple(_compatible_executors(device))


def _compatible_executors(device: DeviceRef) -> frozenset[str]:
    return device.compatible_executors


def _use_playwright_thread(req: P.Execute) -> bool:
    plat = str(req.platform or "").strip().lower()
    hint = req.device_hint if isinstance(getattr(req, "device_hint", None), dict) else {}
    hint_plat = str(hint.get("platform") or "").strip().lower()
    if plat in ("web", "browser", "playwright") or hint_plat in ("web", "browser", "playwright"):
        return True
    from mino_scout.playwright_hub import is_web_slot

    return is_web_slot(
        str(req.sn or req.device_id or hint.get("sn") or ""),
        plat or hint_plat,
    )


def _probe_executor(ex_id: str, ex: Executor) -> tuple[bool, list[str], str]:
    """连通性探测 + provides 上报。

    每个 executor 自己声明 `provides` 与 `probe()`；没声明的按"可用但不报能力"处理
    并记 warn —— 静默上报空能力会让 Nexus 的菜单莫名变空。
    """
    provides = list(getattr(ex, "provides", []) or [])
    probe = getattr(ex, "probe", None)
    if callable(probe):
        ok, reason = probe()
    else:
        ok, reason = True, ""
        SLog.w(TAG, f"executor {ex_id} 没有 probe()，默认按可用上报")
    if not provides:
        SLog.w(TAG, f"executor {ex_id} 没有声明 provides，Nexus 侧菜单会缺这一块")
    return bool(ok), provides, reason


def _discover_devices(executors: dict[str, Executor], *, scout_id: str) -> list[P.DeviceManifest]:
    """当前只发现 adb 设备与 web 虚拟槽。

    remote(ClawNode) / iOS 的发现依赖 EngineFactory 与 clawnode/ 模块（E3），未搬。
    """
    from mino_scout.desktop import get_desktop_runtime
    native = get_desktop_runtime().manifest(scout_id)
    out: list[P.DeviceManifest] = [native] if native is not None else []
    if "adb" in executors:
        out.extend(_discover_adb_devices())
    if "playwright" in executors:
        from mino_scout.playwright_hub import PROBE_OK_STATE, probe_playwright, web_slot_sn

        # probe_playwright() 的成功态是 "available"（见 playwright_hub），
        # 映射到协议的 channels 取值 connected/disconnected。
        state, _detail = probe_playwright()
        out.append(
            P.DeviceManifest(
                sn=web_slot_sn(scout_id), platform="web",
                channels={"playwright": "connected" if state == PROBE_OK_STATE else "disconnected"},
            )
        )
    return out


def _discover_adb_devices() -> list[P.DeviceManifest]:
    import subprocess

    try:
        proc = subprocess.run(["adb", "devices", "-l"], capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        SLog.w(TAG, f"adb devices 失败: {exc}")
        return []
    out: list[P.DeviceManifest] = []
    for line in (proc.stdout or "").splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        serial, state = parts[0], (parts[1] if len(parts) > 1 else "")
        model = ""
        for p in parts[2:]:
            if p.startswith("model:"):
                model = p.split(":", 1)[1]
        out.append(
            P.DeviceManifest(
                sn=serial, platform="android", model=model,
                channels={"adb": {"device": "connected",
                                  "unauthorized": "unauthorized",
                                  "offline": "disconnected"}.get(state, "disconnected")},
            )
        )
    return out
