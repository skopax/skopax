const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const FEIGN = 'log|checkout-service|ERROR|feign';
const GW = 'gw-err|identity/edge-gateway';
const ACCENTS = 'log|sms-sender|ERROR|accents';

function state() {
  const card = (key, extra) => Object.assign({key, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'x', src: 'logs', sig: 'log', now: 1, firstSeen: iso(30), lastSeen: iso(1)}, extra || {});
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(FEIGN, {num: 3, label: 'ERROR lines', sub: 'Mailer API', what: 'FeignException$InternalServerError calling the Mailer API'}),
      card(GW, {num: 4, label: 'Ingress 5xx', what: 'INFO: upstream reset'}),
      card(ACCENTS, {num: 5, label: 'ERROR lines', what: 'Invoice \u00c9QUIPMENT failed for \u00d8stergaard, \u0141ukasz, \u00deora and the Stra\u00dfe \u00c6ble list'})],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot() {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state()));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=anoms', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'nina.white@example.com', operator: true, teams: [], tags: []});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors};
}

const shown = (doc) => [...doc.querySelectorAll('#anoms article.inc')].map((c) => c.dataset.key).sort();

async function lowercaseWordsFindCapitalisedCardsAndFoldedLettersMatchTheirPlainSpellingBothWays() {
  //given
  const {w, doc, errors} = await boot();
  const box = doc.getElementById('card-search');
  const type = async (q) => { box.value = q; box.dispatchEvent(new w.Event('input', {bubbles: true})); await wait(30); return shown(doc); };

  //when
  const found = {};
  for (const q of ['api', 'internal', 'ingress', 'info', 'API', 'invoice', 'INVOICE', '\u00e9quipment', 'EQUIPMENT', 'ostergaard', '\u00f8stergaard', 'lukasz', 'thora', '\u00fe\u00f3ra', 'strasse', 'STRA\u00dfE', 'aeble', '\u00e6ble']) found[q] = await type(q);

  //then
  assert.deepStrictEqual(found.api, [FEIGN]);
  assert.deepStrictEqual(found.internal, [FEIGN]);
  assert.deepStrictEqual(found.ingress, [GW]);
  assert.deepStrictEqual(found.info, [GW]);
  assert.deepStrictEqual(found.API, [FEIGN]);
  for (const q of ['invoice', 'INVOICE', '\u00e9quipment', 'EQUIPMENT', 'ostergaard', '\u00f8stergaard', 'lukasz', 'thora', '\u00fe\u00f3ra', 'strasse', 'STRA\u00dfE', 'aeble', '\u00e6ble']) assert.deepStrictEqual(found[q], [ACCENTS], q);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the card search folds case and letters with marks or strokes: api, internal and ingress find cards written in capitals, and words with accented, stroked, sharp or joined letters match their plain spelling in either case');
}

(async () => {
  await lowercaseWordsFindCapitalisedCardsAndFoldedLettersMatchTheirPlainSpellingBothWays();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
