function cardsState() {
  var s = {};
  document.querySelectorAll('#anoms article.inc').forEach(function (c) {
    var resp = c.querySelector('details.resp');
    s[c.dataset.key] = {
      resp: resp ? resp.open : null,
      forms: [].filter.call(c.querySelectorAll('.reason'), function (f) { return !f.hidden; }).map(function (f) {
        var i = f.querySelector('input, textarea, select');
        return i ? [i.id.split('-')[0], i.value, i === document.activeElement] : null;
      }).filter(Boolean)
    };
  });
  return s;
}
function restoreCards(s, seen) {
  var fresh = [];
  document.querySelectorAll('#anoms article.inc').forEach(function (c) {
    var x = s[c.dataset.key];
    if (!seen[c.dataset.key]) fresh.push(c);
    if (!x) return;
    var resp = c.querySelector('details.resp');
    if (resp && x.resp != null) resp.open = x.resp;
    x.forms.forEach(function (f) {
      var i = c.querySelector('.reason [id^="' + f[0] + '-"]');
      if (!i) return;
      i.closest('.reason').hidden = false;
      i.value = f[1];
      if (f[2]) i.focus({preventScroll: true});
    });
  });
  return fresh;
}
function initLive() {
  var next = null, reloadDue = false, polling = false, pollAgain = false, retry = 0;
  function json(path) {
    return fetch(path, {headers: {'Accept': 'application/json'}, credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(10000)}).then(function (r) {
      if (!r.ok) throw {code: r.status};
      return r.json().catch(function () { throw {code: 'parse'}; });
    });
  }
  function busy() {
    var f = document.activeElement;
    var typing = !!f && /^(INPUT|TEXTAREA|SELECT)$/.test(f.tagName) && f.id !== 'card-search' && document.hasFocus();
    return typing || !!document.querySelector('[data-confirm]');
  }
  function flash(fresh) {
    var times = document.getElementById('last-check');
    if (times) {
      var u = el('span', 'upd', 'Updated<b>' + esc(hm(D.updatedAt || new Date().toISOString())) + '</b>');
      times.appendChild(u);
      setTimeout(function () { u.remove(); }, 6500);
    }
    fresh.forEach(function (c) { c.classList.add('linked'); });
    setTimeout(function () { fresh.forEach(function (c) { c.classList.remove('linked'); }); }, 4000);
  }
  function apply(state) {
    var y = window.scrollY, seen = {}, cards = cardsState();
    ALL.forEach(function (a) { seen[a.key] = 1; });
    paint(state);
    if (!CH.some(function (c) { return c.id === filter && (c.seg !== 'tri-chips' || c.n); })) filter = 'all';
    setFilter(filter);
    var fresh = restoreCards(cards, seen);
    window.scrollTo(0, y);
    flash(fresh);
  }
  function settle() {
    clearTimeout(retry);
    if (!next && !reloadDue) return;
    if (busy()) { retry = setTimeout(settle, 1000); return; }
    if (reloadDue) { location.reload(); return; }
    var state = next;
    next = null;
    try { apply(state); } catch (e) { location.reload(); }
  }
  async function poll() {
    if (!document || document.hidden) return;
    if (polling) { pollAgain = true; return; }
    polling = true;
    try {
      var now = await json('/api/rev');
      if (typeof now.rev === 'number' && now.rev !== (next ? next.rev : D.rev)) {
        try { next = await json('/api/state'); }
        catch (e) { if (e && [401, 403, 404, 'parse'].indexOf(e.code) >= 0) reloadDue = true; }
      }
    } catch (e) {}
    polling = false;
    settle();
    if (pollAgain) { pollAgain = false; poll(); }
  }
  window.__skopaxPoll = poll;
  setInterval(poll, 15000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
  window.addEventListener('pageshow', function (e) { if (e.persisted) poll(); });
}
