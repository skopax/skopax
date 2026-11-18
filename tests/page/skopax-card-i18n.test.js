const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');
const {short} = require('./skopax-zone');

const TEMPLATE = process.argv[2];
const CARD_PAGE = path.join(path.dirname(TEMPLATE), 'card.html');
const RUNTIME = path.join(path.dirname(TEMPLATE), '..', 'runtime.py');
const PAGE_PACK = /^PAGE_PACK = '(.+)'$/m.exec(fs.readFileSync(RUNTIME, 'utf8'))[1];
const SLOT = '<script type="text/plain" id="plugin-scripts"></script>';
const AT = '2026-11-09T16:32:00Z';
const DOT = ' \u00b7 ';
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const GERMAN = {'Open on the board': 'Auf dem Board oeffnen', 'Technical details': 'Technische Details', 'Chat about this card': 'Ueber diese Karte chatten', 'Copy link': 'Link kopieren',
  'Warning': 'Warnung', 'Open': 'Offen', 'Recommended': 'Empfohlen', 'Suspects': 'Verdaechtige', 'Recommended step': 'Empfohlener Schritt', 'from the page': 'von der Seite', 'Fixed': 'Behoben',
  '@months': {'Nov': 'Nov.'}, '@rules': [['^first seen (.+)$', 'zuerst gesehen $1'], ['^Updated (.+)$', 'Aktualisiert $1']]};
const CARD = {num: 7, key: 'log|checkout-service|ERROR|boom', svc: 'checkout-service', label: 'ERROR lines', sev: 'warn', status: 'open', firstSeen: AT, live: true,
  business: {text: 'Open', why: 'Open'}, what: 'Open', suspects: ['Open'], recommend: 'Open', note: {text: 'Open', at: AT}, owner: 'Open', now: 3, d1: 0, d7: 1,
  last: {type: 'dive', status: 'failed', requestedBy: 'Open@example.com', requestedAt: AT, result: 'Open'},
  merged: [{num: 8, key: 'log|cart-service|ERROR|x', svc: 'cart-service', label: 'Open', business: {text: 'Open'}}, {num: 9, key: 'log|cart-service|ERROR|y', svc: 'cart-service', label: 'Open', what: 'Open'}]};
const HANDLED = {num: 11, key: 'log|cart-service|ERROR|z', svc: 'cart-service', label: 'ERROR lines', sev: 'warn', status: 'open', firstSeen: AT, live: true, triage: 'handled',
  resolution: {kind: 'fixed', by: 'Open@example.com', via: 'page'}};
const PROGRESS = {num: 12, key: 'log|cart-service|ERROR|p', svc: 'cart-service', label: 'ERROR lines', sev: 'warn', status: 'open', firstSeen: AT, live: true, triage: 'progress', ticket: 'OPS-12',
  owner: 'carol@example.com', assigned: {by: 'Nov@example.com', at: AT}};
const CARDS = {7: CARD, 11: HANDLED, 12: PROGRESS};

function served(lang, pack) {
  const args = [JSON.stringify(lang), JSON.stringify(pack).replace(/</g, '\\u003c')];
  return '<script>' + PAGE_PACK.replace(/%s/g, () => args.shift()) + '</script>';
}

async function until(check, ms) {
  const end = Date.now() + (ms || 3000);
  while (Date.now() < end && !check()) await wait(5);
}

async function boot(pack, num, me) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(CARD_PAGE).replace(SLOT, () => (pack ? served('de', pack) : SLOT));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/c/' + (num || 7), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.document.cookie = 'skopax_lang=de; path=/';
      w.fetch = async (p) => {
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body))});
        const m = /^\/api\/cards\/(\d+)$/.exec(String(p));
        if (String(p) === '/api/me') return ok(Object.assign({loginEnabled: true, user: null, operator: false, tags: []}, me || {}));
        if (m && CARDS[m[1]]) return ok(CARDS[m[1]]);
        if (String(p).startsWith('/api/jira/search')) return ok([{key: 'OPS-1', summary: 'Open', status: 'Open'}]);
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  const doc = dom.window.document;
  await until(() => doc.querySelector('#card h1') && doc.getElementById('upd').textContent && (!pack || doc.getElementById('upd').textContent.startsWith('Aktualisiert')));
  return {w: dom.window, doc, errors};
}

function labels(doc) {
  return {
    board: doc.getElementById('board').textContent,
    chat: doc.getElementById('chat-about').textContent,
    copy: doc.getElementById('copy-link').textContent,
    tech: doc.querySelector('details.tech > summary').textContent,
    state: doc.getElementById('state').textContent,
    sev: doc.querySelector('.head .pill.warn').textContent,
    todo: doc.querySelector('.todo h2').textContent,
    suspects: [...doc.querySelectorAll('.tech-body h3')].slice(-2).map((h) => h.textContent),
    status: doc.querySelector('.status').textContent,
    updated: doc.getElementById('upd').textContent.split(' ')[0],
  };
}

function data(doc) {
  return {
    business: doc.querySelector('.todo p.rec').textContent,
    why: doc.querySelector('.todo .why').textContent,
    title: doc.querySelector('h1').textContent,
    tech: [...doc.querySelectorAll('.tech-body p:not(.nums):not(.key)')].map((p) => p.textContent),
    suspect: doc.querySelector('.tech-body li').textContent,
    key: doc.querySelector('.tech-body .key').textContent,
    owner: doc.querySelector('.owner b').textContent,
    merged: [...doc.querySelectorAll('.merged .m1 b')].map((b) => b.textContent).concat(doc.querySelector('.merged .m-todo span').textContent, doc.querySelector('.merged .m-what').textContent),
    last: doc.querySelector('.act-last').textContent.split(DOT).slice(-2),
  };
}

