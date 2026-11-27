const assert = require('assert');
const {boot, settings, nameOf, wait, GROUPS, MODELS} = require('./skopax-ai-kit');

const TEMPLATE = process.argv[2];
const EVIL = '<img src=x onerror=alert(1)>';
const HEADS = ['Use', 'Model', 'Effort', 'Cost', 'Data goes to'];
const FIT = 'Cheap models often miss steps in long investigations';
const PHONE = '(max-width: 640px), (max-height: 500px) and (pointer: coarse)';
const SAVED = 'Saved, live within 15 s on every pod';

const rowOf = (doc, use) => doc.querySelector('#ai-rows .aiu-row[data-use="' + use + '"]');
const cell = (doc, use, part) => rowOf(doc, use).querySelector(part === 'price' || part === 'run' ? '.aiu-cost .aiu-' + part : '.aiu-' + part + ' .aiu-v').textContent;
const pick = (w, sel, value) => {
  sel.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true}));
  sel.value = value;
  sel.dispatchEvent(new w.Event('input', {bubbles: true}));
  sel.dispatchEvent(new w.Event('change', {bubbles: true}));
};
const puts = (calls) => calls.filter((c) => c[0] === 'PUT' || (c[0] === 'DELETE' && c[1].indexOf('/api/admin/settings') === 0)).map((c) => [c[0], c[1], c[2]]);

