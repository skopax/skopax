const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

const fld = (key, label, kind, extra) => Object.assign({key, label, kind, required: false, default: '', help: '', choices: [], low: null, high: null, showIf: null, ui: false}, extra || {});
const FIELDS = [
  fld('url', 'URL', 'url', {required: true, ui: true}),
  fld('auth', 'Authentication', 'choice', {choices: ['basic', 'bearer'], default: 'basic'}),
  fld('mode', 'Mode', 'choice', {choices: ['alpha', 'beta', 'gamma', 'delta'], default: 'alpha'}),
  fld('verify', 'Verify', 'bool', {default: 'true'}),
  fld('retries', 'Retries', 'int', {low: 0, high: 9}),
  fld('cert', 'Certificate', 'pem'),
  fld('token', 'Token', 'secret'),
  fld('note', 'Note > about <b>it</b>', 'text', {help: 'Use a > b and <i>x</i> here'}),
];
const OWNED = ['url', 'auth', 'mode', 'verify', 'retries', 'cert', 'token', 'note'];
const MCP_FIELDS = [fld('preset', 'Catalog entry', 'choice', {choices: ['atlassian', 'notion', 'github', 'google-drive']})];

function instance(name, owned) {
  const mine = owned === true ? OWNED : Array.isArray(owned) ? owned : [];
  return {name, primary: name === 'main', enabled: true, source: mine.length ? 'Spring Config' : 'admin', problem: null,
    values: {url: 'https://' + name + '.example.com', auth: 'bearer', mode: 'beta', verify: 'true', retries: '3', cert: 'PEM', note: 'n'}, secrets: {token: {set: true, from: mine.length ? 'Spring Config' : null}}, owned: mine, offChecks: []};
}

async function boot(plugin) {
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const catalog = {edition: 'community', secretsKey: 'generated', plugins: [
    {id: 'acme', name: 'Acme metrics', version: '1.0.0', category: 'metrics', description: 'Reads Acme.', edition: 'community', icon: true, state: 'ok', reason: null, enabled: true, fields: FIELDS, checks: [],
      instances: [instance('main', true), instance('east', false), instance('west', ['url'])], health: {state: 'ok', text: 'Healthy', at: iso(2), instances: []}},
    {id: 'mcp-tools', name: 'MCP tools', version: '1.0.0', category: 'other', description: 'Tools.', edition: 'community', icon: true, state: 'ok', reason: null, enabled: true, fields: MCP_FIELDS, checks: [],
      instances: [Object.assign(instance('pinned', ['preset']), {values: {preset: 'github'}, secrets: {}})], health: {state: 'ok', text: 'Healthy', at: iso(2), instances: []}}]};
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/#t=anoms', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, o) => {
        const p = String(path), method = (o && o.method) || 'GET', body = o && o.body ? JSON.parse(o.body) : null;
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', userRole: 'admin', operator: true, admin: true, teams: [], tags: []});
        if (p.startsWith('/api/admin')) {
          calls.push([method, p, body]);
          if (p === '/api/admin/plugins' && method === 'GET') return ok(catalog);
          if (p === '/api/admin/notices') return ok({items: []});
          if (p.endsWith('/test')) return ok({ok: true, summary: 'answered', ms: 12});
          if (p.includes('/instances/') && method === 'PATCH') return ok({instance: instance('main', true)});
          if (p.startsWith('/api/admin/history')) return ok([]);
          if (p.startsWith('/api/admin/small-talk')) return {ok: false, status: 404, json: async () => ({detail: 'none'})};
          if (p.startsWith('/api/admin/settings')) return ok({items: [], rev: 1, refreshSeconds: 15});
          if (p.startsWith('/api/admin/users') || p.startsWith('/api/admin/tokens')) return ok([]);
        }
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(150);
  const t = {w: dom.window, doc: dom.window.document, errors, calls};
  t.doc.getElementById('tab-integrations').click();
  await wait(120);
  [...t.doc.querySelectorAll('#integrations .int-tile')].filter((x) => x.dataset.id === (plugin || 'acme'))[0].querySelector('.int-open').click();
  return t;
}

const panel = (doc) => doc.getElementById('int-panel');
const field = (doc, key) => [...panel(doc).querySelectorAll('.int-form .int-field')].filter((f) => f.dataset.key === key)[0];
const tabFor = (doc, name) => [...panel(doc).querySelectorAll('.int-itab')].filter((b) => b.dataset.name === name)[0];
const SET_BY = 'Set by Spring Config; change it there';

async function anOwnedTextFieldIsDisabledWithItsSource() {
  //given
  const t = await boot();

  //when
  tabFor(t.doc, 'main').click();
  const f = field(t.doc, 'url'), input = f.querySelector('.int-in');

  //then
  assert.strictEqual(input.disabled, true);
  assert.strictEqual(input.readOnly, false);
  assert.strictEqual(input.title, SET_BY);
  assert.strictEqual(input.value, 'https://main.example.com');
  assert.ok(f.textContent.includes('from Spring Config'));
  assert.ok(field(t.doc, 'retries').querySelector('.int-in').disabled, 'an owned int is disabled');
  assert.ok(field(t.doc, 'cert').querySelector('.int-in').disabled, 'an owned pem is disabled');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS an owned text field is disabled with its source');
}

