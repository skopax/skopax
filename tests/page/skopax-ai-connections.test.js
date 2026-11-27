const assert = require('assert');
const {masks, inlineMask} = require('./mask');
const {boot, settings, connection, nameOf, typeIn, wait, iso} = require('./skopax-ai-kit');

const TEMPLATE = process.argv[2];
const MARKER = 'sk-leak-' + 'z'.repeat(32);
const TYPED = 'sk-test-' + 'c'.repeat(32);
const NOT_FIT = 'Answers, but did not call the tool: not fit for investigations';
const NOT_FIT_NOTE = 'Not fit for investigations: the model did not call the tool';
const INTRO = 'A compatible connection is any AI service that speaks the OpenAI Chat Completions API. Cloud services such as DeepSeek, OpenRouter or Groq work as they are. A model on your own network, such as Ollama, vLLM or LM Studio, needs Private AI addresses turned on first.';
const PRIVATE_ON = 'On: connections may point at private and loopback addresses, such as 192.168.1.20 or localhost. Link-local and cloud metadata addresses stay blocked.';
const PRIVATE_OFF = 'Off: only public addresses are allowed. Turn it on to add a model that runs on your own network.';
const PRIVATE_NEXT = 'Private AI addresses are on. Add your local model here, for example Ollama with the base URL http://192.168.1.20:11434/v1.';
const URL_HELP = 'Examples: a cloud service https://api.deepseek.com/v1, Ollama http://HOST:11434/v1, vLLM http://HOST:8000/v1';
const PRIVATE = 'core.allow-private-ai';
const privateRow = (doc) => doc.querySelector('#ai .aic-private .adm-row[data-key="' + PRIVATE + '"]');

function two() {
  const at = iso(30);
  return {at, connections: [connection('deepseek', {models: [{id: 'deepseek-chat', ref: 'deepseek/deepseek-chat', input: 0.27, output: 1.1}, {id: 'deepseek-reasoner', ref: 'deepseek/deepseek-reasoner', input: null, output: null}],
    lastTest: {ok: true, tools: true, summary: 'Answers, uses tools', model: 'deepseek-chat', at}}), connection('local', {baseUrl: 'http://10.0.0.5:11434/v1', key: {set: false}, images: true, company: '10.0.0.5'})]};
}

const card = (doc, name) => doc.querySelector('#ai .aic-card[data-conn="' + name + '"]');
const models = (c) => [...c.querySelectorAll('.aic-model')].map((m) => [m.querySelector('.aic-m-id').value, m.querySelector('.aic-m-in').value, m.querySelector('.aic-m-out').value]);
const addForm = (doc) => doc.querySelector('#ai .aic-add .aic-card');

