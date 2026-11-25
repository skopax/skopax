const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const ADMIN_USER = 'nina.white@example.com';
const MODELS = ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-fable-5-2', 'gpt-6.2-astra', 'orion-9'];
const PROVIDERS = {'claude-opus-5-5': 'anthropic', 'claude-sonnet-5-5': 'anthropic', 'claude-fable-5-2': 'anthropic', 'gpt-6.2-astra': 'openai', 'orion-9': 'openai'};
const DEFAULTS = {anthropic: 'claude-opus-5-5', openai: 'gpt-6.2-astra'};
const SUMMARY = (crit, high, rest) => 'Which model runs: Critical cards ' + crit + ' \u00b7 High cards ' + high + ' \u00b7 Warning and Info cards ' + rest;

function entry(key, label, extra) {
  return Object.assign({key, group: 'Investigator', label, help: 'Help of ' + label, kind: 'choice', choices: MODELS, live: true, value: 'claude-opus-5-5', base: 'claude-opus-5-5', default: 'claude-opus-5-5', source: 'default', by: null, at: null}, extra || {});
}

async function boot(values) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const v = Object.assign({'investigate.model': 'claude-opus-5-5', 'investigate.model-critical': '', 'investigate.model-high': '', 'investigate.provider': 'anthropic'}, values || {});
  const card = Object.assign({value: v['investigate.model'], modelProviders: PROVIDERS, cardDefaults: DEFAULTS}, v.keyed ? {keyedProviders: v.keyed} : {}, v.cardSource ? {source: v.cardSource} : {});
  let list = [
    entry('investigate.provider', 'AI provider', {choices: ['anthropic', 'openai'], value: v['investigate.provider'], base: 'anthropic', default: 'anthropic'}),
    entry('investigate.model', 'Card model', card),
    entry('investigate.model-critical', 'Model for Critical cards', {choices: [''].concat(MODELS), value: v['investigate.model-critical'], base: '', default: ''}),
    entry('investigate.model-high', 'Model for High cards', {choices: [''].concat(MODELS), value: v['investigate.model-high'], base: '', default: ''}),
    entry('investigate.effort', 'Card effort', {choices: ['low', 'high'], value: 'high', base: 'high', default: 'high'}),
  ];
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/#t=ai', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, opts) => {
        const p = String(path), method = (opts && opts.method) || 'GET', body = opts && opts.body ? JSON.parse(opts.body) : null;
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/admin')) {
          if (p.startsWith('/api/admin/history')) return ok([]);
          if (p === '/api/admin/notices') return ok({items: []});
          if (p === '/api/admin/features') return ok({edition: 'community', items: []});
          if (p === '/api/admin/plugins') return ok({plugins: []});
          if (p.startsWith('/api/admin/teams')) return ok({routes: [], tags: [], hosts: []});
          if (p.startsWith('/api/admin/secrets')) return ok({fields: [], canEncrypt: true});
          if (p.startsWith('/api/admin/agent-sources')) return ok({agents: []});
          if (method === 'GET') return ok({items: list, rev: 3, refreshSeconds: 15});
          if (method === 'PUT') {
            const item = list.find((i) => i.key === body.key);
            const next = Object.assign({}, item, {value: body.value, source: 'admin', by: ADMIN_USER, at: iso(0)});
            list = list.map((i) => i.key === body.key ? next : i);
            return ok({item: next, change: {}});
          }
          return {ok: false, status: 404, json: async () => ({})};
        }
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: ADMIN_USER, operator: true, admin: true, teams: [], tags: []});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors};
}

const summaryOf = (doc) => {
  const pair = doc.querySelector('#ai-rows .adm-pair[data-pair="investigate.model"]');
  const n = pair && pair.querySelector('.adm-model-summary');
  return n ? n.textContent : null;
};

async function theSummarySaysWhichModelRunsEachSeverity() {
  //given
  const {w, doc, errors} = await boot({'investigate.model-critical': 'claude-fable-5-2', 'investigate.model-high': 'claude-sonnet-5-5'});

  //when
  const text = summaryOf(doc);
  const pair = doc.querySelector('#ai-rows .adm-pair[data-pair="investigate.model"]');

  //then
  assert.strictEqual(text, SUMMARY('claude-fable-5-2', 'claude-sonnet-5-5', 'claude-opus-5-5'));
  assert.ok(pair.querySelector('.adm-model-summary').classList.contains('adm-help'));
  assert.strictEqual(doc.querySelectorAll('#ai-rows .adm-model-summary').length, 1, 'only the Card model pair carries it');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the summary says which model runs each severity');
}

async function anEmptyOverrideShowsTheCardModel() {
  //given
  const {w, doc, errors} = await boot({'investigate.model-critical': 'claude-fable-5-2'});

  //when
  const text = summaryOf(doc);

  //then
  assert.strictEqual(text, SUMMARY('claude-fable-5-2', 'claude-opus-5-5', 'claude-opus-5-5'));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS an empty override shows the Card model');
}

