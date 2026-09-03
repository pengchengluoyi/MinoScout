"""pack_release / write_manifest 能产出 zip + items[] manifest。"""
from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from pack_release import _looks_executable, pack, package_version  # noqa: E402
from write_manifest import build_manifest, load_items  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{' — ' + detail if detail else ''}")
        failures.append(name)


print("== pack current OS ==")
ver = package_version()
check("version nonempty", bool(ver), ver)

core_text = Path(__file__).resolve().parent.parent.joinpath("mino_scout", "core.py").read_text(encoding="utf-8")
runtime = next(
    line.split("=", 1)[1].strip().strip('"').strip("'")
    for line in core_text.splitlines()
    if line.startswith("SCOUT_VERSION")
)
check("runtime version matches pyproject", runtime == ver, f"{runtime} vs {ver}")

with tempfile.TemporaryDirectory(prefix="scout-pack-") as tmp:
    out = Path(tmp)
    zip_path, item = pack(out)
    check("zip exists", zip_path.is_file(), str(zip_path))
    check("zip named", zip_path.name.startswith("MinoScout-") and zip_path.suffix == ".zip", zip_path.name)
    check("sha256", len(item.get("sha256") or "") == 64, str(item.get("sha256")))
    check("installer zip", item.get("installer") == "zip")
    check("payload kind", item.get("payload") in {"source", "binary"}, str(item.get("payload")))
    items = load_items(out)
    check("item fragment", len(items) == 1, str(items))
    manifest = build_manifest(items, version=ver, prefix="https://github.com/example/MinoScout/releases/download/v" + ver)
    check("manifest version", manifest.get("version") == ver, str(manifest.get("version")))
    check("manifest items", len(manifest.get("items") or []) == 1)
    row = manifest["items"][0]
    check("url uses prefix", str(row.get("url") or "").startswith("https://github.com/example/MinoScout/"), str(row.get("url")))
    check("filename matches zip", row.get("filename") == zip_path.name, str(row.get("filename")))

    with zipfile.ZipFile(zip_path) as zf:
        sh_name = next(n for n in zf.namelist() if n.endswith("/install.sh") or n == "install.sh")
        info = zf.getinfo(sh_name)
        mode = (info.external_attr >> 16) & 0o777
        check("install.sh unix zip", info.create_system == 3, str(info.create_system))
        check("install.sh 0755", mode == 0o755, oct(mode))

    node = out / "node"
    node.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 16)
    check("mach-o looks executable", _looks_executable(node))
    txt = out / "readme.txt"
    txt.write_text("hi", encoding="utf-8")
    check("text not executable", not _looks_executable(txt))

print()
if failures:
    print(f"FAILED {len(failures)}: {', '.join(failures)}")
    sys.exit(1)
print("ALL OK — packaging")
