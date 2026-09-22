"""mino-scout 命令行。

    mino-scout                                 服务在跑 → 公开 status；否则尝试拉起服务
    mino-scout run                             常驻（launchd / systemd 用）
    mino-scout probe                           只探测 manifest，不连 Nexus
    mino-scout status | stop | update | start
    mino-scout configure nexus-url <origin>    只改 Nexus 地址（token 由 Nexus REGISTER 下发）

`probe` 子命令是部署新节点时的第一步（docs/DEVICE_SETUP.md §7）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys

from mino_scout.core import SCOUT_VERSION, ScoutCore
from mino_scout.log import SLog
from mino_scout.service import (
    clear_pid,
    cmd_public_status,
    collect_status,
    request_stop,
    try_start_service,
    write_pid,
)

TAG = "CLI"


def build_core() -> ScoutCore:
    """按本机实际情况装配 executor。"""
    executors: dict = {}

    from mino_scout.executors.adb_executor import AdbExecutor

    executors["adb"] = AdbExecutor()

    try:
        from mino_scout.executors.playwright_executor import PlaywrightExecutor

        executors["playwright"] = PlaywrightExecutor()
    except ImportError as exc:
        SLog.w(TAG, f"playwright executor 未装载: {exc}")

    return ScoutCore(executors)


def cmd_probe(core: ScoutCore) -> int:
    execs, devices = core.manifest()
    out = {
        "scout_id": core.scout_id,
        "node_id": core.node_id,
        "scout_version": SCOUT_VERSION,
        "executors": [
            {"id": e.id, "available": e.available, "provides": list(e.provides), "reason": e.reason}
            for e in execs
        ],
        "devices": [
            {"sn": d.sn, "platform": d.platform, "model": d.model, "channels": dict(d.channels)}
            for d in devices
        ],
    }
    payload = json.dumps(out, ensure_ascii=True, indent=2)
    try:
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()
    except Exception:
        sys.stdout.buffer.write((payload + "\n").encode("ascii"))
        sys.stdout.buffer.flush()
    if not any(e.available for e in execs):
        print("\n没有任何可用 executor —— 这台机器现在不能作为执行节点。", file=sys.stderr)
        return 1
    if not devices:
        print("\n没发现任何设备。", file=sys.stderr)
    return 0


def cmd_status() -> int:
    st = collect_status()
    print(json.dumps(st, ensure_ascii=False, indent=2))
    return 0 if st.get("running") else 1


def cmd_stop() -> int:
    st = request_stop()
    print(json.dumps(st, ensure_ascii=False, indent=2))
    return 0 if st.get("ok") else 1


def cmd_heavy_deps() -> int:
    from mino_scout.heavy_deps import install_heavy_deps

    core = build_core()
    try:
        out = install_heavy_deps(adb_hook=core.ensure_android_adb_keyboard_on_startup)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


def cmd_update(argv: list[str]) -> int:
    from mino_scout.self_update import run_update

    ap = argparse.ArgumentParser(prog="mino-scout update")
    ap.add_argument("--manifest-url", default="", help="覆盖 config manifest_url")
    ns = ap.parse_args(argv)
    try:
        out = run_update(manifest_url=ns.manifest_url)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


def cmd_configure(argv: list[str]) -> int:
    if not argv or argv[0] != "nexus-url":
        print("用法: mino-scout configure nexus-url <http://host:port>", file=sys.stderr)
        return 2
    if len(argv) < 2 or not str(argv[1]).strip():
        print("缺少 Nexus 地址", file=sys.stderr)
        return 2
    from mino_scout.config import config_path, set_nexus_url

    path = set_nexus_url(argv[1])
    print(json.dumps({"ok": True, "config": str(path), "nexus_url": str(argv[1]).strip()}, ensure_ascii=False))
    return 0


def cmd_default() -> int:
    st = collect_status()
    if st.get("running"):
        return cmd_public_status()
    from mino_scout.config import config_path, resolve_runtime

    _, token = resolve_runtime()
    if not token:
        print(
            f"未配置 token。请用 Studio「复制远程安装命令」安装，或在 {config_path()} 写入 token。",
            file=sys.stderr,
        )
        return 2
    return try_start_service()


def cmd_run() -> int:
    from mino_scout.config import config_path, resolve_runtime
    from mino_scout.playwright_hub import apply_browsers_path, install_playwright_closed_quiet

    apply_browsers_path()
    install_playwright_closed_quiet()

    core = build_core()
    nexus, token = resolve_runtime()
    if not token:
        print(f"需要 token：{config_path()} 或 Studio 安装流程写入", file=sys.stderr)
        return 2

    from mino_scout.transport.node import NodeTransport

    transport = NodeTransport(core, nexus_url=nexus, token=token)
    write_pid()
    _install_signals(transport)
    try:
        asyncio.run(transport.run_forever())
    except KeyboardInterrupt:
        transport.request_shutdown()
        SLog.i(TAG, "收到 Ctrl-C，退出")
    finally:
        try:
            core.shutdown()
        except Exception:
            pass
        from mino_scout.power import get_guard

        get_guard().sync([])
        clear_pid(only_if=_os_getpid())
    return 0


def _install_signals(transport) -> None:
    def _handle(signum, _frame):
        SLog.i(TAG, f"收到信号 {signum}，准备退出")
        transport.request_shutdown()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _handle)


def _os_getpid() -> int:
    import os

    return os.getpid()


def main(argv: list[str] | None = None) -> int:
    from mino_scout.config import config_path, configure_proxy_bypass

    configure_proxy_bypass()

    raw = list(argv) if argv is not None else sys.argv[1:]
    if not raw:
        return cmd_default()
    if raw[0] in ("-h", "--help"):
        return _legacy_argparse_entry(raw)

    sub = raw[0]
    if sub == "configure":
        return cmd_configure(raw[1:])
    if sub == "status":
        return cmd_status()
    if sub == "stop":
        return cmd_stop()
    if sub == "update":
        return cmd_update(raw[1:])
    if sub == "heavy-deps":
        return cmd_heavy_deps()
    if sub in ("run", "start"):
        if sub == "start" and not raw[1:]:
            st = collect_status()
            if not st.get("running"):
                return try_start_service()
            return cmd_public_status()
        if sub == "start":
            print("start 仅用于拉起后台服务；前台常驻请用 mino-scout run", file=sys.stderr)
            return 2
        return cmd_run()
    if sub == "probe":
        return cmd_probe(build_core())

    print(f"未知命令: {sub}（可用: run start probe status stop update heavy-deps configure）", file=sys.stderr)
    return 2


def _legacy_argparse_entry(argv: list[str] | None = None) -> int:
    """保留 --help 文案。"""
    from mino_scout.config import config_path

    ap = argparse.ArgumentParser(
        prog="mino-scout",
        description="Mino Scout 执行器",
        epilog=f"无子命令时：服务已运行则公开 status，否则尝试启动服务。配置见 {config_path()}。",
    )
    ap.parse_args(argv)
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help"):
        sys.exit(_legacy_argparse_entry(sys.argv[1:]))
    sys.exit(main())
