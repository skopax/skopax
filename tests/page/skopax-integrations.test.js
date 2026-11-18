const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const TEMPLATE_CSS = () => require('fs').readFileSync(TEMPLATE, 'utf8');
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const EVIL = '<img src=x onerror=alert(1)>"\'';
const DOT = '\u00b7';

const FIELDS = [
  {key: 'url', label: 'URL', kind: 'url', required: true, default: '', help: 'Where it listens', choices: [], low: null, high: null, showIf: null, ui: true},
  {key: 'auth', label: 'Authentication', kind: 'choice', required: false, default: 'basic', help: '', choices: ['basic', 'bearer'], low: null, high: null, showIf: null, ui: false},
  {key: 'token', label: 'Token', kind: 'secret', required: false, default: '', help: '', choices: [], low: null, high: null, showIf: ['auth', 'bearer'], ui: false},
];
const CHECKS = [{id: 'pulse', label: 'Pulse', period: 900, offset: 0, ran: ['acme']}, {id: 'beat', label: 'Beat', period: 900, offset: 0, ran: ['beat']}];
const OFF = 'Off \u00b7 set up, paused';

const NOTICES = [
  {id: 'secret-key', level: 'info', text: 'Secrets are sealed with a key that Skopax generated and keeps in its database. Set SKOPAX_SECRET_KEY to manage the key yourself.'},
  {id: 'source:feed', level: 'warn', text: 'Settings source feed is not being read (TimeoutError); settings from it may be missing or out of date.', at: new Date(Date.now() - 5 * 60000).toISOString()},
];

function instance(name, over) {
  return Object.assign({name, primary: false, enabled: true, source: 'admin', problem: null, values: {url: 'https://' + name + '.example.com', auth: 'bearer'},
    secrets: {token: {set: true, from: null}}, owned: [], offChecks: []}, over || {});
}

function health(state, text, at, rows) {
  return {state, text, at: at || null, instances: rows || []};
}

function catalog() {
  return {
    edition: 'community', secretsKey: 'generated',
    plugins: [
      {id: 'acme', name: 'Acme metrics', version: '1.0.0', category: 'metrics', description: 'Reads Acme.', edition: 'community', icon: true, state: 'ok', reason: null, enabled: true, fields: FIELDS, checks: CHECKS,
        instances: [instance('main', {primary: true, source: 'config file', owned: ['token', 'url'], secrets: {token: {set: true, from: 'config file'}}}), instance('east')],
        health: health('ok', 'Healthy', iso(2), [{name: 'main', state: 'ok', text: 'Healthy', at: iso(2)}, {name: 'east', state: 'ok', text: 'Healthy', at: iso(4)}])},
      {id: 'pager', name: 'Pager', version: '1.0.0', category: 'alerts', description: 'Reads pages.', edition: 'community', icon: true, state: 'ok', reason: null, enabled: true, fields: [], checks: [CHECKS[0]], instances: [],
        health: health('error', 'Token rejected (403)', iso(6))},
      {id: 'gold', name: 'Gold board', version: '1.0.0', category: 'tickets', description: '', edition: 'enterprise', icon: false, state: 'locked', reason: 'Gold board needs the Enterprise edition of Skopax', enabled: false, fields: FIELDS, checks: [],
        instances: [instance('main', {primary: true})], health: health('off', OFF, null, [{name: 'main', state: 'off', text: OFF, at: null}])},
      {id: 'cracked', module: 'cracked', reason: 'cracked failed to start (RuntimeError)', enabled: true},
      {id: 'odd.module', module: 'odd.module', reason: 'odd.module was not loaded: it could not be imported (ImportError)'},
      {id: 'evil', name: EVIL, version: '1.0.0', category: 'other', description: EVIL, edition: 'community', icon: false, state: 'ok', reason: null, enabled: false, fields: FIELDS, checks: CHECKS,
        instances: [instance('faulty', {enabled: false, problem: EVIL, values: {url: EVIL}})], health: health('off', OFF, null, [{name: 'faulty', state: 'off', text: OFF, at: null}])},
      {id: 'prometheus', name: 'Prometheus', version: '1.0.0', category: 'metrics', description: 'Errors and latency.', edition: 'community', icon: true, state: 'ok', reason: null, enabled: true, fields: FIELDS, checks: CHECKS,
        instances: [], health: health('off', 'Not set up yet')},
      {id: 'relic', name: 'Relic', version: '1.0.0', category: 'infrastructure', description: '', edition: 'community', icon: false, state: 'incompatible', reason: 'Relic needs core >=9 (this is 0.1.0)', enabled: false, fields: [], checks: [],
        instances: [], health: health('off', OFF)},
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
  const admin = opts.admin !== false;
  const flags = {hook: null, catalog: null};
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/#t=anoms', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, o) => {
        const p = String(path), method = (o && o.method) || 'GET', body = o && o.body ? JSON.parse(o.body) : null;
        const ok = (b, status) => ({ok: true, status: status || 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        const bad = (status, detail, field) => ({ok: false, status, json: async () => ({detail, field})});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', userRole: admin ? 'admin' : 'operator', operator: true, admin, teams: [], tags: []});
        if (p.startsWith('/api/admin')) {
          calls.push([method, p, body]);
          if (!admin) return bad(403, 'Admins only');
          const hooked = flags.hook && await flags.hook(method, p, body, {ok, bad});
          if (hooked) return hooked;
          if (p === '/api/admin/plugins' && method === 'GET') return ok(flags.catalog ? flags.catalog() : opts.catalog || catalog());
          if (p === '/api/admin/notices') return ok({items: NOTICES});
          if (p.endsWith('/test')) return ok({ok: true, summary: 'answered', ms: 12});
          if (p.endsWith('/instances') && method === 'POST') return ok(instance(body.name), 201);
          if (p.includes('/instances/') && method === 'PATCH') return ok({instance: instance('east')});
          if (p.includes('/instances/') && method === 'DELETE') return {ok: true, status: 204, json: async () => { throw new Error('no body'); }};
          if (method === 'PUT') return ok({id: 'acme'});
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
  return {w: dom.window, doc: dom.window.document, errors, calls, flags};
}

async function open(t) {
  t.doc.getElementById('tab-integrations').click();
  await wait(120);
}
const tile = (doc, id) => [...doc.querySelectorAll('#integrations .int-tile')].filter((s) => s.dataset.id === id)[0];
const tiles = (doc) => [...doc.querySelectorAll('#integrations .int-tile')];
const shown = (doc) => tiles(doc).filter((x) => !x.hidden).map((x) => x.dataset.id);
const wrap = (doc) => doc.getElementById('int-wrap');
const panel = (doc) => doc.getElementById('int-panel');
const itab = (doc, name) => [...panel(doc).querySelectorAll('.int-itab')].filter((b) => b.dataset.name === name)[0];
const field = (doc, key) => [...panel(doc).querySelectorAll('.int-form .int-field')].filter((f) => f.dataset.key === key)[0];
const inPanel = (doc, sel) => panel(doc).querySelector(sel);
const checks = (doc) => [...panel(doc).querySelectorAll('.int-checks .int-check')];
const picks = (doc) => [...doc.querySelectorAll('#int-pop .int-pick')];
const pick = (doc, id) => picks(doc).filter((b) => b.dataset.id === id)[0];
const setValue = (w, el, v) => { el.value = v; el.dispatchEvent(new w.Event('input', {bubbles: true})); el.dispatchEvent(new w.Event('change', {bubbles: true})); };
const key = (w, el, k, shift) => el.dispatchEvent(new w.KeyboardEvent('keydown', {key: k, shiftKey: !!shift, bubbles: true, cancelable: true}));
const addBtn = (doc) => doc.getElementById('int-add-btn');

function openTile(t, id) {
  tile(t.doc, id).querySelector('.int-open').click();
}
function openInstance(t, id, name) {
  openTile(t, id);
  const b = itab(t.doc, name);
  if (b) b.click();
}
function addInstance(t, id) {
  openTile(t, id);
  inPanel(t.doc, '.int-add').click();
}

async function theTilesShowEveryConnectedPluginWithItsStateHealthAndIcon() {
  //given
  const t = await boot();

  //when
  await open(t);
  const {doc} = t;

  //then
  assert.ok(!doc.getElementById('panel-integrations').hidden, 'the Integrations panel opens');
  assert.deepStrictEqual(tiles(doc).map((s) => [s.dataset.id, s.dataset.state]), [['acme', 'on'], ['pager', 'on'], ['gold', 'locked'], ['cracked', 'broken'], ['evil', 'off']], 'connected plug-ins only: not prometheus (no instance), not relic (off, nothing to set up), not odd.module (did not load)');
  const acme = tile(doc, 'acme');
  assert.strictEqual(acme.querySelector('img.ico').getAttribute('src'), '/plugins/acme/icon.svg');
  assert.strictEqual(acme.querySelector('img.ico').getAttribute('alt'), '');
  assert.deepStrictEqual([acme.querySelector('.int-name').textContent, acme.querySelector('.int-cat').textContent], ['Acme metrics', 'Metrics']);
  assert.deepStrictEqual([acme.querySelector('.int-switch').getAttribute('role'), acme.querySelector('.int-switch').getAttribute('aria-checked'), acme.querySelector('.int-switch').getAttribute('aria-label')], ['switch', 'true', 'Switch Acme metrics']);
  assert.strictEqual(acme.querySelector('.int-health').textContent, 'Healthy ' + DOT + ' 2 min ago');
  assert.ok(acme.querySelector('.int-dot.ok'), 'a green dot');
  assert.strictEqual(tile(doc, 'pager').querySelector('.int-health').textContent, 'Token rejected (403)');
  assert.ok(tile(doc, 'pager').querySelector('.int-dot.error'), 'the error text with a red dot');
  assert.ok(tile(doc, 'evil').classList.contains('off') && !acme.classList.contains('off'), 'an off plug-in is styled off');
  assert.strictEqual(tile(doc, 'evil').querySelector('.int-health').textContent, OFF);
  assert.strictEqual(tile(doc, 'cracked').querySelector('.int-name').textContent, 'cracked');
  assert.ok(/RuntimeError/.test(tile(doc, 'cracked').querySelector('.int-health').textContent) && tile(doc, 'cracked').querySelector('.int-dot.error'), 'a broken plug-in shows why');
  assert.ok(!tile(doc, 'gold').querySelector('img') && tile(doc, 'gold').querySelector('.int-mono').textContent === 'G', 'no icon: a letter tile, never an inlined image');
  assert.ok(!doc.querySelector('#integrations .int-tile svg'), 'no inline svg in a tile');
  assert.ok(/generated/.test(doc.querySelector('#adm-notices [data-id="secret-key"]').textContent), 'the generated-key warning shows in the notices strip at the top');
  assert.ok(/feed/.test(doc.querySelector('#adm-notices [data-id="source:feed"]').textContent) && /TimeoutError/.test(doc.querySelector('#adm-notices [data-id="source:feed"]').textContent), 'a failing source shows why');
  assert.ok(!doc.querySelector('#integrations .int-warn') && !doc.querySelector('#integrations .int-source-error'), 'and only there: the page draws no banner of its own');
  assert.ok(!doc.getElementById('integrations').textContent.includes('ImportError'), 'a plug-in that did not load is only in the notices');
  assert.ok(!doc.querySelector('#panel-integrations .panel-note'), 'no table-era note above the header');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Admin, Integrations: tiles show the connected plug-ins with icon, category, switch, health and off styling; notices stay in the strip');
}

async function theSubtitleCountsAndTheChipsFilterTheTiles() {
  //given
  const t = await boot();
  await open(t);
  const {doc} = t;
  const chip = (label) => [...doc.querySelectorAll('#integrations .int-chip')].filter((c) => c.textContent === label)[0];

  //when
  const sub = doc.querySelector('#integrations .int-sub').textContent;
  const labels = [...doc.querySelectorAll('#integrations .int-chip')].map((c) => [c.textContent, c.getAttribute('aria-pressed')]);
  chip('Metrics').click();
  const metrics = [shown(doc), chip('Metrics').getAttribute('aria-pressed'), chip('All').getAttribute('aria-pressed')];
  chip('Other').click();
  const other = shown(doc);
  chip('All').click();
  const all = shown(doc);

  //then
  assert.strictEqual(sub, '2 connected ' + DOT + ' 3 need attention', 'connected: acme and pager; pager fails, gold is locked and cracked is broken');
  assert.strictEqual(doc.getElementById('count-integrations').textContent, '2', 'the sub-nav counts the same');
  assert.deepStrictEqual(labels, [['All', 'true'], ['Metrics', 'false'], ['Alerts', 'false'], ['Tickets', 'false'], ['Other', 'false']], 'All, then the categories present, labelled by the page map');
  assert.deepStrictEqual(metrics, [['acme'], 'true', 'false']);
  assert.deepStrictEqual(other, ['cracked', 'evil'], 'a plug-in without a category counts as Other');
  assert.deepStrictEqual(all, ['acme', 'pager', 'gold', 'cracked', 'evil']);
  t.w.close();

  const one = catalog();
  one.plugins = [one.plugins[0], one.plugins[1], Object.assign({}, one.plugins[6], {instances: [instance('main')], health: health('pending', 'Waiting for the first check')})];
  const u = await boot({catalog: one});
  await open(u);
  assert.strictEqual(u.doc.querySelector('#integrations .int-sub').textContent, '3 connected ' + DOT + ' 1 needs attention');
  assert.strictEqual(u.doc.getElementById('count-integrations').textContent, '3');
  assert.strictEqual(tile(u.doc, 'prometheus').querySelector('.int-health').textContent, 'Waiting for the first check');
  assert.ok(tile(u.doc, 'prometheus').querySelector('.int-dot.pending'));
  u.w.close();
  console.log('PASS Admin, Integrations: the subtitle counts what is connected and what needs attention, and the chips filter the tiles');
}

async function aLockedSwitchIsDisabledAndShowsTheReason() {
  //given
  const t = await boot();
  await open(t);
  const gold = tile(t.doc, 'gold');
  const sw = gold.querySelector('.int-switch');

  //when
  sw.click();
  await wait(40);
  openTile(t, 'gold');

  //then
  assert.deepStrictEqual([sw.getAttribute('aria-disabled'), sw.getAttribute('aria-checked'), sw.title], ['true', 'false', 'Gold board needs the Enterprise edition of Skopax']);
  assert.strictEqual(gold.querySelector('.int-reason').textContent, 'Gold board needs the Enterprise edition of Skopax');
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'PUT'), [], 'an off switch that cannot be used sends nothing');
  assert.ok(!wrap(t.doc).hidden && inPanel(t.doc, '.int-reason').textContent === 'Gold board needs the Enterprise edition of Skopax', 'the panel says why');
  assert.ok(!inPanel(t.doc, '.int-add') && !inPanel(t.doc, '.int-form'), 'no instance can be added to a locked plug-in');
  t.w.close();
  console.log('PASS Admin, Integrations: a locked plug-in that is off has a disabled switch and shows its reason');
}

async function aStoredOnPluginThatCannotRunCanStillBeSwitchedOff() {
  //given
  const cat = catalog();
  cat.plugins[2].enabled = true;
  cat.plugins[7] = Object.assign({}, cat.plugins[7], {fields: FIELDS, enabled: true});
  const t = await boot({catalog: cat});
  await open(t);
  const cracked = tile(t.doc, 'cracked').querySelector('.int-switch'), gold = tile(t.doc, 'gold').querySelector('.int-switch');
  const relic = tile(t.doc, 'relic') && tile(t.doc, 'relic').querySelector('.int-switch');
  const before = [[cracked.getAttribute('aria-checked'), cracked.hasAttribute('aria-disabled'), cracked.title], [gold.getAttribute('aria-checked'), gold.hasAttribute('aria-disabled')],
    relic && [relic.getAttribute('aria-checked'), relic.hasAttribute('aria-disabled'), relic.title]];

  //when
  cracked.click();
  await wait(80);
  tile(t.doc, 'gold').querySelector('.int-switch').click();
  await wait(80);
  tile(t.doc, 'relic').querySelector('.int-switch').click();
  await wait(80);

  //then
  assert.deepStrictEqual(before, [['true', false, 'cracked failed to start (RuntimeError)'], ['true', false], ['true', false, 'Relic needs core >=9 (this is 0.1.0)']], 'stored on with fields and no instance still gets a tile');
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'PUT'), [['PUT', '/api/admin/plugins/cracked', {enabled: false}], ['PUT', '/api/admin/plugins/gold', {enabled: false}], ['PUT', '/api/admin/plugins/relic', {enabled: false}]]);
  t.w.close();
  console.log('PASS Admin, Integrations: a broken, locked or incompatible plug-in that is stored on can always be switched off');
}

async function theSwitchSendsTheChoiceAndPutsItBackOnARefusal() {
  //given
  const t = await boot();
  await open(t);
  t.flags.hook = async (method, p, body, r) => method === 'PUT' && p === '/api/admin/plugins/evil' ? r.bad(409, 'Not now') : null;

  //when
  tile(t.doc, 'evil').querySelector('.int-switch').click();
  await wait(80);
  const refused = [tile(t.doc, 'evil').querySelector('.int-switch').getAttribute('aria-checked'), tile(t.doc, 'evil').querySelector('.int-msg').textContent, tile(t.doc, 'evil').querySelector('.int-msg').classList.contains('bad')];
  t.flags.hook = null;
  tile(t.doc, 'acme').querySelector('.int-switch').click();
  await wait(80);

  //then
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'PUT'), [['PUT', '/api/admin/plugins/evil', {enabled: true}], ['PUT', '/api/admin/plugins/acme', {enabled: false}]]);
  assert.deepStrictEqual(refused, ['false', 'Not now', true]);
  assert.strictEqual(t.doc.querySelector('#integrations .int-note').textContent, 'Acme metrics switched off');
  assert.ok(wrap(t.doc).hidden, 'the switch does not open the panel');
  t.w.close();
  console.log('PASS Admin, Integrations: the plug-in switch sends the choice and shows a refusal');
}

