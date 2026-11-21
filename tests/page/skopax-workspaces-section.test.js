const assert = require('assert');
const {boot, links, typeIn, puts, wait, pack} = require('./skopax-admin-lists-kit');

const TEMPLATE = process.argv[2];
const STAGING = 'https://staging.example.com';
const rowsOf = (doc) => [...doc.querySelectorAll('#workspaces .ws-row')];
const vals = (doc) => rowsOf(doc).map((r) => [r.querySelector('.ws-name').value, r.querySelector('.ws-url').value]);
const addRow = async (w, doc, name, url) => {
  doc.querySelector('#workspaces .ws-add').click();
  const row = rowsOf(doc).pop();
  typeIn(w, row.querySelector('.ws-name'), name);
  typeIn(w, row.querySelector('.ws-url'), url);
  return row;
};

async function instancesAreTheirOwnAdminSectionWithTheDeploymentNameAndTheListRows() {
  //given
  const {w, doc, errors} = await boot(TEMPLATE, '#t=workspaces', [links({value: 'staging=' + STAGING + ', eu=https://eu.example.com/board', base: '', source: 'admin', by: 'nina.white@example.com', at: new Date().toISOString()})]);

  //then
  assert.ok(!doc.getElementById('panel-workspaces').hidden, 'the Workspaces panel is shown');
  assert.strictEqual(doc.querySelector('#tab-workspaces .lbl').textContent, 'Workspaces');
  assert.ok(doc.querySelector('#panel-workspaces .panel-note').textContent.includes('Other workspaces of Skopax, such as staging, that the switcher at the top of the sidebar opens; they share nothing with this one.'));
  assert.strictEqual(doc.querySelector('#workspaces .ws-me-n b').textContent, 'production');
  assert.ok(doc.querySelector('#workspaces .ws-me-n .tag').textContent.includes('environment'), 'the environment name shows where it comes from, read only');
  assert.ok(!doc.querySelector('#workspaces .ws-me input'), 'and has nothing to type into');
  assert.deepStrictEqual(vals(doc), [['staging', STAGING], ['eu', 'https://eu.example.com/board']]);
  assert.strictEqual(rowsOf(doc)[0].querySelector('.ws-open').getAttribute('href'), STAGING);
  assert.strictEqual(rowsOf(doc)[0].querySelector('.ws-open').getAttribute('rel'), 'noopener noreferrer');
  assert.strictEqual(doc.querySelectorAll('#admin .adm-row[data-key="core.workspace-links"]').length, 0, 'and it is gone from General');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS Other workspaces is its own Admin section with the workspace name and the list as rows');
}

async function addingValidatingReorderingAndRemovingRowsSendsTheSameNameEqualsUrlText() {
  //given
  const {w, doc, errors, calls} = await boot(TEMPLATE, '#t=workspaces', [links({value: 'staging=' + STAGING, base: 'staging=' + STAGING, source: 'config', from: 'environment'})]);
  const save = () => doc.querySelector('#workspaces .ws-save');
  assert.ok(save().disabled, 'nothing changed, nothing to save');

  //when
  const bad = await addRow(w, doc, 'eu', 'ftp://eu.example.com');

  //then
  assert.strictEqual(bad.querySelector('.ws-why').textContent, 'the address must start with http:// or https:// and name a host');
  assert.ok(save().disabled, 'a row with a problem cannot be saved');

  //when
  typeIn(w, bad.querySelector('.ws-url'), 'https://u:p@eu.example.com');

  //then
  assert.strictEqual(bad.querySelector('.ws-why').textContent, 'the address must not hold a user name or password');

  //when
  typeIn(w, bad.querySelector('.ws-name'), 'Staging');
  typeIn(w, bad.querySelector('.ws-url'), 'https://eu.example.com');

  //then
  assert.strictEqual(bad.querySelector('.ws-why').textContent, 'the name is already used by an earlier entry');

  //when
  typeIn(w, bad.querySelector('.ws-name'), 'bad<name>');

  //then
  assert.ok(bad.querySelector('.ws-why').textContent.startsWith('a name is 1 to 40 letters'));

  //when
  typeIn(w, bad.querySelector('.ws-name'), 'eu');
  typeIn(w, bad.querySelector('.ws-url'), 'https://eu.example.com/a,b');

  //then
  assert.ok(bad.querySelector('.ws-why').textContent.startsWith('An address cannot hold a comma'));

  //when
  typeIn(w, bad.querySelector('.ws-url'), 'https://eu.example.com/board');
  await addRow(w, doc, 'asia', 'http://asia.example.com');
  rowsOf(doc)[2].querySelector('.ws-up').click();
  rowsOf(doc)[0].querySelector('.ws-rm').click();

  //then
  assert.deepStrictEqual(vals(doc), [['asia', 'http://asia.example.com'], ['eu', 'https://eu.example.com/board']]);
  assert.ok(!save().disabled);

  //when
  save().click();
  await wait(80);

  //then
  assert.deepStrictEqual(puts(calls), [{key: 'core.workspace-links', value: 'asia=http://asia.example.com, eu=https://eu.example.com/board'}], 'the saved text is the same name=url list as before');
  assert.strictEqual(doc.querySelector('#workspaces .ws-msg').textContent, 'Saved, live within 15 s on every pod');
  assert.deepStrictEqual(vals(doc), [['asia', 'http://asia.example.com'], ['eu', 'https://eu.example.com/board']]);
  assert.ok(doc.querySelector('#workspaces .ws-reset'), 'a value set here can be reset');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS rows add, validate with the server messages, reorder and remove, and Save sends the same name=url text');
}

