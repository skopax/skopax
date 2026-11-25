const assert = require('assert');
const {masks, inlineMask} = require('./mask');
const {boot, settings, connection, vertex, vertexTest, vertexLast, REGIONS, nameOf, typeIn, wait, iso, GROUPS, MODELS} = require('./skopax-ai-kit');

const TEMPLATE = process.argv[2];
const MARKER = 'sk-leak-' + 'v'.repeat(32);
const TYPED = '{"type":"service_account","private_key":"typed-' + 'k'.repeat(24) + '"}';
const FED = '{"type":"external_account","credential_source":{"file":"/var/run/secrets/tokens/example"}}';
const NOT_FIT = 'Answers, but did not call the tool: not fit for investigations';
const REGION_NOTE = 'There is no access to claude-opus-5-5 in europe-west1: pick a region that serves it (newer models need global, us or eu)';

const at = iso(30);
const one = () => vertex('eu', {lastTest: Object.assign(vertexLast(), {at})});
const fed = () => vertex('fed', {credentials: {mode: 'federation', set: true}, project: 'shop-fed-2', region: 'europe-west4', company: 'Google Cloud Vertex AI (europe-west4)'});
const pod = () => vertex('pod', {credentials: {mode: 'pod', set: false}, project: 'shop-pod-3', models: []});
const three = () => [one(), fed(), pod()];

const card = (doc, name) => doc.querySelector('#ai .aiv-card[data-vertex="' + name + '"]');
const addForm = (doc) => doc.querySelector('#ai .aiv-add .aiv-card');
const q = (c, s) => c.querySelector(s);
const models = (c) => [...c.querySelectorAll('.aiv-model')].map((m) => [q(m, '.aiv-m-name').value, q(m, '.aiv-m-vid').value, q(m, '.aiv-m-in').value, q(m, '.aiv-m-out').value]);
const pick = (w, sel, value) => { sel.value = value; sel.dispatchEvent(new w.Event('change', {bubbles: true})); };
const puts = (calls) => calls.filter((c) => c[0] === 'PUT' && c[1].indexOf('/api/admin/ai-vertex/') === 0).map((c) => [c[1], c[2]]);

