const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const DAY = 1440;
const EVIL = '<img src=x onerror=alert(1)>"\'';
const E_ACUTE = String.fromCharCode(0xe9);
const LAST_ADMIN = 'The last admin keeps the admin role and stays enabled: make someone else an admin first';
const RULES = [
  {id: 'google', hosts: ['accounts.google.com'], suffixes: [], labels: [], paths: []},
  {id: 'microsoft', hosts: ['login.microsoftonline.com', 'sts.windows.net'], suffixes: [], labels: [], paths: []},
  {id: 'okta', hosts: [], suffixes: ['okta.com', 'oktapreview.com'], labels: [], paths: []},
  {id: 'keycloak', hosts: [], suffixes: [], labels: ['keycloak'], paths: ['/realms/']},
];

function people() {
  return [
    {email: 'alice@example.com', name: 'Alice Archer', role: 'admin', source: 'local', disabled: false, createdAt: iso(9000), lastLogin: iso(30), ssoLink: {state: 'bound', boundAt: iso(8000)}},
    {email: 'bob@example.com', name: 'Bob Baker', role: 'operator', source: 'sso', roleFrom: 'claim', disabled: false, createdAt: iso(8000), lastLogin: iso(3 * DAY)},
    {email: 'chloe@example.com', name: 'Chlo' + E_ACUTE + ' Durand', role: 'viewer', source: 'local', disabled: false, createdAt: iso(7000), lastLogin: null, ssoLink: null},
    {email: 'dana@example.com', name: 'Dana Diaz', role: 'viewer', source: 'sso', roleFrom: 'admin', disabled: false, createdAt: iso(60), lastLogin: null, opened: false},
    {email: 'erin@example.com', name: 'Erin Evans', role: 'operator', source: 'local', disabled: true, createdAt: iso(6000), lastLogin: iso(40 * DAY), ssoLink: null},
    {email: 'frank@example.com', name: 'Frank Fox', role: 'admin', source: 'local', disabled: true, createdAt: iso(5000), lastLogin: iso(90), ssoLink: null},
  ];
}

function tokens() {
  return [
    {id: 'a1b2c3d4', name: 'ci', role: 'operator', createdBy: 'alice@example.com', createdAt: iso(20 * DAY), lastUsed: iso(5), revoked: false},
    {id: 'b2c3d4e5', name: 'deploy-bot', role: 'viewer', createdBy: 'alice@example.com', createdAt: iso(25 * DAY), lastUsed: null, revoked: false, expiresAt: iso(-3 * DAY)},
    {id: 'c3d4e5f6', name: 'nightly', role: 'viewer', createdBy: 'alice@example.com', createdAt: iso(40 * DAY), lastUsed: null, revoked: false, expiresAt: iso(-60 * DAY)},
  ];
}

function ssoView(over) {
  const v = (value, source) => ({value, source: source || 'default', from: null});
  return Object.assign({
    enabled: true, active: true, problem: null, redirectUri: 'https://skopax.example.com/authorized', provider: {ok: true, since: null, error: null},
    values: {issuer: v('https://acme.okta.com', 'admin'), 'client-id': v('skopax', 'admin'), scope: v('openid email profile'), claims: v('id-token'), 'client-auth': v('auto'),
      'require-verified-email': v('false'), 'authorize-url': v(''), 'token-url': v(''), 'jwks-url': v(''), 'roles-claim': v('groups', 'admin'), 'role-map': v(''), 'default-role': v('viewer'),
      'new-accounts': v('added-only', 'admin'), 'allowed-domains': v('example.com, example.org', 'admin'), 'button-text': v(''), 'button-icon': v('auto')},
    bases: {}, switch: {value: 'true', source: 'admin', from: null, base: 'default', baseValue: true}, secretNote: null,
    secret: {set: true, from: null, unreadable: false, updatedBy: null, updatedAt: null}, roleMapInUse: [], mcpBase: 'https://skopax.example.com',
    icon: {set: false, type: null, size: 0, version: null},
    button: {defaultText: 'Sign in with SSO', textMax: 40, iconMax: 65536, marks: RULES, branded: {microsoft: 'Sign in with Microsoft'}},
  }, over || {});
}

function settings() {
  const item = (key, group, value, extra) => Object.assign({key, group, label: key, help: key + ' help.', kind: 'text', live: true, value, base: value, default: '', source: 'default', by: null, at: null}, extra || {});
  return [item('mcp.client-id', 'MCP', 'skopax-mcp'), item('mcp.scope', 'MCP', 'mcp'), item('core.mcp', 'Features', 'true', {kind: 'bool'})];
}

