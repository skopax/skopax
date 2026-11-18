const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');
const {short} = require('./skopax-zone');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const EVIL = '<img src=x onerror=alert(1)>"\'';
const IDP_ROLE = "The identity provider or a role plug-in sets this person's role, so it cannot be changed here. Change it there; disabling the person still works here.";
const BOUND_AT = '2026-03-02T08:15:00Z';
const ALLOWED_AT = '2026-03-05T10:00:00Z';
const EXPIRES_AT = '2026-03-13T11:30:00Z';
const WAITING = 'Waiting for first sign-in since ' + short(ALLOWED_AT) + ', expires ' + short(EXPIRES_AT);

function makeUsers() {
  return [
    {email: 'alice@example.com', name: 'Alice Archer', role: 'admin', source: 'local', disabled: false, createdAt: iso(500)},
    {email: 'bob@example.com', name: 'Bob Baker', role: 'viewer', source: 'sso', disabled: false, createdAt: iso(400)},
    {email: 'carol@example.com', name: EVIL, role: 'operator', source: 'local', disabled: true, createdAt: iso(300)},
  ];
}
function makeTokens() {
  return [
    {id: 'a1b2c3d4', name: 'ci', role: 'operator', createdBy: 'alice@example.com', createdAt: iso(200), lastUsed: iso(5), revoked: false, endedByStop: false},
    {id: 'e5f6a7b8', name: 'deploy-bot', role: 'viewer', createdBy: 'alice@example.com', createdAt: iso(100), lastUsed: null, revoked: true, endedByStop: true},
    {id: 'c9d8e7f6', name: 'child', role: 'viewer', createdBy: 'token:ci', createdAt: iso(90), lastUsed: null, revoked: false, endedByStop: true},
    {id: '11223344', name: 'old', role: 'viewer', createdBy: 'token', createdAt: iso(80), lastUsed: null, revoked: false},
  ];
}

async function boot(opts) {
  opts = opts || {};
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state));
  let users = opts.users || makeUsers(), tokens = opts.tokens || makeTokens();
  let settings = opts.items || [{key: 'access.public-read', group: 'Access', label: 'Public read', help: 'Let anyone read the board.', kind: 'bool', live: true, value: 'false', base: 'false', default: 'false', source: 'default', by: null, at: null}];
  const flags = {failReload: false, hook: null};
  const admin = opts.admin !== false;
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=anoms', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, o) => {
        const p = String(path), method = (o && o.method) || 'GET';
        const body = o && o.body ? JSON.parse(o.body) : null;
        const ok = (b, status) => ({ok: true, status: status || 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        const none = () => ({ok: true, status: 204, json: async () => { throw new Error('no body'); }, text: async () => ''});
        const bad = (status, detail) => ({ok: false, status, json: async () => ({detail})});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, sso: false, user: 'alice@example.com', userRole: admin ? 'admin' : 'operator', source: 'local', canChangePassword: true, operator: true, admin, teams: [], tags: []});
        if (p.startsWith('/api/admin')) {
          calls.push([method, p, body]);
          if (!admin) return bad(403, 'Admins only');
          const hooked = flags.hook && await flags.hook(method, p, body, {ok, bad, none});
          if (hooked) return hooked;
          if (flags.failReload && method === 'GET' && (p === '/api/admin/users' || p === '/api/admin/tokens')) return bad(503, 'Database unavailable');
          if (p === '/api/admin/users' && method === 'GET') return ok(users);
          if (p === '/api/admin/users' && method === 'POST') {
            if (body.email === 'dup@example.com') return bad(409, 'That user already exists.');
            if (body.password.length < 12) return bad(422, 'A password needs 12 to 256 characters.');
            const made = {email: body.email, name: body.name, role: body.role, source: 'local', disabled: false, createdAt: iso(0)};
            users = users.concat([made]);
            return ok(made, 201);
          }
          if (p.startsWith('/api/admin/users/') && method === 'PATCH') {
            const email = decodeURIComponent(p.split('/').pop());
            if (email === 'alice@example.com' && ((body.role && body.role !== 'admin') || body.disabled === true)) return bad(409, 'Skopax needs at least one admin.');
            if (body.password !== undefined && body.password.length < 12) return bad(422, 'A password needs 12 to 256 characters.');
            const held = users.find((u) => u.email === email);
            if (body.role !== undefined && held && (held.roleFrom === 'claim' || held.roleFrom === 'resolver')) return bad(409, IDP_ROLE);
            const link = (u) => body.ssoLink === undefined ? u.ssoLink : !body.ssoLink ? null : u.ssoLink && u.ssoLink.state === 'bound' ? u.ssoLink : {state: 'pending', boundAt: null, allowedAt: ALLOWED_AT, expiresAt: EXPIRES_AT};
            const fields = Object.assign({}, body);
            delete fields.ssoLink;
            users = users.map((u) => u.email === email ? Object.assign({}, u, body.password === undefined ? fields : {}, {ssoLink: link(u)}) : u);
            if (opts.slowPatch) await wait(opts.slowPatch);
            return ok(users.find((u) => u.email === email));
          }
          if (p === '/api/admin/tokens' && method === 'GET') return ok(tokens);
          if (p === '/api/admin/tokens' && method === 'POST') {
            if (body.name === 'bootstrap') return bad(422, 'That token name is reserved.');
            const doc = {id: '0a1b2c3d', name: body.name, role: body.role, createdBy: 'alice@example.com', createdAt: iso(0), lastUsed: null, revoked: false};
            tokens = tokens.concat([doc]);
            return ok(Object.assign({}, doc, {token: 'skopax_0a1b2c3d_SECRETSECRETSECRETSECRET'}), 201);
          }
          if (p.startsWith('/api/admin/tokens/') && method === 'DELETE') {
            const id = decodeURIComponent(p.split('/').pop());
            tokens = tokens.map((t) => t.id === id ? Object.assign({}, t, {revoked: true}) : t);
            return none();
          }
          if (p.startsWith('/api/admin/history')) return ok([]);
          if (p.startsWith('/api/admin/small-talk')) return bad(404, 'none');
          if (p === '/api/admin/settings' && method === 'PUT') {
            settings = settings.map((i) => i.key === body.key ? Object.assign({}, i, {value: body.value, source: 'admin', by: 'alice@example.com', at: iso(0)}) : i);
            return ok({item: settings.find((i) => i.key === body.key), change: {}});
          }
          if (p.startsWith('/api/admin/settings')) return ok({items: settings, rev: 1, refreshSeconds: 15});
        }
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(150);
  return {w: dom.window, doc: dom.window.document, errors, calls, flags, close: () => dom.window.close()};
}

