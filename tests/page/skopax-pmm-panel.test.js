const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const PANEL = fs.readFileSync(path.join(__dirname, '..', '..', 'skopax', 'plugins', 'pmm', 'panel.js'), 'utf8');
const SLOT = '<script type="text/plain" id="plugin-scripts"></script>';
const ICON = '/plugins/pmm/icon.svg';
const PLUGINS = [{id: 'pmm', name: 'PMM (Postgres, MongoDB)', icon: ICON, tab: 'pg', passes: ['pmm', 'qan', 'mongo'], instances: [],
  pages: [{tab: 'pg', label: 'Postgres', group: 'observe', icon: ICON}, {tab: 'mongo', label: 'MongoDB', group: 'observe', icon: ICON}]}];
const UI = 'https://pmm.example.com/graph/d';
const EVIL = '<img src=x onerror="window.__pwned=1">';
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

function pgRow(svc, extra) {
  return Object.assign({svc, up: 1, maxconn: 1000, conn: 50, connD1: 40, connD7: 45, pct: 5, idletx: 0, longtx: 3, lag: null, deadlocks: 0, plugin: 'pmm',
    link: UI + '/postgresql-instance-summary/postgresql-instance-summary?var-service_name=' + encodeURIComponent(svc)}, extra || {});
}

function mongoRow(svc, extra) {
  return Object.assign({svc, node: 'db-node-1', addr: '10.0.0.1:27017', set: 'rs0', up: 1, state: 'SECONDARY', conn: 80, connD1: 70, connD7: 75, lag: 0, ops: 1000, opsD1: 900, opsD7: 950,
    readMs: 0.5, writeMs: 1.25, cmdMs: 0.004, queue: 0, plugin: 'pmm', link: UI + '/mongodb-instance-summary/mongodb-instance-summary?var-service_name=' + encodeURIComponent(svc)}, extra || {});
}

function stateWith(rev, over) {
  return Object.assign({rev, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 14 * 60000).toISOString(), sources: {prom: 'ok', pmm: 'ok', qan: 'ok', mongo: 'ok'},
    summary: {level: 'ok', headline: 'x', points: [], at: iso(1)}, anomalies: [], services: [], history: [], routes: [], deploys: [], pmm: [], qan: [], mongo: []}, over);
}

function filled(rev, conn) {
  return stateWith(rev, {
    anomalies: [{key: 'pmm|pg-1-primary|saturation', status: 'open', sev: 'warn', svc: 'pg-1-primary', label: 'connection saturation', short: 'WARN pg-1-primary connections 800/1000 (80.0%)'},
      {key: 'qan|pg-1-primary|load|q1', status: 'open', sev: 'crit', svc: 'checkout-service', label: 'query DB time (s)', short: 'CRIT checkout-service query DB time'},
      {key: 'mongo|rs0|election', status: 'open', sev: 'warn', svc: 'rs0', label: 'replica set roles changed', short: 'WARN rs0 election'},
      {key: 'pmm|pg-3-down|down', status: 'cleared', sev: 'crit', svc: 'pg-3-down', label: 'Postgres down', short: 'CRIT pg-3-down Postgres down'}],
    pmm: [pgRow('pg-1-primary', {conn, connD1: 300, connD7: 320, pct: 100 * conn / 1000, longtx: 1500, deadlocks: 2}),
      pgRow('pg-2-replica', {cluster: 'east', lag: 300, link: 'http://pmm.internal/graph/d/x'}),
      pgRow('pg-3-down', {up: 0, conn: null, pct: null})],
    qan: [{svc: 'pg-1-primary', db: 'checkout_service', app: 'checkout-service', queryid: 'q1', q: 'UPDATE stock SET count = $1 WHERE code = $2', calls: 150000, t: 2500,
      avgMs: 16.67, peakMs: 500, peakAt: iso(20), peakCalls: 600, callsD1: 140000, tD1: 800, avgD1: 5.71, callsD7: 130000, tD7: 790, avgD7: 6.08, plugin: 'pmm',
      link: UI + '/pmm-qan/pmm-query-analytics?var-service_name=pg-1-primary&filter_by=q1&query_selected=true'}],
    mongo: [mongoRow('mongo-rs0-1', {state: 'PRIMARY', lag: null, conn: 500}), mongoRow('mongo-rs0-2', {lag: 200})],
  });
}

