const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');
const {masks, inlineMask} = require('./mask');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const EVIL = '<img src=x onerror=alert(1)>"\'';
const FROM_CONFIG = 'The Anthropic API key in use comes from the configuration (ANTHROPIC_API_KEY or a settings source). A key set here takes its place.';
const TYPED = 'sk-test-' + 'c'.repeat(32);
const MARKER = 'sk-leak-' + 'z'.repeat(32);
const BUSY = 'An earlier Anthropic connection test is still running; try again when it ends';
const OPENAI_FROM_CONFIG = 'The OpenAI API key in use comes from the configuration (OPENAI_API_KEY or a settings source). A key set here takes its place.';
const OFF = 'Not available with OpenAI: its web search finds pages but has no separate page read with a limit of its own. Web searches per run still applies';

function status(using, extra) {
  return Object.assign({using, admin: using === 'admin', config: using === 'config', unreadable: false, updatedBy: using === 'admin' ? 'dora.lane@example.com' : null, updatedAt: using === 'admin' ? iso(30) : null,
    note: using === 'config' ? FROM_CONFIG : null, key: MARKER, value: MARKER}, extra || {});
}

function settings() {
  const row = (key, group, label, extra) => Object.assign({key, group, label, help: label + ' help', kind: 'int', live: true, value: '2', base: '2', default: '2', source: 'default', by: null, at: null, low: 1, high: 6}, extra || {});
  return [row('investigate.rooms', 'Investigator', 'Parallel rooms'), row('investigate.mode', 'Investigator', 'Mode', {kind: 'choice', value: 'off', base: 'off', default: 'off', choices: ['off', 'shadow', 'live']}), row('slack.channel', 'Slack', 'Channel', {kind: 'text', value: '', base: '', default: ''})];
}

