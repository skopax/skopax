var AIU_HEADS = ['Use', 'Model', 'Effort', 'Cost', 'Data goes to'];
var AIU_FIT = 'Cheap models often miss steps in long investigations', AIU_FIT_USES = ['card', 'critical', 'high'];
var AIU_ADV_SAID = {}, AIU_ADV_BUSY = {}, AIU_ADV_FIELDS = {uses: {suffix: '-advisor-uses', label: 'Advisor consultations per run', min: 1, max: 10}, tokens: {suffix: '-advisor-tokens', label: 'Advisor answer size', min: 1024, max: 4096}};
var AIU_SAID = {}, AIU_SEQ = 0, AIU_EDIT = {}, AIU_PENDING = {}, AIU_CHAIN = Promise.resolve(), AIU_FAILED = false;
var AIC_TESTING = {}, AIC_SEQ = 0, AIC_PRIVATE_KEY = 'core.allow-private-ai';
var AIV_TESTING = {}, AIV_MODES = ['key', 'federation', 'pod'];
var AIV_REGION_TEXT = {global: 'global (current models, no residency guarantee)', us: 'us (current models, processing stays in the US)', eu: 'eu (current models, processing stays in the EU)'};
var AIV_REGION_NOTE = 'Current Claude models run on global, us and eu only; a single region serves older models. Choose eu to keep processing in the EU.';
var AIC_NOT_FIT = 'Not fit for investigations: the model did not call the tool';
var AIU_PROVIDERS = {anthropic: 'Anthropic', openai: 'OpenAI', vertex: 'Vertex AI'};
var AIU_GUARD = 'guard', AIU_GUARD_KEYS = ['guard.check-questions', 'guard.check-answers', 'guard.mode', 'guard.on-hold'];
var AIU_GUARD_WATCH = 'Watch only: the guard records what it would have done but never refuses, hides or holds anything, so it protects nothing. Switch back to Enforce when you have seen enough.';
var AIU_GUARD_SEES = 'The guard model reads questions, answers and the tool results behind them. If it runs on another provider, that provider receives this text.';
var AIU_GUARD_NEEDS = 'Pick a guard model first; the check stays off until one is set';
var AIU_GUARD_NO_MODEL = 'A guard check is on but no guard model is set, so the check is off; pick a guard model';
function aiuText(v) { return typeof v === 'string' ? v : ''; }
function aiuNum(v) { return typeof v === 'number' && isFinite(v) ? v : null; }
function aiuAdvOf(a) {
  if (!a || typeof a !== 'object') return null;
  var uses = aiuNum(a.maxUses), tokens = aiuNum(a.maxTokens);
  return {model: aiuText(a.model), choices: (Array.isArray(a.choices) ? a.choices : []).filter(function (c) { return typeof c === 'string' && c; }), maxUses: uses == null ? 3 : uses, maxTokens: tokens == null ? 2000 : tokens,
    capable: a.capable === true, problem: aiuText(a.problem) || null};
}
function aiuOf(r) {
  return (r && Array.isArray(r.uses) ? r.uses : []).filter(function (u) { return u && typeof u.use === 'string' && u.use && typeof u.setting === 'string'; }).map(function (u) {
    var p = u.price && typeof u.price === 'object' ? {input: aiuNum(u.price.input), output: aiuNum(u.price.output)} : null;
    return {use: u.use, label: aiuText(u.label) || u.use, setting: u.setting, model: aiuText(u.model), provider: aiuText(u.provider), company: aiuText(u.company), price: p,
      avgCost7d: aiuNum(u.avgCost7d), problem: aiuText(u.problem) || null, fit: u.fit === 'cheap' ? 'cheap' : 'ok', advisor: aiuAdvOf(u.advisor)};
  });
}
function aicLast(t) {
  return t && typeof t === 'object' ? {ok: t.ok === true, tools: t.tools !== false, summary: aiuText(t.summary), model: aiuText(t.model), at: aiuText(t.at), models: aivTested(t.models)} : null;
}
function aivTested(list) {
  return (Array.isArray(list) ? list : []).filter(function (m) { return m && typeof m.id === 'string' && m.id; }).map(function (m) { return {id: m.id, ok: m.ok === true, summary: aiuText(m.summary)}; });
}
function aicConn(c) {
  return {name: c.name, baseUrl: aiuText(c.baseUrl), keySet: !!(c.key && c.key.set === true), tools: c.tools !== false, images: c.images === true, company: aiuText(c.company),
    models: (Array.isArray(c.models) ? c.models : []).filter(function (m) { return m && typeof m.id === 'string'; }).map(function (m) { return {id: m.id, ref: aiuText(m.ref), input: aiuNum(m.input), output: aiuNum(m.output)}; }),
    lastTest: aicLast(c.lastTest)};
}
function aicOf(r) {
  if (r && r.failed) return {failed: true, code: r.code};
  if (!r || !Array.isArray(r.items)) return null;
  return {allowPrivate: r.allowPrivate === true, connections: r.items.filter(function (c) { return c && typeof c.name === 'string' && c.name; }).map(aicConn)};
}
function aiuCovered() {
  var out = {};
  (AIUSES || []).forEach(function (u) {
    out[u.setting] = 1;
    if (AI_PAIRS[u.setting]) out[AI_PAIRS[u.setting][0]] = 1;
    if (u.advisor) ['-advisor', '-advisor-uses', '-advisor-tokens'].forEach(function (x) { out[u.setting + x] = 1; });
    if (u.use === AIU_GUARD) AIU_GUARD_KEYS.forEach(function (k) { out[k] = 1; });
  });
  return out;
}
function aiuSummary() {
  var by = {};
  (AIUSES || []).forEach(function (u) { by[u.use] = u; });
  if (!by.card || !by.critical || !by.high) return null;
  return 'Which model runs: Critical cards ' + by.critical.model + ' \u00b7 High cards ' + by.high.model + ' \u00b7 Warning and Info cards ' + by.card.model;
}
function aiuItem(key) { return adminItems().filter(function (i) { return i.key === key; })[0] || null; }
function aiuUsd(v) {
  if (v == null) return '\u2013';
  var s = v.toFixed(2);
  if (Number(s) !== v) s = String(Number(v.toFixed(4)));
  return '$' + s;
}
function aiuId(use) { return 'aiu-' + String(use).replace(/[^a-z0-9]/gi, '-'); }
function aiuKey(n, id) {
  var k = accText('span', 'aiu-k', AIU_HEADS[n]);
  if (id) k.id = id;
  return k;
}
function aiuProvider(p) {
  if (AIU_PROVIDERS[p]) return AIU_PROVIDERS[p];
  if (p.indexOf('vertex:') === 0) return AIU_PROVIDERS.vertex;
  return p.indexOf('compat:') === 0 ? p.slice(7) : p;
}
function aiuSection() {
  var s = el('section', 'adm-grp ai-grp aiu');
  s.dataset.group = 'Who does what';
  var h = accText('h3', null, 'Who does what');
  h.id = 'aiu-h';
  s.appendChild(h);
  s.appendChild(accText('p', 'adm-help adm-model-summary', cardModelSummary()));
  var box = el('div', 'aiu-box'), table = el('table', 'aiu-t'), head = document.createElement('thead'), hr = document.createElement('tr'), body = document.createElement('tbody');
  table.setAttribute('aria-labelledby', h.id);
  AIU_HEADS.forEach(function (t) {
    var th = accText('th', null, t);
    th.setAttribute('scope', 'col');
    hr.appendChild(th);
  });
  head.appendChild(hr);
  table.appendChild(head);
  AIUSES.forEach(function (u) { body.appendChild(aiuRow(u)); });
  table.appendChild(body);
  box.appendChild(table);
  s.appendChild(box);
  return s;
}
function aiuFailed() { return accText('p', 'adm-msg bad aiu-failed', 'The uses table did not load; open the tab again.'); }
function aiuCell(cls, n, value) {
  var td = accText('td', cls);
  td.appendChild(aiuKey(n));
  if (value != null) td.appendChild(accText('span', 'aiu-v', value));
  return td;
}
function aiuHost(value) {
  var span = accText('span', 'aiu-v');
  String(value).split('.').forEach(function (part, n, all) {
    span.appendChild(document.createTextNode(n < all.length - 1 ? part + '.' : part));
    if (n < all.length - 1) span.appendChild(document.createElement('wbr'));
  });
  return span;
}
function aiuNote(into, cls, text, id, notes) {
  var p = accText('p', cls, text);
  p.id = id;
  into.appendChild(p);
  notes.push(id);
}
function aiuOff(u) { return u.use === AIU_GUARD && !u.model; }
function aiuGuardModel() { var i = aiuItem('investigate.guard-model'); return i ? aiuSaved(i) : ''; }
function aiuSwitches() {
  var model = aiuGuardModel();
  return AIU_GUARD_KEYS.map(aiuItem).filter(Boolean).map(function (i) {
    var copy = Object.assign({}, i);
    delete copy.off;
    delete copy.problem;
    if (!model && i.value === 'true') copy.problem = AIU_GUARD_NO_MODEL;
    else if (!model) copy.off = AIU_GUARD_NEEDS;
    return copy;
  });
}
function aiuGuard(id) {
  var box = el('div', 'aiu-guard');
  box.setAttribute('role', 'group');
  box.setAttribute('aria-labelledby', id + '-l');
  aiuSwitches().forEach(function (i) { box.appendChild(adminRow(i)); });
  var mode = aiuItem('guard.mode');
  if (mode && aiuSaved(mode) === 'watch') box.appendChild(accText('p', 'adm-help adm-problem aiu-guard-watch', AIU_GUARD_WATCH));
  return box;
}
function aiuRow(u) {
  var guarded = u.use === AIU_GUARD && aiuSwitches().some(function (i) { return i.problem; });
  var id = aiuId(u.use), item = aiuItem(u.setting), tr = el('tr', 'aiu-row' + (u.problem || (u.advisor && u.advisor.problem) || guarded ? ' aiu-bad' : '') + (item && item.source === 'admin' ? ' set' : '')), notes = [];
  tr.dataset.use = u.use;
  if (item) tr.dataset.key = item.key;
  var th = accText('th', 'aiu-use');
  th.setAttribute('scope', 'row');
  th.appendChild(aiuKey(0));
  var name = accText('b', null, u.label);
  name.id = id + '-l';
  th.appendChild(name);
  if (u.provider && !aiuOff(u)) th.appendChild(accText('span', 'tag plain aiu-prov', aiuProvider(u.provider)));
  tr.appendChild(th);
  var model = accText('td', 'aiu-model');
  model.appendChild(aiuKey(1, id + '-k'));
  var msg = accText('span', 'adm-msg aiu-msg');
  msg.setAttribute('role', 'status');
  if (item && item.kind === 'choice') aiuControls(model, u, item, id, notes, msg);
  else { model.appendChild(accText('span', 'aiu-v', u.model)); model.appendChild(msg); }
  if (item && item.problem && item.problem !== u.problem) aiuNote(model, 'adm-help adm-problem', item.problem, id + '-iproblem', notes);
  if (u.problem) aiuNote(model, 'adm-help aiu-problem bad', u.problem, id + '-problem', notes);
  if (u.fit === 'cheap' && AIU_FIT_USES.indexOf(u.use) >= 0) aiuNote(model, 'adm-help aiu-fit', AIU_FIT, id + '-fit', notes);
  if (item && item.off) aiuNote(model, 'adm-help adm-off-why', item.off, id + '-off', notes);
  if (u.use === AIU_GUARD) aiuNote(model, 'adm-help aiu-guard-sees', AIU_GUARD_SEES, id + '-sees', notes);
  var sel = model.querySelector('select.aiu-in');
  if (u.advisor) model.appendChild(aiuAdvisor(u, id));
  if (u.use === AIU_GUARD) model.appendChild(aiuGuard(id));
  if (sel && notes.length) sel.setAttribute('aria-describedby', notes.join(' '));
  var said = AIU_SAID[u.use];
  if (said) { msg.textContent = said.text; msg.className = 'adm-msg aiu-msg ' + (said.bad ? 'bad' : 'ok'); delete AIU_SAID[u.use]; }
  if (AIU_PENDING[u.use] != null) { msg.textContent = 'Saving\u2026'; msg.className = 'adm-msg aiu-msg'; }
  tr.appendChild(model);
  var pair = AI_PAIRS[u.setting], effort = pair ? aiuItem(pair[0]) : null, eff = aiuCell('aiu-effort', 2, effort ? null : '\u2013');
  if (effort) eff.appendChild(adminRow(effort));
  tr.appendChild(eff);
  tr.appendChild(aiuCostCell(u));
  var co = aiuCell('aiu-co', 4, aiuOff(u) ? 'Nothing is sent' : null);
  if (!aiuOff(u)) co.appendChild(aiuHost(u.company));
  tr.appendChild(co);
  return tr;
}
function aiuLine(cls, amount, unit) {
  var line = accText('span', 'aiu-v ' + cls);
  line.appendChild(accText('span', 'aiu-n', amount));
  if (unit) {
    line.appendChild(document.createTextNode(' '));
    line.appendChild(accText('span', 'aiu-unit', unit));
  }
  return line;
}
function aiuCostCell(u) {
  var td = accText('td', 'aiu-cost');
  td.appendChild(aiuKey(3));
  if (aiuOff(u)) { td.appendChild(aiuLine('aiu-price', 'Guard off')); return td; }
  td.appendChild(u.price ? aiuLine('aiu-price', aiuUsd(u.price.input) + ' / ' + aiuUsd(u.price.output), 'per 1M tokens') : aiuLine('aiu-price', 'No price'));
  td.appendChild(u.avgCost7d == null ? aiuLine('aiu-run', 'No runs in 7 days') : aiuLine('aiu-run', costUsd(u.avgCost7d), 'per run, 7 days'));
  return td;
}
function aiuAdvOption(value, text) {
  var o = accText('option', null, text);
  o.value = value;
  return o;
}
function aiuAdvisor(u, id) {
  var a = u.advisor, box = el('div', 'aiu-adv'), notes = [], k = accText('span', 'aiu-adv-k', 'Advisor'), sel = el('select', 'aiu-adv-in'), msg = accText('span', 'adm-msg aiu-adv-msg');
  box.dataset.use = u.use;
  k.id = id + '-adv-k';
  sel.id = id + '-adv';
  sel.setAttribute('aria-labelledby', id + '-l ' + k.id);
  sel.appendChild(aiuAdvOption('', 'None'));
  if (a.capable) {
    a.choices.forEach(function (c) { sel.appendChild(aiuAdvOption(c, c)); });
    if (a.model && a.choices.indexOf(a.model) < 0) sel.appendChild(aiuAdvOption(a.model, a.model));
    sel.value = a.model;
  } else sel.disabled = true;
  msg.setAttribute('role', 'status');
  box.appendChild(k);
  box.appendChild(sel);
  if (!a.capable) {
    var note = accText('p', 'adm-help aiu-adv-note', 'Anthropic only');
    note.id = id + '-adv-note';
    box.appendChild(note);
    notes.push(note.id);
  }
  if (a.problem) {
    var bad = accText('p', 'adm-help aiu-problem aiu-adv-problem bad', a.problem);
    bad.id = id + '-adv-problem';
    box.appendChild(bad);
    notes.push(bad.id);
  }
  if (notes.length) sel.setAttribute('aria-describedby', notes.join(' '));
  if (a.capable && a.model) {
    var vals = {uses: a.maxUses, tokens: a.maxTokens};
    Object.keys(AIU_ADV_FIELDS).forEach(function (n) {
      var f = AIU_ADV_FIELDS[n], wrap = el('div', 'aiu-adv-f'), lab = accText('label', 'aiu-adv-l', f.label), inp = el('input', 'aiu-adv-' + n);
      inp.type = 'number';
      inp.id = id + '-adv-' + n;
      inp.min = String(f.min);
      inp.max = String(f.max);
      inp.step = '1';
      inp.value = String(vals[n]);
      lab.setAttribute('for', inp.id);
      inp.addEventListener('change', function () { aiuAdvChange(u, f, inp.value); });
      wrap.appendChild(lab);
      wrap.appendChild(inp);
      box.appendChild(wrap);
    });
  }
  sel.addEventListener('change', function () { aiuAdvPut(u, '-advisor', sel.value); });
  box.appendChild(msg);
  var said = AIU_ADV_SAID[u.use];
  if (said) { msg.textContent = said.text; msg.className = 'adm-msg aiu-adv-msg ' + (said.bad ? 'bad' : 'ok'); delete AIU_ADV_SAID[u.use]; }
  if (AIU_ADV_BUSY[u.use]) box.querySelectorAll('select, input').forEach(function (c) { c.disabled = true; });
  return box;
}
function aiuAdvChange(u, f, raw) {
  var n = Number(raw);
  if (raw === '' || !isFinite(n) || n !== Math.floor(n) || n < f.min || n > f.max) {
    AIU_ADV_SAID[u.use] = {text: 'Enter a whole number from ' + f.min + ' to ' + f.max, bad: true};
    renderAiRows();
    return AIU_CHAIN;
  }
  return aiuAdvPut(u, f.suffix, String(n));
}
function aiuAdvPut(u, suffix, value) {
  if (AIU_ADV_BUSY[u.use]) return AIU_CHAIN;
  AIU_ADV_BUSY[u.use] = true;
  var key = u.setting + suffix, box = document.querySelector('#ai-rows .aiu-adv[data-use="' + u.use + '"]');
  if (box) box.querySelectorAll('select, input').forEach(function (c) { c.disabled = true; });
  var run = function () {
    return aiuAdvWrite(u, key, value).catch(function (e) {
      delete AIU_ADV_BUSY[u.use];
      failToast('the Who does what table could not be redrawn: ' + (e && e.message ? e.message : 'unknown error'));
      try { renderAiRows(); } catch (again) {}
    });
  };
  AIU_CHAIN = AIU_CHAIN.then(run, run);
  return AIU_CHAIN;
}
async function aiuAdvWrite(u, key, value) {
  var said;
  try {
    var r = await adminApi('PUT', '/api/admin/settings', {key: key, value: value});
    ADMIN.items = ADMIN.items.map(function (x) { return x.key === r.item.key ? r.item : x; });
    said = {text: r.item.live === false ? 'Saved, used after the next restart' : 'Saved, live within ' + ((ADMIN && ADMIN.refreshSeconds) || 15) + ' s on every pod', bad: false};
  } catch (e) {
    said = {text: e && e.detail ? e.detail : 'Not saved, try again', bad: true};
  }
  try { ADMIN_HIST = await adminApi('GET', '/api/admin/history?limit=' + ADMIN_HIST_MAX); } catch (e) {}
  await aiuReload();
  delete AIU_ADV_BUSY[u.use];
  AIU_ADV_SAID[u.use] = said;
  adminPaintAll(key);
  adminNotices();
}
function aiuSaved(i) { return String(i.value == null ? '' : i.value); }
function aiuControls(model, u, i, id, notes, msg) {
  var sel = aiuSelect(u, i, id), acts = accText('div', 'adm-acts aiu-acts'), save = accText('button', 'btn small aiu-save', 'Save'), cancel = accText('button', 'btn sec small aiu-cancel', 'Cancel');
  save.type = cancel.type = 'button';
  acts.appendChild(save);
  acts.appendChild(cancel);
  acts.appendChild(msg);
  model.appendChild(sel);
  model.appendChild(acts);
  var meta = el('div', 'aiu-meta', admMeta(i));
  model.appendChild(meta);
  if (i.help) {
    var help = el('div', 'aiu-help', admHelp(i.help));
    model.appendChild(help);
    admUnfold(help);
  }
  var reset = meta.querySelector('.adm-reset'), pending = AIU_PENDING[u.use] != null;
  var edit = AIU_EDIT[u.use], want = pending ? AIU_PENDING[u.use] : edit;
  if (want != null) aiuPick(sel, want, i);
  var show = function () {
    var dirty = !pending && sel.value !== aiuSaved(i);
    if (dirty) AIU_EDIT[u.use] = sel.value; else delete AIU_EDIT[u.use];
    save.hidden = cancel.hidden = !dirty;
  };
  show();
  if (i.off) sel.disabled = true;
  if (pending) [sel, save, cancel, reset].forEach(function (c) { if (c) c.disabled = true; });
  ['input', 'change'].forEach(function (ev) { sel.addEventListener(ev, function () { show(); msg.textContent = ''; msg.className = 'adm-msg aiu-msg'; }); });
  sel.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' || AIU_EDIT[u.use] == null) return;
    e.preventDefault();
    aiuSave(u, i);
  });
  save.addEventListener('click', function () { aiuSave(u, i); });
  cancel.addEventListener('click', function () {
    delete AIU_EDIT[u.use];
    sel.value = aiuSaved(i);
    show();
    sel.focus();
  });
  if (reset) reset.addEventListener('click', function () { aiuQueue(u, i, null); });
}
function aiuPick(sel, value, i) {
  if (![].some.call(sel.options, function (o) { return o.value === value; })) sel.insertBefore(aiuOption(i, value), sel.firstChild);
  sel.value = value;
}
function aiuOption(i, c) {
  var o = accText('option', null, c === '' ? (i.choiceLabels || {})[''] || 'Default' : admChoiceText(i, c));
  o.value = c;
  return o;
}
function aiuSelect(u, i, id) {
  var sel = el('select', 'aiu-in'), cur = aiuSaved(i);
  sel.id = id + '-model';
  sel.setAttribute('aria-labelledby', id + '-l ' + id + '-k');
  var groups = i.choiceGroups && typeof i.choiceGroups === 'object' && !Array.isArray(i.choiceGroups) ? i.choiceGroups : {}, inGroup = {};
  Object.keys(groups).forEach(function (g) { (Array.isArray(groups[g]) ? groups[g] : []).forEach(function (c) { inGroup[String(c)] = 1; }); });
  (Array.isArray(i.choices) ? i.choices : []).map(String).filter(function (c) { return !inGroup[c]; }).forEach(function (c) { sel.appendChild(aiuOption(i, c)); });
  Object.keys(groups).forEach(function (g) {
    var og = document.createElement('optgroup');
    og.label = g;
    (Array.isArray(groups[g]) ? groups[g] : []).map(String).forEach(function (c) { og.appendChild(aiuOption(i, c)); });
    if (og.children.length) sel.appendChild(og);
  });
  aiuPick(sel, cur, i);
  return sel;
}
function aiuRowEl(use) { return [].filter.call(document.querySelectorAll('#ai-rows .aiu-row'), function (r) { return r.dataset.use === use; })[0] || null; }
function aiuSave(u, i) {
  var value = AIU_EDIT[u.use];
  if (value == null) return AIU_CHAIN;
  return aiuQueue(u, i, value);
}
function aiuQueue(u, i, value) {
  if (AIU_PENDING[u.use] != null) return AIU_CHAIN;
  var row = aiuRowEl(u.use), had = !!row && (row.contains(document.activeElement) || document.activeElement === document.body);
  AIU_PENDING[u.use] = value === null ? String(i.base == null ? '' : i.base) : value;
  delete AIU_EDIT[u.use];
  if (row) {
    row.querySelectorAll('.aiu-model select, .aiu-model button').forEach(function (c) { c.disabled = true; });
    var m = row.querySelector('.aiu-msg');
    if (m) { m.textContent = 'Saving\u2026'; m.className = 'adm-msg aiu-msg'; }
  }
  var run = function () {
    return aiuWrite(u, i, value, had).catch(function (e) {
      delete AIU_PENDING[u.use];
      failToast('the Who does what table could not be redrawn: ' + (e && e.message ? e.message : 'unknown error'));
      try { renderAiRows(); } catch (again) {}
    });
  };
  AIU_CHAIN = AIU_CHAIN.then(run, run);
  return AIU_CHAIN;
}
async function aiuWrite(u, i, value, had) {
  var said;
  try {
    var r = value === null ? await adminApi('DELETE', '/api/admin/settings?key=' + encodeURIComponent(i.key)) : await adminApi('PUT', '/api/admin/settings', {key: i.key, value: value});
    ADMIN.items = ADMIN.items.map(function (x) { return x.key === r.item.key ? r.item : x; });
    said = {text: value === null ? 'Reset to ' + (r.item.value || 'empty') : r.item.live ? 'Saved, live within ' + ((ADMIN && ADMIN.refreshSeconds) || 15) + ' s on every pod' : 'Saved, used after the next restart', bad: false};
  } catch (e) {
    said = {text: e && e.detail ? e.detail : value === null ? 'Not reset, try again' : 'Not saved, try again', bad: true};
  }
  try { ADMIN_HIST = await adminApi('GET', '/api/admin/history?limit=' + ADMIN_HIST_MAX); } catch (e) {}
  await aiuReload();
  delete AIU_PENDING[u.use];
  AIU_SAID[u.use] = said;
  adminPaintAll(i.key);
  adminNotices();
  var now = document.activeElement, sel = document.getElementById(aiuId(u.use) + '-model');
  if (had && sel && !sel.disabled && (!now || now === document.body || !now.isConnected)) sel.focus();
}
async function aiuReload() {
  var seq = ++AIU_SEQ, r = await adminApi('GET', '/api/admin/ai-uses').catch(adminNone);
  if (seq !== AIU_SEQ || r === null) return false;
  AIUSES = aiuOf(r);
  return true;
}
function aicSay(card, text, bad) {
  var msg = card && card.querySelector('.aic-msg');
  if (!msg) return;
  msg.textContent = text || '';
  msg.className = 'adm-msg aic-msg' + (bad ? ' bad' : text ? ' ok' : '');
}
function aicResult(into, r, tail) {
  var fit = !!r.ok && r.tools !== false, summary = String(r.summary || '');
  into.textContent = (fit ? '\u2713 ' : '') + summary + tail;
  if (r.tools === false && !/not fit/i.test(summary)) {
    into.appendChild(document.createTextNode(' \u00b7 '));
    into.appendChild(accText('span', 'aic-notfit', AIC_NOT_FIT));
  }
  return fit;
}
function aicNewLi() {
  var add = accText('li', 'ai-provider aic-add-li'), fold = el('details', 'aic-add');
  fold.appendChild(accText('summary', 'aic-add-s', 'Add compatible connection'));
  fold.appendChild(aicCard(null));
  add.appendChild(fold);
  return add;
}
function aicConnLi(c) {
  var li = accText('li', 'ai-provider');
  li.appendChild(aicCard(c));
  return li;
}
function aicPaint(box, said, saidFor) {
  if (!AICONN) return;
  var wrap = el('div', 'aic');
  if (AICONN.failed) {
    if (AICONN.code !== 404) wrap.appendChild(accText('p', 'adm-msg bad aic-failed', 'The compatible connections did not load; open the tab again.'));
    box.appendChild(wrap);
    return;
  }
  var list = accText('ul', 'aic-list');
  AICONN.connections.forEach(function (c) {
    var li = aicConnLi(c);
    list.appendChild(li);
    if (said && saidFor === 'compat:' + c.name) aicSay(li.firstChild, said);
  });
  list.appendChild(aicNewLi());
  wrap.appendChild(list);
  var note = accText('p', 'adm-msg aic-said' + (said && saidFor === 'compat' ? ' ok' : ''), said && saidFor === 'compat' ? said : '');
  note.setAttribute('role', 'status');
  wrap.appendChild(note);
  wrap.appendChild(aicPrivate());
  box.appendChild(wrap);
}
function aicPrivate() {
  var p = accText('p', 'adm-help aic-private'), on = AICONN.allowPrivate;
  p.appendChild(accText('span', 'aic-private-t', on ? 'Private and loopback addresses are allowed for compatible connections (core.allow-private-ai is on).' : 'Private and loopback addresses are refused for compatible connections (core.allow-private-ai is off).'));
  if (!aiuItem(AIC_PRIVATE_KEY) || typeof window.__skopaxFindSetting !== 'function') return p;
  p.appendChild(document.createTextNode(' '));
  var go = accText('a', 'aic-private-go', 'Open the setting');
  go.href = '#t=settings';
  go.addEventListener('click', function (e) {
    e.preventDefault();
    window.__skopaxFindSetting(AIC_PRIVATE_KEY);
  });
  p.appendChild(go);
  return p;
}
var AIC_REFRESH_SEQ = 0, AIC_REFRESH = null;
function aicRefresh() {
  var seq = ++AIC_REFRESH_SEQ;
  AIC_REFRESH = aicRead(seq);
  return AIC_REFRESH;
}
async function aicRead(seq) {
  var r = await Promise.all([adminApi('GET', '/api/admin/ai-connections').catch(adminNone), adminApi('GET', '/api/admin/settings').catch(adminNone), adminApi('GET', '/api/admin/ai-uses').catch(adminNone)]);
  if (seq !== AIC_REFRESH_SEQ) return AIC_REFRESH;
  var fresh = aicOf(r[0]);
  if (fresh && !fresh.failed) AICONN = fresh;
  if (r[1]) ADMIN = r[1];
  if (r[2] !== null) AIUSES = aiuOf(r[2]);
  adminPaintAll('+connections');
  var old = document.querySelector('#ai .aic-private');
  if (old && AICONN && !AICONN.failed) old.replaceWith(aicPrivate());
}
function aicFind(name) { return AICONN && AICONN.connections ? AICONN.connections.filter(function (c) { return c.name === name; })[0] || null : null; }
function aicCardEl(name) { return [].filter.call(document.querySelectorAll('#ai .aic-card[data-conn]'), function (k) { return k.dataset.conn === name; })[0] || null; }
function aicInput(cls, id, value) {
  var input = document.createElement('input');
  input.className = cls;
  if (id) input.id = id;
  input.type = 'text';
  input.value = value || '';
  input.setAttribute('autocomplete', 'off');
  input.setAttribute('spellcheck', 'false');
  input.setAttribute('autocapitalize', 'off');
  input.setAttribute('autocorrect', 'off');
  return input;
}
function aicField(card, text, input) {
  var field = accText('div', 'ai-field'), label = accText('label', null, text);
  label.setAttribute('for', input.id);
  field.appendChild(label);
  field.appendChild(input);
  card.appendChild(field);
}
function aicPrice(cls, v) {
  var input = document.createElement('input');
  input.className = cls;
  input.type = 'number';
  input.min = '0';
  input.step = 'any';
  input.setAttribute('inputmode', 'decimal');
  input.value = v == null ? '' : String(v);
  return input;
}
function aicNumber(list) {
  [].forEach.call(list.querySelectorAll('.aic-model'), function (li, n) {
    var at = String(n + 1);
    li.querySelector('.aic-m-id').setAttribute('aria-label', 'Model ' + at + ' id');
    li.querySelector('.aic-m-in').setAttribute('aria-label', 'Model ' + at + ' input price, USD per million tokens');
    li.querySelector('.aic-m-out').setAttribute('aria-label', 'Model ' + at + ' output price, USD per million tokens');
    li.querySelector('.aic-m-x').setAttribute('aria-label', 'Remove model ' + at);
  });
}
function aicModel(m, list) {
  var li = accText('li', 'aic-model');
  li.appendChild(aicInput('aic-m-id', '', m.id));
  li.appendChild(aicPrice('aic-m-in', m.input));
  li.appendChild(aicPrice('aic-m-out', m.output));
  if (m.ref) li.appendChild(accText('code', 'aic-m-ref', m.ref));
  var x = accText('button', 'btn sec small aic-m-x', 'Remove');
  x.type = 'button';
  x.addEventListener('click', function () {
    var next = li.nextElementSibling || li.previousElementSibling;
    li.remove();
    aicNumber(list);
    var to = next ? next.querySelector('.aic-m-id') : list.parentNode.querySelector('.aic-m-add');
    if (to) to.focus();
  });
  li.appendChild(x);
  return li;
}
function aicBody(card) {
  var body = {baseUrl: card.querySelector('.aic-url').value.trim(), models: [], tools: card.querySelector('.aic-tools').checked, images: card.querySelector('.aic-images').checked};
  var key = card.querySelector('.aic-key-in').value.trim();
  if (key) body.key = key;
  card.querySelectorAll('.aic-model').forEach(function (li) {
    var id = li.querySelector('.aic-m-id').value.trim(), a = li.querySelector('.aic-m-in').value.trim(), b = li.querySelector('.aic-m-out').value.trim();
    if (!id) return;
    var m = {id: id};
    if (a !== '') m.input = Number(a);
    if (b !== '') m.output = Number(b);
    body.models.push(m);
  });
  return body;
}
function aicLastLine(c) {
  var last = accText('p', 'adm-help aic-last'), t = c.lastTest;
  last.appendChild(accText('span', 'aic-last-k', 'Last test'));
  last.appendChild(document.createTextNode(' '));
  var v = accText('span', 'aic-last-v', t ? '' : 'Not tested yet');
  if (t) v.classList.add(aicResult(v, t, (t.model ? ' \u00b7 ' + t.model : '') + (t.at ? ' \u00b7 ' + dayhm(t.at) : '')) ? 'ok' : 'bad');
  last.appendChild(v);
  return last;
}
function aicCheck(card, cls, text, on) {
  var label = accText('label', 'aic-cap'), box = document.createElement('input');
  box.type = 'checkbox';
  box.className = cls;
  box.checked = on;
  label.appendChild(box);
  label.appendChild(accText('span', null, text));
  card.appendChild(label);
}
function aicCard(c) {
  var id = 'aic-' + (++AIC_SEQ), card = accText('section', 'ai-key aic-card' + (c ? '' : ' aic-new'));
  if (c) card.dataset.conn = c.name;
  card.setAttribute('aria-labelledby', id + '-h');
  var head = accText('div', 'ai-head'), h = accText('h3', null, c ? c.name : 'New compatible connection');
  h.id = id + '-h';
  head.appendChild(h);
  if (c) head.appendChild(accText('span', 'tag aic-keytag ' + (c.keySet ? 'k-ok' : 'k-warn'), c.keySet ? 'key set' : 'no key'));
  card.appendChild(head);
  if (c) {
    var co = accText('p', 'ai-state aic-co');
    co.appendChild(accText('span', null, 'Data goes to'));
    co.appendChild(document.createTextNode(' '));
    co.appendChild(accText('b', 'aic-co-v', c.company));
    card.appendChild(co);
    card.appendChild(aicLastLine(c));
  }
  var nameIn = null;
  if (!c) {
    nameIn = aicInput('aic-n', id + '-n', '');
    nameIn.maxLength = 40;
    nameIn.placeholder = 'deepseek';
    aicField(card, 'Name', nameIn);
  }
  var url = aicInput('aic-url', id + '-url', c ? c.baseUrl : '');
  url.maxLength = 400;
  url.setAttribute('inputmode', 'url');
  url.placeholder = 'https://api.example.com/v1';
  aicField(card, 'Base URL', url);
  var key = aicInput('aic-key-in', id + '-key', '');
  key.type = 'password';
  key.setAttribute('autocomplete', 'new-password');
  key.maxLength = 400;
  key.placeholder = 'Paste a new key';
  key.setAttribute('data-1p-ignore', '');
  key.setAttribute('data-lpignore', 'true');
  key.setAttribute('data-bwignore', '');
  key.setAttribute('data-form-type', 'other');
  aicField(card, c && c.keySet ? 'New key' : 'Key', key);
  var mbox = accText('div', 'aic-models-box'), mh = accText('span', 'aic-mh', 'Models, with prices in USD per million tokens (input, output)'), list = accText('ul', 'aic-models');
  mh.id = id + '-mh';
  mbox.setAttribute('role', 'group');
  mbox.setAttribute('aria-labelledby', mh.id);
  mbox.appendChild(mh);
  (c && c.models.length ? c.models : [{id: '', ref: '', input: null, output: null}]).forEach(function (m) { list.appendChild(aicModel(m, list)); });
  aicNumber(list);
  mbox.appendChild(list);
  var more = accText('button', 'btn sec small aic-m-add', 'Add model');
  more.type = 'button';
  more.addEventListener('click', function () {
    var li = aicModel({id: '', ref: '', input: null, output: null}, list);
    list.appendChild(li);
    aicNumber(list);
    li.querySelector('.aic-m-id').focus();
  });
  mbox.appendChild(more);
  card.appendChild(mbox);
  aicCheck(card, 'aic-tools', 'Uses tools', c ? c.tools : true);
  aicCheck(card, 'aic-images', 'Reads images', c ? c.images : false);
  var acts = accText('div', 'adm-acts ai-acts aic-acts');
  var saveB = accText('button', 'btn small aic-save', 'Save'), testB = accText('button', 'btn sec small aic-test', 'Test connection'), delB = null;
  saveB.type = testB.type = 'button';
  acts.appendChild(saveB);
  acts.appendChild(testB);
  if (c) {
    delB = accText('button', 'btn sec small ai-remove aic-del', 'Delete');
    delB.type = 'button';
    acts.appendChild(delB);
  }
  var msg = accText('span', 'adm-msg aic-msg');
  msg.setAttribute('role', 'status');
  acts.appendChild(msg);
  card.appendChild(acts);
  var say = function (text, bad) { aicSay(card, text, bad); };
  var named = function (adding) {
    var name = c ? c.name : nameIn.value.trim();
    if (!name) { say('Enter a name', true); nameIn.focus(); return ''; }
    if (adding && AICONN && AICONN.connections && AICONN.connections.some(function (x) { return x.name.toLowerCase() === name.toLowerCase(); })) {
      say('That name is already used by another connection: pick another one', true);
      nameIn.focus();
      return '';
    }
    return name;
  };
  var path = function (name) { return '/api/admin/ai-connections/' + encodeURIComponent(name); };
  var testKey = c ? c.name : '';
  testB.disabled = !!AIC_TESTING[testKey];
  saveB.addEventListener('click', async function () {
    var name = named(!c);
    if (!name) return;
    saveB.disabled = true;
    say('Saving\u2026');
    try {
      await adminApi('PUT', path(name), aicBody(card));
    } catch (e) {
      say(e && e.detail ? e.detail : 'Not saved, try again', true);
      saveB.disabled = false;
      return;
    }
    key.value = '';
    adminNotices();
    await aicRefresh();
    var conn = aicFind(name), li = card.closest('li');
    if (!conn || !li || !li.isConnected) { say('Saved'); saveB.disabled = false; return; }
    var fresh = aicConnLi(conn);
    if (c) li.replaceWith(fresh);
    else {
      li.parentNode.insertBefore(fresh, li);
      li.replaceWith(aicNewLi());
    }
    aicSay(fresh.firstChild, 'Saved');
    fresh.querySelector('.aic-save').focus();
  });
  testB.addEventListener('click', async function () {
    if (AIC_TESTING[testKey]) return;
    var name = named(false);
    if (!name) return;
    AIC_TESTING[testKey] = true;
    testB.disabled = true;
    say('Testing\u2026');
    try {
      var r = await adminApi('POST', path(name) + '/test', aicBody(card));
      var ok = aicResult(msg, r || {}, ' \u00b7 ' + Number((r && r.ms) || 0) + ' ms');
      msg.className = 'adm-msg aic-msg ' + (ok ? 'ok' : 'bad');
    } catch (e) {
      say(e && e.detail ? e.detail : 'The test could not run', true);
    }
    AIC_TESTING[testKey] = false;
    testB.disabled = false;
    var live = c ? aicCardEl(c.name) : document.querySelector('#ai .aic-new');
    if (live) live.querySelector('.aic-test').disabled = false;
    var seen = await adminApi('GET', '/api/admin/ai-connections').catch(adminNone), fresh = aicOf(seen);
    if (fresh && !fresh.failed) {
      AICONN = fresh;
      var conn = c && aicFind(c.name), at = live && live.querySelector('.aic-last');
      if (conn && at) at.replaceWith(aicLastLine(conn));
    }
    if (await aiuReload()) renderAiRows();
  });
  if (delB) delB.addEventListener('click', async function () {
    var ask = window.__skopaxConfirm;
    if (typeof ask !== 'function') return;
    var ok = await ask('Delete this compatible connection?', 'Its key and models are deleted. A use set to one of its models is held until another model is picked.', 'Delete connection');
    if (!ok) { if (delB.isConnected) delB.focus(); return; }
    delB.disabled = true;
    say('Deleting\u2026');
    try {
      await adminApi('DELETE', path(c.name));
    } catch (e) {
      delB.disabled = false;
      say(e && e.detail ? e.detail : 'Not deleted, try again', true);
      return;
    }
    adminNotices();
    await aicRefresh();
    var li = card.closest('li'), next = li && (li.nextElementSibling || li.previousElementSibling);
    if (li) li.remove();
    var note = document.querySelector('#ai .aic-said');
    if (note) { note.textContent = 'Deleted'; note.className = 'adm-msg aic-said ok'; }
    var to = next && (next.querySelector('.aic-card[data-conn] .aic-url') || next.querySelector('summary'));
    if (to) to.focus();
  });
  return card;
}
function aivSay(card, text, bad) {
  var msg = card && card.querySelector('.aiv-msg');
  if (!msg) return;
  msg.textContent = text || '';
  msg.className = 'adm-msg aic-msg aiv-msg' + (bad ? ' bad' : text ? ' ok' : '');
}
function aivConn(c) {
  var cr = c.credentials && typeof c.credentials === 'object' ? c.credentials : {};
  return {name: c.name, project: aiuText(c.project), region: aiuText(c.region), mode: AIV_MODES.indexOf(cr.mode) >= 0 ? cr.mode : 'key', set: cr.set === true, company: aiuText(c.company),
    models: (Array.isArray(c.models) ? c.models : []).filter(function (m) { return m && typeof m.id === 'string'; }).map(function (m) { return {id: m.id, vertexId: aiuText(m.vertexId), input: aiuNum(m.input), output: aiuNum(m.output)}; }),
    lastTest: aicLast(c.lastTest)};
}
function aivOf(r) {
  if (r && r.failed) return {failed: true, code: r.code};
  if (!r || !Array.isArray(r.items)) return null;
  return {regions: (Array.isArray(r.regions) ? r.regions : []).filter(function (x) { return typeof x === 'string' && x; }),
    connections: r.items.filter(function (c) { return c && typeof c.name === 'string' && c.name; }).map(aivConn)};
}
function aivFind(name) { return AIVTX && AIVTX.connections ? AIVTX.connections.filter(function (c) { return c.name === name; })[0] || null : null; }
function aivCardEl(name) { return [].filter.call(document.querySelectorAll('#ai .aiv-card[data-vertex]'), function (k) { return k.dataset.vertex === name; })[0] || null; }
function aivResults(models) {
  var list = accText('ul', 'aiv-res');
  list.setAttribute('aria-label', 'Test result per model');
  models.forEach(function (m) {
    var li = accText('li', 'aiv-res-m ' + (m.ok ? 'ok' : 'bad'));
    li.appendChild(accText('code', 'aiv-res-id', m.id));
    li.appendChild(document.createTextNode(' ' + (m.ok ? '\u2713 ' : '') + m.summary));
    list.appendChild(li);
  });
  return list;
}
function aivNewLi() {
  var add = accText('li', 'ai-provider aiv-add-li'), fold = el('details', 'aiv-add');
  fold.appendChild(accText('summary', 'aiv-add-s', 'Add Vertex AI connection'));
  fold.appendChild(aivCard(null));
  add.appendChild(fold);
  return add;
}
function aivConnLi(c) {
  var li = accText('li', 'ai-provider');
  li.appendChild(aivCard(c));
  return li;
}
function aivPaint(box) {
  if (!AIVTX) return;
  var wrap = el('div', 'aiv');
  if (AIVTX.failed) {
    if (AIVTX.code !== 404) {
      wrap.appendChild(accText('p', 'adm-msg bad aiv-failed', 'The Vertex AI connections did not load; open the tab again.'));
      box.appendChild(wrap);
    }
    return;
  }
  var list = accText('ul', 'aic-list aiv-list');
  AIVTX.connections.forEach(function (c) { list.appendChild(aivConnLi(c)); });
  list.appendChild(aivNewLi());
  wrap.appendChild(list);
  box.appendChild(wrap);
}
var AIV_REFRESH_SEQ = 0, AIV_REFRESH = null;
function aivRefresh() {
  var seq = ++AIV_REFRESH_SEQ;
  AIV_REFRESH = aivRead(seq);
  return AIV_REFRESH;
}
async function aivRead(seq) {
  var r = await Promise.all([adminApi('GET', '/api/admin/ai-vertex').catch(adminNone), adminApi('GET', '/api/admin/settings').catch(adminNone), adminApi('GET', '/api/admin/ai-uses').catch(adminNone)]);
  if (seq !== AIV_REFRESH_SEQ) return AIV_REFRESH;
  var fresh = aivOf(r[0]);
  if (fresh && !fresh.failed) AIVTX = fresh;
  if (r[1]) ADMIN = r[1];
  if (r[2] !== null) AIUSES = aiuOf(r[2]);
  adminPaintAll('+connections');
}
function aivRegion(c, regions) {
  var sel = el('select', 'aiv-region'), seen = {};
  var add = function (value, text) {
    var o = accText('option', null, text);
    o.value = value;
    sel.appendChild(o);
  };
  add('', 'Choose a region');
  regions.forEach(function (r) { seen[r] = 1; add(r, AIV_REGION_TEXT[r] || r); });
  if (c && c.region && !seen[c.region]) add(c.region, c.region);
  sel.value = c ? c.region : '';
  return sel;
}
function aivModeSelect(c) {
  var sel = el('select', 'aiv-mode');
  [['key', 'Service-account key'], ['federation', 'Workload identity federation'], ['pod', 'The pod\'s Google identity']].forEach(function (m) {
    var o = accText('option', null, m[1]);
    o.value = m[0];
    sel.appendChild(o);
  });
  sel.value = c ? c.mode : 'key';
  return sel;
}
var AIV_NOTES = {
  key: 'The key is saved sealed and never shown again. Enter it again when the project or region changes.',
  federation: 'The config holds no secret: it names the token file mounted in the pod. The token file must be under the directory set by SKOPAX_VERTEX_TOKEN_DIR, or the config is refused.',
  pod: 'Nothing secret is stored. The pod runs as its own Google identity (for example workload identity on GKE).'
};
function aivSecret(box, id, c, mode) {
  box.textContent = '';
  var held = !!c && c.mode === mode && c.set;
  if (mode === 'key') {
    var key = aicInput('aiv-key-in', id + '-key', '');
    key.type = 'password';
    key.setAttribute('autocomplete', 'new-password');
    key.maxLength = 8000;
    key.placeholder = 'Paste a new key';
    key.setAttribute('data-1p-ignore', '');
    key.setAttribute('data-lpignore', 'true');
    key.setAttribute('data-bwignore', '');
    key.setAttribute('data-form-type', 'other');
    aicField(box, held ? 'New key' : 'Key', key);
  } else if (mode === 'federation') {
    var conf = document.createElement('textarea');
    conf.className = 'aiv-fed-in';
    conf.id = id + '-fed';
    conf.rows = 6;
    conf.maxLength = 8000;
    conf.placeholder = 'Paste the external-account JSON config';
    conf.setAttribute('autocomplete', 'off');
    conf.setAttribute('spellcheck', 'false');
    conf.setAttribute('autocapitalize', 'off');
    conf.setAttribute('autocorrect', 'off');
    aicField(box, held ? 'New federation config (JSON)' : 'Federation config (JSON)', conf);
  }
  box.appendChild(accText('p', 'adm-help aiv-mode-note', AIV_NOTES[mode]));
}
function aivNumber(list) {
  [].forEach.call(list.querySelectorAll('.aiv-model'), function (li, n) {
    var at = String(n + 1);
    li.querySelector('.aiv-m-name').setAttribute('aria-label', 'Model ' + at + ' name');
    li.querySelector('.aiv-m-vid').setAttribute('aria-label', 'Model ' + at + ' Vertex id');
    li.querySelector('.aiv-m-in').setAttribute('aria-label', 'Model ' + at + ' input price, USD per million tokens');
    li.querySelector('.aiv-m-out').setAttribute('aria-label', 'Model ' + at + ' output price, USD per million tokens');
    li.querySelector('.aiv-m-x').setAttribute('aria-label', 'Remove model ' + at);
  });
}
function aivModel(m, list, conn) {
  var li = accText('li', 'aic-model aiv-model');
  li.appendChild(aicInput('aiv-m-name', '', m.id));
  li.appendChild(aicInput('aiv-m-vid', '', m.vertexId));
  li.appendChild(aicPrice('aiv-m-in', m.input));
  li.appendChild(aicPrice('aiv-m-out', m.output));
  if (m.id && conn) li.appendChild(accText('code', 'aic-m-ref aiv-m-ref', conn + '/' + m.id));
  var x = accText('button', 'btn sec small aiv-m-x', 'Remove');
  x.type = 'button';
  x.addEventListener('click', function () {
    var next = li.nextElementSibling || li.previousElementSibling;
    li.remove();
    aivNumber(list);
    var to = next ? next.querySelector('.aiv-m-name') : list.parentNode.querySelector('.aiv-m-add');
    if (to) to.focus();
  });
  li.appendChild(x);
  return li;
}
function aivBody(card) {
  var mode = card.querySelector('.aiv-mode').value, cr = {mode: mode};
  var key = card.querySelector('.aiv-key-in'), conf = card.querySelector('.aiv-fed-in');
  if (mode === 'key' && key && key.value.trim()) cr.key = key.value.trim();
  if (mode === 'federation' && conf && conf.value.trim()) cr.config = conf.value.trim();
  var body = {project: card.querySelector('.aiv-project').value.trim(), region: card.querySelector('.aiv-region').value, credentials: cr, models: []};
  card.querySelectorAll('.aiv-model').forEach(function (li) {
    var id = li.querySelector('.aiv-m-name').value.trim(), vid = li.querySelector('.aiv-m-vid').value.trim(), a = li.querySelector('.aiv-m-in').value.trim(), b = li.querySelector('.aiv-m-out').value.trim();
    if (!id && !vid) return;
    var m = {id: id, vertexId: vid};
    if (a !== '') m.input = Number(a);
    if (b !== '') m.output = Number(b);
    body.models.push(m);
  });
  return body;
}
function aivTag(c) {
  if (c.mode === 'pod') return ['pod identity', 'k-ok'];
  if (c.mode === 'federation') return c.set ? ['config set', 'k-ok'] : ['no config', 'k-warn'];
  return c.set ? ['key set', 'k-ok'] : ['no key', 'k-warn'];
}
function aivCard(c) {
  var id = 'aiv-' + (++AIC_SEQ), card = accText('section', 'ai-key aic-card aiv-card' + (c ? '' : ' aiv-new'));
  card.classList.remove('aic-card');
  if (c) card.dataset.vertex = c.name;
  card.setAttribute('aria-labelledby', id + '-h');
  var head = accText('div', 'ai-head'), h = accText('h3', null, c ? c.name : 'New Vertex AI connection');
  h.id = id + '-h';
  head.appendChild(h);
  var tagBox = null;
  if (c) {
    var tag = aivTag(c);
    tagBox = accText('span', 'tag aiv-credtag ' + tag[1], tag[0]);
    head.appendChild(tagBox);
  }
  card.appendChild(head);
  if (c) {
    var co = accText('p', 'ai-state aic-co');
    co.appendChild(accText('span', null, 'Data goes to'));
    co.appendChild(document.createTextNode(' '));
    co.appendChild(accText('b', 'aiv-co-v', c.company));
    card.appendChild(co);
    card.appendChild(aicLastLine(c));
  }
  var nameIn = null;
  if (!c) {
    nameIn = aicInput('aiv-n', id + '-n', '');
    nameIn.maxLength = 20;
    nameIn.placeholder = 'vertex-eu';
    aicField(card, 'Name', nameIn);
  }
  var project = aicInput('aiv-project', id + '-project', c ? c.project : '');
  project.maxLength = 100;
  project.placeholder = 'my-project-id';
  aicField(card, 'Project id', project);
  var regions = AIVTX && AIVTX.regions ? AIVTX.regions : [], region = aivRegion(c, regions);
  region.id = id + '-region';
  aicField(card, 'Region', region);
  card.appendChild(accText('p', 'adm-help aiv-region-note', AIV_REGION_NOTE));
  var mode = aivModeSelect(c);
  mode.id = id + '-mode';
  aicField(card, 'Credentials', mode);
  var secret = accText('div', 'aiv-secret');
  aivSecret(secret, id, c, mode.value);
  card.appendChild(secret);
  mode.addEventListener('change', function () { aivSecret(secret, id, c, mode.value); });
  var mbox = accText('div', 'aic-models-box aiv-models-box'), mh = accText('span', 'aic-mh aiv-mh', 'Models: Skopax name, Vertex model id, prices in USD per million tokens (input, output)'), list = accText('ul', 'aic-models');
  mh.id = id + '-mh';
  mbox.setAttribute('role', 'group');
  mbox.setAttribute('aria-labelledby', mh.id);
  mbox.appendChild(mh);
  (c && c.models.length ? c.models : [{id: '', vertexId: '', input: null, output: null}]).forEach(function (m) { list.appendChild(aivModel(m, list, c && c.name)); });
  aivNumber(list);
  mbox.appendChild(list);
  var more = accText('button', 'btn sec small aiv-m-add', 'Add model');
  more.type = 'button';
  more.addEventListener('click', function () {
    var li = aivModel({id: '', vertexId: '', input: null, output: null}, list, c && c.name);
    list.appendChild(li);
    aivNumber(list);
    li.querySelector('.aiv-m-name').focus();
  });
  mbox.appendChild(more);
  mbox.appendChild(accText('p', 'adm-help aiv-price-note', 'Check your Google Cloud price sheet: regional prices can differ from the Anthropic list prices.'));
  card.appendChild(mbox);
  var acts = accText('div', 'adm-acts ai-acts aic-acts');
  var saveB = accText('button', 'btn small aiv-save', 'Save'), testB = accText('button', 'btn sec small aiv-test', 'Test connection'), delB = null;
  saveB.type = testB.type = 'button';
  acts.appendChild(saveB);
  acts.appendChild(testB);
  if (c) {
    delB = accText('button', 'btn sec small ai-remove aiv-del', 'Delete');
    delB.type = 'button';
    acts.appendChild(delB);
  }
  var msg = accText('span', 'adm-msg aic-msg aiv-msg');
  msg.setAttribute('role', 'status');
  acts.appendChild(msg);
  card.appendChild(acts);
  var results = aivResults(c && c.lastTest ? c.lastTest.models : []);
  card.appendChild(results);
  var say = function (text, bad) { aivSay(card, text, bad); };
  var ready = function (adding) {
    var name = c ? c.name : nameIn.value.trim();
    if (!name) { say('Enter a name', true); nameIn.focus(); return ''; }
    if (adding && AIVTX && AIVTX.connections && AIVTX.connections.some(function (x) { return x.name.toLowerCase() === name.toLowerCase(); })) {
      say('That name is already used by another connection: pick another one', true);
      nameIn.focus();
      return '';
    }
    if (!project.value.trim()) { say('Enter a project id', true); project.focus(); return ''; }
    if (!region.value) { say('Choose a region', true); region.focus(); return ''; }
    return name;
  };
  var path = function (name) { return '/api/admin/ai-vertex/' + encodeURIComponent(name); };
  var testKey = c ? c.name : id;
  testB.disabled = !!AIV_TESTING[testKey];
  saveB.addEventListener('click', async function () {
    var name = ready(!c);
    if (!name) return;
    saveB.disabled = true;
    say('Saving\u2026');
    try {
      await adminApi('PUT', path(name), aivBody(card));
    } catch (e) {
      [].forEach.call(card.querySelectorAll('.aiv-key-in, .aiv-fed-in'), function (x) { x.value = ''; });
      say(e && e.detail ? e.detail : 'Not saved, try again', true);
      saveB.disabled = false;
      return;
    }
    adminNotices();
    await aivRefresh();
    var conn = aivFind(name), li = card.closest('li');
    if (!conn || !li || !li.isConnected) { say('Saved'); saveB.disabled = false; return; }
    var fresh = aivConnLi(conn);
    if (c) li.replaceWith(fresh);
    else {
      li.parentNode.insertBefore(fresh, li);
      li.replaceWith(aivNewLi());
    }
    aivSay(fresh.firstChild, 'Saved');
    fresh.querySelector('.aiv-save').focus();
  });
  testB.addEventListener('click', async function () {
    if (AIV_TESTING[testKey]) return;
    var name = ready(false);
    if (!name) return;
    AIV_TESTING[testKey] = true;
    testB.disabled = true;
    say('Testing\u2026');
    try {
      var r = await adminApi('POST', path(name) + '/test', aivBody(card));
      var ok = aicResult(msg, r || {}, ' \u00b7 ' + Number((r && r.ms) || 0) + ' ms');
      msg.className = 'adm-msg aic-msg aiv-msg ' + (ok ? 'ok' : 'bad');
      results.replaceWith(results = aivResults(aivTested(r && r.models)));
    } catch (e) {
      say(e && e.detail ? e.detail : 'The test could not run', true);
    }
    AIV_TESTING[testKey] = false;
    testB.disabled = false;
    var live = c ? aivCardEl(c.name) : document.querySelector('#ai .aiv-new');
    if (live) live.querySelector('.aiv-test').disabled = false;
    var seen = await adminApi('GET', '/api/admin/ai-vertex').catch(adminNone), fresh = aivOf(seen);
    if (fresh && !fresh.failed) {
      AIVTX = fresh;
      var conn = c && aivFind(c.name), at = live && live.querySelector('.aic-last');
      if (conn && at) at.replaceWith(aicLastLine(conn));
    }
    if (await aiuReload()) renderAiRows();
  });
  if (delB) delB.addEventListener('click', async function () {
    var ask = window.__skopaxConfirm;
    if (typeof ask !== 'function') return;
    var ok = await ask('Delete this Vertex AI connection?', 'Its credentials and models are deleted. A use set to one of its models is held until another model is picked.', 'Delete connection');
    if (!ok) { if (delB.isConnected) delB.focus(); return; }
    delB.disabled = true;
    say('Deleting\u2026');
    try {
      await adminApi('DELETE', path(c.name));
    } catch (e) {
      delB.disabled = false;
      say(e && e.detail ? e.detail : 'Not deleted, try again', true);
      return;
    }
    adminNotices();
    await aivRefresh();
    var li = card.closest('li'), next = li && (li.nextElementSibling || li.previousElementSibling);
    if (li) li.remove();
    var to = next && (next.querySelector('.aiv-card[data-vertex] .aiv-project') || next.querySelector('summary'));
    if (to) to.focus();
  });
  return card;
}
