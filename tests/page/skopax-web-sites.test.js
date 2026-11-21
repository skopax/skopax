const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const BUILT = ['example.com', 'support.google.com', 'developers.google.com', 'firebase.google.com', 'cloud.google.com', 'status.cloud.google.com', 'blog.google', 'developer.apple.com', 'developer.android.com', 'kubernetes.io'];

function entry(key, label, extra) {
  return Object.assign({key, group: 'Investigator', label, help: label, kind: 'text', live: true, value: '', base: '', default: '', source: 'default', by: null, at: null, limit: 1000}, extra);
}

async function boot(items) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/#t=ai', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
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
  assert.strictEqual(shown[0], 'example.com \u00b7 your domain');
  assert.strictEqual(chips(r)[0].title, 'Your domain (from sign-in e-mail domains)');
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
  console.log('PASS the Web sites setting lists the built-in sites, your domain first, and marks the ones left out with -host');
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
  await aboutYourCompanyIsAnEightRowBoxThatResizesBothWays();
  process.exit(0);
})().catch((e) => { console.error('FAIL ' + (e && e.stack || e)); process.exit(1); });
