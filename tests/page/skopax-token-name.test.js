const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const KEY = 'log|checkout-service|ERROR|boom';
const AT = '2026-11-09T16:32:00Z';

function board() {
  const run = {id: 'run-1', trigger: 'action', actionId: 'a1', keys: [KEY], pod: 'skopax-abc:1a2b', room: 'investigator-room-0', startedAt: iso(2), model: 'claude-sonnet-5-5', effort: 'high',
    ask: {text: 'Why did it rise?', by: 'token', source: 'page'}, toolCalls: 2, lastTool: 'esql', lastToolAt: iso(0)};
  return {at: iso(0), mode: 'live', enabled: true, halted: null,
    rooms: [{id: 'investigator-room-0', name: 'Room 1', takes: 'questions', pod: 'skopax-abc', until: iso(-10), run}], unplaced: [],
    waiting: [{id: 'a2', type: 'dive', source: 'page', by: 'token', at: iso(1), text: 'Investigate', cards: [KEY], thread: null, why: 'next for a free room'},
              {id: 'a3', type: 'ask', source: 'chat', by: null, at: iso(0), text: 'Chat message (private)', cards: [], thread: null, why: 'next for a free room'},
              {id: 'a4', type: 'dive', source: 'page', by: 'alice@example.com', at: iso(0), text: 'Investigate', cards: [KEY], thread: null, why: 'next for a free room'}],
    recent: [], limits: {slackAsks: 0, slackAskCap: 10, gateCalls: 0, gateCap: 60, spentToday: 1, cap: 25, askers: [], perPersonCap: 4, pageAsks: 0, pageAskCap: 6}};
}

async function bootBoard() {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [{key: KEY, sev: 'warn', svc: 'checkout-service', label: 'ERROR lines', what: 'x', status: 'open', firstSeen: iso(40), lastSeen: iso(1), src: 'log', sig: 'err'}],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/#t=inv', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (String(p).startsWith('/api/investigator/status')) return ok(board());
        if (String(p).startsWith('/api/me')) return ok({loginEnabled: true, user: null, operator: false});
        if (String(p).startsWith('/api/actions') || String(p).startsWith('/api/deploy-reports') || String(p).startsWith('/api/requests')) return ok([]);
        if (String(p).startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors};
}

async function bootCard(by) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [{key: KEY, num: 7, sev: 'warn', svc: 'checkout-service', label: 'ERROR lines', what: 'x', status: 'open', firstSeen: AT, lastSeen: iso(1), src: 'log', sig: 'err'}],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const action = {id: 'a1', type: 'dive', target: KEY, key: KEY, status: 'requested', requestedBy: by, requestedAt: AT};
  const dom = new JSDOM(pageHtml(TEMPLATE, {view: 'board'}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/#c=7', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (String(p).startsWith('/api/me')) return ok({loginEnabled: true, user: null, operator: false});
        if (String(p).startsWith('/api/actions')) return ok([action]);
        if (String(p).startsWith('/api/deploy-reports') || String(p).startsWith('/api/requests')) return ok([]);
        if (String(p).startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors};
}
const lastBy = (x) => x.doc.querySelector('.card-drawer .last-act').textContent;

async function actionsMadeWithTheApiTokenReadAsInvestigatorOnTheInvestigatorTabAndTheCardPage() {
  //given
  const page = await bootBoard();
  const token = await bootCard('token');
  const person = await bootCard('alice@example.com');

  //when
  const waits = [...page.doc.querySelectorAll('#inv .inv-wait .inv-meta')].map((m) => m.textContent.split(' \u00b7 ')[1]);
  const asked = page.doc.querySelector('#inv .inv-room .inv-meta').textContent;

  //then
  assert.deepStrictEqual(waits, ['Investigator', '?', 'alice'], 'a token-made wait reads Investigator, a private chat keeps ?, a person keeps their name');
  assert.ok(asked.includes('asked by Investigator'), asked);
  assert.ok(/\bInvestigator\b/.test(lastBy(token)) && !/\btoken\b/.test(lastBy(token)), 'the panel names the token Investigator: ' + lastBy(token));
  assert.ok(/\balice\b/.test(lastBy(person)) && !/example\.com/.test(lastBy(person)), 'a person keeps the short name: ' + lastBy(person));
  [page, token, person].forEach((x) => { assert.deepStrictEqual(x.errors, []); x.w.close(); });
  console.log('PASS an action made with the API token reads Claude on the Investigator tab and in the card panel; people keep their short name and a private chat keeps ?');
}

async function bootReports(by) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const report = {id: 'r1', createdAt: iso(30), requestedBy: by, rows: [], issues: [], summary: 'Deploy check'};
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/#t=reps', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (String(p).startsWith('/api/deploy-reports')) return ok([report]);
        if (String(p).startsWith('/api/me')) return ok({loginEnabled: true, user: null, operator: false});
        if (String(p).startsWith('/api/rev')) return ok({rev: 1});
        if (String(p).startsWith('/api/actions') || String(p).startsWith('/api/requests') || String(p).startsWith('/api/deploys')) return ok([]);
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors};
}

async function aNamedTokenReadsAsItsNameOnTheCardPageSlackAndDeployReports() {
  //given
  const card = await bootCard('token:ci');
  const named = await bootReports('token:ci');
  const legacy = await bootReports('token');

  //when
  const reportBy = (x) => x.doc.querySelector('#reps details.drep summary .muted').textContent.split(' \u00b7 ').pop();
  const sender = (by) => named.w.eval('sqSender(' + JSON.stringify(by) + ')');

  //then
  assert.ok(/\bci\b/.test(lastBy(card)) && !/token/.test(lastBy(card)), 'the panel names the token: ' + lastBy(card));
  assert.deepStrictEqual([reportBy(named), reportBy(legacy)], ['ci', 'Investigator'], 'releases.js');
  assert.deepStrictEqual([sender('token:ci'), sender('token'), sender('alice@example.com')], ['ci (on request)', 'Investigator (on request)', 'alice'], 'slack.js');
  [card, named, legacy].forEach((x) => { assert.deepStrictEqual(x.errors, []); x.w.close(); });
  console.log('PASS a named token reads as its name in the card panel, in the Slack queue and on deploy reports, and the legacy token reads Claude');
}

(async () => {
  await actionsMadeWithTheApiTokenReadAsInvestigatorOnTheInvestigatorTabAndTheCardPage();
  await aNamedTokenReadsAsItsNameOnTheCardPageSlackAndDeployReports();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
