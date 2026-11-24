const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const A = 'log|checkout-service|ERROR|boom', B = 'log|payments-api|ERROR|npe';
const KEY = 'skopax.drawer.wide';

function card(key) {
  return {key, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'ERROR lines', src: 'logs', sig: 'log', num: key === A ? 11 : 12, now: 1, firstSeen: '2026-11-18T10:00:00Z', lastSeen: '2026-11-18T10:30:00Z', what: 'first text'};
}
const STATE = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
  anomalies: [card(A), card(B)], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};

async function boot(opts) {
  opts = opts || {};
  const errors = [];
  const vc = new VirtualConsole(); vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE, {view: 'board'}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(STATE)), {
    url: 'https://skopax.example.com/#t=anoms/all', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {}; w.Element.prototype.scrollIntoView = function () {};
      if (opts.store) {
        const kept = Object.assign({}, opts.initial || {});
        const fake = {getItem: (k) => (opts.store === 'throw' ? (() => { throw new Error('blocked'); })() : (k in kept ? kept[k] : null)),
          setItem: (k, v) => { if (opts.store === 'throw') throw new Error('blocked'); kept[k] = String(v); }, removeItem: (k) => { delete kept[k]; }};
        Object.defineProperty(w, 'localStorage', {value: fake, configurable: true});
        w.__kept = kept;
      }
      w.fetch = async (p) => {
        p = String(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'op@example.com', operator: true, teams: [], tags: []});
        if (p.startsWith('/api/actions')) return ok([]);
        if (p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/state')) return ok(STATE);
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(350);
  return {w: dom.window, doc: dom.window.document, errors};
}
const tile = (doc, key) => [...doc.querySelectorAll('#anoms .tile')].find((t) => t.dataset.key === key);

async function theButtonToggles() {
  //given
  const {doc, errors} = await boot({store: 'ok', initial: {[KEY]: '0'}});
  tile(doc, A).click();
  const drawer = doc.querySelector('.card-drawer');
  const btn = drawer.querySelector('.cd-bar .cd-expand');

  //then
  assert.ok(btn, 'the expand button is in the drawer bar');
  assert.strictEqual(btn.tagName, 'BUTTON');
  assert.strictEqual(btn.textContent, 'Expand');
  assert.ok(btn.getAttribute('aria-label') && btn.title, 'it has an aria-label and a title');
  assert.ok(!drawer.classList.contains('wide'));
  const kids = [...drawer.querySelector('.cd-bar').children].map((c) => c.className.split(' ').find((x) => x.startsWith('cd-')));
  assert.deepStrictEqual(kids, ['cd-expand', 'cd-t', 'cd-close'], 'first in the bar, on the left side the drawer grows to');
  assert.strictEqual(btn.querySelectorAll('svg.cd-x-i').length, 1, 'Expand carries an arrow icon');
  const arrowOf = () => [...btn.querySelectorAll('polyline')].map((p) => p.getAttribute('points')).join(' ');
  const expandArrow = arrowOf();

  //when
  btn.click();

  //then
  assert.ok(drawer.classList.contains('wide'));
  assert.strictEqual(btn.textContent, 'Shrink');
  assert.ok(btn.querySelector('svg.cd-x-i') && arrowOf() !== expandArrow, 'Shrink carries the opposite arrow');
  assert.strictEqual(btn.getAttribute('aria-pressed'), 'true');
  assert.strictEqual(btn.getAttribute('aria-label'), 'Shrink the card to the narrow view');
  assert.strictEqual(btn.title, 'Shrink the card to the narrow view');

  //when
  btn.click();

  //then
  assert.ok(!drawer.classList.contains('wide'));
  assert.strictEqual(btn.textContent, 'Expand');
  assert.strictEqual(btn.getAttribute('aria-label'), 'Expand the card to the wide view');
  assert.strictEqual(btn.title, 'Expand the card to the wide view');
  assert.deepStrictEqual(errors, []);
}

async function itOpensWideFromAStoredChoiceAndClosesAfterAToggle() {
  //given
  const {w, doc, errors} = await boot({store: 'ok', initial: {[KEY]: '1'}});

  //when
  tile(doc, A).click();
  const drawer = doc.querySelector('.card-drawer');

  //then
  assert.ok(drawer.classList.contains('wide'), 'the first drawer of the page opens wide');
  assert.strictEqual(drawer.querySelector('.cd-expand').textContent, 'Shrink');
  assert.strictEqual(drawer.querySelector('.cd-expand').getAttribute('aria-pressed'), 'true');

  //when
  drawer.querySelector('.cd-expand').click();
  drawer.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));

  //then
  assert.strictEqual(doc.querySelector('.card-drawer'), null, 'Escape closes the drawer after a toggle');
  assert.strictEqual(w.__kept[KEY], '0');

  //when
  tile(doc, B).click();
  doc.querySelector('.cd-expand').click();
  doc.querySelector('.cd-close').click();

  //then
  assert.strictEqual(doc.querySelector('.card-drawer'), null, 'Close closes the drawer after a toggle');
  assert.strictEqual(w.__kept[KEY], '1');
  assert.deepStrictEqual(errors, []);
}

