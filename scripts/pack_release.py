#!/usr/bin/env python3
"""Build installer zips for the current OS/arch.

Prefer a frozen onedir (`python scripts/build_binary.py`) so the target
machine does not need Python. `--binary` freezes first. Without a freeze
this still packs the source tree (dev / unit tests).

## 分层产物

冻结模式下产出 **4 个 zip**：

| zip | 内容 | 用途 |
|---|---|---|
| `MinoScout-<ver>-<os>-<arch>.zip` | 三层齐全 | 全新安装。**名字刻意不变** —— 老 Studio 只认这一个 |
| `MinoScout-app-<ver>-<os>-<arch>.zip` | 只有 app 层 | 只改了代码的更新，实测 zip 后约 85 KB |
| `MinoScout-runtime-<rtkey>-<os>-<arch>.zip` | 只有 runtime 层 | 依赖 / Python / 冻结配方变了 |
| `MinoScout-browser-<bwkey>-<os>-<arch>.zip` | 只有 browser 层 | playwright 换 browser revision |

四个 zip 的结构完全一致 —— 根目录下是 `layers.json` + 若干载荷目录（`runtime/` /
`app/` / `browser/`）+ 安装脚本。层包只是少几个载荷目录。**install.sh 因此只有一条
代码路径**：present 的层就装，不 present 的层不动。

层包是**增量**，不替代合并包：全新安装本来就要全部字节，拆开只会多几次往返。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import layers as L  # noqa: E402


def _configure_utf8_stdio() -> None:
    """Windows CI 默认 cp1252，freeze 日志里的中文会直接把打包打挂。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not reconfigure:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_configure_utf8_stdio()


def package_version() -> str:
    env = str(os.environ.get("SCOUT_VERSION") or "").strip()
    if env:
        return env.lstrip("v")
    return L.package_version()


def os_name() -> str:
    return {"Darwin": "darwin", "Windows": "win32", "Linux": "linux"}[platform.system()]


def arch_name() -> str:
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return "x64"


def frozen_dir() -> Path:
    return ROOT / "build" / "dist" / "mino-scout"


def frozen_exe(dist: Path) -> Path:
    name = "mino-scout.exe" if platform.system() == "Windows" else "mino-scout"
    return dist / name


def _looks_executable(path: Path) -> bool:
    """Nested Playwright `node` / Chromium / adb are often 0644 after PyInstaller copy."""
    if not path.is_file():
        return False
    name = path.name.lower()
    if name in {"mino-scout", "mino-scout.exe", "node", "node.exe", "adb", "adb.exe", "chrome", "chrome.exe", "chromium", "ffmpeg", "ffmpeg.exe"}:
        return True
    if path.suffix.lower() in {".sh", ".exe", ".bin", ".command"}:
        return True
    try:
        if os.access(path, os.X_OK):
            return True
    except OSError:
        pass
    try:
        magic = path.read_bytes()[:4]
    except OSError:
        return False
    if magic == b"\x7fELF":
        return True
    if magic[:2] == b"MZ":
        return True
    if magic[:2] == b"#!":
        return True
    # Mach-O 64/32 and fat
    if magic in {
        b"\xcf\xfa\xed\xfe",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xfe\xed\xfa\xce",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
    }:
        return True
    return False


def chmod_payload(root: Path) -> None:
    if not root.is_dir():
        return
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if _looks_executable(path):
            try:
                path.chmod(path.stat().st_mode | 0o111)
            except OSError:
                pass


def _add_file(zf: zipfile.ZipFile, src: Path, arcname: str, *, executable: bool = False) -> None:
    info = zipfile.ZipInfo(arcname.replace("\\", "/"))
    info.compress_type = zipfile.ZIP_DEFLATED
    # Unix (3). Default ZipInfo is DOS (0), and macOS unzip then ignores 0755.
    info.create_system = 3
    mode = 0o755 if executable else 0o644
    info.external_attr = ((0o100000 | mode) & 0xFFFF) << 16
    data = src.read_bytes()
    if src.suffix == ".sh" or src.name in ("install.sh",):
        data = data.replace(b"\r\n", b"\n")
    zf.writestr(info, data)


def _add_bytes(zf: zipfile.ZipFile, data: bytes, arcname: str) -> None:
    info = zipfile.ZipInfo(arcname.replace("\\", "/"))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = ((0o100000 | 0o644) & 0xFFFF) << 16
    zf.writestr(info, data)


def _add_tree(zf: zipfile.ZipFile, src: Path, prefix: str) -> None:
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        if path.name == ".DS_Store" or path.suffix == ".pyc":
            continue
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(src).as_posix()
        _add_file(zf, path, f"{prefix}/{rel}", executable=_looks_executable(path))


def _ensure_binary() -> Path:
    dist = frozen_dir()
    exe = frozen_exe(dist)
    if exe.is_file():
        return dist
    from build_binary import build  # noqa: E402

    return build(clean=False)


