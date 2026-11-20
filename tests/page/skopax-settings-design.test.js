const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const LONG = 'Posts a card of this severity or higher to the main channel with its numbers when it is pushed; the findings follow as a reply in its thread, and many cards opening together are grouped by the investigator.';
const CSS = () => fs.readFileSync(TEMPLATE, 'utf8').split('<style>')[1].split('</style>')[0];

function items() {
  const row = (key, label, extra) => Object.assign({key, group: 'Investigator', label, help: label + ' help', kind: 'int', live: true, value: '2', base: '2', default: '2', source: 'default', by: null, at: null, low: 0, high: 9}, extra || {});
  return [
    row('investigate.provider', 'AI provider', {kind: 'choice', value: 'anthropic', base: 'anthropic', default: 'anthropic', choices: ['anthropic', 'openai'], help: LONG}),
    row('investigate.model', 'Card model', {kind: 'choice', value: 'claude-opus-5-5', base: 'claude-opus-5-5', default: 'claude-opus-5-5', choices: ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-opus-5', 'claude-opus-4-8', 'claude-sonnet-5']}),
    row('investigate.effort', 'Card effort', {kind: 'choice', value: 'high', base: 'medium', default: 'medium', choices: ['low', 'medium', 'high'], source: 'admin', by: 'dora.lane@example.com', at: iso(3)}),
    row('investigate.gate-effort', 'Topic gate effort', {kind: 'choice', value: 'low', base: 'low', default: 'low', choices: ['low', 'high'], off: 'Not available with this model'}),
    row('investigate.rooms', 'Parallel rooms', {live: false}),
  ];
}

async function boot(opts) {
  opts = opts || {};
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const list = opts.items || items();
  const key = (using) => ({using, admin: using === 'admin', config: false, unreadable: false, updatedBy: using === 'admin' ? 'dora.lane@example.com' : null, updatedAt: using === 'admin' ? iso(30) : null, note: null});
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/#t=' + (opts.tab || 'ai'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p0, o) => {
        const p = String(p0), method = (o && o.method) || 'GET', body = o && o.body !== undefined ? JSON.parse(o.body) : undefined;
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        const bad = (status, detail) => ({ok: false, status, json: async () => ({detail})});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'dora.lane@example.com', userRole: 'admin', operator: true, admin: true, teams: [], tags: []});
        if (p.startsWith('/api/integrations/custom-checks')) return ok({items: [], limits: {}, choices: {}});
        if (p.startsWith('/api/integrations')) return ok(opts.catalog || {edition: 'community', plugins: []});
        if (p.startsWith('/api/admin')) {
          calls.push([method, p, body]);
          if (p === '/api/admin/anthropic-key' && method === 'GET') return ok(key(opts.using === undefined ? 'admin' : opts.using));
          if (p === '/api/admin/openai-key' && method === 'GET') return ok(key(null));
          if (p === '/api/admin/settings' && method === 'PUT') {
            const was = list.filter((i) => i.key === body.key)[0];
            return ok({item: Object.assign({}, was, {value: body.value, source: 'admin', by: 'dora.lane@example.com', at: iso(0)})});
          }
          if (p === '/api/admin/settings') return ok({items: list, rev: 1, refreshSeconds: 15});
          if (p === '/api/admin/plugins') return ok(opts.catalog || {edition: 'community', plugins: []});
          if (p.startsWith('/api/admin/custom-checks')) return ok({items: [], limits: {}, choices: {}});
          if (p.startsWith('/api/admin/history')) return ok([]);
          if (p === '/api/admin/notices') return ok({items: []});
          if (p === '/api/admin/features') return ok({edition: 'community', items: []});
          if (p.startsWith('/api/admin/users') || p.startsWith('/api/admin/tokens')) return ok([]);
          return bad(404, 'none');
        }
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
      };
    }});
  await wait(300);
  return {dom, w: dom.window, doc: dom.window.document, errors, calls};
}

const rowOf = (doc, key) => doc.querySelector('#ai-rows .adm-row[data-key="' + key + '"]');

