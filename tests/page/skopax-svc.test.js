const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

function anomaly(key, svc, sev, extra) {
  return Object.assign({key, svc, label: key.split('|')[0], sev, status: 'open', now: 40, d1: 5, d7: 4, firstSeen: iso(30), lastSeen: iso(1), ns: 'store', src: 'prom', suspects: ['a suspect'], recommend: 'look at it'}, extra || {});
}

function base(rev, anomalies, services) {
  const s = {
    rev,
    updatedAt: iso(0),
    sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sweptAt: '16:18', nextRun: '17:18',
    sources: {prom: 'ok', logs: 'ok', apm: 'ok', cf: 'ok', pmm: 'ok', qan: 'ok', mongo: 'ok'},
    summary: {level: 'warn', headline: 'h', points: [], at: iso(1)},
    services,
    history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: [],
  };
  if (anomalies) s.anomalies = anomalies;
  return s;
}

const svc = (name, ns, kind, status, req, extra) => Object.assign({svc: name, ns, kind, status, req}, extra || {});

async function boot(serve) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  vc.on('error', (e) => errors.push('console.error ' + e));
  const scrolled = [];
  const blob = JSON.stringify(serve.state).replace(/<\//g, '<\\/');
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', blob);
  const dom = new JSDOM(html, {
    url: 'https://skopax.dev.example.com/',
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () { scrolled.push(this); };
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'op@example.com', operator: true, slack: true, slackChannel: '#ops'});
        if (p.startsWith('/api/actions')) return ok([]);
        if (p.startsWith('/api/deploy')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: serve.rev});
        if (p.startsWith('/api/state')) return ok(serve.state);
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(300);
  return {dom, w: dom.window, doc: dom.window.document, errors, scrolled};
}

const rows = (doc) => [...doc.querySelectorAll('#svcs tbody tr')];
const row = (doc, name) => rows(doc).find((r) => r.dataset.svc === name);
const cards = (doc) => [...doc.querySelectorAll('#anoms article.inc')].map((c) => c.dataset.key);
const chip = (doc, f) => doc.querySelector('#svc-chips button[data-f="' + f + '"]');
const selected = (doc) => doc.querySelector('a.tab[data-tab][aria-current="page"]').dataset.tab;
const navigated = (errors) => errors.some((e) => /navigation/i.test(e));
const expanded = (c) => c.querySelector('.inc-title .fold').getAttribute('aria-expanded') === 'true' && !c.querySelector('.inc-more').hidden;

