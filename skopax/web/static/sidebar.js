var SIDE_ROWS = {
  anoms: ['Board', 'board'],
  reqs: ['Requests', 'inbox'],
  chat: ['Chat', 'chat'],
  inv: ['Investigator', 'spark'],
  svcs: ['Services', 'server'],
  lat: ['Latency', 'timer'],
  rels: ['Releases', 'tag'],
  reps: ['Deploy reports', 'report'],
  slack: ['Notifications', 'bell'],
  hist: ['Events', 'pulse'],
  acts: ['History', 'history'],
  costs: ['Costs', 'coin'],
  tools: ['Tools', 'wrench'],
  mcp: ['Connect MCP', 'plug'],
  review: ['Review', 'check'],
  docs: ['Docs', 'book'],
  prefs: ['Settings', 'gear']
};
var SIDE_GROUPS = [
  {id: 'main', label: '', rows: ['anoms', 'reqs', 'chat', 'inv']},
  {id: 'observe', label: 'Observe', rows: ['svcs', 'lat', 'rels', 'reps']},
  {id: 'activity', label: 'Activity', rows: ['slack', 'hist', 'acts', 'costs']},
  {id: 'connect', label: 'Connect', rows: ['tools', 'mcp']},
  {id: 'more', label: 'More', fold: true, rows: ['review', 'docs']}
];
var SIDE_PINNED = ['prefs'];
var SIDE_NEW = ['chat', 'mcp'];
var SIDE_BLANK = ['chat', 'mcp', 'docs', 'prefs'];
var ADMIN_TABS = ['settings', 'access', 'integrations', 'features', 'ai', 'changes'];
var ADMIN_LABELS = {settings: 'General', access: 'Access & sign-in', integrations: 'Integrations', features: 'Features', ai: 'Investigator', changes: 'Changes'};
var ADMIN_ICONS = {settings: 'gear', access: 'lock', integrations: 'plug', features: 'toggle', ai: 'spark', changes: 'history'};
var ADMIN_COUNTED = ['settings', 'access', 'integrations', 'features'];
var ADMIN_KEY = 'skopax-admin-tab', ADMIN_HOME = 'integrations';
var SIDE_HOT = ['k-crit', 'k-warn', 'k-info', 'k-act', 'k-ok'];
var SIDE_THEMES = ['system', 'light', 'dark'];
var SIDE_KEY = 'skopax-sidebar', SIDE_PHONE = '(max-width: 768px)', SIDE_FULL_W = 252, SIDE_RAIL_W = 64;
var SIDE_PAGE_TAB = /^[a-z][a-z0-9-]{1,30}$/, SIDE_PAGE_ICON = /^\/plugins\/[a-z][a-z0-9-]{1,30}\/[A-Za-z0-9._-]{1,80}$/;
var SIDE_PAGES = null, SIDE_WANTED = null, sideRailOn = false;
var TABS = [];

