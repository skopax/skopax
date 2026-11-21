const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const KEY = 'skopax-appearance';
const LOCAL = {loginEnabled: true, sso: false, user: 'alice.archer@example.com', name: 'Alice A. Archer', userRole: 'admin', source: 'local', canChangePassword: true, operator: true, admin: true, teams: [], tags: [], chat: true};

function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(opts) {
  opts = opts || {};
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const me = opts.me || LOCAL;
  const dom = new JSDOM(pageHtml(TEMPLATE, opts.brand ? {brand: opts.brand} : undefined).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state())), {
    url: 'https://skopax.example.com' + (opts.path || '/') + (opts.hash || '#t=prefs'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      if (opts.packs) w.SKOPAX_I18N = opts.packs;
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      Object.keys(opts.stored || {}).forEach((k) => w.localStorage.setItem(k, opts.stored[k]));
      w.fetch = async (path, o) => {
        const p = String(path), method = (o && o.method) || 'GET';
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p === '/logout') { calls.push([method, p]); return ok({}); }
        if (p.startsWith('/api/me')) return ok(me);
        if (p.startsWith('/api/server')) return ok({plugins: [], tz: 'UTC'});
        if (p.startsWith('/api/notifications/count')) return ok({unread: 0});
        if (p.startsWith('/api/admin/settings')) return ok({items: [], rev: 1, refreshSeconds: 15});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
      };
    },
  });
  await wait(300);
  return {dom, w: dom.window, doc: dom.window.document, errors, calls};
}

const stored = (w) => w.localStorage.getItem(KEY);
const pressed = (doc) => [...doc.querySelectorAll('#panel-prefs [data-ap]')].map((g) => g.dataset.ap + ':' + [...g.querySelectorAll('button[aria-pressed="true"]')].map((b) => b.dataset.v).join(','));
const pick = (doc, group, v) => doc.querySelector('#panel-prefs [data-ap="' + group + '"] button[data-v="' + v + '"]').click();

async function eachControlWritesTheAppearanceAsTheOldPanelDid() {
  //given
  const {dom, w, doc, errors} = await boot();
  const root = doc.documentElement;
  const panel = doc.getElementById('panel-prefs');

  //then
  assert.ok(!panel.hidden, '#t=prefs opens the personal Settings page');
  assert.strictEqual(doc.getElementById('crumb-page').textContent, 'Preferences');
  assert.deepStrictEqual(['ap-panel', 'ap-open', 'ap-backdrop', 'ap-close'].map((id) => doc.getElementById(id)), [null, null, null, null], 'the Appearance panel and its opener are gone');
  assert.deepStrictEqual([...panel.querySelectorAll('[data-ap]')].map((g) => [g.dataset.ap, g.getAttribute('role'), g.getAttribute('aria-label'), [...g.querySelectorAll('button')].map((b) => b.dataset.v + '=' + b.textContent)]), [
    ['size', 'group', 'Text size', ['sm=S', 'md=M', 'lg=L']],
    ['mode', 'group', 'Mode', ['light=Light', 'dark=Dark', 'system=System']],
    ['density', 'group', 'Density', ['comfortable=Comfortable', 'compact=Compact']],
    ['lang', 'group', 'Language', ['auto=Auto', 'en=English']]], 'the four segmented controls moved with their data-ap attributes');
  assert.strictEqual(panel.querySelector('h3').textContent, 'Appearance');
  assert.deepStrictEqual(pressed(doc), ['size:md', 'mode:system', 'density:comfortable', 'lang:auto'], 'nothing stored shows the defaults');
  assert.strictEqual(stored(w), null);

  //when
  pick(doc, 'size', 'lg');
  const afterSize = stored(w);
  pick(doc, 'mode', 'dark');
  const afterMode = stored(w);
  pick(doc, 'density', 'compact');
  const afterDensity = stored(w);

  //then
  assert.strictEqual(afterSize, '{"size":"lg","mode":"system","density":"comfortable","lang":"auto"}', 'a pick stores the whole appearance, defaults first');
  assert.strictEqual(afterMode, '{"size":"lg","mode":"dark","density":"comfortable","lang":"auto"}');
  assert.strictEqual(afterDensity, '{"size":"lg","mode":"dark","density":"compact","lang":"auto"}');
  assert.deepStrictEqual([root.dataset.size, root.dataset.theme, root.dataset.density], ['lg', 'dark', 'compact'], 'each pick applies at once');
  assert.deepStrictEqual(pressed(doc), ['size:lg', 'mode:dark', 'density:compact', 'lang:auto']);

  //when
  w.localStorage.setItem(KEY, '{"size":"sm","mode":"dark","density":"compact","lang":"auto"}');
  pick(doc, 'mode', 'light');

  //then
  assert.strictEqual(stored(w), '{"size":"sm","mode":"light","density":"compact","lang":"auto"}', 'a pick re-reads the stored value first, so another tab or the theme button is not undone');

  //when
  doc.getElementById('theme-btn').click();

  //then
  assert.deepStrictEqual([JSON.parse(stored(w)).mode, root.dataset.theme, pressed(doc)[1]], ['dark', 'dark', 'mode:dark'], 'the theme button and the Mode control stay in step');

  //when
  pick(doc, 'lang', 'en');
  await wait(50);

  //then
  assert.strictEqual(JSON.parse(stored(w)).lang, 'en');
  assert.ok(/skopax_lang=en/.test(doc.cookie), 'the language goes to the cookie the server reads: ' + doc.cookie);
  assert.ok(errors.length && errors.every((e) => /navigation/i.test(e)), 'and the page reloads in that language: ' + errors.join(' | '));
  dom.window.close();
  return 'the four Appearance controls sit on the Settings page and write skopax-appearance exactly as the old panel did, in step with the theme button';
}

