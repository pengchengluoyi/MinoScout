"""收到消息 -> POST 给本机 MinoScout -> prevent_default()。

地址和一次性 token 由 Scout 启动 LangBot 时放进环境变量 MINO_BRIDGE_URL / MINO_BRIDGE_TOKEN。
只打印长度和 ID，不打印正文。
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from pathlib import Path

from langbot_plugin.api.definition.components.common.event_listener import EventListener
from langbot_plugin.api.entities import context, events

_CHANNELS = {"lark": "feishu", "dingtalk": "dingtalk", "wecombot": "wecom", "wecom": "wecom"}


def _map_channel(name: str) -> str:
    low = name.lower()
    for key, channel in _CHANNELS.items():
        if key in low:
            return channel
    return ""


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


def _endpoint() -> tuple[str, str]:
    """回环地址。插件跑在 LangBot 单独拉起的进程里，继承不到 LangBot 的环境变量，
    所以优先读环境变量，没有就沿代码目录往上找 Scout 写的 mino-bridge.json。"""
    url = os.environ.get("MINO_BRIDGE_URL", "")
    token = os.environ.get("MINO_BRIDGE_TOKEN", "")
    if url and token:
        return url, token
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "mino-bridge.json"
        if not cand.is_file():
            continue
        try:
            data = json.loads(cand.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        return str(data.get("url") or ""), str(data.get("token") or "")
    return "", ""


def _post(payload: dict) -> int:
    url, token = _endpoint()
    if not url or not token:
        print("[mino-bridge] endpoint missing", flush=True)
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
            "channel": "", "tenant": "", "chat_id": chat_id,
            "chat_type": "group" if kind == "group" else "private",
            "sender_id": sender, "msg_id": msg_id, "text": text, "mentioned": mentioned,
            "bot_uuid": bot_uuid,
            "reply_ctx": {"bot_uuid": bot_uuid, "target_type": "group" if kind == "group" else "person", "target_id": chat_id},
        }
        if kind == "group" and not mentioned:
            ctx.prevent_default()
            return
        payload["channel"] = await self._channel(ctx, bot_uuid)
        if not payload["channel"]:
            print("[mino-bridge] channel unknown", flush=True)
        try:
            code = _post(payload)
            if code and code != 200:
                print(f"[mino-bridge] forward status={code} channel={payload['channel']}", flush=True)
        except Exception as exc:  # 转发失败也要阻止默认处理，否则 pipeline 会去找模型
            print(
                f"[mino-bridge] forward failed: {type(exc).__name__} channel={payload['channel']}",
                flush=True,
            )
        ctx.prevent_default()

    async def _channel(self, ctx: context.EventContext, bot_uuid: str) -> str:
        """事件模型里没有适配器。用 bot 的 adapter（lark / dingtalk / wecombot）映射渠道。"""
        name = ""
        if bot_uuid:
            try:
                info = await self.plugin.get_bot_info(bot_uuid)
                if isinstance(info, dict):
                    name = str(info.get("adapter") or info.get("name") or "")
            except Exception as exc:
                print(f"[mino-bridge] bot info failed: {type(exc).__name__}", flush=True)
        return _map_channel(name)