async function anOwnedSecretIsDisabledWithNoShowOrClear() {
  //given
  const t = await boot();

  //when
  tabFor(t.doc, 'main').click();
  const f = field(t.doc, 'token'), input = f.querySelector('.int-in');

  //then
  assert.strictEqual(input.disabled, true);
  assert.strictEqual(input.title, SET_BY);
  assert.ok(!f.querySelector('.int-eye') && !f.querySelector('.int-clear'));
  assert.strictEqual(f.querySelector('.int-set').textContent, 'From Spring Config');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS an owned secret is disabled with no Show or Clear');
}

async function anOwnedChoiceSelectAndSegmentAreDisabled() {
  //given
  const t = await boot();

  //when
  tabFor(t.doc, 'main').click();
  const select = field(t.doc, 'mode').querySelector('select'), buttons = [...field(t.doc, 'auth').querySelectorAll('.int-seg-b')];
  buttons[0].click();

  //then
  assert.strictEqual(select.disabled, true);
  assert.strictEqual(select.title, SET_BY);
  assert.deepStrictEqual(buttons.map((b) => [b.disabled, b.getAttribute('aria-disabled')]), [[true, 'true'], [true, 'true']]);
  assert.strictEqual(field(t.doc, 'auth').querySelector('.int-in').value, 'bearer', 'a click does not change the value');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS an owned choice select and segment are disabled');
}

async function anOwnedSwitchDoesNotToggle() {
  //given
  const t = await boot();
  tabFor(t.doc, 'main').click();
  const sw = field(t.doc, 'verify').querySelector('.int-switch');

  //when
  sw.click();
  sw.dispatchEvent(new t.w.KeyboardEvent('keydown', {key: ' ', bubbles: true, cancelable: true}));

  //then
  assert.strictEqual(sw.disabled, true);
  assert.strictEqual(sw.getAttribute('aria-disabled'), 'true');
  assert.strictEqual(sw.getAttribute('aria-checked'), 'true');
  assert.strictEqual(field(t.doc, 'verify').querySelector('.int-in').value, 'true');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS an owned switch does not toggle');
}

async function savingAndTestingNeverSendOwnedKeys() {
  //given
  const t = await boot();
  tabFor(t.doc, 'main').click();
  const form = panel(t.doc).querySelector('.int-form');

  //when
  panel(t.doc).querySelector('.int-test').click();
  await wait(100);
  form.querySelector('.int-save').click();
  await wait(100);
  const sent = t.calls.filter((c) => c[0] !== 'GET' && c[2]).map((c) => c[2].values);
  const stillLocked = [...form.querySelectorAll('.int-seg-b, .int-switch')].every((b) => b.disabled);

  //then
  assert.ok(sent.length >= 1, 'test and save went out');
  sent.forEach((values) => assert.deepStrictEqual(values || {}, {}, 'no owned key is sent'));
  assert.ok(stillLocked, 'the buttons stay disabled after a busy cycle');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS saving and testing never send owned keys');
}

async function everyControlOfAnOwnedFieldIsDisabledEvenWithAngleBracketsInItsText() {
  //given
  const t = await boot();

  //when
  tabFor(t.doc, 'main').click();
  const note = field(t.doc, 'note');
  const controls = [...panel(t.doc).querySelectorAll('.int-form .int-field[data-owned="1"]')].reduce((all, f) => all.concat([...f.querySelectorAll('input, select, textarea, button')]), []);

  //then
  assert.ok(note.querySelector('.adm-help').textContent.includes('a > b'));
  assert.ok(controls.length >= 11);
  assert.deepStrictEqual(controls.filter((c) => !c.disabled).map((c) => c.outerHTML.slice(0, 60)), []);
  assert.ok(controls.every((c) => c.getAttribute('title') === SET_BY));
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS every control of an owned field is disabled even with angle brackets in its text');
}

async function aPartlyOwnedInstanceSendsOnlyTheEditedFieldThatIsNotOwned() {
  //given
  const t = await boot();
  tabFor(t.doc, 'west').click();
  const retries = field(t.doc, 'retries').querySelector('.int-in');
  const locks = [field(t.doc, 'url').querySelector('.int-in').disabled, retries.disabled];

  //when
  retries.value = '7';
  retries.dispatchEvent(new t.w.Event('input', {bubbles: true}));
  panel(t.doc).querySelector('.int-save').click();
  await wait(100);
  const patch = t.calls.filter((c) => c[0] === 'PATCH').map((c) => c[2]);

  //then
  assert.deepStrictEqual(locks, [true, false]);
  assert.strictEqual(patch.length, 1);
  assert.deepStrictEqual(patch[0].values, {retries: '7', token: null});
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS a partly owned instance sends only the edited field that is not owned');
}

