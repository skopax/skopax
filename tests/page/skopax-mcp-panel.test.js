const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const clone = (x) => JSON.parse(JSON.stringify(x));
const EVIL = '<img src=x onerror=alert(1)>';
const ALICE = 'alice@example.com';
const BASE = '/api/admin/mcp-tools/instances/';
const PLUGIN = '/api/admin/plugins/mcp-tools';
const GITHUB = 'https://api.githubcopilot.com/mcp/readonly';
const OFF = 'Off \u00b7 set up, paused';

const FIELDS = [
  {key: 'url', label: 'URL', kind: 'url', required: true, default: '', help: 'The streamable HTTP endpoint of the MCP server.', choices: [], low: null, high: null, showIf: null, ui: false},
  {key: 'preset', label: 'Catalog entry', kind: 'choice', required: false, default: '', help: 'Set by the catalog entry you added it from.', choices: ['atlassian', 'notion', 'github', 'google-drive'], low: null, high: null, showIf: null, ui: false,
    choiceLabels: {atlassian: 'Atlassian (Jira and Confluence)', notion: 'Notion', github: 'GitHub', 'google-drive': 'Google Drive'}, group: 'connection'},
  {key: 'auth', label: 'Sign-in', kind: 'choice', required: false, default: 'none', help: 'No sign-in, a header with a token, or OAuth.', choices: ['none', 'header', 'oauth'], low: null, high: null, showIf: null, ui: false},
  {key: 'header-name', label: 'Header name', kind: 'text', required: false, default: 'Authorization', help: 'The header that carries the token.', choices: [], low: null, high: null, showIf: ['auth', 'header'], ui: false},
  {key: 'header-value', label: 'Header value', kind: 'secret', required: true, default: '', help: 'For example Bearer and the token.', choices: [], low: null, high: null, showIf: ['auth', 'header'], ui: false},
  {key: 'oauth-scopes', label: 'OAuth scopes', kind: 'text', required: false, default: '', help: 'Scopes to ask for, separated by spaces.', choices: [], low: null, high: null, showIf: ['auth', 'oauth'], ui: false},
  {key: 'oauth-client-id', label: 'OAuth client id', kind: 'text', required: false, default: '', help: 'Only for servers that register no clients themselves.', choices: [], low: null, high: null, showIf: ['auth', 'oauth'], ui: false},
  {key: 'oauth-client-secret', label: 'OAuth client secret', kind: 'secret', required: false, default: '', help: 'Only for servers that register no clients themselves.', choices: [], low: null, high: null, showIf: ['auth', 'oauth'], ui: false},
  {key: 'chat-role', label: 'Who may use it in chat and asks', kind: 'choice', required: false, default: 'operator', help: 'Card runs always may.', choices: ['viewer', 'operator', 'admin'], low: null, high: null, showIf: null, ui: false},
  {key: 'knowledge', label: 'Knowledge source', kind: 'bool', required: false, default: 'false', help: 'Works only for a catalog entry with a ready adapter.', choices: [], low: null, high: null, showIf: null, ui: false},
  {key: 'ca', label: 'CA certificate', kind: 'pem', required: false, default: '', help: 'PEM certificate to trust instead of the system roots.', choices: [], low: null, high: null, showIf: null, ui: false},
];

const PRESETS = {
  items: [
    {id: 'atlassian', name: 'Atlassian (Jira and Confluence)', url: 'https://mcp.atlassian.com/v2/mcp', auth: 'header', headerName: 'Authorization', headerHint: 'Basic <base64 of email:api_token> or Bearer <service account key>', scopes: '',
      docs: 'https://support.atlassian.com/atlassian-rovo-mcp-server/docs/configuring-authentication-via-api-token/', knowledge: 'soon', adapter: '', linkHosts: [], knowledgeTools: [], clientRequired: false, preview: false, denied: ['executeWrite', 'executeDestructive']},
    {id: 'notion', name: 'Notion', url: 'https://mcp.notion.com/mcp', auth: 'oauth', headerName: '', headerHint: '', scopes: '', docs: 'https://developers.notion.com/guides/mcp/build-mcp-client', knowledge: 'soon', adapter: '', linkHosts: [], knowledgeTools: [],
      clientRequired: false, preview: false, denied: []},
    {id: 'github', name: 'GitHub', url: GITHUB, auth: 'header', headerName: 'Authorization', headerHint: 'Bearer <fine-grained personal access token>', scopes: '', docs: 'https://github.com/github/github-mcp-server/blob/main/docs/remote-server.md',
      knowledge: 'ready', adapter: 'github', linkHosts: ['github.com'], knowledgeTools: ['search_code', 'get_file_contents'], clientRequired: false, preview: false, denied: []},
    {id: 'google-drive', name: 'Google Drive', url: 'https://drivemcp.googleapis.com/mcp/v1', auth: 'oauth', headerName: '', headerHint: '', scopes: 'https://www.googleapis.com/auth/drive.readonly https://www.googleapis.com/auth/drive.file',
      docs: 'https://developers.google.com/workspace/drive/api/guides/configure-mcp-server', knowledge: 'ready', adapter: 'drive', linkHosts: ['docs.google.com', 'drive.google.com'], knowledgeTools: ['search_files', 'get_file_metadata', 'read_file_content'],
      clientRequired: true, preview: true, denied: []},
  ],
  soon: [{id: 'microsoft-365', name: 'Microsoft SharePoint and OneDrive'}],
  texts: {preview: 'Preview: the vendor still calls this server a preview', client: 'This server needs a client id and secret from its own admin console', knowledgeSoon: 'Knowledge source: coming soon', soon: 'Coming soon'},
};

const WARN_ITEMS = [
  ['readers', "Everyone who can use the chat or the investigator can read whatever this connection's account can read."],
  ['account', 'Connect a narrow service account made for Skopax, not a personal or admin login.'],
  ['provider', 'Document text is sent to the AI provider (Anthropic), so it leaves your network.'],
  ['kept', 'Answers can quote documents, and those answers are kept in chat history and card notes.'],
  ['steer', 'A document can contain instructions that try to steer the AI. Skopax treats them as data, but cannot catch every case.'],
  ['personal', 'Personal data or secrets inside documents can appear in answers.'],
  ['cost', 'Each lookup costs tokens and counts toward the spend caps.'],
];
const WARNINGS = {title: 'Before you turn this on', button: 'Confirm', toolText: '{tool} may change data, and Skopax cannot check. Turn it on only if you know it only reads.', version: 1,
  items: WARN_ITEMS.map((x) => ({id: x[0], text: x[1]}))};
const NOTES = {direct: 'Only the files directly in the folder are read, not its sub-folders.', truncated: 'The server offers more tools than are kept; only the first ones are listed.',
  knowledgeSoon: 'Knowledge source: coming soon', empty: 'The server listed no tools, so it offers nothing to the investigator.'};
const IDENTITY = 'This connection acts as one account, the one you connect here. Everyone who can use the chat or the investigator can read what that account can read, so connect a narrow service account.';
const WRITE_LATER = 'write_it can change data; tools that write come in a later version';
const ONLY_ADAPTED = 'A knowledge source offers only the tools that keep to its folder';
const FLAGGED = 'Its description contains text that reads like an instruction to the AI (ignore previous); it starts off';
const LAST = 'The sign-in was cancelled at the server';

function row(name, kind, over) {
  return Object.assign({name, exposed: 'mcp_main_' + name, title: '', description: '', kind, on: false, blocked: null, flagged: null, changed: false, understood: null, params: []}, over || {});
}

function view(name, over) {
  return Object.assign({name, server: {name: 'fake', version: '1.0.0'}, listedAt: iso(5), truncated: false, preset: 'github',
    tools: [row('search', 'read', {on: true, description: 'Finds code.', params: ['q']}), row('maybe', 'unknown', {description: 'Says nothing about writes.'}), row('write_it', 'write', {blocked: WRITE_LATER})],
    auth: {mode: 'header', state: 'none', connectedBy: null, connectedAt: null, expiresAt: null, last: null},
    knowledge: {on: false, state: 'ready', possible: true, noun: 'repository', scope: null},
    confirm: {current: false, by: null, at: null}, refused: null, warnings: clone(WARNINGS), identity: IDENTITY, notes: clone(NOTES)}, over || {});
}

function views() {
  return {
    main: view('main'),
    live: view('live'),
    docs: view('docs', {preset: 'google-drive', tools: [row('search_files', 'read', {on: true})],
      auth: {mode: 'oauth', state: 'reconnect', connectedBy: ALICE, connectedAt: iso(90), expiresAt: null, last: {result: 'denied', text: LAST, at: iso(1)}},
      knowledge: {on: true, state: 'ready', possible: true, noun: 'folder', scope: null}}),
    east: view('east', {preset: 'notion', tools: [], auth: {mode: 'oauth', state: 'connected', connectedBy: ALICE, connectedAt: iso(30), expiresAt: null, last: {result: 'connected', text: 'Connected', at: iso(30)}},
      knowledge: {on: false, state: 'soon', possible: false, noun: null, scope: null}}),
    atl: view('atl', {preset: 'atlassian', knowledge: {on: false, state: 'soon', possible: false, noun: null, scope: null}}),
  };
}

function instance(name, over) {
  return Object.assign({name, primary: false, enabled: false, source: 'admin', problem: null,
    values: {auth: 'header', 'header-name': 'Authorization', 'chat-role': 'operator', knowledge: 'false', url: GITHUB, preset: 'github'},
    secrets: {'header-value': {set: true, from: null}, 'oauth-client-secret': {set: false, from: null}}, owned: [], offChecks: []}, over || {});
}

function catalog(names) {
  const all = {main: instance('main', {primary: true}), live: instance('live', {enabled: true}), docs: instance('docs', {values: {auth: 'oauth', url: 'https://drivemcp.googleapis.com/mcp/v1', preset: 'google-drive', knowledge: 'true'}}),
    east: instance('east', {values: {auth: 'oauth', url: 'https://mcp.notion.com/mcp', preset: 'notion'}}), atl: instance('atl', {values: {auth: 'header', url: 'https://mcp.atlassian.com/v2/mcp', preset: 'atlassian'}}),
    plain: instance('plain', {values: {auth: 'none', url: 'https://mcp.example.com/mcp', knowledge: 'false'}})};
  const list = (names || ['main', 'live', 'docs', 'east', 'atl']).map((n) => all[n]);
  return {edition: 'community', secretsKey: 'generated', plugins: [
    {id: 'acme', name: 'Acme metrics', version: '1.0.0', category: 'metrics', description: 'Reads Acme.', edition: 'community', icon: false, state: 'ok', reason: null, enabled: true, fields: [], checks: [], instances: [],
      health: {state: 'ok', text: 'Healthy', at: iso(2), instances: []}},
    {id: 'mcp-tools', name: 'MCP server', version: '0.1.0', category: 'other', description: 'Adds the tools of an outside MCP server to the investigator and the chat, and can search one folder of company documents', edition: 'community',
      icon: true, state: 'ok', reason: null, enabled: true, fields: FIELDS, checks: [], instances: list,
      health: {state: 'off', text: OFF, at: null, instances: list.map((i) => ({name: i.name, state: i.enabled ? 'pending' : 'off', text: i.enabled ? 'Needs confirmation' : OFF, at: null}))}},
  ]};
}