async function rowsRenderEveryUseWithPriceCostAndCompany() {
  //given
  const {dom, doc, errors} = await boot(TEMPLATE, {uses: {plugin: {company: EVIL}}});
  const de = await boot(TEMPLATE, {lang: 'de'});

  //when
  const order = [...doc.querySelectorAll('#ai-rows .aiu-row')].map((r) => r.dataset.use);
  const heads = [...doc.querySelectorAll('#ai-rows .aiu-t thead th')].map((th) => th.textContent);
  const labels = [...rowOf(doc, 'card').querySelectorAll('.aiu-k')].map((k) => k.textContent);
  const tags = order.map((u) => rowOf(doc, u).querySelector('.aiu-use .aiu-prov').textContent);
  const settingsLeft = [...doc.querySelectorAll('#ai-rows .ai-grp:not(.aiu) .adm-row')].map((r) => r.dataset.key);
  const summary = doc.querySelector('#ai-rows .aiu .adm-model-summary');
  const table = doc.querySelector('#ai-rows .aiu-t');

  //then
  assert.deepStrictEqual(order, ['card', 'critical', 'high', 'slack', 'chat', 'gate', 'review', 'plugin'], 'one row per use, in the order the server gives');
  assert.strictEqual(doc.querySelector('#ai-rows .aiu h3').textContent, 'Who does what');
  assert.deepStrictEqual(heads, HEADS);
  assert.deepStrictEqual(labels, HEADS, 'each cell carries its column name for the phone layout');
  assert.deepStrictEqual(tags, ['Anthropic', 'Anthropic', 'deepseek', 'OpenAI', 'OpenAI', 'deepseek', 'OpenAI', 'Anthropic'], 'each row names its provider, the plug-in row too');
  assert.strictEqual(rowOf(doc, 'high').querySelector('.aiu-use b').textContent, 'High cards');
  assert.deepStrictEqual([cell(doc, 'card', 'price'), cell(doc, 'card', 'run'), cell(doc, 'card', 'co')], ['$5.00 / $25.00 per 1M tokens', '$0.42 per run, 7 days', 'Anthropic']);
  assert.deepStrictEqual([cell(doc, 'high', 'price'), cell(doc, 'high', 'run'), cell(doc, 'high', 'co')], ['$0.27 / $1.10 per 1M tokens', '$0.01 per run, 7 days', 'api.deepseek.example.com']);
  assert.deepStrictEqual([cell(doc, 'chat', 'price'), cell(doc, 'chat', 'run'), cell(doc, 'chat', 'co')], ['$0.40 / $1.60 per 1M tokens', '$0.0040 per run, 7 days', 'OpenAI']);
  assert.deepStrictEqual([cell(doc, 'plugin', 'model'), cell(doc, 'plugin', 'price'), cell(doc, 'plugin', 'run')], ['claude-sonnet-5-5', 'No price', 'No runs in 7 days'], 'a use with no settings row shows its model as text');
  assert.ok(!rowOf(doc, 'plugin').querySelector('select'));
  assert.strictEqual(cell(doc, 'plugin', 'co'), EVIL, 'a hostile company is text');
  assert.strictEqual(doc.querySelectorAll('#ai-rows img').length, 0);
  assert.ok(rowOf(doc, 'card').querySelector('.aiu-effort .adm-row[data-key="investigate.effort"] select'), 'the effort row sits in its use row');
  assert.ok(rowOf(doc, 'gate').querySelector('.aiu-effort .adm-row[data-key="investigate.gate-effort"]'));
  assert.ok(!rowOf(doc, 'critical').querySelector('.aiu-effort .adm-row') && !rowOf(doc, 'review').querySelector('.aiu-effort .adm-row'), 'a use without an effort has none');
  assert.deepStrictEqual(settingsLeft, ['investigate.provider', 'investigate.rooms'], 'the model and effort rows the table covers, and the Private AI addresses switch shown with the connections, are not drawn twice');
  assert.strictEqual(doc.querySelectorAll('#ai-rows .adm-pair').length, 0);
  assert.strictEqual(doc.querySelectorAll('#ai-rows .adm-model-summary').length, 1, 'one Which model runs line');
  assert.strictEqual(summary.textContent, 'Which model runs: Critical cards claude-opus-5-5 \u00b7 High cards deepseek/deepseek-chat \u00b7 Warning and Info cards claude-opus-5-5', 'the line says what the uses table says');
  assert.ok(summary.compareDocumentPosition(table) & 4, 'and it sits above the table');
  assert.ok(de.doc.querySelector('#ai-rows .aiu h3').textContent !== 'Who does what' && de.doc.querySelector('#ai-rows .aiu-t thead th').textContent !== 'Use', 'the German pack translates the table');
  const order2 = (cellEl, sels) => sels.map((q) => { const n = cellEl.querySelector(q); return n ? [...cellEl.querySelectorAll('*')].indexOf(n) : -1; });
  const modelAt = order2(rowOf(doc, 'high').querySelector('.aiu-model'), ['select.aiu-in', '.aiu-meta', '.aiu-help']);
  const effortAt = order2(rowOf(doc, 'card').querySelector('.aiu-effort'), ['.adm-seg', '.adm-m']);
  assert.ok(modelAt.every((n, k) => n > -1 && (k === 0 || n > modelAt[k - 1])), 'in Model: select, then the source and Reset line, then the help');
  assert.ok(effortAt.every((n, k) => n > -1 && (k === 0 || n > effortAt[k - 1])), 'in Effort: the switch, then the source and Reset line');
  assert.deepStrictEqual([...rowOf(doc, 'card').querySelector('.aiu-cost').children].map((c) => c.className), ['aiu-k', 'aiu-v aiu-price', 'aiu-v aiu-run'], 'Cost has the price line, then the run line');
  assert.ok(de.doc.querySelector('.aiu-cost .aiu-unit').textContent !== 'per 1M tokens', 'the German pack translates the price unit');
  assert.deepStrictEqual(errors.concat(de.errors), []);
  dom.window.close();
  de.dom.window.close();
  return 'one row per use with its label, provider, model, effort, price per million tokens, average cost per run and company; the covered rows are not drawn twice and the Which model runs line above says what the uses say';
}

