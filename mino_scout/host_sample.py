"""心跳附带的本机功耗采样。不启额外进程，不用需要 root 的 powermetrics。"""
from __future__ import annotations

import os
import re
import subprocess
import sys

_PERCENT_RE = re.compile(r"(\d+)\s*%")


def sample_host(*, mode: str = "running") -> dict:
    """给 HEARTBEAT.host。采样失败留空字段，不抛。"""
    host: dict = {
        "mode": "asleep" if str(mode or "") == "asleep" else "running",
        "power_source": "",
        "charging": "",
        "battery_percent": None,
        "inhibit": _inhibit_on(),
        "cpu_percent": None,
        "rss_mb": None,
        "children": {"Chromium": 0, "caffeinate": 0, "adb": 0},
    }
    _fill_power(host)
    _fill_self_usage(host)
    host["children"] = _child_counts()
    return host


def _inhibit_on() -> bool:
    try:
        from mino_scout.power import get_guard

        st = get_guard().status()
    except Exception:
        return False
    return bool(st.get("active") and st.get("child_alive"))


def _fill_power(host: dict) -> None:
    if sys.platform != "darwin":
        return
    try:
        out = subprocess.run(
            ["pmset", "-g", "batt"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        blob = (out.stdout or "") + (out.stderr or "")
    except Exception:
        return
    if "AC Power" in blob:
        host["power_source"] = "ac"
    elif "Battery Power" in blob:
        host["power_source"] = "battery"
    m = _PERCENT_RE.search(blob)
    if m:
        host["battery_percent"] = int(m.group(1))
    low = blob.lower()
    if "not charging" in low:
        host["charging"] = "not charging"
    elif "discharging" in low:
        host["charging"] = "discharging"
    elif "finishing charge" in low or re.search(r"\bcharged\b", low):
        host["charging"] = "charged"
    elif "charging" in low:
        host["charging"] = "charging"


def _fill_self_usage(host: dict) -> None:
    pid = os.getpid()
    try:
        if sys.platform == "darwin":
            out = subprocess.run(
                ["ps", "-o", "%cpu=,rss=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=2,
            )
            parts = (out.stdout or "").split()
            if len(parts) >= 2:
                host["cpu_percent"] = round(float(parts[0]), 1)
                host["rss_mb"] = round(int(float(parts[1])) / 1024, 1)
            return
        out = subprocess.run(
            ["ps", "-o", "pcpu=,rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
        )
        parts = (out.stdout or "").split()
        if len(parts) >= 2:
            host["cpu_percent"] = round(float(parts[0]), 1)
            host["rss_mb"] = round(int(float(parts[1])) / 1024, 1)
    except Exception:
        return


def _child_counts() -> dict[str, int]:
    counts = {"Chromium": 0, "caffeinate": 0, "adb": 0}
    try:
        cmd = ["ps", "-ax", "-o", "pid=,ppid=,comm="] if sys.platform == "darwin" else [
            "ps", "-e", "-o", "pid=,ppid=,comm=",
        ]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
    except Exception:
        return counts
    children: dict[int, list[int]] = {}
    names: dict[int, str] = {}
    for line in (out.stdout or "").splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        names[pid] = parts[2]
        children.setdefault(ppid, []).append(pid)
    stack = [os.getpid()]
    seen: set[int] = set()
    while stack:
        cur = stack.pop()
        for child in children.get(cur, []):
            if child in seen:
                continue
            seen.add(child)
            stack.append(child)
            base = names.get(child, "").lower().rsplit("/", 1)[-1]
            if "chromium" in base or "chrome" in base or "headless_shell" in base:
                counts["Chromium"] += 1
            elif base.startswith("caffeinate"):
                counts["caffeinate"] += 1
            elif base == "adb":
                counts["adb"] += 1
    return counts
