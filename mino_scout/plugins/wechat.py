"""微信 iLink 扫码和发消息。bot_token 只进保险库，回执里不带。"""
from __future__ import annotations

import base64
import hashlib
import random
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urljoin

import httpx

from mino_scout.log import SLog
from mino_scout.plugins.state import plain_value, remember_plain, remember_secret, secret_value

TAG = "Wechat"
LOGIN_BASE = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "2.4.6"
BOT_AGENT = "MinoScout/1.0.0"
TOKEN_EXPIRED = -14

_lock = threading.RLock()
_stop = threading.Event()
_thread: threading.Thread | None = None
_login: dict[str, Any] = {}
_node_id = ""
_HOLDER_IM = "im"  # power.PowerGuard 的 holder 名：监听线程在跑就保持防休眠
_last_message_at = 0
_last_error = ""


def bind_node(node_id: str) -> None:
    global _node_id
    _node_id = str(node_id or "")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _token() -> str:
    return secret_value(_node_id, "bot", "wechat", "bot_token")


def _base() -> str:
    return plain_value("bot", "wechat", "baseurl") or LOGIN_BASE


def public_status() -> dict[str, Any]:
    with _lock:
        status = str(_login.get("status") or "")
        img = str(_login.get("qrcode_img") or "")
        err = str(_login.get("error") or "")
    logged = bool(_token())
    return {
        "logged_in": logged,
        "status": status or ("confirmed" if logged else "idle"),
        "qrcode_img": _as_image_src(img),
        "need_verify": status in ("need_verifycode", "need_verify"),
        "error": err,
        "ilink_user_id": plain_value("bot", "wechat", "ilink_user_id"),
    }


def channel_status() -> dict[str, Any]:
    """随插件快照上报。不读钥匙串（心跳频繁），只看清单标记和线程。不含正文。"""
    from mino_scout.plugins.state import _item

    thread = _thread
    has_token = bool(_item("bot", "wechat").get("has_bot_token"))
    alive = thread is not None and thread.is_alive()
    last = _last_message_at
    if not last:
        try:
            last = int(plain_value("bot", "wechat", "last_message_at") or 0)
        except ValueError:
            last = 0
    with _lock:
        err = _last_error or str(_login.get("error") or "")
    out = {"connected": bool(alive and has_token), "last_message_at": last, "error": err[:200]}
    if is_held_off():
        out["holding"] = False  # Nexus 仲裁让出持有；缺省视为持有
    return out


def start_qr() -> dict[str, Any]:
    with _lock:
        _login.clear()
        _login.update(status="wait", qrcode="", qrcode_img="", error="")
    url = f"{LOGIN_BASE}/ilink/bot/get_bot_qrcode?bot_type=3"
    last = ""
    for method, body in (("GET", None), ("POST", {"local_token_list": []})):
        try:
            data = _request(method, url, json_body=body, timeout=12)
            qrcode, img = _extract_qr(data)
            if qrcode or img:
                with _lock:
                    _login.update(status="wait", qrcode=qrcode, qrcode_img=img, error="")
                return public_status()
            last = str(data.get("errmsg") or data.get("error") or "")
        except Exception as exc:
            last = str(exc)
            SLog.w(TAG, f"get qr {method}: {exc}")
    raise RuntimeError(last or "没有拿到微信登录二维码")


def poll(verify_code: str = "") -> dict[str, Any]:
    with _lock:
        qrcode = str(_login.get("qrcode") or "").strip()
        status = str(_login.get("status") or "")
    if not qrcode or status == "confirmed":
        return public_status()
    query = f"qrcode={quote(qrcode, safe='')}"
    code = str(verify_code or "").strip()
    if code:
        query += f"&verify_code={quote(code, safe='')}"
    data = _request("GET", f"{LOGIN_BASE}/ilink/bot/get_qrcode_status?{query}", timeout=25)
    st = str(data.get("status") or "").strip() or "wait"
    if st in ("scaned_but_redirect", "binded_redirect"):
        return public_status()
    if st in ("confirmed", "confirm", "success"):
        _apply_confirmed(data)
        return public_status()
    if st in ("expired", "expire", "timeout"):
        with _lock:
            _login.update(status="expired", error="二维码过期了，重新扫一次")
        return public_status()
    img = str(data.get("qrcode_img_content") or data.get("qrcode_img") or "").strip()
    with _lock:
        _login["status"] = st
        _login["error"] = ""
        if img:
            _login["qrcode_img"] = img
    return public_status()


