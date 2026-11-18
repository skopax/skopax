const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const PANEL = fs.readFileSync(path.join(__dirname, '..', '..', 'skopax', 'plugins', 'kafka', 'panel.js'), 'utf8');
const SLOT = '<script type="text/plain" id="plugin-scripts"></script>';
const PLUGINS = [{id: 'kafka', name: 'Kafka', icon: '/plugins/kafka/icon.svg', tab: 'kafka', passes: [], instances: [], pages: [{tab: 'kafka', label: 'Kafka', group: 'observe', icon: '/plugins/kafka/icon.svg'}]}];
const EVIL = '<img src=x onerror="window.__evil=1">';
const UI = 'https://prometheus.example.com/query?g0.expr=lag';
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

function card(key, sev, extra) {
  const [sig, rest, sub] = key.split('|');
  const ns = rest.split('/')[0], svc = rest.slice(ns.length + 1);
  return Object.assign({key, src: 'kafka', sig, ns, svc, sub, sev, status: 'open', label: 'Kafka ' + sub, short: sev.toUpperCase() + ' ' + svc, now: 1, firstSeen: iso(30), lastSeen: iso(1)}, extra || {});
}

function stateWith(rows, anomalies, sources) {
  return {rev: 3, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: Object.assign({prom: 'ok', kafka: 'ok'}, sources || {}),
    summary: {level: 'ok', headline: 'x', points: [], at: iso(1)}, anomalies: anomalies || [], services: [], history: [], routes: [], deploys: [], kafka: rows};
}

