const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const PANEL = fs.readFileSync(path.join(__dirname, '..', '..', 'skopax', 'plugins', 'zabbix', 'panel.js'), 'utf8');
const SLOT = '<script type="text/plain" id="plugin-scripts"></script>';
const PLUGINS = [{id: 'zabbix', name: 'Zabbix', icon: '/plugins/zabbix/icon.svg', tab: 'zabbix', passes: [], instances: [], pages: [{tab: 'zabbix', label: 'Zabbix', group: 'observe', icon: '/plugins/zabbix/icon.svg'}]}];
const EVIL = '<img src=x onerror="window.__evil=1">';
const ZBX = 'https://zabbix.example.com';
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

function hostRow(name, tech, groups, extra) {
  return Object.assign({part: 'host', hostId: tech, host: tech, name, groups: groups.map((g, i) => ({id: String(i + 7), name: g})), maintenance: false, problems: {},
    interfaces: [{type: 'agent', available: 'available', main: true}], plugin: 'zabbix'}, extra || {});
}
function problemRow(eventId, triggerId, level, name, hostName, extra) {
  const labels = ['Not classified', 'Information', 'Warning', 'Average', 'High', 'Disaster'];
  return Object.assign({part: 'problem', eventId, triggerId, name, level, severity: labels[level], host: hostName, hostTech: hostName.toLowerCase().replace(' ', '-'), at: iso(90),
    acknowledged: false, link: ZBX + '/tr_events.php?triggerid=' + triggerId + '&eventid=' + eventId, plugin: 'zabbix'}, extra || {});
}
function summary(extra) {
  return Object.assign({part: 'summary', at: iso(1), instance: 'main', minSeverity: 'Warning', hosts: 2, problems: 2, hostsCapped: false, rowsCapped: false, problemsCapped: false,
    maxHosts: 300, maxRows: 200, maxProblems: 500, watchedGroups: [], plugin: 'zabbix'}, extra || {});
}
function filled() {
  return [hostRow('Web 01', 'web-01', ['Linux servers', 'Shop'], {problems: {High: 1, Disaster: 1}}),
    hostRow('Database 01', 'db-01', ['Databases'], {maintenance: true, problems: {Warning: 1}, interfaces: [{type: 'agent', available: 'unavailable', error: 'cannot connect', main: true}]}),
    problemRow('2', '1003', 5, 'Agent down', 'Web 01'), problemRow('1', '1001', 4, 'High CPU', 'Web 01', {acknowledged: true}), problemRow('3', '1002', 2, 'Disk low', 'Database 01', {link: 'javascript:alert(1)'}),
    summary()];
}
function card(triggerId, extra) {
  return Object.assign({key: 'zabbix|web-01|High CPU utilization|' + triggerId, src: 'zabbix', sig: 'zabbix', ns: '-', svc: 'web-01', sev: 'high', status: 'open', label: 'High CPU',
    short: 'HIGH Zabbix High CPU on Web 01', triggerId, zabbixSeverity: 'High', now: 1, firstSeen: iso(30), lastSeen: iso(1)}, extra || {});
}
function stateWith(rows, anomalies, sources) {
  return {rev: 3, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: Object.assign({prom: 'ok', zabbix: 'ok'}, sources || {}),
    summary: {level: 'ok', headline: 'x', points: [], at: iso(1)}, anomalies: anomalies || [], services: [], history: [], routes: [], deploys: [], zabbixPage: rows};
}

async function boot(state, lang) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  vc.on('error', (e) => errors.push('console.error ' + e));
  const html = pageHtml(TEMPLATE).replace(SLOT, '<script>\n' + PANEL + '</script>').replace('/*__SKOPAX_DATA__*/', JSON.stringify(state).replace(/<\//g, '<\\/'));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=zabbix', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      if (lang) {
        w.document.cookie = 'skopax_lang=' + lang + '; path=/';
        const raw = JSON.parse(fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'locale', lang, 'page.json'), 'utf8'));
        w.SKOPAX_I18N = {[lang]: Object.assign({}, raw.page, {'@rules': raw.rules, '@months': raw.months, '@days': raw.days, '@tails': raw.tails, '@th': raw.headers})};
      }
      w.fetch = async (p) => {
        const url = String(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (url.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', operator: true, admin: false, teams: [], tags: []});
        if (url.startsWith('/api/server')) return ok({plugins: PLUGINS, tz: 'UTC'});
        if (url.startsWith('/api/admin/settings')) return ok({items: [], rev: 1, refreshSeconds: 15});
        if (url.startsWith('/api/actions') || url.startsWith('/api/deploy-reports') || url.startsWith('/api/requests')) return ok([]);
        if (url.startsWith('/api/rev')) return ok({rev: state.rev});
        if (url.startsWith('/api/state')) return ok(state);
        return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
      };
    },
  });
  await wait(400);
  return {dom, w: dom.window, doc: dom.window.document, errors, section: dom.window.document.getElementById('panel-zabbix')};
}

