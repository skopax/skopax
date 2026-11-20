function srcDest(k) {
  var tab = SRC_TAB[k] ? SRC_TAB[k][1] : 'integrations';
  var t = TABS.filter(function (x) { return x.id === tab; })[0] || TABS.filter(function (x) { return x.id === 'integrations'; })[0];
  if (t.id === 'integrations' && typeof INT_SIGNED !== 'undefined' && !INT_SIGNED) return null;
  return ADMIN_TABS.indexOf(t.id) >= 0 && typeof ADMIN_OK !== 'undefined' && !ADMIN_OK ? null : t;
}
function openSource(k) {
  var t = srcDest(k);
  if (!t) return;
  if (t.id === 'svcs') kind = 'all';
  showTab(t.id);
  if (t.id === 'svcs') renderSvcs();
}
var srcOpen = {};
var INST_TEXT = {ok: 'ok', warn: 'no access', error: 'error', pending: 'pending', none: 'no checks', off: 'off'};
function instTip(i, st) {
  if (st === 'warn') return instWarn(i.status) ? String(i.status) : instSaid(i);
  if (st === 'error') return instSaid(i) || INST_TEXT.error;
  return i.state === 'off' ? instSaid(i) : INST_TEXT[st];
}
function instSpan(i) {
  var st = instState(i), said = st === 'warn' ? instWarnText(i) : i.state === 'off' ? INST_TEXT.off : INST_TEXT[st];
  return '<span class="src-i src-i-' + st + '" data-name="' + esc(i.name || '') + '" data-state="' + st + '" title="' + esc(instTip(i, st)) + '">' + esc((i.name ? i.name + ' ' : '') + said) + '</span>';
}
function srcText(k) {
  var st = srcState(k), t = TILES_BY_ID[k], warned = st === 'warn' && t ? (t.instances || []).filter(function (i) { return instState(i) === 'warn'; })[0] : null;
  return warned ? instWarnText(warned) : INST_TEXT[st];
}
function srcTile(k) {
  var t = TILES_BY_ID[k], st = srcState(k), name = SRC_TAB[k][0];
  var kk = st === 'ok' ? 'k-ok' : st === 'pending' || st === 'warn' ? 'k-warn' : st === 'none' ? 'k-muted' : 'k-crit';
  var dest = srcDest(k);
  var title = esc(name + (dest ? ' \u00b7 opens ' + (ADMIN_TABS.indexOf(dest.id) >= 0 ? 'Admin \u203a ' : '') + dest.label : ''));
  var icon = t.icon ? '<img class="src-icon" src="' + esc(t.icon) + '" alt="" width="14" height="14">' : '';
  var queued = t.queued ? '<span class="src-q" title="A check was asked to run now; it starts when a run slot is free">queued</span>' : '';
  return '<button type="button" class="src ' + kk + '" data-src="' + esc(k) + '" title="' + title + '">' + icon + '<span class="src-n">' + esc(name) + '</span>' + queued + (t.instances || []).map(instSpan).join('') + '</button>';
}
function renderMeta() {
  var pills = '', bodies = '';
  SOURCE_GROUPS.forEach(function (g) {
    var ks = g.srcs;
    if (!ks.length) return;
    var bad = ks.filter(function (k) { return srcState(k) === 'error' || srcState(k) === 'warn' || srcState(k) === 'pending'; });
    var kk = bad.some(function (k) { return srcState(k) === 'error'; }) ? 'k-crit' : bad.length ? 'k-warn' : ks.every(function (k) { return srcState(k) === 'none'; }) ? 'k-muted' : 'k-ok';
    var on = srcOpen[g.id] != null ? srcOpen[g.id] : kk === 'k-crit';
    var badTxt = bad.slice(0, 2).map(function (k) { return SRC_TAB[k][0] + ' ' + srcText(k); }).join(', ') + (bad.length > 2 ? ' +' + (bad.length - 2) : '');
    var title = g.label + ': ' + ks.map(function (k) { return SRC_TAB[k][0] + ' ' + srcText(k); }).join(' \u00b7 ');
    pills += '<button type="button" class="sg-pill ' + kk + '" data-grp="' + g.id + '" aria-expanded="' + on + '" aria-controls="sg-' + g.id + '" title="' + esc(title) + '">' + esc(g.label) +
      ' <span class="count">' + (ks.length - bad.length) + '/' + ks.length + '</span>' + (bad.length ? '<span class="sg-bad">' + esc(badTxt) + '</span>' : '') + '</button>';
    bodies += '<div class="sg-body" id="sg-' + g.id + '" data-grp="' + g.id + '"' + (on ? '' : ' hidden') + '><span class="sg-l">' + esc(g.label) + '</span>' + ks.map(srcTile).join('') + '</div>';
  });
  var meta = document.getElementById('meta');
  meta.innerHTML = pills ? '<div class="sources"><div class="sg-pills">' + pills + '</div>' + bodies + '</div>' : '';
  meta.hidden = !pills;
  var last = document.getElementById('last-check');
  if (last) last.innerHTML = '<span>Last check<b>' + esc(lastPassAt()) + '</b></span>';
  document.querySelectorAll('#meta .sg-pill').forEach(function (b) {
    b.addEventListener('click', function () {
      var body = document.getElementById('sg-' + b.dataset.grp), on = body.hidden;
      body.hidden = !on;
      b.setAttribute('aria-expanded', on ? 'true' : 'false');
      srcOpen[b.dataset.grp] = on;
    });
  });
  document.querySelectorAll('#meta button.src').forEach(function (b) { b.addEventListener('click', function () { openSource(b.dataset.src); }); });
}
function lastPassAt() {
  var p = D.serverPasses || {}, at = 0;
  Object.keys(p).forEach(function (k) { var t = Date.parse((p[k] || {}).at || ''); if (t > at) at = t; });
  return at ? hm(new Date(at).toISOString()) : '–';
}
function liveness() {
  var inv = INV_INFO || {};
  var off = !!inv.stopped;
  document.querySelector('.page').classList.toggle('is-off', off);
  var tag = document.getElementById('off-tag');
  tag.hidden = !off;
  if (!tag.querySelector('.ot-lead')) tag.innerHTML = '<span class="ot-lead">INVESTIGATOR </span><span>STOPPED</span>';
  tag.title = 'The checks still run; only the investigator is stopped. Resume under Actions › Investigator.';
  var b = document.getElementById('banner');
  if (off) {
    b.className = 'banner k-warn';
    b.innerHTML = '<b>Investigator stopped</b>' + (inv.stopped.by ? ' · by ' + esc(inv.stopped.by) + ' ' + esc(dayhm(inv.stopped.at)) : '') + ' · the checks still run; <a href="#t=acts">Resume under Actions › Investigator</a>';
    b.hidden = false;
  } else if (down.length) {
    b.className = 'banner k-warn';
    b.innerHTML = '<b>Not every source answered on the last check.</b> ' + down.map(function (k) { return esc(SRC_TAB[k][0]) + ': ' + esc(srcDetail(k).replace(/\.+$/, '')); }).join(' \u00b7 ') + '. Cards from those sources keep their last state until they answer again.';
    b.hidden = false;
  } else {
    b.hidden = true;
  }
}

