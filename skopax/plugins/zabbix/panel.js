(function () {
  var TAB = 'zabbix', SRC = 'zabbix', PAGE = 'zabbixPage';
  var DASH = '–', OUT = ' ↗';
  var LEVELS = ['Not classified', 'Information', 'Warning', 'Average', 'High', 'Disaster'];
  var TONE = ['k-info', 'k-info', 'k-warn', 'k-warn', 'k-high', 'k-crit'];
  var AVAIL = {available: ['Available', 'k-ok'], unavailable: ['Unavailable', 'k-crit'], unknown: ['Unknown', 'k-muted']};
  var KINDS = {agent: 'Agent', SNMP: 'SNMP', IPMI: 'IPMI', JMX: 'JMX', other: 'Other'};
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var NOTE = 'Hosts of the watched host groups and the open Zabbix problems at or above the lowest severity of the instance, as the last Zabbix check read them.';
  var state = null, section = null, pick = null, filter = {group: '', level: '', q: ''};

  function obj(r) { return r && typeof r === 'object'; }
  function h(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = String(text);
    return e;
  }
  function raw(tag, cls, text) { var e = h(tag, cls, text); e.setAttribute('data-no-tx', ''); return e; }
  function add(parent) { for (var i = 1; i < arguments.length; i++) if (arguments[i]) parent.appendChild(typeof arguments[i] === 'string' ? document.createTextNode(arguments[i]) : arguments[i]); return parent; }
  function https(u) { return typeof u === 'string' && u.indexOf('https://') === 0; }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function dayhm(at) {
    var d = typeof at === 'string' ? new Date(at) : null;
    if (!d || isNaN(d)) return '';
    return (d.toDateString() === new Date().toDateString() ? '' : d.getDate() + ' ' + MONTHS[d.getMonth()] + ' ') + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  function where(r) { return String(r.cluster || ''); }
  function rows(part) { return (state && Array.isArray(state[PAGE]) ? state[PAGE] : []).filter(function (r) { return obj(r) && r.part === part && where(r) === pick; }); }
  function summaries() { return (state && Array.isArray(state[PAGE]) ? state[PAGE] : []).filter(function (r) { return obj(r) && r.part === 'summary'; }); }
  function level(v) { return typeof v === 'number' && v >= 0 && v < LEVELS.length ? v : 0; }
  function badge(text, tone) { return h('span', 'state ' + tone, text); }
  function sources() { return state && obj(state.sources) ? state.sources : {}; }
  function source() { return String(sources()[pick ? SRC + '-' + pick : SRC] || ''); }
  function refused() { return /^ok\b.* no access to /.test(source()); }
  function cardsOf(r) {
    return (state && Array.isArray(state.anomalies) ? state.anomalies : []).filter(function (a) {
      return obj(a) && a.src === SRC && a.status === 'open' && String(a.triggerId || '') === String(r.triggerId || '') && String(a.cluster || '') === where(r);
    });
  }
  function note(text, attr) { var p = h('p', 'panel-note', text); if (attr) p.setAttribute('data-zbx', attr); return p; }
  function said(lead, reason, attr) { var p = note(null, attr); add(p, h('span', null, lead), ' ', raw('span', null, reason)); return p; }
  function lnk(url, text) {
    if (!https(url)) return null;
    var a = h('a', 'lnk', text + OUT);
    a.href = url;
    a.target = '_blank';
    a.rel = 'noopener';
    return a;
  }
  function cardTag(a) {
    var t = h('a', 'tag ' + (TONE[LEVELS.indexOf(String(a.zabbixSeverity))] || 'k-info'), 'Card');
    t.href = '#a=' + encodeURIComponent(String(a.key));
    t.title = String(a.short || '');
    return t;
  }
  function matches(text) { var q = filter.q.toLowerCase(); return !q || text.some(function (x) { return String(x || '').toLowerCase().indexOf(q) >= 0; }); }
  function counted(r) {
    var by = obj(r.problems) ? r.problems : {};
    return LEVELS.map(function (label, i) { return [label, i, typeof by[label] === 'number' ? by[label] : 0]; }).filter(function (x) { return x[2] > 0; }).reverse();
  }
  function hostShown(r) {
    var min = filter.level === '' ? -1 : Number(filter.level);
    if (min >= 0 && !counted(r).some(function (x) { return x[1] >= min; })) return false;
    return matches([r.name, r.host]);
  }
  function problemShown(r) {
    if (filter.level !== '' && level(r.level) < Number(filter.level)) return false;
    if (filter.group && !rows('host').some(function (x) { return (x.name === r.host || x.host === r.hostTech) && groupsOf(x).indexOf(filter.group) >= 0; })) return false;
    return matches([r.name, r.host, r.hostTech]);
  }
  function groupsOf(r) { return (Array.isArray(r.groups) ? r.groups : []).filter(obj).map(function (g) { return String(g.name); }); }
  function groupNames() {
    var seen = {};
    rows('host').forEach(function (r) { groupsOf(r).forEach(function (g) { seen[g] = 1; }); });
    var sum = rows('summary')[0];
    (sum && Array.isArray(sum.watchedGroups) ? sum.watchedGroups : []).forEach(function (g) { seen[String(g)] = 1; });
    return Object.keys(seen).sort(function (a, b) { return a.localeCompare(b); });
  }
  function table(id, head) {
    var box = h('div', 'box'), t = h('table'), tr = h('tr');
    t.id = id;
    head.forEach(function (x) { tr.appendChild(h('th', x[1] ? 'num' : null, x[0])); });
    add(t, add(h('thead'), tr), h('tbody'));
    box.appendChild(t);
    return box;
  }
  function empty(box, cols, text) {
    var tr = h('tr', 'empty'), td = h('td', null, text);
    td.colSpan = cols;
    box.querySelector('tbody').appendChild(add(tr, td));
  }
  function availability(r) {
    var cell = h('td'), list = (Array.isArray(r.interfaces) ? r.interfaces : []).filter(obj);
    if (!list.length) return add(cell, h('span', 'zero-v', DASH));
    list.forEach(function (i) {
      var x = AVAIL[i.available] || AVAIL.unknown, line = h('div');
      add(line, badge(x[0], x[1]), ' ', h('span', 'ns', KINDS[i.type] || KINDS.other));
      if (i.error) add(line, ' ', raw('span', 'msg', i.error));
      cell.appendChild(line);
    });
    return cell;
  }
  function problemCounts(r) {
    var cell = h('td'), found = counted(r);
    if (!found.length) return add(cell, h('span', 'zero-v', '0'));
    found.forEach(function (x) { add(cell, add(h('span', 'tag ' + TONE[x[1]]), h('span', null, x[0]), ' ', h('span', null, String(x[2]))), ' '); });
    return cell;
  }
  function hostCell(r) {
    var td = h('td');
    add(td, h('span', 'svc', r.name || r.host));
    if (r.host && r.host !== r.name) add(td, raw('span', 'ns', r.host));
    return td;
  }
  function hostRow(r) {
    return add(h('tr'), hostCell(r), availability(r), add(h('td'), r.maintenance ? badge('In maintenance', 'k-warn') : h('span', 'zero-v', DASH)), problemCounts(r));
  }
  function hostsPart(sum) {
    var box = table('zbx-hosts', [['Host'], ['Availability'], ['Maintenance'], ['Open problems']]), body = box.querySelector('tbody');
    var all = rows('host'), shown = all.filter(hostShown), groups = groupNames().filter(function (g) { return !filter.group || g === filter.group; });
    if (!sum) { empty(box, 4, refused() ? 'Not read: no access to hosts and problems.' : 'No Zabbix data yet. The Zabbix check fills this on its next run.'); return box; }
    if ((sum.denied || sum.error || sum.late || sum.broken) && !all.length) { empty(box, 4, sum.denied ? 'Not read: no access to hosts and problems.' : 'Hosts not read on the last check.'); return box; }
    if (!all.length) { empty(box, 4, 'No monitored host in the watched host groups.'); return box; }
    groups.forEach(function (g) {
      var inside = shown.filter(function (r) { return groupsOf(r).indexOf(g) >= 0; });
      if (!inside.length) return;
      var head = h('tr', 'grp'), td = h('td');
      td.colSpan = 4;
      add(td, h('strong', 'svc', g), ' ', h('span', 'ns', String(inside.length)));
      body.appendChild(add(head, td));
      inside.forEach(function (r) { body.appendChild(hostRow(r)); });
    });
    if (!body.children.length) empty(box, 4, 'No host matches the filters.');
    return box;
  }
  function problemRow(r) {
    var lv = level(r.level), links = h('td'), cards = cardsOf(r);
    cards.forEach(function (a) { add(links, cardTag(a), ' '); });
    add(links, lnk(r.link, 'Zabbix'));
    var when = h('td', 'when', dayhm(r.at) || DASH);
    if (r.at) when.title = String(r.at);
    var host = h('td');
    add(host, h('span', 'svc', r.host || r.hostTech || DASH));
    return add(h('tr'), add(h('td'), badge(LEVELS[lv], TONE[lv])), host, add(h('td'), raw('span', 'msg', r.name)), when,
      add(h('td'), r.acknowledged ? badge('Acknowledged', 'k-ok') : h('span', 'zero-v', DASH)), links);
  }
  function problemsPart(sum) {
    var box = table('zbx-problems', [['Severity'], ['Host'], ['Problem'], ['Started'], ['Acknowledged'], ['Links']]), body = box.querySelector('tbody');
    var all = rows('problem'), shown = all.filter(problemShown);
    if (!sum) { empty(box, 6, refused() ? 'Not read: no access to hosts and problems.' : 'No Zabbix data yet. The Zabbix check fills this on its next run.'); return box; }
    if ((sum.denied || sum.error || sum.late || sum.broken) && !all.length) { empty(box, 6, sum.denied ? 'Not read: no access to hosts and problems.' : 'Problems not read on the last check.'); return box; }
    if (!all.length) { empty(box, 6, 'No open problem at or above the lowest severity.'); return box; }
    shown.forEach(function (r) { body.appendChild(problemRow(r)); });
    if (!shown.length) empty(box, 6, 'No problem matches the filters.');
    return box;
  }
  function instances() {
    var seen = {}, out = [];
    summaries().forEach(function (s) { var k = where(s); if (!seen[k]) { seen[k] = 1; out.push([k, String(s.instance || k || 'Zabbix')]); } });
    Object.keys(sources()).forEach(function (key) {
      var k = key === SRC ? '' : key.indexOf(SRC + '-') === 0 ? key.slice(SRC.length + 1) : null;
      if (k === null || seen[k]) return;
      seen[k] = 1;
      out.push([k, k || 'Zabbix']);
    });
    out.sort(function (a, b) { return a[0] === '' ? -1 : b[0] === '' ? 1 : a[1].localeCompare(b[1]); });
    return out;
  }
  function option(value, text, remote) { var o = remote ? raw('option', null, text) : h('option', null, text); o.value = value; return o; }
  function bar() {
    var tools = section.querySelector('.zbx-bar');
    if (tools) return tools;
    tools = h('div', 'toolbar zbx-bar');
    var inst = h('div', 'seg zbx-inst');
    inst.setAttribute('role', 'group');
    inst.setAttribute('aria-label', 'Zabbix instance');
    inst.addEventListener('click', function (e) {
      var b = e.target.closest('button[data-inst]');
      if (!b || b.dataset.inst === pick) return;
      pick = b.dataset.inst;
      filter.group = '';
      render();
    });
    var group = h('select', 'zbx-group'), groupBox = add(h('div', 'seg'), group);
    group.setAttribute('aria-label', 'Host group');
    group.addEventListener('change', function () { filter.group = group.value; paint(); });
    var sev = h('select', 'zbx-sev'), sevBox = add(h('div', 'seg'), sev);
    sev.setAttribute('aria-label', 'Severity');
    sev.addEventListener('change', function () { filter.level = sev.value; paint(); });
    var q = h('input', 'cl-q zbx-q');
    q.type = 'search';
    q.maxLength = 100;
    q.autocomplete = 'off';
    q.spellcheck = false;
    q.placeholder = 'Search hosts and problems';
    q.setAttribute('aria-label', 'Search hosts and problems');
    q.addEventListener('input', function () { filter.q = q.value.trim(); paint(); });
    add(tools, inst, groupBox, sevBox, q);
    section.textContent = '';
    add(section, tools, h('div', 'zbx-body'));
    return tools;
  }
  function same(el, list) {
    var key = JSON.stringify(list);
    if (el.dataset.zbxList === key) return true;
    el.dataset.zbxList = key;
    el.textContent = '';
    return false;
  }
  function fill(select, list, value) {
    if (!same(select, list)) list.forEach(function (x) { select.appendChild(option(x[0], x[1], x[2])); });
    if (select.value !== value) select.value = value;
  }
  function controls() {
    var tools = bar(), inst = tools.querySelector('.zbx-inst'), list = instances();
    inst.hidden = list.length < 2;
    if (!same(inst, list)) {
      list.forEach(function (x) {
        var b = raw('button', null, x[1]);
        b.type = 'button';
        b.dataset.inst = x[0];
        inst.appendChild(b);
      });
    }
    [].forEach.call(inst.querySelectorAll('button[data-inst]'), function (b) { b.setAttribute('aria-pressed', String(b.dataset.inst === pick)); });
    var names = groupNames();
    if (filter.group && names.indexOf(filter.group) < 0) filter.group = '';
    fill(tools.querySelector('.zbx-group'), [['', 'All host groups']].concat(names.map(function (g) { return [g, g, true]; })), filter.group);
    var sum = rows('summary')[0], low = sum ? Math.max(0, LEVELS.indexOf(String(sum.minSeverity))) : 0, levels = [['', 'All severities']];
    if (filter.level !== '' && Number(filter.level) < low) filter.level = '';
    for (var i = LEVELS.length - 1; i >= low; i--) levels.push([String(i), LEVELS[i]]);
    fill(tools.querySelector('.zbx-sev'), levels, filter.level);
  }
  function paint() {
    var body = section.querySelector('.zbx-body');
    if (!body) return;
    var sum = rows('summary')[0], status = source();
    body.textContent = '';
    body.appendChild(note(NOTE));
    if (status && !/^ok( |$)/.test(status)) body.appendChild(said('The last Zabbix check failed:', status.replace(/^error:\s*/, ''), 'failed'));
    if (sum && refused()) body.appendChild(note('Zabbix refused the token on the last check, so these rows are from an earlier check.', 'refused'));
    if (sum && sum.error) body.appendChild(said('Zabbix did not answer the page reads on the last check:', String(sum.error), 'page-error'));
    if (sum && sum.broken) body.appendChild(note('Zabbix sent an answer the page could not read on the last check.', 'page-error'));
    if (sum && sum.late) body.appendChild(note('The page was not read on the last check: the card pass took the time the page reads need.', 'late'));
    if (sum && sum.denied) body.appendChild(note('Not read: no access to hosts and problems.', 'denied'));
    if (sum && sum.rowsAt && rows('host').concat(rows('problem')).length) body.appendChild(said('These rows are from an earlier check:', dayhm(sum.rowsAt), 'stale'));
    if (sum && sum.trimmed) body.appendChild(note('Some rows are left out to keep the page small.', 'trimmed'));
    if (sum && sum.hostsCapped) body.appendChild(note('Only the first ' + sum.maxHosts + ' hosts are shown.', 'hosts-capped'));
    if (sum && sum.problemsCapped) body.appendChild(note('Zabbix has more than ' + sum.maxProblems + ' open problems; only the newest ' + sum.maxProblems + ' are read.', 'problems-capped'));
    if (sum && sum.rowsCapped) body.appendChild(note('Only the ' + sum.maxRows + ' most severe problems are listed.', 'rows-capped'));
    add(body, h('h3', 'sec-t', 'Hosts by host group'), hostsPart(sum), h('h3', 'sec-t', 'Open problems'), problemsPart(sum));
  }
  function render() {
    if (!state || !section) return;
    var list = instances();
    if (pick === null || !list.some(function (x) { return x[0] === pick; })) pick = list.length ? list[0][0] : '';
    controls();
    paint();
  }

  document.addEventListener('skopax:state', function (e) { state = e.detail && obj(e.detail.state) ? e.detail.state : null; render(); });
  document.addEventListener('skopax:page', function (e) {
    if (!e.detail || e.detail.tab !== TAB) return;
    section = e.detail.section;
    render();
  });
})();
