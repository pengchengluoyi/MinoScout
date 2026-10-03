"""只跑插件目录里的固定入口。不用 shell 字符串，不提权。"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from mino_scout.log import SLog
from mino_scout.plugins.state import spec

TAG = "PluginExec"
_MAX_OUTPUT = 8192
_MAX_TIMEOUT = 120.0


class HostExecError(Exception):
    pass


def _safe_entry(plugin_root: Path, entry_name: str) -> Path:
    root = plugin_root.resolve()
    name = str(entry_name or "").strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise HostExecError("入口必须是插件目录里的文件名")
    entry = (root / name).resolve()
    if root != entry and root not in entry.parents:
        raise HostExecError("入口不在插件目录内")
    if not entry.is_file():
        raise HostExecError(f"没有入口文件 {name}")
    return entry


def _base_env() -> dict[str, str]:
    keep = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "SystemRoot", "WINDIR")
    return {k: os.environ[k] for k in keep if os.environ.get(k)}


def redact(text: str, secrets: list[str]) -> str:
    out = str(text or "")
    for secret in secrets:
        token = str(secret or "")
        if len(token) >= 4:
            out = out.replace(token, "***")
    if len(out) > _MAX_OUTPUT:
        out = out[:_MAX_OUTPUT] + "\n…截断"
    return out


def _plugin_secrets(node_id: str, kind: str, plugin_id: str) -> list[str]:
    from mino_scout.plugins.state import secret_value

    row = spec(kind, plugin_id) or {}
    found: list[str] = []
    for field in row.get("fields") or []:
        if not field.get("secret"):
            continue
        value = secret_value(node_id, kind, plugin_id, str(field.get("key") or ""))
        if value:
            found.append(value)
    return found


def run_entry(
    plugin_root: Path,
    entry_name: str,
    argv: list[str],
    *,
    timeout_sec: float,
    node_id: str,
    kind: str,
    plugin_id: str,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    entry = _safe_entry(plugin_root, entry_name)
    if os.name != "nt":
        mode = entry.stat().st_mode
        if not mode & 0o111:
            entry.chmod(mode | 0o755)
    limit = min(_MAX_TIMEOUT, max(1.0, float(timeout_sec or _MAX_TIMEOUT)))
    # 安装脚本允许更长，由调用方传入，这里只拦住单次 CLI。
    if entry_name in ("install.sh", "install.ps1", "uninstall.sh", "uninstall.ps1"):
        limit = max(1.0, float(timeout_sec or 600.0))
    env = _base_env()
    env["MINO_SCOUT_PLUGIN_INSTALL"] = "1"
    for key, value in (extra_env or {}).items():
        name = str(key or "").strip()
        if not name or not name.replace("_", "").isalnum():
            continue
        env[name] = str(value)
    args = [str(entry)]
    for part in argv or []:
        text = str(part)
        if "\n" in text or "\x00" in text:
            raise HostExecError("参数不能含换行")
        args.append(text)
    if entry.suffix.lower() == ".ps1":
        args = ["powershell", "-NoProfile", "-NonInteractive", "-File", str(entry), *args[1:]]
    started = time.time()
    SLog.i(TAG, f"run {kind}/{plugin_id} {entry.name} node={node_id}")
    proc = subprocess.Popen(
        args,
        cwd=str(plugin_root.resolve()),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        _kill(proc)
        raise HostExecError(f"{entry.name} 超时（{int(limit)}s）") from None
    secrets = _plugin_secrets(node_id, kind, plugin_id)
    elapsed = int((time.time() - started) * 1000)
    SLog.i(TAG, f"done {kind}/{plugin_id} {entry.name} code={proc.returncode} {elapsed}ms")
    return {
        "code": int(proc.returncode or 0),
        "stdout": redact(out.decode("utf-8", "replace"), secrets),
        "stderr": redact(err.decode("utf-8", "replace"), secrets),
        "elapsed_ms": elapsed,
    }


def _kill(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        proc.kill()
        proc.communicate()
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        proc.kill()
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
