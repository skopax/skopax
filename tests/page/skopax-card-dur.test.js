const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const BASE = {svc: 'contacts-sync', label: 'Airbyte long run', sev: 'warn', status: 'open', firstSeen: iso(30), lastSeen: iso(1), unit: 'dur'};
const CARDS = [Object.assign({}, BASE, {num: 21, key: 'airbyte|contacts-sync|long', src: 'airbyte', sig: 'airbyte-long-run', now: 9000, d1: 5400, d7: 90}),
  Object.assign({}, BASE, {num: 22, key: 'argo|commerce/checkout-service', src: 'argo', sig: 'argo', now: 7260, d1: null, d7: null}),
  Object.assign({}, BASE, {num: 23, key: 'apm-p95|checkout-service', src: 'apm', sig: 'apm', now: 1500, d1: 320, d7: null, unit: 'ms'})];

async function boot() {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: CARDS, services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const dom = new JSDOM(pageHtml(TEMPLATE, {view: 'board'}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/#t=anoms/all', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        p = String(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: null, operator: false, teams: [], tags: []});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/state')) return ok(state);
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors};
}

async function durationCardsReadAsHoursAndMinutesInThePanel() {
  //given
  const {w, doc, errors} = await boot();
  const line = async (num) => {
    w.location.hash = '#c=' + num;
    await wait(60);
    return doc.querySelector('.card-drawer .cd-details .line').textContent;
  };

  //when
  const long = await line(21), argo = await line(22), ms = await line(23);

  //then
  assert.ok(long.startsWith('running 2 h 30 minusual 90 min'), long);
  assert.ok(argo.startsWith('for 2 h 1 min'), argo);
  assert.ok(ms.startsWith('now 1.5 s1 d 320 ms7 d \u2013'), 'other units stay as they were: ' + ms);
  assert.ok(![long, argo, ms].some((t) => /\bdur\b/.test(t)), 'never the word dur');
  assert.deepStrictEqual(errors, []);
  w.close();
}

(async () => {
  await durationCardsReadAsHoursAndMinutesInThePanel();
  console.log('PASS cards measured in seconds (Airbyte, Argo) read as h / min / s in the card panel instead of a raw number and the word dur');
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
