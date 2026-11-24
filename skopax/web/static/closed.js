var CLOSED = {range: '7d', from: '', to: '', q: '', outcome: '', page: 0, size: 50, data: null, loading: false, failed: false, seq: 0, key: null};
var CLOSED_RANGES = [['1d', '24 h', 1], ['7d', '7 days', 7], ['30d', '30 days', 30], ['custom', 'Custom', 0]];
var CLOSED_OUTCOMES = [['', 'Every outcome', ''], ['fixed', 'Fixed', 'k-good'], ['handled', 'No action needed', 'k-good'], ['duplicate', 'Duplicate', 'k-good'], ['auto-resolved', 'Auto-resolved', 'k-good'], ['progress', 'In progress', 'k-act'],
  ['muted', 'Muted', 'k-muted'], ['known', 'Known noise', 'k-muted'], ['cleared', 'Cleared, nobody acted', 'k-cleared'], ['open', 'Expired', 'k-cleared']];
var CLOSED_SEARCH_MS = 350, closedTimer = null;

function closedDayIso(day, end) {
  var d = new Date(day + 'T00:00:00');
  if (isNaN(d)) return '';
  if (end) d = new Date(d.getTime() + 86399000);
  return d.toISOString().replace(/\.\d{3}Z$/, 'Z');
}
function closedQuery() {
  var p = ['page=' + CLOSED.page, 'size=' + CLOSED.size];
  var range = CLOSED_RANGES.filter(function (r) { return r[0] === CLOSED.range; })[0] || CLOSED_RANGES[1];
  if (range[0] === 'custom' && (CLOSED.from || CLOSED.to)) {
    if (CLOSED.from) p.push('from=' + encodeURIComponent(closedDayIso(CLOSED.from, false)));
    if (CLOSED.to) p.push('to=' + encodeURIComponent(closedDayIso(CLOSED.to, true)));
    if (!CLOSED.from) p.push('days=365');
  } else {
    p.push('days=' + (range[2] || 7));
  }
  if (CLOSED.q) p.push('q=' + encodeURIComponent(CLOSED.q));
  if (CLOSED.outcome) p.push('outcome=' + encodeURIComponent(CLOSED.outcome));
  return p.join('&');
}
function closedOutcome(id) {
  return CLOSED_OUTCOMES.filter(function (o) { return o[0] === id; })[0] || [id, id || '?', 'k-cleared'];
}
function loadClosed() {
  var key = closedQuery(), seq = ++CLOSED.seq;
  CLOSED.key = key;
  CLOSED.loading = true;
  paintClosedBody();
  fetch('/api/history/cards?' + key, {credentials: 'same-origin'}).then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
    if (seq !== CLOSED.seq) return;
    CLOSED.loading = false;
    CLOSED.failed = !d || !Array.isArray(d.items);
    if (!CLOSED.failed) CLOSED.data = d;
    paintClosedBody();
    markClosedChip();
  }, function () {
    if (seq !== CLOSED.seq) return;
    CLOSED.loading = false;
    CLOSED.failed = true;
    paintClosedBody();
  });
}
function markClosedChip() {
  var c = CH.filter(function (x) { return x.id === 'closed'; })[0];
  if (!c || !CLOSED.data) return;
  c.n = CLOSED.data.total;
  var b = document.querySelector('#more-filters-box button[data-f="closed"] .count');
  if (b) b.textContent = String(CLOSED.data.total);
  if (filter === 'closed') renderFilterNote(c);
}
function closedRefresh(resetPage) {
  if (resetPage) CLOSED.page = 0;
  loadClosed();
}
function closedToolbar() {
  var bar = el('div', 'cl-bar');
  var ranges = el('div', 'view-toggle cl-range', CLOSED_RANGES.map(function (r) {
    return '<button type="button" data-range="' + r[0] + '" aria-pressed="' + (CLOSED.range === r[0]) + '">' + esc(r[1]) + '</button>';
  }).join(''));
  ranges.setAttribute('role', 'group');
  ranges.setAttribute('aria-label', 'Time range');
  ranges.addEventListener('click', function (e) {
    var b = e.target.closest('button[data-range]');
    if (!b || b.dataset.range === CLOSED.range) return;
    CLOSED.range = b.dataset.range;
    ranges.querySelectorAll('button').forEach(function (x) { x.setAttribute('aria-pressed', String(x === b)); });
    days.hidden = CLOSED.range !== 'custom';
    closedRefresh(true);
  });
  bar.appendChild(ranges);
  var days = el('div', 'cl-days', '<label>From <input type="date" id="cl-from" value="' + esc(CLOSED.from) + '"></label><label>To <input type="date" id="cl-to" value="' + esc(CLOSED.to) + '"></label>');
  days.hidden = CLOSED.range !== 'custom';
  days.addEventListener('change', function () {
    CLOSED.from = days.querySelector('#cl-from').value;
    CLOSED.to = days.querySelector('#cl-to').value;
    closedRefresh(true);
  });
  bar.appendChild(days);
  var search = el('input', 'cl-q');
  search.type = 'search';
  search.id = 'cl-q';
  search.maxLength = 100;
  search.autocomplete = 'off';
  search.spellcheck = false;
  search.value = CLOSED.q;
  search.placeholder = 'Search closed cards: number, service, text, ticket';
  search.setAttribute('aria-label', 'Search closed cards');
  search.addEventListener('input', function () {
    clearTimeout(closedTimer);
    closedTimer = setTimeout(function () { CLOSED.q = search.value.trim(); closedRefresh(true); }, CLOSED_SEARCH_MS);
  });
  bar.appendChild(search);
  var chips = el('div', 'qf-g cl-out');
  chips.setAttribute('role', 'group');
  chips.setAttribute('aria-label', 'Outcome');
  CLOSED_OUTCOMES.forEach(function (o) {
    var b = el('button', 'qf-c', esc(o[1]));
    b.type = 'button';
    b.dataset.outcome = o[0];
    b.setAttribute('aria-pressed', String(CLOSED.outcome === o[0]));
    chips.appendChild(b);
  });
  chips.addEventListener('click', function (e) {
    var b = e.target.closest('button[data-outcome]');
    if (!b || b.dataset.outcome === CLOSED.outcome) return;
    CLOSED.outcome = b.dataset.outcome;
    chips.querySelectorAll('button').forEach(function (x) { x.setAttribute('aria-pressed', String(x === b)); });
    closedRefresh(true);
  });
  bar.appendChild(chips);
  return bar;
}
function closedRow(r) {
  var o = closedOutcome(r.outcome), k = (SEV[r.sev] || SEV.info)[1];
  var row = el('div', 'cl-row ' + k);
  row.dataset.key = r.key || '';
  var num = r.num ? '<a href="#c=' + encodeURIComponent(r.num) + '" title="Opens the card in the side panel">#' + esc(r.num) + '</a>' : '<span class="muted">–</span>';
  var main = '<span class="cl-svc">' + esc(svcName(r)) + '</span><span class="cl-lbl">' + esc(r.label || r.key || '') + (r.sub ? ' · ' + esc(r.sub) : '') + '</span>' +
    (r.note || r.muted ? '<span class="cl-note muted" data-no-tx>' + esc(r.note || r.muted) + '</span>' : '');
  var how = outcomeHtml(r.outcome, r.resolution), res = resolutionOf(r);
  var out = '<span class="tag ' + (how ? 'k-good res-tag' : o[2]) + '">' + (how || esc(o[1])) + '</span>' +
    (r.ticket && !(res && res.ref === r.ticket) ? (jiraHref(r.ticket) ? ' <a class="cl-ticket" href="' + esc(jiraHref(r.ticket)) + '" target="_blank" rel="noopener">' + esc(r.ticket) + '</a>' : ' <span class="cl-ticket">' + esc(r.ticket) + '</span>') : '') +
    (r.owner ? ' <span class="cl-owner muted">' + esc(String(r.owner).split('@')[0]) + '</span>' : '');
  var when = '<span title="' + esc('first seen ' + (r.firstSeen || '–') + ' · last seen ' + (r.lastSeen || '–') + ' (UTC)') + '">' + esc(dayhm(r.firstSeen)) + ' → ' + esc(dayhm(r.lastSeen)) + '</span>';
  row.innerHTML = '<span class="cl-sev" aria-hidden="true"></span><span class="cl-num">' + num + '</span><span class="cl-main">' + main + '</span><span class="cl-res">' + out + '</span><span class="cl-when">' + when + '</span>';
  return row;
}
function paintClosedBody() {
  var body = document.getElementById('cl-body');
  if (!body) return;
  body.innerHTML = '';
  body.classList.toggle('loading', CLOSED.loading);
  var d = CLOSED.data;
  if (CLOSED.failed && !d) { body.appendChild(el('div', 'card empty-card', '<span>Closed cards are unavailable right now.</span>')); return; }
  if (!d) { body.appendChild(el('p', 'muted', 'Loading closed cards…')); return; }
  if (!d.items.length) {
    body.appendChild(el('div', 'card empty-card', '<span class="ok" aria-hidden="true">✓</span><span>' + esc(CLOSED.q || CLOSED.outcome ? 'No closed card matches.' : 'No card closed in this time range.') + '</span>'));
    return;
  }
  var list = el('div', 'cl-list');
  d.items.forEach(function (r) { list.appendChild(closedRow(r)); });
  body.appendChild(list);
  var first = d.page * d.size + 1, last = d.page * d.size + d.items.length;
  var pager = el('div', 'cl-pager', '<button type="button" class="btn sec small" data-page="-1"' + (d.page > 0 ? '' : ' disabled') + '>‹ Newer</button>' +
    '<span class="muted">' + esc(first + '–' + last + ' of ' + d.total) + '</span>' +
    '<button type="button" class="btn sec small" data-page="1"' + (last < d.total ? '' : ' disabled') + '>Older ›</button>');
  pager.addEventListener('click', function (e) {
    var b = e.target.closest('button[data-page]');
    if (!b || b.disabled) return;
    CLOSED.page = Math.max(0, CLOSED.page + Number(b.dataset.page));
    loadClosed();
    var top = document.getElementById('closed-view');
    if (top && top.scrollIntoView) top.scrollIntoView({block: 'start'});
  });
  body.appendChild(pager);
}
function renderClosed(root) {
  var view = el('section', 'grp k-cleared cl-view');
  view.id = 'closed-view';
  var hd = el('header', 'grp-h', '<h3>Closed</h3><span class="wait-why">cards that already left the page: resolved, muted or stopped and dropped off. Each occurrence is kept for a year; the number opens the card in the side panel</span>');
  view.appendChild(hd);
  view.appendChild(closedToolbar());
  view.appendChild(el('div', 'cl-body', ''));
  view.lastChild.id = 'cl-body';
  root.appendChild(view);
  if (CLOSED.key !== closedQuery() || (!CLOSED.data && !CLOSED.loading)) loadClosed();
  else paintClosedBody();
}
