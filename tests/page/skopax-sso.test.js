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
  const items = opts.items || [settingItem('access.session-hours', 'Access', 'Session length (h)', '12', {kind: 'int', low: 1, high: 720})];
  const flags = {hook: null};
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=anoms', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
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
          if (p === '/api/admin/users' && method === 'GET') return ok([{email: 'alice@example.com', name: 'Alice', role: 'admin', source: 'local', disabled: false, createdAt: iso(50), ssoLink: null}]);
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
const setValue = (el, v) => { el.value = v; el.dispatchEvent(new el.ownerDocument.defaultView.Event('input', {bubbles: true})); };
const field = (doc, name) => [...doc.querySelectorAll('#access .sso-f')].filter((f) => f.dataset.field === name)[0];
const card = (doc) => doc.querySelector('#access .sso-card');
const button = (doc, cls) => card(doc).querySelector('.' + cls);
const puts = (t) => t.calls.filter((c) => c[0] === 'PUT' && c[1] === '/api/admin/sso').map((c) => c[2]);
const attrs = (root) => [...root.querySelectorAll('*')].map((e) => [...e.attributes].map((a) => a.name).join(' ')).join(' ');

async function rendersValuesWithSourceLabels() {
  //given
  const t = await boot();

  //when
  await openAccess(t);

  //then
  const box = t.doc.getElementById('access');
  assert.ok(card(t.doc), 'the Single sign-on card is shown');
  assert.ok(card(t.doc).compareDocumentPosition(box.querySelector('.acc-user')) & t.w.Node.DOCUMENT_POSITION_PRECEDING, 'the card sits below the people list, which comes first');
  assert.strictEqual(card(t.doc).querySelector('.sso-pill').textContent, 'Off');
  assert.strictEqual(field(t.doc, 'issuer').querySelector('.sso-in').value, 'https://idp.example.com');
  assert.strictEqual(field(t.doc, 'issuer').querySelector('.sso-src').textContent, 'from environment');
  assert.strictEqual(field(t.doc, 'client-id').querySelector('.sso-src').textContent, 'set here');
  assert.strictEqual(field(t.doc, 'scope').querySelector('.sso-src').textContent, 'default');
  assert.strictEqual(field(t.doc, 'scope').querySelector('.sso-in').value, 'openid email profile');
  assert.strictEqual(field(t.doc, 'claims').querySelector('select').value, 'id-token');
  assert.deepStrictEqual([...field(t.doc, 'claims').querySelectorAll('option')].map((o) => o.textContent), ['ID token', 'access token']);
  assert.strictEqual(field(t.doc, 'default-role').querySelector('select').value, 'viewer');
  assert.deepStrictEqual([...field(t.doc, 'default-role').querySelectorAll('option')].map((o) => o.value), ['viewer', 'operator']);
  assert.strictEqual(field(t.doc, 'require-verified-email').querySelector('input[type=checkbox]').checked, false);
  const adv = card(t.doc).querySelector('details.sso-adv');
  assert.ok(adv && !adv.open && /Advanced: endpoints/.test(adv.querySelector('summary').textContent), 'the endpoints fold is closed');
  assert.deepStrictEqual([...adv.querySelectorAll('.sso-f')].map((f) => f.dataset.field), ['authorize-url', 'token-url', 'jwks-url', 'client-auth', 'extra-token-headers']);
  assert.deepStrictEqual([...field(t.doc, 'role-map').querySelectorAll('.sso-map-row')].map((r) => [r.querySelector('.sso-map-v').value, r.querySelector('.sso-map-r').value]),
    [['ops-admins', 'admin'], ['ops-operators', 'operator']]);
  assert.strictEqual(field(t.doc, 'client-secret').querySelector('.sso-in').placeholder, 'not set');
  assert.ok(!field(t.doc, 'client-secret').querySelector('.sso-in').readOnly);
  assert.deepStrictEqual(t.errors, []);
  t.close();

  //given
  const pill = async (over) => {
    const b = await boot({sso: makeSso(over)});
    await openAccess(b);
    const text = card(b.doc).querySelector('.sso-pill').textContent, sw = card(b.doc).querySelector('.sso-sw').getAttribute('aria-checked');
    const said = [card(b.doc).querySelector('.sso-problem'), card(b.doc).querySelector('.sso-provider')].map((n) => n ? n.textContent : null);
    b.close();
    return [text, sw, said];
  };

  //then
  assert.deepStrictEqual(await pill({enabled: true, active: true}), ['On', 'true', [null, 'Identity provider: no problem seen']]);
  assert.deepStrictEqual(await pill({enabled: true, active: false, problem: 'Set the issuer URL (sso.issuer).'}), ['Needs attention', 'true', ['Set the issuer URL (sso.issuer).', null]]);
  assert.deepStrictEqual(await pill({enabled: true, active: true, provider: {ok: false, since: SINCE, error: 'discovery'}}),
    ['Needs attention', 'true', [null, 'Identity provider did not answer with its settings since ' + short(SINCE)]]);
  assert.deepStrictEqual(await pill({enabled: false, active: false, secret: {set: false, from: null, unreadable: true}}), ['Needs attention', 'false', [null, null]]);
  return 'the card renders every value with its source label, the pill says On, Off, Not set up or Needs attention, and the provider status shows';
}

async function aSourceOwnedSecretIsReadOnly() {
  //given
  const t = await boot({sso: makeSso({secret: {set: true, from: 'environment', unreadable: false, updatedBy: null, updatedAt: null}})});

  //when
  await openAccess(t);

  //then
  const input = field(t.doc, 'client-secret').querySelector('.sso-in');
  assert.strictEqual(input.placeholder, 'set \u00b7 from environment');
  assert.strictEqual(input.readOnly, true, 'a secret from the environment cannot be typed over');
  assert.strictEqual(input.value, '');
  assert.strictEqual(field(t.doc, 'client-secret').querySelector('.adm-help').textContent, 'Set in the environment; change it there.', 'its help says where to change it');
  t.close();

  //given
  const s = await boot({sso: makeSso({secret: {set: true, from: null, unreadable: false, updatedBy: 'alice@example.com', updatedAt: iso(5)}})});
  await openAccess(s);

  //then
  assert.strictEqual(field(s.doc, 'client-secret').querySelector('.sso-in').placeholder, 'set');
  assert.strictEqual(field(s.doc, 'client-secret').querySelector('.sso-in').readOnly, false);
  assert.strictEqual(field(s.doc, 'client-secret').querySelector('.adm-help').textContent, 'Saved sealed and never shown again.', 'a secret set here keeps the sealed help');
  s.close();
  return 'a source-owned secret is read-only with "set \u00b7 from environment", and a secret set here reads "set"';
}

async function theTypedSecretIsSentOnce() {
  //given
  const t = await boot();
  await openAccess(t);

  //when
  setValue(field(t.doc, 'client-secret').querySelector('.sso-in'), '  s3cret-value  ');
  button(t.doc, 'sso-save').click();
  await wait(80);
  const blank = field(t.doc, 'client-secret').querySelector('.sso-in').value;
  setValue(field(t.doc, 'scope').querySelector('.sso-in'), 'openid email');
  button(t.doc, 'sso-save').click();
  await wait(80);

  //then
  assert.deepStrictEqual(puts(t), [{values: {'client-secret': 's3cret-value'}}, {values: {scope: 'openid email'}}], 'only changed fields go out, the secret once');
  assert.strictEqual(blank, '', 'the secret input is blank after the save');
  assert.strictEqual(field(t.doc, 'client-secret').querySelector('.sso-in').placeholder, 'set');
  assert.strictEqual(card(t.doc).querySelector('.sso-msg').textContent, 'Saved');
  assert.ok(!/s3cret/.test(t.doc.documentElement.outerHTML), 'the secret is nowhere in the page');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'the typed secret is sent once, trimmed, the input is blank after save, and later saves send only what changed';
}

async function testConnectionListsSevenChecks() {
  //given
  const t = await boot();
  await openAccess(t);
  setValue(field(t.doc, 'issuer').querySelector('.sso-in'), 'https://login.example.com');

  //when
  button(t.doc, 'sso-test').click();
  await wait(80);

  //then
  const sent = t.calls.filter((c) => c[1] === '/api/admin/sso/test');
  assert.deepStrictEqual(sent, [['POST', '/api/admin/sso/test', {values: {issuer: 'https://login.example.com'}}]], 'Test sends the typed values');
  const rows = [...card(t.doc).querySelectorAll('.sso-checks .sso-check')];
  assert.strictEqual(rows.length, 7);
  assert.deepStrictEqual(rows.map((r) => r.dataset.check), CHECKS.map((c) => c.id));
  assert.deepStrictEqual(rows.map((r) => r.querySelector('.sso-ok').textContent), ['ok', 'ok', 'ok', 'fail', 'ok', 'ok', 'ok']);
  const failed = rows.filter((r) => r.classList.contains('bad'));
  assert.deepStrictEqual(failed.map((r) => [r.querySelector('.sso-cn').textContent, r.querySelector('.sso-ct').textContent]), [['Signing keys', 'The provider lists no signing keys.']]);
  assert.ok(/1 check failed\. The client secret is checked at the first sign-in\. \u00b7 42 ms/.test(card(t.doc).querySelector('.sso-summary').textContent));
  assert.strictEqual(puts(t).length, 0, 'Test saves nothing');

  //when
  t.flags.hook = (method, p, body, h) => p === '/api/admin/sso/test' ? h.bad(409, 'A connection test is already running; try again in a moment.') : null;
  button(t.doc, 'sso-test').click();
  await wait(80);

  //then
  assert.strictEqual(card(t.doc).querySelector('.sso-msg').textContent, 'A connection test is already running; try again in a moment.');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'Test connection lists the seven checks with ok or fail and their texts, one failing, and a busy 409 says why';
}

