require('./focus');
const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');
const {frozenDate} = require('./clock');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const at = (s) => new Date(s).toISOString().replace('.000Z', 'Z');
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const dayMonth = (s) => new Date(s).getDate() + ' ' + MONTHS[new Date(s).getMonth()];

function dep(id, ns, svc, from, to, when, extra) {
  return Object.assign({id, ns, svc, fromImage: from, toImage: to, at: at(when), createdAt: at(when)}, extra || {});
}

const DEPLOYS = [
  dep('d9', 'storefront', 'shop-admin', '9c9c9c9c', 'f6a7b8c9', '2026-10-13T14:31:00Z', {enriched: {gitlab: '2026-10-13T14:32:00Z'}}),
  dep('d8', 'storefront', 'shop-web', 'd4e5f6a7', '0f1e2d3c', '2026-10-13T05:35:00Z', {tickets: ['SHOP-303'], releases: ['WEB 13.10.2026 Release', 'OPS 08.10.2026 Release'], imageBy: {names: ['Ella'], title: 'SHOP-303 banner', sha: '0f1e2d3c', url: 'https://gitlab.com/acme/web/shop-web/-/commit/0f1e2d3c'}}),
  dep('d7', 'payments', 'orders-service', '3c3c3c3c', '4d4d4d4d', '2026-10-13T05:11:00Z', {tickets: ['OPS-106', 'OPS-115'], releases: ['OPS 08.10.2026 Release', 'OPS 13.10.2026 Release'], imageBy: {names: ['Erin', 'Irene'], title: 'OPS-115 bundle calc', sha: '4d4d4d4d', url: 'https://gitlab.com/acme/payments/orders-service/-/commit/4d4d4d4d'}}),
  dep('d6', 'shop', 'checkout-service', '1a1a1a1a', '2b2b2b2b', '2026-10-13T05:07:00Z', {tickets: ['OPS-115'], releases: ['OPS 13.10.2026 Release']}),
  dep('d5', 'identity', 'api-gateway-public', '78879605', 'c3d4e5f6', '2026-10-13T05:05:00Z', {ticketsNote: 'no local clone'}),
  dep('d4', 'storefront', 'shop-web', '1e2d3c4b', 'd4e5f6a7', '2026-10-08T05:19:00Z', {tickets: ['SHOP-302']}),
  dep('d3', 'web', 'web-bff', 'a1b2c3d4', '3c4b5a69', '2026-10-08T05:15:00Z', {tickets: ['OPS-105']}),
  dep('d2', 'identity', 'batch-service', '96051423', '8b8b8b8b', '2026-10-01T07:53:00Z'),
  dep('d1', 'streaming', 'streaming-kafka-exporter', '0.45.0-kafka-3.9.0', '0.45.2-kafka-3.9.2', '2026-10-05T23:09:00Z'),
];

const REPORTS = [
  {id: 'r1', createdAt: at('2026-10-13T08:22:19Z'), usd: 0, costNote: 'Prometheus and Jira queries only, no model', summary: '14 deploys since 05:06 UTC: 11 clean', windowFrom: at('2026-10-13T05:06:00Z'), windowTo: at('2026-10-13T08:14:30Z'),
    rows: [{svc: 'checkout-service', ns: 'shop', fromImage: '1a1a1a1a', toImage: '2b2b2b2b', at: at('2026-10-13T05:08:00Z'), flags: []},
      {svc: 'orders-service', ns: 'payments', fromImage: '3c3c3c3c', toImage: '4d4d4d4d', at: at('2026-10-13T05:12:00Z'), flags: ['restarts=3']}],
    releases: ['OPS 13.10.2026 Release', 'WEB 13.10.2026 Release'], issues: [{key: 'OPS-115', inRelease: true}], actionId: 'a1'},
  {id: 'r2', createdAt: at('2026-10-08T06:00:00Z'), summary: '12 deploys since 05:05 UTC: all clean', windowFrom: at('2026-10-08T05:05:00Z'),
    rows: [{svc: 'content-service', ns: 'members', fromImage: 'e5f6a7b8', toImage: '87960514', at: at('2026-10-08T05:13:00Z'), flags: []}],
    releases: ['OPS 08.10.2026 Release'], issues: [{key: 'OPS-105', inRelease: true}]},
  {id: 'r3', createdAt: at('2026-08-20T06:00:00Z'), summary: 'old check', windowFrom: at('2026-08-20T05:00:00Z'), rows: [{svc: 'x-service', toImage: 'aaaa1111', at: at('2026-08-20T05:01:00Z')}]},
];

function stateWith() {
  return {
    rev: 5, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sweptAt: '14:04', nextRun: '15:04',
    sources: {prom: 'ok', logs: 'ok', apm: 'ok', cf: 'ok', pmm: 'ok', qan: 'ok', mongo: 'ok'}, summary: {level: 'ok', headline: 'h', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, deploys: [{ns: 'storefront', svc: 'shop-admin', old: '9c9c9c9c', new: 'f6a7b8c9', at: iso(30)}],
  };
}

async function boot(state, deploys, reports, actions) {
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state).replace(/<\//g, '<\\/'));
  const answer = (body) => ({ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body)});
  const dom = new JSDOM(html, {
    url: 'https://skopax.dev.example.com/', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () { w.__scrolledTo = this.id; };
      w.fetch = async (p) => {
        const path = String(p);
        calls.push(path);
        if (path.startsWith('/api/me')) return answer({loginEnabled: false, jiraUrl: 'https://jira.example.com', task: {projects: ['OPS']}});
        if (path.startsWith('/api/rev')) return answer({rev: state.rev});
        if (path.startsWith('/api/deploys')) return answer(typeof deploys === 'function' ? deploys(+new URL(path, 'https://x/').searchParams.get('days')) : deploys);
        if (path.startsWith('/api/deploy-reports')) return answer(reports);
        if (path.startsWith('/api/actions')) return answer(actions || []);
        return answer([]);
      };
    },
  });
  await wait(300);
  return {dom, doc: dom.window.document, errors, calls};
}

async function releasesAndReportsOnTheirOwnTabs() {
  //given
  const {dom, doc, errors, calls} = await boot(stateWith(), DEPLOYS, REPORTS);

  //then
  assert.ok(calls.some((c) => /^\/api\/deploys\?days=365$/.test(c)), 'page asks for a year of deploys: ' + calls.join(','));
  const label = (id) => doc.getElementById('tab-' + id).querySelector('.lbl').textContent;
  assert.deepStrictEqual([label('rels'), label('reps')], ['Releases', 'Deploy reports'], 'two tabs, Deploy reports keeps the reps id');
  assert.ok(doc.getElementById('tab-rels').nextElementSibling === doc.getElementById('tab-reps'), 'Deploy reports sits right after Releases');
  const relTab = doc.getElementById('tab-rels');
  assert.strictEqual(doc.getElementById('count-rels').textContent, '1 deployed', 'with a new image in the last 24 h the Releases tab counts those services');
  assert.ok(relTab.classList.contains('hot') && relTab.classList.contains('k-act'), 'and is coloured: ' + relTab.className);
  assert.strictEqual(relTab.title, '1 service with a new image in the last 24 h: shop-admin');
  assert.strictEqual(doc.getElementById('count-reps').textContent, '3', 'three reports');
  const rels = [...doc.querySelectorAll('#rels > details.rel')];
  const titles = rels.map((d) => d.querySelector(':scope > summary .s').textContent);
  assert.deepStrictEqual(titles.slice(0, 5), [
    'Ad-hoc deploys \u00b7 ' + dayMonth('2026-10-13T14:31:00Z'),
    'OPS 13.10.2026 Release',
    'OPS 08.10.2026 Release',
    'Ad-hoc deploys \u00b7 ' + dayMonth('2026-10-05T23:09:00Z'),
    'Ad-hoc deploys \u00b7 ' + dayMonth('2026-10-01T07:53:00Z'),
  ], 'newest first, named by Jira version, report fallback, otherwise ad-hoc on the day and English month');
  assert.deepStrictEqual([titles[0], titles[4]], ['Ad-hoc deploys \u00b7 13 Oct', 'Ad-hoc deploys \u00b7 1 Oct']);
  assert.deepStrictEqual(rels.slice(0, 5).map((d) => d.open), [false, false, false, false, false], 'everything folded on load');

  const morning = rels[1];
  assert.deepStrictEqual([...morning.querySelectorAll('.rel-deps tbody tr')].map((tr) => tr.querySelector('b').textContent),
    ['api-gateway-public', 'checkout-service', 'orders-service', 'shop-web'], 'deploys 4 min to 24 min apart stay one release');
  assert.match(morning.querySelector('summary .muted').textContent, /^4 services \u00b7 3 tickets \u00b7 also WEB 13\.10\.2026 Release$/, 'versions of the release day win over an older version that ties on count, and the older one is left out');
  const orders = morning.querySelectorAll('.rel-deps tbody tr')[2];
  assert.match(orders.textContent, /3c3c3c3c \u2192 4d4d4d4d/);
  assert.match(orders.textContent, /Erin, Irene/);
  assert.strictEqual(orders.querySelector('.img a.cmp').href, 'https://gitlab.com/acme/payments/orders-service/-/compare/3c3c3c3c...4d4d4d4d', 'the image pair opens the GitLab compare view, built from the commit URL');
  assert.deepStrictEqual([...orders.querySelectorAll('.ikeys a')].map((a) => a.href), ['https://jira.example.com/browse/OPS-106', 'https://jira.example.com/browse/OPS-115']);
  assert.match(morning.querySelectorAll('.rel-deps tbody tr')[0].textContent, /no local clone/);
  assert.deepStrictEqual([...morning.querySelectorAll('.rel-keys a')].map((a) => a.textContent), ['OPS-106', 'OPS-115', 'SHOP-303'], 'release tickets as Jira links, sorted');
  assert.ok(!doc.querySelector('#rels details.drep:not(.rel)') && !doc.querySelector('#rels [id^="rep-"]'), 'no report content inside the release cards');
  assert.deepStrictEqual([...morning.querySelectorAll(':scope > summary .xlink')].map((b) => b.textContent), ['Deploy report \u00b7 1 flagged'], 'the 13.10 release links its report (matched by image)');
  assert.deepStrictEqual([...rels[2].querySelectorAll(':scope > summary .xlink')].map((b) => b.textContent), ['Deploy report \u00b7 clean'], 'the 08.10 report attaches by time when no image matches');
  assert.ok(!rels[0].querySelector('.xlink') && !rels[3].querySelector('.xlink'), 'no link without a report');
  assert.match(rels[2].querySelector('.rel-keys').textContent, /OPS-105/);
  assert.match(rels[3].textContent, /0\.45\.0-kafka-3\.9\.0 \u2192 0\.45\.2-kafka-3\.9\.2/);
  assert.match(rels[0].querySelector('.rel-deps').textContent, /none in the commits/);
  assert.ok(!doc.getElementById('rel-other'), 'no orphan fold on the Releases tab any more');
  const reps = [...doc.querySelectorAll('#reps > details.drep')];
  assert.deepStrictEqual(reps.map((d) => d.id), ['rep-r1', 'rep-r2', 'rep-r3'], 'reports on their own tab, newest first');
  assert.deepStrictEqual(reps.map((d) => d.open), [false, false, false], 'reports fold on load');
  assert.deepStrictEqual(reps.map((d) => (d.querySelector(':scope > summary .xlink') || {}).textContent), ['Release: OPS 13.10.2026 Release', 'Release: OPS 08.10.2026 Release', undefined], 'each report names its release');
  assert.match(reps[2].querySelector(':scope > summary').textContent, /no release in this list/, 'an orphan report says so');
  const hhmm = (t) => new Date(t).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hour12: false});
  assert.deepStrictEqual(reps.map((d) => d.querySelector(':scope > summary .s').textContent), [
    'OPS 13.10.2026 Release \u00b7 ' + hhmm('2026-10-13T05:06:00Z') + '\u2013' + hhmm('2026-10-13T08:14:30Z') + ' \u00b7 1 flagged',
    'OPS 08.10.2026 Release \u00b7 ' + hhmm('2026-10-08T05:05:00Z') + '\u2013' + hhmm('2026-10-08T06:00:00Z') + ' \u00b7 clean',
    'Deploy report \u00b7 ' + hhmm('2026-08-20T05:00:00Z') + '\u2013' + hhmm('2026-08-20T06:00:00Z') + ' \u00b7 clean',
  ], 'short titles built by the page: release name (else Deploy report) \u00b7 check window (saved time when it has no end) \u00b7 clean or N flagged');
  assert.ok(reps.every((d, i) => !d.querySelector(':scope > summary').textContent.includes(REPORTS[i].summary)), 'the stored one-line summary is not in the title');
  assert.ok(reps.every((d) => d.querySelector(':scope > summary').firstElementChild.className === 't'), 'the time stays at the left');
  assert.strictEqual(reps[0].querySelector(':scope > summary .muted').textContent, '2 deploys \u00b7 1 issues \u00b7 Investigator \u00b7 $0.00', 'the muted part no longer repeats the window or the flagged count');
  assert.match(reps[0].querySelector('.drep-body').textContent, /restarts=3/, 'report content stays on the report card');
  assert.ok(!doc.querySelector('#tiles [data-id="deploys"]'), 'no Deployed services pill any more');
  const recent = doc.getElementById('rels-recent');
  assert.ok(!recent.hidden && /^1 service deployed in the last 24 h: shop-admin Show on Services \u203a$/.test(recent.textContent), 'a line above the releases names the 24 h deploys: ' + recent.textContent);
  assert.ok(doc.querySelector('#rels .rel-more button'), 'Show older button');
  const hint = doc.getElementById('rels-hint');
  assert.ok(hint.tagName === 'DETAILS' && !hint.open, 'the tab description is folded');
  assert.strictEqual(hint.querySelector('summary').textContent.trim(), 'Every prod image change, grouped into releases. ?', 'one short line plus the ? badge');
  assert.match(hint.querySelector('.q').title, /^Deploys less than 2 hours apart form one release/, 'full text as the ? tooltip');
  assert.match(hint.querySelector('p').textContent, /Copy link on a release gives a link/, 'full text inside when unfolded');
  const rhint = doc.getElementById('reps-hint');
  assert.ok(rhint.tagName === 'DETAILS' && !rhint.open && rhint.querySelector('summary').textContent.trim() === 'Post-deploy checks, newest first. ?', 'Deploy reports description is one line too');
  assert.match(rhint.querySelector('.q').title, /links to the release it covers/);
  assert.deepStrictEqual(errors, []);

  assert.strictEqual(recent.querySelector('button').title, 'Opens Services, filtered to Deployed (24 h)', 'the line names the sidebar page, not a tab');
  assert.deepStrictEqual([hint.querySelector('.q').title, rhint.querySelector('.q').title].filter((t) => /(Services|Releases|Deploy reports) tab|the tab is coloured/.test(t)), [], 'the Releases and Deploy reports notes name sidebar pages, not tabs');
  assert.match(hint.querySelector('.q').title, /the Releases row in the sidebar is coloured and counts them, and a line above the list opens them on Services/);

  //when
  recent.querySelector('button').click();

  //then
  assert.strictEqual(doc.getElementById('tab-svcs').getAttribute('aria-current'), 'page', 'the line opens the Services tab');
  assert.strictEqual(doc.querySelector('#svc-chips [aria-pressed="true"]').dataset.f, 'deployed', 'filtered to Deployed (24 h)');
  dom.window.close();

  //given
  const quiet = await boot(Object.assign(stateWith(), {deploys: []}), DEPLOYS, REPORTS);

  //then
  const qTab = quiet.doc.getElementById('tab-rels');
  assert.strictEqual(quiet.doc.getElementById('count-rels').textContent, '5', 'without 24 h deploys the tab counts the releases');
  assert.ok(!qTab.classList.contains('hot'), 'and stays plain');
  assert.strictEqual(qTab.title, 'no service got a new image in the last 24 h');
  assert.ok(quiet.doc.getElementById('rels-recent').hidden, 'no 24 h line');
  assert.deepStrictEqual(quiet.errors, []);
  quiet.dom.window.close();
  console.log('PASS Releases and Deploy reports tabs: deploys grouped by a 2 h gap, named by Jira version (report fallback, else Ad-hoc), newest open and older folded, services/who/tickets, no report content in a release, reports newest first with their release named and a short page-built title (release \u00b7 window \u00b7 clean / N flagged, no stored summary), orphan report says so, both counts; the 24 h deploys colour and count the Releases tab and a line opens them on Services (no pill)');
}

