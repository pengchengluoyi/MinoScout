"""One logical console slot, one persistent Cua SDK runtime, fail-closed readiness.

No window, monitor or browser creates another device. Optional native bindings
are imported only after host/session/permission checks have succeeded.
"""
from __future__ import annotations

import asyncio
import base64
import ctypes
import hashlib
import importlib.metadata
import json
import ntpath
import os
import platform
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, is_dataclass
from typing import Any

from mino_scout import protocol as P

SDK_VERSION = "0.34.1"
DESKTOP_CAPS = ("screenshot", "tap_xy", "tap_element", "input_text", "press_key", "launch_app", "get_foreground_app")
_REASONS = {
    "ready": "",
    "no_session": "无图形桌面会话，请在已登录的桌面内启动 Scout",
    "locked": "桌面已锁定，请解锁后重试",
    "permission_required": "缺少录屏或辅助功能权限，请在系统设置中授权 Scout 后重新探测",
    "driver_missing": f"未安装 Cua Driver {SDK_VERSION} 桌面执行插件",
    "driver_incompatible": f"Cua Driver 版本不兼容，需要 {SDK_VERSION}",
    "unsupported": "当前系统或架构尚未验证 Desktop 执行支持",
    "probe_failed": "桌面探测失败，不能确认截图与输入可用",
}


class DesktopUnavailable(RuntimeError):
    pass


def _mac_session() -> dict[str, Any]:
    """Read TCC without requesting permission; never unlock or change a session."""
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    cg.CGSessionCopyCurrentDictionary.restype = ctypes.c_void_p
    cf.CFPropertyListCreateData.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long, ctypes.c_ulong, ctypes.c_void_p]
    cf.CFPropertyListCreateData.restype = ctypes.c_void_p
    cf.CFDataGetLength.argtypes = [ctypes.c_void_p]
    cf.CFDataGetLength.restype = ctypes.c_long
    cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
    cf.CFDataGetBytePtr.restype = ctypes.c_void_p
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
    ax.AXIsProcessTrusted.restype = ctypes.c_bool
    ref = cg.CGSessionCopyCurrentDictionary()
    if not ref:
        return {"state": "no_session", "identity": "none"}
    import plistlib
    data = None
    try:
        data = cf.CFPropertyListCreateData(None, ref, 100, 0, None)
        if not data:
            raise DesktopUnavailable("无法读取桌面会话")
        session = plistlib.loads(ctypes.string_at(cf.CFDataGetBytePtr(data), cf.CFDataGetLength(data)))
    finally:
        if data:
            cf.CFRelease(data)
        cf.CFRelease(ref)
    # A background service belonging to another user may not inject into console.
    uid = session.get("kCGSessionUserIDKey")
    identity = f"{uid}:{session.get('kCGSessionAuditIDKey', '')}"
    if uid != os.getuid() or not session.get("kCGSessionOnConsoleKey") or not session.get("kCGSessionLoginDoneKey"):
        return {"state": "no_session", "identity": identity}
    if session.get("CGSSessionScreenIsLocked"):
        return {"state": "locked", "identity": identity + ":locked"}
    permissions = {"screen_capture": bool(cg.CGPreflightScreenCaptureAccess()), "accessibility": bool(ax.AXIsProcessTrusted())}
    return {"state": "ready" if all(permissions.values()) else "permission_required", "identity": identity, "permissions": permissions}


def host_session() -> dict[str, Any]:
    os_name = {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}.get(platform.system(), "unknown")
    arch = platform.machine().lower()
    try:
        if os_name == "macos":
            session = _mac_session()
        elif os_name == "linux" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            session = {"state": "no_session", "identity": "none"}
        elif os_name == "windows":
            from mino_scout.desktop_windows import windows_session
            session = windows_session()
        else:
            session = {"state": "unsupported", "identity": os.environ.get("XDG_SESSION_ID", "unknown")}
    except Exception:
        session = {"state": "probe_failed", "identity": "unknown"}
    if session["state"] == "ready" and arch not in ("arm64", "aarch64", "x86_64", "amd64"):
        session["state"] = "unsupported"
    return {"os": os_name, "arch": arch, "hostname": platform.node(), **session}