async function saveAndTurnOnAndTheFieldError() {
  //given
  const t = await boot();
  await openAccess(t);
  t.flags.hook = (method, p, body, h) => method === 'PUT' && p === '/api/admin/sso' && body.values && body.values.issuer === 'idp.example.com' ? h.bad(422, 'The issuer URL must be an https address.', 'issuer') : null;

  //when
  setValue(field(t.doc, 'issuer').querySelector('.sso-in'), 'idp.example.com');
  button(t.doc, 'sso-on').click();
  await wait(80);

  //then
  assert.deepStrictEqual(puts(t), [{values: {issuer: 'idp.example.com'}, enabled: true}]);
  const issuer = field(t.doc, 'issuer');
  assert.ok(issuer.classList.contains('bad'), 'the issuer field is marked');
  assert.strictEqual(issuer.querySelector('.sso-in').getAttribute('aria-invalid'), 'true');
  assert.strictEqual(issuer.querySelector('.sso-fmsg').textContent, 'The issuer URL must be an https address.');
  assert.ok(!field(t.doc, 'scope').classList.contains('bad'), 'other fields are not marked');
  assert.strictEqual(card(t.doc).querySelector('.sso-pill').textContent, 'Off');

  //when
  setValue(issuer.querySelector('.sso-in'), 'https://idp2.example.com');
  const cleared = !issuer.classList.contains('bad');
  button(t.doc, 'sso-on').click();
  await wait(80);

  //then
  assert.ok(cleared, 'typing clears the mark');
  assert.deepStrictEqual(puts(t)[1], {values: {issuer: 'https://idp2.example.com'}, enabled: true});
  assert.strictEqual(card(t.doc).querySelector('.sso-pill').textContent, 'On');
  assert.strictEqual(card(t.doc).querySelector('.sso-msg').textContent, 'Saved and turned on');
  assert.ok(!button(t.doc, 'sso-on'), 'Save & turn on is gone once it is on');

  //when
  t.flags.hook = (method, p, body, h) => method === 'PUT' && p === '/api/admin/sso' && body.enabled === false ? h.bad(409, 'Switching single sign-on off needs an active local admin with a password.') : null;
  card(t.doc).querySelector('.sso-sw').click();
  await wait(20);
  t.doc.getElementById('cf-yes').click();
  await wait(80);

  //then
  assert.deepStrictEqual(puts(t)[2], {enabled: false}, 'the switch sends only the switch');
  assert.strictEqual(card(t.doc).querySelector('.sso-msg').textContent, 'Switching single sign-on off needs an active local admin with a password.');
  assert.strictEqual(card(t.doc).querySelector('.sso-sw').getAttribute('aria-checked'), 'true', 'the switch stays on');

  //when
  t.flags.hook = null;
  card(t.doc).querySelector('.sso-sw').click();
  await wait(20);
  t.doc.getElementById('cf-yes').click();
  await wait(80);

  //then
  assert.strictEqual(card(t.doc).querySelector('.sso-pill').textContent, 'Off');
  assert.strictEqual(card(t.doc).querySelector('.sso-msg').textContent, 'Turned off');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'Save & turn on sends PUT with enabled true, a 422 marks the issuer field, and the switch turns it off or says why not';
}

async function roleMapRowsAddAndRemove() {
  //given
  const t = await boot();
  await openAccess(t);
  const rows = () => [...field(t.doc, 'role-map').querySelectorAll('.sso-map-row')];

  //when
  field(t.doc, 'role-map').querySelector('.sso-map-add').click();
  const added = rows().length;
  setValue(rows()[2].querySelector('.sso-map-v'), ' ops-viewers ');
  rows()[2].querySelector('.sso-map-r').value = 'viewer';
  rows()[0].querySelector('.sso-map-del').click();
  button(t.doc, 'sso-save').click();
  await wait(80);

  //then
  assert.strictEqual(added, 3);
  assert.deepStrictEqual(puts(t), [{values: {'role-map': '[{"value":"ops-operators","role":"operator"},{"value":"ops-viewers","role":"viewer"}]'}}]);
  assert.deepStrictEqual([...rows()[0].querySelectorAll('.sso-map-r option')].map((o) => o.value), ['viewer', 'operator', 'admin']);

  //when
  rows().forEach((r) => r.querySelector('.sso-map-del').click());
  button(t.doc, 'sso-save').click();
  await wait(80);

  //then
  assert.deepStrictEqual(puts(t)[1], {values: {'role-map': ''}}, 'no rows saves an empty map');
  t.close();

  //given
  const full = JSON.stringify(Array.from({length: 50}, (_, i) => ({value: 'group-' + i, role: 'viewer'})));
  const f = await boot({sso: makeSso({values: Object.assign(makeSso().values, {'role-map': {value: full, source: 'admin', from: null}})})});
  await openAccess(f);
  const map = field(f.doc, 'role-map');

  //when
  map.querySelector('.sso-map-add').click();
  await wait(20);

  //then
  assert.strictEqual(map.querySelectorAll('.sso-map-row').length, 50, 'the 51st row is not added');
  assert.strictEqual(map.querySelector('.sso-fmsg').textContent, 'At most 50 rows.');
  assert.ok(map.classList.contains('bad'));
  assert.deepStrictEqual(f.calls.filter((c) => c[0] !== 'GET'), [], 'nothing is sent');
  assert.deepStrictEqual(f.errors, []);
  f.close();
  return 'role map rows add and remove, save as the JSON list, and the 51st row is refused before sending';
}

async function theRedirectUriIsShownWithCopy() {
  //given
  const t = await boot();
  await openAccess(t);
  const copied = [];
  Object.defineProperty(t.w.navigator, 'clipboard', {configurable: true, value: {writeText: async (text) => { copied.push(text); }}});
  const box = card(t.doc).querySelector('.sso-redirect');

  //when
  box.querySelector('.sso-copy').click();
  await wait(30);

  //then
  assert.ok(/Redirect URI to register/.test(box.textContent));
  assert.strictEqual(box.querySelector('code').textContent, 'https://skopax.example.com/authorized');
  assert.deepStrictEqual(copied, ['https://skopax.example.com/authorized']);
  assert.strictEqual(box.querySelector('.adm-msg').textContent, 'Copied');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'the redirect URI to register is shown read-only with Copy';
}

async function hostileValuesRenderAsText() {
  //given
  const v = makeSso().values;
  const values = Object.assign({}, v, {issuer: {value: EVIL, source: 'config', from: EVIL}, 'role-map': {value: JSON.stringify([{value: EVIL, role: 'admin'}]), source: 'admin', from: null}});
  const t = await boot({sso: makeSso({enabled: true, active: false, problem: EVIL, provider: {ok: false, since: SINCE, error: EVIL}, values})});

  //when
  await openAccess(t);
  t.flags.hook = (method, p, body, h) => p === '/api/admin/sso/test' ? h.ok({ok: false, summary: EVIL, ms: 1, checks: [{id: EVIL, ok: false, text: EVIL}]}) : null;
  button(t.doc, 'sso-test').click();
  await wait(80);

  //then
  const box = t.doc.getElementById('access');
  assert.strictEqual(field(t.doc, 'issuer').querySelector('.sso-in').value, EVIL);
  assert.strictEqual(field(t.doc, 'issuer').querySelector('.sso-src').textContent, 'from ' + EVIL);
  assert.strictEqual(card(t.doc).querySelector('.sso-problem').textContent, EVIL);
  assert.ok(card(t.doc).querySelector('.sso-provider').textContent.endsWith(': ' + EVIL));
  assert.strictEqual(field(t.doc, 'role-map').querySelector('.sso-map-v').value, EVIL);
  assert.strictEqual(card(t.doc).querySelector('.sso-check .sso-ct').textContent, EVIL);
  assert.strictEqual(box.querySelectorAll('img').length, 0, 'markup never becomes an element');
  assert.ok(!/onerror/.test(attrs(box)), 'no event handler attributes');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'hostile text in the issuer, problem, provider error, a role-map value, a source label and a check renders as text';
}

async function generalNoLongerListsSignInOrMcp() {
  //given
  const items = [settingItem('access.session-hours', 'Access', 'Session length (h)', '12', {kind: 'int', low: 1, high: 720}),
    settingItem('sso.issuer', 'Sign-in', 'Issuer URL', 'https://idp.example.com'), settingItem('mcp.audience', 'MCP', 'Audience', '', {source: 'admin', by: 'alice@example.com', at: iso(3)})];
  const t = await boot({items});

  //when
  t.doc.getElementById('tab-settings').click();
  await wait(150);

  //then
  const groups = [...t.doc.querySelectorAll('#admin details.adm-grp')].map((d) => d.dataset.group);
  assert.deepStrictEqual(groups, ['Access'], 'Sign-in and MCP are on Access & sign-in, not in General');
  assert.ok(!t.doc.querySelector('#admin .adm-row[data-key="sso.issuer"]') && !t.doc.querySelector('#admin .adm-row[data-key="mcp.audience"]'));
  assert.strictEqual(t.doc.getElementById('count-settings').textContent, '0', 'an MCP value set here is not counted in General');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'the General tab no longer lists the Sign-in and MCP groups';
}

async function theLastKnownRoleSettingSitsInTheSignInCardAndSavesFromThere() {
  //given
  const stale = settingItem('roles.stale-max-minutes', 'Sign-in', 'Last known role (minutes)', '60', {kind: 'int', low: 0, high: 1440});
  const items = [settingItem('access.session-hours', 'Access', 'Session length (h)', '12', {kind: 'int', low: 1, high: 720}), stale];
  const t = await boot({items});
  t.flags.hook = async (method, p, body, h) => {
    if (method === 'PUT' && p === '/api/admin/settings') return h.ok({item: Object.assign({}, stale, {value: body.value, source: 'admin'}), change: {}});
  };

  //when
  await openAccess(t);
  const row = card(t.doc).querySelector('.adm-row[data-key="roles.stale-max-minutes"]');
  const inGeneral = !!t.doc.querySelector('#admin .adm-row[data-key="roles.stale-max-minutes"]');
  const input = row.querySelector('.adm-in');
  setValue(input, '30');
  button(t.doc, 'sso-save').click();
  await wait(150);

  //then
  assert.ok(row, 'the setting is a row inside the Sign-in card');
  assert.strictEqual(inGeneral, false, 'and not in General');
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'PUT' && c[1] === '/api/admin/settings').map((c) => c[2]), [{key: 'roles.stale-max-minutes', value: '30'}]);
  const again = card(t.doc).querySelector('.adm-row[data-key="roles.stale-max-minutes"]');
  assert.strictEqual(again.querySelector('.adm-in').value, '30', 'the saved value shows after the repaint');
  assert.strictEqual(card(t.doc).querySelectorAll('.adm-row[data-key="roles.stale-max-minutes"]').length, 1);
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'Last known role (minutes) sits in the Sign-in card, not in General, and saves from there';
}