async function crossLinksAndStateSurvive() {
  //given
  const action = {id: 'a1', type: 'postdeploy', key: 'postdeploy', target: 'postdeploy', status: 'done', result: '14 deploys', requestedAt: iso(60), doneAt: iso(40), reportId: 'r1'};
  const {dom, doc, errors, calls} = await boot(stateWith(), DEPLOYS, REPORTS, [action]);
  const w = dom.window;
  const fetched = () => calls.filter((c) => c.startsWith('/api/deploys')).length;

  //when
  const pdc = doc.getElementById('pdc');
  assert.ok(pdc.parentNode.classList.contains('reps-head') && pdc.previousElementSibling === doc.getElementById('reps-hint'), 'the Post-deploy check sits in the Deploy reports header, beside its description');
  assert.ok(!pdc.closest('#reps') && !doc.getElementById('reps-hint').contains(pdc), 'outside the re-rendered list and the folded hint');
  assert.strictEqual(doc.getElementById('pdc-btn').textContent, 'Post-deploy check');
  assert.ok(doc.getElementById('pdc-btn').classList.contains('op-btn'), 'same operator gate as before');
  assert.match(doc.getElementById('reps-hint').querySelector('.q').title, /Post-deploy check button beside this line/);
  const link = doc.querySelector('#pdc .pdc-st button');
  assert.ok(link, 'post-deploy status shows its report link');
  let before = fetched();
  link.click();
  await wait(50);

  //then
  assert.strictEqual(fetched(), before + 1, 'opening the tab refreshes once');
  assert.strictEqual(doc.getElementById('panel-reps').hidden, false, 'Deploy reports tab shown');
  assert.ok(doc.getElementById('rep-r1').open, 'report opened');
  assert.strictEqual(w.__scrolledTo, 'rep-r1');

  //when
  const rep2 = doc.getElementById('rep-r2');
  rep2.querySelector(':scope > summary .xlink').click();
  await wait(50);

  //then
  assert.strictEqual(doc.getElementById('panel-rels').hidden, false, 'Release: link opens the Releases tab');
  assert.ok(doc.getElementById('panel-reps').hidden);
  assert.ok(doc.getElementById('rel-d3').open, 'its release is opened');
  assert.strictEqual(w.__scrolledTo, 'rel-d3');
  assert.ok(!rep2.open, 'clicking the link does not toggle the report card itself');

  //when
  const morning = doc.getElementById('rel-d5');
  assert.ok(!morning.open);
  morning.querySelector(':scope > summary .xlink').click();
  await wait(50);

  //then
  assert.strictEqual(doc.getElementById('panel-reps').hidden, false, 'Deploy report link opens the Deploy reports tab');
  assert.strictEqual(w.__scrolledTo, 'rep-r1');
  assert.ok(!morning.open, 'clicking the link does not toggle the release card');

  //when
  morning.querySelector(':scope > summary').click();
  await wait(20);

  //then
  assert.ok(morning.open, 'a click on the summary itself still opens the card');

  //when
  before = calls.length;
  doc.querySelector('#rels .rel-more button').click();
  await wait(100);

  //then
  assert.ok(calls.slice(before).some((c) => c === '/api/deploys?days=730'), 'Show older doubles the window');
  assert.ok(!doc.querySelector('.rel-more button') && /No older deploys recorded/.test(doc.querySelector('.rel-more').textContent), 'a wider window with nothing new drops the button');
  assert.ok(doc.getElementById('rel-d5').open && doc.getElementById('rel-d3').open && doc.getElementById('rep-r1').open, 'opened releases and report stay open after a re-render');
  assert.ok(!doc.getElementById('rel-d9').open, 'a release nobody opened stays folded after a re-render');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS the post-deploy report link opens Deploy reports on the report; Release: and Deploy report links jump between the tabs without toggling their own card; Show older widens the window and open folds survive the re-render');
}

async function failingBoot(failing, body) {
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(Object.assign(stateWith(), {deploys: []})));
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(html, {
    url: 'https://skopax.dev.example.com/', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        const path = String(p);
        if (path.startsWith(failing)) return {ok: false, status: 404, json: async () => ({}), text: async () => '{}'};
        const answer = path.startsWith('/api/deploys') || path.startsWith('/api/deploy-reports') ? body(path) : path.startsWith('/api/me') ? {loginEnabled: false, jiraUrl: 'https://jira.example.com', task: {projects: ['OPS']}} : [];
        return {ok: true, status: 200, json: async () => answer, text: async () => JSON.stringify(answer)};
      };
    },
  });
  await wait(300);
  return {dom, doc: dom.window.document, errors};
}

async function emptyAndFailing() {
  //given
  const empty = await boot(Object.assign(stateWith(), {deploys: []}), [], []);

  //then
  assert.deepStrictEqual([empty.doc.getElementById('count-rels').textContent, empty.doc.getElementById('count-reps').textContent], ['0', '0']);
  assert.match(empty.doc.getElementById('rels').textContent, /No deploys recorded yet/);
  assert.match(empty.doc.getElementById('reps').textContent, /No deploy reports yet/);
  empty.dom.window.close();

  //given
  const noDeploys = await failingBoot('/api/deploys?', () => REPORTS);

  //then
  assert.match(noDeploys.doc.getElementById('rels').textContent, /Releases are unavailable right now/);
  assert.strictEqual(noDeploys.doc.getElementById('count-rels').textContent, '\u2013');
  assert.deepStrictEqual([...noDeploys.doc.querySelectorAll('#reps > details.drep')].map((d) => d.id), ['rep-r1', 'rep-r2', 'rep-r3'], 'reports still render without the deploys endpoint');
  assert.ok(!/no release in this list/.test(noDeploys.doc.getElementById('reps').textContent) && !noDeploys.doc.querySelector('#reps .xlink'), 'no release claims without release data');
  assert.ok([...noDeploys.doc.querySelectorAll('#reps > details.drep > summary .s')].every((x) => /^Deploy report \u00b7 \d\d:\d\d\u2013\d\d:\d\d \u00b7 (clean|\d+ flagged)$/.test(x.textContent)), 'without release data the title says Deploy report');
  assert.deepStrictEqual(noDeploys.errors, []);
  noDeploys.dom.window.close();

  //given
  const noReports = await failingBoot('/api/deploy-reports', () => DEPLOYS);

  //then
  assert.match(noReports.doc.getElementById('reps').textContent, /Deploy reports are unavailable right now/);
  assert.strictEqual(noReports.doc.getElementById('count-rels').textContent, '5', 'releases still render without the reports endpoint');
  assert.deepStrictEqual(noReports.errors, []);
  noReports.dom.window.close();
  console.log('PASS no deploys or reports says so on each tab; one endpoint failing shows its note and leaves the other tab working');
}

async function showOlderStopsAtTheCap() {
  //given
  const older = (n) => dep('o' + n, 'identity', 'auth-service', 'aaaa000' + n, 'bbbb000' + n, new Date(Date.now() - (300 + n * 400) * 86400000).toISOString());
  const byDays = (d) => DEPLOYS.concat([1, 2, 3, 4].filter((n) => 300 + n * 400 <= d).map(older));
  const {dom, doc, errors, calls} = await boot(stateWith(), byDays, []);

  //when
  for (let i = 0; i < 6; i++) {
    const b = doc.querySelector('.rel-more button');
    if (!b) break;
    b.click();
    await wait(80);
  }

  //then
  const asked = calls.filter((c) => c.startsWith('/api/deploys')).map((c) => +c.split('=')[1]);
  assert.deepStrictEqual([...new Set(asked)], [365, 730, 1460, 2920, 3650], 'doubles up to the 3650-day API cap and never past it: ' + asked);
  assert.ok(!doc.querySelector('.rel-more button') && /No older deploys recorded/.test(doc.querySelector('.rel-more').textContent), 'button gone at the cap');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS Show older doubles the window up to the 3650-day cap, then says no older deploys; the tab description is one line with a ? tooltip');
}

