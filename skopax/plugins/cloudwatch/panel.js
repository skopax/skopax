(function () {
  var TAB = 'cloudwatch', PLUGIN = 'cloudwatch', ALARMS = 'cloudwatchAlarms', LOGS = 'cloudwatchLogs', PASS = 'cloudwatch', LOG_PASS = 'cloudwatch-logs';
  var DASH = '–', DOT = ' · ', OUT = ' ↗', ALL = '*', HOUR_MIN = 60, SAMPLES = 3;
  var REGION = /^[a-z]{2}(?:-[a-z]+)+-\d{1,2}$/;
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var SEV = {crit: 'k-crit', high: 'k-high', warn: 'k-warn', info: 'k-info', ok: 'k-ok'};
  var SEV_LABEL = {crit: 'Critical', high: 'High', warn: 'Warning', info: 'Info'};
  var RANK = {crit: 0, high: 1, warn: 2, info: 3};
  var STATES = ['ALARM', 'INSUFFICIENT_DATA', 'OK'];
  var STATE_TONE = {ALARM: 'k-crit', INSUFFICIENT_DATA: 'k-warn', OK: 'k-ok'};
  var state = null, section = null, bar = null, body = null;
  var pick = {inst: ALL, region: ALL, st: ALL, q: ''};

  function h(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = String(text); return e; }
  function remote(tag, cls, text) { var e = h(tag, cls, text); e.setAttribute('data-no-tx', ''); return e; }
  function add(parent) { for (var i = 1; i < arguments.length; i++) if (arguments[i]) parent.appendChild(arguments[i]); return parent; }
  function obj(v) { return v && typeof v === 'object' && !Array.isArray(v); }
  function real(v) { return typeof v === 'number' && isFinite(v); }
  function str(v) { return typeof v === 'string' ? v : ''; }
  function https(u) { return typeof u === 'string' && /^https:\/\/[^\s"'<>]+$/.test(u); }
  function count(v) { return real(v) ? Math.round(v).toLocaleString('en-US') : DASH; }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function when(iso) { var d = typeof iso === 'string' && iso ? new Date(iso) : null; return d && !isNaN(d) ? d : null; }
  function hm(iso) { var d = when(iso); return d ? pad(d.getHours()) + ':' + pad(d.getMinutes()) : ''; }
  function dayhm(iso) { var d = when(iso); return d ? (d.toDateString() === new Date().toDateString() ? '' : d.getDate() + ' ' + MONTHS[d.getMonth()] + ' ') + hm(iso) : ''; }
  function inst(r) { return str(r && r.cluster); }
  function rows(name) { return state && Array.isArray(state[name]) ? state[name].filter(function (r) { return obj(r) && (r.plugin || PLUGIN) === PLUGIN; }) : []; }
  function part(name, which) { return rows(name).filter(function (r) { return r.part === which; }); }
  function sources() { return state && obj(state.sources) ? state.sources : {}; }
  function source(pass, name) { var v = sources()[name ? pass + '-' + name : pass]; return typeof v === 'string' ? v : ''; }
  function instances() {
    var seen = {};
    [ALARMS, LOGS].forEach(function (s) { rows(s).forEach(function (r) { seen[inst(r)] = 1; }); });
    Object.keys(sources()).forEach(function (k) {
      if (k === PASS || k === LOG_PASS) seen[''] = 1;
      else if (k.indexOf(LOG_PASS + '-') === 0) seen[k.slice(LOG_PASS.length + 1)] = 1;
      else if (k.indexOf(PASS + '-') === 0) seen[k.slice(PASS.length + 1)] = 1;
    });
    return Object.keys(seen).sort(function (a, b) { return a === '' ? -1 : b === '' ? 1 : a < b ? -1 : a > b ? 1 : 0; });
  }
  function shownInst(name) { return pick.inst === ALL || pick.inst === name; }
  function summaryOf(section, name) { return part(section, 'summary').filter(function (r) { return inst(r) === name; })[0] || null; }
  function regionOf(name) { var s = summaryOf(ALARMS, name) || summaryOf(LOGS, name); return s ? str(s.region) : ''; }
  function regions() { var seen = {}; instances().forEach(function (n) { var r = regionOf(n); if (r) seen[r] = 1; }); return Object.keys(seen).sort(); }
  function consoleOf(region) {
    return REGION.test(region) ? 'https://' + region + (region.indexOf('cn-') === 0 ? '.console.amazonaws.cn' : '.console.aws.amazon.com') + '/cloudwatch/home?region=' + region : '';
  }
  function alarmRegion(r) { var s = summaryOf(ALARMS, inst(r)); return s ? str(s.region) : ''; }
  function logRegion(r) { var s = summaryOf(LOGS, inst(r)); return s ? str(s.region) : ''; }
  function alarmKey(r) { return 'cloudwatch|' + alarmRegion(r) + '|' + (str(r.keyName) || str(r.name)); }
  function logKey(r) { return 'cloudwatch-logs|' + logRegion(r) + '|' + (str(r.keyName) || str(r.group)); }
  function alarmUrl(r) { var base = consoleOf(alarmRegion(r)); return base && r.noLink !== true && str(r.name) ? base + '#alarmsV2:alarm/' + encodeURIComponent(r.name).replace(/['()!*]/g, function (c) { return '%' + c.charCodeAt(0).toString(16).toUpperCase(); }) : ''; }
  function logUrl(r) {
    var base = consoleOf(logRegion(r));
    return base && r.noLink !== true && str(r.group) ? base + '#logsV2:log-groups/log-group/' + encodeURIComponent(encodeURIComponent(r.group)).replace(/%/g, '\x24') : '';
  }
  function inRegion(region) { return pick.region === ALL || region === pick.region; }
  function matches(texts) { var q = pick.q.trim().toLowerCase(); return !q || texts.some(function (t) { return String(t || '').toLowerCase().indexOf(q) >= 0; }); }
  function raised(a) { var tail = a.cluster ? '@' + a.cluster : '', key = String(a.key); return tail && key.slice(-tail.length) === tail ? key.slice(0, -tail.length) : key; }
  function cards(key, name) {
    return (state && Array.isArray(state.anomalies) ? state.anomalies : []).filter(function (a) {
      return obj(a) && a.status === 'open' && raised(a) === key && str(a.cluster) === name;
    }).sort(function (x, y) { return (x.sev in RANK ? RANK[x.sev] : 9) - (y.sev in RANK ? RANK[y.sev] : 9); });
  }
  function active(found) { return found.filter(function (a) { return !a.muted && a.triage !== 'progress' && a.triage !== 'handled'; }); }
  function tags(found) {
    if (!found.length) return null;
    var box = h('div', 'sig-tags');
    found.forEach(function (a) {
      var t = remote('a', 'tag ' + (SEV[a.sev] || SEV.info), a.label || String(a.key).split('|')[0]);
      t.setAttribute('href', '#a=' + encodeURIComponent(String(a.key)));
      if (a.short) t.setAttribute('title', String(a.short));
      box.appendChild(t);
    });
    return box;
  }
  function link(url, text) {
    if (!https(url)) return null;
    var a = h('a', 'lnk', text + OUT);
    a.setAttribute('href', url);
    a.setAttribute('target', '_blank');
    a.setAttribute('rel', 'noopener');
    return a;
  }
  function badge(tone, text, raw) { return (raw ? remote : h)('span', 'state ' + tone, text); }
  function heading(text) { return h('h3', 'sec-t', text); }
  function note(text, attr, tail, lead) {
    var p = h('p', 'panel-note');
    if (attr) p.setAttribute('data-cw', attr);
    if (lead != null) add(p, lead ? remote('b', '', lead) : h('b', '', 'Primary'), document.createTextNode(': '));
    add(p, h('span', '', text));
    if (tail) add(p, document.createTextNode(' '), remote('span', '', tail));
    return p;
  }
  function cell(row, content, cls) { var td = h('td', cls || ''); if (content != null) add(td, typeof content === 'string' ? h('span', '', content) : content); row.appendChild(td); return td; }
  function table(attr, heads, fill, cols, emptyText) {
    var box = h('div', 'box'), t = h('table'), head = h('thead'), tr = h('tr'), tb = h('tbody');
    t.setAttribute('data-cw', attr);
    heads.forEach(function (x) { tr.appendChild(h('th', x[1] ? 'num' : '', x[0])); });
    add(box, add(t, add(head, tr), tb));
    fill(tb);
    if (!tb.children.length) { var e = h('tr', 'empty'), td = h('td', '', emptyText); td.setAttribute('colspan', String(cols)); add(tb, add(e, td)); }
    return box;
  }
  function where(name) { return instances().length > 1 ? (name || '') : ''; }

  function failure(pass, name) {
    var s = source(pass, name);
    return s && !/^ok( |$)/.test(s) && !/^off( |:|$)/.test(s) ? s.replace(/^error:\s*/, '') : '';
  }
  function denied(pass, name, what) { return new RegExp(' no access to ' + what + '( · |$)').test(source(pass, name)); }
  function alarmNotes(names) {
    var out = [], lead = function (n) { return instances().length > 1 ? n : null; };
    names.forEach(function (n) {
      var bad = failure(PASS, n), s = summaryOf(ALARMS, n);
      if (bad) out.push(note('The last CloudWatch alarm check failed:', 'failed-alarms', bad, lead(n)));
      if (denied(PASS, n, 'alarms')) out.push(note('Not read: no access to alarms. The credentials lack the cloudwatch:DescribeAlarms permission.', 'no-access', '', lead(n)));
      if (!s) return;
      if (s.alarmsCut === true) out.push(note('Read 500 alarms in ALARM and stopped, so the ALARM count is at least the number shown.', 'capped', '', lead(n)));
      if (s.countsCut === true) out.push(note('The OK and INSUFFICIENT_DATA counts stop after 10 pages of alarms, so they are at least the number shown.', 'counts-cut', '', lead(n)));
      if (str(s.countsError)) out.push(note('The OK and INSUFFICIENT_DATA counts could not be read:', 'counts-error', str(s.countsError), lead(n)));
      if (s.listCut === true) out.push(note('Not every alarm in ALARM or INSUFFICIENT_DATA is listed: the page keeps at most 200 alarms and about 60 KB of them per instance.', 'list-cut', '', lead(n)));
    });
    names.forEach(function (n) {
      if (!summaryOf(ALARMS, n) && !failure(PASS, n) && !denied(PASS, n, 'alarms')) out.push(note('Not read yet: the alarm check has stored no result for this instance.', 'unread-alarms', '', lead(n)));
    });
    return out;
  }
  function countText(v, more) { return real(v) ? count(v) + (more ? '+' : '') : DASH; }
  function countsTable(names) {
    return table('counts', [['Region'], ['Instance'], ['ALARM', 1], ['INSUFFICIENT_DATA', 1], ['OK', 1], ['Checked']], function (tb) {
      names.forEach(function (n) {
        var s = summaryOf(ALARMS, n), tr = h('tr');
        if (!s) {
          if (pick.region !== ALL) return;
          cell(tr, remote('span', 'svc', DASH));
          cell(tr, n ? remote('span', 'svc', n) : h('span', 'ns', 'Primary'));
          var td = cell(tr, failure(PASS, n) ? 'Not read: the last check failed.' : denied(PASS, n, 'alarms') ? 'Not read: no access to alarms.' : 'Not read yet', 'msg');
          td.setAttribute('colspan', '4');
          tb.appendChild(tr);
          return;
        }
        if (!inRegion(str(s.region))) return;
        var c = obj(s.counts) ? s.counts : {};
        cell(tr, remote('span', 'svc', str(s.region) || DASH));
        cell(tr, n ? remote('span', 'svc', n) : h('span', 'ns', 'Primary'));
        [['ALARM', s.alarmsCut === true], ['INSUFFICIENT_DATA', s.countsCut === true], ['OK', s.countsCut === true]].forEach(function (x) {
          var td = cell(tr, null, 'num'), v = c[x[0]];
          add(td, real(v) && v > 0 && x[0] !== 'OK' ? badge(STATE_TONE[x[0]], countText(v, x[1]), true) : remote('span', '', countText(v, x[1])));
        });
        cell(tr, remote('span', '', dayhm(s.at) || DASH));
        tb.appendChild(tr);
      });
    }, 6, names.length && pick.region !== ALL ? 'No instance in the picked region.' : 'No CloudWatch alarm data yet. The alarm check fills this on its next run.');
  }
  function alarmTable(names) {
    var all = part(ALARMS, 'alarm').filter(function (r) { return names.indexOf(inst(r)) >= 0; });
    var shown = all.filter(function (r) {
      return inRegion(alarmRegion(r)) && (pick.st === ALL || r.state === pick.st) && matches([r.name, r.reason, r.metric, alarmRegion(r), inst(r)]);
    });
    var read = names.filter(function (n) { return summaryOf(ALARMS, n); });
    return table('alarms', [['State'], ['Alarm'], ['Type'], ['Reason'], ['Since'], ['Links']], function (tb) {
      shown.forEach(function (r) {
        var n = inst(r), found = cards(alarmKey(r), n), live = active(found), tr = h('tr');
        var st = str(r.state) || 'ALARM', tone = live.length ? SEV[live[0].sev] || STATE_TONE[st] : STATE_TONE[st] || 'k-muted';
        cell(tr, badge(tone, st, true));
        var who = h('div');
        add(who, remote('span', 'svc', str(r.name) || DASH), str(r.metric) || where(n) ? remote('span', 'ns', [str(r.metric), where(n)].filter(Boolean).join(DOT)) : null);
        cell(tr, who);
        cell(tr, r.type === 'composite' ? 'Composite' : 'Metric');
        cell(tr, remote('span', 'msg', str(r.reason) || DASH), 'msg');
        var since = remote('span', '', dayhm(r.since) || DASH);
        if (when(r.since)) since.setAttribute('title', str(r.since));
        cell(tr, since);
        var links = h('div');
        add(links, tags(found), link(alarmUrl(r), 'AWS console'));
        cell(tr, links);
        tb.appendChild(tr);
      });
    }, 6, all.length ? 'No alarm matches the filters.' : !read.length ? 'No CloudWatch alarm data yet. The alarm check fills this on its next run.' :
      read.length < names.length ? 'No alarm is in ALARM or INSUFFICIENT_DATA on the instances read so far.' : 'No alarm is in ALARM or INSUFFICIENT_DATA.');
  }
  function logNotes(names) {
    var out = [], lead = function (n) { return instances().length > 1 ? n : null; };
    names.forEach(function (n) {
      var s = summaryOf(LOGS, n), src = source(LOG_PASS, n), bad = failure(LOG_PASS, n);
      if (/^off( |:|$)/.test(src)) out.push(note('CloudWatch Logs is off: set Log groups on the instance to count error lines.', 'logs-off', '', lead(n)));
      if (bad) out.push(note('The last CloudWatch Logs check failed:', 'failed-logs', bad, lead(n)));
      if (denied(LOG_PASS, n, 'logs')) out.push(note('Not read: no access to logs. The credentials lack the logs:StartQuery and logs:GetQueryResults permissions.', 'no-access', '', lead(n)));
      var gone = s && Array.isArray(s.missingGroups) ? s.missingGroups.map(String) : [];
      if (gone.length) out.push(note('No log group with these names:', 'missing', gone.join(', '), lead(n)));
      if (s && s.capped === true) out.push(note('More log groups match than are read: at most 50 per instance.', 'logs-cut', '', lead(n)));
      if (s && s.rowsCut === true) out.push(note('Only the log groups with the most error lines are listed: the page keeps about 40 KB of them per instance.', 'rows-cut', '', lead(n)));
      if (!s && !bad && !denied(LOG_PASS, n, 'logs') && !/^off( |:|$)/.test(src)) out.push(note('Not read yet: the Logs check has stored no result for this instance.', 'unread-logs', '', lead(n)));
    });
    return out;
  }
  function samples(r) {
    var seen = [], list = Array.isArray(r.windows) ? r.windows.filter(obj).slice().sort(function (a, b) { return (b.to || 0) - (a.to || 0); }) : [];
    list.forEach(function (w) { var s = str(w.sample); if (s && seen.indexOf(s) < 0 && seen.length < SAMPLES) seen.push(s); });
    return seen;
  }
  function logTable(names) {
    var all = part(LOGS, 'group').filter(function (r) { return names.indexOf(inst(r)) >= 0; });
    var shown = all.filter(function (r) { return inRegion(logRegion(r)) && matches([r.group, logRegion(r), inst(r)].concat(samples(r))); });
    var on = names.filter(function (n) { return !/^off( |:|$)/.test(source(LOG_PASS, n)); });
    var read = on.filter(function (n) { return summaryOf(LOGS, n); });
    return table('logs', [['Status'], ['Log group'], ['Last 15 min', 1], ['About the last hour', 1], ['Latest error lines'], ['Checked'], ['Links']], function (tb) {
      shown.forEach(function (r) {
        var n = inst(r), found = cards(logKey(r), n), live = active(found), tr = h('tr'), s = summaryOf(LOGS, n), least = s && real(s.least) ? s.least : null;
        cell(tr, live.length ? badge(SEV[live[0].sev] || SEV.info, SEV_LABEL[live[0].sev] || 'Info') : !(real(r.n) && r.n > 0) ? badge('k-ok', 'Healthy') :
          least != null && r.n >= least ? badge('k-warn', 'At or above the card line') : badge('k-info', 'Below the card line'));
        var who = h('div');
        add(who, remote('span', 'svc', str(r.group) || DASH), where(n) || logRegion(r) ? remote('span', 'ns', [logRegion(r), where(n)].filter(Boolean).join(DOT)) : null);
        cell(tr, who);
        cell(tr, remote('span', '', count(r.n)), 'num');
        var hour = cell(tr, remote('span', '', count(r.hour)), 'num');
        if (real(r.minutes) && Math.round(r.minutes) !== HOUR_MIN) add(hour, h('span', 'ns', 'over ' + Math.max(0, Math.round(r.minutes)) + ' min'));
        var lines = h('div');
        samples(r).forEach(function (s) { lines.appendChild(remote('div', 'msg', s)); });
        cell(tr, lines.children.length ? lines : remote('span', 'zero-v', DASH), 'msg');
        cell(tr, remote('span', '', (s && dayhm(s.at)) || DASH));
        var links = h('div');
        add(links, tags(found), link(logUrl(r), 'Log group in AWS'));
        cell(tr, links);
        tb.appendChild(tr);
      });
    }, 7, all.length ? 'No log group matches the filters.' : names.length && !on.length ? 'CloudWatch Logs is off on every instance.' :
      !read.length ? 'No CloudWatch Logs data yet. The Logs check fills this on its next run.' :
      read.length < on.length ? 'No watched log group was read on the instances read so far.' : 'No watched log group was read.');
  }

  function seg(attr, label, options, current, onPick) {
    var box = h('div', 'seg');
    box.setAttribute('role', 'group');
    box.setAttribute('aria-label', label);
    box.setAttribute('data-cw', attr);
    options.forEach(function (o) {
      var b = (o[2] ? remote : h)('button', '', o[1]);
      b.setAttribute('type', 'button');
      b.setAttribute('data-v', o[0]);
      b.setAttribute('aria-pressed', o[0] === current ? 'true' : 'false');
      b.addEventListener('click', function () { onPick(o[0]); });
      box.appendChild(b);
    });
    return box;
  }
  function select(attr, label, options, current, onPick) {
    var box = h('label', 'seg'), s = h('select');
    s.setAttribute('aria-label', label);
    s.setAttribute('data-cw', attr);
    options.forEach(function (o) {
      var opt = (o[2] ? remote : h)('option', '', o[1]);
      opt.value = o[0];
      if (o[0] === current) opt.selected = true;
      s.appendChild(opt);
    });
    s.addEventListener('change', function () { onPick(s.value); });
    return add(box, s);
  }
  function controls() {
    var names = instances(), regs = regions();
    if (pick.inst !== ALL && names.indexOf(pick.inst) < 0) pick.inst = ALL;
    if (pick.region !== ALL && regs.indexOf(pick.region) < 0) pick.region = ALL;
    var keep = bar.querySelector('input[data-cw="find"]'), focused = !!keep && document.activeElement === keep;
    var top = h('div', 'toolbar');
    if (names.length > 1) {
      top.appendChild(seg('inst', 'Instance', [[ALL, 'All instances']].concat(names.map(function (n) { return n ? [n, n, true] : ['', 'Primary']; })), pick.inst,
        function (v) { pick.inst = v; render(); }));
    }
    if (regs.length > 1) top.appendChild(select('region', 'Region', [[ALL, 'All regions']].concat(regs.map(function (r) { return [r, r, true]; })), pick.region, function (v) { pick.region = v; render(); }));
    top.appendChild(seg('state', 'Alarm state', [[ALL, 'All states'], ['ALARM', 'ALARM', true], ['INSUFFICIENT_DATA', 'INSUFFICIENT_DATA', true]], pick.st, function (v) { pick.st = v; render(); }));
    var find = keep;
    if (!find) {
      find = h('input');
      find.setAttribute('type', 'search');
      find.setAttribute('maxlength', '120');
      find.setAttribute('autocomplete', 'off');
      find.setAttribute('spellcheck', 'false');
      find.setAttribute('data-cw', 'find');
      find.setAttribute('aria-label', 'Search alarms and log groups');
      find.setAttribute('placeholder', 'Search alarms, reasons, log groups');
      find.addEventListener('input', function () { pick.q = find.value; paint(); });
    }
    top.appendChild(add(h('label', 'tools-find'), find));
    bar.textContent = '';
    bar.appendChild(top);
    if (focused) find.focus();
  }
  function paint() {
    if (!body) return;
    var names = instances().filter(shownInst);
    var out = document.createDocumentFragment();
    add(out, note('Alarm counts and the alarms in ALARM or INSUFFICIENT_DATA, as the CloudWatch alarm check read them on its last run (every 15 minutes). A card tag opens the card; the console link opens the alarm in AWS.'));
    add(out, heading('Alarms by region and state'));
    alarmNotes(names).forEach(function (p) { out.appendChild(p); });
    add(out, countsTable(names), heading('Alarms in ALARM or INSUFFICIENT_DATA'), alarmTable(names));
    add(out, heading('CloudWatch Logs errors'));
    add(out, note('Error lines per watched log group, from the CloudWatch Logs check: the 15 minutes of its last run, and its runs added up over about the last hour. The latest lines are the samples its query returned. This page starts no Logs Insights query of its own.'));
    logNotes(names).forEach(function (p) { out.appendChild(p); });
    add(out, logTable(names));
    body.textContent = '';
    body.appendChild(out);
  }
  function render() {
    if (!state || !section) return;
    if (!bar || !section.contains(bar)) {
      section.textContent = '';
      bar = h('div', 'cw-bar');
      body = h('div', 'pg-part');
      add(section, bar, body);
    }
    controls();
    paint();
  }

  document.addEventListener('skopax:state', function (e) { state = e.detail && obj(e.detail.state) ? e.detail.state : null; render(); });
  document.addEventListener('skopax:page', function (e) {
    if (!e.detail || e.detail.tab !== TAB) return;
    section = e.detail.section;
    bar = null;
    render();
  });
})();
