(function () {
  var TAB = 'airbyte';
  var PASS = 'airbyte';
  var JOB = {succeeded: ['Succeeded', 'k-ok'], running: ['Running', 'k-info'], pending: ['Pending', 'k-info'], failed: ['Failed', 'k-warn'], incomplete: ['Incomplete', 'k-warn'], cancelled: ['Cancelled', 'k-muted']};
  var SEV = {crit: ['Critical', 'k-crit', 0], warn: ['Warning', 'k-warn', 1], info: ['Info', 'k-info', 2]};
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var NOTE = 'The last sync job of each Airbyte connection the check watches.';
  var ABOUT = 'The check reads the Airbyte public API, read only. Usual is the median of the connection\u2019s successful runs among its recent jobs. ' +
    'Airbyte retries a failed attempt inside the same job and its API has no attempt count, so a run far longer than usual is most likely retrying.';
  var DASH = '\u2013', ARROW = ' \u2197', DOT = ' \u00b7 ';
  var MISSING = ['Not listed', 'k-warn'];
  var DENIED = ['No access', 'k-warn'];
  var UNREAD = ['Not read', 'k-muted'];
  var state = null, section = null;

  function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }
  function https(u) { return typeof u === 'string' && u.indexOf('https://') === 0; }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function when(iso) { var d = typeof iso === 'string' ? new Date(iso) : null; return d && !isNaN(d) ? d : null; }
  function hm(iso) { var d = when(iso); return d ? pad(d.getHours()) + ':' + pad(d.getMinutes()) : ''; }
  function dayhm(iso) {
    var d = when(iso);
    if (!d) return '';
    return (d.toDateString() === new Date().toDateString() ? '' : d.getDate() + ' ' + MONTHS[d.getMonth()] + ' ') + hm(iso);
  }
  function real(v) { return typeof v === 'number' && isFinite(v); }
  function dur(v) {
    if (!real(v)) return DASH;
    var x = Math.round(v);
    return x < 120 ? x + ' s' : x < 7200 ? Math.floor(x / 60) + ' min' : Math.floor(x / 3600) + ' h ' + Math.floor(x % 3600 / 60) + ' min';
  }
  function count(v) { return real(v) ? Math.round(v).toLocaleString('en-US') : DASH; }
  function sevOf(a) { return SEV[a.sev] || SEV.info; }
  function open(a) { return a && a.status === 'open' && !a.muted && a.triage !== 'progress' && a.triage !== 'handled'; }

  function cardsOf(r) {
    var head = 'airbyte|' + (r.name || r.id) + '|';
    return (state.anomalies || []).filter(function (a) { return open(a) && String(a.key).indexOf(head) === 0 && (a.cluster || '') === (r.cluster || ''); })
      .sort(function (x, y) { return sevOf(x)[2] - sevOf(y)[2]; });
  }

  function failures() {
    var src = state.sources && typeof state.sources === 'object' ? state.sources : {};
    return Object.keys(src).filter(function (k) { return (k === PASS || k.indexOf(PASS + '-') === 0) && !/^ok( |$)/.test(String(src[k])); })
      .map(function (k) { return (k === PASS ? '' : k.slice(PASS.length + 1) + ': ') + src[k]; });
  }

  function refusals() {
    var src = state.sources && typeof state.sources === 'object' ? state.sources : {};
    return Object.keys(src).filter(function (k) { return (k === PASS || k.indexOf(PASS + '-') === 0) && String(src[k]).indexOf('no access to') !== -1; })
      .map(function (k) { return (k === PASS ? '' : k.slice(PASS.length + 1) + ': ') + src[k]; });
  }

  function tag(a) {
    return '<a class="tag ' + sevOf(a)[1] + '" href="#a=' + esc(encodeURIComponent(a.key)) + '" title="' + esc(a.short || '') + '">' + esc(a.label || 'open card') + '</a>';
  }

  function jobLink(r, status) {
    if (r.job == null || !https(r.jobUrl)) return '';
    var tip = 'job ' + r.job + DOT + status + (r.start ? DOT + 'started ' + dayhm(r.start) : '') + (real(r.durS) ? DOT + dur(r.durS) : '') + DOT + 'opens its logs on the Airbyte timeline';
    return ' <a class="lnk" href="' + esc(r.jobUrl) + '" target="_blank" rel="noopener" title="' + esc(tip) + '">Job ' + esc(r.job) + ARROW + '</a>';
  }

  function row(r) {
    var list = cardsOf(r);
    var j = r.missing ? MISSING : r.jobsDenied ? DENIED : r.jobsFailed ? UNREAD : JOB[r.status] || [r.status || 'No job', 'k-muted'];
    var status = list.length ? '<span class="state ' + sevOf(list[0])[1] + '">' + esc(sevOf(list[0])[0]) + '</span> <div class="sig-tags">' + list.map(tag).join('') + '</div>'
      : '<span class="state ' + j[1] + '">' + esc(j[0]) + '</span>';
    var run = r.start ? dayhm(r.start) + DASH + (r.end ? hm(r.end) : 'now') : DASH;
    var usual = 'median of ' + (real(r.okRuns) ? r.okRuns : 0) + ' successful runs among the recent jobs' + (real(r.usualRows) ? ', ' + count(r.usualRows) + ' rows' : '');
    var sub = [r.cluster, r.schedule].filter(function (x) { return x; }).join(DOT);
    return '<tr><td>' + status + '</td><td><span class="svc">' + esc(r.name || r.id) + '</span>' + (sub ? '<span class="ns">' + esc(sub) + '</span>' : '') + '</td>' +
      '<td class="when"' + (r.job != null ? ' title="job ' + esc(r.job) + '"' : '') + '>' + esc(run) + '</td><td class="num">' + esc(dur(r.durS)) + '</td>' +
      '<td class="num" title="' + esc(usual) + '">' + esc(dur(r.usualS)) + '</td><td class="num">' + esc(count(r.rows)) + '</td>' +
      '<td>' + (https(r.url) ? '<a class="lnk" href="' + esc(r.url) + '" target="_blank" rel="noopener">Timeline' + ARROW + '</a>' : '') + jobLink(r, j[0]) + '</td></tr>';
  }

  function render() {
    if (!state || !section) return;
    var rows = (Array.isArray(state.airbyte) ? state.airbyte : []).filter(function (r) { return r && typeof r === 'object'; });
    var allBad = failures();
    var partial = allBad.filter(function (x) { return /(^|: )error: jobs: /.test(x); });
    var bad = allBad.filter(function (x) { return partial.indexOf(x) < 0; });
    var first = rows.filter(function (r) { return !r.cluster && https(r.url); })[0];
    var all = first ? String(first.url).replace(/\/connections\/.*$/, '/connections') : '';
    var refused = refusals();
    var note = NOTE + (rows.length && bad.length ? ' Airbyte did not answer on the last check (' + bad.join(', ') + '), so these rows are from the check before.' : '') +
      (rows.length && partial.length ? ' Airbyte did not answer some reads on the last check (' + partial.join(', ') + '); a connection it could not read shows Not read.' : '') +
      (rows.length && refused.length ? ' Airbyte refused a read on the last check (' + refused.join(', ') + '), so some rows may be from the check before.' : '');
    var denied = refused.map(function (x) { var m = / no access to ([^\u00b7]+?)( \u00b7 |$)/.exec(x); return m ? m[1] : ''; }).filter(Boolean)[0];
    var body = rows.length ? rows.map(row).join('')
      : '<tr class="empty"><td colspan="7">' + esc(allBad.length ? 'Airbyte did not answer on the last check (' + allBad.join(', ') + ').' : denied ? 'Not read: no access to ' + denied + '.' : 'No Airbyte data yet. The Airbyte check fills this on its next run.') + '</td></tr>';
    section.innerHTML = '<p class="panel-note"><span>' + esc(note) + '</span> <span class="q" tabindex="0" role="img" aria-label="About Airbyte syncs" title="' + esc(ABOUT) + '">?</span>' +
      (all ? ' <a class="lnk" href="' + esc(all) + '" target="_blank" rel="noopener">All connections' + ARROW + '</a>' : '') + '</p>' +
      '<div class="box"><table><thead><tr><th>Status</th><th>Connection</th><th>Last run</th><th class="num">Took</th>' +
      '<th class="num" title="median of the connection\u2019s successful runs among its recent jobs">Usual</th><th class="num">Rows</th><th>Airbyte</th></tr></thead><tbody>' + body + '</tbody></table></div>';
  }

  document.addEventListener('skopax:state', function (e) { state = e.detail && e.detail.state ? e.detail.state : null; render(); });
  document.addEventListener('skopax:page', function (e) {
    if (!e.detail || e.detail.tab !== TAB) return;
    section = e.detail.section;
    render();
  });
})();