def _self_check_binary(dist: Path) -> None:
    """PyInstaller 最常见的失败是打包成功、一跑 ModuleNotFoundError。"""
    from build_binary import check as check_binary  # noqa: E402

    if check_binary(dist) != 0:
        raise SystemExit("frozen binary self-check failed (probe)")


def layer_keys(frozen: Path, ver: str, osn: str, arch: str) -> dict[str, str]:
    return {
        "app": ver,
        "runtime": L.runtime_key(osn, arch),
        "browser": L.browser_key(frozen / "ms-playwright"),
    }


def _layer_meta(layer: str, keys: dict[str, str], frozen: Path) -> dict:
    meta: dict = {"key": keys[layer]}
    if layer == "app":
        # 版本闸门。客户端拿 app 层做增量更新前，必须确认本机 runtime 指纹相符 ——
        # 否则新代码引用了本机 runtime 里没有的依赖，换完就起不来。
        meta["requires_runtime"] = keys["runtime"]
        meta["requires_browser"] = keys["browser"]
    if layer == "browser":
        meta["dirs"] = L.browser_dir_names(frozen / "ms-playwright")
        # 只跑 adb 的节点永远用不到浏览器；它占了 78% 的体积，值得让客户端能跳过。
        meta["optional"] = True
    return meta


def _layers_txt(include: tuple[str, ...], keys: dict[str, str], frozen: Path) -> bytes:
    """zip 根目录的 layers.txt —— 给 install.sh / install.ps1 读的行式清单。

    刻意不让安装脚本去解 layers.json：bash 里没有 JSON 解析器，用 grep/sed 抠字段
    在真实数据面前必然出错。行式清单 awk 一行就取到，powershell 与 Node 同样好读。
    layers.json 保留给 manifest 与排查用。

    格式：`<层> <指纹> [k=v ...]`，`#` 开头是注释。
    注意与**安装后**的 `<bin>/layers.txt` 区分：那份只记 `<层> <指纹>`，是已装状态；
    这份是"本 zip 携带什么、需要什么"，是包清单。
    """
    lines = [
        "# 本 zip 携带的层。格式：<层> <指纹> [k=v ...]",
        "# 安装后的已装状态在 <bin>/layers.txt，只有 <层> <指纹> 两列。",
    ]
    for layer in include:
        meta = _layer_meta(layer, keys, frozen)
        extras: list[str] = []
        if layer == "app":
            extras = [
                f"requires_runtime={meta['requires_runtime']}",
                f"requires_browser={meta['requires_browser']}",
            ]
        if layer == "browser":
            extras = ["optional=1"]
        lines.append(" ".join([layer, meta["key"], *extras]))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _write_zip(
    zip_path: Path,
    root_name: str,
    frozen: Path,
    buckets: dict[str, list[Path]],
    include: tuple[str, ...],
    *,
    ver: str,
    osn: str,
    arch: str,
    keys: dict[str, str],
) -> dict:
    if zip_path.exists():
        zip_path.unlink()
    doc = {
        "version": ver,
        "os": osn,
        "arch": arch,
        "layers": {name: _layer_meta(name, keys, frozen) for name in include},
    }
    with zipfile.ZipFile(zip_path, "w") as zf:
        _add_bytes(zf, (json.dumps(doc, indent=2) + "\n").encode("utf-8"),
                   f"{root_name}/layers.json")
        _add_bytes(zf, _layers_txt(include, keys, frozen), f"{root_name}/layers.txt")
        for layer in include:
            for path in buckets[layer]:
                rel = L.payload_relpath(layer, path, frozen)
                _add_file(zf, path, f"{root_name}/{layer}/{rel}",
                          executable=_looks_executable(path))
        _add_file(zf, ROOT / "packaging" / "install.sh", f"{root_name}/install.sh", executable=True)
        _add_file(zf, ROOT / "packaging" / "install.ps1", f"{root_name}/install.ps1")
        _add_file(zf, ROOT / "packaging" / "README.txt", f"{root_name}/README.txt")
    blob = zip_path.read_bytes()
    return {
        "filename": zip_path.name,
        "sha256": hashlib.sha256(blob).hexdigest(),
        "bytes": len(blob),
    }


def _pack_source(out_dir: Path, name: str, zip_path: Path) -> None:
    """源码包（dev / 单测）。刻意保持旧结构 —— install.sh 的 venv+pip 路径没变。"""
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w") as zf:
        for path in (ROOT / "mino_scout").rglob("*"):
            if not path.is_file():
                continue
            if path.suffix in {".pyc"} or path.name == ".DS_Store":
                continue
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(ROOT).as_posix()
            _add_file(zf, path, f"{name}/{rel}")
        _add_file(zf, ROOT / "pyproject.toml", f"{name}/pyproject.toml")
        _add_file(zf, ROOT / "packaging" / "install.sh", f"{name}/install.sh", executable=True)
        _add_file(zf, ROOT / "packaging" / "install.ps1", f"{name}/install.ps1")
        _add_file(zf, ROOT / "packaging" / "README.txt", f"{name}/README.txt")