async function aConnectionCardShowsItsModelsAndLastTest() {
  //given
  const {at, connections} = two();
  connections.push(connection('groq', {lastTest: {ok: true, tools: false, summary: 'Answers', model: 'groq-chat', at}}));
  const {dom, w, doc, errors} = await boot(TEMPLATE, {connections});
  const bare = await boot(TEMPLATE, {connections, items: settings().filter((i) => i.key !== 'core.allow-private-ai')});

  //when
  const deep = card(doc, 'deepseek'), local = card(doc, 'local'), groq = card(doc, 'groq');
  const names = [...deep.querySelectorAll('.aic-model input, .aic-model button')].map((c) => nameOf(doc, c));

  //then
  assert.strictEqual(doc.querySelectorAll('#ai .ai-providers > li').length, 2, 'the Anthropic and OpenAI key cards stay as they are');
  assert.ok(doc.querySelector('#ai .ai-providers').compareDocumentPosition(deep) & 4, 'the compatible connections follow them');
  assert.deepStrictEqual([...doc.querySelectorAll('#ai .aic-card[data-conn]')].map((c) => c.dataset.conn), ['deepseek', 'local', 'groq']);
  assert.strictEqual(deep.querySelector('h3').textContent, 'deepseek');
  assert.strictEqual(deep.getAttribute('aria-labelledby'), deep.querySelector('h3').id);
  assert.strictEqual(deep.querySelector('.aic-url').value, 'https://api.deepseek.example.com/v1');
  assert.strictEqual(deep.querySelector('.aic-co-v').textContent, 'api.deepseek.example.com');
  assert.deepStrictEqual([deep.querySelector('.aic-keytag').textContent, local.querySelector('.aic-keytag').textContent], ['key set', 'no key']);
  assert.deepStrictEqual(models(deep), [['deepseek-chat', '0.27', '1.1'], ['deepseek-reasoner', '', '']], 'each model with its prices, an unpriced one empty');
  assert.deepStrictEqual(names, ['Model 1 id', 'Model 1 input price, USD per million tokens', 'Model 1 output price, USD per million tokens', 'Remove model 1', 'Model 2 id', 'Model 2 input price, USD per million tokens', 'Model 2 output price, USD per million tokens', 'Remove model 2'], 'each model control is named with its position');
  assert.deepStrictEqual([...deep.querySelectorAll('.aic-m-ref')].map((r) => r.textContent), ['deepseek/deepseek-chat', 'deepseek/deepseek-reasoner'], 'with the reference a use picks');
  assert.deepStrictEqual([deep.querySelector('.aic-tools').checked, deep.querySelector('.aic-images').checked, local.querySelector('.aic-tools').checked, local.querySelector('.aic-images').checked], [true, false, true, true]);
  assert.strictEqual(deep.querySelector('.aic-last-v').textContent, '\u2713 Answers, uses tools \u00b7 deepseek-chat \u00b7 ' + w.dayhm(at), 'the last test, its model and when');
  assert.ok(deep.querySelector('.aic-last-v').classList.contains('ok'));
  assert.strictEqual(groq.querySelector('.aic-last-v').textContent, 'Answers \u00b7 groq-chat \u00b7 ' + w.dayhm(at) + ' \u00b7 ' + NOT_FIT_NOTE, 'a last test without the tool call is not fit, whatever ok says');
  assert.ok(groq.querySelector('.aic-last-v').classList.contains('bad'));
  assert.strictEqual(local.querySelector('.aic-last-v').textContent, 'Not tested yet');
  assert.ok(deep.querySelector('.aic-del') && deep.querySelector('.aic-save') && deep.querySelector('.aic-test'));
  assert.strictEqual(doc.querySelector('#ai .aic-add summary').textContent, 'Add compatible connection');
  assert.strictEqual(doc.querySelector('#ai .aic-intro').textContent, INTRO, 'an intro says what a compatible connection is and when Private AI addresses is needed');
  assert.ok(privateRow(doc) && privateRow(doc).querySelector('input.adm-sw[role="switch"]'), 'the Private AI addresses switch sits in the connections section');
  assert.strictEqual(doc.querySelector('#ai .aic-private-t').textContent, PRIVATE_OFF);
  const order = [doc.querySelector('#ai .aic-intro'), doc.querySelector('#ai .aic-private'), doc.querySelector('#ai .aic-list')];
  assert.ok((order[0].compareDocumentPosition(order[1]) & 4) && (order[1].compareDocumentPosition(order[2]) & 4), 'intro, then the switch, then the connections');
  assert.ok(!doc.querySelector('#ai-rows .adm-row[data-key="' + PRIVATE + '"]'), 'the switch is not listed again below the table');
  assert.ok(bare.doc.querySelector('#ai .aic-private-t') && !bare.doc.querySelector('#ai .aic-private .adm-row'), 'a server without the setting shows the state and no switch');
  const controls = [...doc.querySelectorAll('#ai .aic input, #ai .aic select, #ai .aic button, #ai .aic summary')];
  assert.deepStrictEqual(controls.filter((c) => !nameOf(doc, c)).map((c) => c.outerHTML.slice(0, 80)), [], 'every connection control has a name');
  assert.strictEqual(doc.querySelectorAll('#panel-ai form').length, 0);
  assert.deepStrictEqual([...doc.querySelectorAll('#panel-ai input[type="password"]')].map((i) => i.className), [...doc.querySelectorAll('#ai .aic-key-in')].map(() => 'aic-key-in'), 'only the connection key fields are password fields');
  assert.deepStrictEqual(errors.concat(bare.errors), []);
  dom.window.close();
  bare.dom.window.close();
  return 'each compatible connection has a card with its name, base URL, company, key status, models with numbered prices, tools and images, last test (not fit without a tool call) and actions, after an intro and the Private AI addresses switch';
}