async function boot(serve, tab) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  vc.on('error', (e) => errors.push('console.error ' + e));
  const html = pageHtml(TEMPLATE).replace(SLOT, '<script>\n' + PANEL + '\n</script>').replace('/*__SKOPAX_DATA__*/', JSON.stringify(serve.state).replace(/<\//g, '<\\/'));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=' + tab, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        p = String(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', operator: true, admin: false, teams: [], tags: []});
        if (p.startsWith('/api/server')) return ok({plugins: PLUGINS, tz: 'UTC'});
        if (p.startsWith('/api/admin/settings')) return ok({items: [], rev: 1, refreshSeconds: 15});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: serve.rev});
        if (p.startsWith('/api/state')) return ok(serve.state);
        return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
      };
    },
  });
  await wait(400);
  return {dom, w: dom.window, doc: dom.window.document, errors};
}

function rowsOf(section, name) {
  return [...section.querySelectorAll('table[data-pmm="' + name + '"] tbody tr')];
}

async function filledPanelsShowServersQueriesAndMembersAndRepaint() {
  //given
  const serve = {rev: 5, state: filled(5, 800)};
  const {dom, w, doc, errors} = await boot(serve, 'pg');
  const pg = doc.getElementById('panel-pg');
  const mongo = doc.getElementById('panel-mongo');

  //when
  const servers = rowsOf(pg, 'pg').map((tr) => tr.textContent);
  const tag = pg.querySelector('table[data-pmm="pg"] tbody tr a.tag');
  const links = rowsOf(pg, 'pg').map((tr) => [...tr.querySelectorAll('a.lnk')].map((a) => a.getAttribute('href')));
  const queries = rowsOf(pg, 'qan');
  const members = rowsOf(mongo, 'mongo').map((tr) => tr.textContent);
  const setTags = [...mongo.querySelectorAll('[data-pmm="sets"] a.tag')].map((a) => [a.textContent, a.getAttribute('href')]);
  serve.rev = 6;
  serve.state = filled(6, 900);
  w.document.dispatchEvent(new w.Event('visibilitychange'));
  await wait(300);
  const repainted = rowsOf(pg, 'pg')[0].textContent;

  //then
  assert.strictEqual(servers.length, 3);
  assert.ok(servers[0].includes('pg-1-primary') && servers[0].includes('800') && servers[0].includes('80%') && servers[0].includes('25 min'), servers[0]);
  assert.strictEqual(tag.textContent, 'connection saturation');
  assert.strictEqual(tag.getAttribute('href'), '#a=' + encodeURIComponent('pmm|pg-1-primary|saturation'));
  assert.ok(tag.className.includes('k-warn'));
  assert.ok(servers[1].includes('pg-2-replica') && servers[1].includes('(east)') && servers[1].includes('5 min'), servers[1]);
  assert.ok(servers[2].includes('Down') && servers[2].includes('\u2013'), servers[2]);
  assert.deepStrictEqual(links[0], [UI + '/postgresql-instance-summary/postgresql-instance-summary?var-service_name=pg-1-primary']);
  assert.deepStrictEqual(links[1], [], 'a link that is not https is left out');
  assert.strictEqual(queries.length, 1);
  assert.ok(['checkout-service', 'checkout_service', 'UPDATE stock', '150,000', '17 ms', '500 ms', '5.71 ms'].every((t) => queries[0].textContent.includes(t)), queries[0].textContent);
  assert.strictEqual(queries[0].querySelector('a.tag').getAttribute('href'), '#a=' + encodeURIComponent('qan|pg-1-primary|load|q1'));
  assert.ok(queries[0].querySelector('a.lnk').getAttribute('href').startsWith(UI + '/pmm-qan/'));
  assert.strictEqual(members.length, 2);
  assert.ok(members[0].includes('PRIMARY') && members[0].includes('db-node-1') && members[0].includes('500'), members[0]);
  assert.ok(members[1].includes('SECONDARY') && members[1].includes('200 s') && members[1].includes('1.25 ms'), members[1]);
  assert.deepStrictEqual(setTags, [['replica set roles changed', '#a=' + encodeURIComponent('mongo|rs0|election')]]);
  assert.ok(mongo.querySelector('[data-pmm="sets"]').textContent.includes('rs0'));
  assert.ok(repainted.includes('900') && repainted.includes('90%'), 'a live update repaints the panel: ' + repainted);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the Postgres and MongoDB panels show servers, top queries and members with their open cards, and repaint on a live update';
}