async function aTileClickOpensThePanelAndEscClosesItFocusingTheTile() {
  //given
  const t = await boot();
  await open(t);
  const {doc, w} = t;

  //when
  tile(doc, 'acme').querySelector('.int-health').click();
  const opened = {hidden: wrap(doc).hidden, parent: wrap(doc).parentElement.tagName, role: panel(doc).getAttribute('role'), modal: panel(doc).getAttribute('aria-modal'),
    title: doc.getElementById(panel(doc).getAttribute('aria-labelledby')).textContent, icon: inPanel(doc, '.int-ph img.ico').getAttribute('src'),
    tabs: [...panel(doc).querySelectorAll('.int-itab')].map((b) => [b.dataset.name, b.getAttribute('aria-selected')]), add: inPanel(doc, '.int-add').textContent, inside: panel(doc).contains(doc.activeElement)};
  const focusable = [...panel(doc).querySelectorAll('button, input, select, textarea')].filter((x) => !x.disabled && !x.closest('[hidden]'));
  focusable[focusable.length - 1].focus();
  key(w, doc.activeElement, 'Tab');
  const wrapped = doc.activeElement === focusable[0];
  key(w, doc.activeElement, 'Tab', true);
  const back = doc.activeElement === focusable[focusable.length - 1];
  key(w, doc.activeElement, 'Escape');
  const escaped = [wrap(doc).hidden, doc.activeElement === tile(doc, 'acme').querySelector('.int-open')];
  openTile(t, 'pager');
  inPanel(doc, '.int-close').click();
  const closed = [wrap(doc).hidden, doc.activeElement === tile(doc, 'pager').querySelector('.int-open')];
  openTile(t, 'acme');
  doc.querySelector('#int-wrap .int-scrim').click();

  //then
  assert.deepStrictEqual(opened, {hidden: false, parent: 'BODY', role: 'dialog', modal: 'true', title: 'Acme metrics', icon: '/plugins/acme/icon.svg', tabs: [['main', 'true'], ['east', 'false']], add: '+ Add instance', inside: true});
  assert.deepStrictEqual([wrapped, back], [true, true], 'focus stays inside the panel');
  assert.deepStrictEqual(escaped, [true, true], 'Esc closes it and focuses the tile');
  assert.deepStrictEqual(closed, [true, true], 'the close button does the same');
  assert.ok(wrap(doc).hidden, 'a click outside closes it');
  assert.ok(/Nothing to set up/.test((() => { openTile(t, 'pager'); return panel(doc).textContent; })()), 'a plug-in without fields has nothing to set up');
  t.w.close();
  console.log('PASS Admin, Integrations: a tile opens the side panel, focus stays inside, and Esc, close or a click outside returns focus to the tile');
}

