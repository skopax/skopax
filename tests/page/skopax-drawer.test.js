const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

const F = (key, label, kind, over) => Object.assign({key, label, kind, required: false, default: '', help: '', choices: [], low: null, high: null, showIf: null, ui: false}, over || {});
const FIELDS = [
  F('url', 'URL', 'url', {required: true, help: 'Where it listens', group: 'connection'}),
  F('mode', 'Mode', 'choice', {required: true, default: 'agent', choices: ['in-cluster', 'agent'], group: 'connection'}),
  F('note', 'Note', 'text'),
  F('auth', 'Sign-in', 'choice', {default: 'none', choices: ['none', 'header', 'oauth'], choiceLabels: {none: 'No sign-in', header: 'Token in a header', oauth: 'OAuth'}, group: 'sign-in'}),
  F('token', 'Token', 'secret', {showIf: ['auth', 'header'], group: 'sign-in'}),
  F('level', 'Lowest level', 'choice', {default: 'warn', choices: ['info', 'warn', 'crit', 'page_now'], choiceLabels: {info: 'Info', warn: 'Warning', crit: 'Critical'}, group: 'access'}),
  F('live', 'Live reads', 'bool', {default: 'false', help: 'Reads as it goes', group: 'access'}),
  F('ca', 'CA certificate', 'pem'),
  F('relay', 'Relay', 'text', {group: 'advanced'}),
];

function instance(name, over) {
  return Object.assign({name, primary: false, enabled: true, source: 'admin', problem: null, values: {url: 'https://' + name + '.example.com', mode: 'agent', auth: 'header', level: 'warn', live: 'false'},
    secrets: {token: {set: true, from: null}}, owned: [], offChecks: []}, over || {});
}

function catalog() {
  return {
    edition: 'community', secretsKey: 'generated',
    plugins: [
      {id: 'shop', name: 'Shop', version: '1.0.0', category: 'metrics', description: 'Reads the shop.', edition: 'community', icon: true, state: 'ok', reason: null, enabled: true, fields: FIELDS, checks: [],
        instances: [instance('main', {primary: true}), instance('east', {values: {url: 'https://east.example.com', relay: 'hub'}, secrets: {token: {set: true, from: 'settings file'}}})],
        health: {state: 'ok', text: 'Healthy', at: iso(2), instances: []}},
      {id: 'kube', name: 'Kube', version: '1.0.0', category: 'infrastructure', description: '', edition: 'community', icon: false, state: 'ok', reason: null, enabled: true, checks: [],
        fields: [F('mode', 'Mode', 'choice', {required: true, default: 'agent', choices: ['in-cluster', 'agent']}), F('url', 'URL', 'url')],
        instances: [instance('north', {primary: true, values: {mode: 'in-cluster'}, secrets: {}})], health: {state: 'ok', text: 'Healthy', at: iso(2), instances: []}},
    ],
  };
}

