const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const PANEL = fs.readFileSync(path.join(__dirname, '..', '..', 'skopax', 'plugins', 'cloudflare', 'panel.js'), 'utf8');
const SLOT = '<script type="text/plain" id="plugin-scripts"></script>';
const ICON = '/plugins/cloudflare/icon.svg';
const PLUGINS = [{id: 'cloudflare', name: 'Cloudflare', icon: ICON, tab: 'cf', passes: ['cf', 'edge'], instances: [],
  pages: [{tab: 'cf', label: 'Cloudflare', group: 'observe', icon: ICON}]}];
const DASH = 'https://dash.cloudflare.com/5d0e2f4a6b8c0d1e3f5a7b9c1d2e4f6a';
const EVIL = '<img src=x onerror="window.__pwned=1">';
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const inDays = (d) => new Date(Date.now() + d * 86400000).toISOString().slice(0, 19) + 'Z';

function stateWith(rev, over) {
  return Object.assign({rev, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 14 * 60000).toISOString(), sources: {prom: 'ok', cf: 'ok', edge: 'ok'},
    serverPasses: {cf: {at: iso(3), status: 'ok'}, edge: {at: iso(3), status: 'ok'}},
    summary: {level: 'ok', headline: 'x', points: [], at: iso(1)}, anomalies: [], services: [], history: [], routes: [], deploys: [], cloudflare: [], edge: []}, over);
}

function edgeRow(zone, extra) {
  return Object.assign({zone, tag: 'tag-' + zone, at: iso(70).slice(0, 13) + ':00:00Z', running: false, mins: 60, req: 610000, cached: 540000, cachedPct: 88.52, uniq: 61000,
    conn: 3600, srv: 320, connShare: 0.59016, srvShare: 0.05246, connD1: 0.00339, connD7: 0.00571, evaluated: true, plugin: 'cloudflare',
    link: DASH + '/' + zone + '/analytics/traffic'}, extra || {});
}

