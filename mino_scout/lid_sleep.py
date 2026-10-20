"""Opt-in macOS closed-lid execution with a privileged, leased sleep inhibitor.

The helper executes only fixed Apple tools. No user-owned code is run as root.
The user authorizes installation through macOS; Scout never receives a password.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys
import threading
import time

from mino_scout.log import SLog

LABEL = "com.mino.scout.lid-sleep"
ROOT = Path("/var/db") / LABEL
HELPER = Path("/Library/PrivilegedHelperTools") / LABEL
PLIST = Path("/Library/LaunchDaemons") / (LABEL + ".plist")
CONFIG_KEY = "macos_closed_lid_execution"

# Root-owned original state survives helper crashes and reboot. launchd restarts
# the watchdog; abandoned leases restore the saved state. A lease is valid
# only for a live process of the authorized UID and expires after 15 seconds.
HELPER_SOURCE = r'''#!/bin/sh
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
umask 077
root=/var/db/com.mino.scout.lid-sleep
uid=__UID__
state="$root/original"
status() {
    printf '%s\n' "$1" > "$root/status.next"
    chmod 644 "$root/status.next"
    mv -f "$root/status.next" "$root/status"
}
current() {
    raw=$(/usr/bin/pmset -g) || return 1
    printf '%s\n' "$raw" | /usr/bin/awk '
      $1 == "SleepDisabled" { found=1; if ($2 != 0 && $2 != 1) exit 1; print $2; exit }
      END { if (!found) print 0 }'
}
restore() {
    [ -f "$state" ] || return 0
    old=$(cat "$state")
    case "$old" in 0|1) ;; *) status error_state; return 1;; esac
    /usr/bin/pmset -a disablesleep "$old" || { status error_restore; return 1; }
    [ "$(current)" = "$old" ] || { status error_restore; return 1; }
    rm -f "$state"
}
finish() {
    if restore; then status idle; exit 0; else exit 1; fi
}
trap finish TERM INT HUP
# Preserve recovery state across restarts. Do not briefly enable sleep while
# the lid is closed and another Scout still owns a live lease.
while :; do
    wanted=0
    now=$(date +%s)
    for file in "$root/leases/"*.lease; do
        [ -f "$file" ] && [ ! -L "$file" ] || continue
        pid=${file##*/}; pid=${pid%.lease}
        case "$pid" in ''|*[!0-9]*) continue;; esac
        [ "$pid" -gt 1 ] || continue
        stamp=$(stat -f %m "$file") || continue
        age=$((now - stamp))
        [ "$age" -ge 0 ] && [ "$age" -le 15 ] || continue
        owner=$(/bin/ps -o uid= -p "$pid" | tr -d ' ')
        [ "$owner" = "$uid" ] || continue
        wanted=1
        break
    done
    if [ "$wanted" = 1 ]; then
        if [ ! -f "$state" ]; then
            old=$(current) || { status error_read; sleep 3; continue; }
            case "$old" in 0|1) ;; *) status error_read; sleep 3; continue;; esac
            printf '%s\n' "$old" > "$state.next" && mv -f "$state.next" "$state" || exit 1
            /bin/sync
        fi
        if [ "$(current)" != 1 ]; then
            /usr/bin/pmset -a disablesleep 1 || { status error_apply; sleep 3; continue; }
        fi
        if [ "$(current)" = 1 ]; then status active; else status error_apply; fi
    else
        if restore; then status idle; fi
    fi
    sleep 3
done
'''


def _root_owned(path: Path) -> bool:
    try:
        st = path.lstat()
        return not path.is_symlink() and st.st_uid == 0 and not st.st_mode & 0o022
    except OSError:
        return False


def helper_status() -> dict:
    if sys.platform != "darwin":
        return {"active": False, "reason": "macos_only"}
    try:
        if not all(_root_owned(p) for p in (ROOT, ROOT / "owner", ROOT / "status")):
            return {"active": False, "reason": "authorization_required"}
        if (ROOT / "owner").read_text().strip() != str(os.getuid()):
            return {"active": False, "reason": "helper_owned_by_another_user"}
        path = ROOT / "status"
        if not 0 <= time.time() - path.stat().st_mtime <= 15:
            return {"active": False, "reason": "helper_unavailable"}
        state = path.read_text().strip()
        return {"active": state == "active", "reason": state}
    except OSError:
        return {"active": False, "reason": "helper_unavailable"}


def _installer_command(uid: int) -> str:
    if type(uid) is not int or uid <= 0:
        raise ValueError("请以已登录的普通用户启用功能，不要以 root 运行 Scout")
    source = HELPER_SOURCE.replace("__UID__", str(uid))
    plist = plistlib.dumps({"Label": LABEL, "ProgramArguments": ["/bin/sh", str(HELPER)],
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 5,
        "ProcessType": "Background", "ExitTimeOut": 15})
    script64 = base64.b64encode(source.encode()).decode()
    plist64 = base64.b64encode(plist).decode()
    # All paths and programs are constants; the UID is validated above. Refuse
    # replacing another user's authorization, symlinks or untrusted root state.
    return f'''set -eu
r={shlex.quote(str(ROOT))}
h={shlex.quote(str(HELPER))}
p={shlex.quote(str(PLIST))}
for x in "$r" "$h" "$p"; do
    [ ! -L "$x" ] || exit 71
done
if [ -e "$r" ]; then
    [ "$(/usr/bin/stat -f %u "$r")" = 0 ] || exit 72
    [ "$(/usr/bin/stat -f %Lp "$r")" = 755 ] || exit 72
    [ "$(/bin/cat "$r/owner")" = {uid} ] || exit 73
fi
/bin/mkdir -p "$r" /Library/PrivilegedHelperTools
/usr/sbin/chown root:wheel "$r"
/bin/chmod 755 "$r"
printf '%s\\n' {uid} > "$r/owner"
/bin/chmod 644 "$r/owner"
# Keep the old root-owned recovery state across updates.
if /bin/launchctl print system/{LABEL} >/dev/null 2>&1; then
    /bin/launchctl bootout system/{LABEL}
fi
/bin/mkdir -p "$r/leases"
/usr/sbin/chown {uid} "$r/leases"
/bin/chmod 700 "$r/leases"
printf %s {script64} | /usr/bin/base64 -D > "$h.next"
/usr/sbin/chown root:wheel "$h.next"
/bin/chmod 700 "$h.next"
/bin/mv -f "$h.next" "$h"
printf %s {plist64} | /usr/bin/base64 -D > "$p.next"
/usr/sbin/chown root:wheel "$p.next"
/bin/chmod 644 "$p.next"
/bin/mv -f "$p.next" "$p"
/bin/launchctl bootstrap system "$p"
'''


def configure(mode: str, *, startup: bool = False) -> dict:
    from mino_scout.config import load_config, save_config
    if mode not in {"on", "off", "status"}:
        raise ValueError("用法: mino-scout configure closed-lid on|off|status")
    if sys.platform != "darwin":
        raise ValueError("合盖运行目前仅支持 macOS")
    cfg = load_config()
    if startup and cfg.get(CONFIG_KEY) is False:
        return {"enabled": False, "active": False, "reason": "disabled"}
    if mode == "status":
        return {"enabled": cfg.get(CONFIG_KEY) is True, **helper_status()}
    if mode == "on" and helper_status()["reason"] not in {"active", "idle"}:
        command = _installer_command(os.getuid())
        # macOS displays the permission/password dialog. No credential crosses
        # Scout's stdin, stdout, configuration or logs. Cancellation leaves opt-in off.
        prompt = "启用 Scout 合盖运行（含电池供电）：运行期间禁用整机睡眠，耗电增加；请保持通风。退出或停止保活后自动恢复。"
        applescript = "do shell script " + json.dumps(command) + " with administrator privileges with prompt " + json.dumps(prompt, ensure_ascii=False)
        result = subprocess.run(["/usr/bin/osascript", "-e", applescript], capture_output=True, text=True)
        if result.returncode:
            raise ValueError("合盖运行授权已取消或辅助服务安装失败；配置保持原值，请查询 status")
    # The authorization dialog can stay open while other settings are edited.
    # Merge into a fresh config, and honor an explicit off during startup.
    cfg = load_config()
    if startup and cfg.get(CONFIG_KEY) is False:
        return {"enabled": False, "active": False, "reason": "disabled"}
    cfg[CONFIG_KEY] = mode == "on"
    save_config(cfg)
    return {"enabled": mode == "on", **helper_status(),
            "note": "当前 Scout 会自动读取设置；关闭或进程退出后租约最长 15 秒到期，辅助服务每 3 秒恢复一次。"}


class LidSleepLease:
    """One per Scout process, renewed only while PowerGuard has holders."""
    def __init__(self):
        self._stop = threading.Event()
        self._thread = None
        self._startup_attempted = False
        self._startup_error = ""
        self._path = ROOT / "leases" / f"{os.getpid()}.lease"
        self.state = {"active": False, "reason": "disabled"}

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="scout-lid-sleep", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._remove()
        self.state = {"active": False, "reason": "released"}

    def _remove(self):
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass  # The privileged helper expires abandoned leases independently.

    def _authorize_on_startup(self):
        from mino_scout.config import load_config
        if self._startup_attempted or self._stop.is_set():
            return
        self._startup_attempted = True
        # Missing preference means first launch. Only an explicit off disables
        # automatic authorization; reconnect/keepalive must never prompt again.
        if load_config().get(CONFIG_KEY) is False:
            return
        self.state = {"active": False, "reason": "authorizing"}
        SLog.i("PowerGuard", "正在检查合盖运行权限；首次启用将请求 macOS 管理员授权")
        try:
            configure("on", startup=True)
        except (ValueError, OSError):
            self._startup_error = "authorization_failed_or_cancelled"
            self.state = {"active": False, "reason": self._startup_error}
            SLog.w("PowerGuard", "合盖运行授权未完成；本次进程不再自动请求，可手动开启或下次启动重试")

    def _run(self):
        from mino_scout.config import load_config
        previous = None
        try:
            self._authorize_on_startup()
            while not self._stop.is_set():
                try:
                    enabled = load_config().get(CONFIG_KEY) is True
                    if not enabled:
                        self._remove()
                        self.state = {"active": False, "reason": self._startup_error or "disabled"}
                    else:
                        state = helper_status()
                        if state["reason"] in {"active", "idle"} or state["reason"].startswith("error_"):
                            # Atomic rename also replaces an old symlink without
                            # following it; no writes outside the lease directory.
                            temporary = self._path.with_suffix(".next")
                            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                            with os.fdopen(fd, "w") as stream:
                                stream.write(str(os.getpid()) + "\n")
                            os.replace(temporary, self._path)
                        self.state = state
                    reason = self.state["reason"]
                    if reason != previous:
                        message = f"合盖运行: {reason}"
                        if enabled and reason not in {"active", "idle"}:
                            SLog.w("PowerGuard", message)
                        else:
                            SLog.i("PowerGuard", message)
                        previous = reason
                except Exception as exc:
                    self.state = {"active": False, "reason": "lease_failed"}
                    if previous != "lease_failed":
                        SLog.w("PowerGuard", f"合盖运行保活失败: {type(exc).__name__}")
                        previous = "lease_failed"
                self._stop.wait(3)
        finally:
            self._remove()