async function aVertexCardShowsItsFieldsPerMode() {
  //given
  const {dom, w, doc, errors} = await boot(TEMPLATE, {vertex: three()});
  const bare = await boot(TEMPLATE);

  //when
  const eu = card(doc, 'eu'), fd = card(doc, 'fed'), pd = card(doc, 'pod');
  const controls = [...doc.querySelectorAll('#ai .aiv input, #ai .aiv select, #ai .aiv button, #ai .aiv textarea, #ai .aiv summary')];

  //then
  assert.deepStrictEqual([...doc.querySelectorAll('#ai .aiv-card[data-vertex]')].map((c) => c.dataset.vertex), ['eu', 'fed', 'pod']);
  assert.strictEqual(doc.querySelectorAll('#ai .ai-providers > li').length, 2, 'the Anthropic and OpenAI key cards stay as they are');
  assert.ok(doc.querySelector('#ai .ai-providers').compareDocumentPosition(eu) & 4, 'the Vertex cards follow them');
  assert.strictEqual(doc.querySelectorAll('#ai .aic-card[data-conn]').length, 0, 'and are not compatible connections');
  assert.strictEqual(q(eu, 'h3').textContent, 'eu');
  assert.strictEqual(eu.getAttribute('aria-labelledby'), q(eu, 'h3').id);
  assert.strictEqual(q(eu, '.aiv-project').value, 'shop-prod-1');
  assert.deepStrictEqual([...q(eu, '.aiv-region').options].map((o) => o.value), [''].concat(REGIONS), 'the region is a select of the regions the server lists');
  assert.strictEqual(q(eu, '.aiv-region').value, 'europe-west1');
  assert.deepStrictEqual([...q(eu, '.aiv-region').options].slice(1, 5).map((o) => o.textContent), ['global (current models, no residency guarantee)', 'us (current models, processing stays in the US)', 'eu (current models, processing stays in the EU)', 'us-east5'], 'the regions current models run on say so and the single regions are plain ids');
  assert.ok(/older models/.test(q(eu, '.aiv-region-note').textContent) && /keep processing in the EU/.test(q(eu, '.aiv-region-note').textContent));
  assert.strictEqual(q(fd, '.aiv-region').value, 'europe-west4');
  assert.deepStrictEqual([...q(eu, '.aiv-mode').options].map((o) => [o.value, o.textContent]), [['key', 'Service-account key'], ['federation', 'Workload identity federation'], ['pod', 'The pod\'s Google identity']]);
  assert.deepStrictEqual([eu, fd, pd].map((c) => q(c, '.aiv-mode').value), ['key', 'federation', 'pod']);
  assert.deepStrictEqual([eu, fd, pd].map((c) => q(c, '.aiv-co-v').textContent), ['Google Cloud Vertex AI (europe-west1)', 'Google Cloud Vertex AI (europe-west4)', 'Google Cloud Vertex AI (europe-west1)']);
  assert.deepStrictEqual([eu, fd, pd].map((c) => q(c, '.aiv-credtag').textContent), ['key set', 'config set', 'pod identity']);
  assert.deepStrictEqual([eu, fd, pd].map((c) => [!!q(c, '.aiv-key-in'), !!q(c, '.aiv-fed-in')]), [[true, false], [false, true], [false, false]], 'the key field only in key mode, the federation field only in federation mode, nothing secret in pod mode');
  assert.strictEqual(pd.querySelectorAll('input[type="password"], textarea').length, 0);
  assert.strictEqual(doc.querySelector('label[for="' + q(eu, '.aiv-key-in').id + '"]').textContent, 'New key');
  assert.strictEqual(doc.querySelector('label[for="' + q(fd, '.aiv-fed-in').id + '"]').textContent, 'New federation config (JSON)');
  assert.ok(q(pd, '.aiv-mode-note').textContent.indexOf('Nothing secret is stored') === 0);
  assert.ok(q(fd, '.aiv-mode-note').textContent.indexOf('under the directory set by SKOPAX_VERTEX_TOKEN_DIR') >= 0);
  assert.deepStrictEqual(models(eu), [['claude-opus-5-5', 'claude-opus-5-5@20261101', '5', '25']]);
  assert.deepStrictEqual(models(pd), [['', '', '', '']], 'a connection with no model shows one empty row');
  assert.deepStrictEqual([...eu.querySelectorAll('.aiv-m-ref')].map((r) => r.textContent), ['eu/claude-opus-5-5'], 'with the reference a use picks: the connection name and the model');
  assert.ok(/check your Google Cloud price sheet/i.test(q(eu, '.aiv-price-note').textContent));
  assert.strictEqual(q(eu, '.aic-last-v').textContent, '\u2713 Answers, uses tools \u00b7 ' + w.dayhm(at));
  assert.strictEqual(q(fd, '.aic-last-v').textContent, 'Not tested yet');
  assert.deepStrictEqual([...q(eu, '.aiv-res').children].map((li) => li.textContent), ['claude-opus-5-5 \u2713 Answers, uses tools'], 'the last test shows its result per model');
  assert.deepStrictEqual([...q(fd, '.aiv-res').children].length, 0);
  assert.ok(q(eu, '.aiv-save') && q(eu, '.aiv-test') && q(eu, '.aiv-del'));
  assert.strictEqual(doc.querySelector('#ai .aiv-add summary').textContent, 'Add Vertex AI connection');
  assert.deepStrictEqual(controls.filter((c) => !nameOf(doc, c)).map((c) => c.outerHTML.slice(0, 80)), [], 'every Vertex control has a name');
  assert.deepStrictEqual([...doc.querySelectorAll('#ai .aiv input[type="password"]')].map((i) => i.className), ['aiv-key-in', 'aiv-key-in'], 'the key field of the card and of the Add form are the only password fields');
  assert.strictEqual(bare.doc.querySelectorAll('#ai .aiv, #ai .aiv-card').length, 0, 'a server without Vertex gets no Vertex section');
  assert.deepStrictEqual(errors.concat(bare.errors), []);
  dom.window.close();
  bare.dom.window.close();
  return 'each Vertex connection has a card with project, region select, credentials mode, the key field only in key mode, the federation field only in federation mode, models with Vertex ids and prices, the price-sheet note, last test and actions, followed by Add';
}