async function boot(opts) {
  opts = opts || {};
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state));
  const db = {users: opts.users || people(), tokens: opts.tokens || tokens(), sso: opts.sso || ssoView(), items: opts.items || settings()};
  const flags = {hook: null};
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=anoms', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, o) => {
        const p = String(path), method = (o && o.method) || 'GET';
        const raw = o && o.body, type = o && o.headers && o.headers['Content-Type'];
        const body = raw && type === 'application/json' ? JSON.parse(raw) : raw || null;
        const ok = (b, status) => ({ok: true, status: status || 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        const none = () => ({ok: true, status: 204, json: async () => { throw new Error('no body'); }, text: async () => ''});
        const bad = (status, detail, field) => ({ok: false, status, json: async () => (field === undefined ? {detail} : {detail, field})});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, sso: true, user: 'alice@example.com', userRole: 'admin', source: 'local', canChangePassword: true, operator: true, admin: true, teams: [], tags: []});
        if (!p.startsWith('/api/admin')) {
          if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
          if (p.startsWith('/api/rev')) return ok({rev: 1});
          return {ok: false, status: 404, json: async () => ({})};
        }
        calls.push([method, p, body, type]);
        const hooked = flags.hook && await flags.hook(method, p, body, {ok, bad, none, db});
        if (hooked) return hooked;
        if (p === '/api/admin/users' && method === 'GET') return opts.usersDown ? bad(503, 'Database unavailable') : ok(db.users);
        if (p === '/api/admin/tokens' && method === 'GET') return opts.tokensDown ? bad(503, 'Database unavailable') : ok(db.tokens);
        if (p.startsWith('/api/admin/users/') && method === 'PATCH') {
          const email = decodeURIComponent(p.split('/').pop());
          db.users = db.users.map((u) => u.email === email ? Object.assign({}, u, body) : u);
          return ok(db.users.find((u) => u.email === email));
        }
        if (p === '/api/admin/tokens' && method === 'POST') {
          const doc = Object.assign({id: '0a1b2c3d', name: body.name, role: body.role, createdBy: 'alice@example.com', createdAt: iso(0), lastUsed: null, revoked: false},
            body.expiresDays ? {expiresAt: iso(-body.expiresDays * DAY)} : {});
          db.tokens = db.tokens.concat([doc]);
          return ok(Object.assign({}, doc, {token: 'skopax_0a1b2c3d_SECRETSECRETSECRETSECRET'}), 201);
        }
        if (p.startsWith('/api/admin/tokens/') && method === 'DELETE') return none();
        if (p === '/api/admin/sso' && method === 'GET') return opts.ssoDown ? bad(503, 'Database unavailable') : ok(db.sso);
        if (p === '/api/admin/sso' && method === 'PUT') {
          const values = Object.assign({}, db.sso.values);
          Object.keys(body.values || {}).forEach((k) => { values[k] = body.values[k] === null ? {value: '', source: 'default', from: null} : {value: body.values[k], source: 'admin', from: null}; });
          db.sso = Object.assign({}, db.sso, {values});
          return ok(db.sso);
        }
        if (p === '/api/admin/sso/icon' && method === 'PUT') {
          db.sso = Object.assign({}, db.sso, {icon: {set: true, type: type, size: 120, version: 'abc123'}});
          return ok(db.sso);
        }
        if (p === '/api/admin/sso/icon' && method === 'DELETE') {
          db.sso = Object.assign({}, db.sso, {icon: {set: false, type: null, size: 0, version: null}});
          return ok(db.sso);
        }
        if (p.startsWith('/api/admin/settings') && method === 'GET') return ok({items: db.items, rev: 1, refreshSeconds: 15});
        if (p.startsWith('/api/admin/history')) return ok([]);
        if (p.startsWith('/api/admin/small-talk')) return bad(404, 'none');
        if (p.startsWith('/api/admin/features')) return ok({edition: 'community', items: []});
        if (p.startsWith('/api/admin/notices')) return ok({items: []});
        return bad(404, 'none');
      };
    }});
  await wait(150);
  const t = {w: dom.window, doc: dom.window.document, errors, calls, flags, db, close: () => dom.window.close()};
  t.doc.getElementById('tab-access').click();
  await wait(150);
  return t;
}

const setValue = (el, v) => { el.value = v; el.dispatchEvent(new el.ownerDocument.defaultView.Event('input', {bubbles: true})); };
const change = (el) => el.dispatchEvent(new el.ownerDocument.defaultView.Event('change', {bubbles: true}));
const key = (el, k) => el.dispatchEvent(new el.ownerDocument.defaultView.KeyboardEvent('keydown', {key: k, bubbles: true}));
const rowOf = (doc, email) => [...doc.querySelectorAll('#access .acc-user')].filter((r) => r.dataset.email === email)[0];
const tokenOf = (doc, id) => [...doc.querySelectorAll('#access .acc-token')].filter((r) => r.dataset.id === id)[0];
const nav = (doc) => Object.fromEntries([...doc.querySelectorAll('#access .acc-nav-b')].map((b) => [b.dataset.sec, [b.querySelector('.acc-nav-n').textContent, !b.querySelector('.acc-dot').hidden, b.title]]));
const visible = (doc) => [...doc.querySelectorAll('#access .acc-user')].filter((r) => !r.hidden).map((r) => r.dataset.email);
const card = (doc) => doc.querySelector('#access .sso-card');
const field = (doc, name) => [...doc.querySelectorAll('#access .sso-f')].filter((f) => f.dataset.field === name)[0];
const preview = (doc) => { const b = card(doc).querySelector('.sso-preview .sso-pv'), i = b.querySelector('.sso-pv-ico'); return [b.className, i ? i.style.backgroundImage : null, b.querySelector('.sso-pv-txt').textContent]; };

