var KIND = [{id: 'all', label: 'All'}, {id: 'app', label: 'Apps'}, {id: 'gateway', label: 'Gateways'}, {id: 'ingress', label: 'Ingress'}, {id: 'kafka', label: 'Kafka'}, {id: 'bad', label: 'Not healthy', k: 'k-warn'}, {id: 'deployed', label: 'Deployed (24 h)'}];
function inKind(s, id) { return id === 'all' || (id === 'bad' ? s.status !== 'ok' : id === 'deployed' ? s.deployedAt && Date.now() - Date.parse(s.deployedAt) >= 0 && Date.now() - Date.parse(s.deployedAt) < 24 * 3600000 : s.kind === id); }
var svcChips = document.getElementById('svc-chips');
function renderSvcChips() {
  svcChips.innerHTML = '';
  KIND.forEach(function (c) {
    var n = S.filter(function (s) { return inKind(s, c.id); }).length;
    var b = el('button', c.k || '', esc(c.label) + ' <span class="count">' + n + '</span>');
    b.type = 'button'; b.dataset.f = c.id;
    if (c.tip) b.title = c.tip;
    b.addEventListener('click', function () { kind = c.id; renderSvcs(); markTab(); });
    svcChips.appendChild(b);
  });
}
function renderSvcs() {
  svcChips.querySelectorAll('button').forEach(function (c) { c.setAttribute('aria-pressed', c.dataset.f === kind ? 'true' : 'false'); });
  var tb = document.querySelector('#svcs tbody');
  tb.innerHTML = '';
  var list = S.filter(function (s) { return inKind(s, kind); });
  if (kind === 'deployed') list.sort(function (a, b) { return a.deployedAt < b.deployedAt ? -1 : a.deployedAt > b.deployedAt ? 1 : 0; });
  if (!list.length) tb.appendChild(el('tr', 'empty', '<td colspan="14">No services in this view.</td>'));
  list.forEach(function (s) {
    var req = s.req != null ? s.req : s['gw-req'] != null ? s['gw-req'] : s['ing-req'];
    var tr = el('tr'), bad = s.status !== 'ok', n = (s.drivers || []).length;
    var st = bad ? '<td title="' + esc(svcTip(s)) + '"><button type="button" class="state go ' + (SEV[s.status] || SEV.info)[1] + '">' + esc((SEV[s.status] || [s.status])[0]) + (n ? ' <span class="go-n">' + n + (n === 1 ? ' card' : ' cards') + ' ›</span>' : '') + '</button></td>' : '<td>' + state(s.status) + '</td>';
    tr.innerHTML = st + '<td><span class="svc">' + esc(s.svc) + '</span><span class="ns">' + esc(s.ns) + '</span></td><td><span class="tag plain">' + esc(s.kind) + '</span></td>' +
      '<td class="num">' + num(req) + '</td><td class="num">' + num(s.err) + '</td><td class="num">' + num(s.warn) + '</td><td class="num">' + num(s['5xx'] != null ? s['5xx'] : s['ing-5xx']) + '</td>' +
      '<td class="num">' + num(s['out-err']) + '</td><td class="num">' + num(s['gw-err']) + '</td><td class="num">' + num(s.avgMs, 'ms') + '</td><td class="num">' + num(s.p95Ms, 'ms') + '</td>' +
      '<td class="num">' + num(s.lag) + '</td><td class="num">' + num(s.restarts) + '</td><td class="when">' + (s.deployedAt ? '<b>' + esc(dayhm(s.deployedAt)) + '</b><span class="ns">' + esc(s.image || '') + '</span>' : '') + '</td>';
    tr.dataset.svc = s.svc;
    tr.dataset.ns = s.ns;
    if (bad) {
      tr.className = 'clk ' + (SEV[s.status] || SEV.info)[1];
      tr.addEventListener('click', function () { showSvcCards(s); });
    }
    tb.appendChild(tr);
  });
}
function svcTip(s) {
  var by = s.drivers || [];
  if (!by.length) return 'Status from the last check. Click to see this service’s cards.';
  return by.slice(0, 3).map(function (a) { return (SEV[a.sev] || SEV.info)[0] + ' · ' + (a.label || String(a.key).split('|')[2]); })
    .concat(by.length > 3 ? ['+' + (by.length - 3) + ' more'] : [])
    .concat(['Click to see ' + (by.length === 1 ? 'this card' : 'these cards')]).join('\n');
}
function svcCards(s) {
  var own = function (a) { return healthCard(a) && ownedBy(a, s); };
  var held = function (a) { return own(a) || (KIDS[a.key] || []).some(own); };
  var tiers = [['open', open.filter(own)], ['stopped, not addressed', stale.filter(held), 'k-warn'], ['in progress, action taken or muted', inprog.concat(taken).concat(muted).filter(held), 'k-good'], ['stopped firing', cleared.filter(held), 'k-cleared']];
  for (var i = 0; i < tiers.length; i++) {
    var list = tiers[i][1].sort(bySevOrd);
    if (list.length) return {tier: tiers[i][0], list: list, k: tiers[i][2] || (SEV[list[0].sev] || SEV.info)[1]};
  }
  return {tier: 'nothing open', list: [], k: 'k-ok'};
}
function showSvcCards(s) {
  svcF = {svc: s.svc, ns: s.ns};
  var note = document.getElementById('link-note');
  if (note) note.remove();
  clearSearchUnless(svcCards(svcF).list);
  showTab('anoms');
  renderFilters();
  setFilter('svc');
  var first = document.querySelector('#anoms article.inc');
  if (first) ring(first);
}
function backToSvcs() {
  var f = svcF;
  svcF = null;
  renderFilters();
  setFilter('open');
  showTab('svcs');
  var row = f && [].filter.call(document.querySelectorAll('#svcs tbody tr'), function (r) { return r.dataset.svc === f.svc && r.dataset.ns === f.ns; })[0];
  if (!row) return;
  row.scrollIntoView({block: 'center'});
  var b = row.querySelector('button.go');
  if (b) b.focus({preventScroll: true});
}

function renderRoutes() {
  var tb = document.querySelector('#routes tbody');
  tb.innerHTML = '';
  var rows = D.routes || [];
  if (!rows.length) tb.appendChild(el('tr', 'empty', '<td colspan="6">No gateway route data yet.</td>'));
  rows.forEach(function (r) {
    var base = Math.max(r.d1 || 0, r.d7 || 0);
    var hot = base && r.p95 > 2 * base && r.p95 - base >= 250;
    var tr = el('tr');
    tr.innerHTML = '<td class="svc">' + esc(r.gw) + '</td><td class="msg">' + esc(r.route) + '</td><td class="num">' + num(r.req) + '</td>' +
      '<td class="num' + (hot ? ' hot k-warn' : '') + '">' + num(r.p95, 'ms') + '</td><td class="num">' + num(r.d1, 'ms') + '</td><td class="num">' + num(r.d7, 'ms') + '</td>';
    tb.appendChild(tr);
  });
}