async function boot(using, opts) {
  opts = opts || {};
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const srv = {key: status(using, opts.extra), okey: Object.assign(status(opts.openai === undefined ? null : opts.openai), opts.openai === 'config' ? {note: OPENAI_FROM_CONFIG} : {}), otest: [], hold: null, test: {ok: true, summary: 'Connected: the key works and claude-opus-5-5 is available to it', ms: 312, key: MARKER}, busy: false};
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/#t=ai', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p0, o) => {
        const p = String(p0), method = (o && o.method) || 'GET', body = o && o.body !== undefined ? JSON.parse(o.body) : undefined;
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        const bad = (status, detail) => ({ok: false, status, json: async () => ({detail})});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'dora.lane@example.com', userRole: 'admin', operator: true, admin: true, teams: [], tags: []});
        if (p.startsWith('/api/admin')) {
          calls.push([method, p, body]);
          if (p === '/api/admin/anthropic-key/test') {
            if (srv.busy) return bad(409, BUSY);
            if (srv.hold) await srv.hold;
            return ok(srv.test);
          }
          if (p === '/api/admin/anthropic-key' && method === 'GET') return ok(srv.key);
          if (p === '/api/admin/openai-key' && method === 'GET') return opts.noOpenai ? bad(404, 'none') : ok(srv.okey);
          if (p === '/api/admin/openai-key/test') { srv.otest.push(body); return ok({ok: true, summary: 'Connected: the key works and gpt-6.2-astra is available to it', ms: 210, key: MARKER}); }
          if (p === '/api/admin/openai-key' && method === 'PUT') { srv.okey = status('admin', {updatedAt: iso(0)}); return ok(srv.okey); }
          if (p === '/api/admin/anthropic-key' && method === 'PUT') {
            if (body.key.length < 20) return bad(422, 'An Anthropic API key is 20-400 characters: letters, digits, dash and underscore.');
            srv.key = status('admin', {updatedAt: iso(0)});
            return ok(srv.key);
          }
          if (p === '/api/admin/anthropic-key' && method === 'DELETE') { srv.key = status(null); return {ok: true, status: 204, json: async () => { throw new Error('no body'); }}; }
          if (p === '/api/admin/settings' && method === 'PUT') {
            const was = (opts.items || settings()).filter((i) => i.key === body.key)[0];
            const item = Object.assign({}, was, {value: body.value, source: 'admin', by: 'dora.lane@example.com', at: iso(0)});
            opts.items = (opts.items || settings()).map((i) => i.key === body.key ? item : i);
            return ok({item});
          }
          if (p === '/api/admin/settings') return ok({items: opts.items || settings(), rev: 1, refreshSeconds: 15});
          if (p.startsWith('/api/admin/history')) return ok([]);
          if (p === '/api/admin/notices') return ok({items: []});
          if (p === '/api/admin/features') return ok({edition: 'community', items: []});
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

const q = (doc, sel) => doc.querySelector('#ai ' + sel);
const typeIn = (w, inp, v) => { inp.value = v; inp.dispatchEvent(new w.Event('input', {bubbles: true})); };
const leaked = (doc) => [...doc.querySelectorAll('input, textarea')].some((i) => i.value.indexOf(MARKER) >= 0) || doc.body.innerHTML.indexOf(MARKER) >= 0;

async function theStateLineSaysWhichKeyIsInUse() {
  //given
  const here = await boot('admin');
  const config = await boot('config');
  const none = await boot(null);

  //then
  assert.strictEqual(q(here.doc, '.ai-state').textContent, 'Using the key set here');
  assert.ok(/^Set by dora\.lane /.test(q(here.doc, '.ai-by').textContent), 'who set it and when: ' + q(here.doc, '.ai-by').textContent);
  assert.ok(q(here.doc, '.ai-remove'), 'a key set here can be removed');
  assert.strictEqual(q(config.doc, '.ai-state').textContent, FROM_CONFIG, 'a key from the configuration says so with the server text');
  assert.ok(!q(config.doc, '.ai-remove') && !q(config.doc, '.ai-by'), 'and has nothing to remove here');
  assert.strictEqual(q(none.doc, '.ai-state').textContent, 'No key');
  assert.ok(!here.doc.getElementById('panel-ai').hidden && here.doc.getElementById('crumb-page').textContent === 'Admin \u203a Investigator');
  assert.deepStrictEqual([...here.doc.querySelectorAll('#ai-rows .adm-row')].map((r) => r.dataset.key), ['investigate.rooms', 'investigate.mode'], 'the Investigator settings follow the key card');
  assert.ok(!here.doc.querySelector('#admin .adm-row[data-key="investigate.rooms"]'));
  assert.deepStrictEqual(here.errors.concat(config.errors, none.errors), []);
  [here, config, none].forEach((t) => t.dom.window.close());
  return 'the key card says Using the key set here (with who and when), the server text for a key from the configuration, or No key, and the Investigator settings follow it';
}

async function theKeyFieldIsAnEmptyMaskedFieldThatNoResponseFills() {
  //given
  const {dom, w, doc, errors, calls} = await boot('admin');
  const input = doc.getElementById('ai-key-in');

  //then
  assert.ok(input.closest('#ai'), 'the field is on the key card');
  assert.deepStrictEqual([input.type, input.getAttribute('autocomplete'), input.getAttribute('spellcheck'), input.getAttribute('autocapitalize'), input.getAttribute('autocorrect'), input.value], ['text', 'off', 'false', 'off', 'off', ''], 'a text field the browser does not fill, check or correct');
  assert.strictEqual(doc.querySelectorAll('#panel-ai input[type="password"]').length, 0, 'no password field on the tab, so a password manager has no login password to offer to save');
  assert.deepStrictEqual(['data-1p-ignore', 'data-lpignore', 'data-bwignore', 'data-form-type'].map((a) => input.getAttribute(a)), ['', 'true', '', 'other'], 'and the opt-outs that 1Password, LastPass, Bitwarden and Dashlane honour');
  assert.deepStrictEqual(masks(doc, input), [{onScreen: true, value: 'disc'}], 'exactly one page rule sets the key field mask, outside any print or conditional block, and it masks');
  assert.strictEqual(inlineMask(input), null, 'and no inline style overrides the mask');
  assert.ok(doc.querySelector('label[for="ai-key-in"]'), 'with a label');
  assert.ok(!input.closest('form') && !doc.querySelector('#ai form, #panel-ai form'), 'and no form around it, so Save is not a form submit a password manager offers to save');

  //when
  typeIn(w, input, TYPED);
  const typedMask = inlineMask(input);
  q(doc, '.ai-save').click();
  await wait(60);
  q(doc, '.ai-test').click();
  await wait(60);

  //then
  assert.strictEqual(typedMask, null, 'typing leaves no inline style on the field either');
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'PUT'), [['PUT', '/api/admin/anthropic-key', {key: TYPED}]], 'Save sends the typed key');
  assert.strictEqual(doc.getElementById('ai-key-in').value, '', 'and empties the field');
  assert.strictEqual(q(doc, '.ai-state').textContent, 'Using the key set here');
  assert.strictEqual(q(doc, '.ai-msg').textContent, '\u2713 Connected: the key works and claude-opus-5-5 is available to it \u00b7 312 ms', 'a passed test reads like the Integrations one: a check mark, the summary and the time');
  assert.ok(!leaked(doc), 'no value from a response is ever written into the page');
  assert.ok(doc.body.innerHTML.indexOf(TYPED) < 0, 'nor is the typed key kept anywhere');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the key field is an empty masked text field, not a password field, with autocomplete, spellcheck and corrections off; Save sends PUT and empties it, and no response value ever reaches the page';
}

