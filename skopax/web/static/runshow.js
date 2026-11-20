var RUN_BOARD = null;
var RUN_DONE = {};
var RUN_KEEP_MS = 15000;
function runEsc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); }
function runClock(iso) {
  var d = new Date(iso);
  return isNaN(d) ? '' : d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hour12: false});
}
function runMinutes(iso) { var t = Date.parse(iso); return isNaN(t) ? 0 : Math.max(0, Math.floor((Date.now() - t) / 60000)); }
function runAge(iso, lead) {
  var m = runMinutes(iso);
  return (lead ? lead + ' ' : '') + (m < 60 ? Math.max(1, m) + ' min' : Math.floor(m / 60) + ' h ' + (m % 60) + ' min');
}
function runUsd(v) { return '$' + Number(v || 0).toFixed(2); }
function runCalls(n) { n = Number(n || 0); return n + (n === 1 ? ' tool call' : ' tool calls'); }
function runsNow(d) { return ((d && d.rooms) || []).map(function (r) { return r.run; }).filter(Boolean).concat((d && d.unplaced) || []); }
function runOnCard(r) { return !!r && r.trigger !== 'chat'; }
function runHas(r, key) { return runOnCard(r) && (r.keys || []).indexOf(key) >= 0; }
function runTake(d) {
  var ids = {}, done = {}, now = Date.now();
  runsNow(d).forEach(function (r) { ids[r.id] = 1; });
  if (d) Object.keys(RUN_DONE).forEach(function (k) {
    var held = RUN_DONE[k];
    if (now - held.at < RUN_KEEP_MS && !runsNow(d).some(function (r) { return runHas(r, k); })) done[k] = held;
  });
  runsNow(RUN_BOARD).filter(runOnCard).forEach(function (r) {
    if (!d || ids[r.id]) return;
    var end = (d.recent || []).filter(function (x) { return x.id === r.id; })[0] || Object.assign({}, r, {outcome: null});
    (r.keys || []).forEach(function (k) { done[k] = {run: end, at: now}; });
  });
  RUN_DONE = done;
  RUN_BOARD = d || null;
}
function runLiveUsd(key) {
  var run = runsNow(RUN_BOARD).filter(function (r) { return runHas(r, key); })[0];
  return run && run.usd != null ? Number(run.usd) : 0;
}
function runFor(key) {
  if (!key) return null;
  var run = runsNow(RUN_BOARD).filter(function (r) { return runHas(r, key); })[0];
  if (run) return {state: 'run', run: run};
  var w = (RUN_BOARD && RUN_BOARD.waiting) || [];
  for (var i = 0; i < w.length; i++) if ((w[i].cards || []).indexOf(key) >= 0) return {state: 'queued', ahead: i, at: w[i].at};
  return RUN_DONE[key] ? {state: 'done', run: RUN_DONE[key].run} : null;
}
function runIndHtml(s) {
  return s && s.state !== 'done' ? '<span class="run-ind' + (s.state === 'queued' ? ' run-q' : '') + '" aria-hidden="true"></span>' : '';
}
function runBanner(key) {
  var s = runFor(key);
  if (!s) return '';
  var r = s.run || {}, head, cls, bits = [], step = '', rec = s.state !== 'queued' && r.id;
  if (s.state === 'run') {
    head = r.ask ? 'Asking' : 'Investigating';
    cls = 'run-on';
    bits = ['started ' + runClock(r.startedAt), runAge(r.startedAt), runCalls(r.toolCalls)];
    if (r.usd != null) bits.push(runUsd(r.usd));
    if (r.lastTool) step = '<span class="run-step"><span>last tool</span> <code data-no-tx>' + runEsc(r.lastTool) + '</code></span>';
  } else if (s.state === 'queued') {
    head = s.ahead ? 'Queued, ' + s.ahead + ' ahead' : 'Queued, next';
    cls = 'run-wait';
    if (s.at) bits = [runAge(s.at, 'waiting')];
  } else if (r.outcome && r.outcome !== 'done') {
    head = 'Investigation failed';
    cls = 'run-bad';
    step = '<span class="run-why" data-no-tx>' + runEsc(r.error || r.outcome) + '</span>';
  } else {
    head = r.outcome === 'done' ? 'Investigation done' : 'Investigation finished';
    cls = 'run-ok';
    bits = [runCalls(r.toolCalls)];
    if (r.usd != null) bits.push(runUsd(r.usd));
    bits.push('see the findings below');
  }
  var sig = [cls, head, bits.join('|'), step, rec || ''].join('|');
  return '<div class="run-banner ' + cls + '" data-say="' + runEsc(head) + '" data-sig="' + runEsc(sig) + '">' + runIndHtml(s) + '<b class="run-say" role="status" aria-live="polite">' + runEsc(head) + '</b>' +
    '<span class="run-bits">' + bits.map(function (b) { return '<span>' + runEsc(b) + '</span>'; }).join('') + step + '</span>' +
    (rec ? '<a class="run-rec" href="/api/investigations/' + encodeURIComponent(r.id) + '" target="_blank" rel="noopener" data-json="Run record" title="The full run record: tool calls, writes, usage">Run record</a>' : '') + '</div>';
}
function runPatch(host, after, key) {
  var old = [].filter.call(host.children, function (c) { return c.classList.contains('run-banner'); })[0], html = runBanner(key);
  if (!html) { if (old) old.remove(); return; }
  var box = document.createElement('div');
  box.innerHTML = html;
  var nb = box.firstChild;
  if (!old) {
    var said = nb.querySelector('.run-say'), words = said.textContent;
    said.textContent = '';
    after.after(nb);
    setTimeout(function () { if (said.isConnected && !said.textContent) said.textContent = words; }, 50);
    return;
  }
  if (old.dataset.sig === nb.dataset.sig) return;
  old.dataset.sig = nb.dataset.sig;
  if (old.dataset.say !== nb.dataset.say) {
    old.dataset.say = nb.dataset.say;
    old.querySelector('.run-say').textContent = nb.querySelector('.run-say').textContent;
  }
  old.className = nb.className;
  var rec = old.querySelector('.run-rec'), nrec = nb.querySelector('.run-rec');
  var keep = rec && nrec && rec.getAttribute('href') === nrec.getAttribute('href') ? rec : null;
  [].slice.call(old.children).forEach(function (c) { if (!c.classList.contains('run-say') && c !== keep) c.remove(); });
  var say = old.querySelector('.run-say');
  [].slice.call(nb.children).forEach(function (c) {
    if (c.classList.contains('run-ind')) old.insertBefore(c, say);
    else if (c.classList.contains('run-rec')) { if (!keep) old.appendChild(c); }
    else if (!c.classList.contains('run-say')) old.insertBefore(c, keep);
  });
}
function runDots(root) {
  [].forEach.call(root.querySelectorAll('.tag.tri-inv'), function (t) {
    var at = t.closest('[data-key]'), s = at ? runFor(at.dataset.key) : null, html = runIndHtml(s), dot = t.querySelector('.run-ind');
    if (!html) { if (dot) dot.remove(); return; }
    if (!dot) { t.insertAdjacentHTML('afterbegin', html); return; }
    dot.classList.toggle('run-q', s.state === 'queued');
  });
}
function runSpendText(key) {
  var spend = (typeof INV_SPEND !== 'undefined' ? INV_SPEND : {})[key];
  return runUsd((spend ? Number(spend.usd || 0) : 0) + runLiveUsd(key));
}
function runCardNum(key) {
  var all = typeof ALL !== 'undefined' ? ALL : [], a = all.filter(function (x) { return x.key === key; })[0];
  return a && a.num ? a.num : null;
}
function runPill(d) {
  var p = document.getElementById('run-pill');
  if (!p) return;
  var runs = runsNow(d), q = ((d && d.waiting) || []).length, keys = [];
  runs.filter(runOnCard).forEach(function (r) { (r.keys || []).forEach(function (k) { if (keys.indexOf(k) < 0) keys.push(k); }); });
  var say = '', bits = [], kind = 'run-on', href = '#t=inv', tip = 'Opens the Investigator page', n = '';
  if (runs.length) {
    if (keys.length === 1) {
      var num = runCardNum(keys[0]), first = runs.filter(function (r) { return runHas(r, keys[0]); }).map(function (r) { return r.startedAt; }).sort()[0];
      say = num ? 'Investigating #' + num : 'Investigating 1 card';
      bits.push(runAge(first));
      if (runs.length === 1) { href = '#a=' + encodeURIComponent(keys[0]); tip = 'Opens the card'; }
    } else if (keys.length > 1) {
      say = 'Investigating ' + keys.length + ' cards';
    } else {
      say = 'Investigator running';
      if (runs.length > 1) bits.push(runs.length + ' runs');
      else bits.push(runAge(runs[0].startedAt));
    }
    n = String(runs.length);
  } else if (d && d.halted === 'stopped') {
    say = 'Investigator paused'; kind = 'run-warn run-paused'; n = '!';
    tip = 'Nothing new starts until someone resumes it';
  } else if (d && d.halted === 'capped') {
    say = 'Daily AI cap reached'; kind = 'run-warn'; n = '!';
    tip = 'It starts again tomorrow, or when the daily limit is raised';
  } else if (d && d.problem && !d.halted) {
    say = 'Investigator off'; kind = 'run-muted'; n = '!';
    tip = d.problem === 'key' ? 'The investigator is on but has no AI key: set one on Admin' : 'The investigator is on but its AI model is not known: pick one on Admin';
  } else if (q) {
    say = 'Investigator'; kind = 'run-wait'; n = String(q);
  }
  if (!say) { p.hidden = true; p.innerHTML = ''; return; }
  if (q) bits.push(q + ' queued');
  p.hidden = false;
  p.className = 'run-pill ' + kind;
  p.setAttribute('href', href);
  p.title = tip;
  var ind = kind === 'run-on' || kind === 'run-wait' ? '<span class="run-ind' + (kind === 'run-wait' ? ' run-q' : '') + '" aria-hidden="true"></span>' : '<span class="run-mark" aria-hidden="true">!</span>';
  var html = ind + '<span class="rp-say">' + runEsc(say) + '</span>' + bits.map(function (b) { return '<span class="rp-sep">, </span><span class="rp-bit">' + runEsc(b) + '</span>'; }).join('') + '<span class="rp-n" aria-hidden="true">' + runEsc(n === '!' ? '' : n) + '</span>';
  if (p.dataset.html !== html) { p.dataset.html = html; p.innerHTML = html; }
}
function runShowPaint() {
  var root = document.getElementById('anoms');
  if (root) {
    [].forEach.call(root.querySelectorAll('article.inc'), function (c) {
      var head = c.querySelector('.inc-head'), title = head && head.querySelector('.inc-title'), usd = c.querySelector('.inc-more .line .usd');
      if (title) runPatch(head, title, c.dataset.key);
      if (usd && usd.dataset.said !== runSpendText(c.dataset.key)) usd.textContent = usd.dataset.said = runSpendText(c.dataset.key);
    });
    runDots(root);
  }
  runPill(RUN_BOARD);
}
