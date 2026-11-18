(function () {
  var DASH = '\u2013', OUT = ' \u2197', HOUR = 3600000;
  var SEV = {crit: ['Critical', 'k-crit'], warn: ['Warning', 'k-warn'], info: ['Info', 'k-info'], ok: ['Healthy', 'k-ok']};
  var PARTS = ['zones', 'health checks', 'certificates', 'status'];
  var section = null, state = null;

  function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#x27;'); }
  function num(v) { return v == null ? DASH : Math.abs(v) >= 1 || v === 0 ? Math.round(v).toLocaleString('en-US') : v.toFixed(2); }
  function share(v) { return v == null ? DASH : !v ? '0%' : v < 0.01 ? '&lt;0.01%' : v < 1 ? v.toFixed(2) + '%' : (v >= 10 ? Math.round(v) : v.toFixed(1)) + '%'; }
  function hm(at) {
    var d = at ? new Date(at) : null;
    return d && !isNaN(d) ? d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hour12: false}) : '';
  }
  function badge(sev, label) { var x = SEV[sev] || [sev, 'k-muted']; return '<span class="state ' + x[1] + '">' + esc(label || x[0]) + '</span>'; }
  function out(url, text) { return /^https:\/\//.test(String(url || '')) ? '<a class="lnk" href="' + esc(url) + '" target="_blank" rel="noopener">' + esc(text) + OUT + '</a>' : ''; }
  function where(r) { return r.cluster ? ' (' + esc(r.cluster) + ')' : ''; }
  function rows(name) { return state && Array.isArray(state[name]) ? state[name].filter(function (r) { return r && typeof r === 'object'; }) : []; }
  function part(name) { return rows('cloudflare').filter(function (r) { return r.part === name; }); }
  function list(v) { return Array.isArray(v) ? v.map(String) : []; }
  function raised(a) { var tail = a.cluster ? '@' + a.cluster : '', key = String(a.key); return tail && key.slice(-tail.length) === tail ? key.slice(0, -tail.length) : key; }
  function open(match, r) {
    var rank = {crit: 0, warn: 1, info: 2};
    return (state && Array.isArray(state.anomalies) ? state.anomalies : []).filter(function (a) {
      return a && a.status === 'open' && !a.muted && a.triage !== 'progress' && a.triage !== 'handled' && match(raised(a)) && (!r || String(a.cluster || '') === String(r.cluster || ''));
    }).sort(function (x, y) { return (x.sev in rank ? rank[x.sev] : 3) - (y.sev in rank ? rank[y.sev] : 3); });
  }
  function exact(key, r) { return open(function (k) { return k === key; }, r); }
  function tags(found) {
    return found.length ? '<div class="sig-tags">' + found.map(function (a) {
      return '<a class="tag ' + (SEV[a.sev] || SEV.info)[1] + '" href="#a=' + encodeURIComponent(a.key) + '" title="' + esc(a.short || '') + '">' + esc(a.label || String(a.key).split('|')[0]) + '</a>';
    }).join('') + '</div>' : '';
  }
  function sources() { return state && state.sources && typeof state.sources === 'object' ? state.sources : {}; }
  function named(text) { return PARTS.filter(function (p) { return text.indexOf('error: ' + p + ': ') === 0 || text.indexOf('; ' + p + ': ') >= 0; }); }
  function failed(check, own) {
    var src = sources();
    return Object.keys(src).filter(function (k) {
      var text = String(src[k]), hit = named(text);
      return (k === check || k.indexOf(check + '-') === 0) && !/^ok( |$)/.test(text) && (!own || !hit.length || hit.some(function (p) { return own.indexOf(p) >= 0; }));
    }).map(function (k) {
      return (k === check ? '' : k.slice(check.length + 1) + ': ') + src[k];
    }).join(', ');
  }
  function quiet(check) { return /^ok \u00b7 no data( |$)/.test(String(sources()[check] || '')); }
  function empty(cols, text) { return '<tr class="empty"><td colspan="' + cols + '">' + esc(text) + '</td></tr>'; }
  function table(name, head, body) {
    return '<div class="box"><table data-cf="' + name + '"><thead><tr>' + head.map(function (h) { return '<th' + (h[1] ? ' class="num"' : '') + '>' + esc(h[0]) + '</th>'; }).join('') +
      '</tr></thead><tbody>' + body + '</tbody></table></div>';
  }
  function heading(text) { return '<h3 class="sec-t">' + esc(text) + '</h3>'; }
  function note(text, attr) { return '<p class="panel-note"' + (attr ? ' data-cf="' + attr + '"' : '') + '>' + esc(text) + '</p>'; }

  function failedNote(check, attr, what) {
    var bad = failed(check);
    return bad ? note(what + ' did not answer on the last check (' + bad + ').', attr) : '';
  }
  function refusedPart(summaries, name) {
    var parts = [];
    summaries.forEach(function (s) { (Array.isArray(s.noAccess) ? s.noAccess : []).forEach(function (a) { if (a && typeof a === 'object') parts.push(String(a.part)); }); });
    return parts.indexOf('zones') >= 0 ? 'zones' : parts.indexOf(name) >= 0 ? name : '';
  }
  function notRead(part) { return 'Not read: no access to ' + part + '.'; }
  function checksBody(summaries) {
    var found = part('checks'), bad = failed('cf', summaries.length ? ['zones', 'health checks'] : null), refused = refusedPart(summaries, 'health checks');
    if (!found.length && bad) return empty(5, 'Cloudflare did not answer on the last check (' + bad + ').');
    if (!found.length && refused) return empty(5, notRead(refused));
    if (!found.length) return empty(5, summaries.length ? 'No health checks on the watched zones.' : 'No Cloudflare data yet. The cf check fills this on its next run.');
    return found.map(function (r) {
      var cards = exact('cf-health|cloudflare/' + r.name + '|' + r.zone, r);
      var sev = cards.length ? cards[0].sev : r.status === 'healthy' ? 'ok' : r.status === 'unhealthy' ? 'crit' : 'warn';
      return '<tr><td>' + badge(sev, String(r.status || DASH)) + tags(cards) + '</td><td><span class="svc">' + esc(r.zone) + where(r) + '</span></td><td><span class="svc">' + esc(r.name) + '</span></td>' +
        '<td class="msg">' + esc(r.path) + '</td><td class="msg">' + esc(r.reason || '') + '</td></tr>';
    }).join('');
  }
  function unread(summaries, cols, own) {
    var bad = failed('cf', summaries.length ? own : null);
    if (bad) return empty(cols, 'Cloudflare did not answer on the last check (' + bad + ').');
    return summaries.length ? '' : empty(cols, 'No Cloudflare data yet. The cf check fills this on its next run.');
  }
  function componentsPart(summaries) {
    var found = part('components');
    var off = summaries.length && summaries.every(function (s) { return s.statusOff === true; });
    if (!found.length && off) return note('The status-page check is off: set Status components to watch parts of the Cloudflare status page.', 'status-off');
    var body = !found.length && unread(summaries, 2, ['status']) ? unread(summaries, 2, ['status']) : found.length ? found.map(function (r) {
      var status = String(r.status || ''), cards = exact('cf-status|cloudflare/' + r.name, r);
      var sev = cards.length ? cards[0].sev : status === 'operational' ? 'ok' : /outage/.test(status) ? 'crit' : 'info';
      return '<tr><td>' + badge(sev, status.replace(/_/g, ' ') || DASH) + tags(cards) + '</td><td><span class="svc">' + esc(r.name) + where(r) + '</span></td></tr>';
    }).join('') : empty(2, 'None of the watched components was on the Cloudflare status page on the last check.');
    return table('components', [['Status'], ['Component']], body);
  }
  function certsBody(summaries) {
    var found = part('certs'), refused = refusedPart(summaries, 'certificates');
    if (!found.length && refused) return empty(4, notRead(refused));
    if (!found.length && unread(summaries, 4, ['zones', 'certificates'])) return unread(summaries, 4, ['zones', 'certificates']);
    if (!found.length) return empty(4, 'No certificate packs on the watched zones.');
    return found.map(function (r) {
      var cards = exact('cf-cert|cloudflare/' + r.type + '|' + r.zone + '|' + r.id, r), at = Date.parse(r.expires);
      var days = isNaN(at) ? null : Math.round((at - Date.now()) / 86400000);
      var sev = cards.length ? cards[0].sev : r.status === 'active' ? 'ok' : 'warn';
      var when = days == null ? DASH : 'expires in ' + days + ' days (' + esc(String(r.expires).slice(0, 10)) + ')';
      return '<tr><td>' + badge(sev, String(r.status || DASH)) + tags(cards) + '</td><td><span class="svc">' + esc(r.zone) + where(r) + '</span></td><td>' + esc(r.type) + ' certificate</td><td>' + when + '</td></tr>';
    }).join('');
  }
  function edgeRefused() {
    var src = sources();
    var hit = Object.keys(src).map(function (k) { return (k === 'edge' || k.indexOf('edge-') === 0) && / no access to ([a-z, ]+?)( \u00b7 |$)/.exec(String(src[k])); }).filter(Boolean)[0];
    return hit ? hit[1] : '';
  }
  function edgeBody() {
    var found = rows('edge'), bad = failed('edge'), refused = edgeRefused();
    if (!found.length) return empty(10, bad ? 'The edge check did not answer on the last check (' + bad + ').' : refused ? notRead(refused) : 'No edge data yet. The edge check fills this on its next run.');
    return found.map(function (r) {
      var cards = open(function (k) { return k.indexOf('edge|' + r.zone + '|') === 0; }, r);
      var status = cards.length ? badge(cards[0].sev) : r.evaluated ? badge('ok') : badge('ok', 'Quiet');
      var start = Date.parse(r.at), how = r.req == null ? 'no requests in this hour' : r.running ? 'hour so far' : 'complete hour';
      var when = isNaN(start) ? DASH : hm(r.at) + DASH + hm(new Date(start + HOUR).toISOString());
      return '<tr><td>' + status + tags(cards) + '</td><td><span class="svc">' + esc(r.zone) + where(r) + '</span><span class="ns">' + esc(how) + '</span></td>' +
        '<td class="num">' + num(r.req) + (r.uniq ? '<span class="ns">' + num(r.uniq) + ' unique</span>' : '') + '</td><td class="num">' + share(r.cachedPct) + '</td>' +
        '<td class="num">' + num(r.conn) + '</td><td class="num">' + num(r.srv) + '</td><td class="num">' + (r.evaluated ? share(r.connShare) : DASH) + '</td>' +
        '<td class="num">' + share(r.connD1) + '</td><td class="num">' + share(r.connD7) + '</td><td>' + esc(when) + ' ' + out(r.link, 'Cloudflare') + '</td></tr>';
    }).join('');
  }
  function zoneNotes(summaries) {
    var missing = [], bare = [], refused = [];
    summaries.forEach(function (s) {
      missing = missing.concat(list(s.missing));
      bare = bare.concat(list(s.noHealthChecks));
      (Array.isArray(s.noAccess) ? s.noAccess : []).forEach(function (a) {
        if (a && typeof a === 'object') refused.push('Cloudflare cannot read ' + String(a.part) + (list(a.zones).length ? ' of ' + list(a.zones).join(', ') : '') + ': the token ' + (a.invalid === true ? 'is invalid or lacks ' : 'lacks ') + String(a.permission) + '.');
      });
    });
    return refused.map(function (t) { return note(t, 'no-access'); }).join('') +
      (missing.length ? note('Zones the token cannot see, so nothing of them is read: ' + missing.join(', ') + '.') : '') +
      (bare.length ? note('Zones with no health checks (the zone plan has none): ' + bare.join(', ') + '.') : '');
  }
  function edgeAccess() {
    var needs = {zones: 'Zone Read', analytics: 'Analytics Read'};
    return Object.keys(sources()).map(function (k) {
      var m = (k === 'edge' || k.indexOf('edge-') === 0) && / no access to ([a-z, ]+?)( \u00b7 |$)/.exec(String(sources()[k]));
      if (!m) return '';
      var parts = m[1].split(', '), cluster = k === 'edge' ? '' : k.slice(5);
      var named = open(function (key) { return key.indexOf('no-access|-/edge|') === 0; }, {cluster: cluster});
      if (named.length) return named.map(function (a) { return note(String(a.label) + '.', 'no-access'); }).join('');
      return note('Edge traffic' + (k === 'edge' ? '' : ' (' + k.slice(5) + ')') + ': no access to ' + parts.join(', ') + '; the token lacks ' +
        parts.map(function (p) { return needs[p] || 'the needed permission'; }).join(', ') + '.', 'no-access');
    }).join('');
  }
  function draw() {
    if (!state || !section) return;
    var summaries = part('summary');
    var edgeNote = 'Every request that reached the Cloudflare edge for the watched zones in the hour, from the GraphQL Analytics API: the cached share and the requests that ended 5xx at the edge. ' +
      'A 502 or 520\u2013526 that the edge makes never reaches the origin, so this is where a problem between Cloudflare and the origin shows. Zones are flagged on the share of requests, with a floor ' +
      '(the Cloudflare edge rules); a zone under the floor is shown as Quiet. 4xx are left out. The 7 d column comes from a sampled dataset.';
    var cfFailed = part('checks').length || part('certs').length || part('components').length ? failedNote('cf', 'failed-cf', 'Cloudflare') : '';
    section.innerHTML = heading('Health checks') + cfFailed + table('checks', [['Status'], ['Zone'], ['Health check'], ['Path'], ['Detail']], checksBody(summaries)) + zoneNotes(summaries) +
      heading('Cloudflare status') + componentsPart(summaries) +
      heading('Certificate packs') + table('certs', [['Status'], ['Zone'], ['Certificate'], ['Expires']], certsBody(summaries)) +
      heading('Edge traffic and 5xx, per zone') +
      (rows('edge').length ? failedNote('edge', 'failed-edge', 'The edge check') : '') + edgeAccess() +
      table('edge', [['Status'], ['Zone'], ['Requests', 1], ['Cached', 1], ['502 / 52x', 1], ['Other 5xx', 1], ['502 / 52x share', 1], ['1 d', 1], ['7 d', 1], ['Hour']], edgeBody()) +
      note(edgeNote + (quiet('edge') ? ' Cloudflare reported no traffic for the watched zones on the last check.' : ''));
  }

  document.addEventListener('skopax:state', function (e) { state = e.detail && e.detail.state && typeof e.detail.state === 'object' ? e.detail.state : null; draw(); });
  document.addEventListener('skopax:page', function (e) {
    if (!e.detail || e.detail.tab !== 'cf') return;
    section = e.detail.section;
    draw();
  });
})();
