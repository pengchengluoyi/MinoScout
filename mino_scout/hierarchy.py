# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""UI 层级采集与锚点解析（S0b）。

为什么需要
----------
现引擎让决策 VLM 每步直出绝对坐标，实测同一个按钮会给出七种坐标
（`455,2094 → 450,2081 → 462,2094 → 456,2086 …`，见 docs/plan-review-and-industry-comparison.md §3.5）。
后果：① 震荡检测要求 `str(params)` 全等，永久失效；② 动作轨迹无法复用。

改法：模型给**语义锚点**（resource_id / text / content_desc），执行侧用 UI 层级把它
解析成精确坐标，模型的坐标只作兜底。同一个按钮 → 同一个锚点 → 动作签名稳定。

成本与来源（实测，结论改过一次）
--------------------------------
起初用 `adb shell uiautomator dump`，实测 **2.2s**，且在**动画页面上直接失败**
（`ERROR: could not get idle state.` —— 它死等 idle，而造物相机社区页卡片一直在动，
三次全失败）。改用 uiautomator2 后：首次连接 745ms，**连接复用后单次约 35ms**，
且动画页面能正常 dump。

所以来源优先级是 u2 → adb exec-out → adb 落盘，且 35ms 的开销让"每步都取层级"
重新变得可行（是否注入 prompt 仍由调用方决定）。
"""
from __future__ import annotations

import re
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Optional

from mino_scout.log import SLog

TAG = "Hierarchy"

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# dump 结果缓存：serial -> (ts, UiDump)。同一屏内多次锚点解析共用一次 dump。
_CACHE: dict[str, tuple[float, "UiDump"]] = {}
_CACHE_TTL_SEC = 1.5

# uiautomator2 连接复用：首次连接约 745ms，复用后单次 dump 约 35ms
_U2_CONNS: dict[str, Any] = {}


@dataclass
class UiNode:
    index: int
    resource_id: str = ""
    text: str = ""
    content_desc: str = ""
    cls: str = ""
    package: str = ""
    clickable: bool = False
    enabled: bool = True
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    parent: Optional["UiNode"] = field(default=None, repr=False)

    @property
    def center(self) -> tuple[int, int]:
        l, t, r, b = self.bounds
        return (l + r) // 2, (t + b) // 2

    @property
    def area(self) -> int:
        l, t, r, b = self.bounds
        return max(0, r - l) * max(0, b - t)

    @property
    def rid_short(self) -> str:
        """去掉包名前缀的 resource-id（模型常只给后半段）。"""
        return self.resource_id.split("/", 1)[-1] if self.resource_id else ""

    def label(self) -> str:
        return self.text or self.content_desc or self.rid_short or self.cls.split(".")[-1]

    def to_brief(self) -> dict[str, Any]:
        cx, cy = self.center
        return {
            "resource_id": self.resource_id,
            "text": self.text,
            "content_desc": self.content_desc,
            "class": self.cls,
            "clickable": self.clickable,
            "bounds": list(self.bounds),
            "center": [cx, cy],
        }


@dataclass
class UiDump:
    ok: bool = False
    nodes: list[UiNode] = field(default_factory=list)
    error: str = ""
    elapsed_ms: int = 0
    source: str = "adb"
    raw_text: str = ""

    def __len__(self) -> int:
        return len(self.nodes)


# ---------- 采集 ----------


def _run(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           errors="replace")
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout {timeout}s"
    except FileNotFoundError:
        return -2, "", "adb not in PATH"


def _u2_dump(serial: str) -> tuple[str, str]:
    """用 uiautomator2 抓层级（主路径）。返回 (xml, error)。

    为什么优先用它而不是 `adb shell uiautomator dump`：
      - **动画页面上 `uiautomator dump` 直接失败**（`ERROR: could not get idle state.`），
        它会死等 idle；而造物相机社区页卡片一直在动，实测三次全失败；
      - 快得多：首次连接约 745ms，**连接复用后约 35ms**（对比 exec-out 约 2.2s）。
    依赖已在 requirements（uiautomator2==3.5.0），设备侧 agent 也已安装
    （`driver/tentacle/engine/mobile/mAdb.py` 早就用它注入触控）。
    """
    try:
        import uiautomator2 as u2
    except ImportError as exc:
        return "", f"uiautomator2 不可用: {exc}"
    try:
        dev = _U2_CONNS.get(serial)
        if dev is None:
            dev = u2.connect(serial)
            _U2_CONNS[serial] = dev
        return dev.dump_hierarchy(compressed=False) or "", ""
    except Exception as exc:  # pragma: no cover - 设备侧 agent 异常
        _U2_CONNS.pop(serial, None)   # 连接可能已坏，下次重连
        return "", f"u2 dump 失败: {type(exc).__name__}: {exc}"


def dump_ui_nodes(
    serial: str,
    *,
    timeout_sec: float = 12.0,
    force_fresh: bool = False,
) -> UiDump:
    """抓一次 UI 层级并解析成 UiNode 列表（带 TTL 缓存）。

    来源优先级：uiautomator2 → adb exec-out → adb 落盘再读。
    """
    if not serial:
        return UiDump(ok=False, error="missing serial")
    if not force_fresh:
        hit = _CACHE.get(serial)
        if hit and (time.time() - hit[0]) < _CACHE_TTL_SEC:
            cached = hit[1]
            SLog.i(TAG, f"hierarchy cache hit sn={serial} nodes={len(cached)}")
            return cached

    t0 = time.time()
    errors: list[str] = []
    source = ""

    xml_text, err = _u2_dump(serial)
    if xml_text:
        source = "u2"
    else:
        errors.append(err or "u2 无输出")
        # 兜底 1：exec-out 直出（动画页面会失败，仅作后备）
        rc, out, err2 = _run(
            ["adb", "-s", serial, "exec-out", "uiautomator", "dump", "/dev/tty"], timeout_sec
        )
        xml_text = _extract_xml(out)
        if xml_text:
            source = "adb_exec_out"
        else:
            errors.append((err2 or out or f"rc={rc}").strip()[:120])
            # 兜底 2：落盘再读（老设备 /dev/tty 不通）
            rc2, _, err3 = _run(
                ["adb", "-s", serial, "shell", "uiautomator", "dump", "/sdcard/window_dump.xml"],
                timeout_sec,
            )
            if rc2 == 0:
                _, out2, _ = _run(
                    ["adb", "-s", serial, "shell", "cat", "/sdcard/window_dump.xml"], timeout_sec
                )
                xml_text = _extract_xml(out2)
                if xml_text:
                    source = "adb_file"
            if not xml_text:
                errors.append((err3 or f"rc={rc2}").strip()[:120])

    if not xml_text:
        dump = UiDump(ok=False, error=" | ".join(e for e in errors if e)[:240],
                      elapsed_ms=int((time.time() - t0) * 1000))
        SLog.w(TAG, f"hierarchy dump failed sn={serial}: {dump.error}")
        return dump

    dump = _parse_xml(xml_text)
    dump.source = source
    dump.elapsed_ms = int((time.time() - t0) * 1000)
    SLog.i(TAG, f"hierarchy dump sn={serial} src={source} nodes={len(dump)} "
                f"ok={dump.ok} elapsed={dump.elapsed_ms}ms")
    if dump.ok:
        _CACHE[serial] = (time.time(), dump)
    return dump


def invalidate_cache(serial: str = "") -> None:
    """动作执行后屏幕会变，调用方应主动失效缓存。"""
    if serial:
        _CACHE.pop(serial, None)
    else:
        _CACHE.clear()


def _extract_xml(text: str) -> str:
    """从 uiautomator 输出里截出 XML（它会混入 'UI hierchary dumped to: ...' 之类文案）。"""
    if not text:
        return ""
    i = text.find("<hierarchy")
    if i < 0:
        i = text.find("<?xml")
        if i < 0:
            return ""
    j = text.rfind("</hierarchy>")
    return text[i:j + len("</hierarchy>")] if j > i else text[i:]


def _parse_bounds(raw: str) -> tuple[int, int, int, int]:
    m = _BOUNDS_RE.search(raw or "")
    if not m:
        return (0, 0, 0, 0)
    l, t, r, b = (int(g) for g in m.groups())
    return (l, t, r, b)


def _parse_xml(xml_text: str) -> UiDump:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return UiDump(ok=False, error=f"xml parse: {exc}")

    nodes: list[UiNode] = []

    def walk(el: ET.Element, parent: Optional[UiNode]) -> None:
        node: Optional[UiNode] = None
        if el.tag == "node":
            a = el.attrib
            node = UiNode(
                index=len(nodes),
                resource_id=(a.get("resource-id") or "").strip(),
                text=(a.get("text") or "").strip(),
                content_desc=(a.get("content-desc") or "").strip(),
                cls=(a.get("class") or "").strip(),
                package=(a.get("package") or "").strip(),
                clickable=(a.get("clickable") == "true"),
                enabled=(a.get("enabled") != "false"),
                bounds=_parse_bounds(a.get("bounds") or ""),
                parent=parent,
            )
            nodes.append(node)
        for child in el:
            walk(child, node or parent)

    walk(root, None)
    return UiDump(ok=True, nodes=nodes)


# ---------- 锚点解析 ----------

_MATCH_ORDER = (
    "resource_id",       # 最稳
    "resource_id_short",
    "text",
    "content_desc",
    "text_contains",
    "content_desc_contains",
)


@dataclass
class AnchorMatch:
    node: UiNode
    matched_by: str
    candidates: int = 1
    clickable_candidates: int = 0

    def to_brief(self) -> dict[str, Any]:
        out = self.node.to_brief()
        out["matched_by"] = self.matched_by
        out["candidates"] = self.candidates
        out["clickable_candidates"] = self.clickable_candidates
        return out


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


def _clickable_target(node: UiNode, max_up: int = 4) -> Optional[UiNode]:
    """返回该节点本身或最近的可点击祖先；都不可点击则 None。

    文本节点常包在可点击父容器里（点父容器才有效），所以要往上找。
    """
    cur: Optional[UiNode] = node
    hops = 0
    while cur is not None and hops <= max_up:
        if cur.clickable and cur.area > 0:
            return cur
        cur = cur.parent
        hops += 1
    return None


def resolve_target(
    nodes: list[UiNode],
    target: dict[str, Any],
    *,
    prefer_clickable: bool = True,
) -> Optional[AnchorMatch]:
    """按优先级把语义锚点解析成节点。

    target 支持键：resource_id / text / content_desc（可同时给，按优先级逐个尝试）。

    同一优先级命中多个时的挑选规则（顺序很重要）：
      1. **先按可点击性分层** —— 只在"自身或祖先可点击"的候选里挑。
         实测踩过的坑：`content_desc="社区"` 同时命中顶部标题(121,220, 不可点击)
         与底栏 Tab(233,2494)；若先按面积挑会选中标题，点下去毫无效果
         —— 这正是"点击成功但界面无变化"那类假动作的来源。
      2. 同层内取**面积最小**的（最具体的那个）。
    """
    if not nodes or not isinstance(target, dict):
        return None

    rid = _norm(target.get("resource_id") or target.get("resource-id") or target.get("id"))
    text = _norm(target.get("text"))
    desc = _norm(target.get("content_desc") or target.get("content-desc") or target.get("desc"))

    usable = [n for n in nodes if n.enabled and n.area > 0]

    def pick(cands: list[UiNode], how: str) -> Optional[AnchorMatch]:
        if not cands:
            return None
        total = len(cands)
        if prefer_clickable:
            # 映射到"实际可点的那个节点"，只保留能点的候选
            actionable = [(c, _clickable_target(c)) for c in cands]
            actionable = [(c, t) for c, t in actionable if t is not None]
            if actionable:
                _, chosen = min(actionable, key=lambda pair: pair[1].area)
                return AnchorMatch(node=chosen, matched_by=how, candidates=total,
                                   clickable_candidates=len(actionable))
            # 一个都不可点击：仍返回最小者，但标记出来供审计
            chosen = min(cands, key=lambda n: n.area)
            return AnchorMatch(node=chosen, matched_by=how, candidates=total,
                              clickable_candidates=0)
        return AnchorMatch(node=min(cands, key=lambda n: n.area), matched_by=how,
                           candidates=total, clickable_candidates=0)

    for how in _MATCH_ORDER:
        if how == "resource_id" and rid:
            got = pick([n for n in usable if _norm(n.resource_id) == rid], how)
        elif how == "resource_id_short" and rid:
            got = pick([n for n in usable if _norm(n.rid_short) == rid], how)
        elif how == "text" and text:
            got = pick([n for n in usable if _norm(n.text) == text], how)
        elif how == "content_desc" and desc:
            got = pick([n for n in usable if _norm(n.content_desc) == desc], how)
        elif how == "text_contains" and text:
            got = pick([n for n in usable if text in _norm(n.text)], how)
        elif how == "content_desc_contains" and desc:
            got = pick([n for n in usable if desc in _norm(n.content_desc)], how)
        else:
            got = None
        if got is not None:
            return got
    return None


def has_target(params: dict[str, Any]) -> bool:
    """params 里是否带了可用的语义锚点。"""
    t = (params or {}).get("target")
    if not isinstance(t, dict):
        return False
    return any(_norm(t.get(k)) for k in
               ("resource_id", "resource-id", "id", "text", "content_desc", "content-desc", "desc"))


# ---------- 供 prompt / 调试使用的紧凑视图 ----------


def to_prompt_text(dump: UiDump, *, limit: int = 40, max_chars: int = 3000) -> str:
    """把层级压成紧凑可读列表（仅可点击或带文案的节点）。

    默认不在每步注入（dump 要 ~2.2s）；排障或 dry-run 时用。
    """
    if dump.raw_text:
        return str(dump.raw_text)[:max_chars]
    if not dump.ok:
        return ""
    lines: list[str] = []
    seen: set[tuple] = set()
    for n in dump.nodes:
        if not (n.clickable or n.text or n.content_desc):
            continue
        if n.area <= 0:
            continue
        key = (n.rid_short, n.text, n.content_desc)
        if key in seen:
            continue
        seen.add(key)
        cx, cy = n.center
        bits = []
        if n.text:
            bits.append(f'text="{n.text[:24]}"')
        if n.content_desc:
            bits.append(f'desc="{n.content_desc[:24]}"')
        if n.rid_short:
            bits.append(f"id={n.rid_short[:28]}")
        flag = "点" if n.clickable else " "
        lines.append(f"[{flag}] {' '.join(bits)} @({cx},{cy})")
        if len(lines) >= limit:
            break
    text = "\n".join(lines)
    return text[:max_chars]
