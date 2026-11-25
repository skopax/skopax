const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const OPEN = 'log|checkout-service|ERROR|boom', PROG = 'log|payments-api|ERROR|npe', MUTED = 'log|cart-service|ERROR|quiet';

function card(key, num, extra) {
  return Object.assign({key, num, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'ERROR lines', src: 'logs', sig: 'log', now: 1, firstSeen: iso(30), lastSeen: iso(1), what: 'text'}, extra || {});
}
function state(over) {
  const o = over || {};
  return {rev: o.rev || 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: iso(-59), sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(OPEN, 11, o.open), card(PROG, 12, Object.assign({triage: 'progress', owner: 'alice@example.com', ticket: 'SHOP-123'}, o.prog)), card(MUTED, 13, Object.assign({muted: true}, o.muted))],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}
function later(reply) {
  let open;
  const promise = new Promise((r) => { open = r; });
  return {promise, open: () => open(), reply};
}

async function boot() {
  const errors = [], posts = [], gates = [], timers = [];
  const srv = {rev: 1, state: state(), states: 0, actionGets: 0};
  const vc = new VirtualConsole(); vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE, {view: 'board'}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state())), {
    url: 'https://skopax.example.com/#t=anoms/all', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {}; w.Element.prototype.scrollIntoView = function () {};
      const realSet = w.setTimeout.bind(w);
      w.setTimeout = (fn, ms, ...rest) => { if (ms === 30000) { timers.push(fn); return 0; } return realSet(fn, ms, ...rest); };
      w.fetch = async (p, opts) => {
        p = String(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (opts && opts.method === 'POST' && p.startsWith('/api/actions')) {
          const body = JSON.parse(opts.body);
          posts.push(body);
          const g = gates.shift();
          if (g) await g.promise;
          if (g && g.reply && g.reply.code) return {ok: false, status: g.reply.code, json: async () => ({detail: g.reply.detail})};
          return ok(Object.assign({id: 'x' + posts.length, type: body.type, status: 'done', result: 'ok'}, g && g.reply));
        }
        if (opts && opts.method === 'POST' && p.startsWith('/api/slack-posts')) { posts.push({type: 'slack', key: JSON.parse(opts.body).key}); return ok({ok: true}); }
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'op@example.com', operator: true, slack: true, slackChannel: '#ops', jira: true, jiraUrl: 'https://jira.example.com', teams: [], tags: []});
        if (p.startsWith('/api/actions')) { srv.actionGets += 1; const g = srv.actionsGate; srv.actionsGate = null; if (g) await g.promise; return ok([]); }
        if (p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: srv.rev});
        if (p.startsWith('/api/state')) { srv.states += 1; const body = Object.assign({}, srv.state, {rev: srv.rev}); const g = srv.stateGate; srv.stateGate = null; if (g) await g.promise; return ok(body); }
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(400);
  const w = dom.window;
  const hold = (reply) => { const g = later(reply); gates.push(g); return g; };
  return {w, doc: w.document, errors, posts, gates, timers, srv, hold};
}
const tileOf = (doc, key) => [...doc.querySelectorAll('#anoms .tile')].find((t) => t.dataset.key === key);
const colOf = (doc, key) => { const t = tileOf(doc, key); return t ? t.closest('.bcol').dataset.col : null; };
const busy = (n) => !!n && n.classList.contains('busy') && n.getAttribute('aria-busy') === 'true';
const calm = (n) => !!n && !n.classList.contains('busy') && !n.hasAttribute('aria-busy');
const named = (b) => b.getAttribute('aria-label') || b.textContent.trim();
const drawerBtn = (doc, name) => [...doc.querySelectorAll('.card-drawer .acts button')].find((b) => named(b) === name);
const actionBtns = (doc) => [...doc.querySelectorAll('.card-drawer .acts button.btn:not(.free)')].filter((b) => b.dataset.gpWait !== '1');
const bar = (doc) => doc.querySelector('[role="progressbar"]');
function drag(w, doc, key, col) {
  const t = tileOf(doc, key);
  const start = new w.Event('dragstart', {bubbles: true, cancelable: true});
  t.dispatchEvent(start);
  const target = doc.querySelector('#anoms .bcol[data-col="' + col + '"]');
  target.dispatchEvent(new w.Event('dragover', {bubbles: true, cancelable: true}));
  target.dispatchEvent(new w.Event('drop', {bubbles: true, cancelable: true}));
  return !start.defaultPrevented;
}
async function openPanel(doc, key) {
  tileOf(doc, key).click();
  await wait(50);
}

