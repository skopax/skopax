const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const hhmm = (t) => new Date(t).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hour12: false});
const ORDERS = 'err|orders-service|ERROR';
const CART = '5xx|checkout-service|5xx';
const GW = 'gw-err|identity/api-gateway-public';
const EVIL = 'log|shop-web|WARN pattern';
const SLACK = 'https://example.slack.com/archives/C1/p';
const POSTED = {orders: iso(20), cart: iso(18), gw: iso(16), evil: iso(14), update: iso(10), note: iso(5)};

function anomaly(key, sev, extra) {
  return Object.assign({key, svc: key.split('|')[1], label: key.split('|')[2], sev, status: 'open', now: 40, d1: 5, d7: 4, firstSeen: iso(60), lastSeen: iso(1), ns: 'commerce', src: 'prom'}, extra || {});
}

function post(ts, at, permalink, extra) {
  return Object.assign({channel: 'C1', ts, at, by: 'op@example.com', draft: true}, permalink ? {permalink} : {}, extra || {});
}

function draft(at) {
  return {text: 'draft text', by: 'Claude', at};
}

function boardAt(rev, over) {
  const o = over || {};
  return {
    rev, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sweptAt: '15:04', nextRun: '16:04',
    sources: {prom: 'ok', logs: 'ok', apm: 'ok', cf: 'ok', pmm: 'ok'},
    summary: {level: 'warn', headline: 'h' + rev, points: [], at: iso(1)},
    anomalies: [
      anomaly(ORDERS, 'crit', {slackDraft: draft(iso(30)), slackPost: post('1.1', POSTED.orders, SLACK + '11', o.note ? {stateNote: o.note} : {})}),
      anomaly(CART, 'warn', {slackDraft: draft(iso(30)), slackPost: post('1.2', POSTED.cart, o.cartLink)}),
      anomaly(GW, 'warn', {slackDraft: draft(iso(30)), slackPost: post('1.3', POSTED.gw, SLACK + '13')}),
      anomaly(EVIL, 'warn', {slackDraft: draft(iso(30)), slackPost: post('1.4', POSTED.evil, 'javascript:alert(1)')}),
    ],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: [],
  };
}

function act(id, type, key, at, slack) {
  return {id, type, key, target: type + ':' + key, status: 'done', requestedBy: 'op@example.com', requestedAt: at, doneAt: at, draft: true, result: 'ok', slack};
}

const ACTIONS = [
  act('a1', 'slack', ORDERS, POSTED.orders, {channel: 'C1', ts: '1.1', permalink: SLACK + '11'}),
  act('a2', 'slack-update', ORDERS, POSTED.update, {channel: 'C1', ts: '1.6', permalink: SLACK + '16?thread_ts=1.1&cid=C1'}),
  act('a3', 'slack', CART, POSTED.cart, {channel: 'C1', ts: '1.2'}),
  act('a4', 'slack-update', CART, POSTED.update, {channel: 'C1', ts: '1.7'}),
  act('a5', 'slack', GW, POSTED.gw, {channel: 'C1', ts: '1.3'}),
  act('a6', 'slack', EVIL, POSTED.evil, {channel: 'C1', ts: '1.4', permalink: 'javascript:alert(1)'}),
];

function report(id, at, slackPost) {
  return {id, createdAt: at, summary: id + ' deploys', windowFrom: at, windowTo: at, rows: [{svc: 'checkout-service', ns: 'shop', fromImage: 'a', toImage: 'b', at, flags: []}], slackPost};
}

const REPORTS = [report('r1', iso(40), post('2.1', iso(12), SLACK + '21', {draft: false})), report('r2', iso(50), post('2.2', iso(11), null, {draft: false}))];

async function boot(serve) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(serve.state).replace(/<\//g, '<\\/'));
  const dom = new JSDOM(html, {
    url: 'https://skopax.dev.example.com/',
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    virtualConsole: vc,
    beforeParse(w) {
      Object.defineProperty(w, 'scrollY', {configurable: true, get: () => 0});
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'op@example.com', operator: true, slack: true, slackChannel: '#ops'});
        if (p.startsWith('/api/actions')) return ok(serve.actions || []);
        if (p.startsWith('/api/deploy-reports')) return ok(serve.reports || []);
        if (p.startsWith('/api/deploys')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: serve.state.rev});
        if (p.startsWith('/api/state')) return ok(serve.state);
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(300);
  return {dom, w: dom.window, doc: dom.window.document, errors};
}

