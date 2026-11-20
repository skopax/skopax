const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const NINA = 'nina.white@example.com', ALICE = 'alice@example.com', BOB = 'bob@example.com', CHLOE = 'chloe.martin@example.com', ERIN = 'erin.vale@example.org';
const TAGS = [{id: 'developer', label: 'Developer'}, {id: 'data', label: 'Data'}];

function board() {
  return {rev: 1, updatedAt: new Date().toISOString(), sweptIso: new Date().toISOString(), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: new Date().toISOString()},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

function rulesItem(tag, label) {
  return {key: 'tags.' + tag + '.rules', group: 'Card tags', card: 'Tag rules', order: 9000, label: label, help: 'Cards tagged ' + label + ' by rule', kind: 'tagrules', live: true, value: '', base: '', default: '',
    source: 'default', by: null, at: null, limit: 500, pluginTerms: []};
}

async function boot(opts) {
  opts = opts || {};
  const errors = [], calls = [], asked = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(board()));
  const owners = {developer: [{email: ALICE, name: 'Alice Moss'}], data: [{email: ERIN, name: 'erin.vale'}]};
  const auto = {developer: true, data: true}, ready = {developer: true, data: false};
  const view = (id) => Object.assign({}, TAGS.filter((t) => t.id === id)[0], {responsibles: owners[id], autoAssign: auto[id], autoReady: ready[id]});
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=settings', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, o) => {
        const p = String(path), method = (o && o.method) || 'GET';
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        const m = /^\/api\/admin\/tags\/([a-z-]+)\/responsibles$/.exec(p);
        if (m && method === 'PUT') {
          const body = JSON.parse(o.body);
          calls.push(body.emails);
          owners[m[1]] = body.emails.map((e) => ({email: e, name: e.split('@')[0]}));
          return ok(view(m[1]));
        }
        const a = /^\/api\/admin\/tags\/([a-z-]+)\/auto-assign$/.exec(p);
        if (a && method === 'PUT') {
          const on = JSON.parse(o.body).on;
          calls.push(['auto', a[1], on]);
          auto[a[1]] = on;
          return ok(view(a[1]));
        }
        if (p.startsWith('/api/admin/people')) {
          const q = new URL(p, 'https://x').searchParams.get('q');
          asked.push(q);
          if (opts.slow) await wait(opts.slow(q));
          return ok(opts.people ? opts.people(q) : {people: [], slack: 'off'});
        }
        if (p === '/api/admin/tags') return ok({tags: TAGS.map((t) => view(t.id)), canEdit: true,
          users: [{email: ALICE, name: 'Alice Moss', disabled: false}, {email: BOB, name: 'Bob Reed', disabled: false}, {email: NINA, name: 'Ni\u00f1a White', disabled: false}, {email: 'gone@example.com', name: 'Bob Gone', disabled: true}],
          reportAt: '10:00', emailScopeMissing: false});
        if (p.startsWith('/api/admin/history')) return ok([]);
        if (p.startsWith('/api/admin/teams')) return ok({routes: [], tags: [], hosts: []});
        if (p.startsWith('/api/admin/settings')) return ok({items: TAGS.map((t) => rulesItem(t.id, t.label)), rev: 1, refreshSeconds: 15});
        if (p.startsWith('/api/admin/notices')) return ok({items: []});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: NINA, operator: true, admin: true, teams: [], tags: TAGS});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(450);
  const doc = dom.window.document;
  const box = (tag) => doc.querySelector('#admin .adm-row[data-key="tags.' + (tag || 'developer') + '.rules"] .adm-owners');
  const pick = (tag) => box(tag).querySelector('.adm-owner-pick');
  const type = (text, tag) => { const i = pick(tag); i.focus(); i.value = text; i.dispatchEvent(new dom.window.Event('input', {bubbles: true})); };
  const key = (k, tag) => pick(tag).dispatchEvent(new dom.window.KeyboardEvent('keydown', {key: k, bubbles: true, cancelable: true}));
  const rows = (tag) => [...box(tag).querySelectorAll('.adm-owner-opt')].filter((o) => !o.closest('[hidden]'));
  return {w: dom.window, doc, errors, calls, asked, box, pick, type, key, rows};
}