async function switchingTheModeSwapsTheSecretField() {
  //given
  const {dom, w, doc, errors, calls} = await boot(TEMPLATE, {vertex: three()});
  const eu = card(doc, 'eu');
  typeIn(w, q(eu, '.aiv-key-in'), TYPED);

  //when
  pick(w, q(eu, '.aiv-mode'), 'federation');
  const asFed = [!!q(eu, '.aiv-key-in'), !!q(eu, '.aiv-fed-in'), q(eu, '.aiv-fed-in') && q(eu, '.aiv-fed-in').value];
  pick(w, q(eu, '.aiv-mode'), 'pod');
  const asPod = [!!q(eu, '.aiv-key-in'), !!q(eu, '.aiv-fed-in')];
  pick(w, q(eu, '.aiv-mode'), 'key');
  const back = [!!q(eu, '.aiv-key-in'), q(eu, '.aiv-key-in').value, q(eu, '.aiv-key-in').type];
  pick(w, q(eu, '.aiv-mode'), 'pod');
  q(eu, '.aiv-save').click();
  await wait(150);

  //then
  assert.deepStrictEqual(asFed, [false, true, '']);
  assert.deepStrictEqual(asPod, [false, false]);
  assert.deepStrictEqual(back, [true, '', 'password'], 'a key typed before a mode change is dropped, not kept in a hidden field');
  assert.ok(doc.body.innerHTML.indexOf('typed-kkkk') < 0);
  assert.deepStrictEqual(puts(calls)[0], ['/api/admin/ai-vertex/eu', {project: 'shop-prod-1', region: 'europe-west1', credentials: {mode: 'pod'}, models: [{id: 'claude-opus-5-5', vertexId: 'claude-opus-5-5@20261101', input: 5, output: 25}]}], 'pod mode sends no secret');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'changing the credentials mode swaps the secret field, drops what was typed and sends nothing secret in pod mode';
}

