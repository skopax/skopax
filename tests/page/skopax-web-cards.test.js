const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

function card(sig, src, sub, extra) {
  return Object.assign({key: sig + '|-/shop.example.com|' + sub, src, sig, ns: '-', svc: 'shop.example.com', sub, label: sig, sev: 'warn', status: 'open', now: 1, num: 7,
    firstSeen: iso(60), lastSeen: iso(1), what: 'Part of a box is cut off.'}, extra || {});
}

function state(anomalies) {
  const shot = {id: 'aa11bb22cc33dd44', viewport: 'phone', url: 'https://shop.example.com/cut', at: iso(2), width: 390, height: 700};
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {pagescan: 'ok'},
    summary: {level: 'warn', headline: 'Pages', points: [], at: iso(1)},
    anomalies: anomalies || [card('page-layout', 'pagescan', 'cut', {screenshots: [shot]}), card('ad-disapproved', 'gads-policy', 'ad-1', {num: 8}), card('access', 'crux', 'origins', {num: 9})],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(anomalies) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state(anomalies)));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=anoms/all', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: null, operator: false});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/investigations')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(300);
  return {w: dom.window, doc: dom.window.document, errors};
}

async function webAndMarketingCardTypesShowNamesNotRawIds() {
  //given
  const {w, doc, errors} = await boot();

  //when
  const names = ['ad-disapproved', 'access', 'gtm-publish', 'browser-down', 'umami-access', 'safebrowsing', 'crux'].map((t) => w.typeName(t));
  const access = ['access', 'no-access'].map((t) => w.typeName(t));
  const chip = doc.querySelector('#tab-sub-anoms button[data-type="ad-disapproved"]');

  //then
  assert.deepStrictEqual(names, ['Disapproved ads', 'No access', 'GTM publishes', 'Screenshot browser', 'No access', 'Safe Browsing', 'Page speed']);
  assert.ok(chip && chip.textContent.includes('Disapproved ads'), 'the type chip shows the name');
  assert.deepStrictEqual(access, ['No access', 'No access'], 'every access type reads as one');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS web and marketing card types show names, not raw ids');
}

async function theCardDrawerShowsThePageScanPictures() {
  //given
  const {w, doc, errors} = await boot();

  //when
  w.openDrawer('page-layout|-/shop.example.com|cut', true);
  await wait(50);
  const img = doc.querySelector('.card-drawer .card-shots img');
  const other = doc.querySelectorAll('.card-drawer .card-shots').length;

  //then
  assert.ok(img, 'the drawer shows the card picture');
  assert.strictEqual(img.getAttribute('src'), '/api/screenshots/aa11bb22cc33dd44');
  assert.match(img.getAttribute('alt'), /Screenshot of https:\/\/shop\.example\.com\/cut, phone/);
  assert.strictEqual(other, 1);
  w.closeDrawer();
  w.openDrawer('ad-disapproved|-/shop.example.com|ad-1', true);
  await wait(50);
  assert.strictEqual(doc.querySelectorAll('.card-drawer .card-shots').length, 0, 'a card without pictures shows none');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the card drawer shows the page scan pictures');
}

async function everyAccessTypeFoldsIntoOneNoAccessChipAndGroup() {
  //given
  const list = [card('no-access', 'capacity', 'volumes', {num: 1}), card('access', 'crux', 'origins', {num: 2}), card('umami-access', 'umami', 'site', {num: 3}),
    card('ad-disapproved', 'gads-policy', 'ad-1', {num: 4})];
  const {w, doc, errors} = await boot(list);

  //when
  const chips = [...doc.querySelectorAll('#tab-sub-anoms button[data-type]')].map((b) => b.dataset.type);
  const folded = doc.querySelector('#tab-sub-anoms button[data-type="no-access"]');
  const groups = [...w.typeGroups(w.ALL)].map((g) => [g.type, g.list.length]);
  const oldLink = [...w.ALL].filter((a) => w.typeIs(a, 'access')).length;

  //then
  assert.deepStrictEqual(chips.filter((t) => /access/.test(t)), ['no-access'], 'one access chip: ' + chips.join(', '));
  assert.ok(folded.textContent.includes('No access') && folded.textContent.includes('3'), folded.textContent);
  assert.deepStrictEqual(groups, [['no-access', 3], ['ad-disapproved', 1]]);
  assert.strictEqual(oldLink, 3, 'an older link to a narrower access type opens the folded group');
  assert.deepStrictEqual([...w.ALL].map((a) => a.sig).sort(), ['access', 'ad-disapproved', 'no-access', 'umami-access'], 'the card types themselves stay as they are');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS every access type folds into one No access chip and group');
}

(async () => {
  await webAndMarketingCardTypesShowNamesNotRawIds();
  await theCardDrawerShowsThePageScanPictures();
  await everyAccessTypeFoldsIntoOneNoAccessChipAndGroup();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