const card = (doc, key) => [...doc.querySelectorAll('#anoms article.inc')].find((c) => c.dataset.key === key);
const visible = (w) => w.document.dispatchEvent(new w.Event('visibilitychange'));
const sentTag = (c) => c.querySelector('.slack-rec.sent');
const notify = (c) => c.querySelector('.notif .slack-st');
const links = (node) => [...node.querySelectorAll('a')].map((a) => [a.textContent, a.getAttribute('href'), a.getAttribute('target'), a.getAttribute('rel')]);

function opensSlack(a, href, what) {
  assert.ok(a, what + ': a link');
  assert.strictEqual(a.getAttribute('href'), href, what + ': to the Slack message');
  assert.strictEqual(a.getAttribute('target'), '_blank', what + ': in a new tab');
  assert.strictEqual(a.getAttribute('rel'), 'noopener', what + ': without the opener');
}

async function cardSentTagAndNotifyRowLinkToSlack() {
  //given
  const serve = {state: boardAt(5), actions: ACTIONS};

  //when
  const {doc, errors, dom} = await boot(serve);

  //then
  const orders = card(doc, ORDERS);
  assert.strictEqual(sentTag(orders).textContent, 'Sent to Slack ' + hhmm(POSTED.orders) + ' ↗', 'the sent tag says where it goes');
  opensSlack(sentTag(orders).querySelector('a'), SLACK + '11', 'the sent tag');
  assert.strictEqual(notify(orders).textContent, 'Thread update ' + hhmm(POSTED.update) + ' ↗ · Sent to Slack ' + hhmm(POSTED.orders) + ' ↗ · op · draft', 'the Notify row line');
  assert.deepStrictEqual(links(notify(orders)), [
    ['Thread update ' + hhmm(POSTED.update) + ' ↗', SLACK + '16?thread_ts=1.1&cid=C1', '_blank', 'noopener'],
    ['Sent to Slack ' + hhmm(POSTED.orders) + ' ↗', SLACK + '11', '_blank', 'noopener'],
  ], 'the thread update and the post each open their own message');

  const cart = card(doc, CART);
  assert.strictEqual(sentTag(cart).textContent, 'Sent to Slack ' + hhmm(POSTED.cart), 'no link, the tag stays plain');
  assert.ok(!sentTag(cart).querySelector('a'));
  assert.strictEqual(notify(cart).textContent, 'Thread update ' + hhmm(POSTED.update) + ' · Sent to Slack ' + hhmm(POSTED.cart) + ' · op · draft', 'no link, the line stays plain');
  assert.ok(!notify(cart).querySelector('a'));

  const gw = card(doc, GW);
  opensSlack(notify(gw).querySelector('a'), SLACK + '13', 'an older send action takes the link stored on the card post');

  const evil = card(doc, EVIL);
  assert.ok(!sentTag(evil).querySelector('a') && !notify(evil).querySelector('a'), 'only https Slack links become links');
  assert.strictEqual(sentTag(evil).textContent, 'Sent to Slack ' + hhmm(POSTED.evil));
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the card sent tag and the Notify row "Sent to Slack" / "Thread update" open the Slack message in a new tab; an older send falls back to the card post link; without an https Slack link the text stays plain';
}

async function statusNoteShowsInTheNotifyRow() {
  //given
  const note = {state: 'handled', text: '✅ Solved: op · disk cleaned', at: POSTED.note, by: 'op', ts: '1.9', permalink: SLACK + '19?thread_ts=1.1&cid=C1'};
  const serve = {state: boardAt(5, {note}), actions: ACTIONS};

  //when
  const {doc, errors, dom} = await boot(serve);

  //then
  const line = notify(card(doc, ORDERS));
  assert.ok(line.textContent.startsWith('Status note ' + hhmm(POSTED.note) + ' ↗ · Thread update '), 'the newest thread reply leads: ' + line.textContent);
  const a = line.querySelector('a');
  opensSlack(a, note.permalink, 'the status note');
  assert.strictEqual(a.title, note.text, 'its tooltip is the note');
  assert.ok(!notify(card(doc, CART)).textContent.includes('Status note'), 'no note, no line');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a status note posted in the card thread shows first in the Notify row, linked, with the note as tooltip';
}

