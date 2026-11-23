const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

function anomaly(key, sev, extra) {
  return Object.assign({key, svc: key.split('|')[1], label: key.split('|')[2], sev, status: 'open', now: 40, d1: 5, d7: 4, firstSeen: iso(30), lastSeen: iso(1), ns: 'commerce', src: 'prom', suspects: ['first suspect'], recommend: 'look at it'}, extra || {});
}

function stateAt(rev, opts) {
  const anomalies = [
    anomaly('err|orders-service|ERROR', 'warn', {now: opts.ordersNow}),
    anomaly('5xx|checkout-service|5xx', 'info', {owner: 'Jane Doe', ticket: 'OPS-1'}),
  ].concat(opts.extra || []);
  return {
    rev,
    updatedAt: iso(0),
    sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sweptAt: '15:04', nextRun: '16:04',
    sources: {prom: 'ok', logs: 'ok', apm: 'ok', cf: 'ok', pmm: 'ok'},
    summary: {level: opts.level || 'warn', headline: opts.headline, points: ['point one', 'point two'], at: iso(1)},
    anomalies,
    services: [{svc: 'orders-service', ns: 'commerce', kind: 'app', status: 'warn', req: 100, err: opts.ordersNow}, {svc: 'krakend', ns: 'gw', kind: 'gateway', status: 'ok', 'gw-req': 900}],
    history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: [],
  };
}

async function boot(serve) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  vc.on('error', (e) => errors.push('console.error ' + e));
  const calls = [];
  const scrolls = [];
  const blob = JSON.stringify(serve.state).replace(/<\//g, '<\\/');
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', blob);
  const dom = new JSDOM(html, {
    url: 'https://skopax.dev.example.com/',
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    virtualConsole: vc,
    beforeParse(w) {
      w.localStorage.setItem('skopax-open', JSON.stringify(['err|orders-service|ERROR']));
      if (serve.tab) w.localStorage.setItem('skopax-tab', serve.tab);
      Object.defineProperty(w, 'scrollY', {configurable: true, get: () => 420});
      w.scrollTo = (x, y) => scrolls.push([x, y]);
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path) => {
        const p = String(path);
        calls.push(p.split('?')[0]);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'op@example.com', operator: true, slack: true, slackChannel: '#ops', jira: true, jiraUrl: 'https://jira.example.com', task: {projects: ['OPS']}});
        if (p.startsWith('/api/actions')) return ok([]);
        if (p.startsWith('/api/deploy-reports')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: serve.rev});
        if (p.startsWith('/api/state')) return serve.stateStatus ? {ok: false, status: serve.stateStatus, json: async () => ({})} : ok(serve.state);
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(300);
  return {dom, w: dom.window, doc: dom.window.document, errors, calls, scrolls};
}

const card = (doc, key) => [...doc.querySelectorAll('#anoms article.inc')].find((c) => c.dataset.key === key);
const navigated = (errors) => errors.some((e) => /navigation/i.test(e));
const visible = (w) => w.document.dispatchEvent(new w.Event('visibilitychange'));
const expanded = (c) => c.querySelector('.inc-title .fold').getAttribute('aria-expanded') === 'true' && !c.querySelector('.inc-more').hidden;
const expand = (c) => { if (!expanded(c)) c.querySelector('.inc-title .fold').click(); };