async function aFormRowWiresItsLabelHelpSourceAndFold() {
  //given
  const t = await boot();
  const provider = rowOf(t.doc, 'investigate.provider'), effort = rowOf(t.doc, 'investigate.effort'), rooms = rowOf(t.doc, 'investigate.rooms'), gate = rowOf(t.doc, 'investigate.gate-effort');
  const help = provider.querySelector('.adm-help'), more = provider.querySelector('.adm-more');
  const folded = [help.classList.contains('adm-fold'), help.classList.contains('open'), more.textContent, more.getAttribute('aria-expanded')];

  //when
  more.focus();
  more.click();
  const opened = [help.classList.contains('open'), more.isConnected, more.textContent, more.getAttribute('aria-expanded'), t.doc.activeElement === more];
  more.click();
  const shut = [help.classList.contains('open'), more.textContent, more.getAttribute('aria-expanded'), t.doc.activeElement === more];

  //then
  const label = provider.querySelector('.adm-n b');
  assert.strictEqual(provider.querySelector('select.adm-in').getAttribute('aria-labelledby'), label.id, 'the control is named by the row label');
  assert.deepStrictEqual(folded, [true, false, 'Show more', 'false'], 'a long help starts folded behind Show more');
  assert.deepStrictEqual(opened, [true, true, 'Show less', 'true', true], 'Show more opens the whole help, turns into Show less and keeps focus');
  assert.deepStrictEqual(shut, [false, 'Show more', 'false', true], 'Show less folds it again and keeps focus');
  assert.ok(help.id && more.getAttribute('aria-controls') === help.id, 'the button names the help it opens');
  assert.strictEqual(help.textContent, LONG, 'the help keeps its full text while folded');
  assert.ok(!rooms.querySelector('.adm-more') && !rooms.querySelector('.adm-help').classList.contains('adm-fold'), 'a short help is never folded');
  assert.ok(provider.querySelector('.adm-m .tag.src-default') && provider.querySelector('.adm-m .tag.src-default').textContent === 'default', 'a default value keeps its source for screen readers, marked quiet');
  assert.ok(!effort.querySelector('.adm-m .src-default') && effort.querySelector('.adm-m .tag.k-act').textContent.indexOf('Set by') === 0, 'a value set here shows its one source tag');
  assert.ok(effort.querySelector('.adm-m .btn.adm-reset'), 'with a quiet Reset beside it');
  assert.ok(rooms.querySelector('.adm-m .tag.k-warn').textContent === 'after restart', 'after restart stays a tag in the same line');
  assert.strictEqual(gate.querySelector('.adm-off-why').id, gate.querySelector('select').getAttribute('aria-describedby'), 'the reason a row is off describes its control');
  assert.deepStrictEqual(t.errors, []);
  t.dom.window.close();
  return 'a form row names its control by its label, folds a long help behind Show more, keeps one source tag with a quiet Reset, and wires the off reason';
}

async function aShortChoiceIsASegmentedControlThatDrivesTheSelect() {
  //given
  const t = await boot();
  const provider = rowOf(t.doc, 'investigate.provider'), model = rowOf(t.doc, 'investigate.model'), gate = rowOf(t.doc, 'investigate.gate-effort');
  const seg = provider.querySelector('.adm-seg'), select = provider.querySelector('select.adm-in');
  const buttons = [...seg.querySelectorAll('.adm-seg-b')];
  const before = [buttons.map((b) => [b.textContent, b.getAttribute('aria-checked'), b.tabIndex]), provider.querySelector('.adm-save').disabled, select.hidden];

  //when
  buttons[1].click();
  const clicked = [select.value, buttons.map((b) => b.getAttribute('aria-checked')), provider.querySelector('.adm-save').disabled];
  buttons[1].focus();
  buttons[1].dispatchEvent(new t.w.KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true, cancelable: true}));
  const keyed = [select.value, t.doc.activeElement === buttons[0]];
  select.value = 'openai';
  select.dispatchEvent(new t.w.Event('input', {bubbles: true}));
  provider.querySelector('.adm-save').click();
  await wait(60);

  //then
  assert.deepStrictEqual([seg.getAttribute('role'), seg.getAttribute('aria-labelledby')], ['radiogroup', provider.querySelector('.adm-n b').id], 'a labelled radio group');
  assert.deepStrictEqual(before, [[['Anthropic', 'true', 0], ['OpenAI', 'false', -1]], true, true], 'proper names, the current value checked, the select kept for the value but hidden');
  assert.deepStrictEqual(clicked, ['openai', ['false', 'true'], false], 'a click sets the select and arms Save');
  assert.deepStrictEqual(keyed, ['anthropic', true], 'arrow keys move and pick');
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'PUT').map((c) => c[2]), [{key: 'investigate.provider', value: 'openai'}], 'Save writes the setting as before');
  assert.ok(!model.querySelector('.adm-seg') && !model.querySelector('select').hidden, 'a long list stays a select');
  assert.ok([...gate.querySelectorAll('.adm-seg-b')].every((b) => b.disabled), 'an unavailable choice disables its segments');
  assert.deepStrictEqual(t.errors, []);
  t.dom.window.close();
  return 'a short choice is a labelled segmented control: proper names, click and arrow keys set the same select, Save writes it, a long list stays a select';
}

