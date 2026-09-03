"""adb 截图通道：假 subprocess，不需要真设备。

    python tests/test_screen_adb.py
"""
from __future__ import annotations

import base64
import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mino_scout import screen as S  # noqa: E402
from mino_scout.executors.base import DeviceRef  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{' — ' + detail if detail else ''}")
        failures.append(name)


def make_png(w: int, h: int) -> bytes:
    """非空白 PNG：息屏全黑帧会被 png_is_blank 丢掉，测试图必须有内容。"""

    def chunk(t: bytes, d: bytes) -> bytes:
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    rows = []
    for y in range(h):
        row = bytearray([0])
        for x in range(w):
            row.extend((80 + (x + y) % 40, 110, 140))
        rows.append(bytes(row))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )


PNG = make_png(1080, 2340)


class _Proc:
    def __init__(self, rc: int, out: bytes, err: bytes = b"") -> None:
        self.returncode = rc
        self.stdout = out
        self.stderr = err


print("== peek_png_size 从文件头读尺寸 ==")
check("1080x2340", S.peek_png_size(PNG) == (1080, 2340), str(S.peek_png_size(PNG)))
check("非 PNG 返回 (0,0)", S.peek_png_size(b"notapng") == (0, 0))

print("== adb 通道成功 ==")
CMD: list[list[str]] = []
subprocess.run = lambda cmd, **kw: (CMD.append(list(cmd)), _Proc(0, PNG))[1]  # type: ignore[assignment]
sh = S.capture(DeviceRef(sn="R5", adb_serial="R5CT30xxxx"))
check("has_image", sh.has_image(), sh.error)
check("source=adb", sh.source == "adb")
check("尺寸解析", (sh.width, sh.height) == (1080, 2340), f"{sh.width}x{sh.height}")
joined = " ".join(CMD[-1]) if CMD else ""
check("用了 exec-out screencap", "exec-out" in joined and "screencap" in joined, str(CMD[-1] if CMD else ""))
check("base64 可解回原字节", base64.b64decode(sh.image_base64) == PNG)
check("落盘了", bool(sh.path) and os.path.exists(sh.path), sh.path)
p = sh.path
S.cleanup(sh)
check("cleanup 删掉了", not os.path.exists(p))

print("== claw-* 伪 serial 不走 adb ==")
sh = S.capture(DeviceRef(sn="claw-abc", adb_serial="claw-abc"))
check("不 ok", not sh.has_image())
check("原因 invalid serial", "invalid adb serial" in sh.error, sh.error)

print("== adb rc!=0 ==")
subprocess.run = lambda cmd, **kw: _Proc(1, b"", b"device offline")  # type: ignore[assignment]
sh = S.capture(DeviceRef(sn="R5", adb_serial="R5CT30xxxx"))
check("不 ok", not sh.has_image())
check("带上 stderr", "device offline" in sh.error, sh.error)

print("== adb 不在 PATH ==")


def _boom(cmd, **kw):
    raise FileNotFoundError("adb")


subprocess.run = _boom  # type: ignore[assignment]
sh = S.capture(DeviceRef(sn="R5", adb_serial="R5CT30xxxx"))
check("不 ok", not sh.has_image())
check("提示 PATH", "PATH" in sh.error, sh.error)

print("== 未搬迁的通道要明说，不静默 ==")
subprocess.run = lambda cmd, **kw: _Proc(0, PNG)  # type: ignore[assignment]
sh = S.capture(DeviceRef(sn="claw-abc", adb_serial="claw-abc"), prefer=("remote",))
check("不 ok", not sh.has_image())
check("点名未实现", "未实现" in sh.error, sh.error)

print()
if failures:
    print(f"FAILED {len(failures)}: {', '.join(failures)}")
    sys.exit(1)
print("ALL OK — screen adb 通道")
