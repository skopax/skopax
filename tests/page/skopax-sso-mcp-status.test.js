const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');
const {short} = require('./skopax-zone');
const {masks, inlineMask} = require('./mask');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const EVIL = '<img src=x onerror=alert(1)>"\'';
const SINCE = '2026-03-02T08:15:00Z';
const CHECKS = [
  {id: 'discovery', ok: true, text: 'The discovery document was read.'},
  {id: 'issuer', ok: true, text: 'The issuer matches.'},
  {id: 'pkce', ok: true, text: 'The provider supports S256.'},
  {id: 'keys', ok: false, text: 'The provider lists no signing keys.'},
  {id: 'client-auth', ok: true, text: 'Client authentication is supported.'},
  {id: 'redirect', ok: true, text: 'The redirect URI is an https address.'},
  {id: 'scope', ok: true, text: 'The scope includes openid.'},
];

function makeSso(over) {
  const v = (value, source, from) => ({value, source, from: from || null});
  return Object.assign({
    enabled: false, active: false, problem: null, redirectUri: 'https://skopax.example.com/authorized', provider: {ok: true, since: null, error: null},
    values: {
      issuer: v('https://idp.example.com', 'config', 'environment'), 'client-id': v('skopax', 'admin'), scope: v('openid email profile', 'default'),
      claims: v('id-token', 'default'), 'client-auth': v('auto', 'default'), 'require-verified-email': v('false', 'default'),
      'authorize-url': v('', 'default'), 'token-url': v('', 'default'), 'jwks-url': v('', 'default'), 'roles-claim': v('roles', 'default'),
      'role-map': v('[{"value":"ops-admins","role":"admin"},{"value":"ops-operators","role":"operator"}]', 'admin'), 'default-role': v('viewer', 'default'),
      'new-accounts': v('anyone', 'default'), 'allowed-domains': v('', 'default'),
    },
    bases: {issuer: 'environment', 'client-id': 'default', scope: 'default', claims: 'default', 'client-auth': 'default', 'require-verified-email': 'default', 'authorize-url': 'default',
      'token-url': 'default', 'jwks-url': 'default', 'roles-claim': 'default', 'role-map': 'default', 'default-role': 'default', 'new-accounts': 'default', 'allowed-domains': 'default'},
    switch: {value: 'false', source: 'default', from: null, base: 'default', baseValue: false}, secretNote: null,
    secret: {set: false, from: null, unreadable: false, updatedBy: null, updatedAt: null}, roleMapInUse: [], mcpBase: 'https://skopax.example.com',
  }, over || {});
}

function settingItem(key, group, label, value, extra) {
  return Object.assign({key, group, label, help: label + ' help.', kind: 'text', live: true, value, base: value, default: '', source: 'default', by: null, at: null}, extra || {});
}

