"""心跳热插拔：对照上一轮发现结果，只把变化放进 device_delta。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mino_scout.protocol import DeviceManifest  # noqa: E402
from mino_scout.transport.node import changed_devices  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{' — ' + detail if detail else ''}")
        failures.append(name)


def _dev(sn: str, *, platform: str = "android", model: str = "", **channels) -> DeviceManifest:
    return DeviceManifest(sn=sn, platform=platform, model=model, channels=dict(channels))


print("== 无变化不报 ==")
web = _dev("web3f8a1c0e9b2d4f71", platform="web", playwright="connected")
phone = _dev("5fda2f6d", model="25102RKBEC", adb="connected")
prev = {d.sn: d for d in (web, phone)}
delta = changed_devices(prev, [web, phone])
check("空 delta", delta == [], str(delta))

print("== 后插入的 USB 设备 ==")
delta = changed_devices({web.sn: web}, [web, phone])
check("只报新串号", [d.sn for d in delta] == ["5fda2f6d"], str(delta))
check("通道是 connected", delta[0].channels.get("adb") == "connected", str(delta[0].channels))

print("== 拔线标 disconnected ==")
delta = changed_devices(prev, [web])
check("只报消失的那台", [d.sn for d in delta] == ["5fda2f6d"], str(delta))
check("通道变 disconnected", delta[0].channels.get("adb") == "disconnected", str(delta[0].channels))

print("== 授权状态变化 ==")
unauth = _dev("5fda2f6d", model="25102RKBEC", adb="unauthorized")
delta = changed_devices({phone.sn: phone}, [unauth])
check("报通道变化", len(delta) == 1 and delta[0].channels.get("adb") == "unauthorized", str(delta))

print()
if failures:
    print(f"FAILED {len(failures)}: {', '.join(failures)}")
    raise SystemExit(1)
print("ALL OK — device_delta")
