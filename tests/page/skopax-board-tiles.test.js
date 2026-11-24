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
    card(PLAIN, 46)];
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
  const drawerStatus = doc.querySelector('.card-drawer .c-status').textContent;
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
  assert.ok(drawerStatus.startsWith(words.long + ' '), 'the drawer keeps the long form: ' + drawerStatus);
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

(async () => {
  const jobs = [
    ['tile tag en', () => theTileHoldsNoStatusTagAndTheDrawerKeepsTheLongForm('en')],
    ['tile tag tr', () => theTileHoldsNoStatusTagAndTheDrawerKeepsTheLongForm('tr')],
    ['no service', noServiceNeverShowsABareDash],
    ['shared name', theSharedNameIsUsedInTheSearchTheGroupListAndTheClosedList],
    ['tile css', async () => tilesWrapInsideTheColumn()],
    ['off-board panel title', anOffBoardCardInThePanelNeverShowsABareDash],
  ];
  for (const [name, job] of jobs) {
    try { await job(); console.log('PASS ' + name); } catch (e) {
      console.error('FAIL ' + name + ': ' + e.message.replace(/\x1b\[[0-9;]*m/g, '').split('\n').slice(0, 12).join(' | ')); process.exitCode = 1;
    }
  }
  process.exit(process.exitCode || 0);
})();
