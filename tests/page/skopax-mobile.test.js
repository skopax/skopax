const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const PHONE = '(max-width: 640px)';
const PACKS = ['tr', 'de', 'es', 'fr'];
const TABLES_IN_A_BOX = ['.inv-recent', '.ntable', '.ttable', '.csrc, .cruns', '.rel-deps', '#svcs', '#logs', '#routes', '#hist'];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

function state() {
  const card = (key, extra) => Object.assign({key, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'ERROR lines', src: 'logs', sig: 'log', now: 1, firstSeen: iso(30), lastSeen: iso(1)}, extra || {});
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: iso(-59), sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card('log|checkout-service|ERROR|boom', {num: 14}), card('log|shop|ERROR|npe', {num: 15, sev: 'crit', triage: 'progress', owner: 'alice@example.com'}),
      card('log|cart-service|ERROR|x', {num: 16, triage: 'handled'})],
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
      w.localStorage.setItem('skopax-card-view', 'board');
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        p = String(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', operator: true, admin: false, teams: [], tags: []});
        if (p.startsWith('/api/server')) return ok({plugins: [], tz: 'UTC'});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
      };
    },
  });
  await wait(400);
  return {dom, w: dom.window, doc: dom.window.document, errors};
}

function sheet() {
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', '{}'));
  const top = [...dom.window.document.querySelector('style').sheet.cssRules];
  dom.window.close();
  return top;
}

function plain(list) {
  return list.flatMap((r) => r.cssRules && !r.selectorText ? plain([...r.cssRules]) : r.selectorText ? [r] : []);
}

function phoneRules(top) {
  return top.filter((r) => r.media && r.media.mediaText === PHONE).flatMap((r) => [...r.cssRules]).filter((r) => r.selectorText);
}

function value(rules, selector, prop) {
  return rules.filter((r) => r.selectorText.split(',').map((s) => s.trim()).indexOf(selector) >= 0).map((r) => r.style.getPropertyValue(prop)).filter(Boolean).slice(-1)[0] || '';
}

async function onAPhoneTheMenuOpensAsAnOffCanvasDrawerAndClosesOnEscapeAndTapOut() {
  //given
  const {dom, w, doc, errors} = await boot('#t=anoms');
  const hamb = doc.getElementById('side-open'), scrim = doc.getElementById('side-scrim');
  const state = () => ({open: doc.body.classList.contains('drawer-open'), expanded: hamb.getAttribute('aria-expanded'), scrim: !scrim.hidden});

  //when
  const closed = state();
  hamb.click();
  const opened = state();
  doc.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  const escaped = state();
  hamb.click();
  scrim.click();
  const tapped = state();

  //then
  assert.deepStrictEqual(closed, {open: false, expanded: 'false', scrim: false}, 'the menu starts closed');
  assert.deepStrictEqual(opened, {open: true, expanded: 'true', scrim: true}, 'the hamburger opens it over a backdrop and says so');
  assert.deepStrictEqual(escaped, {open: false, expanded: 'false', scrim: false}, 'Escape closes it');
  assert.deepStrictEqual(tapped, {open: false, expanded: 'false', scrim: false}, 'so does a tap on the backdrop');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the off-canvas menu opens from the hamburger with aria-expanded and a backdrop, and Escape or a tap outside closes it';
}

async function theBoardHasAColumnSwitcherThatKeepsItsColumnAcrossARefresh() {
  //given
  const {dom, w, doc, errors} = await boot('#t=anoms/all');
  const bar = () => doc.querySelector('#anoms .board-tabs');
  const tabs = () => [...bar().querySelectorAll('button.board-tab')];
  const current = () => tabs().filter((b) => b.getAttribute('aria-current') === 'true').map((b) => b.dataset.col);

  //when
  const names = tabs().map((b) => [b.dataset.col, b.firstChild.textContent, b.querySelector('.count').textContent]);
  const cols = [...doc.querySelectorAll('#anoms .board > .bcol')].map((c) => c.dataset.col);
  const first = current();
  tabs()[2].click();
  const picked = current();
  w.setFilter('all');
  await wait(50);
  const kept = current();

  //then
  assert.ok(bar(), 'the board carries a column switcher');
  assert.strictEqual(bar().getAttribute('aria-label'), 'Board columns');
  assert.strictEqual(bar().nextElementSibling.id, 'board-note', 'it sits above the board and its note');
  assert.deepStrictEqual(names, [['open', 'Open', '1'], ['progress', 'In progress', '1'], ['done', 'Done', '1'], ['muted', 'Muted', '0']], 'one tab per column, with its name and count');
  assert.deepStrictEqual(cols, ['open', 'progress', 'done', 'muted'], 'the columns keep their order and data-col');
  assert.deepStrictEqual(first, ['open'], 'Open is shown first');
  assert.deepStrictEqual(picked, ['done'], 'a tap picks a column');
  assert.deepStrictEqual(kept, ['done'], 'and a refresh of the board keeps it');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the board shows Open, In progress, Done and Muted tabs with counts; a tap picks a column and a refresh keeps it';
}

