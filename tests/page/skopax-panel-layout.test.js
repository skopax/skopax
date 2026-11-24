const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString().replace(/\.\d{3}Z$/, 'Z');
const MAIN = 'log|checkout-service|ERROR|boom', NOBIZ = 'log|payments-api|ERROR|npe', EMPTY = 'log|cart-service|ERROR|quiet', INFO = 'log|shipping-api|ERROR|slow', MUTED = 'log|search-api|ERROR|noisy';
const KID = 'log|checkout-service|ERROR|kid', ACT = 'log|orders-api|ERROR|stuck';
const SEEN = iso(30);

function card(key, num, extra) {
  return Object.assign({key, num, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'ERROR lines', src: 'logs', sig: 'log', now: 5, d1: 1, d7: 2, firstSeen: SEEN, lastSeen: iso(1)}, extra || {});
}
function state() {
  const main = card(MAIN, 11, {what: '5 ERROR lines in 10 min', recommend: 'Roll back the last deploy.', suspects: ['the 10:02 deploy'], knownCause: 'payment provider timeouts',
    business: {text: 'Some shoppers cannot pay.', why: 'Orders are lost', kind: 'action', by: 'rule', at: iso(20)},
    alreadyActive: {source: 'Prometheus', since: iso(900)}, cause: {kind: 'event', what: 'Big sale', firstSeen: SEEN},
    followUp: {doneWhen: 'errors stay at zero', checkedAt: iso(10), lastResult: 'not_done', closeAt: iso(-60), closeKind: 'fixed'},
    clearNote: {text: 'calm since the rollback', by: 'pod', at: iso(4)},
    screenshots: [{id: 's1', viewport: 'desktop', url: 'https://shop.example.com/', width: 1280, height: 800, at: iso(5)}],
    slackDraft: {text: 'Checkout errors are up', by: 'pod', at: iso(5)}});
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [main, card(KID, 17, {parent: MAIN, what: 'kid text'}), card(NOBIZ, 12, {what: 'NullPointerException in PaymentService'}), card(EMPTY, 13, {what: ''}),
      card(INFO, 15, {what: 'p95 is 3 s', business: {text: 'The shipping page is slow.', kind: 'info', by: 'rule', at: iso(20)}}), card(MUTED, 16, {what: 'noise', muted: 'too loud'}),
      card(ACT, 18, {what: 'orders are stuck in pending', business: {text: 'Restart the order export.', kind: 'action', by: 'rule', at: iso(20)}})],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}
const LIVE_OFF = {num: 420, key: 'k420', live: true, svc: 'refunds-api', label: 'Refunds slow', sev: 'warn', status: 'cleared', triage: 'progress', firstSeen: iso(900), lastSeen: iso(800), clearedAt: iso(800),
  followUp: {doneWhen: 'no refund errors', closeAt: iso(-60), closeKind: 'fixed'}};
const PAST = {num: 412, key: 'k412', live: false, svc: 'payments-api', label: 'Payments slow', sev: 'warn', status: 'cleared', outcome: 'cleared', firstSeen: iso(90000), lastSeen: iso(89000),
  last: {id: 'a1', type: 'mute', target: 'k412', status: 'done', requestedAt: iso(89500), doneAt: iso(89500), requestedBy: 'alice@example.com', result: 'muted until later'}};

