const assert = require('assert');
const {boot, settings, uses, wait, MODELS, GROUPS} = require('./skopax-ai-kit');

const TEMPLATE = process.argv[2];
const SEES = 'The guard model reads questions, answers and the tool results behind them. If it runs on another provider, that provider receives this text.';
const NEEDS = 'Pick a guard model first; the check stays off until one is set';
const NO_MODEL = 'A guard check is on but no guard model is set, so the check is off; pick a guard model';
const SWITCHES = ['guard.check-questions', 'guard.check-answers'];

function guardItems(model, on) {
  const base = {group: 'Investigator', live: true, source: 'default', by: null, at: null};
  return [
    Object.assign({}, base, {key: 'investigate.guard-model', label: 'Guard model', help: 'A separate model that checks questions and answers', kind: 'choice', value: model || '', base: '', default: '',
      choices: [''].concat(MODELS), choiceGroups: GROUPS, choiceLabels: {'': 'None'}}),
  ].concat(SWITCHES.map((key, n) => Object.assign({}, base, {key, label: n ? 'Check answers' : 'Check questions', help: 'Guard switch', kind: 'bool', value: (on || {})[key] ? 'true' : 'false',
    base: 'false', default: 'false'}, model || (on || {})[key] ? {} : {off: NEEDS})));
}

function modeItems(mode, onHold) {
  const base = {group: 'Investigator', live: true, source: 'admin', by: 'root@example.com', at: null, kind: 'choice'};
  return [
    Object.assign({}, base, {key: 'guard.mode', label: 'Guard mode', help: 'Enforce or watch only', value: mode, base: 'enforce', default: 'enforce', choices: ['enforce', 'watch'],
      choiceLabels: {enforce: 'Enforce', watch: 'Watch only'}}),
    Object.assign({}, base, {key: 'guard.on-hold', label: 'On hold', help: 'Fix or review', value: onHold, base: 'fix', default: 'fix', choices: ['fix', 'review'],
      choiceLabels: {fix: 'Fix automatically', review: 'Send to an admin'}}),
  ];
}

function guardRow(model, extra) {
  return Object.assign({use: 'guard', label: 'Guard', setting: 'investigate.guard-model', model: model || '', provider: model ? 'openai' : 'anthropic', company: model ? 'OpenAI' : 'Anthropic',
    price: model ? {input: 0.4, output: 1.6} : null, avgCost7d: model ? 0.0021 : null, problem: null, fit: 'ok', advisor: null}, extra || {});
}

async function start(model, on, extra, opts) {
  const items = settings().concat(guardItems(model, on)).concat((opts && opts.modes) || []);
  return boot(TEMPLATE, Object.assign({items, rows: uses(items).concat([guardRow(model, extra)])}, opts || {}));
}

const rowOf = (doc) => doc.querySelector('#ai-rows .aiu-row[data-use="guard"]');
const switchOf = (doc, key) => rowOf(doc).querySelector('.aiu-guard .adm-row[data-key="' + key + '"] input.adm-sw');

async function theGuardRowIsOffByDefaultAndItsSwitchesWaitForAModel() {
  //given
  const {dom, doc, errors} = await start('', {});

  //when
  const row = rowOf(doc), sel = doc.getElementById('aiu-guard-model');
  const notes = (sel.getAttribute('aria-describedby') || '').split(' ').map((id) => doc.getElementById(id)).filter(Boolean).map((n) => n.textContent);
  const offs = SWITCHES.map((k) => row.querySelector('.adm-row[data-key="' + k + '"] .adm-off-why'));

  //then
  assert.ok(row, 'Who does what has a Guard row');
  assert.strictEqual(row.querySelector('.aiu-use b').textContent, 'Guard');
  assert.deepStrictEqual([sel.value, sel.options[sel.selectedIndex].textContent], ['', 'None'], 'the model is None by default');
  assert.ok(!row.querySelector('.aiu-prov'), 'an unset guard names no provider');
  assert.deepStrictEqual([row.querySelector('.aiu-cost .aiu-price').textContent, row.querySelector('.aiu-co .aiu-v').textContent], ['Guard off', 'Nothing is sent'], 'nothing is priced or sent while it is off');
  assert.strictEqual(row.querySelector('.aiu-effort .aiu-v').textContent, '\u2013', 'the guard has no effort setting');
  assert.deepStrictEqual(SWITCHES.map((k) => [switchOf(doc, k).checked, switchOf(doc, k).disabled]), [[false, true], [false, true]], 'both checks are off and greyed out without a model');
  assert.deepStrictEqual(offs.map((n) => n && n.textContent), [NEEDS, NEEDS], 'each says why');
  assert.ok(notes.indexOf(SEES) >= 0, 'the note on what the guard sees is tied to the model select');
  assert.strictEqual(row.querySelector('.aiu-guard-sees').textContent, SEES);
  assert.ok(!row.classList.contains('aiu-bad') && !row.querySelector('.aiu-problem'), 'an unset guard is not a problem');
  assert.deepStrictEqual([...doc.querySelectorAll('#ai-rows .ai-grp:not(.aiu) .adm-row')].map((r) => r.dataset.key).filter((k) => /guard/.test(k)), [], 'the guard keys are not listed again below the table');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the Guard row is off by default: model None, no provider, nothing sent, both switches greyed out with the reason, and the note on what the guard sees';
}