async function moreThanTenInstancesIsRefusedAndAddStops() {
  //given
  const ten = Array.from({length: 10}, (_, n) => 'n' + n + '=https://n' + n + '.example.com').join(', ');
  const {w, doc, errors} = await boot(TEMPLATE, '#t=workspaces', [links({value: ten, base: '', source: 'admin'})]);

  //then
  assert.ok(doc.querySelector('#workspaces .ws-add').disabled, 'ten rows is the most');
  assert.strictEqual(rowsOf(doc).length, 10);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the Add button stops at ten instances');
}

async function aListFromTheServiceConfigurationIsEditableDirectlyAndNamesItsRealSource() {
  //given
  const {w, doc, errors, calls} = await boot(TEMPLATE, '#t=workspaces', [links({value: 'staging=' + STAGING, base: 'staging=' + STAGING, source: 'config', from: 'config file'})]);

  //then
  const tag = doc.querySelector('#workspaces .ws-src .tag');
  assert.strictEqual(tag.textContent, 'config file', 'the tag names the real source');
  assert.strictEqual(tag.title, 'A value saved here wins over it');
  assert.ok(!rowsOf(doc)[0].querySelector('.ws-name').readOnly && !rowsOf(doc)[0].querySelector('.ws-url').readOnly, 'the rows are editable at once');
  assert.ok(doc.querySelector('#workspaces .ws-add') && doc.querySelector('#workspaces .ws-rm') && doc.querySelector('#workspaces .ws-save'));
  assert.ok(!doc.querySelector('#workspaces .ws-edit'), 'there is no Edit here gate');
  assert.deepStrictEqual(puts(calls), []);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a configuration list is editable at once and the tag names its source');
}

async function aCommaOnlyListIsNotUnsavedAtLoadAndTheCountRuleStopsTheEleventhRow() {
  //given
  const docs = 'staging=https://staging.example.com,eu=https://eu.example.com';
  const {w, doc, errors} = await boot(TEMPLATE, '#t=workspaces', [links({value: docs, base: docs, source: 'admin'})]);

  //then
  assert.ok(doc.querySelector('#workspaces .ws-save').disabled, 'the docs example with no spaces is not unsaved at load');
  assert.ok(doc.querySelector('#workspaces .ws-discard').hidden);
  w.close();

  //given
  const ten = Array.from({length: 10}, (_, n) => 'n' + n + '=https://n' + n + '.example.com').join(', ');
  const full = await boot(TEMPLATE, '#t=workspaces', [links({value: ten, base: ten, source: 'admin'})]);

  //when
  const rows = [...full.doc.querySelectorAll('#workspaces .ws-row')];
  assert.ok(full.doc.querySelector('#workspaces .ws-add').disabled);
  full.w.INST_DRAFT.push({name: 'n10', url: 'https://n10.example.com'});
  full.w.renderInstances();

  //then
  const last = [...full.doc.querySelectorAll('#workspaces .ws-row')].pop();
  assert.strictEqual(rows.length, 10);
  assert.strictEqual(last.querySelector('.ws-why').textContent, 'at most 10 other instances can be listed'.replace('instances', 'workspaces'), 'the count rule shows on the eleventh row');
  assert.ok(full.doc.querySelector('#workspaces .ws-save').disabled);
  assert.deepStrictEqual(errors.concat(full.errors), []);
  full.w.close();
  console.log('PASS a comma-only list is not unsaved at load and the count rule stops the eleventh row');
}

async function aRevertFromChangesRefreshesTheDraftAndRemovingTheLastRowFocusesAdd() {
  //given
  const {w, doc, errors} = await boot(TEMPLATE, '#t=workspaces', [links({value: 'staging=' + STAGING, base: '', source: 'admin'})]);
  await addRow(w, doc, 'eu', 'https://eu.example.com');

  //when
  const item = Object.assign({}, w.ADMIN.items[0], {value: 'asia=http://asia.example.com'});
  await w.adminDone({item}, 'Reverted', true);

  //then
  assert.deepStrictEqual(vals(doc), [['asia', 'http://asia.example.com']], 'a Revert replaces the draft with the stored list');

  //when
  rowsOf(doc)[0].querySelector('.ws-rm').click();

  //then
  assert.strictEqual(doc.activeElement, doc.querySelector('#workspaces .ws-add'), 'removing the last row moves focus to Add workspace');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS Revert refreshes the draft and removing the last row focuses Add workspace');
}

