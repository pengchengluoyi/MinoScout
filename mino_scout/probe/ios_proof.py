"""iOS self-prove. Same document as Android.

USB identity uses ideviceinfo when that binary is already on PATH. WDA is queried
only at a device_hint URL Scout was given. Appium is echoed, not dialed. Bundle
bytes on device are not hashed: iOS does not expose an installed-app digest here.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from mino_scout.self_prove import (
    assemble,
    empty_app,
    empty_device,
    empty_drivers,
    failed,
    finish,
    parse_endpoint,
    valid_package_id,
    valid_udid,
    ProofResult,
)

Runner = Callable[..., subprocess.CompletedProcess]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirect())


def _first_value(text: str) -> str:
    if not text or not text.strip():
        return ""
    line = text.strip().splitlines()[0].strip()
    if ":" in line:
        line = line.split(":", 1)[1].strip()
    return line


def parse_wda_device(payload: Any) -> dict[str, str]:
    value = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(value, dict):
        value = payload if isinstance(payload, dict) else {}
    udid = str(value.get("uuid") or value.get("udid") or value.get("identifier") or "").strip()
    return {
        "udid": udid,
        "name": str(value.get("name") or "").strip(),
        "model": str(value.get("model") or "").strip(),
    }


def parse_wda_status(payload: Any) -> dict[str, str]:
    value = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(value, dict):
        value = {}
    os_info = value.get("os") if isinstance(value.get("os"), dict) else {}
    build = value.get("build") if isinstance(value.get("build"), dict) else {}
    return {
        "os_version": str(os_info.get("version") or "").strip(),
        "version": str(build.get("version") or "").strip(),
    }


def parse_installer_version(text: str, bundle: str) -> tuple[bool, str]:
    """Return (listed, version). Version may be empty when the bundle is listed."""
    for line in (text or "").splitlines():
        raw = line.strip()
        if not raw or raw.lower().startswith("total:") or raw.startswith("CFBundle"):
            continue
        if raw == bundle or raw.startswith(bundle + ",") or raw.startswith(bundle + " ") or raw.startswith(bundle + "-"):
            version = ""
            tail = raw[len(bundle):].lstrip(" ,-\t")
            for token in tail.replace(",", " ").split():
                cleaned = token.strip("\"'")
                if cleaned[:1].isdigit():
                    version = cleaned
                    break
            return True, version
    return False, ""


def _run_tool(binary: str, args: list[str], runner: Runner, timeout: float) -> tuple[int, str, str]:
    if not shutil.which(binary):
        return -2, "", f"{binary} 不在 PATH"
    try:
        completed = runner(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"{binary} timeout"
    except OSError as exc:
        return -3, "", type(exc).__name__
    return int(completed.returncode or 0), (completed.stdout or "").strip(), (completed.stderr or "").strip()


def _get_json(url: str, timeout: float = 5.0) -> tuple[Any, str]:
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            raw = response.read(65537)
    except HTTPError as exc:
        exc.close()
        return None, f"wda_http_{exc.code}"
    except (OSError, URLError, TimeoutError):
        return None, "wda_unreachable"
    if len(raw) > 65536:
        return None, "wda_response_too_large"
    try:
        return json.loads(raw.decode("utf-8")), ""
    except (UnicodeError, json.JSONDecodeError):
        return None, "wda_json_invalid"


def _wda_identity(base: str) -> tuple[dict[str, str], dict[str, str], str]:
    device = {"udid": "", "name": "", "model": ""}
    status = {"os_version": "", "version": ""}
    error = ""
    info, info_err = _get_json(base.rstrip("/") + "/wda/device/info")
    if isinstance(info, dict):
        device = parse_wda_device(info)
    else:
        error = info_err
    body, status_err = _get_json(base.rstrip("/") + "/status")
    if isinstance(body, dict):
        status = parse_wda_status(body)
        error = "" if device["udid"] or status["version"] or status["os_version"] else error
    elif not error:
        error = status_err
    return device, status, error


def prove_ios(
    *,
    udid: str,
    package: str,
    node_id: str,
    hint: dict[str, Any] | None = None,
    runner: Runner = subprocess.run,
) -> ProofResult:
    try:
        return _prove(udid=str(udid or "").strip(), package=str(package or "").strip(), node_id=node_id, hint=dict(hint or {}), runner=runner)
    except Exception as exc:  # noqa: BLE001 — proof must not escape the executor.
        shown = str(udid or "")
        return failed(
            platform="ios",
            error=f"self_prove ios 失败: {type(exc).__name__}",
            node_id=node_id,
            package_requested=bool(package),
            hint_id=shown if valid_udid(shown) else "",
        )


def _prove(*, udid: str, package: str, node_id: str, hint: dict, runner: Runner) -> ProofResult:
    requested = bool(package)
    endpoint = parse_endpoint(hint.get("wda_url"), hint.get("wda_port"))
    appium = parse_endpoint(hint.get("appium_url"), hint.get("appium_port"))
    if str(hint.get("wda_url") or "").strip() and not endpoint["url"]:
        return failed(
            platform="ios",
            error="self_prove wda_url 不可用：需要带端口的 http(s)，不能带账号或 query",
            node_id=node_id,
            package_requested=requested,
            hint_id=udid if valid_udid(udid) else "",
        )
    if not valid_udid(udid) and not endpoint["url"]:
        return failed(
            platform="ios",
            error="self_prove 缺少可用的 udid 或 wda_url",
            node_id=node_id,
            package_requested=requested,
            hint_id=udid if valid_udid(udid) else "",
        )
    usb_udid = ""
    model = ""
    os_version = ""
    name = ""
    if valid_udid(udid):
        rc, out, _err = _run_tool("ideviceinfo", ["-u", udid, "-k", "UniqueDeviceID"], runner, 8.0)
        reported = _first_value(out)
        if rc == 0 and valid_udid(reported):
            usb_udid = reported
        rc, out, _err = _run_tool("ideviceinfo", ["-u", udid, "-k", "ProductType"], runner, 8.0)
        if rc == 0:
            model = _first_value(out)
        rc, out, _err = _run_tool("ideviceinfo", ["-u", udid, "-k", "ProductVersion"], runner, 8.0)
        if rc == 0:
            os_version = _first_value(out)
        rc, out, _err = _run_tool("ideviceinfo", ["-u", udid, "-k", "DeviceName"], runner, 8.0)
        if rc == 0:
            name = _first_value(out)
    wda_device = {"udid": "", "name": "", "model": ""}
    wda_status = {"os_version": "", "version": ""}
    wda_error = ""
    if endpoint["url"]:
        wda_device, wda_status, wda_error = _wda_identity(endpoint["url"])
    proven = usb_udid or (wda_device["udid"] if valid_udid(wda_device["udid"]) else "")
    device = empty_device("ios")
    device["udid"] = proven
    device["id"] = proven
    device["hint_id"] = udid if valid_udid(udid) else ""
    device["model"] = model or wda_device.get("model") or ""
    device["name"] = name or wda_device.get("name") or ""
    device["os_version"] = os_version or wda_status.get("os_version") or ""
    if usb_udid:
        device["id_source"] = "ideviceinfo"
    elif proven:
        device["id_source"] = "wda"
    if device["hint_id"]:
        device["hint_match"] = bool(proven) and proven.lower() == device["hint_id"].lower()
    if usb_udid and wda_device.get("udid") and valid_udid(wda_device["udid"]):
        device["ids_agree"] = usb_udid.lower() == wda_device["udid"].lower()
    app = empty_app()
    if requested and not valid_package_id(package):
        app["digest"]["reason"] = "package_id_invalid"
    elif requested:
        app["package_id"] = package
        app["digest"]["source"] = ""
        app["digest"]["reason"] = "ios_bundle_bytes_not_readable"
        if valid_udid(udid):
            rc, out, err = _run_tool("ideviceinstaller", ["-u", udid, "-l"], runner, 15.0)
            if rc == 0:
                listed, version = parse_installer_version(out, package)
                app["installed"] = listed
                app["version_name"] = version
                if not listed:
                    app["digest"]["reason"] = "not_installed"
            elif rc == -2:
                app["digest"]["reason"] = "ios_bundle_bytes_not_readable"
            else:
                app["digest"]["reason"] = (err or out or f"ideviceinstaller rc={rc}")[:160]
    drivers = empty_drivers()
    reached = bool(endpoint["url"]) and bool(wda_device.get("udid") or wda_status.get("version") or wda_status.get("os_version"))
    drivers["wda"].update(
        url=endpoint["url"],
        port=endpoint["port"],
        reachable=reached if endpoint["url"] else None,
        version=wda_status.get("version") or "",
        udid=wda_device.get("udid") or "",
        error="" if not endpoint["url"] else wda_error,
    )
    drivers["appium"].update(url=appium["url"], port=appium["port"])
    data = assemble(platform="ios", device=device, app=app, drivers=drivers, node_id=node_id, package_requested=requested)
    if not proven:
        detail = wda_error or "设备没有返回 udid"
        return finish(data, ok=False, error=f"self_prove 读不到 iOS 设备标识: {detail}"[:300])
    return finish(data, ok=True)