async function reportRowsLineByLineAndOngoingRelease() {
  //given
  const busy = {svc: 'orders-service', ns: 'payments', fromImage: '3c3c3c3c', toImage: '4d4d4d4d', at: at('2026-10-13T05:12:00Z'), flags: ['restarts=3', 'oom'],
    note: "05:11-05:13 APPLICATION FAILED TO START, bind 'encryption' (missing config; old pod); healthy since 05:14; ", metrics: 'warn 14 (32/17), req 1013 (1047/983), out-err 9 (20/6)'};
  const calm = {svc: 'checkout-service', ns: 'shop', fromImage: '1a1a1a1a', toImage: '2b2b2b2b', at: at('2026-10-13T05:08:00Z'), flags: ['restarts=1'], note: 'one note', metrics: 'no metrics: reports is not in prod Prometheus'};
  const report = Object.assign({}, REPORTS[0], {rows: [busy, calm]});
  const now = Date.now();
  const changers = {names: ['Ken', 'Bruno', 'Dora'], commits: 7, compare: 'https://gitlab.com/acme/identity/auth-service/-/compare/5e5e5e5e...6f6f6f6f', title: 'Merge OPS-119', date: '2026-10-14', sha: '6f6f6f6f', url: 'https://gitlab.com/acme/identity/auth-service/-/commit/6f6f6f6f'};
  const fresh = [dep('n2', 'identity', 'auth-service', '5e5e5e5e', '6f6f6f6f', new Date(now - 19 * 60000).toISOString(), {tickets: ['OPS-119'], imageBy: changers}),
    dep('n1', 'identity', 'api-gateway-public', 'c3d4e5f6', '2d3c4b5a', new Date(now - 22 * 60000).toISOString())];
  const state = Object.assign(stateWith(), {summary: {level: 'info', headline: 'All quiet; release running since 05:07: 2 services so far', points: [], at: iso(1)}});
  const {dom, doc, errors} = await boot(state, DEPLOYS.concat(fresh), [report].concat(REPORTS.slice(1)));

  //then
  const rows = [...doc.querySelectorAll('#rep-r1 .drep-body tbody tr')];
  const cell = (tr, i) => tr.querySelectorAll('td')[i];
  const parts = (td) => [...td.querySelectorAll(':scope > .cell-lines > *')].map((x) => x.textContent);
  assert.deepStrictEqual(parts(cell(rows[0], 3)), ['restarts=3', 'oom'], 'two flags stack one per line');
  assert.deepStrictEqual(parts(cell(rows[0], 4)), ["05:11-05:13 APPLICATION FAILED TO START, bind 'encryption' (missing config; old pod)", 'healthy since 05:14'], 'note parts split on ; outside brackets, the empty tail dropped');
  assert.deepStrictEqual(parts(cell(rows[0], 5)), ['warn 14 (32/17)', 'req 1013 (1047/983)', 'out-err 9 (20/6)'], 'one signal per line');
  assert.ok(!cell(rows[1], 3).querySelector('.cell-lines') && cell(rows[1], 3).textContent === 'restarts=1', 'a single flag stays a plain label');
  assert.ok(!cell(rows[1], 4).querySelector('.cell-lines') && !cell(rows[1], 5).querySelector('.cell-lines'), 'single note and signal stay plain');
  const flexCells = [...doc.styleSheets].flatMap((sh) => [...sh.cssRules]).filter((r) => /td\.flags/.test(r.selectorText || '') && r.style.display);
  assert.deepStrictEqual(flexCells.map((r) => r.selectorText), [], 'no rule turns the flags td into a flex box (that broke the row borders)');

  const reps = [...doc.querySelectorAll('#reps > details.drep')];
  assert.deepStrictEqual(reps.map((d) => d.id), ['ongoing-n1', 'rep-r1', 'rep-r2', 'rep-r3'], 'ongoing release on top of the reports');
  assert.strictEqual(doc.getElementById('count-reps').textContent, '3', 'tab count stays the number of reports');
  const live = reps[0];
  assert.ok(live.classList.contains('ongoing'), 'ongoing card');
  assert.deepStrictEqual(reps.map((d) => d.open), [false, false, false, false], 'the ongoing card folds on load like everything else');
  assert.strictEqual(live.querySelector(':scope > summary .st').textContent, 'Deploying', 'last deploy under 20 min ago');
  assert.match(live.querySelector(':scope > summary .muted').textContent, /^2 services recorded so far, last \d\d:\d\d \u00b7 the report follows once nothing has deployed for 20 min$/);
  assert.deepStrictEqual([...live.querySelectorAll('.rel-deps tbody tr b')].map((b) => b.textContent), ['api-gateway-public', 'auth-service'], 'deploys so far, same table as the release');
  assert.strictEqual(live.querySelector('.rel-deps .ikeys a').textContent, 'OPS-119');
  const who = live.querySelectorAll('.rel-deps tbody tr')[1].querySelectorAll('td')[3];
  assert.strictEqual(who.textContent, 'Ken, Bruno, Dora', 'every changer of the range, not only the merge author');
  assert.strictEqual(who.querySelector('a').href, changers.compare, 'names link to the compare view of the range');
  assert.match(who.querySelector('a').title, /^7 commits since 5e5e5e5e \u00b7 Merge OPS-119 \u00b7 2026-10-14$/);
  const tab = doc.getElementById('tab-reps');
  assert.ok(tab.classList.contains('hot') && tab.classList.contains('k-info') && tab.title === 'Release deploying now', 'tab coloured while deploying: ' + tab.className);
  assert.strictEqual(doc.getElementById('tab-rels').className, 'tab k-act hot', 'Releases takes its colour from the state\'s 24 h deploys, not from the ongoing release');
  const brief = [...doc.querySelectorAll('#reco .brief-when [data-reps]')];
  assert.deepStrictEqual(brief.map((b) => b.textContent), ['Deploy reports \u203a'], 'the summary keeps one link to the running release');

  //when
  const pageDate = dom.window.Date, settled = Date.parse(fresh[0].at) + 20 * 60000;
  dom.window.Date = frozenDate(pageDate, settled - 5000);
  doc.getElementById('tab-reps').click();
  await wait(100);

  //then
  assert.strictEqual(doc.querySelector('#reps > details.ongoing > summary .st').textContent, 'Deploying', 'still deploying 5 s before 20 min have passed since the last deploy');
  assert.strictEqual(tab.title, 'Release deploying now');

  //when
  dom.window.Date = frozenDate(pageDate, settled + 5000);
  doc.getElementById('tab-reps').click();
  await wait(100);

  //then
  const pending = doc.querySelector('#reps > details.ongoing');
  assert.strictEqual(pending.querySelector(':scope > summary .st').textContent, 'Report pending', 'flips 5 s after 20 min pass without data changes');
  assert.strictEqual(tab.title, 'Release deployed, post-deploy report pending', 'tab tooltip follows the flip');
  assert.match(pending.querySelector(':scope > summary .muted').textContent, /^2 services, last \d\d:\d\d \u00b7 waiting for the post-deploy report$/);

  //when
  pending.open = false;
  doc.getElementById('tab-anoms').click();
  brief[0].click();
  await wait(100);

  //then
  assert.strictEqual(doc.getElementById('panel-reps').hidden, false, 'summary link opens Deploy reports');
  assert.strictEqual(dom.window.__scrolledTo, 'ongoing-n1', 'on the ongoing card');
  assert.ok(doc.getElementById('ongoing-n1').open, 'and opens it');

  //when
  doc.getElementById('ongoing-n1').querySelector(':scope > summary .xlink').click();
  await wait(50);

  //then
  assert.strictEqual(doc.getElementById('panel-rels').hidden, false, 'Release: link opens the Releases tab');
  assert.ok(doc.getElementById('rel-n1').open, 'on the ongoing release');
  assert.ok(doc.getElementById('ongoing-n1').open, 'the link does not toggle the ongoing card');
  assert.deepStrictEqual(errors, []);
  dom.window.close();

  //given
  const covered = Object.assign({}, REPORTS[1], {id: 'r9', createdAt: new Date(now).toISOString(), rows: [{svc: 'auth-service', toImage: '6f6f6f6f', at: fresh[0].at, flags: []}]});
  const done = await boot(stateWith(), DEPLOYS.concat(fresh), [covered].concat(REPORTS));

  //then
  assert.ok(!done.doc.querySelector('#reps .ongoing'), 'gone once a report covers the release');
  assert.deepStrictEqual([...done.doc.querySelectorAll('#reps > details.drep')].map((d) => [d.id, d.open]).slice(0, 2), [['rep-r9', false], ['rep-r1', false]], 'no report opens by itself');
  const doneTab = done.doc.getElementById('tab-reps');
  assert.ok(doneTab.classList.contains('hot') && doneTab.classList.contains('k-ok') && /^New deploy report \d\d:\d\d \u00b7 clean$/.test(doneTab.title), 'a fresh clean report colours the tab green: ' + doneTab.className + ' ' + doneTab.title);
  done.dom.window.close();

  //given
  const old = await boot(stateWith(), DEPLOYS.concat([dep('o1', 'identity', 'auth-service', 'a', 'b', new Date(now - 181 * 60000).toISOString())]), REPORTS);

  //then
  assert.ok(!old.doc.querySelector('#reps .ongoing'), 'no ongoing card 3 h after the last deploy');
  assert.strictEqual(old.doc.getElementById('tab-reps').className, 'tab k-ok hot', 'a deploy from the last 24 h keeps the tab green');
  assert.strictEqual(old.doc.getElementById('tab-reps').title, 'A deploy occurred in the last 24 h');
  old.dom.window.close();

  //given
  const tool = await boot(stateWith(), DEPLOYS.concat([dep('t1', 'platform', 'release-board', 'a', 'b', new Date(now - 30 * 60000).toISOString(), {ignored: true})]), REPORTS);

  //then
  assert.ok(!tool.doc.querySelector('#reps .ongoing'), 'a deploy of a service on the check ignore list (release-board, tracking-service) shows no Deploying / Report pending card');
  assert.ok(tool.doc.querySelector('#rels .tag, #rels') && /release-board/.test(tool.doc.getElementById('rels').textContent), 'the Releases tab still lists it');
  tool.dom.window.close();

  //given
  const cold = await boot(stateWith(), DEPLOYS, REPORTS);

  //then
  assert.strictEqual(cold.doc.getElementById('tab-reps').className, 'tab k-info', 'no colour without an ongoing deploy, a report from the last 2 h or a deploy from the last 24 h');
  cold.dom.window.close();
  console.log('PASS report rows: flags, ; note parts and signals one per line, single values plain, no flex td; the newest release without a report shows on top of Deploy reports as Deploying, then Report pending after 20 min, links to its release, and disappears once a report covers it or 3 h pass');
}

async function otherClusterDeploysCarryTheMarker() {
  //given
  const rpt = dep('d10', 'reports', 'reports-service', '4b5a6978', 'b2c3d4e5', iso(30), {tickets: ['OPS-104'], releases: ['OPS 15.10.2026 Release']});
  const cart = dep('d11', 'shop', 'checkout-service', '2b2b2b2b', '9c0ffee1', iso(25));
  const report = {id: 'r9', createdAt: iso(5), summary: '2 deploys: all clean', windowFrom: iso(30),
    rows: [{svc: 'reports-service', ns: 'reports', fromImage: '4b5a6978', toImage: 'b2c3d4e5', at: iso(30), flags: []},
      {svc: 'checkout-service', ns: 'shop', fromImage: '2b2b2b2b', toImage: '9c0ffee1', at: iso(25), flags: []}]};
  const withEast = Object.assign(stateWith(), {
    services: [{ns: 'reports', svc: 'reports-service', kind: 'app', cluster: 'east', status: 'ok'}],
    deploys: [{ns: 'reports', svc: 'reports-service', old: '4b5a6978', new: 'b2c3d4e5', at: iso(30), cluster: 'east'}, {ns: 'shop', svc: 'checkout-service', old: '2b2b2b2b', new: '9c0ffee1', at: iso(25)}],
  });
  const east = await boot(withEast, [cart, rpt], [report]);
  const before = await boot(stateWith(), [cart, rpt], [report]);
  const names = (doc, sel) => [...doc.querySelectorAll(sel + ' tbody tr b')].map((b) => b.textContent);

  //then
  assert.deepStrictEqual(names(east.doc, '#rels > details.rel:first-of-type .rel-deps'), ['reports-service (east)', 'checkout-service'], 'release rows mark the other cluster\'s deploy only');
  assert.deepStrictEqual(names(east.doc, '#rep-r9'), ['reports-service (east)', 'checkout-service'], 'report rows mark it too');
  assert.match(east.doc.getElementById('tab-rels').title, /reports-service \(east\), checkout-service/, 'the Releases tab tooltip of the 24 h deploys names it with the marker');
  assert.match(east.doc.getElementById('rels-recent').textContent, /reports-service \(east\), checkout-service/, 'so does the 24 h line above the releases');
  assert.deepStrictEqual(names(before.doc, '#rels > details.rel:first-of-type .rel-deps'), ['reports-service', 'checkout-service'], 'a state without rows of another cluster marks nothing');
  for (const x of [east, before]) assert.ok(!x.errors.length, x.errors.join(' | '));
  [east, before].forEach((x) => x.dom.window.close());
  console.log('PASS deploys of another cluster (namespaces of the services or 24 h deploys the check tagged with a cluster) read "<svc> (<cluster>)" in Releases, Deploy reports and the 24 h deploys (Releases tab tooltip and line); others and cluster-less states stay plain');
}

const OPERATOR = {loginEnabled: true, user: 'op@example.com', operator: true, slack: true, slackChannel: '#ops'};
const GUEST = {loginEnabled: true, user: null, operator: false, slack: true, slackChannel: '#ops'};

async function bootLinked(o) {
  const errors = [], calls = [], writes = [], copied = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = o.state || stateWith();
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state).replace(/<\//g, '<\\/'));
  const answer = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
  const param = (path, k) => +new URL(path, 'https://x/').searchParams.get(k);
  const dom = new JSDOM(html, {
    url: 'https://skopax.dev.example.com/' + (o.hash || ''), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      if (o.i18n) { w.SKOPAX_I18N = o.i18n; w.document.cookie = 'skopax_lang=' + Object.keys(o.i18n)[0] + '; path=/'; }
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () { w.__scrolledTo = this.id; };
      Object.defineProperty(w.navigator, 'clipboard', {configurable: true, value: {writeText: async (t) => { copied.push(t); }}});
      w.fetch = async (p, opts) => {
        const path = String(p), method = (opts && opts.method) || 'GET';
        calls.push(path);
        if (method !== 'GET') {
          const body = opts.body ? JSON.parse(opts.body) : undefined;
          writes.push([method, path, body]);
          const out = o.onWrite ? o.onWrite(method, path, body) : {};
          if (out && out.failStatus) return {ok: false, status: out.failStatus, json: async () => ({detail: out.detail}), text: async () => ''};
          return answer(out);
        }
        if (path.startsWith('/api/actions')) return answer(typeof o.actions === 'function' ? o.actions() : o.actions || []);
        if (path.startsWith('/api/me')) return answer(o.me || {loginEnabled: false, jiraUrl: 'https://jira.example.com', task: {projects: ['OPS']}});
        if (path.startsWith('/api/rev')) return answer({rev: state.rev});
        if (path.startsWith('/api/deploys')) return answer(typeof o.deploys === 'function' ? o.deploys(param(path, 'days')) : o.deploys);
        if (path.startsWith('/api/deploy-reports')) return answer(typeof o.reports === 'function' ? o.reports(param(path, 'limit')) : o.reports);
        return answer([]);
      };
    },
  });
  await wait(300);
  return {dom, w: dom.window, doc: dom.window.document, errors, calls, writes, copied};
}

async function copyLinkOnReportsAndReleases() {
  //given
  const now = Date.now();
  const fresh = [dep('n2', 'identity', 'auth-service', '5e5e5e5e', '6f6f6f6f', new Date(now - 5 * 60000).toISOString()), dep('n1', 'identity', 'api-gateway-public', 'c3d4e5f6', '2d3c4b5a', new Date(now - 9 * 60000).toISOString())];
  const {w, doc, errors, copied, dom} = await bootLinked({me: GUEST, deploys: DEPLOYS.concat(fresh), reports: REPORTS});
  const copyOf = (id) => doc.getElementById(id).querySelector(':scope > summary .copy-link');

  //then
  for (const id of ['rep-r1', 'rep-r2', 'rep-r3', 'rel-n1', 'rel-d5', 'rel-d9', 'ongoing-n1']) {
    const b = copyOf(id);
    assert.ok(b, 'Copy link in the summary row of ' + id);
    assert.strictEqual(b.textContent, 'Copy link');
    assert.ok(b.querySelector('svg'), 'with the link icon');
    assert.ok(!b.hasAttribute('aria-disabled'), 'guests can copy links too: ' + id);
  }
  assert.ok(doc.querySelector('#rep-r1 .rep-acts .slack-btn').getAttribute('aria-disabled') === 'true', 'the report Slack button is locked for guests');

  //when
  const rep = doc.getElementById('rep-r2'), rel = doc.getElementById('rel-d5'), live = doc.getElementById('ongoing-n1');
  const before = [rep.open, rel.open, live.open];
  const hashBefore = w.location.hash;
  copyOf('rep-r2').click();
  copyOf('rel-d5').click();
  copyOf('ongoing-n1').click();
  await wait(50);

  //then
  assert.deepStrictEqual(copied, ['https://skopax.dev.example.com/#r=r2', 'https://skopax.dev.example.com/#rel=d5', 'https://skopax.dev.example.com/#rel=n1'], 'report link, release link, and the ongoing card links to its release');
  assert.deepStrictEqual([copyOf('rep-r2').textContent, copyOf('rel-d5').textContent], ['Copied', 'Copied'], 'same Copied feedback as the anomaly cards');
  assert.deepStrictEqual([rep.open, rel.open, live.open], before, 'Copy link does not toggle its card');
  assert.strictEqual(w.location.hash, hashBefore, 'copying does not navigate');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS Copy link on every report, release and ongoing card (summary row, link icon, guests too) copies #r=<id> / #rel=<first deploy id>, says Copied and does not toggle the card');
}

