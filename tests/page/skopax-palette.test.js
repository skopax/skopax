const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const EVIL = '<img src=x onerror=alert(1)>"\'';
const KEY = 'log|checkout-service|ERROR|boom';
const MARKUP = 'log|cart-service|ERROR|markup';
const ADMIN_ITEMS = [
  {key: 'general.timezone', group: 'General', label: 'Timezone', help: 'IANA timezone name', kind: 'text', live: true, value: 'UTC', base: 'UTC', default: 'UTC', source: 'default'},
  {key: 'investigate.model', group: 'Investigator', label: 'Card model', help: 'Model for card runs', kind: 'choice', choices: ['claude-opus-5-5'], live: true, value: 'claude-opus-5-5', base: 'claude-opus-5-5', default: 'claude-opus-5-5', source: 'default'},
  {key: 'cards.archive-hours', group: 'Cards', label: EVIL, help: 'h', kind: 'int', low: 1, high: 720, live: true, value: '48', base: '48', default: '48', source: 'default'}];
const LOCAL = {loginEnabled: true, user: 'alice@example.com', name: 'Alice', userRole: 'admin', source: 'local', canChangePassword: true, operator: true, admin: true, teams: [], tags: [], chat: true,
  instances: {current: {name: 'production', letter: 'P'}, links: []}};
const VIEWER = {loginEnabled: true, user: 'bob@example.com', userRole: 'viewer', source: 'sso', canChangePassword: false, operator: false, admin: false, teams: [], tags: [], chat: false,
  instances: {current: {name: 'production', letter: 'P'}, links: []}};

function state() {
  const card = (key, extra) => Object.assign({key, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'ERROR lines', src: 'logs', sig: 'log', now: 1, ns: 'shop', firstSeen: iso(30), lastSeen: iso(1)}, extra || {});
  const svc = (name) => ({svc: name, ns: 'shop', kind: 'app', status: 'ok', req: 10});
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(KEY, {num: 14}), card('log|shop|ERROR|npe', {num: 15, sev: 'crit'}), card('log|auth-service|ERROR|x', {num: 17}), card('log|search-api|ERROR|y', {num: 18}),
      card('log|payments-api|ERROR|z', {num: 19}), card(MARKUP, {num: 16, status: 'cleared', sev: 'info', label: '<b>kaboom</b> & <i>'})],
    services: [svc('checkout-service'), svc('shop'), svc('cart-service')], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(opts) {
  opts = opts || {};
  const errors = [], seen = [], srv = {down: 0, failNext: 0, keyDown: 0};
  let pending = 0;
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  vc.on('error', (e) => errors.push('console.error ' + e));
  const me = opts.me || LOCAL;
  const plugins = [{id: 'demo', name: 'Demo', icon: '/plugins/demo/icon.svg', tab: 'demo', passes: [], instances: [], pages: [{tab: 'demo', label: 'Demo board', group: 'observe', icon: '/plugins/demo/icon.svg'}]}];
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state())), {
    url: 'https://skopax.example.com/' + (opts.hash || '#t=anoms'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () { w.__scrolled = this; };
      if (opts.mac) Object.defineProperty(w.navigator, 'platform', {get: () => 'MacIntel'});
      const answer = async (path) => {
        const p = String(path);
        seen.push(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me')) return ok(me);
        if (p.startsWith('/api/server')) return ok({plugins, tz: 'UTC'});
        if (p.startsWith('/api/admin/settings')) return ok({items: opts.items || ADMIN_ITEMS, rev: 1, refreshSeconds: 15});
        if (p.startsWith('/api/admin/features')) {
          if (opts.slowFeatures) await new Promise((r) => setTimeout(r, opts.slowFeatures));
          const code = srv.failNext > 0 ? 500 : srv.down;
          if (srv.failNext > 0) srv.failNext -= 1;
          if (code) return {ok: false, status: code, json: async () => ({detail: 'no'}), text: async () => ''};
          return ok({edition: 'community', items: opts.features || []});
        }
        if (p.startsWith('/api/admin/notices')) return ok({items: []});
        if (p.startsWith('/api/admin/anthropic-key')) return srv.keyDown ? {ok: false, status: srv.keyDown, json: async () => ({detail: 'no'}), text: async () => ''} : ok({using: null, admin: false, config: false, unreadable: false, updatedBy: null, updatedAt: null, note: null});
        if (p.startsWith('/api/admin/plugins')) return ok({edition: 'community', secretsKey: true, plugins: []});
        if (p.startsWith('/api/admin/users') || p.startsWith('/api/admin/tokens')) return ok([]);
        if (p.startsWith('/api/admin/sso')) return ok({enabled: true, active: false, problem: null, redirectUri: '', provider: {ok: true, since: null, error: null}, values: {}, secret: {set: false, from: null}});
        if (p.startsWith('/api/admin/small-talk')) return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
        if (p.startsWith('/api/admin/history')) return ok([]);
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
      };
      w.fetch = async (path) => {
        pending += 1;
        try { return await answer(path); } finally { pending -= 1; }
      };
    },
  });
  await wait(350);
  const close = async () => {
    while (pending) await wait(5);
    await wait(0);
    dom.window.close();
  };
  return {dom, w: dom.window, doc: dom.window.document, errors, seen, srv, close};
}

const key = (w, el, k, extra) => el.dispatchEvent(new w.KeyboardEvent('keydown', Object.assign({key: k, bubbles: true, cancelable: true}, extra || {})));
const type = (w, doc, text) => { const q = doc.getElementById('pal-q'); q.value = text; q.dispatchEvent(new w.Event('input', {bubbles: true})); };
const boxes = (doc) => [...doc.querySelectorAll('#pal-list > [role="group"]')];
const groups = (doc) => boxes(doc).map((g) => g.querySelector('.pal-g').textContent);
const options = (doc, group) => boxes(doc).filter((g) => !group || g.querySelector('.pal-g').textContent === group).flatMap((g) => [...g.querySelectorAll('[role="option"]')]);
const rows = (doc, group) => options(doc, group).map((o) => o.querySelector('.lbl').textContent);
const option = (doc, group) => options(doc, group)[0];
const selected = (doc) => [...doc.querySelectorAll('#pal-list [role="option"][aria-selected="true"]')].map((o) => o.id);