async function boot(state) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  vc.on('error', (e) => errors.push('console.error ' + e));
  const html = pageHtml(TEMPLATE).replace(SLOT, '<script>\n' + PANEL + '</script>').replace('/*__SKOPAX_DATA__*/', JSON.stringify(state).replace(/<\//g, '<\\/'));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=kafka', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
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
  return {dom, w: dom.window, doc: dom.window.document, errors, section: dom.window.document.getElementById('panel-kafka')};
}

function cells(row) { return [...row.querySelectorAll('td')].map((td) => td.textContent.trim()); }

async function aFilledStateShowsClustersGroupsServicesAndTheirCards() {
  //given
  const rows = [
    {part: 'clusters', ns: 'pipeline', expected: 3, brokers: 3, brokers10: 3, topics: 14, partitions: 89, urp: 0, msgs: 219, msgsD1: 200, msgsD7: 210, link: UI, plugin: 'kafka'},
    {part: 'clusters', ns: 'streaming', expected: 3, brokers: 2, brokers10: 2, topics: 174, partitions: 625, offline: 4, urp: 3, diskPct: 91.5, diskPvc: 'data-streaming-kafka-1', msgs: 85.4, plugin: 'kafka'},
    {part: 'groups', ns: 'streaming', group: 'profile-service', members: 0, lag: 950, d1: 10, d7: 12, link: UI, plugin: 'kafka'},
    {part: 'groups', ns: 'streaming', group: 'cart-service', members: 6, lag: 101, d1: 90, d7: 95, plugin: 'kafka'},
    {part: 'groups', ns: 'pipeline', group: 'log-shipper', members: 36, lag: 400, plugin: 'kafka'},
    {part: 'groups', ns: 'billing-exporter', namespace: 'billing', group: 'invoice-service', members: 2, lag: 50, plugin: 'kafka'},
    {part: 'app', ns: 'commerce', job: 'inventory-service', source: 'main', in: 500, inFail: 40, inFailD1: 1, inFailD7: 1, inMs: 12.5, out: 0, inLink: 'javascript:alert(1)', plugin: 'kafka'},
    {part: 'summary', at: iso(1), source: 'main', preset: 'strimzi', plugin: 'kafka'}];
  const anomalies = [card('kafka-broker|streaming/streaming|brokers', 'crit', {kafka: 'clusters'}), card('kafka-gone|streaming/kafka/profile-service|members', 'warn', {kafka: 'groups'}),
    card('kafka-consume-err|commerce/inventory-service|in', 'warn', {kafka: 'app'}), card('kafka-lag|pipeline/kafka/log-shipper', 'warn', {src: 'prom', sub: ''}),
    card('kafka-lag|billing/kafka/invoice-service', 'warn', {src: 'prom', sub: ''})];

  //when
  const {dom, errors, section} = await boot(stateWith(rows, anomalies));
  const clusters = [...section.querySelectorAll('#kafka-clusters tbody tr')];
  const groups = [...section.querySelectorAll('#kafka-groups tbody tr')];
  const services = [...section.querySelectorAll('#kafka-apps tbody tr')];
  const tags = [...section.querySelectorAll(':scope > .sig-tags a.tag')];

  //then
  assert.ok(section, 'the page adds the Kafka panel section');
  assert.deepStrictEqual(clusters.map((r) => cells(r)[1]), ['pipeline', 'streaming']);
  assert.deepStrictEqual(clusters.map((r) => cells(r)[0]), ['Healthy', 'Critical']);
  assert.deepStrictEqual(clusters.map((r) => cells(r)[2]), ['3/3', '2/3']);
  assert.strictEqual(cells(clusters[1])[6], '92%');
  assert.strictEqual(cells(clusters[0])[6], '\u2013', 'no disk without the strimzi rows');
  assert.ok(cells(clusters[1])[4].includes('4 offline'));
  assert.deepStrictEqual(groups.map((r) => r.querySelector('.state').textContent), ['Warning', 'Healthy', 'Warning', 'Warning'], 'a group follows its own card and the Prometheus lag card of its namespace');
  assert.deepStrictEqual(groups.map((r) => cells(r)[1]), ['profile-servicestreaming', 'cart-servicestreaming', 'log-shipperpipeline', 'invoice-servicebilling-exporter']);
  assert.deepStrictEqual(services.map((r) => cells(r)[0]), ['Warning']);
  assert.ok(cells(services[0])[3].startsWith('40'), cells(services[0])[3]);
  assert.strictEqual(services[0].querySelectorAll('a').length, 0, 'a javascript link is not a link');
  assert.deepStrictEqual([...section.querySelectorAll('a.lnk')].map((a) => a.getAttribute('href')), [UI, UI]);
  assert.deepStrictEqual(tags.map((a) => a.textContent), ['Kafka brokers', 'Kafka members', 'Kafka in'], 'the tags are the open Kafka cards, worst first');
  assert.ok(!section.textContent.includes('strimzi preset'), 'no preset note under strimzi');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a filled state lists clusters, consumer groups and services with their open cards, and only https graph links';
}

async function anEmptyStateSaysThereIsNoDataYet() {
  //given
  const state = stateWith([{part: 'summary', at: iso(1), source: 'main', preset: 'kafka-exporter', plugin: 'kafka'}], [],
    {kafka: 'ok \u00b7 no data'});

  //when
  const {dom, errors, section} = await boot(state);
  const empty = [...section.querySelectorAll('tr.empty')].map((r) => r.textContent);

  //then
  assert.deepStrictEqual(empty, ['The last Kafka check found no Kafka series.', 'No consumer groups yet.', 'No service Kafka timers yet.'], 'a quiet pass is not a pass that never ran');
  assert.ok(section.textContent.includes('Broker disks and broker pod restarts need the strimzi preset.'));
  assert.ok(!section.textContent.includes('failed'), 'an ok status with a note is not a failure');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a quiet pass shows the empty rows as found empty, with the preset note';
}

async function anEvilRowIsRenderedAsText() {
  //given
  const rows = [{part: 'clusters', ns: EVIL, expected: 1, brokers: 1, diskPvc: EVIL, plugin: 'kafka', cluster: EVIL},
    {part: 'groups', ns: EVIL, group: EVIL, members: 1, lag: 1, link: 'https://x.example.com/"><img src=x onerror="window.__evil=2">', plugin: 'kafka'},
    {part: 'app', ns: EVIL, job: EVIL, source: EVIL, in: 1, plugin: 'kafka'}];
  const anomalies = [card('kafka-gone|x/kafka/y|members', 'warn', {label: EVIL, short: EVIL, key: 'kafka-gone|' + EVIL})];

  //when
  const {dom, w, errors, section} = await boot(stateWith(rows, anomalies, {kafka: 'error: ' + EVIL}));
  await wait(100);

  //then
  assert.strictEqual(section.querySelectorAll('img').length, 0, 'no element comes from row text');
  assert.strictEqual(w.__evil, undefined);
  assert.ok(section.textContent.includes(EVIL), 'the text shows as written');
  assert.ok(section.textContent.includes('The last Kafka check failed: ' + EVIL));
  assert.strictEqual(section.querySelector('#kafka-groups a.lnk').getAttribute('href'), 'https://x.example.com/"><img src=x onerror="window.__evil=2">');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'an EVIL row, card and status render as text, attributes included';
}

async function aLagCardOfASecondPrometheusInstanceFollowsItsGroup() {
  //given
  const rows = (source) => [{part: 'groups', ns: 'pipeline', group: 'log-shipper', members: 36, lag: 400, plugin: 'kafka'}, {part: 'summary', at: iso(1), source, preset: 'strimzi', plugin: 'kafka'}];
  const lag = card('kafka-lag|pipeline/kafka/log-shipper@east', 'warn', {src: 'prom', sub: '', cluster: 'east'});

  //when
  const east = await boot(stateWith(rows('east'), [lag]));
  const throughEast = [...east.section.querySelectorAll('#kafka-groups tbody tr')].map((r) => r.querySelector('.state').textContent);
  east.dom.window.close();
  const main = await boot(stateWith(rows('main'), [lag]));
  const throughMain = [...main.section.querySelectorAll('#kafka-groups tbody tr')].map((r) => r.querySelector('.state').textContent);
  main.dom.window.close();

  //then
  assert.deepStrictEqual(throughEast, ['Warning'], 'a group read through Prometheus east shows the lag card of east');
  assert.deepStrictEqual(throughMain, ['Healthy'], 'a group read through the primary does not take the lag card of east');
  assert.deepStrictEqual(east.errors.concat(main.errors), []);
  return 'the lag card of a second Prometheus instance follows the groups read through that instance only';
}

async function aGroupLinksItsLagCardAndANeverRunPassSaysSo() {
  //given
  const rows = [{part: 'groups', ns: 'pipeline', group: 'log-shipper', members: 36, lag: 400, plugin: 'kafka'},
    {part: 'summary', at: iso(1), source: 'east', sourcePrimary: false, preset: 'strimzi', plugin: 'kafka'}];
  const primaryLag = card('kafka-lag|pipeline/kafka/log-shipper', 'warn', {src: 'prom', sub: '', label: 'consumer lag'});
  const own = rows.slice(0, 1).concat([{part: 'summary', at: iso(1), source: 'main', sourcePrimary: true, preset: 'strimzi', plugin: 'kafka'}]);

  //when
  const east = await boot(stateWith(rows, [primaryLag]));
  const main = await boot(stateWith(own, [primaryLag]));
  const never = await boot(stateWith([], [], {kafka: undefined}));

  //then
  const eastRow = east.section.querySelector('#kafka-groups tbody tr');
  const mainRow = main.section.querySelector('#kafka-groups tbody tr');
  assert.strictEqual(eastRow.querySelector('.state').textContent, 'Healthy', 'a group read through east never takes the primary Prometheus lag card');
  assert.strictEqual(mainRow.querySelector('.state').textContent, 'Warning');
  assert.strictEqual(mainRow.querySelector('a.tag').getAttribute('href'), '#a=' + encodeURIComponent('kafka-lag|pipeline/kafka/log-shipper'), 'the group row links the lag card it follows');
  assert.strictEqual(never.section.querySelector('#kafka-clusters tr.empty').textContent, 'No Kafka data yet. The Kafka check fills this on its next run.');
  const css = fs.readFileSync(TEMPLATE, 'utf8');
  assert.ok(/\.sig-tags \.tag \{[^}]*white-space: normal/.test(css) && /\.sig-tags \{[^}]*max-width: 100%/.test(css), 'a long card tag wraps instead of pushing the page sideways');
  assert.deepStrictEqual(east.errors.concat(main.errors, never.errors), []);
  [east, main, never].forEach((b) => b.dom.window.close());
  return 'a group links its lag card, a group read through another Prometheus never takes the primary one, and a never-run pass says so';
}

async function aRefusedPartSaysItWasNotRead() {
  //given
  const b = await boot(stateWith([{part: 'summary', at: iso(1), source: 'main', preset: 'strimzi', plugin: 'kafka'}], [], {kafka: 'ok \u00b7 no data for services \u00b7 no access to clusters, groups'}));

  //when
  const empty = [...b.section.querySelectorAll('tr.empty')].map((r) => r.textContent);

  //then
  assert.deepStrictEqual(empty, ['Not read: no access to clusters.', 'Not read: no access to groups.', 'No service Kafka timers yet.']);
  assert.deepStrictEqual(b.errors, []);
  b.dom.window.close();
  return 'a refused part says it was not read, never that there is nothing';
}

(async () => {
  for (const t of [aFilledStateShowsClustersGroupsServicesAndTheirCards, anEmptyStateSaysThereIsNoDataYet, anEvilRowIsRenderedAsText, aLagCardOfASecondPrometheusInstanceFollowsItsGroup, aGroupLinksItsLagCardAndANeverRunPassSaysSo, aRefusedPartSaysItWasNotRead]) {
    console.log('PASS ' + (await t()));
  }
})().catch((e) => { console.error('FAIL', e.message); process.exit(1); });
