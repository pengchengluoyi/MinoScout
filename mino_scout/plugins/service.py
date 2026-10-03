"""节点指令和具名插件能力。密钥只在本进程里读，不写进回执。"""
from __future__ import annotations

import time
from typing import Any

from mino_scout import protocol as P
from mino_scout.executors.base import make_event_result, now_iso
from mino_scout.log import SLog
from mino_scout.plugins.gmail_otp import GmailOtpError, fetch_otp_via_imap
from mino_scout.plugins.install import PluginInstallError, install, remove
from mino_scout.plugins.secret_store import SecretStoreError
from mino_scout.plugins.state import apply_config, configured, plain_value, secret_value, status_list
from mino_scout.schemas import EventResult, EventStatus

TAG = "PluginService"
_GMAIL_HINT = "请在这台 Scout 节点的「邮箱 / Gmail」里填写收件箱和应用专用密码"


def handle_node_plugin(core: Any, req: P.Execute, cmd: str, event: Any, started: str) -> EventResult:
    params = dict(req.params or {})
    kind = str(params.get("class") or params.get("kind") or "").strip()
    plugin_id = str(params.get("id") or params.get("plugin_id") or "").strip()
    SLog.i(TAG, f"{cmd} {kind}/{plugin_id} node={core.node_id}")
    try:
        if cmd == "plugin_config":
            values = params.get("values") if isinstance(params.get("values"), dict) else {}
            clear = [str(x) for x in (params.get("clear") or []) if str(x).strip()]
            apply_config(core.node_id, kind, plugin_id, values, clear)
            summary = "已保存" if configured(core.node_id, kind, plugin_id) else "已保存，还有必填项空着"
        elif cmd == "plugin_install":
            summary = install(core.node_id, kind, plugin_id)
        elif cmd == "plugin_remove":
            summary = remove(core.node_id, kind, plugin_id)
        else:
            summary = f"不支持的插件指令 {cmd}"
            return _fail(event, started, summary, cmd)
    except (PluginInstallError, SecretStoreError, ValueError, OSError) as exc:
        return _fail(event, started, str(exc), cmd)
    return make_event_result(
        event,
        status=EventStatus.PASS,
        executor_used="core",
        started_at=started,
        elapsed_ms=0,
        summary=summary,
        raw_response={
            "command": cmd,
            "class": kind,
            "id": plugin_id,
            "configured": configured(core.node_id, kind, plugin_id),
            "plugins": status_list(core.node_id),
        },
    )


def handle_plugin_cap(core: Any, req: P.Execute, cap: str) -> EventResult:
    from mino_scout.plugins import wechat as wechat_plugin

    wechat_plugin.bind_node(core.node_id)
    event = _event(req)
    started = now_iso()
    if cap == "plugin.gmail.fetch_otp":
        return _gmail(core, req, event, started)
    try:
        if cap == "plugin.cli.feishu":
            data = _feishu(core, req)
        elif cap == "plugin.cli.meego":
            from mino_scout.plugins.calls import meego_call

            data = meego_call(core.node_id, dict(req.params or {}))
        elif cap == "plugin.bot.send":
            from mino_scout.plugins.calls import bot_send

            params = dict(req.params or {})
            data = bot_send(core.node_id, str(params.get("kind") or ""), str(params.get("text") or ""))
        elif cap == "plugin.bot.wechat":
            data = _wechat(core, req)
        else:
            return _fail(event, started, f"这台节点还没有能力 {cap}", cap)
    except Exception as exc:
        from mino_scout.plugins.calls import PluginCallError

        text = str(exc)
        if not isinstance(exc, (PluginCallError, RuntimeError, ValueError)):
            text = f"调用失败: {type(exc).__name__}"
        return _fail(event, started, text, cap)
    data = dict(data)
    data["plugins"] = status_list(core.node_id)
    return make_event_result(
        event,
        status=EventStatus.PASS,
        executor_used="core",
        started_at=started,
        elapsed_ms=0,
        summary=str(data.get("summary") or "完成"),
        raw_response=_public_payload(data),
    )


