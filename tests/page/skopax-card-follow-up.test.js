const pageHtml = require('./skopax-page');
const assert = require('assert');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const CARD_PAGE = path.join(path.dirname(TEMPLATE), 'card.html');
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const AT = '2026-11-09T16:32:00Z';
const OPERATOR = {loginEnabled: true, user: 'alice@example.com', operator: true};
const VIEWER = {loginEnabled: true, user: 'bob@example.com', operator: false};
const PLANT = '<img src=x onerror="window.__x=1">';

async function boot(card, who, history) {
  const errors = [], posts = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(CARD_PAGE), {
    url: 'https://skopax.example.com/c/' + card.num, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.fetch = async (p, opts) => {
        if (String(p) === '/api/me') return {ok: true, status: 200, json: async () => Object.assign({}, who)};
        if (String(p).startsWith('/api/card-history')) return {ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(history || {}))};
        if (String(p).startsWith('/api/card-runs') || String(p).startsWith('/api/investigations')) return {ok: true, status: 200, json: async () => ({})};
        if (opts && opts.method === 'POST') { posts.push([String(p), JSON.parse(opts.body)]); return {ok: true, status: 201, json: async () => ({})}; }
        return {ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(card))};
      };
    },
  });
  await wait(120);
  return {w: dom.window, doc: dom.window.document, errors, posts};
}

const base = {num: 42, key: 'log|checkout|ERROR|x', svc: 'checkout', label: 'ERROR pattern', sev: 'warn', status: 'cleared', firstSeen: AT, clearedAt: AT, live: true, recommend: 'Add the 301 redirect in the shop admin'};

async function theNextStepShowsTheFollowUpCheckEscaped() {
  //given
  const card = Object.assign({}, base, {followUp: {doneWhen: 'GET https://example.com/old returns 301 ' + PLANT, checkedAt: AT, lastResult: 'not_done', lastEvidence: 'still 404 ' + PLANT, gaveUp: true, owner: 'ad team'}});

  //when
  const {w, doc, errors} = await boot(card, VIEWER);
  const line = doc.getElementById('follow-up');

  //then
  assert.ok(line, 'the follow-up line is shown');
  assert.deepStrictEqual([line.previousElementSibling.textContent, line.previousElementSibling.previousElementSibling.previousElementSibling.textContent], ['Follow-up check', 'Recommended step'], 'it sits under the next step');
  assert.ok(line.textContent.includes('GET https://example.com/old returns 301 ' + PLANT), 'the condition reads as text');
  assert.ok(line.textContent.includes('Not done yet') && line.textContent.includes('still 404'), 'the last result and evidence show');
  assert.ok(line.textContent.includes('Gave up'), 'a check that gave up says so');
  assert.ok(line.textContent.includes('Pending with ad team'), 'a keep verdict names who it waits on');
  assert.strictEqual(doc.querySelector('#follow-up img'), null, 'nothing in it is markup');
  assert.strictEqual(w.__x, undefined);
  assert.strictEqual(doc.getElementById('act-keep'), null, 'no close is pending, so there is no Keep open');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the card page shows the follow-up check under the next step, escaped, with the last result and gave up';
}

async function aPendingCloseShowsTheTimeAndKeepOpenPostsTheAction() {
  //given
  const card = Object.assign({}, base, {followUp: {doneWhen: 'GET https://example.com/old returns 301', closeAt: '2026-11-10T06:00:00Z', closeKind: 'fixed', lastResult: 'fixed', lastEvidence: 'answered 301 ' + PLANT, checkedAt: AT}});

  //when
  const {w, doc, errors, posts} = await boot(card, OPERATOR);
  const closing = doc.getElementById('follow-close');
  doc.getElementById('act-keep').click();
  await wait(50);

  //then
  assert.ok(/Closes as Fixed at 10 Nov \d\d:\d\d/.test(closing.textContent), 'the close time is shown');
  assert.ok(closing.textContent.includes('answered 301 ' + PLANT) && !doc.querySelector('#follow-close img'), 'with the evidence as text');
  assert.deepStrictEqual(posts.map((p) => [p[0], p[1].type, p[1].key]), [['/api/actions', 'keep', 'log|checkout|ERROR|x']]);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a pending close shows its time and evidence, and Keep open posts the keep action';
}

async function theHistoryListsFollowUpRowsAndKeepOpen() {
  //given
  const card = Object.assign({}, base);
  const history = {actions: [{type: 'keep', status: 'done', requestedAt: '2026-11-09T18:00:00Z', doneAt: '2026-11-09T18:00:00Z', requestedBy: 'alice@example.com', result: 'kept open'}],
    activity: [{kind: 'followup', who: 'Claude (pod)', t: '2026-11-09T17:00:00Z', text: 'Follow-up checked: not done yet \u00b7 still 404'},
      {kind: 'followup', who: 'Claude (pod)', t: '2026-11-09T19:00:00Z', text: 'Closed as Fixed after the follow-up check: Verified: answered 301'}]};

  //when
  const {w, doc, errors} = await boot(card, VIEWER, history);
  doc.getElementById('cdt-h').click();
  await wait(80);
  const rows = [...doc.querySelectorAll('.h-rows li')].map((li) => li.textContent);

  //then
  assert.ok(rows.some((r) => r.includes('Follow-up checked: not done yet')), 'the check is a row');
  assert.ok(rows.some((r) => r.includes('Closed as Fixed after the follow-up check')), 'the close after verify is a row');
  assert.ok(rows.some((r) => r.includes('Kept open')), 'Keep open is a row');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the History tab lists the follow-up check, the close after verify and Keep open';
}

(async () => {
  console.log('PASS ' + await theNextStepShowsTheFollowUpCheckEscaped());
  console.log('PASS ' + await aPendingCloseShowsTheTimeAndKeepOpenPostsTheAction());
  console.log('PASS ' + await theHistoryListsFollowUpRowsAndKeepOpen());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
