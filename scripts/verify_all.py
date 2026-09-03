#!/usr/bin/env python3
"""跑全部守门脚本。CI 入口。"""
import subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = sorted(p.name for p in HERE.glob("verify_*.py") if p.name != "verify_all.py")

failed = []
for s in SCRIPTS:
    print(f"\n=== {s} ===")
    if subprocess.call([sys.executable, str(HERE / s)], cwd=str(HERE)) != 0:
        failed.append(s)

print("\n" + "=" * 50)
if failed:
    print(f"FAILED: {', '.join(failed)}")
    sys.exit(1)
print(f"ALL OK ({len(SCRIPTS)} checks)")
