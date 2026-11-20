var HIST_ACTS = {take: 'Started progress', resolve: 'Resolved', ticket: 'Ticket created', link: 'Ticket linked', mute: 'Muted', unmute: 'Unmuted', known: 'Marked known noise', reopen: 'Reopened',
  dive: 'Investigation queued', group: 'Grouped', severity: 'Severity changed', ask: 'Asked the investigator'};
var HIST_SEV = {crit: 'Critical', warn: 'Warning', info: 'Info'};
var HIST_CUT = 160;
function histEsc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); }
function histWho(by) {
  var s = String(by == null ? '' : by);
  if (!s) return 'system';
  if (s === 'Claude' || s === 'pod') return 'Investigator';
  if (s === 'server' || s === 'auto' || s === 'system') return 'system';
  if (typeof isTokenActor === 'function' && isTokenActor(s)) return tokenActorLabel(s, 'Investigator');
  return s.split('@')[0];
}
function histCut(t) { t = String(t || '').replace(/\s+/g, ' ').trim(); return t.length > HIST_CUT ? t.slice(0, HIST_CUT - 1) + '\u2026' : t; }
function histFirst(t) { return String(t || '').split('\n').map(function (x) { return x.trim(); }).filter(Boolean)[0] || ''; }
var HIST_FIXED = {Investigator: 1, system: 1, signal: 1, rule: 1};
function histTx(t) { return typeof tx === 'function' ? tx(t) : t; }
function histLabel(id, names) { return (names && names[id]) || (typeof tagName === 'function' ? tagName(id) : id); }
function cardHistoryRows(c, data, runs, names) {
  c = c || {};
  data = data || {};
  var rows = [], key = c.key || '', acts = (data.actions || []).slice(), log = data.activity || [];
  function add(at, who, label, data, extra) {
    if (!at || !(label || data)) return;
    rows.push(Object.assign({at: at, who: who, label: label ? histTx(label) : '', data: data || '', what: label && data ? label + ': ' + data : label || data}, extra || {}));
  }
  function plain(text) {
    var t = String(text || ''), tail = ' \u00b7 ' + key;
    if (key && t.slice(-tail.length) === tail) t = t.slice(0, -tail.length);
    return t ? t.charAt(0).toUpperCase() + t.slice(1) : '';
  }
  var said = log.map(function (r) { return r.kind + ':' + plain(r.text).toLowerCase(); });
  var assignLogged = log.some(function (r) { return r.kind === 'assign'; });
  acts.sort(function (x, y) { return (x.doneAt || x.requestedAt || '') < (y.doneAt || y.requestedAt || '') ? -1 : 1; });
  var sevNow = null;
  acts.forEach(function (x) {
    if (!HIST_ACTS[x.type] || ['done', 'failed', 'cancelled'].indexOf(x.status) < 0) return;
    var at = x.doneAt || x.failedAt || x.cancelledAt || x.requestedAt, who = histWho(x.requestedBy);
    if (x.status !== 'done') { add(at, who, HIST_ACTS[x.type] + (x.status === 'failed' ? ' failed' : ' cancelled'), x.result ? histCut(x.result) : '', {kind: 'action'}); return; }
    if (x.type === 'severity') {
      var to = HIST_SEV[x.sev] || (/\(([A-Za-z]+)\)$/.exec(x.result || '') || [])[1] || x.sev || '';
      var from = sevNow || (/the signal says ([A-Za-z]+)/.exec(x.result || '') || [])[1] || '';
      add(at, who, 'Severity changed', '', {kind: 'severity', from: from && from !== to ? histTx(from) : '', to: histTx(to)});
      sevNow = to;
      return;
    }
    add(at, who, HIST_ACTS[x.type], x.result ? histCut(x.result) : '', {kind: 'action'});
  });
  log.forEach(function (r) { add(r.t, histWho(r.who), '', histCut(plain(r.text)), {kind: r.kind}); });
  (runs || []).forEach(function (r) {
    if (!r || !r.id) return;
    var line = histFirst(r.recap);
    add(r.startedAt, 'Investigator', 'Investigation run', line ? histCut(line) : r.outcome || '', {kind: 'run', href: '/api/investigations/' + encodeURIComponent(r.id)});
  });
  (c.tags || []).forEach(function (t) {
    var label = histLabel(t.id, names);
    if (said.indexOf('tags:tagged ' + label.toLowerCase()) >= 0) return;
    add(t.at, t.by === 'rule' ? 'rule' : histWho(t.by), 'Tag added', label, {kind: 'tags'});
  });
  var b = c.business;
  if (b && b.text && b.at && said.indexOf('tags:business sentence saved') < 0) add(b.at, b.by === 'rule' ? 'rule' : histWho(b.by), 'Business note written', '', {kind: 'tags'});
  if (c.assigned && c.assigned.at && !assignLogged) add(c.assigned.at, histWho(c.assigned.by), 'Assigned to', String(c.owner || '').split('@')[0], {kind: 'assign'});
  if (c.sevSet && c.sevSet.at && !acts.some(function (x) { return x.type === 'severity'; })) add(c.sevSet.at, histWho(c.sevSet.by), 'Severity changed', '', {kind: 'severity', from: histTx(HIST_SEV[c.sevSet.auto] || ''), to: histTx(HIST_SEV[c.sevSet.sev] || c.sevSet.sev)});
  var p = c.slackPost;
  if (p && p.at) add(p.at, histWho(p.by), 'Posted to Slack', '', {kind: 'slack', href: p.permalink && /^https:\/\//.test(p.permalink) ? p.permalink : ''});
  (c.teamsPosts || []).forEach(function (t) { add(t.at, histWho(t.by), 'Posted to Teams', histLabel(t.route, names), {kind: 'teams'}); });
  if (c.clearedAt) add(c.clearedAt, 'signal', 'Cleared', '', {kind: 'signal'});
  var again = acts.some(function (x) { return (x.doneAt || x.requestedAt || '') < (c.firstSeen || ''); }) || !!(c.seenBefore && c.seenBefore.count);
  if (c.firstSeen) add(c.firstSeen, 'signal', again ? 'Fired again' : 'Fired', '', {kind: 'signal'});
  return rows.sort(function (x, y) { return x.at < y.at ? 1 : x.at > y.at ? -1 : 0; });
}
function cardHistoryHtml(rows, when, state) {
  if (state === 'loading') return '<p class="muted h-note">Loading the history\u2026</p>';
  var note = state === 'failed' ? '<p class="muted h-note">Actions and activity could not be loaded; showing what the card itself records.</p>' : '';
  if (!rows.length) return note + '<p class="muted h-note">Nothing recorded yet.</p>';
  return note + '<ol class="h-rows">' + rows.map(function (r) {
    var chg = r.to ? ' <span class="h-chg">' + (r.from ? '<span class="h-old">' + histEsc(r.from) + '</span> \u2192 ' : '') + '<span class="h-new">' + histEsc(r.to) + '</span></span>' : '';
    var link = r.href ? ' <a class="h-link" href="' + histEsc(r.href) + '" target="_blank" rel="noopener">' + (r.kind === 'run' ? 'Run record' : 'Open') + ' \u2197</a>' : '';
    return '<li class="h-' + histEsc(r.kind || 'row') + '"><time datetime="' + histEsc(r.at) + '" title="' + histEsc(r.at) + '">' + histEsc(when(r.at)) + '</time><span class="h-body"><span class="h-what">' + (r.label ? '<span class="h-l">' + histEsc(r.label) + '</span>' + (r.data ? ': ' : '') : '') +
      (r.data ? '<span data-no-tx>' + histEsc(r.data) + '</span>' : '') + '</span>' + chg +
      ' <span class="h-who"' + (HIST_FIXED[r.who] ? '' : ' data-no-tx') + '>' + histEsc(r.who) + '</span>' + link + '</span></li>';
  }).join('') + '</ol>';
}
