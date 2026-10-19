"""Bounded, loopback-only Streamable HTTP MCP client. No transport fallback/retry."""
from __future__ import annotations

import json
import os
import re
import time
from urllib.parse import urlsplit, urlunsplit
import httpx


class IDEError(Exception):
    def __init__(self, message, *, effect_unknown=False):
        super().__init__(message)
        self.effect_unknown = effect_unknown


def local_url(value):
    u = urlsplit(str(value))
    if u.scheme not in {"http", "https"} or u.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise IDEError("仅允许所选 Scout 本机的回环 HTTP 地址")
    if u.username or u.password or u.query or u.fragment or not u.port:
        raise IDEError("端点须包含明确端口，不接受凭据、查询或片段")
    host = "[::1]" if u.hostname == "::1" else "127.0.0.1"
    return urlunsplit((u.scheme, f"{host}:{u.port}", u.path or "/", "", ""))


def connection_spec(raw):
    if not isinstance(raw, dict) or set(raw) - {"url", "transport", "type", "headers", "header_env"}:
        raise IDEError("连接只接受 URL、传输和 header 环境变量引用；不执行 command/args")
    transport = raw.get("transport", raw.get("type", "streamable-http"))
    if transport not in {"http", "streamable-http", "sse"}:
        raise IDEError("只支持 HTTP 流或显式 SSE；不支持 Stdio")
    headers = raw.get("headers") or {}
    env = raw.get("header_env") or {}
    if not isinstance(headers, dict) or not isinstance(env, dict):
        raise IDEError("header 配置必须为对象")
    if any(k.lower() != "x-idea-project" for k in headers):
        raise IDEError("不接收明文认证 header；请配置 header_env 的 MINO_IDE_ 环境变量引用")
    if any(not isinstance(v, str) or len(v) > 2048 or "\n" in v or "\r" in v for v in headers.values()):
        raise IDEError("header 无效")
    if any(k.lower() not in {"authorization", "x-api-key"} or not isinstance(v, str)
           or not re.fullmatch(r"MINO_IDE_[A-Z0-9_]{1,100}", v) for k, v in env.items()):
        raise IDEError("只允许 Authorization/X-API-Key 引用 MINO_IDE_ 开头的本机环境变量名")
    if len({k.lower() for k in env}) != len(env):
        raise IDEError("认证 header 重复")
    return {"url": local_url(raw.get("url", "")), "transport": "sse" if transport == "sse" else "streamable-http", "headers": headers, "header_env": env}


def resolved_headers(config):
    headers = dict(config.get("headers", {}))
    for name, reference in config.get("header_env", {}).items():
        value = os.environ.get(reference, "")
        if not value or len(value) > 8192 or "\r" in value or "\n" in value:
            raise IDEError("Scout 本机 IDE 认证环境变量缺失或无效；请在节点配置后重启 Scout")
        headers[name] = value
    from mino_scout.secret_redaction import remember
    for name, value in headers.items():
        if name.lower() in {"authorization", "x-api-key"}:
            remember(value)
            if name.lower() == "authorization" and " " in value:
                remember(value.split(" ", 1)[1])
    return headers

