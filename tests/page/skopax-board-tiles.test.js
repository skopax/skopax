const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const NOW = Date.now();
const iso = (min) => new Date(NOW - min * 60000).toISOString();
const WAITING = 'log|checkout-service|ERROR|boom';
const NOSVC = 'k8s|-|KubePodNotReady|pods';
const NOSVC_BARE = 'k8s|-|DiskPressure|bare';
const EVENT = 'log|push-service|ERROR|send';
const CLUSTER = 'k8s|-|NodeNotReady|node';
const PLAIN = 'log|shop-web|ERROR|plain';
const CRIT = 'log|shop-web|ERROR|crit';
const HIGH = 'log|shop-web|ERROR|high';
const LONG_NAME = 'Alexandria Montgomery-Featherstonehaugh';
const WORDS = {
  en: {long: 'Stopped firing \u00b7 awaiting action \u00b7 assigned to ' + LONG_NAME},
  tr: {long: 'Sinyal durdu \u00b7 aksiyon bekleniyor \u00b7 atanan: ' + LONG_NAME},
};

function card(key, num, extra) {
  return Object.assign({key, num, sev: 'warn', status: 'open', svc: key.split('|')[1], label: key.split('|')[2], src: 'logs', sig: 'log', now: 1, firstSeen: iso(90), lastSeen: iso(1), what: 'text'}, extra || {});
}
function cards() {
  return [card(WAITING, 41, {status: 'cleared', clearedAt: iso(20), owner: 'alexandria.montgomery@example.com', ownerName: LONG_NAME}),
    card(NOSVC, 42, {svc: '-', ns: 'shop', sub: 'checkout-pod'}),
    card(NOSVC_BARE, 43, {svc: '', ns: '-'}),
    card(EVENT, 44, {status: 'cleared', clearedAt: iso(25), cause: {kind: 'event', what: 'the push send', firstSeen: iso(90)}}),
    card(CLUSTER, 45, {svc: '-', ns: '-', cluster: 'a1'}),
    card(PLAIN, 46),
    card(CRIT, 47, {sev: 'crit'}),
    card(HIGH, 48, {sev: 'high'})];
}
function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: iso(-59), sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: cards(), services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(lang, view) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE, {view: view || 'board'}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state())), {
    url: 'https://skopax.example.com/#t=anoms/all', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      if (lang !== 'en') {
        const raw = JSON.parse(fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'locale', lang, 'page.json'), 'utf8'));
        w.SKOPAX_I18N = {[lang]: Object.assign({}, raw.page, {'@rules': raw.rules, '@months': raw.months, '@days': raw.days, '@tails': raw.tails, '@th': raw.headers})};
        w.document.cookie = 'skopax_lang=' + lang + '; path=/';
      }
      w.scrollTo = () => {}; w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        p = String(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'op@example.com', operator: true, teams: [], tags: []});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/state')) return ok(state());
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors};
}

const tileOf = (doc, key) => [...doc.querySelectorAll('#anoms .tile')].find((t) => t.dataset.key === key);

async function theTileHoldsNoStatusTagAndTheDrawerKeepsTheLongForm(lang) {
  //given
  const words = WORDS[lang];
  const {doc, errors} = await boot(lang);

  //when
  const footTags = [WAITING, EVENT, NOSVC].map((k) => tileOf(doc, k).querySelectorAll('.tile-foot .tag').length);
  const topLine = [...tileOf(doc, WAITING).querySelectorAll('.tile-top > *')].map((e) => e.className.split(' ')[0]);
  const initials = tileOf(doc, WAITING).querySelector('.tile-top .tile-asg').textContent;
  tileOf(doc, WAITING).querySelector('.tile-open').click();
  await wait(60);
  const drawerStatus = doc.querySelector('.card-drawer .inc-tags .tag.k-cleared').textContent;
  const drawer = doc.querySelector('.card-drawer');
  assert.strictEqual(drawer.getAttribute('aria-label'), 'checkout-service / ERROR');
  drawer.querySelector('.cd-close').click();
  tileOf(doc, NOSVC).querySelector('.tile-open').click();
  await wait(60);
  const bareLabel = doc.querySelector('.card-drawer').getAttribute('aria-label');

  //then
  assert.deepStrictEqual(footTags, [0, 0, 0], 'the tile footer holds no stopped or investigated tag');
  assert.deepStrictEqual(topLine, ['sev', 'tile-num', 'tile-age', 'tile-asg'], 'the top line has severity, number, age and the owner');
  assert.strictEqual(initials, 'AM', 'owner initials');
  assert.ok(drawerStatus.startsWith(words.long + ' '), 'the tag row in the drawer keeps the long form: ' + drawerStatus);
  assert.strictEqual(bareLabel, 'shop / KubePodNotReady', 'the drawer of a no-service card never says a bare dash');
  assert.deepStrictEqual(errors, []);
  return lang;
}