async function theChoiceIsRemembered() {
  //given
  const {w, doc, errors} = await boot({store: 'ok'});
  tile(doc, A).click();

  //then
  assert.ok(doc.querySelector('.card-drawer').classList.contains('wide'), 'with no saved choice the first drawer opens wide');

  //when
  doc.querySelector('.cd-expand').click();
  doc.querySelector('.cd-expand').click();

  //then
  assert.strictEqual(w.__kept[KEY], '1');

  //when
  doc.querySelector('.cd-close').click();
  tile(doc, B).click();

  //then
  const next = doc.querySelector('.card-drawer');
  assert.ok(next.classList.contains('wide'), 'the next drawer opens wide');
  assert.strictEqual(next.querySelector('.cd-expand').textContent, 'Shrink');

  //when
  next.querySelector('.cd-expand').click();
  doc.querySelector('.cd-close').click();
  tile(doc, A).click();

  //then
  assert.strictEqual(w.__kept[KEY], '0');
  assert.ok(!doc.querySelector('.card-drawer').classList.contains('wide'));
  assert.deepStrictEqual(errors, []);
}

async function itStillTogglesWhenStorageThrows() {
  //given
  const {doc, errors} = await boot({store: 'throw'});
  tile(doc, A).click();
  const drawer = doc.querySelector('.card-drawer');

  //then
  assert.ok(drawer.classList.contains('wide'), 'blocked storage opens wide, the default');

  //when
  drawer.querySelector('.cd-expand').click();

  //then
  assert.ok(!drawer.classList.contains('wide'));
  assert.strictEqual(drawer.querySelector('.cd-expand').textContent, 'Expand');

  //when
  drawer.querySelector('.cd-expand').click();

  //then
  assert.ok(drawer.classList.contains('wide'));
  assert.deepStrictEqual(errors, []);
}

function theStylesCoverWidthAndPhones() {
  //given
  const css = fs.readFileSync(TEMPLATE, 'utf8');

  //then
  assert.ok(/@media \(min-width: 900px\)[^\n]*\.card-drawer\.wide > \.cd-panel \{ width: min\(1400px, 98vw\); \}/.test(css), 'wide width from 900 px up');
  assert.ok(/@media \(max-width: 899px\) \{ \.cd-expand \{ display: none; \} \}/.test(css), 'the button is hidden below 900 px');
  assert.ok(/\.cd-panel \{[^}]*transition: width \.24s var\(--ease\)/.test(css), 'the width animates');
  assert.ok(/@media \(prefers-reduced-motion: reduce\)[\s\S]*\.cd-panel \{ transition: none; animation: none; \}/.test(css), 'reduced motion switches it off');
}

(async () => {
  await theButtonToggles();
  console.log('PASS the Expand button widens the drawer and Shrink narrows it again');
  await itOpensWideFromAStoredChoiceAndClosesAfterAToggle();
  console.log('PASS a stored wide choice opens the first drawer wide and Escape and Close still close it');
  await theChoiceIsRemembered();
  console.log('PASS the wide choice is remembered for the next drawer');
  await itStillTogglesWhenStorageThrows();
  console.log('PASS the button still toggles when storage throws');
  theStylesCoverWidthAndPhones();
  console.log('PASS the styles cover the wide width, phones and reduced motion');
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