async function theInputIsAnAriaComboboxThatFiltersByAnyPartOfNameOrAddressIgnoringCaseAndAccents() {
  //given
  const {w, errors, box, pick, type, rows} = await boot();
  const list = box().querySelector('[role="listbox"]');
  const closed = {role: pick().getAttribute('role'), auto: pick().getAttribute('aria-autocomplete'), expanded: pick().getAttribute('aria-expanded'), controls: pick().getAttribute('aria-controls') === list.id, hidden: list.hidden, kind: pick().type};

  //when
  type('NINA');
  const byName = rows().map((o) => o.dataset.email);
  const shown = rows().map((o) => [o.querySelector('.adm-owner-av').textContent, o.querySelector('.adm-owner-on').textContent, o.querySelector('.adm-owner-oe').textContent, o.querySelector('.adm-owner-src').textContent, o.getAttribute('role')]);
  const open = {expanded: pick().getAttribute('aria-expanded'), hidden: list.hidden, count: box().querySelector('.adm-owner-live').textContent};
  type('eed');
  const middle = rows().map((o) => o.dataset.email);
  type('EXAMPLE.ORG');
  const elsewhere = rows().map((o) => [o.dataset.email, o.querySelector('.adm-owner-src').textContent]);
  type('bob');
  const noDisabled = rows().map((o) => o.dataset.email);

  //then
  assert.deepStrictEqual(closed, {role: 'combobox', auto: 'list', expanded: 'false', controls: true, hidden: true, kind: 'text'});
  assert.deepStrictEqual(byName, [NINA], 'the accent in the name does not stop the match');
  assert.deepStrictEqual(shown, [['NW', 'Ni\u00f1a White', NINA, 'Skopax', 'option']]);
  assert.deepStrictEqual(open, {expanded: 'true', hidden: false, count: 'Suggestions: 1'});
  assert.deepStrictEqual(middle, [BOB], 'a part inside a word matches');
  assert.deepStrictEqual(elsewhere, [[ERIN, 'Responsible elsewhere']], 'a responsible of another tag is offered');
  assert.deepStrictEqual(noDisabled, [BOB], 'a disabled user is never offered');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the responsible field is an ARIA combobox that filters Skopax users and other tags\u2019 responsibles by any part of the name or e-mail, ignoring case and accents, with avatar, name, e-mail and source on each row';
}

async function arrowKeysMoveEnterPicksEscapeClosesAndAClickPicks() {
  //given
  const {w, errors, calls, box, pick, type, key, rows} = await boot();

  //when
  type('e');
  const all = rows().map((o) => o.dataset.email), ids = rows().map((o) => o.id);
  key('ArrowDown');
  const firstActive = pick().getAttribute('aria-activedescendant');
  key('ArrowDown');
  const second = {active: pick().getAttribute('aria-activedescendant'), selected: rows().map((o) => o.getAttribute('aria-selected'))};
  key('ArrowUp');
  key('ArrowUp');
  const wrapped = pick().getAttribute('aria-activedescendant');
  key('Escape');
  const escaped = {expanded: pick().getAttribute('aria-expanded'), active: pick().getAttribute('aria-activedescendant'), value: pick().value};
  type('bob');
  key('ArrowDown');
  key('Enter');
  await wait(80);
  const afterEnter = calls.slice();
  type('nin', 'data');
  const opt = rows('data')[0];
  const down = new w.MouseEvent('mousedown', {bubbles: true, cancelable: true});
  opt.dispatchEvent(down);
  opt.click();
  await wait(80);

  //then
  assert.ok(all.length >= 3 && all.indexOf(ALICE) < 0, 'the person already chosen is left out');
  assert.strictEqual(firstActive, ids[0]);
  assert.ok(second.active === ids[1] && second.selected.filter((s) => s === 'true').length === 1, 'one option is highlighted');
  assert.strictEqual(wrapped, ids[ids.length - 1], 'up from the first wraps to the last');
  assert.deepStrictEqual(escaped, {expanded: 'false', active: null, value: 'e'});
  assert.deepStrictEqual(afterEnter, [[ALICE, BOB]]);
  assert.strictEqual(down.defaultPrevented, true, 'pressing on an option keeps the focus in the field');
  assert.deepStrictEqual(calls[1], [ERIN, NINA]);
  assert.strictEqual(box('data').querySelector('.adm-owner-msg').textContent, 'Added');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'arrow keys move the highlight with aria-activedescendant and wrap, Enter picks the highlighted person, Escape closes and a click picks';
}