async function resetClearsTheStoredAppearance() {
  //given
  const {dom, w, doc, errors} = await boot({stored: {[KEY]: '{"size":"lg","mode":"dark","density":"compact","lang":"auto"}'}});
  const root = doc.documentElement;
  const before = [root.dataset.size, root.dataset.theme, root.dataset.density];

  //when
  doc.getElementById('ap-reset').click();

  //then
  assert.deepStrictEqual(before, ['lg', 'dark', 'compact'], 'a stored appearance applies at boot');
  assert.ok(doc.getElementById('ap-reset').closest('#panel-prefs'), 'Reset sits on the Settings page');
  assert.strictEqual(stored(w), null, 'Reset clears the stored appearance');
  assert.deepStrictEqual([root.dataset.size, root.dataset.theme, root.dataset.density], ['md', undefined, undefined], 'and the defaults apply at once');
  assert.deepStrictEqual(pressed(doc), ['size:md', 'mode:system', 'density:comfortable', 'lang:auto']);
  assert.strictEqual(doc.getElementById('theme-btn').getAttribute('aria-label'), 'Theme: system. Click to change.');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'Reset clears skopax-appearance and the defaults apply at once';
}

async function theAccountSectionOffersPasswordToLocalUsersOnly() {
  //given
  const local = await boot({path: '/login'});
  const sso = await boot({me: Object.assign({}, LOCAL, {source: 'sso', canChangePassword: false, userRole: 'operator', admin: false})});
  const guest = await boot({me: {loginEnabled: true, user: null, name: null, userRole: null, source: null, canChangePassword: false, operator: false, admin: false, teams: [], tags: []}});
  const open = await boot({me: {loginEnabled: false, user: null, operator: true, admin: true, teams: [], tags: []}});
  const sec = (x) => x.doc.getElementById('pf-acct');
  const buttons = (x) => [...sec(x).querySelectorAll('button:not([hidden])')].map((b) => b.textContent);

  //then
  assert.ok(!sec(local).hidden && sec(local).closest('#panel-prefs'));
  assert.strictEqual(sec(local).querySelector('h3').textContent, 'Account');
  assert.strictEqual(local.doc.getElementById('pf-who').textContent, 'Signed in as alice.archer@example.com \u00b7 admin');
  assert.deepStrictEqual(buttons(local), ['Change password', 'Sign out'], 'a local user can change the password here');
  assert.deepStrictEqual(buttons(sso), ['Sign out'], 'an SSO user sees no Change password');
  assert.strictEqual(sso.doc.getElementById('pf-who').textContent, 'Signed in as alice.archer@example.com \u00b7 operator');
  assert.ok(sec(guest).hidden && sec(open).hidden, 'no account section without a login');
  assert.ok(!guest.doc.querySelector('#panel-prefs [data-ap="mode"]').closest('[hidden]'), 'a guest still has Appearance');

  //when
  local.doc.getElementById('pf-pw').click();

  //then
  const dlg = local.doc.getElementById('pw-dialog');
  assert.ok(dlg && dlg.getAttribute('role') === 'dialog', 'Change password opens the password dialog');
  dlg.querySelector('.pw-cancel').click();
  assert.strictEqual(local.doc.activeElement, local.doc.getElementById('pf-pw'), 'closing it gives the focus back to the button that opened it');

  //when
  local.doc.getElementById('pf-out').click();
  await wait(60);

  //then
  assert.deepStrictEqual(local.calls, [['POST', '/logout']]);
  assert.strictEqual(local.w.location.href, 'https://skopax.example.com/login', 'Sign out posts and then goes to the login page (served from /login here, so jsdom can follow the move)');
  assert.deepStrictEqual(local.errors.concat(sso.errors, guest.errors, open.errors), []);
  for (const x of [local, sso, guest, open]) x.dom.window.close();
  return 'the Account section shows who is signed in, Change password for local users only, and Sign out';
}

