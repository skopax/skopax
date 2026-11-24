const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const CORE_PACK = /^CORE_PACK = '(.+)'$/m.exec(fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'locales.py'), 'utf8'))[1];
const SLOT = '<script type="text/plain" id="plugin-scripts"></script>';
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const hhmm = (at) => { const d = new Date(at); return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'); };
const KEY = 'prom|shop/inventory|restarts|inventory-api';
const KEY2 = 'prom|shop/search|errors|5xx';
const KEY3 = 'prom|shop/payments|latency|p95';
const STARTED = iso(2);

function run(over) {
  return Object.assign({id: 'run-45', trigger: 'cards', actionId: 'a45', keys: [KEY], pod: 'skopax-abc:1', room: 'investigator', startedAt: STARTED, model: 'claude-opus-5-5', effort: 'high',
    toolCalls: 3, usd: 0.21, lastTool: 'kube', lastToolAt: iso(0)}, over || {});
}

function board(over) {
  return Object.assign({at: iso(0), mode: 'live', enabled: true, halted: null,
    rooms: [{id: 'investigator-room-0', name: 'Room 1', takes: 'questions, Investigate clicks and deploy assessments', pod: null, until: null, run: null},
            {id: 'investigator', name: 'Cards', takes: 'new and escalated cards, and the queue when the rooms are full', pod: 'skopax-abc', until: iso(-10), run: run()}],
    unplaced: [], waiting: [], recent: [],
    limits: {slackAsks: 0, slackAskCap: 10, gateCalls: 0, gateCap: 60, spentToday: 1, cap: 25, askers: [], perPersonCap: 4, pageAsks: 0, pageAskCap: 6}}, over || {});
}

function idle(over) {
  const b = board(over);
  b.rooms = b.rooms.map((r) => Object.assign({}, r, {run: null}));
  return b;
}

function queued(cards, at) {
  return {id: 'w-' + cards.join(','), type: 'dive', source: 'page', by: 'alice@example.com', at: at || iso(3), text: 'Investigate', cards, thread: null, why: 'next for a free room'};
}

function card(key, num, extra) {
  const [src, place, sig] = key.split('|');
  const [ns, svc] = place.split('/');
  return Object.assign({key, num, src, sig, ns, svc, sev: 'warn', label: sig, status: 'open', firstSeen: iso(40), lastSeen: iso(1), what: 'x'}, extra || {});
}

function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(KEY, 45, {triage: 'investigating', triagedBy: 'pod'}), card(KEY2, 46), card(KEY3, 47)],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(serve, opts) {
  opts = opts || {};
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  let html = pageHtml(TEMPLATE, {view: opts.view || 'board'}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state()));
  if (opts.pack) html = html.replace(SLOT, () => '<script>' + CORE_PACK.replace(/%s/g, ((args) => () => args.shift())([JSON.stringify(opts.pack[0]), JSON.stringify(opts.pack[1]).replace(/</g, '\\u003c')])) + '</script>');
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=anoms/all', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      if (opts.pack) w.document.cookie = 'skopax_lang=' + opts.pack[0] + '; path=/';
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        p = String(p);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/investigator/status')) return serve.board ? ok(serve.board) : {ok: false, status: 503, json: async () => ({})};
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', operator: true, teams: [], tags: []});
        if (p.startsWith('/api/card-runs')) return serve.cardRuns ? ok(serve.cardRuns) : {ok: false, status: 404, json: async () => ({})};
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests') || p.startsWith('/api/investigations')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/nodes')) return ok({nodes: []});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors};
}

function later(w, ms) {
  const was = w.Date.now.bind(w.Date);
  w.Date.now = () => was() + ms;
}

async function refresh(t, serve, b) {
  serve.board = b;
  t.w.loadInvestigator();
  await wait(60);
}

const banner = (doc) => doc.querySelector('.card-drawer article.inc .run-banner');
const bits = (b) => [...b.querySelectorAll('.run-bits > span')].map((s) => s.textContent);
const tileTag = (doc, key) => [...doc.querySelectorAll('#anoms .tile')].find((t) => t.dataset.key === key).querySelector('.tag.tri-inv');
const pill = (doc) => doc.getElementById('run-pill');
const pillText = (doc) => [...pill(doc).querySelectorAll('.rp-say, .rp-bit')].map((s) => s.textContent);

