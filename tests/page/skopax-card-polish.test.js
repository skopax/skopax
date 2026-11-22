const pageHtml = require('./skopax-page');
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
async function until(check, ms) {
  const end = Date.now() + (ms || 3000);
  while (Date.now() < end && !check()) await wait(5);
}
const BIZ = 'page-typos|shop.example.com/products';
const TECH = 'log|checkout-service|ERROR|boom';
const PLAIN = 'log|shop-web|ERROR|other';
const TAGS = [{id: 'bizdev', label: 'Business development'}, {id: 'marketing', label: 'Marketing'}, {id: 'seo', label: 'SEO'}, {id: 'product', label: 'Product'},
  {id: 'developer', label: 'Developer'}, {id: 'data', label: 'Data'}, {id: 'infra', label: 'Infrastructure'}, {id: 'analyst', label: 'Analyst'}];

function card(key, extra) {
  return Object.assign({key, svc: key.split('|')[1], label: key.split('|')[2] || 'x', sev: 'warn', status: 'open', now: 12, d1: 2, d7: 3, firstSeen: iso(30), lastSeen: iso(1), ns: '-', src: 'logs'}, extra || {});
}
function stateOf(cards) {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: iso(-59), sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'h', points: [], at: iso(1)},
    anomalies: cards, services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, deploys: []};
}

async function boot(cards, opt) {
  opt = opt || {};
  const errors = [], posts = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(stateOf(cards)));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      if (opt.lang) {
        const raw = JSON.parse(fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'locale', opt.lang, 'page.json'), 'utf8'));
        w.SKOPAX_I18N = {[opt.lang]: Object.assign({}, raw.page, {'@rules': raw.rules, '@months': raw.months, '@days': raw.days, '@tails': raw.tails, '@th': raw.headers})};
        w.document.cookie = 'skopax_lang=' + opt.lang + '; path=/';
      }
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p, o) => {
        const path = String(p);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (o && o.method && o.method !== 'GET') { posts.push([o.method, path, o.body ? JSON.parse(o.body) : null]); return ok(opt.reply ? opt.reply(path, JSON.parse(o.body || '{}')) : {}); }
        if (path.startsWith('/api/me')) return ok({loginEnabled: true, user: 'op@example.com', operator: true, routes: [], tags: opt.tags || TAGS});
        if (path.startsWith('/api/actions')) return ok(opt.actions || []);
        if (path.startsWith('/api/card-history')) return opt.history ? ok(opt.history) : {ok: false, status: 404, json: async () => ({})};
        if (path.startsWith('/api/rev')) return ok({rev: opt.next ? 2 : 1});
        if (path.startsWith('/api/state')) return ok(opt.next ? stateOf(opt.next()) : stateOf(cards));
        if (path.startsWith('/api/investigations') || path.startsWith('/api/deploy') || path.startsWith('/api/requests')) return ok([]);
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(300);
  return {dom, w: dom.window, doc: dom.window.document, errors, posts};
}
const article = (doc, k) => doc.querySelector('#anoms article.inc[data-key="' + k + '"]');
function expand(a) { if (!a.classList.contains('open')) a.querySelector('.inc-title .fold').click(); }

async function theBusinessNoteIsItsOwnRowNamedForTheTeam() {
  //given
  const {w, doc, errors} = await boot([card(BIZ, {tags: [{id: 'seo', by: 'rule', at: iso(5)}]}), card(TECH, {tags: [{id: 'developer', by: 'rule', at: iso(5)}]}), card(PLAIN),
    card('log|x-service|ERROR|y', {tags: [{id: 'marketing', by: 'rule', at: iso(5)}], business: {text: 'Change the banner.', by: 'rule', at: iso(4)}})]);
  const note = (k) => article(doc, k).querySelector('.biz .biz-note');
  //then
  assert.strictEqual(article(doc, BIZ).querySelector('.biz .biz-h .biz-edit'), null, 'the Write button left the Tags row');
  assert.strictEqual(note(BIZ).querySelector('h5').firstChild.textContent, 'Business note');
  assert.strictEqual(note(BIZ).querySelector('.biz-edit').textContent, 'Write note for SEO');
  assert.strictEqual(note(BIZ).querySelector('.biz-edit').hidden, false);
  assert.strictEqual(note('log|x-service|ERROR|y').querySelector('.biz-edit').textContent, 'Edit note');
  for (const k of [TECH, PLAIN]) {
    assert.strictEqual(note(k).querySelector('.biz-edit').hidden, true, 'no button without a business tag: ' + k);
    assert.strictEqual(note(k).querySelector('.biz-txt').textContent, 'Tag a business team to write a note');
    assert.ok(note(k).querySelector('.biz-txt').classList.contains('muted'));
  }
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the business note is its own row: Write note for the tagged team, Edit note once written, a muted hint without a business tag');
}

