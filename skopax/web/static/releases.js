function initReports() {
  var relBox = document.getElementById('rels');
  var repBox = document.getElementById('reps');
  var opened = {}, relOpen = {}, relOf = {}, relsNow = [], repsNow = [], depBy = {};
  var days = 365, MAX_DAYS = 3650, shown = -1, widened = false, oldest = false;
  var GITLAB_READ_MS = 7 * 86400000;
  var repLimit = 100, MAX_REPORTS = 200, want = null, loaded = false, seq = 0;
  var REP_LV = {ok: ['All clear', 'k-ok'], info: ['Info', 'k-info'], warn: ['Warning', 'k-warn'], crit: ['Critical', 'k-crit']};
  var REP_BY = 'Written when this post-deploy check was saved. Send to Slack uses it under the message.';
  var REP_BY_OLD = 'This report was saved before report summaries existed, so this is its one-line summary.';
  var KEYS_SHOWN = 6, SHA = /^[0-9a-f]{7,40}$/;
  var ICON_GITLAB = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M23.955 13.587l-1.342-4.135-2.664-8.189a.455.455 0 0 0-.867 0L16.418 9.45H7.582L4.919 1.263a.455.455 0 0 0-.867 0L1.388 9.452.046 13.587a.924.924 0 0 0 .331 1.023L12 23.054l11.623-8.443a.92.92 0 0 0 .332-1.024"/></svg>';
  var RELEASE_GAP = 2 * 3600000, SETTLE = 20 * 60000, ONGOING = 3 * 3600000, RECENT = 2 * 3600000, flipAt = 0;
  function who(r) { return isTokenActor(r.requestedBy) ? tokenActorLabel(r.requestedBy, 'Investigator') : r.requestedBy ? r.requestedBy.split('@')[0] : 'Investigator'; }
  function issueLink(k) { return jiraHref(k) ? '<a href="' + jiraHref(k) + '" target="_blank" rel="noopener">' + esc(k) + '</a>' : '<span class="ikey">' + esc(k) + '</span>'; }
  function keyList(keys) {
    var rest = keys.slice(KEYS_SHOWN);
    return '<div class="ikeys">' + keys.slice(0, KEYS_SHOWN).map(issueLink).join(' ') + (rest.length ? ' ' + rest.map(function (k) { return issueLink(k).replace(/^<(a|span) /, '<$1 hidden '); }).join(' ') +
      '<button type="button" class="more" title="' + esc(rest.join(', ')) + '">+' + rest.length + ' more</button>' : '') + '</div>';
  }
  function rowIssues(x) {
    if (x.issuesNote) return '<div class="ikeys muted">tickets: ' + esc(x.issuesNote) + '</div>';
    var k = x.issues || [];
    return k.length ? keyList(k) : '';
  }
  function compareUrl(from, to, by) {
    if (by && by.compare && String(by.compare).startsWith('https://')) return by.compare;
    var m = by && by.url && /^(https:\/\/\S+?)\/-\/commit\/[0-9a-f]+$/.exec(by.url);
    return m && SHA.test(from || '') && SHA.test(to || '') ? m[1] + '/-/compare/' + from + '...' + to : '';
  }
  function imgCell(from, to, by) {
    var pair = esc((from || '?') + ' → ' + (to || '?')), u = compareUrl(from, to, by);
    if (u) {
      var tip = 'Compare ' + from + ' → ' + to + ' on GitLab' + (by.commits ? ' · ' + by.commits + (by.commits === 1 ? ' commit' : ' commits') + ' since ' + from : '') + (by.title ? ' · ' + by.title : '');
      return '<a class="cmp" href="' + esc(u) + '" target="_blank" rel="noopener" title="' + esc(tip) + '">' + ICON_GITLAB + '<span>' + pair + '</span></a>';
    }
    if (by && by.url && String(by.url).startsWith('https://')) return esc((from || '?') + ' → ') + '<a href="' + esc(by.url) + '" target="_blank" rel="noopener" title="' + esc(by.title || '') + '">' + esc(to || '?') + '</a>';
    return pair;
  }
  function issuesBox(r, iss) {
    var svcOf = {};
    (r.rows || []).forEach(function (x) {
      (x.issues || []).forEach(function (k) {
        svcOf[k] = svcOf[k] || [];
        svcOf[k].push(x.svc);
      });
    });
    var groups = [
      {t: 'Shipped', items: []},
      {t: 'Shipped, not in this release', tail: 'not in release version', note: 'The code is in a deployed image, but the ticket has no release version; check its Jira status.', items: []},
      {t: 'In this release, no deploy seen', tail: 'no deploy seen', note: 'No checked image range holds it: the service may have no local clone, or not be deployed yet.', items: []}
    ];
    iss.forEach(function (i) { groups[!svcOf[i.key] ? 2 : i.inRelease === false ? 1 : 0].items.push(i); });
    var tail = groups.filter(function (g) { return g.tail && g.items.length; }).map(function (g) { return g.items.length + ' ' + g.tail; });
    var d = el('details', 'rep');
    var count = !iss.length && r.issuesNote ? 'not read' : String(iss.length);
    d.innerHTML = '<summary class="chev">Released issues \u00b7 ' + count + (tail.length ? ' \u00b7 ' + esc(tail.join(' \u00b7 ')) : '') + '</summary>';
    if ((r.releases || []).length) d.appendChild(el('p', 'muted', 'Jira release versions: ' + esc(r.releases.join(', ')) + '. Tickets come from the commit titles in each deployed image range.'));
    if (r.issuesNote) d.appendChild(el('p', 'muted', esc(r.issuesNote)));
    groups.forEach(function (g) {
      if (!g.items.length) return;
      var tb = g.items.map(function (i) {
        var st = i.status ? '<span class="st ' + (i.status === 'Live' ? 's-done' : 's-other') + '">' + esc(i.status) + '</span>' : '<span class="muted">?</span>';
        return '<tr><td class="key">' + issueLink(i.key) + '</td><td>' + esc(i.summary || '') + '</td><td>' + st + '</td><td class="muted">' + esc((svcOf[i.key] || []).join(', ')) + '</td></tr>';
      }).join('');
      d.appendChild(el('div', 'igrp', '<h4>' + esc(g.t) + ' · ' + g.items.length + '</h4>' + (g.note ? '<p class="muted">' + esc(g.note) + '</p>' : '') +
        '<div class="box"><table><thead><tr><th>Ticket</th><th>Summary</th><th>Status</th><th>Services</th></tr></thead><tbody>' + tb + '</tbody></table></div>'));
    });
    return d;
  }
  var FLAG_SIG = {unavail: 'unavailable'};
  function flagSig(f) { var s = String(f).split(/[\s=]/)[0]; return FLAG_SIG[s] || s; }
  function flagCard(x, r, sig) {
    var at = Date.parse(x.at || r.windowFrom || r.createdAt) || 0;
    var end = Math.max(Date.parse(r.windowTo || r.createdAt) || 0, at) + 3 * 3600000;
    var key = sig + '|' + x.ns + '/' + x.svc, log = sig === 'err' ? 'log|' + x.svc + '|ERROR|' : sig === 'warn' ? 'log|' + x.svc + '|WARN|' : null, tail = x.cluster ? '@' + x.cluster : '';
    var near = ALL.filter(function (a) { return Date.parse(a.lastSeen || a.firstSeen || '') >= at - 3600000 && Date.parse(a.firstSeen || '') <= end; });
    var own = near.filter(function (a) { return (a.cluster || '') === (x.cluster || ''); });
    return own.filter(function (a) { return a.key === key + tail || a.key.indexOf(key + '|') === 0; })[0] || (log && near.filter(function (a) { return a.key.indexOf(log) === 0; })[0]) || null;
  }
  function sigLink(x, sig) { var u = (x.links || {})[sig]; return u && String(u).startsWith('https://') ? u : ''; }
  function sigTip(x, sig, u) {
    var expr = '';
    try { expr = new URL(u).searchParams.get('g0.expr') || ''; } catch (e) {}
    return expr ? sig + ' in Prometheus over the deploy window, the query the check ran: ' + expr : sig + ' over the deploy window';
  }
  function logsLine(x) {
    var u = sigLink(x, 'logs');
    return u ? ['<span><a class="row-logs" href="' + esc(u) + '" target="_blank" rel="noopener" title="' + esc('Logs of ' + x.svc + ' over the deploy window') + '">Logs</a></span>'] : [];
  }
  function sigCell(x, r) {
    return cellLines(splitTop(x.metrics, COMMA).map(function (part) {
      var sig = part.split(/[\s=]/)[0], u = sigLink(x, sig);
      return '<span>' + (u ? '<a href="' + esc(u) + '" target="_blank" rel="noopener" title="' + esc(sigTip(x, sig, u)) + '">' + esc(part) + '</a>' : esc(part)) + '</span>';
    }).concat(logsLine(x)));
  }
  function flagPill(x, r, f) {
    var sig = flagSig(f), c = flagCard(x, r, sig);
    if (c) return '<button type="button" class="st s-warn flag-link" data-key="' + esc(c.key) + '" title="' + esc('Open its anomaly card: ' + c.svc + ' / ' + c.label + (c.status === 'open' ? '' : ' (' + c.status + ')')) + '">' + esc(f) + '</button>';
    var raw = String(f).split(/[\s=]/)[0], u = sigLink(x, raw);
    if (!u) return '<span class="st s-warn">' + esc(f) + '</span>';
    return '<a class="st s-warn flag-link" href="' + esc(u) + '" target="_blank" rel="noopener" title="' + esc(sigTip(x, raw, u)) + '">' + esc(f) + '</a>';
  }
  function flags(row, r) {
    var f = row.flags || [];
    if (!f.length) return '<span class="st s-done">clean</span>';
    return cellLines(f.map(function (x) { return flagPill(row, r, x); }));
  }
  function jump(text, go) {
    var b = el('button', 'xlink', esc(text));
    b.type = 'button';
    b.addEventListener('click', function (e) { e.preventDefault(); e.stopPropagation(); go(); });
    return b;
  }
  function flagged(r) { return (r.rows || []).filter(function (x) { return (x.flags || []).length; }).length; }
  function pageRows(r, state) { return ((r.pages || {}).rows || []).filter(function (x) { return x.state === state; }); }
  function repTitle(r) {
    var g = relOf[r.id], hot = flagged(r), pg = pageRows(r, 'flagged').length;
    var win = r.windowFrom ? hm(r.windowFrom) + '–' + hm(r.windowTo || r.createdAt) : '';
    return [g ? g.name : 'Deploy report', win, hot ? hot + ' flagged' : 'clean', pg ? pg + (pg === 1 ? ' page' : ' pages') + ' flagged' : ''].filter(Boolean).join(' · ');
  }
  var PAGE_ST = {ok: ['ok', 's-done'], flagged: ['flagged', 's-warn'], stale: ['not checked', 's-other'], skipped: ['not loaded', 's-other']};
  var PAGES_WAIT = 5;
  function pageCache(x) {
    return [x.cache ? 'cf ' + x.cache : 'no cf-cache-status', x.age != null ? 'age ' + x.age + ' s' : 'no age header', x.varnish ? 'varnish ' + x.varnish : ''].filter(Boolean).join(' · ');
  }
  function pagePath(u) { try { var x = new URL(u); return x.pathname + x.search; } catch (e) { return u; } }
  function pagesBox(r) {
    var p = r.pages, d = el('details', 'rep pages-box');
    if (p.state === 'waiting' || p.state === 'checking') {
      d.innerHTML = '<summary class="chev">Pages · ' + esc(p.state === 'checking' ? 'checking now' : 'waiting until ' + hm(p.readyAt)) + '</summary>';
      d.appendChild(el('p', 'muted', esc('The server loads the key public pages ' + PAGES_WAIT + ' min after the rollout at ' + hm(p.rolloutAt) + ', so Varnish serves the new pages; the assessment starts after that.')));
      return d;
    }
    var rows = p.rows || [], hot = pageRows(r, 'flagged').length, old = pageRows(r, 'stale').length, ok = pageRows(r, 'ok').length;
    if (p.error || !rows.length) {
      d.innerHTML = '<summary class="chev">Pages · check failed</summary>';
      d.appendChild(el('p', 'muted', esc(p.error || 'no page was checked')));
      return d;
    }
    d.innerHTML = '<summary class="chev">Pages · ' + esc([ok + ' ok', hot ? hot + ' flagged' : '', old ? old + ' cached from before the deploy' : ''].filter(Boolean).join(' · ')) + '</summary>';
    d.appendChild(el('p', 'muted', esc('Loaded at ' + hm(p.at) + ' as a visitor without cookies, at least ' + PAGES_WAIT + ' min after the rollout at ' + hm(p.rolloutAt) +
      '. A copy older than the rollout (its Age header) is not checked: it says nothing about the release.')));
    var tb = rows.map(function (x) {
      var st = PAGE_ST[x.state] || PAGE_ST.skipped, code = x.status != null ? String(x.status) : '–';
      var note = (x.flags || []).length ? cellLines(x.flags.map(function (f) { return '<span class="st s-warn">' + esc(f) + '</span>'; })) : '<span class="st ' + st[1] + '">' + esc(st[0]) + '</span>';
      var tail = [x.final ? 'ended at ' + pagePath(x.final) : '', x.state === 'stale' ? 'cached from before the deploy' : x.note || ''].filter(Boolean).join(' · ');
      return '<tr class="pg-' + esc(x.state || 'skipped') + '"><td class="pg-url"><a href="' + esc(x.url) + '" target="_blank" rel="noopener">' + esc(pagePath(x.url)) + '</a></td>' +
        '<td class="pg-code">' + esc(code) + '</td><td class="pg-ms">' + esc(x.ms ? (x.ms / 1000).toFixed(1) + ' s' : '') + '</td>' +
        '<td class="pg-cache">' + esc(x.cache !== undefined || x.age != null ? pageCache(x) : '') + '</td><td>' + esc(x.title || '') + '</td>' +
        '<td class="flags">' + note + (tail ? '<div class="muted pg-note">' + esc(tail) + '</div>' : '') + '</td></tr>';
    }).join('');
    d.appendChild(el('div', 'box', '<table><thead><tr><th>Page</th><th>Status</th><th>Time</th><th>Cache</th><th>Title</th><th>Flags</th></tr></thead><tbody>' + tb + '</tbody></table>'));
    return d;
  }
  function repShots(r) {
    var seen = {}, out = [];
    ((r.brief || {}).screenshots || []).concat((r.slackDraft || {}).screenshots || []).forEach(function (s) { if (s && s.id && !seen[s.id]) { seen[s.id] = 1; out.push(s); } });
    return out;
  }
  function linkBtn(hash, tip) {
    var b = el('button', 'btn sec small with-icon free copy-link', ICON_LINK + 'Copy link');
    b.type = 'button';
    b.title = tip;
    b.addEventListener('click', function (e) { e.preventDefault(); e.stopPropagation(); copyUrl(location.origin + location.pathname + '#' + hash, b); });
    return b;
  }
  function briefBox(r) {
    var has = !!(r.brief && r.brief.headline), b = has ? r.brief : {level: flagged(r) ? 'warn' : 'ok', headline: r.summary, points: []};
    var L = REP_LV[b.level] || REP_LV.info;
    return el('div', 'brief rep-brief', '<span class="brief-by">Report summary<span class="q" tabindex="0" role="img" aria-label="About the report summary" title="' + esc(has ? REP_BY : REP_BY_OLD) + '">i</span></span><h2><span class="sev ' + L[1] + '">' + esc(L[0]) + '</span><span class="hl">' +
      splitTop(b.headline, SEMI).map(function (x) { return '<span>' + esc(x) + '</span>'; }).join('') + '</span></h2>' +
      ((b.points || []).length ? '<ul>' + b.points.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>' : '') +
      shotsHtml({screenshots: repShots(r), startedAt: (r.brief || {}).at || r.createdAt}));
  }
  function slackTag(r) {
    var d = draftOf(r), p = r.slackPost, t, why = notPosted(r, false);
    if (d && !draftSent(r) && why) {
      t = el('span', 'tag k-warn slack-rec not-posted', ICON_SEND + esc(why));
      t.title = d.text + '\n\n' + draftBy(d.by) + ' \u00b7 ' + dayhm(d.at) + statusTip(r, false);
    } else if (r.slackSkipped && !p) {
      t = el('span', 'tag k-muted slack-rec not-posted', ICON_SEND + esc(skippedText(r)));
      t.title = skippedTip(r);
    } else if (p) {
      t = el('span', 'tag k-good slack-rec sent', ICON_SEND + slackLink(p.permalink, 'Sent to Slack ' + dayhm(p.at)));
      t.title = 'Posted to Slack ' + dayhm(p.at) + ' by ' + draftBy(p.by) + (p.draft && d ? '\n\n' + d.text : '');
    }
    return t || null;
  }
  function committers(x) {
    var d = depBy[x.svc + '|' + x.toImage], by = d && d.imageBy;
    if (!by || !(by.names || []).length) return '';
    var tip = 'Commits by ' + by.names.join(', ') + (by.commits ? ' · ' + by.commits + (by.commits === 1 ? ' commit' : ' commits') + ' since ' + (x.fromImage || d.fromImage || '?') : '');
    if (by.compare && String(by.compare).startsWith('https://')) return ' <a class="q who-q" href="' + esc(by.compare) + '" target="_blank" rel="noopener" data-tip="' + esc(tip + ' · opens the GitLab compare view') + '" aria-label="' + esc(tip) + '">?</a>';
    return ' <span class="q who-q" tabindex="0" role="img" data-tip="' + esc(tip) + '" aria-label="' + esc(tip) + '">?</span>';
  }
  function card(r, open, relsKnown) {
    var d = el('details', 'drep');
    d.id = 'rep-' + r.id;
    if (opened[r.id]) d.open = true;
    var rows = r.rows || [];
    var iss = r.issues || [];
    var g = relOf[r.id], title = repTitle(r);
    d.innerHTML = '<summary class="chev"><span class="t">' + esc(dayhm(r.createdAt)) + '</span><span class="s">' + esc(title) + '</span>' +
      '<span class="muted">' + esc(rows.length + ' deploys' + (iss.length ? ' · ' + iss.length + ' issues' : '') + ' · ' + who(r)) +
      (r.usd != null ? ' · <span class="usd" title="' + esc(r.costNote || 'what this check cost') + '">$' + esc(Number(r.usd).toFixed(2)) + '</span>' : '') + '</span></summary>';
    var sum = d.querySelector('summary'), tag = slackTag(r);
    if (g) sum.appendChild(jump('Release: ' + g.name, function () { showRelease(g.id); }));
    else if (relsKnown) sum.appendChild(el('span', 'muted', 'no release in this list'));
    if (tag) sum.appendChild(tag);
    sum.appendChild(linkBtn('r=' + encodeURIComponent(r.id), 'Copy a link that opens this deploy report'));
    var body = el('div', 'drep-body');
    body.appendChild(briefBox(r));
    var acts = el('div', 'rep-acts');
    acts.__rep = r;
    body.appendChild(acts);
    if (rows.length) {
      var tb = rows.map(function (x) {
        var dep = depBy[x.svc + '|' + x.toImage];
        return '<tr><td class="when">' + esc(hm(x.at)) + '</td><td class="svc-cell"><b>' + esc(x.svc + clusterTag(x)) + '</b>' + committers(x) + (x.ns ? ' <span class="muted">' + esc(x.ns) + '</span>' : '') + rowIssues(x) + '</td>' +
          '<td class="img">' + imgCell(x.fromImage || (dep && dep.fromImage), x.toImage, dep && dep.imageBy) + '</td><td class="flags">' + flags(x, r) + '</td><td>' + textLines(x.note, SEMI) + '</td>' +
          '<td class="sig">' + sigCell(x, r) + '</td></tr>';
      }).join('');
      body.appendChild(el('div', 'box', '<table><thead><tr><th>Deployed</th><th>Service</th><th>Image</th><th>Flags</th><th>Note</th><th>Signals now (1d/7d)</th></tr></thead><tbody>' + tb + '</tbody></table>'));
      body.querySelectorAll('button.flag-link').forEach(function (b) { b.addEventListener('click', function () { showLinked(encodeURIComponent(b.dataset.key)); }); });
    }
    if (r.pages) body.appendChild(pagesBox(r));
    if (iss.length || r.issuesNote) body.appendChild(issuesBox(r, iss));
    if (r.text) {
      var t = el('details', 'rep');
      t.innerHTML = '<summary class="chev">Plain-text report</summary><pre>' + esc(r.text) + '</pre>';
      body.appendChild(t);
    }
    d.appendChild(body);
    d.addEventListener('toggle', function () { opened[r.id] = d.open; });
    return d;
  }
  [relBox, repBox].forEach(function (box) {
    box.addEventListener('click', function (e) {
      var m = e.target.closest('.ikeys .more');
      if (!m) return;
      m.parentNode.querySelectorAll('a[hidden]').forEach(function (a) { a.hidden = false; });
      m.remove();
    });
  });
  var whoTip, whoTimer;
  function hideWhoTip() { clearTimeout(whoTimer); if (whoTip) { whoTip.remove(); whoTip = null; } }
  function showWhoTip(target) {
    hideWhoTip();
    whoTimer = setTimeout(function () {
      whoTip = el('div', 'who-tip', esc(target.dataset.tip));
      whoTip.setAttribute('role', 'tooltip');
      document.body.appendChild(whoTip);
      var r = target.getBoundingClientRect(), b = whoTip.getBoundingClientRect();
      whoTip.style.left = Math.max(8, Math.min(innerWidth - b.width - 8, r.left + r.width / 2 - b.width / 2)) + 'px';
      whoTip.style.top = Math.max(8, r.top - b.height - 7) + 'px';
    }, 80);
  }
  [relBox, repBox].forEach(function (box) {
    box.addEventListener('pointerover', function (e) { var t = e.target.closest('.who-q[data-tip]'); if (t) showWhoTip(t); });
    box.addEventListener('pointerout', function (e) { if (e.target.closest('.who-q[data-tip]')) hideWhoTip(); });
    box.addEventListener('focusin', function (e) { var t = e.target.closest('.who-q[data-tip]'); if (t) showWhoTip(t); });
    box.addEventListener('focusout', function (e) { if (e.target.closest('.who-q[data-tip]')) hideWhoTip(); });
  });
  function day(t) { return dayMonth(new Date(t)); }
  function byKey(a, b) { var x = a.split('-'), y = b.split('-'); return x[0] < y[0] ? -1 : x[0] > y[0] ? 1 : +x[1] - +y[1]; }
  function isRelease(d) { return !!d.kind; }
  function isGtm(d) { return d.kind === 'gtm'; }
  function hasDeploy(g) { return g.deps.some(function (d) { return !isRelease(d); }); }
  function checked(g) { return g.deps.some(function (d) { return !isRelease(d) && !d.ignored; }); }
  function onlyGtm(list) { return list.every(isGtm); }
  function releaseRow(x) {
    return '<tr class="kind-row"><td class="when">' + esc(hm(x.at)) + '</td><td><b>' + esc(x.svc) + '</b> <span class="muted">' + esc(x.kind) + '</span></td>' +
      '<td class="img">' + esc((x.fromImage || '?') + ' \u2192 ' + (x.toImage || '?')) + '</td><td><span class="muted">' + esc(x.kind) + ' release</span></td><td>' + esc(x.title || '') + '</td></tr>';
  }
  function gtmRow(x) {
    var g = x.gtm || {};
    var tip = 'GTM keeps no publish time or author; ' + brandName() + ' saw this version go live at ' + hm(x.at) + (g.storedAt ? ' (GTM stored it ' + hm(g.storedAt) + ')' : '');
    return '<tr class="gtm-row"><td class="when" title="' + esc(tip) + '">' + esc(hm(x.at)) + '</td><td><b>' + esc(x.svc) + '</b> <span class="muted">gtm</span></td>' +
      '<td class="img"><a href="https://tagmanager.google.com" target="_blank" rel="noopener" title="Open Google Tag Manager">' + esc((x.fromImage || '?') + ' → ' + (x.toImage || '?')) + '</a></td>' +
      '<td><span class="muted" title="' + esc(tip) + '">GTM publish</span></td><td><b>' + esc(g.name || '') + '</b>' + (g.changes ? '<div class="muted">' + esc(g.changes) + '</div>' : '') + '</td></tr>';
  }
  function releases(deps) {
    var list = [], cur = null;
    deps.slice().sort(function (a, b) { return a.at < b.at ? -1 : a.at > b.at ? 1 : 0; }).forEach(function (d) {
      var t = Date.parse(d.at);
      if (!(t > 0)) return;
      if (!cur || t - cur.end > RELEASE_GAP) { cur = {id: d.id, start: t, end: t, deps: [], reports: []}; list.push(cur); }
      cur.deps.push(d);
      cur.end = t;
    });
    return list.reverse();
  }
  function attach(rels, reps) {
    reps.forEach(function (r) {
      var best = null, hits = 0;
      rels.forEach(function (g) {
        var n = (r.rows || []).filter(function (x) { return g.deps.some(function (d) { return d.svc === x.svc && d.toImage === x.toImage; }); }).length;
        if (n > hits) { hits = n; best = g; }
      });
      if (!best) {
        var times = [r.windowFrom].concat((r.rows || []).map(function (x) { return x.at; })).map(function (s) { return Date.parse(s || ''); }).filter(function (t) { return t > 0; });
        best = rels.filter(function (g) { return times.some(function (t) { return t >= g.start - 3600000 && t <= g.end + 3600000; }); })[0] || null;
      }
      if (best) { best.reports.push(r); relOf[r.id] = best; }
    });
  }
  function names(g) {
    var n = {};
    g.deps.forEach(function (d) { (d.releases || []).forEach(function (v) { n[v] = (n[v] || 0) + 1; }); });
    var d0 = new Date(g.start);
    var ymd = ('0' + d0.getDate()).slice(-2) + '.' + ('0' + (d0.getMonth() + 1)).slice(-2) + '.' + d0.getFullYear();
    function today(v) { return v.indexOf(ymd) >= 0; }
    var out = Object.keys(n).sort(function (a, b) { return (today(b) - today(a)) || (n[b] - n[a]) || (a < b ? -1 : 1); });
    if (out.some(today)) out = out.filter(today);
    if (!out.length) g.reports.forEach(function (r) {
      if ((r.issues || []).some(function (i) { return i.inRelease; })) (r.releases || []).forEach(function (v) { if (out.indexOf(v) < 0) out.push(v); });
    });
    return out;
  }
  function unread(x) {
    if (!ME_GITLAB) return 'not read: GitLab is off';
    return Date.now() - Date.parse(x.at || '') < GITLAB_READ_MS ? 'not read from GitLab yet' : 'not read from GitLab';
  }
  function depTable(g) {
    var tb = g.deps.map(function (x) {
      if (isGtm(x)) return gtmRow(x);
      if (isRelease(x)) return releaseRow(x);
      var by = x.imageBy || {};
      var tip = esc((by.commits ? by.commits + (by.commits === 1 ? ' commit' : ' commits') + ' since ' + (x.fromImage || '?') + ' · ' : '') + (by.title || '') + (by.date ? ' · ' + by.date : ''));
      var names = esc((by.names || []).join(', '));
      var who = !names ? '<span class="muted">–</span>' : by.compare && String(by.compare).startsWith('https://') ? '<a href="' + esc(by.compare) + '" target="_blank" rel="noopener" title="' + tip + '">' + names + '</a>' : '<span title="' + tip + '">' + names + '</span>';
      var refused = (x.enriched || {}).refused;
      var read = (x.enriched || {}).gitlab;
      var tk = (x.tickets || []).length ? keyList(x.tickets) + (x.releasesNote ? '<div class="muted">releases: ' + esc(x.releasesNote) + '</div>' : '') : refused ? '<span class="muted">not read: ' + esc(refused) + '</span>'
        : x.ticketsNote ? '<span class="muted">' + esc(x.ticketsNote) + '</span>' : read ? '<span class="muted">none in the commits</span>' : '<span class="muted">' + unread(x) + '</span>';
      return '<tr><td class="when">' + esc(hm(x.at)) + '</td><td><b>' + esc(x.svc + clusterTag(x)) + '</b> <span class="muted">' + esc(x.ns || '') + '</span></td>' +
        '<td class="img">' + imgCell(x.fromImage, x.toImage, x.imageBy) + '</td><td>' + who + '</td><td>' + tk + '</td></tr>';
    }).join('');
    return el('div', 'box', '<table class="rel-deps"><thead><tr><th>Deployed</th><th>Service</th><th>Image</th><th>Commits by</th><th>Tickets</th></tr></thead><tbody>' + tb + '</tbody></table>');
  }
  function ongoing(g) {
    var running = Date.now() - g.end < SETTLE;
    var st = running ? ['Deploying', 's-processing'] : ['Report pending', 's-queued'];
    var n = g.deps.length + (g.deps.length === 1 ? ' service' : ' services');
    var last = hm(g.deps[g.deps.length - 1].at);
    var d = el('details', 'drep ongoing');
    d.id = 'ongoing-' + g.id;
    if (opened[d.id]) d.open = true;
    d.innerHTML = '<summary class="chev"><span class="st ' + st[1] + '">' + esc(st[0]) + '</span><span class="t">' + esc(day(g.start) + ' · ' + hm(g.deps[0].at) + (g.end > g.start ? '–' + last : '')) + '</span><span class="s">' + esc(g.name) + '</span>' +
      '<span class="muted">' + esc(running ? n + ' recorded so far, last ' + last + ' · the report follows once nothing has deployed for 20 min' : n + ', last ' + last + ' · waiting for the post-deploy report') + '</span></summary>';
    d.querySelector('summary').appendChild(jump('Release: ' + g.name, function () { showRelease(g.id); }));
    d.querySelector('summary').appendChild(linkBtn('rel=' + encodeURIComponent(g.id), 'Copy a link that opens this release on Releases'));
    var body = el('div', 'drep-body');
    body.appendChild(depTable(g));
    d.appendChild(body);
    d.addEventListener('toggle', function () { opened[d.id] = d.open; });
    return d;
  }
  function release(g) {
    var d = el('details', 'drep rel');
    d.id = 'rel-' + g.id;
    if (relOpen[g.id]) d.open = true;
    var vs = g.vs;
    var keys = [];
    g.deps.forEach(function (x) { (x.tickets || []).forEach(function (k) { if (keys.indexOf(k) < 0) keys.push(k); }); });
    keys.sort(byKey);
    var span = hm(g.deps[0].at) + (g.end > g.start ? '–' + hm(g.deps[g.deps.length - 1].at) : '');
    var plugged = g.deps.filter(isRelease), svcs = g.deps.length - plugged.length, pubs = plugged.length;
    var word = onlyGtm(plugged) ? (pubs === 1 ? ' GTM publish' : ' GTM publishes') : (pubs === 1 ? ' plug-in release' : ' plug-in releases');
    var meta = (svcs ? [svcs + (svcs === 1 ? ' service' : ' services'), keys.length + (keys.length === 1 ? ' ticket' : ' tickets')] : []).concat(pubs ? [pubs + word] : []);
    if (vs.length > 1) meta.push('also ' + vs.slice(1).join(', '));
    d.innerHTML = '<summary class="chev"><span class="t">' + esc(day(g.start) + ' · ' + span) + '</span><span class="s">' + esc(g.name) + '</span>' +
      '<span class="muted">' + esc(meta.join(' · ')) + '</span></summary>';
    var sum = d.querySelector('summary');
    g.reports.forEach(function (r) {
      var n = flagged(r);
      sum.appendChild(jump('Deploy report' + (g.reports.length > 1 ? ' ' + hm(r.createdAt) : '') + ' · ' + (n ? n + ' flagged' : 'clean'), function () { show(r.id); }));
    });
    sum.appendChild(linkBtn('rel=' + encodeURIComponent(g.id), 'Copy a link that opens this release'));
    var body = el('div', 'drep-body');
    body.appendChild(depTable(g));
    if (keys.length) {
      var unchecked = !vs.length && !g.deps.some(function (x) { return x.releases; });
      body.appendChild(el('div', 'rel-keys', '<span class="muted">Tickets · ' + keys.length + (unchecked ? ' · Jira release versions not checked yet' : '') + '</span>' + keyList(keys)));
    }
    d.appendChild(body);
    d.addEventListener('toggle', function () { if (d.open) relOpen[g.id] = true; else delete relOpen[g.id]; });
    return d;
  }
  function olderBar(deps, fresh) {
    if (fresh) {
      if (widened && deps.length === shown) oldest = true;
      widened = false;
      shown = deps.length;
    }
    if (oldest || days >= MAX_DAYS) { relBox.appendChild(el('p', 'muted rel-more', 'No older deploys recorded.')); return; }
    var p = el('p', 'muted rel-more', 'Deploys of the last ' + days + ' days. ');
    var b = el('button', 'btn sec small', 'Show older');
    b.type = 'button';
    b.addEventListener('click', function () { days = Math.min(days * 2, MAX_DAYS); widened = true; refresh(); });
    p.appendChild(b);
    relBox.appendChild(p);
  }
  function render(deps, reps, fresh) {
    var rels = deps ? releases(deps) : [];
    repsNow = reps || [];
    depBy = {};
    (deps || []).forEach(function (d) { if (d.imageBy) depBy[d.svc + '|' + d.toImage] = d; });
    relsNow = rels;
    relOf = {};
    attach(rels, reps || []);
    rels.forEach(function (g) { g.vs = names(g); g.name = g.vs[0] || (hasDeploy(g) ? 'Ad-hoc deploys · ' : onlyGtm(g.deps) ? 'GTM publish · ' : 'Plug-in releases \u00b7 ') + day(g.start); });
    relCount = deps ? rels.length : null;
    renderTabs();
    document.getElementById('count-reps').textContent = reps ? String(reps.length) : '–';
    relBox.innerHTML = '';
    if (!deps) relBox.appendChild(el('p', 'muted', 'Releases are unavailable right now.'));
    else if (!rels.length) relBox.appendChild(el('p', 'muted', 'No deploys recorded yet.'));
    rels.forEach(function (g) { relBox.appendChild(release(g)); });
    if (rels.length) olderBar(deps, fresh);
    var first = rels.filter(checked)[0];
    var live = first && reps && !first.reports.length && Date.now() - first.end < ONGOING ? first : null;
    var fresh0 = !live && reps && reps[0] && Date.now() - Date.parse(reps[0].createdAt || '') < RECENT ? reps[0] : null;
    flipAt = live ? live.end + (Date.now() - live.end < SETTLE ? SETTLE : ONGOING) : fresh0 ? Date.parse(fresh0.createdAt) + RECENT : 0;
    var tab = document.getElementById('tab-reps');
    var recentDeploy = (deps || []).some(function (d) { var age = Date.now() - Date.parse(d.at || ''); return !isRelease(d) && age >= 0 && age < 24 * 3600000; });
    var hot = live ? 'k-info' : fresh0 ? (flagged(fresh0) ? 'k-warn' : 'k-ok') : recentDeploy ? 'k-ok' : '';
    tab.className = 'tab ' + (hot ? hot + ' hot' : 'k-info');
    tab.title = live ? (Date.now() - live.end < SETTLE ? 'Release deploying now' : 'Release deployed, post-deploy report pending') : fresh0 ? 'New deploy report ' + hm(fresh0.createdAt) + ' · ' + (flagged(fresh0) ? flagged(fresh0) + ' flagged' : 'clean') : recentDeploy ? 'A deploy occurred in the last 24 h' : '';
    repBox.innerHTML = '';
    if (live) repBox.appendChild(ongoing(live));
    if (!reps) repBox.appendChild(el('p', 'muted', 'Deploy reports are unavailable right now.'));
    else if (!reps.length) repBox.appendChild(el('p', 'muted', 'No deploy reports yet.'));
    (reps || []).forEach(function (r, i) { repBox.appendChild(card(r, i === 0 && !live, !!deps)); });
    renderSlackQ();
    if (repActs) repActs(repBox);
  }
  var bodies = [null, null], last = null, lastAt = 0;
  function urls() { return ['/api/deploys?days=' + days, '/api/deploy-reports?limit=' + repLimit]; }
  async function fetchBody(u, i) {
    try {
      var r = await fetch(u, {headers: {'Accept': 'application/json'}});
      if (!r.ok) throw new Error('unavailable');
      var t = await r.text();
      JSON.parse(t);
      if (u !== urls()[i]) return false;
      bodies[i] = t;
      return true;
    } catch (e) {
      return false;
    }
  }
  async function refresh() {
    lastAt = Date.now();
    var n = ++seq, u = urls();
    var fresh = await Promise.all([fetchBody(u[0], 0), fetchBody(u[1], 1)]);
    var key = days + '\n' + repLimit + '\n' + ME_GITLAB + '\n' + bodies.join('\n');
    if ((key !== last || (flipAt && Date.now() >= flipAt)) && !repBox.querySelector('[data-confirm]')) {
      last = key;
      render(bodies[0] == null ? null : JSON.parse(bodies[0]), bodies[1] == null ? null : JSON.parse(bodies[1]), fresh[0]);
    }
    loaded = true;
    if (want && n > want.seq) land();
  }
  function link(kind, raw) {
    var id;
    try { id = decodeURIComponent(raw); } catch (e) { return; }
    want = {kind: kind, id: id, wide: false, seq: -1};
    showTab(kind === 'r' ? 'reps' : 'rels');
    if (loaded) land();
  }
  function relCard(id) {
    var g = relsNow.filter(function (x) { return x.id === id || x.deps.some(function (d) { return d.id === id; }); })[0];
    return g ? document.getElementById('rel-' + g.id) : null;
  }
  function onlyOpen(box, d) {
    box.querySelectorAll(':scope > details.drep').forEach(function (x) {
      var on = x === d;
      x.open = on;
      if (x.classList.contains('rel')) relOpen[x.id.slice(4)] = on;
      else opened[String(x.id).startsWith('rep-') ? x.id.slice(4) : x.id] = on;
    });
  }
  function land() {
    var w = want, rep = w.kind === 'r';
    var box = rep ? repBox : relBox;
    var d = rep ? document.getElementById('rep-' + w.id) : relCard(w.id);
    if (!d && !w.wide) {
      w.wide = true;
      w.seq = seq;
      if (rep) repLimit = MAX_REPORTS;
      else if (days < MAX_DAYS) { days = MAX_DAYS; widened = true; }
      refresh();
      return;
    }
    want = null;
    history.replaceState(null, '', location.pathname + location.search);
    var note = document.getElementById('link-note');
    if (note) note.remove();
    document.querySelectorAll('.drep.linked, #anoms article.inc.linked').forEach(function (c) { c.classList.remove('linked'); });
    if (!d) {
      note = el('p', 'panel-note', rep ? 'The linked deploy report is not on the page any more: a newer check may have replaced it.' : 'The linked release is not in the deploy history.');
      note.id = 'link-note';
      box.before(note);
      return;
    }
    onlyOpen(box, d);
    d.classList.add('linked');
    d.scrollIntoView({block: 'start'});
  }
  function reveal(prefix, id) {
    var d = document.getElementById(prefix + id);
    if (!d) return;
    d.open = true;
    d.scrollIntoView({block: 'start'});
  }
  function show(id) {
    showTab('reps');
    if (!id) return;
    opened[id] = true;
    reveal('rep-', id);
  }
  function showRelease(id) {
    showTab('rels');
    relOpen[id] = true;
    reveal('rel-', id);
  }
  refresh();
  setInterval(function () { if (!document.hidden && (!document.getElementById('panel-reps').hidden || !document.getElementById('panel-rels').hidden || !document.getElementById('panel-slack').hidden || Date.now() - lastAt > 900000)) refresh(); }, 60000);
  function current() {
    showTab('reps');
    var t = repBox.querySelector(':scope > details.drep');
    if (!t) return;
    opened[String(t.id).startsWith('rep-') ? t.id.slice(4) : t.id] = true;
    t.open = true;
    t.scrollIntoView({block: 'start'});
  }
  return {show: show, refresh: refresh, current: current, link: link, list: function () { return repsNow; }, title: repTitle};
}
