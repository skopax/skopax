var INV = {};
var INV_SPEND = {};
var INV_ROWS = [];
var INV_INFO = null;
var INV_USAGE = null;
var INV_SIG = '';
var POD_OPEN = {};
var INV_OP = null;
var POD_OUT = {done: 'k-good', running: 'k-info', stopped: 'k-muted', skipped: 'k-muted', abandoned: 'k-muted', capped: 'k-warn', failed: 'k-crit', refused: 'k-crit', held: 'k-warn', releasing: 'k-info', discarded: 'k-muted'};
var GUARD_FULL = {}, GUARD_ASKED = {}, GUARD_SAID = {}, GUARD_KEPT = {}, GUARD_FOCUS = null, GUARD_HELD_SAY = 'Investigation held for review.';
function guardAdmin() { return typeof ADMIN_OK !== 'undefined' && ADMIN_OK === true; }
function guardOf(r) { return r && r.guard && typeof r.guard === 'object' ? r.guard : null; }
function guardVerdict(g) {
  if (!g) return '';
  if (g.questions === 'refuse') return 'Guard: refused';
  if (g.questions === 'failed') return 'Guard: refused, the check failed';
  if (g.answers === 'hold') return 'Guard: held';
  if (g.answers === 'failed') return 'Guard: held, the check failed';
  if (g.answers === 'mask') return 'Guard: masked ' + Math.max(0, Math.floor(Number(g.masked) || 0));
  return g.questions === 'pass' || g.answers === 'pass' ? 'Guard: passed' : '';
}
function guardNeedsFull(r) {
  var g = guardOf(r);
  return r.outcome === 'held' || (!!g && (g.questions === 'refuse' || g.answers === 'hold' || g.answers === 'mask'));
}
function heldChats() {
  if (!guardAdmin()) return Promise.resolve([]);
  return fetch('/api/investigations?held=true&limit=200', {credentials: 'same-origin'}).then(function (r) { return r.ok ? r.json() : []; }, function () { return []; }).then(function (rows) {
    return (Array.isArray(rows) ? rows : []).filter(function (r) { return r && r.trigger === 'chat'; });
  });
}
function heldMerge(rows, held) {
  var seen = {};
  rows.forEach(function (r) { if (r && r.id) seen[r.id] = 1; });
  var more = held.filter(function (r) { return r && r.id && !seen[r.id] && (seen[r.id] = 1); });
  Object.keys(GUARD_KEPT).forEach(function (id) { if (!seen[id] && guardAdmin()) { seen[id] = 1; more.push(GUARD_KEPT[id]); } });
  return more.length ? rows.concat(more).sort(function (a, b) { return String(b.startedAt || '').localeCompare(String(a.startedAt || '')); }) : rows;
}
function proposedLeaves(v, out) {
  if (typeof v === 'string' && v.trim()) out.push(v.trim());
  else if (Array.isArray(v)) v.forEach(function (x) { proposedLeaves(x, out); });
  else if (v && typeof v === 'object') Object.keys(v).forEach(function (k) { proposedLeaves(v[k], out); });
  return out;
}
function heldProposedHtml(p) {
  var kinds = p && typeof p === 'object' ? Object.keys(p).filter(function (k) { return proposedLeaves(p[k], []).length; }) : [];
  if (!kinds.length) return '';
  return '<div class="held-proposed"><p class="muted">' + esc(tx('Held proposals')) + '</p><ul data-no-tx>' + kinds.map(function (k) { return '<li>' + esc(proposedLeaves(p[k], []).join(' \u00b7 ')) + '</li>'; }).join('') + '</ul></div>';
}
function guardWhyHtml(why) { return '<span class="guard-why">, ' + esc(tx('because')) + ' <span data-no-tx>' + esc(why) + '</span></span>'; }
function guardLineHtml(r) {
  var full = GUARD_FULL[r.id], v = guardVerdict(guardOf(r) || (full && full.guard));
  if (!v) return '';
  return '<p class="guard-line" data-guard-run="' + esc(r.id) + '"><span class="guard-v">' + esc(tx(v)) + '</span>' + (full && full.reason ? guardWhyHtml(full.reason) : '') + '</p>';
}
function heldTextHtml(full) { return '<p class="held-text" data-no-tx>' + esc(full.text) + '</p>' + heldProposedHtml(full.proposed); }
function heldSaidHtml(id, tag) { return '<' + tag + ' class="held-result" role="status" tabindex="-1">' + esc(GUARD_SAID[id] || '') + '</' + tag + '>'; }
function heldHtml(r, acts) {
  var full = GUARD_FULL[r.id];
  return '<section class="held" data-guard-run="' + esc(r.id) + '"><p class="held-say">' + esc(tx(GUARD_HELD_SAY)) + '</p>' + (full && full.text != null ? heldTextHtml(full) : '') +
    (acts && guardAdmin() ? '<p class="held-act"><button type="button" class="btn small op-btn admin-only held-release">' + esc(tx('Release answer')) + '</button> <button type="button" class="btn sec small op-btn admin-only held-discard">' +
      esc(tx('Discard answer')) + '</button> ' + heldSaidHtml(r.id, 'span') + '</p>' : '') + '</section>';
}
function guardFill(id) {
  var full = GUARD_FULL[id];
  if (!full) return;
  [].forEach.call(document.querySelectorAll('[data-guard-run]'), function (n) {
    if (n.dataset.guardRun !== id) return;
    if (n.classList.contains('guard-line') && full.reason && !n.querySelector('.guard-why')) n.insertAdjacentHTML('beforeend', guardWhyHtml(full.reason));
    var say = n.classList.contains('held') ? n.querySelector('.held-say') : null;
    if (say && full.text != null && !n.querySelector('.held-text')) say.insertAdjacentHTML('afterend', heldTextHtml(full));
    var line = n.parentNode && n.parentNode.querySelector('.guard-line');
    if (n.classList.contains('held') && full.guard && !(line && line.dataset.guardRun === id)) n.insertAdjacentHTML('afterend', guardLineHtml({id: id, guard: full.guard}));
  });
}
function guardLoad(r) {
  if (!guardAdmin() || !guardNeedsFull(r) || GUARD_ASKED[r.id]) return;
  GUARD_ASKED[r.id] = 1;
  fetch('/api/investigations/' + encodeURIComponent(r.id), {credentials: 'same-origin'}).then(function (x) { return x.ok ? x.json() : null; }).then(function (run) {
    if (!run || typeof run !== 'object') { delete GUARD_ASKED[r.id]; return; }
    var held = run.held && typeof run.held === 'object' ? run.held : {}, g = guardOf(run) || {};
    GUARD_FULL[r.id] = {text: typeof held.text === 'string' ? held.text : null, reason: typeof g.reason === 'string' ? g.reason : '', proposed: held.proposed && typeof held.proposed === 'object' ? held.proposed : null,
      guard: guardOf(run) ? {questions: g.questions, answers: g.answers, masked: g.masked} : null};
    guardFill(r.id);
  }, function () { delete GUARD_ASKED[r.id]; });
}
function guardForget(id) { delete GUARD_FULL[id]; delete GUARD_ASKED[id]; }
function heldAct(r, act, b) {
  var box = b.closest('.held'), out = box.querySelector('.held-result'), word = act === 'release' ? 'Release answer' : 'Discard answer';
  if (b.getAttribute('aria-disabled') === 'true') return;
  if (act === 'discard' && !b.dataset.confirm) {
    b.dataset.confirm = '1';
    b.textContent = tx('Click again to discard');
    b.dataset.timer = setTimeout(function () { delete b.dataset.confirm; b.textContent = tx(word); }, 5000);
    return;
  }
  clearTimeout(+b.dataset.timer);
  delete b.dataset.confirm;
  delete GUARD_SAID[r.id];
  out.textContent = '';
  box.querySelectorAll('button').forEach(function (x) { x.disabled = true; });
  var said = function (text) { GUARD_SAID[r.id] = text; GUARD_FOCUS = r.id; out.textContent = text; out.focus(); };
  var keep = function (run) { if (r.trigger === 'chat' && run && typeof run === 'object' && run.id === r.id && run.outcome !== 'held' && run.outcome !== 'releasing') GUARD_KEPT[r.id] = run; };
  fetch('/api/admin/runs/' + encodeURIComponent(r.id) + '/' + act, {method: 'POST', headers: {'Accept': 'application/json'}, credentials: 'same-origin'}).then(function (x) {
    return x.json().then(function (body) { return [x.status, body]; }, function () { return [x.status, null]; });
  }).then(function (got) {
    var code = got[0], body = got[1], detail = body && typeof body.detail === 'string' && body.detail ? body.detail : '';
    if (code >= 200 && code < 300) {
      guardForget(r.id);
      keep(body);
      said(tx(act === 'release' ? 'Answer released' : 'Answer discarded'));
      loadInvestigations();
      return;
    }
    box.querySelectorAll('button').forEach(function (x) { x.disabled = false; });
    b.textContent = tx(word);
    said(detail || tx('Not done, try again'));
    if (code === 409) {
      guardForget(r.id);
      fetch('/api/investigations/' + encodeURIComponent(r.id), {credentials: 'same-origin'}).then(function (x) { return x.ok ? x.json() : null; }, function () { return null; }).then(function (run) {
        keep(run);
        loadInvestigations();
      });
    }
  }, function () {
    box.querySelectorAll('button').forEach(function (x) { x.disabled = false; });
    b.textContent = tx(word);
    said(tx('Not done, try again'));
  });
}
var GUARD_WAS_ADMIN = false;
document.addEventListener('skopax:admin', function () {
  if (guardAdmin() === GUARD_WAS_ADMIN) return;
  GUARD_WAS_ADMIN = guardAdmin();
  if (!GUARD_WAS_ADMIN) {
    GUARD_FULL = {};
    GUARD_ASKED = {};
    GUARD_SAID = {};
    GUARD_KEPT = {};
    document.querySelectorAll('.held-text, .held-proposed, .guard-why, .held-act, .held-result').forEach(function (n) { n.remove(); });
  }
  if (INV_SIG) { renderPodRuns(); loadInvestigations(); }
  Object.keys(INV).forEach(function (k) { if (INV[k] && INV[k].outcome === 'held') guardLoad(INV[k]); });
});
function usdText(v) { return '$' + Number(v || 0).toFixed(2); }
var CARD_NAME_LIMIT = 80;
function cardName(key) {
  var a = (typeof ALL !== 'undefined' ? ALL : []).filter(function (x) { return x.key === key; })[0];
  if (!a) return String(key).split('|').slice(1).join(' · ') || String(key);
  var what = a.msg ? String(a.msg) : a.label + (a.sub ? ' · ' + a.sub : '');
  return svcName(a) + ' / ' + (what.length > CARD_NAME_LIMIT ? what.slice(0, CARD_NAME_LIMIT - 1) + '…' : what);
}
function cardNames(keys) {
  return (keys || []).map(function (k) { return '<span class="hk" title="' + esc(k) + '">' + esc(cardName(k)) + '</span>'; }).join(', ');
}
function invHtml(a) {
  var r = INV[a.key];
  if (!r) return '';
  var head = r.mode === 'shadow' ? 'Investigator (shadow)' : 'Investigator';
  var meta = r.outcome === 'running' ? dayhm(r.startedAt) + ' · running' : dayhm(r.startedAt) + ' · ' + (r.toolCalls || 0) + ' tool calls · ' + usdText(r.usd) + (r.outcome && r.outcome !== 'done' ? ' · ' + r.outcome : '');
  var props = r.mode === 'shadow' ? (r.writes || []).filter(function (w) { return w.key === a.key; }).map(function (w) {
    return '<li>Would have: ' + esc(w.tool) + ' ' + esc(w.summary) + '</li>';
  }).join('') : '';
  var flags = (r.flags || []).length ? '<p class="podflag" title="' + esc(r.flags.map(function (f) { return f.tool + ': ' + f.excerpt; }).join('\n')) + '">Planted-looking text: ' +
    esc(r.flags.map(function (f) { return f.phrase; }).join(', ')) + '</p>' : '';
  var held = r.outcome === 'held';
  if (held) guardLoad(r);
  return blockFold('inv', 'podrun', '<h5>' + esc(head) + '<span class="h5n">' + esc(meta) + '</span></h5>', (held ? heldHtml(r, false) : r.recap ? '<p>' + esc(r.recap) + '</p>' : '') + guardLineHtml(r) +
    (props ? '<ul>' + props + '</ul>' : '') + flags);
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
  var keys = (r.keys || []).length ? r.keys : ['proposal:' + r.id];
  var run = cardRun(keys, function () { return window.__skopaxApi('POST', '/api/investigations/' + encodeURIComponent(r.id) + '/proposal'); }, {quiet: true, late: function () { out.textContent = 'Still working: refresh to see the result'; }});
  if (!run) return;
  b.disabled = true;
  b.textContent = 'Confirming\u2026';
  run.then(function (x) {
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
  var open = (typeof A !== 'undefined' ? A : []).filter(function (a) { return a.status === 'open' && (a.sev === 'warn' || a.sev === 'high' || a.sev === 'crit'); });
  list.innerHTML = open.length ? open.map(function (a) {
    return '<label><input type="checkbox" value="' + esc(a.key) + '"> <span class="tag ' + (SEV[a.sev] || SEV.info)[1] + '">' + esc(a.sev) + '</span> <span>' + esc(svcName(a)) + ' · ' + esc(a.short || a.label || a.key) + '</span></label>';
  }).join('') : '<span class="muted">No open warning, high or critical card to attach.</span>';
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
var INV_BTN_ICON = '<svg class="ib-i" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 5Q11 12 18 13Q11 14 10 21Q9 14 2 13Q9 12 10 5Z"></path><path d="M19 2v6"></path><path d="M16 5h6"></path></svg>';
function invBtnText(btn, text) {
  var t = btn.querySelector('.ib-t');
  if (!t) { btn.innerHTML = INV_BTN_ICON + '<span class="ib-t"></span>'; t = btn.querySelector('.ib-t'); }
  if (t.dataset.said !== text) { t.dataset.said = text; t.textContent = text; }
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
  invBtnText(btn, 'Investigator · ' + usdText(i.spentToday) + ' / ' + usdText(i.cap) + (i.stopped ? ' · stopped' : ''));
  btn.title = (i.stopped ? 'Investigator stopped' : 'Investigator') + ' · spent today ' + usdText(i.spentToday) + ' of the ' + usdText(i.cap) + ' daily cap · ask it a question, stop or resume it';
  btn.classList.toggle('stopped', !!i.stopped);
  state.textContent = esc(i.mode) + ' · ' + usdText(i.spentToday) + ' of ' + usdText(i.cap) + ' today' + (i.stopped ? ' · stopped by ' + esc(i.stopped.by) + ' ' + esc(dayhm(i.stopped.at)) : i.lastRun ? ' · last run ' + esc(dayhm(i.lastRun)) : '');
  btn.classList.toggle('off', switched);
  if (switched) {
    btn.classList.remove('stopped');
    invBtnText(btn, 'Investigator \u00b7 off');
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
    var what = priv ? '<span class="muted">private chat message</span>' : r.ask ? '<b>' + (slackAsk ? 'Slack request' : 'Request') + '</b> by ' + esc(r.ask.by || 'an operator') + ': ' + esc(r.ask.text) + ((r.keys || []).length ? ' · ' + cardNames(r.keys) : '') + thread : cardNames(r.keys);
    var n = (r.screenshots || []).length;
    var answer = r.outcome === 'held' ? heldHtml(r, true) : (priv ? '' : r.recap ? '<p class="' + (r.ask ? 'answer' : 'recap') + '">' + linkify(r.recap).replace(/\n/g, '<br>') + '</p>' : '<p class="muted">' + (r.outcome === 'running' ? 'Working on it…' : 'No answer text.') + '</p>') +
      (GUARD_SAID[r.id] ? heldSaidHtml(r.id, 'p') : '');
    d.innerHTML = '<summary class="chev"><span class="when"><b>' + esc(dayhm(r.startedAt)) + '</b></span><span class="st ' + (POD_OUT[r.outcome] || 'k-muted') + '">' + esc(r.outcome || '') + '</span><span class="tag plain">' + esc(r.mode) + '</span>' +
      '<span class="hw">' + what + '</span><span class="muted">' + usdText(r.usd) + '</span>' + (n ? '<span class="tag plain shots-n">' + n + ' screenshot' + (n === 1 ? '' : 's') + '</span>' : '') + '</summary>' +
      '<div class="hbody">' + answer + guardLineHtml(r) + proposalHtml(r) + shotsHtml(r) + '<p class="hmeta">' + (r.toolCalls || 0) + ' tool calls \u00b7 <a class="record" href="/api/investigations/' + encodeURIComponent(r.id) + '" target="_blank" rel="noopener" data-json="Run record" title="The full run record: tool calls, writes, usage">Run record</a></p></div>';
    d.addEventListener('toggle', function () { if (d.open) { POD_OPEN[r.id] = 1; guardLoad(r); } else delete POD_OPEN[r.id]; });
    if (d.open) guardLoad(r);
    [['release', '.held-release'], ['discard', '.held-discard']].forEach(function (x) {
      var hb = d.querySelector(x[1]);
      if (hb) hb.addEventListener('click', function () { heldAct(r, x[0], hb); });
    });
    var go = d.querySelector('.prop-go');
    if (go) go.addEventListener('click', function () { confirmProposal(r, go); });
    if (keep === r.id) back = go || d.querySelector('.prop-done') || d.querySelector('.prop-result');
    li.appendChild(d);
    list.appendChild(li);
  });
  if (window.__skopaxGate) window.__skopaxGate();
  if (back) back.focus({preventScroll: true});
  if (GUARD_FOCUS) {
    var said = [].filter.call(list.querySelectorAll('.held-result'), function (n) { var d = n.closest('details'), a = d && d.querySelector('a.record'); return a && a.getAttribute('href') === '/api/investigations/' + encodeURIComponent(GUARD_FOCUS); })[0];
    GUARD_FOCUS = null;
    if (said && (!document.activeElement || document.activeElement === document.body)) said.focus({preventScroll: true});
  }
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
    heldChats(),
  ]).then(function (got) {
    var rows = Array.isArray(got[0]) ? got[0] : [];
    rows = heldMerge(rows, got[4]);
    var info = got[1] && !Array.isArray(got[1]) ? got[1].investigate || null : null;
    var usage = got[2] && !Array.isArray(got[2]) && typeof got[2].runs === 'number' ? got[2] : null;
    var cards = got[3] && typeof got[3] === 'object' && !Array.isArray(got[3]) ? got[3] : null;
    var sig = JSON.stringify([rows.map(function (r) { return [r.id, r.outcome, !!(r.proposal && r.proposal.confirmedAt), guardVerdict(guardOf(r))]; }), info, usage && [usage.runs, usage.usd], cards]);
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