def pack(
    out_dir: Path,
    *,
    require_binary: bool = False,
    check_binary: bool = False,
) -> tuple[Path, dict]:
    ver = package_version()
    osn, arch = os_name(), arch_name()
    name = f"MinoScout-{ver}-{osn}-{arch}"
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{name}.zip"

    frozen = frozen_dir()
    use_binary = frozen_exe(frozen).is_file()
    if require_binary and not use_binary:
        frozen = _ensure_binary()
        use_binary = frozen_exe(frozen).is_file()
    if require_binary and not use_binary:
        raise SystemExit(f"frozen binary missing: {frozen_exe(frozen)}")

    if not use_binary:
        _pack_source(out_dir, name, zip_path)
        item = {
            "os": osn, "arch": arch, "filename": zip_path.name,
            "sha256": hashlib.sha256(zip_path.read_bytes()).hexdigest(),
            "bytes": zip_path.stat().st_size,
            "installer": "zip", "version": ver, "payload": "source",
        }
        (out_dir / f"item-{osn}-{arch}.json").write_text(
            json.dumps(item, indent=2) + "\n", encoding="utf-8")
        return zip_path, item

    chmod_payload(frozen)
    buckets = L.split_frozen(frozen)
    if not buckets["app"]:
        raise SystemExit(
            f"冻结产物缺 app 层：{frozen / 'app' / 'mino_scout'}\n"
            "app 层是纯源码、不进 PYZ（见 build_binary.py 的模块 docstring）。"
            "重跑 build_binary.py 生成。"
        )
    keys = layer_keys(frozen, ver, osn, arch)

    # 合并包：三层齐全，全新安装用。名字刻意与分层前一致，老 Studio 不受影响。
    combined = _write_zip(zip_path, name, frozen, buckets, L.LAYERS,
                          ver=ver, osn=osn, arch=arch, keys=keys)

    # 层包：增量更新用。
    layer_items: dict[str, dict] = {}
    for layer in L.LAYERS:
        if not buckets[layer]:
            continue
        lname = f"MinoScout-{layer}-{keys[layer]}-{osn}-{arch}"
        lzip = out_dir / f"{lname}.zip"
        info = _write_zip(lzip, lname, frozen, buckets, (layer,),
                          ver=ver, osn=osn, arch=arch, keys=keys)
        layer_items[layer] = {**_layer_meta(layer, keys, frozen), **info}

    item = {
        "os": osn, "arch": arch, "filename": zip_path.name,
        "sha256": combined["sha256"], "bytes": combined["bytes"],
        "installer": "zip", "version": ver, "payload": "binary",
        "layers": layer_items,
    }
    (out_dir / f"item-{osn}-{arch}.json").write_text(
        json.dumps(item, indent=2) + "\n", encoding="utf-8")

    # zip 先落盘，再自检：Windows probe 失败时 Release 仍能挂上包。
    if require_binary and check_binary:
        _self_check_binary(frozen)
    return zip_path, item


def _print_sizes(item: dict) -> None:
    mb = lambda n: f"{n / 1024 / 1024:.1f} MB"  # noqa: E731
    print(f"\n合并包  {item['filename']}  {mb(item['bytes'])}")
    for layer, info in (item.get("layers") or {}).items():
        print(f"  {layer:<8} {info['key']:<16} {mb(info['bytes']):>10}  {info['filename']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Pack Mino Scout zips for this OS")
    ap.add_argument("--print-version", action="store_true")
    ap.add_argument("--print-platform", action="store_true")
    ap.add_argument("--print-keys", action="store_true", help="print this platform's layer keys")
    ap.add_argument("--binary", action="store_true", help="freeze with PyInstaller, then pack")
    ap.add_argument(
        "--no-check",
        action="store_true",
        help="skip probe self-check after freeze (default with --binary is to check)",
    )
    ap.add_argument("--out", default="", help="output directory (default dist/)")
    args = ap.parse_args(argv)

    if args.print_version:
        print(package_version())
        return 0
    if args.print_platform:
        print(f"{os_name()} {arch_name()}")
        return 0
    if args.print_keys:
        keys = layer_keys(frozen_dir(), package_version(), os_name(), arch_name())
        for layer in L.LAYERS:
            print(f"{layer}\t{keys[layer] or '(no payload)'}")
        return 0

    out = Path(args.out).expanduser() if args.out else Path(os.environ.get("SCOUT_DIST") or ROOT / "dist")
    zip_path, item = pack(
        out,
        require_binary=args.binary,
        check_binary=args.binary and not args.no_check,
    )
    print(zip_path)
    print(item["sha256"])
    print(item["payload"])
    _print_sizes(item)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyError:
        print(f"unsupported platform: {platform.system()}", file=sys.stderr)
        raise SystemExit(2)
