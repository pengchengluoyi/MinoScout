"""Web 页 DOM 快照 → 协议 `accessibility_json` 节点列表（非安卓 UI hierarchy）。"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from mino_scout.log import SLog
from mino_scout.playwright_hub import get_hub

TAG = "DomHierarchy"

_JS_COLLECT = """
() => {
  const out = [];
  const vw = window.innerWidth || 1280;
  const vh = window.innerHeight || 800;
  const sel = 'input, textarea, select, button, a, [role="button"], [role="link"], [role="textbox"]';
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (!r || r.width < 2 || r.height < 2) continue;
    if (r.bottom < 0 || r.top > vh || r.right < 0 || r.left > vw) continue;
    const tag = (el.tagName || '').toLowerCase();
    const typ = (el.getAttribute('type') || '').toLowerCase();
    const val = (el.value || '').toString();
    const inner = (el.innerText || el.textContent || '').trim().slice(0, 200);
    const text = val || inner;
    const placeholder = el.getAttribute('placeholder') || '';
    const aria = el.getAttribute('aria-label') || '';
    const role = el.getAttribute('role') || '';
    const editable = tag === 'input' || tag === 'textarea' || el.isContentEditable;
    const clickable = tag === 'button' || tag === 'a' || role === 'button' || role === 'link'
      || el.onclick != null || window.getComputedStyle(el).cursor === 'pointer';
    out.push({
      resource_id: el.id || '',
      text,
      content_desc: placeholder || aria || role,
      class: typ ? `${tag}:${typ}` : tag,
      role,
      clickable: !!clickable,
      editable: !!editable,
      bounds: [Math.round(r.left), Math.round(r.top), Math.round(r.right), Math.round(r.bottom)],
      center: [Math.round(r.left + r.width / 2), Math.round(r.top + r.height / 2)],
    });
    if (out.length >= 280) break;
  }
  return out;
}
"""


@dataclass
class DomDump:
    ok: bool
    nodes: list[dict[str, Any]]
    error: str = ""
    elapsed_ms: int = 0


def dump_dom_nodes(*, sn: str, run_id: str = "") -> DomDump:
    t0 = time.time()
    hub = get_hub()
    page = hub.current_page(str(sn or ""), run_id=str(run_id or ""))
    if page is None:
        return DomDump(
            ok=False,
            nodes=[],
            error=f"web 槽无打开页面 sn={sn} run_id={run_id or '(empty)'}",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    try:
        raw = page.evaluate(_JS_COLLECT)
    except Exception as exc:
        SLog.w(TAG, f"dom evaluate failed sn={sn}: {exc}")
        return DomDump(
            ok=False,
            nodes=[],
            error=f"DOM 采集失败: {exc}",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    nodes = [n for n in (raw or []) if isinstance(n, dict)]
    try:
        page_url = str(page.url or "").strip()
    except Exception:
        page_url = ""
    if page_url:
        nodes.insert(
            0,
            {
                "resource_id": "__page__",
                "class": "document:html",
                "text": "",
                "content_desc": page_url[:500],
                "url": page_url,
                "page_url": page_url,
                "href": page_url,
                "clickable": False,
                "editable": False,
                "bounds": [0, 0, 0, 0],
                "center": [0, 0],
            },
        )
    elapsed = int((time.time() - t0) * 1000)
    SLog.i(TAG, f"dom dump sn={sn} nodes={len(nodes)} ms={elapsed}")
    return DomDump(ok=True, nodes=nodes, elapsed_ms=elapsed)
