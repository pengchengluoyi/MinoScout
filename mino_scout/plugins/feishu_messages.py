"""Account-scoped CLI private-message transport; no LangBot model pipeline."""
from __future__ import annotations
import json
import hashlib
import os
import stat
from collections import deque
import subprocess
import threading
import time
from pathlib import Path
from mino_scout.plugins import feishu_auth as auth, im_bridge
from mino_scout.plugins.state import plain_value, remember_plain

_lock = threading.RLock()
_thread = None
_process = None
_stop = threading.Event()
_home: Path | None = None
_connection_ready = False
_status = {"enabled": False, "connected": False, "connection_verified": False,
           "holding": True, "instance_id": "", "last_message_at": 0, "error": "", "authorized_sender_id": ""}


def status() -> dict:
    with _lock:
        return dict(_status)


def parse_event(row: dict) -> dict | None:
    # The installed CLI emits normalized, flat NDJSON (event schema), not raw WS envelopes.
    if row.get("type") != "im.message.receive_v1" or row.get("sender_type") != "user":
        return None
    if row.get("chat_type") != "p2p":
        return None
    keys = ("message_id", "chat_id", "sender_id")
    if not all(isinstance(row.get(k), str) and row[k] for k in keys):
        return None
    from mino_scout.plugins.feishu_media import content
    text, attachments, error = content(str(row.get('message_type') or ''), row.get('content'))
    return {k: row[k] for k in keys} | {"text": text, "attachments": attachments, "media_error": error,
                                       "reply_to": str(row.get('reply_to') or ''),
                                       "thread_id": str(row.get("thread_id") or "")}


def _update(**values):
    with _lock:
        _status.update(values)


def _ipc_home(home: Path) -> Path:
    """Keep Unix socket addresses short without moving or copying credentials."""
    if os.name == "nt":
        return home
    target = home.resolve(strict=True)
    # macOS sockaddr_un.sun_path is 104 bytes; account directories exceed it.
    base = Path("/tmp") / f"mino-feishu-{os.getuid()}"
    base.mkdir(mode=0o700, exist_ok=True)
    info = base.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError("CLI 本地消息目录不安全，无法启动监听")
    alias = base / hashlib.sha256(str(target).encode()).hexdigest()[:20]
    try:
        alias.symlink_to(target, target_is_directory=True)
    except FileExistsError:
        pass
    if not alias.is_symlink() or alias.lstat().st_uid != os.getuid() or alias.resolve(strict=True) != target:
        raise RuntimeError("CLI 本地消息路径冲突，无法启动监听")
    return alias


def _listen_failure(code: int, diagnostics) -> str:
    # Inspect bounded diagnostics in memory, never expose raw tokens/URLs/body.
    detail = "\n".join(diagnostics).lower()
    if "unix" in detail and any(word in detail for word in ("invalid argument", "too long", "bind:")):
        return "CLI 本地消息套接字创建失败：路径超出系统限制"
    if any(word in detail for word in ("missing_scope", "missing required scopes", "permission denied", "99991672")):
        return "CLI 消息权限不足，请检查应用的消息接收和发送权限"
    if code == 3:
        return "CLI 消息鉴权失败，请核对应用授权状态"
    if code == 4:
        return "CLI 消息连接失败，请检查网络后重新连接节点"
    if code == 5:
        return "CLI 本地消息服务启动失败（CLI 内部错误），不是登录授权验证失败"
    return f"CLI 消息监听退出（退出码 {code}），尚未确认具体原因"


