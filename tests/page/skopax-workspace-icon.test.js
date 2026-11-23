const assert = require('assert');
const {boot, item, links, wait, pack} = require('./skopax-admin-lists-kit');

const TEMPLATE = process.argv[2];
const NONE = {set: false, type: null, size: 0, version: null};
const SVG = {set: true, type: 'image/svg+xml', size: 120, version: 'abc123def4567890'};
const PNG = {set: true, type: 'image/png', size: 2048, version: '0123456789abcdef'};
const LINKS = [{name: 'staging', url: 'https://staging.example.com', letter: 'S', colour: '#5cbf80'}];

function iconItem(value) {
  return item('core.workspace-icon', 'Workspaces', 'Workspace icon', {kind: 'choice', choices: ['letter', 'company', 'upload'], value: value || 'letter', base: 'letter', default: 'letter', card: 'Workspace icon', order: 1});
}

function instance(mode, company, upload) {
  return {name: 'production', label: 'production', from: 'environment', icon: {mode, company: company || NONE, upload: upload || NONE, max: 65536}};
}

const block = (doc) => doc.querySelector('#workspaces .ws-icon');
const modes = (doc) => [...block(doc).querySelectorAll('.ws-icon-mode')].map((l) => [l.querySelector('input').value, l.textContent, l.querySelector('input').checked]);
const pickMode = (w, doc, value) => {
  const radio = block(doc).querySelector('input[value="' + value + '"]');
  radio.checked = true;
  radio.dispatchEvent(new w.Event('change', {bubbles: true}));
};
const pickFile = (w, doc, name, type, size) => {
  const input = block(doc).querySelector('.ws-icon-file');
  const file = new w.File([new Uint8Array(size)], name, {type});
  Object.defineProperty(input, 'files', {configurable: true, value: [file]});
  input.dispatchEvent(new w.Event('change', {bubbles: true}));
};
const tileOf = (el) => el.querySelector('.inst-tile');

async function theIconChoiceSitsUnderThisWorkspaceWithLetterPickedAndSavesAsASetting() {
  //given
  let me = {instances: {current: {name: 'production', letter: 'P'}, links: []}};
  const {w, doc, errors, calls} = await boot(TEMPLATE, '#t=workspaces', [links(), iconItem()], {instance: instance('letter'), me: () => me});

  //then
  assert.ok(block(doc), 'the icon choice is on the Workspaces page');
  assert.strictEqual(block(doc).querySelector('.ws-h').textContent, 'Workspace icon');
  assert.strictEqual(block(doc).querySelector('[role="radiogroup"]').getAttribute('aria-labelledby'), 'ws-icon-h');
  assert.deepStrictEqual(modes(doc), [['letter', 'Letter', true], ['company', 'Company icon', false], ['upload', 'Upload', false]]);
  assert.strictEqual(block(doc).querySelector('.ws-icon-pv').textContent, 'P', 'the preview is the letter tile');
  assert.ok(!block(doc).querySelector('.ws-icon-file'), 'no upload control while Letter is picked');

  //when
  me = {instances: {current: {name: 'production', letter: 'P', icon: '/api/workspace-icon?v=' + SVG.version}, links: []}};
  pickMode(w, doc, 'upload');
  await wait(150);

  //then
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'PUT').map((c) => c[2]), [{key: 'core.workspace-icon', value: 'upload'}]);
  assert.deepStrictEqual(modes(doc).map((m) => m[2]), [false, false, true]);
  assert.strictEqual(block(doc).querySelector('.ws-icon-msg').textContent, 'Saved');
  assert.strictEqual(block(doc).querySelector('.ws-icon-state').textContent, 'None uploaded, so the tile shows the letter.');
  assert.ok(block(doc).querySelector('.ws-icon-file'), 'Upload shows the upload control');
  assert.strictEqual(tileOf(doc.getElementById('inst-btn')).querySelector('img').getAttribute('src'), '/api/workspace-icon?v=' + SVG.version, 'the sidebar tile follows at once');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the icon choice sits under This workspace with Letter picked and saves as a setting');
}

