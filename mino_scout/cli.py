"""mino-scout 命令行。

    mino-scout probe                          只探测并打印 manifest，不连 Nexus
    mino-scout status                         本机守护进程在不在
    mino-scout stop                           优雅退出（先发 shutting_down）
    mino-scout --nexus ws://mino.local:10104/node --token xxx    常驻

`probe` 子命令是部署新节点时的第一步（docs/DEVICE_SETUP.md §7）：
它打印的就是 REGISTER 里将要上报的 executors[] / devices[]。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys

from mino_scout.core import SCOUT_VERSION, ScoutCore
from mino_scout.log import SLog
from mino_scout.service import clear_pid, collect_status, request_stop, write_pid

TAG = "CLI"


def build_core(node_id: str = "") -> ScoutCore:
    """按本机实际情况装配 executor。

    装不上的（缺 playwright 包等）不进字典 —— manifest 里就不会出现，
    Nexus 的菜单也不会包含它。比"装上但永远 declined"更诚实。
    """
    executors: dict = {}

    from mino_scout.executors.adb_executor import AdbExecutor

    executors["adb"] = AdbExecutor()

    try:
        from mino_scout.executors.playwright_executor import PlaywrightExecutor

        executors["playwright"] = PlaywrightExecutor()
    except ImportError as exc:
        SLog.w(TAG, f"playwright executor 未装载: {exc}")

    # remote / ios_wda 待 EngineFactory（MIGRATION.md E3）搬迁后接入
    return ScoutCore(executors, node_id=node_id)


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
    # ASCII JSON：Windows 冻结进程即使没切到 UTF-8，print 也不会 UnicodeEncodeError。
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


def _install_signals(transport) -> None:
    def _handle(signum, _frame):
        SLog.i(TAG, f"收到信号 {signum}，准备退出")
        transport.request_shutdown()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _handle)


def main(argv: list[str] | None = None) -> int:
    from mino_scout.config import config_path, configure_proxy_bypass, resolve_runtime
    from mino_scout.playwright_hub import apply_browsers_path

    configure_proxy_bypass()
    apply_browsers_path()

    ap = argparse.ArgumentParser(
        prog="mino-scout",
        description="Mino Scout 执行器",
        epilog=f"无 --nexus/--token 时读 {config_path()}（Studio 写入的 nexus_url + token）。",
    )
    ap.add_argument("command", nargs="?", default="run", choices=["run", "probe", "status", "stop"])
    ap.add_argument("--nexus", default="", help="Nexus node 接入端点，覆盖配置文件")
    ap.add_argument("--token", default="", help="配对 token，覆盖配置文件")
    ap.add_argument("--node-id", default="", help="覆盖自动生成的 scout_id（仅字母数字）")
    args = ap.parse_args(argv)

    if args.command == "status":
        return cmd_status()
    if args.command == "stop":
        return cmd_stop()

    core = build_core(args.node_id)

    if args.command == "probe":
        return cmd_probe(core)

    nexus, token = resolve_runtime(nexus=args.nexus, token=args.token)
    if not token:
        print(f"需要 --token，或在 {config_path()} 写入 token（Studio 领取安装凭证后会写）", file=sys.stderr)
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
        from mino_scout.power import get_guard

        get_guard().sync([])
        clear_pid(only_if=os_getpid())
    return 0


def os_getpid() -> int:
    import os

    return os.getpid()


if __name__ == "__main__":
    sys.exit(main())
