const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const KEY = 'log|checkout-service|ERROR|boom';

const RUNS = [
  {id: 'c2', mode: 'live', trigger: 'chat', startedAt: iso(1), outcome: 'running', usd: 0.01, keys: [], ask: {private: true}, toolCalls: 1},
  {id: 'c1', mode: 'live', trigger: 'chat', startedAt: iso(2), outcome: 'done', usd: 0.03, keys: [], ask: {private: true}, toolCalls: 2},
  {id: 'r1', mode: 'live', trigger: 'cards', startedAt: iso(10), outcome: 'done', usd: 0.4, keys: [KEY], recap: 'pool exhausted after the deploy', toolCalls: 5},
  {id: 'r2', mode: 'live', trigger: 'ask', startedAt: iso(20), outcome: 'done', usd: 0.1, keys: [], recap: 'nothing unusual', ask: {text: 'Why is it slow?', by: 'nina.white@example.com', source: 'page'}, toolCalls: 3},
];

async function boot(serverKnowsTheFlag) {
  const errors = [], asked = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [{key: KEY, sev: 'warn', svc: 'checkout-service', label: 'ERROR lines', what: 'x', status: 'open', firstSeen: iso(40), lastSeen: iso(1), src: 'logs', sig: 'log'}],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/#t=acts', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        const path = String(p);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (path.startsWith('/api/investigations?limit=')) {
          asked.push(path);
          const noChats = serverKnowsTheFlag && new URL(path, 'https://skopax.example.com').searchParams.get('chats') === 'false';
          return ok(noChats ? RUNS.filter((r) => r.trigger !== 'chat') : RUNS);
        }
        if (path.startsWith('/api/server')) return ok({investigate: {mode: 'live', enabled: true, stopped: null, spentToday: 0.5, cap: 20}});
        if (path.startsWith('/api/me')) return ok({loginEnabled: true, user: 'nina.white@example.com', operator: true});
        if (path.startsWith('/api/rev')) return ok({rev: 1});
        if (path.startsWith('/api/actions') || path.startsWith('/api/deploy') || path.startsWith('/api/activity')) return ok([]);
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors, asked};
}

const rows = (doc) => [...doc.querySelectorAll('#pod-list > li.hrow')];

async function theHistoryRunListAsksTheServerToLeaveChatRunsOut() {
  //given
  const {w, doc, errors, asked} = await boot(true);

  //then
  assert.ok(asked.length && asked.every((p) => p.includes('chats=false')), asked.join(' | '));
  assert.strictEqual(rows(doc).length, 2, 'the card run and the page ask');
  assert.ok(rows(doc)[0].textContent.includes('checkout-service') && rows(doc)[1].textContent.includes('Request by nina.white@example.com: Why is it slow?'));
  assert.strictEqual(doc.getElementById('hist-runs-n').textContent, '2');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the History run list asks for runs without chat messages, so chat runs never push card runs out of it');
}

async function aChatRunThatStillArrivesReadsAsAPrivateChatMessage() {
  //given
  const {w, doc, errors} = await boot(false);

  //when
  const list = rows(doc);
  const chats = list.slice(0, 2).map((li) => li.querySelector('.hw').textContent);
  const text = doc.getElementById('pod-list').textContent;

  //then
  assert.strictEqual(list.length, 4);
  assert.deepStrictEqual(chats, ['private chat message', 'private chat message']);
  assert.ok(list.slice(0, 2).every((li) => !li.querySelector('.hbody p:not(.hmeta)')), 'no answer line on a private chat run');
  assert.ok(!/an operator|No answer text|Working on it/.test(text), 'no empty operator ask and no blank answer: ' + text);
  assert.ok(list[3].textContent.includes('Request by nina.white@example.com: Why is it slow?'));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a chat run that still arrives reads as a private chat message with no answer line, not as an empty operator ask');
}

(async () => {
  await theHistoryRunListAsksTheServerToLeaveChatRunsOut();
  await aChatRunThatStillArrivesReadsAsAPrivateChatMessage();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
