"""Android self-prove. Reuses dumpsys version parsing, pm path, and getprop.

The digest is the on-device installed APK (base.apk when present), not the host
file hashed before `adb install`.
"""
from __future__ import annotations

from typing import Any, Callable

from mino_scout.probe.app_query import parse_package_version
from mino_scout.self_prove import (
    adb_server_port,
    assemble,
    empty_app,
    empty_device,
    empty_digest,
    empty_drivers,
    empty_signing,
    failed,
    finish,
    order_apk_paths,
    parse_code_path,
    parse_endpoint,
    parse_pm_paths,
    parse_signing,
    parse_sum_line,
    valid_package_id,
    valid_serial,
    ProofResult,
)

Shell = Callable[..., tuple[int, str, str]]

_DEVICE_PROP_SCRIPT = "\n".join((
    "printf 'serialno=%s\\n' \"$(getprop ro.serialno)\"",
    "printf 'boot_serialno=%s\\n' \"$(getprop ro.boot.serialno)\"",
    "printf 'model=%s\\n' \"$(getprop ro.product.model)\"",
    "printf 'release=%s\\n' \"$(getprop ro.build.version.release)\"",
    "printf 'name=%s\\n' \"$(getprop ro.product.name)\"",
))

_PROP_KEYS = ("serialno", "boot_serialno", "model", "release", "name")


