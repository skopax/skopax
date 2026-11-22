require('./focus');
const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const EVIL = '<img src=x onerror=alert(1)>"\'';
const KEY = 'log|checkout-service|ERROR|boom';
const PLAIN = {name: 'Skopax', slug: 'skopax', version: '0.1.0', attribution: {text: 'Skopax 0.1.0 \u00b7 skopax.io', href: 'https://skopax.io'}};

function state() {
  const card = (key, extra) => Object.assign({key, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'ERROR lines', src: 'logs', sig: 'log', now: 1, firstSeen: iso(30), lastSeen: iso(1)}, extra || {});
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(KEY, {num: 14}), card('log|shop|ERROR|npe', {num: 15, sev: 'crit'}), card('log|cart-service|ERROR|x', {num: 16, status: 'cleared', sev: 'info'})],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

function media(width) {
  return (q) => {
    const parts = String(q).split(/\s+and\s+/);
    const matches = parts.every((part) => {
      const m = /\((min|max)-width:\s*(\d+)px\)/.exec(part);
      if (!m) return false;
      return m[1] === 'min' ? width >= Number(m[2]) : width <= Number(m[2]);
    });
    return {matches, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}};
  };
}

async function boot(opts) {
  opts = opts || {};
  const errors = [], seen = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  let html = pageHtml(TEMPLATE, {brand: opts.brand || PLAIN}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state()));
  if (opts.listen) html = html.replace('<script type="text/plain" id="plugin-scripts"></script>', '<script>document.addEventListener("skopax:page", function (e) { window.__pages = (window.__pages || []).concat([e.detail.tab]); e.detail.section.textContent = "filled by " + e.detail.tab; });</script>');
  const me = Object.assign({loginEnabled: true, user: 'alice@example.com', operator: true, admin: false, teams: [], tags: [], chat: true}, opts.me || {});
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/' + (opts.hash || ''), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      if (opts.width) w.matchMedia = media(opts.width);
      if (opts.calm) w.matchMedia = (q) => ({matches: /\(prefers-reduced-motion:\s*reduce\)/.test(q), media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}});
      Object.keys(opts.stored || {}).forEach((k) => w.localStorage.setItem(k, opts.stored[k]));
      w.fetch = async (path) => {
        const p = String(path);
        seen.push(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me')) { if (opts.meDelay) await wait(opts.meDelay); return ok(me); }
        if (p.startsWith('/api/server')) { if (opts.serverDelay) await wait(opts.serverDelay); return ok({plugins: opts.plugins || [], tz: 'UTC'}); }
        if (p.startsWith('/api/admin/settings')) return ok({items: [], rev: 1, refreshSeconds: 15});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
      };
    },
  });
  await wait(opts.settle || 350);
  return {dom, w: dom.window, doc: dom.window.document, errors, seen};
}

const labels = (box) => [...box.querySelectorAll('a.tab:not([hidden]) .lbl')].map((l) => l.textContent);
const current = (doc) => [...doc.querySelectorAll('#side [aria-current="page"]')].map((a) => a.id);
const visible = (doc) => { const b = doc.body.cloneNode(true); b.querySelectorAll('script, style').forEach((x) => x.remove()); return b.textContent; };
const key = (w, el, k) => el.dispatchEvent(new w.KeyboardEvent('keydown', {key: k, bubbles: true, cancelable: true}));
const css = (doc) => [...doc.styleSheets].flatMap((s) => [...s.cssRules]).map((r) => r.cssText).join('\n');
const sized = (el, box) => ['scrollTop', 'clientHeight', 'scrollHeight'].forEach((k) => Object.defineProperty(el, k, {configurable: true, get: () => box[k], set: (v) => { box[k] = v; }}));