class Client:
    def __init__(self, config):
        config = connection_spec({k: v for k, v in config.items() if k not in {"connection_id", "revision"}})
        self.transport = config["transport"]
        self.sse = None
        self.url = config["url"]
        self.headers = {**resolved_headers(config), "Accept": "application/json, text/event-stream"}
        self.http = httpx.Client(trust_env=False, follow_redirects=False, timeout=40)
        self.serial = 0
        self.deadline = time.monotonic() + 60
        self.tools = {}
        self.server = {}

    def __enter__(self):
        try:
            if self.transport == "sse":
                from mino_scout.ide_sse import SSETransport
                self.sse = SSETransport(self)
            init = self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "mino-scout", "version": "1"}})
            if init.get("protocolVersion") not in {"2025-03-26", "2025-06-18"}:
                raise IDEError("IDE MCP 协议版本不支持")
            self.headers["MCP-Protocol-Version"] = init["protocolVersion"]
            self.server = init.get("serverInfo") or {}
            self.rpc("notifications/initialized", {}, notification=True)
            cursor = None
            for _ in range(10):
                page = self.rpc("tools/list", {"cursor": cursor} if cursor else {})
                for tool in page.get("tools", []):
                    self.tools[tool["name"]] = tool
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            else:
                raise IDEError("IDE 工具列表未完整读取")
            return self
        except BaseException:
            if self.sse:
                self.sse.close()
            self.http.close()
            raise

    def __exit__(self, *args):
        if self.sse:
            self.sse.close()
        self.http.close()

    def rpc(self, method, params, notification=False):
        self.serial += 1
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            body["id"] = self.serial
        if self.sse:
            return self.sse.rpc(body, notification)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise IDEError("IDE 操作期限已到")
        with self.http.stream("POST", self.url, headers=self.headers, json=body, timeout=min(40, remaining)) as response:
            if response.status_code in {401, 403}:
                raise IDEError(f"IDE 拒绝访问（HTTP {response.status_code}），请检查 IDE 访问设置")
            if response.status_code >= 300:
                raise IDEError(f"IDE HTTP {response.status_code}，请核对 MCP 开关、端口和路径")
            sid = response.headers.get("mcp-session-id")
            if sid:
                self.headers["Mcp-Session-Id"] = sid
            if notification:
                return {}
            size = 0
            if "text/event-stream" in response.headers.get("content-type", ""):
                lines = []
                for line in response.iter_lines():
                    if time.monotonic() > self.deadline:
                        raise IDEError("IDE 操作期限已到")
                    size += len(line)
                    if size > 1048576:
                        raise IDEError("IDE 响应超出 1 MiB 上限")
                    if line.startswith("data:"):
                        lines.append(line[5:].lstrip())
                    if not line and lines:
                        message = json.loads("\n".join(lines))
                        lines = []
                        if message.get("id") == self.serial:
                            return self.result(message)
                raise IDEError("IDE 流结束但没有本次回执")
            chunks = []
            for chunk in response.iter_bytes():
                if time.monotonic() > self.deadline:
                    raise IDEError("IDE 操作期限已到")
                size += len(chunk)
                if size > 1048576:
                    raise IDEError("IDE 响应超出 1 MiB 上限")
                chunks.append(chunk)
            message = json.loads(b"".join(chunks))
            if message.get("id") != self.serial:
                raise IDEError("IDE 回执 ID 不匹配")
            return self.result(message)

    @staticmethod
    def result(message):
        if message.get("error"):
            raise IDEError("IDE 拒绝 MCP 请求：" + str(message["error"].get("code", "")))
        if not isinstance(message.get("result"), dict):
            raise IDEError("IDE 回执缺少 result")
        return message["result"]

    def call(self, name, args):
        tool = self.tools.get(name)
        if not tool:
            raise IDEError(f"IDE 未提供 {name}，请在 IDE 人工处理")
        schema = tool.get("inputSchema") or {}
        properties = schema.get("properties") or {}
        if "projectPath" not in properties:
            raise IDEError("IDE 工具不能明确路由 projectPath，拒绝依赖窗口焦点")
        project = args.get("projectPath")
        if not isinstance(project, str) or not project.strip():
            raise IDEError("IDE 操作必须指定非空 projectPath")
        # Routing/intent fields must never disappear during schema adaptation.
        # A permissive or changed IDE schema must fail before any side effect.
        intent_field = {"execute_run_configuration": "configurationName",
                        "execute_terminal_command": "command"}.get(name)
        if intent_field:
            value = args.get(intent_field)
            if intent_field not in properties or not isinstance(value, str) or not value.strip():
                raise IDEError("IDE 工具不能明确接收执行目标，拒绝派发")
        accepted = {k: v for k, v in args.items() if k in properties}
        if set(schema.get("required") or []) - accepted.keys():
            raise IDEError("当前 IDE 工具要求未支持的参数")
        mutating = name in {"execute_run_configuration", "execute_terminal_command"}
        try:
            raw = self.rpc("tools/call", {"name": name, "arguments": accepted})
        except Exception as exc:
            if mutating:
                raise IDEError("IDE 执行回执未确认；不会自动重试", effect_unknown=True) from exc
            raise
        if raw.get("isError"):
            raise IDEError("IDE 工具执行失败，请核对项目及运行配置", effect_unknown=mutating)
        result = raw.get("structuredContent")
        if result is None:
            text = "\n".join(c.get("text", "") for c in raw.get("content", []) if c.get("type") == "text")
            try:
                result = json.loads(text)
            except ValueError:
                result = {"output": text}
        return result


def configuration_names(result):
    rows = result if isinstance(result, list) else result.get("configurations", result.get("runConfigurations", []))
    if not isinstance(rows, list):
        raise IDEError("IDE 运行配置返回格式不支持")
    return [r if isinstance(r, str) else str(r.get("name", "")) for r in rows if isinstance(r, (dict, str))]
