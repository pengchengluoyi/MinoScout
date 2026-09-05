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

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any, Optional

from mino_scout import protocol as P
from mino_scout import screen as SCREEN
from mino_scout.executors.base import DeviceRef, Executor, make_event_result, now_iso
from mino_scout.log import SLog, current_run_id, current_step_idx
from mino_scout.router import Router
from mino_scout.schemas import CapturedScreen, EventResult, EventStatus, PlanEvent

TAG = "ScoutCore"

SCOUT_VERSION = "0.1.8"

# 幂等缓存保留时长。CONVENTIONS.md §5：该 run 结束或 10 分钟，取先到者。
_IDEMPOTENT_TTL_SEC = 600.0

# executor_order 为空时按 **这台设备的 platform** 填。禁止把 adb 和 playwright 排进同一条链。
_PLATFORM_EXECUTOR_ORDER: dict[str, tuple[str, ...]] = {
    "android": ("adb", "remote"),
    "ios": ("ios_wda",),
    "web": ("playwright",),
    "playwright": ("playwright",),
    "other": ("adb", "remote"),
}

_PLATFORM_ALIASES = {
    "android": "android",
    "ios": "ios",
    "iphone": "ios",
    "ipad": "ios",
    "web": "web",
    "browser": "web",
    "playwright": "playwright",
    "other": "other",
}

_CAP_ALIASES = {
    "app_version": "get_app_version",
    "foreground_app": "get_foreground_app",
    "probe_device": "probe",
    "stop": "node.stop",
    "restart": "node.restart",
    "update": "node.update",
}

