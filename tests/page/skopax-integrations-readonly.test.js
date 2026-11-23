const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const NOTE = 'Only admins can change integrations.';

function plugin() {
  return {id: 'prometheus', name: 'Prometheus', version: '0.1.0', category: 'metrics', description: 'Metrics', origin: 'builtin', state: 'ok', reason: null, enabled: true, icon: false,
    fields: [{key: 'url', label: 'URL', kind: 'url', required: true, default: '', help: '', choices: [], showIf: null},
      {key: 'token', label: 'Token', kind: 'secret', required: false, default: '', help: '', choices: [], showIf: null},
      {key: 'namespaces', label: 'Namespaces', kind: 'text', required: false, default: '', help: '', choices: [], showIf: null}],
    checks: [{id: 'prom', label: 'Errors and latency', period: 300, offset: 0, ran: []}, {id: 'restarts', label: 'Restarts', period: 300, offset: 0, ran: []}],
    rules: [{id: 'floors', label: 'Rate floors', defaults: {err: 10, warn: 100}, help: {}, values: {err: 15, warn: 100}}], tools: [],
    setup: {steps: ['Point it at Prometheus.'], docs: 'https://prometheus.example.com/docs', docsLabel: 'Guide'},
    instances: [{name: 'main', primary: true, enabled: true, source: 'admin', problem: null, values: {url: 'https://prometheus.example.com', namespaces: 'shop'}, secrets: {token: {set: true}}, offChecks: ['restarts']}],
    health: {state: 'ok', text: 'Connected', at: iso(1), instances: [{name: 'main', state: 'ok', text: 'Connected', at: iso(1)}]}};
}

async function boot(role, hash, plugins, notices) {
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const admin = role === 'admin', anon = role === 'anon';
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/' + (hash || '#t=anoms'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, opts) => {
        const p = String(path), method = (opts && opts.method) || 'GET';
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        const no = (status) => ({ok: false, status, json: async () => ({detail: 'no'})});
        if (p.startsWith('/api/admin') || p.startsWith('/api/integrations')) calls.push([method, p]);
        if (p.startsWith('/api/me')) return ok(anon ? {loginEnabled: true, user: null, teams: [], tags: []} : {loginEnabled: true, user: 'dora.lane@example.com', userRole: role, operator: role !== 'viewer', admin, teams: [], tags: []});
        if (p === '/api/integrations/custom-checks') return anon ? no(401) : ok({readOnly: true, items: [{id: 'redis-memory', name: 'Redis memory', source: 'prometheus', instance: 'main', op: '>', threshold: 90, severity: 'warn', enabled: true, run: {state: 'ok', series: 2, cards: 0}}],
          limits: {checks: 50, periodSeconds: 300}, choices: {}, presets: []});
        if (p === '/api/integrations') return anon ? no(401) : ok({edition: 'community', readOnly: true, plugins: plugins || [plugin()]});
        if (p.startsWith('/api/admin')) {
          if (!admin) return no(403);
          if (p === '/api/admin/plugins') return ok({edition: 'community', secretsKey: 'generated', plugins: plugins || [plugin()]});
          if (p.startsWith('/api/admin/custom-checks')) return ok({items: [], limits: {checks: 50, periodSeconds: 300}, choices: {}, presets: []});
          if (p === '/api/admin/notices') return ok({items: notices || []});
          if (p.startsWith('/api/admin/settings')) return ok({items: [], rev: 1, refreshSeconds: 15});
          return ok([]);
        }
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/nodes')) return ok({nodes: []});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors, calls};
}

async function theConnectRowShowsForEverySignedInRoleOnly() {
  const seen = {};
  for (const role of ['anon', 'viewer', 'operator', 'admin']) {
    //given
    const t = await boot(role);
    const row = t.doc.getElementById('tab-integrations');

    //then
    seen[role] = !!row && !row.hidden && row.closest('[data-grp]').dataset.grp;
    assert.deepStrictEqual(t.errors, []);
    t.w.close();
  }
  assert.deepStrictEqual(seen, {anon: false, viewer: 'connect', operator: 'connect', admin: 'connect'});
  return 'the Connect group has an Integrations row for viewers, operators and admins, and none without a login';
}