async function theSidebarListsTheMapInOrder() {
  //given
  const {dom, w, doc, errors} = await boot({hash: '#t=anoms'});
  const groups = [...doc.querySelectorAll('#snav > .s-grp')];

  //then
  assert.deepStrictEqual(groups.map((g) => g.dataset.grp), ['main', 'observe', 'activity', 'connect', 'more'], 'five groups in the scroller');
  assert.strictEqual(groups[0].getAttribute('aria-label'), 'Board and investigator', 'the first group has no header, only a name for screen readers');
  assert.deepStrictEqual(groups.slice(1, 4).map((g) => g.querySelector('.g-h').textContent), ['Observe', 'Activity', 'Connect']);
  assert.deepStrictEqual(groups.slice(1, 4).map((g) => g.getAttribute('aria-labelledby')), groups.slice(1, 4).map((g) => g.querySelector('.g-h').id));
  assert.deepStrictEqual(groups.map(labels), [['Board', 'Requests', 'Chat', 'Investigator'], ['Services', 'Information', 'Latency', 'Releases', 'Deploy reports'], ['Notifications', 'Events', 'History', 'Costs'], ['Integrations', 'Tools', 'Connect MCP'], ['Review', 'Docs']]);
  assert.deepStrictEqual(labels(doc.getElementById('pinned')), [], 'a signed-in person has Settings and Admin in the user menu, not as rows');
  const ids = ['anoms', 'reqs', 'chat', 'inv', 'svcs', 'info', 'lat', 'rels', 'reps', 'slack', 'hist', 'acts', 'costs', 'integrations', 'tools', 'mcp', 'review', 'docs', 'prefs'];
  assert.deepStrictEqual([...doc.querySelectorAll('#side a.tab[data-tab]')].map((a) => a.id), ids.map((id) => 'tab-' + id), 'every row is tab-<id>');
  assert.ok(ids.every((id) => doc.getElementById('count-' + id) && doc.getElementById('tab-' + id).contains(doc.getElementById('count-' + id))), 'and carries count-<id>');
  assert.ok(ids.every((id) => doc.getElementById('tab-' + id).getAttribute('href') === '#t=' + id || id === 'anoms'), 'rows are #t= links');
  assert.strictEqual(doc.querySelector('#side [role="tab"], #side [aria-selected]'), null, 'no tab roles on the rows');
  assert.strictEqual(doc.getElementById('count-anoms').textContent, '2', 'Board counts the open cards');
  assert.deepStrictEqual([...doc.querySelectorAll('#side .b-new')].map((b) => [b.closest('a.tab').id, b.textContent]), [], 'no row carries a New badge');
  const drawn = (name) => { const d = doc.createElement('div'); d.innerHTML = w.navIcon(name); return d.firstChild.innerHTML; };
  assert.strictEqual(doc.querySelector('#tab-svcs svg.i').innerHTML, drawn('server'), 'icons come from icons.js');
  assert.strictEqual(doc.querySelector('#tab-anoms svg.i').innerHTML, drawn('board'));
  assert.ok(doc.querySelector('#tab-svcs svg').classList.contains('i'), 'a row icon carries the icon class');
  assert.deepStrictEqual([...doc.querySelectorAll('#side a.tab')].filter((a) => { const i = a.querySelector('svg.i'); return !i || !i.children.length; }).map((a) => a.id), [], 'every sidebar row draws a known icon');
  const iconRule = [...doc.styleSheets].flatMap((s) => [...s.cssRules]).filter((r) => r.selectorText === '.i').map((r) => r.style)[0];
  assert.ok(iconRule, 'the stylesheet carries the .i rule');
  assert.deepStrictEqual(['stroke', 'fill', 'stroke-width', 'stroke-linecap', 'stroke-linejoin', 'width', 'height'].map((p) => iconRule.getPropertyValue(p)), ['currentcolor', 'none', '1.8', 'round', 'round', '16px', '16px'], 'stroked like the nav icons');
  assert.deepStrictEqual([...doc.getElementById('side-top').children].map((c) => c.id), ['inst', 'search-btn'], 'the slot holds the instance switcher and the search');
  assert.ok(!doc.getElementById('toptabs') && !doc.getElementById('top-tabs-r') && !doc.getElementById('nav-open') && !doc.querySelector('.topbar'), 'the old header is gone');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the sidebar lists the map: an unnamed first group, Observe, Activity, Connect and a More fold, with tab-<id> rows, count-<id> counts, icons from icons.js and New on Chat and Connect MCP';
}

async function aRowOpensItsPageAndMarksItself() {
  //given
  const {dom, w, doc, errors} = await boot({hash: '#t=anoms'});

  //when
  doc.getElementById('tab-svcs').click();
  await wait(30);

  //then
  assert.deepStrictEqual(current(doc), ['tab-svcs'], 'only Services is current');
  assert.ok(!doc.getElementById('panel-svcs').hidden && doc.getElementById('panel-anoms').hidden);
  assert.strictEqual(doc.getElementById('crumb-page').textContent, 'Services', 'the crumb names the page');
  assert.strictEqual(doc.title, 'Services \u00b7 Skopax');
  assert.strictEqual(w.location.hash, '#t=svcs');
  assert.deepStrictEqual(errors, []);
  dom.window.close();

  //given
  const svcs = await boot({hash: '#t=svcs'});
  const open = await boot({hash: '#t=anoms/all'});
  const linked = await boot({hash: '#t=svcs'});
  linked.w.location.hash = '#a=' + encodeURIComponent(KEY);
  await wait(100);

  //then
  assert.deepStrictEqual(current(svcs.doc), ['tab-svcs'], '#t=svcs opens Services');
  assert.deepStrictEqual(current(open.doc), ['tab-anoms'], '#t=anoms/all opens the Board');
  assert.strictEqual(open.doc.querySelector('#tab-sub-anoms button[aria-pressed="true"]').dataset.f, 'all', 'on the All filter');
  assert.strictEqual(open.doc.getElementById('crumb-page').textContent, 'Board');
  assert.match(open.doc.getElementById('copy-f').title, /^Copies a link that opens Board with this filter/, 'tooltips name the Board too');
  assert.deepStrictEqual(current(linked.doc), ['tab-anoms'], '#a= opens the Board');
  assert.ok(linked.doc.querySelector('#anoms article.inc.linked[data-key="' + KEY + '"]'), 'with the card marked');
  for (const x of [svcs, open, linked]) { assert.deepStrictEqual(x.errors, []); x.dom.window.close(); }
  return 'a row click marks only itself with aria-current, shows its panel, names it in the crumb and the title, and #t=, #t=anoms/all and #a= links still work';
}