async function aSelectChangeSavesTheSettingAndRepaints() {
  //given
  const {dom, w, doc, errors, calls, srv} = await boot(TEMPLATE);
  const before = calls.filter((c) => c[1] === '/api/admin/ai-uses').length;
  const select = doc.getElementById('aiu-card-model');
  const save = () => rowOf(doc, 'card').querySelector('.aiu-save');

  //when
  select.focus();
  pick(w, select, 'claude-sonnet-5-5');
  await wait(60);
  const waiting = [puts(calls).length, save().hidden, rowOf(doc, 'card').querySelector('.aiu-cancel').hidden];
  save().click();
  await wait(150);
  const saved = [cell(doc, 'card', 'price'), rowOf(doc, 'card').querySelector('.aiu-msg').textContent, doc.getElementById('aiu-card-model').value, save().hidden];
  pick(w, doc.getElementById('aiu-slack-model'), 'gpt-6.2-sol');
  rowOf(doc, 'slack').querySelector('.aiu-cancel').click();
  await wait(30);
  const cancelled = [doc.getElementById('aiu-slack-model').value, rowOf(doc, 'slack').querySelector('.aiu-save').hidden, doc.activeElement === doc.getElementById('aiu-slack-model')];
  srv.refuse = 'That model is not in the list';
  const chat = doc.getElementById('aiu-chat-model');
  pick(w, chat, 'gpt-6.2-astra');
  chat.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
  await wait(150);

  //then
  assert.deepStrictEqual(waiting, [0, false, false], 'an arrow-key change sends nothing and shows Save and Cancel');
  assert.deepStrictEqual(puts(calls), [['PUT', '/api/admin/settings', {key: 'investigate.model', value: 'claude-sonnet-5-5'}], ['PUT', '/api/admin/settings', {key: 'chat.model', value: 'gpt-6.2-astra'}]], 'Save and Enter save; Cancel sends nothing');
  assert.ok(calls.filter((c) => c[1] === '/api/admin/ai-uses').length > before, 'the uses are read again');
  assert.deepStrictEqual(saved, ['$3.00 / $15.00 per 1M tokens', SAVED, 'claude-sonnet-5-5', true], 'the table repaints with the new price and the usual Saved message');
  assert.deepStrictEqual(cancelled, ['gpt-6.2-astra', true, true], 'Cancel puts the saved model back and keeps the focus on the select');
  assert.strictEqual(rowOf(doc, 'chat').querySelector('.aiu-msg').textContent, 'That model is not in the list', 'a refused save shows the server text');
  assert.ok(rowOf(doc, 'chat').querySelector('.aiu-msg').classList.contains('bad'));
  assert.strictEqual(doc.getElementById('aiu-chat-model').value, 'gpt-6.2-sol', 'and the select goes back to the saved model');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a select change shows Save and Cancel and sends nothing; Save or Enter sends the settings PUT and the table repaints with the Saved message; a refusal shows its text and puts the saved model back';
}

async function twoQuickSavesNeverOverlap() {
  //given
  const {dom, w, doc, errors, calls, srv} = await boot(TEMPLATE);
  let release;
  srv.putHold = new Promise((r) => { release = r; });

  //when
  pick(w, doc.getElementById('aiu-card-model'), 'claude-sonnet-5-5');
  rowOf(doc, 'card').querySelector('.aiu-save').click();
  await wait(20);
  const busy = [...rowOf(doc, 'card').querySelectorAll('.aiu-model select, .aiu-model button')].every((c) => c.disabled);
  rowOf(doc, 'card').querySelector('.aiu-save').click();
  doc.getElementById('aiu-card-model').dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
  pick(w, doc.getElementById('aiu-chat-model'), 'gpt-6.2-astra');
  rowOf(doc, 'chat').querySelector('.aiu-save').click();
  await wait(40);
  const during = puts(calls).length;
  srv.putHold = null;
  release();
  await wait(300);

  //then
  assert.ok(busy, 'a save in flight disables the controls of its row');
  assert.strictEqual(during, 1, 'the second save waits for the first');
  assert.strictEqual(srv.most, 1, 'never two saves at once');
  assert.deepStrictEqual(puts(calls).map((c) => c[2].key), ['investigate.model', 'chat.model'], 'each row saves once, in order');
  assert.deepStrictEqual([doc.getElementById('aiu-card-model').value, doc.getElementById('aiu-chat-model').value], ['claude-sonnet-5-5', 'gpt-6.2-astra']);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a save in flight disables its row, and two quick saves run one after the other';
}