_NODE_COMMANDS = {
    "node.stop": "stop",
    "node.restart": "restart",
    "node.update": "update",
}

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
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="scout-exec")
        # Playwright sync API 必须始终在同一条线程里用。8 路通用池会把
        # launch_app 和 screenshot 拆到不同 worker，下一帧就是「没有打开的页面」。
        self._pw_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scout-pw")

    # ---------------- EXECUTE（唯一 dispatch） ----------------

    def execute(self, req: P.Execute) -> EventResult:
        key = (req.run_id, req.step_idx)
        cached = self._get_cached(key)
        if cached is not None:
            SLog.i(TAG, f"幂等命中 {key}，回缓存不重新执行（status={cached.status.value}）")
            return cached

        timeout = float(req.timeout_sec or 0) or 30.0
        pool = self._pw_pool if _use_playwright_thread(req) else self._pool
        fut = pool.submit(self._execute_body, req)
        try:
            result = fut.result(timeout=timeout)
        except TimeoutError:
            result = _timeout_result(req, timeout)
            SLog.w(TAG, f"execute timeout {timeout}s cap={req.capability_id} key={key}")
            self._put_cached(key, result)
            return result

        self._put_cached(key, result)
        return result

    def _execute_body(self, req: P.Execute) -> EventResult:
        token_run = current_run_id.set(req.run_id)
        token_step = current_step_idx.set(req.step_idx)
        if req.run_id:
            with self._lock:
                self._active_runs.add(req.run_id)
                self._run_seen[req.run_id] = time.time()
            from mino_scout.power import get_guard

            get_guard().acquire(req.run_id)
        try:
            return self._dispatch(req)
        except Exception as exc:
            SLog.e(TAG, f"execute 内部异常 cap={req.capability_id}: {exc!r}")
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
            current_run_id.reset(token_run)
            current_step_idx.reset(token_step)

    def _dispatch(self, req: P.Execute) -> EventResult:
        cap = _canonical_cap(req.capability_id)
        req = replace(req, capability_id=cap)

        if cap == "cancel_run":
            return self._dispatch_cancel(req)
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
        if cap == "probe":
            return self._dispatch_probe(req)
        if cap == "screenshot":
            return self._dispatch_screenshot(req)
        if cap == "hierarchy":
            return self._dispatch_hierarchy(req)

        device = _device_from_execute(req, node_id=self.node_id)
        routed = _with_executor_order(req, device, registered=self.executors)
        event = _event_from_execute(routed)
        return self.router.dispatch(
            event,
            device,
            run_id=req.run_id,
            step_idx=req.step_idx,
            capture_prefer=_prefer_for(device),
        )

    def _dispatch_cancel(self, req: P.Execute) -> EventResult:
        dropped = self.cancel_run(req.run_id)
        return make_event_result(
            _event_from_execute(req),
            status=EventStatus.PASS,
            executor_used="core",
            started_at=now_iso(),
            elapsed_ms=0,
            summary=f"已停止 run（清 {dropped} 条幂等缓存）；已发给设备的动作不回滚",
        )

    def _dispatch_node(self, req: P.Execute, cap: str) -> EventResult:
        cmd = _NODE_COMMANDS[cap]
        event = _event_from_execute(req)
        started = now_iso()
        if cmd == "update":
            msg = "远程更新未实现，请在该节点本机 Studio 更新"
            return make_event_result(
                event, status=EventStatus.FAIL, executor_used="core",
                started_at=started, elapsed_ms=0, summary=msg, error=msg,
                raw_response={"command": cmd},
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
        compress = float((req.params or {}).get("compress_ratio") or 2.0)
        shot = SCREEN.capture(
            device,
            timeout_sec=float(req.timeout_sec or 15.0),
            compress_ratio=compress,
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
        status, fields = self._hierarchy_fields(device)
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

    def _hierarchy_fields(self, device: DeviceRef) -> tuple[EventStatus, dict[str, Any]]:
        from mino_scout import hierarchy as H

        if device.is_web:
            return EventStatus.FAIL, {
                "error": f"sn={device.sn} 是 web 槽，没有安卓 UI hierarchy",
                "source": "playwright",
            }
        if not device.adb_serial or device.adb_serial.startswith("claw-"):
            return EventStatus.FAIL, {
                "error": "hierarchy 目前只支持 adb 通道（remote/ios_wda 待 E3 搬迁）",
                "source": "adb",
            }
        dump = H.dump_ui_nodes(device.adb_serial)
        if not dump.ok:
            return EventStatus.FAIL, {"error": dump.error, "source": "adb"}
        nodes = [n.to_brief() for n in dump.nodes]
        return EventStatus.PASS, {
            "source": "adb",
            "summary": f"UI 层级 {len(dump)} 节点",
            "extra": {"nodes": nodes},
            "nodes": nodes,
            "elapsed_ms": getattr(dump, "elapsed_ms", 0),
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


# ---------------- 载荷 ↔ 内部模型 ----------------


def _canonical_cap(capability_id: str) -> str:
    cap = str(capability_id or "").strip()
    return _CAP_ALIASES.get(cap, cap)


def _normalize_platform(raw: str) -> str:
    return _PLATFORM_ALIASES.get(str(raw or "").strip().lower(), str(raw or "").strip().lower())


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
    platform = _normalize_platform(str(req.platform or hint.get("platform") or ""))
    sn = str(req.sn or req.device_id or hint.get("sn") or hint.get("device_id") or "").strip()
    device_id = str(req.device_id or sn).strip()

    if not platform:
        platform = _platform_guess(sn)

    if platform in ("web", "playwright") or sn.lower() in ("playwright", "web"):
        from mino_scout.playwright_hub import is_web_slot, web_slot_sn

        platform = "web"
        if not is_web_slot(sn, platform):
            sn = web_slot_sn(node_id) if node_id else (sn or "playwright")

    adb_serial = str(hint.get("adb_serial") or "")
    if not adb_serial and platform == "android":
        adb_serial = _adb_serial_guess(device_id or sn)

    return DeviceRef(
        sn=sn or device_id,
        platform=platform or "android",
        adb_serial=adb_serial,
        udid=str(hint.get("udid") or (device_id if platform == "ios" else "")),
        password=str(hint.get("password") or ""),
        model=str(hint.get("model") or ""),
        extra={k: v for k, v in hint.items()
               if k not in {"platform", "adb_serial", "udid", "password", "model", "sn", "device_id"}},
    )


def _with_executor_order(
    req: P.Execute, device: DeviceRef, *, registered: Optional[dict] = None,
) -> P.Execute:
    family = _compatible_executors(device)
    if req.executor_order:
        order = [ex for ex in req.executor_order if ex in family]
        return replace(req, executor_order=order)
    plat = _normalize_platform(device.platform or req.platform) or "other"
    order = list(_PLATFORM_EXECUTOR_ORDER.get(plat) or _PLATFORM_EXECUTOR_ORDER["other"])
    order = [ex for ex in order if ex in family]
    if registered:
        present = [ex for ex in order if ex in registered]
        if present:
            order = present
    return replace(req, executor_order=order)


def _timeout_result(req: P.Execute, timeout: float) -> EventResult:
    return make_event_result(
        _event_from_execute(req),
        status=EventStatus.FAIL,
        executor_used="core",
        started_at=now_iso(),
        elapsed_ms=int(timeout * 1000),
        summary=f"timeout after {timeout}s",
        error=(
            f"动作超过 timeout_sec={timeout}s 已终止"
            "（Python 线程无法强杀，底层 adb 可能仍在跑）"
        ),
        raw_response={"timeout": True, "timeout_sec": timeout},
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


def _platform_guess(sn: str) -> str:
    from mino_scout.playwright_hub import is_web_slot

    if is_web_slot(sn):
        return "web"
    return "android"


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