async function taggingABusinessTeamShowsTheButtonWithoutARepaint() {
  //given
  const {w, doc, errors} = await boot([card(PLAIN)], {reply: (path, body) => ({rev: 2, tags: body.on ? [{id: body.tag, by: 'op@example.com', at: iso(0)}] : [], tagsOff: [], business: null})});
  const box = article(doc, PLAIN).querySelector('.biz');
  //when
  box.querySelector('.biz-tb[data-tag="product"]').click();
  await until(() => !box.querySelector('.biz-edit').hidden);
  //then
  assert.strictEqual(box.querySelector('.biz-edit').textContent, 'Write note for Product');
  assert.strictEqual(box.querySelector('.biz-txt').textContent, 'No action written yet: nothing goes to Teams until one is written.');
  //when
  box.querySelector('.biz-tb[data-tag="product"]').click();
  await until(() => box.querySelector('.biz-edit').hidden);
  //then
  assert.strictEqual(box.querySelector('.biz-txt').textContent, 'Tag a business team to write a note');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS tagging and untagging a business team shows and hides the note button at once');
}

async function aLongCodeRowStacksTheExplanationUnderTheLink() {
  //given
  const file = 'src/main/kotlin/com/example/checkout/' + 'VeryLongName'.repeat(8) + 'Response.kt:17-18';
  const {w, doc, errors} = await boot([card(TECH, {code: [{url: 'https://git.example.com/x', label: file, repo: 'checkout-service', note: 'The field is nullable on one side and not on the other.'}]})]);
  const li = article(doc, TECH).querySelector('.code-refs li');
  //then
  assert.ok(li.querySelector('a.code-file'), 'the link has its own class');
  assert.strictEqual(li.querySelector('a.code-file').title.split('\n')[0], file, 'the full name is in the title');
  assert.ok(li.querySelector('.code-why .cn'), 'the explanation sits in its own block under the link');
  const css = doc.querySelector('style').textContent;
  assert.ok(/\.code-refs li \{[^}]*grid-template-columns: minmax\(0, 1fr\)/.test(css), 'one column: the explanation goes under the link');
  assert.ok(/\.code-refs a\.code-file \{[^}]*text-overflow: ellipsis/.test(css));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a long code row keeps the explanation under the link and cuts the file name with an ellipsis');
}

async function longUnbrokenTextWrapsInsideTheMiddleColumn() {
  //given
  const {w, doc, errors} = await boot([card(TECH)]);
  const css = doc.querySelector('style').textContent;
  //then
  assert.ok(/\.analysis \{[^}]*grid-template-columns: minmax\(0, 1fr\)[^}]*overflow-wrap: anywhere/.test(css), 'the middle column never grows past its track');
  assert.ok(/\.analysis > \* \{[^}]*min-width: 0/.test(css));
  assert.ok(/\.sdraft-h \{[^}]*flex-wrap: wrap/.test(css), 'the draft buttons wrap');
  assert.ok(/\.inc-more \{[^}]*min-width: 0/.test(css));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS long unbroken text wraps inside the middle column and the draft buttons wrap');
}

