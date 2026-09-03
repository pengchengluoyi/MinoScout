"""MinoScout ⇄ MinoNexus 协议模型。

契约真源是 tests/fixtures/protocol/*.json（两仓字节相同），本文件必须能 round-trip 全部
fixture。字段级定义见 docs/PROTOCOL.md。改协议的四步流程见 CLAUDE.md §5。

刻意零依赖（只用 stdlib dataclasses），这样守门脚本 scripts/verify_protocol_contract.py
可以用裸 python3 跑，不需要装环境。
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from typing import Any, Optional, get_args, get_origin

PROTOCOL_VERSION = 2


class MsgType(str, Enum):
    REGISTER = "REGISTER"
    REGISTERED = "REGISTERED"
    HEARTBEAT = "HEARTBEAT"
    EXECUTE = "EXECUTE"
    RESULT = "RESULT"


class EventStatus(str, Enum):
    """事件执行终态。取值与上游 MiniOrangeServer 的
    server/services/ai/regression/schemas.py::EventStatus 逐字一致（小写），
    以免搬迁时到处做名字翻译。

    Router 的处置（与上游 router.py:150-170 一致）：

    | status   | Router 行为          | 语义 |
    |----------|---------------------|------|
    | pass     | 立即返回            | 做成了 |
    | blocked  | 立即返回            | 需要人介入 —— 换 executor 没意义 |
    | declined | 试下一个 executor    | 主动让位，不算故障（日志 info，不计失败） |
    | fail     | 试下一个 executor    | 真故障，但仍兜底换通道再试 |
    | skipped  | 试下一个 executor    | 本次未执行（前置不满足等） |

    注意：`declined` 与 `fail` 都会走 fallback，区别在**是否算故障**，
    不在"换个 executor 有没有用"。
    """

    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    DECLINED = "declined"


# 框架层 capability_id。N→S 是节点指令，S→N 是节点事件，形状与设备能力相同。
NODE_COMMAND_CAPS = frozenset({"node.stop", "node.restart", "node.update"})
NODE_EVENT_CAPS = frozenset({
    "node.device_lost",
    "node.device_found",
    "node.channel_changed",
    "node.engine_crashed",
    "node.shutting_down",
})
FRAMEWORK_CAPS = NODE_COMMAND_CAPS | NODE_EVENT_CAPS

OBSERVE_CAPS = {
    "screenshot": "screenshot",
    "hierarchy": "hierarchy",
    "app_version": "get_app_version",
    "foreground_app": "get_foreground_app",
    "get_app_version": "get_app_version",
    "get_foreground_app": "get_foreground_app",
}


# ---------------- 载荷 ----------------


@dataclass
class ExecutorManifest:
    id: str  # adb | remote | ios_wda | playwright
    available: bool
    provides: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class DeviceManifest:
    sn: str
    platform: str
    channels: dict[str, str] = field(default_factory=dict)
    model: str = ""


@dataclass
class Register:
    node_id: str
    token: str
    platform: str
    scout_version: str
    protocol_version: int = PROTOCOL_VERSION
    arch: str = ""
    hostname: str = ""
    studio_id: str = ""
    executors: list[ExecutorManifest] = field(default_factory=list)
    devices: list[DeviceManifest] = field(default_factory=list)


@dataclass
class Registered:
    accepted: bool
    nexus_version: str
    protocol_version: int = PROTOCOL_VERSION
    session_token: str = ""
    heartbeat_interval_sec: int = 15
    reason: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class Heartbeat:
    node_id: str
    uptime_sec: int
    busy: bool = False
    active_runs: list[str] = field(default_factory=list)
    device_delta: list[DeviceManifest] = field(default_factory=list)


@dataclass
class Execute:
    """能力调用与框架指令的统一请求。N→S 或 S→N。"""

    run_id: str
    step_idx: int
    sn: str
    capability_id: str
    params: dict[str, Any] = field(default_factory=dict)
    executor_order: list[str] = field(default_factory=list)
    low_level: dict[str, Any] = field(default_factory=dict)
    selected_impl: dict[str, Any] = field(default_factory=dict)
    device_hint: dict[str, Any] = field(default_factory=dict)
    timeout_sec: float = 30.0
    device_id: str = ""
    platform: str = ""  # android | ios | web | playwright | other


@dataclass
class Attempt:
    executor: str
    status: EventStatus
    elapsed_ms: int = 0
    error: str = ""


@dataclass
class Result:
    """EXECUTE 的应答，靠信封的 reply_to 区分是哪一条。"""

    run_id: str
    status: EventStatus
    step_idx: int = -1
    summary: str = ""
    error: str = ""
    executor_used: str = ""
    source: str = ""
    elapsed_ms: int = 0
    attempts: list[Attempt] = field(default_factory=list)
    image_base64: str = ""
    image_mime: str = ""
    width: int = 0
    height: int = 0
    extra: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)


PAYLOAD_BY_TYPE: dict[MsgType, type] = {
    MsgType.REGISTER: Register,
    MsgType.REGISTERED: Registered,
    MsgType.HEARTBEAT: Heartbeat,
    MsgType.EXECUTE: Execute,
    MsgType.RESULT: Result,
}

# ACK 必须的消息（发送方超时未收到应答需按 docs/PROTOCOL.md §6 重试）
ACK_REQUIRED: frozenset[MsgType] = frozenset(
    {
        MsgType.REGISTER,
        MsgType.EXECUTE,
    }
)


# ---------------- 信封与编解码 ----------------


@dataclass
class Envelope:
    type: MsgType
    msg_id: str
    ts: str
    payload: Any
    v: int = PROTOCOL_VERSION
    reply_to: Optional[str] = None


def _encode(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        out = {}
        for f in fields(value):
            raw = getattr(value, f.name)
            if f.name == "data" and not raw:
                continue
            if f.name in ("device_id", "platform") and not raw:
                continue
            out[f.name] = _encode(raw)
        return out
    if isinstance(value, list):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    return value


_HINT_CACHE: dict[type, dict[str, Any]] = {}


def _resolve_hints(cls: type) -> dict[str, Any]:
    """dataclass 的 f.type 在 `from __future__ import annotations` 下是字符串，这里解析成真类型。"""
    if cls not in _HINT_CACHE:
        import typing

        _HINT_CACHE[cls] = typing.get_type_hints(cls, globalns=globals())
    return _HINT_CACHE[cls]


def _decode(tp: Any, value: Any) -> Any:
    origin = get_origin(tp)
    if origin is list:
        (inner,) = get_args(tp) or (Any,)
        return [_decode(inner, v) for v in value or []]
    if origin is dict:
        return dict(value or {})
    args = get_args(tp)
    if origin is not None and type(None) in args:  # Optional[X]
        if value is None:
            return None
        return _decode(next(a for a in args if a is not type(None)), value)
    if isinstance(tp, type) and issubclass(tp, Enum):
        return tp(value)
    if is_dataclass(tp):
        hints = _resolve_hints(tp)
        kwargs = {n: _decode(t, value[n]) for n, t in hints.items() if n in (value or {})}
        return tp(**kwargs)  # 未知字段按 §7 忽略
    return value


def dumps(env: Envelope) -> dict[str, Any]:
    """Envelope → 可 json.dumps 的 dict。"""
    return {
        "v": env.v,
        "type": env.type.value,
        "msg_id": env.msg_id,
        "reply_to": env.reply_to,
        "ts": env.ts,
        "payload": _encode(env.payload),
    }


def loads(raw: dict[str, Any]) -> Envelope:
    """dict → Envelope。未知 type 抛 UnknownMessageType，调用方应记 warn 并忽略（§7）。"""
    try:
        mtype = MsgType(raw["type"])
    except ValueError as exc:
        raise UnknownMessageType(raw.get("type")) from exc
    payload_cls = PAYLOAD_BY_TYPE[mtype]
    body = raw.get("payload") or {}
    hints = _resolve_hints(payload_cls)
    kwargs = {n: _decode(t, body[n]) for n, t in hints.items() if n in body}
    return Envelope(
        v=int(raw.get("v", PROTOCOL_VERSION)),
        type=mtype,
        msg_id=raw["msg_id"],
        reply_to=raw.get("reply_to"),
        ts=raw["ts"],
        payload=payload_cls(**kwargs),
    )


class UnknownMessageType(Exception):
    """收到未知 type。按 docs/PROTOCOL.md §7，接收方记 warn 并忽略，不可关连接。"""