async function noServiceNeverShowsABareDash() {
  //given
  const {doc, errors} = await boot('en');

  //when
  const titled = tileOf(doc, NOSVC), bare = tileOf(doc, NOSVC_BARE);
  const heads = [...doc.querySelectorAll('#anoms .tile-svc')].map((e) => e.textContent);

  //then
  assert.strictEqual(titled.querySelector('.tile-svc').textContent, 'shop', 'the namespace stands in for the service');
  assert.strictEqual(titled.querySelector('.tile-sig').textContent, 'KubePodNotReady');
  assert.strictEqual(bare.querySelector('.tile-svc').textContent, 'DiskPressure', 'the label stands in when nothing else is known');
  assert.ok(heads.every((h) => h.trim() !== '-' && h.trim() !== ''), 'no bare dash: ' + heads.join(' | '));
  assert.ok(![...doc.querySelectorAll('#anoms .tile-open, #anoms .tile')].some((e) => /(^|\s)-\s\u00b7/.test(e.textContent)), 'no dash in the tile text');
  const cluster = tileOf(doc, CLUSTER);
  assert.strictEqual(cluster.querySelector('.tile-svc').textContent, 'instance a1');
  assert.ok(!/instance/.test(cluster.querySelector('.tile-sig').textContent), 'the instance is not repeated: ' + cluster.querySelector('.tile-sig').textContent);
  assert.deepStrictEqual(errors, []);
}

async function theSharedNameIsUsedInTheSearchTheGroupListAndTheClosedList() {
  //given
  const {w, doc, errors} = await boot('en', 'list');

  //when
  doc.getElementById('search-btn').click();
  await wait(60);
  const rows = [...doc.querySelectorAll('#pal-list [role="option"] .lbl')].map((o) => o.textContent);
  w.document.getElementById('pal').hidden = true;
  const article = doc.querySelector('#anoms article.inc[data-key="' + PLAIN + '"]');
  article.querySelector('.inc-title .fold').click();
  await wait(30);
  const combo = article.querySelector('input[role="combobox"]');
  combo.dispatchEvent(new w.Event('focus'));
  combo.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true}));
  await wait(30);
  const options = [...article.querySelectorAll('[role="listbox"] [role="option"]')].map((o) => o.textContent);
  const closed = w.closedRow({key: NOSVC, num: 42, svc: '-', label: 'KubePodNotReady', sev: 'warn', outcome: 'fixed', ns: 'shop'});
  const bare = w.closedRow({key: 'k8s|-|x', sev: 'warn', outcome: 'fixed'});

  //then
  assert.ok(rows.some((r) => r === '#42 shop KubePodNotReady'), 'the search row: ' + rows.join(' | '));
  assert.ok(!rows.some((r) => /(^|\s)-\s/.test(r)), 'no bare dash in the search');
  assert.ok(options.some((o) => o.indexOf('shop') >= 0 && o.indexOf('KubePodNotReady') >= 0) && !options.some((o) => /(^|\s)-(\s|$)/.test(o)), 'the Group list: ' + options.join(' | '));
  assert.strictEqual(closed.querySelector('.cl-svc').textContent, 'shop');
  assert.notStrictEqual(bare.querySelector('.cl-svc').textContent.trim(), '-');
  assert.notStrictEqual(bare.querySelector('.cl-svc').textContent.trim(), '');
  assert.deepStrictEqual(errors, []);
}

function cssRule(css, selector) {
  const out = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m;
  while ((m = re.exec(css))) if (m[1].split(',').map((s) => s.trim()).indexOf(selector) >= 0) out.push(m[2]);
  return out.join(' ');
}

function tilesWrapInsideTheColumn() {
  //given
  const css = fs.readFileSync(TEMPLATE, 'utf8');

  //then
  assert.match(cssRule(css, '.tile'), /min-width:\s*0/);
  assert.match(cssRule(css, '.tile'), /grid-template-columns:\s*minmax\(0,\s*1fr\)/);
  assert.match(cssRule(css, '.tile-foot'), /min-width:\s*0/);
  assert.match(cssRule(css, '.tile-foot'), /flex-wrap:\s*wrap/);
  const tag = cssRule(css, '.tile-foot .tag');
  assert.match(tag, /white-space:\s*normal/);
  assert.match(tag, /max-width:\s*100%/);
  assert.match(tag, /min-width:\s*0/);
  assert.match(tag, /overflow-wrap:\s*anywhere/);
}

