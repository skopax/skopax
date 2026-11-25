const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const EVIL = '<img src=x onerror=alert(1)>';
const SAVED = 'Shop.example.com,  legacy.example.org';
const ZONES = ['shop.example.com', 'blog.example.com', 'docs.example.org', EVIL];
const COMPONENTS = ['CDN/Cache', 'WAF', 'Tokyo, Japan - (NRT)'];

const FIELDS = [
  {key: 'token', label: 'API token', kind: 'secret', required: true, default: '', help: '', choices: [], low: null, high: null, showIf: null, ui: false},
  {key: 'zones', label: 'Zones', kind: 'text', required: true, default: '', help: 'Pick zones', choices: [], low: null, high: null, showIf: null, ui: false, suggest: true, separator: ','},
  {key: 'status-components', label: 'Status components', kind: 'text', required: false, default: '', help: '', choices: [], low: null, high: null, showIf: null, ui: false, suggest: true, separator: ';'},
  {key: 'region', label: 'Region', kind: 'text', required: false, default: '', help: '', choices: [], low: null, high: null, showIf: null, ui: false, suggest: true, separator: ''},
  {key: 'owned', label: 'Owned', kind: 'text', required: false, default: '', help: '', choices: [], low: null, high: null, showIf: null, ui: false, suggest: true, separator: ','},
];

function catalog(over) {
  return {edition: 'community', secretsKey: 'generated', plugins: [
    {id: 'edge', name: 'Edge', version: '1.0.0', category: 'web', description: 'Edge zones.', edition: 'community', icon: false, state: 'ok', reason: null, enabled: true, fields: FIELDS, checks: [],
      instances: [{name: 'main', primary: true, enabled: true, source: 'admin', problem: null, values: Object.assign({zones: SAVED, 'status-components': 'CDN/Cache', region: 'eu', owned: 'a.example.com'}, over || {}),
        secrets: {token: {set: true, from: null}}, owned: ['owned'], offChecks: []}],
      health: {state: 'ok', text: 'Healthy', at: iso(2), instances: [{name: 'main', state: 'ok', text: 'Healthy', at: iso(2)}]}},
  ]};
}

