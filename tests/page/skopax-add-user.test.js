require('./focus');
const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const OPTIONAL = 'Optional: leave empty for an SSO-only account';
const SSO_HELP = 'The first SSO sign-in with this email, verified by your provider, opens this account';
const NEED_PASSWORD = 'Enter a password of 12-256 characters';

function until(check, what) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const look = () => {
      let got;
      try { got = check(); } catch (e) { got = null; }
      if (got) return resolve(got);
      if (Date.now() - started > 3000) return reject(new Error('timed out waiting for ' + what));
      setTimeout(look, 5);
    };
    look();
  });
}

function makeUsers() {
  return [
    {email: 'alice@example.com', name: 'Alice', role: 'admin', source: 'local', disabled: false, createdAt: iso(500), lastLogin: iso(5), ssoLink: null},
    {email: 'bob@example.com', name: 'Bob', role: 'viewer', source: 'sso', roleFrom: 'default', disabled: false, createdAt: iso(400), lastLogin: iso(50), ssoLink: null},
    {email: 'dana@example.com', name: 'dana', role: 'operator', source: 'sso', roleFrom: 'admin', disabled: false, createdAt: iso(30), lastLogin: null, opened: false, ssoLink: null},
    {email: 'old@example.com', name: 'old', role: 'viewer', source: 'sso', roleFrom: 'admin', disabled: false, createdAt: iso(900), lastLogin: null, ssoLink: null},
    {email: 'mo@example.com', name: 'mo', role: 'viewer', source: 'sso', roleFrom: 'default', disabled: false, createdAt: iso(20), lastLogin: null, ssoLink: null},
    {email: 'carl@example.com', name: 'Carl', role: 'admin', source: 'local', disabled: false, createdAt: iso(60), lastLogin: iso(10), ssoLink: {state: 'bound', boundAt: iso(30), allowedAt: null, expiresAt: null}},
  ];
}

function ssoView(active) {
  return {enabled: active, active, problem: null, redirectUri: 'https://skopax.example.com/authorized', provider: {ok: true, since: null, error: null}, values: {}, bases: {},
    switch: {value: active ? 'true' : 'false', source: 'default', from: null, base: 'default', baseValue: active}, secretNote: null,
    secret: {set: active, from: null, unreadable: false}, roleMapInUse: [], mcpBase: 'https://skopax.example.com'};
}

async function boot(opts) {
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state));
  let users = makeUsers();
  const answer = {post: null};
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=anoms', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, o) => {
        const p = String(path), method = (o && o.method) || 'GET';
        const body = o && o.body ? JSON.parse(o.body) : null;
        const ok = (b, status) => ({ok: true, status: status || 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        const bad = (status, b) => ({ok: false, status, json: async () => b});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, sso: opts.sso, user: 'alice@example.com', userRole: 'admin', source: 'local', canChangePassword: true, operator: true, admin: true, teams: [], tags: []});
        if (p.startsWith('/api/admin')) {
          calls.push([method, p, body]);
          if (p === '/api/admin/sso') return ok(ssoView(opts.sso));
          if (p === '/api/admin/users' && method === 'GET') return ok(users);
          if (p === '/api/admin/users' && method === 'POST') {
            if (answer.post) return bad(answer.post.status, answer.post.body);
            const made = Object.assign({email: body.email, name: body.name || body.email.split('@')[0], role: body.role, source: body.password ? 'local' : 'sso', disabled: false, createdAt: iso(0), lastLogin: null, ssoLink: null},
              body.password ? {} : {opened: false});
            users = users.concat([made]);
            return ok(made, 201);
          }
          if (p.startsWith('/api/admin/users/') && method === 'PATCH') {
            const email = decodeURIComponent(p.split('/').pop());
            const change = body.ssoOnly ? {source: 'sso', ssoLink: null, roleFrom: 'admin'} : {source: 'local', ssoLink: {state: 'pending', boundAt: null, allowedAt: iso(0), expiresAt: iso(-7 * 1440)}};
            users = users.map((u) => u.email === email ? Object.assign({}, u, change) : u);
            return ok(users.find((u) => u.email === email));
          }
          if (p === '/api/admin/tokens') return ok([]);
          if (p.startsWith('/api/admin/settings')) return ok({items: [], rev: 1, refreshSeconds: 15});
          if (p.startsWith('/api/admin/notices')) return ok({items: []});
          return ok([]);
        }
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/')) return ok([]);
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  const doc = dom.window.document;
  await until(() => dom.window.ADMIN_OK === true, 'the admin role');
  doc.getElementById('tab-access').click();
  await until(() => doc.querySelector('#access .acc-add-open'), 'the Access tab');
  return {w: dom.window, doc, errors, calls, answer, close: () => dom.window.close()};
}