async function anOverrideOfAnotherProviderShowsTheCardModel() {
  //given
  const {w, doc, errors} = await boot({'investigate.model-critical': 'gpt-6.2-astra', 'investigate.model-high': 'claude-fable-5-2'});

  //when
  const text = summaryOf(doc);

  //then
  assert.strictEqual(text, SUMMARY('claude-opus-5-5', 'claude-fable-5-2', 'claude-opus-5-5'));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS an override of another provider shows the Card model');
}

async function theSummaryShowsTheProviderDefaultWhenTheCardModelIsOfAnotherProvider() {
  //given
  const {w, doc, errors} = await boot({'investigate.provider': 'openai', 'investigate.model': 'claude-opus-5-5'});

  //when
  const text = summaryOf(doc);

  //then
  assert.strictEqual(text, SUMMARY('gpt-6.2-astra', 'gpt-6.2-astra', 'gpt-6.2-astra'));
  assert.ok(!text.includes('claude-opus-5-5'));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the summary shows the provider default when the Card model is of another provider');
}

async function anUnprefixedModelOfAnotherProviderShowsTheCardModel() {
  //given
  const {w, doc, errors} = await boot({'investigate.model-critical': 'orion-9', 'investigate.model-high': 'claude-sonnet-5-5'});

  //when
  const text = summaryOf(doc);

  //then
  assert.strictEqual(text, SUMMARY('claude-opus-5-5', 'claude-sonnet-5-5', 'claude-opus-5-5'));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS an unprefixed model of another provider shows the Card model');
}

async function theSummaryFollowsASavedProviderChange() {
  //given
  const {w, doc, errors} = await boot({'investigate.model-critical': 'claude-fable-5-2'});
  const before = summaryOf(doc);
  const row = doc.querySelector('#ai-rows .adm-row[data-key="investigate.provider"]');
  const select = row.querySelector('select.adm-in');

  //when
  select.value = 'openai';
  select.dispatchEvent(new w.Event('change', {bubbles: true}));
  row.querySelector('.adm-save').click();
  await wait(200);

  //then
  assert.strictEqual(before, SUMMARY('claude-fable-5-2', 'claude-opus-5-5', 'claude-opus-5-5'));
  assert.strictEqual(summaryOf(doc), SUMMARY('gpt-6.2-astra', 'gpt-6.2-astra', 'gpt-6.2-astra'));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the summary follows a saved provider change');
}

async function theSummaryFollowsASavedChange() {
  //given
  const {w, doc, errors} = await boot();
  const before = summaryOf(doc);
  const row = doc.querySelector('#ai-rows .adm-row[data-key="investigate.model-high"]');
  const select = row.querySelector('select.adm-in');

  //when
  select.value = 'claude-fable-5-2';
  select.dispatchEvent(new w.Event('change', {bubbles: true}));
  row.querySelector('.adm-save').click();
  await wait(120);

  //then
  assert.strictEqual(before, SUMMARY('claude-opus-5-5', 'claude-opus-5-5', 'claude-opus-5-5'));
  assert.strictEqual(summaryOf(doc), SUMMARY('claude-opus-5-5', 'claude-fable-5-2', 'claude-opus-5-5'));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the summary follows a saved change');
}

async function aModelOfAnotherProviderWithAKeyRunsAsChosen() {
  //given
  const {w, doc, errors} = await boot({'investigate.model': 'gpt-6.2-astra', cardSource: 'admin', 'investigate.model-critical': 'orion-9', 'investigate.model-high': 'claude-fable-5-2', keyed: ['anthropic', 'openai']});

  //when
  const text = summaryOf(doc);

  //then
  assert.strictEqual(text, SUMMARY('orion-9', 'claude-fable-5-2', 'gpt-6.2-astra'));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a model of another provider with a key runs as chosen');
}

async function aDefaultCardModelFollowsTheDefaultProviderEvenWithBothKeys() {
  //given
  const {w, doc, errors} = await boot({'investigate.provider': 'openai', 'investigate.model': 'claude-opus-5-5', keyed: ['anthropic', 'openai']});

  //when
  const text = summaryOf(doc);

  //then
  assert.strictEqual(text, SUMMARY('gpt-6.2-astra', 'gpt-6.2-astra', 'gpt-6.2-astra'));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a default card model follows the default provider even with both keys');
}

(async () => {
  await aModelOfAnotherProviderWithAKeyRunsAsChosen();
  await aDefaultCardModelFollowsTheDefaultProviderEvenWithBothKeys();
  await theSummarySaysWhichModelRunsEachSeverity();
  await anEmptyOverrideShowsTheCardModel();
  await anOverrideOfAnotherProviderShowsTheCardModel();
  await theSummaryFollowsASavedChange();
  await theSummaryShowsTheProviderDefaultWhenTheCardModelIsOfAnotherProvider();
  await anUnprefixedModelOfAnotherProviderShowsTheCardModel();
  await theSummaryFollowsASavedProviderChange();
})().catch((e) => { console.error('FAIL', e); process.exit(1); });