async function aRefiredCardKeepsItsCapitalInAnotherLanguage() {
  //given
  const old = {id: 'old-1', type: 'resolve', key: TECH, target: TECH, label: 'Resolve checkout-service', status: 'done', requestedAt: iso(3 * 1440 + 1), doneAt: iso(3 * 1440), requestedBy: 'dora.lane@example.com', result: 'Fixed'};
  const {w, doc, errors} = await boot([card(TECH, {firstSeen: iso(30)})], {actions: [old], lang: 'de'});
  await until(() => !article(doc, TECH).querySelector('.last-act').hidden);
  expand(article(doc, TECH));

  //then
  const text = article(doc, TECH).querySelector('.la-before .st').textContent;
  assert.ok(/^[A-Z\u00c0-\u00de]/.test(text), 'the translated word is left as the pack writes it: ' + text);
  assert.ok(text.indexOf('Resolved') < 0, 'and it is translated: ' + text);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a re-fired card leaves a translated Before that word as the pack writes it');
}

async function aRefiredCardLeadsWithReopenedAndNamesTheOldEventOnce() {
  //given
  const fired = iso(30);
  const old = {id: 'old-1', type: 'resolve', key: TECH, target: TECH, label: 'Resolve checkout-service', status: 'done', requestedAt: iso(3 * 1440 + 1), doneAt: iso(3 * 1440), requestedBy: 'dora.lane@example.com', result: 'Fixed'};
  const {w, doc, errors} = await boot([card(TECH, {firstSeen: fired})], {actions: [old]});
  await until(() => !article(doc, TECH).querySelector('.last-act').hidden);
  expand(article(doc, TECH));
  const a = article(doc, TECH), last = a.querySelector('.last-act');
  //then
  assert.strictEqual(last.querySelector('.la-h').textContent, 'Reopened ' + w.dayhm(fired) + ' \u00b7 fired again');
  assert.strictEqual(last.querySelector('.la-before').textContent, 'Before that: resolved ' + w.dayhm(old.doneAt) + ' by dora.lane (Fixed)');
  assert.ok(!/class="st-t"|la-before \.st-t/.test(last.innerHTML + [...doc.querySelectorAll('style')].map((x) => x.textContent).join('')), 'the sentence case is in the English text, not a CSS trick');
  assert.strictEqual(a.querySelector('.acts .stat').textContent, '', 'the head no longer repeats the same event as Earlier');
  assert.ok(!a.textContent.includes('Earlier:'));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a re-fired card leads with Reopened and names the earlier resolve once, as Before that');
}

async function aChangeThatHidesTheCardShowsAToastWithViewAndClear() {
  //given
  let untagged = false;
  const seo = card(BIZ, {num: 7, tags: [{id: 'seo', by: 'rule', at: iso(5)}]});
  const {w, doc, errors} = await boot([seo, card(PLAIN, {num: 8})], {
    reply: () => { untagged = true; return {rev: 2, tags: [], tagsOff: ['seo'], business: null}; },
    next: () => [untagged ? card(BIZ, {num: 7, tags: [], tagsOff: ['seo']}) : seo, card(PLAIN, {num: 8})]});
  w.setType('tag:seo');
  await wait(20);
  expand(article(doc, BIZ));
  //when
  article(doc, BIZ).querySelector('.biz-tb[data-tag="seo"]').click();
  await until(() => doc.querySelector('#toasts .toast'));
  const box = doc.getElementById('toasts'), t = box.querySelector('.toast');
  //then
  assert.strictEqual(article(doc, BIZ), null, 'the card left the filtered list');
  assert.deepStrictEqual([box.getAttribute('role'), box.getAttribute('aria-live')], ['status', 'polite']);
  assert.strictEqual(t.querySelector('.toast-t').textContent, '#7 moved to Open \u00b7 hidden by the current filter');
  assert.deepStrictEqual([...t.querySelectorAll('button')].map((b) => b.getAttribute('aria-label') || b.textContent), ['View card', 'Clear filter', 'Dismiss']);
  //when
  t.querySelector('.toast-clear').click();
  await wait(20);
  //then
  assert.strictEqual(w.typeF, '', 'Clear filter drops the tag filter');
  assert.ok(article(doc, BIZ), 'and the card shows again');
  assert.strictEqual(box.querySelector('.toast'), null, 'the toast goes with it');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a change made here that hides the card from the filter shows a polite toast with View card and Clear filter');
}

