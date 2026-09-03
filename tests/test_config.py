"""config.json 路径、HTTP→WS 推导、CLI 覆盖。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mino_scout.config import (  # noqa: E402
    DEFAULT_NEXUS_WS,
    load_config,
    nexus_ws_url,
    resolve_runtime,
)

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{' — ' + detail if detail else ''}")
        failures.append(name)


print("== nexus_ws_url ==")
check("empty", nexus_ws_url("") == "")
check("http origin", nexus_ws_url("http://mino.local:10104") == "ws://mino.local:10104/node")
check("https origin", nexus_ws_url("https://nexus.example.com") == "wss://nexus.example.com/node")
check("trailing slash", nexus_ws_url("http://mino.local:10104/") == "ws://mino.local:10104/node")
check("already ws /node", nexus_ws_url("ws://mino.local:10104/node") == "ws://mino.local:10104/node")
check("ws without path", nexus_ws_url("ws://mino.local:10104") == "ws://mino.local:10104/node")
check("bare host", nexus_ws_url("mino.local:10104") == "ws://mino.local:10104/node")

print("== load_config / resolve_runtime ==")
check("missing file", load_config(Path("/no/such/mino-scout-config.json")) == {})

cfg = {"nexus_url": "https://nexus.example.com", "token": "from-file"}
ws, tok = resolve_runtime(config=cfg)
check("from file ws", ws == "wss://nexus.example.com/node")
check("from file token", tok == "from-file")

ws, tok = resolve_runtime(nexus="ws://override/node", token="cli", config=cfg)
check("flag ws wins", ws == "ws://override/node")
check("flag token wins", tok == "cli")

ws, tok = resolve_runtime(config={})
check("default ws", ws == DEFAULT_NEXUS_WS)
check("empty token", tok == "")

print("== load_config tempfile ==")
tmp = Path(__file__).resolve().parent / "_tmp_scout_config.json"
tmp.write_text(json.dumps({"nexus_url": "http://mino.local:10104", "token": "t1"}) + "\n", encoding="utf-8")
try:
    loaded = load_config(tmp)
    check("read nexus_url", loaded.get("nexus_url") == "http://mino.local:10104")
    check("read token", loaded.get("token") == "t1")
finally:
    tmp.unlink(missing_ok=True)

print()
if failures:
    print(f"FAILED {len(failures)}: {', '.join(failures)}")
    sys.exit(1)
print("ALL OK — config")
