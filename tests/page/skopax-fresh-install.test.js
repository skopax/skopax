const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const KEY = 'err|shop/checkout-service';

function state() {
  const card = {key: KEY, num: 1, sev: 'crit', status: 'open', svc: 'checkout-service', ns: 'shop', label: 'Errors', src: 'prom', sig: 'err', now: 4, firstSeen: iso(2), lastSeen: iso(0)};
  const fired = {at: iso(2), type: 'fired', key: KEY, num: 1, sev: 'crit', svc: 'checkout-service', label: 'Errors'};
  return {initialized: true, rev: 1, anomalies: [card], history: [fired]};
}

async function boot() {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state()));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.addEventListener('error', (e) => errors.push('uncaught: ' + e.message));
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: false, user: null, operator: false, teams: [], tags: []});
        if (p.startsWith('/api/server')) return ok({tz: 'UTC'});
        if (p.startsWith('/api/state')) return ok(state());
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(600);
  return {w: dom.window, doc: dom.window.document, errors};
}

async function aFreshInstallBoardRendersItsFirstCardWithoutUndefinedOrNaN() {
  //given
  const {w, doc, errors} = await boot();

  //then
  const card = doc.querySelector('#anoms article.inc[data-key="' + KEY + '"]');
  assert.ok(card, 'the first card is on the board');
  assert.ok(card.textContent.includes('checkout-service'));
  for (const sel of ['header.top', '#side', '#reco']) {
    const el = doc.querySelector(sel);
    assert.ok(el, sel + ' exists');
    assert.ok(!/undefined|NaN/.test(el.textContent), sel + ' text: ' + el.textContent.slice(0, 300));
  }
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a fresh install state with one card, no sweptIso, nextIso or sources, renders the card and no undefined or NaN text');
}

(async () => {
  await aFreshInstallBoardRendersItsFirstCardWithoutUndefinedOrNaN();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
