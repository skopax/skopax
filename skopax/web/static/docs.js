var DOCS_SEQ = 0, DOCS_FOLDS = null;
var DOCS_SEV = {warn: 'While Slack auto-send is on, cards from Warning up are posted to the Slack channel on their own.',
  high: 'While Slack auto-send is on, cards from High up are posted to the Slack channel on their own.',
  crit: 'While Slack auto-send is on, Critical cards are posted to the Slack channel on their own.',
  off: 'Post immediately from is Off: no card is posted to Slack on its own. Send to Slack posts one by hand.'};
var DOCS_MODE = {live: 'It runs in live mode now.', shadow: 'It runs in shadow mode now.', off: 'It is off now.'};
var DOCS_HALT = {off: 'The AI investigator is turned off in Admin > Features now.', stopped: 'It is stopped now: nothing runs until someone turns it back on.',
  capped: 'It reached today\'s spend limit and starts again tomorrow.'};
var DOCS_PROBLEM = {key: 'It does not run: there is no AI key yet. Add one in Admin > Investigator.', model: 'It does not run: the model is not known. Pick one in Admin > Investigator.'};
var DOCS_DISABLED = 'It does not run now. See Admin > Investigator.';
var DOCS_PLAIN = {};

function docsSay(id, text, part) {
  var li = document.getElementById(id), spans = li ? li.querySelectorAll(':scope > span:not(.docs-days)') : [];
  var s = li && part ? li.querySelector(part) : spans[spans.length - 1];
  if (!s) return;
  var at = id + (part || '');
  if (!(at in DOCS_PLAIN)) DOCS_PLAIN[at] = s.textContent;
  var want = text || DOCS_PLAIN[at];
  if (s.dataset.en === want) return;
  s.dataset.en = want;
  s.textContent = want;
}
function docsNum(items, key) {
  var it = (items || []).filter(function (i) { return i && i.key === key; })[0];
  var n = it && String(it.value).trim() !== '' ? Number(it.value) : NaN;
  return isFinite(n) && n >= 0 ? n : null;
}
function docsText(items, key) {
  var it = (items || []).filter(function (i) { return i && i.key === key; })[0];
  return it ? String(it.value || '').trim() : '';
}
var DOCS_AUTO_SEVS = [['crit', 'critical'], ['high', 'high'], ['warn', 'warning']];
function docsAuto(items) {
  var hours = DOCS_AUTO_SEVS.map(function (s) { return docsNum(items, 'core.auto-resolve-hours-' + s[0]); });
  if (hours[1] === null) hours[1] = hours[2];
  if (hours.some(function (h) { return h === null; })) return '';
  if (!hours.some(Boolean)) return 'Stopped cards do not close by themselves: someone has to resolve them.';
  return 'A stopped card that nobody acted on closes by itself: ' + DOCS_AUTO_SEVS.map(function (s, n) { return s[1] + ' cards ' + (hours[n] ? 'after ' + hours[n] + ' h' : 'never'); }).join(', ') + '.';
}
function docsDays(items) {
  var days = docsText(items, 'core.business-days');
  if (!DOCS_AUTO_SEVS.some(function (s) { return docsNum(items, 'core.auto-resolve-hours-' + s[0]); })) return '';
  return days === 'true' ? 'Only Monday to Friday counts.' : days === 'false' ? 'Every day counts.' : '';
}
function docsRemind(items) {
  var h = docsNum(items, 'slack.remind-hours');
  if (h === null) return '';
  return h ? 'A card posted to Slack that nobody has started, resolved or muted gets a reminder in its Slack thread every ' + h + ' h. A new value applies after a restart.' : 'Slack reminders are off (Admin > Settings > Slack).';
}
function docsAdmin(d) {
  var items = d && Array.isArray(d.items) ? d.items : null;
  docsSay('docs-auto', items ? docsAuto(items) : '');
  docsSay('docs-auto', items ? docsDays(items) : '', '.docs-days');
  docsSay('docs-remind', items ? docsRemind(items) : '');
  docsSay('docs-postsev', items ? DOCS_SEV[docsText(items, 'slack.post-sev')] || '' : '');
}
function docsMode(d, cap) {
  if (!d) return '';
  if (d.halted === 'off') return DOCS_HALT.off;
  if (d.mode === 'off') return DOCS_MODE.off;
  if (DOCS_PROBLEM[d.problem]) return DOCS_PROBLEM[d.problem];
  if (d.enabled === false) return DOCS_DISABLED;
  if (d.halted === 'capped' && cap === 0) return '';
  return DOCS_HALT[d.halted] || DOCS_MODE[d.mode] || '';
}
function docsInv(d) {
  var l = d && d.limits, cap = l && typeof l.cap === 'number' && isFinite(l.cap) ? l.cap : null;
  docsSay('docs-mode', docsMode(d, cap));
  if (cap === null) { docsSay('docs-cap', ''); return; }
  var spent = typeof l.spentToday === 'number' && isFinite(l.spentToday) ? l.spentToday : 0;
  docsSay('docs-cap', cap > 0 ? 'The daily limit is $' + cap.toFixed(2) + ', and $' + spent.toFixed(2) + ' is spent today. Once it is reached, the investigator stops until the next day.'
    : 'The daily limit is $' + cap.toFixed(2) + ', so the investigator does not run.');
}
function docsArchive() {
  var h = typeof D !== 'undefined' && D && typeof D.archiveHours === 'number' && D.archiveHours > 0 ? D.archiveHours : null;
  docsSay('docs-arch', h ? 'cards that are not firing, or muted, with no change for ' + h + ' hours; they are hidden everywhere else.' : '');
}
function docsSlack() {
  docsSay('docs-slack', ME_FEATURES['slack-listener'] === true ? 'In Slack, mention the ' + brandName() + ' app with a question in any channel it is in.' : '');
}
function docsShow() {
  var seq = ++DOCS_SEQ;
  docsArchive();
  docsSlack();
  fetch('/api/investigator/status', {credentials: 'same-origin'}).then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
    if (seq === DOCS_SEQ) docsInv(d);
  }, function () { if (seq === DOCS_SEQ) docsInv(null); });
  if (typeof ADMIN_OK === 'undefined' || !ADMIN_OK) { docsAdmin(null); return; }
  var got = ADMIN && Array.isArray(ADMIN.items) ? Promise.resolve(ADMIN) : adminApi('GET', '/api/admin/settings');
  got.then(function (d) {
    if (d && !ADMIN && ADMIN_OK) ADMIN = d;
    if (seq === DOCS_SEQ) docsAdmin(ADMIN_OK ? d : null);
  }, function () { if (seq === DOCS_SEQ) docsAdmin(null); });
}
function docsFind() {
  var input = document.getElementById('docs-find'), none = document.getElementById('docs-none');
  if (!input || !none) return;
  var q = input.value.trim().toLowerCase(), secs = [].slice.call(document.querySelectorAll('#docs-secs > details.docs-sec')), any = false;
  if (q && !DOCS_FOLDS) DOCS_FOLDS = secs.map(function (d) { return d.open; });
  secs.forEach(function (d) {
    var head = !q || d.querySelector('summary').textContent.toLowerCase().indexOf(q) >= 0, hits = 0;
    d.querySelectorAll('li').forEach(function (li) {
      var said = [].map.call(li.childNodes, function (n) { return n.nodeName === 'A' ? '' : n.textContent; }).join(' ');
      var on = head || said.toLowerCase().indexOf(q) >= 0;
      li.hidden = !on;
      if (on) hits++;
    });
    d.hidden = !!q && !hits;
    if (q && hits) d.open = true;
    if (hits) any = true;
  });
  if (!q && DOCS_FOLDS) { secs.forEach(function (d, i) { d.open = DOCS_FOLDS[i]; }); DOCS_FOLDS = null; }
  none.hidden = !q || any;
}
(function () {
  var input = document.getElementById('docs-find');
  if (!input) return;
  input.addEventListener('input', docsFind);
  input.addEventListener('keydown', function (e) { if (e.key === 'Escape' && input.value) { e.preventDefault(); input.value = ''; docsFind(); } });
  document.addEventListener('skopax:admin', function () { if (curTab === 'docs') docsShow(); });
})();