async function boot(opts) {
  opts = opts || {};
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const flags = {refuse: null};
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/#t=anoms', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, o) => {
        const p = String(path), method = (o && o.method) || 'GET', body = o && o.body ? JSON.parse(o.body) : null;
        const ok = (b, status) => ({ok: true, status: status || 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        const bad = (status, detail, field) => ({ok: false, status, json: async () => ({detail, field})});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', userRole: 'admin', operator: true, admin: true, teams: [], tags: []});
        if (p.startsWith('/api/admin')) {
          calls.push([method, p, body]);
          if (p === '/api/admin/plugins' && method === 'GET') return ok(opts.catalog || catalog());
          if (flags.refuse && method !== 'GET') return bad(400, flags.refuse[1], flags.refuse[0]);
          if (p === '/api/admin/notices') return ok({items: []});
          if (p.endsWith('/test')) return ok({ok: true, summary: 'answered', ms: 12});
          if (p.endsWith('/instances') && method === 'POST') return ok(instance(body.name), 201);
          if (p.includes('/instances/') && method === 'PATCH') return ok({instance: instance('main')});
          if (p.startsWith('/api/admin/history')) return ok([]);
          if (p.startsWith('/api/admin/small-talk')) return bad(404, 'none');
          if (p.startsWith('/api/admin/settings')) return ok({items: [], rev: 1, refreshSeconds: 15});
          if (p.startsWith('/api/admin/users') || p.startsWith('/api/admin/tokens')) return ok([]);
        }
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(150);
  dom.window.document.getElementById('tab-integrations').click();
  await wait(120);
  return {w: dom.window, doc: dom.window.document, errors, calls, flags};
}

const panel = (doc) => doc.getElementById('int-panel');
const inPanel = (doc, sel) => panel(doc).querySelector(sel);
const field = (doc, key) => [...panel(doc).querySelectorAll('.int-form .int-field')].filter((f) => f.dataset.key === key)[0];
const carrier = (doc, key) => field(doc, key).querySelector('.int-in');
const tile = (doc, id) => [...doc.querySelectorAll('#integrations .int-tile')].filter((s) => s.dataset.id === id)[0];
const itab = (doc, name) => [...panel(doc).querySelectorAll('.int-itab')].filter((b) => b.dataset.name === name)[0];
const key = (w, el, k, shift) => el.dispatchEvent(new w.KeyboardEvent('keydown', {key: k, shiftKey: !!shift, bubbles: true, cancelable: true}));
const segs = (doc, k) => [...field(doc, k).querySelectorAll('.int-seg [role="radio"]')];
const setValue = (w, el, v) => { el.value = v; el.dispatchEvent(new w.Event('input', {bubbles: true})); el.dispatchEvent(new w.Event('change', {bubbles: true})); };

function openTile(t, id) { tile(t.doc, id).querySelector('.int-open').click(); }
function openInstance(t, id, name) { openTile(t, id); const b = itab(t.doc, name); if (b) b.click(); }
function addInstance(t, id) { openTile(t, id); inPanel(t.doc, '.int-add').click(); }

async function theFormShowsUntitledFieldsFirstThenEachGroupInOrderAndFoldsAdvancedLast() {
  //given
  const t = await boot();

  //when
  addInstance(t, 'shop');
  const form = inPanel(t.doc, '.int-form');
  const parts = [...form.children].filter((c) => c.matches('.int-grp, .int-adv, .int-checks'));
  const layout = parts.map((g) => [g.matches('details') ? 'details' : g.tagName.toLowerCase(), (g.querySelector(':scope > .int-grp-h, :scope > summary') || {textContent: ''}).textContent,
    [...g.querySelectorAll('.int-field')].map((f) => f.dataset.key)]);
  const adv = inPanel(t.doc, '.int-adv');
  const labelled = [...form.querySelectorAll('section.int-grp')].every((s) => t.doc.getElementById(s.getAttribute('aria-labelledby')) === s.querySelector('.int-grp-h'));

  //then
  assert.deepStrictEqual(layout, [
    ['div', '', ['name', 'note']],
    ['section', 'Connection', ['url', 'mode']],
    ['section', 'Sign-in', ['auth', 'token']],
    ['section', 'Access', ['level', 'live']],
    ['details', 'Advanced', ['ca', 'relay']],
  ], 'the name and the untitled fields first, then the groups in the order their fields come, and pem or advanced fields in a folded Advanced last');
  assert.ok(labelled, 'each titled section is labelled by its heading');
  assert.strictEqual(adv.open, false, 'an empty add form keeps Advanced folded');
  assert.strictEqual(adv.querySelector('summary').textContent, 'Advanced');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Drawer: untitled fields first, then each group in field order, and Advanced folded last');
}

async function theHeaderShowsTheIconNameModeAndDescriptionAndTheFooterHoldsTheActions() {
  //given
  const t = await boot();
  const css = require('fs').readFileSync(TEMPLATE, 'utf8');

  //when
  addInstance(t, 'shop');
  const head = [inPanel(t.doc, '.int-ph img.ico').getAttribute('src'), inPanel(t.doc, '.int-ph h2').textContent, inPanel(t.doc, '.int-ph .int-ph-sub').textContent, inPanel(t.doc, '.int-desc').textContent];
  const foot = inPanel(t.doc, '.int-form > .int-foot');
  const order = [...foot.querySelectorAll('button')].map((b) => b.className.split(' ').filter((c) => /^int-(test|cancel|save|save-on|del)$/.test(c))[0]);
  const last = inPanel(t.doc, '.int-form').lastElementChild === foot;
  const heading = !!inPanel(t.doc, '.int-form > b');
  openInstance(t, 'shop', 'east');
  const editSub = inPanel(t.doc, '.int-ph .int-ph-sub');

  //then
  assert.deepStrictEqual(head, ['/plugins/shop/icon.svg', 'Shop', 'Add instance', 'Reads the shop.']);
  assert.deepStrictEqual(order, ['int-test', 'int-cancel', 'int-save', 'int-save-on'], 'Test connection first, then Cancel and the saves');
  assert.ok(last && !heading, 'the footer closes the form and the old in-form heading is gone');
  assert.ok(!editSub, 'an existing instance shows no Add instance');
  const footBottom = /\.int-foot \{[^}]*position: sticky;[^}]*bottom: -(\d+)px;[^}]*margin: 0 -(\d+)px;/.exec(css), panelPad = /\.int-panel \{[^}]*padding: (\d+)px (\d+)px;[^}]*overflow-y: auto;/.exec(css);
  assert.ok(footBottom && panelPad && footBottom[1] === panelPad[1] && footBottom[2] === panelPad[2], 'the footer sticks flush to the bottom of the drawer, over its padding, while the body scrolls');
  t.w.close();
  console.log('PASS Drawer: the header shows icon, name, Add instance and the description; the sticky footer holds Test, Cancel and Save');
}

