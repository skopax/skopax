const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const TYPO = 'page-typos|shop.example.com/products';
const LOG = 'log|checkout-service|ERROR|boom';
const PLAIN = 'log|shop-web|ERROR|other';
const FIX = 'log|payments-api|ERROR|npe';
const SENTENCE = 'example.com/orders: spelling mistake, "Carte" should be "Card".';

function state(more) {
  const card = (key, sev, extra) => Object.assign({key, sev, status: 'open', svc: key.split('|')[1], label: 'x', src: 'logs', now: 1, firstSeen: iso(30), lastSeen: iso(1)}, extra || {});
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(TYPO, 'warn', {src: 'pagescan', sig: 'page-typos', url: 'https://shop.example.com/products', tags: [{id: 'seo', by: 'rule', at: iso(20)}], business: {text: SENTENCE, by: 'rule', at: iso(20)}}),
      card(LOG, 'warn', {sig: 'log', tags: [{id: 'bizdev', by: 'pod', at: iso(10)}]}), card(PLAIN, 'warn', {sig: 'log'})].concat((more || []).map((m) => card(m.key, 'warn', m))),
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(more, routes, tags) {
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state(more)));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=anoms', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, opts) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (opts && (opts.method === 'POST' || opts.method === 'PUT')) {
          const body = opts.body ? JSON.parse(opts.body) : null;
          calls.push([opts.method, p, body]);
          if (p === '/api/tags') return ok({rev: 2, tags: body.on ? [{id: body.tag, by: 'dora.lane@example.com', at: iso(0)}] : [], tagsOff: body.on ? [] : [body.tag], business: null});
          if (p === '/api/business') return ok({rev: 2, tags: [], tagsOff: [], business: body.text.trim() ? {text: body.text.trim(), why: body.why || undefined, by: 'dora.lane@example.com', at: iso(0)} : null});
          if (p === '/api/routes/send') return ok({ok: true, sent: [body.route]});
          return ok({});
        }
        if (p.startsWith('/api/me') && tags === false) return new Promise(() => {});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'dora.lane@example.com', operator: true,
          routes: [{id: 'teams/seo', name: 'seo', tags: ['seo'], tagged: true, business: true, plugin: 'Microsoft Teams'}, {id: 'teams/tech', name: 'tech', tags: [], tagged: false, business: false, plugin: 'Microsoft Teams'}].concat(routes || []),
          tags: tags || [{id: 'bizdev', label: 'Business development'}, {id: 'marketing', label: 'Marketing'}, {id: 'seo', label: 'SEO'}, {id: 'product', label: 'Product'}, {id: 'developer', label: 'Developer'}, {id: 'data', label: 'Data'}, {id: 'infra', label: 'Infrastructure'}, {id: 'analyst', label: 'Analyst'}]});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/nodes')) return ok({nodes: []});
        if (p.startsWith('/api/requests')) return ok([]);
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors, calls};
}

const cardOf = (doc, k) => doc.querySelector('#anoms article.inc[data-key="' + k + '"]');
const shown = (doc) => [...doc.querySelectorAll('#anoms article.inc')].map((c) => c.dataset.key);

async function cardsShowTheirBusinessTagsAndTheTagRowFiltersThem() {
  //given
  const {w, doc, errors} = await boot();
  const row = doc.querySelector('#fb-m-tags');
  const chips = () => [...row.querySelectorAll('.fb-i')].map((b) => b.querySelector('.lbl').textContent + ' ' + b.querySelector('.count').textContent);

  //then
  assert.deepStrictEqual([...cardOf(doc, TYPO).querySelectorAll('.tech-fold .biz-tag')].map((t) => t.textContent), ['SEO']);
  assert.ok(cardOf(doc, TYPO).querySelector('.biz-tag').title.includes('tagged by rule') && cardOf(doc, TYPO).querySelector('.biz-tag').title.includes(SENTENCE));
  assert.ok(cardOf(doc, LOG).querySelector('.biz-tag').title.includes('No action written yet'));
  assert.strictEqual(cardOf(doc, PLAIN).querySelector('.biz-tag'), null);
  assert.deepStrictEqual(chips(), ['Business development 1', 'Marketing 0', 'SEO 1', 'Product 0', 'Developer 0', 'Data 0', 'Infrastructure 0', 'Analyst 0', 'Untagged 1']);
  assert.strictEqual(doc.getElementById(row.getAttribute('aria-labelledby')).textContent, 'Tags');

  //when
  row.querySelector('[data-type="tag:seo"]').click();
  await wait(20);

  //then
  assert.deepStrictEqual(shown(doc), [TYPO]);
  assert.ok(decodeURIComponent(w.location.hash).endsWith('/tag:seo'), w.location.hash);
  assert.ok(doc.querySelector('.filter-note').textContent.includes('SEO'));

  //when
  doc.querySelector('#bf-lists .fb-i[data-type="tag:seo"]').click();
  await wait(20);

  //then
  assert.deepStrictEqual(shown(doc).sort(), [LOG, PLAIN, TYPO].sort());
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS cards show their business tags with who tagged them and the sentence, and the Tags row filters to one tag and back');
}