async function addingAConnectionSendsOnlyTypedValues() {
  //given
  const {dom, w, doc, errors, calls} = await boot(TEMPLATE, {connections: two().connections});

  //when
  doc.querySelector('#ai .aic-add summary').click();
  typeIn(w, addForm(doc).querySelector('.aic-n'), 'DeepSeek');
  addForm(doc).querySelector('.aic-save').click();
  await wait(60);
  const taken = addForm(doc).querySelector('.aic-msg').textContent;
  typeIn(w, addForm(doc).querySelector('.aic-n'), 'groq');
  typeIn(w, addForm(doc).querySelector('.aic-url'), ' https://api.groq.example.com/v1 ');
  typeIn(w, addForm(doc).querySelector('.aic-m-id'), 'llama-9');
  typeIn(w, addForm(doc).querySelector('.aic-m-in'), '0.05');
  addForm(doc).querySelector('.aic-save').click();
  await wait(150);
  const added = card(doc, 'groq');
  const said = [added && added.querySelector('.aic-msg').textContent, doc.activeElement === (added && added.querySelector('.aic-save')), addForm(doc).querySelector('.aic-n').value];
  typeIn(w, card(doc, 'local').querySelector('.aic-url'), 'http://10.0.0.6:11434/v1');
  typeIn(w, addForm(doc).querySelector('.aic-n'), 'mistral');
  const deep = card(doc, 'deepseek');
  deep.querySelector('.aic-images').click();
  deep.querySelector('.aic-save').click();
  await wait(150);
  const kept = [card(doc, 'local').querySelector('.aic-url').value, addForm(doc).querySelector('.aic-n').value, card(doc, 'deepseek') !== deep, card(doc, 'deepseek').querySelector('.aic-msg').textContent];
  const keyed = card(doc, 'local');
  typeIn(w, keyed.querySelector('.aic-key-in'), TYPED);
  keyed.querySelector('.aic-save').click();
  await wait(150);

  //then
  const puts = calls.filter((c) => c[0] === 'PUT').map((c) => [c[1], c[2]]);
  assert.strictEqual(taken, 'That name is already used by another connection: pick another one', 'a name in use is refused before anything is sent');
  assert.deepStrictEqual(puts[0], ['/api/admin/ai-connections/groq', {baseUrl: 'https://api.groq.example.com/v1', models: [{id: 'llama-9', input: 0.05}], tools: true, images: false}], 'a new connection sends what was typed, with no key and no output price when none was typed');
  assert.deepStrictEqual(puts[1], ['/api/admin/ai-connections/deepseek', {baseUrl: 'https://api.deepseek.example.com/v1', models: [{id: 'deepseek-chat', input: 0.27, output: 1.1}, {id: 'deepseek-reasoner'}], tools: true, images: true}], 'saving a card without a typed key keeps the saved one');
  assert.deepStrictEqual(puts[2], ['/api/admin/ai-connections/local', {baseUrl: 'http://10.0.0.6:11434/v1', key: TYPED, models: [{id: 'local-chat', input: 0.27, output: 1.1}], tools: true, images: true}], 'a typed key is sent');
  assert.strictEqual(puts.length, 3);
  assert.deepStrictEqual(said, ['Saved', true, ''], 'the new connection gets its card with Saved and the focus on its Save, and the Add form starts empty again');
  assert.deepStrictEqual(kept, ['http://10.0.0.6:11434/v1', 'mistral', true, 'Saved'], 'saving one card redraws only that card: other cards and the Add form keep what was typed');
  assert.strictEqual(card(doc, 'local').querySelector('.aic-key-in').value, '', 'the key field is empty again');
  assert.ok(doc.body.innerHTML.indexOf(TYPED) < 0, 'and the typed key is kept nowhere');
  assert.ok(calls.filter((c) => c[1] === '/api/admin/ai-uses').length >= 4, 'the uses table is read again after each save');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'Save sends the base URL, the models with only the prices typed, tools and images, and the key only when one is typed; a name in use is refused; only the saved card is redrawn';
}

async function aRefusedSaveShowsTheDetail() {
  //given
  const {dom, w, doc, errors, srv} = await boot(TEMPLATE, {connections: two().connections});
  const deep = card(doc, 'deepseek');
  srv.refuse = 'The host changed: enter the key again';

  //when
  typeIn(w, deep.querySelector('.aic-url'), 'https://other.example.com/v1');
  deep.querySelector('.aic-save').click();
  await wait(120);
  const now = card(doc, 'deepseek');

  //then
  assert.strictEqual(now, deep, 'the card is not drawn again');
  assert.strictEqual(now.querySelector('.aic-msg').textContent, 'The host changed: enter the key again');
  assert.ok(now.querySelector('.aic-msg').classList.contains('bad'));
  assert.strictEqual(now.querySelector('.aic-url').value, 'https://other.example.com/v1', 'the typed value stays for fixing');
  assert.ok(!now.querySelector('.aic-save').disabled, 'Save can be tried again');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a refused save shows the server detail on its card and keeps what was typed';
}

