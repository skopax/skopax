const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const MARKUP = '<b>&"x';
const HOSTILE = {name: MARKUP, slug: 'x', version: '0.1.0', attribution: {text: MARKUP + ' \u00b7 powered by Skopax 0.1.0', href: 'https://skopax.io'}};
const PLAIN = {name: 'Skopax', slug: 'skopax', version: '0.1.0', attribution: {text: 'Skopax 0.1.0 \u00b7 skopax.io', href: 'https://skopax.io'}};

function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)}, anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

const SVG_WORDMARK = '<span class="wordmark" role="img" aria-label="Skopax"><span class="wm-dark"><svg aria-hidden="true" viewBox="0 0 10 10"><path d="M0 0h10v10z"/></svg></span></span>';
const TEXT_WORDMARK = '<span class="wordmark" role="img" aria-label="Skopax">Skopax</span>';
const HIDDEN = {name: 'Acme Ops', slug: 'acme-ops', version: '0.1.0', attribution: null};
const BLANK = {name: 'Skopax', slug: 'skopax', version: '0.1.0', attribution: {text: '', href: 'https://skopax.io'}};
const BRAND_BLOB = /(<script type="application\/json" id="brand-data">)[\s\S]*?(<\/script>)/;

function withBlob(html, blob) {
  if (blob === 'missing') return html.replace(BRAND_BLOB, '');
  if (typeof blob === 'string') return html.replace(BRAND_BLOB, (m, a, b) => a + blob + b);
  return blob ? html.replace(BRAND_BLOB, (m, a, b) => a + JSON.stringify(blob) + b) : html;
}

function shownMarkup(doc) {
  const copy = doc.documentElement.cloneNode(true);
  copy.querySelectorAll('script, style').forEach((s) => s.remove());
  return copy.outerHTML;
}