async function warnRowOpensItsCards() {
  //given
  const anomalies = [
    anomaly('traffic-drop|store/catalog-service', 'catalog-service', 'warn', {label: 'requests'}),
    anomaly('log|catalog-service|ERROR|boom', 'catalog-service', 'crit', {label: 'ERROR log', ns: '-', src: 'logs'}),
    anomaly('5xx|store/catalog-service', 'catalog-service', 'warn', {label: '5xx'}),
    anomaly('avg-latency|store/catalog-service', 'catalog-service', 'info', {label: 'average response time'}),
    anomaly('out-err|store/catalog-service', 'catalog-service', 'warn', {label: 'outbound failures'}),
    anomaly('restart|store/catalog-service', 'catalog-service', 'crit', {label: 'restarts', muted: 'muted from the page'}),
    anomaly('gw-err|store/catalog-service', 'catalog-service', 'crit', {label: 'gateway errors', triage: 'handled'}),
    anomaly('traffic-drop|store/pricing-service', 'pricing-service', 'warn', {label: 'requests', parent: 'traffic-drop|store/catalog-service'}),
    anomaly('err|payments/orders-service', 'orders-service', 'warn', {label: 'ERROR count', ns: 'payments'}),
  ];
  const services = [
    svc('pricing-service', 'store', 'app', 'warn', 5000),
    svc('catalog-service', 'store', 'app', 'warn', 9000),
    svc('krakend', 'gw', 'gateway', 'ok', 9900, {deployedAt: iso(60), image: 'abc12345'}),
    svc('orders-service', 'payments', 'app', 'ok', 100),
  ];
  const serve = {rev: 5, state: base(5, anomalies, services)};
  const {w, doc, errors, scrolled, dom} = await boot(serve);
  doc.getElementById('tab-svcs').click();

  //then
  assert.deepStrictEqual(rows(doc).map((r) => r.dataset.svc), ['catalog-service', 'orders-service', 'pricing-service', 'krakend'], 'worst first, then apps, then by requests');
  const prod = row(doc, 'catalog-service');
  assert.ok(prod.classList.contains('clk'), 'non-ok row is clickable');
  const btn = prod.querySelector('td:first-child button.go');
  assert.ok(btn && btn.type === 'button', 'status is a real button, so Tab focuses it and Enter / Space press it');
  assert.strictEqual(btn.textContent, 'Critical 5 cards \u203a', 'worst open card wins; muted and handled ones do not count');
  assert.strictEqual(prod.querySelector('td:first-child').title, 'Critical \u00b7 ERROR log\nWarning \u00b7 requests\nWarning \u00b7 5xx\n+2 more\nClick to see these cards', 'tooltip names the cards behind the status');
  assert.strictEqual(row(doc, 'orders-service').querySelector('td:first-child').title, 'Warning \u00b7 ERROR count\nClick to see this card', 'stored ok is overridden by its open card');
  const pricing = row(doc, 'pricing-service');
  assert.ok(!pricing.classList.contains('clk') && !pricing.querySelector('button') && /Healthy/.test(pricing.textContent), 'a card merged into an open parent does not colour its service, whatever the stored status said');
  assert.match(chip(doc, 'bad').textContent, /Not healthy 2/, 'Not healthy chip counts the live status');
  assert.match(chip(doc, 'deployed').textContent, /Deployed \(24 h\) 1/, 'Deployed (24 h) chip unchanged');
  assert.ok(!doc.getElementById('tiles'), 'no pills on the overview');
  assert.strictEqual(doc.getElementById('count-svcs').textContent, '2/4', 'the Services tab counts healthy / total rows live');
  assert.ok(doc.getElementById('tab-svcs').classList.contains('hot') && doc.getElementById('tab-svcs').classList.contains('k-crit'), 'and is coloured by the worst service: ' + doc.getElementById('tab-svcs').className);
  assert.match(doc.getElementById('tab-svcs').title, /^2 of 4 not healthy: \S+ \(Critical\), orders-service \(Warning\)$/, 'its tooltip names them');

  //when
  chip(doc, 'deployed').click();

  //then
  assert.deepStrictEqual(rows(doc).map((r) => r.dataset.svc), ['krakend'], 'Deployed (24 h) still filters');

  //when
  chip(doc, 'bad').click();
  row(doc, 'catalog-service').querySelectorAll('td')[3].click();

  //then
  assert.strictEqual(selected(doc), 'anoms', 'row click switches to the anomaly list');
  const want = ['log|catalog-service|ERROR|boom', 'traffic-drop|store/catalog-service', '5xx|store/catalog-service', 'out-err|store/catalog-service', 'avg-latency|store/catalog-service'];
  assert.deepStrictEqual(cards(doc), want, 'only that service\u2019s open cards, worst first; muted, handled and merged ones left out');
  assert.match(doc.querySelector('#anoms .grp-h h3').textContent, /^catalog-service \u00b7 open$/, 'group says which service and tier');
  const first = doc.querySelector('#anoms article.inc');
  assert.ok(first.classList.contains('linked') && expanded(first), 'first card ringed and expanded like a Copy-link jump');
  assert.strictEqual(scrolled[scrolled.length - 1], first, 'scrolled to the first card');
  assert.strictEqual(doc.querySelectorAll('#anoms article.inc.linked').length, 1, 'one ring');
  const note = doc.getElementById('filter-note');
  assert.match(note.querySelector('.fn-cur').textContent, /^Showing catalog-service cards5$/, 'the filter line names the service and its card count');
  assert.ok(doc.getElementById('back-svcs') && doc.getElementById('clear-all') && !doc.getElementById('clear-f'), 'with Back to Services and Clear filters, which already goes back to Open');

  //when
  doc.getElementById('back-svcs').click();

  //then
  assert.strictEqual(selected(doc), 'svcs', 'Back to Services returns to the tab');
  assert.strictEqual(chip(doc, 'bad').getAttribute('aria-pressed'), 'true', 'service kind chip kept');
  assert.strictEqual(doc.activeElement, row(doc, 'catalog-service').querySelector('button.go'), 'focus back on the row it came from');
  assert.ok(/^Showing Open/.test(doc.getElementById('filter-note').textContent) && !doc.getElementById('back-svcs'), 'anomaly filter reset to Open, the default');
  assert.strictEqual(doc.querySelector('#panel-anoms .qf-c[aria-pressed="true"]').dataset.f, 'open');

  //when
  row(doc, 'orders-service').querySelector('button.go').click();

  //then
  assert.deepStrictEqual(cards(doc), ['err|payments/orders-service'], 'the status button opens the cards too');

  //when
  doc.getElementById('tab-svcs').click();
  chip(doc, 'all').click();
  row(doc, 'krakend').click();

  //then
  assert.strictEqual(selected(doc), 'svcs', 'an ok row stays a plain row');
  assert.ok(!row(doc, 'krakend').classList.contains('clk') && !row(doc, 'krakend').querySelector('button'));
  assert.ok(!navigated(errors) && !errors.length, 'no errors: ' + errors.join(' | '));
  dom.window.close();
  return 'a warn row opens its open cards (worst first, first ringed), tooltip lists max 3 + more, Back to Services returns to the row; ok rows, Not healthy and Deployed (24 h) unchanged';
}