def _run(home: Path):
    global _process
    try:
        info = auth.run_json(["auth", "status", "--json", "--verify"], home=home)
        app = str(info.get("appId") or "")
        if not app or info.get("identity") not in ("bot", "user") or info.get("verified") is not True:
            raise RuntimeError("CLI 应用鉴权未通过，请先完成授权")
        if (plain_value("im", "langbot", "lark.enabled") == "1"
                and plain_value("im", "langbot", "lark.app_id") == app):
            raise RuntimeError("同一个应用已由 LangBot 接收；请只启用一种接收方式")
        instance = "cli:" + app
        authorized_sender = ""
        if info.get("identity") == "user":
            identity = auth.run_json(["whoami", "--as", "user", "--json"], home=home)
            if (identity.get("appId") == app and identity.get("identity") == "user"
                    and identity.get("available") is True):
                authorized_sender = str((identity.get("onBehalfOf") or {}).get("openId") or "")
        _update(instance_id=instance, authorized_sender_id=authorized_sender)
        entry = auth._safe_entry(home.parent, "invoke")
        env = auth._env(home)
        env["LARKSUITE_CLI_CONFIG_DIR"] = str(_ipc_home(home))
        while not _stop.is_set():
            # A bounded consumer ignores stdin EOF. Renew it without spawning duplicate listeners.
            diagnostics = deque(maxlen=16)
            proc = subprocess.Popen([str(entry), "event", "consume", "im.message.receive_v1",
                                     "--as", "bot", "--timeout", "5m"],
                                    cwd=home.parent, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace")
            with _lock:
                _process = proc
                if _stop.is_set():
                    proc.terminate()
            for line in proc.stdout:
                if _stop.is_set():
                    break
                try:
                    row = json.loads(line)
                    event = parse_event(row)
                    if isinstance(row, dict) and row.get("error"):
                        diagnostics.append(line[:2000])
                except (ValueError, TypeError, AttributeError):
                    diagnostics.append(line[:2000])
                    continue
                if event is None:
                    continue
                _update(connected=True, connection_verified=True, last_message_at=int(time.time()), error="")
                im_bridge.ingest("feishu_cli", event["chat_id"], "private", event["sender_id"],
                                 event["message_id"], event["text"], True,
                                 {"transport": "feishu_cli", "instance_id": instance,
                                  "source_message_id": event["message_id"]},
                                 instance_id=instance, metadata={key: event[key] for key in
                                     ('thread_id', 'reply_to', 'attachments', 'media_error')})
            proc.stdout.close()
            code = proc.wait()
            with _lock:
                _process = None
            if _stop.is_set():
                break
            if code:
                raise RuntimeError(_listen_failure(code, diagnostics))
            _update(connected=False, connection_verified=False)
            if _stop.wait(1):
                break
    except Exception as exc:
        _update(error=str(exc) if isinstance(exc, RuntimeError) else "CLI 消息监听失败：" + type(exc).__name__)
    finally:
        with _lock:
            proc, _process = _process, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        _update(connected=False, connection_verified=False)


def stop():
    _stop.set()
    with _lock:
        proc, thread = _process, _thread
    if proc and proc.poll() is None:
        proc.terminate()
    if thread and thread is not threading.current_thread():
        thread.join(timeout=6)
    _update(enabled=False, connected=False, connection_verified=False, authorized_sender_id="")


def set_connection(ready: bool):
    global _connection_ready
    _connection_ready = ready
    if ready:
        reconcile()
    else:
        stop()


def reconcile():
    global _thread, _home
    home = auth.root()
    if _home is not None and home != _home:
        stop()
    # Message reception is part of an authorized CLI connection, not an opt-in.
    # Ignore legacy messages_enabled values, including a previously saved "0".
    enabled = _connection_ready and auth.ready()
    holding = plain_value("cli", "feishu", "im_hold") != "0"
    from mino_scout.plugins.state import _item, installed
    if not enabled or not holding or not installed("cli", "feishu") or _item("cli", "feishu").get("import_blocked"):
        stop()
        _update(enabled=enabled, holding=holding)
        return
    with _lock:
        if _thread and _thread.is_alive():
            return
        _home = home
        _stop.clear()
        _status.update(enabled=True, holding=True, connected=False, connection_verified=False,
                       instance_id="", error="", last_message_at=0, authorized_sender_id="")
        _thread = threading.Thread(target=_run, args=(home,), name="feishu-cli-messages", daemon=True)
        _thread.start()


def action(name: str) -> dict:
    if name == "messages_start":
        remember_plain("cli", "feishu", {"im_hold": ""})
        reconcile()
    elif name == "messages_stop":
        remember_plain("cli", "feishu", {"im_hold": "0"})
        stop()
    elif name == "messages_status":
        if not status()["enabled"]:
            reconcile()
    else:
        raise ValueError("不支持的消息操作")
    return {"messages": status()}


def send(chat_id: str, ctx: dict, text: str, delivery_id: str) -> dict:
    state = status()
    if (not state["enabled"] or not state["holding"] or not state["instance_id"]
            or ctx.get("instance_id") != state["instance_id"] or _home != auth.root()):
        raise ValueError("CLI 消息路由已失效，请重新确认连接")
    info = auth.run_json(["auth", "status", "--json"], home=_home)
    if "cli:" + str(info.get("appId") or "") != state["instance_id"]:
        raise ValueError("CLI 应用已变化，拒绝向旧会话发送")
    import hashlib
    args = ["im", "+messages-send", "--chat-id", chat_id, "--msg-type", "text", "--content", json.dumps({"text": text}, ensure_ascii=False), "--as", "bot",
            "--idempotency-key", hashlib.sha256(delivery_id.encode()).hexdigest()[:48]]
    data = auth.run_json(args, home=_home)
    return {"ok": True, "provider_message_id": str(data.get("message_id") or (data.get("data") or {}).get("message_id") or "")}