async function boot(opts) {
  opts = opts || {};
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state));
  const sso = {now: opts.sso || makeSso()};
  const users = opts.users || [{email: 'alice@example.com', name: 'Alice', role: 'admin', source: 'local', disabled: false, createdAt: iso(50), ssoLink: null}];
  const items = opts.items || [settingItem('access.session-hours', 'Access', 'Session length (h)', '12', {kind: 'int', low: 1, high: 720})];
  const flags = {hook: null};
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=anoms', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      if (opts.pack) { w.SKOPAX_I18N = opts.pack; w.document.cookie = 'skopax_lang=' + Object.keys(opts.pack)[0] + '; path=/'; }
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, o) => {
        const p = String(path), method = (o && o.method) || 'GET';
        const body = o && o.body ? JSON.parse(o.body) : null;
        const ok = (b, status) => ({ok: true, status: status || 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        const bad = (status, detail, field) => ({ok: false, status, json: async () => (field === undefined ? {detail} : {detail, field})});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, sso: false, user: 'alice@example.com', userRole: 'admin', source: 'local', canChangePassword: true, operator: true, admin: true, teams: [], tags: []});
        if (p.startsWith('/api/admin')) {
          calls.push([method, p, body]);
          const hooked = flags.hook && await flags.hook(method, p, body, {ok, bad, sso});
          if (hooked) return hooked;
          if (p === '/api/admin/sso' && method === 'GET') return ok(sso.now);
          if (p === '/api/admin/sso' && method === 'PUT') {
            const values = Object.assign({}, sso.now.values);
            Object.keys(body.values || {}).forEach((k) => {
              if (k === 'client-secret') return;
              values[k] = body.values[k] === null ? {value: '', source: 'default', from: null} : {value: body.values[k], source: 'admin', from: null};
            });
            const typed = body.values ? body.values['client-secret'] : undefined;
            const secret = typed === '' ? {set: false, from: null, unreadable: false, updatedBy: null, updatedAt: null} : typeof typed === 'string' ? {set: true, from: null, unreadable: false, updatedBy: 'alice@example.com', updatedAt: iso(0)} : sso.now.secret;
            const enabled = typeof body.enabled === 'boolean' ? body.enabled : sso.now.enabled;
            sso.now = Object.assign({}, sso.now, {values, secret, enabled, active: enabled && !sso.now.problem});
            return ok(sso.now);
          }
          if (p === '/api/admin/sso/test' && method === 'POST') return ok({ok: false, summary: '1 check failed. The client secret is checked at the first sign-in.', ms: 42, checks: CHECKS});
          if (p === '/api/admin/users' && method === 'GET') return ok(users);
          if (p === '/api/admin/tokens' && method === 'GET') return ok([]);
          if (p === '/api/admin/settings?key=sso.enabled' && method === 'DELETE') {
            sso.now = Object.assign({}, sso.now, {enabled: false, active: false, switch: {value: 'false', source: 'config', from: 'environment', base: 'environment', baseValue: false}});
            return ok({item: {key: 'sso.enabled'}, change: {}});
          }
          if (p.startsWith('/api/admin/settings') && method === 'GET') return opts.settingsDown ? bad(503, 'Database unavailable') : ok({items, rev: 1, refreshSeconds: 15});
          if (p.startsWith('/api/admin/history')) return ok([]);
          if (p.startsWith('/api/admin/small-talk')) return bad(404, 'none');
          if (p.startsWith('/api/admin/features')) return ok({edition: 'community', items: []});
          if (p.startsWith('/api/admin/notices')) return ok({items: []});
        }
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(150);
  return {w: dom.window, doc: dom.window.document, errors, calls, flags, sso, close: () => dom.window.close()};
}

async function openAccess(t) {
  t.doc.getElementById('tab-access').click();
  await wait(150);
}

const mcpState = (over) => makeSso({active: true, enabled: true, mcp: Object.assign({
  state: 'waiting', missing: null, ssoOn: true, on: true, mounted: true, days: 7, people: 0, last: null,
  values: {'client-id': {value: 'skopax-mcp', default: true}, audience: {value: 'skopax-mcp', default: true}, issuer: {value: 'https://idp.example.com', default: false}},
}, over)});
const status = (t) => t.doc.querySelector('#acc-mcp .acc-mcp-status');
const mcpItems = [settingItem('mcp.client-id', 'MCP', 'Client id', 'skopax-mcp')];

async function theStatusSaysNotSetUpAndNamesTheFirstMissingPiece() {
  //given
  const t = await boot({sso: mcpState({state: 'not-set-up', missing: 'sso', ssoOn: false}), items: mcpItems});

  //when
  await openAccess(t);

  //then
  const box = status(t);
  assert.strictEqual(box.dataset.state, 'not-set-up');
  assert.strictEqual(box.querySelector('.acc-mcp-conn').textContent, 'Not set up');
  assert.strictEqual(box.querySelector('.acc-mcp-line').textContent, 'Single sign-on is not on');
  assert.ok(box.querySelector('.acc-mcp-conn').classList.contains('plain'));
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'the MCP status says Not set up with the first missing piece';
}

