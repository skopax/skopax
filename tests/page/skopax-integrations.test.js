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
  assert.strictEqual(acme.querySelector('.int-health').textContent, '2 instances ' + DOT + ' Healthy ' + DOT + ' 2 min ago');
  assert.strictEqual(acme.title, 'Healthy ' + DOT + ' 2 min ago', 'the full health text is in the tooltip');
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
  console.log('PASS Integrations: tiles show the connected plug-ins with icon, category, switch, health and off styling; notices stay in the strip');
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
  console.log('PASS Integrations: the subtitle counts what is connected and what needs attention, and the chips filter the tiles');
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
  assert.ok(!gold.querySelector('.int-reason') && gold.title.split('\n').indexOf('Gold board needs the Enterprise edition of Skopax') >= 0, 'the tile keeps the reason in its tooltip');
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'PUT'), [], 'an off switch that cannot be used sends nothing');
  assert.ok(!wrap(t.doc).hidden && inPanel(t.doc, '.int-reason').textContent === 'Gold board needs the Enterprise edition of Skopax', 'the panel says why');
  assert.ok(!inPanel(t.doc, '.int-add') && !inPanel(t.doc, '.int-form'), 'no instance can be added to a locked plug-in');
  t.w.close();
  console.log('PASS Integrations: a locked plug-in that is off has a disabled switch and shows its reason');
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
  console.log('PASS Integrations: a broken, locked or incompatible plug-in that is stored on can always be switched off');
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
  console.log('PASS Integrations: the plug-in switch sends the choice and shows a refusal');
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
  console.log('PASS Integrations: a tile opens the side panel, focus stays inside, and Esc, close or a click outside returns focus to the tile');
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
  assert.deepStrictEqual(opened, {hidden: false, expanded: 'true', focus: true, placeholder: 'Search integrations',
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
  console.log('PASS Integrations: the catalog searches, groups by category, marks what is connected or locked, and opens what is picked');
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
  console.log('PASS Integrations: a plug-in with nothing to set up that is off is turned on from its panel');
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
  console.log('PASS Integrations: Cancel goes back to the first instance, or closes the panel when there is none');
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
  assert.deepStrictEqual([...panel(t.doc).querySelectorAll('.int-form .int-foot button')].map((b) => b.textContent), ['Test connection', 'Cancel', 'Save', 'Save & turn on']);
  assert.deepStrictEqual([...panel(t.doc).querySelectorAll('.int-form button')].filter((b) => !b.closest('.int-foot')).map((b) => b.className), ['int-seg-b', 'int-seg-b', 'int-eye'], 'the only other buttons are the field controls');
  assert.ok(inPanel(t.doc, '.int-add').getAttribute('aria-pressed') === 'true' && !inPanel(t.doc, '.int-itab[aria-selected="true"]'), 'the Add instance tab is the one shown');
  t.w.close();
  console.log('PASS Integrations: the add form shows token only for auth=bearer');
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
  console.log('PASS Integrations: the address of an instance a source owns is read-only');
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
  console.log('PASS Integrations: a secret reads "set \u00b7 from config file", is blank, and is read-only when a source owns it');
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
  console.log('PASS Integrations: an instance shows its role, source, switch, health and problem, and only an instance made here can be deleted');
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
  console.log('PASS Integrations: a plug-in with no checks and no tools, such as a Teams route, shows no primary tag and no Make primary');
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
  console.log('PASS Integrations: Test connection shows the summary and sends null for an untouched secret');
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
  console.log('PASS Integrations: a failed test shows its summary as a problem and an unsaved form sends no name');
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
  console.log('PASS Integrations: Save & turn on sends POST with enabled true and reloads the list');
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
  console.log('PASS Integrations: Save adds the instance switched off');
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
  console.log('PASS Integrations: a 409 shows on the form, which stays open');
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
  console.log('PASS Integrations: a 422 with a field names that field');
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
  console.log('PASS Integrations: an edit sends only the changes, null for an untouched secret and "" only after Clear');
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
  console.log('PASS Integrations: what it watches has one box per check, unchecking sends PATCH offChecks, and a new instance can start without a check');
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
  console.log('PASS Integrations: the box of a once-per-plug-in check shows only on the primary instance');
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
  console.log('PASS Integrations: after a 409 on a checks change the panel reads the row again, and shows the reason only when nothing was saved');
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
  console.log('PASS Integrations: Save & turn on whose checks change conflicts says the instance is saved but stays off');
}

async function aPluginSetUpOutsideAdminIsNeitherConnectedNorNeedingAttention() {
  //given
  const cat = catalog();
  cat.plugins = [cat.plugins[0], cat.plugins[1],
    {id: 'outside', name: 'Outside source', version: '1.0.0', category: 'infrastructure', description: 'Settings read from a server.', edition: 'community', icon: false, state: 'ok', reason: null, enabled: true,
      fields: [], checks: [], panels: [], instances: [], health: health('none', 'No checks')}];
  const t = await boot({catalog: cat});

  //when
  await open(t);
  addBtn(t.doc).click();
  const row = pick(t.doc, 'outside');

  //then
  assert.deepStrictEqual(tiles(t.doc).map((x) => x.dataset.id), ['acme', 'pager'], 'no tile among the connected ones');
  assert.strictEqual(t.doc.querySelector('#integrations .int-sub').textContent, '2 connected ' + DOT + ' 1 needs attention');
  assert.strictEqual(t.doc.getElementById('count-integrations').textContent, '2');
  assert.strictEqual(row.querySelector('.int-has').textContent, 'Set up outside Admin');
  assert.ok(!row.classList.contains('has') && row.querySelector('.int-has').classList.contains('idle'), 'neutral: no tick and not connected');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Integrations: a plug-in with no fields, checks or panels reads Set up outside Admin and counts as neither connected nor needing attention');
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
  console.log('PASS Integrations: connected is on, can run in this edition, and serves an instance or needs none, for the sub-nav and the page alike');
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
  console.log('PASS Integrations: Esc and focus stay with the panel after Test connection, a refused Save, a click on its text, or focus on the body');
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
  console.log('PASS Integrations: a refused check id shows by What it watches, as text');
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
  console.log('PASS Integrations: switch, Make primary (with its notice) and Delete behind the confirm dialog call the right routes');
}

async function droppedPackTextsShowOneWarningLinePerLanguageOnTheCardAndInThePanel() {
  //given
  const long = 'k'.repeat(80);
  const drops = [{lang: 'de', count: 9, text: '9 de texts show in English', keys: ["page['A']", EVIL, long, 'rules[2]', "chat['still']"], more: 4},
    {lang: EVIL, count: 1, text: '1 ' + EVIL + ' texts show in English', keys: ['writing'], more: 0}];
  const t = await boot({catalog: (() => { const c = catalog(); c.plugins[0].localeDrops = drops; c.plugins[1].localeDrops = []; return c; })()});

  //when
  await open(t);
  const tipLines = (id) => tile(t.doc, id).title.split('\n').filter((l) => l.indexOf('texts show in English') >= 0);
  const lines = tipLines('acme');
  const quiet = tipLines('pager').length;
  const onTile = tile(t.doc, 'acme').querySelectorAll('.int-drop').length;
  openTile(t, 'acme');
  const inside = [...panel(t.doc).querySelectorAll('.int-drop')].map((p) => p.textContent);

  //then
  const cut = 'k'.repeat(59) + '\u2026';
  assert.deepStrictEqual(lines, ['9 de texts show in English: page[\'A\'], ' + EVIL + ', ' + cut + ', rules[2], chat[\'still\'] +4 more', '1 ' + EVIL + ' texts show in English: writing'],
    'one line per language: the text, the first five keys cut to 60 characters, and how many more');
  assert.deepStrictEqual(inside, lines, 'the side panel lists them too, for a plug-in reached from the catalog');
  assert.strictEqual(quiet, 0, 'a plug-in with nothing dropped shows no line');
  assert.strictEqual(onTile, 0, 'the compact tile keeps the lines in its tooltip');
  assert.strictEqual(t.doc.querySelectorAll('img[onerror]').length, 0, 'a hostile key or language is text, never markup');
  assert.ok([...panel(t.doc).querySelectorAll('.int-drop')].length === 2 && [...panel(t.doc).querySelectorAll('.int-drop')].every((p) => p.tagName === 'P' && !p.hidden && p.getAttribute('aria-hidden') !== 'true'), 'plain text a screen reader reads');
  assert.ok(/\.int-drop\s*\{[^}]*color:\s*var\(--warn\)/.test(TEMPLATE_CSS()), 'the warning colour comes from the theme token, so it follows light and dark');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Integrations: dropped pack texts show one escaped warning line per language on the card and in the panel');
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
  console.log('PASS Integrations: names, descriptions, problems and field values are escaped on tiles, in the panel and in the catalog');
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
    {id: 'calm', name: 'Calm', version: '1.0.0', category: EVIL, description: '', edition: 'community', icon: false, state: 'ok', reason: null, enabled: true, fields, checks: [], instances: [instance('main')], health: health('ok', 'Healthy', iso(1), [{name: 'main', state: 'ok', text: 'Healthy', at: iso(1)}])},
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
  assert.strictEqual(tile(t.doc, 'lean').dataset.cat, EVIL, 'a category the page does not know is kept as its id');
  assert.strictEqual(tile(t.doc, 'calm').querySelector('.int-cat').textContent, EVIL, 'a healthy tile shows the unknown category as text');
  assert.strictEqual(tile(t.doc, 'calm').querySelector('.int-cat').children.length, 0, 'and never as markup');
  assert.strictEqual(tile(t.doc, 'lean').querySelector('.int-health').textContent, '2 instances ' + DOT + ' 1 needs attention');
  assert.strictEqual(tile(t.doc, 'lean').title, EVIL, 'the hostile health text is the tooltip, as text');
  assert.strictEqual(tile(t.doc, 'gold').querySelector('.int-switch').title, EVIL);
  assert.ok(t.doc.getElementById('integrations').textContent.includes(EVIL), 'and on the tiles and in the catalog');
  assert.strictEqual(t.doc.querySelectorAll('#integrations img, #int-panel img').length, 0, 'nothing became markup');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Integrations: instance names, sources, reasons, labels, help, choices, checks and categories are escaped too');
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
  console.log('PASS Integrations: dotted field keys edit, test and carry their own refusal');
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
  console.log('PASS Integrations: a switched-off instance offers no Make primary');
}

async function aNonAdminSeesNoIntegrationsTab() {
  //given
  const t = await boot({admin: false});

  //then
  assert.ok(!t.doc.getElementById('tab-integrations').hidden, 'a signed-in non-admin has the Connect Integrations row');
  assert.strictEqual(t.calls.filter((c) => c[1] === '/api/admin/plugins').length, 0, 'and never reads the admin catalog');
  t.w.close();
  console.log('PASS Integrations: a non-admin has the row but never reads the admin catalog');
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
  console.log('PASS Integrations: an untouched select (default or blank) is never sent, on edit or add');
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
  console.log('PASS Integrations: a choice whose default is empty keeps its empty option, so a saved value can be cleared again');
}

async function aRouteEchoChoiceShowsExactlyThreeOptionsAndStartsOnTheDefault() {
  //given
  const fields = [FIELDS[0], {key: 'slack-echo', label: 'Also post to Slack', kind: 'choice', required: false, default: '', help: '', choices: ['default', 'on', 'off'], choiceLabels: {default: 'Use the default', on: 'On', off: 'Off'}, low: null, high: null, showIf: null, ui: false}];
  const cat = {edition: 'community', secretsKey: 'env', plugins: [{id: 'lean', name: 'Lean', version: '1.0.0', category: 'chat', description: '', edition: 'community', icon: false, state: 'ok', reason: null, enabled: true, fields,
    instances: [instance('main', {values: {url: 'https://main.example.com'}, secrets: {}})]}]};
  const t = await boot({catalog: cat});
  await open(t);

  //when
  openInstance(t, 'lean', 'main');
  const buttons = [...field(t.doc, 'slack-echo').querySelectorAll('.int-seg-b')];
  inPanel(t.doc, '.int-save').click();
  await wait(100);

  //then
  assert.deepStrictEqual(buttons.map((b) => b.textContent), ['Use the default', 'On', 'Off']);
  assert.deepStrictEqual(buttons.filter((b) => b.getAttribute('aria-checked') === 'true').map((b) => b.textContent), ['Use the default']);
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'PATCH').map((c) => c[2]), [], 'an untouched choice is not sent');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Integrations: a route echo choice shows Use the default, On and Off, starts on the default and sends nothing untouched');
}