var BRIEF_BY = 'The state after the last server pass, from the open cards. The links open the lists; the cards hold the evidence.';
function renderBrief() {
  var r = document.getElementById('reco');
  var briefOpen = openAll().filter(healthCard);
  var openTxt = briefOpen.length ? briefOpen.length + ' open' : 'Nothing open';
  var lv = D.summary && D.summary.level ? D.summary.level : worst;
  var lvMap = {ok: ['All clear', 'k-ok'], info: ['Info', 'k-info'], warn: ['Warning', 'k-warn'], crit: ['Critical', 'k-crit']};
  var L = lvMap[lv] || lvMap.info;
  var dot = {ok: '#1f9d55', info: '#2f6fe0', warn: '#e08a00', crit: '#dc2626'}[lv];
  var icon = "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><path d='M1.55 13.82 10.4 1 14.8 1 3.05 14.98ZM12.95 14.98 1.2 1 5.6 1 14.45 13.82Z' fill='#3cc8f0'/><path d='M6.25 7.01 8 4.48 9.75 7.01 8 9.09Z' fill='#b3eefc'/>" +
    (dot ? "<circle cx='12.5' cy='3.5' r='3.5' fill='" + dot + "' stroke='#14171c' stroke-width='1.2'/>" : '') + '</svg>';
  document.getElementById('favicon').href = 'data:image/svg+xml,' + encodeURIComponent(icon);
  var sevTxt = ['crit', 'warn'].map(function (s) { var n = briefOpen.filter(function (a) { return a.sev === s; }).length; return n ? n + ' ' + SEV[s][0].toLowerCase() : ''; }).filter(Boolean).join(' · ');
  var links = (briefOpen.length ? '<button type="button" class="xlink" data-open-anoms title="Show the open cards">' + esc(sevTxt || openTxt) + ' open</button>' : '<span>Nothing open</span>') +
    (D.summary && /\brelease\b/i.test(D.summary.headline || '') ? ' · <button type="button" class="xlink" data-reps>Deploy reports ›</button>' : '');
  r.innerHTML = '<div class="brief-top"><span class="sev ' + L[1] + '" title="' + esc(BRIEF_BY) + '">' + esc(L[0]) + '</span><span class="brief-when">' + links + '</span></div>';
  r.querySelectorAll('[data-reps]').forEach(function (b) { b.addEventListener('click', function () { reports.current(); }); });
  r.querySelectorAll('[data-open-anoms]').forEach(function (b) { b.addEventListener('click', function () { showTab('anoms'); setFilter('open'); }); });
}