async function boot(brand, blob, wordmark) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  let html = pageHtml(TEMPLATE, {brand}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state()));
  if (wordmark) html = html.replace(TEXT_WORDMARK, wordmark);
  html = withBlob(html, blob);
  const dom = new JSDOM(html, {
    url: 'https://example.com/', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.fetch = async (p) => {
        const ok = (body) => ({ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body)});
        if (String(p).startsWith('/api/me')) return ok({loginEnabled: true, user: 'nina.white@example.com', operator: true, teams: [], tags: []});
        if (String(p).startsWith('/api/actions') || String(p).startsWith('/api/deploy-reports')) return ok([]);
        if (String(p).startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(300);
  return {w: dom.window, doc: dom.window.document, errors};
}

async function theHeaderTitleAndAttributionShowTheDisplayNameAsText() {
  //given
  const {w, doc, errors} = await boot(HOSTILE);

  //when
  const home = doc.querySelector('h1 a.home'), link = doc.getElementById('attribution');

  //then
  assert.deepStrictEqual(errors, []);
  assert.strictEqual(home.textContent, MARKUP);
  assert.strictEqual(doc.querySelector('h1 b'), null);
  assert.strictEqual(doc.title, 'Board \u00b7 ' + MARKUP);
  assert.strictEqual(link.textContent, HOSTILE.attribution.text);
  assert.strictEqual(link.getAttribute('href'), 'https://skopax.io');
  assert.strictEqual(doc.querySelector('#side-attr b, #end-attr b'), null);
  assert.ok(doc.body.textContent.includes('Questions people asked ' + MARKUP), 'the help text carries the name as text');
  assert.strictEqual(w.sourceLabel({source: 'page'}), MARKUP + ' page');
  assert.strictEqual(w.RES_VIA.system, 'via ' + MARKUP);
  const resolved = w.resolutionHtml({kind: 'handled', by: 'auto', via: 'system'});
  assert.ok(resolved.includes('via &lt;b&gt;&amp;&quot;x') && !resolved.includes('<b>'), 'the system actor shows the display name, escaped');
  console.log('PASS the header, the title and the attribution line show the display name as text and link to skopax.io');
}

async function theServerRenderedWordmarkSurvivesTheScriptAndKeepsItsAccessibleName() {
  //given
  const {doc, errors} = await boot(PLAIN, null, SVG_WORDMARK);
  const home = doc.querySelector('h1 a.home');

  //then
  assert.deepStrictEqual(errors, []);
  assert.ok(home.querySelector('svg'), 'the wordmark svg is still there after the script ran');
  assert.strictEqual(home.querySelector('.wordmark').getAttribute('role'), 'img');
  assert.strictEqual(home.querySelector('.wordmark').getAttribute('aria-label'), 'Skopax');
  assert.strictEqual(doc.title, 'Board \u00b7 Skopax');
  console.log('PASS the server-rendered wordmark is kept and keeps role img and its label');
}

async function theDefaultNameGivesTheShortAttributionLine() {
  //given
  const {doc, errors} = await boot(PLAIN);

  //then
  assert.deepStrictEqual(errors, []);
  assert.strictEqual(doc.querySelector('h1 a.home').textContent, 'Skopax');
  assert.strictEqual(doc.getElementById('attribution').textContent, 'Skopax 0.1.0 \u00b7 skopax.io');
  assert.strictEqual(doc.getElementById('attribution').getAttribute('href'), 'https://skopax.io');
  assert.strictEqual(doc.getElementById('attribution').getAttribute('target'), '_blank', 'the Skopax link opens in a new tab');
  assert.ok([...doc.querySelectorAll('.side-attr a, .end-attr a')].every((x) => x.getAttribute('target') === '_blank'), 'every Skopax link in the sidebar opens in a new tab');
  console.log('PASS the default name shows the short attribution line');
}

async function theScriptFallbackBuildsTheFullAttributionAndNeverShortensAServerLine() {
  //given
  const blank = (name) => ({name, slug: 'x', version: '0.1.0', attribution: {text: '', href: 'https://skopax.io'}});
  const custom = await boot(blank('Acme Ops'), {name: 'Acme Ops', slug: 'acme-ops', version: '0.6.0'});
  const plain = await boot(blank('Skopax'), {name: 'Skopax', slug: 'skopax', version: '0.6.0'});
  const kept = await boot(PLAIN, {name: 'Skopax', slug: 'skopax', version: '0.1.0'});

  //then
  assert.strictEqual(custom.doc.getElementById('attribution').textContent, 'Acme Ops \u00b7 powered by Skopax 0.6.0');
  assert.strictEqual(plain.doc.getElementById('attribution').textContent, 'Skopax 0.6.0 \u00b7 skopax.io');
  assert.strictEqual(kept.doc.getElementById('attribution').textContent, PLAIN.attribution.text);
  for (const page of [custom, plain, kept]) { assert.deepStrictEqual(page.errors, []); page.w.close(); }
  console.log('PASS the script fallback builds the full attribution and keeps a correct server line');
}

async function anExplicitNullAttributionDrawsNoLineOnThePage() {
  //given
  const {w, doc, errors} = await boot(HIDDEN);

  //then
  assert.deepStrictEqual(errors, []);
  assert.strictEqual(doc.getElementById('attribution'), null);
  assert.strictEqual(doc.querySelector('footer.attribution'), null);
  assert.ok(!/skopax\.io|powered by/i.test(shownMarkup(doc)), 'no element on the page carries the line');
  assert.strictEqual(w.BRAND.attribution, null);
  assert.strictEqual(doc.querySelector('h1 a.home').textContent, 'Acme Ops');
  assert.deepStrictEqual(['side-attr', 'end-attr'].map((id) => [doc.getElementById(id).childNodes.length, doc.getElementById(id).hidden]), [[0, true], [0, true]], 'the sidebar foot and the phone copy stay empty');
  assert.strictEqual(doc.querySelector('.attr-mark'), null, 'the rail mark is not drawn');
  w.close();
  console.log('PASS an explicit null attribution draws no line anywhere on the page: not the foot, the phone copy or the rail mark');
}

function theServedPageCarriesOneAttributionIdAndThePhoneCopyNone() {
  //given
  const shown = pageHtml(TEMPLATE, {brand: HOSTILE}), hidden = pageHtml(TEMPLATE, {brand: HIDDEN});
  const doc = new JSDOM(shown).window.document, gone = new JSDOM(hidden).window.document;
  const foot = doc.querySelector('#side-attr #attribution'), copy = doc.querySelector('#end-attr a');

  //then
  assert.strictEqual((shown.match(/id="attribution"/g) || []).length, 1, 'one id on the page before any script runs');
  assert.deepStrictEqual([foot.textContent, foot.getAttribute('href')], [HOSTILE.attribution.text, 'https://skopax.io']);
  assert.deepStrictEqual([copy.id, copy.textContent, copy.getAttribute('href'), copy.getAttribute('rel')], ['', HOSTILE.attribution.text, 'https://skopax.io', 'noopener noreferrer'], 'the phone copy is the same link without the id');
  assert.deepStrictEqual(['side-attr', 'end-attr'].map((id) => gone.getElementById(id).innerHTML), ['', ''], 'a hidden line fills neither slot');
  assert.ok(!/id="attribution"|\[\[attribution/.test(hidden) && !/\[\[attribution/.test(shown), 'no id and no slot text is left');
  console.log('PASS the served page carries one attribution id, at the sidebar foot, and its phone copy carries none');
}

async function aMissingOrUnreadableBrandBlobStillGetsTheFallbackLine() {
  //given
  const missing = await boot(BLANK, 'missing');
  const unreadable = await boot(BLANK, '{not json');

  //then
  for (const page of [missing, unreadable]) {
    assert.deepStrictEqual(page.errors, []);
    assert.strictEqual(page.doc.getElementById('attribution').textContent, 'Skopax 0.6.3 \u00b7 skopax.io');
    assert.strictEqual(page.doc.getElementById('attribution').getAttribute('href'), 'https://skopax.io');
    page.w.close();
  }
  console.log('PASS a missing or unreadable brand blob still gets the fallback line from the script');
}

async function theAttributionSitsAtTheBottomOfThePageLayout() {
  //given
  const {doc} = await boot(HOSTILE);
  const main = doc.querySelector('main.main');

  //then
  assert.strictEqual(main.lastElementChild.id, 'end-attr');
  assert.strictEqual(main.lastElementChild.textContent, HOSTILE.attribution.text, 'the phone copy ends the main column');
  assert.ok(doc.getElementById('side-attr').contains(doc.getElementById('attribution')), 'and the line itself sits at the foot of the sidebar');
  console.log('PASS the attribution sits at the foot of the sidebar, and its phone copy is the last element of the main column');
}

(async () => {
  try {
    await theHeaderTitleAndAttributionShowTheDisplayNameAsText();
    await theServerRenderedWordmarkSurvivesTheScriptAndKeepsItsAccessibleName();
    await theDefaultNameGivesTheShortAttributionLine();
    await theScriptFallbackBuildsTheFullAttributionAndNeverShortensAServerLine();
    await anExplicitNullAttributionDrawsNoLineOnThePage();
    theServedPageCarriesOneAttributionIdAndThePhoneCopyNone();
    await aMissingOrUnreadableBrandBlobStillGetsTheFallbackLine();
    await theAttributionSitsAtTheBottomOfThePageLayout();
    process.exit(0);
  } catch (e) {
    console.log('FAIL ' + (e && e.message));
    console.log(e && e.stack);
    process.exit(1);
  }
})();
