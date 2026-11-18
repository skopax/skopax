const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const KEY = 'log|checkout-service|ERROR|boom';
const EVIL = '<img src=x onerror="window.__evil=1">';
const TIMED_OUT = 'The action did not finish in time and may or may not have happened: someone should check it, as this proposal cannot be confirmed again';
const FAILED = 'The confirmed action failed and may or may not have happened: someone should check it, as this proposal cannot be confirmed again';
const DOT = ' \u00b7 ';
const OUTCOMES = [
  ['confirmed', {ok: true, summary: 'demo worker restarted', outcome: 'done'}, 'Confirmed', ''],
  ['failed', {ok: false, summary: FAILED, outcome: 'failed'}, 'Failed', 'bad'],
  ['timed-out', {ok: false, summary: TIMED_OUT, outcome: 'timeout'}, 'Timed out', 'warn'],
  ['declined', {ok: false, summary: 'The plug-in said no', outcome: 'done'}, 'Declined', 'warn'],
];

async function until(check, ms) {
  const end = Date.now() + ms;
  while (!check() && Date.now() < end) await wait(20);
  return check();
}

function runs() {
  return [
    {id: 'r1', mode: 'live', trigger: 'slack', startedAt: iso(2), outcome: 'done', usd: 0.2, keys: [KEY], recap: 'A restart is proposed.', toolCalls: 3,
     ask: {text: 'Can you restart it?', by: 'bob@example.com', source: 'slack'},
     proposal: {tool: 'demo_propose', plugin: 'demo', title: 'Restart the demo worker', text: 'It is stuck since 14:00.'}},
    {id: 'r2', mode: 'live', trigger: 'slack', startedAt: iso(5), outcome: 'done', usd: 0.1, keys: [], recap: 'Proposed.', toolCalls: 1,
     ask: {text: 'And this one?', by: 'bob@example.com', source: 'slack'}, proposal: {tool: 'demo_propose', plugin: 'demo', title: EVIL, text: EVIL}},
    {id: 'r3', mode: 'live', trigger: 'slack', startedAt: iso(9), outcome: 'done', usd: 0.1, keys: [], recap: 'Done before.', toolCalls: 1,
     ask: {text: 'Earlier', by: 'bob@example.com', source: 'slack'},
     proposal: {tool: 'demo_propose', plugin: 'demo', title: 'Flush the demo cache', text: 'Old entries.', confirmedBy: 'carol@example.com', confirmedAt: iso(8), result: {ok: true, summary: 'cache flushed', outcome: 'done'}}},
    {id: 'r4', mode: 'live', trigger: 'slack', startedAt: iso(12), outcome: 'done', usd: 0.1, keys: [], recap: 'Slow one.', toolCalls: 1,
     ask: {text: 'Slow', by: 'bob@example.com', source: 'slack'},
     proposal: {tool: 'demo_propose', plugin: 'demo', title: 'Rebuild the index', text: 'Takes long.', confirmedBy: 'carol@example.com', confirmedAt: iso(11), result: {ok: false, summary: TIMED_OUT, outcome: 'timeout'}}},
  ];
}

