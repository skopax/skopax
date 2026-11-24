require('./focus');
const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const EVIL = '<img src=x onerror=alert(1)>"\'';
const rendered = (doc) => { const body = doc.body.cloneNode(true); body.querySelectorAll('script, style').forEach((n) => n.remove()); return body.textContent; };

function media(width) {
  return (q) => {
    const m = /\((min|max)-width:\s*(\d+)px\)/.exec(String(q));
    const matches = !!m && (m[1] === 'min' ? width >= Number(m[2]) : width <= Number(m[2]));
    return {matches, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}};
  };
}

async function boot(me, opts) {
  opts = opts || {};
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com' + (opts.path || '/') + '#t=anoms', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.localStorage.setItem('skopax-sidebar', opts.rail ? 'rail' : 'full');
      if (opts.width) w.matchMedia = media(opts.width);
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, o) => {
        const p = String(path), method = (o && o.method) || 'GET';
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        const bad = (status, detail) => ({ok: false, status, json: async () => ({detail})});
        if (p.startsWith('/api/me') && !p.startsWith('/api/me/password')) return ok(me);
        if (p === '/api/me/password') {
          calls.push([method, p, JSON.parse(o.body), o.credentials]);
          const next = opts.password || (() => ({status: 204}));
          const r = next(JSON.parse(o.body));
          if (r.status === 204) return {ok: true, status: 204, json: async () => { throw new Error('no body'); }};
          return bad(r.status, r.detail);
        }
        if (p === '/logout') { calls.push([method, p, null, o && o.credentials]); return opts.logoutFails ? bad(500, 'x') : ok({}); }
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(200);
  return {w: dom.window, doc: dom.window.document, errors, calls, close: () => dom.window.close()};
}
const base = {loginEnabled: true, sso: false, user: 'alice.archer@example.com', name: 'Alice A. Archer', userRole: 'admin', source: 'local', canChangePassword: true, operator: true, admin: true, teams: [], tags: []};
const setValue = (el, v) => { el.value = v; el.dispatchEvent(new el.ownerDocument.defaultView.Event('input', {bubbles: true})); };
const items = (doc) => [...doc.querySelectorAll('#user-menu [role="menuitem"]')];
const item = (doc, text) => items(doc).filter((b) => b.textContent.trim() === text)[0];
const key = (t, el, k) => el.dispatchEvent(new t.w.KeyboardEvent('keydown', {key: k, bubbles: true, cancelable: true}));
const open = (t) => { const b = t.doc.getElementById('user-btn'); if (t.doc.getElementById('user-menu').hidden) b.click(); };
const choose = (t, text) => { open(t); item(t.doc, text).click(); };

async function fillAndSend(t, cur, next, again) {
  const d = t.doc.querySelector('#pw-dialog');
  setValue(d.querySelector('[name=current]'), cur);
  setValue(d.querySelector('[name=new]'), next);
  setValue(d.querySelector('[name=again]'), again === undefined ? next : again);
  d.querySelector('button[type=submit]').click();
  await wait(80);
}