async function theKeyFieldIsOneSecretFieldWithItsActions() {
  //given
  const set = await boot();
  const none = await boot({using: null});

  //when
  const card = set.doc.querySelector('#ai .ai-key[data-provider="anthropic"]'), empty = none.doc.querySelector('#ai .ai-key[data-provider="anthropic"]');
  const entry = card.querySelector('.ai-entry'), input = card.querySelector('#ai-key-in');
  const idle = card.querySelector('.ai-save').disabled;
  input.value = 'sk-test-' + 'c'.repeat(32);
  input.dispatchEvent(new set.w.Event('input', {bubbles: true}));

  //then
  assert.deepStrictEqual([...entry.children].map((c) => c.id || c.className), ['ai-key-in', 'btn small ai-save', 'btn sec small ai-test'], 'the input, its Save and Test connection sit on one line');
  assert.strictEqual(input.placeholder, 'Paste a new key');
  assert.deepStrictEqual([idle, card.querySelector('.ai-save').disabled], [true, false], 'Save is off until a key is typed');
  assert.ok(card.querySelector('.ai-line .ai-state') && card.querySelector('.ai-line .ai-by'), 'the state and who set it share one line');
  assert.ok(card.querySelector('.ai-remove') && !empty.querySelector('.ai-remove'), 'Remove shows only for a key set here');
  assert.ok(![...set.doc.querySelectorAll('#ai .ai-key .adm-help')].some((h) => /Test connection tries/.test(h.textContent)), 'the test help is not repeated on every card');
  assert.ok(/Test connection tries the key typed here, or the key in use when the field is empty\./.test(set.doc.querySelector('#panel-ai .panel-note').textContent), 'it is said once in the tab intro');
  assert.deepStrictEqual(set.errors.concat(none.errors), []);
  set.dom.window.close();
  none.dom.window.close();
  return 'an API key card is one secret field: state line, the input with Save and Test connection, Remove only for a key set here, the help said once';
}

async function theSaveButtonOfARowArmsOnlyOnAChange() {
  //given
  const t = await boot();
  const rooms = rowOf(t.doc, 'investigate.rooms'), input = rooms.querySelector('input.adm-in'), save = rooms.querySelector('.adm-save');
  const idle = save.disabled;

  //when
  input.value = '3';
  input.dispatchEvent(new t.w.Event('input', {bubbles: true}));
  const dirty = save.disabled;
  input.value = '2';
  input.dispatchEvent(new t.w.Event('input', {bubbles: true}));

  //then
  assert.deepStrictEqual([idle, dirty, save.disabled], [true, false, true], 'Save arms on a change and disarms when the value is back');
  assert.ok(/\.adm-body \.adm-row:has\(> \.adm-m\) \.adm-acts:not\(:has\(\.adm-save:not\(:disabled\)\)\):has\(> \.adm-msg:empty\) \{ position: absolute; width: 1px; height: 1px;[^}]*clip-path: inset\(50%\);/.test(CSS()), 'an idle actions line takes no room but stays a live region');
  assert.ok(/\.adm-body \.sso-bar\[data-dirty="true"\] \{[^}]*box-shadow/.test(CSS()), 'the sticky save bar lifts when there are unsaved changes');
  assert.deepStrictEqual(t.errors, []);
  t.dom.window.close();
  return 'a row Save arms only on a real change and an idle actions line takes no room; the save bar lifts when dirty';
}

