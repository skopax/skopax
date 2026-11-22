const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const BUILT = ['example.com', 'support.google.com', 'developers.google.com', 'firebase.google.com', 'cloud.google.com', 'status.cloud.google.com', 'blog.google', 'developer.apple.com', 'developer.android.com', 'kubernetes.io'];

function entry(key, label, extra) {
  return Object.assign({key, group: 'Investigator', label, help: label, kind: 'text', live: true, value: '', base: '', default: '', source: 'default', by: null, at: null, limit: 1000}, extra);
}

async function boot(items, pack) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/#t=ai', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      if (pack) { w.SKOPAX_I18N = pack; w.document.cookie = 'skopax_lang=' + Object.keys(pack)[0] + '; path=/'; }
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/admin')) {
          if (p === '/api/admin/notices') return ok({items: []});
          if (p === '/api/admin/features') return ok({edition: 'community', items: []});
          if (p === '/api/admin/plugins') return ok({plugins: []});
          if (p.startsWith('/api/admin/history')) return ok([]);
          return ok({items, rev: 3, refreshSeconds: 15});
        }
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'nina.white@example.com', operator: true, admin: true, teams: [], tags: []});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/nodes')) return ok({at: iso(0), periodS: 60, configured: [], nodes: []});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors};
}

const row = (doc, key) => doc.querySelector('#ai-rows .adm-row[data-key="' + key + '"]');
const chips = (r) => [...r.querySelectorAll('.adm-chip')];

async function theWebSitesSettingShowsTheBuiltInListWithYourDomainFirstAndMarksTheOnesYouLeftOut() {
  //given
  const {w, doc, errors} = await boot([entry('investigate.web-domains', 'Web sites', {value: '-blog.google, example.org', builtIn: BUILT, ownDomains: ['example.com']})]);
  const r = row(doc, 'investigate.web-domains');

  //then
  assert.ok(r.querySelector('.adm-built'), 'the built-in list shows under the setting');
  const shown = chips(r).filter((c) => !c.hidden).map((c) => c.textContent);
  assert.strictEqual(shown.length, 8, 'a compact list of eight');
  assert.strictEqual(shown[0], 'example.com \u00b7 your website');
  assert.strictEqual(chips(r)[0].title, 'One of your websites');
  const removed = chips(r).filter((c) => c.classList.contains('removed')).map((c) => c.textContent);
  assert.deepStrictEqual(removed, ['blog.google'], 'the host written with a dash is marked left out');
  assert.strictEqual(chips(r).find((c) => c.textContent === 'blog.google').title, 'Left out by -blog.google');

  //when
  const more = r.querySelector('.adm-built-more');
  assert.strictEqual(more.textContent, 'Show all 10');
  more.click();

  //then
  assert.strictEqual(chips(r).filter((c) => !c.hidden).length, 10, 'Show all reveals the rest');
  assert.strictEqual(more.textContent, 'Show fewer');

  //when
  const input = r.querySelector('.adm-in');
  input.value = '-kubernetes.io';
  input.dispatchEvent(new w.Event('input', {bubbles: true}));

  //then
  assert.deepStrictEqual(chips(r).filter((c) => c.classList.contains('removed')).map((c) => c.textContent), ['kubernetes.io'], 'the marks follow what is typed');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the Web sites setting lists the built-in sites, your website first, and marks the ones left out with -host');
}

const HOST_ITEMS = {kind: 'web-host', separator: ',', pattern: '(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]{2,63}', flags: 'i', strip: '', why: 'not a host name such as support.google.com; a host covers its subdomains', minus: true, keywords: ['any', 'off', 'none', 'false', 'no']};