async function aCardIsBusyWhileItsActionRuns() {
  //given
  const {w, doc, errors, posts, hold} = await boot();
  await openPanel(doc, OPEN);
  const gate = hold();

  //when
  drawerBtn(doc, 'Mute 24 h').click();
  await wait(50);
  const during = {tile: busy(tileOf(doc, OPEN)), panel: busy(doc.querySelector('.card-drawer')), drag: tileOf(doc, OPEN).draggable,
    enabled: actionBtns(doc).filter((b) => !b.disabled).map(named)};
  const css = require('fs').readFileSync(TEMPLATE, 'utf8');
  gate.open();
  await wait(80);
  const after = {tile: calm(tileOf(doc, OPEN)), panel: calm(doc.querySelector('.card-drawer')), drag: tileOf(doc, OPEN).draggable, disabled: actionBtns(doc).filter((b) => b.disabled).map(named)};

  //then
  assert.deepStrictEqual(posts.map((p) => p.type), ['mute']);
  assert.deepStrictEqual([during.tile, during.panel, during.drag], [true, true, false], 'the tile and the panel are busy and the tile cannot be dragged');
  assert.deepStrictEqual(during.enabled, [], 'every action button of the card is disabled');
  assert.ok(/\.tile\.busy::after[^{]*\{[^}]*animation/.test(css) && /prefers-reduced-motion: reduce\)[^@]*\.tile\.busy::after[^}]*animation: none/.test(css), 'a CSS spinner that stops for reduced motion');
  assert.deepStrictEqual(after, {tile: true, panel: true, drag: true, disabled: []}, 'all is cleared after the action resolves');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a card is busy, disabled and not draggable while its action runs, and usable again after';
}

async function aSecondClickOnABusyCardSendsNothing() {
  //given
  const {w, doc, errors, posts, hold} = await boot();
  await openPanel(doc, OPEN);
  const gate = hold();
  drawerBtn(doc, 'Mute 24 h').click();
  await wait(30);

  //when
  const take = drawerBtn(doc, 'Take it');
  take.click();
  take.disabled = false;
  take.click();
  const dragged = drag(w, doc, OPEN, 'progress');
  await wait(50);
  gate.open();
  await wait(80);

  //then
  assert.deepStrictEqual(posts.map((p) => p.type), ['mute'], 'neither the click, a click on a re-enabled button, nor a drag sends a second action');
  assert.strictEqual(dragged, false, 'the drag does not start');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a second action on a busy card is ignored and never sent';
}

async function aDragMoveLocksTheCardUntilAllStepsAreDone() {
  //given
  const {w, doc, errors, posts, hold, srv} = await boot();
  const first = hold(), second = hold();
  let paints = 0;
  doc.addEventListener('skopax:state', () => { paints += 1; });

  //when
  drag(w, doc, MUTED, 'progress');
  await wait(50);
  const one = {sent: posts.map((p) => p.type), busy: busy(tileOf(doc, MUTED)), col: colOf(doc, MUTED)};
  first.open();
  await wait(50);
  const two = {sent: posts.map((p) => p.type), busy: busy(tileOf(doc, MUTED)), states: srv.states};
  paints = 0;
  srv.rev = 2;
  srv.state = state({rev: 2, muted: {muted: false, triage: 'progress', owner: 'op@example.com'}});
  second.open();
  await wait(120);

  //then
  assert.deepStrictEqual(one, {sent: ['unmute'], busy: true, col: 'progress'}, 'the tile shows in the target column at once and is locked');
  assert.deepStrictEqual(two, {sent: ['unmute', 'take'], busy: true, states: 0}, 'between the steps the card stays locked and nothing repaints');
  assert.strictEqual(paints, 1, 'one repaint after both steps');
  assert.strictEqual(srv.states, 1);
  assert.ok(calm(tileOf(doc, MUTED)) && colOf(doc, MUTED) === 'progress' && tileOf(doc, MUTED).draggable);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a drag with two steps locks the card until both are done, then repaints once';
}

async function aPollDuringAnActionDoesNotSnapTheCardBack() {
  //given
  const {w, doc, errors, hold, srv} = await boot();
  const gate = hold();
  drag(w, doc, OPEN, 'progress');
  await wait(50);

  //when
  srv.rev = 2;
  srv.state = state({rev: 2, prog: {owner: 'bob@example.com'}});
  await w.__skopaxPoll();
  await wait(50);
  const during = {col: colOf(doc, OPEN), busy: busy(tileOf(doc, OPEN)), other: tileOf(doc, PROG).querySelector('.tile-asg').getAttribute('aria-label')};
  srv.rev = 3;
  srv.state = state({rev: 3, open: {triage: 'progress', owner: 'op@example.com'}, prog: {owner: 'bob@example.com'}});
  gate.open();
  await wait(120);

  //then
  assert.deepStrictEqual(during, {col: 'progress', busy: true, other: 'Assignee: bob'}, 'the moved card stays in the target column while other cards update');
  assert.ok(colOf(doc, OPEN) === 'progress' && calm(tileOf(doc, OPEN)), 'after the move it is painted from fresh data');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a poll that returns the old column while the move runs does not snap the card back';
}

