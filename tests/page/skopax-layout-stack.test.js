const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [{key: 'log|checkout-service|ERROR|boom', num: 14, sev: 'warn', status: 'open', svc: 'checkout-service', label: 'ERROR lines', src: 'logs', sig: 'log', now: 1, firstSeen: iso(30), lastSeen: iso(1)}],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(hash, narrow) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state()));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/' + hash, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      if (narrow) w.matchMedia = (q) => ({matches: [...String(q).matchAll(/\((min|max)-width:\s*(\d+)px\)/g)].every((m) => m[1] === 'min' ? narrow >= +m[2] : narrow <= +m[2]) && /width/.test(q), media: q, addEventListener() {}});
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'nina.white@example.com', operator: true, teams: [], tags: []});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(450);
  return {w: dom.window, doc: dom.window.document, errors};
}

function allRules(doc) {
  const out = [];
  const walk = (list, media) => { for (const r of list) { if (r.selectorText) out.push({sel: r.selectorText, media, style: r.style}); else if (r.cssRules) walk(r.cssRules, (r.conditionText || (r.media && r.media.mediaText) || '').replace(/\s/g, '')); } };
  for (const sh of doc.styleSheets) walk(sh.cssRules, '');
  return out;
}

async function aNarrowContentStacksTheActivityRailUnderTheMainColumnOnEveryPage() {
  //given
  const {w, doc, errors} = await boot('#t=slack');
  const rules = allRules(doc);
  const cols = (r) => r.style.getPropertyValue('grid-template-columns').replace(/\s/g, '');
  const single = 'minmax(0px,1fr)';

  //when
  const variants = rules.filter((r) => r.media === '' && cols(r)).flatMap((r) => r.sel.split(',').map((x) => ({sel: x.trim(), cols: cols(r)}))).filter((r) => /^\.layout(\.[\w-]+)*$/.test(r.sel));
  const wideOnes = variants.filter((r) => r.cols !== single).map((r) => r.sel);
  const stacked = rules.filter((r) => r.media === 'content(max-width:900px)' && cols(r) === single).flatMap((r) => r.sel.split(',').map((x) => x.trim()));

  //then
  assert.ok(!doc.getElementById('tabs') && !doc.getElementById('nav-backdrop'), 'there is no filter side menu any more');
  assert.ok(wideOnes.includes('.layout'), 'the wide layout keeps a column for the Activity rail: ' + wideOnes.join(' '));
  assert.deepStrictEqual(wideOnes.filter((sel) => !stacked.includes(sel)), [], 'under 900 px of content every layout variant is one column, so the rail never squeezes or touches the banner and the strip');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'under 900 px of content every layout variant is one column, so the Activity rail sits under the main column';
}

(async () => {
  console.log('PASS ' + await aNarrowContentStacksTheActivityRailUnderTheMainColumnOnEveryPage());
})().catch((e) => { console.error('FAIL', e); process.exit(1); });