function cells(row) { return [...row.querySelectorAll('td')].map((td) => td.textContent.trim()); }
function hostRows(section) { return [...section.querySelectorAll('#zbx-hosts tbody tr')]; }
function problemRows(section) { return [...section.querySelectorAll('#zbx-problems tbody tr')]; }
function choose(w, el, value) { el.value = value; el.dispatchEvent(new w.Event('change', {bubbles: true})); }
function type(w, el, value) { el.value = value; el.dispatchEvent(new w.Event('input', {bubbles: true})); }

async function hostsAreGroupedAndProblemsListedWithCardAndZabbixLinks() {
  //given
  const anomalies = [card('1001'), card('1009', {status: 'cleared'}), card('1001', {src: 'prom', key: 'err|x'})];

  //when
  const {dom, section, errors} = await boot(stateWith(filled(), anomalies));
  const hosts = hostRows(section), problems = problemRows(section);

  //then
  assert.ok(section, 'the page adds the Zabbix section');
  assert.deepStrictEqual(hosts.map((r) => r.classList.contains('grp') ? 'group ' + r.textContent : cells(r)[0]),
    ['group Databases 1', 'Database 01db-01', 'group Linux servers 1', 'Web 01web-01', 'group Shop 1', 'Web 01web-01']);
  assert.deepStrictEqual(cells(hosts[1]).slice(1), ['Unavailable Agent cannot connect', 'In maintenance', 'Warning 1']);
  assert.deepStrictEqual(cells(hosts[3]).slice(1), ['Available Agent', '\u2013', 'Disaster 1 High 1']);
  assert.deepStrictEqual(problems.map((r) => cells(r)[0] + '|' + cells(r)[1] + '|' + cells(r)[2] + '|' + cells(r)[4]),
    ['Disaster|Web 01|Agent down|\u2013', 'High|Web 01|High CPU|Acknowledged', 'Warning|Database 01|Disk low|\u2013']);
  const tags = [...section.querySelectorAll('#zbx-problems a.tag')];
  assert.deepStrictEqual(tags.map((a) => a.getAttribute('href')), ['#a=' + encodeURIComponent('zabbix|web-01|High CPU utilization|1001')], 'only the open Zabbix card of that trigger is linked');
  const out = [...section.querySelectorAll('#zbx-problems a.lnk')].map((a) => a.getAttribute('href'));
  assert.deepStrictEqual(out, [ZBX + '/tr_events.php?triggerid=1003&eventid=2', ZBX + '/tr_events.php?triggerid=1001&eventid=1'], 'only https links to Zabbix');
  assert.ok(/^\d{1,2} [A-Z][a-z]{2} \d{2}:\d{2}$|^\d{2}:\d{2}$/.test(cells(problems[0])[3]), 'the start shows like other pages: ' + cells(problems[0])[3]);
  assert.ok(section.querySelector('.zbx-inst').hidden, 'one instance needs no switcher');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'hosts are grouped by host group with availability, maintenance and counts, and problems list severity, host, start, acknowledged, the card and Zabbix';
}

async function filtersNarrowBothTablesAndSurviveARepaint() {
  //given
  const b = await boot(stateWith(filled()));
  const group = b.section.querySelector('select.zbx-group'), sev = b.section.querySelector('select.zbx-sev'), q = b.section.querySelector('input.zbx-q');

  //when
  const levels = [...sev.options].map((o) => o.textContent);
  choose(b.w, group, 'Linux servers');
  const byGroup = problemRows(b.section).map((r) => cells(r)[2]);
  choose(b.w, sev, '5');
  const bySeverity = problemRows(b.section).map((r) => cells(r)[2]);
  choose(b.w, sev, '');
  choose(b.w, group, '');
  q.focus();
  type(b.w, q, 'disk');
  const byText = problemRows(b.section).map((r) => cells(r)[2]);
  b.doc.dispatchEvent(new b.w.CustomEvent('skopax:state', {detail: {state: stateWith(filled())}}));
  const after = problemRows(b.section).map((r) => cells(r)[2]);
  type(b.w, q, 'nothing like this');
  const none = [...b.section.querySelectorAll('tr.empty')].map((r) => r.textContent);

  //then
  assert.deepStrictEqual(levels, ['All severities', 'Disaster', 'High', 'Average', 'Warning'], 'severities stop at the lowest severity of the instance');
  assert.deepStrictEqual(byGroup, ['Agent down', 'High CPU']);
  assert.deepStrictEqual(bySeverity, ['Agent down']);
  assert.deepStrictEqual(byText, ['Disk low']);
  assert.deepStrictEqual(after, ['Disk low'], 'a live update keeps the filters');
  assert.strictEqual(b.section.querySelector('input.zbx-q'), q, 'the search box is not rebuilt');
  assert.strictEqual(b.doc.activeElement, q, 'and keeps the focus');
  assert.deepStrictEqual(none, ['No host matches the filters.', 'No problem matches the filters.']);
  assert.deepStrictEqual(b.errors, []);
  b.dom.window.close();
  return 'host group, severity and text filters narrow both tables and survive a live update';
}