async function aRunningCardShowsTheBannerUnderItsHeaderWithElapsedToolCallsCostAndStep() {
  //given
  const serve = {board: board()};
  const t = await boot(serve);

  //when
  t.w.openDrawer(KEY, true);
  await wait(30);

  //then
  const b = banner(t.doc);
  assert.ok(b && b.classList.contains('run-on'), 'the drawer card carries a running banner');
  const head = b.parentNode;
  assert.ok(head.classList.contains('inc-head') && b.previousElementSibling.classList.contains('inc-title') && b.nextElementSibling.classList.contains('acts'), 'right under the header row, above the action groups');
  const say = b.querySelector('.run-say');
  assert.strictEqual(say.textContent, 'Investigating');
  assert.strictEqual(say.getAttribute('role'), 'status');
  assert.strictEqual(say.getAttribute('aria-live'), 'polite');
  assert.deepStrictEqual(bits(b), ['started ' + hhmm(STARTED), '2 min', '3 tool calls', '$0.21', 'last tool kube']);
  assert.strictEqual(b.querySelector('.run-step code').getAttribute('data-no-tx'), '', 'the tool name stays as it is');
  assert.strictEqual(b.querySelector('.run-rec').getAttribute('href'), '/api/investigations/run-45');
  assert.strictEqual(b.querySelector('.run-rec').textContent, 'Run record');
  assert.ok(b.querySelector('.run-ind[aria-hidden="true"]') && !b.querySelector('.run-ind.run-q'));
  assert.strictEqual(t.doc.querySelector('.card-drawer article.inc .c-status').textContent, 'Investigating', 'the header keeps the state word');
  const chip = t.doc.querySelector('.card-drawer article.inc .tech-tags .tag.tri-inv');
  assert.ok(chip.querySelector('.run-ind:not(.run-q)'), 'the chip in Technical details carries the activity indicator');
  assert.strictEqual(chip.textContent, 'Investigating \u00b7 by Investigator', 'the indicator adds no text');
  t.w.INV[KEY] = {id: 'run-45', startedAt: STARTED, outcome: 'running', toolCalls: 0, usd: 0, mode: 'live'};
  assert.ok(t.w.invHtml({key: KEY}).includes(t.w.dayhm(STARTED) + ' \u00b7 running</span>') && !t.w.invHtml({key: KEY}).includes('tool calls'), 'the stored run line no longer claims 0 tool calls and $0.00 while it runs');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS a running card shows the banner under its header with started, elapsed, tool calls, cost, last tool and the run record');
}

async function anAskRunSaysAskingAndAPrivateChatRunRaisesNoBanner() {
  //given
  const ask = board();
  ask.rooms[1].run = run({trigger: 'ask', ask: {text: 'Why?', by: 'alice@example.com'}});
  const chat = board();
  chat.rooms[1].run = run({trigger: 'chat', ask: {private: true}});
  const serve = {board: ask};
  const t = await boot(serve);
  t.w.openDrawer(KEY, true);
  await wait(30);
  const asking = banner(t.doc).querySelector('.run-say').textContent;

  //when
  await refresh(t, serve, chat);

  //then
  assert.strictEqual(asking, 'Asking');
  assert.strictEqual(banner(t.doc), null, 'a private chat never names the card');
  assert.ok(!tileTag(t.doc, KEY).querySelector('.run-ind'));
  assert.deepStrictEqual(pillText(t.doc), ['Investigator running', '2 min']);
  assert.strictEqual(pill(t.doc).getAttribute('href'), '#t=inv');
  t.w.close();
  console.log('PASS an ask run says Asking; a private chat run raises no card banner and the top bar names no card');
}

async function aQueuedCardSaysHowManyAreAheadWithoutARunRecord() {
  //given
  const serve = {board: board({waiting: [queued([KEY3]), queued([KEY2], iso(4))]})};
  const t = await boot(serve);

  //when
  t.w.openDrawer(KEY2, true);
  await wait(30);

  //then
  const b = banner(t.doc);
  assert.ok(b.classList.contains('run-wait'));
  assert.strictEqual(b.querySelector('.run-say').textContent, 'Queued, 1 ahead');
  assert.deepStrictEqual(bits(b), ['waiting 4 min']);
  assert.ok(b.querySelector('.run-ind.run-q'), 'a queued card has the still, hollow indicator');
  assert.strictEqual(b.querySelector('.run-rec'), null, 'no run yet, so no run record');
  t.w.openDrawer(KEY3, true);
  await wait(30);
  assert.strictEqual(banner(t.doc).querySelector('.run-say').textContent, 'Queued, next');
  t.w.close();
  console.log('PASS a queued card says how many are ahead and how long it waits, with a hollow indicator and no run record');
}

