const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const ADMIN_USER = 'nina.white@example.com';
const SEV = [['crit', 'Critical', 'critical'], ['warn', 'Warning', 'warning']];
const KEYS = SEV.map(([s]) => 'core.auto-resolve-hours-' + s);

let HELP = null;
function entry(key, label, extra) {
  return Object.assign({key, group: 'Cards', label, help: HELP || 'Help of ' + label, kind: 'int', live: true, value: '6', base: '6', default: '6', source: 'default', by: null, at: null, low: 0, high: 720}, extra || {});
}

async function boot(hash, historyRows, fromOld) {
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  let list = SEV.map(([s, , word], n) => entry(KEYS[n], 'Auto-resolve cleared ' + word + ' cards after (hours)', fromOld ? {value: '9', source: 'admin', by: ADMIN_USER, at: iso(5), base: '6', oldSwitch: 'core.auto-resolve-hours'} : n === 1 ? {value: '12', source: 'admin', by: ADMIN_USER, at: iso(5), base: '6'} : {}))
    .concat([entry('core.info-auto-close-days', 'Auto-resolve info cards still firing after (days)', fromOld ? {value: '0', base: '0', default: '3', high: 90, source: 'config', oldSwitch: 'core.auto-resolve-hours'} : {value: '3', base: '3', default: '3', high: 90}),
      entry('cards.archive-hours', 'Archive after (hours)', {value: '48', base: '48', default: '48', high: 720})]);
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/' + hash, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, opts) => {
        const p = String(path), method = (opts && opts.method) || 'GET', body = opts && opts.body ? JSON.parse(opts.body) : null;
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/admin')) {
          calls.push([method, p, body]);
          if (p.startsWith('/api/admin/history')) return ok(historyRows || []);
          if (p === '/api/admin/notices') return ok({items: []});
          if (p === '/api/admin/features') return ok({edition: 'community', items: []});
          if (p === '/api/admin/plugins') return ok({plugins: []});
          if (p.startsWith('/api/admin/teams')) return ok({routes: [], tags: [], hosts: []});
          if (p.startsWith('/api/admin/secrets')) return ok({fields: [], canEncrypt: true});
          if (p.startsWith('/api/admin/agent-sources')) return ok({agents: []});
          if (method === 'GET') return ok({items: list, rev: 3, refreshSeconds: 15});
          const key = body ? body.key : new URLSearchParams(p.split('?')[1]).get('key');
          const item = list.find((i) => i.key === key);
          const next = method === 'DELETE' ? Object.assign({}, item, {value: item.base, source: 'default', by: null, at: null})
            : Object.assign({}, item, {value: body.value, source: 'admin', by: ADMIN_USER, at: iso(0)});
          list = list.map((i) => i.key === key ? next : i);
          return ok({item: next, change: {}});
        }
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: ADMIN_USER, operator: true, admin: true, teams: [], tags: []});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors, calls};
}

const typeIn = (w, inp, value) => { inp.value = value; inp.dispatchEvent(new w.Event('input', {bubbles: true})); };