async function boot(opts) {
  opts = opts || {};
  const errors = [], calls = [], went = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const flags = {hook: null, views: opts.views || views(), catalog: opts.catalog || catalog(), presets: opts.presets || PRESETS,
    folders: {noun: 'folder', items: [{id: 'f1', label: 'Runbooks', url: 'https://drive.google.com/drive/folders/f1'}, {id: 'f2', label: 'Run notes', url: 'https://drive.google.com/drive/folders/f2'}]},
    start: {url: 'https://auth.example.com/authorize?client_id=x&state=y'}};
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/' + (opts.hash || '#t=anoms'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, o) => {
        const p = String(path), method = (o && o.method) || 'GET', body = o && o.body ? JSON.parse(o.body) : null;
        const ok = (b, status) => ({ok: true, status: status || 200, json: async () => clone(b), text: async () => JSON.stringify(b)});
        const bad = (status, detail, field) => ({ok: false, status, json: async () => ({detail, field})});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: ALICE, userRole: 'admin', operator: true, admin: true, teams: [], tags: []});
        if (p.startsWith('/api/admin')) {
          calls.push([method, p, body]);
          const hooked = flags.hook && await flags.hook(method, p, body, {ok, bad});
          if (hooked) return hooked;
          if (p === '/api/admin/plugins' && method === 'GET') return ok(flags.catalog);
          if (p === '/api/admin/notices') return ok({items: []});
          if (p === '/api/admin/mcp-tools/presets') return ok(flags.presets);
          const m = /^\/api\/admin\/mcp-tools\/instances\/([^/?]+)(.*)$/.exec(p);
          if (m) {
            const name = decodeURIComponent(m[1]), rest = m[2], v = flags.views[name];
            if (!v) return bad(404, 'There is no instance ' + name);
            if (rest === '' && method === 'GET') return ok(v);
            if (rest === '/tools' && method === 'PUT') {
              v.tools.forEach((t) => { t.on = body.on.indexOf(t.name) >= 0; });
              return ok(v);
            }
            if (rest === '/tools/refresh' && method === 'POST') { v.listedAt = iso(0); return ok(v); }
            if (rest === '/confirm' && method === 'POST') { v.confirm = {current: true, by: ALICE, at: iso(0)}; return ok(v); }
            if (rest.startsWith('/folders?q=') && method === 'GET') return ok(flags.folders);
            if (rest === '/scope' && method === 'PUT') {
              const it = flags.folders.items.filter((x) => x.id === body.id)[0];
              v.knowledge.scope = Object.assign({}, it, {by: ALICE, at: iso(0)});
              v.confirm = {current: false, by: null, at: null};
              return ok(v);
            }
            if (rest === '/oauth/start' && method === 'POST') return ok(flags.start);
            if (rest === '/oauth' && method === 'DELETE') {
              v.auth = {mode: 'oauth', state: 'none', connectedBy: null, connectedAt: null, expiresAt: null, last: null};
              return {ok: true, status: 204, json: async () => { throw new Error('no body'); }};
            }
          }
          if (p === PLUGIN + '/instances' && method === 'POST') return ok(instance(body.name), 201);
          if (p.startsWith(PLUGIN + '/instances/') && method === 'PATCH') return ok({instance: instance('main')});
          if (p.startsWith('/api/admin/history')) return ok([]);
          if (p.startsWith('/api/admin/small-talk')) return bad(404, 'none');
          if (p.startsWith('/api/admin/settings')) return ok({items: [], rev: 1, refreshSeconds: 15});
          if (p.startsWith('/api/admin/users') || p.startsWith('/api/admin/tokens') || p.startsWith('/api/admin/features')) return ok([]);
        }
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  dom.window.__skopaxAssign = (url) => went.push(url);
  await wait(150);
  return {w: dom.window, doc: dom.window.document, errors, calls, flags, went};
}

async function open(t) {
  t.doc.getElementById('tab-integrations').click();
  await wait(120);
}
const panel = (doc) => doc.getElementById('int-panel');
const wrap = (doc) => doc.getElementById('int-wrap');
const tile = (doc, id) => [...doc.querySelectorAll('#integrations .int-tile')].filter((s) => s.dataset.id === id)[0];
const itab = (doc, name) => [...panel(doc).querySelectorAll('.int-itab')].filter((b) => b.dataset.name === name)[0];
const field = (doc, key) => [...panel(doc).querySelectorAll('.int-form .int-field')].filter((f) => f.dataset.key === key)[0];
const fieldIn = (doc, key) => field(doc, key).querySelector('.int-in');
const section = (doc, name) => panel(doc).querySelector('.int-mcp-' + name);
const toolRow = (doc, name) => [...panel(doc).querySelectorAll('.int-mcp-tool')].filter((r) => r.dataset.tool === name)[0];
const toolSwitch = (doc, name) => toolRow(doc, name).querySelector('.int-mcp-sw');
const group = (doc) => [...doc.querySelectorAll('#int-pop .int-cat-grp')].filter((g) => g.dataset.cat === 'mcp')[0];
const knowGroup = (doc) => [...doc.querySelectorAll('#int-pop .int-cat-grp')].filter((g) => g.dataset.cat === 'knowledge')[0];
const mcpPicks = (doc) => [...group(doc).querySelectorAll('.int-pick')];
const presetPick = (doc, id) => [...mcpPicks(doc), ...knowGroup(doc).querySelectorAll('.int-pick')].filter((b) => b.dataset.preset === id)[0];
const setValue = (w, el, v) => { el.value = v; el.dispatchEvent(new w.Event('input', {bubbles: true})); el.dispatchEvent(new w.Event('change', {bubbles: true})); };
const key = (w, el, k) => el.dispatchEvent(new w.KeyboardEvent('keydown', {key: k, bubbles: true, cancelable: true}));
const sent = (t, from) => t.calls.slice(from).filter((c) => c[0] !== 'GET' || c[1].indexOf('/folders') >= 0);

async function openInstance(t, name) {
  tile(t.doc, 'mcp-tools').querySelector('.int-open').click();
  const b = itab(t.doc, name);
  if (b && b.getAttribute('aria-selected') !== 'true') b.click();
  await wait(80);
}
async function openPopup(t) {
  t.doc.getElementById('int-add-btn').click();
  await wait(80);
}

async function comingSoonEntryShowsAndHasNoAddControl() {
  //given
  const t = await boot();
  const {doc, w} = t;
  await open(t);
  await openPopup(t);
  const soon = presetPick(doc, 'microsoft-365');
  const from = t.calls.length;

  //when
  soon.click();
  key(w, soon, 'Enter');
  key(w, soon, ' ');
  await wait(80);

  //then
  assert.deepStrictEqual([soon.querySelector('.int-pick-n').textContent, soon.querySelector('.int-mcp-tag').textContent], ['Microsoft SharePoint and OneDrive', 'Coming soon']);
  assert.strictEqual(soon.getAttribute('aria-disabled'), 'true');
  assert.deepStrictEqual([panel(doc).querySelectorAll('.int-save, .int-save-on, .int-name-in').length, [...doc.querySelectorAll('[data-preset="microsoft-365"]')].filter((x) => !x.closest('#int-pop')).length], [0, 0], 'nothing on the page adds it: no Save or name box in the side panel, and the entry appears nowhere outside the catalog');
  assert.ok(wrap(doc).hidden && !doc.querySelector('.int-form'), 'no side panel and no form opens');
  assert.deepStrictEqual(t.calls.slice(from), [], 'it never calls a route');
  assert.ok(!doc.getElementById('int-pop').hidden, 'the catalog stays open');
  assert.deepStrictEqual([...soon.querySelectorAll('button, a, input, select')].length, 0, 'it holds no Add control of its own');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: the Coming soon entry reads Coming soon, is aria-disabled, opens no form and calls no route');
}

async function theGroupListsTheBlankServerThePresetsAndTheSoonItemsAndTheGridLeavesMcpOut() {
  //given
  const t = await boot();
  const {doc, w} = t;
  await open(t);

  //when
  await openPopup(t);
  const grids = [...doc.querySelectorAll('#int-pop .int-cat-grp')].filter((g) => !['mcp', 'knowledge'].includes(g.dataset.cat));
  const heading = group(doc).querySelector('.int-cat-h').textContent;
  const knowHeading = knowGroup(doc).querySelector('.int-cat-h').textContent;
  const knowNames = [...knowGroup(doc).querySelectorAll('.int-pick')].map((b) => b.querySelector('.int-pick-n').textContent);
  const order = [...doc.querySelectorAll('#int-pop .int-cat-grp')].map((g) => g.dataset.cat);
  const everyPick = [...doc.querySelectorAll('#int-pop .int-pick')].map((b) => b.querySelector('.int-pick-n').textContent);
  const names = mcpPicks(doc).map((b) => b.querySelector('.int-pick-n').textContent);
  const drive = presetPick(doc, 'google-drive');
  setValue(w, doc.querySelector('#int-pop .int-search'), 'sharepoint');
  const found = [...doc.querySelectorAll('#int-pop .int-pick')].filter((b) => !b.hidden && !b.closest('[hidden]')).map((b) => b.querySelector('.int-pick-n').textContent);
  setValue(w, doc.querySelector('#int-pop .int-search'), '');
  mcpPicks(doc)[0].click();
  await wait(80);

  //then
  assert.strictEqual(heading, 'MCP servers');
  assert.deepStrictEqual([knowHeading, order[0]], ['Company knowledge', 'knowledge'], 'the Company knowledge group comes first');
  assert.deepStrictEqual(knowNames, ['GitHub', 'Google Drive'], 'the presets that are ready knowledge sources sit in the Company knowledge group');
  assert.deepStrictEqual(names, ['MCP server', 'Atlassian (Jira and Confluence)', 'Notion', 'Microsoft SharePoint and OneDrive'], 'the blank server, then the other presets, then the soon items');
  assert.strictEqual(new Set(everyPick).size, everyPick.length, 'no entry appears twice');
  assert.deepStrictEqual(grids.map((g) => [...g.querySelectorAll('.int-pick')].map((b) => b.dataset.id)), [['acme']], 'mcp-tools is left out of the category grid');
  assert.deepStrictEqual([drive.querySelector('.int-mcp-tag').textContent, drive.title], ['Preview', 'Google Drive \u00b7 ' + PRESETS.texts.preview], 'a preview preset carries its tag, and its full name and the preview text as its title');
  assert.deepStrictEqual(found, ['Microsoft SharePoint and OneDrive'], 'the catalog search finds the MCP entries too');
  const form = panel(doc).querySelector('.int-form');
  assert.ok(!wrap(doc).hidden && form && !form.dataset.name, 'the blank item opens the add form of mcp-tools');
  assert.deepStrictEqual(['url', 'auth', 'header-name', 'preset'].map((k) => fieldIn(doc, k).value), ['', 'none', 'Authorization', ''], 'with nothing filled');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();

  const none = catalog();
  none.plugins = [none.plugins[0]];
  const u = await boot({catalog: none});
  await open(u);
  await openPopup(u);
  assert.ok(!group(u.doc), 'without the mcp-tools plug-in there is no MCP group');
  assert.ok(!u.calls.some((c) => c[1].indexOf('mcp-tools') >= 0), 'and the presets are never asked for');
  u.w.close();
  console.log('PASS MCP panel: the popup group lists MCP server, the presets and the soon items; the category grid leaves mcp-tools out');
}

