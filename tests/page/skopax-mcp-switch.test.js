const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const TOOLS = [{name: 'list_cards', title: 'List cards', what: 'List Skopax cards.', readOnly: true}, {name: 'take_card', title: 'Take card', what: 'Take a card.', readOnly: false}];

async function boot(info, width) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e).slice(0, 160)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3e6).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {url: 'https://skopax.example.com/#t=mcp', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      if (width) Object.defineProperty(w, 'innerWidth', {value: width, configurable: true});
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        p = String(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', operator: true, admin: true, teams: [], tags: [], chat: true});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/mcp/info')) return ok(Object.assign({url: 'https://skopax.example.com/mcp', clientId: 'skopax-mcp', waitSeconds: 45, diveCap: 6, tools: TOOLS}, info));
        return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
      };
    }});
  await wait(300);
  return {dom, doc: dom.window.document, errors};
}

async function connectMcpShowsTheServerAsOffWhileItsSwitchIsOff() {
  //given
  const shown = {};
  for (const [name, info] of Object.entries({on: {on: true, enabled: true}, off: {on: false, enabled: true}, unset: {on: true, enabled: false}, offUnset: {on: false, enabled: false}})) {
    const {dom, doc, errors} = await boot(info);

    //when
    const state = doc.querySelector('#mcp-info .mcp-state');
    shown[name] = {pill: state.querySelector('.st').textContent, warn: state.querySelector('.st').classList.contains('k-warn'), note: state.querySelector('.muted').textContent};

    //then
    assert.deepStrictEqual(errors, []);
    dom.window.close();
  }

  //then
  assert.deepStrictEqual(shown.on, {pill: 'On', warn: false, note: 'Server address https://skopax.example.com/mcp'}, 'a switched-on, set-up server reads On with its address');
  assert.deepStrictEqual(shown.off, {pill: 'Off', warn: true, note: 'The MCP server is turned off in Admin \u203a Features; it refuses every call until an admin turns it back on.'}, 'a switched-off server reads Off and says where to turn it on');
  assert.deepStrictEqual(shown.offUnset, shown.off, 'the switch is named first when both are missing');
  assert.deepStrictEqual(shown.unset, {pill: 'Not set up yet', warn: true, note: 'MCP answers once single sign-on is set up and on (Admin \u203a Access & sign-in).'}, 'a server without single sign-on says single sign-on is what it waits for');
  console.log('PASS Connect MCP shows the server as Off while its switch is off, and On or Not set up yet otherwise, naming single sign-on as what it waits for');
}

async function connectMcpSaysWhatEachRoleGetsAndMarksTheWriteTools() {
  //given
  const {dom, doc, errors} = await boot({on: true, enabled: true});

  //when
  const lead = doc.querySelector('#mcp-info .mcp-lead').textContent;
  const rows = [...doc.querySelectorAll('#mcp-info .mcp-tools tbody tr')].map((r) => [r.children[0].textContent, r.children[2].textContent, !!r.querySelector('.mcp-op')]);

  //then
  assert.strictEqual(lead, 'Use Skopax from an MCP client such as Claude Code, with your own login. Viewers get the read tools: cards, investigations, deploy reports, asks and notifications. ' +
    'Operators and admins also run investigations, ask the investigator, and take, resolve, mute and assign cards; those tools are marked below.');
  assert.deepStrictEqual(rows, [['list_cards', 'read', false], ['take_card', 'change operators and admins', true]], 'a write tool is marked for operators and admins');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS Connect MCP says viewers get the read tools and operators and admins the rest, and marks the write tools');
}

async function onAPhoneOnlyTheToolsTableScrollsSidewaysInsideItsOwnBox() {
  //given
  const {dom, doc, errors} = await boot({on: true, enabled: true}, 390);
  const w = dom.window;

  //when
  const table = doc.querySelector('#mcp-info .mcp-tools');
  const box = table && table.parentElement;

  //then
  assert.ok(box && box.classList.contains('mcp-tools-box'), 'the tools table sits in its own box');
  assert.strictEqual(box.getAttribute('style'), null, 'its rules live in the page stylesheet, not in an inline style');
  assert.strictEqual(w.getComputedStyle(box).overflowX, 'auto', 'the box, not the page, scrolls sideways when the tool names do not fit');
  assert.strictEqual(w.getComputedStyle(box).maxWidth, '100%', 'and it is never wider than the panel');
  assert.ok(doc.documentElement.scrollWidth <= w.innerWidth, 'the page itself does not scroll sideways at 390 px');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS on a 390 px phone only the Connect MCP tools table scrolls sideways, inside its own box');
}

(async () => {
  await connectMcpShowsTheServerAsOffWhileItsSwitchIsOff();
  await connectMcpSaysWhatEachRoleGetsAndMarksTheWriteTools();
  await onAPhoneOnlyTheToolsTableScrollsSidewaysInsideItsOwnBox();
})().catch((e) => { console.error('FAIL', e); process.exit(1); });
