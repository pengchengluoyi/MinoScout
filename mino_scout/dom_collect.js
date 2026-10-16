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
    const checked = checkable ? (ariaChecked === 'true' ? true : ariaChecked === 'false' ? false : ariaChecked !== null ? null : ['checkbox','radio'].includes(typ) ? !!el.checked : null) : null;
    const enabled = !el.disabled && el.getAttribute('aria-disabled')!=='true' && !el.closest('[inert]');
    const node = {node_ref:ref,snapshot_id:snapshot,resource_id:el.id||'',text,value,sensitive,
      content_desc:name,class:typ?`${tag}:${typ}`:tag,role,clickable,editable,enabled,checkable,checked,
      checked_known:checkable && checked!==null,selected:el.getAttribute('aria-selected')==='true',
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
  // Independent scope facts: compact-tree absence is never proof of form absence.
  const credentialForms = Array.from(document.forms).filter(f =>
    f.querySelector('input[type="email"],input[type="tel"],input[type="password"],input[autocomplete="one-time-code"]') &&
    f.querySelector('button[type="submit"],input[type="submit"],button:not([type])'));
  const form = credentialForms.length === 1 ? credentialForms[0] : null;
  const fr = form?.getBoundingClientRect();
  const opaque = Array.from(document.querySelectorAll('*')).some(e =>
    e.shadowRoot || e.tagName.includes('-') || ['CANVAS','SVG','IFRAME','FRAME','IMG'].includes(e.tagName) ||
    ((!['INPUT','TEXTAREA','SELECT','BUTTON','A','LABEL'].includes(e.tagName)) &&
     (e.onclick != null || getComputedStyle(e).cursor === 'pointer')));
  const formControls = {version:1,snapshot_id:snapshot,credential_forms:credentialForms.length,
    complete:Boolean(form && fr.width>0 && fr.height>0 && fr.left>=0 && fr.top>=0 && fr.right<=innerWidth && fr.bottom<=innerHeight &&
      document.readyState==='complete' && getComputedStyle(form).visibility==='visible' &&
      Array.from(state.elements.values()).some(v => form.contains(v.el) && v.el.matches('input[type="email"],input[type="tel"],input[type="password"],input[autocomplete="one-time-code"]')) && !opaque && !truncated && missing.size===0),
    checkable_count:document.querySelectorAll('input[type="checkbox"],input[type="radio"],[role="checkbox"],[role="radio"],[role="switch"]').length};
  // A visible form/dialog is only a candidate scope. Nexus must independently
  // confirm login purpose and visual absence; these facts never imply consent.
  const isVisible = e => {
    const r=e.getBoundingClientRect();
    for (let p=e;p;p=p.parentElement) {
      const s=getComputedStyle(p);
      if (p.hidden || p.getAttribute('aria-hidden')==='true' || s.display==='none' ||
          ['hidden','collapse'].includes(s.visibility) || Number(s.opacity)===0) return false;
    }
    return r.width>1 && r.height>1;
  };
  const candidates = Array.from(document.querySelectorAll('form,[role="dialog"],dialog[open]')).filter(e =>
    isVisible(e) && Array.from(e.querySelectorAll('input,textarea')).some(n =>
      isVisible(n) && !['hidden','checkbox','radio','button','submit','file'].includes(n.type)) &&
    Array.from(e.querySelectorAll('button,[role="button"],input[type="submit"]')).some(isVisible));
  const scopes=candidates.filter(e => !candidates.some(other => other!==e && e.contains(other)));
  const scope=scopes.length===1?scopes[0]:null, sr=scope?.getBoundingClientRect();
  const descendants=scope?Array.from(scope.querySelectorAll('*')).filter(isVisible):[];
  const full = e => {const r=e.getBoundingClientRect();return r.left>=0 && r.top>=0 &&
    r.right<=innerWidth && r.bottom<=innerHeight && r.left>=sr.left-1 && r.top>=sr.top-1 &&
    r.right<=sr.right+1 && r.bottom<=sr.bottom+1 &&
    e.scrollWidth<=e.clientWidth+1 && e.scrollHeight<=e.clientHeight+1;};
  const occluded=scope && [0.1,0.5,0.9].some(x => [0.1,0.5,0.9].some(y => {
    const top=document.elementFromPoint(sr.left+sr.width*x,sr.top+sr.height*y);
    return !top || !(top===scope || scope.contains(top));
  }));
  const loginScope={version:1,snapshot_id:snapshot,candidate_count:scopes.length,
    bounds:sr?[sr.left,sr.top,sr.right,sr.bottom].map(Math.round):[],
    complete:Boolean(scope && document.readyState==='complete' && full(scope) && !occluded &&
      !truncated && missing.size===0 && descendants.every(full) &&
      !descendants.some(e => e.shadowRoot || e.tagName.includes('-') || ['IFRAME','FRAME','CANVAS'].includes(e.tagName))),
    checkable_count:scope?scope.querySelectorAll('input[type="checkbox"],input[type="radio"],[role="checkbox"],[role="radio"],[role="switch"],[aria-checked]').length:0};
  return {nodes:out,observation:{version:1,snapshot_id:snapshot,scope:'viewport',truncated,
    missing:Array.from(missing),node_count:out.length,node_limit:MAX,source:'playwright',viewport_wh:[innerWidth,innerHeight],form_controls:formControls,login_scope:loginScope}};
}
