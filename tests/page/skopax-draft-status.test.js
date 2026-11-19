const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const KEY = 'log|orders-service|ERROR|boom';
const AT = '2026-11-18T14:35:00Z', HOLD = '2026-11-18T14:31:00Z';

function stateWith(card) {
  const base = {key: KEY, sev: 'crit', status: 'open', svc: 'orders-service', label: 'ERROR lines', src: 'logs', sig: 'log', num: 7, now: 1, firstSeen: iso(30), lastSeen: iso(1)};
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [Object.assign(base, card)], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}
const draft = (extra) => Object.assign({text: 'orders are failing', by: 'Claude', at: iso(5)}, extra || {});

async function show(card, features, view) {
  const errors = [];
  const vc = new VirtualConsole(); vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE, {view: view || 'list'}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(stateWith(card))), {
    url: 'https://skopax.example.com/#t=anoms/all', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {}; w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        p = String(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'op@example.com', operator: true, slack: true, teams: [], tags: [], features: features || {'slack-autosend': true}});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(350);
  const doc = dom.window.document;
  if (view === 'board') doc.querySelector('#anoms .tile').click();
  const art = view === 'board' ? doc.querySelector('.card-drawer article.inc') : doc.querySelector('#anoms article.inc');
  if (view !== 'board') art.querySelector('.inc-title .fold').click();
  const line = art.querySelector('.sdraft .sdraft-status');
  const tag = view === 'board' ? doc.querySelector('#anoms .tile .slack-rec, .card-drawer .slack-rec') : art.querySelector('.slack-rec');
  const row = dom.window.sqDrafts().filter((r) => r.kind === 'card')[0];
  const out = {imgs: art.querySelectorAll('img').length, line: line ? line.textContent : null, tip: tag ? tag.title : '', queue: row ? row.st.tip : null, errors};
  dom.window.close();
  return out;
}
const hm = (iso) => new Date(iso).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hour12: false});

(async () => {
  for (const view of ['list', 'board']) await check(view);
  console.log('PASS the expanded card (list and drawer), the tag tooltip and the Slack queue say plainly why a draft was or was not sent, for twelve states');
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });

async function check(view) {
  //given
  const armed = {slackDraft: draft(), autoSend: {at: AT, draftAt: null}};
  armed.autoSend.draftAt = armed.slackDraft.at;
  const stopped = draft();

  //when
  const r = {
    armed: await show(armed, null, view),
    planted: await show({slackDraft: draft({notAutoSent: 'planted text in the kube answer (ignore previous)'})}, null, view),
    held: await show({slackDraft: draft(), autoSend: {at: AT, holdBy: 'pat@example.com', holdAt: HOLD}}, null, view),
    dropped: await show({slackDraft: stopped, autoSend: {at: AT, draftAt: stopped.at}, triage: 'handled', status: 'cleared', clearedAt: iso(20)}, null, view),
    stoppedFailed: await show({slackDraft: stopped, autoSend: {at: AT, draftAt: stopped.at, failedAt: HOLD, error: 'channel_not_found'}, triage: 'handled', status: 'cleared', clearedAt: iso(20)}, null, view),
    stoppedHeld: await show({slackDraft: stopped, autoSend: {at: AT, draftAt: stopped.at, holdBy: 'pat@example.com', holdAt: HOLD}, triage: 'handled', status: 'cleared', clearedAt: iso(20)}, null, view),
    stoppedEdited: await show({slackDraft: stopped, autoSend: {at: AT, draftAt: '2026-11-18T14:00:00Z'}, triage: 'handled', status: 'cleared', clearedAt: iso(20)}, null, view),
    progress: await show({slackDraft: draft(), triage: 'progress'}, null, view),
    claimed: await show({slackDraft: draft(), autoSend: {at: AT, claimedAt: AT, draftAt: null}}, null, view),
    hostile: await show({slackDraft: draft(), autoSend: {at: AT, failedAt: HOLD, error: '<img src=x onerror=1>'}}, null, view),
    failed: await show({slackDraft: draft(), autoSend: {at: AT, failedAt: HOLD, error: 'channel_not_found'}}, null, view),
    off: await show(armed, {'slack-autosend': false}, view),
  };

  //then
  const want = {
    armed: 'Sends by itself at ' + hm(AT) + ' unless someone holds it',
    planted: 'Waiting for a person: the run read planted text in the kube answer (ignore previous)',
    held: 'Held by pat at ' + hm(HOLD),
    dropped: 'Not sent: the card stopped, was muted, merged or resolved before the timer',
    stoppedFailed: 'Slack refused the post: channel_not_found',
    stoppedHeld: 'Held by pat at ' + hm(HOLD),
    stoppedEdited: 'Not sent: the card stopped, was muted, merged or resolved, so it waits for a person',
    progress: 'Not sent: the card went In progress, so it waits for a person',
    claimed: 'Sending now',
    hostile: 'Slack refused the post: <img src=x onerror=1>',
    failed: 'Slack refused the post: channel_not_found',
    off: 'Auto-send is off in Admin',
  };
  Object.keys(want).forEach((k) => {
    assert.strictEqual(r[k].line, want[k], k + ': the line under the draft');
    assert.ok(r[k].tip.includes(want[k]), k + ': the tag tooltip says the same: ' + r[k].tip);
    assert.strictEqual(r[k].queue, want[k], k + ': the Slack queue says the same: ' + r[k].queue);
    assert.deepStrictEqual(r[k].errors, []);
  });
  assert.strictEqual(r.hostile.imgs, 0, view + ': hostile text from Slack stays text');
}