async function handledCardsClearTheRowOnLiveUpdate() {
  //given
  const parent = 'traffic-drop|store/catalog-service';
  const at1618 = () => [
    anomaly(parent, 'catalog-service', 'warn', {label: 'requests'}),
    anomaly('traffic-drop|store/pricing-service', 'pricing-service', 'warn', {label: 'requests'}),
    anomaly('avg-latency|store/pricing-service', 'pricing-service', 'warn', {label: 'average response time'}),
  ];
  const services = [svc('catalog-service', 'store', 'app', 'warn', 9000), svc('pricing-service', 'store', 'app', 'warn', 5000)];
  const serve = {rev: 5, state: base(5, at1618(), services)};
  const {w, doc, errors, dom} = await boot(serve);
  doc.getElementById('tab-svcs').click();
  assert.deepStrictEqual(rows(doc).filter((r) => r.classList.contains('clk')).map((r) => r.dataset.svc), ['catalog-service', 'pricing-service'], 'both warn at the 16:18 check');
  row(doc, 'pricing-service').click();
  assert.deepStrictEqual(cards(doc), ['traffic-drop|store/pricing-service', 'avg-latency|store/pricing-service']);

  //when
  const later = at1618().map((a) => Object.assign(a, {triage: 'handled'}, a.key === parent ? {} : {parent}));
  serve.rev = 6;
  serve.state = base(6, later, services);
  w.document.dispatchEvent(new w.Event('visibilitychange'));
  await wait(300);

  //then
  assert.strictEqual(serve.state.services[0].status, 'warn', 'the stored status still says warn');
  assert.ok(!doc.getElementById('filter-note').hidden && /pricing-service cards/.test(doc.getElementById('filter-note').textContent), 'the service filter survives the live update');
  assert.match(doc.querySelector('#anoms .grp-h h3').textContent, /^pricing-service \u00b7 in progress, done or muted$/, 'no open card left, so it shows where they went');
  assert.deepStrictEqual(cards(doc), [parent], 'the handled parent that holds the merged pricing cards');
  doc.getElementById('back-svcs').click();
  assert.ok(rows(doc).every((r) => !r.classList.contains('clk') && /Healthy/.test(r.querySelector('td').textContent)), 'both rows healthy without waiting for a check');
  assert.match(chip(doc, 'bad').textContent, /Not healthy 0/);
  assert.strictEqual(doc.getElementById('count-svcs').textContent, '2/2');
  assert.ok(!doc.getElementById('tab-svcs').classList.contains('hot'), 'a healthy Services tab is plain again');
  assert.ok(!navigated(errors) && !errors.length, 'no errors: ' + errors.join(' | '));
  dom.window.close();
  return 'handled + merged cards turn the rows healthy on the next live update although the stored status says warn; an open service filter falls back to the action-taken cards';
}

