const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const SLOT = '<script type="text/plain" id="plugin-scripts"></script>';
const SCRIPT = fs.readFileSync(path.join(__dirname, '..', '..', 'skopax', 'plugins', 'elasticsearch', 'panel.js'), 'utf8');
const PLUGINS = [{id: 'elasticsearch', name: 'Elasticsearch', icon: '/plugins/elasticsearch/icon.svg', tab: 'logs', passes: [], instances: [],
  pages: [{tab: 'logs', label: 'Logs', group: 'observe', icon: '/plugins/elasticsearch/icon.svg'}]}];
const EVIL = '<img src=x onerror="window.__evil=1">';
const KIBANA = 'https://kibana.example.com/app/discover#/?_g=(time:(from:now-2h))';

function stateWith(rows, sources, extra) {
  return Object.assign({rev: 3, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: sources || {prom: 'ok', logs: 'ok'},
    summary: {level: 'ok', headline: 'x', points: [], at: iso(1)}, anomalies: [], services: [], history: [], routes: [], deploys: [], logTop: rows}, extra || {});
}

function flowCard(key, ns, sev, label, cluster) {
  return Object.assign({key: key, src: 'logflow', sig: key.split('|')[0], ns: ns, svc: 'logs', sev: sev, status: 'open', label: label, now: 0, short: label, firstSeen: iso(30), lastSeen: iso(5)},
    cluster ? {cluster: cluster} : {});
}

function flowRows(section) {
  return [...section.querySelectorAll('#logflow-table tbody tr')].map((tr) => [...tr.querySelectorAll('td')].map((td) => td.textContent.replace(/\s+/g, ' ').trim()));
}

async function boot(state) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  vc.on('error', (e) => errors.push('console.error ' + e));
  const html = pageHtml(TEMPLATE).replace(SLOT, '<script>\n' + SCRIPT.replace(/<\/script/gi, '<\\/script') + '</script>')
    .replace('/*__SKOPAX_DATA__*/', JSON.stringify(state).replace(/<\//g, '<\\/'));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=logs', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p0) => {
        const p = String(p0);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', operator: true, admin: false, teams: [], tags: []});
        if (p.startsWith('/api/server')) return ok({plugins: PLUGINS, tz: 'UTC'});
        if (p.startsWith('/api/admin/settings')) return ok({items: [], rev: 1, refreshSeconds: 15});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: state.rev});
        if (p.startsWith('/api/state')) return ok(state);
        return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
      };
    },
  });
  await wait(400);
  return {dom, w: dom.window, doc: dom.window.document, errors};
}

function cells(section) {
  return [...section.querySelectorAll('#logs-table tbody tr')].map((tr) => [...tr.querySelectorAll('td')].map((td) => td.textContent.trim()));
}

async function filledRowsShowFlagsCountsAndKibanaLinks() {
  //given
  const rows = [
    {app: 'orders-service', level: 'ERROR', msg: 'Connection refused: payments gateway', now: 60, d1: 0, d7: 0, flag: true, link: KIBANA, plugin: 'elasticsearch'},
    {app: 'checkout-service', level: 'ERROR', msg: 'Timeout on <id> call', now: 40, d1: 10, d7: 5, flag: true, link: KIBANA, plugin: 'elasticsearch'},
    {app: 'cart-service', level: 'WARN', msg: 'slow cart list', now: 35, d1: 10, d7: 2, link: '', plugin: 'elasticsearch', cluster: 'east'},
  ];

  //when
  const {dom, doc, errors} = await boot(stateWith(rows));
  const section = doc.getElementById('panel-logs');

  //then
  assert.ok(section, 'the plug-in page gets its section');
  assert.deepStrictEqual(cells(section).map((c) => c.slice(0, 7)), [
    ['New', 'orders-service', 'ERROR', 'Connection refused: payments gateway', '60', '0', '0'],
    ['Growing', 'checkout-service', 'ERROR', 'Timeout on <id> call', '40', '10', '5'],
    ['Steady', 'cart-service east', 'WARN', 'slow cart list', '35', '10', '2'],
  ]);
  const links = [...section.querySelectorAll('#logs-table tbody a')];
  assert.deepStrictEqual(links.map((a) => a.getAttribute('href')), [KIBANA, KIBANA], 'only https links become anchors');
  assert.ok(links.every((a) => a.getAttribute('target') === '_blank' && a.getAttribute('rel') === 'noopener'));
  assert.strictEqual(links[0].textContent, 'Log lines \u2197');
  assert.ok(section.querySelector('#logs-table tbody tr td .k-crit') && section.querySelectorAll('#logs-table tbody tr')[2].querySelector('.k-warn'), 'ERROR rows read critical, WARN rows warning');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the Logs panel lists each pattern with its flag, level, counts and a Kibana link, and names the instance of a second instance';
}