async function aRefusedKeyStaysForFixing() {
  //given
  const {dom, w, doc, errors} = await boot(null);

  //when
  typeIn(w, doc.getElementById('ai-key-in'), 'short');
  q(doc, '.ai-save').click();
  await wait(60);

  //then
  assert.strictEqual(q(doc, '.ai-msg').textContent, 'An Anthropic API key is 20-400 characters: letters, digits, dash and underscore.');
  assert.ok(q(doc, '.ai-msg').classList.contains('bad'));
  assert.strictEqual(q(doc, '.ai-state').textContent, 'No key');
  assert.ok(!q(doc, '.ai-save').disabled, 'Save can be tried again');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a refused key shows the server text and Save stays usable';
}

async function testConnectionSendsTheTypedKeyOrNoneAndOneAtATime() {
  //given
  const {dom, w, doc, errors, calls, srv} = await boot('config');
  let release;
  srv.hold = new Promise((r) => { release = r; });

  //when
  q(doc, '.ai-test').click();
  await wait(10);
  const during = [q(doc, '.ai-test').disabled, q(doc, '.ai-msg').textContent];
  q(doc, '.ai-test').click();
  await wait(10);
  srv.hold = null;
  release();
  await wait(40);
  const blank = calls.filter((c) => c[1] === '/api/admin/anthropic-key/test').map((c) => c[2]);
  typeIn(w, doc.getElementById('ai-key-in'), TYPED);
  q(doc, '.ai-test').click();
  await wait(40);
  const typed = calls.filter((c) => c[1] === '/api/admin/anthropic-key/test').map((c) => c[2]).slice(1);
  srv.test = {ok: false, summary: 'Anthropic rejected the key', ms: 370};
  q(doc, '.ai-test').click();
  await wait(40);
  const refused = [q(doc, '.ai-msg').textContent, q(doc, '.ai-msg').classList.contains('bad')];
  srv.busy = true;
  q(doc, '.ai-test').click();
  await wait(40);

  //then
  assert.deepStrictEqual(refused, ['Anthropic rejected the key \u00b7 370 ms', true], 'a failed test has no check mark');
  assert.deepStrictEqual(blank, [{}], 'an empty field tests the key in use with {}, and a click while a test runs sends nothing');
  assert.deepStrictEqual(during, [true, 'Testing\u2026'], 'the button waits while the test runs');
  assert.deepStrictEqual(typed, [{key: TYPED}], 'a typed key is tested as typed');
  assert.strictEqual(doc.getElementById('ai-key-in').value, TYPED, 'testing keeps the typed key for Save');
  assert.strictEqual(q(doc, '.ai-msg').textContent, BUSY, 'a 409 shows the server text');
  assert.ok(q(doc, '.ai-msg').classList.contains('bad') && !q(doc, '.ai-test').disabled);
  assert.ok(!leaked(doc));
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'Test connection sends {} for an empty field and {key} for a typed one, waits while one runs, and shows a 409 text';
}

