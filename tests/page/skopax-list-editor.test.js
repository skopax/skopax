const assert = require('assert');
const {boot, item, typeIn, key, puts, wait, pack} = require('./skopax-admin-lists-kit');

const TEMPLATE = process.argv[2];
const EMAIL = {kind: 'email', separator: ','};

function entries() {
  return [
    item('self-review.dm', 'Self-review', 'Send to', {value: 'alice@example.com, bob@example.com', base: 'alice@example.com, bob@example.com', items: EMAIL}),
    item('slack.ask-channels', 'Slack', 'Channels that take questions', {value: 'all', base: 'all', items: {kind: 'channel', separator: ',', keywords: ['all', 'main']}}),
    item('jira.projects', 'Jira tasks', 'Projects', {kind: 'list', value: 'OPS, WEB', base: 'OPS, WEB', items: {kind: 'project-key', separator: ','}}),
    item('core.cert-skip', 'Monitor rules', 'Certificates not watched', {kind: 'list', value: 'shop.example.com', base: 'shop.example.com', items: {kind: 'host', separator: ','}}),
    item('general.timezone', 'General', 'Timezone', {value: 'UTC', base: 'UTC'}),
  ];
}
const row = (doc, k) => doc.querySelector('.adm-row[data-key="' + k + '"]');
const chips = (doc, k) => [...row(doc, k).querySelectorAll('.adm-chip-t')].map((c) => c.textContent);
const entry = (doc, k) => row(doc, k).querySelector('.adm-chip-in');
const value = (doc, k) => row(doc, k).querySelector('.adm-in').value;

async function aListSettingShowsItsItemsAsChipsAndAPlainSettingKeepsItsBox() {
  //given
  const {w, doc, errors} = await boot(TEMPLATE, '#t=settings', entries());

  //then
  assert.deepStrictEqual(chips(doc, 'self-review.dm'), ['alice@example.com', 'bob@example.com']);
  assert.deepStrictEqual(chips(doc, 'jira.projects'), ['OPS', 'WEB']);
  assert.ok(row(doc, 'general.timezone').querySelector('input.adm-in[type="text"]') && !row(doc, 'general.timezone').querySelector('.adm-chips'), 'a plain text setting is untouched');
  assert.ok(row(doc, 'self-review.dm').querySelector('.adm-save').disabled, 'nothing to save yet');
  assert.strictEqual(entry(doc, 'self-review.dm').getAttribute('aria-label'), 'Send to');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a list setting shows chips and a plain setting keeps its box');
}

async function enterAndCommaMakeChipsPasteSplitsAndTheSavedTextIsTheSameCommaList() {
  //given
  const {w, doc, errors, calls} = await boot(TEMPLATE, '#t=settings', entries());
  const dm = entry(doc, 'self-review.dm');

  //when
  typeIn(w, dm, 'carol@example.com');
  key(w, dm, 'Enter');
  typeIn(w, dm, 'dan@example.com,');
  const paste = new w.Event('paste', {bubbles: true, cancelable: true});
  paste.clipboardData = {getData: () => 'erin@example.com frank@example.com,\ngina@example.com'};
  dm.dispatchEvent(paste);

  //then
  assert.deepStrictEqual(chips(doc, 'self-review.dm'), ['alice@example.com', 'bob@example.com', 'carol@example.com', 'dan@example.com', 'erin@example.com', 'frank@example.com', 'gina@example.com']);
  assert.strictEqual(dm.value, '', 'the box is empty again');
  assert.strictEqual(value(doc, 'self-review.dm'), 'alice@example.com, bob@example.com, carol@example.com, dan@example.com, erin@example.com, frank@example.com, gina@example.com');
  assert.ok(!row(doc, 'self-review.dm').querySelector('.adm-save').disabled);

  //when
  row(doc, 'self-review.dm').querySelector('.adm-save').click();
  await wait(80);

  //then
  assert.deepStrictEqual(puts(calls), [{key: 'self-review.dm', value: 'alice@example.com, bob@example.com, carol@example.com, dan@example.com, erin@example.com, frank@example.com, gina@example.com'}], 'the same comma text as before');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS Enter, comma and paste make chips and Save sends the same comma text');
}