async function theSectionsComeInOrderWithANavThatCountsAndFlags() {
  //given
  const t = await boot();

  //then
  const order = [...t.doc.querySelectorAll('#access .acc-main > .acc-part')].map((p) => p.id);
  assert.deepStrictEqual(order, ['acc-people', 'acc-sso', 'acc-tokens', 'acc-mcp']);
  assert.deepStrictEqual(nav(t.doc), {people: ['6', false, ''], sso: ['On', false, ''], tokens: ['3', true, '1 expiring within 7 days or expired'], mcp: ['On', false, '']});
  assert.strictEqual(t.doc.querySelector('#acc-mcp .acc-mcp-pill').textContent, 'On');
  t.close();

  //given
  const s = await boot({sso: ssoView({active: false, problem: 'Single sign-on needs the client secret (sso.client-secret).'}), tokens: tokens().slice(0, 1)});

  //then
  assert.deepStrictEqual(nav(s.doc), {people: ['6', false, ''], sso: ['Needs setup', true, 'Single sign-on is on but needs setup'], tokens: ['1', false, ''],
    mcp: ['Off', true, 'The MCP server is switched on, but single sign-on is not on']});

  //given
  const bare = (value) => ({value, source: 'default', from: null});
  const unset = await boot({sso: ssoView({active: false, values: Object.assign({}, ssoView().values, {issuer: bare(''), 'client-id': bare('skopax')}), secret: {set: false, from: null, unreadable: false}})});

  //then
  assert.deepStrictEqual([nav(unset.doc).sso, nav(unset.doc).mcp], [['Not set up', false, ''], ['Off', false, '']], 'an install without SSO set up gets no warning for MCP being on by default');
  unset.close();

  //when
  s.doc.querySelector('#access .acc-nav-b[data-sec="tokens"]').click();

  //then
  assert.strictEqual(s.doc.activeElement, s.doc.querySelector('#acc-tokens h3'), 'the nav moves focus to the section heading');
  assert.deepStrictEqual(s.errors, []);
  s.close();
  return 'the sections are People, Single sign-on, API tokens and MCP sign-in, and the nav counts them and flags an expiring token, SSO needing setup and MCP on without SSO';
}

async function peopleShowAvatarMethodsAndLastSignIn() {
  //given
  const t = await boot();

  //then
  const alice = rowOf(t.doc, 'alice@example.com'), bob = rowOf(t.doc, 'bob@example.com'), dana = rowOf(t.doc, 'dana@example.com'), chloe = rowOf(t.doc, 'chloe@example.com');
  assert.deepStrictEqual([alice.querySelector('.acc-av').textContent, alice.querySelector('.acc-name').textContent, alice.querySelector('.acc-mail').textContent], ['A', 'Alice Archer', 'alice@example.com']);
  assert.deepStrictEqual([...alice.querySelectorAll('.acc-badge')].map((b) => b.textContent), ['Password', 'SSO'], 'a local account with a bound link signs in both ways');
  assert.deepStrictEqual([...bob.querySelectorAll('.acc-badge')].map((b) => b.textContent), ['SSO only']);
  assert.deepStrictEqual([...chloe.querySelectorAll('.acc-badge')].map((b) => b.textContent), ['Password']);
  assert.deepStrictEqual([alice.querySelector('.acc-last').textContent, bob.querySelector('.acc-last').textContent, rowOf(t.doc, 'frank@example.com').querySelector('.acc-last').textContent], ['30 min ago', '3 d ago', '1 h ago']);
  assert.deepStrictEqual([dana.querySelector('.acc-last').textContent, !!dana.querySelector('.acc-not-yet'), chloe.querySelector('.acc-last').textContent, !!chloe.querySelector('.acc-not-yet')],
    ['Not signed in yet', true, 'Not signed in yet', false]);
  assert.deepStrictEqual([...t.doc.querySelectorAll('#access .acc-table')][0].querySelector('thead').textContent, 'PersonRoleSign-inLast sign-inDisabledSSO link');
  assert.deepStrictEqual([...dana.children].map((c) => c.dataset.l || ''), ['Person', 'Role', 'Sign-in', 'Last sign-in', 'Disabled', 'SSO link', ''], 'each cell names its column for the phone layout');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'a person shows a letter avatar, name and email, Password and SSO badges, and last sign-in as relative time or Not signed in yet';
}

async function searchAndRoleFilterNarrowThePeople() {
  //given
  const t = await boot();
  const find = t.doc.querySelector('#access .acc-find'), roles = t.doc.querySelector('#access .acc-role-f');

  //when
  setValue(find, 'CHLOE');
  const upper = visible(t.doc);
  setValue(find, 'chlo' + E_ACUTE);
  const accented = visible(t.doc);
  setValue(find, 'example.com');
  roles.value = 'admin';
  change(roles);
  const admins = visible(t.doc);
  const shown = t.doc.querySelector('#access .acc-shown').textContent;
  setValue(find, 'nobody-here');
  const none = [visible(t.doc), t.doc.querySelector('#access .acc-none').hidden];
  setValue(find, '');
  roles.value = '';
  change(roles);

  //then
  assert.deepStrictEqual(upper, ['chloe@example.com'], 'case does not matter');
  assert.deepStrictEqual(accented, ['chloe@example.com'], 'accents do not matter');
  assert.deepStrictEqual(admins, ['alice@example.com', 'frank@example.com']);
  assert.strictEqual(shown, '2 of 6 shown');
  assert.deepStrictEqual(none, [[], false], 'no match says so');
  assert.strictEqual(visible(t.doc).length, 6);

  //when
  setValue(find, 'dana');
  const sel = rowOf(t.doc, 'erin@example.com').querySelector('select.acc-role');
  sel.value = 'viewer';
  change(sel);
  await wait(100);

  //then
  assert.strictEqual(t.doc.querySelector('#access .acc-find').value, 'dana', 'the search survives a re-render');
  assert.deepStrictEqual(visible(t.doc), ['dana@example.com']);
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'search matches name or email ignoring case and accents, the role filter narrows, no match says so, and both survive a re-render';
}