async function theTwoSeverityWindowsShareOneLabelledRowWithAnInputEach() {
  //given
  const {w, doc, errors} = await boot('#t=settings');

  //when
  const groups = doc.querySelectorAll('#admin [role="group"]');
  const group = groups[0];
  const label = group && doc.getElementById(group.getAttribute('aria-labelledby'));
  const inputs = group ? [].slice.call(group.querySelectorAll('.adm-row .adm-in')) : [];

  //then
  assert.strictEqual(groups.length, 1, 'one grouped row');
  assert.strictEqual(label.textContent, 'Auto-resolve cleared cards after (hours)');
  assert.deepStrictEqual(inputs.map((i) => i.closest('.adm-row').dataset.key), KEYS);
  assert.deepStrictEqual(inputs.map((i) => i.value), ['6', '12']);
  inputs.forEach((inp, n) => {
    const names = inp.getAttribute('aria-labelledby').split(' ').map((id) => doc.getElementById(id).textContent);
    assert.deepStrictEqual(names, ['Auto-resolve cleared cards after (hours)', SEV[n][1]], 'each input is named by the group label and its severity');
  });
  assert.ok(group.querySelectorAll('.adm-row .adm-save').length === 2, 'each input keeps its own Save');
  assert.ok(group.querySelectorAll('.adm-row .adm-reset').length === 1 && group.querySelector('.adm-row[data-key="' + KEYS[1] + '"] .adm-reset'), 'Reset only where a value was set here');
  assert.ok(group.querySelector('.adm-row[data-key="' + KEYS[1] + '"]').textContent.indexOf('Set by') >= 0, 'the source tag stays per input');
  assert.ok(!doc.querySelector('#admin .adm-row[data-key="core.info-auto-close-days"]').closest('[role="group"]'), 'the info still-firing setting is an ordinary row');
  assert.ok(doc.querySelector('#admin .adm-row[data-key="cards.archive-hours"]'), 'other settings are untouched');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the two auto-resolve severity windows are one labelled group row with an input, source tag and Save each');
}

async function savingOneSeverityWindowSendsOnlyItsKeyAndKeepsTheOtherEdits() {
  //given
  const {w, doc, errors, calls} = await boot('#t=settings');
  const row = (key) => doc.querySelector('#admin .adm-row[data-key="' + key + '"]');
  typeIn(w, row(KEYS[0]).querySelector('.adm-in'), '48');
  typeIn(w, row(KEYS[1]).querySelector('.adm-in'), '2');

  //when
  row(KEYS[0]).querySelector('.adm-save').click();
  await wait(80);

  //then
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'PUT').map((c) => c[2]), [{key: KEYS[0], value: '48'}]);
  assert.strictEqual(row(KEYS[0]).querySelector('.adm-msg').textContent, 'Saved, live within 15 s on every pod');
  assert.strictEqual(row(KEYS[1]).querySelector('.adm-in').value, '2', 'the other window keeps its unsaved edit');
  assert.ok(!row(KEYS[1]).querySelector('.adm-save').disabled);
  assert.ok(row(KEYS[0]).querySelector('.adm-reset'), 'the saved window now offers Reset');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS saving one severity window sends only its key and keeps the other unsaved edits');
}

async function resetOnOneSeverityWindowDeletesOnlyItsKey() {
  //given
  const {w, doc, errors, calls} = await boot('#t=settings');
  const row = (key) => doc.querySelector('#admin .adm-row[data-key="' + key + '"]');

  //when
  row(KEYS[1]).querySelector('.adm-reset').click();
  await wait(80);

  //then
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'DELETE').map((c) => c[1]), ['/api/admin/settings?key=' + encodeURIComponent(KEYS[1])]);
  assert.strictEqual(row(KEYS[1]).querySelector('.adm-in').value, '6');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS Reset on one severity window drops only its own value');
}

async function findingASeverityWordOrTheGroupLabelShowsTheWholeRow() {
  //given
  const {w, doc, errors} = await boot('#t=settings');
  const find = doc.getElementById('adm-find');

  //when
  find.value = 'cleared cards after';
  find.dispatchEvent(new w.Event('input', {bubbles: true}));
  const shown = doc.querySelectorAll('#admin .adm-multi .adm-row:not([hidden])').length;
  const groupShown = !doc.querySelector('#admin .adm-multi').hidden;
  find.value = 'archive';
  find.dispatchEvent(new w.Event('input', {bubbles: true}));
  const hiddenWhenOther = doc.querySelector('#admin .adm-multi').hidden;

  //then
  assert.strictEqual(shown, 2);
  assert.ok(groupShown);
  assert.ok(hiddenWhenOther, 'the group hides when no input matches');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS Find shows the whole severity row for its label and hides it for another search');
}

