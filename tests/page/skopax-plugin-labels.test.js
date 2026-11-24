const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const PLUGIN_TAGS = [{id: 'seo', label: 'SEO'}, {id: 'newsletter', label: 'Newsletter'}];

function rulesItem() {
  return {key: 'tags.seo.rules', group: 'Tags', label: 'SEO tag rules', help: 'Terms that tag a card SEO', kind: 'tagrules', live: true, value: 'type:page-typos', base: 'type:page-typos', default: 'type:page-typos',
    source: 'default', by: null, at: null, pluginTerms: ['type:newsletter', '<b>x</b>']};
}

async function boot(hash) {
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/' + hash, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, opts) => {
        const p = String(path), method = (opts && opts.method) || 'GET', body = opts && opts.body ? JSON.parse(opts.body) : null;
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/admin')) {
          calls.push([method, p, body]);
          if (p.startsWith('/api/admin/history')) return ok([]);
          if (p.startsWith('/api/admin/small-talk')) return {ok: false, status: 404, json: async () => ({})};
          if (method === 'GET') return ok({items: [rulesItem()], rev: 1, refreshSeconds: 15});
          return ok({item: Object.assign(rulesItem(), {value: body.value, source: 'admin', by: 'alice@example.com', at: iso(0)}), change: {}});
        }
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', operator: true, admin: true, teams: [], tags: PLUGIN_TAGS, task: {project: 'OPS', projects: ['OPS', 'PLUG']}});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(300);
  return {w: dom.window, doc: dom.window.document, errors, calls};
}

async function adminShowsPluginTermsApartAndSavesBaseOnly() {
  //given
  const t = await boot('#t=settings');
  const row = t.doc.querySelector('#admin .adm-row[data-key="tags.seo.rules"]');

  //when
  const input = row.querySelector('.adm-in');
  input.value = 'type:page-typos, gone';
  input.dispatchEvent(new t.w.Event('input', {bubbles: true}));
  row.querySelector('.adm-save').click();
  await wait(80);

  //then
  assert.deepStrictEqual([...row.querySelectorAll('.adm-plugin-terms code')].map((c) => c.textContent), ['type:newsletter', '<b>x</b>']);
  assert.ok(!row.querySelector('.adm-plugin-terms b'), 'plug-in terms are escaped');
  assert.ok(!row.querySelector('.adm-plugin-terms input'), 'and cannot be edited');
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'PUT').map((c) => c[2]), [{key: 'tags.seo.rules', value: 'type:page-typos, gone'}]);
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Admin, plug-in tag terms show apart and read-only, and a save sends only the base terms');
}

async function theBoardShowsPluginTagLabelsFromTheServer() {
  //given
  const t = await boot('#t=anoms');

  //then
  assert.strictEqual(t.w.tagName('newsletter'), 'Newsletter');
  assert.strictEqual(t.w.tagName('seo'), 'SEO');
  assert.strictEqual(t.w.tagName('unlisted'), 'unlisted');
  t.w.close();
  console.log('PASS the board takes tag labels from the server, so a plug-in tag shows its label and not its id');
}

async function aCardOpenedByItsNumberShowsPluginTagLabelsFromTheServer() {
  //given
  const card = {num: 7, key: 'demo|shop/checkout-service', svc: 'shop/checkout-service', label: 'Errors', sev: 'warn', status: 'cleared', firstSeen: iso(3000), live: false, tags: [{id: 'newsletter', by: 'rule', at: iso(3000)}], what: 'five errors', now: 5, d1: 0, d7: 0};
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const dom = new JSDOM(pageHtml(TEMPLATE, {view: 'board'}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/#c=7', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p === '/api/cards/7') return ok(card);
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: null, operator: false, tags: PLUGIN_TAGS});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);

  //then
  const tags = [...dom.window.document.querySelectorAll('.card-drawer .tag')].map((t) => t.textContent);
  assert.ok(tags.includes('Newsletter'), 'the plug-in tag shows its label: ' + tags.join(' | '));
  assert.ok(!tags.includes('newsletter'), 'never its id');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS a card opened by its number shows a plug-in tag by its label in the panel');
}

(async () => {
  await adminShowsPluginTermsApartAndSavesBaseOnly();
  await theBoardShowsPluginTagLabelsFromTheServer();
  await aCardOpenedByItsNumberShowsPluginTagLabelsFromTheServer();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