def _feishu(core: Any, req: P.Execute) -> dict[str, Any]:
    from mino_scout.plugins.calls import feishu_check, feishu_read_doc

    params = dict(req.params or {})
    action = str(params.get("action") or "read_doc")
    if action == "check":
        data = feishu_check(core.node_id)
        data["summary"] = "飞书应用凭证可用"
        return data
    data = feishu_read_doc(core.node_id, str(params.get("url") or ""))
    data["summary"] = "已读取飞书文档"
    return data


def _wechat(core: Any, req: P.Execute) -> dict[str, Any]:
    from mino_scout.plugins import wechat as wx

    wx.bind_node(core.node_id)
    params = dict(req.params or {})
    action = str(params.get("action") or "status")
    if action == "qr":
        data = wx.start_qr()
    elif action == "status":
        data = wx.poll()
    elif action == "verify":
        data = wx.poll(str(params.get("verify_code") or ""))
    elif action == "logout":
        data = wx.logout()
    elif action == "send":
        wx.send_text(
            to_user_id=str(params.get("to_user_id") or ""),
            context_token=str(params.get("context_token") or ""),
            text=str(params.get("text") or ""),
        )
        data = {"ok": True, "summary": "已发送"}
    else:
        raise RuntimeError(f"不支持的微信动作 {action}")
    data.setdefault("summary", "微信状态已更新" if action != "send" else "已发送")
    data.pop("bot_token", None)
    return data


def _public_payload(data: dict[str, Any]) -> dict[str, Any]:
    hidden = {"bot_token", "app_secret", "plugin_secret", "webhook_url", "app_password"}
    return {k: v for k, v in data.items() if k not in hidden and "secret" not in k and "token" not in k}


def _gmail(core: Any, req: P.Execute, event: Any, started: str) -> EventResult:
    t0 = time.time()
    params = dict(req.params or {})
    inbox = plain_value("mail", "gmail", "inbox_address")
    password = secret_value(core.node_id, "mail", "gmail", "app_password")
    if not inbox or not password:
        return _fail(event, started, _GMAIL_HINT, "plugin.gmail.fetch_otp")
    since = params.get("since_ts")
    try:
        since_ts = float(since) if since else None
    except (TypeError, ValueError):
        since_ts = None
    allow = params.get("from_allowlist")
    if not isinstance(allow, list):
        allow = []
    deadline = params.get("deadline_ts")
    try:
        deadline_ts = float(deadline) if deadline else None
    except (TypeError, ValueError):
        deadline_ts = None
    try:
        code = fetch_otp_via_imap(
            inbox_address=inbox,
            app_password=password,
            to_address=str(params.get("to_address") or ""),
            since_ts=since_ts,
            from_allowlist=[str(x) for x in allow],
            subject_contains=str(params.get("subject_contains") or ""),
            poll_interval_ms=int(params.get("poll_interval_ms") or 3000),
            max_wait_ms=int(params.get("max_wait_ms") or 90_000),
            deadline_ts=deadline_ts,
        )
    except GmailOtpError as exc:
        text = str(exc)
        if "未配置" in text:
            text = _GMAIL_HINT
        return _fail(event, started, text, "plugin.gmail.fetch_otp", t0=t0)
    return make_event_result(
        event,
        status=EventStatus.PASS,
        executor_used="core",
        started_at=started,
        elapsed_ms=int((time.time() - t0) * 1000),
        summary="已取到验证码",
        raw_response={"code": code, "source": "gmail"},
    )


def _event(req: P.Execute) -> Any:
    from mino_scout.core import _event_from_execute

    return _event_from_execute(req)


def _fail(event: Any, started: str, summary: str, command: str, *, t0: float | None = None) -> EventResult:
    elapsed = 0 if t0 is None else int((time.time() - t0) * 1000)
    return make_event_result(
        event,
        status=EventStatus.FAIL,
        executor_used="core",
        started_at=started,
        elapsed_ms=elapsed,
        summary=summary,
        error=summary,
        raw_response={"command": command},
    )