async function backspaceOnAnEmptyBoxRemovesTheLastChipAndTheCrossRemovesOne() {
  //given
  const {w, doc, errors} = await boot(TEMPLATE, '#t=settings', entries());
  const dm = entry(doc, 'self-review.dm');

  //when
  key(w, dm, 'Backspace');

  //then
  assert.deepStrictEqual(chips(doc, 'self-review.dm'), ['alice@example.com']);
  assert.strictEqual(value(doc, 'self-review.dm'), 'alice@example.com');

  //when
  typeIn(w, dm, 'x');
  key(w, dm, 'Backspace');

  //then
  assert.deepStrictEqual(chips(doc, 'self-review.dm'), ['alice@example.com'], 'Backspace in a box with text leaves the chips alone');

  //when
  typeIn(w, dm, '');
  row(doc, 'self-review.dm').querySelector('.adm-chip-x').click();

  //then
  assert.deepStrictEqual(chips(doc, 'self-review.dm'), []);
  assert.strictEqual(value(doc, 'self-review.dm'), '');
  assert.ok(row(doc, 'self-review.dm').querySelector('.adm-save') && !row(doc, 'self-review.dm').querySelector('.adm-save').disabled, 'an emptied list can be saved');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS Backspace on an empty box and the cross remove chips');
}

async function anInvalidItemIsRedWithItsReasonAndBlocksSave() {
  //given
  const {w, doc, errors} = await boot(TEMPLATE, '#t=settings', entries());
  const dm = entry(doc, 'self-review.dm');

  //when
  typeIn(w, dm, 'not-an-email,');

  //then
  const bad = row(doc, 'self-review.dm').querySelector('.adm-chip.bad');
  assert.ok(bad && bad.querySelector('.adm-chip-t').textContent === 'not-an-email');
  assert.strictEqual(bad.getAttribute('aria-invalid'), 'true');
  assert.strictEqual(bad.title, 'Not an e-mail address');
  assert.ok(row(doc, 'self-review.dm').querySelector('.adm-chip-why').textContent.includes('Not an e-mail address'), 'the reason is written out');
  assert.ok(row(doc, 'self-review.dm').querySelector('.adm-save').disabled, 'a list with an invalid item cannot be saved');

  //when
  row(doc, 'self-review.dm').querySelectorAll('.adm-chip-x')[2].click();

  //then
  assert.ok(!row(doc, 'self-review.dm').querySelector('.adm-chip.bad'));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS an invalid chip is red with its reason and blocks Save');
}

