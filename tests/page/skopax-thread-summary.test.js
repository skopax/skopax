const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString().replace(/\.\d{3}Z$/, 'Z');
const MAIN = 'log|checkout-service|ERROR|boom', SHORT = 'log|search-api|ERROR|slow', BARE = 'log|orders-api|ERROR|stuck';
const LINK = 'https://example.slack.com/archives/C1/p1792224000000100';
const BULLETS = ['Correction: the timeouts come from the payment client, not the database.', 'bea.cole says the first answer pointed at the wrong pool.', 'carl.diaz raises the client timeout in OPS-123.', 'Open: why did it start today?'];
const HOSTILE = 'log|cart-api|ERROR|odd', EVIL = ['<script>window.__pwned = 1</script> & <b>bold</b>', '<img src=x onerror="window.__pwned = 2">'];
const AT = iso(12);

function card(key, num, extra) {
  return Object.assign({key, num, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'ERROR lines', src: 'logs', sig: 'log', now: 5, d1: 1, d7: 2, firstSeen: iso(30), lastSeen: iso(1), what: 'Errors went up after the deploy'}, extra || {});
}
function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(MAIN, 11, {threadSummary: {text: 'x', bullets: BULLETS, at: AT, upto: '1792224000.000140', count: 7, permalink: LINK, model: 'claude-haiku-5-5'}}),
      card(SHORT, 12, {threadSummary: {text: 'x', bullets: BULLETS.slice(0, 2), at: AT, upto: '1792224000.000140', count: 4, permalink: 'javascript:alert(1)'}}),
      card(BARE, 13), card(HOSTILE, 14, {threadSummary: {text: 'x', bullets: EVIL, at: AT, upto: '1792224000.000140', count: 3}})],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(opts) {
  opts = opts || {};
  const errors = [];
  const vc = new VirtualConsole(); vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const kept = Object.assign({}, opts.initial || {});
  const dom = new JSDOM(pageHtml(TEMPLATE, {view: 'board'}).replace('/*__SKOPAX_DATA__*/', () => JSON.stringify(state()).replace(/</g, '\\u003c')), {
    url: 'https://skopax.example.com/#t=anoms/all', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {}; w.Element.prototype.scrollIntoView = function () {};
      Object.defineProperty(w, 'localStorage', {value: {getItem: (k) => (k in kept ? kept[k] : null), setItem: (k, v) => { kept[k] = String(v); }, removeItem: (k) => { delete kept[k]; }}, configurable: true});
      if (opts.lang) {
        const raw = JSON.parse(fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'locale', opts.lang, 'page.json'), 'utf8'));
        w.SKOPAX_I18N = {[opts.lang]: Object.assign({}, raw.page, {'@rules': raw.rules, '@months': raw.months, '@days': raw.days, '@tails': raw.tails, '@th': raw.headers})};
        w.document.cookie = 'skopax_lang=' + opts.lang + '; path=/';
      }
      w.fetch = async (p, init) => {
        p = String(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (init && init.method === 'POST') return ok({id: 'x', status: 'requested'});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'op@example.com', operator: true, teams: [], routes: [], tags: []});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/state')) return ok(state());
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors, kept};
}
const tileOf = (doc, key) => [...doc.querySelectorAll('#anoms .tile')].find((t) => t.dataset.key === key);
async function openCard(doc, key) {
  if (doc.querySelector('.cd-close')) doc.querySelector('.cd-close').click();
  tileOf(doc, key).click();
  await wait(60);
  return doc.querySelector('.card-drawer article.inc');
}
const toggle = (w, d, on) => { d.open = on; d.dispatchEvent(new w.Event('toggle')); };
const shown = (box) => [...box.querySelectorAll('ul.thread-sum li')].filter((li) => !li.closest('details:not([open])')).map((li) => li.textContent);

