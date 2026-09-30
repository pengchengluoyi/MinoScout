"""Web 页焦点：穿透 open shadowRoot 与同源 iframe，供 DOM 采集与输入前聚焦。"""
from __future__ import annotations

from typing import Any

# 只读。返回当前最深层可编辑焦点；没有则 editable_ready=false。
_JS_ACTIVE_FOCUS = """
() => {
  const SKIP_TYPE = { hidden: 1, checkbox: 1, radio: 1, button: 1, submit: 1, file: 1, image: 1 };
  function isEditable(el) {
    if (!el || !el.tagName) return false;
    if (el === el.ownerDocument.body || el === el.ownerDocument.documentElement) return false;
    const tag = el.tagName.toLowerCase();
    const typ = (el.getAttribute('type') || '').toLowerCase();
    if (SKIP_TYPE[typ]) return false;
    const role = (el.getAttribute('role') || '').toLowerCase();
    const ce = !!el.isContentEditable;
    const editable = tag === 'input' || tag === 'textarea' || ce || role === 'textbox';
    return editable && !el.disabled && !el.readOnly;
  }
  function describe(el, ox, oy) {
    if (!el || !el.tagName) {
      return { editable_ready: false, reason: 'none', tag: '', type: '', id: '', name: '', value_len: 0, value: '', bounds: [] };
    }
    const tag = el.tagName.toLowerCase();
    const typ = (el.getAttribute('type') || '').toLowerCase();
    const ce = !!el.isContentEditable;
    const ready = isEditable(el);
    let value = '';
    if (tag === 'input' || tag === 'textarea') value = String(el.value || '');
    else if (ce || (el.getAttribute('role') || '').toLowerCase() === 'textbox') {
      value = String(el.innerText || el.textContent || '').trim();
    }
    const r = el.getBoundingClientRect();
    return {
      editable_ready: ready,
      reason: ready ? 'focus' : (el.disabled ? 'disabled' : (el.readOnly ? 'readonly' : 'not_editable')),
      tag,
      type: typ,
      id: String(el.id || '').slice(0, 64),
      name: String(el.getAttribute('name') || '').slice(0, 64),
      value_len: value.length,
      value: value.slice(0, 120),
      bounds: r ? [
        Math.round(r.left + ox), Math.round(r.top + oy),
        Math.round(r.right + ox), Math.round(r.bottom + oy),
      ] : [],
    };
  }
  function deepest(doc, ox, oy, depth) {
    if (!doc || depth > 8) return describe(null, ox, oy);
    let el = doc.activeElement;
    if (!el || el === doc.body || el === doc.documentElement) return describe(null, ox, oy);
    for (let i = 0; i < 8 && el; i++) {
      if (el.shadowRoot && el.shadowRoot.activeElement && el.shadowRoot.activeElement !== el) {
        el = el.shadowRoot.activeElement;
        continue;
      }
      const tag = el.tagName.toLowerCase();
      if ((tag === 'iframe' || tag === 'frame') && el.contentDocument) {
        const r = el.getBoundingClientRect();
        const inner = deepest(el.contentDocument, ox + r.left, oy + r.top, depth + 1);
        if (inner && inner.editable_ready) return inner;
        if (inner && inner.reason && inner.reason !== 'none') return inner;
      }
      break;
    }
    return describe(el, ox, oy);
  }
  return deepest(document, 0, 0, 0);
}
"""

# 只定位，不改焦点。命中栈里第一个可编辑框（含被遮罩盖住的输入框）。
_JS_LOCATE_EDITABLE = """
(xy) => {
  const x = Number(xy && xy.x);
  const y = Number(xy && xy.y);
  const SKIP_TYPE = { hidden: 1, checkbox: 1, radio: 1, button: 1, submit: 1, file: 1, image: 1 };
  function isEditable(el) {
    if (!el || !el.tagName) return false;
    const tag = el.tagName.toLowerCase();
    const typ = (el.getAttribute('type') || '').toLowerCase();
    if (SKIP_TYPE[typ]) return false;
    const role = (el.getAttribute('role') || '').toLowerCase();
    const ce = !!el.isContentEditable;
    const editable = tag === 'input' || tag === 'textarea' || ce || role === 'textbox';
    return editable && !el.disabled && !el.readOnly;
  }
  function fromLabel(el) {
    if (!el || !el.closest) return null;
    const label = el.tagName.toLowerCase() === 'label' ? el : el.closest('label');
    if (!label) return null;
    const id = label.getAttribute('for');
    const ctl = id ? label.ownerDocument.getElementById(id) : label.querySelector('input, textarea, [contenteditable="true"], [role="textbox"]');
    return isEditable(ctl) ? ctl : null;
  }
  function stackAt(doc, px, py) {
    if (!doc || !doc.elementsFromPoint) {
      const one = doc && doc.elementFromPoint ? doc.elementFromPoint(px, py) : null;
      return one ? [one] : [];
    }
    return doc.elementsFromPoint(px, py) || [];
  }
  function search(doc, px, py, depth) {
    if (!doc || depth > 6) return null;
    const stack = stackAt(doc, px, py);
    for (const start of stack) {
      if (isEditable(start)) return start;
      const labeled = fromLabel(start);
      if (labeled) return labeled;
      if (start.shadowRoot) {
        const inner = search(start.shadowRoot, px, py, depth + 1);
        if (inner) return inner;
      }
      const tag = (start.tagName || '').toLowerCase();
      if ((tag === 'iframe' || tag === 'frame') && start.contentDocument) {
        const r = start.getBoundingClientRect();
        const inner = search(start.contentDocument, px - r.left, py - r.top, depth + 1);
        if (inner) return inner;
      }
    }
    return null;
  }
  return search(document, x, y, 0);
}
"""


def evaluate_web_focus(page: Any) -> dict[str, Any]:
    try:
        raw = page.evaluate(_JS_ACTIVE_FOCUS)
    except Exception:
        return {"editable_ready": False, "reason": "evaluate_failed"}
    return dict(raw) if isinstance(raw, dict) else {"editable_ready": False, "reason": "bad_shape"}


def locate_editable_at(page: Any, x: int, y: int) -> Any:
    """返回命中点上的可编辑 ElementHandle。没有则 None。不改页面。"""
    try:
        handle = page.evaluate_handle(_JS_LOCATE_EDITABLE, {"x": int(x), "y": int(y)})
    except Exception:
        return None
    try:
        el = handle.as_element()
    except Exception:
        el = None
    if el is None:
        try:
            handle.dispose()
        except Exception:
            pass
    return el


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