function rules(doc) {
  const out = [];
  const walk = (list) => [...list].forEach((r) => { if (r.selectorText) out.push(r); else if (r.cssRules) walk(r.cssRules); });
  [...doc.styleSheets].forEach((s) => walk(s.cssRules));
  return out;
}
function px(doc, selector, prop) {
  const rule = rules(doc).filter((r) => r.selectorText === selector && r.style.getPropertyValue(prop))[0];
  assert.ok(rule, 'a rule ' + selector + ' { ' + prop + ' } exists');
  return rule.style.getPropertyValue(prop).trim();
}
function token(doc, name) {
  const v = px(doc, ':root', name);
  assert.ok(/^\d+(\.\d+)?px$/.test(v), name + ' is a px length: ' + v);
  return parseFloat(v);
}
function box(doc, selector) {
  const p = px(doc, selector, 'padding').split(/\s+/).map(parseFloat);
  return {top: p[0], bottom: p.length > 2 ? p[2] : p[0]};
}
function len(doc, selector, prop, tokens) {
  const v = px(doc, selector, prop);
  const t = /^var\((--[a-z-]+)\)$/.exec(v);
  return t ? tokens(t[1]) : parseFloat(v);
}

function sidebarHeight(doc) {
  const t = (n) => token(doc, n);
  const local = (n) => {
    const v = px(doc, '.s-bot', n);
    assert.ok(/^\d+px$/.test(v), n + ' is a px length on .s-bot: ' + v);
    return parseFloat(v);
  };
  const row = t('--row-h'), head = t('--grp-h'), gap = t('--grp-gap'), instH = t('--inst-h'), searchH = t('--search-h'), userH = local('--user-h'), attrH = local('--attr-h');
  const groups = [...doc.querySelectorAll('#snav > .s-grp')];
  const shown = (g) => g.classList.contains('folded') ? 0 : [...g.querySelectorAll(':scope > a.tab:not([hidden])')].length;
  const navRows = groups.reduce((n, g) => n + shown(g), 0), heads = groups.filter((g) => g.querySelector('.g-h')).length;
  const pinned = [...doc.querySelectorAll('#pinned > a.tab:not([hidden])')].length;
  const side = box(doc, '.side'), top = box(doc, '.s-top'), nav = box(doc, '.s-nav');
  const sideGap = parseFloat(px(doc, '.side', 'gap')), botGap = parseFloat(px(doc, '.s-bot', 'gap'));
  const navH = nav.top + nav.bottom + heads * head + navRows * row + (navRows - groups.length) + (groups.length - 1) * len(doc, '.s-grp + .s-grp', 'margin-top', t);
  const footH = parseFloat(px(doc, '.s-bot', 'padding-top')) + parseFloat(px(doc, '.s-bot', 'border-top')) + pinned * row + Math.max(pinned - 1, 0) + botGap + userH + botGap + attrH;
  const slot = doc.getElementById('side-top');
  assert.deepStrictEqual([...slot.children].map((c) => c.id), ['inst', 'search-btn'], 'the switcher and the search fill the slot, so they are in this sum');
  assert.deepStrictEqual([len(doc, '.inst-btn', 'height', t), len(doc, '.search', 'height', t), len(doc, '.side-top', 'gap', t)], [instH, searchH, gap], 'the switcher is --inst-h, the search --search-h, one --grp-gap between them');
  assert.deepStrictEqual(['.inst-btn', '.search'].map((s) => px(doc, s, 'box-sizing')), ['border-box', 'border-box'], 'their borders sit inside those heights');
  const look = (el) => doc.defaultView.getComputedStyle(el), edge = (el, props) => props.map((k) => parseFloat(look(el).getPropertyValue(k)) || 0);
  const vertical = ['margin-top', 'margin-bottom'], box4 = ['padding-top', 'padding-bottom', 'border-top-width', 'border-bottom-width'];
  assert.deepStrictEqual([slot, slot.querySelector('#inst'), slot.querySelector('#inst-btn'), slot.querySelector('#search-btn'), slot.querySelector('#inst-menu')].map((el) => edge(el, vertical)), [[0, 0], [0, 0], [0, 0], [0, 0], [0, 0]], 'no vertical margin in the slot adds to the sum');
  assert.deepStrictEqual([slot, slot.querySelector('#inst')].map((el) => edge(el, box4)), [[0, 0, 0, 0], [0, 0, 0, 0]], 'the slot and the switcher box add no padding or border');
  assert.strictEqual(look(slot.querySelector('#inst-menu')).getPropertyValue('position'), 'absolute', 'the menu floats, so it never adds height');
  assert.ok(slot.querySelector('#inst-btn').classList.contains('inst-btn') && slot.querySelector('#search-btn').classList.contains('search'));
  const topH = instH + gap + searchH;
  assert.deepStrictEqual([row, head, gap, instH, searchH], [28, 18, 6, 36, 30], 'the plan\'s tightened tokens, no smaller');
  assert.strictEqual(len(doc, '.side .tab', 'height', t), row, 'rows are --row-h tall');
  assert.strictEqual(len(doc, '.g-h', 'height', t), head, 'group headers are --grp-h tall');
  assert.strictEqual(len(doc, '.user-btn', 'height', (n) => local(n)), userH, 'the user button is --user-h tall');
  assert.deepStrictEqual(['min-height', 'line-height'].map((k) => len(doc, '.side-attr', k, (n) => local(n))), [attrH, attrH], 'the attribution line is --attr-h tall');
  const line = doc.getElementById('attribution'), cs = (el) => doc.defaultView.getComputedStyle(el);
  const name = line.querySelector('.attr-name'), by = line.querySelector('.attr-by');
  assert.deepStrictEqual(['display', 'white-space', 'overflow', 'text-overflow'].map((k) => cs(line).getPropertyValue(k)), ['flex', 'nowrap', 'hidden', 'ellipsis'], 'the attribution is always one line, clipped inside the sidebar');
  assert.deepStrictEqual(['min-width', 'overflow', 'text-overflow'].map((k) => cs(name).getPropertyValue(k)), ['0px', 'hidden', 'ellipsis'], 'the display name is what an ellipsis cuts');
  if (by) assert.deepStrictEqual([by.textContent, cs(by).getPropertyValue('flex-shrink'), cs(by).getPropertyValue('white-space')], [' \u00b7 powered by Skopax 0.1.0', '0', 'pre'], 'the powered-by part never shrinks');
  assert.strictEqual(line.title, line.textContent, 'the full text is its tooltip');
  assert.ok(doc.getElementById('side-more-list').classList.contains('folded'), 'More is folded');
  assert.deepStrictEqual([groups.length, navRows, heads, pinned], [5, 16, 4, 0], 'an admin sees every core row and More folded; Settings and Admin are in the user menu');
  assert.ok(doc.getElementById('user-btn') && !doc.getElementById('account').hidden, 'the account box is shown');
  const snav = doc.getElementById('snav'), foot = doc.querySelector('#side > .s-bot');
  assert.deepStrictEqual(['flex-grow', 'min-height', 'overflow-y'].map((k) => cs(snav).getPropertyValue(k)), ['1', '0px', 'auto'], 'only the groups area grows, shrinks and scrolls');
  assert.ok(foot && !snav.contains(foot) && !snav.contains(slot) && foot.contains(doc.getElementById('attribution')) && foot.contains(doc.getElementById('pinned')), 'the foot and the top slot sit outside the scroller, so the foot stays in view at any height');
  assert.deepStrictEqual(['display', 'flex-direction'].map((k) => cs(doc.getElementById('side')).getPropertyValue(k)), ['flex', 'column']);
  return side.top + side.bottom + parseFloat(px(doc, '.s-top', 'min-height')) + top.top + top.bottom + 3 * sideGap + topH + navH + footH;
}