async function pickingAModelOpensTheSwitchesAndClearingItClosesThemAgain() {
  //given
  const {dom, w, doc, errors, calls, srv} = await start('', {});
  const sel = doc.getElementById('aiu-guard-model');

  //when
  sel.value = 'gpt-6.2-sol';
  sel.dispatchEvent(new w.Event('change', {bubbles: true}));
  srv.rows = srv.rows.map((r) => r.use === 'guard' ? guardRow('gpt-6.2-sol') : r);
  rowOf(doc).querySelector('.aiu-save').click();
  await wait(200);
  const opened = SWITCHES.map((k) => switchOf(doc, k).disabled);
  const tag = rowOf(doc).querySelector('.aiu-prov').textContent;
  const answers = switchOf(doc, 'guard.check-answers');
  answers.checked = true;
  answers.dispatchEvent(new w.Event('change', {bubbles: true}));
  answers.closest('.adm-row').querySelector('.adm-save').click();
  await wait(200);
  const saved = switchOf(doc, 'guard.check-answers').checked;
  srv.rows = srv.rows.map((r) => r.use === 'guard' ? guardRow('') : r);
  rowOf(doc).querySelector('.aiu-meta .adm-reset').click();
  await wait(200);
  const row = rowOf(doc);

  //then
  assert.deepStrictEqual(opened, [false, false], 'with a model both checks can be switched');
  assert.strictEqual(tag, 'OpenAI', 'and the row names the provider of the guard model');
  assert.deepStrictEqual(calls.filter((c) => c[0] !== 'GET').map((c) => [c[0], c[1], c[2]]), [['PUT', '/api/admin/settings', {key: 'investigate.guard-model', value: 'gpt-6.2-sol'}],
    ['PUT', '/api/admin/settings', {key: 'guard.check-answers', value: 'true'}], ['DELETE', '/api/admin/settings?key=investigate.guard-model', undefined]], 'each change is an Admin setting write');
  assert.ok(saved, 'the switch stays on after its save');
  assert.deepStrictEqual(SWITCHES.map((k) => switchOf(doc, k).disabled), [true, false], 'after the model is cleared the off check is greyed out again, the on one can still be turned off');
  assert.strictEqual(row.querySelector('.adm-row[data-key="guard.check-answers"] .adm-problem').textContent, NO_MODEL, 'the on check says it does nothing without a model');
  assert.ok(row.classList.contains('aiu-bad'), 'and the row is marked');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'picking a guard model opens both switches, a switch saves like any setting, and clearing the model greys the off switch and flags the on one';
}

async function pickingNoneAndSavingTurnsTheGuardOff() {
  //given
  const {dom, w, doc, errors, calls, srv} = await start('gpt-6.2-sol', {});
  const sel = doc.getElementById('aiu-guard-model');

  //when
  sel.value = '';
  sel.dispatchEvent(new w.Event('change', {bubbles: true}));
  srv.rows = srv.rows.map((r) => r.use === 'guard' ? guardRow('') : r);
  rowOf(doc).querySelector('.aiu-save').click();
  await wait(200);

  //then
  assert.deepStrictEqual(calls.filter((c) => c[0] !== 'GET').map((c) => [c[0], c[1], c[2]]), [['PUT', '/api/admin/settings', {key: 'investigate.guard-model', value: ''}]], 'None is saved as an empty model');
  assert.deepStrictEqual(SWITCHES.map((k) => switchOf(doc, k).disabled), [true, true], 'and both checks are greyed out again');
  assert.strictEqual(rowOf(doc).querySelector('.aiu-cost .aiu-price').textContent, 'Guard off');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'picking None and saving turns the guard off and greys out both checks';
}

