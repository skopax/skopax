(function () {
  var TAB = 'batch-jobs';
  var SEV = {crit: ['Critical', 'k-crit', 0], warn: ['Warning', 'k-warn', 1], info: ['Info', 'k-info', 2]};
  var NOTE = 'Each row is one batch job, read from Prometheus over the last 60 minutes: its runs, failed runs, average and longest run against the same window ' +
    '1 and 7 days earlier, when it last ran and its interval (24 hours divided by its runs in 24 hours). The status follows the open batch job cards.';
  var DASH = '-';
  var state = null, section = null;

  function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }
  function real(v) { return typeof v === 'number' && isFinite(v); }
  function secs(v) { var x = Math.round(v); return x < 120 ? x + ' s' : x < 7200 ? Math.floor(x / 60) + ' min' : x < 172800 ? Math.floor(x / 3600) + ' h' : Math.floor(x / 86400) + ' d'; }
  function num(v) { return !real(v) ? '<span class="zero-v">' + DASH + '</span>' : v === 0 ? '<span class="zero-v">0</span>' : esc(Math.round(v).toLocaleString('en-US')); }
  function many(v, word) { return num(v) + ' ' + word + (v === 1 ? '' : 's'); }
  function dur(v) { return real(v) ? esc(secs(v)) : '<span class="zero-v">' + DASH + '</span>'; }
  function trio(r, k, f) { return '<td class="num">' + f(r[k]) + '</td><td class="num">' + f(r[k + 'D1']) + '</td><td class="num">' + f(r[k + 'D7']) + '</td>'; }
  function sevOf(a) { return SEV[a.sev] || SEV.info; }
  function open(a) { return a && a.status === 'open' && !a.muted && a.triage !== 'progress' && a.triage !== 'handled'; }
  function where(x) { return x && typeof x.cluster === 'string' ? x.cluster : ''; }
  function part(v) { return String(v == null ? '' : v).replace(/%/g, '%25').replace(/\//g, '%2F').replace(/\|/g, '%7C'); }
  function subject(a) { return typeof a.key === 'string' ? a.key.split('|')[2] : ''; }
  function ago(at, t) { var now = Date.parse(at) / 1000; return real(t) && real(now) ? esc(secs(Math.max(0, now - t))) + ' ago' : '<span class="zero-v">' + DASH + '</span>'; }

  function cardsOf(sum, subj) {
    return ((state && state.anomalies) || []).filter(function (a) { return open(a) && a.src === TAB && subject(a) === subj && where(a) === where(sum); })
      .sort(function (x, y) { return sevOf(x)[2] - sevOf(y)[2]; });
  }

  function tags(list) {
    return list.length ? '<div class="sig-tags">' + list.map(function (a) {
      return '<a class="tag ' + sevOf(a)[1] + '" href="#a=' + esc(encodeURIComponent(a.key)) + '" title="' + esc(a.short || '') + '">' + esc(a.label || a.sig) + '</a>';
    }).join('') + '</div>' : '';
  }

  function badge(cls, label) { return '<span class="state ' + esc(cls) + '">' + esc(label) + '</span>'; }

  function row(r, sum) {
    var list = cardsOf(sum, part(r.group) + '/' + part(r.job));
    var st = list.length ? badge(sevOf(list[0])[1], sevOf(list[0])[0]) : badge('k-ok', 'OK');
    return '<tr><td>' + st + tags(list) + '</td><td><span class="svc">' + esc(r.job) + '</span></td>' +
      trio(r, 'runs', num) + trio(r, 'fails', num) + trio(r, 'avg', dur) +
      '<td class="num">' + dur(r.max) + '</td><td class="num">' + ago(sum.at, r.last) + '</td><td class="num">' + dur(r.interval) + '</td></tr>';
  }

  function group(name, rows, sum) {
    var html = '<h4 class="sec-t">' + esc(name || DASH) + '</h4>' + tags(cardsOf(sum, part(name)));
    html += '<div class="box"><table><thead><tr><th>Status</th><th>Job</th><th class="num">Runs</th><th class="num">1 d</th><th class="num">7 d</th>' +
      '<th class="num">Failed</th><th class="num">1 d</th><th class="num">7 d</th><th class="num">Average</th><th class="num">1 d</th><th class="num">7 d</th>' +
      '<th class="num">Longest</th><th class="num">Last run</th><th class="num">Interval</th></tr></thead><tbody>';
    return html + rows.map(function (r) { return row(r, sum); }).join('') + '</tbody></table></div>';
  }

  function instance(sum, all) {
    var jobs = all.filter(function (r) { return r.part === 'job' && where(r) === where(sum); });
    var groups = Object.create(null);
    jobs.forEach(function (r) { var g = String(r.group == null ? '' : r.group); (groups[g] = groups[g] || []).push(r); });
    var html = '<h3 class="sec-t">' + esc(sum.name || where(sum) || 'Batch jobs') + '</h3>' +
      '<p>' + many(sum.jobs, 'job') + ' in ' + many(sum.groups, 'group') + ', from <b>' + esc(sum.metric || '') + '</b>' + (sum.selector ? ' where ' + esc(sum.selector) : '') + '</p>';
    var names = Object.keys(groups).sort();
    return html + (names.length ? names.map(function (g) { return group(g, groups[g], sum); }).join('') : '<p>No jobs in the last pass.</p>');
  }

  function render() {
    if (!section) return;
    var all = (state && Array.isArray(state[TAB]) ? state[TAB] : []).filter(function (r) { return r && typeof r === 'object'; });
    var sums = all.filter(function (r) { return r.part === 'summary'; });
    var html = '<p class="panel-note">' + esc(NOTE) + '</p>';
    html += sums.length ? sums.map(function (s) { return instance(s, all); }).join('') : '<p>No batch job data yet. The batch jobs check fills this on its next run.</p>';
    section.innerHTML = html;
  }

  document.addEventListener('skopax:state', function (e) { state = e.detail && e.detail.state; render(); });
  document.addEventListener('skopax:page', function (e) {
    if (!e.detail || e.detail.tab !== TAB) return;
    section = e.detail.section;
    render();
  });
})();