async function theCatalogSearchesGroupsAndOpensWhatIsPicked() {
  //given
  const t = await boot();
  await open(t);
  const {doc, w} = t;
  const groups = () => [...doc.querySelectorAll('#int-pop .int-cat-grp')].filter((g) => !g.hidden).map((g) => [g.querySelector('.int-cat-h').textContent, [...g.querySelectorAll('.int-pick')].filter((b) => !b.hidden).map((b) => b.dataset.id)]);

  //when
  addBtn(doc).click();
  const search = doc.querySelector('#int-pop .int-search');
  const opened = {hidden: doc.getElementById('int-pop').hidden, expanded: addBtn(doc).getAttribute('aria-expanded'), focus: doc.activeElement === search, placeholder: search.placeholder, groups: groups()};
  const marks = {acme: [pick(doc, 'acme').classList.contains('has'), pick(doc, 'acme').textContent.includes('\u2713')], prometheus: pick(doc, 'prometheus').textContent.includes('\u2713'),
    gold: [pick(doc, 'gold').querySelector('.int-ed').textContent, pick(doc, 'gold').getAttribute('aria-disabled'), pick(doc, 'gold').title],
    relic: [pick(doc, 'relic').getAttribute('aria-disabled'), pick(doc, 'relic').title, !!pick(doc, 'relic').querySelector('.int-ed')], icon: pick(doc, 'acme').querySelector('img.ico').getAttribute('src')};
  pick(doc, 'gold').click();
  const goldPicked = [doc.getElementById('int-pop').hidden, wrap(doc).hidden];
  setValue(w, search, 'Prom');
  const narrowed = groups();
  setValue(w, search, 'zzz');
  const none = [groups(), !doc.querySelector('#int-pop .int-pop-none').hidden];
  setValue(w, search, '');
  key(w, search, 'Escape');
  const escaped = [doc.getElementById('int-pop').hidden, addBtn(doc).getAttribute('aria-expanded'), doc.activeElement === addBtn(doc)];
  addBtn(doc).click();
  pick(doc, 'prometheus').focus();
  key(w, doc.activeElement, 'Enter');
  const entered = {pop: doc.getElementById('int-pop').hidden, panel: wrap(doc).hidden, title: inPanel(doc, '.int-ph h2').textContent, name: doc.activeElement === inPanel(doc, '.int-name-in'),
    checks: checks(doc).map((c) => [c.dataset.check, c.checked])};
  key(w, doc.activeElement, 'Escape');
  const backTo = doc.activeElement === addBtn(doc);
  addBtn(doc).click();
  pick(doc, 'acme').click();
  const connected = [inPanel(doc, '.int-ph h2').textContent, inPanel(doc, '.int-itab[aria-selected="true"]').dataset.name];

  //then
  assert.deepStrictEqual(opened, {hidden: false, expanded: 'true', focus: true, placeholder: 'Search integrations\u2026',
    groups: [['Metrics', ['acme', 'prometheus']], ['Alerts', ['pager']], ['Infrastructure', ['relic']], ['Tickets', ['gold']], ['Other', ['evil']]]});
  assert.ok(!pick(doc, 'cracked') && !pick(doc, 'odd.module'), 'a broken plug-in is not listed');
  assert.deepStrictEqual(marks, {acme: [true, true], prometheus: false, gold: ['Enterprise', 'true', 'Gold board needs the Enterprise edition of Skopax'], relic: ['true', 'Relic needs core >=9 (this is 0.1.0)', false], icon: '/plugins/acme/icon.svg'});
  assert.deepStrictEqual(goldPicked, [false, true], 'a locked item opens nothing');
  assert.deepStrictEqual(narrowed, [['Metrics', ['prometheus']]], 'the search narrows and keeps the category group');
  assert.deepStrictEqual(none, [[], true]);
  assert.deepStrictEqual(escaped, [true, 'false', true], 'Esc closes the catalog and focuses the button');
  assert.deepStrictEqual(entered, {pop: true, panel: false, title: 'Prometheus', name: true, checks: [['pulse', true], ['beat', true]]}, 'Enter opens the setup of a new instance, watching every check');
  assert.ok(backTo, 'closing that panel focuses the Add button');
  assert.deepStrictEqual(connected, ['Acme metrics', 'main'], 'a connected plug-in opens on its first instance');
  t.w.close();
  console.log('PASS Admin, Integrations: the catalog searches, groups by category, marks what is connected or locked, and opens what is picked');
}

async function aPluginWithNothingToSetUpThatIsOffCanBeTurnedOnFromTheCatalog() {
  //given
  const cat = catalog();
  cat.plugins.push({id: 'quiet', name: 'Quiet', version: '1.0.0', category: 'other', description: '', edition: 'community', icon: false, state: 'ok', reason: null, enabled: false, fields: [], checks: [], instances: [], health: health('off', OFF)});
  const t = await boot({catalog: cat});
  await open(t);

  //when
  const tiled = !!tile(t.doc, 'quiet');
  addBtn(t.doc).click();
  pick(t.doc, 'quiet').click();
  const offered = [inPanel(t.doc, '.int-ph h2').textContent, /Nothing to set up/.test(panel(t.doc).textContent), inPanel(t.doc, '.int-turn-on').textContent];
  inPanel(t.doc, '.int-turn-on').click();
  await wait(100);

  //then
  assert.deepStrictEqual([tiled, offered], [false, ['Quiet', true, 'Turn on']]);
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'PUT'), [['PUT', '/api/admin/plugins/quiet', {enabled: true}]]);
  assert.ok(wrap(t.doc).hidden && t.doc.querySelector('#integrations .int-note').textContent === 'Quiet switched on', 'the panel closes and the page says so');
  assert.ok(t.doc.activeElement === addBtn(t.doc), 'focus goes back to the Add button');
  t.w.close();
  console.log('PASS Admin, Integrations: a plug-in with nothing to set up that is off is turned on from its panel');
}

async function cancelGoesBackToTheFirstInstanceOrClosesThePanel() {
  //given
  const t = await boot();
  await open(t);

  //when
  addInstance(t, 'acme');
  inPanel(t.doc, '.int-cancel').click();
  const back = [!wrap(t.doc).hidden, inPanel(t.doc, '.int-itab[aria-selected="true"]').dataset.name, !inPanel(t.doc, '.int-name-in'), inPanel(t.doc, '.int-add').getAttribute('aria-pressed')];
  inPanel(t.doc, '.int-close').click();
  addBtn(t.doc).click();
  pick(t.doc, 'prometheus').click();
  inPanel(t.doc, '.int-cancel').click();

  //then
  assert.deepStrictEqual(back, [true, 'main', true, 'false'], 'with instances, Cancel shows the first one');
  assert.ok(wrap(t.doc).hidden && t.doc.activeElement === addBtn(t.doc), 'without one, Cancel closes the panel and focuses the Add button');
  assert.deepStrictEqual(t.calls.filter((c) => c[0] !== 'GET'), [], 'and sends nothing');
  t.w.close();
  console.log('PASS Admin, Integrations: Cancel goes back to the first instance, or closes the panel when there is none');
}

async function theAddFormShowsTokenOnlyForBearerAuth() {
  //given
  const t = await boot();
  await open(t);

  //when
  addInstance(t, 'acme');
  const before = field(t.doc, 'token').hidden;
  setValue(t.w, field(t.doc, 'auth').querySelector('.int-in'), 'bearer');
  const bearer = field(t.doc, 'token').hidden;
  setValue(t.w, field(t.doc, 'auth').querySelector('.int-in'), 'basic');

  //then
  assert.deepStrictEqual([before, bearer, field(t.doc, 'token').hidden], [true, false, true]);
  assert.strictEqual(field(t.doc, 'token').querySelector('.int-in').type, 'password');
  assert.strictEqual(field(t.doc, 'token').querySelector('.int-in').placeholder, 'not set');
  assert.ok(field(t.doc, 'name') && field(t.doc, 'url') && field(t.doc, 'auth'), 'the name and every field are there');
  assert.deepStrictEqual([...panel(t.doc).querySelectorAll('.int-form button')].map((b) => b.textContent), ['Test connection', 'Save', 'Save & turn on', 'Cancel']);
  assert.ok(inPanel(t.doc, '.int-add').getAttribute('aria-pressed') === 'true' && !inPanel(t.doc, '.int-itab[aria-selected="true"]'), 'the Add instance tab is the one shown');
  t.w.close();
  console.log('PASS Admin, Integrations: the add form shows token only for auth=bearer');
}

async function anAddressIsReadOnlyWhenASourceOwnsTheInstance() {
  //given
  const t = await boot();
  await open(t);

  //when
  openInstance(t, 'acme', 'main');
  const owned = field(t.doc, 'url');
  const ownedUrl = [owned.querySelector('.int-in').readOnly, owned.querySelector('.int-in').value, owned.textContent.includes('from config file')];
  itab(t.doc, 'east').click();
  const mine = field(t.doc, 'url').querySelector('.int-in');

  //then
  assert.deepStrictEqual(ownedUrl, [true, 'https://main.example.com', true]);
  assert.deepStrictEqual([mine.readOnly, mine.value], [false, 'https://east.example.com']);
  t.w.close();
  console.log('PASS Admin, Integrations: the address of an instance a source owns is read-only');
}

async function aSecretShowsWhereItComesFromAndIsReadOnlyWhenASourceOwnsIt() {
  //given
  const t = await boot();
  await open(t);

  //when
  openInstance(t, 'acme', 'main');
  const owned = field(t.doc, 'token').querySelector('.int-in');
  const ownedHint = [owned.placeholder, owned.readOnly, owned.value, !!field(t.doc, 'token').querySelector('.int-clear')];
  itab(t.doc, 'east').click();
  const mine = field(t.doc, 'token').querySelector('.int-in');

  //then
  assert.deepStrictEqual(ownedHint, ['set \u00b7 from config file', true, '', false]);
  assert.deepStrictEqual([mine.placeholder, mine.readOnly, mine.value], ['set', false, '']);
  assert.ok(!t.doc.body.innerHTML.includes('tok-'), 'no secret value is in the page');
  t.w.close();
  console.log('PASS Admin, Integrations: a secret reads "set \u00b7 from config file", is blank, and is read-only when a source owns it');
}

async function theInstanceShowsItsRoleSourceHealthAndProblem() {
  //given
  const cat = catalog();
  cat.plugins[0].instances[1].problem = 'east has a value that is not allowed';
  const t = await boot({catalog: cat});
  await open(t);

  //when
  openInstance(t, 'acme', 'main');
  const main = [!!inPanel(t.doc, '.int-primary'), inPanel(t.doc, '.int-src').textContent, inPanel(t.doc, '.int-on').checked, inPanel(t.doc, '.int-ihealth').textContent, !inPanel(t.doc, '.int-del')];
  itab(t.doc, 'east').click();
  const east = [!!inPanel(t.doc, '.int-primary'), inPanel(t.doc, '.int-src').textContent, inPanel(t.doc, '.int-on').checked, inPanel(t.doc, '.int-problem').textContent, !!inPanel(t.doc, '.int-del')];

  //then
  assert.deepStrictEqual(main, [true, 'config file', true, 'Healthy ' + DOT + ' 2 min ago', true], 'a source instance has no Delete');
  assert.deepStrictEqual(east, [false, 'admin', true, 'east has a value that is not allowed', true]);
  t.w.close();
  console.log('PASS Admin, Integrations: an instance shows its role, source, switch, health and problem, and only an instance made here can be deleted');
}

