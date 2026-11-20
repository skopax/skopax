const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const {short} = require('./skopax-zone');
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

function card(key, src, sig, extra) {
  return Object.assign({key, src, sig, svc: key.split('|')[1].split('/').pop(), ns: 'web', label: sig, sev: 'warn', status: 'open', now: 1, firstSeen: iso(60), lastSeen: iso(1)}, extra || {});
}

function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'Typos', points: [], at: iso(1)},
    anomalies: [
      card('page-typos|pages//shipping-calculator', 'pagescan', 'page-typos', {url: 'https://shop.example.com/shipping-calculator?amount=1000', what: '1 typo on https://shop.example.com/shipping-calculator?amount=1000: "bettter" → "better". Found by the page scan.'}),
      card('page-typos|pages//gift-cards', 'pagescan', 'page-typos', {status: 'cleared', clearedAt: iso(30), sev: 'info'}),
      card('page-layout|pages//garden-chairs', 'pagescan', 'page-layout', {gone: {status: 404, since: '2026-11-09T12:45:00Z'}}),
      card('log|members/sms-sender|ERROR', 'logs', 'log', {sev: 'crit'}),
    ],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(hash) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state()));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/' + hash, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: null, operator: false});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/nodes')) return ok({nodes: []});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(300);
  const doc = dom.window.document;
  return {w: dom.window, doc, errors,
    shown: () => [...doc.querySelectorAll('#anoms article.inc')].map((c) => c.dataset.key),
    chip: (t) => doc.querySelector('#tab-sub-anoms button[data-type="' + t + '"]'),
    note: () => doc.getElementById('filter-note')};
}

async function aLinkOpensTheFilterAndTypeAndTheChipsKeepTheLinkCurrent() {
  //given
  const {w, doc, errors, shown, chip, note} = await boot('#t=anoms/all/page-typos');

  //then
  assert.deepStrictEqual(shown(), ['page-typos|pages//shipping-calculator', 'page-typos|pages//gift-cards'], 'all typo cards, open and cleared');
  assert.ok(note().classList.contains('on') && /Showing All · Typos\s*2/.test(note().querySelector('.fn-cur').textContent));
  assert.strictEqual(chip('page-typos').getAttribute('aria-pressed'), 'true');
  assert.deepStrictEqual([...doc.querySelectorAll('#tab-sub-anoms .types-g button[data-type]')].map((b) => [b.querySelector('.lbl').textContent, b.querySelector('.count').textContent]),
    [['Typos', '2'], ['Layout', '1'], ['Log errors', '1']], 'the types on the page, most cards first, with readable names');
  assert.strictEqual(w.location.hash, '#t=anoms/all/page-typos');

  //when
  chip('page-layout').click();

  //then
  assert.deepStrictEqual(shown(), ['page-layout|pages//garden-chairs']);
  assert.strictEqual(w.location.hash, '#t=anoms/all/page-layout', 'the link follows the chip');

  //when
  chip('page-layout').click();

  //then
  assert.strictEqual(shown().length, 4, 'a second click shows every type again');
  assert.strictEqual(w.location.hash, '#t=anoms/all', 'All is written out, since Open is the default');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS #t=anoms/all/page-typos opens All with Typos, a type chip filters and updates the link, a second click clears it');
}

async function anOpenTypeWithNothingOpenSaysSoAndEveryTypeClearsIt() {
  //given
  const {w, doc, errors, shown, chip, note} = await boot('#t=anoms/open/page-layout');

  //when
  chip('page-typos').click();
  const typos = shown();
  doc.querySelector('#tab-sub-anoms button[data-f="cleared"]').click();
  const cleared = shown();
  const hash = w.location.hash;
  doc.getElementById('clear-type').click();

  //then
  assert.deepStrictEqual(typos, ['page-typos|pages//shipping-calculator'], 'Open with Typos: only the open typo');
  assert.deepStrictEqual(cleared, ['page-typos|pages//gift-cards'], 'the type stays while the filter changes');
  assert.strictEqual(hash, '#t=anoms/cleared/page-typos');
  assert.ok(!/Typos/.test(note().textContent) && w.location.hash === '#t=anoms/cleared', 'Every type clears only the type');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the type survives a filter change, the link carries both, and Every type clears only the type');
}

async function anEmptyTypeExplainsAndACardLinkIsNeverHiddenByTheType() {
  //given
  const {w, doc, errors, shown} = await boot('#t=anoms/open/cert-expiry');

  //when
  const empty = doc.querySelector('#anoms .empty-card').textContent;
  w.location.hash = '#a=' + encodeURIComponent('log|members/sms-sender|ERROR');
  w.dispatchEvent(new w.HashChangeEvent('hashchange'));
  await wait(50);

  //then
  assert.ok(/No open Certificates cards here: All shows the stopped, muted and cleared ones too\./.test(empty));
  assert.ok(shown().includes('log|members/sms-sender|ERROR'), 'a card link drops a type that would hide the card');
  assert.ok(doc.querySelector('#anoms article.inc.linked'));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a type with no open cards explains itself, and a card link clears a type that would hide the card');
}

async function aShortTypeLinkOpensOpenWithThatType() {
  //given
  const {w, errors, shown} = await boot('#t=anoms/page-typos');

  //then
  assert.deepStrictEqual(shown(), ['page-typos|pages//shipping-calculator']);
  assert.strictEqual(w.location.hash, '#t=anoms/open/page-typos', 'the link is written out in full');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS #t=anoms/page-typos opens Open with Typos and writes the full link');
}

