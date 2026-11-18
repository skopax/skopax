const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const PANEL = fs.readFileSync(path.join(__dirname, '..', '..', 'skopax', 'plugins', 'redis', 'panel.js'), 'utf8');
const SLOT = '<script type="text/plain" id="plugin-scripts"></script>';
const PLUGINS = [{id: 'redis', name: 'Redis', icon: '/plugins/redis/icon.svg', tab: 'redis', passes: [], instances: [], pages: [{tab: 'redis', label: 'Redis', group: 'observe', icon: '/plugins/redis/icon.svg'}]}];
const EVIL = '<img src=x onerror="window.__evil=1">';
const UI = 'https://prometheus.example.com/query?g0.expr=up';
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

function card(key, sev, extra) {
  const [sig, rest, sub] = key.split('|');
  const [ns, svc] = rest.split('/');
  return Object.assign({key, src: 'redis', sig, ns, svc, sub, sev, status: 'open', label: 'Redis ' + sub, short: sev.toUpperCase() + ' ' + svc, now: 1, firstSeen: iso(30), lastSeen: iso(1)}, extra || {});
}

function stateWith(rows, anomalies, sources) {
  return {rev: 3, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: Object.assign({prom: 'ok', redis: 'ok'}, sources || {}),
    summary: {level: 'ok', headline: 'x', points: [], at: iso(1)}, anomalies: anomalies || [], services: [], history: [], routes: [], deploys: [], redis: rows};
}

