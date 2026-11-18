(function () {
  var TAB = 'logs', state = null, section = null;
  var NOTE = 'New or growing ERROR and WARN patterns, plus ERROR patterns with 30 or more hits, grouped by the first 90 characters. The links open Kibana Discover for the last 2 h.';
  var FLOW_NOTE = 'Log lines that reached Elasticsearch per namespace in the last 20 minutes, against the same window 1 and 7 days earlier, and how old the newest line is. A namespace that went quiet or a pipeline that falls behind gets a card.';
  var SEV = {crit: ['Critical', 'k-crit', 0], warn: ['Warning', 'k-warn', 1], info: ['Info', 'k-info', 2]};
  function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function count(v) { return isNum(v) ? String(Math.round(v)) : '-'; }
  function big(v) { return isNum(v) ? String(Math.round(v)).replace(/\B(?=(\d{3})+(?!\d))/g, ',') : '-'; }
  function obj(r) { return r && typeof r === 'object'; }
  function ok(status) { return typeof status === 'string' && (status === 'ok' || status.indexOf('ok ') === 0); }
  function refused(status) { var m = ok(status) && / no access to ([^\u00b7]+?)( \u00b7 |$)/.exec(status); return m ? m[1] : ''; }
  function notRead(part, check) { return 'Not read: no access to ' + esc(part) + '. Earlier rows are hidden until the ' + check + ' check can read the log index again.'; }
  function where(r) { return r.cluster ? ' <span class="tag plain">' + esc(r.cluster) + '</span>' : ''; }
  function link(r) {
    var u = typeof r.link === 'string' ? r.link : '';
    return u.indexOf('https://') === 0 ? '<a class="lnk" href="' + esc(u) + '" target="_blank" rel="noopener">Log lines \u2197</a>' : '';
  }
  function row(r) {
    var k = r.level === 'ERROR' ? 'k-crit' : 'k-warn';
    var flag = r.flag ? '<span class="tag ' + k + '">' + (r.d1 || r.d7 ? 'Growing' : 'New') + '</span>' : '<span class="tag plain">Steady</span>';
    return '<tr><td>' + flag + '</td><td class="svc">' + esc(r.app) + where(r) + '</td><td><span class="lvl ' + k + '">' + esc(r.level) + '</span></td><td class="msg">' + esc(r.msg) + '</td>' +
      '<td class="num">' + count(r.now) + '</td><td class="num">' + count(r.d1) + '</td><td class="num">' + count(r.d7) + '</td><td>' + link(r) + '</td></tr>';
  }
  function sev(a) { return SEV[a.sev] || [String(a.sev), 'k-muted', 3]; }
  function openFlow(match) {
    var all = Array.isArray(state.anomalies) ? state.anomalies.filter(obj) : [];
    return all.filter(function (a) { return a.status === 'open' && !a.muted && a.triage !== 'progress' && a.triage !== 'handled' && a.src === 'logflow' && (!match || match(a)); })
      .map(function (a, i) { return [a, i]; }).sort(function (x, y) { return sev(x[0])[2] - sev(y[0])[2] || x[1] - y[1]; }).map(function (x) { return x[0]; });
  }
  function tags(list, named) {
    return list.map(function (a) { return '<a class="tag ' + sev(a)[1] + '" href="#a=' + encodeURIComponent(String(a.key)) + '">' + esc(a.label || a.key) + (named && a.cluster ? ' ' + esc(a.cluster) : '') + '</a>'; }).join(' ');
  }
  function flowState(r) {
    var list = openFlow(function (a) { return a.ns === r.ns && (a.cluster || '') === (r.cluster || ''); });
    if (list.length) return '<span class="state ' + sev(list[0])[1] + '">' + esc(sev(list[0])[0]) + '</span> ' + tags(list);
    if (!r.n && Math.min(isNum(r.d1) ? r.d1 : 0, isNum(r.d7) ? r.d7 : 0) > 0) return '<span class="state k-warn">Quiet</span>';
    return '<span class="state k-ok">Healthy</span>';
  }
  function flowRow(r) {
    return '<tr><td>' + flowState(r) + '</td><td><span class="svc">' + esc(r.ns) + '</span>' + where(r) + '</td>' +
      '<td class="num">' + big(r.n) + ' <span class="ns">1 d ' + big(r.d1) + ' \u00b7 7 d ' + big(r.d7) + '</span></td><td class="num">' + (isNum(r.lagMin) ? esc(r.lagMin) + ' min' : '-') + '</td></tr>';
  }
  function lagOf(s) {
    var total = obj(s.total) ? s.total.n : null;
    return (s.cluster ? s.cluster + ': ' : '') + (isNum(s.lagMin) ? 'newest line ' + s.lagMin + ' min old' : 'no fresh line') + ' \u00b7 ' + big(isNum(total) ? total : 0) + ' lines in ' + (isNum(s.windowMin) ? s.windowMin : 20) + ' min';
  }
  function flow() {
    var all = Array.isArray(state.logflow) ? state.logflow.filter(obj) : [];
    var rows = all.filter(function (r) { return r.part === 'namespace'; }).sort(function (x, y) { return (isNum(y.d1) ? y.d1 : 0) - (isNum(x.d1) ? x.d1 : 0); });
    var lag = all.filter(function (r) { return r.part === 'summary'; }).map(lagOf).join(' \u00b7 ');
    var status = (state.sources || {}).logflow, denied = refused(status);
    if (denied) rows = [];
    var empty = denied ? notRead(denied, 'log flow') : ok(status) ? 'No namespace has log lines in these windows' + (status === 'ok' ? '' : ' (' + esc(status) + ')') + '.' : status ? 'The log flow check failed (' + esc(status) + ').' : 'The log flow check has not run yet.';
    return '<h3 class="sec-t">Log flow <span class="count" id="logflow-lag">' + esc(lag) + '</span></h3><p class="panel-note">' + esc(FLOW_NOTE) + '</p>' +
      '<div id="logflow-tags">' + tags(openFlow(), true) + '</div><div class="box"><table id="logflow-table"><thead><tr><th>Status</th><th>Namespace</th><th class="num">Last 20 min</th>' +
      '<th class="num" title="age of the newest line">Newest</th></tr></thead><tbody>' + (rows.length ? rows.map(flowRow).join('') : '<tr class="empty"><td colspan="4">' + empty + '</td></tr>') + '</tbody></table></div>';
  }
  function render() {
    if (!state || !section) return;
    var rows = Array.isArray(state.logTop) ? state.logTop.filter(obj) : [];
    var status = (state.sources || {}).logs, denied = refused(status);
    if (denied) rows = [];
    var empty = denied ? notRead(denied, 'logs') : ok(status) ? 'No flagged patterns and no ERROR pattern with 30 or more hits in the last hour.' : status ? 'The logs check failed (' + esc(status) + ').' : 'The logs check has not run yet.';
    section.innerHTML = '<p class="panel-note">' + esc(NOTE) + '</p><div class="box"><table id="logs-table"><thead><tr><th>Flag</th><th>Service</th><th>Level</th><th>Message</th>' +
      '<th class="num">Last 60 min</th><th class="num">1 day ago</th><th class="num">7 days ago</th><th>Kibana</th></tr></thead><tbody>' +
      (rows.length ? rows.map(row).join('') : '<tr class="empty"><td colspan="8">' + empty + '</td></tr>') + '</tbody></table></div>' + flow();
  }
  document.addEventListener('skopax:state', function (e) { state = e.detail && e.detail.state; render(); });
  document.addEventListener('skopax:page', function (e) { if (!e.detail || e.detail.tab !== TAB) return; section = e.detail.section; render(); });
})();