async function anotherPluginsChoiceWithADefaultOptionKeepsItsBlankOption() {
  //given
  const fields = [FIELDS[0], {key: 'mode', label: 'Mode', kind: 'choice', required: false, default: '', help: '', choices: ['default', 'fast'], choiceLabels: {}, low: null, high: null, showIf: null, ui: false}];
  const cat = {edition: 'community', secretsKey: 'env', plugins: [{id: 'lean', name: 'Lean', version: '1.0.0', category: 'chat', description: '', edition: 'community', icon: false, state: 'ok', reason: null, enabled: true, fields,
    instances: [instance('main', {values: {url: 'https://main.example.com'}, secrets: {}})]}]};
  const t = await boot({catalog: cat});
  await open(t);

  //when
  openInstance(t, 'lean', 'main');
  const buttons = [...field(t.doc, 'mode').querySelectorAll('.int-seg-b')].map((b) => b.dataset.value);

  //then
  assert.deepStrictEqual(buttons, ['', 'default', 'fast']);
  t.w.close();
  console.log('PASS Integrations: only the route echo choice drops its blank option; another plug-in keeps it');
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
  console.log('PASS Integrations: an instance field picks an instance of the plug-in it reads through, keeping a stored name that is gone');
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
  console.log('PASS Integrations: a refusal that names a hidden field shows on the form');
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
  console.log('PASS Integrations: leaving the section or the role closes the panel, and a click outside closes the catalog');
}

