"""微信 iLink 扫码和发消息。bot_token 只进保险库，回执里不带。"""
from __future__ import annotations

import base64
import random
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable
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
_hook: Callable[[dict[str, Any]], None] | None = None
_node_id = ""


def bind_node(node_id: str) -> None:
    global _node_id
    _node_id = str(node_id or "")


def set_message_hook(fn: Callable[[dict[str, Any]], None] | None) -> None:
    global _hook
    _hook = fn


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


def ensure_listener() -> None:
    global _thread
    if not _token():
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
    cursor = plain_value("bot", "wechat", "get_updates_buf")
    try:
        _post("ilink/bot/msg/notifystart", {}, timeout=10)
    except Exception as exc:
        SLog.w(TAG, f"wechat notifystart: {exc}")
    while not _stop.is_set():
        try:
            data = _post("ilink/bot/getupdates", {"get_updates_buf": cursor}, timeout=40)
            cursor = str(data.get("get_updates_buf") or cursor)
            remember_plain("bot", "wechat", {"get_updates_buf": cursor})
            for msg in data.get("msgs") or []:
                if isinstance(msg, dict):
                    _on_message(msg)
        except httpx.TimeoutException:
            continue
        except Exception as exc:
            SLog.w(TAG, f"wechat poll: {exc}")
            if "过期" in str(exc):
                break
            _stop.wait(3)
    SLog.i(TAG, "wechat listener stopped")


def _on_message(msg: dict[str, Any]) -> None:
    if int(msg.get("message_type") or 0) == 2:
        return
    text = _message_text(msg)
    user_id = str(msg.get("from_user_id") or "").strip()
    context = str(msg.get("context_token") or "").strip()
    if not text or not user_id or not context:
        return
    hook = _hook
    if hook is None:
        return
    hook({
        "text": text[:2000],
        "from_user_id": user_id,
        "context_token": context,
    })


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
