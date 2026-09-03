#!/usr/bin/env python3
"""Build an installer zip for the current OS/arch.

Prefer a frozen onedir (`python scripts/build_binary.py`) so the target
machine does not need Python. `--binary` freezes first. Without a freeze
this still packs the source tree (dev / unit tests).
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
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("version"):
            _, _, rest = line.partition("=")
            return rest.strip().strip('"').strip("'")
    raise SystemExit("could not read version from pyproject.toml")


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


def _add_file(zf: zipfile.ZipFile, src: Path, arcname: str, *, executable: bool = False) -> None:
    info = zipfile.ZipInfo(arcname.replace("\\", "/"))
    info.compress_type = zipfile.ZIP_DEFLATED
    mode = 0o755 if executable else 0o644
    info.external_attr = (mode & 0xFFFF) << 16
    data = src.read_bytes()
    if src.suffix == ".sh" or src.name in ("install.sh",):
        data = data.replace(b"\r\n", b"\n")
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
        executable = path.is_file() and (os.access(path, os.X_OK) or path.suffix in {".sh", ".exe"})
        if path.name in ("mino-scout", "mino-scout.exe"):
            executable = True
        _add_file(zf, path, f"{prefix}/{rel}", executable=executable)


def _ensure_binary() -> Path:
    dist = frozen_dir()
    exe = frozen_exe(dist)
    if exe.is_file():
        return dist
    sys.path.insert(0, str(ROOT / "scripts"))
    from build_binary import build  # noqa: E402

    return build(clean=False)


def _self_check_binary(dist: Path) -> None:
    """PyInstaller 最常见的失败是打包成功、一跑 ModuleNotFoundError。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    from build_binary import check as check_binary  # noqa: E402

    if check_binary(dist) != 0:
        raise SystemExit("frozen binary self-check failed (probe)")


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
        if not use_binary:
            raise SystemExit(f"frozen binary missing: {frozen_exe(frozen)}")
    if require_binary and check_binary:
        if not use_binary:
            raise SystemExit(f"frozen binary missing: {frozen_exe(frozen)}")
        _self_check_binary(frozen)

    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w") as zf:
        if use_binary:
            _add_tree(zf, frozen, f"{name}/mino-scout")
        else:
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

    sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    item = {
        "os": osn,
        "arch": arch,
        "filename": zip_path.name,
        "sha256": sha,
        "installer": "zip",
        "version": ver,
        "payload": "binary" if use_binary else "source",
    }
    (out_dir / f"item-{osn}-{arch}.json").write_text(json.dumps(item, indent=2) + "\n", encoding="utf-8")
    return zip_path, item


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Pack a Mino Scout zip for this OS")
    ap.add_argument("--print-version", action="store_true")
    ap.add_argument("--print-platform", action="store_true")
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

    out = Path(args.out).expanduser() if args.out else Path(os.environ.get("SCOUT_DIST") or ROOT / "dist")
    zip_path, item = pack(
        out,
        require_binary=args.binary,
        check_binary=args.binary and not args.no_check,
    )
    print(zip_path)
    print(item["sha256"])
    print(item["payload"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyError:
        print(f"unsupported platform: {platform.system()}", file=sys.stderr)
        raise SystemExit(2)
