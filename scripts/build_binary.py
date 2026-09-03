#!/usr/bin/env python3
"""把 Scout 冻结成不依赖 Python 的可执行文件。

**为什么必须有这一步**：执行节点跑在别人的机器上，不能假设那台机器装了 Python 3.12。
现有的 `install.sh` 第一件事就是检查 `sys.version_info >= (3, 12)`，不满足直接退出 ——
对机房专机勉强可行，对任何非开发机器都是死路。

用法：
    python scripts/build_binary.py                 # 冻结到 build/dist/mino-scout/
    python scripts/build_binary.py --check         # 冻结后跑一次 probe 自检
    python scripts/build_binary.py --clean         # 先清干净

产物是 **onedir**（一个目录，不是单文件）：
  - 单文件启动要每次解压到临时目录，Scout 是常驻进程，没必要付这个代价
  - onedir 便于 Studio 做增量替换与回滚

## 已知的坑（都来自上游 MiniOrangeServer/build.py 的经验）

1. **Playwright 的浏览器不能打进去。** playwright 这个 Python 包会被打包，但它下载的
   Chromium（约 298 MB）在 `~/Library/Caches/ms-playwright`，是运行时资源。
   冻结产物里不含它 —— web 通道要么首次运行时 `playwright install chromium`，
   要么在没有它时如实上报 `available: false`（Scout 已经这么做了）。
2. **动态 import 的模块要显式声明。** Scout 里大量 `def f(): from x import y` 的懒加载
   （executors / engines / probe），PyInstaller 静态分析看不到，必须进 hiddenimports。
   这里用 `find_local_modules()` 把 `mino_scout` 下所有模块全塞进去 —— 上游同款做法。
3. **adbutils 带二进制资源**（adb.exe 等），需要 collect_all 才能带上。
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
DIST = BUILD / "dist"
WORK = BUILD / "work"
NAME = "mino-scout"


def _configure_utf8_stdio() -> None:
    """Windows CI 默认 cp1252，print 中文 / → 会 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not reconfigure:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_configure_utf8_stdio()

# 需要连数据文件一起收集的包（光靠静态分析带不全）
COLLECT_ALL = ["adbutils", "uiautomator2", "playwright", "zeroconf"]

# 静态分析看不到、但运行时会 import 的
EXTRA_HIDDEN = [
    "websockets", "websockets.client", "websockets.legacy",
    "PIL", "PIL.Image",
    "numpy",
    "pydantic", "pydantic.deprecated.decorator",
    "psutil",
    "wda",              # facebook-wda，包名和 pip 名不一样
    "encodings.idna",
]

# 明确排除：Scout 用不到，但会被依赖链拖进来的大件
EXCLUDES = [
    "torch", "torchvision", "transformers", "open_clip",
    "paddle", "paddleocr", "cv2", "onnxruntime", "ultralytics",
    "matplotlib", "scipy", "pandas", "IPython", "tkinter",
    "sqlalchemy",       # 硬约束 1：Scout 不碰数据库
    "fastapi", "uvicorn",
    # 录屏相关：uiautomator2/screenrecord.py 会 import 它们，但 Scout 对
    # av / imageio / ffmpeg / screenrecord **零引用**（grep 验证过），
    # 而且它们不是 uiautomator2 的声明依赖，只是碰巧装在开发 venv 里。
    # 实测这两个占 100 MB —— 冻结产物的 26%。
    "av", "imageio", "imageio_ffmpeg",
    "uiautomator2.screenrecord",
]


def find_local_modules(pkg: str) -> list[str]:
    """把包内所有模块列成 hiddenimports —— 懒加载的那些静态分析看不到。"""
    out: list[str] = []
    base = ROOT / pkg
    for path in base.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(ROOT).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            out.append(".".join(parts))
    return sorted(set(out))


def build(*, clean: bool) -> Path:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise SystemExit(
            "需要 PyInstaller：pip install pyinstaller\n"
            "（它只在打包时用，不进 pyproject 的运行依赖）"
        )

    if clean and BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True, exist_ok=True)

    entry = BUILD / "_entry.py"
    entry.write_text(
        "# PyInstaller 入口。真正的实现在 mino_scout.cli:main。\n"
        "import sys\n"
        "from mino_scout.cli import main\n"
        "sys.exit(main())\n",
        encoding="utf-8",
    )

    hidden = find_local_modules("mino_scout") + EXTRA_HIDDEN
    argv = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", NAME,
        "--distpath", str(DIST),
        "--workpath", str(WORK),
        "--specpath", str(BUILD),
        "--console",                 # 常驻服务，不要 windowed
        "--paths", str(ROOT),
    ]
    for mod in hidden:
        argv += ["--hidden-import", mod]
    for pkg in COLLECT_ALL:
        argv += ["--collect-all", pkg]
    for pkg in EXCLUDES:
        argv += ["--exclude-module", pkg]
    argv.append(str(entry))

    print(f"→ 冻结 {NAME}（{len(hidden)} 个 hidden-import，{len(COLLECT_ALL)} 个 collect-all）")
    proc = subprocess.run(argv, cwd=str(ROOT))
    if proc.returncode != 0:
        raise SystemExit(f"PyInstaller 失败，rc={proc.returncode}")

    out = DIST / NAME
    if not out.is_dir():
        raise SystemExit(f"产物目录没生成：{out}")
    return out


def binary_path(dist_dir: Path) -> Path:
    exe = dist_dir / (f"{NAME}.exe" if platform.system() == "Windows" else NAME)
    if not exe.is_file():
        raise SystemExit(f"找不到可执行文件：{exe}")
    return exe


def dir_size_mb(path: Path) -> int:
    total = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    return round(total / 1024 / 1024)


def check(dist_dir: Path) -> int:
    """跑一次 probe，确认冻结产物真的能用。

    **这一步不能省。** PyInstaller 最常见的失败是"打包成功但一跑就 ModuleNotFoundError"
    —— 懒加载模块没进 hiddenimports。只有真跑一次才看得出来。
    """
    exe = binary_path(dist_dir)
    print(f"\n→ 自检：{exe} probe")
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)          # 确保没有偷偷用到源码树
    proc = subprocess.run(
        [str(exe), "probe"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=180,
    )
    tail = (proc.stdout or "")[-800:]
    print(tail)
    if proc.returncode not in (0, 1):     # 1 = 没有可用 executor，也算跑通了
        print("--- stderr ---")
        print((proc.stderr or "")[-1500:])
        print(f"\n自检失败 rc={proc.returncode}")
        return 1
    if '"node_id"' not in (proc.stdout or ""):
        print("自检失败：probe 没有输出 manifest")
        return 1
    print(f"自检通过。产物大小：{dir_size_mb(dist_dir)} MB")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="冻结 Scout 为免 Python 的可执行文件")
    ap.add_argument("--check", action="store_true", help="冻结后跑一次 probe 自检")
    ap.add_argument("--clean", action="store_true", help="先清掉 build/")
    args = ap.parse_args()

    dist_dir = build(clean=args.clean)
    print(f"\n产物：{dist_dir}（{dir_size_mb(dist_dir)} MB）")
    print(f"可执行：{binary_path(dist_dir)}")
    if args.check:
        return check(dist_dir)
    print("\n提示：加 --check 会真跑一次 probe —— PyInstaller 最常见的失败是"
          "『打包成功但一跑就 ModuleNotFoundError』，只有真跑才看得出来。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
