// 由 dom_hierarchy.py 注入 page.evaluate。同源 iframe + open shadowRoot。
() => {
  const MAX = 320;
  const MAX_DEPTH = 4;
  const vw = window.innerWidth || 1280;
  const vh = window.innerHeight || 800;

  function pushNode(out, el, rect, meta) {
    if (!rect || rect.width < 2 || rect.height < 2) return;
    if (rect.bottom < 0 || rect.top > vh || rect.right < 0 || rect.left > vw) return;
    const tag = (el.tagName || '').toLowerCase();
    const typ = (el.getAttribute && el.getAttribute('type') || '').toLowerCase();
    const val = (el.value || '').toString();
    const inner = (el.innerText || el.textContent || '').trim().slice(0, 200);
    const text = val || inner;
    const placeholder = el.getAttribute && el.getAttribute('placeholder') || '';
    const aria = el.getAttribute && el.getAttribute('aria-label') || '';
    const role = el.getAttribute && el.getAttribute('role') || '';
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
      bounds: [
        Math.round(rect.left),
        Math.round(rect.top),
        Math.round(rect.right),
        Math.round(rect.bottom),
      ],
      center: [
        Math.round(rect.left + rect.width / 2),
        Math.round(rect.top + rect.height / 2),
      ],
      ...meta,
    });
  }

  const sel = 'input, textarea, select, button, a, [role="button"], [role="link"], [role="textbox"]';

  function walkRoot(root, out, depth, prefix) {
    if (out.length >= MAX || depth > MAX_DEPTH) return;
    let list;
    try {
      list = root.querySelectorAll(sel);
    } catch (e) {
      return;
    }
    for (const el of list) {
      if (out.length >= MAX) break;
      let r;
      try {
        r = el.getBoundingClientRect();
      } catch (e) {
        continue;
      }
      pushNode(out, el, r, { frame_path: prefix || '' });
      if (el.shadowRoot) {
        walkRoot(el.shadowRoot, out, depth + 1, prefix);
      }
    }
    if (depth >= MAX_DEPTH) return;
    let iframes;
    try {
      iframes = root.querySelectorAll('iframe, frame');
    } catch (e) {
      return;
    }
    for (const frameEl of iframes) {
      if (out.length >= MAX) break;
      let fr;
      let r;
      try {
        r = frameEl.getBoundingClientRect();
      } catch (e) {
        continue;
      }
      const childPrefix = prefix ? `${prefix}>` : '';
      const iframeSel = frameEl.id ? `#${frameEl.id}` : 'iframe';
      const path = `${childPrefix}${iframeSel}`;
      try {
        fr = frameEl.contentDocument;
      } catch (e) {
        fr = null;
      }
      if (!fr) {
        pushNode(out, frameEl, r, {
          frame_path: path,
          cross_origin: true,
          class: 'iframe:cross-origin',
          text: frameEl.getAttribute('title') || '',
          content_desc: frameEl.getAttribute('src') || '',
          clickable: false,
          editable: false,
        });
        continue;
      }
      walkRoot(fr, out, depth + 1, path);
    }
  }

  const out = [];
  walkRoot(document, out, 0, '');
  return out;
}
