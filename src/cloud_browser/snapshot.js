(() => {
  const options = globalThis.__cbOptions || {};
  const nodeBudget = options.lightweight ? 60 : 300;
  const scanBudget = options.lightweight ? 1000 : 10000;
  const textBudget = options.lightweight ? 8000 : 250000;
  const query = options.query || {};
  let scope = document, selected = null;
  try {
    if (query.scope) scope = document.querySelector(query.scope);
    if (query.selector) selected = scope ? [...scope.querySelectorAll(query.selector)] : [];
  } catch (_) { return {data:{query_error:true},elements:[],frame_elements:[]}; }
  // Executed in a CDP isolated world. Never invoke page handlers while observing.
  if (!globalThis.__cloudBrowserState) {
    const state = {mutations: 0};
    state.observer = new MutationObserver(() => state.mutations++);
    state.observer.observe(document, {subtree:true, childList:true, attributes:true, characterData:true});
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
  const sensitive = e => /password|passwd|one.?time|otp|auth.?code|cc-number|cc-csc|credit.?card|card.?number|secret|api.?key|security.?answer/i.test(
    [e.type, e.name, e.id, e.autocomplete, e.getAttribute('aria-label')].join(' '));
  const allInputs = [...document.querySelectorAll('input,textarea,[contenteditable=true]')];
  let protectedPage = allInputs.some(e => visible(e) && sensitive(e));
  // Child documents are inspected separately through their CDP contexts. Never
  // read embedded credentials as part of their parent's text or AX snapshot.
  // Read native controls without constructing FormData (which fires page events).
  // Values are internal CDP data only; Python replaces them with a digest immediately.
  let formStateComplete = true, formChars = 0;
  const formStates = [];
  if (!protectedPage) {
    const controls = document.querySelectorAll('input,textarea,select');
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
  const all = [...document.querySelectorAll('a,button,summary,input,textarea,select,[role=button],[role=checkbox],[role=tab],[role=link],[tabindex],[contenteditable=true]')]
    .filter(e => visible(e) && inViewport(e) && !sensitive(e));
  // Include ordinary div-based scroll panes, without an unbounded layout scan.
  const candidates = document.querySelectorAll('*');
  const known = new Set(all);
  for (let i = 0; i < Math.min(candidates.length, scanBudget); i++) {
    const e = candidates[i];
    if (!known.has(e) && visible(e) && inViewport(e) && scrollable(e)) { all.push(e); known.add(e); }
  }
  // Scoped CSS queries are read-only and may include non-interactive DOM targets.
  // The whole document's privacy guard remains in force.
  const queried = selected || (scope ? all.filter(e => scope === document || scope.contains(e)) : []);
  const elements = queried.filter(e => visible(e) && inViewport(e) && !sensitive(e)).slice(0, Math.min(nodeBudget,query.limit || nodeBudget));
  const accessibleName = e => {
    const label = e.labels?.[0] ? [...e.labels[0].childNodes].filter(n => n.nodeType === Node.TEXT_NODE).map(n => n.textContent).join(' ') : '';
    const labelledBy = (e.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ');
    return normalize(e.getAttribute('aria-label') || normalize(labelledBy) || label || e.innerText ||
      e.querySelector('img[alt]')?.alt || e.getAttribute('title') || e.placeholder || '').slice(0, 500);
  };
  const formIds = new Map(), ownForms = [];
  const formId = form => {
    if (!form || protectedPage || !formStateComplete) return null;
    if (!formIds.has(form)) {
      formIds.set(form, ownForms.length);
      ownForms.push([...form.elements].slice(0,1000).map(c =>
        [c.name,c.type,c.disabled,c.checked,c.type === 'file' ? [...c.files].map(f => [f.name,f.size,f.lastModified]) : String(c.value || '')]));
    }
    return formIds.get(form);
  };
  const nodes = elements.map(e => {
    const r = e.getBoundingClientRect();
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
    return {tag: e.tagName.toLowerCase(), type: e.type || '', role: e.getAttribute('role'),
      search_context: searchContext,
      view_control: viewControl, search_form: searchForm,
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
      editable: e.isContentEditable, focused: document.activeElement === e,
      pressed: e.getAttribute('aria-pressed'), selected: e.getAttribute('aria-selected'),
      rect: {x:r.x,y:r.y,width:r.width,height:r.height}, href: e.href || null,
      form_action: form ? (e.getAttribute('formaction') ? e.formAction : form.action) : null,
      form_method: form ? (e.getAttribute('formmethod') || form.method || 'get').toUpperCase() : null,
      form_fields: form ? [...form.elements].filter(c => c.name && !c.matches(':disabled') &&
        !['submit','button','reset','image'].includes(c.type) &&
        (!['checkbox','radio'].includes(c.type) || c.checked)).slice(0, 100).map(c =>
          normalize(c.getAttribute('aria-label') || c.labels?.[0]?.innerText || c.name).slice(0, 200)) : [],
      form_fields_truncated: !!form && form.elements.length > 100,
      _form_index: formId(form),
      submits_form: !!submit};
  });

  // Rendered text in document order, main/article first, without an AX/DOM duplicate.
  // Stop at bounded work and never read form values or embedded frame documents.
  const main = [...document.querySelectorAll('main,[role=main],article')].find(visible);
  const root = query.scope ? scope : (selected ? selected[0] : main || document.body);
  let count = 0, chars = 0, textTruncated = false;
  const pieces = [];
  const add = text => {
    if (chars >= textBudget) { textTruncated = true; return; }
    const part = text.slice(0, textBudget - chars);
    pieces.push(part); chars += part.length;
    if (part.length < text.length) textTruncated = true;
  };
  const excluded = /^(SCRIPT|STYLE|TEMPLATE|NOSCRIPT|IFRAME|FRAME|OBJECT|EMBED|SVG|CANVAS|VIDEO|INPUT|TEXTAREA|SELECT)$/;
  const block = /^(P|DIV|SECTION|ARTICLE|MAIN|HEADER|FOOTER|ASIDE|NAV|UL|OL|LI|TABLE|TR|BLOCKQUOTE|PRE|DL|DT|DD|FIGURE|FIGCAPTION|FORM)$/;
  const walk = e => {
    if (!e || textTruncated) return;
    if (++count > scanBudget) { textTruncated = true; return; }
    if (e.nodeType === Node.TEXT_NODE) {
      const text = normalize(e.textContent);
      if (text) add(text + ' ');
      return;
    }
    if (e.nodeType !== Node.ELEMENT_NODE || excluded.test(e.tagName)) return;
    const s = getComputedStyle(e);
    if (s.display === 'none' || s.visibility === 'hidden' || s.visibility === 'collapse' ||
        s.contentVisibility === 'hidden' || e.getAttribute('aria-hidden') === 'true') return;
    if (s.display !== 'contents' && !e.getClientRects().length) return;
    if (e !== root && e.matches('nav,header,footer,aside,[role=navigation],[role=contentinfo],[role=banner]')) return;
    const heading = /^H[1-6]$/.test(e.tagName);
    if (heading) add('\n' + '#'.repeat(Number(e.tagName[1])) + ' ');
    else if (block.test(e.tagName)) add('\n');
    if (e.tagName === 'BR') add('\n');
    if (e.tagName === 'LI') add('- ');
    if (e.tagName === 'IMG' && e.alt) add('[image: ' + normalize(e.alt) + '] ');
    if (e.tagName === 'PRE') add(e.innerText + '\n');
    else for (const child of e.childNodes) walk(child);
    if (/^(TD|TH)$/.test(e.tagName)) add(' | ');
    if (heading || block.test(e.tagName)) add('\n');
  };
  if (!protectedPage && !['interactive','visual'].includes(options.mode)) walk(root);
  const semanticText = pieces.join('')
    .replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();

  const frames = [...document.querySelectorAll('iframe,frame,object,embed')].filter(visible).map(e => {
    const r = e.getBoundingClientRect();
    let safe = true;
    for (let parent = e; parent; parent = parent.parentElement) {
      const s = getComputedStyle(parent);
      // Unsupported compositing could paint frame pixels outside this rectangle.
      if (s.transform !== 'none' || s.filter !== 'none' || s.perspective !== 'none' ||
          (s.backdropFilter && s.backdropFilter !== 'none') ||
          (s.webkitBoxReflect && s.webkitBoxReflect !== 'none') || s.mixBlendMode !== 'normal') safe = false;
    }
    return {x:r.x,y:r.y,width:r.width,height:r.height,mask_safe:safe,tag:e.tagName.toLowerCase()};
  });
  const bodyText = document.body?.innerText || '';
  const frameElements = document.querySelectorAll('iframe,frame');
  return {elements, frame_elements:[...frameElements].slice(0,17), data: {url: location.href, title: document.title,
    frame_count:frameElements.length,
    _form_states: formStates, _forms: ownForms, form_state_complete: formStateComplete,
    readable_frames: 0, frame_reading_truncated: false,
    text: protectedPage ? '' : bodyText.slice(0, 250000),
    semantic_text: semanticText, semantic_source: main ? main.tagName.toLowerCase() : 'body',
    semantic_source_truncated: textTruncated,
    protected: protectedPage, nodes, mutations: globalThis.__cloudBrowserState.mutations,
    viewport: {width:innerWidth,height:innerHeight},
    scroll: {x:scrollX,y:scrollY}, height: document.documentElement.scrollHeight,
    challenge: /verify (that )?you are human|complete the captcha|prove you.re not a robot/i.test(bodyText) ? 'captcha' :
      (/automated queries|automated traffic|automation access.*blocked/i.test(bodyText) ? 'bot' : null),
    has_iframe: !!document.querySelector('iframe,frame,object,embed'), iframe_regions: frames,
    has_canvas: !!document.querySelector('canvas,video'),
    interactive_truncated: queried.length > elements.length,
    scroll_scan_truncated: candidates.length > scanBudget}};
})()
