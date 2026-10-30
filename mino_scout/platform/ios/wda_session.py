"""WDA HTTP 会话。iOS 不走 adb，也不使用 Appium 客户端库。

本机 WDA 地址、签名团队和 Runner bundle 来自环境，不写死某台设备。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import urllib.error
import urllib.request
from typing import Any

from mino_scout.tools.keep_alive.log import SLog

TAG = "Wda"


class WdaError(RuntimeError):
    pass


def wda_base_url() -> str:
    return str(os.environ.get("MINO_WDA_URL") or "http://127.0.0.1:8100").rstrip("/")


def _signing_capabilities() -> dict[str, Any]:
    caps: dict[str, Any] = {}
    org = str(os.environ.get("MINO_IOS_XCODE_ORG_ID") or "").strip()
    bundle = str(os.environ.get("MINO_IOS_WDA_BUNDLE_ID") or "").strip()
    if org:
        caps["appium:xcodeOrgId"] = org
    if bundle:
        caps["appium:updatedWDABundleId"] = bundle
    return caps


class WdaClient:
    def __init__(self, udid: str, base_url: str = ""):
        self.udid = str(udid or "").strip()
        self.base = str(base_url or wda_base_url()).rstrip("/")
        self.session_id = ""

    def reachable(self) -> bool:
        try:
            self._json("GET", "/status")
            return True
        except WdaError:
            return False

    def ensure(self) -> str:
        if self.session_id:
            return self.session_id
        if not self.udid:
            raise WdaError("iOS 设备缺少 udid")
        caps = {
            "platformName": "iOS",
            "appium:automationName": "XCUITest",
            "appium:udid": self.udid,
            "appium:noReset": True,
            "appium:usePrebuiltWDA": True,
            **_signing_capabilities(),
        }
        body = self._json("POST", "/session", {"capabilities": {"alwaysMatch": caps}})
        value = body.get("value") if isinstance(body, dict) else {}
        sid = ""
        if isinstance(value, dict):
            sid = str(value.get("sessionId") or "")
        sid = sid or str((body or {}).get("sessionId") or "")
        if not sid:
            raise WdaError("WDA 没有返回 session")
        self.session_id = sid
        return sid

    def screenshot(self) -> str:
        body = self._json("GET", self._path("/screenshot"))
        value = body.get("value") if isinstance(body, dict) else ""
        if not isinstance(value, str) or not value:
            raise WdaError("WDA 截图为空")
        return value

    def source(self) -> str:
        body = self._json("GET", self._path("/source"))
        value = body.get("value") if isinstance(body, dict) else ""
        if not isinstance(value, str) or not value:
            raise WdaError("WDA 没有返回界面结构")
        return value

    def tap_xy(self, x: int, y: int) -> None:
        self._json("POST", self._path("/wda/tap"), {"x": int(x), "y": int(y)})

    def tap_target(self, target: str) -> None:
        element = self._find(target)
        self._json("POST", self._path(f"/element/{element}/click"), {})

    def activate(self, bundle_id: str) -> None:
        self._json("POST", self._path("/wda/apps/activate"), {"bundleId": bundle_id})

    def terminate(self, bundle_id: str) -> None:
        self._json("POST", self._path("/wda/apps/terminate"), {"bundleId": bundle_id})

    def keys(self, text: str) -> None:
        self._json("POST", self._path("/wda/keys"), {"value": list(str(text or ""))})

    def window_size(self) -> tuple[int, int]:
        body = self._json("GET", self._path("/window/size"))
        value = body.get("value") if isinstance(body, dict) else {}
        if not isinstance(value, dict) or "width" not in value or "height" not in value:
            raise WdaError("WDA 没有返回窗口尺寸")
        return int(value["width"]), int(value["height"])

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.3) -> None:
        self._json("POST", self._path("/wda/dragfromtoforduration"), {
            "fromX": int(x1), "fromY": int(y1), "toX": int(x2), "toY": int(y2), "duration": float(duration),
        })

    def touch_and_hold(self, x: int, y: int, duration: float = 0.6) -> None:
        self._json("POST", self._path("/wda/touchAndHold"), {"x": int(x), "y": int(y), "duration": float(duration)})

    def press_button(self, name: str) -> None:
        self._json("POST", self._path("/wda/pressButton"), {"name": name})

    def active_app(self) -> dict[str, Any]:
        body = self._json("GET", self._path("/wda/activeAppInfo"))
        value = body.get("value") if isinstance(body, dict) else {}
        if not isinstance(value, dict) or not value.get("bundleId"):
            raise WdaError("WDA 没有返回前台应用")
        return value

    def set_clipboard(self, text: str) -> None:
        content = base64.b64encode(str(text or "").encode()).decode()
        self._json("POST", self._path("/wda/setPasteboard"), {"content": content, "contentType": "plaintext"})

    def element_center(self, target: str) -> tuple[int, int]:
        element = self._find(target)
        body = self._json("GET", self._path(f"/element/{element}/rect"))
        value = body.get("value") if isinstance(body, dict) else {}
        if not isinstance(value, dict) or "x" not in value:
            raise WdaError("WDA 没有返回控件位置")
        return int(value["x"] + value.get("width", 0) / 2), int(value["y"] + value.get("height", 0) / 2)

    def _find(self, target: str) -> str:
        text = str(target or "").strip()
        if not text:
            raise WdaError("没有要点的目标")
        strategies = (
            ("accessibility id", text),
            ("name", text),
            ("predicate string", f'name == "{text}" OR label == "{text}"'),
        )
        last = "没有找到元素"
        for using, value in strategies:
            try:
                body = self._json("POST", self._path("/element"), {"using": using, "value": value})
            except WdaError as exc:
                last = str(exc)
                continue
            found = body.get("value") if isinstance(body, dict) else {}
            if isinstance(found, dict):
                element = str(found.get("ELEMENT") or found.get("element-6066-11e4-a52e-4f735466cecf") or "")
                if element:
                    return element
        raise WdaError(last)

    def _path(self, suffix: str) -> str:
        return f"/session/{self.ensure()}{suffix}"

    def _json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise WdaError(f"WDA {method} {path} 失败：{detail or exc.code}") from exc
        except urllib.error.URLError as exc:
            raise WdaError(f"WDA 连不上 {self.base}") from exc
        if not raw:
            return {}
        parsed = json.loads(raw.decode())
        if not isinstance(parsed, dict):
            raise WdaError("WDA 返回的不是 JSON 对象")
        value = parsed.get("value")
        if isinstance(value, dict) and value.get("error"):
            raise WdaError(str(value.get("message") or value.get("error")))
        return parsed


_CLIENTS: dict[str, WdaClient] = {}


def client_for(udid: str) -> WdaClient:
    key = str(udid or "").strip()
    client = _CLIENTS.get(key)
    if client is None:
        client = WdaClient(key)
        _CLIENTS[key] = client
    return client


def list_attached_devices() -> list[dict[str, str]]:
    """用 devicectl 列出已连接的 iOS 设备。失败时返回空列表，不猜 udid。"""
    try:
        proc = subprocess.run(
            ["xcrun", "devicectl", "list", "devices", "--json-output", "-"],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        SLog.w(TAG, f"devicectl list devices 失败: {exc}")
        return []
    if proc.returncode != 0:
        SLog.w(TAG, f"devicectl list devices 退出 {proc.returncode}")
        return []
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return []
    result = payload.get("result") if isinstance(payload, dict) else {}
    rows = result.get("devices") if isinstance(result, dict) else []
    out: list[dict[str, str]] = []
    if not isinstance(rows, list):
        return out
    reachable = WdaClient("").reachable()
    for row in rows:
        if not isinstance(row, dict):
            continue
        udid = str(row.get("identifier") or row.get("udid") or "").strip()
        if not udid:
            continue
        props = row.get("deviceProperties") if isinstance(row.get("deviceProperties"), dict) else {}
        hardware = row.get("hardwareProperties") if isinstance(row.get("hardwareProperties"), dict) else {}
        name = str(props.get("name") or hardware.get("marketingName") or "")
        out.append({
            "udid": udid,
            "name": name,
            "state": "connected" if reachable else "disconnected",
        })
    return out


def install_app(udid: str, bundle_path: str) -> None:
    _devicectl(udid, "install", "app", bundle_path)


def uninstall_app(udid: str, bundle_id: str) -> None:
    _devicectl(udid, "uninstall", "app", bundle_id)


def app_version(udid: str, bundle_id: str) -> dict[str, str]:
    if not udid or not bundle_id:
        raise WdaError("读版本缺少 udid 或 bundle id")
    try:
        proc = subprocess.run(
            ["xcrun", "devicectl", "device", "info", "apps", "--device", udid, "--json-output", "-"],
            capture_output=True, text=True, timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        raise WdaError(f"devicectl 读版本失败：{exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise WdaError(detail or f"devicectl 读版本退出 {proc.returncode}")
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise WdaError("devicectl 版本输出不是 JSON") from exc
    result = payload.get("result") if isinstance(payload, dict) else {}
    apps = result.get("apps") if isinstance(result, dict) else []
    if not isinstance(apps, list):
        apps = []
    for row in apps:
        if not isinstance(row, dict):
            continue
        ident = str(row.get("bundleIdentifier") or row.get("bundleId") or "")
        if ident != bundle_id:
            continue
        version = str(row.get("bundleVersion") or row.get("version") or row.get("shortVersion") or "")
        if not version:
            raise WdaError(f"已安装 {bundle_id}，但没有版本号")
        return {"bundle_id": ident, "version": version}
    raise WdaError(f"设备上没有 {bundle_id}")


def _devicectl(udid: str, action: str, noun: str, arg: str) -> None:
    if not udid or not arg:
        raise WdaError("安装或卸载缺少 udid 或目标")
    try:
        proc = subprocess.run(
            ["xcrun", "devicectl", "device", action, noun, "--device", udid, arg],
            capture_output=True, text=True, timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        raise WdaError(f"devicectl {action} 失败：{exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise WdaError(detail or f"devicectl {action} 退出 {proc.returncode}")