async function theBannerTurnsIntoTheResultLineThenGoes() {
  //given
  const serve = {board: board()};
  const t = await boot(serve);
  t.w.openDrawer(KEY, true);
  await wait(30);
  const say = banner(t.doc).querySelector('.run-say');

  //when
  await refresh(t, serve, idle({recent: [{id: 'run-45', trigger: 'cards', startedAt: STARTED, outcome: 'done', usd: 0.38, toolCalls: 7, keys: [KEY]}]}));

  //then
  const b = banner(t.doc);
  assert.ok(b.classList.contains('run-ok'));
  assert.strictEqual(b.querySelector('.run-say'), say, 'the same live region changes its words, so a screen reader hears the result');
  assert.strictEqual(say.textContent, 'Investigation done');
  assert.deepStrictEqual(bits(b), ['7 tool calls', '$0.38', 'see the findings below']);
  assert.strictEqual(b.querySelector('.run-ind'), null);
  assert.strictEqual(b.querySelector('.run-rec').getAttribute('href'), '/api/investigations/run-45');
  assert.ok(!tileTag(t.doc, KEY).querySelector('.run-ind'), 'the tile indicator goes when the run ends');

  //when
  await refresh(t, serve, idle({recent: [{id: 'run-45', outcome: 'done', usd: 0.38, toolCalls: 7, keys: [KEY]}]}));

  //then
  assert.strictEqual(banner(t.doc).querySelector('.run-say').textContent, 'Investigation done', 'an early refresh keeps the result line for a full poll interval');

  //when
  later(t.w, t.w.INV_POLL_MS + 1000);
  await refresh(t, serve, idle({recent: [{id: 'run-45', outcome: 'done', usd: 0.38, toolCalls: 7, keys: [KEY]}]}));

  //then
  assert.strictEqual(banner(t.doc), null, 'the result line goes once it is older than one poll interval');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS the banner turns into the result line in place, keeps it for a full poll interval, then it goes');
}

async function aFailedRunSaysWhy() {
  //given
  const serve = {board: board()};
  const t = await boot(serve);
  t.w.openDrawer(KEY, true);
  await wait(30);

  //when
  await refresh(t, serve, idle({recent: [{id: 'run-45', outcome: 'failed', usd: 0.01, toolCalls: 0, keys: [KEY], error: 'no Anthropic API key is set'}]}));

  //then
  const b = banner(t.doc);
  assert.ok(b.classList.contains('run-bad'));
  assert.strictEqual(b.querySelector('.run-say').textContent, 'Investigation failed');
  assert.strictEqual(b.querySelector('.run-why').textContent, 'no Anthropic API key is set');
  t.w.close();
  console.log('PASS a failed run says Investigation failed and why');
}

async function boardTilesAndListRowsCarryTheIndicatorOnTheInvestigatingTag() {
  //given
  const serve = {board: board()};
  const t = await boot(serve);

  //then
  const tag = tileTag(t.doc, KEY);
  assert.ok(tag.querySelector('.run-ind:not(.run-q)'));
  assert.strictEqual(tag.textContent, 'Investigating');

  //when
  await refresh(t, serve, idle({waiting: [queued([KEY])]}));

  //then
  assert.ok(tileTag(t.doc, KEY).querySelector('.run-ind.run-q'), 'queued turns the indicator hollow');
  t.w.close();

  //given
  const list = await boot({board: board()}, {view: 'list'});

  //then
  const row = [...list.doc.querySelectorAll('#anoms article.inc')].find((c) => c.dataset.key === KEY);
  assert.ok(row.querySelector('.tech-tags .tag.tri-inv .run-ind'), 'the list row tag in Technical details has it too');
  assert.ok(row.querySelector('.inc-head > .run-banner'), 'the banner is there for when the row opens');
  const css = [...list.doc.querySelectorAll('style')].map((s) => s.textContent).join('\n');
  assert.ok(css.includes('article.inc:not(.open) .run-banner { display: none; }'), 'a folded row shows only the tag');
  list.w.close();
  console.log('PASS board tiles and list rows carry the indicator on the Investigating tag, hollow when queued');
}

