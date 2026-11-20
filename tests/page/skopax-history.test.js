const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const {short, zone} = require('./skopax-zone');
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const LOADING = [['q-list', 'Loading actions\u2026'], ['pod-list', 'Loading investigations\u2026'], ['live-rows', 'Loading\u2026']];
const settled = (doc) => LOADING.every(([id, text]) => doc.getElementById(id).textContent !== text);
async function until(check, ms) {
  const end = Date.now() + (ms || 5000);
  while (Date.now() < end && !check()) await wait(5);
}
const KEY = 'log|reports-service|WARN|ShipCorp 456 invalid client ip';
const NEW = 'log|checkout-service|ERROR|first time';
const PERMALINK = 'https://example.slack.com/archives/C1/p1792224000000200';

function card(key, extra) {
  return Object.assign({key, svc: key.split('|')[1], label: key.split('|')[2], sev: 'warn', status: 'open', now: 12, d1: 2, d7: 3, firstSeen: iso(30), lastSeen: iso(1), ns: '-', src: 'logs'}, extra || {});
}

function stateAt(rev, cards) {
  return {rev, updatedAt: iso(0), sweptIso: iso(1), nextIso: iso(-59), serverPasses: {prom: {at: iso(3)}}, sources: {prom: 'ok', logs: 'ok'},
    summary: {level: 'warn', headline: 'h', points: [], at: iso(1)}, anomalies: cards, services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, deploys: []};
}

const SEEN = {count: 3, lastAt: '2026-10-24T09:05:00Z', outcome: 'handled', ticket: 'OPS-120', slack: {at: '2026-10-25T09:00:00Z', line: 'one customer, 40 lines'}};
const RUNS = [
  {id: 'r-slack', mode: 'live', trigger: 'slack', keys: [KEY], startedAt: iso(20), outcome: 'done', usd: 0.12, recap: 'ShipCorp 456 again, IP allow-list\nsecond line', ask: {text: 'Why?', by: 'adam.baker', source: 'slack', thread: PERMALINK}},
  {id: 'r-auto', mode: 'shadow', trigger: 'cards', keys: [KEY], startedAt: iso(25), outcome: 'failed', usd: 0.4, recap: ''},
  {id: 'r-other', mode: 'live', trigger: 'cards', keys: [NEW], startedAt: iso(26), outcome: 'done', recap: 'not this card'},
];

const LIVE = {mode: 'live', enabled: true, stopped: null, spentToday: 0.4, cap: 20};