async function noAnomalyListFallsBackToStoredStatus() {
  //given
  const serve = {rev: 5, state: base(5, null, [svc('orders-service', 'payments', 'app', 'warn', 100), svc('krakend', 'gw', 'gateway', 'ok', 900)])};
  const {doc, errors, dom} = await boot(serve);
  doc.getElementById('tab-svcs').click();

  //then
  const orders = row(doc, 'orders-service');
  assert.ok(orders.classList.contains('clk'), 'stored warn kept when the state has no anomaly list');
  assert.strictEqual(orders.querySelector('button.go').textContent, 'Warning', 'no card count without cards');
  assert.match(orders.querySelector('td').title, /^Status from the last check/);

  //when
  orders.click();

  //then
  assert.match(doc.querySelector('#anoms .grp-h h3').textContent, /^orders-service \u00b7 nothing open$/);
  assert.match(doc.querySelector('#anoms .empty-card').textContent, /Nothing on orders-service any more/);
  assert.ok(!errors.length, 'no errors: ' + errors.join(' | '));
  dom.window.close();
  return 'a state without an anomaly list keeps the stored status; its row still opens an empty, explained view';
}

async function severityChipsFilterTheOpenCards() {
  //given
  const anomalies = [
    anomaly('log|catalog-service|ERROR|boom', 'catalog-service', 'crit', {label: 'ERROR log', ns: '-', src: 'logs'}),
    anomaly('5xx|store/catalog-service', 'catalog-service', 'warn', {label: '5xx'}),
    anomaly('err|payments/orders-service', 'orders-service', 'warn', {label: 'ERROR count', ns: 'payments'}),
    anomaly('restart|store/catalog-service', 'catalog-service', 'crit', {label: 'restarts', muted: 'muted from the page'}),
    anomaly('avg-latency|store/pricing-service', 'pricing-service', 'info', {label: 'average response time', status: 'cleared'}),
  ];
  const serve = {rev: 5, state: base(5, anomalies, [svc('catalog-service', 'store', 'app', 'ok', 9000)])};
  const {doc, errors, dom} = await boot(serve);
  const sevItem = (f) => doc.querySelector('#panel-anoms .qf-c[data-f="' + f + '"]');
  const txt = (b) => b.querySelector('.lbl').textContent + ' ' + b.querySelector('.count').textContent;
  const statusPressed = () => ['open', 'taken', 'all', 'progress', 'muted', 'cleared'].map(sevItem).find((b) => b.getAttribute('aria-pressed') === 'true') || null;

  //then
  assert.deepStrictEqual(['crit', 'high', 'warn'].map(sevItem).map(txt), ['Critical 1', 'High 0', 'Warning 2'], 'open counts on the chips; muted and cleared cards left out, and no Info filter');
  assert.deepStrictEqual(['archived', 'closed'].map(sevItem).map(txt), ['Archived 0', 'Closed '], 'the fold holds Archived and Closed; Closed has no count until its list is loaded');
  assert.deepStrictEqual(['crit', 'high', 'warn', 'archived', 'closed'].map(sevItem).filter((b) => b.classList.contains('zero')).map((b) => b.dataset.f), ['high', 'archived', 'closed'], 'zero items are faded');
  assert.ok(!doc.getElementById('sev-chips') && !doc.getElementById('anom-chips') && !doc.getElementById('filters'), 'the chips row is gone from the Anomalies tab');
  assert.match(sevItem('crit').title, /^open critical items \(pushed\) \u00b7 opens Board filtered to Critical, click again for all open$/);
  const anoms = doc.getElementById('tab-anoms');
  assert.ok(anoms.classList.contains('hot') && anoms.classList.contains('k-crit'), 'the Anomalies tab takes the worst open severity');
  assert.strictEqual(anoms.title, '1 critical \u00b7 2 warning');
  assert.strictEqual(doc.getElementById('filter-note').title, '1 critical \u00b7 2 warning \u00b7 1 muted \u00b7 1 stopped firing', 'the filter line tooltip keeps the counts');

  //when
  sevItem('warn').click();

  //then
  assert.deepStrictEqual(cards(doc), ['5xx|store/catalog-service', 'err|payments/orders-service'], 'Warning shows the open warning cards only');
  assert.strictEqual(sevItem('warn').getAttribute('aria-pressed'), 'true');
  assert.strictEqual(statusPressed(), null, 'Open is not pressed while a severity filters');
  assert.match(doc.getElementById('filter-note').textContent, /^Showing Warning2Copy linkAllClear filters \u2715/);

  //when
  sevItem('warn').click();

  //then
  assert.strictEqual(statusPressed().dataset.f, 'open', 'a second click goes back to all open');
  assert.strictEqual(doc.getElementById('clear-f'), null, 'Open is the default filter, so the line offers no way back');
  assert.ok(!navigated(errors) && !errors.length, 'no errors: ' + errors.join(' | '));
  dom.window.close();
  return 'Critical / Warning / Info chips in the toolbar (counts, zero-faded) filter the open cards and toggle back; no chips row on the tab; the Anomalies tab takes the worst severity with an N critical \u00b7 N warning tooltip; the filter line tooltip keeps the counts';
}