async function ctrlKOpensTheSearchAndEscRestoresTheFocus() {
  //given
  const {dom, w, doc, errors, close} = await boot();
  const mac = await boot({mac: true});
  const pal = doc.getElementById('pal'), q = doc.getElementById('pal-q'), from = doc.getElementById('tab-svcs');

  //then
  assert.ok(pal.hidden && pal.parentNode === doc.body, 'the palette is a closed dialog at the body level');
  assert.deepStrictEqual(['role', 'aria-modal', 'aria-label'].map((a) => doc.querySelector('#pal .pal-box').getAttribute(a)), ['dialog', 'true', 'Search']);
  assert.deepStrictEqual(['role', 'aria-expanded', 'aria-controls', 'aria-autocomplete', 'autocomplete'].map((a) => q.getAttribute(a)), ['combobox', 'false', 'pal-list', 'list', 'off'], 'closed, it shows no list');
  assert.strictEqual(doc.getElementById('pal-list').getAttribute('role'), 'listbox');
  assert.deepStrictEqual([doc.querySelector('#search-btn kbd').textContent, mac.doc.querySelector('#search-btn kbd').textContent], ['Ctrl K', '\u2318K'], 'the key label follows the platform');
  assert.deepStrictEqual([doc.getElementById('search-btn').getAttribute('aria-label'), mac.doc.getElementById('search-btn').title], ['Search (Ctrl K)', 'Search (\u2318K)']);

  //when
  from.focus();
  const taken = !key(w, from, 'k', {ctrlKey: true});

  //then
  assert.ok(taken && !pal.hidden, 'Ctrl+K opens it, from anywhere');
  assert.strictEqual(doc.activeElement, q, 'and focuses the input');
  assert.strictEqual(q.value, '');

  //when
  const held = !key(w, q, 'Tab');
  const heldBack = !key(w, q, 'Tab', {shiftKey: true});

  //then
  assert.ok(held && heldBack && doc.activeElement === q, 'Tab is held inside');

  //when
  const closed = !key(w, q, 'Escape');

  //then
  assert.ok(closed && pal.hidden, 'Esc closes it');
  assert.strictEqual(doc.activeElement, from, 'and gives the focus back');

  //when
  key(w, doc.body, 'k', {metaKey: true});
  const metaOpen = !pal.hidden;
  key(w, doc.getElementById('pal-q'), 'k', {metaKey: true});
  const toggled = pal.hidden;
  doc.getElementById('search-btn').click();
  const byButton = !pal.hidden;
  doc.getElementById('pal-scrim').click();

  //then
  assert.ok(metaOpen && toggled, 'Cmd+K opens it too, and the same keys close it');

  //when
  const cyrillic = !key(w, doc.body, '\u043b', {code: 'KeyK', ctrlKey: true});
  const byCode = !pal.hidden;
  key(w, doc.getElementById('pal-q'), '\u043b', {code: 'KeyK', metaKey: true});

  //then
  assert.ok(cyrillic && byCode && pal.hidden, 'the physical K key works on a non-Latin layout too');
  assert.ok(byButton && pal.hidden, 'the sidebar search button opens it and a click outside closes it');
  assert.strictEqual(doc.activeElement, doc.getElementById('search-btn'));
  assert.deepStrictEqual(errors.concat(mac.errors), []);
  await close(); await mac.close();
  return 'Ctrl+K or Cmd+K open the search with the input focused, Tab stays inside, Esc closes it and gives the focus back; the search button and the scrim work too';
}

async function anEmptyQueryShowsOpenCardsAndPages() {
  //given
  const {dom, w, doc, errors, close} = await boot();

  //when
  doc.getElementById('search-btn').click();

  //then
  assert.deepStrictEqual(groups(doc), ['Cards', 'Pages']);
  assert.deepStrictEqual(rows(doc, 'Cards'), ['#15 shop ERROR lines', '#14 checkout-service ERROR lines', '#17 auth-service ERROR lines', '#18 search-api ERROR lines'], 'the first four open cards, most severe first');
  assert.deepStrictEqual(rows(doc, 'Pages'), ['Board', 'Requests', 'Chat', 'Investigator', 'Services', 'Information'], 'and the first six pages');
  assert.deepStrictEqual([...doc.getElementById('pal-list').children].map((c) => c.getAttribute('role')), ['group', 'group'], 'the listbox holds one group per kind');
  assert.deepStrictEqual(boxes(doc).map((g) => doc.getElementById(g.getAttribute('aria-labelledby')).textContent), ['Cards', 'Pages'], 'each group is named by its header, so it is announced');
  assert.ok(boxes(doc).every((g) => [...g.children].slice(1).every((o) => o.getAttribute('role') === 'option')), 'and holds only options after its header');
  assert.strictEqual(doc.getElementById('pal-q').getAttribute('aria-expanded'), 'true', 'results are shown');
  assert.deepStrictEqual(errors, []);
  await close();
  return 'with nothing typed the search lists the first four open cards and the first six pages';
}

async function checkoutFindsTheCardAndTheService() {
  //given
  const {dom, w, doc, errors, close} = await boot();
  doc.getElementById('search-btn').click();

  //when
  type(w, doc, 'Checkout');

  //then
  assert.deepStrictEqual(groups(doc), ['Cards', 'Services']);
  assert.deepStrictEqual(rows(doc, 'Cards'), ['#14 checkout-service ERROR lines']);
  assert.deepStrictEqual(rows(doc, 'Services'), ['checkout-service']);
  assert.strictEqual(doc.querySelector('#pal-list [role="option"] .lbl').innerHTML, '#14 <mark>checkout</mark>-service ERROR lines', 'the match is marked, case-folded');
  assert.ok([...doc.querySelectorAll('#pal-list [role="option"] .lbl')].every((l) => l.hasAttribute('data-no-tx')), 'card and service text is never translated');

  //when
  type(w, doc, 'e');

  //then
  assert.ok(groups(doc).every((g) => rows(doc, g).length <= 8), 'at most eight rows a group');
  assert.strictEqual(rows(doc, 'Pages').length, 8, 'many pages match one letter, eight are shown');
  assert.deepStrictEqual(errors, []);
  await close();
  return '"checkout" finds card #14 and the checkout-service service, marked; a group shows at most eight rows';
}

async function downMovesTheSelection() {
  //given
  const {dom, w, doc, errors, close} = await boot();
  doc.getElementById('search-btn').click();
  const q = doc.getElementById('pal-q');

  //when
  const first = [selected(doc), q.getAttribute('aria-activedescendant')];
  key(w, q, 'ArrowDown');
  const down = [selected(doc), q.getAttribute('aria-activedescendant')];
  key(w, q, 'ArrowUp');
  key(w, q, 'ArrowUp');
  const wrapped = [selected(doc), q.getAttribute('aria-activedescendant')];
  const options = doc.querySelectorAll('#pal-list [role="option"]');

  //then
  assert.deepStrictEqual(first, [['po-0'], 'po-0'], 'the first row starts selected');
  assert.deepStrictEqual(down, [['po-1'], 'po-1'], 'Down moves aria-selected and aria-activedescendant');
  assert.deepStrictEqual(wrapped, [['po-' + (options.length - 1)], 'po-' + (options.length - 1)], 'Up from the first row wraps to the last');

  //when
  type(w, doc, 'zzzz');

  //then
  assert.strictEqual(q.hasAttribute('aria-activedescendant'), false, 'nothing to select, nothing active');
  assert.strictEqual(q.getAttribute('aria-expanded'), 'false', 'and no list is shown');

  //when
  type(w, doc, '');
  const foot = doc.querySelector('#pal .pal-foot'), head = doc.querySelector('#pal-list .pal-g');
  const kept = [foot, head, doc.querySelector('#pal .pal-in')].map((el) => !el.dispatchEvent(new w.MouseEvent('mousedown', {bubbles: true, cancelable: true})));
  const inInput = q.dispatchEvent(new w.MouseEvent('mousedown', {bubbles: true, cancelable: true}));
  doc.body.focus();
  q.blur();
  key(w, doc.body, 'ArrowDown');
  const back = [selected(doc), doc.activeElement === q];

  //then
  assert.deepStrictEqual(kept, [true, true, true], 'a press on the box outside the input keeps the focus in the input');
  assert.ok(inInput, 'a press in the input itself is left alone');
  assert.deepStrictEqual(back, [['po-1'], true], 'with the focus lost anyway, the arrows still move and the focus goes back to the input');

  //when
  doc.getElementById('pal-scrim').click();

  //then
  assert.strictEqual(q.getAttribute('aria-expanded'), 'false', 'a closed search shows no list');
  assert.deepStrictEqual(errors, []);
  await close();
  return 'Up and Down move aria-selected and aria-activedescendant, wrapping at the ends';
}

