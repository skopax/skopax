const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const PHONE = '(max-width: 640px), (max-height: 500px) and (pointer: coarse)';
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

async function boot(hash, phone) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state()));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/' + hash, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.localStorage.setItem('skopax-card-view', 'board');
      if (phone === 'short') w.matchMedia = (q) => ({matches: /max-height: 500px/.test(q), media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}});
      if (phone === true) {
        const listeners = [];
        w.__media = {matches: true, listeners};
        w.matchMedia = (q) => ({get matches() { return q === PHONE ? w.__media.matches : false; }, media: q, addEventListener(t, f) { if (q === PHONE) listeners.push(f); }, removeEventListener() {}, addListener(f) { if (q === PHONE) listeners.push(f); }, removeListener() {}});
        const pos = new WeakMap();
        const step = (e) => e.classList.contains('bcol') ? 310 : 90;
        Object.defineProperty(w.HTMLElement.prototype, 'offsetLeft', {configurable: true, get() { return this.parentElement ? [...this.parentElement.children].indexOf(this) * step(this) : 0; }});
        Object.defineProperty(w.HTMLElement.prototype, 'offsetWidth', {configurable: true, get() { return 80; }});
        Object.defineProperty(w.Element.prototype, 'clientWidth', {configurable: true, get() { return 300; }});
        Object.defineProperty(w.Element.prototype, 'scrollLeft', {configurable: true, get() { return pos.get(this) || 0; }, set(v) { pos.set(this, v); }});
      }
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
  const cols = [...doc.querySelectorAll('#anoms .board > .bcol')];
  const wideMarks = cols.filter((c) => c.hasAttribute('aria-labelledby') || c.hasAttribute('tabindex')).length;
  const hint = doc.getElementById('card-search').getAttribute('placeholder');
  const first = current();
  tabs()[2].click();
  const picked = current();
  w.setFilter('all');
  await wait(50);
  const kept = current();
  const inert = [...doc.querySelectorAll('#anoms .bcol[inert]')].length;

  //then
  assert.ok(bar(), 'the board carries a column switcher');
  assert.deepStrictEqual([bar().getAttribute('role'), bar().getAttribute('aria-label')], ['group', 'Board columns'], 'a labelled group');
  assert.strictEqual(bar().nextElementSibling.id, 'board-note', 'it sits above the board and its note');
  assert.deepStrictEqual(names, [['open', 'Open', '1'], ['progress', 'In progress', '1'], ['done', 'Done', '1'], ['muted', 'Muted', '0']], 'one tab per column, with its name and count');
  assert.deepStrictEqual(cols.map((c) => c.dataset.col), ['open', 'progress', 'done', 'muted'], 'the columns keep their order and data-col');
  assert.strictEqual(wideMarks, 0, 'on a wide screen the columns get no new label or tab stop');
  assert.strictEqual(hint, 'Search cards: number, service, text, assignee, ticket', 'the full search hint fits a wide screen');
  assert.deepStrictEqual(first, ['open'], 'Open is shown first');
  assert.deepStrictEqual(picked, ['done'], 'a tap picks a column');
  assert.deepStrictEqual(kept, ['done'], 'and a refresh of the board keeps it');
  assert.strictEqual(inert, 0, 'on a wide screen no column is inert');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the board shows a labelled group of Open, In progress, Done and Muted tabs with counts; a tap picks a column and a refresh keeps it';
}

