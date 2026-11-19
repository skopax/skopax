var OPENK = {};
var SEEN_END = {handled: 'resolved', fixed: 'resolved', duplicate: 'resolved', 'auto-resolved': 'auto-resolved', progress: 'In progress', muted: 'muted', known: 'marked known noise', cleared: 'cleared, nobody acted', open: 'still open'};
var CARD_RUNS = {}, RUNS_TTL = 60000, RUNS_LIMIT = 10, SHOT_KEEP_DAYS = 30, SHOT_SEEN = {};
var RUN_TRIGGER = {cards: 'Investigate (auto)', action: 'Investigate', slack: 'Slack ask', ask: 'Page ask', assess: 'Report assessment'};
var CARD_ASK = {}, ASK_TEXT = {}, ASK_POLL_MS = 15000, ASK_WAIT_MS = 600000;
var ASK_IN_TIP = 'One question about this card: it opens a private chat with the card attached and this question as the first message, answered from the connected integrations, such as prod logs, metrics, code and tickets.';
var ASK_GO_TIP = 'Opens a private chat about this card with this question; it counts toward your chat messages per day';

function people(a) {
  var P = {}, order = [];
  function add(name, w, ref) {
    if (!P[name]) { P[name] = {name: name, w: 0, refs: []}; order.push(name); }
    P[name].w += w;
    if (!P[name].refs.some(function (r) { return r.url === ref.url && r.what === ref.what; })) P[name].refs.push(ref);
  }
  if (a.owner) {
    var tk = ticketKey(a.ticket);
    add(ownerName(a.owner), 1e9, {what: 'assignee' + asgHow(a, ', '), title: tk || brandName(), date: '', url: tk ? jiraHref(tk) : ''});
  }
  var row = S.filter(function (s) { return s.svc === a.svc && s.ns === a.ns && s.deployedAt && s.imageBy; })[0];
  if (row && a.firstSeen) {
    var gap = Math.round((new Date(a.firstSeen) - new Date(row.deployedAt)) / 60000);
    if (gap >= -5 && gap <= 180) (row.imageBy.names || []).forEach(function (n) {
      add(n, 1000, {what: 'deployed ' + row.imageBy.sha + ' ' + Math.max(gap, 0) + ' min before it started', title: row.imageBy.title, date: row.imageBy.date, url: row.imageBy.url});
    });
  }
  codeOf(a).forEach(function (c) {
    var r = String(c.lines || '').split('-'), total = r[1] ? (+r[1] - +r[0] + 1) : 1;
    (c.by || []).forEach(function (b) {
      add(b.name, b.lines, {what: 'last changed ' + c.label + (total > 1 ? ' (' + b.lines + ' of ' + total + ' lines)' : ''), title: b.title, date: b.date, url: b.url});
    });
  });
  return order.map(function (n) { return P[n]; }).sort(function (x, y) { return y.w - x.w; }).slice(0, 3);
}
function codeOf(a) { return (a.code || []).filter(function (c) { return String(c.url).startsWith('https://'); }); }
function isFix(c) { return typeof c.current === 'string' && typeof c.suggestion === 'string' && c.suggestion !== ''; }
function fixesOf(a) { return codeOf(a).filter(isFix); }
function diffRows(a, b) {
  var n = a.length, m = b.length, L = [], i, j, out = [];
  for (i = n; i >= 0; i--) {
    L[i] = [];
    for (j = m; j >= 0; j--) L[i][j] = i === n || j === m ? 0 : a[i] === b[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  }
  i = 0; j = 0;
  while (i < n || j < m) {
    if (i < n && j < m && a[i] === b[j]) { out.push({k: ' ', o: i, n: j, t: a[i]}); i++; j++; }
    else if (j < m && (i === n || L[i][j + 1] > L[i + 1][j])) { out.push({k: '+', n: j, t: b[j]}); j++; }
    else { out.push({k: '-', o: i, t: a[i]}); i++; }
  }
  return out;
}
function fixHtml(c, j) {
  var r = String(c.lines || '').split('-'), first = parseInt(r[0], 10) || 1, span = r[1] ? (+r[1] - +r[0] + 1) : 1;
  var rows = diffRows(c.current.split('\n'), c.suggestion.split('\n')).map(function (r) {
    return '<tr class="' + (r.k === '-' ? 'del' : r.k === '+' ? 'add' : 'ctx') + '"><td class="ln">' + (r.o == null ? '' : first + r.o) + '</td><td class="ln">' + (r.n == null ? '' : first + r.n) + '</td>' +
      '<td class="mk">' + (r.k === ' ' ? '' : r.k) + '</td><td class="cd">' + esc(r.t) + '</td></tr>';
  }).join('');
  var by = (c.by || []).map(function (b) {
    var t = String(b.url).startsWith('https://') ? '<a href="' + esc(b.url) + '" target="_blank" rel="noopener">' + esc(b.title) + ' ↗</a>' : esc(b.title);
    return '<li><span class="fix-l">Last changed by</span> <b>' + esc(b.name) + '</b> · ' + esc(b.lines + ' of ' + span + (span === 1 ? ' line' : ' lines')) + ' · ' + t + ' <span class="d">' + esc(b.date) + '</span></li>';
  }).join('');
  var at = (c.repo || '?') + ' · ' + (c.path || c.label) + ':' + c.lines + ' @ ' + (c.sha || 'master');
  return '<section class="fix"><div class="fix-h"><h5>Suggested fix (untested)</h5>' +
    '<button type="button" class="btn sec small fix-copy" data-fix="' + j + '" title="Copy the suggested lines; it is untested, verify before you use it">Copy</button></div>' +
    '<p class="fix-at"><a href="' + esc(c.url) + '" target="_blank" rel="noopener" title="These lines in GitLab at the commit they were read at">' + esc(at) + ' ↗</a></p>' +
    (by ? '<ul class="fix-by">' + by + '</ul>' : '') + (c.note ? '<p class="fix-what">' + esc(c.note) + '</p>' : '') +
    '<div class="diff" tabindex="0" role="region" aria-label="' + esc('Current lines and the suggested fix for ' + at) + '"><table><tbody>' + rows + '</tbody></table></div></section>';
}
document.addEventListener('click', function (e) {
  var b = e.target.closest && e.target.closest('#anoms .fix-copy');
  if (!b) return;
  var key = b.closest('article.inc').dataset.key, a = ALL.filter(function (x) { return x.key === key; })[0], c = a && fixesOf(a)[+b.dataset.fix];
  function shown(t) { b.textContent = t; setTimeout(function () { b.textContent = 'Copy'; }, 2000); }
  if (!c || !navigator.clipboard) { shown('Select the text to copy'); return; }
  navigator.clipboard.writeText(c.suggestion).then(function () { shown('Copied'); }, function () { shown('Select the text to copy'); });
});
function draftOf(a) { return a.slackDraft && a.slackDraft.text ? a.slackDraft : null; }
function draftSent(a) { var d = draftOf(a), p = a.slackPost; return !!(d && p && Date.parse(p.at) >= Date.parse(d.at)); }
function draftLive(a) { return open.indexOf(a) >= 0 || stale.indexOf(a) >= 0; }
function updateText(a) {
  var t = [];
  if (a.triage === 'progress') t.push('In progress' + (a.ticket ? ': ' + a.ticket : '') + '.');
  else if (a.triage === 'handled') t.push('Checked, no action needed.');
  if (a.status === 'cleared' && a.clearedAt) t.push(hm(a.clearedAt) + (eventCause(a) ? ' it stopped after the event.' : ' it stopped firing.'));
  return t.join(' ') || 'Update: ';
}
var AUTO_BY = 'auto · Claude draft';
function autoSentBy(drafted) { var t = String(drafted == null ? '' : drafted), label = isTokenActor(t) && t !== 'token:bootstrap' ? tokenActorLabel(t, '') : ''; return (t === 'server' ? brandName() : label || 'Investigator') + ' \u00b7 auto-sent'; }
function draftBy(by, drafted) { return isTokenActor(by) ? tokenActorLabel(by, 'Investigator') : !by || by === 'Claude' ? 'Investigator' : by === AUTO_BY ? autoSentBy(drafted) : String(by).split('@')[0]; }
function autoArmed(x) {
  var s = x.autoSend, d = draftOf(x);
  return s && s.at && !s.holdBy && !s.claimedAt && d && s.draftAt === d.at && !draftSent(x) ? s : null;
}
function autoLabel(s) { return Date.parse(s.at) <= Date.now() ? 'Auto-send now' : 'Auto-send ' + hm(s.at); }
function draftStatus(x, card) {
  var d = draftOf(x);
  if (!d || draftSent(x)) return '';
  var s = x.autoSend || {}, off = ME_FEATURES['slack-autosend'] === false;
  if (s.claimedAt) return 'Sending now';
  if (card !== false && x.key && (done(x) || !draftLive(x))) {
    if (x.triage === 'progress') return 'Not sent: the card went In progress, so it waits for a person';
    if (!s.holdBy && !s.failedAt) return autoArmed(x) ? 'Not sent: the card stopped, was muted, merged or resolved before the timer' : 'Not sent: the card stopped, was muted, merged or resolved, so it waits for a person';
  }
  if (s.holdBy) return 'Held by ' + draftBy(s.holdBy) + ' at ' + hm(s.holdAt);
  if (s.failedAt) return 'Slack refused the post' + (s.error ? ': ' + s.error : '');
  if (d.notAutoSent) return 'Waiting for a person: the run read ' + d.notAutoSent;
  if (off) return 'Auto-send is off in Admin';
  var armed = autoArmed(x);
  if (armed) return 'Sends by itself at ' + hm(armed.at) + ' unless someone holds it';
  return 'Waiting for a person: send it with the button';
}
function linkify(t) {
  return String(t).split(/(https:\/\/[^\s<>"']+)/).map(function (p, j) {
    if (!(j % 2)) return esc(p);
    var url = p, tail = '';
    while (/[.,:;!?)]$/.test(url) && (!url.endsWith(')') || url.split(')').length > url.split('(').length)) { tail = url.slice(-1) + tail; url = url.slice(0, -1); }
    return '<a href="' + esc(url) + '" target="_blank" rel="noopener">' + esc(url) + '</a>' + esc(tail);
  }).join('');
}
var ICON_SEND = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>';
function statusTip(x, card) { var t = x ? draftStatus(x, card) : ''; return t ? '\n' + t : ''; }
function draftTip(d, x) { return d.text + '\n\n' + draftBy(d.by) + ' \u00b7 ' + dayhm(d.at) + ' \u00b7 full text and Edit in the expanded card' + statusTip(x); }
function armedTip(d, s, x) { return d.text + '\n\n' + draftBy(d.by) + ' \u00b7 ' + dayhm(d.at) + ' \u00b7 posts itself to Slack within 30 s of ' + hm(s.at) + ' unless someone presses Hold; full text and Edit in the expanded card' + statusTip(x); }
var ICON_LINK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>';
function copyUrl(url, b) {
  function shown(text) { b.innerHTML = ICON_LINK + esc(text); setTimeout(function () { b.innerHTML = ICON_LINK + (b.classList.contains('icon') ? '<span class="sr">Copy link</span>' : 'Copy link'); }, 2000); }
  if (!navigator.clipboard) { history.replaceState(null, '', url); shown('Link is in the address bar'); return; }
  navigator.clipboard.writeText(url).then(function () { shown('Copied'); }, function () { history.replaceState(null, '', url); shown('Link is in the address bar'); });
}
function pageLink(a) {
  if (!String(a.url || '').startsWith('https://')) return '';
  return '<a class="tag open-page" href="' + esc(a.url) + '" target="_blank" rel="noopener" title="' + esc('Open ' + a.url) + '">' + ICON_LINK + 'Open page<span aria-hidden="true">↗</span></a>';
}
function teamsTag(posts) {
  var named = function (r) { return TAG_NAMES[r] || r; };
  var last = posts[posts.length - 1], routes = posts.map(function (p) { return named(p.route); }).filter(function (x, i, l) { return l.indexOf(x) === i; });
  var tip = posts.map(function (p) { return dayhm(p.at) + ' · ' + named(p.route) + ' · ' + draftBy(p.by); }).join('\n');
  return '<span class="tag k-good teams-sent" title="' + esc('Posted to Teams:\n' + tip) + '">' + ICON_SEND + 'Sent to Teams · ' + esc(routes.join(', ')) + ' ' + esc(dayhm(last.at)) + '</span>';
}
function goneText(a) { return 'The page answers HTTP ' + a.gone.status + ' since ' + dayhm(a.gone.since) + ': if it was taken down on purpose, Resolve this card.'; }
function draftTag(a) {
  var d = draftOf(a), p = a.slackPost;
  if (!d) return '';
  if (draftSent(a)) return '<span class="tag k-good slack-rec sent" title="' + esc('Posted to Slack ' + dayhm(p.at) + ' by ' + draftBy(p.by, d.by) + '\n\n' + d.text) + '">' + ICON_SEND + slackLink(p.permalink, 'Sent to Slack ' + dayhm(p.at)) + '</span>';
  if (!draftLive(a)) return '<span class="tag k-muted slack-rec stopped" title="' + esc((a.triage === 'progress' ? 'This card is in progress, so the draft is probably out of date;' : 'This card is handled, muted or cleared, so the draft is probably out of date;') + ' it is not counted under Slack drafts, and the auto-send is off. Send to Slack would still post it.' + statusTip(a) + '\n\n' + d.text) + '">' + ICON_SEND + 'Draft (stopped)</span>';
  var armed = autoArmed(a);
  if (armed) return '<span class="tag k-act slack-rec armed" title="' + esc(armedTip(d, armed, a)) + '">' + ICON_SEND + esc(autoLabel(armed)) + '</span>';
  return '<span class="tag k-act slack-rec" title="' + esc(draftTip(d, a)) + '">' + ICON_SEND + 'Slack recommended</span>';
}
function draftHtml(a, i) {
  var d = draftOf(a);
  if (!d) return '';
  return '<section class="sdraft"><div class="sdraft-h"><h5>Slack draft<span class="h5n sdraft-by">' + esc(draftBy(d.by)) + ' · ' + esc(dayhm(d.at)) + '</span></h5>' +
    '<button type="button" class="btn sec small draft-copy" title="Copy the draft text, e.g. to send it to one person instead of the channel">Copy</button>' +
    '<button type="button" class="btn sec small op-btn draft-edit" data-tip="Edit the message Send to Slack posts" title="Edit the message Send to Slack posts">Edit</button></div>' +
    '<p class="sdraft-t">' + linkify(d.text) + '</p>' + (draftStatus(a) ? '<p class="sdraft-status muted">' + esc(draftStatus(a)) + '</p>' : '') + shotsHtml({screenshots: d.screenshots, startedAt: d.at}) +
    '<div class="reason draft-form" hidden><textarea id="draft-' + i + '" maxlength="1500" rows="4" aria-label="Slack draft for ' + esc(a.svc + ' ' + a.label) + '" placeholder="Empty and Save removes the draft">' + esc(d.text) + '</textarea>' +
    '<button type="button" class="btn small op-btn draft-save">Save</button><button type="button" class="btn sec small draft-cancel">Cancel</button></div></section>';
}
function numLink(a) {
  if (!a.num) return '';
  return '<a class="tag num-link" href="/c/' + esc(a.num) + '" target="_blank" rel="noopener" title="' + esc('Card #' + a.num + ': opens this card alone in a new tab, the page to send to people outside the team') + '">#' + esc(a.num) + '<span aria-hidden="true"> ↗</span></a>';
}
var ASG_NOTE = {};
function asgHow(a, lead) {
  var how = a.assigned ? 'set by ' + draftBy(a.assigned.by) + ' ' + dayhm(a.assigned.at) : String(a.owner || '').indexOf('@') > 0 ? 'took the card' : a.ticket ? 'from the Jira ticket' : '';
  return how ? lead + how : '';
}
function asgTag(a) {
  return a.owner ? '<span class="tag plain asg-tag" title="' + esc('Assigned to ' + a.owner + asgHow(a, ' · ')) + '">@' + esc(ownerName(a.owner)) + '</span>' : '';
}
function bizLead(b) { return b && b.kind === 'info' ? 'Info' : 'Recommended'; }
function bizTags(a) {
  return (a.tags || []).map(function (t) {
    var tip = tagName(t.id) + ' · tagged by ' + tagBy(t) + ' ' + dayhm(t.at) + (TECH_TAGS[t.id] ? '\n\nTo fix this card, ' + TECH_TAGS[t.id] : a.business && a.business.text ? '\n\n' + (a.business.why ? 'Why: ' + a.business.why + '\n' : '') + bizLead(a.business) + ': ' + a.business.text : '\n\nNo action written yet, so nothing goes to Teams');
    return '<span class="tag biz-tag tcol t-' + esc(t.id) + (TECH_TAGS[t.id] ? ' tech-tag' : '') + '" title="' + esc(tip) + '">' + esc(tagName(t.id)) + '</span>';
  }).join('');
}
function bizHtml(a, i) {
  var b = a.business || {}, on = tagIds(a).filter(function (id) { return !TECH_TAGS[id]; }), off = a.tagsOff || [];
  var btns = Object.keys(TAG_NAMES).map(function (id) {
    var t = (a.tags || []).filter(function (x) { return x.id === id; })[0];
    var tip = t ? tagName(id) + ': tagged by ' + tagBy(t) + ' ' + dayhm(t.at) + '. Click to remove; the rules and the investigator then leave it off'
      : (TECH_TAGS[id] ? 'Tag as ' + tagName(id) + ': ' + TECH_TAGS[id] + '. The card goes in full, once, to a Teams channel whose route takes tag:' + id + '; it needs no sentence'
        : 'Tag for ' + tagName(id) + ': the card goes to its Teams channel once, when it has a business sentence') + (off.indexOf(id) >= 0 ? ' (someone removed this tag earlier)' : '');
    return '<button type="button" class="btn sec small op-btn biz-tb" data-tag="' + esc(id) + '" aria-pressed="' + (t ? 'true' : 'false') + '" data-tip="' + esc(tip) + '" title="' + esc(tip) + '">' + esc(tagName(id)) + '</button>';
  }).join('');
  var editTip = 'Write why it matters (Why, shown first), then what you recommend the business team does (Recommended) or what they only need to know (Info): plain language, no technical detail';
  return '<section class="biz"><div class="biz-h"><h5>Tags' + (b.text ? '<span class="h5n biz-by">' + esc(tagBy(b)) + ' · ' + esc(dayhm(b.at)) + '</span>' : '') + '</h5>' +
    '<span class="biz-tbs">' + btns + '</span><button type="button" class="btn sec small op-btn biz-edit" data-tip="' + esc(editTip) + '" title="' + esc(editTip) + '">' + (b.text ? 'Edit' : 'Write') + '</button></div>' +
    '<p class="biz-why"' + (b.text && b.why ? '' : ' hidden') + '><b>Why:</b> ' + esc(b.why || '') + '</p>' +
    '<p class="biz-txt' + (b.text ? '' : ' muted') + '">' + (b.text ? '<b>' + bizLead(b) + ':</b> ' + esc(b.text) : on.length ? 'No action written yet: nothing goes to Teams until one is written.' : 'Not tagged for a business team.') + '</p>' +
    '<div class="reason biz-form" hidden><textarea id="biz-' + i + '" maxlength="500" rows="2" aria-label="What we recommend the business team does, for ' + esc(a.svc + ' ' + a.label) + '" placeholder="Recommended: a recommendation, e.g. we recommend changing the product page title back to Garden Chairs. Empty and Save removes it.">' + esc(b.text || '') + '</textarea>' +
    '<input type="text" class="biz-why-in" maxlength="300" aria-label="Why, in business terms" placeholder="Why, shown first: what users see or lose" value="' + esc(b.why || '') + '">' +
    '<select class="biz-kind" aria-label="Action or information" title="Recommended: we recommend the team changes something itself. Info: the team only needs to know, engineering fixes it."><option value="action"' + (b.kind === 'info' ? '' : ' selected') + '>Recommended: they act</option><option value="info"' + (b.kind === 'info' ? ' selected' : '') + '>Info: they only need to know</option></select>' +
    '<button type="button" class="btn small op-btn biz-save">Save</button><button type="button" class="btn sec small biz-cancel">Cancel</button></div></section>';
}
function seenHtml(a) {
  var s = a.seenBefore;
  if (!s) return '';
  var parts = [];
  if (s.count) {
    var r = outcomeHtml(s.outcome, s.resolution) && resolutionOf(s);
    var t = 'Seen before: ' + (s.count === 1 ? 'once' : s.count + ' times') + (s.lastAt ? ', last ' + daytime(s.lastAt) : '') + (s.outcome ? ', ' + (r ? 'resolved as' : SEEN_END[s.outcome] || s.outcome) : '');
    var ticket = s.ticket && !(r && r.ref === s.ticket) ? ' · ticket ' + jiraLinks(s.ticket) : '';
    parts.push(r ? '<span class="seen-how"><span>' + esc(t) + '</span> ' + resolutionHtml(r) + ticket + '</span>' : '<span>' + esc(t) + ticket + '</span>');
  }
  if (s.slack && s.slack.line && s.slack.at) parts.push('<span>' + esc('Asked in Slack ' + daytime(s.slack.at) + ': ' + s.slack.line) + '</span>');
  if (!parts.length) return '';
  return '<p class="seen-line" title="Earlier occurrences of this card, from the card history (kept for a year); the investigator reads them with past_cards before a deep dive">' + parts.join('') + '</p>';
}
function runLine(r) { return String(r.recap || '').split('\n').map(function (x) { return x.trim(); }).filter(Boolean)[0] || ''; }
function askRun(key) {
  var p = CARD_ASK[key], c = CARD_RUNS[key];
  if (!p || !c) return null;
  return (c.rows || []).filter(function (r) { return r.actionId === p.id; })[0] || null;
}
function askWaitHtml(key) {
  var p = CARD_ASK[key], i = window.INV_INFO || {};
  if (!p || askRun(key)) return '';
  var st = p.ended ? p.ended.status : p.expired ? 'expired' : 'waiting';
  var note = st === 'done' ? p.ended.result : st === 'failed' ? 'The ask failed: ' + (p.ended.result || 'see History') : st === 'cancelled' ? 'Cancelled before the investigator took it.'
    : st === 'expired' ? 'No answer after 10 minutes. The investigator may be stopped or busy; open the card again later to look.'
    : i.stopped ? 'Asked by you · the investigator is stopped and picks it up once resumed'
    : i.cap && i.spentToday >= i.cap ? 'Asked by you · the investigator reached today’s ' + usdText(i.cap) + ' cap and picks it up after midnight (' + (SERVER_TZ || 'server time') + ')'
    : 'Asked by you · the investigator picks it up within about 2 minutes';
  var tag = {done: ['k-info', 'small talk'], failed: ['k-crit', 'failed'], cancelled: ['k-muted', 'cancelled'], expired: ['k-muted', 'no answer yet'], waiting: ['k-info', 'waiting']}[st];
  return '<li class="ask-wait' + (st === 'done' ? ' talk' : '') + '"><span class="rt">' + esc(dayhm(p.at)) + '</span><b>Page ask</b><span class="tag ' + tag[0] + '">' + tag[1] + '</span>' +
    '<span class="rl">' + esc(p.text) + '</span><span class="ra">' + esc(note) + '</span></li>';
}
function askStatus(list) {
  Object.keys(CARD_ASK).forEach(function (key) {
    var p = CARD_ASK[key];
    var x = p.ended ? null : (list || []).filter(function (a) { return a.id === p.id; })[0];
    if (!x || (x.status !== 'failed' && x.status !== 'cancelled') || askRun(key)) return;
    p.ended = {status: x.status, result: x.result || ''};
    clearTimeout(p.timer);
    paintRuns(key);
  });
}
function shotSize(s, long) {
  return s.viewport + ' ' + s.width + ' × ' + s.height + (s.cut ? (long ? ', full page cut at ' + s.height + ' px' : ', cut') : '');
}
function shotBlocked(b) {
  var n = b ? (b.otherHosts || 0) + (b.api || 0) + (b.tracking || 0) : 0;
  return n ? n + ' request' + (n === 1 ? '' : 's') + ' blocked' : '';
}
function shotTip(s) {
  return [s.title || s.finalUrl || s.url, dayhmZone(s.at), shotSize(s), s.status ? 'status ' + s.status : '', shotBlocked(s.blocked), 'click to zoom'].filter(Boolean).join(' · ');
}
function shotMeta(s) {
  var b = s.blocked || {}, parts = [];
  if (b.otherHosts) parts.push(b.otherHosts + ' other host' + (b.otherHosts === 1 ? '' : 's'));
  if (b.api) parts.push(b.api + ' API call' + (b.api === 1 ? '' : 's'));
  if (b.tracking) parts.push(b.tracking + ' tracking');
  return [s.finalUrl || s.url, s.width ? shotSize(s, true) : '', s.status ? 'status ' + s.status : '', parts.length ? 'blocked: ' + parts.join(', ') : ''].filter(Boolean).join(' · ');
}
function shotById(id) { return SHOT_SEEN[id] || null; }
function cardShots(a) {
  var list = (a.screenshots || []).map(function (s) { return typeof s === 'string' ? {id: s} : s; }).filter(function (s) { return s && s.id; });
  var at = list.map(function (s) { return s.at || ''; }).sort().pop() || a.lastSeen || a.firstSeen;
  return list.length ? '<div class="card-shots">' + shotsHtml({screenshots: list, startedAt: at}) + '</div>' : '';
}
function shotsHtml(r, cls) {
  var list = (r.screenshots || []).filter(function (s) { return s && s.id; });
  if (!list.length) return '';
  if (r.startedAt && Date.now() - new Date(r.startedAt).getTime() > SHOT_KEEP_DAYS * 86400000) {
    return '<span class="shots gone">' + list.length + ' screenshot' + (list.length === 1 ? '' : 's') + ', expired after ' + SHOT_KEEP_DAYS + ' days</span>';
  }
  return '<span class="shots' + (cls ? ' ' + cls : '') + '">' + list.map(function (s) {
    SHOT_SEEN[s.id] = s;
    var src = '/api/screenshots/' + encodeURIComponent(s.id), tip = shotTip(s);
    return '<a class="shot' + (s.viewport === 'phone' ? ' phone' : '') + '" href="' + src + '" target="_blank" rel="noopener" data-shot="' + esc(s.id) + '" data-tip="' + esc(tip) + '" title="' + esc(tip) + '">' +
      '<img src="' + src + '" alt="' + esc('Screenshot of ' + (s.finalUrl || s.url) + ', ' + s.viewport) + '" loading="lazy"></a>';
  }).join('') + '</span>';
}
function runsBody(key) {
  var c = CARD_RUNS[key], wait = askWaitHtml(key);
  if (!c || !c.at) return '<p class="muted">Loading runs…</p>';
  if (c.failed && !wait) return '<p class="muted">Runs unavailable.</p>';
  if (!c.rows.length && !wait) return '<p class="muted">No investigator run on this card yet.</p>';
  return '<ul class="runs">' + wait + c.rows.map(function (r) {
    var thread = r.ask && slackThread(r.ask.thread) ? '<a class="thread" href="' + esc(r.ask.thread) + '" target="_blank" rel="noopener" title="The Slack thread this was asked in">Slack thread ↗</a>' : '';
    var page = r.trigger === 'ask';
    var line = page && r.ask ? String(r.ask.by || 'an operator').split('@')[0] + ': ' + r.ask.text : runLine(r);
    var answer = !page ? '' : '<span class="ra">' + (r.recap ? linkify(r.recap).replace(/\n/g, '<br>') : r.outcome === 'running' ? 'Working on it…' : '') + '</span>';
    return '<li' + (page ? ' class="page-ask"' : '') + '><span class="rt">' + esc(dayhm(r.startedAt)) + '</span><b>' + esc((RUN_TRIGGER[r.trigger] || 'Run') + (r.mode === 'shadow' ? ' · shadow' : '')) + '</b>' +
      '<span class="tag ' + (POD_OUT[r.outcome] || 'k-muted') + '">' + esc(r.outcome || '?') + '</span><span class="rl">' + esc(line) + '</span><span class="rlinks">' + thread +
      '<a class="record" href="/api/investigations/' + encodeURIComponent(r.id) + '" target="_blank" rel="noopener" data-json="Run record" title="The full run record: tool calls, writes, usage">Run record</a></span>' + answer + shotsHtml(r) + '</li>';
  }).join('') + '</ul>';
}
function runsCount(key) { var c = CARD_RUNS[key]; return c && c.at && !c.failed ? String(c.rows.length) : ''; }
function askBoxHtml(a, i) {
  var info = window.INV_INFO;
  if (!info || !info.mode || info.mode === 'off') return '';
  var s = ASK_TEXT[a.key] || {};
  return '<div class="reason card-ask"><input type="text" id="cask-' + i + '" class="op-in" maxlength="' + ASK_MAX + '" placeholder="Ask about this card" aria-label="' + esc('Ask the investigator about ' + a.svc + ' ' + a.label) + '" data-tip="' + esc(ASK_IN_TIP) + '" title="' + esc(ASK_IN_TIP) + '" value="' + esc(s.text || '') + '">' +
    '<button type="button" class="btn small op-btn ask-go" data-tip="' + esc(ASK_GO_TIP) + '" title="' + esc(ASK_GO_TIP) + '"' + (s.sending ? ' disabled' : '') + '>' + (s.sending ? 'Sending…' : 'Ask') + '</button>' +
    '<p class="cask-err" role="alert"' + (s.err ? '' : ' hidden') + '>' + esc(s.err || '') + '</p></div>';
}
function runsHtml(a, i) {
  return '<section class="card-runs"><h5>Runs on this card<span class="h5n">' + esc(runsCount(a.key)) + '</span></h5><div class="runs-body">' + runsBody(a.key) + '</div>' + askBoxHtml(a, i) + '</section>';
}
function askBoxes(key) {
  return [].filter.call(document.querySelectorAll('#anoms article.inc'), function (c) { return c.dataset.key === key; }).map(function (c) { return c.querySelector('.card-ask'); }).filter(Boolean);
}
function askShow(key) {
  var s = ASK_TEXT[key] || {};
  askBoxes(key).forEach(function (box) {
    var go = box.querySelector('.ask-go'), err = box.querySelector('.cask-err');
    box.querySelector('input').value = s.text || '';
    go.disabled = !!s.sending;
    go.textContent = s.sending ? 'Sending…' : 'Ask';
    err.textContent = s.err || '';
    err.hidden = !s.err;
  });
}
function askSettle(key) {
  var p = CARD_ASK[key], r = askRun(key);
  if (!p || !r || r.outcome === 'running') return;
  clearTimeout(p.timer);
  delete CARD_ASK[key];
  loadInvestigations();
}
function askPoll(key) {
  var p = CARD_ASK[key];
  function tick() {
    if (CARD_ASK[key] !== p || p.ended) return;
    if (Date.now() >= p.until) { p.expired = true; paintRuns(key); return; }
    loadRuns(key, true);
    if (window.__skopaxActions) window.__skopaxActions();
    p.timer = setTimeout(tick, ASK_POLL_MS);
  }
  p.timer = setTimeout(tick, ASK_POLL_MS);
}
function chatAskError(e) {
  if (e && e.code === 401) return 'Log in to ask.';
  if (e && e.code === 403) return 'Asking needs the operator role.';
  return e && e.detail && [409, 413, 422, 429].indexOf(e.code) >= 0 ? e.detail : 'Not sent, try again.';
}
function askSend(card) {
  var key = card.dataset.key, box = card.querySelector('.card-ask');
  var go = box && box.querySelector('.ask-go');
  if (!go || go.disabled || go.getAttribute('aria-disabled') === 'true') return;
  var s = ASK_TEXT[key] || (ASK_TEXT[key] = {});
  s.text = box.querySelector('input').value;
  var q = s.text.trim();
  if (!q) { s.err = 'Type the question first.'; askShow(key); box.querySelector('input').focus(); return; }
  s.err = '';
  s.sending = true;
  askShow(key);
  window.__skopaxApi('POST', '/api/chats', {text: q, cards: [key]}).then(function (x) {
    delete ASK_TEXT[key];
    askShow(key);
    if (x && x.id) location.hash = '#t=chat/thread/' + encodeURIComponent(x.id);
  }, function (e) {
    s.sending = false;
    s.err = chatAskError(e);
    askShow(key);
  });
}
document.addEventListener('click', function (e) {
  var go = e.target.closest && e.target.closest('#anoms .card-ask .ask-go');
  if (go) askSend(go.closest('article.inc'));
});
document.addEventListener('keydown', function (e) {
  var i = e.key === 'Enter' && !e.isComposing && e.target.closest && e.target.closest('#anoms .card-ask input');
  if (!i) return;
  e.preventDefault();
  askSend(i.closest('article.inc'));
});
document.addEventListener('input', function (e) {
  var i = e.target.closest && e.target.closest('#anoms .card-ask input');
  if (!i) return;
  var key = i.closest('article.inc').dataset.key, s = ASK_TEXT[key] || (ASK_TEXT[key] = {});
  s.text = i.value;
  if (s.err) { s.err = ''; askShow(key); }
});
function paintRuns(key) {
  var root = document && document.getElementById('anoms');
  if (!root) return;
  root.querySelectorAll('article.inc').forEach(function (card) {
    if (card.dataset.key !== key) return;
    var box = card.querySelector('.card-runs');
    if (!box) return;
    box.querySelector('.h5n').textContent = runsCount(key);
    box.querySelector('.runs-body').innerHTML = runsBody(key);
  });
}
function loadRuns(key, force) {
  var c = CARD_RUNS[key];
  if (c && (c.loading || (!force && c.at && !c.failed && Date.now() - c.at < RUNS_TTL))) return;
  CARD_RUNS[key] = Object.assign({rows: []}, c || {}, {loading: true});
  function done(rows, failed) { CARD_RUNS[key] = {rows: rows, at: Date.now(), failed: failed}; askSettle(key); paintRuns(key); }
  fetch('/api/investigations?key=' + encodeURIComponent(key) + '&limit=' + RUNS_LIMIT, {credentials: 'same-origin'}).then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); }).then(function (rows) {
    done((Array.isArray(rows) ? rows : []).filter(function (r) { return (r.keys || []).indexOf(key) >= 0; }), false);
  }, function () { done([], true); });
}
function body(a, recShown, i) {
  var what = (a.what ? '<p class="what">' + linkify(a.what) + '</p>' : '') + cardShots(a);
  var sus = (a.suspects || []).length ? '<section><h5>Suspects</h5><ol>' + a.suspects.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ol></section>' : '';
  var rec = a.recommend && !recShown ? '<section class="rec"><h5>Recommendation</h5><p>' + esc(a.recommend) + '</p></section>' : '';
  var code = codeOf(a).filter(function (c) { return !isFix(c); });
  var codeHtml = fixesOf(a).map(fixHtml).join('') + (code.length ? '<section><h5>Code</h5><ul class="code-refs">' + code.map(function (c) {
    return '<li><a href="' + esc(c.url) + '" target="_blank" rel="noopener" title="' + esc(c.repo + ' @ ' + (c.sha || 'master')) + '">' + esc(c.label) + ' ↗</a><span><span class="cn">' + esc(c.note) + '</span> <span class="repo">' + esc(c.repo) + '</span></span></li>';
  }).join('') + '</ul></section>' : '');
  var ppl = people(a);
  var pplHtml = ppl.length ? '<details class="resp"><summary class="chev"><h5>' + (a.owner ? 'Responsible<span class="h5n">' + esc(ownerName(a.owner)) + ' is the assignee' + (ppl.length > 1 ? ' · ' + (ppl.length - 1) + ' more from git, a lead not a verdict' : '') : 'Potential responsible<span class="h5n">' + ppl.length + (ppl.length === 1 ? ' person' : ' people') + ' · from git, a lead not a verdict') + '</span></h5></summary><ul class="people">' + ppl.map(function (p) {
    var r = p.refs[0], more = p.refs.slice(1);
    var t = String(r.url).startsWith('https://') ? '<a href="' + esc(r.url) + '" target="_blank" rel="noopener">' + esc(r.title) + ' ↗</a>' : esc(r.title);
    return '<li><b>' + esc(p.name) + '</b> <span>' + esc(r.what) + ' · ' + t + ' <span class="d">' + esc(r.date) + '</span>' +
      (more.length ? ' <span class="d" title="' + esc(more.map(function (m) { return m.what + ' · ' + m.title + ' · ' + m.date; }).join('\n')) + '">+' + more.length + ' more</span>' : '') + '</span></li>';
  }).join('') + '</ul></details>' : '';
  var note = a.note ? '<section class="dive"><h5>Investigation · ' + esc(dayhm(a.note.at)) + '</h5><p>' + esc(a.note.text) + '</p></section>' : '';
  var mb = a.muted ? '<section><h5>Muted</h5><p class="reason-line">' + esc(a.muted) + '</p></section>' : '';
  var analysis = sus + rec + draftHtml(a, i) + bizHtml(a, i) + codeHtml + pplHtml + note + invHtml(a) + mb;
  if (!analysis && !recShown) analysis = '<section><h5>Analysis</h5><p class="muted">Not assessed yet. Investigate queues one.</p></section>';
  analysis += runsHtml(a, i);
  var kids = (KIDS[a.key] || []).map(function (c) {
    var ck = (SEV[c.sev] || SEV.info)[1];
    var said = c.business && c.business.text ? '<span class="kid-todo"><b>' + bizLead(c.business) + ':</b> ' + esc(c.business.text) + '</span>' : '';
    return '<li class="' + ck + '"><div class="k1">' + sev(c.sev) + '<b>' + esc(c.svc) + '</b><span>' + esc(c.label) + (c.sub ? ' \u00b7 ' + esc(c.sub) : '') + '</span>' + numLink(c) + pageLink(c) + (c.status === 'cleared' ? '<span class="tag plain">' + (eventCause(c) ? 'stopped after the event' : 'cleared') + '</span>' : '') + '</div>' +
      (c.what ? '<span class="kid-what">' + linkify(c.what) + '</span>' : '') + said +
      '<span class="kv">now <b>' + num(c.now, c.unit) + '</b> · 1 d ' + num(c.d1, c.unit) + ' · 7 d ' + num(c.d7, c.unit) + '</span>' + linksHtml(c.links, 2) + '<span class="keyline">' + esc(c.key) + '</span></li>';
  }).join('');
  var lk = linksHtml(a.links);
  var ev = (lk ? '<section><h5>Open in</h5>' + lk + '</section>' : '') +
    (kids ? '<section><h5>Merged here · same cause (' + KIDS[a.key].length + ')</h5><ul class="kids">' + kids + '</ul></section>' : '') +
    '<section><h5>Key</h5><span class="keyline">' + esc(a.key) + '</span></section>';
  return '<div class="inc-body"><div class="analysis">' + what + '<p class="last-act" hidden></p>' + analysis + '</div><aside class="evidence">' + ev + '</aside></div>';
}
function cardTags(a) {
  var t = [];
  if (a.triage === 'handled' && a.triagedBy !== 'auto') {
    var how = resolutionOf(a);
    t.push({row: 2, html: '<span class="tag ' + TRI.handled[1] + ' res-tag">' + (how ? resolutionHtml(how) : '<span>' + esc('Resolved' + (a.triagedBy === 'pod' ? ' · by investigator' : '')) + '</span>') +
      (a.ticket && !(how && how.ref === a.ticket) ? ' · ' + jiraLinks(a.ticket) : '') + '</span>'});
  } else if (a.triage && TRI[a.triage]) t.push({row: 2, html: '<span class="tag ' + TRI[a.triage][1] + '">' + esc(TRI[a.triage][0]) + (a.triagedBy === 'pod' ? ' · by investigator' : a.triagedBy === 'auto' ? ' · auto-resolved' : '') + (a.ticket ? ' · ' + jiraLinks(a.ticket) : '') + '</span>'});
  var d = draftTag(a);
  if (d) t.push({row: autoArmed(a) && draftLive(a) ? 1 : 0, html: d});
  if (a.src === 'qan' && a.kind) t.push({row: 0, html: a.kind === 'dwh' ? '<span class="tag plain" title="Not application traffic: a data-platform or reporting query. Info only, never pushed.">DWH / data</span>' : '<span class="tag plain" title="Application query traffic">App query</span>'});
  if (a.gone && a.status === 'open') t.push({row: 1, html: '<span class="tag k-warn page-gone" title="' + esc(goneText(a)) + '">Page gone · ' + esc(a.gone.status) + '</span>'});
  if ((a.teamsPosts || []).length) t.push({row: 0, html: teamsTag(a.teamsPosts)});
  if (a.muted) t.push({row: 3, html: '<span class="tag k-muted">Muted</span>'});
  if (a.status === 'cleared') t.push({row: 4, html: '<span class="tag k-cleared">' + clearedWord(a) + ' ' + esc(daytime(a.clearedAt)) + '</span>'});
  if ((KIDS[a.key] || []).length) t.push({row: 0, html: '<span class="tag plain">+' + KIDS[a.key].length + ' merged</span>'});
  if (a.sevSet && a.sevSet.sev) t.push({row: 0, html: '<span class="tag plain sev-hand" title="' + esc('Severity set by hand by ' + draftBy(a.sevSet.by) + ' ' + dayhm(a.sevSet.at) + '; the signal says ' + (SEV[a.sevAuto || a.sevSet.auto] || SEV.info)[0] + '. Change severity › Back to the signal undoes it') + '">Severity by hand</span>'});
  var lead = t.filter(function (x) { return x.row; }).sort(function (x, y) { return x.row - y.row; })[0];
  return {row: (lead ? lead.html : '') + recurringTag(a), rest: t.filter(function (x) { return x !== lead; }).map(function (x) { return x.html; }).join('')};
}
function recurringTag(a) {
  if (!recurs(a)) return '';
  var tip = 'Fired ' + a.recurringCount + ' times in the last ' + (a.recurringDays || 7) + ' days (this one included) and no Jira ticket owns it; the investigator is asked to recommend one. Create task opens it.';
  return '<span class="tag k-warn recurring" title="' + esc(tip) + '">Recurring ×' + esc(a.recurringCount) + '</span>';
}
function clearedWord(a) { return eventCause(a) ? 'Stopped after the event' : 'Cleared'; }
function causeHtml(a) {
  var c = causeOf(a);
  return c ? '<p class="rec-line cause-line">Cause: ' + (c.kind === 'event' ? 'an event' : 'recurring') + ' \u00b7 <span data-no-tx>' + esc(c.what) + '</span></p>' : '';
}
function statusTag(a) {
  if (a.muted) return '<span class="spill k-muted">Muted</span>';
  if (a.status === 'cleared') return '<span class="spill k-cleared">' + clearedWord(a) + '</span>';
  return '<span class="spill">Open</span>';
}
function ageShort(iso) {
  var m = Math.round((Date.now() - Date.parse(iso)) / 60000);
  if (!(m >= 0)) return '';
  return m < 60 ? m + ' min' : m < 2880 ? Math.floor(m / 60) + ' h' : Math.floor(m / 1440) + ' d';
}
function cardWhen(a) { return a.status === 'cleared' ? 'first seen ' + daytime(a.firstSeen) : 'since ' + daytime(a.firstSeen) + (ago(a.firstSeen) ? ', ' + ago(a.firstSeen) : ''); }
function ageTick() {
  var root = document.getElementById('anoms');
  if (!root) return;
  var seen = function (n, text) { if (n.textContent !== text) n.textContent = text; };
  root.querySelectorAll('.tile').forEach(function (t) {
    var a = CARDS.filter(function (c) { return c.key === t.dataset.key; })[0], n = t.querySelector('.tile-age');
    if (a && n) seen(n, ageShort(a.firstSeen));
  });
  root.querySelectorAll('article.inc').forEach(function (c) {
    var a = CARDS.filter(function (x) { return x.key === c.dataset.key; })[0], n = c.querySelector('.c-age');
    if (!a || !n) return;
    seen(n, ageShort(a.firstSeen));
    var w = c.querySelector('.inc-more .line .when');
    if (w) seen(w, cardWhen(a));
    if (n.title !== cardWhen(a)) n.title = cardWhen(a);
  });
}
setInterval(ageTick, 60000);
var LIST_COLS = ['#', 'Sev', 'Card', 'Status', 'Assignee', 'Tags', 'Change', 'Age'];
function listHead() {
  return el('div', 'lhead', '<span class="lh-gap" aria-hidden="true"></span>' + LIST_COLS.map(function (c, n) { return '<span class="c c-' + ['num', 'sev', 'name', 'status', 'asg', 'tags', 'chg', 'age'][n] + '">' + esc(c) + '</span>'; }).join(''));
}
function cardOpen(card, on) {
  var b = card.querySelector('.inc-title .fold'), p = card.querySelector('.inc-more');
  if (!b || !p) return;
  card.classList.toggle('open', on);
  b.setAttribute('aria-expanded', on ? 'true' : 'false');
  p.hidden = !on;
  if (on) OPENK[card.dataset.key] = 1; else delete OPENK[card.dataset.key];
  if (on) loadRuns(card.dataset.key);
}

