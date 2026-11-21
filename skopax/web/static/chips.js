var CHIP_KINDS = {
  'email': {re: /^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/, why: 'Not an e-mail address', hint: 'Type an e-mail address and press Enter or comma'},
  'host': {re: /^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/i, strip: '.', why: 'Not a host name; *.example.com covers only subdomains', hint: 'Type a host name and press Enter or comma'},
  'web-host': {re: /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/i, why: 'Not a host name such as support.google.com; a host covers its subdomains', hint: 'Type a host name and press Enter or comma'},
  'domain': {re: /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i, why: 'Not a domain such as example.com', hint: 'Type a domain and press Enter or comma'},
  'slack-user-id': {re: /^[UW][A-Z0-9]{6,14}$/, why: 'Not a Slack user id such as U012ABCDE', hint: 'Type a Slack user id and press Enter or comma'},
  'client-id': {re: /^[^\s,]{1,200}$/, why: 'A client id has no spaces or commas', hint: 'Type a client id and press Enter or comma'},
  'project-key': {re: /^[A-Za-z][A-Za-z0-9_]{0,49}$/, why: 'A project key starts with a letter and holds letters, digits or underscores, at most 50', hint: 'Type a project key and press Enter or comma'},
  'channel': {re: /^[CG][A-Z0-9]{8,12}$/, why: 'Not a channel id such as C012ABCDEFG', hint: 'Type a channel id and press Enter or comma'},
  'scope': {re: /^[\x21-\x7e]{1,100}$/, why: 'A scope has no spaces', hint: 'Type a scope and press Enter or space'}
};
var CHIP_SPLIT = /[\s,;]+/;
var CHIP_SEQ = 0;
function chipSplit(text) { return String(text || '').split(CHIP_SPLIT).filter(Boolean); }
function chipRule(spec) {
  var kind = CHIP_KINDS[spec.kind] || {};
  if (spec.pattern) {
    try { return {re: new RegExp('^(?:' + spec.pattern + ')$', spec.flags || ''), strip: spec.strip || '', why: spec.why ? spec.why.charAt(0).toUpperCase() + spec.why.slice(1) : kind.why}; } catch (e) {}
  }
  return kind.re ? {re: kind.re, strip: kind.strip || '', why: kind.why} : null;
}
function chipWord(spec, item) { return spec.minus ? String(item).toLowerCase() : item; }
function chipProblem(spec, item) {
  var rule = chipRule(spec);
  if (!rule) return null;
  if ((spec.keywords || []).indexOf(chipWord(spec, item)) >= 0) return null;
  var text = spec.minus && item.charAt(0) === '-' ? item.slice(1) : item;
  while (rule.strip && text.length > 1 && text.charAt(text.length - 1) === rule.strip) text = text.slice(0, -1);
  return rule.re.test(text) ? null : rule.why;
}
function admChipsBad(scope) { return !!(scope && scope.querySelector('.adm-chips[data-invalid="true"]')); }
function admChipsGate(scope) {
  var card = scope && scope.closest ? scope.closest('#acc-mcp, #acc-sso') || scope : scope;
  if (!card || !card.querySelectorAll) return;
  var bad = admChipsBad(card);
  card.querySelectorAll('.acc-mcp-save, .sso-save, .sso-on').forEach(function (b) {
    if (bad) { if (!b.disabled) { b.disabled = true; b.dataset.chipBlock = '1'; } }
    else if (b.dataset.chipBlock) { b.disabled = false; delete b.dataset.chipBlock; }
  });
}
function chipItems(value, spec) {
  var sep = spec.minus ? /[\s,]+/ : spec.separator === ' ' ? /\s+/ : /\s*,\s*/;
  return String(value || '').split(sep).map(function (s) { return s.trim(); }).filter(Boolean);
}
function chipText(items, spec) { return items.join(spec.separator === ' ' ? ' ' : ', '); }
function admChipsMount(value, spec, label) {
  if (!value || !spec || !CHIP_KINDS[spec.kind] || value.dataset.chips) return null;
  var kind = CHIP_KINDS[spec.kind], id = 'chips-' + (++CHIP_SEQ), items = chipItems(value.value, spec), syncing = false;
  value.dataset.chips = id;
  value.hidden = true;
  value.tabIndex = -1;
  value.removeAttribute('aria-labelledby');
  var wrap = document.createElement('div');
  wrap.className = 'adm-chips';
  wrap.setAttribute('role', 'group');
  wrap.setAttribute('aria-label', label || value.getAttribute('aria-label') || 'List');
  var list = document.createElement('ul'), entry = document.createElement('input'), why = document.createElement('ul'), words = document.createElement('div');
  list.className = 'adm-chip-list';
  list.setAttribute('role', 'list');
  entry.className = 'adm-chip-in';
  entry.id = (value.id || id) + '-chip';
  entry.type = 'text';
  entry.setAttribute('aria-label', label || 'Add an item');
  entry.setAttribute('placeholder', kind.hint);
  entry.setAttribute('autocomplete', 'off');
  entry.setAttribute('autocapitalize', 'off');
  entry.setAttribute('spellcheck', 'false');
  why.className = 'adm-chip-why';
  why.setAttribute('role', 'status');
  words.className = 'adm-chip-kw';
  var held = function () { return !!value.disabled; };
  var bad = function () { return items.filter(function (x) { return chipProblem(spec, x); }); };
  var push = function () {
    syncing = true;
    value.value = chipText(items, spec);
    value.dispatchEvent(new Event('input', {bubbles: true}));
    value.dispatchEvent(new Event('change', {bubbles: true}));
    syncing = false;
    wrap.dataset.invalid = bad().length ? 'true' : 'false';
    admChipsGate(wrap);
  };
  var focusAt = function (n) {
    var chips = list.querySelectorAll('.adm-chip');
    if (chips[n]) chips[n].focus(); else entry.focus();
  };
  var remove = function (n, focus) {
    if (held()) return;
    items.splice(n, 1);
    paint();
    push();
    if (focus) focusAt(Math.min(n, items.length - 1));
  };
  var add = function (parts) {
    if (held()) return;
    var words = spec.keywords || [], before = items.join('\n');
    parts.forEach(function (p) {
      if (words.indexOf(chipWord(spec, p)) >= 0) { items = [p]; return; }
      items = items.filter(function (x) { return words.indexOf(chipWord(spec, x)) < 0; });
      if (items.map(function (x) { return x.toLowerCase(); }).indexOf(p.toLowerCase()) < 0) items.push(p);
    });
    if (items.join('\n') === before) return;
    paint();
    push();
  };
  var paint = function () {
    list.textContent = '';
    why.textContent = '';
    items.forEach(function (x, n) {
      var problem = chipProblem(spec, x), li = document.createElement('li'), t = document.createElement('span'), b = document.createElement('button');
      li.className = 'adm-chip' + (problem ? ' bad' : '');
      li.tabIndex = 0;
      li.id = id + '-c' + n;
      t.className = 'adm-chip-t';
      t.id = id + '-t' + n;
      t.textContent = x;
      t.setAttribute('data-no-tx', '');
      b.type = 'button';
      b.className = 'adm-chip-x';
      b.id = id + '-x' + n;
      b.tabIndex = -1;
      b.textContent = '\u00d7';
      b.setAttribute('aria-label', 'Remove');
      b.setAttribute('aria-labelledby', b.id + ' ' + t.id);
      b.title = 'Remove';
      if (problem) { li.setAttribute('aria-invalid', 'true'); li.title = problem; }
      b.addEventListener('click', function () { remove(n, true); });
      li.addEventListener('keydown', function (e) {
        if (e.key === 'Backspace' || e.key === 'Delete') { e.preventDefault(); remove(n, true); }
        else if (e.key === 'ArrowLeft') { e.preventDefault(); focusAt(Math.max(0, n - 1)); }
        else if (e.key === 'ArrowRight') { e.preventDefault(); if (n + 1 < items.length) focusAt(n + 1); else entry.focus(); }
      });
      li.appendChild(t);
      li.appendChild(b);
      list.appendChild(li);
      if (problem) {
        var line = document.createElement('li'), code = document.createElement('code');
        code.textContent = x;
        code.setAttribute('data-no-tx', '');
        line.appendChild(code);
        line.appendChild(document.createTextNode(' '));
        line.appendChild(document.createTextNode(problem));
        why.appendChild(line);
      }
    });
    wrap.dataset.invalid = bad().length ? 'true' : 'false';
    admChipsGate(wrap);
    [].forEach.call(words.querySelectorAll('button'), function (b) { b.disabled = held() || items.indexOf(b.dataset.word) >= 0; });
  };
  var commit = function (all) {
    var text = entry.value, parts = chipSplit(text), rest = !all && parts.length && !/[\s,;]$/.test(text) ? parts.pop() : '';
    entry.value = rest;
    add(parts);
  };
  entry.addEventListener('input', function (e) {
    e.stopPropagation();
    var text = entry.value, sep = spec.separator === ' ' ? /[\s,;]/ : /[,;\n]/;
    if (sep.test(text)) commit(false);
  });
  entry.addEventListener('change', function (e) { e.stopPropagation(); });
  entry.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); commit(true); }
    else if (e.key === ',' || e.key === ';') { e.preventDefault(); commit(true); }
    else if (e.key === ' ' && spec.separator === ' ') { e.preventDefault(); commit(true); }
    else if (e.key === 'Backspace' && !entry.value && items.length) { e.preventDefault(); remove(items.length - 1, false); }
    else if (e.key === 'ArrowLeft' && !entry.value && entry.selectionStart === 0 && items.length) { e.preventDefault(); focusAt(items.length - 1); }
  });
  entry.addEventListener('paste', function (e) {
    var data = e.clipboardData && e.clipboardData.getData ? e.clipboardData.getData('text') : '';
    if (!data || !CHIP_SPLIT.test(data.trim())) return;
    e.preventDefault();
    add(chipSplit(data));
  });
  entry.addEventListener('blur', function () { if (entry.value.trim()) commit(true); });
  value.addEventListener('input', function () {
    if (syncing) return;
    items = chipItems(value.value, spec);
    paint();
  });
  (spec.keywords || []).forEach(function (w) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'btn sec small adm-chip-word';
    b.dataset.word = w;
    b.textContent = '+ ' + w;
    b.setAttribute('aria-label', 'Add ' + w);
    b.addEventListener('click', function () { add([w]); });
    words.appendChild(b);
  });
  wrap.appendChild(list);
  wrap.appendChild(entry);
  wrap.addEventListener('click', function (e) { if (e.target === wrap || e.target === list) entry.focus(); });
  value.parentNode.insertBefore(wrap, value);
  if ((spec.keywords || []).length) value.parentNode.insertBefore(words, value);
  value.parentNode.insertBefore(why, value);
  paint();
  return wrap;
}