async function boot(operator, answer, meLate) {
  const errors = [], posts = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [{key: KEY, sev: 'warn', svc: 'checkout-service', label: 'ERROR lines', what: 'x', status: 'open', firstSeen: iso(40), lastSeen: iso(1), src: 'logs', sig: 'log'}],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/#t=acts', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p, opt) => {
        const path = String(p);
        const ok = (body, status) => ({ok: !status || status < 400, status: status || 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (opt && opt.method === 'POST' && /^\/api\/investigations\/[^/]+\/proposal$/.test(path)) { posts.push(path); return ok(answer.body, answer.status); }
        if (path.startsWith('/api/investigations?limit=')) return ok(runs());
        if (path.startsWith('/api/server')) return ok({investigate: {mode: 'live', enabled: true, stopped: null, spentToday: 0.5, cap: 20}});
        if (path.startsWith('/api/me')) { if (meLate) await wait(150); return ok({loginEnabled: true, user: operator ? 'nina.white@example.com' : 'carol@example.com', operator: !!operator}); }
        if (path.startsWith('/api/rev')) return ok({rev: 1});
        if (path.startsWith('/api/actions') || path.startsWith('/api/deploy') || path.startsWith('/api/activity')) return ok([]);
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors, posts};
}

const row = (doc, id) => [...doc.querySelectorAll('#pod-list > li.hrow')].find((li) => li.querySelector('a.record').getAttribute('href').endsWith('/' + id));

async function theProposalShowsAndOnlyAnOperatorGetsConfirm() {
  //given
  const op = await boot(true, {body: {ok: true, summary: 'demo worker restarted'}});
  const viewer = await boot(false, {body: {ok: true, summary: 'x'}});
  const late = await boot(true, {body: {ok: true, summary: 'x'}}, true);
  const lateShown = await until(() => late.doc.querySelectorAll('#pod-list button.prop-go').length === 2, 3000);

  //when
  const shown = row(op.doc, 'r1').querySelector('.proposal');
  const done = row(op.doc, 'r3').querySelector('.proposal');

  //then
  assert.ok(shown, 'the run shows its proposal');
  assert.strictEqual(shown.querySelector('.prop-title').textContent, 'Proposed: Restart the demo worker');
  assert.strictEqual(shown.querySelector('.prop-text').textContent, 'It is stuck since 14:00.');
  assert.strictEqual(shown.querySelectorAll('button.prop-go').length, 1, 'an operator sees Confirm');
  assert.strictEqual(shown.querySelector('button.prop-go').textContent, 'Confirm');
  assert.ok(!done.querySelector('button.prop-go') && done.querySelector('.prop-done').textContent.includes('carol@example.com') && done.textContent.includes('cache flushed'), 'a confirmed proposal shows who and the result, no button');
  assert.ok(row(viewer.doc, 'r1').querySelector('.prop-title'), 'a viewer reads the proposal');
  assert.strictEqual(viewer.doc.querySelectorAll('#pod-list button.prop-go').length, 0, 'a viewer gets no Confirm');
  assert.ok(lateShown, 'Confirm appears once the operator role arrives after the first paint');
  assert.ok(row(op.doc, 'r4').querySelector('.prop-done').textContent.endsWith(': ' + TIMED_OUT) && !row(op.doc, 'r4').querySelector('button.prop-go'), 'a timed-out confirm says it may or may not have happened');
  assert.deepStrictEqual(op.errors.concat(viewer.errors, late.errors), []);
  op.w.close();
  viewer.w.close();
  late.w.close();
  console.log('PASS a run shows its proposal title and text; an operator sees Confirm (also when the role arrives late), a viewer does not, a confirmed one shows who and the result');
}

async function aConfirmClickPostsAndShowsTheSummary() {
  //given
  const {w, doc, errors, posts} = await boot(true, {body: {ok: true, summary: 'demo worker <b>restarted</b>'}});

  //when
  row(doc, 'r1').querySelector('button.prop-go').click();
  await wait(50);
  const box = row(doc, 'r1').querySelector('.proposal');

  //then
  assert.deepStrictEqual(posts, ['/api/investigations/r1/proposal']);
  assert.ok(!box.querySelector('button.prop-go'), 'the button goes once confirmed');
  assert.strictEqual(box.querySelector('.prop-result').textContent, 'Confirmed' + DOT + 'demo worker <b>restarted</b>');
  assert.deepStrictEqual([...box.querySelectorAll('.prop-result b')].map((b) => b.textContent), ['Confirmed'], 'the summary is text; only the head is bold');
  w.renderPodRuns();
  const again = row(doc, 'r1').querySelector('.proposal');
  assert.ok(!again.querySelector('button.prop-go'), 'a repaint before the next poll keeps the proposal confirmed');
  assert.ok(again.querySelector('.prop-done').textContent.startsWith('Confirmed by nina.white@example.com') && again.textContent.includes('demo worker <b>restarted</b>'));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS Confirm posts to the run proposal route once and shows the summary as text');
}

async function aTimedOutConfirmSaysItMayStillComplete() {
  //given
  const {w, doc, errors} = await boot(true, {body: {ok: false, summary: TIMED_OUT, outcome: 'timeout'}});

  //when
  row(doc, 'r1').querySelector('button.prop-go').click();
  await until(() => !row(doc, 'r1').querySelector('button.prop-go'), 2000);
  const box = row(doc, 'r1').querySelector('.proposal');

  //then
  assert.strictEqual(box.querySelector('.prop-result').textContent, 'Timed out' + DOT + TIMED_OUT);
  assert.ok(!box.querySelector('button.prop-go'), 'a timed-out confirm is closed, never offered again');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a confirm past its deadline says the action may or may not have happened and offers no second try');
}

function rules(list) {
  return [...list].flatMap((r) => r.selectorText ? r.selectorText.split(',').map((s) => s.trim()) : r.cssRules ? rules(r.cssRules) : []);
}

async function anOutcomeIsHeadedMarkedAndFocused(name, body, head, mark) {
  //given
  const {w, doc, errors} = await boot(true, {body});
  const go = row(doc, 'r1').querySelector('button.prop-go');

  //when
  go.focus();
  go.click();
  await until(() => !row(doc, 'r1').querySelector('button.prop-go'), 2000);
  const result = row(doc, 'r1').querySelector('.prop-result');
  const focused = doc.activeElement;
  const restored = [];
  const focus = w.HTMLElement.prototype.focus;
  w.HTMLElement.prototype.focus = function (options) { restored.push([this.className, options]); return focus.call(this, options); };
  w.renderPodRuns();
  w.HTMLElement.prototype.focus = focus;
  const done = row(doc, 'r1').querySelector('.prop-done');
  const css = rules(doc.querySelector('style').sheet.cssRules);

  //then
  assert.strictEqual(result.textContent, head + DOT + body.summary);
  assert.strictEqual(result.querySelector('b').textContent, head);
  assert.deepStrictEqual([...result.classList], ['prop-result'].concat(mark ? [mark] : []));
  assert.ok(focused === result && result.getAttribute('tabindex') === '-1', 'focus lands on the result, not on the page body: ' + focused.tagName);
  assert.ok(done.textContent.startsWith((mark ? head + DOT : '') + 'Confirmed by nina.white@example.com') && done.textContent.endsWith(': ' + body.summary), done.textContent);
  assert.deepStrictEqual([...done.classList], ['prop-done'].concat(mark ? [mark] : []));
  assert.strictEqual(doc.activeElement, done, 'a repaint keeps the focus on the same proposal');
  assert.deepStrictEqual(JSON.parse(JSON.stringify(restored)), [[done.className, {preventScroll: true}]], 'the repaint gives focus back without scrolling the page');
  if (mark) assert.ok(css.includes('.prop-result.' + mark) && css.includes('.prop-done.' + mark), 'the ' + mark + ' mark has its own style');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a ' + name + ' confirm is headed ' + head + (mark ? ' and marked ' + mark : '') + ', and focus lands on its result, also after a repaint');
}

async function aRefusedConfirmShowsTheDetail() {
  //given
  const {w, doc, errors, posts} = await boot(true, {status: 409, body: {detail: 'This proposal was already confirmed'}});

  //when
  row(doc, 'r1').querySelector('button.prop-go').click();
  await wait(50);
  const box = row(doc, 'r1').querySelector('.proposal');

  //then
  assert.deepStrictEqual(posts, ['/api/investigations/r1/proposal']);
  assert.strictEqual(box.querySelector('.prop-result').textContent, 'This proposal was already confirmed');
  assert.ok(!box.querySelector('button.prop-go'), 'no second try on a closed proposal');
  assert.strictEqual(doc.activeElement, box.querySelector('.prop-result'), 'focus lands on the refusal, not on the page body');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a 409 on Confirm shows its detail and removes the button');
}

async function anEvilTitleIsText() {
  //given
  const {w, doc, errors} = await boot(true, {body: {ok: true, summary: 'x'}});

  //when
  const box = row(doc, 'r2').querySelector('.proposal');

  //then
  assert.strictEqual(box.querySelector('.prop-title').textContent, 'Proposed: ' + EVIL);
  assert.strictEqual(box.querySelector('.prop-text').textContent, EVIL);
  assert.strictEqual(box.querySelectorAll('img').length, 0);
  assert.strictEqual(w.__evil, undefined);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a proposal title and text that hold markup show as text');
}

(async () => {
  await theProposalShowsAndOnlyAnOperatorGetsConfirm();
  await aConfirmClickPostsAndShowsTheSummary();
  await aTimedOutConfirmSaysItMayStillComplete();
  for (const outcome of OUTCOMES) await anOutcomeIsHeadedMarkedAndFocused(...outcome);
  await aRefusedConfirmShowsTheDetail();
  await anEvilTitleIsText();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