async function enterOpensTheCardOrThePage() {
  //given
  const {dom, w, doc, errors, close} = await boot();
  const q = () => doc.getElementById('pal-q');

  //when
  doc.getElementById('search-btn').click();
  type(w, doc, '#14');
  key(w, q(), 'Enter');
  await wait(50);
  const card = [doc.getElementById('pal').hidden, doc.querySelector('#anoms article.inc.linked') && doc.querySelector('#anoms article.inc.linked').dataset.key];
  doc.getElementById('search-btn').click();
  type(w, doc, 'latency');
  const coreMeta = options(doc, 'Pages').map((o) => o.querySelector('.pal-m').textContent);
  key(w, q(), 'Enter');
  const page = [doc.getElementById('pal').hidden, doc.getElementById('panel-lat').hidden, doc.getElementById('tab-lat').getAttribute('aria-current')];
  doc.getElementById('search-btn').click();
  type(w, doc, 'shop');
  const shop = rows(doc, 'Services').indexOf('shop');
  for (let i = 0; i < rows(doc, 'Cards').length + shop; i++) key(w, q(), 'ArrowDown');
  key(w, q(), 'Enter');
  const svc = [doc.getElementById('pal').hidden, !doc.getElementById('panel-anoms').hidden, [...doc.querySelectorAll('#anoms article.inc')].map((c) => c.dataset.key)];
  doc.getElementById('search-btn').click();
  type(w, doc, 'demo');
  const plugin = rows(doc, 'Pages');
  const pluginMeta = options(doc, 'Pages').map((o) => o.querySelector('.pal-m').textContent);
  doc.querySelector('#pal-list [role="option"]').click();

  //then
  assert.deepStrictEqual(card, [true, KEY], 'Enter on the card opens it on the board');
  assert.deepStrictEqual(page, [true, false, 'page'], 'Enter on a page shows that panel and marks its row');
  assert.deepStrictEqual(svc, [true, true, ['log|shop|ERROR|npe']], 'a service shows its cards');
  assert.deepStrictEqual(plugin, ['Demo board'], 'plug-in rows are pages too');
  assert.deepStrictEqual(pluginMeta, ['plug-in'], 'and say they come from a plug-in');
  assert.deepStrictEqual(coreMeta, [''], 'a core page says nothing of the kind');
  assert.ok(!doc.getElementById('panel-demo').hidden, 'a click opens a row as Enter does');
  assert.deepStrictEqual(errors, []);
  await close();
  return 'Enter on a card opens it, on a page shows that panel, on a service shows its cards; plug-in pages are listed and a click works as Enter';
}

async function pagesAreTheVisibleSidebarRows() {
  //given
  const viewer = await boot({me: VIEWER});
  const admin = await boot();

  //when
  viewer.doc.getElementById('search-btn').click();
  type(viewer.w, viewer.doc, 'chat');
  const noChat = rows(viewer.doc, 'Pages');
  type(viewer.w, viewer.doc, 'docs');
  const docs = rows(viewer.doc, 'Pages');
  admin.doc.getElementById('search-btn').click();
  type(admin.w, admin.doc, 'admin');
  const adminRow = rows(admin.doc, 'Pages');

  //then
  assert.deepStrictEqual(noChat, [], 'a hidden row is not a page');
  assert.deepStrictEqual(docs, ['Docs'], 'a row inside the folded More is');
  assert.ok(viewer.doc.getElementById('side-more-list').classList.contains('folded'), 'More stays folded');
  assert.deepStrictEqual(adminRow, ['Admin'], 'the pinned Admin row is a page for an admin');
  assert.deepStrictEqual(viewer.errors.concat(admin.errors), []);
  await viewer.close(); await admin.close();
  return 'Pages are the visible sidebar rows, folded More rows and pinned rows included';
}

async function aViewerGetsNoAdminSettingsAndAnAdminGetsThemAfterOneFetch() {
  //given
  const viewer = await boot({me: VIEWER});
  const admin = await boot();
  const fetches = (x) => x.seen.filter((p) => p.startsWith('/api/admin/settings')).length;
  const before = [fetches(viewer), fetches(admin)];

  //when
  viewer.doc.getElementById('search-btn').click();
  type(viewer.w, viewer.doc, 'model');
  const viewerRows = rows(viewer.doc, 'Settings');
  type(viewer.w, viewer.doc, 'appearance');
  const viewerOwn = rows(viewer.doc, 'Settings');
  admin.doc.getElementById('search-btn').click();
  await wait(50);
  type(admin.w, admin.doc, 'model');
  const adminRows = rows(admin.doc, 'Settings');
  const meta = admin.doc.querySelector('#pal-list [role="option"] .pal-m').textContent;
  admin.doc.getElementById('pal-scrim').click();
  admin.doc.getElementById('search-btn').click();
  await wait(50);
  type(admin.w, admin.doc, 'access');
  const sections = rows(admin.doc, 'Settings');
  const after = [fetches(viewer), fetches(admin)];

  //then
  assert.deepStrictEqual(viewerRows, [], 'a viewer gets no admin settings');
  assert.deepStrictEqual(viewerOwn, ['Appearance'], 'but the personal Settings sections');
  assert.deepStrictEqual(adminRows, ['Card model'], 'an admin finds every admin setting by its label');
  assert.strictEqual(meta, 'Admin \u00b7 Investigator');
  assert.deepStrictEqual(sections, ['Access & sign-in'], 'and the Admin sections, under their Admin labels');
  assert.deepStrictEqual([after[0] - before[0], after[1] - before[1]], [0, 1], 'fetched once, the first time the palette opens');

  //when
  type(admin.w, admin.doc, 'card model');
  key(admin.w, admin.doc.getElementById('pal-q'), 'Enter');
  await wait(150);

  //then
  assert.ok(!admin.doc.getElementById('panel-ai').hidden, 'picking one opens its admin tab, Investigator for an Investigator setting');
  assert.strictEqual(admin.w.ADMIN_FIND, '', 'Investigator ignores the admin search, so none is left set');
  assert.deepStrictEqual([...admin.doc.querySelectorAll('#ai-rows .adm-row')].map((r) => r.dataset.key), ['investigate.model'], 'where its row lives');

  //when
  admin.doc.getElementById('search-btn').click();
  await wait(50);
  type(admin.w, admin.doc, 'timezone');
  key(admin.w, admin.doc.getElementById('pal-q'), 'Enter');
  await wait(150);

  //then
  const find = admin.doc.getElementById('adm-find');
  assert.ok(!admin.doc.getElementById('panel-settings').hidden, 'a General setting opens General');
  assert.ok(find && find.value === 'timezone', 'with the search filled');
  assert.deepStrictEqual([...admin.doc.querySelectorAll('#admin .adm-row:not([hidden])')].map((r) => r.dataset.key), ['general.timezone']);
  assert.deepStrictEqual(viewer.errors.concat(admin.errors), []);
  await viewer.close(); await admin.close();
  return 'a viewer gets no admin settings; an admin gets every admin setting after one fetch, and picking one opens the admin tab where it lives, General with the search filled';
}

