"""iOS 用例脚本里的定位，以及在连接设备的本机上执行命令。"""
from __future__ import annotations

import os
import subprocess


def locator_params(kind: str, value: str, name: str, label: str) -> dict:
    text = label or name or value
    if not text:
        raise ValueError("locator_missing")
    if kind in {"name", "accessibility_id", "content_desc"}:
        target = {"name": value or name or label}
    elif kind in {"text", "label", "role"}:
        target = {"text": text, "label": text, "name": name or text}
    else:
        raise ValueError("locator_missing")
    return {"point_policy": "node", "target": target}


def device_udid(sn: str, hint: dict) -> str:
    return str(hint.get("udid") or sn or "")


def run_command(script: str, *, udid: str = "", timeout_ms: int = 30000) -> dict:
    body = str(script or "").strip()
    if not body:
        return {"status": "fail", "message": "exec_script 缺 script", "stdout": "", "stderr": "missing script"}
    env = os.environ.copy()
    if udid:
        env["MINO_IOS_UDID"] = udid
    try:
        proc = subprocess.run(
            ["/bin/zsh", "-c", body], capture_output=True, text=True,
            timeout=max(1, timeout_ms) / 1000, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "fail", "message": "脚本没有跑完", "stdout": "", "stderr": str(exc)[:500]}
    if proc.returncode != 0:
        return {"status": "fail", "message": "脚本失败", "stdout": (proc.stdout or "")[:2000], "stderr": (proc.stderr or "")[:500]}
    return {"status": "success", "message": "已执行脚本", "stdout": (proc.stdout or "")[:2000], "stderr": ""}
