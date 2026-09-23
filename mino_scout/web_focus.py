"""Web 页焦点探测：只读 page.evaluate(activeElement)，供 DOM 采集与 input/tap 后校验。"""
from __future__ import annotations

from typing import Any

_JS_ACTIVE_FOCUS = """
() => {
  const el = document.activeElement;
  if (!el || el === document.body || el === document.documentElement) {
    return { editable_ready: false, reason: 'none', tag: '', type: '', id: '', name: '', value_len: 0, bounds: [] };
  }
  const tag = (el.tagName || '').toLowerCase();
  const typ = (el.getAttribute('type') || '').toLowerCase();
  const ce = !!el.isContentEditable;
  const editable = tag === 'input' || tag === 'textarea' || ce;
  const disabled = !!el.disabled;
  const ro = !!el.readOnly;
  const ready = editable && !disabled && !ro;
  let value_len = 0;
  if (tag === 'input' || tag === 'textarea') {
    value_len = String(el.value || '').length;
  } else if (ce) {
    value_len = String(el.innerText || el.textContent || '').trim().length;
  }
  const r = el.getBoundingClientRect();
  return {
    editable_ready: ready,
    reason: ready ? 'focus' : (disabled ? 'disabled' : (ro ? 'readonly' : 'not_editable')),
    tag,
    type: typ,
    id: String(el.id || '').slice(0, 64),
    name: String(el.name || '').slice(0, 64),
    value_len,
    bounds: r ? [Math.round(r.left), Math.round(r.top), Math.round(r.right), Math.round(r.bottom)] : [],
  };
}
"""


def evaluate_web_focus(page: Any) -> dict[str, Any]:
    try:
        raw = page.evaluate(_JS_ACTIVE_FOCUS)
    except Exception:
        return {"editable_ready": False, "reason": "evaluate_failed"}
    return dict(raw) if isinstance(raw, dict) else {"editable_ready": False, "reason": "bad_shape"}


def web_focus_editable_ready(focus: dict[str, Any] | None) -> bool:
    return bool((focus or {}).get("editable_ready"))


def mark_focused_node(nodes: list[dict[str, Any]], focus: dict[str, Any] | None) -> None:
    if not web_focus_editable_ready(focus):
        return
    fb = list((focus or {}).get("bounds") or [])
    if len(fb) < 4:
        return
    fx = (int(fb[0]) + int(fb[2])) // 2
    fy = (int(fb[1]) + int(fb[3])) // 2
    best_i = -1
    best_d = 10**9
    for i, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        b = node.get("bounds") or []
        if len(b) < 4:
            continue
        cx = (int(b[0]) + int(b[2])) // 2
        cy = (int(b[1]) + int(b[3])) // 2
        d = abs(cx - fx) + abs(cy - fy)
        if d < best_d:
            best_d = d
            best_i = i
    if best_i >= 0 and best_d <= 80:
        nodes[best_i]["focused"] = True