async function cardPageIsTranslatedWithThePacks() {
  //given
  const german = await boot(GERMAN);
  const english = await boot(null);
  const translated = {board: 'Auf dem Board oeffnen', chat: 'Ueber diese Karte chatten', copy: 'Link kopieren', tech: 'Technische Details', state: 'Offen', sev: 'Warnung', todo: 'Empfohlen',
    suspects: ['Verdaechtige', 'Empfohlener Schritt'], status: 'zuerst gesehen ' + short(AT).replace('Nov', 'Nov.'), updated: 'Aktualisiert'};

  //when
  const first = labels(german.doc);
  await german.w.__cardLoad(true);
  await until(() => german.doc.getElementById('board').textContent === translated.board);
  const again = labels(german.doc);

  //then
  assert.deepStrictEqual(first, translated);
  assert.deepStrictEqual(again, translated, 'a re-render is translated again');
  assert.strictEqual(german.doc.documentElement.lang, 'de');
  assert.deepStrictEqual(labels(english.doc), {board: 'Open on the board', chat: 'Chat about this card', copy: 'Copy link', tech: 'Technical details', state: 'Open', sev: 'Warning', todo: 'Recommended',
    suspects: ['Suspects', 'Recommended step'], status: 'first seen ' + short(AT), updated: 'Updated'});
  assert.strictEqual(english.doc.documentElement.lang, 'en');
  const facts = {business: 'Open', why: 'Why: Open', title: 'checkout-service / ERROR lines', tech: ['Open', 'Open', 'Open'], suspect: 'Open', key: CARD.key, owner: 'Open',
    merged: ['cart-service / Open', 'cart-service / Open', 'Open', 'Open'], last: ['Open', 'Open']};
  assert.deepStrictEqual(data(german.doc), facts, 'card data that equals a pack key stays as written');
  assert.deepStrictEqual(data(english.doc), facts);
  assert.deepStrictEqual(german.errors.concat(english.errors), []);
  german.w.close(); english.w.close();
  console.log('PASS the card page reads its labels and short dates in the pack language with a pack loaded, English without one, and never translates the card data');
}

async function namesAndTicketsOnTheCardPageStayAsWritten() {
  //given
  const handled = await boot(GERMAN, 11);
  const operator = await boot(GERMAN, 7, {user: 'alice@example.com', operator: true, jira: true});
  const input = operator.doc.getElementById('take-ticket');

  //when
  input.value = 'Op';
  input.dispatchEvent(new operator.w.Event('input'));
  await until(() => operator.doc.querySelector('#take-ticket-suggest li[data-i]'));

  //then
  assert.strictEqual(handled.doc.getElementById('state').textContent, 'Behoben');
  assert.strictEqual(handled.doc.querySelector('.status').textContent, 'von der Seite' + DOT + 'Open' + DOT + 'zuerst gesehen ' + short(AT).replace('Nov', 'Nov.'), 'the resolver name stays');
  assert.strictEqual(operator.doc.querySelector('#take-ticket-suggest .tsg-s').textContent, 'Open', 'a ticket summary stays');
  assert.strictEqual(operator.doc.querySelector('#take-ticket-suggest .tsg-m').textContent, 'Open', 'a ticket status stays');
  assert.deepStrictEqual(handled.errors.concat(operator.errors), []);
  handled.w.close(); operator.w.close();
  console.log('PASS on the card page the resolver name and the Jira suggestions are data and stay as written while the labels around them translate');
}

async function theTicketKeyAndTheAssignerNameStayAsWrittenWhileTheirLabelsTranslate() {
  //given
  const pack = Object.assign({}, GERMAN, {'OPS-{0}': 'Vorgang {0}', 'Assignee:': 'Zugewiesen:', 'Assigned by:': 'Zugewiesen von:', '{0} {M} {1}': '{0}. {M} {1}',
    '@rules': GERMAN['@rules'].concat([['^Assigned by: (.+)$', 'Zugewiesen von: $1']])});
  const german = await boot(pack, 12);
  const english = await boot(null, 12);

  //when
  const seen = (doc) => ({status: doc.querySelector('.status').textContent, owner: doc.querySelector('.owner').textContent, tip: doc.querySelector('.owner').title});

  //then
  assert.deepStrictEqual(seen(german.doc), {status: 'OPS-12' + DOT + 'zuerst gesehen ' + short(AT).replace('Nov', 'Nov.'), owner: 'Zugewiesen: carol',
    tip: 'Zugewiesen von: Nov' + DOT + short(AT).replace(/^(\d+) Nov/, '$1. Nov.')}, 'the ticket key and the name keep their spelling, the labels and the date around them translate');
  assert.deepStrictEqual(seen(english.doc), {status: 'OPS-12' + DOT + 'first seen ' + short(AT), owner: 'Assignee: carol', tip: 'Assigned by: Nov' + DOT + short(AT)});
  assert.deepStrictEqual(german.errors.concat(english.errors), []);
  german.w.close(); english.w.close();
  console.log('PASS the ticket key and the assigner name on the card page stay as written while the labels around them translate');
}

(async () => {
  await cardPageIsTranslatedWithThePacks();
  await namesAndTicketsOnTheCardPageStayAsWritten();
  await theTicketKeyAndTheAssignerNameStayAsWrittenWhileTheirLabelsTranslate();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
