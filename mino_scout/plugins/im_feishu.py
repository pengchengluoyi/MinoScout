"""飞书原生卡片/线程/草稿更新。复用当前节点自己的机器人配置。"""
from __future__ import annotations
import hashlib
import json
import threading
import time
from typing import Any
import httpx

_BASE = 'https://open.feishu.cn/open-apis/'
_lock = threading.RLock()
_cache: dict[str, dict] = {}


def invalidate() -> None:
    with _lock:
        _cache.clear()


def _credentials(node_id: str) -> tuple[str, str]:
    from mino_scout.plugins.state import plain_value, secret_value
    app = plain_value('im', 'langbot', 'lark.app_id')
    secret = secret_value(node_id, 'im', 'langbot', 'lark.app_secret')
    if not app or not secret:
        raise ValueError('飞书机器人尚未配置完成')
    return app, secret


def _request(method: str, path: str, *, token: str = '', body=None, params=None) -> dict:
    try:
        response = httpx.request(method, _BASE + path, json=body, params=params,
                                headers={'Authorization': 'Bearer ' + token} if token else {},
                                timeout=20, trust_env=False)
        data = response.json()
    except Exception as exc:
        raise RuntimeError('飞书接口暂时无法确认，请查询消息状态后再处理') from exc
    if response.status_code >= 400 or not isinstance(data, dict) or data.get('code', 0) != 0:
        raise RuntimeError('飞书接口拒绝本次操作，请检查机器人权限与会话访问设置')
    return data


def _account(node_id: str) -> dict:
    app, secret = _credentials(node_id)
    key = hashlib.sha256((node_id + ':' + app + ':' + secret).encode()).hexdigest()
    with _lock:
        cached = _cache.get(key)
        if cached and cached['expires'] > time.time():
            return cached
        auth = _request('POST', 'auth/v3/tenant_access_token/internal', body={'app_id': app, 'app_secret': secret})
        token = str(auth.get('tenant_access_token') or '')
        if not token:
            raise RuntimeError('飞书未返回可用的机器人访问凭证')
        info = _request('GET', 'bot/v3/info', token=token)
        bot_id = str((info.get('bot') or (info.get('data') or {}).get('bot') or {}).get('open_id') or '')
        if not bot_id:
            raise RuntimeError('无法验证当前飞书机器人的身份')
        cached = {'token': token, 'bot_id': bot_id, 'expires': time.time() + max(0, min(int(auth.get('expire') or 3600) - 60, 3600))}
        _cache[key] = cached
        return cached


def verify(node_id: str, mentions: list[str] | None = None) -> bool:
    account = _account(node_id)
    return mentions is None or account['bot_id'] in mentions


def ready() -> bool:
    with _lock:
        return any(row['expires'] > time.time() for row in _cache.values())


def card_payload(text: str, card: dict | None = None, *, draft: bool = False) -> dict:
    card = card or {}
    kind = card.get('card_type', 'dialogue')
    title = '回复草稿 · 尚未执行' if draft else str((card.get('header') or {}).get('title') or '助手回复')
    template = 'orange' if kind == 'clarification' else 'blue' if kind == 'notification' else 'turquoise'
    elements = [{'tag': 'div', 'text': {'tag': 'plain_text', 'content': str(text)[:10000]}}]
    if not draft:
        from urllib.parse import urlsplit
        links = []
        for action in card.get('actions') or []:
            if not isinstance(action, dict):
                continue
            url = str(action.get('url') or '')
            try:
                parsed = urlsplit(url)
                valid = parsed.scheme in ('http', 'https') and bool(parsed.hostname) and not parsed.username and not parsed.password
            except ValueError:
                valid = False
            if valid:
                links.append({'tag': 'button', 'text': {'tag': 'plain_text', 'content': str(action.get('label') or '查看详情')[:100]},
                              'type': 'primary', 'url': url})
        if links:
            elements.append({'tag': 'action', 'actions': links[:3]})
    return {'config': {'wide_screen_mode': True}, 'header': {'title': {'tag': 'plain_text', 'content': title[:100]}, 'template': template},
            'elements': elements}


def send(node_id: str, chat_id: str, sender_id: str, ctx: dict, text: str, card: dict, delivery_id: str) -> dict:
    account = _account(node_id)
    content = json.dumps(card_payload(text, card), ensure_ascii=False)
    thread = str(ctx.get('thread_id') or '')
    parent = str(ctx.get('source_message_id') or '') if thread else ''
    if thread and not parent.startswith('om_'):
        parent = thread if thread.startswith('om_') else ''
        if not parent:
            raise ValueError('当前线程缺少可信的原消息引用，请回复线程中的原消息')
    uuid = hashlib.sha256(str(delivery_id).encode()).hexdigest()[:32]
    if parent:
        data = _request('POST', 'im/v1/messages/' + parent + '/reply', token=account['token'],
                        body={'msg_type': 'interactive', 'content': content, 'reply_in_thread': True, 'uuid': uuid})
    else:
        group = ctx.get('target_type') == 'group' or ctx.get('chat_type') == 'group'
        target = chat_id if group else sender_id
        data = _request('POST', 'im/v1/messages', token=account['token'], params={'receive_id_type': 'chat_id' if group else 'open_id'},
                        body={'receive_id': target, 'msg_type': 'interactive', 'content': content, 'uuid': uuid})
    mid = str((data.get('data') or {}).get('message_id') or '')
    if not mid:
        raise RuntimeError('飞书发送结果未返回消息编号，请先查询状态')
    return {'provider_message_id': mid}


def update(node_id: str, message_id: str, text: str, card: dict | None = None, *, draft: bool = False) -> dict:
    if not message_id.startswith('om_'):
        raise ValueError('无效的飞书消息引用')
    _request('PATCH', 'im/v1/messages/' + message_id, token=_account(node_id)['token'],
             body={'content': json.dumps(card_payload(text, card, draft=draft), ensure_ascii=False)})
    return {'provider_message_id': message_id}


def resource(node_id: str, message_id: str, file_key: str, kind: str) -> tuple[bytes, str]:
    from mino_scout.plugins.im_attachments import MAX_BYTES
    from urllib.parse import quote
    if not message_id.startswith('om_') or not file_key or len(file_key) > 256 or kind not in ('image', 'audio'):
        raise ValueError('附件缺少可信的消息与资源引用')
    url = _BASE + 'im/v1/messages/' + quote(message_id, safe='') + '/resources/' + quote(file_key, safe='')
    with httpx.stream('GET', url, params={'type': 'image' if kind == 'image' else 'file'},
                      headers={'Authorization': 'Bearer ' + _account(node_id)['token']}, timeout=20, trust_env=False) as response:
        if response.status_code != 200:
            raise ValueError('当前机器人无法读取这份附件，请检查会话资源权限')
        data = bytearray()
        for piece in response.iter_bytes():
            data.extend(piece)
            if len(data) > MAX_BYTES:
                raise ValueError('附件超过 8 MB')
        return bytes(data), response.headers.get('content-type', '').split(';', 1)[0]
