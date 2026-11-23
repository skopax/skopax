async function initActions() {
  var hint = document.getElementById('q-hint');
  var strip = document.getElementById('queue-strip');
  var me = {loginEnabled: false, user: null, operator: false, slack: false, slackChannel: '', jira: false};
  var all = [];
  var STATUS = {requested: ['Queued', 's-queued'], processing: ['Working…', 's-processing'], done: ['Done', 's-done'], failed: ['Failed', 's-failed'], cancelled: ['Cancelled', 's-other']};
  function label(a) { return a.label || a.type; }
  function chip(a) {
    var s = STATUS[a.status] || [String(a.status), 's-other'];
    return '<span class="st ' + s[1] + '">' + esc(s[0]) + '</span>' + (a.result ? ' <span class="muted">' + esc(a.result) + '</span>' : '');
  }
  function when(a) { return a[a.status + 'At'] || a.requestedAt; }
  var DID = {dive: ['Investigate', 'Investigated', 'Investigating…'], mute: ['Mute', 'Muted', 'Muting…'], unmute: ['Unmute', 'Unmuted', 'Unmuting…'], known: ['Known noise', 'Marked known', 'Marking known…'],
    take: ['Start progress', 'Started', 'Starting…'], group: ['Group', 'Grouped', 'Grouping…'], resolve: ['Resolve', 'Resolved', 'Resolving…'], ticket: ['Create task', 'Task created', 'Creating task…']};
  function named(a) {
    var s = STATUS[a.status] || [String(a.status), 's-other'], d = DID[a.type];
    return !d || a.status === 'requested' ? s[0] : a.status === 'done' ? d[1] : a.status === 'processing' ? d[2] : d[0] + ' ' + s[0].toLowerCase();
  }
  function badge(a, text, t, cls, sentence) {
    var s = STATUS[a.status] || [String(a.status), 's-other'];
    if (sentence && !txLang()) text = text.charAt(0).toLowerCase() + text.slice(1);
    return '<span class="st ' + (cls || s[1]) + '"' + (t ? ' data-tip="' + esc(t) + '" title="' + esc(t) + '"' : '') + '>' + esc(text) + '</span>';
  }
  function muteEnd(a) {
    var at = Date.parse(a.doneAt || '');
    return isNaN(at) ? null : new Date(at + (a.hours || 24) * 3600e3).toISOString();
  }
  function pill(a, card) {
    var s = STATUS[a.status] || [String(a.status), 's-other'];
    var t = card ? label(a) + ' · ' + s[0] + ' ' + dayhm(when(a)) + (a.result ? '\n' + a.result : '') : '';
    if (card && a.type === 'mute' && a.status === 'done') {
      var end = muteEnd(a);
      if (end && Date.parse(end) <= Date.now()) return '<span class="st s-other" data-tip="' + esc(t) + '" title="' + esc(t) + '">Mute ended ' + esc(dayhm(end)) + '</span>';
      if (end) return badge(a, 'Muted until ' + dayhm(end), t);
    }
    return badge(a, (card ? named(a) : s[0]) + (a.status === 'done' && a.doneAt ? ' ' + dayhm(a.doneAt) : ''), t);
  }
  function earlier(x, card) {
    var did = Date.parse(when(x) || ''), back = Date.parse((card && card.firstSeen) || '');
    return x.status === 'done' && !isNaN(did) && !isNaN(back) && did < back;
  }
  function lastLine(a, card) {
    var at = dayhm(when(a));
    if (card && earlier(a, card)) return '<span class="la-h">Reopened ' + esc(dayhm(card.firstSeen)) + ' \u00b7 fired again</span> <span class="la-before"><span>Before that:</span> ' + badge(a, named(a) + (at ? ' ' + at : ''), label(a), 's-other', true) +
      ' by ' + esc(draftBy(a.requestedBy)) + (a.result ? ' (' + linkify(a.result) + ')' : '') + '</span>';
    return '<span class="la-h">Last action:</span> ' + badge(a, named(a) + (at ? ' ' + at : ''), label(a), '') + ' by ' + esc(draftBy(a.requestedBy)) + (a.result ? ' \u00b7 ' + linkify(a.result) : '');
  }
  function btn(text, cls) { var b = el('button', 'btn ' + (cls || '')); b.type = 'button'; b.textContent = text; return b; }
  function lockReason() {
    if (me.operator) return '';
    if (!me.loginEnabled) return 'Actions are off: login is not set up on this service yet';
    return me.user ? 'Needs the operator role: ask an admin' : 'Log in to use this';
  }
  function adminReason() {
    if (!me.user) return 'Log in to use this';
    return me.admin ? '' : 'Admins only: needs the admin role';
  }
  function loginReason() {
    if (me.operator) return '';
    if (!me.loginEnabled) return 'Assigning is off: login is not set up on this service yet';
    return me.user ? 'Needs the operator role: ask an admin' : 'Log in to assign';
  }
  function gate() {
    var why = lockReason(), loginWhy = loginReason();
    document.querySelectorAll('#anoms .tile').forEach(function (t) { t.draggable = window.__tileDraggable(t.dataset.key); });
    document.querySelectorAll('#anoms .acts button.btn:not(.free), #anoms .notif button.btn, .op-btn, .op-in').forEach(function (b) {
      var r = (b.classList.contains('admin-only') ? adminReason() : b.classList.contains('login-ok') ? loginWhy : why) || (b.classList.contains('slack-btn') && !me.slack ? 'Slack is not set up on this service yet: an admin connects it in Connect \u203a Integrations \u203a Slack' : '') || (b.classList.contains('jira-btn') && !me.jira ? 'Jira is not set up on this service yet: an admin adds it under Connect \u203a Integrations' : '');
      if (b.tagName === 'INPUT') b.readOnly = !!r;
      if (!r) {
        b.removeAttribute('aria-disabled');
        if (b.dataset.locked) {
          if (b.dataset.tip) b.title = b.dataset.tip;
          else b.removeAttribute('title');
          delete b.dataset.locked;
        }
      } else {
        b.setAttribute('aria-disabled', 'true');
        b.title = r;
        b.dataset.locked = '1';
      }
    });
  }
  window.__skopaxApi = api;
  window.__skopaxGate = gate;
  window.__boardLock = lockReason;
  window.__skopaxConfirm = confirmDialog;
  window.__boardMove = async function (steps) {
    var last = null;
    for (var i = 0; i < steps.length; i++) {
      last = await api('POST', '/api/actions', steps[i]);
      if (last && last.status === 'failed') break;
    }
    await refresh();
    if (window.__skopaxPoll) await window.__skopaxPoll();
    return last;
  };
  function roleName() {
    var r = me.userRole;
    if (r !== 'admin' && r !== 'operator' && r !== 'viewer') r = me.admin ? 'admin' : me.operator ? 'operator' : 'viewer';
    return r;
  }
  function account() {
    var signed = !!(me.loginEnabled && me.user), role = roleName();
    if (typeof sideFoot === 'function') {
      if (!me.loginEnabled) sideFoot(null);
      else if (!me.user) sideFoot({login: '/login?next=' + encodeURIComponent(location.pathname + location.search), tip: 'Log in to ' + brandName() + '. Actions need the operator role.'});
      else {
        var tip = role === 'admin' ? me.user + ' \u00b7 admin: settings, access and actions' : role === 'operator' ? me.user + ' \u00b7 operator: can queue actions' : me.user + ' \u00b7 viewer: actions need the operator role, ask an admin';
        var items = me.canEditName ? [{id: 'um-name', label: 'Edit name', icon: 'badge', run: function () { openNameDialog(document.getElementById('user-btn')); }}] : [];
        if (me.canChangePassword) items.push({label: 'Change password', icon: 'key', run: function () { openPasswordDialog(document.getElementById('user-btn')); }});
        items.push({label: 'Sign out', icon: 'out', run: logOut});
        sideFoot({user: me.user, name: String(me.name || '').trim() || me.user, role: role, tip: tip, items: items});
      }
    }
    var sec = document.getElementById('pf-acct');
    if (!sec) return;
    sec.hidden = !signed;
    if (!signed) return;
    document.getElementById('pf-who').textContent = 'Signed in as ' + me.user + ' \u00b7 ' + role;
    var pw = document.getElementById('pf-pw'), out = document.getElementById('pf-out');
    pw.hidden = !me.canChangePassword;
    pw.onclick = function () { openPasswordDialog(pw); };
    out.onclick = function () { logOut(out); };
  }
  var noteTimer = null;
  function accountNote(text, bad) {
    clearTimeout(noteTimer);
    document.querySelectorAll('#account .acct-note, #pf-acct .acct-note').forEach(function (n) {
      n.textContent = text;
      n.className = 'acct-note' + (bad ? ' bad' : '');
    });
    if (text) noteTimer = setTimeout(function () { accountNote(''); }, 6000);
  }
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') accountNote(''); });
  async function logOut(b) {
    accountNote('');
    b.disabled = true;
    try {
      var r = await fetch('/logout', {method: 'POST', credentials: 'same-origin', headers: {'Accept': 'text/html'}});
      if (!r.ok) throw new Error('logout ' + r.status);
      location.href = '/login';
    } catch (err) {
      b.disabled = false;
      accountNote('Could not sign out, try again', true);
    }
  }
  window.__skopaxRenamed = function (email, name) {
    if (!me.user || email !== me.user) return;
    me.name = name;
    account();
  };
  function dialogKeys(d, close) {
    d.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') { close(); return; }
      if (ev.key !== 'Tab') return;
      var all = [].filter.call(d.querySelectorAll('input, button'), function (x) { return !x.disabled; });
      var first = all[0], last = all[all.length - 1];
      if (!first) return;
      if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
      else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
    });
  }
  function openNameDialog(opener) {
    accountNote('');
    var d = document.getElementById('name-dialog');
    if (d) d.remove();
    d = el('div', 'pw-dialog name-dialog');
    d.id = 'name-dialog';
    d.setAttribute('role', 'dialog');
    d.setAttribute('aria-modal', 'true');
    d.setAttribute('aria-label', 'Edit name');
    d.innerHTML = '<form class="pw-card"><h2>Edit name</h2>' +
      '<label>Your name, as others see it (1 to 120 characters)<input type="text" name="name" maxlength="120" autocomplete="name" required></label>' +
      '<div class="pw-acts"><button type="submit" class="btn small">Save</button><button type="button" class="btn sec small name-cancel">Cancel</button><span class="adm-msg name-msg" role="status"></span></div></form>';
    var form = d.querySelector('form'), msg = d.querySelector('.name-msg'), go = d.querySelector('button[type="submit"]'), input = form.elements.name;
    var close = function () { d.remove(); if (opener && document.body.contains(opener)) opener.focus(); };
    var say = function (t, bad) { msg.textContent = t; msg.className = 'adm-msg name-msg' + (bad ? ' bad' : ''); };
    input.value = String(me.name || '').trim() || me.user || '';
    d.querySelector('.name-cancel').addEventListener('click', close);
    dialogKeys(d, close);
    form.addEventListener('submit', async function (ev) {
      ev.preventDefault();
      go.disabled = true;
      say('Saving\u2026', false);
      try {
        var r = await adminApi('PATCH', '/api/me', {name: input.value.trim()});
        var name = r && typeof r.name === 'string' ? r.name : input.value.trim();
        d.remove();
        window.__skopaxRenamed(me.user, name);
        if (typeof accRenamed === 'function') accRenamed(me.user, name);
        var back = document.getElementById('user-btn');
        if (back) back.focus();
        accountNote('Name saved.');
      } catch (err) {
        say(err && err.detail ? err.detail : 'The name could not be saved. Try again.', true);
        go.disabled = false;
      }
    });
    document.body.appendChild(d);
    input.focus();
    input.select();
  }
  function openPasswordDialog(opener) {
    accountNote('');
    var d = document.getElementById('pw-dialog');
    if (d) d.remove();
    d = el('div', 'pw-dialog');
    d.id = 'pw-dialog';
    d.setAttribute('role', 'dialog');
    d.setAttribute('aria-modal', 'true');
    d.setAttribute('aria-label', 'Change password');
    d.innerHTML = '<form class="pw-card"><h2>Change password</h2>' +
      '<label>Current password<input type="password" name="current" maxlength="256" autocomplete="current-password" required></label>' +
      '<label>New password (12 to 256 characters)<input type="password" name="new" maxlength="256" autocomplete="new-password" required></label>' +
      '<label>New password again<input type="password" name="again" maxlength="256" autocomplete="new-password" required></label>' +
      '<div class="pw-acts"><button type="submit" class="btn small">Change password</button><button type="button" class="btn sec small pw-cancel">Cancel</button><span class="adm-msg pw-msg" role="status"></span></div></form>';
    var form = d.querySelector('form'), msg = d.querySelector('.pw-msg'), go = d.querySelector('button[type="submit"]');
    var close = function () { d.remove(); if (opener && document.body.contains(opener)) opener.focus(); };
    var say = function (t, bad) { msg.textContent = t; msg.className = 'adm-msg pw-msg' + (bad ? ' bad' : ''); };
    d.querySelector('.pw-cancel').addEventListener('click', close);
    dialogKeys(d, close);
    form.addEventListener('submit', async function (ev) {
      ev.preventDefault();
      var f = form.elements;
      if (f.new.value !== f.again.value) { say('The two new passwords do not match.', true); return; }
      go.disabled = true;
      say('Saving\u2026', false);
      try {
        await adminApi('POST', '/api/me/password', {current: f.current.value, new: f.new.value});
        close();
        accountNote('Password changed.');
      } catch (err) {
        say(err && err.detail ? err.detail : 'The password could not be changed. Try again.', true);
        go.disabled = false;
      }
    });
    document.body.appendChild(d);
    form.elements.current.focus();
  }
  document.addEventListener('click', function (e) {
    var b = e.target.closest('#anoms .acts button[aria-disabled="true"], .op-btn[aria-disabled="true"]');
    if (!b) return;
    e.preventDefault();
    e.stopPropagation();
    if (me.loginEnabled && !me.user) location.href = '/login';
  }, true);
  document.addEventListener('click', function (e) {
    var a = e.target.closest('a[data-json]');
    if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button) return;
    e.preventDefault();
    jsonDialog(a);
  });
  document.addEventListener('click', function (e) {
    var a = e.target.closest('a[data-shot]');
    if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button) return;
    e.preventDefault();
    shotDialog(a);
  });
  async function api(method, path, body) {
    var headers = {'Accept': 'application/json'};
    if (method !== 'GET' && body && typeof watchHide === 'function') watchHide(body.key || body.target);
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    var r;
    try { r = await fetch(path, {method: method, headers: headers, credentials: 'same-origin', body: body === undefined ? undefined : JSON.stringify(body)}); } catch (e) {
      if (method !== 'GET') failToast('the server did not answer (' + method + ' ' + path + ')');
      throw e;
    }
    if (r.status === 401) { me.user = null; me.operator = false; account(); gate(); if (method !== 'GET') failToast('you are signed out; sign in and try again'); throw {code: 401}; }
    if (r.status === 403) { me.operator = false; account(); gate(); if (method !== 'GET') failToast('you are not allowed to do this'); throw {code: 403}; }
    if (!r.ok) { var detail = null; try { detail = (await r.json()).detail; } catch (e) {} if (method !== 'GET') failToast((typeof detail === 'string' && detail ? detail : 'not saved') + ' (' + r.status + ')'); throw {code: r.status, detail: typeof detail === 'string' ? detail : null}; }
    return r.json();
  }
  async function queue(a, b, done) {
    b.disabled = true;
    try { await api('POST', '/api/actions', a); await refresh(); if (window.__skopaxPoll) window.__skopaxPoll(); if (done) done(); } catch (e) { b.disabled = false; if (!e || (e.code !== 401 && e.code !== 403)) b.textContent = e && e.code === 409 && e.detail ? e.detail : 'Not saved, try again'; }
  }
  async function cancel(x, b) {
    b.disabled = true;
    b.textContent = 'Cancelling…';
    try {
      await api('PATCH', '/api/actions/' + encodeURIComponent(x.id), {status: 'cancelled', expect: 'requested'});
      hint.textContent = 'Cancelled “' + label(x) + '”.';
      await refresh();
    } catch (e) {
      if (e && e.code === 409) { hint.textContent = 'Too late to cancel “' + label(x) + '”: the investigator has already picked it up.'; b.textContent = 'Too late'; return; }
      b.disabled = false;
      b.textContent = 'Cancel failed, try again';
    }
  }
  var TASK_PROJECT_KEY = 'skopax-task-project';
  function taskText(a) {
    var lines = [(a.sev || '?') + ' · ' + svcName(a) + ' · ' + a.label + ' · now ' + a.now + ' vs 1 d ' + a.d1 + ' / 7 d ' + a.d7 + ' · since ' + dayhm(a.firstSeen)];
    if (a.short) lines.push(a.short);
    if (a.note && a.note.text) lines.push('Findings: ' + a.note.text);
    if (a.suspects && a.suspects.length) lines.push('Suspects: ' + a.suspects.slice(0, 5).join('; '));
    if (a.recommend) lines.push('Recommended: ' + a.recommend);
    return lines.join('\n');
  }
  function taskDialog(a, name) {
    var wrap = document.getElementById('task-wrap'), title = document.getElementById('task-title'), pts = document.getElementById('task-points'), desc = document.getElementById('task-desc');
    var yes = document.getElementById('task-yes'), no = document.getElementById('task-no'), err = document.getElementById('task-err'), meta = document.getElementById('task-meta');
    var proj = document.getElementById('task-project'), ptsF = document.getElementById('task-points-f'), epic = document.getElementById('task-epic'), epicF = document.getElementById('task-epic-f');
    if (!wrap || wrap.dataset.open) return;
    wrap.dataset.open = '1';
    var d = me.task || {}, pfx = d.titlePrefix || '';
    document.getElementById('task-card').textContent = name + (a.ns && a.ns !== '-' ? ' · ' + a.ns : '');
    title.value = pfx + svcName(a) + ' ' + a.label;
    pts.value = d.points != null ? d.points : 1;
    desc.value = taskText(a);
    var tail = 'The card link' + ((a.links || []).length || (a.code || []).length ? ', the Kibana / Prometheus links and the code links' : '') + ' are added at the end';
    var tech = d.techProject || '';
    var offered = (d.projects && d.projects.length ? d.projects : (d.project ? [d.project] : [])).concat(tech ? [tech] : []);
    var last = load(TASK_PROJECT_KEY);
    proj.innerHTML = '<option value="">Choose…</option>' + offered.map(function (p) { return '<option value="' + esc(p) + '">' + esc(p) + (p === tech ? ' (Infra)' : '') + '</option>'; }).join('');
    proj.value = offered.indexOf(last) >= 0 ? last : '';
    epic.value = '';
    if (!epic.dataset.suggest && typeof ticketSuggest === 'function' && me.jira) {
      epic.dataset.suggest = '1';
      ticketSuggest(epic, {params: function () { return '&type=epic' + (proj.value ? '&project=' + encodeURIComponent(proj.value) : ''); }, none: 'No open epic matches; type its key or leave it empty'});
    }
    function showProject() {
      var isTech = !!(tech && proj.value === tech);
      if (ptsF) ptsF.hidden = isTech;
      epicF.hidden = isTech;
      if (pfx && isTech && title.value.indexOf(pfx) === 0) title.value = title.value.slice(pfx.length);
      if (pfx && !isTech && title.value.indexOf(pfx) !== 0) title.value = pfx + title.value;
      meta.textContent = (!proj.value ? ['Pick the project this task goes to', tail]
        : isTech ? ['Project ' + tech + ' (Infra)', d.techAssignee ? 'Assigned to the Infra owner' : 'No assignee', 'No sprint or points', tail]
        : ['Project ' + proj.value, epic.value.trim() ? 'Epic ' + epic.value.trim().toUpperCase() : 'No epic', 'Active sprint', 'You as assignee and Developer', tail]).filter(Boolean).join(' · ');
    }
    showProject();
    proj.onchange = function () { epic.value = ''; showProject(); };
    epic.oninput = showProject;
    epic.onblur = function () { setTimeout(showProject, 200); };
    err.hidden = true;
    err.textContent = '';
    function close() {
      wrap.hidden = true;
      delete wrap.dataset.open;
      yes.disabled = false;
      yes.textContent = 'Create task';
      yes.removeEventListener('click', onYes); no.removeEventListener('click', onNo); wrap.removeEventListener('click', onWrap); document.removeEventListener('keydown', onKey);
    }
    function onNo() { close(); }
    function onWrap(e) { if (e.target === wrap) close(); }
    function onKey(e) { if (e.key === 'Escape' && !e.defaultPrevented) close(); }
    async function onYes() {
      var t = title.value.trim();
      if (!proj.value) { err.textContent = 'Pick the project this task goes to.'; err.hidden = false; proj.focus(); return; }
      if (!t) { err.textContent = 'The task needs a title.'; err.hidden = false; title.focus(); return; }
      var p = parseFloat(pts.value);
      yes.disabled = true;
      yes.textContent = 'Creating\u2026';
      try {
        var isTech = !!(tech && proj.value === tech), chosen = proj.value, parent = isTech ? '' : epic.value.trim();
        var made = await api('POST', '/api/actions', {type: 'ticket', target: a.key, key: a.key, title: t, description: desc.value.trim() || undefined, points: isTech || isNaN(p) ? undefined : p,
          project: chosen, epic: parent || undefined, label: 'Create ' + chosen + ' task for ' + name});
        if (made.status === 'failed') { err.textContent = made.result || 'Jira refused the task.'; err.hidden = false; yes.disabled = false; yes.textContent = 'Create task'; return; }
        save(TASK_PROJECT_KEY, chosen);
        close();
        hint.textContent = (made.result || 'Task created') + ' · ' + name;
        await refresh();
        if (window.__skopaxPoll) window.__skopaxPoll();
      } catch (e) {
        yes.disabled = false;
        yes.textContent = 'Create task';
        err.textContent = e && (e.code === 401 || e.code === 403) ? 'Operators only.' : 'Not sent, try again.';
        err.hidden = false;
      }
    }
    yes.addEventListener('click', onYes); no.addEventListener('click', onNo); wrap.addEventListener('click', onWrap); document.addEventListener('keydown', onKey);
    wrap.hidden = false;
    title.focus();
    title.select();
  }
  function confirmDialog(title, text, okLabel) {
    var wrap = document.getElementById('cf-wrap'), yes = document.getElementById('cf-yes'), no = document.getElementById('cf-no');
    document.getElementById('cf-title').textContent = title;
    document.getElementById('cf-text').textContent = text;
    yes.textContent = okLabel;
    return new Promise(function (resolve) {
      function close(ok) {
        wrap.hidden = true;
        yes.removeEventListener('click', onYes); no.removeEventListener('click', onNo); wrap.removeEventListener('click', onWrap); document.removeEventListener('keydown', onKey);
        resolve(ok);
      }
      function onYes() { close(true); }
      function onNo() { close(false); }
      function onWrap(e) { if (e.target === wrap) close(false); }
      function onKey(e) { if (e.key === 'Escape') close(false); }
      yes.addEventListener('click', onYes); no.addEventListener('click', onNo); wrap.addEventListener('click', onWrap); document.addEventListener('keydown', onKey);
      wrap.hidden = false;
      yes.focus();
    });
  }
  var jsonSeq = 0;
  function jsonDialog(a) {
    var wrap = document.getElementById('json-wrap'), body = document.getElementById('json-body'), err = document.getElementById('json-err'), shut = document.getElementById('json-close');
    if (!wrap || !wrap.hidden) return;
    var url = a.getAttribute('href'), seq = ++jsonSeq;
    document.getElementById('json-title').textContent = a.dataset.json || 'Record';
    document.getElementById('json-src').textContent = url;
    document.getElementById('json-raw').setAttribute('href', url);
    body.textContent = 'Loading…';
    body.hidden = false;
    err.hidden = true;
    function opener() {
      if (a.isConnected) return a;
      return [].filter.call(document.querySelectorAll('a[data-json]'), function (x) { return x.getAttribute('href') === url; })[0];
    }
    function close() {
      wrap.hidden = true;
      jsonSeq++;
      shut.removeEventListener('click', close); wrap.removeEventListener('click', onWrap); document.removeEventListener('keydown', onKey);
      var back = opener();
      if (back) back.focus();
    }
    function onWrap(e) { if (e.target === wrap) close(); }
    function onKey(e) { if (e.key === 'Escape') close(); }
    shut.addEventListener('click', close); wrap.addEventListener('click', onWrap); document.addEventListener('keydown', onKey);
    wrap.hidden = false;
    shut.focus();
    api('GET', url).then(function (x) {
      if (seq !== jsonSeq) return;
      body.textContent = JSON.stringify(x, null, 2);
    }, function (e) {
      if (seq !== jsonSeq) return;
      body.hidden = true;
      err.textContent = e && (e.code === 401 || e.code === 403) ? 'Log in to read this record.' : e && e.code ? 'Could not load this record (HTTP ' + e.code + ').' : 'Could not load this record, try again.';
      err.hidden = false;
    });
  }
  function shotDialog(a) {
    var wrap = document.getElementById('shot-wrap'), body = document.getElementById('shot-body'), shut = document.getElementById('shot-close');
    if (!wrap || !wrap.hidden) return;
    var img = document.createElement('img');
    img.id = 'shot-img';
    var id = a.dataset.shot, s = shotById(id) || {id: id}, url = a.getAttribute('href'), thumb = a.querySelector('img');
    document.getElementById('shot-title').textContent = s.title || s.finalUrl || s.url || 'Screenshot';
    document.getElementById('shot-meta').textContent = shotMeta(s);
    document.getElementById('shot-raw').setAttribute('href', url);
    img.setAttribute('src', url);
    img.setAttribute('alt', thumb ? thumb.getAttribute('alt') : 'Screenshot');
    body.textContent = '';
    body.appendChild(img);
    body.classList.remove('actual');
    body.classList.toggle('phone', s.viewport === 'phone');
    function opener() {
      if (a.isConnected) return a;
      return [].filter.call(document.querySelectorAll('a[data-shot]'), function (x) { return x.dataset.shot === id; })[0];
    }
    function zoom() { body.classList.toggle('actual'); }
    function close() {
      wrap.hidden = true;
      shut.removeEventListener('click', close); wrap.removeEventListener('click', onWrap); document.removeEventListener('keydown', onKey);
      img.remove();
      var back = opener();
      if (back) back.focus();
    }
    function onWrap(e) { if (e.target === wrap) close(); }
    function onKey(e) { if (e.key === 'Escape') close(); }
    shut.addEventListener('click', close); wrap.addEventListener('click', onWrap); document.addEventListener('keydown', onKey); img.addEventListener('click', zoom);
    wrap.hidden = false;
    shut.focus();
  }
  function cancelBtn(x) {
    var b = btn('Cancel', 'sec small');
    b.setAttribute('aria-label', 'Cancel ' + label(x));
    b.addEventListener('click', function () { cancel(x, b); });
    return b;
  }
  function iconBtn(icon, text, cls) { var b = btn('', 'with-icon ' + (cls || '')); b.innerHTML = icon + esc(text); return b; }
  function relabel(b, icon, text, hide) {
    var tag = b.classList.contains('labeled') ? 'lbl' : hide && b.classList.contains('icon') ? 'sr' : '';
    b.innerHTML = icon + (tag ? '<span class="' + tag + '">' + esc(text) + '</span>' : esc(text));
  }
  var ICON_MORE = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.9"/><circle cx="12" cy="12" r="1.9"/><circle cx="19" cy="12" r="1.9"/></svg>';
  var ACTS_OPEN = {};
  function actsMenu(menu, on) {
    var more = menu.querySelector('.acts-more'), tog = menu.querySelector('.acts-tog'), card = menu.closest('article.inc');
    if (card) { if (on) ACTS_OPEN[card.dataset.key] = true; else delete ACTS_OPEN[card.dataset.key]; }
    more.hidden = !on;
    tog.setAttribute('aria-expanded', on ? 'true' : 'false');
    tog.dataset.tip = tog.title = on ? 'Close this menu' : menu.closest('.acts-grouped') ? 'More actions: mute for a week, known noise, group' : 'More actions: mute, known noise, group, resolve, copy link';
  }
  document.addEventListener('click', function (e) {
    if (!e.target.isConnected) return;
    document.querySelectorAll('.acts-menu').forEach(function (m) { if (!m.querySelector('.acts-more').hidden && !m.contains(e.target)) actsMenu(m, false); });
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    var m = e.target.closest && e.target.closest('.acts-menu');
    if (!m || m.querySelector('.acts-more').hidden) return;
    actsMenu(m, false);
    m.querySelector('.acts-tog').focus();
  });
  function foldActs(a, box, dive) {
    var stat = box.querySelector('.stat');
    var menu = el('span', 'acts-menu');
    var more = el('span', 'acts-more');
    more.id = 'acts-more-' + box.dataset.i;
    more.setAttribute('role', 'group');
    more.setAttribute('aria-label', 'More actions');
    [].slice.call(box.children).forEach(function (c) { if (c !== dive && c !== stat && !c.classList.contains('acts-keep')) more.appendChild(c); });
    var tog = btn('More actions', 'sec free acts-tog');
    tog.classList.add('icon');
    tog.innerHTML = ICON_MORE + '<span class="sr">More actions</span>';
    tog.setAttribute('aria-controls', more.id);
    tog.setAttribute('aria-haspopup', 'true');
    tog.addEventListener('click', function () { actsMenu(menu, more.hidden); });
    menu.append(tog, more);
    box.insertBefore(menu, stat);
    actsMenu(menu, !!ACTS_OPEN[a.key]);
  }
  var ICON_CHAT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>';
  function actGroup(box, id, label, cls) {
    var g = el('div', 'act-group' + (cls ? ' ' + cls : '')), h = el('span', 'ag-h'), body = el('div', 'ag-body');
    h.id = 'ag-' + id + '-' + box.dataset.i;
    h.textContent = label;
    g.setAttribute('role', 'group');
    g.setAttribute('aria-labelledby', h.id);
    g.append(h, body);
    g.body = body;
    return g;
  }
  function showWord(b) {
    var sr = b && b.querySelector(':scope > .sr');
    if (sr) sr.className = 'lbl';
    return b;
  }
  function groupActs(a, box, p) {
    var keep = function (n) { return n || null; };
    box.classList.add('acts-grouped');
    var ai = actGroup(box, 'ai', 'AI', 'ai'), aseg = el('div', 'seg');
    if (a.num) {
      var chat = el('a', 'btn sec seg-b chat-btn', ICON_CHAT + '<span class="lbl">Chat about this card</span>');
      chat.href = '/#t=chat/new/' + encodeURIComponent(a.num);
      chat.title = 'Opens a new private chat with this card attached: ask why it fired, what the investigation found, who should look';
      aseg.append(chat);
    }
    if (p.dive) { p.dive.classList.add('seg-b'); aseg.append(showWord(p.dive)); }
    if (aseg.children.length) ai.body.append(aseg);
    var status = actGroup(box, 'status', 'Status'), seg = el('div', 'seg');
    [[p.take, a.triage === 'progress'], [p.res, false], [p.mute, false], [p.un, true]].forEach(function (x) {
      if (!x[0]) return;
      x[0].classList.add('seg-b');
      x[0].setAttribute('aria-pressed', x[1] ? 'true' : 'false');
      if (x[0] === p.take) {
        x[0].innerHTML = ICON_PLAY + '<span class="sr">' + (x[1] ? 'In progress' : 'Start progress') + '</span>';
        x[0].setAttribute('aria-label', x[1] ? 'In progress' : 'Start progress');
      }
      seg.append(showWord(x[0]));
    });
    if (seg.children.length) status.body.append(seg);
    var sev = box.querySelector('.sev-act'), menu = box.querySelector('.acts-menu');
    if (sev) status.body.append(sev);
    if (menu) { status.body.append(menu); menu.hidden = !menu.querySelector('.acts-more').children.length; actsMenu(menu, !!ACTS_OPEN[a.key]); }
    var owner = actGroup(box, 'owner', 'Owner');
    [box.querySelector(':scope > .asg-btn'), box.querySelector(':scope > .asg-form'), box.querySelector(':scope > .asg-msg')].forEach(function (n) { if (n) owner.body.append(n); });
    var links = actGroup(box, 'links', 'Links', 'links');
    var jira = box.querySelector(':scope > .jira-acts');
    if (jira) links.body.append(jira);
    if (p.copy) links.body.append(showWord(p.copy));
    var foot = el('div', 'act-foot'), stat = box.querySelector(':scope > .stat'), last = box.closest('article.inc').querySelector('.last-act');
    if (last) foot.append(last);
    if (stat) foot.append(stat);
    var forms = [keep(p.tform), keep(p.rform), box.querySelector('.sev-form')].filter(Boolean);
    var after = [box.querySelector(':scope > .jira-form')].filter(Boolean);
    if (aseg.children.length) box.append(ai);
    box.append(status);
    forms.forEach(function (f) { box.append(f); });
    box.append(owner, links);
    after.forEach(function (f) { box.append(f); });
    box.append(foot);
  }
  var ICON_PLAY = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M10 8.5l5 3.5-5 3.5z"></path></svg>';
  var ICON_SPARKLES = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 5Q11 12 18 13Q11 14 10 21Q9 14 2 13Q9 12 10 5Z"></path><path d="M19 2v6"></path><path d="M16 5h6"></path></svg>';
  var ICON_BELL = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"></path><path d="M13.73 21a2 2 0 0 1-3.46 0"></path></svg>';
  var ICON_BELL_OFF = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13.73 21a2 2 0 0 1-3.46 0"></path><path d="M18.63 13A17.89 17.89 0 0 1 18 8"></path><path d="M6.26 6.26A5.86 5.86 0 0 0 6 8c0 7-3 9-3 9h14"></path><path d="M18 8a6 6 0 0 0-9.33-5"></path><line x1="1" y1="1" x2="23" y2="23"></line></svg>';
  var ICON_EYE_OFF = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"></path><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"></path><line x1="1" y1="1" x2="23" y2="23"></line></svg>';
  var ICON_DONE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline></svg>';
  var ICON_REPLY = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9 17 4 12 9 7"></polyline><path d="M20 18v-2a4 4 0 0 0-4-4H4"></path></svg>';
  var ICON_TAKE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"></path><circle cx="9" cy="7" r="4"></circle><polyline points="16 11 18 13 22 9"></polyline></svg>';
  var ICON_GROUP = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="12 2 2 7 12 12 22 7 12 2"></polygon><polyline points="2 17 12 22 22 17"></polyline><polyline points="2 12 12 17 22 12"></polyline></svg>';
  var GP_MAX = 8, GP_NEAR_MS = 30 * 60000;
  function gpHas(v) { return v != null && String(v).trim() !== '' && String(v).trim() !== '-'; }
  function gpName(o) { return svcName(o); }
  function gpFold(s) { return typeof foldCase === 'function' ? foldCase(s) : String(s == null ? '' : s).toLowerCase(); }
  function gpText(o, cache) {
    if (cache && cache.has(o)) return cache.get(o);
    var owner = [o.owner, o.ownerName, o.assigned && o.assigned.name].filter(gpHas).join(' ');
    var tags = (o.tags || []).map(function (t) { return t && t.id ? t.id + ' ' + (typeof tagName === 'function' ? tagName(t.id) : '') : ''; }).join(' ');
    var text = gpFold([o.svc, o.label, o.sub, o.ns, o.cluster, o.src, o.ticket, owner, tags, gpName(o)].filter(gpHas).join(' '));
    if (cache) cache.set(o, text);
    return text;
  }
  function gpMatch(o, q, cache) {
    var raw = String(q || '').trim();
    if (!raw) return true;
    var hash = /^#\s*(\d+)$/.exec(raw);
    if (hash) return String(o.num || '').indexOf(hash[1]) === 0;
    if (/^\d+$/.test(raw) && String(o.num) === raw) return true;
    var hay = gpText(o, cache);
    return gpFold(raw).split(/\s+/).every(function (w) { return hay.indexOf(w.replace(/^#/, '')) >= 0; });
  }
  function gpSort(list) {
    return list.slice().sort(function (x, y) { return bySevOrd(x, y) || (Date.parse(x.firstSeen) || 0) - (Date.parse(y.firstSeen) || 0) || (x.num || 0) - (y.num || 0); });
  }
  function gpSuggest(a, others) {
    var at = Date.parse(a.firstSeen), left = others.slice(), out = [];
    var take = function (test) {
      var hit = left.filter(test);
      left = left.filter(function (o) { return hit.indexOf(o) < 0; });
      out = out.concat(gpSort(hit));
    };
    take(function (o) { return gpHas(a.svc) && o.svc === a.svc; });
    take(function (o) { return (gpHas(a.ns) && o.ns === a.ns) || (gpHas(a.cluster) && o.cluster === a.cluster); });
    take(function (o) { var t = Date.parse(o.firstSeen); return at > 0 && t > 0 && Math.abs(t - at) <= GP_NEAR_MS; });
    return out.concat(gpSort(left));
  }
  function groupPicker(a, others, id, name, onPick) {
    var wrap = el('div', 'gp'), input = el('input'), list = el('div', 'gp-list');
    var shown = [], at = -1, picked = null, cache = new Map();
    input.type = 'text'; input.id = 'parent-' + id; input.maxLength = 100; input.autocomplete = 'off';
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-expanded', 'false');
    input.setAttribute('aria-controls', 'gpl-' + id);
    input.setAttribute('aria-label', 'Parent card for ' + name);
    input.placeholder = tx('Search by number, service, label or ticket');
    list.id = 'gpl-' + id; list.hidden = true;
    list.setAttribute('role', 'listbox');
    list.setAttribute('aria-label', 'Cards to group under');
    list.addEventListener('mousedown', function (e) { e.preventDefault(); });
    wrap.append(input, list);
    function close() { cache = new Map(); list.hidden = true; at = -1; input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant'); }
    function mark() {
      [].forEach.call(list.querySelectorAll('[role="option"]'), function (r, i) { r.setAttribute('aria-selected', String(i === at)); });
      if (at >= 0) { input.setAttribute('aria-activedescendant', 'gpo-' + id + '-' + at); var r = list.children[at]; if (r && r.scrollIntoView) r.scrollIntoView({block: 'nearest'}); } else input.removeAttribute('aria-activedescendant');
    }
    function row(o, i) {
      var r = el('div', 'gp-o');
      r.id = 'gpo-' + id + '-' + i;
      r.setAttribute('role', 'option');
      r.setAttribute('aria-selected', 'false');
      r.dataset.key = o.key;
      var n = el('span', 'gp-num'), c = el('span', 'sev ' + (SEV[o.sev] || [o.sev, 'k-muted'])[1]), nm = el('span', 'gp-name'), lb = el('span', 'gp-label'), ag = el('span', 'gp-age');
      n.textContent = o.num ? '#' + o.num : '';
      c.textContent = tx((SEV[o.sev] || [o.sev])[0]);
      nm.textContent = gpName(o); nm.setAttribute('data-no-tx', '');
      lb.textContent = gpHas(o.label) && o.label !== gpName(o) ? o.label : ''; lb.setAttribute('data-no-tx', '');
      ag.textContent = typeof ageShort === 'function' ? ageShort(o.firstSeen) : '';
      r.append(n, c, nm, lb);
      if (o.status === 'cleared') { var st = el('span', 'gp-stop'); st.textContent = tx('stopped'); r.append(st); }
      r.append(ag);
      r.addEventListener('mousedown', function (e) { e.preventDefault(); });
      r.addEventListener('click', function () { pick(o); });
      return r;
    }
    function render() {
      var q = input.value.trim(), all = q ? gpSuggest(a, others).filter(function (o) { return gpMatch(o, q, cache); }) : gpSuggest(a, others);
      shown = all.slice(0, GP_MAX);
      list.textContent = '';
      shown.forEach(function (o, i) { list.append(row(o, i)); });
      if (!shown.length) { var none = el('div', 'gp-empty'); none.setAttribute('role', 'presentation'); none.textContent = tx('No card matches'); list.append(none); }
      if (all.length > shown.length) { var more = el('div', 'gp-more'); more.setAttribute('role', 'presentation'); more.textContent = tx((all.length - shown.length) + ' more - type to search'); list.append(more); }
      at = -1;
      list.hidden = false;
      input.setAttribute('aria-expanded', 'true');
      mark();
    }
    function pick(o) {
      picked = o;
      input.value = (o.num ? '#' + o.num + ' ' : '') + gpName(o) + (gpHas(o.label) && o.label !== gpName(o) ? ' ' + o.label : '');
      close();
      onPick(o);
    }
    input.addEventListener('focus', render);
    input.addEventListener('click', function () { if (list.hidden) render(); });
    input.addEventListener('blur', close);
    input.addEventListener('input', function () { if (picked) { picked = null; onPick(null); } render(); });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (list.hidden) { render(); return; }
        if (!shown.length) return;
        at = e.key === 'ArrowDown' ? (at + 1) % shown.length : (at <= 0 ? shown.length - 1 : at - 1);
        mark();
      } else if (e.key === 'Enter') {
        if (list.hidden) return;
        e.preventDefault();
        var o = shown[at >= 0 ? at : 0];
        if (o && (at >= 0 || input.value.trim())) pick(o);
      } else if (e.key === 'Escape') {
        e.stopPropagation();
        if (!list.hidden) { e.preventDefault(); close(); return; }
        if (wrap.onleave) wrap.onleave();
      }
    });
    return {box: wrap, input: input, current: function () { return picked; }};
  }
  var ICON_TICKET = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="8" y="2" width="8" height="4" rx="1" ry="1"></rect><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"></path><path d="M12 11v6"></path><path d="M9 14h6"></path></svg>';
  function jiraControls(a, box, name) {
    var key = ticketKey(a.ticket), wrap = el('span', 'jira-acts acts-keep');
    var form = el('div', 'reason jira-form acts-keep');
    form.hidden = true;
    var inp = el('input');
    inp.type = 'text'; inp.maxLength = 100; inp.id = 'jira-link-' + box.dataset.i;
    inp.placeholder = me.jira ? 'Jira key, or words from the task title' : 'Ticket key, like OPS-123';
    inp.setAttribute('aria-label', (me.jira ? 'Jira ticket for ' : 'Ticket for ') + name);
    var go = btn('Link', 'small jira-go'), msg = el('span', 'jira-msg');
    form.append(inp, go, msg);
    function toggle() { form.hidden = !form.hidden; msg.textContent = ''; if (!form.hidden) inp.focus(); }
    if (key) {
      var href = jiraHref(key);
      var open = el(href ? 'a' : 'span', 'btn sec jira-key', ICON_TICKET + '<span>' + esc(key) + (href ? ' ↗' : '') + '</span>');
      if (href) { open.href = href; open.target = '_blank'; open.rel = 'noopener'; open.title = 'Open ' + key + ' in Jira'; }
      var change = btn('Change ticket', 'sec jira-act');
      change.title = (me.jira ? 'Link another Jira ticket instead of ' : 'Link another ticket instead of ') + key;
      change.addEventListener('click', toggle);
      wrap.append(open, change);
    } else {
      var make = btn('Create Jira task', 'sec jira-btn jira-act');
      make.innerHTML = ICON_TICKET + '<span>Create Jira task</span>';
      make.dataset.tip = make.title = 'Files a Jira task from this card (title, points and description in a dialog), then takes the card with it';
      make.addEventListener('click', function () { if (make.getAttribute('aria-disabled') !== 'true') taskDialog(a, name); });
      var linkText = me.jira ? 'Link Jira ticket' : 'Link ticket';
      var link = btn(linkText, 'sec jira-act');
      link.innerHTML = ICON_LINK + '<span>' + linkText + '</span>';
      link.dataset.tip = link.title = me.jira ? 'Link a Jira ticket that already exists: the card goes In progress and stays until the ticket closes; the assignee stays'
        : 'Link a ticket by its key: the card goes In progress; Jira is not set up, so nothing closes the card when the ticket closes';
      link.addEventListener('click', toggle);
      wrap.append(make, link);
    }
    box.append(wrap, form);
    if (me.jira && typeof ticketSuggest === 'function') ticketSuggest(inp);
    go.addEventListener('click', function () {
      var k = ticketKey(inp.value.trim().toUpperCase());
      if (!k) { msg.textContent = me.jira ? 'Pick a ticket from the list or type its key, like OPS-123' : 'Type the ticket key, like OPS-123'; inp.focus(); return; }
      queue({type: 'link', target: a.key, key: a.key, ticket: k, label: 'Link ' + name + ' to ' + k}, go);
    });
    inp.addEventListener('keydown', function (e) { if (e.key === 'Enter' && inp.getAttribute('aria-expanded') !== 'true') { e.preventDefault(); go.click(); } });
  }
  function sevControls(a, box, name) {
    var open = btn('Change severity', 'sec sev-act acts-keep');
    var auto = a.sevAuto || (a.sevSet && a.sevSet.auto);
    open.dataset.tip = open.title = 'Severity now: ' + (SEV[a.sev] || SEV.info)[0] + (a.sevSet ? ', set by hand by ' + draftBy(a.sevSet.by) + '; the signal says ' + (SEV[auto] || SEV.info)[0] : ', from the signal') +
      '. A hand-set severity stays through every check until you set it back; Critical pushes, Move to Information takes it off the board onto the Information page';
    var form = el('div', 'reason sev-form acts-keep');
    form.hidden = true;
    form.setAttribute('role', 'group');
    form.setAttribute('aria-label', 'Severity for ' + name);
    [['crit', 'Critical'], ['high', 'High'], ['warn', 'Warning'], ['info', 'Move to Information']].concat(a.sevSet ? [['auto', 'Back to the signal']] : []).forEach(function (o) {
      var b = btn(o[1], 'sec small sev-pick');
      b.dataset.sev = o[0];
      if (o[0] === a.sev && a.sevSet) b.setAttribute('aria-pressed', 'true');
      if (o[0] === 'auto') b.title = 'The signal says ' + (SEV[auto] || SEV.info)[0];
      b.addEventListener('click', function () {
        if (o[0] === a.sev && o[0] !== 'auto' && !a.sevSet) { form.hidden = true; return; }
        queue({type: 'severity', target: a.key, key: a.key, sev: o[0], label: (o[0] === 'auto' ? 'Severity back to the signal: ' : o[0] === 'info' ? 'Moved to Information: ' : 'Severity ' + o[1] + ': ') + name}, b, o[0] === 'info' && cardInFlight(a) ? function () { stayToast(a); } : null);
      });
      form.appendChild(b);
    });
    open.addEventListener('click', function () { form.hidden = !form.hidden; if (!form.hidden) form.querySelector('button').focus(); });
    box.append(open, form);
  }
  function asIcon(b, icon, tip, shown) {
    var text = b.textContent;
    b.classList.add('icon');
    if (shown) b.classList.add('labeled');
    b.innerHTML = icon + '<span class="' + (shown ? 'lbl' : 'sr') + '">' + esc(text) + '</span>';
    b.setAttribute('aria-label', text);
    b.dataset.tip = b.title = tip || text;
    return b;
  }
  function copyLink(a, b) { copyUrl(a.num ? location.origin + '/c/' + a.num : location.origin + location.pathname + '#a=' + linkKey(a.key), b); }
  function waiting(a) { return !!draftOf(a) && !draftSent(a); }
  function slackLabel(a) { return a.slackPost ? 'Send again' : 'Send to Slack'; }
  function slackTip(a) {
    if (waiting(a)) return 'Post the text kept on this card as the message, with the anomaly card attached below it; click twice to confirm';
    if (a.slackPost) return 'Already posted ' + dayhm(a.slackPost.at) + ' by ' + draftBy(a.slackPost.by, (a.slackDraft || {}).by) + '; this posts it to the channel again' + (draftOf(a) ? ', with its text' : '') + '; click twice to confirm';
    return 'Post this anomaly to the Slack channel; click twice to confirm';
  }
  function slackButton(a, b) {
    relabel(b, ICON_SEND, slackLabel(a), true);
    b.dataset.tip = slackTip(a);
    if (!b.dataset.locked) b.title = b.dataset.tip;
  }
  function slackClick(a, b) {
    if (b.dataset.confirm) { delete b.dataset.confirm; clearTimeout(+b.dataset.timer); sendSlack(a, b); return; }
    b.dataset.confirm = '1';
    relabel(b, ICON_SEND, 'Post ' + (a.slackPost ? 'again ' : '') + 'to ' + (me.slackChannel || 'Slack') + '?');
    b.dataset.timer = setTimeout(function () { delete b.dataset.confirm; slackButton(a, b); }, 5000);
  }
  async function sendSlack(a, b) {
    b.disabled = true;
    relabel(b, ICON_SEND, 'Sending…');
    try {
      await api('POST', '/api/slack-posts', {key: a.key});
      relabel(b, ICON_SEND, 'Sent');
      await refresh();
      if (window.__skopaxPoll) window.__skopaxPoll();
    } catch (e) {
      relabel(b, ICON_SEND, e && e.code === 502 ? 'Slack refused it, see History' : e && e.code === 503 ? 'Slack is not set up' : 'Not sent, try again');
      if (e && e.code === 502) refresh();
    }
    setTimeout(function () { b.disabled = false; slackButton(a, b); }, 3000);
  }
  function teamsTakes(r, a) { var ids = tagIds(a); return !!r.tagged && (r.tags || []).some(function (t) { return ids.indexOf(t) >= 0; }); }
  function routeName(r) { return (r.plugin ? r.plugin + ' \u00b7 ' : '') + r.name; }
  function teamsTip(a, r) {
    if (r.business) return (a.business && a.business.text ? 'Post the business sentence to ' + routeName(r) + ', with the page link and no technical detail' : 'Write the business sentence first: ' + routeName(r) + ' reads only that') + '; click twice to confirm';
    return 'Post this card to ' + routeName(r) + (r.tagged ? '' : ': it takes cards by their keys') + '; click twice to confirm';
  }
  function teamsButtons(a) {
    var routes = (me.routes || []).filter(function (r) { return r && r.id && (!r.tagged || teamsTakes(r, a)); }).sort(function (x, y) { return teamsTakes(y, a) - teamsTakes(x, a); });
    return routes.map(function (r) {
      var b = asIcon(btn(routeName(r), 'sec teams-btn'), ICON_SEND, teamsTip(a, r), true);
      b.dataset.route = r.id;
      b.addEventListener('click', function () { teamsClick(a, r, b); });
      return b;
    });
  }
  function routeSay(b, text) { relabel(b, ICON_SEND, text, true); b.setAttribute('aria-label', text); }
  function teamsReset(a, r, b) { routeSay(b, routeName(r)); b.dataset.tip = teamsTip(a, r); if (!b.dataset.locked) b.title = b.dataset.tip; }
  function teamsClick(a, r, b) {
    if (b.dataset.confirm) { delete b.dataset.confirm; clearTimeout(+b.dataset.timer); sendTeams(a, r, b); return; }
    b.dataset.confirm = '1';
    routeSay(b, 'Post to ' + routeName(r) + '?');
    b.dataset.timer = setTimeout(function () { delete b.dataset.confirm; teamsReset(a, r, b); }, 5000);
  }
  async function sendTeams(a, r, b) {
    b.disabled = true;
    routeSay(b, 'Sending\u2026');
    try {
      await api('POST', '/api/routes/send', {key: a.key, route: r.id});
      routeSay(b, 'Sent to ' + routeName(r));
      if (window.__skopaxPoll) window.__skopaxPoll();
    } catch (e) {
      routeSay(b, e && e.code === 502 ? (e.detail || 'The chat tool did not take it') : e && e.code === 422 && e.detail ? e.detail : 'Not sent, try again');
    }
    setTimeout(function () { b.disabled = false; teamsReset(a, r, b); }, 3000);
  }
  var ASG_DM = {sent: ' · told on Slack', none: ' · no Slack account found for them, not messaged', failed: ' · the Slack message failed'};
  var ICON_ASSIGN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"></path><circle cx="9" cy="7" r="4"></circle><line x1="19" y1="8" x2="19" y2="14"></line><line x1="22" y1="11" x2="16" y2="11"></line></svg>';
  async function setAsg(a, form, msg, body, b) {
    b.disabled = true;
    try {
      var r = await api('PUT', '/api/assignee', Object.assign({key: a.key}, body));
      a.owner = r.owner || undefined; a.assigned = r.assigned || undefined;
      ASG_NOTE[a.key] = (r.owner ? 'Assigned to ' + ownerName(r.owner) + (ASG_DM[r.dm] || '') : 'Assignee removed') + (r.thread ? ' · noted in the Slack thread' : '');
      msg.textContent = ASG_NOTE[a.key];
      if (form.asgSet) form.asgSet(false); else form.hidden = true;
      if (window.__skopaxPoll) window.__skopaxPoll();
    } catch (e) {
      msg.textContent = e && e.code === 404 ? 'This card is gone' : e && e.code === 422 ? 'That name or address was not accepted' : 'Not saved, try again';
    }
    b.disabled = false;
  }
  var ASG_OPEN = {};
  function personLabel(o) {
    var s = ownerName(o);
    return String(o || '').indexOf('@') > 0 ? s.split(/[._-]/).filter(Boolean).map(function (p) { return p.charAt(0).toUpperCase() + p.slice(1); }).join(' ') : s;
  }
  function personInitials(o) { return ownerName(o).split(/[._\s-]/).filter(Boolean).slice(0, 2).map(function (p) { return p.charAt(0).toUpperCase(); }).join(''); }
  function asgPlace(box, open, form) { form.style.left = Math.max(0, Math.min(open.offsetLeft, box.clientWidth - form.offsetWidth)) + 'px'; }
  document.addEventListener('click', function (e) {
    if (!e.target.isConnected) return;
    document.querySelectorAll('.acts .asg-form:not([hidden])').forEach(function (f) {
      var b = f.parentNode.querySelector('.asg-btn');
      if (!f.contains(e.target) && !(b && b.contains(e.target)) && f.asgSet) f.asgSet(false);
    });
  });
  function assignControls(a, box, name) {
    var tip = a.owner ? 'Assignee: ' + ownerName(a.owner) + asgHow(a, ' · ') + '. Click to change or remove' : 'Assign: pick who owns this card; it keeps its status';
    var open = asIcon(btn('Assign', 'sec asg-btn acts-keep login-ok'), ICON_ASSIGN, tip, true);
    if (a.owner) { open.innerHTML = '<span class="lbl">' + esc(personLabel(a.owner)) + '</span>'; open.dataset.ini = personInitials(a.owner); open.classList.add('has-av'); }
    open.insertAdjacentHTML('beforeend', '<span class="asg-chev" aria-hidden="true"></span>');
    open.setAttribute('aria-haspopup', 'dialog');
    var form = el('div', 'asg-form acts-keep');
    form.hidden = true;
    form.setAttribute('role', 'dialog');
    form.setAttribute('aria-label', 'Assign ' + name);
    var inp = el('input'), picked = null;
    inp.type = 'search'; inp.maxLength = 80; inp.id = 'asg-' + box.dataset.i; inp.placeholder = me.jira ? 'Search people in Jira' : 'Name'; inp.autocomplete = 'off';
    inp.setAttribute('aria-label', 'Assignee of ' + name);
    var ok = btn('Assign', 'small asg-save login-ok'), mine = btn('Assign to me', 'sec small asg-me login-ok'), clear = btn('Remove assignee', 'sec small asg-clear login-ok');
    ok.title = 'They get one Slack message with the card link, and the card\'s Slack thread is told when it has one';
    if (ME_USER) mine.dataset.ini = personInitials(ME_USER);
    var msg = el('span', 'asg-msg acts-keep');
    msg.textContent = ASG_NOTE[a.key] || '';
    form.append(el('div', 'asg-head', '<b>' + esc(a.num ? 'Assign card #' + a.num : 'Assign this card') + '</b>' + (a.owner ? '<span>now ' + esc(personLabel(a.owner)) + '</span>' : '')), inp);
    var quick = el('div', 'asg-quick');
    if (!isMine(a)) quick.append(mine);
    if (a.owner) quick.append(clear);
    if (quick.children.length) form.append(quick);
    var foot = el('div', 'asg-foot', '<span>They get one Slack message with the card link.</span>');
    foot.append(ok);
    form.append(foot);
    if (me.jira && typeof ticketSuggest === 'function') ticketSuggest(inp, {url: '/api/people', none: 'No person matches', fail: 'Jira search failed; type the name', picked: function (it) { picked = it; }});
    var set = function (on, focus) {
      form.hidden = !on;
      open.setAttribute('aria-expanded', on ? 'true' : 'false');
      if (on) ASG_OPEN[a.key] = 1; else delete ASG_OPEN[a.key];
      if (!on) return;
      asgPlace(box, open, form);
      if (focus) inp.focus();
    };
    form.asgSet = set;
    open.addEventListener('click', function () { set(form.hidden, true); });
    form.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !e.defaultPrevented) { set(false); open.focus(); } });
    inp.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.defaultPrevented && inp.value.trim()) { e.preventDefault(); ok.click(); } });
    ok.addEventListener('click', function () {
      var who = inp.value.trim();
      if (!who) { inp.focus(); return; }
      setAsg(a, form, msg, {name: who, email: picked && picked.name === who ? picked.email : null}, ok);
    });
    mine.addEventListener('click', function () { setAsg(a, form, msg, {me: true}, mine); });
    clear.addEventListener('click', function () { setAsg(a, form, msg, {name: ''}, clear); });
    box.append(open, form, msg);
    set(!!ASG_OPEN[a.key], false);
  }
  function wireBiz(a, bbox) {
    var form = bbox.querySelector('.biz-form'), ta = form.querySelector('textarea'), save = bbox.querySelector('.biz-save');
    bbox.querySelectorAll('.biz-tb').forEach(function (b) { b.addEventListener('click', function () { toggleTag(a, b); }); });
    bbox.querySelector('.biz-edit').addEventListener('click', function () { form.hidden = !form.hidden; if (!form.hidden) ta.focus(); });
    bbox.querySelector('.biz-cancel').addEventListener('click', function () { ta.blur(); form.hidden = true; ta.value = (a.business || {}).text || ''; bbox.querySelector('.biz-why-in').value = (a.business || {}).why || ''; bbox.querySelector('.biz-kind').value = (a.business || {}).kind === 'info' ? 'info' : 'action'; });
    save.addEventListener('click', function () { saveBiz(a, bbox, save); });
  }
  async function toggleTag(a, b) {
    var on = b.getAttribute('aria-pressed') !== 'true', text = b.textContent;
    b.disabled = true;
    try {
      var r = await api('POST', '/api/tags', {key: a.key, tag: b.dataset.tag, on: on});
      a.tags = r.tags; a.tagsOff = r.tagsOff; a.business = r.business;
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      bizNoteSync(a, b.closest('.biz'));
      if (window.__skopaxPoll) window.__skopaxPoll();
    } catch (e) {
      b.textContent = 'Not saved';
      setTimeout(function () { b.textContent = text; }, 2000);
    }
    b.disabled = false;
  }
  async function saveBiz(a, bbox, b) {
    var ta = bbox.querySelector('textarea');
    b.disabled = true;
    b.textContent = 'Saving\u2026';
    try {
      var r = await api('PUT', '/api/business', {key: a.key, text: ta.value, why: bbox.querySelector('.biz-why-in').value, kind: bbox.querySelector('.biz-kind').value});
      a.business = r.business;
      ta.blur();
      bbox.querySelector('.biz-form').hidden = true;
      bizNoteSync(a, bbox);
      b.textContent = 'Save';
      if (window.__skopaxPoll) window.__skopaxPoll();
    } catch (e) {
      b.textContent = e && e.code === 422 ? 'Too long, max 500' : 'Not saved, try again';
    }
    b.disabled = false;
  }
  async function postUpdate(a, ta, b, form) {
    b.disabled = true;
    b.textContent = 'Posting…';
    try {
      await api('POST', '/api/slack-updates', {key: a.key, text: ta.value});
      ta.blur();
      form.hidden = true;
      b.textContent = 'Post to thread';
      await refresh();
    } catch (e) {
      b.textContent = e && e.code === 409 ? 'No Slack message to reply to' : e && e.code === 502 ? 'Slack refused it, see History' : e && e.code === 422 ? 'Write the update first' : 'Not sent, try again';
      if (e && e.code === 502) refresh();
    }
    b.disabled = false;
  }
  function wireDraft(a, dbox) {
    var copy = dbox.querySelector('.draft-copy');
    copy.addEventListener('click', function () {
      function shown(t) { copy.textContent = t; setTimeout(function () { copy.textContent = 'Copy'; }, 2000); }
      var d = draftOf(a);
      if (!d || !navigator.clipboard) { shown('Select the text to copy'); return; }
      navigator.clipboard.writeText(d.text).then(function () { shown('Copied'); }, function () { shown('Select the text to copy'); });
    });
  }
  function repSlackTip(r) {
    if (waiting(r)) return 'Post the text kept on this report as the message, with the report summary, its flagged rows and the shipped tickets under it; click twice to confirm';
    if (r.slackPost) return 'Already posted ' + dayhm(r.slackPost.at) + ' by ' + draftBy(r.slackPost.by, (r.slackDraft || {}).by) + '; this posts it to the channel again' + (draftOf(r) ? ', with the draft' : '') + '; click twice to confirm';
    return 'Post this deploy report to the Slack channel: release, services, clean or flagged, the summary, the flagged rows and the shipped tickets; click twice to confirm';
  }
  var REASSESS_TIP = "Run the investigator's assessment of this report again (Opus). Replaces the summary; never posts to Slack again.";
  function openAssess(r) {
    return all.filter(function (x) { return x.type === 'assess' && x.reportId === r.id && (x.status === 'requested' || x.status === 'processing'); })[0];
  }
  function reassessState(b, r) {
    var x = openAssess(r);
    b.disabled = !!x;
    b.textContent = x ? (x.status === 'processing' ? 'Re-assessing…' : 'Re-assessment queued') : 'Re-assess';
  }
  async function reassess(r, b) {
    b.disabled = true;
    try {
      await api('POST', '/api/actions', {type: 'assess', key: 'report:' + r.id, target: 'report:' + r.id, reportId: r.id});
      b.textContent = 'Re-assessment queued';
      await refresh();
      if (window.__skopaxPoll) window.__skopaxPoll();
    } catch (e) {
      if (e && (e.code === 401 || e.code === 403)) { b.disabled = false; return; }
      b.textContent = e && e.detail ? e.detail : 'Not saved, try again';
      setTimeout(function () { reassessState(b, r); }, 4000);
    }
  }
  function wireReps(root) {
    root.querySelectorAll('.rep-acts').forEach(function (box) {
      if (box.dataset.wired) return;
      box.dataset.wired = '1';
      var r = box.__rep;
      var b = iconBtn(ICON_SEND, slackLabel(r), 'sec small op-btn slack-btn');
      b.dataset.tip = b.title = repSlackTip(r);
      b.addEventListener('click', function () { repSlackClick(r, b); });
      var st = el('span', 'slack-st');
      if (r.slackPost) st.innerHTML = slackLink(r.slackPost.permalink, 'Sent to Slack ' + dayhm(r.slackPost.at)) + esc(' · ' + draftBy(r.slackPost.by, (r.slackDraft || {}).by) + (r.slackPost.draft ? ' · investigator text' : ''));
      box.append(b);
      var d = draftOf(r);
      if (d && !draftSent(r)) {
        var tag = el('span', 'tag k-act draft-q', ICON_SEND + esc('Slack text · ' + draftBy(d.by) + ' ' + dayhm(d.at)));
        tag.tabIndex = 0;
        tag.title = d.text + '\n\n' + draftBy(d.by) + ' · ' + dayhm(d.at) + ' · Send to Slack posts this text';
        tag.setAttribute('aria-label', 'Slack text: ' + d.text);
        var copy = btn('Copy', 'sec small free draft-copy');
        copy.title = 'Copy the text, e.g. to send it to one person instead of the channel';
        box.append(tag, copy);
        wireRepDraft(r, box);
      }
      box.append(st);
      var again = btn('Re-assess', 'sec small op-btn reassess-btn');
      again.dataset.tip = again.title = REASSESS_TIP;
      reassessState(again, r);
      again.addEventListener('click', function () { reassess(r, again); });
      box.append(again);
    });
    gate();
  }
  function repSlackClick(r, b) {
    if (b.dataset.confirm) { delete b.dataset.confirm; clearTimeout(+b.dataset.timer); sendRep(r, b); return; }
    b.dataset.confirm = '1';
    relabel(b, ICON_SEND, 'Post ' + (r.slackPost ? 'again ' : '') + 'to ' + (me.slackChannel || 'Slack') + '?');
    b.dataset.timer = setTimeout(function () { delete b.dataset.confirm; relabel(b, ICON_SEND, slackLabel(r)); }, 5000);
  }
  async function sendRep(r, b) {
    b.disabled = true;
    relabel(b, ICON_SEND, 'Sending…');
    try {
      await api('POST', '/api/deploy-reports/' + encodeURIComponent(r.id) + '/slack-posts');
      relabel(b, ICON_SEND, 'Sent');
      await refresh();
      await reports.refresh();
    } catch (e) {
      relabel(b, ICON_SEND, e && e.code === 502 ? 'Slack refused it, see History' : e && e.code === 503 ? 'Slack is not set up' : e && e.code === 404 ? 'Report not found' : 'Not sent, try again');
      if (e && e.code === 502) refresh();
    }
    setTimeout(function () { b.disabled = false; relabel(b, ICON_SEND, slackLabel(r)); }, 3000);
  }
  function wireRepDraft(r, box) {
    var copy = box.querySelector('.draft-copy');
    copy.addEventListener('click', function () {
      function shown(t) { copy.textContent = t; setTimeout(function () { copy.textContent = 'Copy'; }, 2000); }
      if (!navigator.clipboard) { shown('Select the text to copy'); return; }
      navigator.clipboard.writeText(draftOf(r).text).then(function () { shown('Copied'); }, function () { shown('Select the text to copy'); });
    });
  }
  function sqPath(row) {
    return row.kind === 'report' ? '/api/deploy-reports/' + encodeURIComponent(row.x.id || '') + '/slack-posts' : '/api/slack-posts';
  }
  async function sqRefresh() {
    await refresh();
    await reports.refresh();
    if (window.__skopaxPoll) await window.__skopaxPoll();
    renderSlackQ();
  }
  function sqArm(b, html, run) {
    if (b.dataset.confirm) { delete b.dataset.confirm; clearTimeout(+b.dataset.timer); run(); return; }
    var was = b.innerHTML;
    b.dataset.confirm = '1';
    b.innerHTML = html;
    b.dataset.timer = setTimeout(function () { delete b.dataset.confirm; b.innerHTML = was; }, 5000);
  }
  async function sqPost(row, b) {
    b.disabled = true;
    relabel(b, ICON_SEND, 'Sending…');
    try {
      await api('POST', sqPath(row), row.kind === 'report' ? undefined : {key: row.x.key});
      relabel(b, ICON_SEND, 'Sent');
      await sqRefresh();
    } catch (e) {
      relabel(b, ICON_SEND, e && e.code === 502 ? 'Slack refused it, see History' : e && e.code === 503 ? 'Slack is not set up' : 'Not sent, try again');
      b.disabled = false;
    }
  }
  function sqJump(row) {
    if (row.kind === 'report') reports.show(row.x.id);
    else showLinked(encodeURIComponent(row.x.key));
  }
  function wireSlackQ(root) {
    if (!root) return;
    root.querySelectorAll('.sq-acts').forEach(function (box) {
      if (box.dataset.wired) return;
      box.dataset.wired = '1';
      var row = box.__row, name = row.kind === 'report' ? 'this deploy report' : svcName(row.x) + ' ' + row.x.label;
      var send = iconBtn(ICON_SEND, 'Send now', 'sec small op-btn slack-btn');
      send.dataset.tip = send.title = 'Post this text to ' + (me.slackChannel || 'the Slack channel') + ' now, as the message for ' + name + '; click twice to confirm';
      send.addEventListener('click', function () { sqArm(send, ICON_SEND + esc('Post to ' + (me.slackChannel || 'Slack') + '?'), function () { sqPost(row, send); }); });
      box.append(send);
      var go = btn('View', 'sec small free');
      go.title = row.kind === 'report' ? 'Open this deploy report' : 'Open this anomaly card';
      go.addEventListener('click', function () { sqJump(row); });
      box.append(go);
    });
    gate();
  }
  var ICON_CHECK ='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>';
  function wirePdc() {
    var box = document.getElementById('pdc');
    if (!box || box.dataset.wired) return;
    box.dataset.wired = '1';
    var b = btn('', 'sec small op-btn');
    b.id = 'pdc-btn';
    b.innerHTML = ICON_CHECK + 'Post-deploy check';
    b.style.display = 'inline-flex';
    b.style.alignItems = 'center';
    b.style.gap = '6px';
    b.addEventListener('click', function () { queue({type: 'postdeploy', key: 'postdeploy', target: 'postdeploy', label: 'Post-deploy check'}, b); });
    box.append(el('span', 'pdc-st'), b);
  }
  function renderPdc(x) {
    var b = document.getElementById('pdc-btn');
    var st = document.querySelector('#pdc .pdc-st');
    if (!b || !st) return;
    var busy = !!(x && (x.status === 'requested' || x.status === 'processing'));
    b.disabled = busy;
    st.innerHTML = '';
    st.title = '';
    if (!x) { st.textContent = 'Checks every image change of the last 6 h'; return; }
    if (x.status === 'requested') { st.textContent = 'Queued · the server picks it up within about two minutes'; return; }
    if (x.status === 'processing') { st.textContent = 'Running…'; return; }
    var more = el('button', null, 'report');
    more.type = 'button';
    more.addEventListener('click', function () { if (x.reportId) reports.show(x.reportId); else showTab('acts'); });
    if (x.reportId && !document.getElementById('rep-' + x.reportId)) reports.refresh();
    st.innerHTML = esc(dayhm(x[x.status + 'At'] || x.requestedAt)) + ' · ' + esc(x.status === 'done' ? (x.result || 'done') : 'failed: ' + (x.result || '')) + ' · ';
    st.appendChild(more);
    st.title = x.result || '';
  }
  var openActs = {};
  function actRow(x) {
    var li = el('li', 'hrow'), d = el('details');
    if (openActs[x.id]) d.open = true;
    var sum = el('summary', 'chev', '<span class="q-l">' + esc(label(x)) + '</span>' + pill(x) + '<span class="muted">' + esc(dayhm(x.requestedAt)) + ' · ' + esc(draftBy(x.requestedBy)) + '</span>');
    if (x.status === 'requested' && me.operator) sum.appendChild(cancelBtn(x));
    var note = x.status === 'requested' ? 'Waiting to be picked up.' : x.status === 'processing' ? 'Working on it…' : 'No result text.';
    d.append(sum, el('div', 'hbody', (x.result ? '<p>' + linkify(x.result) + '</p>' : '<p class="muted">' + note + '</p>') + (x.report ? '<pre>' + esc(x.report) + '</pre>' : '')));
    d.addEventListener('toggle', function () { if (d.open) openActs[x.id] = 1; else delete openActs[x.id]; });
    li.appendChild(d);
    return li;
  }
  function wire() {
    wirePdc();
    document.querySelectorAll('#anoms .acts').forEach(function (box) {
      if (box.dataset.wired) {
        box.hidden = false;
        var nb = box.closest('article.inc').querySelector('.notif');
        if (nb) nb.hidden = false;
        return;
      }
      box.dataset.wired = '1';
      var a = cardOfBox(box);
      if (!a) return;
      var name = svcName(a) + ' ' + a.label;
      var prior = window.INV && INV[a.key];
      var dive = asIcon(btn('Investigate', 'inv-btn'), ICON_SPARKLES, prior ? 'Investigate · last run ' + dayhm(prior.startedAt) + ' (' + prior.mode + ') · ' + usdText(prior.usd) + ' · asks before running again' : 'Investigate: the investigator takes it within about two minutes');
      dive.addEventListener('click', function () {
        var go = function () { queue({type: 'dive', target: a.key, key: a.key, label: 'Investigate ' + name}, dive); };
        if (!prior) { go(); return; }
        confirmDialog('Investigate again?', name + ' was investigated ' + dayhm(prior.startedAt) + ' (' + prior.mode + ', ' + usdText(prior.usd) + '). A new run costs about the same and starts from the earlier findings.', 'Investigate again').then(function (ok) { if (ok) go(); });
      });
      box.append(dive);
      assignControls(a, box, name);
      jiraControls(a, box, name);
      sevControls(a, box, name);
      if (a.muted) {
        var un = asIcon(btn('Unmute', 'sec'), ICON_BELL, 'Unmute');
        un.addEventListener('click', function () { queue({type: 'unmute', target: a.key, key: a.key, label: 'Unmute ' + name}, un); });
        box.append(un);
      } else if (a.status === 'open' || stale.indexOf(a) >= 0) {
        var mute = asIcon(btn('Mute 24 h', 'sec'), ICON_BELL_OFF, 'Mute for 24 hours');
        mute.addEventListener('click', function () { queue({type: 'mute', target: a.key, key: a.key, hours: 24, label: 'Mute ' + name + ' for 24 h'}, mute); });
        var week = asIcon(btn('Mute 1 w', 'sec'), ICON_BELL_OFF, 'Mute for 7 days');
        week.addEventListener('click', function () { queue({type: 'mute', target: a.key, key: a.key, hours: 168, label: 'Mute ' + name + ' for 1 week'}, week); });
        var known = asIcon(btn('Known noise', 'sec'), ICON_EYE_OFF, 'Known noise: mute for good, with a reason');
        var form = el('div', 'reason');
        form.hidden = true;
        var inp = el('input');
        inp.type = 'text'; inp.maxLength = 200; inp.id = 'reason-' + box.dataset.i; inp.placeholder = 'Why this is expected (optional)';
        inp.setAttribute('aria-label', 'Reason for ' + name);
        var ok = btn('Mark as known noise', 'small');
        form.append(inp, ok);
        known.addEventListener('click', function () { form.hidden = !form.hidden; if (!form.hidden) inp.focus(); });
        ok.addEventListener('click', function () { queue({type: 'known', target: a.key, key: a.key, reason: inp.value.trim() || 'marked as known noise on the page', label: 'Known noise: ' + name}, ok); });
        box.append(mute, week, known, form);
      }
      if (a.triage !== 'handled') {
        var inProgress = a.triage === 'progress';
        var takeTip = inProgress ? (me.jira ? 'Start progress: change the ticket or make yourself the owner; it stays In progress' : 'Start progress: make yourself the owner; it stays In progress')
          : 'Start progress: the card goes In progress with you as owner' + (me.jira ? ', ticket optional' : '');
        var take = asIcon(btn('Start progress', 'sec'), ICON_TAKE, takeTip);
        var tform = el('div', 'reason');
        tform.hidden = true;
        var tin = el('input');
        tin.type = 'text'; tin.maxLength = 100; tin.id = 'ticket-' + box.dataset.i; tin.placeholder = 'Jira key or task name to search (optional)';
        if (a.ticket) tin.value = a.ticket;
        tin.setAttribute('aria-label', 'Ticket for ' + name);
        var twhy = el('input');
        twhy.type = 'text'; twhy.maxLength = 300; twhy.id = 'take-why-' + box.dataset.i; twhy.placeholder = 'Note (optional, becomes the card note)';
        twhy.setAttribute('aria-label', 'Note for starting progress on ' + name);
        var tok = btn('Start progress', 'small');
        if (me.jira) tform.append(tin);
        tform.append(twhy, tok);
        if (typeof ticketSuggest === 'function' && me.jira) ticketSuggest(tin);
        take.addEventListener('click', function () { tform.hidden = !tform.hidden; if (!tform.hidden) tin.focus(); });
        tok.addEventListener('click', function () {
          var t = me.jira ? tin.value.trim() : '', w = twhy.value.trim();
          queue({type: 'take', target: a.key, key: a.key, ticket: t || undefined, reason: w || undefined, label: 'Take ' + name + (t ? ' with ' + t : '')}, tok);
        });
        box.append(take, tform);
      }
      if (a.status === 'open' || a.status === 'cleared') {
        var others = A.filter(function (o) { return o !== a && o.triage !== 'handled' && (o.status === 'open' || (a.status === 'cleared' && stale.indexOf(o) >= 0)); });
        if (others.length) {
          var grp = asIcon(btn('Group', 'sec'), ICON_GROUP, a.status === 'open' ? 'Group: merge this card under another open card, the one that explains it' : 'Group: merge this stopped card under an open card or another stopped one, the one that explains it');
          var gform = el('div', 'reason');
          gform.hidden = true;
          var gok = btn('Group', 'small');
          gok.disabled = true;
          gok.dataset.gpWait = '1';
          var gp = groupPicker(a, others, box.dataset.i, name, function (o) { if (o) delete gok.dataset.gpWait; else gok.dataset.gpWait = '1'; gok.disabled = !o || cardBusy(a.key); });
          gform.append(gp.box, gok);
          gp.box.onleave = function () { gform.hidden = true; grp.focus(); };
          grp.addEventListener('click', function () { gform.hidden = !gform.hidden; if (!gform.hidden) gp.input.focus(); });
          gok.addEventListener('click', function () {
            var o = gp.current();
            if (!o) { gp.input.focus(); return; }
            queue({type: 'group', target: a.key, key: a.key, parent: o.key, label: 'Group ' + name + ' under ' + gpName(o) + ' ' + o.label}, gok);
          });
          box.append(grp, gform);
        }
      }
      if (a.triage !== 'handled') {
        var res = asIcon(btn('Resolve', 'sec'), ICON_DONE, 'Resolve: fixed, no action needed or a duplicate of another card');
        var rform = el('div', 'reason res-form');
        rform.hidden = true;
        var why = el('input');
        why.type = 'text'; why.maxLength = 300; why.id = 'why-' + box.dataset.i; why.placeholder = RES_PLACEHOLDER_WHY; why.title = RES_TITLE_WHY;
        why.setAttribute('aria-label', 'Why ' + name + ' is resolved');
        var dup = el('input');
        dup.type = 'text'; dup.maxLength = 200; dup.id = 'dup-' + box.dataset.i; dup.placeholder = RES_PLACEHOLDER_DUP; dup.title = RES_TITLE_DUP; dup.hidden = true;
        dup.setAttribute('aria-label', 'The card ' + name + ' duplicates');
        var picked = 'fixed';
        var kinds = resKindPicker(picked, function (k) {
          picked = k;
          why.hidden = k === 'duplicate';
          dup.hidden = k !== 'duplicate';
          dup.removeAttribute('aria-invalid');
          (k === 'duplicate' ? dup : why).focus();
        });
        var rok = btn('Resolve', 'small');
        rform.append(kinds, why, dup, rok);
        res.addEventListener('click', function () { rform.hidden = !rform.hidden; if (!rform.hidden) (picked === 'duplicate' ? dup : why).focus(); });
        rok.addEventListener('click', function () {
          var ref = dup.value.trim();
          if (picked === 'duplicate' && !ref) { dup.setAttribute('aria-invalid', 'true'); dup.focus(); return; }
          queue(Object.assign({target: a.key, key: a.key}, resolveBody(picked, why.value.trim(), ref, name)), rok);
        });
        box.append(res, rform);
      }
      var copy = asIcon(btn('Copy link', 'sec free'), ICON_LINK, a.num ? 'Copy the direct link to card #' + a.num + ': it opens this card alone' : 'Copy a link that opens this anomaly');
      copy.addEventListener('click', function () { copyLink(a, copy); });
      box.append(copy, el('span', 'stat'));
      foldActs(a, box, dive);
      if (box.closest('.card-drawer')) groupActs(a, box, {dive: dive, take: take, tform: tform, res: res, rform: rform, mute: mute, un: un, copy: copy});
      var nbox = box.closest('article.inc').querySelector('.notif');
      nbox.innerHTML = '';
      nbox.append(el('span', 'nl', 'Notify'));
      var slack = asIcon(btn(slackLabel(a), 'sec slack-btn'), ICON_SEND, null, true);
      slackButton(a, slack);
      slack.addEventListener('click', function () { slackClick(a, slack); });
      nbox.append(slack);
      teamsButtons(a).forEach(function (tb) { nbox.append(tb); });
      if (a.slackPost && a.slackPost.ts) {
        var upd = asIcon(btn('Post update', 'sec slack-btn slack-upd'), ICON_REPLY, 'Reply in the Slack thread of the message posted ' + dayhm(a.slackPost.at) + ', e.g. that it is fixed or has a ticket');
        var uform = el('div', 'reason');
        uform.hidden = true;
        var uta = el('textarea');
        uta.id = 'update-' + box.dataset.i; uta.maxLength = 1000; uta.rows = 2; uta.value = updateText(a);
        uta.setAttribute('aria-label', 'Slack thread update for ' + name);
        var upost = btn('Post to thread', 'slack-btn');
        uform.append(uta, upost);
        upd.addEventListener('click', function () { uform.hidden = !uform.hidden; if (!uform.hidden) uta.focus(); });
        upost.addEventListener('click', function () { postUpdate(a, uta, upost, uform); });
        nbox.append(upd, uform);
      }
      nbox.append(el('span', 'slack-st'));
      nbox.hidden = false;
      var dbox = box.closest('article.inc').querySelector('.sdraft');
      if (dbox) wireDraft(a, dbox);
      var bbox = box.closest('article.inc').querySelector('.biz');
      if (bbox) wireBiz(a, bbox);
      box.hidden = false;
    });
    gate();
  }
  var LATEST = {};
  function cardBusy(key) { var x = LATEST[key]; return !!(x && (x.status === 'requested' || x.status === 'processing')); }
  window.__cardBusy = cardBusy;
  function render() {
    wire();
    askStatus(all);
    var latest = {};
    all.forEach(function (x) { if (!latest[x.target] || latest[x.target].requestedAt < x.requestedAt) latest[x.target] = x; });
    LATEST = latest;
    document.querySelectorAll('#anoms .tile').forEach(function (t) {
      var busy = window.__tileBusy(t.dataset.key);
      t.querySelectorAll('.tile-acts .ta-b[data-move]').forEach(function (b) { if (!b.hasAttribute('aria-busy')) b.disabled = busy; });
      t.draggable = window.__tileDraggable(t.dataset.key);
    });
    renderPdc(latest.postdeploy);
    document.querySelectorAll('.rep-acts .reassess-btn').forEach(function (b) { reassessState(b, b.closest('.rep-acts').__rep); });
    document.querySelectorAll('#anoms .acts').forEach(function (box) {
      var a = cardOfBox(box);
      if (!a) return;
      var x = latest[a.key];
      box.querySelectorAll('button.btn:not(.free):not(.slack-btn)').forEach(function (b) { b.disabled = !!(x && (x.status === 'requested' || x.status === 'processing')) || b.dataset.gpWait === '1'; });
      var st = box.querySelector('.stat'), la = box.closest('article.inc').querySelector('.last-act');
      if (st) {
        st.innerHTML = x && !earlier(x, a) ? pill(x, true) : '';
        if (x && x.status === 'requested' && me.operator) st.appendChild(cancelBtn(x));
      }
      if (la) {
        la.hidden = !x;
        la.innerHTML = x ? lastLine(x, a) : '';
      }
      var sx = latest['slack:' + a.key], ux = latest['slack-update:' + a.key], ss = box.closest('article.inc').querySelector('.notif .slack-st'), sp = a.slackPost || {}, sn = sp.stateNote;
      if (ss) ss.innerHTML = (sn ? slackLink(sn.permalink, 'Status note ' + dayhm(sn.at), sn.text) + ' · ' : '') +
        (!ux ? '' : ux.status === 'done' ? slackLink((ux.slack || {}).permalink, 'Thread update ' + dayhm(ux.doneAt || ux.requestedAt)) + ' · ' : '<span class="st s-failed">Thread update failed</span> <span class="muted">' + esc(ux.result || '') + '</span> · ') + (!sx ? '' : sx.status === 'done'
        ? slackLink((sx.slack || {}).permalink || sp.permalink, 'Sent to Slack ' + dayhm(sx.doneAt || sx.requestedAt)) + ' · ' + esc(draftBy(sx.requestedBy, (a.slackDraft || {}).by)) + (sx.draft ? ' · draft' : '')
        : '<span class="st s-failed">Slack failed</span> ' + esc(dayhm(sx.failedAt || sx.requestedAt)) + ' <span class="muted">' + esc(sx.result || '') + '</span>');
    });
    var pending = all.filter(function (x) { return x.status === 'requested'; });
    var working = all.filter(function (x) { return x.status === 'processing'; });
    document.getElementById('count-acts').dataset.base = pending.length + working.length ? (pending.length + working.length) + ' open' : String(all.length);
    var actTab = document.getElementById('tab-acts');
    actTab.className = 'tab k-act' + (pending.length + working.length ? ' hot' : '');
    actTab.dataset.base = pending.length + working.length ? pending.length + ' queued, ' + working.length + ' in progress' : 'nothing queued';
    if (typeof actsHeldMark === 'function') actsHeldMark();
    strip.hidden = !(pending.length || working.length);
    strip.innerHTML = '';
    strip.appendChild(el('div', 'queue-head', '<span class="t"><b>' + pending.length + ' queued</b>' + (working.length ? ', ' + working.length + ' in progress' : '') + '</span><span class="muted">the investigator and the server jobs pick these up within about two minutes</span>'));
    var ul = el('ul', 'q-rows');
    pending.concat(working).forEach(function (x) {
      var li = el('li', null, '<span class="q-l">' + esc(label(x)) + '</span> <span class="q-src muted">' + esc(String(x.requestedBy || '?').replace(/@.*$/, '')) + ' · ' + esc(sourceLabel(x)) + '</span> ' + chip(x));
      if (x.status === 'requested' && me.operator) li.appendChild(cancelBtn(x));
      ul.appendChild(li);
    });
    if (pending.length || working.length) strip.appendChild(ul);
    var list = document.getElementById('q-list');
    var an = document.getElementById('hist-acts-n');
    if (an) an.textContent = all.length ? String(all.length) : '';
    list.innerHTML = '';
    all.slice().sort(function (x, y) { return x.requestedAt < y.requestedAt ? 1 : -1; }).slice(0, 40).forEach(function (x) { list.appendChild(actRow(x)); });
    if (!all.length) list.appendChild(el('li', 'muted', 'No actions yet. Use Investigate, Mute 24 h or Known noise on an anomaly.'));
  }
  async function refresh() {
    try { all = await api('GET', '/api/actions?limit=200'); render(); }
    catch (e) { if (!e || e.code !== 401) hint.textContent = 'The action list is unavailable right now.'; }
  }
  try {
    var who = await fetch('/api/me', {headers: {'Accept': 'application/json'}, credentials: 'same-origin'});
    if (who.ok) me = Object.assign(me, await who.json());
  } catch (e) {}
  ME_USER = me.user || '';
  var autoBefore = ME_FEATURES['slack-autosend'];
  ME_FEATURES = me.features && typeof me.features === 'object' ? me.features : {};
  if (autoBefore !== ME_FEATURES['slack-autosend']) { bumpRender(); renderAnoms(); }
  ME_OFF = me.featureOff && typeof me.featureOff === 'object' ? me.featureOff : {};
  ME_CHAT = !!me.chat;
  var gitlabBefore = ME_GITLAB, hostBefore = ME_CODE_HOST;
  ME_GITLAB = !!me.gitlab;
  ME_CODE_HOST = typeof me.codeHost === 'string' ? me.codeHost : '';
  if ((ME_GITLAB !== gitlabBefore || ME_CODE_HOST !== hostBefore) && reports) reports.refresh();
  var channelBefore = ME_CHANNEL;
  ME_CHANNEL = me.slackChannel || '';
  if (ME_CHANNEL !== channelBefore && typeof loadRequests === 'function') loadRequests();
  if (me.task) setTicketProjects(me.task.projects);
  if (useTagLabels(me.tags)) { if (fParts(typeF).some(isTagF)) setType(typeF); else renderAnoms(); }
  if (me.jiraUrl) { setJiraBase(me.jiraUrl); renderAnoms(); }
  if (me.chatMax) {
    ASK_MAX = me.chatMax;
    var askBox = document.getElementById('ask-text');
    if (askBox) askBox.maxLength = ASK_MAX;
    document.querySelectorAll('.card-ask input').forEach(function (i) { i.maxLength = ASK_MAX; });
  }
  if (ME_USER) {
    renderAnomSub();
    if (hasF('who:me')) setFilter(filter); else renderQuick();
  }
  repActs = wireReps;
  slackActs = wireSlackQ;
  wire();
  wireReps(document.getElementById('reps'));
  wireSlackQ(document.querySelector('#slackq tbody'));
  if (typeof instRender === 'function') instRender(me);
  account();
  if (typeof sideChat === 'function') sideChat(me);
  if (typeof renderInvMenu === 'function') renderInvMenu();
  gate();
  setCostsAccess(!!me.user);
  if (typeof ntSignedIn === 'function') ntSignedIn(!!me.user);
  if (typeof setAdminAccess === 'function') setAdminAccess(me.admin);
  if (typeof setIntView === 'function') setIntView(!!me.user, !!me.admin);
  window.__skopaxRender = render;
  window.__skopaxActions = refresh;
  await refresh();
  setInterval(refresh, 30000);
}
