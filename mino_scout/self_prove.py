"""Stable self-prove document. Parsers only — device I/O stays in the probe collectors.

Android and iOS fill the same keys. Empty strings and nulls mean "not proven",
not a default identity. `missing` lists fields that were requested or required
and came back empty.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

SCHEMA = "self_prove.v1"

# Bundle / application id. Rejects shell metacharacters. iOS segments may contain '-'.
PACKAGE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_-]*)+$")
SERIAL_RE = re.compile(r"^[A-Za-z0-9._:]{1,128}$")
UDID_RE = re.compile(r"^[A-Fa-f0-9-]{8,64}$")
_CODE_PATH_RE = re.compile(r"^\s*codePath=(\S+)\s*$", re.M)
_SIGNING_VERSION_RE = re.compile(r"apkSigningVersion=(\d+)")
_EXPLICIT_CERT_RE = re.compile(
    r"(?:certDigest=|SHA-256 digest:\s*)((?:[0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2}|[0-9A-Fa-f]{64})"
)
_SIGNATURE_BLOB_RE = re.compile(r"signatures:\[([0-9A-Fa-f,\s]+)\]")
_PM_PATH_RE = re.compile(r"^package:(/[-A-Za-z0-9._@=+/~]+\.apk)$")
_SUM_RE = re.compile(r"\b([0-9a-fA-F]{32}|[0-9a-fA-F]{64})\b")


@dataclass(frozen=True)
class ProofResult:
    ok: bool
    summary: str
    error: str
    data: dict[str, Any]


def valid_package_id(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text) and ".." not in text and bool(PACKAGE_ID_RE.fullmatch(text))


def valid_serial(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text) and not text.startswith("-") and bool(SERIAL_RE.fullmatch(text))


def valid_udid(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text) and ".." not in text and bool(UDID_RE.fullmatch(text))


def empty_digest() -> dict[str, str]:
    return {"sha256": "", "md5": "", "source": "", "path": "", "reason": ""}


def empty_signing() -> dict[str, Any]:
    return {"apk_signing_version": None, "cert_sha256": "", "source": ""}


def empty_app() -> dict[str, Any]:
    return {
        "package_id": "",
        "installed": None,
        "version_name": "",
        "version_code": None,
        "install_path": "",
        "digest": empty_digest(),
        "signing": empty_signing(),
        "splits": [],
    }


def empty_drivers() -> dict[str, Any]:
    return {
        "adb": {"serial": "", "server_port": None},
        "wda": {"url": "", "port": None, "reachable": None, "version": "", "udid": "", "error": ""},
        "appium": {"url": "", "port": None},
    }


def empty_device(platform: str) -> dict[str, Any]:
    return {
        "id": "",
        "serial": "",
        "reported_serial": "",
        "udid": "",
        "platform": platform,
        "model": "",
        "name": "",
        "os_version": "",
        "id_source": "",
        "ids_agree": None,
        "hint_id": "",
        "hint_match": None,
    }


def parse_code_path(dumpsys: str) -> str:
    match = _CODE_PATH_RE.search(dumpsys or "")
    if not match:
        return ""
    path = match.group(1).strip()
    if ".." in path or not path.startswith("/"):
        return ""
    return path


def parse_signing(dumpsys: str) -> dict[str, Any]:
    """Cert fingerprint from the dumpsys already fetched for version. No second inventory."""
    text = dumpsys or ""
    version = None
    found = _SIGNING_VERSION_RE.search(text)
    if found:
        version = int(found.group(1))
    explicit = _EXPLICIT_CERT_RE.search(text)
    if explicit:
        return {
            "apk_signing_version": version,
            "cert_sha256": explicit.group(1).replace(":", "").lower(),
            "source": "cert_digest",
        }
    for blob in _SIGNATURE_BLOB_RE.findall(text):
        for token in re.findall(r"[0-9A-Fa-f]{200,16384}", blob):
            if len(token) % 2:
                continue
            try:
                raw = bytes.fromhex(token)
            except ValueError:
                continue
            if len(raw) < 64:
                continue
            return {
                "apk_signing_version": version,
                "cert_sha256": hashlib.sha256(raw).hexdigest(),
                "source": "signature_der",
            }
    return {"apk_signing_version": version, "cert_sha256": "", "source": ""}


def parse_pm_paths(text: str) -> list[str]:
    paths: list[str] = []
    for line in (text or "").splitlines():
        match = _PM_PATH_RE.fullmatch(line.strip())
        if not match:
            continue
        path = match.group(1)
        if ".." in path or path in paths:
            continue
        paths.append(path)
    return paths


def parse_sum_line(text: str, *, kind: str) -> str:
    """kind is sha256 (64 hex) or md5 (32 hex)."""
    want = 64 if kind == "sha256" else 32
    for token in _SUM_RE.findall(text or ""):
        if len(token) == want:
            return token.lower()
    return ""


def order_apk_paths(paths: list[str], *, limit: int = 8) -> tuple[list[str], bool]:
    base = [path for path in paths if path.endswith("/base.apk")]
    rest = [path for path in paths if path not in base]
    ordered = base + rest
    return ordered[:limit], len(ordered) > limit


def parse_endpoint(url: Any = "", port: Any = None) -> dict[str, Any]:
    """Driver URL already present on the hint. Explicit port, no userinfo, no query."""
    result: dict[str, Any] = {"url": "", "port": None}
    raw = str(url or "").strip()
    if raw:
        try:
            parsed = urlsplit(raw)
            parsed.port  # raises on an illegal port
        except ValueError:
            parsed = None
        if (
            parsed is not None
            and parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and parsed.port is not None
        ):
            host = parsed.hostname
            if ":" in host:
                host = f"[{host}]"
            result = {
                "url": f"{parsed.scheme}://{host}:{parsed.port}{parsed.path or ''}",
                "port": int(parsed.port),
            }
    if result["port"] is None and port not in (None, ""):
        try:
            number = int(port)
        except (TypeError, ValueError):
            number = 0
        if 1 <= number <= 65535:
            result["port"] = number
    return result


def adb_server_port() -> int | None:
    """Port this process would use. Unset env stays null — 5037 is not reported as observed."""
    raw = str(os.environ.get("ANDROID_ADB_SERVER_PORT") or "").strip()
    if raw.isdigit():
        number = int(raw)
        if 1 <= number <= 65535:
            return number
    return None


def runtime_facts(node_id: str) -> dict[str, Any]:
    from mino_scout.app_version import process_build_identity, report_scout_version

    try:
        identity = process_build_identity()
    except Exception:
        identity = {}
    if not isinstance(identity, dict):
        identity = {}
    unchanged = identity.get("source_unchanged") if "source_unchanged" in identity else None
    if unchanged is not None:
        unchanged = bool(unchanged)
    return {
        "scout_version": str(report_scout_version() or ""),
        "node_id": str(node_id or ""),
        "build_id": str(identity.get("build_id") or ""),
        "identity_kind": str(identity.get("identity_kind") or ""),
        "source_unchanged": unchanged,
    }


def missing_fields(data: dict[str, Any], *, package_requested: bool) -> list[str]:
    missing: list[str] = []
    device = data.get("device") or {}
    runtime = data.get("runtime") or {}
    app = data.get("app") or {}
    digest = app.get("digest") or {}
    signing = app.get("signing") or {}
    if not device.get("id"):
        missing.append("device.id")
    if not runtime.get("scout_version"):
        missing.append("runtime.scout_version")
    if not runtime.get("node_id"):
        missing.append("runtime.node_id")
    if package_requested:
        if not app.get("package_id"):
            missing.append("app.package_id")
        if app.get("installed") is not True:
            missing.append("app.installed")
        if not digest.get("sha256"):
            missing.append("app.digest.sha256")
        if not digest.get("md5"):
            missing.append("app.digest.md5")
        if not signing.get("cert_sha256"):
            missing.append("app.signing.cert_sha256")
    return sorted(missing)


def assemble(*, platform: str, device: dict[str, Any], app: dict[str, Any], drivers: dict[str, Any], node_id: str, package_requested: bool) -> dict[str, Any]:
    data = {
        "schema": SCHEMA,
        "device": device,
        "app": app,
        "runtime": runtime_facts(node_id),
        "drivers": drivers,
        "missing": [],
    }
    data["missing"] = missing_fields(data, package_requested=package_requested)
    return data


def finish(data: dict[str, Any], *, ok: bool, error: str = "") -> ProofResult:
    device = data.get("device") or {}
    app = data.get("app") or {}
    digest = str((app.get("digest") or {}).get("sha256") or "")
    parts = [f"self_prove {device.get('platform') or '-'} {device.get('id') or '-'}"]
    if app.get("package_id"):
        parts.append(str(app["package_id"]))
    if digest:
        parts.append(f"sha256={digest[:12]}")
    missing = data.get("missing") or []
    if missing:
        parts.append("missing=" + ",".join(missing))
    summary = " ".join(parts)[:300]
    if not ok and error:
        summary = error[:300]
    return ProofResult(ok=ok, summary=summary, error="" if ok else (error or summary), data=data)


def failed(*, platform: str, error: str, node_id: str = "", package_requested: bool = False, serial: str = "", hint_id: str = "") -> ProofResult:
    device = empty_device(platform)
    device["serial"] = serial
    device["hint_id"] = hint_id
    drivers = empty_drivers()
    if platform == "android":
        drivers["adb"]["serial"] = serial
        drivers["adb"]["server_port"] = adb_server_port()
    data = assemble(
        platform=platform,
        device=device,
        app=empty_app(),
        drivers=drivers,
        node_id=node_id,
        package_requested=package_requested,
    )
    return finish(data, ok=False, error=error)
