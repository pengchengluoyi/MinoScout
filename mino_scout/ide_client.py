"""Bounded, loopback-only Streamable HTTP MCP client. No transport fallback/retry."""
from __future__ import annotations

import json
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
    if not isinstance(raw, dict) or set(raw) - {"url", "transport", "type", "headers"}:
        raise IDEError("仅接受 URL、HTTP 流传输和非秘密 headers；不执行 command/args")
    transport = raw.get("transport", raw.get("type", "streamable-http"))
    if transport not in {"http", "streamable-http"}:
        raise IDEError("首期仅支持 HTTP 流，不支持 SSE/Stdio")
    headers = raw.get("headers") or {}
    if not isinstance(headers, dict) or any(k.lower() not in {"x-idea-project"} for k in headers):
        raise IDEError("当前版本暂不支持认证配置或自定义 header；请使用无鉴权 HTTP 流连接")
    if any(not isinstance(v, str) or len(v) > 2048 or "\n" in v or "\r" in v for v in headers.values()):
        raise IDEError("header 无效")
    return {"url": local_url(raw.get("url", "")), "transport": "streamable-http", "headers": headers}


class Client:
    def __init__(self, config):
        self.url = local_url(config["url"])
        self.http = httpx.Client(trust_env=False, follow_redirects=False, timeout=40)
        self.headers = {**config.get("headers", {}), "Accept": "application/json, text/event-stream"}
        self.serial = 0
        self.deadline = time.monotonic() + 60
        self.tools = {}
        self.server = {}

    def __enter__(self):
        try:
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
            self.http.close()
            raise

    def __exit__(self, *args):
        self.http.close()

    def rpc(self, method, params, notification=False):
        self.serial += 1
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            body["id"] = self.serial
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