async function linksOpenTheirCard() {
  //given
  const reports = (limit) => limit >= 200 ? REPORTS.concat([{id: 'r-old', createdAt: at('2026-07-01T06:00:00Z'), summary: 'very old check', rows: []}]) : REPORTS;
  const deploys = (days) => days >= 3650 ? DEPLOYS.concat([dep('o1', 'identity', 'auth-service', 'aaaa0001', 'bbbb0001', new Date(Date.now() - 500 * 86400000).toISOString())]) : DEPLOYS;
  const rep = await bootLinked({hash: '#r=r2', deploys, reports});
  const relLoad = await bootLinked({hash: '#rel=d3', deploys, reports});
  const tab = await bootLinked({hash: '#reps', deploys, reports});
  const oldLoad = await bootLinked({hash: '#r=r-old', deploys, reports});

  //then
  assert.ok(oldLoad.doc.getElementById('rep-r-old').open && oldLoad.doc.getElementById('rep-r-old').classList.contains('linked'), 'a fresh load needing the wider list still opens the card');
  assert.ok(!oldLoad.doc.getElementById('link-note'), 'a narrower answer landing meanwhile does not end the lookup with a note');
  assert.deepStrictEqual(oldLoad.errors, []);
  oldLoad.dom.window.close();
  const openOf = (doc, sel) => [...doc.querySelectorAll(sel + ' > details.drep')].filter((d) => d.open).map((d) => d.id);
  assert.strictEqual(rep.doc.getElementById('panel-reps').hidden, false, '#r= on a fresh load opens Deploy reports');
  assert.deepStrictEqual(openOf(rep.doc, '#reps'), ['rep-r2'], 'the linked report is the one open panel, the newest folds');
  assert.ok(rep.doc.getElementById('rep-r2').classList.contains('linked'), 'with the link ring');
  assert.strictEqual(rep.w.__scrolledTo, 'rep-r2');
  assert.strictEqual(rep.w.location.hash, '', 'hash dropped after the jump');
  assert.strictEqual(relLoad.doc.getElementById('panel-rels').hidden, false, '#rel= on a fresh load opens Releases');
  assert.deepStrictEqual(openOf(relLoad.doc, '#rels'), ['rel-d3'], 'the linked release is the one open panel, the newest folds');
  assert.ok(relLoad.doc.getElementById('rel-d3').classList.contains('linked'));
  assert.strictEqual(relLoad.w.location.hash, '');
  assert.strictEqual(tab.doc.getElementById('panel-reps').hidden, false, 'old #reps still opens the tab');
  assert.ok(!tab.doc.querySelector('.drep.linked') && !tab.doc.getElementById('rep-r1').open, 'a tab hash rings nothing and opens no card');
  for (const x of [relLoad, tab]) { assert.deepStrictEqual(x.errors, []); x.dom.window.close(); }

  //when
  const {w, doc, calls, errors, dom} = rep;
  w.location.hash = '#rel=d6';
  await wait(80);

  //then
  assert.strictEqual(doc.getElementById('panel-rels').hidden, false, 'hashchange in an open tab switches to Releases');
  assert.deepStrictEqual(openOf(doc, '#rels'), ['rel-d5'], 'a deploy id inside a release opens that release');
  assert.ok(doc.getElementById('rel-d5').classList.contains('linked') && !doc.getElementById('rep-r2').classList.contains('linked'), 'the ring moves');
  assert.strictEqual(w.location.hash, '');

  //when
  w.location.hash = '#r=r1';
  await wait(80);

  //then
  assert.deepStrictEqual(openOf(doc, '#reps'), ['rep-r1'], 'hashchange opens the report as the one open panel');
  assert.ok(doc.getElementById('rep-r1').classList.contains('linked'));

  //when
  const before = calls.length;
  w.location.hash = '#r=r-old';
  await wait(120);

  //then
  assert.ok(calls.slice(before).includes('/api/deploy-reports?limit=200'), 'a report outside the list is looked up once with the widest list');
  assert.deepStrictEqual(openOf(doc, '#reps'), ['rep-r-old'], 'and opened once found');

  //when
  const b2 = calls.length;
  w.location.hash = '#rel=o1';
  await wait(120);

  //then
  assert.ok(calls.slice(b2).includes('/api/deploys?days=3650'), 'a release outside the window is looked up once in the full history');
  assert.deepStrictEqual(openOf(doc, '#rels'), ['rel-o1']);
  assert.ok(/No older deploys recorded/.test(doc.querySelector('.rel-more').textContent), 'the Releases tab now shows the full history');

  //when
  const b3 = calls.length;
  w.location.hash = '#r=nope';
  await wait(120);
  w.location.hash = '#r=nope';
  await wait(120);

  //then
  assert.strictEqual(calls.slice(b3).filter((c) => c.startsWith('/api/deploy-reports')).length >= 2, true, 'one more look before giving up');
  assert.strictEqual(doc.querySelectorAll('#link-note').length, 1, 'one note for repeated misses');
  assert.match(doc.getElementById('link-note').textContent, /linked deploy report is not on the page any more/);
  assert.ok(doc.getElementById('link-note').nextElementSibling === doc.getElementById('reps'), 'the note sits above the reports');
  assert.ok(!doc.querySelector('.drep.linked'), 'no ring on a miss');
  assert.strictEqual(w.location.hash, '', 'hash dropped on a miss too');

  //when
  w.location.hash = '#rel=zzz';
  await wait(120);

  //then
  assert.match(doc.getElementById('link-note').textContent, /linked release is not in the deploy history/);
  assert.ok(doc.getElementById('link-note').nextElementSibling === doc.getElementById('rels'));
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS #r= and #rel= open the right tab and card as the one open panel with the ring, on a fresh load and on hashchange (a deploy id inside a release works too), widen once to find older ones, else one short note; the hash is dropped; old #reps still opens the tab');
}

async function reportSummaryAndSlackSend() {
  //given
  const draft = {text: 'OPS 13.10 release: 4 services, orders-service restarted 3 times at startup.\nhttps://skopax.dev.example.com/#r=r1', by: 'token', at: iso(10), notAutoSent: 'planted text in the esql answer (ignore previous)'};
  const brief = {level: 'warn', headline: 'Release 05:05\u201305:35: 4 services; orders-service restarted 3 times at startup', points: ['healthy since 05:14', 'SHOP-301 config client live'], at: iso(10)};
  let reps = [Object.assign({}, REPORTS[0], {brief, slackDraft: draft}), Object.assign({}, REPORTS[1], {slackPost: {channel: 'C1', ts: '1.2', at: iso(30), by: 'op@example.com', draft: false}}), REPORTS[2]];
  const onWrite = (method, path, body) => {
    if (method === 'POST' && path === '/api/deploy-reports/r1/slack-posts') { reps = [Object.assign({}, reps[0], {slackPost: {channel: 'C1', ts: '1.3', at: new Date().toISOString(), by: 'op@example.com', draft: true}})].concat(reps.slice(1)); return {id: 'a9', type: 'slack', status: 'done'}; }
    return {};
  };
  const {w, doc, writes, errors, dom} = await bootLinked({me: OPERATOR, deploys: DEPLOYS, reports: () => reps, onWrite});
  const r1 = () => doc.getElementById('rep-r1');
  const sendOf = (id) => doc.querySelector('#' + id + ' .rep-acts .slack-btn');

  //then
  const body = r1().querySelector('.drep-body');
  const top = body.querySelector(':scope > .rep-brief');
  assert.ok(top && top === body.firstElementChild, 'Claude\'s summary opens the report body');
  assert.strictEqual(top.querySelector('.brief-by').firstChild.textContent, 'Report summary', 'labelled like the overview eyebrow');
  assert.match(top.querySelector('.brief-by .q').title, /^Written when this post-deploy check was saved/, 'with its i tooltip');
  assert.strictEqual(top.querySelector('.sev').textContent, 'Warning');
  assert.deepStrictEqual([...top.querySelectorAll('.hl > span')].map((x) => x.textContent), ['Release 05:05\u201305:35: 4 services', 'orders-service restarted 3 times at startup'], 'headline parts one per line');
  assert.deepStrictEqual([...top.querySelectorAll('li')].map((x) => x.textContent), brief.points);
  assert.ok(!body.querySelector('.sdraft, .sdraft-t, h5'), 'no Slack draft block in the report body');
  assert.ok([...body.children].filter((c) => !c.hidden).every((c) => !/4 services, orders-service/.test(c.textContent)), 'the draft text shows nowhere in the body');
  assert.strictEqual(top.nextElementSibling, r1().querySelector('.rep-acts'), 'the action row follows the summary');
  const r2top = doc.querySelector('#rep-r2 .drep-body > .rep-brief');
  assert.ok(r2top && r2top === doc.querySelector('#rep-r2 .drep-body').firstElementChild, 'a report saved without a summary leads with its one-line summary');
  assert.deepStrictEqual([...r2top.querySelectorAll('.hl > span')].map((x) => x.textContent), [REPORTS[1].summary]);
  assert.strictEqual(r2top.querySelector('.brief-by').firstChild.textContent, 'Report summary');
  assert.match(r2top.querySelector('.brief-by .q').title, /saved before report summaries existed/, 'its tooltip says why it is one line');
  assert.strictEqual(r2top.querySelector('.sev').textContent, 'All clear', 'no flagged row: all clear');
  assert.ok(!r2top.querySelector('li'), 'no points');
  assert.strictEqual(r1().querySelector(':scope > summary .slack-rec').textContent, 'Not posted: the run read suspicious text');
  assert.match(r1().querySelector(':scope > summary .slack-rec').title, /^OPS 13\.10 release[\s\S]*\nNot posted: the run read suspicious text \(planted text in the esql answer \(ignore previous\)\); check it and use Send to Slack$/);
  assert.strictEqual(sendOf('rep-r1').textContent, 'Send to Slack');
  assert.ok(!sendOf('rep-r1').hasAttribute('aria-disabled'), 'operators can send');
  const tagOf = () => r1().querySelector('.rep-acts .draft-q');
  assert.strictEqual(sendOf('rep-r1').nextElementSibling, tagOf(), 'the kept text sits as a tag right next to Send to Slack');
  assert.match(tagOf().textContent, /^Slack text \u00b7 Investigator \d\d:\d\d$/, 'the tag says who and when');
  assert.ok(tagOf().title.startsWith(draft.text + '\n\nInvestigator \u00b7 '), 'its tooltip is the text plus who and when: ' + tagOf().title);
  assert.deepStrictEqual([...r1().querySelectorAll('.rep-acts .btn')].map((b) => b.textContent), ['Send to Slack', 'Copy', 'Re-assess'], 'Copy sits in the action row, no Edit, Re-assess closes it');
  assert.ok(!r1().querySelector('.draft-form') && !r1().querySelector('.draft-edit'), 'no draft editing');
  assert.ok(!doc.querySelector('#rep-r2 .draft-q') && !doc.querySelector('#rep-r2 .draft-copy'), 'no draft, no tag or Copy');
  assert.strictEqual(sendOf('rep-r2').textContent, 'Send again');
  assert.match(doc.querySelector('#rep-r2 > summary .slack-rec.sent').textContent, /^Sent to Slack \d\d:\d\d$/, 'a posted report says so in its summary row');
  assert.match(doc.querySelector('#rep-r2 .rep-acts .slack-st').textContent, /^Sent to Slack \d\d:\d\d \u00b7 op$/);
  assert.strictEqual(sendOf('rep-r3').textContent, 'Send to Slack');
  assert.ok(!doc.querySelector('#rep-r3 > summary .slack-rec'), 'no tag without a draft or a post');

  //when
  const wasOpen = r1().open;
  sendOf('rep-r1').click();
  await wait(20);

  //then
  assert.strictEqual(sendOf('rep-r1').textContent, 'Post to #ops?', 'first click arms the confirm');
  assert.ok(sendOf('rep-r1').dataset.confirm, 'armed confirm is marked, so live updates wait');
  assert.deepStrictEqual(writes, [], 'nothing posted on the first click');
  assert.strictEqual(r1().open, wasOpen);

  //when
  sendOf('rep-r1').click();
  await wait(120);

  //then
  assert.deepStrictEqual(writes.map((x) => [x[0], x[1]]), [['POST', '/api/deploy-reports/r1/slack-posts']], 'second click posts the report');
  assert.match(r1().querySelector(':scope > summary .slack-rec.sent').textContent, /^Sent to Slack \d\d:\d\d$/, 'the card refreshes to Sent to Slack');
  assert.strictEqual(sendOf('rep-r1').textContent, 'Send again');
  assert.match(r1().querySelector('.rep-acts .slack-st').textContent, / \u00b7 op \u00b7 investigator text$/);
  assert.deepStrictEqual(errors, []);
  dom.window.close();

  //given
  const guest = await bootLinked({me: GUEST, deploys: DEPLOYS, reports: [Object.assign({}, REPORTS[0], {slackDraft: draft})]});

  //then
  const g1 = guest.doc.getElementById('rep-r1');
  assert.strictEqual(g1.querySelector('.rep-acts .slack-btn').getAttribute('aria-disabled'), 'true', 'nor send');

  //when
  g1.querySelector('.rep-acts .draft-copy').click();
  await wait(20);

  //then
  assert.deepStrictEqual(guest.copied, [draft.text], 'guests can copy the draft text');
  assert.strictEqual(g1.querySelector('.rep-acts .draft-copy').getAttribute('aria-disabled'), null, 'Copy is not locked for guests');
  assert.strictEqual(g1.querySelector('.rep-brief .sev').textContent, 'Warning', 'a flagged report without a summary leads with Warning and its one-line summary');
  assert.strictEqual(g1.querySelector('.rep-brief .hl').textContent, REPORTS[0].summary);
  assert.deepStrictEqual(guest.errors, []);
  guest.dom.window.close();
  console.log('PASS a report leads with Claude\'s summary (or its one-line summary when saved without one), no text block in the body: a post kept for a person is a tag next to Send to Slack (text + who/when as tooltip) with Copy beside it, no Edit; Send confirms, posts and refreshes to Sent to Slack; guests can only copy');
}

async function reportAutoSendShowsTheTimeAndHoldStopsIt() {
  //given
  const at = iso(2);
  const draft = {text: 'OPS 15.10 release: 14 services deployed, no new errors.\nhttps://skopax.example.com/#r=r1', by: 'token', at};
  const auto = {at: iso(1), draftAt: at, draftHash: '0a1b2c3d4e5f6a7b'};
  const reps = [Object.assign({}, REPORTS[0], {slackDraft: draft, autoSend: auto}), REPORTS[1], REPORTS[2]];

  //when
  const {doc, errors, dom} = await bootLinked({me: OPERATOR, deploys: DEPLOYS, reports: () => reps, onWrite: () => ({})});
  const r1 = doc.getElementById('rep-r1');

  //then
  assert.ok(!r1.querySelector(':scope > summary .slack-rec'), 'a report post on its way has no Slack recommended or Auto-send tag');
  assert.ok(!r1.querySelector('.hold-btn'), 'no Hold');
  assert.deepStrictEqual([...r1.querySelectorAll('.rep-acts .btn')].map((b) => b.textContent), ['Send to Slack', 'Copy', 'Re-assess']);
  assert.ok(!/Auto-send|Slack recommended|Hold/.test(r1.textContent));
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a report post on its way shows no Slack recommended or Auto-send tag and no Hold; Send to Slack and Copy stay';
}