const form = (t) => t.doc.querySelector('#access .acc-add-user');
const fieldOf = (t, key) => form(t).querySelector('.int-field[data-key="' + key + '"]');
const errorOf = (t, key) => fieldOf(t, key).querySelector('.acc-ferr');
const shown = (el) => !!el && !el.hidden && el.textContent !== '';
const posts = (t) => t.calls.filter((c) => c[0] === 'POST' && c[1] === '/api/admin/users');
const rowOf = (t, email) => [...t.doc.querySelectorAll('#access .acc-user')].filter((r) => r.dataset.email === email)[0];
const type = (el, v) => { el.value = v; el.dispatchEvent(new el.ownerDocument.defaultView.Event('input', {bubbles: true})); };
const key = (t, el, k) => el.dispatchEvent(new t.w.KeyboardEvent('keydown', {key: k, bubbles: true, cancelable: true}));
async function open(t) {
  t.doc.querySelector('#access .acc-add-open').click();
  return until(() => form(t), 'the add-user form');
}
async function submit(t) {
  const before = posts(t).length;
  form(t).querySelector('button[type=submit]').click();
  await until(() => !form(t) || form(t).dataset.busy !== '1', 'the submit to settle');
  return posts(t).length - before;
}

async function theSwitchShowsAndStartsOnOnlyWithSsoOn() {
  //given
  const on = await boot({sso: true});
  const off = await boot({sso: false});

  //when
  await open(on);
  await open(off);
  const sw = fieldOf(on, 'sso').querySelector('[role=switch]');

  //then
  assert.strictEqual(sw.getAttribute('aria-checked'), 'true', 'the switch starts on when SSO is on');
  assert.strictEqual(fieldOf(on, 'sso').querySelector('.adm-help').textContent, SSO_HELP);
  assert.strictEqual(fieldOf(on, 'password').querySelector('.adm-help').textContent, OPTIONAL);
  assert.ok(!fieldOf(on, 'password').querySelector('.int-req'), 'no required mark while the switch is on');
  assert.ok(!fieldOf(off, 'sso'), 'no switch while SSO is not on');
  assert.ok(fieldOf(off, 'password').querySelector('.int-req'), 'the password is marked required');
  assert.strictEqual(on.doc.activeElement, fieldOf(on, 'email').querySelector('input'), 'opening focuses the email');
  assert.deepStrictEqual(on.errors.concat(off.errors), []);
  on.close();
  off.close();
  console.log('PASS the SSO switch shows and starts on only when SSO is on, and the password help follows it');
}

async function thePasswordIsOptionalOnlyWithTheSwitchOn() {
  //given
  const t = await boot({sso: true});
  await open(t);
  type(fieldOf(t, 'email').querySelector('input'), ' erin@example.com ');

  //when
  fieldOf(t, 'sso').querySelector('[role=switch]').click();
  const helpOff = fieldOf(t, 'password').querySelector('.adm-help').textContent;
  const sentOff = await submit(t);
  const pwError = errorOf(t, 'password').textContent;
  const focused = t.doc.activeElement === fieldOf(t, 'password').querySelector('input');
  fieldOf(t, 'sso').querySelector('[role=switch]').click();
  const sentOn = await submit(t);

  //then
  assert.strictEqual(sentOff, 0, 'no request while a required password is missing');
  assert.strictEqual(pwError, NEED_PASSWORD);
  assert.ok(focused, 'the password field gets the focus');
  assert.ok(/12-256/.test(helpOff) && helpOff !== OPTIONAL);
  assert.strictEqual(sentOn, 1);
  assert.deepStrictEqual(posts(t)[0][2], {email: 'erin@example.com', name: '', role: 'viewer', sso: true});
  await until(() => !form(t) && t.doc.querySelector('#access .acc-users-msg').textContent === 'Person added', 'the added note');
  assert.ok(rowOf(t, 'erin@example.com'), 'the new user is listed');
  assert.strictEqual(t.doc.activeElement, t.doc.querySelector('#access .acc-add-open'), 'focus goes back to Add user');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  console.log('PASS the password is optional only while the switch is on, and an SSO-only add sends no password');
}