async function inProgressSplitsFromActionTaken() {
  //given
  const anomalies = [
    anomaly('log|reports-service|WARN|trackhub', 'reports-service', 'info', {label: 'WARN pattern', ns: '-', src: 'logs', triage: 'progress', ticket: 'OPS-117', owner: 'Dora Lane'}),
    anomaly('err|identity/auth-service', 'auth-service', 'warn', {label: 'ERROR logs', ns: 'identity', triage: 'handled'}),
    anomaly('5xx|store/catalog-service', 'catalog-service', 'warn', {label: '5xx'}),
  ];
  const serve = {rev: 5, state: base(5, anomalies, [svc('catalog-service', 'store', 'app', 'ok', 9000)])};
  const {doc, errors, dom} = await boot(serve);
  const seg = (f) => doc.querySelector('#panel-anoms .qf-c[data-f="' + f + '"]');

  //then
  assert.deepStrictEqual(['all', 'open', 'progress', 'taken', 'muted', 'cleared'].map(seg).map((b) => b.querySelector('.lbl').textContent + ' ' + b.querySelector('.count').textContent), ['All 3', 'Open 1', 'In progress 1', 'Done 1', 'Muted 0', 'Stopped firing 0'], 'All first, the default; In progress is its own filter, after Open, before Done');
  assert.match(doc.getElementById('filter-note').title, /1 in progress \u00b7 1 done/, 'the filter line tooltip counts the two buckets apart');

  //when
  seg('progress').click();

  //then
  assert.deepStrictEqual(cards(doc), ['log|reports-service|WARN|trackhub'], 'In progress holds the ticketed card only');
  assert.strictEqual(doc.querySelector('#anoms .grp-h h3').textContent, 'In progress', 'its own group title');
  assert.match(doc.getElementById('filter-note').textContent, /^Showing In progress1/);

  //when
  seg('taken').click();

  //then
  assert.deepStrictEqual(cards(doc), ['err|identity/auth-service'], 'Done keeps the handled card and drops the in-progress one');
  assert.strictEqual(doc.querySelector('#anoms .grp-h h3').textContent, 'Done');
  assert.ok(!navigated(errors) && !errors.length, 'no errors: ' + errors.join(' | '));
  dom.window.close();
  return 'In progress is its own chip and group beside Done: a progress card sits there with its ticket, Done holds only the handled ones, and the folded filter line counts both apart';
}

