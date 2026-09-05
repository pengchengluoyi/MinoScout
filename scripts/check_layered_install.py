#!/usr/bin/env python3
"""端到端验证分层安装：全量装一次，再只装 app 层，确认浏览器一个字节没动。

这是分层方案的核心断言。手工验过一次不算数 —— 回归会悄悄发生：
只要有人在 install.sh 里恢复一句 `rm -rf "$DEST"`，包体收益就全没了，
而表面上一切正常（装完能跑），只有下载量会翻回 439 MB。

用法（先跑 build_binary.py 和 pack_release.py 生成 dist/）：

    python scripts/check_layered_install.py --dist dist
    python scripts/check_layered_install.py --dist dist --probe   # 顺便真跑一次

**只测当前平台。** 装到临时目录，并且带 MINO_SCOUT_SKIP_SERVICE=1 ——
不会在跑测试的机器上注册守护进程。
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import layers as L  # noqa: E402

IS_WINDOWS = platform.system() == "Windows"
EXE = "mino-scout.exe" if IS_WINDOWS else "mino-scout"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    print(f"  {mark} {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(label)


def extract(zip_path: Path, dest: Path) -> Path:
    """解压并**保留 Unix 权限位**。

    zipfile.extractall 会把权限丢光，而 pack_release 是刻意把 node / chromium / adb
    写成 0755 的 —— 用默认解压测，等于没测到那段逻辑。
    """
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            out = zf.extract(info, dest)
            mode = info.external_attr >> 16
            if mode and not IS_WINDOWS:
                os.chmod(out, mode & 0o7777)
    tops = [p for p in dest.iterdir() if p.is_dir()]
    return tops[0] if len(tops) == 1 else dest


def run_installer(root: Path, home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["MINO_SCOUT_HOME"] = str(home)
    env["MINO_SCOUT_SKIP_SERVICE"] = "1"
    if IS_WINDOWS:
        argv = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(root / "install.ps1")]
    else:
        argv = ["/bin/bash", str(root / "install.sh")]
    return subprocess.run(argv, cwd=str(root), env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=20 * 60)


def read_installed(home: Path) -> dict[str, str]:
    f = home / "bin" / "layers.txt"
    out: dict[str, str] = {}
    if not f.is_file():
        return out
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0]] = parts[1]
    return out


def tree_signature(root: Path) -> tuple:
    """(文件数, 总字节, 最新 mtime)。用来断言"这棵树完全没被碰过"。"""
    n = total = 0
    newest = 0.0
    for p in root.rglob("*"):
        if p.is_file():
            st = p.stat()
            n += 1
            total += st.st_size
            newest = max(newest, st.st_mtime)
    return (n, total, newest)


def find_zip(dist: Path, kind: str) -> Path | None:
    osn = {"Darwin": "darwin", "Windows": "win32", "Linux": "linux"}[platform.system()]
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    for p in sorted(dist.glob("MinoScout-*.zip")):
        stem = p.stem
        if not stem.endswith(f"-{osn}-{arch}"):
            continue
        middle = stem[len("MinoScout-"):-len(f"-{osn}-{arch}")]
        is_layer = any(middle.startswith(f"{layer}-") for layer in L.LAYERS)
        if kind == "combined" and not is_layer:
            return p
        if kind != "combined" and middle.startswith(f"{kind}-"):
            return p
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="端到端验证分层安装（仅当前平台）")
    ap.add_argument("--dist", default="dist")
    ap.add_argument("--probe", action="store_true", help="装完后真跑一次 probe")
    ap.add_argument("--keep", action="store_true", help="保留临时目录以便排查")
    args = ap.parse_args()

    dist = Path(args.dist).expanduser().resolve()
    combined = find_zip(dist, "combined")
    app_zip = find_zip(dist, "app")
    if not combined or not app_zip:
        print(f"dist 里缺 zip：combined={combined} app={app_zip}", file=sys.stderr)
        print("先跑 build_binary.py 再跑 pack_release.py --out dist", file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="scout-layered-"))
    home = tmp / "home"
    try:
        print(f"\n[1/4] 全量安装：{combined.name}  ({combined.stat().st_size / 1e6:.1f} MB)")
        root1 = extract(combined, tmp / "pkg-combined")
        proc = run_installer(root1, home)
        if proc.returncode != 0:
            print(proc.stdout[-3000:]); print(proc.stderr[-3000:], file=sys.stderr)
            print(f"全量安装失败 rc={proc.returncode}", file=sys.stderr)
            return 1
        installed = read_installed(home)
        check("三层都记进了 bin/layers.txt", set(installed) == set(L.LAYERS), str(installed))
        check(f"bin/{EXE} 存在", (home / "bin" / EXE).is_file())
        check("bin/app/mino_scout/cli.py 存在", (home / "bin" / "app" / "mino_scout" / "cli.py").is_file())

        browsers = home / "bin" / L.PAYLOAD_DIRS["browser"]
        before = tree_signature(browsers)
        rt_before = (home / "bin" / EXE).stat().st_mtime
        print(f"      浏览器层基线：{before[0]} 个文件 / {before[1] / 1e6:.0f} MB")

        print(f"\n[2/4] 只装 app 层：{app_zip.name}  ({app_zip.stat().st_size / 1024:.0f} KB)")
        root2 = extract(app_zip, tmp / "pkg-app")
        proc = run_installer(root2, home)
        if proc.returncode != 0:
            print(proc.stdout[-3000:]); print(proc.stderr[-3000:], file=sys.stderr)
            print(f"app 层安装失败 rc={proc.returncode}", file=sys.stderr)
            return 1
        after = tree_signature(browsers)
        check("浏览器层一个字节没动", before == after, f"{before} vs {after}")
        check("runtime 可执行文件没被重写", (home / "bin" / EXE).stat().st_mtime == rt_before)
        check("三层记录仍完整", set(read_installed(home)) == set(L.LAYERS))
        ratio = combined.stat().st_size / max(app_zip.stat().st_size, 1)
        print(f"      增量 / 全量 = 1 : {ratio:,.0f}")

        print("\n[3/4] 闸门：runtime 指纹不匹配的 app 层必须被拒")
        manifest = root2 / "layers.txt"
        text = manifest.read_text(encoding="utf-8")
        manifest.write_text(text.replace(
            f"requires_runtime={read_installed(home)['runtime']}",
            "requires_runtime=rt-0000000000"), encoding="utf-8")
        cli = home / "bin" / "app" / "mino_scout" / "cli.py"
        sig_before = cli.stat().st_mtime, cli.stat().st_size
        proc = run_installer(root2, home)
        check("退出码非 0", proc.returncode != 0, f"rc={proc.returncode}")
        check("拒绝后 app 层未被改动", (cli.stat().st_mtime, cli.stat().st_size) == sig_before)

        if args.probe:
            print("\n[4/4] 增量更新后真跑一次 probe")
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
            env["MINIORANGE_PLAYWRIGHT_HEADED"] = "0"
            env["PYTHONUTF8"] = "1"
            proc = subprocess.run([str(home / "bin" / EXE), "probe"], env=env,
                                  cwd=str(home / "bin"), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=300,
                                  stdin=subprocess.DEVNULL)
            blob = (proc.stdout or "") + (proc.stderr or "")
            check("probe 输出了 manifest", '"node_id"' in blob, f"rc={proc.returncode}")
            check("上报的版本是本次打的版本", f'"{L.package_version()}"' in blob)
        else:
            print("\n[4/4] 跳过 probe（加 --probe 会真跑一次）")

        print()
        if failures:
            print(f"FAIL check_layered_install — {len(failures)} 项不通过：")
            for f in failures:
                print(f"  - {f}")
            return 1
        print("OK check_layered_install — 分层安装与增量更新符合预期")
        return 0
    finally:
        if args.keep:
            print(f"\n临时目录保留在 {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