async function otherCardsStayUsableDuringAnAction() {
  //given
  const {w, doc, errors, posts, hold} = await boot();
  const gate = hold(), other = hold();
  drag(w, doc, OPEN, 'progress');
  await wait(50);

  //when
  const free = {busy: busy(tileOf(doc, MUTED)), drag: tileOf(doc, MUTED).draggable};
  const started = drag(w, doc, MUTED, 'progress');
  await wait(50);
  const sent = posts.map((p) => p.target);
  gate.open();
  other.open();
  await wait(120);

  //then
  assert.deepStrictEqual(free, {busy: false, drag: true}, 'another card is not busy and can be dragged');
  assert.ok(started);
  assert.deepStrictEqual(sent, [OPEN, MUTED], 'its action is sent while the first one runs');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'other cards stay usable while one card has an action running';
}

async function aFailedActionPutsTheCardBackAndSaysSo() {
  //given
  const {w, doc, errors, hold} = await boot();
  const refused = hold({status: 'failed', result: 'Jira said no'});

  //when
  drag(w, doc, OPEN, 'progress');
  await wait(50);
  const moved = colOf(doc, OPEN);
  refused.open();
  await wait(120);
  const back = {col: colOf(doc, OPEN), calm: calm(tileOf(doc, OPEN)), note: doc.getElementById('board-note').textContent, toast: (doc.getElementById('fail-toasts') || {}).textContent || ''};
  const broken = hold({code: 500, detail: 'the store is down'});
  drag(w, doc, OPEN, 'muted');
  await wait(50);
  const ask = doc.querySelector('.mute-pick .mp-yes');
  if (ask) ask.click();
  await wait(50);
  broken.open();
  await wait(120);
  const thrown = {col: colOf(doc, OPEN), calm: calm(tileOf(doc, OPEN)), toast: (doc.getElementById('fail-toasts') || {}).textContent || ''};

  //then
  assert.strictEqual(moved, 'progress');
  assert.strictEqual(back.col, 'open', 'a failed step puts the card back in its real column');
  assert.ok(back.calm);
  assert.match(back.note, /Not moved: Jira said no/);
  assert.match(back.toast, /Jira said no/, 'the failure shows as a toast');
  assert.deepStrictEqual([thrown.col, thrown.calm], ['open', true], 'a thrown error puts it back too');
  assert.match(thrown.toast, /the store is down/);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a failed or refused action puts the card back from fresh data and says why';
}

async function theTopBarShowsWhileAnyActionRuns() {
  //given
  const {w, doc, errors, hold} = await boot();
  const before = !bar(doc) || bar(doc).hidden;
  const gate = hold();

  //when
  drag(w, doc, OPEN, 'progress');
  await wait(30);
  const b = bar(doc);
  const during = {shown: !!b && !b.hidden, value: b && b.hasAttribute('aria-valuenow'), label: b && b.getAttribute('aria-label')};
  gate.open();
  await wait(120);

  //then
  assert.ok(before, 'no bar while nothing runs');
  assert.deepStrictEqual(during, {shown: true, value: false, label: 'Working'}, 'an indeterminate bar shows while the action runs');
  assert.ok(bar(doc).hidden, 'it hides when nothing runs');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a thin indeterminate bar shows at the top while any action runs';
}

async function aBusyCardIsReleasedAfterTheTimeout() {
  //given
  const {w, doc, errors, posts, hold, timers} = await boot();
  const slow = hold();
  drag(w, doc, OPEN, 'progress');
  await wait(30);

  //when
  const held = busy(tileOf(doc, OPEN));
  timers.splice(0).forEach((fn) => fn());
  await wait(50);
  const freed = {calm: calm(tileOf(doc, OPEN)), drag: tileOf(doc, OPEN).draggable, toast: doc.getElementById('toasts').textContent};
  const again = hold();
  drag(w, doc, OPEN, 'progress');
  await wait(30);
  slow.open();
  await wait(80);
  const kept = busy(tileOf(doc, OPEN));
  again.open();
  await wait(120);

  //then
  assert.ok(held);
  assert.ok(freed.calm && freed.drag, 'after 30 s the card is released');
  assert.match(freed.toast, /Still working: refresh to see the result/);
  assert.strictEqual(posts.length, 2, 'a new action can be sent after the release');
  assert.ok(kept, 'the late answer of the first action does not release the second lock');
  assert.ok(calm(tileOf(doc, OPEN)));
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a busy card is released after 30 s with a note, and a late answer leaves a newer lock alone';
}