async function theStatusWaitsWhenNoOneConnectedAndMarksDefaults() {
  //given
  const t = await boot({sso: mcpState({}), items: mcpItems});

  //when
  await openAccess(t);

  //then
  const box = status(t);
  assert.strictEqual(box.dataset.state, 'waiting');
  assert.ok(box.querySelector('.acc-mcp-conn').classList.contains('k-info'));
  assert.strictEqual(box.querySelector('.acc-mcp-line').textContent, 'Set up, no one has connected yet');
  const defaults = [...box.querySelectorAll('.acc-mcp-set-i')].filter((r) => r.querySelector('.acc-mcp-def')).map((r) => r.querySelector('dt').textContent);
  assert.deepStrictEqual(defaults, ['MCP client ID', 'MCP audience']);
  const setText = box.querySelector('.acc-mcp-set').textContent;
  assert.ok(/Single sign-on is on/.test(setText) && /The MCP endpoint is mounted/.test(setText));
  assert.strictEqual(box.querySelector('.acc-mcp-def').textContent, 'built-in default');
  assert.strictEqual(t.doc.querySelector('#acc-mcp .acc-card').firstElementChild.className, 'ai-head', 'the head stays first');
  assert.strictEqual(t.doc.querySelector('#acc-mcp .ai-head').nextElementSibling, box, 'the status block comes right after the head');
  t.close();
  return 'the MCP status waits when no one connected and marks which values are defaults';
}

async function theStatusShowsPeopleLastSignInAndTheNameNotTheEmail() {
  //given
  const last = iso(5);
  const t = await boot({sso: mcpState({state: 'connected', people: 3, last: {at: last, by: 'Bob Stone'}}), items: mcpItems});

  //when
  await openAccess(t);

  //then
  const box = status(t);
  assert.ok(box.querySelector('.acc-mcp-conn').classList.contains('k-ok'));
  assert.strictEqual(box.querySelector('.acc-mcp-conn').textContent, 'Connected');
  assert.strictEqual(box.querySelector('.acc-mcp-people').textContent, '3 people in the last 7 days');
  assert.strictEqual(box.querySelector('.acc-mcp-last-by').textContent, 'Bob Stone');
  assert.strictEqual(box.querySelector('.acc-mcp-last-at').textContent, t.w.dayhmZone(last));
  assert.ok(!/@/.test(box.textContent));
  t.close();
  return 'the MCP status shows the count, the last sign-in time and the name';
}

async function theStatusTranslatesAndKeepsNamesAsTheyAre() {
  //given
  const pack = {de: {Connected: 'Verbunden', 'Last sign-in': 'Letzte Anmeldung', 'Signed in by': 'Angemeldet von', '{0} people in the last {1} days': '{0} Personen in den letzten {1} Tagen', 'MCP client ID': 'MCP-Client-ID', 'built-in default': 'Standardwert', 'Single sign-on is on': 'Single Sign-on ist eingeschaltet'}};
  const t = await boot({pack, sso: mcpState({state: 'connected', people: 3, last: {at: iso(5), by: 'Bob Stone'}}), items: mcpItems});

  //when
  await openAccess(t);

  //then
  const box = status(t);
  assert.strictEqual(box.querySelector('.acc-mcp-conn').textContent, 'Verbunden');
  assert.strictEqual(box.querySelector('.acc-mcp-people').textContent, '3 Personen in den letzten 7 Tagen');
  assert.strictEqual(box.querySelector('.acc-mcp-last-l').textContent, 'Letzte Anmeldung');
  assert.strictEqual(box.querySelector('.acc-mcp-last-by-l').textContent, 'Angemeldet von');
  assert.strictEqual(box.querySelector('.acc-mcp-last-by').textContent, 'Bob Stone');
  const de = box.querySelector('.acc-mcp-set').textContent;
  assert.ok(/MCP-Client-ID/.test(de) && /Standardwert/.test(de) && /Single Sign-on ist eingeschaltet/.test(de));
  t.close();
  return 'the MCP status is translated and a name stays as it is';
}

