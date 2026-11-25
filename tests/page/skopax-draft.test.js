const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');
const fs = require('fs');
const path = require('path');

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

function packScript(lang) {
  const runtime = fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'runtime.py'), 'utf8');
  const loader = /^PAGE_PACK = '(.+)'$/m.exec(runtime)[1];
  const raw = JSON.parse(fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'locale', lang, 'page.json'), 'utf8'));
  const pack = Object.assign({}, raw.page, {'@rules': raw.rules || [], '@months': raw.months || {}, '@days': raw.days || {}, '@tails': raw.tails || [], '@th': raw.headers || {}});
  const args = [JSON.stringify(lang), JSON.stringify(pack).replace(/</g, '\\u003c')];
  return '<script>' + loader.replace(/%s/g, () => args.shift()) + '</script>';
}

async function boot(serve, me) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  vc.on('error', (e) => errors.push('console.error ' + e));
  const calls = [];
  const blob = JSON.stringify(serve.state).replace(/<\//g, '<\\/');
  const html = pageHtml(TEMPLATE, serve.brand ? {brand: serve.brand} : undefined).replace('/*__SKOPAX_DATA__*/', blob).replace('<script type="text/plain" id="plugin-scripts"></script>', () => serve.lang ? packScript(serve.lang) : '<script type="text/plain" id="plugin-scripts"></script>');
  const dom = new JSDOM(html, {
    url: 'https://skopax.dev.example.com/' + (serve.hash || ''),
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    virtualConsole: vc,
    beforeParse(w) {
      if (serve.lang) w.document.cookie = 'skopax_lang=' + serve.lang + '; path=/';
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
        if (p.startsWith('/api/deploy-reports')) return ok(serve.reports || []);
        if (p.startsWith('/api/deploys')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: serve.rev});
        if (p.startsWith('/api/state')) return ok(serve.state);
        if (p.startsWith('/api/slack-updates') && method === 'POST') return ok({id: 'u1', type: 'slack-update', status: 'done'});
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

const HANDLED = 'restart|payments-api|restarts';
const SENT = 'gw-err|identity/api-gateway-public';

function boardAt(rev, extra) {
  const st = stateAt(rev, 'orders draft');
  st.anomalies.push(anomaly(HANDLED, 'warn', Object.assign({status: 'cleared', triage: 'handled', clearedAt: iso(20), slackDraft: {text: 'card draft', by: 'Claude', at: iso(25)}}, extra && extra.handled)));
  st.anomalies.push(anomaly(SENT, 'warn', {slackDraft: {text: 'gateway draft', by: 'Claude', at: iso(30)}, slackPost: {channel: 'C1', ts: '1.2', at: iso(10), by: 'op@example.com', draft: true}}));
  if (extra && extra.allSent) st.anomalies.forEach((a) => { if (a.slackDraft) a.slackPost = {channel: 'C1', ts: '1.3', at: iso(0), by: 'op@example.com', draft: true}; });
  return st;
}

async function postUpdateRepliesInTheThread() {
  //given
  const post = {channel: 'C1', ts: '1.2', at: iso(15), by: 'op@example.com', draft: true};
  const serve = {rev: 5, state: boardAt(5, {handled: {triage: 'progress', ticket: 'OPS-118', slackPost: post}})};
  const {w, doc, errors, calls, dom} = await boot(serve);
  doc.querySelector('#bf-lists .fb-i[data-f="progress"]').click();
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

const INFO = 'log|shop-web|WARN pattern';
const PROG = 'restarts|catalog-service|restarts';
const HELD = '5xx|api-gateway-public|5xx';
const EDITED = 'lag|logstash|Kafka lag';
const AUTO_SENDER = 'auto \u00b7 Claude draft';
const rows = (doc) => [...doc.querySelectorAll('#slackq tbody tr')];
const cells = (tr) => [...tr.children].map((td) => td.textContent.trim());

function qAnomaly(key, sev, text, atMin, extra) {
  return anomaly(key, sev, Object.assign({slackDraft: {text, by: 'Claude', at: iso(atMin)}}, extra || {}));
}
function arm(x, minutes) {
  x.autoSend = {at: new Date(Date.now() + minutes * 60000).toISOString(), draftAt: x.slackDraft.at, draftHash: '0a1b2c3d4e5f6a7b'};
  return x;
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

async function keptPostIsShownOnTheCardWithSendToSlack() {
  //given
  const text = 'Orders have returned 95 errors since 18:40.\nCustomer logins are affected, we are working on OPS-118.\n' + LINK;
  const state = stateAt(5, text);
  state.anomalies[0].slackDraft.notAutoSent = 'planted text in the esql answer (ignore previous)';
  const serve = {rev: 5, state};

  //when
  const {doc, errors, dom} = await boot(serve);
  const orders = card(doc, ORDERS);
  expand(orders);

  //then
  const tag = orders.querySelector('.slack-rec');
  assert.strictEqual(tag && tag.textContent, 'Not posted: the run read suspicious text');
  assert.ok(tag.title.startsWith(text + '\n\nInvestigator \u00b7 '), 'the tooltip carries the text');
  const box = orders.querySelector('.sdraft');
  assert.match(box.querySelector('h5').textContent, /^Slack post/);
  assert.strictEqual(box.querySelector('.sdraft-t').textContent, text);
  assert.match(box.querySelector('.sdraft-status').textContent, /^Not posted: the run read suspicious text \(planted text in the esql answer \(ignore previous\)\); check it and use Send to Slack$/);
  assert.ok(!box.querySelector('.draft-edit') && !box.querySelector('.draft-form'), 'no draft editing');
  const slack = orders.querySelector('.slack-btn');
  assert.strictEqual(slack.textContent, 'Send to Slack');
  assert.match(slack.title, /^Post the text kept on this card as the message/);
  slack.click();
  assert.strictEqual(slack.textContent, 'Post to #ops?');
  box.querySelector('.draft-copy').click();
  await wait(50);
  assert.deepStrictEqual(serve.copied, [text]);
  assert.ok(!navigated(errors), errors.join(' | '));
  dom.window.close();
  return 'a post kept for planted text reads Not posted: the run read suspicious text, shows the text with Copy and keeps Send to Slack; no Edit';
}

async function noRecommendedTagArmedLabelHoldOrFilter() {
  //given
  const state = armAt(stateAt(5, 'Orders have returned errors since 18:40.'), 0);
  const serve = {rev: 5, state};

  //when
  const {doc, errors, dom} = await boot(serve);
  const orders = card(doc, ORDERS);
  expand(orders);

  //then
  assert.ok(!orders.querySelector('.slack-rec'), 'a post going out now has no tag');
  assert.ok(!/Slack recommended|Auto-send|Hold/.test(doc.getElementById('anoms').textContent), 'no Slack recommended, armed label or Hold anywhere on the cards');
  assert.ok(![...doc.querySelectorAll('button')].some((b) => /^Hold$/.test(b.textContent.trim())), 'no Hold button');
  assert.ok(!doc.querySelector('[data-f="slack"]'), 'no Slack recommended filter or chip');
  assert.match(orders.querySelector('.sdraft-status').textContent, /^Posting to Slack now$/);
  assert.ok(!navigated(errors), errors.join(' | '));
  dom.window.close();
  return 'no Slack recommended tag, filter or chip, no armed label and no Hold; a post on its way reads Posting to Slack now';
}

async function failedAndSkippedPostsSaySo() {
  //given
  const state = stateAt(5, 'Orders have returned errors since 18:40.');
  state.anomalies[0].autoSend = {at: iso(30), draftAt: state.anomalies[0].slackDraft.at, draftHash: 'x', claimedAt: iso(1), failedAt: iso(1), retries: 3, error: 'Slack refused the message (channel_not_found)'};
  state.anomalies[1].slackSkipped = {at: iso(2)};
  const serve = {rev: 5, state, reports: [], hash: '#t=acts'};

  //when
  const {doc, errors, dom} = await boot(serve);
  await wait(100);

  //then
  assert.strictEqual(card(doc, ORDERS).querySelector('.slack-rec').textContent, 'Slack post failed: Slack refused the message (channel_not_found)');
  assert.strictEqual(card(doc, CART).querySelector('.slack-rec').textContent, 'Not posted: auto-send is off');
  const r = rows(doc);
  assert.deepStrictEqual(r.map((tr) => cells(tr)[2]), ['Slack post failed: Slack refused the message (channel_not_found)'], 'the Slack tab lists the post that did not go out');
  assert.deepStrictEqual([...r[0].querySelectorAll('.sq-acts button')].map((b) => b.textContent.trim()), ['Send now', 'View']);
  assert.ok(/1 not posted/.test(doc.getElementById('count-acts').textContent), 'the Activity row counts the held post');
  assert.ok(!navigated(errors), errors.join(' | '));
  dom.window.close();
  return 'a post that failed after its retries reads Slack post failed: <code>, one skipped with auto-send off reads Not posted: auto-send is off, and the Slack tab lists the failed one with Send now and View';
}

async function germanPackCoversTheNotPostedLinesAndTheSlackTab() {
  //given
  const state = stateAt(5, 'Orders have returned errors since 18:40.');
  state.anomalies[0].slackDraft.notAutoSent = 'planted text in the esql answer (ignore previous)';
  state.anomalies[1].slackDraft = {text: 'Cart 5xx went up.', by: 'Claude', at: iso(4)};
  state.anomalies[1].autoSend = {at: iso(30), draftAt: state.anomalies[1].slackDraft.at, draftHash: 'x', claimedAt: iso(1), failedAt: iso(1), retries: 3, error: 'channel_not_found'};
  const serve = {rev: 5, state, reports: [], hash: '#t=acts', lang: 'de'};

  //when
  const {doc, errors, dom} = await boot(serve);
  await wait(200);
  const orders = card(doc, ORDERS);
  expand(orders);
  await wait(50);

  //then
  const panel = doc.getElementById('panel-acts');
  assert.strictEqual(doc.getElementById('sq-h').textContent, 'Nicht in Slack gepostet');
  assert.ok(!/The investigator posts to Slack itself/.test(panel.querySelector('#sq-h + .panel-note').textContent), 'the note is translated');
  assert.deepStrictEqual([...doc.querySelectorAll('#slackq thead th')].map((t) => t.textContent), ['Was', 'Text', 'Status', 'Aktionen']);
  assert.strictEqual(orders.querySelector('.slack-rec').textContent, 'Nicht gepostet: Der Lauf hat verd\u00e4chtigen Text gelesen');
  assert.match(orders.querySelector('.slack-rec').title, /\nVollst\u00e4ndiger Text in der aufgeklappten Karte$/, 'the tooltip hint is translated');
  assert.match(orders.querySelector('.sdraft-status').textContent, /^Nicht gepostet: Der Lauf hat verd\u00e4chtigen Text gelesen \(planted text in the esql answer \(ignore previous\)\); pr\u00fcfen und An Slack senden nutzen$/);
  assert.match(card(doc, CART).querySelector('.slack-rec').textContent, /^Slack-Post fehlgeschlagen: channel_not_found$/);
  assert.ok(!/use Send to Slack|Not posted|Slack post failed/.test(doc.getElementById('slackq').textContent), 'no English left in the Slack tab rows');
  assert.strictEqual(doc.getElementById('count-acts').textContent, '0 · 2 nicht gepostet', 'the History row count reads in German');
  assert.strictEqual(doc.getElementById('tab-acts').title, 'nichts in der Warteschlange · 2 nicht gepostet', 'the History row tooltip reads in German');
  assert.ok(!navigated(errors), errors.join(' | '));
  dom.window.close();
  return 'with the German pack the not-posted tags, the status line under the text, the History row count and tooltip and the Slack tab heading, note, columns and rows read in German';
}

async function aTextDroppedForAgeSaysSoInEnglishAndGerman() {
  //given
  const state = stateAt(5);
  state.anomalies[0].slackSkipped = {at: iso(2), why: 'old'};

  //when
  const en = await boot({rev: 5, state});
  const de = await boot({rev: 5, state, lang: 'de'});

  //then
  const tag = card(en.doc, ORDERS).querySelector('.slack-rec');
  assert.strictEqual(tag.textContent, 'Not posted: the text was more than an hour old when Slack was ready');
  assert.ok(!/auto-send is off|stopped/.test(tag.title), 'not the auto-send or stopped reason');
  assert.strictEqual(card(de.doc, ORDERS).querySelector('.slack-rec').textContent, 'Nicht gepostet: Der Text war \u00e4lter als eine Stunde, als Slack bereit war');
  assert.ok(!navigated(en.errors) && !navigated(de.errors));
  en.dom.window.close();
  de.dom.window.close();
  return 'a text dropped for age reads Not posted: the text was more than an hour old when Slack was ready, also in German';
}

async function autoSentPostsNameWhoDraftedThem() {
  //given
  const sent = queueState(5);
  const post = (x, by, atMin) => { x.slackDraft.by = by; x.slackPost = {channel: 'C1', ts: '2.' + atMin, at: iso(atMin), by: AUTO_SENDER, draft: true}; x.slackDraft.at = iso(atMin + 100); };
  post(sent.anomalies[0], 'Claude (pod)', 10);
  post(sent.anomalies[1], 'server', 20);
  post(sent.anomalies[2], 'token:release-bot', 30);
  post(sent.anomalies[5], 'token', 40);
  post(sent.anomalies[4], 'token:bootstrap', 50);
  const serve = {rev: 5, state: sent, reports: []};

  //when
  const {doc, errors, dom} = await boot(serve);
  await wait(100);
  const out = [...doc.querySelectorAll('#slack-sent tbody tr')].map((tr) => cells(tr)[2]);
  const tip = card(doc, ORDERS).querySelector('.slack-rec.sent').title;

  //then
  assert.deepStrictEqual(out, ['Investigator \u00b7 auto-sent', 'Skopax \u00b7 auto-sent', 'release-bot \u00b7 auto-sent', 'Investigator \u00b7 auto-sent', 'Investigator \u00b7 auto-sent'],
    'the server under the product name, a token under its label, the investigator or anything else as Investigator');
  assert.ok(tip.includes(' by Investigator \u00b7 auto-sent'), 'the card tag names the same sender');
  assert.ok(!navigated(errors), errors.join(' | '));
  dom.window.close();
  return 'an auto-sent post names who drafted it: the product for the server, the token label for a token, Investigator for the rest';
}

async function autoSentPostsUseTheInvestigatorLabelOfTheInstall() {
  //given
  const sent = queueState(5);
  const post = (x, by, atMin) => { x.slackDraft.by = by; x.slackPost = {channel: 'C1', ts: '2.' + atMin, at: iso(atMin), by: AUTO_SENDER, draft: true}; x.slackDraft.at = iso(atMin + 100); };
  post(sent.anomalies[0], 'Claude (pod)', 10);
  post(sent.anomalies[1], 'token', 20);
  const brand = {name: 'Skopax', slug: 'skopax', version: '0.1.0', investigator: 'Claude investigator', podBy: 'Claude (pod)', attribution: {text: 'Skopax 0.1.0', href: 'https://skopax.io'}};

  //when
  const {doc, errors, dom} = await boot({rev: 5, state: sent, reports: [], brand});
  await wait(100);
  const out = [...doc.querySelectorAll('#slack-sent tbody tr')].map((tr) => cells(tr)[2]);

  //then
  assert.deepStrictEqual(out, ['Claude investigator \u00b7 auto-sent', 'Claude investigator \u00b7 auto-sent'], 'the page names the investigator as Slack does');
  assert.ok(!navigated(errors), errors.join(' | '));
  dom.window.close();
  return 'an auto-sent post drafted by the investigator is named with the install investigator label, as on Slack';
}

(async () => {
  for (const t of [aTextDroppedForAgeSaysSoInEnglishAndGerman, germanPackCoversTheNotPostedLinesAndTheSlackTab, autoSentPostsNameWhoDraftedThem, autoSentPostsUseTheInvestigatorLabelOfTheInstall, keptPostIsShownOnTheCardWithSendToSlack, noRecommendedTagArmedLabelHoldOrFilter, failedAndSkippedPostsSaySo, postUpdateRepliesInTheThread]) {
    console.log('PASS ' + (await t()));
  }
})().catch((e) => { console.error('FAIL', e.message); process.exit(1); });