async function theLastKnownRoleEditSurvivesAnSsoCardSave() {
  //given
  const stale = settingItem('roles.stale-max-minutes', 'Sign-in', 'Last known role (minutes)', '60', {kind: 'int', low: 0, high: 1440});
  const t = await boot({items: [settingItem('access.session-hours', 'Access', 'Session length (h)', '12', {kind: 'int', low: 1, high: 720}), stale]});
  await openAccess(t);
  const row = () => card(t.doc).querySelector('.adm-row[data-key="roles.stale-max-minutes"]');

  //when
  setValue(row().querySelector('.adm-in'), '30');
  setValue(field(t.doc, 'issuer').querySelector('.sso-in'), 'https://idp2.example.com');
  field(t.doc, 'client-id').querySelector('.sso-reset').click();
  await wait(100);

  //then
  assert.deepStrictEqual(puts(t), [{values: {'client-id': null}}], 'the Reset saved only its field');
  assert.strictEqual(row().querySelector('.adm-in').value, '30', 'the typed value is still there');
  button(t.doc, 'sso-save').click();
  await wait(150);
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'PUT' && c[1] === '/api/admin/settings').map((c) => c[2]), [{key: 'roles.stale-max-minutes', value: '30'}], 'and the card Save sends it');
  assert.deepStrictEqual(puts(t)[1], {values: {issuer: 'https://idp2.example.com'}}, 'together with the kept SSO edit');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'an unsaved Last known role edit survives a repaint of the Single sign-on card, and the card Save sends it with the SSO edits';
}

const mcpItem = (key, label, value) => settingItem(key, 'MCP', label, value);
const typed = (doc, name) => doc.getElementById('sso-in-' + name);
const mapValues = (doc) => [...field(doc, 'role-map').querySelectorAll('.sso-map-row')].map((r) => [r.querySelector('.sso-map-v').value, r.querySelector('.sso-map-r').value]);

async function unsavedEditsSurviveAResetAndTheSwitch() {
  //given
  const t = await boot({items: [mcpItem('mcp.client-id', 'Client id', 'skopax-mcp'), mcpItem('mcp.audience', 'Audience', '')]});
  await openAccess(t);
  const audience = () => [...t.doc.querySelectorAll('#acc-mcp .adm-row')].filter((r) => r.dataset.key === 'mcp.audience')[0].querySelector('.adm-in');
  setValue(typed(t.doc, 'issuer'), 'https://new-idp.example.com');
  setValue(typed(t.doc, 'client-secret'), 'typed-secret');
  field(t.doc, 'role-map').querySelector('.sso-map-add').click();
  setValue([...field(t.doc, 'role-map').querySelectorAll('.sso-map-v')][2], 'ops-viewers');
  setValue(audience(), 'api://skopax');
  const kept = () => [typed(t.doc, 'issuer').value, typed(t.doc, 'client-secret').value, mapValues(t.doc), audience().value];
  const want = ['https://new-idp.example.com', 'typed-secret', [['ops-admins', 'admin'], ['ops-operators', 'operator'], ['ops-viewers', 'viewer']], 'api://skopax'];

  //when
  field(t.doc, 'client-id').querySelector('.sso-reset').click();
  await wait(80);
  const afterReset = kept();
  const resetSource = field(t.doc, 'client-id').querySelector('.sso-src').textContent;
  card(t.doc).querySelector('.sso-sw').click();
  await wait(80);
  const afterSwitch = kept();
  button(t.doc, 'sso-save').click();
  await wait(80);

  //then
  assert.deepStrictEqual(puts(t).slice(0, 2), [{values: {'client-id': null}}, {enabled: true}], 'Reset sends null for its field and the switch only the switch');
  assert.strictEqual(resetSource, 'default', 'the reset field shows its new source');
  assert.deepStrictEqual(afterReset, want, 'a Reset keeps typed fields, added role-map rows, the typed secret and MCP input');
  assert.deepStrictEqual(afterSwitch, want, 'so does the switch');
  assert.deepStrictEqual(puts(t)[2], {values: {issuer: 'https://new-idp.example.com', 'role-map': '[{"value":"ops-admins","role":"admin"},{"value":"ops-operators","role":"operator"},{"value":"ops-viewers","role":"viewer"}]',
    'client-secret': 'typed-secret'}}, 'and the kept edits are what Save sends');
  assert.strictEqual(typed(t.doc, 'client-secret').value, '', 'the saved secret is not put back');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'a per-field Reset sends {values: {name: null}}, and a Reset or the switch keeps unsaved fields, role-map rows, the typed secret and MCP input';
}

async function theSecretIsSentOnTestAndNeverLeftInThePage() {
  //given
  const t = await boot();
  await openAccess(t);
  const hidden = () => [...t.doc.querySelectorAll('input, textarea')].some((i) => i.value.indexOf('s3cret') >= 0) || /s3cret/.test(t.doc.body.textContent) ||
    /s3cret/.test(attrs(t.doc.documentElement).concat([...t.doc.querySelectorAll('*')].map((e) => [...e.attributes].map((a) => a.value).join(' ')).join(' ')));

  //when
  setValue(typed(t.doc, 'client-secret'), 's3cret-for-test');
  const present = hidden();
  button(t.doc, 'sso-test').click();
  await wait(80);
  t.doc.getElementById('tab-anoms').click();
  await wait(60);

  //then
  assert.ok(present, 'the check sees a typed secret');
  assert.deepStrictEqual(t.calls.filter((c) => c[1] === '/api/admin/sso/test').map((c) => c[2]), [{values: {'client-secret': 's3cret-for-test'}}], 'Test sends the typed secret');
  assert.strictEqual(typed(t.doc, 'client-secret').value, '', 'leaving the tab clears the typed secret');
  assert.ok(!hidden(), 'the secret is in no input value, text or attribute');

  //when
  t.doc.getElementById('tab-access').click();
  await wait(150);
  setValue(typed(t.doc, 'client-secret'), 's3cret-to-save');
  button(t.doc, 'sso-save').click();
  await wait(80);

  //then
  assert.strictEqual(typed(t.doc, 'client-secret').value, '');
  assert.ok(!hidden(), 'nor after a save');
  t.close();

  //given
  const o = await boot({sso: makeSso({secret: {set: true, from: 'environment', unreadable: false, updatedBy: null, updatedAt: null}})});
  await openAccess(o);
  typed(o.doc, 'client-secret').value = 'forced-value';
  setValue(typed(o.doc, 'scope'), 'openid email');

  //when
  button(o.doc, 'sso-test').click();
  await wait(80);
  button(o.doc, 'sso-save').click();
  await wait(80);

  //then
  assert.deepStrictEqual(o.calls.filter((c) => c[0] !== 'GET').map((c) => c[2]), [{values: {scope: 'openid email'}}, {values: {scope: 'openid email'}}], 'a read-only secret is never sent');
  assert.ok(!field(o.doc, 'client-secret').querySelector('.sso-secret-del'), 'and has no Remove');
  o.close();
  return 'a typed secret goes to Test, leaving the tab or saving clears it from the page, and a source-owned secret is never sent';
}

async function anAdminSecretCanBeRemovedAndAnUnreadableOneSaysSo() {
  //given
  const t = await boot({sso: makeSso({secret: {set: true, from: null, unreadable: false, updatedBy: 'alice@example.com', updatedAt: iso(5)}})});
  await openAccess(t);

  //when
  field(t.doc, 'client-secret').querySelector('.sso-secret-del').click();
  await wait(30);
  const asked = [t.doc.getElementById('cf-wrap').hidden, t.doc.getElementById('cf-title').textContent];
  t.doc.getElementById('cf-yes').click();
  await wait(80);

  //then
  assert.deepStrictEqual(asked, [false, 'Remove the client secret?']);
  assert.deepStrictEqual(puts(t), [{values: {'client-secret': ''}}], 'Remove sends an empty secret');
  assert.strictEqual(typed(t.doc, 'client-secret').placeholder, 'not set');
  assert.ok(!field(t.doc, 'client-secret').querySelector('.sso-secret-del'));
  assert.strictEqual(card(t.doc).querySelector('.sso-msg').textContent, 'Secret removed');
  t.close();

  //given
  const u = await boot({sso: makeSso({secret: {set: false, from: null, unreadable: true, updatedBy: null, updatedAt: null}, secretNote: 'The stored client secret cannot be read: enter it again.'})});

  //when
  await openAccess(u);

  //then
  const f = field(u.doc, 'client-secret');
  assert.deepStrictEqual([f.querySelector('.sso-src').textContent, typed(u.doc, 'client-secret').placeholder, f.querySelector('.sso-unreadable').textContent],
    ['unreadable', 'cannot be read', 'The stored client secret cannot be read: enter it again.']);
  u.close();
  return 'a secret set here has Remove, which sends an empty secret after the confirm, and an unreadable one shows its own note';
}

