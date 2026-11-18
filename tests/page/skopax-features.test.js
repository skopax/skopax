const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const svg = (doc, html) => { const box = doc.createElement('div'); box.innerHTML = html; return box.firstChild.outerHTML; };
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const EVIL = '<img src=x onerror=alert(1)>"\'';
const FEATURE_SRC = fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'features.py'), 'utf8');
const TABLE = [...FEATURE_SRC.matchAll(/Feature\('([a-z-]+)', '([a-z.-]+)', '([^']+)',\s*'([^']*)', '([a-z]+)'(?:, edition='([a-z]+)')?/g)].map((m) => ({id: m[1], key: m[2], label: m[3], help: m[4], icon: m[5], edition: m[6] || 'community'}));
const NEEDS_SSO = 'Needs single sign-on (the SSO_* settings in the environment).';
const RESTART = 'Turning it on takes effect after a restart.';
const LOCKED = 'Hide "powered by Skopax" needs the Enterprise edition.';
const REFUSED = 'MCP server needs single sign-on first.';

function features() {
  return TABLE.map((f) => {
    const off = ['public-read', 'self-review', 'hide-attribution', 'mcp'].indexOf(f.id) >= 0;
    const locked = f.edition !== 'community';
    return {id: f.id, key: f.key, label: f.label, help: f.help, icon: f.icon, on: !off, stored: !off, locked, edition: f.edition,
      needs: f.id === 'mcp' ? [NEEDS_SSO] : [], restart: f.id === 'slack-listener' ? RESTART : null, source: 'default', reason: locked ? LOCKED : null};
  });
}

function settings() {
  const row = (key, label, extra) => Object.assign({key, group: 'Features', label, help: label + ' help', kind: 'bool', live: true, value: 'true', base: 'true', default: 'true', source: 'default', by: null, at: null}, extra || {});
  return [row('core.investigator', 'AI investigator'), row('core.release-settle', 'Release settle', {live: false}), row('core.assign-dm', 'Assignee message'), row('self-review.enabled', 'Self-review', {value: 'false', base: 'false', default: 'false'}),
    row('access.public-read', 'Read without login', {group: 'Access', value: 'false', base: 'false', default: 'false'}), row('slack.channel', 'Channel', {group: 'Slack', kind: 'text', value: '', base: '', default: ''})];
}

async function boot(opts) {
  opts = opts || {};
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const srv = {features: opts.features || features(), items: settings(), hold: null, gates: {}, stale: null, slow: null};
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/' + (opts.hash || '#t=features'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p0, o) => {
        const p = String(p0), method = (o && o.method) || 'GET', body = o && o.body ? JSON.parse(o.body) : null;
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        const bad = (status, detail) => ({ok: false, status, json: async () => ({detail})});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', userRole: 'admin', operator: true, admin: true, teams: [], tags: []});
        if (p.startsWith('/api/admin')) {
          calls.push([method, p, body]);
          if (p.startsWith('/api/admin/features/') && method === 'PUT') {
            if (srv.hold) await srv.hold;
            const id = decodeURIComponent(p.split('/').pop());
            if (srv.gates[id]) await srv.gates[id];
            if (id === 'mcp' && body.on) return bad(409, REFUSED);
            if (body.on && srv.features.find((f) => f.id === id).locked) return bad(409, LOCKED);
            srv.features = srv.features.map((f) => f.id === id ? Object.assign({}, f, {on: body.on && !f.locked, stored: body.on}) : f);
            if (id === 'investigator') srv.features = srv.features.map((f) => f.id === 'chat' ? Object.assign({}, f, {needs: body.on ? [] : ['Needs the AI investigator: it is off or not set up (Admin \u203a Features).']}) : f);
            return ok(srv.features.find((f) => f.id === id));
          }
          if (p === '/api/admin/features') {
            const items = JSON.parse(JSON.stringify(srv.stale || srv.features)), slow = srv.slow;
            srv.slow = null;
            if (slow) await slow;
            return ok({edition: 'community', items});
          }
          if (p === '/api/admin/settings' && method === 'GET') return ok({items: srv.items, rev: 1, refreshSeconds: 15});
          if (p === '/api/admin/settings' && method === 'PUT') {
            srv.items = srv.items.map((i) => i.key === body.key ? Object.assign({}, i, {value: body.value, source: 'admin', by: 'alice@example.com', at: iso(0)}) : i);
            return ok({item: srv.items.find((i) => i.key === body.key), change: {}});
          }
          if (p.startsWith('/api/admin/history')) return ok([]);
          if (p === '/api/admin/notices') return ok({items: []});
          if (p === '/api/admin/plugins') return ok({plugins: []});
          return bad(404, 'none');
        }
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
      };
    }});
  await wait(300);
  return {dom, w: dom.window, doc: dom.window.document, errors, calls, srv};
}