async function testShowsThePlainResult() {
  //given
  const {dom, w, doc, errors, calls, srv} = await boot(TEMPLATE, {connections: two().connections});
  let release;
  srv.hold = new Promise((r) => { release = r; });
  const deep = card(doc, 'deepseek');
  const msg = () => card(doc, 'deepseek').querySelector('.aic-msg');

  //when
  deep.querySelector('.aic-test').click();
  await wait(10);
  const during = [deep.querySelector('.aic-test').disabled, msg().textContent];
  deep.querySelector('.aic-test').click();
  srv.hold = null;
  release();
  await wait(80);
  const passed = [msg().textContent, msg().classList.contains('bad')];
  srv.test = {ok: false, tools: false, summary: NOT_FIT, ms: 812};
  srv.lastTest = {ok: false, tools: false, summary: NOT_FIT, model: 'deepseek-chat', at: iso(0)};
  card(doc, 'deepseek').querySelector('.aic-test').click();
  await wait(80);
  const failed = [msg().textContent, msg().classList.contains('bad'), card(doc, 'deepseek').querySelector('.aic-last-v').textContent];
  srv.test = {ok: true, tools: false, summary: 'Answers', ms: 200};
  card(doc, 'deepseek').querySelector('.aic-test').click();
  await wait(80);
  const noTool = [msg().textContent, msg().classList.contains('bad')];

  //then
  const tests = calls.filter((c) => c[1] === '/api/admin/ai-connections/deepseek/test');
  assert.deepStrictEqual(during, [true, 'Testing\u2026'], 'the button waits while the test runs');
  assert.strictEqual(tests.length, 3, 'a click while a test runs sends nothing');
  assert.deepStrictEqual([tests[0][0], tests[0][2]], ['POST', {baseUrl: 'https://api.deepseek.example.com/v1', models: [{id: 'deepseek-chat', input: 0.27, output: 1.1}, {id: 'deepseek-reasoner'}], tools: true, images: false}], 'the test sends the values on the card');
  assert.deepStrictEqual(passed, ['\u2713 Answers, uses tools \u00b7 300 ms', false]);
  assert.deepStrictEqual(failed.slice(0, 2), [NOT_FIT + ' \u00b7 812 ms', true], 'a failed tool step reads in plain words, with no check mark');
  assert.strictEqual(failed[2], NOT_FIT + ' \u00b7 deepseek-chat \u00b7 ' + w.dayhm(srv.lastTest.at), 'and the Last test line follows');
  assert.deepStrictEqual(noTool, ['Answers \u00b7 200 ms \u00b7 ' + NOT_FIT_NOTE, true], 'an answer without the tool call is not fit, even when ok');
  assert.ok(calls.filter((c) => c[1] === '/api/admin/ai-uses').length >= 4, 'the uses table is read again, since a failed tool step is a problem there');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'Test connection sends the card values, waits while it runs, shows the plain result with the time (not fit without a tool call) and refreshes the Last test line';
}

async function deleteAsksFirst() {
  //given
  const {dom, w, doc, errors, calls} = await boot(TEMPLATE, {connections: two().connections});
  const wrap = doc.getElementById('cf-wrap');
  const del = () => card(doc, 'deepseek').querySelector('.aic-del');

  //when
  typeIn(w, card(doc, 'local').querySelector('.aic-url'), 'http://10.0.0.7:11434/v1');
  del().click();
  await wait(20);
  const asked = [wrap.hidden, doc.getElementById('cf-title').textContent, doc.getElementById('cf-yes').textContent];
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
  assert.deepStrictEqual(asked, [false, 'Delete this compatible connection?', 'Delete connection'], 'Delete asks in the confirm dialog');
  assert.strictEqual(kept, 0, 'Cancel and Esc send nothing');
  assert.ok(back, 'and the focus goes back to Delete');
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'DELETE').map((c) => c[1]), ['/api/admin/ai-connections/deepseek']);
  assert.ok(!card(doc, 'deepseek') && card(doc, 'local'), 'the card is gone');
  assert.strictEqual(card(doc, 'local').querySelector('.aic-url').value, 'http://10.0.0.7:11434/v1', 'the other cards keep what was typed');
  assert.strictEqual(doc.activeElement, card(doc, 'local').querySelector('.aic-url'), 'the focus moves to the next card');
  assert.strictEqual(doc.querySelector('#ai .aic-said').textContent, 'Deleted');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'Delete asks in the confirm dialog, Cancel or Esc keeps the connection, and confirming sends DELETE and removes only that card';
}

