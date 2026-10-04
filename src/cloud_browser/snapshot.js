(() => {
  const options = globalThis.__cbOptions || {};
  const nodeBudget = options.lightweight ? 60 : 300;
  const scanBudget = options.lightweight ? 1000 : 10000;
  const textBudget = options.lightweight ? 8000 : 250000;
  const query = options.query || {};
  const exactTarget = options.exact_target ? globalThis.__cbExactElement : null;
  // Executed in a CDP isolated world. Never invoke page handlers while observing.
  if (!globalThis.__cloudBrowserState) {
    const state = {mutations: 0, privacyEpoch:0, roots: new WeakSet(),
      protectedNodes:new WeakSet(), protectedAncestors:new WeakSet(), labelNodes:new WeakSet(),
      safeOffscreenFrames:new WeakSet(), safeFrames:new WeakSet()};
    const positionalFrameStyle = record => {
      const e=record.target;
      if(record.attributeName!=='style' || !state.safeFrames.has(e))return false;
      const old=document.createElement('span').style;
      old.cssText=record.oldValue||'';
      const keys=new Set([...old,...e.style]);
      for(const key of keys) {
        if(old.getPropertyValue(key)===e.style.getPropertyValue(key) &&
           old.getPropertyPriority(key)===e.style.getPropertyPriority(key))continue;
        if(!['left','right','top','bottom'].includes(key))return false;
        for(const value of [old.getPropertyValue(key),e.style.getPropertyValue(key)])
          if(value && !/^(auto|-?\d+(\.\d+)?px)$/.test(value))return false;
      }
      return true;
    };
    const risky = node => {
      const pending=[node];
      for(let n=0;pending.length && n<128;n++) {
        const e=pending.pop();
        if(e.nodeType!==Node.ELEMENT_NODE)continue;
        if(e.shadowRoot || state.protectedNodes.has(e) ||
          e.matches('input,textarea,select,form,label,iframe,frame,style,link,[contenteditable]'))return true;
        for(let i=0;i<e.children.length;i++) {
          if(pending.length>=128)return true;
          pending.push(e.children[i]);
        }
      }
      return pending.length>0;
    };
    state.observer = new MutationObserver(records => {
      state.mutations++;
      let tainted=records.length>1000;
      for(const record of records.slice(0,1000)) {
        const target=record.target.nodeType===Node.ELEMENT_NODE?record.target:record.target.parentElement;
        if(target && (state.protectedNodes.has(target) || state.labelNodes.has(target) ||
          target.matches('label,style')))tainted=true;
        const safeFrameStyle=target && record.attributeName==='style' &&
          (state.safeOffscreenFrames.has(target) || positionalFrameStyle(record));
        if(record.type==='attributes' && target && !safeFrameStyle &&
          (state.protectedAncestors.has(target) ||
           target.matches('input,textarea,select,form,iframe,frame,link,[contenteditable]')))tainted=true;
        if(record.type==='childList') {
          if(record.addedNodes.length+record.removedNodes.length>128)tainted=true;
          else for(const node of [...record.addedNodes,...record.removedNodes])if(risky(node))tainted=true;
        }
      }
      if(tainted)state.privacyEpoch++;
    });
    state.observer.observe(document, {subtree:true, childList:true, attributes:true, attributeOldValue:true, characterData:true});
    globalThis.__cloudBrowserState = state;
  }
  const visible = e => {
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' &&
      s.visibility !== 'collapse' && s.display !== 'none' && s.contentVisibility !== 'hidden';
  };
  const inViewport = e => {
    const r = e.getBoundingClientRect();
    return r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
  };
  const normalize = s => String(s || '').replace(/\s+/g, ' ').trim();
  globalThis.__cloudBrowserState.labelNodes=new WeakSet();
  const labelProbe = root => {
    if (!root) return {text:'',incomplete:false};
    const pending=[root], out=[];
    let chars=0, incomplete=false;
    for(let visited=0;pending.length && visited<32 && chars<1024;visited++) {
      const node=pending.pop();
      globalThis.__cloudBrowserState.labelNodes.add(node);
      if(node.nodeType===Node.TEXT_NODE) {
        const value=node.substringData(0,1024-chars);
        if(value.length<node.length)incomplete=true;
        out.push(value);chars+=value.length;
      } else if(node.nodeType===Node.ELEMENT_NODE &&
          !node.matches('input,textarea,select,[contenteditable],script,style,template')) {
        if(node.childNodes.length>32)incomplete=true;
        for(let i=Math.min(node.childNodes.length,32)-1;i>=0;i--)pending.push(node.childNodes[i]);
      }
    }
    return {text:out.join(' '),incomplete:incomplete||pending.length>0};
  };
  const sensitive = e => {
    const labels=[], connected=e.labels||[];
    const aria=e.getAttribute('aria-labelledby')||'';
    if(connected.length>4 || aria.length>2048)return true;
    for(let i=0;i<connected.length;i++)labels.push(labelProbe(connected[i]));
    const ids=aria.trim().split(/\s+/);
    if(ids.length>8)return true;
    const labelledBy=ids
      .map(id=>labelProbe(e.getRootNode().getElementById?.(id)||document.getElementById(id)));
    if([...labels,...labelledBy].some(label=>label.incomplete))return true;
    return /password|passwd|one.?time|otp|auth.?code|verification.?code|auth.?token|access.?token|refresh.?token|cc-number|cc-csc|credit.?card|card.?number|secret|api.?key|security.?answer|비밀번호|인증\s*(번호|코드)|일회용\s*(번호|코드)|카드\s*번호|보안\s*답변/i.test(normalize(
      [e.type,e.name,e.id,e.autocomplete,e.getAttribute('aria-label'),
       ...labels.map(label=>label.text),...labelledBy.map(label=>label.text)].join(' ')));
  };
  const parentOf = e => e.assignedSlot || e.parentElement || e.getRootNode()?.host || null;
  const ancestor = (e, test) => {
    for (let p=e, depth=0; p && depth < 20000; p=parentOf(p), depth++) if (test(p)) return p;
    return null;
  };
  // Scan attributes before reading any text or values. Hidden controls are also
  // protected: visibility is not permission to expose a secret or form digest.
  // Fixed security budgets do not shrink when the output is memory-limited.
  const privacyBudget = 20000, inputBudget = 1000;
  const inspected = [], protectedRoots = new Set(), stack = [document.documentElement];
  const seen = new Set();
  let inputCount = 0, privacyIncomplete = false;
  while (stack.length) {
    const e = stack.pop();
    if (!e || seen.has(e)) continue;
    if (inspected.length >= privacyBudget) { privacyIncomplete = true; break; }
    seen.add(e); inspected.push(e);
    if (e.matches('input,textarea,select,[contenteditable]')) {
      if (++inputCount > inputBudget) { privacyIncomplete = true; break; }
      if (sensitive(e)) protectedRoots.add(e.form || ancestor(e, p => p.tagName === 'FORM') || e);
    }
    if (e.shadowRoot) {
      if (!globalThis.__cloudBrowserState.roots.has(e.shadowRoot)) {
        globalThis.__cloudBrowserState.privacyEpoch++;
        globalThis.__cloudBrowserState.observer.observe(e.shadowRoot,
          {subtree:true,childList:true,attributes:true,attributeOldValue:true,characterData:true});
        globalThis.__cloudBrowserState.roots.add(e.shadowRoot);
      }
      for (let i=e.shadowRoot.children.length-1; i>=0; i--) stack.push(e.shadowRoot.children[i]);
    }
    for (let i=e.children.length-1; i>=0; i--) stack.push(e.children[i]);
  }
  const protectionCache = new WeakMap();
  const protectionRoot = e => {
    if (!e || !protectedRoots.size) return null;
    if (e.form && protectedRoots.has(e.form)) return e.form;
    const path=[];
    let p=e, result=null;
    for (let depth=0;p && depth<privacyBudget;p=parentOf(p),depth++) {
      if (protectedRoots.has(p)) { result=p;break; }
      if (protectionCache.has(p)) { result=protectionCache.get(p);break; }
      path.push(p);
    }
    for (const item of path) protectionCache.set(item,result);
    return result;
  };
  const protectedElement = e => privacyIncomplete || !!protectionRoot(e);
  globalThis.__cloudBrowserState.protectedNodes=new WeakSet();
  globalThis.__cloudBrowserState.protectedAncestors=new WeakSet();
  for(const e of inspected)if(protectionRoot(e))globalThis.__cloudBrowserState.protectedNodes.add(e);
  for(const root of protectedRoots)for(let e=root;e;e=parentOf(e)) {
    if(globalThis.__cloudBrowserState.protectedAncestors.has(e))break;
    globalThis.__cloudBrowserState.protectedAncestors.add(e);
  }
  const composedChildren = e => e.tagName === 'SLOT' && e.assignedNodes().length ? e.assignedNodes() :
    e.shadowRoot ? e.shadowRoot.childNodes : e.childNodes;
  const hidden = e => {
    const s = getComputedStyle(e);
    return s.display === 'none' || s.visibility === 'hidden' || s.visibility === 'collapse' ||
      s.contentVisibility === 'hidden' || e.getAttribute('aria-hidden') === 'true';
  };
  const rendered = e => {
    if (!e || hidden(e)) return false;
    if (visible(e)) return true;
    // display:contents and zero-height wrappers can still contain rendered text.
    const pending = [...composedChildren(e)], visited = new Set();
    for (let n=0; pending.length && n<scanBudget; n++) {
      const child = pending.pop();
      if (!child || visited.has(child)) continue;
      visited.add(child);
      if (child.nodeType !== Node.ELEMENT_NODE || hidden(child) || protectedElement(child)) continue;
      if (visible(child)) return true;
      pending.push(...composedChildren(child));
    }
    return false;
  };
  const protectedRegions = [];
  let maskInspectionIncomplete=false, geometryCount=0;
  const regionBounds = new Map(), geometrySafety = new WeakMap();
  const maskSafe = target => {
    const path=[];
    let safe=true;
    for (let p=target; p; p=parentOf(p)) {
      if (geometrySafety.has(p)) {safe=geometrySafety.get(p);break;}
      path.push(p);
        const s=getComputedStyle(p);
        // Generated content may paint outside the element/text rectangles.
        // Do not read/return its text or trust its placement as a mask boundary.
        for(const pseudo of ['::before','::after']) {
          const generated=getComputedStyle(p,pseudo).content;
          if(generated && !['none','normal','""',"''"].includes(generated))safe=false;
        }
        if (s.transform !== 'none' || s.filter !== 'none' || s.perspective !== 'none' ||
            s.boxShadow !== 'none' || s.textShadow !== 'none' ||
            (s.backdropFilter && s.backdropFilter !== 'none') ||
            (s.webkitBoxReflect && s.webkitBoxReflect !== 'none') || s.mixBlendMode !== 'normal') safe=false;
        if(!safe)break;
    }
    for(const item of path)geometrySafety.set(item,safe);
    return safe;
  };
  // One bounded pass, rather than rescanning the document for every password form.
  for(const target of inspected) {
    const owner=protectionRoot(target);
    if(!owner)continue;
    const safe=maskSafe(target);
    if(!safe)maskInspectionIncomplete=true;
    const rects=[];
    if(visible(target))rects.push(target.getBoundingClientRect());
    // display:contents forms/editables and direct form text have no own box.
    // Read geometry only, never the protected text or a control value.
    for(const child of target.childNodes) {
      if(child.nodeType!==Node.TEXT_NODE)continue;
      if(++geometryCount>10000){maskInspectionIncomplete=true;break;}
      const range=document.createRange();range.selectNode(child);
      const boxes=range.getClientRects();
      if(boxes.length>100){maskInspectionIncomplete=true;break;}
      for(const rect of boxes)if(rect.width>0&&rect.height>0)rects.push(rect);
    }
    if(!rects.length)continue;
    const bound=regionBounds.get(owner)||{left:Infinity,top:Infinity,right:-Infinity,bottom:-Infinity,safe:true};
    for(const r of rects) {
      bound.left=Math.min(bound.left,r.left);bound.top=Math.min(bound.top,r.top);
      bound.right=Math.max(bound.right,r.right);bound.bottom=Math.max(bound.bottom,r.bottom);
    }
    bound.safe=bound.safe && safe;
    regionBounds.set(owner,bound);
  }
  for(const [owner,bound] of regionBounds) {
    protectedRegions.push({x:bound.left-2,y:bound.top-2,width:bound.right-bound.left+4,
      height:bound.bottom-bound.top+4,mask_safe:bound.safe,tag:owner.tagName.toLowerCase()});
  }
  const protectedPage = privacyIncomplete;
  let scope = document, scopeMatches = null, selected = null;
  try {
    if (query.scope) {
      scopeMatches=inspected.filter(e => e.matches(query.scope));
      scope=scopeMatches.find(e => !protectedElement(e) && rendered(e)) || scopeMatches[0] || null;
    }
    if (query.selector) selected = inspected.filter(e => e.matches(query.selector) &&
      (scope === document || (scope && !!ancestor(e, p => p === scope))));
  } catch (_) { return {data:{query_error:true},elements:[],frame_elements:[]}; }
  // Child documents are inspected separately through their CDP contexts. Never
  // read embedded credentials as part of their parent's text or AX snapshot.
  // Read native controls without constructing FormData (which fires page events).
  // Values are internal CDP data only; Python replaces them with a digest immediately.
  let formStateComplete = true, formChars = 0;
  const formStates = [];
  if (!protectedPage) {
    const controls = inspected.filter(e => e.matches('input,textarea,select') && !protectedElement(e));
    if (controls.length > 1000) formStateComplete = false;
    for (const e of [...controls].slice(0, 1000)) {
      const values = e.tagName === 'SELECT' ? [...e.selectedOptions].map(o => o.value) : [e.value];
      const entry = [e.name, e.type, !!e.disabled, !!e.checked, values,
        e.files ? [...e.files].map(f => [f.name, f.size, f.lastModified]) : []];
      formChars += JSON.stringify(entry).length;
      if (formChars > 250000) { formStateComplete = false; break; }
      formStates.push(entry);
    }
  }
  const scrollable = e => {
    if (e === document.body || e === document.documentElement) return false;
    const s = getComputedStyle(e);
    return (/auto|scroll/.test(s.overflowY) && e.scrollHeight > e.clientHeight) ||
      (/auto|scroll/.test(s.overflowX) && e.scrollWidth > e.clientWidth);
  };
  const accessibleName = e => {
    if (!e || protectedElement(e)) return '';
    const safeText = root => {
      if (!root || protectedElement(root)) return '';
      const pending=[root], visited=new Set(), out=[];
      let chars=0;
      for (let n=0; pending.length && n<scanBudget && chars<2000; n++) {
        const child=pending.pop();
        if (!child || visited.has(child)) continue;
        visited.add(child);
        if (child.nodeType === Node.TEXT_NODE) { const text=child.substringData(0,2000-chars);out.push(text);chars+=text.length;continue; }
        if (child.nodeType !== Node.ELEMENT_NODE || hidden(child) || protectedElement(child) ||
            /^(SCRIPT|STYLE|TEMPLATE|NOSCRIPT|INPUT|TEXTAREA|SELECT|IFRAME|FRAME)$/.test(child.tagName)) continue;
        const children=composedChildren(child);
        for (let i=children.length-1;i>=0;i--) pending.push(children[i]);
      }
      return out.join(' ');
    };
    const label = [...(e.labels || [])].slice(0, 4).map(safeText).join(' ');
    const labelledBy = (e.getAttribute('aria-labelledby') || '').split(/\s+/).slice(0, 16)
      .map(id => safeText(e.getRootNode().getElementById?.(id) || document.getElementById(id))).join(' ');
    const image = e.querySelector('img[alt]');
    return normalize(normalize(labelledBy) || e.getAttribute('aria-label') || label || safeText(e) ||
      (e.tagName === 'IMG' ? e.alt : '') || (image && !protectedElement(image) ? image.alt : '') ||
      e.getAttribute('title') || e.placeholder || '').slice(0, 500);
  };
  // Bounded native-role fallback also works before the first AX-enriched observation.
  const roleOf = e => {
    const explicit = normalize(e.getAttribute('role'));
    if (explicit) return explicit.split(' ')[0].toLowerCase();
    const tag = e.tagName;
    if (/^H[1-6]$/.test(tag)) return 'heading';
    if (tag === 'INPUT') {
      if (e.hasAttribute('list') && ['text','search','email','url','tel'].includes(e.type)) return 'combobox';
      return ({search:'searchbox',checkbox:'checkbox',radio:'radio',range:'slider',number:'spinbutton',
        button:'button',submit:'button',reset:'button',image:'button',hidden:null})[e.type] ??
        (['text','email','url','tel','password'].includes(e.type) ? 'textbox' : null);
    }
    if (tag === 'SELECT') return e.multiple || e.size > 1 ? 'listbox' : 'combobox';
    if (tag === 'A') return e.hasAttribute('href') ? 'link' : null;
    return ({BUTTON:'button',SUMMARY:'button',TEXTAREA:'textbox',IMG:'img',OPTION:'option',
      NAV:'navigation',MAIN:'main',ASIDE:'complementary',DIALOG:'dialog',ARTICLE:'article',
      UL:'list',OL:'list',LI:'listitem',TABLE:'table',TR:'row',TD:'cell',TH:'columnheader',
      PROGRESS:'progressbar',METER:'meter',SEARCH:'search'})[tag] || (e.isContentEditable ? 'textbox' : null);
  };
  const targeted = !!(query.selector || query.role || query.name || query.label);
  const roleSelectors = {heading:'h1,h2,h3,h4,h5,h6',button:'button,summary,input',
    textbox:'input,textarea,[contenteditable=true]',searchbox:'input',combobox:'input,select',
    listbox:'select',checkbox:'input',radio:'input',link:'a[href]',img:'img',
    navigation:'nav',main:'main',dialog:'dialog',list:'ul,ol',listitem:'li',table:'table',
    row:'tr',cell:'td',columnheader:'th',option:'option',search:'search'};
  const controlSelector = 'a,button,summary,input,textarea,select,[role=button],[role=checkbox],[role=tab],[role=link],[tabindex],[contenteditable=true]';
  const all = targeted ? [] : inspected.filter(e => e.matches(controlSelector) &&
    visible(e) && inViewport(e) && !protectedElement(e));
  // Include ordinary div-based scroll panes, without an unbounded layout scan.
  const candidates = targeted ? [] : inspected;
  const known = new Set(all);
  for (let i = 0; !targeted && i < Math.min(candidates.length, scanBudget); i++) {
    const e = candidates[i];
    if (!known.has(e) && !protectedElement(e) && visible(e) && inViewport(e) && scrollable(e)) { all.push(e); known.add(e); }
  }
  // Scoped CSS queries are read-only and may include non-interactive DOM targets.
  // The whole document's privacy guard remains in force.
  const pool = exactTarget ? [exactTarget] : selected || (scope ? (targeted ? inspected.filter(e =>
    (scope === document || !!ancestor(e, p => p === scope)) && e.matches(
      query.role ? (roleSelectors[normalize(query.role).toLowerCase()] || '*') + ',[role]' : '*'
    )) : all.filter(e => scope === document || !!ancestor(e, p => p === scope))) : []);
  const queryScanTruncated = targeted && pool.length > scanBudget;
  const queried = [];
  for (let i = 0; i < Math.min(pool.length, exactTarget ? 1 : targeted ? scanBudget : pool.length); i++) {
    const e = pool[i];
    if (exactTarget) { queried.push(e); continue; }
    if (protectedElement(e)) continue;
    if (targeted && !query.selector && !roleOf(e) && !e.hasAttribute('aria-label') && !e.hasAttribute('aria-labelledby')) continue;
    if (query.visibility !== 'all' && !(query.selector ? rendered(e) : visible(e))) continue;
    if (!query.visibility && !query.selector && !inViewport(e)) continue;
    if (query.enabled_only && (e.matches(':disabled') || e.getAttribute('aria-disabled') === 'true')) continue;
    if (query.role && roleOf(e) !== normalize(query.role).toLowerCase()) continue;
    if ((query.name || query.label) && ![query.name, query.label].filter(Boolean).every(
      text => accessibleName(e).toLowerCase().includes(normalize(text).toLowerCase()))) continue;
    queried.push(e);
  }
  // Apply the result limit AFTER the scoped role/name match, not before it.
  const elements = queried.slice(0, Math.min(nodeBudget,query.limit || nodeBudget));
  const formIds = new Map(), ownForms = [];
  const formId = form => {
    if (!form || protectedElement(form) || protectedPage || !formStateComplete) return null;
    if (!formIds.has(form)) {
      formIds.set(form, ownForms.length);
      ownForms.push([...form.elements].filter(c => !protectedElement(c)).slice(0,1000).map(c =>
        [c.name,c.type,c.disabled,c.checked,c.type === 'file' ? [...c.files].map(f => [f.name,f.size,f.lastModified]) : String(c.value || '')]));
    }
    return formIds.get(form);
  };
  const nodes = elements.map(e => {
    const r = e.getBoundingClientRect();
    if (protectedElement(e)) return {tag:e.tagName.toLowerCase(),type:e.type || '',protected:true,
      visible:visible(e),in_viewport:inViewport(e),rect:{x:r.x,y:r.y,width:r.width,height:r.height},
      name:'',role:null,value:null,_own_value:null,_form_index:null,form_fields:[],href:null};
    const form = e.form;
    const submit = form && ((e.tagName === 'BUTTON' && e.type === 'submit') ||
      (e.tagName === 'INPUT' && ['submit','image'].includes(e.type)));
    // Policy hints are observed structure, never authority supplied by page text.
    // No claim that arbitrary page handlers cannot have additional side effects.
    const controls = normalize(e.getAttribute('aria-controls')).split(' ').filter(Boolean);
    const controlled = controls.length > 0 && controls.length <= 4 &&
      controls.every(id => {
        const target = document.getElementById(id);
        return target && !target.isContentEditable && !/^(INPUT|TEXTAREA|SELECT|BUTTON|A)$/.test(target.tagName);
      });
    const role = e.getAttribute('role');
    const viewControl = !submit && (
      (e.tagName === 'SUMMARY' && e.parentElement?.tagName === 'DETAILS') ||
      (controlled && ['true','false'].includes(e.getAttribute('aria-expanded')))
    ) ? 'disclosure' : !submit && controlled && role === 'tab' &&
      controls.every(id => document.getElementById(id).getAttribute('role') === 'tabpanel') ? 'tab' : null;
    const popupRole = e.getAttribute('aria-haspopup');
    const popupTarget = e.getAttribute('popovertarget') && document.getElementById(e.getAttribute('popovertarget'));
    const popupView = !form && !e.href && e.tagName === 'BUTTON' && (
      (popupTarget && popupTarget.hasAttribute('popover')) ||
      (controlled && ['menu','dialog','listbox'].includes(popupRole) && controls.every(
        id => document.getElementById(id).getAttribute('role') === popupRole))
    );
    const toolGroup = e.closest('[role=toolbar],[role=radiogroup],fieldset');
    const shortcut = normalize(e.getAttribute('aria-keyshortcuts'));
    const helpShortcut = /^(\?|F1)$/i.test(shortcut) || /[—-]\s*(\?|F1)\s*$/i.test(e.title);
    const toolState = (e.tagName === 'BUTTON' && ['true','false'].includes(e.getAttribute('aria-pressed'))) ||
      (e.tagName === 'INPUT' && e.type === 'radio');
    let canvasContext = false;
    for (let parent=e.parentElement, depth=0; !form && !submit && !e.href && (toolState || helpShortcut) && parent && depth<8; parent=parent.parentElement, depth++) {
      const canvas = parent.querySelector('canvas');
      if (canvas && visible(canvas) && parent.querySelectorAll('button[aria-pressed],input[type=radio]').length >= 2) {
        canvasContext = true; break;
      }
    }
    const localUi = !form && !e.href && !submit && canvasContext ? (
      toolState && (toolGroup || shortcut) ? 'editor_tool' :
      e.tagName === 'BUTTON' && helpShortcut ? 'editor_help' : null
    ) : null;
    // Context hints are server-observed evidence, not proof that arbitrary JS is harmless.
    const contextGroup = toolGroup || e.closest('[role=dialog],[role=group],[role=application],section[aria-label]');
    const uiContext = contextGroup && !protectedElement(contextGroup) ? normalize(contextGroup.getAttribute('aria-label') ||
      (contextGroup.getAttribute('aria-labelledby') || '').split(/\s+/).slice(0,4)
        .map(id=>{const target=document.getElementById(id);return target && !protectedElement(target) ? accessibleName(target) : '';}).join(' ') ||
      accessibleName(contextGroup.querySelector('legend'))) : '';
    const submitters = form && form.elements.length <= 100 ? [...form.elements].filter(c =>
      c.type === 'submit' || c.type === 'image') : [];
    // Enter can activate the default submitter, including its method/action override.
    // Ambiguous or overridden submission targets are not automatic search forms.
    const searchForm = !!form && form.elements.length <= 100 && submitters.length <= 1 && (
      form.getAttribute('role') === 'search' || !!form.closest('search') ||
      [...form.elements].some(c => c.type === 'search' || c.getAttribute('role') === 'searchbox' ||
        (c.getAttribute('role') === 'combobox' && c.hasAttribute('aria-controls') && /search|검색|検索/i.test(accessibleName(c))) ||
        (/^(q|query|search|search_query)$/.test(c.name || '') && /search|검색|検索/i.test(accessibleName(c)) &&
          submitters.length === 1 && /search|검색|検索/i.test(accessibleName(submitters[0]))))
    ) && [...form.elements].every(c => !sensitive(c) &&
      !/csrf|token|auth|operation|command|action|method/i.test([c.name,c.id].join(' ')) &&
      !c.hasAttribute('formaction') && !c.hasAttribute('formmethod') &&
      !['password','file','email','tel','reset','image'].includes(c.type));
    const searchContext = /search|검색|検索/i.test(accessibleName(e)) &&
      (role === 'combobox' || role === 'searchbox' || e.type === 'search') &&
      (!!e.closest('[role=search],search') || !!e.getAttribute('aria-controls') || role === 'searchbox' || e.type === 'search');
    return {tag: e.tagName.toLowerCase(), type: e.type || '', role: roleOf(e),
      visible: visible(e), in_viewport: inViewport(e),
      search_context: searchContext,
      view_control: popupView ? 'popup' : viewControl, local_ui: localUi, ui_context: uiContext.slice(0,500), search_form: searchForm,
      search_submitter_name: searchForm && submitters.length ? accessibleName(submitters[0]) : null,
      download: e.hasAttribute('download'), link_ping: !!normalize(e.getAttribute('ping')),
      name: accessibleName(e),
      value: ('value' in e && e.type !== 'file') ? String(e.value).slice(0, 2000) : null,
      _own_value: formStateComplete && 'value' in e && e.type !== 'file' ? String(e.value) : null,
      checked: typeof e.checked === 'boolean' ? e.checked : null,
      expanded: e.tagName === 'SUMMARY' ? String(!!e.parentElement?.open) : e.getAttribute('aria-expanded'),
      disabled: e.matches(':disabled') || e.getAttribute('aria-disabled') === 'true',
      readonly: !!e.readOnly || e.getAttribute('aria-readonly') === 'true',
      options: e.tagName === 'SELECT' ? [...e.options].slice(0, 200).map(o => ({
        value: String(o.value).slice(0, 2000), label: normalize(o.label).slice(0, 500),
        selected: o.selected, disabled: o.disabled || !!o.closest('optgroup[disabled]')
      })) : null,
      options_truncated: e.tagName === 'SELECT' && e.options.length > 200,
      multiple: !!e.multiple,
      scrollable: scrollable(e),
      scroll: scrollable(e) ? {x:e.scrollLeft,y:e.scrollTop,width:e.scrollWidth,height:e.scrollHeight,
        client_width:e.clientWidth,client_height:e.clientHeight} : null,
      editable: e.isContentEditable, focused: (()=>{let active=document.activeElement;while(active?.shadowRoot?.activeElement)active=active.shadowRoot.activeElement;return active===e;})(),
      pressed: e.getAttribute('aria-pressed'), selected: e.getAttribute('aria-selected'),
      rect: {x:r.x,y:r.y,width:r.width,height:r.height}, href: e.href || null,
      form_action: form ? (e.getAttribute('formaction') ? e.formAction : form.action) : null,
      form_method: form ? (e.getAttribute('formmethod') || form.method || 'get').toUpperCase() : null,
      form_fields: form ? [...form.elements].filter(c => !protectedElement(c) && c.name && !c.matches(':disabled') &&
        !['submit','button','reset','image'].includes(c.type) &&
        (!['checkbox','radio'].includes(c.type) || c.checked)).slice(0, 100).map(c =>
          normalize(accessibleName(c) || c.name).slice(0, 200)) : [],
      form_fields_truncated: !!form && form.elements.length > 100,
      _form_index: formId(form),
      submits_form: !!submit};
  });

  // Rendered text in document order, main/article first, without an AX/DOM duplicate.
  // Stop at bounded work and never read form values or embedded frame documents.
  const main = inspected.find(e => e.matches('main,[role=main],article') && !protectedElement(e) && rendered(e));
  const matchedRoots = selected ? selected.filter(e => !protectedElement(e) && rendered(e)) : null;
  const roots = matchedRoots || (query.scope ? (scope && !protectedElement(scope) && rendered(scope) ? [scope] : []) : [main || document.body]);
  const root = roots[0] || null;
  const excluded = /^(SCRIPT|STYLE|TEMPLATE|NOSCRIPT|IFRAME|FRAME|OBJECT|EMBED|SVG|CANVAS|VIDEO|INPUT|TEXTAREA|SELECT)$/;
  const block = /^(P|DIV|SECTION|ARTICLE|MAIN|HEADER|FOOTER|ASIDE|NAV|UL|OL|LI|TABLE|TR|BLOCKQUOTE|PRE|DL|DT|DD|FIGURE|FIGCAPTION|FORM)$/;
  const collectText = (start, semantic, budget=textBudget) => {
    const rootSet = new Set(start);
    let count=0, chars=0, truncated=false;
    const pieces=[], pending=[], visited=new Set();
    const add = text => {
      const part=text.slice(0,Math.max(0,budget-chars));
      if (part) {pieces.push(part);chars+=part.length;}
      if (part.length < text.length) truncated=true;
    };
    for(let i=start.length-1;i>=0;i--) if(start[i]) pending.push([start[i],false]);
    while(pending.length && !truncated) {
      const [e,closing]=pending.pop();
      if (closing) {if(/^(TD|TH)$/.test(e.tagName)) add(' | ');if(/^H[1-6]$/.test(e.tagName)||block.test(e.tagName)) add('\n');continue;}
      if(visited.has(e))continue;
      visited.add(e);
      if(++count>scanBudget){truncated=true;break;}
      if(e.nodeType===Node.TEXT_NODE){
        const remaining=Math.max(0,budget-chars), part=e.substringData(0,remaining);
        const text=normalize(part);if(text)add(text+' ');
        if(part.length<e.length)truncated=true;
        continue;
      }
      if(e.nodeType!==Node.ELEMENT_NODE || excluded.test(e.tagName) || protectedElement(e) || hidden(e))continue;
      if(getComputedStyle(e).display!=='contents' && !e.getClientRects().length)continue;
      if(semantic && !rootSet.has(e) && e.matches('nav,header,footer,aside,[role=navigation],[role=contentinfo],[role=banner]'))continue;
      const heading=/^H[1-6]$/.test(e.tagName);
      if(heading)add('\n'+'#'.repeat(Number(e.tagName[1]))+' ');else if(block.test(e.tagName))add('\n');
      if(e.tagName==='BR')add('\n');if(e.tagName==='LI')add('- ');
      if(e.tagName==='IMG' && e.alt)add('[image: '+normalize(e.alt)+'] ');
      pending.push([e,true]);
      const children=composedChildren(e);
      for(let i=children.length-1;i>=0;i--)pending.push([children[i],false]);
    }
    return {text:pieces.join('').replace(/[ \t]+\n/g,'\n').replace(/\n{3,}/g,'\n\n').trim(),truncated};
  };
  const semantic = !protectedPage && !exactTarget && !['interactive','visual'].includes(options.mode) ?
    collectText(roots,true,Math.min(textBudget,options.max_text_chars || textBudget)) : {text:'',truncated:false};
  const publicText = !protectedPage && !exactTarget ? collectText([document.body],false) : {text:'',truncated:false};
  const readerLinks=[], linkUrls=new Set();
  let readerLinksTruncated=false;
  if(options.collect_links && !protectedPage && !exactTarget) {
    const rootSet=new Set(roots);
    for(const a of inspected) {
      if(!a.matches('a[href]') || protectedElement(a) || !rendered(a) || !/^https?:\/\//i.test(a.href))continue;
      // Use the semantic roots and exclude their navigation/header/footer descendants.
      let inside=false;
      for(let p=a;p;p=parentOf(p)) {
        if(rootSet.has(p)){inside=true;break;}
        if(p.matches('nav,header,footer,aside,[role=navigation],[role=contentinfo],[role=banner]'))break;
      }
      if(!inside || linkUrls.has(a.href))continue;
      if(readerLinks.length>=200){readerLinksTruncated=true;break;}
      linkUrls.add(a.href);
      const text=normalize(collectText([a],true,200).text || a.getAttribute('aria-label') || a.title).slice(0,200);
      readerLinks.push({text,url:a.href});
    }
  }
  let queryEmptyReason=null;
  const queryEmpty=['interactive','visual'].includes(options.mode)?!queried.length:!semantic.text;
  if ((query.selector || query.scope) && queryEmpty) {
    const matches=selected || scopeMatches || (scope ? [scope] : []);
    queryEmptyReason=!matches.length?'missing':matches.every(protectedElement)?'protected':
      !roots.length?'hidden':semantic.truncated?'scan_budget':'empty';
  }

  const frames = inspected.filter(e => e.matches('iframe,frame,object,embed') && visible(e)).map(e => {
    const r = e.getBoundingClientRect();
    let safe = true;
    for (let parent = e; parent; parent = parentOf(parent)) {
      const s = getComputedStyle(parent);
      // Unsupported compositing could paint frame pixels outside this rectangle.
      if (s.transform !== 'none' || s.filter !== 'none' || s.perspective !== 'none' ||
          (s.backdropFilter && s.backdropFilter !== 'none') ||
          (s.webkitBoxReflect && s.webkitBoxReflect !== 'none') || s.mixBlendMode !== 'normal') safe = false;
    }
    return {x:r.x,y:r.y,width:r.width,height:r.height,mask_safe:safe,tag:e.tagName.toLowerCase()};
  });
  const bodyText = publicText.text;
  const frameElements = inspected.filter(e => e.matches('iframe,frame'));
  // Text alone is never a challenge signal: ordinary articles quote these phrases.
  const challengeFrame = inspected.some(e => e.matches('iframe') && visible(e) && inViewport(e) &&
    /https:\/\/(?:[^/]*\.)?(?:google\.com|recaptcha\.net)\/(?:recaptcha\/)(?:api2|enterprise)\/(?:anchor|bframe)|https:\/\/[^/]*hcaptcha\.com\/.*captcha|https:\/\/challenges\.cloudflare\.com\/.*turnstile/i.test(e.getAttribute('src') || ''));
  const challengeControl = inspected.some(e => !protectedElement(e) && visible(e) && inViewport(e) &&
    e.matches('input,button,[role=checkbox]') && /captcha|cf-chl|challenge-response/i.test([e.name,e.id].join(' ')));
  const blockPanel = inspected.some(e => !protectedElement(e) && visible(e) && inViewport(e) &&
    e.matches('form,[role=alert],[role=dialog],#challenge-form,#cf-error-details') &&
    /automated queries|automated traffic|automation access.*blocked/i.test(accessibleName(e)));
  return {elements, frame_elements:[...frameElements].slice(0,17), data: {url: location.href, title: document.title,
    frame_count:frameElements.length,
    frame_protected:frameElements.slice(0,17).map(protectedElement),
    _form_states: formStates, _forms: ownForms, form_state_complete: formStateComplete,
    readable_frames: 0, frame_reading_truncated: false,
    text: bodyText, text_scan_truncated: publicText.truncated,
    semantic_text: semantic.text, semantic_source: root ? root.tagName.toLowerCase() : null,
    semantic_source_truncated: semantic.truncated,
    ...(options.collect_links ? {reader_links:readerLinks,reader_links_truncated:readerLinksTruncated} : {}),
    has_sensitive_regions: protectedRoots.size > 0, protected_regions: protectedRegions,
    active_sensitive_controls:inspected.some(e=>e.matches('input,textarea,select,[contenteditable]') &&
      protectionRoot(e) && visible(e)),
    privacy_mask_unsafe: maskInspectionIncomplete || protectedRegions.length > 100,
    privacy_incomplete: privacyIncomplete, shadow_dom_support:'open-composed-tree',
    protected: protectedPage, nodes, mutations: globalThis.__cloudBrowserState.mutations,
    privacy_epoch:globalThis.__cloudBrowserState.privacyEpoch,
    viewport: {width:innerWidth,height:innerHeight},
    scroll: {x:scrollX,y:scrollY}, height: document.documentElement.scrollHeight,
    challenge: challengeFrame || (challengeControl && /verify (that )?you are human|complete the captcha|prove you.re not a robot/i.test(bodyText)) ? 'captcha' : blockPanel ? 'bot' : null,
    has_iframe: inspected.some(e => e.matches('iframe,frame,object,embed')), iframe_regions: frames,
    has_canvas: inspected.some(e => e.matches('canvas,video') && !protectedElement(e) && visible(e)),
    interactive_truncated: queryScanTruncated || queried.length > elements.length,
    query_scan_truncated: queryScanTruncated,
    query_match_count: selected ? selected.length : scopeMatches ? scopeMatches.length : queried.length,
    query_empty_reason: queryEmptyReason,
    scroll_scan_truncated: !targeted && candidates.length > scanBudget}};
})()
