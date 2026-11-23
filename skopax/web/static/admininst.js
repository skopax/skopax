var INST_KEY = 'core.workspace-links', INST_SEQ = 0, INST_DRAFT = null, INST_TOLD = null;
var INST_FROM = null;
var INST_ICON_KEY = 'core.workspace-icon', INST_ICON_TOLD = null, INST_ICON_MAX = 64 * 1024, INST_ICON_ACCEPT = ['image/png', 'image/webp', 'image/svg+xml'];
var INST_ICON_TYPES = {'image/png': 'PNG', 'image/webp': 'WebP', 'image/svg+xml': 'SVG'};
var INST_ICON_MODES = [['letter', 'Letter'], ['company', 'Company icon'], ['upload', 'Upload']];
var INST_COMMA = 'An address cannot hold a comma, which separates the entries';
var INST_DEFAULTS = {max: 10, nameMax: 40, rules: {}};
function instItem() { return adminItems().filter(function (i) { return i.key === INST_KEY; })[0] || null; }
function instRules() {
  var item = instItem(), links = item && item.links ? item.links : INST_DEFAULTS;
  return {max: links.max || INST_DEFAULTS.max, nameMax: links.nameMax || INST_DEFAULTS.nameMax, rules: links.rules || {}};
}
function instParse(text) {
  return String(text || '').split(',').map(function (p) { return p.trim(); }).filter(Boolean).map(function (p) {
    var at = p.indexOf('=');
    return at < 0 ? {name: p, url: ''} : {name: p.slice(0, at).trim(), url: p.slice(at + 1).trim()};
  });
}
function instText(rows) {
  return rows.filter(function (r) { return r.name || r.url; }).map(function (r) { return r.name + '=' + r.url; }).join(', ');
}
function instNameOk(name) { return new RegExp('^[A-Za-z0-9 ._-]{1,' + instRules().nameMax + '}$').test(name); }
function instRule(name) {
  var r = instRules();
  return r.rules[name] || name;
}
function instUrlProblem(url) {
  if (/[\s\u0000-\u001f\u007f]/.test(url)) return instRule('url');
  var m = /^([A-Za-z][A-Za-z0-9+.-]*):\/\/([^\/?#]*)/.exec(url);
  if (!m || ['http', 'https'].indexOf(m[1].toLowerCase()) < 0) return instRule('url');
  if (m[2].indexOf('@') >= 0) return instRule('userinfo');
  try { if (!new URL(url).hostname) return instRule('url'); } catch (e) { return instRule('url'); }
  return null;
}
function instProblems(rows) {
  var seen = {}, out = [], max = instRules(), counted = 0;
  rows.forEach(function (r, n) {
    var blank = !r.name && !r.url, p = null;
    if (blank) { out.push(null); return; }
    counted += 1;
    if (counted > max.max) p = instRule('count');
    else if (!r.name || !r.url) p = instRule('pair');
    else if (!instNameOk(r.name)) p = instRule('name');
    else if (r.url.indexOf(',') >= 0) p = INST_COMMA;
    else if (instUrlProblem(r.url)) p = instUrlProblem(r.url);
    else if (seen[r.name.toLowerCase()]) p = instRule('repeat');
    else seen[r.name.toLowerCase()] = true;
    out.push(p);
  });
  return out;
}
function instDraft() {
  var item = instItem();
  var held = item && typeof item.value === 'string' ? item.value : '';
  if (!INST_DRAFT || INST_FROM !== held) { INST_DRAFT = instParse(held); INST_FROM = held; }
  return INST_DRAFT;
}
async function loadInstances() {
  var seq = ++INST_SEQ, box = document.getElementById('workspaces');
  try {
    var r = await adminApi('GET', '/api/admin/settings');
    if (seq !== INST_SEQ) return;
    ADMIN = r;
    INST_DRAFT = null;
        INST_TOLD = null;
    INST_ICON_TOLD = null;
    markAdminCount();
    renderInstances();
    adminReveal('workspaces');
  } catch (e) {
    if (seq !== INST_SEQ) return;
    adminUnreveal('workspaces');
    adminFailed(box, adminDenied(e) ? null : 'Workspaces did not load; open the tab again.');
  }
}
function instTold(text, bad) { INST_TOLD = text ? {text: text, bad: !!bad} : null; }
function instMove(rows, n, by) {
  var to = n + by;
  if (to < 0 || to >= rows.length) return;
  var held = rows[n];
  rows[n] = rows[to];
  rows[to] = held;
}
function instRow(rows, n, problem) {
  var r = rows[n], li = document.createElement('li');
  li.className = 'ws-row' + (problem ? ' bad' : '');
  li.dataset.n = String(n);
  var name = document.createElement('input'), url = document.createElement('input');
  name.className = 'ws-name';
  name.type = 'text';
  name.value = r.name;
  name.maxLength = instRules().nameMax;
  name.setAttribute('aria-label', 'Name');
  name.setAttribute('placeholder', 'staging');
  name.setAttribute('autocomplete', 'off');
  name.setAttribute('spellcheck', 'false');
  url.className = 'ws-url';
  url.type = 'url';
  url.value = r.url;
  url.setAttribute('aria-label', 'Address');
  url.setAttribute('placeholder', 'https://staging.example.com');
  url.setAttribute('autocomplete', 'off');
  url.setAttribute('autocapitalize', 'off');
  url.setAttribute('spellcheck', 'false');
  var acts = document.createElement('div');
  acts.className = 'ws-acts';
  var open = document.createElement('a');
  open.className = 'btn sec small ws-open';
  open.textContent = 'Open workspace';
  var safe = r.url && !instUrlProblem(r.url);
  if (safe) { open.setAttribute('href', r.url); open.setAttribute('target', '_blank'); open.setAttribute('rel', 'noopener noreferrer'); }
  else open.setAttribute('aria-disabled', 'true');
  acts.appendChild(open);
  var up = accText('button', 'btn sec small ws-up', '\u2191'), down = accText('button', 'btn sec small ws-down', '\u2193'), rm = accText('button', 'btn sec small ws-rm', 'Remove');
  up.type = down.type = rm.type = 'button';
  up.setAttribute('aria-label', 'Move up');
  up.title = 'Move up';
  down.setAttribute('aria-label', 'Move down');
  down.title = 'Move down';
  up.disabled = n === 0;
  down.disabled = n === rows.length - 1;
  up.addEventListener('click', function () { instMove(rows, n, -1); renderInstances(null, 'up', n - 1); });
  down.addEventListener('click', function () { instMove(rows, n, 1); renderInstances(null, 'down', n + 1); });
  rm.addEventListener('click', function () { rows.splice(n, 1); renderInstances(null, 'add', Math.min(n, rows.length - 1)); });
  acts.appendChild(up);
  acts.appendChild(down);
  acts.appendChild(rm);
  var typed = function () { r.name = name.value.trim(); r.url = url.value.trim(); instPaintState(); };
  name.addEventListener('input', typed);
  url.addEventListener('input', typed);
  var why = accText('p', 'adm-msg bad ws-why', problem || '');
  why.setAttribute('role', 'status');
  li.appendChild(name);
  li.appendChild(url);
  li.appendChild(acts);
  li.appendChild(why);
  return li;
}
function instPaintState() {
  var box = document.getElementById('workspaces'), item = instItem();
  if (!box || !item) return;
  var rows = instDraft(), problems = instProblems(rows), text = instText(rows);
  [].forEach.call(box.querySelectorAll('.ws-row'), function (li, n) {
    var p = problems[n], why = li.querySelector('.ws-why'), r = rows[n], open = li.querySelector('.ws-open');
    li.classList.toggle('bad', !!p);
    if (why.textContent !== (p || '')) why.textContent = p || '';
    var safe = r.url && !instUrlProblem(r.url);
    if (safe) { open.setAttribute('href', r.url); open.setAttribute('target', '_blank'); open.setAttribute('rel', 'noopener noreferrer'); open.removeAttribute('aria-disabled'); }
    else { open.removeAttribute('href'); open.setAttribute('aria-disabled', 'true'); }
  });
  var save = box.querySelector('.ws-save'), max = instRules().max, add = box.querySelector('.ws-add'), revert = box.querySelector('.ws-discard');
  var changed = text !== instText(instParse(typeof item.value === 'string' ? item.value : ''));
  if (save) save.disabled = !changed || problems.some(Boolean);
  if (revert) revert.hidden = !changed;
  if (add) add.disabled = rows.filter(function (r) { return r.name || r.url; }).length >= max;
}
function renderInstances(saved, focusKind, focusAt) {
  var box = document.getElementById('workspaces');
  if (!box || !ADMIN) return;
  var item = instItem();
  box.textContent = '';
  if (!item) { adminFailed(box, 'Workspaces did not load; open the tab again.'); return; }
  var rows = instDraft(), problems = instProblems(rows), info = ADMIN.instance || {name: '', label: '', from: ''}, max = instRules().max;
  var card = el('section', 'adm-card plain ws-card');
  var me = el('div', 'ws-me');
  me.appendChild(accText('h3', 'ws-h', 'This workspace'));
  var nameLine = el('p', 'ws-me-n');
  var nm = accText('b', null, info.name || '');
  nm.setAttribute('data-no-tx', '');
  nameLine.appendChild(nm);
  if (info.label) nameLine.appendChild(el('span', 'tag plain', esc(info.from || 'environment')));
  me.appendChild(nameLine);
  me.appendChild(accText('p', 'adm-help', info.label ? 'Set by SKOPAX_WORKSPACE_LABEL in the service configuration; this page shows it and does not change it.' : 'Named after its address. Set SKOPAX_WORKSPACE_LABEL in the service configuration to give it a name.'));
  card.appendChild(me);
  var icon = instIconBlock();
  if (icon) card.appendChild(icon);
  var wrap = el('div', 'adm-row ws-list' + (item.source === 'admin' ? ' set' : ''));
  wrap.dataset.key = INST_KEY;
  wrap.dataset.find = (item.label + ' ' + item.key).toLowerCase();
  var head = el('div', 'ws-head');
  var title = accText('h3', 'ws-h', 'Other workspaces');
  title.id = 'ws-h';
  head.appendChild(title);
  var src = el('span', 'ws-src', item.source === 'config' && item.from ? '' : adminSource(item));
  if (item.source === 'config' && item.from) {
    var from = accText('span', 'tag plain', String(item.from));
    from.title = 'A value saved here wins over it';
    src.appendChild(from);
  }
  head.appendChild(src);
  if (item.source === 'admin') {
    var reset = accText('button', 'btn sec small ws-reset', 'Reset');
    reset.type = 'button';
    reset.title = item.base ? 'Drop the list saved here and go back to the configured list' : 'Drop the list saved here and go back to the default';
    head.appendChild(reset);
  }
  wrap.appendChild(head);
  if (item.problem) wrap.appendChild(accText('p', 'adm-help adm-problem', item.problem));
  var list = el('ol', 'ws-rows');
  list.setAttribute('aria-labelledby', 'ws-h');
  rows.forEach(function (r, n) { list.appendChild(instRow(rows, n, problems[n])); });
  if (!rows.length) list.appendChild(accText('li', 'muted ws-none', 'No other workspaces yet'));
  wrap.appendChild(list);
  var acts = el('div', 'adm-acts ws-actions');
  var add = accText('button', 'btn sec small ws-add', '+ Add workspace'), save = accText('button', 'btn small ws-save', 'Save'), discard = accText('button', 'btn sec small ws-discard', 'Discard changes');
  add.type = save.type = discard.type = 'button';
  acts.appendChild(add);
  acts.appendChild(save);
  acts.appendChild(discard);
  add.addEventListener('click', function () { rows.push({name: '', url: ''}); renderInstances(null, 'add', rows.length - 1); });
  save.addEventListener('click', function () { instSave(save); });
  discard.addEventListener('click', function () { INST_DRAFT = null; instTold(''); renderInstances(); });
  var msg = accText('span', 'adm-msg ws-msg', INST_TOLD ? INST_TOLD.text : '');
  msg.className = 'adm-msg ws-msg' + (INST_TOLD ? (INST_TOLD.bad ? ' bad' : ' ok') : '');
  msg.setAttribute('role', 'status');
  acts.appendChild(msg);
  wrap.appendChild(acts);
  wrap.appendChild(accText('p', 'adm-help ws-max', 'At most ' + max + ' workspaces. A name is letters, digits, spaces, dots, dashes or underscores; an address is http:// or https:// without a user name or password.'));
  card.appendChild(wrap);
  box.appendChild(card);
  var reset2 = box.querySelector('.ws-reset');
  if (reset2) reset2.addEventListener('click', function () { instReset(reset2); });
  instPaintState();
  if (focusKind && !rows.length) { var addBtn = box.querySelector('.ws-add'); if (addBtn) addBtn.focus(); }
  else if (focusKind) {
    var target = box.querySelectorAll('.ws-row')[focusAt];
    var btn = target && (focusKind === 'up' ? target.querySelector('.ws-up:not(:disabled)') : focusKind === 'down' ? target.querySelector('.ws-down:not(:disabled)') : null);
    var first2 = target && (btn || target.querySelector('.ws-name'));
    if (first2) first2.focus();
  }
}
async function instSave(b) {
  var item = instItem(), text = instText(instDraft());
  b.disabled = true;
  instTold('Saving\u2026');
  var msg = document.querySelector('#workspaces .ws-msg');
  if (msg) { msg.textContent = 'Saving\u2026'; msg.className = 'adm-msg ws-msg'; }
  try {
    var r = await adminApi('PUT', '/api/admin/settings', {key: item.key, value: text});
    INST_DRAFT = null;
        instTold(r.item.live ? 'Saved, live within ' + ((ADMIN && ADMIN.refreshSeconds) || 15) + ' s on every pod' : 'Saved, used after the next restart');
    await adminDone(r, INST_TOLD.text);
  } catch (e) {
    instTold(e && e.detail ? e.detail : 'Not saved, try again', true);
    renderInstances();
  }
}
async function instReset(b) {
  var item = instItem();
  b.disabled = true;
  try {
    var r = await adminApi('DELETE', '/api/admin/settings?key=' + encodeURIComponent(item.key));
    INST_DRAFT = null;
        instTold('Reset to ' + (r.item.value || 'empty'));
    await adminDone(r, INST_TOLD.text);
  } catch (e) {
    instTold(e && e.detail ? e.detail : 'Not reset, try again', true);
    renderInstances();
  }
}
function instIconItem() { return adminItems().filter(function (i) { return i.key === INST_ICON_KEY; })[0] || null; }
function instIconInfo() {
  var i = ADMIN && ADMIN.instance && ADMIN.instance.icon ? ADMIN.instance.icon : {}, none = {set: false, type: null, size: 0, version: null};
  return {company: i.company || none, upload: i.upload || none, max: i.max || INST_ICON_MAX, problem: i.problem || ''};
}
function instIconMode() {
  var item = instIconItem(), v = item && typeof item.value === 'string' ? item.value : 'letter';
  return INST_ICON_MODES.some(function (m) { return m[0] === v; }) ? v : 'letter';
}
function instIconSize(n) { return n < 1024 ? n + ' bytes' : Math.round(n / 102.4) / 10 + ' KiB'; }
function instIconTold(text, bad) { INST_ICON_TOLD = text ? {text: text, bad: !!bad} : null; }
function instIconNote(text, bad) {
  instIconTold(text, bad);
  var m = document.querySelector('#workspaces .ws-icon-msg');
  if (!m) return;
  m.textContent = text || '';
  m.className = 'adm-msg ws-icon-msg' + (text ? (bad ? ' bad' : ' ok') : '');
}
function instIconPreview(mode, info) {
  var tile = accText('span', 'inst-tile ws-icon-pv', ''), shown = mode === 'company' ? info.company : mode === 'upload' ? info.upload : null;
  tile.setAttribute('aria-hidden', 'true');
  var name = ADMIN && ADMIN.instance && ADMIN.instance.name ? String(ADMIN.instance.name) : location.hostname;
  var letter = (Array.from(name)[0] || '?').toUpperCase();
  if (shown && shown.set && shown.version) {
    var img = document.createElement('img');
    img.alt = '';
    img.addEventListener('error', function () { if (img.parentNode === tile) { tile.classList.remove('has-img'); tile.textContent = letter; } });
    img.src = '/api/workspace-icon?v=' + encodeURIComponent(shown.version);
    tile.classList.add('has-img');
    tile.appendChild(img);
  } else tile.textContent = letter;
  return tile;
}
function instIconBlock() {
  var item = instIconItem();
  if (!item) return null;
  var mode = instIconMode(), info = instIconInfo();
  var box = el('div', 'adm-row ws-icon');
  box.dataset.key = INST_ICON_KEY;
  var head = el('div', 'ws-head');
  var title = accText('h3', 'ws-h', 'Workspace icon');
  title.id = 'ws-icon-h';
  head.appendChild(instIconPreview(mode, info));
  head.appendChild(title);
  box.appendChild(head);
  var group = el('div', 'ws-icon-modes');
  group.setAttribute('role', 'radiogroup');
  group.setAttribute('aria-labelledby', 'ws-icon-h');
  INST_ICON_MODES.forEach(function (m) {
    var lab = el('label', 'ws-icon-mode'), radio = document.createElement('input');
    radio.type = 'radio';
    radio.name = 'ws-icon-mode';
    radio.value = m[0];
    radio.checked = m[0] === mode;
    radio.addEventListener('change', function () { if (radio.checked) instIconSave(m[0]); });
    lab.appendChild(radio);
    lab.appendChild(accText('span', null, m[1]));
    group.appendChild(lab);
  });
  box.appendChild(group);
  if (info.problem) box.appendChild(accText('p', 'adm-help adm-problem', info.problem));
  if (mode === 'company') {
    if (info.company.set) box.appendChild(accText('p', 'adm-help ws-icon-state', 'The icon uploaded for the single sign-on button under Access & sign-in.'));
    else {
      var none = el('p', 'adm-help ws-icon-state');
      none.appendChild(accText('span', null, 'No company icon uploaded yet, so the tile shows the letter.'));
      none.appendChild(document.createTextNode(' '));
      var go = accText('button', 'btn sec small ws-icon-go', 'Upload it under Access & sign-in');
      go.type = 'button';
      go.addEventListener('click', function () { if (typeof pickTab === 'function') pickTab('access'); });
      none.appendChild(go);
      box.appendChild(none);
    }
  }
  if (mode === 'upload') {
    var up = info.upload, pick = el('div', 'sso-icon-box ws-icon-up');
    pick.appendChild(accText('p', 'adm-help ws-icon-state', up.set ? (INST_ICON_TYPES[up.type] || 'Image') + ', ' + instIconSize(up.size) : 'None uploaded, so the tile shows the letter.'));
    var acts = el('div', 'sso-icon-acts');
    var lab2 = accText('label', 'btn sec small sso-icon-pick', 'Upload image'), file = document.createElement('input');
    file.type = 'file';
    file.className = 'sso-icon-file ws-icon-file';
    file.accept = INST_ICON_ACCEPT.join(',');
    file.addEventListener('change', function () { instIconPut(file.files && file.files[0]); });
    lab2.appendChild(file);
    acts.appendChild(lab2);
    if (up.set) {
      var rm = accText('button', 'btn sec small ws-icon-del', 'Remove');
      rm.type = 'button';
      rm.addEventListener('click', function () { instIconRemove(rm); });
      acts.appendChild(rm);
    }
    pick.appendChild(acts);
    pick.appendChild(accText('p', 'adm-help', 'PNG, WebP or SVG, at most ' + Math.round(info.max / 1024) + ' KiB, shown at 24 px. An SVG may hold only shapes, gradients, clip paths and masks: styles, text, scripts and links outside the file are refused.'));
    box.appendChild(pick);
  }
  var msg = accText('p', 'adm-msg ws-icon-msg' + (INST_ICON_TOLD ? (INST_ICON_TOLD.bad ? ' bad' : ' ok') : ''), INST_ICON_TOLD ? INST_ICON_TOLD.text : '');
  msg.setAttribute('role', 'status');
  box.appendChild(msg);
  return box;
}
async function instIconMe() {
  if (typeof instRender !== 'function') return;
  try { instRender(await adminApi('GET', '/api/me')); } catch (e) {}
}
async function instIconSave(mode, said) {
  instIconNote('Saving\u2026');
  try {
    var r = await adminApi('PUT', '/api/admin/settings', {key: INST_ICON_KEY, value: mode});
    instIconTold(said || 'Saved');
    await adminDone(r, INST_ICON_TOLD.text);
    instIconMe();
  } catch (e) {
    instIconTold(e && e.detail ? e.detail : 'Not saved, try again', true);
    renderInstances();
  }
}
async function instIconPut(file) {
  var info = instIconInfo();
  if (!file) return;
  if (file.size > info.max) { instIconNote('The icon is at most ' + Math.round(info.max / 1024) + ' KiB.', true); return; }
  if (INST_ICON_ACCEPT.indexOf(file.type) < 0) { instIconNote('The icon must be a PNG, WebP or SVG image.', true); return; }
  instIconNote('Uploading\u2026');
  var r;
  try {
    r = await adminApi('PUT', '/api/admin/workspace/icon', file, undefined, file.type);
  } catch (e) {
    instIconNote(e && e.detail ? e.detail : 'Not uploaded, try again', true);
    return;
  }
  if (ADMIN && ADMIN.instance) ADMIN.instance.icon = r.icon;
  try { ADMIN_HIST = await adminApi('GET', '/api/admin/history?limit=' + ADMIN_HIST_MAX); } catch (e) {}
  instIconTold('Icon uploaded');
  renderInstances();
  renderChanges();
  instIconMe();
}
async function instIconRemove(b) {
  var ask = window.__skopaxConfirm;
  if (typeof ask !== 'function') return;
  var ok = await ask('Remove the workspace icon?', 'The workspace tile shows the letter until another image is uploaded.', 'Remove icon');
  if (!ok) { if (b.isConnected) b.focus(); return; }
  var r;
  try {
    r = await adminApi('DELETE', '/api/admin/workspace/icon');
  } catch (e) {
    instIconNote(e && e.detail ? e.detail : 'Not removed, try again', true);
    return;
  }
  if (ADMIN && ADMIN.instance) ADMIN.instance.icon = r.icon;
  try { ADMIN_HIST = await adminApi('GET', '/api/admin/history?limit=' + ADMIN_HIST_MAX); } catch (e) {}
  instIconTold('Icon removed');
  renderInstances();
  renderChanges();
  instIconMe();
}