async function openAccess(t) {
  t.doc.getElementById('tab-access').click();
  await wait(120);
}
const setValue = (el, v) => { el.value = v; el.dispatchEvent(new el.ownerDocument.defaultView.Event('input', {bubbles: true})); };
const change = (el) => el.dispatchEvent(new el.ownerDocument.defaultView.Event('change', {bubbles: true}));
const rowOf = (doc, email) => [...doc.querySelectorAll('#access .acc-user')].filter((r) => r.dataset.email === email)[0];

(async () => {
  //given
  const t = await boot();
  await openAccess(t);
  const box = t.doc.getElementById('access');

  //then
  assert.ok(!t.doc.getElementById('panel-access').hidden, 'the Access panel opens');
  assert.deepStrictEqual([...box.querySelectorAll('.acc-user')].map((r) => r.dataset.email), ['alice@example.com', 'bob@example.com', 'carol@example.com']);
  const bob = rowOf(t.doc, 'bob@example.com');
  assert.strictEqual(bob.querySelector('select.acc-role').value, 'viewer');
  assert.ok(/sso/i.test(bob.querySelector('.acc-src').textContent) && /local/i.test(rowOf(t.doc, 'alice@example.com').querySelector('.acc-src').textContent), 'source badges');
  assert.strictEqual(rowOf(t.doc, 'carol@example.com').querySelector('input.acc-off').checked, true);
  assert.ok(!bob.querySelector('.acc-reset') && !bob.querySelector('.acc-pw-form'), 'an SSO user has no Reset password');
  assert.ok(rowOf(t.doc, 'alice@example.com').querySelector('.acc-reset') && rowOf(t.doc, 'carol@example.com').querySelector('.acc-reset'), 'local users have Reset password');
  assert.ok(t.calls.some((c) => c[0] === 'GET' && c[1] === '/api/admin/users') && t.calls.some((c) => c[0] === 'GET' && c[1] === '/api/admin/tokens'));
  console.log('PASS the Access tab lists users with role select, source badge, disabled switch and Reset password');

  //when
  const sel = bob.querySelector('select.acc-role');
  sel.value = 'operator';
  change(sel);
  await wait(80);

  //then
  const patch = t.calls.filter((c) => c[0] === 'PATCH');
  assert.deepStrictEqual(patch[0], ['PATCH', '/api/admin/users/bob%40example.com', {role: 'operator'}]);
  assert.strictEqual(rowOf(t.doc, 'bob@example.com').querySelector('select.acc-role').value, 'operator');
  console.log('PASS changing a role sends PATCH with the new role');

  //when
  const aliceRow = rowOf(t.doc, 'alice@example.com');
  const off = aliceRow.querySelector('input.acc-off');
  off.checked = true;
  change(off);
  await wait(80);

  //then
  const arow = rowOf(t.doc, 'alice@example.com');
  assert.ok(/Skopax needs at least one admin\./.test(arow.querySelector('.adm-msg').textContent), 'the 409 text shows in the row');
  assert.strictEqual(arow.querySelector('input.acc-off').checked, false, 'the switch goes back');
  const asel = arow.querySelector('select.acc-role');
  asel.value = 'viewer';
  change(asel);
  await wait(80);
  const arow2 = rowOf(t.doc, 'alice@example.com');
  assert.strictEqual(arow2.querySelector('select.acc-role').value, 'admin', 'the role goes back');
  assert.ok(/at least one admin/.test(arow2.querySelector('.adm-msg').textContent));
  console.log('PASS disabling or demoting the last admin shows the 409 text inline and puts the control back');

  //when
  const reset = rowOf(t.doc, 'carol@example.com').querySelector('.acc-reset');
  reset.click();
  const pw = rowOf(t.doc, 'carol@example.com').querySelector('input.acc-pw');
  setValue(pw, 'short');
  rowOf(t.doc, 'carol@example.com').querySelector('.acc-pw-save').click();
  await wait(80);

  //then
  assert.ok(/12 to 256/.test(rowOf(t.doc, 'carol@example.com').querySelector('.adm-msg').textContent), '422 detail inline on the row');
  setValue(rowOf(t.doc, 'carol@example.com').querySelector('input.acc-pw'), 'a-long-enough-password');
  rowOf(t.doc, 'carol@example.com').querySelector('.acc-pw-save').click();
  await wait(80);
  const pwPatch = t.calls.filter((c) => c[0] === 'PATCH' && c[2].password);
  assert.deepStrictEqual(pwPatch[1][2], {password: 'a-long-enough-password'});
  assert.ok([...t.doc.querySelectorAll('#access .acc-pw-form')].every((f) => f.hidden), 'the reset form closes');
  assert.ok(/Password reset/.test(rowOf(t.doc, 'carol@example.com').querySelector('.adm-msg').textContent), 'success note on the row');
  console.log('PASS Reset password sends the password and shows 422 inline');

  //when
  const form = t.doc.querySelector('#access .acc-add-user');
  setValue(form.querySelector('[name=email]'), 'dup@example.com');
  setValue(form.querySelector('[name=name]'), 'Dup');
  form.querySelector('[name=role]').value = 'viewer';
  setValue(form.querySelector('[name=password]'), 'a-long-enough-password');
  form.querySelector('button[type=submit]').click();
  await wait(80);

  //then
  assert.ok(/already exists/.test(t.doc.querySelector('#access .acc-add-user .adm-msg').textContent), '409 shown next to the add form');
  setValue(t.doc.querySelector('#access .acc-add-user [name=email]'), 'dave@example.com');
  t.doc.querySelector('#access .acc-add-user button[type=submit]').click();
  await wait(80);
  const post = t.calls.filter((c) => c[0] === 'POST' && c[1] === '/api/admin/users');
  assert.deepStrictEqual(post[1][2], {email: 'dave@example.com', name: 'Dup', role: 'viewer', password: 'a-long-enough-password'});
  assert.ok(rowOf(t.doc, 'dave@example.com'), 'the new user is listed');
  console.log('PASS Add user posts the form and shows server errors next to it');

  //then
  const toks = [...t.doc.querySelectorAll('#access .acc-token')];
  assert.deepStrictEqual(toks.map((r) => r.dataset.id), ['a1b2c3d4', 'e5f6a7b8', 'c9d8e7f6', '11223344']);
  assert.ok(/skopax_a1b2c3d4_/.test(toks[0].textContent), 'prefix shown');
  assert.ok(!/SECRET/.test(t.doc.body.textContent));
  assert.ok(toks[0].querySelector('.acc-revoke') && !toks[1].querySelector('.acc-revoke'), 'a revoked token has no Revoke button');
  assert.ok(/alice@example\.com/.test(toks[0].textContent));
  const createdBy = (id) => t.doc.querySelector('#access .acc-token[data-id="' + id + '"]').children[3].textContent;
  assert.strictEqual(createdBy('c9d8e7f6'), 'ci', 'a token creator shows the token name');
  assert.strictEqual(createdBy('11223344'), 'Claude', 'the legacy bare token reads Claude, like token_label on the server');
  assert.deepStrictEqual(toks.map((r) => [...r.querySelectorAll('.acc-stop')].map((x) => [x.textContent, x.classList.contains('tag'), x.title])), [[], [], [['Refused by the SSO stop', true,
    'Made from a single sign-on session and refused by the SSO stop: it stays refused after SSO is switched back on, so make a new token']], []],
    'only a live token with endedByStop true is tagged as refused by the SSO stop, never a revoked one');
  console.log('PASS the tokens table shows name, role, prefix, creator and Revoke for live tokens only, and tags a live token refused by the SSO stop');

  //when
  const tf = t.doc.querySelector('#access .acc-add-token');
  setValue(tf.querySelector('[name=name]'), 'bootstrap');
  tf.querySelector('[name=role]').value = 'operator';
  tf.querySelector('button[type=submit]').click();
  await wait(80);

  //then
  assert.ok(/reserved/.test(t.doc.querySelector('#access .acc-add-token .adm-msg').textContent), '422 inline next to the token form');
  assert.ok(!t.doc.querySelector('#access .acc-secret code'), 'no token was shown');

  //when
  setValue(t.doc.querySelector('#access .acc-add-token [name=name]'), 'Ops-Bot');
  t.doc.querySelector('#access .acc-add-token button[type=submit]').click();
  await wait(100);

  //then
  const tpost = t.calls.filter((c) => c[0] === 'POST' && c[1] === '/api/admin/tokens');
  assert.deepStrictEqual(tpost[1][2], {name: 'Ops-Bot', role: 'operator'});
  const secret = t.doc.querySelector('#access .acc-secret');
  assert.ok(secret && secret.textContent.indexOf('skopax_0a1b2c3d_SECRETSECRETSECRETSECRET') >= 0, 'the token is shown');
  assert.ok(/Copy it now\. It won't be shown again\./.test(secret.textContent), 'the note');
  assert.ok([...secret.querySelectorAll('button')].some((b) => b.textContent.trim() === 'Copy'), 'Copy button');
  const store = (s) => { let out = ''; for (let i = 0; i < s.length; i++) out += s.key(i) + '=' + s.getItem(s.key(i)) + ';'; return out; };
  assert.ok(!/SECRET/.test(store(t.w.localStorage)) && !/SECRET/.test(store(t.w.sessionStorage)), 'never in web storage');
  console.log('PASS creating a token shows it once with Copy and the note, and never stores it');

  //when
  const keep = [...t.doc.querySelectorAll('#access .acc-user')][1].querySelector('select.acc-role');
  keep.value = 'viewer';
  change(keep);
  await wait(100);

  //then
  assert.ok(!/SECRET/.test(t.doc.documentElement.outerHTML), 'the token is gone from the DOM after the next render');
  assert.ok(!t.doc.querySelector('#access .acc-secret code'));
  assert.ok(t.doc.querySelector('#access .acc-token[data-id="0a1b2c3d"]'), 'the token row stays in the list');
  console.log('PASS the created token is gone from the DOM after the next re-render');

  //when
  t.doc.querySelector('#access .acc-token[data-id="a1b2c3d4"] .acc-revoke').click();
  await wait(100);

  //then
  const del = t.calls.filter((c) => c[0] === 'DELETE');
  assert.deepStrictEqual(del[0], ['DELETE', '/api/admin/tokens/a1b2c3d4', null]);
  assert.ok(!t.doc.querySelector('#access .acc-token[data-id="a1b2c3d4"] .acc-revoke'), 'Revoke is gone after the 204');
  console.log('PASS Revoke sends DELETE and copes with the empty 204 answer');

  //then
  assert.strictEqual(t.doc.querySelectorAll('#access img').length, 0, 'markup in a name never becomes an element');
  assert.ok(box.textContent.indexOf(EVIL) >= 0, 'it shows as text');
  assert.ok(!/onerror/.test([...box.querySelectorAll('*')].map((e) => [...e.attributes].map((a) => a.name).join(' ')).join(' ')), 'no event handler attributes');
  assert.deepStrictEqual(t.errors, []);
  console.log('PASS hostile names render as text');
  t.close();

  //given
  const r = await boot();
  await openAccess(r);
  r.flags.failReload = true;

  //when
  const rbob = rowOf(r.doc, 'bob@example.com').querySelector('input.acc-off');
  rbob.checked = true;
  change(rbob);
  await wait(100);

  //then
  const after = rowOf(r.doc, 'bob@example.com');
  assert.strictEqual(after.querySelector('input.acc-off').checked, true, 'the row shows the saved state even when the list cannot reload');
  assert.ok(/Disabled/.test(after.querySelector('.adm-msg').textContent));

  //when
  r.doc.querySelector('#access .acc-token[data-id="a1b2c3d4"] .acc-revoke').click();
  await wait(100);

  //then
  const rev = r.doc.querySelector('#access .acc-token[data-id="a1b2c3d4"]');
  assert.ok(!rev.querySelector('.acc-revoke') && /Revoked/.test(rev.textContent), 'a revoked token has no Revoke button when the reload fails');
  const stale = r.doc.querySelectorAll('#access .acc-stale');
  assert.strictEqual(stale.length, 1, 'one note at a time');
  assert.ok(stale[0].classList.contains('bad') && /Could not refresh the list/.test(stale[0].textContent), 'the failed refresh is said, styled as an error');
  r.close();
  console.log('PASS a failed reload after a save never leaves stale rows next to a success note');

  //given
  const nonAdmin = await boot({admin: false});

  //then
  assert.ok(nonAdmin.doc.getElementById('tab-access').hidden, 'no Access tab for a non-admin');
  nonAdmin.w.location.hash = '#t=access';
  await wait(150);
  assert.ok(!nonAdmin.doc.getElementById('tab-access').hasAttribute('aria-current') && nonAdmin.doc.getElementById('side-admin').hidden, 'a link does not open Access, and there is no Admin row');
  assert.ok(!nonAdmin.calls.some((c) => /\/api\/admin\/(users|tokens)/.test(c[1])), 'no users or tokens calls');
  assert.deepStrictEqual(nonAdmin.errors, []);
  nonAdmin.close();
  console.log('PASS a non-admin sees no Access tab and the page makes no users or tokens calls');

  //given
  const source = fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'tunables.py'), 'utf8');
  const names = /^(\w+(?:, \w+)+) = \(([^)]*)\)/m.exec(source);
  const groups = {};
  names[1].split(', ').forEach((n, i) => { groups[n] = names[2].split(',').map((x) => x.trim().replace(/^'|'$/g, ''))[i]; });
  const declared = /T\('access\.public-read', 'public_read', (\w+), '([^']+)', '([^']+)', 'bool'/.exec(source);
  const item = {key: 'access.public-read', group: groups[declared[1]], label: declared[2], help: declared[3], kind: 'bool', live: true, value: 'false', base: 'false', default: 'false', source: 'default', by: null, at: null};
  const features = fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'features.py'), 'utf8');
  const card = /Feature\('public-read', 'access\.public-read', '([^']+)', '([^']+)', '([a-z]+)'\)/.exec(features);
  const feature = {id: 'public-read', key: 'access.public-read', label: card[1], help: card[2], icon: card[3], on: false, stored: false, locked: false, edition: 'community', needs: [], restart: null, source: 'default', reason: null};
  const sibling = Object.assign({}, item, {key: 'access.session-hours', label: 'Session length (h)', kind: 'int', value: '12', base: '12', default: '12', low: 1, high: 720});
  const s = await boot();
  s.flags.hook = (method, p, body, h) => method !== 'GET' ? null : p.startsWith('/api/admin/settings') ? h.ok({items: [item, sibling], rev: 1, refreshSeconds: 15}) : p === '/api/admin/features' ? h.ok({edition: 'community', items: [feature]}) : null;
  s.doc.getElementById('tab-settings').click();
  await wait(150);
  const group = s.doc.querySelector('#admin details[data-group="' + item.group + '"]');
  group.open = true;
  const inGeneral = [...group.querySelectorAll('.adm-row')].map((r) => r.dataset.key);
  s.doc.getElementById('tab-features').click();
  await wait(150);
  const shown = s.doc.querySelector('#features .feat[data-id="public-read"]');

  //then
  assert.strictEqual(item.group, 'Access', 'the server declares public read in the Access group');
  assert.deepStrictEqual(inGeneral, ['access.session-hours'], 'General keeps the Access group but not the public-read switch');
  assert.ok(shown && shown.querySelector('.feat-l').textContent === card[1] && shown.querySelector('[role="switch"]').getAttribute('aria-checked') === 'false', 'public read is a switch on the Features tab');
  assert.deepStrictEqual(s.errors, []);
  s.close();
  console.log('PASS the public-read switch tunables.py declares in the Access group is a card on Admin \u00b7 Features, not a row in General');

  //given
  const h = await boot();

  //then
  const ev = (code) => h.w.eval(code);
  assert.strictEqual(ev("isTokenActor('token')"), true);
  assert.strictEqual(ev("isTokenActor('token:ci')"), true);
  assert.strictEqual(ev("isTokenActor('alice@example.com')"), false);
  assert.strictEqual(ev("isTokenActor('tokenx')"), false);
  assert.strictEqual(ev("tokenActorLabel('token:deploy-bot', 'Claude')"), 'deploy-bot');
  assert.strictEqual(ev("tokenActorLabel('token', 'Claude')"), 'Claude');
  assert.strictEqual(ev("tokenActorLabel('token:', 'API')"), 'API');
  assert.strictEqual(ev("draftBy('token:ci')"), 'ci');
  assert.strictEqual(ev("draftBy('token')"), 'Claude');
  assert.strictEqual(ev("resWho('token:ci')"), 'ci');
  assert.strictEqual(ev("resWho('token')"), 'Claude');
  assert.strictEqual(ev("costBy('token:ci')"), 'ci');
  assert.strictEqual(ev("costBy('token')"), 'API calls');
  assert.strictEqual(ev("sourceLabel({requestedBy: 'token:ci'})"), 'API');
  h.close();
  console.log('PASS token actors share one helper: legacy token and token:<name> label the same way everywhere');

  //given
  const attrs = (root) => [...root.querySelectorAll('*')].map((e) => [...e.attributes].map((a) => a.name).join(' ')).join(' ');
  const x = await boot({tokens: [{id: EVIL, name: EVIL, role: 'viewer', createdBy: 'token:' + EVIL, createdAt: iso(10), lastUsed: null, revoked: false},
                                 {id: 'b0b0b0b0', name: 'plain', role: 'viewer', createdBy: EVIL, createdAt: iso(9), lastUsed: null, revoked: false}]});
  await openAccess(x);
  const tokenRow = (doc, id) => [...doc.querySelectorAll('#access .acc-token')].filter((r) => r.dataset.id === id)[0];

  //when
  const cellsBefore = [...tokenRow(x.doc, EVIL).children].map((c) => c.textContent);
  const plainBy = tokenRow(x.doc, 'b0b0b0b0').children[3].textContent;
  tokenRow(x.doc, EVIL).querySelector('.acc-revoke').click();
  await wait(100);

  //then
  assert.deepStrictEqual([cellsBefore[0], cellsBefore[3], plainBy], [EVIL, EVIL, EVIL], 'hostile token name and creators show as text');
  assert.deepStrictEqual(x.calls.filter((c) => c[0] === 'DELETE').map((c) => c[1]), ['/api/admin/tokens/' + encodeURIComponent(EVIL)], 'the id is encoded into the path');
  assert.strictEqual(tokenRow(x.doc, EVIL).querySelector('.adm-msg').textContent, 'Revoked', 'the note finds the row by its id, whatever characters it holds');
  assert.strictEqual(x.doc.querySelectorAll('#access img').length, 0);
  assert.ok(!/onerror/.test(attrs(x.doc.getElementById('access'))));
  assert.deepStrictEqual(x.errors, []);
  x.close();
  console.log('PASS hostile token names, creators and ids render as text, and Revoke still reaches and notes the right row');

  //given
  const odd = 'o"brien\\x@example.com';
  const q = await boot({users: [{email: 'alice@example.com', name: 'Alice', role: 'admin', source: 'local', disabled: false, createdAt: iso(5)},
                                {email: odd, name: 'Odd', role: 'viewer', source: 'local', disabled: false, createdAt: iso(4)}]});
  await openAccess(q);

  //when
  const oddSel = rowOf(q.doc, odd).querySelector('select.acc-role');
  oddSel.value = 'operator';
  change(oddSel);
  await wait(100);

  //then
  assert.deepStrictEqual(q.calls.filter((c) => c[0] === 'PATCH').map((c) => c[1]), ['/api/admin/users/' + encodeURIComponent(odd)]);
  assert.strictEqual(rowOf(q.doc, odd).querySelector('.adm-msg').textContent, 'Role set to operator', 'an email with a quote or backslash still gets its note');
  q.close();
  console.log('PASS the saved note finds the user row by comparing its email, so quotes and backslashes in it do not lose the note');

  //given
  const e = await boot();
  await openAccess(e);
  const answers = [[429, 'Too many changes; try again in a minute.'], [503, 'database unavailable'], [422, EVIL], [422, [{msg: 'first problem'}, {msg: 'second problem'}]]];

  //when
  const seen = [];
  for (const [status, detail] of answers) {
    e.flags.hook = (method, p, body, h) => method === 'POST' && p === '/api/admin/tokens' ? h.bad(status, detail) : null;
    setValue(e.doc.querySelector('#access .acc-add-token [name=name]'), 'ops');
    e.doc.querySelector('#access .acc-add-token button[type=submit]').click();
    await wait(60);
    const note = e.doc.querySelector('#access .acc-add-token .adm-msg');
    seen.push([note.textContent, note.classList.contains('bad')]);
  }
  e.flags.hook = (method, p, body, h) => method === 'PATCH' ? h.bad(503, 'database unavailable') : null;
  const busySel = rowOf(e.doc, 'bob@example.com').querySelector('select.acc-role');
  busySel.value = 'operator';
  change(busySel);
  await wait(60);

  //then
  assert.deepStrictEqual(seen, [['Too many changes; try again in a minute.', true], ['database unavailable', true], [EVIL, true], ['first problem; second problem', true]],
    '429 and 503 texts, a hostile detail as text, and a list detail joined');
  assert.deepStrictEqual([rowOf(e.doc, 'bob@example.com').querySelector('.adm-msg').textContent, busySel.value], ['database unavailable', 'viewer'], 'a 503 on a row says why and puts the role back');
  assert.strictEqual(e.doc.querySelectorAll('#access img').length, 0);
  assert.deepStrictEqual(e.errors, []);
  e.close();
  console.log('PASS 429 and 503 answers, a hostile detail and a list of details all show inline as text');

  //given
  const c = await boot();
  await openAccess(c);
  setValue(c.doc.querySelector('#access .acc-add-token [name=name]'), 'ops');
  c.doc.querySelector('#access .acc-add-token button[type=submit]').click();
  await wait(100);
  const copied = [];
  Object.defineProperty(c.w.navigator, 'clipboard', {configurable: true, value: {writeText: async (text) => { copied.push(text); }}});
  const copyButton = () => [...c.doc.querySelectorAll('#access .acc-secret button')].filter((b) => b.textContent === 'Copy')[0];
  const copyNote = () => c.doc.querySelector('#access .acc-secret .adm-msg').textContent;

  //when
  copyButton().click();
  await wait(30);
  const viaClipboard = [copied.slice(), copyNote()];
  Object.defineProperty(c.w.navigator, 'clipboard', {configurable: true, value: {writeText: async () => { throw new Error('denied'); }}});
  copyButton().click();
  await wait(30);

  //then
  assert.deepStrictEqual(viaClipboard, [['skopax_0a1b2c3d_SECRETSECRETSECRETSECRET'], 'Copied']);
  assert.strictEqual(copyNote(), 'Selected: press Ctrl+C or Cmd+C to copy', 'without clipboard access the token is selected instead');
  assert.strictEqual(String(c.w.getSelection()), 'skopax_0a1b2c3d_SECRETSECRETSECRETSECRET');

  //when
  c.doc.getElementById('tab-anoms').click();
  await wait(60);

  //then
  assert.strictEqual(c.doc.querySelector('#access .acc-secret'), null, 'leaving the Access tab drops the shown token');
  assert.ok(!/SECRET/.test(c.doc.documentElement.outerHTML), 'it is not left in the hidden panel');
  assert.deepStrictEqual(c.errors, []);
  c.close();
  console.log('PASS Copy writes the token to the clipboard or selects it when that is refused, and leaving the tab drops it');

  //given
  const k = await boot({slowPatch: 60});
  await openAccess(k);
  k.flags.hook = async (method, p, body, h) => {
    if (method !== 'PATCH' || !p.endsWith(encodeURIComponent('carol@example.com'))) return null;
    await wait(150);
    return h.bad(422, 'Carol cannot change right now.');
  };

  //when
  const bobSel = rowOf(k.doc, 'bob@example.com').querySelector('select.acc-role');
  bobSel.value = 'operator';
  change(bobSel);
  const carolOff = rowOf(k.doc, 'carol@example.com').querySelector('input.acc-off');
  carolOff.checked = false;
  change(carolOff);
  await wait(300);

  //then
  const carolNow = rowOf(k.doc, 'carol@example.com');
  assert.ok(carolNow.isConnected && carolNow !== carolOff.closest('tr'), 'the list was redrawn while Carol\'s change was in flight');
  assert.deepStrictEqual([carolNow.querySelector('.adm-msg').textContent, carolNow.querySelector('.adm-msg').classList.contains('bad')], ['Carol cannot change right now.', true],
    'the error lands on the row on screen, not on the one the redraw dropped');
  assert.strictEqual(rowOf(k.doc, 'bob@example.com').querySelector('.adm-msg').textContent, 'Role set to operator');
  k.close();
  console.log('PASS an error from an edit that finishes after another edit redrew the list shows on the visible row');

  //given
  const linkUsers = [{email: 'alice@example.com', name: 'Alice', role: 'admin', source: 'local', disabled: false, createdAt: iso(9), ssoLink: null},
    {email: 'bob@example.com', name: 'Bob', role: 'viewer', source: 'sso', disabled: false, createdAt: iso(8), ssoLink: null, roleFrom: 'claim'},
    {email: 'dave@example.com', name: 'Dave', role: 'operator', source: 'local', disabled: false, createdAt: iso(7), ssoLink: {state: 'bound', boundAt: BOUND_AT}},
    {email: 'erin@example.com', name: 'Erin', role: 'viewer', source: 'local', disabled: false, createdAt: iso(6), ssoLink: {state: 'expired', boundAt: null, allowedAt: '2026-02-01T09:00:00Z', expiresAt: '2026-02-08T09:00:00Z'}},
    {email: 'frank@example.com', name: 'Frank', role: 'viewer', source: 'sso', disabled: false, createdAt: iso(5), ssoLink: null, roleFrom: 'default'}];
  const l = await boot({users: linkUsers});
  await openAccess(l);
  const linkOf = (email) => rowOf(l.doc, email).querySelector('.acc-link');
  const stateOf = (email) => rowOf(l.doc, email).querySelector('.acc-link-state').textContent;

  //then
  assert.deepStrictEqual([...l.doc.querySelectorAll('#access .acc-table')][0].querySelector('thead').textContent, 'EmailNameRoleSourceDisabledSSO link');
  assert.deepStrictEqual([linkOf('alice@example.com').getAttribute('role'), linkOf('alice@example.com').getAttribute('aria-checked'), stateOf('alice@example.com')], ['switch', 'false', 'Off']);
  assert.deepStrictEqual([linkOf('dave@example.com').getAttribute('aria-checked'), stateOf('dave@example.com')], ['true', 'Linked ' + short(BOUND_AT)]);
  assert.ok(!rowOf(l.doc, 'bob@example.com').querySelector('.acc-link') && !rowOf(l.doc, 'frank@example.com').querySelector('.acc-link'), 'no link switch on SSO users');
  assert.strictEqual(linkOf('alice@example.com').getAttribute('aria-label'), 'SSO link for alice@example.com');

  //when
  linkOf('alice@example.com').click();
  await wait(100);

  //then
  assert.deepStrictEqual(l.calls.filter((c) => c[0] === 'PATCH'), [['PATCH', '/api/admin/users/alice%40example.com', {ssoLink: true}]]);
  assert.deepStrictEqual([linkOf('alice@example.com').getAttribute('aria-checked'), stateOf('alice@example.com')], ['true', WAITING]);
  assert.strictEqual(rowOf(l.doc, 'alice@example.com').querySelector('.adm-msg').textContent, 'SSO link allowed');

  //when
  linkOf('dave@example.com').click();
  await wait(100);

  //then
  assert.deepStrictEqual(l.calls.filter((c) => c[0] === 'PATCH')[1], ['PATCH', '/api/admin/users/dave%40example.com', {ssoLink: false}]);
  assert.deepStrictEqual([linkOf('dave@example.com').getAttribute('aria-checked'), stateOf('dave@example.com')], ['false', 'Off']);
  console.log('PASS a local user gets an SSO link switch: Off, Waiting for first sign-in since and expires dates after PATCH ssoLink true, Linked with its date, and SSO users get none');

  //then
  assert.deepStrictEqual([linkOf('erin@example.com').getAttribute('aria-checked'), stateOf('erin@example.com')], ['true', 'Expired ' + short('2026-02-08T09:00:00Z') + ' with no first sign-in']);

  //when
  rowOf(l.doc, 'erin@example.com').querySelector('.acc-link-again').click();
  await wait(100);

  //then
  assert.deepStrictEqual(l.calls.filter((c) => c[0] === 'PATCH')[2], ['PATCH', '/api/admin/users/erin%40example.com', {ssoLink: true}]);
  assert.strictEqual(stateOf('erin@example.com'), WAITING);
  assert.ok(!rowOf(l.doc, 'erin@example.com').querySelector('.acc-link-again'), 'the re-allow action goes once the link waits again');
  console.log('PASS an expired link says so and Allow again PATCHes ssoLink true');

  //then
  const idpSel = rowOf(l.doc, 'bob@example.com').querySelector('select.acc-role');
  assert.strictEqual(idpSel.disabled, true, 'a role from the sign-in provider cannot be picked here');
  assert.strictEqual(rowOf(l.doc, 'bob@example.com').querySelector('.acc-idp').textContent, 'from sign-in provider');
  assert.ok(!rowOf(l.doc, 'frank@example.com').querySelector('select.acc-role').disabled && !rowOf(l.doc, 'frank@example.com').querySelector('.acc-idp'), 'a default role stays editable');

  //when
  idpSel.value = 'admin';
  change(idpSel);
  await wait(100);

  //then
  assert.deepStrictEqual(l.calls.filter((c) => c[0] === 'PATCH')[3], ['PATCH', '/api/admin/users/bob%40example.com', {role: 'admin'}]);
  assert.strictEqual(rowOf(l.doc, 'bob@example.com').querySelector('.adm-msg').textContent, IDP_ROLE);
  assert.strictEqual(rowOf(l.doc, 'bob@example.com').querySelector('select.acc-role').value, 'viewer', 'the role goes back');
  assert.deepStrictEqual(l.errors, []);
  l.close();
  console.log('PASS a role from the sign-in provider is a disabled select with its badge, and a forced change shows the 409 text inline');

  //given
  const mcp = (key, label, value, extra) => Object.assign({key, group: 'MCP', label, help: label + ' help.', kind: 'text', live: true, value, base: value, default: '', source: 'default', by: null, at: null}, extra || {});
  const items = [mcp('mcp.scope', 'Scope', 'skopax'), mcp('mcp.audience', 'Audience', ''), mcp('mcp.client-id', 'Client id', 'skopax-mcp', {source: 'config', from: 'environment', base: 'skopax-mcp'}),
    mcp('mcp.allowed-clients', 'Allowed clients', '', {kind: 'list'}), mcp('mcp.required-role', 'Required role claim', 'SKOPAX_MCP'), mcp('mcp.issuer', 'Issuer', 'https://idp.example.com'),
    mcp('mcp.resource-url', 'Resource URL', 'https://mcp.example.com'), {key: 'access.session-hours', group: 'Access', label: 'Session length (h)', help: 'h', kind: 'int', live: true, value: '12', base: '12', default: '12', source: 'default', by: null, at: null, low: 1, high: 720}];
  const m = await boot({items});
  await openAccess(m);
  const mcpBox = m.doc.querySelector('#access #acc-mcp');
  const mcpRow = (key) => [...mcpBox.querySelectorAll('.adm-row')].filter((r) => r.dataset.key === key)[0];

  //then
  assert.deepStrictEqual([...mcpBox.querySelectorAll('.adm-row')].map((r) => r.dataset.key), ['mcp.client-id', 'mcp.audience', 'mcp.allowed-clients', 'mcp.required-role', 'mcp.issuer', 'mcp.scope', 'mcp.resource-url']);
  assert.ok(/needs single sign-on/.test(mcpBox.querySelector('.acc-mcp-note').textContent), 'a note says MCP needs SSO on');
  assert.strictEqual(mcpRow('mcp.audience').querySelector('.adm-in').placeholder, 'skopax-mcp', 'the audience placeholder is the client id');
  assert.strictEqual(mcpRow('mcp.client-id').querySelector('.tag').textContent, 'config', 'each row shows its source label');
  assert.strictEqual(mcpRow('mcp.resource-url').querySelector('.acc-mcp-meta code').textContent, 'https://mcp.example.com/.well-known/oauth-protected-resource');
  assert.ok(!mcpBox.querySelector('.adm-row[data-key="access.session-hours"]'));

  //when
  setValue(mcpRow('mcp.audience').querySelector('.adm-in'), 'api://skopax');
  mcpRow('mcp.audience').querySelector('.adm-save').click();
  await wait(100);

  //then
  assert.deepStrictEqual(m.calls.filter((c) => c[0] === 'PUT'), [['PUT', '/api/admin/settings', {key: 'mcp.audience', value: 'api://skopax'}]]);
  assert.ok(/^Saved/.test(mcpRow('mcp.audience').querySelector('.adm-msg').textContent), 'the saved note lands on the MCP row');
  assert.ok(/Set by alice/.test(mcpRow('mcp.audience').querySelector('.tag').textContent));
  assert.deepStrictEqual(m.errors, []);
  m.close();
  console.log('PASS the MCP card lists the MCP settings in order, notes it needs SSO, and saves mcp.audience through PUT /api/admin/settings');
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
