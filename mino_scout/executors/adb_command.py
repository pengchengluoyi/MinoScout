# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""命令级 ClawNode→adb 执行器 (v3, P1)。

`/device/command` 对 adb 直连设备的落地：接收与 ClawNode **完全相同**的命令名与
params（TAP/SWIPE/OPEN_APP/INPUT_TEXT/INSTALL_APK/EXEC_SCRIPT...），本地经 adb 执行，
并封装成与 ClawNode 回传 **相同结构**的响应 {status, stdout, stderr, message[, base64_image]}，
使上层（send_command / rDevice）对两条渠道无感知。

设计要点：
  - 坐标/时长契约与 ClawNode 对齐：绝对像素 int、duration_ms 毫秒 int
  - 渠道差异在此内部消化：url→下载再 install、keyevent 名→keycode、activity→am start -n、
    TAP duration_ms→长按转 input swipe
  - 与回归层的 AdbExecutor(capability 事件级) 互补：此处是命令级、供设备详情页直发
  - EXEC_SCRIPT 委托 server/services/shared/adb_script.py（与 clawnode_script 隔离）
"""
from __future__ import annotations

import base64
import os
import re
import subprocess
import tempfile
from typing import Any, Dict, Tuple

from mino_scout.log import SLog

TAG = "AdbCommand"

# ClawNode keyevent 名 → Android keycode
_KEYEVENT_MAP: Dict[str, int] = {
    "BACK": 4, "HOME": 3, "MENU": 82, "POWER": 26, "ENTER": 66,
    "APP_SWITCH": 187, "RECENT": 187, "VOLUME_UP": 24, "VOLUME_DOWN": 25,
    "WAKEUP": 224, "SLEEP": 223, "DEL": 67, "ESCAPE": 111, "TAB": 61, "SPACE": 62,
    # paste 在 adb 无直接 keyevent，交由 INPUT_TEXT/SET_CLIPBOARD 路径处理
    "PASTE": 279,
}

_LONG_PRESS_MS = 500  # TAP 的 duration_ms ≥ 此值转长按(input swipe 原地)

# 命令别名 → ClawNode 标准命令名（与 wClawNode.translate_control_to_clawnode 的别名对齐）
_ALIASES: Dict[str, str] = {
    "CLICK": "TAP", "TAP": "TAP",
    "SWIPE": "SWIPE",
    "WAKE": "WAKE_UP", "WAKE_UP": "WAKE_UP",
    "KEY": "KEY_EVENT", "KEYEVENT": "KEY_EVENT", "PRESS_KEY": "KEY_EVENT", "KEY_EVENT": "KEY_EVENT",
    "OPEN_APP": "OPEN_APP", "START_APP": "OPEN_APP", "LAUNCH_APP": "OPEN_APP",
    "STOP_APP": "CLOSE_APP", "FORCE_STOP": "CLOSE_APP", "CLOSE_APP": "CLOSE_APP",
    "KILL_APP": "KILL_APP", "FORCE_KILL": "KILL_APP",
    "CLEAR_APP_CACHE": "CLEAR_APP_CACHE", "CLEAR_CACHE": "CLEAR_APP_CACHE",
    "RUN_SHELL": "RUN_SHELL", "SHELL": "RUN_SHELL",
    "EXEC_SCRIPT": "EXEC_SCRIPT", "RUN_SCRIPT": "EXEC_SCRIPT", "EXEC_CODE": "EXEC_SCRIPT", "EXEC": "EXEC_SCRIPT",
    "OPEN_APP_ALIAS": "OPEN_APP",
    "SET_CLIPBOARD": "SET_CLIPBOARD", "CLIPBOARD": "SET_CLIPBOARD",
    "INPUT_TEXT": "INPUT_TEXT", "TYPE_TEXT": "INPUT_TEXT", "TYPE": "INPUT_TEXT",
    "INSTALL_APK": "INSTALL_APK", "INSTALL": "INSTALL_APK", "INSTALLAPK": "INSTALL_APK",
    "GET_SCREENSHOT": "GET_SCREENSHOT", "SCREENSHOT": "GET_SCREENSHOT",
    "GET_FOREGROUND_APP": "GET_FOREGROUND_APP", "FOREGROUND_APP": "GET_FOREGROUND_APP",
}


def _adb_shell(serial: str, *args: str, timeout: float = 30.0) -> Tuple[int, str, str]:
    return _run(["adb", "-s", serial, "shell", *args], timeout)


def _adb(serial: str, *args: str, timeout: float = 60.0) -> Tuple[int, str, str]:
    return _run(["adb", "-s", serial, *args], timeout)


def _run(cmd: list[str], timeout: float) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except FileNotFoundError:
        return -2, "", "adb binary not in PATH"
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout after {timeout}s"
    except Exception as e:  # pragma: no cover
        return -3, "", f"adb invoke failed: {e}"


def _ok(message: str = "", stdout: str = "", **extra: Any) -> Dict[str, Any]:
    return {"status": "success", "message": message, "stdout": stdout, "stderr": "", **extra}


def _err(message: str, stderr: str = "") -> Dict[str, Any]:
    return {"status": "error", "message": message, "stdout": "", "stderr": stderr or message}


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return default


def run_adb_command(serial: str, command: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """执行一条 ClawNode 契约命令，返回 ClawNode 结构的结果 dict（同步）。"""
    if not serial:
        return _err("adb serial 未解析")
    params = dict(params or {})
    cmd = str(command or "").strip().upper().replace("-", "_")
    cmd = _ALIASES.get(cmd, cmd)

    try:
        handler = _HANDLERS.get(cmd)
        if handler is None:
            return _err(f"adb 渠道不支持命令 {cmd}")
        return handler(serial, params)
    except Exception as e:
        SLog.e(TAG, f"run_adb_command {cmd} failed: {e}")
        return _err(f"exception: {e}")


# ---------------- 各命令实现（对齐 ClawNode params） ----------------

def _h_tap(serial: str, p: Dict[str, Any]) -> Dict[str, Any]:
    x, y = _int(p.get("x")), _int(p.get("y"))
    dur = _int(p.get("duration_ms"), 0)
    if dur >= _LONG_PRESS_MS:
        rc, out, err = _adb_shell(serial, "input", "swipe", str(x), str(y), str(x), str(y), str(dur))
    else:
        rc, out, err = _adb_shell(serial, "input", "tap", str(x), str(y))
    return _ok(f"tap ({x},{y})", out) if rc == 0 else _err("tap failed", err)


def _h_swipe(serial: str, p: Dict[str, Any]) -> Dict[str, Any]:
    x, y = _int(p.get("x")), _int(p.get("y"))
    x2, y2 = _int(p.get("x2")), _int(p.get("y2"))
    dur = _int(p.get("duration_ms"), 300) or 300
    rc, out, err = _adb_shell(serial, "input", "swipe", str(x), str(y), str(x2), str(y2), str(dur))
    return _ok(f"swipe ({x},{y})->({x2},{y2})", out) if rc == 0 else _err("swipe failed", err)


def _h_key_event(serial: str, p: Dict[str, Any]) -> Dict[str, Any]:
    raw = str(p.get("keyevent") or p.get("key") or "").strip()
    if not raw:
        return _err("keyevent 缺失")
    code = _KEYEVENT_MAP.get(raw.upper())
    kev = str(code) if code is not None else raw  # 允许直接传数字 keycode
    rc, out, err = _adb_shell(serial, "input", "keyevent", kev)
    return _ok(f"keyevent {raw}", out) if rc == 0 else _err("keyevent failed", err)


def _h_wake_up(serial: str, p: Dict[str, Any]) -> Dict[str, Any]:
    rc, out, err = _adb_shell(serial, "input", "keyevent", "224")  # KEYCODE_WAKEUP
    return _ok("wake", out) if rc == 0 else _err("wake failed", err)


def _h_open_app(serial: str, p: Dict[str, Any]) -> Dict[str, Any]:
    pkg = str(p.get("package") or "").strip()
    if not pkg:
        return _err("package 缺失")
    activity = str(p.get("activity") or "").strip()
    if activity:
        comp = activity if "/" in activity else f"{pkg}/{activity}"
        rc, out, err = _adb_shell(serial, "am", "start", "-n", comp)
    else:
        rc, out, err = _adb_shell(
            serial, "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"
        )
    ok = rc == 0 and "error" not in (out + err).lower()
    return _ok(f"open {pkg}", out) if ok else _err("open_app failed", err or out)


def _h_close_app(serial: str, p: Dict[str, Any]) -> Dict[str, Any]:
    pkg = str(p.get("package") or "").strip()
    if not pkg:
        return _err("package 缺失")
    rc, out, err = _adb_shell(serial, "am", "force-stop", pkg)
    return _ok(f"force-stop {pkg}", out) if rc == 0 else _err("close_app failed", err)


def _h_clear_app_cache(serial: str, p: Dict[str, Any]) -> Dict[str, Any]:
    pkg = str(p.get("package") or "").strip()
    if not pkg:
        return _err("package 缺失")
    # adb 全特权：pm clear 清应用数据（比 ClawNode 拟人化清缓存更彻底）
    rc, out, err = _adb_shell(serial, "pm", "clear", pkg)
    ok = rc == 0 and "success" in out.lower()
    return _ok(f"pm clear {pkg}", out) if ok else _err("clear_app_cache failed", err or out)


def _h_run_shell(serial: str, p: Dict[str, Any]) -> Dict[str, Any]:
    command = str(p.get("command") or "").strip()
    if not command:
        return _err("command 缺失")
    rc, out, err = _adb_shell(serial, command)
    return _ok("shell ok", out) if rc == 0 else _err("shell failed", err)


def _h_input_text(serial: str, p: Dict[str, Any]) -> Dict[str, Any]:
    text = str(p.get("text") or "")
    if p.get("x") is not None and p.get("y") is not None:
        _adb_shell(serial, "input", "tap", str(_int(p.get("x"))), str(_int(p.get("y"))))
    if not text.isascii():
        return _err("adb input text 不支持非 ASCII(中文)，请走 remote 渠道或 IME")
    safe = text.replace(" ", "%s")
    rc, out, err = _adb_shell(serial, "input", "text", safe)
    return _ok("input text", out) if rc == 0 else _err("input_text failed", err)


def _h_set_clipboard(serial: str, p: Dict[str, Any]) -> Dict[str, Any]:
    # adb 无原生剪贴板写入（需 helper app / IME），明确返回不支持
    return _err("adb 渠道不支持 SET_CLIPBOARD，请走 remote 渠道")


def _h_install_apk(serial: str, p: Dict[str, Any]) -> Dict[str, Any]:
    path = str(p.get("path") or "").strip()
    url = str(p.get("url") or "").strip()
    tmp_path = ""
    try:
        if not path and url:
            path = _download_apk(url, p.get("file_name") or "")
            tmp_path = path
        if not path or not os.path.exists(path):
            return _err(f"apk 文件不存在: {path or url}")
        rc, out, err = _adb(serial, "install", "-r", "-t", path, timeout=300.0)
        ok = rc == 0 and "success" in out.lower()
        return _ok("install ok", out) if ok else _err("install_apk failed", err or out)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _download_apk(url: str, file_name: str = "") -> str:
    """下载 apk 到临时文件，返回本地路径（对齐 ClawNode 的 url 传参）。"""
    import httpx

    suffix = ".apk"
    if file_name and file_name.lower().endswith(".apk"):
        suffix = "_" + re.sub(r"[^A-Za-z0-9._-]+", "_", file_name)
    fd, path = tempfile.mkstemp(prefix="adb_apk_", suffix=suffix)
    os.close(fd)
    with httpx.stream("GET", url, timeout=120.0, follow_redirects=True) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    return path


def _h_get_screenshot(serial: str, p: Dict[str, Any]) -> Dict[str, Any]:
    # exec-out 取二进制 png，转 base64，字段名与 ClawNode 回传对齐(base64_image)
    try:
        proc = subprocess.run(
            ["adb", "-s", serial, "exec-out", "screencap", "-p"],
            capture_output=True, timeout=30.0,
        )
    except Exception as e:
        return _err(f"screencap failed: {e}")
    if proc.returncode != 0 or not proc.stdout:
        return _err("screencap failed", (proc.stderr or b"").decode("utf-8", "ignore"))
    b64 = base64.b64encode(proc.stdout).decode("ascii")
    return _ok("screenshot ok", base64_image=b64, format="png")


def _h_get_foreground_app(serial: str, p: Dict[str, Any]) -> Dict[str, Any]:
    rc, out, err = _adb_shell(serial, "dumpsys", "activity", "activities")
    if rc != 0:
        return _err("get_foreground_app failed", err)
    m = re.search(r"(?:mResumedActivity|topResumedActivity|ResumedActivity).*?([A-Za-z0-9_.]+)/", out)
    pkg = m.group(1) if m else ""
    return _ok(pkg, pkg)


def _h_exec_script(serial: str, p: Dict[str, Any]) -> Dict[str, Any]:
    """EXEC_SCRIPT 委托 adb_script（协议与 ClawNode 一致，js 返回 not_supported）。"""
    from mino_scout.executors.adb_script import run_adb_script

    return run_adb_script(serial, p)


_HANDLERS = {
    "TAP": _h_tap,
    "SWIPE": _h_swipe,
    "KEY_EVENT": _h_key_event,
    "WAKE_UP": _h_wake_up,
    "OPEN_APP": _h_open_app,
    "CLOSE_APP": _h_close_app,
    "KILL_APP": _h_close_app,  # adb 无区分，统一 force-stop
    "CLEAR_APP_CACHE": _h_clear_app_cache,
    "RUN_SHELL": _h_run_shell,
    "INPUT_TEXT": _h_input_text,
    "SET_CLIPBOARD": _h_set_clipboard,
    "INSTALL_APK": _h_install_apk,
    "GET_SCREENSHOT": _h_get_screenshot,
    "GET_FOREGROUND_APP": _h_get_foreground_app,
    "EXEC_SCRIPT": _h_exec_script,
}


def supported_commands() -> set[str]:
    return set(_HANDLERS.keys())