async function liveUpdateKeepsTheView() {
  //given
  const serve = {rev: 5, state: stateAt(5, {headline: 'Orders errors up', ordersNow: 40})};
  const {w, doc, errors, calls, scrolls, dom} = await boot(serve);
  assert.ok(!expanded(card(doc, 'err|orders-service|ERROR')), 'a reload starts collapsed, even with an old skopax-open key in localStorage');
  expand(card(doc, 'err|orders-service|ERROR'));
  doc.querySelector('#tab-sub-anoms button[data-f="all"]').click();
  doc.querySelector('#svc-chips button[data-f="app"]').click();
  const knownBtn = [...card(doc, 'err|orders-service|ERROR').querySelectorAll('.acts button')].find((b) => b.textContent === 'Known noise');
  knownBtn.click();
  const reason = card(doc, 'err|orders-service|ERROR').querySelector('input[id^="reason-"]');
  reason.value = 'nightly batch';
  reason.blur();
  doc.getElementById('tab-svcs').click();
  const before = {svcTab: doc.getElementById('tab-svcs').getAttribute('aria-current'), anomsCount: doc.getElementById('count-anoms').textContent};

  //when
  serve.rev = 6;
  serve.state = stateAt(6, {headline: 'Orders errors up, cart 5xx new', ordersNow: 95, extra: [anomaly('restart|payments-api|restarts', 'crit')]});
  visible(w);
  await wait(300);

  //then
  assert.ok(!navigated(errors), 'no location.reload: ' + errors.join(' | '));
  assert.strictEqual(calls.filter((c) => c === '/api/state').length, 1, 'state fetched once');
  assert.strictEqual(doc.querySelector('#reco h2'), null, 'the Summary holds no headline text');
  assert.strictEqual(doc.querySelector('#reco details'), null, 'and no notes');
  assert.strictEqual(before.svcTab, 'page');
  assert.strictEqual(doc.getElementById('tab-svcs').getAttribute('aria-current'), 'page', 'Services tab still selected');
  assert.ok(!doc.getElementById('panel-svcs').hidden && doc.getElementById('panel-anoms').hidden, 'panels unchanged');
  assert.strictEqual(doc.querySelector('#tab-sub-anoms button[data-f="all"]').getAttribute('aria-pressed'), 'true', 'All filter kept');
  assert.match(doc.getElementById('filter-note').textContent, /^Showing All3/, 'the filter line follows the live update: ' + doc.getElementById('filter-note').textContent);
  assert.strictEqual(doc.querySelector('#svc-chips button[data-f="app"]').getAttribute('aria-pressed'), 'true', 'service kind chip kept');
  const orders = card(doc, 'err|orders-service|ERROR');
  assert.ok(expanded(orders), 'expanded card stays open');
  assert.match(orders.querySelector('.line').textContent, /now 95/, 'card shows the new value');
  const form = orders.querySelector('input[id^="reason-"]');
  assert.ok(form && !form.closest('.reason').hidden && form.value === 'nightly batch', 'known-noise form and its text kept');
  assert.ok(card(doc, 'restart|payments-api|restarts').classList.contains('linked'), 'new card highlighted');
  assert.ok(!orders.classList.contains('linked'), 'existing card not highlighted');
  assert.strictEqual(doc.getElementById('last-check'), null, 'no Last check or Updated flash in the top bar');
  assert.deepStrictEqual(scrolls[scrolls.length - 1], [0, 420], 'scroll position restored');
  assert.strictEqual(before.anomsCount, '2');
  assert.strictEqual(doc.getElementById('count-anoms').textContent, '3', 'tab count updated');
  assert.ok(orders.querySelector('.acts button').textContent === 'Investigate' && !orders.querySelector('.acts').hidden, 'action buttons re-wired');
  assert.strictEqual(doc.querySelectorAll('#pdc').length, 1, 'one post-deploy box');
  assert.ok(doc.getElementById('pdc').closest('#panel-reps') && !doc.getElementById('pdc').closest('#reps'), 'it sits in the Deploy reports header, outside the re-rendered list');
  assert.deepStrictEqual([...doc.querySelector('section.overview').children].map((c) => c.id), ['reco', 'meta'], 'the overview holds the summary and the source pills, also after a live update');
  assert.ok(!doc.getElementById('tiles') && !doc.querySelector('.pill'), 'no count pills');
  assert.strictEqual(doc.getElementById('tab-anoms').title, '1 critical \u00b7 1 warning', 'the Anomalies tab tooltip counts the new card and never information');
  assert.ok(doc.getElementById('tab-anoms').classList.contains('k-crit') && doc.getElementById('tab-anoms').classList.contains('hot'), 'and takes the worst open severity');
  assert.deepStrictEqual([...doc.querySelectorAll('#tab-sub-anoms button')].filter((b) => ['crit', 'warn', 'info', 'slack'].includes(b.dataset.f)).map((b) => b.querySelector('.lbl').textContent + ' ' + b.querySelector('.count').textContent), ['Critical 1', 'Warning 1'], 'the severity filters in the side menu follow the live update');
  await wait(4100);
  assert.ok(!card(doc, 'restart|payments-api|restarts').classList.contains('linked'), 'highlight is brief');
  dom.window.close();
  return 'live update re-renders in place: no reload, tab/filter/kind/expanded card/notes/form/scroll kept, new card highlighted, flash shown; the overview stays the summary and the source pills, the side menu severity filters, the filter line and the Anomalies tab colour follow';
}

