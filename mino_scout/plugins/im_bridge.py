"""统一 IM 入站：微信 iLink（以及后续 LangBot 渠道）都走 `ingest`。

流程：取本会话最近 8 条（不含本条）→ 落库本条 → 组装 `params["im"]` → 交给已注册 hook。
没有 hook（transport 还没起）时直接进 `im_pending`，等重连后补发。
日志不打正文，只打长度 / 渠道 / ID。
"""
from __future__ import annotations

import time
from typing import Any, Callable

from mino_scout.log import SLog
from mino_scout.plugins import im_store

TAG = "ImBridge"
TEXT_MAX = 12000
RECENT_TURNS = 8

_hook: Callable[[dict[str, Any]], None] | None = None


def set_hook(fn: Callable[[dict[str, Any]], None] | None) -> None:
    global _hook
    _hook = fn


def ingest(
    channel: str,
    chat_id: str,
    chat_type: str,
    sender_id: str,
    msg_id: str,
    text: str,
    mentioned: bool,
    reply_ctx: dict[str, str] | None,
    tenant: str = "",
    instance_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """返回发出的 im 载荷；重复消息（已入库）返回 None 不再上报。"""
    body = str(text or "")[:TEXT_MAX]
    ts = int(time.time())
    turns = im_store.recent(channel, chat_id, RECENT_TURNS, sender_id=sender_id,
                            instance_id=instance_id, tenant=tenant, thread_id=str((metadata or {}).get('thread_id') or ''))
    im = {
        "instance_id": str(instance_id or ""),
        "channel": str(channel),
        "tenant": str(tenant or ""),
        "chat_id": str(chat_id),
        "chat_type": "group" if chat_type == "group" else "private",
        "sender_id": str(sender_id),
        "msg_id": str(msg_id),
        "text": body,
        "mentioned": bool(mentioned),
        "reply_ctx": {str(k): str(v) for k, v in (reply_ctx or {}).items()},
        "recent_turns": turns,
    }
    meta = metadata or {}
    im.update({key: meta[key] for key in ('event_type', 'reply_to', 'target_message_id', 'thread_id', 'revision', 'attachments', 'media_error', 'callback') if key in meta})
    if im.get('attachments'):
        from mino_scout.plugins.im_attachments import save
        normalized = []
        for item in im['attachments'][:4]:
            if not isinstance(item, dict):
                continue
            if item.get('ref'):
                normalized.append(item)
            else:
                try:
                    if channel == 'feishu_cli' and item.get('file_key'):
                        from mino_scout.plugins import feishu_media
                        import base64
                        content, mime = feishu_media.resource(msg_id, item)
                        item = {**item, 'base64': base64.b64encode(content).decode(), 'mime': mime}
                    elif channel == 'feishu' and item.get('file_key'):
                        from mino_scout.plugins import im_feishu, langbot_host
                        import base64
                        content, mime = im_feishu.resource(langbot_host._node_id, msg_id, str(item['file_key']), str(item.get('kind') or ''))
                        item = {**item, 'base64': base64.b64encode(content).decode(), 'mime': mime}
                    normalized.append(save(im, item))
                except Exception as exc:
                    SLog.w(TAG, f"附件读取失败 channel={channel} kind={item.get('kind')} type={type(exc).__name__}")
                    reason = (str(exc) if isinstance(exc, (ValueError, RuntimeError)) and str(exc)
                              else '附件读取失败，请稍后重新发送；若持续失败，请检查当前渠道的资源读取权限。')
                    normalized.append({'kind': str(item.get('kind') or ''), 'ref': '', 'error': reason[:300]})
        im['attachments'] = normalized
    if meta.get('quoted_text'):
        mapped = im_store.outgoing_reference(im, str(meta['quoted_text']))
        if mapped:
            im['reply_to'] = mapped
    im['capabilities'] = sender_capabilities(channel)
    if not im_store.durable_ingest(im, ts):
        return None
    SLog.i(TAG, f"ingest channel={channel} msg_id={msg_id} len={len(body)} turns={len(turns)}")
    hook = _hook
    if hook is not None:
        try:
            hook(im)
            return im
        except Exception as exc:
            SLog.w(TAG, f"hook 失败，转入暂存: {type(exc).__name__}")
    return im


# ---------------- 出站发送注册表 ----------------
# 渠道各自注册发送函数，service 只按 channel 查表，不写 if 渠道 分支。
# text: (chat_id, sender_id, reply_ctx, text) -> None，失败抛异常
# image: (chat_id, sender_id, reply_ctx, data: bytes, mime: str) -> None；没有图片能力的渠道不注册
SenderText = Callable[[str, str, dict[str, str], str], None]
SenderImage = Callable[[str, str, dict[str, str], bytes, str], None]
_text_senders: dict[str, SenderText] = {}
_image_senders: dict[str, SenderImage] = {}


def register_sender(channel: str, text: SenderText, image: SenderImage | None = None) -> None:
    _text_senders[channel] = text
    if image is not None:
        _image_senders[channel] = image


def sender_capabilities(channel: str) -> dict[str, Any]:
    if channel == 'feishu_cli':
        return {'markdown': False, 'cards': False, 'buttons': False, 'streaming': False,
                'threads': False, 'max_characters': 1800, 'length_unit': 'characters',
                'attachment_types': ['image', 'audio', 'file'], 'version': 1}
    if channel == 'feishu':
        from mino_scout.plugins import im_feishu
        if im_feishu.ready():
            return {'markdown': False, 'cards': True, 'buttons': False, 'streaming': True,
                    'threads': True, 'max_characters': 1800, 'length_unit': 'characters',
                    'attachment_types': ['image', 'audio'], 'version': 2}
    return {"markdown": False, "cards": False, "buttons": False, "streaming": False,
            "threads": False, "max_characters": 1800, "length_unit": "characters",
            "attachment_types": ["image", "audio"] if channel in _image_senders else [], "version": 1}


def text_sender(channel: str) -> SenderText | None:
    return _text_senders.get(channel)


def image_sender(channel: str) -> SenderImage | None:
    return _image_senders.get(channel)


# ---------------- 本机回环入口（收 mino-bridge 转来的消息） ----------------
import hmac  # noqa: E402
import json  # noqa: E402
import secrets  # noqa: E402
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

_BODY_MAX = 4 * (8 * 1024 * 1024 * 4 // 3 + 1024) + 65536
_CHANNELS = {"feishu", "wecom", "dingtalk"}
_loop_lock = threading.Lock()
_loop_server: ThreadingHTTPServer | None = None
_loop_token = ""


class _Handler(BaseHTTPRequestHandler):
    server_version = "MinoImBridge"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # 不打请求行以外的任何东西，更不打正文
        return

    def _reply(self, code: int, obj: dict[str, Any]) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        token = _loop_token
        got = str(self.headers.get("X-Mino-Token") or "")
        if not token or not hmac.compare_digest(got.encode(), token.encode()):
            self._reply(401, {"ok": False, "error": "unauthorized"})
            return
        if self.path != "/ingest":
            self._reply(404, {"ok": False, "error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n <= 0 or n > _BODY_MAX:
            self._reply(413 if n > 0 else 400, {"ok": False, "error": "bad length"})
            return
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("not object")
        except Exception:
            self._reply(400, {"ok": False, "error": "bad json"})
            return
        self._reply(*_handle_ingest(body))

    def do_GET(self) -> None:  # noqa: N802
        self._reply(405, {"ok": False, "error": "method not allowed"})


def _handle_ingest(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    channel = str(body.get("channel") or "")
    chat_id = str(body.get("chat_id") or "")
    if channel not in _CHANNELS or not chat_id:
        SLog.w(TAG, f"loopback reject channel={channel or '-'} has_chat={bool(chat_id)}")
        return 400, {"ok": False, "error": "bad channel or chat_id"}
    chat_type = "group" if body.get("chat_type") == "group" else "private"
    mentioned = bool(body.get("mentioned"))
    if chat_type == "group" and not mentioned:  # 群里没 @ 的不上报（§7.2）
        return 200, {"ok": True, "dropped": "not_mentioned"}
    ctx = body.get("reply_ctx") if isinstance(body.get("reply_ctx"), dict) else {}
    try:
        from mino_scout.plugins import langbot_host

        platform = {"feishu": "lark", "wecom": "wecom", "dingtalk": "dingtalk"}.get(channel)
        instance = str(body.get("bot_uuid") or ctx.get("bot_uuid") or "")
        actual = str((langbot_host.channel_status().get("platforms") or {}).get(platform, {}).get("instance_id") or "")
        if not instance or instance != actual or ctx.get("bot_uuid") != instance:
            return 403, {"ok": False, "error": "unverified robot instance"}
        if channel == 'feishu':
            from mino_scout.plugins import im_feishu
            mentions = [str(value) for value in body.get('mention_targets') or []]
            try:
                verified = im_feishu.verify(langbot_host._node_id, mentions if chat_type == 'group' else None)
            except Exception:
                verified = False
            if chat_type == 'group' and not verified:
                return 200, {'ok': True, 'dropped': 'not_mentioned_bot'}
        if chat_type == 'group' and channel != 'feishu':
            from mino_scout.plugins.state import plain_value
            field = 'wecombot.BotId' if channel == 'wecom' else 'dingtalk.robot_code'
            bot_id = plain_value('im', 'langbot', field)
            if not bot_id or bot_id not in [str(value) for value in body.get('mention_targets') or []]:
                return 200, {'ok': True, 'dropped': 'unverified_bot_mention'}
        langbot_host.note_message(channel)
    except Exception:
        return 403, {"ok": False, "error": "unverified robot instance"}
    try:
        im = ingest(
            channel, chat_id, chat_type,
            str(body.get("sender_id") or ""), str(body.get("msg_id") or ""),
            str(body.get("text") or ""), mentioned,
            {str(k): str(v) for k, v in ctx.items()}, tenant=str(body.get("tenant") or ""), instance_id=instance, metadata=body,
        )
    except Exception as exc:
        SLog.e(TAG, f"loopback ingest 失败: {type(exc).__name__}")
        return 500, {"ok": False, "error": "ingest failed"}
    return 200, {"ok": True, "duplicate": im is None}


def start_loopback() -> tuple[str, str]:
    """起回环入口，返回 (url, token)。已经起过就原样返回。只绑 127.0.0.1。"""
    global _loop_server, _loop_token
    with _loop_lock:
        if _loop_server is None:
            _loop_token = secrets.token_urlsafe(32)
            srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, name="im-loopback", daemon=True).start()
            _loop_server = srv
            SLog.i(TAG, f"loopback listening 127.0.0.1:{srv.server_address[1]}")
        return f"http://127.0.0.1:{_loop_server.server_address[1]}/ingest", _loop_token


def stop_loopback() -> None:
    global _loop_server, _loop_token
    with _loop_lock:
        srv, _loop_server, _loop_token = _loop_server, None, ""
    if srv is not None:
        srv.shutdown()
        srv.server_close()