async function theActionsWork() {
  //given
  const admin = await boot();
  const sso = await boot({me: VIEWER});
  const run = async (x, text) => { x.doc.getElementById('search-btn').click(); type(x.w, x.doc, text); const r = rows(x.doc, 'Actions'); option(x.doc, 'Actions').click(); await wait(30); return r; };

  //when
  const theme = await run(admin, 'theme');
  const mode = JSON.parse(admin.w.localStorage.getItem('skopax-appearance') || '{}').mode;
  const rail = await run(admin, 'collapse');
  const railed = admin.doc.body.classList.contains('is-rail');
  const pw = await run(admin, 'password');
  const dialog = admin.doc.getElementById('pw-dialog');
  sso.doc.getElementById('search-btn').click();
  type(sso.w, sso.doc, 'password');
  const ssoPw = rows(sso.doc, 'Actions');

  //then
  assert.deepStrictEqual([theme, mode], [['Switch theme'], 'light'], 'Switch theme steps the theme');
  assert.deepStrictEqual([rail, railed, admin.w.localStorage.getItem('skopax-sidebar')], [['Collapse or expand sidebar'], true, 'rail'], 'the sidebar folds to the rail');
  assert.deepStrictEqual(pw, ['Change password']);
  assert.ok(dialog && admin.doc.activeElement === dialog.querySelector('input[name="current"]'), 'Change password opens the password dialog');
  assert.ok(!admin.doc.getElementById('panel-prefs').hidden, 'over the Settings page, where the focus goes back');
  assert.deepStrictEqual(ssoPw, [], 'an SSO user has no Change password');
  assert.deepStrictEqual(admin.errors.concat(sso.errors), []);
  await admin.close(); await sso.close();
  return 'the actions switch the theme, fold the sidebar and open Change password for local users';
}

async function hostileTextIsEscaped() {
  //given
  const viewer = await boot({me: VIEWER});
  const {dom, w, doc, errors, close} = await boot();
  viewer.doc.getElementById('search-btn').click();
  doc.getElementById('search-btn').click();
  await wait(50);

  //when
  type(viewer.w, viewer.doc, '  ' + EVIL + ' ');

  //then
  const empty = viewer.doc.querySelector('#pal-list .pal-empty');
  assert.ok(empty, 'nothing matches');
  assert.strictEqual(empty.textContent, 'Nothing matches "' + EVIL + '".', 'the query is shown as text');
  assert.strictEqual(viewer.doc.querySelectorAll('#pal img').length, 0);
  assert.strictEqual(viewer.doc.getElementById('pal-q').hasAttribute('aria-activedescendant'), false);

  //when
  type(w, doc, 'kaboom');

  //then
  const lbl = doc.querySelector('#pal-list [role="option"] .lbl');
  assert.strictEqual(lbl.textContent, '#16 cart-service <b>kaboom</b> & <i>', 'card text with markup stays text');
  assert.strictEqual(lbl.innerHTML, '#16 cart-service &lt;b&gt;<mark>kaboom</mark>&lt;/b&gt; &amp; &lt;i&gt;', 'escaped around and inside the highlight');
  assert.strictEqual(doc.querySelectorAll('#pal-list b, #pal-list i').length, 0);

  //when
  type(w, doc, 'onerror');

  //then
  assert.deepStrictEqual(rows(doc, 'Settings'), [EVIL], 'a hostile setting label is text too');
  assert.strictEqual(doc.querySelectorAll('#pal img').length, 0);
  assert.deepStrictEqual(errors.concat(viewer.errors), []);
  await close(); await viewer.close();
  return 'a hostile query, card text and setting label render as text, with the highlight inside the escaping';
}

async function aDisabledRowIsNotAPage() {
  //given
  const guest = await boot({me: {loginEnabled: true, user: null, userRole: null, source: null, canChangePassword: false, operator: false, admin: false, teams: [], tags: [], chat: false, instances: {current: {name: 'production', letter: 'P'}, links: []}}});
  const signed = await boot({me: VIEWER});

  //when
  guest.doc.getElementById('search-btn').click();
  type(guest.w, guest.doc, 'costs');
  signed.doc.getElementById('search-btn').click();
  type(signed.w, signed.doc, 'costs');

  //then
  assert.strictEqual(guest.doc.getElementById('tab-costs').getAttribute('aria-disabled'), 'true', 'a guest sees Costs greyed out');
  assert.deepStrictEqual(rows(guest.doc, 'Pages'), [], 'so it is not offered as a page');
  assert.deepStrictEqual(rows(signed.doc, 'Pages'), ['Costs'], 'a signed-in reader gets it');
  assert.deepStrictEqual(guest.errors.concat(signed.errors), []);
  await guest.close(); await signed.close();
  return 'a sidebar row disabled for the reader, such as Costs for a guest, is not offered as a page';
}

async function aPaletteJumpLeavesNoStaleAdminFilter() {
  //given
  const {dom, w, doc, errors, close} = await boot();
  const pick = async (text, group) => { doc.getElementById('search-btn').click(); await wait(30); type(w, doc, text); option(doc, group).click(); await wait(150); };
  const shownRows = () => [...doc.querySelectorAll('#admin .adm-row:not([hidden])')].map((r) => r.dataset.key);

  const generalKeys = ADMIN_ITEMS.filter((i) => i.group !== 'Investigator').map((i) => i.key);

  //when
  await pick('timezone', 'Settings');
  const filtered = [doc.getElementById('adm-find').value, shownRows()];
  await pick('access', 'Settings');
  const access = !doc.getElementById('panel-access').hidden;
  doc.getElementById('tab-settings').click();
  await wait(150);
  const general = [doc.getElementById('adm-find').value, shownRows()];
  await pick('timezone', 'Settings');
  await pick('admin', 'Pages');
  const viaRow = [doc.getElementById('adm-find').value, shownRows(), !doc.getElementById('panel-settings').hidden];
  await pick('timezone', 'Settings');
  await pick('card model', 'Settings');
  const viaSetting = [doc.getElementById('adm-find').value, shownRows(), w.ADMIN_FIND, !doc.getElementById('panel-ai').hidden];

  //then
  assert.deepStrictEqual(filtered, ['timezone', ['general.timezone']], 'a setting opens General searched for it');
  assert.ok(access, 'another admin section opens');
  assert.deepStrictEqual(general, ['', generalKeys], 'and General is no longer filtered when the reader comes back');
  assert.deepStrictEqual(viaRow, ['', generalKeys, true], 'the Admin page from the search opens the last section, General here, unfiltered too');
  assert.deepStrictEqual(viaSetting, ['', generalKeys, '', true], 'a setting that lives on a tab without the search clears the box and the filter too');
  assert.deepStrictEqual(errors, []);
  await close();
  return 'a search jump to another admin tab clears the admin search, so General is never left filtered by an earlier jump';
}