function filled(rev, requests) {
  return stateWith(rev, {
    anomalies: [{key: 'cf-health|cloudflare/api-origin|shop.example.com', status: 'open', sev: 'crit', svc: 'api-origin', label: 'health check', short: 'CRIT api-origin shop.example.com health check'},
      {key: 'cf-status|cloudflare/CDN/Cache', status: 'open', sev: 'info', svc: 'CDN/Cache', label: 'Cloudflare status', short: 'INFO CDN/Cache Cloudflare status'},
      {key: 'cf-cert|cloudflare/advanced|shop.example.com|a-1', status: 'open', sev: 'crit', svc: 'advanced', label: 'days to certificate expiry', short: 'CRIT Cloudflare advanced certificate pack'},
      {key: 'edge|shop.example.com|edge-conn', status: 'open', sev: 'crit', svc: 'shop.example.com', label: '502 / 52x share at the edge', short: 'CRIT Cloudflare shop.example.com 502 / 52x'},
      {key: 'edge|blog.example.com|edge-5xx', status: 'cleared', sev: 'warn', svc: 'blog.example.com', label: 'other 5xx share at the edge', short: 'WARN blog'}],
    cloudflare: [
      {part: 'checks', zone: 'shop.example.com', name: 'api-origin', path: '/actuator/health', status: 'unhealthy', reason: 'HTTP 503', plugin: 'cloudflare'},
      {part: 'checks', zone: 'shop.example.com', name: 'web-origin', path: '/', status: 'healthy', reason: null, plugin: 'cloudflare'},
      {part: 'checks', zone: 'docs.example.org', name: 'docs-origin', path: '/', status: 'unknown', reason: 'no data yet', plugin: 'cloudflare', cluster: 'east'},
      {part: 'components', name: 'Tokyo, Japan - (NRT)', status: 'operational', plugin: 'cloudflare'},
      {part: 'components', name: 'CDN/Cache', status: 'degraded_performance', plugin: 'cloudflare'},
      {part: 'certs', zone: 'shop.example.com', id: 'a-1', type: 'advanced', status: 'active', expires: inDays(2), plugin: 'cloudflare'},
      {part: 'certs', zone: 'shop.example.com', id: 'u-1', type: 'universal', status: 'active', expires: inDays(80), plugin: 'cloudflare'},
      {part: 'summary', zones: ['shop.example.com', 'blog.example.com'], missing: [], noHealthChecks: ['blog.example.com'], statusOff: false, plugin: 'cloudflare'}],
    edge: [edgeRow('shop.example.com', {req: requests}), edgeRow('blog.example.com', {req: 3, conn: 2, srv: 0, connShare: 66.67, evaluated: false, link: 'http://dash.example.com/x'}),
      edgeRow('tools.example.net', {req: null, evaluated: false, conn: null, srv: null, connShare: null, cachedPct: null, uniq: null})],
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
  return [...section.querySelectorAll('table[data-cf="' + name + '"] tbody tr')];
}

async function filledPanelShowsChecksStatusCertificatesAndEdgeAndRepaints() {
  //given
  const serve = {rev: 5, state: filled(5, 610000)};
  const {dom, w, doc, errors} = await boot(serve, 'cf');
  const cf = doc.getElementById('panel-cf');

  //when
  const checks = rowsOf(cf, 'checks').map((tr) => tr.textContent);
  const tag = cf.querySelector('table[data-cf="checks"] tbody tr a.tag');
  const components = rowsOf(cf, 'components').map((tr) => tr.textContent);
  const certs = rowsOf(cf, 'certs').map((tr) => tr.textContent);
  const edge = rowsOf(cf, 'edge');
  const links = edge.map((tr) => [...tr.querySelectorAll('a.lnk')].map((a) => a.getAttribute('href')));
  const notes = cf.textContent;
  serve.rev = 6;
  serve.state = filled(6, 720000);
  w.document.dispatchEvent(new w.Event('visibilitychange'));
  await wait(300);
  const repainted = rowsOf(cf, 'edge')[0].textContent;

  //then
  assert.strictEqual(checks.length, 3);
  assert.ok(checks[0].includes('api-origin') && checks[0].includes('shop.example.com') && checks[0].includes('/actuator/health') && checks[0].includes('HTTP 503'), checks[0]);
  assert.strictEqual(tag.getAttribute('href'), '#a=' + encodeURIComponent('cf-health|cloudflare/api-origin|shop.example.com'));
  assert.ok(tag.className.includes('k-crit'));
  assert.ok(checks[1].includes('web-origin') && checks[1].includes('healthy'), checks[1]);
  assert.ok(checks[2].includes('docs-origin') && checks[2].includes('(east)'), checks[2]);
  assert.strictEqual(components.length, 2);
  assert.ok(components[0].includes('Tokyo, Japan - (NRT)') && components[0].includes('operational'), components[0]);
  assert.ok(components[1].includes('CDN/Cache') && components[1].includes('degraded performance'), components[1]);
  assert.strictEqual(certs.length, 2);
  assert.ok(certs[0].includes('advanced') && certs[0].includes('2 days'), certs[0]);
  assert.strictEqual(rowsOf(cf, 'certs')[0].querySelector('a.tag').getAttribute('href'), '#a=' + encodeURIComponent('cf-cert|cloudflare/advanced|shop.example.com|a-1'));
  assert.ok(certs[1].includes('universal') && certs[1].includes('80 days'), certs[1]);
  assert.strictEqual(edge.length, 3);
  assert.ok(['shop.example.com', '610,000', '3,600', '0.59%', 'complete hour'].every((t) => edge[0].textContent.includes(t)), edge[0].textContent);
  assert.strictEqual(edge[0].querySelector('a.tag').getAttribute('href'), '#a=' + encodeURIComponent('edge|shop.example.com|edge-conn'));
  assert.ok(edge[1].textContent.includes('Quiet'), edge[1].textContent);
  assert.ok(edge[2].textContent.includes('no requests in this hour'), edge[2].textContent);
  assert.deepStrictEqual(links[0], [DASH + '/shop.example.com/analytics/traffic']);
  assert.deepStrictEqual(links[1], [], 'a link that is not https is left out');
  assert.ok(notes.includes('blog.example.com') && notes.includes('no health checks'), 'the zones without health checks are named');
  assert.ok(repainted.includes('720,000'), 'a live update repaints the panel: ' + repainted);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the Cloudflare panel shows health checks, status components, certificates and edge traffic with their open cards, and repaints on a live update';
}

async function emptyPanelSaysWhyThereIsNoData() {
  //given
  const serve = {rev: 3, state: stateWith(3, {sources: {prom: 'ok', cf: 'error: Cloudflare API zones HTTP 403', edge: 'ok', 'edge-east': 'error: Cloudflare GraphQL HTTP 401'},
    cloudflare: [{part: 'summary', zones: ['shop.example.com'], missing: ['gone.example.com'], noHealthChecks: [], statusOff: true, plugin: 'cloudflare'}]})};

  //when
  const {dom, doc, errors} = await boot(serve, 'cf');
  const cf = doc.getElementById('panel-cf');

  //then
  assert.strictEqual(rowsOf(cf, 'checks')[0].textContent, 'Cloudflare did not answer on the last check (error: Cloudflare API zones HTTP 403).');
  assert.strictEqual(rowsOf(cf, 'components').length, 0);
  assert.ok(cf.querySelector('[data-cf="status-off"]').textContent.includes('status-page check is off'));
  assert.strictEqual(rowsOf(cf, 'edge')[0].textContent, 'The edge check did not answer on the last check (east: error: Cloudflare GraphQL HTTP 401).');
  assert.ok(cf.textContent.includes('gone.example.com'), 'a zone the token cannot see is named');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'an empty panel names the failed check, the zones not found and a switched-off status check';
}

async function eachInstanceKeepsItsRowsAndAccessIsNamed() {
  //given
  const serve = {rev: 4, state: stateWith(4, {sources: {prom: 'ok', cf: 'ok \u00b7 no access to health checks', 'cf-east': 'error: Cloudflare API zones HTTP 500', edge: 'ok \u00b7 no access to analytics'},
    cloudflare: [
      {part: 'checks', zone: 'shop.example.com', name: 'api-origin', path: '/actuator/health', status: 'unhealthy', reason: 'HTTP 503', plugin: 'cloudflare'},
      {part: 'certs', zone: 'shop.example.com', id: 'u-1', type: 'universal', status: 'active', expires: inDays(80), plugin: 'cloudflare'},
      {part: 'summary', zones: ['shop.example.com', 'blog.example.com'], missing: [], noHealthChecks: [], statusOff: true, plugin: 'cloudflare',
        noAccess: [{part: 'health checks', permission: 'Health Checks Read', zones: ['blog.example.com']}]}]})};

  //when
  const {dom, doc, errors} = await boot(serve, 'cf');
  const cf = doc.getElementById('panel-cf');
  const checks = rowsOf(cf, 'checks').map((tr) => tr.textContent);

  //then
  assert.strictEqual(checks.length, 1);
  assert.ok(checks[0].includes('api-origin'), 'the rows of an instance that answered stay listed: ' + checks[0]);
  assert.ok(cf.querySelector('[data-cf="failed-cf"]').textContent.includes('east: error: Cloudflare API zones HTTP 500'));
  assert.ok(cf.textContent.includes('the token lacks Health Checks Read') && cf.textContent.includes('blog.example.com'), 'a refused part is named with its permission');
  assert.ok(cf.textContent.includes('no access to analytics'), 'a refused edge read is named');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'each instance keeps its own rows when another fails, and a refused part is named with the permission it needs';
}

async function refusedPartsSayTheyWereNotRead() {
  //given
  const serve = {rev: 4, state: stateWith(4, {sources: {prom: 'ok', cf: 'ok \u00b7 no access to health checks, certificates', edge: 'ok \u00b7 no access to analytics'},
    cloudflare: [{part: 'summary', zones: ['shop.example.com'], missing: [], noHealthChecks: [], statusOff: true, plugin: 'cloudflare',
      noAccess: [{part: 'health checks', permission: 'Health Checks Read', zones: ['shop.example.com']}, {part: 'certificates', permission: 'SSL and Certificates Read', zones: ['shop.example.com']}]}]})};

  //when
  const {dom, doc, errors} = await boot(serve, 'cf');
  const cf = doc.getElementById('panel-cf');

  //then
  assert.strictEqual(rowsOf(cf, 'checks')[0].textContent, 'Not read: no access to health checks.');
  assert.strictEqual(rowsOf(cf, 'certs')[0].textContent, 'Not read: no access to certificates.');
  assert.strictEqual(rowsOf(cf, 'edge')[0].textContent, 'Not read: no access to analytics.');
  assert.ok(!cf.textContent.includes('No health checks on the watched zones') && !cf.textContent.includes('No edge data yet'));
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a refused part says it was not read, never that there is nothing';
}

async function hostileRowsAreShownAsText() {
  //given
  const serve = {rev: 2, state: stateWith(2, {
    anomalies: [{key: 'cf-health|cloudflare/' + EVIL + '|' + EVIL, status: 'open', sev: 'crit', svc: EVIL, label: EVIL, short: EVIL}],
    cloudflare: [{part: 'checks', zone: EVIL, name: EVIL, path: EVIL, status: EVIL, reason: EVIL, cluster: EVIL},
      {part: 'components', name: EVIL, status: EVIL}, {part: 'certs', zone: EVIL, type: EVIL, status: EVIL, expires: EVIL},
      {part: 'summary', zones: [EVIL], missing: [EVIL], noHealthChecks: [EVIL], statusOff: false, noAccess: [{part: EVIL, permission: EVIL, zones: [EVIL]}]}],
    edge: [edgeRow(EVIL, {link: 'javascript:alert(1)', at: EVIL}), edgeRow('shop.example.com', {link: 'https://dash.example.com/" onmouseover="window.__pwned=1'})],
  })};

  //when
  const {dom, w, doc, errors} = await boot(serve, 'cf');
  const cf = doc.getElementById('panel-cf');

  //then
  assert.strictEqual(cf.querySelectorAll('img').length, 0);
  assert.strictEqual(w.__pwned, undefined);
  assert.ok(rowsOf(cf, 'checks')[0].textContent.includes(EVIL) && rowsOf(cf, 'components')[0].textContent.includes(EVIL) && rowsOf(cf, 'certs')[0].textContent.includes(EVIL));
  assert.ok(rowsOf(cf, 'edge')[0].textContent.includes(EVIL));
  assert.ok(![...cf.querySelectorAll('a')].some((a) => /^javascript:/i.test(a.getAttribute('href') || '') || a.hasAttribute('onmouseover')));
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'row values, card labels and links from the state are rendered as text, never as markup';
}

async function aCardOfASecondInstanceStaysWithItsOwnRow() {
  //given
  const health = (zone, name, extra) => Object.assign({part: 'checks', zone, name, path: '/', status: 'healthy', reason: null, plugin: 'cloudflare'}, extra || {});
  const card = (key, extra) => Object.assign({key, status: 'open', sev: 'crit', svc: 'origin', label: 'health check', short: 'CRIT origin health check'}, extra || {});
  const serve = {rev: 5, state: stateWith(5, {
    cloudflare: [health('shop.example.com', 'origin'), health('shop.example.com', 'origin', {cluster: 'east'})],
    anomalies: [card('cf-health|cloudflare/origin|shop.example.com@east', {cluster: 'east'})],
    edge: [edgeRow('shop.example.com'), edgeRow('shop.example.com', {cluster: 'east', tag: 'tag-east'})]})};
  serve.state.anomalies.push({key: 'edge|shop.example.com|edge-conn@east', cluster: 'east', status: 'open', sev: 'crit', svc: 'shop.example.com', label: '502 / 52x share at the edge', short: 'CRIT edge'});

  //when
  const {dom, doc, errors} = await boot(serve, 'cf');
  const cf = doc.getElementById('panel-cf');
  const checks = rowsOf(cf, 'checks').map((tr) => [tr.textContent.includes('(east)'), tr.querySelectorAll('.sig-tags a.tag').length]);
  const edge = rowsOf(cf, 'edge').map((tr) => [tr.textContent.includes('(east)'), tr.querySelectorAll('.sig-tags a.tag').length]);

  //then
  assert.deepStrictEqual(checks, [[false, 0], [true, 1]], 'the east card shows on the east row only');
  assert.deepStrictEqual(edge, [[false, 0], [true, 1]], 'the east edge card shows on the east edge row only');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a card of a second instance shows on that instance\'s row only';
}

async function certificatesAndStatusNeverClaimAReadThatDidNotHappen() {
  //given
  const never = {rev: 5, state: stateWith(5, {sources: {prom: 'ok'}, serverPasses: {}})};
  const failed = {rev: 5, state: stateWith(5, {sources: {prom: 'ok', cf: 'error: zones: Cloudflare API zones HTTP 503'}})};

  //when
  const a = await boot(never, 'cf');
  const b = await boot(failed, 'cf');
  const text = (t, part) => rowsOf(t.doc.getElementById('panel-cf'), part)[0].textContent;

  //then
  assert.strictEqual(text(a, 'certs'), 'No Cloudflare data yet. The cf check fills this on its next run.');
  assert.strictEqual(text(a, 'components'), 'No Cloudflare data yet. The cf check fills this on its next run.');
  assert.strictEqual(text(b, 'certs'), 'Cloudflare did not answer on the last check (error: zones: Cloudflare API zones HTTP 503).');
  assert.strictEqual(text(b, 'components'), 'Cloudflare did not answer on the last check (error: zones: Cloudflare API zones HTTP 503).');
  assert.deepStrictEqual(a.errors.concat(b.errors), []);
  a.dom.window.close();
  b.dom.window.close();
  return 'the certificate and status tables say the check has not run or failed, never that there was nothing';
}

async function anEmptyTableSaysDidNotAnswerOnlyWhenItsOwnPartFailed() {
  //given
  const summary = {part: 'summary', zones: ['shop.example.com'], missing: [], noHealthChecks: [], statusOff: false, plugin: 'cloudflare'};
  const status = {rev: 7, state: stateWith(7, {sources: {prom: 'ok', cf: 'error: status: Cloudflare status page HTTP 503'}, cloudflare: [summary]})};
  const certs = {rev: 7, state: stateWith(7, {sources: {prom: 'ok', 'cf-east': 'error: certificates: Cloudflare API certificate packs HTTP 502; health checks: HTTP 500'}, cloudflare: [summary]})};

  //when
  const a = await boot(status, 'cf');
  const b = await boot(certs, 'cf');
  const text = (t, part) => rowsOf(t.doc.getElementById('panel-cf'), part)[0].textContent;

  //then
  assert.strictEqual(text(a, 'certs'), 'No certificate packs on the watched zones.');
  assert.strictEqual(text(a, 'components'), 'Cloudflare did not answer on the last check (error: status: Cloudflare status page HTTP 503).');
  assert.strictEqual(text(b, 'certs'), 'Cloudflare did not answer on the last check (east: error: certificates: Cloudflare API certificate packs HTTP 502; health checks: HTTP 500).');
  assert.strictEqual(text(b, 'components'), 'None of the watched components was on the Cloudflare status page on the last check.');
  assert.deepStrictEqual(a.errors.concat(b.errors), []);
  a.dom.window.close();
  b.dom.window.close();
  return 'an empty certificate or status table says Cloudflare did not answer only when its own part failed';
}

async function anEmptyHealthCheckTableSaysDidNotAnswerOnlyWhenItsOwnPartFailed() {
  //given
  const summary = {part: 'summary', zones: ['shop.example.com'], missing: [], noHealthChecks: [], statusOff: false, plugin: 'cloudflare'};
  const status = {rev: 7, state: stateWith(7, {sources: {prom: 'ok', cf: 'error: status: Cloudflare status page HTTP 503'}, cloudflare: [summary]})};
  const checks = {rev: 7, state: stateWith(7, {sources: {prom: 'ok', 'cf-east': 'error: certificates: HTTP 502; health checks: Cloudflare API health checks HTTP 500'}, cloudflare: [summary]})};
  const zones = {rev: 7, state: stateWith(7, {sources: {prom: 'ok', cf: 'error: zones: Cloudflare API zones HTTP 500'}, cloudflare: [summary]})};

  //when
  const a = await boot(status, 'cf');
  const b = await boot(checks, 'cf');
  const c = await boot(zones, 'cf');
  const text = (t) => rowsOf(t.doc.getElementById('panel-cf'), 'checks')[0].textContent;

  //then
  assert.strictEqual(text(a), 'No health checks on the watched zones.');
  assert.strictEqual(text(b), 'Cloudflare did not answer on the last check (east: error: certificates: HTTP 502; health checks: Cloudflare API health checks HTTP 500).');
  assert.strictEqual(text(c), 'Cloudflare did not answer on the last check (error: zones: Cloudflare API zones HTTP 500).');
  assert.deepStrictEqual(a.errors.concat(b.errors, c.errors), []);
  [a, b, c].forEach((t) => t.dom.window.close());
  return 'an empty health-check table says Cloudflare did not answer only when the zones or the health checks failed';
}

async function anInvalidTokenIsNamedAsInvalidOrLacking() {
  //given
  const label = 'Cloudflare cannot read analytics: the token is invalid or lacks Analytics Read';
  const card = {key: 'no-access|-/edge|analytics', src: 'edge', sig: 'no-access', ns: '-', svc: 'edge', sub: 'analytics', sev: 'warn', status: 'open', label, short: 'WARN ' + label, now: 1, firstSeen: iso(30), lastSeen: iso(5)};
  const serve = {rev: 6, state: stateWith(6, {sources: {prom: 'ok', cf: 'ok \u00b7 no access to zones', edge: 'ok \u00b7 no access to analytics'}, anomalies: [card],
    cloudflare: [{part: 'summary', zones: [], missing: [], noHealthChecks: [], statusOff: true, plugin: 'cloudflare', noAccess: [{part: 'zones', permission: 'Zone Read', zones: [], invalid: true}]}]})};

  //when
  const {dom, doc, errors} = await boot(serve, 'cf');
  const cf = doc.getElementById('panel-cf');
  const notes = [...cf.querySelectorAll('[data-cf="no-access"]')].map((p) => p.textContent);

  //then
  assert.deepStrictEqual(notes, ['Cloudflare cannot read zones: the token is invalid or lacks Zone Read.', label + '.']);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a 401 is named as an invalid or lacking token in the zone and edge notes';
}

(async () => {
  for (const t of [filledPanelShowsChecksStatusCertificatesAndEdgeAndRepaints, emptyPanelSaysWhyThereIsNoData, eachInstanceKeepsItsRowsAndAccessIsNamed, refusedPartsSayTheyWereNotRead, hostileRowsAreShownAsText, aCardOfASecondInstanceStaysWithItsOwnRow, certificatesAndStatusNeverClaimAReadThatDidNotHappen, anEmptyTableSaysDidNotAnswerOnlyWhenItsOwnPartFailed, anEmptyHealthCheckTableSaysDidNotAnswerOnlyWhenItsOwnPartFailed, anInvalidTokenIsNamedAsInvalidOrLacking]) {
    console.log('PASS ' + (await t()));
  }
})().catch((e) => { console.error('FAIL', e.message); process.exit(1); });
