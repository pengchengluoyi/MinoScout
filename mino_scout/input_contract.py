"""本进程实际支持的输入组合。只报已经实现的，不凭包版本猜测。"""
from __future__ import annotations

from typing import Any


def runtime_input_capabilities() -> list[dict[str, Any]]:
    import platform
    from mino_scout.platform_contract import host_os_from_system
    system = host_os_from_system(platform.system())
    rows = [
        {
            "id": "input_text", "executor": "desktop", "platform": system, "contract_version": 2,
            "supported_combinations": [{"target_mode": "current_focus", "write_modes": ["replace", "append"]}],
        },
        {
            "id": "input_text",
            "executor": "playwright",
            "platform": "web",
            "contract_version": 2,
            "supported_combinations": [
                {"target_mode": "current_focus", "write_modes": ["replace", "append"]},
                {"target_mode": "coordinate", "write_modes": ["replace"]},
                {"target_mode": "node", "write_modes": ["replace"]},
            ],
        },
        {
            "id": "input_text",
            "executor": "adb",
            "platform": "android",
            "contract_version": 2,
            "supported_combinations": [
                {"target_mode": "current_focus", "write_modes": ["append"]},
                {"target_mode": "coordinate", "write_modes": ["replace"]},
            ],
        },
    ]

    return [row for row in rows if row["executor"] != "desktop" or system in {"mac", "win"}]