function realSettings() {
  const src = (f) => fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', f), 'utf8');
  const tunables = src('tunables.py'), features = src('features.py'), tags = src('tags.py');
  const constant = (text, name) => new RegExp('^' + name + " = '([^']+)'", 'm').exec(text)[1];
  const names = /^(\w+(?:, \w+)+) = \(([^)]*)\)/m.exec(tunables);
  const groups = {};
  names[1].split(', ').forEach((n, i) => { groups[n] = names[2].split(',').map((x) => x.trim().replace(/^'|'$/g, ''))[i]; });
  groups.MCP = constant(tunables, 'MCP');
  const table = [...features.matchAll(/Feature\('([a-z-]+)', '([a-z.-]+)', '([^']+)',\s*'([^']*)', '([a-z]+)'(?:, edition='([a-z]+)')?/g)].map((m) => ({id: m[1], key: m[2], label: m[3], help: m[4], icon: m[5], edition: m[6] || 'community'}));
  const block = tunables.slice(tunables.indexOf('SIGN_IN_ROLES = ('), tunables.indexOf('CONFIG_KEY'));
  const declared = (tunables.match(/^\s+(?:T|feature_tunable)\(/gm) || []).length;
  const item = (k, group, label) => ({key: k, group, label, help: label + ' help', kind: 'text', live: true, value: '', base: '', default: '', source: 'default', by: null, at: null});
  const items = [...block.matchAll(/^\s+T\((?:'([a-z0-9.-]+)'|([A-Z_]+)), '\w+', ([A-Z]+), '([^']*)'/gm)].map((m) => item(m[1] || constant(tunables, m[2]), groups[m[3]], m[4]))
    .concat([...block.matchAll(/feature_tunable\('([a-z0-9.-]+)'/g)].map((m) => item(m[1], groups.FEATURES, table.find((f) => f.key === m[1]).label)));
  const tagSet = /^TAGS: dict\[str, str\] = \{([^}]*)\}/m.exec(tags)[1];
  [...tagSet.matchAll(/(\w+): '([^']+)'/g)].forEach((m) => items.push(item('tags.' + constant(tags, m[1]) + '.rules', groups.TAGS, m[2])));
  const tagCount = (tagSet.match(/:/g) || []).length;
  const feats = table.map((f) => ({id: f.id, key: f.key, label: f.label, help: f.help, icon: f.icon, on: true, stored: true, locked: f.edition !== 'community', edition: f.edition, needs: [], restart: null, source: 'default', reason: null}));
  const tabOf = (i) => i.group === groups.INV ? 'ai' : i.group === groups.SIGNIN || i.group === groups.MCP ? 'access' : i.group === groups.FEATURES || table.some((f) => f.key === i.key) ? 'features' : 'settings';
  const metaOf = (i) => 'Admin \u00b7 ' + ({ai: 'Investigator', features: 'Features', access: 'Access & sign-in'}[tabOf(i)] || i.group);
  return {items, feats, groups, declared: declared + tagCount, tabOf, metaOf};
}

async function aSettingPickOpensTheAdminTabWhereItsRowLives() {
  //given
  const {items, feats, groups, declared, metaOf} = realSettings();
  const {dom, w, doc, errors, close} = await boot({items, features: feats});
  const shownTab = () => w.ADMIN_TABS.filter((t) => !doc.getElementById('panel-' + t).hidden);
  const pick = async (i) => {
    doc.getElementById('search-btn').click();
    await wait(20);
    type(w, doc, i.key);
    const hits = options(doc, 'Settings').filter((o) => o.querySelector('.lbl').textContent === i.label && o.querySelector('.pal-m').textContent === metaOf(i));
    assert.strictEqual(hits.length, 1, 'one Settings row for ' + i.key);
    hits[0].click();
    await wait(60);
  };
  const lives = (i) => {
    const feat = feats.find((f) => f.key === i.key);
    if (i.group !== groups.INV && feat) return [...doc.querySelectorAll('#features .feat[data-id="' + feat.id + '"]')].find((c) => !c.closest('[id^="panel-"]').hidden);
    return [...doc.querySelectorAll('.adm-row[data-key="' + i.key + '"], #access [data-key="' + i.key + '"]')].find((r) => !r.closest('[id^="panel-"]').hidden);
  };
  const seen = {};
  const fresh = await boot({items, features: feats});
  fresh.doc.getElementById('search-btn').click();
  await wait(50);
  type(fresh.w, fresh.doc, 'access.public-read');
  const unloaded = fresh.w.FEATS;
  options(fresh.doc, 'Settings')[0].click();
  await wait(60);
  const firstPick = [unloaded, !fresh.doc.getElementById('panel-features').hidden, fresh.errors];
  await fresh.close();

  //then
  assert.deepStrictEqual(firstPick, [null, true, []], 'a feature switch picked from the board, before any admin tab has loaded the features, opens Features');

  //given
  const slow = await boot({items, features: feats, slowFeatures: 400});

  //when
  slow.doc.getElementById('search-btn').click();
  await wait(80);
  type(slow.w, slow.doc, 'access.public-read');
  const early = rows(slow.doc, 'Settings');
  await wait(450);
  const late = rows(slow.doc, 'Settings');
  options(slow.doc, 'Settings')[0].click();
  await wait(60);
  const slowPick = [early, late, !slow.doc.getElementById('panel-features').hidden, slow.w.ADMIN_FIND, slow.errors];
  await slow.close();

  //then
  assert.deepStrictEqual(slowPick, [[], ['Public read'], true, '', []], 'while the features list is still on its way no admin setting is offered, so none can open the wrong tab; once it arrives, public read opens Features');

  //when
  for (const i of items) {
    await pick(i);
    const tab = shownTab(), row = lives(i), box = doc.getElementById('adm-find');
    seen[tab.join()] = (seen[tab.join()] || 0) + 1;

    //then
    assert.strictEqual(tab.length, 1, i.key + ' opens one admin tab');
    assert.ok(row, i.key + ' opens ' + tab[0] + ', where its row or card is shown');
    if (tab[0] === 'settings') {
      assert.ok(!row.hidden && box.value === i.label.toLowerCase() && w.ADMIN_FIND === i.label.toLowerCase(), i.key + ' opens General searched for its label');
    } else {
      assert.ok(w.ADMIN_FIND === '' && (!box || box.value === ''), i.key + ' leaves no admin search behind on a tab that ignores it');
      assert.ok(!box || [...doc.querySelectorAll('#admin .adm-row')].every((r) => !r.hidden), 'and General is not left filtered');
      const fold = row.closest('details');
      assert.ok(row.classList.contains('adm-hit') && doc.activeElement === row && w.__scrolled === row && (!fold || fold.open), i.key + ' is scrolled to, highlighted and focused, with its fold open');
    }
  }
  const tabOf = async (k) => { await pick(items.find((i) => i.key === k)); return shownTab()[0]; };
  const picked = ['sso.roles-claim', 'sso.role-map', 'sso.default-role'].concat(items.filter((i) => i.group === groups.MCP).map((i) => i.key)).filter((k) => items.some((i) => i.key === k));
  await pick(items.find((i) => i.key === 'mcp.audience'));
  const mcpRow = doc.activeElement;
  const mcpLanded = !!(mcpRow && mcpRow.classList.contains('adm-row') && mcpRow.dataset.key === 'mcp.audience' && mcpRow.closest('#acc-mcp') && !doc.getElementById('panel-access').hidden);
  const named = [await tabOf('access.public-read'), await tabOf('investigate.model'), await tabOf('core.release-settle'), await tabOf('sso.issuer'), await tabOf('general.timezone')];
  const timezone = [doc.getElementById('adm-find').value, [...doc.querySelectorAll('#admin .adm-row:not([hidden])')].map((r) => r.dataset.key)];

  //then
  assert.ok(declared > 0 && items.length === declared, 'every built-in setting that tunables.py and tags.py declare is picked: ' + items.length + ' of ' + declared);
  assert.deepStrictEqual(Object.keys(seen).sort(), ['access', 'ai', 'features', 'settings'], 'they land on General, Features, Investigator and Access & sign-in');
  assert.strictEqual(picked.length, 10, 'the role settings and the seven MCP settings are among the picks: ' + picked.join(', '));
  assert.ok(mcpLanded, 'Cmd-K to an MCP setting lands on its row on the MCP card of Access & sign-in');
  assert.deepStrictEqual(named, ['features', 'ai', 'features', 'access', 'settings'], 'public read opens Features, an Investigator setting opens Investigator, an Other switches row opens Features, a sign-in setting opens Access & sign-in and a General setting opens General');
  assert.deepStrictEqual(timezone, ['timezone', ['general.timezone']], 'General is filtered to that row');
  assert.deepStrictEqual(errors, []);
  await close();
  return 'every built-in setting opens the admin tab where 8b shows it: feature switches and Other switches on Features, Investigator settings on Investigator, sign-in settings on Access & sign-in, the rest on General filtered to the row';
}

async function theFeaturesListIsAskedOnceForAdminsAndAgainAfterAFailure() {
  //given
  const {items, feats} = realSettings();
  const asks = (x) => x.seen.filter((p) => p.startsWith('/api/admin/features')).length;
  const open = async (x) => { x.doc.getElementById('search-btn').click(); await wait(40); };
  const shut = (x) => x.doc.getElementById('pal-scrim').click();
  const publicRead = async (x, ready) => {
    type(x.w, x.doc, 'access.public-read');
    const found = options(x.doc, 'Settings').map((o) => [o.querySelector('.lbl').textContent, o.querySelector('.pal-m').textContent]);
    if (ready) ready();
    if (found.length) options(x.doc, 'Settings')[0].click();
    await wait(80);
    return found;
  };
  const landed = (x) => {
    const tab = x.w.ADMIN_TABS.filter((t) => !x.doc.getElementById('panel-' + t).hidden)[0];
    const card = x.doc.querySelector('#features .feat[data-id="public-read"]');
    const row = x.doc.querySelector('#admin .adm-row[data-key="access.public-read"]');
    return [tab, tab === 'features' ? !!card && !card.closest('[id^="panel-"]').hidden : !!row && !row.hidden];
  };
  const viewer = await boot({me: VIEWER, items, features: feats});
  const admin = await boot({items, features: feats});
  const down = await boot({items, features: feats});
  const flaky = await boot({items, features: feats});
  const again = await boot({items, features: feats});
  const union = await boot({items, features: feats});
  const busy = await boot({items, features: feats, slowFeatures: 300});
  down.srv.down = 403;
  flaky.srv.failNext = 1;
  again.srv.failNext = 1;
  union.srv.down = 500;

  //when
  await open(viewer); shut(viewer); await open(viewer); shut(viewer);
  await open(admin); shut(admin); await open(admin); shut(admin);
  await open(again); shut(again);
  await open(busy); shut(busy); await open(busy);
  const sharedNow = asks(busy);
  await wait(400);
  const onBoard = [asks(viewer), asks(admin), asks(again), sharedNow, asks(busy)];
  await open(again);
  const retried = asks(again);
  const againRows = await publicRead(again);
  await open(down);
  const downRows = await publicRead(down);
  await open(flaky);
  const flakyRows = await publicRead(flaky);
  await open(union); shut(union);
  union.srv.down = 0;
  union.doc.getElementById('tab-features').click();
  await wait(80);
  union.srv.down = 500;
  await open(union);
  const unionAsks = asks(union);
  const unionRows = await publicRead(union, () => { union.srv.down = 0; });

  //then
  assert.deepStrictEqual(onBoard, [0, 1, 1, 1, 1], 'on the board a viewer never asks for the features list; an admin asks once across two opens after a success, once on a failed first open, and once for two opens while the answer is on its way');
  assert.strictEqual(retried, 2, 'after a failed answer the next open asks again');
  assert.deepStrictEqual([againRows, landed(again)], [[['Public read', 'Admin \u00b7 Features']], ['features', true]], 'once the retry lands, public read names Features and opens it on its card');
  assert.deepStrictEqual([downRows, landed(down), down.doc.getElementById('adm-find').value], [[['Public read', 'Admin \u00b7 Access']], ['settings', true], 'public read'], 'with the list refused every time, the admin settings are still offered, and public read opens General, where its row is shown');
  assert.deepStrictEqual([flakyRows, landed(flaky)], [[['Public read', 'Admin \u00b7 Access']], ['features', true]], 'a pick right after a failed answer asks again before it routes, so public read opens Features, never an empty General');
  assert.deepStrictEqual([unionAsks, unionRows, landed(union)], [3, [['Public read', 'Admin \u00b7 Features']], ['features', true]], 'when only the Admin page has the list (the search asked twice and was refused twice), the search names Features and routes by it too');
  assert.deepStrictEqual([viewer, admin, down, flaky, again, union, busy].flatMap((x) => x.errors), []);
  for (const x of [viewer, admin, down, flaky, again, union, busy]) await x.close();
  return 'the search asks for the features list only for admins, once after a success and again after a failure, never opens an empty General, and falls back to General with the row shown when the list is refused';
}

async function aPickOnFeaturesOrInvestigatorShowsItsTarget() {
  //given
  const {items, feats} = realSettings();
  const {w, doc, errors, close} = await boot({items, features: feats});
  const pick = async (text, label) => {
    doc.getElementById('search-btn').click();
    await wait(30);
    type(w, doc, text);
    const o = options(doc, 'Settings').find((x) => x.querySelector('.lbl').textContent === label);
    assert.ok(o, '"' + text + '" finds the ' + label + ' setting');
    const meta = o.querySelector('.pal-m').textContent;
    o.click();
    await wait(80);
    return meta;
  };
  const shown = (el) => !!el && !el.closest('[id^="panel-"]').hidden;

  //when
  const byCard = rows(await (async () => { doc.getElementById('search-btn').click(); await wait(30); type(w, doc, 'read without login'); return doc; })(), 'Settings');
  doc.getElementById('pal-scrim').click();
  const cardMeta = await pick('read without login', 'Public read');
  const card = doc.querySelector('#features .feat[data-id="public-read"]');
  const onCard = [cardMeta, shown(card), card.classList.contains('adm-hit'), doc.activeElement === card, w.__scrolled === card];
  const otherMeta = await pick('release settle', 'Release settle');
  const other = doc.querySelector('#feat-other .adm-row[data-key="core.release-settle"]');
  const onOther = [otherMeta, shown(other), other.closest('details').open, other.classList.contains('adm-hit'), doc.activeElement === other, w.__scrolled === other];
  const aiMeta = await pick('card model', 'Card model');
  const ai = doc.querySelector('#ai-rows .adm-row[data-key="investigate.model"]');
  const onAi = [aiMeta, shown(ai), ai.classList.contains('adm-hit'), doc.activeElement === ai, w.__scrolled === ai];
  w.ADMIN_HIT_MS = 400;
  await pick('release settle', 'Release settle');
  const lit = doc.querySelector('#feat-other .adm-row[data-key="core.release-settle"]');
  const litAtFirst = lit.classList.contains('adm-hit');
  await wait(450);
  const litLater = lit.classList.contains('adm-hit');
  await pick('timezone', 'Timezone');
  const general = [doc.querySelectorAll('#panel-settings .adm-hit').length, doc.getElementById('adm-find').value];

  //then
  assert.deepStrictEqual(byCard, ['Public read'], 'the card name as Features shows it finds the setting');
  assert.deepStrictEqual(onCard, ['Admin \u00b7 Features', true, true, true, true], 'public read names Features, and the pick scrolls to its card, highlights it and focuses the card itself, not its switch, so a held Enter cannot flip it');
  assert.deepStrictEqual(onOther, ['Admin \u00b7 Features', true, true, true, true, true], 'an Other switches row opens its fold and is scrolled to, highlighted and focused');
  assert.deepStrictEqual(onAi, ['Admin \u00b7 Investigator', true, true, true, true], 'an Investigator row is scrolled to, highlighted and focused');
  assert.deepStrictEqual([litAtFirst, litLater], [true, false], 'the highlight is short');
  assert.deepStrictEqual(general, [0, 'timezone'], 'General keeps its search and gets no highlight');
  const hitRule = [...doc.styleSheets].flatMap((x) => [...x.cssRules]).filter((r) => r.selectorText === '.adm-row.adm-hit, .feat.adm-hit').map((r) => [r.style.getPropertyValue('outline'), r.style.getPropertyValue('background-color')])[0];
  assert.deepStrictEqual(hitRule, ['2px solid var(--ring)', 'var(--active)'], 'the highlight is a ring and a tint');
  assert.deepStrictEqual(errors, []);
  await close();
  return 'a pick on Features or Investigator scrolls to its card or row, opens the Other switches fold when needed, highlights it briefly and focuses it; a feature setting names Features and is found by its card name';
}

async function theAdminPageListIsEnoughToListAndRouteFeatureSettings() {
  //given
  const {items, feats} = realSettings();
  const x = await boot({items, features: feats, hash: '#t=features', slowFeatures: 150});
  await wait(250);
  const asks = () => x.seen.filter((p) => p.startsWith('/api/admin/features')).length;
  x.srv.failNext = 1;

  //when
  x.doc.getElementById('search-btn').click();
  await wait(40);
  type(x.w, x.doc, 'read without login');
  const atOnce = options(x.doc, 'Settings').map((o) => [o.querySelector('.lbl').textContent, o.querySelector('.pal-m').textContent]);
  await wait(200);
  const before = asks();
  options(x.doc, 'Settings')[0].click();
  await wait(250);
  const atPick = asks() - before;
  const card = x.doc.querySelector('#features .feat[data-id="public-read"]');

  //then
  assert.deepStrictEqual(atOnce, [['Public read', 'Admin \u00b7 Features']], 'with the features list known from the Admin page, the search lists and names the feature setting while its own request is still on the way');
  assert.strictEqual(atPick, 1, 'and after its own request was refused, a pick routes by that list without asking again: the one request is the Features tab loading itself');
  assert.ok(card && card.classList.contains('adm-hit') && x.doc.activeElement === card, 'the pick lands on the Features card');
  assert.deepStrictEqual(x.errors, []);
  await x.close();
  return 'the features list the Admin page already holds is enough for the search to list a feature setting and route its pick, with no second request';
}

async function aLockedFeatureCardGetsTheHighlightTintToo() {
  //given
  const {items, feats} = realSettings();
  const {w, doc, errors, close} = await boot({items, features: feats});
  const pick = async (text, label) => {
    doc.getElementById('search-btn').click();
    await wait(30);
    type(w, doc, text);
    options(doc, 'Settings').find((o) => o.querySelector('.lbl').textContent === label).click();
    await wait(80);
  };
  const rules = [...doc.styleSheets].flatMap((x) => [...x.cssRules]);
  const background = (selector, prop) => rules.filter((r) => r.selectorText === selector).map((r) => r.style.getPropertyValue(prop));

  //when
  await pick('hide powered', feats.find((f) => f.id === 'hide-attribution').label);
  const locked = doc.querySelector('#features .feat[data-id="hide-attribution"]');

  //then
  assert.deepStrictEqual([locked.classList.contains('locked'), locked.classList.contains('adm-hit'), doc.activeElement === locked], [true, true, true], 'a locked card picked from the search is highlighted and focused');
  assert.deepStrictEqual([background('.feat.locked', 'background'), background('.feat.adm-hit', 'background-color'), background('.feat.locked.adm-hit', 'background-color')], [['var(--raised)'], [], ['var(--active)']],
    'the locked background is beaten by a rule of its own for the highlight, more specific than .feat.locked, with the same tint as every other highlight');
  assert.deepStrictEqual(errors, []);
  await close();
  return 'a locked feature card picked from the search gets the highlight tint, not only the ring';
}

async function aFailedTabLoadDropsThePendingHighlight() {
  //given
  const {items, feats} = realSettings();
  const x = await boot({items, features: feats});
  const pick = async (text, label) => {
    x.doc.getElementById('search-btn').click();
    await wait(30);
    type(x.w, x.doc, text);
    await wait(40);
    options(x.doc, 'Settings').find((o) => o.querySelector('.lbl').textContent === label).click();
    await wait(80);
  };
  const reopen = async (id) => {
    x.doc.getElementById('tab-changes').click();
    await wait(60);
    x.doc.getElementById('tab-' + id).click();
    await wait(80);
  };

  //when
  x.srv.down = 500;
  await pick('release settle', 'Release settle');
  const failed = x.doc.querySelector('#features').textContent;
  x.srv.down = 0;
  await reopen('features');
  const row = x.doc.querySelector('#feat-other .adm-row[data-key="core.release-settle"]');
  const features = [!!row, x.doc.querySelectorAll('#panel-features .adm-hit').length, x.doc.activeElement === row];
  x.srv.keyDown = 500;
  await pick('card model', 'Card model');
  x.srv.keyDown = 0;
  await reopen('ai');
  const ai = x.doc.querySelector('#ai-rows .adm-row[data-key="investigate.model"]');
  const investigator = [!!ai, x.doc.querySelectorAll('#panel-ai .adm-hit').length, x.doc.activeElement === ai];

  //then
  assert.match(failed, /Features did not load/);
  assert.deepStrictEqual(features, [true, 0, false], 'once the Features load after a pick has failed, a later ordinary visit neither highlights nor focuses the old target');
  assert.deepStrictEqual(investigator, [true, 0, false], 'the same on Investigator');
  assert.deepStrictEqual(x.errors, []);
  await x.close();
  return 'a pick whose admin tab fails to load leaves no highlight armed for a later ordinary visit, on Features and on Investigator';
}

async function theSearchDoesNotOpenOverTheConfirmDialog() {
  //given
  const {w, doc, errors, close} = await boot();
  const pal = doc.getElementById('pal'), wrap = doc.getElementById('cf-wrap');
  let answer = 'pending';
  w.__skopaxConfirm('Remove the key?', 'It is deleted.', 'Remove').then((v) => { answer = v; });
  await wait(10);

  //when
  key(w, doc.getElementById('cf-yes'), 'k', {ctrlKey: true});
  const byKey = pal.hidden;
  doc.getElementById('search-btn').click();
  const byButton = pal.hidden;
  key(w, doc.getElementById('cf-yes'), 'Escape');
  await wait(10);
  key(w, doc.body, 'k', {ctrlKey: true});
  const afterwards = !pal.hidden;
  key(w, doc.getElementById('pal-q'), 'Escape');

  //then
  assert.deepStrictEqual([byKey, byButton], [true, true], 'while the confirm dialog waits for an answer, neither Ctrl+K nor the search button opens the search over it');
  assert.deepStrictEqual([wrap.hidden, answer], [true, false], 'Esc still answers the dialog');
  assert.ok(afterwards, 'and the search opens again once it is closed');
  assert.deepStrictEqual(errors, []);
  await close();
  return 'the search does not open over the confirm dialog, by key or by button, and opens again once the dialog is answered';
}

async function theSearchDoesNotOpenOverAnyModalDialog() {
  //given
  const {w, doc, errors, close} = await boot();
  const pal = doc.getElementById('pal');
  const card = w.CARDS.find((a) => a.key === KEY);
  const esc = () => key(w, doc.activeElement || doc.body, 'Escape');
  const link = (attr, href) => { const a = doc.createElement('a'); a.setAttribute(attr, 'Probe'); a.href = href; doc.body.appendChild(a); a.click(); a.remove(); };
  let extra = null;
  const kinds = [
    ['ask', () => w.askDialog(), esc],
    ['task', () => { doc.getElementById('task-wrap').hidden = false; }, () => { doc.getElementById('task-wrap').hidden = true; }],
    ['mute', () => { w.muteDialog(card); }, esc],
    ['resolve', () => { w.resolveDialog(card); }, esc],
    ['JSON preview', () => link('data-json', '/api/cards/14'), () => doc.getElementById('json-close').click()],
    ['screenshot preview', () => link('data-shot', '/shots/s1.png'), () => doc.getElementById('shot-close').click()],
    ['Change password', () => doc.getElementById('pf-pw').click(), () => doc.querySelector('#pw-dialog .pw-cancel').click()],
    ['card drawer', () => w.openDrawer(KEY), () => w.closeDrawer()],
    ['an open dialog element', () => { extra = doc.createElement('dialog'); extra.setAttribute('open', ''); doc.body.appendChild(extra); }, () => extra.remove()],
    ['any later aria-modal dialog', () => { extra = doc.createElement('div'); extra.setAttribute('role', 'dialog'); extra.setAttribute('aria-modal', 'true'); doc.body.appendChild(extra); }, () => extra.remove()],
    ['a bare modal overlay', () => { extra = doc.createElement('div'); extra.className = 'modal-wrap'; doc.body.appendChild(extra); }, () => extra.remove()],
  ];
  const rows = [];

  //when
  for (const [name, show, shut] of kinds) {
    show();
    await wait(20);
    const opened = !!doc.querySelector('[aria-modal="true"]:not(#pal [aria-modal]), dialog[open], .modal-wrap:not([hidden])');
    key(w, doc.activeElement || doc.body, 'k', {ctrlKey: true});
    const byKey = pal.hidden;
    doc.getElementById('search-btn').click();
    const byButton = pal.hidden;
    if (!pal.hidden) key(w, doc.getElementById('pal-q'), 'Escape');
    shut();
    await wait(20);
    key(w, doc.body, 'k', {ctrlKey: true});
    const after = !pal.hidden;
    if (after) key(w, doc.getElementById('pal-q'), 'Escape');
    rows.push([name, opened, byKey, byButton, after]);
  }

  //then
  assert.deepStrictEqual(rows, kinds.map((k) => [k[0], true, true, true, true]), 'over every kind of modal dialog neither Ctrl+K nor the search button opens the search, and it opens again once the dialog is closed');
  assert.deepStrictEqual(errors, []);
  await close();
  return 'the search never opens while a modal dialog is open: ask, task, mute, resolve, the JSON and screenshot previews, Change password, the card drawer, an open dialog element, any later aria-modal dialog or a bare modal overlay';
}

(async () => {
  console.log('PASS ' + await ctrlKOpensTheSearchAndEscRestoresTheFocus());
  console.log('PASS ' + await anEmptyQueryShowsOpenCardsAndPages());
  console.log('PASS ' + await checkoutFindsTheCardAndTheService());
  console.log('PASS ' + await downMovesTheSelection());
  console.log('PASS ' + await enterOpensTheCardOrThePage());
  console.log('PASS ' + await pagesAreTheVisibleSidebarRows());
  console.log('PASS ' + await aViewerGetsNoAdminSettingsAndAnAdminGetsThemAfterOneFetch());
  console.log('PASS ' + await theActionsWork());
  console.log('PASS ' + await hostileTextIsEscaped());
  console.log('PASS ' + await aDisabledRowIsNotAPage());
  console.log('PASS ' + await aPaletteJumpLeavesNoStaleAdminFilter());
  console.log('PASS ' + await aSettingPickOpensTheAdminTabWhereItsRowLives());
  console.log('PASS ' + await theFeaturesListIsAskedOnceForAdminsAndAgainAfterAFailure());
  console.log('PASS ' + await aPickOnFeaturesOrInvestigatorShowsItsTarget());
  console.log('PASS ' + await theAdminPageListIsEnoughToListAndRouteFeatureSettings());
  console.log('PASS ' + await aLockedFeatureCardGetsTheHighlightTintToo());
  console.log('PASS ' + await aFailedTabLoadDropsThePendingHighlight());
  console.log('PASS ' + await theSearchDoesNotOpenOverTheConfirmDialog());
  console.log('PASS ' + await theSearchDoesNotOpenOverAnyModalDialog());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