async function removeAsksFirstAndThenDeletes() {
  //given
  const {dom, w, doc, errors, calls} = await boot('admin');
  const wrap = doc.getElementById('cf-wrap');

  //when
  q(doc, '.ai-remove').click();
  await wait(20);
  const asked = [wrap.hidden, doc.getElementById('cf-title').textContent, doc.getElementById('cf-yes').textContent];
  doc.getElementById('cf-no').click();
  await wait(20);
  const afterCancel = doc.activeElement === q(doc, '.ai-remove');
  q(doc, '.ai-remove').click();
  await wait(20);
  doc.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  await wait(20);
  const afterEsc = [wrap.hidden, doc.activeElement === q(doc, '.ai-remove')];
  const kept = calls.filter((c) => c[0] === 'DELETE').length;
  q(doc, '.ai-remove').click();
  await wait(20);
  doc.getElementById('cf-yes').click();
  await wait(60);

  //then
  assert.deepStrictEqual(asked, [false, 'Remove the Anthropic API key?', 'Remove key'], 'Remove asks in the confirm dialog');
  assert.strictEqual(kept, 0, 'Cancel and Esc send nothing');
  assert.deepStrictEqual([afterCancel, afterEsc], [true, [true, true]], 'after Cancel or Esc the focus is back on Remove');
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'DELETE').map((c) => c[1]), ['/api/admin/anthropic-key'], 'confirming sends DELETE');
  assert.strictEqual(q(doc, '.ai-state').textContent, 'No key');
  assert.ok(!q(doc, '.ai-remove'));
  assert.strictEqual(q(doc, '.ai-msg').textContent, 'Removed');
  assert.ok(wrap.hidden);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'Remove asks in the confirm dialog, Cancel or Esc keeps the key and gives the focus back to Remove, and confirming sends DELETE';
}

async function aHostileUpdatedByIsText() {
  //given
  const {dom, doc, errors} = await boot('admin', {extra: {updatedBy: EVIL}});

  //then
  assert.strictEqual(q(doc, '.ai-by').querySelectorAll('img').length, 0);
  assert.ok(q(doc, '.ai-by').textContent.indexOf('<img') >= 0, 'the name is shown as text');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a hostile updatedBy renders as text';
}