async function addingAConnectionSendsOnlyTypedValues() {
  //given
  const {dom, w, doc, errors, calls} = await boot(TEMPLATE, {vertex: three()});

  //when
  doc.querySelector('#ai .aiv-add summary').click();
  q(addForm(doc), '.aiv-save').click();
  await wait(40);
  const empty = q(addForm(doc), '.aiv-msg').textContent;
  typeIn(w, q(addForm(doc), '.aiv-n'), 'EU');
  q(addForm(doc), '.aiv-save').click();
  await wait(40);
  const taken = q(addForm(doc), '.aiv-msg').textContent;
  typeIn(w, q(addForm(doc), '.aiv-n'), 'west');
  q(addForm(doc), '.aiv-save').click();
  await wait(40);
  const noProject = q(addForm(doc), '.aiv-msg').textContent;
  typeIn(w, q(addForm(doc), '.aiv-project'), ' shop-new-4 ');
  q(addForm(doc), '.aiv-save').click();
  await wait(40);
  const noRegion = q(addForm(doc), '.aiv-msg').textContent;
  pick(w, q(addForm(doc), '.aiv-region'), 'europe-west4');
  typeIn(w, q(addForm(doc), '.aiv-key-in'), TYPED);
  typeIn(w, q(addForm(doc), '.aiv-m-name'), 'claude-sonnet-5-5');
  typeIn(w, q(addForm(doc), '.aiv-m-vid'), ' claude-sonnet-5-5@20261101 ');
  typeIn(w, q(addForm(doc), '.aiv-m-in'), '3');
  q(addForm(doc), '.aiv-save').click();
  await wait(150);
  const added = card(doc, 'west');
  const said = [added && q(added, '.aiv-msg').textContent, doc.activeElement === (added && q(added, '.aiv-save')), q(addForm(doc), '.aiv-n').value];
  typeIn(w, q(card(doc, 'fed'), '.aiv-fed-in'), FED);
  q(card(doc, 'fed'), '.aiv-save').click();
  await wait(150);
  const eu = card(doc, 'eu');
  typeIn(w, q(addForm(doc), '.aiv-n'), 'draft');
  typeIn(w, q(eu, '.aiv-project'), 'shop-prod-9');
  q(eu, '.aiv-save').click();
  await wait(150);
  const kept = [q(addForm(doc), '.aiv-n').value, card(doc, 'eu') !== eu, q(card(doc, 'eu'), '.aiv-msg').textContent];

  //then
  const sent = puts(calls);
  assert.strictEqual(empty, 'Enter a name');
  assert.strictEqual(taken, 'That name is already used by another connection: pick another one', 'a name in use is refused before anything is sent');
  assert.strictEqual(noProject, 'Enter a project id');
  assert.strictEqual(noRegion, 'Choose a region');
  assert.deepStrictEqual(sent[0], ['/api/admin/ai-vertex/west', {project: 'shop-new-4', region: 'europe-west4', credentials: {mode: 'key', key: TYPED}, models: [{id: 'claude-sonnet-5-5', vertexId: 'claude-sonnet-5-5@20261101', input: 3}]}], 'a new connection sends what was typed, with no output price when none was typed');
  assert.deepStrictEqual(sent[1], ['/api/admin/ai-vertex/fed', {project: 'shop-fed-2', region: 'europe-west4', credentials: {mode: 'federation', config: FED}, models: [{id: 'claude-opus-5-5', vertexId: 'claude-opus-5-5@20261101', input: 5, output: 25}]}], 'a typed federation config is sent');
  assert.deepStrictEqual(sent[2], ['/api/admin/ai-vertex/eu', {project: 'shop-prod-9', region: 'europe-west1', credentials: {mode: 'key'}, models: [{id: 'claude-opus-5-5', vertexId: 'claude-opus-5-5@20261101', input: 5, output: 25}]}], 'saving a card without a typed key sends no key, so the saved one is kept');
  assert.strictEqual(sent.length, 3);
  assert.deepStrictEqual(said, ['Saved', true, ''], 'the new connection gets its card with Saved and the focus on its Save, and the Add form starts empty again');
  assert.deepStrictEqual(kept, ['draft', true, 'Saved'], 'saving one card redraws only that card');
  assert.strictEqual(q(card(doc, 'west'), '.aiv-key-in').value, '', 'the key field is empty again');
  assert.ok(doc.body.innerHTML.indexOf('typed-kkkk') < 0 && doc.body.innerHTML.indexOf(FED) < 0, 'and the typed key and config are kept nowhere in the page');
  assert.ok(calls.filter((c) => c[1] === '/api/admin/ai-uses').length >= 3, 'the uses table is read again after a save');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'Save sends the project, region, credentials and models with only the typed prices, the key or config only when typed; a missing name, project or region and a name in use are refused before sending';
}

async function aRefusedSaveShowsTheDetail() {
  //given
  const {dom, w, doc, errors, srv} = await boot(TEMPLATE, {vertex: three()});
  const eu = card(doc, 'eu');
  srv.refuse = 'The project changed: enter the key again';

  //when
  typeIn(w, q(eu, '.aiv-project'), 'shop-other-5');
  typeIn(w, q(eu, '.aiv-key-in'), TYPED);
  q(eu, '.aiv-save').click();
  await wait(120);
  const now = card(doc, 'eu');

  //then
  assert.strictEqual(now, eu, 'the card is not drawn again');
  assert.strictEqual(q(now, '.aiv-msg').textContent, 'The project changed: enter the key again');
  assert.ok(q(now, '.aiv-msg').classList.contains('bad'));
  assert.strictEqual(q(now, '.aiv-project').value, 'shop-other-5', 'the typed value stays for fixing');
  assert.strictEqual(q(now, '.aiv-key-in').value, '', 'but a typed key is cleared from the field');
  assert.ok(!q(now, '.aiv-save').disabled, 'Save can be tried again');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a refused save shows the server detail on its card and keeps what was typed';
}