async function anEmptyStateSaysWhetherTheCheckRan() {
  //given
  const ran = await boot(stateWith([], {prom: 'ok', logs: 'ok'}));
  const failed = await boot(stateWith([], {prom: 'ok', logs: 'error: ES|QL HTTP 500'}));
  const none = await boot(stateWith(undefined, {prom: 'ok'}));
  const fallback = await boot(stateWith([], {prom: 'ok', logs: 'ok \u00b7 no data'}));

  //when
  const quiet = ran.doc.querySelector('#panel-logs #logs-table').textContent;
  const broken = failed.doc.querySelector('#panel-logs #logs-table').textContent;
  const missing = none.doc.querySelector('#panel-logs #logs-table').textContent;
  const read = fallback.doc.querySelector('#panel-logs #logs-table').textContent;

  //then
  assert.ok(quiet.includes('No flagged patterns and no ERROR pattern with 30 or more hits in the last hour.'), quiet);
  assert.ok(broken.includes('The logs check failed (error: ES|QL HTTP 500).') && !broken.includes('not run yet'), broken);
  assert.ok(missing.includes('The logs check has not run yet.'), missing);
  assert.ok(read.includes('No flagged patterns and no ERROR pattern with 30 or more hits in the last hour.') && !read.includes('not run yet'), read);
  for (const b of [ran, failed, none, fallback]) {
    assert.deepStrictEqual(b.errors, []);
    b.dom.window.close();
  }
  return 'with no rows the Logs panel says whether the logs check found nothing, failed or has not run, and reads an ok status with a note as ok';
}

async function hostileRowTextIsShownAsText() {
  //given
  const rows = [{app: EVIL, level: '<b>ERROR</b>', msg: '<script>window.__evil=2</script>' + EVIL, now: 7, d1: 0, d7: 0, flag: true, link: 'javascript:window.__evil=3', cluster: EVIL}];

  //when
  const {dom, w, doc, errors} = await boot(stateWith(rows, {logs: 'ok'}));
  const section = doc.getElementById('panel-logs');
  await wait(50);

  //then
  const [cellsOfRow] = cells(section);
  assert.strictEqual(cellsOfRow[1], EVIL + ' ' + EVIL);
  assert.strictEqual(cellsOfRow[2], '<b>ERROR</b>');
  assert.strictEqual(cellsOfRow[3], '<script>window.__evil=2</script>' + EVIL);
  assert.strictEqual(section.querySelectorAll('img, script, b').length, 0, 'no markup from a row reaches the page');
  assert.strictEqual(section.querySelectorAll('#logs-table tbody a').length, 0, 'a link that is not https is dropped');
  assert.strictEqual(w.__evil, undefined);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'an EVIL row in the Logs panel is rendered as text, and a non-https link is dropped';
}