const card = (doc, id) => doc.querySelector('#features .feat[data-id="' + id + '"]');
const puts = (calls, id) => calls.filter((c) => c[0] === 'PUT' && c[1] === '/api/admin/features/' + id).map((c) => c[2]);
const gate = (srv, id) => { let open; srv.gates[id] = new Promise((r) => { open = r; }); return () => { delete srv.gates[id]; open(); }; };
const sw = (doc, id) => card(doc, id).querySelector('[role="switch"]');

async function theGridHasTenCardsInTableOrderWithNeedsTheRestartNoteAndTheLock() {
  //given
  const {dom, w, doc, errors} = await boot();
  const cards = [...doc.querySelectorAll('#features .feat')];

  //then
  assert.strictEqual(TABLE.length, 10, 'features.py lists ten features');
  assert.deepStrictEqual(cards.map((c) => c.dataset.id), TABLE.map((f) => f.id), 'ten cards in the order of the feature table');
  assert.deepStrictEqual(cards.map((c) => c.querySelector('.feat-l').textContent), TABLE.map((f) => f.label));
  assert.deepStrictEqual(cards.map((c) => c.querySelector('.feat-i svg').outerHTML), TABLE.map((f) => svg(doc, w.navIcon(f.icon))), 'each card has its icon tile');
  assert.deepStrictEqual(cards.map((c) => c.querySelector('.feat-h').textContent), TABLE.map((f) => f.help));
  assert.deepStrictEqual(cards.map((c) => c.querySelector('[role="switch"]').getAttribute('aria-checked')), TABLE.map((f) => String(['public-read', 'self-review', 'hide-attribution', 'mcp'].indexOf(f.id) < 0)));
  assert.deepStrictEqual(cards.map((c) => c.querySelector('[role="switch"]').getAttribute('aria-label')), TABLE.map((f) => f.label), 'every switch is named by its feature');
  assert.deepStrictEqual([...card(doc, 'mcp').querySelectorAll('.feat-need')].map((n) => n.textContent), [NEEDS_SSO], 'a missing need is a pill');
  assert.strictEqual(card(doc, 'investigator').querySelectorAll('.feat-need').length, 0);
  assert.strictEqual(card(doc, 'slack-listener').querySelector('.feat-restart').textContent, RESTART, 'the restart note shows');
  assert.ok(!card(doc, 'chat').querySelector('.feat-restart'));
  const lock = card(doc, 'hide-attribution');
  assert.strictEqual(lock.querySelector('.feat-ed').textContent, 'Enterprise', 'the locked card has the Enterprise badge');
  assert.ok(lock.classList.contains('locked'));
  assert.strictEqual(sw(doc, 'hide-attribution').getAttribute('aria-disabled'), 'true', 'and a disabled switch');
  assert.strictEqual(sw(doc, 'hide-attribution').title, LOCKED, 'whose title is the reason');
  assert.ok(cards.filter((c) => c.dataset.id !== 'hide-attribution').every((c) => !c.querySelector('.feat-ed') && !c.querySelector('[role="switch"]').hasAttribute('aria-disabled')), 'no other card is locked');
  assert.strictEqual(doc.getElementById('count-features').textContent, '6/10');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'Features shows the ten cards in table order with their icon, help, needs pills, restart note, and an Enterprise badge and disabled switch titled with the reason on the locked one';
}