async function aCardStaysLockedUntilTheFreshStateArrives() {
  //given
  const {w, doc, errors, posts, hold, srv} = await boot();
  await openPanel(doc, OPEN);
  const gate = hold();
  drawerBtn(doc, 'Investigate').click();
  await wait(30);
  const list = later();
  srv.actionsGate = list;

  //when
  gate.open();
  await wait(50);
  const held = {tile: busy(tileOf(doc, OPEN)), off: drawerBtn(doc, 'Investigate').disabled};
  const again = drawerBtn(doc, 'Investigate');
  again.click();
  again.disabled = false;
  again.click();
  await wait(30);
  list.open();
  await wait(120);

  //then
  assert.deepStrictEqual(held, {tile: true, off: true}, 'the card stays locked while the fresh action list and state load');
  assert.deepStrictEqual(posts.map((p) => p.type), ['dive'], 'a second click in that window sends no second paid run');
  assert.ok(calm(tileOf(doc, OPEN)), 'it is released once the fresh data is in');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a card stays locked until the fresh data after its action has landed';
}

async function aPollFromBeforeTheMoveNeverPaintsTheOldColumn() {
  //given
  const {w, doc, errors, hold, srv} = await boot();
  const seen = [];
  doc.addEventListener('skopax:state', () => { const a = w.ALL.find((x) => x.key === OPEN); seen.push(w.boardColumn(a)); });
  const gate = hold();
  drag(w, doc, OPEN, 'progress');
  await wait(30);
  srv.rev = 2;
  srv.stateGate = later();
  const oldState = srv.stateGate;
  w.__skopaxPoll();
  await wait(30);

  //when
  srv.rev = 3;
  srv.state = state({rev: 3, open: {triage: 'progress', owner: 'op@example.com'}});
  gate.open();
  await wait(30);
  oldState.open();
  await wait(150);

  //then
  assert.ok(seen.length >= 2, 'the board was painted');
  assert.deepStrictEqual(seen.filter((c) => c !== 'progress'), [], 'the card is never painted in its old column');
  assert.ok(colOf(doc, OPEN) === 'progress' && calm(tileOf(doc, OPEN)));
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a poll answered before the move committed is dropped, and the card never snaps back';
}

async function aTimedOutDragSendsNoFurtherSteps() {
  //given
  const {w, doc, errors, posts, hold, timers} = await boot();
  const first = hold();
  drag(w, doc, MUTED, 'progress');
  await wait(30);

  //when
  timers.splice(0).forEach((fn) => fn());
  first.open();
  await wait(120);

  //then
  assert.deepStrictEqual(posts.map((p) => p.type), ['unmute'], 'after the timeout released the card, the second step is not sent');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a drag whose lock timed out stops before its next step';
}

async function anArmedConfirmOnAnotherCardSurvivesAMove() {
  //given
  const {w, doc, errors, hold} = await boot();
  await openPanel(doc, PROG);
  const slack = doc.querySelector('.card-drawer .acts button.slack-btn:not(.slack-upd)');
  slack.click();
  const gate = hold();

  //when
  drag(w, doc, OPEN, 'progress');
  await wait(50);
  const during = {armed: slack.isConnected && slack.dataset.confirm === '1', col: colOf(doc, OPEN), busy: busy(tileOf(doc, OPEN))};
  gate.open();
  await wait(120);

  //then
  assert.deepStrictEqual(during, {armed: true, col: 'open', busy: true}, 'the repaint waits while the other card has an armed Send to Slack; the moved card is locked meanwhile');
  assert.ok(slack.isConnected && slack.dataset.confirm === '1', 'and after the move');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a move waits to repaint while another card has an armed confirm';
}

async function sendToSlackKeepsItsCoolDown() {
  //given
  const {w, doc, errors, posts} = await boot();
  await openPanel(doc, OPEN);
  const slack = doc.querySelector('.card-drawer .acts button.slack-btn:not(.slack-upd)');

  //when
  slack.click();
  slack.click();
  await wait(150);

  //then
  assert.deepStrictEqual(posts.map((p) => p.type), ['slack']);
  assert.ok(calm(tileOf(doc, OPEN)), 'the card is released');
  assert.ok(slack.disabled && /Sent/.test(slack.textContent), 'the button stays off with Sent for its cool-down');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'Send to Slack keeps its short Sent cool-down after the lock is released';
}

