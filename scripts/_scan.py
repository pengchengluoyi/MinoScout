#!/usr/bin/env python3
"""共用的静态禁止项扫描器。

各 verify_no_*.py 只声明「禁止什么、为什么」，扫描逻辑在这里。
纯静态：不 import 被检查的代码，不需要装环境，用裸 python3 即可跑。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", "scripts", "tests", "docs"}


def iter_py() -> list[Path]:
    out = []
    for p in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
            continue
        out.append(p)
    return sorted(out)


def scan(name: str, why: str, patterns: dict[str, str], *, allow: tuple[str, ...] = ()) -> int:
    """patterns: {正则: 人类可读的违规说明}。allow: 豁免的相对路径前缀。

    只扫**会执行的代码行**：
      - `#` 注释行跳过
      - 文档字符串（三引号块）内跳过 —— 解释「为什么我们不用 X」的散文不该触发
        对 X 的禁令。这类说明恰恰是搬迁时最该写清的东西。
      - 带 `# allow-constraint: <理由>` 的行跳过（真代码的逃生口，必须写理由）
    """
    hits: list[str] = []
    compiled = [(re.compile(p), msg) for p, msg in patterns.items()]
    scanned = iter_py()
    for path in scanned:
        rel = str(path.relative_to(ROOT))
        if rel.startswith(allow):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue

        in_doc = False
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # 先按当前状态判断这一行算不算代码
            is_code = not in_doc and not stripped.startswith("#")
            # 再按本行的三引号数量翻转状态（奇数个才翻转）
            if (line.count('"""') + line.count("'''")) % 2 == 1:
                in_doc = not in_doc
                is_code = False  # 三引号开头/结尾这一行本身也不算代码
            if not is_code:
                continue
            if "allow-constraint:" in line:
                continue
            for rx, msg in compiled:
                if rx.search(line):
                    hits.append(f"{rel}:{i}: {msg}\n      {stripped[:120]}")
                    break

    if hits:
        print(f"FAIL {name}")
        print(f"  约束：{why}")
        for h in hits:
            print("  " + h)
        print(f"\n  共 {len(hits)} 处违规。见 CLAUDE.md §1。")
        return 1
    print(f"OK {name} — 扫了 {len(scanned)} 个文件的代码行")
    return 0


if __name__ == "__main__":  # pragma: no cover
    print("这是共用模块，请跑具体的 verify_no_*.py 或 verify_all.py")
    sys.exit(2)