async function everyCoreGroupFitsAn800PxScreenAndTheFootStaysOnShorterOnes() {
  //given
  const max = Number(/^MAX_NAME = (\d+)$/m.exec(fs.readFileSync(path.join(__dirname, '..', '..', 'skopax', 'brand.py'), 'utf8'))[1]);
  const brand = (name) => ({name, slug: 'shop', version: '0.1.0', attribution: {text: name + ' \u00b7 powered by Skopax 0.1.0', href: 'https://skopax.io'}});
  const names = ['Northwind Shops Platform Health Board EU', 'Northwindshopsplatformhealthboardeuwest1', 'WMWM WMWM WMWM WMWM WMWM WMWM WMWM WMWMW'].map((n) => n.slice(0, max));
  const plain = await boot({hash: '#t=anoms'});
  const custom = [];
  for (const n of names) custom.push(await boot({hash: '#t=anoms', brand: brand(n)}));

  //when
  const heights = [plain].concat(custom).map((x) => sidebarHeight(x.doc));

  //then
  assert.deepStrictEqual(names.map((n) => n.length), [max, max, max], 'each display name is as long as the server allows: spaced, one word, wide letters');
  assert.deepStrictEqual(custom.map((x) => x.doc.getElementById('attribution').textContent), names.map((n) => brand(n).attribution.text), 'the link keeps the whole powered-by text');
  assert.deepStrictEqual(custom.map((x) => x.doc.querySelector('#attribution .attr-name').textContent), names, 'the display name sits in its own part');
  assert.deepStrictEqual([plain.doc.querySelector('#attribution .attr-name').textContent, plain.doc.querySelector('#attribution .attr-by')], ['Skopax 0.1.0 \u00b7 skopax.io', null], 'the default line is one part');
  assert.deepStrictEqual(heights, heights.map(() => heights[0]), 'a long display name adds no height');
  assert.ok(heights[0] <= 800, 'the switcher, the search, every core group, the pinned rows, the account and the one-line attribution fit an 800 px tall screen: ' + heights[0] + ' px');
  assert.deepStrictEqual([plain].concat(custom).reduce((a, x) => a.concat(x.errors), []), []);
  [plain].concat(custom).forEach((x) => x.dom.window.close());
  return 'with the switcher, the search and the account shown and More folded, the sidebar adds up to ' + heights[0] + ' px of an 800 px screen (' + (800 - heights[0]) + ' px spare), whatever the display name; on a shorter screen only the groups scroll';
}

