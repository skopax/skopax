const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const KAFKA = 'kafka-lag|commerce/orders-consumer';
const CART = 'log|checkout-service|ERROR|boom';

function state() {
  const card = (key, extra) => Object.assign({key, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'x', src: 'logs', sig: 'log', now: 1, firstSeen: iso(30), lastSeen: iso(1)}, extra || {});
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(KAFKA, {svc: 'orders-consumer', ns: 'commerce', src: 'kafka', sig: 'kafka-lag', what: 'kafka lag 5000'}),
      card(CART, {ns: 'shop', label: 'ERROR lines', what: 'NullPointerException in OrderService', slackDraft: {text: 'draft text', by: 'Claude', at: iso(5)}})],
    services: [{svc: 'checkout-service', ns: 'shop', kind: 'app', req: 10}], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
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
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'nina.white@example.com', operator: true, slack: true, teams: [], tags: []});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors};
}

const shown = (doc) => [...doc.querySelectorAll('#anoms article.inc')].map((c) => c.dataset.key);
const cardOf = (doc, k) => doc.querySelector('#anoms article.inc[data-key="' + k + '"]');

async function aLinkToACardTheSearchHidesDropsTheSearchAndOpensTheCard() {
  //given
  const {w, doc, errors} = await boot();
  const box = doc.getElementById('card-search');
  const type = async (q) => { box.value = q; box.dispatchEvent(new w.Event('input', {bubbles: true})); await wait(30); return shown(doc); };
  const edit = [...doc.querySelectorAll('#slackq .sq-acts button')].find((b) => b.textContent === 'Edit');

  //when
  const searched = await type('kafka');
  edit.click();
  await wait(30);
  const fromQueue = [shown(doc).includes(CART), box.value, cardOf(doc, CART) && cardOf(doc, CART).classList.contains('linked'), cardOf(doc, CART) && !cardOf(doc, CART).querySelector('.draft-form').hidden];
  await type('kafka');
  doc.querySelector('#svcs tbody tr.clk').click();
  await wait(30);
  const fromServices = [shown(doc), box.value, doc.getElementById('card-search-n').textContent];
  await type('nullpointer');
  w.showLinked(encodeURIComponent(CART));
  const kept = [box.value, shown(doc)];

  //then
  assert.deepStrictEqual(searched, [KAFKA]);
  assert.deepStrictEqual(fromQueue, [true, '', true, true], 'Edit in the Slack queue drops the search, rings the card and opens its draft form');
  assert.deepStrictEqual(fromServices, [[CART], '', ''], 'a Services row drops a search that hides all of its cards');
  assert.deepStrictEqual(kept, ['nullpointer', [CART]], 'a search that already shows the linked card stays');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a card link (Slack queue Edit, a Services row) drops a card search that hides its card and opens it; a search that shows the card stays');
}

(async () => {
  await aLinkToACardTheSearchHidesDropsTheSearchAndOpensTheCard();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
