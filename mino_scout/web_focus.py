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

# 只定位，不改焦点。先看命中栈，再在本文档里找包含该点或离该点最近的可见输入框。
# iframe 由 Python 按帧再调一次，避免跨 frame 的 ElementHandle 丢失。
_JS_LOCATE_EDITABLE = """
(xy) => {
  const x = Number(xy && xy.x);
  const y = Number(xy && xy.y);
  const field = String((xy && xy.field) || '').toLowerCase();
  const mode = String((xy && xy.mode) || 'all');
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
  function visible(el) {
    if (!isEditable(el)) return false;
    const r = el.getBoundingClientRect();
    if (!r || r.width < 4 || r.height < 4) return false;
    const win = el.ownerDocument && el.ownerDocument.defaultView;
    if (!win) return true;
    const st = win.getComputedStyle(el);
    if (!st || st.visibility === 'hidden' || st.display === 'none') return false;
    return true;
  }
  function fromLabel(el) {
    if (!el || !el.closest) return null;
    const label = el.tagName.toLowerCase() === 'label' ? el : el.closest('label');
    if (!label) return null;
    const id = label.getAttribute('for');
    const ctl = id ? label.ownerDocument.getElementById(id) : label.querySelector('input, textarea, [contenteditable="true"], [role="textbox"]');
    return visible(ctl) ? ctl : null;
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
      if (visible(start)) return start;
      const labeled = fromLabel(start);
      if (labeled) return labeled;
      if (start.shadowRoot) {
        const inner = search(start.shadowRoot, px, py, depth + 1);
        if (inner) return inner;
      }
    }
    return null;
  }
  function walk(root, acc, depth) {
    if (!root || !root.querySelectorAll || depth > 8) return;
    const nodes = root.querySelectorAll('input, textarea, [contenteditable="true"], [contenteditable=""], [role="textbox"]');
    for (const el of nodes) {
      if (visible(el)) acc.push(el);
      if (el.shadowRoot) walk(el.shadowRoot, acc, depth + 1);
    }
    const hosts = root.querySelectorAll('*');
    for (const el of hosts) {
      if (el.shadowRoot) walk(el.shadowRoot, acc, depth + 1);
    }
  }
  function fieldBoost(el) {
    if (field !== 'email' && field !== 'login_email' && field !== 'phone' && field !== 'sms_code') return 0;
    const typ = (el.getAttribute('type') || '').toLowerCase();
    const blob = [
      el.getAttribute('name') || '',
      el.getAttribute('id') || '',
      el.getAttribute('autocomplete') || '',
      el.getAttribute('placeholder') || '',
      el.getAttribute('aria-label') || '',
    ].join(' ').toLowerCase();
    if ((field === 'email' || field === 'login_email') && (typ === 'email' || /email|e-mail|username/.test(blob))) return 500;
    if (field === 'phone' && (typ === 'tel' || /phone|mobile|tel/.test(blob))) return 500;
    if (field === 'sms_code' && (typ === 'tel' || typ === 'number' || /otp|code|one-time|digit/.test(blob))) return 500;
    return 0;
  }
  function pick(list) {
    let best = null;
    let bestScore = 1e12;
    for (const el of list) {
      const r = el.getBoundingClientRect();
      const pad = 20;
      const inside = x >= r.left - pad && x <= r.right + pad && y >= r.top - pad && y <= r.bottom + pad;
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      const dist = Math.abs(cx - x) + Math.abs(cy - y);
      if (!inside && dist > 240) continue;
      const score = (inside ? dist : 1000 + dist) - fieldBoost(el);
      if (score < bestScore) {
        bestScore = score;
        best = el;
      }
    }
    return best;
  }
  const direct = search(document, x, y, 0);
  if (direct) return direct;
  if (mode === 'hit') return null;
  const all = [];
  walk(document, all, 0);
  return pick(all);
}
"""


def evaluate_web_focus(page: Any) -> dict[str, Any]:
    try:
        raw = page.evaluate(_JS_ACTIVE_FOCUS)
    except Exception:
        return {"editable_ready": False, "reason": "evaluate_failed"}
    return dict(raw) if isinstance(raw, dict) else {"editable_ready": False, "reason": "bad_shape"}


def _locate_in_frame(frame: Any, x: int, y: int, field: str, mode: str) -> Any:
    try:
        handle = frame.evaluate_handle(
            _JS_LOCATE_EDITABLE,
            {"x": int(x), "y": int(y), "field": str(field or ""), "mode": str(mode or "hit")},
        )
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


def _frame_local(frame: Any, px: int, py: int) -> tuple[int, int] | None:
    """点落在该 frame 内时返回 frame 内坐标。"""
    try:
        host = frame.frame_element()
        box = host.bounding_box() if host is not None else None
    except Exception:
        return None
    if not box:
        return None
    lx = px - int(box.get("x") or 0)
    ly = py - int(box.get("y") or 0)
    bw = float(box.get("width") or 0)
    bh = float(box.get("height") or 0)
    if lx < 0 or ly < 0 or lx > bw or ly > bh:
        return None
    return lx, ly


def locate_editable_at(page: Any, x: int, y: int, field: str = "") -> Any:
    """先在落点所在帧里命中输入框，再在同一帧里找最近的可见输入框。不改页面。"""
    px, py = int(x), int(y)
    hint = str(field or "")
    main = getattr(page, "main_frame", None) or page
    hit = _locate_in_frame(main, px, py, hint, "hit")
    if hit is not None:
        return hit
    frames = list(getattr(page, "frames", []) or [])
    containing: list[tuple[Any, int, int]] = []
    for frame in frames:
        if frame is main:
            continue
        local = _frame_local(frame, px, py)
        if local is None:
            continue
        hit = _locate_in_frame(frame, local[0], local[1], hint, "hit")
        if hit is not None:
            return hit
        containing.append((frame, local[0], local[1]))
    near_targets = containing or [(main, px, py)]
    for frame, lx, ly in near_targets:
        hit = _locate_in_frame(frame, lx, ly, hint, "near")
        if hit is not None:
            return hit
    return None


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