async function typingAndConfirmDeferTheUpdate() {
  //given
  const serve = {rev: 5, state: stateAt(5, {headline: 'one', ordersNow: 40})};
  const {w, doc, errors, dom} = await boot(serve);
  const box = card(doc, 'err|orders-service|ERROR');
  const nowLine = () => card(doc, 'err|orders-service|ERROR').querySelector('.line').textContent;
  expand(box);
  box.querySelector('.acts .acts-tog').click();
  [...box.querySelectorAll('.acts button')].find((b) => b.textContent === 'Start progress').click();
  const ticket = box.querySelector('input[id^="ticket-"]');
  ticket.focus();

  //when
  serve.rev = 6;
  serve.state = stateAt(6, {headline: 'two', ordersNow: 50});
  visible(w);
  await wait(300);

  //then
  assert.match(nowLine(), /^now 401 d/, 'no re-render while typing');
  ticket.value = 'OPS-9';
  ticket.blur();
  await wait(1300);
  assert.match(nowLine(), /^now 501 d/, 'applied after typing ends');
  assert.strictEqual(card(doc, 'err|orders-service|ERROR').querySelector('input[id^="ticket-"]').value, 'OPS-9', 'typed ticket kept');

  //given
  const slack = card(doc, 'err|orders-service|ERROR').querySelector('.slack-btn');
  slack.click();
  assert.ok(slack.dataset.confirm, 'confirm armed');

  //when
  serve.rev = 7;
  serve.state = stateAt(7, {headline: 'three', ordersNow: 60});
  visible(w);
  await wait(1300);

  //then
  assert.match(nowLine(), /^now 501 d/, 'no re-render while confirm armed');
  await wait(4500);
  assert.match(nowLine(), /^now 601 d/, 'applied after the confirm expires');
  assert.ok(!navigated(errors), 'no reload: ' + errors.join(' | '));
  dom.window.close();
  return 'typing and an armed Send to Slack confirm defer the update; it lands when they end';
}

async function hiddenTabPausesAndStateFailureReloads() {
  //given
  const serve = {rev: 5, state: stateAt(5, {headline: 'one', ordersNow: 40})};
  const {w, doc, errors, calls, dom} = await boot(serve);
  let hidden = true;
  Object.defineProperty(doc, 'hidden', {configurable: true, get: () => hidden});
  serve.rev = 6;

  //when
  visible(w);
  await wait(200);

  //then
  assert.strictEqual(calls.filter((c) => c === '/api/rev').length, 0, 'no polling while hidden');

  //given
  serve.stateStatus = 401;
  hidden = false;

  //when
  visible(w);
  await wait(300);

  //then
  assert.strictEqual(calls.filter((c) => c === '/api/rev').length, 1, 'polls right away when visible');
  assert.ok(navigated(errors), 'falls back to location.reload when /api/state is refused');
  dom.window.close();
  return 'hidden tab skips polling, visibility polls at once, refused /api/state falls back to reload';
}

async function pastedCardLinkJumpsInAnOpenTab() {
  //given
  const handled = anomaly('restart|payments-api|restarts', 'warn', {status: 'cleared', triage: 'handled', clearedAt: iso(10)});
  const serve = {rev: 5, state: stateAt(5, {headline: 'one', ordersNow: 40, extra: [handled]})};
  const {w, doc, errors, dom} = await boot(serve);
  const pressed = () => doc.querySelector('#tab-sub-anoms button[aria-pressed="true"]').dataset.f;
  assert.strictEqual(pressed(), 'open', 'Anomalies opens on Open');
  doc.querySelector('#tab-sub-anoms button[data-f="open"]').click();
  assert.strictEqual(pressed(), 'open');
  assert.ok(!card(doc, handled.key), 'handled card is not in the Open view');

  //when
  w.location.hash = '#a=' + encodeURIComponent(handled.key);
  await wait(100);

  //then
  const c = card(doc, handled.key);
  assert.ok(c && c.classList.contains('linked') && expanded(c), 'card shown, ringed and expanded');
  assert.strictEqual(pressed(), 'taken', 'filter switched to where the card sits');
  assert.strictEqual(w.location.hash, w.cardHash(handled), 'the card hash stays in the address bar so it can be shared');

  //when
  w.location.hash = '#a=' + encodeURIComponent('gone|x|y');
  await wait(100);
  w.location.hash = '#a=' + encodeURIComponent('gone|x|y');
  await wait(100);

  //then
  assert.strictEqual(doc.querySelectorAll('#link-note').length, 1, 'one missing-card note, not one per paste');
  assert.ok(!doc.querySelector('#anoms article.inc.linked'), 'previous ring removed');
  assert.ok(!navigated(errors), 'no reload: ' + errors.join(' | '));
  dom.window.close();
  return 'a card link pasted into an open tab jumps to the card without a reload; a missing card shows one note';
}