async function everyControlOnTheInvestigatorTabHasAName() {
  //given
  const row = (key, label, kind, extra) => Object.assign({key, group: 'Investigator', label, help: label + ' help', kind, live: true, value: '2', base: '2', default: '2', source: 'default', by: null, at: null, low: 0, high: 9}, extra || {});
  const items = [row('investigate.model', 'Card model', 'choice', {value: 'a', base: 'a', default: 'a', choices: ['a', 'b']}), row('investigate.effort', 'Effort', 'choice', {value: 'high', base: 'high', default: 'high', choices: ['low', 'high']}),
    row('investigate.enabled', 'Card runs', 'bool', {value: 'true', base: 'true', default: 'true'}), row('investigate.rooms', 'Parallel rooms', 'int'), row('investigate.cap', 'Daily limit (USD)', 'float', {value: '20', base: '20', default: '20'}),
    row('investigate.repo', 'Code project', 'text', {value: '', base: '', default: ''}), row('investigate.' + EVIL, EVIL, 'int'), row('slack.channel', 'Channel', 'text', {group: 'Slack', value: '', base: '', default: ''})];
  const {dom, doc, errors} = await boot('admin', {items});
  const textOf = (ids) => ids.split(/\s+/).map((id) => doc.getElementById(id)).filter(Boolean).map((e) => e.textContent.trim()).join(' ').trim();
  const nameOf = (c) => (c.getAttribute('aria-labelledby') && textOf(c.getAttribute('aria-labelledby'))) || (c.getAttribute('aria-label') || '').trim() || (c.id && [...doc.querySelectorAll('label[for="' + c.id + '"]')].map((l) => l.textContent.trim()).join(' ')) || (c.closest('label') ? c.closest('label').textContent.trim() : '') || (c.tagName === 'BUTTON' ? c.textContent.trim() : '');
  const shown = (c) => !c.closest('[hidden]') && c.type !== 'hidden';

  //when
  const controls = [...doc.querySelectorAll('#panel-ai input, #panel-ai select, #panel-ai textarea, #panel-ai button')].filter(shown);
  const unnamed = controls.filter((c) => !nameOf(c)).map((c) => c.outerHTML.slice(0, 80));
  const rows = [...doc.querySelectorAll('#ai-rows .adm-row')].map((r) => [r.dataset.key, nameOf(r.querySelector('.adm-in'))]);

  //then
  assert.ok(controls.length >= 15, 'the tab is drawn: ' + controls.length + ' controls');
  assert.deepStrictEqual(unnamed, [], 'no control on the Investigator tab lacks an accessible name');
  assert.deepStrictEqual(rows, items.filter((i) => i.group === 'Investigator').map((i) => [i.key, i.label]), 'each setting control is named by its row label, as text');
  assert.strictEqual(doc.querySelectorAll('#ai-rows img').length, 0);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'every select, input and button on the Investigator tab has an accessible name; each setting control is named by its row label';
}

async function aiProvidersListAnthropicAndOpenAiKeyCards() {
  //given
  const items = settings().concat([{key: 'investigate.provider', group: 'Investigator', label: 'AI provider', help: 'x', kind: 'choice', live: true, value: 'openai', base: 'anthropic', default: 'anthropic', source: 'admin', by: 'dora.lane@example.com', at: iso(5), choices: ['anthropic', 'openai']}]);
  const {dom, w, doc, errors, calls, srv} = await boot('admin', {openai: 'config', items});

  //when
  const rows = [...doc.querySelectorAll('#ai .ai-providers > li')];
  const anthropic = rows[0], openai = rows[1];
  openai.querySelector('.ai-test').click();
  await wait(40);
  typeIn(w, doc.getElementById('oai-key-in'), TYPED);
  openai.querySelector('.ai-save').click();
  await wait(60);
  const after = doc.querySelector('#ai [data-provider="openai"]');

  //then
  assert.strictEqual(rows.length, 2);
  assert.strictEqual(anthropic.querySelector('#ai-key-h').textContent, 'Anthropic API key');
  assert.strictEqual(openai.querySelector('#oai-key-h').textContent, 'OpenAI API key');
  assert.ok(!anthropic.querySelector('.ai-active') && openai.querySelector('.ai-active').textContent === 'Default provider', 'the card of the provider in use says so');
  assert.strictEqual(openai.querySelector('.ai-state').textContent, OPENAI_FROM_CONFIG);
  assert.deepStrictEqual(masks(doc, doc.getElementById('oai-key-in')), [{onScreen: true, value: 'disc'}], 'the OpenAI key field is masked too');
  assert.ok(doc.querySelector('label[for="oai-key-in"]'));
  assert.deepStrictEqual(srv.otest, [{}], 'Test connection on the OpenAI card tests the OpenAI key');
  assert.strictEqual(calls.filter((c) => c[1] === '/api/admin/anthropic-key/test').length, 0);
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'PUT').map((c) => [c[1], c[2]]), [['/api/admin/openai-key', {key: TYPED}]]);
  assert.strictEqual(after.querySelector('.ai-state').textContent, 'Using the key set here');
  assert.strictEqual(after.querySelector('.ai-msg').textContent, 'Saved', 'the message lands on the card that saved');
  assert.strictEqual(doc.querySelector('#ai [data-provider="anthropic"] .ai-msg').textContent, '');
  assert.strictEqual(doc.getElementById('oai-key-in').value, '');
  assert.ok(!leaked(doc));
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the provider list has an Anthropic and an OpenAI key card, each with its own field, test and save, and the provider in use is marked';
}