async function theWebSitesSettingUsesTheChipEditorAndKeepsTheBuiltInListAndDashRemoval() {
  //given
  const {w, doc, errors} = await boot([entry('investigate.web-domains', 'Web sites', {value: '-blog.google, example.org', builtIn: BUILT, ownDomains: ['example.com'], items: HOST_ITEMS})]);
  const r = row(doc, 'investigate.web-domains');
  const typed = () => [...r.querySelectorAll('.adm-chips li.adm-chip')];
  const built = () => [...r.querySelectorAll('.adm-built .adm-chip')];
  const text = r.querySelector('.adm-in');
  const entryBox = r.querySelector('.adm-chip-in');

  //then
  assert.ok(r.querySelector('.adm-chips') && entryBox && text.hidden, 'the chip editor replaces the text field');
  assert.deepStrictEqual(typed().map((c) => c.querySelector('.adm-chip-t').textContent), ['-blog.google', 'example.org']);
  assert.ok(typed().every((c) => !c.classList.contains('bad')), 'a dash host is a valid chip');
  assert.deepStrictEqual(built().filter((c) => c.classList.contains('removed')).map((c) => c.textContent), ['blog.google'], 'the built-in list still marks what is left out');

  //when
  entryBox.value = '-kubernetes.io';
  entryBox.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
  entryBox.value = '-bad_host';
  entryBox.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
  entryBox.value = 'localhost';
  entryBox.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));

  //then
  assert.deepStrictEqual(built().filter((c) => c.classList.contains('removed')).map((c) => c.textContent), ['blog.google', 'kubernetes.io'], 'the marks follow the chips');
  assert.strictEqual(typed().filter((c) => c.classList.contains('bad')).length, 2, 'a dash before a bad host and a one-word host stay invalid');
  assert.strictEqual(text.value, '-blog.google, example.org, -kubernetes.io, -bad_host, localhost', 'the saved text keeps the comma-separated form');

  //when
  const word = (name) => [...r.querySelectorAll('.adm-chip-word')].find((b) => b.dataset.word === name);
  word('off').click();

  //then
  assert.deepStrictEqual(typed().map((c) => c.querySelector('.adm-chip-t').textContent), ['off'], 'a keyword replaces the list');
  assert.strictEqual(text.value, 'off');
  assert.deepStrictEqual(built().filter((c) => c.classList.contains('removed')), [], 'and no built-in site is marked left out');

  //when
  entryBox.value = 'example.net';
  entryBox.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
  entryBox.value = 'ANY';
  entryBox.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
  entryBox.value = 'example.net';
  entryBox.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));

  //then
  assert.strictEqual(text.value, 'example.net', 'adding a host removes a keyword, even typed in capitals');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the Web sites setting uses the chip editor, keeps the built-in list and accepts -host chips');
}

const OWN_ITEMS = {kind: 'own-site', separator: ',', pattern: '(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]{2,63}', flags: 'i', strip: '', why: 'not a host name such as example.com; leave out https://, any path and any port', loose: true};
const ownRow = (from, hosts, value) => entry('core.own-sites', 'Your websites', {kind: 'list', value, items: OWN_ITEMS, ownFrom: from, ownHosts: hosts});
const fromNote = (doc) => doc.querySelector('#ai-rows .adm-row[data-key="core.own-sites"] .adm-own-from');

