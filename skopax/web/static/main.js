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
  renderRoutes();
  renderHist();
  renderSlackQ();
  loadInvestigations();
  document.dispatchEvent(new CustomEvent('skopax:state', {detail: {state: D}}));
}

paint(JSON.parse(document.getElementById('skopax-data').textContent || '{}'));
setInterval(liveness, 60000);
var hash = (location.hash || '').replace('#', '');
showTab(TABS.some(function (t) { return t.id === hash; }) ? hash : defaultTab());
setFilter('all');
window.addEventListener('hashchange', function () { followHash((location.hash || '').replace('#', '')); });
function followHash(h) {
  var m = /^(a|r|rel|t)=(.*)$/.exec(h);
  if (!m) return;
  if (m[1] === 't') openTabPath(m[2]);
  else if (m[1] === 'a') showLinked(m[2]);
  else reports.link(m[1], m[2]);
}

function showLinked(raw) {
  var key;
  try { key = decodeURIComponent(raw); } catch (e) { return; }
  var note = document.getElementById('link-note');
  if (note) note.remove();
  document.querySelectorAll('#anoms article.inc.linked').forEach(function (c) { c.classList.remove('linked'); });
  var a = ALL.filter(function (x) { return x.key === key; })[0];
  if (!a) {
    var near = ALL.filter(function (x) { return x.key !== key + '@' + x.cluster && (x.key.indexOf(key) === 0 || key.indexOf(x.key) === 0); });
    if (near.length === 1) a = near[0];
  }
  if (a && A.indexOf(a) < 0 && ARCHIVED.indexOf(a) < 0 && a.parent) a = A.concat(ARCHIVED).filter(function (x) { return x.key === a.parent; })[0];
  showTab('anoms');
  if (!a || (A.indexOf(a) < 0 && ARCHIVED.indexOf(a) < 0)) {
    note = el('p', 'panel-note', 'The linked anomaly is no longer on the page: cleared items drop off after 24 hours, In progress ones with a Jira ticket 24 hours after the ticket closes.');
    note.id = 'link-note';
    document.getElementById('anoms').before(note);
    history.replaceState(null, '', location.pathname + location.search);
    return;
  }
  if (typeF && !typeIs(a, typeF)) { typeF = ''; renderAnomSub(); }
  clearSearchUnless([a]);
  setFilter(ARCHIVED.indexOf(a) >= 0 ? 'archived' : open.indexOf(a) >= 0 || stale.indexOf(a) >= 0 ? 'open' : muted.indexOf(a) >= 0 ? 'muted' : cleared.indexOf(a) >= 0 ? 'cleared' : inprog.indexOf(a) >= 0 ? 'progress' : taken.indexOf(a) >= 0 ? 'taken' : 'all');
  if (CARD_VIEW === 'board') {
    openDrawer(a.key);
    history.replaceState(null, '', location.pathname + location.search);
    return;
  }
  var card = [].filter.call(document.querySelectorAll('#anoms article.inc'), function (c) { return c.dataset.key === a.key; })[0];
  if (!card) return;
  ring(card);
  history.replaceState(null, '', location.pathname + location.search);
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
