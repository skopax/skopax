const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

async function boot(summary) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary,
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=anoms', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: null, operator: false});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/nodes')) return ok({nodes: []});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(300);
  return {w: dom.window, doc: dom.window.document, errors};
}

async function theSummaryShowsOnlyTheStateBadgeAndLinks() {
  //given
  const {w, doc, errors} = await boot({level: 'warn', headline: 'first line; second line; third line', points: ['note one', 'note two', 'note three'], at: iso(2)});
  const reco = doc.getElementById('reco');

  //then
  assert.deepStrictEqual([...reco.children].map((c) => c.className), ['brief-top']);
  assert.strictEqual(reco.querySelector('.sev').textContent, 'Warning');
  assert.ok(!/first line|second line|note one|Written|Show all/.test(reco.textContent), 'no summary text, time or fold: ' + reco.textContent);
  assert.strictEqual(reco.querySelector('.brief-when').textContent, 'Nothing open');
  assert.ok(!reco.classList.contains('folded'));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the Summary box shows only the state badge and its links: no headline, no notes, no written time and no fold');
}

(async () => {
  await theSummaryShowsOnlyTheStateBadgeAndLinks();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