async function boot(serve) {
  const errors = [], gets = [], posts = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  vc.on('error', (e) => errors.push('console.error ' + e));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(serve.state).replace(/<\//g, '<\\/'));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p, opt) => {
        const path = String(p);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (opt && opt.method === 'POST' && path === '/api/actions') {
          posts.push(JSON.parse(opt.body));
          if (serve.post) return {ok: false, status: serve.post.status, json: async () => ({detail: serve.post.detail})};
          serve.lastAction = Object.assign({id: 'act-' + posts.length, status: 'requested', requestedAt: iso(0), requestedBy: 'op@example.com'}, posts[posts.length - 1], serve.reply ? {status: 'done', result: serve.reply, smallTalk: true} : {});
          return ok(serve.lastAction);
        }
        if (opt && opt.method === 'POST' && path === '/api/chats') {
          posts.push(JSON.parse(opt.body));
          if (serve.post) return {ok: false, status: serve.post.status, json: async () => ({detail: serve.post.detail})};
          return ok({id: 'c' + posts.length, cards: [], messages: []});
        }
        gets.push(path);
        const rec = path.match(/^\/api\/investigations\/([^?]+)$/);
        if (rec) return (serve.records || {})[decodeURIComponent(rec[1])] ? ok(serve.records[decodeURIComponent(rec[1])]) : {ok: false, status: 404, json: async () => ({detail: 'no such run'})};
        if (path.startsWith('/api/investigations?key='))return serve.runsStatus ? {ok: false, status: serve.runsStatus, json: async () => ({})} : ok(serve.runs);
        if (path.startsWith('/api/investigations?limit=')) return ok(serve.allRuns || []);
        if (path.startsWith('/api/server')) return ok({investigate: serve.info === undefined ? LIVE : serve.info});
        if (path.startsWith('/api/me')) return ok(serve.guest ? {loginEnabled: true, user: null, operator: false, jiraUrl: 'https://jira.example.com', task: {projects: ['OPS']}} : {loginEnabled: true, user: 'op@example.com', operator: true, jiraUrl: 'https://jira.example.com', task: {projects: ['OPS']}});
        if (path.startsWith('/api/rev')) return ok({rev: serve.state.rev});
        if (path.startsWith('/api/state')) return ok(serve.state);
        if (path.startsWith('/api/actions')) return ok(serve.actions || []);
        if (path.startsWith('/api/activity')) return ok((serve.activity || []).slice());
        if (path.startsWith('/api/investigations') || path.startsWith('/api/actions') || path.startsWith('/api/deploy')) return ok([]);
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await until(() => settled(dom.window.document));
  return {dom, w: dom.window, doc: dom.window.document, errors, gets, posts};
}

const article = (doc, key) => [...doc.querySelectorAll('#anoms article.inc')].find((c) => c.dataset.key === key);
const expand = (c) => { if (c.querySelector('.inc-title .fold').getAttribute('aria-expanded') !== 'true') c.querySelector('.inc-title .fold').click(); };
const runFetches = (gets) => gets.filter((g) => g.startsWith('/api/investigations?key='));
const close = (dom) => setTimeout(() => dom.window.close(), 300);
const loaded = (doc, key) => { const b = article(doc, key) && article(doc, key).querySelector('.card-runs .runs-body'); return !!b && !b.textContent.startsWith('Loading'); };

async function seenBeforeLineInTheExpandedCard() {
  //given
  const serve = {state: stateAt(5, [card(KEY, {seenBefore: SEEN}), card(NEW), card('log|x-service|WARN|once', {seenBefore: {count: 1, lastAt: '2026-10-20T09:00:00Z', outcome: 'progress'}})]), runs: []};
  const {dom, doc, errors} = await boot(serve);
  const c = article(doc, KEY);
  //when
  const hidden = c.querySelector('.inc-more').hidden;
  expand(c);
  expand(article(doc, NEW));
  expand(article(doc, 'log|x-service|WARN|once'));
  const line = c.querySelector('.inc-more .seen-line');
  const parts = [...line.querySelectorAll('span')].map((s) => s.textContent);
  //then
  assert.ok(hidden, 'the line lives in the expanded panel, not on the row');
  assert.strictEqual(parts[0], 'Seen before: 3 times, last ' + short(SEEN.lastAt) + ', resolved \u00b7 ticket OPS-120 \u2197');
  assert.strictEqual(parts[1], 'Asked in Slack ' + short(SEEN.slack.at) + ': one customer, 40 lines', 'an English date on the English page, the Slack line kept as asked');
  assert.strictEqual(line.querySelector('a').getAttribute('href'), 'https://jira.example.com/browse/OPS-120', 'the ticket links to Jira');
  assert.ok(/card history/.test(line.title), 'the tooltip says where it comes from');
  assert.strictEqual(c.querySelector('.inc-more .cd-details > .line').nextElementSibling, line, 'right under the metric line');
  assert.strictEqual(article(doc, NEW).querySelector('.seen-line'), null, 'a first-timer has no line');
  assert.strictEqual(article(doc, 'log|x-service|WARN|once').querySelector('.seen-line').textContent, 'Seen before: once, last ' + short('2026-10-20T09:00:00Z') + ', In progress');
  assert.deepStrictEqual(errors, []);
  close(dom);
  console.log('PASS seenBeforeLineInTheExpandedCard');
}

async function runsOnThisCardLoadOnExpandAndSurviveALiveUpdate() {
  //given
  const serve = {state: stateAt(5, [card(KEY), card(NEW)]), runs: RUNS};
  const {dom, w, doc, errors, gets} = await boot(serve);
  //when
  const before = runFetches(gets).length;
  const items = () => [...article(doc, KEY).querySelectorAll('.card-runs li')];
  expand(article(doc, KEY));
  await until(() => loaded(doc, KEY));
  const first = items()[0];
  const shown = items().map((li) => li.textContent);
  const painted = article(doc, KEY);
  serve.state = stateAt(6, [card(KEY, {now: 14}), card(NEW)]);
  await w.__skopaxPoll();
  await until(() => article(doc, KEY) !== painted && loaded(doc, KEY));
  const after = items().map((li) => li.textContent);
  //then
  assert.strictEqual(before, 0, 'nothing is fetched for a collapsed card');
  assert.deepStrictEqual(runFetches(gets), ['/api/investigations?key=' + encodeURIComponent(KEY) + '&limit=10'], 'one fetch, also across the live update');
  assert.strictEqual(article(doc, KEY).querySelector('.card-runs h5').textContent, 'Runs on this card2');
  assert.strictEqual(shown.length, 2, 'runs of other cards are left out');
  assert.ok(shown[0].includes(w.dayhm(RUNS[0].startedAt)) && shown[0].includes('Slack ask') && shown[0].includes('done') && shown[0].includes('ShipCorp 456 again, IP allow-list') && !shown[0].includes('second line'), shown[0]);
  assert.strictEqual(first.querySelector('a.thread').getAttribute('href'), PERMALINK);
  assert.strictEqual(first.querySelector('a.record').getAttribute('href'), '/api/investigations/r-slack');
  assert.ok(shown[1].includes('Investigate (auto) \u00b7 shadow') && shown[1].includes('failed') && !shown[1].includes('Slack thread'), shown[1]);
  assert.deepStrictEqual(after, shown, 'the list survives the repaint');
  assert.deepStrictEqual(errors, []);
  close(dom);
  console.log('PASS runsOnThisCardLoadOnExpandAndSurviveALiveUpdate');
}

async function runsEmptyOrUnavailableStayQuiet() {
  //given
  const empty = await boot({state: stateAt(5, [card(KEY)]), runs: []});
  const down = await boot({state: stateAt(5, [card(KEY)]), runs: [], runsStatus: 500});
  //when
  expand(article(empty.doc, KEY));
  expand(article(down.doc, KEY));
  await until(() => loaded(empty.doc, KEY) && loaded(down.doc, KEY));
  //then
  assert.strictEqual(article(empty.doc, KEY).querySelector('.card-runs .runs-body').textContent, 'No investigator run on this card yet.');
  assert.strictEqual(article(down.doc, KEY).querySelector('.card-runs .runs-body').textContent, 'Runs unavailable.');
  assert.deepStrictEqual(empty.errors.concat(down.errors), []);
  close(empty.dom);
  close(down.dom);
  console.log('PASS runsEmptyOrUnavailableStayQuiet');
}

async function recurringTagOnTheRowAndInTheSideMenu() {
  //given
  const TICKETED = 'log|b-service|WARN|ticketed';
  const serve = {state: stateAt(5, [card(KEY, {recurring: true, recurringCount: 3, recurringDays: 7}), card(TICKETED, {recurring: true, recurringCount: 4, recurringDays: 7, triage: 'progress', ticket: 'OPS-1'}), card(NEW)]), runs: []};
  const quiet = await boot({state: stateAt(5, [card(NEW)]), runs: []});
  const {dom, doc, errors} = await boot(serve);
  const item = (d) => d.querySelector('#tab-sub-anoms button[data-f="recurring"]');
  const keys = () => [...doc.querySelectorAll('#anoms article.inc')].map((c) => c.dataset.key);
  //when
  doc.querySelector('#tab-sub-anoms button[data-f="all"]').click();
  const tag = article(doc, KEY).querySelector('.inc-title .tag.recurring');
  const others = [article(doc, TICKETED).querySelector('.tag.recurring'), article(doc, NEW).querySelector('.tag.recurring')];
  const label = [item(doc).querySelector('.lbl').textContent, item(doc).querySelector('.count').textContent, item(doc).classList.contains('zero')];
  item(doc).click();
  const filtered = keys();
  const note = doc.getElementById('filter-note').textContent;
  item(doc).click();
  //then
  assert.strictEqual(tag.textContent, 'Recurring \u00d73', 'the row says it keeps coming back');
  assert.ok(/Fired 3 times in the last 7 days/.test(tag.title) && /Create task/.test(tag.title), tag.title);
  assert.deepStrictEqual(others, [null, null], 'a card in progress with a Jira ticket is not flagged again, a plain card never');
  assert.deepStrictEqual(label, ['Recurring', '1', false]);
  assert.deepStrictEqual(filtered, [KEY], 'the filter shows only the recurring card');
  assert.ok(note.includes('Recurring') && note.includes('1'), note);
  assert.strictEqual(doc.querySelector('#tab-sub-anoms button[data-f="open"]').getAttribute('aria-pressed'), 'true', 'a second click goes back to Open');
  assert.deepStrictEqual(keys(), [KEY, NEW]);
  assert.strictEqual(item(quiet.doc), null, 'no Recurring item while no card recurs, like the Triage items');
  assert.deepStrictEqual(errors.concat(quiet.errors), []);
  close(dom);
  close(quiet.dom);
  console.log('PASS recurringTagOnTheRowAndInTheSideMenu');
}

async function runRecordOpensInTheJsonViewerAndThePageStays() {
  //given
  const RECORD = {id: 'r-slack', trigger: 'slack', usage: {input_tokens: 1200}, calls: [{tool: 'logs', args: {q: '<b>a & "c"</b>'}}]};
  const serve = {state: stateAt(5, [card(KEY), card(NEW)]), runs: RUNS, records: {'r-slack': RECORD}};
  const {dom, w, doc, errors, gets} = await boot(serve);
  expand(article(doc, KEY));
  await until(() => loaded(doc, KEY));
  const link = (id) => [...article(doc, KEY).querySelectorAll('.card-runs a.record')].find((a) => a.getAttribute('href') === '/api/investigations/' + id);
  const wrap = doc.getElementById('json-wrap');
  const click = (node, extra) => node.dispatchEvent(new w.MouseEvent('click', Object.assign({bubbles: true, cancelable: true, button: 0}, extra || {})));
  const esc = () => doc.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  const recordGets = () => gets.filter((g) => /^\/api\/investigations\/[^?]/.test(g));
  const jsonLoaded = () => doc.getElementById('json-body').textContent !== 'Loading\u2026' || !doc.getElementById('json-err').hidden;
  const href = w.location.href;
  //when
  const modifier = click(link('r-slack'), {metaKey: true});
  const shutAfterModifier = wrap.hidden;
  const plain = click(link('r-slack'));
  await until(jsonLoaded);
  const opened = {
    title: doc.getElementById('json-title').textContent, src: doc.getElementById('json-src').textContent, body: doc.getElementById('json-body').textContent,
    bodyShown: !doc.getElementById('json-body').hidden, errHidden: doc.getElementById('json-err').hidden, focus: doc.activeElement && doc.activeElement.id,
    raw: ['href', 'target', 'rel'].map((a) => doc.getElementById('json-raw').getAttribute(a)), rawText: doc.getElementById('json-raw').textContent,
  };
  click(doc.getElementById('json-body'));
  const openAfterInsideClick = !wrap.hidden;
  const painted = article(doc, KEY);
  serve.state = stateAt(6, [card(KEY, {now: 14}), card(NEW)]);
  await w.__skopaxPoll();
  await until(() => article(doc, KEY) !== painted && link('r-slack'));
  const survives = !wrap.hidden && doc.getElementById('json-body').textContent === opened.body;
  esc();
  const afterEsc = [wrap.hidden, doc.activeElement === link('r-slack')];
  click(link('r-slack'));
  await until(jsonLoaded);
  doc.getElementById('json-close').click();
  const afterClose = wrap.hidden;
  click(link('r-slack'));
  await until(jsonLoaded);
  click(wrap);
  const afterBackdrop = wrap.hidden;
  click(link('r-auto'));
  await until(jsonLoaded);
  const failed = {err: doc.getElementById('json-err').textContent, errShown: !doc.getElementById('json-err').hidden, bodyHidden: doc.getElementById('json-body').hidden,
    raw: doc.getElementById('json-raw').getAttribute('href'), src: doc.getElementById('json-src').textContent};
  esc();
  //then
  assert.deepStrictEqual([modifier, shutAfterModifier], [true, true], 'a modifier click is left to the browser: no viewer, default not prevented');
  assert.strictEqual(plain, false, 'a plain click never navigates: the default is prevented');
  assert.strictEqual(w.location.href, href, 'the page stays where it is');
  assert.strictEqual(link('r-slack').textContent, 'Run record', 'no \u2197 now that it opens in the page');
  assert.deepStrictEqual(['target', 'rel'].map((a) => link('r-slack').getAttribute(a)), ['_blank', 'noopener'], 'the link itself still works in a new tab');
  assert.strictEqual(opened.title, 'Run record');
  assert.strictEqual(opened.src, '/api/investigations/r-slack');
  assert.strictEqual(opened.body, JSON.stringify(RECORD, null, 2), 'pretty-printed with two spaces, markup shown as text');
  assert.ok(opened.bodyShown && opened.errHidden);
  assert.strictEqual(opened.focus, 'json-close', 'focus moves into the dialog');
  assert.deepStrictEqual(opened.raw, ['/api/investigations/r-slack', '_blank', 'noopener']);
  assert.strictEqual(opened.rawText, 'Open raw \u2197');
  assert.ok(openAfterInsideClick, 'a click inside the panel keeps it open');
  assert.ok(survives, 'a live update leaves the open viewer alone');
  assert.deepStrictEqual(afterEsc, [true, true], 'Esc closes it and focus goes back to the run record link, also after the card re-rendered');
  assert.ok(afterClose, 'Close closes it');
  assert.ok(afterBackdrop, 'a click on the backdrop closes it');
  assert.deepStrictEqual(failed, {err: 'Could not load this record (HTTP 404).', errShown: true, bodyHidden: true, raw: '/api/investigations/r-auto', src: '/api/investigations/r-auto'}, 'a failed load says so and keeps Open raw');
  assert.deepStrictEqual(recordGets(), ['/api/investigations/r-slack', '/api/investigations/r-slack', '/api/investigations/r-slack', '/api/investigations/r-auto'], 'one fetch per opening, none for the modifier click');
  assert.ok(wrap.hidden);
  assert.deepStrictEqual(errors, []);
  close(dom);
  console.log('PASS runRecordOpensInTheJsonViewerAndThePageStays');
}

const SHOTS = [
  {id: 's1', url: 'https://shop.example.com/', at: '2026-10-26T11:15:00Z', finalUrl: 'https://shop.example.com/', viewport: 'desktop', full: false, cut: false, width: 1280, height: 800, status: 200, title: 'Shop: <b>best</b> offers', kb: 76.5,
    blocked: {otherHosts: 3, api: 2, tracking: 1}, link: 'https://skopax.example.com/api/screenshots/s1'},
  {id: 's2', url: 'https://shop.example.com/products', finalUrl: 'https://shop.example.com/products/garden-chairs', viewport: 'phone', full: true, cut: true, width: 390, height: 2576, status: 404, title: '', kb: 120,
    blocked: {otherHosts: 0, api: 0, tracking: 0}, link: 'https://skopax.example.com/api/screenshots/s2'},
];

async function screenshotsShowAsThumbnailsAndZoomInADialog() {
  //given
  const runs = [Object.assign({}, RUNS[0], {screenshots: SHOTS}), RUNS[1], Object.assign({}, RUNS[1], {id: 'r-old', startedAt: iso(60 * 24 * 31), screenshots: [SHOTS[1]]})];
  const serve = {state: stateAt(5, [card(KEY), card(NEW)]), runs};
  const {dom, w, doc, errors} = await boot(serve);
  expand(article(doc, KEY));
  await until(() => loaded(doc, KEY));
  const thumbs = () => [...article(doc, KEY).querySelectorAll('.card-runs .shots a.shot')];
  const wrap = doc.getElementById('shot-wrap');
  const click = (node, extra) => node.dispatchEvent(new w.MouseEvent('click', Object.assign({bubbles: true, cancelable: true, button: 0}, extra || {})));
  const esc = () => doc.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  const rows = runRows(doc);
  const strip = thumbs().map((a) => ({href: a.getAttribute('href'), target: a.getAttribute('target'), phone: a.classList.contains('phone'), src: a.querySelector('img').getAttribute('src'),
    alt: a.querySelector('img').getAttribute('alt'), lazy: a.querySelector('img').getAttribute('loading'), tip: a.getAttribute('title')}));
  //when
  const modifier = click(thumbs()[0], {ctrlKey: true});
  const shutAfterModifier = wrap.hidden;
  const plain = click(thumbs()[0]);
  const opened = {img: doc.getElementById('shot-img').getAttribute('src'), alt: doc.getElementById('shot-img').getAttribute('alt'), title: doc.getElementById('shot-title').textContent,
    meta: doc.getElementById('shot-meta').textContent, raw: doc.getElementById('shot-raw').getAttribute('href'), focus: doc.activeElement && doc.activeElement.id};
  click(doc.getElementById('shot-img'));
  const zoomed = doc.getElementById('shot-body').classList.contains('actual');
  click(doc.getElementById('shot-img'));
  const unzoomed = !doc.getElementById('shot-body').classList.contains('actual');
  const painted = article(doc, KEY);
  serve.state = stateAt(6, [card(KEY, {now: 14}), card(NEW)]);
  await w.__skopaxPoll();
  await until(() => article(doc, KEY) !== painted && thumbs().length > 0);
  const survives = !wrap.hidden;
  esc();
  const afterEsc = [wrap.hidden, doc.activeElement === thumbs()[0]];
  click(thumbs()[1]);
  const phone = {meta: doc.getElementById('shot-meta').textContent, title: doc.getElementById('shot-title').textContent, phone: doc.getElementById('shot-body').classList.contains('phone')};
  doc.getElementById('shot-close').click();
  const afterClose = wrap.hidden && !doc.getElementById('shot-img');
  click(thumbs()[1]);
  click(wrap);
  const afterBackdrop = wrap.hidden;
  //then
  assert.strictEqual(rows.length, 3);
  assert.ok(!rows[1].querySelector('.shots'), 'a run without screenshots shows no strip');
  assert.deepStrictEqual([rows[2].querySelector('.shots.gone').textContent, rows[2].querySelectorAll('img').length], ['1 screenshot, expired after 30 days', 0], 'an old run says its images expired instead of showing broken ones');
  assert.deepStrictEqual(strip, [
    {href: '/api/screenshots/s1', target: '_blank', phone: false, src: '/api/screenshots/s1', alt: 'Screenshot of https://shop.example.com/, desktop', lazy: 'lazy',
      tip: 'Shop: <b>best</b> offers \u00b7 ' + short('2026-10-26T11:15:00Z') + ' ' + zone() + ' \u00b7 desktop 1280 \u00d7 800 \u00b7 status 200 \u00b7 6 requests blocked \u00b7 click to zoom'},
    {href: '/api/screenshots/s2', target: '_blank', phone: true, src: '/api/screenshots/s2', alt: 'Screenshot of https://shop.example.com/products/garden-chairs, phone', lazy: 'lazy',
      tip: 'https://shop.example.com/products/garden-chairs \u00b7 phone 390 \u00d7 2576, cut \u00b7 status 404 \u00b7 click to zoom'},
  ]);
  assert.deepStrictEqual([modifier, shutAfterModifier], [true, true], 'a modifier click is left to the browser');
  assert.strictEqual(plain, false, 'a plain click opens the dialog instead of navigating');
  assert.deepStrictEqual(opened, {img: '/api/screenshots/s1', alt: 'Screenshot of https://shop.example.com/, desktop', title: 'Shop: <b>best</b> offers',
    meta: 'https://shop.example.com/ \u00b7 desktop 1280 \u00d7 800 \u00b7 status 200 \u00b7 blocked: 3 other hosts, 2 API calls, 1 tracking', raw: '/api/screenshots/s1', focus: 'shot-close'});
  assert.deepStrictEqual([zoomed, unzoomed], [true, true], 'a click on the image toggles 1:1');
  assert.ok(survives, 'a live update leaves the open dialog alone');
  assert.deepStrictEqual(afterEsc, [true, true], 'Esc closes it and focus goes back to the thumbnail');
  assert.deepStrictEqual(phone, {meta: 'https://shop.example.com/products/garden-chairs \u00b7 phone 390 \u00d7 2576, full page cut at 2576 px \u00b7 status 404', title: 'https://shop.example.com/products/garden-chairs', phone: true});
  assert.ok(afterClose && afterBackdrop, 'Close and the backdrop close it, and the picture leaves with the dialog');
  assert.deepStrictEqual(errors, []);
  close(dom);
  console.log('PASS screenshotsShowAsThumbnailsAndZoomInADialog');
}

const SHOT_LINK = 'https://skopax.example.com/api/screenshots/0a1b2c3d4e5f60718293a4b5c6d7e8f9';
const HOME = {id: '0a1b2c3d4e5f60718293a4b5c6d7e8f9', url: 'https://shop.example.com/', finalUrl: 'https://shop.example.com/', viewport: 'desktop', full: false, cut: false, width: 1280, height: 800, status: 200,
  title: 'shop.example.com: Chairs, Tables and Lamps', kb: 80.2, blocked: {otherHosts: 5, api: 1, tracking: 2}, link: SHOT_LINK};
const PAGE_ASK = {id: 'r-page', mode: 'live', trigger: 'ask', actionId: 'act-page', keys: [], startedAt: iso(15), outcome: 'done', usd: 0.3, toolCalls: 2,
  recap: 'The home page loads, the hero banner is there.\nScreenshot: ' + SHOT_LINK + '\nNot a link: http://shop.example.com/ or javascript:alert(1) or <b>bold</b>', ask: {text: 'How does shop.example.com look?', by: 'op@example.com'}, screenshots: [HOME]};
const CARD_RUN = {id: 'r-card', mode: 'live', trigger: 'cards', keys: [KEY], startedAt: iso(40), outcome: 'done', usd: 0.2, toolCalls: 4, recap: 'Ticket https://jira.example.com/browse/OPS-120 owns it.\nsecond line'};
const OLD_RUN = {id: 'r-gone', mode: 'live', trigger: 'cards', keys: [NEW], startedAt: iso(60 * 24 * 31), outcome: 'done', usd: 0.1, toolCalls: 1, recap: 'old', screenshots: [SHOTS[1]]};
const podRows = (doc) => [...doc.querySelectorAll('#pod-list li')];
const pageRow = (doc) => podRows(doc).find((li) => li.textContent.includes('How does shop.example.com look?'));

async function historyListShowsTheScreenshotsAndLinksOfEveryRun() {
  //given
  const running = Object.assign({}, PAGE_ASK, {outcome: 'running', recap: '', screenshots: undefined});
  const serve = {state: stateAt(5, [card(KEY), card(NEW)]), runs: [], allRuns: [running, CARD_RUN, OLD_RUN]};
  const {dom, w, doc, errors} = await boot(serve);
  const click = (node, extra) => node.dispatchEvent(new w.MouseEvent('click', Object.assign({bubbles: true, cancelable: true, button: 0}, extra || {})));
  const wrap = doc.getElementById('shot-wrap');
  //when
  const whileRunning = pageRow(doc).querySelectorAll('.shots').length;
  serve.allRuns = [PAGE_ASK, CARD_RUN, OLD_RUN];
  serve.state = stateAt(6, [card(KEY), card(NEW)]);
  await w.__skopaxPoll();
  await until(() => pageRow(doc) && pageRow(doc).querySelector('.shots a.shot'));
  const row = pageRow(doc);
  const thumbs = [...row.querySelectorAll('.shots a.shot')];
  const links = [...row.querySelectorAll('.answer a')].map((a) => [a.getAttribute('href'), a.getAttribute('target'), a.getAttribute('rel'), a.textContent]);
  const cardRow = podRows(doc).find((li) => li.textContent.includes('Ticket'));
  const oldRow = podRows(doc).find((li) => li.textContent.includes('checkout-service / ERROR'));
  const plain = click(thumbs[0]);
  const opened = {img: doc.getElementById('shot-img').getAttribute('src'), title: doc.getElementById('shot-title').textContent, meta: doc.getElementById('shot-meta').textContent};
  doc.getElementById('shot-close').click();
  //then
  assert.strictEqual(whileRunning, 0, 'a running run has no strip yet');
  assert.deepStrictEqual(thumbs.map((a) => [a.getAttribute('href'), a.querySelector('img').getAttribute('src'), a.querySelector('img').getAttribute('alt'), a.getAttribute('data-tip')]), [
    ['/api/screenshots/' + HOME.id, '/api/screenshots/' + HOME.id, 'Screenshot of https://shop.example.com/, desktop', 'shop.example.com: Chairs, Tables and Lamps \u00b7 desktop 1280 \u00d7 800 \u00b7 status 200 \u00b7 8 requests blocked \u00b7 click to zoom']],
  'a page ask with no card shows its screenshot after the live update');
  assert.deepStrictEqual(links, [[SHOT_LINK, '_blank', 'noopener', SHOT_LINK]], 'only the https link in the answer is clickable');
  assert.ok(row.querySelector('.answer').textContent.includes('http://shop.example.com/ or javascript:alert(1) or <b>bold</b>'), 'the rest stays escaped text');
  assert.strictEqual(row.querySelector('.answer b'), null);
  assert.deepStrictEqual([...cardRow.querySelectorAll('.hbody .recap a')].map((a) => a.getAttribute('href')), ['https://jira.example.com/browse/OPS-120'], 'the recap of a card run is linkified too');
  assert.ok(cardRow.querySelector('.hbody').textContent.includes('second line') && !cardRow.querySelector('summary').textContent.includes('second line'), 'the whole recap waits in the expanded row');
  assert.deepStrictEqual([oldRow.querySelector('.shots.gone').textContent, oldRow.querySelectorAll('img').length], ['1 screenshot, expired after 30 days', 0]);
  assert.strictEqual(plain, false, 'a plain click opens the zoom dialog');
  assert.deepStrictEqual(opened, {img: '/api/screenshots/' + HOME.id, title: 'shop.example.com: Chairs, Tables and Lamps',
    meta: 'https://shop.example.com/ \u00b7 desktop 1280 \u00d7 800 \u00b7 status 200 \u00b7 blocked: 5 other hosts, 1 API call, 2 tracking'}, 'the dialog knows the History run\'s screenshot');
  assert.ok(wrap.hidden);
  assert.deepStrictEqual(errors, []);
  close(dom);
  //given
  const guest = await boot({state: stateAt(5, [card(KEY)]), runs: [], allRuns: [PAGE_ASK], guest: true});
  //when
  const gthumb = pageRow(guest.doc).querySelector('.shots a.shot');
  gthumb.dispatchEvent(new guest.w.MouseEvent('click', {bubbles: true, cancelable: true, button: 0}));
  //then
  assert.ok(gthumb && !guest.doc.getElementById('shot-wrap').hidden, 'a guest sees the thumbnail and can zoom it');
  assert.strictEqual(pageRow(guest.doc).querySelector('.answer a').getAttribute('href'), SHOT_LINK);
  close(guest.dom);
  console.log('PASS historyListShowsTheScreenshotsAndLinksOfEveryRun');
}

async function activityRailShowsTheRunsScreenshotsAndAnswerLinks() {
  //given
  const ANSWER = {id: 'a3', t: iso(13), kind: 'ask', who: 'investigator', text: 'answer for op@example.com \u00b7 How does shop.example.com look? \u2192 see ' + SHOT_LINK, runId: 'r-page', outcome: 'done'};
  const ENDED = {id: 'a2', t: iso(14), kind: 'run', who: 'investigator', text: 'run done \u00b7 3 turns \u00b7 2 tool calls \u00b7 $0.30', runId: 'r-page', outcome: 'done', screenshots: [HOME]};
  const WROTE = {id: 'a1', t: iso(20), kind: 'write', who: 'investigator', text: 'note \u00b7 ' + KEY + ' \u00b7 see https://jira.example.com/browse/OPS-1', runId: 'r-x', tool: 'note', key: KEY};
  const serve = {state: stateAt(5, [card(KEY)]), runs: [], activity: [WROTE]};
  const {dom, w, doc, errors} = await boot(serve);
  const rows = () => [...doc.querySelectorAll('#live-rows li')];
  const click = (node) => node.dispatchEvent(new w.MouseEvent('click', {bubbles: true, cancelable: true, button: 0}));
  //when
  const before = doc.querySelectorAll('#live-rows .shots').length;
  serve.activity = [ANSWER, ENDED, WROTE];
  await w.__activityPoll();
  const answerRow = rows().find((li) => li.textContent.includes('How does shop.example.com look?'));
  const endRow = rows().find((li) => li.textContent.includes('run done'));
  const writeRow = rows().find((li) => li.textContent.includes('note \u00b7'));
  const thumb = endRow.querySelector('.shots a.shot');
  click(thumb);
  const opened = {title: doc.getElementById('shot-title').textContent, meta: doc.getElementById('shot-meta').textContent};
  doc.getElementById('shot-close').click();
  //then
  assert.strictEqual(before, 0);
  assert.deepStrictEqual([...answerRow.querySelectorAll('a')].map((a) => [a.getAttribute('href'), a.getAttribute('target'), a.getAttribute('rel')]), [[SHOT_LINK, '_blank', 'noopener']], 'the answer line links');
  assert.ok(endRow.classList.contains('child'), 'the run line with the screenshots sits under the answer');
  assert.deepStrictEqual([thumb.getAttribute('href'), thumb.querySelector('img').getAttribute('src')], ['/api/screenshots/' + HOME.id, '/api/screenshots/' + HOME.id], 'the new line brings its thumbnail on the poll');
  assert.deepStrictEqual(opened, {title: 'shop.example.com: Chairs, Tables and Lamps', meta: 'https://shop.example.com/ \u00b7 desktop 1280 \u00d7 800 \u00b7 status 200 \u00b7 blocked: 5 other hosts, 1 API call, 2 tracking'});
  assert.strictEqual(writeRow.querySelector('a'), null, 'only answers are linkified, other lines may be cut mid-link');
  assert.ok(doc.getElementById('shot-wrap').hidden);
  assert.deepStrictEqual(errors, []);
  close(dom);
  //given
  const guest = await boot({state: stateAt(5, [card(KEY)]), runs: [], activity: [ANSWER, ENDED], guest: true});
  //then
  assert.ok(guest.doc.querySelector('#live-rows .shots a.shot img'), 'a guest sees the thumbnail in Activity too');
  close(guest.dom);
  console.log('PASS activityRailShowsTheRunsScreenshotsAndAnswerLinks');
}

async function slackDraftsShowTheirScreenshots() {
  //given
  const draft = {text: 'example.com home page looks empty.\nScreenshot below.', by: 'Claude (pod)', at: iso(2), screenshots: [HOME]};
  const serve = {state: stateAt(5, [card(KEY, {sev: 'crit', slackDraft: Object.assign({notAutoSent: 'planted text in the esql answer (ignore previous)'}, draft)}), card(NEW, {slackDraft: {text: 'plain', by: 'Claude (pod)', at: iso(3), notAutoSent: 'planted text in the esql answer (ignore previous)'}})]), runs: []};
  const {dom, w, doc, errors} = await boot(serve);
  const click = (node) => node.dispatchEvent(new w.MouseEvent('click', {bubbles: true, cancelable: true, button: 0}));
  //when
  const rows = [...doc.querySelectorAll('#slackq tbody tr')];
  const withShots = rows.find((tr) => tr.textContent.includes('home page'));
  const without = rows.find((tr) => tr.textContent.includes('plain'));
  expand(article(doc, KEY));
  const inCard = article(doc, KEY).querySelector('.sdraft .shots a.shot');
  click(withShots.querySelector('.shots a.shot'));
  const title = doc.getElementById('shot-title').textContent;
  doc.getElementById('shot-close').click();
  //then
  assert.deepStrictEqual([...withShots.querySelectorAll('.shots a.shot')].map((a) => a.getAttribute('href')), ['/api/screenshots/' + HOME.id], 'the Slack tab row shows the screenshot');
  assert.ok(withShots.querySelector('.shots').classList.contains('mini'));
  assert.strictEqual(without.querySelector('.shots'), null);
  assert.ok(inCard && inCard.getAttribute('href') === '/api/screenshots/' + HOME.id, 'the card\'s draft section shows it too');
  assert.strictEqual(title, 'shop.example.com: Chairs, Tables and Lamps');
  assert.deepStrictEqual(errors, []);
  close(dom);
  console.log('PASS slackDraftsShowTheirScreenshots');
}

const ACTS = [
  {id: 'x1', type: 'dive', key: KEY, target: KEY, label: 'Investigate reports-service WARN logs', status: 'done', requestedAt: iso(30), doneAt: iso(25), requestedBy: 'op@example.com',
    result: 'Same PayCo outage: 27 ERRORs between 12:10 and 12:20 UTC, see https://jira.example.com/browse/OPS-120'},
  {id: 'x2', type: 'mute', key: NEW, target: NEW, label: 'Mute checkout-service first time for 24 h', status: 'requested', requestedAt: iso(1), requestedBy: 'op@example.com', hours: 24},
  {id: 'x3', type: 'dive', key: 'log|x-service|ERROR|y', target: 'log|x-service|ERROR|y', label: 'Investigate x-service ERROR logs', status: 'failed', requestedAt: iso(50), failedAt: iso(49), requestedBy: 'auto', result: 'the investigator is stopped'},
  {id: 'x4', type: 'postdeploy', key: 'postdeploy', target: 'postdeploy', label: 'Post-deploy check', status: 'done', requestedAt: iso(70), doneAt: iso(68), requestedBy: 'op@example.com',
    result: '2 deploys, 1 flagged', report: 'checkout-service err 40 (5/4)\npayments-api clean'},
];

async function cardHeadShowsACompactActionBadgeAndTheFullLineInside() {
  //given
  const serve = {state: stateAt(5, [card(KEY), card(NEW)]), runs: [], actions: ACTS};
  const {dom, w, doc, errors} = await boot(serve);
  //when
  const done = article(doc, KEY).querySelector('.acts .stat');
  const queued = article(doc, NEW).querySelector('.acts .stat');
  const pill = done.querySelector('.st');
  const lastHidden = article(doc, KEY).querySelector('.inc-more .last-act').closest('.inc-more').hidden;
  expand(article(doc, KEY));
  expand(article(doc, NEW));
  const last = article(doc, KEY).querySelector('.inc-more .last-act');
  const lastNew = article(doc, NEW).querySelector('.inc-more .last-act');
  //then
  assert.strictEqual(done.textContent, 'Investigated ' + w.hm(ACTS[0].doneAt), 'only the status pill sits next to the buttons, and it names the action');
  assert.ok(pill.classList.contains('s-done'));
  assert.strictEqual(pill.getAttribute('data-tip'), 'Investigate reports-service WARN logs \u00b7 Done ' + w.dayhm(ACTS[0].doneAt) + '\n' + ACTS[0].result, 'label and result live in the tooltip');
  assert.deepStrictEqual([queued.querySelector('.st').textContent, queued.querySelector('button').textContent], ['Queued', 'Cancel'], 'a queued action keeps Cancel for operators');
  assert.ok(lastHidden, 'the full line lives in the expanded card');
  assert.strictEqual(last.textContent, 'Last action: Investigated ' + w.dayhm(ACTS[0].doneAt) + ' by op \u00b7 ' + ACTS[0].result);
  assert.strictEqual(last.querySelector('.st').getAttribute('data-tip'), 'Investigate reports-service WARN logs', 'the action\'s own label lives in the tooltip');
  assert.strictEqual(last.querySelector('a').getAttribute('href'), 'https://jira.example.com/browse/OPS-120');
  assert.strictEqual(lastNew.textContent, 'Last action: Queued ' + w.dayhm(ACTS[1].requestedAt) + ' by op');
  assert.strictEqual(lastNew.querySelector('.st').getAttribute('title'), 'Mute checkout-service first time for 24 h');
  assert.deepStrictEqual(errors, []);
  close(dom);
  //given
  const guest = await boot({state: stateAt(5, [card(KEY), card(NEW)]), runs: [], actions: ACTS, guest: true});
  //then
  assert.deepStrictEqual([article(guest.doc, NEW).querySelector('.acts .stat').textContent, article(guest.doc, NEW).querySelector('.acts .stat button')], ['Queued', null], 'no Cancel for a guest');
  close(guest.dom);
  console.log('PASS cardHeadShowsACompactActionBadgeAndTheFullLineInside');
}

async function cardPillNamesTheActionNotJustDone() {
  //given
  const k = (n) => 'log|pill-' + n + '-service|ERROR|x';
  const doneAt = iso(8), askedAt = iso(9);
  const act = (n, type, status, extra) => Object.assign({id: 'p-' + n, type, key: k(n), target: k(n), label: type + ' pill-' + n + '-service', status, requestedAt: askedAt, requestedBy: 'op@example.com'}, status === 'done' ? {doneAt} : status === 'processing' ? {processingAt: doneAt} : status === 'failed' ? {failedAt: doneAt, result: 'the investigator is stopped'} : status === 'cancelled' ? {cancelledAt: doneAt} : {}, extra || {});
  const acts = [act(1, 'dive', 'done', {result: 'one client retried 40 times'}), act(2, 'dive', 'processing'), act(3, 'dive', 'requested'), act(4, 'dive', 'failed'), act(5, 'dive', 'cancelled'),
    act(6, 'mute', 'done'), act(7, 'unmute', 'done'), act(8, 'known', 'done'), act(9, 'take', 'done'), act(10, 'group', 'done'), act(11, 'resolve', 'done'), act(12, 'ticket', 'done'), act(13, 'mute', 'failed')];
  const serve = {state: stateAt(5, acts.map((x) => card(x.key))), runs: [], actions: acts};
  const {dom, w, doc, errors} = await boot(serve);
  const at = w.hm(doneAt);
  //when
  const pills = acts.map((x) => article(doc, x.key).querySelector('.acts .stat .st'));
  const queued = article(doc, k(3)).querySelector('.acts .stat');
  //then
  assert.deepStrictEqual(pills.map((p) => p.textContent), ['Investigated ' + at, 'Investigating\u2026', 'Queued', 'Investigate failed', 'Investigate cancelled',
    'Muted until ' + w.dayhm(new Date(Date.parse(doneAt) + 24 * 3600e3).toISOString()), 'Unmuted ' + at, 'Marked known ' + at, 'Started ' + at, 'Grouped ' + at, 'Resolved ' + at, 'Task created ' + at, 'Mute failed'], 'the pill names what was done, not just Done');
  assert.deepStrictEqual(pills.slice(0, 5).map((p) => p.className), ['st s-done', 'st s-processing', 'st s-queued', 'st s-failed', 'st s-other'], 'the colors stay');
  assert.strictEqual(pills[0].getAttribute('data-tip'), 'dive pill-1-service \u00b7 Done ' + w.dayhm(doneAt) + '\none client retried 40 times', 'the tooltip keeps the full label and result');
  assert.strictEqual(pills[3].getAttribute('data-tip'), 'dive pill-4-service \u00b7 Failed ' + w.dayhm(doneAt) + '\nthe investigator is stopped');
  assert.deepStrictEqual([queued.querySelector('.st').textContent, queued.querySelector('button').textContent], ['Queued', 'Cancel'], 'a queued one keeps Cancel');
  assert.deepStrictEqual(errors, []);
  close(dom);
  console.log('PASS cardPillNamesTheActionNotJustDone');
}

async function anActionFromBeforeTheCardFiredAgainIsMarkedEarlier() {
  //given
  const back = card(KEY, {firstSeen: iso(30)});
  const old = {id: 'old-1', type: 'resolve', key: KEY, target: KEY, label: 'Resolve reports-service WARN logs', status: 'done', requestedAt: iso(3 * 24 * 60 + 1), doneAt: iso(3 * 24 * 60), requestedBy: 'dora.lane@example.com', result: 'No action needed'};
  const {dom, w, doc, errors} = await boot({state: stateAt(5, [back]), runs: [], actions: [old]});
  //when
  const stat = article(doc, KEY).querySelector('.acts .stat');
  expand(article(doc, KEY));
  const last = article(doc, KEY).querySelector('.inc-more .last-act');
  //then
  assert.strictEqual(stat.textContent, '', 'a resolve from the last round is not repeated in the head');
  assert.strictEqual(last.querySelector('.la-h').textContent, 'Reopened ' + w.dayhm(back.firstSeen) + ' \u00b7 fired again');
  assert.ok(last.querySelector('.la-before').textContent.startsWith('Before that: Resolved ' + w.dayhm(old.doneAt) + ' by dora.lane (No action needed)'), last.textContent);
  assert.strictEqual(last.querySelector('.st').className, 'st s-other', 'and it is grey, not green');
  assert.deepStrictEqual(errors, []);
  close(dom);
  console.log('PASS anActionFromBeforeTheCardFiredAgainIsMarkedEarlier');
}

async function lastActionLineNamesTheActionLikeThePill() {
  //given
  const k = (n) => 'log|line-' + n + '-service|ERROR|x';
  const doneAt = iso(8), askedAt = iso(9);
  const act = (n, type, status, extra) => Object.assign({id: 'l-' + n, type, key: k(n), target: k(n), label: type + ' line-' + n + '-service', status, requestedAt: askedAt, requestedBy: 'dora.lane@example.com'}, status === 'done' ? {doneAt} : status === 'processing' ? {processingAt: doneAt} : status === 'failed' ? {failedAt: doneAt, result: 'the investigator is stopped'} : {}, extra || {});
  const acts = [act(1, 'dive', 'done', {result: 'one client retried 40 times, see https://jira.example.com/browse/OPS-120'}), act(2, 'dive', 'processing'), act(3, 'dive', 'requested'), act(4, 'dive', 'failed'), act(5, 'mute', 'done')];
  const serve = {state: stateAt(5, acts.map((x) => card(x.key))), runs: [], actions: acts};
  const {dom, w, doc, errors} = await boot(serve);
  const at = w.dayhm(doneAt);
  //when
  acts.forEach((x) => expand(article(doc, x.key)));
  const lines = acts.map((x) => article(doc, x.key).querySelector('.inc-more .last-act'));
  const history = [...doc.querySelectorAll('#q-list > li.hrow')].find((li) => li.textContent.includes('dive line-1-service'));
  //then
  assert.deepStrictEqual(lines.map((l) => l.hidden), [false, false, false, false, false]);
  assert.deepStrictEqual(lines.map((l) => l.textContent), [
    'Last action: Investigated ' + at + ' by dora.lane \u00b7 one client retried 40 times, see https://jira.example.com/browse/OPS-120',
    'Last action: Investigating\u2026 ' + at + ' by dora.lane',
    'Last action: Queued ' + w.dayhm(askedAt) + ' by dora.lane',
    'Last action: Investigate failed ' + at + ' by dora.lane \u00b7 the investigator is stopped',
    'Last action: Muted ' + at + ' by dora.lane'], 'the line uses the pill\'s words, not Done');
  assert.deepStrictEqual(lines.map((l) => l.querySelector('.st').className), ['st s-done', 'st s-processing', 'st s-queued', 'st s-failed', 'st s-done'], 'the colors stay');
  assert.deepStrictEqual(lines.map((l) => l.querySelector('.st').getAttribute('data-tip')), acts.map((x) => x.label), 'the action\'s own label is the tooltip');
  assert.strictEqual(lines[0].querySelector('a').getAttribute('href'), 'https://jira.example.com/browse/OPS-120', 'the result keeps its link');
  assert.strictEqual(lines[0].querySelector('.la-h').textContent, 'Last action:');
  assert.strictEqual(history.querySelector('summary .st').textContent, 'Done ' + w.hm(doneAt), 'History rows keep the generic chip');
  assert.deepStrictEqual(errors, []);
  close(dom);
  console.log('PASS lastActionLineNamesTheActionLikeThePill');
}

async function historyRowsFoldAndRememberWhichAreOpen() {
  //given
  const serve = {state: stateAt(5, [card(KEY), card(NEW)]), runs: [], actions: ACTS, allRuns: [PAGE_ASK, CARD_RUN]};
  const {dom, w, doc, errors} = await boot(serve);
  const actRows = () => [...doc.querySelectorAll('#q-list > li.hrow')];
  const podRowsH = () => [...doc.querySelectorAll('#pod-list > li.hrow')];
  const sum = (li) => li.querySelector('summary').textContent;
  const body = (li) => li.querySelector('.hbody');
  const byText = (rows, t) => rows().find((li) => li.textContent.includes(t));
  //when
  const closedFirst = [actRows().length, actRows().every((li) => !li.querySelector('details').open), podRowsH().length, podRowsH().every((li) => !li.querySelector('details').open)];
  const done = byText(actRows, 'reports-service'), queued = byText(actRows, 'Mute checkout-service'), report = byText(actRows, 'Post-deploy check');
  const page = byText(podRowsH, 'How does shop.example.com look?'), cardRun = byText(podRowsH, 'Ticket');
  const shapes = {doneSum: sum(done), doneBody: body(done).textContent, queuedCancel: queued.querySelector('summary button').textContent, reportPre: body(report).querySelector('pre').textContent,
    pageSum: sum(page), pageBody: [body(page).querySelector('.answer') !== null, body(page).querySelectorAll('.shots a.shot').length, body(page).querySelector('a.record').getAttribute('href'), body(page).querySelector('a.record').hasAttribute('data-json')],
    cardSum: sum(cardRun), cardBody: body(cardRun).textContent};
  let toggled = 0;
  [report, page].forEach((li) => li.querySelector('details').addEventListener('toggle', () => { toggled += 1; }));
  report.querySelector('details').open = true;
  page.querySelector('details').open = true;
  await until(() => toggled === 2);
  serve.actions = [{id: 'x5', type: 'mute', key: KEY, target: KEY, label: 'Mute reports-service for 24 h', status: 'done', requestedAt: iso(0), doneAt: iso(0), requestedBy: 'op@example.com', result: 'muted'}].concat(ACTS);
  await w.__skopaxActions();
  serve.allRuns = [PAGE_ASK, Object.assign({}, CARD_RUN, {outcome: 'failed'})];
  serve.state = stateAt(6, [card(KEY), card(NEW)]);
  await w.__skopaxPoll();
  await until(() => sum(byText(podRowsH, 'Ticket')).includes('failed'));
  const openAfter = [actRows().length, actRows().filter((li) => li.querySelector('details').open).map((li) => li.querySelector('.q-l').textContent),
    podRowsH().filter((li) => li.querySelector('details').open).map((li) => li.textContent.includes('How does shop.example.com look?')), sum(byText(podRowsH, 'Ticket')).includes('failed')];
  //then
  assert.deepStrictEqual(closedFirst, [4, true, 2, true], 'every History row starts folded');
  assert.strictEqual(shapes.doneSum, 'Investigate reports-service WARN logsDone ' + w.hm(ACTS[0].doneAt) + w.dayhm(ACTS[0].requestedAt) + ' \u00b7 op', 'the summary is label, status, time and who');
  assert.ok(shapes.doneBody.includes('Same PayCo outage') && !shapes.doneSum.includes('Same PayCo'), 'the result waits in the expanded row');
  assert.strictEqual(shapes.queuedCancel, 'Cancel');
  assert.strictEqual(shapes.reportPre, 'checkout-service err 40 (5/4)\npayments-api clean', 'the report is right in the expanded row');
  assert.ok(shapes.pageSum.includes('Ask by op@example.com: How does shop.example.com look?') && shapes.pageSum.includes('$0.30') && shapes.pageSum.includes('1 screenshot') && !shapes.pageSum.includes('hero banner'), shapes.pageSum);
  assert.deepStrictEqual(shapes.pageBody, [true, 1, '/api/investigations/r-page', true], 'answer, thumbnails and the run record live in the expanded run');
  assert.ok(!shapes.cardSum.includes('owns it') && shapes.cardBody.includes('owns it.') && shapes.cardBody.includes('second line'), 'a card run shows its whole recap when expanded');
  assert.deepStrictEqual(openAfter, [5, ['Post-deploy check'], [true], true], 'the open rows stay open across live re-renders');
  assert.deepStrictEqual(errors, []);
  close(dom);
  console.log('PASS historyRowsFoldAndRememberWhichAreOpen');
}

async function historyNamesTheCardsAndKeepsTheHowToInTheFoot() {
  //given
  const GONE = 'log|x-service|ERROR|gone thing', FIVE = '5xx|platform/audit-service';
  const runs = [
    {id: 'r-1', mode: 'live', trigger: 'cards', keys: [KEY, NEW], startedAt: iso(5), outcome: 'done', usd: 0.2, toolCalls: 2, recap: 'two cards'},
    {id: 'r-2', mode: 'live', trigger: 'cards', keys: [GONE, FIVE], startedAt: iso(9), outcome: 'done', usd: 0.1, toolCalls: 1, recap: 'gone cards'},
    {id: 'r-3', mode: 'live', trigger: 'ask', keys: [KEY], startedAt: iso(12), outcome: 'done', usd: 0.1, toolCalls: 1, recap: 'answer', ask: {text: 'Why?', by: 'op@example.com'}},
  ];
  const serve = {state: stateAt(5, [card(KEY, {msg: 'ShipCorp 456 invalid client ip'}), card(NEW)]), runs: [], allRuns: runs};
  const {dom, doc, errors} = await boot(serve);
  //when
  const names = (id) => [...doc.querySelectorAll('#pod-list > li.hrow')].find((li) => li.textContent.includes(id)).querySelectorAll('summary .hk');
  const shown = (t) => [...names(t)].map((s) => [s.textContent, s.getAttribute('title')]);
  const foot = doc.querySelector('#panel-docs .foot-body').textContent;
  //then
  assert.deepStrictEqual(shown('two cards'), [['reports-service / ShipCorp 456 invalid client ip', KEY], ['checkout-service / ERROR', NEW]], 'a card still on the page reads svc / message or svc / label, the raw key in the tooltip');
  assert.deepStrictEqual(shown('gone cards'), [['x-service \u00b7 ERROR \u00b7 gone thing', GONE], ['platform/audit-service', FIVE]], 'a card no longer in the state shows its key parts without the source prefix');
  assert.deepStrictEqual(shown('answer'), [['reports-service / ShipCorp 456 invalid client ip', KEY]], 'an ask names its attached card the same way');
  assert.ok(!doc.querySelector('#pod-list summary').textContent.includes('log|'), 'no raw key in a summary');
  assert.strictEqual(doc.getElementById('q-hint').textContent, '', 'the Actions section opens without the long explanation');
  assert.ok(foot.includes('Investigate, Mute, Known noise and Post-deploy check queue an action'), 'the explanation lives in How this works on the Docs page');
  assert.deepStrictEqual(errors, []);
  close(dom);
  console.log('PASS historyNamesTheCardsAndKeepsTheHowToInTheFoot');
}

const Q = 'Why did ShipCorp 456 come back?';
const askBox = (c) => c.querySelector('.card-runs .card-ask');
const typeIn = (w, input, text) => { input.value = text; input.dispatchEvent(new w.Event('input', {bubbles: true})); };
const pressEnter = (w, input) => input.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
const runRows = (doc) => [...article(doc, KEY).querySelectorAll('.card-runs li')];

async function askAboutThisCardForOperatorsLockedForGuests() {
  //given
  const op = await boot({state: stateAt(5, [card(KEY)]), runs: RUNS});
  const guest = await boot({state: stateAt(5, [card(KEY)]), runs: RUNS, guest: true});
  const off = await boot({state: stateAt(5, [card(KEY)]), runs: RUNS, info: {mode: 'off', enabled: false, stopped: null, spentToday: 0, cap: 20}});
  //when
  [op, guest, off].forEach((b) => expand(article(b.doc, KEY)));
  await until(() => [op, guest, off].every((b) => loaded(b.doc, KEY)));
  const box = askBox(article(op.doc, KEY));
  const input = box.querySelector('input'), go = box.querySelector('button.ask-go');
  const gbox = askBox(article(guest.doc, KEY));
  const ginput = gbox.querySelector('input'), ggo = gbox.querySelector('button.ask-go');
  typeIn(guest.w, ginput, 'Why?');
  pressEnter(guest.w, ginput);
  //then
  assert.strictEqual(box.previousElementSibling.className, 'runs-body', 'the box sits at the bottom of Runs on this card, under the list');
  assert.strictEqual(input.getAttribute('placeholder'), 'Ask about this card');
  assert.strictEqual(input.getAttribute('maxlength'), '1000');
  assert.strictEqual(input.getAttribute('aria-label'), 'Ask the investigator about reports-service WARN', 'screen readers hear which card');
  assert.ok(/opens a private chat with the card attached/.test(input.title), input.title);
  assert.strictEqual(go.textContent, 'Ask');
  assert.deepStrictEqual([go.getAttribute('aria-disabled'), input.getAttribute('aria-disabled'), input.readOnly], [null, null, false], 'an operator can type and send');
  const why = 'Log in to use this';
  assert.deepStrictEqual([ggo.getAttribute('aria-disabled'), ggo.title], ['true', why], 'a guest sees Ask greyed out with the login reason');
  assert.deepStrictEqual([ginput.getAttribute('aria-disabled'), ginput.readOnly, ginput.title], ['true', true, why], 'and a read-only box with the same reason');
  assert.deepStrictEqual(guest.posts, [], 'Enter in a guest box sends nothing');
  assert.strictEqual(askBox(article(off.doc, KEY)), null, 'no box while the investigator is off, like the top Ask');
  assert.deepStrictEqual(op.errors.concat(guest.errors, off.errors), []);
  [op, guest, off].forEach((b) => close(b.dom));
  console.log('PASS askAboutThisCardForOperatorsLockedForGuests');
}

async function askAboutThisCardStartsAPrivateChatAndClearsTheBox() {
  //given
  const serve = {state: stateAt(5, [card(KEY), card(NEW)]), runs: RUNS};
  const {dom, w, doc, errors, posts} = await boot(serve);
  expand(article(doc, KEY));
  await until(() => loaded(doc, KEY));
  const input = askBox(article(doc, KEY)).querySelector('input');
  let hashed = false;
  w.addEventListener('hashchange', () => { hashed = true; });
  //when
  askBox(article(doc, KEY)).querySelector('.ask-go').click();
  const err = askBox(article(doc, KEY)).querySelector('.cask-err');
  const empty = [err.textContent, err.hidden, posts.length];
  typeIn(w, input, '  ' + Q + '  ');
  pressEnter(w, input);
  const sending = [askBox(article(doc, KEY)).querySelector('.ask-go').textContent, askBox(article(doc, KEY)).querySelector('.ask-go').disabled];
  await until(() => hashed);
  const rows = runRows(doc);
  const box = askBox(article(doc, KEY));
  //then
  assert.deepStrictEqual(empty, ['Type the question first.', false, 0], 'an empty box asks for the question and sends nothing');
  assert.deepStrictEqual(posts, [{text: Q, cards: [KEY]}], 'a private chat with this card attached and the question as its first message');
  assert.deepStrictEqual(sending, ['Sending\u2026', true]);
  assert.strictEqual(w.location.hash, '#t=chat/thread/c1', 'the Chat tab opens on the new chat, where the answer arrives');
  assert.ok(rows.length === 2 && !rows.some((li) => li.classList.contains('ask-wait')), 'no waiting row on the card: its runs stay as they were');
  assert.strictEqual(article(doc, KEY).querySelector('.card-runs h5').textContent, 'Runs on this card2', 'the count is runs only');
  assert.deepStrictEqual([box.querySelector('input').value, box.querySelector('.cask-err').hidden, box.querySelector('.ask-go').textContent, box.querySelector('.ask-go').disabled], ['', true, 'Ask', false], 'the box is ready for the next question');
  assert.ok(askBox(article(doc, NEW)).closest('.inc-more').hidden, 'a collapsed card keeps its box in the folded panel');
  assert.strictEqual(askBox(article(doc, NEW)).querySelector('input').value, '', 'another card\u2019s box is untouched');
  assert.deepStrictEqual(errors, []);
  close(dom);
  console.log('PASS askAboutThisCardStartsAPrivateChatAndClearsTheBox');
}

async function askAboutThisCardShowsEachErrorInline() {
  //given
  const serve = {state: stateAt(5, [card(KEY)]), runs: RUNS};
  const {dom, w, doc, errors, posts} = await boot(serve);
  expand(article(doc, KEY));
  await until(() => loaded(doc, KEY));
  const box = () => askBox(article(doc, KEY));
  const send = async (post) => {
    serve.post = post;
    typeIn(w, box().querySelector('input'), Q);
    box().querySelector('.ask-go').click();
    await until(() => !box().querySelector('.cask-err').hidden);
    const e = box().querySelector('.cask-err');
    return [e.textContent, e.hidden, box().querySelector('input').value, box().querySelector('.ask-go').textContent, box().querySelector('.ask-go').disabled, runRows(doc).some((li) => li.classList.contains('ask-wait'))];
  };
  //when
  const offTopic = await send({status: 422, detail: 'Only questions about Skopax and our systems: asks for a poem'});
  typeIn(w, box().querySelector('input'), Q + ' ');
  const clearedOnTyping = box().querySelector('.cask-err').hidden;
  const capped = await send({status: 429, detail: '6 asks an hour is the cap; try again later'});
  const down = await send({status: 500});
  const forbidden = await send({status: 403});
  const locked = box().querySelector('.ask-go').getAttribute('aria-disabled');
  const other = await boot({state: stateAt(5, [card(KEY)]), runs: RUNS, post: {status: 401}});
  expand(article(other.doc, KEY));
  await until(() => loaded(other.doc, KEY));
  typeIn(other.w, askBox(article(other.doc, KEY)).querySelector('input'), Q);
  askBox(article(other.doc, KEY)).querySelector('.ask-go').click();
  await until(() => !askBox(article(other.doc, KEY)).querySelector('.cask-err').hidden);
  //then
  assert.deepStrictEqual(offTopic, ['Only questions about Skopax and our systems: asks for a poem', false, Q, 'Ask', false, false], 'off topic shows the gate reason and keeps the question');
  assert.ok(clearedOnTyping, 'typing again clears the error');
  assert.deepStrictEqual(capped, ['6 asks an hour is the cap; try again later', false, Q, 'Ask', false, false], 'a 429 shows the server reason as it is');
  assert.deepStrictEqual(down.slice(0, 2), ['Not sent, try again.', false]);
  assert.deepStrictEqual(forbidden.slice(0, 2), ['Asking needs the operator role.', false]);
  assert.strictEqual(locked, 'true', 'a 403 locks Ask like the other operator buttons: a viewer cannot ask');
  assert.strictEqual(askBox(article(other.doc, KEY)).querySelector('.cask-err').textContent, 'Log in to ask.', 'a 401 says the same');
  assert.strictEqual(posts.length, 4);
  assert.deepStrictEqual(errors.concat(other.errors), []);
  close(dom);
  close(other.dom);
  console.log('PASS askAboutThisCardShowsEachErrorInline');
}

async function pageAskRunsOnTheCardShowTheQuestionAndTheFullAnswer() {
  //given
  const OLD = {id: 'r-old', mode: 'live', trigger: 'ask', actionId: 'act-old', keys: [KEY], startedAt: iso(40), outcome: 'done', usd: 0.3, recap: 'Earlier answer.\nSecond line of it.', ask: {text: Q, by: 'op@example.com'}};
  const RUN = {id: 'r-new', mode: 'live', trigger: 'ask', actionId: 'act-1', keys: [KEY], startedAt: iso(0), outcome: 'running', usd: 0, recap: '', ask: {text: Q, by: 'op@example.com'}};
  const ANSWER = 'ShipCorp answered 456 for 12 customers.\nTheir IP allow-list, see https://jira.example.com/browse/OPS-120\nNothing to do on ours.';
  const running = await boot({state: stateAt(5, [card(KEY)]), runs: [RUN].concat(RUNS, [OLD])});
  const answered = await boot({state: stateAt(5, [card(KEY)]), runs: [Object.assign({}, RUN, {outcome: 'done', usd: 0.52, recap: ANSWER})].concat(RUNS, [OLD])});
  //when
  [running, answered].forEach((b) => expand(article(b.doc, KEY)));
  await until(() => loaded(running.doc, KEY) && loaded(answered.doc, KEY));
  const oldRow = runRows(answered.doc).find((li) => li.textContent.includes('Earlier answer'));
  const oldAnswer = [oldRow.querySelector('.ra').textContent, oldRow.querySelectorAll('.ra br').length, oldRow.querySelector('.rl').textContent];
  const first = runRows(running.doc)[0];
  const runningRow = [first.className, first.querySelector('b').textContent, first.querySelector('.tag').textContent, first.querySelector('.rl').textContent, first.querySelector('.ra').textContent];
  const row = runRows(answered.doc)[0];
  //then
  assert.deepStrictEqual(oldAnswer, ['Earlier answer.Second line of it.', 1, 'op: ' + Q], 'a page ask shows its question and its full answer, line breaks kept');
  assert.deepStrictEqual(runningRow, ['page-ask', 'Page ask', 'running', 'op: ' + Q, 'Working on it\u2026']);
  assert.strictEqual(row.querySelector('.tag').textContent, 'done');
  assert.strictEqual(row.querySelector('.ra').textContent, ANSWER.replace(/\n/g, ''), 'the full answer, not only its first line');
  assert.strictEqual(row.querySelectorAll('.ra br').length, 2);
  assert.strictEqual(row.querySelector('.ra a').getAttribute('href'), 'https://jira.example.com/browse/OPS-120', 'links in the answer are clickable');
  assert.ok(runRows(answered.doc).find((li) => li.textContent.includes('Slack ask')).querySelector('.ra') === null, 'a Slack ask keeps its first line, the thread has the rest');
  assert.deepStrictEqual(running.errors.concat(answered.errors), []);
  close(running.dom);
  close(answered.dom);
  console.log('PASS pageAskRunsOnTheCardShowTheQuestionAndTheFullAnswer');
}

async function askAboutThisCardSurvivesLiveUpdates() {
  //given
  const serve = {state: stateAt(5, [card(KEY), card(NEW)]), runs: RUNS};
  const {dom, w, doc, errors} = await boot(serve);
  expand(article(doc, KEY));
  expand(article(doc, NEW));
  await until(() => loaded(doc, KEY) && loaded(doc, NEW));
  typeIn(w, askBox(article(doc, NEW)).querySelector('input'), 'Same cause as the ShipCorp card?');
  const before = article(doc, NEW);
  //when
  serve.state = stateAt(6, [card(KEY, {now: 14}), card(NEW)]);
  serve.allRuns = [{id: 'r-x', mode: 'live', trigger: 'cards', keys: [NEW], startedAt: iso(0), outcome: 'done', usd: 0.1, recap: 'x'}];
  await w.__skopaxPoll();
  await until(() => article(doc, NEW) !== before && w.INV_ROWS.some((r) => r.id === 'r-x'));
  const afterLive = [article(doc, NEW) !== before, askBox(article(doc, NEW)).querySelector('input').value, article(doc, KEY).classList.contains('open')];
  //when
  const input = askBox(article(doc, NEW)).querySelector('input');
  input.focus();
  typeIn(w, input, 'Same cause as the ShipCorp card? And since when?');
  serve.allRuns = serve.allRuns.concat([{id: 'r-y', mode: 'live', trigger: 'cards', keys: [KEY], startedAt: iso(0), outcome: 'done', usd: 0.2, recap: 'y'}]);
  w.loadInvestigations();
  await until(() => w.INV_ROWS.some((r) => r.id === 'r-y'));
  const whileTyping = [doc.activeElement === input, input.isConnected];
  input.blur();
  await until(() => askBox(article(doc, NEW)).querySelector('input') !== input);
  //then
  assert.deepStrictEqual(afterLive, [true, 'Same cause as the ShipCorp card?', true], 'the typed text survives the repaint and the other expanded card stays open');
  assert.deepStrictEqual(whileTyping, [true, true], 'the cards are not repainted under a focused box');
  assert.notStrictEqual(askBox(article(doc, NEW)).querySelector('input'), input, 'they repaint once the box is left');
  assert.strictEqual(askBox(article(doc, NEW)).querySelector('input').value, 'Same cause as the ShipCorp card? And since when?');
  assert.deepStrictEqual(errors, []);
  close(dom);
  console.log('PASS askAboutThisCardSurvivesLiveUpdates');
}

(async () => {
  for (const t of [seenBeforeLineInTheExpandedCard, runsOnThisCardLoadOnExpandAndSurviveALiveUpdate, runsEmptyOrUnavailableStayQuiet, recurringTagOnTheRowAndInTheSideMenu, runRecordOpensInTheJsonViewerAndThePageStays,
    screenshotsShowAsThumbnailsAndZoomInADialog, historyListShowsTheScreenshotsAndLinksOfEveryRun, activityRailShowsTheRunsScreenshotsAndAnswerLinks, slackDraftsShowTheirScreenshots,
    cardHeadShowsACompactActionBadgeAndTheFullLineInside, cardPillNamesTheActionNotJustDone, anActionFromBeforeTheCardFiredAgainIsMarkedEarlier, lastActionLineNamesTheActionLikeThePill, historyRowsFoldAndRememberWhichAreOpen, historyNamesTheCardsAndKeepsTheHowToInTheFoot,
    askAboutThisCardForOperatorsLockedForGuests, askAboutThisCardStartsAPrivateChatAndClearsTheBox, askAboutThisCardShowsEachErrorInline, pageAskRunsOnTheCardShowTheQuestionAndTheFullAnswer, askAboutThisCardSurvivesLiveUpdates]) await t();
})().catch((e) => { console.error(e); process.exit(1); });