async function moreFoldsAndOpensForItsRows() {
  //given
  const {dom, doc, errors} = await boot({hash: '#t=anoms'});
  const more = doc.getElementById('side-more'), list = doc.getElementById('side-more-list');
  const folded = {expanded: more.getAttribute('aria-expanded'), hidden: list.classList.contains('folded'), controls: more.getAttribute('aria-controls')};

  //when
  more.click();
  const opened = {expanded: more.getAttribute('aria-expanded'), hidden: list.classList.contains('folded')};
  more.click();
  const shut = list.classList.contains('folded');
  dom.window.close();
  const review = await boot({hash: '#t=review'});
  const docs = await boot({hash: '#t=docs'});

  //then
  assert.deepStrictEqual(folded, {expanded: 'false', hidden: true, controls: 'side-more-list'}, 'More starts folded');
  assert.deepStrictEqual(opened, {expanded: 'true', hidden: false}, 'a click unfolds it');
  assert.ok(shut, 'and folds it again');
  assert.ok(review.doc.getElementById('side-more-list').classList.contains('folded') && review.doc.getElementById('tab-review').getAttribute('aria-current') === 'page', 'opening Review keeps More folded; the active row stays visible by the fold rule');
  assert.deepStrictEqual(current(review.doc), ['tab-review']);
  assert.ok(!docs.doc.getElementById('panel-docs').hidden, 'Docs has its own page');
  assert.match(docs.doc.getElementById('panel-docs').textContent, /How this works/);
  assert.strictEqual(docs.doc.querySelector('#panel-docs a[href="/docs"]').textContent.trim(), 'API reference', 'with a link to the API reference');
  assert.strictEqual(docs.doc.querySelector('details.foot'), null, 'the foot of the page no longer holds the explainer');
  assert.deepStrictEqual(errors.concat(review.errors, docs.errors), []);
  review.dom.window.close(); docs.dom.window.close();
  return 'More starts folded, a click unfolds and folds it, opening Review leaves it folded with the row current, and Docs is a page with How this works and the API reference link';
}

async function openingMoreScrollsItsRowsIntoView() {
  //given
  const {dom, w, doc, errors} = await boot({hash: '#t=anoms'});
  const calls = [];
  w.Element.prototype.scrollIntoView = function (o) { calls.push([this.id, Object.assign({}, o)]); };
  const more = doc.getElementById('side-more');

  //when
  more.click();
  const opened = calls.slice();
  more.click();
  const closed = calls.slice();
  more.click();
  doc.getElementById('tab-review').click();
  doc.getElementById('tab-docs').click();
  const again = calls.slice();
  dom.window.close();
  const calm = await boot({hash: '#t=anoms', calm: true});
  const quiet = [];
  calm.w.Element.prototype.scrollIntoView = function (o) { quiet.push([this.id, Object.assign({}, o)]); };
  calm.doc.getElementById('side-more').click();

  //then
  assert.deepStrictEqual(opened, [['side-more-list', {block: 'nearest', behavior: 'smooth'}]], 'opening More scrolls its rows into view in the nav');
  assert.deepStrictEqual(closed, opened, 'closing it does not scroll');
  assert.deepStrictEqual(again, opened.concat(opened), 'opening it again scrolls once more, and a page in More that is already open does not');
  assert.deepStrictEqual(quiet, [['side-more-list', {block: 'nearest', behavior: 'auto'}]], 'with reduced motion the scroll is not smooth');
  assert.deepStrictEqual(errors.concat(calm.errors), []);
  calm.dom.window.close();
  return 'opening More scrolls its rows into view, smoothly unless reduced motion is asked for, and closing it or opening one of its pages does not';
}

async function theNavFadesWhileRowsAreBelowTheFold() {
  //given
  const {dom, w, doc, errors} = await boot({hash: '#t=anoms'});
  const nav = doc.getElementById('snav'), box = {scrollTop: 0, clientHeight: 400, scrollHeight: 400};
  const faded = () => nav.classList.contains('more-below');
  const scrolled = (top) => { box.scrollTop = top; nav.dispatchEvent(new w.Event('scroll')); return faded(); };
  const start = faded();
  sized(nav, box);

  //when
  const fits = scrolled(0);
  box.scrollHeight = 600;
  const steps = [scrolled(0), scrolled(198), scrolled(199), scrolled(200)];
  box.scrollHeight = 400;
  w.dispatchEvent(new w.Event('resize'));
  const resized = faded();
  box.scrollTop = 0;
  box.scrollHeight = 700;
  doc.getElementById('side-more').click();
  await null;
  const grown = faded();
  box.scrollHeight = 400;
  doc.getElementById('side-rail').click();
  const railFits = faded();
  box.scrollHeight = 650;
  doc.getElementById('side-rail').click();
  const fullBelow = faded();
  const rules = css(doc);

  //then
  assert.deepStrictEqual([start, fits], [false, false], 'a nav whose rows all fit shows no fade');
  assert.deepStrictEqual(steps, [true, true, false, false], 'the fade shows while the nav can scroll further down, and goes at the bottom');
  assert.strictEqual(resized, false, 'a resize checks again');
  assert.strictEqual(grown, true, 'rows that open below the fold bring the fade');
  assert.deepStrictEqual([railFits, fullBelow], [false, true], 'switching between the rail and the full sidebar checks again');
  assert.match(rules, /\.s-nav\.more-below\s*\{[^}]*mask-image:\s*linear-gradient/, 'the fade is a mask on the nav, so it catches no clicks and needs no colour');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the nav gets more-below while it can scroll further down, checked on scroll, resize, opened rows and the rail toggle, and the fade is a mask';
}