async function theCatalogEntryIsAChipThatCanBeChangedOrCleared() {
  //given
  const t = await boot();
  const {doc, w} = t;
  await open(t);
  await openPopup(t);

  //when
  presetPick(doc, 'github').click();
  await wait(80);
  const chip = field(doc, 'preset').querySelector('.int-entry-chip');
  const picked = [chip.hidden, chip.querySelector('.int-entry-n').textContent, fieldIn(doc, 'preset').hidden, fieldIn(doc, 'preset').value];
  chip.querySelector('.int-entry-change').click();
  const changing = [chip.hidden, fieldIn(doc, 'preset').hidden, doc.activeElement === fieldIn(doc, 'preset'), [...fieldIn(doc, 'preset').options].map((o) => o.textContent)];
  setValue(w, fieldIn(doc, 'preset'), 'notion');
  const changed = [chip.hidden, chip.querySelector('.int-entry-n').textContent, fieldIn(doc, 'preset').hidden, doc.activeElement === chip.querySelector('.int-entry-change')];
  chip.querySelector('.int-entry-clear').click();
  const cleared = [chip.querySelector('.int-entry-n').textContent, fieldIn(doc, 'preset').value, chip.querySelector('.int-entry-clear').hidden, doc.activeElement === chip.querySelector('.int-entry-change')];
  setValue(w, panel(doc).querySelector('.int-name-in'), 'main');
  const from = t.calls.length;
  panel(doc).querySelector('.int-save').click();
  await wait(120);
  const post = t.calls.slice(from).filter((c) => c[0] === 'POST')[0];
  await openInstance(t, 'east');
  const edit = field(doc, 'preset').querySelector('.int-entry-n').textContent;

  //then
  assert.deepStrictEqual(picked, [false, 'GitHub', true, 'github'], 'the entry shows as a chip with its name and the select stays out of the way');
  assert.deepStrictEqual(changing, [true, false, true, ['No catalog entry', 'Atlassian (Jira and Confluence)', 'Notion', 'GitHub', 'Google Drive']], 'Change brings back the select, with readable names');
  assert.deepStrictEqual(changed, [false, 'Notion', true, true]);
  assert.deepStrictEqual(cleared, ['No catalog entry', '', true, true], 'the clear button leaves no catalog entry');
  assert.ok(post && !('preset' in post[2].values), 'a cleared entry is not sent');
  assert.strictEqual(edit, 'Notion');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: the catalog entry is a chip with its name, which Change turns back into a select and the clear button empties');
}

async function presetPrefillsUrlAndAuthAndNeverAValue() {
  //given
  const t = await boot();
  const {doc, w} = t;
  await open(t);
  await openPopup(t);

  //when
  presetPick(doc, 'github').click();
  await wait(80);
  const values = ['url', 'auth', 'header-name', 'preset'].map((k) => fieldIn(doc, k).value);
  const initial = ['url', 'auth', 'header-name', 'preset'].map((k) => field(doc, k).dataset.initial);
  const secret = fieldIn(doc, 'header-value');
  const shown = [field(doc, 'header-name').hidden, field(doc, 'header-value').hidden, field(doc, 'oauth-scopes').hidden];
  const saveOn = panel(doc).querySelector('.int-save-on');
  setValue(w, panel(doc).querySelector('.int-name-in'), 'main');
  const from = t.calls.length;
  panel(doc).querySelector('.int-save').click();
  await wait(120);
  const post = t.calls.slice(from).filter((c) => c[0] === 'POST')[0];

  //then
  assert.deepStrictEqual(values, [GITHUB, 'header', 'Authorization', 'github']);
  assert.deepStrictEqual(initial, ['', 'none', 'Authorization', ''], 'the fields keep their defaults as initial values');
  assert.deepStrictEqual([secret.value, secret.placeholder], ['', 'Bearer <fine-grained personal access token>'], 'the hint is a placeholder, never a value');
  assert.deepStrictEqual(shown, [false, false, true], 'the header fields show, the OAuth ones stay hidden');
  assert.strictEqual(saveOn, null, 'the add form has no Save & turn on');
  assert.deepStrictEqual(post, ['POST', PLUGIN + '/instances', {name: 'main', values: {url: GITHUB, auth: 'header', 'header-name': 'Authorization', preset: 'github'}, enabled: false}], 'the preset values are sent and the form saves off');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: a preset fills url, auth, header name and preset, its hint is only a placeholder, and the add form saves off');
}

async function aPresetThatNeedsAClientSaysSoAboveTheOauthFields() {
  //given
  const t = await boot();
  const {doc, w} = t;
  await open(t);
  await openPopup(t);

  //when
  presetPick(doc, 'google-drive').click();
  await wait(80);
  const note = panel(doc).querySelector('.int-mcp-client');
  const first = [note.textContent, note.hidden, note.nextElementSibling === field(doc, 'oauth-scopes')];
  const scopes = fieldIn(doc, 'oauth-scopes').value;
  setValue(w, fieldIn(doc, 'auth'), 'header');
  const hidden = note.hidden;

  //then
  assert.deepStrictEqual(first, [PRESETS.texts.client, false, true], 'the client text stands right above the OAuth fields');
  assert.strictEqual(scopes, PRESETS.items[3].scopes);
  assert.ok(hidden, 'and goes with them when the sign-in changes');
  assert.ok(!panel(doc).querySelector('.int-save-on'));
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: a preset that needs its own client shows the client text above the OAuth fields');
}

async function warningsShowBeforeConfirmAndTheSwitchStaysOff() {
  //given
  const t = await boot();
  const {doc} = t;
  await open(t);

  //when
  await openInstance(t, 'main');
  const warn = section(doc, 'warn');
  const on = panel(doc).querySelector('.int-inst .int-on');
  const agree = warn.querySelector('.int-mcp-agree');

  //then
  assert.strictEqual(warn.querySelector('.int-mcp-h').textContent, WARNINGS.title);
  assert.deepStrictEqual([...warn.querySelectorAll('.int-mcp-warns li')].map((li) => li.textContent), WARN_ITEMS.map((x) => x[1]), 'the seven texts in order');
  assert.deepStrictEqual([agree.type, agree.checked, agree.closest('label').textContent], ['checkbox', false, 'I understand']);
  assert.strictEqual(warn.querySelector('.int-mcp-confirm').textContent, WARNINGS.button);
  assert.strictEqual(on.disabled, true, 'the instance switch is disabled until the warnings are confirmed');
  assert.strictEqual(doc.getElementById(on.getAttribute('aria-describedby')), warn.querySelector('.int-mcp-h'), 'and says why');
  assert.strictEqual(panel(doc).querySelector('.int-save-on'), null, 'no Save & turn on while unconfirmed');
  assert.deepStrictEqual(t.errors, []);

  const from = t.calls.length;
  itab(doc, 'live').click();
  await wait(80);
  const liveOn = panel(doc).querySelector('.int-inst .int-on');
  const live = [liveOn.disabled, liveOn.checked];
  liveOn.checked = false;
  liveOn.dispatchEvent(new t.w.Event('change', {bubbles: true}));
  await wait(120);
  assert.deepStrictEqual(live, [false, true], 'an enabled connection that is not confirmed can still be switched off');
  assert.deepStrictEqual(t.calls.slice(from).filter((c) => c[0] === 'PATCH'), [['PATCH', PLUGIN + '/instances/live', {enabled: false}]]);
  t.w.close();
  console.log('PASS MCP panel: the warnings show in order before confirmation, the switch and Save & turn on stay off, and turning off is always allowed');
}

async function confirmPostsAndShowsWho() {
  //given
  const t = await boot();
  const {doc} = t;
  await open(t);
  await openInstance(t, 'main');
  const warn = section(doc, 'warn');

  //when
  const from = t.calls.length;
  warn.querySelector('.int-mcp-confirm').click();
  await wait(60);
  const unticked = [sent(t, from), warn.querySelector('.int-mcp-msg').textContent];
  warn.querySelector('.int-mcp-agree').click();
  warn.querySelector('.int-mcp-confirm').click();
  await wait(200);
  const after = section(doc, 'warn');

  //then
  assert.deepStrictEqual(unticked, [[], 'Tick I understand first'], 'without the checkbox nothing is sent');
  assert.deepStrictEqual(t.calls.slice(from).filter((c) => c[0] === 'POST'), [['POST', BASE + 'main/confirm', {understood: true, version: 1}]]);
  assert.ok(after.querySelector('.int-mcp-confirmed').textContent.startsWith('Confirmed by ' + ALICE + ' at '), after.textContent);
  assert.ok(!after.querySelector('.int-mcp-warns') && !after.querySelector('.int-mcp-agree'), 'the warnings and the checkbox give way to who confirmed');
  assert.strictEqual(panel(doc).querySelector('.int-inst .int-on').disabled, false, 'the instance switch can now be turned on');
  assert.ok(panel(doc).querySelector('.int-save-on'), 'and Save & turn on is offered');
  assert.strictEqual(t.calls.slice(from).filter((c) => c[0] === 'GET' && c[1] === BASE + 'main').length, 0, 'the confirm answer is drawn as it came, with no second read');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: confirm POSTs understood and the version, then reads Confirmed by alice@example.com');
}

async function aNoHintToolNeedsUnderstood() {
  //given
  const t = await boot();
  const {doc} = t;
  await open(t);
  await openInstance(t, 'main');
  const from = t.calls.length;

  //when
  toolSwitch(doc, 'maybe').click();
  await wait(40);
  const ask = toolRow(doc, 'maybe').querySelector('.int-mcp-ask');
  const shown = [ask.querySelector('.int-mcp-asktext').textContent, ask.querySelector('.int-mcp-agree').closest('label').textContent, doc.activeElement === ask.querySelector('.int-mcp-agree')];
  ask.querySelector('.int-mcp-turn').click();
  await wait(40);
  const unticked = sent(t, from);
  ask.querySelector('.int-mcp-agree').click();
  ask.querySelector('.int-mcp-turn').click();
  await wait(120);
  const puts = t.calls.slice(from).filter((c) => c[0] === 'PUT');
  const maybeOn = toolSwitch(doc, 'maybe').getAttribute('aria-checked');
  toolSwitch(doc, 'search').click();
  await wait(120);

  //then
  assert.deepStrictEqual(shown, ['maybe may change data, and Skopax cannot check. Turn it on only if you know it only reads.', 'I understand', true], 'the tool warning with its name and an I understand box, which takes the focus');
  assert.deepStrictEqual(unticked, [], 'without the checkbox nothing is sent');
  assert.deepStrictEqual(puts, [['PUT', BASE + 'main/tools', {on: ['search', 'maybe'], understood: true}]], 'the PUT keeps the tools already on and carries understood');
  assert.strictEqual(maybeOn, 'true');
  assert.deepStrictEqual(t.calls.slice(from).filter((c) => c[0] === 'PUT')[1], ['PUT', BASE + 'main/tools', {on: ['maybe'], understood: false}], 'turning a tool off sends the rest of the set');
  assert.deepStrictEqual(['search', 'maybe', 'write_it'].map((n) => toolRow(doc, n).querySelector('.int-mcp-kind').textContent), ['Reads', 'May change data', 'Changes data']);
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: a tool with no hint shows the tool warning and needs I understand before its PUT carries understood');
}

