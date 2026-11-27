(function () {
  var box = document.getElementById('sem-find'), input = document.getElementById('sem-q'), pop = document.getElementById('sem-pop'), list = document.getElementById('sem-list');
  var noteEl = document.getElementById('sem-note'), countEl = document.getElementById('sem-count');
  if (!box || !input || !pop || !list || !noteEl || !countEl) return;
  var GROUPS = [['cards', 'Cards', true], ['runs', 'Runs', true], ['changes', 'Changes', true], ['pages', 'Pages', false], ['errors', 'Errors', false]];
  var LINK = /^(?:#(?:c|a|r|rel)=[^\s]{1,300}|#t=[a-z]{1,20}|\/api\/investigations\/[A-Za-z0-9_.~%-]{1,100})$/;
  var DELAY = 300, MIN = 3, LIMIT = 12;
  var seq = 0, timer = null, rows = [], at = -1, enabled = false;

  var WEB = /^https?:\/\/[^\s<>"\\]{1,500}$/i;
  function cleanLink(link, kind) {
    if (typeof link !== 'string') return '';
    return LINK.test(link) || (kind === 'pages' && WEB.test(link)) ? link : '';
  }
  function linkless(it) { return it.kind === 'errors' && !it.link; }
  function setOpen(on) {
    pop.hidden = !on;
    input.setAttribute('aria-expanded', on ? 'true' : 'false');
    if (!on) input.removeAttribute('aria-activedescendant');
  }
  function close() {
    clearTimeout(timer);
    seq++;
    setOpen(false);
    rows = [];
    at = -1;
  }
  function say(text) { countEl.textContent = text; }
  function plural(n) { return n === 0 ? tx('No results') : tx(n + (n === 1 ? ' result' : ' results')); }
  function select(i, scroll) {
    if (!rows.length) { at = -1; input.removeAttribute('aria-activedescendant'); return; }
    at = (i + rows.length) % rows.length;
    rows.forEach(function (r, k) { r.setAttribute('aria-selected', String(k === at)); });
    input.setAttribute('aria-activedescendant', rows[at].id);
    if (scroll && rows[at].scrollIntoView) rows[at].scrollIntoView({block: 'nearest'});
  }
  function note(d) {
    noteEl.textContent = '';
    noteEl.hidden = !d.note;
    if (!d.note) return;
    var said = el('span');
    said.setAttribute('data-no-tx', '');
    said.textContent = d.note;
    noteEl.appendChild(said);
    if (d.semantic === false) noteEl.appendChild(el('span', '', ' Showing word matches.'));
  }
  function row(it, n) {
    var plain = !it.link, a = el(plain ? 'div' : 'a', 'sem-o');
    a.id = 'sem-o-' + n;
    a.setAttribute('role', 'option');
    a.setAttribute('aria-selected', 'false');
    if (!plain) a.setAttribute('href', it.link);
    if (!plain && it.link.charAt(0) !== '#') { a.target = '_blank'; a.rel = 'noopener noreferrer'; }
    var t = el('span', 'sem-t');
    t.setAttribute('data-no-tx', '');
    var b = el('b');
    b.textContent = it.title;
    t.appendChild(b);
    var sub = [it.sub, it.when ? dayhm(it.when) : ''].filter(Boolean).join(' \u00b7 ');
    if (sub) { var s = el('small'); s.textContent = sub; t.appendChild(s); }
    a.appendChild(t);
    var meaning = it.match === 'meaning', chip = el('span', 'sem-chip', meaning ? 'meaning' : 'words');
    chip.title = meaning ? 'Found by meaning' : 'Found by matching words';
    a.appendChild(chip);
    if (!plain) a.addEventListener('click', function () { close(); });
    return a;
  }
  function groupName(label) { return el('div', 'sem-g', label); }
  function paint(d) {
    var items = (Array.isArray(d.items) ? d.items : []).filter(function (it) { return it && typeof it.title === 'string' && it.title && typeof it.kind === 'string' && (linkless(it) || cleanLink(it.link, it.kind)); });
    note(d);
    list.textContent = '';
    rows = [];
    if (!items.length) {
      list.appendChild(el('div', 'sem-empty', 'Nothing matches.'));
      say(plural(0));
      setOpen(true);
      select(-1);
      return;
    }
    GROUPS.forEach(function (g, gi) {
      var found = items.filter(function (it) { return it.kind === g[0]; });
      if (!found.length && !g[2]) return;
      var wrap = el('div', 'sem-grp'), head = groupName(g[1]);
      head.id = 'sem-g-' + g[0];
      head.setAttribute('role', 'presentation');
      wrap.setAttribute('role', 'group');
      wrap.setAttribute('aria-labelledby', head.id);
      wrap.appendChild(head);
      if (!found.length) wrap.appendChild(el('div', 'sem-none', 'None'));
      found.forEach(function (it) {
        var r = row(it, rows.length);
        rows.push(r);
        wrap.appendChild(r);
      });
      list.appendChild(wrap);
    });
    say(plural(items.length));
    setOpen(true);
    select(0, false);
  }
  function problem(text) {
    note({});
    list.textContent = '';
    rows = [];
    list.appendChild(el('div', 'sem-empty', text));
    say(text);
    setOpen(true);
    select(-1);
  }
  function run(text) {
    var mine = ++seq;
    fetch('/api/search?q=' + encodeURIComponent(text) + '&limit=' + LIMIT, {headers: {'Accept': 'application/json'}, credentials: 'same-origin'}).then(function (r) {
      if (mine !== seq) return null;
      if (r.status === 429) { problem('Too many searches, wait a minute and try again.'); return null; }
      if (!r.ok) { problem('Search is not available right now.'); return null; }
      return r.json();
    }).then(function (d) {
      if (d && mine === seq) paint(d);
    }, function () {
      if (mine === seq) problem('Search is not available right now.');
    });
  }
  function typed() {
    clearTimeout(timer);
    var text = input.value.trim();
    if (text.length < MIN) { close(); say(''); return; }
    timer = setTimeout(function () { run(text); }, DELAY);
  }
  function show(on) {
    enabled = !!on;
    box.hidden = !enabled;
    if (!enabled) { close(); input.value = ''; say(''); }
  }
  function editable(node) {
    return !!node && (node.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(node.tagName));
  }
  function modalOpen() {
    return [].some.call(document.querySelectorAll('[aria-modal="true"], dialog[open], .modal-wrap'), function (d) { return !d.closest('[hidden]'); });
  }

  input.addEventListener('input', typed);
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      if (pop.hidden) return;
      e.preventDefault();
      e.stopPropagation();
      close();
      return;
    }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (!rows.length) return;
      e.preventDefault();
      if (pop.hidden) { setOpen(true); select(at < 0 ? 0 : at, true); return; }
      select(at + (e.key === 'ArrowDown' ? 1 : -1), true);
      return;
    }
    if (e.key === 'Enter') {
      e.preventDefault();
      clearTimeout(timer);
      if (!pop.hidden && rows[at]) { rows[at].click(); return; }
      if (input.value.trim().length >= MIN) run(input.value.trim());
    }
  });
  input.addEventListener('focus', function () { if (rows.length && pop.hidden) setOpen(true); });
  list.addEventListener('mousemove', function (e) {
    var o = e.target.closest ? e.target.closest('[role="option"]') : null;
    if (o && rows.indexOf(o) !== at) select(rows.indexOf(o), false);
  });
  box.addEventListener('click', function (e) { if (e.target === box) input.focus(); });
  document.addEventListener('click', function (e) { if (!pop.hidden && !box.contains(e.target)) close(); });
  box.addEventListener('focusout', function (e) {
    if (!pop.hidden && e.relatedTarget && !box.contains(e.relatedTarget)) close();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== '/' || e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented || e.isComposing || !enabled || window.getComputedStyle(box).display === 'none') return;
    if (editable(document.activeElement) || modalOpen()) return;
    var palette = document.getElementById('pal');
    if (palette && !palette.hidden) return;
    e.preventDefault();
    input.focus();
    input.select();
  });

  var inst = window.instRender;
  window.instRender = function (me) {
    if (typeof inst === 'function') inst(me);
    show(!!me && !!me.features && me.features['semantic-search'] === true);
  };
  window.semEnabled = function () { return enabled; };
  window.semOpen = function (text) {
    if (!enabled) return;
    input.value = String(text || '').slice(0, 200);
    input.focus();
    if (input.value.trim().length >= MIN) { clearTimeout(timer); run(input.value.trim()); }
  };
})();
