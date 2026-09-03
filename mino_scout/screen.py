"""屏幕采集。

port(scout): 源自 MiniOrangeServer `server/services/regression/screen.py`（578 行）。

**已搬：adb、playwright。** 其余两条通道待后续批次：
  - remote (ClawNode)  依赖 EngineFactory（MIGRATION.md 的 E3），未搬
  - ios_wda            依赖 ios_wda_session + EngineFactory，未搬

与上游的差异：
  - `CapturedScreen` 移到 `mino_scout.schemas`（要进 RESULT 载荷，改成 pydantic）。
    上游的 `image_path` 字段在本仓叫 `path`。
  - `capture_screen(ctx: RunContext, ...)` 的入口签名改成 `capture(device, prefer=...)`
    —— Scout 没有 RunContext（那是 Nexus 的）。
  - Web 压缩比上游从 `system_settings_service.get_ai_web_compress_ratio()` 读（Nexus 设置），
    本仓改为由 `OBSERVE.compress_ratio` 下发（协议 §4.4）。
  - **不做缩略图。** 上游在 router 里 `make_thumb`；拆分后缩略图归 Nexus
    （它才服务 UI）。Scout 回原图。
"""
from __future__ import annotations

import base64
import os
import subprocess
import tempfile
import time
from io import BytesIO
from typing import Any, Optional

from mino_scout.executors.base import DeviceRef
from mino_scout.log import SLog
from mino_scout.schemas import CapturedScreen

TAG = "ScreenCapture"

# 已实现的通道。prefer 里出现其它值会被跳过并记 warn ——
# 这样 Nexus 提前下发 remote/ios_wda 时不会静默变成"抓不到图"。
_IMPLEMENTED = ("adb", "playwright")

# serial → 最近一次成功截图的像素尺寸。input 坐标空间若与截图不一致（wm override），
# AdbExecutor 用它来缩放 tap，避免点到屏幕外。
_LAST_CAPTURE_SIZE: dict[str, tuple[int, int]] = {}


def last_capture_size(serial: str) -> tuple[int, int]:
    return _LAST_CAPTURE_SIZE.get(str(serial or "")) or (0, 0)


def remember_capture_size(serial: str, width: int, height: int) -> None:
    if serial and width > 0 and height > 0:
        _LAST_CAPTURE_SIZE[str(serial)] = (int(width), int(height))


def peek_png_size(data: bytes) -> tuple[int, int]:
    """PNG 文件头里取 width/height。失败返回 (0,0)。

    port: 上游 `screen.py::_peek_png_size`，逐字一致。
    """
    try:
        if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
            return 0, 0
        # IHDR length=13 starts at byte 8; width @16-19, height @20-23
        w = int.from_bytes(data[16:20], "big")
        h = int.from_bytes(data[20:24], "big")
        return w, h
    except Exception:
        return 0, 0


# ---------- ADB 通路 ----------


def capture_via_adb(adb_serial: str, *, timeout_sec: float = 15.0) -> CapturedScreen:
    """port: 上游 `screen.py::_capture_via_adb`，逻辑逐条对齐。"""
    if not adb_serial or adb_serial.startswith("claw-"):
        # claw-* 是 ClawNode 的伪 serial，adb 认不出来 —— 让位给 remote 通道
        return CapturedScreen(ok=False, source="adb", error="invalid adb serial")
    started = time.time()
    try:
        # exec-out 直出二进制比 shell screencap → pull 更快、更稳
        proc = subprocess.run(
            ["adb", "-s", adb_serial, "exec-out", "screencap", "-p"],
            capture_output=True,
            timeout=timeout_sec,
        )
    except FileNotFoundError:
        return CapturedScreen(ok=False, source="adb", error="adb binary not in PATH")
    except subprocess.TimeoutExpired:
        return CapturedScreen(ok=False, source="adb", error=f"adb screencap timeout {timeout_sec}s")
    except Exception as e:
        return CapturedScreen(ok=False, source="adb", error=f"adb screencap failed: {e}")

    elapsed_ms = int((time.time() - started) * 1000)
    if proc.returncode != 0 or not proc.stdout:
        return CapturedScreen(
            ok=False,
            source="adb",
            error=f"adb screencap rc={proc.returncode} stderr={(proc.stderr or b'')[:200]!r}",
            elapsed_ms=elapsed_ms,
        )

    png_bytes = proc.stdout
    # 落盘到 tmp 便于排查。ARCHITECTURE.md §7：原图用后即删，不是持久状态。
    fd, path = tempfile.mkstemp(prefix=f"screen_{adb_serial}_", suffix=".png")
    try:
        os.write(fd, png_bytes)
    finally:
        os.close(fd)

    width, height = peek_png_size(png_bytes)
    remember_capture_size(adb_serial, width, height)
    return CapturedScreen(
        ok=True,
        source="adb",
        path=path,
        image_base64=base64.b64encode(png_bytes).decode("ascii"),
        image_mime="image/png",
        width=width,
        height=height,
        elapsed_ms=elapsed_ms,
    )


