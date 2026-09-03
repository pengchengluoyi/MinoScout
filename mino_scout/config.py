"""Studio-written node config.

Studio writes `{ nexus_url, token }` (and optional `studio_id`) to a per-OS
Application Support path. Scout persists `scout_id` here on first run.
CLI flags `--nexus` / `--token` override the file. `nexus_url` is an HTTP
origin; this module derives the WebSocket `/node` URL.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

LAN_HOST = "mino.local"
DEFAULT_NEXUS_HTTP = f"http://{LAN_HOST}:10104"
DEFAULT_NEXUS_WS = f"ws://{LAN_HOST}:10104/node"
SCOUT_ID_LEN = 16


def config_dir() -> Path:
    override = str(os.environ.get("MINO_SCOUT_HOME") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home()) / "MinoScout"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MinoScout"
    xdg = str(os.environ.get("XDG_CONFIG_HOME") or "").strip()
    if xdg:
        return Path(xdg).expanduser() / "minoscout"
    return Path.home() / ".config" / "minoscout"


def config_path() -> Path:
    return config_dir() / "config.json"


def load_config(path: Path | None = None) -> dict[str, Any]:
    target = path or config_path()
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(data: dict[str, Any], path: Path | None = None) -> Path:
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(target)
    return target


def sanitize_scout_id(raw: str) -> str:
    """Letters and digits only; lowercase. Hyphens / underscores / dots are dropped."""
    return "".join(c for c in str(raw or "").lower() if c.isalnum())


def _machine_id() -> str:
    for candidate in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        try:
            text = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text
    if sys.platform == "darwin":
        try:
            import subprocess

            proc = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=5,
            )
            for line in (proc.stdout or "").splitlines():
                if "IOPlatformUUID" in line:
                    return line.split("=", 1)[-1].strip().strip('"')
        except (OSError, subprocess.TimeoutExpired):
            pass
    if os.name == "nt":
        try:
            import subprocess

            proc = subprocess.run(
                ["reg", "query", r"HKLM\SOFTWARE\Microsoft\Cryptography", "/v", "MachineGuid"],
                capture_output=True, text=True, timeout=5,
            )
            for line in (proc.stdout or "").splitlines():
                if "MachineGuid" in line:
                    return line.strip().split()[-1]
        except (OSError, subprocess.TimeoutExpired):
            pass
    return platform.node() or "scout"


def generate_scout_id() -> str:
    """16 lowercase hex from hostname + machine-id. No hyphens / underscores / dots."""
    material = f"{platform.node()}|{_machine_id()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:SCOUT_ID_LEN]


def resolve_studio_id() -> str:
    return sanitize_scout_id(str(load_config().get("studio_id") or ""))


def resolve_hostname() -> str:
    return platform.node() or ""


def resolve_scout_id(*, override: str = "", persist: bool = True) -> str:
    """CLI `--node-id` wins (sanitized). Else `config.scout_id`. Else generate once."""
    sid = sanitize_scout_id(override)
    if sid:
        return sid
    path = config_path()
    cfg = load_config(path)
    existing = sanitize_scout_id(str(cfg.get("scout_id") or ""))
    if existing:
        if persist and cfg.get("scout_id") != existing:
            cfg["scout_id"] = existing
            save_config(cfg, path)
        return existing
    generated = generate_scout_id()
    if persist:
        cfg["scout_id"] = generated
        save_config(cfg, path)
    return generated


def nexus_ws_url(raw: str) -> str:
    """Turn an HTTP origin or WS URL into the Nexus node endpoint."""
    value = str(raw or "").strip()
    if not value:
        return ""
    value = value.rstrip("/")
    if value.endswith("/node"):
        root = value[: -len("/node")].rstrip("/")
        suffix = "/node"
    else:
        root, suffix = value, "/node"

    if root.startswith("wss://") or root.startswith("ws://"):
        return f"{root}{suffix}"
    if root.startswith("https://"):
        return f"wss://{root[len('https://'):]}{suffix}"
    if root.startswith("http://"):
        return f"ws://{root[len('http://'):]}{suffix}"
    return f"ws://{root}{suffix}"


def resolve_runtime(*, nexus: str = "", token: str = "", config: dict[str, Any] | None = None) -> tuple[str, str]:
    """Return `(ws_url, token)` with CLI flags winning over the config file."""
    cfg = config if config is not None else load_config()
    flag_nexus = str(nexus or "").strip()
    flag_token = str(token or "").strip()
    if flag_nexus:
        ws = nexus_ws_url(flag_nexus)
    else:
        ws = nexus_ws_url(str(cfg.get("nexus_url") or ""))
    if not ws:
        ws = DEFAULT_NEXUS_WS
    tok = flag_token or str(cfg.get("token") or "").strip()
    return ws, tok


def configure_proxy_bypass() -> None:
    """Dial mino.local without going through a system HTTP proxy."""
    extra = ["localhost", "127.0.0.1", "::1", LAN_HOST, "0.0.0.0"]
    for key in ("no_proxy", "NO_PROXY"):
        cur = [p.strip() for p in str(os.environ.get(key) or "").split(",") if p.strip()]
        for host in extra:
            if host not in cur:
                cur.append(host)
        os.environ[key] = ",".join(cur)
