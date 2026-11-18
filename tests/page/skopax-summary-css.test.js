const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

function state() {
  const card = (key, extra) => Object.assign({key, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'x', src: 'logs', sig: 'log', now: 1, firstSeen: iso(90), lastSeen: iso(1)}, extra || {});
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'release 10.11 deployed', points: ['first point'], at: iso(1)},
    anomalies: [card('log|checkout-service|ERROR|boom'), card('log|payments-api|ERROR|npe', {status: 'cleared', clearedAt: iso(30)})],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot() {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state())), {
    url: 'https://skopax.example.com/#t=anoms', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (String(p).startsWith('/api/me')) return ok({loginEnabled: true, user: null, operator: false});
        if (String(p).startsWith('/api/actions') || String(p).startsWith('/api/deploy-reports')) return ok([]);
        if (String(p).startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors};
}

function rules(list) {
  return [...list].flatMap((r) => r.selectorText ? [r.selectorText] : r.cssRules ? rules(r.cssRules) : []);
}

async function everySummaryBoxRuleStillStylesSomethingOnThePage() {
  //given
  const {w, doc, errors} = await boot();
  const reco = doc.getElementById('reco');

  //when
  doc.documentElement.dataset.density = 'compact';
  const summary = rules(doc.querySelector('style').sheet.cssRules).filter((s) => /#reco|brief-meta|\.brief summary|\.brief h2 \.xlink/.test(s));
  const dead = summary.filter((s) => !s.split(',').some((one) => doc.querySelector(one.replace(/:(hover|focus-visible|focus)\b/g, '').trim())));

  //then
  assert.deepStrictEqual([...reco.children].map((c) => c.className), ['brief-top']);
  assert.deepStrictEqual([...reco.querySelectorAll('.xlink')].map((b) => b.textContent), ['2 warning open', 'Deploy reports \u203a']);
  assert.ok(summary.length >= 5, 'the Summary box rules are found: ' + summary.join(' | '));
  assert.deepStrictEqual(dead, [], 'Summary box rules that style nothing');
  assert.ok(!reco.classList.contains('folded'));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS every CSS rule of the links-only Summary box styles something on the page: no leftovers of the old headline, points or fold');
}

(async () => {
  await everySummaryBoxRuleStillStylesSomethingOnThePage();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
