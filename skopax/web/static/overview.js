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
    var title = g.label + ': ' + ks.map(function (k) { return SRC_TAB[k][0] + ' ' + srcText(k); }).join(' \u00b7 ') + ' \u00b7 Last check ' + lastPassAt();
    pills += '<button type="button" class="sg-pill ' + kk + '" data-grp="' + g.id + '" aria-expanded="' + on + '" aria-controls="sg-' + g.id + '" title="' + esc(title) + '">' + esc(g.label) +
      ' <span class="count">' + (ks.length - bad.length) + '/' + ks.length + '</span>' + (bad.length ? '<span class="sg-bad">' + esc(badTxt) + '</span>' : '') + '</button>';
    bodies += '<div class="sg-body" id="sg-' + g.id + '" data-grp="' + g.id + '"' + (on ? '' : ' hidden') + '><span class="sg-l">' + esc(g.label) + '</span>' + ks.map(srcTile).join('') + '</div>';
  });
  var meta = document.getElementById('meta');
  meta.innerHTML = pills ? '<div class="sources"><div class="sg-pills">' + pills + '</div>' + bodies + '</div>' : '';
  meta.hidden = !pills;
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
  var lvMap = {ok: ['All clear', 'k-ok'], info: ['Info', 'k-info'], warn: ['Warning', 'k-warn'], high: ['High', 'k-high'], crit: ['Critical', 'k-crit']};
  var L = lvMap[lv] || lvMap.info;
  var dot = {ok: '#1f9d55', info: '#2f6fe0', warn: '#e9b308', high: '#f97316', crit: '#dc2626'}[lv];
  var icon = "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><path d='M1.55 13.82 10.4 1 14.8 1 3.05 14.98ZM12.95 14.98 1.2 1 5.6 1 14.45 13.82Z' fill='#3cc8f0'/><path d='M6.25 7.01 8 4.48 9.75 7.01 8 9.09Z' fill='#b3eefc'/>" +
    (dot ? "<circle cx='12.5' cy='3.5' r='3.5' fill='" + dot + "' stroke='#14171c' stroke-width='1.2'/>" : '') + '</svg>';
  document.getElementById('favicon').href = 'data:image/svg+xml,' + encodeURIComponent(icon);
  var sevTxt = CARD_SEVS.map(function (s) { var n = briefOpen.filter(function (a) { return a.sev === s; }).length; return n ? n + ' ' + SEV[s][0].toLowerCase() : ''; }).filter(Boolean).join(' · ');
  var links = (briefOpen.length ? '<button type="button" class="xlink" data-open-anoms title="Show the open cards">' + esc(sevTxt || openTxt) + ' open</button>' : '<span>Nothing open</span>') +
    (D.summary && /\brelease\b/i.test(D.summary.headline || '') ? ' · <button type="button" class="xlink" data-reps>Deploy reports ›</button>' : '');
  r.innerHTML = '<div class="brief-top"><span class="sev ' + L[1] + '" title="' + esc(BRIEF_BY) + '">' + esc(L[0]) + '</span><span class="brief-when">' + links + '</span></div>';
  r.querySelectorAll('[data-reps]').forEach(function (b) { b.addEventListener('click', function () { reports.current(); }); });
  r.querySelectorAll('[data-open-anoms]').forEach(function (b) { b.addEventListener('click', function () { showTab('anoms'); setFilter('open'); }); });
}