async function severalInstancesGetASwitcher() {
  //given
  const eu = [hostRow('Edge 01', 'edge-01', ['Edge'], {cluster: 'eu'}), problemRow('9', '2001', 3, 'Edge slow', 'Edge 01', {cluster: 'eu'}), summary({instance: 'eu', cluster: 'eu'})];
  const b = await boot(stateWith(filled().concat(eu), [card('2001', {cluster: 'eu', key: 'zabbix|edge-01|Edge slow|2001@eu'}), card('2001')]));
  const inst = b.section.querySelector('.zbx-inst');

  //when
  const names = [...inst.querySelectorAll('button')].map((x) => x.textContent + ':' + x.getAttribute('aria-pressed'));
  const first = problemRows(b.section).map((r) => cells(r)[2]);
  inst.querySelector('button[data-inst="eu"]').click();
  const second = problemRows(b.section).map((r) => cells(r)[2]);
  const tags = [...b.section.querySelectorAll('#zbx-problems a.tag')].map((a) => a.getAttribute('href'));

  //then
  assert.ok(!inst.hidden);
  assert.deepStrictEqual(names, ['main:true', 'eu:false']);
  assert.deepStrictEqual(first, ['Agent down', 'High CPU', 'Disk low']);
  assert.deepStrictEqual(second, ['Edge slow']);
  assert.deepStrictEqual(tags, ['#a=' + encodeURIComponent('zabbix|edge-01|Edge slow|2001@eu')], 'a card of the same trigger id on another instance is not linked');
  assert.deepStrictEqual(b.errors, []);
  b.dom.window.close();
  return 'several instances get a switcher and each shows its own hosts, problems and cards';
}

async function readErrorsAndEmptyStatesAreClearSentences() {
  //given
  const none = await boot(stateWith([]));
  const failed = await boot(stateWith([summary({error: 'Zabbix host.get: HTTP 502'})], [], {zabbix: 'error: Zabbix problem.get: unreachable'}));
  const denied = await boot(stateWith([summary({denied: true})]));
  const capped = await boot(stateWith(filled().slice(0, 5).concat([summary({hostsCapped: true, rowsCapped: true, problemsCapped: true})])));

  //when
  const empty = (b) => [...b.section.querySelectorAll('tr.empty')].map((r) => r.textContent);
  const notes = (b) => [...b.section.querySelectorAll('p[data-zbx]')].map((p) => p.textContent);

  //then
  assert.deepStrictEqual(empty(none), ['No Zabbix data yet. The Zabbix check fills this on its next run.', 'No Zabbix data yet. The Zabbix check fills this on its next run.']);
  assert.deepStrictEqual(notes(failed), ['The last Zabbix check failed: Zabbix problem.get: unreachable', 'Zabbix did not answer the page reads on the last check: Zabbix host.get: HTTP 502']);
  assert.deepStrictEqual(empty(failed), ['Hosts not read on the last check.', 'Problems not read on the last check.']);
  assert.deepStrictEqual(notes(denied), ['Not read: no access to hosts and problems.']);
  assert.deepStrictEqual(notes(capped), ['Only the first 300 hosts are shown.', 'Zabbix has more than 500 open problems; only the newest 500 are read.', 'Only the 200 most severe problems are listed.']);
  [none, failed, denied, capped].forEach((b) => { assert.deepStrictEqual(b.errors, []); b.dom.window.close(); });
  return 'no data, a failed check, a failed page read, a refused read and the caps each show a clear sentence';
}