async function theRailIsRememberedAndShowsLabelsAsTips() {
  //given
  const {dom, w, doc, errors} = await boot({hash: '#t=anoms'});
  const btn = doc.getElementById('side-rail');
  const full = {rail: doc.body.classList.contains('is-rail'), label: btn.getAttribute('aria-label')};

  //when
  btn.click();
  const rail = {rail: doc.body.classList.contains('is-rail'), stored: w.localStorage.getItem('skopax-sidebar'), label: btn.getAttribute('aria-label')};
  doc.getElementById('tab-svcs').dispatchEvent(new w.Event('pointerover', {bubbles: true}));
  await wait(150);
  const tip = {shown: !doc.getElementById('tipbox').hidden, text: doc.getElementById('tipbox').textContent};
  doc.getElementById('tab-svcs').dispatchEvent(new w.Event('pointerout', {bubbles: true}));
  const rules = [...doc.styleSheets].flatMap((s) => [...s.cssRules]);
  const hidesLabels = rules.some((r) => /\.is-rail \.side \.lbl/.test(r.selectorText || '') && r.style.getPropertyValue('position') === 'absolute' && r.style.getPropertyValue('width') === '1px');
  btn.click();
  const back = {rail: doc.body.classList.contains('is-rail'), stored: w.localStorage.getItem('skopax-sidebar')};
  doc.getElementById('tab-svcs').dispatchEvent(new w.Event('pointerover', {bubbles: true}));
  await wait(150);
  const fullTip = doc.getElementById('tipbox').hidden || !doc.getElementById('tipbox').textContent.startsWith('Services');
  dom.window.close();
  const again = await boot({hash: '#t=anoms', stored: {'skopax-sidebar': 'rail'}});

  //then
  assert.deepStrictEqual(full, {rail: false, label: 'Collapse sidebar'});
  assert.deepStrictEqual(rail, {rail: true, stored: 'rail', label: 'Expand sidebar'}, 'the toggle folds the sidebar to a rail and remembers it');
  assert.ok(tip.shown && tip.text.startsWith('Services'), 'in the rail a row shows its label as a tip: ' + JSON.stringify(tip));
  assert.ok(hidesLabels, 'the labels are visually hidden in the rail');
  assert.deepStrictEqual(back, {rail: false, stored: 'full'}, 'and opens it again');
  assert.ok(fullTip, 'the full sidebar shows no label tips');
  assert.ok(again.doc.body.classList.contains('is-rail'), 'a reload starts in the rail');
  assert.deepStrictEqual(errors.concat(again.errors), []);
  again.dom.window.close();
  return 'the rail toggle writes skopax-sidebar, a reload keeps the rail, labels are visually hidden there and a hovered row shows its label in the tip box';
}

async function aRailTipCarriesTheRowBadges() {
  //given
  const plugins = [{id: 'demo', name: 'Demo', icon: '/plugins/demo/icon.svg', tab: 'demo', passes: [], instances: [], pages: [{tab: 'demo', label: 'Demo board', group: 'observe', icon: '/plugins/demo/icon.svg'}]}];
  const {dom, w, doc, errors} = await boot({hash: '#t=anoms', stored: {'skopax-sidebar': 'rail'}, plugins, settle: 500});
  const tip = async (id) => {
    const row = doc.getElementById(id);
    row.dispatchEvent(new w.Event('pointerover', {bubbles: true}));
    await wait(150);
    const text = doc.getElementById('tipbox').hidden ? null : doc.getElementById('tipbox').textContent;
    row.dispatchEvent(new w.Event('pointerout', {bubbles: true}));
    return text;
  };

  //when
  w.ntBadge(0);
  const zero = (await tip('tab-slack')).split('\n')[0];
  w.ntBadge(3);
  const rail = [await tip('tab-mcp'), await tip('tab-chat'), await tip('tab-demo'), (await tip('tab-slack')).split('\n')[0], (await tip('tab-svcs')).split('\n')[0]];
  w.ntBadge(12);
  const capped = (await tip('tab-slack')).split('\n')[0];
  doc.getElementById('side-rail').click();
  const full = await tip('tab-mcp');

  //then
  assert.deepStrictEqual(rail, ['Connect MCP', 'Chat', 'Demo board \u00b7 plug-in', 'Notifications \u00b7 3 unread', 'Services'], 'in the rail a tip carries what the hidden row shows: the unread count and the plug-in tag');
  assert.strictEqual(capped, 'Notifications \u00b7 9+ unread', 'the unread part reads as the badge does');
  assert.strictEqual(zero, 'Notifications', 'with nothing unread the hidden zero badge adds nothing to the tip');
  assert.ok(full === null || full.indexOf('\u00b7 New') < 0, 'the full sidebar shows the badges themselves, so no tip repeats them');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a rail tip carries the row badges as the mockup does: label \u00b7 New, \u00b7 N unread and \u00b7 plug-in';
}

