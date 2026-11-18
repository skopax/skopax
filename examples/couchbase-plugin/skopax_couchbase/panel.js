(function () {
  var TAB = 'couchbase';
  var SEV = {crit: ['Critical', 'k-crit', 0], warn: ['Warning', 'k-warn', 1], info: ['Info', 'k-info', 2]};
  var NOTE = 'Each Couchbase instance is one cluster, read from its Prometheus over the last 60 minutes: node uptime, the client connection peak against the same window ' +
    "1 and 7 days earlier, index memory against its quota, and the cluster's N1QL queries, errors, timeouts and average time. The status follows the open Couchbase cards.";
  var DASH = '-', DOT = ' / ';
  var state = null, section = null;

  function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }
  function https(u) { return typeof u === 'string' && u.indexOf('https://') === 0; }
  function real(v) { return typeof v === 'number' && isFinite(v); }
  function secs(v) { var x = Math.round(v); return x < 120 ? x + ' s' : x < 7200 ? Math.floor(x / 60) + ' min' : Math.floor(x / 3600) + ' h'; }
  function num(v, unit) {
    if (!real(v)) return '<span class="zero-v">' + DASH + '</span>';
    if (unit === 'ms') return v >= 1000 ? (v / 1000).toFixed(1) + ' s' : Math.round(v) + ' ms';
    if (unit === '%') return (v >= 10 ? Math.round(v) : v.toFixed(1)) + '%';
    return v === 0 ? '<span class="zero-v">0</span>' : (Math.abs(v) >= 1 ? esc(Math.round(v).toLocaleString('en-US')) : v.toFixed(2));
  }
  function upFor(s) { return !real(s) ? num(null) : s >= 172800 ? Math.floor(s / 86400) + ' d' : esc(secs(s)); }
  function gib(v) { return !real(v) ? DASH : (v / 1073741824).toFixed(1) + ' GiB'; }
  function sevOf(a) { return SEV[a.sev] || SEV.info; }
  function open(a) { return a && a.status === 'open' && !a.muted && a.triage !== 'progress' && a.triage !== 'handled'; }
  function where(x) { return x && typeof x.cluster === 'string' ? x.cluster : ''; }

  function cardsOf(sum, svc) {
    return ((state && state.anomalies) || []).filter(function (a) { return open(a) && a.src === TAB && a.svc === svc && where(a) === where(sum); })
      .sort(function (x, y) { return sevOf(x)[2] - sevOf(y)[2]; });
  }

  function tags(list) {
    return list.length ? '<div class="sig-tags">' + list.map(function (a) {
      return '<a class="tag ' + sevOf(a)[1] + '" href="#a=' + esc(encodeURIComponent(a.key)) + '" title="' + esc(a.short || '') + '">' + esc(a.label || a.sig) + '</a>';
    }).join('') + '</div>' : '';
  }

  function badge(cls, label) { return '<span class="state ' + esc(cls) + '">' + esc(label) + '</span>'; }

  function graph(sum, node) {
    if (!https(sum.ui) || typeof sum.selector !== 'string') return '';
    var expr = 'kv_curr_connections{' + sum.selector + ',instance=' + JSON.stringify(String(node)) + '}';
    var url = sum.ui + '/query?g0.expr=' + encodeURIComponent(expr) + '&g0.tab=graph&g0.range_input=2d';
    return '<a class="lnk" href="' + esc(url) + '" target="_blank" rel="noopener">Connections graph</a>';
  }

  function row(r, sum) {
    var list = cardsOf(sum, r.node);
    var st = list.length ? badge(sevOf(list[0])[1], sevOf(list[0])[0]) : r.up >= 0.5 ? badge('k-ok', 'OK') : badge('k-crit', 'Down');
    return '<tr><td>' + st + tags(list) + '</td><td><span class="svc">' + esc(r.node) + '</span></td>' +
      '<td class="num">' + upFor(r.uptime) + '</td>' +
      '<td class="num" title="peak in the last 60 min">' + num(r.conn) + '</td><td class="num">' + num(r.connD1) + '</td><td class="num">' + num(r.connD7) + '</td>' +
      '<td class="num" title="' + esc('index service memory: ' + gib(r.imem) + ' used of a ' + gib(r.iquota) + ' quota') + '">' + num(r.imemPct, '%') + '</td>' +
      '<td>' + graph(sum, r.node) + '</td></tr>';
  }

  function totals(s) {
    return '<p>' + ['N1QL <b>' + num(s.req) + '</b> queries (1 d ' + num(s.reqD1) + DOT + '7 d ' + num(s.reqD7) + ')',
      'errors <b>' + num(s.errors) + '</b>' + (real(s.share) ? ' (' + s.share.toFixed(2) + '%)' : ''),
      'timeouts <b>' + num(s.timeouts) + '</b>',
      'average <b>' + num(s.avgMs, 'ms') + '</b> (1 d ' + num(s.avgD1, 'ms') + DOT + '7 d ' + num(s.avgD7, 'ms') + ')',
      'queued <b>' + num(s.queue) + '</b>',
      'rebalance <b>' + (s.stuck ? 'stuck' : s.rebalance ? 'running' : 'none') + '</b>',
      'auto-failover <b>' + (s.failover ? num(s.failover) : 'none') + '</b>'].join(', ') + '</p>';
  }

  function cluster(sum, all) {
    var nodes = all.filter(function (r) { return r.part === 'node' && where(r) === where(sum); });
    var html = '<h3 class="sec-t">' + esc(sum.name || where(sum) || 'Couchbase') + '</h3>' + tags(cardsOf(sum, sum.name)) + totals(sum);
    html += '<div class="box"><table><thead><tr><th>Status</th><th>Node</th><th class="num">Uptime</th><th class="num">Connections</th><th class="num">1 day ago</th>' +
      '<th class="num">7 days ago</th><th class="num">Index memory</th><th>Prometheus</th></tr></thead><tbody>';
    html += nodes.length ? nodes.map(function (r) { return row(r, sum); }).join('') : '<tr class="empty"><td colspan="8">No nodes in the last pass.</td></tr>';
    return html + '</tbody></table></div>';
  }

  function render() {
    if (!section) return;
    var all = (state && Array.isArray(state[TAB]) ? state[TAB] : []).filter(function (r) { return r && typeof r === 'object'; });
    var sums = all.filter(function (r) { return r.part === 'summary'; });
    var html = '<p class="panel-note">' + esc(NOTE) + '</p>';
    html += sums.length ? sums.map(function (s) { return cluster(s, all); }).join('') : '<p>No Couchbase data yet. The Couchbase check fills this on its next run.</p>';
    section.innerHTML = html;
  }

  document.addEventListener('skopax:state', function (e) { state = e.detail && e.detail.state; render(); });
  document.addEventListener('skopax:page', function (e) {
    if (!e.detail || e.detail.tab !== TAB) return;
    section = e.detail.section;
    render();
  });
})();
