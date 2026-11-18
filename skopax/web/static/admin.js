var ADMIN_OK = false, ADMIN_ROLE = 'admin', ADMIN_WANTED = false, ADMIN = null, ADMIN_HIST = null, ADMIN_SEQ = 0, ADMIN_FIND = '', ADMIN_TALK = null;
var ADMIN_NOTES = [], NOTES_SEQ = 0, FEATS = null, FEATS_SEQ = 0, AIKEY = null, AIKEY_SEQ = 0, AI_TESTING = false, CHANGES_SEQ = 0;
var FEAT_PENDING = {}, FEAT_HELD = {}, FEAT_SAID = {}, FEAT_HOLD_MS = 15000;
var TALK_NAMES = {'small-talk.enabled': 'Small talk replies', 'small-talk.hourly-cap': 'Small talk replies per hour', 'small-talk.rules': 'Small talk rules'};
var ADMIN_FOLD = {}, ADMIN_HIST_SHOW = 10, ADMIN_HIST_STEP = 20, ADMIN_HIST_MAX = 200, ADMIN_CLIP = 140, ADMIN_LINE_MAX = 1000;
var FEATURES_GROUP = 'Features', AI_GROUP = 'Investigator', SIGNIN_GROUP = 'Sign-in', MCP_GROUP = 'MCP', OTHER_SWITCHES = 'Other switches';
var ADMIN_REVEAL = null, ADMIN_HIT_MS = 2000;
var ADMIN_TITLES = {settings: 'General: the tunable settings', access: 'Access & sign-in: single sign-on, users, API tokens and MCP', integrations: 'Integrations: plug-ins and their instances',
  features: 'Features: core features to switch on or off', ai: 'Investigator: the Anthropic API key and the investigator settings', changes: 'Changes: what was changed in Admin, with Revert'};