async function aCoveredSettingKeepsItsSourceResetHelpAndProblem() {
  //given
  const items = settings().map((i) => i.key === 'investigate.slack-model' ? Object.assign({}, i, {problem: 'This model was retired', live: false}) : i);
  const {dom, doc, errors, calls} = await boot(TEMPLATE, {items});
  const high = rowOf(doc, 'high');

  //when
  const tag = high.querySelector('.aiu-meta .tag.k-act');
  const reset = high.querySelector('.aiu-meta .adm-reset');
  const slack = rowOf(doc, 'slack');
  reset.click();
  await wait(150);

  //then
  assert.ok(tag && tag.textContent.indexOf('Set by ') === 0, 'a setting saved here says who set it: ' + (tag && tag.textContent));
  assert.ok(high.classList.contains('set'));
  assert.strictEqual(slack.querySelector('.aiu-meta .tag.k-warn').textContent, 'after restart', 'a setting read at start says so');
  assert.strictEqual(slack.querySelector('.aiu-model .adm-problem').textContent, 'This model was retired', 'the setting\'s own problem shows in the row');
  assert.strictEqual(rowOf(doc, 'review').querySelector('.aiu-help .adm-help').textContent, 'Model of the daily self-review', 'and its help');
  assert.ok(!rowOf(doc, 'card').querySelector('.adm-reset'), 'a default has no Reset');
  assert.deepStrictEqual(puts(calls), [['DELETE', '/api/admin/settings?key=investigate.model-high', undefined]], 'Reset sends DELETE');
  assert.strictEqual(doc.getElementById('aiu-high-model').value, '', 'the use follows the default again');
  assert.deepStrictEqual([cell(doc, 'high', 'price'), rowOf(doc, 'high').querySelector('.aiu-prov').textContent], ['$5.00 / $25.00 per 1M tokens', 'Anthropic'], 'and the row repaints from a fresh read');
  assert.strictEqual(rowOf(doc, 'high').querySelector('.aiu-msg').textContent, 'Reset to empty');
  assert.ok(!rowOf(doc, 'high').querySelector('.adm-reset'));
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a covered setting keeps its Set by and after-restart tags, help, own problem and Reset; Reset sends DELETE and the row repaints';
}

async function aPaletteJumpFocusesTheModelSelect() {
  //given
  const {dom, w, doc, errors} = await boot(TEMPLATE);

  //when
  w.ADMIN_REVEAL = {tab: 'ai', key: 'investigate.model'};
  await w.loadAi();
  await wait(30);

  //then
  assert.strictEqual(rowOf(doc, 'card').dataset.key, 'investigate.model');
  assert.ok(rowOf(doc, 'card').classList.contains('adm-hit'), 'the row is highlighted');
  assert.strictEqual(doc.activeElement, doc.getElementById('aiu-card-model'), 'and its select has the focus');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a jump to a covered model setting highlights its row and focuses the select';
}

async function settingsHasNoCoveredRow() {
  //given
  const {dom, w, doc, errors} = await boot(TEMPLATE, {hash: '#t=settings'});

  //when
  const keys = () => [...doc.querySelectorAll('#admin .adm-row')].map((r) => r.dataset.key);
  const first = keys();
  w.pickTab('ai');
  await wait(200);
  w.pickTab('settings');
  await wait(200);

  //then
  assert.ok(first.indexOf('self-review.hour') >= 0, 'Settings is drawn');
  assert.ok(first.indexOf('self-review.model') < 0, 'a model the uses table covers is not drawn in Settings');
  assert.ok(keys().indexOf('self-review.model') < 0 && keys().indexOf('self-review.hour') >= 0, 'nor after a visit to the Investigator page');
  assert.ok(rowOf(doc, 'review'));
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a model setting the uses table covers is drawn only in the table, not again in Settings';
}

async function aFailedUsesReadSaysSo() {
  //given
  const {dom, doc, errors} = await boot(TEMPLATE, {usesFail: 500});
  const gone = await boot(TEMPLATE, {usesFail: 404});

  //then
  assert.strictEqual(doc.querySelector('#ai-rows .aiu-failed').textContent, 'The uses table did not load; open the tab again.');
  assert.ok(!doc.querySelector('#ai-rows .aiu') && doc.querySelector('#ai-rows .adm-pair[data-pair="investigate.model"]'), 'and the model rows are drawn as before');
  assert.ok(!gone.doc.querySelector('#ai-rows .aiu-failed'), 'a server without the table says nothing');
  assert.deepStrictEqual(errors.concat(gone.errors), []);
  dom.window.close();
  gone.dom.window.close();
  return 'a failed uses read shows a short message and the model rows as before';
}

