"""Stable capability snapshot identity; declarations only, no device or storage I/O."""
from __future__ import annotations
from dataclasses import asdict, is_dataclass
import hashlib
import json

def capability_revision(executors, runtime_capabilities):
    if not isinstance(executors, list) or not isinstance(runtime_capabilities, list):
        raise ValueError("capability_snapshot_incomplete")
    rows = [asdict(row) if is_dataclass(row) else dict(row) for row in executors]
    if any(not row.get("id") or type(row.get("available")) is not bool for row in rows):
        raise ValueError("executor_manifest_invalid")
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("executor_manifest_duplicate")
    for row in rows:
        row["provides"] = sorted(row.get("provides") or [])
    encode = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    body = {"contract": 1, "executors": sorted(rows, key=lambda row: row["id"]),
            "runtime_capabilities": sorted(runtime_capabilities, key=encode)}
    return "cap-" + hashlib.sha256(encode(body).encode("utf-8")).hexdigest()
