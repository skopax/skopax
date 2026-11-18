const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const ORDERS = 'err|orders-service|ERROR';
const CART = '5xx|checkout-service|5xx';
const LINK = 'https://skopax.dev.example.com/#a=err%7Corders-service%7CERROR';

function anomaly(key, sev, extra) {
  return Object.assign({key, svc: key.split('|')[1], label: key.split('|')[2], sev, status: 'open', now: 40, d1: 5, d7: 4, firstSeen: iso(30), lastSeen: iso(1), ns: 'commerce', src: 'prom', suspects: ['first suspect'], recommend: 'look at it'}, extra || {});
}

function stateAt(rev, draftText, headline, by) {
  return {
    rev,
    updatedAt: iso(0),
    sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sweptAt: '15:04', nextRun: '16:04',
    sources: {prom: 'ok', logs: 'ok', apm: 'ok', cf: 'ok', pmm: 'ok'},
    summary: {level: 'warn', headline: headline || 'h' + rev, points: [], at: iso(1)},
    anomalies: [
      anomaly(ORDERS, 'crit', draftText ? {slackDraft: {text: draftText, by: by || 'Claude', at: iso(5)}} : {}),
      anomaly(CART, 'warn'),
    ],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: [],
  };
}

async function boot(serve, me) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  vc.on('error', (e) => errors.push('console.error ' + e));
  const calls = [];
  const blob = JSON.stringify(serve.state).replace(/<\//g, '<\\/');
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', blob);
  const dom = new JSDOM(html, {
    url: 'https://skopax.dev.example.com/' + (serve.hash || ''),
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    virtualConsole: vc,
    beforeParse(w) {
      Object.defineProperty(w, 'scrollY', {configurable: true, get: () => 0});
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      Object.defineProperty(w.navigator, 'clipboard', {configurable: true, value: {writeText: async (t) => { (serve.copied = serve.copied || []).push(t); }}});
      w.fetch = async (path, opts) => {
        const p = String(path);
        const method = (opts && opts.method) || 'GET';
        calls.push({method, path: p.split('?')[0], body: opts && opts.body ? JSON.parse(opts.body) : undefined});
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok(Object.assign({loginEnabled: true, user: 'op@example.com', operator: true, slack: true, slackChannel: '#ops'}, me || {}));
        if (p.startsWith('/api/actions')) return ok([]);
        if (/^\/api\/deploy-reports\/[^/]+\/slack-posts/.test(p) && method === 'POST') return ok(serve.onRepPost(p.split('/')[3]));
        if (/^\/api\/deploy-reports\/[^/]+\/slack-hold/.test(p) && method === 'POST') return ok(serve.onRepHold(p.split('/')[3]));
        if (/^\/api\/deploy-reports\/[^/]+\/slack-draft/.test(p) && method === 'PUT') return ok(serve.onRepDraft(p.split('/')[3], JSON.parse(opts.body)));
        if (p.startsWith('/api/deploy-reports')) return ok(serve.reports || []);
        if (p.startsWith('/api/deploys')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: serve.rev});
        if (p.startsWith('/api/state')) return ok(serve.state);
        if (p.startsWith('/api/slack-drafts') && method === 'PUT') return ok(serve.onDraft(JSON.parse(opts.body)));
        if (p.startsWith('/api/slack-updates') && method === 'POST') return ok({id: 'u1', type: 'slack-update', status: 'done'});
        if (p.startsWith('/api/slack-holds') && method === 'POST') return ok(serve.onHold(JSON.parse(opts.body)));
        if (p.startsWith('/api/slack-posts') && method === 'POST') return ok(serve.onPost(JSON.parse(opts.body)));
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(300);
  return {dom, w: dom.window, doc: dom.window.document, errors, calls};
}

function armAt(state, minutes) {
  const a = state.anomalies[0];
  a.autoSend = {at: new Date(Date.now() + minutes * 60000).toISOString(), draftAt: a.slackDraft.at, draftHash: '0a1b2c3d4e5f6a7b'};
  return state;
}

const card = (doc, key) => [...doc.querySelectorAll('#anoms article.inc')].find((c) => c.dataset.key === key);
const navigated = (errors) => errors.some((e) => /navigation/i.test(e));
const visible = (w) => w.document.dispatchEvent(new w.Event('visibilitychange'));
const expanded = (c) => c.querySelector('.inc-title .fold').getAttribute('aria-expanded') === 'true' && !c.querySelector('.inc-more').hidden;
const expand = (c) => { if (!expanded(c)) c.querySelector('.inc-title .fold').click(); };

async function draftIsShownOnTheCard() {
  //given
  const text = 'Orders have returned 95 errors since 18:40.\nCustomer logins are affected, we are working on OPS-118.\n' + LINK;
  const serve = {rev: 5, state: stateAt(5, text)};

  //when
  const {doc, errors, dom} = await boot(serve);

  //then
  const orders = card(doc, ORDERS);
  const tag = orders.querySelector('.inc-more .inc-tags .slack-rec');
  assert.ok(tag && tag.textContent === 'Slack recommended', 'the expanded tags say Slack recommended');
  assert.ok(!orders.querySelector('.inc-head .slack-rec'), 'a draft that is not armed stays off the collapsed row');
  assert.ok(tag.title.startsWith(text + '\n\nClaude · '), 'tag tooltip carries the draft');
  assert.match(tag.title, / · full text and Edit in the expanded card$/, 'the tooltip points at the expanded card');
  const box = orders.querySelector('.sdraft');
  assert.ok(box && box.closest('.inc-more') && box.closest('.inc-more').hidden, 'draft text sits in the collapsed panel, the card stays compact');
  assert.ok(![...orders.children].some((c) => c.classList.contains('sdraft')), 'no draft box on the card face');
  expand(orders);
  assert.ok(!box.closest('.inc-more').hidden && box.closest('.inc-more > .inc-body'), 'expanding the card shows the draft in the analysis, no second fold to open');
  assert.match(box.querySelector('h5').textContent, /^Slack draft/);
  assert.strictEqual(box.querySelector('.sdraft-t').textContent, text, 'draft text shown as written, line breaks kept');
  assert.strictEqual(box.querySelector('.sdraft-t a').getAttribute('href'), LINK, 'card link is clickable');
  assert.match(box.querySelector('.sdraft-by').textContent, /^Claude · \d\d:\d\d$/, 'who and when');
  assert.ok(box.querySelector('.draft-form').hidden, 'edit form closed by default');
  assert.ok(!box.querySelector('.draft-edit').hasAttribute('aria-disabled'), 'operator can edit');
  const slack = orders.querySelector('.slack-btn');
  assert.ok(!orders.querySelector('.slack-upd'), 'no Post update on a card never posted');
  assert.strictEqual(slack.textContent, 'Send draft to Slack');
  assert.match(slack.title, /Slack draft on this card as the message/);
  slack.click();
  assert.strictEqual(slack.textContent, 'Post draft to #ops?', 'confirm names the draft');
  const copy = box.querySelector('.draft-copy');
  copy.click();
  await wait(50);
  assert.deepStrictEqual(serve.copied, [text], 'Copy puts the draft on the clipboard');
  assert.strictEqual(copy.textContent, 'Copied');
  const cart = card(doc, CART);
  assert.ok(!cart.querySelector('.sdraft') && !cart.querySelector('.slack-rec'), 'no tag or draft without a draft');
  assert.strictEqual(cart.querySelector('.slack-btn').textContent, 'Send to Slack');
  assert.match(cart.querySelector('.slack-btn').title, /^Post this anomaly/);
  assert.ok(!navigated(errors), errors.join(' | '));
  dom.window.close();
  return 'Slack recommended tag with the draft as tooltip, text + who/when + link + Copy inside the expanded card, button reads Send draft to Slack; plain cards unchanged';
}

async function editSurvivesALiveUpdateAndSaves() {
  //given
  const serve = {rev: 5, state: stateAt(5, 'first draft ' + LINK)};
  const {w, doc, errors, calls, dom} = await boot(serve);
  expand(card(doc, ORDERS));
  await wait(50);
  card(doc, ORDERS).querySelector('.draft-edit').click();
  const ta = card(doc, ORDERS).querySelector('textarea[id^="draft-"]');
  assert.strictEqual(ta.value, 'first draft ' + LINK, 'textarea starts with the draft');
  assert.strictEqual(doc.activeElement, ta, 'Edit focuses the textarea');
  ta.value = 'corrected draft';

  //when
  serve.rev = 6;
  serve.state = stateAt(6, 'the new draft on the server', 'during typing');
  visible(w);
  await wait(300);

  //then
  assert.strictEqual(String((w.D.summary || {}).headline || '').endsWith('during typing'), false, 'no re-render while the textarea is focused');

  //when
  ta.blur();
  await wait(1300);

  //then
  assert.match(String((w.D.summary || {}).headline || ''), /during typing$/i, 'update lands once typing stops');
  const after = card(doc, ORDERS);
  const ta2 = after.querySelector('textarea[id^="draft-"]');
  assert.ok(expanded(after), 'the card stays expanded');
  assert.ok(!after.querySelector('.draft-form').hidden, 'edit form still open after the live update');
  assert.strictEqual(ta2.value, 'corrected draft', 'typed text kept over the server copy');
  assert.strictEqual(after.querySelector('.sdraft-t').textContent, 'the new draft on the server', 'box shows the server copy');

  //when
  after.querySelector('.slack-btn').click();
  after.querySelector('.slack-btn').click();
  await wait(100);

  //then
  assert.strictEqual(after.querySelector('.slack-btn').textContent, 'Save or cancel the draft edit first', 'no send while the edit is unsaved');
  assert.ok(!calls.some((c) => c.path === '/api/slack-posts'), 'nothing posted');

  //given
  serve.onDraft = (body) => {
    serve.rev = 7;
    serve.state = stateAt(7, body.text, 'saved', 'op@example.com');
    return {rev: 7, slackDraft: {text: body.text, by: 'op@example.com', at: iso(0)}};
  };

  //when
  after.querySelector('.draft-save').click();
  await wait(400);

  //then
  const put = calls.filter((c) => c.method === 'PUT');
  assert.deepStrictEqual(put.map((c) => [c.path, c.body]), [['/api/slack-drafts', {key: ORDERS, text: 'corrected draft'}]], 'one PUT with key and text');
  const saved = card(doc, ORDERS);
  assert.ok(saved.querySelector('.draft-form').hidden, 'form closes after Save');
  assert.strictEqual(saved.querySelector('.sdraft-t').textContent, 'corrected draft');
  assert.match(saved.querySelector('.sdraft-by').textContent, /^op · \d\d:\d\d$/, 'editor shown as the writer');
  assert.match(String((w.D.summary || {}).headline || ''), /saved$/, 'save pulls the new state at once');
  assert.ok(!navigated(errors), errors.join(' | '));
  dom.window.close();
  return 'draft edit defers the update while typing, survives a live update with its text, Save PUTs and refreshes in place';
}

async function emptySaveRemovesTheDraftAndGuestsCannotEdit() {
  //given
  const serve = {rev: 5, state: stateAt(5, 'draft')};
  serve.onDraft = () => { serve.rev = 6; serve.state = stateAt(6, null); return {rev: 6, slackDraft: null}; };
  const {doc, dom} = await boot(serve);
  const orders = card(doc, ORDERS);
  orders.querySelector('.draft-edit').click();
  orders.querySelector('textarea').value = '   ';

  //when
  orders.querySelector('.draft-save').click();
  await wait(400);

  //then
  assert.ok(!card(doc, ORDERS).querySelector('.sdraft') && !card(doc, ORDERS).querySelector('.slack-rec'), 'draft and tag gone');
  assert.strictEqual(card(doc, ORDERS).querySelector('.slack-btn').textContent, 'Send to Slack', 'button back to the plain label');
  dom.window.close();

  //given
  const guest = await boot({rev: 5, state: stateAt(5, 'draft')}, {user: null, operator: false});

  //when
  const edit = card(guest.doc, ORDERS).querySelector('.draft-edit');

  //then
  assert.strictEqual(edit.getAttribute('aria-disabled'), 'true', 'Edit greyed out for guests');
  assert.ok(!card(guest.doc, ORDERS).querySelector('.draft-copy').hasAttribute('aria-disabled'), 'guests can still copy');
  assert.match(edit.title, /Log in/);
  assert.ok(card(guest.doc, ORDERS).querySelector('.sdraft-t').textContent === 'draft', 'guests still read the draft');
  guest.dom.window.close();
  return 'empty Save removes the draft and restores Send to Slack; guests read the draft but cannot edit it';
}

const HANDLED = 'restart|payments-api|restarts';
const SENT = 'gw-err|identity/api-gateway-public';

function boardAt(rev, extra) {
  const st = stateAt(rev, 'orders draft');
  st.anomalies.push(anomaly(HANDLED, 'warn', Object.assign({status: 'cleared', triage: 'handled', clearedAt: iso(20), slackDraft: {text: 'card draft', by: 'Claude', at: iso(25)}}, extra && extra.handled)));
  st.anomalies.push(anomaly(SENT, 'warn', {slackDraft: {text: 'gateway draft', by: 'Claude', at: iso(30)}, slackPost: {channel: 'C1', ts: '1.2', at: iso(10), by: 'op@example.com', draft: true}}));
  if (extra && extra.allSent) st.anomalies.forEach((a) => { if (a.slackDraft) a.slackPost = {channel: 'C1', ts: '1.3', at: iso(0), by: 'op@example.com', draft: true}; });
  return st;
}

async function pillFiltersWaitingDraftsAndSentOnesSaySo() {
  //given
  const serve = {rev: 5, state: boardAt(5)};
  const {w, doc, errors, dom} = await boot(serve);
  const chip = () => doc.querySelector('#tab-sub-anoms button[data-f="slack"]');
  const txt = (b) => b.querySelector('.lbl').textContent + ' ' + b.querySelector('.count').textContent;
  const sevGroup = [...doc.querySelectorAll('#tab-sub-anoms .tab-sub-g')].find((g) => (g.querySelector('.tab-sub-l') || {}).textContent === 'Lists');
  assert.ok(!doc.getElementById('tiles'), 'no pills on the overview any more');
  assert.strictEqual(txt(chip()), 'Slack drafts 1', 'the side menu item counts waiting drafts on live cards only (open or stopped-not-addressed)');
  assert.ok(!chip().classList.contains('zero') && chip().classList.contains('k-act'), 'a waiting draft is not faded');
  assert.deepStrictEqual([...sevGroup.querySelectorAll('button.tab-sub-i')].map((b) => b.dataset.f), ['slack', 'archived', 'closed'], 'it sits in the Lists group of the Anomalies sub menu, above Archived and Closed');
  assert.match(doc.getElementById('tab-anoms').title, / · 1 Slack draft$/, 'the Anomalies tab tooltip counts it: ' + doc.getElementById('tab-anoms').title);

  //when
  chip().click();

  //then
  assert.deepStrictEqual([...doc.querySelectorAll('#anoms article.inc')].map((c) => c.dataset.key), [ORDERS], 'filter shows the card whose draft is waiting');
  assert.strictEqual(chip().getAttribute('aria-pressed'), 'true', 'the menu item shows it is filtering');
  assert.match(doc.getElementById('filter-note').textContent, /^Showing Slack recommended1/);
  assert.match(doc.getElementById('filter-note').title, /1 Slack drafts/);

  //when
  doc.querySelector('#tab-sub-anoms button[data-f="all"]').click();

  //then
  const stopped = card(doc, HANDLED).querySelector('.slack-rec');
  assert.ok(stopped.textContent === 'Draft (stopped)' && stopped.classList.contains('k-muted'), 'handled card: draft marked stopped');
  assert.match(stopped.title, /out of date/);
  assert.strictEqual(card(doc, HANDLED).querySelector('.slack-btn').textContent, 'Send draft to Slack', 'button still says what a click would post');
  const sent = card(doc, SENT);
  const tag = sent.querySelector('.slack-rec');
  assert.match(tag.textContent, /^Sent to Slack \d\d:\d\d$/, 'sent draft tag');
  assert.ok(tag.classList.contains('k-good') && /by op/.test(tag.title), 'sent tag says who');
  assert.strictEqual(sent.querySelector('.slack-btn').textContent, 'Send again');
  assert.match(sent.querySelector('.slack-btn').title, /^Already posted \d\d:\d\d by op/);

  //when
  chip().click();
  serve.rev = 6;
  serve.state = boardAt(6, {allSent: true});
  visible(w);
  await wait(300);

  //then
  assert.strictEqual(txt(chip()), 'Slack drafts 0');
  assert.ok(chip().classList.contains('zero'), 'zero is faded');
  assert.match(doc.querySelector('#anoms').textContent, /No Slack drafts waiting\./, 'empty filter says so instead of a blank panel');
  assert.doesNotMatch(doc.getElementById('tab-anoms').title, /Slack draft/, 'no drafts, none in the tab tooltip');

  //when
  chip().click();

  //then
  assert.strictEqual(doc.querySelector('#tab-sub-anoms button[aria-pressed="true"]').dataset.f, 'open', 'a second click goes back to all open');
  assert.ok(!navigated(errors), errors.join(' | '));
  dom.window.close();
  return 'Slack drafts filter (Anomalies side sub menu, zero-faded, counted in the tab tooltip) counts waiting drafts on live cards and filters to them, a second click shows all open; handled ones say Draft (stopped); a sent draft shows Sent to Slack + Send again; an emptied filter says so';
}

async function postUpdateRepliesInTheThread() {
  //given
  const post = {channel: 'C1', ts: '1.2', at: iso(15), by: 'op@example.com', draft: true};
  const serve = {rev: 5, state: boardAt(5, {handled: {triage: 'progress', ticket: 'OPS-118', slackPost: post}})};
  const {w, doc, errors, calls, dom} = await boot(serve);
  doc.querySelector('#tab-sub-anoms button[data-f="progress"]').click();
  const c = card(doc, HANDLED);
  assert.strictEqual(c.querySelector('.slack-btn').textContent, 'Send again', 'Send stays the first Slack button');

  //when
  c.querySelector('.slack-upd').click();
  const ta = c.querySelector('textarea[id^="update-"]');

  //then
  assert.match(ta.value, /^In progress: OPS-118\. \d\d:\d\d it stopped firing\.$/, 'update prefilled from triage, ticket and clear time');

  //when
  ta.value = 'Fixed, OPS-118 is live.';
  ta.blur();
  serve.rev = 6;
  serve.state = boardAt(6, {handled: {triage: 'progress', ticket: 'OPS-118', slackPost: post}});
  visible(w);
  await wait(300);
  const after = card(doc, HANDLED);
  assert.ok(!after.querySelector('textarea[id^="update-"]').closest('.reason').hidden, 'update form kept open over a live update');
  assert.strictEqual(after.querySelector('textarea[id^="update-"]').value, 'Fixed, OPS-118 is live.', 'typed update kept');
  after.querySelector('.reason button.slack-btn').click();
  await wait(200);

  //then
  const sentCalls = calls.filter((x) => x.path === '/api/slack-updates');
  assert.deepStrictEqual(sentCalls.map((x) => [x.method, x.body]), [['POST', {key: HANDLED, text: 'Fixed, OPS-118 is live.'}]]);
  assert.ok(card(doc, HANDLED).querySelector('textarea[id^="update-"]').closest('.reason').hidden, 'form closes after posting');
  assert.ok(!navigated(errors), errors.join(' | '));
  dom.window.close();
  return 'Post update shows only on posted cards, prefills from triage/ticket/clear time, survives a live update and POSTs the thread reply';
}

async function armedDraftCountsDownAndHoldStopsIt() {
  //given
  const text = 'Orders have returned 95 errors since 18:40.\nCustomer logins are affected.\n' + LINK;
  const serve = {rev: 5, state: armAt(stateAt(5, text), 8)};
  serve.onHold = (body) => {
    const next = armAt(stateAt(6, text), 8);
    next.anomalies[0].autoSend.holdBy = 'op@example.com';
    next.anomalies[0].autoSend.holdAt = new Date().toISOString();
    serve.rev = 6;
    serve.state = next;
    return {autoSend: next.anomalies[0].autoSend, key: body.key};
  };

  //when
  const {w, doc, errors, calls, dom} = await boot(serve);

  //then
  const tag = () => card(doc, ORDERS).querySelector('.slack-rec');
  assert.match(tag().textContent, /^Auto-send \d\d:\d\d$/, 'the tag counts down instead of saying Slack recommended');
  assert.ok(tag().classList.contains('armed'));
  assert.ok(tag().closest('.inc-title'), 'an armed auto-send sits on the collapsed row');
  assert.ok(tag().title.startsWith(text + '\n\nClaude · '), 'tooltip keeps the draft text');
  assert.match(tag().title, /posts itself to Slack within 30 s of \d\d:\d\d unless someone presses Hold; full text and Edit in the expanded card$/);
  assert.strictEqual(doc.querySelector('#tab-sub-anoms button[data-f="slack"] .count').textContent, '1', 'an armed draft is still a waiting Slack draft');
  const hold = () => card(doc, ORDERS).querySelector('.notif .hold-btn');
  assert.ok(hold(), 'the card offers Hold');
  assert.strictEqual(hold().previousElementSibling, card(doc, ORDERS).querySelector('.slack-btn'), 'Hold sits right after Send draft to Slack');
  assert.strictEqual(hold().getAttribute('aria-disabled'), null, 'operators can hold');
  assert.match(hold().title, /^Stop the auto-send, which posts the draft within 30 s/);
  assert.ok(!card(doc, CART).querySelector('.hold-btn'), 'no Hold without an armed draft');

  //when
  hold().click();
  await wait(300);

  //then
  assert.deepStrictEqual(calls.filter((c) => c.path === '/api/slack-holds').map((c) => [c.method, c.body]), [['POST', {key: ORDERS}]]);
  assert.strictEqual(tag().textContent, 'Slack recommended', 'a held draft is a normal manual draft again');
  assert.ok(tag().closest('.inc-tags') && !card(doc, ORDERS).querySelector('.inc-head .slack-rec'), 'and moves off the row into the expanded tags');
  assert.match(tag().title, /Auto-send held by op \d\d:\d\d; it only goes out with the button now\.$/);
  assert.ok(!hold(), 'the Hold button goes once it is held');
  assert.strictEqual(card(doc, ORDERS).querySelector('.slack-btn').textContent, 'Send draft to Slack', 'Send still posts it');
  assert.ok(!navigated(errors), errors.join(' | '));
  dom.window.close();

  //given
  const due = {rev: 5, state: armAt(stateAt(5, text), -1)};
  const guest = await boot(due, {user: null, operator: false});

  //then
  const g = card(guest.doc, ORDERS);
  assert.strictEqual(g.querySelector('.inc-title .slack-rec').textContent, 'Auto-send now', 'a due draft says now, not a past time');
  assert.strictEqual(g.querySelector('.notif .hold-btn').getAttribute('aria-disabled'), 'true', 'guests see Hold greyed out');
  assert.strictEqual(g.querySelector('.notif .hold-btn').title, 'Log in to use this', 'with the reason');
  assert.ok(!navigated(guest.errors), guest.errors.join(' | '));
  guest.dom.window.close();
  return 'an armed draft says Auto-send HH:MM (now once due) and offers Hold next to Send: Hold posts the key, turns the tag back into Slack recommended with who held it and when, and drops the button; guests see it greyed out with the reason';
}

const INFO = 'log|shop-web|WARN pattern';
const PROG = 'restarts|catalog-service|restarts';
const HELD = '5xx|api-gateway-public|5xx';
const EDITED = 'lag|logstash|Kafka lag';
const REPORT_ID = 'rp1';
const hhmm = (t) => new Date(t).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hour12: false});
const AUTO_SENDER = 'auto · Claude draft';
const rows = (doc) => [...doc.querySelectorAll('#slackq tbody tr')];
const cells = (tr) => [...tr.children].map((td) => td.textContent.trim());
const sqBtn = (tr, text) => [...tr.querySelectorAll('.sq-acts button')].find((b) => b.textContent.trim().startsWith(text));

function qAnomaly(key, sev, text, atMin, extra) {
  return anomaly(key, sev, Object.assign({slackDraft: {text, by: 'Claude', at: iso(atMin)}}, extra || {}));
}
function arm(x, minutes) {
  x.autoSend = {at: new Date(Date.now() + minutes * 60000).toISOString(), draftAt: x.slackDraft.at, draftHash: '0a1b2c3d4e5f6a7b'};
  return x;
}
function report(extra) {
  return Object.assign({id: REPORT_ID, createdAt: iso(30), summary: '2 deploys since 09:00 UTC: all clean', windowFrom: iso(50), windowTo: iso(35), rows: [], issues: []}, extra || {});
}
function queueState(rev) {
  const orders = arm(qAnomaly(ORDERS, 'crit', 'Orders have returned errors since 18:40.\nCustomer logins are affected.', 6), 8);
  const cart = arm(qAnomaly(CART, 'warn', 'Cart 5xx rate went up.', 5), 3);
  const info = qAnomaly(INFO, 'info', 'web-frontend config key warning continues.', 4);
  const prog = arm(qAnomaly(PROG, 'warn', 'catalog-service keeps restarting.', 10, {triage: 'progress', ticket: 'OPS-1'}), 2);
  const held = arm(qAnomaly(HELD, 'warn', 'Gateway 5xx went up.', 20), 6);
  held.autoSend.holdBy = 'op@example.com';
  held.autoSend.holdAt = iso(3);
  const edited = qAnomaly(EDITED, 'warn', 'Kafka lag is growing.', 2);
  edited.slackDraft.by = 'op@example.com';
  const s = stateAt(rev);
  s.anomalies = [orders, cart, info, prog, held, edited];
  return s;
}
function queuedReport() {
  return report({slackDraft: {text: 'Release 15.10 done, 2 services updated.', by: 'Claude', at: iso(7)},
    autoSend: {at: new Date(Date.now() + 5 * 60000).toISOString(), draftAt: iso(7), draftHash: 'r1'}});
}

async function slackTabQueuesEveryUnsentDraft() {
  //given
  const rep = queuedReport();
  rep.autoSend.draftAt = rep.slackDraft.at;
  const serve = {rev: 5, state: queueState(5), reports: [rep], hash: '#t=slack'};

  //when
  const {doc, errors, dom} = await boot(serve);
  await wait(100);

  //then
  assert.strictEqual(doc.querySelector('a.tab[data-tab][aria-current="page"]').dataset.tab, 'slack', '#t=slack opens the tab on a fresh load');
  assert.ok(!doc.getElementById('panel-slack').hidden, 'its panel is the visible one');
  const r = rows(doc);
  assert.deepStrictEqual(r.map((tr) => cells(tr)[0]), [
    'Warningcheckout-service / 5xx', 'ReportDeploy report · ' + hhmm(rep.windowFrom) + '–' + hhmm(rep.windowTo) + ' · clean', 'Criticalorders-service / ERROR',
    'Warninglogstash / Kafka lag', 'Infoshop-web / WARN pattern', 'Warningcatalog-service / restarts', 'Warningapi-gateway-public / 5xx',
  ], 'armed drafts first and soonest first (cards and reports together), then the rest newest first');
  assert.deepStrictEqual(r.map((tr) => cells(tr)[2]), [
    'Auto-send ' + hhmm(serve.state.anomalies[1].autoSend.at), 'Auto-send ' + hhmm(rep.autoSend.at), 'Auto-send ' + hhmm(serve.state.anomalies[0].autoSend.at),
    'Manual (edited by op)', 'Manual (info)', 'Stopped: card in progress, won’t auto-send', 'Held by op ' + hhmm(serve.state.anomalies[4].autoSend.holdAt),
  ], 'every row says what happens to it next');
  const stopped = r[5].children[2].querySelector('.state');
  assert.ok(stopped.classList.contains('k-warn'), 'the stopped one is amber, because it is the trap');
  assert.match(stopped.title, /skips a card that is in progress/);
  assert.ok(!r[5].querySelector('.sq-acts .hold-btn'), 'nothing to hold on a card that will not auto-send');
  assert.strictEqual(cells(r[2])[1], 'Orders have returned errors since 18:40.', 'the draft cell shows its first line');
  assert.ok(r[2].querySelector('.sq-d span').title.startsWith(serve.state.anomalies[0].slackDraft.text + '\n\nClaude · '), 'the full text is its tooltip');
  const tab = doc.getElementById('tab-slack');
  assert.strictEqual(doc.getElementById('count-slack').textContent, '7');
  assert.ok(tab.classList.contains('hot') && tab.classList.contains('k-info'), 'armed drafts make the tab hot and blue');
  assert.strictEqual(tab.title, '3 queued · next auto-send ' + hhmm(serve.state.anomalies[1].autoSend.at));
  assert.ok(!navigated(errors), errors.join(' | '));
  dom.window.close();

  //given
  const manual = queueState(5);
  manual.anomalies = manual.anomalies.filter((a) => !a.autoSend).slice(0, 2);
  const quiet = await boot({rev: 5, state: manual, reports: [report()]});
  const calm = await boot({rev: 5, state: stateAt(5), reports: []});

  //then
  assert.strictEqual(quiet.doc.getElementById('tab-slack').title, '2 waiting', 'without an armed one the tab counts what waits for a click');
  assert.ok(!quiet.doc.getElementById('tab-slack').classList.contains('hot'), 'and stays plain');
  assert.strictEqual(calm.doc.getElementById('tab-slack').title, 'no Slack drafts waiting');
  assert.strictEqual(calm.doc.getElementById('count-slack').textContent, '–');
  assert.match(rows(calm.doc)[0].textContent, /^No Slack drafts waiting/);
  quiet.dom.window.close();
  calm.dom.window.close();

  //given
  const guest = await boot({rev: 5, state: queueState(5), reports: [rep]}, {user: null, operator: false});

  //then
  const g = rows(guest.doc)[0];
  assert.deepStrictEqual([...g.querySelectorAll('.sq-acts button')].map((b) => [b.textContent.trim(), b.getAttribute('aria-disabled')]),
    [['Send now', 'true'], ['Hold', 'true'], ['Discard', 'true'], ['Edit', 'true'], ['Open', null]], 'guests see the acting buttons greyed out, Open still works');
  assert.strictEqual(sqBtn(g, 'Send now').title, 'Log in to use this', 'with the reason');
  guest.dom.window.close();
  return 'the Slack tab (#t=slack) queues every unsent draft on cards and deploy reports: armed first and soonest first, then newest first, each with its first line, full text as tooltip and what happens next — Auto-send HH:MM, Held by, Manual (info / edited) and the amber Stopped: card in progress; the tab is blue and hot with N queued · next auto-send, N waiting otherwise; guests see the buttons greyed out';
}

async function slackTabSendsHoldsAndDiscards() {
  //given
  const rep = queuedReport();
  const serve = {rev: 5, state: queueState(5), reports: [rep]};
  serve.onPost = (body) => {
    const next = queueState(6);
    next.anomalies.find((a) => a.key === body.key).slackPost = {channel: 'C1', ts: '1.1', at: new Date().toISOString(), by: 'op@example.com', draft: true};
    serve.rev = 6;
    serve.state = next;
    return {id: 's1', type: 'slack', status: 'done'};
  };
  serve.onHold = (body) => {
    const next = queueState(7);
    const a = next.anomalies.find((x) => x.key === body.key);
    a.autoSend.holdBy = 'op@example.com';
    a.autoSend.holdAt = new Date().toISOString();
    serve.rev = 7;
    serve.state = next;
    return {autoSend: a.autoSend};
  };
  serve.onDraft = (body) => {
    const next = queueState(8);
    delete next.anomalies.find((a) => a.key === body.key).slackDraft;
    serve.rev = 8;
    serve.state = next;
    return {rev: 8, slackDraft: null, autoSend: null};
  };
  serve.onRepPost = () => { serve.reports = [report()]; return {id: 's2', type: 'slack', status: 'done'}; };

  //when
  const {doc, calls, errors, dom} = await boot(serve);
  await wait(100);
  const orders = () => rows(doc).find((tr) => cells(tr)[0].endsWith('orders-service / ERROR'));
  const send = sqBtn(orders(), 'Send now');
  send.click();

  //then
  assert.strictEqual(send.textContent.trim(), 'Post to #ops?', 'the first click arms the confirm');
  assert.ok(send.dataset.confirm, 'so a live update waits for it');

  //when
  send.click();
  await wait(300);

  //then
  assert.deepStrictEqual(calls.filter((c) => c.path === '/api/slack-posts').map((c) => [c.method, c.body]), [['POST', {key: ORDERS}]], 'the second click posts the draft');
  assert.ok(!orders(), 'the sent draft leaves the queue');

  //when
  const cart = () => rows(doc).find((tr) => cells(tr)[0].endsWith('checkout-service / 5xx'));
  sqBtn(cart(), 'Hold').click();
  await wait(300);

  //then
  assert.deepStrictEqual(calls.filter((c) => c.path === '/api/slack-holds').map((c) => [c.method, c.body]), [['POST', {key: CART}]], 'Hold is the cancel for the timer');
  assert.strictEqual(cells(cart())[2], 'Held by op ' + hhmm(serve.state.anomalies[1].autoSend.holdAt), 'and the row says who held it');
  assert.ok(!sqBtn(cart(), 'Hold'), 'the button goes once it is held');

  //when
  const info = () => rows(doc).find((tr) => cells(tr)[0].endsWith('shop-web / WARN pattern'));
  const drop = sqBtn(info(), 'Discard');
  drop.click();

  //then
  assert.strictEqual(drop.textContent.trim(), 'Discard it?', 'Discard confirms too');

  //when
  drop.click();
  await wait(300);

  //then
  assert.deepStrictEqual(calls.filter((c) => c.path === '/api/slack-drafts').map((c) => [c.method, c.body]), [['PUT', {key: INFO, text: ''}]], 'an empty text clears the draft');
  assert.ok(!info(), 'and the row goes');

  //when
  const repRow = () => rows(doc).find((tr) => cells(tr)[0].startsWith('ReportDeploy report'));
  const repSend = sqBtn(repRow(), 'Send now');
  repSend.click();
  repSend.click();
  await wait(300);

  //then
  assert.deepStrictEqual(calls.filter((c) => c.path === '/api/deploy-reports/' + REPORT_ID + '/slack-posts').map((c) => [c.method, c.body]), [['POST', undefined]], 'a report posts on its own endpoint');
  assert.ok(!repRow(), 'and leaves the queue');
  assert.ok(!navigated(errors), errors.join(' | '));
  dom.window.close();
  return 'Send now (second click confirms) posts the card draft or the report draft on its own endpoint, Hold cancels the 5-minute timer and the row says who held it, Discard (second click confirms) clears the draft with an empty text, and every sent or discarded row leaves the queue';
}

async function slackTabJumpsToTheDraftAndListsWhatWentOut() {
  //given
  const sent = queueState(5);
  sent.anomalies[0].slackPost = {channel: 'C1', ts: '1.1', at: iso(90), by: 'token', draft: true};
  sent.anomalies[0].slackDraft.at = iso(200);
  sent.anomalies[1].slackPost = {channel: 'C1', ts: '1.2', at: iso(20), by: AUTO_SENDER, draft: true};
  sent.anomalies[1].slackDraft.at = iso(300);
  sent.anomalies[2].slackPost = {channel: 'C1', ts: '1.3', at: iso(2000), by: 'op@example.com', draft: false};
  const rep = report({slackPost: {channel: 'C1', ts: '1.4', at: iso(300), by: 'op@example.com', draft: false}});
  const serve = {rev: 5, state: sent, reports: [rep, queuedReport()]};

  //when
  const {w, doc, errors, dom} = await boot(serve);
  await wait(100);

  //then
  const out = [...doc.querySelectorAll('#slack-sent tbody tr')].map((tr) => cells(tr));
  assert.deepStrictEqual(out, [
    [hhmm(sent.anomalies[1].slackPost.at), 'Warningcheckout-service / 5xx', 'Claude · auto-sent', 'draft'],
    [hhmm(sent.anomalies[0].slackPost.at), 'Criticalorders-service / ERROR', 'Claude (on request)', 'draft'],
    [hhmm(rep.slackPost.at), 'ReportDeploy report · ' + hhmm(rep.windowFrom) + '–' + hhmm(rep.windowTo) + ' · clean', 'op', 'plain'],
  ], 'the last 24 hours of posts, newest first, with who sent it and whether the draft was the message');

  //when
  const prog = rows(doc).find((tr) => cells(tr)[0].endsWith('catalog-service / restarts'));
  sqBtn(prog, 'Edit').click();

  //then
  assert.strictEqual(doc.querySelector('a.tab[data-tab][aria-current="page"]').dataset.tab, 'anoms', 'Edit jumps to the card');
  const form = card(doc, PROG).querySelector('.sdraft .draft-form');
  assert.ok(form && !form.hidden, 'with its edit form open');
  assert.ok(expanded(card(doc, PROG)), 'on an expanded card, so the form is on screen');
  assert.strictEqual(form.querySelector('textarea').value, 'catalog-service keeps restarting.', 'holding the draft');
  form.querySelector('textarea').blur();

  //when
  w.showTab('slack');
  const repRow = rows(doc).find((tr) => cells(tr)[0].startsWith('ReportDeploy report'));
  sqBtn(repRow, 'Open').click();

  //then
  assert.strictEqual(doc.querySelector('a.tab[data-tab][aria-current="page"]').dataset.tab, 'reps', 'Open jumps to the report');
  assert.ok(doc.getElementById('rep-' + REPORT_ID).open, 'and opens its card');

  //given
  w.showTab('slack');
  const held = rows(doc).find((tr) => cells(tr)[0].endsWith('api-gateway-public / 5xx'));
  const confirm = sqBtn(held, 'Send now');
  confirm.click();

  //when
  const next = queueState(6);
  next.anomalies = next.anomalies.filter((a) => a.key !== EDITED);
  serve.rev = 6;
  serve.state = next;
  visible(w);
  await wait(1300);

  //then
  assert.ok(rows(doc).some((tr) => cells(tr)[0].endsWith('logstash / Kafka lag')), 'an armed confirm defers the live update');
  assert.strictEqual(sqBtn(rows(doc).find((tr) => cells(tr)[0].endsWith('api-gateway-public / 5xx')), 'Post').textContent.trim(), 'Post to #ops?', 'the confirm is still armed');

  //when
  await wait(4500);

  //then
  assert.ok(!rows(doc).some((tr) => cells(tr)[0].endsWith('logstash / Kafka lag')), 'once it expires the queue follows the new state');
  assert.strictEqual(rows(doc).length, 6, 'five cards and the report');
  assert.ok(!navigated(errors), errors.join(' | '));
  dom.window.close();
  return 'Edit opens the draft form on its card and Open jumps to the report, the Sent section lists the last 24 hours (auto, Claude on request, a person; draft or plain, older posts left out), and the queue survives a live update while an armed confirm defers it';
}

(async () => {
  for (const t of [draftIsShownOnTheCard, editSurvivesALiveUpdateAndSaves, emptySaveRemovesTheDraftAndGuestsCannotEdit, pillFiltersWaitingDraftsAndSentOnesSaySo, postUpdateRepliesInTheThread, armedDraftCountsDownAndHoldStopsIt, slackTabQueuesEveryUnsentDraft, slackTabSendsHoldsAndDiscards, slackTabJumpsToTheDraftAndListsWhatWentOut]) {
    console.log('PASS ' + (await t()));
  }
})().catch((e) => { console.error('FAIL', e.message); process.exit(1); });
