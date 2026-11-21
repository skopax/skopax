const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const KEY = 'log|checkout-service|ERROR|null total';
const HAND = 'log|catalog-service|ERROR|hand posted';
const WEB = 'https://gitlab.com/acme/shop/checkout-service';
const PATH = 'src/main/kotlin/com/example/shop/CartService.kt';
const SHA = '1a1a1a1a' + 'b'.repeat(32);
const SUGGESTION = 'fun total(cart: Cart): Double? {\n    return cart.total\n}';
const BY = [{name: 'Nina', lines: 3, sha: 'cccccccc', date: '2026-10-20', title: 'OPS-1 total from the cart', url: WEB + '/-/commit/' + 'c'.repeat(40)}];

function fix(extra) {
  return Object.assign({label: 'CartService.kt:3-5', repo: 'checkout-service', dir: 'checkout-service', path: PATH, lines: '3-5', note: 'check that callers handle a null total',
    url: WEB + '/-/blob/' + SHA + '/' + PATH + '#L3-5', sha: '1a1a1a1a', ref: '1a1a1a1a', by: BY,
    current: 'fun total(cart: Cart): Double {\n    return cart.total!!\n}', suggestion: SUGGESTION, language: 'kotlin'}, extra || {});
}
const PLAIN = {label: 'CartClient.kt:40', repo: 'checkout-service', path: 'src/main/kotlin/CartClient.kt', lines: '40', note: 'where the cart comes from', url: WEB + '/-/blob/' + 'a'.repeat(40) + '/src/main/kotlin/CartClient.kt#L40', sha: 'aaaaaaaa', ref: 'master',
  by: [{name: 'Adam', lines: 1, sha: 'dddddddd', date: '2026-10-01', title: 'OPS-2 cart client', url: WEB + '/-/commit/' + 'd'.repeat(40)}]};

function card(key, extra) {
  return Object.assign({key, svc: key.split('|')[1], label: key.split('|')[2], sev: 'warn', status: 'open', now: 12, d1: 2, d7: 3, firstSeen: iso(30), lastSeen: iso(1), ns: '-', src: 'logs',
    suspects: ['total is null on 40 carts'], recommend: 'guard the null total'}, extra || {});
}

function stateAt(rev, cards) {
  return {rev, updatedAt: iso(0), sweptIso: iso(1), nextIso: iso(-59), serverPasses: {prom: {at: iso(3)}}, sources: {prom: 'ok', logs: 'ok'},
    summary: {level: 'warn', headline: 'h', points: [], at: iso(1)}, anomalies: cards, services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, deploys: []};
}

const base = () => [card(KEY, {code: [fix(), PLAIN]}), card(HAND, {code: [{label: 'Hand.kt:1', url: 'https://gitlab.com/acme/x/-/blob/abc/Hand.kt#L1', suggestion: 'fixed()'}]})];