async function thePeopleListMarksMcpOnlyAndPageAndMcp() {
  //given
  const person = (email, name, mcp, login) => ({email, name, role: 'viewer', source: 'sso', disabled: false, createdAt: iso(500), lastLogin: login, mcp});
  const users = [person('alice@example.com', 'Alice Archer', {at: iso(3), only: true}, iso(3)), person('bob@example.com', 'Bob Baker', {at: iso(60), only: false}, iso(2 * 1440)),
    person('carol@example.com', 'Carol Cole', null, iso(20)), person('dan@example.com', 'Dan Dale', {at: iso(9), only: null}, iso(20))];
  const t = await boot({sso: mcpState({}), users, items: mcpItems});

  //when
  await openAccess(t);

  //then
  const rows = [...t.doc.querySelectorAll('#acc-people tbody tr, #access tbody tr')];
  const badge = (name) => { const r = rows.filter((x) => x.textContent.includes(name))[0]; const b = r.querySelector('.acc-mcp-badge'); return b ? [b.textContent, b.dataset.mcp, !!r.querySelector('.acc-mcp-use')] : null; };
  assert.deepStrictEqual([badge('Alice Archer'), badge('Bob Baker'), badge('Carol Cole')], [['MCP only', 'only', true], ['Page and MCP', 'both', true], null]);
  assert.deepStrictEqual(badge('Dan Dale'), ['Uses MCP', 'uses', true]);
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'the people list marks MCP only and Page and MCP, and nothing for page only';
}

async function thePeopleBadgesTranslate() {
  //given
  const pack = {de: {'MCP only': 'Nur MCP', 'Page and MCP': 'Seite und MCP', 'Uses MCP': 'Nutzt MCP', 'Never signed in on the page': 'Nie auf der Seite angemeldet'}};
  const users = [{email: 'alice@example.com', name: 'Alice Archer', role: 'viewer', source: 'sso', disabled: false, createdAt: iso(500), lastLogin: iso(3), mcp: {at: iso(3), only: true}}];
  users.push({email: 'dan@example.com', name: 'Dan Dale', role: 'viewer', source: 'sso', disabled: false, createdAt: iso(500), lastLogin: iso(3), mcp: {at: iso(3), only: null}});
  const t = await boot({pack, sso: mcpState({}), users, items: mcpItems});

  //when
  await openAccess(t);

  //then
  assert.strictEqual([...t.doc.querySelectorAll('.acc-mcp-badge')][1].textContent, 'Nutzt MCP');
  const b = t.doc.querySelector('.acc-mcp-badge');
  assert.strictEqual(b.textContent, 'Nur MCP');
  assert.strictEqual(b.title, 'Nie auf der Seite angemeldet');
  t.close();
  return 'the MCP badges and their tooltip are translated';
}

async function noStatusBlockWithoutStatusData() {
  //given
  const t = await boot({items: mcpItems});

  //when
  await openAccess(t);

  //then
  assert.strictEqual(status(t), null);
  assert.ok(t.doc.querySelector('#acc-mcp .acc-mcp-note'));
  t.close();
  return 'an older server without status data shows the card as before';
}

(async () => {
  console.log('PASS ' + await theStatusSaysNotSetUpAndNamesTheFirstMissingPiece());
  console.log('PASS ' + await theStatusWaitsWhenNoOneConnectedAndMarksDefaults());
  console.log('PASS ' + await theStatusShowsPeopleLastSignInAndTheNameNotTheEmail());
  console.log('PASS ' + await theStatusTranslatesAndKeepsNamesAsTheyAre());
  console.log('PASS ' + await thePeopleListMarksMcpOnlyAndPageAndMcp());
  console.log('PASS ' + await thePeopleBadgesTranslate());
  console.log('PASS ' + await noStatusBlockWithoutStatusData());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