async function aFailedPaintNeverLocksLaterSaves() {
  //given
  const {dom, w, doc, errors, calls} = await boot(TEMPLATE);
  const paint = w.adminPaintAll;
  let once = true;
  w.adminPaintAll = function () {
    if (once) { once = false; throw new Error('paint failed'); }
    return paint.apply(this, arguments);
  };

  //when
  pick(w, doc.getElementById('aiu-card-model'), 'claude-sonnet-5-5');
  rowOf(doc, 'card').querySelector('.aiu-save').click();
  await wait(150);
  const stuck = Object.keys(w.AIU_PENDING);
  pick(w, doc.getElementById('aiu-chat-model'), 'gpt-6.2-astra');
  rowOf(doc, 'chat').querySelector('.aiu-save').click();
  await wait(150);

  //then
  assert.deepStrictEqual(stuck, [], 'a save whose repaint throws still ends');
  assert.deepStrictEqual(puts(calls).map((c) => c[2].key), ['investigate.model', 'chat.model'], 'and the next save still sends its PUT');
  assert.strictEqual(rowOf(doc, 'chat').querySelector('.aiu-msg').textContent, SAVED);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a save whose repaint throws never locks the saves after it';
}

async function signingOutClearsTheTableState() {
  //given
  const {dom, w, doc, errors} = await boot(TEMPLATE);
  pick(w, doc.getElementById('aiu-card-model'), 'claude-sonnet-5-5');
  w.AIU_SAID.chat = {text: 'Saved', bad: false};
  w.AIU_PENDING.slack = 'gpt-6.2-sol';

  //when
  w.setAdminAccess(false);
  const left = [Object.keys(w.AIU_EDIT), Object.keys(w.AIU_PENDING), Object.keys(w.AIU_SAID)];
  w.setAdminAccess(true);
  await w.loadAi();
  await wait(30);

  //then
  assert.deepStrictEqual(left, [[], [], []], 'unsaved picks, pending saves and messages are dropped');
  assert.strictEqual(doc.getElementById('aiu-card-model').value, 'claude-opus-5-5', 'the next admin sees the saved model');
  assert.ok(rowOf(doc, 'card').querySelector('.aiu-save').hidden && !doc.getElementById('aiu-slack-model').disabled && rowOf(doc, 'chat').querySelector('.aiu-msg').textContent === '');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'losing admin access drops the unsaved picks, pending saves and messages of the table';
}

async function aFindForACoveredKeyOutsideTheInvestigatorFocusesItsSelect() {
  //given
  const {dom, w, doc, errors} = await boot(TEMPLATE, {hash: '#t=settings'});

  //when
  const found = w.__skopaxFindSetting('self-review.model');
  await wait(250);

  //then
  assert.strictEqual(found, true);
  assert.ok(!doc.getElementById('panel-ai').hidden, 'the Investigator page opens');
  assert.strictEqual(doc.activeElement, doc.getElementById('aiu-review-model'), 'with the focus on the self-review model select');
  assert.ok(rowOf(doc, 'review').classList.contains('adm-hit'));
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'finding a covered setting of another group opens the Investigator page on its table row';
}