async function theSwitchShowsItsSourceAndResets() {
  //given
  const t = await boot({sso: makeSso({enabled: true, active: true, switch: {value: 'true', source: 'admin', from: null, base: 'environment', baseValue: false}})});
  await openAccess(t);
  const label = () => [card(t.doc).querySelector('.sso-sw-l .sso-src').textContent, (card(t.doc).querySelector('.sso-sw-l .sso-base') || {}).textContent];
  const before = label();
  setValue(typed(t.doc, 'issuer'), 'https://kept.example.com');

  //when
  card(t.doc).querySelector('.sso-sw-reset').click();
  await wait(20);
  t.doc.getElementById('cf-yes').click();
  await wait(100);

  //then
  assert.deepStrictEqual(before, ['set here', 'over environment']);
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'DELETE').map((c) => c[1]), ['/api/admin/settings?key=sso.enabled']);
  assert.deepStrictEqual(label(), ['from environment', undefined]);
  assert.ok(!card(t.doc).querySelector('.sso-sw-reset'), 'no Reset without an override');
  assert.strictEqual(card(t.doc).querySelector('.sso-sw').getAttribute('aria-checked'), 'false');
  assert.strictEqual(card(t.doc).querySelector('.sso-msg').textContent, 'Switch reset to the environment value');
  assert.strictEqual(typed(t.doc, 'issuer').value, 'https://kept.example.com', 'typed fields survive the switch reset');

  //when
  t.flags.hook = (method, p, body, h) => method === 'DELETE' ? h.bad(409, 'Switching single sign-on off needs an active local admin with a password.') : null;
  t.sso.now = Object.assign({}, t.sso.now, {enabled: true, active: true, switch: {value: 'true', source: 'admin', from: null, base: 'environment', baseValue: false}});
  t.doc.getElementById('tab-anoms').click();
  await wait(40);
  await openAccess(t);
  card(t.doc).querySelector('.sso-sw-reset').click();
  await wait(20);
  t.doc.getElementById('cf-yes').click();
  await wait(80);

  //then
  assert.strictEqual(card(t.doc).querySelector('.sso-msg').textContent, 'Switching single sign-on off needs an active local admin with a password.');
  assert.ok(!card(t.doc).querySelector('.sso-sw-reset').disabled);
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'the switch shows its source and the layer under an override, and its Reset drops the override or says why not';
}

async function roleMapValuesKeepTheirSpacesAndTheJsonCapIsRefused() {
  //given
  const values = Object.assign(makeSso().values, {'role-map': {value: '[{"value":" ops-admins","role":"admin"}]', source: 'admin', from: null}});
  const t = await boot({sso: makeSso({values})});
  await openAccess(t);

  //when
  setValue(typed(t.doc, 'scope'), 'openid email');
  button(t.doc, 'sso-save').click();
  await wait(80);

  //then
  assert.deepStrictEqual(puts(t), [{values: {scope: 'openid email'}}], 'a stored value with a space is not rewritten');

  //when
  setValue(field(t.doc, 'role-map').querySelector('.sso-map-v'), '  ops-leads  ');
  button(t.doc, 'sso-save').click();
  await wait(80);

  //then
  assert.deepStrictEqual(puts(t)[1], {values: {'role-map': '[{"value":"ops-leads","role":"admin"}]'}}, 'newly typed input is trimmed');
  t.close();

  //given
  const b = await boot();
  await openAccess(b);
  const map = field(b.doc, 'role-map');
  for (let i = 0; i < 20; i++) map.querySelector('.sso-map-add').click();
  [...map.querySelectorAll('.sso-map-v')].slice(2).forEach((inp, i) => setValue(inp, String(i).padStart(3, '0') + 'g'.repeat(190)));

  //when
  button(b.doc, 'sso-save').click();
  await wait(80);

  //then
  assert.strictEqual(map.querySelectorAll('.sso-map-row').length, 22);
  assert.deepStrictEqual(b.calls.filter((c) => c[0] !== 'GET'), [], 'nothing is sent');
  assert.ok(map.classList.contains('bad'));
  assert.strictEqual(map.querySelector('.sso-fmsg').textContent, 'The role map is at most 4000 characters as JSON; shorten or remove rows.');
  b.close();
  return 'stored role-map values are compared and sent untrimmed, new input is trimmed, and a map over 4000 characters is refused before sending';
}

async function theMcpCardAlwaysShowsItsNote() {
  //given
  const t = await boot();
  await openAccess(t);
  const d = await boot({settingsDown: true});
  await openAccess(d);

  //then
  assert.ok(/needs single sign-on set up and on/.test(t.doc.querySelector('#acc-mcp .acc-mcp-note').textContent), 'with no MCP settings the note still shows');
  assert.ok(!t.doc.querySelector('#acc-mcp .acc-mcp-failed'));
  assert.ok(/needs single sign-on set up and on/.test(d.doc.querySelector('#acc-mcp .acc-mcp-note').textContent));
  assert.strictEqual(d.doc.querySelector('#acc-mcp .acc-mcp-failed').textContent, 'MCP settings did not load; open the tab again.');
  assert.ok(card(d.doc), 'the SSO card still renders');
  assert.deepStrictEqual([t.errors, d.errors], [[], []]);
  t.close();
  d.close();
  return 'the MCP card always shows its needs-SSO note, and says when its settings did not load';
}

async function anOverrideShowsTheLayerUnderIt() {
  //given
  const sso = makeSso();
  sso.bases['client-id'] = EVIL;
  const t = await boot({sso: makeSso({bases: Object.assign({}, sso.bases, {'client-id': 'environment'})})});

  //when
  await openAccess(t);

  //then
  const f = field(t.doc, 'client-id');
  assert.deepStrictEqual([f.querySelector('.sso-src').textContent, f.querySelector('.sso-base').textContent], ['set here', 'over environment']);
  assert.strictEqual(f.querySelector('.sso-reset').title, 'Drop the value set here and go back to environment');
  assert.ok(!field(t.doc, 'issuer').querySelector('.sso-base'), 'a value that is not overridden has no base label');
  t.close();

  //given
  const x = await boot({sso: makeSso({redirectUri: EVIL, bases: sso.bases, secret: {set: true, from: EVIL, unreadable: false, updatedBy: null, updatedAt: null}})});

  //when
  await openAccess(x);

  //then
  const s = field(x.doc, 'client-secret');
  assert.deepStrictEqual([s.querySelector('.sso-src').textContent, typed(x.doc, 'client-secret').placeholder, typed(x.doc, 'client-secret').title],
    ['from ' + EVIL, 'set \u00b7 from ' + EVIL, 'Set in ' + EVIL + '; change it there']);
  assert.strictEqual(field(x.doc, 'client-id').querySelector('.sso-base').textContent, 'over ' + EVIL);
  assert.strictEqual(card(x.doc).querySelector('.sso-redirect code').textContent, EVIL);
  assert.strictEqual(x.doc.getElementById('access').querySelectorAll('img').length, 0);
  assert.ok(!/onerror/.test(attrs(x.doc.getElementById('access'))));
  assert.deepStrictEqual(x.errors, []);
  x.close();
  return 'an Admin override shows the layer it sits on, and hostile text in that label, the secret source and the redirect URI renders as text';
}

const leaked = (doc, text) => [...doc.querySelectorAll('input, textarea')].some((i) => i.value.indexOf(text) >= 0) || doc.body.textContent.indexOf(text) >= 0 ||
  [...doc.querySelectorAll('*')].some((e) => [...e.attributes].some((a) => a.value.indexOf(text) >= 0));
const slowly = (match) => async (method, p, body) => { if (match(method, p)) await wait(150); return null; };

async function aSecretClearedByLeavingTheTabStaysClearedWhenTheRequestEnds() {
  //given
  const t = await boot();
  await openAccess(t);
  t.flags.hook = slowly((method, p) => method === 'PUT' && p === '/api/admin/sso');
  setValue(typed(t.doc, 'client-secret'), 'mid-flight-secret');
  const seen = leaked(t.doc, 'mid-flight-secret');

  //when
  field(t.doc, 'client-id').querySelector('.sso-reset').click();
  await wait(10);
  t.doc.getElementById('tab-anoms').click();
  await wait(300);

  //then
  assert.ok(seen, 'the check sees a typed secret');
  assert.deepStrictEqual(puts(t), [{values: {'client-id': null}}]);
  assert.ok(t.doc.getElementById('panel-access').hidden, 'Access is not the tab on screen');
  assert.strictEqual(typed(t.doc, 'client-secret').value, '', 'the secret is not written back into the hidden panel');
  assert.ok(!leaked(t.doc, 'mid-flight-secret'), 'and is nowhere in the page');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'a typed secret cleared by leaving the tab stays cleared when a Reset that was in flight comes back';
}

async function editsTypedWhileARequestRunsSurviveIt() {
  //given
  const t = await boot({sso: makeSso({enabled: true, active: true, switch: {value: 'true', source: 'admin', from: null, base: 'environment', baseValue: false}})});
  await openAccess(t);
  t.flags.hook = slowly((method, p) => (method === 'PUT' && p === '/api/admin/sso') || method === 'DELETE');

  //when
  field(t.doc, 'client-id').querySelector('.sso-reset').click();
  await wait(10);
  setValue(typed(t.doc, 'issuer'), 'https://during-reset.example.com');
  setValue(typed(t.doc, 'client-secret'), 'during-reset-secret');
  await wait(300);
  const afterReset = [typed(t.doc, 'issuer').value, typed(t.doc, 'client-secret').value, field(t.doc, 'client-id').querySelector('.sso-src').textContent];
  card(t.doc).querySelector('.sso-sw-reset').click();
  await wait(10);
  t.doc.getElementById('cf-yes').click();
  await wait(10);
  setValue(typed(t.doc, 'scope'), 'openid during');
  await wait(300);
  const afterSwitch = [typed(t.doc, 'issuer').value, typed(t.doc, 'scope').value, card(t.doc).querySelector('.sso-sw-l .sso-src').textContent];
  card(t.doc).querySelector('.sso-on').click();
  await wait(10);
  setValue(typed(t.doc, 'roles-claim'), 'groups');
  setValue(typed(t.doc, 'issuer'), 'https://late.example.com');
  await wait(300);
  const afterSave = [typed(t.doc, 'roles-claim').value, typed(t.doc, 'issuer').value, typed(t.doc, 'client-secret').value];
  const firstMap = () => field(t.doc, 'role-map').querySelector('.sso-map-v');
  setValue(firstMap(), 'ops-sent');
  setValue(typed(t.doc, 'client-secret'), 'sent-secret');
  button(t.doc, 'sso-save').click();
  await wait(10);
  setValue(firstMap(), 'ops-late');
  setValue(typed(t.doc, 'client-secret'), 'late-secret');
  await wait(300);

  //then
  assert.deepStrictEqual(afterReset, ['https://during-reset.example.com', 'during-reset-secret', 'default'], 'edits typed during a Reset survive it');
  assert.deepStrictEqual(afterSwitch, ['https://during-reset.example.com', 'openid during', 'from environment'], 'and during the switch Reset');
  assert.deepStrictEqual(puts(t)[1], {values: {issuer: 'https://during-reset.example.com', scope: 'openid during', 'client-secret': 'during-reset-secret'}, enabled: true});
  assert.deepStrictEqual(afterSave, ['groups', 'https://late.example.com', ''], 'and during a Save, an edit to the very field being sent included, while the saved secret is not put back');
  assert.deepStrictEqual(puts(t)[2], {values: {issuer: 'https://late.example.com', 'roles-claim': 'groups', 'role-map': '[{"value":"ops-sent","role":"admin"},{"value":"ops-operators","role":"operator"}]', 'client-secret': 'sent-secret'}});
  assert.deepStrictEqual([firstMap().value, typed(t.doc, 'client-secret').value], ['ops-late', 'late-secret'], 'a role map and a secret typed again during their own Save survive it');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'edits typed while a Reset, the switch Reset or a Save is in flight survive the rebuild, an edit to the field being sent included';
}