def logout() -> dict[str, Any]:
    if _token():
        try:
            _post("ilink/bot/msg/notifystop", {}, timeout=8)
        except Exception:
            pass
    remember_secret(_node_id, "bot", "wechat", "bot_token", "")
    remember_plain("bot", "wechat", {"baseurl": "", "ilink_user_id": "", "ilink_bot_id": ""})
    stop_listener()
    with _lock:
        _login.clear()
        _login["status"] = "idle"
    return public_status()


def send_text(*, to_user_id: str, context_token: str, text: str) -> None:
    body = str(text or "").strip()
    user_id = str(to_user_id or "").strip()
    token = str(context_token or "").strip()
    if not body or not user_id or not token:
        raise RuntimeError("发送微信消息缺少收件人或正文")
    if not _token():
        raise RuntimeError("这台机器还没登录微信")
    _post(
        "ilink/bot/sendmessage",
        {
            "msg": {
                "to_user_id": user_id,
                "from_user_id": "",
                "client_id": uuid.uuid4().hex,
                "message_type": 2,
                "message_state": 2,
                "context_token": token,
                "item_list": [{"type": 1, "text_item": {"text": body}}],
            }
        },
        timeout=20,
    )


def is_held_off() -> bool:
    """Nexus 仲裁让出持有（im_hold=false）。持久化，重启后仍然生效；没收到过指令就是 False。"""
    return plain_value("bot", "wechat", "im_hold") == "0"


def set_hold(hold: bool) -> None:
    """hold=False：停监听线程但不登出、不清 token；hold=True：恢复监听。"""
    remember_plain("bot", "wechat", {"im_hold": "" if hold else "0"})
    if hold:
        ensure_listener()
    else:
        stop_listener()
    SLog.i(TAG, f"wechat im_hold={hold}")


def ensure_listener() -> None:
    global _thread
    if not _token() or is_held_off():
        return
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_poll_loop, name="wechat-ilink", daemon=True)
        _thread.start()


def stop_listener() -> None:
    global _thread
    _stop.set()
    thread = _thread
    _thread = None
    if thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=1.5)


def _apply_confirmed(data: dict[str, Any]) -> None:
    token = str(data.get("bot_token") or data.get("token") or "").strip()
    if not token:
        raise RuntimeError("扫码成功但没有登录凭证")
    remember_secret(_node_id, "bot", "wechat", "bot_token", token)
    remember_plain("bot", "wechat", {
        "baseurl": str(data.get("baseurl") or LOGIN_BASE).rstrip("/"),
        "ilink_user_id": str(data.get("ilink_user_id") or ""),
        "ilink_bot_id": str(data.get("ilink_bot_id") or ""),
    })
    with _lock:
        _login.update(status="confirmed", qrcode="", qrcode_img="", error="")
    SLog.i(TAG, "wechat login confirmed")
    ensure_listener()


def _poll_loop() -> None:
    from mino_scout.power import get_guard

    get_guard().acquire(_HOLDER_IM)
    try:
        _poll_loop_inner()
    finally:
        get_guard().release(_HOLDER_IM)


def _poll_loop_inner() -> None:
    global _last_error
    cursor = plain_value("bot", "wechat", "get_updates_buf")
    try:
        _post("ilink/bot/msg/notifystart", {}, timeout=10)
    except Exception as exc:
        SLog.w(TAG, f"wechat notifystart: {exc}")
    while not _stop.is_set():
        try:
            data = _post("ilink/bot/getupdates", {"get_updates_buf": cursor}, timeout=40)
            cursor = str(data.get("get_updates_buf") or cursor)
            _last_error = ""
            remember_plain("bot", "wechat", {"get_updates_buf": cursor})
            for msg in data.get("msgs") or []:
                if isinstance(msg, dict):
                    _on_message(msg)
        except httpx.TimeoutException:
            continue
        except Exception as exc:
            SLog.w(TAG, f"wechat poll: {exc}")
            _last_error = str(exc)[:200]
            if "过期" in str(exc):
                break
            _stop.wait(3)
    SLog.i(TAG, "wechat listener stopped")