async function aWriteToolSwitchIsLocked() {
  //given
  const v = views();
  v.main.tools.push(row('outside', 'read', {blocked: ONLY_ADAPTED}), row('planted', 'read', {flagged: FLAGGED}), row('moved', 'read', {changed: true}));
  const t = await boot({views: v});
  const {doc} = t;
  await open(t);
  await openInstance(t, 'main');
  const from = t.calls.length;
  const sw = toolSwitch(doc, 'write_it');

  //when
  sw.click();
  key(t.w, sw, 'Enter');
  toolSwitch(doc, 'outside').click();
  await wait(80);
  const why = doc.getElementById(sw.getAttribute('aria-describedby'));

  //then
  assert.deepStrictEqual([sw.getAttribute('role'), sw.getAttribute('aria-checked'), sw.getAttribute('aria-disabled')], ['switch', 'false', 'true'], 'a Changes data switch is locked off');
  assert.strictEqual(why.textContent, WRITE_LATER, 'with its blocked text');
  assert.ok(toolRow(doc, 'write_it').contains(why));
  assert.strictEqual(toolSwitch(doc, 'outside').getAttribute('aria-disabled'), 'true', 'any blocked row is locked, whatever the reason');
  assert.strictEqual(toolRow(doc, 'outside').querySelector('.int-mcp-blocked').textContent, ONLY_ADAPTED);
  assert.deepStrictEqual(sent(t, from), [], 'and nothing is sent');
  assert.strictEqual(toolRow(doc, 'planted').querySelector('.int-mcp-flag').textContent, FLAGGED, 'a flagged row shows the flagged text from the view');
  assert.strictEqual(toolRow(doc, 'moved').querySelector('.int-mcp-changed').textContent, 'Changed on the server');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: a Changes data switch, and any blocked one, is locked with its text; flagged and changed rows say so');
}

async function refreshToolsListsAgainAndShowsARefusal() {
  //given
  const t = await boot();
  const {doc} = t;
  await open(t);
  await openInstance(t, 'main');
  const tools = section(doc, 'tools');
  const before = tools.querySelector('.int-mcp-server').textContent;

  //when
  tools.querySelector('.int-mcp-refresh').click();
  await wait(120);
  const after = section(doc, 'tools').querySelector('.int-mcp-server').textContent;
  t.flags.hook = async (method, p, body, {bad}) => p.endsWith('/tools/refresh') ? bad(409, 'The connection changed while its tools were being listed; list them again') : null;
  section(doc, 'tools').querySelector('.int-mcp-refresh').click();
  await wait(120);

  //then
  assert.deepStrictEqual([before, after], ['fake 1.0.0 \u00b7 3 tools \u00b7 listed 5 min ago', 'fake 1.0.0 \u00b7 3 tools \u00b7 listed 0 min ago']);
  assert.deepStrictEqual(t.calls.filter((c) => c[1] === BASE + 'main/tools/refresh').map((c) => c[0]), ['POST', 'POST']);
  assert.strictEqual(section(doc, 'tools').querySelector('.int-mcp-tmsg').textContent, 'The connection changed while its tools were being listed; list them again', 'a refusal shows its text');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: Refresh tools lists the server again and shows a refusal as given');
}

async function aLateReReadAfterARefusedRefreshNeverPaintsOverANewerLoad() {
  //given
  const t = await boot();
  const {doc, w} = t;
  await open(t);
  await openInstance(t, 'main');
  const shown = section(doc, 'tools').querySelector('.int-mcp-server').textContent;
  t.flags.hook = async (method, p, body, {ok, bad}) => {
    if (method === 'POST' && p === BASE + 'main/tools/refresh') return bad(409, 'The connection changed while its tools were being listed; list them again');
    if (method === 'GET' && p === BASE + 'main') {
      await wait(200);
      return ok(Object.assign(clone(t.flags.views.main), {server: {name: 'stale-main', version: '9.9.9'}}));
    }
    return null;
  };

  //when
  section(doc, 'tools').querySelector('.int-mcp-refresh').click();
  await wait(60);
  w.INT_MCP_SEQ += 1;
  await wait(300);

  //then
  assert.strictEqual(section(doc, 'tools').querySelector('.int-mcp-server').textContent, shown, 'a load started after the re-read owns the box');
  assert.ok(!section(doc, 'tools').querySelector('.int-mcp-refresh').disabled, 'Refresh is usable again');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: a late re-read after a refused Refresh never paints over a newer load');
}

async function theFolderPickerOffersOnlyServerItems() {
  //given
  const t = await boot();
  const {doc, w} = t;
  await open(t);
  await openInstance(t, 'docs');
  const scope = section(doc, 'scope');
  const inputs = [...scope.querySelectorAll('input, textarea, select')].map((i) => i.type);
  const help = scope.querySelector('.int-mcp-direct').textContent;

  //when
  setValue(w, scope.querySelector('.int-mcp-q'), 'run');
  scope.querySelector('.int-mcp-search').click();
  await wait(100);
  const listed = [...section(doc, 'scope').querySelectorAll('.int-mcp-folder')].map((b) => b.textContent);
  const from = t.calls.length;
  section(doc, 'scope').querySelectorAll('.int-mcp-folder')[0].click();
  await wait(200);
  const picked = section(doc, 'scope').querySelector('.int-mcp-picked a');

  //then
  assert.deepStrictEqual(inputs, ['search'], 'one search box and no free-text id input');
  assert.strictEqual(help, 'Only the files directly in the folder are read, not its sub-folders.');
  assert.deepStrictEqual(t.calls.filter((c) => c[1].indexOf('/folders') >= 0), [['GET', BASE + 'docs/folders?q=run', null]]);
  assert.deepStrictEqual(listed, ['Runbooks', 'Run notes']);
  assert.deepStrictEqual(t.calls.slice(from).filter((c) => c[0] === 'PUT'), [['PUT', BASE + 'docs/scope', {id: 'f1'}]], 'a pick sends the listed id');
  assert.deepStrictEqual([picked.textContent, picked.getAttribute('href'), picked.getAttribute('rel')], ['Runbooks', 'https://drive.google.com/drive/folders/f1', 'noopener noreferrer'], 'the picked folder shows as a link');
  assert.strictEqual(panel(doc).querySelector('.int-inst .int-on').disabled, true, 'a new folder needs the warnings again');
  assert.deepStrictEqual(t.errors, []);

  await openInstance(t, 'atl');
  const soon = section(doc, 'scope');
  await wait(40);
  const ksw = soon.querySelector('.int-mcp-ksw');
  assert.strictEqual(soon.querySelector('.int-mcp-ksoon').textContent, PRESETS.texts.knowledgeSoon, 'a soon preset shows the coming soon text');
  assert.deepStrictEqual([ksw.getAttribute('role'), ksw.getAttribute('aria-checked'), ksw.getAttribute('aria-disabled')], ['switch', 'false', 'true'], 'and a disabled switch');
  assert.ok(!soon.querySelector('.int-mcp-q'), 'with no folder picker');
  t.w.close();
  console.log('PASS MCP panel: the folder picker offers only the server items and sends the listed id; a soon preset shows its text and a locked switch');
}

async function theOauthReturnShowsTheServerText() {
  //given
  const t = await boot({hash: '#t=integrations/mcp/docs'});
  const {doc, w} = t;
  await wait(200);

  //when
  const note = panel(doc).querySelector('.int-panel-note');

  //then
  assert.ok(!wrap(doc).hidden, 'the side panel opens');
  assert.strictEqual(itab(doc, 'docs').getAttribute('aria-selected'), 'true', 'on that instance');
  assert.deepStrictEqual([note.textContent, note.classList.contains('bad')], [LAST, true], 'and shows auth.last.text from the view');
  assert.strictEqual(w.location.hash, '#t=integrations', 'the hash loses mcp and the instance');
  const lastLine = section(doc, 'conn').querySelector('.int-mcp-last').textContent;
  assert.ok(lastLine.startsWith(LAST + ' \u00b7 ') && / \u00b7 (\d{1,2} [A-Z][a-z]{2} )?\d\d:\d\d$/.test(lastLine), 'the Connection section shows the last result with its time: ' + lastLine);
  assert.deepStrictEqual(t.errors, []);
  t.w.close();

  const old = views();
  old.docs.auth.last = {result: 'denied', text: LAST, at: iso(60 * 26)};
  const late = await boot({hash: '#t=integrations/mcp/docs', views: old});
  await wait(200);
  const lateLast = section(late.doc, 'conn').querySelector('.int-mcp-last').textContent;
  assert.ok(!wrap(late.doc).hidden && itab(late.doc, 'docs').getAttribute('aria-selected') === 'true', 'an old return link still opens the instance');
  assert.strictEqual(panel(late.doc).querySelector('.int-panel-note').textContent, '', 'but an old result is not shown as news');
  assert.ok(lateLast.startsWith(LAST + ' \u00b7 ') && / \u00b7 \d{1,2} [A-Z][a-z]{2} \d\d:\d\d$/.test(lateLast), 'it stays in the Connection section with its date and time: ' + lateLast);
  assert.strictEqual(late.w.location.hash, '#t=integrations');
  late.w.close();

  const none = views();
  none.docs.auth.last = {result: 'denied', text: LAST};
  const bare = await boot({hash: '#t=integrations/mcp/docs', views: none});
  await wait(200);
  assert.strictEqual(panel(bare.doc).querySelector('.int-panel-note').textContent, '', 'a result with no time is never shown as news');
  assert.strictEqual(section(bare.doc, 'conn').querySelector('.int-mcp-last').textContent, LAST);
  bare.w.close();

  const u = await boot({hash: '#t=integrations/mcp/nope'});
  await wait(200);
  assert.ok(wrap(u.doc).hidden, 'an unknown name opens nothing');
  assert.strictEqual(u.w.location.hash, '#t=integrations');
  u.w.close();
  console.log('PASS MCP panel: the OAuth return opens that instance, shows its last sign-in text from the view and cleans the hash');
}

async function theConnectionSectionConnectsAndDisconnects() {
  //given
  const t = await boot();
  const {doc} = t;
  await open(t);
  await openInstance(t, 'docs');
  const conn = section(doc, 'conn');
  const line = conn.querySelector('.int-mcp-state').textContent;

  //when
  conn.querySelector('.int-mcp-connect').click();
  await wait(80);
  const first = [t.went.slice(), conn.querySelector('.int-mcp-connect').disabled];
  t.flags.start = {url: 'javascript:alert(1)'};
  await openInstance(t, 'docs');
  section(doc, 'conn').querySelector('.int-mcp-connect').click();
  await wait(80);
  const refused = [t.went.length, section(doc, 'conn').querySelector('.int-mcp-cmsg').textContent];
  await openInstance(t, 'east');
  const east = section(doc, 'conn');
  const connected = [east.querySelector('.int-mcp-state').textContent.startsWith('Connected by ' + ALICE + ' at '), east.querySelector('.int-mcp-connect') && east.querySelector('.int-mcp-connect').textContent];
  const from = t.calls.length;
  east.querySelector('.int-mcp-disconnect').click();
  await wait(200);

  //then
  assert.ok(line.startsWith('Needs a new sign-in'), line);
  assert.strictEqual(conn.querySelector('.int-mcp-connect').textContent, 'Reconnect');
  assert.deepStrictEqual(first, [['https://auth.example.com/authorize?client_id=x&state=y'], true], 'Connect POSTs oauth/start and opens the returned URL once');
  assert.ok(t.calls.some((c) => c[0] === 'POST' && c[1] === BASE + 'docs/oauth/start'));
  assert.deepStrictEqual(refused, [1, 'The sign-in did not start, try again'], 'a URL that is not http or https is never opened');
  assert.deepStrictEqual(connected, [true, null], 'a connected server offers Disconnect only');
  assert.deepStrictEqual(t.calls.slice(from).filter((c) => c[0] === 'DELETE'), [['DELETE', BASE + 'east/oauth', null]]);
  assert.strictEqual(section(doc, 'conn').querySelector('.int-mcp-connect').textContent, 'Connect', 'after Disconnect it offers Connect');
  assert.strictEqual(section(doc, 'conn').querySelector('.int-mcp-state').textContent, 'Not connected');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();

  const header = await boot();
  await open(header);
  await openInstance(header, 'main');
  assert.ok(!section(header.doc, 'conn'), 'a header connection has no Connection section');
  header.w.close();
  console.log('PASS MCP panel: Connect opens the sign-in URL from oauth/start, Disconnect DELETEs and the state line follows');
}