def parse_device_props(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in _PROP_KEYS:
            found[key] = value.strip()
    return found


def _hint_drivers(serial: str, hint: dict[str, Any]) -> dict[str, Any]:
    drivers = empty_drivers()
    drivers["adb"] = {"serial": serial, "server_port": adb_server_port()}
    wda = parse_endpoint(hint.get("wda_url"), hint.get("wda_port"))
    appium = parse_endpoint(hint.get("appium_url"), hint.get("appium_port"))
    drivers["wda"].update(url=wda["url"], port=wda["port"])
    drivers["appium"].update(url=appium["url"], port=appium["port"])
    return drivers


def _hash_one(argv: Shell, path: str) -> tuple[str, str, str]:
    reason = ""
    sums = {"sha256": "", "md5": ""}
    for kind, binary in (("sha256", "sha256sum"), ("md5", "md5sum")):
        rc, out, err = argv(binary, path, timeout=20.0)
        digest = parse_sum_line(out, kind=kind) if rc == 0 else ""
        if not digest:
            rc2, out2, err2 = argv("toybox", binary, path, timeout=20.0)
            digest = parse_sum_line(out2, kind=kind) if rc2 == 0 else ""
            if not digest:
                reason = reason or (err2 or err or out2 or out or f"{binary} rc={rc}")[:160]
        sums[kind] = digest
    return sums["sha256"], sums["md5"], reason.strip()


def package_identity(dumpsys: str, package: str) -> dict[str, Any]:
    """Fields shared with get_app_version. `dumpsys` is the text already read."""
    parsed = parse_package_version(dumpsys)
    installed = f"Package [{package}]" in (dumpsys or "") or bool(parsed.get("version_name"))
    app = empty_app()
    app["package_id"] = package
    app["installed"] = bool(installed)
    app["version_name"] = str(parsed.get("version_name") or "")
    app["version_code"] = parsed.get("version_code")
    app["install_path"] = parse_code_path(dumpsys)
    app["signing"] = parse_signing(dumpsys)
    return app


def digest_paths(argv: Shell, paths: list[str]) -> dict[str, Any]:
    ordered, truncated = order_apk_paths(paths)
    digest = empty_digest()
    splits: list[dict[str, str]] = []
    if not ordered:
        digest["reason"] = "pm_path_empty"
        return {"digest": digest, "splits": splits, "install_path": ""}
    primary = ordered[0]
    sha, md5, reason = _hash_one(argv, primary)
    digest.update(
        sha256=sha,
        md5=md5,
        source="installed_base_apk" if primary.endswith("/base.apk") else "installed_apk",
        path=primary,
        reason="" if sha or md5 else (reason or "digest_unreadable"),
    )
    if truncated:
        digest["reason"] = (digest["reason"] + " " if digest["reason"] else "") + "paths_truncated"
    for path in ordered[1:]:
        sha_i, md5_i, _err = _hash_one(argv, path)
        splits.append({"path": path, "sha256": sha_i, "md5": md5_i})
    return {"digest": digest, "splits": splits, "install_path": primary}


def prove_android(
    *,
    serial: str,
    package: str,
    node_id: str,
    hint: dict[str, Any] | None = None,
    script: Shell,
    argv: Shell,
) -> ProofResult:
    """Collect one proof. `script` runs a fixed sh snippet; `argv` is adb shell argv."""
    try:
        return _prove(serial=serial, package=package, node_id=node_id, hint=dict(hint or {}), script=script, argv=argv)
    except Exception as exc:  # noqa: BLE001 — proof must not escape the executor.
        shown = str(serial or "")
        safe = shown if valid_serial(shown) else ""
        return failed(
            platform="android",
            error=f"self_prove android 失败: {type(exc).__name__}",
            node_id=node_id,
            package_requested=bool(str(package or "").strip()),
            serial=safe,
            hint_id=safe,
        )


def _prove(*, serial: str, package: str, node_id: str, hint: dict, script: Shell, argv: Shell) -> ProofResult:
    package = str(package or "").strip()
    requested = bool(package)
    serial = str(serial or "").strip()
    if not valid_serial(serial):
        return failed(
            platform="android",
            error="self_prove 缺少可用的 adb serial",
            node_id=node_id,
            package_requested=requested,
        )
    rc, out, err = script(_DEVICE_PROP_SCRIPT, timeout=8.0)
    props = parse_device_props(out) if rc == 0 else {}
    reported = props.get("serialno") or props.get("boot_serialno") or ""
    answered = rc == 0
    device = empty_device("android")
    device["serial"] = serial
    device["reported_serial"] = reported
    device["hint_id"] = serial
    device["model"] = props.get("model") or ""
    device["name"] = props.get("name") or ""
    device["os_version"] = props.get("release") or ""
    if answered:
        device["id"] = reported or serial
        device["id_source"] = "ro.serialno" if props.get("serialno") else (
            "ro.boot.serialno" if props.get("boot_serialno") else "adb_serial"
        )
    observed = [item for item in (props.get("serialno") or "", props.get("boot_serialno") or "", serial if answered else "") if item]
    device["ids_agree"] = None if len(observed) < 2 else len(set(observed)) == 1
    reported_ids = [item for item in (props.get("serialno") or "", props.get("boot_serialno") or "") if item]
    device["hint_match"] = None if not reported_ids else all(item == serial for item in reported_ids)
    app = empty_app()
    if requested and not valid_package_id(package):
        app["digest"]["reason"] = "package_id_invalid"
        app["signing"] = empty_signing()
    elif requested:
        drc, dump, derr = argv("dumpsys", "package", package, timeout=20.0)
        if drc == 0:
            app = package_identity(dump, package)
            if app.get("installed") is not True:
                app["digest"]["reason"] = "not_installed"
        else:
            app["package_id"] = package
            app["installed"] = False
            app["digest"]["reason"] = (derr or dump or f"dumpsys rc={drc}")[:160]
        if app.get("installed") is True:
            prc, pout, perr = argv("pm", "path", package, timeout=10.0)
            paths = parse_pm_paths(pout) if prc == 0 else []
            hashed = digest_paths(argv, paths)
            app["digest"] = hashed["digest"]
            app["splits"] = hashed["splits"]
            if hashed["install_path"]:
                app["install_path"] = hashed["install_path"]
            if not paths:
                app["digest"]["reason"] = app["digest"]["reason"] or (perr or "pm_path_empty")[:160]
    drivers = _hint_drivers(serial, hint)
    data = assemble(
        platform="android",
        device=device,
        app=app,
        drivers=drivers,
        node_id=node_id,
        package_requested=requested,
    )
    if not answered or not device["id"]:
        return finish(data, ok=False, error=err or out or f"adb rc={rc}" or "读不到设备标识")
    return finish(data, ok=True)