function sideTabs() {
  TABS.length = 0;
  Object.keys(SIDE_ROWS).forEach(function (id) { TABS.push({id: id, label: SIDE_ROWS[id][0]}); });
  ADMIN_TABS.forEach(function (id) { TABS.push({id: id, label: ADMIN_LABELS[id] || id, admin: true}); });
  (SIDE_PAGES || []).forEach(function (p) { TABS.push({id: p.tab, label: p.label, plugin: p.plugin}); });
}
function sideGo(e) {
  var a = e.currentTarget;
  if (e.type === 'keydown') {
    if (e.key !== ' ' && e.key !== 'Spacebar') return;
  } else if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button) return;
  e.preventDefault();
  if (a.getAttribute('aria-disabled') === 'true') return;
  pickTab(a.dataset.go || a.dataset.tab);
}
function sideLink(id, label, icon, end) {
  var a = el('a', 'tab', icon + '<span class="lbl"></span><span class="end">' + end + '</span>');
  a.querySelector('.lbl').textContent = label;
  a.dataset.label = label;
  if (id) {
    a.id = 'tab-' + id;
    a.href = '#t=' + id;
    a.dataset.tab = id;
  }
  a.addEventListener('click', sideGo);
  a.addEventListener('keydown', sideGo);
  return a;
}
function sideCount(id, blank) { return '<span class="count" id="count-' + id + '">' + (blank || SIDE_BLANK.indexOf(id) >= 0 ? '' : '\u2013') + '</span>'; }
function sideRow(id) {
  var r = SIDE_ROWS[id];
  return sideLink(id, r[0], navIcon(r[1]), sideCount(id) + (SIDE_NEW.indexOf(id) >= 0 ? '<span class="b-new">New</span>' : ''));
}
function sideTop() {
  var slot = document.getElementById('side-top');
  if (!slot || slot.firstChild) return slot;
  var box = el('div', 'inst'), menu = el('div', 'menu');
  box.id = 'inst';
  var btn = el('button', 'inst-btn', '<span class="inst-tile" aria-hidden="true"></span><span class="inst-t"><span class="inst-k">Instance</span><span class="inst-name" data-no-tx></span></span>' + navIcon('updown'));
  btn.type = 'button';
  btn.id = 'inst-btn';
  btn.setAttribute('aria-haspopup', 'menu');
  btn.setAttribute('aria-expanded', 'false');
  btn.setAttribute('aria-controls', 'inst-menu');
  menu.id = 'inst-menu';
  menu.setAttribute('role', 'menu');
  menu.setAttribute('aria-label', 'Switch instance');
  menu.hidden = true;
  box.appendChild(btn);
  box.appendChild(menu);
  var search = el('button', 'search', navIcon('search') + '<span class="lbl">Search</span><kbd></kbd>');
  search.type = 'button';
  search.id = 'search-btn';
  search.setAttribute('aria-haspopup', 'dialog');
  search.setAttribute('aria-controls', 'pal');
  slot.appendChild(box);
  slot.appendChild(search);
  return slot;
}
function sideGroups() {
  var nav = document.getElementById('snav');
  nav.innerHTML = '';
  SIDE_GROUPS.forEach(function (g) {
    var box = el('div', 's-grp'), rows = box;
    box.dataset.grp = g.id;
    if (g.fold) {
      var more = el('button', 'tab more', navIcon('dots') + '<span class="lbl"></span><span class="end"><span class="m-dot" aria-hidden="true"></span><span class="m-chev">' + navIcon('chevD') + '</span></span>');
      more.type = 'button';
      more.id = 'side-more';
      more.dataset.label = g.label;
      more.querySelector('.lbl').textContent = g.label;
      more.setAttribute('aria-expanded', 'false');
      more.setAttribute('aria-controls', 'side-more-list');
      more.addEventListener('click', function () { sideMore(more.getAttribute('aria-expanded') !== 'true'); });
      box.appendChild(more);
      rows = el('div', 's-more');
      rows.id = 'side-more-list';
      rows.setAttribute('role', 'group');
      rows.setAttribute('aria-label', g.label);
      rows.hidden = true;
      box.appendChild(rows);
      if (typeof MutationObserver === 'function') new MutationObserver(sideMarkMore).observe(rows, {subtree: true, attributes: true, attributeFilter: ['class']});
    } else if (g.label) {
      var h = el('h2', 'g-h');
      h.id = 'gh-' + g.id;
      h.textContent = g.label;
      box.setAttribute('role', 'group');
      box.setAttribute('aria-labelledby', h.id);
      box.appendChild(h);
    } else {
      box.setAttribute('role', 'group');
      box.setAttribute('aria-label', 'Board and investigator');
    }
    g.rows.forEach(function (id) { rows.appendChild(sideRow(id)); });
    nav.appendChild(box);
  });
  var pinned = document.getElementById('pinned');
  SIDE_PINNED.forEach(function (id) { pinned.appendChild(sideRow(id)); });
}
function sideGroupBox(id) {
  if (id === 'more') return document.getElementById('side-more-list');
  return [].filter.call(document.querySelectorAll('#snav > .s-grp'), function (b) { return b.dataset.grp === id; })[0] || sideGroupBox('observe');
}
function sideMore(open) {
  var more = document.getElementById('side-more'), list = document.getElementById('side-more-list');
  if (!more) return;
  var shown = open && list.hidden;
  list.hidden = !open;
  more.setAttribute('aria-expanded', open ? 'true' : 'false');
  sideMarkMore();
  if (shown && typeof list.scrollIntoView === 'function') list.scrollIntoView({block: 'nearest', behavior: sideCalm() ? 'auto' : 'smooth'});
}
function sideFade() {
  var nav = document.getElementById('snav');
  if (nav) nav.classList.toggle('more-below', nav.scrollTop + nav.clientHeight < nav.scrollHeight - 1);
}
function sideCalm() { return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches); }
function sideMarkMore() {
  var more = document.getElementById('side-more'), list = document.getElementById('side-more-list');
  if (!more) return;
  var hot = list.hidden ? [].filter.call(list.querySelectorAll('a.tab'), function (t) { return t.classList.contains('hot'); }) : [];
  var k = SIDE_HOT.filter(function (c) { return hot.some(function (t) { return t.classList.contains(c); }); })[0];
  var cls = 'tab more' + (k ? ' hot ' + k : '');
  if (more.className !== cls) more.className = cls;
  if (hot.length) more.title = more.dataset.label + ' \u00b7 ' + hot.map(function (t) { var n = (t.querySelector('.count') || {}).textContent; return t.dataset.label + (n ? ' ' + n : ''); }).join(', ');
  else more.removeAttribute('title');
}
function sideInMore(id) {
  var list = document.getElementById('side-more-list');
  return !!list && [].some.call(list.querySelectorAll('a.tab'), function (a) { return a.dataset.tab === id; });
}
function sideAdmin() {
  var on = typeof ADMIN_OK !== 'undefined' && !!ADMIN_OK;
  var row = document.getElementById('side-admin');
  if (!row) {
    row = sideLink('', 'Admin', navIcon('shield'), '<span class="a-dot" role="img" hidden></span>');
    row.id = 'side-admin';
    document.getElementById('pinned').appendChild(row);
    var nav = document.getElementById('adm-nav');
    ADMIN_TABS.forEach(function (id) { nav.appendChild(sideLink(id, ADMIN_LABELS[id] || id, navIcon(ADMIN_ICONS[id]), ADMIN_COUNTED.indexOf(id) >= 0 ? sideCount(id) : '')); });
  }
  var last = load(ADMIN_KEY), go = ADMIN_TABS.indexOf(last) >= 0 ? last : ADMIN_HOME;
  row.href = '#t=' + go;
  row.dataset.go = go;
  var notes = on && typeof ADMIN_NOTES !== 'undefined' && Array.isArray(ADMIN_NOTES) ? ADMIN_NOTES : [];
  var warn = notes.filter(function (n) { return n.level === 'warn'; }).length, dot = row.querySelector('.a-dot');
  var said = warn + (warn === 1 ? ' warning' : ' warnings');
  dot.hidden = !warn;
  if (warn) { dot.setAttribute('aria-label', said); row.title = 'Admin \u00b7 ' + said; }
  else { dot.removeAttribute('aria-label'); row.removeAttribute('title'); }
  row.hidden = !on;
}
function sideAttr() {
  var raw;
  try { raw = JSON.parse(document.getElementById('brand-data').textContent); } catch (e) { raw = null; }
  if (raw && raw.name && raw.attribution === null) return null;
  var at = typeof BRAND !== 'undefined' && BRAND.attribution;
  return at && at.text ? at : null;
}
function sideFoot(acct) {
  var at = sideAttr(), foot = document.getElementById('side-attr'), end = document.getElementById('end-attr'), box = document.getElementById('account');
  [foot, end].forEach(function (b) { if (b) { b.innerHTML = ''; b.hidden = !at; } });
  var line = function (id) {
    var a = el('a', 'attr-line'), name = typeof BRAND !== 'undefined' && BRAND.name ? String(BRAND.name) : '';
    var cut = name && at.text.indexOf(name + ' \u00b7 ') === 0 ? name.length : at.text.length;
    if (id) a.id = id;
    a.setAttribute('href', at.href);
    a.setAttribute('rel', 'noopener noreferrer');
    a.title = at.text;
    a.appendChild(el('span', 'attr-name')).textContent = at.text.slice(0, cut);
    if (cut < at.text.length) a.appendChild(el('span', 'attr-by')).textContent = at.text.slice(cut);
    return a;
  };
  if (at && foot) {
    foot.appendChild(line('attribution'));
    var mark = el('a', 'attr-mark', '<img src="/icons/mark.svg" alt="" width="16" height="16">');
    mark.setAttribute('href', at.href);
    mark.setAttribute('rel', 'noopener noreferrer');
    mark.setAttribute('aria-label', at.text);
    mark.title = at.text;
    foot.appendChild(mark);
  }
  if (at && end) end.appendChild(line(''));
  if (!box || acct === undefined) return;
  box.innerHTML = '';
  box.hidden = !acct;
  if (!acct) return;
  if (acct.login) {
    var login = el('a', 'tab login-row', navIcon('key') + '<span class="lbl">Log in</span><span class="end">read only</span>');
    login.dataset.label = 'Log in';
    login.setAttribute('href', acct.login);
    login.title = acct.tip;
    box.appendChild(login);
    return;
  }
  var btn = el('button', 'user-btn', '<span class="av" aria-hidden="true"></span><span class="user-t"><b data-no-tx></b><span></span></span>' + navIcon('updown'));
  btn.type = 'button';
  btn.id = 'user-btn';
  btn.setAttribute('aria-haspopup', 'menu');
  btn.setAttribute('aria-expanded', 'false');
  btn.setAttribute('aria-controls', 'user-menu');
  btn.title = acct.tip;
  btn.querySelector('.av').textContent = (Array.from(acct.name)[0] || '?').toUpperCase();
  btn.querySelector('.user-t b').textContent = acct.name;
  btn.querySelector('.user-t span').textContent = acct.role;
  var menu = el('div', 'menu', '<div class="menu-h" role="none" data-no-tx></div>');
  menu.id = 'user-menu';
  menu.setAttribute('role', 'menu');
  menu.setAttribute('aria-label', 'Account');
  menu.hidden = true;
  menu.firstChild.textContent = 'Signed in as ' + acct.user + ' \u00b7 ' + acct.role;
  var note = el('span', 'acct-note');
  note.setAttribute('role', 'status');
  var items = function () { return [].slice.call(menu.querySelectorAll('[role="menuitem"]')); };
  var outside = function (e) { if (!box.contains(e.target)) shut(false); };
  var shut = function (back) {
    if (menu.hidden) return;
    menu.hidden = true;
    btn.setAttribute('aria-expanded', 'false');
    document.removeEventListener('click', outside, true);
    if (back) btn.focus();
  };
  var show = function (last) {
    menu.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
    if (window.__skopaxTipHide) window.__skopaxTipHide();
    document.addEventListener('click', outside, true);
    var list = items();
    if (list.length) list[last ? list.length - 1 : 0].focus();
  };
  (acct.items || []).forEach(function (it) {
    var b = el('button', 'mi', navIcon(it.icon) + '<span></span>');
    b.type = 'button';
    b.setAttribute('role', 'menuitem');
    b.lastChild.textContent = it.label;
    b.addEventListener('click', function () { shut(true); it.run(b); });
    menu.appendChild(b);
  });
  btn.addEventListener('click', function () { if (menu.hidden) show(false); else shut(false); });
  btn.addEventListener('keydown', function (e) {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    show(e.key === 'ArrowUp');
  });
  menu.addEventListener('keydown', function (e) {
    var list = items(), i = list.indexOf(document.activeElement), to = null;
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); shut(true); return; }
    if (e.key === 'Tab') { shut(false); return; }
    if (e.key === 'ArrowDown') to = (i + 1) % list.length;
    else if (e.key === 'ArrowUp') to = (i - 1 + list.length) % list.length;
    else if (e.key === 'Home') to = 0;
    else if (e.key === 'End') to = list.length - 1;
    if (to === null || !list.length) return;
    e.preventDefault();
    list[to].focus();
  });
  box.appendChild(btn);
  box.appendChild(menu);
  box.appendChild(note);
}
function sideChat(me) {
  var row = document.getElementById('tab-chat');
  if (row) row.hidden = !(me && me.chat && (me.features || {}).chat !== false);
}
function sidePhone() { return !!(window.matchMedia && window.matchMedia(SIDE_PHONE).matches); }
function sideWidth() { return sidePhone() ? 0 : sideRailOn ? SIDE_RAIL_W : SIDE_FULL_W; }
function sideLayout() {
  var phone = sidePhone(), rail = sideRailOn && !phone, btn = document.getElementById('side-rail');
  var label = phone ? 'Close menu' : rail ? 'Expand sidebar' : 'Collapse sidebar';
  document.body.classList.toggle('is-rail', rail);
  btn.innerHTML = navIcon(phone ? 'x' : rail ? 'expand' : 'collapse');
  btn.setAttribute('aria-label', label);
  btn.title = label;
  if (phone) btn.removeAttribute('aria-expanded'); else btn.setAttribute('aria-expanded', rail ? 'false' : 'true');
  if (!phone) sideCloseDrawer(false);
  if (window.__skopaxTipHide) window.__skopaxTipHide();
  if (typeof applyNavOff === 'function' && curTab) applyNavOff();
  sideFade();
}
function sideRail(on, keep) {
  sideRailOn = !!on;
  if (keep) save(SIDE_KEY, sideRailOn ? 'rail' : 'full');
  sideLayout();
}
function sideOpenDrawer() {
  document.body.classList.add('drawer-open');
  document.getElementById('side-scrim').hidden = false;
  document.getElementById('side-open').setAttribute('aria-expanded', 'true');
  document.getElementById('content').setAttribute('inert', '');
  document.getElementById('side-rail').focus();
}
function sideCloseDrawer(ret) {
  if (!document.body.classList.contains('drawer-open')) return;
  document.body.classList.remove('drawer-open');
  document.getElementById('side-scrim').hidden = true;
  document.getElementById('side-open').setAttribute('aria-expanded', 'false');
  document.getElementById('content').removeAttribute('inert');
  if (ret) document.getElementById('side-open').focus();
}
function sideLabel(id) {
  var t = TABS.filter(function (x) { return x.id === id; })[0];
  return t ? (t.admin ? 'Admin \u203a ' : '') + t.label : '';
}
function sideMark(id) {
  document.querySelectorAll('#side a.tab, #adm-nav a.tab').forEach(function (a) {
    var on = a.dataset.tab ? a.dataset.tab === id : a.id === 'side-admin' && ADMIN_TABS.indexOf(id) >= 0;
    if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  var shell = document.getElementById('adm-shell');
  if (shell) shell.hidden = ADMIN_TABS.indexOf(id) < 0;
  if (sideInMore(id)) sideMore(true);
  var label = sideLabel(id);
  document.getElementById('crumb-page').textContent = label;
  document.title = label ? label + ' \u00b7 ' + brandName() : brandName();
  sideCloseDrawer(document.getElementById('side').contains(document.activeElement));
}
function sideFree(tab) { return !['tab-', 'panel-', 'count-'].some(function (pre) { return document.getElementById(pre + tab); }); }
function sidePlugins(pages) {
  var had = {};
  (SIDE_PAGES || []).forEach(function (p) { had[p.tab] = 1; });
  var seen = {}, list = (Array.isArray(pages) ? pages : []).filter(function (p) {
    var ok = p && typeof p.tab === 'string' && SIDE_PAGE_TAB.test(p.tab) && !SIDE_ROWS[p.tab] && ADMIN_TABS.indexOf(p.tab) < 0 && typeof p.label === 'string' && !!p.label && !seen[p.tab] && (had[p.tab] || sideFree(p.tab));
    if (ok) seen[p.tab] = 1;
    return ok;
  });
  (SIDE_PAGES || []).forEach(function (p) {
    if (seen[p.tab]) return;
    ['tab-', 'panel-'].forEach(function (pre) { var x = document.getElementById(pre + p.tab); if (x) x.remove(); });
  });
  SIDE_PAGES = list;
  sideTabs();
  list.forEach(function (p) {
    if (had[p.tab]) return;
    var icon = typeof p.icon === 'string' && SIDE_PAGE_ICON.test(p.icon) ? '<img class="i" alt="" width="16" height="16">' : navIcon('board');
    var row = sideLink(p.tab, p.label, icon, sideCount(p.tab, true));
    row.dataset.plugin = '1';
    var img = row.querySelector('img');
    if (img) img.setAttribute('src', p.icon);
    sideGroupBox(p.group).appendChild(row);
    var section = el('section', 'panel');
    section.id = 'panel-' + p.tab;
    section.hidden = true;
    document.querySelector('main.main').insertBefore(section, document.getElementById('end-attr'));
    document.dispatchEvent(new CustomEvent('skopax:page', {detail: {tab: p.tab, section: section}}));
  });
  if (curTab && !TABS.some(function (t) { return t.id === curTab; })) showTab('anoms');
  sideMarkMore();
  sideFade();
  if (SIDE_WANTED) {
    var wanted = SIDE_WANTED;
    SIDE_WANTED = null;
    if (curTab === wanted.at && TABS.some(function (t) { return t.id === wanted.raw.split('/')[0]; })) openTabPath(wanted.raw);
  }
}
function sideTheme(step) {
  var ap = {};
  try { ap = JSON.parse(load('skopax-appearance') || '{}') || {}; } catch (e) {}
  var mode = SIDE_THEMES.indexOf(ap.mode) >= 0 ? ap.mode : 'system';
  if (step) {
    mode = SIDE_THEMES[(SIDE_THEMES.indexOf(mode) + 1) % SIDE_THEMES.length];
    ap.mode = mode;
    save('skopax-appearance', JSON.stringify(ap));
  }
  var root = document.documentElement, btn = document.getElementById('theme-btn');
  if (mode === 'system') delete root.dataset.theme; else root.dataset.theme = mode;
  document.querySelectorAll('#panel-prefs [data-ap="mode"] button').forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.v === mode)); });
  btn.innerHTML = navIcon(mode === 'light' ? 'sun' : mode === 'dark' ? 'moon' : 'monitor');
  btn.setAttribute('aria-label', 'Theme: ' + mode + '. Click to change.');
  btn.title = 'Theme: ' + mode;
}
function sideRender() {
  sideTabs();
  sideTop();
  sideGroups();
  sideAdmin();
  sideFoot();
  document.getElementById('side-open').innerHTML = navIcon('menu');
  document.getElementById('side-open').addEventListener('click', sideOpenDrawer);
  document.getElementById('side-scrim').addEventListener('click', function () { sideCloseDrawer(true); });
  document.getElementById('side-rail').addEventListener('click', function () { if (sidePhone()) sideCloseDrawer(true); else sideRail(!sideRailOn, true); });
  document.getElementById('theme-btn').addEventListener('click', function () { sideTheme(true); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && document.body.classList.contains('drawer-open')) sideCloseDrawer(true); });
  var nav = document.getElementById('snav');
  nav.addEventListener('scroll', sideFade, {passive: true});
  window.addEventListener('resize', sideFade);
  if (typeof MutationObserver === 'function') new MutationObserver(sideFade).observe(nav, {childList: true, subtree: true, attributes: true, attributeFilter: ['hidden']});
  if (window.matchMedia) {
    var phone = window.matchMedia(SIDE_PHONE);
    if (phone.addEventListener) phone.addEventListener('change', sideLayout);
  }
  sideTheme(false);
  sideRail(load(SIDE_KEY) === 'rail', false);
}
sideRender();