function noteName(t) {
  var known = whoPeople(A.concat(ARCHIVED));
  return fKinds(t).map(function (ps) {
    if (ps.length < 2) return typeName(ps[0]);
    return ps.map(function (p) { return isPersonF(p) && known[p.slice(6)] ? known[p.slice(6)].name : typeName(p); }).join(' ' + tx('or') + ' ');
  }).join(' \u00b7 ');
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
function setType(t) {
  typeF = t || '';
  setFilter(filter === 'svc' && !svcF ? 'open' : filter);
}
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
  if (cardUrlHeld()) return;
  CARD_URL = null;
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
    var picks = String(given || '').split('+');
    typeF = given && picks.every(function (p) { return /^(tag:|who:(u:)?)?[a-z0-9.-]{1,60}$/.test(p); }) ? picks.slice(0, F_MAX).join('+') : '';
    showTab(id);
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
  var svcTop = CARD_SEVS.filter(function (v) { return bad.some(function (s) { return s.status === v; }); })[0], svcK = svcTop ? SEV[svcTop][1] : 'k-info';
  var badTip = bad.slice(0, 5).map(function (s) { return s.svc + ' (' + (SEV[s.status] || SEV.info)[0] + ')'; }).join(', ') + (bad.length > 5 ? ', +' + (bad.length - 5) + ' more' : '');
  var openNow = openAll(), openTop = CARD_SEVS.filter(function (s) { return openNow.some(function (a) { return healthCard(a) && a.sev === s; }); })[0];
  var anomTip = [[openBySev('crit').length, 'critical'], [openBySev('high').length, 'high'], [openBySev('warn').length, 'warning']].filter(function (x) { return x[0]; }).map(function (x) { return x[0] + ' ' + x[1]; });
  if (!openAll().length) anomTip.push('nothing open');
  var T = {
    anoms: [String(openNow.length), openTop ? SEV[openTop][1] : SEV[worst][1], !!openNow.length, anomTip.join(' · ')],
    svcs: [S.length ? svcOk + '/' + S.length : '–', svcK, !!bad.length, !S.length ? 'no services yet' : bad.length ? bad.length + ' of ' + S.length + ' not healthy: ' + badTip : 'all ' + S.length + ' healthy'],
    info: [infoFiring().length, 'k-info', false, infoFiring().length ? plural(infoFiring().length, 'item', 'items') + ' still firing' : 'nothing firing'],
    lat: [(D.routes || []).length, 'k-info', false, ''],
    hist: [H.length, 'k-cleared', false, ''],
    slack: ['', 'k-info', false, ''],
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
  if (ADMIN_TABS.indexOf(id) < 0) save('skopax-tab', id);
  var moved = curTab !== null && curTab !== id;
  if (moved && curTab === 'access' && typeof clearAccessSecret === 'function') clearAccessSecret();
  if (moved && curTab === 'integrations' && typeof intLeave === 'function') intLeave();
  if (moved && id === 'prefs' && typeof ntMailAgain === 'function') ntMailAgain();
  curTab = id;
  if (moved && typeof window.__skopaxPoll === 'function') window.__skopaxPoll();
  var bare = ADMIN_TABS.indexOf(id) >= 0 || id === 'chat' || id === 'integrations';
  document.getElementById('reco').hidden = bare;
  document.getElementById('reco').parentNode.hidden = bare;
  var live = document.getElementById('live');
  if (live) { live.hidden = bare; live.parentNode.classList.toggle('no-live', bare); }
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
  if (id === 'docs' && typeof docsShow === 'function') docsShow();
  if (ADMIN_TABS.indexOf(id) >= 0 && typeof adminShow === 'function') adminShow(id);
  if (id === 'integrations' && typeof intShow === 'function') intShow();
}

function renderFilters() {
  var triCount = function (t) { return A.filter(function (a) { return a.triage === t; }).length; };
  CH = [
    {id: 'all', label: 'All', k: 'k-act', n: A.length, seg: 'anom-chips'},
    {id: 'open', label: 'Open', k: SEV[worst][1], n: openAll().length, seg: 'anom-chips', tip: 'firing, or stopped with nobody acting on it yet'},
    {id: 'progress', label: TRI.progress[0], k: TRI.progress[1], n: inprog.length, seg: 'anom-chips'},
    {id: 'taken', label: 'Done', k: 'k-good', n: taken.length, seg: 'anom-chips'},
    {id: 'muted', label: 'Muted', k: 'k-muted', n: muted.length, seg: 'anom-chips'},
    {id: 'cleared', label: 'Stopped firing', k: 'k-cleared', n: cleared.length, seg: 'anom-chips'},
    {id: 'investigating', label: 'Investigating', k: 'k-act', n: triCount('investigating'), seg: 'tri-chips'},
    {id: 'dived', label: 'Investigated', k: 'k-cleared', n: triCount('dived'), seg: 'tri-chips'},
    {id: 'crit', label: 'Critical', k: 'k-crit', n: openBySev('crit').length, seg: 'sev-chips', tip: 'open critical items (pushed)'},
    {id: 'high', label: 'High', k: 'k-high', n: openBySev('high').length, seg: 'sev-chips', tip: 'open high items (pushed)'},
    {id: 'warn', label: 'Warning', k: 'k-warn', n: openBySev('warn').length, seg: 'sev-chips', tip: 'open warning items (pushed)'},
    {id: 'recurring', label: 'Recurring', k: 'k-warn', n: recurringL.length, seg: 'list-chips', whenAny: true, tip: 'cards that keep coming back with no Jira ticket on them, whatever their status'},
    {id: 'archived', label: 'Archived', k: 'k-cleared', n: ARCHIVED.length, seg: 'list-chips', tip: 'cards not firing (or muted) with no change for ' + archiveHours() + ' hours; hidden everywhere else'},
    {id: 'closed', label: 'Closed', k: 'k-cleared', n: CLOSED.data ? CLOSED.data.total : '', seg: 'list-chips', tip: 'cards that already left the page, kept for a year; the count is for the time range picked in the list'}
  ];
  if (svcF) CH.push({id: 'svc', label: svcF.svc + ' cards', k: 'k-act', n: svcCards(svcF).list.length});
  renderQuick();
}
function renderFilterNote(c) {
  var note = document.getElementById('filter-note');
  var f = c ? c.id : 'open';
  note.className = 'filter-note ' + (c ? c.k : SEV[worst][1]) + (f !== 'open' || typeF || searchQ ? ' on' : '');
  note.title = [[openBySev('crit').length, 'critical'], [openBySev('high').length, 'high'], [openBySev('warn').length, 'warning'], [inprog.length, 'in progress'], [taken.length, 'done'], [muted.length, 'muted'], [cleared.length, 'stopped firing'], [recurringL.length, 'recurring']].filter(function (x) { return x[0]; }).map(function (x) { return x[0] + ' ' + x[1]; }).join(' · ') || 'nothing on the page';
  note.innerHTML = '<span class="fn-cur">Showing <b>' + esc(c ? c.label : 'Open') + '</b>' + (typeF ? ' · <b>' + esc(noteName(typeF)) + '</b>' : '') +
    '<span class="count">' + esc(typeF ? shownN : c ? c.n : openAll().length) + '</span></span>' +
    (f === 'svc' ? '<button class="xlink" type="button" id="back-svcs">← Back to Services</button>' : '') +
    (typeF ? '<button class="xlink" type="button" id="clear-type">Every type ›</button>' : '') +
    (f !== 'svc' && (f !== 'open' || typeF) ? '<button class="xlink copy-f" type="button" id="copy-f" title="Copies a link that opens Board with this filter' + (typeF ? ' and type' : '') + '">' + ICON_LINK + 'Copy link</button>' : '') +
    (f !== 'open' && (typeF || searchQ) ? '<button class="xlink" type="button" id="clear-f">Back to Open ›</button>' : '') +
    (f !== 'all' && (f !== 'open' || typeF || searchQ) ? '<button class="xlink fn-all" type="button" id="all-f">All</button>' : '') +
    (f !== 'open' || typeF || searchQ ? '<button class="xlink fn-clear" type="button" id="clear-all">Clear filters \u2715</button>' : '');
  var back = document.getElementById('clear-f'), allf = document.getElementById('all-f'), clearAll = document.getElementById('clear-all');
  if (back) back.addEventListener('click', function () { setFilter('open'); });
  if (allf) allf.addEventListener('click', function () { setFilter('all'); });
  if (clearAll) clearAll.addEventListener('click', clearBoardFilters);
  if (typeF) document.getElementById('clear-type').addEventListener('click', function () { setType(''); });
  var copyF = document.getElementById('copy-f');
  if (copyF) copyF.addEventListener('click', function () { copyUrl(location.origin + location.pathname + '#t=' + tabPath('anoms'), copyF); });
  if (f === 'svc') document.getElementById('back-svcs').addEventListener('click', backToSvcs);
}
function clearBoardFilters() {
  clearCardFilters();
  setFilter('open');
}
function boardFiltersOn() { return filter !== 'open' || !!typeF || !!searchQ; }
document.addEventListener('keydown', function (e) {
  if (e.key !== 'Escape' || e.defaultPrevented || curTab !== 'anoms' || !boardFiltersOn()) return;
  var t = e.target, tag = t && t.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (t && t.isContentEditable)) return;
  if (document.body.classList.contains('drawer-open') || (typeof DRAWER !== 'undefined' && DRAWER) || [].some.call(document.querySelectorAll('.modal, .pal-box, #cf-wrap, #int-wrap, [role="dialog"], [role="menu"]'), function (x) { return !x.closest('[hidden]'); })) return;
  e.preventDefault();
  clearBoardFilters();
});
function setFilter(f) {
  filter = f === 'stale' ? 'open' : f;
  renderQuick();
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
