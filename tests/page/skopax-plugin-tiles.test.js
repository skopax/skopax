const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const STATIC = path.join(path.dirname(TEMPLATE), 'static');
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

const PLUGINS = [
  {id: 'acme', name: 'Acme <i>metrics</i>', icon: '/plugins/acme/icon.svg', tab: 'svcs', passes: ['acme', 'acme-east'],
    instances: [{name: 'main', primary: true, pass: 'acme', status: 'ok'}, {name: 'east', primary: false, pass: 'acme-east', status: 'error: timed out after 30 s'}]},
  {id: 'cluster', name: 'Cluster', icon: '/plugins/cluster/icon.svg', tab: null, passes: [], instances: [{name: 'local', primary: true, pass: null, status: null}]},
];

async function boot(plugins, hash) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'error: stale'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/' + (hash || ''), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        const path = String(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (path.startsWith('/api/server')) return ok({plugins, tz: 'UTC'});
        if (path.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', operator: true, admin: true, teams: [], tags: []});
        if (path.startsWith('/api/actions') || path.startsWith('/api/deploy-reports') || path.startsWith('/api/requests')) return ok([]);
        if (path.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(250);
  return {w: dom.window, doc: dom.window.document, errors};
}

const tile = (doc, id) => doc.querySelector('#meta .src[data-src="' + id + '"]');

async function tilesComeFromThePluginsList() {
  //given
  const t = await boot(PLUGINS);
  const {doc} = t;

  //then
  assert.deepStrictEqual([...doc.querySelectorAll('#meta .sg-pill')].map((b) => b.dataset.grp), ['integrations']);
  assert.ok(/Integrations/.test(doc.querySelector('#meta .sg-pill').textContent));
  assert.deepStrictEqual([...doc.querySelectorAll('#meta .src')].map((b) => b.dataset.src), ['acme', 'cluster']);
  const acme = tile(doc, 'acme');
  assert.strictEqual(acme.querySelector('img').getAttribute('src'), '/plugins/acme/icon.svg');
  assert.deepStrictEqual([...acme.querySelectorAll('.src-i')].map((s) => [s.dataset.name, s.dataset.state, s.textContent.trim()]), [['main', 'ok', 'main ok'], ['east', 'error', 'east error']]);
  assert.ok(acme.classList.contains('k-crit'), 'one failing instance colours the tile');
  assert.ok(!doc.querySelector('#meta [data-src="prom"]'), 'nothing is hardcoded for prom');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS tiles come from plugins: main ok, east error, with the plug-in icon');
}

async function aPluginWithoutChecksShowsItsInstancesNeutrally() {
  //given
  const t = await boot(PLUGINS);

  //when
  const cluster = tile(t.doc, 'cluster');

  //then
  const rows = [...cluster.querySelectorAll('.src-i')];
  assert.deepStrictEqual(rows.map((s) => [s.dataset.name, s.dataset.state, s.textContent.trim()]), [['local', 'none', 'local no checks']]);
  assert.ok(!cluster.classList.contains('k-crit') && !cluster.classList.contains('k-warn'), 'no checks is not an error');
  assert.ok(cluster.querySelector('img'), 'the tile is not empty');
  t.w.close();
  console.log('PASS a plug-in without checks shows its instances as no checks, never an error');
}

async function valuesAreEscapedAndTheTileOpensItsTab() {
  //given
  const t = await boot(PLUGINS);
  const acme = tile(t.doc, 'acme');

  //then
  assert.ok(!acme.querySelector('i'), 'the plug-in name is escaped');
  assert.ok(acme.textContent.indexOf('<i>metrics</i>') >= 0);

  //when
  acme.click();
  await wait(50);

  //then
  assert.ok(!t.doc.getElementById('panel-svcs').hidden, 'the tile opens its tab');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS tile text is escaped and a click opens the plug-in tab');
}

async function aFailingInstanceShowsInTheBanner() {
  //given
  const t = await boot(PLUGINS);

  //then
  const banner = t.doc.getElementById('banner');
  assert.ok(!banner.hidden && /Acme/.test(banner.textContent) && /timed out after 30 s/.test(banner.textContent));
  assert.ok(!/stale/.test(banner.textContent), 'the old sources map no longer feeds the tiles');
  t.w.close();
  console.log('PASS a failing instance shows in the banner');
}

async function aPassThatDroppedRepeatedCardsIsOkAndRaisesNoBanner() {
  //given
  const dropped = [{id: 'acme', name: 'Acme', icon: '/plugins/acme/icon.svg', tab: 'svcs', passes: ['acme', 'acme-east'],
    instances: [{name: 'main', primary: true, pass: 'acme', status: 'ok'}, {name: 'east', primary: false, pass: 'acme-east', status: 'ok \u00b7 1 card dropped: checkout-service'}]}];
  const odd = [{id: 'acme', name: 'Acme', icon: '/plugins/acme/icon.svg', tab: 'svcs', passes: ['acme'],
    instances: [{name: 'main', primary: true, pass: 'acme', status: 'okay then'}]}];

  //when
  const t = await boot(dropped);
  const u = await boot(odd);

  //then
  const acme = tile(t.doc, 'acme');
  assert.deepStrictEqual([...acme.querySelectorAll('.src-i')].map((s) => [s.dataset.name, s.dataset.state]), [['main', 'ok'], ['east', 'ok']]);
  assert.ok(!acme.classList.contains('k-crit') && !acme.classList.contains('k-warn'), 'a dropped repeat is not an error');
  assert.ok(t.doc.getElementById('banner').hidden, 'no banner for a pass that answered');
  assert.deepStrictEqual([...tile(u.doc, 'acme').querySelectorAll('.src-i')].map((s) => s.dataset.state), ['error']);
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  u.w.close();
  console.log('PASS a pass that dropped repeated cards is ok and raises no banner');
}

async function hostileInstanceNamesAndStatusesStayText() {
  //given
  const evil = '<b>x</b><img src=x onerror=alert(1)>';
  const t = await boot([{id: 'acme', name: 'Acme', icon: '/plugins/acme/icon.svg', tab: 'svcs', passes: ['acme'],
    instances: [{name: evil, primary: true, pass: 'acme', status: 'error: ' + evil}]}]);

  //then
  const acme = tile(t.doc, 'acme');
  assert.strictEqual(acme.querySelectorAll('b').length + acme.querySelectorAll('img[onerror]').length, 0, 'nothing became markup');
  assert.strictEqual(acme.querySelector('.src-i').dataset.name, evil);
  assert.strictEqual(acme.querySelector('.src-i').getAttribute('title'), 'error: ' + evil);
  assert.ok(!t.doc.getElementById('banner').querySelector('b + img, img[onerror]'), 'the banner shows the status as text');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS hostile instance names and error statuses stay text in the tile, its tooltip and the banner');
}

async function aTileNamesTheTabItOpensAndANonAdminTileWithoutATabOpensNothing() {
  //given
  const t = await boot(PLUGINS);
  const cluster = tile(t.doc, 'cluster');
  const before = cluster.getAttribute('title');

  //when
  t.w.setIntView(false, false);
  t.w.setAdminAccess(false);
  const after = tile(t.doc, 'cluster').getAttribute('title');
  tile(t.doc, 'cluster').click();
  await wait(50);

  //then
  assert.strictEqual(tile(t.doc, 'acme').getAttribute('title'), 'Acme <i>metrics</i> \u00b7 opens Services');
  assert.strictEqual(before, 'Cluster \u00b7 opens Integrations');
  assert.strictEqual(after, 'Cluster');
  assert.ok(t.doc.getElementById('panel-integrations').hidden, 'a visitor without a login opens no Integrations page');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS a tile names the tab it opens, and for a non-admin a tile without a tab promises and opens nothing');
}

async function noPluginsMeansNoTiles() {
  //given
  const t = await boot([]);

  //then
  assert.strictEqual(t.doc.querySelectorAll('#meta .src').length, 0);
  assert.strictEqual(t.doc.querySelectorAll('#meta .sg-pill').length, 0);
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS without plug-ins there are no tiles');
}

async function theToolClusterListPutsThePrimaryFirstFromTheData() {
  //given
  const flipped = PLUGINS.map((p) => Object.assign({}, p, {instances: p.instances.map((i) => Object.assign({}, i, {primary: i.name === 'east'}))}));
  const first = await boot(PLUGINS);
  const second = await boot(flipped);
  const by = {alpha: {calls: 1, errors: 0}, east: {calls: 2, errors: 0}, main: {calls: 3, errors: 0}};
  const order = (t) => [...new JSDOM('<div>' + t.w.toolClusters({byCluster: by}) + '</div>').window.document.querySelectorAll('.tclusters')][0].textContent.replace(/\(.*?\)/g, '').split(' \u00b7 ').map((x) => x.trim().split(' ')[0]);

  //then
  assert.deepStrictEqual(order(first), ['main', 'alpha', 'east']);
  assert.deepStrictEqual(order(second), ['east', 'alpha', 'main']);
  first.w.close();
  second.w.close();
  console.log('PASS the tool cluster list puts the primary instance first, resolved from the plugins data');
}

function noProdIsHardcoded() {
  //given
  const files = ['overview.js', 'core.js'].map((f) => fs.readFileSync(path.join(STATIC, f), 'utf8'));

  //then
  files.forEach((text) => assert.ok(!/['"]prom['"]/.test(text), 'no prom entry in the page scripts'));
  console.log('PASS no prom is hardcoded in overview.js or core.js');
}

async function aRefusedInstanceNeedsAttentionAndIsNeverOk() {
  //given
  const plugins = [{id: 'capacity', name: 'Capacity', icon: '/plugins/capacity/icon.svg', tab: null, passes: ['capacity', 'capacity-locked'],
    instances: [{name: 'main', primary: true, pass: 'capacity', status: 'ok \u00b7 no data'}, {name: 'locked', primary: false, pass: 'capacity-locked', status: 'ok \u00b7 no access to volumes, autoscalers'}]}];
  const t = await boot(plugins);
  const {doc} = t;

  //then
  const cap = tile(doc, 'capacity');
  assert.deepStrictEqual([...cap.querySelectorAll('.src-i')].map((s) => [s.dataset.name, s.dataset.state, s.textContent.trim()]), [['main', 'ok', 'main ok'], ['locked', 'warn', 'locked no access']]);
  assert.strictEqual(cap.querySelector('.src-i-warn').getAttribute('title'), 'ok \u00b7 no access to volumes, autoscalers', 'the tooltip names the refused parts');
  assert.ok(cap.classList.contains('k-warn'), 'a refused instance colours the tile amber');
  const pill = doc.querySelector('#meta .sg-pill');
  assert.ok(pill.classList.contains('k-warn') && /0\/1/.test(pill.textContent) && /Capacity no access/.test(pill.textContent), 'the strip counts it under attention');
  const css = fs.readFileSync(TEMPLATE, 'utf8');
  assert.ok(/\.src-i-warn\s*\{[^}]*var\(--warn\)/.test(css), 'the warn instance uses the theme warn colour in both themes');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS a refused instance shows as no access on the strip, counted under attention');
}

async function aDownBrowserNeedsAttentionAndNamesItsPart() {
  //given
  const plugins = [{id: 'website', name: 'Website', icon: '/plugins/website/icon.svg', tab: null, passes: ['browser', 'pagescan-east'],
    instances: [{name: 'main', primary: true, pass: 'browser', status: 'ok \u00b7 browser down'}, {name: 'east', primary: false, pass: 'pagescan-east', status: 'ok \u00b7 no data for browser downtime'}]}];
  const t = await boot(plugins);
  const {doc} = t;

  //then
  const site = tile(doc, 'website');
  assert.deepStrictEqual([...site.querySelectorAll('.src-i')].map((s) => [s.dataset.name, s.dataset.state, s.textContent.trim()]), [['main', 'warn', 'main browser down'], ['east', 'ok', 'east ok']]);
  assert.strictEqual(site.querySelector('.src-i-warn').getAttribute('title'), 'ok \u00b7 browser down');
  assert.ok(site.classList.contains('k-warn'), 'a down browser colours the tile amber');
  const pill = doc.querySelector('#meta .sg-pill');
  assert.ok(pill.classList.contains('k-warn') && /Website browser down/.test(pill.textContent) && !/no access/.test(pill.textContent), 'the strip names the part, not no access');
  assert.ok(doc.getElementById('banner').hidden, 'a down browser is a warning, not a failing check');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS a down browser shows as browser down on the strip, counted under attention');
}

async function theServerHealthDecidesTheTileAsOnAdmin() {
  //given
  const plugins = [
    {id: 'jira', name: 'Jira', icon: '/plugins/jira/icon.svg', tab: null, passes: [], queued: false,
      instances: [{name: 'main', primary: true, pass: null, status: null, state: 'warn', text: 'No access'}]},
    {id: 'teams', name: 'Microsoft Teams', icon: '/plugins/teams/icon.svg', tab: null, passes: [], queued: false,
      instances: [{name: 'seo', primary: true, pass: null, status: null, state: 'error', text: 'error: seo: HTTP 400'}]},
    {id: 'demo', name: 'Demo', icon: '/plugins/demo/icon.svg', tab: null, passes: ['pulse'], queued: true,
      instances: [{name: 'main', primary: true, pass: 'pulse', status: 'ok', state: 'ok', text: 'Healthy'},
        {name: 'east', primary: false, pass: 'pulse-east', status: null, state: 'error', text: 'URL must be an http or https address'},
        {name: 'west', primary: false, pass: 'pulse-west', status: null, state: 'off', text: 'Off \u00b7 set up, paused'}]},
    {id: 'cluster', name: 'Cluster', icon: '/plugins/cluster/icon.svg', tab: null, passes: [], queued: false,
      instances: [{name: 'local', primary: true, pass: null, status: null, state: 'none', text: 'No checks'}]},
    {id: 'tools', name: 'Tools', icon: '/plugins/tools/icon.svg', tab: null, passes: [], queued: false,
      instances: [{name: 'main', primary: true, pass: null, status: null, state: 'ok', text: 'Healthy'}]},
  ];
  const t = await boot(plugins);
  const {doc} = t;
  const rows = (id) => [...tile(doc, id).querySelectorAll('.src-i')].map((s) => [s.dataset.name, s.dataset.state, s.textContent.trim(), s.getAttribute('title')]);

  //then
  assert.deepStrictEqual(rows('jira'), [['main', 'warn', 'main no access', 'No access']]);
  assert.deepStrictEqual(rows('teams'), [['seo', 'error', 'seo error', 'error: seo: HTTP 400']]);
  assert.deepStrictEqual(rows('demo'), [['main', 'ok', 'main ok', 'ok'], ['east', 'error', 'east error', 'URL must be an http or https address'], ['west', 'none', 'west off', 'Off \u00b7 set up, paused']]);
  assert.deepStrictEqual(rows('cluster'), [['local', 'none', 'local no checks', 'no checks']], 'an instance without a check stays neutral');
  assert.deepStrictEqual(rows('tools'), [['main', 'ok', 'main ok', 'ok']], 'the board trusts the server state, as Admin does');
  assert.ok(tile(doc, 'jira').classList.contains('k-warn') && tile(doc, 'teams').classList.contains('k-crit') && tile(doc, 'demo').classList.contains('k-crit'));
  const banner = doc.getElementById('banner');
  assert.ok(!banner.hidden && /Microsoft Teams: seo: HTTP 400/.test(banner.textContent) && /east URL must be an http or https address/.test(banner.textContent), banner.textContent);
  const pill = doc.querySelector('#meta .sg-pill');
  assert.ok(/Jira no access/.test(pill.title) && /2\/5/.test(pill.textContent), pill.textContent);
  assert.deepStrictEqual([...doc.querySelectorAll('#meta .src-q')].map((q) => [q.closest('.src').dataset.src, q.textContent]), [['demo', 'queued']], 'a nudged tile reads queued');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS the server health decides the tile as on Admin: no access, a failed route, a config problem, an off instance and queued');
}

async function aTileWithManyInstancesWrapsInsideThePage() {
  //given
  const css = fs.readFileSync(TEMPLATE, 'utf8');

  //when
  const rule = (css.match(/\n\.src \{[^}]*\}/) || [''])[0];
  const inst = (css.match(/\n\.src-i \{[^}]*\}/) || [''])[0];

  //then
  assert.ok(/flex-wrap:\s*wrap/.test(rule) && /white-space:\s*normal/.test(rule) && /max-width:\s*100%/.test(rule), rule);
  assert.ok(/white-space:\s*nowrap/.test(inst), 'one instance never breaks inside its own words: ' + inst);
  assert.ok(/\n\.src-q \{[^}]*\}/.test(css), 'the queued mark has its own style');
  console.log('PASS a tile with many instances wraps inside the page instead of pushing it sideways at 390 px');
}

async function aFailingSourceKeepsTheStripClosedUntilClicked() {
  //given
  const plugins = [{id: 'teams', name: 'Microsoft Teams', icon: '/plugins/teams/icon.svg', tab: null, passes: [], queued: false,
    instances: [{name: 'seo', primary: true, pass: null, status: null, state: 'error', text: 'error: seo: HTTP 400'}]}];
  const t = await boot(plugins);
  const {doc} = t;
  const pill = doc.querySelector('#meta .sg-pill');
  const body = doc.getElementById('sg-integrations');
  const before = {crit: pill.classList.contains('k-crit'), expanded: pill.getAttribute('aria-expanded'), hidden: body.hidden};

  //when
  pill.click();

  //then
  assert.deepStrictEqual(before, {crit: true, expanded: 'false', hidden: true}, 'an error colours the pill but does not open the strip');
  assert.deepStrictEqual({expanded: pill.getAttribute('aria-expanded'), hidden: doc.getElementById('sg-integrations').hidden}, {expanded: 'true', hidden: false}, 'one click opens it');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS a failing source colours the strip pill and leaves the strip closed until clicked');
}

(async () => {
  await aFailingSourceKeepsTheStripClosedUntilClicked();
  noProdIsHardcoded();
  await tilesComeFromThePluginsList();
  await aPluginWithoutChecksShowsItsInstancesNeutrally();
  await valuesAreEscapedAndTheTileOpensItsTab();
  await aFailingInstanceShowsInTheBanner();
  await aPassThatDroppedRepeatedCardsIsOkAndRaisesNoBanner();
  await hostileInstanceNamesAndStatusesStayText();
  await aTileNamesTheTabItOpensAndANonAdminTileWithoutATabOpensNothing();
  await noPluginsMeansNoTiles();
  await theToolClusterListPutsThePrimaryFirstFromTheData();
  await aRefusedInstanceNeedsAttentionAndIsNeverOk();
  await aDownBrowserNeedsAttentionAndNamesItsPart();
  await theServerHealthDecidesTheTileAsOnAdmin();
  await aTileWithManyInstancesWrapsInsideThePage();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