async function arrowKeysAndEnterOnAnOwnedSegmentChangeNothing() {
  //given
  const t = await boot();
  tabFor(t.doc, 'main').click();
  const seg = field(t.doc, 'auth').querySelector('.int-seg'), input = field(t.doc, 'auth').querySelector('.int-in');
  const buttons = [...seg.querySelectorAll('.int-seg-b')];

  //when
  ['ArrowLeft', 'ArrowRight', 'Home', 'End', ' ', 'Enter'].forEach((k) => buttons.forEach((b) => b.dispatchEvent(new t.w.KeyboardEvent('keydown', {key: k, bubbles: true, cancelable: true}))));
  const sw = field(t.doc, 'verify').querySelector('.int-switch');
  ['Enter', ' '].forEach((k) => sw.dispatchEvent(new t.w.KeyboardEvent('keydown', {key: k, bubbles: true, cancelable: true})));

  //then
  assert.strictEqual(input.value, 'bearer');
  assert.deepStrictEqual(buttons.map((b) => b.getAttribute('aria-checked')), ['false', 'true']);
  assert.strictEqual(field(t.doc, 'verify').querySelector('.int-in').value, 'true');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS arrow keys and Enter on an owned segment change nothing');
}

async function theEntryChangeAndClearButtonsOfAnOwnedFieldDoNothing() {
  //given
  const t = await boot('mcp-tools');
  tabFor(t.doc, 'pinned').click();
  const f = field(t.doc, 'preset'), change = f.querySelector('.int-entry-change'), clear = f.querySelector('.int-entry-clear'), select = f.querySelector('select');

  //when
  [change, clear].forEach((b) => { b.click(); ['Enter', ' '].forEach((k) => b.dispatchEvent(new t.w.KeyboardEvent('keydown', {key: k, bubbles: true, cancelable: true}))); });

  //then
  assert.ok(change.disabled && clear.disabled && select.disabled);
  assert.strictEqual(select.value, 'github');
  assert.strictEqual(f.dataset.pick, undefined);
  assert.strictEqual(select.hidden, true);
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS the entry Change and Clear buttons of an owned field do nothing');
}

async function aDisabledCheckedSwitchAndSegmentStayDistinctFromUnchecked() {
  //given
  const t = await boot();

  //when
  tabFor(t.doc, 'main').click();
  const sw = field(t.doc, 'verify').querySelector('.int-switch'), seg = [...field(t.doc, 'auth').querySelectorAll('.int-seg-b')];
  const css = require('fs').readFileSync(TEMPLATE, 'utf8');

  //then
  assert.strictEqual(sw.getAttribute('aria-checked'), 'true');
  assert.deepStrictEqual(seg.map((b) => b.getAttribute('aria-checked')), ['false', 'true']);
  assert.ok(css.includes('.int-switch[aria-checked="true"] { background: var(--good); }'));
  assert.ok(css.includes('.int-seg-b[aria-checked="true"] { color: var(--fg);'));
  assert.ok(css.includes('.int-field[data-owned="1"] .int-switch[aria-disabled="true"] { opacity: 1; }'));
  assert.ok(!/\.int-seg-b:disabled[^}]*opacity/.test(css));
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS a disabled checked switch and segment stay distinct from unchecked');
}

async function aFieldNotOwnedStaysEditable() {
  //given
  const t = await boot();

  //when
  tabFor(t.doc, 'east').click();
  const all = [...panel(t.doc).querySelectorAll('.int-form .int-field .int-in, .int-form .int-seg-b, .int-form .int-switch, .int-form select')];
  const url = field(t.doc, 'url').querySelector('.int-in');
  url.value = 'https://other.example.com';
  url.dispatchEvent(new t.w.Event('input', {bubbles: true}));
  panel(t.doc).querySelector('.int-test').click();
  await wait(100);
  const sent = t.calls.filter((c) => c[0] === 'POST' && c[1].endsWith('/test')).map((c) => c[2].values);

  //then
  assert.ok(all.length >= 6 && all.every((x) => !x.disabled && x.getAttribute('aria-disabled') !== 'true' && !x.title.includes('Set by')));
  assert.strictEqual(sent[0].url, 'https://other.example.com');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS a field not owned stays editable');
}

(async () => {
  await anOwnedTextFieldIsDisabledWithItsSource();
  await anOwnedSecretIsDisabledWithNoShowOrClear();
  await anOwnedChoiceSelectAndSegmentAreDisabled();
  await anOwnedSwitchDoesNotToggle();
  await savingAndTestingNeverSendOwnedKeys();
  await everyControlOfAnOwnedFieldIsDisabledEvenWithAngleBracketsInItsText();
  await aPartlyOwnedInstanceSendsOnlyTheEditedFieldThatIsNotOwned();
  await arrowKeysAndEnterOnAnOwnedSegmentChangeNothing();
  await theEntryChangeAndClearButtonsOfAnOwnedFieldDoNothing();
  await aDisabledCheckedSwitchAndSegmentStayDistinctFromUnchecked();
  await aFieldNotOwnedStaysEditable();
})().catch((e) => { console.error('FAIL', e); process.exit(1); });