function markAdminTab(id, on) {
  var tab = document.getElementById('tab-' + id);
  if (!tab) return;
  tab.hidden = !on;
  tab.setAttribute('aria-label', ADMIN_LABELS[id] || id);
  if (on) { tab.removeAttribute('aria-disabled'); tab.title = ADMIN_TITLES[id] || ''; return; }
  tab.setAttribute('aria-disabled', 'true');
  tab.title = 'Admins only: needs the ' + ADMIN_ROLE + ' role';
  var c = document.getElementById('count-' + id);
  if (c) c.textContent = '\u2013';
}
ADMIN_TABS.forEach(function (id) { markAdminTab(id, false); });
function setAdminAccess(on) {
  ADMIN_OK = !!on;
  ADMIN_TABS.forEach(function (id) { markAdminTab(id, ADMIN_OK); });
  if (typeof renderMeta === 'function') renderMeta();
  if (!ADMIN_OK) {
    ADMIN = null;
    ACCESS = null;
    ACCESS_N = null;
    SSO = null;
    INTEG = null;
    FEATS = null;
    FEAT_PENDING = {};
    FEAT_HELD = {};
    FEAT_SAID = {};
    AIKEY = null;
    NOTES_SEQ++;
    adminNotes([]);
    if (ADMIN_TABS.indexOf(curTab) >= 0) showTab('anoms');
    return;
  }
  if (typeof sideAdmin === 'function') sideAdmin();
  if (ADMIN_WANTED) { var wanted = ADMIN_WANTED; ADMIN_WANTED = false; showTab(wanted); }
  else if (ADMIN_TABS.indexOf(curTab) >= 0) adminShow(curTab);
  else adminNotices();
}
function adminShow(id) {
  if (!ADMIN_OK || ADMIN_TABS.indexOf(id) < 0) return;
  save(ADMIN_KEY, id);
  if (typeof sideAdmin === 'function') sideAdmin();
  adminNotices();
  if (id === 'settings') loadAdmin();
  else if (id === 'access') loadAccess();
  else if (id === 'integrations') loadIntegrations();
  else if (id === 'features') loadFeatures();
  else if (id === 'ai') loadAi();
  else loadChanges();
  adminCounts(id);
}
async function adminApi(method, path, body) {
  var headers = {'Accept': 'application/json'};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  var r = await fetch(path, {method: method, headers: headers, credentials: 'same-origin', body: body === undefined ? undefined : JSON.stringify(body)});
  if (!r.ok) {
    var detail = null, field = null;
    try { var failure = await r.json(); detail = failure.detail; field = failure.field; } catch (e) {}
    if (Array.isArray(detail)) detail = detail.map(function (d) { return d && d.msg; }).filter(Boolean).join('; ');
    throw {code: r.status, detail: typeof detail === 'string' && detail ? detail : null, field: typeof field === 'string' ? field : null};
  }
  if (r.status === 204) return null;
  return r.json();
}
function adminNone() { return null; }
function adminDenied(e) { return !!e && (e.code === 401 || e.code === 403); }
function adminFailed(box, what) {
  if (box) box.innerHTML = '<p class="muted">' + (what === null ? 'Admins only: log in with the ' + esc(ADMIN_ROLE) + ' role.' : esc(what)) + '</p>';
}
function adminItems() { return ADMIN && Array.isArray(ADMIN.items) ? ADMIN.items : []; }
function featItems() { return FEATS ? FEATS.items : []; }
function featKeys() { return featItems().map(function (f) { return f.key; }); }
function generalOf(items) {
  var keys = featKeys();
  return items.filter(function (i) { return [FEATURES_GROUP, AI_GROUP, SIGNIN_GROUP, MCP_GROUP].indexOf(i.group) < 0 && keys.indexOf(i.key) < 0; });
}
function otherSwitches() {
  var keys = featKeys();
  return adminItems().filter(function (i) { return i.group === FEATURES_GROUP && keys.indexOf(i.key) < 0; });
}
function aiItems() { return adminItems().filter(function (i) { return i.group === AI_GROUP; }); }
function featValid(f) { return !!f && typeof f.id === 'string' && typeof f.key === 'string'; }
function featHeld(f) {
  var held = FEAT_HELD[f.id];
  if (!held) return f;
  if (Date.now() >= held.until || f.stored === held.item.stored) { delete FEAT_HELD[f.id]; return f; }
  return held.item;
}
function setFeats(data) {
  var items = data && Array.isArray(data.items) ? data.items : [];
  FEATS = {edition: data && data.edition, items: items.filter(featValid).map(featHeld)};
  markFeatCount(FEATS);
  markAdminCount();
}
async function adminCounts(id) {
  var settings = !ADMIN && (id === 'access' || id === 'integrations'), feats = !FEATS && id !== 'features' && id !== 'settings', plugins = !INTEG && id !== 'integrations', users = ACCESS_N === null && !ACCESS && id !== 'access';
  if (users) ACCESS_N = -1;
  var r = await Promise.all([settings ? adminApi('GET', '/api/admin/settings').catch(adminNone) : null, feats ? adminApi('GET', '/api/admin/features').catch(adminNone) : null,
    plugins ? adminApi('GET', '/api/admin/plugins').catch(adminNone) : null, users ? adminApi('GET', '/api/admin/users').catch(adminNone) : null]);
  if (!ADMIN_OK) return;
  if (r[0] && !ADMIN) ADMIN = r[0];
  if (r[1] && !FEATS) setFeats(r[1]);
  if (r[2] && !INTEG) INTEG = r[2];
  if (users && !ACCESS) { if (Array.isArray(r[3])) markAccessCount(r[3].length); else ACCESS_N = null; }
  markAdminCount();
  markIntegCount(INTEG);
}
function markAccessCount(n) {
  ACCESS_N = n;
  var c = document.getElementById('count-access');
  if (c) c.textContent = String(n);
}
function markAdminCount(data) {
  var c = document.getElementById('count-settings'), d = data || ADMIN;
  if (c && d && Array.isArray(d.items)) c.textContent = String(generalOf(d.items).filter(function (i) { return i.source === 'admin'; }).length);
}
function markFeatCount(data) {
  var c = document.getElementById('count-features'), items = data && Array.isArray(data.items) ? data.items : null;
  if (c && items) c.textContent = items.filter(function (f) { return f && f.on === true; }).length + '/' + items.length;
}
function markIntegCount(data) {
  var c = document.getElementById('count-integrations'), list = data && Array.isArray(data.plugins) ? data.plugins : null;
  if (c && list) c.textContent = String(list.filter(intConnected).length);
}
async function adminNotices() {
  var seq = ++NOTES_SEQ;
  try {
    var r = await adminApi('GET', '/api/admin/notices');
    if (seq !== NOTES_SEQ || !ADMIN_OK) return;
    adminNotes(r && Array.isArray(r.items) ? r.items : []);
  } catch (e) {}
}
function adminNotes(list) {
  ADMIN_NOTES = list.filter(function (n) { return n && typeof n.text === 'string' && n.text; });
  var box = document.getElementById('adm-notices');
  if (box) {
    box.textContent = '';
    ADMIN_NOTES.forEach(function (n) {
      var p = accText('p', 'adm-notice ' + (n.level === 'warn' ? 'k-warn' : 'k-info'));
      if (typeof n.id === 'string') p.dataset.id = n.id;
      p.appendChild(accText('span', 'adm-notice-t', n.text));
      if (typeof n.at === 'string' && n.at) p.appendChild(accText('span', 'muted adm-notice-at', dayhm(n.at)));
      box.appendChild(p);
    });
    box.hidden = !ADMIN_NOTES.length;
  }
  if (typeof sideAdmin === 'function') sideAdmin();
}
async function loadAdmin() {
  var seq = ++ADMIN_SEQ, box = document.getElementById('admin');
  try {
    var r = await Promise.all([adminApi('GET', '/api/admin/settings'), adminApi('GET', '/api/admin/small-talk').catch(adminNone), adminApi('GET', '/api/admin/features').catch(adminNone)]);
    if (seq !== ADMIN_SEQ) return;
    ADMIN = r[0]; ADMIN_TALK = r[1];
    if (r[2]) setFeats(r[2]);
    renderAdmin();
  } catch (e) {
    if (seq !== ADMIN_SEQ) return;
    adminFailed(box, adminDenied(e) ? null : 'Settings did not load; open the tab again.');
  }
}
async function loadFeatures() {
  var seq = ++FEATS_SEQ, box = document.getElementById('features');
  FEAT_SAID = {};
  try {
    var r = await Promise.all([adminApi('GET', '/api/admin/features'), adminApi('GET', '/api/admin/settings').catch(adminNone)]);
    if (seq !== FEATS_SEQ) return;
    setFeats(r[0]);
    if (r[1]) ADMIN = r[1];
    markAdminCount();
    renderFeatures();
    renderFeatOther();
    adminReveal('features');
  } catch (e) {
    if (seq !== FEATS_SEQ) return;
    adminUnreveal('features');
    adminFailed(box, adminDenied(e) ? null : 'Features did not load; open the tab again.');
  }
}
async function loadAi(said) {
  var seq = ++AIKEY_SEQ, box = document.getElementById('ai');
  try {
    var r = await Promise.all([adminApi('GET', '/api/admin/anthropic-key'), adminApi('GET', '/api/admin/settings').catch(adminNone)]);
    if (seq !== AIKEY_SEQ) return;
    AIKEY = keyStatus(r[0]);
    if (r[1]) ADMIN = r[1];
    markAdminCount();
    paintAiKey(said);
    renderAiRows();
    adminReveal('ai');
  } catch (e) {
    if (seq !== AIKEY_SEQ) return;
    adminUnreveal('ai');
    adminFailed(box, adminDenied(e) ? null : 'The key did not load; open the tab again.');
  }
}
async function loadChanges() {
  var seq = ++CHANGES_SEQ, box = document.getElementById('changes');
  try {
    var r = await Promise.all([adminApi('GET', '/api/admin/settings'), adminApi('GET', '/api/admin/history?limit=' + ADMIN_HIST_MAX)]);
    if (seq !== CHANGES_SEQ) return;
    ADMIN = r[0]; ADMIN_HIST = Array.isArray(r[1]) ? r[1] : [];
    markAdminCount();
    renderChanges();
  } catch (e) {
    if (seq !== CHANGES_SEQ) return;
    adminFailed(box, adminDenied(e) ? null : 'Changes did not load; open the tab again.');
  }
}
function adminSource(i) {
  if (i.source === 'admin') return '<span class="tag k-act" title="' + esc('Set on this page by ' + draftBy(i.by) + ' ' + dayhm(i.at) + (i.base !== i.value ? '\nWithout it: ' + (i.base || 'empty') : '')) + '">Set by ' + esc(draftBy(i.by)) + ' ' + esc(dayhm(i.at)) + '</span>';
  if (i.source === 'config') return '<span class="tag plain" title="From the service configuration; a value saved here wins over it">config</span>';
  return '<span class="tag plain" title="The built-in default">default</span>';
}
function adminId(key) { return 'adm-' + key.replace(/[^a-z0-9]/gi, '-'); }
function adminControl(i) {
  var id = adminId(i.key), named = ' aria-labelledby="' + esc(id + '-l') + '"';
  if (i.kind === 'choice') return '<select id="' + esc(id) + '" class="adm-in"' + named + '>' + i.choices.map(function (c) { return '<option value="' + esc(c) + '"' + (c === i.value ? ' selected' : '') + '>' + esc(c) + '</option>'; }).join('') + '</select>';
  if (i.kind === 'bool') return '<select id="' + esc(id) + '" class="adm-in"' + named + '><option value="true"' + (i.value === 'true' ? ' selected' : '') + '>true</option><option value="false"' + (i.value === 'false' ? ' selected' : '') + '>false</option></select>';
  if (i.kind === 'int' || i.kind === 'float') return '<input id="' + esc(id) + '" class="adm-in"' + named + ' type="number" inputmode="decimal" step="' + (i.kind === 'int' ? '1' : 'any') + '" min="' + esc(i.low) + '" max="' + esc(i.high) + '" value="' + esc(i.value) + '">';
  if (i.kind === 'rules') {
    return '<table class="adm-rules"><tbody>' + Object.keys(i.defaults).map(function (k) {
      var v = i.rules[k], d = i.defaults[k];
      var tip = (i.ruleHelp || {})[k];
      return '<tr><th scope="row"><code>' + esc(k) + '</code>' + (tip ? '<span class="adm-rule-q" tabindex="0" role="img" aria-label="' + esc('What ' + k + ' is for: ' + tip) + '" title="' + esc(tip) + '">?</span>' : '') + '</th><td><input class="adm-rule" type="number" step="any" min="0" data-rule="' + esc(k) + '" value="' + esc(v) + '" aria-label="' + esc(i.label + ' ' + k) + '"></td>' +
        '<td class="muted">' + (v !== d ? 'default ' + esc(d) : '') + '</td></tr>';
    }).join('') + '</tbody></table>';
  }
  if (i.kind === 'text' && i.limit > ADMIN_LINE_MAX) return '<textarea id="' + esc(id) + '" class="adm-in adm-long"' + named + ' rows="8" spellcheck="false" maxlength="' + esc(i.limit) + '" placeholder="' + esc(i.default != null ? 'default: ' + (i.default || 'empty') : '') + '">' + esc(i.value) + '</textarea>';
  return '<input id="' + esc(id) + '" class="adm-in"' + named + ' type="text"' + (i.limit ? ' maxlength="' + esc(i.limit) + '"' : '') + ' value="' + esc(i.value) + '" placeholder="' + esc(i.default != null ? 'default: ' + (i.default || 'empty') : '') + '">';
}
function rowId(row) { return row.dataset.key || row.dataset.secret || row.dataset.route || row.dataset.source || row.dataset.talk || (row.classList.contains('route-add') ? '+route' : ''); }
function unsaved(box, saved) {
  var out = [];
  box.querySelectorAll('.adm-row').forEach(function (row) {
    var id = rowId(row);
    if (!id || id === saved) return;
    row.querySelectorAll('input:not(.r-link), select, textarea').forEach(function (inp) {
      var was = inp.tagName === 'SELECT' ? ([].filter.call(inp.options, function (o) { return o.defaultSelected; })[0] || inp.options[0] || {}).value : inp.defaultValue, on = inp === document.activeElement;
      if (inp.value !== was || on) out.push({id: id, sel: '.' + inp.className.trim().split(/\s+/).join('.') + (inp.dataset.rule ? '[data-rule="' + inp.dataset.rule + '"]' : ''), value: inp.value, on: on});
    });
  });
  return out;
}
function restoreUnsaved(box, list) {
  list.forEach(function (u) {
    var row = [].filter.call(box.querySelectorAll('.adm-row'), function (r) { return rowId(r) === u.id; })[0], inp = row && row.querySelector(u.sel);
    if (!inp || inp.disabled) return;
    if (inp.value !== u.value) { inp.value = u.value; inp.dispatchEvent(new Event('input')); }
    var form = inp.closest('.route-form');
    if (form) form.hidden = false;
    if (u.on) inp.focus();
  });
}
function adminValue(row, i) {
  if (i.kind !== 'rules') return row.querySelector('.adm-in').value;
  var out = {};
  row.querySelectorAll('.adm-rule').forEach(function (inp) {
    var k = inp.dataset.rule, v = inp.value.trim();
    if (v !== '' && Number(v) !== i.defaults[k]) out[k] = Number(v);
  });
  return Object.keys(out).length ? JSON.stringify(out) : '';
}
function adminRow(i) {
  var row = el('div', 'adm-row' + (i.source === 'admin' ? ' set' : ''));
  row.dataset.key = i.key;
  row.dataset.find = (i.label + ' ' + i.key + ' ' + i.help + ' ' + Object.keys(i.ruleHelp || {}).map(function (k) { return k + ' ' + i.ruleHelp[k]; }).join(' ')).toLowerCase();
  row.innerHTML = '<div class="adm-l"><div class="adm-n"><b id="' + esc(adminId(i.key) + '-l') + '">' + esc(i.label) + '</b>' + adminSource(i) +
    (i.live ? '' : '<span class="tag k-warn" title="Read when the service starts: saved now, used after the next restart">after restart</span>') + '</div>' +
    '<p class="adm-help">' + esc(i.help) + '</p><code class="adm-key">' + esc(i.key) + '</code></div>' +
    '<div class="adm-c">' + adminControl(i) + (i.pluginTerms && i.pluginTerms.length ? '<p class="adm-help adm-plugin-terms">From plug-ins, not saved here: ' + i.pluginTerms.map(function (t) { return '<code>' + esc(t) + '</code>'; }).join(' ') + '</p>' : '') + '<div class="adm-acts"><button type="button" class="btn small adm-save" disabled>Save</button>' +
    (i.source === 'admin' ? '<button type="button" class="btn sec small adm-reset" title="' + esc('Drop the value set here and go back to ' + (i.base ? i.base : 'the default')) + '">Reset</button>' : '') +
    '<span class="adm-msg" role="status"></span></div></div>';
  if (i.problem) {
    var note = el('p', 'adm-help adm-problem');
    note.textContent = i.problem;
    row.querySelector('.adm-l').appendChild(note);
  }
  var save = row.querySelector('.adm-save'), msg = row.querySelector('.adm-msg');
  row.querySelectorAll('input, select, textarea').forEach(function (inp) { inp.addEventListener('input', function () { save.disabled = adminValue(row, i) === adminCurrent(i); msg.textContent = ''; }); });
  save.addEventListener('click', function () { adminSave(i, adminValue(row, i), save, msg); });
  var reset = row.querySelector('.adm-reset');
  if (reset) reset.addEventListener('click', function () { adminReset(i, reset, msg); });
  return row;
}
function rulesDiff(i) {
  var out = {};
  Object.keys(i.defaults).forEach(function (k) { if (i.rules[k] !== i.defaults[k]) out[k] = i.rules[k]; });
  return out;
}
function adminCurrent(i) {
  if (i.kind !== 'rules') return i.value;
  var d = rulesDiff(i);
  return Object.keys(d).length ? JSON.stringify(d) : '';
}
async function adminSave(i, value, b, msg) {
  b.disabled = true;
  msg.textContent = 'Saving\u2026';
  msg.className = 'adm-msg';
  try {
    var r = await adminApi('PUT', '/api/admin/settings', {key: i.key, value: value});
    adminDone(r, r.item.live ? 'Saved, live within ' + ((ADMIN && ADMIN.refreshSeconds) || 15) + ' s on every pod' : 'Saved, used after the next restart');
  } catch (e) {
    msg.textContent = e && e.detail ? e.detail : 'Not saved, try again';
    msg.className = 'adm-msg bad';
    b.disabled = false;
  }
}
async function adminReset(i, b, msg) {
  b.disabled = true;
  try {
    var r = await adminApi('DELETE', '/api/admin/settings?key=' + encodeURIComponent(i.key));
    adminDone(r, 'Reset to ' + (r.item.value || 'empty'));
  } catch (e) {
    msg.textContent = e && e.detail ? e.detail : 'Not reset, try again';
    msg.className = 'adm-msg bad';
    b.disabled = false;
  }
}
function adminHistory() {
  if (!ADMIN_HIST.length) return '<p class="muted">No changes made in Admin yet.</p>';
  var label = {};
  adminItems().forEach(function (i) { label[i.key] = i.label; });
  var shown = function (v) {
    if (v == null) return '<span class="muted">config / default</span>';
    if (v === '') return '<span class="muted">empty</span>';
    var t = String(v);
    return t.length > ADMIN_CLIP ? '<code class="adm-clip" title="' + esc(t) + '">' + esc(t.slice(0, ADMIN_CLIP - 1)) + '…</code>' : '<code>' + esc(t) + '</code>';
  };
  var rest = ADMIN_HIST.length - ADMIN_HIST_SHOW;
  return '<table class="adm-hist"><thead><tr><th>When</th><th>Who</th><th>Setting</th><th>Change</th><th></th></tr></thead><tbody>' + ADMIN_HIST.slice(0, ADMIN_HIST_SHOW).map(function (h) {
    var name = h.route ? 'Teams route ' + h.key.slice(6) : h.source ? 'Agent ' + h.key.slice(7) : h.secret ? 'Secret ' + h.key : h.talk ? TALK_NAMES[h.key] || 'Small talk' : label[h.key] || h.key;
    var shownR = function (v) { return v == null ? '<span class="muted">' + (h.route ? 'none' : h.source || h.secret ? 'not set' : 'config / default') + '</span>' : shown(v); };
    var change = h.seen ? '<span class="muted">workflow link shown</span>' : shownR(h.old) + ' → ' + shownR(h.new);
    return '<tr><td>' + esc(dayhm(h.at)) + '</td><td>' + esc(draftBy(h.by)) + '</td><td title="' + esc(h.key) + '">' + esc(name) + '</td><td class="adm-ch">' + change + '</td>' +
      '<td>' + (h.route || h.source || h.secret || h.talk ? '' : '<button type="button" class="btn sec small adm-revert" data-id="' + esc(h.id) + '" title="Put back the value before this change">Revert</button>') + '</td></tr>';
  }).join('') + '</tbody></table>' + (rest > 0 ? '<p class="adm-hist-more"><button type="button" class="btn sec small adm-more">Show ' + Math.min(rest, ADMIN_HIST_STEP) + ' more</button><span class="muted">' + Math.min(ADMIN_HIST_SHOW, ADMIN_HIST.length) + ' of ' + ADMIN_HIST.length + '</span></p>' : ADMIN_HIST.length >= ADMIN_HIST_MAX ? '<p class="muted adm-hist-more">The latest ' + ADMIN_HIST_MAX + ' changes; older ones stay stored.</p>' : '');
}
async function adminRevert(h, b) {
  b.disabled = true;
  try {
    var r = h.old == null ? await adminApi('DELETE', '/api/admin/settings?key=' + encodeURIComponent(h.key)) : await adminApi('PUT', '/api/admin/settings', {key: h.key, value: h.old});
    adminDone(r, 'Reverted', true);
  } catch (e) {
    b.textContent = e && e.detail ? e.detail : 'Not reverted';
  }
}
function talkGroup() {
  var t = ADMIN_TALK, name = 'Small talk';
  var d = el('details', 'adm-grp adm-talk');
  d.dataset.group = name;
  d.appendChild(el('summary', 'chev', '<h3>' + name + '</h3><span class="count">3</span>' + (t.custom ? '<span class="tag k-act">set here</span>' : '')));
  var row = el('div', 'adm-row talk-row' + (t.custom ? ' set' : ''));
  row.dataset.talk = 'small-talk';
  row.dataset.find = ('small talk smalltalk slack hello thanks rules replies per hour ' + (t.rules || '')).toLowerCase();
  var src = t.custom ? '<span class="tag k-act" title="' + esc('Set by ' + draftBy(t.updatedBy) + ' ' + dayhm(t.updatedAt)) + '">Set by ' + esc(draftBy(t.updatedBy)) + ' ' + esc(dayhm(t.updatedAt)) + '</span>' : '<span class="tag plain" title="The built-in rules">default</span>';
  row.innerHTML = '<div><div class="adm-n"><b>Small talk replies</b>' + src + '</div>' +
    '<p class="adm-help">When someone writes to ' + esc(brandName()) + ' in Slack without a prod question (hello, thanks, a joke), the gate model answers with one short message that follows these rules. The hourly limit counts replies from everyone together.</p>' +
    '<p class="adm-key">' + esc(t.usedThisHour) + ' of ' + esc(t.hourlyCap) + ' replies used this hour</p></div>' +
    '<div class="adm-c talk-c"><label class="talk-f">Replies<select class="adm-in talk-on"><option value="on">On</option><option value="off">Off</option></select></label>' +
    '<label class="talk-f">Replies per hour<input type="number" class="adm-in talk-cap" min="0" max="60" step="1"></label>' +
    '<label class="talk-f">Rules<textarea class="adm-in talk-rules" maxlength="2000" rows="10" spellcheck="false"></textarea></label>' +
    '<div class="adm-acts"><button type="button" class="btn small talk-save" disabled>Save</button>' + (t.custom ? '<button type="button" class="btn sec small talk-reset">Back to default</button>' : '') + '<span class="adm-msg" role="status"></span></div></div>';
  var on = row.querySelector('.talk-on'), cap = row.querySelector('.talk-cap'), rules = row.querySelector('.talk-rules');
  var saveBtn = row.querySelector('.talk-save'), reset = row.querySelector('.talk-reset'), msg = row.querySelector('.adm-msg');
  on.querySelector('option[value="' + (t.enabled ? 'on' : 'off') + '"]').defaultSelected = true;
  on.value = t.enabled ? 'on' : 'off';
  cap.defaultValue = cap.value = String(t.hourlyCap);
  rules.defaultValue = rules.value = t.rules || '';
  var value = function () { return {enabled: on.value === 'on', hourlyCap: Number(cap.value), rules: rules.value.trim()}; };
  var valid = function () { var v = value(); return /^\d+$/.test(cap.value.trim()) && v.hourlyCap <= 60 && v.rules.length >= 1 && v.rules.length <= 2000; };
  var changed = function () { var v = value(); return v.enabled !== t.enabled || v.hourlyCap !== t.hourlyCap || v.rules !== t.rules; };
  [on, cap, rules].forEach(function (inp) {
    inp.addEventListener('input', function () {
      saveBtn.disabled = !valid() || !changed();
      msg.textContent = valid() ? '' : 'Rules need 1 to 2000 characters and replies per hour 0 to 60';
      msg.className = 'adm-msg' + (valid() ? '' : ' bad');
    });
  });
  saveBtn.addEventListener('click', function () { talkCall(saveBtn, msg, function () { return adminApi('PUT', '/api/admin/small-talk', value()); }, 'Saved'); });
  if (reset) reset.addEventListener('click', function () {
    if (!reset.dataset.confirm) { reset.dataset.confirm = '1'; reset.textContent = 'Back to default?'; setTimeout(function () { delete reset.dataset.confirm; reset.textContent = 'Back to default'; }, 4000); return; }
    talkCall(reset, msg, function () { return adminApi('DELETE', '/api/admin/small-talk'); }, 'Back to the default');
  });
  d.appendChild(row);
  d.open = ADMIN_FIND ? true : ADMIN_FOLD[name] === 1;
  d.addEventListener('toggle', function () {
    if (ADMIN_FIND) return;
    ADMIN_FOLD[name] = d.open ? 1 : 0;
  });
  return d;
}
async function talkCall(b, msg, run, said) {
  b.disabled = true;
  try {
    ADMIN_TALK = await run();
    try { ADMIN_HIST = await adminApi('GET', '/api/admin/history?limit=' + ADMIN_HIST_MAX); } catch (e) {}
    adminPaintAll('small-talk');
    var m = document.querySelector('#admin .talk-row .adm-msg');
    if (m) { m.textContent = said; m.className = 'adm-msg ok'; }
  } catch (e) {
    b.disabled = false;
    msg.textContent = e && e.detail ? e.detail : 'Not saved, try again';
    msg.className = 'adm-msg bad';
  }
}
function adminFilter() {
  document.querySelectorAll('#admin .adm-grp').forEach(function (d) {
    var n = 0;
    d.querySelectorAll('.adm-row').forEach(function (r) { var on = !ADMIN_FIND || r.dataset.find.indexOf(ADMIN_FIND) >= 0; r.hidden = !on; if (on) n++; });
    d.hidden = !n;
    if (ADMIN_FIND && n) d.open = true;
    else if (!ADMIN_FIND && d.dataset.group) d.open = ADMIN_FOLD[d.dataset.group] === 1;
  });
}