async function summaryLabelInBothBranches() {
  //given
  const serve = {rev: 5, state: stateAt(5, {ordersNow: 40})};
  const {w, doc, errors, dom} = await boot(serve);
  const reco = () => doc.getElementById('reco');
  const links = () => [...reco().querySelectorAll('.brief-when .xlink')].map((b) => b.textContent);

  //then
  assert.deepStrictEqual([...reco().children].map((c) => c.className), ['brief-top'], 'one row and nothing else');
  assert.ok(reco().querySelector('.brief-top > .sev'), 'the state badge');
  assert.match(reco().querySelector('.sev').title, /^The state after the last server pass/, 'the badge says what it is');
  assert.strictEqual(reco().querySelector('h2, details, ul, .brief-by, .brief-fold'), null, 'no headline, notes, label or fold');
  assert.deepStrictEqual(links(), ['1 warning open']);

  //when
  serve.rev = 6;
  serve.state = stateAt(6, {headline: 'Release finished 05:28: 14 services (05:06\u201305:28; OPS and WEB); post-deploy check running;  nothing flagged.', ordersNow: 50});
  visible(w);
  await wait(300);

  //then
  assert.ok(!/Release finished|14 services|nothing flagged/i.test(reco().textContent), 'the written summary text is not shown: ' + reco().textContent);
  assert.deepStrictEqual(links(), ['1 warning open', 'Deploy reports \u203a'], 'a release in the summary leaves only its link');

  //when
  reco().querySelector('[data-reps]').click();
  await wait(50);

  //then
  assert.strictEqual(doc.getElementById('panel-reps').hidden, false, 'the link opens the Deploy reports tab');

  //when
  serve.rev = 8;
  serve.state = stateAt(8, {level: 'ok', headline: 'All clear at 15:15: the order-total WARN is input validation (handled); batch jobs now watched', ordersNow: 70});
  visible(w);
  await wait(300);
  reco().querySelector('[data-open-anoms]').click();
  await wait(50);

  //then
  assert.strictEqual(reco().querySelector('.sev').textContent, 'All clear', 'the badge follows the level');
  assert.strictEqual(doc.getElementById('panel-anoms').hidden, false, 'the open count opens the Anomalies tab');
  assert.ok(!navigated(errors), 'no reload: ' + errors.join(' | '));
  dom.window.close();
  return 'the Summary box is one row: the state badge and its links (open count, waiting, Deploy reports when a release is on), with no written text, across live updates';
}

const ORDERS = 'err|orders-service|ERROR';
const CART = '5xx|checkout-service|5xx';
const MUTED = 'lag|logstash|Kafka lag';
const CLEARED = 'restart|payments-api|restarts';
const TICKETED = '5xx|payments-api|5xx';
const inCard = (c, el) => { if (!el) return false; for (let n = el; n && n !== c; n = n.parentElement) if (n.hidden) return false; return true; };

function rowState(rev, ordersNow) {
  const st = stateAt(rev, {headline: 'rows', ordersNow});
  const draft = (x, min) => { x.slackDraft = {text: 'draft ' + x.svc, by: 'Claude', at: iso(5)}; x.autoSend = {at: iso(min), draftAt: x.slackDraft.at, draftHash: '0a1b2c3d4e5f6a7b', claimedAt: iso(min), failedAt: iso(1), retries: 3, error: 'channel_not_found'}; };
  const [orders, cart] = st.anomalies;
  Object.assign(orders, {triage: 'investigating', ticket: 'OPS-7', src: 'qan', kind: 'app'});
  draft(orders, 8);
  draft(cart, 9);
  st.anomalies.push(anomaly('err|orders-service|WARN', 'warn', {parent: ORDERS}));
  st.anomalies.push(anomaly(MUTED, 'warn', {status: 'cleared', clearedAt: iso(10), muted: 'nightly batch'}));
  st.anomalies.push(anomaly(CLEARED, 'info', {status: 'cleared', clearedAt: iso(12)}));
  st.anomalies.push(anomaly(TICKETED, 'warn', {triage: 'progress', ticket: 'OPS-8'}));
  return st;
}