async function aRefusedTokenSaysSoWithAndWithoutEarlierRows() {
  //given
  const refused = {zabbix: 'ok \u00b7 no access to problems'};
  const fresh = await boot(stateWith([], [], refused));
  const old = await boot(stateWith(filled(), [], refused));

  //when
  const empty = [...fresh.section.querySelectorAll('tr.empty')].map((r) => r.textContent);
  const notes = [...old.section.querySelectorAll('p[data-zbx]')].map((p) => p.textContent);
  const late = await boot(stateWith([summary({late: true})]));
  const lateNotes = [...late.section.querySelectorAll('p[data-zbx]')].map((p) => p.textContent);

  //then
  assert.deepStrictEqual(empty, ['Not read: no access to hosts and problems.', 'Not read: no access to hosts and problems.']);
  assert.deepStrictEqual(notes, ['Zabbix refused the token on the last check, so these rows are from an earlier check.']);
  assert.strictEqual(problemRows(old.section).length, 3, 'the earlier rows stay, named as earlier');
  assert.deepStrictEqual(lateNotes, ['The page was not read on the last check: the card pass took the time the page reads need.']);
  [fresh, old, late].forEach((b) => { assert.deepStrictEqual(b.errors, []); b.dom.window.close(); });
  return 'a refused token says the rows were not read, or that they are from an earlier check, and a skipped page read says why';
}

async function aNeverReadInstanceGetsItsTabAndItsSentence() {
  //given
  const b = await boot(stateWith(filled(), [], {'zabbix-eu': 'error: Zabbix apiinfo.version: unreachable'}));
  const inst = b.section.querySelector('.zbx-inst');

  //when
  const names = [...inst.querySelectorAll('button')].map((x) => x.textContent);
  inst.querySelector('button[data-inst="eu"]').click();
  const notes = [...b.section.querySelectorAll('p[data-zbx]')].map((p) => p.textContent);
  const empty = [...b.section.querySelectorAll('tr.empty')].map((r) => r.textContent);

  //then
  assert.ok(!inst.hidden);
  assert.deepStrictEqual(names, ['main', 'eu']);
  assert.deepStrictEqual(notes, ['The last Zabbix check failed: Zabbix apiinfo.version: unreachable']);
  assert.deepStrictEqual(empty, ['No Zabbix data yet. The Zabbix check fills this on its next run.', 'No Zabbix data yet. The Zabbix check fills this on its next run.']);
  assert.deepStrictEqual(b.errors, []);
  b.dom.window.close();
  return 'an instance that was never read still gets its tab and the sentence of its last check';
}

async function selectsKeepTheirValueFocusAndOptionsAcrossALiveUpdate() {
  //given
  const b = await boot(stateWith(filled()));
  const group = b.section.querySelector('select.zbx-group'), sev = b.section.querySelector('select.zbx-sev');
  choose(b.w, group, 'Databases');
  choose(b.w, sev, '4');
  const options = [...group.options], button = b.section.querySelector('.zbx-inst');
  group.focus();

  //when
  b.doc.dispatchEvent(new b.w.CustomEvent('skopax:state', {detail: {state: stateWith(filled())}}));

  //then
  assert.strictEqual(b.section.querySelector('select.zbx-group'), group);
  assert.deepStrictEqual([...group.options], options, 'the options are the same elements');
  assert.strictEqual(group.value, 'Databases');
  assert.strictEqual(sev.value, '4');
  assert.strictEqual(b.doc.activeElement, group, 'the open select keeps the focus');
  assert.strictEqual(b.section.querySelector('.zbx-inst'), button);
  assert.deepStrictEqual(b.errors, []);
  b.dom.window.close();
  return 'the host group and severity selects keep their value, focus and option elements across a live update';
}

async function aFailedPageReadShowsTheEarlierRowsWithTheirTime() {
  //given
  const rows = filled().slice(0, 5).concat([summary({error: 'Zabbix host.get: HTTP 502', rowsAt: '2026-11-20T08:05:00Z', trimmed: true})]);

  //when
  const b = await boot(stateWith(rows));
  const notes = [...b.section.querySelectorAll('p[data-zbx]')].map((p) => p.textContent);
  const at = new Date('2026-11-20T08:05:00Z');
  const pad = (n) => (n < 10 ? '0' : '') + n;
  const shown = at.getDate() + ' Nov ' + pad(at.getHours()) + ':' + pad(at.getMinutes());

  //then
  assert.deepStrictEqual(notes, ['Zabbix did not answer the page reads on the last check: Zabbix host.get: HTTP 502', 'These rows are from an earlier check: ' + shown,
    'Some rows are left out to keep the page small.']);
  assert.strictEqual(problemRows(b.section).length, 3);
  assert.strictEqual(hostRows(b.section).filter((r) => !r.classList.contains('grp')).length, 3);
  assert.deepStrictEqual(b.errors, []);
  b.dom.window.close();
  return 'a failed page read shows the earlier rows with the time they were read, and a trimmed page says so';
}

