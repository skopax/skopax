const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
async function open(saved) {
  const errors = [];
  const vc = new VirtualConsole(); vc.on('jsdomError', (e) => errors.push(String(e.message || e).slice(0, 120)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3e6).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const dom = new JSDOM(pageHtml(process.argv[2]).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {url: 'https://skopax.example.com/', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      if (saved) w.localStorage.setItem('skopax-tab', saved);
      w.scrollTo = () => {}; w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        p = String(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'nina.white@example.com', operator: true, admin: true, teams: [], tags: [], chat: true});
        if (p.startsWith('/api/admin/settings')) return ok({groups: [], history: []});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(200);
  return {dom, errors};
}
const cur = (doc) => [...doc.querySelectorAll('#side a.tab[data-tab][aria-current="page"]')].map((b) => b.id).join(',');
(async () => {
  //given
  const fresh = await open('prefs');
  const w = fresh.dom.window, doc = w.document;
  const before = cur(doc);
  const mark = doc.querySelector('a.s-mark');

  //when
  const ev = new w.MouseEvent('click', {bubbles: true, cancelable: true, button: 0});
  mark.dispatchEvent(ev);
  await wait(100);
  const modified = new w.MouseEvent('click', {bubbles: true, cancelable: true, button: 0, ctrlKey: true});
  doc.getElementById('tab-prefs') && doc.getElementById('tab-prefs').click();
  await wait(50);
  mark.dispatchEvent(modified);

  //then
  assert.strictEqual(before, 'tab-prefs', 'a bare load reopens the remembered tab: ' + before);
  assert.strictEqual(ev.defaultPrevented, true, 'the mark does not reload the page');
  assert.strictEqual(modified.defaultPrevented, false, 'a modified click keeps the browser default');
  const second = await open('prefs');
  second.dom.window.document.querySelector('a.s-mark').dispatchEvent(new second.dom.window.MouseEvent('click', {bubbles: true, cancelable: true, button: 0}));
  await wait(100);
  assert.strictEqual(cur(second.dom.window.document), 'tab-anoms', 'the mark opens the main tab');
  assert.strictEqual(second.dom.window.localStorage.getItem('skopax-tab'), 'anoms', 'the main tab is remembered');
  assert.deepStrictEqual(fresh.errors.concat(second.errors), []);
  fresh.dom.window.close(); second.dom.window.close();
  console.log('PASS the header mark opens the main tab and remembers it, while a bare load still reopens the last tab');
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