async function aToggleSendsTheChoiceAndTheCountFollows() {
  //given
  const {dom, w, doc, errors, calls, srv} = await boot();
  let release;
  srv.hold = new Promise((r) => { release = r; });

  //when
  sw(doc, 'investigator').click();
  await wait(10);
  const during = [sw(doc, 'investigator').getAttribute('aria-checked'), sw(doc, 'investigator').getAttribute('aria-busy')];
  sw(doc, 'investigator').click();
  await wait(10);
  srv.hold = null;
  release();
  await wait(60);

  //then
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'PUT'), [['PUT', '/api/admin/features/investigator', {on: false}]], 'one PUT with the new state, and a second click while it runs sends nothing');
  assert.deepStrictEqual(during, ['false', 'true'], 'the switch moves at once and is busy until the answer');
  assert.strictEqual(sw(doc, 'investigator').getAttribute('aria-checked'), 'false');
  assert.ok(!sw(doc, 'investigator').hasAttribute('aria-busy'));
  assert.strictEqual(doc.getElementById('count-features').textContent, '5/10', 'the Features count follows');
  assert.deepStrictEqual([...card(doc, 'chat').querySelectorAll('.feat-need')].map((n) => n.textContent), ['Needs the AI investigator: it is off or not set up (Admin \u203a Features).'], 'the other cards show their new needs');
  assert.strictEqual(card(doc, 'investigator').querySelector('.feat-msg').textContent, 'Turned off');

  //when
  sw(doc, 'public-read').click();
  await wait(60);

  //then
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'PUT').map((c) => c.slice(1)).slice(-1), [['/api/admin/features/public-read', {on: true}]]);
  assert.strictEqual(doc.getElementById('count-features').textContent, '6/10');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a click sends PUT /api/admin/features/<id> with on, a second click waits, and the Features count and the other cards follow';
}

async function aRefusalPutsTheSwitchBackAndALockedOneSendsNothing() {
  //given
  const {dom, doc, errors, calls} = await boot();

  //when
  sw(doc, 'mcp').click();
  await wait(60);
  sw(doc, 'hide-attribution').click();
  await wait(40);

  //then
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'PUT').map((c) => c.slice(1)), [['/api/admin/features/mcp', {on: true}]], 'the locked switch sends nothing');
  assert.strictEqual(sw(doc, 'mcp').getAttribute('aria-checked'), 'false', 'a 409 puts the switch back');
  assert.strictEqual(card(doc, 'mcp').querySelector('.feat-msg').textContent, REFUSED, 'and shows the server text under the card');
  assert.ok(card(doc, 'mcp').querySelector('.feat-msg').classList.contains('bad'));
  assert.strictEqual(sw(doc, 'hide-attribution').getAttribute('aria-checked'), 'false');
  assert.strictEqual(doc.getElementById('count-features').textContent, '6/10', 'the count does not move');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a 409 puts the switch back and shows its text under the card; a locked switch sends nothing';
}