var tabsEl = document.getElementById('tabs');
(function () {
  var box = el('div', 'tab-grp');
  box.id = 'tabs-anomalies';
  box.setAttribute('role', 'presentation');
  var row = el('div', 'tab-row');
  row.setAttribute('role', 'presentation');
  row.appendChild(el('span', 'tabs-l filters-l', 'Anomaly filters'));
  var fold = el('button', 'tab-fold');
  fold.type = 'button'; fold.id = 'tab-fold-anoms';
  fold.setAttribute('aria-controls', 'tab-sub-anoms'); fold.setAttribute('aria-expanded', 'false'); fold.setAttribute('aria-label', 'Anomaly filters');
  fold.title = 'Show or hide the anomaly filters';
  fold.addEventListener('click', function () { setAnomSub(!anomSubOpen); });
  row.appendChild(fold);
  box.appendChild(row);
  var sub = el('div', 'tab-sub');
  sub.id = 'tab-sub-anoms'; sub.hidden = true;
  sub.setAttribute('role', 'group'); sub.setAttribute('aria-label', 'Anomaly filters');
  box.appendChild(sub);
  tabsEl.appendChild(box);
})();
var anomSubOpen = false;
function setAnomSub(on) {
  anomSubOpen = on;
  document.getElementById('tab-sub-anoms').hidden = !on;
  document.getElementById('tab-fold-anoms').setAttribute('aria-expanded', on ? 'true' : 'false');
}
function renderAnomSub() {
  var sub = document.getElementById('tab-sub-anoms');
  sub.innerHTML = '';
  [['anom-chips', '', ''], ['list-chips', 'Lists', 'lists'], ['tri-chips', 'Triage', 'triage']].forEach(function (g) {
    var list = CH.filter(function (c) { return c.seg === g[0] && ((g[0] !== 'tri-chips' && !c.whenAny) || c.n || c.id === filter); });
    if (!list.length) return;
    var box = el('div', 'tab-sub-g');
    box.setAttribute('role', 'presentation');
    if (g[1]) box.appendChild(subHead(g[2], g[1], box));
    var rows = [];
    list.forEach(function (c) { rows.push(c); if (c.id === 'open') CH.forEach(function (x) { if (x.seg === 'sev-chips') rows.push(x); }); });
    rows.forEach(function (c) {
      if (g[2] && subFolded(g[2]) && c.id !== filter) return;
      var b = el('button', 'tab-sub-i ' + c.k + (c.n ? '' : ' zero') + (c.seg === 'sev-chips' ? ' child' : ''), '<span class="lbl">' + esc(c.chip || c.label) + '</span><span class="count">' + esc(c.n) + '</span>');
      b.type = 'button'; b.dataset.f = c.id;
      b.setAttribute('aria-pressed', c.id === filter ? 'true' : 'false');
      b.title = (c.tip ? c.tip + ' \u00b7 ' : '') + 'opens Board filtered to ' + c.label + (c.seg !== 'anom-chips' ? ', click again for all open' : '');
      b.addEventListener('click', function () {
        var back = curTab === 'anoms' && c.id === filter && c.seg !== 'anom-chips';
        showTab('anoms');
        setFilter(back ? 'open' : c.id);
      });
      box.appendChild(b);
    });
    sub.appendChild(box);
  });
  var counts = {}, label = (CH.filter(function (c) { return c.id === filter; })[0] || {label: 'Open'}).label;
  filterBase(filter).forEach(function (a) { var t = typeOf(a); counts[t] = (counts[t] || 0) + 1; });
  fParts(typeF).filter(function (p) { return fKind(p) === 'type'; }).forEach(function (p) { if (!counts[p]) counts[p] = 0; });
  var types = Object.keys(counts).sort(function (x, y) { return counts[y] - counts[x] || (typeName(x) < typeName(y) ? -1 : 1); });
  sub.appendChild(whoRow(label));
  sub.appendChild(tagsRow(label));
  if (!types.length) return;
  var tbox = el('div', 'tab-sub-g types-g' + (typesFold ? ' folded' : ''));
  tbox.setAttribute('role', 'presentation');
  var head = el('button', 'tab-sub-l types-fold', 'Types <span class="types-n">' + types.length + '</span><span class="types-chev" aria-hidden="true"></span>');
  head.type = 'button'; head.dataset.fold = 'types';
  head.setAttribute('aria-expanded', typesFold ? 'false' : 'true');
  head.title = (typesFold ? 'Show' : 'Hide') + ' the ' + types.length + ' card types under ' + label;
  head.addEventListener('click', function () {
    typesFold = !typesFold;
    save('skopax-types-fold', typesFold ? '1' : '0');
    renderAnomSub();
    var again = document.querySelector('#tab-sub-anoms [data-fold="types"]');
    if (again) again.focus();
  });
  tbox.appendChild(head);
  types.forEach(function (t) {
    if (typesFold && !hasF(t)) return;
    var b = el('button', 'tab-sub-i k-act' + (counts[t] ? '' : ' zero'), '<span class="lbl">' + esc(typeName(t)) + '</span><span class="count">' + esc(counts[t]) + '</span>');
    b.type = 'button'; b.dataset.type = t;
    b.setAttribute('aria-pressed', hasF(t) ? 'true' : 'false');
    b.title = counts[t] + ' ' + typeName(t) + ' ' + (counts[t] === 1 ? 'card' : 'cards') + ' under ' + label + ' (signal ' + t + ') · ' + (hasF(t) ? 'click again for every type' : 'shows only them');
    b.addEventListener('click', function () {
      showTab('anoms');
      setType(toggledF(t));
    });
    tbox.appendChild(b);
  });
  sub.appendChild(tbox);
}
typesFold = load('skopax-types-fold') === '1';
var SUB_FOLD = (function () { try { return JSON.parse(load('skopax-sub-fold') || '{}') || {}; } catch (e) { return {}; } })();
function subFolded(id) { return !!SUB_FOLD[id]; }
function subHead(id, label, box) {
  var folded = subFolded(id);
  box.classList.toggle('folded', folded);
  var head = el('button', 'tab-sub-l types-fold', esc(label) + '<span class="types-chev" aria-hidden="true"></span>');
  head.type = 'button'; head.dataset.fold = id;
  head.setAttribute('aria-expanded', folded ? 'false' : 'true');
  head.title = (folded ? 'Show ' : 'Hide ') + label;
  head.addEventListener('click', function () {
    if (SUB_FOLD[id]) delete SUB_FOLD[id]; else SUB_FOLD[id] = 1;
    save('skopax-sub-fold', JSON.stringify(SUB_FOLD));
    renderAnomSub();
    var again = document.querySelector('#tab-sub-anoms [data-fold="' + id + '"]');
    if (again) again.focus();
  });
  return head;
}
function whoPeople(base) {
  var people = {};
  base.forEach(function (a) {
    var s = a.owner ? personSlug(a.owner) : '';
    if (!s) return;
    people[s] = people[s] || {n: 0, name: (a.assigned && a.assigned.name) || ownerName(a.owner)};
    people[s].n++;
  });
  fParts(typeF).filter(isPersonF).forEach(function (t) { if (!people[t.slice(6)]) people[t.slice(6)] = {n: 0, name: t.slice(6)}; });
  return people;
}
function whoRow(label) {
  var base = filterBase(filter);
  var box = el('div', 'tab-sub-g who-g');
  box.setAttribute('role', 'presentation');
  box.appendChild(subHead('who', 'Assignee', box));
  var folded = subFolded('who'), people = whoPeople(base);
  Object.keys(WHO_NAMES).forEach(function (t) {
    if (folded && !hasF(t)) return;
    var c = base.filter(function (a) { return typeIs(a, t); }).length;
    var b = el('button', 'tab-sub-i k-act' + (c ? '' : ' zero'), '<span class="lbl">' + esc(WHO_CHIPS[t]) + '</span><span class="count">' + esc(c) + '</span>');
    b.type = 'button'; b.dataset.type = t;
    b.setAttribute('aria-pressed', hasF(t) ? 'true' : 'false');
    b.title = c + ' ' + (c === 1 ? 'card' : 'cards') + ' under ' + label + (t === 'who:me' ? (ME_USER ? ' assigned to you' : ' assigned to you (log in to see them)') : t === 'who:any' ? ' with an assignee, whoever it is' : ' with no assignee') + ' · ' + (hasF(t) ? 'click again to drop this filter' : 'shows only them');
    b.addEventListener('click', function () {
      showTab('anoms');
      setType(toggledF(t));
    });
    box.appendChild(b);
  });
  Object.keys(people).sort(function (x, y) { return people[y].n - people[x].n || (x < y ? -1 : 1); }).forEach(function (s) {
    var t = 'who:u:' + s, p = people[s];
    if (folded && !hasF(t)) return;
    var b = el('button', 'tab-sub-i k-act child' + (p.n ? '' : ' zero'), '<span class="lbl">' + esc(p.name) + '</span><span class="count">' + esc(p.n) + '</span>');
    b.type = 'button'; b.dataset.type = t;
    b.setAttribute('aria-pressed', hasF(t) ? 'true' : 'false');
    b.title = p.n + ' ' + (p.n === 1 ? 'card' : 'cards') + ' under ' + label + ' assigned to ' + p.name + ' · ' + (hasF(t) ? 'click again for everyone' : 'shows only theirs');
    b.addEventListener('click', function () {
      showTab('anoms');
      setType(toggledF(t));
    });
    box.appendChild(b);
  });
  return box;
}
function tagsRow(label) {
  var n = {};
  filterBase(filter).forEach(function (a) { tagIds(a).forEach(function (t) { n[t] = (n[t] || 0) + 1; }); });
  var box = el('div', 'tab-sub-g tags-g');
  box.setAttribute('role', 'presentation');
  box.appendChild(subHead('tags', 'Tags', box));
  var folded = subFolded('tags');
  Object.keys(TAG_NAMES).forEach(function (id) {
    var t = 'tag:' + id, c = n[id] || 0;
    if (folded && !hasF(t)) return;
    var b = el('button', 'tab-sub-i k-act ' + tagClass(id) + (c ? '' : ' zero'), '<span class="lbl">' + esc(tagName(id)) + '</span><span class="count">' + esc(c) + '</span>');
    b.type = 'button'; b.dataset.type = t;
    b.setAttribute('aria-pressed', hasF(t) ? 'true' : 'false');
    b.title = c + ' ' + (c === 1 ? 'card' : 'cards') + ' tagged ' + tagName(id) + ' under ' + label + (TECH_TAGS[id] ? ': ' + TECH_TAGS[id] + ' to fix them · ' : ': they go to its Teams channel once they have a business sentence · ') + (hasF(t) ? 'click again to drop this filter' : 'shows only them');
    b.addEventListener('click', function () {
      showTab('anoms');
      setType(toggledF(t));
    });
    box.appendChild(b);
  });
  var u = UNTAGGED_F, un = filterBase(filter).filter(function (a) { return !tagIds(a).length; }).length;
  if (Object.keys(TAG_NAMES).length && (!folded || hasF(u))) {
    var ub = el('button', 'tab-sub-i k-act' + (un ? '' : ' zero'), '<span class="lbl">Untagged</span><span class="count">' + esc(un) + '</span>');
    ub.type = 'button'; ub.dataset.type = u;
    ub.setAttribute('aria-pressed', hasF(u) ? 'true' : 'false');
    ub.title = un + ' ' + (un === 1 ? 'card' : 'cards') + ' with no tag under ' + label + ' \u00b7 ' + (hasF(u) ? 'click again to drop this filter' : 'shows only them');
    ub.addEventListener('click', function () {
      showTab('anoms');
      setType(toggledF(u));
    });
    box.appendChild(ub);
  }
  return box;
}
function setType(t) {
  typeF = t || '';
  setFilter(filter === 'svc' && !svcF ? 'open' : filter);
}
var navBack = document.getElementById('nav-backdrop');
function navOpen() { return document.body.classList.contains('nav-open'); }
function setNav(on) {
  if (on === navOpen()) return;
  var opener = document.getElementById('filter-menu');
  document.body.classList.toggle('nav-open', on);
  if (opener) opener.setAttribute('aria-expanded', on ? 'true' : 'false');
  navBack.hidden = !on;
  if (on) { var sel = tabsEl.querySelector('button[aria-pressed="true"]') || tabsEl.querySelector('button:not(.nav-fold)'); if (sel) sel.focus(); }
  else if (tabsEl.contains(document.activeElement) && opener) opener.focus();
}
var NAV_OFF_KEY = 'skopax-nav-off', NAV_CHAT_KEY = 'skopax-nav-chat';
var layoutEl = document.querySelector('.layout');
var navHide = el('button', 'nav-fold', '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="11 17 6 12 11 7"></polyline><polyline points="18 17 13 12 18 7"></polyline></svg><span class="nf-l sr">Hide menu</span>');
navHide.type = 'button';
navHide.id = 'nav-fold';
navHide.setAttribute('aria-controls', 'tabs');
tabsEl.insertBefore(navHide, tabsEl.firstChild);
navHide.addEventListener('click', function () { setNavOff(!navOff()); });
function navSide() { return typeof sideWidth === 'function' ? sideWidth() : 0; }
function wideNav() { return !window.matchMedia || window.matchMedia('(min-width: ' + (901 + navSide()) + 'px)').matches; }
function narrowContent() { return !!window.matchMedia && window.matchMedia('(max-width: ' + (1239 + navSide()) + 'px)').matches; }
function navOff() {
  if (curTab === 'chat') return load(NAV_CHAT_KEY) !== 'on';
  var kept = load(NAV_OFF_KEY);
  return kept === '1' || kept === '0' ? kept === '1' : narrowContent();
}
function armNavAnim() {
  var arm = function () { layoutEl.classList.add('nav-anim'); };
  if (window.requestAnimationFrame) requestAnimationFrame(function () { requestAnimationFrame(arm); }); else setTimeout(arm, 50);
}
function applyNavOff() {
  var off = navOff(), wide = wideNav();
  layoutEl.classList.toggle('nav-off', off);
  document.body.classList.toggle('nav-off', off);
  if (window.__skopaxTipHide) window.__skopaxTipHide();
  navHide.setAttribute('aria-expanded', String(!off));
  navHide.querySelector('.nf-l').textContent = off ? 'Menu' : 'Hide menu';
  navHide.dataset.tip = navHide.title = off ? 'Show the menu' : 'Hide the menu';
  navHide.setAttribute('aria-label', off ? 'Show the menu' : 'Hide the menu');
  if (off && wide && tabsEl.contains(document.activeElement) && document.activeElement !== navHide) navHide.focus();
  if (!layoutEl.classList.contains('nav-anim')) armNavAnim();
}
function setNavOff(off) {
  if (curTab === 'chat') save(NAV_CHAT_KEY, off ? 'off' : 'on'); else save(NAV_OFF_KEY, off ? '1' : '0');
  applyNavOff();
}
var navResize = 0;
window.addEventListener('resize', function () {
  clearTimeout(navResize);
  navResize = setTimeout(function () { if (curTab) applyNavOff(); }, 120);
});
navBack.addEventListener('click', function () { setNav(false); });
document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && navOpen()) setNav(false); });
var curTab = null, urlReady = false;
function tabPath(id) {
  if (id === 'anoms') {
    var f = LINK_FILTERS.indexOf(filter) >= 0 ? filter : 'open';
    return typeF ? id + '/' + f + '/' + encodeURIComponent(typeF) : f === 'open' ? id : id + '/' + f;
  }
  if (id === 'svcs') return kind === 'all' ? id : id + '/' + kind;
  if (id === 'chat') return chatPath();
  if (id === 'access' && typeof ACCESS_TAB !== 'undefined' && ACCESS_TAB !== 'people') return id + '/' + ACCESS_TAB;
  return id;
}
function markTab() {
  if (!urlReady || !curTab) return;
  var h = '#t=' + tabPath(curTab), b = document.getElementById('tab-' + curTab);
  if (b) b.setAttribute('href', h);
  history.replaceState(null, '', location.pathname + location.search + h);
}
function defaultTab() { var s = load('skopax-tab'); return ADMIN_TABS.indexOf(s) < 0 && TABS.some(function (t) { return t.id === s; }) ? s : 'anoms'; }
function openTabPath(raw) {
  var p;
  try { p = decodeURIComponent(raw); } catch (e) { return; }
  var parts = p.split('/'), id = parts[0], sub = parts[1] || '';
  var note = document.getElementById('link-note');
  if (note) note.remove();
  var t = TABS.filter(function (x) { return x.id === id; })[0];
  if (!t) {
    var back = defaultTab();
    if (SIDE_PAGES === null && SIDE_PAGE_TAB.test(id)) SIDE_WANTED = {raw: raw, at: back};
    showTab(back);
    note = el('p', 'panel-note', 'No \u201c' + esc(p) + '\u201d page here: showing ' + esc(TABS.filter(function (x) { return x.id === back; })[0].label) + ' instead.');
    note.id = 'link-note';
    document.getElementById('panel-' + back).prepend(note);
    return;
  }
  if (id === 'svcs') { kind = KIND.some(function (c) { return c.id === sub; }) ? sub : 'all'; renderSvcs(); }
  if (id === 'anoms') {
    var known = LINK_FILTERS.indexOf(sub) >= 0, given = known ? parts[2] : sub;
    typeF = /^(tag:|who:(u:)?)?[a-z0-9.-]{1,60}(\+(tag:|who:(u:)?)?[a-z0-9.-]{1,60}){0,2}$/.test(given || '') ? given : '';
    showTab(id);
    renderAnomSub();
    setFilter(known ? sub : 'open');
    return;
  }
  if (id === 'chat') openChatPath(parts.slice(1));
  if (id === 'integrations' && typeof intWant === 'function') intWant(sub === 'mcp' ? parts[2] || '' : '', sub === 'signin-expired');
  if (id === 'access' && typeof accWant === 'function') accWant(sub);
  showTab(id);
}
var relCount = null;
function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }
function renderTabs() {
  var bad = S.filter(function (s) { return s.status !== 'ok'; });
  var svcK = bad.some(function (s) { return s.status === 'crit'; }) ? 'k-crit' : bad.some(function (s) { return s.status === 'warn'; }) ? 'k-warn' : 'k-info';
  var badTip = bad.slice(0, 5).map(function (s) { return s.svc + ' (' + (SEV[s.status] || SEV.info)[0] + ')'; }).join(', ') + (bad.length > 5 ? ', +' + (bad.length - 5) + ' more' : '');
  var openNow = openAll(), openTop = ['crit', 'warn'].filter(function (s) { return openNow.some(function (a) { return healthCard(a) && a.sev === s; }); })[0];
  var anomTip = [[openBySev('crit').length, 'critical'], [openBySev('warn').length, 'warning']].filter(function (x) { return x[0]; }).map(function (x) { return x[0] + ' ' + x[1]; });
  if (!openAll().length) anomTip.push('nothing open');
  var T = {
    anoms: [String(openNow.length), openTop ? SEV[openTop][1] : SEV[worst][1], !!openNow.length, anomTip.join(' · ')],
    svcs: [S.length ? svcOk + '/' + S.length : '–', svcK, !!bad.length, !S.length ? 'no services yet' : bad.length ? bad.length + ' of ' + S.length + ' not healthy: ' + badTip : 'all ' + S.length + ' healthy'],
    info: [infoFiring().length, 'k-info', false, infoFiring().length ? plural(infoFiring().length, 'item', 'items') + ' still firing' : 'nothing firing'],
    lat: [(D.routes || []).length, 'k-info', false, ''],
    hist: [H.length, 'k-cleared', false, ''],
    slack: slackTabState(),
    rels: deps.length ? [deps.length + ' deployed', 'k-act', true, plural(deps.length, 'service', 'services') + ' with a new image in the last 24 h: ' + deps.map(function (d) { return d.svc + clusterTag(d); }).join(', ')] :
      [relCount == null ? '–' : relCount, 'k-info', false, 'no service got a new image in the last 24 h']
  };
  Object.keys(T).forEach(function (id) {
    var b = document.getElementById('tab-' + id);
    b.className = 'tab ' + T[id][1] + (T[id][2] ? ' hot' : '');
    b.title = T[id][3];
    document.getElementById('count-' + id).textContent = String(T[id][0]);
  });
}
function renderRecent() {
  var box = document.getElementById('rels-recent');
  box.hidden = !deps.length;
  box.innerHTML = '';
  if (!deps.length) return;
  box.appendChild(el('span', null, '<b>' + esc(plural(deps.length, 'service', 'services')) + ' deployed in the last 24 h:</b> ' + esc(deps.map(function (d) { return d.svc + clusterTag(d); }).join(', ')) + ' '));
  var b = el('button', 'xlink', 'Show on Services ›');
  b.type = 'button';
  b.title = 'Opens Services, filtered to Deployed (24 h)';
  b.addEventListener('click', function () { kind = 'deployed'; showTab('svcs'); renderSvcs(); });
  box.appendChild(b);
}
function pickTab(id) {
  showTab(id);
  if (id !== 'anoms') return;
  typeF = '';
  setFilter('open');
}
function showTab(id) {
  if (id === 'costs' && COSTS_OP === false) id = 'anoms';
  if (ADMIN_TABS.indexOf(id) >= 0 && typeof ADMIN_OK !== 'undefined' && !ADMIN_OK) { ADMIN_WANTED = id; id = 'anoms'; }
  else if (id === 'integrations' && typeof INT_SIGNED !== 'undefined' && INT_SIGNED === false) { INT_WANTED = true; id = 'anoms'; }
  else if (typeof ADMIN_WANTED !== 'undefined') ADMIN_WANTED = false;
  TABS.forEach(function (t) {
    var panel = document.getElementById('panel-' + t.id);
    if (panel) panel.hidden = t.id !== id;
  });
  sideMark(id);
  layoutEl.classList.toggle('no-nav', id !== 'anoms');
  if (ADMIN_TABS.indexOf(id) < 0) save('skopax-tab', id);
  var moved = curTab !== null && curTab !== id;
  if (moved && curTab === 'access' && typeof clearAccessSecret === 'function') clearAccessSecret();
  if (moved && curTab === 'integrations' && typeof intLeave === 'function') intLeave();
  curTab = id;
  if (moved && typeof window.__skopaxPoll === 'function') window.__skopaxPoll();
  var bare = ADMIN_TABS.indexOf(id) >= 0 || id === 'chat' || id === 'integrations';
  document.getElementById('reco').hidden = bare;
  document.getElementById('reco').parentNode.hidden = bare;
  var live = document.getElementById('live');
  if (live) { live.hidden = bare; live.parentNode.classList.toggle('no-live', bare); }
  applyNavOff();
  document.getElementById('tabs-anomalies').hidden = id !== 'anoms';
  if (id === 'anoms') setAnomSub(true);
  setNav(false);
  markTab();
  if ((id === 'reps' || id === 'rels' || id === 'slack') && reports) reports.refresh();
  if (typeof ntTab === 'function') ntTab(id === 'slack');
  if (id === 'costs') loadCosts();
  if (id === 'tools') loadTools();
  if (id === 'mcp') loadMcpInfo();
  if (id === 'reqs') loadRequests();
  if (id === 'inv') loadInvestigator();
  if (id === 'chat') loadChat();
  if (id === 'review') loadReview();
  if (ADMIN_TABS.indexOf(id) >= 0 && typeof adminShow === 'function') adminShow(id);
  if (id === 'integrations' && typeof intShow === 'function') intShow();
}