async function everyItemKindChecksItsOwnShapeAndTheChannelKeywordsAreChoices() {
  //given
  const {w, doc, errors} = await boot(TEMPLATE, '#t=settings', entries().concat([item('slack.push-user', 'Slack', 'Push users', {value: '', items: {kind: 'slack-user-id', separator: ','}}),
    item('mcp.allowed-clients', 'General', 'MCP allowed clients', {kind: 'list', value: '', items: {kind: 'client-id', separator: ','}})]));
  const badIn = (k, text) => { typeIn(w, entry(doc, k), text + ','); const n = row(doc, k).querySelectorAll('.adm-chip.bad').length; return n; };

  //then
  assert.strictEqual(badIn('jira.projects', '1ab'), 1, 'a project key starts with a letter');
  assert.strictEqual(badIn('jira.projects', 'OPS'), 1, 'a good key adds no problem');
  assert.strictEqual(badIn('jira.projects', 'PLAT_CORE'), 1, 'letters, digits and underscores are fine');
  assert.strictEqual(badIn('jira.projects', 'a1'), 1, 'and so are lower case letters');
  assert.strictEqual(badIn('core.cert-skip', '*.example.com'), 0, 'a wildcard host is fine');
  assert.strictEqual(badIn('core.cert-skip', 'shop.example.com.'), 0, 'a trailing dot is stripped before the check');
  assert.strictEqual(badIn('core.cert-skip', 'bad_host'), 1, 'a host has no underscores');
  assert.strictEqual(badIn('slack.push-user', 'U012ABCDE'), 0);
  assert.strictEqual(badIn('slack.push-user', 'nope'), 1);
  assert.strictEqual(badIn('mcp.allowed-clients', 'skopax-mcp'), 0);

  //when
  const words = [...row(doc, 'slack.ask-channels').querySelectorAll('.adm-chip-word')];
  const channel = entry(doc, 'slack.ask-channels');
  words.find((b) => b.dataset.word === 'main').click();
  const afterMain = chips(doc, 'slack.ask-channels');
  typeIn(w, channel, 'C012ABCDEFG,');
  const afterId = chips(doc, 'slack.ask-channels');
  words.find((b) => b.dataset.word === 'all').click();

  //then
  assert.deepStrictEqual(words.map((b) => b.dataset.word), ['all', 'main']);
  assert.deepStrictEqual(afterMain, ['main'], 'a keyword replaces the list');
  assert.deepStrictEqual(afterId, ['C012ABCDEFG'], 'a channel id removes the keyword');
  assert.deepStrictEqual(chips(doc, 'slack.ask-channels'), ['all'], 'a keyword replaces the ids');
  assert.strictEqual(value(doc, 'slack.ask-channels'), 'all');
  assert.ok(!row(doc, 'slack.ask-channels').querySelector('.adm-chip.bad'));
  assert.ok(words.find((b) => b.dataset.word === 'all').disabled, 'a keyword already listed is not offered again');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS every item kind checks its own shape and all/main stay choices');
}

async function spaceSeparatedScopeSavesWithSpacesAndChipsAreKeyboardReachable() {
  //given
  const {w, doc, errors, calls} = await boot(TEMPLATE, '#t=settings', [item('sso.scope', 'General', 'Scope', {value: 'openid profile', base: 'openid profile', items: {kind: 'scope', separator: ' '}})]);
  const sc = entry(doc, 'sso.scope');

  //when
  typeIn(w, sc, 'email ');

  //then
  assert.deepStrictEqual(chips(doc, 'sso.scope'), ['openid', 'profile', 'email']);
  assert.strictEqual(value(doc, 'sso.scope'), 'openid profile email');
  const first = row(doc, 'sso.scope').querySelector('.adm-chip');
  assert.strictEqual(first.tabIndex, 0, 'a chip can be focused');
  assert.strictEqual(row(doc, 'sso.scope').querySelector('.adm-chips').getAttribute('role'), 'group');
  assert.strictEqual(row(doc, 'sso.scope').querySelector('.adm-chip-x').getAttribute('aria-label'), 'Remove');
  assert.ok(row(doc, 'sso.scope').querySelector('.adm-chip-x').getAttribute('aria-labelledby').split(' ').length === 2, 'named by Remove and the item');

  //when
  first.focus();
  key(w, first, 'Backspace');

  //then
  assert.deepStrictEqual(chips(doc, 'sso.scope'), ['profile', 'email']);
  assert.strictEqual(doc.activeElement.className.includes('adm-chip'), true, 'focus moves to the next chip');

  //when
  row(doc, 'sso.scope').querySelector('.adm-save').click();
  await wait(80);

  //then
  assert.deepStrictEqual(puts(calls), [{key: 'sso.scope', value: 'profile email'}]);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a space separated scope saves with spaces and chips are focusable, named and removable with the keyboard');
}

async function aTextTypedWithoutACommaIsKeptWhenTheBoxLosesFocus() {
  //given
  const {w, doc, errors} = await boot(TEMPLATE, '#t=settings', entries());
  const dm = entry(doc, 'self-review.dm');

  //when
  typeIn(w, dm, 'hank@example.com');
  dm.dispatchEvent(new w.Event('blur'));

  //then
  assert.deepStrictEqual(chips(doc, 'self-review.dm').slice(-1), ['hank@example.com']);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS text typed without a comma becomes a chip when the box loses focus');
}