# ---------- 空白帧检测 ----------


def shot_is_blank(shot: Any, *, white_threshold: float = 244.0, std_threshold: float = 14.0) -> bool:
    """检测全白/全黑过渡帧。

    port(scout): 源自上游 `shared/screenshot/regression_capture.py::shot_is_blank`，
    判据逐条一致（只把入参从 PIL Image 放宽到"能被 np.asarray 吃下的东西"）。

    为什么必须有：真机息屏时 `adb exec-out screencap` **照样返回一张合法 PNG**，
    只是全黑。不检测就会把黑屏当有效帧回给 Nexus，VLM 拿着黑图做决策。
    （在 vivo V2528A 上实测过：息屏截图 mean≈0，亮屏后 mean≈161。）
    """
    if shot is None:
        return True
    try:
        import numpy as np

        arr = np.asarray(shot)
        if arr.size == 0:
            return True
        gray = arr.mean(axis=2) if arr.ndim == 3 else arr.astype(float)
        mean = float(gray.mean())
        std = float(gray.std())
        if mean >= white_threshold and std <= std_threshold:
            return True
        if mean <= 18.0 and std <= std_threshold:
            return True
        return False
    except Exception:
        return False


def png_is_blank(png_bytes: bytes) -> bool:
    """PNG 字节版。缺 numpy/pillow 时保守返回 False（不误判成空白）。"""
    try:
        from PIL import Image

        return shot_is_blank(Image.open(BytesIO(png_bytes)).convert("RGB"))
    except Exception:
        return False


# ---------- Playwright 通路 ----------


def compress_web_png(png_bytes: bytes, ratio: float) -> tuple[bytes, str, int, int]:
    """按压缩比缩小 Web 截图。返回 (bytes, mime, orig_w, orig_h)。

    port: 上游 `screen.py::_compress_web_png`，逻辑逐条对齐。

    **width/height 返回的始终是原图尺寸** —— 坐标体系按真实视口，不受压缩影响。
    这一点写进了协议 §4.4，改动会破坏 Nexus 的千分比坐标换算。
    """
    orig_w, orig_h = peek_png_size(png_bytes)
    if ratio <= 1.0 or not png_bytes:
        return png_bytes, "image/png", orig_w, orig_h
    try:
        from PIL import Image

        img = Image.open(BytesIO(png_bytes)).convert("RGB")
        orig_w, orig_h = img.size
        preview_w = max(1, round(orig_w / ratio))
        preview_h = max(1, round(orig_h / ratio))
        if preview_w < orig_w or preview_h < orig_h:
            img = img.resize((preview_w, preview_h), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=82, optimize=True)
        return buf.getvalue(), "image/jpeg", orig_w, orig_h
    except Exception as exc:
        SLog.w(TAG, f"web screenshot compress failed: {exc}")
        return png_bytes, "image/png", orig_w, orig_h