function onAPhoneTheStylesGiveOneColumnAFullScreenDrawerBigTargetsAnd16pxInputs() {
  //given
  const top = sheet();
  const phone = phoneRules(top), all = plain(top);

  //when
  const got = {
    tabsHidden: value(all.filter((r) => !phone.includes(r)), '.board-tabs', 'display'),
    tabsShown: value(phone, '.board-tabs', 'display'),
    snap: value(phone, '.board', 'scroll-snap-type'),
    column: value(phone, '.board', 'grid-auto-columns'),
    colSnap: value(phone, '.bcol', 'scroll-snap-align'),
    drawer: value(phone, '.card-drawer > .cd-panel', 'width'),
    drawerEdge: value(phone, '.card-drawer > .cd-panel', 'border-left-width') || value(phone, '.card-drawer > .cd-panel', 'border-left'),
    ibtn: [value(phone, '.ibtn', 'width'), value(phone, '.ibtn', 'height')],
    btn: value(phone, '.btn', 'min-height'),
    tile: [value(phone, '.tile-acts .ta-b', 'min-width'), value(phone, '.tile-acts .ta-b', 'min-height')],
    input: value(phone, 'textarea', 'font-size'),
    inputPriority: phone.filter((r) => r.selectorText.indexOf('textarea') >= 0).map((r) => r.style.getPropertyPriority('font-size'))[0],
    chat: value(phone, '.chat-frame', 'height'),
    safeTop: value(phone, '.top', 'padding-left'),
  };

  //then
  assert.ok(phone.length > 10, 'the phone rules are read: ' + phone.length);
  assert.deepStrictEqual(got, {
    tabsHidden: 'none', tabsShown: 'flex', snap: 'x mandatory', column: '100%', colSnap: 'start',
    drawer: '100%', drawerEdge: '0px', ibtn: ['44px', '44px'], btn: '44px', tile: ['44px', '44px'],
    input: '16px', inputPriority: 'important', chat: 'calc(100dvh - 150px)', safeTop: 'max(16px, env(safe-area-inset-left))',
  });
  return 'at 640 px and below the board snaps one column at a time under its tabs, the card drawer fills the screen, targets are 44 px, inputs 16 px and the top bar and chat respect the safe area and dynamic viewport';
}

function noRuleGivesAnythingAFixedWidthWiderThanAPhone() {
  //given
  const all = plain(sheet());
  const px = (v) => { const m = /^(\d+(?:\.\d+)?)(px|rem)$/.exec(String(v).trim()); return m ? Number(m[1]) * (m[2] === 'rem' ? 16 : 1) : 0; };

  //when
  const wide = all.filter((r) => px(r.style.getPropertyValue('width')) > 375 && !/^(100%|calc\(100% *-|100vw)/.test(r.style.getPropertyValue('max-width'))).map((r) => r.selectorText);
  const floors = all.filter((r) => px(r.style.getPropertyValue('min-width')) > 375).map((r) => r.selectorText).filter((s) => TABLES_IN_A_BOX.indexOf(s) < 0);

  //then
  assert.deepStrictEqual(wide, [], 'a fixed width over 375 px needs a max-width that keeps it inside the screen');
  assert.deepStrictEqual(floors, [], 'only tables that scroll inside their own box may be wider than a phone');
  return 'no rule sets a fixed width over 375 px without a cap, and only the tables that scroll in their own box have a wider floor';
}

function theNewWordsAreInEveryPack() {
  //given
  const words = ['Board columns'];

  //when
  const missing = PACKS.flatMap((lang) => {
    const pack = JSON.parse(fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'locale', lang, 'page.json'), 'utf8')).page;
    return words.filter((k) => typeof pack[k] !== 'string' || !pack[k]).map((k) => lang + ': ' + k);
  });

  //then
  assert.deepStrictEqual(missing, []);
  return 'the column switcher label is translated in tr, de, es and fr';
}

(async () => {
  console.log('PASS ' + await onAPhoneTheMenuOpensAsAnOffCanvasDrawerAndClosesOnEscapeAndTapOut());
  console.log('PASS ' + await theBoardHasAColumnSwitcherThatKeepsItsColumnAcrossARefresh());
  console.log('PASS ' + onAPhoneTheStylesGiveOneColumnAFullScreenDrawerBigTargetsAnd16pxInputs());
  console.log('PASS ' + noRuleGivesAnythingAFixedWidthWiderThanAPhone());
  console.log('PASS ' + theNewWordsAreInEveryPack());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