async function theLastAdminKeepsRoleAndSignInWithTheReason() {
  //given
  const t = await boot();
  const alice = rowOf(t.doc, 'alice@example.com'), frank = rowOf(t.doc, 'frank@example.com');

  //then
  const sel = alice.querySelector('select.acc-role'), off = alice.querySelector('input.acc-off');
  assert.deepStrictEqual([sel.disabled, sel.title, off.disabled, off.title], [true, LAST_ADMIN, true, LAST_ADMIN]);
  assert.deepStrictEqual([alice.querySelector('.acc-last-admin').textContent, alice.querySelector('.acc-last-admin').title], ['Last admin', LAST_ADMIN]);
  const items = [...alice.querySelectorAll('.acc-menu [role=menuitem]')].map((m) => [m.textContent, m.disabled, m.title]);
  assert.deepStrictEqual(items, [['Change role', true, LAST_ADMIN], ['Reset password', false, ''], ['Disable sign-in', true, LAST_ADMIN]]);
  assert.ok(!frank.querySelector('select.acc-role').disabled && !frank.querySelector('.acc-last-admin'), 'a disabled admin does not count and is not the last admin');

  //when
  const fsel = frank.querySelector('input.acc-off');
  fsel.checked = false;
  change(fsel);
  await wait(100);

  //then
  assert.ok(!rowOf(t.doc, 'alice@example.com').querySelector('select.acc-role').disabled, 'with a second enabled admin, nobody is the last admin');
  assert.strictEqual(t.doc.querySelectorAll('#access .acc-last-admin').length, 0);
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'the last enabled admin has the role, Disabled and the menu items locked with the reason, and a second admin unlocks them';
}

async function theRowMenuWorksByKeyboard() {
  //given
  const t = await boot();
  const chloe = rowOf(t.doc, 'chloe@example.com'), menuB = chloe.querySelector('.acc-menu-b');

  //when
  menuB.click();
  const opened = [menuB.getAttribute('aria-expanded'), t.doc.activeElement.textContent];
  key(t.doc.activeElement, 'ArrowDown');
  const second = t.doc.activeElement.textContent;
  key(t.doc.activeElement, 'Escape');
  const closed = [chloe.querySelector('.acc-menu').hidden, t.doc.activeElement === menuB];
  menuB.click();
  key(t.doc.activeElement, 'ArrowDown');
  t.doc.activeElement.click();
  const pw = chloe.querySelector('input.acc-pw');

  //then
  assert.deepStrictEqual(opened, ['true', 'Change role']);
  assert.strictEqual(second, 'Reset password');
  assert.deepStrictEqual(closed, [true, true], 'Escape closes the menu and gives focus back');
  assert.ok(!chloe.querySelector('.acc-pw-form').hidden && t.doc.activeElement === pw, 'Reset password opens the password field');
  assert.deepStrictEqual([...rowOf(t.doc, 'dana@example.com').querySelectorAll('.acc-menu [role=menuitem]')].map((m) => m.textContent), ['Change role', 'Turn into local account', 'Disable sign-in']);
  assert.deepStrictEqual([...rowOf(t.doc, 'erin@example.com').querySelectorAll('.acc-menu [role=menuitem]')].map((m) => m.textContent), ['Change role', 'Reset password', 'Enable sign-in']);
  assert.deepStrictEqual([rowOf(t.doc, 'chloe@example.com').querySelector('.acc-mi-off').classList.contains('acc-mi-danger'), rowOf(t.doc, 'erin@example.com').querySelector('.acc-mi-off').classList.contains('acc-mi-danger')],
    [true, false], 'Disable is a danger action, Enable is not');

  //when
  rowOf(t.doc, 'erin@example.com').querySelector('.acc-mi-off').click();
  await wait(100);

  //then
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'PATCH').map((c) => c[2]), [{disabled: false}]);
  assert.strictEqual(t.doc.activeElement, rowOf(t.doc, 'erin@example.com').querySelector('.acc-menu-b'), 'focus is back on the menu button of the redrawn row');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'the row menu opens on the first item, moves with the arrows, closes on Escape back to its button, and offers Turn into local account and Enable only where they apply';
}

async function focusStaysOnTheControlAfterASaveRedraws() {
  //given
  const t = await boot();
  const sel = rowOf(t.doc, 'chloe@example.com').querySelector('select.acc-role');
  sel.focus();

  //when
  sel.value = 'operator';
  change(sel);
  await wait(100);

  //then
  const now = rowOf(t.doc, 'chloe@example.com').querySelector('select.acc-role');
  assert.notStrictEqual(now, sel, 'the list was redrawn');
  assert.strictEqual(t.doc.activeElement, now, 'focus is on the same control in the redrawn row');
  assert.strictEqual(rowOf(t.doc, 'chloe@example.com').querySelector('.adm-msg').textContent, 'Role set to operator');
  t.close();
  return 'after a save redraws the list, focus is back on the same control of the same row';
}

