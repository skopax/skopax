const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const PANEL_JS = path.join(path.dirname(TEMPLATE), '..', 'plugins', 'capacity', 'panel.js');
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const SLOT = '<script type="text/plain" id="plugin-scripts"></script>';
const ICON = '/plugins/capacity/icon.svg';
const PLUGINS = [{id: 'capacity', name: 'Capacity', icon: ICON, tab: 'disk', passes: ['capacity', 'capacity-east'],
  instances: [{name: 'main', primary: true, pass: 'capacity', status: 'ok'}, {name: 'east', primary: false, pass: 'capacity-east', status: 'error: heap: Prometheus query HTTP 502'}],
  pages: [{tab: 'disk', label: 'Disk', group: 'observe', icon: ICON}, {tab: 'jvm', label: 'JVM', group: 'observe', icon: ICON}, {tab: 'cap', label: 'Capacity', group: 'observe', icon: ICON}]}];
const KEY = 'pvc-disk|search/es-data-0|disk';
const EVIL = '<img src=x onerror="window.__evil=1">';

function card() {
  return {key: KEY, num: 7, src: 'capacity', sig: 'pvc-disk', ns: 'search', svc: 'es-data-0', sub: 'disk', sev: 'crit', status: 'open', label: 'Volume disk',
    short: 'CRIT es-data-0 disk Volume disk 91.2%', what: 'es-data-0 (search) is 91.2 % full', now: 91.2, d1: null, d7: null, unit: '%', peak: 91.2,
    firstSeen: iso(30), lastSeen: iso(1), muted: null, triage: null, capacity: 'volumes', ranKey: 'capacity:volumes'};
}

function stateWith(rows, extra) {
  return Object.assign({rev: 3, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {capacity: 'ok'},
    summary: {level: 'ok', headline: 'x', points: [], at: iso(1)}, anomalies: [], services: [], history: [], routes: [], deploys: []}, rows === undefined ? {} : {capacity: rows}, extra || {});
}

function filled() {
  return stateWith([
    {part: 'volumes', source: 'main', ns: 'search', pvc: 'es-data-0', pct: 91.2, capGb: 337.6, freeGb: 29.7, growGbDay: 3.9, daysToFull: 7.6, link: 'https://prometheus.example.com/query?g0.expr=used', plugin: 'capacity'},
    {part: 'volumes', source: 'east', ns: 'data', pvc: 'pg-data-0', pct: 41, capGb: 50, freeGb: 29.5, growGbDay: 0.1, daysToFull: 295, link: 'https://prom-east.example.com/query?g0.expr=used', plugin: 'capacity', cluster: 'east'},
    {part: 'dbdisk', source: 'main', node: 'db-1', mount: '/data', pct: 88, capGb: 1000, freeGb: 120, growGbDay: 10, daysToFull: 12, plugin: 'capacity'},
    {part: 'hpa', source: 'main', ns: 'shop', hpa: 'checkout-service', current: 15, min: 10, max: 15, gap30: 0, cpu: 640, cpuTarget: 500, atMax30: true, plugin: 'capacity'},
    {part: 'heap', source: 'main', ns: 'shop', job: 'cart-service', pct: 97, pct15: 96.1, maxMb: 564, gcMsPerS: 12.5, link: 'https://prometheus.example.com/query?g0.expr=heap', plugin: 'capacity'},
    {part: 'summary', at: iso(5), source: 'main', plugin: 'capacity'},
    {part: 'summary', at: iso(5), source: 'east', plugin: 'capacity', cluster: 'east'},
  ], {anomalies: [card()], sources: {capacity: 'ok', 'capacity-east': 'error: heap: Prometheus query HTTP 502'}});
}

