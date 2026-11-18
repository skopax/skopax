function ticketSuggest(input, opts) {
  opts = opts || {};
  var MIN = 2, WAIT_MS = 250, seq = 0, timer = null, items = [], at = -1;
  var wrap = document.createElement('span');
  wrap.className = 'tsg';
  input.parentNode.insertBefore(wrap, input);
  wrap.appendChild(input);
  var list = document.createElement('ul');
  list.className = 'tsg-list';
  list.setAttribute('role', 'listbox');
  list.id = input.id + '-suggest';
  list.hidden = true;
  wrap.appendChild(list);
  input.setAttribute('role', 'combobox');
  input.setAttribute('aria-autocomplete', 'list');
  input.setAttribute('aria-controls', list.id);
  input.setAttribute('aria-expanded', 'false');
  input.setAttribute('autocomplete', 'off');
  function clean(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); }
  function close() { list.hidden = true; input.setAttribute('aria-expanded', 'false'); at = -1; }
  function mark() {
    [].forEach.call(list.children, function (li, i) { li.setAttribute('aria-selected', i === at ? 'true' : 'false'); });
    if (at >= 0 && list.children[at]) input.setAttribute('aria-activedescendant', list.children[at].id); else input.removeAttribute('aria-activedescendant');
  }
  function pick(i) {
    var it = items[i];
    if (!it) return;
    input.value = it.key || it.name;
    close();
    if (opts.picked) opts.picked(it);
  }
  function show(found, note) {
    items = found;
    list.innerHTML = found.length ? found.map(function (it, i) {
      return '<li role="option" id="' + list.id + '-' + i + '" data-i="' + i + '"><b data-no-tx>' + clean(it.key || it.name) + '</b> <span class="tsg-s" data-no-tx>' + clean(it.summary || it.email || '') + '</span>' +
        '<span class="tsg-m" data-no-tx>' + clean([it.status, it.developer].filter(Boolean).join(' \u00b7 ')) + '</span></li>';
    }).join('') : '<li class="tsg-none">' + clean(note || opts.none || 'No open ticket matches') + '</li>';
    list.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    at = -1;
    mark();
  }
  async function look(q, n) {
    try {
      var r = await fetch((opts.url || '/api/jira/search') + '?q=' + encodeURIComponent(q) + (opts.params ? opts.params() : ''), {headers: {'Accept': 'application/json'}, credentials: 'same-origin'});
      if (n !== seq) return;
      if (r.status === 503) { show([], 'Jira is not set up on this service'); return; }
      if (!r.ok) { show([], opts.fail || 'Jira search failed; type the key'); return; }
      show(await r.json());
    } catch (e) {
      if (n === seq) show([], opts.fail || 'Jira search failed; type the key');
    }
  }
  input.addEventListener('input', function () {
    clearTimeout(timer);
    var q = input.value.trim(), n = ++seq;
    if (q.length < MIN) { close(); return; }
    timer = setTimeout(function () { look(q, n); }, WAIT_MS);
  });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { if (!list.hidden) { e.preventDefault(); close(); } return; }
    if (list.hidden || !items.length) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); at = (at + 1) % items.length; mark(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); at = (at - 1 + items.length) % items.length; mark(); }
    else if (e.key === 'Enter' && at >= 0) { e.preventDefault(); pick(at); }
  });
  list.addEventListener('mousedown', function (e) {
    var li = e.target.closest('li[data-i]');
    if (!li) return;
    e.preventDefault();
    pick(+li.dataset.i);
  });
  input.addEventListener('blur', function () { setTimeout(close, 150); });
  return {close: close};
}
