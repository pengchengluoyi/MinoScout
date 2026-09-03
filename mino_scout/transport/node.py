"""node daemon transport：反向 dial Nexus 的 WebSocket 客户端。

协议见 docs/PROTOCOL.md。这一层只做四件事：
  1. 连接与重连（指数退避）、REGISTER / HEARTBEAT（心跳重探设备，报 device_delta）
  2. 收帧 → 解码 → 交给 ScoutCore → 编码 → 回帧
  3. 超时/异常兜底：**任何情况下都要回一条 RESULT**，不能让 Nexus 干等
  4. 未知消息类型按协议 §7 记 warn 并忽略（不可关连接）

**业务逻辑一律不在这里。** core 不知道网络存在（CLAUDE.md §2.2）。
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime, timezone
from typing import Any, Optional

from mino_scout import protocol as P
from mino_scout.core import (
    SCOUT_VERSION,
    ScoutCore,
)
from mino_scout.log import SLog, current_node_id
from mino_scout.schemas import EventStatus

TAG = "NodeTransport"

_BACKOFF_START = 1.0
_BACKOFF_MAX = 30.0
# 单帧上限（协议 §1）。超了要降质重发而不是分片 —— 这里只负责拦住并报错。
_MAX_FRAME_BYTES = 32 * 1024 * 1024


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


_seq = 0


def _msg_id() -> str:
    """单调递增 + 时间戳。不引 ulid 依赖，够唯一即可。"""
    global _seq
    _seq += 1
    return f"{int(time.time() * 1000):013d}{_seq:06d}"


def changed_devices(
    prev: dict[str, P.DeviceManifest], now: list[P.DeviceManifest],
) -> list[P.DeviceManifest]:
    """对照上一轮发现结果，只返回变化的设备（协议 §4.3 device_delta）。

    新出现 / 通道变化用当前 manifest；消失的设备把通道标成 disconnected，
    这样 node.device_lost 丢了也能靠心跳把 Nexus 缓存改过来。
    """
    current = {d.sn: d for d in now}
    out: list[P.DeviceManifest] = []
    for sn, old in prev.items():
        if sn not in current:
            out.append(
                P.DeviceManifest(
                    sn=old.sn,
                    platform=old.platform,
                    model=old.model,
                    channels={ch: "disconnected" for ch in (old.channels or {})},
                )
            )
    for sn, dev in current.items():
        old = prev.get(sn)
        if old is None or dict(old.channels) != dict(dev.channels):
            out.append(dev)
    return out


class NodeTransport:
    def __init__(
        self,
        core: ScoutCore,
        *,
        nexus_url: str,
        token: str,
        heartbeat_sec: int = 15,
    ):
        self.core = core
        self.nexus_url = nexus_url
        self.token = token
        self.heartbeat_sec = heartbeat_sec
        self._ws: Any = None
        self._session_token = ""
        self._stop = asyncio.Event()
        self._intentional_stop = False
        self._seen_register = False
        self._last_devices: dict[str, P.DeviceManifest] = {}
        # msg_id -> 等待应答的 future
        self._waiters: dict[str, asyncio.Future] = {}
        # 在途的请求处理任务。必须持强引用，否则会被 GC（见 _on_frame）
        self._inflight: set[asyncio.Task] = set()

    # ---------------- 生命周期 ----------------

    async def run_forever(self) -> None:
        """连不上就退避重连，无限重试（协议 §1）。"""
        current_node_id.set(self.core.node_id)
        backoff = _BACKOFF_START
        while not self._stop.is_set():
            try:
                await self._connect_once()
                backoff = _BACKOFF_START  # 成功连过一次就重置
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                SLog.w(TAG, f"连接断开/失败: {type(exc).__name__}: {exc}")
            if self._stop.is_set():
                break
            # 加抖动，避免多节点同时重连打爆 Nexus
            wait = min(backoff, _BACKOFF_MAX) * (0.8 + 0.4 * random.random())
            SLog.i(TAG, f"{wait:.1f}s 后重连 {self.nexus_url}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, _BACKOFF_MAX)

    def stop(self) -> None:
        self.request_shutdown()

    def request_shutdown(self) -> None:
        """人主动停（SIGTERM / `mino-scout stop`）。断线重连不要走这条。"""
        self._intentional_stop = True
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self._abort_ws(ws)))

    async def _abort_ws(self, ws: Any) -> None:
        try:
            await ws.close()
        except Exception:
            pass

    async def _connect_once(self) -> None:
        import websockets

        SLog.i(TAG, f"dial {self.nexus_url}")
        async with websockets.connect(self.nexus_url, max_size=_MAX_FRAME_BYTES) as ws:
            self._ws = ws
            # 顺序要紧：**先起接收循环，再 REGISTER**。
            # REGISTER 要等 REGISTERED 应答，而应答只能由接收循环派发；
            # 反过来写会死等到超时，且一条帧都处理不到。
            recv = asyncio.create_task(self._recv_loop(ws))
            stopper = asyncio.create_task(self._stop.wait())
            hb: Optional[asyncio.Task] = None
            try:
                await self._register()
                hb = asyncio.create_task(self._heartbeat_loop())
                done, _pending = await asyncio.wait(
                    {recv, stopper}, return_when=asyncio.FIRST_COMPLETED,
                )
                if stopper in done and not recv.done():
                    await self._announce_shutdown()
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    await recv
            finally:
                if hb is not None:
                    hb.cancel()
                if not recv.done():
                    recv.cancel()
                if not stopper.done():
                    stopper.cancel()
                self._ws = None

    async def _recv_loop(self, ws: Any) -> None:
        async for raw in ws:
            await self._on_frame(raw)

    # ---------------- REGISTER / HEARTBEAT ----------------

    async def _register(self) -> None:
        import platform

        # manifest() 会跑连通性探测，里面有阻塞调用；playwright 的 sync API
        # 更是**明确拒绝在事件循环里被调用**（"Please use the Async API instead"）。
        # 必须丢线程池 —— 直接 await 会让 playwright 永远上报不可用。
        execs, devices = await asyncio.to_thread(self.core.manifest)
        from mino_scout.config import resolve_hostname, resolve_studio_id

        payload = P.Register(
            node_id=self.core.node_id,
            token=self.token,
            platform=platform.system().lower(),
            arch=platform.machine(),
            scout_version=SCOUT_VERSION,
            hostname=resolve_hostname(),
            studio_id=resolve_studio_id(),
            executors=execs,
            devices=devices,
        )
        reply = await self._request(P.MsgType.REGISTER, payload)
        if reply is None:
            raise RuntimeError("REGISTER 没等到 REGISTERED")
        reg: P.Registered = reply.payload
        if not reg.accepted:
            # 不要立刻重试 —— 退避由 run_forever 负责（协议 §4.2）
            raise RuntimeError(f"Nexus 拒绝注册: {reg.reason or '未给原因'}")
        if reg.protocol_version != P.PROTOCOL_VERSION:
            raise RuntimeError(
                f"协议版本不一致：Scout v{P.PROTOCOL_VERSION} vs Nexus v{reg.protocol_version}。"
                "按协议 §7 不做降级协商"
            )
        self._session_token = reg.session_token
        self.heartbeat_sec = reg.heartbeat_interval_sec or self.heartbeat_sec
        avail = [e.id for e in execs if e.available]
        SLog.i(
            TAG,
            f"已注册 node={self.core.node_id} nexus=v{reg.nexus_version} "
            f"executors={avail} devices={len(devices)} hb={self.heartbeat_sec}s",
        )
        for w in reg.warnings or []:
            SLog.w(TAG, f"Nexus: {w}")
        if self._seen_register:
            await self._emit_device_delta(self._last_devices, devices)
        self._seen_register = True
        self._last_devices = {d.sn: d for d in devices}

    async def _emit_device_delta(
        self, prev: dict[str, P.DeviceManifest], now: list[P.DeviceManifest],
    ) -> None:
        """对照上一轮发现结果发 EXECUTE node.device_*。权威状态仍以 HEARTBEAT.device_delta 为准。"""
        current = {d.sn: d for d in now}
        for sn, old in prev.items():
            if sn not in current:
                await self._send_framework(
                    "node.device_lost", sn=sn, platform=old.platform, detail="重探后消失",
                )
        for sn, dev in current.items():
            if sn not in prev:
                await self._send_framework(
                    "node.device_found", sn=sn, platform=dev.platform, detail="重探后出现",
                )
                continue
            if dict(prev[sn].channels) != dict(dev.channels):
                await self._send_framework(
                    "node.channel_changed",
                    sn=sn,
                    platform=dev.platform,
                    detail=f"{prev[sn].channels} → {dev.channels}",
                )

    async def _announce_shutdown(self) -> None:
        if not self._intentional_stop:
            return
        try:
            await self._send(P.MsgType.HEARTBEAT, self.core.heartbeat())
        except Exception:
            pass
        hb = self.core.heartbeat()
        runs = ",".join(hb.active_runs) or "none"
        await self._send_framework("node.shutting_down", detail=f"active_runs={runs}")

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.heartbeat_sec)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self._tick_heartbeat()
            except Exception as exc:
                SLog.w(TAG, f"心跳发送失败: {exc}")
                return

    async def _tick_heartbeat(self) -> None:
        """心跳时重探 adb / 本机设备，把热插拔放进 device_delta。"""
        devices: Optional[list[P.DeviceManifest]]
        try:
            devices = await asyncio.to_thread(self.core.discover_devices)
        except Exception as exc:
            SLog.w(TAG, f"心跳重探设备失败: {exc}")
            devices = None
        delta: list[P.DeviceManifest] = []
        if devices is not None:
            delta = changed_devices(self._last_devices, devices)
        hb = self.core.heartbeat()
        hb.device_delta = delta
        await self._send(P.MsgType.HEARTBEAT, hb)
        if devices is None:
            return
        if delta:
            SLog.i(TAG, f"心跳设备变化 {[d.sn for d in delta]}")
            await self._emit_device_delta(self._last_devices, devices)
        self._last_devices = {d.sn: d for d in devices}

    # ---------------- 收帧 ----------------

    async def _on_frame(self, raw: Any) -> None:
        if isinstance(raw, (bytes, bytearray)):
            SLog.w(TAG, f"收到二进制帧 {len(raw)} 字节，协议只用文本帧，忽略")
            return
        try:
            data = json.loads(raw)
        except Exception as exc:
            SLog.e(TAG, f"帧不是合法 JSON，忽略: {exc}")
            return
        try:
            env = P.loads(data)
        except P.UnknownMessageType as exc:
            # 协议 §7：未知 type 记 warn 并忽略，不可关连接
            SLog.w(TAG, f"未知消息类型 {exc}，忽略（协议 §7）")
            return
        except Exception as exc:
            SLog.e(TAG, f"解码失败，忽略: {type(exc).__name__}: {exc}")
            return

        if env.v != P.PROTOCOL_VERSION:
            raise RuntimeError(f"Nexus 用了协议 v{env.v}，本 Scout 是 v{P.PROTOCOL_VERSION}")

        # 应答类消息交给等待者
        if env.reply_to and env.reply_to in self._waiters:
            fut = self._waiters.pop(env.reply_to)
            if not fut.done():
                fut.set_result(env)
            return

        handler = {
            P.MsgType.EXECUTE: self._on_execute,
        }.get(env.type)
        if handler is None:
            SLog.w(TAG, f"收到不该由 Scout 处理的消息 {env.type.value}，忽略")
            return
        # 每条请求独立处理，慢的那条不挡住后面的。
        # 必须持引用：asyncio 只保弱引用，不留着会被 GC 掉，表现为"请求静默消失"。
        task = asyncio.create_task(self._guarded(handler, env))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _guarded(self, handler, env: P.Envelope) -> None:
        """兜底：handler 无论怎么炸，都要回一条 RESULT。

        协议 §6 规定 Nexus 会等 RESULT；这里静默失败会让它干等到超时。
        """
        try:
            await handler(env)
        except Exception as exc:
            SLog.e(TAG, f"处理 {env.type.value} 抛异常: {type(exc).__name__}: {exc}")
            run_id = str(getattr(env.payload, "run_id", "") or "")
            step_idx = int(getattr(env.payload, "step_idx", -1) or -1)
            await self._reply_result(
                env,
                P.Result(
                    run_id=run_id, step_idx=step_idx, status=EventStatus.FAIL,
                    summary=f"scout 内部错误：{type(exc).__name__}",
                    error=str(exc)[:400], executor_used="transport",
                ),
            )

    # ---------------- 请求处理 ----------------

    async def _on_execute(self, env: P.Envelope) -> None:
        await self._run_execute(env, env.payload)

    async def _run_execute(self, env: P.Envelope, req: P.Execute) -> None:
        # core 是同步的（executor 全是阻塞 IO），丢线程池，别卡住事件循环
        result = await asyncio.to_thread(self.core.execute, req)
        await self._reply_result(env, _result_from_event(req, result))
        self._apply_node_side_effects(result)

    def _apply_node_side_effects(self, ev) -> None:
        extra = dict(getattr(ev, "raw_response", None) or {})
        if extra.get("_scout_reexec"):
            from mino_scout.service import schedule_reexec

            schedule_reexec()
        if extra.get("_scout_shutdown"):
            self.request_shutdown()

    # ---------------- 发送 ----------------

    async def _send(self, mtype: P.MsgType, payload: Any, *, reply_to: Optional[str] = None) -> str:
        if self._ws is None:
            raise RuntimeError("未连接")
        env = P.Envelope(type=mtype, msg_id=_msg_id(), ts=_now_iso(), payload=payload, reply_to=reply_to)
        text = json.dumps(P.dumps(env), ensure_ascii=False)
        size = len(text.encode())
        if size > _MAX_FRAME_BYTES:
            raise RuntimeError(f"帧过大 {size} > {_MAX_FRAME_BYTES}（协议 §1：应降质重发，不分片）")
        await self._ws.send(text)
        return env.msg_id

    async def _reply_result(self, req_env: P.Envelope, result: P.Result) -> None:
        try:
            await self._send(P.MsgType.RESULT, result, reply_to=req_env.msg_id)
        except Exception as exc:
            SLog.e(TAG, f"回 RESULT 失败（Nexus 将超时重发）: {exc}")

    async def _request(self, mtype: P.MsgType, payload: Any, *, timeout: float = 30.0):
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        msg_id = await self._send(mtype, payload)
        self._waiters[msg_id] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._waiters.pop(msg_id, None)
            SLog.w(TAG, f"{mtype.value} 等应答超时 {timeout}s")
            return None

    async def _send_framework(
        self,
        capability_id: str,
        *,
        sn: str = "",
        platform: str = "",
        detail: str = "",
        severity: str = "info",
    ) -> None:
        """S→N 框架事件，形状与能力调用相同。等 RESULT；丢了靠心跳收敛。"""
        event = capability_id.split(".", 1)[-1] if capability_id.startswith("node.") else capability_id
        req = P.Execute(
            run_id="",
            step_idx=-1,
            sn=sn,
            capability_id=capability_id,
            params={
                "node_id": self.core.node_id,
                "event": event,
                "detail": detail,
                "severity": severity,
            },
            timeout_sec=5.0,
            device_id=sn,
            platform=platform,
        )
        try:
            reply = await self._request(P.MsgType.EXECUTE, req, timeout=5.0)
            if reply is None:
                SLog.w(TAG, f"{capability_id} 未获 RESULT，等心跳收敛")
        except Exception:
            pass


_SKIP_EXTRA_KEYS = frozenset(
    {"image_base64", "image_mime", "width", "height", "summary", "error", "source", "elapsed_ms"}
)


def _result_from_event(req: P.Execute, ev) -> P.Result:
    """EventResult → 协议的 Result。截图填现有 image_* 字段，同时放进 data。"""
    raw = dict(ev.raw_response or {})
    image_base64 = str(raw.get("image_base64") or "")
    image_mime = str(raw.get("image_mime") or "")
    width = int(raw.get("width") or 0)
    height = int(raw.get("height") or 0)

    if isinstance(raw.get("extra"), dict) and raw["extra"]:
        extra = {k: v for k, v in dict(raw["extra"]).items() if not str(k).startswith("_")}
    else:
        extra = {
            k: v for k, v in raw.items()
            if k not in _SKIP_EXTRA_KEYS and k != "extra" and not str(k).startswith("_")
        }

    data = {k: v for k, v in raw.items() if k != "extra" and not str(k).startswith("_")}
    if image_base64:
        data.setdefault("image_base64", image_base64)
        data.setdefault("image_mime", image_mime)
        data.setdefault("width", width)
        data.setdefault("height", height)

    return P.Result(
        run_id=req.run_id,
        step_idx=req.step_idx,
        status=ev.status,
        summary=ev.summary,
        error=ev.error,
        executor_used=ev.executor_used,
        source=ev.executor_used or str(raw.get("source") or ""),
        elapsed_ms=ev.elapsed_ms,
        attempts=[
            P.Attempt(
                executor=str(a.get("executor") or ""),
                status=EventStatus(a.get("status") or "fail"),
                elapsed_ms=int(a.get("elapsed_ms") or 0),
                error=str(a.get("error") or ""),
            )
            for a in (ev.attempts or [])
        ],
        image_base64=image_base64,
        image_mime=image_mime,
        width=width,
        height=height,
        extra=extra,
        data=data,
    )
