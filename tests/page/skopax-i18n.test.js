const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const KEY = 'log|checkout-service|ERROR|boom';
const DONE = 'err|shop/orders-service';

function state() {
  const card = (key, extra) => Object.assign({key, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'Open', src: 'logs', sig: 'log', now: 1, firstSeen: iso(30), lastSeen: iso(1)}, extra || {});
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(KEY, {num: 14, suspects: ['Open the door'], recommend: 'Investigate the deploy', triage: 'dived', triagedBy: 'pod'}), card(DONE, {num: 35, status: 'cleared', clearedAt: iso(5)})],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(lang, pack, navLang) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state()));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=anoms/all', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      if (pack) w.SKOPAX_I18N = pack;
      if (navLang) {
        Object.defineProperty(w.navigator, 'language', {get: () => navLang, configurable: true});
        Object.defineProperty(w.navigator, 'languages', {get: () => [navLang], configurable: true});
      }
      if (lang) w.document.cookie = 'skopax_lang=' + lang + '; path=/';
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', operator: true, teams: [], tags: [], chat: true});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(500);
  return {w: dom.window, doc: dom.window.document, errors};
}

const cardOf = (doc, k) => doc.querySelector('#anoms article.inc[data-key="' + k + '"]');
const tabText = (doc, id) => doc.getElementById('tab-' + id).textContent.replace(/[\d+\u2013\s]+$/, '').trim();

async function englishAndAutoOnAnEnglishBrowserChangeNothing() {
  //given
  const english = await boot('en');
  const auto = await boot('');

  //then
  for (const {doc} of [english, auto]) {
    assert.strictEqual(tabText(doc, 'anoms'), 'Board');
    assert.ok([...cardOf(doc, KEY).querySelectorAll('h5')].some((h) => h.firstChild.textContent === 'Suspects'));
    assert.notStrictEqual(doc.documentElement.lang, 'nl');
  }
  assert.strictEqual(english.w.tx('Open'), 'Open');
  assert.deepStrictEqual(english.errors.concat(auto.errors), []);
  english.w.close(); auto.w.close();
  console.log('PASS with English picked, or Auto on an English browser, the page is unchanged');
}

const openChip = (doc) => doc.querySelector('#panel-anoms .qf-c[data-f="open"]');
const chipLabel = (doc) => openChip(doc).firstChild.textContent.trim();

async function aRegisteredPackTranslatesAndNoPackLeavesEnglish() {
  //given
  const pack = {xx: {'Open': 'Ouvrir', 'Board': 'Board xx'}};
  const withPack = await boot('xx', pack);
  const withoutPack = await boot('xx');
  const otherLang = await boot('en', pack);

  //then
  assert.strictEqual(chipLabel(withPack.doc), 'Ouvrir');
  assert.strictEqual(tabText(withPack.doc, 'anoms'), 'Board xx');
  assert.strictEqual(withPack.w.tx('Open'), 'Ouvrir');
  assert.strictEqual(withPack.doc.documentElement.lang, 'xx');
  assert.strictEqual(chipLabel(withoutPack.doc), 'Open');
  assert.strictEqual(withoutPack.w.tx('Open'), 'Open');
  assert.strictEqual(chipLabel(otherLang.doc), 'Open');
  assert.deepStrictEqual(withPack.errors.concat(withoutPack.errors, otherLang.errors), []);
  withPack.w.close(); withoutPack.w.close(); otherLang.w.close();
  console.log('PASS a pack registered in SKOPAX_I18N for the chosen language translates the page, and without one everything stays English');
}

async function aMissingKeyFallsBackToEnglishWhileTheRestIsTranslated() {
  //given
  const {w, doc, errors} = await boot('xx', {xx: {'Open': 'Ouvrir'}});

  //then
  assert.strictEqual(chipLabel(doc), 'Ouvrir');
  assert.strictEqual(tabText(doc, 'anoms'), 'Board', 'a text the pack does not know stays English');
  assert.strictEqual(w.tx('Board'), 'Board');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a pack missing a key leaves that text in English');
}

async function theBrowserLanguageAloneSelectsARegisteredPack() {
  //given
  const {w, doc, errors} = await boot('', {xx: {'Open': 'Ouvrir'}}, 'xx-YY');
  const other = await boot('', {xx: {'Open': 'Ouvrir'}}, 'en-US');

  //then
  assert.ok(!/skopax_lang/.test(doc.cookie));
  assert.strictEqual(chipLabel(doc), 'Ouvrir');
  assert.strictEqual(w.tx('Open'), 'Ouvrir');
  assert.strictEqual(chipLabel(other.doc), 'Open');
  assert.deepStrictEqual(errors.concat(other.errors), []);
  w.close(); other.w.close();
  console.log('PASS with no cookie, navigator.language alone picks the registered pack');
}

async function aNumberShapedKeyIsTranslatedWithItsValueSubstituted() {
  //given
  const {w, errors} = await boot('xx', {xx: {'{0} cards': '{0} cartes', '{0} of {1}': '{0} sur {1}'}});

  //then
  assert.strictEqual(w.tx('3 cards'), '3 cartes');
  assert.strictEqual(w.tx('2 of 10'), '2 sur 10');
  assert.strictEqual(w.tx('  4 cards '), '  4 cartes ', 'surrounding spaces are kept');
  assert.strictEqual(w.tx('no number here'), 'no number here');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a {0} key translates a text with a number and puts the value back');
}

(async () => {
  await aMissingKeyFallsBackToEnglishWhileTheRestIsTranslated();
  await theBrowserLanguageAloneSelectsARegisteredPack();
  await aNumberShapedKeyIsTranslatedWithItsValueSubstituted();
  await englishAndAutoOnAnEnglishBrowserChangeNothing();
  await aRegisteredPackTranslatesAndNoPackLeavesEnglish();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