async function emptyFieldsGetInlineMessagesAndTheFirstBadOneIsFocused() {
  //given
  const t = await boot({sso: false});
  await open(t);

  //when
  const sent = await submit(t);
  const email = fieldOf(t, 'email').querySelector('input'), pw = fieldOf(t, 'password').querySelector('input');
  const first = [errorOf(t, 'email').textContent, errorOf(t, 'password').textContent, email.getAttribute('aria-invalid'), pw.getAttribute('aria-invalid'), t.doc.activeElement === email];
  type(email, 'not-an-email');
  const cleared = [shown(errorOf(t, 'email')), email.getAttribute('aria-invalid')];
  type(pw, 'short');
  await submit(t);
  const second = [errorOf(t, 'email').textContent, errorOf(t, 'password').textContent];

  //then
  assert.strictEqual(sent, 0);
  assert.deepStrictEqual(first, ['Enter an email', NEED_PASSWORD, 'true', 'true', true]);
  assert.strictEqual(email.getAttribute('aria-describedby').split(' ').indexOf(errorOf(t, 'email').id) >= 0, true, 'the message is tied to the field');
  assert.deepStrictEqual(cleared, [false, null], 'typing clears the message of that field');
  assert.deepStrictEqual(second, ['Enter a valid email address', NEED_PASSWORD]);
  assert.strictEqual(posts(t).length, 0);
  assert.strictEqual(form(t).querySelector('.adm-msg').textContent, '', 'no form message for field problems');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  console.log('PASS empty and bad fields get one inline message each, aria-invalid and focus, before any request');
}

async function aServerRefusalLandsOnItsField() {
  //given
  const t = await boot({sso: true});
  await open(t);
  type(fieldOf(t, 'email').querySelector('input'), 'dana@example.com');
  type(fieldOf(t, 'password').querySelector('input'), 'a-long-enough-password');

  //when
  t.answer.post = {status: 422, body: {detail: 'A password is 12-256 characters.', field: 'password'}};
  await submit(t);
  const onPassword = [errorOf(t, 'password').textContent, fieldOf(t, 'password').querySelector('input').getAttribute('aria-invalid'), t.doc.activeElement === fieldOf(t, 'password').querySelector('input')];
  t.answer.post = {status: 422, body: {detail: 'Single sign-on is not set up and on, so the account needs a password.', field: 'sso'}};
  await submit(t);
  const onSso = errorOf(t, 'sso').textContent;
  t.answer.post = {status: 409, body: {detail: 'A user with this email already exists.'}};
  await submit(t);
  const onForm = form(t).querySelector('.adm-msg').textContent;
  t.answer.post = {status: 422, body: {detail: [{loc: ['body', 'role'], msg: 'Input should be a valid string', type: 'string_type'}]}};
  await submit(t);
  const listed = form(t).querySelector('.adm-msg').textContent;

  //then
  assert.deepStrictEqual(onPassword, ['A password is 12-256 characters.', 'true', true]);
  assert.strictEqual(onSso, 'Single sign-on is not set up and on, so the account needs a password.');
  assert.strictEqual(onForm, 'A user with this email already exists.');
  assert.strictEqual(listed, 'Not added, try again', 'a validation list is never shown as it came');
  assert.ok(!/Input should|string_type|loc/.test(form(t).textContent));
  assert.deepStrictEqual(t.errors, []);
  t.close();
  console.log('PASS a 422 lands under the field it names, others go to the form message, and a raw list is never shown');
}

async function theSwitchAndTheSegmentsWorkFromTheKeyboard() {
  //given
  const t = await boot({sso: true});
  await open(t);
  const sw = fieldOf(t, 'sso').querySelector('[role=switch]');
  const seg = fieldOf(t, 'role').querySelector('[role=radiogroup]');
  const bs = [...seg.querySelectorAll('[role=radio]')];
  const role = () => form(t).querySelector('[name=role]').value;
  const checked = () => bs.map((b) => b.getAttribute('aria-checked'));

  //when
  key(t, sw, ' ');
  const afterSpace = sw.getAttribute('aria-checked');
  key(t, sw, 'Enter');
  const afterEnter = sw.getAttribute('aria-checked');
  bs[0].focus();
  key(t, bs[0], 'ArrowRight');
  const right = [role(), checked(), t.doc.activeElement === bs[1], bs.map((b) => b.tabIndex)];
  key(t, bs[1], 'End');
  const end = role();
  key(t, bs[2], 'ArrowRight');
  const wrap = role();
  key(t, bs[0], 'ArrowLeft');
  const left = role();
  key(t, bs[2], 'Home');
  const home = role();
  const eye = fieldOf(t, 'password').querySelector('.int-eye');
  eye.click();
  const showing = [fieldOf(t, 'password').querySelector('input').type, eye.textContent, eye.getAttribute('aria-pressed')];
  eye.click();

  //then
  assert.deepStrictEqual(bs.map((b) => b.textContent), ['Viewer', 'Operator', 'Admin']);
  assert.deepStrictEqual([afterSpace, afterEnter], ['false', 'true']);
  assert.deepStrictEqual(right, ['operator', ['false', 'true', 'false'], true, [-1, 0, -1]]);
  assert.deepStrictEqual([end, wrap, left, home], ['admin', 'viewer', 'admin', 'viewer']);
  assert.deepStrictEqual(showing, ['text', 'Hide', 'true']);
  assert.strictEqual(fieldOf(t, 'password').querySelector('input').type, 'password');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  console.log('PASS the switch toggles on Space and Enter, the role segments move with the arrows, Home and End, and Show/Hide flips the password');
}

