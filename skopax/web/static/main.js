function renderHist() {
  var tb = document.querySelector('#hist tbody');
  tb.innerHTML = '';
  if (!H.length) tb.appendChild(el('tr', 'empty', '<td colspan="4">No events yet.</td>'));
  var EV = {new: 'k-warn', escalated: 'k-crit', reopened: 'k-warn', cleared: 'k-cleared'};
  H.forEach(function (e) {
    var tr = el('tr');
    tr.innerHTML = '<td class="when"><b>' + esc(dayhm(e.t)) + '</b></td><td><span class="tag ' + (EV[e.ev] || 'k-muted') + '">' + esc(e.ev) + '</span>' + (e.muted ? ' <span class="tag plain">muted</span>' : '') + '</td><td>' + state(e.sev) + '</td><td>' + esc(e.text) + '</td>';
    tb.appendChild(tr);
  });
}

function paint(data) {
  derive(data);
  renderMeta();
  liveness();
  renderBrief();
  renderTabs();
  renderRecent();
  renderFilters();
  renderSvcChips();
  renderSvcs();
  renderInfo();
  renderRoutes();
  renderHist();
  renderSlackQ();
  loadInvestigations();
  document.dispatchEvent(new CustomEvent('skopax:state', {detail: {state: D}}));
}

paint(JSON.parse(document.getElementById('skopax-data').textContent || '{}'));
setInterval(liveness, 60000);
document.querySelectorAll('a.s-mark').forEach(function (a) {
  a.addEventListener('click', function (e) {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button) return;
    e.preventDefault();
    pickTab('anoms');
  });
});
var hash = (location.hash || '').replace('#', '');
showTab(TABS.some(function (t) { return t.id === hash; }) ? hash : defaultTab());
setFilter('open');
window.addEventListener('hashchange', function () { followHash((location.hash || '').replace('#', '')); });
document.addEventListener('click', function (e) {
  var a = e.target && e.target.closest ? e.target.closest('a[href^="#a="], a[href^="#c="]') : null;
  if (!a || e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || a.getAttribute('href') !== location.hash) return;
  e.preventDefault();
  followHash(location.hash.replace('#', ''));
});
function followHash(h) {
  LINK_ASK++;
  var m = /^(a|c|r|rel|t)=(.*)$/.exec(h);
  if (!m || m[1] === 't') dropCard();
  if (!m) return;
  if (m[1] === 't') openTabPath(m[2]);
  else if (m[1] === 'a' || m[1] === 'c') showLinked(m[2], m[1] === 'c');
  else reports.link(m[1], m[2]);
}

function showLinked(raw, byNum) {
  var key;
  try { key = decodeURIComponent(raw); } catch (e) { return; }
  var note = document.getElementById('link-note');
  if (note) note.remove();
  document.querySelectorAll('#anoms article.inc.linked').forEach(function (c) { c.classList.remove('linked'); });
  var a = ALL.filter(function (x) { return byNum ? x.num != null && String(x.num) === key : x.key === key; })[0];
  if (!a && !byNum) {
    var near = ALL.filter(function (x) { return x.key !== key + '@' + x.cluster && (x.key.indexOf(key) === 0 || key.indexOf(x.key) === 0); });
    if (near.length === 1) a = near[0];
  }
  if (a && A.indexOf(a) < 0 && ARCHIVED.indexOf(a) < 0 && a.parent) a = A.concat(ARCHIVED).filter(function (x) { return x.key === a.parent; })[0];
  if (CARD_URL && (!a || CARD_URL.key !== a.key)) dropCard();
  showTab('anoms');
  if (byNum && (!a || (A.indexOf(a) < 0 && ARCHIVED.indexOf(a) < 0))) {
    fetchLinked(key);
    return;
  }
  if (!a || (A.indexOf(a) < 0 && ARCHIVED.indexOf(a) < 0)) {
    linkNote('The linked anomaly is no longer on the page: cleared items drop off after 24 hours, In progress ones with a Jira ticket 24 hours after the ticket closes.');
    tabUrl();
    return;
  }
  if (typeF && !typeIs(a, typeF)) { typeF = ''; renderQuick(); }
  clearSearchUnless([a]);
  setFilter(ARCHIVED.indexOf(a) >= 0 ? 'archived' : open.indexOf(a) >= 0 || stale.indexOf(a) >= 0 ? 'open' : muted.indexOf(a) >= 0 ? 'muted' : cleared.indexOf(a) >= 0 ? 'cleared' : inprog.indexOf(a) >= 0 ? 'progress' : taken.indexOf(a) >= 0 ? 'taken' : 'all');
  if (CARD_VIEW === 'board') {
    openDrawer(a.key);
    cardUrl(a, false);
    return;
  }
  var card = [].filter.call(document.querySelectorAll('#anoms article.inc'), function (c) { return c.dataset.key === a.key; })[0];
  if (!card) { tabUrl(); return; }
  ring(card);
  cardUrl(a, false);
}
var LINK_ASK = 0;
function linkNote(text) {
  var note = el('p', 'panel-note', esc(text));
  note.id = 'link-note';
  document.getElementById('anoms').before(note);
}
function fetchLinked(num) {
  var ask = LINK_ASK;
  var stale = function () { return ask !== LINK_ASK; };
  fetch('/api/cards/' + encodeURIComponent(num), {headers: {'Accept': 'application/json'}, credentials: 'same-origin'}).then(function (r) {
    if (r.status === 404) return null;
    if (!r.ok) throw new Error(String(r.status));
    return r.json();
  }).then(function (c) {
    if (stale()) return;
    if (!c || !c.key) { linkNote('Card not found.'); tabUrl(); return; }
    openDrawerCard(c);
    cardUrl(c, false);
  }).catch(function () {
    if (stale()) return;
    linkNote('The card could not be loaded; reload the page.');
    tabUrl();
  });
}
function ring(card) {
  document.querySelectorAll('#anoms article.inc.linked').forEach(function (c) { c.classList.remove('linked'); });
  card.classList.add('linked');
  var fold = card.closest('details.tgrp');
  if (fold) fold.open = true;
  cardOpen(card, true);
  card.scrollIntoView({block: 'start'});
}

initAppearance();
var repActs = null;
var reports = initReports();
followHash(hash);
urlReady = true;
initActions();
initLive();
initActivity();
loadRequests();
loadInvestigator();
startLocalize();