async function logFlowRowsShowCountsLagAndOpenCards() {
  //given
  const flow = [
    {part: 'summary', at: iso(1), windowMin: 20, lagMin: 0.5, total: {n: 9803, d1: 10402, d7: 10402}, plugin: 'elasticsearch'},
    {part: 'namespace', ns: 'auth', n: 9000, d1: 8800, d7: 8800, newest: iso(0.5), lagMin: 0.5, plugin: 'elasticsearch'},
    {part: 'namespace', ns: 'orders', n: 0, d1: 700, d7: 700, plugin: 'elasticsearch'},
    {part: 'namespace', ns: 'tools', n: 0, d1: 2, d7: 2, plugin: 'elasticsearch'},
    {part: 'namespace', ns: 'payments', n: 800, d1: 900, d7: 900, newest: iso(1), lagMin: 0.7, plugin: 'elasticsearch'},
    {part: 'summary', at: iso(1), windowMin: 20, lagMin: null, total: {n: 0, d1: 50, d7: 40}, plugin: 'elasticsearch', cluster: 'east'},
    {part: 'namespace', ns: 'auth', n: 0, d1: 50, d7: 40, plugin: 'elasticsearch', cluster: 'east'},
  ];
  const cards = [flowCard('logs-stopped|orders/logs|flow', 'orders', 'warn', 'Logs stopped arriving'), flowCard('logs-lag|-/pipeline|lag', '-', 'crit', 'Log pipeline behind'),
    flowCard('logs-stopped|auth/logs|flow', 'auth', 'warn', 'Logs stopped arriving', 'east'), Object.assign(flowCard('logs-stopped|tools/logs|flow', 'tools', 'warn', 'Closed'), {status: 'cleared'}),
    Object.assign(flowCard('logs-stopped|payments/logs|flow', 'payments', 'crit', 'Muted'), {muted: 'known noise'}),
    Object.assign(flowCard('logs-lag|-/pipeline|lag', '-', 'crit', 'Handled'), {key: 'logs-lag|-/pipeline|lag-handled', triage: 'handled'}),
    Object.assign(flowCard('logs-stopped|payments/logs|flow', 'payments', 'crit', 'Taken'), {key: 'logs-stopped|payments/logs|flow-taken', triage: 'progress'})];

  //when
  const {dom, doc, errors} = await boot(stateWith([], {logs: 'ok', logflow: 'ok', 'logflow-east': 'ok'}, {logflow: flow, anomalies: cards}));
  const section = doc.getElementById('panel-logs');

  //then
  assert.deepStrictEqual(flowRows(section), [
    ['Healthy', 'auth', '9,000 1 d 8,800 \u00b7 7 d 8,800', '0.5 min'],
    ['Healthy', 'payments', '800 1 d 900 \u00b7 7 d 900', '0.7 min'],
    ['Warning Logs stopped arriving', 'orders', '0 1 d 700 \u00b7 7 d 700', '-'],
    ['Warning Logs stopped arriving', 'auth east', '0 1 d 50 \u00b7 7 d 40', '-'],
    ['Quiet', 'tools', '0 1 d 2 \u00b7 7 d 2', '-'],
  ]);
  const lag = section.querySelector('#logflow-lag').textContent;
  assert.strictEqual(lag, 'newest line 0.5 min old \u00b7 9,803 lines in 20 min \u00b7 east: no fresh line \u00b7 0 lines in 20 min');
  const tags = [...section.querySelectorAll('#logflow-tags a')].map((a) => [a.textContent, a.getAttribute('href')]);
  assert.deepStrictEqual(tags.map((t) => t[0]), ['Log pipeline behind', 'Logs stopped arriving', 'Logs stopped arriving east'], 'muted and handled cards are left out, and a second instance is named');
  assert.strictEqual(tags[0][1], '#a=' + encodeURIComponent('logs-lag|-/pipeline|lag'));
  assert.ok(section.querySelector('#logflow-table tbody tr td .state.k-warn'), 'a namespace with an open card shows its severity');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the Log flow table lists each namespace with its counts, the age of its newest line, its open cards, and the lag of each instance';
}

