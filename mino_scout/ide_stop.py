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


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--created", type=float, required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--ide-port", type=int, required=True)
    ap.add_argument("--source", required=True)
    args = ap.parse_args(argv)
    if os.name != "posix" or args.port in {10104, 5273, args.ide_port}:
        raise ValueError("unsupported platform or protected port")
    proc = psutil.Process(args.pid)
    if proc.create_time() != args.created or proc.uids().real != os.getuid():
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
    proc.send_signal(signal.SIGTERM)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