async function theSummaryIsAtTheTopOfDetailsFoldedToTwoBullets() {
  //given
  const {w, doc, errors, kept} = await boot();

  //when
  const art = await openCard(doc, MAIN);
  const details = art.querySelector('.cd-details');
  const box = details.querySelector('section.card-thread');

  //then
  assert.ok(box, 'the opened card shows the thread block');
  const blocks = [...details.querySelectorAll('section, details.tech-fold')];
  assert.ok(blocks.indexOf(box) < blocks.indexOf(details.querySelector('section.card-what')), 'above What happened');
  assert.strictEqual(box.querySelector('h5').firstChild.textContent, 'From the Slack thread');
  assert.strictEqual(box.querySelector('h5 .h5n').textContent, 'updated ' + w.dayhm(AT) + ' \u00b7 7 messages');
  const link = box.querySelector('a.thread-link');
  assert.strictEqual(link.getAttribute('href'), LINK);
  assert.strictEqual(link.getAttribute('target'), '_blank');
  assert.deepStrictEqual(shown(box), BULLETS.slice(0, 2), 'folded to the first two');
  const fold = box.querySelector('details[data-fold="thread"]');
  assert.ok(fold && !fold.open, 'the rest is folded');
  assert.strictEqual(fold.querySelector('summary').textContent, 'Show 2 more');

  //when
  toggle(w, fold, true);
  const again = await openCard(doc, MAIN);

  //then
  assert.strictEqual(kept['skopax.fold.thread'], '1', 'the fold kind remembers it was opened');
  assert.deepStrictEqual(shown(again.querySelector('section.card-thread')), BULLETS, 'all bullets after opening');
  assert.deepStrictEqual(errors, []);
}

async function aShortSummaryHasNoFoldAndAnUnsafeLinkIsLeftOut() {
  //given
  const {doc} = await boot();

  //when
  const art = await openCard(doc, SHORT);
  const box = art.querySelector('section.card-thread');

  //then
  assert.deepStrictEqual(shown(box), BULLETS.slice(0, 2));
  assert.strictEqual(box.querySelector('details'), null, 'two bullets need no fold');
  assert.strictEqual(box.querySelector('a'), null, 'only an https link to the thread is shown');
}

async function noSummaryNoBlockAndTilesStayTheSame() {
  //given
  const {doc} = await boot();

  //when
  const art = await openCard(doc, BARE);
  const tile = tileOf(doc, MAIN);

  //then
  assert.strictEqual(art.querySelector('section.card-thread'), null);
  assert.ok(!/From the Slack thread/.test(tile.textContent), 'board tiles do not show the summary');
}

async function hostileBulletsStayText() {
  //given
  const {w, doc} = await boot();

  //when
  const art = await openCard(doc, HOSTILE);
  const box = art.querySelector('section.card-thread');
  await wait(60);

  //then
  assert.deepStrictEqual(shown(box), EVIL, 'the bullets show as written text');
  assert.strictEqual(box.querySelector('script, b, img'), null, 'no markup from a bullet reaches the page');
  assert.strictEqual(w.__pwned, undefined);
}

async function theHeadingIsTranslated() {
  for (const [lang, head, meta] of [['de', 'Aus dem Slack-Thread', 'aktualisiert'], ['es', 'Del hilo de Slack', 'actualizado'], ['fr', 'Du fil Slack', 'mis \u00e0 jour'], ['tr', 'Slack konu\u015fmas\u0131ndan', 'g\u00fcncellendi']]) {
    //given
    const {w, doc} = await boot({lang});

    //when
    const art = await openCard(doc, MAIN);
    const box = art.querySelector('section.card-thread');

    //then
    assert.strictEqual(box.querySelector('h5').firstChild.textContent, head, lang);
    assert.ok(box.querySelector('h5 .h5n').textContent.startsWith(meta + ' '), lang);
    assert.ok(box.querySelector('h5 .h5n').textContent.includes(w.dayhm(AT)), lang);
    assert.deepStrictEqual(shown(box), BULLETS.slice(0, 2), lang + ': bullets stay as written');
  }
}

(async () => {
  for (const t of [theSummaryIsAtTheTopOfDetailsFoldedToTwoBullets, aShortSummaryHasNoFoldAndAnUnsafeLinkIsLeftOut, noSummaryNoBlockAndTilesStayTheSame, hostileBulletsStayText, theHeadingIsTranslated]) {
    try { await t(); console.log('PASS ' + t.name); } catch (e) { console.log('FAIL ' + t.name + '\n' + (e && e.stack || e)); process.exitCode = 1; }
  }
  process.exit(process.exitCode || 0);
})();