async function cardsCollapseToOneRow() {
  //given
  const serve = {rev: 5, state: rowState(5, 40)};
  const {w, doc, errors, dom} = await boot(serve);
  doc.querySelector('#tab-sub-anoms button[data-f="all"]').click();
  const c = (key) => card(doc, key);
  const fold = (key) => c(key).querySelector('.inc-title .fold');
  const panel = (key) => c(key).querySelector('.inc-more');
  const isOpen = (key) => fold(key).getAttribute('aria-expanded') === 'true' && !panel(key).hidden && c(key).classList.contains('open');
  const rowTags = (key) => [...c(key).querySelectorAll('.inc-head .tag:not(.asg-tag)')].map((t) => t.textContent);
  const restTags = (key) => [...panel(key).querySelectorAll('.inc-tags .tag')].map((t) => t.textContent);

  //then
  for (const key of [ORDERS, CART, MUTED, CLEARED]) {
    const x = c(key);
    assert.ok(x && x.classList.contains('inc'), 'card ' + key + ' rendered');
    assert.ok(!isOpen(key), key + ' starts collapsed');
    assert.strictEqual(fold(key).tagName, 'BUTTON', 'the toggle is a real button');
    assert.strictEqual(fold(key).type, 'button');
    assert.strictEqual(doc.getElementById(fold(key).getAttribute('aria-controls')), panel(key), 'aria-controls names the panel');
    assert.ok(!fold(key).closest('summary') && !x.querySelector('.acts').closest('summary'), 'no toggle or icon inside a summary');
    assert.ok(!x.querySelector('.acts').closest('.inc-title'), 'icons sit outside the clickable title area');
    assert.deepStrictEqual([...x.querySelector('.inc-title').children].map((e) => e.className), ['c c-num', 'c c-sev', 'c c-name', 'c c-status', 'c c-asg', 'c c-tags', 'c c-chg', 'c c-age'], 'row: one cell per list column');
    assert.ok(x.querySelector('.c-sev .sev') && x.querySelector('.c-chg .chg'), 'row: the sev badge and the change chip sit in their columns');
    assert.strictEqual(x.querySelector('.inc-title h4').firstElementChild, fold(key), 'the title is the button');
    assert.strictEqual(rowTags(key).length, 1, key + ' row carries one status tag: ' + rowTags(key).join(' | '));
    assert.strictEqual(x.querySelector('.inc-title .chg').textContent, x.querySelector('.line .ch').textContent, 'row change chip matches the numbers line');
    assert.ok(x.querySelectorAll('.slack-rec').length <= 1, 'one Slack tag per card at most');
    for (const sel of ['.line', '.inc-body', '.notif']) assert.ok(x.querySelector(sel) && !inCard(x, x.querySelector(sel)), key + ' ' + sel + ' hidden while collapsed');
    assert.ok(![...x.querySelectorAll('summary')].some((s) => /^Analysis/.test(s.textContent)), 'no nested Analysis fold');
  }
  assert.strictEqual(rowTags(ORDERS)[0], 'Slack post failed: channel_not_found', 'a post that did not go out wins the row slot over triage');
  assert.match(restTags(ORDERS)[0], /^Investigating \u00b7 OPS-7/, 'triage moves into the panel');
  assert.deepStrictEqual(restTags(ORDERS).slice(1), ['App query', '+1 merged'], 'the other tags move into the panel');
  assert.strictEqual(rowTags(CART)[0], 'Slack post failed: channel_not_found', 'a post that did not go out takes the row slot without triage');
  assert.ok(c(CART).querySelector('.inc-title .slack-rec.not-posted'));
  assert.deepStrictEqual(restTags(CART), [], 'nothing else to show for cart');
  assert.strictEqual(rowTags(MUTED)[0], 'Muted', 'Muted before Cleared');
  assert.deepStrictEqual(restTags(MUTED).map((t) => t.replace(/\d{1,2} [A-Z][a-z]{2} \d\d:\d\d/, 'D Mon HH:MM')), ['Stopped firing D Mon HH:MM'], 'a muted card reads the short words with its time');
  assert.match(rowTags(CLEARED)[0], /^Stopped firing \d{1,2} [A-Z][a-z]{2} \d\d:\d\d$/, 'the cleared time carries its day');
  assert.ok(c(ORDERS).querySelector('.rec-line') && !inCard(c(ORDERS), c(ORDERS).querySelector('.rec-line')), 'the Next line is in the panel too');
  assert.ok(!c(ORDERS).querySelector('.acts').hidden && inCard(c(ORDERS), c(ORDERS).querySelector('.acts button.icon')), 'action icons stay on the collapsed row');

  //when
  c(ORDERS).querySelector('.inc-title .sev').click();

  //then
  assert.ok(isOpen(ORDERS), 'a click on the title area expands the card');
  for (const sel of ['.line', '.inc-tags', '.rec-line', '.inc-body', '.notif']) assert.ok(inCard(c(ORDERS), c(ORDERS).querySelector(sel)), sel + ' shown once expanded');
  const order = ['.line', '.inc-tags', '.rec-line', '.inc-body', '.notif'].map((s) => [...panel(ORDERS).querySelector('.cd-details').children].indexOf(panel(ORDERS).querySelector('.cd-details > ' + s)));
  assert.deepStrictEqual(order, [0, 1, 2, 3, 4], 'panel order: numbers, tags, Next, analysis, Notify');
  assert.match(panel(ORDERS).querySelector('.inc-body').textContent, /first suspect/, 'the analysis shows directly');
  assert.ok(!isOpen(CART), 'other cards stay collapsed');

  //when
  const copy = [...c(ORDERS).querySelectorAll('.acts button')].find((b) => b.getAttribute('aria-label') === 'Copy link');
  copy.click();
  const link = c(TICKETED).querySelector('.inc-title .tag a');
  link.addEventListener('click', (e) => e.preventDefault());
  link.click();

  //then
  assert.match(rowTags(TICKETED)[0], /^In progress \u00b7 OPS-8/, 'triage holds the row slot when no auto-send is armed');
  assert.ok(isOpen(ORDERS) && !isOpen(TICKETED), 'an action icon or a row tag link does not toggle the card');

  //when
  fold(ORDERS).click();

  //then
  assert.ok(!isOpen(ORDERS), 'the title button collapses it again');

  //when
  c(ORDERS).querySelector('.inc-title .chg').click();
  serve.rev = 6;
  serve.state = rowState(6, 95);
  visible(w);
  await wait(300);

  //then
  assert.ok(isOpen(ORDERS), 'the open state survives a live re-render');
  assert.match(c(ORDERS).querySelector('.line').textContent, /now 95/, 'with the new numbers');
  assert.ok(!isOpen(CART), 'a collapsed card stays collapsed');

  //when
  w.location.hash = '#a=' + encodeURIComponent(CART);
  await wait(100);

  //then
  assert.ok(c(CART).classList.contains('linked') && isOpen(CART), 'a deep-linked card opens expanded');

  //when
  serve.rev = 7;
  serve.state = rowState(7, 96);
  visible(w);
  await wait(300);

  //then
  assert.ok(isOpen(CART), 'and stays expanded over the next live update');
  assert.ok(!navigated(errors), errors.join(' | '));
  dom.window.close();
  return 'anomaly cards collapse to one row (chevron, sev, title button, one status tag: armed auto-send \u2192 triage \u2192 Muted \u2192 Cleared, change chip, icons); numbers, other tags, Next, analysis and Notify sit in the panel in that order; a title click toggles, icons and tag links do not; open state survives a re-render and a deep link opens the card expanded';
}

