"""收到消息 -> POST 给本机 MinoScout -> prevent_default()。

地址和一次性 token 由 Scout 启动 LangBot 时放进环境变量 MINO_BRIDGE_URL / MINO_BRIDGE_TOKEN。
只打印长度和 ID，不打印正文。
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.request

from langbot_plugin.api.definition.components.common.event_listener import EventListener
from langbot_plugin.api.entities import context, events

_CHANNELS = {"lark": "feishu", "dingtalk": "dingtalk", "wecombot": "wecom", "wecom": "wecom"}


def _adapter_channel(ctx) -> str:
    """适配器名 -> Mino channel。取不到就按 launcher/事件里的 adapter 字段猜，猜不到用 adapter 原名。"""
    ev = getattr(ctx.event, "query", None)
    name = ""
    for obj in (getattr(ev, "adapter", None), getattr(ev, "bot_adapter", None)):
        if obj is not None:
            name = type(obj).__name__.lower()
    for k, v in _CHANNELS.items():
        if k in name:
            return v
    return name or "unknown"


def _elements(chain):
    try:
        return list(chain)
    except TypeError:
        return []


def _is_at(chain, bot_ids: set[str]) -> bool:
    for el in _elements(chain):
        if type(el).__name__ == "At":
            target = str(getattr(el, "target", "") or "")
            if not bot_ids or target in bot_ids or target in ("all",):
                return target != "all"
    return False


def _post(payload: dict) -> int:
    url = os.environ.get("MINO_BRIDGE_URL", "")
    token = os.environ.get("MINO_BRIDGE_TOKEN", "")
    if not url or not token:
        return 0
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "X-Mino-Token": token},
    )
    # 不走系统代理
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=8) as resp:
        return resp.status


class ForwardListener(EventListener):
    async def initialize(self) -> None:
        await super().initialize()

        @self.handler(events.PersonNormalMessageReceived)
        async def on_person(ctx: context.EventContext):
            await self._forward(ctx, "person")

        @self.handler(events.GroupNormalMessageReceived)
        async def on_group(ctx: context.EventContext):
            await self._forward(ctx, "group")

    async def _forward(self, ctx: context.EventContext, kind: str) -> None:
        ev = ctx.event
        try:
            bot_uuid = await ctx.get_bot_uuid()
        except Exception:
            bot_uuid = ""
        text = str(getattr(ev, "text_message", "") or "")
        chain = getattr(ev, "message_chain", None)
        mentioned = True if kind == "person" else _is_at(chain, set())
        chat_id = str(getattr(ev, "launcher_id", "") or "")
        sender = str(getattr(ev, "sender_id", "") or "")
        msg_id = ""
        me = getattr(ev, "message_event", None)
        for src in (getattr(me, "message_chain", None), chain):
            mid = getattr(src, "message_id", None)
            if mid not in (None, -1, ""):
                msg_id = str(mid)
                break
        if not msg_id:
            msg_id = hashlib.sha1(f"{bot_uuid}|{chat_id}|{sender}|{text}".encode()).hexdigest()[:24]
        payload = {
            "channel": _adapter_channel(ctx), "tenant": "", "chat_id": chat_id,
            "chat_type": "group" if kind == "group" else "private",
            "sender_id": sender, "msg_id": msg_id, "text": text, "mentioned": mentioned,
            "bot_uuid": bot_uuid,
            "reply_ctx": {"bot_uuid": bot_uuid, "target_type": "group" if kind == "group" else "person", "target_id": chat_id},
        }
        if kind == "group" and not mentioned:
            ctx.prevent_default()
            return
        try:
            _post(payload)
        except Exception as exc:  # 转发失败也要阻止默认处理，否则 pipeline 会去找模型
            print(f"[mino-bridge] forward failed: {type(exc).__name__}", flush=True)
        ctx.prevent_default()