async function aGuardProblemFromTheServerShowsOnTheRow() {
  //given
  const problem = 'OpenAI has no working key or connection for the guard model gpt-6.2-sol: questions are refused and answers held until you fix it';
  const {dom, doc, errors} = await start('gpt-6.2-sol', {'guard.check-questions': true}, {problem});

  //when
  const row = rowOf(doc), note = row.querySelector('.aiu-problem');

  //then
  assert.strictEqual(note.textContent, problem);
  assert.ok(row.classList.contains('aiu-bad') && note.closest('.aiu-model'), 'the problem is a red line in the model cell');
  assert.deepStrictEqual(SWITCHES.map((k) => [switchOf(doc, k).checked, switchOf(doc, k).disabled]), [[true, false], [false, false]]);
  assert.deepStrictEqual([row.querySelector('.aiu-cost .aiu-price').textContent, row.querySelector('.aiu-co .aiu-v').textContent], ['$0.40 / $1.60 per 1M tokens', 'OpenAI']);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a guard problem the server reports, such as a missing key, is a red line on the Guard row';
}

async function theGuardTextsAreTranslated() {
  //given
  const packs = await Promise.all(['de', 'es', 'fr', 'tr'].map((lang) => start('', {}, null, {lang})));

  //when
  const said = packs.map((p) => [rowOf(p.doc).querySelector('.aiu-guard-sees').textContent, rowOf(p.doc).querySelector('.aiu-cost .aiu-price').textContent, rowOf(p.doc).querySelector('.aiu-co .aiu-v').textContent,
    rowOf(p.doc).querySelector('.adm-off-why').textContent]);

  //then
  said.forEach((s) => assert.ok(s[0] !== SEES && s[1] !== 'Guard off' && s[2] !== 'Nothing is sent' && s[3] !== NEEDS, 'every pack translates the guard texts: ' + s.join(' | ')));
  packs.forEach((p) => { assert.deepStrictEqual(p.errors, []); p.dom.window.close(); });
  return 'German, Spanish, French and Turkish translate the guard note, Guard off, Nothing is sent and the switch reason';
}

const WATCH = 'Watch only: the guard records what it would have done but never refuses, hides or holds anything, so it protects nothing. Switch back to Enforce when you have seen enough.';

async function theGuardRowOffersModeAndOnHoldAndWarnsInWatchOnly() {
  //given
  const enforce = await start(MODELS[0], {'guard.check-answers': true}, {}, {modes: modeItems('enforce', 'fix')});
  const watch = await start(MODELS[0], {'guard.check-answers': true}, {}, {modes: modeItems('watch', 'review')});

  //when
  const picked = (b, key) => { const sel = rowOf(b.doc).querySelector('.aiu-guard .adm-row[data-key="' + key + '"] select'); return sel && sel.options[sel.selectedIndex].textContent; };

  //then
  assert.deepStrictEqual([picked(enforce, 'guard.mode'), picked(enforce, 'guard.on-hold')], ['Enforce', 'Fix automatically'], 'both selects sit on the Guard row with plain labels');
  assert.deepStrictEqual([picked(watch, 'guard.mode'), picked(watch, 'guard.on-hold')], ['Watch only', 'Send to an admin']);
  assert.ok(!rowOf(enforce.doc).querySelector('.aiu-guard-watch'), 'no warning while it enforces');
  assert.strictEqual(rowOf(watch.doc).querySelector('.aiu-guard-watch').textContent, WATCH, 'watch only says plainly that it protects nothing');
  assert.deepStrictEqual([...watch.doc.querySelectorAll('#ai-rows .ai-grp:not(.aiu) .adm-row')].map((r) => r.dataset.key).filter((k) => /guard/.test(k)), [], 'the new keys are not listed again below the table');
  assert.deepStrictEqual(enforce.errors.concat(watch.errors), []);
  enforce.dom.window.close();
  watch.dom.window.close();
  return 'the Guard row offers Guard mode and On hold, and Watch only shows a plain warning that it protects nothing';
}

(async () => {
  console.log('PASS ' + await theGuardRowIsOffByDefaultAndItsSwitchesWaitForAModel());
  console.log('PASS ' + await theGuardRowOffersModeAndOnHoldAndWarnsInWatchOnly());
  console.log('PASS ' + await pickingAModelOpensTheSwitchesAndClearingItClosesThemAgain());
  console.log('PASS ' + await pickingNoneAndSavingTurnsTheGuardOff());
  console.log('PASS ' + await aGuardProblemFromTheServerShowsOnTheRow());
  console.log('PASS ' + await theGuardTextsAreTranslated());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