async function onAPhoneTheHamburgerOpensTheDrawer() {
  //given
  const {dom, w, doc, errors} = await boot({hash: '#t=anoms', width: 600});
  const hamb = doc.getElementById('side-open'), content = doc.getElementById('content'), scrim = doc.getElementById('side-scrim');
  const closed = {open: doc.body.classList.contains('drawer-open'), inert: content.hasAttribute('inert'), expanded: hamb.getAttribute('aria-expanded'), rail: doc.getElementById('side-rail').getAttribute('aria-label')};

  //when
  hamb.click();
  const opened = {open: doc.body.classList.contains('drawer-open'), inert: content.hasAttribute('inert'), scrim: !scrim.hidden, expanded: hamb.getAttribute('aria-expanded'), focus: doc.activeElement.id};
  key(w, doc.activeElement, 'Escape');
  const escaped = {open: doc.body.classList.contains('drawer-open'), inert: content.hasAttribute('inert'), scrim: scrim.hidden, focus: doc.activeElement.id};
  hamb.click();
  scrim.click();
  const scrimmed = {open: doc.body.classList.contains('drawer-open'), focus: doc.activeElement.id};
  hamb.click();
  w.setPlugins([{id: 'demo', name: 'Demo', icon: '/plugins/demo/icon.svg', tab: null, passes: [], instances: [{name: 'main', primary: true, pass: 'demo', status: 'ok'}], pages: []}]);
  const refreshed = {open: doc.body.classList.contains('drawer-open'), inert: content.hasAttribute('inert')};
  doc.getElementById('tab-hist').click();
  const picked = {open: doc.body.classList.contains('drawer-open'), inert: content.hasAttribute('inert'), page: !doc.getElementById('panel-hist').hidden};

  //then
  assert.deepStrictEqual(closed, {open: false, inert: false, expanded: 'false', rail: 'Close menu'}, 'the drawer starts closed and its toggle reads Close menu on a phone');
  assert.deepStrictEqual(opened, {open: true, inert: true, scrim: true, expanded: 'true', focus: 'side-rail'}, 'the hamburger opens the drawer, makes the content inert and focuses the close button');
  assert.deepStrictEqual(escaped, {open: false, inert: false, scrim: true, focus: 'side-open'}, 'Escape closes it and gives the focus back to the hamburger');
  assert.deepStrictEqual(scrimmed, {open: false, focus: 'side-open'}, 'so does the scrim');
  assert.deepStrictEqual(refreshed, {open: true, inert: true}, 'a background refresh of the plug-in list leaves the drawer open');
  assert.deepStrictEqual(picked, {open: false, inert: false, page: true}, 'picking a row opens its page and closes the drawer');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'at 600 px the hamburger opens the drawer with the content inert and the focus on the close button; Escape, the scrim or a pick close it and give the focus back';
}

async function theAdminRowShowsForAdminsOnly() {
  //given
  const viewer = await boot({hash: '#t=anoms'});
  const admin = await boot({hash: '#t=anoms', me: {admin: true, userRole: 'admin'}});
  const row = admin.doc.getElementById('side-admin');

  //when
  row.click();
  await wait(40);

  //then
  assert.ok(viewer.doc.getElementById('side-admin').hidden, 'a viewer sees no Admin row');
  assert.ok(row.hidden && !admin.doc.getElementById('um-admin').hidden && viewer.doc.getElementById('um-admin').hidden, 'an admin has Admin in the user menu, a viewer does not');
  assert.ok(!admin.doc.getElementById('panel-settings').hidden, 'the first time it opens General');
  assert.deepStrictEqual(current(admin.doc), ['side-admin', 'user-btn'], 'and the user button is marked current');
  assert.strictEqual(admin.doc.getElementById('crumb-page').textContent, 'Admin \u203a Settings');
  assert.deepStrictEqual([...admin.doc.querySelectorAll('#adm-nav a.tab')].map((a) => [a.id, a.querySelector('.lbl').textContent]), [['tab-settings', 'Settings'], ['tab-access', 'Access & sign-in'], ['tab-workspaces', 'Workspaces'], ['tab-features', 'Features'], ['tab-ai', 'Investigator'], ['tab-changes', 'Changes']], 'the admin pages keep their tab ids');
  assert.ok(!admin.doc.getElementById('adm-shell').hidden && viewer.doc.getElementById('adm-shell').hidden, 'their sub-nav shows only on an admin page');
  assert.deepStrictEqual(viewer.errors.concat(admin.errors), []);
  viewer.dom.window.close(); admin.dom.window.close();
  return 'the Admin row is hidden for a viewer and shown for an admin, opens the last admin section (General the first time) and marks itself current';
}

