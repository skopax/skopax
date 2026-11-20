var ADM_NONE_ID = 'adm-none';
function admChanged(i) {
  if (i.default == null) return false;
  if (i.kind === 'rules') return !!i.rules && !!i.defaults && Object.keys(i.defaults).some(function (k) { return i.rules[k] !== i.defaults[k]; });
  return String(i.value == null ? '' : i.value) !== String(i.default);
}
function admDot(i) {
  if (!admChanged(i)) return '';
  var said = 'Changed from the default' + (i.kind === 'rules' ? '' : ': ' + (i.default === '' ? 'empty' : i.default));
  return '<span class="adm-dot" role="img" aria-label="' + esc(said) + '" title="' + esc(said) + '"></span>';
}
function admSwitch(i, id, named) {
  return '<span class="adm-sw-w"><input id="' + esc(id) + '" class="adm-in adm-sw" type="checkbox" role="switch"' + named + (i.value === 'true' ? ' checked' : '') + '></span>';
}
function admUnit(i, input) {
  var u = typeof i.unit === 'string' ? i.unit : '';
  if (!u) return input;
  var tag = '<span class="adm-unit">' + esc(u) + '</span>';
  return '<span class="adm-num' + (u === '$' ? ' pre' : '') + '">' + (u === '$' ? tag + input : input + tag) + '</span>';
}
function admMeta(i) {
  return '<div class="adm-m">' + adminSource(i) + (i.live ? '' : '<span class="tag k-warn" title="Read when the service starts: saved now, used after the next restart">after restart</span>') +
    (i.source === 'admin' ? '<button type="button" class="btn sec small adm-reset" title="' + esc('Drop the value set here and go back to ' + (i.base ? i.base : 'the default')) + '">Reset</button>' : '') + '</div>';
}
function admRowHtml(i) {
  var name = typeof i.shortLabel === 'string' && i.shortLabel ? esc(i.shortLabel) + '<span class="sr">' + esc(i.label.slice(i.shortLabel.length)) + '</span>' : esc(i.label);
  return '<div class="adm-l"><div class="adm-n"><b id="' + esc(adminId(i.key) + '-l') + '">' + name + '</b>' + admDot(i) + '</div>' +
    admHelp(i.help) + '<code class="adm-key">' + esc(i.key) + '</code></div>' +
    '<div class="adm-c">' + adminControl(i) + (i.pluginTerms && i.pluginTerms.length ? '<p class="adm-help adm-plugin-terms">From plug-ins, not saved here: ' + i.pluginTerms.map(function (t) { return '<code>' + esc(t) + '</code>'; }).join(' ') + '</p>' : '') +
    '<div class="adm-acts"><button type="button" class="btn small adm-save" disabled>Save</button><span class="adm-msg" role="status"></span></div></div>' + admMeta(i);
}
var ADM_FOLD_AT = 160, ADM_FOLD_SEQ = 0;
function admHelp(text) {
  var long = String(text || '').length > ADM_FOLD_AT;
  return '<p class="adm-help' + (long ? ' adm-fold' : '') + '">' + esc(text) + '</p>' + (long ? '<button type="button" class="adm-more" aria-expanded="false">Show more</button>' : '');
}
function admUnfold(row) {
  var more = row.querySelector('.adm-more');
  if (!more) return;
  var help = more.previousElementSibling;
  if (!help) return;
  ADM_FOLD_SEQ += 1;
  help.id = help.id || 'adm-fold-' + ADM_FOLD_SEQ;
  more.setAttribute('aria-controls', help.id);
  more.addEventListener('click', function () {
    var open = !help.classList.contains('open');
    help.classList.toggle('open', open);
    more.setAttribute('aria-expanded', String(open));
    more.textContent = open ? 'Show less' : 'Show more';
  });
}
var ADM_SEG_MAX = 5, ADM_SEG_LEN = 12, ADM_NAMES = {anthropic: 'Anthropic', openai: 'OpenAI'};
function admChoiceText(i, c) { return (i.choiceLabels || {})[c] || ADM_NAMES[c] || c; }
function admSegOk(i) { return i.kind === 'choice' && Array.isArray(i.choices) && i.choices.length > 1 && i.choices.length <= ADM_SEG_MAX && i.choices.indexOf(i.value) >= 0 && i.choices.every(function (c) { return String(admChoiceText(i, c)).length <= ADM_SEG_LEN; }); }
function admSeg(row, i) {
  var sel = row.querySelector('select.adm-in');
  if (!sel || !admSegOk(i)) return;
  var seg = el('div', 'adm-seg');
  seg.setAttribute('role', 'radiogroup');
  seg.setAttribute('aria-labelledby', adminId(i.key) + '-l');
  var pick = function (b) {
    if (sel.disabled || sel.value === b.dataset.v) return;
    sel.value = b.dataset.v;
    sel.dispatchEvent(new Event('input', {bubbles: true}));
    sel.dispatchEvent(new Event('change', {bubbles: true}));
  };
  i.choices.forEach(function (c) {
    var b = accText('button', 'adm-seg-b', admChoiceText(i, c));
    b.type = 'button';
    b.dataset.v = c;
    b.setAttribute('role', 'radio');
    b.addEventListener('click', function () { pick(b); });
    seg.appendChild(b);
  });
  var paint = function () {
    [].forEach.call(seg.children, function (b) {
      var on = b.dataset.v === sel.value;
      b.setAttribute('aria-checked', on ? 'true' : 'false');
      b.tabIndex = on ? 0 : -1;
      b.disabled = sel.disabled;
    });
  };
  seg.addEventListener('keydown', function (e) {
    var step = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0;
    if (!step) return;
    e.preventDefault();
    var list = [].slice.call(seg.children), at = list.indexOf(document.activeElement), to = list[(at + step + list.length) % list.length];
    if (!to) return;
    pick(to);
    to.focus();
  });
  ['input', 'change'].forEach(function (ev) { sel.addEventListener(ev, paint); });
  sel.hidden = true;
  sel.parentNode.insertBefore(seg, sel);
  paint();
}
function admOrder(i) { return typeof i.order === 'number' ? i.order : 9999; }
function admCards(into, items) {
  var titles = [], by = {};
  items.map(function (i, n) { return {i: i, n: n}; }).sort(function (a, b) { return admOrder(a.i) - admOrder(b.i) || a.n - b.n; }).forEach(function (x) {
    var t = typeof x.i.card === 'string' && x.i.card ? x.i.card : '';
    if (!by[t]) { by[t] = []; titles.push(t); }
    by[t].push(x.i);
  });
  var named = titles.length > 1;
  titles.forEach(function (t) {
    var card = el('section', 'adm-card' + (named ? '' : ' plain'));
    card.dataset.card = t;
    if (named) {
      var h = el('h4', 'adm-card-h');
      h.appendChild(document.createTextNode(t));
      h.appendChild(el('span', 'count', String(by[t].length)));
      card.appendChild(h);
    }
    admFill(card, by[t]);
    into.appendChild(card);
  });
}
function admFill(into, items) {
  var pairs = typeof AI_PAIRS !== 'undefined' ? AI_PAIRS : {}, byKey = {}, mates = {}, run = [];
  items.forEach(function (i) { byKey[i.key] = i; });
  Object.keys(pairs).forEach(function (k) { if (byKey[k] && byKey[pairs[k][0]]) mates[pairs[k][0]] = k; });
  var flush = function () { if (run.length) adminRows(into, run); run = []; };
  items.forEach(function (i) {
    if (mates[i.key]) return;
    var mate = pairs[i.key] && byKey[pairs[i.key][0]];
    if (mate) { flush(); into.appendChild(aiPair(i, mate, pairs[i.key][1])); } else run.push(i);
  });
  flush();
}
function admFilterCards(box, find) {
  if (!box) return;
  box.querySelectorAll('.adm-card').forEach(function (c) { c.hidden = ![].some.call(c.querySelectorAll('.adm-row'), function (r) { return !r.hidden; }); });
  var none = document.getElementById(ADM_NONE_ID), any = [].some.call(box.querySelectorAll('.adm-row'), function (r) { return !r.hidden; });
  if (!none) {
    none = el('p', 'muted adm-none');
    none.id = ADM_NONE_ID;
    none.setAttribute('role', 'status');
    box.appendChild(none);
  }
  none.hidden = !find || any;
  none.textContent = find && !any ? 'No setting matches \u201c' + find + '\u201d.' : '';
}