async function boot(state, tab) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  vc.on('error', (e) => errors.push('console.error ' + e));
  const script = '<script>' + fs.readFileSync(PANEL_JS, 'utf8').replace(/<\/script/gi, '<\\/script') + '</script>';
  const html = pageHtml(TEMPLATE).replace(SLOT, () => script).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state).replace(/<\//g, '<\\/'));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=' + (tab || 'disk'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        const at = String(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (at.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', operator: true, admin: false, teams: [], tags: []});
        if (at.startsWith('/api/server')) return ok({plugins: PLUGINS, tz: 'UTC'});
        if (at.startsWith('/api/admin/settings')) return ok({items: [], rev: 1, refreshSeconds: 15});
        if (at.startsWith('/api/actions') || at.startsWith('/api/deploy-reports') || at.startsWith('/api/requests')) return ok([]);
        if (at.startsWith('/api/rev')) return ok({rev: state.rev});
        if (at.startsWith('/api/state')) return ok(state);
        return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
      };
    },
  });
  await wait(400);
  return {dom, w: dom.window, doc: dom.window.document, errors};
}

const cells = (tr) => [...tr.querySelectorAll('td')].map((td) => td.textContent.replace(/\s+/g, ' ').trim());
const rowsOf = (section, id) => [...section.querySelectorAll('table[data-part="' + id + '"] tbody tr')];

async function theThreePagesDrawTheCapacityRowsWithTheirCardsAndGraphs() {
  //given
  const {dom, doc, errors} = await boot(filled());

  //when
  const disk = doc.getElementById('panel-disk'), jvm = doc.getElementById('panel-jvm'), cap = doc.getElementById('panel-cap');

  //then
  assert.ok(disk && jvm && cap, 'every capacity page gets its section');
  const [es, pg] = rowsOf(disk, 'volumes');
  assert.deepStrictEqual(cells(es).slice(1), ['es-data-0 search', '91% \u2197', '29.7 GB of 338 GB', '+3.9 GB', '7.6 d']);
  assert.ok(/^Critical/.test(cells(es)[0]), 'a volume with an open card shows its severity');
  const tag = es.querySelector('a.tag');
  assert.strictEqual(tag.getAttribute('href'), '#a=' + encodeURIComponent(KEY));
  assert.strictEqual(es.querySelector('a.lnk').getAttribute('href'), 'https://prometheus.example.com/query?g0.expr=used');
  assert.deepStrictEqual(cells(pg).slice(0, 2), ['Healthy', 'pg-data-0 data \u00b7 east'], 'another instance names itself next to the namespace');
  assert.deepStrictEqual(rowsOf(disk, 'dbdisk').map(cells), [['Healthy', 'db-1 /data', '88%', '120 GB of 1000 GB', '+10.0 GB', '12 d']]);
  assert.deepStrictEqual(rowsOf(jvm, 'heap').map(cells), [['Healthy', 'cart-service shop', '97% \u2197 of 564 MB', '12.5 ms/s']]);
  assert.deepStrictEqual(rowsOf(cap, 'hpa').map(cells), [['At max', 'checkout-service shop', '15 / 15 min 10', '640% target 500%']]);
  [disk, jvm, cap].forEach((s) => assert.ok(/failed partly \(east: heap: Prometheus query HTTP 502\)/.test(s.querySelector('.panel-note').textContent), 'a failed part is named on every page'));
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the Disk, JVM and Capacity pages draw volumes, host disks, heap and autoscalers, with card tags, graph links and the instance name';
}