async function boot(state) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  vc.on('error', (e) => errors.push('console.error ' + e));
  const html = pageHtml(TEMPLATE).replace(SLOT, '<script>\n' + PANEL + '</script>').replace('/*__SKOPAX_DATA__*/', JSON.stringify(state).replace(/<\//g, '<\\/'));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=redis', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
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
  return {dom, w: dom.window, doc: dom.window.document, errors, section: dom.window.document.getElementById('panel-redis')};
}

function cells(row) { return [...row.querySelectorAll('td')].map((td) => td.textContent.trim()); }

async function aFilledStateShowsInstancesServicesAndTheirCards() {
  //given
  const rows = [
    {part: 'exporter', ns: 'cache', name: 'redis-0', up: 1, role: 'slave', memoryBytes: 2097152, clients: 12, ops: 300, plugin: 'redis'},
    {part: 'exporter', ns: 'cache', name: 'redis-1', up: 0, role: 'slave', plugin: 'redis'},
    {part: 'exporter', ns: 'cache', name: 'redis-2', up: 1, role: 'master', replicas: 1, memoryBytes: 3145728, clients: 20, ops: 900, plugin: 'redis'},
    {part: 'app', ns: 'commerce', job: 'cart-service', redis: 'redis.example.com', source: 'main', ms: 12, d1: 2, d7: 2.5, calls: 1000, errors: 0, link: UI, plugin: 'redis'},
    {part: 'app', ns: 'orders', job: 'orders-service', redis: 'redis.example.com', source: 'east', ms: 3, d1: 3, d7: 3, calls: 50, errors: 0, link: 'javascript:alert(1)', plugin: 'redis', cluster: 'east'},
    {part: 'summary', at: iso(1), source: 'main', plugin: 'redis'}];
  const anomalies = [card('redis-app|commerce/cart-service|latency', 'warn'), card('redis-repl|cache/redis|replicas', 'warn'), card('redis-down|cache/redis-1|up', 'crit'),
    card('redis-app|orders/orders-service|latency', 'warn', {status: 'cleared'}), card('err|commerce/cart-service', 'crit', {src: 'prom'})];

  //when
  const {dom, doc, errors, section} = await boot(stateWith(rows, anomalies));
  const instances = [...section.querySelectorAll('#redis-instances tbody tr')];
  const services = [...section.querySelectorAll('#redis-apps tbody tr')];
  const tags = [...section.querySelectorAll('.sig-tags a.tag')];
  const links = [...section.querySelectorAll('#redis-apps a.lnk')];

  //then
  assert.ok(section, 'the page adds the Redis panel section');
  assert.deepStrictEqual(instances.map((r) => cells(r)[1]), ['redis-0cache', 'redis-1cache', 'redis-2cache']);
  assert.deepStrictEqual(instances.map((r) => cells(r)[0]), ['Healthy', 'Critical', 'Healthy'], 'a down instance follows its open card');
  assert.strictEqual(cells(instances[2])[2], 'master');
  assert.strictEqual(cells(instances[2])[3], '1', 'replicas show on the master only');
  assert.strictEqual(cells(instances[0])[3], '\u2013');
  assert.strictEqual(cells(instances[2])[4], '3.0 MiB');
  assert.deepStrictEqual(services.map((r) => cells(r)[0]), ['Warning', 'Healthy'], 'the cleared card of the east service does not count');
  assert.strictEqual(cells(services[1])[1], 'orders-serviceorders \u00b7 east', 'a row of a second instance names it');
  assert.ok(cells(services[0])[3].startsWith('12 ms'), cells(services[0])[3]);
  assert.deepStrictEqual(links.map((a) => a.getAttribute('href')), [UI], 'only an https link becomes a link');
  assert.deepStrictEqual(tags.map((a) => a.textContent), ['Redis up', 'Redis latency', 'Redis replicas']);
  assert.strictEqual(tags[0].getAttribute('href'), '#a=' + encodeURIComponent('redis-down|cache/redis-1|up'));
  assert.ok(!doc.getElementById('panel-redis').textContent.includes('err|'), 'cards of other plug-ins stay out');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a filled state lists instances and services with their open Redis cards, and only https graph links';
}

async function anEmptyStateSaysThereIsNoDataYet() {
  //given
  const state = stateWith([{part: 'summary', at: iso(1), source: 'main', plugin: 'redis'}], [], {redis: 'error: instances: Prometheus query HTTP 502'});

  //when
  const {dom, errors, section} = await boot(state);
  const empty = [...section.querySelectorAll('tr.empty')].map((r) => r.textContent);

  //then
  assert.deepStrictEqual(empty, ['No redis_exporter data yet. The Redis check fills this on its next run.', 'No service Redis timings yet.']);
  assert.ok(section.textContent.includes('The last Redis check failed: instances: Prometheus query HTTP 502'), section.textContent);
  assert.strictEqual(section.querySelectorAll('.sig-tags').length, 0);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'an empty state shows the empty rows and the failed part of the last check';
}

async function anEvilRowIsRenderedAsText() {
  //given
  const rows = [{part: 'exporter', ns: EVIL, name: EVIL, up: 1, role: EVIL, plugin: 'redis'},
    {part: 'app', ns: EVIL, job: EVIL, redis: EVIL, source: 'main', ms: 1, calls: 1, errors: 0, link: 'https://x.example.com/"><img src=x onerror="window.__evil=2">', plugin: 'redis'}];
  const anomalies = [card('redis-down|x/y|up', 'crit', {label: EVIL, short: EVIL, key: 'redis-down|' + EVIL})];

  //when
  const {dom, w, errors, section} = await boot(stateWith(rows, anomalies));
  await wait(100);

  //then
  assert.strictEqual(section.querySelectorAll('img').length, 0, 'no element comes from row text');
  assert.strictEqual(w.__evil, undefined);
  assert.ok(section.textContent.includes(EVIL), 'the text shows as written');
  assert.strictEqual(section.querySelector('#redis-apps a.lnk').getAttribute('href'), 'https://x.example.com/"><img src=x onerror="window.__evil=2">');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'an EVIL row and card render as text, attributes included';
}

async function aQuietPassIsNotAPassThatNeverRan() {
  //given
  const quiet = await boot(stateWith([{part: 'summary', at: iso(1), source: 'main', plugin: 'redis'}], [], {redis: 'ok \u00b7 no data'}));

  //when
  const empty = [...quiet.section.querySelectorAll('tr.empty')].map((r) => r.textContent);

  //then
  assert.deepStrictEqual(empty, ['The last Redis check found no redis_exporter series.', 'The last Redis check found no service Redis timings.']);
  assert.deepStrictEqual(quiet.errors, []);
  quiet.dom.window.close();
  return 'a quiet pass says it found nothing, not that it has not run';
}

async function aRefusedPartSaysItWasNotRead() {
  //given
  const b = await boot(stateWith([{part: 'summary', at: iso(1), source: 'main', plugin: 'redis'}], [], {redis: 'ok \u00b7 no access to instances, services'}));

  //when
  const empty = [...b.section.querySelectorAll('tr.empty')].map((r) => r.textContent);

  //then
  assert.deepStrictEqual(empty, ['Not read: no access to instances.', 'Not read: no access to services.']);
  assert.deepStrictEqual(b.errors, []);
  b.dom.window.close();
  return 'a refused part says it was not read, never that there is nothing';
}

(async () => {
  for (const t of [aFilledStateShowsInstancesServicesAndTheirCards, anEmptyStateSaysThereIsNoDataYet, anEvilRowIsRenderedAsText, aQuietPassIsNotAPassThatNeverRan, aRefusedPartSaysItWasNotRead]) {
    console.log('PASS ' + (await t()));
  }
})().catch((e) => { console.error('FAIL', e.message); process.exit(1); });
