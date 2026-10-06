"""插件非密钥参数写在节点目录。心跳只报安装与是否已配置。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mino_scout.config import config_dir
from mino_scout.plugins.secret_store import account_name, delete_secret, get_secret, put_secret

# 本轮四类里已经定下的条目。MCP 还没有具体项。
CATALOG: list[dict[str, Any]] = [
    {
        "class": "cli",
        "id": "feishu",
        "needs_install": True,
        "fields": [
            {"key": "app_id", "secret": False},
            {"key": "app_secret", "secret": True},
        ],
    },
    {
        "class": "cli",
        "id": "meego",
        "needs_install": True,
        "fields": [
            {"key": "base_url", "secret": False, "optional": True},
            {"key": "plugin_id", "secret": False},
            {"key": "user_key", "secret": False, "optional": True},
            {"key": "plugin_secret", "secret": True},
        ],
    },
    {
        "class": "bot",
        "id": "wechat",
        "needs_install": False,
        "fields": [
            {"key": "bot_token", "secret": True},
        ],
    },
    {
        "class": "bot",
        "id": "feishu_bot",
        "needs_install": False,
        "fields": [
            {"key": "webhook_url", "secret": True},
        ],
    },
    {
        "class": "bot",
        "id": "wecom",
        "needs_install": False,
        "fields": [
            {"key": "webhook_url", "secret": True},
        ],
    },
    {
        "class": "mail",
        "id": "gmail",
        "needs_install": False,
        "fields": [
            {"key": "inbox_address", "secret": False},
            {"key": "app_password", "secret": True},
        ],
    },
]


def _key(kind: str, plugin_id: str) -> str:
    return f"{kind}:{plugin_id}"


def state_path() -> Path:
    return config_dir() / "plugins" / "state.json"


def plugin_dir(kind: str, plugin_id: str) -> Path:
    return config_dir() / "plugins" / kind / plugin_id


def spec(kind: str, plugin_id: str) -> dict[str, Any] | None:
    for row in CATALOG:
        if row["class"] == kind and row["id"] == plugin_id:
            return row
    return None


def load_state() -> dict[str, Any]:
    path = state_path()
    if not path.is_file():
        return {"items": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"items": {}}
    if not isinstance(raw, dict):
        return {"items": {}}
    items = raw.get("items")
    if not isinstance(items, dict):
        raw["items"] = {}
    return raw


def save_state(data: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def _item(kind: str, plugin_id: str) -> dict[str, Any]:
    root = load_state()
    items = root.get("items") if isinstance(root.get("items"), dict) else {}
    row = items.get(_key(kind, plugin_id))
    return dict(row) if isinstance(row, dict) else {}


def plain_value(kind: str, plugin_id: str, field: str) -> str:
    return str(_item(kind, plugin_id).get(field) or "").strip()


def secret_value(node_id: str, kind: str, plugin_id: str, field: str) -> str:
    return get_secret(account_name(node_id, kind, plugin_id, field))


def installed(kind: str, plugin_id: str) -> bool:
    row = spec(kind, plugin_id)
    if row is None:
        return False
    if not row.get("needs_install"):
        return True
    marker = plugin_dir(kind, plugin_id) / ".installed"
    return marker.is_file()


def configured(node_id: str, kind: str, plugin_id: str) -> bool:
    """心跳会频繁调用。密钥是否在，只看本机清单里的标记，不去读保险库。"""
    del node_id
    row = spec(kind, plugin_id)
    if row is None or not row.get("fields"):
        return False
    item = _item(kind, plugin_id)
    for field in row["fields"]:
        key = str(field.get("key") or "")
        if field.get("optional"):
            continue
        if field.get("secret"):
            if not item.get(f"has_{key}"):
                return False
        elif not str(item.get(key) or "").strip():
            return False
    return True


def status_list(node_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in CATALOG:
        kind = str(row["class"])
        pid = str(row["id"])
        item = _item(kind, pid)
        values: dict[str, str] = {}
        saved_secrets: list[str] = []
        for field in row.get("fields") or []:
            key = str(field.get("key") or "")
            if not key:
                continue
            if field.get("secret"):
                if item.get(f"has_{key}"):
                    saved_secrets.append(key)
            else:
                text = str(item.get(key) or "").strip()
                if text:
                    values[key] = text
        out.append({
            "class": kind,
            "id": pid,
            "installed": installed(kind, pid),
            "configured": configured(node_id, kind, pid),
            "values": values,
            "saved_secrets": saved_secrets,
        })
    return out


def apply_config(
    node_id: str,
    kind: str,
    plugin_id: str,
    values: dict[str, Any],
    clear: list[str],
) -> None:
    row = spec(kind, plugin_id)
    if row is None:
        raise ValueError(f"未知插件 {kind}/{plugin_id}")
    allowed = {str(field.get("key") or ""): bool(field.get("secret")) for field in row["fields"]}
    incoming = values if isinstance(values, dict) else {}
    unknown = [k for k in incoming if k not in allowed]
    if unknown:
        raise ValueError(f"不能写这些字段: {', '.join(unknown)}")
    for key in clear or []:
        if key not in allowed:
            raise ValueError(f"不能清除字段 {key}")
    root = load_state()
    items = root.setdefault("items", {})
    current = dict(items.get(_key(kind, plugin_id)) or {})
    for key, secret in allowed.items():
        if key in (clear or []):
            if secret:
                delete_secret(account_name(node_id, kind, plugin_id, key))
                current.pop(f"has_{key}", None)
            else:
                current.pop(key, None)
            continue
        if key not in incoming:
            continue
        text = str(incoming.get(key) or "").strip()
        if secret:
            if text:
                put_secret(account_name(node_id, kind, plugin_id, key), text)
                current[f"has_{key}"] = True
            continue
        if text:
            current[key] = text[:500]
        else:
            current.pop(key, None)
    items[_key(kind, plugin_id)] = current
    save_state(root)


def remember_secret(node_id: str, kind: str, plugin_id: str, field: str, secret: str) -> None:
    """扫码登录这类不走表单的密钥。只在清单里记「有没有」，明文进保险库。"""
    text = str(secret or "")
    if text:
        put_secret(account_name(node_id, kind, plugin_id, field), text)
    else:
        delete_secret(account_name(node_id, kind, plugin_id, field))
    root = load_state()
    items = root.setdefault("items", {})
    current = dict(items.get(_key(kind, plugin_id)) or {})
    if text:
        current[f"has_{field}"] = True
    else:
        current.pop(f"has_{field}", None)
    items[_key(kind, plugin_id)] = current
    save_state(root)


def remember_plain(kind: str, plugin_id: str, values: dict[str, Any]) -> None:
    root = load_state()
    items = root.setdefault("items", {})
    current = dict(items.get(_key(kind, plugin_id)) or {})
    for key, value in values.items():
        text = str(value or "").strip()
        if text:
            current[str(key)] = text[:500]
        else:
            current.pop(str(key), None)
    items[_key(kind, plugin_id)] = current
    save_state(root)


def clear_plugin_secrets(node_id: str, kind: str, plugin_id: str) -> None:
    row = spec(kind, plugin_id) or {}
    for field in row.get("fields") or []:
        if field.get("secret"):
            delete_secret(account_name(node_id, kind, plugin_id, str(field.get("key") or "")))
    root = load_state()
    items = root.setdefault("items", {})
    items.pop(_key(kind, plugin_id), None)
    save_state(root)
