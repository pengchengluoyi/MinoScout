#!/usr/bin/env python3
"""守门：本仓 protocol.py 必须能 round-trip 全部 golden fixture，且 fixture 哈希与
docs/PROTOCOL.md §8 记录一致。

契约真源是 tests/fixtures/protocol/，两仓字节相同。哈希不一致 = 协议漂移。
用法：
    python scripts/verify_protocol_contract.py            # 校验
    python scripts/verify_protocol_contract.py --write    # 把当前哈希写进 docs/PROTOCOL.md
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures" / "protocol"
PROTOCOL_DOC = ROOT / "docs" / "PROTOCOL.md"
HASH_RE = re.compile(r"^fixtures_sha256\s*=\s*(\S+)\s*$", re.M)

# 本仓的 protocol 模块（Scout 与 Nexus 各自一份，模块名不同）
for candidate in ("mino_scout.protocol", "mino_nexus.protocol"):
    try:
        protocol = __import__(candidate, fromlist=["*"])
        break
    except ImportError:
        protocol = None
if protocol is None:  # pragma: no cover
    print("FAIL: 找不到 protocol 模块（mino_scout.protocol / mino_nexus.protocol）")
    sys.exit(1)


def fixtures_hash() -> str:
    h = hashlib.sha256()
    for path in sorted(FIXTURES.glob("*.json")):
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def main() -> int:
    files = sorted(FIXTURES.glob("*.json"))
    if not files:
        print(f"FAIL: {FIXTURES} 下没有 fixture")
        return 1

    errors: list[str] = []
    seen_types = set()

    for path in files:
        raw = json.loads(path.read_text(encoding="utf-8"))
        try:
            env = protocol.loads(raw)
        except Exception as exc:
            errors.append(f"{path.name}: loads 失败 -> {type(exc).__name__}: {exc}")
            continue
        seen_types.add(env.type.value)
        again = protocol.dumps(env)
        if again != raw:
            diff = [k for k in set(raw) | set(again) if raw.get(k) != again.get(k)]
            errors.append(f"{path.name}: round-trip 不一致，差异字段 {diff}")
            for k in diff:
                if k == "payload":
                    a, b = raw.get("payload") or {}, again.get("payload") or {}
                    for f in sorted(set(a) | set(b)):
                        if a.get(f) != b.get(f):
                            errors.append(f"    payload.{f}: fixture={a.get(f)!r} != code={b.get(f)!r}")

    # 每个 MsgType 至少要有一条 fixture
    missing = {t.value for t in protocol.MsgType} - seen_types
    if missing:
        errors.append(f"以下消息类型没有 fixture 覆盖: {sorted(missing)}")

    current = fixtures_hash()
    doc = PROTOCOL_DOC.read_text(encoding="utf-8") if PROTOCOL_DOC.exists() else ""
    match = HASH_RE.search(doc)

    if "--write" in sys.argv:
        if match:
            doc = HASH_RE.sub(f"fixtures_sha256 = {current}", doc)
        else:
            doc += f"\n```\nfixtures_sha256 = {current}\n```\n"
        PROTOCOL_DOC.write_text(doc, encoding="utf-8")
        print(f"written fixtures_sha256 = {current}")
    elif not match:
        errors.append("docs/PROTOCOL.md §8 没有 fixtures_sha256 记录（先跑 --write）")
    elif match.group(1) != current:
        errors.append(
            "fixture 哈希与 docs/PROTOCOL.md 不一致（协议漂移）：\n"
            f"    doc  = {match.group(1)}\n    real = {current}"
        )

    if errors:
        print("FAIL verify_protocol_contract")
        for e in errors:
            print("  " + e)
        return 1

    print(f"OK verify_protocol_contract — {len(files)} fixtures, {len(seen_types)} 种消息")
    return 0


if __name__ == "__main__":
    sys.exit(main())