async function theRowTagsAndSummaryAreTranslated() {
  //given
  const items = settings().map((i) => i.key === 'investigate.slack-model' ? Object.assign({}, i, {source: 'admin', by: 'dora.lane@example.com', at: new Date().toISOString()}) : i);
  const {dom, w, doc, errors} = await boot(TEMPLATE, {lang: 'de', items});

  //when
  const tag = rowOf(doc, 'slack').querySelector('.aiu-meta .tag.k-act').textContent;
  const plain = rowOf(doc, 'card').querySelector('.aiu-meta .src-default').textContent;
  const summary = doc.querySelector('#ai-rows .aiu .adm-model-summary').textContent;
  rowOf(doc, 'slack').querySelector('.adm-reset').click();
  await wait(150);
  const back = rowOf(doc, 'slack').querySelector('.aiu-msg').textContent;
  rowOf(doc, 'high').querySelector('.adm-reset').click();
  await wait(150);
  const empty = rowOf(doc, 'high').querySelector('.aiu-msg').textContent;

  //then
  assert.ok(/^Gesetzt von dora\.lane \d\d:\d\d$/.test(tag), 'Set by: ' + tag);
  assert.strictEqual(plain, 'Standard');
  assert.strictEqual(summary, 'Welches Modell l\u00e4uft: Kritische Karten claude-opus-5-5 \u00b7 Hohe Karten deepseek/deepseek-chat \u00b7 Warn- und Info-Karten claude-opus-5-5');
  assert.strictEqual(back, 'Zur\u00fcckgesetzt auf claude-opus-5-5');
  assert.strictEqual(empty, 'Auf leer zur\u00fcckgesetzt');
  const toast = w.failToast('the Who does what table could not be redrawn: boom');
  await wait(30);
  assert.strictEqual(toast.querySelector('.toast-t').textContent, 'Nicht erledigt: Die Tabelle \u201eWer macht was\u201c konnte nicht neu gezeichnet werden: boom.', 'and the redraw toast');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the Set by and default tags, Reset to and the Which model runs line are translated';
}

async function aProblemRowShowsItsProblem() {
  //given
  const problem = 'No OpenAI key: runs use claude-opus-5-5 instead';
  const {dom, doc, errors} = await boot(TEMPLATE, {uses: {review: {problem}}});

  //when
  const row = rowOf(doc, 'review'), note = row.querySelector('.aiu-problem');

  //then
  assert.strictEqual(note.textContent, problem);
  assert.ok(note.classList.contains('bad') && row.classList.contains('aiu-bad'), 'the problem is a red line on its row');
  assert.ok(note.closest('.aiu-model') && !row.querySelector('th .aiu-problem'), 'in the model cell, not the row header');
  assert.ok(doc.getElementById('aiu-review-model').getAttribute('aria-describedby').split(' ').indexOf(note.id) >= 0, 'and is tied to the model select');
  assert.strictEqual(doc.querySelectorAll('#ai-rows .aiu-problem').length, 1, 'rows without a problem have none');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a use with a problem shows it as a red line in its model cell, tied to the select';
}

async function aCheapModelOnCardRowsShowsTheFitWarning() {
  //given
  const {dom, doc, errors} = await boot(TEMPLATE, {uses: {critical: {fit: 'cheap'}, chat: {fit: 'cheap'}}});

  //when
  const warned = [...doc.querySelectorAll('#ai-rows .aiu-fit')].map((n) => [n.closest('.aiu-row').dataset.use, n.textContent, !!n.closest('.aiu-model')]);

  //then
  assert.deepStrictEqual(warned, [['critical', FIT, true], ['high', FIT, true]], 'card, critical and high rows warn in the model cell; gate and chat do not, even on a cheap model');
  assert.ok(doc.getElementById('aiu-high-model').getAttribute('aria-describedby').indexOf(rowOf(doc, 'high').querySelector('.aiu-fit').id) >= 0);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a cheap model on the Card, Critical or High row shows the fit warning, and other uses do not';
}

async function optionsAreGroupedByProvider() {
  //given
  const items = settings().map((i) => i.key === 'investigate.slack-model' ? Object.assign({}, i, {value: 'gpt-legacy'}) : i);
  const {dom, doc, errors} = await boot(TEMPLATE, {items});

  //when
  const shape = (id) => [...doc.getElementById(id).children].map((c) => c.tagName === 'OPTGROUP' ? [c.label, [...c.children].map((o) => o.value)] : c.value);
  const critical = doc.getElementById('aiu-critical-model');

  //then
  assert.deepStrictEqual(shape('aiu-card-model'), Object.keys(GROUPS).map((g) => [g, GROUPS[g]]), 'one optgroup per provider');
  assert.deepStrictEqual(shape('aiu-critical-model'), [''].concat(Object.keys(GROUPS).map((g) => [g, GROUPS[g]])), 'an ungrouped choice comes first');
  assert.strictEqual(critical.options[0].textContent, 'Same as Card runs');
  assert.strictEqual(doc.getElementById('aiu-high-model').value, 'deepseek/deepseek-chat');
  assert.strictEqual(doc.getElementById('aiu-slack-model').value, 'gpt-legacy', 'a saved model outside the list stays selected');
  assert.strictEqual(doc.getElementById('aiu-slack-model').options.length, MODELS.length + 1);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'each model select groups its choices by provider, with ungrouped choices first and a saved model outside the list kept';
}

