"""Fixed internal stop entry; never accepts a command or escalates to SIGKILL."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import psutil


def listeners(port):
    if sys.platform == "darwin":
        # psutil.net_connections is root-only on macOS; lsof can enumerate our user's listeners.
        result = subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-t"],
                                capture_output=True, text=True, timeout=5)
        if result.returncode not in {0, 1} or result.stderr.strip():
            raise ValueError("cannot inspect listener ownership")
        return {int(line) for line in result.stdout.splitlines() if line.strip().isdigit()}
    return {c.pid for c in psutil.net_connections(kind="tcp")
            if c.status == psutil.CONN_LISTEN and c.laddr.port == port and c.pid}


def same_user(proc):
    return proc.uids().real == os.getuid() if os.name == "posix" else proc.username().casefold() == psutil.Process().username().casefold()


def close_windows(pid):
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    windows = []
    @callback_type
    def collect(hwnd, unused):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            windows.append(hwnd)
        return True
    user32.EnumWindows(collect, 0)
    if not windows:
        raise ValueError("目标没有可协作关闭的窗口；请在 PyCharm Rerun，不使用强制终止")
    for hwnd in windows:
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid or not user32.PostMessageW(hwnd, 0x0010, 0, 0):
            raise ValueError("窗口身份变化或关闭请求被拒绝")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--created", type=float, required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--ide-port", type=int, required=True)
    ap.add_argument("--source", required=True)
    args = ap.parse_args(argv)
    if os.name not in {"posix", "nt"} or args.port in {10104, 5273, 50101, 50104, 60101, 60104, args.ide_port}:
        raise ValueError("unsupported platform or protected port")
    proc = psutil.Process(args.pid)
    if proc.create_time() != args.created or not same_user(proc):
        raise ValueError("process identity changed")
    if Path(proc.cwd()).resolve() != Path(args.source).resolve():
        raise ValueError("source directory changed")
    if listeners(args.port) != {args.pid}:
        raise ValueError("port ownership changed")
    ide_pids = listeners(args.ide_port)
    if not any(p.pid in ide_pids and "pycharm" in (p.name() + " " + p.exe()).lower() for p in proc.parents()):
        raise ValueError("IDE parent mismatch")
    if proc.create_time() != args.created:
        raise ValueError("process identity changed before signal")
    if os.name == "nt":
        close_windows(proc.pid)
    else:
        proc.send_signal(signal.SIGTERM)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