async function yourWebsitesShowsWhichSourceIsInUseAndTakesHostsWithCommasOrSpaces() {
  //given
  const set = await boot([ownRow('setting', ['example.com', 'shop.example.org'], 'example.com, shop.example.org')]);
  const site = await boot([ownRow('website', ['shop.example.com', 'shop.example.net'], '')]);
  const none = await boot([ownRow('none', [], '')]);

  //then
  assert.strictEqual(fromNote(set.doc).textContent, 'In use: the sites set here');
  assert.strictEqual(fromNote(site.doc).textContent, 'In use: from the Website integration (shop.example.com, shop.example.net)');
  assert.strictEqual(fromNote(site.doc).dataset.from, 'website');
  assert.strictEqual(fromNote(none.doc).textContent, 'In use: none yet. Set Your websites or add a Website integration');
  const r = row(set.doc, 'core.own-sites');
  assert.deepStrictEqual([...r.querySelectorAll('.adm-chips li.adm-chip .adm-chip-t')].map((c) => c.textContent), ['example.com', 'shop.example.org'], 'the chip editor shows each host');

  //when
  const text = r.querySelector('.adm-in'), entryBox = r.querySelector('.adm-chip-in');
  for (const typed of ['https://example.net/shop', 'example.net']) {
    entryBox.value = typed;
    entryBox.dispatchEvent(new set.w.KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
  }

  //then
  const chipsNow = [...r.querySelectorAll('.adm-chips li.adm-chip')];
  assert.deepStrictEqual(chipsNow.filter((c) => c.classList.contains('bad')).map((c) => c.querySelector('.adm-chip-t').textContent), ['https://example.net/shop'], 'a pasted address stays invalid');
  assert.ok(!r.querySelector('.adm-chip-word'), 'no any or off keyword is offered');
  assert.strictEqual(text.value, 'example.com, shop.example.org, https://example.net/shop, example.net');
  [set, site, none].forEach((x) => { assert.deepStrictEqual(x.errors, []); x.w.close(); });
  console.log('PASS Your websites shows which source is in use and its chip editor refuses a pasted address');
}

async function yourWebsitesTranslatesTheSourceNoteWithAPack() {
  //given
  const de = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'skopax', 'locale', 'de', 'page.json'), 'utf8'));
  const pack = {de: Object.assign({}, de.page, {'@rules': de.rules})};
  const site = await boot([ownRow('website', ['shop.example.com'], ''), entry('investigate.web-domains', 'Web sites', {value: '', builtIn: BUILT, ownDomains: ['example.com']})], pack);
  const set = await boot([ownRow('setting', ['example.com'], 'example.com')], pack);
  const none = await boot([ownRow('none', [], '')], pack);

  //then
  assert.strictEqual(fromNote(site.doc).textContent, 'Verwendet: aus der Website-Integration (shop.example.com)');
  assert.strictEqual(fromNote(set.doc).textContent, 'Verwendet: die hier eingestellten Websites');
  assert.strictEqual(fromNote(none.doc).textContent, 'Verwendet: noch keine. Legen Sie Ihre Websites fest oder f\u00fcgen Sie eine Website-Integration hinzu');
  const own = chips(row(site.doc, 'investigate.web-domains')).find((c) => c.classList.contains('own'));
  assert.strictEqual(own.textContent, 'example.com \u00b7 Ihre Website');
  assert.strictEqual(own.title, 'Eine Ihrer Websites');
  [site, set, none].forEach((x) => { assert.deepStrictEqual(x.errors, []); x.w.close(); });
  console.log('PASS Your websites translates the source note and the own-site chip with a pack');
}

async function aboutYourCompanyIsAnEightRowBoxThatResizesBothWays() {
  //given
  const {w, doc, errors} = await boot([entry('core.org-about', 'About your company', {limit: 2000, lines: 8})]);
  const box = row(doc, 'core.org-about').querySelector('textarea.adm-long');

  //then
  assert.strictEqual(box.getAttribute('rows'), '8');
  assert.strictEqual(box.getAttribute('maxlength'), '2000');
  const css = [...doc.querySelectorAll('style')].map((s) => s.textContent).join('\n');
  assert.match(css, /\.adm-c \.adm-long \{[^}]*resize: both;/, 'it can be dragged wider and taller');
  assert.match(css, /\.adm-c \.adm-long \{[^}]*max-width: 100%;/, 'and never wider than its column');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS About your company is an eight-row box with resize both and a column-wide maximum');
}

(async () => {
  await theWebSitesSettingShowsTheBuiltInListWithYourDomainFirstAndMarksTheOnesYouLeftOut();
  await theWebSitesSettingUsesTheChipEditorAndKeepsTheBuiltInListAndDashRemoval();
  await yourWebsitesShowsWhichSourceIsInUseAndTakesHostsWithCommasOrSpaces();
  await yourWebsitesTranslatesTheSourceNoteWithAPack();
  await aboutYourCompanyIsAnEightRowBoxThatResizesBothWays();
  process.exit(0);
})().catch((e) => { console.error('FAIL ' + (e && e.stack || e)); process.exit(1); });