async function theTableIsKeyboardUsable() {
  //given
  const {dom, w, doc, errors} = await boot(TEMPLATE);
  const box = doc.querySelector('#ai-rows .aiu');
  const shown = (c) => !c.closest('[hidden]') && !c.hidden;
  const stops = () => [...box.querySelectorAll('select, input, button, a[href], [tabindex]')].filter((c) => shown(c) && !c.disabled && c.tabIndex >= 0);

  //when
  const controls = [...box.querySelectorAll('select, input, button')].filter(shown);
  const unnamed = controls.filter((c) => !nameOf(doc, c)).map((c) => c.outerHTML.slice(0, 80));
  const order = stops().map((c) => c.closest('.aiu-row').dataset.use + ':' + (c.matches('select.aiu-in') ? 'model' : c.closest('.aiu-effort') ? 'effort' : c.classList.contains('adm-reset') ? 'reset' : 'other'));
  const select = doc.getElementById('aiu-gate-model');
  select.focus();
  pick(w, select, 'claude-opus-5-5');
  const afterPick = stops().map((c) => c.closest('.aiu-row').dataset.use + ':' + (c.classList.contains('aiu-save') ? 'save' : c.classList.contains('aiu-cancel') ? 'cancel' : c.matches('select.aiu-in') ? 'model' : '-')).filter((s) => s.indexOf('gate:') === 0 && s !== 'gate:-');
  select.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
  await wait(150);

  //then
  assert.deepStrictEqual(unnamed, [], 'every control has a name');
  assert.strictEqual(nameOf(doc, doc.getElementById('aiu-card-model')), 'Card runs Model', 'a model select is named by its use and column');
  assert.ok([...box.querySelectorAll('[tabindex]')].every((c) => c.tabIndex <= 0), 'no control jumps the Tab order');
  assert.deepStrictEqual(order.filter((s) => /:model$/.test(s)), ['card', 'critical', 'high', 'slack', 'chat', 'gate', 'review'].map((u) => u + ':model'), 'Tab reaches each row in turn');
  const rowOrder = ['card', 'critical', 'high', 'slack', 'chat', 'gate', 'review'];
  assert.ok(['card', 'slack', 'chat', 'gate'].every((u) => { const next = rowOrder[rowOrder.indexOf(u) + 1]; return order.indexOf(u + ':model') < order.indexOf(u + ':effort') && order.indexOf(u + ':effort') < order.indexOf(next + ':model'); }), 'and inside a row, the model before its effort');
  assert.ok(order.indexOf('high:reset') > order.indexOf('high:model') && order.indexOf('high:reset') < order.indexOf('slack:model'), 'Reset sits in its row');
  assert.deepStrictEqual(afterPick, ['gate:model', 'gate:save', 'gate:cancel'], 'after a pick, Tab goes from the select to Save and Cancel');
  assert.ok([...box.querySelectorAll('thead th')].every((th) => th.getAttribute('scope') === 'col'));
  assert.ok([...box.querySelectorAll('tbody th')].every((th) => th.getAttribute('scope') === 'row'), 'each row is headed by its use');
  assert.ok([...box.querySelectorAll('[role="radiogroup"]')].every((g) => [...g.querySelectorAll('[role="radio"]')].filter((r) => r.tabIndex === 0).length === 1), 'an effort switch is one Tab stop, moved with the arrow keys');
  assert.strictEqual(box.querySelector('table').getAttribute('aria-labelledby'), box.querySelector('h3').id, 'the table is named by its heading');
  assert.strictEqual(doc.activeElement, doc.getElementById('aiu-gate-model'), 'after Enter saves and the table repaints, the focus is back on the same select');
  assert.strictEqual(rowOf(doc, 'gate').querySelector('.aiu-msg').textContent, SAVED);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the table has column and row headers, every control is named, Tab goes row by row (model, then Save and Cancel, Reset, effort), and the focus stays on the select across a save';
}

