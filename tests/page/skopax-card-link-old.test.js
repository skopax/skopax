const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString().replace(/\.\d{3}Z$/, 'Z');
const ON_BOARD = 'log|checkout-service|ERROR|boom';
const OLD = {num: 412, key: 'k412', live: false, label: 'Payments slow', status: 'cleared'};
const HANDLED = {num: 413, key: 'k413', live: true, label: 'Refunds slow', svc: 'payments-api', sev: 'warn', status: 'cleared', triage: 'handled', firstSeen: iso(9000), lastSeen: iso(8000)};

function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [{key: ON_BOARD, num: 11, sev: 'warn', status: 'open', svc: 'checkout-service', label: 'ERROR lines', src: 'logs', sig: 'log', now: 1, firstSeen: iso(30), lastSeen: iso(5), what: 'first text'}],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(hash, opts) {
  opts = opts || {};
  const errors = [], posted = [], opened = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE, {view: opts.view || 'board'}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state())), {
    url: 'https://skopax.example.com/' + (hash || '#t=anoms'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.open = (...a) => { opened.push(a); return null; };
      w.fetch = async (path, init) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p === '/api/cards/412' && opts.slow) return new Promise((res) => { w.__answer = () => res(ok(OLD)); });
        if (p === '/api/cards/412') return ok(OLD);
        if (p === '/api/cards/416') throw new Error('network down');
        if (p === '/api/cards/413') return ok(HANDLED);
        if (p === '/api/cards/414') return ok({num: 414, key: 'log|gone|ERROR|y', live: false});
        if (p === '/api/cards/415') return ok(Object.assign({}, HANDLED, {num: 415, key: 'k415', triage: undefined, status: 'open'}));
        if (p === '/api/cards/100') return ok({num: 100, key: 'log|svc-0|ERROR|boom', live: false, svc: 'svc-0', label: 'ERROR pattern', sev: 'crit', status: 'cleared', outcome: 'handled'});
        if (p.startsWith('/api/history/cards')) {
          return ok({from: iso(10080), to: iso(0), page: 0, size: 50, total: 1, outcomes: {},
            items: [{key: 'log|svc-0|ERROR|boom', num: 100, svc: 'svc-0', label: 'ERROR pattern', sev: 'crit', firstSeen: iso(3000), lastSeen: iso(2000), clearedAt: iso(2000), outcome: 'handled'}]});
        }
        if (p.startsWith('/api/me')) return ok(opts.me || {loginEnabled: true, user: 'op@example.com', operator: true, teams: [], tags: []});
        if (p.startsWith('/api/actions')) {
          if (init && init.method === 'POST') { posted.push(JSON.parse(init.body)); return opts.post ? opts.post() : ok({id: 'x1', status: 'done'}); }
          return ok([]);
        }
        if (p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/state')) return ok(state());
        return {ok: false, status: 404, json: async () => ({detail: 'no card'})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors, posted, opened};
}

const buttons = (root) => [...root.querySelectorAll('button, a')].map((b) => b.textContent.trim());

async function anOldClosedCardOpensInThePanel() {
  //given
  const {w, doc, errors} = await boot('#t=anoms');

  //when
  w.location.hash = '#c=412';
  await wait(150);
  const drawer = doc.querySelector('.card-drawer');

  //then
  assert.ok(drawer, 'the panel opens for a card that is not on the board');
  assert.ok(drawer.textContent.includes('#412'), drawer.textContent);
  assert.ok(drawer.textContent.includes('Payments slow'));
  assert.strictEqual(doc.querySelector('#panel-anoms .panel-note'), null, 'no "no longer on the page" note');
  assert.ok(!/undefined/.test(drawer.textContent), drawer.textContent);
  assert.strictEqual(w.location.hash, '#c=412');

  //when
  w.renderAnoms();
  w.setFilter('all');

  //then
  assert.ok(doc.querySelector('.card-drawer'), 'a repaint of the board keeps the panel open');
  assert.ok(doc.querySelector('.card-drawer').textContent.includes('Payments slow'));

  //when
  doc.querySelector('.card-drawer .cd-close').click();

  //then
  assert.strictEqual(doc.querySelector('.card-drawer'), null);
  assert.ok(w.location.hash.startsWith('#t=anoms'), w.location.hash);
  assert.deepStrictEqual(errors, []);
  w.close();
}

async function aClosedCardHidesActionsThatChangeStatus() {
  //given
  const {w, doc, errors, posted} = await boot('#c=412');

  //when
  const old = buttons(doc.querySelector('.card-drawer'));

  //then
  ['Take it', 'Start progress', 'Resolve', 'Reopen'].forEach((t) => assert.ok(!old.includes(t), t + ' is not offered on a card that is gone: ' + old.join(', ')));
  assert.ok(old.includes('Copy link'), old.join(', '));

  //when
  w.location.hash = '#c=413';
  await wait(150);
  const handled = buttons(doc.querySelector('.card-drawer'));

  //then
  ['Take it', 'Start progress', 'Resolve'].forEach((t) => assert.ok(!handled.includes(t), t + ' is not offered on a closed card: ' + handled.join(', ')));
  assert.ok(handled.includes('Reopen') && handled.includes('Copy link'), handled.join(', '));

  //when
  [...doc.querySelectorAll('.card-drawer button')].find((b) => b.textContent.trim() === 'Reopen').click();
  await wait(100);

  //then
  assert.deepStrictEqual(posted.map((x) => [x.type, x.key]), [['reopen', 'k413']]);
  assert.deepStrictEqual(errors, []);
  w.close();
}

async function aBareCardFromTheApiOpensWithoutUndefined() {
  //given
  const {w, doc, errors} = await boot('#c=414');

  //when
  const drawer = doc.querySelector('.card-drawer');
  const said = [drawer.textContent, drawer.getAttribute('aria-label')].concat([...drawer.querySelectorAll('[title], [aria-label]')].map((e) => (e.getAttribute('title') || '') + ' ' + (e.getAttribute('aria-label') || '')));

  //then
  assert.ok(drawer.textContent.includes('#414'));
  assert.ok(!said.some((t) => /undefined|null|NaN/.test(t)), said.filter((t) => /undefined|null|NaN/.test(t)).join(' | '));
  assert.ok(!buttons(drawer).includes('Reopen'));
  assert.deepStrictEqual(errors, []);
  w.close();
}

async function aLiveOpenCardOffTheBoardOffersNoReopen() {
  //given
  const {w, doc, errors} = await boot('#c=415');

  //then
  assert.ok(doc.querySelector('.card-drawer'), 'a card newer than the loaded page still opens');
  assert.ok(!buttons(doc.querySelector('.card-drawer')).includes('Reopen'), 'nothing to reopen on an open card');
  assert.deepStrictEqual(errors, []);
  w.close();
}

async function aLateAnswerNeverTakesOverThePanel() {
  //given
  const {w, doc, errors} = await boot('#c=412', {slow: true});

  //when
  [...doc.querySelectorAll('#anoms .tile')].find((t) => t.dataset.key === ON_BOARD).click();
  await wait(30);
  const hash = w.location.hash;
  w.__answer();
  await wait(100);

  //then
  assert.strictEqual(doc.querySelector('.card-drawer').dataset.key, ON_BOARD, 'the card picked on the board stays open');
  assert.strictEqual(w.location.hash, hash);
  assert.strictEqual(hash, '#c=11');
  assert.deepStrictEqual(errors, []);
  w.close();
}

async function aLateAnswerAfterCloseOpensNothing() {
  //given
  const {w, doc, errors} = await boot('#c=412', {slow: true});

  //when
  [...doc.querySelectorAll('#anoms .tile')].find((t) => t.dataset.key === ON_BOARD).click();
  await wait(30);
  doc.querySelector('.card-drawer .cd-close').click();
  w.__answer();
  await wait(100);

  //then
  assert.strictEqual(doc.querySelector('.card-drawer'), null);
  assert.ok(w.location.hash.startsWith('#t=anoms'), w.location.hash);
  assert.deepStrictEqual(errors, []);
  w.close();
}

async function aNetworkErrorSaysTheCardCouldNotBeLoaded() {
  //given
  const {w, doc, errors} = await boot('#c=416');

  //then
  assert.strictEqual(doc.querySelector('#panel-anoms .panel-note').textContent, 'The card could not be loaded; reload the page.');
  assert.strictEqual(doc.querySelector('.card-drawer'), null);
  assert.deepStrictEqual(errors, []);
  w.close();
}

async function theClosedCardRowNamesEveryControlAndHidesItsIcons() {
  //given
  const {w, doc, errors} = await boot('#c=413');

  //when
  const row = doc.querySelector('.card-drawer .acts-closed');
  const controls = [...row.querySelectorAll('button, a')];

  //then
  assert.ok(controls.length >= 3, controls.map((c) => c.textContent).join(', '));
  [...row.querySelectorAll('svg')].forEach((g) => assert.strictEqual(g.getAttribute('aria-hidden'), 'true', 'an icon is hidden from screen readers'));
  controls.forEach((c) => assert.ok((c.getAttribute('aria-label') || c.textContent).trim(), 'every control has a name: ' + c.outerHTML));
  assert.deepStrictEqual(errors, []);
  w.close();
}

async function aViewerGetsNoReopen() {
  //given
  const {w, doc, errors} = await boot('#c=413', {me: {loginEnabled: true, user: 'viewer@example.com', operator: false, teams: [], tags: []}});

  //when
  const re = [...doc.querySelectorAll('.card-drawer button')].find((b) => b.textContent.trim() === 'Reopen');

  //then
  assert.ok(!re || re.hidden, 'a viewer does not see Reopen');
  assert.ok(buttons(doc.querySelector('.card-drawer')).includes('Copy link'));
  assert.deepStrictEqual(errors, []);
  w.close();
}

async function aRefusedOrFailedReopenSaysSo() {
  //given
  const failed = await boot('#c=413', {post: () => ({ok: true, status: 201, json: async () => ({id: 'x1', type: 'reopen', status: 'failed', result: 'no anomaly with key k413'})})});
  const out = await boot('#c=413', {post: () => ({ok: false, status: 401, json: async () => ({detail: 'log in first'})})});
  const reopen = (doc) => [...doc.querySelectorAll('.card-drawer button')].find((b) => b.textContent.trim() === 'Reopen');
  const toasts = (doc) => [...doc.querySelectorAll('#fail-toasts .toast-t')].map((t) => t.textContent);

  //when
  reopen(failed.doc).click();
  reopen(out.doc).click();
  await wait(100);

  //then
  assert.deepStrictEqual(toasts(failed.doc), ['Not done: no anomaly with key k413.']);
  assert.deepStrictEqual(toasts(out.doc), ['Not done: you are signed out; sign in and try again.']);
  const after = reopen(out.doc);
  assert.ok(!after || after.hidden || after.getAttribute('aria-disabled') === 'true', 'signed out, Reopen is no longer offered');
  assert.deepStrictEqual(failed.errors.concat(out.errors), []);
  failed.w.close();
  out.w.close();
}

async function anUnknownNumberShowsNotFound() {
  //given
  const {w, doc, errors} = await boot('#c=999');

  //when
  const note = doc.querySelector('#panel-anoms .panel-note');

  //then
  assert.strictEqual(doc.getElementById('panel-anoms').hidden, false, 'the board is shown');
  assert.ok(note, 'a note says so');
  assert.strictEqual(note.textContent, 'Card not found.');
  assert.strictEqual(doc.querySelector('.card-drawer'), null);
  assert.deepStrictEqual(errors, []);
  w.close();
}

async function thePanelHasNoOpenAloneLink() {
  //given
  const {w, doc, errors} = await boot('#c=11');

  //when
  const board = doc.querySelector('.card-drawer');
  const boardText = board && board.textContent;
  w.location.hash = '#c=412';
  await wait(150);

  //then
  assert.ok(boardText && boardText.includes('checkout-service'), 'the board card opens in the panel');
  assert.ok(!boardText.includes('Open alone'));
  assert.ok(!doc.body.textContent.includes('Open alone'));
  assert.strictEqual(doc.querySelector('.card-drawer .cd-link'), null);
  assert.deepStrictEqual(errors, []);
  w.close();
}

async function theClosedListNumberOpensThePanel() {
  //given
  const {w, doc, errors, opened} = await boot('#t=anoms/closed');
  const link = doc.querySelector('#cl-body .cl-num a');

  //when
  link.click();
  await wait(150);

  //then
  assert.strictEqual(link.getAttribute('href'), '#c=100');
  assert.ok(doc.querySelector('.card-drawer'), 'the panel opens over the Closed list');
  assert.ok(doc.querySelector('.card-drawer').textContent.includes('#100'));
  assert.strictEqual(w.location.hash, '#c=100');
  assert.deepStrictEqual(opened, [], 'no new window');
  assert.ok(doc.getElementById('closed-view'), 'the Closed list stays underneath');
  assert.deepStrictEqual(errors, []);
  w.close();
}

async function theListViewToastOpensThePanel() {
  //given
  const {w, doc, errors, opened} = await boot('#t=anoms', {view: 'list'});

  //when
  const t = w.hiddenToast(ON_BOARD);
  t.querySelector('.toast-view').click();
  await wait(60);
  w.renderAnoms();

  //then
  assert.ok(doc.querySelector('.card-drawer'), 'the list view toast opens the panel');
  assert.ok(doc.querySelector('.card-drawer').textContent.includes('checkout-service'));
  assert.strictEqual(w.location.hash, '#c=11');
  assert.deepStrictEqual(opened, [], 'no new window');
  assert.deepStrictEqual(errors, []);
  w.close();
}

(async () => {
  await anOldClosedCardOpensInThePanel();
  console.log('PASS an old closed card opens in the panel and stays open through a repaint');
  await aClosedCardHidesActionsThatChangeStatus();
  console.log('PASS a closed card hides actions that change status; a live closed one offers Reopen and Copy link');
  await aBareCardFromTheApiOpensWithoutUndefined();
  console.log('PASS a bare card from the API opens with no undefined text');
  await aLiveOpenCardOffTheBoardOffersNoReopen();
  console.log('PASS a live open card that is not on the loaded board opens without Reopen');
  await aLateAnswerNeverTakesOverThePanel();
  console.log('PASS a late answer for a linked card never replaces the card picked on the board or its address');
  await aLateAnswerAfterCloseOpensNothing();
  console.log('PASS a late answer after the panel was closed opens nothing');
  await aNetworkErrorSaysTheCardCouldNotBeLoaded();
  console.log('PASS a network error on the linked card says it could not be loaded');
  await theClosedCardRowNamesEveryControlAndHidesItsIcons();
  console.log('PASS the closed card row hides its icons and names every control');
  await aViewerGetsNoReopen();
  console.log('PASS a viewer gets no Reopen on a closed card');
  await aRefusedOrFailedReopenSaysSo();
  console.log('PASS a failed or signed-out Reopen says so');
  await anUnknownNumberShowsNotFound();
  console.log('PASS an unknown number shows the board with "Card not found."');
  await thePanelHasNoOpenAloneLink();
  console.log('PASS the panel has no Open alone link');
  await theClosedListNumberOpensThePanel();
  console.log('PASS the Closed list number opens the panel without a new window');
  await theListViewToastOpensThePanel();
  console.log('PASS the View card toast in list view opens the panel, not a new window');
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