async function tokensShowExpiryChipsAndCreateWithAnExpiry() {
  //given
  const t = await boot();

  //then
  const exp = (id) => { const e = tokenOf(t.doc, id).querySelector('.acc-exp'); return [e.textContent, e.classList.contains('k-warn'), e.classList.contains('k-crit')]; };
  assert.deepStrictEqual(exp('a1b2c3d4'), ['Never', false, false]);
  assert.deepStrictEqual(exp('b2c3d4e5'), ['Expires in 3 days', true, false]);
  assert.deepStrictEqual(exp('c3d4e5f6')[1], false, 'a far expiry is a plain date');
  assert.deepStrictEqual(tokenOf(t.doc, 'a1b2c3d4').children[3].textContent, 'alice@example.com', 'Created by stays the fourth cell');

  //when
  setValue(t.doc.querySelector('#access .acc-add-token [name=name]'), 'weekly');
  t.doc.querySelector('#access .acc-add-token [name=expires]').value = '7';
  t.doc.querySelector('#access .acc-add-token button[type=submit]').click();
  await wait(100);

  //then
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'POST').map((c) => c[2]), [{name: 'weekly', role: 'viewer', expiresDays: 7}]);
  const secret = t.doc.querySelector('#access .acc-secret');
  assert.ok(/You will not see this token after you leave this tab/.test(secret.textContent), 'the once-only line');
  assert.strictEqual(t.doc.activeElement.textContent, 'Copy', 'focus lands on Copy');
  assert.deepStrictEqual(nav(t.doc).tokens.slice(0, 2), ['4', true]);

  //when
  t.doc.getElementById('tab-anoms').click();
  await wait(60);
  t.doc.getElementById('tab-access').click();
  await wait(150);

  //then
  assert.strictEqual(t.doc.querySelector('#access .acc-secret'), null, 'the token is not shown again after leaving the tab');
  assert.ok(!/SECRET/.test(t.doc.documentElement.outerHTML));
  t.close();

  //given
  const p = await boot({tokens: [{id: 'd4e5f6a7', name: 'old', role: 'viewer', createdBy: 'alice@example.com', createdAt: iso(40 * DAY), lastUsed: null, revoked: false, expiresAt: iso(2 * DAY)}]});

  //then
  const past = tokenOf(p.doc, 'd4e5f6a7').querySelector('.acc-exp');
  assert.ok(past.classList.contains('k-crit') && /^Expired /.test(past.textContent), 'a past expiry is a red chip');
  assert.deepStrictEqual(nav(p.doc).tokens.slice(0, 2), ['1', true]);
  p.close();
  return 'tokens show Never, a warn chip within 7 days and a red chip once expired, Create token sends expiresDays, and the secret is shown once';
}

async function revokeAsksFirst() {
  //given
  const t = await boot();

  //when
  tokenOf(t.doc, 'a1b2c3d4').querySelector('.acc-revoke').click();
  await wait(30);
  const asked = [t.doc.getElementById('cf-wrap').hidden, t.doc.getElementById('cf-title').textContent];
  t.doc.getElementById('cf-no').click();
  await wait(30);
  const kept = t.calls.filter((c) => c[0] === 'DELETE').length;
  tokenOf(t.doc, 'a1b2c3d4').querySelector('.acc-revoke').click();
  t.doc.getElementById('cf-yes').click();
  await wait(100);

  //then
  assert.deepStrictEqual(asked, [false, 'Revoke the token ci?']);
  assert.strictEqual(kept, 0, 'No keeps the token');
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'DELETE').map((c) => c[1]), ['/api/admin/tokens/a1b2c3d4']);
  assert.strictEqual(t.doc.activeElement, t.doc.querySelector('#acc-tokens h3'), 'focus lands on the API tokens heading, not on the closed dialog');

  //when
  t.w.__skopaxConfirm = undefined;
  tokenOf(t.doc, 'b2c3d4e5').querySelector('.acc-revoke').click();
  await wait(60);

  //then
  assert.strictEqual(t.calls.filter((c) => c[0] === 'DELETE').length, 1, 'without the confirmation dialog Revoke does nothing');
  t.close();
  return 'Revoke asks first: No keeps the token and Yes sends the DELETE';
}

async function eachSectionLoadsAndFailsOnItsOwn() {
  //given
  const t = await boot({tokensDown: true});

  //then
  assert.strictEqual(visible(t.doc).length, 6, 'people still show');
  assert.ok(/API tokens did not load\./.test(t.doc.querySelector('#acc-tokens .acc-failed').textContent));
  assert.ok(card(t.doc), 'single sign-on still shows');
  assert.deepStrictEqual(nav(t.doc).tokens, ['', true, 'API tokens did not load']);

  //when
  t.flags.hook = async (method, p, body, h) => { if (method === 'GET' && p === '/api/admin/tokens') { await wait(60); return h.ok(tokens()); } return null; };
  t.doc.querySelector('#acc-tokens .acc-again').click();
  const loading = [!!t.doc.querySelector('#acc-tokens .acc-loading'), !t.doc.querySelector('#acc-tokens .acc-failed')];
  await wait(200);

  //then
  assert.deepStrictEqual(loading, [true, true], 'Try again shows the loading state of its section');
  assert.ok(tokenOf(t.doc, 'a1b2c3d4'), 'Try again loads the tokens');
  t.close();

  //given
  const s = await boot({ssoDown: true, users: []});

  //then
  assert.ok(/Single sign-on did not load\./.test(s.doc.querySelector('#acc-sso .acc-failed').textContent));
  assert.strictEqual(s.doc.querySelector('#acc-people .acc-empty').textContent, 'No people yet: add the first person who signs in.');
  assert.ok(s.doc.querySelector('#acc-people .acc-add-open'), 'the empty list keeps its Add person action');
  assert.deepStrictEqual(s.errors, []);
  s.close();
  return 'a section that fails says so with Try again while the others render, and an empty list says what it is with its action';
}