async function aBoolIsASwitchThatTogglesByClickAndKeyAndSubmitsTrueOrFalse() {
  //given
  const t = await boot();
  addInstance(t, 'shop');
  const sw = field(t.doc, 'live').querySelector('.int-tog');

  //when
  const first = [sw.tagName, sw.getAttribute('role'), sw.getAttribute('aria-checked'), carrier(t.doc, 'live').value, sw.labels[0].textContent, t.doc.getElementById(sw.getAttribute('aria-labelledby')).textContent];
  sw.click();
  const clicked = [sw.getAttribute('aria-checked'), carrier(t.doc, 'live').value];
  key(t.w, sw, ' ');
  const spaced = [sw.getAttribute('aria-checked'), carrier(t.doc, 'live').value];
  key(t.w, sw, 'Enter');
  const entered = [sw.getAttribute('aria-checked'), carrier(t.doc, 'live').value];
  const up = new t.w.KeyboardEvent('keyup', {key: ' ', bubbles: true, cancelable: true});
  sw.dispatchEvent(up);
  const once = [up.defaultPrevented, sw.getAttribute('aria-checked')];
  inPanel(t.doc, '.int-name-in').value = 'west';
  setValue(t.w, carrier(t.doc, 'url'), 'https://west.example.com');
  inPanel(t.doc, '.int-save').click();
  await wait(100);

  //then
  assert.deepStrictEqual(first, ['BUTTON', 'switch', 'false', 'false', 'Live reads', 'Live reads']);
  assert.deepStrictEqual([clicked, spaced, entered], [['true', 'true'], ['false', 'false'], ['true', 'true']]);
  assert.deepStrictEqual(once, [true, 'true'], 'the Space keyup is held back, so a browser cannot fire a second click');
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'POST').map((c) => c[2].values.live), ['true'], 'the switch still submits the text true or false');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Drawer: a bool is a named switch that toggles by click, Space and Enter and submits true or false');
}

async function aLockedSwitchIgnoresClicksAndAValueSetFromOutsideRepaintsIt() {
  //given
  const t = await boot();
  addInstance(t, 'shop');
  const sw = field(t.doc, 'live').querySelector('.int-tog');

  //when
  setValue(t.w, carrier(t.doc, 'live'), 'true');
  const repainted = sw.getAttribute('aria-checked');
  carrier(t.doc, 'live').disabled = true;
  setValue(t.w, carrier(t.doc, 'url'), 'https://west.example.com');
  sw.click();
  const locked = [sw.getAttribute('aria-disabled'), sw.getAttribute('aria-checked'), carrier(t.doc, 'live').value];
  inPanel(t.doc, '.int-test').click();
  await wait(80);
  sw.click();
  const afterTest = [sw.getAttribute('aria-disabled'), carrier(t.doc, 'live').value];

  //then
  assert.strictEqual(repainted, 'true');
  assert.deepStrictEqual(locked, ['true', 'true', 'true']);
  assert.deepStrictEqual(afterTest, ['true', 'true'], 'a Test connection run does not unlock a locked switch');
  t.w.close();
  console.log('PASS Drawer: a locked switch ignores clicks, also after a test, and a value set from outside repaints it');
}