async function emptyPanelsSayWhyThereIsNoData() {
  //given
  const serve = {rev: 3, state: stateWith(3, {sources: {prom: 'ok', pmm: 'error: PMM metrics query HTTP 401', qan: 'ok', 'qan-east': 'error: PMM QAN query HTTP 502', mongo: 'ok \u00b7 no data'}})};

  //when
  const {dom, doc, errors} = await boot(serve, 'mongo');
  const pg = doc.getElementById('panel-pg');
  const mongo = doc.getElementById('panel-mongo');

  //then
  assert.strictEqual(rowsOf(pg, 'pg').length, 1);
  assert.strictEqual(rowsOf(pg, 'pg')[0].textContent, 'PMM did not answer on the last check (error: PMM metrics query HTTP 401).');
  assert.strictEqual(rowsOf(pg, 'qan')[0].textContent, 'Query Analytics did not answer on the last check (east: error: PMM QAN query HTTP 502).');
  assert.strictEqual(rowsOf(mongo, 'mongo')[0].textContent, 'PMM reported no MongoDB series for the watched services on the last check.');
  assert.strictEqual(mongo.querySelector('[data-pmm="sets"]').textContent, '');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'empty panels name the failed check or say that PMM had no series';
}

async function refusedReadsSayTheyWereNotRead() {
  //given
  const serve = {rev: 3, state: stateWith(3, {sources: {prom: 'ok', pmm: 'ok \u00b7 no access to metrics', qan: 'ok \u00b7 no access to QAN', mongo: 'ok \u00b7 no access to data sources'}})};

  //when
  const {dom, doc, errors} = await boot(serve, 'pg');
  const pg = doc.getElementById('panel-pg');
  const mongo = doc.getElementById('panel-mongo');

  //then
  assert.strictEqual(rowsOf(pg, 'pg')[0].textContent, 'Not read: PMM refused the last check (no access to metrics).');
  assert.strictEqual(rowsOf(pg, 'qan')[0].textContent, 'Not read: PMM refused the last check (no access to QAN).');
  assert.strictEqual(rowsOf(mongo, 'mongo')[0].textContent, 'Not read: PMM refused the last check (no access to data sources).');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a refused PMM read says it was not read, never that there is no data';
}

async function hostileRowsAreShownAsText() {
  //given
  const serve = {rev: 2, state: stateWith(2, {
    anomalies: [{key: 'pmm|' + EVIL + '|down', status: 'open', sev: 'crit', svc: EVIL, label: EVIL, short: EVIL}],
    pmm: [pgRow(EVIL, {up: 0, cluster: EVIL, link: 'https://pmm.example.com/" onmouseover="window.__pwned=1'})],
    qan: [{svc: EVIL, db: EVIL, app: EVIL, queryid: EVIL, q: EVIL, calls: 1, t: 1, avgMs: 1, link: 'javascript:alert(1)'}],
    mongo: [mongoRow(EVIL, {node: EVIL, addr: EVIL, state: EVIL, set: EVIL})],
  })};

  //when
  const {dom, w, doc, errors} = await boot(serve, 'pg');
  const pg = doc.getElementById('panel-pg');
  const mongo = doc.getElementById('panel-mongo');

  //then
  assert.strictEqual(pg.querySelectorAll('img').length + mongo.querySelectorAll('img').length, 0);
  assert.strictEqual(w.__pwned, undefined);
  assert.ok(rowsOf(pg, 'pg')[0].textContent.includes(EVIL) && rowsOf(pg, 'qan')[0].textContent.includes(EVIL) && rowsOf(mongo, 'mongo')[0].textContent.includes(EVIL));
  assert.ok(![...pg.querySelectorAll('a')].some((a) => /^javascript:/i.test(a.getAttribute('href') || '') || a.hasAttribute('onmouseover')));
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'row values, card labels and links from the state are rendered as text, never as markup';
}