async function aSettingNotAvailableWithTheProviderIsDisabledWithItsReason() {
  //given
  const items = settings().concat([{key: 'investigate.web-fetches', group: 'Investigator', label: 'Web pages per run', help: 'x', kind: 'int', live: true, value: '4', base: '4', default: '4', source: 'default', by: null, at: null, low: 0, high: 10, off: OFF}]);
  const {dom, doc, errors} = await boot('admin', {items, noOpenai: true});

  //when
  const row = doc.querySelector('#ai-rows .adm-row[data-key="investigate.web-fetches"]');
  const input = row.querySelector('.adm-in');

  //then
  assert.ok(input.disabled && row.querySelector('.adm-save').disabled, 'the control and Save are disabled');
  assert.strictEqual(row.querySelector('.adm-off-why').textContent, OFF);
  assert.strictEqual(input.getAttribute('aria-describedby'), row.querySelector('.adm-off-why').id, 'the reason is tied to the control');
  assert.ok(!doc.querySelector('#ai-rows .adm-row[data-key="investigate.rooms"] .adm-in').disabled, 'other settings stay usable');
  const cards = [...doc.querySelectorAll('#ai .ai-providers > li')];
  assert.strictEqual(cards.length, 2, 'a failed OpenAI key read still shows its card');
  assert.strictEqual(cards[1].querySelector('.ai-msg').textContent, 'The OpenAI key did not load; open the tab again.');
  assert.ok(!cards[1].querySelector('input') && cards[0].querySelector('#ai-key-in'), 'with no key controls, next to a working Anthropic card');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a setting the AI provider cannot use is disabled with its reason next to it';
}

async function eachModelSitsNextToItsEffortInOneLabelledRow() {
  //given
  const row = (key, label, extra) => Object.assign({key, group: 'Investigator', label, help: label + ' help', kind: 'choice', live: true, value: 'low', base: 'low', default: 'low', source: 'default', by: null, at: null, choices: ['low', 'high']}, extra || {});
  const model = (key, label) => row(key, label, {value: 'gpt-6.2-sol', base: 'gpt-6.2-sol', default: 'gpt-6.2-sol', choices: ['gpt-6.2-astra', 'gpt-6.2-sol']});
  const items = [model('investigate.model', 'Card model'), row('investigate.rooms', 'Parallel rooms', {kind: 'int', value: '2', base: '2', default: '2', low: 1, high: 6}), model('investigate.slack-model', 'Slack model'),
    model('chat.model', 'Chat model'), row('investigate.effort', 'Card effort', {source: 'admin', by: 'dora.lane@example.com', at: iso(3), value: 'high'}), row('investigate.slack-effort', 'Slack effort'),
    model('investigate.gate-model', 'Topic gate model'), row('investigate.gate-effort', 'Topic gate effort', {off: 'Not available with this model'})];
  const {dom, w, doc, errors, calls} = await boot('admin', {items, noOpenai: true});

  //when
  const pairs = [...doc.querySelectorAll('#ai-rows .adm-pair')].map((p) => [doc.getElementById(p.getAttribute('aria-labelledby')).textContent, ...[...p.querySelectorAll('.adm-row')].map((r) => r.dataset.key)]);
  const order = [...doc.querySelectorAll('#ai-rows .adm-row')].map((r) => r.dataset.key);
  const card = doc.querySelector('#ai-rows .adm-row[data-key="investigate.effort"]');
  const gate = doc.querySelector('#ai-rows .adm-row[data-key="investigate.gate-effort"]');
  const select = card.querySelector('select');
  select.value = 'low';
  select.dispatchEvent(new w.Event('input', {bubbles: true}));
  card.querySelector('.adm-save').click();
  await wait(60);

  //then
  assert.deepStrictEqual(pairs, [['Card model and effort', 'investigate.model', 'investigate.effort'], ['Slack model and effort', 'investigate.slack-model', 'investigate.slack-effort'],
    ['Topic gate model and effort', 'investigate.gate-model', 'investigate.gate-effort']], 'each model and its effort share one labelled group');
  assert.deepStrictEqual(order, ['investigate.model', 'investigate.effort', 'investigate.rooms', 'investigate.slack-model', 'investigate.slack-effort', 'chat.model', 'investigate.gate-model', 'investigate.gate-effort'], 'the effort moves next to its model and a model without one stays alone');
  assert.ok(card.querySelector('.adm-reset') && card.querySelector('.adm-help').textContent === 'Card effort help' && card.querySelector('.tag').textContent.indexOf('Set by') === 0, 'the effort keeps its own help, source and reset');
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'PUT').map((c) => c[2]), [{key: 'investigate.effort', value: 'low'}], 'Save still writes the effort setting on its own');
  assert.ok(gate.querySelector('select').disabled && gate.querySelector('.adm-off-why').textContent === 'Not available with this model', 'an effort the model has not shows as not available with the reason');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'each model and its thinking effort sit side by side in one labelled group, each with its own help, source, reset and save';
}