async function identitySitsNextToTheChatRoleField() {
  //given
  const t = await boot();
  const {doc} = t;
  await open(t);

  //when
  await openInstance(t, 'main');
  const note = field(doc, 'chat-role').querySelector('.int-mcp-identity');

  //then
  assert.strictEqual(note.textContent, IDENTITY);
  assert.strictEqual(fieldIn(doc, 'chat-role').getAttribute('aria-describedby'), note.id, 'the role field is described by it');
  assert.strictEqual(panel(doc).querySelectorAll('.int-mcp-identity').length, 1);
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: the identity note sits next to the chat-role field');
}

async function aToolSwitchKeepsTheFocusAfterItsRedraw() {
  //given
  const t = await boot();
  const {doc} = t;
  await open(t);
  await openInstance(t, 'main');
  const sw = toolSwitch(doc, 'search');

  //when
  sw.focus();
  sw.click();
  await wait(120);

  //then
  assert.ok(!sw.isConnected, 'the section was drawn again');
  assert.strictEqual(doc.activeElement, toolSwitch(doc, 'search'), 'the focus is on the same tool switch');
  assert.strictEqual(toolSwitch(doc, 'search').getAttribute('aria-checked'), 'false');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: a tool switch keeps the keyboard focus after its redraw');
}

async function oneToolWriteRunsAtATime() {
  //given
  const v = views();
  v.main.tools.push(row('other', 'read', {on: true}));
  const t = await boot({views: v});
  const {doc} = t;
  await open(t);
  await openInstance(t, 'main');
  let release;
  t.flags.hook = async (method, p) => method === 'PUT' && p.endsWith('/tools') && !release ? new Promise((r) => { release = r; }).then(() => null) : null;
  const from = t.calls.length;

  //when
  toolSwitch(doc, 'search').click();
  await wait(30);
  toolSwitch(doc, 'other').click();
  section(doc, 'tools').querySelector('.int-mcp-refresh').click();
  await wait(30);
  const held = [toolRow(doc, 'other').querySelector('.int-mcp-rmsg').textContent, section(doc, 'tools').querySelector('.int-mcp-tmsg').textContent, sent(t, from).length];
  release();
  await wait(120);
  const settled = [toolSwitch(doc, 'search').getAttribute('aria-checked'), toolSwitch(doc, 'other').getAttribute('aria-checked')];
  toolSwitch(doc, 'other').click();
  await wait(120);

  //then
  assert.deepStrictEqual(held, ['Wait for the last change to finish', 'Wait for the last change to finish', 1], 'while one tools write is on its way, another switch or a Refresh sends nothing');
  assert.deepStrictEqual(settled, ['false', 'true'], 'so the second click never undoes the first');
  assert.deepStrictEqual(sent(t, from), [['PUT', BASE + 'main/tools', {on: ['other'], understood: false}], ['PUT', BASE + 'main/tools', {on: [], understood: false}]], 'each PUT is built from the latest answer');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: one tools write or Refresh runs at a time, so a second click never undoes the first');
}

async function theGateFailsClosed() {
  //given
  const t = await boot();
  const {doc} = t;
  t.flags.hook = async (method, p, body, {bad}) => method === 'GET' && p === BASE + 'main' ? bad(500, 'The connection did not load') : null;
  await open(t);

  //when
  await openInstance(t, 'main');

  //then
  assert.strictEqual(panel(doc).querySelector('.int-inst .int-on').disabled, true, 'a view that did not load leaves the switch off');
  assert.strictEqual(panel(doc).querySelector('.int-save-on'), null, 'and offers no Save & turn on');
  assert.strictEqual(panel(doc).querySelector('.int-mcp-fail').textContent, 'The connection did not load');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();

  const v = views();
  v.main.confirm = {current: true, by: ALICE, at: iso(3)};
  const u = await boot({views: v});
  let release;
  u.flags.hook = async (method, p) => method === 'GET' && p === BASE + 'main' && !release ? new Promise((r) => { release = r; }).then(() => null) : null;
  await open(u);
  u.doc.querySelector('#integrations .int-tile[data-id="mcp-tools"] .int-open').click();
  await wait(40);
  itab(u.doc, 'docs').click();
  await wait(80);
  release();
  await wait(80);
  assert.strictEqual(panel(u.doc).querySelector('.int-mcp').dataset.name, 'docs');
  assert.strictEqual(panel(u.doc).querySelector('.int-inst .int-on').disabled, true, 'a late confirmed view of main never unlocks docs');
  assert.strictEqual(panel(u.doc).querySelector('.int-save-on'), null);
  assert.ok(section(u.doc, 'warn').querySelector('.int-mcp-warns'), 'docs still shows its warnings');
  assert.deepStrictEqual(u.errors, []);
  u.w.close();
  console.log('PASS MCP panel: the instance gate fails closed when the view does not load, and a late view of another instance is dropped');
}

async function aConnectionThatCannotBeAKnowledgeSourceShowsOneLockedControl() {
  //given
  const v = views();
  v.plain = view('plain', {preset: '', knowledge: {on: false, state: 'none', possible: false, noun: null, scope: null}});
  const t = await boot({views: v, catalog: catalog(['atl', 'plain'])});
  const {doc} = t;
  await open(t);

  //when
  await openInstance(t, 'atl');
  await wait(40);
  const soonOff = [field(doc, 'knowledge').hidden, section(doc, 'scope').querySelector('.int-mcp-ksw').getAttribute('aria-checked'),
    [...panel(doc).querySelectorAll('select, input, button')].filter((c) => !c.closest('[hidden]') && /Knowledge source/.test((c.labels && c.labels[0] && c.labels[0].textContent) || c.getAttribute('aria-label') || '')).length];
  await openInstance(t, 'plain');
  const generic = [fieldIn(doc, 'knowledge').disabled, fieldIn(doc, 'knowledge').value, field(doc, 'knowledge').hidden, !!section(doc, 'scope')];

  //then
  assert.deepStrictEqual(soonOff, [true, 'false', 1], 'a soon preset shows one locked Knowledge source control, reading off');
  assert.deepStrictEqual(generic, [true, 'false', false, false], 'a generic server keeps the select, locked off, and has no knowledge section');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();

  const on = views();
  on.atl.knowledge.on = true;
  on.plain = view('plain', {preset: '', knowledge: {on: true, state: 'none', possible: false, noun: null, scope: null}});
  const cat = catalog(['atl', 'plain']);
  cat.plugins[1].instances.forEach((i) => { i.values.knowledge = 'true'; });
  const u = await boot({views: on, catalog: cat});
  await open(u);
  await openInstance(u, 'atl');
  await wait(40);
  const soonOn = [section(u.doc, 'scope').querySelector('.int-mcp-ksw').getAttribute('aria-checked'), field(u.doc, 'knowledge').hidden, fieldIn(u.doc, 'knowledge').disabled, fieldIn(u.doc, 'knowledge').value];
  await openInstance(u, 'plain');
  const genericOn = [fieldIn(u.doc, 'knowledge').disabled, fieldIn(u.doc, 'knowledge').value];
  setValue(u.w, fieldIn(u.doc, 'knowledge'), 'false');
  const before = u.calls.length;
  panel(u.doc).querySelector('.int-save').click();
  await wait(80);
  assert.deepStrictEqual(soonOn, ['true', false, false, 'true'], 'a soon preset stored on reads on in both controls, and its select can be turned off');
  assert.deepStrictEqual(genericOn, [false, 'true'], 'a generic server stored on can still be turned off');
  assert.deepStrictEqual(u.calls.slice(before).filter((c) => c[0] === 'PATCH'), [['PATCH', PLUGIN + '/instances/plain', {values: {knowledge: 'false'}}]]);
  assert.deepStrictEqual(u.errors, []);
  u.w.close();
  console.log('PASS MCP panel: a connection that cannot be a knowledge source shows one locked control reading off, and one stored on can be turned off');
}

async function everyPanelWriteWaitsForTheOneInFlight() {
  //given
  const v = views();
  v.east.tools = [row('find', 'read', {on: true})];
  const t = await boot({views: v});
  const {doc} = t;
  const hold = (method, end) => {
    const h = {};
    t.flags.hook = async (m, p) => m === method && p.endsWith(end) && !h.release ? new Promise((r) => { h.release = r; }).then(() => null) : null;
    return h;
  };
  const tap = (el) => { if (el) el.click(); };
  const say = (box, sel) => { const m = box && box.querySelector(sel); return m ? m.textContent : null; };
  const writes = (from) => t.calls.slice(from).filter((c) => c[0] !== 'GET');
  await open(t);
  const seen = [];

  //when
  await openInstance(t, 'main');
  let h = hold('PUT', '/tools');
  let from = t.calls.length;
  tap(toolSwitch(doc, 'search'));
  await wait(30);
  tap(section(doc, 'warn').querySelector('.int-mcp-agree'));
  tap(section(doc, 'warn').querySelector('.int-mcp-confirm'));
  await wait(30);
  seen.push(['confirm during a tools PUT', writes(from).map((c) => c[1]), say(section(doc, 'warn'), '.int-mcp-msg')]);
  if (h.release) h.release();
  await wait(150);

  await openInstance(t, 'main');
  h = hold('POST', '/confirm');
  from = t.calls.length;
  tap(section(doc, 'warn').querySelector('.int-mcp-agree'));
  tap(section(doc, 'warn').querySelector('.int-mcp-confirm'));
  await wait(30);
  tap(toolSwitch(doc, 'search'));
  await wait(30);
  seen.push(['a tools PUT during confirm', writes(from).map((c) => c[1]), say(toolRow(doc, 'search'), '.int-mcp-rmsg')]);
  if (h.release) h.release();
  await wait(200);

  await openInstance(t, 'docs');
  t.flags.hook = null;
  setValue(t.w, section(doc, 'scope').querySelector('.int-mcp-q'), 'run');
  tap(section(doc, 'scope').querySelector('.int-mcp-search'));
  await wait(80);
  h = hold('PUT', '/tools');
  from = t.calls.length;
  tap(toolSwitch(doc, 'search_files'));
  await wait(30);
  tap(section(doc, 'scope').querySelector('.int-mcp-folder'));
  await wait(30);
  seen.push(['a folder pick during a tools PUT', writes(from).map((c) => c[1]), say(section(doc, 'scope'), '.int-mcp-smsg')]);
  if (h.release) h.release();
  await wait(150);
  tap(section(doc, 'scope').querySelector('.int-mcp-search'));
  await wait(80);

  h = hold('PUT', '/scope');
  from = t.calls.length;
  tap(section(doc, 'scope').querySelector('.int-mcp-folder'));
  await wait(30);
  tap(toolSwitch(doc, 'search_files'));
  await wait(30);
  seen.push(['a tools PUT during a folder pick', writes(from).map((c) => c[1]), say(toolRow(doc, 'search_files'), '.int-mcp-rmsg')]);
  if (h.release) h.release();
  await wait(200);

  await openInstance(t, 'east');
  h = hold('PUT', '/tools');
  from = t.calls.length;
  tap(toolSwitch(doc, 'find'));
  await wait(30);
  tap(section(doc, 'conn').querySelector('.int-mcp-disconnect'));
  await wait(30);
  seen.push(['disconnect during a tools PUT', writes(from).map((c) => c[1]), say(section(doc, 'conn'), '.int-mcp-cmsg')]);
  if (h.release) h.release();
  await wait(150);

  t.flags.views.east.auth = {mode: 'oauth', state: 'connected', connectedBy: ALICE, connectedAt: iso(30), expiresAt: null, last: null};
  await openInstance(t, 'east');
  h = hold('DELETE', '/oauth');
  from = t.calls.length;
  tap(section(doc, 'conn').querySelector('.int-mcp-disconnect'));
  await wait(30);
  tap(toolSwitch(doc, 'find'));
  await wait(30);
  seen.push(['a tools PUT during disconnect', writes(from).map((c) => c[1]), say(toolRow(doc, 'find'), '.int-mcp-rmsg')]);
  if (h.release) h.release();
  await wait(200);

  //then
  const WAIT = 'Wait for the last change to finish';
  assert.deepStrictEqual(seen, [
    ['confirm during a tools PUT', [BASE + 'main/tools'], WAIT],
    ['a tools PUT during confirm', [BASE + 'main/confirm'], WAIT],
    ['a folder pick during a tools PUT', [BASE + 'docs/tools'], WAIT],
    ['a tools PUT during a folder pick', [BASE + 'docs/scope'], WAIT],
    ['disconnect during a tools PUT', [BASE + 'east/tools'], WAIT],
    ['a tools PUT during disconnect', [BASE + 'east/oauth'], WAIT],
  ], 'confirm, a folder pick and disconnect share the one-write guard with the tools, both ways');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: confirm, a folder pick and disconnect wait for a tools write in flight, and a tool switch waits for them');
}

