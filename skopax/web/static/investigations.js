var INV = {};
var INV_SPEND = {};
var INV_ROWS = [];
var INV_INFO = null;
var INV_USAGE = null;
var INV_SIG = '';
var POD_OPEN = {};
var INV_OP = null;
var POD_OUT = {done: 'k-good', running: 'k-info', stopped: 'k-muted', skipped: 'k-muted', abandoned: 'k-muted', capped: 'k-warn', failed: 'k-crit', refused: 'k-crit'};
function usdText(v) { return '$' + Number(v || 0).toFixed(2); }
var CARD_NAME_LIMIT = 80;
function cardName(key) {
  var a = (typeof ALL !== 'undefined' ? ALL : []).filter(function (x) { return x.key === key; })[0];
  if (!a) return String(key).split('|').slice(1).join(' · ') || String(key);
  var what = a.msg ? String(a.msg) : a.label + (a.sub ? ' · ' + a.sub : '');
  return a.svc + ' / ' + (what.length > CARD_NAME_LIMIT ? what.slice(0, CARD_NAME_LIMIT - 1) + '…' : what);
}
function cardNames(keys) {
  return (keys || []).map(function (k) { return '<span class="hk" title="' + esc(k) + '">' + esc(cardName(k)) + '</span>'; }).join(', ');
}
function invHtml(a) {
  var r = INV[a.key];
  if (!r) return '';
  var head = r.mode === 'shadow' ? 'Investigator (shadow)' : 'Investigator';
  var meta = dayhm(r.startedAt) + ' · ' + (r.toolCalls || 0) + ' tool calls · ' + usdText(r.usd) + (r.outcome && r.outcome !== 'done' ? ' · ' + r.outcome : '');
  var props = r.mode === 'shadow' ? (r.writes || []).filter(function (w) { return w.key === a.key; }).map(function (w) {
    return '<li>Would have: ' + esc(w.tool) + ' ' + esc(w.summary) + '</li>';
  }).join('') : '';
  var flags = (r.flags || []).length ? '<p class="podflag" title="' + esc(r.flags.map(function (f) { return f.tool + ': ' + f.excerpt; }).join('\n')) + '">Planted-looking text: ' +
    esc(r.flags.map(function (f) { return f.phrase; }).join(', ')) + '</p>' : '';
  return '<section class="podrun"><h5>' + esc(head) + '<span class="h5n">' + esc(meta) + '</span></h5>' + (r.recap ? '<p>' + esc(r.recap) + '</p>' : '') +
    (props ? '<ul>' + props + '</ul>' : '') + flags + '</section>';
}
function invOperator() { return typeof window.__boardLock === 'function' && !window.__boardLock(); }
var PROP_HEADS = {done: 'Confirmed', failed: 'Failed', timeout: 'Timed out', declined: 'Declined'};
var PROP_MARKS = {failed: 'bad', timeout: 'warn', declined: 'warn'};
function propOutcome(res) {
  if (!res || typeof res !== 'object') return 'done';
  return res.outcome === 'failed' || res.outcome === 'timeout' ? res.outcome : res.ok === false ? 'declined' : 'done';
}
function propClass(base, outcome) { return base + (PROP_MARKS[outcome] ? ' ' + PROP_MARKS[outcome] : ''); }
function proposalHtml(r) {
  var p = r.proposal;
  if (!p || typeof p !== 'object') return '';
  var res = p.result && typeof p.result === 'object' ? p.result : null, outcome = propOutcome(res);
  var head = outcome === 'done' ? '' : '<b>' + esc(PROP_HEADS[outcome]) + '</b> \u00b7 ';
  var done = p.confirmedAt ? '<p class="' + propClass('prop-done', outcome) + '" tabindex="-1">' + head + esc('Confirmed by ' + (p.confirmedBy || '?') + ' ' + dayhm(p.confirmedAt) + (res && res.summary ? ': ' + res.summary : '')) + '</p>' : '';
  var go = !p.confirmedAt && INV_OP ? '<p class="prop-act"><button type="button" class="btn small prop-go">Confirm</button> <span class="prop-result" role="status" tabindex="-1"></span></p>' : '';
  return '<section class="proposal" data-run="' + esc(r.id) + '"><p class="prop-title"><b>Proposed:</b> ' + esc(p.title) + '</p><p class="prop-text">' + esc(p.text) + '</p>' + done + go + '</section>';
}
function heldProposal() {
  var f = document.activeElement, box = f && f.closest ? f.closest('#pod-list .proposal') : null;
  return box ? box.dataset.run : null;
}
function confirmProposal(r, b) {
  var out = b.parentNode.querySelector('.prop-result');
  b.disabled = true;
  b.textContent = 'Confirming\u2026';
  window.__skopaxApi('POST', '/api/investigations/' + encodeURIComponent(r.id) + '/proposal').then(function (x) {
    r.proposal = Object.assign({}, r.proposal, {confirmedAt: new Date().toISOString(), confirmedBy: ME_USER, result: x});
    var outcome = propOutcome(x);
    b.remove();
    out.className = propClass('prop-result', outcome);
    out.innerHTML = '<b>' + esc(PROP_HEADS[outcome]) + '</b>' + (x && x.summary ? ' \u00b7 ' + esc(String(x.summary)) : '');
    out.focus();
  }, function (e) {
    if (e && e.code === 409 && e.detail) { b.remove(); out.textContent = e.detail; out.focus(); loadInvestigations(); return; }
    b.disabled = false;
    b.textContent = 'Confirm';
    out.textContent = e && e.detail ? e.detail : 'Not confirmed, try again';
  });
}
function podToggle(b) {
  var i = INV_INFO || {};
  if (b.getAttribute('aria-disabled') === 'true') return;
  if (!b.dataset.confirm) {
    b.dataset.confirm = i.stopped ? 'resume' : 'stop';
    b.textContent = 'Click again to confirm';
    b.dataset.timer = setTimeout(function () { delete b.dataset.confirm; b.textContent = (INV_INFO || {}).stopped ? 'Resume' : 'Stop investigator'; }, 5000);
    return;
  }
  var stop = b.dataset.confirm === 'stop';
  clearTimeout(+b.dataset.timer);
  delete b.dataset.confirm;
  b.disabled = true;
  window.__skopaxApi('PUT', '/api/investigate', {stopped: stop}).then(function (info) { INV_INFO = info; b.disabled = false; renderPodRuns(); liveness(); }, function () { b.disabled = false; b.textContent = 'Not saved, try again'; });
}
function askDialog() {
  var wrap = document.getElementById('ask-wrap'), text = document.getElementById('ask-text'), list = document.getElementById('ask-cards'), err = document.getElementById('ask-err');
  var yes = document.getElementById('ask-yes'), no = document.getElementById('ask-no');
  if (!wrap || wrap.dataset.open) return;
  wrap.dataset.open = '1';
  err.hidden = true;
  err.textContent = '';
  var open = (typeof A !== 'undefined' ? A : []).filter(function (a) { return a.status === 'open' && (a.sev === 'warn' || a.sev === 'crit'); });
  list.innerHTML = open.length ? open.map(function (a) {
    return '<label><input type="checkbox" value="' + esc(a.key) + '"> <span class="tag ' + (SEV[a.sev] || SEV.info)[1] + '">' + esc(a.sev) + '</span> <span>' + esc(a.svc || '') + ' · ' + esc(a.short || a.label || a.key) + '</span></label>';
  }).join('') : '<span class="muted">No open warning or critical card to attach.</span>';
  function close() {
    wrap.hidden = true;
    delete wrap.dataset.open;
    yes.disabled = false;
    yes.textContent = 'Ask in chat';
    yes.removeEventListener('click', onYes); no.removeEventListener('click', onNo); wrap.removeEventListener('click', onWrap); document.removeEventListener('keydown', onKey);
  }
  function onNo() { close(); }
  function onWrap(e) { if (e.target === wrap) close(); }
  function onKey(e) { if (e.key === 'Escape') close(); }
  function onYes() {
    var q = text.value.trim();
    if (!q) { err.textContent = 'Type the question first.'; err.hidden = false; text.focus(); return; }
    var cards = Array.prototype.map.call(list.querySelectorAll('input:checked'), function (c) { return c.value; });
    yes.disabled = true;
    yes.textContent = 'Opening the chat…';
    window.__skopaxApi('POST', '/api/chats', {text: q, cards: cards}).then(function (x) {
      text.value = '';
      close();
      if (x && x.id) location.hash = '#t=chat/thread/' + encodeURIComponent(x.id);
    }, function (e) {
      yes.disabled = false;
      yes.textContent = 'Ask in chat';
      err.textContent = chatAskError(e);
      err.hidden = false;
    });
  }
  yes.addEventListener('click', onYes); no.addEventListener('click', onNo); wrap.addEventListener('click', onWrap); document.addEventListener('keydown', onKey);
  wrap.hidden = false;
  text.focus();
}
function renderInvMenu() {
  var btn = document.getElementById('inv-btn'), pop = document.getElementById('inv-pop'), state = document.getElementById('inv-state'), ask = document.getElementById('inv-ask'), stop = document.getElementById('inv-stop'), chat = document.getElementById('inv-chat');
  if (!btn || !pop) return;
  if (INV_OP !== null && INV_OP !== invOperator() && INV_ROWS.some(function (r) { return r.proposal && !r.proposal.confirmedAt; })) { renderPodRuns(); return; }
  var i = INV_INFO || {};
  var on = !!(i.mode && i.mode !== 'off'), switched = ME_FEATURES.investigator === false, chatOff = !(ME_CHAT && ME_FEATURES.chat !== false);
  btn.hidden = !on;
  if (!on) { pop.hidden = true; btn.setAttribute('aria-expanded', 'false'); return; }
  ask.hidden = chatOff;
  if (chat) chat.hidden = chatOff;
  btn.textContent = 'Investigator · ' + usdText(i.spentToday) + ' / ' + usdText(i.cap) + (i.stopped ? ' · stopped' : '');
  btn.title = (i.stopped ? 'Investigator stopped' : 'Investigator') + ' · spent today ' + usdText(i.spentToday) + ' of the ' + usdText(i.cap) + ' daily cap · ask it a question, stop or resume it';
  btn.classList.toggle('stopped', !!i.stopped);
  state.textContent = esc(i.mode) + ' · ' + usdText(i.spentToday) + ' of ' + usdText(i.cap) + ' today' + (i.stopped ? ' · stopped by ' + esc(i.stopped.by) + ' ' + esc(dayhm(i.stopped.at)) : i.lastRun ? ' · last run ' + esc(dayhm(i.lastRun)) : '');
  btn.classList.toggle('off', switched);
  if (switched) {
    btn.classList.remove('stopped');
    btn.textContent = 'Investigator \u00b7 off';
    btn.title = state.textContent = typeof ME_OFF.investigator === 'string' ? ME_OFF.investigator : '';
  }
  if (!stop.dataset.confirm) stop.textContent = i.stopped ? 'Resume' : 'Stop investigator';
  stop.dataset.tip = stop.title = i.stopped ? 'Let the investigator run again' : 'Stops new runs at once; a running one ends after its current turn';
  ask.dataset.tip = ask.title = 'Opens a private chat with one question and the cards you pick';
  if (chat) chat.dataset.tip = chat.title = 'Opens Chat: talk with the investigator in a private chat';
  if (!btn.dataset.bound) {
    btn.dataset.bound = '1';
    function close() { pop.hidden = true; btn.setAttribute('aria-expanded', 'false'); }
    btn.addEventListener('click', function () { pop.hidden = !pop.hidden; btn.setAttribute('aria-expanded', pop.hidden ? 'false' : 'true'); if (window.__skopaxTipHide) window.__skopaxTipHide(); });
    document.addEventListener('click', function (e) { if (!pop.hidden && !e.target.closest('#inv-menu')) close(); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });
    ask.addEventListener('click', function () {
      if (ask.getAttribute('aria-disabled') === 'true') return;
      close();
      askDialog();
    });
    if (chat) chat.addEventListener('click', function () {
      if (chat.getAttribute('aria-disabled') === 'true') return;
      close();
      location.hash = '#t=chat';
    });
    stop.addEventListener('click', function () { podToggle(stop); });
    document.getElementById('inv-more').addEventListener('click', close);
  }
  if (window.__skopaxGate) window.__skopaxGate();
}
function podUsageLine() {
  var u = INV_USAGE;
  var head = u && u.runs ? esc('Usage · 7 d · ' + u.runs + ' runs · ' + usdText(u.usd) + (u.cacheShare != null ? ' · cache ' + Math.round(u.cacheShare * 100) + ' %' : '')) : '';
  if (!head && !COSTS_OP) return null;
  var usage = el('p', 'pod-usage', head + (head && COSTS_OP ? ' · ' : '') + (COSTS_OP ? '<a class="costs-link" href="#t=costs">Open Costs ›</a>' : ''));
  var go = usage.querySelector('a');
  if (go) go.addEventListener('click', function (e) {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button) return;
    e.preventDefault();
    showTab('costs');
  });
  return usage;
}
function refreshPodUsage() {
  var ctl = document.getElementById('pod-ctl'), state = ctl && ctl.querySelector('.pod-state');
  if (!INV_SIG || !state) return;
  var old = ctl.querySelector('.pod-usage');
  if (old) old.remove();
  var usage = podUsageLine();
  if (usage) state.after(usage);
}
function renderPodRuns() {
  INV_OP = invOperator();
  renderInvMenu();
  var ctl = document.getElementById('pod-ctl'), list = document.getElementById('pod-list');
  if (!ctl || !list) return;
  var i = INV_INFO || {};
  ctl.innerHTML = '';
  var line = el('span', 'pod-state', i.mode ? esc(i.mode) + ' · ' + usdText(i.spentToday) + ' of ' + usdText(i.cap) + ' today' + (i.stopped ? ' · Stopped by ' + esc(i.stopped.by) + ' ' + esc(dayhm(i.stopped.at)) : '') : 'not set up');
  ctl.append(line);
  var usage = podUsageLine();
  if (usage) ctl.append(usage);
  if (i.mode && i.mode !== 'off') {
    var b = el('button', 'btn sec small op-btn admin-only');
    b.type = 'button';
    b.textContent = i.stopped ? 'Resume' : 'Stop investigator';
    b.dataset.tip = b.title = i.stopped ? 'Let the investigator run again' : 'Stops new runs at once; a running one ends after its current turn';
    b.addEventListener('click', function () { podToggle(b); });
    ctl.append(b);
    var ask = el('button', 'btn small op-btn');
    ask.type = 'button';
    ask.id = 'ask-btn';
    ask.textContent = 'Ask the investigator';
    ask.dataset.tip = ask.title = 'Opens a private chat with one question and the cards you pick';
    ask.addEventListener('click', function () { if (ask.getAttribute('aria-disabled') !== 'true') askDialog(); });
    ctl.append(ask);
  }
  var keep = heldProposal(), back = null;
  list.innerHTML = '';
  var rn = document.getElementById('hist-runs-n');
  if (rn) rn.textContent = INV_ROWS.length ? String(Math.min(INV_ROWS.length, 30)) : '';
  if (!INV_ROWS.length) list.appendChild(el('li', 'muted', 'No investigations yet.'));
  INV_ROWS.slice(0, 30).forEach(function (r) {
    var li = el('li', 'hrow'), d = el('details');
    if (POD_OPEN[r.id]) d.open = true;
    var slackAsk = !!(r.ask && r.ask.source === 'slack'), priv = !!(r.ask && r.ask.private);
    var thread = slackAsk && slackThread(r.ask.thread) ? ' · <a href="' + esc(r.ask.thread) + '" target="_blank" rel="noopener">thread</a>' : '';
    var what = priv ? '<span class="muted">private chat message</span>' : r.ask ? '<b>' + (slackAsk ? 'Slack ask' : 'Ask') + '</b> by ' + esc(r.ask.by || 'an operator') + ': ' + esc(r.ask.text) + ((r.keys || []).length ? ' · ' + cardNames(r.keys) : '') + thread : cardNames(r.keys);
    var n = (r.screenshots || []).length;
    var answer = priv ? '' : r.recap ? '<p class="' + (r.ask ? 'answer' : 'recap') + '">' + linkify(r.recap).replace(/\n/g, '<br>') + '</p>' : '<p class="muted">' + (r.outcome === 'running' ? 'Working on it…' : 'No answer text.') + '</p>';
    d.innerHTML = '<summary class="chev"><span class="when"><b>' + esc(dayhm(r.startedAt)) + '</b></span><span class="st ' + (POD_OUT[r.outcome] || 'k-muted') + '">' + esc(r.outcome || '') + '</span><span class="tag plain">' + esc(r.mode) + '</span>' +
      '<span class="hw">' + what + '</span><span class="muted">' + usdText(r.usd) + '</span>' + (n ? '<span class="tag plain shots-n">' + n + ' screenshot' + (n === 1 ? '' : 's') + '</span>' : '') + '</summary>' +
      '<div class="hbody">' + answer + proposalHtml(r) + shotsHtml(r) + '<p class="hmeta">' + (r.toolCalls || 0) + ' tool calls \u00b7 <a class="record" href="/api/investigations/' + encodeURIComponent(r.id) + '" target="_blank" rel="noopener" data-json="Run record" title="The full run record: tool calls, writes, usage">Run record</a></p></div>';
    d.addEventListener('toggle', function () { if (d.open) POD_OPEN[r.id] = 1; else delete POD_OPEN[r.id]; });
    var go = d.querySelector('.prop-go');
    if (go) go.addEventListener('click', function () { confirmProposal(r, go); });
    if (keep === r.id) back = go || d.querySelector('.prop-done') || d.querySelector('.prop-result');
    li.appendChild(d);
    list.appendChild(li);
  });
  if (window.__skopaxGate) window.__skopaxGate();
  if (back) back.focus({preventScroll: true});
}
var PLUGIN_SIG = null;
function pluginTilesChanged(list) {
  var sig = JSON.stringify(list);
  if (sig === PLUGIN_SIG) return;
  PLUGIN_SIG = sig;
  if (setPlugins(list)) repaintCards();
  renderMeta();
  liveness();
  if (typeof TOOLS !== 'undefined' && TOOLS && Array.isArray(TOOLS.tools)) renderTools(TOOLS);
}
function loadInvestigations() {
  Promise.all([
    fetch('/api/investigations?limit=100&chats=false', {credentials: 'same-origin'}).then(function (r) { return r.ok ? r.json() : []; }),
    fetch('/api/server', {credentials: 'same-origin'}).then(function (r) { return r.ok ? r.json() : {}; }),
    fetch('/api/investigator/usage?days=7', {credentials: 'same-origin'}).then(function (r) { return r.ok ? r.json() : null; }, function () { return null; }),
    fetch('/api/card-runs', {credentials: 'same-origin'}).then(function (r) { return r.ok ? r.json() : null; }, function () { return null; }),
  ]).then(function (got) {
    var rows = Array.isArray(got[0]) ? got[0] : [];
    var info = got[1] && !Array.isArray(got[1]) ? got[1].investigate || null : null;
    var usage = got[2] && !Array.isArray(got[2]) && typeof got[2].runs === 'number' ? got[2] : null;
    var cards = got[3] && typeof got[3] === 'object' && !Array.isArray(got[3]) ? got[3] : null;
    var sig = JSON.stringify([rows.map(function (r) { return [r.id, r.outcome, !!(r.proposal && r.proposal.confirmedAt)]; }), info, usage && [usage.runs, usage.usd], cards]);
    if (got[1] && !Array.isArray(got[1]) && Array.isArray(got[1].plugins)) pluginTilesChanged(got[1].plugins);
    SERVER_TZ = got[1] && !Array.isArray(got[1]) && typeof got[1].tz === 'string' ? got[1].tz : '';
    INV_USAGE = usage;
    INV_ROWS = rows;
    INV_INFO = info;
    costsCount();
    INV = {};
    INV_SPEND = {};
    if (cards) Object.keys(cards).forEach(function (k) {
      var c = cards[k] || {};
      if (c.latest) INV[k] = c.latest;
      INV_SPEND[k] = {n: Number(c.n || 0), usd: Number(c.usd || 0)};
    });
    else rows.forEach(function (r) { (r.keys || []).forEach(function (k) {
      if (!INV[k]) INV[k] = r;
      var s = INV_SPEND[k] || (INV_SPEND[k] = {n: 0, usd: 0});
      s.n += 1; s.usd += Number(r.usd || 0);
    }); });
    if (sig !== INV_SIG) { INV_SIG = sig; bumpRender(); renderPodRuns(); liveness(); repaintCards(); }
  }, function () {});
}
var CARDS_RETRY = 0;
function repaintCards() {
  clearTimeout(CARDS_RETRY);
  var f = document.activeElement;
  if ((f && /^(INPUT|TEXTAREA|SELECT)$/.test(f.tagName) && f.closest('#anoms') && document.hasFocus()) || document.querySelector('#anoms [data-confirm]')) { CARDS_RETRY = setTimeout(repaintCards, 1000); return; }
  var y = window.scrollY, cards = cardsState(), seen = {};
  ALL.forEach(function (a) { seen[a.key] = 1; });
  setFilter(filter);
  restoreCards(cards, seen);
  window.scrollTo(0, y);
}