async function aShortChoiceIsASegmentedRadioGroupWithArrowKeysAndLabels() {
  //given
  const t = await boot();
  addInstance(t, 'shop');
  const group = field(t.doc, 'auth').querySelector('.int-seg');

  //when
  const shown = [group.getAttribute('role'), t.doc.getElementById(group.getAttribute('aria-labelledby')).textContent, segs(t.doc, 'auth').map((b) => b.textContent),
    segs(t.doc, 'auth').map((b) => b.getAttribute('aria-checked')), segs(t.doc, 'auth').map((b) => b.getAttribute('tabindex')), segs(t.doc, 'mode').map((b) => b.textContent), field(t.doc, 'token').hidden];
  segs(t.doc, 'auth')[0].focus();
  key(t.w, segs(t.doc, 'auth')[0], 'ArrowRight');
  const right = [carrier(t.doc, 'auth').value, t.doc.activeElement === segs(t.doc, 'auth')[1], segs(t.doc, 'auth').map((b) => b.getAttribute('tabindex')), field(t.doc, 'token').hidden];
  key(t.w, t.doc.activeElement, 'ArrowLeft');
  key(t.w, t.doc.activeElement, 'ArrowLeft');
  const wrapped = [carrier(t.doc, 'auth').value, t.doc.activeElement === segs(t.doc, 'auth')[2]];
  key(t.w, t.doc.activeElement, 'Home');
  const home = carrier(t.doc, 'auth').value;
  key(t.w, t.doc.activeElement, 'End');
  const end = carrier(t.doc, 'auth').value;
  segs(t.doc, 'auth')[1].click();
  const clicked = [carrier(t.doc, 'auth').value, segs(t.doc, 'auth')[1].getAttribute('aria-checked'), field(t.doc, 'token').hidden];
  setValue(t.w, carrier(t.doc, 'auth'), 'none');
  const outside = segs(t.doc, 'auth').map((b) => b.getAttribute('aria-checked'));

  //then
  assert.deepStrictEqual(shown, ['radiogroup', 'Sign-in', ['No sign-in', 'Token in a header', 'OAuth'], ['true', 'false', 'false'], ['0', '-1', '-1'], ['In-cluster', 'Agent'], true],
    'labels where given, a humanised value otherwise, and one tab stop on the checked option');
  assert.deepStrictEqual(right, ['header', true, ['-1', '0', '-1'], false], 'an arrow key moves the choice and the focus, and showIf follows');
  assert.deepStrictEqual(wrapped, ['oauth', true], 'the arrows wrap around');
  assert.deepStrictEqual([home, end], ['none', 'oauth']);
  assert.deepStrictEqual(clicked, ['header', 'true', false]);
  assert.deepStrictEqual(outside, ['true', 'false', 'false'], 'a value set on the field repaints the segments');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Drawer: a choice of two or three is a labelled segmented radio group with arrow, Home and End keys');
}

async function aLongerChoiceIsASelectWithReadableLabels() {
  //given
  const t = await boot();

  //when
  addInstance(t, 'shop');
  const select = carrier(t.doc, 'level');

  //then
  assert.strictEqual(select.tagName, 'SELECT');
  assert.deepStrictEqual([...select.options].map((o) => [o.value, o.textContent]), [['info', 'Info'], ['warn', 'Warning'], ['crit', 'Critical'], ['page_now', 'Page now']]);
  assert.strictEqual(select.value, 'warn');
  assert.ok(!field(t.doc, 'level').querySelector('.int-seg'));
  t.w.close();
  console.log('PASS Drawer: a choice of four or more is a select showing its labels, or a humanised value');
}