async function aRunningTestOnTheAddFormDoesNotDisableTheNextOne() {
  //given
  const {dom, w, doc, errors, srv} = await boot(TEMPLATE, {vertex: three()});
  let release;
  srv.vhold = new Promise((r) => { release = r; });
  doc.querySelector('#ai .aiv-add summary').click();
  typeIn(w, q(addForm(doc), '.aiv-n'), 'west');
  typeIn(w, q(addForm(doc), '.aiv-project'), 'shop-new-4');
  pick(w, q(addForm(doc), '.aiv-region'), 'europe-west4');

  //when
  q(addForm(doc), '.aiv-test').click();
  await wait(20);
  const running = q(addForm(doc), '.aiv-test').disabled;
  q(addForm(doc), '.aiv-save').click();
  await wait(150);
  const next = q(addForm(doc), '.aiv-test').disabled;
  release();
  await wait(40);

  //then
  assert.deepStrictEqual([running, next], [true, false], 'a new Add form has its own test state');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a test running on one Add form does not disable the Test button of the form that replaces it';
}

async function testShowsThePlainResult() {
  //given
  const {dom, w, doc, errors, calls, srv} = await boot(TEMPLATE, {vertex: three()});
  let release;
  srv.vhold = new Promise((r) => { release = r; });
  const eu = card(doc, 'eu');
  const msg = () => q(card(doc, 'eu'), '.aiv-msg');

  //when
  q(eu, '.aiv-test').click();
  await wait(10);
  const during = [q(eu, '.aiv-test').disabled, msg().textContent];
  q(eu, '.aiv-test').click();
  srv.vhold = null;
  release();
  await wait(80);
  const passed = [msg().textContent, msg().classList.contains('bad')];
  srv.vtest = vertexTest({ok: false, summary: REGION_NOTE, models: [{id: 'claude-opus-5-5', ok: false, summary: REGION_NOTE}, {id: 'claude-haiku-5-5', ok: true, summary: 'Answers, uses tools'}], ms: 812});
  srv.vlast = Object.assign(vertexLast(srv.vtest), {at: iso(0)});
  q(card(doc, 'eu'), '.aiv-test').click();
  await wait(80);
  const failed = [msg().textContent, msg().classList.contains('bad'), q(card(doc, 'eu'), '.aic-last-v').textContent, [...q(card(doc, 'eu'), '.aiv-res').children].map((li) => [li.textContent, li.className])];
  srv.vtest = vertexTest({tools: false, summary: 'Answers', ms: 200});
  q(card(doc, 'eu'), '.aiv-test').click();
  await wait(80);
  const noTool = [msg().textContent, msg().classList.contains('bad')];

  //then
  const tests = calls.filter((c) => c[1] === '/api/admin/ai-vertex/eu/test');
  assert.deepStrictEqual(during, [true, 'Testing\u2026'], 'the button waits while the test runs');
  assert.strictEqual(tests.length, 3, 'a click while a test runs sends nothing');
  assert.deepStrictEqual([tests[0][0], tests[0][2]], ['POST', {project: 'shop-prod-1', region: 'europe-west1', credentials: {mode: 'key'}, models: [{id: 'claude-opus-5-5', vertexId: 'claude-opus-5-5@20261101', input: 5, output: 25}]}], 'the test sends the values on the card');
  assert.deepStrictEqual(passed, ['\u2713 Answers, uses tools \u00b7 300 ms', false]);
  assert.deepStrictEqual(failed.slice(0, 2), [REGION_NOTE + ' \u00b7 812 ms', true], 'a failed check reads in plain words, with no check mark');
  assert.strictEqual(failed[2], REGION_NOTE + ' \u00b7 ' + w.dayhm(srv.vlast.at), 'and the Last test line follows');
  assert.deepStrictEqual(failed[3], [['claude-opus-5-5 ' + REGION_NOTE, 'aiv-res-m bad'], ['claude-haiku-5-5 \u2713 Answers, uses tools', 'aiv-res-m ok']], 'each model has its own result on the card');
  assert.deepStrictEqual(noTool, ['Answers \u00b7 200 ms \u00b7 Not fit for investigations: the model did not call the tool', true], 'an answer without the tool call is not fit, even when ok');
  assert.ok(calls.filter((c) => c[1] === '/api/admin/ai-uses').length >= 3, 'the uses table is read again, since a failed test is a problem there');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'Test connection sends the card values, waits while it runs, shows the plain result with the time and refreshes the Last test line';
}