async function flagPillsLinkToTheirCardOrLogs() {
  //given
  const anomaly = (key, svc, ns, label, extra) => Object.assign({key, svc, ns, label, sev: 'warn', status: 'open', src: 'prom', now: 9, d1: 1, d7: 1, unit: '', firstSeen: iso(40), lastSeen: iso(5), short: 'x'}, extra || {});
  const state = Object.assign(stateWith(), {anomalies: [
    anomaly('out-err|web/web-bff', 'web-bff', 'web', 'failed outbound calls'),
    anomaly('log|orders-service|ERROR|Could not bind encryption', 'orders-service', '-', 'ERROR pattern', {status: 'cleared'}),
    anomaly('5xx|shop/old-service', 'old-service', 'shop', '5xx responses', {firstSeen: iso(3 * 24 * 60), lastSeen: iso(3 * 24 * 60 - 30)}),
  ]});
  const rows = [
    {svc: 'web-bff', ns: 'web', toImage: 'a1', at: iso(30), flags: ['out-err up']},
    {svc: 'orders-service', ns: 'payments', toImage: 'b1', at: iso(30), flags: ['err up', 'restarts=3']},
    {svc: 'old-service', ns: 'shop', toImage: 'c1', at: iso(30), flags: ['5xx up']},
  ];
  const report = {id: 'rf', createdAt: iso(10), summary: '3 deploys: 3 flagged', windowFrom: iso(35), windowTo: iso(12), rows};
  const {w, doc, errors, dom} = await bootLinked({state, deploys: [], reports: [report]});
  const pills = (svc) => [...doc.querySelectorAll('#rep-rf tbody tr')].filter((tr) => tr.querySelector('b').textContent === svc)[0].querySelectorAll('td.flags .st');

  //then
  const [outErr] = pills('web-bff');
  assert.strictEqual(outErr.tagName, 'BUTTON', 'a flag with its anomaly card still on the page links to the card');
  assert.strictEqual(outErr.textContent, 'out-err up', 'the label text stays the flag; the \u203a comes from CSS');
  assert.match(outErr.title, /^Open its anomaly card: web-bff \/ failed outbound calls$/);
  const [err, restarts] = pills('orders-service');
  assert.strictEqual(err.tagName, 'BUTTON', 'err up falls back to the service\'s ERROR pattern card');
  assert.match(err.title, /ERROR pattern \(cleared\)$/);
  assert.strictEqual(restarts.tagName, 'SPAN', 'no restarts card and no stored link: a plain flag');
  const [old] = pills('old-service');
  assert.strictEqual(old.tagName, 'SPAN', 'a card from days before the deploy is not this flag\'s card');
  assert.strictEqual(restarts.textContent, 'restarts=3');
  const nowrap = [...doc.styleSheets].flatMap((sh) => [...sh.cssRules]).filter((r) => r.selectorText === '.st' && r.style.getPropertyValue('white-space') === 'nowrap');
  assert.strictEqual(nowrap.length, 1, 'status labels never wrap inside a word pair like "out-err up"');

  //when
  outErr.click();
  await wait(50);

  //then
  assert.strictEqual(doc.getElementById('panel-anoms').hidden, false, 'the card opens on Anomalies');
  const ringed = doc.querySelector('#anoms article.inc.linked');
  assert.ok(ringed && ringed.dataset.key === 'out-err|web/web-bff', 'with the link ring');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS report flag labels never wrap; a flag opens its anomaly card (the service + signal key, or for err/warn the service\'s log pattern card) when one from around the deploy is on the page, else the service\'s ERROR/WARN lines in Kibana for the deploy window');
}

async function reportRowsShowCommitters() {
  //given
  const compare = 'https://gitlab.com/acme/payments/orders-service/-/compare/3c3c3c3c...4d4d4d4d';
  const withCompare = DEPLOYS.map((d) => d.id === 'd7' ? Object.assign({}, d, {imageBy: Object.assign({}, d.imageBy, {commits: 3, compare})}) : d);
  const plain = await boot(stateWith(), DEPLOYS, REPORTS);
  const linked = await boot(stateWith(), withCompare, REPORTS);
  const svcCell = (doc, svc) => [...doc.querySelectorAll('#rep-r1 .drep-body tbody tr')].map((tr) => tr.querySelectorAll('td')[1]).filter((td) => td && td.querySelector('b') && td.querySelector('b').textContent === svc)[0];

  //then
  const span = svcCell(plain.doc, 'orders-service').querySelector('.who-q');
  assert.ok(span && span.tagName === 'SPAN' && span.textContent === '?', 'a ? next to the service when its deploy has committers');
  assert.strictEqual(span.dataset.tip, 'Commits by Erin, Irene', 'the tooltip lists the committers');
  assert.strictEqual(span.previousElementSibling.tagName, 'B', 'the badge sits right after the service name');
  assert.ok(!svcCell(plain.doc, 'checkout-service').querySelector('.who-q'), 'no badge when the deploy has no committers');
  const a = svcCell(linked.doc, 'orders-service').querySelector('a.who-q');
  assert.ok(a && a.href === compare && a.target === '_blank', 'with a compare URL the badge links to the GitLab compare view');
  assert.strictEqual(a.dataset.tip, 'Commits by Erin, Irene \u00b7 3 commits since 3c3c3c3c \u00b7 opens the GitLab compare view');
  assert.strictEqual(a.getAttribute('aria-label'), 'Commits by Erin, Irene \u00b7 3 commits since 3c3c3c3c');
  const rules = [...plain.doc.styleSheets].flatMap((sh) => [...sh.cssRules]).filter((r) => r.style && r.style.display === 'inline-grid').map((r) => r.selectorText).join(' ');
  assert.ok(/\.who-q/.test(rules) && /\.panel-note \.q/.test(rules), 'the badge and a panel-note ? badge get the round ? style: ' + rules);
  for (const x of [plain, linked]) assert.ok(!x.errors.length, x.errors.join(' | '));
  [plain, linked].forEach((x) => x.dom.window.close());
  console.log('PASS a deploy report row shows a ? after the service with its committers (from the matching deploy) as the tooltip, linked to the GitLab compare view when there is one; no committers, no badge; the ? style also covers panel-note badges');
}

async function signalsLinkToTheirSource() {
  //given
  const prom = (ui, expr) => ui + '/query?g0.expr=' + encodeURIComponent(expr) + '&g0.tab=graph&g0.range_input=2h&g0.end_input=' + encodeURIComponent('2026-10-15 06:40:00');
  const reqQ = 'sum(increase(http_server_requests_seconds_count{namespace="payments",job="orders-service"}[1800s]))';
  const restartQ = 'sum(increase(kube_pod_container_status_restarts_total{namespace="payments",pod=~"orders-service-[a-z0-9]+-[a-z0-9]+"}[1800s]))';
  const eastQ = 'sum(increase(http_server_requests_seconds_count{namespace="reports",job="reports-service"}[1800s]))';
  const kib = 'https://logs.example.com/search?service=orders-service&level=ERROR';
  const rows = [
    {svc: 'orders-service', ns: 'payments', toImage: 'b1', at: iso(30), flags: ['restarts=3'], metrics: 'err 12 (3/4), req 1013 (1047/983), hikari-pend 0 (1/0)',
      links: {err: kib, req: prom('https://prometheus.example.com', reqQ), restarts: prom('https://prometheus.example.com', restartQ)}},
    {svc: 'reports-service', ns: 'reports', cluster: 'east', toImage: 'c1', at: iso(30), flags: [], metrics: 'req 50 (40/45)', links: {req: prom('https://prometheus-b.example.com', eastQ)}},
    {svc: 'checkout-service', ns: 'shop', toImage: 'd1', at: iso(30), flags: ['restarts=1'], metrics: 'err 5 (1/1), warn 9 (2/2), req 100 (90/95)'},
  ];
  const report = {id: 'rs', createdAt: iso(10), summary: '3 deploys: 1 flagged', windowFrom: iso(35), windowTo: iso(12), rows};
  const {doc, errors, dom} = await bootLinked({deploys: [], reports: [report]});
  const sig = (svc) => [...doc.querySelectorAll('#rep-rs tbody tr')].filter((tr) => tr.querySelector('b').textContent.startsWith(svc))[0].querySelector('td.sig');
  const parts = (svc) => [...sig(svc).querySelectorAll('.cell-lines > span')];

  //then
  const w = parts('orders-service');
  assert.deepStrictEqual(w.map((x) => x.textContent), ['err 12 (3/4)', 'req 1013 (1047/983)', 'hikari-pend 0 (1/0)'], 'still one signal per line, in a span');
  assert.strictEqual(w[0].querySelector('a').getAttribute('href'), kib, 'err opens its stored link');
  assert.strictEqual(w[0].querySelector('a').title, 'err over the deploy window');
  assert.strictEqual(w[1].querySelector('a').getAttribute('href'), prom('https://prometheus.example.com', reqQ), 'a metric opens its stored Prometheus graph');
  assert.strictEqual(w[1].querySelector('a').target, '_blank');
  assert.strictEqual(w[1].querySelector('a').title, 'req in Prometheus over the deploy window, the query the check ran: ' + reqQ, 'the tooltip names the query');
  assert.ok(!w[2].querySelector('a'), 'a signal without a stored link stays plain text');
  const n = parts('reports-service');
  assert.ok(!n.length && sig('reports-service').querySelector('a').title.startsWith('req in Prometheus over the deploy window'), 'a row of another cluster links to that cluster\'s Prometheus');
  assert.ok(sig('reports-service').querySelector('a').href.startsWith('https://prometheus-b.example.com/query?'));
  const o = parts('checkout-service');
  assert.ok(!o[0].querySelector('a') && !o[1].querySelector('a') && !o[2].querySelector('a'), 'an older report without links leaves every signal plain');
  const restart = [...doc.querySelectorAll('#rep-rs tbody tr')][0].querySelector('td.flags .st');
  assert.strictEqual(restart.tagName, 'A');
  assert.strictEqual(restart.getAttribute('href'), prom('https://prometheus.example.com', restartQ), 'a flag without its card opens its stored graph');
  assert.match(restart.title, /^restarts in Prometheus over the deploy window/);
  const oldFlag = [...doc.querySelectorAll('#rep-rs tbody tr')][2].querySelector('td.flags .st');
  assert.strictEqual(oldFlag.tagName, 'SPAN', 'without a stored link a flag is plain');
  const nowrap = [...doc.styleSheets].flatMap((sh) => [...sh.cssRules]).filter((r) => r.selectorText === '.drep-body .sig .cell-lines > span' && r.style.getPropertyValue('white-space') === 'nowrap');
  assert.strictEqual(nowrap.length, 1, 'signals never wrap');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS every signal links to its source: stored links (the check\'s query in the tooltip), and older reports without links stay plain; a flag without its card opens its stored graph');
}

async function imagePairAndTicketsStayCompact() {
  //given
  const compare = 'https://gitlab.com/acme/identity/auth-service/-/compare/5e5e5e5e...6f6f6f6f';
  const deploys = DEPLOYS.concat([dep('dx', 'identity', 'auth-service', '5e5e5e5e', '6f6f6f6f', '2026-10-13T05:20:00Z', {imageBy: {names: ['Ken'], commits: 7, compare, title: 'Merge OPS-119', sha: '6f6f6f6f', url: 'https://gitlab.com/acme/identity/auth-service/-/commit/6f6f6f6f'}})]);
  const keys = ['OPS-106', 'OPS-107', 'OPS-108', 'OPS-109', 'OPS-110', 'OPS-111', 'OPS-112', 'OPS-113', 'SHOP-301'];
  const reports = [Object.assign({}, REPORTS[0], {rows: REPORTS[0].rows.concat([{svc: 'auth-service', ns: 'identity', fromImage: '5e5e5e5e', toImage: '6f6f6f6f', at: at('2026-10-13T05:21:00Z'), flags: [], issues: keys}])})];
  const {doc, errors, dom} = await bootLinked({deploys, reports});
  const relRow = (svc) => [...doc.querySelectorAll('.rel-deps tbody tr')].filter((tr) => tr.querySelector('b').textContent === svc)[0];
  const repRow = (svc) => [...doc.querySelectorAll('#rep-r1 .drep-body > .box tbody tr')].filter((tr) => tr.querySelector('b').textContent === svc)[0];

  //then
  const orders = relRow('orders-service').querySelector('.img a.cmp');
  assert.strictEqual(orders.href, 'https://gitlab.com/acme/payments/orders-service/-/compare/3c3c3c3c...4d4d4d4d', 'without a stored compare URL it is built from the commit URL');
  assert.strictEqual(orders.title, 'Compare 3c3c3c3c \u2192 4d4d4d4d on GitLab \u00b7 OPS-115 bundle calc');
  assert.strictEqual(orders.target, '_blank');
  const icon = orders.querySelector('svg');
  assert.ok(icon && icon.getAttribute('fill') === 'currentColor' && icon.getAttribute('aria-hidden') === 'true', 'with a GitLab icon in the text colour');
  assert.strictEqual(orders.textContent, '3c3c3c3c \u2192 4d4d4d4d');
  const auth = relRow('auth-service').querySelector('.img a.cmp');
  assert.strictEqual(auth.href, compare, 'a stored compare URL wins');
  assert.strictEqual(auth.title, 'Compare 5e5e5e5e \u2192 6f6f6f6f on GitLab \u00b7 7 commits since 5e5e5e5e \u00b7 Merge OPS-119', 'the commit count when known');
  for (const svc of ['streaming-kafka-exporter', 'checkout-service', 'api-gateway-public']) {
    const cell = relRow(svc).querySelector('.img');
    assert.ok(!cell.querySelector('a') && !cell.querySelector('svg'), svc + ': a non-git tag or no repo stays plain text');
  }
  assert.strictEqual(repRow('orders-service').querySelector('.img a.cmp').href, 'https://gitlab.com/acme/payments/orders-service/-/compare/3c3c3c3c...4d4d4d4d', 'report rows link the same way');
  assert.strictEqual(repRow('auth-service').querySelector('.img a.cmp').title, 'Compare 5e5e5e5e \u2192 6f6f6f6f on GitLab \u00b7 7 commits since 5e5e5e5e \u00b7 Merge OPS-119');
  assert.ok(!repRow('checkout-service').querySelector('.img a'), 'a report row without commit data stays plain');
  const ik = repRow('auth-service').querySelector('.ikeys');
  const shown = () => [...ik.querySelectorAll('a')].filter((a) => !a.hidden).map((a) => a.textContent);
  assert.deepStrictEqual(shown(), keys.slice(0, 6), 'the first six tickets show');
  const more = ik.querySelector('.more');
  assert.strictEqual(more.textContent, '+3 more');
  assert.strictEqual(more.title, 'OPS-112, OPS-113, SHOP-301', 'the rest in the tooltip');
  const capped = [...doc.styleSheets].flatMap((sh) => [...sh.cssRules]).filter((r) => r.selectorText === '.drep-body .ikeys' && r.style.getPropertyValue('max-width'));
  assert.strictEqual(capped.length, 1, 'the ticket list has a capped width so Note and Signals keep their room');
  assert.ok(repRow('auth-service').querySelector('td.svc-cell'), 'the service cell is marked for its own width rule');

  //when
  const wasOpen = doc.getElementById('rep-r1').open;
  more.click();

  //then
  assert.deepStrictEqual(shown(), keys, '+N more shows the rest');
  assert.ok(!ik.querySelector('.more'), 'and goes away');
  assert.strictEqual(doc.getElementById('rep-r1').open, wasOpen, 'without toggling the card');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS the image pair links to the GitLab compare view (stored URL or built from the commit URL) with a GitLab icon and a Compare \u2026 \u00b7 N commits since \u2026 tooltip; tags and missing repos stay plain; a long ticket list shows six plus +N more (rest in the tooltip, click shows them)');
}