async function aChangeThatKeepsTheCardVisibleShowsNoToast() {
  //given
  let tagged = false;
  const {w, doc, errors} = await boot([card(PLAIN, {num: 8})], {
    reply: () => { tagged = true; return {rev: 2, tags: [{id: 'product', by: 'op@example.com', at: iso(0)}], tagsOff: [], business: null}; },
    next: () => [card(PLAIN, {num: 8, tags: tagged ? [{id: 'product', by: 'op@example.com', at: iso(0)}] : []})]});
  expand(article(doc, PLAIN));
  //when
  article(doc, PLAIN).querySelector('.biz-tb[data-tag="product"]').click();
  await until(() => article(doc, PLAIN).querySelector('.biz-tag'));
  await wait(50);
  //then
  assert.ok(article(doc, PLAIN), 'the card stays in the list');
  assert.strictEqual(doc.querySelector('#toasts .toast'), null, 'so no toast');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a change that keeps the card in view shows no toast');
}

async function toastsStackAtMostThreeAndPauseWhileHovered() {
  //given
  const {w, doc, errors} = await boot([card(PLAIN, {num: 8}), card(TECH, {num: 9})]);
  w.TOAST_MS = 60;
  //when
  [PLAIN, TECH, PLAIN, TECH].forEach((k) => w.hiddenToast(k));
  const stacked = doc.querySelectorAll('#toasts .toast').length;
  const kept = doc.querySelector('#toasts .toast');
  kept.dispatchEvent(new w.Event('mouseenter'));
  await wait(150);
  const paused = doc.querySelectorAll('#toasts .toast').length;
  kept.dispatchEvent(new w.Event('mouseleave'));
  await wait(150);
  //then
  assert.strictEqual(stacked, 3, 'at most three at once');
  assert.strictEqual(paused, 1, 'the hovered one waits, the others went after their time');
  assert.strictEqual(doc.querySelectorAll('#toasts .toast').length, 0, 'and it goes once the pointer leaves');
  //when
  w.hiddenToast(PLAIN);
  doc.querySelector('#toasts .toast-x').click();
  //then
  assert.strictEqual(doc.querySelectorAll('#toasts .toast').length, 0, 'Dismiss closes it at once');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS toasts stack at most three, pause while hovered and close on Dismiss');
}

async function clearFilterAlsoDropsTheSearchSoTheCardShowsAgain() {
  //given
  const {w, doc, errors} = await boot([card(BIZ, {num: 7}), card(PLAIN, {num: 8})]);
  const box = doc.getElementById('card-search');
  box.value = 'shop-web';
  box.dispatchEvent(new w.Event('input'));
  await wait(20);
  const hidden = article(doc, BIZ);
  //when
  w.hiddenToast(BIZ);
  doc.querySelector('#toasts .toast-clear').click();
  await wait(20);
  //then
  assert.strictEqual(hidden, null, 'the search hid the card');
  assert.ok(article(doc, BIZ), 'Clear filter shows it again');
  assert.deepStrictEqual([w.searchQ, box.value, w.typeF], ['', '', '']);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS Clear filter also drops the search, so the card shows again');
}

module.exports = {boot, BIZ, PLAIN, card, iso, wait, until, article, expand, TAGS};

if (require.main === module) {
  (async () => {
    for (const t of [theBusinessNoteIsItsOwnRowNamedForTheTeam, taggingABusinessTeamShowsTheButtonWithoutARepaint, aLongCodeRowStacksTheExplanationUnderTheLink,
      longUnbrokenTextWrapsInsideTheMiddleColumn, aRefiredCardLeadsWithReopenedAndNamesTheOldEventOnce, aRefiredCardKeepsItsCapitalInAnotherLanguage,
      aChangeThatHidesTheCardShowsAToastWithViewAndClear, aChangeThatKeepsTheCardVisibleShowsNoToast, toastsStackAtMostThreeAndPauseWhileHovered, clearFilterAlsoDropsTheSearchSoTheCardShowsAgain]) {
      try { await t(); } catch (e) { console.error('FAIL ' + t.name + ': ' + e.message.split('\n').slice(0, 8).join(' | ')); process.exitCode = 1; }
    }
    process.exit(process.exitCode || 0);
  })();
}