async function aSwitchResetWhoseReloadFailsSaysItWasReset() {
  //given
  const t = await boot({sso: makeSso({enabled: true, active: true, switch: {value: 'true', source: 'admin', from: null, base: 'environment', baseValue: false}})});
  await openAccess(t);
  let dropped = false;
  t.flags.hook = (method, p, body, h) => {
    if (method === 'DELETE') { dropped = true; return null; }
    return dropped && method === 'GET' && p === '/api/admin/sso' ? h.bad(503, 'Database unavailable') : null;
  };

  //when
  card(t.doc).querySelector('.sso-sw-reset').click();
  await wait(20);
  t.doc.getElementById('cf-yes').click();
  await wait(100);

  //then
  assert.ok(dropped);
  assert.strictEqual(card(t.doc).querySelector('.sso-msg').textContent, 'Switch reset; open the tab again to see it.');
  assert.ok(!card(t.doc).querySelector('.sso-msg').classList.contains('bad'));
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'a switch Reset that worked but whose reload failed says it was reset and to open the tab again';
}

async function hostileSwitchLabelsAndSecretNoteRenderAsText() {
  //given
  const over = await boot({sso: makeSso({enabled: true, active: true, switch: {value: 'true', source: 'admin', from: null, base: EVIL}})});
  const from = await boot({sso: makeSso({switch: {value: 'false', source: 'config', from: EVIL, base: EVIL}, secret: {set: false, from: null, unreadable: true, updatedBy: null, updatedAt: null}, secretNote: EVIL})});

  //when
  await openAccess(over);
  await openAccess(from);

  //then
  assert.strictEqual(card(over.doc).querySelector('.sso-sw-l .sso-base').textContent, 'over ' + EVIL);
  assert.strictEqual(card(over.doc).querySelector('.sso-sw-reset').title, 'Drop the switch set here and go back to ' + EVIL);
  assert.strictEqual(card(from.doc).querySelector('.sso-sw-l .sso-src').textContent, 'from ' + EVIL);
  assert.strictEqual(field(from.doc, 'client-secret').querySelector('.sso-unreadable').textContent, EVIL);
  for (const x of [over, from]) {
    assert.strictEqual(x.doc.getElementById('access').querySelectorAll('img').length, 0);
    assert.ok(!/onerror/.test(attrs(x.doc.getElementById('access'))));
    assert.deepStrictEqual(x.errors, []);
    x.close();
  }
  return 'hostile text in the switch source, the layer under it and the secret note renders as text';
}

const unsetValues = () => Object.assign(makeSso().values, {issuer: {value: '', source: 'default', from: null}, 'client-id': {value: 'skopax', source: 'default', from: null},
  'role-map': {value: '', source: 'default', from: null}});
const STOP_TEXT = ['Single sign-on sessions end within 30 seconds and stay ended after switching back on: people sign in again.', 'MCP tokens issued before stay refused',
  'API tokens made from single sign-on sessions are refused and stay refused after switching back on: make new ones afterwards.', 'Password logins go on.'];

async function nothingConfiguredReadsNotSetUpWithNoProblemLine() {
  //given
  const shown = async (over) => {
    const b = await boot({sso: makeSso(over)});
    await openAccess(b);
    const pill = card(b.doc).querySelector('.sso-pill'), problem = card(b.doc).querySelector('.sso-problem');
    const out = [pill.textContent, pill.classList.contains('k-warn'), problem ? problem.textContent : null, b.errors];
    b.close();
    return out;
  };
  const problem = 'Set the issuer URL (sso.issuer).';

  //when
  const blankOn = await shown({enabled: true, active: false, problem, values: unsetValues()});
  const blankOff = await shown({enabled: false, active: false, problem, values: unsetValues()});
  const withSecret = await shown({enabled: true, active: false, problem, values: unsetValues(), secret: {set: true, from: 'environment', unreadable: false, updatedBy: null, updatedAt: null}});
  const withClient = await shown({enabled: true, active: false, problem, values: Object.assign(unsetValues(), {'client-id': {value: 'other-app', source: 'admin', from: null}})});
  const withIssuer = await shown({enabled: true, active: false, problem: 'Set the client secret (sso.client-secret).', values: Object.assign(unsetValues(), {issuer: {value: 'https://idp.example.com', source: 'config', from: 'environment'}})});

  //then
  assert.deepStrictEqual(blankOn, ['Not set up', false, null, []], 'with nothing configured the pill is a neutral Not set up and no problem line shows');
  assert.deepStrictEqual(blankOff, ['Not set up', false, null, []], 'with the switch off too');
  assert.deepStrictEqual(withSecret, ['Needs attention', true, problem, []], 'a secret counts as configured');
  assert.deepStrictEqual(withClient, ['Needs attention', true, problem, []], 'so does a client ID other than the default');
  assert.deepStrictEqual(withIssuer, ['Needs attention', true, 'Set the client secret (sso.client-secret).', []], 'and an issuer');
  return 'with no issuer, no client ID other than the default and no secret the pill reads Not set up with no problem line; anything configured brings back Needs attention and the problem';
}

async function theProviderLineSaysWhatWentWrongInPlainWords() {
  //given
  const line = async (error) => {
    const b = await boot({sso: makeSso({enabled: true, active: true, provider: {ok: false, since: SINCE, error}})});
    await openAccess(b);
    const text = card(b.doc).querySelector('.sso-provider').textContent;
    b.close();
    return text;
  };

  //when
  const said = [await line('discovery'), await line('issuer'), await line('unreachable')];

  //then
  assert.deepStrictEqual(said, ['Identity provider did not answer with its settings since ' + short(SINCE), 'Identity provider names a different issuer since ' + short(SINCE),
    'Identity provider not reachable since ' + short(SINCE)], 'each reason code reads as plain text, never as the raw code');
  return 'the provider line turns the discovery, issuer and unreachable codes into plain text';
}

async function anOverCapStoredMapStillLetsOtherFieldsSaveAndTest() {
  //given
  const big = JSON.stringify(Array.from({length: 40}, (_, i) => ({value: 'cn=ops-group-' + String(i).padStart(2, '0') + ',ou=' + 'g'.repeat(90), role: 'viewer'})));
  const values = Object.assign(makeSso().values, {'role-map': {value: big, source: 'config', from: 'environment'}});
  const t = await boot({sso: makeSso({values})});
  await openAccess(t);
  const map = field(t.doc, 'role-map');

  //when
  setValue(typed(t.doc, 'scope'), 'openid email');
  button(t.doc, 'sso-test').click();
  await wait(80);
  button(t.doc, 'sso-save').click();
  await wait(80);
  const sent = t.calls.filter((c) => c[0] !== 'GET');
  const marked = map.classList.contains('bad');
  setValue(field(t.doc, 'role-map').querySelector('.sso-map-v'), 'cn=ops-group-00');
  button(t.doc, 'sso-save').click();
  await wait(80);

  //then
  assert.ok(big.length > 4000, 'the stored map is over the cap: ' + big.length);
  assert.deepStrictEqual(sent, [['POST', '/api/admin/sso/test', {values: {scope: 'openid email'}}], ['PUT', '/api/admin/sso', {values: {scope: 'openid email'}}]], 'Test and a Scope-only Save both send');
  assert.ok(!marked, 'the unchanged map is not marked');
  assert.strictEqual(t.calls.filter((c) => c[0] !== 'GET').length, 2, 'a changed map over the cap is still refused before sending');
  assert.strictEqual(field(t.doc, 'role-map').querySelector('.sso-fmsg').textContent, 'The role map is at most 4000 characters as JSON; shorten or remove rows.');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'a stored role map over 4000 characters lets a Scope-only Save and Test send, and the cap still refuses a changed map';
}