async function theListShowsSsoOnlyAccountsAndCancelGivesFocusBack() {
  //given
  const t = await boot({sso: true});
  const dana = rowOf(t, 'dana@example.com'), bob = rowOf(t, 'bob@example.com');
  const opened = ['bob@example.com', 'old@example.com', 'mo@example.com'].map((e) => rowOf(t, e)).map((r) => [!!r.querySelector('.acc-not-yet'), !!r.querySelector('.acc-setpw'), !!r.querySelector('.acc-reset')]);

  //when
  const rows = [dana.querySelector('.acc-sso-only').textContent, dana.querySelector('.acc-not-yet').textContent, !!dana.querySelector('.acc-setpw'),
    bob.querySelector('.acc-sso-only').textContent, !!bob.querySelector('.acc-not-yet'), dana.querySelector('.acc-setpw').textContent, !!rowOf(t, 'alice@example.com').querySelector('.acc-sso-only')];
  dana.querySelector('.acc-setpw').click();
  type(dana.querySelector('input.acc-pw'), 'a-long-enough-password');
  dana.querySelector('.acc-pw-save').click();
  await until(() => t.calls.some((c) => c[0] === 'PATCH'), 'the password patch');
  await open(t);
  form(t).querySelector('.acc-add-cancel').click();

  //then
  assert.deepStrictEqual(rows, ['SSO only', 'Not signed in yet', true, 'SSO only', false, 'Set a password', false]);
  assert.deepStrictEqual(opened, [[false, true, false], [false, true, false], [false, true, false]], 'every SSO-only person can be given a password again');
  assert.deepStrictEqual(t.calls.filter((c) => c[0] === 'PATCH')[0], ['PATCH', '/api/admin/users/dana%40example.com', {password: 'a-long-enough-password'}]);
  assert.ok(!form(t), 'Cancel closes the form');
  assert.strictEqual(t.doc.activeElement, t.doc.querySelector('#access .acc-add-open'), 'focus goes back to Add user');
  assert.strictEqual(t.doc.querySelector('#access .acc-add-open').getAttribute('aria-expanded'), 'false');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  console.log('PASS the list marks SSO-only accounts, offers Set a password to every one of them, and Cancel gives focus back');
}

async function theOpenCardFollowsSsoStateThatArrivesLater() {
  //given
  const opts = {sso: false};
  const t = await boot(opts);
  await open(t);
  type(form(t).querySelector('[name=email]'), 'erin@example.com');
  const pw = form(t).querySelector('[name=password]');
  const before = [!!fieldOf(t, 'sso'), !!fieldOf(t, 'password').querySelector('.int-req'), pw.getAttribute('aria-required')];

  //when
  opts.sso = true;
  await t.w.loadAccess();
  await until(() => fieldOf(t, 'sso'), 'the SSO switch');
  const on = [fieldOf(t, 'sso').querySelector('[role=switch]').getAttribute('aria-checked'), fieldOf(t, 'password').querySelector('.adm-help').textContent,
    !!fieldOf(t, 'password').querySelector('.int-req'), form(t).querySelector('[name=password]').getAttribute('aria-required'), form(t).querySelector('[name=email]').value];
  opts.sso = false;
  await t.w.loadAccess();
  await until(() => !fieldOf(t, 'sso'), 'the switch to go');
  const off = [fieldOf(t, 'password').querySelector('.adm-help').textContent, !!fieldOf(t, 'password').querySelector('.int-req'), form(t).querySelector('[name=password]').getAttribute('aria-required'),
    form(t).querySelector('[name=email]').value];

  //then
  assert.deepStrictEqual(before, [false, true, 'true']);
  assert.deepStrictEqual(on, ['true', OPTIONAL, false, null, 'erin@example.com'], 'the switch and the password help repaint, and what was typed stays');
  assert.deepStrictEqual(off, ['12-256 characters', true, 'true', 'erin@example.com']);
  assert.strictEqual(t.doc.querySelector('#access .acc-add-open').getAttribute('aria-controls'), form(t).id, 'Add user names the card it opens');
  assert.ok(form(t).id, 'the card has an id');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  console.log('PASS an open add-user card repaints the SSO switch and the password help when SSO state arrives, keeps what was typed, and the opener names it');
}