async function anEmptyOrQuietStateSaysSo() {
  //given
  const none = await boot(stateWith(undefined));
  const quiet = await boot(stateWith([{part: 'summary', at: iso(5), source: 'main', plugin: 'capacity'}]));
  const broken = await boot(stateWith([{part: 'summary', at: iso(5), source: 'main', plugin: 'capacity'}], {sources: {capacity: 'error: JVM heap: Prometheus query HTTP 502'}}));
  const off = await boot(stateWith([{part: 'summary', at: iso(5), source: 'main', off: ['heap', 'dbdisk'], plugin: 'capacity'}]));

  //when
  const text = (t, tab) => t.doc.getElementById('panel-' + tab).textContent;

  //then
  assert.ok(/No volume data yet/.test(text(none, 'disk')) && /No host disk data yet/.test(text(none, 'disk')));
  assert.ok(/No JVM heap data yet/.test(text(none, 'jvm')) && /No autoscaler data yet/.test(text(none, 'cap')));
  assert.ok(/The last check found no volumes/.test(text(quiet, 'disk')) && /The last check found no host disks/.test(text(quiet, 'disk')));
  assert.ok(/The last check found no JVM heap/.test(text(quiet, 'jvm')) && /The last check found no autoscalers/.test(text(quiet, 'cap')));
  assert.ok(!/failed partly/.test(text(none, 'disk')));
  assert.ok(/The last check could not read the JVM heap/.test(text(broken, 'jvm')) && !/found no JVM heap/.test(text(broken, 'jvm')), 'a part that failed is not called empty');
  assert.ok(/The last check found no autoscalers/.test(text(broken, 'cap')), 'the parts that ran keep their own text');
  assert.ok(/Reading the JVM heap is switched off/.test(text(off, 'jvm')) && !/found no JVM heap/.test(text(off, 'jvm')), 'a part switched off says so');
  assert.ok(/Reading host disks is switched off/.test(text(off, 'disk')) && /The last check found no volumes/.test(text(off, 'disk')));
  assert.deepStrictEqual(none.errors.concat(quiet.errors, broken.errors, off.errors), []);
  [none, quiet, broken, off].forEach((t) => t.dom.window.close());
  return 'an empty state says no data yet, a quiet check says it found none, a failed part says it could not be read, and a switched-off part says it is off';
}

async function anEvilRowIsDrawnAsText() {
  //given
  const state = stateWith([
    {part: 'volumes', source: 'main', ns: '<b>bold</b>', pvc: EVIL, pct: 50, link: 'javascript:window.__evil=2', plugin: 'capacity'},
    {part: 'heap', source: 'main', ns: 'shop', job: EVIL, pct: 50, link: 'https://prometheus.example.com/" onmouseover="window.__evil=3', plugin: 'capacity'},
  ], {sources: {capacity: 'error: volumes: ' + EVIL}});

  //when
  const {dom, w, doc, errors} = await boot(state);
  const disk = doc.getElementById('panel-disk'), jvm = doc.getElementById('panel-jvm');

  //then
  assert.strictEqual(w.__evil, undefined);
  assert.strictEqual(disk.querySelector('img'), null);
  assert.strictEqual(disk.querySelector('b'), null);
  assert.ok(disk.textContent.includes(EVIL) && disk.textContent.includes('<b>bold</b>'), 'the names are shown as text');
  assert.ok(![...disk.querySelectorAll('a')].some((a) => /^javascript:/i.test(a.getAttribute('href') || '')), 'only https graph links are drawn');
  assert.ok(![...jvm.querySelectorAll('a')].some((a) => a.hasAttribute('onmouseover')));
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'an EVIL row name, namespace, link and status are drawn as text, never as markup';
}

async function aRefusedPartSaysItWasNotReadNeverThatItFoundNone() {
  //given
  const refused = await boot(stateWith([{part: 'summary', at: iso(5), source: 'main', off: [], noAccess: ['heap', 'dbdisk'], plugin: 'capacity'}],
    {sources: {capacity: 'ok \u00b7 no access to JVM heap, host disks'}}));

  //when
  const text = (tab) => refused.doc.getElementById('panel-' + tab).textContent;

  //then
  assert.ok(/Not read: no access to the JVM heap/.test(text('jvm')) && !/found no JVM heap/.test(text('jvm')), 'a refusal is never read as none');
  assert.ok(/Not read: no access to the host disks/.test(text('disk')) && /The last check found no volumes/.test(text('disk')), 'the parts that were read keep their own text');
  assert.ok(/The last check found no autoscalers/.test(text('cap')));
  assert.deepStrictEqual(refused.errors, []);
  refused.dom.window.close();
  return 'a refused part says it was not read for lack of access, never that the check found none';
}

(async () => {
  for (const t of [theThreePagesDrawTheCapacityRowsWithTheirCardsAndGraphs, anEmptyOrQuietStateSaysSo, anEvilRowIsDrawnAsText, aRefusedPartSaysItWasNotReadNeverThatItFoundNone]) {
    console.log('PASS ' + (await t()));
  }
})().catch((e) => { console.error('FAIL', e.message); process.exit(1); });