def application_target(package: str, os_name: str) -> dict[str, str]:
    if not package or any(ord(char) < 32 for char in package):
        raise DesktopUnavailable("请配置被测 PC 应用标识")
    if os_name == "macos":
        if "." not in package or not all(char.isalnum() or char in "._-" for char in package):
            raise DesktopUnavailable("macOS 应用必须配置 bundle ID")
        return {"bundle_id": package}
    if os_name == "windows":
        drive, tail = ntpath.splitdrive(package)
        if drive and tail.startswith(("\\", "/")) and package.lower().endswith(".exe"):
            return {"path": ntpath.normpath(package)}
        if package.count("!") == 1 and all(part and all(char.isalnum() or char in "._-" for char in part) for part in package.split("!")):
            return {"aumid": package}
        raise DesktopUnavailable("Windows 应用必须配置完整 EXE 路径或 AUMID，不能使用模糊应用名")
    raise DesktopUnavailable(_REASONS["unsupported"])


def application_matches(app: Any, package: str, os_name: str) -> bool:
    target = application_target(package, os_name)
    if "bundle_id" in target or "aumid" in target:
        actual = str(getattr(app, "bundle_id", "") or "")
        return actual == package if os_name == "macos" else actual.casefold() == package.casefold()
    actual = str(getattr(app, "launch_path", "") or "")
    if getattr(app, "pid", 0):
        from mino_scout.desktop_windows import process_image
        actual = process_image(app.pid) or ""
    return bool(actual and ntpath.normcase(ntpath.normpath(actual)) == ntpath.normcase(target["path"]))


def key_input(params: dict[str, Any], os_name: str) -> tuple[str, list[str]]:
    key = str(params.get("key") or "").strip()
    modifiers = params.get("modifiers") or []
    if not isinstance(modifiers, list) or not all(isinstance(item, str) for item in modifiers):
        raise DesktopUnavailable("按键 modifiers 必须是字符串列表")
    if "+" in key and key != "+":
        parts = key.split("+")
        modifiers = modifiers + parts[:-1]
        key = parts[-1]
    aliases = {"control": "ctrl", "option": "alt", "cmd": "command", "meta": "command" if os_name == "macos" else "win",
               "super": "command" if os_name == "macos" else "win", "primary": "command" if os_name == "macos" else "ctrl"}
    normalized = []
    for modifier in modifiers:
        value = aliases.get(modifier.strip().lower(), modifier.strip().lower())
        if value not in ({"ctrl", "alt", "shift", "command"} if os_name == "macos" else {"ctrl", "alt", "shift", "win"}):
            raise DesktopUnavailable("当前系统不支持这项按键修饰符")
        if value not in normalized:
            normalized.append(value)
    if not key:
        raise DesktopUnavailable("请提供要按下的键")
    return key, normalized


def action_evidence(result: Any) -> dict[str, Any]:
    record = public_record(getattr(result, "action", None))
    if not isinstance(record, dict):
        try:
            record = json.loads(getattr(result, "structured_json", None) or getattr(result, "raw_json", None) or "{}")
        except (ValueError, TypeError):
            record = {}
    if not isinstance(record, dict):
        return {}
    record = record.get("action", record)
    if not isinstance(record, dict):
        return {}
    # Retain the evidence type, omit free-form details that can echo field contents.
    evidence = record.get("evidence")
    out = {"evidence": [{"kind": str(row.get("kind") or "")[:80], "detail_omitted": bool(row.get("detail"))}
                        for row in evidence[:16] if isinstance(row, dict)]} if isinstance(evidence, list) else {}
    out.update({name: record[name] for name in ("effect", "route") if isinstance(record.get(name), str)})
    for name, fields in (("delivery", ("mode", "delivered_count")), ("error", ("code",)), ("escalation", ("target", "reason"))):
        child = record.get(name)
        if isinstance(child, dict):
            out[name] = {field: child[field] for field in fields if isinstance(child.get(field), (str, int))}
    return out