async function chipsEditTheAllowedDomains() {
  //given
  const t = await boot();
  const chips = () => [...card(t.doc).querySelectorAll('.sso-chip .sso-chip-t')].map((c) => c.textContent);

  //when
  const before = chips();
  card(t.doc).querySelector('.sso-chip-del[data-domain="example.org"]').click();
  const add = card(t.doc).querySelector('.sso-chip-in');
  add.value = 'shop.example.com';
  key(add, 'Enter');
  const after = chips();
  card(t.doc).querySelector('.sso-save').click();
  await wait(100);

  //then
  assert.deepStrictEqual(before, ['example.com', 'example.org']);
  assert.deepStrictEqual(after, ['example.com', 'shop.example.com']);
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'PUT' && c[1] === '/api/admin/sso').map((c) => c[2]), [{values: {'allowed-domains': 'example.com, shop.example.com'}}]);
  assert.deepStrictEqual(chips(), ['example.com', 'shop.example.com']);
  t.close();
  return 'allowed domains are chips: Remove drops one, Enter adds one, and Save sends the list';
}

async function aDomainTypedButNotAddedIsSavedAndSurvivesARepaint() {
  //given
  const t = await boot();
  const add = () => card(t.doc).querySelector('.sso-chip-in');
  const label = field(t.doc, 'allowed-domains').querySelector('label.sso-fn');

  //when
  setValue(add(), 'example.net');
  field(t.doc, 'client-id').querySelector('.sso-reset').click();
  await wait(100);
  const kept = add().value;
  card(t.doc).querySelector('.sso-save').click();
  await wait(100);

  //then
  assert.strictEqual(label.getAttribute('for'), add().id, 'the label names the visible domain box');
  assert.strictEqual(kept, 'example.net', 'the typed domain survives a repaint');
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'PUT' && c[1] === '/api/admin/sso').map((c) => c[2]), [{values: {'client-id': null}}, {values: {'allowed-domains': 'example.com, example.org, example.net'}}]);
  assert.deepStrictEqual([...card(t.doc).querySelectorAll('.sso-chip .sso-chip-t')].map((c) => c.textContent), ['example.com', 'example.org', 'example.net']);
  assert.strictEqual(add().value, '', 'the box is empty once the domain is saved');
  t.close();
  return 'a domain typed in the box but not added is saved with Save and kept through a repaint, and the label names the box';
}

async function theMcpAndLastKnownRoleFieldsSaveWithTheirCard() {
  //given
  const items = settings().concat([Object.assign({}, settings()[0], {key: 'roles.stale-max-minutes', group: 'Sign-in', label: 'Last known role (minutes)', kind: 'int', value: '60', base: '60', low: 0, high: 1440})]);
  const t = await boot({items});
  t.flags.hook = async (method, p, body, h) => {
    if (method !== 'PUT' || p !== '/api/admin/settings') return null;
    if (body.key === 'mcp.scope') return h.bad(422, 'The scope is too long.');
    const item = Object.assign({}, h.db.items.find((i) => i.key === body.key), {value: body.value, source: 'admin', by: 'alice@example.com', at: iso(0)});
    h.db.items = h.db.items.map((i) => i.key === body.key ? item : i);
    return h.ok({item, change: {}});
  };
  const mcpIn = (key) => t.doc.querySelector('#acc-mcp .adm-row[data-key="' + key + '"] .adm-in');
  const stale = () => card(t.doc).querySelector('.sso-roles .adm-row[data-key="roles.stale-max-minutes"]');

  //then
  assert.strictEqual(t.doc.querySelectorAll('#acc-mcp .adm-save, #acc-sso .adm-save').length, 0, 'no Save per field');
  assert.strictEqual(t.doc.querySelectorAll('#acc-mcp .acc-mcp-save').length, 1, 'one Save for the MCP card');
  assert.ok(t.doc.querySelector('#acc-mcp .acc-fields') && card(t.doc).querySelector('.sso-roles.acc-fields'), 'both use the field layout');

  //when
  setValue(mcpIn('mcp.client-id'), 'skopax-mcp-2');
  setValue(mcpIn('mcp.scope'), 'mcp-long');
  t.doc.querySelector('#acc-mcp .acc-mcp-save').click();
  await wait(150);

  //then
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'PUT').map((c) => c[2]), [{key: 'mcp.client-id', value: 'skopax-mcp-2'}, {key: 'mcp.scope', value: 'mcp-long'}]);
  assert.strictEqual(mcpIn('mcp.client-id').value, 'skopax-mcp-2');
  assert.strictEqual(mcpIn('mcp.scope').value, 'mcp-long', 'the refused edit stays typed');
  assert.strictEqual(t.doc.querySelector('#acc-mcp .adm-row[data-key="mcp.scope"] .adm-msg').textContent, 'The scope is too long.');
  assert.ok(/^Saved/.test(t.doc.querySelector('#acc-mcp .adm-row[data-key="mcp.client-id"] .adm-msg').textContent));
  assert.strictEqual(t.doc.querySelector('#acc-mcp .acc-mcp-msg').textContent, 'Not saved: mcp.scope');

  //when
  setValue(stale().querySelector('.adm-in'), '30');
  card(t.doc).querySelector('.sso-save').click();
  await wait(150);

  //then
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'PUT').slice(2).map((c) => [c[1], c[2]]), [['/api/admin/settings', {key: 'roles.stale-max-minutes', value: '30'}]], 'the SSO card Save sends the role setting and no empty SSO save');
  assert.strictEqual(stale().querySelector('.adm-in').value, '30');
  assert.strictEqual(card(t.doc).querySelector('.sso-msg').textContent, 'Saved');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'the MCP fields and Last known role use the field layout and save with one Save per card, keeping a refused edit typed with its reason';
}