async function aFailingRefreshAfterTheActionDoesNotFailIt() {
  //given
  const {w, doc, errors, posts} = await boot();
  await openPanel(doc, OPEN);
  w.__skopaxActions = () => { throw new Error('the list did not load'); };
  const mute = drawerBtn(doc, 'Mute 24 h');

  //when
  mute.click();
  await wait(120);

  //then
  assert.deepStrictEqual(posts.map((p) => p.type), ['mute']);
  assert.ok(!/Not saved/.test(mute.textContent), 'the action that went through is not shown as failed');
  assert.ok(calm(tileOf(doc, OPEN)));
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'an error while refreshing after an action keeps the action a success';
}

async function aMoveOfAnotherCardDuringTheRefreshKeepsTheFirstInPlace() {
  //given
  const {w, doc, errors, hold, srv} = await boot();
  const seen = [];
  doc.addEventListener('skopax:state', () => { const a = w.ALL.find((x) => x.key === OPEN); seen.push(w.boardColumn(a)); });
  const first = hold();
  drag(w, doc, OPEN, 'progress');
  await wait(30);
  const list = later();
  srv.actionsGate = list;
  srv.rev = 2;
  srv.state = state({rev: 2, open: {triage: 'progress', owner: 'op@example.com'}});
  first.open();
  await wait(30);

  //when
  const unmute = hold(), take = hold();
  drag(w, doc, MUTED, 'progress');
  await wait(50);
  const during = {open: colOf(doc, OPEN), busy: busy(tileOf(doc, OPEN)), other: colOf(doc, MUTED)};
  list.open();
  await wait(100);
  unmute.open();
  take.open();
  await wait(200);

  //then
  assert.deepStrictEqual(during, {open: 'progress', busy: true, other: 'progress'}, 'the second move repaints without snapping the first card back');
  assert.deepStrictEqual(seen.filter((c) => c !== 'progress'), [], 'the first card is never painted in its old column');
  assert.ok(colOf(doc, OPEN) === 'progress' && calm(tileOf(doc, OPEN)));
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a drag of another card while the first one waits for fresh data keeps the first one in place';
}

async function aDoneActionWithASlowRefreshIsReleasedQuietly() {
  //given
  const {w, doc, errors, posts, srv, timers} = await boot();
  await openPanel(doc, OPEN);
  const list = later();
  srv.actionsGate = list;
  drawerBtn(doc, 'Mute 24 h').click();
  await wait(30);
  const gets = srv.actionGets;

  //when
  timers.splice(0).forEach((fn) => fn());
  await wait(50);
  const toasts = (doc.getElementById('toasts') || {}).textContent || '';
  const released = calm(tileOf(doc, OPEN));
  list.open();
  await wait(100);

  //then
  assert.deepStrictEqual(posts.map((p) => p.type), ['mute']);
  assert.ok(released, 'the card is released at 30 s');
  assert.ok(!/Still working/.test(toasts), 'no Still working note, as the action is already done');
  assert.strictEqual(srv.actionGets, gets, 'no second refresh starts');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a done action whose refresh is slow is released at 30 s without a note or a second refresh';
}

(async () => {
  console.log('PASS ' + await aCardIsBusyWhileItsActionRuns());
  console.log('PASS ' + await aSecondClickOnABusyCardSendsNothing());
  console.log('PASS ' + await aDragMoveLocksTheCardUntilAllStepsAreDone());
  console.log('PASS ' + await aPollDuringAnActionDoesNotSnapTheCardBack());
  console.log('PASS ' + await otherCardsStayUsableDuringAnAction());
  console.log('PASS ' + await aFailedActionPutsTheCardBackAndSaysSo());
  console.log('PASS ' + await theTopBarShowsWhileAnyActionRuns());
  console.log('PASS ' + await aBusyCardIsReleasedAfterTheTimeout());
  console.log('PASS ' + await aCardStaysLockedUntilTheFreshStateArrives());
  console.log('PASS ' + await aPollFromBeforeTheMoveNeverPaintsTheOldColumn());
  console.log('PASS ' + await aTimedOutDragSendsNoFurtherSteps());
  console.log('PASS ' + await anArmedConfirmOnAnotherCardSurvivesAMove());
  console.log('PASS ' + await sendToSlackKeepsItsCoolDown());
  console.log('PASS ' + await aFailingRefreshAfterTheActionDoesNotFailIt());
  console.log('PASS ' + await aMoveOfAnotherCardDuringTheRefreshKeepsTheFirstInPlace());
  console.log('PASS ' + await aDoneActionWithASlowRefreshIsReleasedQuietly());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