async function theTopBarPillSaysWhatRunsWhatWaitsAndWhenTheInvestigatorIsHeldBack() {
  //given
  const serve = {board: board()};
  const t = await boot(serve);

  //then
  const p = pill(t.doc);
  assert.ok(!p.hidden && p.closest('header.top .top-r') && !p.closest('#last-check'), 'in the top bar, outside Last check');
  assert.deepStrictEqual(pillText(t.doc), ['Investigating #45', '2 min']);
  const spoken = (el) => [...el.childNodes].filter((n) => !(n.getAttribute && n.getAttribute('aria-hidden') === 'true')).map((n) => n.textContent).join('');
  assert.strictEqual(spoken(p), 'Investigating #45, 2 min', 'a screen reader hears the parts apart');
  assert.strictEqual(p.getAttribute('href'), '#a=' + encodeURIComponent(KEY), 'one run on one card opens that card');
  assert.ok(p.classList.contains('run-on') && p.querySelector('.run-ind:not(.run-q)'));
  assert.strictEqual(p.querySelector('.rp-n').textContent, '1');

  //when
  const two = board({waiting: [queued([KEY3])]});
  two.rooms[0].run = run({id: 'run-46', keys: [KEY2], room: 'investigator-room-0'});
  await refresh(t, serve, two);

  //then
  assert.deepStrictEqual(pillText(t.doc), ['Investigating 2 cards', '1 queued']);
  assert.strictEqual(p.getAttribute('href'), '#t=inv', 'several runs open the Investigator page');

  //when
  await refresh(t, serve, idle({waiting: [queued([KEY3]), queued([KEY2])]}));

  //then
  assert.deepStrictEqual(pillText(t.doc), ['Investigator', '2 queued']);
  assert.ok(p.classList.contains('run-wait') && p.querySelector('.run-ind.run-q'));

  //when
  await refresh(t, serve, idle({halted: 'stopped'}));

  //then
  assert.deepStrictEqual(pillText(t.doc), ['Investigator paused']);
  assert.ok(p.classList.contains('run-warn') && !p.querySelector('.run-ind') && p.getAttribute('href') === '#t=inv');

  //when
  await refresh(t, serve, idle({halted: 'capped'}));

  //then
  assert.deepStrictEqual(pillText(t.doc), ['Daily AI cap reached']);
  assert.ok(p.classList.contains('run-warn'));

  //when
  await refresh(t, serve, idle({enabled: false, problem: 'key'}));

  //then
  assert.deepStrictEqual(pillText(t.doc), ['Investigator off']);
  assert.ok(p.classList.contains('run-muted') && p.title === 'The investigator is on but has no AI key: set one on Admin');

  //when
  await refresh(t, serve, idle({enabled: false, problem: 'model'}));

  //then
  assert.ok(p.title === 'The investigator is on but its AI model is not known: pick one on Admin');

  //when
  await refresh(t, serve, idle({enabled: false, problem: null}));

  //then
  assert.ok(p.hidden, 'not enabled without a named problem is not called an error');

  //when
  await refresh(t, serve, idle());

  //then
  assert.ok(p.hidden, 'nothing extra when idle');

  //when
  await refresh(t, serve, idle({mode: 'off', enabled: false}));

  //then
  assert.ok(p.hidden, 'an install that never turned the AI on stays quiet');

  //when
  await refresh(t, serve, idle({halted: 'off', enabled: false}));

  //then
  assert.ok(p.hidden, 'an investigator switched off on purpose stays quiet');

  //when
  await refresh(t, serve, null);

  //then
  assert.ok(p.hidden && !t.doc.querySelector('#anoms .run-ind'), 'no status, no claims');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS the top bar pill: one card with elapsed, several cards, queued, paused, cap reached, off by error, and quiet when idle or off');
}

async function aBannerThatAppearsLaterIsAnnounced() {
  //given
  const serve = {board: idle()};
  const t = await boot(serve);
  t.w.openDrawer(KEY, true);
  await wait(30);
  const seen = [];
  new t.w.MutationObserver((ms) => ms.forEach((m) => [...m.addedNodes].forEach((n) => { if (n.classList && n.classList.contains('run-banner')) seen.push(n.querySelector('.run-say').textContent); }))).observe(t.doc.getElementById('anoms'), {childList: true, subtree: true});

  //when
  await refresh(t, serve, board());
  await wait(120);

  //then
  assert.deepStrictEqual(seen, [''], 'the live region goes in empty');
  assert.strictEqual(banner(t.doc).querySelector('.run-say').textContent, 'Investigating', 'then gets its words, so it is announced');
  t.w.close();
  console.log('PASS a banner that appears later goes in empty and then gets its words, so it is announced');
}