async function aBrokenAnswerSentenceIsTranslated() {
  //given
  const rows = [summary({broken: true})];
  const raw = JSON.parse(fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'locale', 'tr', 'page.json'), 'utf8')).page;

  //when
  const en = await boot(stateWith(rows));
  const tr = await boot(stateWith(rows), 'tr');
  await wait(100);
  const text = (b) => b.section.querySelector('p[data-zbx="page-error"]').textContent;

  //then
  assert.strictEqual(text(en), 'Zabbix sent an answer the page could not read on the last check.');
  assert.strictEqual(text(tr), raw['Zabbix sent an answer the page could not read on the last check.']);
  assert.notStrictEqual(text(tr), text(en));
  assert.strictEqual(tr.section.querySelector('p[data-zbx="page-error"] [data-no-tx]'), null);
  assert.deepStrictEqual([...en.section.querySelectorAll('tr.empty')].map((r) => r.textContent), ['Hosts not read on the last check.', 'Problems not read on the last check.']);
  [en, tr].forEach((b) => { assert.deepStrictEqual(b.errors, []); b.dom.window.close(); });
  return 'an answer the page could not read gives a fixed sentence that the language packs translate';
}

async function remoteTextIsRenderedAsText() {
  //given
  const rows = [hostRow(EVIL, EVIL, [EVIL], {interfaces: [{type: 'agent', available: 'unavailable', error: EVIL}]}), problemRow('1', '1001', 4, EVIL, EVIL, {link: EVIL}), summary({error: EVIL})];

  //when
  const b = await boot(stateWith(rows, [card('1001', {short: EVIL, key: EVIL})], {zabbix: 'error: ' + EVIL}));
  await wait(50);

  //then
  assert.strictEqual(b.w.__evil, undefined);
  assert.strictEqual(b.section.querySelectorAll('img').length, 0);
  assert.ok(b.section.textContent.indexOf(EVIL) >= 0, 'the remote text shows as text');
  assert.strictEqual(b.section.querySelectorAll('#zbx-problems a.lnk').length, 0);
  assert.deepStrictEqual([...b.section.querySelectorAll('select.zbx-group option')].map((o) => o.textContent), ['All host groups', EVIL]);
  assert.deepStrictEqual(b.errors, []);
  b.dom.window.close();
  return 'host, group, problem and error text from Zabbix renders as text, attributes included';
}

async function turkishTranslatesThePageButNotZabbixText() {
  //given
  const b = await boot(stateWith(filled()), 'tr');
  await wait(100);

  //when
  const heads = [...b.section.querySelectorAll('h3.sec-t')].map((x) => x.textContent);
  const cols = [...b.section.querySelectorAll('#zbx-problems th')].map((x) => x.textContent);
  const problems = problemRows(b.section).map((r) => cells(r)[2]);
  const first = cells(problemRows(b.section)[0])[0];

  //then
  assert.deepStrictEqual(heads, ['Host grubuna g\u00f6re host\'lar', 'A\u00e7\u0131k problemler']);
  assert.deepStrictEqual(cols, ['\u00d6nem derecesi', 'Host', 'Problem', 'Ba\u015fland\u0131', 'Onayland\u0131', 'Linkler']);
  assert.strictEqual(first, 'Felaket');
  assert.deepStrictEqual(problems, ['Agent down', 'High CPU', 'Disk low'], 'problem names from Zabbix stay as Zabbix wrote them');
  assert.strictEqual(b.section.querySelector('input.zbx-q').placeholder, 'Host ve problem ara');
  assert.deepStrictEqual(b.errors, []);
  b.dom.window.close();
  return 'in Turkish the page text is translated and the text from Zabbix is not';
}

(async () => {
  for (const t of [hostsAreGroupedAndProblemsListedWithCardAndZabbixLinks, filtersNarrowBothTablesAndSurviveARepaint, severalInstancesGetASwitcher, readErrorsAndEmptyStatesAreClearSentences,
    aRefusedTokenSaysSoWithAndWithoutEarlierRows, aNeverReadInstanceGetsItsTabAndItsSentence, selectsKeepTheirValueFocusAndOptionsAcrossALiveUpdate,
    aFailedPageReadShowsTheEarlierRowsWithTheirTime, aBrokenAnswerSentenceIsTranslated, remoteTextIsRenderedAsText, turkishTranslatesThePageButNotZabbixText]) {
    console.log('PASS ' + (await t()));
  }
})().catch((e) => { console.error('FAIL', e.message); process.exit(1); });
