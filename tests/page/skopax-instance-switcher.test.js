const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const EVIL = '<img src=x onerror=alert(1)>"\'';
const STAGING = 'https://staging.example.com';
const EU = 'https://eu.example.com/board';
const LINKS = [{name: 'staging', url: STAGING, letter: 'S', colour: '#5cbf80'}, {name: 'eu', url: EU, letter: 'E', colour: '#9a7ce3'}];
const BRAND = 'rgb(60, 200, 240)';
const ADMIN_ITEMS = [
  {key: 'general.timezone', group: 'General', label: 'Timezone', help: 'IANA timezone name', kind: 'text', live: true, value: 'UTC', base: 'UTC', default: 'UTC', source: 'default'},
  {key: 'core.workspace-links', group: 'Workspaces', label: 'Other workspaces', help: 'name=url pairs', kind: 'text', live: true, value: '', base: '', default: '', source: 'default'},
  {key: 'investigate.model', group: 'Investigator', label: 'Card model', help: 'Model for card runs', kind: 'choice', choices: ['claude-opus-5-5'], live: true, value: 'claude-opus-5-5', base: 'claude-opus-5-5', default: 'claude-opus-5-5', source: 'default'}];

function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'ok', headline: 'x', points: [], at: iso(1)}, anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(opts) {
  opts = opts || {};
  const errors = [], went = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  vc.on('error', (e) => errors.push('console.error ' + e));
  const me = Object.assign({loginEnabled: true, user: 'alice@example.com', operator: true, admin: false, teams: [], tags: [], chat: true,
    instances: {current: {name: 'production', letter: 'P'}, links: LINKS}}, opts.me || {});
  if (opts.noInstances) delete me.instances;
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state())), {
    url: 'https://skopax.example.com/' + (opts.hash || '#t=anoms'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      Object.keys(opts.stored || {}).forEach((k) => w.localStorage.setItem(k, opts.stored[k]));
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me')) { if (opts.meDelay) await wait(opts.meDelay); return ok(me); }
        if (p.startsWith('/api/server')) return ok({plugins: [], tz: 'UTC'});
        if (p.startsWith('/api/admin/settings')) return ok({items: opts.items || ADMIN_ITEMS, rev: 1, refreshSeconds: 15});
        if (p.startsWith('/api/admin/history')) return ok([]);
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
      };
    },
  });
  dom.window.__skopaxGo = (url) => went.push(url);
  await wait(opts.settle || 350);
  return {dom, w: dom.window, doc: dom.window.document, errors, went};
}

const key = (w, el, k, extra) => el.dispatchEvent(new w.KeyboardEvent('keydown', Object.assign({key: k, bubbles: true, cancelable: true}, extra || {})));
const items = (doc) => [...doc.querySelectorAll('#inst-menu [role^="menuitem"]')];
const names = (doc) => items(doc).map((i) => i.querySelector('.mi-n').textContent);
const tile = (w, el) => el.querySelector('.inst-tile').style.getPropertyValue('background-color');

