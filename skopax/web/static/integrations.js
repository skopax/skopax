var INTEG = null, INTEG_SEQ = 0, INT_CAT = '', INT_Q = '', INT_PANEL = null, INT_POP_OFF = null, INT_SETUP_OPEN = {};
var INT_CATS = {metrics: 'Metrics', logs: 'Logs', alerts: 'Alerts', infrastructure: 'Infrastructure', tickets: 'Tickets', code: 'Code', chat: 'Chat', web: 'Web', apps: 'Apps', marketing: 'Marketing', other: 'Other'};
var INT_DOTS = {ok: 'ok', warn: 'warn', error: 'error', pending: 'pending', none: 'off', off: 'off'};
var INT_MCP = 'mcp-tools', INT_MCP_PRESETS = null, INT_MCP_BACK = null, INT_MCP_SEQ = 0, INT_MCP_FRESH_MS = 600000, INT_MCP_WRITES = {}, INT_MCP_EXPIRED = false, INT_MCP_NOTE = '';
var INT_MCP_KINDS = {read: ['Reads', 'k-good'], unknown: ['May change data', 'k-warn'], write: ['Changes data', 'k-crit']};
var INT_MCP_STATES = {connected: 'Connected', reconnect: 'Needs a new sign-in', none: 'Not connected'};
var INT_SEG_MAX = 3, INT_CUSTOM = '@custom', INT_KNOW = '@knowledge';
function intBase(p) { return '/api/admin/plugins/' + encodeURIComponent(p.id); }
function intStateOf(p) { return !p.state ? 'broken' : p.state === 'ok' ? (p.enabled ? 'on' : 'off') : p.state; }
function intSay(msg, text, bad) {
  if (!msg) return;
  msg.textContent = text || '';
  msg.className = msg.className.replace(/\s*\b(ok|bad)\b/g, '') + (bad ? ' bad' : text ? ' ok' : '');
}
function intPlaceholder(f, inst) {
  var s = inst && inst.secrets && inst.secrets[f.key];
  if (s && s.set) return s.from ? 'set \u00b7 from ' + s.from : 'set';
  return 'not set';
}
function intPeerNames(f) {
  var used = intFind(intList(f.choices)[0]);
  return intList(used && used.instances).map(function (i) { return i && i.name; }).filter(function (n) { return typeof n === 'string' && n !== ''; });
}
function intHuman(v) {
  v = String(v == null ? '' : v).replace(/_/g, ' ');
  return v.charAt(0).toUpperCase() + v.slice(1);
}
function intChoiceText(f, v, blank) {
  var l = f.choiceLabels && typeof f.choiceLabels === 'object' ? f.choiceLabels : {};
  if (v === '') return blank;
  return Object.prototype.hasOwnProperty.call(l, v) && typeof l[v] === 'string' && l[v] ? l[v] : intHuman(v);
}
function intGroupOf(f) { return f.kind === 'pem' ? 'advanced' : typeof f.group === 'string' ? f.group : ''; }
function intInit(f, inst) { return inst && inst.values && inst.values[f.key] != null ? String(inst.values[f.key]) : (f.default || ''); }
function intMoved(f, inst) {
  if (f.kind === 'secret') return !!(inst && inst.secrets && inst.secrets[f.key] && inst.secrets[f.key].set);
  return intInit(f, inst) !== (f.default || '');
}
var INT_SUG_SHOWN = 200;
function intSuggests(f) { return f.suggest === true && (f.kind === 'text' || f.kind === 'url') && (f.separator === '' || f.separator === ',' || f.separator === ';' || f.separator == null); }
function intSugSplit(v, sep) { return String(v || '').split(sep).map(function (x) { return x.trim(); }).filter(Boolean); }
function intPicker(form, p, inst, w) {
  var sep = w.dataset.sep || '', multi = !!sep, input = w.querySelector('.int-in'), box = w.querySelector('.int-sug-in'), pop = w.querySelector('.int-sug-pop');
  var list = w.querySelector('.int-sug-list'), note = w.querySelector('.int-sug-note'), chipsEl = w.querySelector('.int-sug-chips'), tag = w.querySelector('.int-sug-one');
  var st = {state: 'idle', values: [], canon: {}, note: '', seq: 0, active: -1, shown: [], quiet: false, edited: false};
  var chosen = multi ? intSugSplit(input.value, sep) : [];
  var known = function (v) { return st.state === 'ok' && Object.prototype.hasOwnProperty.call(st.canon, foldCase(v)); };
  var canon = function (v) { return known(v) ? st.canon[foldCase(v)] : v; };
  var has = function (v) { var k = foldCase(v); return chosen.some(function (c) { return foldCase(c) === k; }); };
  var store = function () { intPut(input, chosen.join(sep + ' ')); };
  var paintOne = function () {
    if (multi || !tag) return;
    tag.hidden = !(st.state === 'ok' && box.value.trim() && !known(box.value.trim()));
  };
  var paintChips = function () {
    if (!multi) { paintOne(); return; }
    chipsEl.textContent = '';
    chosen.forEach(function (v) {
      var li = accText('li', 'int-sug-chip'), x = accText('button', 'int-sug-x', '\u2715');
      li.appendChild(accText('span', 'int-sug-v', v));
      if (st.state === 'ok' && !known(v)) { li.classList.add('free'); li.appendChild(accText('span', 'int-sug-tag', 'not in the list')); }
      x.type = 'button';
      x.setAttribute('aria-label', 'Remove ' + v);
      x.addEventListener('click', function () { chosen = chosen.filter(function (c) { return c !== v; }); store(); paintChips(); box.focus(); });
      li.appendChild(x);
      chipsEl.appendChild(li);
    });
  };
  var mark = function (i) {
    st.active = i;
    [].forEach.call(list.children, function (o, n) { o.setAttribute('aria-selected', String(n === i)); });
    if (i >= 0 && list.children[i]) { box.setAttribute('aria-activedescendant', list.children[i].id); list.children[i].scrollIntoView({block: 'nearest'}); } else box.removeAttribute('aria-activedescendant');
  };
  var paintList = function () {
    list.textContent = '';
    st.shown = [];
    mark(-1);
    if (st.state === 'loading') { note.textContent = 'Loading\u2026'; return; }
    if (st.state === 'refused') { note.textContent = st.text; return; }
    if (st.state !== 'ok') { note.textContent = ''; return; }
    var q = foldCase((multi || box.dataset.typed === '1') ? box.value.trim() : '');
    var hits = st.values.filter(function (v) { return !has(v) && foldCase(v).indexOf(q) >= 0; });
    st.shown = hits.slice(0, INT_SUG_SHOWN);
    st.shown.forEach(function (v, n) {
      var o = accText('li', 'int-sug-o', v);
      o.id = box.id + '-o' + n;
      o.setAttribute('role', 'option');
      o.setAttribute('aria-selected', 'false');
      o.addEventListener('click', function () { take(v); });
      list.appendChild(o);
    });
    var said = !hits.length ? 'No match' : hits.length > st.shown.length ? 'Showing ' + st.shown.length + ' of ' + hits.length + '; type to narrow the list' : '';
    note.textContent = [said, st.note].filter(Boolean).join('. ');
  };
  var setOpen = function (on) {
    pop.hidden = !on;
    box.setAttribute('aria-expanded', String(on));
    if (!on) mark(-1);
  };
  var load = async function () {
    var seq = ++st.seq;
    st.state = 'loading';
    paintList();
    try {
      var r = await adminApi('POST', intBase(p) + '/fields/' + encodeURIComponent(w.dataset.key) + '/suggestions', {name: inst ? inst.name : undefined, values: intValues(form, 'test')});
      if (seq !== st.seq) return;
      st.values = intList(r && r.values).filter(function (v) { return typeof v === 'string'; });
      st.canon = {};
      st.values.forEach(function (v) { var k = foldCase(v); if (!Object.prototype.hasOwnProperty.call(st.canon, k)) st.canon[k] = v; });
      st.note = r && typeof r.note === 'string' ? r.note : '';
      st.state = 'ok';
    } catch (e) {
      if (seq !== st.seq) return;
      st.state = 'refused';
      st.text = e && e.detail ? String(e.detail) : 'The list could not be loaded';
    }
    paintChips();
    paintList();
  };
  var open = function () {
    setOpen(true);
    if (st.state === 'idle' || st.state === 'refused') load(); else paintList();
  };
  var putOne = function (v) {
    box.value = v;
    st.quiet = true;
    intPut(input, v);
    st.quiet = false;
    st.edited = false;
  };
  var take = function (v) {
    v = canon(String(v).trim());
    if (!v) return;
    if (multi) {
      if (!has(v)) chosen.push(v);
      box.value = '';
      store();
      paintChips();
    } else {
      putOne(v);
      delete box.dataset.typed;
      paintOne();
    }
    paintList();
    if (!multi) setOpen(false);
  };
  var commit = function () {
    if (!multi) {
      if (!st.edited) return;
      var one = box.value.trim();
      if (one && canon(one) !== one) putOne(canon(one));
      return;
    }
    var parts = intSugSplit(box.value, sep).map(canon);
    box.value = '';
    var added = false;
    parts.forEach(function (v) { if (!has(v)) { chosen.push(v); added = true; } });
    if (added) { store(); paintChips(); }
  };
  w._sugFlush = commit;
  pop.addEventListener('mousedown', function (e) { e.preventDefault(); });
  box.addEventListener('focus', open);
  box.addEventListener('blur', function () { delete box.dataset.typed; setOpen(false); });
  box.addEventListener('input', function () {
    if (st.quiet) return;
    st.edited = true;
    if (!multi) { box.dataset.typed = '1'; paintOne(); }
    if (pop.hidden) open(); else paintList();
  });
  box.addEventListener('keydown', function (e) {
    var n = st.shown.length;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (pop.hidden) { open(); return; }
      if (n) mark(e.key === 'ArrowDown' ? (st.active + 1) % n : (st.active - 1 + n) % n);
    } else if (e.key === 'Enter') {
      if (!pop.hidden && st.active >= 0 && st.shown[st.active] != null) { e.preventDefault(); take(st.shown[st.active]); }
      else if (multi) { e.preventDefault(); commit(); paintList(); }
    } else if (e.key === 'Escape') {
      if (pop.hidden) return;
      e.preventDefault();
      e.stopPropagation();
      setOpen(false);
    } else if (e.key === 'Backspace' && multi && box.value === '' && chosen.length) {
      e.preventDefault();
      chosen.pop();
      store();
      paintChips();
      paintList();
    }
  });
  form.addEventListener('input', function (e) { if (!w.contains(e.target) && st.state !== 'loading') st.state = 'idle'; });
  paintChips();
}
function intFlushPickers(form) {
  form.querySelectorAll('.int-field').forEach(function (w) { if (typeof w._sugFlush === 'function') w._sugFlush(); });
}
function intFieldHtml(p, f, inst) {
  var id = 'int-' + p.id + '-' + (inst ? inst.name : 'new') + '-' + f.key;
  var init = intInit(f, inst);
  var plain = f.kind === 'choice' && f.key === 'slack-echo' && intList(f.choices).indexOf('default') >= 0;
  if (plain && init === '') init = 'default';
  var owned = !!(inst && (inst.owned || []).indexOf(f.key) >= 0);
  var show = f.showIf ? ' data-show-key="' + esc(f.showIf[0]) + '" data-show-val="' + esc(f.showIf[1]) + '"' : '';
  var input, tag = 'label', extra = '', side = '';
  if (f.kind === 'secret') {
    var locked = owned, held = inst && inst.secrets && inst.secrets[f.key];
    input = '<div class="int-sec-row"><input id="' + esc(id) + '" class="int-in" type="password" autocomplete="new-password" maxlength="4000" placeholder="' + esc(intPlaceholder(f, inst)) + '"' + (locked ? ' readonly' : '') + '>' +
      (locked ? '' : '<button type="button" class="int-eye" aria-pressed="false" aria-controls="' + esc(id) + '" data-label="' + esc(f.label) + '" aria-label="Show ' + esc(f.label) + '">Show</button>') + '</div>';
    if (held && held.set) {
      var said = held.from ? 'From ' + held.from : 'Set';
      side = '<span class="int-set" data-said="' + esc(said) + '">' + esc(said) + '</span>' + (locked ? '' : '<button type="button" class="btn sec small int-clear">Clear</button>');
    }
  } else if (f.kind === 'pem') {
    input = '<textarea id="' + esc(id) + '" class="int-in int-pem" rows="4" maxlength="20000" spellcheck="false">' + esc(init) + '</textarea>';
  } else if (f.kind === 'bool') {
    extra = ' data-default="' + esc(f.default || '') + '"';
    input = '<input type="hidden" class="int-in" value="' + esc(init) + '"><button type="button" id="' + esc(id) + '" class="int-switch int-tog" role="switch" aria-checked="false" aria-labelledby="' + esc(id) + '-l"></button>';
  } else if (f.kind === 'choice' || f.kind === 'instance') {
    var opts = f.kind === 'instance' ? intPeerNames(f) : intList(f.choices).slice();
    if (f.kind === 'instance' && init !== '' && opts.indexOf(init) < 0) opts.unshift(init);
    var entry = p.id === INT_MCP && f.key === 'preset';
    if (!plain && (init === '' || (f.kind === 'choice' && !f.required && !f.default))) opts.unshift('');
    var text = function (c) { return f.kind === 'instance' ? c : intChoiceText(f, c, entry ? 'No catalog entry' : ''); };
    if (f.kind === 'choice' && !entry && opts.length >= 2 && opts.length <= INT_SEG_MAX) {
      tag = 'span';
      input = '<input type="hidden" class="int-in" value="' + esc(init) + '"><div class="int-seg" id="' + esc(id) + '" role="radiogroup" aria-labelledby="' + esc(id) + '-l">' +
        opts.map(function (c) { return '<button type="button" class="int-seg-b" role="radio" data-value="' + esc(c) + '" aria-checked="false" tabindex="-1">' + esc(intChoiceText(f, c, 'Not set')) + '</button>'; }).join('') + '</div>';
    } else {
      input = '<select id="' + esc(id) + '" class="int-in' + (entry ? '' : ' int-sel') + '"' + (entry ? ' hidden' : '') + '>' + opts.map(function (c) { return '<option value="' + esc(c) + '"' + (c === init ? ' selected' : '') + '>' + esc(text(c)) + '</option>'; }).join('') + '</select>';
      if (entry) input = '<div class="int-entry"><span class="int-entry-chip"><span class="int-entry-n"></span><button type="button" class="int-entry-change">Change\u2026</button>' +
        '<button type="button" class="int-entry-clear" aria-label="No catalog entry" title="No catalog entry">\u2715</button></span>' + input + '</div>';
    }
  } else if (f.kind === 'int') {
    input = '<input id="' + esc(id) + '" class="int-in" type="number" inputmode="numeric" step="1"' + (f.low != null ? ' min="' + esc(f.low) + '"' : '') + (f.high != null ? ' max="' + esc(f.high) + '"' : '') + ' value="' + esc(init) + '">';
  } else if (intSuggests(f) && !owned) {
    extra = ' data-sep="' + esc(f.separator || '') + '"';
    var combo = ' role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="' + esc(id) + '-list" autocomplete="off" spellcheck="false"';
    input = '<div class="int-sug">' + (f.separator ? '<input type="hidden" class="int-in" value="' + esc(init) + '"><ul class="int-sug-chips" aria-label="' + esc(f.label) + '"></ul>' +
      '<input id="' + esc(id) + '" class="int-sug-in" type="text" maxlength="1000"' + combo + '>' : '<input id="' + esc(id) + '" class="int-in int-sug-in" type="text" maxlength="1000" value="' + esc(init) + '"' + combo + '><span class="int-sug-tag int-sug-one" hidden>not in the list</span>') +
      '<div class="int-sug-pop" hidden><ul id="' + esc(id) + '-list" class="int-sug-list" role="listbox" aria-label="' + esc(f.label) + '"></ul><p class="int-sug-note" aria-live="polite"></p></div></div>';
  } else {
    input = '<input id="' + esc(id) + '" class="int-in" type="text" maxlength="1000" value="' + esc(init) + '"' + (owned ? ' readonly' : '') + '>';
  }
  var lab = '<' + tag + (tag === 'label' ? ' for="' + esc(id) + '"' : ' class="int-lab"') + ' id="' + esc(id) + '-l">' + esc(f.label) + (f.required ? '<span class="int-req"> *</span>' : '') + '</' + tag + '>';
  return '<div class="int-field" data-key="' + esc(f.key) + '" data-kind="' + esc(f.kind) + '" data-initial="' + esc(init) + '"' + extra + show + '>' +
    '<div class="int-lrow">' + lab + side + '</div>' + input +
    (f.help ? '<span class="adm-help">' + esc(f.help) + '</span>' : '') +
    (owned && f.kind !== 'secret' ? '<span class="adm-msg">from ' + esc(inst.source) + '</span>' : '') +
    '<span class="adm-msg int-field-msg" role="status"></span></div>';
}
function intList(x) { return Array.isArray(x) ? x : []; }
function intName(p) { return String(p.name || p.id); }
function intUsable(p) { return p.state === 'ok'; }
function intCatOf(p) { return typeof p.category === 'string' && p.category ? p.category : 'other'; }
function intIsCustom(p) { return p.origin === 'package' || p.origin === 'env'; }
function intKnowKey(p) { return typeof p.companyKnowledge === 'string' ? p.companyKnowledge : ''; }
function intKnowing(p) {
  var key = intKnowKey(p), insts = intList(p.instances);
  if (p.id === INT_MCP) return insts.some(function (i) { return !!i && !!i.values && String(i.values.knowledge) === 'true'; });
  if (key === 'always') return intUsable(p);
  return !!key && insts.some(function (i) { return !!i && !!i.values && i.values[key] != null && String(i.values[key]).trim() !== ''; });
}
var INT_KNOW_TIP = 'Knowledge source: the investigator searches and cites it';
function intKnowErr(p) {
  var rows = intList(p.health && p.health.instances);
  return rows.length ? rows.some(function (r) { return !!r && r.state === 'error'; }) : !!p.health && p.health.state === 'error';
}
function intKnowState(p) {
  if (!p) return null;
  var key = intKnowKey(p), on = {name: 'on', text: 'Knowledge', tip: INT_KNOW_TIP};
  if (!key && !intKnowing(p)) return null;
  if (!intUsable(p)) return {name: 'setup', text: 'Knowledge needs setup', tip: 'Knowledge source: the plug-in is not working, so the investigator cannot search it'};
  if (p.enabled !== true) return {name: 'off', text: 'Knowledge off', tip: 'Knowledge source, off while this plug-in is off'};
  if (key && !intKnowing(p)) return {name: 'setup', text: 'Knowledge needs setup', tip: tx('Knowledge source: fill the {0} field of an instance so the investigator can search it').replace('{0}', function () { return tx(key.charAt(0).toUpperCase() + key.slice(1)); })};
  if (intKnowErr(p)) return {name: 'setup', text: 'Knowledge needs setup', tip: 'Knowledge source: fix the problem shown on this tile so the investigator can search it'};
  return on;
}
function intKnowTagOf(st) {
  if (!st) return null;
  var tag = accText('span', 'tag int-know-tag k-' + (st.name === 'on' ? 'info' : st.name === 'off' ? 'muted' : 'warn'), st.text);
  tag.dataset.state = st.name;
  tag.title = st.tip;
  return tag;
}
function intKnowNote(p) {
  var key = intKnowKey(p);
  return key && key !== 'always' ? 'with its ' + key.charAt(0).toUpperCase() + key.slice(1) + ' field' : '';
}
function intCustomSaid(p) {
  var parts = [];
  if (p.author) parts.push(' by ' + p.author);
  if (p.version) parts.push(' version ' + p.version);
  if (p.module) parts.push(' from ' + p.module);
  return 'Custom plug-in' + parts.join(',');
}
function intMetaOf(p) {
  if (!intIsCustom(p)) return 'Built-in' + (p.version ? ' \u00b7 version ' + p.version : '');
  return ['Custom plug-in', p.author ? 'by ' + p.author : '', p.version ? 'version ' + p.version : '', p.licence || ''].filter(Boolean).join(' \u00b7 ');
}
function intCatLabel(c) { return Object.prototype.hasOwnProperty.call(INT_CATS, c) ? INT_CATS[c] : c; }
function intOutside(p) {
  return !!p && intUsable(p) && !intList(p.fields).length && Array.isArray(p.checks) && !p.checks.length && Array.isArray(p.panels) && !p.panels.length;
}
function intTiled(p) {
  if (!intUsable(p)) return p.enabled === true || intList(p.instances).length > 0;
  if (intOutside(p)) return false;
  return intList(p.instances).length > 0 || (!intList(p.fields).length && p.enabled === true);
}
function intConnected(p) {
  return !!p && intUsable(p) && !intOutside(p) && p.enabled === true && (!intList(p.fields).length || intList(p.instances).some(function (i) { return i && i.enabled === true && !i.problem; }));
}
function intAttention(p) { return !intUsable(p) || !!(p.health && (p.health.state === 'error' || p.health.state === 'warn')); }
function intChecks(p) { return intList(p.checks).filter(function (c) { return c && typeof c.id === 'string' && c.id; }); }
function intShownChecks(p, inst) {
  var primary = inst ? !!inst.primary : !intList(p.instances).length;
  return intChecks(p).filter(function (c) { return c.once !== true || primary; });
}
function intStoredOff(p, inst) {
  var shown = intShownChecks(p, inst).map(function (c) { return c.id; });
  var hidden = intChecks(p).filter(function (c) { return shown.indexOf(c.id) < 0; }).map(function (c) { return c.id; });
  return intList(inst.offChecks).filter(function (id) { return hidden.indexOf(id) < 0; });
}
function intCatsOf(list) {
  var seen = list.map(intCatOf);
  return Object.keys(INT_CATS).filter(function (c) { return seen.indexOf(c) >= 0; })
    .concat(seen.filter(function (c, i) { return !Object.prototype.hasOwnProperty.call(INT_CATS, c) && seen.indexOf(c) === i; }));
}
function intSameSet(a, b) {
  var x = a.slice().sort(), y = b.slice().sort();
  return x.length === y.length && x.every(function (v, i) { return v === y[i]; });
}
function intShown(h) {
  if (!h || typeof h.text !== 'string') return null;
  if (h.state === 'none') return {state: 'off', text: '', none: true};
  var when = h.state === 'ok' && typeof h.at === 'string' && h.at ? ago(h.at) : '';
  return {state: INT_DOTS[h.state] || 'off', text: h.text + (when ? ' \u00b7 ' + when : '')};
}
function intHealthOf(p) {
  if (!p.state) return {state: 'error', text: String(p.reason || '')};
  return intShown(p.health);
}
function intInstHealth(p, inst) {
  return intShown(intList(p.health && p.health.instances).filter(function (r) { return r && r.name === inst.name; })[0]);
}
function intHealthEl(h, cls) {
  var line = accText('p', cls);
  line.appendChild(accText('span', 'int-dot ' + h.state));
  line.appendChild(accText('span', 'int-health-t', h.text));
  return line;
}
var INT_DARK_ICONS = ['kafka', 'umami'];
function intIcon(p) {
  var box = accText('span', 'int-ico');
  if (p.icon === true) {
    var img = document.createElement('img');
    img.className = 'ico';
    img.setAttribute('src', '/plugins/' + encodeURIComponent(p.id) + '/icon.svg');
    img.setAttribute('alt', '');
    box.appendChild(img);
    if (INT_DARK_ICONS.indexOf(p.id) >= 0) box.classList.add('int-ico-dark');
  } else {
    box.classList.add('int-mono');
    box.textContent = intName(p).charAt(0).toUpperCase();
  }
  return box;
}
function intCut(s) {
  s = String(s == null ? '' : s);
  return s.length > 60 ? s.slice(0, 59) + '\u2026' : s;
}
function intDropTexts(p) {
  return intList(p.localeDrops).filter(function (d) { return d && typeof d.text === 'string'; }).map(function (d) {
    var keys = intList(d.keys).slice(0, 5).map(intCut);
    return d.text + (keys.length ? ': ' + keys.join(', ') + (d.more > 0 ? ' +' + d.more + ' more' : '') : '');
  });
}
function intDrops(p, into) {
  intDropTexts(p).forEach(function (text) { into.appendChild(accText('p', 'int-drop', text)); });
}
function intPlugins() { return (INTEG && INTEG.plugins) || []; }
function intFind(id) { return intPlugins().filter(function (p) { return p.id === id; })[0]; }
function intTileSwitch(id) {
  var t = [].filter.call(document.querySelectorAll('#integrations .int-tile'), function (x) { return x.dataset.id === id; })[0];
  return t && t.querySelector('.int-switch');
}
function intTileOpen(id) {
  var t = [].filter.call(document.querySelectorAll('#integrations .int-tile'), function (x) { return x.dataset.id === id; })[0];
  return t && t.querySelector('.int-open');
}
function intHay(p, ks) {
  if (ks === undefined) ks = intKnowState(p);
  return foldCase([intName(p), p.id, intCatLabel(intCatOf(p)), typeof p.description === 'string' ? p.description : '', intIsCustom(p) ? 'custom' : '', ks ? 'knowledge' : ''].join(' '));
}
function intMatches(hay, q) {
  return foldCase(String(q || '').trim()).split(/\s+/).filter(Boolean).every(function (w) { return String(hay || '').indexOf(w) >= 0; });
}
function intShort(p, h) {
  var n = intList(p.instances).length;
  if (n < 2) return h.text;
  var bad = intList(p.health && p.health.instances).filter(function (r) { return r && (r.state === 'error' || r.state === 'warn'); }).length;
  return n + ' instances \u00b7 ' + (bad ? bad + (bad === 1 ? ' needs attention' : ' need attention') : h.text);
}
function intWhy(p) {
  var rows = intList(p.health && p.health.instances).filter(function (r) { return r && (r.state === 'error' || r.state === 'warn') && typeof r.text === 'string' && r.text; });
  return rows.length ? rows[0].text : '';
}
function intTip(p, h) {
  return [h ? h.text : '', p.state && !intUsable(p) && p.reason ? String(p.reason) : ''].concat(intDropTexts(p)).filter(Boolean).join('\n');
}
function intTile(p) {
  var st = intStateOf(p), usable = intUsable(p), on = p.enabled === true, need = intAttention(p), h = intHealthOf(p);
  var t = accText('article', 'int-tile' + (st === 'on' ? '' : ' off') + (need ? ' need' : ''));
  t.dataset.id = p.id;
  t.dataset.state = st;
  t.dataset.cat = intCatOf(p);
  t.dataset.origin = p.origin || 'builtin';
  var ks = intKnowState(p);
  t.dataset.knowledge = ks ? '1' : '0';
  t.dataset.find = intHay(p, ks);
  var tip = intTip(p, h);
  if (tip) t.title = tip;
  var top = accText('div', 'int-tile-top');
  top.appendChild(intIcon(p));
  var open = accText('button', 'int-open'), row = accText('span', 'int-row1'), dot = accText('span', 'int-st'), line = accText('span', 'int-line');
  open.type = 'button';
  row.appendChild(accText('b', 'int-name', intName(p)));
  dot.appendChild(accText('span', 'int-dot ' + (h ? h.state : 'off')));
  dot.appendChild(accText('span', 'sr', need ? 'Needs attention' : st === 'on' ? 'On' : 'Off'));
  row.appendChild(dot);
  var kt = intKnowTagOf(ks);
  if (kt) {
    row.classList.add('know');
    row.appendChild(kt);
  }
  open.appendChild(row);
  var custom = intIsCustom(p);
  if (!need || custom) line.appendChild(accText('span', 'int-cat', need ? 'Custom' : intCatLabel(intCatOf(p)) + (custom ? ' \u00b7 Custom' : '')));
  var healthText = h ? (h.none ? (intList(p.instances).length > 1 ? intList(p.instances).length + ' instances' : '') : intShort(p, h)) : '';
  if (healthText) line.appendChild(accText('span', 'int-health', healthText));
  var why = need && h && intList(p.instances).length > 1 ? intWhy(p) : '';
  if (why) line.appendChild(accText('span', 'int-why', why));
  open.appendChild(line);
  if (custom) {
    var said = intCustomSaid(p), note = accText('span', 'int-custom-d', said);
    note.id = 'int-custom-' + p.id;
    note.hidden = true;
    open.title = said;
    open.setAttribute('aria-describedby', note.id);
    open.appendChild(note);
  }
  top.appendChild(open);
  var sw = accText('button', 'int-switch');
  sw.type = 'button';
  sw.setAttribute('role', 'switch');
  sw.setAttribute('aria-checked', on ? 'true' : 'false');
  sw.setAttribute('aria-label', 'Switch ' + intName(p));
  if (!usable) {
    sw.title = String(p.reason || '');
    if (!on) sw.setAttribute('aria-disabled', 'true');
  }
  if (intRO()) {
    sw.setAttribute('aria-disabled', 'true');
    sw.title = INT_RO_NOTE;
  }
  top.appendChild(sw);
  t.appendChild(top);
  var msg = accText('span', 'adm-msg int-msg');
  msg.setAttribute('role', 'status');
  t.appendChild(msg);
  sw.addEventListener('click', function (e) { e.stopPropagation(); intSwitch(p, sw, msg); });
  t.addEventListener('click', function (e) { if (!e.target.closest('.int-switch')) intOpen(p.id, undefined, {tile: p.id}); });
  return t;
}
async function intSwitch(p, sw, msg) {
  if (sw.getAttribute('aria-disabled') === 'true' || sw.getAttribute('aria-busy') === 'true') return;
  var want = sw.getAttribute('aria-checked') !== 'true', held = document.activeElement === sw;
  if (want && !intUsable(p)) return;
  sw.setAttribute('aria-checked', String(want));
  sw.setAttribute('aria-busy', 'true');
  intSay(msg, 'Saving\u2026', false);
  try {
    await adminApi('PUT', intBase(p), {enabled: want});
    await loadIntegrations(intName(p) + (want ? ' switched on' : ' switched off'));
    var again = held && intTileSwitch(p.id);
    if (again && (document.activeElement === document.body || !document.activeElement)) again.focus();
  } catch (e) {
    sw.setAttribute('aria-checked', String(!want));
    sw.removeAttribute('aria-busy');
    intSay(msg, e && e.detail ? e.detail : 'Not saved, try again', true);
  }
}
function intFilter() {
  var seen = 0, all = document.querySelectorAll('#integrations .int-tile'), none = document.querySelector('#integrations .int-none'), q = INT_Q.trim();
  all.forEach(function (t) {
    t.hidden = (INT_CAT === INT_KNOW ? t.dataset.knowledge !== '1' : INT_CAT === INT_CUSTOM ? t.dataset.origin === 'builtin' : !!INT_CAT && t.dataset.cat !== INT_CAT) || !intMatches(t.dataset.find, q);
    if (!t.hidden) seen++;
  });
  document.querySelectorAll('#integrations .int-group').forEach(function (g) { g.hidden = ![].some.call(g.querySelectorAll('.int-tile'), function (t) { return !t.hidden; }); });
  document.querySelectorAll('#integrations .int-chip').forEach(function (c) { c.setAttribute('aria-pressed', String(c.dataset.cat === INT_CAT)); });
  if (!none) return;
  none.hidden = !(q && all.length && !seen);
  none.querySelector('.int-none-t').textContent = 'No integration matches "' + q + '"';
}
function intFindBox() {
  var w = accText('div', 'int-find-w'), f = document.createElement('input'), k = accText('kbd', 'int-kbd', '/');
  f.type = 'search';
  f.id = 'int-find';
  f.className = 'int-find';
  f.placeholder = 'Search integrations';
  f.value = INT_Q;
  f.setAttribute('aria-label', 'Search integrations');
  f.setAttribute('aria-keyshortcuts', '/');
  f.setAttribute('autocomplete', 'off');
  f.setAttribute('spellcheck', 'false');
  k.setAttribute('aria-hidden', 'true');
  f.addEventListener('input', function () { INT_Q = f.value; intFilter(); });
  f.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && f.value) {
      e.preventDefault();
      e.stopPropagation();
      f.value = '';
      INT_Q = '';
      intFilter();
    } else if (e.key === 'Enter') {
      var seen = [].filter.call(document.querySelectorAll('#integrations .int-tile'), function (t) { return !t.hidden; });
      e.preventDefault();
      if (seen.length === 1) intOpen(seen[0].dataset.id, undefined, {tile: seen[0].dataset.id});
    }
  });
  w.appendChild(f);
  w.appendChild(k);
  return w;
}
function intNoneBox() {
  var none = accText('div', 'int-none'), clear = accText('button', 'btn sec small int-none-clear', 'Clear');
  none.setAttribute('role', 'status');
  none.hidden = true;
  none.appendChild(accText('p', 'muted int-none-t'));
  clear.type = 'button';
  clear.addEventListener('click', function () {
    var f = document.getElementById('int-find');
    INT_Q = '';
    if (f) { f.value = ''; f.focus(); }
    intFilter();
  });
  none.appendChild(clear);
  return none;
}
function intSlash(e) {
  if (e.key !== '/' || e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented || e.isComposing) return;
  var f = document.getElementById('int-find'), sec = document.getElementById('panel-integrations'), pop = document.getElementById('int-pop'), cf = document.getElementById('cf-wrap'), at = document.activeElement;
  if (!f || !sec || sec.hidden || INT_PANEL || (pop && !pop.hidden) || (cf && !cf.hidden)) return;
  if (at && (at.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(at.tagName))) return;
  e.preventDefault();
  f.focus();
  f.select();
}
document.addEventListener('keydown', intSlash);
function renderIntegrations(note) {
  var box = document.getElementById('integrations');
  if (!box || !INTEG) return;
  INTEG_SEQ++;
  intPopClose(false);
  var was = document.activeElement, held = !!was && was.id === 'int-find', caret = held ? [was.selectionStart, was.selectionEnd] : null;
  box.textContent = '';
  var shown = intPlugins().filter(intTiled), cats = intCatsOf(shown), need = shown.filter(intAttention).length, live = intPlugins().filter(intConnected).length;
  var anyCustom = shown.some(intIsCustom), anyKnow = shown.some(intKnowState);
  if (INT_CAT && (INT_CAT === INT_CUSTOM ? !anyCustom : INT_CAT === INT_KNOW ? !anyKnow : cats.indexOf(INT_CAT) < 0)) INT_CAT = '';
  var top = accText('div', 'int-top'), head = accText('div', 'int-h');
  head.appendChild(accText('h3', 'int-title', 'Integrations'));
  if (shown.length) head.appendChild(intFindBox());
  var picker = accText('div', 'int-picker'), add = accText('button', 'btn small int-add-btn', '+ Add integration'), pop = accText('div', 'int-pop');
  add.type = 'button';
  add.id = 'int-add-btn';
  add.setAttribute('aria-haspopup', 'dialog');
  add.setAttribute('aria-expanded', 'false');
  add.setAttribute('aria-controls', 'int-pop');
  pop.id = 'int-pop';
  pop.setAttribute('role', 'dialog');
  pop.setAttribute('aria-label', 'Add integration');
  pop.hidden = true;
  picker.appendChild(add);
  picker.appendChild(pop);
  if (!intRO()) {
    head.appendChild(picker);
    var mcpTool = intFind(INT_MCP);
    if (mcpTool && intUsable(mcpTool)) {
      var addMcp = accText('button', 'btn small int-add-mcp', '+ Add custom MCP');
      addMcp.type = 'button';
      addMcp.id = 'int-add-mcp';
      addMcp.addEventListener('click', function () { intPopClose(false); intOpen(INT_MCP, null, 'add'); });
      head.appendChild(addMcp);
    }
  }
  top.appendChild(head);
  if (intRO()) top.appendChild(intViewNote('int-ro-list'));
  top.appendChild(accText('p', 'muted int-sub', live + ' connected \u00b7 ' + need + (need === 1 ? ' needs attention' : ' need attention')));
  if (shown.length) {
    var chips = accText('div', 'int-chips');
    chips.setAttribute('role', 'group');
    chips.setAttribute('aria-label', 'Categories');
    [''].concat(anyKnow ? [INT_KNOW] : [], cats, anyCustom ? [INT_CUSTOM] : []).forEach(function (c) {
      var b = accText('button', 'int-chip', c === INT_KNOW ? 'Knowledge' : c === INT_CUSTOM ? 'Custom' : c ? intCatLabel(c) : 'All');
      b.type = 'button';
      b.dataset.cat = c;
      b.addEventListener('click', function () { INT_CAT = c; intFilter(); });
      chips.appendChild(b);
    });
    top.appendChild(chips);
  }
  box.appendChild(top);
  var said = accText('p', 'adm-msg ok int-note', note || '');
  said.setAttribute('role', 'status');
  box.appendChild(said);
  if (anyCustom) {
    [['custom', 'Custom plug-ins', shown.filter(intIsCustom)], ['builtin', 'Built-in plug-ins', shown.filter(function (p) { return !intIsCustom(p); })]].forEach(function (g) {
      if (!g[2].length) return;
      var sec = accText('section', 'int-group'), h = accText('h4', 'int-group-h', g[1] + ' '), grid = accText('div', 'int-grid');
      sec.dataset.group = g[0];
      h.id = 'int-group-' + g[0];
      h.appendChild(accText('span', 'int-group-n', String(g[2].length)));
      sec.setAttribute('aria-labelledby', h.id);
      sec.appendChild(h);
      g[2].forEach(function (p) { grid.appendChild(intTile(p)); });
      sec.appendChild(grid);
      box.appendChild(sec);
    });
  } else {
    var grid = accText('div', 'int-grid');
    shown.forEach(function (p) { grid.appendChild(intTile(p)); });
    box.appendChild(grid);
  }
  if (shown.length) box.appendChild(intNoneBox());
  if (!shown.length) box.appendChild(accText('p', 'muted int-empty', intRO() ? 'Nothing is connected yet.' : 'Nothing is connected yet: + Add integration lists what can be set up.'));
  intFilter();
  var find = held && document.getElementById('int-find');
  if (find) {
    find.focus();
    try { find.setSelectionRange(caret[0], caret[1]); } catch (e) {}
  }
  add.addEventListener('click', function () { if (pop.hidden) intPopOpen(); else intPopClose(true); });
  pop.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      intPopClose(true);
      return;
    }
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    var list = [pop.querySelector('.int-search')].concat(intPopSeen(pop)), at = list.indexOf(document.activeElement);
    if (at < 0) return;
    e.preventDefault();
    list[Math.max(0, Math.min(list.length - 1, at + (e.key === 'ArrowDown' ? 1 : -1)))].focus();
  });
  markIntegCount(INTEG);
}
async function loadIntegrations(note) {
  var seq = ++INTEG_SEQ, box = document.getElementById('integrations');
  try {
    var ro = intRO(), r = await adminApi('GET', ro ? INT_RO_PATH : '/api/admin/plugins');
    if (seq !== INTEG_SEQ) return false;
    INTEG = r;
    renderIntegrations(note);
    if (!ro) intMcpBack();
    return true;
  } catch (e) {
    if (seq !== INTEG_SEQ) return false;
    box.innerHTML = '<p class="muted">' + (e && (e.code === 401 || e.code === 403) && !intRO() ? 'Admins only: log in with the ' + esc(ADMIN_ROLE) + ' role.' : 'Integrations did not load; open the tab again.') + '</p>';
    return false;
  }
}
function intPickBody(b, icon, name, desc, note, tags, status) {
  var words = accText('span', 'int-pick-t'), top = accText('span', 'int-pick-top');
  b.appendChild(icon);
  top.appendChild(accText('span', 'int-pick-n', name));
  tags.filter(Boolean).forEach(function (t) { top.appendChild(t); });
  words.appendChild(top);
  if (desc) words.appendChild(accText('span', 'int-pick-d', desc));
  if (note) words.appendChild(accText('span', 'int-pick-k', note));
  b.appendChild(words);
  if (status) b.appendChild(status);
}
function intPick(p, note) {
  var b = accText('button', 'int-pick'), connected = intConnected(p);
  b.type = 'button';
  b.dataset.id = p.id;
  b.dataset.origin = p.origin || 'builtin';
  b.dataset.find = intHay(p);
  var n = intList(p.instances).length, tags = [intKnowKey(p) ? intKnowTagOf({name: 'on', text: 'Knowledge', tip: INT_KNOW_TIP}) : null, intIsCustom(p) ? accText('span', 'muted int-pick-c', 'Custom') : null,
    p.state === 'locked' ? accText('span', 'tag k-act int-ed', String(p.edition || '').charAt(0).toUpperCase() + String(p.edition || '').slice(1)) : null];
  var status = intOutside(p) ? accText('span', 'int-has idle', 'Set up outside Admin') : connected || n ? accText('span', 'int-has' + (connected ? '' : ' idle'), (connected ? '\u2713 ' : '') + (n > 1 ? n + ' added' : 'Added')) : null;
  intPickBody(b, intIcon(p), intName(p), typeof p.description === 'string' ? p.description : '', note, tags, status);
  if (connected) {
    b.classList.add('has');
    b.title = 'Connected: opens its setup';
  }
  if (!intUsable(p)) {
    b.setAttribute('aria-disabled', 'true');
    b.title = String(p.reason || '');
  }
  var go = function () {
    if (b.getAttribute('aria-disabled') === 'true') return;
    intPopClose(false);
    intOpen(p.id, intList(p.instances).length ? undefined : null, 'add');
  };
  b.addEventListener('click', go);
  b.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); go(); } });
  return b;
}
function intPopSeen(pop) {
  return [].filter.call(pop.querySelectorAll('.int-pick'), function (b) { return !b.hidden && !b.closest('[hidden]'); });
}
function intPopFind(pop, q) {
  var words = foldCase(q.trim()).split(/\s+/).filter(Boolean), any = false;
  pop.querySelectorAll('.int-cat-grp').forEach(function (g) {
    var n = 0;
    g.querySelectorAll('.int-pick').forEach(function (b) {
      var hit = words.every(function (w) { return b.dataset.find.indexOf(w) >= 0; });
      b.hidden = !hit;
      if (hit) n++;
    });
    g.hidden = !n;
    if (n) any = true;
  });
  pop.querySelector('.int-pop-none').hidden = any;
}
function intPopOpen() {
  var pop = document.getElementById('int-pop'), add = document.getElementById('int-add-btn');
  if (!pop || !add || !INTEG) return;
  var list = intPlugins().filter(function (p) { return p.state && p.state !== 'broken' && p.id !== INT_MCP; }), mcp = intFind(INT_MCP);
  pop.textContent = '';
  var search = document.createElement('input');
  search.type = 'search';
  search.className = 'int-search';
  search.placeholder = 'Search integrations';
  search.setAttribute('aria-label', 'Search integrations');
  search.setAttribute('autocomplete', 'off');
  search.setAttribute('spellcheck', 'false');
  var top = accText('div', 'int-pop-top'), shut = accText('button', 'btn sec small int-pop-x', '\u2715');
  shut.type = 'button';
  shut.title = 'Close';
  shut.setAttribute('aria-label', 'Close');
  shut.addEventListener('click', function () { intPopClose(true); });
  top.appendChild(search);
  top.appendChild(shut);
  pop.appendChild(top);
  var body = accText('div', 'int-pop-body'), rest = list;
  intCatsOf(rest).forEach(function (c) {
    var g = accText('section', 'int-cat-grp'), grid = accText('div', 'int-pg');
    g.dataset.cat = c;
    g.appendChild(accText('h4', 'int-cat-h', intCatLabel(c)));
    rest.filter(function (p) { return intCatOf(p) === c; }).forEach(function (p) { grid.appendChild(intPick(p, intKnowNote(p))); });
    g.appendChild(grid);
    body.appendChild(g);
  });
  if (mcp && mcp.state && mcp.state !== 'broken') body.appendChild(intMcpGroup(pop, mcp));
  var none = accText('p', 'muted int-pop-none', 'No integration matches.');
  none.hidden = true;
  body.appendChild(none);
  pop.appendChild(body);
  pop.hidden = false;
  add.setAttribute('aria-expanded', 'true');
  search.addEventListener('input', function () { intPopFind(pop, search.value); });
  search.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter') return;
    var seen = intPopSeen(pop);
    e.preventDefault();
    if (seen.length === 1) seen[0].click();
  });
  search.focus();
  INT_POP_OFF = function (e) { if (!pop.contains(e.target) && !add.contains(e.target)) intPopClose(false); };
  document.addEventListener('click', INT_POP_OFF);
}
function intPopClose(back) {
  if (INT_POP_OFF) { document.removeEventListener('click', INT_POP_OFF); INT_POP_OFF = null; }
  var pop = document.getElementById('int-pop'), add = document.getElementById('int-add-btn');
  if (pop) { pop.hidden = true; pop.textContent = ''; }
  if (!add) return;
  add.setAttribute('aria-expanded', 'false');
  if (back) add.focus();
}
function intWrap() { return document.getElementById('int-wrap'); }
function intPanelEl() { return document.getElementById('int-panel'); }
function intFocusables(panel) {
  return [].filter.call(panel.querySelectorAll('button, input, select, textarea, summary, a[href], [tabindex]:not([tabindex="-1"])'), function (x) {
    if (x.disabled || x.closest('[hidden]') || x.type === 'hidden' || x.getAttribute('tabindex') === '-1') return false;
    var fold = x.closest('details');
    return !fold || fold.open || (x.tagName === 'SUMMARY' && x.parentElement === fold);
  });
}
function intFocusFirst(sel) {
  var panel = intPanelEl();
  if (!panel) return;
  var list = intFocusables(panel), to = (sel && list.filter(function (x) { return x.matches(sel); })[0]) || list.filter(function (x) { return x.matches('.int-name-in, .int-in, .int-tog, .int-seg-b'); })[0] || list[0];
  if (to) to.focus();
}
function intOpen(id, name, from) {
  var w = intWrap();
  if (!w) return;
  INT_PANEL = {id: id, name: name, from: from};
  if (!intPanelPaint()) return;
  w.hidden = false;
  document.addEventListener('keydown', intKeys);
  intFocusFirst();
}
function intKeys(e) {
  var panel = intPanelEl(), cf = document.getElementById('cf-wrap');
  if (!INT_PANEL || !panel || (cf && !cf.hidden)) return;
  if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); intClose(true); return; }
  if (e.key !== 'Tab') return;
  var list = intFocusables(panel), at = document.activeElement, inside = panel.contains(at) && at !== panel;
  if (!list.length) { e.preventDefault(); panel.focus(); return; }
  if (e.shiftKey && (at === list[0] || !inside)) { e.preventDefault(); list[list.length - 1].focus(); }
  else if (!e.shiftKey && (at === list[list.length - 1] || !inside)) { e.preventDefault(); list[0].focus(); }
}
function intHold(el) {
  var panel = intPanelEl();
  if (panel && el && el === document.activeElement && panel.contains(el) && (el.tagName === 'BUTTON' || el.type === 'checkbox')) panel.focus();
}
function intGiveBack(el) {
  var panel = intPanelEl(), at = document.activeElement;
  if (!INT_PANEL || !panel || !el || !el.isConnected || el.disabled || !panel.contains(el)) return;
  if (!at || at === document.body || at === panel || !panel.contains(at)) el.focus();
}
function intClose(back) {
  var s = INT_PANEL, w = intWrap(), panel = intPanelEl();
  INT_PANEL = null;
  document.removeEventListener('keydown', intKeys);
  if (w) w.hidden = true;
  if (panel) panel.textContent = '';
  if (!back || !s) return;
  var to = (s.from && s.from.tile && intTileOpen(s.from.tile)) || document.getElementById('int-add-btn');
  if (to) to.focus();
}
function intLeave() {
  intClose(false);
  intPopClose(false);
}
function intSetup(p, panel, open) {
  var setup = p.setup, steps = setup && Array.isArray(setup.steps) ? setup.steps : [];
  if (!steps.length) return;
  var box = accText('details', 'int-setup'), list = accText('ol', 'int-setup-steps');
  var kept = INT_SETUP_OPEN[p.id];
  box.open = typeof kept === 'boolean' ? kept : open;
  var sum = accText('summary', null, 'How to set it up');
  sum.addEventListener('click', function () { INT_SETUP_OPEN[p.id] = !box.open; });
  box.appendChild(sum);
  steps.forEach(function (step) { list.appendChild(accText('li', null, String(step))); });
  box.appendChild(list);
  var docs = typeof setup.docs === 'string' ? setup.docs : '';
  if (/^https:\/\/\S+$/.test(docs)) {
    var link = accText('a', 'int-setup-link', typeof setup.docsLabel === 'string' && setup.docsLabel.trim() ? setup.docsLabel.trim() : 'Setup guide');
    link.href = docs;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    box.appendChild(link);
  }
  panel.appendChild(box);
}
function intPanelPaint(note) {
  var s = INT_PANEL, panel = intPanelEl(), p = s && intFind(s.id);
  if (!p || !panel) { intClose(false); return false; }
  var insts = intList(p.instances), usable = intUsable(p), fields = intList(p.fields), cur = null;
  if (s.name !== null) cur = insts.filter(function (i) { return i.name === s.name; })[0] || insts[0] || null;
  s.name = cur ? cur.name : null;
  panel.textContent = '';
  var head = accText('div', 'int-ph'), title = accText('h2', null, intName(p)), close = accText('button', 'btn sec small int-close', '\u2715');
  title.id = 'int-panel-h';
  close.type = 'button';
  close.title = 'Close';
  close.setAttribute('aria-label', 'Close');
  close.addEventListener('click', function () { intClose(true); });
  var names = accText('div', 'int-ph-t');
  names.appendChild(title);
  if (usable && fields.length && !cur && !intRO()) names.appendChild(accText('span', 'int-ph-sub', 'Add instance'));
  head.appendChild(intIcon(p));
  head.appendChild(names);
  head.appendChild(close);
  panel.appendChild(head);
  if (p.description) panel.appendChild(accText('p', 'adm-help int-desc', String(p.description)));
  panel.appendChild(accText('p', 'muted int-meta', intMetaOf(p)));
  intSetup(p, panel, !insts.length);
  if (!usable && p.reason) panel.appendChild(accText('p', 'adm-msg bad int-reason', String(p.reason)));
  intDrops(p, panel);
  var said = accText('p', 'adm-msg ok int-panel-note', note || '');
  said.setAttribute('role', 'status');
  panel.appendChild(said);
  if (!usable) return true;
  if (intRO()) { intViewPanel(panel, p, insts, cur, said); return true; }
  if (!fields.length) {
    panel.appendChild(accText('p', 'adm-help int-nofields', 'Nothing to set up: this plug-in needs no instance.'));
    if (p.enabled !== true) panel.appendChild(intTurnOn(p));
    return true;
  }
  var row = accText('div', 'int-irow int-ilist'), lh = accText('div', 'int-ilist-h'), add = accText('button', 'btn sec small int-add'), plus = accText('span', 'int-add-i', '+');
  lh.appendChild(accText('h3', 'int-ilist-t', 'Instances' + (insts.length ? ' \u00b7 ' + insts.length : '')));
  row.appendChild(lh);
  add.type = 'button';
  plus.setAttribute('aria-hidden', 'true');
  add.appendChild(plus);
  add.appendChild(document.createTextNode(' Add instance'));
  add.setAttribute('aria-pressed', String(!cur));
  add.addEventListener('click', function () { INT_PANEL.name = null; intPanelPaint(); intFocusFirst(); });
  if (insts.length) {
    lh.appendChild(add);
    var tabs = accText('div', 'int-itabs');
    tabs.setAttribute('role', 'tablist');
    tabs.setAttribute('aria-label', 'Instances');
    insts.forEach(function (i) { tabs.appendChild(intCard(p, i, cur, said)); });
    row.appendChild(tabs);
  } else {
    var empty = accText('div', 'int-iempty');
    empty.appendChild(accText('b', 'int-iempty-t', 'No instances yet'));
    empty.appendChild(accText('p', 'adm-help int-iempty-d', 'An instance is one connection to ' + intName(p) + ', with its own address and sign-in. Add one for each server or account.'));
    empty.appendChild(add);
    row.appendChild(empty);
  }
  panel.appendChild(row);
  if (cur) panel.appendChild(intInstHead(p, cur));
  var slot = accText('div', 'int-slot');
  panel.appendChild(slot);
  intForm(slot, p, cur);
  if (p.id === INT_MCP && cur) intMcpMount(panel, p, cur);
  return true;
}
function intCard(p, i, cur, said) {
  var sel = !!cur && cur.name === i.name, ranked = !!((p.checks || []).length || (p.tools || []).length), h = intInstHealth(p, i);
  var c = accText('div', 'int-icard' + (sel ? ' sel' : '') + (i.enabled ? '' : ' off')), b = accText('button', 'int-itab', i.name), meta = accText('span', 'int-icard-m'), line = accText('span', 'int-icard-h'), edit = accText('span', 'int-icard-e', intRO() ? (sel ? 'Viewing' : 'View') : sel ? 'Editing' : 'Edit');
  b.type = 'button';
  b.dataset.name = i.name;
  b.setAttribute('role', 'tab');
  b.setAttribute('aria-selected', String(sel));
  b.addEventListener('click', function () {
    if (INT_PANEL && INT_PANEL.name === i.name) return;
    var mcp = intMcpBoxOf(p);
    if (mcp && mcp.dataset.busy === '1') { intSay(said, 'Wait for the last change to finish', true); return; }
    INT_PANEL.name = i.name;
    intPanelPaint();
    intFocusFirst('.int-itab[aria-selected="true"]');
  });
  c.appendChild(b);
  if (i.primary && ranked && p.id !== INT_MCP) meta.appendChild(accText('span', 'tag k-act int-icard-p', 'primary'));
  meta.appendChild(accText('span', 'int-icard-s', i.enabled ? 'On' : 'Off'));
  c.appendChild(meta);
  line.appendChild(accText('span', 'int-dot ' + (h ? h.state : 'off')));
  if (!(h && h.none)) line.appendChild(accText('span', 'int-icard-l', h ? h.text : 'Not checked yet'));
  c.appendChild(line);
  edit.setAttribute('aria-hidden', 'true');
  c.appendChild(edit);
  return c;
}
async function intRepaint(note, sel) {
  await loadIntegrations();
  if (!INT_PANEL || !intPanelPaint(note)) return;
  intFocusFirst(sel);
}
function intTurnOn(p) {
  var acts = accText('div', 'int-acts'), b = accText('button', 'btn small int-turn-on', 'Turn on'), msg = accText('span', 'adm-msg int-form-msg');
  b.type = 'button';
  msg.setAttribute('role', 'status');
  acts.appendChild(b);
  acts.appendChild(msg);
  b.addEventListener('click', async function () {
    intHold(b);
    b.disabled = true;
    intSay(msg, 'Saving\u2026', false);
    try {
      await adminApi('PUT', intBase(p), {enabled: true});
      await loadIntegrations(intName(p) + ' switched on');
      intClose(true);
    } catch (e) {
      b.disabled = false;
      intSay(msg, e && e.detail ? e.detail : 'Not saved, try again', true);
      intGiveBack(b);
    }
  });
  return acts;
}
function intInstHead(p, inst) {
  var box = accText('div', 'int-inst'), line = accText('div', 'int-inst-h');
  box.dataset.name = inst.name;
  line.appendChild(accText('b', 'int-iname', inst.name));
  var ranked = !!((p.checks || []).length || (p.tools || []).length);
  if (inst.primary && ranked && p.id !== INT_MCP) line.appendChild(accText('span', 'tag k-act int-primary', 'primary'));
  line.appendChild(accText('span', 'tag plain int-src', String(inst.source || '')));
  var label = accText('label', 'int-on-l'), on = document.createElement('input');
  on.type = 'checkbox';
  on.className = 'int-on';
  on.checked = !!inst.enabled;
  on.setAttribute('aria-label', 'Enabled: ' + inst.name);
  if (p.id === INT_MCP && !inst.enabled) {
    on.disabled = true;
    on.setAttribute('aria-describedby', 'int-mcp-warn-h');
  }
  label.appendChild(on);
  label.appendChild(document.createTextNode('On'));
  line.appendChild(label);
  var mk = null;
  if (!inst.primary && inst.enabled && ranked && p.id !== INT_MCP) {
    mk = accText('button', 'btn sec small int-primary-btn', 'Make primary');
    mk.type = 'button';
    line.appendChild(mk);
  }
  var rmsg = accText('span', 'adm-msg int-row-msg');
  rmsg.setAttribute('role', 'status');
  line.appendChild(rmsg);
  box.appendChild(line);
  var h = intInstHealth(p, inst);
  if (h && !h.none) box.appendChild(intHealthEl(h, 'int-ihealth'));
  if (inst.problem) box.appendChild(accText('p', 'adm-msg bad int-problem', String(inst.problem)));
  on.addEventListener('change', async function () {
    var want = on.checked, mcp = intMcpBoxOf(p);
    if (mcp && !intMcpClaim(mcp, rmsg)) { on.checked = !want; return; }
    intHold(on);
    on.disabled = true;
    intSay(rmsg, 'Saving\u2026', false);
    try {
      await adminApi('PATCH', intBase(p) + '/instances/' + encodeURIComponent(inst.name), {enabled: want});
      await intRepaint(inst.name + (want ? ' switched on' : ' switched off'), '.int-on');
    } catch (e) {
      on.checked = !want;
      on.disabled = false;
      intSay(rmsg, e && e.detail ? e.detail : 'Not saved, try again', true);
      intGiveBack(on);
    } finally {
      if (mcp) delete mcp.dataset.busy;
    }
  });
  if (mk) mk.addEventListener('click', async function () {
    var mcp = intMcpBoxOf(p);
    if (mcp && !intMcpClaim(mcp, rmsg)) return;
    intHold(mk);
    mk.disabled = true;
    intSay(rmsg, 'Saving\u2026', false);
    try {
      var r = await adminApi('PATCH', intBase(p) + '/instances/' + encodeURIComponent(inst.name), {primary: true});
      await intRepaint((r && r.notice) || inst.name + ' is now the primary instance', '.int-on');
    } catch (e) {
      mk.disabled = false;
      intSay(rmsg, e && e.detail ? e.detail : 'Not saved, try again', true);
      intGiveBack(mk);
    } finally {
      if (mcp) delete mcp.dataset.busy;
    }
  });
  return box;
}
function intFieldOf(form, key) {
  return [].filter.call(form.querySelectorAll('.int-field'), function (w) { return w.dataset.key === key; })[0];
}
function intSync(form) {
  form.querySelectorAll('.int-field').forEach(function (w) {
    var input = w.querySelector('.int-in'), sw = w.querySelector('.int-tog'), seg = w.querySelector('.int-seg'), entry = w.querySelector('.int-entry');
    if (!input) return;
    if (sw) {
      sw.setAttribute('aria-checked', String((input.value === '' ? w.dataset.default || '' : input.value) === 'true'));
      if (input.disabled) sw.setAttribute('aria-disabled', 'true'); else sw.removeAttribute('aria-disabled');
    }
    if (seg) {
      var bs = [].slice.call(seg.querySelectorAll('.int-seg-b')), hit = bs.filter(function (b) { return b.dataset.value === input.value; })[0];
      bs.forEach(function (b) {
        b.setAttribute('aria-checked', String(b === hit));
        b.tabIndex = b === (hit || bs[0]) ? 0 : -1;
      });
      if (input.disabled) seg.setAttribute('aria-disabled', 'true'); else seg.removeAttribute('aria-disabled');
    }
    if (entry) {
      var chip = entry.querySelector('.int-entry-chip'), picking = w.dataset.pick === '1', f = {choiceLabels: {}};
      [].forEach.call(input.options, function (o) { f.choiceLabels[o.value] = o.textContent; });
      chip.hidden = picking;
      input.hidden = !picking;
      chip.classList.toggle('none', input.value === '');
      chip.querySelector('.int-entry-n').textContent = input.value === '' ? 'No catalog entry' : intChoiceText(f, input.value, '');
      chip.querySelector('.int-entry-clear').hidden = input.value === '';
    }
  });
}
function intShowIf(form) {
  intSync(form);
  form.querySelectorAll('[data-show-key]').forEach(function (w) {
    var other = intFieldOf(form, w.dataset.showKey), input = other && other.querySelector('.int-in');
    w.hidden = !input || (w.dataset.showVal === '*' ? !input.value.trim() : input.value !== w.dataset.showVal);
  });
  form.querySelectorAll('.int-grp, .int-adv').forEach(function (g) { g.hidden = ![].some.call(g.querySelectorAll('.int-field'), function (w) { return !w.hidden; }); });
}
function intPut(input, v) {
  input.value = v;
  input.dispatchEvent(new Event('input', {bubbles: true}));
  input.dispatchEvent(new Event('change', {bubbles: true}));
}
function intOptions(w) {
  var input = w.querySelector('.int-in'), segs = w.querySelectorAll('.int-seg-b');
  if (input.tagName === 'SELECT') return [].map.call(input.options, function (o) { return o.value; });
  if (segs.length) return [].map.call(segs, function (b) { return b.dataset.value; });
  return w.querySelector('.int-tog') ? ['true', 'false'] : null;
}
function intValues(form, mode) {
  var out = {};
  form.querySelectorAll('.int-field').forEach(function (w) {
    if (w.hidden || !w.querySelector('.int-in') || w.classList.contains('int-name')) return;
    var key = w.dataset.key, input = w.querySelector('.int-in');
    if (w.dataset.kind === 'secret') {
      if (input.readOnly) return;
      if (w.dataset.clear === '1') out[key] = '';
      else if (input.value !== '') out[key] = input.value;
      else if (mode !== 'create') out[key] = null;
      return;
    }
    if (mode !== 'test' && input.value === w.dataset.initial && w.dataset.prefill !== '1') return;
    if (mode === 'create' && input.value === '') return;
    out[key] = input.value;
  });
  return out;
}
function intFail(form, e, fallback) {
  var w = e && e.field ? intFieldOf(form, e.field) : null;
  if (w && w.hidden) w = null;
  intSay(form.querySelector('.int-form-msg'), '', false);
  if (w && w.closest('details')) w.closest('details').open = true;
  if (w) intSay(w.querySelector('.int-field-msg'), e.detail || fallback, true);
  else intSay(form.querySelector('.int-form-msg'), e && e.detail ? e.detail : fallback, true);
}
function intBusy(form, on) {
  form.querySelectorAll('button').forEach(function (b) { b.disabled = on; });
}
function intOffChecks(form) {
  return [].filter.call(form.querySelectorAll('.int-check'), function (c) { return !c.checked; }).map(function (c) { return c.dataset.check; });
}
function intGroupHtml(p, g, html) {
  if (!g) return '<div class="int-grp" data-group="">' + html + '</div>';
  var hid = 'int-g-' + p.id + '-' + g;
  return '<section class="int-grp" data-group="' + esc(g) + '" aria-labelledby="' + esc(hid) + '"><h3 class="int-grp-h" id="' + esc(hid) + '">' + esc(intHuman(g)) + '</h3>' + html + '</section>';
}
function intFormHtml(p, inst) {
  var h = '<form class="int-form" data-plugin="' + esc(p.id) + '"' + (inst ? ' data-name="' + esc(inst.name) + '"' : '') + '>';
  var order = [''], by = {'': ''}, adv = '', open = false;
  p.fields.forEach(function (f) {
    var g = intGroupOf(f), html = intFieldHtml(p, f, inst);
    if (g === 'advanced') { adv += html; if (intMoved(f, inst)) open = true; return; }
    if (!Object.prototype.hasOwnProperty.call(by, g)) { by[g] = ''; order.push(g); }
    by[g] += html;
  });
  var top = (by[''] || order.length === 1) ? '' : order[1];
  if (!inst) by[top] = '<div class="int-field int-name" data-key="name"><div class="int-lrow"><label for="int-' + esc(p.id) + '-name">Name<span class="int-req"> *</span></label></div><input id="int-' + esc(p.id) + '-name" class="int-name-in" type="text" maxlength="20" autocomplete="off"><span class="adm-msg int-field-msg" role="status"></span></div>' + by[top];
  order.forEach(function (g) { if (by[g]) h += intGroupHtml(p, g, by[g]); });
  var checks = intShownChecks(p, inst), off = inst ? intList(inst.offChecks) : [];
  if (checks.length) {
    h += '<div class="int-field int-checks" data-key="offChecks" data-kind="checks" role="group" aria-labelledby="int-checks-h"><span class="int-checks-h" id="int-checks-h">What it watches</span>' +
      checks.map(function (c) { return '<label class="int-check-l"><input type="checkbox" class="int-check" data-check="' + esc(c.id) + '"' + (off.indexOf(c.id) >= 0 ? '' : ' checked') + '>' + esc(c.label || c.id) + '</label>'; }).join('') +
      '<span class="adm-msg int-field-msg" role="status"></span></div>';
  }
  if (adv) h += '<details class="int-adv"' + (open ? ' open' : '') + '><summary class="int-adv-h">Advanced</summary><div class="int-adv-b">' + adv + '</div></details>';
  h += '<div class="int-foot"><p class="int-result" role="status" hidden></p><span class="adm-msg int-form-msg" role="status"></span><div class="int-foot-row"><div class="int-test-row"><button type="button" class="btn sec small int-test">Test connection</button>';
  if (inst && inst.source === 'admin') h += '<button type="button" class="btn sec small int-del">Delete</button>';
  h += '</div><div class="int-acts int-foot-acts">';
  if (!inst) h += '<button type="button" class="btn sec small int-cancel">Cancel</button>';
  h += '<button type="submit" class="btn small int-save">Save</button>';
  if ((!inst || !inst.enabled) && p.id !== INT_MCP) h += '<button type="button" class="btn small int-save-on">Save &amp; turn on</button>';
  return h + '</div></div></div></form>';
}
function intForm(slot, p, inst) {
  slot.innerHTML = intFormHtml(p, inst);
  var form = slot.querySelector('form'), result = form.querySelector('.int-result');
  intShowIf(form);
  form.addEventListener('input', function () { intShowIf(form); });
  form.addEventListener('change', function () { intShowIf(form); });
  form.querySelectorAll('.int-clear').forEach(function (b) {
    b.addEventListener('click', function () {
      var w = b.closest('.int-field'), input = w.querySelector('.int-in'), on = w.dataset.clear !== '1', chip = w.querySelector('.int-set');
      if (on) w.dataset.clear = '1'; else delete w.dataset.clear;
      input.disabled = on;
      input.value = '';
      input.placeholder = on ? 'will be cleared on save' : 'set';
      b.textContent = on ? 'Keep' : 'Clear';
      if (chip) { chip.textContent = on ? 'Cleared on save' : chip.dataset.said; chip.classList.toggle('clearing', on); }
    });
  });
  intWidgets(form);
  form.querySelectorAll('.int-sug').forEach(function (b) { intPicker(form, p, inst, b.closest('.int-field')); });
  var cancel = form.querySelector('.int-cancel');
  if (cancel) cancel.addEventListener('click', function () {
    if (!intList(p.instances).length) { intClose(true); return; }
    INT_PANEL.name = undefined;
    intPanelPaint();
    intFocusFirst();
  });
  form.querySelector('.int-test').addEventListener('click', async function () {
    var pressed = document.activeElement;
    form.querySelectorAll('.int-field-msg').forEach(function (m) { intSay(m, '', false); });
    intSay(form.querySelector('.int-form-msg'), '', false);
    intHold(pressed);
    intBusy(form, true);
    result.textContent = 'Testing\u2026';
    result.className = 'int-result';
    result.hidden = false;
    try {
      intFlushPickers(form);
      var off = intOffChecks(form), sent = {name: inst ? inst.name : undefined, values: intValues(form, 'test')};
      if (!intSameSet(off, inst ? intStoredOff(p, inst) : [])) sent.offChecks = off;
      var r = await adminApi('POST', intBase(p) + '/test', sent);
      intSay(result, (r.ok ? '\u2713 ' : '') + r.summary + ' \u00b7 ' + Math.round(Number(r.ms) || 0) + ' ms', !r.ok);
    } catch (e) {
      result.hidden = true;
      result.textContent = '';
      intFail(form, e, 'The test could not run');
    }
    intBusy(form, false);
    intGiveBack(pressed);
  });
  form.addEventListener('submit', function (e) { e.preventDefault(); intSave(form, p, inst, false); });
  var on = form.querySelector('.int-save-on');
  if (on) on.addEventListener('click', function () { intSave(form, p, inst, true); });
  var del = form.querySelector('.int-del');
  if (del) del.addEventListener('click', function () { intDelete(form, p, inst); });
}
function intWidgets(form) {
  form.querySelectorAll('.int-tog').forEach(function (sw) {
    var input = sw.closest('.int-field').querySelector('.int-in');
    sw.addEventListener('click', function () {
      if (input.disabled || sw.getAttribute('aria-disabled') === 'true') return;
      intPut(input, sw.getAttribute('aria-checked') === 'true' ? 'false' : 'true');
    });
    sw.addEventListener('keydown', function (e) {
      if (e.key !== ' ' && e.key !== 'Enter') return;
      e.preventDefault();
      sw.click();
    });
    sw.addEventListener('keyup', function (e) { if (e.key === ' ') e.preventDefault(); });
  });
  form.querySelectorAll('.int-seg').forEach(function (seg) {
    var input = seg.closest('.int-field').querySelector('.int-in'), bs = [].slice.call(seg.querySelectorAll('.int-seg-b'));
    var pick = function (b) {
      if (!b || input.disabled) return;
      if (input.value !== b.dataset.value) intPut(input, b.dataset.value);
      b.focus();
    };
    bs.forEach(function (b) { b.addEventListener('click', function () { pick(b); }); });
    seg.addEventListener('keydown', function (e) {
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
  });
  form.querySelectorAll('.int-eye').forEach(function (b) {
    var input = b.closest('.int-field').querySelector('.int-in'), name = b.dataset.label;
    b.addEventListener('click', function () {
      var on = b.getAttribute('aria-pressed') !== 'true';
      input.type = on ? 'text' : 'password';
      b.setAttribute('aria-pressed', String(on));
      b.setAttribute('aria-label', (on ? 'Hide ' : 'Show ') + name);
      b.textContent = on ? 'Hide' : 'Show';
    });
  });
  form.querySelectorAll('.int-entry').forEach(function (box) {
    var w = box.closest('.int-field'), input = w.querySelector('.int-in'), change = box.querySelector('.int-entry-change');
    change.addEventListener('click', function () {
      w.dataset.pick = '1';
      intSync(form);
      input.focus();
    });
    box.querySelector('.int-entry-clear').addEventListener('click', function () {
      intPut(input, '');
      change.focus();
    });
    input.addEventListener('change', function () {
      if (w.dataset.pick !== '1') return;
      delete w.dataset.pick;
      intSync(form);
      change.focus();
    });
  });
}
async function intChecksStored(p, name, sent, said) {
  if (!(await loadIntegrations())) return false;
  var fresh = intFind(p.id), i = fresh && intList(fresh.instances).filter(function (x) { return x.name === name; })[0];
  if (!i || !intSameSet(intStoredOff(fresh, i), sent) || !INT_PANEL || INT_PANEL.id !== p.id) return false;
  INT_PANEL.name = name;
  if (intPanelPaint(said || name + ' saved')) intFocusFirst('.int-check');
  return true;
}
async function intSave(form, p, inst, turnOn) {
  var msg = form.querySelector('.int-form-msg'), body = {}, mcp = intMcpBoxOf(p);
  if (mcp && !intMcpClaim(mcp, msg)) return;
  form.querySelectorAll('.int-field-msg').forEach(function (m) { intSay(m, '', false); });
  intFlushPickers(form);
  var values = intValues(form, inst ? 'edit' : 'create'), off = intOffChecks(form), pressed = document.activeElement;
  intHold(pressed);
  intBusy(form, true);
  intSay(msg, 'Saving\u2026', false);
  try {
    var said, reopen = null;
    if (inst) {
      if (Object.keys(values).some(function (k) { return values[k] !== null; })) body.values = values;
      if (turnOn && !inst.enabled) body.enabled = true;
      if (!intSameSet(off, intStoredOff(p, inst))) body.offChecks = off;
      if (!Object.keys(body).length) { intSay(msg, 'Nothing changed', false); intBusy(form, false); intGiveBack(pressed); return; }
      await adminApi('PATCH', intBase(p) + '/instances/' + encodeURIComponent(inst.name), body);
      said = inst.name + ' saved' + (body.enabled ? ' and turned on' : '');
      if (p.id === INT_MCP && body.values && typeof body.values.url === 'string' && body.values.url !== String((inst.values || {}).url || '')) {
        reopen = [inst.name, inst.name + ' saved: list its tools again, then confirm the warnings'];
      }
    } else {
      var name = form.querySelector('.int-name-in').value.trim();
      body = {name: name, values: values, enabled: !!turnOn};
      if (off.length) body.offChecks = off;
      await adminApi('POST', intBase(p) + '/instances', body);
      said = name + ' added' + (turnOn ? ' and turned on' : '');
      if (p.id === INT_MCP) reopen = [name, name + ' added: list its tools, then confirm the warnings'];
    }
    if (reopen) {
      intClose(false);
      intWant(reopen[0]);
      INT_MCP_NOTE = reopen[1];
      await loadIntegrations(said);
      return;
    }
    await loadIntegrations(said);
    intClose(true);
  } catch (e) {
    if (inst && body.offChecks && e && e.code === 409 && await intChecksStored(p, inst.name, body.offChecks, e.detail)) return;
    intFail(form, e, 'Not saved, try again');
    intBusy(form, false);
    intGiveBack(pressed);
  } finally {
    if (mcp) delete mcp.dataset.busy;
  }
}
async function intDelete(form, p, inst) {
  var ask = window.__skopaxConfirm, del = form.querySelector('.int-del'), msg = form.querySelector('.int-form-msg');
  if (typeof ask !== 'function') return;
  var ok = await ask('Delete ' + inst.name + '?', 'The instance ' + inst.name + ' of ' + intName(p) + ' is deleted with its saved values and secrets. This cannot be undone.', 'Delete');
  if (!ok) { if (del.isConnected) del.focus(); return; }
  if (!del.isConnected) return;
  del.focus();
  intHold(del);
  intBusy(form, true);
  intSay(msg, 'Deleting\u2026', false);
  try {
    await adminApi('DELETE', intBase(p) + '/instances/' + encodeURIComponent(inst.name));
    await loadIntegrations(inst.name + ' deleted');
    intClose(true);
  } catch (e) {
    intBusy(form, false);
    intSay(msg, e && e.detail ? e.detail : 'Not deleted, try again', true);
    intGiveBack(del);
  }
}
function intMcpBase(name) { return '/api/admin/mcp-tools/instances/' + encodeURIComponent(name); }
function intMcpItem(it) { return !!it && typeof it.id === 'string' && it.id !== ''; }
function intMcpTexts(r) { return r && r.texts && typeof r.texts === 'object' ? r.texts : {}; }
function intMcpPresets() {
  if (!INT_MCP_PRESETS) INT_MCP_PRESETS = adminApi('GET', '/api/admin/mcp-tools/presets').then(function (r) { return r && typeof r === 'object' ? r : null; }, function () { INT_MCP_PRESETS = null; return null; });
  return INT_MCP_PRESETS;
}
function intMcpGroup(pop, mcp) {
  var g = accText('section', 'int-cat-grp int-mcp-grp'), grid = accText('div', 'int-pg');
  g.dataset.cat = 'mcp';
  g.appendChild(accText('h4', 'int-cat-h', 'MCP servers'));
  grid.appendChild(intMcpPick(mcp, null, {}));
  g.appendChild(grid);
  if (intUsable(mcp)) intMcpPresets().then(function (r) {
    if (!r || !g.isConnected) return;
    var texts = intMcpTexts(r), search = pop.querySelector('.int-search');
    intList(r.items).filter(intMcpItem).forEach(function (it) { grid.appendChild(intMcpPick(mcp, it, texts)); });
    intList(r.soon).filter(intMcpItem).forEach(function (it) { grid.appendChild(intMcpSoon(it, texts)); });
    intPopFind(pop, search ? search.value : '');
  });
  return g;
}
function intMcpPick(mcp, preset, texts) {
  var name = preset ? String(preset.name || preset.id) : intName(mcp), b = accText('button', 'int-pick int-mcp-pick');
  b.type = 'button';
  if (preset) b.dataset.preset = preset.id;
  b.dataset.find = foldCase([name, preset ? preset.id : mcp.id].join(' '));
  b.title = name;
  var tags = [preset && preset.knowledge === 'ready' ? intKnowTagOf({name: 'on', text: 'Knowledge', tip: INT_KNOW_TIP}) : null];
  if (preset && preset.preview === true) {
    tags.push(accText('span', 'tag k-info int-mcp-tag', 'Preview'));
    b.title = name + (texts.preview ? ' \u00b7 ' + String(texts.preview) : '');
  }
  intPickBody(b, preset ? intIcon({id: preset.id, name: name}) : intIcon(mcp), name, '', '', tags, null);
  if (!intUsable(mcp)) {
    b.setAttribute('aria-disabled', 'true');
    b.title = String(mcp.reason || '');
  }
  var go = function () {
    if (b.getAttribute('aria-disabled') === 'true') return;
    intPopClose(false);
    intOpen(INT_MCP, null, 'add');
    if (preset) intMcpPrefill(preset, texts);
  };
  b.addEventListener('click', go);
  b.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); go(); } });
  return b;
}
function intMcpSoon(it, texts) {
  var name = String(it.name || it.id), b = accText('button', 'int-pick int-mcp-pick int-mcp-soon');
  b.type = 'button';
  b.dataset.preset = it.id;
  b.dataset.find = foldCase(name + ' ' + it.id);
  b.setAttribute('aria-disabled', 'true');
  intPickBody(b, intIcon({id: it.id, name: name}), name, '', '', [accText('span', 'tag plain int-mcp-tag', String(texts.soon || ''))], null);
  return b;
}
function intMcpPrefill(preset, texts) {
  var panel = intPanelEl(), form = panel && panel.querySelector('.int-slot .int-form');
  if (!form || form.dataset.plugin !== INT_MCP || form.dataset.name !== undefined) return;
  [['url', preset.url], ['auth', preset.auth], ['header-name', preset.headerName], ['oauth-scopes', preset.scopes], ['preset', preset.id]].forEach(function (x) {
    var w = intFieldOf(form, x[0]), input = w && w.querySelector('.int-in'), allowed = input && intOptions(w);
    if (!input || typeof x[1] !== 'string' || !x[1]) return;
    if (allowed && allowed.indexOf(x[1]) < 0) return;
    input.value = x[1];
    w.dataset.prefill = '1';
  });
  var hint = intFieldOf(form, 'header-value'), secret = hint && hint.querySelector('.int-in'), oauth = intFieldOf(form, 'oauth-scopes');
  if (secret && typeof preset.headerHint === 'string' && preset.headerHint) secret.placeholder = preset.headerHint;
  if (preset.clientRequired === true && oauth) {
    var note = accText('p', 'adm-help int-mcp-client', String(texts.client || ''));
    note.dataset.showKey = 'auth';
    note.dataset.showVal = 'oauth';
    oauth.before(note);
  }
  intShowIf(form);
}
function intWant(name, expired) {
  INT_MCP_BACK = typeof name === 'string' && name ? name : null;
  INT_MCP_EXPIRED = expired === true;
}
function intMcpBack() {
  var name = INT_MCP_BACK, p = intFind(INT_MCP), note = INT_MCP_NOTE, expired = INT_MCP_EXPIRED;
  INT_MCP_BACK = null;
  INT_MCP_NOTE = '';
  INT_MCP_EXPIRED = false;
  if (expired) {
    history.replaceState(null, '', location.pathname + location.search + '#t=integrations');
    intSay(document.querySelector('#integrations .int-note'), 'The sign-in took too long or its link was already used; open the connection and start it again.', true);
    return;
  }
  if (!name) return;
  history.replaceState(null, '', location.pathname + location.search + '#t=integrations');
  if (!p || !intUsable(p) || !intList(p.instances).some(function (i) { return i && i.name === name; })) return;
  intOpen(INT_MCP, name, note ? {} : {back: true});
  if (note) intSay(intPanelEl().querySelector('.int-panel-note'), note, false);
}
function intMcpMount(panel, p, inst) {
  var box = accText('div', 'int-mcp'), held = INT_PANEL.mcp;
  box.dataset.name = inst.name;
  INT_PANEL.mcp = null;
  panel.appendChild(box);
  var form = panel.querySelector('.int-slot .int-form'), know = form && intFieldOf(form, 'knowledge'), lock = know && know.querySelector('.int-in');
  if (lock) { lock.disabled = true; intSync(form); }
  if (held && held.name === inst.name && held.view) { intMcpPaint(box, p, inst, held.view); return; }
  box.appendChild(accText('p', 'muted int-mcp-wait', 'Loading the connection\u2026'));
  intMcpLoad(box, p, inst);
}
function intMcpTrack(name, work) {
  var done = work.then(function () {}, function () {});
  INT_MCP_WRITES[name] = done;
  done.then(function () { if (INT_MCP_WRITES[name] === done) delete INT_MCP_WRITES[name]; });
  return work;
}
async function intMcpLoad(box, p, inst) {
  var seq = ++INT_MCP_SEQ;
  try {
    while (INT_MCP_WRITES[inst.name]) await INT_MCP_WRITES[inst.name];
    if (seq !== INT_MCP_SEQ || !box.isConnected) return;
    var v = await adminApi('GET', intMcpBase(inst.name));
    if (seq === INT_MCP_SEQ && box.isConnected) intMcpPaint(box, p, inst, v);
  } catch (e) {
    if (seq !== INT_MCP_SEQ || !box.isConnected) return;
    box.textContent = '';
    box.appendChild(accText('p', 'adm-msg bad int-mcp-fail', e && e.detail ? e.detail : 'The connection did not load; open it again.'));
  }
}
function intMcpAfter(inst, v, note, sel) {
  if (!INT_PANEL) return Promise.resolve();
  INT_PANEL.mcp = {name: inst.name, view: v};
  if (INT_PANEL.name !== inst.name) return loadIntegrations().then(adminNone);
  return intRepaint(note, sel);
}
function intMcpPaint(box, p, inst, v) {
  box.textContent = '';
  if (!v || typeof v !== 'object') return;
  var auth = v.auth && typeof v.auth === 'object' ? v.auth : {}, know = v.knowledge && typeof v.knowledge === 'object' ? v.knowledge : {}, s = INT_PANEL, notes = intMcpNotes(v);
  intMcpGate(p, inst, v);
  intMcpIdentity(v);
  intMcpKnowField(know);
  if (auth.mode === 'oauth') box.appendChild(intMcpConn(box, inst, auth));
  box.appendChild(intMcpTools(box, p, inst, v));
  if (know.possible === true) box.appendChild(intMcpScope(box, inst, know, notes));
  else if (know.state === 'soon') box.appendChild(intMcpKnowSoon(know, notes));
  box.appendChild(intMcpWarn(box, inst, v));
  if (s && s.from && s.from.back === true) {
    s.from = {};
    var last = auth.last, note = intPanelEl().querySelector('.int-panel-note');
    if (last && typeof last.text === 'string' && last.text && intMcpFresh(last.at)) intSay(note, last.text, last.result !== 'connected');
  }
}
function intMcpNotes(v) {
  var n = v.notes && typeof v.notes === 'object' ? v.notes : {};
  return {direct: typeof n.direct === 'string' ? n.direct : '', truncated: typeof n.truncated === 'string' ? n.truncated : '', knowledgeSoon: typeof n.knowledgeSoon === 'string' ? n.knowledgeSoon : '',
    empty: typeof n.empty === 'string' ? n.empty : ''};
}
function intMcpFresh(at) {
  var t = typeof at === 'string' ? new Date(at).getTime() : NaN;
  return !isNaN(t) && Math.abs(Date.now() - t) <= INT_MCP_FRESH_MS;
}
function intMcpBoxOf(p) {
  var panel = intPanelEl();
  return p && p.id === INT_MCP && panel ? panel.querySelector('.int-mcp') : null;
}
function intMcpClaim(box, msg) {
  if (box.dataset.busy === '1') { intSay(msg, 'Wait for the last change to finish', true); return false; }
  box.dataset.busy = '1';
  return true;
}
function intMcpKnowField(know) {
  var panel = intPanelEl(), form = panel && panel.querySelector('.int-slot .int-form'), w = form && intFieldOf(form, 'knowledge'), input = w && w.querySelector('.int-in');
  if (!input) return;
  var stuck = know.possible !== true && know.on !== true;
  w.hidden = stuck && know.state === 'soon';
  input.disabled = stuck && know.state !== 'soon';
  intShowIf(form);
}
function intMcpSection(key, title) {
  var s = accText('section', 'int-mcp-sec int-mcp-' + key), h = accText('h3', 'int-mcp-h', title);
  h.id = 'int-mcp-' + key + '-h';
  s.setAttribute('aria-labelledby', h.id);
  s.appendChild(h);
  return s;
}
function intMcpStatus(cls) {
  var msg = accText('span', 'adm-msg ' + cls);
  msg.setAttribute('role', 'status');
  return msg;
}
function intMcpGate(p, inst, v) {
  var panel = intPanelEl(), ok = !!(v.confirm && v.confirm.current === true);
  if (!panel || inst.enabled) return;
  var on = panel.querySelector('.int-inst .int-on'), form = panel.querySelector('.int-slot .int-form'), save = form && form.querySelector('.int-save'), more = form && form.querySelector('.int-save-on');
  if (on) on.disabled = !ok;
  if (!save) return;
  if (ok && !more) {
    more = accText('button', 'btn small int-save-on', 'Save & turn on');
    more.type = 'button';
    save.after(more);
    more.addEventListener('click', function () { intSave(form, p, inst, true); });
  } else if (!ok && more) more.remove();
}
function intMcpIdentity(v) {
  var panel = intPanelEl(), form = panel && panel.querySelector('.int-slot .int-form'), w = form && intFieldOf(form, 'chat-role'), input = w && w.querySelector('.int-in');
  if (!input) return;
  var old = w.querySelector('.int-mcp-identity'), seg = w.querySelector('.int-seg');
  if (old) old.remove();
  if (typeof v.identity !== 'string' || !v.identity) { input.removeAttribute('aria-describedby'); if (seg) seg.removeAttribute('aria-describedby'); return; }
  var note = accText('p', 'adm-help int-mcp-identity', v.identity), help = w.querySelector('.adm-help');
  note.id = 'int-mcp-identity';
  (help || seg || input).after(note);
  input.setAttribute('aria-describedby', note.id);
  if (seg) seg.setAttribute('aria-describedby', note.id);
}
function intMcpConn(box, inst, auth) {
  var s = intMcpSection('conn', 'Connection'), st = Object.prototype.hasOwnProperty.call(INT_MCP_STATES, auth.state) ? auth.state : 'none', line = INT_MCP_STATES[st];
  if (st === 'connected' && auth.connectedBy) line += ' by ' + auth.connectedBy;
  if (st === 'connected' && dayhm(auth.connectedAt)) line += ' at ' + dayhm(auth.connectedAt);
  s.appendChild(accText('p', 'int-mcp-state', line));
  if (auth.last && typeof auth.last.text === 'string' && auth.last.text && auth.last.result !== 'connected') s.appendChild(accText('p', 'adm-help int-mcp-last', auth.last.text + (dayhm(auth.last.at) ? ' \u00b7 ' + dayhm(auth.last.at) : '')));
  var acts = accText('div', 'int-acts'), msg = intMcpStatus('int-mcp-cmsg'), b;
  if (st === 'connected') {
    b = accText('button', 'btn sec small int-mcp-disconnect', 'Disconnect');
    b.addEventListener('click', function () { intMcpDisconnect(box, b, msg, inst); });
  } else {
    b = accText('button', 'btn small int-mcp-connect', st === 'reconnect' ? 'Reconnect' : 'Connect');
    b.addEventListener('click', function () { intMcpConnect(b, msg, inst); });
  }
  b.type = 'button';
  acts.appendChild(b);
  acts.appendChild(msg);
  s.appendChild(acts);
  return s;
}
async function intMcpConnect(b, msg, inst) {
  if (b.disabled) return;
  intHold(b);
  b.disabled = true;
  intSay(msg, 'Opening the sign-in\u2026', false);
  try {
    var r = await adminApi('POST', intMcpBase(inst.name) + '/oauth/start'), url = r && typeof r.url === 'string' ? r.url : '';
    if (!/^https?:\/\//i.test(url)) throw {detail: null};
    window.__skopaxAssign(url);
  } catch (e) {
    b.disabled = false;
    intSay(msg, e && e.detail ? e.detail : 'The sign-in did not start, try again', true);
    intGiveBack(b);
  }
}
async function intMcpDisconnect(box, b, msg, inst) {
  if (b.disabled || !intMcpClaim(box, msg)) return;
  intHold(b);
  b.disabled = true;
  intSay(msg, 'Disconnecting\u2026', false);
  try {
    await intMcpTrack(inst.name, adminApi('DELETE', intMcpBase(inst.name) + '/oauth'));
  } catch (e) {
    delete box.dataset.busy;
    b.disabled = false;
    intSay(msg, e && e.detail ? e.detail : 'Not disconnected, try again', true);
    intGiveBack(b);
    return;
  }
  try {
    var v = await adminApi('GET', intMcpBase(inst.name)).catch(adminNone);
    await intMcpAfter(inst, v, inst.name + ' disconnected', '.int-mcp-connect');
  } finally {
    delete box.dataset.busy;
  }
}
function intMcpTools(box, p, inst, v) {
  var s = intMcpSection('tools', 'What it can look up'), tools = intList(v.tools).filter(function (t) { return t && typeof t.name === 'string'; }), srv = v.server && typeof v.server === 'object' ? v.server : null;
  if (v.listedAt) s.appendChild(accText('p', 'adm-help int-mcp-server', [srv && srv.name ? String(srv.name) + (srv.version ? ' ' + String(srv.version) : '') : '', tools.length + (tools.length === 1 ? ' tool' : ' tools'), 'listed ' + ago(v.listedAt)].filter(Boolean).join(' \u00b7 ')));
  else s.appendChild(accText('p', 'adm-help int-mcp-server', 'Not listed yet: Refresh tools asks the server what it offers.'));
  if (v.truncated === true && intMcpNotes(v).truncated) s.appendChild(accText('p', 'adm-help int-mcp-cut', intMcpNotes(v).truncated));
  if (v.refused && typeof v.refused === 'object' && typeof v.refused.text === 'string' && v.refused.text) {
    s.appendChild(accText('p', 'adm-msg bad int-mcp-refused', (v.refused.call === true ? 'A tool call was refused: ' : 'Left out of runs: ') + v.refused.text + (dayhm(v.refused.at) ? ' \u00b7 ' + dayhm(v.refused.at) : '')));
  }
  var acts = accText('div', 'int-acts'), b = accText('button', 'btn sec small int-mcp-refresh', 'Refresh tools'), msg = intMcpStatus('int-mcp-tmsg');
  b.type = 'button';
  b.addEventListener('click', function () { intMcpRefresh(box, p, inst, b, msg); });
  acts.appendChild(b);
  acts.appendChild(msg);
  s.appendChild(acts);
  if (!tools.length) return s;
  var list = accText('ul', 'int-mcp-tlist');
  tools.forEach(function (t, i) { list.appendChild(intMcpTool(box, p, inst, v, t, i)); });
  s.appendChild(list);
  return s;
}
async function intMcpRefresh(box, p, inst, b, msg) {
  if (b.disabled || !intMcpClaim(box, msg)) return;
  intHold(b);
  b.disabled = true;
  intSay(msg, 'Listing the tools\u2026', false);
  try {
    var v = await intMcpTrack(inst.name, adminApi('POST', intMcpBase(inst.name) + '/tools/refresh'));
    if (!box.isConnected) return;
    intMcpPaint(box, p, inst, v);
    intSay(box.querySelector('.int-mcp-tmsg'), intList(v && v.tools).length ? 'Tools listed' : intMcpNotes(v).empty || 'Tools listed', false);
    intGiveBack(box.querySelector('.int-mcp-refresh'));
  } catch (e) {
    if (!b.isConnected) return;
    var said = e && e.detail ? e.detail : 'The tools were not listed, try again';
    if (e && e.code === 409) {
      var seq = INT_MCP_SEQ, fresh = await adminApi('GET', intMcpBase(inst.name)).catch(adminNone);
      if (fresh && seq === INT_MCP_SEQ && box.isConnected) {
        intMcpPaint(box, p, inst, fresh);
        intSay(box.querySelector('.int-mcp-tmsg'), said, true);
        intGiveBack(box.querySelector('.int-mcp-refresh'));
        return;
      }
      if (!b.isConnected) return;
    }
    b.disabled = false;
    intSay(msg, said, true);
    intGiveBack(b);
  } finally {
    delete box.dataset.busy;
  }
}
function intMcpSwitchOf(box, name) {
  var li = [].filter.call(box.querySelectorAll('.int-mcp-tool'), function (x) { return x.dataset.tool === name; })[0];
  return li ? li.querySelector('.int-mcp-sw') : null;
}
function intMcpTool(box, p, inst, v, t, i) {
  var li = accText('li', 'int-mcp-tool'), top = accText('div', 'int-mcp-trow'), label = accText('span', 'int-mcp-tn'), kind = INT_MCP_KINDS[t.kind] || INT_MCP_KINDS.unknown, sw = accText('button', 'int-switch int-mcp-sw');
  li.dataset.tool = t.name;
  label.appendChild(accText('b', null, t.name));
  if (typeof t.title === 'string' && t.title && t.title !== t.name) label.appendChild(accText('span', 'int-mcp-tt', ' ' + t.title));
  top.appendChild(label);
  top.appendChild(accText('span', 'tag ' + kind[1] + ' int-mcp-kind', kind[0]));
  sw.type = 'button';
  sw.setAttribute('role', 'switch');
  sw.setAttribute('aria-checked', t.on === true ? 'true' : 'false');
  sw.setAttribute('aria-label', 'Switch ' + t.name);
  top.appendChild(sw);
  li.appendChild(top);
  if (typeof t.description === 'string' && t.description) li.appendChild(accText('p', 'int-mcp-tdesc', t.description));
  if (typeof t.blocked === 'string' && t.blocked) {
    var why = accText('p', 'int-mcp-blocked', t.blocked);
    why.id = 'int-mcp-why-' + i;
    sw.setAttribute('aria-disabled', 'true');
    sw.setAttribute('aria-describedby', why.id);
    li.appendChild(why);
  }
  if (typeof t.flagged === 'string' && t.flagged) li.appendChild(accText('p', 'int-mcp-flag', t.flagged));
  if (t.changed === true) li.appendChild(accText('p', 'int-mcp-changed', 'Changed on the server'));
  li.appendChild(intMcpStatus('int-mcp-rmsg'));
  sw.addEventListener('click', function () {
    if (sw.getAttribute('aria-disabled') === 'true' || sw.getAttribute('aria-busy') === 'true') return;
    var want = t.on !== true;
    if (want && t.kind === 'unknown') intMcpAsk(box, p, inst, v, t, li);
    else intMcpSwitch(box, p, inst, v, t.name, want, false, sw);
  });
  return li;
}
function intMcpAsk(box, p, inst, v, t, li) {
  var open = li.querySelector('.int-mcp-ask');
  if (open) { open.remove(); return; }
  var text = String((v.warnings && v.warnings.toolText) || '').split('{tool}').join(t.name);
  var ask = accText('div', 'int-mcp-ask'), label = accText('label', 'int-mcp-check'), cb = document.createElement('input'), go = accText('button', 'btn small int-mcp-turn', 'Turn on'), msg = intMcpStatus('int-mcp-amsg');
  ask.setAttribute('role', 'group');
  ask.setAttribute('aria-label', 'Turn on ' + t.name);
  ask.appendChild(accText('p', 'int-mcp-asktext', text));
  cb.type = 'checkbox';
  cb.className = 'int-mcp-agree';
  label.appendChild(cb);
  label.appendChild(document.createTextNode('I understand'));
  ask.appendChild(label);
  go.type = 'button';
  ask.appendChild(go);
  ask.appendChild(msg);
  go.addEventListener('click', function () {
    if (go.getAttribute('aria-busy') === 'true') return;
    if (!cb.checked) { intSay(msg, 'Tick I understand first', true); return; }
    intMcpSwitch(box, p, inst, v, t.name, true, true, go);
  });
  li.insertBefore(ask, li.querySelector('.int-mcp-rmsg'));
  cb.focus();
}
async function intMcpSwitch(box, p, inst, v, name, want, understood, el) {
  var on = intList(v.tools).filter(function (x) { return x && x.on === true && x.name !== name; }).map(function (x) { return x.name; }), row = el.closest('.int-mcp-tool'), msg = row && row.querySelector('.int-mcp-rmsg');
  if (want) on.push(name);
  if (!intMcpClaim(box, msg)) return;
  intHold(el);
  el.setAttribute('aria-busy', 'true');
  intSay(msg, 'Saving\u2026', false);
  try {
    var r = await intMcpTrack(inst.name, adminApi('PUT', intMcpBase(inst.name) + '/tools', {on: on, understood: !!understood}));
    if (!box.isConnected) return;
    intMcpPaint(box, p, inst, r);
    var fresh = intMcpSwitchOf(box, name);
    if (fresh) intSay(fresh.closest('.int-mcp-tool').querySelector('.int-mcp-rmsg'), name + (want ? ' switched on' : ' switched off'), false);
    intGiveBack(fresh);
  } catch (e) {
    if (!el.isConnected) return;
    el.removeAttribute('aria-busy');
    intSay(msg, e && e.detail ? e.detail : 'Not saved, try again', true);
    intGiveBack(el);
  } finally {
    delete box.dataset.busy;
  }
}
function intMcpLink(label, url) {
  if (typeof url !== 'string' || !/^https:\/\//i.test(url)) return accText('span', 'int-mcp-plain', label);
  var a = accText('a', 'int-mcp-link', label);
  a.href = url;
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  return a;
}
function intMcpScope(box, inst, know, notes) {
  var noun = know.noun === 'repository' ? 'repository' : 'folder', s = intMcpSection('scope', 'Knowledge source'), sc = know.scope && typeof know.scope === 'object' ? know.scope : null, picked = accText('p', 'int-mcp-picked');
  if (sc && typeof sc.label === 'string' && sc.label) {
    picked.appendChild(document.createTextNode('Searches the ' + noun + ' '));
    picked.appendChild(intMcpLink(sc.label, sc.url));
  } else picked.textContent = 'No ' + noun + ' picked yet: search and pick one.';
  s.appendChild(picked);
  if (noun === 'folder' && notes.direct) s.appendChild(accText('p', 'adm-help int-mcp-direct', notes.direct));
  var find = accText('form', 'int-mcp-find'), lab = accText('label', 'int-mcp-ql', 'Find a ' + noun), q = document.createElement('input'), go = accText('button', 'btn sec small int-mcp-search', 'Search'), msg = intMcpStatus('int-mcp-smsg'), list = accText('ul', 'int-mcp-folders');
  find.setAttribute('role', 'search');
  q.type = 'search';
  q.id = 'int-mcp-q';
  q.className = 'int-mcp-q';
  q.maxLength = 100;
  q.setAttribute('autocomplete', 'off');
  q.setAttribute('spellcheck', 'false');
  lab.htmlFor = q.id;
  go.type = 'submit';
  find.appendChild(lab);
  find.appendChild(q);
  find.appendChild(go);
  find.appendChild(msg);
  find.addEventListener('submit', function (e) { e.preventDefault(); intMcpFind(box, inst, noun, q, go, msg, list); });
  s.appendChild(find);
  s.appendChild(list);
  return s;
}
async function intMcpFind(box, inst, noun, q, go, msg, list) {
  if (go.disabled) return;
  intHold(go);
  go.disabled = true;
  intSay(msg, 'Searching\u2026', false);
  try {
    var r = await adminApi('GET', intMcpBase(inst.name) + '/folders?q=' + encodeURIComponent(q.value.trim()));
    if (!list.isConnected) return;
    var items = intList(r && r.items).filter(intMcpItem);
    list.textContent = '';
    items.forEach(function (it) { list.appendChild(intMcpFolder(box, inst, noun, it, msg)); });
    intSay(msg, items.length ? items.length + ' found' : 'Nothing found', false);
  } catch (e) {
    if (!list.isConnected) return;
    intSay(msg, e && e.detail ? e.detail : 'The search did not run, try again', true);
  }
  go.disabled = false;
  intGiveBack(go);
}
function intMcpFolder(box, inst, noun, it, msg) {
  var li = accText('li', 'int-mcp-fitem'), b = accText('button', 'int-mcp-folder', String(it.label || it.id));
  b.type = 'button';
  b.addEventListener('click', async function () {
    if (b.disabled || !intMcpClaim(box, msg)) return;
    intHold(b);
    b.disabled = true;
    intSay(msg, 'Saving\u2026', false);
    try {
      var v = await intMcpTrack(inst.name, adminApi('PUT', intMcpBase(inst.name) + '/scope', {id: it.id}));
      await intMcpAfter(inst, v, inst.name + ': ' + noun + ' set', '.int-mcp-q');
    } catch (e) {
      if (!b.isConnected) return;
      b.disabled = false;
      intSay(msg, e && e.detail ? e.detail : 'Not saved, try again', true);
      intGiveBack(b);
    } finally {
      delete box.dataset.busy;
    }
  });
  li.appendChild(b);
  return li;
}
function intMcpKnowSoon(know, notes) {
  var s = intMcpSection('scope', 'Knowledge source'), line = accText('div', 'int-mcp-krow'), text = accText('p', 'int-mcp-ksoon'), sw = accText('button', 'int-switch int-mcp-ksw');
  text.id = 'int-mcp-ksoon';
  sw.type = 'button';
  sw.setAttribute('role', 'switch');
  sw.setAttribute('aria-checked', know.on === true ? 'true' : 'false');
  sw.setAttribute('aria-disabled', 'true');
  sw.setAttribute('aria-label', 'Knowledge source');
  text.textContent = notes.knowledgeSoon;
  if (notes.knowledgeSoon) sw.setAttribute('aria-describedby', text.id);
  line.appendChild(text);
  line.appendChild(sw);
  s.appendChild(line);
  return s;
}
function intMcpWarn(box, inst, v) {
  var w = v.warnings && typeof v.warnings === 'object' ? v.warnings : {}, c = v.confirm && typeof v.confirm === 'object' ? v.confirm : {}, s = intMcpSection('warn', String(w.title || 'Warnings'));
  if (c.current === true) {
    s.appendChild(accText('p', 'int-mcp-confirmed', 'Confirmed' + (c.by ? ' by ' + c.by : '') + (dayhm(c.at) ? ' at ' + dayhm(c.at) : '')));
    return s;
  }
  var ol = accText('ol', 'int-mcp-warns'), label = accText('label', 'int-mcp-check'), cb = document.createElement('input'), acts = accText('div', 'int-acts'), b = accText('button', 'btn small int-mcp-confirm', String(w.button || '')), msg = intMcpStatus('int-mcp-msg');
  intList(w.items).forEach(function (it) { if (it && typeof it.text === 'string') ol.appendChild(accText('li', null, it.text)); });
  s.appendChild(ol);
  cb.type = 'checkbox';
  cb.className = 'int-mcp-agree';
  label.appendChild(cb);
  label.appendChild(document.createTextNode('I understand'));
  s.appendChild(label);
  b.type = 'button';
  acts.appendChild(b);
  acts.appendChild(msg);
  s.appendChild(acts);
  b.addEventListener('click', async function () {
    if (b.disabled) return;
    if (!cb.checked) { intSay(msg, 'Tick I understand first', true); return; }
    if (!intMcpClaim(box, msg)) return;
    intHold(b);
    b.disabled = true;
    intSay(msg, 'Saving\u2026', false);
    try {
      var r = await intMcpTrack(inst.name, adminApi('POST', intMcpBase(inst.name) + '/confirm', {understood: true, version: w.version}));
      await intMcpAfter(inst, r, inst.name + ': warnings confirmed', '.int-on');
    } catch (e) {
      if (!b.isConnected) return;
      b.disabled = false;
      intSay(msg, e && e.detail ? e.detail : 'Not saved, try again', true);
      var know = v.knowledge && typeof v.knowledge === 'object' ? v.knowledge : {}, q = box.querySelector('.int-mcp-q');
      if (q && know.possible === true && !know.scope) {
        msg.id = 'int-mcp-msg';
        q.setAttribute('aria-describedby', msg.id);
        q.focus();
      } else intGiveBack(b);
    } finally {
      delete box.dataset.busy;
    }
  });
  return s;
}
(function () {
  var w = intWrap();
  if (w) w.querySelector('.int-scrim').addEventListener('click', function () { intClose(true); });
  window.__skopaxAssign = function (url) { location.assign(url); };
})();