const langRow = (doc) => doc.querySelector('#panel-prefs [data-ap="lang"]');
const langButtons = (doc) => [...langRow(doc).querySelectorAll('button')].map((b) => b.dataset.v + '=' + b.textContent + (b.getAttribute('aria-pressed') === 'true' ? '*' : ''));
const langOptions = (doc) => [...langRow(doc).querySelectorAll('select option')].map((o) => o.value + '=' + o.textContent);
const reloaded = (errors) => errors.some((e) => /navigation/i.test(e));

async function theLanguageRowListsTheLoadedPacksAndChoosingOneSetsTheCookieAndReloads() {
  //given
  const {dom, w, doc, errors} = await boot({packs: {tr: {}, de: {}}});

  //then
  assert.deepStrictEqual(langButtons(doc), ['auto=Auto*', 'en=English', 'de=Deutsch', 'tr=T\u00fcrk\u00e7e'], 'Auto, English, then each loaded pack under its own name, in tag order');
  assert.strictEqual(langRow(doc).querySelector('select'), null, 'up to five entries stay buttons');

  //when
  pick(doc, 'lang', 'de');
  await wait(50);

  //then
  assert.strictEqual(JSON.parse(stored(w)).lang, 'de');
  assert.ok(/skopax_lang=de/.test(doc.cookie), 'the pick goes to the cookie the server and the chat read: ' + doc.cookie);
  assert.ok(reloaded(errors), 'and the page reloads in German: ' + errors.join(' | '));
  assert.ok(langButtons(doc).includes('de=Deutsch*'), 'the pick is pressed');
  dom.window.close();
  return 'the Language row lists Auto, English and each loaded pack by its own name, and choosing German sets the cookie and reloads';
}