async function aRoutePluginShowsNoPrimaryControls() {
  //given
  const cat = catalog();
  cat.plugins[0].checks = [];
  cat.plugins[0].tools = [];
  const t = await boot({catalog: cat});
  await open(t);

  //when
  openInstance(t, 'acme', 'main');
  const main = [!!inPanel(t.doc, '.int-primary'), !!inPanel(t.doc, '.int-primary-btn')];
  itab(t.doc, 'east').click();
  const east = [!!inPanel(t.doc, '.int-primary'), !!inPanel(t.doc, '.int-primary-btn')];

  //then
  assert.deepStrictEqual([main, east], [[false, false], [false, false]], 'every route posts, so a plug-in with no checks and no tools has no primary');
  t.w.close();
  console.log('PASS Admin, Integrations: a plug-in with no checks and no tools, such as a Teams route, shows no primary tag and no Make primary');
}

async function testConnectionShowsTheSummaryAndKeepsAnUntouchedSecretNull() {
  //given
  const t = await boot();
  await open(t);
  openInstance(t, 'acme', 'east');

  //when
  inPanel(t.doc, '.int-test').click();
  await wait(80);

  //then
  assert.deepStrictEqual(t.calls.filter((c) => c[1].endsWith('/test')), [['POST', '/api/admin/plugins/acme/test', {name: 'east', values: {url: 'https://east.example.com', auth: 'bearer', token: null}}]]);
  assert.strictEqual(inPanel(t.doc, '.int-result').textContent, '\u2713 answered \u00b7 12 ms');
  assert.ok(inPanel(t.doc, '.int-result').classList.contains('ok') && !inPanel(t.doc, '.int-result').hidden);
  t.w.close();
  console.log('PASS Admin, Integrations: Test connection shows the summary and sends null for an untouched secret');
}

async function aFailedTestShowsTheSummaryAsAProblem() {
  //given
  const t = await boot();
  await open(t);
  t.flags.hook = async (method, p, body, r) => p.endsWith('/test') ? r.ok({ok: false, summary: 'refused', ms: 2.6}) : null;
  addInstance(t, 'acme');

  //when
  inPanel(t.doc, '.int-test').click();
  await wait(80);

  //then
  const box = inPanel(t.doc, '.int-result');
  assert.strictEqual(box.textContent, 'refused \u00b7 3 ms', 'no tick on a failure, and whole milliseconds');
  assert.ok(box.classList.contains('bad') && !box.classList.contains('ok'));
  assert.deepStrictEqual(t.calls.filter((c) => c[1].endsWith('/test'))[0][2], {values: {url: '', auth: 'basic'}});
  t.w.close();
  console.log('PASS Admin, Integrations: a failed test shows its summary as a problem and an unsaved form sends no name');
}

async function saveAndTurnOnSendsAPostWithEnabledTrue() {
  //given
  const t = await boot();
  await open(t);
  addInstance(t, 'acme');
  inPanel(t.doc, '.int-name-in').value = 'backup';
  setValue(t.w, field(t.doc, 'url').querySelector('.int-in'), 'https://backup.example.com');
  setValue(t.w, field(t.doc, 'auth').querySelector('.int-in'), 'bearer');
  setValue(t.w, field(t.doc, 'token').querySelector('.int-in'), 'sekret');

  //when
  inPanel(t.doc, '.int-save-on').click();
  await wait(100);

  //then
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'POST'), [['POST', '/api/admin/plugins/acme/instances', {name: 'backup', values: {url: 'https://backup.example.com', auth: 'bearer', token: 'sekret'}, enabled: true}]]);
  assert.strictEqual(t.doc.querySelector('#integrations .int-note').textContent, 'backup added and turned on');
  assert.ok(wrap(t.doc).hidden && !panel(t.doc).querySelector('.int-form'), 'the panel closes');
  assert.ok(t.doc.activeElement === tile(t.doc, 'acme').querySelector('.int-open'), 'and focus goes back to the tile');
  assert.ok(t.calls.filter((c) => c[0] === 'GET' && c[1] === '/api/admin/plugins').length >= 2, 'the list reloads');
  t.w.close();
  console.log('PASS Admin, Integrations: Save & turn on sends POST with enabled true and reloads the list');
}

async function plainSaveSendsEnabledFalse() {
  //given
  const t = await boot();
  await open(t);
  addInstance(t, 'acme');
  inPanel(t.doc, '.int-name-in').value = 'backup';
  setValue(t.w, field(t.doc, 'url').querySelector('.int-in'), 'https://backup.example.com');

  //when
  inPanel(t.doc, '.int-save').click();
  await wait(100);

  //then
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'POST').map((c) => c[2].enabled), [false]);
  assert.ok(!('token' in t.calls.filter((c) => c[0] === 'POST')[0][2].values), 'a hidden or untouched secret is not sent on create');
  assert.ok(!('offChecks' in t.calls.filter((c) => c[0] === 'POST')[0][2]), 'watching every check sends no offChecks');
  t.w.close();
  console.log('PASS Admin, Integrations: Save adds the instance switched off');
}

async function aDuplicateNameShowsOnTheForm() {
  //given
  const t = await boot();
  await open(t);
  t.flags.hook = async (method, p, body, r) => method === 'POST' ? r.bad(409, 'An instance named backup already exists for this plug-in') : null;
  addInstance(t, 'acme');
  inPanel(t.doc, '.int-name-in').value = 'backup';

  //when
  inPanel(t.doc, '.int-save').click();
  await wait(100);

  //then
  assert.strictEqual(inPanel(t.doc, '.int-form-msg').textContent, 'An instance named backup already exists for this plug-in');
  assert.ok(inPanel(t.doc, '.int-form-msg').classList.contains('bad'));
  assert.ok(!wrap(t.doc).hidden && inPanel(t.doc, '.int-form'), 'the form stays open to fix it');
  assert.ok([...panel(t.doc).querySelectorAll('.int-form button')].every((b) => !b.disabled), 'and its buttons work again');
  t.w.close();
  console.log('PASS Admin, Integrations: a 409 shows on the form, which stays open');
}

async function aFieldRefusalLandsOnItsOwnField() {
  //given
  const t = await boot();
  await open(t);
  t.flags.hook = async (method, p, body, r) => method === 'POST' && p.endsWith('/instances') ? r.bad(422, 'URL must be an http or https address', 'url') : null;
  addInstance(t, 'acme');
  inPanel(t.doc, '.int-name-in').value = 'backup';

  //when
  inPanel(t.doc, '.int-save').click();
  await wait(100);

  //then
  assert.strictEqual(field(t.doc, 'url').querySelector('.int-field-msg').textContent, 'URL must be an http or https address');
  assert.ok(field(t.doc, 'url').querySelector('.int-field-msg').classList.contains('bad'));
  assert.strictEqual(field(t.doc, 'auth').querySelector('.int-field-msg').textContent, '');
  assert.strictEqual(inPanel(t.doc, '.int-form-msg').textContent, '');
  t.w.close();
  console.log('PASS Admin, Integrations: a 422 with a field names that field');
}

async function editingSendsOnlyChangesAndAnExplicitClearSendsAnEmptySecret() {
  //given
  const t = await boot();
  await open(t);
  openInstance(t, 'acme', 'east');
  setValue(t.w, field(t.doc, 'url').querySelector('.int-in'), 'https://east2.example.com');

  //when
  inPanel(t.doc, '.int-save').click();
  await wait(100);
  const note = t.doc.querySelector('#integrations .int-note').textContent;
  openInstance(t, 'acme', 'east');
  field(t.doc, 'token').querySelector('.int-clear').click();
  inPanel(t.doc, '.int-save').click();
  await wait(100);
  openInstance(t, 'acme', 'east');
  inPanel(t.doc, '.int-save').click();
  await wait(60);

  //then
  const patches = t.calls.filter((c) => c[0] === 'PATCH').map((c) => c[2]);
  assert.deepStrictEqual(patches, [{values: {url: 'https://east2.example.com', token: null}}, {values: {token: ''}}]);
  assert.strictEqual(note, 'east saved');
  assert.strictEqual(inPanel(t.doc, '.int-form-msg').textContent, 'Nothing changed');
  t.w.close();
  console.log('PASS Admin, Integrations: an edit sends only the changes, null for an untouched secret and "" only after Clear');
}

async function whatItWatchesSendsOffChecksAndAChecksOnlySaveIsAChange() {
  //given
  const cat = catalog();
  cat.plugins[0].instances[0].offChecks = ['beat'];
  const t = await boot({catalog: cat});
  await open(t);

  //when
  openInstance(t, 'acme', 'main');
  const mainBoxes = checks(t.doc).map((c) => [c.dataset.check, c.checked, c.closest('label').textContent]);
  checks(t.doc)[1].click();
  inPanel(t.doc, '.int-save').click();
  await wait(100);
  openInstance(t, 'acme', 'east');
  const eastBoxes = checks(t.doc).map((c) => c.checked);
  checks(t.doc)[0].click();
  inPanel(t.doc, '.int-save').click();
  await wait(100);
  const note = t.doc.querySelector('#integrations .int-note').textContent;
  addInstance(t, 'acme');
  inPanel(t.doc, '.int-name-in').value = 'backup';
  setValue(t.w, field(t.doc, 'url').querySelector('.int-in'), 'https://backup.example.com');
  checks(t.doc)[1].click();
  inPanel(t.doc, '.int-save').click();
  await wait(100);

  //then
  assert.deepStrictEqual(mainBoxes, [['pulse', true, 'Pulse'], ['beat', false, 'Beat']], 'one box per check, unchecked for what it does not watch');
  assert.deepStrictEqual(eastBoxes, [true, true]);
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'PATCH').map((c) => [c[1], c[2]]), [['/api/admin/plugins/acme/instances/main', {offChecks: []}], ['/api/admin/plugins/acme/instances/east', {offChecks: ['pulse']}]]);
  assert.strictEqual(note, 'east saved', 'a save that changes only what it watches is a change');
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'POST').map((c) => c[2]), [{name: 'backup', values: {url: 'https://backup.example.com'}, enabled: false, offChecks: ['beat']}]);
  t.w.close();
  console.log('PASS Admin, Integrations: what it watches has one box per check, unchecking sends PATCH offChecks, and a new instance can start without a check');
}