function adminRowEl(key) {
  return document.querySelector(['#admin', '#feat-other', '#ai-rows', '#acc-mcp'].map(function (box) { return box + ' .adm-row[data-key="' + key + '"]'; }).join(', '));
}
function adminKeep(box, saved, paint) {
  var keep = document.activeElement && box.contains(document.activeElement) ? document.activeElement.id : null, edits = saved ? unsaved(box, saved) : [];
  paint();
  restoreUnsaved(box, edits);
  if (keep && document.getElementById(keep)) document.getElementById(keep).focus();
}
function adminPaintAll(saved) {
  renderAdmin(saved);
  renderFeatOther(saved);
  renderAiRows(saved);
  renderMcpRows(saved);
  renderChanges();
}
async function adminDone(r, said, reverted) {
  ADMIN.items = ADMIN.items.map(function (x) { return x.key === r.item.key ? r.item : x; });
  try { ADMIN_HIST = await adminApi('GET', '/api/admin/history?limit=' + ADMIN_HIST_MAX); } catch (e) {}
  adminPaintAll(r.item.key);
  var row = adminRowEl(r.item.key);
  if (row) { var m = row.querySelector('.adm-msg'); m.textContent = said; m.className = 'adm-msg ok'; }
  var note = reverted && document.getElementById('adm-ch-msg');
  if (note) { note.textContent = said + ' ' + (r.item.label || r.item.key); note.className = 'adm-msg ok'; }
}
function renderChanges() {
  var box = document.getElementById('changes');
  if (!box || !ADMIN || !Array.isArray(ADMIN_HIST)) return;
  var n = ADMIN_HIST.length;
  box.textContent = '';
  var head = accText('div', 'adm-head');
  head.appendChild(accText('span', 'muted', n + (n >= ADMIN_HIST_MAX ? '+' : '') + (n === 1 ? ' change' : ' changes')));
  var note = accText('span', 'adm-msg');
  note.id = 'adm-ch-msg';
  note.setAttribute('role', 'status');
  head.appendChild(note);
  box.appendChild(head);
  box.appendChild(el('div', 'adm-hist-body', adminHistory()));
  box.querySelectorAll('.adm-revert').forEach(function (b) {
    var h = ADMIN_HIST.filter(function (x) { return x.id === b.dataset.id; })[0];
    b.addEventListener('click', function () { adminRevert(h, b); });
  });
  var more = box.querySelector('.adm-more');
  if (more) more.addEventListener('click', function () {
    ADMIN_HIST_SHOW += ADMIN_HIST_STEP;
    renderChanges();
    var next = box.querySelectorAll('.adm-hist tbody tr')[ADMIN_HIST_SHOW - ADMIN_HIST_STEP];
    if (next) { next.tabIndex = -1; next.focus(); }
  });
}
function renderAdmin(saved) {
  var box = document.getElementById('admin');
  if (!box || !ADMIN) return;
  adminKeep(box, saved, function () {
    var items = generalOf(adminItems());
    box.innerHTML = '';
    var head = el('div', 'adm-head');
    head.innerHTML = '<input type="search" id="adm-find" placeholder="Find a setting" aria-label="Find a setting" value="' + esc(ADMIN_FIND) + '">' +
      '<span class="muted">' + items.filter(function (i) { return i.source === 'admin'; }).length + ' set here \u00b7 ' + items.length + ' settings</span>';
    box.appendChild(head);
    var groups = [], by = {};
    items.forEach(function (i) { if (!by[i.group]) { by[i.group] = []; groups.push(i.group); } by[i.group].push(i); });
    groups.forEach(function (g) {
      var d = el('details', 'adm-grp');
      d.dataset.group = g;
      var set = by[g].filter(function (i) { return i.source === 'admin'; }).length;
      d.appendChild(el('summary', 'chev', '<h3>' + esc(g) + '</h3><span class="count">' + by[g].length + '</span>' + (set ? '<span class="tag k-act">' + set + ' set here</span>' : '')));
      by[g].forEach(function (i) { d.appendChild(adminRow(i)); });
      d.open = ADMIN_FIND ? true : ADMIN_FOLD[g] === 1;
      d.addEventListener('toggle', function () {
        if (ADMIN_FIND) return;
        ADMIN_FOLD[g] = d.open ? 1 : 0;
      });
      box.appendChild(d);
    });
    if (ADMIN_TALK) box.appendChild(talkGroup());
    var find = document.getElementById('adm-find');
    find.addEventListener('input', function () { ADMIN_FIND = find.value.trim().toLowerCase(); adminFilter(); });
    adminFilter();
    markAdminCount();
  });
}
function renderFeatOther(saved) {
  var box = document.getElementById('feat-other');
  if (!box || !ADMIN || !FEATS) return;
  adminKeep(box, saved, function () {
    var items = otherSwitches();
    box.textContent = '';
    if (!items.length) return;
    var d = el('details', 'adm-grp feat-other');
    d.dataset.group = OTHER_SWITCHES;
    d.appendChild(el('summary', 'chev', '<h3>' + OTHER_SWITCHES + '</h3><span class="count">' + items.length + '</span>'));
    items.forEach(function (i) { d.appendChild(adminRow(i)); });
    d.open = ADMIN_FOLD[OTHER_SWITCHES] === 1;
    d.addEventListener('toggle', function () { ADMIN_FOLD[OTHER_SWITCHES] = d.open ? 1 : 0; });
    box.appendChild(d);
  });
}
function renderAiRows(saved) {
  var box = document.getElementById('ai-rows');
  if (!box || !ADMIN) return;
  adminKeep(box, saved, function () {
    var items = aiItems();
    box.textContent = '';
    if (!items.length) return;
    var s = el('section', 'adm-grp ai-grp');
    s.dataset.group = AI_GROUP;
    s.appendChild(accText('h3', null, 'Investigator settings'));
    items.forEach(function (i) { s.appendChild(adminRow(i)); });
    box.appendChild(s);
  });
}
function adminUnreveal(tab) { if (ADMIN_REVEAL && ADMIN_REVEAL.tab === tab) ADMIN_REVEAL = null; }
function adminReveal(tab) {
  var want = ADMIN_REVEAL;
  if (!want || want.tab !== tab) return;
  ADMIN_REVEAL = null;
  var panel = document.getElementById('panel-' + tab), f = tab === 'features' ? featItems().filter(function (x) { return x.key === want.key; })[0] : null;
  if (!panel || panel.hidden) return;
  var hit = [].filter.call(panel.querySelectorAll(f ? '.feat' : tab === 'access' ? '.adm-row, .sso-f, .sso-sw' : '.adm-row'), function (n) { return f ? n.dataset.id === f.id : n.dataset.key === want.key; })[0];
  if (!hit) return;
  var fold = hit.closest('details');
  if (fold) fold.open = true;
  hit.classList.add('adm-hit');
  setTimeout(function () { hit.classList.remove('adm-hit'); }, ADMIN_HIT_MS);
  hit.scrollIntoView({block: 'center'});
  hit.tabIndex = -1;
  hit.focus({preventScroll: true});
}
function featById(id) { return featItems().filter(function (f) { return f.id === id; })[0]; }
function featCard(f) {
  var card = accText('article', 'feat' + (f.locked ? ' locked' : ''));
  card.dataset.id = f.id;
  card.appendChild(el('span', 'feat-i', navIcon(String(f.icon || ''))));
  var t = accText('div', 'feat-t'), name = accText('div', 'feat-n');
  name.appendChild(accText('b', 'feat-l', f.label));
  if (f.locked) name.appendChild(accText('span', 'tag k-act feat-ed', String(f.edition || '').charAt(0).toUpperCase() + String(f.edition || '').slice(1)));
  t.appendChild(name);
  if (f.help) t.appendChild(accText('p', 'feat-h', f.help));
  var needs = Array.isArray(f.needs) ? f.needs.filter(function (x) { return typeof x === 'string' && x; }) : [];
  if (needs.length) {
    var list = accText('div', 'feat-needs');
    needs.forEach(function (x) { list.appendChild(accText('span', 'feat-need', x)); });
    t.appendChild(list);
  }
  if (typeof f.restart === 'string' && f.restart) t.appendChild(accText('p', 'feat-restart', f.restart));
  var clearable = f.locked && f.stored === true;
  if (clearable) t.appendChild(accText('p', 'feat-stale', 'Still saved as on from an earlier edition, where it has no effect: switch it off to clear it.'));
  var said = FEAT_SAID[f.id], msg = accText('p', 'adm-msg feat-msg' + (said ? (said.bad ? ' bad' : ' ok') : ''), said ? said.text : '');
  msg.setAttribute('role', 'status');
  t.appendChild(msg);
  card.appendChild(t);
  var pending = FEAT_PENDING[f.id], shown = pending ? pending.want : f.locked ? f.stored === true : f.on === true;
  var sw = accText('button', 'feat-sw');
  sw.type = 'button';
  sw.id = 'feat-sw-' + f.id;
  sw.setAttribute('role', 'switch');
  sw.setAttribute('aria-checked', shown ? 'true' : 'false');
  sw.setAttribute('aria-label', f.label);
  if (pending) sw.setAttribute('aria-busy', 'true');
  if (f.locked) {
    if (!clearable) sw.setAttribute('aria-disabled', 'true');
    sw.title = typeof f.reason === 'string' ? f.reason : '';
  }
  sw.addEventListener('click', function () { featSwitch(f.id); });
  card.appendChild(sw);
  return card;
}
function renderFeatures() {
  var box = document.getElementById('features');
  if (!box || !FEATS) return;
  var keep = document.activeElement && box.contains(document.activeElement) ? document.activeElement.id : null;
  box.textContent = '';
  var grid = accText('div', 'feat-grid');
  featItems().forEach(function (f) { grid.appendChild(featCard(f)); });
  box.appendChild(grid);
  if (keep && document.getElementById(keep)) document.getElementById(keep).focus();
  markFeatCount(FEATS);
}
async function featSwitch(id) {
  var f = featById(id);
  if (!f || FEAT_PENDING[id] || (f.locked && f.stored !== true)) return;
  var body = f.locked ? {on: false} : {on: f.on !== true}, mine = {want: body.on};
  FEAT_PENDING[id] = mine;
  delete FEAT_SAID[id];
  renderFeatures();
  var item;
  try {
    item = await adminApi('PUT', '/api/admin/features/' + encodeURIComponent(id), body);
  } catch (e) {
    if (FEAT_PENDING[id] !== mine) return;
    delete FEAT_PENDING[id];
    FEAT_SAID[id] = {text: e && e.detail ? e.detail : 'Not saved, try again', bad: true};
    renderFeatures();
    return;
  }
  if (FEAT_PENDING[id] !== mine) return;
  delete FEAT_PENDING[id];
  if (!featValid(item) || item.id !== id) item = Object.assign({}, f, {on: body.on && !f.locked, stored: body.on});
  FEAT_HELD[id] = {item: item, until: Date.now() + FEAT_HOLD_MS};
  FEAT_SAID[id] = {text: body.on ? 'Turned on' : 'Turned off', bad: false};
  if (FEATS) FEATS.items = FEATS.items.map(function (x) { return x.id === id ? item : x; });
  renderFeatures();
  var seq = ++FEATS_SEQ, fresh = await adminApi('GET', '/api/admin/features').catch(adminNone);
  if (!fresh || seq !== FEATS_SEQ || !ADMIN_OK) return;
  setFeats(fresh);
  renderFeatures();
}
function keyStatus(r) {
  r = r || {};
  return {using: r.using === 'admin' || r.using === 'config' ? r.using : null, admin: r.admin === true, config: r.config === true,
    updatedBy: typeof r.updatedBy === 'string' ? r.updatedBy : null, updatedAt: typeof r.updatedAt === 'string' ? r.updatedAt : null, note: typeof r.note === 'string' ? r.note : ''};
}
function aiMsg() { return document.querySelector('#ai .ai-msg'); }
function aiSay(text, bad) {
  var msg = aiMsg();
  if (!msg) return;
  msg.textContent = text || '';
  msg.className = 'adm-msg ai-msg' + (bad ? ' bad' : text ? ' ok' : '');
}
function paintAiKey(said) {
  var box = document.getElementById('ai');
  if (!box || !AIKEY) return;
  var s = AIKEY;
  box.textContent = '';
  var list = accText('ul', 'ai-providers'), first = accText('li', 'ai-provider');
  var card = accText('section', 'ai-key');
  card.setAttribute('aria-labelledby', 'ai-key-h');
  var head = accText('div', 'ai-head'), h = accText('h3', null, 'Anthropic API key');
  h.id = 'ai-key-h';
  head.appendChild(h);
  head.appendChild(accText('span', 'tag ' + (s.using ? 'k-ok' : 'k-warn'), s.using === 'admin' ? 'set here' : s.using === 'config' ? 'from the configuration' : 'not set'));
  card.appendChild(head);
  card.appendChild(accText('p', 'ai-state', s.using === 'admin' ? 'Using the key set here' : s.using === 'config' ? s.note : 'No key'));
  if (s.admin && s.updatedBy) card.appendChild(accText('p', 'adm-help ai-by', 'Set by ' + draftBy(s.updatedBy) + (s.updatedAt ? ' ' + dayhm(s.updatedAt) : '')));
  var field = accText('div', 'ai-field'), label = accText('label', null, s.using ? 'New key' : 'Key');
  label.setAttribute('for', 'ai-key-in');
  var input = document.createElement('input');
  input.id = 'ai-key-in';
  input.type = 'text';
  input.maxLength = 400;
  input.setAttribute('autocomplete', 'off');
  input.setAttribute('spellcheck', 'false');
  input.setAttribute('autocapitalize', 'off');
  input.setAttribute('autocorrect', 'off');
  input.setAttribute('data-1p-ignore', '');
  input.setAttribute('data-lpignore', 'true');
  input.setAttribute('data-bwignore', '');
  input.setAttribute('data-form-type', 'other');
  field.appendChild(label);
  field.appendChild(input);
  card.appendChild(field);
  card.appendChild(accText('p', 'adm-help', 'Saved sealed and never shown again. Test connection tries the key typed here, or the key in use when the field is empty.'));
  var acts = accText('div', 'adm-acts');
  var saveB = accText('button', 'btn small ai-save', 'Save'), testB = accText('button', 'btn sec small ai-test', 'Test connection');
  saveB.type = testB.type = 'button';
  saveB.disabled = true;
  testB.disabled = AI_TESTING;
  acts.appendChild(saveB);
  acts.appendChild(testB);
  var remove = null;
  if (s.admin) {
    remove = accText('button', 'btn sec small ai-remove', 'Remove');
    remove.type = 'button';
    acts.appendChild(remove);
  }
  var msg = accText('span', 'adm-msg ai-msg');
  msg.setAttribute('role', 'status');
  acts.appendChild(msg);
  card.appendChild(acts);
  first.appendChild(card);
  list.appendChild(first);
  var soon = accText('li', 'ai-provider ai-soon');
  soon.setAttribute('aria-disabled', 'true');
  soon.appendChild(accText('span', 'ai-provider-name', 'OpenAI'));
  soon.appendChild(document.createTextNode(' '));
  soon.appendChild(accText('span', 'tag', 'Coming soon'));
  list.appendChild(soon);
  box.appendChild(list);
  if (said) aiSay(said);
  input.addEventListener('input', function () { saveB.disabled = !input.value.trim(); aiSay(''); });
  saveB.addEventListener('click', async function () {
    var key = input.value.trim();
    if (!key) return;
    saveB.disabled = true;
    aiSay('Saving\u2026');
    try {
      var r = await adminApi('PUT', '/api/admin/anthropic-key', {key: key});
      input.value = '';
      AIKEY = keyStatus(r);
      paintAiKey('Saved');
      adminNotices();
    } catch (e) {
      aiSay(e && e.detail ? e.detail : 'Not saved, try again', true);
      saveB.disabled = !input.value.trim();
    }
  });
  testB.addEventListener('click', async function () {
    if (AI_TESTING) return;
    var typed = input.value.trim();
    AI_TESTING = true;
    testB.disabled = true;
    aiSay('Testing\u2026');
    try {
      var r = await adminApi('POST', '/api/admin/anthropic-key/test', typed ? {key: typed} : {});
      aiSay((r && r.ok ? '\u2713 ' : '') + String((r && r.summary) || '') + ' \u00b7 ' + Number((r && r.ms) || 0) + ' ms', !(r && r.ok));
    } catch (e) {
      aiSay(e && e.detail ? e.detail : 'The test could not run', true);
    }
    AI_TESTING = false;
    var now = document.querySelector('#ai .ai-test');
    if (now) now.disabled = false;
  });
  if (remove) remove.addEventListener('click', async function () {
    var ask = window.__skopaxConfirm;
    if (typeof ask !== 'function') return;
    var ok = await ask('Remove the Anthropic API key?', s.config ? 'The key set here is deleted, and the key from the configuration is used again.' : 'The key set here is deleted. The investigator, the chat and the self-review have no key until one is set again.', 'Remove key');
    if (!ok) { if (remove.isConnected) remove.focus(); return; }
    remove.disabled = true;
    aiSay('Removing\u2026');
    try {
      await adminApi('DELETE', '/api/admin/anthropic-key');
    } catch (e) {
      remove.disabled = false;
      aiSay(e && e.detail ? e.detail : 'Not removed, try again', true);
      return;
    }
    adminNotices();
    loadAi('Removed');
  });
}