async function makeSsoOnlyShowsOnlyForPasswordPeopleWhileSsoIsOn() {
  //given
  const on = await boot({sso: true});
  const off = await boot({sso: false});

  //when
  const has = (t, e) => !!rowOf(t, e).querySelector('.acc-mi-ssoonly');
  const seen = [has(on, 'carl@example.com'), has(on, 'alice@example.com'), has(on, 'bob@example.com'), has(on, 'dana@example.com'), has(off, 'carl@example.com'), has(off, 'alice@example.com')];
  const item = rowOf(on, 'carl@example.com').querySelector('.acc-mi-ssoonly');

  //then
  assert.deepStrictEqual(seen, [true, true, false, false, false, false]);
  assert.deepStrictEqual([item.textContent, item.title], ['Make SSO only', 'Removes the password; this person signs in with SSO only']);
  assert.deepStrictEqual(on.errors.concat(off.errors), []);
  on.close();
  off.close();
  console.log('PASS Make SSO only shows only for password people and only while SSO is on');
}

async function makeSsoOnlyConfirmsInTheRowAndWarnsOnYourOwnRow() {
  //given
  const t = await boot({sso: true});
  const patches = () => t.calls.filter((c) => c[0] === 'PATCH');
  const carl = rowOf(t, 'carl@example.com'), alice = rowOf(t, 'alice@example.com');

  //when
  carl.querySelector('.acc-mi-ssoonly').click();
  const ask = carl.querySelector('.acc-ssoonly-form');
  const asked = [!ask.hidden, ask.querySelector('.acc-ssoonly-q').textContent, !!ask.querySelector('.acc-ssoonly-self'), patches().length, t.doc.activeElement === ask.querySelector('.acc-ssoonly-go')];
  alice.querySelector('.acc-mi-ssoonly').click();
  const own = alice.querySelector('.acc-ssoonly-form');
  const warned = [!own.hidden, own.querySelector('.acc-ssoonly-self').textContent];
  own.querySelector('.acc-ssoonly-cancel').click();
  const cancelled = [own.hidden, patches().length];
  ask.querySelector('.acc-ssoonly-go').click();
  await until(() => patches().length === 1 && rowOf(t, 'carl@example.com').querySelector('.acc-sso-only'), 'the SSO-only patch');
  const after = rowOf(t, 'carl@example.com');

  //then
  assert.deepStrictEqual(asked, [true, 'Remove the password? This person then signs in with SSO only.', false, 0, true]);
  assert.deepStrictEqual(warned, [true, 'You will be signed out if you signed in with your password']);
  assert.deepStrictEqual(cancelled, [true, 0]);
  assert.deepStrictEqual(patches()[0], ['PATCH', '/api/admin/users/carl%40example.com', {ssoOnly: true}]);
  assert.ok(!after.querySelector('.acc-mi-ssoonly') && after.querySelector('.acc-setpw'), 'the row now offers Set a password instead');
  assert.strictEqual(after.querySelector('.adm-msg').textContent, 'Signs in with SSO only now');
  assert.deepStrictEqual(t.errors, []);
  t.close();
  console.log('PASS Make SSO only asks in the row, warns on your own row, and Cancel sends nothing');
}

(async () => {
  await makeSsoOnlyShowsOnlyForPasswordPeopleWhileSsoIsOn();
  await makeSsoOnlyConfirmsInTheRowAndWarnsOnYourOwnRow();
  await theOpenCardFollowsSsoStateThatArrivesLater();
  await theSwitchShowsAndStartsOnOnlyWithSsoOn();
  await thePasswordIsOptionalOnlyWithTheSwitchOn();
  await emptyFieldsGetInlineMessagesAndTheFirstBadOneIsFocused();
  await aServerRefusalLandsOnItsField();
  await theSwitchAndTheSegmentsWorkFromTheKeyboard();
  await theListShowsSsoOnlyAccountsAndCancelGivesFocusBack();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
