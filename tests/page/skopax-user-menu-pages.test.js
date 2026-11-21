require('./focus');
const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const NOTICES = [{id: 'a', level: 'warn', text: 'First', at: iso(5)}, {id: 'b', level: 'warn', text: 'Second', at: iso(4)}, {id: 'c', level: 'info', text: 'Third'}];

async function boot(me, hash, notices) {
  const errors = [];
  const vc = new VirtualConsole(); vc.on('jsdomError', (e) => errors.push(String(e.message || e).slice(0, 120)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3e6).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const dom = new JSDOM(pageHtml(process.argv[2]).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {url: 'https://skopax.example.com/' + (hash || '#t=anoms'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {}; w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me')) return ok(me);
        if (p.startsWith('/api/admin/notices')) return me.admin ? ok({items: notices || []}) : {ok: false, status: 403, json: async () => ({detail: 'no'})};
        if (p.startsWith('/api/admin/settings')) return me.admin ? ok({items: [], rev: 1, refreshSeconds: 15}) : {ok: false, status: 403, json: async () => ({detail: 'no'})};
        if (p.startsWith('/api/admin')) return me.admin ? ok([]) : {ok: false, status: 403, json: async () => ({detail: 'no'})};
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(300);
  return {w: dom.window, doc: dom.window.document, errors};
}
const signed = (role) => ({loginEnabled: true, user: 'alice@example.com', name: 'Alice', userRole: role, operator: role !== 'viewer', admin: role === 'admin', teams: [], tags: [], canChangePassword: true});
const menu = (doc) => [...doc.querySelectorAll('#user-menu [role="menuitem"]')].filter((b) => !b.hidden).map((b) => b.textContent.trim());
const rowShown = (doc, id) => !!doc.getElementById(id) && !doc.getElementById(id).hidden;

(async () => {
  //given
  const admin = await boot(signed('admin'), '#t=anoms', NOTICES);
  const op = await boot(signed('operator'));
  const guest = await boot({loginEnabled: true, user: null, operator: false, admin: false, teams: [], tags: []});
  const off = await boot({loginEnabled: false, operator: true, admin: true, userRole: 'admin', teams: [], tags: []}, '#t=anoms', NOTICES);
  const link = await boot(signed('admin'), '#t=prefs', NOTICES);
  const adminTab = await boot(signed('admin'), '#t=access', NOTICES);

  //when
  const btn = admin.doc.getElementById('user-btn');
  btn.click();
  const focusedFirst = admin.doc.activeElement.textContent.trim();
  admin.w.document.getElementById('um-admin').click();
  await wait(100);

  //then
  assert.deepStrictEqual(menu(admin.doc).slice(0, 3), ['Preferences', 'Admin2', 'Change password'], 'Preferences and Admin lead the menu, Admin with its warning count');
  assert.deepStrictEqual([rowShown(admin.doc, 'tab-prefs'), rowShown(admin.doc, 'side-admin')], [false, false], 'no Preferences or Admin row for a signed-in admin');
  assert.strictEqual(focusedFirst, 'Preferences');
  assert.ok(!btn.querySelector('.u-dot').hidden && btn.querySelector('.u-dot').textContent === '2' && btn.querySelector('.u-dot').getAttribute('aria-label') === '2 warnings', 'the warning count shows on the user button');
  assert.ok(!admin.doc.getElementById('adm-shell').hidden && btn.getAttribute('aria-current') === 'page', 'Admin opens its remembered tab and the user button is current');
  assert.deepStrictEqual(menu(op.doc), ['Preferences', 'Change password', 'Sign out'], 'a non-admin sees only Preferences');
  assert.deepStrictEqual([rowShown(op.doc, 'tab-prefs'), rowShown(op.doc, 'side-admin')], [false, false]);
  assert.ok(!op.doc.getElementById('user-btn').querySelector('.u-dot') || op.doc.getElementById('user-btn').querySelector('.u-dot').hidden);
  assert.ok(rowShown(guest.doc, 'tab-prefs') && !guest.doc.getElementById('user-btn'), 'a read-only viewer keeps the Preferences row');
  assert.ok(!link.doc.getElementById('panel-prefs').hidden && link.doc.getElementById('user-btn').getAttribute('aria-current') === 'page', 'a link to Preferences opens it and marks the user button');
  assert.ok(!adminTab.doc.getElementById('panel-access').hidden, 'a link to an admin tab opens it');
  assert.ok(rowShown(off.doc, 'tab-prefs') && rowShown(off.doc, 'side-admin') && !off.doc.getElementById('user-btn'), 'a login-off install keeps the Preferences and Admin rows, its only way in');
  [admin, op, guest, off, link, adminTab].forEach((x) => { assert.deepStrictEqual(x.errors, []); x.w.close(); });
  console.log('PASS Preferences and Admin live in the user menu for a signed-in person, with the Admin warning count on the button, and a read-only viewer keeps the Preferences row');
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
