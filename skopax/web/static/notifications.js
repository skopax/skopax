var NT_POLL_MS = 30000, NT_PAGE = 20, NT_CAP = 9;
var NT_LABELS = {
  assigned: 'Assigned to you', unassigned: 'No longer assigned to you', progress: 'In progress', resolved: 'Resolved', reopened: 'Reopened', ticket: 'Jira ticket linked',
  severity: 'Severity changed', merged: 'Merged into another card', ungrouped: 'Taken out of its group', muted: 'Muted', known: 'Marked as known noise', unmuted: 'Unmuted',
  auto: 'Auto-resolved', fired: 'Fired again', escalated: 'Got worse', stopped: 'Stopped firing', run: 'Investigation finished', slack: 'Posted to Slack',
  thread: 'Slack thread update', reminder: 'Reminder sent', answer: 'Chat answer ready'
};
var NT_TONE = {
  assigned: 'k-warn', unassigned: 'plain', progress: 'k-info', resolved: 'k-good', reopened: 'k-warn', ticket: 'k-info', severity: 'k-warn', merged: 'plain', ungrouped: 'plain',
  muted: 'k-muted', known: 'k-muted', unmuted: 'plain', auto: 'k-good', fired: 'k-crit', escalated: 'k-crit', stopped: 'k-cleared', run: 'k-act', slack: 'k-act', thread: 'k-act',
  reminder: 'k-warn', answer: 'k-act'
};
var NT = {on: false, ready: false, tab: false, mark: false, items: [], unread: 0, more: false, busy: false};