async function aLockedFeatureStoredOnCanOnlyBeSwitchedOff() {
  //given
  const stale = features().map((f) => f.id === 'hide-attribution' ? Object.assign({}, f, {stored: true}) : f);
  const {dom, doc, errors, calls} = await boot({features: stale});
  const first = {checked: sw(doc, 'hide-attribution').getAttribute('aria-checked'), disabled: sw(doc, 'hide-attribution').hasAttribute('aria-disabled'), title: sw(doc, 'hide-attribution').title,
    note: (card(doc, 'hide-attribution').querySelector('.feat-stale') || {}).textContent, badge: card(doc, 'hide-attribution').querySelector('.feat-ed').textContent};

  //when
  sw(doc, 'hide-attribution').setAttribute('aria-checked', 'false');
  sw(doc, 'hide-attribution').click();
  await wait(60);
  const after = {checked: sw(doc, 'hide-attribution').getAttribute('aria-checked'), disabled: sw(doc, 'hide-attribution').getAttribute('aria-disabled'), title: sw(doc, 'hide-attribution').title, note: !!card(doc, 'hide-attribution').querySelector('.feat-stale')};
  sw(doc, 'hide-attribution').click();
  await wait(40);

  //then
  assert.deepStrictEqual(first, {checked: 'true', disabled: false, title: LOCKED, note: 'Still saved as on from an earlier edition, where it has no effect: switch it off to clear it.', badge: 'Enterprise'}, 'a locked feature stored on shows the stored value, stays switchable and says why');
  assert.deepStrictEqual(puts(calls, 'hide-attribution'), [{on: false}], 'one click sends off, whatever the switch says, and nothing after that');
  assert.deepStrictEqual(after, {checked: 'false', disabled: 'true', title: LOCKED, note: false}, 'then it is locked and disabled like any locked switch');
  assert.strictEqual(doc.getElementById('count-features').textContent, '6/10', 'the count never counted it as on');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a locked feature stored on is painted from the stored value and can only be switched off: one literal {on: false}, then the switch is disabled';
}

async function noPagePathSendsOnForALockedFeature() {
  //given
  const {dom, w, doc, errors, calls} = await boot();

  //when
  sw(doc, 'hide-attribution').click();
  sw(doc, 'hide-attribution').dispatchEvent(new w.KeyboardEvent('keydown', {key: ' ', bubbles: true}));
  sw(doc, 'hide-attribution').removeAttribute('aria-disabled');
  sw(doc, 'hide-attribution').setAttribute('aria-checked', 'false');
  sw(doc, 'hide-attribution').click();
  await wait(40);
  const stored = await boot({features: features().map((f) => f.id === 'hide-attribution' ? Object.assign({}, f, {stored: true}) : f)});
  sw(stored.doc, 'hide-attribution').click();
  sw(stored.doc, 'hide-attribution').click();
  await wait(60);
  sw(stored.doc, 'hide-attribution').removeAttribute('aria-disabled');
  sw(stored.doc, 'hide-attribution').click();
  await wait(40);

  //then
  assert.deepStrictEqual(puts(calls, 'hide-attribution'), [], 'a locked switch stored off sends nothing, even with its attributes changed');
  assert.deepStrictEqual(puts(stored.calls, 'hide-attribution'), [{on: false}], 'stored on, it sends one off and never on');
  assert.ok(calls.concat(stored.calls).every((c) => !(c[0] === 'PUT' && /hide-attribution/.test(c[1]) && c[2] && c[2].on === true)), 'no {on: true} for the locked feature from any click');
  assert.deepStrictEqual(errors.concat(stored.errors), []);
  dom.window.close(); stored.dom.window.close();
  return 'no click, key or tampered attribute on a locked switch ever sends {on: true}';
}

async function aStaleReadAfterAToggleDoesNotFlipTheSwitchBack() {
  //given
  const {dom, w, doc, errors, srv} = await boot();
  srv.stale = features();

  //when
  sw(doc, 'investigator').click();
  await wait(60);
  const held = [sw(doc, 'investigator').getAttribute('aria-checked'), doc.getElementById('count-features').textContent, card(doc, 'investigator').querySelector('.feat-msg').textContent];
  doc.getElementById('tab-ai').click();
  await wait(30);
  doc.getElementById('tab-features').click();
  await wait(60);
  const reopened = sw(doc, 'investigator').getAttribute('aria-checked');
  srv.stale = null;
  doc.getElementById('tab-ai').click();
  await wait(30);
  doc.getElementById('tab-features').click();
  await wait(60);
  const agreed = JSON.parse(w.eval('JSON.stringify(Object.keys(FEAT_HELD))'));
  w.FEAT_HOLD_MS = 0;
  srv.stale = features();
  sw(doc, 'push').click();
  await wait(60);

  //then
  assert.deepStrictEqual(held, ['false', '5/10', 'Turned off'], 'a read from a pod that has not seen the change yet does not flip the switch back');
  assert.strictEqual(reopened, 'false', 'nor does reopening Features while that pod is behind');
  assert.deepStrictEqual(agreed, [], 'once a read agrees, the page drops what it held');
  assert.strictEqual(sw(doc, 'push').getAttribute('aria-checked'), 'true', 'after the hold time a read wins again, so a change made elsewhere shows');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'after a toggle the PUT answer wins for that card over a stale read, until a read agrees or the 15 s hold ends';
}

