(() => {
  // Read-only observation: no page event handlers are invoked.
  // Evaluated in a CDP isolated world, so page scripts cannot forge this counter.
  if (!globalThis.__cloudBrowserState) {
    const state = {mutations: 0};
    state.observer = new MutationObserver(() => state.mutations++);
    state.observer.observe(document, {subtree:true, childList:true, attributes:true, characterData:true});
    globalThis.__cloudBrowserState = state;
  }
  const visible = e => {
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const sensitive = e => /password|passwd|one.?time|otp|auth.?code|cc-number|cc-csc|credit.?card|card.?number|secret|api.?key|security.?answer/i.test(
    [e.type, e.name, e.id, e.autocomplete, e.getAttribute('aria-label')].join(' '));
  const allInputs = [...document.querySelectorAll('input,textarea,[contenteditable=true]')];
  const protectedPage = allInputs.some(e => visible(e) && sensitive(e));
  const all = [...document.querySelectorAll('a,button,input,textarea,select,[role=button],[role=checkbox],[tabindex],[contenteditable=true]')]
    .filter(e => visible(e) && !sensitive(e));
  const elements = all.filter(e => {
    const r = e.getBoundingClientRect();
    return r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
  }).slice(0, 300);
  const nodes = elements.map(e => {
    const r = e.getBoundingClientRect();
    const label = e.labels?.[0] ? [...e.labels[0].childNodes].filter(n => n.nodeType === Node.TEXT_NODE).map(n => n.textContent).join(' ') : '';
    const labelledBy = (e.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ').trim();
    return {tag: e.tagName.toLowerCase(), type: e.type || '',
      name: (e.getAttribute('aria-label') || labelledBy || label || e.innerText || e.placeholder || '').trim().slice(0, 500),
      value: ('value' in e && e.type !== 'file') ? String(e.value).slice(0, 2000) : null,
      checked: typeof e.checked === 'boolean' ? e.checked : null,
      expanded: e.getAttribute('aria-expanded'), disabled: !!e.disabled,
      editable: e.isContentEditable,
      focused: document.activeElement === e,
      pressed: e.getAttribute('aria-pressed'), selected: e.getAttribute('aria-selected'),
      rect: {x:r.x,y:r.y,width:r.width,height:r.height}, href: e.href || null};
  });
  return {elements, data: {url: location.href, title: document.title,
    text: protectedPage ? '' : (document.body?.innerText || '').slice(0, 250000),
    protected: protectedPage, nodes, mutations: globalThis.__cloudBrowserState.mutations,
    viewport: {width:innerWidth,height:innerHeight},
    scroll: {x:scrollX,y:scrollY}, height: document.documentElement.scrollHeight,
    challenge: /verify (that )?you are human|complete the captcha|prove you.re not a robot/i.test(document.body?.innerText || '') ? 'captcha' :
      (/automated queries|automated traffic|automation access.*blocked/i.test(document.body?.innerText || '') ? 'bot' : null),
    has_iframe: !!document.querySelector('iframe'),
    has_canvas: !!document.querySelector('canvas,video'),
    interactive_truncated: all.length > 300}};
})()