async function typeCountsFollowTheFilterTheGroupFoldsAndTheViewCopiesItsLink() {
  //given
  const {w, doc, errors, chip, note} = await boot('#t=anoms/cleared');
  const types = () => [...doc.querySelectorAll('#tab-sub-anoms .types-g button[data-type]')].map((b) => [b.dataset.type, b.querySelector('.count').textContent]);
  let copied = null;
  Object.defineProperty(w.navigator, 'clipboard', {configurable: true, value: {writeText: async (t) => { copied = t; }}});

  //then
  assert.deepStrictEqual(types(), [['page-typos', '1']], 'under Cleared only the cleared typo counts, other types are not offered');

  //when
  doc.querySelector('#tab-sub-anoms button[data-f="open"]').click();

  //then
  assert.deepStrictEqual(types(), [['page-layout', '1'], ['log', '1'], ['page-typos', '1']], 'under Open the open cards count, ties by name');

  //when
  chip('page-layout').click();
  const fold = () => doc.querySelector('#tab-sub-anoms [data-fold="types"]');
  fold().click();

  //then
  assert.strictEqual(fold().getAttribute('aria-expanded'), 'false');
  assert.deepStrictEqual(types(), [['page-layout', '1']], 'folded, only the active type stays');
  assert.strictEqual(w.localStorage.getItem('skopax-types-fold'), '1');

  //when
  doc.getElementById('copy-f').click();
  await wait(20);

  //then
  assert.strictEqual(copied, 'https://skopax.example.com/#t=anoms/open/page-layout');
  assert.ok(/Copied/.test(doc.getElementById('copy-f').textContent));
  fold().click();
  assert.strictEqual(types().length, 3, 'unfolding shows every type again');
  assert.ok(note().querySelector('#copy-f'), 'the filter line keeps its Copy link');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS type counts follow the chosen filter, the Types group folds to the active type, and Copy link copies the filter and type link');
}

async function aPageCardOpensItsPageFromTheHeadAndFromItsText() {
  //given
  const {w, doc, errors} = await boot('#t=anoms/open');
  const cardOf = (k) => doc.querySelector('#anoms article.inc[data-key="' + k + '"]');
  const page = cardOf('page-typos|pages//shipping-calculator');

  //then
  const head = page.querySelector('.inc-title a.open-page');
  assert.ok(head, 'the folded head carries the page link');
  assert.strictEqual(head.getAttribute('href'), 'https://shop.example.com/shipping-calculator?amount=1000');
  assert.strictEqual(head.getAttribute('target'), '_blank');
  assert.ok(/Open page/.test(head.textContent));
  assert.ok(!page.classList.contains('open'), 'the card is still folded');

  //when
  head.click();

  //then
  assert.ok(!page.classList.contains('open'), 'clicking the link does not unfold the card');
  const inText = page.querySelector('.what a');
  assert.strictEqual(inText && inText.getAttribute('href'), 'https://shop.example.com/shipping-calculator?amount=1000', 'the URL in the card text is a link');
  assert.ok(!cardOf('page-layout|pages//garden-chairs').querySelector('a.open-page'), 'a card without a url gets no link');
  assert.ok(!cardOf('log|members/sms-sender|ERROR').querySelector('a.open-page'));
  const gone = cardOf('page-layout|pages//garden-chairs');
  assert.strictEqual(gone.querySelector('.inc-title .page-gone').textContent, 'Page gone · 404', 'a removed page says so on the folded head');
  assert.strictEqual(gone.querySelector('.rec-line').textContent, 'NextThe page answers HTTP 404 since ' + short('2026-11-09T12:45:00Z') + ': if it was taken down on purpose, Resolve this card.');
  assert.ok(!page.querySelector('.page-gone') && !page.querySelector('.rec-line'), 'a live page gets neither');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a page card opens its page from the folded head and from the URL in its text');
}

async function everyPluginSigHasAReadableTypeNameAndOldPageGlobalsAreGone() {
  //given
  const t = await boot('');
  const w = t.w;

  //when
  const names = ['no-access', 'db-disk', 'hpa-max', 'jvm-heap', 'apm-fail', 'airbyte-failed', 'varnish-fields', 'varnish-cache-fields', 'logflow-fields', 'csp-fields',
    'lf-errors', 'cf-health', 'edge-conn', 'kafka-broker', 'redis-down', 'qan-load', 'pg-saturation', 'mongo-state', 'logs-stopped'].map((x) => [x, w.typeName(x)]);

  //then
  names.forEach(([sig, name]) => assert.ok(name !== sig.replace(/-/g, ' ') && /^[A-Z]/.test(name), sig + ' shows as ' + name));
  assert.strictEqual(w.typeName('no-access'), 'No access');
  for (const old of ['CAP', 'LF', 'KF', 'kfK', 'AB', 'abAll', 'EG', 'egAll', 'JB', 'jbAll']) assert.strictEqual(typeof w[old], 'undefined', old + ' is not a page global any more');
  w.close();
  console.log('PASS every plug-in signature has a readable type name, and the old section globals are gone');
}

(async () => {
  await aPageCardOpensItsPageFromTheHeadAndFromItsText();
  await typeCountsFollowTheFilterTheGroupFoldsAndTheViewCopiesItsLink();
  await aShortTypeLinkOpensOpenWithThatType();
  await aLinkOpensTheFilterAndTypeAndTheChipsKeepTheLinkCurrent();
  await anOpenTypeWithNothingOpenSaysSoAndEveryTypeClearsIt();
  await anEmptyTypeExplainsAndACardLinkIsNeverHiddenByTheType();
  await everyPluginSigHasAReadableTypeNameAndOldPageGlobalsAreGone();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
