const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const KEY = 'log|checkout-service|ERROR|boom';
const INVESTIGATOR_OFF = 'AI investigator is turned off in Admin \u203a Features.';
const CHAT_OFF = 'Chat is turned off in Admin \u203a Features.';

async function boot(refusals) {
  const errors = [], posted = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const card = {key: KEY, sev: 'warn', svc: 'checkout-service', label: 'ERROR lines', what: 'x', status: 'open', firstSeen: iso(30), lastSeen: iso(1), src: 'logs', sig: 'log'};
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/#t=anoms', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p, init) => {
        const path = String(p), method = (init && init.method) || 'GET';
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (method === 'POST' && refusals[path]) {
          posted.push(path);
          return {ok: false, status: 409, json: async () => ({detail: refusals[path]})};
        }
        if (path === '/api/card-runs') return ok({});
        if (path.startsWith('/api/investigations')) return ok([]);
        if (path.startsWith('/api/server')) return ok({investigate: {mode: 'live', enabled: true, stopped: null, spentToday: 0.5, cap: 20}});
        if (path.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', operator: true, teams: [], tags: [], chat: true});
        if (path.startsWith('/api/rev')) return ok({rev: 1});
        if (path.startsWith('/api/actions') || path.startsWith('/api/deploy') || path.startsWith('/api/activity')) return ok([]);
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(500);
  return {w: dom.window, doc: dom.window.document, errors, posted};
}

const cardOf = (doc) => doc.querySelector('#anoms article.inc[data-key="' + KEY + '"]');

async function anInvestigateClickWhileTheInvestigatorIsOffShowsTheServerText() {
  //given
  const {w, doc, errors, posted} = await boot({'/api/actions': INVESTIGATOR_OFF});
  const c = cardOf(doc);
  c.querySelector('.inc-title .fold').click();
  await wait(60);

  //when
  const b = c.querySelector('.acts .inv-btn');
  b.click();
  await wait(100);

  //then
  assert.deepStrictEqual(posted, ['/api/actions']);
  assert.strictEqual(b.textContent, INVESTIGATOR_OFF, 'the 409 says where to turn it on instead of inviting a retry');
  assert.strictEqual(b.disabled, false);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS an Investigate click while the AI investigator is off shows the server text naming Admin \u203a Features, not try again');
}

async function aCardAskWhileTheChatIsOffShowsTheServerText() {
  //given
  const {w, doc, errors, posted} = await boot({'/api/chats': CHAT_OFF});
  const c = cardOf(doc);
  c.querySelector('.inc-title .fold').click();
  await wait(60);
  const box = c.querySelector('.card-ask');

  //when
  box.querySelector('input').value = 'Why was this card opened?';
  box.querySelector('input').dispatchEvent(new w.Event('input', {bubbles: true}));
  box.querySelector('.ask-go').click();
  await wait(100);

  //then
  const err = c.querySelector('.card-ask .cask-err');
  assert.deepStrictEqual(posted, ['/api/chats']);
  assert.ok(!err.hidden && err.textContent === CHAT_OFF, 'the 409 text is shown under the ask box');
  assert.strictEqual(c.querySelector('.card-ask input').value, 'Why was this card opened?', 'the question is kept');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a card ask while the chat is off shows the server text naming Admin \u203a Features, not try again');
}

(async () => {
  await anInvestigateClickWhileTheInvestigatorIsOffShowsTheServerText();
  await aCardAskWhileTheChatIsOffShowsTheServerText();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