async function anOffBoardCardInThePanelNeverShowsABareDash() {
  //given
  const {w, doc, errors} = await boot('en');
  const c = {num: 142, key: 'k8s|-|KubePodNotReady|old', svc: '-', ns: 'shop', label: 'KubePodNotReady', sev: 'warn', status: 'cleared', live: false, firstSeen: iso(3000), now: 1, d1: 0, d7: 0};
  const bare = Object.assign({}, c, {num: 143, key: 'k8s|-|DiskPressure|old', svc: '', ns: '-', label: 'DiskPressure'});
  const real = w.fetch;
  w.fetch = async (p, init) => {
    const hit = [c, bare].find((x) => String(p) === '/api/cards/' + x.num);
    return hit ? {ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(hit))} : real(p, init);
  };

  //when
  w.location.hash = '#c=142';
  await wait(100);
  const named = doc.querySelector('.card-drawer');
  const namedLabel = named.getAttribute('aria-label'), namedSvc = named.querySelector('.inc-title .svc').textContent;
  w.location.hash = '#c=143';
  await wait(100);
  const alone = doc.querySelector('.card-drawer');

  //then
  assert.strictEqual(namedLabel, 'shop / KubePodNotReady');
  assert.strictEqual(namedSvc, 'shop');
  assert.strictEqual(alone.querySelector('.inc-title .svc').textContent, 'DiskPressure', 'the label stands in when nothing else is known');
  assert.ok(!/(^|\s)-\s(\/|\u00b7)/.test(alone.querySelector('.inc-title').textContent), alone.querySelector('.inc-title').textContent);
  assert.deepStrictEqual(errors, []);
}

function themeBlocks(css) {
  const cut = (marker, end) => {
    const at = css.indexOf(marker);
    assert.ok(at >= 0, 'missing block ' + marker);
    return css.slice(at, css.indexOf(end || '\n}', at));
  };
  return {
    light: cut('\n:root {'),
    darkMedia: cut('\n  :root:not([data-theme="light"]) {\n', '\n  }'),
    darkTheme: cut('\n:root[data-theme="dark"] {'),
  };
}
function channel(v) { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }
function luminance(hex) {
  const n = parseInt(hex.slice(1), 16);
  return 0.2126 * channel(n >> 16) + 0.7152 * channel((n >> 8) & 255) + 0.0722 * channel(n & 255);
}
function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}
function token(block, name) {
  const m = new RegExp('(?:^|[\\s;])' + name + ':\\s*([^;]+);').exec(block);
  return m ? m[1].trim() : null;
}

