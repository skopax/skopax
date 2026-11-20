const assert = require('assert');
const {boot, card, iso, wait, until, article, expand} = require('./skopax-card-polish.test.js');

const TEMPLATE = process.argv[2];
const KEY = 'log|checkout-service|ERROR|boom';
const T = (min) => iso(min);

function fixture() {
  const c = card(KEY, {num: 12, firstSeen: T(300), tags: [{id: 'seo', by: 'rule', at: T(290)}], business: {text: 'Change the banner.', by: 'alice@example.com', at: T(280)},
    slackPost: {at: T(120), by: 'Claude', permalink: 'https://example.slack.com/archives/C1/p1'}, teamsPosts: [{at: T(110), route: 'seo', by: 'server'}]});
  const data = {
    actions: [
      {id: 'a3', type: 'resolve', target: KEY, status: 'done', requestedBy: 'bob@example.com', requestedAt: T(60), doneAt: T(60), result: 'Fixed', label: 'Resolve checkout-service'},
      {id: 'a2', type: 'severity', sev: 'warn', target: KEY, status: 'done', requestedBy: 'bob@example.com', requestedAt: T(200), doneAt: T(200), result: 'severity set to Warning by hand (the signal says Critical)'},
      {id: 'a1', type: 'take', target: KEY, status: 'done', requestedBy: 'alice@example.com', requestedAt: T(250), doneAt: T(250), label: 'Take checkout-service'},
      {id: 'a0', type: 'ticket', target: KEY, status: 'done', requestedBy: 'alice@example.com', requestedAt: T(240), doneAt: T(240), result: 'OPS-123'}],
    activity: [
      {id: 'v2', kind: 'tags', who: 'alice@example.com', t: T(100), key: KEY, text: 'untagged SEO \u00b7 ' + KEY},
      {id: 'v1', kind: 'assign', who: 'system', t: T(260), key: KEY, text: 'Auto-assigned to Nina White by the SEO tag \u00b7 ' + KEY}],
  };
  const runs = [{id: 'run-9', startedAt: T(230), outcome: 'done', recap: 'one client retried\nmore', keys: [KEY]}];
  return {c, data, runs};
}

async function historyRowsComeNewestFirstWithWhoWhenAndOldToNew() {
  //given
  const {w, errors} = await boot([card(KEY)]);
  const {c, data, runs} = fixture();
  //when
  const rows = JSON.parse(JSON.stringify(w.cardHistoryRows(c, data, runs)));
  const what = rows.map((r) => r.what);
  //then
  assert.deepStrictEqual(rows.map((r) => r.at), rows.map((r) => r.at).slice().sort().reverse(), 'newest first');
  assert.ok(rows.every((r) => r.at && r.who), 'every row says who and when');
  assert.deepStrictEqual(what, ['Resolved: Fixed', 'Untagged SEO', 'Posted to Teams: SEO', 'Posted to Slack', 'Severity changed', 'Investigation run: one client retried', 'Ticket created: OPS-123',
    'Started progress', 'Auto-assigned to Nina White by the SEO tag', 'Business note written', 'Tag added: SEO', 'Fired']);
  const sev = rows.filter((r) => r.kind === 'severity')[0];
  assert.deepStrictEqual([sev.what, sev.from, sev.to, sev.who], ['Severity changed', 'Critical', 'Warning', 'bob']);
  assert.strictEqual(rows.filter((r) => r.kind === 'run')[0].href, '/api/investigations/run-9', 'a run links its Run record');
  assert.strictEqual(rows[rows.length - 1].what, 'Fired', 'the oldest row is when it fired');
  assert.strictEqual(rows.filter((r) => r.what === 'Tag added: SEO').length, 1, 'one row per event');
  assert.ok(rows.some((r) => r.what === 'Auto-assigned to Nina White by the SEO tag' && r.who === 'system'));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS history rows come newest first from what the card records, with who, when and old to new for severity');
}

