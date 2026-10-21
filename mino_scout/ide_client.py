"""Bounded, loopback-only Streamable HTTP MCP client. No transport fallback/retry."""
from __future__ import annotations

import json
import os
import re
import time
from urllib.parse import urlsplit, urlunsplit
import httpx


class IDEError(Exception):
    def __init__(self, message, *, effect_unknown=False, reason_code="", remediation="", error_field=""):
        super().__init__(message)
        self.effect_unknown = effect_unknown
        self.reason_code = reason_code
        self.remediation = remediation
        self.error_field = error_field


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
                raise IDEError(f"IDE 拒绝访问（HTTP {response.status_code}）", reason_code="ide_auth_failed",
                               remediation="在 PyCharm 的 MCP 服务器设置中允许此连接；若启用了认证，请核对所选 Scout 本机的认证环境变量。", error_field="connection_id")
            if response.status_code >= 300:
                raise IDEError(f"IDE HTTP {response.status_code}", reason_code="ide_http_error",
                               remediation="在 PyCharm 设置 → 工具 → MCP 服务器中启用并应用，复制当前 HTTP 流配置，通过“更换 IDE 连接”更新地址、端口和路径。", error_field="connection_id")
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
        if name == "get_run_configurations" and name not in self.tools and "list_run_configurations" in self.tools:
            name = "list_run_configurations"
        tool = self.tools.get(name)
        if not tool:
            raise IDEError(f"IDE 未提供 {name}，请检查所选 MCP 插件连接")
        schema = tool.get("inputSchema") or {}
        properties = schema.get("properties") or {}
        project_field = "project_path" if "project_path" in properties else "projectPath"
        global_run_control = name in {"list_running_run_configurations", "stop_run_configuration"}
        if project_field not in properties and not global_run_control:
            raise IDEError("IDE 工具缺少明确项目路由，拒绝依赖窗口焦点")
        project = args.get("projectPath", args.get("project_path"))
        if not isinstance(project, str) or not project.strip():
            raise IDEError("IDE 操作必须指定非空项目路径")
        from pathlib import Path
        accepted = {key: value for key, value in args.items() if key in properties}
        if project_field in properties:
            accepted[project_field] = str(Path(project).expanduser().resolve(strict=True))
        if name == "stop_run_configuration":
            if not isinstance(args.get("sessionId"), str) or not args["sessionId"] or not isinstance(args.get("configurationName"), str) or not args["configurationName"] or args.get("stopAllMatches") is not False:
                raise IDEError("普通 Run 停止必须指定已核对的会话 ID 和配置名，不允许停止所有匹配实例")
            if not {"sessionId", "configurationName", "stopAllMatches"} <= properties.keys():
                raise IDEError("运行控制工具缺少精确停止参数")
        intent_field = {"execute_run_configuration": "configurationName", "start_debug_session": "configurationName",
                        "stop_debug_session": "session_id", "execute_terminal_command": "command"}.get(name)
        if intent_field:
            target_field = intent_field
            if name == "start_debug_session":
                target_field = "configuration_name"
            elif name == "execute_run_configuration" and "name" in properties:
                target_field = "name"
            value = args.get(intent_field)
            if target_field not in properties or not isinstance(value, str) or not value.strip():
                raise IDEError("IDE 工具不能明确接收执行目标，拒绝派发")
            accepted[target_field] = value
        if name == "execute_run_configuration" and "mode" in properties and args.get("mode") not in {"run", "debug"}:
            raise IDEError("Debugger MCP 执行配置必须明确选择 run/debug 模式")
        if set(schema.get("required") or []) - accepted.keys():
            raise IDEError("当前 IDE 工具要求未支持的参数")
        mutating = name in {"execute_run_configuration", "execute_terminal_command", "start_debug_session", "stop_debug_session", "stop_run_configuration"}
        try:
            raw = self.rpc("tools/call", {"name": name, "arguments": accepted})
        except Exception as exc:
            if mutating:
                raise IDEError("IDE 执行回执未确认；不会自动重试", effect_unknown=True) from exc
            raise
        if raw.get("isError"):
            text = "\n".join(part.get("text", "") for part in raw.get("content", []) if part.get("type") == "text")
            if "buildSessionId" in text:
                raise IDEError("MCP Run Control 与当前 PyCharm 的原生 MCP 接口不兼容（缺少 buildSessionId）",
                               effect_unknown=mutating, reason_code="ide_plugin_incompatible",
                               remediation="需要与当前 IDE 构建匹配的插件兼容版本；原版插件仅显示工具名称并不代表实际可用。当前普通实例可继续使用，不要重复启动。")
            if name in {"get_run_configurations", "list_run_configurations"}:
                raise IDEError("PyCharm 未能读取指定项目的启动配置", reason_code="ide_project_rejected",
                               remediation="在 PyCharm 打开“IDE 项目路径”对应的项目，并将该字段改为 PyCharm 实际打开的项目根目录，再重新检查。", error_field="project_path")
            raise IDEError("IDE 工具执行失败，请核对项目及运行配置", effect_unknown=mutating)
        result = raw.get("structuredContent")
        if result is None:
            text = "\n".join(content.get("text", "") for content in raw.get("content", []) if content.get("type") == "text")
            try:
                result = json.loads(text)
            except ValueError:
                result = {"output": text}
        if isinstance(result, dict) and result.get("error"):
            raise IDEError("IDE MCP 拒绝请求：" + str(result["error"]), effect_unknown=mutating,
                           reason_code="ide_project_rejected" if result["error"] in {"project_not_found", "multiple_projects_open", "no_project_open"} else "ide_tool_rejected",
                           error_field="project_path")
        return result


def configuration_names(result):
    rows = result if isinstance(result, list) else result.get("configurations", result.get("runConfigurations", []))
    if not isinstance(rows, list):
        raise IDEError("IDE 运行配置返回格式不支持")
    return [r if isinstance(r, str) else str(r.get("name", "")) for r in rows if isinstance(r, (dict, str))]
