var ADMIN_OK = false, ADMIN_ROLE = 'admin', ADMIN_WANTED = false, ADMIN = null, ADMIN_HIST = null, ADMIN_SEQ = 0, ADMIN_FIND = '', ADMIN_TALK = null;
var ADMIN_NOTES = [], NOTES_SEQ = 0, FEATS = null, FEATS_SEQ = 0, AIKEY = null, OAIKEY = null, AIKEY_SEQ = 0, AI_TESTING = {anthropic: false, openai: false}, CHANGES_SEQ = 0, AIUSES = null, AICONN = null, AIVTX = null;
var FEAT_PENDING = {}, FEAT_HELD = {}, FEAT_SAID = {}, FEAT_HOLD_MS = 15000;
var TALK_NAMES = {'small-talk.enabled': 'Small talk replies', 'small-talk.hourly-cap': 'Small talk replies per hour', 'small-talk.rules': 'Small talk rules'};
var ADMIN_FOLD = {}, ADMIN_HIST_SHOW = 10, ADMIN_HIST_STEP = 20, ADMIN_HIST_MAX = 200, ADMIN_CLIP = 140, ADMIN_LINE_MAX = 1000;
var FEATURES_GROUP = 'Features', AI_GROUP = 'Investigator', SIGNIN_GROUP = 'Sign-in', MCP_GROUP = 'MCP', INSTANCES_GROUP = 'Workspaces', OTHER_SWITCHES = 'Other switches';
var ADMIN_REVEAL = null, ADMIN_HIT_MS = 2000;
var ADMIN_TITLES = {settings: 'Settings: the tunable settings', access: 'Access & sign-in: single sign-on, users, API tokens and MCP',
  workspaces: 'Workspaces: the other workspaces the switcher opens', features: 'Features: core features to switch on or off', ai: 'Investigator: the AI providers and their keys, which model each use runs, and the investigator settings', changes: 'Changes: what was changed in Admin, with Revert'};
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
  document.dispatchEvent(new CustomEvent('skopax:admin', {detail: {on: ADMIN_OK}}));
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
    OAIKEY = null;
    AIUSES = null;
    AICONN = null;
    AIVTX = null;
    AIU_EDIT = {};
    AIU_PENDING = {};
    AIU_SAID = {};
    AIU_ADV_SAID = {};
    AIU_ADV_BUSY = {};
    if (typeof pqcClear === 'function') pqcClear();
    NOTES_SEQ++;
    adminNotes([]);
    if (ADMIN_TABS.indexOf(curTab) >= 0) showTab('anoms');
    if (curTab === 'integrations' && typeof intLeave === 'function') { intLeave(); var ib = document.getElementById('integrations'); if (ib) ib.textContent = ''; if (typeof intShow === 'function') intShow(); }
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
  else if (id === 'features') loadFeatures();
  else if (id === 'ai') loadAi();
  else if (id === 'workspaces') loadInstances();
  else loadChanges();
  adminCounts(id);
}
async function adminApi(method, path, body, signal, raw) {
  var headers = {'Accept': 'application/json'};
  if (body !== undefined) headers['Content-Type'] = raw || 'application/json';
  var r;
  try { var sent = fetch(path, {method: method, headers: headers, credentials: 'same-origin', body: body === undefined ? undefined : raw ? body : JSON.stringify(body), signal: signal}); r = await (method === 'GET' ? sent : workTrack(sent)); } catch (e) {
    if (!(e && e.name === 'AbortError')) failToast('the server did not answer (' + method + ' ' + path + ')');
    throw e;
  }
  if (!r.ok) {
    var detail = null, field = null;
    try { var failure = await r.json(); detail = failure.detail; field = failure.field; } catch (e) {}
    var list = Array.isArray(detail);
    if (list) detail = detail.map(function (d) { return d && d.msg; }).filter(Boolean).join('; ');
    var said = typeof detail === 'string' && detail ? detail : null;
    if (method !== 'GET') failToast((said || 'the change was not saved') + ' (' + r.status + ')');
    throw {code: r.status, detail: said, field: typeof field === 'string' ? field : null, list: list};
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
function adminCoveredKeys() { return typeof aiuCovered === 'function' ? aiuCovered() : {}; }
function generalOf(items) {
  var keys = featKeys(), covered = adminCoveredKeys();
  return items.filter(function (i) { return [FEATURES_GROUP, AI_GROUP, SIGNIN_GROUP, MCP_GROUP, INSTANCES_GROUP].indexOf(i.group) < 0 && keys.indexOf(i.key) < 0 && !covered[i.key]; });
}
function otherSwitches() {
  var keys = featKeys(), covered = adminCoveredKeys();
  return adminItems().filter(function (i) { return i.group === FEATURES_GROUP && keys.indexOf(i.key) < 0 && !covered[i.key]; });
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
  var settings = !ADMIN && id === 'access', feats = !FEATS && id !== 'features' && id !== 'settings', users = ACCESS_N === null && !ACCESS && id !== 'access';
  if (users) ACCESS_N = -1;
  var r = await Promise.all([settings ? adminApi('GET', '/api/admin/settings').catch(adminNone) : null, feats ? adminApi('GET', '/api/admin/features').catch(adminNone) : null,
    users ? adminApi('GET', '/api/admin/users').catch(adminNone) : null]);
  if (!ADMIN_OK) return;
  if (r[0] && !ADMIN) ADMIN = r[0];
  if (r[1] && !FEATS) setFeats(r[1]);
  if (users && !ACCESS) { if (Array.isArray(r[2])) markAccessCount(r[2].length); else ACCESS_N = null; }
  markAdminCount();
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
  document.querySelectorAll('.adm-notices').forEach(function (box) {
    box.textContent = '';
    ADMIN_NOTES.forEach(function (n) {
      var p = accText('p', 'adm-notice ' + (n.level === 'warn' ? 'k-warn' : 'k-info'));
      if (typeof n.id === 'string') p.dataset.id = n.id;
      p.appendChild(accText('span', 'adm-notice-t', n.text));
      if (typeof n.href === 'string' && /^https:\/\//i.test(n.href) && typeof n.linkText === 'string' && n.linkText) {
        var a = accText('a', 'adm-notice-link', n.linkText);
        a.setAttribute('href', n.href);
        a.setAttribute('target', '_blank');
        a.setAttribute('rel', 'noopener noreferrer');
        p.appendChild(a);
      }
      if (typeof n.at === 'string' && n.at) p.appendChild(accText('span', 'muted adm-notice-at', dayhm(n.at)));
      box.appendChild(p);
    });
    box.hidden = !ADMIN_NOTES.length;
  });
  if (typeof sideAdmin === 'function') sideAdmin();
}
async function loadAdmin() {
  var seq = ++ADMIN_SEQ, box = document.getElementById('admin');
  try {
    var r = await Promise.all([adminApi('GET', '/api/admin/settings'), adminApi('GET', '/api/admin/small-talk').catch(adminNone), adminApi('GET', '/api/admin/features').catch(adminNone),
      adminApi('GET', '/api/admin/tags').catch(adminNone), adminApi('GET', '/api/admin/ai-uses').catch(adminNone)]);
    if (seq !== ADMIN_SEQ) return;
    ADMIN = r[0]; ADMIN_TALK = r[1]; ADMIN_TAGS = r[3] && Array.isArray(r[3].tags) ? r[3] : null;
    if (r[4] !== null) AIUSES = aiuOf(r[4]);
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
async function loadAi(said, saidFor) {
  var seq = ++AIKEY_SEQ, box = document.getElementById('ai');
  try {
    var failed = function (e) { return {failed: true, code: e && e.code}; };
    var r = await Promise.all([adminApi('GET', '/api/admin/anthropic-key'), adminApi('GET', '/api/admin/settings').catch(adminNone), adminApi('GET', '/api/admin/openai-key').catch(function () { return {failed: true}; }),
      adminApi('GET', '/api/admin/ai-connections').catch(failed), adminApi('GET', '/api/admin/ai-uses').catch(failed), adminApi('GET', '/api/admin/ai-vertex').catch(failed)]);
    if (seq !== AIKEY_SEQ) return;
    AIKEY = keyStatus(r[0]);
    OAIKEY = r[2] && r[2].failed ? {failed: true} : keyStatus(r[2]);
    if (r[1]) ADMIN = r[1];
    AICONN = aicOf(r[3]);
    AIVTX = aivOf(r[5]);
    AIU_FAILED = !!(r[4] && r[4].failed && r[4].code !== 404);
    AIUSES = r[4] && r[4].failed ? null : aiuOf(r[4]);
    markAdminCount();
    paintAiKey(said, saidFor);
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
  if (i.oldSwitch && i.oldSwitch.indexOf('core.auto-resolve-') === 0 && i.source !== 'admin') return '<span class="tag plain" title="' + esc('Off because the old Auto-resolve cleared cards after setting (' + i.oldSwitch + ') is 0; a value saved here wins over it') + '">old setting</span>';
  if (i.oldSwitch && i.oldSwitch.indexOf('core.auto-resolve-') === 0) return '<span class="tag k-act" title="' + esc('From the old Auto-resolve cleared cards after setting (' + i.oldSwitch + '), set by ' + draftBy(i.by) + ' ' + dayhm(i.at) + '; Reset or saving a value here clears it') + '">old setting</span>';
  if (i.oldSwitch) return '<span class="tag ' + (i.source === 'admin' ? 'k-act' : 'plain') + '" title="' + esc('From the old Critical cards at once switch (' + i.oldSwitch + ')' + (i.source === 'admin' ? ', set by ' + draftBy(i.by) + ' ' + dayhm(i.at) + '; Reset or saving a value here clears it' : ' in the service configuration; a value saved here wins over it')) + '">old switch</span>';
  if (i.source === 'admin') return '<span class="tag k-act" title="' + esc('Set on this page by ' + draftBy(i.by) + ' ' + dayhm(i.at) + (i.base !== i.value ? '\nWithout it: ' + (i.base || 'empty') : '')) + '">Set by ' + esc(draftBy(i.by)) + ' ' + esc(dayhm(i.at)) + '</span>';
  if (i.source === 'config') return '<span class="tag plain" title="From the service configuration; a value saved here wins over it">config</span>';
  return '<span class="tag plain src-default" title="The built-in default">default</span>';
}
var SEV_ROW_KEYS = ['core.auto-resolve-hours-crit', 'core.auto-resolve-hours-high', 'core.auto-resolve-hours-warn'];
var SEV_ROW_NAMES = ['Critical', 'High', 'Warning'];
var SEV_ROW_LABEL = 'Auto-resolve cleared cards after (hours)';
var SEV_ROW_HELP = 'Closes a card that stopped firing and that nobody acted on after N hours, one value per severity; 0 turns it off for that severity. With Business days only on, only Monday to Friday counts';
function adminSevRow(items) {
  var box = el('div', 'adm-multi');
  box.setAttribute('role', 'group');
  box.setAttribute('aria-labelledby', 'adm-sev-l');
  box.appendChild(el('div', 'adm-multi-h', '<b id="adm-sev-l">' + esc(SEV_ROW_LABEL) + '</b><p class="adm-help">' + esc(SEV_ROW_HELP) + '</p>'));
  items.forEach(function (i, n) {
    var row = adminRow(i), name = row.querySelector('.adm-n b'), input = row.querySelector('.adm-in');
    row.classList.add('adm-sub');
    row.dataset.find += ' ' + SEV_ROW_LABEL.toLowerCase() + ' ' + SEV_ROW_NAMES[n].toLowerCase();
    name.textContent = SEV_ROW_NAMES[n];
    row.querySelectorAll('.adm-l > .adm-help, .adm-l > .adm-more').forEach(function (x) { x.remove(); });
    input.setAttribute('aria-labelledby', 'adm-sev-l ' + name.id);
    box.appendChild(row);
  });
  return box;
}
function adminRows(d, items) {
  var found = SEV_ROW_KEYS.map(function (k) { return items.filter(function (i) { return i.key === k; })[0]; });
  var grouped = found.every(Boolean), placed = false;
  items.forEach(function (i) {
    if (!grouped || found.indexOf(i) < 0) { d.appendChild(adminRow(i)); return; }
    if (!placed) { d.appendChild(adminSevRow(found)); placed = true; }
  });
}
function adminId(key) { return 'adm-' + key.replace(/[^a-z0-9]/gi, '-'); }
function admHint(i) {
  if (i.kind === 'tagrules') return tx('Add rules, such as type:log, log|checkout-*');
  return i.default != null ? 'default: ' + (i.default || 'empty') : '';
}
function adminControl(i) {
  var id = adminId(i.key), named = ' aria-labelledby="' + esc(id + '-l') + '"';
  if (i.kind === 'choice') return '<select id="' + esc(id) + '" class="adm-in"' + named + '>' + i.choices.map(function (c) { return '<option value="' + esc(c) + '"' + (c === i.value ? ' selected' : '') + '>' + esc((i.choiceLabels || {})[c] || c) + '</option>'; }).join('') + '</select>';
  if (i.kind === 'bool') return admSwitch(i, id, named);
  if (i.kind === 'int' || i.kind === 'float') return admUnit(i, '<input id="' + esc(id) + '" class="adm-in"' + named + ' type="number" inputmode="decimal" step="' + (i.kind === 'int' ? '1' : 'any') + '" min="' + esc(i.low) + '" max="' + esc(i.high) + '" value="' + esc(i.value) + '">');
  if (i.kind === 'rules') {
    return '<table class="adm-rules"><tbody>' + Object.keys(i.defaults).map(function (k) {
      var v = i.rules[k], d = i.defaults[k];
      var tip = (i.ruleHelp || {})[k];
      return '<tr><th scope="row"><code>' + esc(k) + '</code>' + (tip ? '<span class="adm-rule-q" tabindex="0" role="img" aria-label="' + esc('What ' + k + ' is for: ' + tip) + '" title="' + esc(tip) + '">?</span>' : '') + '</th><td><input class="adm-rule" type="number" step="any" min="0" data-rule="' + esc(k) + '" value="' + esc(v) + '" aria-label="' + esc(i.label + ' ' + k) + '"></td>' +
        '<td class="muted">' + (v !== d ? 'default ' + esc(d) : '') + '</td></tr>';
    }).join('') + '</tbody></table>';
  }
  if (i.kind === 'text' && (i.limit > ADMIN_LINE_MAX || i.lines)) return '<textarea id="' + esc(id) + '" class="adm-in adm-long"' + named + ' rows="' + esc(i.lines || 8) + '" spellcheck="' + (i.lines ? 'true' : 'false') + '" maxlength="' + esc(i.limit) + '" placeholder="' + esc(admHint(i)) + '">' + esc(i.value) + '</textarea>';
  return '<input id="' + esc(id) + '" class="adm-in"' + named + ' type="text"' + (i.limit ? ' maxlength="' + esc(i.limit) + '"' : '') + ' value="' + esc(i.value) + '" placeholder="' + esc(admHint(i)) + '">';
}
function rowId(row) { return row.dataset.key || row.dataset.secret || row.dataset.route || row.dataset.source || row.dataset.talk || (row.classList.contains('route-add') ? '+route' : ''); }
var UNSAVED_FIELDS = 'input:not(.r-link), select, textarea';
function unsaved(box, saved) {
  var out = [];
  box.querySelectorAll('.adm-row').forEach(function (row) {
    var id = rowId(row);
    if (!id || id === saved) return;
    row.querySelectorAll(UNSAVED_FIELDS).forEach(function (inp, at) {
      var box = inp.type === 'checkbox', was = box ? String(inp.defaultChecked) : inp.tagName === 'SELECT' ? ([].filter.call(inp.options, function (o) { return o.defaultSelected; })[0] || inp.options[0] || {}).value : inp.defaultValue, on = inp === document.activeElement;
      var now = box ? String(inp.checked) : inp.value;
      if (now !== was || on) out.push({id: id, at: at, rule: inp.dataset.rule || '', value: now, on: on});
    });
  });
  return out;
}
function restoreUnsaved(box, list) {
  list.forEach(function (u) {
    try { restoreOne(box, u); } catch (e) { failToast('an unsaved edit could not be kept: ' + (e && e.message ? e.message : 'unknown error')); }
  });
}
function restoreOne(box, u) {
  [u].forEach(function (u) {
    var row = [].filter.call(box.querySelectorAll('.adm-row'), function (r) { return rowId(r) === u.id; })[0], fields = row ? [].slice.call(row.querySelectorAll(UNSAVED_FIELDS)) : [];
    var inp = (u.rule ? fields.filter(function (f) { return f.dataset.rule === u.rule; })[0] : null) || fields[u.at];
    if (!inp || inp.disabled) return;
    if (inp.type === 'checkbox') { if (String(inp.checked) !== u.value) { inp.checked = u.value === 'true'; inp.dispatchEvent(new Event('input')); } }
    else if (inp.value !== u.value) { inp.value = u.value; inp.dispatchEvent(new Event('input')); }
    var form = inp.closest('.route-form');
    if (form) form.hidden = false;
    if (u.on) inp.focus();
  });
}
function adminValue(row, i) {
  if (i.kind !== 'rules') { var inp = row.querySelector('.adm-in'); return inp.type === 'checkbox' ? (inp.checked ? 'true' : 'false') : inp.value; }
  var out = {};
  row.querySelectorAll('.adm-rule').forEach(function (inp) {
    var k = inp.dataset.rule, v = inp.value.trim();
    if (v !== '' && Number(v) !== i.defaults[k]) out[k] = Number(v);
  });
  return Object.keys(out).length ? JSON.stringify(out) : '';
}
var BUILT_SHOWN = 8;
function adminLeftOut(value) {
  return String(value || '').toLowerCase().split(/[\s,]+/).filter(function (t) { return t.charAt(0) === '-' && t.length > 1; }).map(function (t) { return t.slice(1); });
}
function adminBuiltIn(i, row) {
  var host = row.querySelector('.adm-c') || row, input = row.querySelector('.adm-in'), own = i.ownDomains || [], all = i.builtIn || [], open = false;
  var box = el('div', 'adm-built'), list = el('div', 'adm-built-l'), more = accText('button', 'btn sec small adm-built-more', '');
  box.appendChild(accText('b', 'adm-built-h', 'Built-in sites'));
  box.appendChild(list);
  more.type = 'button';
  var paint = function () {
    var out = adminLeftOut(input ? input.value : i.value);
    list.textContent = '';
    all.forEach(function (h, n) {
      var chip = accText('span', 'adm-chip' + (own.indexOf(h) >= 0 ? ' own' : '') + (out.indexOf(h) >= 0 ? ' removed' : ''), own.indexOf(h) >= 0 ? h + ' \u00b7 ' + tx('your website') : h);
      chip.title = out.indexOf(h) >= 0 ? tx('Left out by -{0}').replace('{0}', h) : own.indexOf(h) >= 0 ? tx('One of your websites') : '';
      chip.hidden = !open && n >= BUILT_SHOWN;
      list.appendChild(chip);
    });
    more.hidden = all.length <= BUILT_SHOWN;
    more.textContent = open ? tx('Show fewer') : tx('Show all {0}').replace('{0}', all.length);
    more.setAttribute('aria-expanded', String(open));
  };
  more.addEventListener('click', function () { open = !open; paint(); });
  if (input) input.addEventListener('input', paint);
  box.appendChild(more);
  host.appendChild(box);
  paint();
}
function adminOwnFrom(i, row) {
  var note = accText('p', 'adm-help adm-own-from', ''), hosts = (i.ownHosts || []).join(', ');
  note.textContent = i.ownFrom === 'setting' ? tx('In use: the sites set here') : i.ownFrom === 'website' ? tx('In use: from the Website integration ({0})').replace('{0}', hosts) : tx('In use: none yet. Set Your websites or add a Website integration');
  note.dataset.from = i.ownFrom;
  (row.querySelector('.adm-c') || row).appendChild(note);
}
var JIRA_FIELD_ROLES = {'jira.scope-field': 'scope', 'jira.points-field': 'points', 'jira.developer-field': 'developer', 'jira.sprint-field': 'sprint'};
function adminJiraField(i, row) {
  var input = row.querySelector('.adm-in'), host = row.querySelector('.adm-c');
  if (!input || input.tagName !== 'INPUT' || !host) return;
  var list = document.createElement('datalist'), box = el('div', 'adm-jira-field'), go = accText('button', 'btn sec small adm-jira-list', 'List Jira fields'), note = accText('p', 'adm-help adm-jira-note', ''), loaded = null;
  list.id = adminId(i.key) + '-fields';
  list.setAttribute('data-no-tx', '');
  note.setAttribute('data-no-tx', '');
  input.setAttribute('list', list.id);
  go.type = 'button';
  box.appendChild(list);
  box.appendChild(go);
  box.appendChild(note);
  host.insertBefore(box, host.querySelector('.adm-acts'));
  function named() {
    var hit = loaded && loaded.fields.filter(function (f) { return f.id === input.value.trim(); })[0];
    return hit ? hit.label : '';
  }
  function paint() {
    note.textContent = '';
    if (!loaded) return;
    var now = named();
    if (now) note.appendChild(document.createTextNode(tx('In Jira: {0}').replace('{0}', function () { return now; })));
    var s = loaded.suggest;
    if (s && s.id !== input.value.trim()) {
      if (now) note.appendChild(document.createTextNode(' \u00b7 '));
      note.appendChild(document.createTextNode(tx('Suggested: {0}').replace('{0}', function () { return s.label; }) + ' '));
      var use = accText('button', 'btn sec small adm-jira-use', tx('Use it'));
      use.type = 'button';
      use.addEventListener('click', function () { input.value = s.id; input.dispatchEvent(new Event('input', {bubbles: true})); paint(); });
      note.appendChild(use);
    }
    if (!now && !(s && s.id !== input.value.trim()) && input.value.trim()) note.textContent = tx('Not a field Jira lists: check the id');
  }
  async function fill(again) {
    if (loaded && again !== true) return;
    go.disabled = true;
    try {
      loaded = await adminApi('GET', '/api/admin/jira/fields?role=' + encodeURIComponent(JIRA_FIELD_ROLES[i.key]));
      list.textContent = '';
      loaded.fields.forEach(function (f) {
        var opt = document.createElement('option');
        opt.value = f.id;
        opt.label = f.label;
        opt.textContent = f.label;
        list.appendChild(opt);
      });
      go.textContent = tx('List again');
      go.disabled = false;
      paint();
    } catch (e) {
      loaded = null;
      go.disabled = false;
      note.textContent = tx(e && e.detail ? e.detail : 'The Jira fields could not be read; type the field id');
    }
  }
  go.addEventListener('click', function () { fill(true); });
  input.addEventListener('focus', fill);
  input.addEventListener('input', paint);
}
function adminRow(i, card) {
  var row = el('div', 'adm-row' + (i.source === 'admin' ? ' set' : '') + (card ? ' adm-field' : ''));
  row.dataset.key = i.key;
  row.dataset.find = (i.label + ' ' + i.key + ' ' + i.help + ' ' + Object.keys(i.ruleHelp || {}).map(function (k) { return k + ' ' + i.ruleHelp[k]; }).join(' ')).toLowerCase();
  row.innerHTML = !card ? admRowHtml(i) : '<div class="adm-l"><div class="adm-n"><b id="' + esc(adminId(i.key) + '-l') + '">' + esc(i.label) + '</b>' + adminSource(i) +
    (i.live ? '' : '<span class="tag k-warn" title="Read when the service starts: saved now, used after the next restart">after restart</span>') + '</div>' +
    '<p class="adm-help">' + esc(i.help) + '</p><code class="adm-key">' + esc(i.key) + '</code></div>' +
    '<div class="adm-c">' + adminControl(i) + (i.pluginTerms && i.pluginTerms.length ? '<p class="adm-help adm-plugin-terms">' + esc(tx('Also tagged by plug-ins (fixed, added to yours):')) + ' ' + i.pluginTerms.map(function (t) { return '<code>' + esc(t) + '</code>'; }).join(' ') + '</p>' : '') + '<div class="adm-acts">' + (card ? '' : '<button type="button" class="btn small adm-save" disabled>Save</button>') +
    (i.source === 'admin' ? '<button type="button" class="btn sec small adm-reset" title="' + esc('Drop the value set here and go back to ' + (i.base ? i.base : 'the default')) + '">Reset</button>' : '') +
    '<span class="adm-msg" role="status"></span></div></div>';
  if (i.builtIn && i.builtIn.length) adminBuiltIn(i, row);
  if (i.ownFrom) adminOwnFrom(i, row);
  if (JIRA_FIELD_ROLES[i.key]) adminJiraField(i, row);
  var chipIn = i.items && i.kind !== 'rules' ? row.querySelector('.adm-in') : null;
  if (chipIn && chipIn.tagName === 'INPUT') admChipsMount(chipIn, i.items, i.label);
  if (i.channelName) row.querySelector('.adm-c').insertBefore(accText('p', 'adm-help adm-channel-name', 'Posts to #' + i.channelName), row.querySelector('.adm-c .adm-acts'));
  if (i.problem) {
    var note = el('p', 'adm-help adm-problem');
    note.textContent = i.problem;
    row.querySelector('.adm-l').appendChild(note);
  }
  if (i.off) {
    row.classList.add('adm-off');
    row.querySelectorAll('input, select, textarea').forEach(function (inp) { inp.disabled = true; inp.setAttribute('aria-describedby', adminId(i.key) + '-off'); });
    var off = el('p', 'adm-help adm-off-why');
    off.id = adminId(i.key) + '-off';
    off.textContent = i.off;
    row.querySelector('.adm-l').appendChild(off);
  }
  admUnfold(row);
  if (!card) admSeg(row, i);
  var save = row.querySelector('.adm-save'), msg = row.querySelector('.adm-msg');
  row.querySelectorAll('input:not(.adm-chip-in), select, textarea').forEach(function (inp) { ['input', 'change'].forEach(function (ev) { inp.addEventListener(ev, function () { if (save) save.disabled = adminValue(row, i) === adminCurrent(i) || admChipsBad(row); msg.textContent = ''; }); }); });
  if (save) save.addEventListener('click', function () { adminSave(i, adminValue(row, i), save, msg); });
  var reset = row.querySelector('.adm-reset');
  if (reset) reset.addEventListener('click', function () { adminReset(i, reset, msg); });
  var owners = i.kind === 'tagrules' ? tagOwnersBox(i) : null;
  if (owners) {
    tagNameTint(row, owners.dataset.tag);
    var chip = accText('code', 'adm-tag-id', owners.dataset.tag);
    chip.title = tx('Tag id: use it in a route\'s Card tags');
    var nameBox = row.querySelector('.adm-n b');
    if (nameBox) nameBox.parentNode.insertBefore(chip, nameBox.nextSibling);
    row.dataset.find += ' ' + owners.dataset.tag.toLowerCase();
    row.querySelector('.adm-c').appendChild(owners);
    row.dataset.find += ' responsibles ' + tagOwnersOf(owners.dataset.tag).responsibles.map(function (p) { return p.name + ' ' + p.email; }).join(' ').toLowerCase();
  }
  return row;
}
var ADMIN_TAGS = null;
var TAG_RULES_KEY = /^tags\.([a-z0-9-]{1,30})\.rules$/;
function tagOwnersOf(id) {
  var list = ADMIN_TAGS && Array.isArray(ADMIN_TAGS.tags) ? ADMIN_TAGS.tags : [];
  return list.filter(function (t) { return t && t.id === id && Array.isArray(t.responsibles); })[0] || null;
}
function tagOwnersBox(i) {
  var m = TAG_RULES_KEY.exec(i.key), t = m && tagOwnersOf(m[1]);
  if (!t) return null;
  var edit = !!ADMIN_TAGS.canEdit, box = el('div', 'adm-owners'), lid = 'adm-own-' + t.id + '-l';
  box.dataset.tag = t.id;
  box.setAttribute('role', 'group');
  box.setAttribute('aria-labelledby', lid);
  var head = accText('span', 'adm-owners-l', 'Responsibles');
  head.id = lid;
  box.appendChild(head);
  var list = el('ul', 'adm-owner-list'), have = t.responsibles.map(function (p) { return p.email; });
  if (!t.responsibles.length) list.appendChild(accText('li', 'muted adm-owner-none', 'No one yet'));
  t.responsibles.forEach(function (p) {
    var li = el('li', 'tag plain adm-owner' + (p.stale ? ' adm-owner-stale' : ''));
    li.dataset.email = p.email;
    li.title = p.stale ? 'No longer an active user' : p.email;
    li.appendChild(accText('span', 'adm-owner-n', p.name || p.email));
    if (p.slack === 'unknown') {
      var warn = accText('span', 'adm-owner-warn', 'Not found in Slack: shown as plain text in the report');
      warn.title = p.email;
      li.appendChild(warn);
    }
    if (edit) {
      var x = accText('button', 'adm-owner-x', '\u00d7');
      x.type = 'button';
      x.title = 'Remove from the responsibles';
      x.setAttribute('aria-label', 'Remove ' + (p.name || p.email));
      x.addEventListener('click', function () { tagOwnersSave(i, t, have.filter(function (e) { return e !== p.email; }), box, 'Removed'); });
      li.appendChild(x);
    }
    list.appendChild(li);
  });
  box.appendChild(list);
  if (edit) box.appendChild(tagOwnerPicker(i, t, have, box));
  if (edit) box.appendChild(tagAutoSwitch(i, t, box));
  if (edit) box.appendChild(tagToneBox(i, t, box));
  if (t.autoAssign !== false && t.responsibles.length && t.autoReady === false) box.appendChild(accText('p', 'adm-help adm-owner-auto-note', 'Auto-assign needs a responsible with a Skopax account'));
  var said = accText('span', 'adm-msg adm-owner-msg');
  said.setAttribute('role', 'status');
  box.appendChild(said);
  return box;
}
var OWNER_PICK_ROWS = 8;
var OWNER_PICK_WAIT = 200;
var OWNER_PICK_AGAIN = 1500;
var OWNER_SOURCES = {skopax: 'Skopax', responsible: 'Responsible elsewhere', slack: 'Slack'};
var OWNER_SOURCE_ORDER = ['skopax', 'responsible', 'slack'];
var OWNER_EMAIL = /^[^\s@]+@[^\s@.]+(\.[^\s@.]+)+$/;
function ownerFold(s) { return String(s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase(); }
function ownerInitials(p) {
  var words = String(p.name || '').split(/[\s._-]+/).filter(Boolean);
  if (!words.length) words = [String(p.email || '?')];
  return (words[0].charAt(0) + (words.length > 1 ? words[words.length - 1].charAt(0) : '')).toUpperCase();
}
function ownerLocal(have) {
  var out = [], seen = {};
  var add = function (email, name, source) {
    email = String(email || '').toLowerCase();
    if (!email || seen[email] || have.indexOf(email) >= 0) return;
    seen[email] = true;
    out.push({email: email, name: name || email.split('@')[0], source: source});
  };
  (Array.isArray(ADMIN_TAGS.users) ? ADMIN_TAGS.users : []).forEach(function (u) {
    if (u && !u.disabled) add(u.email, u.name, 'skopax');
    else if (u && u.email) seen[String(u.email).toLowerCase()] = true;
  });
  (Array.isArray(ADMIN_TAGS.tags) ? ADMIN_TAGS.tags : []).forEach(function (x) { (x.responsibles || []).forEach(function (p) { if (!p.stale) add(p.email, p.name, 'responsible'); }); });
  return out;
}
function ownerMatches(list, q) {
  var want = ownerFold(q.trim());
  var hits = list.filter(function (p) { return !want || ownerFold(p.name).indexOf(want) >= 0 || ownerFold(p.email).indexOf(want) >= 0; });
  var starts = function (p) { return (ownerFold(p.name) + ' ' + ownerFold(p.email)).split(/[\s@._-]+/).some(function (w) { return want && w.indexOf(want) === 0; }) ? 0 : 1; };
  return hits.sort(function (a, b) {
    return starts(a) - starts(b) || OWNER_SOURCE_ORDER.indexOf(a.source) - OWNER_SOURCE_ORDER.indexOf(b.source) || (ownerFold(a.name) < ownerFold(b.name) ? -1 : ownerFold(a.name) > ownerFold(b.name) ? 1 : 0);
  });
}
function tagOwnerPicker(i, t, have, box) {
  var base = 'adm-own-' + t.id, add = el('div', 'adm-owner-add'), combo = el('div', 'adm-owner-combo');
  var pick = el('input', 'adm-owner-pick'), opts = el('ul', 'adm-owner-opts'), go = accText('button', 'btn sec small adm-owner-go', 'Add');
  var live = accText('span', 'adm-owner-live'), note = accText('span', 'adm-owner-slack', 'Slack names unavailable');
  var local = ownerLocal(have), remote = [], shown = [], active = -1, held = null, timer = null, again = null, seq = 0, ctrl = null, retried = false;
  opts.id = base + '-opts';
  opts.hidden = true;
  opts.setAttribute('role', 'listbox');
  opts.setAttribute('aria-label', 'Suggestions for responsibles');
  pick.type = 'text';
  pick.setAttribute('role', 'combobox');
  pick.setAttribute('aria-autocomplete', 'list');
  pick.setAttribute('aria-expanded', 'false');
  pick.setAttribute('aria-controls', opts.id);
  pick.setAttribute('aria-label', 'Add a responsible');
  pick.setAttribute('placeholder', 'Name or e-mail');
  pick.setAttribute('inputmode', 'email');
  pick.setAttribute('autocomplete', 'off');
  pick.setAttribute('autocapitalize', 'off');
  pick.setAttribute('spellcheck', 'false');
  live.setAttribute('aria-live', 'polite');
  note.hidden = true;
  go.type = 'button';
  go.disabled = true;
  var wanted = function () { return pick.value.trim().toLowerCase(); };
  var highlight = function (n) {
    active = n;
    [].forEach.call(opts.children, function (o, k) { o.setAttribute('aria-selected', k === n ? 'true' : 'false'); o.classList.toggle('on', k === n); });
    if (n >= 0 && opts.children[n]) { pick.setAttribute('aria-activedescendant', opts.children[n].id); opts.children[n].scrollIntoView({block: 'nearest'}); }
    else pick.removeAttribute('aria-activedescendant');
  };
  var close = function () {
    opts.hidden = true;
    pick.setAttribute('aria-expanded', 'false');
    highlight(-1);
  };
  var picked = function () { return active >= 0 && shown[active] ? shown[active].email : held; };
  var typedIn = function () {
    var v = wanted();
    if (!v) return;
    if (!OWNER_EMAIL.test(v)) {
      var said = box.querySelector('.adm-owner-msg');
      said.textContent = 'Pick a suggestion or type a full e-mail';
      said.className = 'adm-msg adm-owner-msg bad';
      return;
    }
    choose(v);
  };
  var choose = function (email) {
    if (!email) return;
    if (have.indexOf(email) >= 0) { pick.value = ''; go.disabled = true; close(); return; }
    clearTimeout(timer);
    clearTimeout(again);
    seq++;
    close();
    tagOwnersSave(i, t, have.concat([email]), box, 'Added');
  };
  var row = function (p, k, fresh) {
    var o = el('li', 'adm-owner-opt' + (fresh ? ' adm-owner-new' : ''));
    o.id = base + '-opt-' + k;
    o.dataset.email = p.email;
    o.setAttribute('role', 'option');
    o.setAttribute('aria-selected', 'false');
    if (fresh) {
      o.appendChild(accText('span', 'adm-owner-nomatch', 'No match: press Enter to add'));
      o.appendChild(document.createTextNode(' '));
      var e = accText('span', 'adm-owner-oe', p.email);
      e.setAttribute('data-no-tx', '');
      o.appendChild(e);
    } else {
      var av = accText('span', 'adm-owner-av', ownerInitials(p)), who = el('span', 'adm-owner-who'), n = accText('span', 'adm-owner-on', p.name || p.email), m = accText('span', 'adm-owner-oe', p.email);
      av.setAttribute('aria-hidden', 'true');
      n.setAttribute('data-no-tx', '');
      m.setAttribute('data-no-tx', '');
      who.appendChild(n);
      who.appendChild(m);
      o.appendChild(av);
      o.appendChild(who);
      o.appendChild(accText('span', 'adm-owner-src', OWNER_SOURCES[p.source] || p.source));
    }
    o.addEventListener('mousedown', function (e) { e.preventDefault(); });
    o.addEventListener('click', function () { choose(p.email); });
    return o;
  };
  var render = function (force) {
    var q = pick.value.trim(), keep = active >= 0 && shown[active] ? shown[active].email : null;
    if (!q && !force) { shown = []; opts.textContent = ''; close(); live.textContent = ''; return; }
    var seen = {}, pool = local.concat(remote).filter(function (p) { if (seen[p.email] || have.indexOf(p.email) >= 0) return false; seen[p.email] = true; return true; });
    shown = ownerMatches(pool, q).slice(0, OWNER_PICK_ROWS);
    var typed = wanted(), fresh = !shown.length && OWNER_EMAIL.test(typed) && have.indexOf(typed) < 0;
    opts.textContent = '';
    shown.forEach(function (p, k) { opts.appendChild(row(p, k, false)); });
    if (fresh) { shown = [{email: typed}]; opts.appendChild(row(shown[0], 0, true)); }
    opts.hidden = !shown.length;
    pick.setAttribute('aria-expanded', shown.length ? 'true' : 'false');
    var back = -1;
    shown.forEach(function (p, k) { if (p.email === keep) back = k; });
    highlight(back);
    live.textContent = fresh ? '' : 'Suggestions: ' + shown.filter(function (p) { return p.name; }).length;
  };
  var ask = function () {
    var q = pick.value.trim(), mine = ++seq;
    if (ctrl) ctrl.abort();
    ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    adminApi('GET', '/api/admin/people?q=' + encodeURIComponent(q), undefined, ctrl ? ctrl.signal : undefined).then(function (r) {
      if (mine !== seq || !box.isConnected || !r) return;
      remote = (Array.isArray(r.people) ? r.people : []).filter(function (p) { return p && p.email; }).map(function (p) { return {email: String(p.email).toLowerCase(), name: p.name, source: p.source}; });
      note.hidden = r.slack !== 'unavailable';
      if (r.slack === 'loading' && !retried) { retried = true; again = setTimeout(function () { if (box.isConnected && pick.value.trim() === q) ask(); }, OWNER_PICK_AGAIN); }
      if (document.activeElement === pick) render();
    }, function () {});
  };
  pick.addEventListener('input', function () {
    held = null;
    go.disabled = !wanted();
    remote = [];
    clearTimeout(timer);
    seq++;
    render();
    if (pick.value.trim().length >= 2) timer = setTimeout(ask, OWNER_PICK_WAIT);
  });
  pick.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (opts.hidden) { render(true); if (!shown.length) return; }
      var n = shown.length, step = e.key === 'ArrowDown' ? 1 : -1;
      highlight(active < 0 ? (step > 0 ? 0 : n - 1) : (active + step + n) % n);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (picked()) choose(picked()); else typedIn();
    } else if (e.key === 'Escape') {
      if (!opts.hidden) { e.preventDefault(); close(); }
    }
  });
  pick.addEventListener('blur', function () { held = picked(); close(); });
  pick.addEventListener('focus', function () { held = null; });
  go.addEventListener('mousedown', function (e) { e.preventDefault(); });
  go.addEventListener('click', function () { if (picked()) choose(picked()); else typedIn(); });
  combo.appendChild(pick);
  combo.appendChild(opts);
  add.appendChild(combo);
  add.appendChild(go);
  add.appendChild(live);
  add.appendChild(note);
  return add;
}
function tagAutoSwitch(i, t, box) {
  var wrap = el('label', 'adm-owner-auto'), sw = el('input');
  sw.type = 'checkbox';
  sw.checked = t.autoAssign !== false;
  wrap.appendChild(sw);
  wrap.appendChild(accText('span', null, 'Auto-assign new cards'));
  sw.addEventListener('change', function () { tagAutoSave(i, t, sw.checked, box); });
  return wrap;
}
function tagNameTint(row, id) {
  var b = row && row.querySelector('.adm-n b');
  if (!b) return;
  b.className = b.className.split(/\s+/).filter(function (c) { return c && c !== 'tcol' && c.indexOf('th-') !== 0; }).join(' ');
  b.classList.add('adm-tagname');
  tagClass(id).split(' ').forEach(function (c) { b.classList.add(c); });
}
function tagToneBox(i, t, box) {
  var g = el('div', 'adm-tone'), cur = typeof t.color === 'number' ? t.color : null, opts = [null].concat(TAG_PALETTE.map(function (p, n) { return n; }));
  g.setAttribute('role', 'radiogroup');
  g.setAttribute('aria-label', 'Colour of ' + (t.label || t.id));
  g.appendChild(accText('span', 'adm-owners-l', 'Colour'));
  var radios = opts.map(function (n) {
    var hue = n === null ? tagHue(t.id) : n, r = el('button', 'adm-tone-s th-' + hue + (n === null ? ' adm-tone-def' : ''));
    r.type = 'button';
    r.setAttribute('role', 'radio');
    r.dataset.tone = n === null ? '' : String(n);
    var name = n === null ? 'Default (' + TAG_PALETTE[hue].name + ')' : TAG_PALETTE[n].name;
    r.setAttribute('aria-label', name);
    r.title = n === null ? name + ': picked from the tag id, the same everywhere' : name;
    r.setAttribute('aria-checked', String(n === cur));
    r.tabIndex = n === cur ? 0 : -1;
    if (n === null) r.textContent = 'A';
    r.addEventListener('click', function () { if (n !== cur) tagToneSave(i, t, n, box); });
    r.addEventListener('keydown', function (e) {
      var d = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0;
      if (!d) return;
      e.preventDefault();
      var next = radios[(radios.indexOf(r) + d + radios.length) % radios.length];
      next.focus();
      next.click();
    });
    g.appendChild(r);
    return r;
  });
  return g;
}
async function tagToneSave(i, t, color, box) {
  var said = box.querySelector('.adm-owner-msg');
  box.querySelectorAll('button, select, input').forEach(function (c) { c.disabled = true; });
  said.textContent = 'Saving\u2026';
  said.className = 'adm-msg adm-owner-msg';
  try {
    var r = await adminApi('PUT', '/api/admin/tags/' + encodeURIComponent(t.id) + '/color', {color: color});
    ADMIN_TAGS.tags = ADMIN_TAGS.tags.map(function (x) { return x.id === r.id ? r : x; });
    if (typeof r.color === 'number') TAG_TONE_SET[r.id] = r.color; else delete TAG_TONE_SET[r.id];
    bumpRender();
    if (typeof renderAnoms === 'function') renderAnoms();
    tagNameTint(box.closest('.adm-row'), r.id);
    var fresh = tagOwnersBox(i);
    if (!fresh) return;
    box.replaceWith(fresh);
    var note = fresh.querySelector('.adm-owner-msg');
    note.textContent = 'Colour saved';
    note.className = 'adm-msg adm-owner-msg ok';
    var back = fresh.querySelector('.adm-tone [aria-checked="true"]');
    if (back) back.focus();
  } catch (e) {
    box.querySelectorAll('button, select, input').forEach(function (c) { c.disabled = c.classList.contains('adm-owner-go') && !((box.querySelector('.adm-owner-pick') || {}).value || '').trim(); });
    said.textContent = e && e.detail ? e.detail : 'Colour not saved, try again';
    said.className = 'adm-msg adm-owner-msg bad';
  }
}
async function tagAutoSave(i, t, on, box) {
  var said = box.querySelector('.adm-owner-msg');
  box.querySelectorAll('button, select, input').forEach(function (c) { c.disabled = true; });
  said.textContent = 'Saving\u2026';
  said.className = 'adm-msg adm-owner-msg';
  try {
    var r = await adminApi('PUT', '/api/admin/tags/' + encodeURIComponent(t.id) + '/auto-assign', {on: on});
    ADMIN_TAGS.tags = ADMIN_TAGS.tags.map(function (x) { return x.id === r.id ? r : x; });
    var fresh = tagOwnersBox(i);
    if (!fresh) return;
    box.replaceWith(fresh);
    var note = fresh.querySelector('.adm-owner-msg');
    note.textContent = on ? 'Auto-assign on' : 'Auto-assign off';
    note.className = 'adm-msg adm-owner-msg ok';
    var back = fresh.querySelector('.adm-owner-auto input');
    if (back) back.focus();
  } catch (e) {
    box.querySelectorAll('button, select, input').forEach(function (c) { c.disabled = c.classList.contains('adm-owner-go') && !((box.querySelector('.adm-owner-pick') || {}).value || '').trim(); });
    var back2 = box.querySelector('.adm-owner-auto input');
    if (back2) back2.checked = !on;
    said.textContent = e && e.detail ? e.detail : 'Auto-assign not saved, try again';
    said.className = 'adm-msg adm-owner-msg bad';
  }
}
async function tagOwnersSave(i, t, emails, box, done) {
  var said = box.querySelector('.adm-owner-msg');
  box.querySelectorAll('button, select, input').forEach(function (c) { c.disabled = true; });
  said.textContent = 'Saving\u2026';
  said.className = 'adm-msg adm-owner-msg';
  try {
    var r = await adminApi('PUT', '/api/admin/tags/' + encodeURIComponent(t.id) + '/responsibles', {emails: emails});
    ADMIN_TAGS.tags = ADMIN_TAGS.tags.map(function (x) { return x.id === r.id ? r : x; });
    try { ADMIN_HIST = await adminApi('GET', '/api/admin/history?limit=' + ADMIN_HIST_MAX); } catch (e) {}
    var fresh = tagOwnersBox(i);
    if (!fresh) return;
    box.replaceWith(fresh);
    var note = fresh.querySelector('.adm-owner-msg');
    note.textContent = done;
    note.className = 'adm-msg adm-owner-msg ok';
    var back = fresh.querySelector('.adm-owner-pick') || document.getElementById('adm-own-' + t.id + '-l');
    if (back) { if (!back.matches('input')) back.tabIndex = -1; back.focus(); }
    renderChanges();
  } catch (e) {
    box.querySelectorAll('button, select, input').forEach(function (c) { c.disabled = c.classList.contains('adm-owner-go') && !((box.querySelector('.adm-owner-pick') || {}).value || '').trim(); });
    said.textContent = e && e.detail ? e.detail : 'Responsibles not saved, try again';
    said.className = 'adm-msg adm-owner-msg bad';
  }
}
function tagLabelOf(id) {
  var t = tagOwnersOf(id);
  return t ? t.label : id;
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
    var name = h.access ? 'People and access' : h.route ? 'Teams route ' + h.key.slice(6) : h.source ? 'Agent ' + h.key.slice(7) : h.secret ? 'Secret ' + h.key : h.talk ? TALK_NAMES[h.key] || 'Small talk' : h.owners ? 'Responsibles of ' + tagLabelOf(h.tag) : label[h.current || h.key] || h.key;
    var shownR = function (v) { return v == null ? '<span class="muted">' + (h.route || h.owners ? 'none' : h.source || h.secret ? 'not set' : 'config / default') + '</span>' : shown(v); };
    var change = h.access ? esc(h.text) : h.seen ? '<span class="muted">workflow link shown</span>' : shownR(h.old) + ' → ' + shownR(h.new);
    return '<tr><td>' + esc(dayhm(h.at)) + '</td><td>' + esc(draftBy(h.by)) + '</td><td title="' + esc(h.key) + '">' + esc(name) + '</td><td class="adm-ch">' + change + '</td>' +
      '<td>' + (h.access || h.route || h.source || h.secret || h.talk || h.owners || h.legacy ? '' : '<button type="button" class="btn sec small adm-revert" data-id="' + esc(h.id) + '" title="Put back the value before this change">Revert</button>') + '</td></tr>';
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
    d.querySelectorAll('.adm-multi').forEach(function (m) { m.hidden = !m.querySelector('.adm-row:not([hidden])'); });
    d.hidden = !n;
    if (ADMIN_FIND && n) d.open = true;
    else if (!ADMIN_FIND && d.dataset.group) d.open = ADMIN_FOLD[d.dataset.group] === 1;
  });
  admFilterCards(document.getElementById('admin'), ADMIN_FIND);
}

function adminRowEl(key) {
  return document.querySelector(['#admin', '#feat-other', '#ai-rows', '#ai', '#acc-mcp', '#acc-sso .sso-roles'].map(function (box) { return box + ' .adm-row[data-key="' + key + '"]'; }).join(', '));
}
function adminKeep(box, saved, paint) {
  var keep = document.activeElement && box.contains(document.activeElement) ? document.activeElement.id : null, edits = saved ? unsaved(box, saved) : [];
  paint();
  restoreUnsaved(box, edits);
  if (keep && document.getElementById(keep)) document.getElementById(keep).focus();
}
function accCardEdits(box) {
  return box ? [].map.call(box.querySelectorAll('.adm-row[data-key]'), function (row) {
    var i = adminItems().filter(function (x) { return x.key === row.dataset.key; })[0];
    return i && adminValue(row, i) !== adminCurrent(i) ? {i: i, value: adminValue(row, i)} : null;
  }).filter(Boolean) : [];
}
async function accSaveFields(edits) {
  for (var n = 0; n < edits.length; n++) {
    var e = edits[n];
    try {
      var r = await adminApi('PUT', '/api/admin/settings', {key: e.i.key, value: e.value});
      ADMIN.items = ADMIN.items.map(function (x) { return x.key === r.item.key ? r.item : x; });
      e.said = r.item.live ? 'Saved, live within ' + ((ADMIN && ADMIN.refreshSeconds) || 15) + ' s on every pod' : 'Saved, used after the next restart';
    } catch (err) {
      e.bad = true;
      e.said = err && err.detail ? err.detail : 'Not saved, try again';
    }
  }
  try { ADMIN_HIST = await adminApi('GET', '/api/admin/history?limit=' + ADMIN_HIST_MAX); } catch (err) {}
  adminPaintAll(SSO_KEEP);
  accShowSaid(edits);
  return edits.filter(function (e) { return e.bad; });
}
function accShowSaid(edits) {
  edits.forEach(function (e) {
    var row = adminRowEl(e.i.key), m = row && row.querySelector('.adm-msg');
    if (m && e.said) { m.textContent = e.said; m.className = 'adm-msg ' + (e.bad ? 'bad' : 'ok'); }
  });
}
function accNotSaved(failed) { return 'Not saved: ' + failed.map(function (e) { return e.i.label || e.i.key; }).join(', '); }
function adminPaintAll(saved) {
  renderAdmin(saved);
  renderFeatOther(saved);
  renderAiRows(saved);
  renderInstances(saved);
  renderMcpRows(saved);
  renderSsoRoles(saved);
  renderChanges();
}
async function adminDone(r, said, reverted) {
  ADMIN.items = ADMIN.items.map(function (x) { return x.key === r.item.key ? r.item : x; });
  try { ADMIN_HIST = await adminApi('GET', '/api/admin/history?limit=' + ADMIN_HIST_MAX); } catch (e) {}
  adminPaintAll(r.item.key);
  if (r.item.key === 'investigate.provider') { await loadAi(); adminNotices(); }
  if (r.item.key === AIC_PRIVATE_KEY && typeof aicPrivateDone === 'function') await aicPrivateDone(r.item);
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
      admCards(d, by[g]);
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
    admCards(d, items);
    d.open = ADMIN_FOLD[OTHER_SWITCHES] === 1;
    d.addEventListener('toggle', function () { ADMIN_FOLD[OTHER_SWITCHES] = d.open ? 1 : 0; });
    box.appendChild(d);
  });
}
var AI_PAIRS = {'investigate.model': ['investigate.effort', 'Card model and effort'], 'investigate.slack-model': ['investigate.slack-effort', 'Slack model and effort'],
  'investigate.gate-model': ['investigate.gate-effort', 'Topic gate model and effort'], 'chat.model': ['chat.effort', 'Chat model and effort']};