async function theDetailsAndHistoryTabsSwitchByClickAndKeyboardAndStayAcrossARepaint() {
  //given
  const {c, data} = fixture();
  const {w, doc, errors} = await boot([c], {history: data});
  const a = () => article(doc, KEY);
  expand(a());
  const tabs = () => [...a().querySelectorAll('[role="tablist"] [role="tab"]')];
  const first = tabs().map((t) => [t.textContent, t.getAttribute('aria-selected'), t.tabIndex]);
  const panel = a().querySelector('#' + tabs()[1].getAttribute('aria-controls'));
  //when
  tabs()[0].focus();
  tabs()[0].dispatchEvent(new w.KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true}));
  await until(() => a().querySelector('.cd-hist li'));
  const after = {sel: tabs().map((t) => t.getAttribute('aria-selected')), focus: doc.activeElement === tabs()[1], detailsHidden: a().querySelector('.cd-details').hidden,
    rows: a().querySelectorAll('.cd-hist li').length, first: a().querySelector('.cd-hist li .h-what').textContent, time: a().querySelector('.cd-hist li time').getAttribute('datetime')};
  w.renderAnoms();
  await wait(20);
  const kept = tabs().map((t) => t.getAttribute('aria-selected'));
  tabs()[1].dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Home', bubbles: true}));
  //then
  assert.deepStrictEqual(first, [['Details', 'true', 0], ['History', 'false', -1]]);
  assert.strictEqual(panel.getAttribute('role'), 'tabpanel');
  assert.deepStrictEqual(after.sel, ['false', 'true']);
  assert.strictEqual(after.focus, true, 'the arrow moves the focus');
  assert.strictEqual(after.detailsHidden, true);
  assert.ok(after.rows >= 8, 'rows from the card, its actions and its activity: ' + after.rows);
  assert.strictEqual(after.first, 'Resolved: Fixed');
  assert.strictEqual(after.time, data.actions[0].doneAt);
  assert.deepStrictEqual(kept, ['false', 'true'], 'a live repaint keeps the History tab');
  assert.deepStrictEqual(tabs().map((t) => t.getAttribute('aria-selected')), ['true', 'false'], 'Home goes back to Details');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the card has Details and History tabs, switched by click or arrows, that load the timeline and survive a live repaint');
}

async function historyRowsAreTranslatedButTheirDataStaysAsWritten() {
  //given
  const {c, data} = fixture();
  const {w, doc, errors} = await boot([c], {history: data, lang: 'de'});
  const a = () => article(doc, KEY);
  expand(a());
  //when
  a().querySelector('.cd-tab[data-tab="history"]').click();
  await until(() => a().querySelector('.cd-hist li'));
  await wait(30);
  const first = a().querySelector('.cd-hist li .h-what'), tag = [...a().querySelectorAll('.cd-hist li .h-what')].filter((x) => x.textContent.indexOf('SEO') >= 0).pop();
  //then
  assert.strictEqual(first.querySelector('.h-l').textContent, 'Gel\u00f6st', 'the label is in German');
  assert.strictEqual(first.querySelector('[data-no-tx]').textContent, 'Fixed', 'the result stays as written');
  assert.strictEqual(first.hasAttribute('data-no-tx'), false, 'only the data part is kept from translation');
  assert.strictEqual(tag.textContent, 'Tag hinzugef\u00fcgt: SEO');
  assert.strictEqual(a().querySelector('.cd-tab[data-tab="history"]').textContent, 'Verlauf');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS history rows are translated while tag names, results and people stay as written');
}

(async () => {
  for (const t of [historyRowsComeNewestFirstWithWhoWhenAndOldToNew, historyRowsAreTranslatedButTheirDataStaysAsWritten, theDetailsAndHistoryTabsSwitchByClickAndKeyboardAndStayAcrossARepaint]) {
    try { await t(); } catch (e) { console.error('FAIL ' + t.name + ': ' + e.message.replace(/\x1b\[[0-9;]*m/g, '').split('\n').slice(0, 30).join(' | ')); process.exitCode = 1; }
  }
  process.exit(process.exitCode || 0);
})();