async function sameServiceInSeveralNamespaces() {
  //given
  const anomalies = [
    anomaly('job|members/*|missing', 'batch-service', 'crit', {label: 'batch jobs not running', ns: 'members', src: 'jobs', sub: '*'}),
    anomaly('job|members/sync-contacts|fail', 'batch-service', 'info', {label: 'batch job failed', ns: 'members', src: 'jobs', sub: 'sync-contacts'}),
    anomaly('log|batch-service|WARN|lock wait', 'batch-service', 'info', {label: 'WARN pattern', ns: '-', src: 'logs'}),
  ];
  const services = [
    svc('batch-service', 'members', 'app', 'ok', 300),
    svc('batch-service', 'store', 'app', 'ok', 200),
    svc('batch-service', 'reports', 'app', 'ok', 100, {cluster: 'east'}),
  ];
  const serve = {rev: 7, state: base(7, anomalies, services)};
  const {doc, errors, dom} = await boot(serve);
  doc.getElementById('tab-svcs').click();
  const byNs = (ns) => rows(doc).find((r) => r.dataset.ns === ns);
  const go = (ns) => byNs(ns).querySelector('button.go');

  //then
  assert.deepStrictEqual(rows(doc).map((r) => r.dataset.ns), ['members', 'store', 'reports'], 'the namespace with the critical card first');
  assert.strictEqual(go('members').textContent, 'Critical 3 cards \u203a', 'cards naming members colour only that row, plus the namespace-less log card');
  assert.strictEqual(go('store').textContent, 'Info 1 card \u203a', 'another namespace of the same service gets only the namespace-less card');
  assert.strictEqual(go('reports').textContent, 'Info 1 card \u203a');
  assert.strictEqual(doc.getElementById('count-svcs').textContent, '0/3');

  //when
  go('store').click();

  //then
  assert.deepStrictEqual(cards(doc), ['log|batch-service|WARN|lock wait'], 'its card list leaves out the other namespace\u2019s job cards');

  //when
  doc.getElementById('back-svcs').click();
  go('members').click();

  //then
  assert.deepStrictEqual(cards(doc), ['job|members/*|missing', 'job|members/sync-contacts|fail', 'log|batch-service|WARN|lock wait'], 'the members row lists its own cards, worst first');
  assert.ok(!errors.length, errors.join(' | '));
  dom.window.close();
  return 'a card that names a namespace with Services rows (prom, kube, alert, batch job) colours only the row of that namespace and shows only there; a card without one (logs, APM: ns "-") still colours every row of that service name';
}

