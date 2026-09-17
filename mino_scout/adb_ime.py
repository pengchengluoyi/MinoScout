"""Android 默认输入法：ADB Keyboard（com.android.adbkeyboard）。

Scout 重启时自检；未安装时可从网络下载 APK 并 adb install（缓存到本机）。
Nexus 经 EXECUTE capability `set_input_method` 触发切换。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from mino_scout.config import config_dir
from mino_scout.log import SLog

TAG = "AdbIme"

ADB_KEYBOARD_PACKAGE = "com.android.adbkeyboard"
ADB_KEYBOARD_IME_ID = f"{ADB_KEYBOARD_PACKAGE}/.AdbIME"
DEFAULT_APK_URL = "https://github.com/senzhk/ADBKeyboard/raw/master/ADBKeyboard.apk"
CACHE_APK_NAME = "ADBKeyboard.apk"

ShellRunner = Callable[[str, tuple[str, ...], float], tuple[int, str, str]]


def _default_shell(serial: str, args: tuple[str, ...], timeout_sec: float) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["adb", "-s", serial, "shell", *args],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout {timeout_sec}s"
    except FileNotFoundError:
        return -2, "", "adb not in PATH"
    except OSError as exc:
        return -3, "", str(exc)


def _run(
    serial: str,
    *args: str,
    timeout_sec: float = 20.0,
    shell: ShellRunner | None = None,
) -> tuple[int, str, str]:
    runner = shell or _default_shell
    return runner(serial, args, timeout_sec)


def normalize_ime_id(raw: str) -> str:
    val = str(raw or "").strip()
    if not val or val.lower() in {"null", "none"}:
        return ""
    return val


def current_default_ime(serial: str, *, shell: ShellRunner | None = None) -> str:
    rc, out, _ = _run(serial, "settings", "get", "secure", "default_input_method", shell=shell)
    if rc != 0:
        return ""
    return normalize_ime_id(out)


def package_installed(serial: str, package: str, *, shell: ShellRunner | None = None) -> bool:
    rc, out, _ = _run(serial, "pm", "path", package, shell=shell)
    return rc == 0 and bool(out.strip())


def list_enabled_imes(serial: str, *, shell: ShellRunner | None = None) -> list[str]:
    rc, out, _ = _run(serial, "ime", "list", "-s", shell=shell)
    if rc != 0:
        return []
    return [normalize_ime_id(line) for line in out.splitlines() if normalize_ime_id(line)]


def _auto_install_enabled() -> bool:
    raw = str(os.environ.get("MINO_SCOUT_ADB_KEYBOARD_AUTO_INSTALL") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def apk_download_url() -> str:
    return str(os.environ.get("MINO_SCOUT_ADB_KEYBOARD_APK_URL") or DEFAULT_APK_URL).strip()


def cached_apk_path() -> Path:
    return config_dir() / "cache" / CACHE_APK_NAME


def resolve_local_apk() -> Path | None:
    explicit = str(os.environ.get("MINO_SCOUT_ADB_KEYBOARD_APK_PATH") or "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    path = cached_apk_path()
    return path if path.is_file() and path.stat().st_size > 0 else None


def ensure_apk_cached(*, force_download: bool = False) -> Path:
    existing = resolve_local_apk()
    if existing is not None and not force_download:
        return existing
    from mino_scout.executors.adb_command import _download_apk

    url = apk_download_url()
    if not url:
        raise ValueError("未配置 ADB Keyboard 下载地址")
    tmp = Path(_download_apk(url, CACHE_APK_NAME))
    try:
        dest = cached_apk_path()
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp), str(dest))
        return dest
    finally:
        if tmp.exists() and tmp != cached_apk_path():
            try:
                tmp.unlink()
            except OSError:
                pass


def install_apk(serial: str, apk_path: Path | str, *, timeout_sec: float = 300.0) -> dict[str, Any]:
    sid = str(serial or "").strip()
    path = Path(apk_path)
    if not sid:
        return {"ok": False, "error": "缺少 adb serial"}
    if not path.is_file():
        return {"ok": False, "error": f"APK 不存在: {path}"}
    try:
        proc = subprocess.run(
            ["adb", "-s", sid, "install", "-r", "-t", str(path)],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"adb install 超时 {timeout_sec}s"}
    except FileNotFoundError:
        return {"ok": False, "error": "adb not in PATH"}
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    ok = proc.returncode == 0 and "success" in out.lower()
    if ok:
        return {"ok": True, "stdout": out, "apk": str(path)}
    return {"ok": False, "error": err or out or f"adb install rc={proc.returncode}"}


def resolve_target_ime(params: dict[str, Any] | None) -> str:
    raw = dict(params or {})
    explicit = str(raw.get("ime_id") or "").strip()
    if explicit and "/" in explicit:
        return explicit
    ime = str(raw.get("ime") or explicit or "adbkeyboard").strip().lower()
    if ime in ("adbkeyboard", "adb_keyboard", "adb-keyboard"):
        return ADB_KEYBOARD_IME_ID
    if "/" in ime:
        return ime
    if ime.startswith("com."):
        return f"{ime}/.AdbIME"
    return ADB_KEYBOARD_IME_ID


def ensure_adb_keyboard(
    serial: str,
    *,
    target_ime: str = ADB_KEYBOARD_IME_ID,
    shell: ShellRunner | None = None,
    auto_install: bool | None = None,
) -> dict[str, Any]:
    """启用并设为默认输入法。已是目标则跳过 set。"""
    sid = str(serial or "").strip()
    if not sid:
        return {"ok": False, "error": "缺少 adb serial", "serial": sid}

    installed = package_installed(sid, ADB_KEYBOARD_PACKAGE, shell=shell)
    if not installed:
        do_install = _auto_install_enabled() if auto_install is None else bool(auto_install)
        if not do_install:
            return {
                "ok": False,
                "serial": sid,
                "error": f"设备未安装 {ADB_KEYBOARD_PACKAGE}（自动安装已关闭）",
                "package": ADB_KEYBOARD_PACKAGE,
            }
        try:
            apk = ensure_apk_cached()
        except Exception as exc:
            SLog.w(TAG, f"{sid} 下载 ADB Keyboard 失败: {exc}")
            return {
                "ok": False,
                "serial": sid,
                "error": f"下载 ADB Keyboard 失败: {exc}",
                "step": "download",
            }
        ins = install_apk(sid, apk)
        if not ins.get("ok"):
            SLog.w(TAG, f"{sid} 安装 ADB Keyboard 失败: {ins.get('error')}")
            return {
                "ok": False,
                "serial": sid,
                "error": str(ins.get("error") or "adb install 失败"),
                "step": "install",
                "apk": str(apk),
            }
        if not package_installed(sid, ADB_KEYBOARD_PACKAGE, shell=shell):
            return {
                "ok": False,
                "serial": sid,
                "error": f"安装后仍未检测到 {ADB_KEYBOARD_PACKAGE}",
                "step": "verify_install",
            }
        SLog.i(TAG, f"{sid} 已安装 ADB Keyboard（{apk}）")

    previous = current_default_ime(sid, shell=shell)
    if previous == target_ime:
        return {
            "ok": True,
            "serial": sid,
            "changed": False,
            "previous_ime": previous,
            "current_ime": target_ime,
            "summary": "已是 ADB Keyboard",
        }

    enabled = list_enabled_imes(sid, shell=shell)
    if target_ime not in enabled:
        rc, out, err = _run(sid, "ime", "enable", target_ime, shell=shell)
        if rc != 0:
            msg = err or out or f"ime enable rc={rc}"
            SLog.w(TAG, f"{sid} enable {target_ime} 失败: {msg}")
            return {
                "ok": False,
                "serial": sid,
                "previous_ime": previous,
                "error": msg,
                "step": "enable",
            }

    rc, out, err = _run(sid, "ime", "set", target_ime, shell=shell)
    if rc != 0:
        msg = err or out or f"ime set rc={rc}"
        SLog.w(TAG, f"{sid} set {target_ime} 失败: {msg}")
        return {
            "ok": False,
            "serial": sid,
            "previous_ime": previous,
            "error": msg,
            "step": "set",
        }

    current = current_default_ime(sid, shell=shell) or target_ime
    changed = current == target_ime and current != previous
    if current != target_ime:
        return {
            "ok": False,
            "serial": sid,
            "previous_ime": previous,
            "current_ime": current,
            "error": f"切换后当前输入法仍是 {current or '未知'}",
            "step": "verify",
        }

    SLog.i(TAG, f"{sid} 输入法 {previous or '—'} → {current}")
    return {
        "ok": True,
        "serial": sid,
        "changed": changed,
        "previous_ime": previous,
        "current_ime": current,
        "summary": "已切换为 ADB Keyboard",
    }
