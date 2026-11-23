const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const EVIL = '<b id="pwn">x</b>';
const TAGS = [{id: 'developer', label: 'Developer'}, {id: 'infra', label: 'Infrastructure'}, {id: 'ops-team', label: EVIL}];
const IDS = ['developer', 'infra', 'ops-team'];

function rulesItem(tag, label) {
  return {key: 'tags.' + tag + '.rules', group: 'Card tags', card: 'Tag rules', order: 9000, label: label, help: 'Cards tagged ' + label + ' by rule', kind: 'tagrules', live: true, value: '', base: '', default: '',
    source: 'default', by: null, at: null, limit: 500, pluginTerms: tag === 'infra' ? ['type:newsletter'] : []};
}

const FIELDS = [
  {key: 'webhook', label: 'Webhook', kind: 'secret', required: true, default: '', help: '', choices: [], low: null, high: null, showIf: null, ui: false},
  {key: 'tags', label: 'Card tags', kind: 'text', required: false, default: '', help: 'Comma list of card tag ids or names', choices: [], low: null, high: null, showIf: null, ui: false, suggest: true, separator: ','},
];

function catalog() {
  return {edition: 'community', secretsKey: 'generated', plugins: [
    {id: 'teams', name: 'Microsoft Teams', version: '0.1.0', category: 'chat', description: 'Posts cards.', edition: 'community', icon: false, state: 'ok', reason: null, enabled: true, fields: FIELDS, checks: [],
      instances: [{name: 'ops', primary: true, enabled: true, source: 'admin', problem: null, values: {tags: 'Infrastructure'}, secrets: {webhook: {set: true, from: null}}, owned: [], offChecks: []}],
      health: {state: 'ok', text: 'Healthy', at: iso(2), instances: [{name: 'ops', state: 'ok', text: 'Healthy', at: iso(2)}]}},
  ]};
}

async function boot(hash) {
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/' + hash, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, o) => {
        const p = String(path), method = (o && o.method) || 'GET', body = o && o.body ? JSON.parse(o.body) : null;
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', userRole: 'admin', operator: true, admin: true, teams: [], tags: TAGS});
        if (p.startsWith('/api/admin')) {
          calls.push([method, p, body]);
          if (p === '/api/admin/plugins' && method === 'GET') return ok(catalog());
          if (p === '/api/admin/plugins/teams/fields/tags/suggestions') return ok({values: IDS});
          if (p === '/api/admin/tags') return ok({tags: TAGS.map((t) => Object.assign({}, t, {responsibles: []})), canEdit: true, users: [], reportAt: '10:00', emailScopeMissing: false});
          if (p.startsWith('/api/admin/settings')) return ok({items: TAGS.map((t) => rulesItem(t.id, t.label)), rev: 1, refreshSeconds: 15});
          if (p.startsWith('/api/admin/notices')) return ok({items: []});
          if (p.startsWith('/api/admin/history')) return ok([]);
          if (p.startsWith('/api/admin/users') || p.startsWith('/api/admin/tokens')) return ok([]);
          if (p.startsWith('/api/admin/teams')) return ok({routes: [], tags: [], hosts: []});
          return {ok: false, status: 404, json: async () => ({detail: 'none'})};
        }
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(450);
  return {w: dom.window, doc: dom.window.document, errors, calls};
}

async function eachCardTagsRowShowsItsIdAsAnEscapedChipAndTheFilterFindsIt() {
  //given
  const {w, doc, errors} = await boot('#t=settings');
  const row = (id) => doc.querySelector('#admin .adm-row[data-key="tags.' + id + '.rules"]');

  //when
  const chips = IDS.map((id) => [...row(id).querySelectorAll('.adm-n code.adm-tag-id')].map((c) => c.textContent));
  const input = row('infra').querySelector('.adm-in');
  const note = row('infra').querySelector('.adm-plugin-terms').textContent;
  const find = doc.getElementById('adm-find');
  find.value = 'ops-team';
  find.dispatchEvent(new w.Event('input', {bubbles: true}));
  const visible = IDS.filter((id) => !row(id).hidden);

  //then
  assert.deepStrictEqual(chips, [['developer'], ['infra'], ['ops-team']], 'every row shows its id in a code chip next to the label');
  assert.strictEqual(input.placeholder, 'Add rules, such as type:log, log|checkout-*', 'the empty rule box invites typing');
  assert.strictEqual(note, 'Also tagged by plug-ins (fixed, added to yours): type:newsletter');
  assert.strictEqual(doc.getElementById('pwn'), null, 'a label with markup stays text');
  assert.deepStrictEqual(visible, ['ops-team'], 'the row filter finds a tag by its id');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS Admin, Settings, Card tags: every row shows the tag id in a code chip and the filter finds it');
}

async function theCardTagsFieldOfARouteSuggestsTheTagIds() {
  //given
  const {w, doc, errors, calls} = await boot('#t=anoms');
  doc.getElementById('tab-integrations').click();
  await wait(120);
  const panel = doc.getElementById('int-panel');
  [...doc.querySelectorAll('#integrations .int-tile')].filter((x) => x.dataset.id === 'teams')[0].querySelector('.int-open').click();
  const tab = [...panel.querySelectorAll('.int-itab')].filter((x) => x.dataset.name === 'ops')[0];
  if (tab) tab.click();
  const field = [...panel.querySelectorAll('.int-form .int-field')].filter((f) => f.dataset.key === 'tags')[0];
  const box = field.querySelector('[role="combobox"]');

  //when
  box.focus();
  box.dispatchEvent(new w.Event('focus'));
  await wait(150);
  const offered = [...field.querySelectorAll('[role="option"]')].map((o) => o.textContent);

  //then
  assert.deepStrictEqual(offered, IDS, 'the picker offers the tag ids');
  assert.strictEqual(calls.filter((c) => c[1] === '/api/admin/plugins/teams/fields/tags/suggestions').length, 1);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS Admin, Integrations: the Card tags field of a route offers the tag ids as suggestions');
}

(async () => {
  for (const t of [eachCardTagsRowShowsItsIdAsAnEscapedChipAndTheFilterFindsIt, theCardTagsFieldOfARouteSuggestsTheTagIds]) {
    try { await t(); } catch (e) { console.error('FAIL ' + t.name + ': ' + e.message.split('\n')[0]); process.exitCode = 1; }
  }
  process.exit(process.exitCode || 0);
})();