async function enterAddsATypedAddressAndAValidUnknownOneOffersANoMatchRow() {
  //given
  const {w, errors, calls, box, type, key, rows} = await boot();

  //when
  type('zed.new@example.com');
  const offer = rows().map((o) => [o.dataset.email, o.textContent]);
  type('zed.new@');
  const partial = rows().length;
  type('Zed.New@Example.com ');
  key('Enter');
  await wait(80);

  //then
  assert.deepStrictEqual(offer, [['zed.new@example.com', 'No match: press Enter to add zed.new@example.com']]);
  assert.strictEqual(partial, 0, 'no add row before the text is an e-mail');
  assert.deepStrictEqual(calls, [[ALICE, 'zed.new@example.com']]);
  assert.strictEqual(box().querySelector('.adm-owner-msg').textContent, 'Added');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a typed e-mail nobody matches shows a No match row and Enter adds it, lower-cased';
}

async function slackPeopleMergeInAfterTheDebounceAndStaleAnswersAreDropped() {
  //given
  const people = (q) => ({people: q.startsWith('chl') ? [{email: CHLOE, name: 'Chlo\u00e9 Martin', source: 'slack'}, {email: ALICE, name: 'Alice Moss', source: 'skopax'}] : [{email: 'stale@example.com', name: 'Chl Stale', source: 'slack'}], slack: 'ok'});
  const {w, errors, asked, box, type, rows} = await boot({people: people, slow: (q) => q === 'ch' ? 300 : 0});

  //when
  type('c');
  await wait(260);
  const oneLetter = asked.slice();
  type('ch');
  await wait(230);
  type('chl');
  const instant = rows().map((o) => o.dataset.email);
  await wait(60);
  const beforeDebounce = asked.slice();
  await wait(250);
  const merged = rows().map((o) => [o.dataset.email, o.querySelector('.adm-owner-src').textContent]);
  await wait(300);
  const later = rows().map((o) => o.dataset.email);

  //then
  assert.deepStrictEqual(oneLetter, [], 'one letter asks the server nothing');
  assert.deepStrictEqual(instant, [], 'the local list filters at once');
  assert.deepStrictEqual(beforeDebounce, ['ch']);
  assert.deepStrictEqual(merged, [[CHLOE, 'Slack']], 'Slack people merge in, the chosen one stays out');
  assert.deepStrictEqual(later, [CHLOE], 'the slow answer for an older text is dropped');
  assert.strictEqual(box().querySelector('.adm-owner-slack').hidden, true);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'typing waits about 200 ms before asking the server, Slack members merge into the list when they arrive and an answer for older text is dropped';
}

async function anUnavailableSlackShowsAQuietNoteAndLoadingAsksOnceMore() {
  //given
  let state = 'loading';
  const {w, errors, asked, box, type} = await boot({people: () => ({people: [], slack: state})});

  //when
  type('zz');
  await wait(260);
  const loading = {asked: asked.length, note: box().querySelector('.adm-owner-slack').hidden};
  state = 'unavailable';
  await wait(1700);
  const note = box().querySelector('.adm-owner-slack');

  //then
  assert.deepStrictEqual(loading, {asked: 1, note: true});
  assert.strictEqual(asked.length, 2, 'a loading answer is asked once more');
  assert.deepStrictEqual([note.hidden, note.textContent], [false, 'Slack names unavailable']);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'while Slack members load the field asks once more, and when Slack cannot be read a quiet Slack names unavailable note shows';
}

async function eachTagHasAnAutoAssignSwitchOnByDefaultAndAQuietNoteWithoutAnAccount() {
  //given
  const {w, errors, calls, box} = await boot();
  const sw = (tag) => box(tag).querySelector('.adm-owner-auto input[type="checkbox"]');
  const note = (tag) => box(tag).querySelector('.adm-owner-auto-note');
  const first = {dev: sw('developer').checked, data: sw('data').checked, label: box('developer').querySelector('.adm-owner-auto').textContent.trim(),
    devNote: !!note('developer'), dataNote: note('data') && note('data').textContent};

  //when
  sw('developer').click();
  await wait(80);
  const after = {checked: sw('developer').checked, said: box('developer').querySelector('.adm-owner-msg').textContent, focus: w.document.activeElement === sw('developer')};

  //then
  assert.deepStrictEqual(first, {dev: true, data: true, label: 'Auto-assign new cards', devNote: false, dataNote: 'Auto-assign needs a responsible with a Skopax account'});
  assert.deepStrictEqual(calls, [['auto', 'developer', false]]);
  assert.deepStrictEqual(after, {checked: false, said: 'Auto-assign off', focus: true});
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'each tag row has an Auto-assign new cards switch, on by default, that saves at once, and a tag whose responsibles have no Skopax account says so quietly';
}