async function tabsAreLinks() {
  //given
  const svcState = Object.assign(stateWith(), {services: [
    {ns: 'web', svc: 'api-gateway-public', kind: 'gateway', req: 3100, status: 'ok'},
    {ns: 'commerce', svc: 'orders-service', kind: 'app', req: 900, status: 'ok'}]});
  const {dom, doc, w, errors} = await bootLinked({deploys: DEPLOYS, reports: REPORTS});
  const tabs = [...doc.querySelectorAll('#snav .s-grp:not([data-grp="main"]) a.tab[data-tab]')];
  const selected = () => doc.querySelector('a.tab[data-tab][aria-current="page"]').dataset.tab;

  //then
  assert.ok(tabs.length === 14 && tabs.every((t) => t.tagName === 'A'), 'every row is a real link, so copy-link and middle-click work');
  assert.deepStrictEqual(tabs.map((t) => t.getAttribute('href')), tabs.map((t) => '#t=' + t.dataset.tab), 'each one points at its own #t= path');
  assert.ok(tabs.every((t) => !t.hasAttribute('role') && !t.hasAttribute('type')), 'they are plain links: no tab role and no button type');
  assert.strictEqual(w.location.hash, '#t=anoms', 'a load without a hash writes the open tab in the address bar');

  //when
  const entries = w.history.length;
  doc.getElementById('tab-lat').click();

  //then
  assert.strictEqual(selected(), 'lat', 'a plain click switches the tab');
  assert.strictEqual(w.location.hash, '#t=lat', 'and writes the tab link in the address bar');

  //when
  doc.getElementById('tab-svcs').click();
  doc.getElementById('tab-rels').click();
  await wait(30);

  //then
  assert.strictEqual(w.location.hash, '#t=rels', 'across groups too');
  assert.strictEqual(w.history.length, entries, 'clicks replace the entry, so tab hopping does not flood the history');

  //when
  const space = new w.KeyboardEvent('keydown', {bubbles: true, cancelable: true, key: ' '});
  doc.getElementById('tab-hist').dispatchEvent(space);

  //then
  assert.ok(space.defaultPrevented && selected() === 'hist', 'Space still opens a tab, as it did while they were buttons');

  //when
  w.location.hash = '#t=acts';
  await wait(60);

  //then
  assert.strictEqual(selected(), 'acts', 'a #t= link pasted into an open page switches the tab');
  assert.strictEqual(w.location.hash, '#t=acts', 'and the link stays in the address bar');

  //when
  const meta = new w.MouseEvent('click', {bubbles: true, cancelable: true, metaKey: true});
  doc.getElementById('tab-svcs').dispatchEvent(meta);
  await wait(60);

  //then
  assert.strictEqual(meta.defaultPrevented, false, 'a modifier click is left to the browser, so open in a new tab works');
  assert.deepStrictEqual(errors, []);
  dom.window.close();

  //given
  const fresh = await bootLinked({hash: '#t=lat', deploys: DEPLOYS, reports: REPORTS});
  const gw = await bootLinked({hash: '#t=svcs/gateway', state: svcState, deploys: DEPLOYS, reports: REPORTS});
  const kept = await bootLinked({hash: '#reps', deploys: DEPLOYS, reports: REPORTS});
  const bad = await bootLinked({hash: '#t=nope', deploys: DEPLOYS, reports: REPORTS});

  //then
  assert.strictEqual(fresh.doc.getElementById('panel-lat').hidden, false, '#t= opens its tab on a fresh load');
  assert.strictEqual(fresh.w.location.hash, '#t=lat', 'and keeps the link');
  assert.strictEqual(gw.doc.getElementById('panel-svcs').hidden, false, '#t=svcs/gateway opens Services');
  assert.strictEqual(gw.doc.querySelector('#svc-chips [aria-pressed="true"]').dataset.f, 'gateway', 'on the Gateways kind chip');
  assert.deepStrictEqual([...gw.doc.querySelectorAll('#svcs tbody tr')].map((r) => r.dataset.svc), ['api-gateway-public'], 'with only the gateway rows');
  assert.strictEqual(kept.doc.getElementById('panel-reps').hidden, false, 'the old bare #reps hash still opens its tab');
  assert.strictEqual(kept.w.location.hash, '#t=reps', 'and the address bar then shows its #t= link');
  assert.strictEqual(bad.doc.querySelector('a.tab[data-tab][aria-current="page"]').dataset.tab, 'anoms', 'an unknown tab falls back to the default one');
  const note = bad.doc.getElementById('link-note');
  assert.ok(note && note.parentElement.id === 'panel-anoms', 'with one short note on that tab');
  assert.strictEqual(note.textContent, 'No \u201cnope\u201d page here: showing Board instead.', 'an unknown link names a page, not a tab');
  for (const x of [fresh, gw, kept, bad]) { assert.deepStrictEqual(x.errors, []); x.dom.window.close(); }
  console.log('PASS every sidebar row is an <a href="#t=\u2026"> that a plain click follows with replaceState (no history entries, Space and modifier clicks unchanged); #t= opens its tab and sub-state (svcs/gateway) on a fresh load and on hashchange, an unknown one falls back to the default with one note, and the old bare #reps opens its tab and then shows as #t=reps');
}

async function tabGroupsCarryTheHealthState() {
  //given
  const state = Object.assign(stateWith(), {
    services: [{svc: 'checkout-service', ns: 'shop', kind: 'app', status: 'ok'}, {svc: 'orders-service', ns: 'payments', kind: 'app', status: 'ok'}],
    anomalies: [{key: 'err|payments/orders-service', svc: 'orders-service', ns: 'payments', label: 'ERROR count', sev: 'crit', status: 'open', src: 'prom', now: 9, d1: 1, d7: 1, firstSeen: iso(20), lastSeen: iso(5)}],
    cloudflare: {checks: [{name: 'shop.example.com', status: 'healthy'}, {name: 'api.example.com', status: 'unhealthy'}], edge: [], certs: []},
  });
  const actions = [{id: 'q1', type: 'dive', key: 'err|payments/orders-service', target: 'err|payments/orders-service', label: 'Investigate orders-service', status: 'requested', requestedAt: iso(1)}];
  const {doc, errors, dom} = await boot(state, DEPLOYS, REPORTS, actions);
  const groups = [...doc.querySelectorAll('#tabs > .tab-grp')];
  const tab = (id) => doc.getElementById('tab-' + id);

  //then
  assert.deepStrictEqual(groups.map((g) => (g.querySelector('.tabs-l') || {}).textContent || ''), ['Anomaly filters'], 'the side menu keeps only the anomaly filters');
  const rows = (grp) => [...doc.querySelectorAll('#snav .s-grp[data-grp="' + grp + '"] a.tab[data-tab]')].map((t) => t.dataset.tab);
  assert.deepStrictEqual(['main', 'observe', 'activity', 'connect', 'more'].map(rows), [['anoms', 'reqs', 'chat', 'inv'], ['svcs', 'info', 'lat', 'rels', 'reps'], ['slack', 'hist', 'acts', 'costs'], ['integrations', 'tools', 'mcp'], ['review', 'docs']], 'the sidebar groups the tabs: the Board group, then Observe (the old Metrics and Delivery), Activity, Connect and More');
  assert.ok(!doc.getElementById('toptabs') && !doc.getElementById('menu-metrics') && !doc.getElementById('tabs-metrics'), 'no top-bar menus any more');
  assert.deepStrictEqual(['hist', 'acts'].map((id) => tab(id).querySelector('.lbl').textContent), ['Events', 'History'], 'the action log is History, the anomaly events are Events');
  assert.ok(groups.every((g) => g.getAttribute('role') === 'presentation') && !doc.querySelector('#tabs .tabs-l[role="tab"]'), 'labels are not tabs');
  const rules = [...doc.styleSheets].flatMap((sh) => [...sh.cssRules]);
  assert.ok(rules.some((r) => r.selectorText === '.s-grp + .s-grp' && r.style.getPropertyValue('margin-top')), 'a visible gap between the sidebar groups');
  assert.ok(rules.some((r) => r.selectorText === '.tabs' && r.style.getPropertyValue('position') === 'sticky' && r.style.getPropertyValue('flex-direction') === 'column'), 'the filter column is a sticky menu, one per line');
  const layout = doc.querySelector('.layout');
  assert.deepStrictEqual([...layout.children].filter((c) => !c.hidden).map((c) => c.tagName + '#' + c.id), ['NAV#tabs', 'MAIN#', 'ASIDE#live'], 'the menu sits left of the main column, the Activity rail right of it');
  assert.ok(['reco', 'queue-strip', 'banner'].concat([...doc.querySelectorAll('#side a.tab[data-tab]')].map((t) => 'panel-' + t.dataset.tab)).every((id) => layout.querySelector('main').contains(doc.getElementById(id))), 'the summary, the queue strip and every panel are in the main column');
  const narrow = rules.filter((r) => r.cssRules && /max-width: 900px/.test(r.conditionText || (r.media && r.media.mediaText) || '')).flatMap((r) => [...r.cssRules]);
  assert.ok(narrow.some((r) => r.selectorText === '.layout' && r.style.getPropertyValue('grid-template-columns') === 'minmax(0px, 1fr)'), 'on a narrow content the main column takes the full width');
  assert.ok(narrow.some((r) => r.selectorText === '.tabs' && r.style.getPropertyValue('position') === 'fixed') && narrow.some((r) => r.selectorText === 'body.nav-open .tabs'), 'and the menu becomes a drawer');
  assert.deepStrictEqual([...doc.querySelector('section.overview').children].map((c) => c.id), ['reco', 'meta'], 'the overview keeps the Skopax summary and the source pills');
  assert.deepStrictEqual([...doc.querySelectorAll('#tab-sub-anoms button')].map((b) => b.dataset.f).slice(0, 5), ['all', 'open', 'crit', 'high', 'warn'], 'All first, then the Open counts right under Open in the Anomalies side sub menu');
  assert.strictEqual(tab('anoms').title, '1 critical', 'the Anomalies tab tooltip counts by severity');
  assert.ok(tab('svcs').classList.contains('hot') && tab('svcs').classList.contains('k-crit'), 'Services is coloured by its worst service: ' + tab('svcs').className);
  assert.strictEqual(doc.getElementById('count-svcs').textContent, '1/2');
  assert.strictEqual(tab('svcs').title, '1 of 2 not healthy: orders-service (Critical)');
  assert.ok(tab('anoms').classList.contains('hot') && tab('anoms').classList.contains('k-crit'), 'open anomalies colour their tab');
  assert.ok(!tab('lat').classList.contains('hot') && !tab('hist').classList.contains('hot'), 'lists without a health state stay plain');
  assert.ok(tab('acts').classList.contains('hot') && doc.getElementById('count-acts').textContent === '1 open', 'History is coloured while an action waits for Claude');
  assert.strictEqual(tab('acts').title, '1 queued, 0 in progress');

  //when
  tab('reps').click();

  //then
  assert.ok(!doc.getElementById('panel-reps').hidden && tab('reps').getAttribute('aria-current') === 'page', 'tabs still switch');
  assert.strictEqual(doc.querySelectorAll('a.tab[data-tab][aria-current="page"]').length, 1);
  assert.ok(doc.querySelector('.layout').classList.contains('no-nav'), 'away from Anomalies the side menu steps aside and the main column takes its room');

  //when
  dom.window.matchMedia = (q) => ({media: q, matches: false});
  tab('acts').click();

  //then
  assert.ok(!doc.getElementById('panel-acts').hidden && doc.getElementById('panel-reps').hidden, 'across groups too');

  //when
  tab('anoms').click();

  //then
  const navBtn = () => doc.getElementById('filter-menu'), backdrop = doc.getElementById('nav-backdrop');
  assert.ok(!doc.querySelector('.layout').classList.contains('no-nav'), 'on Anomalies the side menu with its filters is back');
  assert.strictEqual(doc.getElementById('crumb-page').textContent, 'Board', 'the crumb names the open section');
  assert.ok(!doc.body.classList.contains('nav-open') && navBtn().getAttribute('aria-expanded') === 'false' && backdrop.hidden, 'the drawer starts closed');

  //when
  navBtn().click();

  //then
  assert.ok(doc.body.classList.contains('nav-open') && navBtn().getAttribute('aria-expanded') === 'true' && !backdrop.hidden, 'the Filters button opens the drawer');
  assert.strictEqual(doc.activeElement, doc.querySelector('#tab-sub-anoms button[aria-pressed="true"]'), 'focus lands on the active anomaly filter');

  //when
  doc.querySelector('#tab-sub-anoms button[data-f="open"]').click();

  //then
  assert.ok(!doc.body.classList.contains('nav-open') && backdrop.hidden && navBtn().getAttribute('aria-expanded') === 'false', 'picking a filter closes the drawer');
  assert.strictEqual(doc.activeElement, navBtn(), 'focus goes back to the Filters button');

  //when
  navBtn().click();
  doc.dispatchEvent(new dom.window.KeyboardEvent('keydown', {key: 'Escape'}));

  //then
  assert.ok(!doc.body.classList.contains('nav-open') && backdrop.hidden, 'Escape closes it');

  //when
  navBtn().click();
  backdrop.click();

  //then
  assert.ok(!doc.body.classList.contains('nav-open') && backdrop.hidden, 'so does a click beside it');
  assert.deepStrictEqual(errors, []);
  dom.window.close();

  //given
  const healthy = await boot(Object.assign(stateWith(), {services: [{svc: 'checkout-service', ns: 'shop', kind: 'app', status: 'ok'}], cloudflare: {checks: [{name: 'shop.example.com', status: 'healthy'}], edge: [], certs: []}}), DEPLOYS, REPORTS);

  //then
  const ht = (id) => healthy.doc.getElementById('tab-' + id);
  assert.ok(!ht('svcs').classList.contains('hot') && !ht('anoms').classList.contains('hot') && !ht('acts').classList.contains('hot'), 'a healthy page has plain tabs');
  assert.strictEqual(healthy.doc.getElementById('count-svcs').textContent, '1/1');
  assert.strictEqual(ht('svcs').title, 'all 1 healthy');
  assert.deepStrictEqual(healthy.errors, []);
  healthy.dom.window.close();

  //given
  const reps = await bootLinked({hash: '#reps', deploys: DEPLOYS, reports: REPORTS});
  const linked = await bootLinked({hash: '#r=r2', deploys: DEPLOYS, reports: REPORTS});

  //then
  assert.strictEqual(reps.doc.getElementById('tab-reps').getAttribute('aria-current'), 'page', '#reps still opens Deploy reports');
  assert.strictEqual(linked.doc.getElementById('tab-reps').getAttribute('aria-current'), 'page', '#r= still opens its tab');
  assert.ok(linked.doc.getElementById('rep-r2').open && linked.doc.getElementById('rep-r2').classList.contains('linked'), 'and its report');
  for (const x of [reps, linked]) { assert.deepStrictEqual(x.errors, []); x.dom.window.close(); }
  console.log('PASS tabs sit in the sidebar groups (Board, Observe, Activity, Connect, More) with a gap between them; Actions reads History, the old History reads Events; the overview keeps only the Skopax summary (Open counts are Anomalies filter chips); Services / Cloudflare / Anomalies / History carry their health as a coloured count with a tooltip and stay plain when healthy; tabs switch across groups; #reps and #r= still work');
}