async function deleteAsksFirstWithTheHeldWording() {
  //given
  const {dom, w, doc, errors, calls} = await boot(TEMPLATE, {vertex: three()});
  const wrap = doc.getElementById('cf-wrap');
  const del = () => q(card(doc, 'eu'), '.aiv-del');

  //when
  typeIn(w, q(card(doc, 'fed'), '.aiv-project'), 'shop-typed-6');
  del().click();
  await wait(20);
  const asked = [wrap.hidden, doc.getElementById('cf-title').textContent, doc.getElementById('cf-yes').textContent, doc.getElementById('cf-text').textContent];
  doc.getElementById('cf-no').click();
  await wait(20);
  const back = doc.activeElement === del();
  del().click();
  await wait(20);
  doc.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  await wait(20);
  const kept = calls.filter((c) => c[0] === 'DELETE').length;
  del().click();
  await wait(20);
  doc.getElementById('cf-yes').click();
  await wait(150);

  //then
  assert.deepStrictEqual(asked.slice(0, 3), [false, 'Delete this Vertex AI connection?', 'Delete connection']);
  assert.ok(asked[3].indexOf('A use set to one of its models is held until another model is picked.') >= 0, 'the held wording of the compatible connections');
  assert.strictEqual(kept, 0, 'Cancel and Esc send nothing');
  assert.ok(back, 'and the focus goes back to Delete');
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'DELETE').map((c) => c[1]), ['/api/admin/ai-vertex/eu']);
  assert.ok(!card(doc, 'eu') && card(doc, 'fed'), 'the card is gone');
  assert.strictEqual(q(card(doc, 'fed'), '.aiv-project').value, 'shop-typed-6', 'the other cards keep what was typed');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'Delete asks in the confirm dialog with the held wording, Cancel or Esc keeps the connection, and confirming sends DELETE and removes only that card';
}

async function aSavedKeyOrConfigIsNeverShown() {
  //given
  const leaky = vertex('eu', {credentials: {mode: 'key', set: true, key: MARKER, value: MARKER, secret: MARKER}, key: MARKER, secret: MARKER});
  const leakyFed = vertex('fed', {credentials: {mode: 'federation', set: true, config: MARKER}, config: MARKER});
  const {dom, w, doc, errors} = await boot(TEMPLATE, {vertex: [leaky, leakyFed]});
  const input = q(card(doc, 'eu'), '.aiv-key-in'), add = q(addForm(doc), '.aiv-key-in');
  const leaked = () => [...doc.querySelectorAll('input, textarea')].some((i) => i.value.indexOf(MARKER) >= 0) || doc.body.innerHTML.indexOf(MARKER) >= 0;

  //then
  assert.strictEqual(input.value, '', 'the key field is empty');
  assert.strictEqual(q(card(doc, 'fed'), '.aiv-fed-in').value, '', 'and so is the federation field');
  assert.strictEqual(input.placeholder, 'Paste a new key');
  assert.ok(!leaked(), 'nothing from a response reaches the page');
  [input, add].forEach((i) => {
    assert.deepStrictEqual([i.type, i.getAttribute('autocomplete'), i.getAttribute('spellcheck'), i.getAttribute('autocapitalize'), i.getAttribute('autocorrect')], ['password', 'new-password', 'false', 'off', 'off']);
    assert.deepStrictEqual(['data-1p-ignore', 'data-lpignore', 'data-bwignore', 'data-form-type'].map((a) => i.getAttribute(a)), ['', 'true', '', 'other']);
    assert.deepStrictEqual(masks(doc, i), [{onScreen: true, value: 'disc'}], 'one page rule masks it');
    assert.strictEqual(inlineMask(i), null);
  });

  //when
  typeIn(w, input, TYPED);
  q(card(doc, 'eu'), '.aiv-save').click();
  await wait(150);

  //then
  assert.strictEqual(q(card(doc, 'eu'), '.aiv-key-in').value, '');
  assert.ok(!leaked() && doc.body.innerHTML.indexOf('typed-kkkk') < 0, 'after Save neither a saved nor the typed secret is in the page');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a saved key or config is never written into the page; the key field is an empty masked field with the password-manager opt-outs';
}

