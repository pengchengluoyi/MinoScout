"""端到端：假 Nexus（真 WebSocket 服务端）↔ NodeTransport ↔ ScoutCore ↔ 假 adb。

这是整条链路第一次真正跑起来 —— 真的开 WS、真的走协议编解码、真的过 Router 和
executor，只有 adb 子进程是假的。

    python tests/test_transport_e2e.py    （需要 websockets + pydantic）
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mino_scout import protocol as P  # noqa: E402
from mino_scout.core import ScoutCore  # noqa: E402
from mino_scout.executors.adb_executor import AdbExecutor  # noqa: E402
from mino_scout.transport.node import NodeTransport, _msg_id, _now_iso  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{' — ' + detail if detail else ''}")
        failures.append(name)


# ---- 假 adb：截图返回一张最小 PNG，其余命令成功 ----
import struct, zlib  # noqa: E402


def _png(w: int, h: int) -> bytes:
    def chunk(t: bytes, d: bytes) -> bytes:
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


PNG = _png(1080, 2340)
ADB_CALLS: list[str] = []
ADB_DEVICES_OUT = "List of devices attached\nR5CT30xxxx\tdevice model:SM_S9210\n"


class _Proc:
    def __init__(self, rc, out, err=b""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def fake_adb(cmd, **kwargs):
    joined = " ".join(str(c) for c in cmd)
    ADB_CALLS.append(joined)
    if "screencap" in joined:
        return _Proc(0, PNG)
    if "adb version" in joined:
        return _Proc(0, "Android Debug Bridge version 1.0.41")
    if "devices" in joined:
        return _Proc(0, ADB_DEVICES_OUT)
    text = kwargs.get("text", False)
    return _Proc(0, "" if text else b"")


subprocess.run = fake_adb  # type: ignore[assignment]


# ---- 假 Nexus ----
class FakeNexus:
    def __init__(self):
        self.register: P.Register | None = None
        self.heartbeats: list[P.Heartbeat] = []
        self.node_events: list[P.NodeEvent] = []
        self.results: dict[str, P.Result] = {}
        self._ws = None
        self._ready = asyncio.Event()

    async def handler(self, ws):
        self._ws = ws
        async for raw in ws:
            env = P.loads(json.loads(raw))
            if env.type is P.MsgType.REGISTER:
                self.register = env.payload
                await self._send(ws, P.MsgType.REGISTERED, P.Registered(
                    accepted=True, nexus_version="0.1.0", session_token="sess-1",
                    heartbeat_interval_sec=1, warnings=["测试用 Nexus"],
                ), reply_to=env.msg_id)
                self._ready.set()
            elif env.type is P.MsgType.HEARTBEAT:
                self.heartbeats.append(env.payload)
            elif env.type is P.MsgType.NODE_EVENT:
                self.node_events.append(env.payload)
            elif env.type is P.MsgType.RESULT:
                self.results[env.reply_to or ""] = env.payload

    async def _send(self, ws, mtype, payload, reply_to=None):
        env = P.Envelope(type=mtype, msg_id=_msg_id(), ts=_now_iso(), payload=payload, reply_to=reply_to)
        await ws.send(json.dumps(P.dumps(env), ensure_ascii=False))
        return env.msg_id

    async def ask(self, mtype, payload, *, timeout=10.0) -> P.Result:
        """下发一条请求，等它的 RESULT。"""
        mid = await self._send(self._ws, mtype, payload)
        for _ in range(int(timeout * 100)):
            if mid in self.results:
                return self.results.pop(mid)
            await asyncio.sleep(0.01)
        raise AssertionError(f"{mtype.value} 没等到 RESULT")


async def main() -> int:
    import websockets

    nexus = FakeNexus()
    server = await websockets.serve(nexus.handler, "127.0.0.1", 0, max_size=32 * 1024 * 1024)
    port = server.sockets[0].getsockname()[1]

    core = ScoutCore({"adb": AdbExecutor()}, node_id="testnode01")
    transport = NodeTransport(core, nexus_url=f"ws://127.0.0.1:{port}", token="tok-1")
    task = asyncio.create_task(transport.run_forever())
    await asyncio.wait_for(nexus._ready.wait(), timeout=10)

    print("== REGISTER：身份 / 能力 / 设备都报上来了 ==")
    reg = nexus.register
    check("node_id", reg.node_id == "testnode01", reg.node_id)
    check("token", reg.token == "tok-1")
    check("协议版本", reg.protocol_version == P.PROTOCOL_VERSION)
    adb_m = [e for e in reg.executors if e.id == "adb"]
    check("上报了 adb", bool(adb_m) and adb_m[0].available, str(reg.executors))
    check("provides 是 abstract cap", "ui_native_input" in adb_m[0].provides, str(adb_m[0].provides))
    check("发现了设备", any(d.sn == "R5CT30xxxx" for d in reg.devices), str(reg.devices))

    print("== OBSERVE screenshot：真的过了一遍协议编解码 ==")
    res = await nexus.ask(P.MsgType.OBSERVE, P.Observe(
        run_id="run-1", sn="R5CT30xxxx", kind=P.ObserveKind.SCREENSHOT, prefer=["adb"]))
    check("pass", res.status is P.EventStatus.PASS, f"{res.status} {res.error}")
    check("source=adb", res.source == "adb")
    check("原图尺寸", (res.width, res.height) == (1080, 2340), f"{res.width}x{res.height}")
    check("带了图", len(res.image_base64) > 100)
    check("mime", res.image_mime == "image/png", res.image_mime)

    print("== EXECUTE tap：命令拼装 + RESULT 回填 ==")
    ADB_CALLS.clear()
    res = await nexus.ask(P.MsgType.EXECUTE, P.Execute(
        run_id="run-1", step_idx=7, sn="R5CT30xxxx", capability_id="tap_element",
        params={"x": 512, "y": 830}, executor_order=["adb"],
        device_hint={"adb_serial": "R5CT30xxxx", "password": "hunter2"}))
    check("pass", res.status is P.EventStatus.PASS, f"{res.status} {res.error}")
    check("executor_used=adb", res.executor_used == "adb")
    check("step_idx 回填", res.step_idx == 7)
    check("真发了 input tap", any("input tap 512 830" in c for c in ADB_CALLS), str(ADB_CALLS))
    check("attempts 有一条", len(res.attempts) == 1 and res.attempts[0].executor == "adb", str(res.attempts))

    print("== 幂等：同 (run_id, step_idx) 重发不重复操作设备 ==")
    ADB_CALLS.clear()
    res2 = await nexus.ask(P.MsgType.EXECUTE, P.Execute(
        run_id="run-1", step_idx=7, sn="R5CT30xxxx", capability_id="tap_element",
        params={"x": 512, "y": 830}, executor_order=["adb"],
        device_hint={"adb_serial": "R5CT30xxxx"}))
    check("仍回 pass", res2.status is P.EventStatus.PASS)
    check("没有再点一次", not any("input tap" in c for c in ADB_CALLS), str(ADB_CALLS))

    print("== CANCEL_RUN：清掉幂等缓存，语义是不再继续 ==")
    res = await nexus.ask(P.MsgType.CANCEL_RUN, P.CancelRun(run_id="run-1", reason="test"))
    check("pass", res.status is P.EventStatus.PASS)
    check("说明不回滚", "不回滚" in res.summary, res.summary)
    ADB_CALLS.clear()
    await nexus.ask(P.MsgType.EXECUTE, P.Execute(
        run_id="run-1", step_idx=7, sn="R5CT30xxxx", capability_id="tap_element",
        params={"x": 1, "y": 2}, executor_order=["adb"], device_hint={"adb_serial": "R5CT30xxxx"}))
    check("缓存清了，会重新执行", any("input tap 1 2" in c for c in ADB_CALLS), str(ADB_CALLS))

    print("== PROBE：回该设备的通道状态 ==")
    res = await nexus.ask(P.MsgType.PROBE, P.Probe(sn="R5CT30xxxx", channels=["adb"]))
    check("pass", res.status is P.EventStatus.PASS)
    check("带 channels", "channels" in res.extra, str(res.extra))

    print("== NODE_COMMAND update：诚实失败，不断连 ==")
    os.environ["MINO_SCOUT_NO_REEXEC"] = "1"
    res = await nexus.ask(P.MsgType.NODE_COMMAND, P.NodeCommand(command="update", reason="test"))
    check("update fail", res.status is P.EventStatus.FAIL, f"{res.status} {res.error}")
    check("说明本机更新", "本机" in (res.error or res.summary or ""), f"{res.summary} {res.error}")

    print("== 未知消息类型：记 warn 并忽略，不断连（协议 §7）==")
    await nexus._ws.send(json.dumps({
        "v": 1, "type": "FUTURE_MSG", "msg_id": "x", "reply_to": None,
        "ts": _now_iso(), "payload": {},
    }))
    await asyncio.sleep(0.2)
    res = await nexus.ask(P.MsgType.PROBE, P.Probe(sn="R5CT30xxxx"))
    check("连接仍活着", res.status is P.EventStatus.PASS)

    print("== 坏载荷：也要回 RESULT，不能让 Nexus 干等 ==")
    res = await nexus.ask(P.MsgType.OBSERVE, P.Observe(
        run_id="run-2", sn="R5CT30xxxx", kind=P.ObserveKind.SCREENSHOT, prefer=["nonexistent"]))
    check("回了 fail 而不是超时", res.status is P.EventStatus.FAIL, str(res.status))
    check("点名未实现", "未实现" in (res.error or ""), res.error)

    print("== HEARTBEAT 在跑 ==")
    await asyncio.sleep(1.3)
    check("收到心跳", len(nexus.heartbeats) >= 1, str(len(nexus.heartbeats)))
    check("心跳带 node_id", nexus.heartbeats[0].node_id == "testnode01")

    print("== HEARTBEAT 热插拔：REGISTER 之后再出现的 adb 设备 ==")
    global ADB_DEVICES_OUT
    before_hb = len(nexus.heartbeats)
    before_ev = len(nexus.node_events)
    ADB_DEVICES_OUT = (
        "List of devices attached\n"
        "R5CT30xxxx\tdevice model:SM_S9210\n"
        "5fda2f6d\tdevice model:25102RKBEC\n"
    )
    await asyncio.sleep(1.3)
    new_hbs = nexus.heartbeats[before_hb:]
    delta_sns = [d.sn for hb in new_hbs for d in (hb.device_delta or [])]
    check("又收到心跳", len(new_hbs) >= 1, str(len(new_hbs)))
    check("device_delta 含新串号", "5fda2f6d" in delta_sns, str(delta_sns))
    found = [e for e in nexus.node_events[before_ev:] if e.event == "device_found" and e.sn == "5fda2f6d"]
    check("NODE_EVENT device_found", bool(found), str(nexus.node_events[before_ev:]))

    print("== HEARTBEAT 热拔出：消失的设备标 disconnected ==")
    before_hb = len(nexus.heartbeats)
    before_ev = len(nexus.node_events)
    ADB_DEVICES_OUT = "List of devices attached\nR5CT30xxxx\tdevice model:SM_S9210\n"
    await asyncio.sleep(1.3)
    new_hbs = nexus.heartbeats[before_hb:]
    lost_delta = [
        d for hb in new_hbs for d in (hb.device_delta or [])
        if d.sn == "5fda2f6d" and (d.channels or {}).get("adb") == "disconnected"
    ]
    lost_ev = [e for e in nexus.node_events[before_ev:] if e.event == "device_lost" and e.sn == "5fda2f6d"]
    check("device_delta 标 disconnected", bool(lost_delta), str([d.channels for d in lost_delta]))
    check("NODE_EVENT device_lost", bool(lost_ev), str(nexus.node_events[before_ev:]))

    transport.stop()
    task.cancel()
    server.close()
    await server.wait_closed()

    print()
    if failures:
        print(f"FAILED {len(failures)}: {', '.join(failures)}")
        return 1
    print("ALL OK — transport 端到端（真 WebSocket + 真协议 + 假 adb）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