def _on_message(msg: dict[str, Any]) -> None:
    global _last_message_at
    if int(msg.get("message_type") or 0) == 2:
        return
    text = _message_text(msg)
    user_id = str(msg.get("from_user_id") or "").strip()
    context = str(msg.get("context_token") or "").strip()
    if not text or not user_id or not context:
        return
    from mino_scout.plugins import im_bridge

    _last_message_at = int(time.time())
    try:
        remember_plain("bot", "wechat", {"last_message_at": _last_message_at})
    except Exception:
        pass
    im_bridge.ingest(
        channel="wechat",
        chat_id=user_id,
        chat_type="private",
        sender_id=user_id,
        msg_id=_message_uid(msg, user_id, text),
        text=text,
        mentioned=True,
        reply_ctx={"context_token": context},
    )


def _message_uid(msg: dict[str, Any], user_id: str, text: str) -> str:
    """去重键：优先 iLink 自带的唯一字段，都没有才退回 用户+正文+时间 的哈希。"""
    for key in ("message_id", "msg_id", "svr_msg_id", "seq", "client_id"):
        val = msg.get(key)
        if val not in (None, "", 0):
            return f"{user_id}:{key}:{val}"
    stamp = msg.get("create_time_ms") or msg.get("create_time") or int(time.time())
    return "h:" + hashlib.sha1(f"{user_id}\n{text}\n{stamp}".encode()).hexdigest()[:24]


def _message_text(msg: dict[str, Any]) -> str:
    items = msg.get("item_list") if isinstance(msg.get("item_list"), list) else []
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        row = item.get("text_item") if isinstance(item.get("text_item"), dict) else {}
        text = str(row.get("text") or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _post(path: str, body: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    token = _token()
    if not token:
        raise RuntimeError("这台机器还没登录微信")
    payload = dict(body)
    payload.setdefault("base_info", {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT})
    data = _request("POST", f"{_base().rstrip('/')}/{path.lstrip('/')}", token=token, json_body=payload, timeout=timeout)
    err = data.get("errcode", data.get("ret"))
    if err == TOKEN_EXPIRED:
        remember_secret(_node_id, "bot", "wechat", "bot_token", "")
        raise RuntimeError("微信登录过期，请重新扫码")
    return data


def _request(
    method: str,
    url: str,
    *,
    token: str = "",
    json_body: dict[str, Any] | None = None,
    timeout: float = 20,
) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "iLink-App-Id": "bot",
        "iLink-App-ClientVersion": "132102",
        "X-WECHAT-UIN": base64.b64encode(str(random.randint(0, 0xFFFFFFFF)).encode()).decode(),
        "SKRouteTag": "1001",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = httpx.request(method, url, headers=headers, json=json_body, timeout=timeout)
    data = resp.json() if resp.content else {}
    if not isinstance(data, dict):
        data = {}
    if resp.status_code >= 400 and "ret" not in data and "errcode" not in data:
        raise RuntimeError(f"微信接口 HTTP {resp.status_code}")
    return data


def _extract_qr(data: dict[str, Any]) -> tuple[str, str]:
    row = data.get("data") if isinstance(data.get("data"), dict) else data
    qrcode = str(row.get("qrcode") or row.get("qrcode_id") or "").strip()
    img = str(
        row.get("qrcode_img_content")
        or row.get("qrcode_img")
        or row.get("qrcode_url")
        or ""
    ).strip()
    if not img and qrcode.startswith(("http://", "https://", "data:")):
        img = qrcode
    return qrcode, img


def _as_image_src(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://", "data:")):
        return text
    if text.startswith("/"):
        return urljoin(LOGIN_BASE + "/", text.lstrip("/"))
    compact = "".join(text.split())
    if compact.startswith("/9j/") or compact.startswith("iVBORw0K") or len(compact) > 80:
        mime = "image/jpeg" if compact.startswith("/9j/") else "image/png"
        return f"data:{mime};base64,{compact}"
    return text


def _im_text_sender(chat_id: str, sender_id: str, ctx: dict[str, str], text: str) -> None:
    send_text(
        to_user_id=chat_id or sender_id,
        context_token=str(ctx.get("context_token") or ""),
        text=text,
    )


# iLink 现有发送链路只有文本（item type=1），没有图片上传，所以不注册 image sender
from mino_scout.plugins import im_bridge as _im_bridge  # noqa: E402

_im_bridge.register_sender("wechat", _im_text_sender)