async function aCardStaysBusyWhileAnotherCardFinishes() {
  //given
  const {dom, doc, errors, calls, srv} = await boot();
  const openPush = gate(srv, 'push'), openChat = gate(srv, 'chat');

  //when
  sw(doc, 'push').click();
  sw(doc, 'chat').click();
  await wait(10);
  openPush();
  await wait(60);
  const during = [sw(doc, 'chat').getAttribute('aria-checked'), sw(doc, 'chat').getAttribute('aria-busy'), sw(doc, 'push').getAttribute('aria-checked')];
  sw(doc, 'chat').click();
  await wait(10);
  openChat();
  await wait(60);

  //then
  assert.deepStrictEqual(during, ['false', 'true', 'false'], 'the chat switch stays off and busy while the push answer repaints the grid');
  assert.deepStrictEqual(puts(calls, 'chat'), [{on: false}], 'two quick clicks give one PUT');
  assert.deepStrictEqual(puts(calls, 'push'), [{on: false}]);
  assert.strictEqual(sw(doc, 'chat').getAttribute('aria-checked'), 'false');
  assert.ok(!sw(doc, 'chat').hasAttribute('aria-busy'));
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a card stays busy, and a second click sends nothing, while another card\'s answer repaints the grid';
}

async function anAnswerThatLandsAfterALogoutHoldsAndSaysNothing() {
  //given
  const {dom, w, doc, errors, srv} = await boot();
  const openPush = gate(srv, 'push'), openMcp = gate(srv, 'mcp');
  sw(doc, 'push').click();
  sw(doc, 'mcp').click();
  await wait(10);

  //when
  w.setAdminAccess(false);
  w.setAdminAccess(true);
  openPush();
  openMcp();
  await wait(60);

  //then
  const left = JSON.parse(w.eval('JSON.stringify([Object.keys(FEAT_HELD), Object.keys(FEAT_SAID), Object.keys(FEAT_PENDING)])'));
  assert.deepStrictEqual(left, [[], [], []], 'a saved and a refused answer that end after a logout leave nothing held or said, even after a new login');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a toggle answer that ends after an admin logout holds nothing and says nothing, also after a new login';
}

async function aRepaintKeepsFocusOnTheSwitchThatHadIt() {
  //given
  const {dom, doc, errors, srv} = await boot();
  const openPush = gate(srv, 'push'), openChat = gate(srv, 'chat');

  //when
  sw(doc, 'push').focus();
  sw(doc, 'push').click();
  await wait(10);
  const pending = doc.activeElement === sw(doc, 'push');
  sw(doc, 'chat').focus();
  openPush();
  await wait(60);
  const other = doc.activeElement === sw(doc, 'chat');
  sw(doc, 'chat').click();
  await wait(10);
  openChat();
  await wait(60);
  const own = doc.activeElement === sw(doc, 'chat');
  doc.activeElement.blur();
  sw(doc, 'investigator').click();
  await wait(60);

  //then
  assert.deepStrictEqual([pending, other, own], [true, true, true], 'the focused switch keeps focus through its own repaints and through another card\'s answer');
  assert.strictEqual(doc.activeElement, doc.body, 'a repaint never moves focus into the grid when it was elsewhere');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a grid repaint keeps keyboard focus on the switch that had it, and takes none when focus was elsewhere';
}