async function ntApi(method, path, body) {
  var r = await fetch(path, {method: method, credentials: 'same-origin', headers: Object.assign({'Accept': 'application/json'}, body ? {'Content-Type': 'application/json'} : {}), body: body ? JSON.stringify(body) : undefined});
  if (!r.ok) throw {code: r.status};
  return r.json();
}
function ntShow(on) {
  NT.on = on;
  var sec = document.getElementById('nt-sec');
  if (sec) sec.hidden = !on;
  if (!on) ntBadge(0);
}
function ntBadge(n) {
  NT.unread = n;
  var b = document.getElementById('nt-badge');
  if (b) {
    b.firstChild.textContent = n > NT_CAP ? NT_CAP + '+' : String(n);
    b.hidden = !n;
  }
  var all = document.getElementById('nt-read-all');
  if (all) all.disabled = !n;
  if (typeof sideMarkFolds === 'function') sideMarkFolds();
  var tb = document.getElementById('top-bell-badge'), bell = document.getElementById('top-bell');
  if (tb) {
    tb.firstChild.textContent = n > NT_CAP ? NT_CAP + '+' : String(n);
    tb.hidden = !n;
  }
  if (bell) bell.setAttribute('aria-label', n ? 'Notifications, ' + n + ' unread' : 'Notifications');
}
async function ntCount() {
  try {
    var r = await ntApi('GET', '/api/notifications/count');
    var was = NT.on, changed = r.unread !== NT.unread;
    ntShow(true);
    ntBadge(r.unread || 0);
    if (NT.tab && (!was || changed)) ntLoad(false);
  } catch (e) {
    if (e && (e.code === 401 || e.code === 403)) ntShow(false);
  }
}
function ntLink(n) {
  if (n.link) return n.link;
  return n.key ? '#a=' + encodeURIComponent(n.key) : '';
}
function ntItem(n) {
  var li = document.createElement('li');
  var b = document.createElement('button');
  var fresh = !n.read || n.fresh;
  b.type = 'button';
  b.className = 'nt-item' + (fresh ? ' unread' : '');
  b.dataset.id = n.id;
  var what = (n.num ? '#' + n.num + ' ' : '') + (n.title || '');
  b.innerHTML = '<span class="nt-dot" aria-hidden="true"></span><span class="nt-body"><span class="nt-head"><span class="tag ' + (NT_TONE[n.kind] || 'plain') + '">' + esc(NT_LABELS[n.kind] || n.kind) + '</span>' +
    (fresh ? '<span class="sr">Unread</span>' : '') + '<span class="nt-when">' + esc(ago(n.t)) + '</span></span>' +
    (what ? '<span class="nt-what" data-no-tx>' + esc(what) + '</span>' : '') + (n.note ? '<span class="nt-note"' + (n.kind === 'auto' ? '' : ' data-no-tx') + '>' + esc(n.note) + '</span>' : '') +
    '<span class="nt-by" data-no-tx>' + esc(isInvestigatorBy(n.by) ? investigatorName() : n.by || '') + '</span></span>';
  b.addEventListener('click', function () { ntGo(n); });
  li.appendChild(b);
  return li;
}
function ntRender() {
  var list = document.getElementById('nt-list');
  if (!list) return;
  list.innerHTML = '';
  if (!NT.items.length) {
    var empty = document.createElement('li');
    empty.className = 'nt-empty muted';
    empty.textContent = 'Nothing yet. Cards assigned to you and what happens on them show up here.';
    list.appendChild(empty);
  }
  NT.items.forEach(function (n) { list.appendChild(ntItem(n)); });
  document.getElementById('nt-more').hidden = !NT.more;
}
async function ntLoad(more) {
  if (NT.busy) return;
  NT.busy = true;
  try {
    var last = more && NT.items.length ? NT.items[NT.items.length - 1].id : '';
    var r = await ntApi('GET', '/api/notifications?limit=' + NT_PAGE + (last ? '&before=' + encodeURIComponent(last) : ''));
    var fresh = {};
    NT.items.forEach(function (n) { if (n.fresh) fresh[n.id] = true; });
    r.items.forEach(function (n) { n.fresh = !!fresh[n.id] && !!n.read; });
    NT.items = more ? NT.items.concat(r.items) : r.items;
    NT.more = r.items.length === NT_PAGE;
    ntBadge(r.unread || 0);
    ntRender();
    if (NT.mark && NT.tab) {
      NT.mark = false;
      var ids = r.items.filter(function (n) { return !n.read; }).map(function (n) { return n.id; });
      if (ids.length) ntRead(ids, true);
    }
  } catch (e) {
    if (e && (e.code === 401 || e.code === 403)) ntShow(false);
  } finally {
    NT.busy = false;
  }
}
async function ntRead(ids, keep) {
  try {
    var r = await ntApi('POST', '/api/notifications/read', ids ? {ids: ids} : {all: true});
    NT.items.forEach(function (n) {
      if (ids && ids.indexOf(n.id) < 0) return;
      n.fresh = !!keep && (!n.read || !!n.fresh);
      n.read = true;
    });
    ntBadge(r.unread || 0);
    ntRender();
  } catch (e) {
    if (e && (e.code === 401 || e.code === 403)) ntShow(false);
  }
}
async function ntGo(n) {
  var to = ntLink(n);
  if (!n.read) await ntRead([n.id]);
  if (to.charAt(0) === '#') location.hash = to.slice(1);
  else if (to) location.href = to;
}
function ntSignedIn(on) {
  NT.signed = !!on;
  if (!NT.ready) return;
  if (on) ntCount(); else ntShow(false);
}
function ntTab(on) {
  var was = NT.tab;
  NT.tab = on;
  if (!on) {
    NT.mark = false;
    NT.items.forEach(function (n) { n.fresh = false; });
    return;
  }
  if (was) return;
  NT.mark = true;
  if (NT.on) ntLoad(false);
}
(function () {
  var end = document.querySelector('#tab-slack .end'), all = document.getElementById('nt-read-all');
  if (!end || !all) return;
  var badge = el('span', 'b-num');
  badge.id = 'nt-badge';
  badge.hidden = true;
  badge.appendChild(document.createTextNode('0'));
  badge.appendChild(el('span', 'sr')).textContent = ' unread';
  end.appendChild(badge);
  all.addEventListener('click', function () { ntRead(null); });
  document.getElementById('nt-more').addEventListener('click', function () { ntLoad(true); });
  var bell = document.getElementById('top-bell');
  if (bell) {
    bell.innerHTML = navIcon('bell');
    var tb = el('span', 'b-num');
    tb.id = 'top-bell-badge';
    tb.hidden = true;
    tb.appendChild(document.createTextNode('0'));
    tb.appendChild(el('span', 'sr')).textContent = ' unread';
    bell.appendChild(tb);
    bell.addEventListener('click', function () {
      if (NT.signed === false) { window.__skopaxGo('/login?next=' + encodeURIComponent(location.pathname + location.search + location.hash)); return; }
      pickTab('slack');
      var head = document.getElementById('crumb-page');
      if (head) { head.tabIndex = -1; head.focus(); }
    });
  }
  NT.ready = true;
  setInterval(function () { if (!document.hidden && NT.on) ntCount(); }, NT_POLL_MS);
  document.addEventListener('visibilitychange', function () { if (!document.hidden && NT.on) ntCount(); });
  window.__ntCount = ntCount;
})();
