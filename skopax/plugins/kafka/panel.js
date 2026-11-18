(function () {
  var TAB = 'kafka', SRC = 'kafka';
  var DASH = '\u2013', DOT = ' \u00b7 ', OUT = ' \u2197';
  var SEV = {crit: ['Critical', 'k-crit'], warn: ['Warning', 'k-warn'], info: ['Info', 'k-info'], ok: ['Healthy', 'k-ok']};
  var RANK = {crit: 0, warn: 1, info: 2};
  var NOTE = 'Kafka from the kafka_exporter series: brokers count against the ones seen in the last day, messages/s and consumer lag against the same moment 1 and 7 days earlier, ' +
    'and each service\'s Spring Kafka listener and send timers over the last 30 minutes. Growing lag stays a Prometheus card. The status follows the open Kafka cards.';
  var state = null, section = null;

  function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }
  function real(v) { return typeof v === 'number' && isFinite(v); }
  function none() { return '<span class="zero-v">' + DASH + '</span>'; }
  function count(v) { return !real(v) ? none() : v === 0 ? '<span class="zero-v">0</span>' : esc(Math.abs(v) >= 1 ? Math.round(v).toLocaleString() : v.toFixed(2)); }
  function rate(v) { return real(v) ? esc(v < 10 ? v.toFixed(1) : String(Math.round(v))) : none(); }
  function ms(v) { return real(v) ? esc(v < 10 ? v.toFixed(2) : String(Math.round(v))) + ' ms' : none(); }
  function pct(v) { return real(v) ? esc(v >= 10 ? String(Math.round(v)) : v.toFixed(1)) + '%' : none(); }
  function badge(sev, label) { var x = SEV[sev] || [sev, 'k-muted']; return '<span class="state ' + x[1] + '">' + esc(label || x[0]) + '</span>'; }
  function safe(url) { return typeof url === 'string' && /^https:\/\//.test(url) ? url : ''; }
  function graph(url, text, tip) { var u = safe(url); return u ? '<a class="lnk" href="' + esc(u) + '" target="_blank" rel="noopener" title="' + esc(tip) + '">' + text + OUT + '</a>' : text; }
  function same(a, b) { return String(a || '') === String(b || ''); }
  function rank(a) { return RANK[a.sev] == null ? 3 : RANK[a.sev]; }
  function live(a) { return a && a.status === 'open' && !a.muted; }
  function bySev(a, b) { return rank(a) - rank(b); }
  function all() { return ((state && state.anomalies) || []).filter(live); }
  function tags(list) {
    return list.length ? '<div class="sig-tags">' + list.map(function (a) {
      return '<a class="tag ' + (SEV[a.sev] || SEV.info)[1] + '" href="' + esc('#a=' + encodeURIComponent(String(a.key))) + '" title="' + esc(a.short || '') + '">' + esc(a.label || a.sig) + '</a>';
    }).join('') + '</div>' : '';
  }
  function rows(part) { var got = state && state[SRC]; return (Array.isArray(got) ? got : []).filter(function (r) { return r && r.part === part; }); }
  function summaryOf(r) { return rows('summary').filter(function (x) { return same(x.cluster, r.cluster); })[0] || {}; }
  function denied(part) { var m = / no access to ([^\u00b7]+?)( \u00b7 |$)/.exec(String(((state && state.sources) || {})[SRC] || '')); return m && m[1].split(', ').indexOf(part) >= 0 ? 'Not read: no access to ' + part + '.' : ''; }
  function quiet(part) { var v = String(((state && state.sources) || {})[SRC] || ''), m = / no data for ([^\u00b7]+?)( \u00b7 |$)/.exec(v); return /^ok \u00b7 no data( \u00b7 |$)/.test(v) || !!(m && m[1].split(', ').indexOf(part) >= 0); }
  function raised(a) { var tail = a.cluster ? '@' + a.cluster : '', key = String(a.key || ''); return tail && key.slice(-tail.length) === tail ? key.slice(0, -tail.length) : key; }
  function where(r, text) { return [text, r.cluster].filter(function (x) { return x; }).map(esc).join(DOT); }
  function named(r, name, ns) { return '<span class="svc">' + esc(name) + '</span><span class="ns">' + where(r, ns) + '</span>'; }
  function table(id, head, body, cols, empty) {
    return '<div class="box"><table id="' + id + '"><thead><tr>' + head.map(function (h) { return '<th' + (h[1] ? ' class="num"' : '') + (h[2] ? ' title="' + esc(h[2]) + '"' : '') + '>' + esc(h[0]) + '</th>'; }).join('') +
      '</tr></thead><tbody>' + (body.length ? body.join('') : '<tr class="empty"><td colspan="' + cols + '">' + esc(empty) + '</td></tr>') + '</tbody></table></div>';
  }
  function earlier(d1, d7, show) { return '<span class="ns">1 d ' + show(d1) + DOT + '7 d ' + show(d7) + '</span>'; }
  function notes() {
    var sources = (state && state.sources) || {}, seen = {}, out = [], summaries = rows('summary');
    [''].concat(summaries.map(function (r) { return r.cluster || ''; })).forEach(function (name) {
      var key = name ? SRC + '-' + name : SRC, s = sources[key];
      if (seen[key] || typeof s !== 'string' || !s || /^ok( |$)/.test(s)) return;
      seen[key] = 1;
      out.push('<p class="panel-note">The last Kafka check' + (name ? ' of ' + esc(name) : '') + ' failed: ' + esc(s.replace(/^error:\s*/, '')) + '</p>');
    });
    if (summaries.some(function (r) { return r.preset === 'kafka-exporter'; })) out.push('<p class="panel-note">Broker disks and broker pod restarts need the strimzi preset.</p>');
    return out.join('');
  }
  function render() {
    if (!state || !section) return;
    var open = all(), mine = open.filter(function (a) { return a.src === SRC; }).sort(bySev);
    var html = '<p class="panel-note">' + esc(NOTE) + '</p>' + notes() + tags(mine);
    var clusters = rows('clusters').map(function (r) {
      var list = mine.filter(function (a) { return a.kafka === 'clusters' && a.ns === r.ns && same(a.cluster, r.cluster); });
      var fallback = !real(r.brokers) ? badge('warn', 'No data') : r.brokers < (r.expected || 0) ? badge('crit', 'Brokers down') : badge('ok');
      return '<tr><td>' + (list.length ? badge(list[0].sev) : fallback) + '</td><td>' + named(r, r.ns, '') + '</td>' +
        '<td class="num">' + (real(r.brokers) ? esc(r.brokers + '/' + (r.expected || r.brokers)) : none()) + '</td><td class="num">' + count(r.topics) + '</td>' +
        '<td class="num">' + count(r.partitions) + (r.offline ? '<span class="ns">' + count(r.offline) + ' offline</span>' : '') + '</td><td class="num">' + count(r.urp) + '</td>' +
        '<td class="num" title="' + esc(r.diskPvc || '') + '">' + pct(r.diskPct) + '</td>' +
        '<td class="num">' + graph(r.link, rate(r.msgs), 'messages written per second') + earlier(r.msgsD1, r.msgsD7, rate) + '</td></tr>';
    });
    var groups = rows('groups').map(function (r) {
      var lagKey = 'kafka-lag|' + (r.namespace || r.ns) + '/kafka/' + r.group, s = summaryOf(r), via = s.source || '';
      var lagOf = function (a) { return typeof s.sourcePrimary === 'boolean' ? same(a.cluster, s.sourcePrimary ? '' : via) : !a.cluster || a.cluster === via; };
      var list = open.filter(function (a) { return (a.src === SRC && a.svc === 'kafka/' + r.group && a.ns === r.ns && same(a.cluster, r.cluster)) || (raised(a) === lagKey && lagOf(a)); }).sort(bySev);
      return '<tr><td>' + (list.length ? badge(list[0].sev) + tags(list) : r.members === 0 ? badge('warn', 'No members') : badge('ok')) + '</td><td>' + named(r, r.group, r.ns) + '</td>' +
        '<td class="num">' + graph(r.link, count(r.lag), 'messages waiting, per topic') + earlier(r.d1, r.d7, count) + '</td><td class="num">' + count(r.members) + '</td></tr>';
    });
    var services = rows('app').map(function (r) {
      var list = mine.filter(function (a) { return a.kafka === 'app' && a.svc === r.job && a.ns === r.ns && same(a.cluster, r.cluster); });
      var side = function (k, link) {
        var n = r[k], f = r[k + 'Fail'], base = r[k + 'FailD1'] || r[k + 'FailD7'];
        return '<td class="num">' + (n ? graph(link, count(n), 'calls in 30 min by result') : count(n)) + '</td>' +
          '<td class="num">' + (f ? '<b>' + count(f) + '</b>' : count(n ? 0 : null)) + (base ? earlier(r[k + 'FailD1'] || 0, r[k + 'FailD7'] || 0, count) : '') + '</td>' +
          '<td class="num">' + (n ? ms(r[k + 'Ms']) : none()) + '</td>';
      };
      return '<tr><td>' + (list.length ? badge(list[0].sev) : badge('ok')) + '</td><td>' + named(r, r.job, r.ns) + '</td>' + side('in', r.inLink) + side('out', r.outLink) + '</tr>';
    });
    html += '<h3 class="sec-t">Clusters</h3>' + table('kafka-clusters', [['Status'], ['Cluster'], ['Brokers', 1, 'brokers up of the ones seen in the last day'], ['Topics', 1], ['Partitions', 1],
      ['Under-replicated', 1, 'partitions whose in-sync replicas are fewer than their replicas'], ['Disk', 1, 'the fullest broker data volume'],
      ['Msgs/s', 1, 'messages written per second over 30 min, with the same window 1 and 7 days earlier']], clusters, 8, denied('clusters') || (quiet('clusters') ? 'The last Kafka check found no Kafka series.' : 'No Kafka data yet. The Kafka check fills this on its next run.'));
    html += '<h3 class="sec-t">Consumer groups</h3>' + table('kafka-groups', [['Status'], ['Group'], ['Lag', 1, 'messages waiting now, with the same moment 1 and 7 days earlier'], ['Members', 1]],
      groups, 4, denied('groups') || 'No consumer groups yet.');
    html += '<h3 class="sec-t">Services</h3>' + table('kafka-apps', [['Status'], ['Service'], ['Consumed', 1, 'Spring Kafka listener calls in 30 min'], ['Failed', 1], ['Avg', 1],
      ['Sent', 1, 'KafkaTemplate sends in 30 min'], ['Failed', 1], ['Avg', 1]], services, 8, denied('services') || 'No service Kafka timers yet.');
    section.innerHTML = html;
  }
  document.addEventListener('skopax:state', function (e) { state = e.detail && e.detail.state; render(); });
  document.addEventListener('skopax:page', function (e) {
    if (!e.detail || e.detail.tab !== TAB) return;
    section = e.detail.section;
    render();
  });
})();
