const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const KEY = 'log|checkout-service|ERROR|boom';
const OTHER = 'log|payments-api|ERROR|npe';

const CHATS = Array.from({length: 100}, (_, i) => ({id: 'c' + i, mode: 'live', trigger: 'chat', startedAt: iso(i + 1), outcome: 'done', usd: 0.01, keys: [], ask: {private: true}, toolCalls: 1}));
const LATEST = {id: 'r-old', mode: 'live', trigger: 'cards', startedAt: iso(2880), outcome: 'done', usd: 0.6, keys: [KEY], recap: 'pool exhausted after the 09:00 deploy', toolCalls: 7};

async function boot(cardRuns, runs) {
  const errors = [], gets = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const card = (key) => ({key, sev: 'warn', svc: key.split('|')[1], label: 'ERROR lines', what: 'x', status: 'open', firstSeen: iso(3000), lastSeen: iso(1), src: 'logs', sig: 'log'});
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(KEY), card(OTHER)], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/#t=anoms', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        const path = String(p);
        gets.push(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (path === '/api/card-runs') return cardRuns ? ok(cardRuns) : {ok: false, status: 404, json: async () => ({detail: 'Not Found'})};
        if (path.startsWith('/api/investigations?limit=')) return ok(runs);
        if (path.startsWith('/api/investigations?key=')) return ok([]);
        if (path.startsWith('/api/server')) return ok({investigate: {mode: 'live', enabled: true, stopped: null, spentToday: 0.5, cap: 20}});
        if (path.startsWith('/api/me')) return ok({loginEnabled: true, user: 'nina.white@example.com', operator: true, teams: [], tags: []});
        if (path.startsWith('/api/rev')) return ok({rev: 1});
        if (path.startsWith('/api/actions') || path.startsWith('/api/deploy') || path.startsWith('/api/activity')) return ok([]);
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(500);
  return {w: dom.window, doc: dom.window.document, errors, gets};
}

const cardOf = (doc, k) => doc.querySelector('#anoms article.inc[data-key="' + k + '"]');

async function aCardInvestigatedLongAgoKeepsItsRecapSpendAndConfirmWhenAHundredNewerRunsCameAfter() {
  //given
  const {w, doc, errors, gets} = await boot({[KEY]: {latest: LATEST, n: 3, usd: 1.5}}, CHATS);
  const c = cardOf(doc, KEY);

  //when
  c.querySelector('.inc-title .fold').click();
  const badge = c.querySelector('.usd');
  const recap = c.querySelector('.podrun');
  c.querySelector('.acts .inv-btn').click();
  await wait(20);

  //then
  assert.ok(gets.includes('/api/card-runs'));
  assert.ok(badge && badge.textContent === '$1.50' && badge.title === 'Investigator: 3 runs, $1.50', 'the spend badge counts every run on the card');
  assert.ok(recap && recap.textContent.includes('pool exhausted after the 09:00 deploy'), 'the card keeps the recap of its latest run');
  assert.ok(!doc.getElementById('cf-wrap').hidden && doc.getElementById('cf-title').textContent === 'Investigate again?', 'Investigate asks before a new paid run');
  assert.strictEqual(cardOf(doc, OTHER).querySelector('.usd'), null, 'a card without runs has no badge');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a card investigated two days ago keeps its recap, its run count and spend, and the Investigate again? confirm after a hundred newer runs (chat messages) came along');
}

async function withoutTheCardRunsAnswerTheCardsFallBackToTheLatestRuns() {
  //given
  const {w, doc, errors} = await boot(null, [Object.assign({}, LATEST, {id: 'r-new', startedAt: iso(5), recap: 'from the run list'})]);

  //then
  assert.strictEqual(cardOf(doc, KEY).querySelector('.usd').title, 'Investigator: 1 run, $0.60');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS without the per-card answer the cards still read their runs from the latest run list');
}

(async () => {
  await aCardInvestigatedLongAgoKeepsItsRecapSpendAndConfirmWhenAHundredNewerRunsCameAfter();
  await withoutTheCardRunsAnswerTheCardsFallBackToTheLatestRuns();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