async function reassessButtonOnReports() {
  //given
  let queue = [];
  const onWrite = (method, path, body) => {
    if (method === 'POST' && path === '/api/actions') {
      if (body.reportId === 'r2') return {failStatus: 409, detail: 'an assessment of this report is already queued or running'};
      queue = [Object.assign({id: 'a7', status: 'requested', requestedAt: new Date().toISOString()}, body)];
      return queue[0];
    }
    return {};
  };
  const busy = [{id: 'a1', type: 'assess', key: 'report:r2', target: 'report:r2', reportId: 'r2', status: 'processing', requestedAt: iso(2)}];
  const op = await bootLinked({me: OPERATOR, deploys: DEPLOYS, reports: REPORTS, onWrite, actions: () => queue});
  const btnOf = (x, id) => x.doc.querySelector('#' + id + ' .rep-acts .reassess-btn');

  //then
  assert.strictEqual(btnOf(op, 'rep-r1').textContent, 'Re-assess', 'operators see Re-assess on a report');
  assert.ok(!btnOf(op, 'rep-r1').hasAttribute('aria-disabled') && !btnOf(op, 'rep-r1').disabled);
  assert.strictEqual(btnOf(op, 'rep-r1').title, "Run the investigator's assessment of this report again (Opus). Replaces the summary; never posts to Slack again.");

  //when
  btnOf(op, 'rep-r1').click();
  await wait(300);

  //then
  assert.deepStrictEqual(op.writes, [['POST', '/api/actions', {type: 'assess', key: 'report:r1', target: 'report:r1', reportId: 'r1'}]], 'one assess action for the report');
  assert.strictEqual(btnOf(op, 'rep-r1').textContent, 'Re-assessment queued');
  assert.ok(btnOf(op, 'rep-r1').disabled, 'disabled while the assess action is queued');
  assert.ok(!btnOf(op, 'rep-r2').disabled, 'other reports stay available');

  //when
  btnOf(op, 'rep-r2').click();
  await wait(300);

  //then
  assert.strictEqual(btnOf(op, 'rep-r2').textContent, 'an assessment of this report is already queued or running', "the server's reason is shown");
  assert.deepStrictEqual(op.errors, []);
  op.dom.window.close();

  //given
  const open = await bootLinked({me: OPERATOR, deploys: DEPLOYS, reports: REPORTS, actions: busy});

  //then
  assert.ok(btnOf(open, 'rep-r2').disabled && btnOf(open, 'rep-r2').textContent === 'Re-assessing\u2026', 'an assess already running for the report disables its button');
  assert.ok(!btnOf(open, 'rep-r1').disabled, 'and only that report');
  assert.deepStrictEqual(open.errors, []);
  open.dom.window.close();

  //given
  const guest = await bootLinked({me: GUEST, deploys: DEPLOYS, reports: REPORTS});

  //when
  btnOf(guest, 'rep-r1').click();
  await wait(100);

  //then
  assert.strictEqual(btnOf(guest, 'rep-r1').getAttribute('aria-disabled'), 'true', 'guests see it greyed out');
  assert.deepStrictEqual(guest.writes, [], 'and a click queues nothing');
  assert.deepStrictEqual(guest.errors.map((e) => e.split(' (')[0]), ['Not implemented: navigation to another Document'], 'it sends the guest to /login instead');
  guest.dom.window.close();
  console.log('PASS Re-assess on a deploy report: operators queue an assess action (type, key, target, reportId), the button reads Re-assessment queued and stays disabled while an assess for that report is queued or running, a refusal shows the server reason, guests see it greyed out, queue nothing and a click sends them to the login');
}

async function pagesBlockAndScreenshotsOnAReport() {
  //given
  const shot = {id: 'shot1', url: 'https://shop.example.com/products/garden-chairs', finalUrl: 'https://shop.example.com/products/garden-chairs', viewport: 'desktop', width: 1280, height: 800, status: 200, title: 'Garden Chairs', at: iso(9)};
  const pages = {state: 'done', rolloutAt: iso(20), at: iso(12), rows: [
    {url: 'https://shop.example.com/', status: 200, ms: 420, cache: 'HIT', age: 60, varnish: 'hit', title: 'Shop', state: 'ok', flags: []},
    {url: 'https://shop.example.com/products/garden-chairs', status: 503, ms: 3400, cache: 'MISS', age: null, title: 'Service Unavailable', state: 'flagged', flags: ['status 503', 'slow 3.4 s']},
    {url: 'https://shop.example.com/shipping-calculator', status: 200, ms: 300, cache: 'HIT', age: 4000, title: 'Shipping', state: 'stale', flags: [], note: 'cached from before the deploy, not checked'},
    {url: 'https://shop.example.com/api/x', state: 'skipped', flags: [], note: 'refused: any /api/ path is refused: token mints and API calls'}]};
  const reps = [
    Object.assign({}, REPORTS[0], {pages, brief: {level: 'warn', headline: 'h', points: ['p'], at: iso(10), screenshots: [shot]}, slackDraft: {text: 'Deploy check.', by: 'Claude (pod)', at: iso(10), screenshots: [shot]}}),
    Object.assign({}, REPORTS[1], {pages: {state: 'waiting', rolloutAt: iso(2), readyAt: new Date(Date.now() + 3 * 60000).toISOString()}}),
    Object.assign({}, REPORTS[2], {pages: {state: 'done', rolloutAt: iso(70), at: iso(60), error: 'page check failed: RuntimeError: boom'}})];
  const {dom, doc, errors} = await boot(stateWith(), DEPLOYS, reps);
  const box = doc.querySelector('#rep-r1 .pages-box');
  const cells = (tr) => [...tr.children].map((td) => td.textContent);

  //then
  assert.strictEqual(box.querySelector('summary').textContent, 'Pages \u00b7 1 ok \u00b7 1 flagged \u00b7 1 cached from before the deploy');
  assert.ok(!box.open, 'folded on load like the other report sections');
  assert.match(box.querySelector('p').textContent, /^Loaded at \d\d:\d\d as a visitor without cookies, at least 5 min after the rollout at \d\d:\d\d\. A copy older than the rollout/);
  const trs = [...box.querySelectorAll('tbody tr')];
  assert.deepStrictEqual(cells(trs[0]), ['/', '200', '0.4 s', 'cf HIT \u00b7 age 60 s \u00b7 varnish hit', 'Shop', 'ok']);
  assert.strictEqual(trs[0].querySelector('a').getAttribute('href'), 'https://shop.example.com/');
  assert.deepStrictEqual([...trs[1].querySelectorAll('.flags .st.s-warn')].map((x) => x.textContent), ['status 503', 'slow 3.4 s'], 'each flag is a warn pill');
  assert.strictEqual(cells(trs[1])[3], 'cf MISS \u00b7 no age header');
  assert.ok(trs[2].classList.contains('pg-stale'));
  assert.strictEqual(trs[2].querySelector('.flags .st').textContent, 'not checked', 'a copy from before the deploy is neither ok nor flagged');
  assert.strictEqual(trs[2].querySelector('.pg-note').textContent, 'cached from before the deploy', 'the pill says not checked, the note says why');
  assert.deepStrictEqual([trs[3].querySelector('.flags .st').textContent, trs[3].querySelector('.pg-note').textContent], ['not loaded', 'refused: any /api/ path is refused: token mints and API calls']);
  const body = doc.querySelector('#rep-r1 .drep-body');
  const order = [...body.children].map((c) => c.classList.contains('pages-box') ? 'pages' : c.classList.contains('box') ? 'rows' : /^Released issues/.test((c.querySelector('summary') || {}).textContent || '') ? 'issues' : '');
  assert.ok(order.indexOf('rows') < order.indexOf('pages') && order.indexOf('pages') < order.indexOf('issues'), 'Pages sits between the service rows and the released issues: ' + order.join(','));
  assert.match(doc.querySelector('#rep-r1 > summary .s').textContent, / \u00b7 1 page flagged$/, 'the report title counts flagged pages');
  assert.match(doc.querySelector('#rep-r2 .pages-box summary').textContent, /^Pages \u00b7 waiting until \d\d:\d\d$/);
  assert.match(doc.querySelector('#rep-r2 .pages-box p').textContent, /5 min after the rollout at \d\d:\d\d, so Varnish serves the new pages; the assessment starts after that\.$/);
  assert.strictEqual(doc.querySelector('#rep-r3 .pages-box summary').textContent, 'Pages \u00b7 check failed');
  assert.strictEqual(doc.querySelector('#rep-r3 .pages-box p').textContent, 'page check failed: RuntimeError: boom');
  assert.ok(!doc.querySelector('#rep-r2 .pages-box table') && !doc.querySelector('#rep-r3 .pages-box table'));
  const shots = [...doc.querySelectorAll('#rep-r1 .rep-brief .shots a.shot')];
  assert.strictEqual(shots.length, 1, 'the summary and the draft share one shot: shown once');
  assert.strictEqual(shots[0].dataset.shot, 'shot1');
  assert.strictEqual(shots[0].querySelector('img').getAttribute('src'), '/api/screenshots/shot1');
  assert.ok(!doc.querySelector('#rep-r2 .rep-brief .shots'), 'no shots, no strip');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS Pages block on a deploy report: per-page status, time, cache and title, warn pills for flags, "not checked" for a copy from before the deploy, "not loaded" for a refusal, a waiting and a failed line, flagged pages in the title, and the report screenshots under the summary');
}

async function aRefusedReleaseSearchIsNamedOnTheReport() {
  //given
  const note = 'Jira refused to read issues (HTTP 403): the token lacks the Browse projects permission (scope read:jira-work)';
  const reps = [Object.assign({}, REPORTS[0], {issues: [{key: 'OPS-115'}], issuesNote: note}), Object.assign({}, REPORTS[1], {issues: [], issuesNote: note})];
  const {dom, doc, errors} = await boot(stateWith(), DEPLOYS, reps);
  const box = (id) => [...doc.querySelectorAll('#' + id + ' .drep-body > details.rep')].filter((d) => /^Released issues/.test(d.querySelector('summary').textContent))[0];

  //then
  assert.ok(box('rep-r1') && box('rep-r1').textContent.indexOf(note) >= 0, 'the refusal is named next to the keys it could not detail');
  assert.ok(box('rep-r2'), 'a refused search with no keys still shows the box instead of nothing');
  assert.strictEqual(box('rep-r2').querySelector('p.muted:last-of-type').textContent, note);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS a refused Jira release search is named in the released-issues box of a deploy report, even when no ticket key was found');
}

async function aDeployGitlabNeverReadSaysSoAndAHiddenIssueAndAnUnreadReleaseSearchAreNamed() {
  //given
  const hidden = 'Jira did not return OPS-999: it does not exist or the token cannot see it';
  const note = 'Jira refused to read issues (HTTP 403): the token lacks the Browse projects permission (scope read:jira-work)';
  const deploys = DEPLOYS.concat([dep('dx', 'shop', 'cart-service', '7e7e7e7e', '8f8f8f8f', '2026-10-13T05:09:00Z'),
    dep('dy', 'shop', 'basket-service', '7e7e7e7e', '8f8f8f8f', '2026-10-13T05:10:00Z', {tickets: ['OPS-999'], releasesNote: hidden})]);
  const reps = [Object.assign({}, REPORTS[1], {issues: [], issuesNote: note})];
  const {doc, errors, dom} = await bootLinked({deploys, reports: reps});
  const cell = (svc) => [...doc.querySelectorAll('.rel-deps tbody tr')].filter((tr) => tr.querySelector('b').textContent === svc)[0].lastElementChild;
  const box = [...doc.querySelectorAll('.drep-body > details.rep')].filter((d) => /^Released issues/.test(d.querySelector('summary').textContent))[0];

  //then
  assert.strictEqual(cell('cart-service').textContent, 'not read: the code host is off', 'no commit was read, so it never says none');
  assert.ok(cell('basket-service').textContent.endsWith('releases: ' + hidden));
  assert.strictEqual(box.querySelector('summary').textContent, 'Released issues \u00b7 not read', 'a refused search never reads as 0');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS a deploy GitLab never read says not read, a hidden Jira issue is named under its tickets, and a refused release search reads as not read, not 0');
}

async function aRefusedEnrichmentShowsInsteadOfNoTickets() {
  //given
  const refused = 'GitLab refused to read projects (HTTP 403): the token lacks the read_api scope or access to the group';
  const deploys = DEPLOYS.concat([dep('dr', 'shop', 'cart-service', '7e7e7e7e', '8f8f8f8f', '2026-10-13T05:09:00Z', {enriched: {refused}})]);
  const {doc, errors, dom} = await bootLinked({deploys, reports: REPORTS});
  const cell = (svc) => [...doc.querySelectorAll('.rel-deps tbody tr')].filter((tr) => tr.querySelector('b').textContent === svc)[0].lastElementChild;

  //then
  assert.strictEqual(cell('cart-service').textContent, 'not read: ' + refused, 'a refused read names the missing permission instead of "none in the commits", and says it was not read');
  assert.strictEqual(cell('api-gateway-public').textContent, 'no local clone', 'a stored note still shows');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS a deploy whose enrichment GitLab refused shows the refusal in its ticket cell, naming the missing permission, instead of "none in the commits"');
}

