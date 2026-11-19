(function () {
  var SEV = {crit: ['Critical', 'crit'], warn: ['Warning', 'warn'], info: ['Info', 'info']};
  var WAITING = 'waiting to be addressed', INVESTIGATION = {investigating: 'investigating', dived: 'investigated'};
  var TAGS = {};
  var POLL_MS = 60000;
  var num = (/\/c\/(\d+)/.exec(location.pathname) || [])[1];
  var box = document.getElementById('card');
  var techOpen = null;
  var me = {operator: false, user: null, loginEnabled: false};
  var current = null;
  var ACT_NAMES = {dive: 'Investigation', take: 'Start progress', resolve: 'Resolve', mute: 'Mute', unmute: 'Unmute', known: 'Known noise', group: 'Group', ticket: 'Create task', link: 'Link Jira ticket', reopen: 'Reopen', severity: 'Severity change'};
  var ACT_STATES = {requested: 'queued', processing: 'processing', done: 'done', failed: 'failed', cancelled: 'cancelled'};
  var RES_NAMES = {fixed: 'Fixed', handled: 'No action needed', duplicate: 'Duplicate'};
  var RES_VIA = {page: 'from the page', chat: 'from chat', slack: 'from Slack'};
  RES_VIA[SYSTEM_ACTOR] = 'by ' + brandName();
  var RES_BY = {pod: 'investigator', auto: 'automatic', 'jira-links': 'Jira link sweep'};
  RES_BY[SYSTEM_ACTOR] = 'quality check';
  var RES_KINDS = [['fixed', 'Fixed'], ['handled', 'No action needed'], ['duplicate', 'Duplicate']];
  var resPick = 'fixed';
  var SPARKLES = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 5Q11 12 18 13Q11 14 10 21Q9 14 2 13Q9 12 10 5Z"></path><path d="M19 2v6"></path><path d="M16 5h6"></path></svg>';
  var when = (function () {
    try {
      var months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
      var f = new Intl.DateTimeFormat('en-GB', {day: 'numeric', month: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'});
      return function (iso) {
        var d = iso ? new Date(iso) : null;
        if (!d || isNaN(d)) return '';
        var p = {};
        f.formatToParts(d).forEach(function (x) { p[x.type] = x.value; });
        return +p.day + ' ' + months[+p.month - 1] + ' ' + p.hour + ':' + p.minute;
      };
    } catch (e) {
      return function (iso) { return iso || ''; };
    }
  })();
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); }
  function line(parts) {
    return parts.filter(Boolean).map(function (p) { return typeof p === 'object' ? '<span data-no-tx>' + esc(p.data) + '</span>' : '<span>' + esc(p) + '</span>'; }).join(' \u00b7 ');
  }
  function theme() {
    try {
      var a = JSON.parse(localStorage.getItem('skopax-appearance') || '{}') || {};
      if (a.mode === 'light' || a.mode === 'dark') document.documentElement.setAttribute('data-theme', a.mode);
    } catch (e) {}
  }
  function amount(v, unit) {
    if (v == null) return '\u2013';
    if (unit === 'dur') { var x = Math.round(v); return x < 120 ? x + ' s' : x < 7200 ? Math.floor(x / 60) + ' min' : Math.floor(x / 3600) + ' h' + (x % 3600 >= 60 ? ' ' + Math.floor(x % 3600 / 60) + ' min' : ''); }
    if (unit === 'ms') return v >= 1000 ? (v / 1000).toLocaleString('en-GB', {maximumFractionDigits: 1}) + ' s' : Math.round(v) + ' ms';
    return Number(v).toLocaleString('en-GB', {maximumFractionDigits: 2}) + (unit && unit !== 'ms' ? ' ' + unit : '');
  }
  function title(c) { return (c.svc && c.label ? c.svc + ' / ' + c.label + (c.sub && c.sub !== c.label ? ' \u00b7 ' + c.sub : '') : c.key || ('#' + c.num)) + (c.cluster ? ' \u00b7 instance ' + c.cluster : ''); }
  function resolution(c) { var r = c.resolution; return r && RES_NAMES[r.kind] ? r : null; }
  function resolvedName(r) { return !r ? 'Resolved' : r.kind === 'duplicate' && r.ref ? RES_NAMES.duplicate + ': ' + r.ref : RES_NAMES[r.kind] + (r.ref ? ' (' + r.ref + ')' : ''); }
  function resolvedBy(r) {
    var by = String(r.by || ''), who = RES_BY[by] || (isTokenActor(by) ? tokenActorLabel(by, 'Investigator') : by === 'Claude' ? 'Investigator' : by.split('@')[0]);
    return [RES_VIA[r.via], who ? {data: who} : ''];
  }
  function state(c) {
    var sev = (SEV[c.sev] || SEV.info)[1], r = resolution(c);
    if (!c.live && r && RES_NAMES[c.outcome]) return [resolvedName(r), 'good', resolvedBy(r).concat(['last seen ' + when(c.lastSeen || c.clearedAt)])];
    if (!c.live) return ['Closed', 'clear', ['last seen ' + when(c.lastSeen || c.clearedAt)]];
    if (c.triage === 'handled') {
      var how = c.triagedBy === 'auto' ? null : r;
      return [resolvedName(how), 'good', (how ? resolvedBy(how) : []).concat([c.status === 'cleared' ? (afterEvent(c) ? 'stopped after the event ' : 'fixed ') + when(c.clearedAt) : 'first seen ' + when(c.firstSeen)])];
    }
    var since = c.status === 'cleared' ? (afterEvent(c) ? 'stopped after the event ' : 'stopped ') + when(c.clearedAt) : 'first seen ' + when(c.firstSeen);
    if (c.triage === 'progress') return ['In progress', 'info', [c.ticket ? {data: c.ticket} : '', since]];
    if (c.muted) return ['Muted', 'clear', [since]];
    if (c.status === 'cleared' && !waiting(c)) return [afterEvent(c) ? 'Stopped after the event' : 'Fixed', afterEvent(c) ? 'clear' : 'good', [when(c.clearedAt)]];
    return ['Open', sev, [since, INVESTIGATION[c.triage], waiting(c) ? WAITING : '']];
  }
  function afterEvent(c) { return !!(c.cause && c.cause.kind === 'event' && c.cause.what); }
  function waiting(c) { return (c.status === 'cleared' && (c.sev === 'warn' || c.sev === 'crit')) || (c.status === 'open' && c.triage === 'dived'); }
  function tech(c) {
    var parts = [];
    if (c.what) parts.push('<p data-no-tx>' + esc(c.what) + '</p>');
    if (c.cause && c.cause.what && (c.cause.kind === 'event' || c.cause.kind === 'recurring')) parts.push('<p class="cause">Cause: ' + (c.cause.kind === 'event' ? 'an event' : 'recurring') + ' \u00b7 <span data-no-tx>' + esc(c.cause.what) + '</span></p>');
    if (c.now != null) parts.push('<p class="nums">Now ' + esc(amount(c.now, c.unit)) + (c.d1 != null ? ' \u00b7 yesterday ' + esc(amount(c.d1, c.unit)) : '') + (c.d7 != null ? ' \u00b7 last week ' + esc(amount(c.d7, c.unit)) : '') + '</p>');
    if (c.note && c.note.text) parts.push('<h3>Investigation \u00b7 ' + esc(when(c.note.at)) + '</h3><p data-no-tx>' + esc(c.note.text) + '</p>');
    if (c.clearNote && c.clearNote.text) parts.push('<h3>Cleared \u00b7 <span data-no-tx>' + esc(c.clearNote.by || '') + '</span> \u00b7 ' + esc(when(c.clearNote.at)) + '</h3><p data-no-tx>' + esc(c.clearNote.text) + '</p>');
    if ((c.suspects || []).length) parts.push('<h3>Suspects</h3><ul data-no-tx>' + c.suspects.map(function (s) { return '<li>' + esc(s) + '</li>'; }).join('') + '</ul>');
    if (c.recommend) parts.push('<h3>Recommended step</h3><p data-no-tx>' + esc(c.recommend) + '</p>');
    parts.push('<p class="key" data-no-tx>' + esc(c.key || '') + '</p>');
    return parts.join('');
  }
  function busy(c) { return !!(c.last && (c.last.status === 'requested' || c.last.status === 'processing')); }
  function lastLine(c) {
    var l = c.last;
    if (!l) return '';
    var by = String(l.requestedBy || ''), who = isTokenActor(by) ? tokenActorLabel(by, 'Investigator') : by === 'Claude' ? 'Investigator' : by.split('@')[0];
    var back = Date.parse(c.firstSeen || ''), did = Date.parse(l.doneAt || l.requestedAt || ''), old = l.status === 'done' && !isNaN(back) && !isNaN(did) && did < back;
    var head = (old ? 'Last action (before the card fired again ' + when(c.firstSeen) + '): ' : 'Last action: ') + (ACT_NAMES[l.type] || l.type) + ' \u00b7 ' + (ACT_STATES[l.status] || l.status) + (l.requestedAt ? ' \u00b7 ' + when(l.requestedAt) : '');
    return '<span class="act-last' + (l.status === 'failed' ? ' bad' : old ? ' old' : '') + '">' + line([head, who ? {data: who} : '', l.result && l.status !== 'done' ? {data: l.result} : '']) + '</span>';
  }
  function assignButton(c) {
    return '<button type="button" class="btn sec small" id="act-assign" title="Pick the card owner; its status does not change">' + (c.owner ? 'Change assignee' : 'Assign') + '</button>';
  }
  function assignForm(c) {
    return '<div class="act-form" id="form-assign" hidden><input id="assign-who" type="text" maxlength="80" placeholder="' + (me.jira ? 'Name, searched in Jira' : 'Name') + '" aria-label="Assign to">' +
      '<button type="button" class="btn small" id="assign-go" title="The person gets a Slack message with the card link">Assign</button>' +
      (mine(c) ? '' : '<button type="button" class="btn sec small" id="assign-me">Assign to me</button>') +
      (c.owner ? '<button type="button" class="btn sec small" id="assign-clear">Remove assignee</button>' : '') + '<span class="act-note" id="assign-msg" role="status"></span></div>';
  }
  function routeTakes(r, c) {
    var ids = (c.tags || []).map(function (t) { return t && t.id; });
    return !!r.tagged && (r.tags || []).some(function (t) { return ids.indexOf(t) >= 0; });
  }
  function routesFor(c) {
    return (me.routes || []).filter(function (r) { return r && r.id && (!r.tagged || routeTakes(r, c)); })
      .sort(function (x, y) { return routeTakes(y, c) - routeTakes(x, c); });
  }
  function routeName(r) { return (r.plugin ? r.plugin + ' \u00b7 ' : '') + r.name; }
  function routeControl(c, off) {
    var list = routesFor(c);
    if (!list.length) return '';
    return '<button type="button" class="btn sec small" id="act-route" title="Post this card to a chat channel through one of its routes" aria-expanded="false" aria-controls="form-route"' + off + '>Send to\u2026</button>';
  }
  function routeForm(c) {
    var list = routesFor(c);
    if (!list.length) return '';
    return '<div class="act-form" id="form-route" hidden>' + list.map(function (r) {
      return '<button type="button" class="btn sec small route-go" data-route="' + esc(r.id) + '">' + esc(routeName(r)) + '</button>';
    }).join('') + '<span class="act-note" id="route-msg" role="status"></span></div>';
  }
  async function sendRoute(c, r, b) {
    var note = document.getElementById('route-msg');
    b.disabled = true;
    note.textContent = '\u2026';
    try {
      var res = await fetch('/api/routes/send', {method: 'POST', credentials: 'same-origin', headers: {'Accept': 'application/json', 'Content-Type': 'application/json'}, body: JSON.stringify({key: c.key, route: r.id})});
      if (res.status === 401 || res.status === 403) {
        me.operator = false;
        if (res.status === 401) me.user = null;
        await whoami();
        render(current);
        return;
      }
      if (!res.ok) {
        var detail = null;
        try { detail = (await res.json()).detail; } catch (e) {}
        throw new Error(typeof detail === 'string' ? detail : String(res.status));
      }
      note.textContent = 'Sent to ' + routeName(r);
    } catch (e) {
      note.textContent = 'Not sent: ' + (e && e.message ? e.message : 'try again');
    }
    b.disabled = false;
  }
  function actions(c) {
    var copy = '<button type="button" class="btn sec small" id="copy-link">Copy link</button>';
    if (!c.live) return '<div class="acts">' + copy + '</div>';
    if (!me.operator) {
      var why = me.user ? 'Actions need the operator role.' : me.loginEnabled ? '<a href="/login?next=' + encodeURIComponent(location.pathname) + '">Log in</a>: actions need the operator role.' : '';
      return '<div class="acts">' + copy + (why ? '<span class="act-note">' + why + '</span>' : '') + lastLine(c) + '</div>';
    }
    var off = busy(c) ? ' disabled' : '';
    var handled = c.triage === 'handled';
    return '<div class="acts op">' +
      '<button type="button" class="btn icon" id="act-dive" title="Investigate: the investigator starts within about two minutes" aria-label="Investigate"' + off + '>' + SPARKLES + '</button>' +
      '<button type="button" class="btn sec small" id="act-take" title="The card goes In progress and you become its owner' + (me.jira ? '; you can add a Jira ticket' : '') + '"' + off + '>Start progress</button>' + jiraButtons(c, off) +
      (handled ? '' : '<button type="button" class="btn sec small" id="act-resolve"' + off + '>Resolve</button>') +
      (c.muted ? '<button type="button" class="btn sec small" id="act-unmute"' + off + '>Unmute</button>' : '<button type="button" class="btn sec small" id="act-mute"' + off + '>Mute 24 h</button>') +
      assignButton(c) + routeControl(c, off) + copy + lastLine(c) + assignForm(c) + routeForm(c) +
      '<div class="act-form" id="form-take" hidden>' + (me.jira ? '<input id="take-ticket" type="text" maxlength="100" placeholder="Jira key, or a task name to search (optional)" aria-label="Jira key">' : '') + '<input id="take-why" type="text" maxlength="300" placeholder="Note (optional)" aria-label="Start progress note"><button type="button" class="btn small" id="take-go">Start progress</button></div>' +
      '<div class="act-form" id="form-link" hidden><input id="link-ticket" type="text" maxlength="100" placeholder="Jira key, or words from the task title" aria-label="Jira ticket"><button type="button" class="btn small" id="link-go">Link</button><span class="act-note" id="link-msg"></span></div>' +
      '<div class="act-form" id="form-resolve" hidden><div class="res-kinds" role="group" aria-label="How it was resolved">' + RES_KINDS.map(function (k) {
        return '<button type="button" class="btn sec small sev-pick res-kind" id="resolve-' + k[0] + '" data-kind="' + k[0] + '" aria-pressed="' + (k[0] === resPick) + '">' + k[1] + '</button>';
      }).join('') + '<p class="res-hint" id="resolve-hint">' + esc(resHint(resPick)) + '</p></div>' +
      '<input id="resolve-why" type="text" maxlength="300" placeholder="' + RES_PLACEHOLDER_WHY + '" title="' + esc(RES_TITLE_WHY) + '" aria-label="Why it was resolved"' + (resPick === 'duplicate' ? ' hidden' : '') + '>' +
      '<input id="resolve-dup" type="text" maxlength="200" placeholder="' + RES_PLACEHOLDER_DUP + '" title="' + esc(RES_TITLE_DUP) + '" aria-label="Duplicate of"' + (resPick === 'duplicate' ? '' : ' hidden') + '>' +
      '<button type="button" class="btn small" id="resolve-go">Resolve</button></div>' +
      '</div>';
  }
  function jiraKey(t) { return (/\b[A-Z][A-Z0-9]{1,9}-\d{1,7}\b/.exec(String(t || '').toUpperCase()) || [])[0]; }
  function jiraButtons(c, off) {
    var k = jiraKey(c.ticket), base = String(me.jiraUrl || '').replace(/\/+$/, '');
    var open = k ? (base ? '<a class="btn sec small" id="jira-open" href="' + esc(base + '/browse/' + encodeURIComponent(k)) + '" target="_blank" rel="noopener" title="Open ' + esc(k) + ' in Jira">' + esc(k) + ' \u2197</a>' : '<span class="btn sec small" id="jira-open">' + esc(k) + '</span>') : '';
    var tip = me.jira ? 'Links an existing Jira ticket: the card goes In progress and stays there until the ticket closes; the assignee does not change'
      : 'Links a ticket by its key: the card goes In progress; Jira is not set up, so nothing closes the card when the ticket closes';
    return k ? open + '<button type="button" class="btn sec small" id="act-link"' + off + '>Change ticket</button>'
      : '<button type="button" class="btn sec small" id="act-link"' + off + ' title="' + tip + '">' + (me.jira ? 'Link Jira ticket' : 'Link ticket') + '</button>';
  }
  function mine(c) {
    var o = String(c.owner || '').toLowerCase(), u = String(me.user || '').toLowerCase();
    return !!(o && u && (o === u || o === u.split('@')[0] || o.replace(/\s+/g, '.') === u.split('@')[0]));
  }
  function ownerName(o) { return String(o || '').indexOf('@') > 0 ? String(o).split('@')[0] : String(o || ''); }
  async function assign(c, body, b) {
    var msg = document.getElementById('assign-msg');
    b.disabled = true;
    msg.textContent = '\u2026';
    try {
      var r = await fetch('/api/assignee', {method: 'PUT', credentials: 'same-origin', headers: {'Accept': 'application/json', 'Content-Type': 'application/json'}, body: JSON.stringify(Object.assign({key: c.key}, body))});
      if (r.status === 401 || r.status === 403) {
        me.operator = false;
        if (r.status === 401) me.user = null;
        await whoami();
        render(current);
        return;
      }
      if (!r.ok) {
        var detail = null;
        try { detail = (await r.json()).detail; } catch (e) {}
        throw new Error(typeof detail === 'string' ? detail : String(r.status));
      }
      await load(true);
    } catch (e) {
      b.disabled = false;
      msg.textContent = 'Could not assign: ' + (e && e.message ? e.message : 'try again');
    }
  }
  async function queue(body, b) {
    var label = b.innerHTML;
    b.disabled = true;
    try {
      var r = await fetch('/api/actions', {method: 'POST', credentials: 'same-origin', headers: {'Accept': 'application/json', 'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      if (r.status === 401 || r.status === 403) {
        me.operator = false;
        if (r.status === 401) me.user = null;
        await whoami();
        render(current);
        return;
      }
      if (!r.ok) throw new Error(String(r.status));
      await load(true);
    } catch (e) {
      b.disabled = false;
      b.innerHTML = label;
      var said = box.querySelector('.act-err');
      if (said) said.remove();
      var note = box.querySelector('.act-last') || box.querySelector('.acts');
      if (note) note.insertAdjacentHTML('afterend', '<p class="act-err">The action was not saved; try again.</p>');
    }
  }
  function wireActions(c) {
    var on = function (id, fn) { var b = document.getElementById(id); if (b) b.addEventListener('click', function () { fn(b); }); };
    var name = title(c), base = {key: c.key, target: c.key};
    var form = function (id) { var f = document.getElementById(id); f.hidden = !f.hidden; var i = f.querySelector('input'); if (!f.hidden && i) i.focus(); };
    on('copy-link', function (b) {
      var link = location.origin + '/c/' + c.num;
      var done = function (t) { b.textContent = t; setTimeout(function () { b.textContent = 'Copy link'; }, 2000); };
      if (navigator.clipboard) navigator.clipboard.writeText(link).then(function () { done('Copied'); }, function () { done(link); }); else done(link);
    });
    on('act-dive', function (b) { queue(Object.assign({type: 'dive', label: 'Investigate ' + name}, base), b); });
    on('act-take', function () { form('form-take'); });
    var tk = document.getElementById('take-ticket');
    if (tk && me.jira && typeof ticketSuggest === 'function') ticketSuggest(tk, {none: 'No open ticket matches', fail: 'The Jira search failed; type the key'});
    on('act-resolve', function () { form('form-resolve'); });
    on('act-link', function () { form('form-link'); });
    var lk = document.getElementById('link-ticket');
    if (lk && me.jira && typeof ticketSuggest === 'function') ticketSuggest(lk, {none: 'No open ticket matches', fail: 'The Jira search failed; type the key'});
    on('link-go', function (b) {
      var k = jiraKey(lk.value.trim());
      if (!k) { document.getElementById('link-msg').textContent = 'Pick a ticket from the list or type its key, like OPS-123'; lk.focus(); return; }
      queue(Object.assign({type: 'link', ticket: k, label: 'Link ' + name + ' to ' + k}, base), b);
    });
    on('act-mute', function (b) { queue(Object.assign({type: 'mute', hours: 24, label: 'Mute ' + name + ' for 24 h'}, base), b); });
    on('act-unmute', function (b) { queue(Object.assign({type: 'unmute', label: 'Unmute ' + name}, base), b); });
    var picked = null, who = document.getElementById('assign-who');
    on('act-assign', function () { form('form-assign'); });
    if (who && me.jira && typeof ticketSuggest === 'function') ticketSuggest(who, {url: '/api/people', none: 'No matching person', fail: 'The Jira search failed; type the name', picked: function (it) { picked = it; }});
    on('assign-go', function (b) {
      var name = who.value.trim();
      if (!name) { who.focus(); return; }
      assign(c, {name: name, email: picked && picked.name === name ? picked.email : null}, b);
    });
    on('assign-me', function (b) { assign(c, {me: true}, b); });
    on('act-route', function (b) {
      var f = document.getElementById('form-route');
      f.hidden = !f.hidden;
      b.setAttribute('aria-expanded', String(!f.hidden));
      var first = f.querySelector('.route-go');
      if (!f.hidden && first) first.focus();
    });
    [].forEach.call(box.querySelectorAll('#form-route .route-go'), function (b) {
      var r = routesFor(c).filter(function (x) { return x.id === b.dataset.route; })[0];
      if (r) b.addEventListener('click', function () { sendRoute(c, r, b); });
    });
    on('assign-clear', function (b) { assign(c, {name: ''}, b); });
    on('take-go', function (b) {
      var tt = document.getElementById('take-ticket'), t = tt ? tt.value.trim() : '', w = document.getElementById('take-why').value.trim();
      queue(Object.assign({type: 'take', ticket: t || undefined, reason: w || undefined, label: 'Take ' + name + (t ? ' with ' + t : '')}, base), b);
    });
    var why = document.getElementById('resolve-why'), dup = document.getElementById('resolve-dup');
    [].forEach.call(box.querySelectorAll('#form-resolve .res-kind'), function (k) {
      k.addEventListener('click', function () {
        resPick = k.dataset.kind;
        document.getElementById('resolve-hint').textContent = resHint(resPick);
        [].forEach.call(box.querySelectorAll('#form-resolve .res-kind'), function (x) { x.setAttribute('aria-pressed', String(x === k)); });
        why.hidden = resPick === 'duplicate';
        dup.hidden = resPick !== 'duplicate';
        dup.removeAttribute('aria-invalid');
        (resPick === 'duplicate' ? dup : why).focus();
      });
    });
    on('resolve-go', function (b) {
      var w = why.value.trim(), ref = dup.value.trim();
      if (resPick === 'duplicate' && !ref) { dup.setAttribute('aria-invalid', 'true'); dup.focus(); return; }
      var body = resPick === 'duplicate' ? {type: 'resolve', kind: 'duplicate', ref: ref, label: 'Resolve ' + name + ' (duplicate of ' + ref + ')'}
        : {type: 'resolve', kind: resPick === 'fixed' ? 'fixed' : undefined, reason: w || undefined, label: 'Resolve ' + name + (resPick === 'fixed' ? ' (fixed)' : ' (no action needed)')};
      queue(Object.assign(body, base), b);
    });
  }
  function typing() {
    var a = document.activeElement;
    return !!(a && box.contains(a) && a.tagName === 'INPUT') || [].some.call(box.querySelectorAll('.act-form:not([hidden]) input'), function (i) { return i.value.trim(); });
  }
  async function whoami() {
    try {
      var r = await fetch('/api/me', {headers: {'Accept': 'application/json'}, credentials: 'same-origin'});
      if (r.ok) me = Object.assign(me, await r.json());
      (me.tags || []).forEach(function (t) { if (t && typeof t.id === 'string' && typeof t.label === 'string' && t.label) TAGS[t.id] = t.label; });
    } catch (e) {}
  }
  function merged(list) {
    if (!list.length) return '';
    return '<section class="merged"><h2>Merged topics (' + list.length + ')</h2><ul>' + list.map(function (k) {
      var b = k.business || {};
      return '<li><div class="m1"><b data-no-tx>' + esc(title(k)) + '</b>' + (k.num ? ' <a href="/c/' + esc(k.num) + '">#' + esc(k.num) + '</a>' : '') +
        (String(k.url || '').startsWith('https://') ? ' \u00b7 <a href="' + esc(k.url) + '" target="_blank" rel="noopener">Open the page \u2197</a>' : '') + (k.status === 'cleared' ? ' \u00b7 fixed' : '') + '</div>' +
        (b.text ? '<p class="m-todo"><b>' + (b.kind === 'info' ? 'Info' : 'Recommended') + ':</b> <span data-no-tx>' + esc(b.text) + '</span></p>' : k.what ? '<p class="m-what" data-no-tx>' + esc(k.what) + '</p>' : '') + '</li>';
    }).join('') + '</ul></section>';
  }
  function render(c) {
    var b = c.business || {}, sev = SEV[c.sev] || SEV.info, st = state(c), said = line(st[2]);
    document.title = '#' + c.num + ' \u00b7 ' + title(c) + ' \u00b7 ' + brandName();
    document.getElementById('num').textContent = '#' + c.num;
    if (c.key) document.getElementById('full').href = '/#a=' + encodeURIComponent(c.key);
    var tags = (c.tags || []).map(function (t) { return '<span class="pill tag">' + esc(TAGS[t.id] || t.id) + '</span>'; }).join('');
    var shots = (c.screenshots || []).map(function (s) {
      var id = typeof s === 'string' ? s : s && s.id;
      if (!id) return '';
      var src = '/api/screenshots/' + encodeURIComponent(id), phone = s.viewport === 'phone';
      var cap = 'Screenshot' + (phone ? ' \u00b7 phone' : s.viewport ? ' \u00b7 desktop' : '') + (s.at ? ' \u00b7 ' + when(s.at) : '');
      return '<figure class="shot' + (phone ? ' phone' : '') + '"><a href="' + src + '" target="_blank" rel="noopener"><img src="' + src + '" alt="' + esc(cap + (s.finalUrl || s.url ? ', ' + (s.finalUrl || s.url) : '')) + '" loading="lazy"></a><figcaption>' + esc(cap) + '</figcaption></figure>';
    }).join('');
    var open = techOpen == null ? !b.text : techOpen;
    box.innerHTML = '<div class="head"><span class="state-pill ' + st[1] + '" id="state">' + esc(st[0]) + '</span><span class="pill ' + sev[1] + '">' + sev[0] + '</span>' + tags + '</div>' +
      '<h1 data-no-tx>' + esc(title(c)) + '</h1>' +
      (said ? '<p class="status">' + said + '</p>' : '') +
      (c.owner ? '<p class="owner" data-no-tx title="' + esc(c.assigned && c.assigned.by ? tx('Assigned by:') + ' ' + ownerName(c.assigned.by) + (c.assigned.at ? ' \u00b7 ' + tx(when(c.assigned.at)) : '') : '') + '">' + esc(tx('Assignee:')) + ' <b data-no-tx>' + esc(ownerName(c.owner)) + '</b></p>' : '') +
      '<a class="btn big chat-btn" id="chat-about" href="/#t=chat/new/' + encodeURIComponent(c.num) + '" title="Opens a new private chat with this card attached: ask why it fired, what the investigation found, who should look">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>Chat about this card</a>' +
      (b.text ? '<section class="todo"><h2>' + (b.kind === 'info' ? 'Info' : 'Recommended') + '</h2>' + (b.why ? '<p class="why"><b>Why:</b> <span data-no-tx>' + esc(b.why) + '</span></p>' : '') + '<p class="rec" data-no-tx>' + esc(b.text) + '</p></section>' : '') +
      '<div class="btns">' + (String(c.url || '').startsWith('https://') ? '<a class="btn" href="' + esc(c.url) + '" target="_blank" rel="noopener">Open the page \u2197</a>' : '') +
      '<a class="btn sec" id="board" href="' + esc(c.key ? '/#a=' + encodeURIComponent(c.key) : '/') + '">Open on the board</a></div>' +
      (shots ? '<div class="shots">' + shots + '</div>' : '') +
      actions(c) +
      merged(c.merged || []) +
      '<details class="tech"' + (open ? ' open' : '') + '><summary>Technical details</summary><div class="tech-body">' + tech(c) + '</div></details>';
    box.querySelector('details.tech').addEventListener('toggle', function (e) { techOpen = e.target.open; });
    wireActions(c);
    document.getElementById('upd').textContent = 'Updated ' + when(new Date().toISOString());
  }
  function missing(text) {
    box.innerHTML = '<p class="empty">' + esc(text) + '</p>';
    document.title = brandName();
  }
  var shownSig = '';
  async function load(force) {
    if (!num) { missing('There is no card number in this address.'); return; }
    if (!force && typing()) return;
    try {
      var r = await fetch('/api/cards/' + num, {headers: {'Accept': 'application/json'}, credentials: 'same-origin'});
      if (r.status === 404) { missing('Card #' + num + ' not found.'); return; }
      if (!r.ok) throw new Error(String(r.status));
      var next = await r.json(), sig = JSON.stringify(next);
      current = next;
      if (sig === shownSig && box.querySelector('h1')) { var upd = document.getElementById('upd'); if (upd) upd.textContent = 'Updated ' + when(new Date().toISOString()); return; }
      shownSig = sig;
      var y = window.scrollY;
      render(current);
      if (y) window.scrollTo(0, y);
    } catch (e) {
      if (!box.querySelector('h1')) missing('The card could not be loaded; reload the page.');
    }
  }
  theme();
  if (typeof startLocalize === 'function') startLocalize();
  whoami().then(function () { load(true); });
  setInterval(function () { if (!document.hidden) load(); }, POLL_MS);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) load(); });
  window.__cardLoad = load;
})();