async function theButtonNamesTheCurrentInstance() {
  //given
  const {dom, w, doc, errors} = await boot();
  const early = await boot({meDelay: 600, settle: 100});
  const old = await boot({noInstances: true});
  const btn = doc.getElementById('inst-btn');

  //then
  assert.deepStrictEqual([...doc.getElementById('side-top').children].map((c) => c.id), ['inst', 'search-btn'], 'the switcher, then the search, at the top of the sidebar');
  assert.strictEqual(btn.querySelector('.inst-name').textContent, 'production', 'the button names the current instance');
  const nameStyle = w.getComputedStyle(btn.querySelector('.inst-name'));
  assert.ok(nameStyle.paddingTop !== '7px' && nameStyle.borderTopWidth !== '1px', 'the Admin workspace field rules do not restyle the sidebar name');
  assert.ok(![...doc.styleSheets].some((sheet) => [...sheet.cssRules].some((r) => r.selectorText && /\.inst-(name|url)\b/.test(r.selectorText) && /padding: 7px/.test(r.cssText))), 'no rule gives the sidebar name the field padding');
  assert.strictEqual(btn.querySelector('.inst-k').textContent, 'Workspace');
  assert.strictEqual(btn.querySelector('.inst-tile').textContent, 'P', 'with its letter on the tile');
  assert.strictEqual(tile(w, btn), BRAND, 'the current entry is brand cyan');
  assert.deepStrictEqual(['aria-haspopup', 'aria-expanded', 'aria-controls', 'aria-label', 'title'].map((a) => btn.getAttribute(a)),
    ['menu', 'false', 'inst-menu', 'Workspace: production. Switch workspace', 'Workspace: production']);
  assert.strictEqual(doc.getElementById('inst-menu').getAttribute('role'), 'menu');
  assert.ok(doc.getElementById('inst-menu').hidden, 'the menu starts closed');
  assert.strictEqual(doc.getElementById('env-tag'), null, 'the environment chip left the top bar: the switcher names the deployment now');
  assert.ok(!/undefined|null/.test(early.doc.getElementById('side-top').textContent), 'before the login check answers the slot shows no placeholder text: ' + early.doc.getElementById('side-top').textContent);
  assert.strictEqual(old.doc.querySelector('#inst-btn .inst-name').textContent, 'skopax.example.com', 'an answer without instances names the host the page came from');
  await wait(650);
  assert.strictEqual(early.doc.querySelector('#inst-btn .inst-name').textContent, 'production', 'and the name once it answers');
  assert.deepStrictEqual(errors.concat(early.errors, old.errors), []);
  dom.window.close(); early.dom.window.close(); old.dom.window.close();
  return 'the switcher button shows the letter tile, "Workspace" and the current name, closed, ahead of the search button';
}

async function theMenuListsTheLinksWithTheirHosts() {
  //given
  const {dom, w, doc, errors} = await boot();
  const btn = doc.getElementById('inst-btn');

  //when
  btn.click();

  //then
  const menu = doc.getElementById('inst-menu'), list = items(doc);
  assert.ok(!menu.hidden && btn.getAttribute('aria-expanded') === 'true', 'a click opens it');
  assert.strictEqual(menu.getAttribute('aria-label'), 'Switch workspace');
  assert.deepStrictEqual(names(doc), ['production', 'staging', 'eu'], 'the current entry first, then the links in their order');
  assert.deepStrictEqual(list.map((i) => i.getAttribute('role')), ['menuitemradio', 'menuitemradio', 'menuitemradio']);
  assert.deepStrictEqual(list.map((i) => i.getAttribute('aria-checked')), ['true', 'false', 'false'], 'the current one is checked');
  assert.deepStrictEqual(list.map((i) => i.querySelector('small').textContent), ['skopax.example.com', 'staging.example.com', 'eu.example.com'], 'each shows its host small');
  assert.deepStrictEqual(list.map((i) => i.querySelector('.inst-tile').textContent), ['P', 'S', 'E']);
  assert.deepStrictEqual(list.map((i) => tile(w, i)), [BRAND, 'rgb(92, 191, 128)', 'rgb(154, 124, 227)'], 'each link keeps the colour the server picked');
  assert.deepStrictEqual(list.slice(1).map((i) => [i.tagName, i.getAttribute('href'), i.getAttribute('rel')]), [['A', STAGING, 'noopener noreferrer'], ['A', EU, 'noopener noreferrer']], 'the links are real links');
  assert.deepStrictEqual(list.slice(1).map((i) => w.getComputedStyle(i).getPropertyValue('box-sizing')), ['border-box', 'border-box'], 'a link row keeps its padding inside the menu width, as the button rows do');
  assert.strictEqual(doc.querySelector('#inst-menu [role="separator"]'), null, 'a non-admin gets no separator');
  assert.ok(!/Manage workspaces/.test(menu.textContent), 'and no Manage workspaces');
  assert.strictEqual(doc.activeElement, list[0], 'the checked entry takes the focus');

  //when
  doc.body.dispatchEvent(new w.MouseEvent('click', {bubbles: true}));

  //then
  assert.ok(menu.hidden && btn.getAttribute('aria-expanded') === 'false', 'a click outside closes it');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the menu lists the current entry checked, then each link with its host, letter and colour';
}

