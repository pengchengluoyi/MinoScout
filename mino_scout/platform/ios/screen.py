"""iOS 截图。"""
from __future__ import annotations

import base64
import time

from mino_scout.platform.ios.wda_session import WdaError, client_for
from mino_scout.schemas import CapturedScreen
from mino_scout.screen import peek_png_size


def capture(device, **_ignored) -> CapturedScreen:
    started = time.time()
    try:
        image = client_for(device.udid or device.sn).screenshot()
        raw = base64.b64decode(image)
        width, height = peek_png_size(raw)
        return CapturedScreen(
            ok=True, source="ios_wda", image_base64=image, image_mime="image/png",
            width=width, height=height, elapsed_ms=int((time.time() - started) * 1000),
        )
    except (WdaError, ValueError) as exc:
        return CapturedScreen(ok=False, source="ios_wda", error=str(exc))