async function addTakesTheHighlightedPersonAndPlainTextIsNeverSaved() {
  //given
  const {w, errors, calls, box, pick, type, key} = await boot();

  //when
  type('reed');
  key('ArrowDown');
  const go = box().querySelector('.adm-owner-go');
  const down = new w.MouseEvent('mousedown', {bubbles: true, cancelable: true});
  go.dispatchEvent(down);
  pick().dispatchEvent(new w.FocusEvent('blur'));
  go.click();
  await wait(80);
  const added = calls.slice();
  type('reed', 'data');
  key('ArrowDown', 'data');
  pick('data').dispatchEvent(new w.FocusEvent('blur'));
  pick('data').dispatchEvent(new w.FocusEvent('focus'));
  key('Escape', 'data');
  pick('data').value = 'just a name';
  key('Enter', 'data');
  await wait(80);
  const afterFocus = calls.length;
  type('just a name', 'data');
  key('Enter', 'data');
  await wait(80);
  const hint = box('data').querySelector('.adm-owner-msg').textContent;
  box('data').querySelector('.adm-owner-go').click();
  await wait(80);

  //then
  assert.deepStrictEqual(added, [[ALICE, BOB]], 'Add after the arrow keys adds the highlighted person, not the typed text');
  assert.strictEqual(afterFocus, 1, 'coming back to the field forgets the person highlighted before');
  assert.strictEqual(down.defaultPrevented, true, 'pressing Add keeps the focus in the field');
  assert.deepStrictEqual(calls, [[ALICE, BOB]], 'text that is no e-mail is never saved');
  assert.strictEqual(hint, 'Pick a suggestion or type a full e-mail');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'Add after arrowing to a suggestion adds that person, and Enter or Add with text that is no e-mail only shows a hint';
}

function cssRules(list) {
  return [...list].flatMap((r) => r.cssRules && !r.selectorText ? cssRules(r.cssRules) : r.selectorText ? [r] : []);
}

async function onAPhoneTheListSitsBelowTheFieldWithTallRows() {
  //given
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', '{}'));
  const all = cssRules(dom.window.document.querySelector('style').sheet.cssRules);
  const value = (sel, prop) => all.filter((r) => r.selectorText.split(',').map((s) => s.trim()).indexOf(sel) >= 0).map((r) => r.style.getPropertyValue(prop)).filter(Boolean).slice(-1)[0];

  //then
  assert.deepStrictEqual([value('.adm-owner-opts', 'position'), value('.adm-owner-opts', 'width'), value('.adm-owner-opts', 'max-width'), value('.adm-owner-combo', 'flex-direction')], ['static', '100%', '100%', 'column'], 'the list flows under the field, so a card that clips overflow never hides it');
  assert.strictEqual(value('.adm-owner-opt', 'min-height'), '44px');
  assert.strictEqual(value('.adm-owner-combo', 'min-width'), '0px');
  assert.strictEqual(value('.adm-owner-oe', 'text-overflow'), 'ellipsis');
  dom.window.close();
  return 'on a phone the suggestion list sits under the field at its width, rows are 44 px tall and long addresses are cut';
}

(async () => {
  console.log('PASS ' + await theInputIsAnAriaComboboxThatFiltersByAnyPartOfNameOrAddressIgnoringCaseAndAccents());
  console.log('PASS ' + await arrowKeysMoveEnterPicksEscapeClosesAndAClickPicks());
  console.log('PASS ' + await enterAddsATypedAddressAndAValidUnknownOneOffersANoMatchRow());
  console.log('PASS ' + await slackPeopleMergeInAfterTheDebounceAndStaleAnswersAreDropped());
  console.log('PASS ' + await anUnavailableSlackShowsAQuietNoteAndLoadingAsksOnceMore());
  console.log('PASS ' + await onAPhoneTheListSitsBelowTheFieldWithTallRows());
  console.log('PASS ' + await eachTagHasAnAutoAssignSwitchOnByDefaultAndAQuietNoteWithoutAnAccount());
  console.log('PASS ' + await addTakesTheHighlightedPersonAndPlainTextIsNeverSaved());
})().catch((e) => { console.error('FAIL', e); process.exit(1); });