async function theSidePanelIsAViewportOverlayAndFullScreenOnAPhone() {
  //given
  const css = require('fs').readFileSync(TEMPLATE, 'utf8');

  //then
  assert.ok(/\.int-wrap \{[^}]*position: fixed;[^}]*inset: 0;/.test(css), 'the wrap covers the viewport');
  assert.ok(/\.int-panel \{[^}]*width: 420px;/.test(css), '420 px wide');
  assert.ok(/@media \(max-width: 768px\) \{[^@]*\.int-panel \{ width: 100%;/.test(css), 'full screen at 768 px and below');
  assert.ok(/<div class="int-wrap" id="int-wrap" hidden>/.test(css) && css.indexOf('id="int-wrap"') > css.indexOf('</main>'), 'outside the content, at body level');
  console.log('PASS Integrations: the side panel is a fixed body-level overlay, 420 px wide, full screen on a phone');
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
  console.log('PASS Integrations: a tile switch keeps the keyboard focus after its toggle and the redraw');
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
  console.log('PASS Integrations: the search owns Tab while it is open over the side panel, and Esc closes only the search');
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
  console.log('PASS Integrations: Test connection sends the unsaved ticks of what it watches, and nothing when they match the saved ones');
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
  console.log('PASS Integrations: a refused plug-in shows No access with an amber dot and needs attention');
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
  console.log('PASS Integrations: the certificate pin switch shows only once a certificate is pasted');
}

async function anInstanceWithoutACheckShowsNoHealthTextAndNeverConnected() {
  //given
  const one = catalog();
  const neutral = {name: 'main', state: 'none', text: 'No checks', at: null};
  one.plugins = [Object.assign({}, one.plugins[0], {id: 'gitlab', name: 'GitLab', category: 'code', checks: [], instances: [instance('main', {primary: true})], health: health('none', 'No checks', null, [neutral])})];
  const t = await boot({catalog: one});

  //when
  await open(t);
  const onTile = [tile(t.doc, 'gitlab').querySelector('.int-health'), tile(t.doc, 'gitlab').querySelector('.int-dot').className, tile(t.doc, 'gitlab').textContent.includes('No checks')];
  const sub = t.doc.querySelector('#integrations .int-sub').textContent;
  openInstance(t, 'gitlab', 'main');
  await wait(60);
  const inDrawer = [inPanel(t.doc, '.int-ihealth'), panel(t.doc).textContent.includes('No checks'), panel(t.doc).textContent.includes('Not checked yet')];

  //then
  assert.deepStrictEqual(onTile, [null, 'int-dot off', false], 'the tile says nothing about checks and keeps a grey dot');
  assert.deepStrictEqual(inDrawer, [null, false, false], 'the instance says nothing about checks either');
  assert.ok(/ 0 need attention$/.test(sub), sub);
  assert.ok(!t.doc.getElementById('integrations').textContent.includes('Connected'), 'nothing says Connected without a check behind it');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Integrations: an instance without a check shows no health text, only a grey dot, and never Connected');
}

async function twoInstancesWithoutChecksShowTheCountOnlyAndNotCheckedYetOnlyWhereNoHealthEntryExists() {
  //given
  const cat = catalog();
  const neutral = {name: 'main', state: 'none', text: 'No checks', at: null};
  cat.plugins = [Object.assign({}, cat.plugins[0], {id: 'gitlab', name: 'GitLab', category: 'code', checks: [], instances: [instance('main', {primary: true}), instance('east')], health: health('none', 'No checks', null, [neutral])})];
  const t = await boot({catalog: cat});

  //when
  await open(t);
  const tl = tile(t.doc, 'gitlab');
  openInstance(t, 'gitlab', 'main');
  await wait(60);
  const cards = [...panel(t.doc).querySelectorAll('.int-ilist .int-icard')];

  //then
  assert.strictEqual(tl.querySelector('.int-health').textContent, '2 instances');
  assert.strictEqual(tl.querySelector('.int-dot').className, 'int-dot off');
  assert.strictEqual(cards.length, 2);
  assert.strictEqual(cards[0].querySelector('.int-icard-l'), null, 'a neutral instance has no health label');
  assert.strictEqual(cards[1].querySelector('.int-icard-l').textContent, 'Not checked yet');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Integrations: two instances without checks show only the count, and Not checked yet only without a health entry');
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
  console.log('PASS Integrations: no img is rendered without a source, on the page, in the side panel or in the catalog');
}

function customCatalog() {
  const c = catalog();
  const by = (id) => c.plugins.filter((x) => x.id === id)[0];
  Object.assign(by('acme'), {origin: 'builtin', author: 'Skopax', licence: 'Apache-2.0'});
  Object.assign(by('pager'), {origin: 'package', module: 'acme_pager', author: 'Pat Pager', licence: 'MIT', version: '2.3.1'});
  Object.assign(by('evil'), {origin: 'env', module: 'local.evil', author: 'Eve', licence: 'GPL-3.0', version: '0.0.1'});
  Object.assign(by('prometheus'), {origin: 'package', module: 'acme_prom', author: 'Pat', licence: 'MIT', version: '1.0.0'});
  Object.assign(by('cracked'), {origin: 'env'});
  by('gold').origin = 'builtin';
  return c;
}

function knowCatalog(over) {
  over = over || {};
  const c = customCatalog();
  const docsField = {key: 'docs', label: 'Docs repository', kind: 'text', required: false, default: '', help: '', choices: [], low: null, high: null, showIf: null, ui: false};
  c.plugins.push(
    {id: 'sharepoint', name: 'SharePoint', version: '1.0.0', category: 'other', description: 'Reads a library.', edition: 'community', origin: 'builtin', icon: true, state: 'ok', reason: null, enabled: true, fields: FIELDS, checks: [],
      companyKnowledge: 'always', instances: over.sharepoint === false ? [] : [instance('docs')], health: health('ok', 'Healthy', iso(2))},
    {id: 'github', name: 'GitHub', version: '1.0.0', category: 'tickets', description: 'Reads repos.', edition: 'community', origin: 'builtin', icon: true, state: 'ok', reason: null, enabled: true, fields: FIELDS.concat([docsField]), checks: [],
      companyKnowledge: 'docs', instances: [instance('main', {values: {url: 'https://github.com', docs: over.docs === undefined ? 'runbooks' : over.docs}})], health: health('ok', 'Healthy', iso(2))},
    {id: 'mcp-tools', name: 'MCP servers', version: '1.0.0', category: 'other', description: 'Outside tools.', edition: 'community', origin: 'builtin', icon: true, state: 'ok', reason: null, enabled: true, fields: FIELDS, checks: [],
      companyKnowledge: '', instances: [instance('drive', {values: {url: 'https://mcp.example.com', knowledge: over.mcp === undefined ? 'true' : over.mcp}})], health: health('ok', 'Healthy', iso(2))});
  return c;
}

const KNOW_TIP = 'Knowledge source: the investigator searches and cites it';
const badgeOf = (doc, id) => doc.querySelector('#integrations .int-tile[data-id="' + id + '"] .int-know-tag');
const badgedIds = (doc) => [...doc.querySelectorAll('#integrations .int-tile')].filter((x) => x.querySelector('.int-know-tag')).map((x) => x.dataset.id);

async function thereIsNoKnowledgeGroupAndEveryKnowledgeTileSitsOnceInItsCategoryWithABadge() {
  //given
  const t = await boot({catalog: knowCatalog()});
  await open(t);
  const {doc} = t;

  //when
  const groups = [...doc.querySelectorAll('#integrations .int-group')].map((g) => [g.dataset.group, [...g.querySelectorAll('.int-tile')].map((x) => x.dataset.id)]);
  const everyTile = [...doc.querySelectorAll('#integrations .int-tile')].map((x) => x.dataset.id);
  const ids = [...doc.querySelectorAll('#integrations [id]')].map((x) => x.id);
  const cats = ['sharepoint', 'github', 'mcp-tools'].map((id) => doc.querySelector('#integrations .int-tile[data-id="' + id + '"]').dataset.cat);

  //then
  assert.deepStrictEqual(groups, [['custom', ['pager', 'cracked', 'evil']], ['builtin', ['acme', 'gold', 'sharepoint', 'github', 'mcp-tools']]]);
  assert.ok(!doc.querySelector('#integrations [data-group="knowledge"], #integrations .int-group-know, #integrations .int-group-sub'), 'no Company knowledge group');
  assert.deepStrictEqual(badgedIds(doc), ['sharepoint', 'github', 'mcp-tools'], 'every knowledge tile carries the badge, no other does');
  assert.deepStrictEqual(['sharepoint', 'github', 'mcp-tools'].map((id) => [badgeOf(doc, id).textContent, badgeOf(doc, id).title, badgeOf(doc, id).dataset.state]), [1, 2, 3].map(() => ['Knowledge', KNOW_TIP, 'on']));
  assert.deepStrictEqual(cats, ['other', 'tickets', 'other'], 'each stays in its own category');
  assert.strictEqual(new Set(everyTile).size, everyTile.length, 'a plug-in is one tile');
  assert.strictEqual(new Set(ids).size, ids.length, 'no duplicate DOM id');
  assert.strictEqual(tiles(doc).length, everyTile.length);
  const css = TEMPLATE_CSS();
  assert.ok(!/\.int-group-know|\.int-group-sub/.test(css), 'the group CSS is gone');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Integrations: there is no Company knowledge group, and each knowledge tile sits once in its own category with a Knowledge badge');
}

async function theKnowledgeBadgeShowsWhenAKnowledgeSourceIsOffOrNeedsSetup() {
  //given
  const c = knowCatalog({docs: ''});
  const by = (id) => c.plugins.filter((x) => x.id === id)[0];
  by('sharepoint').enabled = false;
  const t = await boot({catalog: c});
  await open(t);
  const warned = knowCatalog();
  warned.plugins.filter((x) => x.id === 'sharepoint')[0].health = health('warn', 'Slow', iso(2));
  const w2 = await boot({catalog: warned});
  await open(w2);
  const none = await boot({catalog: knowCatalog({mcp: 'false'})});
  await open(none);

  //when
  const read = (id) => [badgeOf(t.doc, id).textContent, badgeOf(t.doc, id).dataset.state, badgeOf(t.doc, id).className.includes('k-muted') ? 'muted' : badgeOf(t.doc, id).className.includes('k-warn') ? 'warn' : 'info', badgeOf(t.doc, id).title];

  //then
  assert.deepStrictEqual(read('sharepoint'), ['Knowledge off', 'off', 'muted', 'Knowledge source, off while this plug-in is off'], 'a switched-off source');
  assert.deepStrictEqual(read('github'), ['Knowledge needs setup', 'setup', 'warn', 'Knowledge source: fill the Docs field of an instance so the investigator can search it'], 'a source whose field is empty');
  assert.deepStrictEqual([badgeOf(w2.doc, 'sharepoint').textContent, badgeOf(w2.doc, 'sharepoint').dataset.state], ['Knowledge needs setup', 'setup'], 'a source with a problem on its tile');
  assert.ok(badgeOf(w2.doc, 'sharepoint').title.startsWith('Knowledge source: '), 'the tooltip says what to do');
  assert.ok(!badgeOf(none.doc, 'mcp-tools'), 'an MCP server with no knowledge connection carries none');
  assert.deepStrictEqual(badgedIds(t.doc), ['sharepoint', 'github', 'mcp-tools'], 'a source that is off or needs setup is still a knowledge tile');
  [t, w2, none].forEach((x) => { assert.deepStrictEqual(x.errors, []); x.w.close(); });
  console.log('PASS Integrations: the Knowledge badge reads Knowledge off for a switched-off source and Knowledge needs setup for an empty field or a problem');
}

async function withoutCustomTilesThereAreNoGroupsAndTheKnowledgeChipListsOnlyBadgedTiles() {
  //given
  const flat = await boot({catalog: (() => { const c = knowCatalog(); c.plugins = c.plugins.filter((x) => !['pager', 'cracked', 'evil', 'prometheus'].includes(x.id)); c.plugins.forEach((x) => { x.origin = 'builtin'; }); return c; })()});
  await open(flat);
  const none = await boot({catalog: knowCatalog({docs: '', mcp: 'false', sharepoint: false})});
  await open(none);
  const chips = (t) => [...t.doc.querySelectorAll('#integrations .int-chip')].map((c) => c.textContent);

  //when
  const chip = [...flat.doc.querySelectorAll('#integrations .int-chip')].filter((c) => c.textContent === 'Knowledge')[0];
  chip.click();

  //then
  assert.deepStrictEqual([flat.doc.querySelectorAll('#integrations .int-group').length, flat.doc.querySelectorAll('#integrations .int-grid').length], [0, 1], 'no custom tile: one plain grid');
  assert.deepStrictEqual(shown(flat.doc), badgedIds(flat.doc), 'the chip lists exactly the badged tiles');
  assert.ok(chips(none).includes('Knowledge'), 'GitHub with an empty Docs field still carries the badge, so the chip stays');
  [flat, none].forEach((t) => { assert.deepStrictEqual(t.errors, []); t.w.close(); });
  console.log('PASS Integrations: with no custom tile the tiles are one plain grid, and the Knowledge chip lists exactly the badged tiles');
}

async function theKnowledgeChipFiltersWithSearchCountsEachTileOnceAndFollowsAll() {
  //given
  const t = await boot({catalog: knowCatalog()});
  await open(t);
  const {doc, w} = t;
  const chip = (label) => [...doc.querySelectorAll('#integrations .int-chip')].filter((c) => c.textContent === label)[0];
  const find = doc.getElementById('int-find');

  //when
  const labels = [...doc.querySelectorAll('#integrations .int-chip')].map((c) => c.textContent);
  chip('Knowledge').click();
  const only = [shown(doc), chip('Knowledge').getAttribute('aria-pressed'), chip('All').getAttribute('aria-pressed')];
  find.value = 'git';
  find.dispatchEvent(new w.Event('input'));
  const searched = shown(doc);
  find.value = 'acme';
  find.dispatchEvent(new w.Event('input'));
  const missed = shown(doc);
  find.value = '';
  find.dispatchEvent(new w.Event('input'));
  chip('Custom').click();
  const custom = [shown(doc), chip('Knowledge').getAttribute('aria-pressed')];
  chip('All').click();
  const all = shown(doc).length;

  //then
  assert.deepStrictEqual(labels.slice(0, 3), ['All', 'Knowledge', 'Metrics']);
  assert.strictEqual(labels.filter((l) => l === 'Knowledge').length, 1, 'one Knowledge chip');
  assert.deepStrictEqual(only, [['sharepoint', 'github', 'mcp-tools'], 'true', 'false'], 'each knowledge plug-in once');
  assert.deepStrictEqual(searched, ['github'], 'the chip works with the search');
  assert.deepStrictEqual(missed, []);
  assert.deepStrictEqual(custom, [['pager', 'cracked', 'evil'], 'false'], 'the Custom chip replaces the Knowledge chip');
  assert.strictEqual(all, tiles(doc).length);
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Integrations: the Knowledge chip follows All, lists each knowledge plug-in once and filters with the search');
}

async function theCatalogHasNoKnowledgeGroupAndBadgesEveryKnowledgeEntryOnce() {
  //given
  const t = await boot({catalog: knowCatalog()});
  await open(t);
  const {doc, w} = t;
  const groups = () => [...doc.querySelectorAll('#int-pop .int-cat-grp')].filter((g) => !g.hidden).map((g) => [g.querySelector('.int-cat-h').textContent, [...g.querySelectorAll('.int-pick')].filter((b) => !b.hidden).map((b) => b.dataset.id)]);

  //when
  addBtn(doc).click();
  await wait(60);
  const start = groups();
  const note = pick(doc, 'github').querySelector('.int-pick-k').textContent;
  const quiet = !pick(doc, 'sharepoint').querySelector('.int-pick-k');
  const badged = picks(doc).filter((b) => b.querySelector('.int-know-tag')).map((b) => b.dataset.id);
  const every = picks(doc).map((b) => b.dataset.id);
  const search = doc.querySelector('#int-pop .int-search');
  search.value = 'sharepoint';
  search.dispatchEvent(new w.Event('input', {bubbles: true}));
  const found = groups();

  //then
  assert.ok(!doc.querySelector('#int-pop [data-cat="knowledge"]') && !start.some((g) => g[0] === 'Company knowledge'), 'no Company knowledge group');
  assert.ok(start.some((g) => g[1].includes('sharepoint')) && start.some((g) => g[1].includes('github')), 'each entry is in its own category');
  assert.deepStrictEqual(badged.sort(), ['github', 'sharepoint'], 'every knowledge entry carries the badge');
  assert.strictEqual(note, 'with its Docs field');
  assert.ok(quiet, 'an always-on source carries no note');
  assert.strictEqual(new Set(every).size, every.length, 'no entry appears twice');
  assert.deepStrictEqual(found.map((g) => g[1]), [['sharepoint']], 'the catalog search still finds it');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Integrations: the add-integration catalog has no knowledge group, badges each knowledge entry and lists nothing twice');
}

async function aCustomKnowledgePluginCarriesTheBadgeInItsGroupAndABrokenOneHasNone() {
  //given
  const c = customCatalog();
  const by = (id) => c.plugins.filter((x) => x.id === id)[0];
  by('pager').companyKnowledge = 'always';
  by('cracked').companyKnowledge = 'always';
  by('pager').health = health('ok', 'Healthy', iso(2));
  const t = await boot({catalog: c});
  await open(t);

  //when
  const read = [...t.doc.querySelectorAll('#integrations .int-group')].map((g) => [g.dataset.group, [...g.querySelectorAll('.int-tile')].map((x) => x.dataset.id)]);

  //then
  assert.deepStrictEqual(read, [['custom', ['pager', 'cracked', 'evil']], ['builtin', ['acme', 'gold']]], 'a custom knowledge plug-in stays in Custom plug-ins');
  assert.strictEqual(badgeOf(t.doc, 'pager').textContent, 'Knowledge');
  assert.strictEqual(badgeOf(t.doc, 'cracked').textContent, 'Knowledge off', 'a broken one reads off');
  assert.ok(!badgeOf(t.doc, 'evil'));
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Integrations: a custom knowledge plug-in carries the badge in its own group and a broken one reads Knowledge off');
}

async function aCustomPluginIsMarkedOnItsTileWithATooltipAndAnOrigin() {
  //given
  const t = await boot({catalog: customCatalog()});

  //when
  await open(t);
  const {doc} = t;
  const desc = (id) => { const open = tile(doc, id).querySelector('.int-open'); const ref = doc.getElementById(open.getAttribute('aria-describedby')); return [open.title, ref && ref.textContent]; };

  //then
  assert.deepStrictEqual(tiles(doc).map((x) => [x.dataset.id, x.dataset.origin]), [['pager', 'package'], ['cracked', 'env'], ['evil', 'env'], ['acme', 'builtin'], ['gold', 'builtin']]);
  assert.strictEqual(tile(doc, 'acme').querySelector('.int-cat').textContent, 'Metrics', 'a built-in tile looks as before');
  assert.ok(!tile(doc, 'acme').querySelector('.int-open').title && !tile(doc, 'acme').querySelector('.int-open').hasAttribute('aria-describedby'), 'no tooltip on a built-in');
  assert.strictEqual(tile(doc, 'pager').querySelector('.int-cat').textContent, 'Custom', 'a tile that needs attention keeps only the Custom marker');
  const said = 'Custom plug-in by Pat Pager, version 2.3.1, from acme_pager';
  assert.deepStrictEqual(desc('pager'), [said, said]);
  assert.strictEqual(tile(doc, 'evil').querySelector('.int-cat').textContent, 'Other ' + DOT + ' Custom');
  const eve = 'Custom plug-in by Eve, version 0.0.1, from local.evil';
  assert.deepStrictEqual(desc('evil'), [eve, eve]);
  assert.strictEqual(tile(doc, 'cracked').querySelector('.int-cat').textContent, 'Custom', 'a broken custom plug-in is marked too');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Integrations: a custom plug-in reads Custom on its tile with a tooltip and an accessible description, a built-in is unchanged');
}

async function theDrawerHeaderShowsOneMutedLineSayingBuiltInOrCustom() {
  //given
  const t = await boot({catalog: customCatalog()});
  await open(t);

  //when
  openTile(t, 'acme');
  const builtin = [inPanel(t.doc, '.int-meta').textContent, panel(t.doc).querySelectorAll('.int-meta').length, inPanel(t.doc, '.int-desc').nextElementSibling === inPanel(t.doc, '.int-meta')];
  openTile(t, 'pager');
  const custom = [inPanel(t.doc, '.int-meta').textContent, panel(t.doc).querySelectorAll('.int-meta').length];
  openTile(t, 'evil');
  const second = inPanel(t.doc, '.int-meta').textContent;

  //then
  assert.deepStrictEqual(builtin, ['Built-in ' + DOT + ' version 1.0.0', 1, true]);
  assert.deepStrictEqual(custom, ['Custom plug-in ' + DOT + ' by Pat Pager ' + DOT + ' version 2.3.1 ' + DOT + ' MIT', 1]);
  assert.strictEqual(second, 'Custom plug-in ' + DOT + ' by Eve ' + DOT + ' version 0.0.1 ' + DOT + ' GPL-3.0');
  assert.ok(inPanel(t.doc, '.int-meta').classList.contains('muted'));
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Integrations: the drawer header shows one muted meta line, Built-in or Custom plug-in with author, version and licence');
}

async function theCustomChipAppearsOnlyWhenACustomPluginIsLoadedAndFiltersAlone() {
  //given
  const none = await boot();
  await open(none);
  const plain = [...none.doc.querySelectorAll('#integrations .int-chip')].map((c) => c.textContent);
  none.w.close();
  const t = await boot({catalog: customCatalog()});
  await open(t);
  const {doc} = t;
  const chip = (label) => [...doc.querySelectorAll('#integrations .int-chip')].filter((c) => c.textContent === label)[0];

  //when
  const labels = [...doc.querySelectorAll('#integrations .int-chip')].map((c) => c.textContent);
  chip('Metrics').click();
  chip('Custom').click();
  const custom = [shown(doc), chip('Custom').getAttribute('aria-pressed'), chip('Metrics').getAttribute('aria-pressed'), chip('All').getAttribute('aria-pressed')];
  chip('Alerts').click();
  const alerts = [shown(doc), chip('Custom').getAttribute('aria-pressed')];
  chip('Custom').click();
  chip('All').click();
  const all = [shown(doc), chip('Custom').getAttribute('aria-pressed')];

  //then
  assert.ok(!plain.includes('Custom'), 'no custom plug-in loaded: no chip');
  assert.deepStrictEqual(labels, ['All', 'Metrics', 'Alerts', 'Tickets', 'Other', 'Custom']);
  assert.deepStrictEqual(custom, [['pager', 'cracked', 'evil'], 'true', 'false', 'false']);
  assert.deepStrictEqual(alerts, [['pager'], 'false']);
  assert.deepStrictEqual(all, [['pager', 'cracked', 'evil', 'acme', 'gold'], 'false']);
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Integrations: the Custom chip shows only when a custom plug-in is loaded and filters on its own, replacing the category');
}

async function customAndBuiltInPluginsSitInTheirOwnGroupsThatFollowTheFilter() {
  //given
  const plain = await boot();
  await open(plain);
  const ungrouped = [plain.doc.querySelectorAll('#integrations .int-group').length, plain.doc.querySelectorAll('#integrations .int-grid').length];
  plain.w.close();
  const t = await boot({catalog: customCatalog()});
  await open(t);
  const {doc} = t;
  const groups = () => [...doc.querySelectorAll('#integrations .int-group')];
  const read = () => groups().map((g) => [g.dataset.group, g.querySelector('.int-group-h').textContent, [...g.querySelectorAll('.int-tile')].map((x) => x.dataset.id), g.hidden]);
  const chip = (label) => [...doc.querySelectorAll('#integrations .int-chip')].filter((c) => c.textContent === label)[0];
  const find = doc.getElementById('int-find');

  //when
  const start = read();
  const named = groups().map((g) => doc.getElementById(g.getAttribute('aria-labelledby')) === g.querySelector('.int-group-h'));
  chip('Custom').click();
  const custom = groups().map((g) => g.hidden);
  chip('Tickets').click();
  const tickets = groups().map((g) => g.hidden);
  chip('All').click();
  find.value = 'acme';
  find.dispatchEvent(new t.w.Event('input'));
  const searched = groups().map((g) => g.hidden);

  //then
  assert.deepStrictEqual(ungrouped, [0, 1], 'no custom plug-in: one plain grid, no group headings');
  assert.deepStrictEqual(start, [['custom', 'Custom plug-ins 3', ['pager', 'cracked', 'evil'], false], ['builtin', 'Built-in plug-ins 2', ['acme', 'gold'], false]]);
  assert.deepStrictEqual(named, [true, true]);
  assert.deepStrictEqual(custom, [false, true], 'the Custom chip hides the built-in group');
  assert.deepStrictEqual(tickets, [true, false], 'a category with only built-ins hides the custom group');
  assert.deepStrictEqual(searched, [true, false], 'a search hides a group with no match');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Integrations: custom plug-ins and built-ins sit in their own headed groups, and a group with nothing shown hides');
}

async function theCatalogMarksACustomPluginQuietly() {
  //given
  const t = await boot({catalog: customCatalog()});
  await open(t);

  //when
  addBtn(t.doc).click();
  await wait(60);

  //then
  assert.strictEqual(pick(t.doc, 'prometheus').querySelector('.int-pick-c').textContent, 'Custom');
  assert.ok(!pick(t.doc, 'acme').querySelector('.int-pick-c'), 'a built-in carries no mark');
  assert.strictEqual(pick(t.doc, 'prometheus').dataset.origin, 'package');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Integrations: the add-integration catalog marks a custom plug-in with a quiet Custom label');
}

async function theSearchFiltersTheTilesByNameIdCategoryAndDescriptionWithoutCaseOrAccents() {
  //given
  const cat = catalog();
  cat.plugins[1].name = '\u00c7a\u011fr\u0131 Pager';
  cat.plugins[0].name = 'Acme Gr\u00f6\u00dfe';
  const t = await boot({catalog: cat});
  await open(t);
  const {doc, w} = t;
  const find = () => doc.querySelector('#integrations .int-find');
  const none = () => doc.querySelector('#integrations .int-none');
  const chip = (label) => [...doc.querySelectorAll('#integrations .int-chip')].filter((c) => c.textContent === label)[0];

  //when
  const box = [find().type, find().placeholder, find().getAttribute('aria-label'), find().closest('.int-top') !== null];
  const by = {};
  for (const q of ['ACME', 'gold', 'tickets', 'reads PAGES', 'cagri', 'grosse', 'Gr\u00f6sse', '\u00e7A\u011eRI']) {
    setValue(w, find(), q);
    by[q] = shown(doc);
  }
  setValue(w, find(), 'a');
  const wide = shown(doc);
  chip('Metrics').click();
  const narrowed = shown(doc);
  chip('All').click();
  setValue(w, find(), 'zz<b>q');
  const empty = [shown(doc), !none().hidden, none().querySelector('.int-none-t').textContent, none().querySelector('.int-none-clear').textContent, none().querySelectorAll('b').length];
  none().querySelector('.int-none-clear').click();
  const cleared = [find().value, shown(doc).length, none().hidden, doc.activeElement === find()];
  setValue(w, find(), 'acme');
  key(w, find(), 'Escape');
  const escaped = [find().value, shown(doc).length, wrap(doc).hidden];
  find().blur();
  key(w, doc.body, '/');
  const slashed = doc.activeElement === find();
  setValue(w, find(), 'pager');
  const typed = [key(w, find(), '/'), doc.activeElement === find()];
  find().blur();
  tile(doc, 'pager').querySelector('.int-switch').focus();
  key(w, doc.activeElement, '/');
  const fromSwitch = doc.activeElement === find();
  find().focus();
  tile(doc, 'pager').querySelector('.int-switch').click();
  await wait(120);
  const rerendered = [find().value, shown(doc), doc.activeElement === find()];
  tile(doc, 'pager').querySelector('.int-open').click();
  const opened = [!wrap(doc).hidden, intTileFound(w, 'pager')];
  key(w, doc.activeElement, 'Escape');
  const back = doc.activeElement === tile(doc, 'pager').querySelector('.int-open');

  //then
  assert.deepStrictEqual(box, ['search', 'Search integrations', 'Search integrations', true], 'a search box at the top of the page');
  assert.deepStrictEqual(by, {ACME: ['acme'], gold: ['gold'], tickets: ['gold'], 'reads PAGES': ['pager'], cagri: ['pager'], grosse: ['acme'], 'Gr\u00f6sse': ['acme'], '\u00e7A\u011eRI': ['pager']},
    'name, id, category label and description match, without case or accents');
  assert.ok(wide.length > 1 && wide.indexOf('acme') >= 0 && wide.indexOf('pager') >= 0, 'a wide query keeps many tiles: ' + wide);
  assert.deepStrictEqual(narrowed, ['acme'], 'the chip narrows and the search narrows further');
  assert.deepStrictEqual(empty, [[], true, 'No integration matches "zz<b>q"', 'Clear', 0], 'no match says so as text, with a Clear button');
  assert.deepStrictEqual(cleared, ['', 5, true, true], 'Clear empties the search, shows every tile and keeps the focus in the box');
  assert.deepStrictEqual(escaped, ['', 5, true], 'Esc clears the search');
  assert.ok(slashed, '/ focuses the search');
  assert.deepStrictEqual(typed, [true, true], 'a / typed in the box is left to the box');
  assert.ok(fromSwitch, 'a / on a tile switch, which is no input, focuses the search');
  assert.deepStrictEqual(rerendered, ['pager', ['pager'], true], 'a reload keeps the query, the filter and the focus');
  assert.deepStrictEqual(opened, [true, true], 'a filtered tile still opens and its controls are found');
  assert.ok(back, 'Esc returns focus to the filtered tile');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Integrations: the search filters tiles by name, id, category and description, without case or accents, with the chips, Clear, Esc and /');
}

function intTileFound(w, id) { return !!w.intTileOpen(id) && !!w.intTileSwitch(id); }

async function aSlashDoesNotStealFocusFromAFieldOrTheOpenDrawer() {
  //given
  const t = await boot();
  await open(t);
  const {doc, w} = t;
  const find = () => doc.querySelector('#integrations .int-find');

  //when
  openTile(t, 'acme');
  const field0 = doc.activeElement;
  const inField = [key(w, field0, '/'), doc.activeElement === field0];
  inPanel(doc, '.int-close').focus();
  key(w, doc.activeElement, '/');
  const drawer = [doc.activeElement === inPanel(doc, '.int-close'), doc.activeElement !== find()];
  key(w, doc.activeElement, 'Escape');
  addBtn(doc).click();
  pick(doc, 'acme').focus();
  key(w, doc.activeElement, '/');
  const pop = doc.activeElement === pick(doc, 'acme');
  key(w, doc.activeElement, 'Escape');
  doc.getElementById('tab-settings').click();
  await wait(80);
  doc.body.focus();
  const ev = new w.KeyboardEvent('keydown', {key: '/', bubbles: true, cancelable: true});
  doc.body.dispatchEvent(ev);

  //then
  assert.deepStrictEqual(inField, [true, true], 'a / typed in a field is left to the field');
  assert.deepStrictEqual(drawer, [true, true], 'the open drawer keeps the focus');
  assert.ok(pop, 'the open picker keeps the focus');
  assert.ok(!ev.defaultPrevented, 'off the Integrations page / does nothing');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Integrations: / leaves fields, the open drawer and the picker alone, and does nothing off the page');
}

async function theTilesAreCompactWithAStatusDotAShortHealthLineAndTheRestInTheTooltip() {
  //given
  const cat = catalog();
  cat.plugins[0].instances.push(instance('west'));
  cat.plugins[0].health = health('error', 'Token rejected (403)', iso(2), [{name: 'main', state: 'ok', text: 'Healthy', at: iso(2)}, {name: 'east', state: 'error', text: 'Token rejected (403)', at: iso(4)}, {name: 'west', state: 'ok', text: 'Healthy', at: iso(3)}]);
  cat.plugins[0].localeDrops = [{lang: 'de', count: 2, text: '2 de texts show in English', keys: ['a', 'b'], more: 0}];
  const t = await boot({catalog: cat});
  const css = TEMPLATE_CSS();

  //when
  await open(t);
  const {doc} = t;
  const acme = tile(doc, 'acme'), gold = tile(doc, 'gold'), evil = tile(doc, 'evil'), pager = tile(doc, 'pager');
  const row = (x) => [...x.querySelector('.int-tile-top').children].map((c) => c.className.split(' ')[0]);
  const st = (x) => [x.querySelector('.int-row1 .int-st .int-dot').className, x.querySelector('.int-row1 .int-st .sr').textContent];
  const line = (x) => [x.querySelector('.int-line .int-cat') ? x.querySelector('.int-line .int-cat').textContent : '', x.querySelector('.int-line .int-health').textContent];

  //then
  assert.deepStrictEqual(row(acme), ['int-ico', 'int-open', 'int-switch'], 'one row: icon, name with its dot, and the switch');
  assert.ok(acme.querySelector('.int-open .int-row1 .int-name') && acme.querySelector('.int-open .int-line'), 'the name row and a second line');
  assert.deepStrictEqual(st(acme), ['int-dot error', 'Needs attention']);
  assert.deepStrictEqual(st(pager), ['int-dot error', 'Needs attention']);
  assert.deepStrictEqual(st(evil), ['int-dot off', 'Off']);
  assert.deepStrictEqual(line(acme), ['', '3 instances ' + DOT + ' 1 needs attention'], 'more than one instance reads in short form');
  assert.deepStrictEqual(line(pager), ['', 'Token rejected (403)'], 'one instance or none keeps its health text');
  assert.ok(!doc.querySelector('#integrations .int-tile .int-reason') && !doc.querySelector('#integrations .int-tile .int-drop'), 'no reason and no drop line on a tile');
  assert.strictEqual(gold.title, 'Off ' + DOT + ' set up, paused\nGold board needs the Enterprise edition of Skopax', 'the reason is in the tooltip');
  assert.strictEqual(acme.title, 'Token rejected (403)\n2 de texts show in English: a, b', 'the full health and the drops are in the tooltip');
  assert.ok(acme.classList.contains('need') && pager.classList.contains('need') && gold.classList.contains('need') && !evil.classList.contains('need'), 'a tile that needs attention is marked');
  assert.ok(/\.int-tile\.need \{[^}]*border-left: 3px solid var\(--warn\);/.test(css), 'with a warn border on its left');
  assert.ok(/\.int-grid \{[^}]*grid-template-columns: repeat\(auto-fill, minmax\(min\(100%, 230px\), 1fr\)\);[^}]*gap: 8px;/.test(css), 'more tiles per row');
  assert.ok(/\.int-tile \.int-ico \{[^}]*width: 20px;[^}]*height: 20px;/.test(css), 'a 20 px icon');
  assert.ok(/\.int-line \{[^}]*white-space: nowrap;/.test(css) && /\.int-name \{[^}]*white-space: nowrap;/.test(css), 'both lines stay one line high, so a late health text never moves the grid');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Integrations: tiles are compact, with a status dot, a short health line, a warn edge and the reason and drops in the tooltip');
}