async function aSavedLanguageWhosePackHasGoneFallsBackToAuto() {
  //given
  const {dom, w, doc, errors} = await boot({packs: {tr: {}}, stored: {[KEY]: '{"size":"md","mode":"system","density":"comfortable","lang":"de"}'}});

  //then
  assert.deepStrictEqual(langButtons(doc), ['auto=Auto*', 'en=English', 'tr=T\u00fcrk\u00e7e'], 'a pack that is not on the page cannot stay pressed, Auto is');
  assert.ok(!/skopax_lang=de/.test(doc.cookie), 'and the cookie does not carry it: ' + doc.cookie);
  assert.ok(!reloaded(errors), 'nothing reloads at boot');
  dom.window.close();
  return 'an unknown saved language shows as Auto and sends no cookie';
}

async function pastFiveEntriesTheRowIsASelectThatPicksAndResets() {
  //given
  const core = {tr: {}, de: {}, es: {}, fr: {}};
  const {dom, w, doc, errors} = await boot({packs: core});
  const sel = langRow(doc).querySelector('select');

  //then
  assert.ok(sel && langRow(doc).querySelectorAll('button').length === 0, 'six entries use a select');
  assert.deepStrictEqual(langOptions(doc), ['auto=Auto', 'en=English', 'de=Deutsch', 'es=Espa\u00f1ol', 'fr=Fran\u00e7ais', 'tr=T\u00fcrk\u00e7e'], 'every pack under its own name');
  assert.strictEqual(sel.value, 'auto');

  //when
  sel.value = 'es';
  sel.dispatchEvent(new w.Event('change', {bubbles: true}));
  await wait(50);

  //then
  assert.strictEqual(JSON.parse(stored(w)).lang, 'es');
  assert.ok(/skopax_lang=es/.test(doc.cookie), doc.cookie);
  assert.ok(reloaded(errors), 'the page reloads: ' + errors.join(' | '));
  assert.strictEqual(langRow(doc).querySelector('select').value, 'es');

  //when
  doc.getElementById('ap-reset').click();

  //then
  assert.strictEqual(langRow(doc).querySelector('select').value, 'auto', 'Reset goes back to Auto');
  assert.ok(!/skopax_lang=es/.test(doc.cookie), 'and clears the cookie: ' + doc.cookie);
  dom.window.close();
  return 'with more than five entries the Language row is a select that picks, reloads and resets';
}

async function packKeysThatAreNotLanguageTagsAreNeverListedOrInjected() {
  //given
  const {dom, doc, errors} = await boot({packs: {de: {}, '<img src=x onerror=alert(1)>': {}, 'x"y': {}, '': {}}});

  //then
  assert.deepStrictEqual(langButtons(doc), ['auto=Auto*', 'en=English', 'de=Deutsch'], 'only the real tag is listed');
  assert.strictEqual(langRow(doc).querySelector('img'), null, 'no markup lands in the row');
  assert.ok(!/onerror|x"y/.test(langRow(doc).outerHTML), 'and no hostile key shows in the row: ' + langRow(doc).outerHTML);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a pack key that is not a language tag is neither listed nor injected into the row';
}

(async () => {
  console.log('PASS ' + await eachControlWritesTheAppearanceAsTheOldPanelDid());
  console.log('PASS ' + await resetClearsTheStoredAppearance());
  console.log('PASS ' + await theLanguageRowListsTheLoadedPacksAndChoosingOneSetsTheCookieAndReloads());
  console.log('PASS ' + await aSavedLanguageWhosePackHasGoneFallsBackToAuto());
  console.log('PASS ' + await pastFiveEntriesTheRowIsASelectThatPicksAndResets());
  console.log('PASS ' + await packKeysThatAreNotLanguageTagsAreNeverListedOrInjected());
  console.log('PASS ' + await theAccountSectionOffersPasswordToLocalUsersOnly());
  console.log('PASS ' + await everyCoreGroupFitsAn800PxScreenAndTheFootStaysOnShorterOnes());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