async function aViewerOpensTheSamePageReadOnly() {
  //given
  const {w, doc, errors, calls} = await boot('viewer');

  //when
  doc.getElementById('tab-integrations').click();
  await wait(150);

  //then
  assert.ok(!doc.getElementById('panel-integrations').hidden, 'the Integrations page shows');
  assert.ok(doc.getElementById('adm-shell').hidden && !doc.getElementById('panel-integrations').closest('#adm-shell'), 'outside Admin');
  assert.strictEqual(doc.getElementById('crumb-page').textContent, 'Integrations');
  assert.strictEqual(doc.getElementById('tab-integrations').getAttribute('aria-current'), 'page');
  assert.deepStrictEqual(calls.filter((c) => c[1].startsWith('/api/admin/plugins') || c[1].startsWith('/api/admin/custom-checks')), [], 'no admin reads');
  assert.ok(calls.some((c) => c[1] === '/api/integrations') && calls.some((c) => c[1] === '/api/integrations/custom-checks'));
  const box = doc.getElementById('integrations');
  assert.ok(!doc.getElementById('int-add-btn') || !box.contains(doc.getElementById('int-add-btn')), 'no + Add integration');
  assert.strictEqual(box.querySelector('.int-ro-note').textContent, NOTE);
  const sw = box.querySelector('.int-tile .int-switch');
  assert.strictEqual(sw.getAttribute('aria-disabled'), 'true');
  assert.strictEqual(sw.getAttribute('aria-checked'), 'true', 'the switch still shows the plug-in is on');

  //when
  sw.click();
  await wait(40);

  //then
  assert.deepStrictEqual(calls.filter((c) => c[0] !== 'GET'), [], 'a disabled switch sends nothing');
  const pqc = doc.getElementById('pqc');
  assert.ok(pqc.querySelector('.pqc-row[data-id="redis-memory"]'), 'the custom checks list shows');
  assert.ok(!pqc.querySelector('.pqc-add, .pqc-from-preset, .pqc-edit'), 'without Add, preset add or Edit');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a viewer opens the same Integrations page read-only: one note, disabled switches, custom checks without Add or Edit, and no admin calls';
}

async function theReadOnlyPanelShowsInstancesChecksThresholdsAndSecretsAsSet() {
  //given
  const {w, doc, errors, calls} = await boot('operator', '#t=integrations');
  await wait(100);

  //when
  w.intOpen('prometheus', undefined, {tile: 'prometheus'});
  await wait(40);
  const panel = doc.getElementById('int-panel');

  //then
  assert.ok(!doc.getElementById('panel-integrations').hidden, 'a link opens the read-only page');
  assert.strictEqual(panel.querySelector('.int-ro-note').textContent, NOTE);
  assert.strictEqual(panel.querySelector('.int-itab').textContent, 'main');
  assert.strictEqual(panel.querySelector('.int-icard-e').textContent, 'Viewing');
  assert.ok(!panel.querySelector('.int-inst-ro .int-inst-h'), 'one instance is named once, on its card, not again below it');
  assert.ok(panel.querySelector('.int-setup'), 'the setup guide shows');
  const value = (key) => panel.querySelector('.int-ro-fields dd[data-key="' + key + '"]').textContent;
  assert.deepStrictEqual([value('url'), value('token'), value('namespaces')], ['https://prometheus.example.com', 'set', 'shop']);
  assert.deepStrictEqual([...panel.querySelectorAll('.int-ro-checks li')].map((li) => li.dataset.check + '=' + li.querySelector('.tag').textContent), ['prom=On', 'restarts=Off']);
  assert.deepStrictEqual([...panel.querySelectorAll('.int-ro-rule tr')].map((tr) => [...tr.children].map((c) => c.textContent).join('|')), ['err|15|default 10', 'warn|100|']);
  assert.strictEqual(panel.querySelectorAll('input, select, textarea, form').length, 0, 'nothing to edit');
  assert.ok(!panel.querySelector('.int-save, .int-test, .int-del, .int-add, .int-on, .int-primary-btn, .int-turn-on'), 'no Save, Test, Delete, Add, toggle or Make primary');
  assert.deepStrictEqual(calls.filter((c) => c[1].startsWith('/api/admin')), []);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the read-only panel shows instances, which checks are on, thresholds, the setup guide and secrets only as set';
}