async function companyPointsToAccessWhenNothingIsUploadedAndShowsTheIconWhenItIs() {
  //given
  const bare = await boot(TEMPLATE, '#t=workspaces', [links(), iconItem('company')], {instance: instance('company')});
  const set = await boot(TEMPLATE, '#t=workspaces', [links(), iconItem('company')], {instance: instance('company', PNG)});

  //then
  assert.strictEqual(block(bare.doc).querySelector('.ws-icon-state span').textContent, 'No company icon uploaded yet, so the tile shows the letter.');
  assert.strictEqual(block(bare.doc).querySelector('.ws-icon-pv').textContent, 'P');
  assert.strictEqual(block(set.doc).querySelector('.ws-icon-state').textContent, 'The icon uploaded for the single sign-on button under Access & sign-in.');
  assert.strictEqual(block(set.doc).querySelector('.ws-icon-pv img').getAttribute('src'), '/api/workspace-icon?v=' + PNG.version);
  assert.strictEqual(block(set.doc).querySelector('.ws-icon-pv img').getAttribute('alt'), '');
  assert.ok(!block(set.doc).querySelector('.ws-icon-go'));

  //when
  block(bare.doc).querySelector('.ws-icon-go').click();
  await wait(50);

  //then
  assert.ok(!bare.doc.getElementById('panel-access').hidden, 'the button opens Access & sign-in');
  assert.deepStrictEqual([...bare.errors, ...set.errors], []);
  bare.w.close();
  set.w.close();
  console.log('PASS Company icon points to Access & sign-in when nothing is uploaded and shows the icon when it is');
}

async function anUploadIsCheckedSentRawShownAndRemoved() {
  //given
  let held = NONE;
  const route = (method, p) => {
    if (p !== '/api/admin/workspace/icon') return null;
    held = method === 'PUT' ? SVG : NONE;
    return {icon: {mode: 'upload', company: NONE, upload: held, max: 65536}};
  };
  const {w, doc, errors, calls} = await boot(TEMPLATE, '#t=workspaces', [links(), iconItem('upload')], {instance: instance('upload'), route});

  //when
  pickFile(w, doc, 'big.png', 'image/png', 65537);
  const big = block(doc).querySelector('.ws-icon-msg').textContent;
  pickFile(w, doc, 'logo.gif', 'image/gif', 10);
  const gif = block(doc).querySelector('.ws-icon-msg').textContent;
  pickFile(w, doc, 'logo.svg', 'image/svg+xml', 120);
  await wait(150);

  //then
  assert.strictEqual(big, 'The icon is at most 64 KiB.');
  assert.strictEqual(gif, 'The icon must be a PNG, WebP or SVG image.');
  const put = calls.filter((c) => c[0] === 'PUT' && c[1] === '/api/admin/workspace/icon');
  assert.strictEqual(put.length, 1, 'only the good file is sent');
  assert.ok(put[0][2] instanceof w.File, 'the raw file goes up');
  assert.strictEqual(block(doc).querySelector('.ws-icon-state').textContent, 'SVG, 120 bytes');
  assert.strictEqual(block(doc).querySelector('.ws-icon-msg').textContent, 'Icon uploaded');
  assert.strictEqual(block(doc).querySelector('.ws-icon-pv img').getAttribute('src'), '/api/workspace-icon?v=' + SVG.version);

  //when
  block(doc).querySelector('.ws-icon-pv img').dispatchEvent(new w.Event('error'));

  //then
  assert.strictEqual(block(doc).querySelector('.ws-icon-pv').textContent, 'P', 'a preview that does not load shows the letter');
  assert.ok(!block(doc).querySelector('.ws-icon-pv img'));

  //when
  block(doc).querySelector('.ws-icon-del').click();
  await wait(20);
  const asked = doc.getElementById('cf-title').textContent;
  doc.getElementById('cf-yes').click();
  await wait(150);

  //then
  assert.strictEqual(asked, 'Remove the workspace icon?');
  assert.ok(calls.some((c) => c[0] === 'DELETE' && c[1] === '/api/admin/workspace/icon'));
  assert.strictEqual(block(doc).querySelector('.ws-icon-msg').textContent, 'Icon removed');
  assert.strictEqual(block(doc).querySelector('.ws-icon-pv').textContent, 'P');
  assert.ok(!block(doc).querySelector('.ws-icon-del'), 'nothing left to remove');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS an upload is checked, sent raw, shown in the preview and removed');
}

async function theSidebarButtonMenuAndRailShowTheIconAndLinksStayLetters() {
  //given
  const icon = '/api/workspace-icon?v=' + SVG.version;
  const me = () => ({instances: {current: {name: 'production', letter: 'P', icon}, links: LINKS}});
  const {w, doc, errors} = await boot(TEMPLATE, '#t=anoms', [links()], {me});

  //when
  const btnTile = tileOf(doc.getElementById('inst-btn'));
  const menuTiles = [...doc.querySelectorAll('#inst-menu .inst-tile')];

  //then
  assert.strictEqual(btnTile.querySelector('img').getAttribute('src'), icon);
  assert.strictEqual(btnTile.querySelector('img').getAttribute('alt'), '');
  assert.strictEqual(btnTile.textContent, '', 'the letter is replaced');
  assert.ok(btnTile.classList.contains('has-img'));
  assert.strictEqual(btnTile.getAttribute('aria-hidden'), 'true');
  assert.strictEqual(menuTiles[0].querySelector('img').getAttribute('src'), icon, 'the current entry in the menu shows it too');
  assert.strictEqual(menuTiles[1].querySelector('img'), null, 'a linked workspace stays a letter tile');
  assert.strictEqual(menuTiles[1].textContent, 'S');
  const css = doc.querySelector('style').textContent + [...doc.querySelectorAll('style')].map((s) => s.textContent).join('');
  assert.ok(/\.inst-tile img \{[^}]*object-fit: contain[^}]*border-radius: inherit/.test(css), 'the image fits the tile and keeps its rounding');

  //when
  doc.body.classList.add('is-rail');

  //then
  assert.ok(tileOf(doc.getElementById('inst-btn')).querySelector('img'), 'the collapsed rail shows the same tile');

  //when
  btnTile.querySelector('img').dispatchEvent(new w.Event('error'));

  //then
  assert.strictEqual(btnTile.textContent, 'P', 'a broken image falls back to the letter');
  assert.ok(!btnTile.querySelector('img') && !btnTile.classList.contains('has-img'));
  assert.strictEqual(btnTile.style.getPropertyValue('background-color'), 'rgb(60, 200, 240)');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the sidebar button, the menu and the rail show the icon, and linked workspaces stay letters');
}