async function boot(over) {
  const errors = [], calls = [], gates = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const flags = {held: false};
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/#t=anoms', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, o) => {
        const p = String(path), method = (o && o.method) || 'GET', body = o && o.body ? JSON.parse(o.body) : null;
        const ok = (b, status) => ({ok: true, status: status || 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        const bad = (status, detail) => ({ok: false, status, json: async () => ({detail})});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', userRole: 'admin', operator: true, admin: true, teams: [], tags: []});
        if (p.startsWith('/api/admin')) {
          calls.push([method, p, body]);
          if (p === '/api/admin/plugins' && method === 'GET') return ok(catalog(over));
          if (p === '/api/admin/notices') return ok({items: []});
          const m = /^\/api\/admin\/plugins\/edge\/fields\/([^/]+)\/suggestions$/.exec(p);
          if (m) {
            if (flags.held) await new Promise((r) => gates.push(r));
            const key = decodeURIComponent(m[1]);
            if (key === 'zones') return !body.name && !body.values.token ? bad(400, 'Enter the API token first') : ok({values: ZONES});
            if (key === 'status-components') return ok({values: COMPONENTS});
            return ok({values: ['eu', 'us', 'ap']});
          }
          if (p.endsWith('/test')) return ok({ok: true, summary: 'answered', ms: 12});
          if (p.endsWith('/instances') && method === 'POST') return ok({name: body.name}, 201);
          if (p.includes('/instances/') && method === 'PATCH') return ok({instance: catalog().plugins[0].instances[0]});
          if (p.startsWith('/api/admin/history')) return ok([]);
          if (p.startsWith('/api/admin/settings')) return ok({items: [], rev: 1, refreshSeconds: 15});
          if (p.startsWith('/api/admin/users') || p.startsWith('/api/admin/tokens')) return ok([]);
          return bad(404, 'none');
        }
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(150);
  const t = {w: dom.window, doc: dom.window.document, errors, calls, flags, gates};
  t.doc.getElementById('tab-integrations').click();
  await wait(120);
  return t;
}

const panel = (doc) => doc.getElementById('int-panel');
const field = (doc, key) => [...panel(doc).querySelectorAll('.int-form .int-field')].filter((f) => f.dataset.key === key)[0];
const search = (doc, key) => field(doc, key).querySelector('[role="combobox"]');
const value = (doc, key) => field(doc, key).querySelector('.int-in').value;
const chips = (doc, key) => [...field(doc, key).querySelectorAll('.int-sug-chip')].map((c) => c.querySelector('.int-sug-v').textContent);
const options = (doc, key) => [...field(doc, key).querySelectorAll('[role="option"]')].map((o) => o.textContent);
const note = (doc, key) => field(doc, key).querySelector('.int-sug-note').textContent;
const key = (w, el, k) => { const e = new w.KeyboardEvent('keydown', {key: k, bubbles: true, cancelable: true}); el.dispatchEvent(e); return e; };
const type = (w, el, v) => { el.value = v; el.dispatchEvent(new w.Event('input', {bubbles: true})); };
const suggestCalls = (t, k) => t.calls.filter((c) => c[1] === '/api/admin/plugins/edge/fields/' + k + '/suggestions');

function openMain(t) {
  [...t.doc.querySelectorAll('#integrations .int-tile')].filter((x) => x.dataset.id === 'edge')[0].querySelector('.int-open').click();
  const b = [...panel(t.doc).querySelectorAll('.int-itab')].filter((x) => x.dataset.name === 'main')[0];
  if (b) b.click();
}

async function savedValuesShowAsChipsAndAnUntouchedValueIsSentByteForByte() {
  //given
  const t = await boot();
  openMain(t);

  //when
  const shown = [chips(t.doc, 'zones'), chips(t.doc, 'status-components')];
  const removers = [...field(t.doc, 'zones').querySelectorAll('.int-sug-x')].map((b) => b.getAttribute('aria-label'));
  const box = search(t.doc, 'zones');
  const label = field(t.doc, 'zones').querySelector('label').getAttribute('for');
  panel(t.doc).querySelector('.int-test').click();
  await wait(60);

  //then
  assert.deepStrictEqual(shown, [['Shop.example.com', 'legacy.example.org'], ['CDN/Cache']], 'each saved value is a chip, whether or not the list has it');
  assert.deepStrictEqual(removers, ['Remove Shop.example.com', 'Remove legacy.example.org']);
  assert.strictEqual(label, box.id, 'the label names the search box');
  assert.deepStrictEqual([box.getAttribute('aria-expanded'), box.getAttribute('aria-autocomplete'), field(t.doc, 'zones').querySelector('[role="listbox"]').id], ['false', 'list', box.getAttribute('aria-controls')]);
  assert.strictEqual(value(t.doc, 'zones'), SAVED);
  const tested = t.calls.filter((c) => c[1].endsWith('/test'))[0][2];
  assert.strictEqual(tested.values.zones, SAVED, 'the stored string is sent exactly as it was');
  assert.strictEqual(suggestCalls(t, 'zones').length, 0, 'the list loads only when the box is used');
  assert.ok(!field(t.doc, 'owned').querySelector('[role="combobox"]') && field(t.doc, 'owned').querySelector('.int-in').disabled, 'a value owned by a source stays disabled with no picker');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Admin, Integrations: a picker field shows saved values as removable chips and sends an untouched value byte for byte');
}

async function theListLoadsOnFocusFiltersAndAddsByKeyboard() {
  //given
  const t = await boot();
  openMain(t);
  const box = search(t.doc, 'zones');
  t.flags.held = true;

  //when
  box.focus();
  await wait(20);
  const loading = [note(t.doc, 'zones'), box.getAttribute('aria-expanded')];
  t.flags.held = false;
  t.gates.splice(0).forEach((r) => r());
  await wait(40);
  const listed = options(t.doc, 'zones');
  const press = new t.w.MouseEvent('mousedown', {bubbles: true, cancelable: true});
  field(t.doc, 'zones').querySelector('.int-sug-pop').dispatchEvent(press);
  const sent = suggestCalls(t, 'zones')[0][2];
  type(t.w, box, 'BLOG');
  const filtered = options(t.doc, 'zones');
  key(t.w, box, 'ArrowDown');
  const active = box.getAttribute('aria-activedescendant');
  const activeText = t.doc.getElementById(active).textContent;
  const enter = key(t.w, box, 'Enter');
  await wait(20);
  type(t.w, box, 'BLOG.example.com');
  key(t.w, box, 'Enter');
  const marks = [...field(t.doc, 'zones').querySelectorAll('.int-sug-chip')].map((c) => c.classList.contains('free'));

  //then
  assert.deepStrictEqual(loading, ['Loading\u2026', 'true']);
  assert.deepStrictEqual(sent, {name: 'main', values: {token: null, zones: SAVED, 'status-components': 'CDN/Cache', region: 'eu'}}, 'the form values go along like a connection test, without the value a source owns');
  assert.deepStrictEqual(listed, ['blog.example.com', 'docs.example.org', EVIL], 'chosen values are hidden from the list, whatever their case');
  assert.ok(press.defaultPrevented, 'pressing inside the list keeps focus in the box');
  assert.ok(!field(t.doc, 'zones').querySelector('[role="listbox"] img'), 'text only');
  assert.deepStrictEqual(filtered, ['blog.example.com'], 'case-insensitive substring filter');
  assert.strictEqual(activeText, 'blog.example.com');
  assert.ok(enter.defaultPrevented, 'Enter never submits the form');
  assert.deepStrictEqual(chips(t.doc, 'zones'), ['Shop.example.com', 'legacy.example.org', 'blog.example.com'], 'a typed value that is already chosen in another case adds nothing');
  assert.deepStrictEqual(marks, [false, true, false], 'a saved value that the list has in another case is not marked');
  assert.strictEqual(value(t.doc, 'zones'), 'Shop.example.com, legacy.example.org, blog.example.com');
  assert.strictEqual(box.value, '');
  assert.strictEqual(t.calls.filter((c) => c[0] === 'PATCH').length, 0);
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Admin, Integrations: a picker loads its list on focus with the form values, filters as you type and adds with the keyboard');
}

async function chipsComeOffByButtonOrBackspaceAndFreeTextIsKeptButMarked() {
  //given
  const t = await boot();
  openMain(t);
  const box = search(t.doc, 'status-components');
  box.focus();
  await wait(40);

  //when
  type(t.w, box, 'Osaka, Japan - (KIX)');
  const none = note(t.doc, 'status-components');
  key(t.w, box, 'Enter');
  const free = [...field(t.doc, 'status-components').querySelectorAll('.int-sug-chip')].map((c) => c.textContent);
  type(t.w, box, 'waf');
  key(t.w, box, 'Enter');
  const added = value(t.doc, 'status-components');
  field(t.doc, 'status-components').querySelector('.int-sug-x[aria-label="Remove CDN/Cache"]').click();
  const afterClick = [chips(t.doc, 'status-components'), t.doc.activeElement === box];
  type(t.w, box, '');
  key(t.w, box, 'Backspace');
  const afterBack = chips(t.doc, 'status-components');
  const esc = key(t.w, box, 'Escape');

  //then
  assert.strictEqual(none, 'No match');
  assert.ok(free[1].includes('not in the list') && !free[0].includes('not in the list'), 'typed text is added as is and marked quietly');
  assert.strictEqual(added, 'CDN/Cache; Osaka, Japan - (KIX); WAF', 'typed waf is stored as the list spells it');
  assert.deepStrictEqual(afterClick, [['Osaka, Japan - (KIX)', 'WAF'], true]);
  assert.deepStrictEqual(afterBack, ['Osaka, Japan - (KIX)']);
  assert.strictEqual(value(t.doc, 'status-components'), 'Osaka, Japan - (KIX)');
  assert.ok(esc.defaultPrevented && box.getAttribute('aria-expanded') === 'false', 'Escape closes the list');
  assert.ok(!panel(t.doc).hidden && panel(t.doc).querySelector('.int-form'), 'and not the panel');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Admin, Integrations: chips come off by their button or Backspace, free text is kept but marked, Escape closes only the list');
}

async function aRefusalShowsAndTheNextOpenAsksAgain() {
  //given
  const t = await boot();
  [...t.doc.querySelectorAll('#integrations .int-tile')].filter((x) => x.dataset.id === 'edge')[0].querySelector('.int-open').click();
  panel(t.doc).querySelector('.int-add').click();
  const box = search(t.doc, 'zones');

  //when
  box.focus();
  await wait(40);
  const refused = [note(t.doc, 'zones'), options(t.doc, 'zones')];
  box.blur();
  field(t.doc, 'token').querySelector('.int-in').value = 'typed-token';
  box.focus();
  await wait(40);

  //then
  assert.deepStrictEqual(refused, ['Enter the API token first', []]);
  assert.strictEqual(suggestCalls(t, 'zones').length, 2, 'a refusal is not kept');
  assert.deepStrictEqual(options(t.doc, 'zones'), ZONES);
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Admin, Integrations: a picker shows a refusal and asks again on the next open');
}

async function aSingleValuePickerHasNoChipsAndTheTypedTextIsTheValue() {
  //given
  const t = await boot();
  openMain(t);
  const box = search(t.doc, 'region');

  //when
  const start = [box.value, value(t.doc, 'region'), field(t.doc, 'region').querySelectorAll('.int-sug-chip').length];
  box.focus();
  await wait(40);
  const listed = options(t.doc, 'region');
  type(t.w, box, 'a');
  key(t.w, box, 'ArrowDown');
  key(t.w, box, 'Enter');
  const picked = value(t.doc, 'region');
  const pickedMark = field(t.doc, 'region').querySelector('.int-sug-one').hidden;
  type(t.w, box, 'mars');
  const typed = [value(t.doc, 'region'), field(t.doc, 'region').querySelector('.int-sug-one').hidden];
  type(t.w, box, 'US');
  const known = field(t.doc, 'region').querySelector('.int-sug-one').hidden;
  box.blur();
  const kept = box.value;
  panel(t.doc).querySelector('.int-test').click();
  await wait(40);

  //then
  assert.deepStrictEqual(start, ['eu', 'eu', 0]);
  assert.deepStrictEqual(listed, ['eu', 'us', 'ap']);
  assert.strictEqual(picked, 'ap');
  assert.strictEqual(pickedMark, true);
  assert.deepStrictEqual(typed, ['mars', false], 'free text is the value and is marked not in the list');
  assert.strictEqual(known, true);
  assert.strictEqual(kept, 'US', 'blur keeps the text');
  assert.strictEqual(t.calls.filter((c) => c[1].endsWith('/test'))[0][2].values.region, 'us', 'Test connection sends the list spelling');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Admin, Integrations: a single-value picker has no chips and keeps what is typed');
}

async function blurKeepsTheTextAndSaveOrTestSendsItSplitByTheSeparator() {
  //given
  const t = await boot();
  openMain(t);
  const box = search(t.doc, 'zones');
  box.focus();
  await wait(40);

  //when
  type(t.w, box, 'x.example.net,  Y.example.net, shop.example.com, docs.EXAMPLE.org');
  box.blur();
  const blurred = [chips(t.doc, 'zones'), value(t.doc, 'zones'), box.value, box.getAttribute('aria-expanded')];
  panel(t.doc).querySelector('.int-test').click();
  await wait(40);
  const tested = t.calls.filter((c) => c[1].endsWith('/test'))[0][2].values.zones;
  type(t.w, box, 'z.example.net');
  panel(t.doc).querySelector('.int-save').click();
  await wait(60);
  const patched = t.calls.filter((c) => c[0] === 'PATCH').map((c) => c[2].values.zones);

  //then
  assert.deepStrictEqual(blurred, [['Shop.example.com', 'legacy.example.org'], SAVED, 'x.example.net,  Y.example.net, shop.example.com, docs.EXAMPLE.org', 'false'], 'blur closes the list and adds nothing');
  assert.strictEqual(tested, 'Shop.example.com, legacy.example.org, x.example.net, Y.example.net, docs.example.org', 'Test connection sends the pending text split, without the duplicate, in the list spelling');
  assert.deepStrictEqual(patched, ['Shop.example.com, legacy.example.org, x.example.net, Y.example.net, docs.example.org, z.example.net'], 'Save sends pending text too');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Admin, Integrations: blur keeps the typed text, and Save or Test connection sends it split by the separator');
}

async function changingAnotherFieldReloadsTheListOnTheNextOpen() {
  //given
  const t = await boot();
  openMain(t);
  const box = search(t.doc, 'zones');
  box.focus();
  await wait(40);
  box.blur();

  //when
  box.focus();
  await wait(40);
  const same = suggestCalls(t, 'zones').length;
  box.blur();
  const token = field(t.doc, 'token').querySelector('.int-in');
  token.value = 'new-token';
  token.dispatchEvent(new t.w.Event('input', {bubbles: true}));
  box.focus();
  await wait(40);

  //then
  assert.strictEqual(same, 1, 'a loaded list is kept while nothing else changes');
  assert.strictEqual(suggestCalls(t, 'zones').length, 2);
  assert.strictEqual(suggestCalls(t, 'zones')[1][2].values.token, 'new-token');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Admin, Integrations: changing another field reloads the list on the next open');
}

async function anUntouchedSingleValueIsSentUnchangedWhateverItsCaseOrSpaces() {
  for (const saved of ['EU', ' eu']) {
    //given
    const t = await boot({region: saved});
    openMain(t);
    const box = search(t.doc, 'region');

    //when
    box.focus();
    await wait(40);
    box.blur();
    panel(t.doc).querySelector('.int-test').click();
    await wait(40);

    //then
    assert.strictEqual(options(t.doc, 'region').length, 3, 'the list loaded');
    assert.strictEqual(t.calls.filter((c) => c[1].endsWith('/test'))[0][2].values.region, saved, 'an untouched value is not rewritten');
    assert.deepStrictEqual(t.errors, []);
    t.w.close();
  }
  console.log('PASS Admin, Integrations: an untouched single value is sent unchanged, whatever its case or spaces');
}

(async () => {
  await savedValuesShowAsChipsAndAnUntouchedValueIsSentByteForByte();
  await theListLoadsOnFocusFiltersAndAddsByKeyboard();
  await chipsComeOffByButtonOrBackspaceAndFreeTextIsKeptButMarked();
  await aRefusalShowsAndTheNextOpenAsksAgain();
  await aSingleValuePickerHasNoChipsAndTheTypedTextIsTheValue();
  await blurKeepsTheTextAndSaveOrTestSendsItSplitByTheSeparator();
  await changingAnotherFieldReloadsTheListOnTheNextOpen();
  await anUntouchedSingleValueIsSentUnchangedWhateverItsCaseOrSpaces();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
