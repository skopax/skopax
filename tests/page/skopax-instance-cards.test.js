const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const MAIN = 'err|shop/checkout-service';
const EAST = MAIN + '@east';
const LOG = 'log|orders-service|ERROR|Could not bind encryption';

function card(key, src, sig, extra) {
  return Object.assign({key, src, sig, svc: 'checkout-service', ns: 'shop', label: sig, sev: 'warn', status: 'open', now: 1, firstSeen: iso(60), lastSeen: iso(1)}, extra || {});
}

function state(anomalies) {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'h', points: [], at: iso(1)}, anomalies, services: [], history: [], logTop: [], routes: [], deploys: []};
}

async function boot(anomalies, hash) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state(anomalies)));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/' + (hash || ''), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: null, operator: false});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(300);
  return {w: dom.window, doc: dom.window.document, errors};
}

async function aLinkToAGonePrimaryCardNeverLandsOnItsSecondInstanceTwin() {
  //given
  const {w, doc, errors} = await boot([card(EAST, 'prom', 'err', {cluster: 'east'})], '#a=' + encodeURIComponent(MAIN));

  //then
  assert.ok(doc.getElementById('link-note'), 'the east twin is another card, so the link says the card is gone');
  assert.deepStrictEqual(errors, []);
  w.close();

  //when
  const own = await boot([card(EAST, 'prom', 'err', {cluster: 'east'})], '#a=' + encodeURIComponent(EAST));
  const near = await boot([card(MAIN, 'prom', 'err')], '#a=' + encodeURIComponent(MAIN + '|db'));

  //then
  assert.ok(!own.doc.getElementById('link-note'), 'its own link still finds it');
  assert.ok(!near.doc.getElementById('link-note'), 'a nearby key of another shape is still found as before');
  own.w.close();
  near.w.close();
  return 'a link to a primary card that is gone never lands on its second-instance twin';
}

async function aSecondInstanceLogCardTitleAndTheNoAccessTypeReadPlainly() {
  //given
  const east = card(LOG + '@east', 'logs', 'log', {cluster: 'east'});
  const plain = card(LOG, 'logs', 'log');
  const {w, errors} = await boot([east, plain, card('no-access|-/prom|metrics', 'prom', 'no-access')]);

  //then
  assert.strictEqual(w.cardTitle(east), w.cardTitle(plain), 'the instance tail is not part of the log pattern title');
  assert.strictEqual(w.cardTitle(plain), 'Could not bind encryption');
  assert.strictEqual(w.typeName('no-access'), 'No access');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a second instance log card has the pattern title of its twin, and the no-access type reads No access';
}

(async () => {
  for (const t of [aLinkToAGonePrimaryCardNeverLandsOnItsSecondInstanceTwin, aSecondInstanceLogCardTitleAndTheNoAccessTypeReadPlainly]) {
    console.log('PASS ' + (await t()));
  }
})().catch((e) => { console.error('FAIL', e.message); process.exit(1); });