async function anOlderRefreshNeverAppliesLast() {
  //given
  const {dom, w, doc, errors, srv} = await boot(TEMPLATE, {connections: two().connections});
  let release;
  srv.items = srv.items.map((i) => i.key === 'self-review.hour' ? Object.assign({}, i, {value: '9'}) : i);

  //when
  srv.connHold = new Promise((r) => { release = r; });
  card(doc, 'deepseek').querySelector('.aic-save').click();
  await wait(40);
  const local = card(doc, 'local');
  typeIn(w, local.querySelector('.aic-key-in'), 'sk-test-' + 'd'.repeat(32));
  local.querySelector('.aic-save').click();
  await wait(100);
  release();
  await wait(150);

  //then
  assert.strictEqual(w.AICONN.connections.filter((c) => c.name === 'local')[0].keySet, true, 'the older read does not overwrite the newer one');
  assert.strictEqual(card(doc, 'local').querySelector('.aic-keytag').textContent, 'key set');
  assert.strictEqual(card(doc, 'deepseek').querySelector('.aic-msg').textContent, 'Saved', 'the older save still ends with Saved');
  assert.strictEqual(doc.querySelector('#admin .adm-row[data-key="self-review.hour"] .adm-in').value, '9', 'the Settings tab is drawn again from the fresh settings');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a refresh that answers late never applies over a newer one, and a refresh redraws the Settings tab too';
}

async function aConnectionSaveKeepsUnsavedSettings() {
  //given
  const {dom, w, doc, errors} = await boot(TEMPLATE, {hash: '#t=settings', connections: two().connections});
  const hour = () => doc.querySelector('#admin .adm-row[data-key="self-review.hour"] .adm-in');
  typeIn(w, hour(), '11');
  w.pickTab('ai');
  await wait(200);

  //when
  card(doc, 'deepseek').querySelector('.aic-save').click();
  await wait(150);

  //then
  assert.strictEqual(card(doc, 'deepseek').querySelector('.aic-msg').textContent, 'Saved');
  assert.strictEqual(hour().value, '11', 'a value typed on the Settings tab and not saved survives the redraw');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'saving a connection keeps what was typed and not saved on the Settings tab';
}

async function aSavedKeyIsNeverShownInTheInput() {
  //given
  const conns = [connection('deepseek', {key: {set: true, value: MARKER, key: MARKER, hint: MARKER}, secret: MARKER})];
  const {dom, w, doc, errors} = await boot(TEMPLATE, {connections: conns});
  const deep = card(doc, 'deepseek');
  const input = deep.querySelector('.aic-key-in');
  const add = addForm(doc).querySelector('.aic-key-in');
  const leaked = () => [...doc.querySelectorAll('input, textarea')].some((i) => i.value.indexOf(MARKER) >= 0) || doc.body.innerHTML.indexOf(MARKER) >= 0;

  //then
  assert.strictEqual(input.value, '', 'the key field is empty');
  assert.strictEqual(input.placeholder, 'Paste a new key');
  assert.strictEqual(doc.querySelector('label[for="' + input.id + '"]').textContent, 'New key');
  assert.ok(!leaked(), 'no key value from a response reaches the page');
  [input, add].forEach((i) => {
    assert.deepStrictEqual([i.type, i.getAttribute('autocomplete'), i.getAttribute('spellcheck'), i.getAttribute('autocapitalize'), i.getAttribute('autocorrect')], ['password', 'new-password', 'false', 'off', 'off']);
    assert.deepStrictEqual(['data-1p-ignore', 'data-lpignore', 'data-bwignore', 'data-form-type'].map((a) => i.getAttribute(a)), ['', 'true', '', 'other'], 'the password-manager opt-outs of the key cards');
    assert.deepStrictEqual(masks(doc, i), [{onScreen: true, value: 'disc'}], 'one page rule masks it');
    assert.strictEqual(inlineMask(i), null);
  });
  assert.deepStrictEqual(masks(doc, doc.getElementById('ai-key-in')), [{onScreen: true, value: 'disc'}], 'the Anthropic key field keeps exactly its one mask rule');

  //when
  typeIn(w, input, TYPED);
  deep.querySelector('.aic-save').click();
  await wait(150);

  //then
  assert.strictEqual(card(doc, 'deepseek').querySelector('.aic-key-in').value, '');
  assert.ok(!leaked() && doc.body.innerHTML.indexOf(TYPED) < 0, 'after Save neither the saved nor the typed key is in the page');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a saved key is never written into the page; the key fields are empty masked text fields with the password-manager opt-outs';
}