async function saveTabsAndTheSwitchWaitForAToolsChangeInFlight() {
  //given
  const t = await boot();
  const {doc, w} = t;
  const say = (el) => el ? el.textContent : null;
  await open(t);
  await openInstance(t, 'live');
  let release;
  t.flags.hook = async (method, p) => method === 'PUT' && p.endsWith('/tools') && !release ? new Promise((r) => { release = r; }).then(() => null) : null;
  const from = t.calls.length;

  //when
  toolSwitch(doc, 'search').click();
  await wait(30);
  setValue(w, fieldIn(doc, 'chat-role'), 'admin');
  panel(doc).querySelector('.int-save')?.click();
  await wait(30);
  const saving = [t.calls.slice(from).filter((c) => c[0] === 'PATCH').length, say(panel(doc).querySelector('.int-form-msg'))];
  const on = panel(doc).querySelector('.int-inst .int-on');
  if (on) {
    on.checked = false;
    on.dispatchEvent(new w.Event('change', {bubbles: true}));
  }
  await wait(30);
  const switching = [t.calls.slice(from).filter((c) => c[0] === 'PATCH').length, on?.checked, say(panel(doc).querySelector('.int-row-msg'))];
  itab(doc, 'main')?.click();
  await wait(30);
  const tabbing = [itab(doc, 'live')?.getAttribute('aria-selected'), panel(doc).querySelector('.int-mcp')?.dataset.name, say(panel(doc).querySelector('.int-panel-note'))];
  release();
  await wait(120);
  let held;
  t.flags.hook = async (method, p) => method === 'PATCH' && !held ? new Promise((r) => { held = r; }).then(() => null) : null;
  const mid = t.calls.length;
  panel(doc).querySelector('.int-save')?.click();
  await wait(30);
  toolRow(doc, 'search')?.querySelector('.int-mcp-sw').click();
  await wait(30);
  const reverse = [t.calls.slice(mid).map((c) => c[0] + ' ' + c[1]), say(toolRow(doc, 'search')?.querySelector('.int-mcp-rmsg'))];
  if (held) held();
  await wait(150);

  //then
  const WAIT = 'Wait for the last change to finish';
  assert.deepStrictEqual(saving, [0, WAIT], 'Save waits for a tools change in flight');
  assert.deepStrictEqual(switching, [0, true, WAIT], 'so does the instance switch, which keeps its state');
  assert.deepStrictEqual(tabbing, ['true', 'live', WAIT], 'and switching to another instance');
  assert.deepStrictEqual(reverse, [['PATCH ' + PLUGIN + '/instances/live'], WAIT], 'a tool switch waits for a Save in flight');
  assert.deepStrictEqual(t.calls.slice(mid).filter((c) => c[0] === 'PATCH').map((c) => c[2]), [{values: {'chat-role': 'admin', 'header-value': null}}], 'and the Save goes out once the tools change is in, keeping the untouched secret null');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: Save, the instance switch and the instance tabs wait for a tools change in flight, and a tool switch waits for a Save');
}

async function theKnowledgeSelectIsLockedUntilTheViewIsIn() {
  //given
  const t = await boot();
  const {doc} = t;
  let release;
  t.flags.hook = async (method, p) => method === 'GET' && p === BASE + 'main' && !release ? new Promise((r) => { release = r; }).then(() => null) : null;
  await open(t);

  //when
  tile(doc, 'mcp-tools').querySelector('.int-open').click();
  await wait(40);
  const before = fieldIn(doc, 'knowledge').disabled;
  release();
  await wait(80);
  const after = fieldIn(doc, 'knowledge').disabled;

  //then
  assert.deepStrictEqual([before, after], [true, false], 'the knowledge select is locked until the view says what it may be');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();

  const u = await boot();
  u.flags.hook = async (method, p, body, {bad}) => method === 'GET' && p === BASE + 'main' ? bad(500, 'The connection did not load') : null;
  await open(u);
  await openInstance(u, 'main');
  assert.strictEqual(fieldIn(u.doc, 'knowledge').disabled, true, 'and stays locked when the view does not load');
  u.w.close();
  console.log('PASS MCP panel: the knowledge select is locked until the view is in, and stays locked when it fails');
}

