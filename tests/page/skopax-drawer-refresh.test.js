const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const hm = (at) => new Date(at).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hour12: false});
const A = 'log|checkout-service|ERROR|boom', B = 'log|payments-api|ERROR|npe', C = 'log|cart-service|ERROR|new';

function card(key, extra) {
  return Object.assign({key, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'ERROR lines', src: 'logs', sig: 'log', num: key === A ? 11 : key === B ? 12 : 13, now: 1, firstSeen: '2026-11-18T10:00:00Z', lastSeen: '2026-11-18T10:30:00Z', what: 'first text'}, extra || {});
}
function stateAt(rev, aExtra) {
  return {rev, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(A, aExtra), card(B)], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(serve) {
  const errors = [];
  const vc = new VirtualConsole(); vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE, {view: 'board'}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(serve.state)), {
    url: 'https://skopax.example.com/#t=anoms/all', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {}; w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        p = String(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me') && serve.hold) await serve.hold;
        if (p.startsWith('/api/me')) return ok(Object.assign({loginEnabled: true, user: 'op@example.com', operator: true, teams: [], tags: []}, serve.me || {}));
        if (p.startsWith('/api/actions')) return ok(serve.actions || []);
        if (p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: serve.rev});
        if (p.startsWith('/api/state')) return ok(serve.state);
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(350);
  return {w: dom.window, doc: dom.window.document, errors};
}
const refresh = async (w) => { w.document.dispatchEvent(new w.Event('visibilitychange')); await wait(350); };
const tile = (doc, key) => [...doc.querySelectorAll('#anoms .tile')].find((t) => t.dataset.key === key);

async function aRefreshKeepsTheOpenDrawerItsScrollFormMenuAndFocus() {
  //given
  const serve = {rev: 5, state: stateAt(5)};
  const {w, doc, errors} = await boot(serve);
  serve.rev = 5.5; serve.state = stateAt(5.5);
  await refresh(w);
  const tileB = tile(doc, B);
  tile(doc, A).click();
  const drawer = doc.querySelector('.card-drawer'), panel = drawer.querySelector('.cd-panel'), backdrop = drawer.querySelector('.cd-backdrop');
  panel.scrollTop = 120;

  //when
  serve.rev = 6; serve.state = stateAt(6);
  await refresh(w);

  //then
  assert.strictEqual(doc.querySelector('.card-drawer'), drawer, 'the same drawer node after a refresh with the same card');
  assert.strictEqual(drawer.querySelector('.cd-panel'), panel);
  assert.strictEqual(drawer.querySelector('.cd-backdrop'), backdrop);
  assert.strictEqual(panel.scrollTop, 120, 'the scroll position stays');
  assert.strictEqual(tile(doc, B), tileB, 'an unchanged board tile keeps its node');

  //given
  const inner = panel.querySelector('article.inc');
  [...panel.querySelectorAll('.acts button')].find((b) => b.textContent === 'Resolve').click();
  const why = panel.querySelector('.res-form input[id^="why-"]');
  why.value = 'rolled back';
  panel.querySelector('.res-form .res-kind[data-kind="handled"]').click();
  panel.querySelector('.res-form .res-kind[data-kind="handled"]').focus();

  //when
  serve.rev = 7; serve.state = stateAt(7, {what: 'second text'});
  await refresh(w);

  //then
  assert.strictEqual(doc.querySelector('.card-drawer'), drawer, 'the drawer stays when the card changed');
  assert.notStrictEqual(panel.querySelector('article.inc'), inner, 'the card inside is swapped');
  assert.ok(/second text/.test(panel.textContent) && !/first text/.test(panel.textContent), 'the note shows the new text');
  assert.strictEqual(panel.scrollTop, 120, 'the scroll position stays through a swap');
  assert.strictEqual(panel.querySelector('.res-form').hidden, false, 'the Resolve form stays open');
  assert.strictEqual(panel.querySelector('.res-form input[id^="why-"]').value, 'rolled back', 'with its typed text');
  assert.strictEqual(panel.querySelector('.res-form .res-kind[data-kind="handled"]').getAttribute('aria-pressed'), 'true', 'and its picked outcome');
  assert.strictEqual(doc.activeElement, panel.querySelector('.res-form .res-kind[data-kind="handled"]'), 'focus stays on the same button');

  //when
  panel.querySelector('.card-more').click();
  serve.rev = 8; serve.state = stateAt(8, {what: 'third text'});
  await refresh(w);

  //then
  assert.ok(/third text/.test(panel.textContent));
  assert.strictEqual(panel.querySelector('.card-more-box').hidden, false, 'the More menu stays open');
  assert.strictEqual(panel.querySelector('.card-more').getAttribute('aria-expanded'), 'true');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a refresh keeps the open drawer, its scroll, form, menu and focus, swaps only a changed card, and keeps unchanged board tiles');
}

async function aLeavingNeighbourLeavesTheOpenDrawerWorking() {
  //given
  const serve = {rev: 5, state: stateAt(5)};
  const {w, doc, errors} = await boot(serve);
  tile(doc, B).click();
  const drawer = doc.querySelector('.card-drawer');

  //when
  serve.rev = 6; serve.state = stateAt(6); serve.state.anomalies = serve.state.anomalies.filter((c) => c.key !== A);
  await refresh(w);

  //then
  assert.ok(!errors.some((e) => /navigation/i.test(e)), 'no page reload: ' + errors.join(' | '));
  assert.strictEqual(doc.querySelector('.card-drawer'), drawer, 'the same drawer node');
  assert.doesNotThrow(() => w.__skopaxRender(), 'the actions still resolve their card');
  assert.strictEqual(drawer.querySelector('.acts').dataset.key, B, 'by key, not by position');
  w.close();
  console.log('PASS a card leaving the list does not break the open drawer');
}

async function aCardInsertedAheadKeepsTheDrawerActionsOnItsOwnCard() {
  //given
  const act = {id: 'x1', type: 'dive', target: B, status: 'requested', requestedAt: iso(1), requestedBy: 'op@example.com', label: 'Investigate payments-api'};
  const serve = {rev: 5, state: stateAt(5), actions: [act]};
  const {w, doc} = await boot(serve);
  tile(doc, B).click();
  const drawer = doc.querySelector('.card-drawer');

  //when
  serve.rev = 6; serve.state = stateAt(6); serve.state.anomalies.unshift(card(C));
  await refresh(w);
  w.__skopaxRender();

  //then
  assert.strictEqual(doc.querySelector('.card-drawer'), drawer);
  assert.strictEqual(drawer.querySelector('.acts').dataset.key, B);
  assert.ok(/Queued/.test(drawer.querySelector('.last-act').textContent), 'the last action line shows the action of the open card: ' + drawer.querySelector('.last-act').textContent);
  assert.ok(/Investigate payments-api/.test(drawer.querySelector('.stat button').getAttribute('aria-label')), 'and its Cancel stays beside it');
  w.close();
  console.log('PASS a card inserted ahead leaves the open drawer on its own actions');
}

async function pageLevelInputsReachTilesAndTheOpenDrawer() {
  //given
  let release;
  const hold = new Promise((r) => { release = r; });
  const draft = {text: 'payments are failing', by: 'Claude', at: iso(5)};
  const serve = {rev: 5, state: stateAt(5), hold, me: {tags: [{id: 'pay', label: 'Payments team'}], features: {'slack-autosend': false}}};
  serve.state.anomalies[1] = card(B, {tags: [{id: 'pay', by: 'rule', at: iso(9)}], slackDraft: draft, autoSend: {at: '2026-11-18T14:35:00Z', draftAt: draft.at}});
  const {w, doc} = await boot(serve);
  tile(doc, B).click();
  const drawer = doc.querySelector('.card-drawer');
  const before = [tile(doc, B).querySelector('.biz-tag').textContent, drawer.querySelector('.sdraft-status').textContent];

  //when
  release();
  await wait(300);

  //then
  assert.deepStrictEqual(before, ['pay', 'Posting to Slack now'], 'before /api/me the page knows no labels and no switch');
  assert.strictEqual(tile(doc, B).querySelector('.biz-tag').textContent, 'Payments team', 'the tile follows the tag labels from /api/me');
  assert.strictEqual(doc.querySelector('.card-drawer'), drawer, 'the same drawer');
  assert.strictEqual(drawer.querySelector('.sdraft-status').textContent, 'Not posted: auto-send is off', 'the open drawer follows the feature switch');
  w.close();
  console.log('PASS tag labels and the auto-send switch reach unchanged tiles and the open drawer');
}

async function aChildArrivingUnderTheOpenCardUpdatesItsMergedCount() {
  //given
  const serve = {rev: 5, state: stateAt(5)};
  const {w, doc} = await boot(serve);
  tile(doc, B).click();
  const drawer = doc.querySelector('.card-drawer');

  //when
  serve.rev = 6; serve.state = stateAt(6); serve.state.anomalies.push(card(C, {parent: B}));
  await refresh(w);

  //then
  assert.strictEqual(doc.querySelector('.card-drawer'), drawer);
  assert.ok(/Merged here \u00b7 same cause \(1\)/.test(drawer.textContent), 'the merged section appears: ' + drawer.textContent.slice(0, 80));
  w.close();
  console.log('PASS a child arriving under the open card shows in its merged section at once');
}

async function onlyTheAgeTextsMoveWhenTimePasses() {
  //given
  const serve = {rev: 5, state: stateAt(5)};
  serve.state.anomalies[1].firstSeen = iso(5);
  const {w, doc} = await boot(serve);
  serve.rev = 5.5; serve.state = stateAt(5.5); serve.state.anomalies[1].firstSeen = serve.state.anomalies[0].firstSeen = iso(5);
  await refresh(w);
  const t = tile(doc, B);
  const before = t.querySelector('.tile-age').textContent;
  tile(doc, B).click();
  const drawer = doc.querySelector('.card-drawer');
  const sinceLine = () => [...drawer.querySelectorAll('.inc-more .line span')].map((s) => s.textContent).find((x) => /^since /.test(x));
  const lineBefore = sinceLine();
  const real = w.Date.now;
  w.Date.now = () => real() + 3600000;

  //when
  w.ageTick();

  //then
  assert.strictEqual(before, '5 min');
  assert.strictEqual(t.querySelector('.tile-age').textContent, '1 h', 'the age text moves');
  assert.strictEqual(tile(doc, B), t, 'and the tile is not replaced');
  assert.ok(/, 5 min ago$/.test(lineBefore), 'the open drawer line starts at 5 min: ' + lineBefore);
  assert.ok(/, 1 h 5 min ago$/.test(sinceLine()), 'the open drawer since-line moves too: ' + sinceLine());
  assert.strictEqual(doc.querySelector('.card-drawer'), drawer, 'and the drawer is not replaced');
  w.close();
  console.log('PASS an age tick rewrites only the age texts');
}

async function everyTypedFieldOfAnOpenFormSurvivesARefresh() {
  //given
  const serve = {rev: 5, state: stateAt(5), me: {jira: true, jiraUrl: 'https://jira.example.com', task: {projects: ['OPS']}}};
  const {w, doc} = await boot(serve);
  tile(doc, B).click();
  const panel = doc.querySelector('.card-drawer .cd-panel');
  const menu = () => panel.querySelector('.card-more').click();
  const item = (t) => [...panel.querySelectorAll('.acts button')].find((b) => b.textContent === t);
  menu();
  item('Resolve').click();
  panel.querySelector('.res-form .res-kind[data-kind="duplicate"]').click();
  const dup = panel.querySelector('.res-form input[id^="dup-"]');
  dup.value = '12';
  dup.blur();
  item('Start progress').click();
  const tin = panel.querySelector('input[id^="ticket-"]'), note = panel.querySelector('input[id^="take-why-"]');
  tin.value = 'OPS-9';
  note.value = 'on it';
  doc.activeElement.blur();

  //when
  serve.rev = 6; serve.state = stateAt(6); serve.state.anomalies[1].what = 'second text';
  await refresh(w);

  //then
  const after = (sel) => panel.querySelector(sel);
  assert.ok(/second text/.test(panel.textContent), 'the card was swapped');
  assert.strictEqual(after('.res-form input[id^="dup-"]').value, '12', 'the duplicate field keeps its text');
  assert.strictEqual(after('.res-form .res-kind[data-kind="duplicate"]').getAttribute('aria-pressed'), 'true');
  assert.deepStrictEqual([after('input[id^="ticket-"]').value, after('input[id^="take-why-"]').value], ['OPS-9', 'on it'], 'the ticket and the note both stay');
  w.close();
  console.log('PASS every typed field of an open form survives a refresh');
}

(async () => {
  await aRefreshKeepsTheOpenDrawerItsScrollFormMenuAndFocus();
  await aLeavingNeighbourLeavesTheOpenDrawerWorking();
  await aCardInsertedAheadKeepsTheDrawerActionsOnItsOwnCard();
  await pageLevelInputsReachTilesAndTheOpenDrawer();
  await aChildArrivingUnderTheOpenCardUpdatesItsMergedCount();
  await onlyTheAgeTextsMoveWhenTimePasses();
  await everyTypedFieldOfAnOpenFormSurvivesARefresh();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
