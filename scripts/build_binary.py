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

1. **Playwright 的浏览器要单独装进 onedir。** `collect-all playwright` 只带 Python 包和
   driver/`node`，不含 Chromium。冻结后执行 `playwright install chromium` 写到
   `ms-playwright/`，运行时 `PLAYWRIGHT_BROWSERS_PATH` 指向那里。
2. **动态 import 的模块要显式声明。** Scout 里大量 `def f(): from x import y` 的懒加载
   （executors / engines / probe），PyInstaller 静态分析看不到，必须进 hiddenimports。
   这里用 `find_local_modules()` 把 `mino_scout` 下所有模块全塞进去 —— 上游同款做法。
3. **adbutils 带二进制资源**（adb.exe 等），需要 collect_all 才能带上。
4. **zip 必须用 Unix 文件模式。** PyInstaller 拷出来的 `node` 经常是 0644；打包脚本按
   Mach-O/ELF/PE 魔数写成 0755，否则 macOS 解压后 Playwright 会 Permission denied。
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
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "if getattr(sys, 'frozen', False):\n"
        "    _browsers = Path(sys.executable).resolve().parent / 'ms-playwright'\n"
        "    if _browsers.is_dir():\n"
        "        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(_browsers))\n"
        "for _stream in (sys.stdout, sys.stderr):\n"
        "    _reconfigure = getattr(_stream, 'reconfigure', None)\n"
        "    if not _reconfigure:\n"
        "        continue\n"
        "    try:\n"
        "        _reconfigure(encoding='utf-8', errors='replace')\n"
        "    except Exception:\n"
        "        pass\n"
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
    install_playwright_chromium(out)
    chmod_frozen_payload(out)
    return out


def browsers_dir(dist_dir: Path) -> Path:
    return dist_dir / "ms-playwright"


def install_playwright_chromium(dist_dir: Path) -> None:
    dest = browsers_dir(dist_dir)
    dest.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(dest)
    print(f"→ playwright install chromium → {dest}")
    proc = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        env=env,
        cwd=str(ROOT),
        timeout=15 * 60,
    )
    if proc.returncode != 0:
        raise SystemExit(f"playwright install chromium 失败，rc={proc.returncode}")


def chmod_frozen_payload(dist_dir: Path) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    from pack_release import chmod_payload  # noqa: E402

    chmod_payload(dist_dir)


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
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    browsers = browsers_dir(dist_dir)
    if browsers.is_dir():
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
    proc = subprocess.run(
        [str(exe), "probe"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=180,
        cwd=str(dist_dir),
        stdin=subprocess.DEVNULL,
    )
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    print(f"rc={proc.returncode}")
    print("--- stdout ---")
    print(stdout[-2000:] or "(empty)")
    print("--- stderr ---")
    print(stderr[-2000:] or "(empty)")
    # 1 = 没有可用 executor，也算跑通了（CI 上本来就没有设备）
    if proc.returncode not in (0, 1):
        print(f"\n自检失败 rc={proc.returncode}")
        return 1
    if '"node_id"' not in stdout and '"node_id"' not in stderr:
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