async function theEditorReadsInEveryPackAndStaysInsideAPhoneWidth() {
  for (const lang of ['de', 'es', 'fr', 'tr']) {
    //given
    const {w, doc, errors} = await boot(TEMPLATE, '#t=settings', entries(), {lang});
    const p = pack(TEMPLATE, lang);

    //when
    typeIn(w, entry(doc, 'self-review.dm'), 'nope,');
    await wait(30);

    //then
    assert.strictEqual(entry(doc, 'self-review.dm').getAttribute('placeholder'), p['Type an e-mail address and press Enter or comma'], lang);
    assert.ok(row(doc, 'self-review.dm').querySelector('.adm-chip-why').textContent.includes(p['Not an e-mail address']), lang + ' translates the reason');
    assert.strictEqual(row(doc, 'self-review.dm').querySelector('.adm-chip-x').getAttribute('aria-label'), p['Remove'], lang);
    assert.strictEqual(row(doc, 'slack.ask-channels').querySelector('.adm-chip-word').getAttribute('aria-label'), p['Add all'], lang);
    assert.deepStrictEqual(errors, []);
    w.close();
  }
  const css = require('fs').readFileSync(TEMPLATE, 'utf8');
  assert.ok(/@media \(max-width: 768px\) \{ \.ws-row \{ grid-template-columns: minmax\(0, 1fr\); \} \.adm-chip-x \{ width: 36px; height: 36px; \}/.test(css), 'the phone rules are in place');
  console.log('PASS the editor reads in de, es, fr and tr and has phone rules');
}

async function aServerPatternWinsAndAnInvalidChipKeepsSaveOffAfterARepaint() {
  //given
  const strict = {kind: 'email', separator: ',', pattern: '[a-z]+@example\\.com', flags: '', strip: '', why: 'not a mailbox of example.com'};
  const {w, doc, errors, calls} = await boot(TEMPLATE, '#t=settings', entries().map((e) => e.key === 'self-review.dm' ? Object.assign({}, e, {items: strict}) : e));
  const dm = entry(doc, 'self-review.dm');

  //when
  typeIn(w, dm, 'bob2@example.com,');

  //then
  const bad = row(doc, 'self-review.dm').querySelector('.adm-chip.bad');
  assert.ok(bad && bad.title === 'Not a mailbox of example.com', 'the pattern and reason come from the server entry');
  assert.ok(row(doc, 'self-review.dm').querySelector('.adm-save').disabled);

  //when
  typeIn(w, entry(doc, 'jira.projects'), 'WEB2,');
  row(doc, 'jira.projects').querySelector('.adm-save').click();
  await wait(100);

  //then
  assert.ok(puts(calls).length === 1 && puts(calls)[0].key === 'jira.projects');
  assert.ok(row(doc, 'self-review.dm').querySelector('.adm-chip.bad'), 'the unsaved invalid chip survived the repaint');
  assert.ok(row(doc, 'self-review.dm').querySelector('.adm-save').disabled, 'and its Save is still off');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the server pattern wins and an invalid chip keeps Save off after a repaint');
}

(async () => {
  const tests = [aListSettingShowsItsItemsAsChipsAndAPlainSettingKeepsItsBox, enterAndCommaMakeChipsPasteSplitsAndTheSavedTextIsTheSameCommaList, backspaceOnAnEmptyBoxRemovesTheLastChipAndTheCrossRemovesOne,
    anInvalidItemIsRedWithItsReasonAndBlocksSave, everyItemKindChecksItsOwnShapeAndTheChannelKeywordsAreChoices, spaceSeparatedScopeSavesWithSpacesAndChipsAreKeyboardReachable,
    aTextTypedWithoutACommaIsKeptWhenTheBoxLosesFocus, aServerPatternWinsAndAnInvalidChipKeepsSaveOffAfterARepaint, theEditorReadsInEveryPackAndStaysInsideAPhoneWidth];
  for (const t of tests) {
    try { await t(); } catch (e) { console.log('FAIL ' + (e && e.message ? e.message : e)); console.log(e && e.stack); process.exitCode = 1; return; }
  }
})();
