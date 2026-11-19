(function () {
  var COLOUR = /^#[0-9a-f]{6}$/i, FALLBACK = '#e3a744', CURRENT = '#3cc8f0', WEB = /^https?:\/\//i;
  var GROUPS = ['Cards', 'Services', 'Pages', 'Settings', 'Actions'], MAX = 8, FIRST_CARDS = 4, FIRST_PAGES = 6;
  var FIND_TABS = ['settings'];
  var MAC = /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent || '');
  var KEY = MAC ? '\u2318K' : 'Ctrl K';
  var pal = document.getElementById('pal'), q = document.getElementById('pal-q'), list = document.getElementById('pal-list');
  var box = document.getElementById('inst'), btn = document.getElementById('inst-btn'), menu = document.getElementById('inst-menu'), search = document.getElementById('search-btn');
  if (!pal || !q || !list || !box || !btn || !menu || !search) return;
  var me = {}, sets = null, asked = false, owned = null, ownedAsk = null, ownedTried = false, items = [], at = 0, back = null;

  window.__skopaxGo = function (url) { location.href = url; };

  function hostOf(url) { try { return new URL(url).host; } catch (e) { return ''; } }
  function first(letter, name) { return (Array.from(String(letter || name || ''))[0] || '?').toUpperCase(); }
  function paint(tile, letter, colour) {
    tile.textContent = letter;
    tile.style.backgroundColor = colour;
  }
  function menuItems() { return [].slice.call(menu.querySelectorAll('[role^="menuitem"]')); }
  function outside(e) { if (!box.contains(e.target)) shut(false); }
  function shut(focus) {
    if (menu.hidden) return;
    menu.hidden = true;
    btn.setAttribute('aria-expanded', 'false');
    document.removeEventListener('click', outside, true);
    if (focus) btn.focus();
  }
  function show() {
    if (window.__skopaxTipHide) window.__skopaxTipHide();
    menu.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
    document.addEventListener('click', outside, true);
    var all = menuItems(), on = all.filter(function (i) { return i.getAttribute('aria-checked') === 'true'; })[0] || all[0];
    if (on) on.focus();
  }
  function ownedKnown() { return !!owned || (typeof FEATS !== 'undefined' && !!FEATS); }
  function featureOf(key) { return (owned || []).concat(typeof featItems === 'function' ? featItems() : []).filter(function (f) { return f.key === key; })[0] || null; }
  function adminTabForKey(key, group) {
    if (group === AI_GROUP) return 'ai';
    if (group === SIGNIN_GROUP || group === MCP_GROUP) return 'access';
    if (group === FEATURES_GROUP || featureOf(key)) return 'features';
    return 'settings';
  }
  function adminFind(tab, text, key) {
    var to = tab && ADMIN_TABS.indexOf(tab) >= 0 ? tab : 'settings';
    if (FIND_TABS.indexOf(to) < 0) clearAdminFind();
    else if (typeof ADMIN_FIND !== 'undefined') ADMIN_FIND = String(text).trim().toLowerCase();
    if (typeof ADMIN_REVEAL !== 'undefined') ADMIN_REVEAL = key && FIND_TABS.indexOf(to) < 0 ? {tab: to, key: key} : null;
    pickTab(to);
  }
  function adminPick(i) {
    var go = function () { adminFind(adminTabForKey(i.key, i.group), i.label, i.key); };
    if (ownedKnown()) go();
    else askOwned().then(go);
  }
  function clearAdminFind() {
    if (typeof ADMIN_FIND === 'undefined' || !ADMIN_FIND) return;
    ADMIN_FIND = '';
    var box = document.getElementById('adm-find');
    if (box) box.value = '';
    if (typeof adminFilter === 'function') adminFilter();
  }
  function entry(url, name, letter, colour, host) {
    var it = el(url ? 'a' : 'button', 'mi', '<span class="inst-tile" aria-hidden="true"></span><span class="mi-t" data-no-tx><span class="mi-n"></span><small></small></span>' + navIcon('tick', 'chk'));
    paint(it.querySelector('.inst-tile'), letter, colour);
    it.querySelector('.mi-n').textContent = name;
    it.querySelector('small').textContent = host;
    it.setAttribute('role', 'menuitemradio');
    it.setAttribute('aria-checked', url ? 'false' : 'true');
    if (url) {
      it.setAttribute('href', url);
      it.setAttribute('rel', 'noopener noreferrer');
    } else it.type = 'button';
    it.addEventListener('click', function (e) {
      if (url && (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button)) { shut(false); return; }
      e.preventDefault();
      shut(!url);
      if (url) window.__skopaxGo(url);
    });
    return it;
  }
  function instRender(given) {
    me = given || {};
    var inst = me.instances || {}, cur = inst.current || {};
    var name = String(cur.name || location.hostname || 'local'), letter = first(cur.letter, name);
    paint(btn.querySelector('.inst-tile'), letter, CURRENT);
    btn.querySelector('.inst-name').textContent = name;
    btn.setAttribute('aria-label', 'Instance: ' + name + '. Switch instance');
    btn.title = 'Instance: ' + name;
    menu.innerHTML = '';
    menu.appendChild(entry('', name, letter, CURRENT, location.host));
    (Array.isArray(inst.links) ? inst.links : []).forEach(function (l) {
      if (!l || typeof l.name !== 'string' || !l.name || typeof l.url !== 'string' || !WEB.test(l.url) || !hostOf(l.url)) return;
      menu.appendChild(entry(l.url, l.name, first(l.letter, l.name), COLOUR.test(l.colour || '') ? l.colour : FALLBACK, hostOf(l.url)));
    });
    if (!me.admin) return;
    var sep = el('div', 'menu-sep');
    sep.setAttribute('role', 'separator');
    menu.appendChild(sep);
    var manage = el('button', 'mi', navIcon('gear') + '<span class="mi-t">Manage instances</span>');
    manage.type = 'button';
    manage.setAttribute('role', 'menuitem');
    manage.addEventListener('click', function () { shut(true); adminFind('settings', 'instance'); });
    menu.appendChild(manage);
  }

  btn.addEventListener('click', function () { if (menu.hidden) show(); else shut(false); });
  btn.addEventListener('keydown', function (e) {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    show();
  });
  menu.addEventListener('keydown', function (e) {
    var all = menuItems(), i = all.indexOf(document.activeElement), to = null;
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); shut(true); return; }
    if (e.key === 'Tab') { shut(false); return; }
    if ((e.key === 'Enter' || e.key === ' ') && i >= 0) { e.preventDefault(); all[i].click(); return; }
    if (e.key === 'ArrowDown') to = (i + 1) % all.length;
    else if (e.key === 'ArrowUp') to = (i - 1 + all.length) % all.length;
    else if (e.key === 'Home') to = 0;
    else if (e.key === 'End') to = all.length - 1;
    if (to === null || !all.length) return;
    e.preventDefault();
    all[to].focus();
  });

  function words(text) { return foldCase(text).split(/\s+/).filter(Boolean); }
  function hl(text, ws) {
    var chars = Array.from(String(text)), folded = '', of = [], on = chars.map(function () { return false; });
    chars.forEach(function (c, i) { var f = foldCase(c); for (var k = 0; k < f.length; k++) of.push(i); folded += f; });
    ws.forEach(function (w) {
      var i = folded.indexOf(w);
      for (var k = i; i >= 0 && k < i + w.length; k++) on[of[k]] = true;
    });
    var out = '', marked = false;
    chars.forEach(function (c, i) {
      if (on[i] !== marked) { out += on[i] ? '<mark>' : '</mark>'; marked = on[i]; }
      out += esc(c);
    });
    return out + (marked ? '</mark>' : '');
  }
  function index() {
    var out = [], opened = openAll().slice().sort(bySevOrd);
    opened.concat(CARDS.filter(function (a) { return opened.indexOf(a) < 0; })).forEach(function (a) {
      out.push({kind: 'Cards', raw: true, open: opened.indexOf(a) >= 0, sev: a.sev, label: (a.num ? '#' + a.num + ' ' : '') + [a.svc, a.label].filter(Boolean).join(' '), hay: cardText(a),
        meta: (SEV[a.sev] || SEV.info)[0], run: function () { showLinked(encodeURIComponent(a.key)); }});
    });
    S.forEach(function (s) { out.push({kind: 'Services', raw: true, label: String(s.svc || ''), hay: s.ns, meta: s.ns || 'service', icon: 'server', run: function () { showSvcCards(s); }}); });
    [].forEach.call(document.querySelectorAll('#snav a.tab, #pinned a.tab'), function (a) {
      if (a.hidden || a.getAttribute('aria-disabled') === 'true' || !(a.dataset.tab || a.dataset.go)) return;
      out.push({kind: 'Pages', label: a.dataset.label || '', meta: a.dataset.plugin ? 'plug-in' : '', node: a.querySelector('.i'), run: function () { a.click(); }});
    });
    if (typeof SIDE_MENU !== 'undefined' && SIDE_MENU) {
      out.push({kind: 'Pages', label: 'Settings', meta: '', node: null, icon: 'gear', run: function () { pickTab('prefs'); }});
      var adm = document.getElementById('um-admin');
      if (adm && !adm.hidden) out.push({kind: 'Pages', label: 'Admin', meta: '', node: null, icon: 'shield', run: function () { pickTab(adm.dataset.go || ADMIN_HOME); }});
    }
    out.push({kind: 'Settings', label: 'Appearance', hay: 'theme mode text size density language', meta: 'Settings', icon: 'monitor', run: function () { pickTab('prefs'); }});
    if (me.user) out.push({kind: 'Settings', label: 'Account', hay: 'password sign out', meta: 'Settings', icon: 'key', run: function () { pickTab('prefs'); }});
    if (me.admin) {
      ADMIN_TABS.forEach(function (id) { out.push({kind: 'Settings', label: ADMIN_LABELS[id] || id, meta: 'Admin', icon: 'shield', run: function () { pickTab(id); }}); });
      ((ownedTried || ownedKnown()) && sets || []).forEach(function (i) {
        var tab = adminTabForKey(i.key, i.group), card = featureOf(i.key);
        out.push({kind: 'Settings', label: i.label, hay: i.key + (card ? ' ' + card.label : ''), meta: 'Admin \u00b7 ' + (tab === 'settings' ? i.group : ADMIN_LABELS[tab] || tab), icon: 'gear', find: true, run: function () { adminPick(i); }});
      });
    }
    out.push({kind: 'Actions', label: 'Collapse or expand sidebar', hay: 'rail', meta: 'Action', icon: 'collapse', run: function () {
      if (sidePhone()) { sideOpenDrawer(); return; }
      sideRail(!sideRailOn, true);
      document.getElementById('side-rail').focus();
    }});
    if (me.user && me.canChangePassword) out.push({kind: 'Actions', label: 'Change password', meta: 'Account', icon: 'key', run: function () {
      pickTab('prefs');
      var pw = document.getElementById('pf-pw');
      if (pw && !pw.hidden) pw.click();
    }});
    out.push({kind: 'Actions', label: 'Switch theme', hay: 'light dark system mode', meta: 'Action', icon: 'sun', run: function () { sideTheme(true); }});
    return out;
  }
  function matches(x, ws) {
    var hay = foldCase(x.label + ' ' + (x.hay || '') + ' ' + x.kind + ' ' + (x.meta || ''));
    return ws.every(function (w) { return hay.indexOf(w) >= 0; });
  }
  function lead(x) {
    if (x.kind === 'Cards') {
      var dot = el('span', 'sdot ' + (SEV[x.sev] || SEV.info)[1]);
      dot.setAttribute('aria-hidden', 'true');
      return dot;
    }
    if (x.node) return x.node.cloneNode(true);
    return el('span', 'pal-i', navIcon(x.icon || 'chevR'));
  }
  function select(i, scroll) {
    if (!items.length) { at = 0; q.removeAttribute('aria-activedescendant'); return; }
    at = (i + items.length) % items.length;
    [].forEach.call(list.querySelectorAll('[role="option"]'), function (o) { o.setAttribute('aria-selected', String(+o.dataset.i === at)); });
    q.setAttribute('aria-activedescendant', 'po-' + at);
    var o = scroll && document.getElementById('po-' + at);
    if (o) o.scrollIntoView({block: 'nearest'});
  }
  function palRender() {
    var ws = words(q.value), all = index(), res;
    if (!ws.length) res = all.filter(function (x) { return x.open; }).slice(0, FIRST_CARDS).concat(all.filter(function (x) { return x.kind === 'Pages'; }).slice(0, FIRST_PAGES));
    else res = all.filter(function (x) { return matches(x, ws); });
    items = [];
    list.innerHTML = '';
    GROUPS.forEach(function (g) {
      var group = res.filter(function (x) { return x.kind === g; }).slice(0, MAX);
      if (!group.length) return;
      var box = el('div', 'pal-grp'), h = el('div', 'pal-g');
      h.id = 'pal-g-' + g.toLowerCase();
      h.setAttribute('role', 'presentation');
      h.textContent = g;
      box.setAttribute('role', 'group');
      box.setAttribute('aria-labelledby', h.id);
      box.appendChild(h);
      list.appendChild(box);
      group.forEach(function (x) {
        var o = el('div', 'pal-o', '<span class="lbl"' + (x.raw ? ' data-no-tx' : '') + '>' + hl(x.label, ws) + '</span><span class="pal-m"></span>');
        o.id = 'po-' + items.length;
        o.dataset.i = String(items.length);
        o.setAttribute('role', 'option');
        o.setAttribute('aria-selected', 'false');
        o.querySelector('.pal-m').textContent = x.meta || '';
        o.insertBefore(lead(x), o.firstChild);
        items.push(x);
        box.appendChild(o);
      });
    });
    q.setAttribute('aria-expanded', items.length ? 'true' : 'false');
    if (!items.length) {
      var none = el('div', 'pal-empty');
      none.textContent = 'Nothing matches "' + q.value.trim() + '".';
      list.appendChild(none);
    }
    select(0, false);
  }
  function adminGet(path) {
    return fetch(path, {headers: {'Accept': 'application/json'}, credentials: 'same-origin'}).then(function (r) {
      if (!r.ok) throw r.status;
      return r.json();
    });
  }
  function askOwned() {
    if (owned) return Promise.resolve(owned);
    if (!ownedAsk) ownedAsk = adminGet('/api/admin/features').then(function (d) {
      owned = (d && Array.isArray(d.items) ? d.items : []).filter(function (f) { return f && typeof f.key === 'string'; }).map(function (f) { return {key: f.key, label: typeof f.label === 'string' ? f.label : ''}; });
    }, function () {}).then(function () {
      ownedAsk = null;
      ownedTried = true;
      if (!pal.hidden) palRender();
      return owned;
    });
    return ownedAsk;
  }
  function loadSettings() {
    if (!me.admin) return;
    askOwned();
    if (asked) return;
    asked = true;
    adminGet('/api/admin/settings').then(function (d) {
      sets = (d && Array.isArray(d.items) ? d.items : []).filter(function (i) { return i && typeof i.label === 'string' && i.label && typeof i.group === 'string'; });
    }, function () { asked = false; }).then(function () {
      if (!pal.hidden) palRender();
    });
  }
  function modalOpen() {
    return [].some.call(document.querySelectorAll('[aria-modal="true"], dialog[open], .modal-wrap'), function (d) { return !d.closest('[hidden], #pal, #int-wrap'); });
  }
  function palOpen(from) {
    if (!pal.hidden || modalOpen()) return;
    back = from && from.focus ? from : document.activeElement;
    shut(false);
    if (window.__skopaxTipHide) window.__skopaxTipHide();
    pal.hidden = false;
    q.value = '';
    palRender();
    q.focus();
    loadSettings();
  }
  function palClose(restore) {
    if (pal.hidden) return;
    pal.hidden = true;
    q.removeAttribute('aria-activedescendant');
    q.setAttribute('aria-expanded', 'false');
    var to = back;
    back = null;
    if (restore !== false && to && to.focus && document.contains(to) && !(to.closest && to.closest('[inert]'))) to.focus();
  }
  function go(x) {
    if (!x) return;
    palClose(false);
    if (typeof ADMIN_REVEAL !== 'undefined') ADMIN_REVEAL = null;
    x.run();
    if (!x.find && ADMIN_TABS.indexOf(curTab) >= 0) clearAdminFind();
  }

  document.addEventListener('keydown', function (e) {
    if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && (e.key === 'k' || e.key === 'K' || e.code === 'KeyK')) {
      e.preventDefault();
      e.stopPropagation();
      if (pal.hidden) palOpen(); else palClose();
      return;
    }
    if (pal.hidden) return;
    e.stopPropagation();
    if (e.key === 'Escape') { e.preventDefault(); palClose(); return; }
    if (e.key === 'Tab') { e.preventDefault(); q.focus(); return; }
    if (document.activeElement !== q) q.focus();
    if (e.key === 'ArrowDown') { e.preventDefault(); select(at + 1, true); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); select(at - 1, true); }
    else if (e.key === 'Enter') { e.preventDefault(); go(items[at]); }
  }, true);
  q.addEventListener('input', palRender);
  pal.querySelector('.pal-box').addEventListener('mousedown', function (e) { if (e.target !== q) e.preventDefault(); });
  list.addEventListener('click', function (e) { var o = e.target.closest('[role="option"]'); if (o) go(items[+o.dataset.i]); });
  list.addEventListener('mousemove', function (e) { var o = e.target.closest('[role="option"]'); if (o && +o.dataset.i !== at) select(+o.dataset.i, false); });
  document.getElementById('pal-scrim').addEventListener('click', function () { palClose(); });
  search.addEventListener('click', function () { palOpen(search); });
  search.querySelector('kbd').textContent = KEY;
  search.title = 'Search (' + KEY + ')';
  search.setAttribute('aria-label', 'Search (' + KEY + ')');
  search.setAttribute('aria-keyshortcuts', MAC ? 'Meta+K' : 'Control+K');
  pal.querySelector('.pal-in').insertAdjacentHTML('afterbegin', navIcon('search'));

  window.instRender = instRender;
  window.palOpen = palOpen;
  window.palClose = palClose;
  window.palRender = palRender;
})();