async function anAdminStillGetsTheEditablePage() {
  //given
  const {w, doc, errors, calls} = await boot('admin');

  //when
  doc.getElementById('tab-integrations').click();
  await wait(150);

  //then
  assert.ok(!doc.getElementById('panel-integrations').hidden);
  assert.ok(doc.getElementById('adm-shell').hidden);
  assert.strictEqual(doc.getElementById('crumb-page').textContent, 'Integrations');
  assert.ok(!doc.querySelector('#adm-nav [data-tab="integrations"]'), 'Admin no longer lists an Integrations section');
  assert.ok(calls.some((c) => c[1] === '/api/admin/plugins') && !calls.some((c) => c[1] === '/api/integrations'));
  assert.ok(doc.getElementById('int-add-btn') && !doc.querySelector('#integrations .int-ro-note'));
  assert.notStrictEqual(doc.querySelector('#integrations .int-switch').getAttribute('aria-disabled'), 'true');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'an admin following the Connect Integrations row gets the editable page, and Admin no longer lists it';
}

async function aVisitorWithoutLoginCannotOpenIt() {
  //given
  const {w, doc, errors, calls} = await boot('anon', '#t=integrations');

  //then
  assert.ok(doc.getElementById('panel-integrations').hidden, 'the link falls back to the board');
  assert.ok(!doc.getElementById('panel-anoms').hidden);
  assert.deepStrictEqual(calls.filter((c) => c[1].startsWith('/api/integrations')), []);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a visitor without a login is sent to the board and makes no integrations call';
}


function knowledgePlugins(withMcp) {
  const base = (id, name, category, extra) => Object.assign(plugin(), {id, name, category, rules: [], checks: [], setup: null,
    instances: [{name: 'main', primary: true, enabled: true, source: 'admin', problem: null, values: {url: 'https://' + id + '.example.com'}, secrets: {}, offChecks: []}],
    health: {state: 'ok', text: 'Connected', at: iso(1), instances: [{name: 'main', state: 'ok', text: 'Connected', at: iso(1)}]}}, extra);
  return [plugin(), base('jira', 'Jira', 'tickets', {companyKnowledge: 'always', knowledgeAlso: true}), base('confluence', 'Confluence', 'other', {companyKnowledge: 'always', knowledgeAlso: false})].concat(withMcp ? [base('mcp-tools', 'MCP server', 'other', {})] : []);
}