async function aOnceCheckShowsItsBoxOnlyOnThePrimary() {
  //given
  const once = {id: 'nodes', label: 'Nodes', period: 900, offset: 0, ran: ['nodes'], once: true};
  const cat = catalog();
  cat.plugins.filter((p) => p.id === 'acme' || p.id === 'prometheus').forEach((p) => { p.checks = CHECKS.concat([once]); });
  cat.plugins[0].instances[1].offChecks = ['nodes'];
  const t = await boot({catalog: cat});
  await open(t);

  //when
  openInstance(t, 'acme', 'main');
  const mainBoxes = checks(t.doc).map((c) => c.dataset.check);
  openInstance(t, 'acme', 'east');
  const eastBoxes = checks(t.doc).map((c) => c.dataset.check);
  inPanel(t.doc, '.int-save').click();
  await wait(100);
  const unchanged = inPanel(t.doc, '.int-form-msg').textContent;
  checks(t.doc)[0].click();
  inPanel(t.doc, '.int-save').click();
  await wait(100);
  addInstance(t, 'acme');
  const addBoxes = checks(t.doc).map((c) => c.dataset.check);
  addBtn(t.doc).click();
  pick(t.doc, 'prometheus').click();
  const firstBoxes = checks(t.doc).map((c) => c.dataset.check);

  //then
  assert.deepStrictEqual(mainBoxes, ['pulse', 'beat', 'nodes'], 'the primary shows the box of a check that runs once per plug-in');
  assert.deepStrictEqual(eastBoxes, ['pulse', 'beat'], 'another instance does not');
  assert.strictEqual(unchanged, 'Nothing changed', 'a hidden stored choice is not a change');
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'PATCH').map((c) => [c[1], c[2]]), [['/api/admin/plugins/acme/instances/east', {offChecks: ['pulse']}]]);
  assert.deepStrictEqual(addBoxes, ['pulse', 'beat'], 'a new instance beside others is not the primary');
  assert.deepStrictEqual(firstBoxes, ['pulse', 'beat', 'nodes'], 'the first instance of a plug-in becomes its primary');
  t.w.close();
  console.log('PASS Admin, Integrations: the box of a once-per-plug-in check shows only on the primary instance');
}

async function aConflictOnAChecksChangeReloadsTheRow() {
  //given
  const t = await boot();
  await open(t);
  const stored = {off: []};
  t.flags.catalog = () => { const c = catalog(); c.plugins[0].instances[1].offChecks = stored.off.slice(); return c; };
  t.flags.hook = async (method, p, body, r) => {
    if (method !== 'PATCH') return null;
    stored.off = body.offChecks;
    return r.bad(409, 'east is saved. The change to what it watches finishes within 15 seconds.');
  };
  openInstance(t, 'acme', 'east');

  //when
  checks(t.doc)[0].click();
  inPanel(t.doc, '.int-save').click();
  await wait(120);
  const saved = {gets: t.calls.filter((c) => c[0] === 'GET' && c[1] === '/api/admin/plugins').length, open: !wrap(t.doc).hidden, on: inPanel(t.doc, '.int-itab[aria-selected="true"]').dataset.name,
    boxes: checks(t.doc).map((c) => c.checked), note: inPanel(t.doc, '.int-panel-note').textContent, text: panel(t.doc).textContent};
  t.flags.hook = async (method, p, body, r) => method === 'PATCH' ? r.bad(409, 'URL comes from config file and cannot be changed here') : null;
  checks(t.doc)[1].click();
  inPanel(t.doc, '.int-save').click();
  await wait(120);

  //then
  assert.deepStrictEqual([saved.gets, saved.open, saved.on, saved.boxes], [2, true, 'east', [false, true]], 'the row is read again and the panel shows what was stored');
  assert.strictEqual(saved.note, 'east is saved. The change to what it watches finishes within 15 seconds.', 'the server says what was saved');
  assert.ok(!/Not saved/.test(saved.text), 'never "not saved"');
  assert.strictEqual(inPanel(t.doc, '.int-form-msg').textContent, 'URL comes from config file and cannot be changed here', 'a 409 that saved nothing shows its reason');
  assert.ok(inPanel(t.doc, '.int-form-msg').classList.contains('bad'));
  t.w.close();
  console.log('PASS Admin, Integrations: after a 409 on a checks change the panel reads the row again, and shows the reason only when nothing was saved');
}

async function saveAndTurnOnWhoseChecksChangeConflictsSaysItStaysOff() {
  //given
  const t = await boot();
  const stored = {off: []};
  const off = () => { const c = catalog(); c.plugins[0].instances[1].enabled = false; c.plugins[0].instances[1].offChecks = stored.off.slice(); return c; };
  t.flags.catalog = off;
  await open(t);
  const said = 'east is saved but stays off: turn it on again. The change to what it watches finishes within 15 seconds.';
  t.flags.hook = async (method, p, body, r) => {
    if (method !== 'PATCH') return null;
    stored.off = body.offChecks;
    return r.bad(409, said);
  };
  openInstance(t, 'acme', 'east');

  //when
  checks(t.doc)[0].click();
  inPanel(t.doc, '.int-save-on').click();
  await wait(120);

  //then
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'PATCH').map((c) => c[2]), [{offChecks: ['pulse'], enabled: true}]);
  assert.deepStrictEqual([!wrap(t.doc).hidden, inPanel(t.doc, '.int-panel-note').textContent, inPanel(t.doc, '.int-on').checked, !!inPanel(t.doc, '.int-save-on'), checks(t.doc).map((c) => c.checked)],
    [true, said, false, true, [false, true]], 'the note says it stays off, and the row read again offers Save & turn on');
  t.w.close();
  console.log('PASS Admin, Integrations: Save & turn on whose checks change conflicts says the instance is saved but stays off');
}

async function theConnectedCountIsOneDefinitionForTheSubNavAndTheSubtitle() {
  //given
  const cat = catalog();
  cat.plugins = [cat.plugins[0], cat.plugins[1],
    {id: 'ent', name: 'Enterprise board', version: '1.0.0', category: 'other', description: '', edition: 'enterprise', icon: false, state: 'locked', reason: 'Enterprise board needs the Enterprise edition of Skopax', enabled: true, fields: [], checks: [], instances: [], health: health('off', OFF)},
    {id: 'parked', name: 'Parked', version: '1.0.0', category: 'logs', description: '', edition: 'community', icon: false, state: 'ok', reason: null, enabled: true, fields: FIELDS, checks: CHECKS,
      instances: [instance('main', {problem: 'main has a value that is not allowed'})], health: health('error', 'main has a value that is not allowed')},
    {id: 'idle', name: 'Idle', version: '1.0.0', category: 'logs', description: '', edition: 'community', icon: false, state: 'ok', reason: null, enabled: true, fields: FIELDS, checks: CHECKS,
      instances: [instance('main', {enabled: false})], health: health('off', OFF)},
    {id: 'bare', name: 'Bare', version: '1.0.0', category: 'logs', description: '', edition: 'community', icon: false, state: 'ok', reason: null, enabled: true, fields: FIELDS, checks: CHECKS, instances: [], health: health('off', 'Not set up yet')}];
  const t = await boot({catalog: cat});

  //when
  await open(t);
  addBtn(t.doc).click();
  const ticks = picks(t.doc).filter((b) => b.classList.contains('has')).map((b) => b.dataset.id);

  //then
  assert.deepStrictEqual(tiles(t.doc).map((x) => x.dataset.id), ['acme', 'pager', 'ent', 'parked', 'idle'], 'the Enterprise plug-in stored on keeps its tile, to be switched off');
  assert.strictEqual(t.doc.querySelector('#integrations .int-sub').textContent, '2 connected ' + DOT + ' 3 need attention');
  assert.strictEqual(t.doc.getElementById('count-integrations').textContent, '2', 'the sub-nav and the page agree');
  assert.deepStrictEqual(ticks, ['acme', 'pager'], 'only what is connected has a tick');
  t.w.close();
  console.log('PASS Admin, Integrations: connected is on, can run in this edition, and serves an instance or needs none, for the sub-nav and the page alike');
}

async function escAndFocusStayWithThePanelWhateverHasFocus() {
  //given
  const t = await boot();
  await open(t);
  const {doc, w} = t;
  const esc = (target) => key(w, target, 'Escape');

  //when
  openInstance(t, 'acme', 'east');
  doc.activeElement.blur();
  const blurred = doc.activeElement === doc.body;
  esc(doc.body);
  const bodyEsc = [wrap(doc).hidden, doc.activeElement === tile(doc, 'acme').querySelector('.int-open')];
  openInstance(t, 'acme', 'east');
  doc.activeElement.blur();
  key(w, doc.body, 'Tab', true);
  const shiftTab = panel(doc).contains(doc.activeElement) && doc.activeElement !== panel(doc);
  inPanel(doc, '.int-test').focus();
  inPanel(doc, '.int-test').click();
  const during = doc.activeElement === panel(doc);
  await wait(80);
  const afterTest = doc.activeElement === inPanel(doc, '.int-test');
  t.flags.hook = async (method, p, body, r) => method === 'PATCH' ? r.bad(409, 'URL comes from config file and cannot be changed here') : null;
  checks(doc)[0].click();
  inPanel(doc, '.int-save').focus();
  inPanel(doc, '.int-save').click();
  await wait(120);
  const afterRefusal = [doc.activeElement === inPanel(doc, '.int-save'), inPanel(doc, '.int-form-msg').textContent];
  inPanel(doc, '.int-del').click();
  await wait(20);
  esc(doc.activeElement);
  const confirmEsc = [doc.getElementById('cf-wrap').hidden, wrap(doc).hidden];
  panel(doc).focus();
  esc(doc.activeElement);

  //then
  assert.ok(blurred);
  assert.deepStrictEqual(bodyEsc, [true, true], 'Esc closes the panel even with focus on the page body');
  assert.ok(shiftTab, 'Shift+Tab from outside lands inside the panel');
  assert.ok(during, 'while Test connection runs, focus waits on the panel, not on the body');
  assert.ok(afterTest, 'and goes back to the button');
  assert.deepStrictEqual(afterRefusal, [true, 'URL comes from config file and cannot be changed here'], 'a refused Save gives focus back to Save');
  assert.deepStrictEqual(confirmEsc, [true, false], 'Esc in the confirm dialog closes only the dialog');
  assert.strictEqual(panel(doc).getAttribute('tabindex'), '-1', 'a click on panel text keeps focus in the panel');
  assert.ok(wrap(doc).hidden, 'Esc with focus on the panel itself closes it');
  t.w.close();
  console.log('PASS Admin, Integrations: Esc and focus stay with the panel after Test connection, a refused Save, a click on its text, or focus on the body');
}

async function aRefusedCheckShowsByWhatItWatchesAsText() {
  //given
  const t = await boot();
  await open(t);
  t.flags.hook = async (method, p, body, r) => method === 'PATCH' ? r.bad(422, EVIL + ' is not a check of this plug-in', 'offChecks') : null;
  openInstance(t, 'acme', 'east');

  //when
  checks(t.doc)[0].click();
  inPanel(t.doc, '.int-save').click();
  await wait(100);

  //then
  assert.strictEqual(inPanel(t.doc, '.int-checks .int-field-msg').textContent, EVIL + ' is not a check of this plug-in');
  assert.ok(inPanel(t.doc, '.int-checks .int-field-msg').classList.contains('bad'));
  assert.strictEqual(inPanel(t.doc, '.int-form-msg').textContent, '');
  assert.strictEqual(t.doc.querySelectorAll('img[onerror]').length, 0);
  t.w.close();
  console.log('PASS Admin, Integrations: a refused check id shows by What it watches, as text');
}

