var INV_SEQ = 0;
var INV_POLL_MS = 15000;
var INV_TRIGGER = {slack: 'Slack question', chat: 'Chat message', ask: 'Page question', action: 'Investigate click', cards: 'New cards', assess: 'Deploy assessment'};
var INV_TYPE = {ask: 'Question', dive: 'Investigate click', assess: 'Deploy assessment'};
function invWho(by) { return by ? draftBy(by) : '?'; }
function invFor(iso) {
  var s = Math.max(0, Math.round((Date.now() - new Date(iso)) / 1000));
  if (!(s >= 0)) return '–';
  return s < 60 ? s + ' s' : s < 3600 ? Math.floor(s / 60) + ' min ' + (s % 60) + ' s' : Math.floor(s / 3600) + ' h ' + Math.floor((s % 3600) / 60) + ' min';
}
function invCards(keys) {
  return (keys || []).slice(0, 3).map(function (k) { return '<a href="#a=' + encodeURIComponent(k) + '">' + esc(cardName(k)) + '</a>'; }).join(', ') + ((keys || []).length > 3 ? ' and ' + (keys.length - 3) + ' more' : '');
}
function invWhat(run) {
  var ask = run.ask || {};
  if (ask.private) return '<span class="muted">private chat message</span>';
  if (ask.text) return '<strong>' + esc(ask.text) + '</strong>';
  if (run.report && run.report.summary) return '<strong>' + esc(run.report.summary) + '</strong>';
  return (run.keys || []).length ? invCards(run.keys) : '<span class="muted">no card</span>';
}
function invRunMeta(run) {
  var ask = run.ask || {}, bits = [esc(INV_TRIGGER[run.trigger] || run.trigger || 'run')];
  if (ask.by) bits.push('asked by ' + esc(invWho(ask.by)));
  bits.push('running for ' + esc(invFor(run.startedAt)));
  bits.push(esc(run.toolCalls + (run.toolCalls === 1 ? ' tool call' : ' tool calls')) + (run.lastTool ? ', last ' + esc(run.lastTool) + (run.lastToolAt ? ' ' + esc(invFor(run.lastToolAt)) + ' ago' : '') : ''));
  if (run.model) bits.push(esc(run.model.replace(/^claude-/, '')) + (run.effort ? ' · ' + esc(run.effort) : ''));
  return bits.join(' · ');
}
function invRunLinks(run) {
  var ask = run.ask || {}, links = [];
  if (ask.thread) links.push('<a href="' + esc(ask.thread) + '" target="_blank" rel="noopener">Open in Slack ↗</a>');
  if (ask.text && (run.keys || []).length) links.push('Cards: ' + invCards(run.keys));
  return links.length ? '<p class="inv-links">' + links.join(' · ') + '</p>' : '';
}
function invRoom(r) {
  var run = r.run;
  return '<div class="inv-room' + (run ? ' busy' : '') + '" data-room="' + esc(r.id) + '"><div class="inv-room-h"><b>' + esc(r.name) + '</b>' +
    '<span class="tag ' + (run ? 'k-info' : 'plain') + '">' + (run ? 'Running' : 'Free') + '</span>' + (r.pod ? '<span class="muted inv-pod" title="The pod that holds this room">' + esc(r.pod) + '</span>' : '') + '</div>' +
    (run ? '<p class="inv-what">' + invWhat(run) + '</p><p class="muted inv-meta">' + invRunMeta(run) + '</p>' + invRunLinks(run)
         : '<p class="muted inv-meta">Takes ' + esc(r.takes) + '.</p>') + '</div>';
}
function invWaiting(w, i) {
  var where = w.via === 'mcp' ? 'MCP' : w.source === 'slack' ? 'Slack' : sourceLabel({source: w.source, requestedBy: w.by});
  return '<li class="inv-wait"><span class="inv-n">' + (i + 1) + '</span><div><p class="inv-what"><strong>' + (w.type === 'dive' && (w.cards || []).length ? 'Investigate ' + invCards(w.cards) : esc(w.text || INV_TYPE[w.type] || w.type)) + '</strong></p>' +
    '<p class="muted inv-meta">' + esc(INV_TYPE[w.type] || w.type) + ' · ' + esc(invWho(w.by)) + (where ? ' · ' + esc(where) : '') + ' · waiting ' + esc(invFor(w.at)) + ' · <span class="inv-why">' + esc(w.why) + '</span></p>' +
    ((w.thread || (w.type !== 'dive' && (w.cards || []).length)) ? '<p class="inv-links">' + [w.thread ? '<a href="' + esc(w.thread) + '" target="_blank" rel="noopener">Open in Slack ↗</a>' : '', w.type !== 'dive' && (w.cards || []).length ? 'Cards: ' + invCards(w.cards) : ''].filter(Boolean).join(' · ') + '</p>' : '') +
    '</div></li>';
}
function invRecentRow(r) {
  var ok = r.outcome === 'done', ask = r.ask || {};
  var what = ask.private ? '<span class="muted">private chat message</span>' : ask.text ? esc(ask.text) : (r.keys || []).length ? invCards(r.keys) : '<span class="muted">–</span>';
  var trace = /^https?:\/\//.test(r.traceUrl || '') ? ' <a class="inv-trace" href="' + esc(r.traceUrl) + '" target="_blank" rel="noopener">Open trace \u2197</a>' : '';
  return '<tr><td>' + esc(dayhm(r.startedAt)) + '</td><td>' + esc(INV_TRIGGER[r.trigger] || r.trigger || '') + '</td><td class="inv-rw">' + what + trace + '</td>' +
    '<td><span class="tag ' + (ok ? 'k-ok' : 'k-warn') + '">' + esc(r.outcome || '?') + '</span></td><td class="num">' + esc(String(r.toolCalls || 0)) + '</td><td class="num">' + (r.usd != null ? esc(usdText(r.usd)) : '–') + '</td></tr>';
}
function invMeter(label, used, cap, title) {
  var full = Number(cap) > 0 && Number(used) >= Number(cap);
  return '<div class="inv-lim' + (full ? ' full' : '') + '"' + (title ? ' title="' + esc(title) + '"' : '') + '><span class="inv-lim-l">' + esc(label) + '</span><b>' + esc(String(used)) + '</b><span class="muted"> of ' + esc(String(cap)) + '</span></div>';
}
function invLimits(d) {
  var l = d.limits || {};
  var askers = (l.askers || []).map(function (a) { return esc(invWho(a.by)) + ' ' + a.asks + (l.perPersonCap ? '/' + l.perPersonCap : ''); }).join(' · ');
  return '<div class="inv-lims">' +
    invMeter('Slack questions this hour', l.slackAsks || 0, l.slackAskCap || 0, 'Everyone together; change it on Admin \u203a Settings \u203a Slack') +
    invMeter('Page questions this hour', l.pageAsks || 0, l.pageAskCap || 0) +
    invMeter('Topic checks this hour', l.gateCalls || 0, l.gateCap || 0, 'The small model call that decides whether a Slack message is for ' + brandName()) +
    invMeter('Spend today ($)', Number(l.spentToday || 0).toFixed(2), Number(l.cap || 0).toFixed(2), 'The daily limit; the investigator stops for the day when it is reached') + '</div>' +
    '<p class="muted inv-meta">' + (askers ? 'Asked in Slack this hour: ' + askers + (l.perPersonCap ? ' (each person can ask ' + l.perPersonCap + ' an hour)' : '') : 'Nobody asked in Slack this hour.') + '</p>';
}
function invBanner(d) {
  if (d.halted === 'off') return '<p class="inv-banner k-warn">The AI investigator is turned off in Admin \u203a Features. Nothing new starts until it is turned back on; queued work waits.</p>';
  if (d.halted === 'stopped') return '<p class="inv-banner k-crit">The investigator is stopped. Nothing runs until someone turns it back on.</p>';
  if (d.halted === 'capped') return '<p class="inv-banner k-warn">The investigator reached today\'s spend limit. It starts again tomorrow, or when the limit is raised on Admin \u203a Investigator.</p>';
  if (!d.enabled) return '<p class="inv-banner k-warn">The investigator is off (mode ' + esc(d.mode || 'off') + ').</p>';
  return '';
}
function renderInvestigator(d) {
  var box = document.getElementById('inv');
  if (!box) return;
  var running = (d.rooms || []).filter(function (r) { return r.run; }).length + (d.unplaced || []).length;
  var extra = (d.unplaced || []).map(function (run) { return invRoom({id: 'unplaced-' + run.id, name: 'Other run', pod: String(run.pod || '').split(':')[0], takes: '', run: run}); }).join('');
  box.innerHTML = invBanner(d) +
    '<section class="inv-sec"><h3>Now <span class="count">' + running + ' running</span></h3><div class="inv-rooms">' + (d.rooms || []).map(invRoom).join('') + extra + '</div></section>' +
    '<section class="inv-sec"><h3>Waiting <span class="count">' + (d.waiting || []).length + '</span></h3>' +
      ((d.waiting || []).length ? '<ol class="inv-waits">' + d.waiting.map(invWaiting).join('') + '</ol>' : '<p class="muted">Nothing is waiting.</p>') + '</section>' +
    '<section class="inv-sec"><h3>Limits</h3>' + invLimits(d) + '</section>' +
    '<section class="inv-sec"><h3>Recent runs</h3>' + ((d.recent || []).length ? '<div class="box"><table class="ctable inv-recent"><thead><tr><th>Started</th><th>Kind</th><th>What</th><th>Outcome</th><th class="num">Tool calls</th><th class="num">Cost</th></tr></thead><tbody>' +
      d.recent.map(invRecentRow).join('') + '</tbody></table></div><p class="inv-links"><a href="#t=reqs">All questions</a> · <a href="#t=acts">All runs in History</a> · <a href="#t=costs">Costs</a> · <a href="#t=tools">Tools</a></p>' : '<p class="muted">No runs yet.</p>') + '</section>';
}
function markInvestigatorTab(d) {
  var b = document.getElementById('tab-inv'), count = document.getElementById('count-inv');
  if (!b || !count) return;
  var running = (d.rooms || []).filter(function (r) { return r.run; }).length + (d.unplaced || []).length, waiting = (d.waiting || []).length;
  count.textContent = running || waiting ? running + (waiting ? '+' + waiting : '') : '';
  b.className = 'tab' + (d.halted ? ' k-warn hot' : running || waiting ? ' k-info hot' : '');
  b.title = d.halted === 'off' ? 'the AI investigator is turned off' : d.halted === 'stopped' ? 'the investigator is stopped' : d.halted === 'capped' ? 'the investigator reached today\'s spend limit'
    : running || waiting ? running + ' running, ' + waiting + ' waiting' : 'what the investigator is doing now';
}
function loadInvestigator() {
  var box = document.getElementById('inv');
  if (!box) return;
  var seq = ++INV_SEQ;
  fetch('/api/investigator/status', {credentials: 'same-origin'}).then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
    if (seq !== INV_SEQ) return;
    if (!d || !Array.isArray(d.rooms)) { runTake(null); runShowPaint(); box.innerHTML = '<p class="muted">The investigator status is unavailable right now.</p>'; return; }
    runTake(d);
    runShowPaint();
    renderInvestigator(d);
    markInvestigatorTab(d);
  }, function () {
    if (seq !== INV_SEQ) return;
    runTake(null);
    runShowPaint();
    box.innerHTML = '<p class="muted">The investigator status is unavailable right now.</p>';
  });
}
setInterval(function () { if (!document.hidden) loadInvestigator(); }, INV_POLL_MS);
