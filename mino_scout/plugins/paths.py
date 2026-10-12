"""账号插件存储。未归属的旧文件保留原位，新账号从独立空目录开始。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mino_scout.config import config_dir, load_config


def profile_id() -> str:
    cfg = load_config()
    if cfg.get("plugin_storage_version") != 2:
        return ""
    owner = str(cfg.get("plugin_owner_user_id") or "").strip()
    origin = str(cfg.get("plugin_owner_nexus") or "").strip()
    if not owner or not origin:
        raise PermissionError("插件存储缺少已确认的账号或服务归属")
    return hashlib.sha256(json.dumps([origin, owner], ensure_ascii=False).encode()).hexdigest()


def data_dir() -> Path:
    profile = profile_id()
    return config_dir() / "accounts" / profile if profile else config_dir()