async function instanceControlsSwitchPromoteAndDeleteWithConfirmation() {
  //given
  const t = await boot();
  await open(t);
  t.flags.hook = async (method, p, body, r) => method === 'PATCH' && body.primary ? r.ok({instance: instance('east', {primary: true}), notice: 'east is now the primary Acme metrics instance.'}) : null;
  openInstance(t, 'acme', 'east');

  //when
  inPanel(t.doc, '.int-on').click();
  await wait(80);
  const kept = [!wrap(t.doc).hidden, inPanel(t.doc, '.int-itab[aria-selected="true"]').dataset.name, inPanel(t.doc, '.int-panel-note').textContent];
  inPanel(t.doc, '.int-primary-btn').click();
  await wait(80);
  const note = inPanel(t.doc, '.int-panel-note').textContent;
  inPanel(t.doc, '.int-del').click();
  await wait(20);
  const cf = t.doc.getElementById('cf-wrap');
  const asked = [cf.hidden, t.doc.getElementById('cf-title').textContent, t.doc.getElementById('cf-yes').textContent];
  t.doc.getElementById('cf-no').click();
  await wait(20);
  const early = [t.calls.filter((c) => c[0] === 'DELETE').length, !wrap(t.doc).hidden];
  inPanel(t.doc, '.int-del').click();
  await wait(20);
  t.doc.getElementById('cf-yes').click();
  await wait(100);

  //then
  assert.deepStrictEqual(t.calls.filter((c) => c[0] !== 'GET').map((c) => [c[0], c[1], c[2]]), [['PATCH', '/api/admin/plugins/acme/instances/east', {enabled: false}], ['PATCH', '/api/admin/plugins/acme/instances/east', {primary: true}], ['DELETE', '/api/admin/plugins/acme/instances/east', null]]);
  assert.deepStrictEqual(kept, [true, 'east', 'east switched off'], 'the panel stays on the instance');
  assert.strictEqual(note, 'east is now the primary Acme metrics instance.');
  assert.deepStrictEqual(asked, [false, 'Delete east?', 'Delete']);
  assert.deepStrictEqual(early, [0, true], 'Cancel sends nothing and keeps the panel');
  assert.ok(wrap(t.doc).hidden && t.doc.querySelector('#integrations .int-note').textContent === 'east deleted');
  t.w.close();
  console.log('PASS Admin, Integrations: switch, Make primary (with its notice) and Delete behind the confirm dialog call the right routes');
}

async function droppedPackTextsShowOneWarningLinePerLanguageOnTheCardAndInThePanel() {
  //given
  const long = 'k'.repeat(80);
  const drops = [{lang: 'de', count: 9, text: '9 de texts show in English', keys: ["page['A']", EVIL, long, 'rules[2]', "chat['still']"], more: 4},
    {lang: EVIL, count: 1, text: '1 ' + EVIL + ' texts show in English', keys: ['writing'], more: 0}];
  const t = await boot({catalog: (() => { const c = catalog(); c.plugins[0].localeDrops = drops; c.plugins[1].localeDrops = []; return c; })()});

  //when
  await open(t);
  const lines = [...tile(t.doc, 'acme').querySelectorAll('.int-drop')].map((p) => p.textContent);
  const quiet = tile(t.doc, 'pager').querySelectorAll('.int-drop').length;
  openTile(t, 'acme');
  const inside = [...panel(t.doc).querySelectorAll('.int-drop')].map((p) => p.textContent);

  //then
  const cut = 'k'.repeat(59) + '\u2026';
  assert.deepStrictEqual(lines, ['9 de texts show in English: page[\'A\'], ' + EVIL + ', ' + cut + ', rules[2], chat[\'still\'] +4 more', '1 ' + EVIL + ' texts show in English: writing'],
    'one line per language: the text, the first five keys cut to 60 characters, and how many more');
  assert.deepStrictEqual(inside, lines, 'the side panel lists them too, for a plug-in reached from the catalog');
  assert.strictEqual(quiet, 0, 'a plug-in with nothing dropped shows no line');
  assert.strictEqual(t.doc.querySelectorAll('img[onerror]').length, 0, 'a hostile key or language is text, never markup');
  assert.ok([...tile(t.doc, 'acme').querySelectorAll('.int-drop')].every((p) => p.tagName === 'P' && !p.hidden && p.getAttribute('aria-hidden') !== 'true'), 'plain text a screen reader reads');
  assert.ok(/\.int-drop\s*\{[^}]*color:\s*var\(--warn\)/.test(TEMPLATE_CSS()), 'the warning colour comes from the theme token, so it follows light and dark');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Admin, Integrations: dropped pack texts show one escaped warning line per language on the card and in the panel');
}

async function everyValueIsEscaped() {
  //given
  const t = await boot();
  await open(t);

  //when
  const evil = tile(t.doc, 'evil');
  openTile(t, 'evil');
  const inside = [inPanel(t.doc, '.int-ph h2').textContent, inPanel(t.doc, '.int-desc').textContent, inPanel(t.doc, '.int-problem').textContent, field(t.doc, 'url').querySelector('.int-in').value,
    t.doc.querySelectorAll('img[onerror]').length];
  inPanel(t.doc, '.int-close').click();
  addBtn(t.doc).click();

  //then
  assert.strictEqual(evil.querySelector('.int-name').textContent, EVIL);
  assert.strictEqual(evil.querySelector('.int-mono').textContent, '<');
  assert.deepStrictEqual(inside, [EVIL, EVIL, EVIL, EVIL, 0], 'the panel title, description, problem and field value are text');
  assert.strictEqual(pick(t.doc, 'evil').querySelector('.int-pick-n').textContent, EVIL);
  assert.strictEqual(t.doc.querySelectorAll('img[onerror]').length, 0, 'nothing became markup');
  assert.ok([...t.doc.querySelectorAll('#integrations img, #int-panel img')].every((i) => i.getAttribute('src').startsWith('/plugins/')), 'every image is a plug-in icon');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Admin, Integrations: names, descriptions, problems and field values are escaped on tiles, in the panel and in the catalog');
}

async function instanceNamesSourcesFieldsAndNotesAreEscapedToo() {
  //given
  const fields = [
    {key: 'url', label: EVIL, kind: 'url', required: true, default: '', help: EVIL, choices: [], low: null, high: null, showIf: null, ui: true},
    {key: 'mode', label: 'Mode', kind: 'choice', required: false, default: '', help: '', choices: [EVIL, 'plain'], low: null, high: null, showIf: null, ui: false},
    {key: 'token', label: 'Token', kind: 'secret', required: false, default: '', help: '', choices: [], low: null, high: null, showIf: null, ui: false},
  ];
  const cat = {edition: 'community', secretsKey: 'generated', plugins: [
    {id: 'lean', name: 'Lean', version: '1.0.0', category: EVIL, description: '', edition: 'community', icon: false, state: 'ok', reason: null, enabled: true, fields, checks: [{id: 'tick', label: EVIL}],
      instances: [instance(EVIL, {source: EVIL, owned: ['url', 'token'], values: {url: EVIL, mode: EVIL}, secrets: {token: {set: true, from: EVIL}}}), instance('main')],
      health: health('error', EVIL, iso(1), [{name: EVIL, state: 'error', text: EVIL, at: iso(1)}])},
    {id: 'gold', name: 'Gold', version: '1.0.0', category: 'other', description: '', edition: 'enterprise', icon: false, state: 'locked', reason: EVIL, enabled: true, fields: [], instances: []},
    {id: 'odd.module', module: 'odd.module', reason: EVIL}]};
  const t = await boot({catalog: cat});
  await open(t);

  //when
  openTile(t, 'lean');
  const tab = [...panel(t.doc).querySelectorAll('.int-itab')][0];
  const text = panel(t.doc).textContent;
  inPanel(t.doc, '.int-close').click();
  addBtn(t.doc).click();

  //then
  assert.strictEqual(tab.dataset.name, EVIL);
  assert.strictEqual(tab.textContent, EVIL);
  assert.ok(text.includes(EVIL), 'the hostile text shows as text in the panel');
  assert.strictEqual(tile(t.doc, 'lean').querySelector('.int-cat').textContent, EVIL, 'a category the page does not know shows as its id');
  assert.strictEqual(tile(t.doc, 'lean').querySelector('.int-health').textContent, EVIL);
  assert.strictEqual(tile(t.doc, 'gold').querySelector('.int-switch').title, EVIL);
  assert.ok(t.doc.getElementById('integrations').textContent.includes(EVIL), 'and on the tiles and in the catalog');
  assert.strictEqual(t.doc.querySelectorAll('#integrations img, #int-panel img').length, 0, 'nothing became markup');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Admin, Integrations: instance names, sources, reasons, labels, help, choices, checks and categories are escaped too');
}

async function dottedFieldKeysEditAndCarryTheirRefusal() {
  //given
  const fields = [
    {key: 'url', label: 'URL', kind: 'url', required: true, default: '', help: '', choices: [], low: null, high: null, showIf: null, ui: false},
    {key: 'echo.url', label: 'Echo URL', kind: 'url', required: false, default: '', help: '', choices: [], low: null, high: null, showIf: null, ui: false},
    {key: 'echo.token', label: 'Echo token', kind: 'secret', required: false, default: '', help: '', choices: [], low: null, high: null, showIf: null, ui: false},
  ];
  const cat = {edition: 'community', secretsKey: 'env', plugins: [{id: 'kubernetes', name: 'Kubernetes', version: '1.0.0', category: 'infrastructure', description: '', edition: 'community', icon: false, state: 'ok', reason: null, enabled: true, fields,
    instances: [instance('east', {values: {url: 'https://east.example.com', 'echo.url': 'https://echo.example.com'}, secrets: {'echo.token': {set: true, from: null}}})]}]};
  const t = await boot({catalog: cat});
  await open(t);
  t.flags.hook = async (method, p, body, r) => p.endsWith('/test') ? r.bad(422, 'Re-enter Echo token to test a new address', 'echo.token') : null;
  openInstance(t, 'kubernetes', 'east');
  setValue(t.w, field(t.doc, 'echo.url').querySelector('.int-in'), 'https://echo-two.example.com');

  //when
  inPanel(t.doc, '.int-test').click();
  await wait(100);
  const refused = field(t.doc, 'echo.token').querySelector('.int-field-msg').textContent;
  inPanel(t.doc, '.int-save').click();
  await wait(100);

  //then
  const tested = t.calls.filter((c) => c[1].endsWith('/test')).map((c) => c[2]);
  assert.deepStrictEqual(tested, [{name: 'east', values: {url: 'https://east.example.com', 'echo.url': 'https://echo-two.example.com', 'echo.token': null}}]);
  assert.strictEqual(refused, 'Re-enter Echo token to test a new address');
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'PATCH').map((c) => c[2]), [{values: {'echo.url': 'https://echo-two.example.com', 'echo.token': null}}]);
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Admin, Integrations: dotted field keys edit, test and carry their own refusal');
}

