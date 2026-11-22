require('./focus');
const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const ME = {loginEnabled: true, sso: false, user: 'alice@example.com', name: 'Alice', userRole: 'admin', source: 'local', canChangePassword: true, canEditName: true, operator: true, admin: true, teams: [], tags: []};

async function boot() {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/#t=anoms', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me')) return ok(ME);
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(200);
  return {w: dom.window, doc: dom.window.document, errors, close: () => dom.window.close()};
}
const choose = (t, text) => {
  const b = t.doc.getElementById('user-btn');
  if (t.doc.getElementById('user-menu').hidden) b.click();
  [...t.doc.querySelectorAll('#user-menu [role="menuitem"]')].filter((m) => m.textContent.trim() === text)[0].click();
};
const tab = (t, el, shift) => {
  const e = new t.w.KeyboardEvent('keydown', {key: 'Tab', shiftKey: !!shift, bubbles: true, cancelable: true});
  el.dispatchEvent(e);
  return e.defaultPrevented;
};
const focusables = (d) => [...d.querySelectorAll('input, button')].filter((x) => !x.disabled);

(async () => {
  for (const [label, id] of [['Change password', 'pw-dialog'], ['Edit name', 'name-dialog']]) {
    //given
    const t = await boot();
    const btn = t.doc.getElementById('user-btn');
    choose(t, label);
    const d = t.doc.getElementById(id), all = focusables(d), first = all[0], last = all[all.length - 1];

    //when
    last.focus();
    const wrapped = tab(t, last);
    const atFirst = t.doc.activeElement === first;
    const back = tab(t, first, true);
    const atLast = t.doc.activeElement === last;
    all[1].focus();
    const middle = tab(t, all[1]);

    //then
    assert.ok(wrapped && atFirst, label + ': Tab on the last control goes back to the first one inside the dialog');
    assert.ok(back && atLast, label + ': Shift+Tab on the first control goes to the last one');
    assert.strictEqual(middle, false, label + ': Tab between inner controls is left to the browser');

    //when
    t.doc.activeElement.dispatchEvent(new t.w.KeyboardEvent('keydown', {key: 'Escape', bubbles: true, cancelable: true}));

    //then
    assert.strictEqual(t.doc.getElementById(id), null, label + ': Escape closes the dialog');
    assert.strictEqual(t.doc.activeElement, btn, label + ': the focus returns to the account button');
    assert.deepStrictEqual(t.errors, []);
    t.close();
    console.log('PASS ' + label + ' keeps Tab inside the dialog, and Escape closes it and gives the focus back');
  }
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