function renderFilters() {
  var triCount = function (t) { return A.filter(function (a) { return a.triage === t; }).length; };
  CH = [
    {id: 'all', label: 'All', k: 'k-act', n: A.length, seg: 'anom-chips'},
    {id: 'open', label: 'Open', k: SEV[worst][1], n: openAll().length, seg: 'anom-chips', tip: 'firing, or stopped with nobody acting on it yet'},
    {id: 'progress', label: TRI.progress[0], k: TRI.progress[1], n: inprog.length, seg: 'anom-chips'},
    {id: 'taken', label: 'Action taken', k: 'k-good', n: taken.length, seg: 'anom-chips'},
    {id: 'muted', label: 'Muted', k: 'k-muted', n: muted.length, seg: 'anom-chips'},
    {id: 'cleared', label: 'Cleared', k: 'k-cleared', n: cleared.length, seg: 'anom-chips'},
    {id: 'investigating', label: 'Investigating', k: 'k-act', n: triCount('investigating'), seg: 'tri-chips'},
    {id: 'dived', label: 'Investigated', k: 'k-cleared', n: triCount('dived'), seg: 'tri-chips'},
    {id: 'crit', label: 'Critical', k: 'k-crit', n: openBySev('crit').length, seg: 'sev-chips', tip: 'open critical items (pushed)'},
    {id: 'warn', label: 'Warning', k: 'k-warn', n: openBySev('warn').length, seg: 'sev-chips', tip: 'open warning items (pushed)'},
    {id: 'recurring', label: 'Recurring', k: 'k-warn', n: recurringL.length, seg: 'list-chips', whenAny: true, tip: 'cards that keep coming back with no Jira ticket on them, whatever their status'},
    {id: 'archived', label: 'Archived', k: 'k-cleared', n: ARCHIVED.length, seg: 'list-chips', tip: 'cards not firing (or muted) with no change for ' + archiveHours() + ' hours; hidden everywhere else'},
    {id: 'closed', label: 'Closed', k: 'k-cleared', n: CLOSED.data ? CLOSED.data.total : '', seg: 'list-chips', tip: 'cards that already left the page, kept for a year; the count is for the time range picked in the list'}
  ];
  if (svcF) CH.push({id: 'svc', label: svcF.svc + ' cards', k: 'k-act', n: svcCards(svcF).list.length});
  renderAnomSub();
}
function openFilterMenu() {
  setAnomSub(true);
  setNav(true);
  var b = document.querySelector('#tab-sub-anoms button[aria-pressed="true"]') || document.querySelector('#tab-sub-anoms button');
  if (b) b.focus();
}
function renderFilterNote(c) {
  var note = document.getElementById('filter-note');
  var f = c ? c.id : 'open', refocus = document.activeElement && document.activeElement.id === 'filter-menu';
  note.className = 'filter-note ' + (c ? c.k : SEV[worst][1]) + (f !== 'open' || typeF ? ' on' : '');
  note.title = [[openBySev('crit').length, 'critical'], [openBySev('warn').length, 'warning'], [inprog.length, 'in progress'], [taken.length, 'action taken'], [muted.length, 'muted'], [cleared.length, 'cleared'], [recurringL.length, 'recurring']].filter(function (x) { return x[0]; }).map(function (x) { return x[0] + ' ' + x[1]; }).join(' · ') || 'nothing on the page';
  note.innerHTML = '<span class="fn-cur">Showing <b>' + esc(c ? c.label : 'Open') + '</b>' + (typeF ? ' · <b>' + esc(typeName(typeF)) + '</b>' : '') +
    '<span class="count">' + esc(typeF ? shownN : c ? c.n : openAll().length) + '</span></span>' +
    (f === 'svc' ? '<button class="xlink" type="button" id="back-svcs">← Back to Services</button>' : '') +
    (typeF ? '<button class="xlink" type="button" id="clear-type">Every type ›</button>' : '') +
    (f !== 'svc' && (f !== 'open' || typeF) ? '<button class="xlink copy-f" type="button" id="copy-f" title="Copies a link that opens Board with this filter' + (typeF ? ' and type' : '') + '">' + ICON_LINK + 'Copy link</button>' : '') +
    (f !== 'open' ? '<button class="xlink" type="button" id="clear-f">Back to Open ›</button>' : '') +
    '<button class="btn sec small with-icon fn-menu" type="button" id="filter-menu" aria-controls="tab-sub-anoms" title="Open the section menu at the anomaly filters"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"></polygon></svg>Filters</button>';
  if (f !== 'open') document.getElementById('clear-f').addEventListener('click', function () { setFilter('open'); });
  if (typeF) document.getElementById('clear-type').addEventListener('click', function () { setType(''); });
  var copyF = document.getElementById('copy-f');
  if (copyF) copyF.addEventListener('click', function () { copyUrl(location.origin + location.pathname + '#t=' + tabPath('anoms'), copyF); });
  if (f === 'svc') document.getElementById('back-svcs').addEventListener('click', backToSvcs);
  var menu = document.getElementById('filter-menu');
  menu.setAttribute('aria-expanded', navOpen() ? 'true' : 'false');
  menu.addEventListener('click', openFilterMenu);
  if (refocus) menu.focus();
}
function setFilter(f) {
  filter = f === 'stale' ? 'open' : f;
  var sub = document.getElementById('tab-sub-anoms'), had = sub && sub.contains(document.activeElement) ? document.activeElement.dataset : null;
  renderAnomSub();
  if (had) {
    var back = had.f ? sub.querySelector('button[data-f="' + had.f + '"]') : had.type ? sub.querySelector('button[data-type="' + had.type + '"]') : null;
    if (back) back.focus();
  }
  renderAnoms();
  renderFilterNote(CH.filter(function (x) { return x.id === f; })[0]);
  markTab();
  if (window.__skopaxRender) window.__skopaxRender();
}