(async () => {
  //given
  const t = await boot(base);
  const acc = t.doc.getElementById('account');
  const btn = t.doc.getElementById('user-btn');

  //then
  assert.ok(!acc.hidden && acc.closest('#side .s-bot'), 'the account sits in the sidebar foot');
  assert.ok(btn.previousElementSibling === null && acc.previousElementSibling.id === 'pinned' && acc.nextElementSibling.id === 'side-attr', 'between the pinned rows and the attribution');
  assert.deepStrictEqual(['aria-haspopup', 'aria-expanded', 'aria-controls'].map((a) => btn.getAttribute(a)), ['menu', 'false', 'user-menu']);
  assert.strictEqual(btn.querySelector('.av').textContent, 'A', 'the avatar is the first letter of the name');
  assert.strictEqual(btn.querySelector('.av').getAttribute('aria-hidden'), 'true');
  assert.deepStrictEqual([btn.querySelector('.user-t b').textContent, btn.querySelector('.user-t span').textContent], ['Alice A. Archer', 'admin'], 'the stored name and the user role');
  assert.ok(!/SKOPAX_(OPERATOR|ADMIN)/.test(acc.outerHTML), 'the identity provider role name is not shown');
  const menu = t.doc.getElementById('user-menu');
  assert.ok(menu.hidden && menu.getAttribute('role') === 'menu' && menu.getAttribute('aria-label') === 'Account');
  console.log('PASS the user button in the sidebar foot shows the avatar letter, the stored name and the role, its menu closed');

  //given
  const unnamed = await boot(Object.assign({}, base, {name: null}));
  const hostile = await boot(Object.assign({}, base, {name: EVIL}));

  //then
  assert.strictEqual(unnamed.doc.querySelector('#user-btn .user-t b').textContent, 'alice.archer@example.com', 'without a stored name the button shows the email');
  assert.strictEqual(unnamed.doc.querySelector('#user-btn .av').textContent, 'A');
  assert.strictEqual(hostile.doc.querySelector('#user-btn .user-t b').textContent, EVIL, 'a hostile name is shown as text');
  assert.strictEqual(hostile.doc.querySelector('#user-btn .av').textContent, '<');
  open(hostile);
  assert.strictEqual(hostile.doc.querySelectorAll('#account img').length, 0);
  assert.deepStrictEqual(unnamed.errors.concat(hostile.errors), []);
  unnamed.close(); hostile.close();
  console.log('PASS without a stored name the button shows the email, and a hostile name stays text');

  //then
  const viewer = await boot(Object.assign({}, base, {userRole: 'viewer', operator: false, admin: false}));
  assert.strictEqual(viewer.doc.querySelector('#user-btn .user-t span').textContent, 'viewer');
  assert.ok(/operator role/.test(viewer.doc.getElementById('user-btn').title), 'the hint names the local role: ' + viewer.doc.getElementById('user-btn').title);
  assert.ok(!/SKOPAX_(OPERATOR|ADMIN)/.test(viewer.doc.getElementById('account').outerHTML + rendered(viewer.doc)), 'no environment variable name is shown to the viewer');
  viewer.close();
  console.log('PASS the needs-a-role text names the local role, not the identity provider role');

  //when
  btn.click();

  //then
  assert.ok(!menu.hidden && btn.getAttribute('aria-expanded') === 'true', 'a click opens the menu');
  assert.deepStrictEqual([menu.querySelector('.menu-h').textContent, menu.querySelector('.menu-h').getAttribute('role')], ['Signed in as alice.archer@example.com \u00b7 admin', 'none']);
  assert.deepStrictEqual(items(t.doc).map((b) => [b.tagName, b.textContent.trim()]), [['BUTTON', 'Preferences'], ['BUTTON', 'Admin'], ['BUTTON', 'Change password'], ['BUTTON', 'Sign out']], 'Settings and Admin come first');
  assert.ok(items(t.doc).every((b) => b.querySelector('svg.i[aria-hidden="true"]')), 'each item has its icon');
  assert.strictEqual(t.doc.activeElement, items(t.doc)[0], 'the first item takes the focus');

  //when
  const moves = [];
  for (const k of ['ArrowDown', 'ArrowDown', 'ArrowUp', 'End', 'Home']) { key(t, t.doc.activeElement, k); moves.push(t.doc.activeElement.textContent.trim()); }
  key(t, t.doc.activeElement, 'Escape');

  //then
  assert.deepStrictEqual(moves, ['Admin', 'Change password', 'Admin', 'Sign out', 'Preferences'], 'the arrows move and wrap, Home and End jump');
  assert.ok(menu.hidden && t.doc.activeElement === btn && btn.getAttribute('aria-expanded') === 'false', 'Escape closes the menu and gives the focus back to the button');

  //when
  key(t, btn, 'ArrowDown');
  const byKey = !menu.hidden;
  key(t, t.doc.activeElement, 'Tab');
  const tabbed = menu.hidden;
  btn.click();
  t.doc.body.click();

  //then
  assert.deepStrictEqual([byKey, tabbed, menu.hidden], [true, true, true], 'ArrowDown on the button opens it, Tab and a click outside close it');

  //given
  const sso = await boot(Object.assign({}, base, {source: 'sso', canChangePassword: false}));
  open(sso);

  //then
  assert.deepStrictEqual(items(sso.doc).map((b) => b.textContent.trim()), ['Preferences', 'Admin', 'Sign out'], 'an SSO user has no Change password');
  sso.close();
  console.log('PASS the menu says who is signed in, offers Change password to local users and Sign out, and follows the keyboard');

  //given
  const guest = await boot({loginEnabled: true, sso: false, user: null, name: null, userRole: null, source: null, canChangePassword: false, operator: false, admin: false, teams: [], tags: []}, {path: '/?view=board'});
  const off = await boot({loginEnabled: false, user: null, operator: true, admin: true, teams: [], tags: []});

  //then
  const login = guest.doc.querySelector('#account a.login-row');
  assert.ok(login && login.classList.contains('tab'), 'a Log in row');
  assert.strictEqual(login.querySelector('.lbl').textContent, 'Log in');
  assert.strictEqual(login.getAttribute('href'), '/login?next=' + encodeURIComponent('/?view=board'));
  assert.ok(!/SKOPAX_(OPERATOR|ADMIN)/.test(login.title));
  assert.strictEqual(guest.doc.getElementById('user-btn'), null, 'and no user button');
  assert.ok(off.doc.getElementById('account').hidden, 'no account box when the install has no login');
  assert.deepStrictEqual(guest.errors.concat(off.errors), []);
  guest.close(); off.close();
  console.log('PASS signed out with public read on, the foot shows a Log in row with next');

  //given
  const leaving = await boot(base, {path: '/login'});

  //when
  choose(leaving, 'Sign out');
  await wait(60);

  //then
  assert.deepStrictEqual(leaving.calls.filter((c) => c[1] === '/logout').map((c) => [c[0], c[3]]), [['POST', 'same-origin']]);
  assert.strictEqual(leaving.w.location.href, 'https://skopax.example.com/login', 'after the POST the page goes to the login page (served from /login here, so jsdom can follow the move)');
  assert.deepStrictEqual(leaving.errors, []);
  leaving.close();
  console.log('PASS Sign out is a same-origin POST and then goes to the login page');

  //when
  choose(t, 'Change password');
  const dlg = t.doc.querySelector('#pw-dialog');

  //then
  assert.ok(menu.hidden, 'picking an item closes the menu');
  assert.ok(dlg && !dlg.hidden, 'the dialog opens');
  assert.strictEqual(dlg.getAttribute('role'), 'dialog');
  assert.ok(['current', 'new', 'again'].every((n) => dlg.querySelector('[name=' + n + '][type=password]')));

  //when
  await fillAndSend(t, 'old-password-123', 'new-password-456', 'different-password');

  //then
  assert.strictEqual(t.calls.filter((c) => c[1] === '/api/me/password').length, 0, 'a mismatch is not sent');
  assert.ok(/do not match/.test(t.doc.querySelector('#pw-dialog .pw-msg').textContent));

  //when
  await fillAndSend(t, 'old-password-123', 'new-password-456');

  //then
  const sent = t.calls.filter((c) => c[1] === '/api/me/password');
  assert.deepStrictEqual([sent[0][0], sent[0][2], sent[0][3]], ['POST', {current: 'old-password-123', new: 'new-password-456'}, 'same-origin']);
  assert.strictEqual(t.doc.querySelector('#pw-dialog'), null, 'the dialog is removed on 204');
  assert.ok([...t.doc.querySelectorAll('input')].every((i) => !/password-(123|456)/.test(i.value) && !/password-(123|456)/.test(i.getAttribute('value') || '')), 'typed passwords do not stay in the DOM');
  assert.ok(/Password changed/.test(t.doc.getElementById('account').textContent), 'a success message shows');
  assert.strictEqual(t.doc.activeElement, btn, 'the focus goes back to the user button');
  console.log('PASS Change password from the menu posts current and new, and on 204 shows a message and closes');

  //then
  for (const [status, detail] of [[400, 'The password could not be changed.'], [429, 'Too many attempts. Try again later.'], [503, 'Login is unavailable right now.'], [400, EVIL]]) {
    const e = await boot(base, {password: () => ({status, detail})});
    choose(e, 'Change password');
    await fillAndSend(e, 'old-password-123', 'new-password-456');
    const dd = e.doc.querySelector('#pw-dialog');
    assert.ok(dd && !dd.hidden, 'the dialog stays open on ' + status);
    assert.strictEqual(dd.querySelector('.pw-msg').textContent, detail, status + ' detail shows inline as text');
    assert.strictEqual(e.doc.querySelectorAll('#pw-dialog img').length, 0);
    assert.ok(!dd.querySelector('button[type=submit]').disabled, 'it can be tried again');
    e.close();
  }
  console.log('PASS 400, 429 and 503 show the server text inline, escaped, and keep the dialog open');

  //when
  choose(t, 'Change password');
  setValue(t.doc.querySelector('#pw-dialog [name=current]'), 'typed-secret-value');
  t.doc.querySelector('#pw-dialog .pw-cancel').click();
  assert.ok(![...t.doc.querySelectorAll('input')].some((i) => i.value === 'typed-secret-value'), 'a cancelled dialog leaves no typed password');

  //then
  assert.strictEqual(t.doc.querySelector('#pw-dialog'), null, 'Cancel removes the dialog');
  assert.strictEqual(t.doc.activeElement, btn);
  assert.deepStrictEqual(t.errors, []);
  t.close();
  console.log('PASS Cancel closes the dialog and leaves no typed password in the DOM');

  //given
  const failing = await boot(base, {logoutFails: true});

  //when
  choose(failing, 'Sign out');
  await wait(80);

  //then
  const note = failing.doc.querySelector('#account .acct-note');
  assert.ok(/Could not sign out/.test(note.textContent) && note.classList.contains('bad'), 'a failed sign out looks like an error');
  assert.strictEqual(failing.w.location.href, 'https://skopax.example.com/#t=anoms', 'a failed sign out stays on the page');
  choose(failing, 'Sign out');
  await wait(80);
  assert.strictEqual(failing.calls.filter((c) => c[1] === '/logout').length, 2, 'and Sign out can be tried again');
  failing.close();
  console.log('PASS a failed sign out shows an error-styled note, stays on the page and can be tried again');

  //given
  const railed = await boot(base, {rail: true, path: '/login'});
  const railGuest = await boot({loginEnabled: true, sso: false, user: null, name: null, userRole: null, source: null, canChangePassword: false, operator: false, admin: false, teams: [], tags: []}, {rail: true, path: '/?view=board'});
  const cs = (x, el) => x.w.getComputedStyle(el);
  const rb = railed.doc.getElementById('user-btn');

  //then
  assert.ok(railed.doc.body.classList.contains('is-rail'), 'the sidebar is the rail');
  assert.ok(cs(railed, railed.doc.getElementById('account')).display !== 'none' && cs(railed, rb).display !== 'none', 'the rail keeps the user button');
  assert.ok(cs(railed, rb.querySelector('.av')).display !== 'none' && cs(railed, rb.querySelector('.av')).position !== 'absolute', 'its avatar shows');
  assert.deepStrictEqual([cs(railed, rb.querySelector('.user-t')).position, cs(railed, rb.querySelector('.user-t')).getPropertyValue('clip-path')], ['absolute', 'inset(50%)'], 'the name and role are only for screen readers');
  assert.strictEqual(rb.title, 'alice.archer@example.com \u00b7 admin: settings, access and actions', 'and the tooltip says who');

  //when
  rb.click();
  const railMenu = railed.doc.getElementById('user-menu');

  //then
  assert.ok(!railMenu.hidden && rb.getAttribute('aria-expanded') === 'true', 'the avatar opens the same account menu');
  assert.strictEqual(cs(railed, railMenu).left, 'calc(100% + 12px)', 'beside the rail');
  assert.deepStrictEqual(items(railed.doc).map((b) => b.textContent.trim()), ['Preferences', 'Admin', 'Change password', 'Sign out']);

  //when
  item(railed.doc, 'Change password').click();

  //then
  assert.ok(railed.doc.getElementById('pw-dialog'), 'Change password works from the rail');
  railed.doc.querySelector('#pw-dialog .pw-cancel').click();

  //when
  choose(railed, 'Sign out');
  await wait(60);

  //then
  assert.deepStrictEqual(railed.calls.filter((c) => c[1] === '/logout').map((c) => c[0]), ['POST'], 'and so does Sign out');
  assert.strictEqual(railed.w.location.href, 'https://skopax.example.com/login');
  const railLogin = railGuest.doc.querySelector('#account a.login-row');
  assert.ok(railLogin && cs(railGuest, railGuest.doc.getElementById('account')).display !== 'none' && cs(railGuest, railLogin).display !== 'none', 'signed out, the rail keeps the Log in row');
  assert.ok(railLogin.querySelector('svg.i') && cs(railGuest, railLogin.querySelector('.lbl')).position === 'absolute', 'as its key icon, the label only for screen readers');
  assert.deepStrictEqual([railLogin.dataset.label, railLogin.getAttribute('href')], ['Log in', '/login?next=' + encodeURIComponent('/?view=board')]);
  assert.deepStrictEqual(railed.errors.concat(railGuest.errors), []);
  railed.close(); railGuest.close();
  console.log('PASS in the rail the avatar opens the same account menu beside the rail, and signed out the Log in row stays');

  //given
  const noted = await boot(base, {rail: true, logoutFails: true});
  const full = await boot(base, {logoutFails: true});

  //when
  choose(noted, 'Sign out');
  choose(full, 'Sign out');
  await wait(80);
  const railNote = noted.doc.querySelector('#account .acct-note'), fullNote = full.doc.querySelector('#account .acct-note');

  //then
  assert.ok(/Could not sign out/.test(railNote.textContent) && railNote.getAttribute('role') === 'status', 'the rail still announces a failed sign out');
  assert.deepStrictEqual(['position', 'left', 'bottom', 'width', 'max-width'].map((k) => cs(noted, railNote).getPropertyValue(k)), ['absolute', 'calc(100% + 12px)', '0px', 'max-content', '220px'], 'in the rail the note floats beside the rail as a tooltip, as wide as its text up to 220 px, out of the sidebar flow');
  assert.ok(/Could not sign out/.test(fullNote.textContent), 'the full sidebar announces it too');
  assert.deepStrictEqual(['position', 'left', 'right', 'bottom'].map((k) => cs(full, fullNote).getPropertyValue(k)), ['absolute', '0px', '0px', 'calc(100% + 6px)'], 'in the full sidebar it floats above the button, so the foot never grows');

  //when
  key(noted, noted.doc.getElementById('user-btn'), 'Escape');
  const byEscape = railNote.textContent;
  choose(noted, 'Sign out');
  await wait(80);
  const again = railNote.textContent;
  choose(noted, 'Change password');
  const byAction = railNote.textContent;
  noted.doc.querySelector('#pw-dialog .pw-cancel').click();
  choose(noted, 'Sign out');
  await wait(80);
  const before = railNote.textContent;
  await wait(5600);
  const early = railNote.textContent;
  await wait(600);
  const late = railNote.textContent;

  //then
  assert.strictEqual(byEscape, '', 'Escape clears the note');
  assert.ok(/Could not sign out/.test(again), 'a second failure shows it again');
  assert.strictEqual(byAction, '', 'the next account action clears it');
  assert.deepStrictEqual([/Could not sign out/.test(before), /Could not sign out/.test(early), late], [true, true, ''], 'and on its own it goes after 6 s');
  assert.ok(cs(noted, railNote).display === 'none', 'an empty note takes no room');
  assert.deepStrictEqual(noted.errors.concat(full.errors), []);
  noted.close(); full.close();
  console.log('PASS the account note floats beside the rail or above the button, and clears after 6 s, on Escape or on the next account action');

  //given
  const phone = await boot(base, {width: 600});
  phone.doc.getElementById('side-open').click();
  const open1 = phone.doc.body.classList.contains('drawer-open');
  open(phone);

  //when
  key(phone, phone.doc.activeElement, 'Escape');
  const afterMenu = {menu: phone.doc.getElementById('user-menu').hidden, focus: phone.doc.activeElement.id, drawer: phone.doc.body.classList.contains('drawer-open')};
  key(phone, phone.doc.activeElement, 'Escape');
  const afterDrawer = {drawer: phone.doc.body.classList.contains('drawer-open'), focus: phone.doc.activeElement.id};

  //then
  assert.ok(open1, 'the phone drawer is open');
  assert.deepStrictEqual(afterMenu, {menu: true, focus: 'user-btn', drawer: true}, 'Escape in the menu closes only the menu and keeps the drawer open');
  assert.deepStrictEqual(afterDrawer, {drawer: false, focus: 'side-open'}, 'the next Escape closes the drawer');
  assert.deepStrictEqual(phone.errors, []);
  phone.close();
  console.log('PASS on a phone, Escape in the account menu closes the menu and leaves the drawer open');
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