async function aSwitchedOffInstanceOffersNoMakePrimary() {
  //given
  const cat = catalog();
  cat.plugins[0].instances = [instance('main', {primary: true}), instance('east', {enabled: false}), instance('backup')];
  const t = await boot({catalog: cat});

  //when
  await open(t);
  openInstance(t, 'acme', 'east');
  const east = [!!inPanel(t.doc, '.int-primary-btn'), !!inPanel(t.doc, '.int-save-on')];
  itab(t.doc, 'backup').click();
  const backup = [!!inPanel(t.doc, '.int-primary-btn'), !!inPanel(t.doc, '.int-save-on')];

  //then
  assert.deepStrictEqual(east, [false, true], 'no Make primary on a switched-off instance, which offers Save & turn on');
  assert.deepStrictEqual(backup, [true, false], 'Make primary on a switched-on one');
  t.w.close();
  console.log('PASS Admin, Integrations: a switched-off instance offers no Make primary');
}

async function aNonAdminSeesNoIntegrationsTab() {
  //given
  const t = await boot({admin: false});

  //then
  assert.ok(t.doc.getElementById('tab-integrations').hidden, 'no Integrations tab for a non-admin');
  assert.strictEqual(t.calls.filter((c) => c[1] === '/api/admin/plugins').length, 0);
  t.w.close();
  console.log('PASS Admin, Integrations: hidden for a non-admin');
}

async function anUntouchedSelectIsNeverSent() {
  //given
  const fields = [FIELDS[0],
    {key: 'auth', label: 'Authentication', kind: 'choice', required: false, default: 'basic', help: '', choices: ['bearer', 'basic'], low: null, high: null, showIf: null, ui: false},
    {key: 'insecure', label: 'Skip verification', kind: 'bool', required: false, default: '', help: '', choices: [], low: null, high: null, showIf: null, ui: false}];
  const cat = {edition: 'community', secretsKey: 'env', plugins: [{id: 'lean', name: 'Lean', version: '1.0.0', category: 'other', description: '', edition: 'community', icon: false, state: 'ok', reason: null, enabled: true, fields,
    instances: [instance('main', {values: {url: 'https://main.example.com'}, secrets: {}})]}]};
  const t = await boot({catalog: cat});
  await open(t);

  //when
  openInstance(t, 'lean', 'main');
  const shownValues = [field(t.doc, 'auth').querySelector('.int-in').value, field(t.doc, 'insecure').querySelector('.int-in').value];
  setValue(t.w, field(t.doc, 'url').querySelector('.int-in'), 'https://env2.example.com');
  inPanel(t.doc, '.int-save').click();
  await wait(100);
  addInstance(t, 'lean');
  inPanel(t.doc, '.int-name-in').value = 'backup';
  setValue(t.w, field(t.doc, 'url').querySelector('.int-in'), 'https://backup.example.com');
  inPanel(t.doc, '.int-save').click();
  await wait(100);

  //then
  assert.deepStrictEqual(shownValues, ['basic', '']);
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'PATCH').map((c) => c[2]), [{values: {url: 'https://env2.example.com'}}]);
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'POST').map((c) => c[2]), [{name: 'backup', values: {url: 'https://backup.example.com'}, enabled: false}]);
  t.w.close();
  console.log('PASS Admin, Integrations: an untouched select (default or blank) is never sent, on edit or add');
}

async function aChoiceWhoseDefaultIsEmptyCanBeSetBackToEmpty() {
  //given
  const fields = [FIELDS[0], {key: 'min-sev', label: 'Lowest severity', kind: 'choice', required: false, default: '', help: 'Empty uses the plug-in setting', choices: ['info', 'warn', 'crit'], low: null, high: null, showIf: null, ui: false}];
  const cat = {edition: 'community', secretsKey: 'env', plugins: [{id: 'lean', name: 'Lean', version: '1.0.0', category: 'chat', description: '', edition: 'community', icon: false, state: 'ok', reason: null, enabled: true, fields,
    instances: [instance('main', {values: {url: 'https://main.example.com', 'min-sev': 'crit'}, secrets: {}})]}]};
  const t = await boot({catalog: cat});
  await open(t);

  //when
  openInstance(t, 'lean', 'main');
  const select = field(t.doc, 'min-sev').querySelector('.int-in');
  const options = [...select.options].map((o) => o.value);
  const shown = select.value;
  setValue(t.w, select, '');
  inPanel(t.doc, '.int-save').click();
  await wait(100);

  //then
  assert.deepStrictEqual([options, shown], [['', 'info', 'warn', 'crit'], 'crit'], 'a choice that may stay empty keeps its empty option after a value is saved');
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'PATCH').map((c) => c[2]), [{values: {'min-sev': ''}}]);
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Admin, Integrations: a choice whose default is empty keeps its empty option, so a saved value can be cleared again');
}

async function anInstanceFieldPicksAnInstanceOfTheUsedPlugin() {
  //given
  const fields = [{key: 'source', label: 'Source', kind: 'instance', required: true, default: '', help: '', choices: ['acme'], low: null, high: null, showIf: null, ui: false}];
  const cat = catalog();
  cat.plugins.push({id: 'reader', name: 'Reader', version: '1.0.0', category: 'metrics', description: '', edition: 'community', icon: false, state: 'ok', reason: null, enabled: true, fields, checks: [],
    uses: ['acme'], maxInstances: null, instances: [instance('main', {primary: true, values: {source: 'gone'}, secrets: {}})]});
  const t = await boot({catalog: cat});
  await open(t);

  //when
  openInstance(t, 'reader', 'main');
  const input = field(t.doc, 'source').querySelector('.int-in');
  const kept = [...input.querySelectorAll('option')].map((o) => o.value);
  const shown = input.value;
  addInstance(t, 'reader');
  const offered = [...field(t.doc, 'source').querySelectorAll('.int-in option')].map((o) => o.value);
  inPanel(t.doc, '.int-name-in').value = 'second';
  setValue(t.w, field(t.doc, 'source').querySelector('.int-in'), 'east');
  inPanel(t.doc, '.int-save').click();
  await wait(100);

  //then
  assert.strictEqual(input.tagName, 'SELECT');
  assert.deepStrictEqual(kept, ['gone', 'main', 'east']);
  assert.strictEqual(shown, 'gone');
  assert.deepStrictEqual(offered, ['', 'main', 'east']);
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'POST').map((c) => c[2]), [{name: 'second', values: {source: 'east'}, enabled: false}]);
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Admin, Integrations: an instance field picks an instance of the plug-in it reads through, keeping a stored name that is gone');
}

async function aRefusalOnAHiddenFieldStillShowsAMessage() {
  //given
  const t = await boot();
  await open(t);
  t.flags.hook = async (method, p, body, r) => p.endsWith('/test') ? r.bad(422, 'Re-enter Token to test a new address', 'token') : null;
  openInstance(t, 'acme', 'east');
  setValue(t.w, field(t.doc, 'auth').querySelector('.int-in'), 'basic');
  setValue(t.w, field(t.doc, 'url').querySelector('.int-in'), 'https://other.example.com');

  //when
  inPanel(t.doc, '.int-test').click();
  await wait(80);

  //then
  assert.strictEqual(field(t.doc, 'token').hidden, true);
  assert.strictEqual(inPanel(t.doc, '.int-form-msg').textContent, 'Re-enter Token to test a new address');
  assert.ok(inPanel(t.doc, '.int-form-msg').classList.contains('bad'));
  assert.strictEqual(field(t.doc, 'token').querySelector('.int-field-msg').textContent, '');
  t.w.close();
  console.log('PASS Admin, Integrations: a refusal that names a hidden field shows on the form');
}

async function leavingTheTabOrTheRoleClosesThePanelAndTheCatalog() {
  //given
  const t = await boot();
  await open(t);
  openTile(t, 'acme');

  //when
  t.doc.getElementById('tab-settings').click();
  await wait(80);
  const left = wrap(t.doc).hidden;
  await open(t);
  addBtn(t.doc).click();
  const stays = !t.doc.getElementById('int-pop').hidden;
  t.doc.body.click();
  const outside = t.doc.getElementById('int-pop').hidden;
  openTile(t, 'acme');
  t.w.setAdminAccess(false);
  await wait(40);

  //then
  assert.ok(left, 'another admin section closes the panel');
  assert.deepStrictEqual([stays, outside], [true, true], 'a click outside closes the catalog');
  assert.ok(wrap(t.doc).hidden && !panel(t.doc).textContent, 'losing the role closes the panel and empties it');
  t.w.close();
  console.log('PASS Admin, Integrations: leaving the section or the role closes the panel, and a click outside closes the catalog');
}

