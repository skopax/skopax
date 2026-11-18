var LIVE_KEEP = 200, LIVE_PAGE = 60, LIVE_EVERY = 5000;
function initActivity() {
  var box = document.getElementById('live'), rows = document.getElementById('live-rows'), dot = document.getElementById('live-dot'), hide = document.getElementById('live-hide');
  if (!box || !rows) return;
  var seen = {}, items = [], newest = null, polling = false;
  function later(a, b) { return a.t < b.t ? 1 : a.t > b.t ? -1 : a.id < b.id ? 1 : a.id > b.id ? -1 : 0; }
  function row(it, child, last) {
    var who = it.who === 'pod' ? 'investigator' : it.who, shown = it.who === SYSTEM_ACTOR ? brandName() : who;
    var li = el('li', 'lv ' + esc(it.kind) + (it.error ? ' err' : '') + (child ? ' child' : ''));
    li.innerHTML = '<span class="lt">' + (child ? '<span class="tree" aria-hidden="true">' + (last ? '└' : '├') + '</span>' : esc(hm(it.t))) + '</span>' +
      '<span class="lx">' + (child ? '' : '<span class="lw ' + esc(who) + '">' + esc(shown) + '</span> ') + (it.kind === 'ask' ? linkify(it.text) : esc(it.text)) + shotsHtml({screenshots: it.screenshots, startedAt: it.t}) + '</span>';
    if (it.key) li.title = it.key;
    return li;
  }
  function render() {
    rows.innerHTML = '';
    if (!items.length) { rows.appendChild(el('li', 'muted', 'Nothing yet.')); return; }
    var run = null;
    items.forEach(function (it, i) {
      var child = !!it.runId && it.runId === run, next = items[i + 1];
      rows.appendChild(row(it, child, child && !(next && next.runId === it.runId)));
      run = it.runId || null;
    });
  }
  function take(list) {
    var fresh = list.filter(function (it) {
      if (!it || !it.id || seen[it.id]) return false;
      seen[it.id] = 1;
      return true;
    });
    if (!fresh.length && items.length) return;
    items = fresh.concat(items).sort(later).slice(0, LIVE_KEEP);
    if (items.length) newest = items[0].t;
    render();
  }
  async function poll() {
    if (document.hidden || polling) return;
    polling = true;
    try {
      var r = await fetch('/api/activity?limit=' + LIVE_PAGE + (newest ? '&since=' + encodeURIComponent(newest) : ''), {credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(8000)});
      var list = r.ok ? await r.json() : null;
      if (!Array.isArray(list)) throw list;
      take(list);
      dot.classList.remove('stale');
    } catch (e) {
      dot.classList.add('stale');
      if (!items.length) { rows.innerHTML = ''; rows.appendChild(el('li', 'muted', 'Not available right now.')); }
    }
    polling = false;
  }
  function setOpen(on) {
    box.classList.toggle('is-closed', !on);
    hide.textContent = on ? 'Hide' : 'Activity';
    hide.setAttribute('aria-expanded', on ? 'true' : 'false');
  }
  hide.addEventListener('click', function () { var on = box.classList.contains('is-closed'); setOpen(on); save('skopax-live', on ? '1' : '0'); });
  var pref = load('skopax-live');
  setOpen(pref === '1');
  window.__activityPoll = poll;
  poll();
  setInterval(poll, LIVE_EVERY);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
}