function anomCard(a, i) {
  var isDone = done(a), isOpen = a.status === 'open' && !a.muted && !isDone;
  var k = (SEV[a.sev] || SEV.info)[1];
  var card = el('article', 'inc ' + k + (isOpen ? '' : ' calm'));
  card.dataset.key = a.key;
  var tags = cardTags(a);
  var when = cardWhen(a);
  var ctx = [a.ns && a.ns !== '-' ? a.ns : '', a.sub || '', a.src || '', a.cluster ? 'instance ' + a.cluster : ''].filter(Boolean).join(' \u00b7 ');
  var spend = (window.INV_SPEND || {})[a.key];
  var usd = spend ? '<span class="usd" title="Investigator: ' + spend.n + (spend.n === 1 ? ' run, ' : ' runs, ') + usdText(spend.usd) + '">' + usdText(spend.usd) + '</span>' : '';
  var line = (a.src === 'airbyte' ? '<span>' + (a.sig === 'airbyte-long-run' ? 'running' : 'took') + ' <b>' + num(a.now, a.unit) + '</b></span><span>usual ' + num(a.d1, a.unit) + '</span>' :
    a.src === 'argo' ? '<span>for <b>' + num(a.now, a.unit) + '</b></span>' :
    '<span>now <b>' + num(a.now, a.unit) + '</b></span><span>1 d ' + num(a.d1, a.unit) + '</span><span>7 d ' + num(a.d7, a.unit) + '</span>') +
    '<span class="ch">' + esc(change(a)) + '</span><span class="when">' + esc(when) + '</span>' + (ctx ? '<span>' + esc(ctx) + '</span>' : '') + usd;
  var recShown = isOpen && !!a.recommend;
  var name = a.svc + ' / ' + a.label;
  card.innerHTML = '<div class="inc-head"><div class="inc-title">' +
    '<span class="c c-num">' + numLink(a) + '</span><span class="c c-sev">' + sev(a.sev) + '</span>' +
    '<span class="c c-name"><h4><button type="button" class="fold" aria-expanded="false" aria-controls="more-' + i + '" title="' + esc(name) + '"><span class="svc">' + esc(a.svc) + '</span><span class="sep">/</span><span class="sig">' + esc(cardTitle(a) || a.label) + '</span></button></h4>' + pageLink(a) + '</span>' +
    '<span class="c c-status">' + (tags.row || statusTag(a)) + '</span><span class="c c-asg">' + asgTag(a) + '</span><span class="c c-tags">' + bizTags(a) + '</span>' +
    '<span class="c c-chg"><span class="chg">' + esc(change(a)) + '</span></span><span class="c c-age" title="' + esc(when) + '">' + esc(ageShort(a.firstSeen)) + '</span></div>' +
    '<div class="acts" data-i="' + i + '" data-key="' + esc(a.key) + '" hidden></div></div>' +
    '<div class="inc-more" id="more-' + i + '" hidden><div class="line">' + line + '</div>' + causeHtml(a) + seenHtml(a) +
    (tags.rest ? '<div class="inc-tags">' + tags.rest + '</div>' : '') +
    (recShown ? '<p class="rec-line"><span>Next</span>' + esc(a.recommend) + '</p>' : isOpen && a.gone ? '<p class="rec-line"><span>Next</span>' + esc(goneText(a)) + '</p>' : '') +
    body(a, recShown, i) + '<div class="notif" data-i="' + i + '" data-key="' + esc(a.key) + '" hidden></div></div>';
  card.querySelector('.inc-title').addEventListener('click', function (e) {
    if (e.target.closest('a, input, select, textarea, label, button:not(.fold)')) return;
    var s = window.getSelection && window.getSelection();
    if (s && !s.isCollapsed && card.contains(s.anchorNode)) return;
    cardOpen(card, !card.classList.contains('open'));
  });
  if (OPENK[a.key]) cardOpen(card, true);
  return card;
}