async function theCardStatLineShowsTheLiveCostDuringARun() {
  //given
  const serve = {board: board(), cardRuns: {[KEY]: {latest: {id: 'run-45', startedAt: STARTED, outcome: 'running', toolCalls: 0, usd: 0, mode: 'live', keys: [KEY]}, n: 2, usd: 0.1}}};
  const t = await boot(serve);
  await wait(200);

  //when
  t.w.openDrawer(KEY, true);
  await wait(30);

  //then
  const usd = t.doc.querySelector('.card-drawer article.inc .inc-more .line .usd');
  assert.strictEqual(usd.textContent, '$0.31', 'earlier runs plus the cost so far, like the banner');

  //when
  await refresh(t, serve, idle());

  //then
  assert.strictEqual(t.doc.querySelector('.card-drawer article.inc .inc-more .line .usd').textContent, '$0.10');
  t.w.close();
  console.log('PASS the card stat line adds the live cost while a run goes');
}

async function clickingThePillOpensTheCardWhenExactlyOneRuns() {
  //given
  const t = await boot({board: board()});

  //when
  pill(t.doc).click();
  await wait(60);

  //then
  const d = t.doc.querySelector('#anoms .card-drawer');
  assert.ok(d && d.dataset.key === KEY, 'the drawer of the running card opens');
  assert.ok(d.querySelector('.run-banner.run-on'));
  t.w.close();
  console.log('PASS clicking the pill opens the running card');
}

async function reducedMotionAndPhoneWidthAreHandledInTheStyles() {
  //given
  const t = await boot({board: board()});
  const rules = [];
  [...t.doc.styleSheets].forEach((s) => [...s.cssRules].forEach((r) => rules.push(r)));
  const inMedia = (match, sel) => rules.filter((r) => r.media && match(r.media.mediaText)).some((m) => [...m.cssRules].some((r) => r.selectorText === sel && sel && r.style));
  const ruleIn = (match, sel) => rules.filter((r) => r.media && match(r.media.mediaText)).map((m) => [...m.cssRules].find((r) => r.selectorText === sel)).find(Boolean);

  //then
  const still = ruleIn((m) => m.includes('prefers-reduced-motion: reduce'), '.run-ind::after');
  assert.ok(still && still.style.display === 'none' && still.style.animationName === 'none', 'reduced motion stops the pulse');
  const dot = rules.find((r) => r.selectorText === '.run-ind');
  assert.ok(dot && dot.style.background === 'var(--info)', 'the dot stays clearly marked without motion');
  assert.ok(inMedia((m) => m.includes('max-width: 640px'), '.run-pill .rp-say, .run-pill .rp-bit'));
  const hide = ruleIn((m) => m.includes('max-width: 640px'), '.run-pill .rp-say, .run-pill .rp-bit');
  assert.ok(hide.style.position === 'absolute' && hide.style.width === '1px', 'the words leave the phone bar but stay for screen readers');
  const n = ruleIn((m) => m.includes('max-width: 640px'), '.run-pill .rp-n');
  assert.strictEqual(n.style.display, 'inline', 'the phone shows the count');
  assert.strictEqual(ruleIn((m) => m.includes('max-width: 640px'), '.run-pill.run-paused').style.display, 'none', 'on a phone the stopped tag already says paused');
  const lead = ruleIn((m) => m.includes('max-width: 640px'), '.top .off-tag .ot-lead');
  assert.ok(lead && lead.style.display === 'none', 'on a phone the stopped tag says only Stopped');
  const label = ruleIn((m) => m.includes('max-width: 640px'), '#inv-btn .ib-t');
  assert.ok(label && label.style.position === 'absolute' && label.style.width === '1px', 'on a phone the spend button is an icon; its words stay for screen readers');
  assert.strictEqual(ruleIn((m) => m.includes('max-width: 640px'), '.top #inv-btn .ib-i').style.display, 'inline-block');
  const crumb = ruleIn((m) => m.includes('max-width: 640px'), '.top .crumb');
  assert.ok(crumb && crumb.style.flexShrink === '0', 'the page name never gives way');
  assert.ok(!rules.some((r) => r.media && [...r.cssRules].some((x) => x.selectorText === '#inv-btn' && x.style.textOverflow)), 'the spend button is not cut with an ellipsis');
  const base = rules.find((r) => r.selectorText === '.run-pill');
  assert.ok(base.style.whiteSpace === 'nowrap' && base.style.overflow === 'hidden' && base.style.minWidth === '0px', 'the pill never pushes the bar sideways');
  t.w.close();
  console.log('PASS reduced motion stops the pulse, the dot stays marked, and the phone shows a compact count');
}

