"""ScoutCore：纯本地、零网络、可单测。

`transport/` 负责收发协议消息，把载荷交给这里；`core` 只认
`Execute → EventResult` / `Observe → CapturedScreen`，**内部不出现任何连接形态的分支**
（CLAUDE.md §2.2）。

三个对外方法对应协议里三类请求：
    execute(Execute)   ← EXECUTE
    observe(Observe)   ← OBSERVE
    manifest()         → REGISTER 的 executors[] / devices[]，也用于 mino-scout probe

幂等（协议 §4.5 / CONVENTIONS.md §5）也在这一层：`(run_id, step_idx)` 是唯一键，
重复的 EXECUTE 直接回缓存，不重新操作设备。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Optional

from mino_scout import protocol as P
from mino_scout import screen as SCREEN
from mino_scout.executors.base import DeviceRef, Executor
from mino_scout.log import SLog, current_run_id, current_step_idx
from mino_scout.router import Router
from mino_scout.schemas import CapturedScreen, EventResult, EventStatus, PlanEvent

TAG = "ScoutCore"

SCOUT_VERSION = "0.1.4"

# 幂等缓存保留时长。CONVENTIONS.md §5：该 run 结束或 10 分钟，取先到者。
_IDEMPOTENT_TTL_SEC = 600.0


class ScoutCore:
    def __init__(self, executors: dict[str, Executor], *, node_id: str = ""):
        from mino_scout.config import resolve_scout_id, sanitize_scout_id

        # scout_id == Register.node_id. One alphanumeric id; do not add a second.
        self.node_id = sanitize_scout_id(node_id) or resolve_scout_id()
        self.scout_id = self.node_id
        self.executors = executors
        self.router = Router(executors)
        self._started = time.time()
        self._lock = threading.Lock()
        # (run_id, step_idx) -> (完成时刻, EventResult)
        self._done: dict[tuple[str, int], tuple[float, EventResult]] = {}
        self._active_runs: set[str] = set()
        self._run_seen: dict[str, float] = {}

    # ---------------- EXECUTE ----------------

    def execute(self, req: P.Execute) -> EventResult:
        key = (req.run_id, req.step_idx)
        cached = self._get_cached(key)
        if cached is not None:
            SLog.i(TAG, f"幂等命中 {key}，回缓存不重新执行（status={cached.status.value}）")
            return cached

        token_run = current_run_id.set(req.run_id)
        token_step = current_step_idx.set(req.step_idx)
        with self._lock:
            self._active_runs.add(req.run_id)
            self._run_seen[req.run_id] = time.time()
        from mino_scout.power import get_guard

        get_guard().acquire(req.run_id)
        try:
            event = _event_from_execute(req)
            device = _device_from_execute(req)
            result = self.router.dispatch(
                event,
                device,
                run_id=req.run_id,
                step_idx=req.step_idx,
                capture_prefer=_prefer_for(device),
            )
        finally:
            current_run_id.reset(token_run)
            current_step_idx.reset(token_step)

        self._put_cached(key, result)
        return result

    # ---------------- OBSERVE ----------------

    def observe(self, req: P.Observe) -> tuple[EventStatus, dict[str, Any]]:
        """返回 (status, RESULT 的补充字段)。

        `OBSERVE` 没有 step_idx，也**不做幂等** —— 它本来就是"再看一眼"。
        """
        token = current_run_id.set(req.run_id)
        if req.run_id:
            with self._lock:
                self._active_runs.add(req.run_id)
                self._run_seen[req.run_id] = time.time()
            from mino_scout.power import get_guard

            get_guard().acquire(req.run_id)
        try:
            device = DeviceRef(sn=req.sn, adb_serial=_adb_serial_guess(req.sn))
            kind = req.kind if isinstance(req.kind, P.ObserveKind) else P.ObserveKind(req.kind)

            if kind is P.ObserveKind.SCREENSHOT:
                shot = SCREEN.capture(
                    device,
                    prefer=tuple(req.prefer or ("adb", "remote")),
                    timeout_sec=req.timeout_sec,
                    compress_ratio=req.compress_ratio,
                )
                return _screen_to_result(shot)

            if kind is P.ObserveKind.HIERARCHY:
                return self._observe_hierarchy(device, req)

            if kind in (P.ObserveKind.APP_VERSION, P.ObserveKind.FOREGROUND_APP):
                return self._observe_via_capability(device, req, kind)

            return EventStatus.FAIL, {"error": f"未知 OBSERVE kind: {kind}", "source": "core"}
        finally:
            current_run_id.reset(token)

    def _observe_hierarchy(self, device: DeviceRef, req: P.Observe):
        from mino_scout import hierarchy as H

        if not device.adb_serial or device.adb_serial.startswith("claw-"):
            return EventStatus.FAIL, {
                "error": "hierarchy 目前只支持 adb 通道（remote/ios_wda 待 E3 搬迁）",
                "source": "adb",
            }
        dump = H.dump_ui_nodes(device.adb_serial)
        if not dump.ok:
            return EventStatus.FAIL, {"error": dump.error, "source": "adb"}
        return EventStatus.PASS, {
            "source": "adb",
            "summary": f"UI 层级 {len(dump)} 节点",
            "extra": {"nodes": [n.to_brief() for n in dump.nodes]},
            "elapsed_ms": getattr(dump, "elapsed_ms", 0),
        }

    def _observe_via_capability(self, device: DeviceRef, req: P.Observe, kind: P.ObserveKind):
        """app_version / foreground_app 复用 executor 里已有的实现，不另写一套。"""
        cap = "get_app_version" if kind is P.ObserveKind.APP_VERSION else "get_foreground_app"
        order = [ex for ex in (req.prefer or ("adb",)) if ex in self.executors] or ["adb"]
        event = PlanEvent(seq=0, capability_id=cap, params={}, executor_order=order)
        res = self.router.dispatch(event, device, run_id=req.run_id, step_idx=-1)
        return res.status, {
            "source": res.executor_used,
            "summary": res.summary,
            "error": res.error,
            "extra": res.raw_response,
            "elapsed_ms": res.elapsed_ms,
        }

    # ---------------- PROBE / manifest ----------------

    def manifest(self) -> tuple[list[P.ExecutorManifest], list[P.DeviceManifest]]:
        """探一遍本机，产出 REGISTER 要上报的东西。

        **provides 由各 executor 自报**，core 不硬编码能力表 —— 加一个 executor
        不该回来改这里（docs/EXECUTORS.md §4）。
        """
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
        return execs, self.discover_devices()

    def discover_devices(self) -> list[P.DeviceManifest]:
        """只重探设备，不重跑 executor probe。心跳热插拔走这里。"""
        return _discover_devices(self.executors, scout_id=self.node_id)

    # ---------------- CANCEL ----------------

    def cancel_run(self, run_id: str) -> int:
        """丢掉该 run 的幂等缓存并标记不再继续。

        协议 §4.8：语义是"不再继续"，**不是回滚** —— 已经发给设备的动作撤不回来。
        """
        with self._lock:
            keys = [k for k in self._done if k[0] == run_id]
            for k in keys:
                self._done.pop(k, None)
            self._active_runs.discard(run_id)
            self._run_seen.pop(run_id, None)
        from mino_scout.power import get_guard

        get_guard().release(run_id)
        SLog.i(TAG, f"cancel run={run_id}，清掉 {len(keys)} 条幂等缓存")
        return len(keys)

    # ---------------- 状态 ----------------

    def heartbeat(self) -> P.Heartbeat:
        self._evict_idle_runs()
        with self._lock:
            active = sorted(self._active_runs)
        from mino_scout.power import get_guard

        get_guard().sync(active)
        return P.Heartbeat(
            node_id=self.node_id,
            uptime_sec=int(time.time() - self._started),
            busy=bool(active),
            active_runs=active,
        )

    def _evict_idle_runs(self) -> None:
        """Nexus 不会发 RUN_DONE。超过幂等 TTL 没再来 EXECUTE 的 run 视为结束。"""
        cutoff = time.time() - _IDEMPOTENT_TTL_SEC
        from mino_scout.power import get_guard

        dropped: list[str] = []
        with self._lock:
            for rid, ts in list(self._run_seen.items()):
                if ts < cutoff:
                    self._active_runs.discard(rid)
                    self._run_seen.pop(rid, None)
                    dropped.append(rid)
        for rid in dropped:
            get_guard().release(rid)

    # ---------------- 幂等缓存 ----------------

    def _get_cached(self, key: tuple[str, int]) -> Optional[EventResult]:
        if key[1] < 0:  # OBSERVE 之类不带 step_idx 的不做幂等
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


# ---------------- 载荷 ↔ 内部模型 ----------------


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


def _device_from_execute(req: P.Execute) -> DeviceRef:
    hint = dict(req.device_hint or {})
    return DeviceRef(
        sn=req.sn,
        platform=str(hint.get("platform") or _platform_guess(req.sn)),
        adb_serial=str(hint.get("adb_serial") or _adb_serial_guess(req.sn)),
        udid=str(hint.get("udid") or ""),
        password=str(hint.get("password") or ""),
        model=str(hint.get("model") or ""),
        extra={k: v for k, v in hint.items()
               if k not in {"platform", "adb_serial", "udid", "password", "model"}},
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
    }


# ---------------- 小工具 ----------------


def _platform_guess(sn: str) -> str:
    from mino_scout.playwright_hub import is_web_slot

    if is_web_slot(sn):
        return "web"
    return "android"


def _adb_serial_guess(sn: str) -> str:
    """claw-* 是 ClawNode 伪 serial，adb 认不出来；web 槽不是设备。

    真实映射由 Nexus 在 `EXECUTE.device_hint.adb_serial` 里给。这里只是 OBSERVE
    没带 hint 时的兜底。
    """
    from mino_scout.playwright_hub import is_web_slot

    if not sn or sn.startswith("claw-") or is_web_slot(sn):
        return ""
    return sn


def _prefer_for(device: DeviceRef) -> tuple[str, ...]:
    return ("playwright",) if device.is_web else ("adb", "remote")


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
    out: list[P.DeviceManifest] = []
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
