var REQ_SEQ = 0;
var REQ_OPEN = {};
var REQ_POLL_MS = 30000;
var REQ_STATE = {requested: ['Waiting', 'k-act'], processing: ['Running', 'k-info'], done: ['Answered', 'k-ok'], failed: ['Failed', 'k-crit'], cancelled: ['Cancelled', 'k-muted']};
function reqState(r) {
  if (r.status === 'done' && r.run && r.run.outcome && r.run.outcome !== 'done') return ['Not answered · ' + r.run.outcome, 'k-warn'];
  return REQ_STATE[r.status] || [r.status || '?', 'k-muted'];
}
function reqWhere(r) { return sourceLabel(r); }
function reqWho(by) { return String(by || '?').replace(/@.*$/, ''); }
function reqMeta(r) {
  var bits = [esc(reqWho(r.requestedBy)), esc(reqWhere(r)), esc(dayhm(r.requestedAt))];
  if (r.run && r.run.usd != null) bits.push(esc(usdText(r.run.usd)));
  if (r.run && r.run.toolCalls) bits.push(esc(r.run.toolCalls + (r.run.toolCalls === 1 ? ' tool call' : ' tool calls')));
  return bits.join(' · ');
}
function reqAnswer(r) {
  var answer = (r.run && r.run.recap) || r.result || '';
  return answer ? '<div class="review-body req-ans">' + reviewBody(answer) + '</div>' : '<p class="muted req-ans">' + (r.status === 'requested' ? 'Waiting for the investigator; it starts within about two minutes.' : r.status === 'processing' ? 'The investigator is on it.' : 'No answer was written.') + '</p>';
}
function reqLinks(r) {
  var links = [];
  if (r.slack && r.slack.permalink) links.push('<a href="' + esc(r.slack.permalink) + '" target="_blank" rel="noopener">Open in Slack ↗</a>');
  (r.cards || []).forEach(function (k) { links.push('<a href="#a=' + encodeURIComponent(k) + '">Card: ' + esc(k) + '</a>'); });
  return links.length ? '<p class="req-links">' + links.join(' · ') + '</p>' : '';
}
function reqThread(r) {
  var s = r.slack || {};
  return r.source === 'slack' && s.channel && s.threadTs ? 'thread:' + s.channel + ':' + s.threadTs : r.id;
}
function reqGroupKey(r) { return r.cards && r.cards[0] ? 'card:' + r.cards[0] : reqThread(r); }
function reqGroups(rows) {
  var by = {}, order = [];
  rows.forEach(function (r) {
    var k = reqGroupKey(r);
    if (!by[k]) { by[k] = []; order.push(k); }
    by[k].push(r);
  });
  return order.map(function (k) { return {key: k, rows: by[k].slice().reverse()}; });
}
function reqLive(r) { return r.status === 'requested' || r.status === 'processing'; }
function reqSummary(head, st, meta, extra) {
  return '<summary class="chev"><span class="tag ' + st[1] + '">' + esc(st[0]) + '</span><span class="req-main"><strong class="req-q" title="' + esc(head.text || '') + '">' + esc(head.text || '') + '</strong>' +
    '<span class="muted req-meta">' + meta + '</span></span>' + (extra || '') + '</summary>';
}
function reqGroupHtml(g) {
  var rows = g.rows, first = rows[0], last = rows[rows.length - 1];
  var live = rows.filter(reqLive), open = REQ_OPEN[g.key] != null ? REQ_OPEN[g.key] : !!live.length;
  if (rows.length === 1) return '<details class="box review req" data-id="' + esc(g.key) + '"' + (open ? ' open' : '') + '>' + reqSummary(first, reqState(first), reqMeta(first)) + reqAnswer(first) + reqLinks(first) + '</details>';
  var st = live.length ? reqState(live[live.length - 1]) : reqState(last);
  var who = rows.map(function (r) { return reqWho(r.requestedBy); }).filter(function (w, i, a) { return a.indexOf(w) === i; });
  var card = g.key.indexOf('card:') === 0 ? g.key.slice(5) : '';
  var places = rows.map(reqWhere).filter(function (w, i, a) { return a.indexOf(w) === i; });
  var meta = esc(who.join(', ')) + ' · ' + esc(places.join(', ')) + ' · ' + esc(daytime(first.requestedAt)) + ' → ' + esc(daytime(last.requestedAt));
  var items = rows.map(function (r) {
    return '<div class="req-item"><p class="req-iq"><span class="tag ' + reqState(r)[1] + '">' + esc(reqState(r)[0]) + '</span> <strong>' + esc(r.text || '') + '</strong> <span class="muted">' + reqMeta(r) + '</span></p>' + reqAnswer(r) + reqLinks(r) + '</div>';
  }).join('');
  return '<details class="box review req req-thread" data-id="' + esc(g.key) + '"' + (open ? ' open' : '') + '>' +
    reqSummary(card ? {text: cardName(card)} : first, st, meta, '<span class="tag plain req-n">' + rows.length + (card ? ' about this card' : ' in thread') + '</span>') +
    (card ? '<p class="req-links req-card"><a href="#a=' + encodeURIComponent(card) + '">Open the card</a></p>' : '') + items + '</details>';
}
function renderRequests(rows) {
  var box = document.getElementById('reqs');
  if (!box) return;
  if (!rows.length) {
    box.innerHTML = '<p class="muted">No requests yet. Tag @' + esc(brandName()) + ' with a question in any Slack channel it is in, or use Ask the investigator.</p>';
    return;
  }
  box.innerHTML = reqGroups(rows).map(reqGroupHtml).join('');
}
function markRequestsTab(rows) {
  var b = document.getElementById('tab-reqs'), count = document.getElementById('count-reqs');
  if (!b || !count) return;
  var live = rows.filter(function (r) { return r.status === 'requested' || r.status === 'processing'; }).length;
  count.textContent = live ? String(live) : '';
  b.className = 'tab' + (live ? ' k-act hot' : '');
  b.title = live ? live + (live === 1 ? ' request is' : ' requests are') + ' waiting or running' : 'questions people asked ' + brandName();
}
function loadRequests() {
  var box = document.getElementById('reqs');
  if (!box) return;
  var seq = ++REQ_SEQ;
  fetch('/api/requests', {credentials: 'same-origin'}).then(function (r) { return r.ok ? r.json() : null; }).then(function (rows) {
    if (seq !== REQ_SEQ) return;
    if (!Array.isArray(rows)) { box.innerHTML = '<p class="muted">Requests are unavailable right now.</p>'; return; }
    renderRequests(rows);
    markRequestsTab(rows);
  }, function () {
    if (seq !== REQ_SEQ) return;
    box.innerHTML = '<p class="muted">Requests are unavailable right now.</p>';
  });
}
document.getElementById('reqs').addEventListener('toggle', function (e) {
  var d = e.target;
  if (d && d.dataset && d.dataset.id) REQ_OPEN[d.dataset.id] = d.open;
}, true);
document.getElementById('req-ask').addEventListener('click', function (e) {
  if (e.currentTarget.getAttribute('aria-disabled') !== 'true') askDialog();
});
setInterval(function () { if (!document.hidden) loadRequests(); }, REQ_POLL_MS);