async function theRolePairsInUseShowWhileNoMapIsSaved() {
  //given
  const values = Object.assign(makeSso().values, {'role-map': {value: '', source: 'default', from: null}});
  const t = await boot({sso: makeSso({values, roleMapInUse: [{value: 'SKOPAX_ADMIN', role: 'admin'}, {value: EVIL, role: 'operator'}]})});

  //when
  await openAccess(t);

  //then
  const f = field(t.doc, 'role-map');
  const rows = [...f.querySelectorAll('.sso-inuse-row')].map((r) => [r.querySelector('.sso-inuse-v').textContent, r.querySelector('.sso-inuse-r').textContent, r.querySelector('.sso-inuse-tag').textContent]);
  assert.deepStrictEqual(rows, [['SKOPAX_ADMIN', 'admin', 'in use now'], [EVIL, 'operator', 'in use now']], 'the pairs in force show as in use now');
  assert.strictEqual(f.querySelectorAll('.sso-map-row').length, 0, 'they are not editable rows');
  assert.strictEqual(f.querySelectorAll('.sso-inuse input, .sso-inuse select, .sso-inuse button').length, 0, 'and are read-only');
  assert.strictEqual(f.querySelector('.adm-help').textContent, 'Claim values matched exactly; several matches give the highest role. No role map is saved, so SKOPAX_ADMIN \u2192 admin and ' + EVIL +
    ' \u2192 operator are in use (from SKOPAX_ADMIN_ROLE and SKOPAX_OPERATOR_ROLE). Adding a row replaces them; add them as rows to keep them.');
  assert.strictEqual(t.doc.getElementById('access').querySelectorAll('img').length, 0);
  assert.ok(!/onerror/.test(attrs(t.doc.getElementById('access'))));

  //when
  setValue(typed(t.doc, 'scope'), 'openid email');
  button(t.doc, 'sso-save').click();
  await wait(80);
  field(t.doc, 'role-map').querySelector('.sso-map-add').click();
  setValue(field(t.doc, 'role-map').querySelector('.sso-map-v'), 'ops-viewers');
  const inUseNow = () => [...field(t.doc, 'role-map').querySelectorAll('.sso-inuse .sso-inuse-row')].length;
  const added = [field(t.doc, 'role-map').querySelectorAll('.sso-inuse .sso-map-row').length, inUseNow()];
  field(t.doc, 'client-id').querySelector('.sso-reset').click();
  await wait(80);
  const repainted = [inUseNow(), field(t.doc, 'role-map').querySelectorAll('.sso-inuse .sso-map-row').length, mapValues(t.doc)];
  button(t.doc, 'sso-save').click();
  await wait(80);

  //then
  assert.deepStrictEqual(added, [0, 2], 'an added row goes to the editable rows, never into the rows in use');
  assert.deepStrictEqual(repainted, [2, 0, [['ops-viewers', 'viewer']]], 'a repaint keeps the rows in use and puts the unsaved row back among the editable rows');
  assert.deepStrictEqual(puts(t), [{values: {scope: 'openid email'}}, {values: {'client-id': null}}, {values: {'role-map': '[{"value":"ops-viewers","role":"viewer"}]'}}], 'the rows in use are never sent; an added row is the whole map');
  assert.deepStrictEqual(t.errors, []);
  t.close();

  //given
  const s = await boot();

  //when
  await openAccess(s);

  //then
  assert.ok(!field(s.doc, 'role-map').querySelector('.sso-inuse'), 'with a map in force nothing reads in use now');
  assert.strictEqual(field(s.doc, 'role-map').querySelector('.adm-help').textContent, 'Claim values matched exactly; several matches give the highest role.');
  s.close();

  //given
  const single = async (pair) => {
    const one = await boot({sso: makeSso({values, roleMapInUse: [pair]})});
    await openAccess(one);
    const text = field(one.doc, 'role-map').querySelector('.adm-help').textContent;
    one.close();
    return text;
  };

  //when
  const adminOnly = await single({value: 'ops-admins', role: 'admin'});
  const operatorOnly = await single({value: 'ops-operators', role: 'operator'});

  //then
  assert.strictEqual(adminOnly, 'Claim values matched exactly; several matches give the highest role. No role map is saved, so ops-admins \u2192 admin is in use (from SKOPAX_ADMIN_ROLE).' +
    ' Adding a row replaces it; add it as a row to keep it.', 'one pair reads in the singular and names only its own variable');
  assert.strictEqual(operatorOnly, 'Claim values matched exactly; several matches give the highest role. No role map is saved, so ops-operators \u2192 operator is in use (from SKOPAX_OPERATOR_ROLE).' +
    ' Adding a row replaces it; add it as a row to keep it.');
  return 'while no role map is saved the pairs in force show read-only as in use now, the help names the variables they come from, in the singular for one pair, and says a row replaces them';
}

async function switchingOffAndAResetThatTurnsItOffAskFirst() {
  //given
  const t = await boot({sso: makeSso({enabled: true, active: true, switch: {value: 'true', source: 'admin', from: null, base: 'environment', baseValue: false}})});
  await openAccess(t);
  const reset = () => card(t.doc).querySelector('.sso-sw-reset');
  const asked = () => [t.doc.getElementById('cf-wrap').hidden, t.doc.getElementById('cf-title').textContent, t.doc.getElementById('cf-text').textContent, t.doc.getElementById('cf-yes').textContent];
  const sent = () => t.calls.filter((c) => c[0] !== 'GET');

  //when
  const label = reset().textContent;
  reset().click();
  await wait(20);
  const resetAsk = asked();
  t.doc.getElementById('cf-no').click();
  await wait(40);
  const afterNo = sent().length;
  card(t.doc).querySelector('.sso-sw').click();
  await wait(20);
  const offAsk = asked();
  t.doc.getElementById('cf-no').click();
  await wait(40);
  const afterSwitchNo = sent().length;
  card(t.doc).querySelector('.sso-sw').click();
  await wait(20);
  t.doc.getElementById('cf-yes').click();
  await wait(80);

  //then
  assert.strictEqual(label, 'Reset (turns SSO off)');
  assert.deepStrictEqual(resetAsk.slice(0, 2), [false, 'Reset and turn single sign-on off?']);
  assert.strictEqual(resetAsk[3], 'Reset and turn off');
  assert.deepStrictEqual(offAsk.slice(0, 2), [false, 'Turn single sign-on off?']);
  assert.strictEqual(offAsk[3], 'Turn off');
  for (const text of [resetAsk[2], offAsk[2]]) STOP_TEXT.forEach((part) => assert.ok(text.indexOf(part) >= 0, part + ' in: ' + text));
  assert.ok(!/keep working/.test(resetAsk[2] + offAsk[2]), 'no promise that API tokens keep working');
  assert.deepStrictEqual([afterNo, afterSwitchNo], [0, 0], 'Cancel sends nothing');
  assert.deepStrictEqual(sent().map((c) => [c[0], c[1], c[2]]), [['PUT', '/api/admin/sso', {enabled: false}]], 'the confirmed switch-off is sent');
  assert.strictEqual(card(t.doc).querySelector('.sso-msg').textContent, 'Turned off');
  assert.deepStrictEqual(t.errors, []);
  t.close();

  //given
  const k = await boot({sso: makeSso({enabled: true, active: true, switch: {value: 'true', source: 'admin', from: null, base: 'environment', baseValue: true}})});
  await openAccess(k);
  const plain = card(k.doc).querySelector('.sso-sw-reset').textContent;

  //when
  card(k.doc).querySelector('.sso-sw-reset').click();
  await wait(100);

  //then
  assert.strictEqual(plain, 'Reset');
  assert.ok(k.doc.getElementById('cf-wrap').hidden, 'a Reset that keeps SSO on does not ask');
  assert.deepStrictEqual(k.calls.filter((c) => c[0] === 'DELETE').map((c) => c[1]), ['/api/admin/settings?key=sso.enabled']);
  k.close();

  //given
  const o = await boot();
  await openAccess(o);

  //when
  card(o.doc).querySelector('.sso-sw').click();
  await wait(80);

  //then
  assert.ok(o.doc.getElementById('cf-wrap').hidden, 'switching on does not ask');
  assert.deepStrictEqual(puts(o), [{enabled: true}]);
  o.close();
  return 'switching off and a Reset that turns SSO off ask first, naming what ends and stays ended, Cancel sends nothing, and switching on or a Reset that keeps it on do not ask';
}

async function aSecretPutIntoTheHiddenTabIsNotWrittenBack() {
  //given
  const t = await boot();
  await openAccess(t);
  t.flags.hook = slowly((method, p) => method === 'PUT' && p === '/api/admin/sso');

  //when
  field(t.doc, 'client-id').querySelector('.sso-reset').click();
  await wait(10);
  t.doc.getElementById('tab-anoms').click();
  await wait(20);
  typed(t.doc, 'client-secret').value = 'autofilled-secret';
  await wait(300);

  //then
  assert.ok(t.doc.getElementById('panel-access').hidden, 'Access is not the tab on screen');
  assert.strictEqual(typed(t.doc, 'client-secret').value, '', 'a secret in the input of the hidden tab is not written back into the rebuilt card');
  assert.ok(!leaked(t.doc, 'autofilled-secret'));
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'a request that ends while Access is not on screen never writes a secret back, even one put into the input after leaving the tab';
}

async function theCardStylesItsOverrideLabelFoldCheckboxAndRows() {
  //given
  const t = await boot({sso: makeSso({enabled: true, active: true, switch: {value: 'true', source: 'admin', from: null, base: 'environment', baseValue: true}})});

  //when
  await openAccess(t);

  //then
  const css = (el) => t.w.getComputedStyle(el);
  const base = card(t.doc).querySelector('.sso-sw-l .sso-base'), tag = card(t.doc).querySelector('.sso-sw-l .sso-src');
  assert.strictEqual(tag.title, base.textContent, 'the override is the tooltip of the one source tag');
  assert.strictEqual(css(base).clipPath, 'inset(50%)', 'and stays for screen readers without a second label beside the tag');
  assert.ok(card(t.doc).querySelector('.sso-adv > summary').classList.contains('chev'), 'Advanced: endpoints has a fold marker');
  assert.strictEqual(css(field(t.doc, 'require-verified-email').querySelector('input[type=checkbox]')).justifySelf, 'start', 'the checkbox sits under its label');
  const sheet = [...t.doc.querySelector('style').sheet.cssRules].filter((r) => r.selectorText === '.sso-map-t, .sso-inuse').map((r) => r.style.width);
  assert.deepStrictEqual(sheet, ['auto'], 'role-map rows keep value, role and Remove together, not stretched like a full-width table');
  const counts = [...t.doc.querySelectorAll('#access .acc-sec h3 .count')];
  assert.strictEqual(counts.length, 2);
  counts.forEach((c) => assert.ok(parseFloat(css(c).marginLeft) > 0, 'the count keeps a gap from its heading'));
  assert.deepStrictEqual(counts.map((c) => c.parentNode.textContent), ['People1', 'API tokens0'], 'the gap is styling, not text');
  t.close();
  return 'the override label is styled, Advanced: endpoints has a chevron, the checkbox and role-map rows line up, and the People and API tokens counts stand apart';
}

