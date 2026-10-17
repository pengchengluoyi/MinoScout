"""Explicit legacy SSE transport; no fallback, reconnect, or request replay."""
import json
import queue
import threading
import time
from urllib.parse import urljoin, urlsplit
from mino_scout.ide_client import IDEError


class SSETransport:
    def __init__(self, client):
        self.client = client
        self.events = queue.Queue(maxsize=64)
        self.stop = threading.Event()
        self.response = None
        self.thread = threading.Thread(target=self._read, daemon=True)
        client.sse = self
        self.thread.start()
        event, endpoint = self.next()
        if event != "endpoint":
            raise IDEError("SSE 未返回消息端点")
        target = urlsplit(urljoin(client.url, endpoint))
        origin = urlsplit(client.url)
        if (target.scheme, target.hostname, target.port) != (origin.scheme, origin.hostname, origin.port) or target.username or target.password or target.fragment:
            raise IDEError("SSE 消息端点必须与已配置 IDE 同源")
        self.endpoint = target.geturl()

    def _emit(self, value):
        try:
            self.events.put(value, timeout=.2)
        except queue.Full:
            self.stop.set()

    def _read(self):
        try:
            with self.client.http.stream("GET", self.client.url, headers=self.client.headers, timeout=40) as response:
                self.response = response
                if response.status_code != 200 or "text/event-stream" not in response.headers.get("content-type", ""):
                    raise IDEError(f"SSE 连接不可用（HTTP {response.status_code}），请检查端点和权限")
                kind, lines, size = "message", [], 0
                for line in response.iter_lines():
                    if self.stop.is_set() or time.monotonic() > self.client.deadline:
                        break
                    size += len(line.encode())
                    if size > 1048576:
                        raise IDEError("SSE 事件超出 1 MiB 上限")
                    if line.startswith("event:"):
                        kind = line[6:].strip()
                    elif line.startswith("data:"):
                        lines.append(line[5:].lstrip())
                    elif not line:
                        if lines:
                            self._emit((kind, "\n".join(lines)))
                        kind, lines, size = "message", [], 0
                if not self.stop.is_set():
                    raise IDEError("SSE 已断开；不会重新发送原请求")
        except Exception:
            if not self.stop.is_set():
                self._emit(("error", "SSE 连接中断或响应无效；请检查端点和访问设置，不会自动重连"))

    def next(self):
        remaining = self.client.deadline - time.monotonic()
        try:
            event, value = self.events.get(timeout=max(.01, remaining))
        except queue.Empty:
            raise IDEError("等待 SSE 回执超时")
        if event == "error":
            raise IDEError(value)
        return event, value

    def rpc(self, body, notification):
        remaining = self.client.deadline - time.monotonic()
        if remaining <= 0:
            raise IDEError("IDE 操作期限已到")
        with self.client.http.stream("POST", self.endpoint, headers=self.client.headers, json=body, timeout=min(40, remaining)) as response:
            if response.status_code not in {200, 202, 204}:
                raise IDEError(f"SSE 请求被拒（HTTP {response.status_code}）")
        if notification:
            return {}
        for _ in range(64):
            event, text = self.next()
            if event != "message":
                continue
            message = json.loads(text)
            if message.get("id") == body["id"]:
                return self.client.result(message)
        raise IDEError("SSE 未返回匹配的请求回执")

    def close(self):
        self.stop.set()
        if self.response is not None:
            self.response.close()