function aiPair(model, effort, title) {
  var pair = el('div', 'adm-pair'), id = adminId(model.key) + '-pair';
  pair.setAttribute('role', 'group');
  pair.setAttribute('aria-labelledby', id);
  pair.dataset.pair = model.key;
  var h = accText('h4', 'adm-pair-h', title);
  h.id = id;
  pair.appendChild(h);
  pair.appendChild(adminRow(model));
  pair.appendChild(adminRow(effort));
  if (model.key === 'investigate.model') pair.appendChild(accText('p', 'adm-help adm-model-summary', cardModelSummary()));
  return pair;
}
function adminValueOf(key) {
  var i = adminItems().filter(function (x) { return x.key === key; })[0];
  return i ? String(i.value || '') : '';
}
function cardModelItem() { return adminItems().filter(function (x) { return x.key === 'investigate.model'; })[0] || {}; }
function cardModelProviderOf(model) { return (cardModelItem().modelProviders || {})[model] || null; }
function cardModelKeyed(owner) { return (cardModelItem().keyedProviders || []).indexOf(owner) >= 0; }
function cardModelActive() {
  var item = cardModelItem(), model = String(item.value || ''), provider = aiProvider(), owner = cardModelProviderOf(model);
  if (!owner || owner === provider || (item.source !== 'default' && cardModelKeyed(owner))) return model;
  return (item.cardDefaults || {})[provider] || model;
}
function cardModelFor(override) {
  var owner = cardModelProviderOf(override);
  return override && owner && (owner === aiProvider() || cardModelKeyed(owner)) ? override : cardModelActive();
}
function cardModelSummary() {
  var rows = typeof aiuSummary === 'function' ? aiuSummary() : null;
  if (rows) return rows;
  return 'Which model runs: Critical cards ' + cardModelFor(adminValueOf('investigate.model-critical')) + ' \u00b7 High cards ' + cardModelFor(adminValueOf('investigate.model-high')) +
    ' \u00b7 Warning and Info cards ' + cardModelActive();
}
function renderAiRows(saved) {
  var box = document.getElementById('ai-rows');
  if (!box || !ADMIN) return;
  adminKeep(box, saved, function () {
    var covered = aiuCovered(), items = aiItems().filter(function (i) { return !covered[i.key]; });
    box.textContent = '';
    if (AIU_FAILED) box.appendChild(aiuFailed());
    if (AIUSES && AIUSES.length) box.appendChild(aiuSection());
    if (!items.length) return;
    var s = el('section', 'adm-grp ai-grp');
    s.dataset.group = AI_GROUP;
    s.appendChild(accText('h3', null, 'Investigator settings'));
    admCards(s, items);
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
  var hit = [].filter.call(panel.querySelectorAll(f ? '.feat' : tab === 'access' ? '.adm-row, .sso-f, .sso-sw' : '.adm-row, .aiu-row'), function (n) { return f ? n.dataset.id === f.id : n.dataset.key === want.key; })[0];
  if (!hit) return;
  var part = tab === 'access' ? hit.closest('.acc-part') : null;
  if (part) accSelect(part.id.slice(4), false);
  var fold = hit.closest('details');
  if (fold) fold.open = true;
  hit.classList.add('adm-hit');
  setTimeout(function () { hit.classList.remove('adm-hit'); }, ADMIN_HIT_MS);
  hit.scrollIntoView({block: 'center'});
  var ctl = hit.classList.contains('aiu-row') ? hit.querySelector('select.aiu-in') : null;
  if (ctl && !ctl.disabled) { ctl.focus({preventScroll: true}); return; }
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
var AI_KINDS = [{id: 'anthropic', name: 'Anthropic', path: '/api/admin/anthropic-key', head: 'ai-key-h', input: 'ai-key-in', cls: 'ai-key'},
  {id: 'openai', name: 'OpenAI', path: '/api/admin/openai-key', head: 'oai-key-h', input: 'oai-key-in', cls: 'ai-key ai-openai'}];
function aiKind(id) { return AI_KINDS.filter(function (k) { return k.id === id; })[0] || AI_KINDS[0]; }
function aiStatus(kind) { return kind.id === 'openai' ? OAIKEY : AIKEY; }
function aiSetStatus(kind, s) { if (kind.id === 'openai') OAIKEY = s; else AIKEY = s; }
function aiProvider() {
  var i = (ADMIN && ADMIN.items || []).filter(function (x) { return x.key === 'investigate.provider'; })[0];
  return i && i.value === 'openai' ? 'openai' : 'anthropic';
}
function aiMsg(card) { return (card || document.querySelector('#ai .ai-key')).querySelector('.ai-msg'); }
function aiSay(text, bad, card) {
  var msg = card ? aiMsg(card) : document.querySelector('#ai .ai-msg');
  if (!msg) return;
  msg.textContent = text || '';
  msg.className = 'adm-msg ai-msg' + (bad ? ' bad' : text ? ' ok' : '');
}
function paintAiKey(said, saidFor) {
  var box = document.getElementById('ai');
  if (!box || !AIKEY) return;
  box.textContent = '';
  var list = accText('ul', 'ai-providers'), active = aiProvider();
  AI_KINDS.forEach(function (kind) {
    var s = aiStatus(kind);
    if (!s) return;
    var row = accText('li', 'ai-provider');
    var card = s.failed ? aiKeyFailed(kind) : aiKeyCard(kind, s, kind.id === active);
    row.appendChild(card);
    list.appendChild(row);
    if (said && (saidFor || 'anthropic') === kind.id) aiSay(said, false, card);
  });
  box.appendChild(list);
  aicPaint(box, said, saidFor);
  aivPaint(box);
}
function aiKeyFailed(kind) {
  var card = accText('section', kind.cls);
  card.dataset.provider = kind.id;
  card.setAttribute('aria-labelledby', kind.head);
  var h = accText('h3', null, kind.name + ' API key');
  h.id = kind.head;
  var head = accText('div', 'ai-head');
  head.appendChild(h);
  card.appendChild(head);
  var bad = accText('p', 'adm-msg ai-msg bad', 'The ' + kind.name + ' key did not load; open the tab again.');
  bad.setAttribute('role', 'status');
  card.appendChild(bad);
  return card;
}
function aiKeyCard(kind, s, active) {
  var card = accText('section', kind.cls);
  card.dataset.provider = kind.id;
  card.setAttribute('aria-labelledby', kind.head);
  var head = accText('div', 'ai-head'), h = accText('h3', null, kind.name + ' API key');
  h.id = kind.head;
  head.appendChild(h);
  head.appendChild(accText('span', 'tag ' + (s.using ? 'k-ok' : 'k-warn'), s.using === 'admin' ? 'set here' : s.using === 'config' ? 'from the configuration' : 'not set'));
  if (active) head.appendChild(accText('span', 'tag k-act ai-active', 'Default provider'));
  card.appendChild(head);
  var line = accText('div', 'ai-line');
  line.appendChild(accText('p', 'ai-state', s.using === 'admin' ? 'Using the key set here' : s.using === 'config' ? s.note : 'No key'));
  if (s.admin && s.updatedBy) line.appendChild(accText('p', 'adm-help ai-by', 'Set by ' + draftBy(s.updatedBy) + (s.updatedAt ? ' ' + dayhm(s.updatedAt) : '')));
  card.appendChild(line);
  var field = accText('div', 'ai-field'), label = accText('label', null, s.using ? 'New key' : 'Key');
  label.setAttribute('for', kind.input);
  var input = document.createElement('input');
  input.id = kind.input;
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
  var entry = accText('div', 'ai-entry');
  entry.appendChild(input);
  field.appendChild(entry);
  card.appendChild(field);
  input.placeholder = 'Paste a new key';
  var acts = accText('div', 'adm-acts ai-acts');
  var saveB = accText('button', 'btn small ai-save', 'Save'), testB = accText('button', 'btn sec small ai-test', 'Test connection');
  saveB.type = testB.type = 'button';
  saveB.disabled = true;
  testB.disabled = AI_TESTING[kind.id];
  entry.appendChild(saveB);
  entry.appendChild(testB);
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
  var say = function (text, bad) { aiSay(text, bad, card); };
  input.addEventListener('input', function () { saveB.disabled = !input.value.trim(); say(''); });
  saveB.addEventListener('click', async function () {
    var key = input.value.trim();
    if (!key) return;
    saveB.disabled = true;
    say('Saving\u2026');
    try {
      var r = await adminApi('PUT', kind.path, {key: key});
      input.value = '';
      aiSetStatus(kind, keyStatus(r));
      paintAiKey('Saved', kind.id);
      adminNotices();
    } catch (e) {
      say(e && e.detail ? e.detail : 'Not saved, try again', true);
      saveB.disabled = !input.value.trim();
    }
  });
  testB.addEventListener('click', async function () {
    if (AI_TESTING[kind.id]) return;
    var typed = input.value.trim();
    AI_TESTING[kind.id] = true;
    testB.disabled = true;
    say('Testing\u2026');
    try {
      var r = await adminApi('POST', kind.path + '/test', typed ? {key: typed} : {});
      say((r && r.ok ? '\u2713 ' : '') + String((r && r.summary) || '') + ' \u00b7 ' + Number((r && r.ms) || 0) + ' ms', !(r && r.ok));
    } catch (e) {
      say(e && e.detail ? e.detail : 'The test could not run', true);
    }
    AI_TESTING[kind.id] = false;
    var now = document.querySelector('#ai [data-provider="' + kind.id + '"] .ai-test');
    if (now) now.disabled = false;
  });
  if (remove) remove.addEventListener('click', async function () {
    var ask = window.__skopaxConfirm;
    if (typeof ask !== 'function') return;
    var ok = await ask('Remove the ' + kind.name + ' API key?', s.config ? 'The key set here is deleted, and the key from the configuration is used again.' : 'The key set here is deleted. The investigator, the chat and the self-review have no ' + kind.name + ' key until one is set again.', 'Remove key');
    if (!ok) { if (remove.isConnected) remove.focus(); return; }
    remove.disabled = true;
    say('Removing\u2026');
    try {
      await adminApi('DELETE', kind.path);
    } catch (e) {
      remove.disabled = false;
      say(e && e.detail ? e.detail : 'Not removed, try again', true);
      return;
    }
    adminNotices();
    loadAi('Removed', kind.id);
  });
  return card;
}

var ACCESS = null, ACCESS_SEQ = 0, ACCESS_N = null, ACCESS_ROLES = ['viewer', 'operator', 'admin'], ACCESS_IDP = ['claim', 'resolver'], ACCESS_LINKS = ['pending', 'bound', 'expired'];
var ACCESS_PW_MIN = 12, ACCESS_PW_MAX = 256, ACCESS_EMAIL = /^[^@\s]{1,64}@[^@\s]{1,189}$/, ACCESS_ROLE_LABELS = {viewer: 'Viewer', operator: 'Operator', admin: 'Admin'};
var ACCESS_FIND = '', ACCESS_ROLE_F = '', ACCESS_FAIL = {users: false, tokens: false}, ACCESS_MENU_WIRED = false, ACCESS_SOON_DAYS = 7, ACCESS_DAY_MS = 86400000;
var ACCESS_SECTIONS = [['people', 'People', 'people'], ['sso', 'Single sign-on', 'single sign-on'], ['tokens', 'API tokens', 'API tokens'], ['mcp', 'MCP sign-in', 'MCP sign-in']];
var ACCESS_TAB = 'people', ACCESS_TAB_KEYS = {ArrowRight: 1, ArrowLeft: -1, Home: 'first', End: 'last'};
var ACCESS_USER_HEADS = ['Person', 'Role', 'Sign-in', 'Last sign-in', 'Disabled', 'SSO link', ''], ACCESS_TOKEN_HEADS = ['Name', 'Role', 'Token', 'Created by', 'Created', 'Last used', 'Expires', ''];
var ACCESS_EXPIRY = [['', 'Never'], ['7', '7 days'], ['30', '30 days'], ['90', '90 days'], ['365', '1 year']];
var ACCESS_LAST_ADMIN = 'The last admin keeps the admin role and stays enabled: make someone else an admin first';
var ACCESS_LAST_PW_ADMIN = 'The last active admin with a password keeps it, so there is a way in when SSO fails: give another admin a password first';
var ACCESS_LAST_PW_ROLE = 'The last active admin with a password stays an enabled admin, so there is a way in when SSO fails: give another admin a password first';
var ACCESS_ADD_ID = 'acc-add-user', ACCESS_ADD_FIELDS = ['email', 'name', 'role', 'sso', 'password'], ACCESS_SET_PW_NOTE = 'Give this person a password: they become a local account, and their SSO link waits 7 days for the next SSO sign-in', ACCESS_NEED_PW = 'Enter a password of ' + ACCESS_PW_MIN + '-' + ACCESS_PW_MAX + ' characters';
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
  var held = function (p) { return p.then(function (v) { return {ok: v}; }, function (e) { return {err: e || {}}; }); };
  try {
    accShell(box);
    var r = await Promise.all([held(adminApi('GET', '/api/admin/users')), held(adminApi('GET', '/api/admin/tokens')), adminApi('GET', '/api/admin/sso').catch(adminNone),
      adminApi('GET', '/api/admin/settings').catch(adminNone)]);
    if (seq !== ACCESS_SEQ) return;
    if (adminDenied(r[0].err) || adminDenied(r[1].err)) throw r[0].err && adminDenied(r[0].err) ? r[0].err : r[1].err;
    ACCESS_FAIL = {users: !Array.isArray(r[0].ok), tokens: !Array.isArray(r[1].ok)};
    ACCESS = {users: ACCESS_FAIL.users ? [] : r[0].ok, tokens: ACCESS_FAIL.tokens ? [] : r[1].ok};
    SSO = r[2] ? ssoView(r[2]) : null;
    if (r[3]) ADMIN = r[3];
    MCP_FAILED = !r[3];
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
  try { ACCESS = await accFetch(); ACCESS_FAIL = {users: false, tokens: false}; return true; } catch (e) { return false; }
}
function accStale(fresh, sec) {
  var box = document.getElementById('acc-' + (sec || 'people'));
  var at = box && box.querySelector('.acc-sec');
  if (fresh || !at) return;
  at.insertBefore(accText('p', 'adm-msg bad acc-stale', 'Could not refresh the list; open the tab again to see the latest.'), at.firstChild);
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
  if (!msg) return;
  accMsg(msg, text, bad);
  if (text && !bad && msg.closest && msg.closest('.acc-ctl')) setTimeout(function () { if (msg.textContent === text) accMsg(msg, '', false); }, ACC_NOTE_MS);
}
var ACC_NOTE_MS = 5000;
function clearAccessSecret() {
  var slot = document.querySelector('#access .acc-secret-slot');
  if (slot) slot.textContent = '';
  var typed = document.getElementById('sso-in-client-secret');
  if (typed) typed.value = '';
}
function accShell(box) {
  if (box.querySelector('#acc-people')) return;
  box.textContent = '';
  var page = accText('div', 'acc-page'), nav = accText('div', 'acc-nav'), main = accText('div', 'acc-main');
  nav.setAttribute('role', 'tablist');
  nav.setAttribute('aria-label', 'Access sections');
  ACCESS_SECTIONS.forEach(function (s) {
    var b = accText('button', 'acc-nav-b'), dot = accText('span', 'acc-dot');
    b.type = 'button';
    b.id = 'acc-tab-' + s[0];
    b.dataset.sec = s[0];
    b.setAttribute('role', 'tab');
    b.setAttribute('aria-controls', 'acc-' + s[0]);
    b.appendChild(accText('span', 'acc-nav-l', s[1]));
    b.appendChild(accText('span', 'count acc-nav-n', ''));
    dot.hidden = true;
    b.appendChild(dot);
    b.addEventListener('click', function () { accSelect(s[0], true); });
    nav.appendChild(b);
    var part = accText('div', 'acc-part acc-' + s[0]);
    part.id = 'acc-' + s[0];
    part.setAttribute('role', 'tabpanel');
    part.setAttribute('aria-labelledby', 'acc-tab-' + s[0]);
    part.appendChild(accText('p', 'muted acc-loading', 'Loading ' + s[2] + '\u2026'));
    main.appendChild(part);
  });
  nav.addEventListener('keydown', accTabKey);
  page.appendChild(nav);
  page.appendChild(main);
  box.appendChild(page);
  accTabsPaint();
  if (!ACCESS_MENU_WIRED) {
    ACCESS_MENU_WIRED = true;
    document.addEventListener('click', function (e) { if (!e.target.closest || !e.target.closest('.acc-menu-w')) accMenuClose(null, false); });
    window.addEventListener('scroll', accMenuFollow, true);
    window.addEventListener('resize', function () { accMenuClose(null, false); });
  }
}
function renderAccess() {
  var box = document.getElementById('access');
  if (!box || !ACCESS) return;
  ACCESS_SEQ++;
  accShell(box);
  var back = accFocusKey();
  renderPeople();
  renderTokens();
  accNavPaint();
  if (!ACCESS_FAIL.users) markAccessCount(ACCESS.users.length);
  accFocusBack(back);
}
function accKnown(sec) { return ACCESS_SECTIONS.some(function (s) { return s[0] === sec; }); }
function accTabsPaint() {
  ACCESS_SECTIONS.forEach(function (s) {
    var on = s[0] === ACCESS_TAB, b = document.getElementById('acc-tab-' + s[0]), part = document.getElementById('acc-' + s[0]);
    if (b) { b.setAttribute('aria-selected', String(on)); b.tabIndex = on ? 0 : -1; }
    if (part) part.hidden = !on;
  });
}
function accWant(sec) {
  ACCESS_TAB = accKnown(sec) ? sec : 'people';
  accTabsPaint();
}
function accSelect(sec, focus) {
  if (!accKnown(sec)) return;
  ACCESS_TAB = sec;
  accTabsPaint();
  if (focus) { var b = document.getElementById('acc-tab-' + sec); if (b) b.focus(); }
  if (typeof curTab !== 'undefined' && curTab === 'access' && typeof markTab === 'function') markTab();
}
function accTabKey(e) {
  var step = ACCESS_TAB_KEYS[e.key], ids = ACCESS_SECTIONS.map(function (s) { return s[0]; });
  if (step === undefined || !e.target.closest || !e.target.closest('[role=tab]')) return;
  e.preventDefault();
  var at = ids.indexOf(e.target.dataset.sec);
  var to = step === 'first' ? 0 : step === 'last' ? ids.length - 1 : (at + step + ids.length) % ids.length;
  accSelect(ids[to], true);
}
function accFailed(text) {
  var p = accText('p', 'adm-msg bad acc-failed', text + ' '), again = accText('button', 'btn sec small acc-again', 'Try again');
  again.type = 'button';
  again.addEventListener('click', function () {
    var part = p.closest('.acc-part'), sec = part && ACCESS_SECTIONS.filter(function (x) { return 'acc-' + x[0] === part.id; })[0];
    if (part && sec) { part.textContent = ''; part.appendChild(accText('p', 'muted acc-loading', 'Loading ' + sec[2] + '\u2026')); }
    loadAccess();
  });
  p.appendChild(again);
  return p;
}
function accFold(text) { return String(text || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase(); }
function accAdmins() { return ACCESS.users.filter(function (u) { return u.role === 'admin' && !u.disabled; }).length; }
function accLastAdmin(u) { return u.role === 'admin' && !u.disabled && accAdmins() <= 1; }
function accPwAdmin(u) { return u.role === 'admin' && !u.disabled && u.source === 'local'; }
function accLastPwAdmin(u) { return accPwAdmin(u) && ACCESS.users.filter(accPwAdmin).length <= 1; }
function accInitial(u) { var s = Array.from(String(u.name || u.email || '?').trim()); return (s[0] || '?').toUpperCase(); }
function accRel(iso) {
  var t = Date.parse(iso || '');
  if (isNaN(t)) return '';
  var m = Math.floor((Date.now() - t) / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return m + ' min ago';
  if (m < 1440) return Math.floor(m / 60) + ' h ago';
  if (m < 30 * 1440) return Math.floor(m / 1440) + ' d ago';
  return daytime(iso);
}
function accExpiry(t) {
  var at = Date.parse(t && t.expiresAt || '');
  if (isNaN(at)) return null;
  var left = at - Date.now();
  return {at: t.expiresAt, past: left <= 0, soon: left > 0 && left <= ACCESS_SOON_DAYS * ACCESS_DAY_MS, days: Math.ceil(left / ACCESS_DAY_MS)};
}
function accTokenWarn(t) { var x = accExpiry(t); return !t.revoked && !!x && (x.past || x.soon); }
function accLabel(tr, heads) { [].forEach.call(tr.children, function (td, n) { if (heads[n]) td.dataset.l = heads[n]; }); }
function accNavState() {
  var a = ACCESS || {users: [], tokens: []}, mcp = mcpState();
  var soon = a.tokens.filter(accTokenWarn).length, expired = a.users.filter(function (u) { var l = accLink(u); return l && l.state === 'expired'; }).length;
  return {
    people: {n: ACCESS && !ACCESS_FAIL.users ? String(a.users.length) : '', warn: ACCESS_FAIL.users ? 'People did not load' : expired ? expired + ' SSO link' + (expired === 1 ? '' : 's') + ' expired' : ''},
    sso: {n: SSO ? ssoPill()[0] : '', warn: SSO ? ssoAttention() : ACCESS ? 'Single sign-on did not load' : ''},
    tokens: {n: ACCESS && !ACCESS_FAIL.tokens ? String(a.tokens.length) : '', warn: ACCESS_FAIL.tokens ? 'API tokens did not load' : soon ? soon + ' expiring within ' + ACCESS_SOON_DAYS + ' days or expired' : ''},
    mcp: {n: mcp[0], warn: MCP_FAILED ? 'MCP settings did not load' : mcp[2]}
  };
}
function accNavPaint() {
  var nav = document.querySelector('#access .acc-nav');
  if (!nav) return;
  var st = accNavState();
  [].forEach.call(nav.querySelectorAll('.acc-nav-b'), function (b) {
    var s = st[b.dataset.sec] || {n: '', warn: ''}, dot = b.querySelector('.acc-dot');
    b.querySelector('.acc-nav-n').textContent = s.n;
    dot.hidden = !s.warn;
    dot.textContent = s.warn ? 'needs attention' : '';
    b.title = s.warn || '';
    b.classList.toggle('warn', !!s.warn);
  });
}
function accFocusKey() {
  var a = document.activeElement, box = document.getElementById('access');
  if (!a || !box || a === document.body || !box.contains(a) || a.closest('#acc-sso, #acc-mcp')) return null;
  var row = a.closest('[data-email], [data-id]'), cls = String(a.className || '').split(/\s+/).filter(function (c) { return /^acc-/.test(c); })[0] || '';
  return {email: row ? row.dataset.email : null, id: row ? row.dataset.id : null, cls: cls, tag: a.tagName.toLowerCase(), name: a.name || '', sec: (a.closest('.acc-part') || {}).id || ''};
}
function accFocusBack(k) {
  var now = document.activeElement, box = document.getElementById('access');
  if (!k || !box || (now && now !== document.body && box.contains(now))) return;
  var scope = document.getElementById(k.sec) || box;
  if (k.email != null || k.id != null) scope = [].filter.call(box.querySelectorAll(k.email != null ? '.acc-user' : '.acc-token'), function (r) { return k.email != null ? r.dataset.email === k.email : r.dataset.id === k.id; })[0] || scope;
  var to = [].filter.call(scope.querySelectorAll(k.tag), function (n) { return (k.cls ? n.classList.contains(k.cls) : true) && (!k.name || n.name === k.name) && !n.disabled && !n.closest('[hidden]'); })[0];
  if (!to && scope !== box) to = scope.querySelector('.acc-menu-b') || scope.querySelector('h3');
  if (to) { if (to.tagName === 'H3') to.tabIndex = -1; to.focus(); }
}
function accFilterPeople() {
  var part = document.getElementById('acc-people');
  if (!part) return;
  var want = accFold(ACCESS_FIND.trim()), rows = part.querySelectorAll('.acc-user'), n = 0;
  [].forEach.call(rows, function (r) {
    var on = (!want || r.dataset.find.indexOf(want) >= 0) && (!ACCESS_ROLE_F || r.dataset.role === ACCESS_ROLE_F);
    r.hidden = !on;
    if (on) n++;
  });
  var none = part.querySelector('.acc-none'), shown = part.querySelector('.acc-shown');
  if (none) none.hidden = n > 0 || !rows.length;
  if (shown) shown.textContent = rows.length && n !== rows.length ? n + ' of ' + rows.length + ' shown' : '';
}
function accPeopleTools() {
  var tools = accText('div', 'acc-tools'), find = document.createElement('input'), rf = document.createElement('select'), shown = accText('span', 'muted acc-shown');
  find.type = 'search';
  find.className = 'acc-find';
  find.maxLength = 200;
  find.placeholder = 'Search name or email';
  find.setAttribute('aria-label', 'Search people by name or email');
  find.value = ACCESS_FIND;
  find.addEventListener('input', function () { ACCESS_FIND = find.value; accFilterPeople(); });
  rf.className = 'acc-role-f';
  rf.setAttribute('aria-label', 'Show role');
  [['', 'All roles']].concat(ACCESS_ROLES.map(function (r) { return [r, ACCESS_ROLE_LABELS[r] + 's']; })).forEach(function (o) {
    var opt = document.createElement('option');
    opt.value = o[0];
    opt.textContent = o[1];
    rf.appendChild(opt);
  });
  rf.value = ACCESS_ROLE_F;
  rf.addEventListener('change', function () { ACCESS_ROLE_F = rf.value; accFilterPeople(); });
  shown.setAttribute('role', 'status');
  tools.appendChild(find);
  tools.appendChild(rf);
  tools.appendChild(shown);
  return tools;
}
function renderPeople() {
  var part = document.getElementById('acc-people');
  if (!part) return;
  var kept = part.querySelector('.acc-add-user');
  part.textContent = '';
  var users = accSection('People', ACCESS_FAIL.users ? null : ACCESS.users.length);
  var head = accText('div', 'acc-users-h'), open = accText('button', 'btn small acc-add-open', 'Add person'), said = accText('span', 'adm-msg acc-users-msg');
  open.type = 'button';
  open.setAttribute('aria-expanded', kept ? 'true' : 'false');
  open.setAttribute('aria-controls', ACCESS_ADD_ID);
  said.setAttribute('role', 'status');
  head.appendChild(users.querySelector('h3'));
  head.appendChild(open);
  head.appendChild(said);
  users.insertBefore(head, users.firstChild);
  users.appendChild(accText('p', 'adm-help acc-sec-help', 'Everyone who can sign in, with their role and how they sign in. Disabling someone or lowering a role reaches their sessions and API tokens within 30 seconds.'));
  open.addEventListener('click', function () { accAddOpen(users, open); });
  part.appendChild(users);
  if (ACCESS_FAIL.users) { users.appendChild(accFailed('People did not load.')); return; }
  if (!ACCESS.users.length) {
    users.appendChild(accText('p', 'muted acc-empty', 'No people yet: add the first person who signs in.'));
  } else {
    users.appendChild(accPeopleTools());
    var ut = accTable(ACCESS_USER_HEADS);
    ACCESS.users.forEach(function (u) { var tr = accUserRow(u); accLabel(tr, ACCESS_USER_HEADS); ut.body.appendChild(tr); });
    users.appendChild(ut.wrap);
    var none = accText('p', 'muted acc-none', 'No one matches the search or the role.');
    none.hidden = true;
    users.appendChild(none);
  }
  if (kept) { users.appendChild(kept); accAddSync(kept); }
  accFilterPeople();
}
function renderTokens() {
  var part = document.getElementById('acc-tokens');
  if (!part) return;
  part.textContent = '';
  var tokens = accSection('API tokens', ACCESS_FAIL.tokens ? null : ACCESS.tokens.length);
  tokens.appendChild(accText('p', 'adm-help acc-sec-help', 'Tokens let scripts and CI jobs call the API. A token never has more access than the person who made it.'));
  part.appendChild(tokens);
  if (ACCESS_FAIL.tokens) { tokens.appendChild(accFailed('API tokens did not load.')); return; }
  tokens.appendChild(accText('div', 'acc-secret-slot'));
  if (!ACCESS.tokens.length) {
    tokens.appendChild(accText('p', 'muted acc-empty', 'No API tokens yet: create one below for a script or a CI job.'));
  } else {
    var tt = accTable(ACCESS_TOKEN_HEADS);
    ACCESS.tokens.forEach(function (t) { var tr = accTokenRow(t); accLabel(tr, ACCESS_TOKEN_HEADS); tt.body.appendChild(tr); });
    tokens.appendChild(tt.wrap);
  }
  tokens.appendChild(accAddToken());
}
function accMenuClose(except, back) {
  [].forEach.call(document.querySelectorAll('#access .acc-menu:not([hidden])'), function (m) {
    if (m === except) return;
    m.hidden = true;
    var b = m.parentNode.querySelector('.acc-menu-b');
    if (b) { b.setAttribute('aria-expanded', 'false'); if (back) b.focus(); }
  });
}
function accMenuFollow() {
  [].forEach.call(document.querySelectorAll('#access .acc-menu:not([hidden])'), function (m) {
    var b = m.parentNode.querySelector('.acc-menu-b'), r = b && b.getBoundingClientRect();
    if (r && r.bottom > 0 && r.top < window.innerHeight) { accMenuPlace(b, m); return; }
    m.hidden = true;
    if (b) b.setAttribute('aria-expanded', 'false');
  });
}
function accMenuPlace(b, m) {
  var r = b.getBoundingClientRect(), h = m.offsetHeight || 0, w = m.offsetWidth || 0, cw = document.documentElement.clientWidth, room = window.innerHeight - r.bottom;
  m.style.position = 'fixed';
  m.style.left = Math.max(8, Math.min(r.right - w, cw - w - 8)) + 'px';
  m.style.top = (room < h + 8 && r.top > h + 8 ? r.top - h - 4 : r.bottom + 4) + 'px';
}
function accMenu(u, items) {
  var w = accText('span', 'acc-menu-w'), b = accText('button', 'btn sec small acc-menu-b', '\u22ef'), m = accText('div', 'acc-menu');
  b.type = 'button';
  b.setAttribute('aria-haspopup', 'menu');
  b.setAttribute('aria-expanded', 'false');
  b.setAttribute('aria-label', 'Actions for ' + u.email);
  m.setAttribute('role', 'menu');
  m.setAttribute('aria-label', 'Actions for ' + u.email);
  m.hidden = true;
  items.forEach(function (it) {
    var mi = accText('button', 'acc-mi ' + it.cls, it.text);
    mi.type = 'button';
    mi.setAttribute('role', 'menuitem');
    mi.tabIndex = -1;
    if (it.title) mi.title = it.title;
    if (it.off) { mi.disabled = true; mi.setAttribute('aria-disabled', 'true'); }
    mi.addEventListener('click', function () { m.hidden = true; b.setAttribute('aria-expanded', 'false'); b.focus(); it.run(); });
    m.appendChild(mi);
  });
  var live = function () { return [].filter.call(m.querySelectorAll('.acc-mi'), function (x) { return !x.disabled; }); };
  b.addEventListener('click', function () {
    var opening = m.hidden;
    accMenuClose(m, false);
    m.hidden = !opening;
    b.setAttribute('aria-expanded', String(opening));
    if (opening) accMenuPlace(b, m);
    if (opening && live()[0]) live()[0].focus();
  });
  m.addEventListener('keydown', function (e) {
    var list = live(), at = list.indexOf(document.activeElement), to = null;
    if (e.key === 'Escape') { e.preventDefault(); m.hidden = true; b.setAttribute('aria-expanded', 'false'); b.focus(); return; }
    if (e.key === 'Tab') { m.hidden = true; b.setAttribute('aria-expanded', 'false'); return; }
    if (e.key === 'ArrowDown') to = (at + 1) % list.length;
    else if (e.key === 'ArrowUp') to = (at - 1 + list.length) % list.length;
    else if (e.key === 'Home') to = 0;
    else if (e.key === 'End') to = list.length - 1;
    if (to === null || !list.length) return;
    e.preventDefault();
    list[to].focus();
  });
  w.appendChild(b);
  w.appendChild(m);
  return w;
}
function accMethods(u) {
  var box = accText('span', 'acc-src'), l = accLink(u);
  if (u.source === 'sso') {
    var only = accText('span', 'tag plain acc-badge acc-sso-only', 'SSO only');
    only.title = 'Signs in with SSO and has no password';
    box.appendChild(only);
    accMcpBadge(box, u);
    return box;
  }
  box.appendChild(accText('span', 'tag plain acc-badge acc-pw-badge', 'Password'));
  if (l && l.state === 'bound') {
    var both = accText('span', 'tag plain acc-badge acc-sso-badge', 'SSO');
    both.title = 'Also signs in with SSO';
    box.appendChild(both);
  }
  accMcpBadge(box, u);
  return box;
}
function accMcpBadge(box, u) {
  if (!u.mcp || !u.mcp.at) return;
  var kind = u.mcp.only === null ? 'uses' : u.mcp.only ? 'only' : 'both';
  var b = accText('span', 'tag plain acc-badge acc-mcp-badge', {uses: 'Uses MCP', only: 'MCP only', both: 'Page and MCP'}[kind]);
  b.title = {uses: 'Uses MCP; no use of the page seen yet', only: 'Never signed in on the page', both: 'Signs in on the page and over MCP'}[kind];
  b.dataset.mcp = kind;
  box.appendChild(b);
}
async function accPatch(u, body, msg, revert, said, onFail) {
  accMsg(msg, 'Saving\u2026', false);
  try {
    var saved = await adminApi('PATCH', '/api/admin/users/' + encodeURIComponent(u.email), body);
    accApply(function (a) { a.users = a.users.map(function (x) { return x.email === u.email ? Object.assign({}, x, saved || (body.password === undefined ? body : {})) : x; }); });
    var fresh = await accReload();
    renderAccess();
    accStale(fresh, 'people');
    accNote(accRowMsg('user', u.email), said);
    return saved || {};
  } catch (e) {
    if (revert) revert();
    if (onFail) {
      accNote(accLive(msg, accRowMsg('user', u.email)), '');
      onFail(accError(e, 'Not saved, try again'));
      return null;
    }
    accNote(accLive(msg, accRowMsg('user', u.email)), accError(e, 'Not saved, try again'), true);
    return null;
  }
}
function accRenamed(email, name) {
  if (!ACCESS) return;
  accApply(function (a) { a.users = a.users.map(function (x) { return x.email === email ? Object.assign({}, x, {name: name}) : x; }); });
  renderAccess();
}
var ACCESS_PW_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789-_.!@#%+=';
function accGenPassword() {
  var n = ACCESS_PW_ALPHABET.length, len = Math.min(Math.max(20, ACCESS_PW_MIN), ACCESS_PW_MAX), limit = Math.floor(4294967296 / n) * n, out = '', buf = new Uint32Array(1);
  while (out.length < len) {
    crypto.getRandomValues(buf);
    if (buf[0] < limit) out += ACCESS_PW_ALPHABET.charAt(buf[0] % n);
  }
  return out;
}
function accEyeSet(eye, input, on) {
  input.type = on ? 'text' : 'password';
  eye.setAttribute('aria-pressed', String(on));
  eye.setAttribute('aria-label', (on ? 'Hide' : 'Show') + ' password');
  eye.textContent = on ? 'Hide' : 'Show';
}
function accGenWire(input, gen, copy, eye) {
  var timer = null;
  gen.addEventListener('click', function () {
    input.value = accGenPassword();
    input.dispatchEvent(new Event('input', {bubbles: true}));
    if (eye) accEyeSet(eye, input, true); else input.type = 'text';
    clearTimeout(timer);
    copy.textContent = 'Copy';
    copy.hidden = false;
    input.focus();
  });
  input.addEventListener('input', function () { if (!input.value) copy.hidden = true; });
  copy.addEventListener('click', async function () {
    var done = false;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      try { await navigator.clipboard.writeText(input.value); done = true; } catch (e) { done = false; }
    }
    if (!done) { input.select(); return; }
    copy.textContent = 'Copied';
    clearTimeout(timer);
    timer = setTimeout(function () { copy.textContent = 'Copy'; }, 2500);
  });
}
function accMenuBtn(email) {
  var m = accRowMsg('user', email), row = m && m.closest('tr');
  return row ? row.querySelector('.acc-menu-b') : null;
}
function accDialogClose() {
  var d = document.getElementById('acc-dlg-wrap');
  if (d) d.remove();
}
function accDialog(cfg) {
  accDialogClose();
  var wrap = accText('div', 'modal-wrap acc-dlg-wrap'), panel = accText('form', 'modal acc-dlg');
  wrap.id = 'acc-dlg-wrap';
  panel.id = 'acc-dlg';
  panel.noValidate = true;
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-modal', 'true');
  panel.setAttribute('aria-labelledby', 'acc-dlg-title');
  var h = accText('h2', null, '');
  h.id = 'acc-dlg-title';
  h.appendChild(accText('span', null, cfg.lead));
  h.appendChild(document.createTextNode(' '));
  h.appendChild(accText('span', 'acc-dlg-who', cfg.who));
  panel.appendChild(h);
  if (cfg.sub) panel.appendChild(accText('p', 'acc-dlg-sub', cfg.sub));
  var input = document.createElement('input');
  input.id = 'acc-dlg-in';
  input.name = cfg.field;
  input.type = cfg.secret ? 'password' : 'text';
  input.maxLength = cfg.max;
  input.autocomplete = cfg.secret ? 'new-password' : 'off';
  input.spellcheck = false;
  input.value = cfg.value || '';
  var lab = accText('label', 'acc-dlg-lab', cfg.label);
  lab.htmlFor = input.id;
  panel.appendChild(lab);
  var row = accText('div', 'acc-dlg-row'), eye = null;
  row.appendChild(input);
  if (cfg.secret) {
    eye = accText('button', 'btn sec small acc-dlg-eye', 'Show');
    eye.type = 'button';
    eye.setAttribute('aria-pressed', 'false');
    eye.setAttribute('aria-controls', input.id);
    eye.setAttribute('aria-label', 'Show password');
    row.appendChild(eye);
  }
  panel.appendChild(row);
  var gen = null, copy = null;
  if (cfg.secret) {
    var tools = accText('div', 'acc-dlg-gen');
    gen = accText('button', 'btn sec small acc-dlg-generate', 'Generate');
    copy = accText('button', 'btn sec small acc-dlg-copy', 'Copy');
    gen.type = 'button';
    copy.type = 'button';
    copy.hidden = true;
    tools.appendChild(gen);
    tools.appendChild(copy);
    panel.appendChild(tools);
    accGenWire(input, gen, copy, eye);
  }
  var hint = accText('p', 'acc-dlg-hint', cfg.hint);
  hint.id = 'acc-dlg-hint';
  panel.appendChild(hint);
  var err = accText('p', 'ask-err acc-dlg-err', '');
  err.id = 'acc-dlg-err';
  err.setAttribute('role', 'alert');
  err.hidden = true;
  input.setAttribute('aria-describedby', 'acc-dlg-hint acc-dlg-err');
  panel.appendChild(err);
  if (cfg.note) panel.appendChild(accText('p', 'acc-dlg-note', cfg.note));
  var acts = accText('div', 'modal-acts'), cancel = accText('button', 'btn sec acc-dlg-cancel', 'Cancel'), go = accText('button', 'btn acc-dlg-go', cfg.go);
  cancel.type = 'button';
  go.type = 'button';
  acts.appendChild(cancel);
  acts.appendChild(go);
  panel.appendChild(acts);
  wrap.appendChild(panel);
  var back = function () { var b = accMenuBtn(cfg.email); if (b) b.focus(); };
  var close = function () { accDialogClose(); back(); };
  var say = function (text) { err.textContent = text || ''; err.hidden = !text; input.setAttribute('aria-invalid', text ? 'true' : 'false'); };
  var busy = false;
  var submit = async function () {
    if (busy) return;
    var value = cfg.trim ? input.value.trim() : input.value, bad = cfg.check ? cfg.check(value) : '';
    if (bad) { say(bad); input.focus(); return; }
    busy = true;
    go.disabled = true;
    input.readOnly = true;
    say('');
    var ok = await cfg.run(value, say);
    busy = false;
    if (ok) { accDialogClose(); back(); return; }
    go.disabled = false;
    input.readOnly = false;
    if (panel.isConnected) input.focus();
  };
  go.addEventListener('click', submit);
  cancel.addEventListener('click', close);
  panel.addEventListener('submit', function (e) { e.preventDefault(); });
  wrap.addEventListener('click', function (e) { if (e.target === wrap) close(); });
  input.addEventListener('input', function () { if (!err.hidden) say(''); });
  if (eye) eye.addEventListener('click', function () { accEyeSet(eye, input, eye.getAttribute('aria-pressed') !== 'true'); });
  panel.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); return; }
    if (e.key === 'Enter' && e.target === input) { e.preventDefault(); submit(); return; }
    if (e.key !== 'Tab') return;
    var all = [].filter.call(panel.querySelectorAll('input, button'), function (x) { return !x.disabled; }), first = all[0], last = all[all.length - 1];
    if (!first) return;
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  document.body.appendChild(wrap);
  input.focus();
  if (!cfg.secret) input.select();
}
function accNameDialog(u, msg) {
  accDialog({lead: 'Edit name for', who: u.email, email: u.email, field: 'name', label: 'Name', max: 120, value: u.name || '', trim: true, hint: '1-120 characters', go: 'Save',
    run: async function (name, say) {
      var saved = await accPatch(u, {name: name}, msg, null, 'Name saved', say);
      if (saved && typeof window.__skopaxRenamed === 'function') window.__skopaxRenamed(u.email, saved.name || name);
      return !!saved;
    }});
}
function accPasswordDialog(u, msg) {
  var sso = u.source === 'sso';
  accDialog({lead: sso ? 'Set a password for' : 'Reset password for', who: u.name || u.email, sub: u.email, email: u.email, field: 'password', secret: true, label: 'New password', max: ACCESS_PW_MAX,
    hint: ACCESS_PW_MIN + '-' + ACCESS_PW_MAX + ' characters', go: 'Set password',
    note: sso ? ACCESS_SET_PW_NOTE : 'Their other password sessions end. API tokens and SSO sessions are not touched.',
    check: function (v) { return v ? '' : ACCESS_NEED_PW; },
    run: async function (pw, say) { return !!(await accPatch(u, {password: pw}, msg, null, 'Password set', say)); }});
}
function accUserRow(u) {
  var tr = accText('tr', 'acc-user' + (u.disabled ? ' off' : ''));
  tr.dataset.email = u.email;
  tr.dataset.find = accFold((u.name || '') + ' ' + u.email);
  tr.dataset.role = u.role;
  var who = accText('div', 'acc-who'), av = accText('span', 'acc-av', accInitial(u)), names = accText('div', 'acc-names');
  av.setAttribute('aria-hidden', 'true');
  names.appendChild(accText('span', 'acc-name', u.name || u.email));
  names.appendChild(accText('span', 'acc-mail', u.email));
  who.appendChild(av);
  who.appendChild(names);
  accCell(tr, who);
  var sel = accRoleSelect(u.role, 'acc-role acc-chip acc-chip-' + (ACCESS_ROLES.indexOf(u.role) >= 0 ? u.role : 'viewer')), idp = ACCESS_IDP.indexOf(u.roleFrom) >= 0, last = accLastAdmin(u);
  var lastPw = accLastPwAdmin(u), kept = last ? ACCESS_LAST_ADMIN : lastPw ? ACCESS_LAST_PW_ROLE : '';
  sel.setAttribute('aria-label', 'Role of ' + u.email);
  var roleCell = accCell(tr, sel);
  if (idp) {
    sel.disabled = true;
    sel.title = 'Set by the sign-in provider or a role plug-in at each sign-in';
    roleCell.appendChild(accText('span', 'tag plain acc-idp', 'from sign-in provider'));
  } else if (kept) {
    sel.disabled = true;
    sel.title = kept;
  }
  if (last) {
    var lone = accText('span', 'tag plain acc-last-admin', 'Last admin');
    lone.title = ACCESS_LAST_ADMIN;
    roleCell.appendChild(lone);
  }
  accCell(tr, accMethods(u));
  var fresh = u.source === 'sso' && u.opened === false, seen = accRel(u.lastLogin);
  var lastIn = accText('span', 'acc-last' + (fresh ? ' acc-not-yet' : seen ? '' : ' muted'), seen || 'Not signed in yet');
  if (seen) lastIn.title = dayhm(u.lastLogin);
  var lastCell = accCell(tr, lastIn);
  if (u.mcp && u.mcp.at && accRel(u.mcp.at)) {
    var viaMcp = accText('span', 'acc-mcp-use muted', '');
    viaMcp.appendChild(accText('span', null, 'MCP'));
    var useAt = accText('span', 'acc-mcp-use-at', accRel(u.mcp.at));
    useAt.title = dayhm(u.mcp.at);
    viaMcp.appendChild(document.createTextNode(' '));
    viaMcp.appendChild(useAt);
    lastCell.appendChild(viaMcp);
  }
  var off = document.createElement('input');
  off.type = 'checkbox';
  off.className = 'acc-off';
  off.checked = !!u.disabled;
  off.setAttribute('aria-label', 'Disabled: ' + u.email);
  if (kept) { off.disabled = true; off.title = kept; }
  accCell(tr, off);
  var linkCell = accCell(tr, null);
  var ctl = accCell(tr, null);
  ctl.className = 'acc-ctl';
  var msg = accText('span', 'adm-msg');
  msg.setAttribute('role', 'status');
  if (u.source !== 'sso') accLinkCell(linkCell, u, msg);
  else linkCell.appendChild(accText('span', 'muted acc-link-none', '\u2013'));
  sel.addEventListener('change', function () { accPatch(u, {role: sel.value}, msg, function () { sel.value = u.role; off.checked = !!u.disabled; }, 'Role set to ' + sel.value); });
  off.addEventListener('change', function () { accPatch(u, {disabled: off.checked}, msg, function () { sel.value = u.role; off.checked = !!u.disabled; }, off.checked ? 'Disabled' : 'Enabled'); });
  var ask = accSsoOnlyForm(u, msg);
  var showPw = function () { ask.hidden = true; accPasswordDialog(u, msg); };
  var items = [{cls: 'acc-mi-name', text: 'Edit name', run: function () { ask.hidden = true; accNameDialog(u, msg); }}];
  if (u.source !== 'sso') items.push({cls: 'acc-reset', text: 'Reset password', run: showPw});
  if (u.source === 'sso') items.push({cls: 'acc-setpw', text: 'Set a password', title: ACCESS_SET_PW_NOTE, run: showPw});
  if (u.source === 'local' && accSsoOn()) items.push({cls: 'acc-mi-ssoonly', text: 'Make SSO only', off: lastPw, title: lastPw ? ACCESS_LAST_PW_ADMIN : 'Removes the password; this person signs in with SSO only', run: function () { ask.open(); }});
  items.push({cls: 'acc-mi-off' + (u.disabled ? '' : ' acc-mi-danger'), text: u.disabled ? 'Enable sign-in' : 'Disable sign-in', off: !!kept, title: kept || (u.disabled ? '' : 'Stops sign-in for this person; their sessions and API tokens stop within 30 seconds'),
    run: function () { off.checked = !off.checked; off.dispatchEvent(new Event('change', {bubbles: true})); }});
  var menu = accMenu(u, items);
  ctl.appendChild(menu);
  ask.menu = menu;
  ctl.appendChild(ask);
  ctl.appendChild(msg);
  return tr;
}
function accSsoOnlyForm(u, msg) {
  var box = accText('span', 'acc-ssoonly-form'), go = accText('button', 'btn small acc-ssoonly-go', 'Make SSO only'), no = accText('button', 'btn sec small acc-ssoonly-cancel', 'Cancel');
  box.hidden = true;
  go.type = 'button';
  no.type = 'button';
  box.appendChild(accText('span', 'acc-ssoonly-q', 'Remove the password? This person then signs in with SSO only.'));
  if (u.email === ME_USER) box.appendChild(accText('span', 'acc-ssoonly-self', 'You will be signed out if you signed in with your password'));
  box.appendChild(go);
  box.appendChild(no);
  box.open = function () { box.hidden = false; go.focus(); };
  no.addEventListener('click', function () { box.hidden = true; accMsg(msg, ''); var b = box.menu && box.menu.querySelector('.acc-menu-b'); if (b) b.focus(); });
  go.addEventListener('click', function () { go.disabled = true; accPatch(u, {ssoOnly: true}, msg, function () { go.disabled = false; }, 'Signs in with SSO only now'); });
  return box;
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
function accSsoOn() { return !!(SSO && SSO.active); }
function accAddField(key, label, req, control, help) {
  var id = 'acc-new-' + key, bool = key === 'sso';
  var lab = bool || key === 'role' ? '<span class="int-lab" id="' + id + '-l">' + esc(label) + '</span>' : '<label for="' + id + '" id="' + id + '-l">' + esc(label) + (req ? '<span class="int-req"> *</span>' : '') + '</label>';
  return '<div class="int-field" data-key="' + key + '"' + (bool ? ' data-kind="bool"' : '') + '><div class="int-lrow">' + lab + '</div>' + control +
    '<p class="adm-help" id="' + id + '-h">' + esc(help) + '</p><p class="acc-ferr" id="' + id + '-err" role="alert" hidden></p></div>';
}
function accAddHtml(sso) {
  var described = function (key) { return ' aria-describedby="acc-new-' + key + '-h acc-new-' + key + '-err"'; };
  var seg = '<input type="hidden" name="role" value="viewer"><div class="int-seg" role="radiogroup" aria-labelledby="acc-new-role-l"' + described('role') + '>' +
    ACCESS_ROLES.map(function (r) { return '<button type="button" class="int-seg-b" role="radio" data-value="' + r + '" aria-checked="false" tabindex="-1">' + ACCESS_ROLE_LABELS[r] + '</button>'; }).join('') + '</div>';
  return '<div class="acc-add-head"><h4 id="acc-add-h">Add person</h4></div>' +
    accAddField('email', 'Email', true, '<input id="acc-new-email" class="int-in" name="email" type="email" maxlength="254" autocomplete="off" spellcheck="false" autocapitalize="off"' + described('email') + '>', '') +
    accAddField('name', 'Name', false, '<input id="acc-new-name" class="int-in" name="name" type="text" maxlength="120" autocomplete="off"' + described('name') + '>', 'Empty uses the part of the email before the @') +
    accAddField('role', 'Role', true, seg, 'Viewers read, operators work cards, admins also manage users and settings') +
    (sso ? accAddField('sso', 'Signs in with SSO', false, '<button type="button" id="acc-new-sso" class="int-switch int-tog" role="switch" aria-checked="true" aria-labelledby="acc-new-sso-l"' + described('sso') + '></button>',
      'The first SSO sign-in with this email, verified by your provider, opens this account') : '') +
    accAddField('password', 'Password', true, '<div class="int-sec-row"><input id="acc-new-password" class="int-in" name="password" type="password" maxlength="' + ACCESS_PW_MAX + '" autocomplete="new-password"' + described('password') + '>' +
      '<button type="button" class="int-eye" aria-pressed="false" aria-controls="acc-new-password" aria-label="Show password">Show</button></div>' +
      '<div class="acc-dlg-gen"><button type="button" class="btn sec small acc-gen">Generate</button><button type="button" class="btn sec small acc-copy" hidden>Copy</button></div>', '') +
    '<div class="int-foot"><span class="adm-msg int-form-msg" role="status"></span><div class="int-foot-row"><div class="int-acts int-foot-acts">' +
    '<button type="button" class="btn sec small acc-add-cancel">Cancel</button><button type="submit" class="btn small acc-add-go">Add person</button></div></div></div>';
}
function accAddPart(f, key) { return f.querySelector('.int-field[data-key="' + key + '"]'); }
function accAddInput(f, key) {
  var w = accAddPart(f, key);
  return w && (w.querySelector('input.int-in') || w.querySelector('[role=switch]') || w.querySelector('.int-seg-b[aria-checked="true"]') || w.querySelector('.int-seg-b'));
}
function accAddSso(f) {
  var sw = f.querySelector('#acc-new-sso');
  return !!sw && sw.getAttribute('aria-checked') === 'true';
}
function accAddMark(f, key, text) {
  var w = accAddPart(f, key);
  if (!w) return false;
  var err = w.querySelector('.acc-ferr'), input = w.querySelector('input.int-in');
  err.textContent = text || '';
  err.hidden = !text;
  if (input) { if (text) input.setAttribute('aria-invalid', 'true'); else input.removeAttribute('aria-invalid'); }
  return true;
}
function accAddSay(f, text, bad) {
  var m = f.querySelector('.int-form-msg');
  m.textContent = text || '';
  m.classList.toggle('bad', !!bad);
  m.classList.toggle('ok', !bad && !!text);
}
function accAddSync(f) {
  var sso = accSsoOn(), part = accAddPart(f, 'sso');
  if (sso && !part) {
    var box = document.createElement('div');
    box.innerHTML = accAddHtml(true);
    accAddPart(f, 'password').before(accAddPart(box, 'sso'));
    accAddWireSso(f);
  } else if (!sso && part) {
    part.remove();
  }
  accAddPaint(f);
}
function accAddPaint(f) {
  var need = !accAddSso(f), w = accAddPart(f, 'password'), label = w.querySelector('label'), mark = label.querySelector('.int-req');
  if (need && !mark) label.appendChild(accText('span', 'int-req', ' *'));
  if (!need && mark) mark.remove();
  var input = w.querySelector('input.int-in');
  if (need) input.setAttribute('aria-required', 'true'); else input.removeAttribute('aria-required');
  w.querySelector('.adm-help').textContent = need ? ACCESS_PW_MIN + '-' + ACCESS_PW_MAX + ' characters' : 'Optional: leave empty for an SSO-only account';
  var role = f.querySelector('[name=role]');
  [].forEach.call(f.querySelectorAll('.int-seg-b'), function (b) {
    var on = b.dataset.value === role.value;
    b.setAttribute('aria-checked', String(on));
    b.tabIndex = on ? 0 : -1;
  });
}
function accAddProblems(f) {
  var email = f.querySelector('[name=email]').value.trim(), name = f.querySelector('[name=name]').value.trim(), pw = f.querySelector('[name=password]').value, out = [];
  if (!email) out.push(['email', 'Enter an email']);
  else if (!ACCESS_EMAIL.test(email)) out.push(['email', 'Enter a valid email address']);
  if (name.length > 120) out.push(['name', 'A name is at most 120 characters']);
  if ((pw || !accAddSso(f)) && (pw.length < ACCESS_PW_MIN || pw.length > ACCESS_PW_MAX)) out.push(['password', ACCESS_NEED_PW]);
  return out;
}
function accAddBody(f) {
  var body = {email: f.querySelector('[name=email]').value.trim(), name: f.querySelector('[name=name]').value.trim(), role: f.querySelector('[name=role]').value}, pw = f.querySelector('[name=password]').value;
  var sw = f.querySelector('#acc-new-sso');
  if (pw || !accAddSso(f)) body.password = pw;
  if (sw) body.sso = accAddSso(f);
  return body;
}
function accAddFocus(f, key) {
  var to = accAddInput(f, key);
  if (to) to.focus();
}
function accAddClose(f, back) {
  var open = document.querySelector('#access .acc-add-open');
  f.remove();
  if (open) open.setAttribute('aria-expanded', 'false');
  if (back && open) open.focus();
}
function accAddOpen(section, open) {
  var held = section.querySelector('.acc-add-user');
  if (held) { accAddFocus(held, 'email'); return; }
  var f = accAddUser();
  section.appendChild(f);
  open.setAttribute('aria-expanded', 'true');
  accAddFocus(f, 'email');
}
function accAddWire(f) {
  var role = f.querySelector('[name=role]'), bs = [].slice.call(f.querySelectorAll('.int-seg-b'));
  var pick = function (b) { role.value = b.dataset.value; accAddPaint(f); b.focus(); };
  bs.forEach(function (b) { b.addEventListener('click', function () { pick(b); }); });
  f.querySelector('.int-seg').addEventListener('keydown', function (e) {
    var at = bs.indexOf(e.target), n = bs.length, to = null;
    if (at < 0) return;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') to = (at + 1) % n;
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') to = (at - 1 + n) % n;
    else if (e.key === 'Home') to = 0;
    else if (e.key === 'End') to = n - 1;
    else if (e.key === ' ' || e.key === 'Enter') to = at;
    if (to === null) return;
    e.preventDefault();
    pick(bs[to]);
  });
  accAddWireSso(f);
  var eye = f.querySelector('.int-eye'), pw = f.querySelector('[name=password]');
  eye.addEventListener('click', function () { accEyeSet(eye, pw, eye.getAttribute('aria-pressed') !== 'true'); });
  accGenWire(pw, f.querySelector('.acc-gen'), f.querySelector('.acc-copy'), eye);

  f.addEventListener('input', function (e) {
    var w = e.target.closest('.int-field');
    if (w) accAddMark(f, w.dataset.key, '');
  });
  f.querySelector('.acc-add-cancel').addEventListener('click', function () { accAddClose(f, true); });
}
function accAddWireSso(f) {
  var sw = f.querySelector('#acc-new-sso');
  if (!sw) return;
  sw.addEventListener('click', function () {
    sw.setAttribute('aria-checked', String(!accAddSso(f)));
    accAddMark(f, 'sso', '');
    accAddMark(f, 'password', '');
    accAddPaint(f);
  });
  sw.addEventListener('keydown', function (e) {
    if (e.key !== ' ' && e.key !== 'Enter') return;
    e.preventDefault();
    sw.click();
  });
  sw.addEventListener('keyup', function (e) { if (e.key === ' ') e.preventDefault(); });
}
function accAddUser() {
  var f = accText('form', 'int-form acc-add-user');
  f.id = ACCESS_ADD_ID;
  f.noValidate = true;
  f.setAttribute('aria-labelledby', 'acc-add-h');
  f.innerHTML = accAddHtml(accSsoOn());
  accAddPaint(f);
  accAddWire(f);
  var go = f.querySelector('.acc-add-go');
  f.addEventListener('submit', async function (e) {
    e.preventDefault();
    ACCESS_ADD_FIELDS.forEach(function (k) { accAddMark(f, k, ''); });
    accAddSay(f, '');
    var bad = accAddProblems(f);
    if (bad.length) {
      bad.forEach(function (p) { accAddMark(f, p[0], p[1]); });
      accAddFocus(f, bad[0][0]);
      return;
    }
    go.disabled = true;
    f.dataset.busy = '1';
    accAddSay(f, 'Adding\u2026');
    try {
      var made = await adminApi('POST', '/api/admin/users', accAddBody(f));
      accApply(function (a) { if (made && made.email) a.users = a.users.concat([made]); });
      accAddClose(f, false);
      var fresh = await accReload();
      renderAccess();
      accStale(fresh);
      var said = document.querySelector('#access .acc-users-msg'), open = document.querySelector('#access .acc-add-open');
      if (said) { said.textContent = 'Person added'; said.classList.add('ok'); }
      if (open) open.focus();
    } catch (err) {
      delete f.dataset.busy;
      go.disabled = false;
      var named = err && err.field && ACCESS_ADD_FIELDS.indexOf(err.field) >= 0 && err.detail && accAddMark(f, err.field, err.detail);
      if (named) { accAddSay(f, ''); accAddFocus(f, err.field); }
      else accAddSay(f, err && err.detail && !err.list ? err.detail : 'Not added, try again', true);
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
  accCell(tr, accText('span', 'acc-chip acc-chip-' + (ACCESS_ROLES.indexOf(t.role) >= 0 ? t.role : 'viewer'), t.role));
  var code = accText('code', null, 'skopax_' + t.id + '_\u2026');
  accCell(tr, code);
  accCell(tr, isTokenActor(t.createdBy) ? tokenActorLabel(t.createdBy) : t.createdBy || '');
  accCell(tr, dayhm(t.createdAt));
  accCell(tr, t.lastUsed ? dayhm(t.lastUsed) : 'never');
  accCell(tr, accExpiryEl(t));
  var ctl = accCell(tr, null);
  ctl.className = 'acc-ctl';
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
      var ask = window.__skopaxConfirm;
      if (typeof ask !== 'function') return;
      if (!(await ask('Revoke the token ' + t.name + '?', 'Scripts and jobs that use it are refused at once. This cannot be undone: make a new token to replace it.', 'Revoke token'))) { if (b.isConnected) b.focus(); return; }
      b.disabled = true;
      accMsg(msg, 'Revoking\u2026', false);
      try {
        await adminApi('DELETE', '/api/admin/tokens/' + encodeURIComponent(t.id));
        accApply(function (a) { a.tokens = a.tokens.map(function (x) { return x.id === t.id ? Object.assign({}, x, {revoked: true}) : x; }); });
        var fresh = await accReload();
        renderAccess();
        accStale(fresh, 'tokens');
        accNote(accRowMsg('token', t.id), 'Revoked');
        accFocusBack({email: null, id: null, cls: '', tag: 'h3', name: '', sec: 'acc-tokens'});
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
function accExpiryEl(t) {
  var x = accExpiry(t);
  if (!x) return accText('span', 'muted acc-exp', 'Never');
  if (t.revoked) return accText('span', 'acc-exp', dayhm(x.at));
  if (!x.past && !x.soon) { var plain = accText('span', 'acc-exp', daytime(x.at)); plain.title = dayhm(x.at); return plain; }
  var chip = accText('span', 'tag ' + (x.past ? 'k-crit acc-exp-past' : 'k-warn acc-exp-soon') + ' acc-exp', x.past ? 'Expired ' + dayhm(x.at) : x.days <= 1 ? 'Expires within a day' : 'Expires in ' + x.days + ' days');
  chip.title = (x.past ? 'Refused since ' : 'Refused from ') + dayhm(x.at) + ': make a new token to replace it';
  return chip;
}
function accAddToken() {
  var f = accText('form', 'acc-form acc-add-token');
  var name = accInput('name', 'text', 80, 'Token name'), role = accRoleSelect('viewer', null, 'role'), exp = document.createElement('select');
  name.placeholder = 'deploy-bot';
  exp.name = 'expires';
  ACCESS_EXPIRY.forEach(function (o) {
    var opt = document.createElement('option');
    opt.value = o[0];
    opt.textContent = o[1];
    exp.appendChild(opt);
  });
  var go = accText('button', 'btn small', 'Create token');
  go.type = 'submit';
  var msg = accText('span', 'adm-msg');
  msg.setAttribute('role', 'status');
  f.appendChild(accField('Name', name));
  f.appendChild(accField('Role', role));
  f.appendChild(accField('Expires', exp));
  f.appendChild(go);
  f.appendChild(msg);
  f.addEventListener('submit', async function (e) {
    e.preventDefault();
    go.disabled = true;
    accMsg(msg, 'Creating\u2026', false);
    var made, body = {name: name.value.trim(), role: role.value};
    if (exp.value) body.expiresDays = Number(exp.value);
    try {
      made = await adminApi('POST', '/api/admin/tokens', body);
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
    accStale(fresh, 'tokens');
    accShowToken(made.name, made.token);
  });
  return f;
}
function accShowToken(name, token) {
  var slot = document.querySelector('#access .acc-secret-slot');
  if (!slot) return;
  var box = accText('div', 'acc-secret');
  box.setAttribute('role', 'group');
  box.setAttribute('aria-label', 'New token ' + name);
  box.appendChild(accText('b', null, 'New token ' + name));
  box.appendChild(accText('code', 'acc-secret-v', token));
  var row = accText('div', 'adm-acts');
  var copy = accText('button', 'btn small', 'Copy');
  copy.type = 'button';
  var state = accText('span', 'adm-msg');
  state.setAttribute('role', 'status');
  row.appendChild(copy);
  row.appendChild(state);
  box.appendChild(row);
  box.appendChild(accText('p', 'adm-help acc-secret-note', "Copy it now. It won't be shown again. You will not see this token after you leave this tab."));
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
  copy.focus();
}

var SSO = null, SSO_TESTING = false, SSO_MAP_MAX = 50, SSO_VALUE_MAX = 4000, SSO_SEALED = 'client-secret', SSO_MAP = 'role-map', SSO_VERIFIED = 'require-verified-email', SSO_KEEP = '+sso', MCP_FAILED = false;
var SSO_MAIN = ['issuer', 'client-id', 'client-secret', 'scope', 'claims', 'roles-claim', 'role-map', 'default-role', 'require-verified-email', 'new-accounts', 'allowed-domains', 'button-text', 'button-icon'];
var SSO_GROUPS = [['provider', 'Provider', ['issuer', 'client-id', 'client-secret', 'scope', 'claims', 'require-verified-email']], ['who', 'Who may sign in', ['new-accounts', 'allowed-domains']],
  ['roles', 'Roles from groups', ['roles-claim', 'role-map', 'default-role']], ['button', 'Sign-in button', ['button-text', 'button-icon']]];
var SSO_CHIP_IN = 'sso-chip-in', SSO_DOMAINS = 'allowed-domains', SSO_BUTTON_TEXT = 'button-text', SSO_LAST_TEST = null, SSO_MARK_ID = /^[a-z0-9-]{1,40}$/, SSO_ICON_ACCEPT = ['image/png', 'image/webp', 'image/svg+xml'];
var SSO_ICON_TYPES = {'image/png': 'PNG', 'image/webp': 'WebP', 'image/svg+xml': 'SVG'};
var SSO_MARK_NAMES = {google: 'Google', microsoft: 'Microsoft', okta: 'Okta', auth0: 'Auth0', onelogin: 'OneLogin', jumpcloud: 'JumpCloud', zitadel: 'ZITADEL', authentik: 'authentik', gitlab: 'GitLab',
  keycloak: 'Keycloak', key: 'OpenID Connect'};
var SSO_ROLE_KEYS = ['roles.stale-max-minutes'];
var SSO_ADVANCED = ['authorize-url', 'token-url', 'jwks-url', 'client-auth', 'extra-token-headers'];
var SSO_LABELS = {issuer: 'Issuer URL', 'client-id': 'Client ID', 'client-secret': 'Client secret', scope: 'Scope', claims: 'Claims come from', 'roles-claim': 'Roles claim',
  'role-map': 'Role map', 'default-role': 'Default role', 'require-verified-email': 'Require verified email', 'authorize-url': 'Authorize URL', 'token-url': 'Token URL',
  'jwks-url': 'Keys (JWKS) URL', 'client-auth': 'Client authentication', 'new-accounts': 'New accounts', 'allowed-domains': 'Allowed email domains', 'button-text': 'Button text',
  'button-icon': 'Button icon', 'extra-token-headers': 'Extra token request headers'};
var SSO_HELP = {issuer: 'Its /.well-known/openid-configuration document gives the endpoints.', 'client-secret': 'Saved sealed and never shown again.',
  scope: 'Separated by spaces; it includes openid unless the claims come from the access token.', 'roles-claim': 'A claim name such as roles or groups, or a dot path such as realm_access.roles.',
  'role-map': 'Claim values matched exactly; several matches give the highest role.',
  'default-role': 'For someone no plug-in, claim or Admin-set role gives a role.', 'require-verified-email': 'Refuses a sign-in whose token does not say the email is verified.',
  'authorize-url': 'Empty uses the one the provider lists.', 'token-url': 'Empty uses the one the provider lists.', 'jwks-url': 'Empty uses the one the provider lists.',
  'client-auth': 'auto follows what the provider lists, post sends the secret in the form and basic in an Authorization header.',
  'extra-token-headers': 'Sent with the token request only, as Name: value pairs separated by semicolons; at most 10. Shown here in plain text: no secrets.',
  'new-accounts': 'Added only lets in only people who already have an account, added here or by an earlier sign-in. With a public provider, such as a consumer Google sign-in, Anyone means any account there.',
  'allowed-domains': 'Email domains such as example.com, matched exactly; none lets every domain in. Checked before an account is made or used.',
  'button-text': 'The text of the login button, at most 40 characters; empty uses Sign in with SSO.', 'button-icon': 'Auto picks a mark from the issuer host; Company shows the image uploaded below; None shows the text only.'};
var SSO_CHOICES = {claims: [['id-token', 'ID token'], ['access-token', 'access token']], 'client-auth': [['auto', 'auto'], ['post', 'post'], ['basic', 'basic']],
  'default-role': [['viewer', 'viewer'], ['operator', 'operator']], 'new-accounts': [['anyone', 'Anyone the provider signs in'], ['added-only', 'Added only']],
  'button-icon': [['auto', 'Auto, from the issuer'], ['company', 'Company icon'], ['none', 'None']]};
var SSO_REASONS = {discovery: 'did not answer with its settings', issuer: 'names a different issuer', unreachable: 'not reachable'};
var SSO_STOP = 'Single sign-on sessions end within 30 seconds and stay ended after switching back on: people sign in again. MCP tokens issued before stay refused. API tokens made from single sign-on sessions are refused and stay refused after switching back on: make new ones afterwards. Password logins go on.';
var SSO_CHECKS = {discovery: 'Discovery', issuer: 'Issuer', pkce: 'PKCE', keys: 'Signing keys', 'client-auth': 'Client authentication', redirect: 'Redirect URI', scope: 'Scope'};
var MCP_SWITCH = 'core.mcp';
var MCP_KEYS = ['mcp.client-id', 'mcp.audience', 'mcp.allowed-clients', 'mcp.required-role', 'mcp.issuer', 'mcp.scope', 'mcp.resource-url'], MCP_META = '/.well-known/oauth-protected-resource';
function ssoText(v) { return typeof v === 'string' ? v : ''; }
function ssoView(r) {
  r = r && typeof r === 'object' ? r : {};
  var p = r.provider && typeof r.provider === 'object' ? r.provider : {}, s = r.secret && typeof r.secret === 'object' ? r.secret : {};
  return {enabled: r.enabled === true, active: r.active === true, problem: ssoText(r.problem), redirectUri: ssoText(r.redirectUri),
    provider: {ok: p.ok !== false, since: ssoText(p.since), error: ssoText(p.error)}, values: r.values && typeof r.values === 'object' ? r.values : {},
    bases: r.bases && typeof r.bases === 'object' ? r.bases : {}, switch: ssoLayer(r.switch), secretNote: ssoText(r.secretNote),
    secret: {set: s.set === true, from: ssoText(s.from) || null, unreadable: s.unreadable === true}, inUse: ssoInUse(r.roleMapInUse), mcpBase: ssoText(r.mcpBase), mcp: mcpClean(r.mcp),
    icon: ssoIconView(r.icon), button: ssoButtonView(r.button)};
}
function ssoIconView(v) {
  v = v && typeof v === 'object' ? v : {};
  var ver = typeof v.version === 'string' && /^[0-9a-f]{1,64}$/.test(v.version) ? v.version : null;
  return {set: v.set === true && !!ver, type: ssoText(v.type), size: Number(v.size) || 0, version: ver};
}
function ssoButtonView(v) {
  v = v && typeof v === 'object' ? v : {};
  var list = function (x) { return Array.isArray(x) ? x.filter(function (s) { return typeof s === 'string' && s; }).map(function (s) { return s.toLowerCase(); }) : []; };
  var marks = (Array.isArray(v.marks) ? v.marks : []).filter(function (m) { return m && typeof m.id === 'string' && SSO_MARK_ID.test(m.id); })
    .map(function (m) { return {id: m.id, hosts: list(m.hosts), suffixes: list(m.suffixes), labels: list(m.labels), paths: Array.isArray(m.paths) ? m.paths.filter(function (s) { return typeof s === 'string' && s; }) : []}; });
  var branded = {};
  if (v.branded && typeof v.branded === 'object') Object.keys(v.branded).forEach(function (k) { if (SSO_MARK_ID.test(k) && typeof v.branded[k] === 'string') branded[k] = v.branded[k]; });
  return {defaultText: ssoText(v.defaultText) || 'Sign in with SSO', textMax: Number(v.textMax) || 40, iconMax: Number(v.iconMax) || 65536, marks: marks, branded: branded};
}
function ssoHost(issuer) {
  try {
    var u = new URL(String(issuer || '').trim());
    return u.protocol === 'http:' || u.protocol === 'https:' ? {host: u.hostname.toLowerCase(), path: u.pathname || ''} : null;
  } catch (e) { return null; }
}
function ssoMarkOf(issuer) {
  var at = ssoHost(issuer);
  if (!at || !at.host || !SSO) return 'key';
  var host = at.host, first = host.split('.')[0];
  var hit = SSO.button.marks.filter(function (m) {
    return m.hosts.indexOf(host) >= 0 || m.suffixes.some(function (s) { return host === s || host.slice(-s.length - 1) === '.' + s; }) || m.labels.indexOf(first) >= 0 ||
      m.paths.some(function (p) { return at.path.indexOf(p) >= 0; });
  })[0];
  return hit ? hit.id : 'key';
}
function ssoButtonOf(issuer, text, mode) {
  var label = String(text || '').trim(), b = SSO.button;
  if (mode === 'none') return {mark: 'none', text: label || b.defaultText, icon: null, note: ''};
  if (mode === 'company') return {mark: 'company', text: label || b.defaultText, icon: SSO.icon.version ? '/login/sso-icon?v=' + SSO.icon.version : null, note: SSO.icon.version ? '' : 'No company icon uploaded yet, so the button shows text only.'};
  var mark = ssoMarkOf(issuer), note = '';
  if (b.branded[mark]) {
    if (label) { note = 'The ' + SSO_MARK_NAMES[mark] + ' mark goes only with the text ' + b.branded[mark] + ', so this text gets the key icon.'; mark = 'key'; }
    else label = b.branded[mark];
  }
  return {mark: mark, text: label || b.defaultText, icon: '/icons/sso/' + mark + '.svg', note: note};
}
function ssoInputValue(name) {
  var inp = document.getElementById('sso-in-' + name);
  return inp ? inp.value : ssoVal(name).value;
}
function ssoPreview() {
  var card = ssoCard(), box = card && card.querySelector('.sso-preview'), note = card && card.querySelector('.sso-pv-note');
  if (!box) return;
  var shown = ssoButtonOf(ssoInputValue('issuer'), ssoInputValue('button-text'), ssoInputValue('button-icon') || 'auto');
  var b = accText('span', 'sso-pv sso-btn-' + shown.mark);
  if (shown.icon) {
    var ico = accText('span', 'sso-pv-ico');
    ico.style.backgroundImage = 'url("' + shown.icon + '")';
    b.appendChild(ico);
  }
  b.appendChild(accText('span', 'sso-pv-txt', shown.text));
  box.textContent = '';
  box.appendChild(b);
  box.dataset.mark = shown.mark;
  if (note) { note.textContent = shown.note; note.hidden = !shown.note; }
}
function ssoDomains(text) {
  var seen = {};
  return String(text || '').split(',').map(function (d) { return d.trim(); }).filter(function (d) { var k = d.toLowerCase(); if (!d || seen[k]) return false; seen[k] = 1; return true; });
}
function ssoPending() {
  var chip = document.getElementById(SSO_CHIP_IN);
  return chip ? chip.value.trim() : '';
}
function ssoChips() {
  var card = ssoCard(), inp = document.getElementById('sso-in-allowed-domains'), box = card && card.querySelector('.sso-chips');
  if (!box || !inp) return;
  box.textContent = '';
  var list = ssoDomains(inp.value);
  list.forEach(function (d) {
    var chip = accText('span', 'sso-chip'), del = accText('button', 'sso-chip-del', '\u00d7');
    chip.setAttribute('role', 'listitem');
    chip.appendChild(accText('span', 'sso-chip-t', d));
    del.type = 'button';
    del.dataset.domain = d;
    del.setAttribute('aria-label', 'Remove ' + d);
    chip.appendChild(del);
    box.appendChild(chip);
  });
  if (!list.length) box.appendChild(accText('span', 'muted sso-chips-none', 'Every domain may sign in'));
}
function ssoSetDomains(list, focusAdd) {
  var inp = document.getElementById('sso-in-allowed-domains');
  if (!inp) return;
  inp.value = list.join(', ');
  inp.dispatchEvent(new Event('input', {bubbles: true}));
  ssoChips();
  var add = ssoCard().querySelector('.sso-chip-in');
  if (focusAdd && add) add.focus();
}
function ssoAddDomain() {
  var add = ssoCard().querySelector('.sso-chip-in'), inp = document.getElementById('sso-in-allowed-domains');
  if (!add || !inp) return;
  var typed = ssoDomains(add.value);
  if (!typed.length) return;
  add.value = '';
  ssoSetDomains(ssoDomains(inp.value + ',' + typed.join(',')), true);
}
function ssoAttention() {
  if (!SSO) return '';
  if (SSO.enabled && !SSO.active && !ssoUnset()) return 'Single sign-on is on but needs setup';
  if (SSO.active && !SSO.provider.ok) return 'The identity provider is not reachable';
  if (SSO.secret.unreadable && !SSO.secret.set) return 'The client secret cannot be read';
  return '';
}
function ssoStatus() {
  var issuer = ssoVal('issuer').value, at = ssoHost(issuer), mark = ssoMarkOf(issuer), parts = [];
  parts.push(at ? 'Provider: ' + (SSO_MARK_NAMES[mark] || SSO_MARK_NAMES.key) : 'Provider: not set');
  if (at) parts.push(at.host);
  parts.push(SSO_LAST_TEST ? (SSO_LAST_TEST.ok ? 'Test passed ' : 'Test failed ') + hm(SSO_LAST_TEST.at) : 'Not tested since this page opened');
  return '<p class="sso-status" data-tested="' + (SSO_LAST_TEST ? SSO_LAST_TEST.ok ? 'ok' : 'bad' : 'none') + '">' + esc(parts.join(' \u00b7 ')) + '</p>';
}
function ssoIconBox() {
  var i = SSO.icon, state = i.set ? (SSO_ICON_TYPES[i.type] || 'Image') + ', ' + (i.size < 1024 ? i.size + ' bytes' : Math.round(i.size / 102.4) / 10 + ' KiB') : 'None uploaded';
  return '<div class="sso-icon-box"><span class="sso-fn">Company icon</span><p class="adm-help sso-icon-state">' + esc(state) + '</p>' +
    '<div class="sso-icon-acts"><label class="btn sec small sso-icon-pick">Upload image<input type="file" class="sso-icon-file" accept="image/png,image/webp,image/svg+xml"></label>' +
    (i.set ? '<button type="button" class="btn sec small sso-icon-del">Remove</button>' : '') + '</div>' +
    '<p class="adm-help">PNG, WebP or SVG, at most ' + Math.round(SSO.button.iconMax / 1024) + ' KiB, shown at 20 px. An SVG may hold only shapes, gradients, clip paths and masks: styles, text, scripts and links outside the file are refused.</p>' +
    '<p class="adm-msg sso-icon-msg" role="status"></p></div>';
}
function ssoStep(key, title, n, body) {
  return '<section class="sso-grp" data-grp="' + key + '" aria-labelledby="sso-g-' + key + '"><h4 class="sso-grp-h" id="sso-g-' + key + '"><span class="sso-num" aria-hidden="true">' + n +
    '</span><span class="sso-grp-l">' + esc(title) + '</span></h4><div class="sso-grp-b">' + body + '</div></section>';
}
function ssoGroup(g, n) {
  var fields = g[2].map(ssoField).join('');
  if (g[0] === 'button') {
    return ssoStep(g[0], g[1], n, '<div class="sso-btn-grid"><div class="sso-fields">' + fields + ssoIconBox() + '</div>' +
      '<div class="sso-preview-w"><span class="sso-fn">Preview</span><div class="sso-preview" aria-live="polite"></div><p class="adm-help sso-pv-note" hidden></p></div></div>');
  }
  var body = '<div class="sso-fields">' + fields + (g[0] === 'roles' ? '<div class="sso-roles acc-fields"></div>' : '') + '</div>';
  if (g[0] === 'provider') {
    body += '<details class="sso-adv"><summary class="chev">Advanced: endpoints and token request</summary><div class="sso-fields">' + SSO_ADVANCED.map(ssoField).join('') + '</div></details>' +
      '<div class="sso-test-row"><button type="button" class="btn sec small sso-test">Test connection</button><span class="adm-help">Checks the provider with the values typed here, without saving them.</span></div>' +
      '<div class="sso-result" aria-live="polite"></div>';
  }
  return ssoStep(g[0], g[1], n, body);
}
function ssoRedirectStep(n) {
  var uri = SSO.redirectUri;
  return ssoStep('redirect', 'Redirect URI', n, '<div class="sso-redirect"><span class="sso-fn" id="sso-uri-l">Redirect URI to register</span><div class="sso-uri-row">' +
    (uri ? '<code class="sso-uri" role="textbox" aria-readonly="true" aria-labelledby="sso-uri-l">' + esc(uri) + '</code>' : '<span class="muted sso-nouri">Not set: set SKOPAX_BASE_URL (or SSO_REDIRECT_URI) and restart.</span>') +
    '<button type="button" class="btn sec small sso-copy"' + (uri ? '' : ' disabled') + '>Copy</button></div>' +
    '<span class="adm-msg" role="status"></span><p class="adm-help">Register this address in your identity provider as an allowed redirect (callback) URI.</p></div>');
}
function ssoSteps() {
  var groups = SSO_GROUPS.slice(), unset = ssoUnset(), out = [];
  if (unset) out.push(ssoRedirectStep(1));
  groups.forEach(function (g) {
    out.push(ssoGroup(g, out.length + 1));
    if (!unset && g[0] === 'provider') out.push(ssoRedirectStep(out.length + 1));
  });
  return out.join('');
}
function ssoIntro() {
  if (!ssoUnset()) return '';
  return '<div class="sso-intro"><h4 class="sso-intro-h">Set up single sign-on in three steps</h4><ol class="sso-intro-l">' +
    '<li>Copy the redirect URI below and register it in your identity provider.</li><li>Enter the issuer URL, client ID and client secret the provider gives you.</li>' +
    '<li>Test the connection, then save and turn single sign-on on.</li></ol></div>';
}
function ssoAtIcon() {
  var card = ssoCard(), ico = card && card.querySelector('.sso-at-ico'), at = ssoHost(ssoVal('issuer').value);
  if (!ico) return;
  ico.hidden = !at;
  if (at) ico.style.backgroundImage = 'url("/icons/sso/' + ssoMarkOf(ssoVal('issuer').value) + '.svg")';
}
function ssoDirty() {
  var card = ssoCard();
  if (!card) return false;
  var d = ssoDraft(), roles = document.querySelector('#acc-sso .sso-roles');
  var changed = Object.keys(d.fields).filter(function (name) { var inp = document.getElementById('sso-in-' + name); return !(inp && inp.tagName === 'SELECT' && ssoSelectKept(inp)); });
  return changed.length > 0 || !!d.map || !!d.secret.trim() || !!d.pending || accCardEdits(roles).length > 0;
}
function ssoBarPaint() {
  var card = ssoCard(), bar = card && card.querySelector('.sso-bar');
  if (!bar) return;
  var dirty = ssoDirty();
  bar.dataset.dirty = String(dirty);
  bar.querySelector('.sso-dirty-l').hidden = !dirty;
}
async function ssoIconPut(file) {
  var card = ssoCard(), msg = card && card.querySelector('.sso-icon-msg');
  if (!file) return;
  if (file.size > SSO.button.iconMax) { ssoNote(msg, 'The icon is at most ' + Math.round(SSO.button.iconMax / 1024) + ' KiB.', true); return; }
  if (SSO_ICON_ACCEPT.indexOf(file.type) < 0) { ssoNote(msg, 'The icon must be a PNG, WebP or SVG image.', true); return; }
  ssoNote(msg, 'Uploading\u2026');
  var r;
  try {
    r = await adminApi('PUT', '/api/admin/sso/icon', file, undefined, file.type);
  } catch (e) {
    var now = ssoCard();
    ssoNote(now && now.querySelector('.sso-icon-msg'), accError(e, 'Not uploaded, try again'), true);
    return;
  }
  var draft = ssoDraft();
  SSO = ssoView(r);
  ssoRepaint('', draft, []);
  ssoIconMode('company');
  var after = ssoCard();
  ssoNote(after && after.querySelector('.sso-icon-msg'), 'Icon uploaded');
}
async function ssoIconRemove(b) {
  var ask = window.__skopaxConfirm;
  if (typeof ask !== 'function') return;
  var ok = await ask('Remove the company icon?', 'The login button shows no icon until another image is uploaded, or the icon is set to Auto or None.', 'Remove icon');
  if (!ok) { if (b.isConnected) b.focus(); return; }
  var r;
  try {
    r = await adminApi('DELETE', '/api/admin/sso/icon');
  } catch (e) {
    var card = ssoCard();
    ssoNote(card && card.querySelector('.sso-icon-msg'), accError(e, 'Not removed, try again'), true);
    return;
  }
  var draft = ssoDraft();
  SSO = ssoView(r);
  ssoRepaint('', draft, []);
  ssoIconMode('auto');
  var after = ssoCard();
  ssoNote(after && after.querySelector('.sso-icon-msg'), 'Icon removed');
}
function ssoIconMode(to) {
  var sel = document.getElementById('sso-in-button-icon');
  if (!sel || sel.value === 'none' || (to === 'auto' && sel.value !== 'company') || sel.value === to) return;
  sel.value = to;
  ssoPreview();
  ssoBarPaint();
}
function ssoFocusKey() {
  var a = document.activeElement, box = document.getElementById('acc-sso');
  if (!a || !box || !box.contains(a)) return null;
  var f = a.closest('.sso-f');
  return {id: a.id || '', cls: String(a.className || '').split(/\s+/).filter(function (c) { return /^sso-/.test(c); })[0] || '', field: f ? f.dataset.field : ''};
}
function ssoFocusBack(k) {
  var card = ssoCard();
  if (!k || !card || (document.activeElement && document.activeElement !== document.body && card.contains(document.activeElement))) return;
  var to = k.id ? document.getElementById(k.id) : null;
  if (!to && k.cls) {
    var scope = k.field ? ssoFieldEl(k.field) || card : card;
    to = scope.querySelector('.' + k.cls);
  }
  if (!to || to.disabled) to = card.querySelector('#sso-h');
  if (to) { if (to.id === 'sso-h') to.tabIndex = -1; to.focus(); }
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
  return '<span class="tag ' + (v.source === 'admin' ? 'k-act' : 'plain') + ' sso-src' + (v.source === 'admin' || v.source === 'config' ? '' : ' src-default') + '"' + (v.source === 'admin' && v.base ? ' title="' + esc('over ' + v.base) + '"' : '') + '>' + esc(text) + '</span>' + (v.source === 'admin' && v.base ? '<span class="muted sso-base">over ' + esc(v.base) + '</span>' : '');
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
  if (ssoUnset()) return ['Not set up', 'plain'];
  if (ssoAttention()) return ['Needs attention', 'k-warn'];
  if (SSO.active) return ['On', 'k-ok'];
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
  if (name === SSO_MAP) return '<div class="sso-map">' + ssoInUseRows() + '<table class="sso-map-t"><thead><tr><th scope="col">Claim value</th><th scope="col">Role</th><th><span class="sr">Actions</span></th></tr></thead><tbody>' + ssoMapRows(v.value).map(function (r) { return ssoMapRow(r.value, r.role); }).join('') + '</tbody></table>' +
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
  if (name === SSO_DOMAINS) return '<input type="text"' + id + ' class="sso-in adm-in" hidden maxlength="4000" value="' + esc(v.value) + '"><div class="sso-chips" role="list" aria-label="Allowed email domains"></div>' +
    '<div class="sso-chip-row"><input type="text" id="' + SSO_CHIP_IN + '" class="sso-chip-in adm-in" maxlength="253" spellcheck="false" autocomplete="off" autocapitalize="off" placeholder="example.com" aria-label="Add an allowed email domain">' +
    '<button type="button" class="btn sec small sso-chip-add">Add</button></div>';
  if (name === SSO_BUTTON_TEXT) return '<input type="text"' + id + ' class="sso-in adm-in" maxlength="' + SSO.button.textMax + '" spellcheck="false" autocomplete="off" placeholder="' + esc(SSO.button.defaultText) + '" value="' + esc(v.value) + '">';
  return '<input type="text"' + id + ' class="sso-in adm-in" maxlength="4000" spellcheck="false" autocomplete="off" value="' + esc(v.value) + '">';
}
function ssoField(name) {
  var label = esc(SSO_LABELS[name]), head = name === SSO_MAP ? '<b class="sso-fn">' + label + '</b>' : '<label class="sso-fn" for="' + (name === SSO_DOMAINS ? SSO_CHIP_IN : 'sso-in-' + name) + '">' + label + '</label>';
  var v = ssoVal(name), reset = name !== SSO_SEALED && v.source === 'admin' ? '<button type="button" class="btn sec small sso-reset" title="' + esc('Drop the value set here and go back to ' + (v.base || 'the default')) + '">Reset</button>' : '';
  var note = name === SSO_SEALED && SSO.secretNote ? '<p class="adm-help adm-problem sso-unreadable">' + esc(SSO.secretNote) + '</p>' : '';
  var help = ssoHelp(name);
  return '<div class="sso-f" data-field="' + esc(name) + '" data-key="sso.' + esc(name) + '"><div class="adm-n">' + head + ssoSrc(name) + reset + '</div>' + ssoControl(name) + note +
    (help ? '<p class="adm-help">' + esc(help) + '</p>' : '') + '<p class="adm-msg sso-fmsg" role="status"></p></div>';
}
function renderSso(said) {
  var kept = document.querySelector('#acc-sso .sso-roles'), edits = kept ? unsaved(kept, null) : [];
  var box = document.getElementById('acc-sso'), adding = document.querySelector('#access .acc-add-user');
  if (adding) accAddSync(adding);
  if (!box) return;
  if (!SSO) {
    box.textContent = '';
    var lost = accText('section', 'ai-key sso-card-off acc-card'), lostH = accText('h3', null, 'Single sign-on');
    lost.appendChild(lostH);
    lost.appendChild(accFailed('Single sign-on did not load.'));
    box.appendChild(lost);
    accNavPaint();
    return;
  }
  var pill = ssoPill(), prov = ssoProvider(), stops = ssoResetStops();
  box.innerHTML = '<section class="ai-key sso-card acc-card" aria-labelledby="sso-h" data-state="' + pill[1] + '"><div class="sso-top"><div class="ai-head"><h3 id="sso-h">Single sign-on</h3><span class="tag ' + pill[1] + ' sso-pill">' + pill[0] + '</span>' +
    '<span class="sso-sw-w"><button type="button" role="switch" class="feat-sw sso-sw" data-key="sso.enabled" aria-checked="' + (SSO.enabled ? 'true' : 'false') + '" aria-label="Single sign-on" title="' +
    (SSO.enabled ? 'Switch off: single sign-on sessions end within 30 seconds and stay ended after switching back on, and MCP turns off, while password logins go on' : 'Switch on') + '"></button><span class="sso-sw-l">' + ssoSource(SSO.switch) + '</span>' +
    (SSO.switch.source === 'admin' ? '<button type="button" class="btn sec small sso-sw-reset" title="' + esc('Drop the switch set here and go back to ' + (SSO.switch.base || 'the default')) + '">' + (stops ? 'Reset (turns SSO off)' : 'Reset') + '</button>' : '') + '</span></div>' +
    '<div class="sso-at"><span class="sso-at-ico" aria-hidden="true" hidden></span>' + ssoStatus() + '</div>' +
    (SSO.problem && !ssoUnset() ? '<p class="adm-help adm-problem sso-problem">' + esc(SSO.problem) + '</p>' : '') + (prov ? '<p class="adm-help sso-provider">' + esc(prov) + '</p>' : '') + '</div>' +
    ssoIntro() + ssoSteps() +
    '<div class="sso-bar" data-dirty="false"><span class="sso-dirty-l" hidden>Unsaved changes</span><div class="sso-bar-acts"><button type="button" class="btn small sso-save">Save</button>' +
    (SSO.enabled ? '' : '<button type="button" class="btn small sso-on">Save &amp; turn on</button>') + '<button type="button" class="btn sec small sso-discard" title="Drop the changes not saved yet on this card">Discard changes</button></div>' +
    '<span class="adm-msg sso-msg" role="status"></span></div></section>';
  var card = ssoCard();
  card.querySelector('.sso-test').disabled = SSO_TESTING;
  card.addEventListener('input', function (e) { ssoUnmark(e.target.closest('.sso-f')); ssoLive(e.target); ssoBarPaint(); });
  card.addEventListener('change', function (e) {
    ssoUnmark(e.target.closest('.sso-f'));
    ssoLive(e.target);
    if (e.target.classList.contains('sso-icon-file')) ssoIconPut(e.target.files && e.target.files[0]);
    ssoBarPaint();
  });
  card.addEventListener('keydown', function (e) { if (e.key === 'Enter' && e.target.classList.contains('sso-chip-in')) { e.preventDefault(); ssoAddDomain(); } });
  card.addEventListener('click', function (e) { ssoClick(e); ssoBarPaint(); });
  renderSsoRoles();
  var scopeIn = document.getElementById('sso-in-scope');
  var scopeItem = adminItems().filter(function (x) { return x.key === 'sso.scope'; })[0];
  if (scopeIn && scopeIn.tagName === 'INPUT') admChipsGate(admChipsMount(scopeIn, (scopeItem && scopeItem.items) || {kind: 'scope', separator: ' '}, SSO_LABELS.scope));
  var again = document.querySelector('#acc-sso .sso-roles');
  if (again && edits.length) restoreUnsaved(again, edits);
  ssoChips();
  ssoPreview();
  ssoAtIcon();
  ssoBarPaint();
  accNavPaint();
  if (said) ssoSay(said);
}
function ssoLive(t) {
  if (t && (t.id === 'sso-in-issuer' || t.id === 'sso-in-button-text' || t.id === 'sso-in-button-icon')) ssoPreview();
}
function ssoRoleItems() { return adminItems().filter(function (i) { return SSO_ROLE_KEYS.indexOf(i.key) >= 0; }); }
function renderSsoRoles(saved) {
  var box = document.querySelector('#acc-sso .sso-roles');
  if (!box || !ADMIN) return;
  adminKeep(box, saved, function () {
    box.textContent = '';
    ssoRoleItems().forEach(function (i) { box.appendChild(adminRow(i, true)); });
  });
}
function ssoClick(e) {
  var t = e.target.closest('button');
  if (!t || t.disabled) return;
  var f = t.closest('.sso-f');
  if (t.classList.contains('sso-map-add')) return ssoAddRow(f);
  if (t.classList.contains('sso-map-del')) { t.closest('tr').remove(); ssoUnmark(f); return; }
  if (t.classList.contains('sso-chip-del')) { var at = document.getElementById('sso-in-allowed-domains'); return ssoSetDomains(ssoDomains(at.value).filter(function (d) { return d !== t.dataset.domain; }), true); }
  if (t.classList.contains('sso-chip-add')) return ssoAddDomain();
  if (t.classList.contains('sso-icon-del')) return ssoIconRemove(t);
  if (t.classList.contains('sso-discard')) return ssoDiscard();
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
  var roles = accCardEdits(document.querySelector('#acc-sso .sso-roles'));
  if (!on && !Object.keys(values).length && !roles.length) { ssoSay('Nothing changed'); return; }
  if (roles.length) return ssoSaveWithRoles(on, values, roles);
  ssoPut(on ? {values: values, enabled: true} : {values: values}, on ? 'Saved and turned on' : 'Saved');
}
async function ssoSaveWithRoles(on, values, roles) {
  ssoBusy(true);
  ssoSay('Saving\u2026');
  var failed = await accSaveFields(roles), sso = on || Object.keys(values).length, ok = true;
  if (sso) ok = await ssoPut(on ? {values: values, enabled: true} : {values: values}, on ? 'Saved and turned on' : 'Saved');
  else ssoBusy(false);
  accShowSaid(roles);
  var card = ssoCard(), msg = card && card.querySelector('.sso-msg');
  if (failed.length) ssoSay((ok ? '' : msg.textContent + '; ') + accNotSaved(failed), true);
  else if (!sso) ssoSay('Saved');
}
function ssoDiscard() {
  var roles = document.querySelector('#acc-sso .sso-roles');
  if (roles) roles.textContent = '';
  renderSso('Changes discarded');
  renderMcpRows(SSO_KEEP);
  var b = ssoCard() && ssoCard().querySelector('.sso-discard');
  if (b) b.focus();
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
    if (inp.tagName === 'SELECT' && ssoSelectKept(inp)) return;
    now = inp.type === 'checkbox' ? (inp.checked ? 'true' : 'false') : inp.value === inp.defaultValue ? ssoVal(name).value : inp.value.trim();
    if (name === SSO_DOMAINS && ssoPending()) now = ssoDomains(inp.value + ',' + ssoPending()).join(', ');
    if (now !== ssoVal(name).value) out[name] = now;
  });
  var secret = document.getElementById('sso-in-' + SSO_SEALED);
  if (secret && !secret.readOnly && secret.value.trim()) out[SSO_SEALED] = secret.value.trim();
  return refused ? null : out;
}
function ssoSelectKept(inp) {
  var was = [].filter.call(inp.options, function (o) { return o.defaultSelected; })[0];
  return was ? was.value === inp.value : inp.selectedIndex === 0;
}
function ssoRowsOf(card) {
  return [].map.call(card.querySelectorAll('.sso-map-row'), function (r) {
    var v = r.querySelector('.sso-map-v'), role = r.querySelector('.sso-map-r'), was = [].filter.call(role.options, function (o) { return o.defaultSelected; })[0];
    return {value: v.value, orig: v.defaultValue, role: role.value, origRole: was ? was.value : 'viewer'};
  });
}
function ssoDraft() {
  var card = ssoCard(), out = {fields: {}, map: null, secret: '', pending: ssoPending()};
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
    if (inp.dataset.chips) inp.dispatchEvent(new Event('input'));
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
  var chip = document.getElementById(SSO_CHIP_IN);
  if (draft.pending && chip && skip.indexOf(SSO_DOMAINS) < 0) chip.value = draft.pending;
  var secret = document.getElementById('sso-in-' + SSO_SEALED);
  var shown = typeof curTab !== 'undefined' && curTab === 'access';
  if (shown && draft.secret && secret && !secret.readOnly && skip.indexOf(SSO_SEALED) < 0) secret.value = draft.secret;
}
function ssoRepaint(said, draft, skip) {
  var back = ssoFocusKey();
  renderSso(said);
  ssoRestore(draft, skip);
  ssoChips();
  ssoPreview();
  renderMcpRows(SSO_KEEP);
  ssoBarPaint();
  ssoFocusBack(back);
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
  return sent.fields[name] !== late.fields[name] || (name === SSO_DOMAINS && sent.pending !== late.pending);
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
    return false;
  }
  var draft = ssoDraft();
  SSO = ssoView(r);
  ssoRepaint(said, draft, skip.filter(function (name) { return !ssoEditedSince(sent, draft, name); }));
  return true;
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
    ssoTested(r);
  } catch (e) {
    if (e && e.field && ssoMark(e.field, accError(e, ''))) ssoSay('The test did not run: see ' + (SSO_LABELS[e.field] || e.field), true);
    else ssoSay(accError(e, 'The test could not run'), true);
  }
  SSO_TESTING = false;
  var now = ssoCard();
  if (now) now.querySelector('.sso-test').disabled = false;
}
function ssoTested(r) {
  SSO_LAST_TEST = {ok: !!r && r.ok === true, at: new Date().toISOString()};
  var line = document.querySelector('#acc-sso .sso-status');
  if (line) line.outerHTML = ssoStatus();
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
    s.classList.add('acc-card');
    var head = accText('div', 'ai-head'), state = mcpState();
    head.appendChild(accText('h3', null, 'MCP sign-in'));
    if (state[0]) head.appendChild(accText('span', 'tag ' + state[1] + ' acc-mcp-pill', state[0]));
    s.appendChild(head);
    var live = mcpStatusBlock();
    if (live) s.appendChild(live);
    s.appendChild(accText('p', 'acc-mcp-method', 'Method: access tokens from the identity provider (OAuth), checked against the settings below'));
    s.appendChild(accText('p', 'adm-help acc-mcp-note', 'MCP signs people in through the identity provider, so it needs single sign-on set up and on, and the MCP server switch on in Features' +
      (SSO && !SSO.active ? '. Single sign-on is not on, so MCP is off.' : '.')));
    if (!ADMIN || MCP_FAILED) s.appendChild(accText('p', 'muted acc-mcp-note acc-mcp-failed', 'MCP settings did not load; open the tab again.'));
    var fields = accText('div', 'acc-fields acc-mcp-fields');
    s.appendChild(fields);
    items.forEach(function (i) {
      var row = adminRow(i, true), input = row.querySelector('.adm-in');
      if (i.key === 'mcp.audience' && input) input.placeholder = client && client.value ? client.value : 'the client ID';
      if (i.key === 'mcp.resource-url') {
        var meta = accText('p', 'adm-help acc-mcp-meta', 'Metadata: ');
        meta.appendChild(accText('code', null, mcpMetaUrl(i.value)));
        row.querySelector('.adm-c').appendChild(meta);
      }
      fields.appendChild(row);
    });
    if (items.length) {
      var acts = accText('div', 'adm-acts acc-mcp-acts'), save = accText('button', 'btn small acc-mcp-save', 'Save'), said = accText('span', 'adm-msg acc-mcp-msg');
      save.type = 'button';
      said.setAttribute('role', 'status');
      acts.appendChild(save);
      acts.appendChild(said);
      s.appendChild(acts);
      save.addEventListener('click', function () { mcpSave(save, said); });
    }
    box.appendChild(s);
    admChipsGate(box);
  });
  accNavPaint();
}
async function mcpSave(b, said) {
  if (admChipsBad(document.getElementById('acc-mcp'))) return;
  var edits = accCardEdits(document.getElementById('acc-mcp'));
  if (!edits.length) { ssoNote(said, 'Nothing changed'); return; }
  b.disabled = true;
  ssoNote(said, 'Saving\u2026');
  var failed = await accSaveFields(edits), now = document.querySelector('#acc-mcp .acc-mcp-msg'), again = document.querySelector('#acc-mcp .acc-mcp-save');
  if (now) ssoNote(now, failed.length ? accNotSaved(failed) : 'Saved', failed.length > 0);
  if (again) { again.disabled = false; again.focus(); }
}
var MCP_MISSING = {sso: 'Single sign-on is not on', 'switch': 'The MCP server switch is off', issuer: 'No issuer is known: set the MCP issuer or the single sign-on issuer'};
var MCP_PILLS = {connected: ['Connected', 'k-ok'], waiting: ['Waiting', 'k-info'], 'not-set-up': ['Not set up', 'plain']};
function mcpClean(m) {
  if (!m || typeof m !== 'object') return null;
  var values = {};
  ['client-id', 'audience', 'issuer'].forEach(function (k) {
    var e = m.values && m.values[k];
    if (e && typeof e === 'object' && typeof e.value === 'string' && e.value) values[k] = {value: e.value, default: e.default === true};
  });
  var last = m.last && typeof m.last === 'object' && typeof m.last.at === 'string' ? {at: m.last.at, by: ssoText(m.last.by)} : null;
  return {state: MCP_PILLS[m.state] ? m.state : 'not-set-up', missing: ssoText(m.missing), ssoOn: m.ssoOn === true, on: m.on === true, mounted: m.mounted === true,
    days: Number(m.days) > 0 ? Number(m.days) : 7, people: Number(m.people) > 0 ? Math.floor(Number(m.people)) : 0, last: last, values: values};
}
function mcpStatusBlock() {
  var m = SSO && SSO.mcp;
  if (!m || typeof m !== 'object') return null;
  var pill = MCP_PILLS[m.state] || MCP_PILLS['not-set-up'], box = accText('div', 'acc-mcp-status'), line = accText('p', 'acc-mcp-line');
  box.dataset.state = m.state;
  box.appendChild(accText('span', 'tag ' + pill[1] + ' acc-mcp-conn', pill[0]));
  if (m.state === 'connected') {
    var n = Number(m.people) || 0, days = Number(m.days) || 7;
    line.appendChild(accText('span', 'acc-mcp-people', n + (n === 1 ? ' person in the last ' : ' people in the last ') + days + ' days'));
    if (m.last && m.last.at) {
      line.appendChild(accText('span', 'acc-mcp-last-l', 'Last sign-in'));
      var at = accText('span', 'acc-mcp-last-at', dayhmZone(m.last.at));
      at.title = m.last.at;
      line.appendChild(at);
      if (m.last.by) {
        line.appendChild(accText('span', 'acc-mcp-last-by-l', 'Signed in by'));
        line.appendChild(accText('span', 'acc-mcp-last-by', m.last.by));
      }
    }
  } else if (m.state === 'waiting') {
    line.appendChild(accText('span', 'acc-mcp-people', 'Set up, no one has connected yet'));
  } else {
    line.appendChild(accText('span', 'acc-mcp-people', MCP_MISSING[m.missing] || 'Not set up'));
  }
  box.appendChild(line);
  var set = accText('dl', 'acc-mcp-set');
  var item = function (label, value, code, isDefault) {
    var row = accText('div', 'acc-mcp-set-i');
    if (label) row.appendChild(accText('dt', null, label));
    var dd = accText('dd', null, '');
    dd.appendChild(accText(code ? 'code' : 'span', null, value));
    if (isDefault) dd.appendChild(accText('span', 'tag plain acc-mcp-def', 'built-in default'));
    row.appendChild(dd);
    set.appendChild(row);
  };
  item('', m.ssoOn ? 'Single sign-on is on' : 'Single sign-on is off');
  item('', m.on && m.mounted ? 'The MCP endpoint is mounted' : 'The MCP endpoint is switched off');
  var v = m.values || {};
  [['MCP client ID', 'client-id'], ['MCP audience', 'audience'], ['MCP issuer', 'issuer']].forEach(function (x) {
    var e = v[x[1]];
    if (e && e.value) item(x[0], e.value, true, e.default === true);
  });
  box.appendChild(set);
  return box;
}
function mcpSwitch() {
  var i = adminItems().filter(function (x) { return x.key === MCP_SWITCH; })[0];
  return i ? i.value === 'true' : null;
}
function mcpState() {
  if (!SSO || MCP_FAILED) return ['', 'plain', ''];
  var on = mcpSwitch();
  if (!SSO.active) return ['Off', 'plain', on === true && !ssoUnset() ? 'The MCP server is switched on, but single sign-on is not on' : ''];
  if (on === false) return ['Off', 'plain', ''];
  return on === true ? ['On', 'k-ok', ''] : ['Ready', 'plain', ''];
}