async function vertexModelsAppearInWhoDoesWhat() {
  //given
  const groups = Object.assign({}, GROUPS, {'eu': ['eu/claude-opus-5-5']});
  const choices = MODELS.concat(['eu/claude-opus-5-5']);
  const items = settings().map((i) => i.key === 'investigate.slack-model' ? Object.assign({}, i, {value: 'eu/claude-opus-5-5', choices, choiceGroups: groups}) : i.choices && i.choiceGroups ? Object.assign({}, i, {choices: i.choices.concat(['eu/claude-opus-5-5']), choiceGroups: groups}) : i);
  const {dom, doc, errors} = await boot(TEMPLATE, {vertex: three(), items});

  //when
  const row = doc.querySelector('#ai-rows .aiu-row[data-use="slack"]');
  const sel = row.querySelector('select.aiu-in');
  const group = [...sel.querySelectorAll('optgroup')].filter((g) => g.label === 'eu')[0];

  //then
  assert.ok(group && [...group.querySelectorAll('option')].map((o) => o.value).join() === 'eu/claude-opus-5-5', 'the models of a Vertex connection are listed under their group');
  assert.strictEqual(sel.value, 'eu/claude-opus-5-5');
  assert.strictEqual(row.querySelector('.aiu-prov').textContent, 'Vertex AI', 'the row names its provider');
  assert.strictEqual(row.querySelector('.aiu-co .aiu-v, .aiu-co').textContent.indexOf('Google Cloud Vertex AI (europe-west1)') >= 0, true, 'and the company its data goes to');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the models of a Vertex connection are in the Who does what selects under their own group, tagged Vertex AI with the company the data goes to';
}

async function theSectionHandlesAFailedRead() {
  //given
  const down = await boot(TEMPLATE, {vertex: three(), vertexFail: 500});
  const gone = await boot(TEMPLATE, {vertexFail: 404});
  const odd = await boot(TEMPLATE, {vertex: [one()], regions: ['europe-west9']});

  //then
  assert.strictEqual(down.doc.querySelector('#ai .aiv-failed').textContent, 'The Vertex AI connections did not load; open the tab again.');
  assert.strictEqual(down.doc.querySelectorAll('#ai .aiv-card').length, 0);
  assert.strictEqual(gone.doc.querySelectorAll('#ai .aiv-failed, #ai .aiv-card').length, 0, 'a server without the route says nothing');
  const region = q(odd.doc.querySelector('#ai .aiv-card[data-vertex="eu"]'), '.aiv-region');
  assert.deepStrictEqual([...region.options].map((o) => o.value), ['', 'europe-west9', 'europe-west1'], 'a saved region that the list lacks stays selectable');
  assert.strictEqual(region.value, 'europe-west1');
  assert.deepStrictEqual(down.errors.concat(gone.errors, odd.errors), []);
  [down, gone, odd].forEach((b) => b.dom.window.close());
  return 'a failed read says so, a server without the route shows nothing, and a saved region missing from the list stays selectable';
}

