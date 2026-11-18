const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

async function boot(stored) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: iso(-59), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/#t=anoms', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      Object.defineProperty(w, 'innerWidth', {value: 1920, configurable: true});
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      if (stored) Object.keys(stored).forEach((k) => w.localStorage.setItem(k, stored[k]));
      w.fetch = async (p) => {
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (String(p).startsWith('/api/me')) return ok({loginEnabled: true, user: 'nina.white@example.com', operator: true, teams: [], tags: []});
        if (String(p).startsWith('/api/rev')) return ok({rev: 1});
        return ok([]);
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors};
}

async function activityStartsCollapsedEvenOnAWideScreenAndRemembersWhenOpened() {
  //given
  const first = await boot();
  const box = first.doc.querySelector('.live');
  const btn = box.querySelector('.live-hide');

  //when
  const before = {closed: box.classList.contains('is-closed'), label: btn.textContent, expanded: btn.getAttribute('aria-expanded')};
  btn.click();
  const saved = first.w.localStorage.getItem('skopax-live');
  const again = await boot({'skopax-live': saved});

  //then
  assert.deepStrictEqual(before, {closed: true, label: 'Activity', expanded: 'false'}, 'Activity starts collapsed, even on a 1920 px screen');
  assert.strictEqual(saved, '1');
  assert.ok(!again.doc.querySelector('.live').classList.contains('is-closed'), 'once opened it stays open in this browser');
  assert.deepStrictEqual(first.errors.concat(again.errors), []);
  first.w.close(); again.w.close();
  return 'Activity starts collapsed even on a wide screen, and opening it is remembered in this browser';
}

(async () => {
  console.log('PASS ' + await activityStartsCollapsedEvenOnAWideScreenAndRemembersWhenOpened());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