async function aPluginPageGetsARowAndASection() {
  //given
  const plugins = [{id: 'demo', name: EVIL, icon: '/plugins/demo/icon.svg', tab: 'demo', passes: [], instances: [], pages: [{tab: 'demo', label: EVIL, group: 'observe', icon: '/plugins/demo/icon.svg'}]}];
  const {dom, w, doc, errors} = await boot({hash: '#t=demo', plugins, listen: true, serverDelay: 120, settle: 500});
  const row = doc.getElementById('tab-demo');
  const observe = doc.querySelector('#snav .s-grp[data-grp="observe"]');

  //then
  assert.ok(row && observe.lastElementChild === row, 'the plug-in row ends its group');
  assert.strictEqual(row.querySelector('.lbl').textContent, EVIL, 'the label is text');
  assert.strictEqual(row.querySelector('.lbl').children.length, 0);
  assert.strictEqual(row.querySelector('.b-plug'), null, 'the row carries no plug-in pill');
  assert.ok(!/plug-in/.test(row.textContent), 'and no plug-in text');
  assert.strictEqual(row.dataset.plugin, '1', 'it is marked as a plug-in row by an attribute');
  assert.ok(!doc.getElementById('tab-svcs').hasAttribute('data-plugin'), 'a core row is not');
  assert.ok(!css(doc).includes('b-plug'), 'the pill style is gone');
  assert.strictEqual(doc.getElementById('count-demo').textContent, '', 'no count until the plug-in sets one');
  const img = row.querySelector('img');
  assert.ok(img && img.getAttribute('src') === '/plugins/demo/icon.svg' && img.getAttribute('alt') === '' && !img.hasAttribute('onerror'), 'the icon is an img');
  assert.strictEqual(doc.querySelectorAll('img[onerror], img[src="x"]').length, 0, 'nothing from the label became markup');
  const section = doc.getElementById('panel-demo');
  assert.ok(section && section.tagName === 'SECTION' && section.classList.contains('panel') && section.closest('main.main'), 'the page has its own section in the main column');
  assert.deepStrictEqual([...(w.__pages || [])], ['demo'], 'the plug-in script is told once that its section is there');
  assert.strictEqual(section.textContent, 'filled by demo');
  assert.ok(!section.hidden && current(doc).join() === 'tab-demo', 'a #t= link that arrived before the plug-in list opens the page once it is known');

  //when
  doc.getElementById('tab-svcs').click();
  row.click();
  const reopened = !section.hidden && doc.getElementById('crumb-page').textContent === EVIL;
  doc.getElementById('side-more').click();
  doc.getElementById('side-more').click();
  w.setPlugins([Object.assign({}, plugins[0], {instances: [{name: 'main', primary: true, pass: 'demo', status: 'ok'}]})]);
  const quiet = {more: doc.getElementById('side-more-list').classList.contains('folded'), current: current(doc).join(), pages: [...(w.__pages || [])].length};
  w.setPlugins([]);
  await wait(20);

  //then
  assert.ok(reopened, 'the row opens the page and the crumb names it as text');
  assert.deepStrictEqual(quiet, {more: true, current: 'tab-demo', pages: 1}, 'a tile refresh with the same pages leaves the folds, the current page and the section alone');
  assert.ok(!doc.getElementById('tab-demo') && !doc.getElementById('panel-demo'), 'a plug-in that stops serving takes its row and page away');
  assert.deepStrictEqual(current(doc), ['tab-anoms'], 'and its open page falls back to the Board');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a plug-in page lands at the end of Observe marked by data-plugin with no pill, its label as text and its icon as an img, gets its own section, and goes away when the plug-in stops serving';
}

async function aPluginPageCannotTakeAPageId() {
  //given
  const plugins = [{id: 'demo', name: 'Demo', icon: '/plugins/demo/icon.svg', tab: null, passes: [], instances: [], pages: [{tab: 'sub-anoms', label: 'Filters', group: 'observe', icon: '/plugins/demo/icon.svg'}, {tab: 'fold-anoms', label: 'Fold', group: 'main', icon: '/plugins/demo/icon.svg'}]}];
  const {dom, doc, errors} = await boot({hash: '#t=anoms', plugins, settle: 500});

  //then
  assert.strictEqual(doc.querySelector('#side [data-tab="sub-anoms"], #side [data-tab="fold-anoms"]'), null, 'a page whose ids the page already uses gets no row');
  assert.deepStrictEqual(['tab-sub-anoms', 'tab-fold-anoms', 'panel-sub-anoms'].map((id) => doc.querySelectorAll('[id="' + id + '"]').length), [1, 1, 0], 'and no second element with those ids');
  assert.ok(doc.getElementById('tab-sub-anoms').closest('#tabs') && doc.querySelector('#tab-sub-anoms button[data-f="open"]'), 'the Board keeps its filter column');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a plug-in page whose tab would reuse an id the page already has gets no row and no section';
}