async function historyNamesTheSeverityWindowAndTheInfoDays() {
  //given
  const rows = [{id: 'a', seq: 2, key: KEYS[0], old: null, new: '48', by: ADMIN_USER, at: iso(2)}, {id: 'b', seq: 1, key: 'core.info-auto-close-days', old: '3', new: '5', by: ADMIN_USER, at: iso(3)}];
  const {w, doc, errors} = await boot('#t=changes', rows);

  //when
  const names = [].map.call(doc.querySelectorAll('#changes .adm-hist tbody tr td:nth-child(3)'), (td) => td.textContent);

  //then
  assert.deepStrictEqual(names, ['Auto-resolve cleared critical cards after (hours)', 'Auto-resolve info cards still firing after (days)']);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the History lists the severity window and the info days by their labels');
}

async function aValueSeededByTheOldHoursKeyShowsTheOldSettingTagAndResets() {
  //given
  const {w, doc, errors, calls} = await boot('#t=settings', [], true);
  const row = (key) => doc.querySelector('#admin .adm-row[data-key="' + key + '"]');
  const infoRow = doc.querySelector('#admin .adm-row[data-key="core.info-auto-close-days"]');

  //when
  const tags = KEYS.map((k) => [].map.call(row(k).querySelectorAll('.adm-m .tag'), (t) => t.textContent));
  const title = row(KEYS[0]).querySelector('.adm-m .tag').title;
  row(KEYS[0]).querySelector('.adm-reset').click();
  await wait(80);

  //then
  assert.deepStrictEqual(tags, [['old setting'], ['old setting']]);
  assert.deepStrictEqual([].map.call(infoRow.querySelectorAll('.adm-m .tag'), (t) => t.textContent), ['old setting']);
  assert.strictEqual(infoRow.querySelector('.adm-m .tag').title, 'Off because the old Auto-resolve cleared cards after setting (core.auto-resolve-hours) is 0; a value saved here wins over it');
  assert.ok(!infoRow.querySelector('.adm-reset'), 'the info row is not an Admin value, so it has no Reset');
  assert.ok(title.startsWith('From the old Auto-resolve cleared cards after setting (core.auto-resolve-hours), set by ') && title.endsWith('Reset or saving a value here clears it'), title);
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'DELETE').map((c) => c[1]), ['/api/admin/settings?key=' + encodeURIComponent(KEYS[0])]);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a severity window seeded by the old hours key shows the old setting tag and a working Reset');
}

async function aLongHelpLeavesNoShowMoreInTheSeverityRow() {
  //given
  HELP = 'Closes a card that stopped firing and that nobody acted on after N hours for this one severity; 0 turns it off. With Business days only on, only Monday to Friday counts toward the window.';
  const {w, doc, errors} = await boot('#t=settings');
  HELP = null;

  //when
  const group = doc.querySelector('#admin [role="group"]');

  //then
  assert.strictEqual(group.querySelectorAll('.adm-row .adm-help').length, 0, 'the severity rows say the help once, in the group head');
  assert.strictEqual(group.querySelectorAll('.adm-row .adm-more').length, 0, 'and keep no Show more button without a help to open');
  assert.ok(doc.querySelector('#admin .adm-row[data-key="cards.archive-hours"] .adm-more'), 'an ordinary row with a long help keeps its fold');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a long help leaves no Show more in the severity rows');
}

(async () => {
  await theTwoSeverityWindowsShareOneLabelledRowWithAnInputEach();
  await savingOneSeverityWindowSendsOnlyItsKeyAndKeepsTheOtherEdits();
  await resetOnOneSeverityWindowDeletesOnlyItsKey();
  await findingASeverityWordOrTheGroupLabelShowsTheWholeRow();
  await historyNamesTheSeverityWindowAndTheInfoDays();
  await aValueSeededByTheOldHoursKeyShowsTheOldSettingTagAndResets();
  await aLongHelpLeavesNoShowMoreInTheSeverityRow();
})().catch((e) => { console.error('FAIL', e); process.exit(1); });
