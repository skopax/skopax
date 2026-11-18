const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [{key: 'log|checkout-service|ERROR|boom', num: 14, sev: 'warn', status: 'open', svc: 'checkout-service', label: 'ERROR lines', src: 'logs', sig: 'log', now: 1, firstSeen: iso(30), lastSeen: iso(1)}],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(hash, narrow) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state()));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/' + hash, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      if (narrow) w.matchMedia = (q) => ({matches: [...String(q).matchAll(/\((min|max)-width:\s*(\d+)px\)/g)].every((m) => m[1] === 'min' ? narrow >= +m[2] : narrow <= +m[2]) && /width/.test(q), media: q, addEventListener() {}});
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'nina.white@example.com', operator: true, teams: [], tags: []});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(450);
  return {w: dom.window, doc: dom.window.document, errors};
}

function allRules(doc) {
  const out = [];
  const walk = (list, media) => { for (const r of list) { if (r.selectorText) out.push({sel: r.selectorText, media, style: r.style}); else if (r.cssRules) walk(r.cssRules, (r.conditionText || (r.media && r.media.mediaText) || '').replace(/\s/g, '')); } };
  for (const sh of doc.styleSheets) walk(sh.cssRules, '');
  return out;
}

async function theHideButtonSitsAtTheTopOfTheMenuAndAFoldedMenuKeepsAStrip() {
  //given
  const {w, doc, errors} = await boot('#t=anoms');
  const layout = doc.querySelector('.layout');
  const btn = doc.getElementById('nav-fold');
  for (let i = 0; i < 40 && !layout.classList.contains('nav-anim'); i++) await wait(25);
  const before = {inMenu: btn.parentNode.id === 'tabs', first: btn.parentNode.firstElementChild === btn, text: btn.textContent, expanded: btn.getAttribute('aria-expanded'), title: btn.title, armed: layout.classList.contains('nav-anim')};

  //when
  doc.querySelector('#tab-sub-anoms button').focus();
  btn.click();
  const folded = {off: layout.classList.contains('nav-off') && doc.body.classList.contains('nav-off'), text: btn.textContent, expanded: btn.getAttribute('aria-expanded'), title: btn.getAttribute('title') || btn.dataset.tip,
    focus: doc.activeElement === btn, same: doc.getElementById('nav-fold') === btn && btn.parentNode.id === 'tabs'};
  btn.click();
  const opened = {off: layout.classList.contains('nav-off'), text: btn.textContent, expanded: btn.getAttribute('aria-expanded'), title: btn.title};

  //then
  assert.deepStrictEqual(before, {inMenu: true, first: true, text: 'Hide menu', expanded: 'true', title: 'Hide the menu', armed: true}, 'the hide icon leads the side menu, named for screen readers, and the slide is armed after the first frame');
  assert.deepStrictEqual(folded, {off: true, text: 'Menu', expanded: 'false', title: 'Show the menu', focus: true, same: true}, 'one click folds the menu to a strip that keeps the same button, now reading Menu, with the focus on it');
  assert.deepStrictEqual(opened, {off: false, text: 'Hide menu', expanded: 'true', title: 'Hide the menu'}, 'the strip opens it again');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'Hide menu sits at the top of the side menu; a folded menu leaves a strip whose Menu button opens it again and keeps the focus';
}