function unsetSso(over) {
  const d = (value) => ({value, source: 'default', from: null});
  const base = makeSso();
  return makeSso(Object.assign({values: Object.assign({}, base.values, {issuer: d(''), 'client-id': d(''), 'role-map': d('')})}, over || {}));
}

async function theStatusHeaderShowsStateHostIconAndSwitch() {
  //given
  const on = await boot({sso: makeSso({enabled: true, active: true})});
  const unset = await boot({sso: unsetSso()});

  //when
  await openAccess(on);
  await openAccess(unset);

  //then
  const head = (doc) => card(doc).querySelector('.sso-top');
  const sw = head(on.doc).querySelector('.sso-sw');
  assert.deepStrictEqual([sw.getAttribute('role'), sw.getAttribute('aria-checked'), sw.getAttribute('aria-label')], ['switch', 'true', 'Single sign-on']);
  assert.strictEqual(head(on.doc).querySelector('.sso-pill').textContent, 'On');
  assert.ok(head(on.doc).querySelector('.sso-status').textContent.indexOf('idp.example.com') >= 0, 'the provider host shows in the header');
  const ico = head(on.doc).querySelector('.sso-at-ico');
  assert.ok(!ico.hidden && ico.style.backgroundImage.indexOf('/icons/sso/key.svg') >= 0, 'the provider icon shows beside the host');
  assert.strictEqual(card(on.doc).dataset.state, 'k-ok');
  assert.ok(!card(on.doc).querySelector('.sso-intro'), 'a set-up provider shows no intro');
  assert.deepStrictEqual([...card(on.doc).querySelectorAll('.sso-grp')].map((g) => g.dataset.grp), ['provider', 'redirect', 'who', 'roles', 'button']);
  assert.strictEqual(head(unset.doc).querySelector('.sso-pill').textContent, 'Not set up');
  assert.ok(head(unset.doc).querySelector('.sso-at-ico').hidden, 'no icon while no issuer is set');
  assert.strictEqual(card(unset.doc).querySelectorAll('.sso-intro li').length, 3, 'not set up shows a three-step intro');
  assert.deepStrictEqual([...card(unset.doc).querySelectorAll('.sso-grp')].map((g) => [g.dataset.grp, g.querySelector('.sso-num').textContent]),
    [['redirect', '1'], ['provider', '2'], ['who', '3'], ['roles', '4'], ['button', '5']], 'the redirect URI to copy comes first, then the provider');
  assert.strictEqual(card(unset.doc).querySelectorAll('.sso-redirect').length, 1, 'the redirect URI shows once');
  assert.strictEqual(card(on.doc).querySelector('.sso-result').getAttribute('aria-live'), 'polite');
  assert.strictEqual(card(on.doc).querySelector('.sso-msg').getAttribute('role'), 'status');
  assert.deepStrictEqual(on.errors.concat(unset.errors), []);
  on.close();
  unset.close();
  return 'the status header shows the state pill, the provider host with its icon and the switch; not set up starts with a three-step intro and the redirect URI';
}

async function theCopyButtonSelectsTheAddressWhenTheClipboardIsRefused() {
  //given
  const t = await boot();
  await openAccess(t);
  Object.defineProperty(t.w.navigator, 'clipboard', {configurable: true, value: {writeText: async () => { throw new Error('denied'); }}});
  const box = card(t.doc).querySelector('.sso-redirect');

  //when
  box.querySelector('.sso-copy').click();
  await wait(30);

  //then
  assert.strictEqual(box.querySelector('.adm-msg').textContent, 'Selected: press Ctrl+C or Cmd+C to copy');
  assert.strictEqual(String(t.w.getSelection()), 'https://skopax.example.com/authorized');
  const uri = box.querySelector('code');
  assert.deepStrictEqual([uri.getAttribute('role'), uri.getAttribute('aria-readonly'), uri.getAttribute('aria-labelledby')], ['textbox', 'true', 'sso-uri-l'], 'the address is a labelled read-only field');
  assert.strictEqual(t.doc.getElementById('sso-uri-l').textContent, 'Redirect URI to register');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'Copy falls back to selecting the labelled redirect URI when the clipboard is refused';
}

async function theSaveBarSticksOnlyWhileThereAreUnsavedChanges() {
  //given
  const t = await boot();
  await openAccess(t);
  const bar = () => card(t.doc).querySelector('.sso-bar');
  const state = () => [bar().dataset.dirty, bar().querySelector('.sso-dirty-l').hidden];
  const before = state();
  const client = field(t.doc, 'client-id').querySelector('.sso-in');

  //when
  setValue(client, 'skopax-prod');
  const typed = state();
  setValue(client, 'skopax');
  const back = state();
  button(t.doc, 'sso-map-add').click();
  const added = state();
  button(t.doc, 'sso-discard').click();
  await wait(30);
  const discarded = state();
  t.doc.getElementById('sso-chip-in').value = 'example.org';
  t.doc.getElementById('sso-chip-in').dispatchEvent(new t.w.Event('input', {bubbles: true}));
  const pending = state();

  //then
  assert.deepStrictEqual([before, typed, back, added, discarded, pending], [['false', true], ['true', false], ['false', true], ['true', false], ['false', true], ['true', false]]);
  assert.ok(bar().querySelector('.sso-save') && bar().querySelector('.sso-msg'), 'Save and the message live in the bar');
  const sticky = [...t.doc.querySelector('style').sheet.cssRules].filter((r) => r.selectorText === '.sso-bar[data-dirty="true"]').map((r) => [r.style.position, r.style.bottom]);
  assert.deepStrictEqual(sticky, [['sticky', '12px']], 'the bar sticks to the bottom only while there are unsaved changes');
  assert.ok(bar().parentNode === card(t.doc), 'the bar is a direct child of the section, so no card clips it');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'the save bar marks Unsaved changes and sticks while a field, a role-map row or a typed domain differs, and clears when the edit is undone or discarded';
}

async function theScopeIsAChipListThatSavesWithSpaces() {
  //given
  const t = await boot();
  await openAccess(t);
  const scope = field(t.doc, 'scope');
  const chips = () => [...scope.querySelectorAll('.adm-chip-t')].map((c) => c.textContent);

  //then
  assert.deepStrictEqual(chips(), ['openid', 'email', 'profile']);
  assert.ok(scope.querySelector('.sso-in').hidden, 'the value box is hidden behind the chips');

  //when
  const add = scope.querySelector('.adm-chip-in');
  setValue(add, 'groups ');
  await wait(20);
  scope.querySelector('.adm-chip-x').click();
  button(t.doc, 'sso-save').click();
  await wait(80);

  //then
  assert.deepStrictEqual(puts(t), [{values: {scope: 'email profile groups'}}], 'the saved text is the same space separated scope');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'the scope is a chip list that saves the same space separated text';
}

async function theScopeChipsFollowTheValueAfterARepaintAndAnInvalidChipBlocksTheSaves() {
  //given
  const longId = 'x'.repeat(201);
  const t = await boot({items: [settingItem('access.session-hours', 'Access', 'Session length (h)', '12', {kind: 'int', low: 1, high: 720}),
    settingItem('mcp.allowed-clients', 'MCP', 'MCP allowed clients', '', {kind: 'list', items: {kind: 'client-id', separator: ','}})]});
  await openAccess(t);
  const scope = field(t.doc, 'scope');
  const chips = () => [...field(t.doc, 'scope').querySelectorAll('.adm-chip-t')].map((c) => c.textContent);

  //when
  scope.querySelector('.adm-chip-x').click();
  t.w.ssoRepaint('', t.w.ssoDraft(), []);

  //then
  assert.deepStrictEqual(chips(), ['email', 'profile']);
  assert.strictEqual(field(t.doc, 'scope').querySelector('.sso-in').value, 'email profile', 'the chips equal the hidden value after the repaint');

  //when
  setValue(field(t.doc, 'scope').querySelector('.adm-chip-in'), 'caf\u00e9 ');
  const save = () => card(t.doc).querySelector('.sso-save');

  //then
  assert.ok(field(t.doc, 'scope').querySelector('.adm-chip.bad'), 'an invalid scope is red');
  assert.ok(save().disabled, 'the card Save waits for the fix');
  assert.ok(!button(t.doc, 'sso-on') || button(t.doc, 'sso-on').disabled, 'and so does Save & turn on');
  assert.strictEqual(puts(t).length, 0);

  //when
  t.w.ssoRepaint('', t.w.ssoDraft(), []);

  //then
  assert.ok(card(t.doc).querySelector('.sso-save').disabled, 'a repaint keeps it blocked');

  //when
  const mcp = t.doc.querySelector('#acc-mcp .adm-row[data-key="mcp.allowed-clients"]');
  setValue(mcp.querySelector('.adm-chip-in'), longId + ',');

  //then
  assert.ok(mcp.querySelector('.adm-chip.bad'), 'a client id over 200 characters is red');
  assert.ok(t.doc.querySelector('#acc-mcp .acc-mcp-save').disabled, 'the MCP card Save is blocked too');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'scope chips follow the value after a repaint, and an invalid chip blocks the SSO and MCP card saves';
}