async function aRefusedRoleFieldOnTheSsoCardKeepsItsReason() {
  //given
  const items = settings().concat([Object.assign({}, settings()[0], {key: 'roles.stale-max-minutes', group: 'Sign-in', label: 'Last known role (minutes)', kind: 'int', value: '60', base: '60', low: 0, high: 1440})]);
  const t = await boot({items});
  let ssoDown = false;
  t.flags.hook = async (method, p, body, h) => {
    if (method === 'PUT' && p === '/api/admin/settings') return h.bad(422, 'Last known role is 0 to 1440 minutes.');
    if (method === 'PUT' && p === '/api/admin/sso' && ssoDown) return h.bad(503, 'Database unavailable');
    return null;
  };
  const stale = () => card(t.doc).querySelector('.sso-roles .adm-row[data-key="roles.stale-max-minutes"]');

  //when
  setValue(stale().querySelector('.adm-in'), '5000');
  setValue(field(t.doc, 'scope').querySelector('.sso-in'), 'openid email');
  card(t.doc).querySelector('.sso-save').click();
  await wait(150);
  const first = [stale().querySelector('.adm-in').value, stale().querySelector('.adm-msg').textContent, card(t.doc).querySelector('.sso-msg').textContent];
  ssoDown = true;
  setValue(field(t.doc, 'scope').querySelector('.sso-in'), 'openid profile');
  card(t.doc).querySelector('.sso-save').click();
  await wait(150);
  const both = [stale().querySelector('.adm-msg').textContent, card(t.doc).querySelector('.sso-msg').textContent, card(t.doc).querySelector('.sso-msg').classList.contains('bad')];

  //then
  assert.deepStrictEqual(first, ['5000', 'Last known role is 0 to 1440 minutes.', 'Not saved: Last known role (minutes)'], 'the SSO save went through and the refused row keeps its value and reason');
  assert.deepStrictEqual(both, ['Last known role is 0 to 1440 minutes.', 'Database unavailable; Not saved: Last known role (minutes)', true], 'both failures are said');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'a refused Last known role keeps its typed value and reason through the SSO repaint, and when the SSO save fails too both reasons show';
}

async function theCardGroupsItsFieldsAndShowsItsStatus() {
  //given
  const t = await boot();

  //then
  const groups = [...card(t.doc).querySelectorAll('.sso-grp')].map((g) => [g.querySelector('legend').textContent, [...g.querySelectorAll('.sso-f')].map((f) => f.dataset.field)]);
  assert.deepStrictEqual(groups, [['Provider', ['issuer', 'client-id', 'client-secret', 'scope', 'claims', 'require-verified-email']], ['Who can sign in', ['new-accounts', 'allowed-domains']],
    ['Roles', ['roles-claim', 'role-map', 'default-role']], ['Sign-in button', ['button-text', 'button-icon']]]);
  assert.strictEqual(card(t.doc).querySelector('.sso-status').textContent, 'Provider: Okta \u00b7 acme.okta.com \u00b7 Not tested since this page opened');
  assert.ok(card(t.doc).querySelector('.sso-adv') && !card(t.doc).querySelector('.sso-adv').open, 'Advanced stays folded');
  t.close();
  return 'the SSO card shows provider, issuer host and test state, and groups Provider, Who can sign in, Roles and Sign-in button';
}

async function thePreviewFollowsUnsavedEdits() {
  //given
  const t = await boot();
  const issuer = field(t.doc, 'issuer').querySelector('.sso-in'), text = field(t.doc, 'button-text').querySelector('.sso-in'), mode = field(t.doc, 'button-icon').querySelector('.sso-in');
  const note = () => { const n = card(t.doc).querySelector('.sso-pv-note'); return n.hidden ? '' : n.textContent; };

  //when
  const first = preview(t.doc);
  setValue(issuer, 'https://login.microsoftonline.com/tenant/v2.0');
  const microsoft = preview(t.doc);
  setValue(text, 'Sign in with Acme');
  const custom = [preview(t.doc), note()];
  setValue(issuer, 'https://accounts.google.com.evil.example');
  const lookAlike = preview(t.doc);
  setValue(text, EVIL);
  const hostile = preview(t.doc);
  mode.value = 'none';
  change(mode);
  const plain = preview(t.doc);
  mode.value = 'company';
  change(mode);
  const company = [preview(t.doc), note()];

  //then
  assert.deepStrictEqual(first, ['sso-pv sso-btn-okta', 'url("/icons/sso/okta.svg")', 'Sign in with SSO']);
  assert.deepStrictEqual(microsoft, ['sso-pv sso-btn-microsoft', 'url("/icons/sso/microsoft.svg")', 'Sign in with Microsoft']);
  assert.deepStrictEqual(custom, [['sso-pv sso-btn-key', 'url("/icons/sso/key.svg")', 'Sign in with Acme'], 'The Microsoft mark goes only with the text Sign in with Microsoft, so this text gets the key icon.']);
  assert.deepStrictEqual(lookAlike, ['sso-pv sso-btn-key', 'url("/icons/sso/key.svg")', 'Sign in with Acme']);
  assert.deepStrictEqual(hostile[2], EVIL, 'the text is text');
  assert.strictEqual(t.doc.querySelectorAll('#access img').length, 0, 'markup in the text never becomes an element');
  assert.deepStrictEqual(plain, ['sso-pv sso-btn-none', null, EVIL]);
  assert.deepStrictEqual(company, [['sso-pv sso-btn-company', null, EVIL], 'No company icon uploaded yet, so the button shows text only.']);
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'PUT'), [], 'nothing was saved');
  t.close();
  return 'the button preview follows unsaved issuer, text and icon edits, keeps the Microsoft mark to its own wording, shows look-alikes the key icon and hostile text as text';
}

