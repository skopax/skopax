const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const BRAND_BLOB = /(<script type="application\/json" id="brand-data">)[\s\S]*?(<\/script>)/;
const POD = 'Claude (pod)';

function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)}, anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(label) {
  const vc = new VirtualConsole();
  const brand = {name: 'Skopax', slug: 'skopax', version: '0.5.4', attribution: null, podBy: POD};
  if (label) brand.investigator = label;
  const html = pageHtml(TEMPLATE, {}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state())).replace(BRAND_BLOB, (m, a, b) => a + JSON.stringify(brand) + b);
  const dom = new JSDOM(html, {url: 'https://example.com/', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc, beforeParse(w) {
    w.scrollTo = () => {};
    w.fetch = async () => ({ok: false, status: 404, json: async () => ({}), text: async () => ''});
  }});
  await wait(200);
  return dom.window;
}

(async () => {
  const claude = await boot('Claude investigator');
  const other = await boot('Investigator');
  const plain = await boot();

  assert.strictEqual(claude.draftBy(POD), 'Claude investigator');
  assert.strictEqual(other.draftBy(POD), 'Investigator');
  assert.strictEqual(plain.draftBy(POD), 'Investigator', 'a page without the label still never shows the pod');
  assert.strictEqual(claude.histWho(POD), 'Claude investigator');
  assert.strictEqual(claude.resWho(POD), 'Claude investigator');
  assert.strictEqual(claude.sqSender(POD), 'Claude investigator (on request)');
  assert.strictEqual(claude.costBy(POD), 'Claude investigator');
  assert.strictEqual(claude.draftBy('alice@example.com'), 'alice');
  [claude, other, plain].forEach((w) => ['draftBy', 'histWho', 'resWho', 'sqSender', 'costBy'].forEach((f) => assert.ok(!/\(pod\)/.test(w[f](POD)), f)));
  console.log('PASS the investigator is never shown as a pod');
  process.exit(0);
})().catch((e) => { console.error('FAIL', e); process.exit(1); });
