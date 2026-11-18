const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const DISK = 'pvc-disk|cluster-logging/elasticsearch-data-2';
const CART = 'log|checkout-service|ERROR|boom';
const KID = 'log|shop-web|WARN|OrderDetailPage getOrderDetail';
const DONE = 'err|members/sms-sender';

function state() {
  const card = (key, extra) => Object.assign({key, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'x', src: 'logs', sig: 'log', now: 1, firstSeen: iso(30), lastSeen: iso(1)}, extra || {});
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(DISK, {num: 9, label: 'Volume disk', owner: 'Zoe Hart', what: 'Volume is 83 % full'}), card(CART, {num: 14, label: 'ERROR lines', ticket: 'WEB-204', what: 'NullPointerException in OrderService'}),
      card(KID, {num: 64, parent: CART, what: 'storeSlug brightshop not found'}), card(DONE, {num: 35, status: 'cleared', triage: 'handled', clearedAt: iso(5), what: '\u00c9clair orders stopped'})],
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

const shown = (doc) => [...doc.querySelectorAll('#anoms article.inc')].map((c) => c.dataset.key);

async function theSearchBoxFindsCardsInEveryListByNumberTextAssigneeTicketAndMergedCards() {
  //given
  const {w, doc, errors} = await boot();
  const box = doc.getElementById('card-search');
  const type = async (q) => { box.value = q; box.dispatchEvent(new w.Event('input', {bubbles: true})); await wait(30); return shown(doc); };
  const before = shown(doc);

  //when
  const byNumber = await type('#35');
  const note = doc.getElementById('card-search-n').textContent;
  const hash = decodeURIComponent(w.location.hash);
  const byPlainNumber = await type('9');
  const byAssignee = await type('zoe');
  const byTicket = await type('web-204');
  const byText = await type('nullpointer order');
  const byKid = await type('brightshop');
  const accented = await type('\u00e9clair');
  const nothing = await type('no-such-thing');
  const none = doc.getElementById('card-search-n').textContent;
  const cleared = await type('');

  //then
  assert.deepStrictEqual(before.sort(), [DISK, CART, DONE].sort(), 'All before the search');
  assert.deepStrictEqual(byNumber, [DONE], 'a handled card is found too');
  assert.strictEqual(note, '1 card found in every list');
  assert.ok(hash.endsWith('#t=anoms'), hash);
  assert.deepStrictEqual(byPlainNumber, [DISK]);
  assert.deepStrictEqual(byAssignee, [DISK]);
  assert.deepStrictEqual(byTicket, [CART]);
  assert.deepStrictEqual(byText, [CART]);
  assert.deepStrictEqual(byKid, [CART], 'a merged card is found through its parent');
  assert.deepStrictEqual(accented, [DONE], 'a lower-case accented word finds the capitalised one');
  assert.deepStrictEqual(nothing, []);
  assert.strictEqual(none, 'No card matches');
  assert.deepStrictEqual(cleared.sort(), [DISK, CART, DONE].sort(), 'clearing the box goes back to the list that was open');
  assert.strictEqual(doc.getElementById('card-search-n').textContent, '');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the search box finds cards in every list by number, words, assignee, ticket and merged cards, says how many it found, and goes back to the earlier list when cleared');
}

(async () => {
  await theSearchBoxFindsCardsInEveryListByNumberTextAssigneeTicketAndMergedCards();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