async function reportLineAndTagLinkToSlack() {
  //given
  const serve = {state: boardAt(5), actions: ACTIONS, reports: REPORTS};

  //when
  const {doc, errors, dom} = await boot(serve);

  //then
  const r1 = doc.getElementById('rep-r1'), r2 = doc.getElementById('rep-r2');
  const tag = r1.querySelector(':scope > summary .slack-rec.sent');
  assert.strictEqual(tag.textContent, 'Sent to Slack ' + hhmm(REPORTS[0].slackPost.at) + ' ↗');
  opensSlack(tag.querySelector('a'), SLACK + '21', 'the report tag');
  const st = r1.querySelector('.rep-acts .slack-st');
  assert.strictEqual(st.textContent, 'Sent to Slack ' + hhmm(REPORTS[0].slackPost.at) + ' ↗ · op');
  opensSlack(st.querySelector('a'), SLACK + '21', 'the report line');
  assert.ok(!r1.open, 'the link in the summary row does not unfold the report on render');
  assert.strictEqual(r2.querySelector(':scope > summary .slack-rec.sent').textContent, 'Sent to Slack ' + hhmm(REPORTS[1].slackPost.at));
  assert.strictEqual(r2.querySelector('.rep-acts .slack-st').textContent, 'Sent to Slack ' + hhmm(REPORTS[1].slackPost.at) + ' · op');
  assert.ok(!r2.querySelector('.slack-rec a, .rep-acts .slack-st a'), 'a report without a link stays plain');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the deploy report "Sent to Slack" tag and action-row line open the Slack message; without a link they stay plain';
}

async function slackTabSentListLinksTheTime() {
  //given
  const serve = {state: boardAt(5), actions: ACTIONS, reports: REPORTS};
  const {w, doc, errors, dom} = await boot(serve);

  //when
  w.showTab('slack');
  const rows = [...doc.querySelectorAll('#slack-sent tbody tr')];
  const when = (key) => rows.find((tr) => tr.children[1].textContent.includes(key)).children[0];

  //then
  assert.strictEqual(when('orders-service').textContent, hhmm(POSTED.orders) + ' ↗');
  opensSlack(when('orders-service').querySelector('a'), SLACK + '11', 'the Sent list time');
  assert.strictEqual(when('checkout-service').textContent, hhmm(POSTED.cart), 'no link, plain time');
  assert.ok(!when('shop-web').querySelector('a'), 'a non-Slack link stays plain');
  const reps = rows.filter((tr) => tr.children[1].textContent.startsWith('Report')).map((tr) => tr.children[0]);
  assert.deepStrictEqual(reps.map((td) => td.textContent), [hhmm(REPORTS[1].slackPost.at), hhmm(REPORTS[0].slackPost.at) + ' ↗'], 'report rows too, newest first');
  opensSlack(reps[1].querySelector('a'), SLACK + '21', 'a report row');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the Slack tab Sent list links each post time to its Slack message, plain without a link';
}

async function aLinkThatArrivesLaterShowsOnTheLiveUpdate() {
  //given
  const serve = {state: boardAt(5), actions: ACTIONS};
  const {w, doc, errors, dom} = await boot(serve);
  assert.ok(!sentTag(card(doc, CART)).querySelector('a'));

  //when
  serve.state = boardAt(6, {cartLink: SLACK + '12', note: {state: 'progress', text: '🛠️ Being worked on: op', at: POSTED.note, by: 'op', ts: '1.9', permalink: SLACK + '19'}});
  visible(w);
  await wait(400);

  //then
  opensSlack(sentTag(card(doc, CART)).querySelector('a'), SLACK + '12', 'the sent tag after the live update');
  const line = notify(card(doc, CART));
  assert.strictEqual(line.textContent, 'Thread update ' + hhmm(POSTED.update) + ' · Sent to Slack ' + hhmm(POSTED.cart) + ' ↗ · op · draft', 'the Notify row takes the card post link');
  opensSlack(line.querySelector('a'), SLACK + '12', 'the Notify row after the live update');
  opensSlack(notify(card(doc, ORDERS)).querySelector('a'), SLACK + '19', 'a status note that arrives later');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a link or a status note that arrives with a live state update shows on the tag and in the Notify row';
}

(async () => {
  for (const t of [cardSentTagAndNotifyRowLinkToSlack, statusNoteShowsInTheNotifyRow, reportLineAndTagLinkToSlack, slackTabSentListLinksTheTime, aLinkThatArrivesLaterShowsOnTheLiveUpdate]) {
    console.log('PASS ' + (await t()));
  }
})().catch((e) => { console.error('FAIL', e.message); process.exit(1); });