function initAppearance() {
  var KEY = 'skopax-appearance';
  var DEFAULTS = {size: 'md', mode: 'system', density: 'comfortable', lang: 'auto'};
  var LANG_COOKIE = 'skopax_lang', langNow = null, LANG_SEGS = 5;
  var root = document.documentElement;
  var panel = document.getElementById('panel-prefs');
  var ap = Object.assign({}, DEFAULTS);
  try { Object.assign(ap, JSON.parse(load(KEY) || '{}')); } catch (e) {}
  function langPacks() {
    var all = window.SKOPAX_I18N, tag = new RegExp('^(?:' + SKOPAX_LANG_TAG + ')$');
    if (!all || typeof all !== 'object') return [];
    return Object.keys(all).filter(function (k) { return tag.test(k) && k.toLowerCase() !== 'en' && all[k] && typeof all[k] === 'object'; }).sort(function (a, b) { return a.toLowerCase() < b.toLowerCase() ? -1 : a.toLowerCase() > b.toLowerCase() ? 1 : 0; });
  }
  function langName(tag) {
    var name = '';
    try { name = new Intl.DisplayNames([tag], {type: 'language'}).of(tag) || ''; } catch (e) {}
    if (!name || name === tag) return tag;
    return name.charAt(0).toLocaleUpperCase(tag) + name.slice(1);
  }
  function langChoice() {
    var packs = langPacks();
    return {packs: packs, value: ap.lang === 'auto' || ap.lang === 'en' || packs.indexOf(ap.lang) >= 0 ? ap.lang : 'auto'};
  }
  function langRow() {
    var group = panel.querySelector('[data-ap="lang"]'), packs = langPacks(), sig = packs.join('|');
    if (!group || group.dataset.packs === sig) return;
    group.dataset.packs = sig;
    var entries = [['auto', 'Auto'], ['en', 'English']].concat(packs.map(function (t) { return [t, langName(t)]; }));
    group.textContent = '';
    if (entries.length <= LANG_SEGS) {
      entries.forEach(function (en) {
        var b = document.createElement('button');
        b.type = 'button';
        b.dataset.v = en[0];
        b.textContent = en[1];
        if (en[0] === 'auto') b.title = 'Follows your browser';
        else b.setAttribute('data-no-tx', '');
        group.appendChild(b);
      });
      return;
    }
    var sel = document.createElement('select');
    sel.className = 'ap-lang-select';
    entries.forEach(function (en) {
      var o = document.createElement('option');
      o.value = en[0];
      o.textContent = en[1];
      if (en[0] !== 'auto') o.setAttribute('data-no-tx', '');
      sel.appendChild(o);
    });
    group.appendChild(sel);
  }
  function apply() {
    langRow();
    var choice = langChoice();
    root.dataset.size = ap.size;
    if (ap.mode === 'system') delete root.dataset.theme; else root.dataset.theme = ap.mode;
    if (ap.density === 'compact') root.dataset.density = 'compact'; else delete root.dataset.density;
    panel.querySelectorAll('[data-ap] button').forEach(function (b) {
      var key = b.parentNode.dataset.ap;
      b.setAttribute('aria-pressed', String((key === 'lang' ? choice.value : ap[key]) === b.dataset.v));
    });
    var pick = panel.querySelector('[data-ap="lang"] select');
    if (pick) pick.value = choice.value;
    var lang = choice.value === 'auto' ? '' : choice.value;
    document.cookie = LANG_COOKIE + '=' + lang + '; path=/; max-age=' + (lang ? 31536000 : 0) + '; samesite=lax';
    var changed = langNow !== null && langNow !== lang;
    langNow = lang;
    if (typeof sideTheme === 'function') sideTheme(false);
    if (changed) location.reload();
  }
  panel.addEventListener('click', function (e) {
    var b = e.target.closest('[data-ap] button');
    if (!b) return;
    try { Object.assign(ap, JSON.parse(load(KEY) || '{}')); } catch (err) {}
    ap[b.parentNode.dataset.ap] = b.dataset.v;
    save(KEY, JSON.stringify(ap));
    apply();
  });
  panel.addEventListener('change', function (e) {
    var sel = e.target.closest('[data-ap] select');
    if (!sel) return;
    try { Object.assign(ap, JSON.parse(load(KEY) || '{}')); } catch (err) {}
    ap.lang = sel.value;
    save(KEY, JSON.stringify(ap));
    apply();
  });
  document.getElementById('ap-reset').addEventListener('click', function () {
    ap = Object.assign({}, DEFAULTS);
    try { localStorage.removeItem(KEY); } catch (e) {}
    apply();
  });
  apply();
}
function initCardSearch() {
  var box = document.getElementById('card-search');
  if (!box) return;
  box.addEventListener('input', function () {
    var was = searchQ;
    searchQ = box.value.trim();
    if (searchQ && !was) { searchBack = filter; setFilter('all'); return; }
    if (!searchQ && was) { setFilter(LINK_FILTERS.indexOf(searchBack) >= 0 ? searchBack : 'open'); return; }
    renderAnoms();
    if (window.__skopaxRender) window.__skopaxRender();
  });
  box.addEventListener('keydown', function (e) { if (e.key === 'Escape' && box.value) { box.value = ''; box.dispatchEvent(new Event('input')); } });
}
initCardSearch();