async function boot(serve) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  vc.on('error', (e) => errors.push('console.error ' + e));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(serve.state).replace(/<\//g, '<\\/'));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      if (!serve.noClipboard) Object.defineProperty(w.navigator, 'clipboard', {configurable: true, value: {writeText: async (t) => { (serve.copied = serve.copied || []).push(t); }}});
      w.fetch = async (p) => {
        const path = String(p);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (path.startsWith('/api/me')) return ok(serve.guest ? {loginEnabled: true, user: null, operator: false} : {loginEnabled: true, user: 'op@example.com', operator: true});
        if (path.startsWith('/api/rev')) return ok({rev: serve.state.rev});
        if (path.startsWith('/api/state')) return ok(serve.state);
        if (path.startsWith('/api/server')) return ok({investigate: {mode: 'live', enabled: true, stopped: null, spentToday: 0, cap: 20}});
        if (path.startsWith('/api/investigations') || path.startsWith('/api/actions') || path.startsWith('/api/deploy') || path.startsWith('/api/activity')) return ok([]);
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(300);
  return {dom, w: dom.window, doc: dom.window.document, errors};
}

const article = (doc, key) => [...doc.querySelectorAll('#anoms article.inc')].find((c) => c.dataset.key === key);
const expand = (c) => { if (c.querySelector('.inc-title .fold').getAttribute('aria-expanded') !== 'true') c.querySelector('.inc-title .fold').click(); };
const rows = (box) => [...box.querySelectorAll('.diff tr')].map((tr) => [tr.className, ...[...tr.cells].map((td) => td.textContent)]);
const close = (dom) => setTimeout(() => dom.window.close(), 300);
const DIFF = [
  ['del', '3', '', '-', 'fun total(cart: Cart): Double {'],
  ['del', '4', '', '-', '    return cart.total!!'],
  ['add', '', '3', '+', 'fun total(cart: Cart): Double? {'],
  ['add', '', '4', '+', '    return cart.total'],
  ['ctx', '5', '5', '', '}'],
];

async function suggestedFixShowsTheAddressWhoWhatAndTheDiff() {
  //given
  const {dom, doc, errors} = await boot({state: stateAt(5, base())});
  const c = article(doc, KEY);
  //when
  expand(c);
  const box = c.querySelector('.analysis .fix');
  const at = box.querySelector('.fix-at a');
  const refs = [...c.querySelectorAll('.code-refs li a')].map((a) => a.textContent);
  const people = [...c.querySelectorAll('.people li b')].map((b) => b.textContent);
  //then
  assert.strictEqual(box.querySelector('h5').textContent, 'Suggested fix (untested)');
  assert.strictEqual(at.textContent, 'checkout-service \u00b7 ' + PATH + ':3-5 @ 1a1a1a1a \u2197');
  assert.strictEqual(at.getAttribute('href'), WEB + '/-/blob/' + SHA + '/' + PATH + '#L3-5', 'the address links GitLab at that commit and those lines');
  assert.strictEqual(at.getAttribute('target'), '_blank');
  assert.ok(box.querySelector('.fix-at').compareDocumentPosition(box.querySelector('.fix-by')) & 4, 'who last changed the lines comes after the address');
  assert.strictEqual(box.querySelector('.fix-by li').textContent, 'Last changed by Nina \u00b7 3 of 3 lines \u00b7 OPS-1 total from the cart \u2197 2026-10-20');
  assert.strictEqual(box.querySelector('.fix-by a').getAttribute('href'), BY[0].url);
  assert.ok(box.querySelector('.fix-by').compareDocumentPosition(box.querySelector('.fix-what')) & 4);
  assert.strictEqual(box.querySelector('.fix-what').textContent, 'check that callers handle a null total');
  assert.deepStrictEqual(rows(box), DIFF, 'removed lines first with -, then added with +, line numbers from the span, the unchanged line as context');
  assert.strictEqual(box.querySelector('.diff').getAttribute('tabindex'), '0', 'the scroll box takes the keyboard');
  assert.strictEqual(box.querySelector('.fix-copy').textContent, 'Copy');
  assert.deepStrictEqual(refs, ['CartClient.kt:40 \u2197'], 'the Code list keeps the plain link and does not repeat the fix');
  assert.deepStrictEqual(people.sort(), ['Adam', 'Nina'], 'Responsible still counts who changed the lines of both links');
  assert.strictEqual(c.querySelectorAll('.analysis .fix').length, 1);
  assert.deepStrictEqual(errors, []);
  close(dom);
  console.log('PASS suggestedFixShowsTheAddressWhoWhatAndTheDiff');
}

async function codeTextIsEscapedAndTwoFixesGetTwoBlocks() {
  //given
  const odd = fix({label: 'Tpl.kt:7', path: 'src/Tpl.kt', lines: '7', url: WEB + '/-/blob/' + SHA + '/src/Tpl.kt#L7', current: 'val s = "<b>${x}</b>" // & more', suggestion: 'val s = "<i>${x}</i>"\nlog.info("&lt;")', by: []});
  const {dom, doc, errors} = await boot({state: stateAt(5, [card(KEY, {code: [fix(), odd]})])});
  const c = article(doc, KEY);
  //when
  expand(c);
  const boxes = c.querySelectorAll('.analysis .fix');
  //then
  assert.strictEqual(boxes.length, 2);
  assert.deepStrictEqual(rows(boxes[1]), [['del', '7', '', '-', 'val s = "<b>${x}</b>" // & more'], ['add', '', '7', '+', 'val s = "<i>${x}</i>"'], ['add', '', '8', '+', 'log.info("&lt;")']]);
  assert.strictEqual(boxes[1].querySelector('.diff b'), null, 'code is text, never markup');
  assert.strictEqual(boxes[1].querySelector('.fix-by'), null, 'no blame rows, no line');
  assert.strictEqual(c.querySelector('.code-refs'), null, 'no Code section when every link carries a fix');
  assert.deepStrictEqual(errors, []);
  close(dom);
  console.log('PASS codeTextIsEscapedAndTwoFixesGetTwoBlocks');
}

async function copyPutsTheSuggestionOnTheClipboard() {
  //given
  const serve = {state: stateAt(5, [card(KEY, {code: [fix(), fix({lines: '9', label: 'CartService.kt:9', current: 'a()', suggestion: 'b()'})]})])};
  const {dom, doc, errors} = await boot(serve);
  const c = article(doc, KEY);
  expand(c);
  //when
  const copies = c.querySelectorAll('.fix-copy');
  copies[1].click();
  copies[0].click();
  await wait(20);
  //then
  assert.deepStrictEqual(serve.copied, ['b()', SUGGESTION], 'each Copy copies its own suggestion, never the current lines');
  assert.strictEqual(copies[0].textContent, 'Copied');
  assert.ok(c.classList.contains('open'), 'Copy does not fold the card');
  assert.deepStrictEqual(errors, []);
  close(dom);
  const bare = await boot({state: stateAt(5, base()), noClipboard: true});
  expand(article(bare.doc, KEY));
  const b = article(bare.doc, KEY).querySelector('.fix-copy');
  b.click();
  await wait(20);
  assert.strictEqual(b.textContent, 'Select the text to copy', 'without a clipboard the button says so');
  close(bare.dom);
  console.log('PASS copyPutsTheSuggestionOnTheClipboard');
}

async function guestsSeeTheFixAndCanCopyIt() {
  //given
  const serve = {state: stateAt(5, base()), guest: true};
  const {dom, doc, errors} = await boot(serve);
  const c = article(doc, KEY);
  expand(c);
  //when
  const copy = c.querySelector('.fix-copy');
  copy.click();
  await wait(20);
  //then
  assert.deepStrictEqual(rows(c.querySelector('.fix')), DIFF);
  assert.ok(!copy.disabled && copy.getAttribute('aria-disabled') !== 'true' && !copy.classList.contains('op-btn'), 'copying is not an operator action');
  assert.deepStrictEqual(serve.copied, [SUGGESTION]);
  assert.deepStrictEqual(errors, []);
  close(dom);
  console.log('PASS guestsSeeTheFixAndCanCopyIt');
}

async function theBlockSurvivesALiveUpdate() {
  //given
  const serve = {state: stateAt(5, base())};
  const {dom, w, doc, errors} = await boot(serve);
  expand(article(doc, KEY));
  const before = article(doc, KEY).querySelector('.fix');
  //when
  serve.state = stateAt(6, [card(KEY, {now: 14, code: [fix(), PLAIN]}), base()[1]]);
  await w.__skopaxPoll();
  await wait(50);
  const c = article(doc, KEY);
  const after = c.querySelector('.fix');
  after.querySelector('.fix-copy').click();
  await wait(20);
  serve.state = stateAt(7, [card(KEY, {now: 15, code: [fix({suggestion: 'fun total(cart: Cart): Double = cart.total ?: 0.0'}), PLAIN]}), base()[1]]);
  await w.__skopaxPoll();
  await wait(50);
  const changed = rows(article(doc, KEY).querySelector('.fix'));
  //then
  assert.notStrictEqual(after, before, 'the card was re-rendered');
  assert.ok(c.classList.contains('open') && !c.querySelector('.inc-more').hidden, 'the card stays open');
  assert.deepStrictEqual(rows(after), DIFF, 'the block is rebuilt from the state');
  assert.deepStrictEqual(serve.copied, [SUGGESTION], 'Copy works on the re-rendered button');
  assert.deepStrictEqual(changed, [['del', '3', '', '-', 'fun total(cart: Cart): Double {'], ['del', '4', '', '-', '    return cart.total!!'], ['del', '5', '', '-', '}'],
    ['add', '', '3', '+', 'fun total(cart: Cart): Double = cart.total ?: 0.0']], 'a new suggestion shows on the next update');
  assert.deepStrictEqual(errors, []);
  close(dom);
  console.log('PASS theBlockSurvivesALiveUpdate');
}

async function aSuggestionWithoutServerReadLinesStaysAPlainLink() {
  //given
  const {dom, doc, errors} = await boot({state: stateAt(5, base())});
  const c = article(doc, HAND);
  //when
  expand(c);
  //then
  assert.strictEqual(c.querySelector('.fix'), null, 'no diff without the current lines the server read');
  assert.deepStrictEqual([...c.querySelectorAll('.code-refs li a')].map((a) => a.textContent), ['Hand.kt:1 \u2197']);
  assert.deepStrictEqual(errors, []);
  close(dom);
  console.log('PASS aSuggestionWithoutServerReadLinesStaysAPlainLink');
}

(async () => {
  for (const t of [suggestedFixShowsTheAddressWhoWhatAndTheDiff, codeTextIsEscapedAndTwoFixesGetTwoBlocks, copyPutsTheSuggestionOnTheClipboard, guestsSeeTheFixAndCanCopyIt,
    theBlockSurvivesALiveUpdate, aSuggestionWithoutServerReadLinesStaysAPlainLink]) await t();
})().catch((e) => { console.error(e); process.exit(1); });
