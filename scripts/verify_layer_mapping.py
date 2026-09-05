#!/usr/bin/env python3
"""守门：分层的「层 → 目录」映射在四处必须一致。

`scripts/layers.py` 的 `PAYLOAD_DIRS` 是真源，但另外三处不得不各带一份副本：

| 处 | 为什么带副本 |
|---|---|
| `packaging/install.sh`  | bash 里没法 import python |
| `packaging/install.ps1` | 同上 |
| `build_binary.py` 的冻结入口 | 那段代码跑在冻结产物里，看不到 scripts/ |

漂移的症状极难认：装是装上了，但 app 层落在 Scout 找不到的地方，
启动时只有一句 "app layer missing"。所以宁可在 CI 里断掉。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import layers as L  # noqa: E402

NAME = "verify_layer_mapping"
problems: list[str] = []


def _fail(msg: str) -> None:
    problems.append(msg)


def check_install_sh() -> int:
    path = ROOT / "packaging" / "install.sh"
    text = path.read_text(encoding="utf-8")
    body = re.search(r"layer_target\(\)\s*\{(.*?)\n\}", text, re.S)
    if not body:
        _fail(f"{path.name}: 找不到 layer_target() —— 函数改形状了，守门脚本要跟着改")
        return 0
    found = dict(re.findall(r"^\s*(\w+)\)\s*printf\s+'%s'\s+\"([^\"]*)\"", body.group(1), re.M))
    if found != L.PAYLOAD_DIRS:
        _fail(f"{path.name}: layer_target() 映射为 {found}，layers.py 是 {L.PAYLOAD_DIRS}")
    # 装完的 plist 里 PLAYWRIGHT_BROWSERS_PATH 也要指向 browser 层的落点。
    want = f"$PREFIX/bin/{L.PAYLOAD_DIRS['browser']}"
    if "${PREFIX}/bin/" + L.PAYLOAD_DIRS["browser"] not in text:
        _fail(f"{path.name}: plist 的 PLAYWRIGHT_BROWSERS_PATH 没指向 {want}")
    return len(found)


def check_install_ps1() -> int:
    path = ROOT / "packaging" / "install.ps1"
    text = path.read_text(encoding="utf-8")
    body = re.search(r"function Get-LayerTarget.*?switch \(\$layer\)\s*\{(.*?)\n  \}", text, re.S)
    if not body:
        _fail(f"{path.name}: 找不到 Get-LayerTarget 的 switch —— 函数改形状了")
        return 0
    found = dict(re.findall(r'^\s*"(\w+)"\s*\{\s*return\s*"([^"]*)"', body.group(1), re.M))
    if found != L.PAYLOAD_DIRS:
        _fail(f"{path.name}: Get-LayerTarget 映射为 {found}，layers.py 是 {L.PAYLOAD_DIRS}")
    return len(found)


def check_frozen_entry() -> int:
    """冻结入口里写死的目录名，必须与 PAYLOAD_DIRS 对得上。"""
    import build_binary as B  # noqa: E402

    src = B._ENTRY_SRC
    checks = 0
    app = L.PAYLOAD_DIRS["app"]
    if f'_base / "{app}"' not in src:
        _fail(f"build_binary._ENTRY_SRC: app 层目录不是 {app!r}")
    checks += 1
    browser = L.PAYLOAD_DIRS["browser"]
    if f'_base / "{browser}"' not in src:
        _fail(f"build_binary._ENTRY_SRC: browser 层目录不是 {browser!r}")
    checks += 1
    # 入口只能用 importlib 取 mino_scout —— 写成 import 就会被编进 PYZ，
    # 而 FrozenImporter 优先级高于文件系统，app 层会被无声忽略。
    if "importlib.import_module" not in src:
        _fail("build_binary._ENTRY_SRC: 入口没用 importlib.import_module，app 层会被 PYZ 盖掉")
    checks += 1
    if re.search(r"^\s*(from|import)\s+mino_scout", src, re.M):
        _fail("build_binary._ENTRY_SRC: 入口静态 import 了 mino_scout，它会被编进 PYZ")
    checks += 1
    return checks


def check_layer_names() -> int:
    """两个安装脚本枚举的层名要与 layers.LAYERS 一致，否则会漏装一层。"""
    checks = 0
    sh = (ROOT / "packaging" / "install.sh").read_text(encoding="utf-8")
    m = re.search(r'^LAYER_NAMES="([^"]*)"', sh, re.M)
    if not m:
        _fail("install.sh: 找不到 LAYER_NAMES")
    elif tuple(m.group(1).split()) != L.LAYERS:
        _fail(f"install.sh: LAYER_NAMES={m.group(1).split()}，layers.py 是 {list(L.LAYERS)}")
    checks += 1

    ps1 = (ROOT / "packaging" / "install.ps1").read_text(encoding="utf-8")
    m = re.search(r"^\$LayerNames = @\(([^)]*)\)", ps1, re.M)
    if not m:
        _fail("install.ps1: 找不到 $LayerNames")
    else:
        names = tuple(re.findall(r'"(\w+)"', m.group(1)))
        if names != L.LAYERS:
            _fail(f"install.ps1: $LayerNames={list(names)}，layers.py 是 {list(L.LAYERS)}")
    checks += 1
    return checks


def main() -> int:
    total = 0
    total += check_install_sh()
    total += check_install_ps1()
    total += check_frozen_entry()
    total += check_layer_names()

    if problems:
        print(f"FAIL {NAME} — 层映射漂移了：")
        for p in problems:
            print(f"  - {p}")
        print("\n真源是 scripts/layers.py 的 PAYLOAD_DIRS / LAYERS。")
        return 1
    print(f"OK {NAME} — 层映射一致（{len(L.LAYERS)} 层，{total} 项断言）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