async function highSitsBetweenCriticalAndWarning() {
  //given
  const anomalies = [
    anomaly('lag|store/catalog-service', 'catalog-service', 'warn', {label: 'lag'}),
    anomaly('5xx|store/orders-service', 'orders-service', 'high', {label: '5xx'}),
    anomaly('restart|store/pricing-service', 'pricing-service', 'crit', {label: 'restarts'}),
  ];
  const serve = {rev: 7, state: base(7, anomalies, [svc('catalog-service', 'store', 'app', 'ok', 9000), svc('orders-service', 'store', 'app', 'ok', 8000), svc('pricing-service', 'store', 'app', 'ok', 7000)])};
  const {doc, errors, dom} = await boot(serve);
  const sevItem = (f) => doc.querySelector('#panel-anoms .qf-c[data-f="' + f + '"]');
  const chips = ['crit', 'high', 'warn'].map(sevItem).map((b) => b.dataset.f + ':' + b.querySelector('.count').textContent);
  const tpl = require('fs').readFileSync(TEMPLATE, 'utf8');

  //when
  sevItem('high').click();
  const onlyHigh = cards(doc);
  const pill = doc.querySelector('#anoms article.inc .sev');

  //then
  assert.deepStrictEqual(chips, ['crit:1', 'high:1', 'warn:1'], 'High sits between Critical and Warning between the chips');
  assert.ok(sevItem('high').classList.contains('k-high'), 'the High filter wears the high colour');
  assert.deepStrictEqual(onlyHigh, ['5xx|store/orders-service'], 'High shows the open high cards only');
  assert.ok(pill && pill.classList.contains('k-high') && pill.textContent === 'High', 'a high card says High in the high colour');
  assert.strictEqual(doc.getElementById('tab-anoms').title, '1 critical \u00b7 1 high \u00b7 1 warning');
  assert.ok(/\.k-high \{ --c: var\(--high\); --cb: var\(--high-bg\); \}/.test(tpl), 'k-high reads the high tokens');
  assert.strictEqual((tpl.match(/--high: #[0-9a-f]{6}; --high-bg:/g) || []).length, 5, 'the light theme, both dark blocks and both dark sidebar blocks set the high colour');
  assert.ok(/--high: #ff8c42; --high-bg: rgba\(255, 140, 66, 0\.14\);/.test(tpl), 'the sidebar sets it too');
  assert.ok(!errors.length, 'no errors: ' + errors.join(' | '));
  dom.window.close();
  return 'High sits between Critical and Warning: the filter chip and its colour, the card pill, the tab tooltip and the theme tokens';
}

function rgbOf(hex) { return [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)); }
function lumOf(rgb) {
  const c = rgb.map((v) => v / 255).map((v) => (v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)));
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
}
function ratio(a, b) { const x = lumOf(a), y = lumOf(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); }
function hueOf(rgb) {
  const [r, g, b] = rgb.map((v) => v / 255), hi = Math.max(r, g, b), lo = Math.min(r, g, b), d = hi - lo;
  const h = hi === r ? ((g - b) / d) % 6 : hi === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return {hue: (h * 60 + 360) % 360, sat: d / (1 - Math.abs(hi + lo - 1))};
}
function over(rgba, under) {
  const m = /rgba\((\d+), (\d+), (\d+), ([\d.]+)\)/.exec(rgba), a = Number(m[4]);
  return [1, 2, 3].map((i, k) => Math.round(a * Number(m[i]) + (1 - a) * under[k]));
}

async function highIsAClearOrangeApartFromWarning() {
  //given
  const tpl = require('fs').readFileSync(TEMPLATE, 'utf8').replace(/\n\.side \{[^}]*\}\n/, '\n');
  const all = (name) => [...tpl.matchAll(new RegExp('--' + name + ': (#[0-9a-f]{6}|rgba\\([^)]*\\))', 'g'))].map((m) => m[1]);
  const high = all('high'), highBg = all('high-bg'), warn = all('warn'), crit = all('crit'), bar = rgbOf(all('bar-bg')[1]);
  const themes = [
    {name: 'light', surfaces: [all('surface')[0], all('bg')[0], all('sunken')[0], all('raised')[0], all('hover')[0], all('active')[0], highBg[0]].map(rgbOf)},
    {name: 'dark', surfaces: [all('surface')[1], all('bg')[1], all('sunken')[1], all('raised')[1], all('hover')[1], all('active')[1], highBg[1]].map(rgbOf)},
    {name: 'dark by choice', surfaces: [all('surface')[2], all('bg')[2], all('sunken')[2], all('raised')[2], all('hover')[2], all('active')[2], highBg[2]].map(rgbOf)},
    {name: 'sidebar', surfaces: [bar, over(highBg[3], bar), over(all('hover')[3], bar), over(all('active')[3], bar)]},
  ];

  //then
  assert.deepStrictEqual(high, ['#b54400', '#ff7a2e', '#ff7a2e', '#ff8c42', '#ff8c42'], 'a purer orange in light, dark and the sidebar');
  themes.forEach((t, i) => {
    const h = rgbOf(high[i]), tone = hueOf(h), w = hueOf(rgbOf(warn[i])), c = hueOf(rgbOf(crit[i]));
    for (const s of t.surfaces) assert.ok(ratio(h, s) >= 4.5, t.name + ' high text contrast ' + ratio(h, s).toFixed(2) + ' is below 4.5');
    assert.ok(tone.hue >= 20 && tone.hue <= 26 && tone.sat >= 0.95, t.name + ' high is a saturated orange (hue ' + tone.hue.toFixed(0) + ')');
    assert.ok(w.hue - tone.hue >= 20, t.name + ' high and warning sit at least 20 degrees apart');
    assert.ok(tone.hue - c.hue >= 12, t.name + ' high and critical sit apart');
  });
  return 'High is a saturated orange at 20-26 degrees, AA readable on every surface it sits on and set apart from Warning yellow and Critical red';
}

(async () => {
  for (const t of [warnRowOpensItsCards, highIsAClearOrangeApartFromWarning, handledCardsClearTheRowOnLiveUpdate, noAnomalyListFallsBackToStoredStatus, severityChipsFilterTheOpenCards, highSitsBetweenCriticalAndWarning, inProgressSplitsFromActionTaken, sameServiceInSeveralNamespaces]) {
    console.log('PASS ' + (await t()));
  }
})().catch((e) => { console.error('FAIL', e.message); process.exit(1); });