async function anOlderReadThatEndsLastIsDropped() {
  //given
  const {dom, doc, errors, srv} = await boot();
  let release;
  srv.slow = new Promise((r) => { release = r; });

  //when
  sw(doc, 'investigator').click();
  await wait(40);
  sw(doc, 'investigator').click();
  await wait(40);
  release();
  await wait(40);

  //then
  assert.strictEqual(sw(doc, 'investigator').getAttribute('aria-checked'), 'true', 'the second toggle stands');
  assert.deepStrictEqual([...card(doc, 'chat').querySelectorAll('.feat-need')].map((n) => n.textContent), [], 'the read sent after the first toggle, ending last, does not bring back the need it saw');
  assert.strictEqual(doc.getElementById('count-features').textContent, '6/10');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a read that was sent earlier but ends later never repaints the grid';
}

async function otherSwitchesFoldTheRestOfTheFeaturesGroup() {
  //given
  const {dom, w, doc, errors, calls} = await boot();
  const fold = doc.querySelector('#feat-other details.feat-other');

  //then
  assert.ok(fold && !fold.open, 'an Other switches fold, folded');
  assert.strictEqual(fold.querySelector('summary h3').textContent, 'Other switches');
  assert.deepStrictEqual([...fold.querySelectorAll('.adm-row')].map((r) => r.dataset.key), ['core.release-settle', 'core.assign-dm'], 'the Features-group settings that are not a feature card');
  assert.ok(fold.querySelector('.adm-row[data-key="core.release-settle"]').textContent.includes('after restart'), 'with their after restart label');

  //when
  fold.open = true;
  await wait(20);
  const row = fold.querySelector('.adm-row[data-key="core.assign-dm"]');
  row.querySelector('select').value = 'false';
  row.querySelector('select').dispatchEvent(new w.Event('input'));
  row.querySelector('.adm-save').click();
  await wait(60);

  //then
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'PUT').map((c) => c[2]), [{key: 'core.assign-dm', value: 'false'}]);
  assert.strictEqual(doc.querySelector('#feat-other .adm-row[data-key="core.assign-dm"] .adm-msg').textContent, 'Saved, live within 15 s on every pod');
  assert.ok(doc.querySelector('#feat-other details.feat-other').open, 'the fold stays open after a save');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'below the grid an Other switches fold holds the rest of the Features group as setting rows that save as on General';
}

async function hostileFeatureTextsAreText() {
  //given
  const evil = features().map((f) => f.id === 'push' ? Object.assign({}, f, {label: EVIL, help: EVIL, needs: [EVIL], restart: EVIL}) : f);
  const {dom, doc, errors} = await boot({features: evil});
  const push = card(doc, 'push');

  //then
  assert.strictEqual(push.querySelectorAll('img').length, 0, 'no markup from the server');
  assert.deepStrictEqual([push.querySelector('.feat-l').textContent, push.querySelector('.feat-h').textContent, push.querySelector('.feat-need').textContent, push.querySelector('.feat-restart').textContent], [EVIL, EVIL, EVIL, EVIL]);
  assert.strictEqual(sw(doc, 'push').getAttribute('aria-label'), EVIL);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a hostile label, help, need or restart note renders as text';
}

(async () => {
  console.log('PASS ' + await theGridHasTenCardsInTableOrderWithNeedsTheRestartNoteAndTheLock());
  console.log('PASS ' + await aToggleSendsTheChoiceAndTheCountFollows());
  console.log('PASS ' + await aRefusalPutsTheSwitchBackAndALockedOneSendsNothing());
  console.log('PASS ' + await aLockedFeatureStoredOnCanOnlyBeSwitchedOff());
  console.log('PASS ' + await noPagePathSendsOnForALockedFeature());
  console.log('PASS ' + await aStaleReadAfterAToggleDoesNotFlipTheSwitchBack());
  console.log('PASS ' + await aCardStaysBusyWhileAnotherCardFinishes());
  console.log('PASS ' + await anAnswerThatLandsAfterALogoutHoldsAndSaysNothing());
  console.log('PASS ' + await aRepaintKeepsFocusOnTheSwitchThatHadIt());
  console.log('PASS ' + await anOlderReadThatEndsLastIsDropped());
  console.log('PASS ' + await otherSwitchesFoldTheRestOfTheFeaturesGroup());
  console.log('PASS ' + await hostileFeatureTextsAreText());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