async function downOpensTheMenuOnTheCheckedItem() {
  //given
  const {dom, w, doc, errors} = await boot();
  const btn = doc.getElementById('inst-btn'), menu = doc.getElementById('inst-menu');

  //when
  btn.focus();
  key(w, btn, 'ArrowDown');

  //then
  assert.ok(!menu.hidden, 'ArrowDown on the button opens the menu');
  assert.strictEqual(doc.activeElement, items(doc)[0], 'on the checked entry');

  //when
  key(w, doc.activeElement, 'ArrowDown');
  const second = doc.activeElement;
  key(w, doc.activeElement, 'End');
  const last = doc.activeElement;
  key(w, doc.activeElement, 'ArrowDown');
  const wrapped = doc.activeElement;
  key(w, doc.activeElement, 'ArrowUp');
  const back = doc.activeElement;
  key(w, doc.activeElement, 'Home');
  const home = doc.activeElement;
  const escaped = !key(w, doc.activeElement, 'Escape');

  //then
  assert.deepStrictEqual([second, last, wrapped, back, home], [items(doc)[1], items(doc)[2], items(doc)[0], items(doc)[2], items(doc)[0]], 'the arrows move and wrap, Home and End jump');
  assert.ok(escaped && menu.hidden && doc.activeElement === btn, 'Escape closes it and gives the focus back to the button');

  //when
  key(w, btn, 'ArrowUp');
  const up = doc.activeElement;
  key(w, doc.activeElement, 'Tab');

  //then
  assert.strictEqual(up, items(doc)[0], 'ArrowUp opens it on the checked entry too');
  assert.ok(menu.hidden, 'Tab closes it');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'ArrowDown opens the menu on the checked entry; the arrows, Home and End move; Escape and Tab close it';
}

async function pickingAnotherInstanceGoesToItsAddress() {
  //given
  const {dom, w, doc, errors, went} = await boot();
  const btn = doc.getElementById('inst-btn'), menu = doc.getElementById('inst-menu');
  const left = [];
  doc.addEventListener('click', (e) => { if (e.target.closest && e.target.closest('#inst-menu a')) { left.push(e.defaultPrevented); e.preventDefault(); } });

  //when
  btn.click();
  items(doc)[0].click();
  const afterCurrent = [menu.hidden, went.length];
  btn.click();
  items(doc)[1].click();
  const afterPick = [menu.hidden, went.slice()];
  btn.click();
  items(doc)[2].dispatchEvent(new w.MouseEvent('click', {bubbles: true, cancelable: true, ctrlKey: true}));
  items(doc)[2].dispatchEvent(new w.MouseEvent('click', {bubbles: true, cancelable: true, metaKey: true}));
  const afterTab = went.slice();
  btn.click();
  key(w, btn, 'ArrowDown');
  key(w, doc.activeElement, 'ArrowDown');
  key(w, doc.activeElement, 'ArrowDown');
  const entered = !key(w, doc.activeElement, 'Enter');

  //then
  assert.deepStrictEqual(afterCurrent, [true, 0], 'picking the current entry only closes the menu');
  assert.deepStrictEqual(afterPick, [true, [STAGING]], 'picking staging goes to its address');
  assert.deepStrictEqual(afterTab, [STAGING], 'a Ctrl- or Cmd-click goes nowhere here');
  assert.deepStrictEqual(left, [true, false, false, true], 'it is left to the browser, which opens the link in a new tab');
  assert.ok(entered, 'Enter on an entry is taken');
  assert.deepStrictEqual(went, [STAGING, EU], 'and goes to that entry');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'picking staging goes to its URL, Enter does the same, the current entry only closes the menu, and Ctrl- or Cmd-click is left to the browser for a new tab';
}