async function anOperatorTagsAndWritesTheSentenceAndOnlyTaggedCardsOfferTheBusinessChannel() {
  //given
  const {w, doc, errors, calls} = await boot();
  const teams = (k) => [...cardOf(doc, k).querySelectorAll('.card-more-box button.teams-btn')].map((b) => b.textContent.trim());
  const pressed = (k) => [...cardOf(doc, k).querySelectorAll('.biz-tb')].filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.dataset.tag);

  //then
  assert.deepStrictEqual(teams(TYPO), ['Microsoft Teams \u00b7 seo', 'Microsoft Teams \u00b7 tech']);
  assert.deepStrictEqual(teams(PLAIN), ['Microsoft Teams \u00b7 tech'], 'a business channel shows only on cards with its tag');
  assert.deepStrictEqual(pressed(TYPO), ['seo']);
  assert.strictEqual(cardOf(doc, TYPO).querySelector('.biz-txt').textContent, 'Recommended: ' + SENTENCE);
  assert.ok(cardOf(doc, TYPO).querySelector('.biz-by').textContent.startsWith('rule'));
  assert.strictEqual(cardOf(doc, LOG).querySelector('.biz-txt').textContent, 'No action written yet: nothing goes to Teams until one is written.');
  assert.strictEqual(cardOf(doc, PLAIN).querySelector('.biz-txt').textContent, 'Tag a business team to write a note');

  //when
  cardOf(doc, PLAIN).querySelector('.biz-tb[data-tag="product"]').click();
  await wait(30);
  const box = cardOf(doc, PLAIN).querySelector('.biz');
  box.querySelector('.biz-edit').click();
  box.querySelector('textarea').value = '  The checkout form gets stuck at delivery selection.  ';
  box.querySelector('.biz-why-in').value = 'Orders are left unfinished';
  box.querySelector('.biz-save').click();
  await wait(30);

  //then
  assert.deepStrictEqual(calls.slice(0, 2), [['POST', '/api/tags', {key: PLAIN, tag: 'product', on: true}], ['PUT', '/api/business', {key: PLAIN, text: '  The checkout form gets stuck at delivery selection.  ', why: 'Orders are left unfinished', kind: 'action'}]]);
  const after = cardOf(doc, PLAIN).querySelector('.biz');
  assert.strictEqual(after.querySelector('.biz-txt').textContent, 'Recommended: The checkout form gets stuck at delivery selection.');
  assert.ok(after.querySelector('.biz-why').compareDocumentPosition(after.querySelector('.biz-txt')) & w.Node.DOCUMENT_POSITION_FOLLOWING, 'the why shows first');
  assert.strictEqual(after.querySelector('.biz-why').textContent, 'Why: Orders are left unfinished');
  assert.ok(!after.querySelector('.biz-why').hidden);
  assert.ok(!after.querySelector('.biz-why').classList.contains('muted'), 'the why is not faded: the team reads it first');
  assert.ok(after.querySelector('.biz-form').hidden);

  //when
  cardOf(doc, TYPO).querySelector('.biz-tb[data-tag="seo"]').click();
  await wait(30);

  //then
  assert.deepStrictEqual(calls[2], ['POST', '/api/tags', {key: TYPO, tag: 'seo', on: false}]);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS an operator tags a card and writes its sentence, removes a tag, and a business channel button shows only on cards with its tag');
}

