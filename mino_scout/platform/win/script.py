"""Windows 用例脚本里的定位。坐标写在该渠道的 locator_spec。"""
from __future__ import annotations


def locator_params(selector: dict) -> dict:
    if "x" in selector and "y" in selector:
        return {"x": selector["x"], "y": selector["y"], "point_policy": "coordinate"}
    kind = str(selector.get("kind") or "")
    value = str(selector.get("value") or selector.get("name") or selector.get("label") or "")
    if kind in {"text", "name", "label", "title"} and value:
        return {"point_policy": "node", "target": {"text": value, "name": str(selector.get("name") or value)}}
    raise ValueError("locator_missing")