function filterBase(f) {
  var out = [];
  function add(list) { list.forEach(function (a) { if (out.indexOf(a) < 0) out.push(a); }); }
  if (f === 'open') { add(open); add(stale); return out; }
  if (f === 'all') { [open, stale, inprog, taken, muted, cleared].forEach(add); return out; }
  if (f === 'crit' || f === 'warn' || f === 'info') return openBySev(f);
  if (f === 'investigating' || f === 'dived') return A.filter(function (a) { return a.triage === f; });
  if (f === 'svc') return svcF ? svcCards(svcF).list : [];
  return {stale: stale, progress: inprog, taken: taken, muted: muted, cleared: cleared, recurring: recurringL, slack: slackRec, archived: ARCHIVED, closed: []}[f] || A;
}
var VIEW_KEY = 'skopax-card-view';
var CARD_VIEW = load(VIEW_KEY) === 'list' ? 'list' : 'board';
var DRAWER = null;
var BOARD_SHOWN = 15, BOARD_FULL = {};
var DONE_TICK = '<svg class="bcol-tick" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><polyline points="20 6 9 17 4 12"/></svg>';
var BOARD_COLS = [['open', 'Open', 'firing, or stopped with nobody acting on it yet; critical first, firing before stopped. The tags say whether it was investigated and whether it stopped'],
  ['progress', 'In progress', 'a ticket or hand-off is underway'], ['done', 'Done', 'action taken, or cleared with nothing to do'], ['muted', 'Muted', 'no pushes while the mute lasts']];