async function theDeveloperTagShowsOnTheCardNeedsNoSentenceAndItsRouteShowsOnlyOnTaggedCards() {
  //given
  const {w, doc, errors, calls} = await boot([{key: FIX, sig: 'log', tags: [{id: 'developer', by: 'pod', at: iso(5)}]}],
    [{id: 'teams/eng', name: 'eng', tags: ['developer'], tagged: true, business: false, plugin: 'Microsoft Teams'}]);
  const teams = (k) => [...cardOf(doc, k).querySelectorAll('.card-more-box button.teams-btn')].map((b) => b.textContent.trim());
  const pill = cardOf(doc, FIX).querySelector('.tech-fold .biz-tag');

  //then
  assert.strictEqual(pill.textContent, 'Developer');
  assert.ok(pill.classList.contains('tech-tag') && pill.title.includes('engineering has to change the code or config of a service') && !pill.title.includes('No action written yet'), pill.title);
  assert.strictEqual(cardOf(doc, FIX).querySelector('.biz-txt').textContent, 'Tag a business team to write a note', 'the Developer tag asks for no sentence');
  assert.deepStrictEqual(teams(FIX), ['Microsoft Teams \u00b7 eng', 'Microsoft Teams \u00b7 tech']);
  assert.deepStrictEqual(teams(PLAIN), ['Microsoft Teams \u00b7 tech'], 'the Developer channel shows only on cards with the tag');
  assert.strictEqual(doc.querySelector('#bf-lists .fb-i[data-type="tag:developer"] .count').textContent, '1');
  assert.ok(doc.querySelector('#bf-lists .fb-i[data-type="tag:developer"]').title.includes('engineering has to change the code or config of a service'));
  assert.ok(cardOf(doc, PLAIN).querySelector('.biz-tb[data-tag="infra"]').title.includes('infrastructure has to change') && cardOf(doc, PLAIN).querySelector('.biz-tb[data-tag="infra"]').title.includes('tag:infra'));
  assert.ok(cardOf(doc, PLAIN).querySelector('.biz-tb[data-tag="developer"]').title.includes('tag:developer'));

  //when
  cardOf(doc, PLAIN).querySelector('.biz-tb[data-tag="developer"]').click();
  await wait(30);
  doc.querySelector('#bf-lists .fb-i[data-type="tag:developer"]').click();
  await wait(20);

  //then
  assert.deepStrictEqual(calls[0], ['POST', '/api/tags', {key: PLAIN, tag: 'developer', on: true}]);
  assert.deepStrictEqual(shown(doc).sort(), [FIX, PLAIN].sort());
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the Developer tag shows on the card and in the Tags row, needs no sentence, an operator sets it, and a tag:developer channel shows only on tagged cards');
}

async function sendToRouteListsTheRoutesAndPosts() {
  //given
  const EVIL = '<img src=x onerror="window.__evil=1">';
  const {w, doc, errors, calls} = await boot(null, [{id: 'teams/biz', name: 'biz', tags: ['bizdev'], tagged: true, business: true, plugin: 'Microsoft Teams'}, {id: 'teams/evil', name: EVIL, tags: [], tagged: false, business: false, plugin: EVIL},
    {id: 'teams/mixed', name: 'mixed', tags: ['bizdev'], tagged: false, business: false, plugin: 'Microsoft Teams'}]);
  const buttons = (k) => [...cardOf(doc, k).querySelectorAll('.card-more-box button.teams-btn')];

  //then
  assert.deepStrictEqual(buttons(PLAIN).map((b) => b.dataset.route), ['teams/tech', 'teams/evil', 'teams/mixed'], 'a route that takes card keys shows on every card, also when it names tags');
  assert.deepStrictEqual(buttons(TYPO).map((b) => b.dataset.route), ['teams/seo', 'teams/tech', 'teams/evil', 'teams/mixed'], 'the route that takes the card tag comes first');
  assert.ok(buttons(LOG)[1].title.startsWith('Post this card to Microsoft Teams \u00b7 tech'), buttons(LOG)[1].title);
  assert.ok(!buttons(LOG).filter((b) => b.dataset.route === 'teams/mixed')[0].title.includes('business sentence'), 'a route with card keys never asks for the sentence');
  assert.strictEqual(buttons(PLAIN)[1].textContent.trim(), EVIL + ' \u00b7 ' + EVIL);
  assert.strictEqual(doc.querySelectorAll('.card-more-box img').length, 0, 'a route name is text');
  assert.ok(buttons(TYPO)[0].title.startsWith('Post the business sentence to Microsoft Teams \u00b7 seo'), buttons(TYPO)[0].title);
  assert.ok(buttons(LOG)[0].title.startsWith('Write the business sentence first'), buttons(LOG)[0].title);

  //when
  const tech = buttons(PLAIN)[0];
  tech.click();
  await wait(10);
  const asked = tech.textContent.trim();
  tech.click();
  await wait(30);

  //then
  assert.strictEqual(asked, 'Post to Microsoft Teams \u00b7 tech?');
  assert.deepStrictEqual(calls, [['POST', '/api/routes/send', {key: PLAIN, route: 'teams/tech'}]]);
  assert.strictEqual(tech.textContent.trim(), 'Sent to Microsoft Teams \u00b7 tech');
  assert.deepStrictEqual([asked, tech.getAttribute('aria-label')], ['Post to Microsoft Teams \u00b7 tech?', 'Sent to Microsoft Teams \u00b7 tech'], 'a screen reader hears the question and the result, not the old name');
  assert.strictEqual(w.__evil, undefined);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the Notify row lists the routes that take the card, keeps a route name as text, and posts the card to the route chosen');
}