async function boot(opts) {
  opts = opts || {};
  const errors = [], posts = [];
  const vc = new VirtualConsole(); vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE, {view: 'board'}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state())), {
    url: 'https://skopax.example.com/' + (opts.hash || '#t=anoms/all'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {}; w.Element.prototype.scrollIntoView = function () {};
      const kept = Object.assign({}, opts.initial || {});
      const fake = {getItem: (k) => (opts.store === 'throw' ? (() => { throw new Error('blocked'); })() : (k in kept ? kept[k] : null)),
        setItem: (k, v) => { if (opts.store === 'throw') throw new Error('blocked'); kept[k] = String(v); }, removeItem: (k) => { delete kept[k]; }};
      Object.defineProperty(w, 'localStorage', {value: fake, configurable: true});
      w.__kept = kept;
      w.fetch = async (p, init) => {
        p = String(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (init && init.method === 'POST' && p.startsWith('/api/actions')) { posts.push(JSON.parse(init.body)); return ok({id: 'x', status: 'requested'}); }
        if (p === '/api/cards/412') return ok(PAST);
        if (p === '/api/cards/420') return ok(LIVE_OFF);
        if (p.startsWith('/api/me')) return ok(Object.assign({loginEnabled: true, user: 'op@example.com', operator: true, teams: [], tags: [], routes: [{id: 'ops', name: 'ops-channel'}]}, opts.me || {}));
        if (p.startsWith('/api/actions')) return ok(opts.actions || []);
        if (p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/state')) return ok(state());
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors, posts};
}
const tileOf = (doc, key) => [...doc.querySelectorAll('#anoms .tile')].find((t) => t.dataset.key === key);
async function openCard(doc, key) {
  tileOf(doc, key).click();
  await wait(60);
  return doc.querySelector('.card-drawer article.inc');
}
const topHeads = (art) => [...art.querySelectorAll('h4, h5')].filter((h) => !h.closest('.tech-fold') && !h.closest('.cd-hist')).map((h) => h.textContent.trim());
const said = (n) => (n.getAttribute('aria-label') || n.textContent).trim();
const kids = (box) => [...box.querySelectorAll('button, a')].filter((b) => !b.closest('.reason, .asg-form, .jira-form, .sev-form, .res-form, .slack-st')).map(said);

async function theTopReadsPlainly() {
  //given
  const {w, doc, errors} = await boot();

  //when
  const art = await openCard(doc, MAIN);

  //then
  const heads = topHeads(art);
  assert.deepStrictEqual(heads.slice(1, 3), ['What happened', 'What to do'], heads.join(' | '));
  assert.ok(heads[0].includes('checkout-service'), 'the title comes first: ' + heads[0]);
  const num = art.querySelector('.inc-head .num-link');
  assert.deepStrictEqual([num.textContent.trim().startsWith('#11'), num.getAttribute('href'), num.hasAttribute('target')], [true, '#c=11', false], 'the number opens the card in place');
  assert.ok(!/alone/.test(num.title), num.title);
  assert.strictEqual(art.querySelector('.card-what p').textContent, 'Some shoppers cannot pay.');
  assert.strictEqual(art.querySelector('.card-todo p').textContent, 'Roll back the last deploy.');
  assert.ok(!art.querySelector('.card-what').closest('.tech-fold') && !art.querySelector('.card-todo').closest('.tech-fold'));
  const st = art.querySelector('.card-state');
  assert.ok(st && st.textContent === 'Open' && /^Open \u00b7 first seen /.test(st.title), st && st.textContent + ' / ' + st.title);
  const close = art.querySelector('.follow-close');
  assert.ok(close && /^Closes as Fixed at /.test(close.textContent), 'the announced close shows at the top');
  assert.ok(art.querySelector('.tech-fold .what') && art.querySelector('.tech-fold .what').textContent === '5 ERROR lines in 10 min', 'the technical what moves into the fold');
  assert.ok(!art.querySelector('.rec-line:not(.cause-line):not(.already-active)'), 'no second Next line');
  assert.ok(!/Recommendation/.test(topHeads(art).join(' ')));
  assert.ok(art.querySelector('.biz .biz-txt').hidden, 'the business block does not repeat the sentence as read-only text');
  assert.strictEqual(art.querySelector('.biz textarea').value, 'Some shoppers cannot pay.', 'the editor still holds it');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the top reads number and title, a state line, What happened (the business text) and What to do';
}

async function noBusinessText() {
  //given
  const {w, doc, errors} = await boot();

  //when
  const art = await openCard(doc, NOBIZ);

  //then
  assert.deepStrictEqual(topHeads(art).slice(1, 2), ['What happened']);
  assert.strictEqual(art.querySelector('.card-what p').textContent, 'NullPointerException in PaymentService');
  assert.ok(art.querySelector('.card-what p.what'), 'the what paragraph keeps its class');

  //when
  doc.querySelector('.cd-close').click();
  const empty = await openCard(doc, EMPTY);

  //then
  assert.ok(!topHeads(empty).includes('What happened'), topHeads(empty).join(' | '));
  assert.strictEqual(empty.querySelector('.card-what'), null);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'with no business text What happened shows the what text, and with neither there is no empty heading';
}

async function noRecommendation() {
  //given
  const {w, doc, errors} = await boot();

  //when
  const art = await openCard(doc, INFO);

  //then
  assert.ok(!topHeads(art).includes('What to do'), topHeads(art).join(' | '));
  assert.strictEqual(art.querySelector('.card-todo'), null);
  assert.strictEqual(art.querySelector('.card-what p').textContent, 'The shipping page is slow.', 'an info note is what happened');
  assert.ok(art.querySelector('.biz .biz-txt').hidden);

  //when
  doc.querySelector('.cd-close').click();
  const act = await openCard(doc, ACT);

  //then
  assert.deepStrictEqual(topHeads(act).slice(1, 3), ['What happened', 'What to do']);
  assert.strictEqual(act.querySelector('.card-what p').textContent, 'orders are stuck in pending', 'with an action note the what text says what happened');
  assert.strictEqual(act.querySelector('.card-todo p').textContent, 'Restart the order export.', 'a recommended note is what to do when there is no recommendation');
  assert.ok(act.querySelector('.biz .biz-txt').hidden);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'with no recommendation an info note shows no What to do, and an action note becomes What to do';
}

async function fourMainButtons() {
  //given
  const {w, doc, errors, posts} = await boot();

  //when
  const art = await openCard(doc, MAIN);
  const main = art.querySelector('.acts .card-main');

  //then
  assert.deepStrictEqual([...main.children].map((b) => b.textContent.trim()), ['Take it', 'Assign', 'Resolve', 'Ask AI']);
  assert.strictEqual(main.querySelector('a').getAttribute('href'), '/#t=chat/new/11', 'Ask AI opens a chat about this card');

  //when
  main.children[0].click();
  await wait(60);

  //then
  assert.deepStrictEqual(posts.map((p) => [p.type, p.key, p.ticket]), [['take', MAIN, undefined]], 'Take it takes the card in one click');

  //when
  main.children[2].click();

  //then
  assert.ok(!art.querySelector('.acts .res-form').hidden, 'Resolve opens its form');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the opened card shows four main buttons: Take it, Assign, Resolve, Ask AI';
}

async function theRestIsUnderMore() {
  //given
  const {w, doc, errors, posts} = await boot();
  const art = await openCard(doc, MAIN);
  const more = art.querySelector('.acts button.card-more'), box = art.querySelector('.acts .card-more-box');

  //then
  assert.ok(more && box, 'a More button and its box');
  assert.deepStrictEqual([more.textContent.trim(), more.getAttribute('aria-expanded'), box.hidden], ['More', 'false', true]);
  assert.strictEqual(more.getAttribute('aria-controls'), box.id);
  assert.ok(more.classList.contains('free'), 'a viewer can open More too');

  //when
  more.click();

  //then
  assert.deepStrictEqual([more.getAttribute('aria-expanded'), box.hidden], ['true', false]);
  const names = kids(box);
  ['Mute 24 h', 'Mute 1 w', 'Known noise', 'Start progress', 'Investigate', 'Group', 'Change severity', 'Create Jira task', 'Link ticket', 'Copy link', 'Send to Slack', 'ops-channel', 'Keep open'].forEach((n) => assert.ok(names.includes(n), n + ' is under More: ' + names.join(', ')));
  assert.ok(!names.includes('Reopen'), 'an open card has nothing to reopen');
  const copy = [...box.querySelectorAll('button')].find((b) => said(b) === 'Copy link');
  assert.ok(!/alone/.test(copy.title) && /#11/.test(copy.title), copy.title);

  //when
  [...box.querySelectorAll('button')].find((b) => said(b) === 'Keep open').click();
  await wait(60);

  //then
  assert.deepStrictEqual(posts.map((p) => [p.type, p.key]), [['keep', MAIN]], 'Keep open stops the announced close');

  //when
  doc.querySelector('.cd-close').click();
  const mutedArt = await openCard(doc, MUTED);
  mutedArt.querySelector('.acts button.card-more').click();
  const mutedNames = kids(mutedArt.querySelector('.card-more-box'));

  //then
  assert.ok(mutedNames.includes('Reopen') && mutedNames.includes('Unmute'), mutedNames.join(', '));
  assert.ok(!mutedNames.includes('Mute 24 h'));

  //when
  [...mutedArt.querySelectorAll('.card-more-box button')].find((b) => said(b) === 'Reopen').click();
  await wait(60);

  //then
  assert.deepStrictEqual(posts.slice(1).map((p) => [p.type, p.key]), [['reopen', MUTED]]);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'More holds Mute, Start progress, Reopen when it applies, Investigate, Jira, routes, Copy link, Keep open and the rest';
}

async function aPastCardKeepsOnlyWhatMakesSense() {
  //given
  const {w, doc, errors} = await boot({hash: '#t=anoms'});

  //when
  w.location.hash = '#c=412';
  await wait(150);
  const art = doc.querySelector('.card-drawer article.inc');

  //then
  assert.deepStrictEqual([...art.querySelector('.card-main').children].map((b) => b.textContent.trim()), ['Ask AI']);
  art.querySelector('button.card-more').click();
  assert.deepStrictEqual(kids(art.querySelector('.card-more-box')), ['Copy link'], 'a past card gets no Take it, Start progress, Resolve or Reopen');
  assert.ok(art.querySelector('.card-state').textContent === 'Closed' && /^Closed \u00b7 last seen /.test(art.querySelector('.card-state').title), art.querySelector('.card-state').title);
  assert.ok(!/Stopped firing/.test(art.querySelector('.inc-head').textContent), 'a past card does not read as stopped firing');
  const last = art.querySelector('.last-act');
  assert.ok(last && !last.hidden && last.querySelector('summary').title === 'Last action' && /alice/.test(last.textContent), 'the last action the card API sends shows: ' + (last && last.textContent));
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a past card opened by number shows Ask AI, Copy link under More, its last action, and reads Closed';
}

async function aSignedOutVisitorGetsALogInLink() {
  //given
  const {w, doc, errors} = await boot({me: {user: '', operator: false}});

  //when
  const art = await openCard(doc, MAIN);
  const hint = art.querySelector('.acts .login-hint');

  //then
  assert.ok(hint && !hint.hidden, 'the log-in hint shows');
  assert.strictEqual(hint.querySelector('a').getAttribute('href'), '/login?next=%2Fc%2F11', 'the card path survives the sign-in');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a signed-out visitor sees a log-in link that brings them back to the card';
}

async function technicalDetailsFold() {
  //given
  const {w, doc, errors} = await boot();
  const art = await openCard(doc, MAIN);
  const fold = art.querySelector('.tech-fold');

  //then
  assert.ok(fold && fold.tagName === 'DETAILS', 'a details element');
  assert.strictEqual(fold.open, false, 'closed by default');
  assert.strictEqual(fold.querySelector('summary').textContent.trim(), 'Technical details');
  ['.cause-line', '.known', '.already-active', '.line', '.sus', '.keyline', '.follow', '.card-shots', '.kids', '.card-runs', '.sdraft', '.clear-note'].forEach((s) => assert.ok(fold.querySelector(s), s + ' is in the fold'));
  assert.ok(/errors stay at zero/.test(fold.querySelector('.follow').textContent) && /Not done yet/.test(fold.querySelector('.follow').textContent));
  assert.ok(/calm since the rollback/.test(fold.querySelector('.clear-note').textContent));
  assert.ok(!art.querySelector('.last-act').closest('.tech-fold'), 'the last action stays in view');

  //when
  fold.open = true;
  fold.dispatchEvent(new w.Event('toggle'));
  doc.querySelector('.cd-close').click();
  const again = await openCard(doc, MAIN);

  //then
  assert.strictEqual(w.__kept['skopax.techOpen'], '1');
  assert.strictEqual(again.querySelector('.tech-fold').open, true, 'reopening the card keeps it open');

  //when
  again.querySelector('.tech-fold').open = false;
  again.querySelector('.tech-fold').dispatchEvent(new w.Event('toggle'));

  //then
  assert.strictEqual(w.__kept['skopax.techOpen'], '0');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'Technical details is folded by default, holds the engineering detail and remembers being opened';
}

async function mergedCardsFromTheApi() {
  //given
  const merged = Object.assign({}, PAST, {merged: [{num: 413, key: 'k413', svc: 'refunds-api', label: 'Refunds slow', sev: 'warn', status: 'cleared', what: 'refund errors'}]});
  const {w, doc, errors} = await boot({hash: '#t=anoms'});
  w.fetch = ((orig) => async (p, init) => (String(p) === '/api/cards/412' ? {ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(merged))} : orig(p, init)))(w.fetch);

  //when
  w.location.hash = '#c=412';
  await wait(150);
  const art = doc.querySelector('.card-drawer article.inc');

  //then
  const list = art.querySelector('.tech-fold .kids');
  assert.ok(list && /refunds-api/.test(list.textContent) && /refund errors/.test(list.textContent), 'the merged cards the API sends show');
  assert.strictEqual(list.querySelector('.num-link').getAttribute('href'), '#c=413');
  assert.ok(/\+1 merged/.test(art.textContent), 'the merged count counts the API list too');
  assert.ok(!/undefined|NaN/.test(list.textContent), list.textContent);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a card opened by number shows the merged cards the card API sends';
}

async function aQueuedActionLeavesTheChannelButtonsAlone() {
  //given
  const {w, doc, errors} = await boot({actions: [{id: 'q1', type: 'dive', target: MAIN, key: MAIN, status: 'requested', requestedAt: iso(1), requestedBy: 'op@example.com', label: 'Investigate'}]});
  const art = await openCard(doc, MAIN);
  await w.__skopaxActions();
  await wait(60);

  //when
  art.querySelector('button.card-more').click();
  const route = art.querySelector('.card-more-box .teams-btn');

  //then
  assert.ok(route, 'the channel button is under More');
  assert.strictEqual(route.disabled, false, 'a queued card action does not lock the channel buttons, which keep their own sending state');
  assert.strictEqual(art.querySelector('.card-main .take-it').disabled, true, 'the card actions wait for the queued one');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a queued action disables the card actions but leaves the channel buttons under More alone';
}

async function aBareCardReadsClosedWithoutAnEmptyDate() {
  //given
  const {w, doc, errors} = await boot({hash: '#t=anoms'});
  w.fetch = ((orig) => async (p, init) => (String(p) === '/api/cards/414' ? {ok: true, status: 200, json: async () => ({num: 414, key: 'log|gone|ERROR|y', live: false})} : orig(p, init)))(w.fetch);

  //when
  w.location.hash = '#c=414';
  await wait(150);
  const art = doc.querySelector('.card-drawer article.inc');

  //then
  assert.strictEqual(art.querySelector('.card-state').textContent, 'Closed');
  assert.ok(!/Not assessed yet/.test(art.textContent), 'a past card does not invite an investigation it cannot get');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a bare past card reads Closed with no empty date and no Investigate hint';
}

async function aLiveCardOffTheBoardCanBeKeptOpen() {
  //given
  const {w, doc, errors, posts} = await boot({hash: '#t=anoms'});

  //when
  w.location.hash = '#c=420';
  await wait(150);
  const art = doc.querySelector('.card-drawer article.inc');
  art.querySelector('button.card-more').click();

  //then
  assert.deepStrictEqual(kids(art.querySelector('.card-more-box')), ['Reopen', 'Keep open', 'Copy link']);

  //when
  [...art.querySelectorAll('.card-more-box button')].find((b) => said(b) === 'Keep open').click();
  await wait(80);

  //then
  assert.deepStrictEqual(posts.map((p) => [p.type, p.key]), [['keep', 'k420']]);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a live card loaded by number with an announced close offers Keep open under More';
}

async function aSignedOutVisitorGetsALogInLinkOnACardLoadedByNumber() {
  //given
  const {w, doc, errors} = await boot({hash: '#t=anoms', me: {user: '', operator: false}});

  //when
  w.location.hash = '#c=412';
  await wait(150);
  const hint = doc.querySelector('.card-drawer .acts .login-hint');

  //then
  assert.ok(hint && !hint.hidden, 'the log-in hint shows on a card loaded by number too');
  assert.strictEqual(hint.querySelector('a').getAttribute('href'), '/login?next=%2Fc%2F412');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a signed-out visitor sees the log-in link on a card loaded by number';
}

async function opensWide() {
  //given
  const {w, doc, errors} = await boot();

  //when
  await openCard(doc, MAIN);
  const drawer = doc.querySelector('.card-drawer');

  //then
  assert.ok(drawer.classList.contains('wide'), 'with no saved choice the panel opens wide');

  //when
  drawer.querySelector('.cd-expand').click();
  doc.querySelector('.cd-close').click();
  await openCard(doc, MAIN);

  //then
  assert.ok(!doc.querySelector('.card-drawer').classList.contains('wide'), 'a person who shrinks it keeps it narrow');
  assert.strictEqual(w.__kept['skopax.drawer.wide'], '0');
  assert.deepStrictEqual(errors, []);
  w.close();

  //given
  const blocked = await boot({store: 'throw'});

  //when
  await openCard(blocked.doc, MAIN);

  //then
  assert.ok(blocked.doc.querySelector('.card-drawer').classList.contains('wide'), 'blocked storage still opens wide');
  assert.deepStrictEqual(blocked.errors, []);
  blocked.w.close();
  return 'the panel opens wide by default and keeps a shrink';
}

async function theBodyHasGuttersAndMoreSitsOnTheButtonRow() {
  //given
  const css = require('fs').readFileSync(TEMPLATE, 'utf8');
  const rule = (selector) => { const re = new RegExp('(?:^|\\n)' + selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ' \\{([^}]*)\\}', 'g'); return [...css.matchAll(re)].map((m) => m[1]).join(' '); };

  //when
  const body = rule('.cd-details > *');
  const row = rule('.acts.acts-grouped.acts-panel');
  const more = rule('.acts-panel > .cm-wrap');

  //then
  assert.match(body, /padding-inline:\s*18px/, 'the details body keeps the header gutter');
  assert.match(row, /display:\s*flex/);
  assert.match(row, /flex-wrap:\s*wrap/, 'More wraps only when the row is too narrow');
  assert.match(more, /flex:\s*none/, 'More is a plain item on the button row');
  assert.match(rule('.acts .card-more-box'), /position:\s*absolute/, 'its menu opens over the panel');
  return 'the details body keeps the header gutter and More sits on the button row, wrapping only when narrow';
}

(async () => {
  for (const t of [theTopReadsPlainly, theBodyHasGuttersAndMoreSitsOnTheButtonRow, noBusinessText, noRecommendation, fourMainButtons, theRestIsUnderMore, aPastCardKeepsOnlyWhatMakesSense, aSignedOutVisitorGetsALogInLink, technicalDetailsFold, mergedCardsFromTheApi, aQueuedActionLeavesTheChannelButtonsAlone, aBareCardReadsClosedWithoutAnEmptyDate, aLiveCardOffTheBoardCanBeKeptOpen, aSignedOutVisitorGetsALogInLinkOnACardLoadedByNumber, opensWide]) {
    try { console.log('PASS ' + await t()); } catch (e) { console.log('FAIL ' + t.name + ': ' + (e && e.stack || e)); process.exitCode = 1; }
  }
  process.exit(process.exitCode || 0);
})();