async function everyKnowledgeSourceCarriesABadgeInItsOwnCategoryAndSitsOnceInTheChip() {
  const said = [];
  for (const role of ['admin', 'viewer']) {
    //given
    const {w, doc, errors} = await boot(role, '#t=integrations', knowledgePlugins());
    await wait(100);
    const tilesOf = (id) => doc.querySelectorAll('#integrations .int-tile[data-id="' + id + '"]');
    const tag = (id) => doc.querySelector('#integrations .int-tile[data-id="' + id + '"] .int-know-tag');
    const ids = [...doc.querySelectorAll('#integrations [id]')].map((x) => x.id);

    //then
    assert.ok(!doc.querySelector('#integrations [data-group="knowledge"]'), role + ': no Company knowledge group');
    assert.deepStrictEqual(['jira', 'confluence', 'prometheus'].map((id) => tilesOf(id).length), [1, 1, 1], role + ': each plug-in is one tile');
    assert.deepStrictEqual(['jira', 'confluence'].map((id) => [tag(id).textContent, tag(id).title]), [['Knowledge', 'Knowledge source: the investigator searches and cites it'], ['Knowledge', 'Knowledge source: the investigator searches and cites it']], role + ': Jira and Confluence carry the badge');
    assert.ok(!tag('prometheus'), 'a plain plug-in carries none');
    assert.strictEqual(new Set(ids).size, ids.length, 'no duplicate DOM id');

    //when
    [...doc.querySelectorAll('#integrations .int-chip')].filter((c) => c.textContent === 'Knowledge')[0].click();

    //then
    assert.deepStrictEqual([...doc.querySelectorAll('#integrations .int-tile')].filter((t) => !t.hidden).map((t) => t.dataset.id).sort(), ['confluence', 'jira'], 'the Knowledge chip lists both once');
    if (role === 'admin') {
      //when
      doc.getElementById('int-add-btn').click();
      const pick = (id) => doc.querySelector('#int-pop .int-pick[data-id="' + id + '"]');

      //then
      assert.strictEqual(pick('jira').closest('.int-cat-grp').dataset.cat, 'tickets', '+ Add integration lists Jira in its category');
      assert.ok(pick('jira').querySelector('.int-know-tag') && pick('confluence').querySelector('.int-know-tag'), 'both with the badge');
      assert.ok(!doc.querySelector('#int-pop [data-cat="knowledge"]'), 'and no knowledge group');
    }
    assert.deepStrictEqual(errors, []);
    w.close();
    said.push(role);
  }
  return 'Jira and Confluence each sit once in their own category with a Knowledge badge for ' + said.join(' and ') + ', no knowledge group, the Knowledge chip lists them and + Add integration badges them';
}

async function theAddCustomMcpButtonShowsForAnAdminOnlyWhileTheMcpPluginServes() {
  //given
  const admin = await boot('admin', '#t=integrations', knowledgePlugins(true));
  const viewer = await boot('viewer', '#t=integrations', knowledgePlugins(true));
  const without = await boot('admin', '#t=integrations', knowledgePlugins());
  const broken = knowledgePlugins(true);
  broken.filter((x) => x.id === 'mcp-tools')[0].state = 'broken';
  const down = await boot('admin', '#t=integrations', broken);
  await wait(100);
  const btn = (t) => t.doc.querySelector('#integrations #int-add-mcp');

  //then
  assert.ok(btn(admin) && btn(admin).textContent === '+ Add custom MCP' && btn(admin).classList.contains('btn') && btn(admin).previousElementSibling.classList.contains('int-picker'), 'an admin sees it next to + Add integration');
  assert.ok(!btn(viewer) && !viewer.doc.getElementById('int-add-btn'), 'a read-only viewer sees neither');
  assert.ok(!btn(without), 'hidden when the MCP server plug-in is not listed');
  assert.ok(!btn(down), 'hidden when the MCP server plug-in is unavailable');
  [admin, viewer, without, down].forEach((t) => { assert.deepStrictEqual(t.errors, []); t.w.close(); });
  return 'the Add custom MCP button shows for an admin next to + Add integration, and not for a viewer or without a working MCP server plug-in';
}

async function aJiraOnlyInstallKeepsThePlainGridAndTheChip() {
  //given
  const plugins = knowledgePlugins().filter((p) => p.id !== 'confluence');
  const {w, doc, errors} = await boot('admin', '#t=integrations', plugins);
  await wait(100);

  //then
  assert.ok(!doc.querySelector('#integrations .int-group'), 'no groups: one plain grid');
  assert.ok([...doc.querySelectorAll('#integrations .int-chip')].some((c) => c.textContent === 'Knowledge'), 'the Knowledge chip still counts Jira');
  assert.ok(doc.querySelector('#integrations .int-tile[data-id="jira"] .int-know-tag'));
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'with Jira as the only knowledge source the tiles stay one plain grid, with the Knowledge chip and badge';
}