async function onAPhoneTheSwitcherScrollsFocusesAndFollowsSwipes() {
  //given
  const {dom, w, doc, errors} = await boot('#t=anoms/all', true);
  const board = () => doc.querySelector('#anoms .board');
  const col = (c) => doc.querySelector('#anoms .bcol[data-col="' + c + '"]');
  const tab = (c) => doc.querySelector('#anoms .board-tab[data-col="' + c + '"]');
  const inert = () => [...doc.querySelectorAll('#anoms .bcol')].filter((c) => c.hasAttribute('inert')).map((c) => c.dataset.col);
  const current = () => [...doc.querySelectorAll('#anoms .board-tab[aria-current="true"]')].map((b) => b.dataset.col);

  //when
  const start = {inert: inert(), x: board().scrollLeft};
  const labels = [...doc.querySelectorAll('#anoms .bcol')].map((c) => [doc.getElementById(c.getAttribute('aria-labelledby')).textContent, c.getAttribute('tabindex')]);
  const hint = doc.getElementById('card-search').getAttribute('placeholder');
  tab('done').click();
  const picked = {x: board().scrollLeft, focus: doc.activeElement === col('done'), inert: inert(), bar: doc.querySelector('#anoms .board-tabs').scrollLeft};
  w.setFilter('all');
  const redrawn = {x: board().scrollLeft, current: current(), inert: inert()};
  board().scrollLeft = 3 * 310 - 20;
  board().dispatchEvent(new w.Event('scroll'));
  const swiped = {current: current(), inert: inert(), bar: doc.querySelector('#anoms .board-tabs').scrollLeft};
  board().scrollLeft = 0;
  const panel = doc.getElementById('panel-anoms');
  panel.hidden = true;
  panel.hidden = false;
  await wait(20);
  const shown = board().scrollLeft;
  w.__media.matches = false;
  w.__media.listeners.forEach((f) => f({matches: false}));
  const wide = inert();
  const wideMarks = [...doc.querySelectorAll('#anoms .bcol')].filter((c) => c.hasAttribute('aria-labelledby') || c.hasAttribute('tabindex')).length;
  const wideHint = doc.getElementById('card-search').getAttribute('placeholder');

  //then
  assert.deepStrictEqual(start, {inert: ['progress', 'done', 'muted'], x: 0}, 'Open shows first and the other columns are out of the tab order');
  assert.deepStrictEqual(picked, {x: 620, focus: true, inert: ['open', 'progress', 'muted'], bar: 0}, 'a tab scrolls to its column, moves the focus there and frees only that column');
  assert.deepStrictEqual(redrawn, {x: 620, current: ['done'], inert: ['open', 'progress', 'muted']}, 'a refresh puts the same column back at once');
  assert.deepStrictEqual(swiped, {current: ['muted'], inert: ['open', 'progress', 'done'], bar: 50}, 'a swipe moves the current tab and scrolls it into view');
  assert.strictEqual(shown, 930, 'showing the board page again lines up the current column');
  assert.deepStrictEqual(wide, [], 'growing past phone width frees every column');
  assert.deepStrictEqual(labels, [['Open', '-1'], ['In progress', '-1'], ['Done', '-1'], ['Muted', '-1']], 'on a phone each column is named by its heading and can take the focus');
  assert.strictEqual(wideMarks, 0, 'and loses both again on a wide screen');
  assert.deepStrictEqual([hint, wideHint], ['Search cards', 'Search cards: number, service, text, assignee, ticket'], 'the search hint is short on a phone and full again on a wide screen');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'on a phone a tab scrolls to its column and focuses it, other columns are inert, refreshes and page switches keep the column, a swipe moves the tab, and a wide screen frees every column';
}

async function theCardDrawerShowsChatAndInvestigateAsAnAiSegmentLikeStatus() {
  //given
  const {dom, w, doc, errors} = await boot('#t=anoms/all');

  //when
  w.openDrawer('log|checkout-service|ERROR|boom', true);
  await wait(50);
  const acts = doc.querySelector('.card-drawer .acts');
  const groups = [...acts.querySelectorAll(':scope > .act-group')].map((g) => doc.getElementById(g.getAttribute('aria-labelledby')).textContent);
  const ai = acts.querySelector(':scope > .act-group.ai');
  const segs = [...ai.querySelectorAll(':scope > .ag-body > .seg > .seg-b')].map((b) => [b.tagName.toLowerCase(), b.classList.contains('chat-btn') ? 'chat' : b.classList.contains('inv-btn') ? 'investigate' : '?', b.getAttribute('aria-label') || b.textContent, !!b.querySelector('svg[aria-hidden="true"]'), b.querySelector('.lbl') ? b.querySelector('.lbl').textContent : null]);
  const chat = ai.querySelector('.chat-btn');

  //then
  assert.deepStrictEqual(groups, ['AI', 'Status', 'Owner', 'Links'], 'the drawer reads AI, Status, Owner, Links');
  assert.strictEqual(ai.getAttribute('role'), 'group', 'AI is a labelled group like Status');
  assert.deepStrictEqual(segs, [['a', 'chat', 'Chat about this card', true, 'Chat about this card'], ['button', 'investigate', 'Investigate', true, 'Investigate']], 'Chat and Investigate are icon and word segments with their names');
  assert.strictEqual(chat.getAttribute('href'), '/#t=chat/new/14', 'Chat still opens a new chat with the card');
  assert.strictEqual(acts.querySelector('.act-primary'), null, 'there is no filled primary button row any more');
  assert.ok(![...ai.querySelectorAll('.seg-b')].some((b) => b.hasAttribute('aria-pressed')), 'they are actions, not toggles');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the card drawer shows Chat and Investigate as an AI group of icon and word segments like Status, with their names, and no filled primary button';
}