var ACCESS = null, ACCESS_SEQ = 0, ACCESS_N = null, ACCESS_ROLES = ['viewer', 'operator', 'admin'], ACCESS_IDP = ['claim', 'resolver'], ACCESS_LINKS = ['pending', 'bound', 'expired'];
function accText(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function accMsg(msg, text, bad) {
  msg.textContent = text || '';
  msg.className = 'adm-msg' + (bad ? ' bad' : text ? ' ok' : '');
}
function accError(e, fallback) { return e && e.detail ? e.detail : fallback; }
function accRoleSelect(value, cls, name) {
  var s = document.createElement('select');
  if (cls) s.className = cls;
  if (name) s.name = name;
  ACCESS_ROLES.forEach(function (r) {
    var o = document.createElement('option');
    o.value = r;
    o.textContent = r;
    s.appendChild(o);
  });
  s.value = value;
  return s;
}
function accField(label, input) {
  var l = document.createElement('label');
  l.appendChild(document.createTextNode(label));
  l.appendChild(input);
  return l;
}
function accInput(name, type, max, label) {
  var i = document.createElement('input');
  i.name = name;
  i.type = type;
  i.maxLength = max;
  i.required = true;
  i.setAttribute('aria-label', label);
  if (type === 'password') i.autocomplete = 'new-password';
  return i;
}
function accSection(title, count) {
  var s = accText('section', 'acc-sec');
  var h = accText('h3', null, title);
  if (count != null) h.appendChild(accText('span', 'count', String(count)));
  s.appendChild(h);
  return s;
}
function accTable(heads) {
  var wrap = accText('div', 'acc-wrap'), t = accText('table', 'acc-table'), tr = document.createElement('tr');
  heads.forEach(function (h) { tr.appendChild(accText('th', null, h)); });
  t.appendChild(document.createElement('thead')).appendChild(tr);
  t.appendChild(document.createElement('tbody'));
  wrap.appendChild(t);
  return {wrap: wrap, body: t.querySelector('tbody')};
}
function accCell(row, child) {
  var td = document.createElement('td');
  if (child != null) td.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  row.appendChild(td);
  return td;
}
async function loadAccess() {
  var seq = ++ACCESS_SEQ, box = document.getElementById('access');
  try {
    var r = await Promise.all([accFetch(), adminApi('GET', '/api/admin/sso').catch(adminNone), adminApi('GET', '/api/admin/settings').catch(adminNone)]);
    if (seq !== ACCESS_SEQ) return;
    ACCESS = r[0];
    SSO = r[1] ? ssoView(r[1]) : null;
    if (r[2]) ADMIN = r[2];
    MCP_FAILED = !r[2];
    markAdminCount();
    renderAccess();
    renderSso();
    renderMcpRows();
    adminReveal('access');
  } catch (e) {
    if (seq !== ACCESS_SEQ) return;
    adminUnreveal('access');
    box.textContent = '';
    box.appendChild(accText('p', 'muted', e && (e.code === 401 || e.code === 403) ? 'Admins only: log in with the ' + ADMIN_ROLE + ' role.' : 'Access did not load; open the tab again.'));
  }
}
async function accFetch() {
  var r = await Promise.all([adminApi('GET', '/api/admin/users'), adminApi('GET', '/api/admin/tokens')]);
  return {users: r[0], tokens: r[1]};
}
async function accReload() {
  try { ACCESS = await accFetch(); return true; } catch (e) { return false; }
}
function accStale(fresh) {
  var box = document.getElementById('acc-lists');
  if (fresh || !box) return;
  box.insertBefore(accText('p', 'adm-msg bad acc-stale', 'Could not refresh the list; open the tab again to see the latest.'), box.firstChild);
}
function accApply(change) {
  if (ACCESS) change(ACCESS);
}
function accRowMsg(kind, key) {
  var rows = document.querySelectorAll('#access .acc-' + kind);
  for (var i = 0; i < rows.length; i++) {
    if ((kind === 'user' ? rows[i].dataset.email : rows[i].dataset.id) === key) return rows[i].querySelector('.adm-msg');
  }
  return null;
}
function accFormMsg(cls) { return document.querySelector('#access .' + cls + ' .adm-msg'); }
function accLive(msg, fallback) { return msg && msg.isConnected ? msg : fallback; }
function accNote(msg, text, bad) {
  if (msg) accMsg(msg, text, bad);
}
function clearAccessSecret() {
  var slot = document.querySelector('#access .acc-secret-slot');
  if (slot) slot.textContent = '';
  var typed = document.getElementById('sso-in-client-secret');
  if (typed) typed.value = '';
}
function accShell(box) {
  var lists = document.getElementById('acc-lists');
  if (lists && box.contains(lists)) return lists;
  box.textContent = '';
  ['acc-sso', 'acc-lists', 'acc-mcp'].forEach(function (id) {
    var part = accText('div', id);
    part.id = id;
    box.appendChild(part);
  });
  return document.getElementById('acc-lists');
}
function renderAccess() {
  var box = document.getElementById('access');
  if (!box || !ACCESS) return;
  ACCESS_SEQ++;
  var lists = accShell(box);
  lists.textContent = '';
  box = lists;
  var users = accSection('Users', ACCESS.users.length);
  var ut = accTable(['Email', 'Name', 'Role', 'Source', 'Disabled', 'SSO link', '']);
  ACCESS.users.forEach(function (u) { ut.body.appendChild(accUserRow(u)); });
  users.appendChild(ut.wrap);
  users.appendChild(accAddUser());
  box.appendChild(users);
  var tokens = accSection('API tokens', ACCESS.tokens.length);
  tokens.appendChild(accText('div', 'acc-secret-slot'));
  var tt = accTable(['Name', 'Role', 'Token', 'Created by', 'Created', 'Last used', '']);
  ACCESS.tokens.forEach(function (t) { tt.body.appendChild(accTokenRow(t)); });
  tokens.appendChild(tt.wrap);
  tokens.appendChild(accAddToken());
  box.appendChild(tokens);
  markAccessCount(ACCESS.users.length);
}
async function accPatch(u, body, msg, revert, said) {
  accMsg(msg, 'Saving\u2026', false);
  try {
    var saved = await adminApi('PATCH', '/api/admin/users/' + encodeURIComponent(u.email), body);
    accApply(function (a) { a.users = a.users.map(function (x) { return x.email === u.email ? Object.assign({}, x, saved || (body.password === undefined ? body : {})) : x; }); });
    var fresh = await accReload();
    renderAccess();
    accStale(fresh);
    accNote(accRowMsg('user', u.email), said);
  } catch (e) {
    if (revert) revert();
    accNote(accLive(msg, accRowMsg('user', u.email)), accError(e, 'Not saved, try again'), true);
  }
}
function accUserRow(u) {
  var tr = accText('tr', 'acc-user' + (u.disabled ? ' off' : ''));
  tr.dataset.email = u.email;
  accCell(tr, u.email);
  accCell(tr, u.name || '');
  var sel = accRoleSelect(u.role, 'acc-role'), idp = ACCESS_IDP.indexOf(u.roleFrom) >= 0;
  sel.setAttribute('aria-label', 'Role of ' + u.email);
  var roleCell = accCell(tr, sel);
  if (idp) {
    sel.disabled = true;
    sel.title = 'Set by the sign-in provider or a role plug-in at each sign-in';
    roleCell.appendChild(accText('span', 'tag plain acc-idp', 'from sign-in provider'));
  }
  accCell(tr, accText('span', 'tag plain acc-src', u.source === 'sso' ? 'sso' : 'local'));
  var off = document.createElement('input');
  off.type = 'checkbox';
  off.className = 'acc-off';
  off.checked = !!u.disabled;
  off.setAttribute('aria-label', 'Disabled: ' + u.email);
  accCell(tr, off);
  var linkCell = accCell(tr, null);
  var ctl = accCell(tr, null);
  ctl.className = 'acc-ctl';
  var msg = accText('span', 'adm-msg');
  msg.setAttribute('role', 'status');
  if (u.source !== 'sso') accLinkCell(linkCell, u, msg);
  sel.addEventListener('change', function () { accPatch(u, {role: sel.value}, msg, function () { sel.value = u.role; off.checked = !!u.disabled; }, 'Role set to ' + sel.value); });
  off.addEventListener('change', function () { accPatch(u, {disabled: off.checked}, msg, function () { sel.value = u.role; off.checked = !!u.disabled; }, off.checked ? 'Disabled' : 'Enabled'); });
  if (u.source === 'sso') { ctl.appendChild(msg); return tr; }
  var reset = accText('button', 'btn sec small acc-reset', 'Reset password');
  reset.type = 'button';
  var form = accText('span', 'acc-pw-form');
  form.hidden = true;
  var pw = accInput('password', 'password', 256, 'New password for ' + u.email);
  pw.className = 'acc-pw';
  pw.required = false;
  var save = accText('button', 'btn small acc-pw-save', 'Set');
  save.type = 'button';
  var cancel = accText('button', 'btn sec small acc-pw-cancel', 'Cancel');
  cancel.type = 'button';
  form.appendChild(pw);
  form.appendChild(save);
  form.appendChild(cancel);
  ctl.appendChild(reset);
  ctl.appendChild(form);
  ctl.appendChild(msg);
  reset.addEventListener('click', function () { form.hidden = !form.hidden; if (!form.hidden) pw.focus(); });
  cancel.addEventListener('click', function () { form.hidden = true; pw.value = ''; accMsg(msg, ''); });
  save.addEventListener('click', function () { accPatch(u, {password: pw.value}, msg, null, 'Password reset'); });
  return tr;
}
function accLink(u) {
  var l = u.ssoLink;
  return l && typeof l === 'object' && ACCESS_LINKS.indexOf(l.state) >= 0 ? l : null;
}
function accLinkText(l) {
  if (!l) return 'Off';
  var since = typeof l.allowedAt === 'string' ? dayhm(l.allowedAt) : '', until = typeof l.expiresAt === 'string' ? dayhm(l.expiresAt) : '';
  if (l.state === 'pending') return 'Waiting for first sign-in' + (since ? ' since ' + since : '') + (until ? ', expires ' + until : '');
  if (l.state === 'expired') return 'Expired' + (until ? ' ' + until : '') + ' with no first sign-in';
  var at = typeof l.boundAt === 'string' ? dayhm(l.boundAt) : '';
  return at ? 'Linked ' + at : 'Linked';
}
function accLinkCell(cell, u, msg) {
  var l = accLink(u), wrap = accText('span', 'acc-link-w');
  var sw = accText('button', 'feat-sw acc-link');
  sw.type = 'button';
  sw.setAttribute('role', 'switch');
  sw.setAttribute('aria-checked', l ? 'true' : 'false');
  sw.setAttribute('aria-label', 'SSO link for ' + u.email);
  sw.title = l ? 'Switch off to stop this account signing in with SSO' : 'Let the next SSO sign-in with this email, verified by the provider, bind to this account';
  wrap.appendChild(sw);
  wrap.appendChild(accText('span', 'acc-link-state', accLinkText(l)));
  cell.appendChild(wrap);
  sw.addEventListener('click', function () { sw.disabled = true; accPatch(u, {ssoLink: !l}, msg, function () { sw.disabled = false; }, l ? 'SSO link removed' : 'SSO link allowed'); });
  if (!l || l.state !== 'expired') return;
  var again = accText('button', 'btn sec small acc-link-again', 'Allow again');
  again.type = 'button';
  again.title = 'Allow the SSO link again for a new waiting period';
  cell.appendChild(again);
  again.addEventListener('click', function () { again.disabled = true; accPatch(u, {ssoLink: true}, msg, function () { again.disabled = false; }, 'SSO link allowed again'); });
}
function accAddUser() {
  var f = accText('form', 'acc-form acc-add-user');
  var email = accInput('email', 'email', 254, 'Email'), name = accInput('name', 'text', 120, 'Name'), role = accRoleSelect('viewer', null, 'role'), pw = accInput('password', 'password', 256, 'Password');
  var go = accText('button', 'btn small', 'Add user');
  go.type = 'submit';
  var msg = accText('span', 'adm-msg');
  msg.setAttribute('role', 'status');
  f.appendChild(accField('Email', email));
  f.appendChild(accField('Name', name));
  f.appendChild(accField('Role', role));
  f.appendChild(accField('Password', pw));
  f.appendChild(go);
  f.appendChild(msg);
  f.addEventListener('submit', async function (e) {
    e.preventDefault();
    go.disabled = true;
    accMsg(msg, 'Adding\u2026', false);
    try {
      var made = await adminApi('POST', '/api/admin/users', {email: email.value.trim(), name: name.value.trim(), role: role.value, password: pw.value});
      accApply(function (a) { if (made && made.email) a.users = a.users.concat([made]); });
      var fresh = await accReload();
      renderAccess();
      accStale(fresh);
      accNote(accFormMsg('acc-add-user'), 'User added');
    } catch (err) {
      accNote(accLive(msg, accFormMsg('acc-add-user')), accError(err, 'Not added, try again'), true);
      go.disabled = false;
    }
  });
  return f;
}
function accTokenRow(t) {
  var tr = accText('tr', 'acc-token' + (t.revoked ? ' off' : ''));
  tr.dataset.id = t.id;
  var named = accCell(tr, t.name);
  if (t.endedByStop === true && !t.revoked) {
    var stop = accText('span', 'tag plain acc-idp acc-stop', 'Refused by the SSO stop');
    stop.title = 'Made from a single sign-on session and refused by the SSO stop: it stays refused after SSO is switched back on, so make a new token';
    named.appendChild(stop);
  }
  accCell(tr, t.role);
  var code = accText('code', null, 'skopax_' + t.id + '_\u2026');
  accCell(tr, code);
  accCell(tr, isTokenActor(t.createdBy) ? tokenActorLabel(t.createdBy) : t.createdBy || '');
  accCell(tr, dayhm(t.createdAt));
  accCell(tr, t.lastUsed ? dayhm(t.lastUsed) : 'never');
  var ctl = accCell(tr, null);
  var msg = accText('span', 'adm-msg');
  msg.setAttribute('role', 'status');
  if (t.revoked) {
    ctl.appendChild(accText('span', 'tag plain', 'Revoked'));
  } else {
    var b = accText('button', 'btn sec small acc-revoke', 'Revoke');
    b.type = 'button';
    b.title = 'Stop this token working right away';
    b.setAttribute('aria-label', 'Revoke token ' + t.name);
    b.addEventListener('click', async function () {
      b.disabled = true;
      accMsg(msg, 'Revoking\u2026', false);
      try {
        await adminApi('DELETE', '/api/admin/tokens/' + encodeURIComponent(t.id));
        accApply(function (a) { a.tokens = a.tokens.map(function (x) { return x.id === t.id ? Object.assign({}, x, {revoked: true}) : x; }); });
        var fresh = await accReload();
        renderAccess();
        accStale(fresh);
        accNote(accRowMsg('token', t.id), 'Revoked');
      } catch (e) {
        b.disabled = false;
        accNote(accLive(msg, accRowMsg('token', t.id)), accError(e, 'Not revoked, try again'), true);
      }
    });
    ctl.appendChild(b);
  }
  ctl.appendChild(msg);
  return tr;
}
function accAddToken() {
  var f = accText('form', 'acc-form acc-add-token');
  var name = accInput('name', 'text', 80, 'Token name'), role = accRoleSelect('viewer', null, 'role');
  name.placeholder = 'deploy-bot';
  var go = accText('button', 'btn small', 'Create token');
  go.type = 'submit';
  var msg = accText('span', 'adm-msg');
  msg.setAttribute('role', 'status');
  f.appendChild(accField('Name', name));
  f.appendChild(accField('Role', role));
  f.appendChild(go);
  f.appendChild(msg);
  f.addEventListener('submit', async function (e) {
    e.preventDefault();
    go.disabled = true;
    accMsg(msg, 'Creating\u2026', false);
    var made;
    try {
      made = await adminApi('POST', '/api/admin/tokens', {name: name.value.trim(), role: role.value});
    } catch (err) {
      accNote(accLive(msg, accFormMsg('acc-add-token')), accError(err, 'Not created, try again'), true);
      go.disabled = false;
      return;
    }
    var listed = Object.assign({}, made);
    delete listed.token;
    accApply(function (a) { a.tokens = a.tokens.concat([listed]); });
    var fresh = await accReload();
    renderAccess();
    accStale(fresh);
    accShowToken(made.name, made.token);
  });
  return f;
}
function accShowToken(name, token) {
  var slot = document.querySelector('#access .acc-secret-slot');
  if (!slot) return;
  var box = accText('div', 'acc-secret');
  box.appendChild(accText('b', null, 'New token ' + name));
  box.appendChild(accText('code', null, token));
  var row = accText('div', 'adm-acts');
  var copy = accText('button', 'btn small', 'Copy');
  copy.type = 'button';
  var state = accText('span', 'adm-msg');
  state.setAttribute('role', 'status');
  row.appendChild(copy);
  row.appendChild(state);
  box.appendChild(row);
  box.appendChild(accText('p', 'adm-help', "Copy it now. It won't be shown again."));
  copy.addEventListener('click', async function () {
    try {
      await navigator.clipboard.writeText(token);
      accMsg(state, 'Copied', false);
    } catch (e) {
      var code = box.querySelector('code'), range = document.createRange();
      range.selectNodeContents(code);
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      accMsg(state, 'Selected: press Ctrl+C or Cmd+C to copy', false);
    }
  });
  slot.appendChild(box);
}

var SSO = null, SSO_TESTING = false, SSO_MAP_MAX = 50, SSO_VALUE_MAX = 4000, SSO_SEALED = 'client-secret', SSO_MAP = 'role-map', SSO_VERIFIED = 'require-verified-email', SSO_KEEP = '+sso', MCP_FAILED = false;
var SSO_MAIN = ['issuer', 'client-id', 'client-secret', 'scope', 'claims', 'roles-claim', 'role-map', 'default-role', 'require-verified-email'];
var SSO_ADVANCED = ['authorize-url', 'token-url', 'jwks-url', 'client-auth'];
var SSO_LABELS = {issuer: 'Issuer URL', 'client-id': 'Client ID', 'client-secret': 'Client secret', scope: 'Scope', claims: 'Claims come from', 'roles-claim': 'Roles claim',
  'role-map': 'Role map', 'default-role': 'Default role', 'require-verified-email': 'Require verified email', 'authorize-url': 'Authorize URL', 'token-url': 'Token URL',
  'jwks-url': 'Keys (JWKS) URL', 'client-auth': 'Client authentication'};
var SSO_HELP = {issuer: 'Its /.well-known/openid-configuration document gives the endpoints.', 'client-secret': 'Saved sealed and never shown again.',
  scope: 'Separated by spaces; it includes openid unless the claims come from the access token.', 'roles-claim': 'A claim name such as roles or groups, or a dot path such as realm_access.roles.',
  'role-map': 'Claim values matched exactly; several matches give the highest role.',
  'default-role': 'For someone no plug-in, claim or Admin-set role gives a role.', 'require-verified-email': 'Refuses a sign-in whose token does not say the email is verified.',
  'authorize-url': 'Empty uses the one the provider lists.', 'token-url': 'Empty uses the one the provider lists.', 'jwks-url': 'Empty uses the one the provider lists.',
  'client-auth': 'auto follows what the provider lists, post sends the secret in the form and basic in an Authorization header.'};
var SSO_CHOICES = {claims: [['id-token', 'ID token'], ['access-token', 'access token']], 'client-auth': [['auto', 'auto'], ['post', 'post'], ['basic', 'basic']],
  'default-role': [['viewer', 'viewer'], ['operator', 'operator']]};
var SSO_REASONS = {discovery: 'did not answer with its settings', issuer: 'names a different issuer', unreachable: 'not reachable'};
var SSO_STOP = 'Single sign-on sessions end within 30 seconds and stay ended after switching back on: people sign in again. MCP tokens issued before stay refused. API tokens made from single sign-on sessions are refused and stay refused after switching back on: make new ones afterwards. Password logins go on.';
var SSO_CHECKS = {discovery: 'Discovery', issuer: 'Issuer', pkce: 'PKCE', keys: 'Signing keys', 'client-auth': 'Client authentication', redirect: 'Redirect URI', scope: 'Scope'};
var MCP_KEYS = ['mcp.client-id', 'mcp.audience', 'mcp.allowed-clients', 'mcp.required-role', 'mcp.issuer', 'mcp.scope', 'mcp.resource-url'], MCP_META = '/.well-known/oauth-protected-resource';
function ssoText(v) { return typeof v === 'string' ? v : ''; }
function ssoView(r) {
  r = r && typeof r === 'object' ? r : {};
  var p = r.provider && typeof r.provider === 'object' ? r.provider : {}, s = r.secret && typeof r.secret === 'object' ? r.secret : {};
  return {enabled: r.enabled === true, active: r.active === true, problem: ssoText(r.problem), redirectUri: ssoText(r.redirectUri),
    provider: {ok: p.ok !== false, since: ssoText(p.since), error: ssoText(p.error)}, values: r.values && typeof r.values === 'object' ? r.values : {},
    bases: r.bases && typeof r.bases === 'object' ? r.bases : {}, switch: ssoLayer(r.switch), secretNote: ssoText(r.secretNote),
    secret: {set: s.set === true, from: ssoText(s.from) || null, unreadable: s.unreadable === true}, inUse: ssoInUse(r.roleMapInUse), mcpBase: ssoText(r.mcpBase)};
}
function ssoInUse(list) {
  return (Array.isArray(list) ? list : []).filter(function (x) { return x && typeof x.value === 'string' && ACCESS_ROLES.indexOf(x.role) >= 0; }).map(function (x) { return {value: x.value, role: x.role}; });
}
function ssoLayer(v) {
  v = v && typeof v === 'object' ? v : {};
  return {source: v.source === 'admin' || v.source === 'config' ? v.source : 'default', from: ssoText(v.from) || null, base: ssoText(v.base), baseValue: typeof v.baseValue === 'boolean' ? v.baseValue : null};
}
function ssoSource(v) {
  var text = v.source === 'admin' ? 'set here' : v.source === 'config' ? 'from ' + (v.from || 'the configuration') : 'default';
  return '<span class="tag ' + (v.source === 'admin' ? 'k-act' : 'plain') + ' sso-src">' + esc(text) + '</span>' + (v.source === 'admin' && v.base ? '<span class="muted sso-base">over ' + esc(v.base) + '</span>' : '');
}
function ssoVal(name) {
  var v = SSO && SSO.values[name];
  v = v && typeof v === 'object' ? v : {};
  return {value: ssoText(v.value), source: v.source === 'admin' || v.source === 'config' ? v.source : 'default', from: ssoText(v.from) || null, base: SSO ? ssoText(SSO.bases[name]) : ''};
}
function ssoCard() { return document.querySelector('#acc-sso .sso-card'); }
function ssoNote(node, text, bad) {
  if (!node) return;
  node.textContent = text || '';
  node.classList.toggle('bad', !!bad);
  node.classList.toggle('ok', !bad && !!text);
}
function ssoSay(text, bad) {
  var card = ssoCard(), msg = card && card.querySelector('.sso-msg');
  if (msg) ssoNote(msg, text, bad);
}
function ssoUnset() {
  var s = SSO.secret;
  return !SSO.active && !ssoVal('issuer').value && ssoVal('client-id').source === 'default' && !s.set && !s.from && !s.unreadable;
}
function ssoPill() {
  if (SSO.active) return ['On', 'k-ok'];
  if (ssoUnset()) return ['Not set up', 'plain'];
  if (SSO.enabled) return ['Needs setup', 'k-warn'];
  return ['Off', 'plain'];
}
function ssoProvider() {
  var p = SSO.provider, since = p.since ? ' since ' + dayhm(p.since) : '';
  if (!p.ok) return 'Identity provider ' + (SSO_REASONS[p.error] ? SSO_REASONS[p.error] + since : 'not reachable' + since + (p.error ? ': ' + p.error : ''));
  return SSO.active ? 'Identity provider: no problem seen' : '';
}
function ssoMapRows(text) {
  var list;
  try { list = JSON.parse(text); } catch (e) { return []; }
  if (!Array.isArray(list)) return [];
  return list.filter(function (x) { return x && typeof x.value === 'string'; }).map(function (x) { return {value: x.value, role: ACCESS_ROLES.indexOf(x.role) >= 0 ? x.role : 'viewer'}; });
}
function ssoMapText(rows) { return rows.length ? JSON.stringify(rows.map(function (r) { return {value: r.value, role: r.role}; })) : ''; }
function ssoMapRow(value, role) {
  return '<tr class="sso-map-row"><td><input type="text" class="sso-map-v" maxlength="200" spellcheck="false" autocomplete="off" aria-label="Claim value" value="' + esc(value) + '"></td>' +
    '<td><select class="sso-map-r" aria-label="Role">' + ACCESS_ROLES.map(function (r) { return '<option value="' + r + '"' + (r === role ? ' selected' : '') + '>' + r + '</option>'; }).join('') + '</select></td>' +
    '<td><button type="button" class="btn sec small sso-map-del">Remove</button></td></tr>';
}
function ssoSrc(name) {
  if (name !== SSO_SEALED) return ssoSource(ssoVal(name));
  var s = SSO.secret, text = s.from ? 'from ' + s.from : s.set ? 'set here' : s.unreadable ? 'unreadable' : 'not set';
  return '<span class="tag ' + (s.from ? 'plain' : s.set ? 'k-act' : 'k-warn') + ' sso-src">' + esc(text) + '</span>' +
    (s.set && !s.from ? '<button type="button" class="btn sec small sso-secret-del" title="Delete the client secret set here">Remove</button>' : '');
}
function ssoInUseRows() {
  if (!SSO.inUse.length) return '';
  return '<table class="sso-inuse"><tbody>' + SSO.inUse.map(function (r) {
    return '<tr class="sso-inuse-row"><td><code class="sso-inuse-v">' + esc(r.value) + '</code></td><td class="sso-inuse-r">' + esc(r.role) + '</td><td><span class="tag plain sso-inuse-tag">in use now</span></td></tr>';
  }).join('') + '</tbody></table>';
}
function ssoHelp(name) {
  var help = SSO_HELP[name] || '';
  if (name === SSO_SEALED && SSO.secret.from) return ssoOwned(SSO.secret.from) + '.';
  if (name !== SSO_MAP || !SSO.inUse.length) return help;
  var one = SSO.inUse.length === 1, names = [];
  SSO.inUse.forEach(function (r) { if (SSO_ENV_NAMES[r.role] && names.indexOf(SSO_ENV_NAMES[r.role]) < 0) names.push(SSO_ENV_NAMES[r.role]); });
  return help + ' No role map is saved, so ' + SSO.inUse.map(function (r) { return r.value + ' \u2192 ' + r.role; }).join(' and ') + (one ? ' is' : ' are') + ' in use' +
    (names.length ? ' (from ' + names.join(' and ') + ')' : '') + (one ? '. Adding a row replaces it; add it as a row to keep it.' : '. Adding a row replaces them; add them as rows to keep them.');
}
var SSO_ENV_NAMES = {admin: 'SKOPAX_ADMIN_ROLE', operator: 'SKOPAX_OPERATOR_ROLE'};
function ssoOwned(from) { return 'Set in ' + (from === 'environment' ? 'the environment' : from) + '; change it there'; }
function ssoControl(name) {
  var v = ssoVal(name), id = ' id="sso-in-' + name + '"';
  if (name === SSO_MAP) return '<div class="sso-map">' + ssoInUseRows() + '<table class="sso-map-t"><tbody>' + ssoMapRows(v.value).map(function (r) { return ssoMapRow(r.value, r.role); }).join('') + '</tbody></table>' +
    '<button type="button" class="btn sec small sso-map-add">Add row</button></div>';
  if (name === SSO_VERIFIED) return '<input type="checkbox"' + id + ' class="sso-in"' + (v.value === 'true' ? ' checked' : '') + '>';
  if (SSO_CHOICES[name]) {
    var opts = SSO_CHOICES[name].slice();
    if (v.value && !opts.some(function (o) { return o[0] === v.value; })) opts.push([v.value, v.value]);
    return '<select' + id + ' class="sso-in adm-in">' + opts.map(function (o) { return '<option value="' + esc(o[0]) + '"' + (o[0] === v.value ? ' selected' : '') + '>' + esc(o[1]) + '</option>'; }).join('') + '</select>';
  }
  if (name === SSO_SEALED) {
    var s = SSO.secret, shown = s.from ? 'set \u00b7 from ' + s.from : s.set ? 'set' : s.unreadable ? 'cannot be read' : 'not set';
    return '<input type="text"' + id + ' class="sso-in adm-in" maxlength="2000" autocomplete="off" spellcheck="false" autocapitalize="off" data-1p-ignore data-lpignore="true" data-bwignore data-form-type="other" placeholder="' +
      esc(shown) + '"' + (s.from ? ' readonly title="' + esc(ssoOwned(s.from)) + '"' : '') + '>';
  }
  return '<input type="text"' + id + ' class="sso-in adm-in" maxlength="4000" spellcheck="false" autocomplete="off" value="' + esc(v.value) + '">';
}
function ssoField(name) {
  var label = esc(SSO_LABELS[name]), head = name === SSO_MAP ? '<b class="sso-fn">' + label + '</b>' : '<label class="sso-fn" for="sso-in-' + name + '">' + label + '</label>';
  var v = ssoVal(name), reset = name !== SSO_SEALED && v.source === 'admin' ? '<button type="button" class="btn sec small sso-reset" title="' + esc('Drop the value set here and go back to ' + (v.base || 'the default')) + '">Reset</button>' : '';
  var note = name === SSO_SEALED && SSO.secretNote ? '<p class="adm-help adm-problem sso-unreadable">' + esc(SSO.secretNote) + '</p>' : '';
  var help = ssoHelp(name);
  return '<div class="sso-f" data-field="' + esc(name) + '" data-key="sso.' + esc(name) + '"><div class="adm-n">' + head + ssoSrc(name) + reset + '</div>' + ssoControl(name) + note +
    (help ? '<p class="adm-help">' + esc(help) + '</p>' : '') + '<p class="adm-msg sso-fmsg" role="status"></p></div>';
}
function renderSso(said) {
  var box = document.getElementById('acc-sso');
  if (!box) return;
  if (!SSO) { box.innerHTML = '<p class="muted">Single sign-on did not load; open the tab again.</p>'; return; }
  var pill = ssoPill(), prov = ssoProvider(), uri = SSO.redirectUri, stops = ssoResetStops();
  box.innerHTML = '<section class="ai-key sso-card" aria-labelledby="sso-h"><div class="ai-head"><h3 id="sso-h">Single sign-on</h3><span class="tag ' + pill[1] + ' sso-pill">' + pill[0] + '</span>' +
    '<button type="button" role="switch" class="feat-sw sso-sw" data-key="sso.enabled" aria-checked="' + (SSO.enabled ? 'true' : 'false') + '" aria-label="Single sign-on" title="' +
    (SSO.enabled ? 'Switch off: single sign-on sessions end within 30 seconds and stay ended after switching back on, and MCP turns off, while password logins go on' : 'Switch on') + '"></button><span class="sso-sw-l">' + ssoSource(SSO.switch) + '</span>' +
    (SSO.switch.source === 'admin' ? '<button type="button" class="btn sec small sso-sw-reset" title="' + esc('Drop the switch set here and go back to ' + (SSO.switch.base || 'the default')) + '">' + (stops ? 'Reset (turns SSO off)' : 'Reset') + '</button>' : '') + '</div>' +
    (SSO.problem && !ssoUnset() ? '<p class="adm-help adm-problem sso-problem">' + esc(SSO.problem) + '</p>' : '') + (prov ? '<p class="adm-help sso-provider">' + esc(prov) + '</p>' : '') +
    '<div class="sso-fields">' + SSO_MAIN.map(ssoField).join('') + '</div>' +
    '<details class="sso-adv"><summary class="chev">Advanced: endpoints</summary><div class="sso-fields">' + SSO_ADVANCED.map(ssoField).join('') + '</div></details>' +
    '<div class="sso-redirect"><span class="sso-fn">Redirect URI to register</span>' + (uri ? '<code>' + esc(uri) + '</code>' : '<span class="muted sso-nouri">Not set: set SKOPAX_BASE_URL (or SSO_REDIRECT_URI) and restart.</span>') +
    '<button type="button" class="btn sec small sso-copy"' + (uri ? '' : ' disabled') + '>Copy</button>' +
    '<span class="adm-msg" role="status"></span></div>' +
    '<div class="adm-acts"><button type="button" class="btn sec small sso-test">Test connection</button><button type="button" class="btn small sso-save">Save</button>' +
    (SSO.enabled ? '' : '<button type="button" class="btn small sso-on">Save &amp; turn on</button>') + '<span class="adm-msg sso-msg" role="status"></span></div>' +
    '<div class="sso-result"></div></section>';
  var card = ssoCard();
  card.querySelector('.sso-test').disabled = SSO_TESTING;
  card.addEventListener('input', function (e) { ssoUnmark(e.target.closest('.sso-f')); });
  card.addEventListener('change', function (e) { ssoUnmark(e.target.closest('.sso-f')); });
  card.addEventListener('click', ssoClick);
  if (said) ssoSay(said);
}
function ssoClick(e) {
  var t = e.target.closest('button');
  if (!t || t.disabled) return;
  var f = t.closest('.sso-f');
  if (t.classList.contains('sso-map-add')) return ssoAddRow(f);
  if (t.classList.contains('sso-map-del')) { t.closest('tr').remove(); ssoUnmark(f); return; }
  if (t.classList.contains('sso-reset')) { var reset = {}; reset[f.dataset.field] = null; return ssoPut({values: reset}, 'Reset'); }
  if (t.classList.contains('sso-secret-del')) return ssoRemoveSecret(t);
  if (t.classList.contains('sso-sw-reset')) return ssoSwitchReset(t);
  if (t.classList.contains('sso-copy')) return ssoCopy(t);
  if (t.classList.contains('sso-sw')) return SSO.enabled ? ssoSwitchOff(t) : ssoPut({enabled: true}, 'Turned on');
  if (t.classList.contains('sso-test')) return ssoTest();
  var on = t.classList.contains('sso-on');
  if (!on && !t.classList.contains('sso-save')) return;
  var values = ssoTyped();
  if (!values) return;
  if (!on && !Object.keys(values).length) { ssoSay('Nothing changed'); return; }
  ssoPut(on ? {values: values, enabled: true} : {values: values}, on ? 'Saved and turned on' : 'Saved');
}
function ssoAddRow(f) {
  var body = f.querySelector('.sso-map-t tbody');
  if (body.querySelectorAll('.sso-map-row').length >= SSO_MAP_MAX) { ssoMark(SSO_MAP, 'At most ' + SSO_MAP_MAX + ' rows.'); return; }
  body.insertAdjacentHTML('beforeend', ssoMapRow('', 'viewer'));
  var rows = body.querySelectorAll('.sso-map-v');
  rows[rows.length - 1].focus();
}
function ssoTyped() {
  var card = ssoCard(), out = {}, refused = false;
  SSO_MAIN.concat(SSO_ADVANCED).forEach(function (name) {
    if (name === SSO_SEALED) return;
    var now;
    if (name === SSO_MAP) {
      var rows = ssoRowsOf(card).map(function (r) { return {value: r.value === r.orig ? r.value : r.value.trim(), role: r.role}; });
      now = ssoMapText(rows.filter(function (r) { return r.value.trim(); }));
      if (now === ssoMapText(ssoMapRows(ssoVal(name).value))) return;
      if (rows.length > SSO_MAP_MAX) { refused = true; ssoMark(SSO_MAP, 'At most ' + SSO_MAP_MAX + ' rows.'); return; }
      if (now.length > SSO_VALUE_MAX) { refused = true; ssoMark(SSO_MAP, 'The role map is at most ' + SSO_VALUE_MAX + ' characters as JSON; shorten or remove rows.'); return; }
      out[name] = now;
      return;
    }
    var inp = document.getElementById('sso-in-' + name);
    if (!inp) return;
    now = inp.type === 'checkbox' ? (inp.checked ? 'true' : 'false') : inp.value === inp.defaultValue ? ssoVal(name).value : inp.value.trim();
    if (now !== ssoVal(name).value) out[name] = now;
  });
  var secret = document.getElementById('sso-in-' + SSO_SEALED);
  if (secret && !secret.readOnly && secret.value.trim()) out[SSO_SEALED] = secret.value.trim();
  return refused ? null : out;
}
function ssoRowsOf(card) {
  return [].map.call(card.querySelectorAll('.sso-map-row'), function (r) {
    var v = r.querySelector('.sso-map-v'), role = r.querySelector('.sso-map-r'), was = [].filter.call(role.options, function (o) { return o.defaultSelected; })[0];
    return {value: v.value, orig: v.defaultValue, role: role.value, origRole: was ? was.value : 'viewer'};
  });
}
function ssoDraft() {
  var card = ssoCard(), out = {fields: {}, map: null, secret: ''};
  if (!card) return out;
  SSO_MAIN.concat(SSO_ADVANCED).forEach(function (name) {
    var inp = document.getElementById('sso-in-' + name);
    if (!inp || name === SSO_SEALED) return;
    if (inp.type === 'checkbox') { if (inp.checked !== inp.defaultChecked) out.fields[name] = inp.checked; return; }
    if (inp.tagName === 'SELECT') {
      var was = [].filter.call(inp.options, function (o) { return o.defaultSelected; })[0];
      if (!was || was.value !== inp.value) out.fields[name] = inp.value;
      return;
    }
    if (inp.value !== inp.defaultValue) out.fields[name] = inp.value;
  });
  var rows = ssoRowsOf(card), pairs = function (list) { return JSON.stringify(list.map(function (r) { return [r.value, r.role]; })); };
  if (pairs(rows) !== pairs(ssoMapRows(ssoVal(SSO_MAP).value))) out.map = rows;
  var secret = document.getElementById('sso-in-' + SSO_SEALED);
  if (secret && !secret.readOnly) out.secret = secret.value;
  return out;
}
function ssoRestore(draft, skip) {
  var card = ssoCard();
  if (!card || !draft) return;
  Object.keys(draft.fields).forEach(function (name) {
    var inp = document.getElementById('sso-in-' + name);
    if (!inp || skip.indexOf(name) >= 0) return;
    if (inp.type === 'checkbox') inp.checked = draft.fields[name];
    else inp.value = draft.fields[name];
    var fold = inp.closest('details');
    if (fold) fold.open = true;
  });
  var body = card.querySelector('.sso-map-t tbody');
  if (draft.map && body && skip.indexOf(SSO_MAP) < 0) {
    body.innerHTML = draft.map.map(function (r) { return ssoMapRow(r.orig, r.origRole); }).join('');
    [].forEach.call(body.querySelectorAll('.sso-map-row'), function (row, n) {
      row.querySelector('.sso-map-v').value = draft.map[n].value;
      row.querySelector('.sso-map-r').value = draft.map[n].role;
    });
  }
  var secret = document.getElementById('sso-in-' + SSO_SEALED);
  var shown = typeof curTab !== 'undefined' && curTab === 'access';
  if (shown && draft.secret && secret && !secret.readOnly && skip.indexOf(SSO_SEALED) < 0) secret.value = draft.secret;
}
function ssoRepaint(said, draft, skip) {
  renderSso(said);
  ssoRestore(draft, skip);
  renderMcpRows(SSO_KEEP);
}
async function ssoRemoveSecret(b) {
  var ask = window.__skopaxConfirm;
  if (typeof ask !== 'function') return;
  var ok = await ask('Remove the client secret?', 'The client secret set here is deleted. Single sign-on needs a secret again before anyone can sign in with it.', 'Remove secret');
  if (!ok) { if (b.isConnected) b.focus(); return; }
  ssoPut({values: {'client-secret': ''}}, 'Secret removed');
}
function ssoResetStops() { return SSO.switch.source === 'admin' && SSO.switch.baseValue === false && SSO.enabled; }
async function ssoAskStop(b, title, yes) {
  var ask = window.__skopaxConfirm;
  if (typeof ask !== 'function') return false;
  var ok = await ask(title, SSO_STOP, yes);
  if (!ok && b.isConnected) b.focus();
  return ok;
}
async function ssoSwitchOff(b) {
  if (await ssoAskStop(b, 'Turn single sign-on off?', 'Turn off')) ssoPut({enabled: false}, 'Turned off');
}
async function ssoSwitchReset(b) {
  var r;
  if (ssoResetStops() && !(await ssoAskStop(b, 'Reset and turn single sign-on off?', 'Reset and turn off'))) return;
  ssoBusy(true);
  ssoSay('Resetting\u2026');
  try {
    await adminApi('DELETE', '/api/admin/settings?key=' + encodeURIComponent('sso.enabled'));
  } catch (e) {
    ssoBusy(false);
    ssoFail(e, 'Not reset, try again');
    return;
  }
  try {
    r = await adminApi('GET', '/api/admin/sso');
  } catch (e) {
    ssoBusy(false);
    ssoSay('Switch reset; open the tab again to see it.');
    return;
  }
  var draft = ssoDraft();
  SSO = ssoView(r);
  ssoRepaint('Switch reset to ' + (SSO.switch.source === 'config' ? 'the ' + (SSO.switch.from || 'configuration') + ' value' : 'the default'), draft, []);
}
function ssoFieldEl(name) {
  var card = ssoCard();
  return card ? [].filter.call(card.querySelectorAll('.sso-f'), function (f) { return f.dataset.field === name; })[0] || null : null;
}
function ssoMark(name, text) {
  var f = ssoFieldEl(name);
  if (!f) return false;
  f.classList.add('bad');
  f.querySelectorAll('.sso-in, .sso-map-v').forEach(function (i) { i.setAttribute('aria-invalid', 'true'); });
  ssoNote(f.querySelector('.sso-fmsg'), text, true);
  var fold = f.closest('details');
  if (fold) fold.open = true;
  return true;
}
function ssoUnmark(f) {
  if (!f || !f.classList.contains('bad')) return;
  f.classList.remove('bad');
  f.querySelectorAll('[aria-invalid]').forEach(function (i) { i.removeAttribute('aria-invalid'); });
  ssoNote(f.querySelector('.sso-fmsg'), '');
}
function ssoUnmarkAll() {
  var card = ssoCard();
  if (card) card.querySelectorAll('.sso-f.bad').forEach(ssoUnmark);
}
function ssoBusy(on) {
  var card = ssoCard();
  if (card) card.querySelectorAll('.sso-save, .sso-on, .sso-sw, .sso-reset, .sso-sw-reset, .sso-secret-del').forEach(function (b) { b.disabled = on; });
}
function ssoFail(e, fallback) {
  var text = accError(e, fallback);
  if (e && e.field && ssoMark(e.field, text)) ssoSay('Not saved: see ' + (SSO_LABELS[e.field] || e.field), true);
  else ssoSay(text, true);
}
function ssoEditedSince(sent, late, name) {
  if (name === SSO_MAP) return JSON.stringify(sent.map) !== JSON.stringify(late.map);
  if (name === SSO_SEALED) return sent.secret !== late.secret;
  return sent.fields[name] !== late.fields[name];
}
async function ssoPut(body, said) {
  var skip = Object.keys(body.values || {}), sent = ssoDraft();
  ssoUnmarkAll();
  ssoBusy(true);
  ssoSay('Saving\u2026');
  var r;
  try {
    r = await adminApi('PUT', '/api/admin/sso', body);
  } catch (e) {
    ssoBusy(false);
    ssoFail(e, 'Not saved, try again');
    return;
  }
  var draft = ssoDraft();
  SSO = ssoView(r);
  ssoRepaint(said, draft, skip.filter(function (name) { return !ssoEditedSince(sent, draft, name); }));
}
async function ssoTest() {
  if (SSO_TESTING) return;
  var values = ssoTyped(), card = ssoCard();
  if (!values) return;
  ssoUnmarkAll();
  SSO_TESTING = true;
  card.querySelector('.sso-test').disabled = true;
  card.querySelector('.sso-result').textContent = '';
  ssoSay('Testing\u2026');
  try {
    var r = await adminApi('POST', '/api/admin/sso/test', {values: values});
    ssoSay('');
    ssoResult(r);
  } catch (e) {
    if (e && e.field && ssoMark(e.field, accError(e, ''))) ssoSay('The test did not run: see ' + (SSO_LABELS[e.field] || e.field), true);
    else ssoSay(accError(e, 'The test could not run'), true);
  }
  SSO_TESTING = false;
  var now = ssoCard();
  if (now) now.querySelector('.sso-test').disabled = false;
}
function ssoResult(r) {
  var box = document.querySelector('#acc-sso .sso-result');
  if (!box) return;
  r = r && typeof r === 'object' ? r : {};
  var checks = Array.isArray(r.checks) ? r.checks.filter(function (c) { return c && typeof c.id === 'string'; }) : [];
  box.innerHTML = '<p class="adm-msg sso-summary ' + (r.ok === true ? 'ok' : 'bad') + '">' + esc((r.ok === true ? '\u2713 ' : '') + ssoText(r.summary) + ' \u00b7 ' + (Number(r.ms) || 0) + ' ms') + '</p>' +
    (checks.length ? '<ul class="sso-checks">' + checks.map(function (c) {
      var ok = c.ok === true;
      return '<li class="sso-check ' + (ok ? 'ok' : 'bad') + '" data-check="' + esc(c.id) + '"><b class="sso-cn">' + esc(SSO_CHECKS[c.id] || c.id) + '</b><span class="tag ' + (ok ? 'k-ok' : 'k-crit') + ' sso-ok">' +
        (ok ? 'ok' : 'fail') + '</span><span class="sso-ct">' + esc(ssoText(c.text)) + '</span></li>';
    }).join('') + '</ul>' : '');
}
async function ssoCopy(b) {
  var box = b.closest('.sso-redirect'), state = box.querySelector('.adm-msg');
  try {
    await navigator.clipboard.writeText(SSO.redirectUri);
    ssoNote(state, 'Copied', false);
  } catch (e) {
    var range = document.createRange(), sel = window.getSelection();
    range.selectNodeContents(box.querySelector('code'));
    sel.removeAllRanges();
    sel.addRange(range);
    ssoNote(state, 'Selected: press Ctrl+C or Cmd+C to copy', false);
  }
}
function mcpItems() {
  var rank = function (i) { var n = MCP_KEYS.indexOf(i.key); return n < 0 ? MCP_KEYS.length : n; };
  return adminItems().filter(function (i) { return i.group === MCP_GROUP; }).map(function (i, n) { return [i, n]; })
    .sort(function (a, b) { return rank(a[0]) - rank(b[0]) || a[1] - b[1]; }).map(function (x) { return x[0]; });
}
function mcpMetaUrl(value) { return String(value || (SSO && SSO.mcpBase) || location.origin).replace(/\/+$/, '') + MCP_META; }
function renderMcpRows(saved) {
  var box = document.getElementById('acc-mcp');
  if (!box || (!ADMIN && !MCP_FAILED)) return;
  adminKeep(box, saved, function () {
    var items = ADMIN && !MCP_FAILED ? mcpItems() : [];
    box.textContent = '';
    var s = el('section', 'adm-grp ai-grp acc-mcp-grp'), client = items.filter(function (i) { return i.key === 'mcp.client-id'; })[0];
    s.dataset.group = MCP_GROUP;
    s.appendChild(accText('h3', null, 'MCP'));
    s.appendChild(accText('p', 'adm-help acc-mcp-note', 'MCP signs people in through the identity provider, so it needs single sign-on set up and on, and the MCP server switch on in Features' +
      (SSO && !SSO.active ? '. Single sign-on is not on, so MCP is off.' : '.')));
    if (!ADMIN || MCP_FAILED) s.appendChild(accText('p', 'muted acc-mcp-note acc-mcp-failed', 'MCP settings did not load; open the tab again.'));
    items.forEach(function (i) {
      var row = adminRow(i), input = row.querySelector('.adm-in');
      if (i.key === 'mcp.audience' && input) input.placeholder = client && client.value ? client.value : 'the client ID';
      if (i.key === 'mcp.resource-url') {
        var meta = accText('p', 'adm-help acc-mcp-meta', 'Metadata: ');
        meta.appendChild(accText('code', null, mcpMetaUrl(i.value)));
        row.querySelector('.adm-c').appendChild(meta);
      }
      s.appendChild(row);
    });
    box.appendChild(s);
  });
}