async function anAdminSeesTheNoticesOnTheIntegrationsPage() {
  //given
  const notes = [{id: 'locale', level: 'warn', text: 'The de language pack of Demo has 2 texts that show in English instead.'}];
  const admin = await boot('admin', '#t=integrations', null, notes);
  const viewer = await boot('viewer', '#t=integrations', null, notes);
  await wait(100);
  const box = (d) => d.getElementById('int-notices');

  //then
  assert.ok(!box(admin.doc).hidden && box(admin.doc).textContent.includes('language pack of Demo'), 'an admin sees the notices strip on Connect > Integrations');
  assert.ok(box(viewer.doc).hidden && !box(viewer.doc).textContent, 'a viewer does not');
  assert.deepStrictEqual(admin.errors.concat(viewer.errors), []);
  admin.w.close(); viewer.w.close();
  return 'the admin notices strip shows on Connect > Integrations for admins only';
}

async function anAdminCanPreviewThePageAsReadOnlyFromTheSameDataOthersGet() {
  //given
  const t = await boot('admin', '#t=integrations');
  const btn = () => t.doc.getElementById('int-preview');
  const viewer = await boot('viewer', '#t=integrations');

  //when
  const before = {text: btn().textContent, pressed: btn().getAttribute('aria-pressed'), add: !!t.doc.querySelector('#int-add-btn, #int-add-mcp'), note: !!t.doc.querySelector('#integrations .int-ro-note')};
  t.calls.length = 0;
  btn().click();
  await wait(200);
  const on = {text: btn().textContent, pressed: btn().getAttribute('aria-pressed'), note: (t.doc.querySelector('#integrations .int-ro-note') || {}).textContent, read: t.calls.some((c) => c[1] === '/api/integrations'), admin: t.calls.some((c) => c[1] === '/api/admin/plugins'),
    edits: t.doc.querySelectorAll('#int-add-btn, #int-add-mcp').length};
  t.calls.length = 0;
  btn().click();
  await wait(200);
  const off = {text: btn().textContent, admin: t.calls.some((c) => c[1] === '/api/admin/plugins'), note: !!t.doc.querySelector('#integrations .int-ro-note')};

  //then
  assert.deepStrictEqual(before, {text: 'View as read-only', pressed: 'false', add: true, note: false});
  assert.deepStrictEqual(on, {text: 'Back to editing', pressed: 'true', note: 'Preview: this is the page people without the admin role see. Only admins can change integrations.', read: true, admin: false, edits: 0},
    'the preview loads the readers\' data and drops every edit control');
  assert.deepStrictEqual(off, {text: 'View as read-only', admin: true, note: false});
  assert.strictEqual(viewer.doc.getElementById('int-preview'), null, 'only an admin gets the toggle');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  viewer.w.close();
  return 'an admin can switch Integrations to the read-only view, loaded from the same data viewers get, and back';
}

(async () => {
  for (const t of [theConnectRowShowsForEverySignedInRoleOnly, aViewerOpensTheSamePageReadOnly, theReadOnlyPanelShowsInstancesChecksThresholdsAndSecretsAsSet, anAdminStillGetsTheEditablePage,
    aVisitorWithoutLoginCannotOpenIt, everyKnowledgeSourceCarriesABadgeInItsOwnCategoryAndSitsOnceInTheChip, aJiraOnlyInstallKeepsThePlainGridAndTheChip, theAddCustomMcpButtonShowsForAnAdminOnlyWhileTheMcpPluginServes, anAdminSeesTheNoticesOnTheIntegrationsPage, anAdminCanPreviewThePageAsReadOnlyFromTheSameDataOthersGet]) console.log('PASS ' + await t());
})().catch((e) => { console.log('FAIL ' + e.message); console.error(e); process.exit(1); });