async function aShortLandscapePhoneGetsTheDrawerMenuToo() {
  //given
  const {dom, doc, errors} = await boot('#t=anoms', 'short');
  const hamb = doc.getElementById('side-open');

  //when
  const rail = doc.getElementById('side-rail').getAttribute('aria-label');
  hamb.click();
  const opened = {open: doc.body.classList.contains('drawer-open'), inert: doc.getElementById('content').hasAttribute('inert'), expanded: hamb.getAttribute('aria-expanded')};

  //then
  assert.strictEqual(rail, 'Close menu', 'the sidebar runs as a drawer on a short coarse screen');
  assert.deepStrictEqual(opened, {open: true, inert: true, expanded: 'true'}, 'and opens over the content like on a phone');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a phone held sideways (500 px high or less, coarse pointer) gets the drawer menu';
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
    headClip: [value(phone, '.board .bcol-h', 'clip'), value(phone, '.board .bcol-h', 'position')],
    viewIcon: value(phone, '#card-view > button[data-view]', 'font-size'),
    switchHit: value(phone, '.feat-sw::before, .int-switch::before, .adm-c .adm-sw::before'.split(', ')[0], 'inset'),
    chips: value(phone, '#card-qf .qf-g', 'flex-wrap'),
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
    tabsHidden: 'none', tabsShown: 'flex', snap: 'x mandatory', column: '100%', colSnap: 'start', headClip: ['rect(0px 0px 0px 0px)', 'absolute'], viewIcon: '0px', switchHit: '-14px -8px', chips: 'nowrap',
    drawer: '100%', drawerEdge: '0px', ibtn: ['44px', '44px'], btn: '44px', tile: ['44px', '44px'],
    input: '16px', inputPriority: 'important', chat: 'calc(100dvh - 150px)', safeTop: 'max(16px, env(safe-area-inset-left))',
  });
  return 'at 640 px and below the board snaps one column at a time under its tabs, the card drawer fills the screen, targets are 44 px, inputs 16 px and the top bar and chat respect the safe area and dynamic viewport';
}

function onAPhoneTheBoardHeaderKeepsItsDomOrderOnScreen() {
  //given
  const phone = phoneRules(sheet());
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', '{}'));
  const panel = dom.window.document.getElementById('panel-anoms');

  //when
  const reordered = phone.filter((r) => r.style.getPropertyValue('order') !== '').map((r) => r.selectorText);
  const flattened = phone.filter((r) => r.style.getPropertyValue('display') === 'contents').map((r) => r.selectorText);
  const ids = [...panel.querySelectorAll('#card-search, #card-view, #card-qf, #filter-note, #anoms')].map((e) => e.id);
  dom.window.close();

  //then
  assert.deepStrictEqual(reordered, [], 'no phone rule moves a control away from its place in the page, so the visual order is the focus order');
  assert.deepStrictEqual(flattened, ['#panel-anoms:not(.closed-on) > .card-search, #panel-anoms:not(.closed-on) > #filter-note'], 'the header boxes are flattened into one row flow, not reordered');
  assert.deepStrictEqual(ids, ['card-search', 'card-view', 'card-qf', 'filter-note', 'anoms'], 'search, Board/List, chips, the Showing line with Filters, then the board');
  return 'the compact phone board header sets no CSS order, so search, Board/List, chips, the Showing line with Filters and the board read in DOM order';
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
  const words = ['Board columns', 'AI'];

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
  console.log('PASS ' + await onAPhoneTheSwitcherScrollsFocusesAndFollowsSwipes());
  console.log('PASS ' + await aShortLandscapePhoneGetsTheDrawerMenuToo());
  console.log('PASS ' + await theCardDrawerShowsChatAndInvestigateAsAnAiSegmentLikeStatus());
  console.log('PASS ' + onAPhoneTheStylesGiveOneColumnAFullScreenDrawerBigTargetsAnd16pxInputs());
  console.log('PASS ' + onAPhoneTheBoardHeaderKeepsItsDomOrderOnScreen());
  console.log('PASS ' + noRuleGivesAnythingAFixedWidthWiderThanAPhone());
  console.log('PASS ' + theNewWordsAreInEveryPack());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
