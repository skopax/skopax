var PQC = null, PQC_SEQ = 0, PQC_EDIT = null, PQC_FORM = null, PQC_PATH = '/api/admin/custom-checks', PQC_CONFIRM_MS = 4000;
var PQC_STATES = {ok: ['ok', 'k-ok'], failing: ['failing', 'k-warn'], off: ['off', 'plain']};
var PQC_SEV = {info: 'k-info', warn: 'k-warn', high: 'k-high', crit: 'k-crit'};
var PQC_BASE = {'1d': '1 day ago', '1w': '1 week ago'};
var PQC_OPEN = {}, PQC_GSEQ = 0;
var PQC_PRESETS = null, PQC_PICK = null, PQC_PICK_FORM = null, PQC_PICK_RUN = null, PQC_SOURCE_NAMES = {prometheus: 'Prometheus', grafana: 'Grafana datasource', elasticsearch: 'Elasticsearch'};
var PQC_QUERY = {elasticsearch: 'ES|QL query'};

function pqcEl(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = String(text);
  return e;
}
function pqcBox() { return document.getElementById('pqc'); }
function pqcRO() { return typeof intRO === 'function' && intRO(); }
function pqcCount(n, one, many) { n = n || 0; return n + ' ' + (n === 1 ? one : many); }
function pqcList(x) { return Array.isArray(x) ? x : []; }
function pqcEdit(d) {
  PQC_EDIT = d;
  PQC_FORM = null;
  if (d) { pqcStopRun(); PQC_PICK = null; PQC_PICK_FORM = null; }
  pqcRender();
}
function pqcClear() {
  PQC = null;
  PQC_EDIT = null;
  PQC_FORM = null;
  pqcStopRun();
  PQC_PICK = null;
  PQC_PICK_FORM = null;
  PQC_SEQ++;
  var box = pqcBox();
  if (box) box.textContent = '';
}
async function pqcLoad(said) {
  var box = pqcBox();
  if (!box) return;
  var seq = ++PQC_SEQ;
  try {
    var r = await adminApi('GET', pqcRO() ? '/api/integrations/custom-checks' : PQC_PATH);
    if (seq !== PQC_SEQ) return;
    PQC = r && typeof r === 'object' ? r : {items: []};
    pqcRender(said);
  } catch (e) {
    if (seq !== PQC_SEQ) return;
    PQC = null;
    box.textContent = '';
    box.appendChild(pqcEl('p', 'muted', adminDenied(e) && !pqcRO() ? 'Admins only: log in with the ' + ADMIN_ROLE + ' role.' : 'Custom checks did not load; open the tab again.'));
  }
}
function pqcLimit(name, fallback) { var l = PQC && PQC.limits; return l && typeof l[name] === 'number' ? l[name] : fallback; }
function pqcChoices(name) { var c = PQC && PQC.choices; return c ? pqcList(c[name]) : []; }
function pqcWhere(d) { return String(d.source || '') + ' ' + String(d.instance || '') + (d.source === 'grafana' && d.datasource ? ' \u00b7 ' + String(d.datasource) : ''); }
function pqcRender(said) {
  var box = pqcBox();
  if (!box || !PQC) return;
  box.textContent = '';
  var items = pqcList(PQC.items), most = pqcLimit('checks', 50), head = pqcEl('div', 'pqc-head');
  head.appendChild(pqcEl('h3', '', 'Custom checks'));
  var ro = pqcRO();
  var add = pqcEl('button', 'btn small' + (PQC_EDIT && !ro ? ' sec' : '') + ' pqc-add', '+ Add check');
  add.type = 'button';
  add.disabled = items.length >= most;
  add.addEventListener('click', function () { pqcEdit({}); });
  if (!ro) head.appendChild(add);
  var from = pqcEl('button', 'btn sec small pqc-from-preset', 'Add from preset');
  from.type = 'button';
  from.disabled = items.length >= most;
  from.addEventListener('click', function () { pqcPick(''); });
  if (!ro) head.appendChild(from);
  box.appendChild(head);
  box.appendChild(pqcEl('p', 'muted pqc-sub', 'Cards from your own PromQL or ES|QL query on a Prometheus, Grafana or Elasticsearch instance, checked every ' + Math.round(pqcLimit('periodSeconds', 300) / 60) +
    ' min. ' + items.length + ' of ' + most + ' set up.'));
  var msg = pqcEl('p', 'adm-msg ok', said || '');
  msg.setAttribute('role', 'status');
  box.appendChild(msg);
  if (PQC_EDIT && !ro) {
    PQC_FORM = PQC_FORM || pqcForm(PQC_EDIT);
    box.appendChild(PQC_FORM);
  }
  if (PQC_PICK && !ro) {
    PQC_PICK_FORM = PQC_PICK_FORM || pqcPicker(PQC_PICK);
    box.appendChild(PQC_PICK_FORM);
  }
  if (!items.length) {
    box.appendChild(pqcEl('p', 'muted pqc-empty', ro ? 'No custom checks yet.' : 'No custom checks yet: + Add check sets one up, or Add from preset adds a ready-made set.'));
    return;
  }
  var list = pqcEl('ul', 'pqc-list'), groups = [], byKey = {};
  items.forEach(function (d) {
    if (!d.preset) { list.appendChild(pqcRow(d)); return; }
    var key = String(d.preset) + '|' + JSON.stringify(d.presetValues || {});
    if (!byKey[key]) { byKey[key] = {preset: String(d.preset), values: d.presetValues || {}, items: []}; groups.push(byKey[key]); }
    byKey[key].items.push(d);
  });
  groups.forEach(function (g) { list.appendChild(pqcGroup(g)); });
  box.appendChild(list);
}
async function pqcPick(id) {
  if (!PQC_PRESETS) {
    try {
      var r = await adminApi('GET', PQC_PATH + '/presets');
      PQC_PRESETS = pqcList(r && r.items);
    } catch (e) {
      PQC_PRESETS = null;
      pqcRender((e && e.detail) || 'The presets did not load.');
      return;
    }
  }
  pqcStopRun();
  PQC_EDIT = null;
  PQC_FORM = null;
  PQC_PICK = {id: id};
  PQC_PICK_FORM = null;
  pqcRender();
  var box = pqcBox(), to = box && (id ? box.querySelector('.pqc-preset-form [name]') : box.querySelector('.pqc-picker h4'));
  if (to) to.focus();
}
function pqcStopRun() {
  if (PQC_PICK_RUN) PQC_PICK_RUN.abort();
  PQC_PICK_RUN = null;
}
function pqcPreset(id) { return pqcList(PQC_PRESETS).filter(function (p) { return p && p.id === id; })[0] || null; }
function pqcPicker(pick) {
  var preset = pick.id ? pqcPreset(pick.id) : null, wrap = pqcEl('div', 'pqc-picker');
  wrap.setAttribute('role', 'group');
  wrap.setAttribute('aria-label', preset ? 'Add the preset ' + preset.name : 'Add from preset');
  wrap.addEventListener('keydown', function (e) { if (e.key === 'Escape') { e.preventDefault(); pqcUnpick(); } });
  if (!preset) {
    var h = pqcEl('h4', '', 'Add from preset');
    h.tabIndex = -1;
    wrap.appendChild(h);
    wrap.appendChild(pqcEl('p', 'muted pqc-sub', 'A preset is a ready-made set of custom checks: pick one, fill in where its series are, preview and add. Each check stays editable.'));
    var ul = pqcEl('ul', 'pqc-presets');
    pqcList(PQC_PRESETS).forEach(function (p) {
      var li = pqcEl('li', 'pqc-preset'), top = pqcEl('div', 'pqc-ptop');
      top.appendChild(pqcEl('span', 'pqc-n', p.name));
      top.appendChild(pqcEl('span', 'tag plain pqc-src', PQC_SOURCE_NAMES[p.source] || p.source));
      top.appendChild(pqcEl('span', 'pqc-m', pqcList(p.checks).length + ' checks'));
      var choose = pqcButton('pqc-choose', 'Choose');
      choose.setAttribute('aria-label', 'Choose ' + String(p.name || ''));
      choose.addEventListener('click', function () { pqcPick(p.id); });
      top.appendChild(choose);
      li.appendChild(top);
      li.appendChild(pqcEl('p', 'pqc-what', p.summary));
      li.appendChild(pqcEl('p', 'adm-help', 'Needs: ' + String(p.needs || '')));
      ul.appendChild(li);
    });
    wrap.appendChild(ul);
    var cancel = pqcButton('pqc-cancel', 'Cancel');
    cancel.addEventListener('click', pqcUnpick);
    wrap.appendChild(cancel);
    return wrap;
  }
  var f = pqcEl('form', 'pqc-form pqc-preset-form');
  f.noValidate = true;
  wrap.appendChild(f);
  f.addEventListener('submit', function (e) { e.preventDefault(); });
  var head = pqcEl('div', 'pqc-f wide');
  head.appendChild(pqcEl('h4', '', preset.name));
  head.appendChild(pqcEl('p', 'pqc-what', preset.summary));
  head.appendChild(pqcEl('p', 'adm-help', 'Needs: ' + String(preset.needs || '')));
  f.appendChild(head);
  var none = !pqcInstances(preset.source, '').length;
  pqcList(preset.placeholders).forEach(function (p) {
    var control;
    if (p.kind === 'instance') {
      control = pqcSelect(p.key, pqcInstances(preset.source, ''), '');
      if (none) {
        var hint = pqcField(f, p.label + (p.optional ? ' (optional)' : ''), control, p.help, false);
        hint.appendChild(pqcEl('p', 'adm-msg bad pqc-noinst', 'No ' + (PQC_SOURCE_NAMES[preset.source] || preset.source).replace(' datasource', '') +
          ' instance is set up yet: add one under Integrations first.'));
        return;
      }
    } else {
      control = pqcInput(p.key, p['default'] == null ? '' : p['default'], p.max);
      control.placeholder = String(p.example || '');
      control.spellcheck = false;
    }
    pqcField(f, p.label + (p.optional ? ' (optional)' : ''), control, p.help, p.kind === 'matchers');
  });
  var checks = pqcEl('ul', 'pqc-preset-checks');
  pqcList(preset.checks).forEach(function (c) {
    var li = pqcEl('li', 'pqc-pcheck');
    li.appendChild(pqcEl('span', 'tag ' + (PQC_SEV[c.severity] || 'plain'), c.severity));
    li.appendChild(document.createTextNode(' '));
    li.appendChild(pqcEl('span', 'pqc-n', c.name));
    li.appendChild(pqcEl('div', 'pqc-preview'));
    checks.appendChild(li);
  });
  var list = pqcEl('div', 'pqc-f wide');
  list.appendChild(pqcEl('span', '', pqcList(preset.checks).length + ' checks to add'));
  list.appendChild(checks);
  f.appendChild(list);
  var acts = pqcEl('div', 'pqc-acts'), msg = pqcEl('span', 'adm-msg');
  msg.setAttribute('role', 'status');
  var preview = pqcButton('pqc-preset-preview', 'Preview'), addB = pqcButton('pqc-preset-add', 'Add ' + pqcList(preset.checks).length + ' checks');
  var back = pqcButton('pqc-back', 'Back'), cancelB = pqcButton('pqc-cancel', 'Cancel');
  preview.disabled = addB.disabled = none;
  preview.addEventListener('click', function () { pqcPresetPreview(f, preset, preview, msg); });
  addB.addEventListener('click', function () { pqcPresetAdd(f, preset, addB, msg); });
  back.addEventListener('click', function () { pqcPick(''); });
  cancelB.addEventListener('click', pqcUnpick);
  [preview, addB, back, cancelB, msg].forEach(function (b) { acts.appendChild(b); });
  f.appendChild(acts);
  return wrap;
}
function pqcUnpick() {
  pqcStopRun();
  PQC_PICK = null;
  PQC_PICK_FORM = null;
  pqcRender();
  var from = pqcBox() && pqcBox().querySelector('.pqc-from-preset');
  if (from) from.focus();
}
function pqcPresetValues(f, preset) {
  var out = {};
  pqcList(preset.placeholders).forEach(function (p) { var e = f.querySelector('[name="' + p.key + '"]'); out[p.key] = e ? e.value.trim() : ''; });
  return {values: out};
}
async function pqcPresetPreview(f, preset, b, msg) {
  b.disabled = true;
  msg.className = 'adm-msg';
  msg.textContent = 'Filling in the checks\u2026';
  var rows = f.querySelectorAll('.pqc-preset-checks > li');
  pqcStopRun();
  var run = PQC_PICK_RUN = new AbortController();
  try {
    var r = await adminApi('POST', PQC_PATH + '/presets/' + encodeURIComponent(preset.id) + '/render', pqcPresetValues(f, preset), run.signal);
    if (run.signal.aborted) return;
    var made = pqcList(r && r.checks);
    msg.textContent = 'Running ' + made.length + ' queries, one check at a time\u2026';
    for (var i = 0; i < made.length && i < rows.length; i++) {
      if (run.signal.aborted) return;
      rows[i].querySelector('.pqc-n').textContent = String(made[i].name || '');
      var out = rows[i].querySelector('.pqc-preview');
      out.textContent = '';
      out.appendChild(pqcEl('p', 'muted pqc-sum', 'Running the query\u2026'));
      try {
        var shown = await adminApi('POST', PQC_PATH + '/preview', made[i], run.signal);
        if (run.signal.aborted) return;
        pqcShow(out, shown);
      } catch (e) {
        if (run.signal.aborted) return;
        out.textContent = '';
        out.appendChild(pqcEl('p', 'adm-msg bad pqc-sum', (e && e.detail) || 'The preview did not run.'));
      }
    }
    msg.textContent = 'Previewed ' + made.length + ' checks. Nothing is saved.';
  } catch (e) {
    if (run.signal.aborted) return;
    msg.className = 'adm-msg bad';
    msg.textContent = (e && e.detail) || 'The preview did not run.';
  } finally {
    if (PQC_PICK_RUN === run) PQC_PICK_RUN = null;
    b.disabled = false;
  }
}
async function pqcPresetAdd(f, preset, b, msg) {
  b.disabled = true;
  msg.className = 'adm-msg';
  msg.textContent = 'Adding\u2026';
  try {
    var r = await adminApi('POST', PQC_PATH + '/presets/' + encodeURIComponent(preset.id), pqcPresetValues(f, preset));
    PQC_PICK = null;
    PQC_PICK_FORM = null;
    await pqcLoad('Added ' + pqcList(r && r.items).length + ' checks from ' + preset.name);
  } catch (e) {
    msg.className = 'adm-msg bad';
    msg.textContent = (e && e.detail) || 'Not added.';
    b.disabled = false;
  }
}
function pqcRowState(d) {
  var run = d.run && typeof d.run === 'object' ? d.run : null;
  return !d.enabled ? 'off' : run ? String(run.state || '') : '';
}
function pqcCards(d) {
  var run = d.run && typeof d.run === 'object' ? d.run : null;
  return d.enabled && run && run.state === 'ok' ? Number(run.cards) || 0 : 0;
}
function pqcSummary(items) {
  var n = {failing: 0, fresh: 0, off: 0}, cards = 0;
  items.forEach(function (d) {
    var st = pqcRowState(d);
    if (st === 'failing') n.failing++;
    else if (st === 'off') n.off++;
    else if (st !== 'ok') n.fresh++;
    cards += pqcCards(d);
  });
  var parts = [];
  if (n.failing) parts.push(n.failing + ' failing');
  if (n.fresh) parts.push(n.fresh + ' not run yet');
  if (n.off) parts.push(n.off + ' off');
  return {text: parts.length ? parts.join(' \u00b7 ') : 'all ok', cards: cards, failing: n.failing > 0, bad: n.failing > 0 || cards > 0};
}
function pqcWords(name) { return String(name || '').split(/\s+/).filter(Boolean); }
function pqcPrefix(names, label) {
  var lists = names.map(pqcWords), most = Math.min.apply(null, lists.map(function (w) { return w.length; })) - 1;
  if (!(most >= 2)) return 0;
  var head = pqcWords(label), n = head.length;
  if (n >= 2 && n <= most && lists.every(function (w) { return w.slice(0, n).join(' ') === head.join(' '); })) return n;
  if (names.length < 2) return 0;
  var k = 0;
  while (k < most && lists.every(function (w) { return w[k] === lists[0][k]; })) k++;
  return k >= 2 ? k : 0;
}
function pqcGroup(g) {
  var known = pqcList(PQC.presets).filter(function (x) { return x && x.id === g.preset; })[0];
  var chip = String(known ? known.name : g.preset), gname = known && g.values[known.nameKey] != null ? String(g.values[known.nameKey]) : '';
  var key = g.preset + '|' + JSON.stringify(g.values), sum = pqcSummary(g.items), id = 'pqc-g' + (++PQC_GSEQ);
  var open = PQC_OPEN.hasOwnProperty(key) ? PQC_OPEN[key] : sum.bad;
  var li = pqcEl('li', 'pqc-group'), btn = pqcEl('button', 'pqc-ghead');
  btn.type = 'button';
  btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  btn.setAttribute('aria-controls', id);
  btn.appendChild(pqcEl('span', 'pqc-caret')).setAttribute('aria-hidden', 'true');
  btn.appendChild(pqcEl('span', 'tag pqc-chip', chip));
  btn.appendChild(pqcEl('span', 'pqc-gname', gname));
  btn.appendChild(pqcEl('span', 'pqc-m pqc-gcount', pqcCount(g.items.length, 'check', 'checks')));
  btn.appendChild(pqcEl('span', 'pqc-gsum' + (sum.failing ? ' bad' : ''), sum.text));
  if (sum.cards > 0) btn.appendChild(pqcEl('span', 'pqc-gcards', pqcCount(sum.cards, 'card', 'cards')));
  var rows = pqcEl('ul', 'pqc-grows'), skip = pqcPrefix(g.items.map(function (d) { return d.name; }), chip + ' ' + gname);
  rows.id = id;
  rows.hidden = !open;
  g.items.forEach(function (d) { rows.appendChild(pqcRow(d, skip)); });
  btn.addEventListener('click', function () {
    var now = btn.getAttribute('aria-expanded') !== 'true';
    PQC_OPEN[key] = now;
    btn.setAttribute('aria-expanded', now ? 'true' : 'false');
    rows.hidden = !now;
  });
  li.appendChild(btn);
  li.appendChild(rows);
  return li;
}
function pqcRow(d, skip) {
  var li = pqcEl('li', 'pqc-row'), run = d.run && typeof d.run === 'object' ? d.run : null;
  li.dataset.id = String(d.id || '');
  var full = String(d.name == null ? '' : d.name), short = skip ? pqcWords(full).slice(skip).join(' ') : full, nm = pqcEl('span', 'pqc-n', short || full);
  if (short && short !== full) {
    nm.title = full;
    li.appendChild(pqcEl('span', 'pqc-sr', pqcWords(full).slice(0, skip).join(' ') + ' '));
  }
  li.appendChild(nm);
  var state = !d.enabled ? 'off' : run ? run.state : '', kind = PQC_STATES[state] || ['not run yet', 'plain'];
  var tag = pqcEl('span', 'tag pqc-state ' + kind[1], kind[0] + (state === 'failing' && run.reason ? ': ' + run.reason : ''));
  li.appendChild(tag);
  var meta = pqcWhere(d) + ' \u00b7 ' + String(d.op || '') + ' ' + String(d.threshold) + ' \u00b7 ' + String(d.severity || '');
  if (run && state === 'ok') meta += ' \u00b7 ' + (run.series || 0) + ' series, ' + pqcCount(run.cards, 'card', 'cards') + (run.noBaseline ? ', ' + pqcCount(run.noBaseline, 'group', 'groups') + ' had no baseline value' : '');
  li.appendChild(pqcEl('span', 'pqc-m', meta));
  if (pqcRO()) return li;
  var edit = pqcEl('button', 'btn sec small pqc-edit', 'Edit');
  edit.type = 'button';
  edit.setAttribute('aria-label', 'Edit ' + full);
  edit.addEventListener('click', function () { pqcEdit(d); var f = pqcBox().querySelector('.pqc-form [name="name"]'); if (f) f.focus(); });
  li.appendChild(edit);
  return li;
}
function pqcField(f, label, control, help, wide) {
  var wrap = pqcEl('label', 'pqc-f' + (wide ? ' wide' : ''));
  wrap.appendChild(pqcEl('span', '', label));
  wrap.appendChild(control);
  if (help) wrap.appendChild(pqcEl('p', 'adm-help', help));
  f.appendChild(wrap);
  return wrap;
}
function pqcInput(name, value, max) {
  var i = pqcEl('input');
  i.type = 'text';
  i.name = name;
  i.value = value == null ? '' : String(value);
  if (max) i.maxLength = max;
  return i;
}
function pqcSelect(name, options, value) {
  var s = pqcEl('select');
  s.name = name;
  options.forEach(function (o) {
    var opt = pqcEl('option', '', o[1]);
    opt.value = o[0];
    s.appendChild(opt);
  });
  s.value = value;
  if (s.selectedIndex < 0 && s.options.length) s.selectedIndex = 0;
  return s;
}
function pqcPairs(list) { return list.map(function (x) { return [x, x]; }); }
function pqcInstances(source, value) {
  var names = PQC && PQC.sources ? pqcList(PQC.sources[source]) : [];
  if (value && names.indexOf(value) < 0) names = names.concat([value]);
  return names.map(function (n) { return [n, n]; });
}
function pqcForm(d) {
  var fresh = !d.id, f = pqcEl('form', 'pqc-form');
  f.noValidate = true;
  f.setAttribute('aria-label', fresh ? 'New custom check' : 'Edit custom check ' + String(d.name || ''));
  f.addEventListener('submit', function (e) { e.preventDefault(); });
  var source = d.source || 'prometheus';
  pqcField(f, 'Name', pqcInput('name', d.name, pqcLimit('name', 80)), 'Shown on every card of this check.');
  if (fresh) pqcField(f, 'Id', pqcInput('id', '', 41), 'Optional: lowercase letters, digits and dashes; made from the name when empty. It never changes, so open cards survive a rename.');
  pqcField(f, 'State', pqcSelect('enabled', [['on', 'On'], ['off', 'Off: clears its cards']], d.enabled === false ? 'off' : 'on'));
  var src = pqcSelect('source', pqcChoices('source').map(function (s) { return [s, PQC_SOURCE_NAMES[s] || s]; }), source);
  pqcField(f, 'Source', src);
  var inst = pqcSelect('instance', pqcInstances(source, d.instance), d.instance || '');
  pqcField(f, 'Instance', inst, 'The Prometheus, Grafana or Elasticsearch instance set up under Integrations.');
  var ds = pqcField(f, 'Datasource', pqcInput('datasource', d.datasource, 200), 'The name or uid of the Grafana Prometheus datasource. A uid keeps working after a rename.');
  ds.hidden = source !== 'grafana';
  var q = pqcEl('textarea', 'pqc-q');
  q.name = 'query';
  q.rows = 3;
  q.maxLength = pqcLimit('query', 2000);
  q.value = d.query || '';
  q.spellcheck = false;
  var qf = pqcField(f, 'PromQL query', q, ' ', true);
  var win = pqcInput('window', d.window == null ? pqcLimit('window', 60) : d.window, 4);
  win.type = 'number';
  win.min = String(pqcLimit('windowMin', 5));
  win.max = String(pqcLimit('windowMax', 1440));
  var wf = pqcField(f, 'Window (minutes)', win, 'The time the query reads: ?start is this many minutes before the pass and ?end the pass time, both moved back for a baseline.');
  var shape = function (to) {
    var es = to === 'elasticsearch';
    ds.hidden = to !== 'grafana';
    wf.hidden = !es;
    qf.firstChild.textContent = PQC_QUERY[to] || 'PromQL query';
    qf.lastChild.textContent = es ? 'Starts with FROM and returns a number column named value, such as FROM logs-* | WHERE @timestamp >= ?start AND @timestamp < ?end | STATS value = COUNT(*) BY service = service.name. At most ' +
      pqcLimit('rows', 1000) + ' rows are read and each pass has ' + pqcLimit('querySeconds', 20) + ' s for its queries.' :
      'An instant query. At most ' + pqcLimit('series', 500) + ' series are read and each pass has ' + pqcLimit('querySeconds', 20) + ' s for its queries.';
  };
  shape(source);
  src.addEventListener('change', function () {
    shape(src.value);
    var keep = inst.value;
    inst.textContent = '';
    pqcInstances(src.value, '').forEach(function (o) { var opt = pqcEl('option', '', o[1]); opt.value = o[0]; inst.appendChild(opt); });
    inst.value = keep;
    if (inst.selectedIndex < 0 && inst.options.length) inst.selectedIndex = 0;
  });
  pqcField(f, 'Comparison', pqcSelect('op', pqcPairs(pqcChoices('op')), d.op || '>'));
  var th = pqcInput('threshold', d.threshold == null ? '' : d.threshold, 40);
  th.inputMode = 'decimal';
  pqcField(f, 'Threshold', th);
  var ticks = pqcEl('span', 'pqc-ticks');
  pqcChoices('baselines').forEach(function (b) {
    var l = pqcEl('label'), c = pqcEl('input');
    c.type = 'checkbox';
    c.name = 'baseline-' + b;
    c.checked = pqcList(d.baselines).indexOf(b) >= 0;
    l.appendChild(c);
    l.appendChild(document.createTextNode(' ' + (PQC_BASE[b] || b)));
    ticks.appendChild(l);
  });
  pqcField(f, 'Compare with', ticks, 'The same query at the same time 1 day or 1 week earlier; with both, it fires only against both.');
  pqcField(f, 'Baseline as', pqcSelect('baselineMode', pqcChoices('baselineMode').map(function (m) { return [m, m === 'ratio' ? 'Ratio (now / then)' : 'Difference (now - then)']; }), d.baselineMode || 'ratio'));
  pqcField(f, 'Severity', pqcSelect('severity', pqcPairs(pqcChoices('severity')), d.severity || 'warn'));
  pqcField(f, 'Split by labels', pqcInput('by', pqcList(d.by).join(', '), 200), 'One card per value set of these labels (for ES|QL, columns), at most ' + pqcLimit('cards', 20) + ' cards; the rest on one card.');
  var n = pqcInput('for', d['for'] || 1, 3);
  n.type = 'number';
  n.min = '1';
  n.max = String(pqcLimit('for', 12));
  pqcField(f, 'For (passes)', n, 'How many passes in a row it must match before a card opens.');
  pqcField(f, 'Card title', pqcInput('title', d.title, pqcLimit('title', 200)), 'Plain text with {label} and {value} placeholders, such as {instance} at {value} %.', true);
  var t = pqcEl('textarea');
  t.name = 'text';
  t.rows = 2;
  t.maxLength = pqcLimit('text', 1000);
  t.value = d.text || '';
  pqcField(f, 'Card text', t, 'Plain text with the same placeholders; the comparison is added after it.', true);
  var acts = pqcEl('div', 'pqc-acts'), msg = pqcEl('span', 'adm-msg'), out = pqcEl('div', 'pqc-preview');
  msg.setAttribute('role', 'status');
  out.setAttribute('aria-live', 'polite');
  var preview = pqcButton('pqc-preview-btn', 'Preview'), saveB = pqcButton('pqc-save', 'Save'), cancel = pqcButton('pqc-cancel', 'Cancel');
  preview.addEventListener('click', function () { pqcPreview(f, preview, out); });
  saveB.addEventListener('click', function () { pqcSave(f, d, saveB, msg); });
  cancel.addEventListener('click', function () { pqcEdit(null); });
  [preview, saveB, cancel].forEach(function (b) { acts.appendChild(b); });
  if (!fresh) {
    var del = pqcButton('pqc-delete', 'Delete');
    del.addEventListener('click', function () { pqcDelete(d, del, msg); });
    acts.appendChild(del);
  }
  acts.appendChild(msg);
  f.appendChild(acts);
  f.appendChild(out);
  return f;
}
var PQC_MAIN = ['pqc-save', 'pqc-preset-add'];
function pqcButton(cls, text) {
  var b = pqcEl('button', 'btn ' + (PQC_MAIN.indexOf(cls) >= 0 ? '' : 'sec ') + 'small ' + cls, text);
  b.type = 'button';
  return b;
}
function pqcValue(f) {
  var v = function (name) { var e = f.querySelector('[name="' + name + '"]'); return e ? e.value : ''; };
  var body = {name: v('name').trim(), enabled: v('enabled') !== 'off', source: v('source'), instance: v('instance'), datasource: v('datasource').trim(), query: v('query').trim(), op: v('op'),
    threshold: v('threshold').trim(), baselines: pqcChoices('baselines').filter(function (b) { var c = f.querySelector('[name="baseline-' + b + '"]'); return c && c.checked; }),
    baselineMode: v('baselineMode'), severity: v('severity'), by: v('by').split(',').map(function (x) { return x.trim(); }).filter(Boolean), 'for': Number(v('for')) || 1,
    title: v('title').trim(), text: v('text').trim()};
  if (f.querySelector('[name="id"]') && v('id').trim()) body.id = v('id').trim();
  if (body.source === 'elasticsearch') body.window = v('window').trim();
  return body;
}
function pqcSafe(url) { return typeof url === 'string' && /^https:\/\/[^\s]+$/.test(url); }
function pqcShow(out, r) {
  out.textContent = '';
  if (!r || !r.ok) {
    out.appendChild(pqcEl('p', 'adm-msg bad pqc-sum', 'The check would fail: ' + String(r && r.reason || 'no answer')));
    return;
  }
  var cards = pqcList(r.cards);
  out.appendChild(pqcEl('p', 'muted pqc-sum', (r.series || 0) + ' series, ' + (r.firing || 0) + ' match, ' + pqcCount(cards.length, 'card', 'cards') + (r.skipped ? ', ' + r.skipped + ' not a number' : '') +
    (r.read < r.series ? ', ' + r.read + ' read' : '') + (r.noBaseline ? ', ' + pqcCount(r.noBaseline, 'group', 'groups') + ' had no baseline value' : '') + ((r['for'] || 1) > 1 ? '; a card opens only after ' + r['for'] + ' passes in a row' : '') + '. Nothing is saved.'));
  pqcList(r.warnings).forEach(function (x) { out.appendChild(pqcEl('p', 'adm-msg pqc-warn', 'Note: ' + String(x))); });
  var ul = pqcEl('ul', 'pqc-cards');
  cards.forEach(function (c) {
    var li = pqcEl('li', 'pqc-card');
    li.appendChild(pqcEl('span', 'tag ' + (PQC_SEV[c.sev] || 'plain'), c.sev));
    li.appendChild(document.createTextNode(' '));
    li.appendChild(pqcEl('span', 'pqc-t', c.title));
    li.appendChild(pqcEl('p', '', c.text));
    pqcList(c.links).forEach(function (l) {
      if (!l || !pqcSafe(l.url)) return;
      var a = pqcEl('a', '', l.label || l.url);
      a.href = l.url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      li.appendChild(a);
    });
    ul.appendChild(li);
  });
  out.appendChild(ul);
}
async function pqcPreview(f, b, out) {
  b.disabled = true;
  out.textContent = '';
  out.appendChild(pqcEl('p', 'muted pqc-sum', 'Running the query\u2026'));
  try {
    pqcShow(out, await adminApi('POST', PQC_PATH + '/preview', pqcValue(f)));
  } catch (e) {
    out.textContent = '';
    out.appendChild(pqcEl('p', 'adm-msg bad pqc-sum', (e && e.detail) || 'The preview did not run.'));
  } finally {
    b.disabled = false;
  }
}
async function pqcSave(f, d, b, msg) {
  b.disabled = true;
  msg.className = 'adm-msg';
  msg.textContent = 'Saving\u2026';
  try {
    if (d.id) await adminApi('PUT', PQC_PATH + '/' + encodeURIComponent(d.id), pqcValue(f));
    else await adminApi('POST', PQC_PATH, pqcValue(f));
    PQC_EDIT = null;
    PQC_FORM = null;
    await pqcLoad('Saved');
  } catch (e) {
    msg.className = 'adm-msg bad';
    msg.textContent = (e && e.detail) || 'Not saved.';
    b.disabled = false;
  }
}
async function pqcDelete(d, b, msg) {
  if (!b.dataset.confirm) {
    b.dataset.confirm = '1';
    b.textContent = 'Delete for good?';
    setTimeout(function () { delete b.dataset.confirm; b.textContent = 'Delete'; }, PQC_CONFIRM_MS);
    return;
  }
  b.disabled = true;
  try {
    await adminApi('DELETE', PQC_PATH + '/' + encodeURIComponent(d.id));
    PQC_EDIT = null;
    PQC_FORM = null;
    await pqcLoad('Deleted; its cards are cleared');
  } catch (e) {
    msg.className = 'adm-msg bad';
    msg.textContent = (e && e.detail) || 'Not deleted.';
    b.disabled = false;
  }
}