function theCriticalTokensLiveInEveryThemeBlockAndTheLightTintKeepsContrast() {
  //given
  const css = fs.readFileSync(TEMPLATE, 'utf8');
  const blocks = themeBlocks(css);

  //when
  const light = blocks.light;
  const stops = token(light, '--crit-tile-bg').match(/#[0-9a-fA-F]{6}/g);

  //then
  for (const [name, block] of Object.entries(blocks)) {
    for (const t of ['--crit-glow', '--crit-tile-bg', '--crit-tile-line']) assert.ok(token(block, t), name + ' block defines ' + t);
  }
  assert.strictEqual(stops.length, 2, 'the light tint has two gradient stops');
  for (const stop of stops) {
    for (const fg of ['--fg', '--muted']) {
      const ratio = contrast(token(light, fg), stop);
      assert.ok(ratio >= 4.5, fg + ' on ' + stop + ' is ' + ratio.toFixed(2));
    }
  }
}

async function criticalTilesGlowAndHighTilesDoNot() {
  //given
  const css = fs.readFileSync(TEMPLATE, 'utf8');
  const {doc, errors} = await boot('en');

  //when
  const crit = tileOf(doc, CRIT), high = tileOf(doc, HIGH);
  const rule = cssRule(css, '.tile.k-crit');

  //then
  assert.ok(crit.classList.contains('k-crit') && !high.classList.contains('k-crit'), 'only the critical tile carries k-crit');
  assert.match(rule, /box-shadow:\s*var\(--crit-glow\)/);
  assert.match(rule, /background:\s*var\(--crit-tile-bg\)/);
  assert.match(rule, /border-color:\s*var\(--crit-tile-line\)/);
  assert.ok(!/animation/.test(rule), 'the glow is steady');
  assert.strictEqual(cssRule(css, '.tile.k-high'), '', 'a high tile gets no glow rule');
  assert.ok(!/\.(inc|cl-row|spill|sev|tag)[^{]*\.k-crit[^{]*\{[^}]*crit-glow/.test(css), 'list rows, chips and the badge take no glow');
  assert.deepStrictEqual(errors, []);
}

function aCriticalTileKeepsHoverFeedback() {
  //given
  const css = fs.readFileSync(TEMPLATE, 'utf8');

  //when
  const hover = cssRule(css, '.tile.k-crit:hover');

  //then
  assert.match(hover, /border-color:\s*var\(--crit-tile-line-hover\)/);
  const blocks = themeBlocks(css);
  for (const [name, block] of Object.entries(blocks)) assert.ok(token(block, '--crit-tile-line-hover'), name + ' block defines the hover border');
}

async function aSelectedCriticalTileKeepsTheRingAndTheGlow() {
  //given
  const css = fs.readFileSync(TEMPLATE, 'utf8');
  const {doc, errors} = await boot('en');

  //when
  tileOf(doc, CRIT).querySelector('.tile-open').click();
  await wait(60);
  const selected = tileOf(doc, CRIT);
  const rule = cssRule(css, '.tile.k-crit.on');

  //then
  assert.ok(selected.classList.contains('on') && selected.classList.contains('k-crit'));
  assert.match(rule, /box-shadow:\s*0 0 0 2px var\(--ring\),\s*var\(--crit-glow\)/);
  assert.ok(!/var\(--ring\)/.test(cssRule(css, '.tile.k-crit')), 'an unselected critical tile shows no ring');
  assert.deepStrictEqual(errors, []);
}

function onlyOneTooltipShowsOnATile(doc) {
  const tile = tileOf(doc, WAITING);
  const badge = tile.querySelector('.tile-top .tile-asg');
  const chain = [];
  for (let e = badge; e && e !== doc.body; e = e.parentElement) if (e.hasAttribute('title')) chain.push(e.className.split(' ')[0] || e.tagName);
  return {tile, badge, chain};
}

async function hoveringABadgeShowsOnlyTheBadgeTooltip() {
  //given
  const {doc, errors} = await boot('en');

  //when
  const {tile, badge, chain} = onlyOneTooltipShowsOnATile(doc);
  const hint = tile.querySelector('.tile-open').getAttribute('title');

  //then
  assert.match(badge.getAttribute('title'), /^Assigned to /);
  assert.deepStrictEqual(chain, ['tile-asg'], 'only the badge carries a tooltip on its path: ' + chain.join(' > '));
  assert.ok(!tile.hasAttribute('title'), 'the tile itself carries no title');
  assert.match(hint, / opens the card/, 'the hint lives on the tile body');
  assert.ok(!tile.querySelector('.tile-open').contains(badge), 'the badge is outside the tile body');
  assert.deepStrictEqual(errors, []);
}

(async () => {
  const jobs = [
    ['tile tag en', () => theTileHoldsNoStatusTagAndTheDrawerKeepsTheLongForm('en')],
    ['tile tag tr', () => theTileHoldsNoStatusTagAndTheDrawerKeepsTheLongForm('tr')],
    ['no service', noServiceNeverShowsABareDash],
    ['shared name', theSharedNameIsUsedInTheSearchTheGroupListAndTheClosedList],
    ['tile css', async () => tilesWrapInsideTheColumn()],
    ['off-board panel title', anOffBoardCardInThePanelNeverShowsABareDash],
    ['critical glow', criticalTilesGlowAndHighTilesDoNot],
    ['selected critical', aSelectedCriticalTileKeepsTheRingAndTheGlow],
    ['one tooltip', hoveringABadgeShowsOnlyTheBadgeTooltip],
    ['theme blocks and contrast', async () => theCriticalTokensLiveInEveryThemeBlockAndTheLightTintKeepsContrast()],
    ['critical hover', async () => aCriticalTileKeepsHoverFeedback()],
  ];
  for (const [name, job] of jobs) {
    try { await job(); console.log('PASS ' + name); } catch (e) {
      console.error('FAIL ' + name + ': ' + e.message.replace(/\x1b\[[0-9;]*m/g, '').split('\n').slice(0, 12).join(' | ')); process.exitCode = 1;
    }
  }
  process.exit(process.exitCode || 0);
})();