async function manageWorkspacesIsOnlyForAdmins() {
  //given
  const admin = await boot({me: {admin: true, userRole: 'admin'}});
  const viewer = await boot({me: {admin: false, operator: false, userRole: 'viewer'}});
  const doc = admin.doc;

  //when
  doc.getElementById('inst-btn').click();
  viewer.doc.getElementById('inst-btn').click();
  const sep = doc.querySelector('#inst-menu [role="separator"]');
  const manage = [...doc.querySelectorAll('#inst-menu [role="menuitem"]')];

  //then
  assert.ok(sep && sep.previousElementSibling === items(doc)[2], 'a separator follows the entries');
  assert.deepStrictEqual(manage.map((m) => m.textContent), ['Manage workspaces'], 'then Manage workspaces');
  assert.strictEqual(items(doc).pop(), manage[0], 'it is the last item the keys reach');
  assert.deepStrictEqual([viewer.doc.querySelector('#inst-menu [role="separator"]'), viewer.doc.querySelectorAll('#inst-menu [role="menuitem"]').length], [null, 0], 'a viewer gets neither');

  //when
  manage[0].click();
  await wait(150);

  //then
  assert.ok(doc.getElementById('inst-menu').hidden, 'the menu closes');
  assert.ok(!doc.getElementById('panel-workspaces').hidden, 'Manage workspaces opens the Workspaces section');
  assert.strictEqual(admin.w.location.hash, '#t=workspaces');
  assert.deepStrictEqual(admin.errors.concat(viewer.errors), []);
  admin.dom.window.close(); viewer.dom.window.close();
  return 'admins get a separator and Manage workspaces, which opens the Workspaces section; viewers get neither';
}

async function hostileNamesAndAddressesStayText() {
  //given
  const evil = {current: {name: EVIL, letter: '<'}, links: [{name: EVIL, url: 'https://evil.example.com/?q="><img src=x>', letter: '<', colour: 'red;background:url(x)'}, {name: 'bad', url: 'javascript:alert(1)', letter: 'B', colour: '#e3a744'}, {name: 'files', url: 'ftp://files.example.com', letter: 'F', colour: '#e3a744'}]};
  const {dom, w, doc, errors, went} = await boot({me: {instances: evil}});
  const btn = doc.getElementById('inst-btn');

  //when
  btn.click();

  //then
  assert.strictEqual(btn.querySelector('.inst-name').textContent, EVIL, 'the name is text');
  assert.strictEqual(btn.getAttribute('aria-label'), 'Workspace: ' + EVIL + '. Switch workspace');
  assert.deepStrictEqual(names(doc), [EVIL, EVIL], 'only http and https links are listed');
  assert.strictEqual(items(doc)[1].getAttribute('href'), 'https://evil.example.com/?q="><img src=x>');
  assert.strictEqual(items(doc)[1].querySelector('small').textContent, 'evil.example.com');
  assert.strictEqual(tile(w, items(doc)[1]), 'rgb(227, 167, 68)', 'a colour outside the fixed list falls back to the first one');
  assert.strictEqual(doc.querySelectorAll('#side-top img').length, 0, 'no markup got through');
  assert.ok(items(doc).every((i) => i.querySelector('.mi-t').hasAttribute('data-no-tx')), 'names are never translated');

  //when
  items(doc)[1].click();

  //then
  assert.deepStrictEqual(went, ['https://evil.example.com/?q="><img src=x>']);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a hostile name, letter, colour or address renders as text, and a javascript: or ftp: link is never listed';
}