async function evilServerTextIsText() {
  //given
  const v = views();
  v.docs.server = {name: EVIL, version: EVIL};
  v.docs.tools = [row(EVIL, 'unknown', {title: EVIL, description: EVIL, flagged: EVIL}), row('w', 'write', {blocked: EVIL})];
  v.docs.auth.last = {result: 'failed', text: EVIL, at: iso(1)};
  v.docs.auth.connectedBy = EVIL;
  v.docs.knowledge.scope = {id: 'x', label: EVIL, url: 'javascript:alert(1)', by: ALICE, at: iso(1)};
  v.docs.warnings.items[0].text = EVIL;
  v.docs.identity = EVIL;
  const t = await boot({views: v});
  const {doc, w} = t;
  t.flags.folders = {noun: 'folder', items: [{id: 'e', label: EVIL, url: 'javascript:alert(2)'}, {id: 'h', label: 'Plain', url: 'http://drive.example.com/plain'}]};
  await open(t);
  await openInstance(t, 'docs');

  //when
  setValue(w, section(doc, 'scope').querySelector('.int-mcp-q'), 'x');
  section(doc, 'scope').querySelector('.int-mcp-search').click();
  await wait(100);
  toolSwitch(doc, EVIL).click();
  await wait(40);
  const text = panel(doc).textContent;

  //then
  assert.ok(!panel(doc).querySelector('img:not(.ico)'), 'no element is made from server text');
  assert.ok(text.split(EVIL).length - 1 >= 10, 'every server value shows as text: ' + (text.split(EVIL).length - 1));
  assert.ok(toolRow(doc, EVIL).querySelector('.int-mcp-asktext').textContent.startsWith(EVIL + ' may change data'), 'the tool name fills the tool warning as text');
  assert.ok(![...panel(doc).querySelectorAll('a')].some((a) => !/^https:\/\//.test(a.getAttribute('href'))), 'a javascript: or http: folder URL is never a link');
  assert.strictEqual(section(doc, 'scope').querySelector('.int-mcp-picked').textContent.indexOf(EVIL) >= 0, true);
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: server names, tool names, descriptions, warnings and folder labels render as text, and only https folder URLs are links');
}

async function theNewControlsAreNamedAndTheSectionsLabelled() {
  //given
  const t = await boot();
  const {doc} = t;
  await open(t);
  await openInstance(t, 'docs');

  //when
  const box = panel(doc).querySelector('.int-mcp');
  const sections = [...box.querySelectorAll('section')];
  const named = (el) => (el.getAttribute('aria-label') || '').trim() || (el.labels && el.labels.length && el.labels[0].textContent.trim()) || el.textContent.trim();
  const controls = [...box.querySelectorAll('button, input, a, select')];

  //then
  assert.deepStrictEqual(sections.map((s) => s.querySelector('.int-mcp-h').textContent), ['Connection', 'What it can look up', 'Knowledge source', WARNINGS.title], 'the sections in order');
  assert.ok(sections.every((s) => doc.getElementById(s.getAttribute('aria-labelledby')) === s.querySelector('.int-mcp-h')), 'each section is labelled by its heading');
  assert.deepStrictEqual(controls.filter((c) => !named(c)).map((c) => c.outerHTML), [], 'every control has a name');
  assert.ok([...box.querySelectorAll('.int-mcp-sw')].every((s) => s.getAttribute('role') === 'switch' && /^(true|false)$/.test(s.getAttribute('aria-checked')) && s.getAttribute('aria-label') === 'Switch ' + s.closest('.int-mcp-tool').dataset.tool));
  assert.ok([...box.querySelectorAll('.int-mcp-msg, .int-mcp-tmsg, .int-mcp-cmsg, .int-mcp-smsg')].every((m) => m.getAttribute('role') === 'status'), 'messages are announced');
  assert.ok(box.querySelector('.int-mcp-find').getAttribute('role') === 'search');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: every new control is named, switches carry their state, sections are labelled and messages announced');
}

async function theMcpStylesUseThemeTokensOnly() {
  //given
  const css = fs.readFileSync(TEMPLATE, 'utf8');
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', '{}'));
  const flat = (list) => [...list].flatMap((r) => r.cssRules && !r.selectorText ? flat(r.cssRules) : r.selectorText ? [r] : []);
  const all = flat(dom.window.document.querySelector('style').sheet.cssRules);

  //when
  const mine = all.filter((r) => /\.int-mcp/.test(r.selectorText));
  const block = css.slice(css.indexOf('.int-mcp {'), css.indexOf('@media (max-width: 768px) { .int-panel'));
  const decls = [...block.matchAll(/([a-z-]+): ([^;}]+)/g)].filter((m) => /color|background|border|outline|shadow|fill/.test(m[1]));
  const literal = decls.filter((m) => /#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(|\b(white|black)\b/i.test(m[2])).map((m) => m[0]);
  const tokens = [...new Set(decls.flatMap((m) => (m[2].match(/var\(--[a-z0-9-]+/g) || []).map((x) => x.slice(4))))];
  const scope = (from) => css.slice(css.indexOf(from), css.indexOf('}', css.indexOf(from)));
  const scopes = [scope(':root {'), scope(':root:not([data-theme="light"]) {'), scope(':root[data-theme="dark"] {')];
  const missing = tokens.filter((tk) => !scopes.every((sc) => new RegExp('\\s' + tk + ':').test(sc)));
  const wrapped = ['.int-mcp-tn', '.int-mcp-tdesc', '.int-mcp-picked', '.int-mcp-server', '.int-mcp-last', '.int-mcp-state', '.int-mcp-folder'].filter((s) => !mine.some((r) => r.selectorText.split(',').map((x) => x.trim()).indexOf(s) >= 0 && r.style.getPropertyValue('overflow-wrap') === 'anywhere'));

  //then
  assert.ok(mine.length >= 12 && block.length > 1000, 'the MCP rules are read: ' + mine.length);
  assert.ok(block.split('\n').every((l) => !l || l.startsWith('.int-mcp')), 'the block holds only .int-mcp rules');
  assert.deepStrictEqual(literal, [], 'no colour literal: every colour is a theme token');
  assert.ok(tokens.length >= 5);
  assert.ok(scopes.every((sc) => sc.length > 500), 'the three theme scopes are read');
  assert.deepStrictEqual(missing, [], 'each token is set for light, the dark media query and the dark theme switch');
  assert.deepStrictEqual(wrapped, [], 'server text wraps instead of pushing the panel sideways');
  assert.ok(css.indexOf('.int-mcp') > css.indexOf('.int-acts {') && css.indexOf('.int-mcp') < css.indexOf('@media (max-width: 768px) { .int-panel'), 'the rules sit after .int-acts');
  assert.ok(!/\.mcp-tools[\s{,.:]/.test(css.slice(css.indexOf('.int-acts {'), css.indexOf('@media (max-width: 768px) { .int-panel'))), 'the page classes never reuse .mcp-tools');
  dom.window.close();
  console.log('PASS MCP panel: the new rules use theme tokens set for light and both dark scopes, and server text wraps');
}

async function aWarnHealthShowsAsAWarningAndNeedsAttention() {
  //given
  const cat = catalog();
  const mcp = cat.plugins[1];
  mcp.health = {state: 'warn', text: 'Needs confirmation', at: null, instances: [{name: 'main', state: 'off', text: OFF, at: null}, {name: 'live', state: 'warn', text: 'Needs confirmation', at: null}]};
  const t = await boot({catalog: cat});
  const {doc} = t;

  //when
  await open(t);
  const dot = tile(doc, 'mcp-tools').querySelector('.int-st .int-dot');
  const need = doc.querySelector('#integrations .int-sub').textContent;
  await openInstance(t, 'live');
  const inst = panel(doc).querySelector('.int-ihealth .int-dot');

  //then
  assert.deepStrictEqual([dot.className, inst.className], ['int-dot warn', 'int-dot warn'], 'a warn state keeps its own dot, not the off one');
  assert.ok(need.endsWith('1 needs attention'), need);
  const css = fs.readFileSync(TEMPLATE, 'utf8');
  assert.ok(/\.int-dot\.warn \{ background: var\(--warn\); \}/.test(css), 'the warn dot uses the warn token');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: a warn health state shows a warn dot and counts as needing attention');
}

async function theCatalogEntryCanBeCleared() {
  //given
  const t = await boot();
  const {doc, w} = t;
  await open(t);
  await openInstance(t, 'main');
  const select = fieldIn(doc, 'preset');

  //when
  const options = [...select.options].map((o) => [o.value, o.textContent]);
  setValue(w, select, '');

  //then
  assert.deepStrictEqual(options[0], ['', 'No catalog entry'], 'an empty choice clears the catalog entry');
  assert.deepStrictEqual(options.slice(1).map((o) => o[0]), ['atlassian', 'notion', 'github', 'google-drive']);
  assert.strictEqual(select.value, '');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: the catalog entry select offers No catalog entry');
}

async function aRefusedConnectionShowsTheRefusal() {
  //given
  const views_ = views();
  views_.main.refused = {text: 'The server refused the request: ' + EVIL, at: iso(3)};
  views_.live.refused = {text: 'MCP server live refused mcp_live_search (HTTP 403)', at: iso(2), call: true};
  views_.docs.refused = null;
  const t = await boot({views: views_});
  const {doc} = t;

  //when
  await open(t);
  await openInstance(t, 'main');
  const shown = section(doc, 'tools').querySelector('.int-mcp-refused');
  await openInstance(t, 'live');
  const call = section(doc, 'tools').querySelector('.int-mcp-refused').textContent;
  await openInstance(t, 'docs');
  const quiet = section(doc, 'tools').querySelector('.int-mcp-refused');

  //then
  assert.ok(shown && shown.textContent.startsWith('Left out of runs: The server refused the request: ' + EVIL), shown && shown.textContent);
  assert.ok(!shown.querySelector('img'), 'the server text stays text');
  assert.ok(call.startsWith('A tool call was refused: MCP server live refused mcp_live_search (HTTP 403)'), call);
  assert.strictEqual(quiet, null, 'no refusal, no line');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: a refused connection shows why it is left out of runs');
}

async function theNotesComeFromTheViewEvenWithoutThePresets() {
  //given
  const views_ = views();
  views_.docs.notes = {direct: 'D note', truncated: 'T note', knowledgeSoon: 'K note'};
  views_.docs.truncated = true;
  views_.atl.notes = {direct: 'D note', truncated: 'T note', knowledgeSoon: ''};
  views_.east.notes = {direct: 'D note', truncated: 'T note', knowledgeSoon: 'K note'};
  const t = await boot({views: views_});
  const {doc} = t;
  t.flags.hook = async (method, p, body, {bad}) => p === '/api/admin/mcp-tools/presets' ? bad(500, 'down') : null;

  //when
  await open(t);
  await openInstance(t, 'docs');
  const direct = section(doc, 'scope').querySelector('.int-mcp-direct').textContent;
  const cut = section(doc, 'tools').querySelector('.int-mcp-cut').textContent;
  await openInstance(t, 'east');
  await wait(40);
  const soon = section(doc, 'scope');
  const soonText = soon.querySelector('.int-mcp-ksoon').textContent, described = soon.querySelector('.int-mcp-ksw').getAttribute('aria-describedby');
  await openInstance(t, 'atl');
  await wait(40);
  const empty = section(doc, 'scope').querySelector('.int-mcp-ksw').getAttribute('aria-describedby');

  //then
  assert.deepStrictEqual([direct, cut, soonText, described], ['D note', 'T note', 'K note', 'int-mcp-ksoon'], 'the notes are the server texts, with no second presets read');
  assert.strictEqual(empty, null, 'an empty text is not pointed at');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: the folder, truncated and coming soon notes come from the view');
}

async function aConfirmThatNeedsAFolderPointsToThePicker() {
  //given
  const SCOPE_FIRST = 'Pick the folder first';
  const t = await boot();
  const {doc} = t;
  t.flags.hook = async (method, p, body, {bad}) => p === BASE + 'docs/confirm' ? bad(409, SCOPE_FIRST) : null;
  await open(t);
  await openInstance(t, 'docs');
  const warn = section(doc, 'warn');

  //when
  warn.querySelector('.int-mcp-agree').click();
  warn.querySelector('.int-mcp-confirm').click();
  await wait(150);
  const said = section(doc, 'warn').querySelector('.int-mcp-msg');

  //then
  assert.strictEqual(said.textContent, SCOPE_FIRST);
  assert.strictEqual(doc.activeElement, section(doc, 'scope').querySelector('.int-mcp-q'), 'the focus moves to the folder search');
  assert.strictEqual(section(doc, 'scope').querySelector('.int-mcp-q').getAttribute('aria-describedby'), said.id, 'which names the reason');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: a confirm refused for a missing folder points to the folder picker');
}

async function failedFolderReadsShowTheirText() {
  //given
  const BAD_GATEWAY = 'The server answered this folder read with an error, so nothing was read';
  const PICK = 'Pick a folder from the list the server gave';
  const t = await boot();
  const {doc, w} = t;
  t.flags.hook = async (method, p, body, {bad}) => p.indexOf('/folders?') >= 0 && t.flags.failFolders ? bad(502, BAD_GATEWAY) : p.endsWith('/scope') ? bad(422, PICK, 'id') : null;
  await open(t);
  await openInstance(t, 'docs');

  //when
  t.flags.failFolders = true;
  setValue(w, section(doc, 'scope').querySelector('.int-mcp-q'), 'run');
  section(doc, 'scope').querySelector('.int-mcp-search').click();
  await wait(100);
  const failed = [section(doc, 'scope').querySelector('.int-mcp-smsg').textContent, section(doc, 'scope').querySelectorAll('.int-mcp-folder').length];
  t.flags.failFolders = false;
  section(doc, 'scope').querySelector('.int-mcp-search').click();
  await wait(100);
  section(doc, 'scope').querySelectorAll('.int-mcp-folder')[0].click();
  await wait(150);
  const refused = [section(doc, 'scope').querySelector('.int-mcp-smsg').textContent, section(doc, 'scope').querySelector('.int-mcp-folder').disabled];

  //then
  assert.deepStrictEqual(failed, [BAD_GATEWAY, 0], 'a 502 shows its text and lists nothing');
  assert.deepStrictEqual(refused, [PICK, false], 'a 422 shows its text and the folder can be tried again');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: a failed folder list and a refused pick show their texts');
}

async function aReopenWaitsForAnInFlightWrite() {
  //given
  const t = await boot();
  const {doc} = t;
  let release;
  const held = new Promise((r) => { release = r; });
  t.flags.hook = async (method, p) => { if (method === 'PUT' && p === BASE + 'main/tools') await held; return null; };
  await open(t);
  await openInstance(t, 'main');
  toolSwitch(doc, 'search').click();
  await wait(40);

  //when
  panel(doc).querySelector('.int-close').click();
  await wait(40);
  await openInstance(t, 'main');
  await wait(60);
  const early = t.calls.filter((c) => c[0] === 'GET' && c[1] === BASE + 'main').length;
  release();
  await wait(150);
  const order = t.calls.filter((c) => (c[0] === 'GET' && c[1] === BASE + 'main') || (c[0] === 'PUT' && c[1] === BASE + 'main/tools')).map((c) => c[0]);

  //then
  assert.strictEqual(early, 1, 'only the first open read the view while the write was on its way');
  assert.deepStrictEqual(order, ['GET', 'PUT', 'GET'], 'the reopened panel reads the view after the write');
  assert.strictEqual(toolSwitch(doc, 'search').getAttribute('aria-checked'), 'false', 'and draws its answer');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: a reopened panel waits for an in-flight write of the same instance');
}

async function aSignInThatCameBackTooLateSaysSo() {
  //given
  const t = await boot({hash: '#t=integrations/signin-expired'});
  const {doc, w} = t;
  await wait(200);

  //when
  const note = doc.querySelector('#integrations .int-note');

  //then
  assert.deepStrictEqual([note.textContent, note.classList.contains('bad')], ['The sign-in took too long or its link was already used; open the connection and start it again.', true]);
  assert.ok(wrap(doc).hidden, 'no panel opens');
  assert.strictEqual(w.location.hash, '#t=integrations', 'the hash is cleaned');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: a sign-in that comes back after its link expired says so on Integrations');
}

async function savingANewConnectionReopensItsPanel() {
  //given
  const t = await boot();
  const {doc, w} = t;
  t.flags.hook = async (method, p, body, {ok}) => {
    if (method === 'POST' && p === PLUGIN + '/instances') {
      t.flags.catalog.plugins[1].instances.push(instance(body.name));
      t.flags.views[body.name] = view(body.name, {listedAt: null, tools: []});
      return ok(instance(body.name), 201);
    }
    return null;
  };
  await open(t);
  await openPopup(t);
  mcpPicks(doc)[0].click();
  await wait(80);
  const form = panel(doc).querySelector('.int-form');
  setValue(w, form.querySelector('.int-name-in'), 'wiki');
  setValue(w, fieldIn(doc, 'url'), 'https://mcp.example.com/mcp');

  //when
  form.querySelector('.int-save').click();
  await wait(300);

  //then
  assert.ok(!wrap(doc).hidden, 'the panel stays open');
  assert.strictEqual(itab(doc, 'wiki').getAttribute('aria-selected'), 'true', 'on the new connection');
  assert.strictEqual(panel(doc).querySelector('.int-panel-note').textContent, 'wiki added: list its tools, then confirm the warnings');
  assert.ok(section(doc, 'tools').querySelector('.int-mcp-refresh'), 'with Refresh tools in reach');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: saving a new connection reopens its panel with the next steps');
}

async function anEmptyRefreshSaysTheServerOffersNothing() {
  //given
  const views_ = views();
  views_.main.tools = [];
  const t = await boot({views: views_});
  const {doc} = t;
  await open(t);
  await openInstance(t, 'main');

  //when
  section(doc, 'tools').querySelector('.int-mcp-refresh').click();
  await wait(150);

  //then
  assert.strictEqual(section(doc, 'tools').querySelector('.int-mcp-tmsg').textContent, NOTES.empty);
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: a Refresh that lists no tools says the server offers nothing');
}

async function theCatalogAndTheConnectionHeadDropTheirNoise() {
  //given
  const views_ = views();
  views_.east.auth.last = {result: 'connected', text: 'Connected', at: iso(30)};
  const t = await boot({views: views_});
  const {doc} = t;
  await open(t);

  //when
  await openPopup(t);
  const search = doc.querySelector('#int-pop .int-search').placeholder;
  await wait(80);
  const titles = ['atlassian', 'google-drive'].map((id) => presetPick(doc, id).title);
  doc.getElementById('int-add-btn').click();
  await openInstance(t, 'live');
  const primary = [!!panel(doc).querySelector('.int-primary'), !!panel(doc).querySelector('.int-primary-btn')];
  await openInstance(t, 'east');
  const last = section(doc, 'conn').querySelector('.int-mcp-last');

  //then
  assert.strictEqual(search, 'Search integrations', 'no count that leaves out the MCP entries');
  assert.deepStrictEqual(titles, ['Atlassian (Jira and Confluence)', 'Google Drive \u00b7 ' + PRESETS.texts.preview], 'a cut name can be read in full');
  assert.deepStrictEqual(primary, [false, false], 'an MCP connection has no primary');
  assert.strictEqual(last, null, 'a connected result does not repeat the Connected line');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: the catalog names in full, MCP connections have no primary, and Connected is said once');
}

async function aRefreshThatFindsTheAddressMovedOffersReconnect() {
  //given
  const t = await boot();
  const {doc} = t;
  const moved = 'MCP server east needs a new sign-in: its address changed';
  t.flags.hook = async (method, p, body, {bad}) => {
    if (method === 'POST' && p === BASE + 'east/tools/refresh') {
      t.flags.views.east.auth = {mode: 'oauth', state: 'reconnect', connectedBy: ALICE, connectedAt: iso(30), expiresAt: null, last: {result: 'reconnect', text: moved, at: iso(0)}};
      return bad(409, moved);
    }
    return null;
  };
  await open(t);
  await openInstance(t, 'east');
  const before = !!section(doc, 'conn').querySelector('.int-mcp-disconnect');

  //when
  section(doc, 'tools').querySelector('.int-mcp-refresh').click();
  await wait(200);
  const conn = section(doc, 'conn'), said = section(doc, 'tools').querySelector('.int-mcp-tmsg');

  //then
  assert.ok(before, 'the panel showed the sign-in as connected before the Refresh');
  assert.strictEqual(conn.querySelector('.int-mcp-state').textContent, 'Needs a new sign-in', 'the panel no longer says Connected');
  assert.strictEqual(conn.querySelector('.int-mcp-connect') && conn.querySelector('.int-mcp-connect').textContent, 'Reconnect', 'Reconnect is offered at once');
  assert.ok(!conn.querySelector('.int-mcp-disconnect'), 'and Disconnect is gone');
  assert.deepStrictEqual([said.textContent, said.classList.contains('bad')], [moved, true], 'the refusal is still said');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: a Refresh that finds the address moved re-reads the view and offers Reconnect');
}

async function aConfirmAnswerAfterAddInstanceKeepsTheAddForm() {
  //given
  const t = await boot();
  const {doc, w} = t;
  let release;
  const held = new Promise((r) => { release = r; });
  t.flags.hook = async (method, p) => { if (method === 'POST' && p === BASE + 'main/confirm') await held; return null; };
  await open(t);
  await openInstance(t, 'main');
  const warn = section(doc, 'warn');
  warn.querySelector('.int-mcp-agree').click();
  warn.querySelector('.int-mcp-confirm').click();
  await wait(40);
  panel(doc).querySelector('.int-add').click();
  await wait(40);
  const name = panel(doc).querySelector('.int-form .int-name-in');
  setValue(w, name, 'wiki');
  name.focus();

  //when
  release();
  await wait(200);
  const kept = [panel(doc).querySelector('.int-form .int-name-in') === name, name.value, doc.activeElement === name, panel(doc).querySelector('.int-add').getAttribute('aria-pressed')];
  const from = t.calls.length;
  itab(doc, 'main').click();
  await wait(150);

  //then
  assert.deepStrictEqual(kept, [true, 'wiki', true, 'true'], 'the answer does not repaint the add form, so what was typed and the focus stay');
  assert.ok(section(doc, 'warn').querySelector('.int-mcp-confirmed'), 'back on the tab, the held answer is drawn');
  assert.strictEqual(t.calls.slice(from).filter((c) => c[0] === 'GET' && c[1] === BASE + 'main').length, 0, 'from the held view, with no second read');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: a confirm answer that lands after + Add instance keeps the add form and is drawn back on the tab');
}

async function savingAMovedUrlReopensThePanelWithTheNextSteps() {
  //given
  const t = await boot();
  const {doc, w} = t;
  const moved = 'https://mcp.example.com/moved';
  t.flags.hook = async (method, p, body, {ok}) => {
    if (method === 'PATCH' && p === PLUGIN + '/instances/main') {
      const it = t.flags.catalog.plugins[1].instances.filter((i) => i.name === 'main')[0];
      it.values = Object.assign({}, it.values, {url: body.values.url});
      return ok({instance: it});
    }
    return null;
  };
  await open(t);
  await openInstance(t, 'main');
  setValue(w, fieldIn(doc, 'url'), moved);

  //when
  const from = t.calls.length;
  panel(doc).querySelector('.int-form .int-save').click();
  await wait(300);

  //then
  assert.deepStrictEqual(t.calls.slice(from).filter((c) => c[0] === 'PATCH').map((c) => c[2].values.url), [moved]);
  assert.ok(!wrap(doc).hidden, 'the panel opens again');
  assert.strictEqual(itab(doc, 'main').getAttribute('aria-selected'), 'true', 'on the connection whose address moved');
  assert.strictEqual(panel(doc).querySelector('.int-panel-note').textContent, 'main saved: list its tools again, then confirm the warnings');
  assert.ok(t.calls.slice(from).some((c) => c[0] === 'GET' && c[1] === BASE + 'main'), 'and reads its view again');
  assert.ok(section(doc, 'tools').querySelector('.int-mcp-refresh'), 'with Refresh tools in reach');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: saving a new URL reopens the panel with the next steps');
}

async function backFromAddInstanceWaitsForAnInFlightWrite() {
  //given
  const t = await boot();
  const {doc} = t;
  let release;
  const held = new Promise((r) => { release = r; });
  t.flags.hook = async (method, p) => { if (method === 'PUT' && p === BASE + 'main/tools') await held; return null; };
  await open(t);
  await openInstance(t, 'main');
  toolSwitch(doc, 'search').click();
  await wait(40);

  //when
  panel(doc).querySelector('.int-add').click();
  await wait(40);
  itab(doc, 'main').click();
  await wait(60);
  const early = t.calls.filter((c) => c[0] === 'GET' && c[1] === BASE + 'main').length;
  release();
  await wait(150);
  const order = t.calls.filter((c) => (c[0] === 'GET' && c[1] === BASE + 'main') || (c[0] === 'PUT' && c[1] === BASE + 'main/tools')).map((c) => c[0]);

  //then
  assert.strictEqual(early, 1, 'going back to the tab did not read the view while the write was on its way');
  assert.deepStrictEqual(order, ['GET', 'PUT', 'GET'], 'the tab reads the view after the write');
  assert.strictEqual(toolSwitch(doc, 'search').getAttribute('aria-checked'), 'false', 'and draws its answer');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS MCP panel: + Add instance and back to the tab waits for an in-flight write of that instance');
}

(async () => {
  await comingSoonEntryShowsAndHasNoAddControl();
  await theGroupListsTheBlankServerThePresetsAndTheSoonItemsAndTheGridLeavesMcpOut();
  await presetPrefillsUrlAndAuthAndNeverAValue();
  await theCatalogEntryIsAChipThatCanBeChangedOrCleared();
  await aPresetThatNeedsAClientSaysSoAboveTheOauthFields();
  await warningsShowBeforeConfirmAndTheSwitchStaysOff();
  await confirmPostsAndShowsWho();
  await aNoHintToolNeedsUnderstood();
  await aWriteToolSwitchIsLocked();
  await refreshToolsListsAgainAndShowsARefusal();
  await aLateReReadAfterARefusedRefreshNeverPaintsOverANewerLoad();
  await theFolderPickerOffersOnlyServerItems();
  await theOauthReturnShowsTheServerText();
  await theConnectionSectionConnectsAndDisconnects();
  await identitySitsNextToTheChatRoleField();
  await aToolSwitchKeepsTheFocusAfterItsRedraw();
  await oneToolWriteRunsAtATime();
  await theGateFailsClosed();
  await everyPanelWriteWaitsForTheOneInFlight();
  await aConnectionThatCannotBeAKnowledgeSourceShowsOneLockedControl();
  await saveTabsAndTheSwitchWaitForAToolsChangeInFlight();
  await theKnowledgeSelectIsLockedUntilTheViewIsIn();
  await evilServerTextIsText();
  await theNewControlsAreNamedAndTheSectionsLabelled();
  await theMcpStylesUseThemeTokensOnly();
  await aWarnHealthShowsAsAWarningAndNeedsAttention();
  await theCatalogEntryCanBeCleared();
  await aRefusedConnectionShowsTheRefusal();
  await theNotesComeFromTheViewEvenWithoutThePresets();
  await aConfirmThatNeedsAFolderPointsToThePicker();
  await failedFolderReadsShowTheirText();
  await aReopenWaitsForAnInFlightWrite();
  await aSignInThatCameBackTooLateSaysSo();
  await savingANewConnectionReopensItsPanel();
  await anEmptyRefreshSaysTheServerOffersNothing();
  await theCatalogAndTheConnectionHeadDropTheirNoise();
  await aRefreshThatFindsTheAddressMovedOffersReconnect();
  await aConfirmAnswerAfterAddInstanceKeepsTheAddForm();
  await savingAMovedUrlReopensThePanelWithTheNextSteps();
  await backFromAddInstanceWaitsForAnInFlightWrite();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
