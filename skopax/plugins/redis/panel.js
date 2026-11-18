(function () {
  var TAB = 'redis', SRC = 'redis';
  var DASH = '\u2013', DOT = ' \u00b7 ', OUT = ' \u2197';
  var SEV = {crit: ['Critical', 'k-crit'], warn: ['Warning', 'k-warn'], info: ['Info', 'k-info'], ok: ['Healthy', 'k-ok']};
  var RANK = {crit: 0, warn: 1, info: 2};
  var NOTE = 'Redis over the last 30 minutes: each redis_exporter instance, and each service\'s own Redis call time as its Lettuce client measures it, ' +
    'against the same window 1 and 7 days earlier. The status follows the open Redis cards.';
  var state = null, section = null;

  function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }
  function real(v) { return typeof v === 'number' && isFinite(v); }
  function none() { return '<span class="zero-v">' + DASH + '</span>'; }
  function count(v) { return !real(v) ? none() : v === 0 ? '<span class="zero-v">0</span>' : esc(Math.abs(v) >= 1 ? Math.round(v).toLocaleString() : v.toFixed(2)); }
  function ms(v) { return real(v) ? esc(v < 10 ? v.toFixed(2) : String(Math.round(v))) + ' ms' : none(); }
  function mib(v) { return real(v) ? esc((v / 1048576).toFixed(1)) + ' MiB' : none(); }
  function badge(sev, label) { var x = SEV[sev] || [sev, 'k-muted']; return '<span class="state ' + x[1] + '">' + esc(label || x[0]) + '</span>'; }
  function safe(url) { return typeof url === 'string' && /^https:\/\//.test(url) ? url : ''; }
  function graph(url, text, tip) { var u = safe(url); return u ? '<a class="lnk" href="' + esc(u) + '" target="_blank" rel="noopener" title="' + esc(tip) + '">' + text + OUT + '</a>' : text; }
  function same(a, b) { return String(a || '') === String(b || ''); }
  function rank(a) { return RANK[a.sev] == null ? 3 : RANK[a.sev]; }
  function open() {
    return ((state && state.anomalies) || []).filter(function (a) { return a && a.src === SRC && a.status === 'open' && !a.muted; }).sort(function (a, b) { return rank(a) - rank(b); });
  }
  function tags(list) {
    return list.length ? '<div class="sig-tags">' + list.map(function (a) {
      return '<a class="tag ' + (SEV[a.sev] || SEV.info)[1] + '" href="' + esc('#a=' + encodeURIComponent(String(a.key))) + '" title="' + esc(a.short || '') + '">' + esc(a.label || a.sig) + '</a>';
    }).join('') + '</div>' : '';
  }
  function rows(part) { var all = state && state[SRC]; return (Array.isArray(all) ? all : []).filter(function (r) { return r && r.part === part; }); }
  function where(r) { return esc(r.ns) + (r.cluster ? DOT + esc(r.cluster) : ''); }
  function named(r) { return '<span class="svc">' + esc(r.name || r.job) + '</span><span class="ns">' + where(r) + '</span>'; }
  function table(id, head, body, cols, empty) {
    return '<div class="box"><table id="' + id + '"><thead><tr>' + head.map(function (h) { return '<th' + (h[1] ? ' class="num"' : '') + (h[2] ? ' title="' + esc(h[2]) + '"' : '') + '>' + esc(h[0]) + '</th>'; }).join('') +
      '</tr></thead><tbody>' + (body.length ? body.join('') : '<tr class="empty"><td colspan="' + cols + '">' + esc(empty) + '</td></tr>') + '</tbody></table></div>';
  }
  function failures() {
    var sources = (state && state.sources) || {}, seen = {}, out = [];
    [''].concat(rows('summary').map(function (r) { return r.cluster || ''; })).forEach(function (name) {
      var key = name ? SRC + '-' + name : SRC, s = sources[key];
      if (seen[key] || typeof s !== 'string' || !s || /^ok( |$)/.test(s)) return;
      seen[key] = 1;
      out.push('<p class="panel-note">The last Redis check' + (name ? ' of ' + esc(name) : '') + ' failed: ' + esc(s.replace(/^error:\s*/, '')) + '</p>');
    });
    return out.join('');
  }
  function denied(part) { var m = / no access to ([^\u00b7]+?)( \u00b7 |$)/.exec(String(((state && state.sources) || {})[SRC] || '')); return m && m[1].split(', ').indexOf(part) >= 0 ? 'Not read: no access to ' + part + '.' : ''; }
  function quiet(part) { var v = String(((state && state.sources) || {})[SRC] || ''), m = / no data for ([^\u00b7]+?)( \u00b7 |$)/.exec(v); return /^ok \u00b7 no data( \u00b7 |$)/.test(v) || !!(m && m[1].split(', ').indexOf(part) >= 0); }
  function render() {
    if (!state || !section) return;
    var cards = open(), html = '<p class="panel-note">' + esc(NOTE) + '</p>' + failures() + tags(cards);
    var instances = rows('exporter').map(function (r) {
      var list = cards.filter(function (a) { return a.svc === r.name && a.ns === r.ns && same(a.cluster, r.cluster); });
      return '<tr><td>' + (list.length ? badge(list[0].sev) : r.up === 0 ? badge('crit', 'Down') : badge('ok')) + '</td><td>' + named(r) + '</td>' +
        '<td>' + (r.role ? esc(r.role) : none()) + '</td><td class="num">' + (r.role === 'master' ? count(r.replicas) : none()) + '</td>' +
        '<td class="num">' + mib(r.memoryBytes) + '</td><td class="num">' + count(r.clients) + '</td><td class="num">' + count(r.ops) + '</td></tr>';
    });
    var services = rows('app').map(function (r) {
      var list = cards.filter(function (a) { return a.svc === r.job && a.ns === r.ns && same(a.cluster, r.cluster); });
      return '<tr><td>' + (list.length ? badge(list[0].sev) : badge('ok')) + '</td><td>' + named(r) + '</td><td class="nowrap">' + (r.redis ? esc(r.redis) : none()) + '</td>' +
        '<td class="num">' + graph(r.link, ms(r.ms), 'Redis call time graph') + '<span class="ns">1 d ' + ms(r.d1) + DOT + '7 d ' + ms(r.d7) + '</span></td>' +
        '<td class="num">' + count(r.calls) + '</td><td class="num">' + count(r.errors) + '</td></tr>';
    });
    html += '<h3 class="sec-t">Instances</h3>' + table('redis-instances', [['Status'], ['Instance'], ['Role'], ['Replicas', 1], ['Memory', 1], ['Clients', 1], ['Ops/s', 1, 'commands per second over 30 min']],
      instances, 7, denied('instances') || (quiet('instances') ? 'The last Redis check found no redis_exporter series.' : 'No redis_exporter data yet. The Redis check fills this on its next run.'));
    html += '<h3 class="sec-t">Services</h3>' + table('redis-apps', [['Status'], ['Service'], ['Redis'], ['Avg call', 1, 'average Redis call time measured in the service, with the same 30 min 1 and 7 days earlier'],
      ['Calls', 1], ['Errors', 1]], services, 6, denied('services') || (quiet('services') ? 'The last Redis check found no service Redis timings.' : 'No service Redis timings yet.'));
    section.innerHTML = html;
  }
  document.addEventListener('skopax:state', function (e) { state = e.detail && e.detail.state; render(); });
  document.addEventListener('skopax:page', function (e) {
    if (!e.detail || e.detail.tab !== TAB) return;
    section = e.detail.section;
    render();
  });
})();