async function thePhoneLayoutIsOneColumnWithLargeTargets() {
  //given
  const t = await boot();
  await openAccess(t);

  //then
  const phone = [...t.doc.querySelector('style').sheet.cssRules].filter((r) => r.media && r.media.mediaText === '(max-width: 640px)' && [...r.cssRules].some((x) => /sso-btn-grid/.test(x.selectorText)));
  assert.strictEqual(phone.length, 1, 'the SSO phone rules sit in their own block');
  const rule = (sel) => [...phone[0].cssRules].filter((x) => x.selectorText === sel)[0];
  assert.strictEqual(rule('.sso-btn-grid').style.gridTemplateColumns, 'minmax(0px, 1fr)', 'the button card is one column');
  assert.strictEqual(rule('#acc-sso .btn, #acc-sso .adm-in, #acc-sso select, #acc-sso .sso-map-v').style.minHeight, '44px', 'buttons and fields are 44 px tall');
  assert.deepStrictEqual([rule('.sso-chip-del').style.width, rule('.sso-chip-del').style.height], ['44px', '44px'], 'a chip remove button is a 44 px target');
  assert.strictEqual(rule('.sso-uri-row').style.flexDirection, 'column', 'the redirect URI and Copy stack');
  const sw = rule('#acc-sso .sso-sw::before').style;
  assert.deepStrictEqual([sw.position, sw.inset], ['absolute', '-14px -8px'], 'the 17 by 30 px switch gets a 45 by 46 px hit area');
  const box = rule('#acc-sso .sso-f input[type="checkbox"]').style;
  assert.deepStrictEqual([box.width, box.height, box.padding, box.boxSizing], ['24px', '24px', '10px', 'content-box'], 'the checkbox is a 44 px target');
  assert.strictEqual(rule('#acc-sso .sso-adv > summary').style.minHeight, '44px', 'the Advanced fold is a 44 px target');
  const map = field(t.doc, 'role-map');
  assert.ok(map.querySelectorAll('.sso-map-row').length > 0);
  map.querySelectorAll('.sso-map-row').forEach((r) => assert.deepStrictEqual([r.querySelector('.sso-map-v').getAttribute('aria-label'), r.querySelector('.sso-map-r').getAttribute('aria-label')], ['Claim value', 'Role'], 'each row stays labelled with the header hidden'));
  assert.deepStrictEqual([...map.querySelectorAll('.sso-map-t thead th')].map((h) => [h.textContent, h.querySelector('.sr') ? 'sr' : '']), [['Claim value', ''], ['Role', ''], ['Actions', 'sr']], 'the actions column has a visually hidden header');
  t.close();
  return 'at phone width the SSO cards are one column with 44 px targets, including the switch, the checkbox and the fold, and role-map rows stay labelled';
}

async function aMissingRedirectUriSaysWhatSetsIt() {
  //given
  const t = await boot({sso: makeSso({redirectUri: null})});

  //when
  await openAccess(t);

  //then
  const box = card(t.doc).querySelector('.sso-redirect');
  assert.ok(!box.querySelector('code'), 'no address is shown');
  assert.strictEqual(box.querySelector('.sso-nouri').textContent, 'Not set: set SKOPAX_BASE_URL (or SSO_REDIRECT_URI) and restart.');
  assert.ok(box.querySelector('.sso-copy').disabled);
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'a missing redirect URI says SKOPAX_BASE_URL and a restart set it';
}

async function theMetadataLineUsesTheServerAddress() {
  //given
  const line = async (value, mcpBase) => {
    const b = await boot({sso: makeSso({mcpBase}), items: [mcpItem('mcp.client-id', 'Client id', 'skopax-mcp'), mcpItem('mcp.resource-url', 'Resource URL', value)]});
    await openAccess(b);
    const text = b.doc.querySelector('#acc-mcp .acc-mcp-meta code').textContent;
    b.close();
    return text;
  };

  //when
  const said = [await line('', 'https://public.example.com/'), await line('https://mcp.example.com', 'https://public.example.com'), await line('', '')];

  //then
  assert.deepStrictEqual(said, ['https://public.example.com/.well-known/oauth-protected-resource', 'https://mcp.example.com/.well-known/oauth-protected-resource',
    'https://skopax.example.com/.well-known/oauth-protected-resource'], 'the resource URL, else the server base URL, else the page origin');
  return 'the MCP metadata line uses the resource URL, else the server base URL, else the page origin';
}

async function theMcpCardSaysWhenItsSettingsDidNotLoad() {
  //given
  const t = await boot({items: [mcpItem('mcp.client-id', 'Client id', 'skopax-mcp'), mcpItem('mcp.audience', 'Audience', '')]});
  await openAccess(t);
  const loaded = t.doc.querySelectorAll('#acc-mcp .adm-row').length;
  t.doc.getElementById('tab-anoms').click();
  await wait(40);
  t.flags.hook = (method, p, body, h) => method === 'GET' && p.startsWith('/api/admin/settings') ? h.bad(503, 'Database unavailable') : null;

  //when
  await openAccess(t);

  //then
  const box = t.doc.getElementById('acc-mcp');
  assert.strictEqual(loaded, 2);
  assert.strictEqual(box.querySelector('.acc-mcp-failed').textContent, 'MCP settings did not load; open the tab again.', 'a failed reload says so although settings were loaded before');
  assert.strictEqual(box.querySelectorAll('.adm-row').length, 0, 'and shows no old rows');
  assert.ok(/needs single sign-on set up and on, and the MCP server switch on in Features/.test(box.querySelector('.acc-mcp-note').textContent), 'the note names the MCP server switch too');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'the MCP card says its settings did not load even when older ones were loaded, and its note names the MCP server switch';
}

async function theClientSecretFieldIsMaskedOnScreen() {
  //given
  const t = await boot();
  await openAccess(t);
  const input = field(t.doc, 'client-secret').querySelector('.sso-in');
  const before = [input.id, input.type, masks(t.doc, input), inlineMask(input)];

  //when
  setValue(input, 'typed-secret');

  //then
  assert.deepStrictEqual(before, ['sso-in-client-secret', 'text', [{onScreen: true, value: 'disc'}], null], 'exactly one page rule sets the client secret mask, outside any print or conditional block, and no inline style overrides it');
  assert.strictEqual(inlineMask(input), null, 'typing leaves no inline style on the field either');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'the client secret field is masked on screen by exactly one page rule and no inline style';
}

async function aSelectTheServerSentNoValueForIsNotSavedByItself() {
  //given
  const view = makeSso();
  delete view.values['new-accounts'];
  const t = await boot({sso: view});
  await openAccess(t);

  //when
  setValue(field(t.doc, 'scope').querySelector('.sso-in'), 'openid email');
  button(t.doc, 'sso-save').click();
  await wait(80);
  const first = puts(t)[0];
  const pick = field(t.doc, 'new-accounts').querySelector('.sso-in');
  pick.value = 'added-only';
  pick.dispatchEvent(new t.w.Event('change', {bubbles: true}));
  button(t.doc, 'sso-save').click();
  await wait(80);

  //then
  assert.deepStrictEqual(first, {values: {scope: 'openid email'}});
  assert.deepStrictEqual(puts(t)[1].values['new-accounts'], 'added-only');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  return 'a select the server sent no value for is not saved by itself, and is saved once it is changed';
}

(async () => {
  console.log('PASS ' + await rendersValuesWithSourceLabels());
  console.log('PASS ' + await aSelectTheServerSentNoValueForIsNotSavedByItself());
  console.log('PASS ' + await aSourceOwnedSecretIsReadOnly());
  console.log('PASS ' + await theTypedSecretIsSentOnce());
  console.log('PASS ' + await testConnectionListsSevenChecks());
  console.log('PASS ' + await saveAndTurnOnAndTheFieldError());
  console.log('PASS ' + await roleMapRowsAddAndRemove());
  console.log('PASS ' + await theRedirectUriIsShownWithCopy());
  console.log('PASS ' + await hostileValuesRenderAsText());
  console.log('PASS ' + await generalNoLongerListsSignInOrMcp());
  console.log('PASS ' + await theLastKnownRoleSettingSitsInTheSignInCardAndSavesFromThere());
  console.log('PASS ' + await theLastKnownRoleEditSurvivesAnSsoCardSave());
  console.log('PASS ' + await unsavedEditsSurviveAResetAndTheSwitch());
  console.log('PASS ' + await theSecretIsSentOnTestAndNeverLeftInThePage());
  console.log('PASS ' + await anAdminSecretCanBeRemovedAndAnUnreadableOneSaysSo());
  console.log('PASS ' + await theSwitchShowsItsSourceAndResets());
  console.log('PASS ' + await roleMapValuesKeepTheirSpacesAndTheJsonCapIsRefused());
  console.log('PASS ' + await theMcpCardAlwaysShowsItsNote());
  console.log('PASS ' + await anOverrideShowsTheLayerUnderIt());
  console.log('PASS ' + await aSecretClearedByLeavingTheTabStaysClearedWhenTheRequestEnds());
  console.log('PASS ' + await editsTypedWhileARequestRunsSurviveIt());
  console.log('PASS ' + await aSwitchResetWhoseReloadFailsSaysItWasReset());
  console.log('PASS ' + await hostileSwitchLabelsAndSecretNoteRenderAsText());
  console.log('PASS ' + await nothingConfiguredReadsNotSetUpWithNoProblemLine());
  console.log('PASS ' + await theProviderLineSaysWhatWentWrongInPlainWords());
  console.log('PASS ' + await anOverCapStoredMapStillLetsOtherFieldsSaveAndTest());
  console.log('PASS ' + await theRolePairsInUseShowWhileNoMapIsSaved());
  console.log('PASS ' + await switchingOffAndAResetThatTurnsItOffAskFirst());
  console.log('PASS ' + await aSecretPutIntoTheHiddenTabIsNotWrittenBack());
  console.log('PASS ' + await theCardStylesItsOverrideLabelFoldCheckboxAndRows());
  console.log('PASS ' + await aMissingRedirectUriSaysWhatSetsIt());
  console.log('PASS ' + await theMetadataLineUsesTheServerAddress());
  console.log('PASS ' + await theMcpCardSaysWhenItsSettingsDidNotLoad());
  console.log('PASS ' + await theClientSecretFieldIsMaskedOnScreen());
  console.log('PASS ' + await theStatusHeaderShowsStateHostIconAndSwitch());
  console.log('PASS ' + await theCopyButtonSelectsTheAddressWhenTheClipboardIsRefused());
  console.log('PASS ' + await theSaveBarSticksOnlyWhileThereAreUnsavedChanges());
  console.log('PASS ' + await thePhoneLayoutIsOneColumnWithLargeTargets());
  console.log('PASS ' + await theScopeIsAChipListThatSavesWithSpaces());
  console.log('PASS ' + await theScopeChipsFollowTheValueAfterARepaintAndAnInvalidChipBlocksTheSaves());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