async function theCatalogKeepsBadgesWithTheNameAndTheStatusAtTheEnd() {
  //given
  const plugin = (id, name, extra) => Object.assign({id, name, version: '1.0.0', category: 'tickets', description: name + ' reads tickets, links and releases of one site for the cards and the read tools.', edition: 'community', icon: false, state: 'ok', reason: null, enabled: true,
    fields: [], checks: [], instances: [], health: {state: 'off', text: 'Not set up yet', at: null, instances: []}}, extra || {});
  const t = await boot({tab: 'integrations', catalog: {edition: 'community', plugins: [plugin('jira', 'Jira', {companyKnowledge: 'always', instances: [{name: 'main', primary: true, enabled: true, source: 'admin', problem: null, values: {}, secrets: {}, owned: [], offChecks: []}],
    health: {state: 'ok', text: 'Connected', at: iso(1), instances: [{name: 'main', state: 'ok', text: 'Connected', at: iso(1)}]}}), plugin('acme', 'Acme', {origin: 'package'})]}});
  t.doc.getElementById('tab-integrations').click();
  await wait(150);

  //when
  t.doc.getElementById('int-add-btn').click();
  await wait(30);
  const pop = t.doc.getElementById('int-pop');
  const row = (id) => pop.querySelector('.int-pick[data-id="' + id + '"]');
  const jira = row('jira'), acme = row('acme');
  const shut = pop.querySelector('.int-pop-top .int-pop-x');
  const parts = [[...jira.children].map((c) => c.className.split(' ')[0]), [...jira.querySelector('.int-pick-top').children].map((c) => c.className), jira.querySelector('.int-pick-d').textContent.length > 60];
  shut.click();

  //then
  assert.deepStrictEqual(parts[0], ['int-ico', 'int-pick-t', 'int-has'], 'icon, words, then the status at the end of the row');
  assert.deepStrictEqual(parts[1], ['int-pick-n', 'tag int-know-tag k-info'], 'the Knowledge badge sits on the name line, not in the status column');
  assert.ok(parts[2], 'the whole description is in the row');
  assert.deepStrictEqual([...acme.querySelector('.int-pick-top').children].map((c) => c.textContent), ['Acme', 'Custom'], 'the Custom mark sits on the name line too');
  assert.ok(pop.hidden && t.doc.activeElement === t.doc.getElementById('int-add-btn'), 'the close button closes the catalog and gives focus back');
  const css = CSS();
  assert.ok(/\.int-pop \{[^}]*width: min\(720px, calc\(100vw - 32px\)\);/.test(css), 'the catalog is up to 720 px wide');
  assert.ok(/\.int-pick-d \{[^}]*-webkit-line-clamp: 2;/.test(css), 'a description shows on up to two lines');
  assert.ok(/\.int-pick-top \.tag, \.int-pick-top \.int-pick-c \{[^}]*flex: none;[^}]*white-space: nowrap;/.test(css), 'a badge never shrinks or clips');
  assert.ok(/@media \(max-width: 480px\) \{ \.int-pop \{[^}]*bottom: 0;[^}]*max-height: none;/.test(css), 'a full-screen sheet on a phone');
  assert.deepStrictEqual(t.errors, []);
  t.dom.window.close();
  return 'the add-integration catalog keeps badges on the name line, the status at the row end, two-line descriptions, a close button, 720 px wide and full screen on phones';
}