async function switchingTheProviderReloadsTheKeyCardsAndTheNotices() {
  //given
  const items = settings().concat([{key: 'investigate.provider', group: 'Investigator', label: 'AI provider', help: 'x', kind: 'choice', live: true, value: 'anthropic', base: 'anthropic', default: 'anthropic', source: 'default', by: null, at: null, choices: ['anthropic', 'openai']}]);
  const {dom, w, doc, errors, calls} = await boot('admin', {openai: 'config', items});
  const row = doc.querySelector('#ai-rows .adm-row[data-key="investigate.provider"]');
  const before = [calls.filter((c) => c[1] === '/api/admin/notices').length, calls.filter((c) => c[1] === '/api/admin/openai-key').length];
  const active = () => doc.querySelector('#ai .ai-active').closest('[data-provider]').dataset.provider;
  const was = active();

  //when
  const pick = row.querySelector('select');
  pick.value = 'openai';
  pick.dispatchEvent(new w.Event('input', {bubbles: true}));
  row.querySelector('.adm-save').click();
  await wait(120);

  //then
  assert.deepStrictEqual([was, active()], ['anthropic', 'openai'], 'the in-use tag moves to the OpenAI card');
  assert.ok(calls.filter((c) => c[1] === '/api/admin/notices').length > before[0], 'the notices strip is read again');
  assert.ok(calls.filter((c) => c[1] === '/api/admin/openai-key').length > before[1], 'the key cards are read again');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'saving the AI provider reloads the key cards and the notices strip';
}

(async () => {
  console.log('PASS ' + await theStateLineSaysWhichKeyIsInUse());
  console.log('PASS ' + await theKeyFieldIsAnEmptyMaskedFieldThatNoResponseFills());
  console.log('PASS ' + await aRefusedKeyStaysForFixing());
  console.log('PASS ' + await testConnectionSendsTheTypedKeyOrNoneAndOneAtATime());
  console.log('PASS ' + await removeAsksFirstAndThenDeletes());
  console.log('PASS ' + await aHostileUpdatedByIsText());
  console.log('PASS ' + await everyControlOnTheInvestigatorTabHasAName());
  console.log('PASS ' + await aiProvidersListAnthropicAndOpenAiKeyCards());
  console.log('PASS ' + await aSettingNotAvailableWithTheProviderIsDisabledWithItsReason());
  console.log('PASS ' + await eachModelSitsNextToItsEffortInOneLabelledRow());
  console.log('PASS ' + await switchingTheProviderReloadsTheKeyCardsAndTheNotices());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