async function anomalyFiltersInTheSideMenu() {
  //given
  const cleared = {status: 'cleared', clearedAt: iso(60)};
  const extra = [
    anomaly('err|payments-api|ERROR', 'warn', cleared),
    anomaly('err|inventory-service|ERROR', 'warn', Object.assign({triage: 'progress', ticket: 'OPS-134'}, cleared)),
    anomaly('err|auth-service|ERROR', 'warn', Object.assign({triage: 'handled'}, cleared)),
    anomaly('err|sms-sender|ERROR', 'info', cleared),
  ];
  const serve = {rev: 5, tab: 'anoms', state: stateAt(5, {headline: 'h', ordersNow: 40, extra})};
  const {w, doc, errors, dom} = await boot(serve);
  const sub = doc.getElementById('tab-sub-anoms'), foldBtn = doc.getElementById('tab-fold-anoms');
  const items = () => [...sub.querySelectorAll('button[data-f]')];
  const item = (f) => sub.querySelector('button[data-f="' + f + '"]');
  const kOf = (b) => [...b.classList].find((c) => c.startsWith('k-'));
  const pressed = () => items().filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.dataset.f);
  const note = () => doc.getElementById('filter-note');
  const refresh = async (rev, more) => {
    serve.rev = rev;
    serve.state = stateAt(rev, {headline: 'h', ordersNow: 40, extra: extra.concat(more || [])});
    visible(w);
    await wait(300);
  };

  //then
  assert.ok(!sub.hidden && foldBtn.getAttribute('aria-expanded') === 'true' && foldBtn.getAttribute('aria-controls') === 'tab-sub-anoms', 'the sub menu opens by itself while Anomalies is the open tab');
  assert.deepStrictEqual([...doc.querySelectorAll('#snav .s-grp[data-grp="main"] a.tab')].map((a) => a.id), ['tab-anoms', 'tab-reqs', 'tab-chat', 'tab-inv'], 'Board, Requests, Chat and Investigator lead the sidebar');
  assert.ok(sub.closest('#tabs') && sub.previousElementSibling.contains(foldBtn) && sub.previousElementSibling.querySelector('.filters-l').textContent === 'Anomaly filters', 'the filters lead the side menu under their own heading, the arrow beside it');
  assert.deepStrictEqual(items().map((b) => b.dataset.f), ['all', 'open', 'crit', 'warn', 'progress', 'taken', 'muted', 'cleared', 'archived', 'closed'], 'All first, then status, with Open split by severity right under it; Lists; Triage only when above 0');
  assert.deepStrictEqual([...sub.querySelectorAll('.tab-sub-g')].map((g) => (g.querySelector('.tab-sub-l') || {textContent: ''}).textContent.replace(/\s*\d+$/, '')), ['', 'Lists', 'Assignee', 'Tags', 'Types'], 'four groups: status (severity nested under Open), Lists, the business tags and the card types (with their count)');
  assert.deepStrictEqual(items().map((b) => b.querySelector('.lbl').textContent), ['All', 'Open', 'Critical', 'Warning', 'In progress', 'Done', 'Muted', 'Stopped firing', 'Archived', 'Closed'], 'short labels');
  assert.deepStrictEqual(['taken', 'muted', 'cleared', 'all', 'crit', 'warn'].map((f) => kOf(item(f))), ['k-good', 'k-muted', 'k-cleared', 'k-act', 'k-crit', 'k-warn'], 'with their colours');
  assert.deepStrictEqual(['open', 'progress', 'taken', 'muted', 'cleared', 'all'].map((f) => item(f).querySelector('.count').textContent), ['3', '1', '1', '0', '1', '6'], 'Open counts the stopped card nobody acted on too, as the board does');
  assert.ok(item('crit').classList.contains('zero') && item('muted').classList.contains('zero') && !item('warn').classList.contains('zero'), 'a zero count is faded');
  assert.deepStrictEqual(pressed(), ['open'], 'the active filter is marked, Open by default');
  assert.ok(!doc.getElementById('filters') && !doc.getElementById('anom-chips') && !doc.getElementById('sev-chips') && !doc.getElementById('tri-chips') && !doc.querySelector('#panel-anoms .seg'), 'no chips row on the Anomalies tab any more');
  assert.strictEqual(note().textContent, 'Showing Open3Filters', 'one small line names the filter and its count');
  assert.ok(!doc.getElementById('clear-all'), 'no way back needed on All');
  assert.match(note().title, /^2 warning \u00b7 1 in progress \u00b7 1 done \u00b7 1 stopped firing/, 'the line tooltip counts every bucket: ' + note().title);
  const rules = [...doc.styleSheets].flatMap((sh) => [...sh.cssRules]);
  assert.ok(rules.some((r) => r.selectorText === '.tab-sub[hidden]' && r.style.getPropertyValue('display') === 'none'), 'a folded sub menu takes no space');
  const narrow = rules.filter((r) => r.cssRules && /max-width: 900px/.test(r.conditionText || (r.media && r.media.mediaText) || '')).flatMap((r) => [...r.cssRules]);
  assert.ok(rules.some((r) => r.selectorText === '.filter-note .fn-menu' && r.style.getPropertyValue('display') === 'none'), 'the Filters button is hidden on wide screens');
  assert.ok(narrow.some((r) => r.selectorText === '.filter-note .fn-menu' && r.style.getPropertyValue('display') === 'inline-flex'), 'and shown under 900 px of content, where the side menu is a drawer');
  assert.ok(rules.some((r) => r.selectorText === '.filter-note:not(.on)' && r.style.getPropertyValue('display') === 'none'), 'on a wide screen the line shows only while a filter other than All is on, like the old banner');
  assert.ok(narrow.some((r) => r.selectorText === '.filter-note:not(.on)' && r.style.getPropertyValue('display') === 'flex'), 'on a narrow screen it always shows, to carry the Filters button');

  //when
  doc.getElementById('tab-svcs').click();
  item('progress').click();

  //then
  assert.strictEqual(doc.getElementById('tab-anoms').getAttribute('aria-current'), 'page', 'a sub item opens the Anomalies tab');
  assert.ok(!doc.getElementById('panel-anoms').hidden && doc.getElementById('panel-svcs').hidden);
  assert.deepStrictEqual([...doc.querySelectorAll('#anoms article.inc')].map((c) => c.dataset.key), ['err|inventory-service|ERROR'], 'with that filter');
  assert.deepStrictEqual(pressed(), ['progress'], 'and marks it as the active one');
  assert.ok(note().classList.contains('on') && note().classList.contains('k-info'), 'the line takes the filter colour');
  assert.strictEqual(note().querySelector('.fn-cur').textContent, 'Showing In progress1', 'and names it with its count');

  assert.strictEqual(doc.getElementById('clear-all').textContent, 'Clear filters \u2715', 'the way back leads to Open');

  //when
  doc.getElementById('clear-all').click();

  //then
  assert.deepStrictEqual(pressed(), ['open'], 'Back to Open on the line goes back to Open');
  assert.ok(!note().classList.contains('on') && !doc.getElementById('clear-all'));

  //when
  item('warn').click();
  const warnOnly = pressed();
  item('warn').click();

  //then
  assert.deepStrictEqual([warnOnly, pressed()], [['warn'], ['open']], 'a second click on a severity goes back to Open, like the old chips');

  //when
  item('progress').click();
  foldBtn.click();
  await refresh(6, [anomaly('restart|payments-api|restarts', 'crit')]);

  //then
  assert.ok(sub.hidden && foldBtn.getAttribute('aria-expanded') === 'false', 'the arrow folds it and a live refresh keeps it folded');

  //when
  doc.getElementById('tab-svcs').click();
  foldBtn.click();
  await refresh(7, [anomaly('restart|payments-api|restarts', 'crit'), anomaly('restart|checkout-service|restarts', 'crit')]);

  //then
  assert.ok(!sub.hidden && foldBtn.getAttribute('aria-expanded') === 'true', 'unfolded by hand on another tab, it stays open over a live refresh');
  assert.ok(item('crit').querySelector('.count').textContent === '2' && !item('crit').classList.contains('zero'), 'with the new counts');
  assert.deepStrictEqual(pressed(), ['progress'], 'and the active filter still marked');

  //when
  foldBtn.click();
  doc.getElementById('tab-anoms').click();

  //then
  assert.ok(!sub.hidden, 'going back to Anomalies unfolds it again');
  assert.deepStrictEqual(pressed(), ['open'], 'a click on the Anomalies item itself resets the filter to Open');
  assert.strictEqual(w.location.hash, '#t=anoms');

  //when
  doc.getElementById('filter-menu').click();
  const drawer = doc.body.classList.contains('nav-open');
  item('progress').click();

  //then
  assert.ok(drawer && !doc.body.classList.contains('nav-open'), 'in the phone drawer a sub item closes the drawer like a section does');
  assert.deepStrictEqual(pressed(), ['progress']);

  //when
  foldBtn.click();
  doc.getElementById('filter-menu').click();

  //then
  assert.ok(doc.body.classList.contains('nav-open') && doc.getElementById('filter-menu').getAttribute('aria-expanded') === 'true', 'the narrow-screen Filters button opens the drawer');
  assert.ok(!sub.hidden, 'with the sub menu unfolded');
  assert.strictEqual(doc.activeElement, item('progress'), 'and the active filter focused');

  //when
  item('open').click();

  //then
  assert.ok(!doc.body.classList.contains('nav-open') && pressed()[0] === 'open', 'a pick closes it again');
  assert.ok(!navigated(errors), errors.join(' | '));
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the Anomalies filters live only in the side menu sub menu (labels, counts, colours, zero faded), no chips row on the tab; a click opens Anomalies with that filter and marks it, a second severity click goes back to Open; the small filter line names the filter and its count, takes its colour and offers Back to Open; the arrow folds the sub menu, it unfolds by itself on the Anomalies tab, its fold state and counts survive live refreshes; in the phone drawer a pick closes the drawer, and the narrow-only Filters button opens the drawer at the sub menu';
}