function builtIn(lang) {
  const raw = JSON.parse(fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'locale', lang, 'page.json'), 'utf8'));
  return Object.assign({}, raw.page, {'@rules': raw.rules, '@months': raw.months, '@days': raw.days, '@tails': raw.tails, '@th': raw.headers});
}

const WORDS = ['Investigating', 'Asking', 'Queued, {0} ahead', 'Queued, next', 'started {0}', 'waiting {0} min', 'waiting {0} h {1} min', '{0} tool calls', '{0} tool call', 'last tool',
  'Investigation done', 'Investigation failed', 'Investigation finished', 'see the findings below', 'Run record', 'Investigating #{0}', 'Investigating {0} cards', 'Investigating {0} card',
  'Investigator running', '{0} runs', '{0} queued', 'Investigator paused', 'Daily AI cap reached', 'Investigator off', 'Opens the Investigator page', 'Opens the card',
  'Nothing new starts until someone resumes it', 'It starts again tomorrow, or when the daily limit is raised', 'The investigator is on but has no AI key: set one on Admin', 'The investigator is on but its AI model is not known: pick one on Admin'];

async function everyPackSaysTheBannerAndThePill() {
  for (const lang of ['de', 'es', 'fr', 'tr']) {
    //given
    const pack = builtIn(lang);
    const missing = WORDS.filter((w) => typeof pack[w] !== 'string' || !pack[w]);
    assert.deepStrictEqual(missing, [], lang);
    const serve = {board: board({waiting: [queued([KEY3])]})};
    const t = await boot(serve, {pack: [lang, pack]});

    //when
    t.w.openDrawer(KEY, true);
    await wait(60);

    //then
    const b = banner(t.doc);
    assert.strictEqual(b.querySelector('.run-say').textContent, pack['Investigating'], lang);
    assert.deepStrictEqual(bits(b).slice(1, 3), [pack['{0} min'].replace('{0}', '2'), pack['{0} tool calls'].replace('{0}', '3')], lang);
    assert.strictEqual(b.querySelector('.run-step code').textContent, 'kube', lang);
    assert.deepStrictEqual(pillText(t.doc), [pack['Investigating #{0}'].replace('{0}', '45'), pack['{0} min'].replace('{0}', '2'), pack['{0} queued'].replace('{0}', '1')], lang);
    t.w.close();
  }
  console.log('PASS every built-in pack says the banner and the pill');
}

async function theStoppedTagAndTheSpendButtonKeepTheirWords() {
  //given
  const t = await boot({board: idle({halted: 'stopped'})});
  t.w.INV_INFO = {mode: 'live', spentToday: 1.9, cap: 25, stopped: {by: 'alice@example.com', at: iso(5)}};

  //when
  t.w.liveness();
  t.w.renderInvMenu();

  //then
  const tag = t.doc.getElementById('off-tag'), btn = t.doc.getElementById('inv-btn');
  assert.strictEqual(tag.textContent, 'INVESTIGATOR STOPPED');
  assert.strictEqual(tag.querySelector('.ot-lead').textContent, 'INVESTIGATOR ');
  assert.strictEqual(btn.textContent, 'Investigator \u00b7 $1.90 / $25.00 \u00b7 stopped', 'the button keeps its accessible name with the amount');
  assert.ok(btn.querySelector('svg.ib-i[aria-hidden="true"]') && btn.title.includes('$1.90'));
  t.w.close();
  console.log('PASS the stopped tag and the spend button keep their words; the phone styles only hide parts');
}

(async () => {
  await aRunningCardShowsTheBannerUnderItsHeaderWithElapsedToolCallsCostAndStep();
  await anAskRunSaysAskingAndAPrivateChatRunRaisesNoBanner();
  await aQueuedCardSaysHowManyAreAheadWithoutARunRecord();
  await theBannerTurnsIntoTheResultLineThenGoes();
  await aFailedRunSaysWhy();
  await boardTilesAndListRowsCarryTheIndicatorOnTheInvestigatingTag();
  await theTopBarPillSaysWhatRunsWhatWaitsAndWhenTheInvestigatorIsHeldBack();
  await clickingThePillOpensTheCardWhenExactlyOneRuns();
  await reducedMotionAndPhoneWidthAreHandledInTheStyles();
  await everyPackSaysTheBannerAndThePill();
  await theStoppedTagAndTheSpendButtonKeepTheirWords();
  await aBannerThatAppearsLaterIsAnnounced();
  await theCardStatLineShowsTheLiveCostDuringARun();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