function boardColumn(a) {
  if (a.muted) return 'muted';
  if (a.triage === 'handled') return 'done';
  if (a.triage === 'progress') return 'progress';
  return a.status === 'open' || stale.indexOf(a) >= 0 ? 'open' : 'done';
}
function boardState(a) {
  var t = '';
  if (TRI[a.triage] && (a.triage === 'investigating' || a.triage === 'dived')) t += '<span class="tag ' + TRI[a.triage][1] + '" title="' + esc(a.triage === 'dived' ? 'Investigated; nobody acted on it yet' : 'The investigator is on it') + '">' + esc(TRI[a.triage][0]) + '</span>';
  if (a.status === 'cleared' && stale.indexOf(a) >= 0) t += '<span class="tag k-crit" title="' + esc((eventCause(a) ? 'Stopped after the event ' : 'Stopped firing ') + daytime(a.clearedAt) + ', nobody acted yet: waiting to be addressed') + '">' + esc(clearedWord(a) + ' ' + daytime(a.clearedAt)) + '</span>';
  return t;
}
function setCardView(v) {
  if (v === CARD_VIEW) return;
  CARD_VIEW = v;
  save(VIEW_KEY, v);
  DRAWER = null;
  markCardView();
  setFilter(filter);
}
function markCardView() {
  document.querySelectorAll('#card-view button[data-view]').forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.view === CARD_VIEW)); });
  var root = document.getElementById('anoms');
  if (root) { root.classList.toggle('view-board', CARD_VIEW === 'board'); root.classList.toggle('view-list', CARD_VIEW !== 'board'); }
}
var TITLE_MAX = 90;
function cut(text, max) { text = String(text || '').replace(/\s+/g, ' ').trim(); return text.length > max ? text.slice(0, max - 1).replace(/\s+\S*$/, '') + '…' : text; }
function patternTitle(a) {
  var key = String(a.key || ''), tail = a.cluster ? '@' + a.cluster : '';
  var parts = (tail && key.slice(-tail.length) === tail ? key.slice(0, -tail.length) : key).split('|');
  if (parts[0] !== 'log' || parts.length < 4) return '';
  var t = parts.slice(3).join('|').replace(/\s*-\s*Request data:.*$/i, '').replace(/[:\s]*[{[].*$/, '').replace(/\bApi:\s*/g, '');
  var bare = t.replace(/\s*\([^()]*\)\s*$/, '');
  if (bare.replace(/[\s:.-]/g, '').length >= 3) t = bare;
  return cut(t.replace(/[\s:.-]+$/, '').replace(/\s+-\s+/g, ' · '), TITLE_MAX);
}
function businessTitle(a) {
  var text = a.business && a.business.text;
  if (!text || !tagIds(a).some(function (id) { return !TECH_TAGS[id]; })) return '';
  var first = String(text);
  if (a.svc && first.startsWith(a.svc)) first = first.slice(a.svc.length).replace(/^\s*(?::|-)\s*/i, '');
  return cut((a.business.kind === 'info' ? '' : 'Recommended: ') + first, TITLE_MAX);
}
function cardTitle(a) { return businessTitle(a) || patternTitle(a) || ''; }
function tile(a) {
  var b = el('div', 'tile ' + (SEV[a.sev] || SEV.info)[1]);
  b.tabIndex = 0;
  b.setAttribute('role', 'button');
  b.dataset.key = a.key;
  b.setAttribute('aria-haspopup', 'dialog');
  var lock = window.__boardLock ? window.__boardLock() : 'Log in to move cards';
  b.draggable = !lock;
  var where = a.cluster ? ' \u00b7 instance ' + a.cluster : '';
  b.title = a.svc + ' / ' + a.label + where + (a.num ? ' \u00b7 #' + a.num : '') + ' \u00b7 opens the card' + (lock ? '' : '; drag it to another column to change its status');
  var who = a.owner ? '<span class="tile-asg" title="' + esc('Assigned to ' + a.owner) + '">' + esc(ownerName(a.owner).split(/[._-]/).map(function (p) { return p.charAt(0); }).join('').slice(0, 2).toUpperCase()) + '</span>' : '';
  b.innerHTML = '<span class="tile-top">' + sev(a.sev) + (a.num ? '<span class="tile-num">#' + esc(a.num) + '</span>' : '') + '<span class="tile-age">' + esc(ageShort(a.firstSeen)) + '</span></span>' +
    (cardTitle(a) ? '<span class="tile-svc tile-title">' + esc(cardTitle(a)) + '</span><span class="tile-sig">' + esc(a.svc + ' \u00b7 ' + a.label + where) + '</span>'
      : '<span class="tile-svc">' + esc(a.svc) + '</span><span class="tile-sig">' + esc(a.label + where) + '</span>') +
    '<span class="tile-foot">' + (boardColumn(a) === 'open' ? boardState(a) : '') + bizTags(a) + (draftOf(a) && !draftSent(a) ? '<span class="tag k-act">Slack draft</span>' : '') + (recurs(a) ? '<span class="tag k-warn">Recurring</span>' : '') + who + '</span>';
  b.addEventListener('click', function () { openDrawer(a.key); });
  b.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openDrawer(a.key); } });
  b.addEventListener('dragstart', function (e) {
    if (!b.draggable) { e.preventDefault(); return; }
    DRAG = a;
    if (e.dataTransfer) { e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', a.key); }
    b.classList.add('dragging');
    document.querySelectorAll('#anoms .bcol').forEach(function (c) { c.classList.add(moveSteps(a, c.dataset.col) ? 'drop-ok' : 'drop-no'); });
  });
  b.addEventListener('dragend', dragDone);
  if (DRAWER === a.key) b.classList.add('on');
  return b;
}
var TILES = {};
function cardSig(a) { return JSON.stringify(a) + '|' + RENDER_EPOCH + '|' + JSON.stringify(KIDS[a.key] || []); }
function tileFor(a, next) {
  var sig = cardSig(a) + '|' + (window.__boardLock ? window.__boardLock() : ''), held = TILES[a.key], node;
  if (held && held.sig === sig && !held.node.classList.contains('dragging')) node = held.node;
  else node = tile(a);
  next[a.key] = {sig: sig, node: node};
  node.classList.toggle('on', DRAWER === a.key);
  return node;
}
var DRAG = null;
function dragDone() {
  DRAG = null;
  document.querySelectorAll('#anoms .tile.dragging').forEach(function (t) { t.classList.remove('dragging'); });
  document.querySelectorAll('#anoms .bcol').forEach(function (c) { c.classList.remove('drop-ok', 'drop-no', 'drop-on'); });
}
function landsIn(a) {
  return a.status === 'open' || (a.status === 'cleared' && (a.sev === 'warn' || a.sev === 'crit')) ? 'open' : null;
}
function moveSteps(a, to) {
  var from = boardColumn(a), name = a.svc + ' ' + a.label, base = {target: a.key, key: a.key};
  if (!a || from === to) return null;
  var un = a.muted ? [Object.assign({type: 'unmute', label: 'Unmute ' + name}, base)] : [];
  if (to === 'open') return (a.muted || a.triage) && landsIn(a) === to ? [Object.assign({type: 'reopen', label: 'Reopen ' + name}, base)] : null;
  if (to === 'progress') return un.concat([Object.assign({type: 'take', label: 'Take ' + name}, base)]);
  if (to === 'done') return 'ask';
  if (to === 'muted') return !a.muted && (a.status === 'open' || stale.indexOf(a) >= 0) ? 'ask' : null;
  return null;
}
function boardNote(text, bad) {
  var n = document.getElementById('board-note');
  if (!n) return;
  n.textContent = text;
  n.className = 'board-note' + (bad ? ' bad' : '');
}
async function dropOn(a, to) {
  var steps = moveSteps(a, to), name = (a.num ? '#' + a.num + ' ' : '') + a.svc + ' ' + a.label;
  if (!steps) return;
  if (steps === 'ask') {
    var got = to === 'done' ? await resolveDialog(a) : await muteDialog(a);
    if (!got) return;
    steps = (to === 'done' && a.muted ? [{type: 'unmute', label: 'Unmute ' + a.svc + ' ' + a.label, target: a.key, key: a.key}] : []).concat([Object.assign({target: a.key, key: a.key}, got)]);
  }
  if (!window.__boardMove) return;
  boardNote('Moving ' + name + '…');
  try {
    var last = await window.__boardMove(steps);
    if (last && last.status === 'failed') boardNote('Not moved: ' + (last.result || brandName() + ' refused it'), true);
    else boardNote('Moved ' + name + ' to ' + BOARD_COLS.filter(function (c) { return c[0] === to; })[0][1] + '.');
  } catch (e) {
    boardNote('Not moved: ' + (e && e.code === 403 ? 'this account may not do that' : e && e.code === 401 ? 'log in first' : (e && e.detail) || 'try again'), true);
  }
}
function muteDialog(a) {
  return new Promise(function (resolve) {
    var name = a.svc + ' / ' + a.label;
    var wrap = el('div', 'modal-wrap mute-pick');
    wrap.innerHTML = '<div class="modal" role="dialog" aria-modal="true" aria-labelledby="mp-title"><h2 id="mp-title">Mute ' + esc(name) + '</h2>' +
      '<p>For how long? A mute stops pushes; the card stays on the page.</p><div class="mp-opts" role="radiogroup" aria-label="Mute for">' +
      '<label><input type="radio" name="mp" value="24" checked> 24 hours</label><label><input type="radio" name="mp" value="168"> 1 week</label>' +
      '<label><input type="radio" name="mp" value="known"> For good: known noise</label></div>' +
      '<input type="text" class="mp-why" maxlength="200" placeholder="Why this is expected (optional)" aria-label="Reason" hidden>' +
      '<div class="modal-acts"><button type="button" class="btn sec mp-no">Cancel</button><button type="button" class="btn mp-yes">Mute</button></div></div>';
    document.body.appendChild(wrap);
    var why = wrap.querySelector('.mp-why');
    function pick() { return (wrap.querySelector('input[name="mp"]:checked') || {}).value; }
    function close(v) { wrap.remove(); document.removeEventListener('keydown', onKey); resolve(v); }
    function onKey(e) { if (e.key === 'Escape') close(null); }
    wrap.querySelectorAll('input[name="mp"]').forEach(function (r) { r.addEventListener('change', function () { why.hidden = pick() !== 'known'; if (!why.hidden) why.focus(); }); });
    wrap.querySelector('.mp-no').addEventListener('click', function () { close(null); });
    wrap.addEventListener('click', function (e) { if (e.target === wrap) close(null); });
    wrap.querySelector('.mp-yes').addEventListener('click', function () {
      var v = pick();
      close(v === 'known' ? {type: 'known', reason: why.value.trim() || 'marked as known noise on the board', label: 'Known noise: ' + a.svc + ' ' + a.label}
        : {type: 'mute', hours: +v, label: 'Mute ' + a.svc + ' ' + a.label + (v === '24' ? ' for 24 h' : ' for 1 week')});
    });
    document.addEventListener('keydown', onKey);
    wrap.querySelector('input[name="mp"]:checked').focus();
  });
}
function resolveDialog(a) {
  return new Promise(function (resolve) {
    var name = a.svc + ' / ' + a.label, picked = 'fixed';
    var wrap = el('div', 'modal-wrap resolve-pick');
    wrap.innerHTML = '<div class="modal" role="dialog" aria-modal="true" aria-labelledby="rp-title"><h2 id="rp-title">Resolve ' + esc(name) + '</h2>' +
      '<p>How was it resolved?</p><div class="rp-opts"></div>' +
      '<input type="text" class="rp-why" maxlength="300" placeholder="' + RES_PLACEHOLDER_WHY + '" title="' + esc(RES_TITLE_WHY) + '" aria-label="Why it is resolved">' +
      '<input type="text" class="rp-dup" maxlength="200" placeholder="' + RES_PLACEHOLDER_DUP + '" title="' + esc(RES_TITLE_DUP) + '" aria-label="The card it duplicates" hidden>' +
      '<div class="modal-acts"><button type="button" class="btn sec rp-no">Cancel</button><button type="button" class="btn rp-yes">Resolve</button></div></div>';
    document.body.appendChild(wrap);
    var why = wrap.querySelector('.rp-why'), dup = wrap.querySelector('.rp-dup');
    wrap.querySelector('.rp-opts').appendChild(resKindPicker(picked, function (k) {
      picked = k;
      why.hidden = k === 'duplicate';
      dup.hidden = k !== 'duplicate';
      dup.removeAttribute('aria-invalid');
      (k === 'duplicate' ? dup : why).focus();
    }));
    function close(v) { wrap.remove(); document.removeEventListener('keydown', onKey); resolve(v); }
    function onKey(e) { if (e.key === 'Escape') close(null); }
    wrap.querySelector('.rp-no').addEventListener('click', function () { close(null); });
    wrap.addEventListener('click', function (e) { if (e.target === wrap) close(null); });
    wrap.querySelector('.rp-yes').addEventListener('click', function () {
      var ref = dup.value.trim();
      if (picked === 'duplicate' && !ref) { dup.setAttribute('aria-invalid', 'true'); dup.focus(); return; }
      close(resolveBody(picked, why.value.trim(), ref, a.svc + ' ' + a.label));
    });
    document.addEventListener('keydown', onKey);
    wrap.querySelector('.res-kind[aria-pressed="true"]').focus();
  });
}
function wireDrop(col) {
  col.addEventListener('dragover', function (e) {
    if (!DRAG || !moveSteps(DRAG, col.dataset.col)) return;
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
    col.classList.add('drop-on');
  });
  col.addEventListener('dragleave', function (e) { if (!col.contains(e.relatedTarget)) col.classList.remove('drop-on'); });
  col.addEventListener('drop', function (e) {
    e.preventDefault();
    var a = DRAG;
    dragDone();
    if (a) dropOn(a, col.dataset.col);
  });
}
var QF_PEOPLE = 8;
function renderQuick() {
  var box = document.getElementById('card-qf');
  if (!box) return;
  box.innerHTML = '';
  var base = filterBase(filter);
  function chip(t, label, n, tip, cls) {
    var b = el('button', 'qf-c' + (cls ? ' ' + cls : '') + (n ? '' : ' zero'), label + '<span class="count">' + esc(n) + '</span>');
    b.type = 'button'; b.dataset.type = t;
    b.setAttribute('aria-pressed', hasF(t) ? 'true' : 'false');
    b.title = tip + ' · ' + (hasF(t) ? 'click again to drop this filter' : 'shows only them; combines with the other group');
    b.addEventListener('click', function () { setType(toggledF(t)); });
    return b;
  }
  var who = el('div', 'qf-g');
  who.setAttribute('role', 'group'); who.setAttribute('aria-label', 'Assignee');
  who.appendChild(el('span', 'qf-l', 'Assignee'));
  ['who:me', 'who:none'].forEach(function (t) {
    who.appendChild(chip(t, esc(WHO_CHIPS[t]), base.filter(function (a) { return typeIs(a, t); }).length, t === 'who:me' ? (ME_USER ? 'cards assigned to you' : 'cards assigned to you (log in to see them)') : 'cards with no assignee'));
  });
  var people = whoPeople(base);
  Object.keys(people).sort(function (x, y) { return people[y].n - people[x].n || (x < y ? -1 : 1); }).filter(function (s, i) { return i < QF_PEOPLE || hasF('who:u:' + s); }).forEach(function (s) {
    var p = people[s], ini = p.name.split(/[\s._-]+/).filter(Boolean).map(function (w) { return w.charAt(0); }).join('').slice(0, 2).toUpperCase();
    who.appendChild(chip('who:u:' + s, '<span class="qf-av" aria-hidden="true">' + esc(ini) + '</span><span class="qf-n">' + esc(p.name) + '</span>', p.n, 'cards assigned to ' + p.name, 'qf-p'));
  });
  var tg = el('div', 'qf-g');
  tg.setAttribute('role', 'group'); tg.setAttribute('aria-label', 'Tags');
  tg.appendChild(el('span', 'qf-l', 'Tags'));
  var n = {};
  base.forEach(function (a) { tagIds(a).forEach(function (t) { n[t] = (n[t] || 0) + 1; }); });
  Object.keys(TAG_NAMES).filter(function (id) { return n[id] || hasF('tag:' + id); }).forEach(function (id) {
    tg.appendChild(chip('tag:' + id, esc(tagName(id)), n[id] || 0, 'cards tagged ' + tagName(id), 'tcol t-' + id));
  });
  if (tg.children.length > 1) box.append(tg);
  box.append(who);
}
function renderBoard(root, groups) {
  var cards = [], nextTiles = {};
  groups.forEach(function (gr) { gr.list.forEach(function (a) { if (cards.indexOf(a) < 0) cards.push(a); }); });
  var board = el('div', 'board');
  BOARD_COLS.forEach(function (c) {
    var list = cards.filter(function (a) { return boardColumn(a) === c[0]; }).sort(function (x, y) {
      return (x.sev in SEV_ORD ? SEV_ORD[x.sev] : 3) - (y.sev in SEV_ORD ? SEV_ORD[y.sev] : 3) || (c[0] === 'open' ? (x.status === 'open' ? 0 : 1) - (y.status === 'open' ? 0 : 1) : 0) || String(y.lastSeen || y.firstSeen || '').localeCompare(String(x.lastSeen || x.firstSeen || ''));
    });
    var col = el('section', 'bcol bcol-' + c[0]);
    col.dataset.col = c[0];
    wireDrop(col);
    var hd = el('header', 'bcol-h', (c[0] === 'done' ? DONE_TICK : '') + '<h3>' + esc(c[1]) + '</h3><span class="count">' + list.length + '</span>');
    hd.title = c[2];
    col.appendChild(hd);
    var body = el('div', 'bcol-body');
    if (!list.length) body.appendChild(el('p', 'bcol-empty', '–'));
    var full = BOARD_FULL[c[0]] || list.length <= BOARD_SHOWN + 1 || list.some(function (a) { return a.key === DRAWER; });
    (full ? list : list.slice(0, BOARD_SHOWN)).forEach(function (a) { body.appendChild(tileFor(a, nextTiles)); });
    if (!full) {
      var more = el('button', 'bcol-more', 'Show ' + (list.length - BOARD_SHOWN) + ' more');
      more.type = 'button';
      more.addEventListener('click', function () { BOARD_FULL[c[0]] = 1; setFilter(filter); });
      body.appendChild(more);
    }
    col.appendChild(body);
    board.appendChild(col);
  });
  root.appendChild(board);
  var note = el('p', 'board-note');
  note.id = 'board-note';
  note.setAttribute('aria-live', 'polite');
  root.insertBefore(note, board);
  if (!cards.length && searchQ) root.appendChild(el('p', 'muted', 'No card matches.'));
  TILES = nextTiles;
  if (DRAWER && cards.some(function (a) { return a.key === DRAWER; })) openDrawer(DRAWER, true);
  else { DRAWER = null; var gone = drawerNode(root); if (gone) gone.remove(); }
}
function drawerNode(root) { return [].filter.call(root.children, function (c) { return c.classList.contains('card-drawer'); })[0] || null; }
function openDrawer(key, quiet) {
  var a = CARDS.filter(function (x) { return x.key === key; })[0], root = document.getElementById('anoms');
  if (!a || !root) return;
  DRAWER = key;
  var old = drawerNode(root);
  if (old && old.dataset.key === key) { refreshDrawer(old, a, root); return; }
  if (old) old.remove();
  var wrap = el('div', 'card-drawer');
  wrap.dataset.key = key;
  wrap.dataset.sig = cardSig(a);
  wrap.setAttribute('role', 'dialog');
  wrap.setAttribute('aria-modal', 'true');
  wrap.setAttribute('aria-label', a.svc + ' / ' + a.label);
  wrap.innerHTML = '<div class="cd-backdrop"></div><div class="cd-panel"><div class="cd-bar"><span class="cd-t">' + (a.num ? 'Card #' + esc(a.num) : 'Card') + '</span>' +
    (a.num ? '<a class="cd-link" href="/c/' + esc(a.num) + '" target="_blank" rel="noopener">Open alone ↗</a>' : '') + '<button type="button" class="btn sec small cd-close">Close</button></div></div>';
  var card = anomCard(a, CARDS.indexOf(a));
  wrap.querySelector('.cd-panel').appendChild(card);
  root.appendChild(wrap);
  cardOpen(card, true);
  root.querySelectorAll('.tile').forEach(function (t) { t.classList.toggle('on', t.dataset.key === key); });
  wrap.querySelector('.cd-backdrop').addEventListener('click', closeDrawer);
  wrap.querySelector('.cd-close').addEventListener('click', closeDrawer);
  wrap.addEventListener('keydown', function (e) { if (e.key === 'Escape') { e.stopPropagation(); closeDrawer(); } });
  if (!quiet) wrap.querySelector('.cd-close').focus();
  if (window.__skopaxRender) window.__skopaxRender();
}
function refreshDrawer(wrap, a, root) {
  root.querySelectorAll('.tile').forEach(function (t) { t.classList.toggle('on', t.dataset.key === a.key); });
  var sig = cardSig(a);
  if (wrap.dataset.sig === sig) return;
  var panel = wrap.querySelector('.cd-panel'), at = panel.scrollTop, held = panel.querySelector('article.inc');
  var card = anomCard(a, CARDS.indexOf(a));
  if (held) panel.replaceChild(card, held); else panel.appendChild(card);
  cardOpen(card, true);
  wrap.dataset.sig = sig;
  wrap.setAttribute('aria-label', a.svc + ' / ' + a.label);
  panel.scrollTop = at;
  if (window.__skopaxRender) window.__skopaxRender();
}
function closeDrawer() {
  var root = document.getElementById('anoms'), key = DRAWER;
  DRAWER = null;
  var d = root && root.querySelector('.card-drawer');
  if (d) d.remove();
  if (key) delete OPENK[key];
  var t = root && [].filter.call(root.querySelectorAll('.tile'), function (x) { return x.dataset.key === key; })[0];
  if (root) root.querySelectorAll('.tile.on').forEach(function (x) { x.classList.remove('on'); });
  if (t) t.focus();
}
function renderAnoms() {
  var root = document.getElementById('anoms'), panel = document.getElementById('panel-anoms');
  var keep = DRAWER && CARD_VIEW === 'board' && filter !== 'closed' ? drawerNode(root) : null;
  [].slice.call(root.children).forEach(function (c) { if (c !== keep) c.remove(); });
  if (root.firstChild && !keep) root.textContent = '';
  if (panel) panel.classList.toggle('closed-on', filter === 'closed');
  if (filter === 'closed') {
    renderClosed(root);
    shownN = CLOSED.data ? CLOSED.data.total : 0;
    return;
  }
  markCardView();
  renderQuick();
  var groups = [];
  function g(id, title, hint, k, list, empty) {
    if (typeF) {
      list = typed(list);
      if (!list.length && (id === 'open' || empty)) empty = 'No ' + (id === 'open' ? 'open ' : '') + typeName(typeF) + ' cards here' + (filter === 'open' ? ': All shows the stopped, muted and cleared ones too.' : '.');
    }
    if (searchQ) {
      list = searched(list);
      empty = null;
    }
    if (list.length || (!searchQ && id === 'open') || empty) groups.push({id: id, title: title, hint: hint, k: k, list: list, empty: empty});
  }
  if (filter === 'open' || filter === 'all') g('open', 'Open', 'critical first; a card that stopped firing or was investigated stays here until someone takes, resolves or mutes it, and its tags say which', SEV[worst][1], openAll());
  if (['crit', 'warn', 'info'].indexOf(filter) >= 0) g('open', SEV[filter][0], 'open: firing, or stopped with nobody acting on it yet', SEV[filter][1], openBySev(filter));
  if (filter === 'progress' || filter === 'all') g('progress', TRI.progress[0], 'a ticket or hand-off is underway; pushes again only if it doubles or gets worse. A card with a Jira ticket stays until the ticket closes, one with free text drops off 24 hours after it cleared', TRI.progress[1], inprog);
  if (filter === 'taken' || filter === 'all') g('taken', 'Action taken', 'no action needed; pushes again only if it doubles or gets worse, and comes back under Open when it stops and fires again. Drops off 24 hours after it cleared or was resolved, whichever is later', 'k-good', taken);
  if (filter === 'muted' || filter === 'all') g('muted', 'Muted', 'no pushes while the mute lasts, open or already cleared; unmute to get them again', 'k-muted', muted);
  if (filter === 'cleared' || filter === 'all') g('cleared', 'Cleared · no action needed', 'the signal went back to normal and nothing needs doing (info or muted); drops off 24 hours after it cleared', 'k-cleared', filter === 'all' ? cleared.filter(function (a) { return muted.indexOf(a) < 0; }) : cleared);
  if (filter === 'archived') g('archived', 'Archived', 'cards that are not firing (or are muted) and had no change for ' + archiveHours() + ' hours (Admin \u203a General, Archive after); they show nowhere else', 'k-cleared', ARCHIVED, 'Nothing archived.');
  if (filter === 'recurring') g('recurring', 'Recurring', 'cards that keep coming back with no Jira ticket on them, whatever their status: open a ticket with Create task, or Take one that exists', 'k-warn', recurringL, 'No recurring cards.');
  if (filter === 'slack') g('slack', 'Slack recommended', 'a Slack draft is waiting on each of these, whatever their status: read it (tag tooltip or the expanded card) and use Send draft to Slack', 'k-act', slackRec, 'No Slack drafts waiting.');
  if (TRI[filter] && (filter === 'investigating' || filter === 'dived')) g(filter, TRI[filter][0], 'open, muted and cleared', TRI[filter][1], A.filter(function (a) { return a.triage === filter; }));
  if (filter === 'svc' && svcF) {
    var sc = svcCards(svcF);
    g('svc', svcF.svc + ' \u00b7 ' + sc.tier, 'the cards behind this service\u2019s status on Services: the open ones, or when none is open, the ones already stopped, action taken or muted', sc.k, sc.list, 'Nothing on ' + svcF.svc + ' any more: its cards cleared and dropped off.');
  }
  shownN = groups.reduce(function (n, gr) { return n + gr.list.length; }, 0);
  var found = document.getElementById('card-search-n');
  if (found) found.textContent = searchQ ? (shownN ? shownN + (shownN === 1 ? ' card' : ' cards') + ' found in every list' : 'No card matches') : '';
  if (CARD_VIEW === 'board') { renderBoard(root, groups); return; }
  groups.forEach(function (gr) {
    var sec = el('section', 'grp ' + gr.k);
    var hd = el('header', 'grp-h', '<h3>' + esc(gr.title) + '</h3><span class="count">' + gr.list.length + '</span>');
    if (gr.hint) hd.title = gr.hint;
    sec.appendChild(hd);
    if (!gr.list.length) sec.appendChild(el('div', 'card empty-card', '<span class="ok" aria-hidden="true">✓</span><span>' + esc(gr.empty || 'Nothing open.') + '</span>'));
    else sec.appendChild(listHead());
    var parts = !typeF && gr.list.length >= TYPE_SPLIT ? typeGroups(gr.list) : [];
    if (parts.length > 1) parts.forEach(function (p) { sec.appendChild(typeGroup(gr.id, p)); });
    else gr.list.forEach(function (a) { sec.appendChild(anomCard(a, CARDS.indexOf(a))); });
    root.appendChild(sec);
  });
}
var TYPE_SPLIT = 8, TYPE_FOLD_AT = 4, TFOLD_KEY = 'skopax-type-fold';
var TFOLD = (function () { try { return JSON.parse(load(TFOLD_KEY) || '{}') || {}; } catch (e) { return {}; } })();
function typeGroups(list) {
  var by = {}, order = [];
  list.forEach(function (a) {
    var t = typeOf(a);
    if (!by[t]) { by[t] = []; order.push(t); }
    by[t].push(a);
  });
  var rank = function (list) { return Math.min.apply(null, list.map(function (a) { return a.sev in SEV_ORD ? SEV_ORD[a.sev] : 3; })); };
  return order.map(function (t, i) { return {type: t, list: by[t], rank: rank(by[t]), i: i}; })
    .sort(function (x, y) { return x.rank - y.rank || x.i - y.i; });
}
function typeGroup(groupId, p) {
  var id = groupId + ':' + p.type, d = el('details', 'tgrp');
  d.dataset.fold = id;
  var crit = p.list.filter(function (a) { return a.sev === 'crit'; }).length;
  d.appendChild(el('summary', 'tgrp-h chev', '<span class="tgrp-n">' + esc(typeName(p.type)) + '</span><span class="count">' + p.list.length + '</span>' +
    (crit ? '<span class="tag k-crit">' + crit + ' critical</span>' : '')));
  var body = el('div', 'tgrp-body');
  p.list.forEach(function (a) { body.appendChild(anomCard(a, CARDS.indexOf(a))); });
  d.appendChild(body);
  var kept = TFOLD[id], expanded = p.list.some(function (a) { return OPENK[a.key]; });
  d.open = expanded || (kept != null ? kept === 1 : p.list.length < TYPE_FOLD_AT);
  d.addEventListener('toggle', function () {
    TFOLD[id] = d.open ? 1 : 0;
    save(TFOLD_KEY, JSON.stringify(TFOLD));
  });
  return d;
}
(function () {
  var v = document.getElementById('card-view');
  if (v) v.addEventListener('click', function (e) { var b = e.target.closest('button[data-view]'); if (b) setCardView(b.dataset.view); });
})();