async function aTileThatNeedsAttentionDropsItsCategoryAndAMultiInstanceTileShowsTheFirstServerText() {
  //given
  const cat = catalog();
  cat.plugins[0].instances.push(instance('west'));
  cat.plugins[0].health = health('error', 'Token rejected (403)', iso(2), [{name: 'main', state: 'ok', text: 'Healthy', at: iso(2)}, {name: 'east', state: 'error', text: 'Token rejected (403)', at: iso(4)}, {name: 'west', state: 'error', text: 'Timed out', at: iso(3)}]);
  cat.plugins[1].origin = 'package';
  const t = await boot({catalog: cat});
  const css = TEMPLATE_CSS();

  //when
  await open(t);
  const {doc} = t;
  const line = (x) => [...x.querySelectorAll('.int-line > span')].map((c) => c.className.split(' ')[0] + ':' + c.textContent);

  //then
  assert.deepStrictEqual(line(tile(doc, 'acme')), ['int-health:3 instances ' + DOT + ' 2 need attention', 'int-why:Token rejected (403)'], 'no category, and the first failing server text is on the tile');
  assert.deepStrictEqual(line(tile(doc, 'pager')), ['int-cat:Custom', 'int-health:Token rejected (403)'], 'a custom marker stays, the category goes');
  assert.deepStrictEqual(line(tile(doc, 'evil')), ['int-cat:Other', 'int-health:' + OFF], 'a tile that is fine keeps its category');
  assert.ok(!tile(doc, 'pager').querySelector('.int-why'), 'a single instance has no extra text');
  assert.ok(/\.int-line \.int-cat \+ \.int-health::before/.test(css), 'the dot separator only follows a category');
  assert.ok(/\.int-line \.int-why::before/.test(css), 'the server text has its own separator');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Integrations: a tile that needs attention drops the category and a multi-instance tile shows the first server text');
}