async function turningPrivateAddressesOnSavesItAndOpensTheAddForm() {
  //given
  const {dom, w, doc, errors, calls} = await boot(TEMPLATE, {connections: two().connections});
  const flip = (on) => {
    const sw = privateRow(doc).querySelector('input.adm-sw');
    sw.checked = on;
    sw.dispatchEvent(new w.Event('change', {bubbles: true}));
    privateRow(doc).querySelector('.adm-save').click();
  };

  //when
  flip(true);
  await wait(250);
  const on = {fold: doc.querySelector('#ai .aic-add').open, focus: doc.activeElement === addForm(doc).querySelector('.aic-n'), said: doc.querySelector('#ai .aic-said').textContent,
    state: doc.querySelector('#ai .aic-private-t').textContent, checked: privateRow(doc).querySelector('input.adm-sw').checked, msg: privateRow(doc).querySelector('.adm-msg').textContent};
  flip(false);
  await wait(250);
  const off = {said: doc.querySelector('#ai .aic-said').textContent, state: doc.querySelector('#ai .aic-private-t').textContent, checked: privateRow(doc).querySelector('input.adm-sw').checked};

  //then
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'PUT' && c[1] === '/api/admin/settings').map((c) => c[2]), [{key: PRIVATE, value: 'true'}, {key: PRIVATE, value: 'false'}], 'each flip is a setting write');
  assert.deepStrictEqual(on, {fold: true, focus: true, said: PRIVATE_NEXT, state: PRIVATE_ON, checked: true, msg: 'Saved, live within 15 s on every pod'}, 'turning it on opens the add form, focuses the name and says what to add');
  assert.deepStrictEqual(off, {said: '', state: PRIVATE_OFF, checked: false}, 'turning it off only saves and updates the text');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'turning Private AI addresses on saves it, opens the add form with the name focused and an example base URL, and turning it off only updates the text';
}

async function aPrivateAddressSwitchThatCannotBeChangedShowsWhy() {
  //given
  const why = 'Set by SKOPAX_ALLOW_PRIVATE_AI in the environment';
  const items = settings().map((i) => i.key === PRIVATE ? Object.assign({}, i, {value: 'true', off: why}) : i);
  const {dom, doc, errors} = await boot(TEMPLATE, {connections: two().connections, items, allowPrivate: true});

  //when
  const sw = privateRow(doc).querySelector('input.adm-sw');

  //then
  assert.deepStrictEqual([sw.disabled, sw.checked], [true, true], 'the switch shows its value but cannot be changed');
  assert.strictEqual(privateRow(doc).querySelector('.adm-off-why').textContent, why, 'and says why');
  assert.strictEqual(doc.querySelector('#ai .aic-private-t').textContent, PRIVATE_ON);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a Private AI addresses switch that cannot be changed here shows its state and the reason';
}

async function theBaseUrlHasExamples() {
  //given
  const {dom, doc, errors} = await boot(TEMPLATE, {connections: two().connections});

  //when
  const said = [addForm(doc), card(doc, 'deepseek')].map((c) => {
    const url = c.querySelector('.aic-url');
    return doc.getElementById(url.getAttribute('aria-describedby')).textContent;
  });

  //then
  assert.deepStrictEqual(said, [URL_HELP, URL_HELP], 'the add form and each card give base URL examples tied to the field');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the Base URL field of the add form and of each card has examples for a cloud service, Ollama and vLLM';
}

(async () => {
  console.log('PASS ' + await aConnectionCardShowsItsModelsAndLastTest());
  console.log('PASS ' + await addingAConnectionSendsOnlyTypedValues());
  console.log('PASS ' + await aRefusedSaveShowsTheDetail());
  console.log('PASS ' + await testShowsThePlainResult());
  console.log('PASS ' + await deleteAsksFirst());
  console.log('PASS ' + await anOlderRefreshNeverAppliesLast());
  console.log('PASS ' + await aConnectionSaveKeepsUnsavedSettings());
  console.log('PASS ' + await aSavedKeyIsNeverShownInTheInput());
  console.log('PASS ' + await turningPrivateAddressesOnSavesItAndOpensTheAddForm());
  console.log('PASS ' + await aPrivateAddressSwitchThatCannotBeChangedShowsWhy());
  console.log('PASS ' + await theBaseUrlHasExamples());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