async function gtmPublishesShowAsReleaseRows() {
  //given
  const ago = (h) => new Date(Date.now() - h * 3600000).toISOString();
  const gtm = (to, h, name, changes) => ({id: 'gtm-GTM-ABC1234-' + to, ns: 'gtm', svc: 'GTM-ABC1234', at: ago(h), fromImage: 'v' + (to - 1), toImage: 'v' + to, kind: 'gtm', gtm: {name, changes, storedAt: ago(h + 0.1)}});
  const alone = [gtm(134, 1, 'Ads conversion fix', '1 tags changed (Ads conversion)')];
  const mixed = [dep('d1', 'web', 'web-bff', 'aaaa1111', 'bbbb2222', ago(5.5)), gtm(135, 5, 'Meta CAPI', '1 tags added (Meta CAPI)')];

  //when
  const a = await boot(stateWith(), alone, [], []);
  const m = await boot(stateWith(), mixed, [], []);

  //then
  const rel = a.doc.querySelector('#rels details.rel');
  assert.match(rel.querySelector('summary .s').textContent, /^GTM publish \u00b7 /, 'a GTM-only group is named after the publish, not Ad-hoc deploys');
  assert.strictEqual(rel.querySelector('summary .muted').textContent, '1 GTM publish');
  const row = rel.querySelector('tr.gtm-row');
  assert.ok(row && /GTM-ABC1234/.test(row.textContent) && /v133 \u2192 v134/.test(row.textContent) && /Ads conversion fix/.test(row.textContent) && /1 tags changed \(Ads conversion\)/.test(row.textContent), 'the row names the container, versions, name and changes');
  assert.match(row.querySelector('td.when').title, /GTM keeps no publish time or author/);
  assert.ok(!a.doc.querySelector('#reps .ongoing'), 'a GTM publish never waits for a post-deploy report');
  assert.ok(!/hot/.test(a.doc.getElementById('tab-reps').className), 'and does not light the Deploy reports tab');
  const both = m.doc.querySelector('#rels details.rel');
  assert.strictEqual(both.querySelector('summary .muted').textContent, '1 service \u00b7 0 tickets \u00b7 1 GTM publish', 'a publish inside a release joins it and is counted apart');
  assert.strictEqual(both.querySelectorAll('tr.gtm-row').length, 1);
  assert.deepStrictEqual(a.errors.concat(m.errors).filter((e) => !/navigation/.test(e)), []);
  [a, m].forEach((x) => setTimeout(() => x.dom.window.close(), 300));
  console.log('PASS GTM publishes show as release rows, name a GTM-only release and never wait for a deploy report');
}

async function aDeployNotReadSaysYetOnlyWhileGitlabServesAndTheDeployIsInsideTheReadWindow() {
  //given
  const daysAgo = (d) => new Date(Date.now() - d * 86400000).toISOString();
  const deploys = [dep('n1', 'shop', 'cart-service', '7e7e7e7e', '8f8f8f8f', daysAgo(1)), dep('n2', 'shop', 'basket-service', '7e7e7e7e', '8f8f8f8f', daysAgo(10))];
  const on = await bootLinked({me: {loginEnabled: false, gitlab: true, codeHost: 'GitHub'}, deploys, reports: []});
  const unnamed = await bootLinked({me: {loginEnabled: false, gitlab: true}, deploys, reports: []});
  const off = await bootLinked({me: {loginEnabled: false, gitlab: false}, deploys, reports: []});
  const cell = (doc, svc) => [...doc.querySelectorAll('.rel-deps tbody tr')].filter((tr) => tr.querySelector('b').textContent === svc)[0].lastElementChild.textContent;

  //then
  assert.deepStrictEqual([cell(on.doc, 'cart-service'), cell(on.doc, 'basket-service')], ['not read from GitHub yet', 'not read from GitHub'], 'the host the server reports is named, and only a deploy inside the 7-day read window still waits for a read');
  assert.deepStrictEqual([cell(unnamed.doc, 'cart-service'), cell(unnamed.doc, 'basket-service')], ['not read from the code host yet', 'not read from the code host'], 'an unknown host is called the code host');
  assert.deepStrictEqual([cell(off.doc, 'cart-service'), cell(off.doc, 'basket-service')], ['not read: the code host is off', 'not read: the code host is off']);
  assert.deepStrictEqual(on.errors.concat(off.errors, unnamed.errors), []);
  unnamed.dom.window.close();
  on.dom.window.close();
  off.dom.window.close();
  console.log('PASS a deploy GitLab has not read says yet only while GitLab serves and the deploy is inside the read window; otherwise it says why no read comes');
}

async function aTurkishPageTranslatesTheCodeHostNote() {
  //given
  const fs = require('fs'), path = require('path');
  const raw = JSON.parse(fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'locale', 'tr', 'page.json'), 'utf8'));
  const pack = Object.assign({}, raw.page, {'@rules': raw.rules, '@months': raw.months, '@days': raw.days, '@tails': raw.tails, '@th': raw.headers});
  const daysAgo = (d) => new Date(Date.now() - d * 86400000).toISOString();
  const deploys = [dep('n1', 'shop', 'cart-service', '7e7e7e7e', '8f8f8f8f', daysAgo(1)), dep('n2', 'shop', 'basket-service', '7e7e7e7e', '8f8f8f8f', daysAgo(10))];
  const named = await bootLinked({me: {loginEnabled: false, gitlab: true, codeHost: 'GitHub'}, deploys, reports: [], i18n: {tr: pack}});
  const cell = (doc, svc) => [...doc.querySelectorAll('.rel-deps tbody tr')].filter((tr) => tr.querySelector('b').textContent === svc)[0].lastElementChild.textContent;

  //then
  assert.deepStrictEqual([cell(named.doc, 'cart-service'), cell(named.doc, 'basket-service')], ['GitHub \u00fczerinden hen\u00fcz okunmad\u0131', 'GitHub \u00fczerinden okunmad\u0131']);
  assert.deepStrictEqual(named.errors, []);
  named.dom.window.close();
  console.log('PASS a Turkish page translates the code host note, naming the host');
}

async function aRowLogsLinkSitsWithItsSignals() {
  //given
  const logs = 'https://logs.example.com/app/discover#/?service=orders-service&from=2026-10-15T06:00:00Z';
  const evil = 'https://logs.example.com/x"><img src=x onerror="window.__evil=1">';
  const rows = [
    {svc: 'orders-service', ns: 'payments', toImage: 'b1', at: iso(30), flags: [], metrics: 'err 12 (3/4)', links: {err: 'https://logs.example.com/search?level=ERROR', logs}},
    {svc: 'reports-service', ns: 'reports', toImage: 'c1', at: iso(30), flags: [], links: {logs}},
    {svc: 'checkout-service', ns: 'shop', toImage: 'd1', at: iso(30), flags: [], metrics: 'req 100 (90/95)', links: {logs: 'javascript:alert(1)'}},
    {svc: 'cart-service', ns: 'shop', toImage: 'e1', at: iso(30), flags: [], metrics: 'req 7 (6/6)', links: {logs: evil}},
  ];
  const report = {id: 'rl', createdAt: iso(10), summary: '4 deploys: all clean', windowFrom: iso(35), windowTo: iso(12), rows};
  const {doc, w, errors, dom} = await bootLinked({deploys: [], reports: [report]});
  const sig = (svc) => [...doc.querySelectorAll('#rep-rl tbody tr')].filter((tr) => tr.querySelector('b').textContent.startsWith(svc))[0].querySelector('td.sig');
  const logsOf = (svc) => sig(svc).querySelector('a.row-logs');

  //then
  assert.deepStrictEqual([...sig('orders-service').querySelectorAll('.cell-lines > span')].map((x) => x.textContent), ['err 12 (3/4)', 'Logs'], 'the Logs link is one more line after the signals');
  const a = logsOf('orders-service');
  assert.strictEqual(a.getAttribute('href'), logs);
  assert.deepStrictEqual([a.target, a.rel, a.title], ['_blank', 'noopener', 'Logs of orders-service over the deploy window']);
  assert.ok(sig('orders-service').querySelector('.cell-lines > span a').getAttribute('href').startsWith('https://logs.example.com/search'), 'the signal keeps its own link');
  assert.strictEqual(logsOf('reports-service').getAttribute('href'), logs, 'a row without signals still gets its Logs link');
  assert.ok(!logsOf('checkout-service') && sig('checkout-service').textContent === 'req 100 (90/95)', 'a link that is not https is left out');
  assert.strictEqual(logsOf('cart-service').getAttribute('href'), evil, 'a hostile link is escaped into the attribute');
  assert.ok(!doc.querySelector('#rep-rl img') && !w.__evil, 'and never becomes markup');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS a deploy report row shows the Logs link a plug-in gives it as one more line after its signals, https only, escaped');
}

async function aFlagOfASecondInstanceOpensThatInstancesCard() {
  //given
  const anomaly = (key, extra) => Object.assign({key, svc: 'orders-service', ns: 'payments', label: 'failed outbound calls', sev: 'warn', status: 'open', src: 'prom', now: 9, d1: 1, d7: 1, unit: '',
    firstSeen: iso(40), lastSeen: iso(5), short: 'x'}, extra || {});
  const state = Object.assign(stateWith(), {anomalies: [anomaly('out-err|payments/orders-service'), anomaly('out-err|payments/orders-service@east', {cluster: 'east', label: 'failed outbound calls east'})]});
  const rows = [{svc: 'orders-service', ns: 'payments', toImage: 'a1', at: iso(30), flags: ['out-err up']},
    {svc: 'orders-service', ns: 'payments', cluster: 'east', toImage: 'b1', at: iso(30), flags: ['out-err up']}];
  const report = {id: 'rc', createdAt: iso(10), summary: '2 deploys: 2 flagged', windowFrom: iso(35), windowTo: iso(12), rows};
  const {doc, errors, dom} = await bootLinked({state, deploys: [], reports: [report]});

  //then
  const [main, east] = [...doc.querySelectorAll('#rep-rc tbody tr td.flags .st')];
  assert.deepStrictEqual([main.tagName, main.dataset.key], ['BUTTON', 'out-err|payments/orders-service'], 'the primary row opens the primary card');
  assert.deepStrictEqual([east.tagName, east.dataset.key], ['BUTTON', 'out-err|payments/orders-service@east'], 'the row of another instance opens that instance\'s card');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS a flag on a deploy row of a second instance opens that instance\'s card, the primary row the primary card');
}

async function pluginReleasesAreKnownByTheirKind() {
  //given
  const ago = (h) => new Date(Date.now() - h * 3600000).toISOString();
  const other = {id: 'rules-shop-12', ns: 'rules', svc: 'shop', at: ago(1), fromImage: 'r11', toImage: 'r12', kind: 'rules', title: 'Rule set 12'};
  const mixed = [dep('d1', 'web', 'web-bff', 'aaaa1111', 'bbbb2222', ago(5.5)), Object.assign({}, other, {id: 'rules-shop-13', at: ago(5), toImage: 'r13'})];

  //when
  const a = await boot(stateWith(), [other], [], []);
  const m = await boot(stateWith(), mixed, [], []);

  //then
  const rel = a.doc.querySelector('#rels details.rel');
  assert.match(rel.querySelector('summary .s').textContent, /^Plug-in releases \u00b7 /, 'a group of plug-in releases is not named Ad-hoc deploys or GTM publish');
  assert.strictEqual(rel.querySelector('summary .muted').textContent, '1 plug-in release');
  const row = rel.querySelector('tr.kind-row');
  assert.ok(row && /shop/.test(row.textContent) && /rules/.test(row.textContent) && /r11 \u2192 r12/.test(row.textContent) && /Rule set 12/.test(row.textContent), 'the row names the release by its kind and title');
  assert.ok(!rel.querySelector('tr.gtm-row'), 'only kind gtm is drawn as a GTM publish');
  assert.ok(!a.doc.querySelector('#reps .ongoing'), 'a plug-in release never waits for a post-deploy report');
  assert.ok(!/hot/.test(a.doc.getElementById('tab-reps').className), 'and does not light the Deploy reports tab');
  const both = m.doc.querySelector('#rels details.rel');
  assert.strictEqual(both.querySelector('summary .muted').textContent, '1 service \u00b7 0 tickets \u00b7 1 plug-in release');
  assert.deepStrictEqual(a.errors.concat(m.errors).filter((e) => !/navigation/.test(e)), []);
  [a, m].forEach((x) => setTimeout(() => x.dom.window.close(), 300));
  console.log('PASS plug-in releases are known by their kind, never wait for a deploy report and are counted apart');
}

(async () => {
  await releasesAndReportsOnTheirOwnTabs();
  await crossLinksAndStateSurvive();
  await emptyAndFailing();
  await showOlderStopsAtTheCap();
  await reportRowsLineByLineAndOngoingRelease();
  await otherClusterDeploysCarryTheMarker();
  await copyLinkOnReportsAndReleases();
  await linksOpenTheirCard();
  await reportSummaryAndSlackSend();
  await reportAutoSendShowsTheTimeAndHoldStopsIt();
  await reassessButtonOnReports();
  await flagPillsLinkToTheirCardOrLogs();
  await reportRowsShowCommitters();
  await signalsLinkToTheirSource();
  await imagePairAndTicketsStayCompact();
  await tabGroupsCarryTheHealthState();
  await tabsAreLinks();
  await pagesBlockAndScreenshotsOnAReport();
  await gtmPublishesShowAsReleaseRows();
  await aRefusedReleaseSearchIsNamedOnTheReport();
  await aRefusedEnrichmentShowsInsteadOfNoTickets();
  await aDeployGitlabNeverReadSaysSoAndAHiddenIssueAndAnUnreadReleaseSearchAreNamed();
  await aDeployNotReadSaysYetOnlyWhileGitlabServesAndTheDeployIsInsideTheReadWindow();
  await aTurkishPageTranslatesTheCodeHostNote();
  await aRowLogsLinkSitsWithItsSignals();
  await aFlagOfASecondInstanceOpensThatInstancesCard();
  await pluginReleasesAreKnownByTheirKind();
})().catch((e) => { console.error(e); process.exit(1); });