async function theLimitsTheResetTitleAndTheAtMostSentenceReadInGerman() {
  //given
  const {w, doc, errors} = await boot(TEMPLATE, '#t=workspaces', [links({value: 'x=https://x.example.com', base: 'staging=' + STAGING, source: 'admin', by: 'nina.white@example.com', at: new Date().toISOString()})], {lang: 'de'});
  const p = pack(TEMPLATE, 'de');

  //then
  assert.strictEqual(doc.querySelector('#workspaces .ws-max').textContent, p['At most {0} workspaces. A name is letters, digits, spaces, dots, dashes or underscores; an address is http:// or https:// without a user name or password.'].replace('{0}', '10'));
  assert.strictEqual(doc.querySelector('#workspaces .ws-reset').title, p['Drop the list saved here and go back to the configured list']);
  assert.ok(p['Drop the list saved here and go back to the default']);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the at most sentence and the Reset title read in German');
}

async function resetGoesBackToTheConfigurationList() {
  //given
  const {w, doc, errors, calls} = await boot(TEMPLATE, '#t=workspaces', [links({value: 'x=https://x.example.com', base: 'staging=' + STAGING, source: 'admin', by: 'nina.white@example.com', at: new Date().toISOString()})]);

  //when
  doc.querySelector('#workspaces .ws-reset').click();
  await wait(80);

  //then
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'DELETE').map((c) => c[1]), ['/api/admin/settings?key=core.workspace-links']);
  assert.deepStrictEqual(vals(doc), [['staging', STAGING]]);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS Reset drops the list set here and shows the configuration list');
}

async function aRefusedSaveShowsTheServerReasonAndKeepsTheRows() {
  //given
  const {w, doc, errors} = await boot(TEMPLATE, '#t=workspaces', [links({value: '', base: '', source: 'default'})], {refuse: () => 'Other workspaces, entry 1: nope'});
  await addRow(w, doc, 'eu', 'https://eu.example.com');

  //when
  doc.querySelector('#workspaces .ws-save').click();
  await wait(80);

  //then
  assert.strictEqual(doc.querySelector('#workspaces .ws-msg').textContent, 'Other workspaces, entry 1: nope');
  assert.deepStrictEqual(vals(doc), [['eu', 'https://eu.example.com']]);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a refused save keeps the rows and shows the reason');
}

async function theSectionIsTranslatedAndKeyboardReachable() {
  for (const lang of ['de', 'es', 'fr', 'tr']) {
    //given
    const en = await boot(TEMPLATE, '#t=workspaces', [links({value: 'staging=' + STAGING, base: '', source: 'admin'})]);
    const {w, doc, errors} = await boot(TEMPLATE, '#t=workspaces', [links({value: 'staging=' + STAGING, base: '', source: 'admin'})], {lang});

    //then
    const words = (d, sel, attr) => [...d.querySelectorAll(sel)].map((e) => attr ? e.getAttribute(attr) : e.textContent);
    for (const [sel, attr] of [['#workspaces .ws-add'], ['#workspaces .ws-rm'], ['#workspaces .ws-open'], ['#workspaces .ws-url', 'aria-label'], ['#workspaces .ws-up', 'aria-label'], ['#workspaces .ws-down', 'aria-label'], ['#workspaces .ws-me .ws-h']]) {
      assert.notDeepStrictEqual(words(doc, sel, attr), words(en.doc, sel, attr), lang + ' translates ' + sel);
    }
    assert.strictEqual(doc.querySelector('#workspaces .ws-name').getAttribute('aria-label'), pack(TEMPLATE, lang)['Name'], lang + ' translates the name field');
    assert.notStrictEqual(doc.querySelector('#panel-workspaces .panel-note').textContent, en.doc.querySelector('#panel-workspaces .panel-note').textContent, lang + ' translates the sentence');
    assert.strictEqual(doc.querySelector('#tab-workspaces .lbl').textContent, pack(TEMPLATE, lang)['Workspaces'], lang + ' translates the tab');
    assert.deepStrictEqual(errors, []);
    w.close();
    en.w.close();
  }
  console.log('PASS the Workspaces section reads in de, es, fr and tr');
}

(async () => {
  const tests = [instancesAreTheirOwnAdminSectionWithTheDeploymentNameAndTheListRows, addingValidatingReorderingAndRemovingRowsSendsTheSameNameEqualsUrlText, moreThanTenInstancesIsRefusedAndAddStops, aCommaOnlyListIsNotUnsavedAtLoadAndTheCountRuleStopsTheEleventhRow, aRevertFromChangesRefreshesTheDraftAndRemovingTheLastRowFocusesAdd, theLimitsTheResetTitleAndTheAtMostSentenceReadInGerman,
    aListFromTheServiceConfigurationIsEditableDirectlyAndNamesItsRealSource, resetGoesBackToTheConfigurationList, aRefusedSaveShowsTheServerReasonAndKeepsTheRows, theSectionIsTranslatedAndKeyboardReachable];
  for (const t of tests) {
    try { await t(); } catch (e) { console.log('FAIL ' + (e && e.message ? e.message : e)); console.log(e && e.stack); process.exitCode = 1; return; }
  }
})();
