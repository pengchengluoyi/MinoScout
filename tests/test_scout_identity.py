"""scout_id 持久化、字母数字约束、web 槽按节点命名。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mino-scout-id-")
os.environ["MINO_SCOUT_HOME"] = _TMP

from mino_scout.cli import build_core, cmd_probe  # noqa: E402
from mino_scout.config import (  # noqa: E402
    SCOUT_ID_LEN,
    config_path,
    generate_scout_id,
    load_config,
    resolve_scout_id,
    resolve_studio_id,
    sanitize_scout_id,
    save_config,
)
from mino_scout.core import ScoutCore  # noqa: E402
import mino_scout.playwright_hub as playwright_hub  # noqa: E402
from mino_scout.playwright_hub import is_web_slot, web_slot_sn  # noqa: E402

playwright_hub.probe_playwright = lambda: (playwright_hub.PROBE_OK_STATE, {})
from mino_scout.service import collect_status  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{' — ' + detail if detail else ''}")
        failures.append(name)


print("== sanitize / generate ==")
check("strip hyphens", sanitize_scout_id("Host-Name.01") == "hostname01")
check("strip underscore", sanitize_scout_id("ab_cd") == "abcd")
check("empty junk", sanitize_scout_id("---") == "")
sid = generate_scout_id()
check("generated len", len(sid) == SCOUT_ID_LEN, sid)
check("generated hex", sid.isalnum() and sid.islower() and all(c in "0123456789abcdef" for c in sid), sid)
check("no special chars", "-" not in sid and "_" not in sid and "." not in sid, sid)
check("stable generate", generate_scout_id() == sid)

print("== persist studio_id ==")
save_config({**load_config(), "studio_id": "A1B2-C3D4.E5F67890"})
check("resolve_studio_id sanitizes", resolve_studio_id() == "a1b2c3d4e5f67890", resolve_studio_id())

print("== persist scout_id ==")
got = resolve_scout_id()
check("resolve alphanumeric", got.isalnum() and got.islower(), got)
cfg = load_config()
check("written to config.json", cfg.get("scout_id") == got, str(cfg))
check("same on second resolve", resolve_scout_id() == got)
save_config({**cfg, "nexus_url": "http://mino.local:10104", "token": "t"})
again = resolve_scout_id()
check("keep id when other keys written", again == got)
check("keep nexus_url", load_config().get("nexus_url") == "http://mino.local:10104")

print("== CLI override sanitizes ==")
over = resolve_scout_id(override="Mac-Studio.01")
check("override sanitized", over == "macstudio01", over)
check("override does not clobber file", load_config().get("scout_id") == got)

print("== ScoutCore + web slot ==")
core = ScoutCore({}, node_id="3f8a1c0e9b2d4f71")
check("core.scout_id == node_id", core.scout_id == core.node_id == "3f8a1c0e9b2d4f71")
check("web sn", web_slot_sn(core.scout_id) == "web3f8a1c0e9b2d4f71")


class FakePw:
    id = "playwright"
    provides = ["ui_screenshot"]

    def probe(self):
        return True, ""

    def supports(self, capability_id, low_level=None):
        return False

    def execute(self, event, ctx):
        raise AssertionError("not used")


core_pw = ScoutCore({"playwright": FakePw()}, node_id="3f8a1c0e9b2d4f71")
_, devices = core_pw.manifest()
web = [d for d in devices if d.platform == "web"]
check("manifest has web slot", len(web) == 1, str(devices))
check("manifest sn == web+scout_id", web[0].sn == "web3f8a1c0e9b2d4f71", web[0].sn if web else "")
check("is_web_slot new", is_web_slot("web3f8a1c0e9b2d4f71") is True)
check("is_web_slot legacy", is_web_slot("web-local") is True)
check("phone is not web", is_web_slot("R5CT30xxxx") is False)

print("== probe / status print scout_id ==")
import io
from contextlib import redirect_stdout

buf = io.StringIO()
with redirect_stdout(buf):
    rc = cmd_probe(build_core("aabbccddeeff0011"))
probe_out = json.loads(buf.getvalue())
check("probe rc", rc in (0, 1))
check("probe scout_id", probe_out.get("scout_id") == "aabbccddeeff0011", str(probe_out.get("scout_id")))
check("probe node_id same", probe_out.get("node_id") == "aabbccddeeff0011")
web_sns = [d["sn"] for d in probe_out.get("devices") or [] if d.get("platform") == "web"]
if web_sns:
    check("probe web sn", web_sns == ["webaabbccddeeff0011"], str(web_sns))
st = collect_status()
check("status scout_id", str(st.get("scout_id") or "").isalnum(), str(st.get("scout_id")))
check("config path under tmp", config_path().resolve().parent == Path(_TMP).resolve(), str(config_path()))

print()
if failures:
    print(f"FAILED {len(failures)}: {', '.join(failures)}")
    sys.exit(1)
print("ALL OK — scout identity")
