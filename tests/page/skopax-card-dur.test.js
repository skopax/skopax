const pageHtml = require('./skopax-page');
const assert = require('assert');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const CARD_PAGE = path.join(path.dirname(TEMPLATE), 'card.html');
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const AT = '2026-11-09T16:32:00Z';

async function boot(card) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(CARD_PAGE), {
    url: 'https://skopax.example.com/c/' + card.num, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.fetch = async (p) => {
        if (String(p) === '/api/me') return {ok: true, status: 200, json: async () => ({loginEnabled: true, user: null, operator: false})};
        return {ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(card))};
      };
    },
  });
  await wait(100);
  return {w: dom.window, doc: dom.window.document, errors};
}

async function durationCardsReadAsHoursAndMinutesOnTheCardPage() {
  //given
  const base = {svc: 'contacts-sync', label: 'Airbyte long run', sev: 'warn', status: 'open', firstSeen: AT, live: true, src: 'airbyte', sig: 'airbyte-long-run', unit: 'dur'};
  const long = await boot(Object.assign({num: 21, key: 'airbyte|contacts-sync|long', now: 9000, d1: 5400, d7: 90}, base));
  const argo = await boot(Object.assign({num: 22, key: 'argo|commerce/checkout-service', now: 7260, d1: null, d7: null}, base));
  const ms = await boot(Object.assign({}, base, {num: 23, key: 'apm-p95|checkout-service', now: 1500, d1: 320, d7: null, unit: 'ms'}));

  //then
  assert.strictEqual(long.doc.querySelector('.nums').textContent, 'Now 2 h 30 min \u00b7 yesterday 90 min \u00b7 last week 90 s');
  assert.strictEqual(argo.doc.querySelector('.nums').textContent, 'Now 2 h 1 min');
  assert.strictEqual(ms.doc.querySelector('.nums').textContent, 'Now 1.5 s \u00b7 yesterday 320 ms', 'other units stay as they were');
  [long, argo, ms].forEach((x) => { assert.deepStrictEqual(x.errors, []); x.w.close(); });
  console.log('PASS cards measured in seconds (Airbyte, Argo) read as h / min / s on the card page, like the board, instead of a raw number and the word dur');
}

(async () => {
  await durationCardsReadAsHoursAndMinutesOnTheCardPage();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