async function theTagNamesComeFromTheServerOnly() {
  //given
  const core = [{id: 'developer', label: 'Developer'}, {id: 'data', label: 'Data'}, {id: 'infra', label: 'Infrastructure'}, {id: 'analyst', label: 'Analyst'}];
  const odd = '<b>x</b>';
  const plain = await boot([{key: 'log|odd-service|ERROR|x', sig: 'log', tags: [{id: odd, by: 'rule', at: iso(5)}]}], null, core);
  const plugged = await boot(null, null, core.concat([{id: 'release-gate', label: 'Release gate'}]));
  const silent = await boot(null, null, false);
  const labels = (t) => [...t.doc.querySelectorAll('#fb-m-tags .fb-i .lbl')].map((l) => l.textContent);

  //then
  assert.deepStrictEqual(labels(plain), ['Developer', 'Data', 'Infrastructure', 'Analyst', 'Untagged']);
  assert.ok(!/Business development|Marketing|SEO|Product/.test(plain.doc.getElementById('bf-lists').textContent), 'no tag the server did not send');
  assert.deepStrictEqual([...cardOf(plain.doc, 'log|odd-service|ERROR|x').querySelectorAll('.biz-tag')].map((t) => t.textContent), [odd]);
  assert.strictEqual(cardOf(plain.doc, 'log|odd-service|ERROR|x').querySelectorAll('.biz-tag b').length, 0, 'an unknown tag id stays text');
  assert.deepStrictEqual(labels(plugged), ['Developer', 'Data', 'Infrastructure', 'Analyst', 'Release gate', 'Untagged']);
  assert.deepStrictEqual(labels(silent), [], 'before the server answers the page invents no tags');
  assert.deepStrictEqual(plain.errors.concat(plugged.errors), []);
  plain.w.close();
  plugged.w.close();
  silent.w.close();
  console.log('PASS the Tags list and card tags show only the tags the server sends, with its labels, and an unknown id as escaped text');
}

async function anUntaggedEntryEndsTheTagRowAndFiltersCardsWithNoTag() {
  //given
  const {w, doc, errors} = await boot();
  const btn = () => doc.querySelector('#bf-lists .fb-i[data-type="tag:-"]');

  //then
  assert.strictEqual(btn().querySelector('.lbl').textContent, 'Untagged');
  assert.strictEqual(btn().querySelector('.count').textContent, '1');
  assert.strictEqual(btn().parentNode.lastElementChild, btn(), 'the last entry of the Tags list');
  assert.strictEqual(btn().getAttribute('aria-checked'), 'false');

  //when
  btn().click();
  await wait(20);

  //then
  assert.deepStrictEqual(shown(doc), [PLAIN]);
  assert.strictEqual(btn().getAttribute('aria-checked'), 'true');
  assert.ok(decodeURIComponent(w.location.hash).endsWith('/tag:-'), w.location.hash);
  assert.ok(doc.querySelector('.filter-note').textContent.includes('Untagged'));

  //when
  btn().click();
  await wait(20);

  //then
  assert.deepStrictEqual(shown(doc).sort(), [LOG, PLAIN, TYPO].sort());
  assert.strictEqual(btn().getAttribute('aria-checked'), 'false');

  //when
  w.location.hash = '#t=anoms/all/tag:-';
  await wait(60);

  //then
  assert.deepStrictEqual(shown(doc), [PLAIN], 'a link with the filter opens it');
  assert.strictEqual(btn().getAttribute('aria-checked'), 'true');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS an Untagged entry ends the Tags list, counts and filters the cards with no tag, toggles off, and round-trips through the link');
}

(async () => {
  await anUntaggedEntryEndsTheTagRowAndFiltersCardsWithNoTag();
  await cardsShowTheirBusinessTagsAndTheTagRowFiltersThem();
  await theDeveloperTagShowsOnTheCardNeedsNoSentenceAndItsRouteShowsOnlyOnTaggedCards();
  await anOperatorTagsAndWritesTheSentenceAndOnlyTaggedCardsOfferTheBusinessChannel();
  await theTagNamesComeFromTheServerOnly();
  await sendToRouteListsTheRoutesAndPosts();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