async function theCardIsTranslated() {
  //given
  const want = {
    de: ['Vertex-AI-Verbindung hinzuf\u00fcgen', 'Projekt-ID', 'Region', 'Zugangsdaten'],
    es: ['A\u00f1adir conexi\u00f3n de Vertex AI', 'ID del proyecto', 'Regi\u00f3n', 'Credenciales'],
    fr: ['Ajouter une connexion Vertex AI', 'ID du projet', 'R\u00e9gion', 'Identifiants'],
    tr: ['Vertex AI ba\u011flant\u0131s\u0131 ekle', 'Proje kimli\u011fi', 'B\u00f6lge', 'Kimlik bilgileri']
  };

  //when / then
  for (const lang of Object.keys(want)) {
    const {dom, doc, errors} = await boot(TEMPLATE, {lang, vertex: three()});
    const eu = card(doc, 'eu'), pd = card(doc, 'pod');
    const labels = [...eu.querySelectorAll('.ai-field > label')].map((l) => l.textContent);
    assert.strictEqual(doc.querySelector('#ai .aiv-add summary').textContent, want[lang][0], lang + ' summary');
    assert.deepStrictEqual(labels.slice(0, 3), want[lang].slice(1), lang + ' labels');
    [[q(eu, '.aiv-test'), 'Test connection'], [q(eu, '.aiv-save'), 'Save'], [q(eu, '.aiv-del'), 'Delete'], [q(pd, '.aiv-mode-note'), 'Nothing secret is stored. The pod runs as its own Google identity (for example workload identity on GKE).'],
      [q(eu, '.aiv-price-note'), 'Check your Google Cloud price sheet: regional prices can differ from the Anthropic list prices.'], [q(eu, '.aiv-mh'), 'Models: Skopax name, Vertex model id, prices in USD per million tokens (input, output)'],
      [q(eu, '.aiv-region-note'), 'Current Claude models run on global, us and eu only; a single region serves older models. Choose eu to keep processing in the EU.'], [q(eu, '.aiv-region').options[3], 'eu (current models, processing stays in the EU)'], [q(eu, '.aiv-region').options[1], 'global (current models, no residency guarantee)'],
      [q(eu, '.aiv-mode').options[1], 'Workload identity federation'], [q(eu, '.aiv-mode').options[2], 'The pod\'s Google identity']].forEach(([node, english]) => assert.notStrictEqual(node.textContent, english, lang + ' ' + english));
    assert.notStrictEqual(q(eu, '.aiv-m-vid').getAttribute('aria-label'), 'Model 1 Vertex id', lang + ' model label');
    const note = q(card(doc, 'fed'), '.aiv-mode-note').textContent;
    assert.ok(note.indexOf('SKOPAX_VERTEX_TOKEN_DIR') >= 0 && note.indexOf('allowed mount path') < 0 && note.indexOf('under the directory') < 0, lang + ' federation note');
    assert.notStrictEqual(q(eu, '.aiv-res').getAttribute('aria-label'), 'Test result per model', lang + ' result label');
    assert.deepStrictEqual(errors, []);
    dom.window.close();
  }
  return 'the Vertex card, its notes, labels and Add summary are translated into de, es, fr and tr';
}

(async () => {
  console.log('PASS ' + await aVertexCardShowsItsFieldsPerMode());
  console.log('PASS ' + await switchingTheModeSwapsTheSecretField());
  console.log('PASS ' + await addingAConnectionSendsOnlyTypedValues());
  console.log('PASS ' + await aRefusedSaveShowsTheDetail());
  console.log('PASS ' + await aRunningTestOnTheAddFormDoesNotDisableTheNextOne());
  console.log('PASS ' + await testShowsThePlainResult());
  console.log('PASS ' + await deleteAsksFirstWithTheHeldWording());
  console.log('PASS ' + await aSavedKeyOrConfigIsNeverShown());
  console.log('PASS ' + await vertexModelsAppearInWhoDoesWhat());
  console.log('PASS ' + await theSectionHandlesAFailedRead());
  console.log('PASS ' + await theCardIsTranslated());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