async function settingsControlsAre44PxOnPhones() {
  //given
  const css = CSS();

  //when
  const phone = /@media \(max-width: 640px\) \{\n  \.adm-body \.adm-c \.adm-in:not\(\.adm-sw\), ([^{]+)\{ min-height: 44px; \}/.exec(css);

  //then
  assert.ok(phone, 'one phone rule raises the settings controls');
  ['.adm-body .btn', '#pqc .btn', '#int-panel .btn', '#int-panel .int-field .int-in', '#panel-prefs .btn', '#panel-prefs .seg button'].forEach((sel) => assert.ok(phone[1].indexOf(sel) >= 0, sel + ' is 44 px tall on a phone'));
  assert.ok(/\.adm-body \.btn, #int-panel \.btn, #pqc \.btn, #panel-prefs \.btn, \.int-top \.btn \{ min-height: 36px;/.test(css), 'one 36 px button height on desktop');
  assert.ok(/@container content \(max-width: 760px\) \{\n  \.adm-body \.adm-row:has\(> \.adm-m\), [^{]+\{ grid-template-columns: auto auto minmax\(0, 1fr\); \}/.test(css), 'a form row is one column when its container is narrow');
  return 'settings controls and buttons are 36 px on desktop and 44 px on phones, and a form row goes to one column in a narrow container';
}

async function theInstanceHeadUsesASwitchAndTheDrawerFooterStaysOneRowOnPhones() {
  //given
  const css = CSS();

  //when
  const phoneFoot = /@media \(max-width: 480px\) \{ \.int-foot-acts \{([^}]*)\} \}/.exec(css);

  //then
  assert.ok(phoneFoot && phoneFoot[1].indexOf('flex: 1 1 100%') < 0 && phoneFoot[1].indexOf('margin-left: 0') < 0, 'Save stays on the Test connection row on a phone when it fits');
  assert.ok(/#int-panel \.int-on \{ appearance: none;[^}]*width: 36px; height: 20px;/.test(css), 'the instance On box is drawn as the same 36 px switch');
  assert.ok(/#int-panel \.int-on:checked \{ background: var\(--good\); \}/.test(css), 'an instance that is on shows a green switch');
  assert.ok(/#int-panel \.int-inst \{[^}]*border-bottom: 1px solid var\(--line\);/.test(css), 'the instance head is set off from the first section');
  assert.ok(/@media \(max-width: 480px\) \{ #pqc \.pqc-head h3 \{ flex: 1 1 100%; \} \}/.test(css), 'on a phone both custom-check buttons sit together under the title');
  return 'the instance head is a switch row set off from the form, the drawer footer stays one row on phones, and the custom-check buttons sit together';
}

async function phoneTargetsAndQuietPartsStayReachable() {
  //given
  const css = CSS();

  //when
  const phone = css.split('@media (max-width: 640px) {\n  .adm-body .adm-c .adm-in:not(.adm-sw)')[1].split('\n}')[0];

  //then
  ['.adm-seg-b', '#int-panel .int-seg-b', '.int-pop-x', '#int-panel .btn.int-close'].forEach((sel) => assert.ok(new RegExp('(^|, |\\n  )' + sel.replace(/[.#]/g, (c) => '\\' + c) + '[,{ ][^}]*\\{ min-height: 44px;').test(phone) || new RegExp(sel.replace(/[.#]/g, (c) => '\\' + c) + '(, [^{]+)?\\{ min-height: 44px;').test(phone), sel + ' is 44 px tall on a phone'));
  const hit = /\n  ([^{\n]+)::before \{ content: ""; position: absolute; inset: -12px -8px; \}/.exec(phone);
  assert.ok(hit, 'one phone rule widens the switch hit areas');
  ['.adm-sw::before', '.feat-sw::before', '.sso-sw::before', '.int-tog::before', '.int-on'].forEach((sel) => assert.ok(hit[0].indexOf(sel) >= 0, sel + ' gets a 44 px hit area'));
  assert.ok(/#int-panel \.int-on-l[^{]*\{ min-height: 44px;/.test(phone), 'the instance On label is 44 px tall');
  assert.ok(/\.adm-key \{ display: block;[^}]*opacity: 1;/.test(phone), 'the setting key shows on a phone, where there is no hover');
  assert.ok(!/\.adm-m(:not\([^{]*\))? \{ visibility: hidden; \}/.test(css), 'the default tag is only visually hidden, so screen readers still hear it');
  assert.ok(!/\.adm-acts[^{]*\{ display: none; \}/.test(css), 'a row status line stays in the page as a live region');
  return 'on phones the segmented choices, catalog and drawer close buttons and switches have 44 px targets, the setting key shows, and the quiet parts stay readable';
}

(async () => {
  console.log('PASS ' + await aFormRowWiresItsLabelHelpSourceAndFold());
  console.log('PASS ' + await aShortChoiceIsASegmentedControlThatDrivesTheSelect());
  console.log('PASS ' + await theKeyFieldIsOneSecretFieldWithItsActions());
  console.log('PASS ' + await theSaveButtonOfARowArmsOnlyOnAChange());
  console.log('PASS ' + await theCatalogKeepsBadgesWithTheNameAndTheStatusAtTheEnd());
  console.log('PASS ' + await settingsControlsAre44PxOnPhones());
  console.log('PASS ' + await theInstanceHeadUsesASwitchAndTheDrawerFooterStaysOneRowOnPhones());
  console.log('PASS ' + await phoneTargetsAndQuietPartsStayReachable());
})().catch((e) => { console.error(e); process.exit(1); });
