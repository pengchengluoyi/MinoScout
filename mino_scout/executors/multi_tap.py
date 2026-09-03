# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""连点：一次 dispatch 内短间隔点击，给调试面板 / 版本号彩蛋用。"""
from __future__ import annotations

from typing import Any, Optional, Tuple

Point = Tuple[int, int, int, int]


def parse_multi_tap(params: Optional[dict[str, Any]]) -> Tuple[Optional[Point], str]:
    """返回 (x, y, count, interval_ms) 或错误。坐标已是像素。"""
    p = params if isinstance(params, dict) else {}
    try:
        x = int(p.get("x"))
        y = int(p.get("y"))
    except (TypeError, ValueError):
        return None, "multi_tap 缺坐标 x/y"
    try:
        count = int(p.get("count") if p.get("count") is not None else 6)
    except (TypeError, ValueError):
        count = 6
    try:
        interval = int(p.get("interval_ms") if p.get("interval_ms") is not None else 80)
    except (TypeError, ValueError):
        interval = 80
    count = max(2, min(12, count))
    interval = max(40, min(400, interval))
    return (x, y, count, interval), ""