async function aCardOfASecondInstanceStaysWithItsOwnRow() {
  //given
  const query = (extra) => Object.assign({svc: 'pg-1', db: 'shop', app: 'checkout-service', queryid: 'q7', q: 'SELECT 1', calls: 10, t: 1, avgMs: 1, plugin: 'pmm'}, extra || {});
  const serve = {rev: 5, state: stateWith(5, {
    pmm: [pgRow('pg-1'), pgRow('pg-1', {cluster: 'east'})],
    qan: [query(), query({cluster: 'east'})],
    anomalies: [{key: 'pmm|pg-1|saturation@east', cluster: 'east', status: 'open', sev: 'warn', svc: 'pg-1', label: 'connection saturation', short: 'WARN pg-1'},
      {key: 'qan|pg-1|load|q7@east', cluster: 'east', status: 'open', sev: 'crit', svc: 'checkout-service', label: 'query DB time (s)', short: 'CRIT checkout-service'}]})};

  //when
  const {dom, doc, errors} = await boot(serve, 'pg');
  const pg = doc.getElementById('panel-pg');
  const servers = rowsOf(pg, 'pg').map((tr) => [tr.textContent.includes('(east)'), tr.querySelectorAll('.sig-tags a.tag').length]);
  const queries = rowsOf(pg, 'qan').map((tr) => [tr.textContent.includes('(east)'), tr.querySelectorAll('.sig-tags a.tag').length]);

  //then
  assert.deepStrictEqual(servers, [[false, 0], [true, 1]], 'the east card shows on the east server row only');
  assert.deepStrictEqual(queries, [[false, 0], [true, 1]], 'the east query card is found by its query id on the east row only');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a card of a second instance shows on that instance\'s row only, also when matched by query id';
}

async function aReplicaSetOfOneNameOnTwoInstancesKeepsItsOwnCards() {
  //given
  const serve = {rev: 6, state: stateWith(6, {
    mongo: [mongoRow('mongo-a-1', {state: 'PRIMARY'}), mongoRow('mongo-b-1', {cluster: 'east'})],
    anomalies: [{key: 'mongo|rs0|no-primary@east', cluster: 'east', status: 'open', sev: 'crit', svc: 'rs0', label: 'no primary', short: 'CRIT rs0 no primary'}]})};

  //when
  const {dom, doc, errors} = await boot(serve, 'mongo');
  const sets = [...doc.querySelectorAll('#panel-mongo [data-pmm="sets"] .svc')].map((s) => [s.textContent, s.nextElementSibling ? s.nextElementSibling.querySelectorAll('a.tag').length : 0]);

  //then
  assert.deepStrictEqual(sets, [['rs0', 0], ['rs0 (east)', 1]], 'each instance keeps its own rs0, and the east card stays with the east set');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a replica set of one name on two instances is listed once per instance with its own cards';
}

async function theQueryTableSaysWhenQueryAnalyticsHasNotRunOrFoundNothing() {
  //given
  const never = {rev: 3, state: stateWith(3, {sources: {prom: 'ok', pmm: 'ok'}})};
  const ran = {rev: 3, state: stateWith(3, {sources: {prom: 'ok', pmm: 'ok', qan: 'ok'}})};

  //when
  const a = await boot(never, 'pg');
  const b = await boot(ran, 'pg');

  //then
  assert.strictEqual(rowsOf(a.doc.getElementById('panel-pg'), 'qan')[0].textContent, 'No Query Analytics data yet. The Postgres queries check fills this on its next run.');
  assert.strictEqual(rowsOf(b.doc.getElementById('panel-pg'), 'qan')[0].textContent, 'No query over the thresholds in the last 60 minutes.');
  assert.deepStrictEqual(a.errors.concat(b.errors), []);
  a.dom.window.close();
  b.dom.window.close();
  return 'the query table says Query Analytics has not run yet before its first check';
}

(async () => {
  for (const t of [filledPanelsShowServersQueriesAndMembersAndRepaint, emptyPanelsSayWhyThereIsNoData, refusedReadsSayTheyWereNotRead, hostileRowsAreShownAsText, aCardOfASecondInstanceStaysWithItsOwnRow, aReplicaSetOfOneNameOnTwoInstancesKeepsItsOwnCards, theQueryTableSaysWhenQueryAnalyticsHasNotRunOrFoundNothing]) {
    console.log('PASS ' + (await t()));
  }
})().catch((e) => { console.error('FAIL', e.message); process.exit(1); });
