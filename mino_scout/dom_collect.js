// Compact current-viewport observation. DOM handles stay inside this page; no HTML is transmitted.
() => {
  const MAX = 320, MAX_DEPTH = 4;
  const snapshot = `dom:${Date.now()}:${Math.random().toString(36).slice(2)}`;
  const state = { snapshot, elements: new Map() };
  window.__minoObservation = state;
  const out = [], seen = new Set(), missing = new Set();
  let truncated = false;
  const viewport = {left: 0, top: 0, right: innerWidth, bottom: innerHeight};
  const interactive = 'input,textarea,select,button,a,[role="button"],[role="link"],[role="textbox"],[role="checkbox"],[role="radio"],[role="tab"],[role="switch"],[role="menuitem"],[contenteditable="true"],[contenteditable=""]';
  const semantic = 'h1,h2,h3,h4,label,p,li,dt,dd,td,th,output,[role="heading"],[role="alert"],[role="status"],[role="dialog"],[aria-live],span,div';
  const clean = (s, n=200) => String(s || '').replace(/\s+/g,' ').trim().slice(0,n);
  function add(el, offset, clip, path, cross=false) {
    if (seen.has(el)) return;
    seen.add(el);
    const doc = el.ownerDocument, win = doc.defaultView;
    const style = win.getComputedStyle(el), r = el.getBoundingClientRect();
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse' || Number(style.opacity) === 0 || el.hidden || el.getAttribute('aria-hidden') === 'true') return;
    const bounds = [r.left+offset.x,r.top+offset.y,r.right+offset.x,r.bottom+offset.y];
    if (r.width < 2 || r.height < 2 || bounds[2]<=clip.left || bounds[0]>=clip.right || bounds[3]<=clip.top || bounds[1]>=clip.bottom) return;
    const tag = el.tagName.toLowerCase(), typ = (el.getAttribute('type') || '').toLowerCase();
    const sensitive = typ === 'password';
    const role = el.getAttribute('role') || ({button:'button',a:'link',select:'combobox',textarea:'textbox',input:typ==='checkbox'?'checkbox':typ==='radio'?'radio':'textbox',h1:'heading',h2:'heading',h3:'heading',p:'paragraph',label:'label'}[tag] || 'text');
    const editable = !cross && (tag==='textarea' || tag==='input' && !['checkbox','radio','button','submit','hidden'].includes(typ) || el.isContentEditable);
    const clickable = !cross && (['button','a','select'].includes(tag) || ['button','link','tab','menuitem','checkbox','radio','switch'].includes(role) || el.onclick != null || style.cursor==='pointer');
    const directText = Array.from(el.childNodes).filter(n=>n.nodeType===3).map(n=>n.textContent).join(' ');
    const value = sensitive ? '' : clean(el.value);
    const text = sensitive ? '' : value || clean(clickable || editable || ['heading','alert','status','label','paragraph'].includes(role) ? el.innerText || el.textContent : directText);
    const labels = el.labels ? Array.from(el.labels).map(l=>clean(l.innerText)).join(' ') : '';
    const described = clean((el.getAttribute('aria-labelledby')||'').split(/\s+/).map(id=>doc.getElementById(id)?.textContent||'').join(' '));
    const name = clean(el.getAttribute('aria-label') || described || labels || el.getAttribute('placeholder') || el.getAttribute('title'));
    if (!text && !name && !clickable && !editable && !cross) return;
    if (out.length >= MAX) { truncated = true; return; }
    const ref = `${snapshot}:${out.length}`;
    const checkable = ['checkbox','radio','switch'].includes(role) || ['checkbox','radio'].includes(typ);
    const ariaChecked = el.getAttribute('aria-checked');
    const checked = checkable ? ariaChecked ? ariaChecked==='mixed'?null:ariaChecked==='true' : !!el.checked : false;
    const enabled = !el.disabled && el.getAttribute('aria-disabled')!=='true' && !el.closest('[inert]');
    const node = {node_ref:ref,snapshot_id:snapshot,resource_id:el.id||'',text,value,sensitive,
      content_desc:name,class:typ?`${tag}:${typ}`:tag,role,clickable,editable,enabled,checkable,checked,
      checked_known:!checkable || checked!==null,selected:el.getAttribute('aria-selected')==='true',
      focused:doc.activeElement===el,visible:true,bounds:bounds.map(Math.round),
      center:[Math.round((bounds[0]+bounds[2])/2),Math.round((bounds[1]+bounds[3])/2)],frame_path:path,cross_origin:cross};
    if (!cross) state.elements.set(ref, {el,bounds:node.bounds,tag,type:typ,name});
    out.push(node);
  }
  function walk(root, depth, offset, clip, path) {
    if (depth > MAX_DEPTH) { truncated=true; missing.add('depth_limit'); return; }
    for (const selector of [interactive,semantic]) {
      for (const el of root.querySelectorAll(selector)) add(el,offset,clip,path);
    }
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) walk(el.shadowRoot,depth+1,offset,clip,path);
      if (!['IFRAME','FRAME'].includes(el.tagName)) continue;
      const r=el.getBoundingClientRect();
      const x=offset.x+r.left+el.clientLeft,y=offset.y+r.top+el.clientTop;
      const childClip={left:Math.max(clip.left,x),top:Math.max(clip.top,y),right:Math.min(clip.right,x+el.clientWidth),bottom:Math.min(clip.bottom,y+el.clientHeight)};
      if (childClip.right<=childClip.left || childClip.bottom<=childClip.top) continue;
      const index=Array.from(root.querySelectorAll('iframe,frame')).indexOf(el);
      const childPath=path?`${path}>frame[${index}]`:`frame[${index}]`;
      let doc; try { doc=el.contentDocument; } catch (_) { doc=null; }
      if (!doc) { missing.add('cross_origin_frame'); add(el,offset,clip,childPath,true); }
      else walk(doc,depth+1,{x,y},childClip,childPath);
    }
  }
  walk(document,0,{x:0,y:0},viewport,'');
  return {nodes:out,observation:{version:1,snapshot_id:snapshot,scope:'viewport',truncated,
    missing:Array.from(missing),node_count:out.length,node_limit:MAX,source:'playwright'}};
}