def public_record(value: Any) -> Any:
    """Only SDK action evidence; callers must not include input text in diagnostics."""
    from enum import Enum
    if isinstance(value, Enum):
        return value.name.lower()
    if is_dataclass(value):
        return public_record(asdict(value))
    if isinstance(value, dict):
        return {k: public_record(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [public_record(v) for v in value]
    if hasattr(value, "__dict__"):
        return public_record(vars(value))
    return value


class DesktopRuntime:
    def __init__(self, *, session_probe=host_session, focus_probe=None):
        from mino_scout.desktop_focus import probe_focus
        self._probe = session_probe
        self._focus_probe = focus_probe or probe_focus
        self._focus = {}
        self._boot = uuid.uuid4().hex
        self._identity = ""
        self._epoch = 0
        self._generation = ""
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scout-desktop")
        self._lock = threading.RLock()
        self._driver = None
        self._loop = None
        self._disabled = False
        self._observations: dict[str, dict[str, Any]] = {}
        self._cancelled: set[str] = set()
        self._sessions: set[str] = set()

    def _clear_focus(self, run_id=None):
        with self._lock:
            for rid in list(self._focus):
                if run_id is None or rid == run_id or rid.startswith(run_id + "::"):
                    self._focus.pop(rid)["native"].close()

    def _require_focus(self, run_id, params):
        with self._lock:
            focus = self._focus.get(run_id)
            if not focus or params.get("focus_ref") != focus["ref"]:
                raise DesktopUnavailable("contract_unsupported: 焦点目标已变化或引用失效")
            try:
                native = self._focus_probe()
                try:
                    matched = native.matches(focus["native"])
                finally:
                    native.close()
                if not matched:
                    raise DesktopUnavailable("focus_unavailable: active_element_check_failed")
            except DesktopUnavailable:
                raise
            except Exception as exc:
                raise DesktopUnavailable("focus_unavailable: active_element_check_failed") from exc

    def state(self) -> dict[str, Any]:
        with self._lock:
            state = self._probe()
            identity = str(state.get("identity")) + ":" + str(state.get("state"))
            if identity != self._identity:
                self._identity = identity
                self._epoch += 1
                self._generation = hashlib.sha256(f"{self._boot}:{self._epoch}:{identity}".encode()).hexdigest()[:24]
                self._observations.clear()
                self._clear_focus()
            if state["state"] == "ready":
                try:
                    version = importlib.metadata.version("cua-driver")
                    state["state"] = "ready" if version == SDK_VERSION else "driver_incompatible"
                except importlib.metadata.PackageNotFoundError:
                    state["state"] = "driver_missing"
            if self._disabled:
                state["state"] = "probe_failed"
            return {**state, "session_key": "console", "session_generation": self._generation,
                    "blocked_reason": _REASONS[state["state"]]}

    def manifest(self, node_id: str) -> P.DeviceManifest:
        state = self.state()
        # Package/permission checks alone cannot prove the native runtime works.
        if state["state"] == "ready":
            try:
                self._submit(self._health, timeout=8)
            except Exception:
                state.update(state="probe_failed", blocked_reason=_REASONS["probe_failed"])
        ready = state["state"] == "ready"
        return P.DeviceManifest(
            sn=f"desktop:{node_id}:console", platform="desktop", model="PC 桌面",
            channels={"desktop": "connected" if ready else "disconnected"},
            os=state["os"], session_key="console", session_generation=state["session_generation"],
            desktop_state=state["state"], blocked_reason=state["blocked_reason"],
            capabilities=list(DESKTOP_CAPS) if ready else [],
            readiness={"permissions": state.get("permissions", {}), "arch": state["arch"]},
        )

    def host(self, node_id: str, dev: P.DeviceManifest | None = None) -> dict[str, Any]:
        dev = dev or self.manifest(node_id)
        return {"os": dev.os, "arch": platform.machine(), "hostname": platform.node(),
                "desktop": {"state": dev.desktop_state, "blocked_reason": dev.blocked_reason,
                            "session_key": dev.session_key, "session_generation": dev.session_generation,
                            "readiness": dev.readiness}}

    def _submit(self, fn, *args, timeout=15):
        with self._lock:
            if self._disabled:
                raise DesktopUnavailable(_REASONS["probe_failed"])
            future = self._pool.submit(fn, *args)
        try:
            return future.result(timeout=timeout)
        except TimeoutError as exc:
            if future.cancel():
                raise DesktopUnavailable("桌面执行通道繁忙，请重新观察后重试") from exc
            with self._lock:
                # Do not reuse an uncertain native operation or reset its lane.
                self._disabled = True
                self._observations.clear()
                self._clear_focus()
            raise DesktopUnavailable("桌面操作超时，结果未知；请观察实际状态并重启 Scout") from exc

    def _sdk(self):
        if self._driver is None:
            import cua_driver as sdk
            self._sdk_module = sdk
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._driver = sdk.CuaDriver.create()
        return self._sdk_module, self._driver

    def _await(self, awaitable):
        return self._loop.run_until_complete(awaitable)

    def _session(self, run_id):
        sdk, driver = self._sdk()
        if run_id not in self._sessions:
            self._await(driver.start_session(sdk.StartSessionInput(session=run_id, capture_scope=sdk.CaptureScope.DESKTOP, cursor_theme=None, cursor_motion=None)))
            self._sessions.add(run_id)
        return sdk, driver

    def _health(self):
        sdk, driver = self._session("mino-probe")
        result = self._await(driver.get_desktop_state(sdk.GetDesktopStateInput(session="mino-probe", screenshot_out_file=None)))
        if result.is_error or not result.images:
            raise DesktopUnavailable("无法捕获桌面")

    def _check(self, hint: dict[str, Any], run_id: str, *, mutate=False):
        state = self.state()
        if state["state"] != "ready":
            raise DesktopUnavailable(state["blocked_reason"])
        if hint.get("os") not in (None, "", state["os"]) or hint.get("session_key") not in (None, "", "console"):
            raise DesktopUnavailable("PC 系统或桌面会话目标不匹配")
        if hint.get("session_generation") != state["session_generation"]:
            raise DesktopUnavailable("桌面会话已变化，旧任务引用失效，请重新创建任务")
        if run_id in self._cancelled or run_id.split("::", 1)[0] in self._cancelled:
            raise DesktopUnavailable("任务已取消")
        if mutate and (not run_id or hint.get("desktop_lease") != run_id.split("::", 1)[0]):
            raise DesktopUnavailable("桌面输入缺少任务独占租约")

    def observe(self, hint: dict[str, Any], run_id: str) -> dict[str, Any]:
        return self._submit(self._observe, hint, run_id)

    def _observe(self, hint, run_id):
        self._check(hint, run_id)
        sdk, driver = self._session(run_id or "mino-observe")
        result = self._await(driver.get_desktop_state(sdk.GetDesktopStateInput(session=run_id or "mino-observe", screenshot_out_file=None)))
        if result.is_error or not result.images:
            raise DesktopUnavailable("Cua 截图失败")
        data = json.loads(result.structured_json or result.raw_json or "{}")
        capture = data.get("capture_id") or (data.get("capture") or {}).get("capture_id")
        width = data.get("screenshot_width") or data.get("width")
        height = data.get("screenshot_height") or data.get("height")
        image = result.images[0]
        if not width or not height:
            from PIL import Image
            from io import BytesIO
            width, height = Image.open(BytesIO(base64.b64decode(image.data_base64))).size
        if not capture:
            raise DesktopUnavailable("Cua 未返回捕获引用，不能安全执行桌面动作")
        obs = {"capture_id": capture, "width": width, "height": height,
               "image_base64": image.data_base64, "image_mime": image.mime_type,
               "session_generation": hint["session_generation"]}
        self._observations[run_id] = obs
        return dict(obs)

    def action(self, capability: str, params: dict[str, Any], hint: dict[str, Any], run_id: str):
        return self._submit(self._action, capability, params, hint, run_id)

    def _action(self, capability, params, hint, run_id):
        self._check(hint, run_id, mutate=capability != "get_foreground_app")
        os_name = self.state()["os"]
        sdk, driver = self._session(run_id)
        target = sdk.ActionTarget.DESKTOP("primary")
        if capability == "get_foreground_app":
            apps = self._await(driver.list_apps(sdk.ListAppsInput()))
            active = [a for a in apps.apps if a.active]
            package = str(hint.get("target_package") or "")
            return {"apps": [public_record(a) for a in active],
                    "target_active": bool(package and any(application_matches(a, package, os_name) for a in active)),
                    "foreground_package": str(getattr(active[0], "bundle_id", "") or "") if active else ""}
        obs = self._observations.pop(run_id, None)  # every attempt consumes it
        if capability != "launch_app" and (not obs or obs["session_generation"] != hint["session_generation"]):
            raise DesktopUnavailable("桌面动作需要重新截图，旧捕获引用已消费或失效")
        if capability != "launch_app":
            package = str(hint.get("target_package") or "")
            apps = self._await(driver.list_apps(sdk.ListAppsInput()))
            if not package or not any(a.active and application_matches(a, package, os_name) for a in apps.apps):
                raise DesktopUnavailable("不能确认被测应用处于前台，请检查应用标识和权限，再重新观察并激活目标应用")
            self._check(hint, run_id, mutate=True)
        if capability in ("tap_xy", "tap_element"):
            # Mino uses permille coordinates, matching its screenshot dimensions.
            x, y = float(params["x"]), float(params["y"])
            if not 0 <= x <= 1000 or not 0 <= y <= 1000:
                raise DesktopUnavailable("桌面坐标必须在 0..1000 范围内")
            result = self._await(driver.click(sdk.ClickInput(
                target=target, position=sdk.ClickPosition.CAPTURED_COORDINATES(x * (obs["width"] - 1) / 1000, y * (obs["height"] - 1) / 1000, obs["capture_id"]),
                delivery_mode=sdk.InputDeliveryMode.FOREGROUND, session=run_id, button=None, count=1)))
        elif capability == "input_text":
            if str(params.get("target_mode") or "current_focus") != "current_focus":
                raise DesktopUnavailable("Desktop 输入仅支持 current_focus")
            mode = str(params.get("mode") or "replace")
            if mode not in ("replace", "append"):
                raise DesktopUnavailable("Desktop 输入 mode 必须是 replace 或 append")
            self._require_focus(run_id, params)
            if mode == "replace":
                selected = self._await(driver.press_key(sdk.PressKeyInput(key="A", target=target,
                    scope=sdk.DesktopScope.DESKTOP, session=run_id, modifiers=["command" if os_name == "macos" else "ctrl"])))
                select_evidence = action_evidence(selected)
                if getattr(selected, "is_error", False) or select_evidence.get("effect") in ("refused", "partial", "suspected_noop"):
                    self._clear_focus(run_id)
                    raise DesktopUnavailable("无法确认替换输入的选中动作；未发送输入正文")
                self._check(hint, run_id, mutate=True)
                self._require_focus(run_id, params)
            try:
                result = self._await(driver.type_text(sdk.TypeTextInput(text=str(params.get("text") or ""), target=target, scope=sdk.DesktopScope.DESKTOP, session=run_id)))
            finally:
                self._clear_focus(run_id)
        elif capability == "press_key":
            key, modifiers = key_input(params, os_name)
            result = self._await(driver.press_key(sdk.PressKeyInput(key=key, target=target, scope=sdk.DesktopScope.DESKTOP, session=run_id, modifiers=modifiers or None)))
        elif capability == "launch_app":
            # Driver validates a structured application target; never use a shell.
            package = str(hint.get("target_package") or "")
            if not package or str(params.get("package") or package) != package:
                raise DesktopUnavailable("请配置被测 PC 应用标识，不能切换到其他应用")
            result = self._await(driver.call_tool("launch_app", json.dumps({**application_target(package, os_name), "session": run_id})))
            if result.is_error:
                raise DesktopUnavailable("Cua 无法启动被测应用")
            launched = json.loads(result.structured_json or result.raw_json or "{}")
            pid = launched.get("pid")
            if not pid:
                raise DesktopUnavailable("Cua 未确认被测应用进程，请回看实际状态")
            self._check(hint, run_id, mutate=True)
            result = self._await(driver.call_tool("bring_to_front", json.dumps({"pid": pid, "session": run_id})))
            self._check(hint, run_id, mutate=True)
            apps = self._await(driver.list_apps(sdk.ListAppsInput()))
            if not any(app.active and application_matches(app, package, os_name) for app in apps.apps):
                raise DesktopUnavailable("被测 PC 应用未进入前台，请回看实际状态")
        else:
            raise DesktopUnavailable("Desktop 未实现这项能力")
        evidence = action_evidence(result)
        if capability in ("tap_xy", "tap_element"):
            self._clear_focus(run_id)
            ref = str(params.get("focus_ref") or "")
            if ref.startswith(f"focus:{run_id}:") and ref.rsplit(":", 1)[-1].isdigit() and not getattr(result, "is_error", False) and evidence.get("effect") not in ("refused", "partial", "suspected_noop"):
                try:
                    self._focus[run_id] = {"ref": ref, "native": self._focus_probe()}
                except Exception:
                    pass  # The click occurred; subsequent input is blocked without a confirmed focus.
        elif capability in ("press_key", "launch_app"):
            self._clear_focus(run_id)
        if getattr(result, "is_error", False):
            evidence["effect"] = "refused"
        return {"cua_action": evidence, "capture_id": (obs or {}).get("capture_id", ""), "session_generation": hint["session_generation"]}

    def cancel(self, run_id: str):
        # Cancellation prevents any newly admitted input, including delayed retries.
        with self._lock:
            for rid in list(self._observations):
                if rid == run_id or rid.startswith(run_id + "::"):
                    self._observations.pop(rid, None)
                    self._cancelled.add(rid)
            self._cancelled.add(run_id)
            self._clear_focus(run_id)
            if not self._disabled:
                self._pool.submit(self._end_run, run_id)

    def _end_run(self, run_id: str):
        if self._driver is None:
            return
        for session in list(self._sessions):
            if session == run_id or session.startswith(run_id + "::"):
                try:
                    self._await(self._driver.end_session(self._sdk_module.EndSessionInput(session=session)))
                finally:
                    self._sessions.discard(session)

    def shutdown(self):
        with self._lock:
            if getattr(self, "_closed", False):
                return
            self._closed = True
            self._disabled = True
            self._observations.clear()
            self._clear_focus()
            closing = self._pool.submit(self._close_sdk)
        try:
            closing.result(timeout=5)
        except Exception:
            pass  # A stuck native lane is never restarted or reused.
        finally:
            self._pool.shutdown(wait=False)

    def _close_sdk(self):
        if self._driver is not None:
            self._await(self._driver.shutdown())
            self._sessions.clear()
            self._driver = None
        if self._loop is not None:
            self._loop.close()
            self._loop = None


_runtime: DesktopRuntime | None = None
_runtime_lock = threading.Lock()


def get_desktop_runtime() -> DesktopRuntime:
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = DesktopRuntime()
        return _runtime
