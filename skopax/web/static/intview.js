var INT_SIGNED = null, INT_WANTED = false, INT_PREVIEW = false, INT_RO_PATH = '/api/integrations', INT_RO_NOTE = 'Only admins can change integrations.';
var INT_PREVIEW_NOTE = 'Preview: this is the page people without the admin role see. Only admins can change integrations.';
function intAdmin() { return typeof ADMIN_OK !== 'undefined' && !!ADMIN_OK; }
function intRO() { return !!INT_SIGNED && (!intAdmin() || INT_PREVIEW); }
function setIntView(signedIn, admin) {
  INT_SIGNED = !!signedIn;
  var row = document.getElementById('tab-integrations');
  if (row) row.hidden = !INT_SIGNED;
  if (typeof renderMeta === 'function') renderMeta();
  if (!INT_SIGNED) {
    INT_WANTED = false;
    INTEG = null;
    if (typeof curTab !== 'undefined' && curTab === 'integrations') showTab('anoms');
    return;
  }
  if (INT_WANTED) { INT_WANTED = false; showTab('integrations'); return; }
  if (typeof curTab !== 'undefined' && curTab === 'integrations') intShow();
}
function intShow() {
  if (!INT_SIGNED) return;
  INT_PREVIEW = false;
  if (typeof ADMIN_OK !== 'undefined' && ADMIN_OK && typeof adminNotices === 'function') adminNotices();
  loadIntegrations();
  if (typeof pqcLoad === 'function') pqcLoad();
}
function intPreviewToggle() {
  var b = accText('button', 'btn sec small int-preview', INT_PREVIEW ? 'Back to editing' : 'View as read-only');
  b.type = 'button';
  b.id = 'int-preview';
  b.setAttribute('aria-pressed', INT_PREVIEW ? 'true' : 'false');
  b.title = INT_PREVIEW ? 'Show the page you can edit again' : 'Shows this page as operators and viewers see it, from the same data they get';
  b.addEventListener('click', function () {
    INT_PREVIEW = !INT_PREVIEW;
    if (typeof intPopClose === 'function') intPopClose(false);
    loadIntegrations();
  });
  return b;
}
function intViewNote(cls) {
  var p = accText('p', 'int-ro-note ' + (cls || ''), INT_PREVIEW && intAdmin() ? INT_PREVIEW_NOTE : INT_RO_NOTE);
  p.setAttribute('role', 'note');
  return p;
}
function intViewValue(f, inst) {
  var held = inst.secrets && inst.secrets[f.key];
  if (held) return {text: held.set ? 'set' : 'not set', cls: held.set ? 'k-act' : 'plain'};
  if (f.kind === 'secret' || f.kind === 'pem') return {text: 'not set', cls: 'plain'};
  var v = inst.values && inst.values[f.key] != null ? String(inst.values[f.key]) : '';
  if (f.kind === 'bool') return {text: v === 'true' ? 'On' : v === 'false' ? 'Off' : (f.default === 'true' ? 'On' : 'Off')};
  if (f.kind === 'choice' && v !== '') return {text: intChoiceText(f, v, '')};
  return v === '' ? {text: f.default ? f.default + ' (default)' : 'not set', muted: true} : {text: v};
}
function intViewHead(p, inst, single) {
  var box = accText('div', 'int-inst int-inst-ro'), line = accText('div', 'int-inst-h');
  box.dataset.name = inst.name;
  if (single) {
    if (inst.problem) box.appendChild(accText('p', 'adm-msg bad int-problem', String(inst.problem)));
    return box;
  }
  line.appendChild(accText('b', 'int-iname', inst.name));
  var ranked = !!((p.checks || []).length || (p.tools || []).length);
  if (inst.primary && ranked && p.id !== INT_MCP) line.appendChild(accText('span', 'tag k-act int-primary', 'primary'));
  if (inst.source) line.appendChild(accText('span', 'tag plain int-src', String(inst.source)));
  line.appendChild(accText('span', 'tag int-ro-on ' + (inst.enabled ? 'k-ok' : 'plain'), inst.enabled ? 'On' : 'Off'));
  box.appendChild(line);
  var h = intInstHealth(p, inst);
  if (h && !h.none) box.appendChild(intHealthEl(h, 'int-ihealth'));
  if (inst.problem) box.appendChild(accText('p', 'adm-msg bad int-problem', String(inst.problem)));
  return box;
}
function intViewBody(slot, p, inst) {
  slot.textContent = '';
  var box = accText('div', 'int-ro');
  if (inst) {
    var dl = accText('dl', 'int-ro-fields');
    intList(p.fields).filter(function (f) { return f && f.key; }).forEach(function (f) {
      var v = intViewValue(f, inst), dd = accText('dd', null);
      dl.appendChild(accText('dt', null, String(f.label || f.key)));
      if (v.cls) dd.appendChild(accText('span', 'tag ' + v.cls, v.text));
      else dd.appendChild(accText('span', v.muted ? 'muted' : 'int-ro-v', v.text));
      dd.dataset.key = f.key;
      dl.appendChild(dd);
    });
    box.appendChild(intViewSection('Settings', dl));
    var checks = intShownChecks(p, inst), off = intList(inst.offChecks);
    if (checks.length) {
      var ul = accText('ul', 'int-ro-checks');
      checks.forEach(function (c) {
        var on = off.indexOf(c.id) < 0, li = accText('li', on ? 'on' : 'off');
        li.dataset.check = c.id;
        li.appendChild(accText('span', 'int-dot ' + (on ? 'ok' : 'off')));
        li.appendChild(accText('span', null, String(c.label || c.id)));
        li.appendChild(accText('span', 'tag ' + (on ? 'k-ok' : 'plain'), on ? 'On' : 'Off'));
        ul.appendChild(li);
      });
      box.appendChild(intViewSection('What it watches', ul));
    }
  }
  var rules = intList(p.rules).filter(function (r) { return r && r.defaults && typeof r.defaults === 'object'; });
  if (rules.length) {
    var wrap = accText('div', 'int-ro-rules');
    rules.forEach(function (r) {
      var t = accText('table', 'adm-rules int-ro-rule'), body = accText('tbody', null), vals = r.values && typeof r.values === 'object' ? r.values : r.defaults;
      t.dataset.rule = String(r.id || '');
      if (rules.length > 1 || r.label) t.appendChild(accText('caption', null, String(r.label || r.id)));
      Object.keys(r.defaults).forEach(function (k) {
        var tr = accText('tr', null), th = accText('th', null), v = vals[k] != null ? vals[k] : r.defaults[k];
        th.setAttribute('scope', 'row');
        th.appendChild(accText('code', null, k));
        tr.appendChild(th);
        tr.appendChild(accText('td', 'int-ro-v', String(v)));
        tr.appendChild(accText('td', 'muted', v !== r.defaults[k] ? 'default ' + r.defaults[k] : ''));
        body.appendChild(tr);
      });
      t.appendChild(body);
      wrap.appendChild(t);
    });
    box.appendChild(intViewSection('Thresholds', wrap));
  }
  slot.appendChild(box);
}
function intViewSection(title, body) {
  var s = accText('section', 'int-ro-sec');
  s.appendChild(accText('h3', 'int-grp-h', title));
  s.appendChild(body);
  return s;
}
function intViewPanel(panel, p, insts, cur, said) {
  panel.appendChild(intViewNote('int-ro-top'));
  if (insts.length) {
    var row = accText('div', 'int-irow int-ilist'), lh = accText('div', 'int-ilist-h'), tabs = accText('div', 'int-itabs');
    lh.appendChild(accText('h3', 'int-ilist-t', 'Instances \u00b7 ' + insts.length));
    row.appendChild(lh);
    tabs.setAttribute('role', 'tablist');
    tabs.setAttribute('aria-label', 'Instances');
    insts.forEach(function (i) { tabs.appendChild(intCard(p, i, cur, said)); });
    row.appendChild(tabs);
    panel.appendChild(row);
  } else if (intList(p.fields).length) {
    panel.appendChild(accText('p', 'adm-help int-nofields', 'No instances yet.'));
  }
  if (cur) panel.appendChild(intViewHead(p, cur, insts.length === 1));
  var slot = accText('div', 'int-slot');
  panel.appendChild(slot);
  intViewBody(slot, p, cur);
}