async function anEmptyLogFlowSaysWhetherTheCheckRan() {
  //given
  const ran = await boot(stateWith([], {logs: 'ok', logflow: 'ok'}, {logflow: [{part: 'summary', at: iso(1), windowMin: 20, lagMin: null, total: {n: 0, d1: 0, d7: 0}}]}));
  const quiet = await boot(stateWith([], {logs: 'ok', logflow: 'ok \u00b7 no data'}, {logflow: []}));
  const failed = await boot(stateWith([], {logs: 'ok', logflow: 'error: ES|QL HTTP 504'}));
  const none = await boot(stateWith([], {logs: 'ok'}));

  //when
  const texts = [ran, quiet, failed, none].map((b) => b.doc.querySelector('#logflow-table tbody').textContent);

  //then
  assert.strictEqual(texts[0], 'No namespace has log lines in these windows.');
  assert.strictEqual(texts[1], 'No namespace has log lines in these windows (ok \u00b7 no data).');
  assert.strictEqual(texts[2], 'The log flow check failed (error: ES|QL HTTP 504).');
  assert.strictEqual(texts[3], 'The log flow check has not run yet.');
  for (const b of [ran, quiet, failed, none]) {
    assert.deepStrictEqual(b.errors, []);
    b.dom.window.close();
  }
  return 'with no namespace rows the Log flow table says whether the check found nothing, found no data, failed or has not run';
}

async function hostileLogFlowTextIsShownAsText() {
  //given
  const flow = [{part: 'summary', at: iso(1), windowMin: EVIL, lagMin: EVIL, total: {n: EVIL}, cluster: EVIL},
    {part: 'namespace', ns: EVIL, n: EVIL, d1: '<b>1</b>', d7: 3, lagMin: '<i>2</i>', cluster: EVIL}];
  const cards = [Object.assign(flowCard('logs-stopped|x/logs|flow', EVIL, '<b>crit</b>', EVIL), {cluster: EVIL})];

  //when
  const {dom, w, doc, errors} = await boot(stateWith([], {logs: 'ok', logflow: 'ok'}, {logflow: flow, anomalies: cards}));
  const section = doc.getElementById('panel-logs');
  await wait(50);

  //then
  const [row] = flowRows(section);
  assert.strictEqual(row[1], EVIL + ' ' + EVIL);
  assert.strictEqual(section.querySelectorAll('img, script, b, i').length, 0, 'no markup from a row or a card reaches the page');
  assert.strictEqual(w.__evil, undefined);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'an EVIL log flow row and card are rendered as text';
}

async function aRefusedReadSaysItWasNotReadAndHidesTheOldRows() {
  //given
  const old = [{app: 'checkout-service', level: 'ERROR', msg: 'boom', now: 40, d1: 0, d7: 0, flag: true}];
  const flowRowsOld = [{part: 'namespace', ns: 'shop', n: 900, d1: 800, d7: 850}];
  const b = await boot(stateWith(old, {prom: 'ok', logs: 'ok \u00b7 no access to logs', logflow: 'ok \u00b7 no access to logs'}, {logflow: flowRowsOld}));

  //when
  const logs = b.doc.querySelector('#panel-logs #logs-table tbody').textContent;
  const flowText = b.doc.querySelector('#logflow-table tbody').textContent;

  //then
  assert.strictEqual(logs, 'Not read: no access to logs. Earlier rows are hidden until the logs check can read the log index again.');
  assert.ok(!logs.includes('boom') && !logs.includes('No flagged patterns'), 'a refusal is never shown as nothing found or as current rows');
  assert.strictEqual(flowText, 'Not read: no access to logs. Earlier rows are hidden until the log flow check can read the log index again.');
  assert.deepStrictEqual(b.errors, []);
  b.dom.window.close();
  return 'a refused logs or log flow read says it was not read and hides the earlier rows';
}

(async () => {
  for (const t of [filledRowsShowFlagsCountsAndKibanaLinks, anEmptyStateSaysWhetherTheCheckRan, hostileRowTextIsShownAsText, logFlowRowsShowCountsLagAndOpenCards,
    anEmptyLogFlowSaysWhetherTheCheckRan, hostileLogFlowTextIsShownAsText, aRefusedReadSaysItWasNotReadAndHidesTheOldRows]) {
    console.log('PASS ' + (await t()));
  }
})().catch((e) => { console.error('FAIL', e.message); process.exit(1); });
