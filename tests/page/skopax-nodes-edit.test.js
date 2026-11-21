const fs = require('fs');
const path = require('path');
const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const PANEL_JS = fs.readFileSync(path.join(__dirname, '..', '..', 'skopax', 'plugins', 'kubernetes', 'panel.js'), 'utf8');
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const SLOT = '<script type="text/plain" id="plugin-scripts"></script>';
const ICON = '/plugins/kubernetes/icon.svg';
const PLUGINS = [{id: 'kubernetes', name: 'Kubernetes', icon: ICON, tab: 'nodes', passes: ['nodes'], instances: [], pages: [{tab: 'nodes', label: 'Nodes', group: 'connect', icon: ICON}]}];

function kubernetes() {
  const inst = (name, url) => ({name, primary: name === 'hub', enabled: true, source: 'admin', problem: null, values: {mode: 'agent', url}, secrets: {token: {set: true}}, offChecks: []});
  return {id: 'kubernetes', name: 'Kubernetes', version: '0.1.0', category: 'infrastructure', description: 'Nodes', origin: 'builtin', state: 'ok', reason: null, enabled: true, icon: false,
    fields: [{key: 'mode', label: 'Mode', kind: 'choice', required: false, default: 'agent', help: '', choices: ['in-cluster', 'agent'], showIf: null},
      {key: 'url', label: 'Agent URL', kind: 'url', required: false, default: '', help: '', choices: [], showIf: null},
      {key: 'token', label: 'Token', kind: 'secret', required: false, default: '', help: '', choices: [], showIf: null}],
    checks: [], rules: [], tools: [], setup: {steps: ['Install the agent.'], docs: 'https://kube.example.com/docs', docsLabel: 'Guide'},
    instances: [inst('hub', 'https://hub.example.com'), inst('east', 'https://east.example.com')],
    health: {state: 'ok', text: 'Connected', at: iso(1), instances: []}};
}

function nodeRows() {
  return [
    {name: 'hub', role: 'agent', status: 'Ready', since: iso(600), lastSeen: iso(1), via: 'https://hub.example.com', version: 'abc12345', plugin: 'kubernetes'},
    {name: 'east', role: 'agent', status: 'Ready', since: iso(600), lastSeen: iso(1), via: 'https://east.example.com', version: 'abc12345', plugin: 'kubernetes'},
    {name: 'local', role: 'master', status: 'Ready', since: iso(600), lastSeen: iso(1), via: 'in-cluster', plugin: 'kubernetes'},
  ];
}

async function boot(role) {
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 5, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'ok', headline: 'x', points: [], at: iso(1)}, anomalies: [], services: [], history: [], routes: [], deploys: [], nodes: nodeRows()};
  const admin = role === 'admin';
  const panel = '<script>\n' + PANEL_JS.replace(/<\/script/gi, '<\\/script') + '\n</script>';
  const html = pageHtml(TEMPLATE).replace(SLOT, () => panel).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state).replace(/<\//g, '<\\/'));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=nodes', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p, opts) => {
        const url = String(p), method = (opts && opts.method) || 'GET', body = opts && opts.body ? JSON.parse(opts.body) : null;
        const ok = (b, status) => ({ok: true, status: status || 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (url.startsWith('/api/admin') || url.startsWith('/api/integrations')) calls.push([method, url, body]);
        if (url.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', userRole: role, operator: true, admin, teams: [], tags: []});
        if (url.startsWith('/api/server')) return ok({plugins: PLUGINS, tz: 'UTC'});
        if (url === '/api/integrations') return ok({edition: 'community', readOnly: !admin, plugins: [kubernetes()]});
        if (url === '/api/admin/plugins' && method === 'GET') return ok({edition: 'community', secretsKey: 'generated', plugins: [kubernetes()]});
        if (url.startsWith('/api/admin/plugins/kubernetes/instances/') && method === 'PATCH') return ok({instance: {name: 'east'}});
        if (url.startsWith('/api/admin/plugins/kubernetes/instances') && method === 'POST') return ok({name: body.name}, 201);
        if (url === '/api/admin/notices') return ok({items: []});
        if (url.startsWith('/api/admin/custom-checks')) return ok({items: [], limits: {checks: 50, periodSeconds: 300}, choices: {}, presets: []});
        if (url.startsWith('/api/admin/settings')) return ok({items: [], rev: 1, refreshSeconds: 15});
        if (url.startsWith('/api/actions') || url.startsWith('/api/deploy-reports') || url.startsWith('/api/requests')) return ok([]);
        if (url.startsWith('/api/rev')) return ok({rev: state.rev});
        if (url.startsWith('/api/state')) return ok(state);
        return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
      };
    },
  });
  await wait(500);
  const doc = dom.window.document;
  return {w: dom.window, doc, errors, calls, section: doc.getElementById('panel-nodes')};
}