async function phoneWidthShowsOneCardPerUse() {
  //given
  const {dom, doc, errors} = await boot(TEMPLATE);
  const top = [...doc.querySelector('style').sheet.cssRules];
  const flat = (list) => list.flatMap((r) => r.cssRules && !r.selectorText ? flat([...r.cssRules]) : r.selectorText ? [r] : []);
  const value = (rules, selector, prop) => rules.filter((r) => r.selectorText.split(',').map((s) => s.trim()).indexOf(selector) >= 0).map((r) => r.style.getPropertyValue(prop)).filter(Boolean).slice(-1)[0] || '';
  const phone = top.filter((r) => r.media && r.media.mediaText === PHONE).flatMap((r) => [...r.cssRules]).filter((r) => r.selectorText);
  const wide = top.filter((r) => r.selectorText);

  //when
  const got = [['.aiu-t thead', 'display'], ['.aiu-t tr', 'display'], ['.aiu-t td', 'display'], ['.aiu-t tbody th', 'display'], ['.aiu-k', 'display'], ['.aiu-t td', 'min-width'], ['.aiu-t td', 'overflow-wrap'], ['.aiu-t select', 'max-width']].map((x) => value(phone, x[0], x[1]));

  //then
  assert.deepStrictEqual(got, ['none', 'block', 'block', 'block', 'block', '0px', 'anywhere', '100%'], 'on a phone the header row hides, each use is one block and each cell shows its column name');
  assert.strictEqual(value(phone, '.aiu-t tr', 'border-radius'), '6px', 'each use reads as a card');
  assert.strictEqual(value(wide, '.aiu-k', 'display'), 'none', 'on a wide screen the column names stay in the header');
  assert.deepStrictEqual([value(flat(top), '.aiu-box', 'max-width'), value(flat(top), '.aiu-box', 'overflow-x')], ['100%', 'auto'], 'between phone and wide the table scrolls inside its own box, never the page');
  assert.strictEqual(doc.querySelectorAll('#ai-rows .aiu-row').length, 8);
  assert.ok([...doc.querySelectorAll('#ai-rows .aiu-row')].every((r) => [...r.children].every((c) => c.querySelector('.aiu-k'))), 'every cell carries its column name');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'at phone width the header row hides and each use is one card whose cells show their column names, with no sideways page scroll';
}

(async () => {
  console.log('PASS ' + await rowsRenderEveryUseWithPriceCostAndCompany());
  console.log('PASS ' + await aSelectChangeSavesTheSettingAndRepaints());
  console.log('PASS ' + await twoQuickSavesNeverOverlap());
  console.log('PASS ' + await aCoveredSettingKeepsItsSourceResetHelpAndProblem());
  console.log('PASS ' + await aPaletteJumpFocusesTheModelSelect());
  console.log('PASS ' + await settingsHasNoCoveredRow());
  console.log('PASS ' + await aFailedUsesReadSaysSo());
  console.log('PASS ' + await aFailedPaintNeverLocksLaterSaves());
  console.log('PASS ' + await signingOutClearsTheTableState());
  console.log('PASS ' + await aFindForACoveredKeyOutsideTheInvestigatorFocusesItsSelect());
  console.log('PASS ' + await theRowTagsAndSummaryAreTranslated());
  console.log('PASS ' + await aProblemRowShowsItsProblem());
  console.log('PASS ' + await aCheapModelOnCardRowsShowsTheFitWarning());
  console.log('PASS ' + await optionsAreGroupedByProvider());
  console.log('PASS ' + await theTableIsKeyboardUsable());
  console.log('PASS ' + await phoneWidthShowsOneCardPerUse());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