def capture_via_playwright(
    sn: str,
    *,
    timeout_sec: float = 15.0,
    compress_ratio: float = 2.0,
) -> CapturedScreen:
    """port: 上游 `screen.py::_capture_via_playwright`。

    与上游唯一的差别：压缩比由参数传入（来自 `OBSERVE.compress_ratio`），
    不再回查 `system_settings_service` —— 那是 Nexus 的设置服务。
    """
    started = time.time()
    try:
        from mino_scout.playwright_hub import get_hub

        png_bytes = get_hub().screenshot_png(str(sn or ""), timeout_ms=int(timeout_sec * 1000))
    except Exception as e:
        return CapturedScreen(
            ok=False,
            source="playwright",
            error=f"playwright screenshot failed: {e}",
            elapsed_ms=int((time.time() - started) * 1000),
        )

    elapsed_ms = int((time.time() - started) * 1000)
    if not png_bytes:
        return CapturedScreen(
            ok=False, source="playwright", error="empty screenshot", elapsed_ms=elapsed_ms
        )

    out, mime, width, height = compress_web_png(png_bytes, compress_ratio)
    suffix = ".jpg" if mime == "image/jpeg" else ".png"
    fd, path = tempfile.mkstemp(prefix="screen_web_", suffix=suffix)
    try:
        os.write(fd, out)
    finally:
        os.close(fd)

    return CapturedScreen(
        ok=True,
        source="playwright",
        path=path,
        image_base64=base64.b64encode(out).decode("ascii"),
        image_mime=mime,
        width=width,
        height=height,
        elapsed_ms=elapsed_ms,
    )


# ---------- 对外入口 ----------


def capture(
    device: DeviceRef,
    *,
    prefer: tuple[str, ...] = ("adb", "remote"),
    timeout_sec: float = 15.0,
    compress_ratio: float = 2.0,
    allow_blank: bool = False,
) -> CapturedScreen:
    """按 prefer 顺序尝试各通道，返回第一张成功的帧。

    对应协议的 `OBSERVE {kind: screenshot, prefer: [...], compress_ratio: ...}`。
    实际用了哪个通道在返回值的 `source` 里，Scout 必须如实回报
    （Nexus 的 trace 依赖它）。
    """
    tried: list[str] = []
    errors: list[str] = []
    blank_shot: Optional[CapturedScreen] = None

    for channel in prefer or _IMPLEMENTED:
        if channel not in _IMPLEMENTED:
            SLog.w(TAG, f"通道 {channel} 尚未搬迁（见 screen.py 模块注释），跳过")
            errors.append(f"{channel}: 未实现")
            continue
        tried.append(channel)
        if channel == "playwright":
            shot = capture_via_playwright(
                device.sn, timeout_sec=timeout_sec, compress_ratio=compress_ratio
            )
        else:
            shot = capture_via_adb(device.adb_serial, timeout_sec=timeout_sec)
        if shot.has_image():
            # 息屏 / 过渡帧：screencap 会返回一张合法但全黑的 PNG。
            # 判空后换下一个通道；都空白就把最后一张连同 blank 标记回给 Nexus，
            # 由它决定唤醒还是重试 —— Scout 不自己做决策。
            if allow_blank or not png_is_blank(base64.b64decode(shot.image_base64)):
                SLog.i(
                    TAG,
                    f"capture ok via {shot.source} {shot.width}x{shot.height} "
                    f"mime={shot.image_mime} bytes={len(shot.image_base64)} {shot.elapsed_ms}ms",
                )
                return shot
            SLog.w(TAG, f"capture via {channel} 拿到空白帧（息屏或过渡帧），尝试下一个通道")
            blank_shot = shot
            errors.append(f"{channel}: 空白帧")
            continue
        errors.append(f"{channel}: {shot.error}")
        SLog.w(TAG, f"capture via {channel} 失败: {shot.error}")

    if blank_shot is not None:
        # 所有通道都只拿到空白帧：如实回给 Nexus 并标注，让它决定唤醒/重试
        blank_shot.remote_detail = {**dict(blank_shot.remote_detail or {}), "blank": True}
        blank_shot.error = "所有通道都只拿到空白帧（设备可能息屏）"
        SLog.w(TAG, blank_shot.error)
        return blank_shot

    return CapturedScreen(
        ok=False,
        source=",".join(tried) or "none",
        error="；".join(errors) or f"prefer={list(prefer)} 里没有已实现的通道",
    )


def cleanup(shot: Optional[CapturedScreen]) -> None:
    """删掉落盘的原图。ARCHITECTURE.md §7：Scout 不留持久状态。"""
    if shot is None or not shot.path:
        return
    try:
        os.remove(shot.path)
    except OSError:
        pass
