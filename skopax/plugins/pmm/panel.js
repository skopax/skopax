(function () {
  var DASH = '\u2013', OUT = ' \u2197';
  var SEV = {crit: ['Critical', 'k-crit'], warn: ['Warning', 'k-warn'], info: ['Info', 'k-info'], ok: ['Healthy', 'k-ok']};
  var sections = {pg: null, mongo: null}, state = null;

  function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#x27;'); }
  function num(v) { return v == null ? DASH : Math.abs(v) >= 1 || v === 0 ? Math.round(v).toLocaleString('en-US') : v.toFixed(2); }
  function pct(v) { return v == null ? DASH : (v >= 10 ? Math.round(v) : v.toFixed(1)) + '%'; }
  function ms(v) { return v == null ? DASH : v < 0.01 ? '&lt;0.01 ms' : v < 10 ? v.toFixed(2) + ' ms' : v >= 1000 ? (v / 1000).toFixed(1) + ' s' : Math.round(v) + ' ms'; }
  function dur(s) {
    if (s == null) return DASH;
    s = Math.round(s);
    return s < 120 ? s + ' s' : s < 7200 ? Math.floor(s / 60) + ' min' : Math.floor(s / 3600) + ' h' + (s % 3600 >= 60 ? ' ' + Math.floor(s % 3600 / 60) + ' min' : '');
  }
  function hm(at) {
    var d = at ? new Date(at) : null;
    return d && !isNaN(d) ? d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hour12: false}) : '';
  }
  function badge(sev, label) { var x = SEV[sev] || [sev, 'k-muted']; return '<span class="state ' + x[1] + '">' + esc(label || x[0]) + '</span>'; }
  function out(url, text) { return /^https:\/\//.test(String(url || '')) ? '<a class="lnk" href="' + esc(url) + '" target="_blank" rel="noopener">' + esc(text) + OUT + '</a>' : ''; }
  function where(r) { return r.cluster ? ' (' + esc(r.cluster) + ')' : ''; }
  function rows(name) { return state && Array.isArray(state[name]) ? state[name].filter(function (r) { return r && typeof r === 'object'; }) : []; }
  function raised(a) { var tail = a.cluster ? '@' + a.cluster : '', key = String(a.key); return tail && key.slice(-tail.length) === tail ? key.slice(0, -tail.length) : key; }
  function open(prefix, queryid, r) {
    var rank = {crit: 0, warn: 1, info: 2};
    return (state && Array.isArray(state.anomalies) ? state.anomalies : []).filter(function (a) {
      if (!a || a.status !== 'open' || a.muted || a.triage === 'progress' || a.triage === 'handled' || raised(a).indexOf(prefix) !== 0) return false;
      if (r && String(a.cluster || '') !== String(r.cluster || '')) return false;
      return queryid == null || raised(a).split('|')[3] === queryid;
    }).sort(function (x, y) { return (x.sev in rank ? rank[x.sev] : 3) - (y.sev in rank ? rank[y.sev] : 3); });
  }
  function tags(list) {
    return list.length ? '<div class="sig-tags">' + list.map(function (a) {
      return '<a class="tag ' + (SEV[a.sev] || SEV.info)[1] + '" href="#a=' + encodeURIComponent(a.key) + '" title="' + esc(a.short || '') + '">' + esc(a.label || String(a.key).split('|')[2]) + '</a>';
    }).join('') + '</div>' : '';
  }
  function status(list, up) { return list.length ? badge(list[0].sev) : up >= 0.5 ? badge('ok') : badge('down', 'Down'); }
  function failed(check) {
    var src = state && state.sources && typeof state.sources === 'object' ? state.sources : {};
    return Object.keys(src).filter(function (k) { return (k === check || k.indexOf(check + '-') === 0) && !/^ok( |$)/.test(String(src[k])); }).map(function (k) {
      return (k === check ? '' : k.slice(check.length + 1) + ': ') + src[k];
    }).join(', ');
  }
  function ran(check) {
    var src = state && state.sources && typeof state.sources === 'object' ? state.sources : {};
    return Object.keys(src).some(function (k) { return k === check || k.indexOf(check + '-') === 0; });
  }
  function quiet(check) {
    var src = state && state.sources && typeof state.sources === 'object' ? state.sources : {};
    return /no data/.test(String(src[check] || ''));
  }
  function refused(check) {
    var src = state && state.sources && typeof state.sources === 'object' ? state.sources : {};
    var hit = Object.keys(src).map(function (k) { return (k === check || k.indexOf(check + '-') === 0) && / (no access to .+)$/.exec(String(src[k])); }).filter(Boolean)[0];
    return hit ? 'Not read: PMM refused the last check (' + hit[1] + ').' : '';
  }
  function empty(cols, text) { return '<tr class="empty"><td colspan="' + cols + '">' + esc(text) + '</td></tr>'; }
  function table(name, head, body) {
    return '<div class="box"><table data-pmm="' + name + '"><thead><tr>' + head.map(function (h) { return '<th' + (h[1] ? ' class="num"' : '') + '>' + esc(h[0]) + '</th>'; }).join('') +
      '</tr></thead><tbody>' + body + '</tbody></table></div>';
  }

  function pgBody() {
    var list = rows('pmm'), bad = failed('pmm');
    if (!list.length) return empty(11, bad ? 'PMM did not answer on the last check (' + bad + ').' : refused('pmm') ? refused('pmm') : quiet('pmm') ? 'PMM reported no Postgres series for the watched services on the last check.' : 'No Postgres data yet. The Postgres check fills this on its next run.');
    return list.map(function (r) {
      var cards = open('pmm|' + r.svc + '|', null, r), use = r.pct != null ? r.pct : r.conn != null && r.maxconn ? 100 * r.conn / r.maxconn : null;
      return '<tr><td>' + status(cards, r.up) + tags(cards) + '</td><td><span class="svc">' + esc(r.svc) + where(r) + '</span><span class="ns">' + (r.lag != null ? 'replica' : 'primary') + (r.up >= 0.5 ? '' : ' \u00b7 not up') + '</span></td>' +
        '<td class="num">' + num(r.conn) + ' <span class="muted">/ ' + num(r.maxconn) + '</span></td><td class="num">' + pct(use) + '</td><td class="num">' + num(r.connD1) + '</td><td class="num">' + num(r.connD7) + '</td>' +
        '<td class="num">' + num(r.idletx) + '</td><td class="num">' + dur(r.longtx) + '</td><td class="num">' + dur(r.lag) + '</td><td class="num">' + num(r.deadlocks) + '</td><td>' + out(r.link, 'Instance summary') + '</td></tr>';
    }).join('');
  }
  function qanBody() {
    var list = rows('qan'), bad = failed('qan');
    if (!list.length) return empty(9, bad ? 'Query Analytics did not answer on the last check (' + bad + ').' : refused('qan') ? refused('qan') : quiet('qan') ? 'Query Analytics reported no queries for the watched services on the last check.' : ran('qan') ? 'No query over the thresholds in the last 60 minutes.' : 'No Query Analytics data yet. The Postgres queries check fills this on its next run.');
    return list.map(function (r) {
      var cards = open('qan|' + r.svc + '|', String(r.queryid || ''), r), when = hm(r.peakAt);
      var peak = r.peakMs == null ? DASH : ms(r.peakMs) + (when ? ' <span class="muted">' + esc(when) + '</span>' : '');
      return '<tr><td><span class="svc">' + esc(r.svc) + where(r) + '</span></td><td><span class="svc">' + esc(r.app) + '</span><span class="ns">' + esc(r.db) + '</span>' + tags(cards) + '</td>' +
        '<td class="msg qtext" title="' + esc(r.q) + '">' + esc(r.q) + '</td><td class="num">' + num(r.calls) + '</td><td class="num">' + ms(r.avgMs) + '</td><td class="num">' + peak + '</td>' +
        '<td class="num">' + ms(r.avgD1) + '</td><td class="num">' + ms(r.avgD7) + '</td><td>' + out(r.link, 'Query Analytics') + '</td></tr>';
    }).join('');
  }
  function drawPg(section) {
    var note = 'Postgres services PMM reports. Connections are the peak of the last 60 minutes against max_connections; 1 d and 7 d are the same window a day and a week earlier.';
    var qnote = 'Queries with the most DB time in the last 60 minutes, from PMM Query Analytics. The worst minute is the highest one-minute average, not a per-call maximum.';
    section.innerHTML = '<p class="panel-note">' + esc(note) + '</p>' +
      table('pg', [['Status'], ['Service'], ['Connections', 1], ['Used', 1], ['1 d', 1], ['7 d', 1], ['Idle in tx', 1], ['Longest tx', 1], ['Lag', 1], ['Deadlocks', 1], ['']], pgBody()) +
      '<p class="panel-note">' + esc(qnote) + '</p>' +
      table('qan', [['Server'], ['Database'], ['Query'], ['Calls', 1], ['Average', 1], ['Worst minute', 1], ['1 d average', 1], ['7 d average', 1], ['']], qanBody());
  }
  function drawMongo(section) {
    var list = rows('mongo'), bad = failed('mongo'), sets = {};
    list.forEach(function (r) { if (r.set != null && r.set !== '') sets[JSON.stringify([String(r.cluster || ''), String(r.set)])] = {cluster: r.cluster || '', name: String(r.set)}; });
    var setHtml = Object.keys(sets).sort().map(function (k) {
      var s = sets[k], cards = open('mongo|' + s.name + '|', null, s);
      return '<span class="svc">' + esc(s.name) + where(s) + '</span>' + tags(cards);
    }).join('');
    var body = !list.length ? empty(15, bad ? 'PMM did not answer the MongoDB queries on the last check (' + bad + ').' : refused('mongo') ? refused('mongo') : quiet('mongo') ? 'PMM reported no MongoDB series for the watched services on the last check.' : 'No MongoDB data yet. The MongoDB check fills this on its next run.') :
      list.map(function (r) {
        var cards = open('mongo|' + r.svc + '|', null, r), role = r.state || DASH;
        return '<tr><td>' + status(cards, r.up) + tags(cards) + '</td><td><span class="svc">' + esc(r.svc) + where(r) + '</span><span class="ns">' + esc((r.node || '') + (r.addr ? ' \u00b7 ' + r.addr : '')) + '</span></td>' +
          '<td><span class="tag ' + (role === 'PRIMARY' ? 'k-act' : role === 'SECONDARY' ? 'plain' : 'k-crit') + '">' + esc(role) + '</span></td>' +
          '<td class="num">' + num(r.conn) + '</td><td class="num">' + num(r.connD1) + '</td><td class="num">' + num(r.connD7) + '</td><td class="num">' + (r.lag == null ? DASH : Math.round(r.lag) + ' s') + '</td>' +
          '<td class="num">' + num(r.ops) + '</td><td class="num">' + num(r.opsD1) + '</td><td class="num">' + num(r.opsD7) + '</td>' +
          '<td class="num">' + ms(r.readMs) + '</td><td class="num">' + ms(r.writeMs) + '</td><td class="num">' + ms(r.cmdMs) + '</td><td class="num">' + num(r.queue) + '</td><td>' + out(r.link, 'Instance summary') + '</td></tr>';
      }).join('');
    section.innerHTML = '<p class="panel-note">' + esc('MongoDB members PMM reports. Connections and operations are the last 60 minutes; 1 d and 7 d are the same window a day and a week earlier.') + '</p>' +
      '<div class="sig-sets" data-pmm="sets">' + setHtml + '</div>' +
      table('mongo', [['Status'], ['Member'], ['Role'], ['Connections', 1], ['1 d', 1], ['7 d', 1], ['Lag', 1], ['Operations', 1], ['1 d', 1], ['7 d', 1], ['Read', 1], ['Write', 1], ['Command', 1], ['Queued', 1], ['']], body);
  }
  function draw() {
    if (!state) return;
    if (sections.pg) drawPg(sections.pg);
    if (sections.mongo) drawMongo(sections.mongo);
  }

  document.addEventListener('skopax:state', function (e) { state = e.detail && e.detail.state && typeof e.detail.state === 'object' ? e.detail.state : null; draw(); });
  document.addEventListener('skopax:page', function (e) {
    var tab = e.detail && e.detail.tab;
    if (tab !== 'pg' && tab !== 'mongo') return;
    sections[tab] = e.detail.section;
    draw();
  });
})();