async function datesReadAsDayAndEnglishMonth() {
  //given
  const serve = {rev: 5, tab: 'anoms', state: stateAt(5, {headline: 'h', ordersNow: 40, extra: [anomaly('err|payments-api|ERROR', 'warn', {status: 'cleared', clearedAt: '2026-10-23T08:05:00Z', firstSeen: '2026-10-22T09:00:00Z'})]})};
  serve.state.history = [{t: '2026-10-23T08:05:00Z', key: 'err|payments-api|ERROR', ev: 'cleared', sev: 'warn', text: 'WARN payments-api', muted: false}];
  const {doc, errors, dom} = await boot(serve);

  //when
  const tag = card(doc, 'err|payments-api|ERROR').querySelector('.tag.k-cleared').textContent;
  const event = doc.querySelector('#hist tbody td.when').textContent;
  const shown = doc.getElementById('anoms').textContent + doc.getElementById('hist').textContent;

  //then
  assert.match(tag, /^Stopped firing \u00b7 unassigned 23 Oct \d{2}:\d{2}$/, 'a card cleared on another day reads day + English month: ' + tag);
  assert.match(event, /^23 Oct \d{2}:\d{2}$/, 'so does the Events tab: ' + event);
  assert.ok(/\d (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b/.test(shown), 'English short months on the cards or events');
  assert.ok(!/\d{1,2}\.\d{2}\b/.test(event), 'never "23.10"');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'dates read as day + English month ("23 Oct 11:05") on cards and events, the page being English';
}

async function switchingTabsRefreshesAtOnce() {
  //given
  const serve = {rev: 5, state: stateAt(5, {headline: 'one', ordersNow: 40})};
  const {doc, errors, calls, dom} = await boot(serve);
  const other = doc.getElementById('tab-svcs');
  const before = calls.filter((c) => c === '/api/rev').length;

  //when
  serve.rev = 6;
  serve.state = stateAt(6, {headline: 'two', ordersNow: 90});
  other.click();
  await wait(50);
  doc.getElementById('tab-anoms').click();
  await wait(200);

  //then
  assert.ok(calls.filter((c) => c === '/api/rev').length >= before + 2, 'each tab switch asks /api/rev at once instead of waiting for the 15 s poll');
  assert.ok(calls.includes('/api/state'), 'the moved rev fetches the new state');
  assert.ok(/90/.test(card(doc, 'err|orders-service|ERROR').textContent), 'the Anomalies tab shows the change made elsewhere (for example from the chat) right after the click');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'switching tabs refreshes at once, so a card changed from the chat shows its new state when Anomalies opens';
}

(async () => {
  for (const t of [liveUpdateKeepsTheView, typingAndConfirmDeferTheUpdate, hiddenTabPausesAndStateFailureReloads, pastedCardLinkJumpsInAnOpenTab, summaryLabelInBothBranches, cardsCollapseToOneRow, anomalyFiltersInTheSideMenu, datesReadAsDayAndEnglishMonth, switchingTabsRefreshesAtOnce]) {
    console.log('PASS ' + (await t()));
  }
})().catch((e) => { console.error('FAIL', e.message); process.exit(1); });
