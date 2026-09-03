#!/usr/bin/env python3
"""跑本仓全部测试。CI 入口之一（另一个是 scripts/verify_all.py）。"""
import subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTS = sorted(p.name for p in HERE.glob("test_*.py"))
failed = []
for t in TESTS:
    print(f"\n{'='*60}\n{t}\n{'='*60}")
    if subprocess.call([sys.executable, str(HERE / t)]) != 0:
        failed.append(t)
print("\n" + "=" * 60)
if failed:
    print(f"FAILED: {', '.join(failed)}"); sys.exit(1)
print(f"ALL TESTS OK ({len(TESTS)} files)")
