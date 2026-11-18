var slackActs = null;
var SQ_EMPTY = 'No Slack drafts waiting. Claude writes one when an anomaly or a deploy report is worth telling the channel.';
var SQ_SENT_EMPTY = 'Nothing posted to Slack in the last 24 hours.';
var SQ_STOPPED = {progress: 'card in progress', handled: 'card marked no action needed'};
var SQ_STOPPED_TIP = 'The auto-send job skips a card that is in progress or marked as no action needed, and disarms it: this draft goes out only with Send now. A draft that sat here unsent was most likely stopped this way.';
var SQ_ARMED_TIP = 'Posts itself to the channel within 30 s of this time unless someone presses Hold. Claude writes every draft, on any card or deploy report, as a message that is about to go out.';
var SQ_HELD_TIP = 'The auto-send was stopped, so this draft waits for Send now.';
var SQ_MANUAL_TIP = 'Claude’s drafts arm the auto-send, which posts them within 30 s; a draft an operator edited, and an info draft saved before 19.10, wait for Send now.';
function sqReports() { return reports && reports.list ? reports.list() : []; }
function sqTitle(x) { return reports && reports.title ? reports.title(x) : x.summary || 'Deploy report'; }
function sqArmedAt(r) { return r.st.armed ? Date.parse(r.st.armed.at) : 0; }
function sqState(r) {
  var x = r.x, s = x.autoSend || {}, armed = autoArmed(x);
  if (r.kind === 'card' && done(x)) return {k: 'k-warn', text: 'Stopped: ' + SQ_STOPPED[x.triage] + ', won’t auto-send', tip: SQ_STOPPED_TIP};
  if (armed) return {k: 'k-info', armed: armed, text: autoLabel(armed), tip: SQ_ARMED_TIP};
  if (s.holdBy) return {k: 'k-muted', text: 'Held by ' + draftBy(s.holdBy) + ' ' + hm(s.holdAt), tip: SQ_HELD_TIP};
  if (s.failedAt) return {k: 'k-warn', text: 'Auto-send failed ' + hm(s.failedAt), tip: 'Slack refused it' + (s.error ? ': ' + s.error : '') + '; it does not retry by itself.'};
  if (r.d.notAutoSent) return {k: 'k-warn', text: 'Manual (not auto-sent)', tip: 'The run that wrote it read ' + r.d.notAutoSent + ', so it waits for a person: read it, then Send now or Discard.'};
  var by = r.d.by, edited = by && by !== 'Claude' && !isTokenActor(by) && by !== AUTO_BY;
  if (edited) return {k: 'k-muted', text: 'Manual (edited by ' + draftBy(by) + ')', tip: SQ_MANUAL_TIP};
  return {k: 'k-muted', text: r.kind === 'card' && x.sev === 'info' ? 'Manual (info)' : 'Manual', tip: SQ_MANUAL_TIP};
}
function sqDrafts() {
  var rows = [];
  A.forEach(function (a) { if (draftOf(a) && !draftSent(a) && (draftLive(a) || done(a))) rows.push({kind: 'card', x: a, d: draftOf(a)}); });
  sqReports().forEach(function (r) { if (draftOf(r) && !draftSent(r)) rows.push({kind: 'report', x: r, d: draftOf(r)}); });
  rows.forEach(function (r) { r.st = sqState(r); });
  return rows.sort(function (x, y) {
    var ax = sqArmedAt(x), ay = sqArmedAt(y);
    if (ax && ay) return ax - ay;
    if (ax || ay) return ay - ax;
    return Date.parse(y.d.at) - Date.parse(x.d.at);
  });
}
function sqSent() {
  var since = Date.now() - 24 * 3600000, out = [];
  ALL.forEach(function (a) { if (a.slackPost && Date.parse(a.slackPost.at) >= since) out.push({kind: 'card', x: a, p: a.slackPost}); });
  sqReports().forEach(function (r) { if (r.slackPost && Date.parse(r.slackPost.at) >= since) out.push({kind: 'report', x: r, p: r.slackPost}); });
  return out.sort(function (x, y) { return Date.parse(y.p.at) - Date.parse(x.p.at); });
}
function sqWhat(r) {
  if (r.kind === 'report') return '<span class="state k-info">Report</span><span class="svc">' + esc(sqTitle(r.x)) + '</span>';
  return state(r.x.sev) + '<span class="svc">' + esc(r.x.svc + ' / ' + r.x.label) + '</span>';
}
function sqSender(by, drafted) { return by === AUTO_BY ? draftBy(by, drafted) : isTokenActor(by) ? tokenActorLabel(by, 'Claude') + ' (on request)' : !by || by === 'Claude' ? 'Claude (on request)' : String(by).split('@')[0]; }
function sqFirstLine(t) { return String(t).split('\n').filter(function (l) { return l.trim(); })[0] || ''; }
function slackTabState() {
  var rows = sqDrafts(), armed = rows.filter(function (r) { return r.st.armed; });
  var tip = armed.length ? armed.length + ' queued · next auto-send ' + hm(armed[0].st.armed.at) : rows.length ? rows.length + ' waiting' : 'no Slack drafts waiting';
  return [rows.length || '–', armed.length ? 'k-info' : 'k-act', !!armed.length, tip];
}
function renderSlackSent() {
  var tb = document.querySelector('#slack-sent tbody');
  if (!tb) return;
  tb.innerHTML = '';
  var rows = sqSent();
  if (!rows.length) tb.appendChild(el('tr', 'empty', '<td colspan="4">' + SQ_SENT_EMPTY + '</td>'));
  rows.forEach(function (r) {
    var tr = el('tr');
    tr.innerHTML = '<td class="when">' + slackLink(r.p.permalink, dayhm(r.p.at)) + '</td><td>' + sqWhat({kind: r.kind, x: r.x}) + '</td><td>' + esc(sqSender(r.p.by, (r.x.slackDraft || {}).by)) + '</td>' +
      '<td><span class="tag ' + (r.p.draft ? 'k-act' : 'plain') + '" title="' + esc(r.p.draft ? 'Claude’s draft was the message' : 'The standard message built from the card or the report') + '">' + (r.p.draft ? 'draft' : 'plain') + '</span></td>';
    tb.appendChild(tr);
  });
}
function renderSlackQ() {
  var tb = document.querySelector('#slackq tbody');
  if (!tb || tb.querySelector('[data-confirm]')) return;
  tb.innerHTML = '';
  var rows = sqDrafts();
  if (!rows.length) tb.appendChild(el('tr', 'empty', '<td colspan="4">' + SQ_EMPTY + '</td>'));
  rows.forEach(function (r) {
    var tr = el('tr');
    tr.innerHTML = '<td>' + sqWhat(r) + '</td>' +
      '<td class="sq-d"><span title="' + esc(r.d.text + '\n\n' + draftBy(r.d.by) + ' · ' + dayhm(r.d.at)) + '">' + esc(sqFirstLine(r.d.text)) + '</span>' + shotsHtml({screenshots: r.d.screenshots, startedAt: r.d.at}, 'mini') + '</td>' +
      '<td><span class="state ' + r.st.k + '" title="' + esc(r.st.tip) + '">' + esc(r.st.text) + '</span></td><td class="sq-c"></td>';
    var acts = el('div', 'sq-acts');
    acts.__row = r;
    tr.querySelector('.sq-c').appendChild(acts);
    tb.appendChild(tr);
  });
  renderSlackSent();
  if (slackActs) slackActs(tb);
}