async function theOverlaysSitOutsideTheContainer() {
  //given
  const {dom, doc, errors} = await boot({hash: '#t=anoms'});
  const ids = ['side-scrim', 'nav-backdrop', 'tipbox', 'cf-wrap', 'json-wrap', 'shot-wrap', 'ask-wrap', 'task-wrap'];

  //then
  assert.deepStrictEqual(ids.filter((id) => !doc.getElementById(id) || doc.getElementById(id).closest('.pane') || doc.getElementById(id).parentNode !== doc.body), [], 'every fixed overlay is a child of the body, outside the content container');
  assert.ok(doc.querySelector('.pane') && doc.querySelector('.pane').contains(doc.querySelector('main.main')));
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the scrim, the backdrops, the Appearance panel, the tip box and the five modals sit at body level, outside the content container';
}

async function theAttributionSitsAtTheFootAndInTheRail() {
  //given
  const {dom, doc, errors} = await boot({hash: '#t=anoms'});
  const link = doc.getElementById('attribution'), mark = doc.querySelector('#side-attr a.attr-mark'), end = doc.querySelector('#end-attr a');

  //then
  assert.ok(link && link.closest('#side-attr'), 'the line sits at the foot of the sidebar');
  assert.deepStrictEqual([link.textContent, link.getAttribute('href'), link.getAttribute('rel')], ['Skopax 0.1.0 \u00b7 skopax.io', 'https://skopax.io', 'noopener noreferrer']);
  assert.deepStrictEqual([mark.getAttribute('aria-label'), mark.getAttribute('title'), mark.getAttribute('href'), mark.getAttribute('rel')], ['Skopax 0.1.0 \u00b7 skopax.io', 'Skopax 0.1.0 \u00b7 skopax.io', 'https://skopax.io', 'noopener noreferrer'], 'the rail shows a mark link named by the line');
  assert.ok(end && !end.id && end.textContent === link.textContent && doc.querySelector('main.main').lastElementChild.id === 'end-attr', 'a phone copy ends the main column');
  assert.strictEqual(visible(doc).indexOf('[[attribution_html]]'), -1, 'no slot text is left');
  assert.strictEqual(doc.querySelectorAll('[id="attribution"]').length, 1);
  assert.deepStrictEqual(errors, []);
  dom.window.close();

  //given
  const hidden = await boot({hash: '#t=anoms', brand: {name: 'Skopax', slug: 'skopax', version: '0.1.0', attribution: null}});

  //then
  assert.strictEqual(hidden.doc.querySelectorAll('a[href="https://skopax.io"]').length, 0, 'a hidden line leaves no link anywhere');
  assert.ok(!/skopax\.io|powered by/.test(visible(hidden.doc)), 'and no text');
  assert.deepStrictEqual(hidden.errors, []);
  hidden.dom.window.close();
  return 'the attribution is the sidebar foot link, a mark link named by it in the rail and a phone copy at the end of the content; a null attribution draws none of them';
}

async function theThemeButtonCyclesTheMode() {
  //given
  const {dom, w, doc, errors} = await boot({hash: '#t=anoms'});
  const btn = doc.getElementById('theme-btn');
  const mode = () => (JSON.parse(w.localStorage.getItem('skopax-appearance') || '{}').mode || 'unset') + ':' + (doc.documentElement.dataset.theme || 'none') + ':' + btn.getAttribute('aria-label');

  //when
  const steps = [mode()];
  for (let i = 0; i < 3; i++) { btn.click(); steps.push(mode()); }
  doc.querySelector('#panel-prefs [data-ap="size"] button[data-v="lg"]').click();
  const kept = JSON.parse(w.localStorage.getItem('skopax-appearance'));

  //then
  assert.deepStrictEqual(steps, ['unset:none:Theme: system. Click to change.', 'light:light:Theme: light. Click to change.', 'dark:dark:Theme: dark. Click to change.', 'system:none:Theme: system. Click to change.']);
  assert.ok(btn.closest('header.top .top-r'), 'it sits in the right corner of the top bar');
  assert.deepStrictEqual([kept.mode, kept.size], ['system', 'lg'], 'Appearance on the Settings page keeps the mode the button set');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the theme button cycles system, light and dark into skopax-appearance and applies it, and Appearance on the Settings page keeps it';
}