async function theSidePanelIsAViewportOverlayAndFullScreenOnAPhone() {
  //given
  const css = require('fs').readFileSync(TEMPLATE, 'utf8');

  //then
  assert.ok(/\.int-wrap \{[^}]*position: fixed;[^}]*inset: 0;/.test(css), 'the wrap covers the viewport');
  assert.ok(/\.int-panel \{[^}]*width: 340px;/.test(css), '340 px wide');
  assert.ok(/@media \(max-width: 768px\) \{[^@]*\.int-panel \{ width: 100%;/.test(css), 'full screen at 768 px and below');
  assert.ok(/<div class="int-wrap" id="int-wrap" hidden>/.test(css) && css.indexOf('id="int-wrap"') > css.indexOf('</main>'), 'outside the content, at body level');
  console.log('PASS Admin, Integrations: the side panel is a fixed body-level overlay, 340 px wide, full screen on a phone');
}

async function aTileSwitchKeepsTheKeyboardFocusOnItsSwitch() {
  //given
  const t = await boot();
  await open(t);
  const {doc} = t;
  const sw = (id) => tile(doc, id).querySelector('.int-switch');

  //when
  sw('acme').focus();
  sw('acme').click();
  await wait(80);
  const first = [doc.activeElement === sw('acme'), doc.querySelector('#integrations .int-note').textContent];
  sw('acme').click();
  await wait(80);
  const second = [doc.activeElement === sw('acme'), t.calls.filter((c) => c[0] === 'PUT').length];
  doc.activeElement.blur();
  sw('pager').click();
  await wait(80);
  const unfocused = doc.activeElement === doc.body;
  let release;
  t.flags.hook = async (method) => method === 'PUT' ? new Promise((r) => { release = r; }).then(() => null) : null;
  sw('acme').focus();
  sw('acme').click();
  await wait(20);
  doc.getElementById('search-btn').focus();
  t.flags.hook = null;
  release();
  await wait(80);
  const movedOn = doc.activeElement === doc.getElementById('search-btn');

  //then
  assert.deepStrictEqual(first, [true, 'Acme metrics switched off'], 'after a toggle and the redraw, the focus is on the same tile switch, not on the page body');
  assert.deepStrictEqual(second, [true, 2], 'so a second Space toggles it again');
  assert.ok(unfocused, 'a switch that did not hold the focus does not take it');
  assert.ok(movedOn, 'focus moved elsewhere while the save was on its way stays there; only focus that fell to the body goes back to the switch');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Admin, Integrations: a tile switch keeps the keyboard focus after its toggle and the redraw');
}

async function theSearchOwnsTabWhileItIsOpenOverTheSidePanel() {
  //given
  const t = await boot();
  await open(t);
  const {doc, w} = t;
  const press = (el, k, extra) => el.dispatchEvent(new w.KeyboardEvent('keydown', Object.assign({key: k, bubbles: true, cancelable: true}, extra || {})));
  openTile(t, 'acme');
  const from = doc.activeElement;

  //when
  press(from, 'k', {ctrlKey: true});
  const q = doc.getElementById('pal-q');
  const opened = [!doc.getElementById('pal').hidden, doc.activeElement === q];
  press(q, 'Tab');
  const tab = doc.activeElement === q;
  press(q, 'Tab', {shiftKey: true});
  const shiftTab = doc.activeElement === q;
  press(q, 'Escape');
  const closed = [doc.getElementById('pal').hidden, wrap(doc).hidden, doc.activeElement === from];

  //then
  assert.ok(panel(doc).contains(from), 'the side panel had the focus');
  assert.deepStrictEqual(opened, [true, true], 'Ctrl+K opens the search over the side panel');
  assert.deepStrictEqual([tab, shiftTab], [true, true], 'Tab and Shift+Tab stay in the search input; the side panel trap below does not pull the focus away');
  assert.deepStrictEqual(closed, [true, false, true], 'Esc closes only the search and gives the focus back inside the panel');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Admin, Integrations: the search owns Tab while it is open over the side panel, and Esc closes only the search');
}

async function testConnectionSendsTheTicksOnlyWhenTheFormChangedThem() {
  //given
  const t = await boot();
  await open(t);
  openInstance(t, 'acme', 'main');

  //when
  inPanel(t.doc, '.int-test').click();
  await wait(80);
  checks(t.doc)[1].click();
  inPanel(t.doc, '.int-test').click();
  await wait(80);
  addInstance(t, 'acme');
  inPanel(t.doc, '.int-test').click();
  await wait(80);
  checks(t.doc)[0].click();
  inPanel(t.doc, '.int-test').click();
  await wait(80);

  //then
  const sent = t.calls.filter((c) => c[1].endsWith('/test')).map((c) => c[2].offChecks);
  assert.deepStrictEqual(sent, [undefined, ['beat'], undefined, ['pulse']], 'a test sends the ticks only when they differ from the saved ones, or from all on for a new instance');
  t.w.close();
  console.log('PASS Admin, Integrations: Test connection sends the unsaved ticks of what it watches, and nothing when they match the saved ones');
}

async function aRefusedPluginShowsNoAccessAndNeedsAttention() {
  //given
  const one = catalog();
  one.plugins = [one.plugins[0], Object.assign({}, one.plugins[6], {instances: [instance('main', {primary: true})],
    health: health('warn', 'No access', iso(3), [{name: 'main', state: 'warn', text: 'No access', at: iso(3)}])})];
  const t = await boot({catalog: one});
  await open(t);

  //then
  const prom = tile(t.doc, 'prometheus');
  assert.ok(prom.querySelector('.int-dot.warn'), 'an amber dot, not a green one');
  assert.ok(prom.querySelector('.int-health').textContent.startsWith('No access'));
  assert.strictEqual(t.doc.querySelector('#integrations .int-sub').textContent, '2 connected ' + DOT + ' 1 needs attention');
  const css = require('fs').readFileSync(TEMPLATE, 'utf8');
  assert.ok(/\.int-dot\.warn\s*\{[^}]*var\(--warn\)/.test(css), 'the dot takes the theme warn colour in both themes');
  t.w.close();
  console.log('PASS Admin, Integrations: a refused plug-in shows No access with an amber dot and needs attention');
}

async function thePinSwitchShowsOnlyOnceACertificateIsPasted() {
  //given
  const one = catalog();
  one.plugins[0] = Object.assign({}, one.plugins[0], {fields: FIELDS.concat([
    {key: 'ca', label: 'CA certificate', kind: 'pem', required: false, default: '', help: '', choices: [], low: null, high: null, showIf: null, ui: false},
    {key: 'ca-pinned', label: 'Trust this certificate as it is', kind: 'bool', required: false, default: 'false', help: 'skips the hostname and expiry checks', choices: [], low: null, high: null, showIf: ['ca', '*'], ui: false}])});
  const t = await boot({catalog: one});
  await open(t);

  //when
  addInstance(t, 'acme');
  const before = field(t.doc, 'ca-pinned').hidden;
  setValue(t.w, field(t.doc, 'ca').querySelector('.int-in'), '-----BEGIN CERTIFICATE-----');
  const pasted = field(t.doc, 'ca-pinned').hidden;
  setValue(t.w, field(t.doc, 'ca').querySelector('.int-in'), '');

  //then
  assert.deepStrictEqual([before, pasted, field(t.doc, 'ca-pinned').hidden], [true, false, true]);
  t.w.close();
  console.log('PASS Admin, Integrations: the certificate pin switch shows only once a certificate is pasted');
}

async function anInstanceWithoutACheckReadsNoChecksGreyAndNeverConnected() {
  //given
  const one = catalog();
  const neutral = {name: 'main', state: 'none', text: 'No checks', at: null};
  one.plugins = [Object.assign({}, one.plugins[0], {id: 'gitlab', name: 'GitLab', category: 'code', checks: [], instances: [instance('main', {primary: true})], health: health('none', 'No checks', null, [neutral])})];
  const t = await boot({catalog: one});

  //when
  await open(t);
  const onTile = [tile(t.doc, 'gitlab').querySelector('.int-health').textContent, tile(t.doc, 'gitlab').querySelector('.int-dot').className];
  const sub = t.doc.querySelector('#integrations .int-sub').textContent;
  openInstance(t, 'gitlab', 'main');
  await wait(60);
  const inDrawer = [inPanel(t.doc, '.int-ihealth').textContent, inPanel(t.doc, '.int-ihealth .int-dot').className];

  //then
  assert.deepStrictEqual(onTile, ['No checks', 'int-dot off'], 'the tile reads the neutral state with a grey dot');
  assert.deepStrictEqual(inDrawer, ['No checks', 'int-dot off'], 'the instance reads the same');
  assert.ok(/ 0 need attention$/.test(sub), sub);
  assert.ok(!t.doc.getElementById('integrations').textContent.includes('Connected'), 'nothing says Connected without a check behind it');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Admin, Integrations: an instance without a check reads No checks with a grey dot, as on the board, never Connected');
}

async function noImageIsRenderedWithoutASource() {
  //given
  const t = await boot();
  const empty = () => [...t.doc.querySelectorAll('img')].filter((i) => !String(i.getAttribute('src') || '').trim()).map((i) => i.id || i.className || i.outerHTML);

  //when
  await open(t);
  const page = empty();
  openInstance(t, 'acme', 'main');
  await wait(60);
  const drawer = empty();
  addBtn(t.doc).click();
  await wait(60);
  const picker = empty();

  //then
  assert.deepStrictEqual([page, drawer, picker], [[], [], []], 'an img without a src makes the browser request the page itself');
  t.w.close();
  console.log('PASS Admin, Integrations: no img is rendered without a source, on the page, in the side panel or in the catalog');
}

(async () => {
  await theTilesShowEveryConnectedPluginWithItsStateHealthAndIcon();
  await anInstanceWithoutACheckReadsNoChecksGreyAndNeverConnected();
  await noImageIsRenderedWithoutASource();
  await theSubtitleCountsAndTheChipsFilterTheTiles();
  await aRefusedPluginShowsNoAccessAndNeedsAttention();
  await aLockedSwitchIsDisabledAndShowsTheReason();
  await aStoredOnPluginThatCannotRunCanStillBeSwitchedOff();
  await theSwitchSendsTheChoiceAndPutsItBackOnARefusal();
  await aTileSwitchKeepsTheKeyboardFocusOnItsSwitch();
  await theSearchOwnsTabWhileItIsOpenOverTheSidePanel();
  await aTileClickOpensThePanelAndEscClosesItFocusingTheTile();
  await theCatalogSearchesGroupsAndOpensWhatIsPicked();
  await aPluginWithNothingToSetUpThatIsOffCanBeTurnedOnFromTheCatalog();
  await cancelGoesBackToTheFirstInstanceOrClosesThePanel();
  await theAddFormShowsTokenOnlyForBearerAuth();
  await thePinSwitchShowsOnlyOnceACertificateIsPasted();
  await aSecretShowsWhereItComesFromAndIsReadOnlyWhenASourceOwnsIt();
  await anAddressIsReadOnlyWhenASourceOwnsTheInstance();
  await theInstanceShowsItsRoleSourceHealthAndProblem();
  await aRoutePluginShowsNoPrimaryControls();
  await testConnectionShowsTheSummaryAndKeepsAnUntouchedSecretNull();
  await aFailedTestShowsTheSummaryAsAProblem();
  await saveAndTurnOnSendsAPostWithEnabledTrue();
  await plainSaveSendsEnabledFalse();
  await aFieldRefusalLandsOnItsOwnField();
  await aDuplicateNameShowsOnTheForm();
  await editingSendsOnlyChangesAndAnExplicitClearSendsAnEmptySecret();
  await whatItWatchesSendsOffChecksAndAChecksOnlySaveIsAChange();
  await aOnceCheckShowsItsBoxOnlyOnThePrimary();
  await aConflictOnAChecksChangeReloadsTheRow();
  await saveAndTurnOnWhoseChecksChangeConflictsSaysItStaysOff();
  await aRefusedCheckShowsByWhatItWatchesAsText();
  await theConnectedCountIsOneDefinitionForTheSubNavAndTheSubtitle();
  await escAndFocusStayWithThePanelWhateverHasFocus();
  await instanceControlsSwitchPromoteAndDeleteWithConfirmation();
  await droppedPackTextsShowOneWarningLinePerLanguageOnTheCardAndInThePanel();
  await everyValueIsEscaped();
  await instanceNamesSourcesFieldsAndNotesAreEscapedToo();
  await anUntouchedSelectIsNeverSent();
  await aChoiceWhoseDefaultIsEmptyCanBeSetBackToEmpty();
  await anInstanceFieldPicksAnInstanceOfTheUsedPlugin();
  await aRefusalOnAHiddenFieldStillShowsAMessage();
  await dottedFieldKeysEditAndCarryTheirRefusal();
  await aSwitchedOffInstanceOffersNoMakePrimary();
  await leavingTheTabOrTheRoleClosesThePanelAndTheCatalog();
  await theSidePanelIsAViewportOverlayAndFullScreenOnAPhone();
  await aNonAdminSeesNoIntegrationsTab();
  await testConnectionSendsTheTicksOnlyWhenTheFormChangedThem();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