async function unsavedButtonEditsSurviveASaveOfAnotherField() {
  //given
  const t = await boot();
  setValue(field(t.doc, 'button-text').querySelector('.sso-in'), 'Sign in with Acme');
  const mode = field(t.doc, 'button-icon').querySelector('.sso-in');
  mode.value = 'none';
  change(mode);

  //when
  field(t.doc, 'client-id').querySelector('.sso-reset').click();
  await wait(100);

  //then
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'PUT').map((c) => c[2]), [{values: {'client-id': null}}]);
  assert.strictEqual(field(t.doc, 'button-text').querySelector('.sso-in').value, 'Sign in with Acme');
  assert.strictEqual(field(t.doc, 'button-icon').querySelector('.sso-in').value, 'none');
  assert.deepStrictEqual(preview(t.doc), ['sso-pv sso-btn-none', null, 'Sign in with Acme'], 'the preview follows the kept edits');

  //when
  card(t.doc).querySelector('.sso-discard').click();

  //then
  assert.strictEqual(field(t.doc, 'button-text').querySelector('.sso-in').value, '', 'Discard changes drops them');
  assert.strictEqual(t.doc.activeElement, card(t.doc).querySelector('.sso-discard'));
  t.close();
  return 'unsaved button text and icon survive a reset of another field, and Discard changes drops them';
}

async function theCompanyIconUploadsAndRemoves() {
  //given
  const t = await boot();
  const pick = (name, type, size) => {
    const input = card(t.doc).querySelector('.sso-icon-file');
    const file = new t.w.File([new Uint8Array(size)], name, {type});
    Object.defineProperty(input, 'files', {configurable: true, value: [file]});
    change(input);
  };

  //when
  pick('big.png', 'image/png', 65537);
  const big = card(t.doc).querySelector('.sso-icon-msg').textContent;
  pick('logo.gif', 'image/gif', 10);
  const gif = card(t.doc).querySelector('.sso-icon-msg').textContent;
  pick('logo.svg', 'image/svg+xml', 120);
  await wait(100);

  //then
  assert.strictEqual(big, 'The icon is at most 64 KiB.');
  assert.strictEqual(gif, 'The icon must be a PNG, WebP or SVG image.');
  const put = t.calls.filter((c) => c[0] === 'PUT' && c[1] === '/api/admin/sso/icon');
  assert.strictEqual(put.length, 1);
  assert.strictEqual(put[0][3], 'image/svg+xml', 'the raw file goes with its own type');
  assert.strictEqual(card(t.doc).querySelector('.sso-icon-state').textContent, 'SVG, 120 bytes');
  assert.strictEqual(card(t.doc).querySelector('.sso-icon-msg').textContent, 'Icon uploaded');

  //when
  const mode = field(t.doc, 'button-icon').querySelector('.sso-in');
  mode.value = 'company';
  change(mode);
  const shown = preview(t.doc);
  card(t.doc).querySelector('.sso-icon-del').click();
  await wait(20);
  const asked = t.doc.getElementById('cf-title').textContent;
  t.doc.getElementById('cf-yes').click();
  await wait(100);

  //then
  assert.deepStrictEqual(shown, ['sso-pv sso-btn-company', 'url("/login/sso-icon?v=abc123")', 'Sign in with SSO']);
  assert.strictEqual(asked, 'Remove the company icon?');
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'DELETE').map((c) => c[1]), ['/api/admin/sso/icon']);
  assert.strictEqual(card(t.doc).querySelector('.sso-icon-state').textContent, 'None uploaded');
  assert.strictEqual(field(t.doc, 'button-icon').querySelector('.sso-in').value, 'company', 'the unsaved mode survives the upload and removal');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'the company icon refuses a big or wrong file before sending, uploads the raw file, shows in the preview, and Remove asks first';
}

(async () => {
  const cases = [theSectionsComeInOrderWithANavThatCountsAndFlags, peopleShowAvatarMethodsAndLastSignIn, searchAndRoleFilterNarrowThePeople, theLastAdminKeepsRoleAndSignInWithTheReason,
    theRowMenuWorksByKeyboard, focusStaysOnTheControlAfterASaveRedraws, tokensShowExpiryChipsAndCreateWithAnExpiry, revokeAsksFirst, eachSectionLoadsAndFailsOnItsOwn, chipsEditTheAllowedDomains, aDomainTypedButNotAddedIsSavedAndSurvivesARepaint, theMcpAndLastKnownRoleFieldsSaveWithTheirCard, aRefusedRoleFieldOnTheSsoCardKeepsItsReason,
    theCardGroupsItsFieldsAndShowsItsStatus, thePreviewFollowsUnsavedEdits, unsavedButtonEditsSurviveASaveOfAnotherField, theCompanyIconUploadsAndRemoves];
  for (const c of cases) console.log('PASS ' + await c());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