async function withNoLinksTheMenuHoldsTheCurrentEntry() {
  //given
  const alone = {current: {name: 'production', letter: 'P'}, links: []};
  const viewer = await boot({me: {instances: alone}});
  const admin = await boot({me: {instances: alone, admin: true, userRole: 'admin'}});

  //when
  viewer.doc.getElementById('inst-btn').click();
  admin.doc.getElementById('inst-btn').click();

  //then
  assert.deepStrictEqual(names(viewer.doc), ['production'], 'the current entry alone');
  assert.strictEqual(items(viewer.doc)[0].getAttribute('aria-checked'), 'true');
  assert.deepStrictEqual(items(admin.doc).map((i) => [i.getAttribute('role'), (i.querySelector('.mi-n') || i).textContent]), [['menuitemradio', 'production'], ['menuitem', 'Manage workspaces']], 'and Manage workspaces for an admin');
  assert.deepStrictEqual(viewer.errors.concat(admin.errors), []);
  viewer.dom.window.close(); admin.dom.window.close();
  return 'with no linked deployments the menu holds the current entry, and Manage workspaces for admins';
}

async function theRailKeepsTheTileAndTheSearchIcon() {
  //given
  const {dom, w, doc, errors} = await boot({stored: {'skopax-sidebar': 'rail'}});
  const cs = (sel) => w.getComputedStyle(doc.querySelector(sel));

  //then
  assert.ok(doc.body.classList.contains('is-rail'));
  assert.strictEqual(cs('#inst-btn .inst-t').getPropertyValue('clip-path'), 'inset(50%)', 'the name is kept for screen readers only');
  assert.strictEqual(cs('#search-btn .lbl').getPropertyValue('clip-path'), 'inset(50%)');
  assert.strictEqual(cs('#search-btn kbd').getPropertyValue('clip-path'), 'inset(50%)');
  assert.notStrictEqual(cs('#inst-btn .inst-tile').getPropertyValue('display'), 'none', 'the tile stays');
  assert.deepStrictEqual(['left', 'right'].map((p) => cs('#inst-menu').getPropertyValue(p)), ['calc(100% + 12px)', 'auto'], 'the menu opens beside the rail');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'in the rail the switcher shows its tile and the search its icon, and the menu opens beside the rail';
}

async function aRefusedValueShowsOnlyItsNoteInAdmin() {
  //given
  const problem = 'The environment value is not used. Entry 1: ' + EVIL;
  const items = ADMIN_ITEMS.map((i) => i.key === 'core.workspace-links' ? Object.assign({}, i, {value: null, base: null, source: 'config', from: 'environment', problem}) : i);
  const {dom, doc, errors} = await boot({me: {admin: true, userRole: 'admin'}, items, hash: '#t=workspaces'});
  const row = doc.querySelector('#workspaces .adm-row[data-key="core.workspace-links"]');

  //then
  assert.ok(row, 'the Other workspaces list is shown');
  assert.deepStrictEqual([...row.querySelectorAll('.adm-problem')].map((n) => n.textContent), [problem], 'it carries the note naming the source, the entry and the rule');
  assert.strictEqual(row.querySelectorAll('.ws-row').length, 0, 'and no rows');
  assert.strictEqual(row.querySelectorAll('img').length, 0, 'the note is text');
  assert.strictEqual(doc.querySelectorAll('#workspaces .adm-problem').length, 1, 'there is only the one note');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'Admin \u203a Workspaces shows a refused Other workspaces value only as its note, as text, with no rows';
}

(async () => {
  console.log('PASS ' + await theButtonNamesTheCurrentInstance());
  console.log('PASS ' + await theMenuListsTheLinksWithTheirHosts());
  console.log('PASS ' + await downOpensTheMenuOnTheCheckedItem());
  console.log('PASS ' + await pickingAnotherInstanceGoesToItsAddress());
  console.log('PASS ' + await manageWorkspacesIsOnlyForAdmins());
  console.log('PASS ' + await hostileNamesAndAddressesStayText());
  console.log('PASS ' + await withNoLinksTheMenuHoldsTheCurrentEntry());
  console.log('PASS ' + await theRailKeepsTheTileAndTheSearchIcon());
  console.log('PASS ' + await aRefusedValueShowsOnlyItsNoteInAdmin());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