async function anIconFromElsewhereIsNeverLoaded() {
  for (const icon of ['https://evil.example.com/x.png', 'javascript:alert(1)', '/api/workspace-icon?v=a"b', '//evil.example.com/api/workspace-icon?v=1', '']) {
    //given
    const {w, doc, errors} = await boot(TEMPLATE, '#t=anoms', [links()], {me: () => ({instances: {current: {name: 'production', letter: 'P', icon}, links: []}})});

    //then
    const tile = tileOf(doc.getElementById('inst-btn'));
    assert.strictEqual(tile.querySelector('img'), null, icon);
    assert.strictEqual(tile.textContent, 'P');
    assert.deepStrictEqual(errors, []);
    w.close();
  }
  console.log('PASS an icon address that is not the workspace icon is never loaded and the letter stays');
}

async function theIconChoiceReadsInEveryLanguage() {
  for (const lang of ['de', 'es', 'fr', 'tr']) {
    //given
    const texts = pack(TEMPLATE, lang);
    const {w, doc, errors} = await boot(TEMPLATE, '#t=workspaces', [links(), iconItem('upload')], {instance: instance('upload'), lang});
    const bare = await boot(TEMPLATE, '#t=workspaces', [links(), iconItem('company')], {instance: instance('company'), lang});

    //then
    assert.strictEqual(block(doc).querySelector('.ws-h').textContent, texts['Workspace icon'], lang);
    assert.deepStrictEqual(modes(doc).map((m) => m[1]), ['Letter', 'Company icon', 'Upload'].map((k) => texts[k]), lang);
    assert.strictEqual(block(doc).querySelector('.ws-icon-state').textContent, texts['None uploaded, so the tile shows the letter.'], lang);
    assert.strictEqual(block(bare.doc).querySelector('.ws-icon-state span').textContent, texts['No company icon uploaded yet, so the tile shows the letter.'], lang);
    assert.strictEqual(block(bare.doc).querySelector('.ws-icon-go').textContent, texts['Upload it under Access & sign-in'], lang);
    const help = texts['PNG, WebP or SVG, at most {0} KiB, shown at {1} px. An SVG may hold only shapes, gradients, clip paths and masks: styles, text, scripts and links outside the file are refused.'];
    assert.strictEqual(block(doc).querySelector('.ws-icon-up > .adm-help:not(.ws-icon-state)').textContent, help.replace('{0}', '64').replace('{1}', '24'), lang + ' shows the upload rules translated');
    for (const key of ['The icon uploaded for the single sign-on button under Access & sign-in.', 'Remove the workspace icon?', 'The workspace tile shows the letter until another image is uploaded.',
      'PNG, WebP or SVG, at most {0} KiB, shown at {1} px. An SVG may hold only shapes, gradients, clip paths and masks: styles, text, scripts and links outside the file are refused.']) {
      assert.ok(texts[key] && texts[key] !== key, lang + ' carries ' + key);
    }
    assert.deepStrictEqual([...errors, ...bare.errors], []);
    w.close();
    bare.w.close();
  }
  console.log('PASS the icon choice reads in de, es, fr and tr');
}

(async () => {
  const tests = [theIconChoiceSitsUnderThisWorkspaceWithLetterPickedAndSavesAsASetting, companyPointsToAccessWhenNothingIsUploadedAndShowsTheIconWhenItIs, anUploadIsCheckedSentRawShownAndRemoved,
    theSidebarButtonMenuAndRailShowTheIconAndLinksStayLetters, anIconFromElsewhereIsNeverLoaded, theIconChoiceReadsInEveryLanguage];
  for (const t of tests) {
    try { await t(); } catch (e) { console.log('FAIL ' + (e && e.message ? e.message : e)); console.log(e && e.stack); process.exitCode = 1; return; }
  }
})();
