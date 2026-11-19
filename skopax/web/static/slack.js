var slackActs = null;
var SQ_EMPTY = 'No Slack drafts waiting. The investigator writes one when an anomaly or a deploy report is worth telling the channel.';
var SQ_SENT_EMPTY = 'Nothing posted to Slack in the last 24 hours.';
var SQ_STOPPED = {progress: 'card in progress', handled: 'card marked no action needed'};
function sqReports() { return reports && reports.list ? reports.list() : []; }
function sqTitle(x) { return reports && reports.title ? reports.title(x) : x.summary || 'Deploy report'; }
function sqArmedAt(r) { return r.st.armed ? Date.parse(r.st.armed.at) : 0; }
function sqState(r) {
  var x = r.x, s = x.autoSend || {}, armed = autoArmed(x), why = draftStatus(x, r.kind === 'card');
  if (r.kind === 'card' && done(x)) return {k: 'k-warn', text: 'Stopped: ' + SQ_STOPPED[x.triage] + ', won\u2019t auto-send', tip: why};
  if (armed) return {k: 'k-info', armed: armed, text: autoLabel(armed), tip: why};
  if (s.holdBy) return {k: 'k-muted', text: 'Held by ' + draftBy(s.holdBy) + ' ' + hm(s.holdAt), tip: why};
  if (s.failedAt) return {k: 'k-warn', text: 'Auto-send failed ' + hm(s.failedAt), tip: why};
  if (r.d.notAutoSent) return {k: 'k-warn', text: 'Manual (not auto-sent)', tip: why};
  var by = r.d.by, edited = by && by !== 'Claude' && !isTokenActor(by) && by !== AUTO_BY;
  if (edited) return {k: 'k-muted', text: 'Manual (edited by ' + draftBy(by) + ')', tip: why};
  return {k: 'k-muted', text: r.kind === 'card' && x.sev === 'info' ? 'Manual (info)' : 'Manual', tip: why};
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
function sqSender(by, drafted) { return by === AUTO_BY ? draftBy(by, drafted) : isTokenActor(by) ? tokenActorLabel(by, 'Investigator') + ' (on request)' : !by || by === 'Claude' ? 'Investigator (on request)' : String(by).split('@')[0]; }
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
      '<td><span class="tag ' + (r.p.draft ? 'k-act' : 'plain') + '" title="' + esc(r.p.draft ? 'Investigator\u2019s draft was the message' : 'The standard message built from the card or the report') + '">' + (r.p.draft ? 'draft' : 'plain') + '</span></td>';
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