async function theMenuFoldsToAStripWithoutAHeaderArrow() {
  //given
  const {w, doc, errors} = await boot('#t=anoms');
  const rules = allRules(doc);
  const wide = (sel) => rules.filter((r) => r.sel === sel && r.media === 'content(min-width:901px)').map((r) => r.style);
  const narrow = (sel) => rules.filter((r) => r.sel === sel && r.media === 'content(max-width:900px)').map((r) => r.style);

  //when
  const off = wide('.layout.nav-off .tabs')[0];
  const rest = wide('.layout.nav-off .tabs > :not(.nav-fold)')[0];
  const strip = wide('.layout.nav-off .tabs > .nav-fold')[0];
  const sliding = wide('.layout.nav-anim .tabs')[0].getPropertyValue('transition');
  const calm = rules.filter((r) => r.media === '(prefers-reduced-motion:reduce)' && /\.layout\.nav-anim \.tabs/.test(r.sel)).map((r) => r.style.getPropertyValue('transition'));
  const drawer = narrow('.tabs').map((s) => s.getPropertyValue('transition'))[0];

  //then
  assert.strictEqual(off.getPropertyValue('width'), '2.5rem', 'a folded menu keeps a narrow strip');
  assert.strictEqual(rest.getPropertyValue('display'), 'none', 'with only the Menu button in it');
  assert.strictEqual(strip.getPropertyValue('position'), 'static', 'which keeps the same icon, now in the strip');
  assert.strictEqual(wide('.tabs > .nav-fold')[0].getPropertyValue('position'), 'absolute', 'while the menu is open the icon sits on the Anomaly filters row');
  assert.ok(/width/.test(sliding), 'the width still slides');
  assert.deepStrictEqual(calm, ['none'], 'reduced motion turns the slide off');
  assert.strictEqual(doc.getElementById('nav-open'), null, 'there is no header arrow any more: the sidebar hamburger and the Filters button take its place');
  assert.strictEqual(narrow('.nav-fold')[0].getPropertyValue('display'), 'none', 'a narrow content keeps the drawer, without Hide menu');
  assert.ok(/transform/.test(drawer) && /visibility 0s linear 0?\.32s/.test(drawer), 'the phone drawer slides as before');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the folded menu is a 2.5rem strip holding only the show icon, the width slides unless reduced motion is on, the rules follow the content width, and there is no header arrow';
}

async function aNarrowContentStacksTheActivityRailUnderTheMainColumnOnEveryPage() {
  //given
  const {w, doc, errors} = await boot('#t=slack');
  const rules = allRules(doc);
  const cols = (r) => r.style.getPropertyValue('grid-template-columns').replace(/\s/g, '');
  const single = 'minmax(0,1fr)';

  //when
  const variants = rules.filter((r) => r.media === '' && cols(r)).flatMap((r) => r.sel.split(',').map((x) => ({sel: x.trim(), cols: cols(r)}))).filter((r) => /^\.layout(\.[\w-]+)*$/.test(r.sel));
  const wideOnes = variants.filter((r) => r.cols !== single).map((r) => r.sel);
  const stacked = rules.filter((r) => r.media === 'content(max-width:900px)' && cols(r) === single).flatMap((r) => r.sel.split(',').map((x) => x.trim()));

  //then
  assert.ok(doc.querySelector('.layout').classList.contains('no-nav'), 'Notifications has no side menu');
  assert.ok(wideOnes.includes('.layout.no-nav') && wideOnes.includes('.layout'), 'the wide layouts keep a column for the Activity rail: ' + wideOnes.join(' '));
  assert.deepStrictEqual(wideOnes.filter((sel) => !stacked.includes(sel)), [], 'under 900 px of content every layout variant is one column, so the rail never squeezes or touches the banner and the strip');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'under 900 px of content every layout variant, with or without the side menu, is one column, so the Activity rail sits under the main column';
}

async function onAPhoneTheFiltersButtonOpensTheDrawer() {
  //given
  const {w, doc, errors} = await boot('#t=anoms', 600);
  const btn = () => doc.getElementById('filter-menu');
  const closed = {expanded: btn().getAttribute('aria-expanded'), controls: btn().getAttribute('aria-controls'), crumb: doc.getElementById('crumb-page').textContent};

  //when
  btn().click();
  const open = {drawer: doc.body.classList.contains('nav-open'), expanded: btn().getAttribute('aria-expanded'), backdrop: !doc.getElementById('nav-backdrop').hidden};
  doc.getElementById('nav-backdrop').click();
  const shut = {drawer: doc.body.classList.contains('nav-open'), expanded: btn().getAttribute('aria-expanded')};

  //then
  assert.deepStrictEqual(closed, {expanded: 'false', controls: 'tab-sub-anoms', crumb: 'Board'}, 'on a phone the crumb names the open section and the Filters button starts closed');
  assert.deepStrictEqual(open, {drawer: true, expanded: 'true', backdrop: true});
  assert.deepStrictEqual(shut, {drawer: false, expanded: 'false'});
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'on a phone the Board\'s Filters button opens the filter drawer and the backdrop closes it';
}

(async () => {
  console.log('PASS ' + await theHideButtonSitsAtTheTopOfTheMenuAndAFoldedMenuKeepsAStrip());
  console.log('PASS ' + await theMenuFoldsToAStripWithoutAHeaderArrow());
  console.log('PASS ' + await onAPhoneTheFiltersButtonOpensTheDrawer());
  console.log('PASS ' + await aNarrowContentStacksTheActivityRailUnderTheMainColumnOnEveryPage());
})().catch((e) => { console.error('FAIL', e); process.exit(1); });
