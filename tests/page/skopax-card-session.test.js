const pageHtml = require('./skopax-page');
const assert = require('assert');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const CARD_PAGE = path.join(path.dirname(TEMPLATE), 'card.html');
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const AT = '2026-11-09T16:32:00Z';
const CARD = {num: 14, key: 'log|checkout-service|ERROR|x', svc: 'checkout-service', label: 'ERROR lines', sev: 'warn', status: 'open', firstSeen: AT, live: true, last: null};

async function boot(answer, after) {
  const errors = [], posts = [];
  let who = {user: 'alice@example.com', operator: true, jira: true};
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(CARD_PAGE), {
    url: 'https://skopax.example.com/c/14', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.fetch = async (p, opts) => {
        if (String(p) === '/api/me') return {ok: true, status: 200, json: async () => Object.assign({loginEnabled: true, user: null, operator: false}, who)};
        if (opts && opts.method === 'POST') {
          posts.push(JSON.parse(opts.body));
          if (after) who = after;
          return {ok: false, status: answer, json: async () => ({detail: 'no'})};
        }
        return {ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(CARD))};
      };
    },
  });
  await wait(100);
  return {w: dom.window, doc: dom.window.document, errors, posts};
}

async function aTakeAfterTheSessionEndedShowsTheLoginLinkInsteadOfTheButtons() {
  //given
  const {w, doc, errors, posts} = await boot(401, {user: null, operator: false});
  doc.getElementById('act-take').click();
  doc.getElementById('take-ticket').value = 'OPS-12';

  //when
  doc.getElementById('take-go').click();
  await wait(50);

  //then
  assert.strictEqual(posts.length, 1);
  assert.strictEqual(doc.getElementById('act-dive'), null, 'the operator buttons are gone');
  assert.ok(doc.querySelector('.act-note a[href="/login?next=%2Fc%2F14"]'), 'the login link back to this card replaces them');
  assert.strictEqual(doc.querySelector('.act-err'), null, 'no try-again note');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a Take after the session ended (401) swaps the card actions for the login link back to the card, with no try-again note');
}

async function aTakeWithoutTheRoleAnymoreSaysWhichRoleIsMissing() {
  //given
  const {w, doc, errors} = await boot(403, {user: 'alice@example.com', operator: false});

  //when
  doc.getElementById('act-mute').click();
  await wait(50);

  //then
  assert.strictEqual(doc.getElementById('act-mute'), null);
  assert.strictEqual(doc.querySelector('.act-note').textContent, 'Actions need the operator role.');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a refused action (403) swaps the card actions for the missing-role note');
}

async function aFailingActionSaysTryAgainOnceHoweverOftenItIsClicked() {
  //given
  const {w, doc, errors} = await boot(500);

  //when
  doc.getElementById('act-mute').click();
  await wait(30);
  doc.getElementById('act-mute').click();
  await wait(30);

  //then
  assert.strictEqual(doc.querySelectorAll('.act-err').length, 1, 'one try-again note, not one per click');
  assert.ok(doc.getElementById('act-mute') && !doc.getElementById('act-mute').disabled, 'the button can be tried again');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a failing action keeps its button and shows one try-again note however often it is clicked');
}

(async () => {
  await aTakeAfterTheSessionEndedShowsTheLoginLinkInsteadOfTheButtons();
  await aTakeWithoutTheRoleAnymoreSaysWhichRoleIsMissing();
  await aFailingActionSaysTryAgainOnceHoweverOftenItIsClicked();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