async function darkBrandIconsGetALightBackplateInDarkModeOnly() {
  //given
  const cat = catalog();
  cat.plugins.push({id: 'kafka', name: 'Kafka', version: '1.0.0', category: 'infrastructure', description: '', edition: 'community', icon: true, state: 'ok', reason: null, enabled: true, fields: FIELDS, checks: [], instances: [instance('main', {primary: true})], health: health('ok', 'Healthy', iso(1))});
  const t = await boot({catalog: cat});
  const css = TEMPLATE_CSS();

  //when
  await open(t);

  //then
  assert.ok(tile(t.doc, 'kafka').querySelector('.int-ico').classList.contains('int-ico-dark'), 'a dark brand icon is marked');
  assert.ok(!tile(t.doc, 'acme').querySelector('.int-ico').classList.contains('int-ico-dark'), 'other icons are not');
  assert.ok(/prefers-color-scheme: dark\) \{[\s\S]*?:root:not\(\[data-theme="light"\]\) \.int-ico-dark \{ background: #[0-9a-f]{6}; \}/i.test(css), 'dark mode gives it a light backplate');
  assert.ok(/:root\[data-theme="dark"\] \.int-ico-dark \{ background: #[0-9a-f]{6}; \}/i.test(css), 'also when dark is picked by hand');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Integrations: dark brand icons get a light backplate in dark mode');
}

async function thePickerListsRowsWithDescriptionsAndWhatIsAddedAndMovesByKeyboard() {
  //given
  const cat = catalog();
  cat.plugins[0].instances.push(instance('west'));
  const t = await boot({catalog: cat});
  await open(t);
  const {doc, w} = t;
  const css = TEMPLATE_CSS();
  const visible = () => [...doc.querySelectorAll('#int-pop .int-pick')].filter((b) => !b.hidden && !b.closest('[hidden]'));

  //when
  addBtn(doc).click();
  const search = doc.querySelector('#int-pop .int-search');
  const rows = {acme: [pick(doc, 'acme').querySelector('.int-pick-n').textContent, pick(doc, 'acme').querySelector('.int-pick-d').textContent, pick(doc, 'acme').querySelector('.int-has').textContent],
    pager: pick(doc, 'pager').querySelector('.int-has').textContent, evil: [pick(doc, 'evil').querySelector('.int-has').textContent, pick(doc, 'evil').classList.contains('has')],
    prometheus: [pick(doc, 'prometheus').querySelector('.int-pick-d').textContent, !pick(doc, 'prometheus').querySelector('.int-has')], gold: !pick(doc, 'gold').querySelector('.int-pick-d')};
  key(w, search, 'ArrowDown');
  const first = doc.activeElement === visible()[0];
  key(w, doc.activeElement, 'ArrowDown');
  const second = doc.activeElement === visible()[1];
  key(w, doc.activeElement, 'ArrowUp');
  key(w, doc.activeElement, 'ArrowUp');
  const top = doc.activeElement === search;
  setValue(w, search, 'latency');
  const one = visible().map((b) => b.dataset.id);
  key(w, search, 'Enter');
  const entered = [doc.getElementById('int-pop').hidden, !wrap(doc).hidden, inPanel(doc, '.int-ph h2').textContent];
  key(w, doc.activeElement, 'Escape');
  addBtn(doc).click();
  setValue(w, doc.querySelector('#int-pop .int-search'), 'e');
  const many = visible().length;
  key(w, doc.querySelector('#int-pop .int-search'), 'Enter');
  const stayed = [doc.getElementById('int-pop').hidden, wrap(doc).hidden];
  setValue(w, doc.querySelector('#int-pop .int-search'), 'gold');
  key(w, doc.querySelector('#int-pop .int-search'), 'Enter');
  const locked = [doc.getElementById('int-pop').hidden, wrap(doc).hidden];
  key(w, doc.querySelector('#int-pop .int-search'), 'Escape');
  const closed = [doc.getElementById('int-pop').hidden, doc.activeElement === addBtn(doc)];

  //then
  assert.deepStrictEqual(rows, {acme: ['Acme metrics', 'Reads Acme.', '\u2713 3 added'], pager: '\u2713 Added', evil: ['Added', false], prometheus: ['Errors and latency.', true], gold: true},
    'each row: name, one-line description, and what is added already');
  assert.deepStrictEqual([first, second, top], [true, true, true], 'the arrows move from the search through the rows and back');
  assert.deepStrictEqual(one, ['prometheus'], 'the search reads the description too');
  assert.deepStrictEqual(entered, [true, true, 'Prometheus'], 'Enter on a single result opens it');
  assert.ok(many > 1 && stayed[0] === false && stayed[1] === true, 'Enter with more than one result opens nothing');
  assert.deepStrictEqual(locked, [false, true], 'a single locked result stays inert');
  assert.deepStrictEqual(closed, [true, true], 'Esc closes and returns focus to the button');
  assert.ok(/\.int-pg \{[^}]*grid-template-columns: minmax\(0, 1fr\);/.test(css), 'entries are rows');
  assert.ok(/@media \(max-width: 480px\) \{[^@]*\.int-pop \{[^}]*position: fixed;[^}]*left: 0;[^}]*right: 0;[^}]*width: auto;/.test(css), 'full width on a phone');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Integrations: the picker lists rows with a description and what is added, moves by arrows, and Enter opens a single result');
}

async function theDrawerListsInstancesAsCardsUnderAHeaderWithTheAddButton() {
  //given
  const t = await boot();
  await open(t);
  const {doc} = t;
  const cards = () => [...panel(doc).querySelectorAll('.int-ilist .int-icard')];

  //when
  openTile(t, 'acme');
  const head = [inPanel(doc, '.int-ilist-h .int-ilist-t').textContent, inPanel(doc, '.int-ilist-h .int-add') === inPanel(doc, '.int-add'), inPanel(doc, '.int-add .int-add-i').getAttribute('aria-hidden')];
  const listed = cards().map((c) => [c.querySelector('.int-itab').textContent, !!c.querySelector('.int-icard-p'), c.querySelector('.int-icard-s').textContent, c.querySelector('.int-dot').className,
    c.querySelector('.int-icard-l').textContent, c.querySelector('.int-icard-e').textContent, c.classList.contains('sel')]);
  const focused = doc.activeElement;
  cards()[0].querySelector('.int-itab').click();
  const same = [doc.activeElement === focused, cards()[0].classList.contains('sel')];
  cards()[1].querySelector('.int-itab').click();
  const east = [cards()[1].classList.contains('sel'), doc.activeElement === cards()[1].querySelector('.int-itab'), inPanel(doc, '.int-inst').dataset.name];
  inPanel(doc, '.int-close').click();
  addBtn(doc).click();
  pick(doc, 'prometheus').click();
  const empty = [inPanel(doc, '.int-ilist-t').textContent, inPanel(doc, '.int-iempty .int-iempty-t').textContent, inPanel(doc, '.int-iempty .int-iempty-d').textContent, !!inPanel(doc, '.int-iempty .int-add'),
    cards().length, !!inPanel(doc, '.int-form'), doc.activeElement === inPanel(doc, '.int-name-in')];
  inPanel(doc, '.int-add').click();
  const added = doc.activeElement === inPanel(doc, '.int-name-in');

  //then
  assert.deepStrictEqual(head, ['Instances ' + DOT + ' 2', true, 'true'], 'the header counts the instances and holds the one Add button, with an icon');
  assert.deepStrictEqual(listed, [['main', true, 'On', 'int-dot ok', 'Healthy ' + DOT + ' 2 min ago', 'Editing', true], ['east', false, 'On', 'int-dot ok', 'Healthy ' + DOT + ' 4 min ago', 'Edit', false]],
    'each card: name, primary badge, state, health dot, last check and Edit');
  assert.deepStrictEqual(same, [true, true], 'a click on the open card changes nothing');
  assert.deepStrictEqual(east, [true, true, 'east'], 'a click on another card opens it');
  assert.deepStrictEqual(empty, ['Instances', 'No instances yet', 'An instance is one connection to Prometheus, with its own address and sign-in. Add one for each server or account.', true, 0, true, true],
    'no instance: an empty state with its Add button, and the form below');
  assert.ok(added, 'its Add button goes to the form');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS Integrations: the drawer lists instances as cards under a counted header with the Add button, or an empty state');
}

async function thePanelShowsTheSetupGuideOpenWithoutInstancesAndClosedWithOne() {
  //given
  const guide = {steps: ['Open Settings', EVIL, 'Copy the token'], docs: 'https://docs.example.com/tokens', docsLabel: 'Example: tokens'};
  const bare = {steps: ['Create a key'], docs: '', docsLabel: ''};
  const unlabeled = {steps: ['Create a key'], docs: 'https://docs.example.com/key', docsLabel: '  '};
  const hostile = {steps: ['x'], docs: 'javascript:alert(1)', docsLabel: 'Bad'};
  const base = {version: '1.0.0', category: 'other', description: '', edition: 'community', icon: false, state: 'ok', reason: null, enabled: true, fields: FIELDS, checks: []};
  const cat = {edition: 'community', secretsKey: 'generated', plugins: [
    Object.assign({id: 'fresh', name: 'Fresh', instances: [], health: health('off', 'Not set up yet'), setup: guide}, base),
    Object.assign({id: 'used', name: 'Used', instances: [instance('main', {primary: true})], health: health('ok', 'Healthy', iso(1), [{name: 'main', state: 'ok', text: 'Healthy', at: iso(1)}]), setup: bare}, base),
    Object.assign({id: 'none', name: 'None', instances: [], health: health('off', 'Not set up yet'), setup: null}, base),
    Object.assign({id: 'formless', name: 'Formless', fields: [], instances: [], health: health('off', 'Not set up yet'), setup: bare}, base, {fields: []}),
    Object.assign({id: 'plain', name: 'Plain', instances: [], health: health('off', 'Not set up yet'), setup: unlabeled}, base),
    Object.assign({id: 'odd', name: 'Odd', instances: [], health: health('off', 'Not set up yet'), setup: hostile}, base)]};
  const t = await boot({catalog: cat});
  await open(t);
  const via = (id) => { if (panel(t.doc) && inPanel(t.doc, '.int-close')) inPanel(t.doc, '.int-close').click(); addBtn(t.doc).click(); pick(t.doc, id).click(); };

  //when
  via('fresh');
  const fresh = inPanel(t.doc, '.int-setup');
  const freshOpen = fresh.open, summary = fresh.querySelector('summary').textContent, items = [...fresh.querySelectorAll('ol > li')].map((li) => li.textContent);
  const link = fresh.querySelector('a'), position = [...panel(t.doc).children].indexOf(fresh) - [...panel(t.doc).children].indexOf(inPanel(t.doc, '.int-meta'));
  const markup = t.doc.querySelectorAll('img[onerror]').length;
  via('used');
  const used = inPanel(t.doc, '.int-setup'), usedOpen = used.open, usedLink = used.querySelector('a');
  via('none');
  const none = inPanel(t.doc, '.int-setup');
  via('formless');
  const formless = inPanel(t.doc, '.int-setup'), formlessText = inPanel(t.doc, '.int-nofields') !== null;
  via('plain');
  const plainLink = inPanel(t.doc, '.int-setup a').textContent;
  via('fresh');
  inPanel(t.doc, '.int-setup summary').click();
  via('used');
  inPanel(t.doc, '.int-setup summary').click();
  via('fresh');
  const keptClosed = inPanel(t.doc, '.int-setup').open;
  via('used');
  const keptOpen = inPanel(t.doc, '.int-setup').open;
  via('odd');
  const odd = inPanel(t.doc, '.int-setup');

  //then
  assert.strictEqual(freshOpen, true, 'open when the plug-in has no instance');
  assert.strictEqual(summary, 'How to set it up');
  assert.deepStrictEqual(items, guide.steps, 'the steps are text');
  assert.strictEqual(markup, 0);
  assert.strictEqual(position, 1, 'right after the meta line');
  assert.deepStrictEqual([link.textContent, link.getAttribute('href'), link.target, link.rel], ['Example: tokens', 'https://docs.example.com/tokens', '_blank', 'noopener noreferrer']);
  assert.strictEqual(usedOpen, false, 'closed when an instance exists');
  assert.strictEqual(usedLink, null, 'no link without docs');
  assert.ok(formless && formless.open && formlessText, 'shown, open, for a plug-in with no fields');
  assert.strictEqual(plainLink, 'Setup guide', 'a blank label falls back');
  assert.deepStrictEqual([keptClosed, keptOpen], [false, true], 'a toggle is kept per plug-in across repaints');
  assert.strictEqual(none, null, 'absent when setup is null');
  assert.strictEqual(odd.querySelector('a'), null, 'only https links are linked');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  const css = TEMPLATE_CSS();
  assert.ok(/\.int-setup > summary::before \{[^}]*border-right:[^}]*transform: rotate\(-45deg\)/.test(css), 'a chevron marks the folded summary as clickable');
  assert.ok(/\.int-setup\[open\] > summary::before \{[^}]*rotate\(45deg\)/.test(css), 'and turns down when open');
  assert.ok(/\.int-setup > summary:hover \{[^}]*var\(--fg\)/.test(css), 'and the summary darkens on hover');
  console.log('PASS Integrations: the setup guide is open without instances, closed with one, escaped, and links only https docs');
}

(async () => {
  await theTilesShowEveryConnectedPluginWithItsStateHealthAndIcon();
  await aCustomPluginIsMarkedOnItsTileWithATooltipAndAnOrigin();
  await theDrawerHeaderShowsOneMutedLineSayingBuiltInOrCustom();
  await theCustomChipAppearsOnlyWhenACustomPluginIsLoadedAndFiltersAlone();
  await customAndBuiltInPluginsSitInTheirOwnGroupsThatFollowTheFilter();
  await theCatalogMarksACustomPluginQuietly();
  await anInstanceWithoutACheckShowsNoHealthTextAndNeverConnected();
  await twoInstancesWithoutChecksShowTheCountOnlyAndNotCheckedYetOnlyWhereNoHealthEntryExists();
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
  await aPluginSetUpOutsideAdminIsNeitherConnectedNorNeedingAttention();
  await escAndFocusStayWithThePanelWhateverHasFocus();
  await instanceControlsSwitchPromoteAndDeleteWithConfirmation();
  await droppedPackTextsShowOneWarningLinePerLanguageOnTheCardAndInThePanel();
  await everyValueIsEscaped();
  await instanceNamesSourcesFieldsAndNotesAreEscapedToo();
  await anUntouchedSelectIsNeverSent();
  await aChoiceWhoseDefaultIsEmptyCanBeSetBackToEmpty();
  await aRouteEchoChoiceShowsExactlyThreeOptionsAndStartsOnTheDefault();
  await anotherPluginsChoiceWithADefaultOptionKeepsItsBlankOption();
  await anInstanceFieldPicksAnInstanceOfTheUsedPlugin();
  await aRefusalOnAHiddenFieldStillShowsAMessage();
  await dottedFieldKeysEditAndCarryTheirRefusal();
  await aSwitchedOffInstanceOffersNoMakePrimary();
  await leavingTheTabOrTheRoleClosesThePanelAndTheCatalog();
  await theSidePanelIsAViewportOverlayAndFullScreenOnAPhone();
  await aNonAdminSeesNoIntegrationsTab();
  await testConnectionSendsTheTicksOnlyWhenTheFormChangedThem();
  await theSearchFiltersTheTilesByNameIdCategoryAndDescriptionWithoutCaseOrAccents();
  await aSlashDoesNotStealFocusFromAFieldOrTheOpenDrawer();
  await theTilesAreCompactWithAStatusDotAShortHealthLineAndTheRestInTheTooltip();
  await aTileThatNeedsAttentionDropsItsCategoryAndAMultiInstanceTileShowsTheFirstServerText();
  await darkBrandIconsGetALightBackplateInDarkModeOnly();
  await thePickerListsRowsWithDescriptionsAndWhatIsAddedAndMovesByKeyboard();
  await theDrawerListsInstancesAsCardsUnderAHeaderWithTheAddButton();
  await thePanelShowsTheSetupGuideOpenWithoutInstancesAndClosedWithOne();
  await thereIsNoKnowledgeGroupAndEveryKnowledgeTileSitsOnceInItsCategoryWithABadge();
  await theKnowledgeBadgeShowsWhenAKnowledgeSourceIsOffOrNeedsSetup();
  await withoutCustomTilesThereAreNoGroupsAndTheKnowledgeChipListsOnlyBadgedTiles();
  await theKnowledgeChipFiltersWithSearchCountsEachTileOnceAndFollowsAll();
  await theCatalogHasNoKnowledgeGroupAndBadgesEveryKnowledgeEntryOnce();
  await aCustomKnowledgePluginCarriesTheBadgeInItsGroupAndABrokenOneHasNone();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