async function aSecretShowsAndHidesWhatWasTypedAndSaysWhereItIsSet() {
  //given
  const t = await boot();

  //when
  openInstance(t, 'shop', 'east');
  setValue(t.w, carrier(t.doc, 'auth'), 'header');
  const eye = field(t.doc, 'token').querySelector('.int-eye');
  const input = carrier(t.doc, 'token');
  const chipFrom = field(t.doc, 'token').querySelector('.int-set').textContent;
  const before = [input.type, eye.getAttribute('aria-pressed'), eye.textContent, eye.getAttribute('aria-label'), eye.getAttribute('aria-controls') === input.id];
  input.value = 'sekret';
  eye.click();
  const shown = [input.type, eye.getAttribute('aria-pressed'), eye.textContent, eye.getAttribute('aria-label'), input.value];
  eye.click();
  const hidden = [input.type, eye.getAttribute('aria-pressed')];
  field(t.doc, 'token').querySelector('.int-clear').click();
  const clearing = [field(t.doc, 'token').querySelector('.int-set').textContent, input.disabled, field(t.doc, 'token').querySelector('.int-clear').textContent];
  openInstance(t, 'shop', 'main');
  const chipSet = field(t.doc, 'token').querySelector('.int-set').textContent;
  inPanel(t.doc, '.int-add').click();
  const unset = [!!field(t.doc, 'token').querySelector('.int-set'), carrier(t.doc, 'token').placeholder];

  //then
  assert.strictEqual(chipFrom, 'From settings file');
  assert.deepStrictEqual(before, ['password', 'false', 'Show', 'Show Token', true]);
  assert.deepStrictEqual(shown, ['text', 'true', 'Hide', 'Hide Token', 'sekret']);
  assert.deepStrictEqual(hidden, ['password', 'false']);
  assert.deepStrictEqual(clearing, ['Cleared on save', true, 'Keep'], 'the Clear button stays and the chip says what Save does');
  assert.strictEqual(chipSet, 'Set');
  assert.deepStrictEqual(unset, [false, 'not set'], 'an unset secret has no chip and keeps its placeholder');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Drawer: a secret has a show and hide button and a chip that says it is set and from where');
}

async function advancedOpensOnItsOwnForAValueOrARefusalInside() {
  //given
  const t = await boot();

  //when
  openInstance(t, 'shop', 'east');
  const withValue = inPanel(t.doc, '.int-adv').open;
  openInstance(t, 'shop', 'main');
  const without = inPanel(t.doc, '.int-adv').open;
  t.flags.refuse = ['ca', 'The certificate does not parse'];
  setValue(t.w, carrier(t.doc, 'url'), 'https://main2.example.com');
  inPanel(t.doc, '.int-save').click();
  await wait(100);
  const refused = [inPanel(t.doc, '.int-adv').open, field(t.doc, 'ca').querySelector('.int-field-msg').textContent];

  //then
  assert.deepStrictEqual([withValue, without], [true, false], 'Advanced opens when one of its fields holds a value that is not the default');
  assert.deepStrictEqual(refused, [true, 'The certificate does not parse'], 'and when one of its fields is refused');
  t.w.close();
  console.log('PASS Drawer: Advanced opens on its own for a value that is not the default and for a refusal inside it');
}

async function focusStartsOnTheFirstRealControlAndTheTrapWraps() {
  //given
  const t = await boot();

  //when
  openInstance(t, 'kube', 'north');
  const first = [t.doc.activeElement.getAttribute('role'), t.doc.activeElement.textContent];
  inPanel(t.doc, '.int-close').click();
  openTile(t, 'shop');
  const shopFirst = t.doc.activeElement === carrier(t.doc, 'note');
  const last = [...inPanel(t.doc, '.int-foot').querySelectorAll('button')].pop();
  last.focus();
  key(t.w, last, 'Tab');
  const wrapped = t.doc.activeElement === inPanel(t.doc, '.int-close');
  key(t.w, t.doc.activeElement, 'Tab', true);
  const back = t.doc.activeElement === last;

  //then
  assert.deepStrictEqual(first, ['radio', 'In-cluster'], 'an edit form that starts with a segmented choice focuses its checked option');
  assert.ok(shopFirst, 'otherwise the first field input, the untitled one at the top');
  assert.ok(wrapped, 'Tab from the last footer button wraps to the first control');
  assert.ok(back, 'Shift+Tab from the first control wraps to the last footer button');
  t.w.close();
  console.log('PASS Drawer: focus starts on the first real control, a segmented checked option included, and the trap wraps at both ends');
}

(async () => {
  await theFormShowsUntitledFieldsFirstThenEachGroupInOrderAndFoldsAdvancedLast();
  await theHeaderShowsTheIconNameModeAndDescriptionAndTheFooterHoldsTheActions();
  await aBoolIsASwitchThatTogglesByClickAndKeyAndSubmitsTrueOrFalse();
  await aLockedSwitchIgnoresClicksAndAValueSetFromOutsideRepaintsIt();
  await aShortChoiceIsASegmentedRadioGroupWithArrowKeysAndLabels();
  await aLongerChoiceIsASelectWithReadableLabels();
  await aSecretShowsAndHidesWhatWasTypedAndSaysWhereItIsSet();
  await advancedOpensOnItsOwnForAValueOrARefusalInside();
  await focusStartsOnTheFirstRealControlAndTheTrapWraps();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