async function anAdminEditsANodeInTheIntegrationsDrawerAndSavesThroughTheSameEndpoint() {
  //given
  const {w, doc, errors, calls, section} = await boot('admin');
  const edits = [...section.querySelectorAll('.node-edit')];

  //then
  assert.deepStrictEqual(edits.map((b) => b.dataset.edit), ['hub', 'east'], 'a node row has Edit only when a Kubernetes instance of that name exists');
  assert.strictEqual(section.querySelectorAll('tr.node-row').length, 3);
  assert.ok(!section.querySelector('tr.node-row[data-node="local"] .node-edit'), 'a row with no instance has no Edit');
  assert.ok(section.querySelector('.node-add'), 'and the page has + Add node');
  assert.strictEqual(doc.getElementById('tab-nodes').closest('[data-grp]').dataset.grp, 'connect', 'Nodes sits under Connect');
  const group = [...doc.getElementById('tab-nodes').closest('[data-grp]').querySelectorAll('a.tab')].map((a) => a.id);
  assert.strictEqual(group[group.indexOf('tab-integrations') + 1], 'tab-nodes', 'right after Integrations');

  //when
  edits[1].click();
  await wait(300);

  //then
  const drawer = doc.getElementById('int-wrap');
  assert.ok(!drawer.hidden, 'the drawer opens');
  const form = drawer.querySelector('form.int-form, .int-form');
  assert.ok(form, 'with the instance form');
  assert.strictEqual(form.dataset.name, 'east', 'for that instance');
  assert.strictEqual(form.dataset.plugin, 'kubernetes');
  assert.strictEqual(doc.getElementById('panel-nodes').hidden, false, 'the Nodes page stays where it is');

  //when
  const url = drawer.querySelector('#int-kubernetes-east-url');
  url.value = 'https://east2.example.com';
  url.dispatchEvent(new w.Event('input', {bubbles: true}));
  await wait(50);
  const save = [...drawer.querySelectorAll('button')].find((b) => /^Save/.test(b.textContent) && !b.disabled);
  assert.ok(save, 'Save is offered once a field changed');
  save.click();
  await wait(300);

  //then
  const patch = calls.filter((c) => c[0] === 'PATCH');
  assert.deepStrictEqual(patch.map((c) => c[1]), ['/api/admin/plugins/kubernetes/instances/east'], 'the same save endpoint as the Integrations page');
  assert.strictEqual(patch[0][2].values.url, 'https://east2.example.com');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'an admin edits a node from the Nodes page in the Integrations drawer and saves through the same endpoint';
}

async function anAdminAddsANodeWithANewKubernetesInstanceForm() {
  //given
  const {w, doc, errors, section} = await boot('admin');

  //when
  section.querySelector('.node-add').click();
  await wait(300);

  //then
  const drawer = doc.getElementById('int-wrap');
  assert.ok(!drawer.hidden, 'the drawer opens');
  const form = drawer.querySelector('.int-form');
  assert.ok(form && (form.dataset.name === undefined || form.dataset.name === ''), 'a new instance form, with no instance name yet');
  assert.strictEqual(form.dataset.plugin, 'kubernetes');
  assert.deepStrictEqual(errors, []);
  w.close();
  return '+ Add node opens a new Kubernetes instance form';
}

async function aViewerSeesNoEditOrAddButtons() {
  //given
  const {w, section, errors} = await boot('viewer');

  //then
  assert.ok(section.querySelector('tr.node-row'), 'the node rows show');
  assert.ok(!section.querySelector('.node-edit') && !section.querySelector('.node-add'), 'with no Edit and no + Add node');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a viewer sees the Nodes page read-only';
}

(async () => {
  for (const t of [anAdminEditsANodeInTheIntegrationsDrawerAndSavesThroughTheSameEndpoint, anAdminAddsANodeWithANewKubernetesInstanceForm, aViewerSeesNoEditOrAddButtons]) {
    console.log('PASS ' + await t());
  }
  process.exit(0);
})().catch((e) => { console.error('FAIL ' + (e && e.stack || e)); process.exit(1); });