async function theChatRowFollowsTheChatAndItsSwitch() {
  //given
  const on = await boot({hash: '#t=anoms'});
  const early = await boot({hash: '#t=anoms', me: {chat: false}, meDelay: 300, settle: 60});
  const earlyRow = !early.doc.getElementById('tab-chat').hidden;
  await wait(500);
  const off = await boot({hash: '#t=anoms', me: {features: {chat: false}}});

  //then
  assert.ok(!on.doc.getElementById('tab-chat').hidden, 'the Chat row shows while the chat is set up');
  assert.ok(earlyRow && early.doc.getElementById('tab-chat').hidden, 'it is there at boot and hides once the login check says there is no chat');
  assert.ok(off.doc.getElementById('tab-chat').hidden, 'it hides while the chat feature is off');
  assert.deepStrictEqual(on.errors.concat(early.errors, off.errors), []);
  on.dom.window.close(); early.dom.window.close(); off.dom.window.close();
  return 'the Chat row shows at boot, and hides when the login check says there is no chat or features.chat is false';
}

async function theTopBarHoldsTheCrumbAndTheLastCheck() {
  //given
  const {dom, doc, errors} = await boot({hash: '#t=anoms', me: {workspaceLabel: 'STAGING'}});
  const top = doc.querySelector('#content > header.top');

  //then
  assert.ok(top, 'the content column starts with the top bar');
  assert.deepStrictEqual([...top.children].map((c) => c.id || c.className), ['side-open', 'crumb', 'off-tag', 'top-find', 'top-r'], 'hamburger, crumb, stopped tag, the search field, then the right cluster');
  assert.strictEqual(doc.getElementById('env-tag'), null, 'the environment label moved into the instance switcher');
  assert.deepStrictEqual([...doc.querySelector('.top-r').children].map((c) => c.id), ['top-find-btn', 'run-pill', 'last-check', 'inv-menu', 'top-bell', 'theme-btn'], 'the search icon leads the right cluster, and the bell sits before the theme button');
  assert.match(doc.getElementById('last-check').textContent, /^Last check(\d\d:\d\d|\u2013)$/);
  assert.strictEqual(doc.querySelector('.top .live'), null, 'the Activity rail keeps the live class to itself');
  assert.ok(doc.getElementById('meta').parentNode.classList.contains('overview'), 'the source pills sit in the overview card');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the top bar holds the hamburger, the crumb, the stopped tag and a right cluster with Last check, the investigator menu and the theme button; the source pills sit in the overview card';
}

async function theFilterColumnStartsFoldedWhenTheContentIsNarrow() {
  //given
  const narrow = await boot({hash: '#t=anoms', width: 1280});
  const wide = await boot({hash: '#t=anoms', width: 1600});
  const kept = await boot({hash: '#t=anoms', width: 1280, stored: {'skopax-nav-off': '0'}});
  const plain = await boot({hash: '#t=anoms'});
  const between = await boot({hash: '#t=anoms', width: 1400});
  const railed = await boot({hash: '#t=anoms', width: 1400, stored: {'skopax-sidebar': 'rail'}});
  const off = (x) => x.doc.querySelector('.layout').classList.contains('nav-off');
  const before = off(between);
  between.doc.getElementById('side-rail').click();
  const afterRail = off(between);

  //then
  assert.deepStrictEqual([off(narrow), off(wide), off(kept), off(plain)], [true, false, false, false], 'folded under 1240 px of content, open when wider, the stored choice wins, and no media query keeps today\'s default');
  assert.deepStrictEqual([before, off(railed), afterRail], [true, false, false], 'at 1400 px the full sidebar leaves under 1240 px of content and the rail leaves more, also right after the toggle');
  assert.deepStrictEqual(narrow.errors.concat(wide.errors, kept.errors, plain.errors, between.errors, railed.errors), []);
  for (const x of [narrow, wide, kept, plain, between, railed]) x.dom.window.close();
  return 'the filter column starts folded while the content is under 1240 px wide, unless the reader chose otherwise';
}

(async () => {
  console.log('PASS ' + await theSidebarListsTheMapInOrder());
  console.log('PASS ' + await aRowOpensItsPageAndMarksItself());
  console.log('PASS ' + await moreFoldsAndOpensForItsRows());
  console.log('PASS ' + await openingMoreScrollsItsRowsIntoView());
  console.log('PASS ' + await theNavFadesWhileRowsAreBelowTheFold());
  console.log('PASS ' + await theRailIsRememberedAndShowsLabelsAsTips());
  console.log('PASS ' + await aRailTipCarriesTheRowBadges());
  console.log('PASS ' + await onAPhoneTheHamburgerOpensTheDrawer());
  console.log('PASS ' + await theAdminRowShowsForAdminsOnly());
  console.log('PASS ' + await aPluginPageGetsARowAndASection());
  console.log('PASS ' + await aPluginPageCannotTakeAPageId());
  console.log('PASS ' + await theOverlaysSitOutsideTheContainer());
  console.log('PASS ' + await theAttributionSitsAtTheFootAndInTheRail());
  console.log('PASS ' + await theThemeButtonCyclesTheMode());
  console.log('PASS ' + await theChatRowFollowsTheChatAndItsSwitch());
  console.log('PASS ' + await theTopBarHoldsTheCrumbAndTheLastCheck());
  console.log('PASS ' + await theFilterColumnStartsFoldedWhenTheContentIsNarrow());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
