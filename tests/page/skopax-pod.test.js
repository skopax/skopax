const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const KEY = 'log|checkout-service|ERROR|boom';
const POD_KEY = 'log|payment-service|ERROR|quiet';

function stateWith() {
  return {rev: 5, updatedAt: iso(0), sweptIso: iso(1), nextIso: iso(120), sweptAt: '21:04', nextRun: '22:04',
    offline: {since: iso(300), back: 'on request', note: 'Paused by the user at 13:18 UTC.'}, serverPasses: {prom: {at: iso(7)}},
    sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'h', points: [], at: iso(1)},
    anomalies: [{key: KEY, svc: 'checkout-service', label: 'ERROR pattern', sev: 'warn', status: 'open', now: 40, d1: 5, d7: 4, firstSeen: iso(30), lastSeen: iso(1), ns: '-', src: 'logs'},
      {key: POD_KEY, svc: 'payment-service', label: 'ERROR pattern', sev: 'warn', status: 'open', now: 9, d1: 2, d7: 2, firstSeen: iso(40), lastSeen: iso(1), ns: '-', src: 'logs', triage: 'handled', triagedBy: 'pod'},
      {key: 'integrity-off|web/web-bff|ANDROID', svc: 'web-bff', label: 'integrity off', sev: 'info', status: 'open', now: 235, d1: 230, d7: 200, firstSeen: iso(900), lastSeen: iso(1), ns: 'web', src: 'prom', triage: 'progress', ticket: 'Android nonce fix, no Jira task yet'}],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, deploys: []};
}

const RUNS = [
  {id: 'r2', mode: 'shadow', keys: [KEY], startedAt: iso(5), outcome: 'done', usd: 0.62, toolCalls: 2, recap: 'One client retried 40 times.',
   writes: [{tool: 'triage', key: KEY, summary: 'handled', proposed: true}], flags: [{tool: 'esql', phrase: 'known noise', excerpt: 'q=known noise'}]},
  {id: 'r3', mode: 'live', keys: [], startedAt: iso(2), outcome: 'done', usd: 0.41, toolCalls: 3, recap: 'Spike at 14:00 UTC: one client.\nNo action needed.', ask: {text: 'Why did checkout-service spike?', by: 'dora.lane'}, writes: [], flags: []},
];

const ACT = [
  {id: 'a6', t: iso(4), kind: 'run', who: 'investigator', text: 'run done \u00b7 3 turns \u00b7 2 tool calls \u00b7 $0.62', runId: 'r2', outcome: 'done'},
  {id: 'a5', t: iso(4.02), kind: 'write', who: 'investigator', text: 'note (proposed) \u00b7 ' + KEY + ' \u00b7 one client retried 40 times', runId: 'r2', tool: 'note', key: KEY, proposed: true},
  {id: 'a4', t: iso(4.5), kind: 'tool', who: 'investigator', text: 'esql \u00b7 1200 ms \u00b7 40 rows', runId: 'r2', tool: 'esql'},
  {id: 'a3', t: iso(5), kind: 'run', who: 'investigator', text: 'run started \u00b7 shadow \u00b7 ' + KEY, runId: 'r2'},
  {id: 'a2', t: iso(7), kind: 'merge', who: 'server', text: '1 new'},
  {id: 'a1', t: iso(7.02), kind: 'pass', who: 'server', text: 'prom ok \u00b7 logs ok'},
];

const USAGE = {since: iso(7 * 1440), runs: 4, usd: 2, turns: 30, usage: {input_tokens: 12000, output_tokens: 11800, cache_read_input_tokens: 150000, cache_creation_input_tokens: 28000}, cacheShare: 0.789,
  models: {'claude-opus-5-5': 30}, outcomes: {done: 4}, tools: [{name: 'esql', calls: 12, ms: 90000, maxMs: 59085, truncated: 1, errors: 1, avgMs: 7500}, {name: 'card', calls: 4, ms: 8, maxMs: 3, truncated: 0, errors: 0, avgMs: 2}],
  keys: [{key: KEY, runs: 1, usd: 0.62}], slowest: [{run: 'r2', tool: 'esql', ms: 59085, startedAt: iso(5)}]};

async function boot(info, operator, narrow, hash, extra) {
  const errors = [], puts = [], gets = [], bodies = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = stateWith();
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state).replace(/<\//g, '<\\/'));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/' + (hash || ''), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      if (!narrow) w.localStorage.setItem('skopax-live', '1');
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p, opt) => {
        const path = String(p);
        if (opt && opt.method === 'PUT') { puts.push([path, JSON.parse(opt.body)]); return {ok: true, status: 200, json: async () => Object.assign({}, info, {stopped: {by: 'dora.lane', at: iso(0)}})}; }
        gets.push(path);
        if (opt && opt.method === 'POST') bodies.push(JSON.parse(opt.body));
        if (opt && opt.method === 'POST' && path === '/api/chats') return {ok: true, status: 201, json: async () => ({id: 'c7'})};
        const body = path.startsWith('/api/me') ? Object.assign({loginEnabled: true, user: operator ? 'dora.lane@example.com' : null, operator: !!operator, admin: !!operator, jira: !!operator, task: {project: 'OPS', parent: 'OPS-103', points: 1}}, extra || {})
          : path.startsWith('/api/rev') ? {rev: state.rev} : path.startsWith('/api/investigations') ? RUNS : path.startsWith('/api/server') ? {investigate: info} : path.startsWith('/api/activity') ? ACT.slice()
          : path.startsWith('/api/investigator/usage') ? USAGE : [];
        return {ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body)};
      };
    },
  });
  await wait(400);
  return {dom, w: dom.window, doc: dom.window.document, errors, puts, gets, bodies};
}

async function activityRailStreamsServerAndPodLines() {
  //given
  const {dom, w, doc, gets, errors} = await boot({mode: 'shadow', enabled: true, stopped: null, spentToday: 1.2, cap: 20}, true);
  const rows = () => [...doc.querySelectorAll('#live-rows li')];
  const text = (li) => [...li.children].map((c) => c.textContent.trim()).join(' ');
  //when
  const before = rows().map(text);
  const classes = rows().map((li) => li.className);
  ACT.unshift({id: 'a7', t: iso(1), kind: 'tool', who: 'pod', text: 'triage \u00b7 0 ms \u00b7 error: a critical card stays open', runId: 'r9', tool: 'triage', error: 'a critical card stays open'});
  await w.__activityPoll();
  await w.__activityPoll();
  const hide = doc.getElementById('live-hide');
  hide.click();
  const closed = [doc.getElementById('live').className, hide.textContent, hide.getAttribute('aria-expanded')];
  hide.click();
  //then
  assert.strictEqual(before.length, 6);
  const at = (id) => w.hm(ACT.find((x) => x.id === id).t);
  assert.strictEqual(before[0], at('a6') + ' investigator run done \u00b7 3 turns \u00b7 2 tool calls \u00b7 $0.62', 'the newest line of a run carries the time and who');
  assert.deepStrictEqual(before.slice(1, 4), ['\u251c note (proposed) \u00b7 ' + KEY + ' \u00b7 one client retried 40 times', '\u251c esql \u00b7 1200 ms \u00b7 40 rows', '\u2514 run started \u00b7 shadow \u00b7 ' + KEY], 'the rest of the run hangs under it');
  assert.ok(classes[1].includes('child') && classes[1].includes('write') && classes[2].includes('tool') && !classes[4].includes('child'), 'kinds on the rows, server lines stand alone');
  assert.strictEqual(before[4], at('a2') + ' server 1 new');
  assert.strictEqual(before[5], at('a1') + ' server prom ok \u00b7 logs ok');
  assert.strictEqual(rows().length, 7, 'a new line arrives on the next poll and repeats are not added twice');
  assert.ok(rows()[0].classList.contains('err') && /error: a critical card stays open/.test(text(rows()[0])), 'a refused write shows in the error colour');
  assert.strictEqual(rows()[0].querySelector('.lw').textContent, 'investigator', 'rows written as who: pod before the rename show as investigator');
  assert.ok(gets.some((p) => /^\/api\/activity\?limit=60&since=/.test(p)), 'later polls ask for lines since the newest one');
  assert.deepStrictEqual(closed, ['live is-closed', 'Activity', 'false'], 'Hide folds the rail to a strip');
  assert.strictEqual(doc.getElementById('live').className, 'live');
  assert.strictEqual(doc.getElementById('live-hide').textContent, 'Hide');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS the Activity rail lists server and pod lines newest first, groups a run under its newest line, polls with since, dedupes and folds');
}

async function cardShowsTheShadowRun() {
  //given
  const {dom, w, doc, errors} = await boot({mode: 'shadow', enabled: true, stopped: null, spentToday: 1.2, cap: 20}, true);
  //when
  const sec = doc.querySelector('#anoms article.inc .podrun');
  w.setFilter('all');
  const podCard = [...doc.querySelectorAll('#anoms article.inc')].find((a) => a.dataset.key === POD_KEY);
  //then
  assert.ok(sec, 'pod run section on the card');
  assert.match(sec.textContent, /Investigator \(shadow\)/);
  assert.match(sec.textContent, /2 tool calls \u00b7 \$0\.62/);
  assert.match(sec.textContent, /One client retried 40 times\./);
  assert.match(sec.textContent, /Would have: triage handled/);
  assert.match(sec.textContent, /Planted-looking text: known noise/);
  assert.ok(podCard, 'the pod-handled card shows under All');
  assert.match(podCard.textContent, / \u00b7 by Investigator/, 'the pill names the investigator by its display label');
  const pill = podCard.querySelector('.tag.tri-wrap');
  assert.ok(pill && pill.querySelector('[data-no-tx]').textContent === 'Investigator', 'the label is its own untranslated element inside the wrapping pill');
  assert.match([...doc.querySelectorAll('style')].map((x) => x.textContent).join('\n'), /\.tag\.tri-wrap \{[^}]*white-space: normal;[^}]*max-width: 100%;/, 'the pill wraps inside its row instead of running past it');
  const usd = doc.querySelector('#anoms article.inc .line .usd');
  assert.strictEqual(usd.textContent, '$0.62', 'the card line shows what the investigator spent on it');
  assert.strictEqual(usd.title, 'Investigator: 1 run, $0.62');
  assert.ok(!podCard.querySelector('.line .usd'), 'no badge on a card without a run');
  assert.deepStrictEqual([...sec.closest('article.inc').querySelectorAll('.acts button.icon')].map((b) => b.getAttribute('aria-label') || b.textContent), ['Investigate', 'Assign', 'More actions', 'Mute 24 h', 'Mute 1 w', 'Known noise', 'Start progress', 'Group', 'Resolve', 'Copy link'], 'icon action buttons keep their names for screen readers');
  assert.ok(sec.closest('article.inc').querySelectorAll('.acts button.icon > .sr').length === 9 && [...sec.closest('article.inc').querySelectorAll('.acts button.icon > .lbl')].every((l) => l.parentNode.classList.contains('asg-btn')) && sec.closest('article.inc').querySelector('.acts button.icon').title.startsWith('Investigate \u00b7 last run '), 'labels hidden on every icon button, Investigate too, tooltips carry them');
  assert.strictEqual(doc.getElementById('off-tag').hidden, true, 'the laptop offline flag and the stale nextIso no longer pause the page');
  assert.ok(!/paused/i.test(doc.getElementById('banner').textContent), 'no paused banner while the pod runs');
  assert.ok(!doc.getElementById('pause-btn') && doc.getElementById('off-tag').hidden, 'no Pause button and no stopped tag in the header while the investigator runs');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS card shows the shadow run, its proposals, its flags and the pod triage mark; the laptop pause flag is ignored');
}

async function investigateAgainNeedsASecondClick() {
  //given
  const {dom, w, doc, puts, gets} = await boot({mode: 'live', enabled: true, stopped: null, spentToday: 0, cap: 20}, true);
  w.setFilter('all');
  const dive = (card) => [...card.querySelectorAll('.acts button')].find((b) => b.textContent === 'Investigate' || b.textContent === 'Investigate again?');
  const cards = [...doc.querySelectorAll('#anoms article.inc')];
  const investigated = cards.find((c) => c.dataset.key === KEY);
  const fresh = cards.find((c) => c.dataset.key === POD_KEY);
  const posts = () => gets.filter((p) => p === '/api/actions').length;
  const wrap = doc.getElementById('cf-wrap');
  //when
  const tipBefore = dive(investigated).title;
  dive(investigated).click();
  const asked = [wrap.hidden, doc.getElementById('cf-title').textContent, doc.getElementById('cf-text').textContent, doc.getElementById('cf-yes').textContent, posts()];
  doc.getElementById('cf-no').click();
  await wait(50);
  const cancelled = [wrap.hidden, posts()];
  dive(investigated).click();
  doc.getElementById('cf-yes').click();
  await wait(100);
  const confirmed = [wrap.hidden, posts()];
  const freshTip = dive(fresh).title;
  dive(fresh).click();
  await wait(100);
  //then
  assert.match(tipBefore, /^Investigate \u00b7 last run \d\d:\d\d \(shadow\) \u00b7 \$0\.62 \u00b7 asks before running again$/, 'the tooltip names the earlier run and its cost');
  assert.deepStrictEqual(asked, [false, 'Investigate again?', 'checkout-service ERROR pattern was investigated ' + w.dayhm(RUNS[0].startedAt) + ' (shadow, $0.62). A new run costs about the same and starts from the earlier findings.', 'Investigate again', 0], 'the click opens a dialog with the earlier run, nothing queued yet');
  assert.deepStrictEqual(cancelled, [true, 0], 'Cancel closes it and queues nothing');
  assert.deepStrictEqual(confirmed, [true, 1], 'Investigate again queues the dive and closes the dialog');
  assert.strictEqual(freshTip, 'Investigate: the investigator takes it within about two minutes');
  assert.ok(wrap.hidden && posts() === 2, 'a card without a run queues at once, no dialog');
  assert.deepStrictEqual(puts, []);
  dom.window.close();
  console.log('PASS Investigate on an already investigated card opens a confirm dialog (Cancel / Investigate again); a fresh card queues at once');
}

const SPARKLE = 'M10 5Q11 12 18 13Q11 14 10 21Q9 14 2 13Q9 12 10 5Z';

async function investigateIsAnIconOnlyButtonWithTheSparklesIcon() {
  //given
  const {dom, w, doc, gets, errors} = await boot({mode: 'live', enabled: true, stopped: null, spentToday: 0, cap: 20}, true);
  w.setFilter('all');
  const card = [...doc.querySelectorAll('#anoms article.inc')].find((c) => c.dataset.key === KEY);
  const dive = card.querySelector('.acts button.inv-btn');
  const svg = dive.querySelector('svg');
  const others = [...card.querySelectorAll('.acts button.icon')].filter((b) => b !== dive);
  const wrap = doc.getElementById('cf-wrap');
  const posts = () => gets.filter((p) => p === '/api/actions').length;
  //when
  svg.querySelector('path').dispatchEvent(new w.MouseEvent('click', {bubbles: true}));
  const byIcon = [wrap.hidden, doc.getElementById('cf-title').textContent];
  doc.getElementById('cf-no').click();
  await wait(50);
  //then
  assert.ok([...doc.querySelectorAll('#anoms .acts .labeled')].every((b) => b.classList.contains('asg-btn')), 'Investigate is icon only since 9 Nov; only Assign carries its word, since 11 Nov');
  assert.strictEqual(card.querySelector('.acts button'), dive, 'Investigate leads the row');
  assert.ok(dive.classList.contains('icon') && !dive.classList.contains('sec'), 'it keeps the blue primary style');
  assert.deepStrictEqual([...dive.children].map((c) => c.tagName.toLowerCase() + (c.getAttribute('class') ? '.' + c.getAttribute('class') : '')), ['svg', 'span.sr'], 'the icon, then the word for screen readers only');
  assert.strictEqual(dive.textContent, 'Investigate');
  assert.strictEqual(dive.getAttribute('aria-label'), 'Investigate', 'the accessible name stays');
  assert.match(dive.title, /^Investigate \u00b7 last run \d\d:\d\d \(shadow\) \u00b7 \$0\.62 \u00b7 asks before running again$/, 'the tooltip names the earlier run');
  assert.strictEqual(dive.dataset.tip, dive.title);
  assert.deepStrictEqual(['viewBox', 'fill', 'stroke', 'stroke-width', 'stroke-linecap', 'stroke-linejoin', 'aria-hidden'].map((k) => svg.getAttribute(k)), ['0 0 24 24', 'none', 'currentColor', '2', 'round', 'round', 'true'], 'drawn like the other icons');
  assert.ok(!svg.querySelector('circle') && !svg.querySelector('line'), 'not the magnifier any more');
  assert.strictEqual(svg.querySelector('path').getAttribute('d'), SPARKLE, 'a four-point sparkle leads the icon');
  assert.ok(svg.querySelectorAll('path').length >= 2, 'with a small sparkle beside it');
  assert.deepStrictEqual(others.map((b) => b.getAttribute('aria-label') || b.textContent), ['Assign', 'More actions', 'Mute 24 h', 'Mute 1 w', 'Known noise', 'Start progress', 'Group', 'Resolve', 'Copy link'], 'the other icon buttons; Create task is the worded Create Jira task button since 11 Nov');
  assert.ok(others.filter((b) => !b.classList.contains('asg-btn')).every((b) => b.classList.contains('icon') && b.querySelector(':scope > .sr') && !b.querySelector('.lbl')), 'and stay icon-only, Assign apart');
  assert.ok(card.querySelector('.acts .asg-btn > .lbl'), 'Assign carries its word since 11 Nov');
  assert.ok(card.querySelector('.notif .slack-btn.labeled > .lbl'), 'the Slack button in the Notify row carries its word next to the icon, like Teams, since 11 Nov');
  assert.deepStrictEqual(byIcon, [false, 'Investigate again?'], 'a click on the icon opens the Investigate again? dialog');
  assert.ok(wrap.hidden && posts() === 0, 'Cancel still queues nothing');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS Investigate is an icon-only button (sparkles, blue) leading the row, the other icon buttons stay icon-only, and a click on the icon still asks before running again');
}

async function guestsSeeInvestigateLockedWithItsIcon() {
  //given
  const {dom, w, doc, gets, errors} = await boot({mode: 'live', enabled: true, stopped: null, spentToday: 0, cap: 20}, false);
  const card = [...doc.querySelectorAll('#anoms article.inc')].find((c) => c.dataset.key === KEY);
  const dive = card.querySelector('.acts button.inv-btn');
  //when
  dive.querySelector('svg path').dispatchEvent(new w.MouseEvent('click', {bubbles: true}));
  await wait(50);
  //then
  assert.strictEqual(dive.getAttribute('aria-disabled'), 'true', 'the operator gate locks it');
  assert.strictEqual(dive.title, 'Log in to use this', 'the tooltip says why');
  assert.ok(dive.dataset.tip.startsWith('Investigate'), 'the own tooltip waits for the unlock');
  assert.ok(dive.querySelector('svg') && dive.querySelector('.sr').textContent === 'Investigate' && dive.getAttribute('aria-label') === 'Investigate', 'locked, it keeps the icon and its name');
  assert.ok(doc.getElementById('cf-wrap').hidden && !gets.includes('/api/actions'), 'a click on the icon opens no dialog and queues nothing');
  assert.deepStrictEqual(errors.map((e) => e.split(' (')[0]), ['Not implemented: navigation to another Document'], 'it sends the guest to /login instead');
  dom.window.close();
  console.log('PASS guests see Investigate locked with its icon and name; a click goes to the login, not the dialog');
}

async function takeReticketsAnInProgressCardAndResolveMarksItDone() {
  //given
  const K = 'integrity-off|web/web-bff|ANDROID';
  const {dom, w, doc, gets, bodies} = await boot({mode: 'live', enabled: true, stopped: null, spentToday: 0, cap: 20}, true);
  w.setFilter('all');
  const cards = [...doc.querySelectorAll('#anoms article.inc')];
  const progress = cards.find((c) => c.dataset.key === 'integrity-off|web/web-bff|ANDROID');
  const handled = cards.find((c) => c.dataset.key === POD_KEY);
  const icon = (card, t) => [...card.querySelectorAll('.acts button.icon')].find((b) => b.textContent === t);
  const posts = () => gets.filter((p) => p === '/api/actions').length;
  //when
  icon(progress, 'Start progress').click();
  const input = progress.querySelector('input[id^="ticket-"]');
  const prefilled = input.value;
  input.value = 'WEB-95';
  progress.querySelector('input[id^="take-why-"]').value = 'nonce fix shipped in WEB-95';
  progress.querySelector('.acts .jira-btn').click();
  await wait(50);
  const dialogOpen = !doc.getElementById('task-wrap').hidden;
  const title = doc.getElementById('task-title').value;
  const desc = doc.getElementById('task-desc').value;
  doc.getElementById('task-project').value = 'OPS';
  doc.getElementById('task-project').dispatchEvent(new w.Event('change'));
  const meta = doc.getElementById('task-meta').textContent;
  doc.getElementById('task-title').value = 'Android nonce fix';
  doc.getElementById('task-points').value = '2';
  doc.getElementById('task-yes').click();
  await wait(150);
  const ticketBody = bodies[bodies.length - 1];
  const dialogClosed = doc.getElementById('task-wrap').hidden;
  [...progress.querySelectorAll('.reason button')].find((b) => b.textContent === 'Start progress').click();
  await wait(100);
  const takeBody = bodies[bodies.length - 1];
  icon(progress, 'Resolve').click();
  progress.querySelector('input[id^="why-"]').value = 'done';
  [...progress.querySelectorAll('.reason button')].find((b) => b.textContent === 'Resolve').click();
  await wait(100);
  //then
  assert.ok(progress.textContent.includes('In progress \u00b7 Android nonce fix, no Jira task yet'), 'the card is In progress with a free-text ticket');
  assert.match(icon(progress, 'Start progress').title, /^Start progress: change the ticket/, 'the tooltip says what Take does on an In-progress card');
  assert.strictEqual(prefilled, 'Android nonce fix, no Jira task yet', 'the ticket field starts with the current ticket');
  assert.strictEqual(posts(), 3, 'Create task, Take and Resolve post one action each');
  assert.ok(dialogOpen && dialogClosed, 'Create task opens its dialog and closes it after the post');
  assert.strictEqual(title, 'web-bff integrity off', 'the task title starts from the card');
  assert.ok(desc.startsWith('info \u00b7 web-bff \u00b7 integrity off \u00b7 now 235 vs 1 d 230 / 7 d 200 \u00b7 since '), desc);
  assert.ok(meta.startsWith('Project OPS \u00b7 No epic \u00b7 No sprint'), 'no sprint field set, so no sprint is promised: ' + meta);
  assert.deepStrictEqual(ticketBody, {type: 'ticket', target: K, key: K, title: 'Android nonce fix', description: desc, points: 2, project: 'OPS', label: 'Create OPS task for web-bff integrity off'});
  assert.deepStrictEqual(takeBody, {type: 'take', target: K, key: K, ticket: 'WEB-95', reason: 'nonce fix shipped in WEB-95', label: 'Take web-bff integrity off with WEB-95'});
  assert.deepStrictEqual(bodies[bodies.length - 1], {type: 'resolve', target: K, key: K, kind: 'fixed', reason: 'done', label: 'Resolve web-bff integrity off (fixed)'}, 'Resolve sends Fixed unless another kind is picked');
  assert.ok(!icon(handled, 'Resolve') && !icon(handled, 'Start progress'), 'an Action-taken card has neither Take nor Resolve');
  const combo = progress.querySelector('input[id^="parent-"]');
  icon(progress, 'Group').click();
  combo.dispatchEvent(new dom.window.Event('focus'));
  const offered = [...dom.window.document.getElementById(combo.getAttribute('aria-controls')).querySelectorAll('[role="option"]')].map((r) => r.dataset.key);
  assert.ok(icon(progress, 'Group') && offered.includes(KEY), 'Group offers the other open cards as parent');
  assert.ok(progress.querySelector('.notif .slack-btn') && !progress.querySelector('.acts .slack-btn') && progress.querySelector('.notif .nl').textContent === 'Notify', 'Send to Slack sits in the Notify strip, not in the action row');
  dom.window.close();
  console.log('PASS Take re-tickets an In-progress card with the ticket prefilled, Resolve marks it No action needed, Group lists the other open cards, and the Slack buttons live in the Notify strip');
}

async function historyListsRunsAndStopsTheInvestigator() {
  //given
  const {dom, doc, puts} = await boot({mode: 'live', enabled: true, stopped: null, spentToday: 3.4, cap: 20}, true);
  //when
  const head = doc.querySelector('#pod-ctl').textContent;
  const row = doc.querySelector('#pod-list li').textContent;
  const stop = doc.querySelector('#pod-ctl button.op-btn');
  stop.click();
  const afterFirst = [puts.length, stop.textContent];
  stop.click();
  await wait(100);
  //then
  assert.deepStrictEqual(afterFirst, [0, 'Click again to confirm']);
  assert.match(head, /live \u00b7 \$3\.40 of \$20\.00 today/);
  assert.strictEqual(doc.querySelector('#pod-ctl .pod-usage').textContent, 'Usage \u00b7 7 d \u00b7 4 runs \u00b7 $2.00 \u00b7 cache 79 % \u00b7 Open Costs \u203a', 'the 7-day usage is one line with a link to the Costs tab');
  assert.ok(!doc.querySelector('#pod-ctl details.usage'), 'no usage fold any more: the detail lives on Costs');
  assert.ok(doc.getElementById('hist-runs').open && !doc.getElementById('hist-acts').open, 'runs open by default, actions folded');
  assert.match(row, /done/);
  assert.match(row, /One client retried 40 times\./);
  assert.deepStrictEqual(puts, [['/api/investigate', {stopped: true}]]);
  assert.match(doc.querySelector('#pod-ctl').textContent, /Stopped by dora\.lane/);
  assert.strictEqual(doc.getElementById('off-tag').hidden, false);
  assert.strictEqual(doc.getElementById('off-tag').textContent, 'INVESTIGATOR STOPPED');
  assert.match(doc.getElementById('banner').textContent, /^Investigator stopped \u00b7 by dora\.lane /);
  assert.ok(!/Investigation|every source|wait/.test(doc.getElementById('banner').textContent), 'no explanation in the paused banner');
  assert.ok(!doc.getElementById('off-tag').hidden && doc.getElementById('off-tag').textContent === 'INVESTIGATOR STOPPED', 'the header tag says the investigator is stopped');
  assert.match(doc.getElementById('banner').textContent, /Investigator stopped \u00b7 by dora.lane .* the checks still run/, 'the banner says what is stopped and where to resume');
  dom.window.close();
  console.log('PASS History lists pod runs and Stop pod investigator confirms, calls PUT, shows who stopped it and flips the header to PAUSED');
}

async function offHidesThePauseControls() {
  //given
  const {dom, doc} = await boot({mode: 'off', enabled: false, stopped: null, spentToday: 0, cap: 20}, true, true);
  //then
  assert.strictEqual(doc.getElementById('live').className, 'live is-closed', 'without a saved choice the rail starts folded on a viewport under 1500 px');
  assert.ok(!doc.getElementById('pause-btn') && doc.getElementById('off-tag').hidden, 'nothing about the investigator in the header when it is off');
  assert.strictEqual(doc.getElementById('off-tag').hidden, true);
  dom.window.close();
  console.log('PASS mode off hides the Pause button and shows no PAUSED tag');
}

async function resumeNeedsTwoClicksToo() {
  //given
  const {dom, doc, puts} = await boot({mode: 'live', enabled: true, stopped: {by: 'dora.lane', at: iso(1)}, spentToday: 3.4, cap: 20}, true);
  //when
  const btn = doc.querySelector('#pod-ctl button.op-btn');
  const label = btn.textContent;
  btn.click();
  const afterFirst = puts.length;
  btn.click();
  await wait(100);
  //then
  assert.strictEqual(label, 'Resume');
  assert.strictEqual(afterFirst, 0);
  assert.deepStrictEqual(puts, [['/api/investigate', {stopped: false}]]);
  dom.window.close();
  console.log('PASS Resume confirms before calling PUT with stopped false');
}

async function guestsSeeTheButtonLocked() {
  //given
  const {dom, doc} = await boot({mode: 'live', enabled: true, stopped: null, spentToday: 0, cap: 20}, false);
  //then
  assert.strictEqual(doc.querySelector('#pod-ctl button.op-btn').getAttribute('aria-disabled'), 'true');
  dom.window.close();
  console.log('PASS guests see Stop pod investigator and the header Pause button locked');
}

async function askTheInvestigatorOpensAPrivateChatWithTheQuestion() {
  //given
  const {dom, w, doc, bodies, gets} = await boot({mode: 'live', enabled: true, stopped: null, spentToday: 0, cap: 20}, true);
  const btn = doc.getElementById('ask-btn');
  //when
  btn.click();
  await wait(50);
  const open = !doc.getElementById('ask-wrap').hidden;
  const options = Array.from(doc.querySelectorAll('#ask-cards input')).map((c) => c.value);
  doc.getElementById('ask-yes').click();
  const emptyErr = doc.getElementById('ask-err').textContent;
  doc.getElementById('ask-text').value = 'Why did checkout-service spike?';
  doc.querySelectorAll('#ask-cards input')[0].checked = true;
  doc.getElementById('ask-yes').click();
  await wait(150);
  //then
  assert.ok(open, 'the ask dialog opens for a logged-in person');
  assert.deepStrictEqual(options, [KEY, POD_KEY], 'open warning and critical cards can be attached, info ones not');
  assert.strictEqual(emptyErr, 'Type the question first.');
  assert.deepStrictEqual(bodies[bodies.length - 1], {text: 'Why did checkout-service spike?', cards: [KEY]}, 'the question and the picked card start a private chat');
  assert.ok(gets.includes('/api/chats') && !gets.includes('/api/actions'), 'no queued ask action any more');
  assert.ok(doc.getElementById('ask-wrap').hidden, 'the dialog closes');
  assert.strictEqual(w.location.hash, '#t=chat/thread/c7', 'and the page opens that chat, where the answer comes');
  dom.window.close();
  //given
  const guest = await boot({mode: 'live', enabled: true, stopped: null, spentToday: 0, cap: 20}, false);
  //when
  guest.doc.getElementById('ask-btn').click();
  await wait(50);
  //then
  assert.strictEqual(guest.doc.getElementById('ask-btn').getAttribute('aria-disabled'), 'true');
  assert.ok(guest.doc.getElementById('ask-wrap').hidden, 'a guest cannot open the ask dialog');
  guest.dom.window.close();
  console.log('PASS Ask the investigator opens a private chat with the question and the attached card and goes there, and guests see the button locked');
}

async function aRefusedAskShowsTheReasonAndKeepsTheDialog() {
  //given
  const {dom, w, doc} = await boot({mode: 'live', enabled: true, stopped: null, spentToday: 0, cap: 20}, true);
  const inner = w.fetch;
  w.fetch = async (p, opt) => String(p).startsWith('/api/chats') && opt && opt.method === 'POST'
    ? {ok: false, status: 429, json: async () => ({detail: 'You have used today\'s 40 chat messages; more tomorrow.'})}
    : inner(p, opt);
  //when
  doc.getElementById('ask-btn').click();
  await wait(50);
  doc.getElementById('ask-text').value = 'One more question';
  doc.getElementById('ask-yes').click();
  await wait(150);
  //then
  assert.strictEqual(doc.getElementById('ask-err').textContent, 'You have used today\'s 40 chat messages; more tomorrow.');
  assert.ok(!doc.getElementById('ask-wrap').hidden, 'the dialog stays open on a refusal');
  assert.strictEqual(doc.getElementById('ask-yes').textContent, 'Ask in chat');
  assert.ok(!w.location.hash.startsWith('#t=chat'), 'no chat opened: ' + w.location.hash);
  dom.window.close();
  console.log('PASS A refused ask shows the reason from the server and keeps the dialog open');
}

async function aSlackAskRowShowsTheAskerAndTheThreadLink() {
  //given
  RUNS.push(
    {id: 'r4', mode: 'live', keys: [KEY], startedAt: iso(1), outcome: 'done', usd: 0.2, toolCalls: 1, recap: 'One client.', writes: [], flags: [],
     ask: {text: 'Why did it go up?', by: 'adam.baker', source: 'slack', thread: 'https://example.slack.com/archives/C1/p1792224000000200'}},
    {id: 'r5', mode: 'live', keys: [], startedAt: iso(1), outcome: 'done', usd: 0.1, toolCalls: 0, recap: 'x', writes: [], flags: [],
     ask: {text: 'Bad link', by: 'adam.baker', source: 'slack', thread: 'javascript:alert(1)'}});
  const {dom, doc} = await boot({mode: 'live', enabled: true, stopped: null, spentToday: 0, cap: 20}, true);
  //when
  const rows = Array.from(doc.querySelectorAll('#pod-list li'));
  const good = rows.find((li) => li.textContent.includes('Why did it go up?'));
  const bad = rows.find((li) => li.textContent.includes('Bad link'));
  //then
  assert.ok(good && good.textContent.includes('Slack request by adam.baker: Why did it go up?'), good && good.textContent);
  assert.strictEqual(good.querySelector('a').getAttribute('href'), 'https://example.slack.com/archives/C1/p1792224000000200');
  assert.ok(bad && !bad.querySelector('summary a'), 'only https Slack links are rendered');
  RUNS.splice(RUNS.length - 2, 2);
  dom.window.close();
  console.log('PASS A Slack ask row shows the asker and a link to its Slack thread, and only https Slack links');
}

async function anAutoResolvedCardSaysSoUnderActionTaken() {
  //given
  const {dom, w, doc, errors} = await boot({mode: 'live', enabled: true, stopped: null, spentToday: 0, cap: 20}, true);
  const st = stateWith();
  const AUTO_KEY = 'log|x-service|ERROR|quiet';
  st.anomalies.push({key: AUTO_KEY, svc: 'x-service', label: 'ERROR pattern', sev: 'warn', status: 'cleared', clearedAt: iso(4400), now: 0, d1: 5, d7: 4, firstSeen: iso(4500), lastSeen: iso(4400), ns: '-', src: 'logs',
    triage: 'handled', triagedBy: 'auto', triagedAt: iso(60), autoResolvedAt: iso(60), note: {text: 'Auto-resolved: cleared 22.10 14:05, nobody acted in 3 days', at: iso(60)}});
  //when
  w.paint(st);
  w.setFilter('all');
  const card = [...doc.querySelectorAll('#anoms article.inc')].find((c) => c.dataset.key === AUTO_KEY);
  const group = card && card.closest('.grp');
  //then
  assert.ok(card, 'the auto-resolved card is on the page');
  assert.strictEqual(group.querySelector('.grp-h h3').textContent, 'Done', 'it sits under Done');
  assert.ok([...card.querySelectorAll('.tag')].some((t) => t.textContent === 'No action needed \u00b7 auto-resolved'), 'the triage tag says it was auto-resolved');
  assert.ok(!doc.querySelector('#anoms details.grp.wait article.inc[data-key="' + AUTO_KEY + '"]'), 'not waiting any more');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS an auto-resolved card reads No action needed \u00b7 auto-resolved and sits under Done');
}

async function aStoppedCardNobodyActedOnIsJustOpen() {
  //given
  const {dom, w, doc} = await boot({mode: 'live', enabled: true, stopped: null, spentToday: 0, cap: 20}, true);
  const st = stateWith();
  st.anomalies.push({key: 'log|x-service|ERROR|gone', svc: 'x-service', label: 'ERROR pattern', sev: 'warn', status: 'cleared', clearedAt: iso(50), now: 0, d1: 5, d7: 4, firstSeen: iso(300), lastSeen: iso(60), ns: '-', src: 'logs'});
  //when
  w.paint(st);
  w.setFilter('open');
  const groups = [...doc.querySelectorAll('#anoms .grp')].map((g) => g.tagName + ' ' + g.querySelector('.grp-h h3').textContent);
  const card = doc.querySelector('#anoms section.grp article.inc[data-key="log|x-service|ERROR|gone"]');
  w.setFilter('stale');
  const oldLink = [w.filter, [...doc.querySelectorAll('#anoms .grp h3')].map((h) => h.textContent)];
  //then
  assert.deepStrictEqual(groups, ['SECTION Open'], 'one Open group, no separate Waiting group');
  assert.ok(card, 'the stopped card nobody acted on sits in Open');
  assert.ok(/Stopped firing \u00b7 unassigned /.test(card.querySelector('.c-status').textContent), 'its row says it stopped');
  assert.strictEqual(doc.getElementById('count-anoms').textContent, '2', 'the Anomalies tab shows one Open count');
  assert.ok(!/waiting to be addressed/.test(doc.getElementById('tab-anoms').title), 'the tab tooltip has no separate waiting count');
  assert.strictEqual(doc.querySelector('#panel-anoms .qf-c[data-f="stale"]'), null, 'no Waiting item in the side menu');
  assert.strictEqual(doc.querySelector('#reco [data-open-stale]'), null, 'no waiting link in the summary row');
  assert.deepStrictEqual(oldLink, ['open', ['Open']], 'an old waiting link opens Open');
  dom.window.close();
  console.log('PASS a stopped card nobody acted on is just Open: one group, one count, no Waiting item or link, its row says it stopped');
}

async function headerInvestigatorMenuHoldsAskAndStop() {
  //given
  const {dom, doc, puts} = await boot({mode: 'live', enabled: true, stopped: null, spentToday: 3.4, cap: 20, lastRun: iso(9)}, true, false, '', {chat: true});
  const btn = doc.getElementById('inv-btn'), pop = doc.getElementById('inv-pop');
  //when
  const shown = !btn.hidden;
  btn.click();
  const opened = !pop.hidden;
  const state = doc.getElementById('inv-state').textContent;
  const order = [...pop.querySelectorAll('button')].map((b) => b.id + (b.classList.contains('sec') ? ':sec' : ':primary'));
  doc.getElementById('inv-chat').click();
  const chatHash = dom.window.location.hash, chatClosed = pop.hidden;
  btn.click();
  doc.getElementById('inv-ask').click();
  await wait(30);
  const askOpened = !doc.getElementById('ask-wrap').hidden && pop.hidden;
  doc.getElementById('ask-no').click();
  btn.click();
  doc.getElementById('inv-stop').click();
  const confirmLabel = doc.getElementById('inv-stop').textContent;
  doc.getElementById('inv-stop').click();
  await wait(100);
  //then
  assert.ok(shown && opened, 'the header Investigator button shows in live mode and opens its menu');
  assert.match(state, /^live \u00b7 \$3\.40 of \$20\.00 today \u00b7 last run /);
  assert.deepStrictEqual(order, ['inv-chat:primary', 'inv-ask:sec', 'inv-stop:sec'], 'Chat with Skopax leads the menu as the primary button');
  assert.ok(chatHash === '#t=chat' && chatClosed, 'Chat with Skopax opens the Chat tab and closes the menu');
  assert.ok(askOpened, 'Ask from the menu opens the ask dialog and closes the menu');
  assert.strictEqual(confirmLabel, 'Click again to confirm');
  assert.deepStrictEqual(puts, [['/api/investigate', {stopped: true}]]);
  assert.strictEqual(btn.textContent, 'Investigator \u00b7 $3.40 / $20.00 \u00b7 stopped', 'the header button carries today\'s spend and the daily cap');
  dom.window.close();
  //given
  const off = await boot({mode: 'off', enabled: false, stopped: null, spentToday: 0, cap: 20}, true);
  //then
  assert.ok(off.doc.getElementById('inv-btn').hidden, 'no Investigator menu when the mode is off');
  off.dom.window.close();
  console.log('PASS the header Investigator menu holds Ask and Stop / Resume with the state line, and hides when the mode is off');
}

async function theHeaderMenuFollowsTheChatAndInvestigatorSwitches() {
  //given
  const live = {mode: 'live', enabled: true, stopped: null, spentToday: 3.4, cap: 20, lastRun: iso(9)};
  const OFF = 'Server text: the AI investigator is switched off.';
  const chatOff = await boot(live, true, false, '', {chat: true, features: {chat: false, investigator: true}, featureOff: {chat: 'Chat is off.'}});
  const noChatUi = await boot(live, true, false, '', {chat: false, features: {chat: true, investigator: true}, featureOff: {}});
  const aiOff = await boot(live, true, false, '', {chat: true, features: {chat: true, investigator: false}, featureOff: {investigator: OFF}});
  const allOn = await boot(live, true, false, '', {chat: true, features: {chat: true, investigator: true}, featureOff: {}});
  const menu = (x) => {
    const btn = x.doc.getElementById('inv-btn');
    btn.click();
    const out = {shown: !btn.hidden, text: btn.textContent, off: btn.classList.contains('off'), title: btn.title, state: x.doc.getElementById('inv-state').textContent,
      items: [...x.doc.querySelectorAll('#inv-pop button')].filter((b) => !b.hidden).map((b) => b.id)};
    btn.click();
    return out;
  };

  //when
  const noChat = menu(chatOff), noUi = menu(noChatUi), noAi = menu(aiOff), both = menu(allOn);

  //then
  assert.deepStrictEqual(noChat.items, ['inv-stop'], 'with Chat switched off the menu offers neither Chat with nor Ask the investigator, whose chats would be refused');
  assert.strictEqual(noChat.text, 'Investigator \u00b7 $3.40 / $20.00');
  assert.deepStrictEqual(noUi.items, ['inv-stop'], 'without the chat UI or single sign-on (me.chat false) the menu offers no chat either, as the sidebar hides its Chat row');
  assert.deepStrictEqual([noAi.shown, noAi.text, noAi.off], [true, 'Investigator \u00b7 off', true], 'with the AI investigator switched off the button shows the off state instead of the spend');
  assert.deepStrictEqual([noAi.title, noAi.state], [OFF, OFF], 'and gives the server off text, not a copy of its own');
  assert.deepStrictEqual([both.items, both.off, both.text], [['inv-chat', 'inv-ask', 'inv-stop'], false, 'Investigator \u00b7 $3.40 / $20.00'], 'with both on nothing changes');
  assert.deepStrictEqual(chatOff.errors.concat(noChatUi.errors, aiOff.errors, allOn.errors), []);
  for (const x of [chatOff, noChatUi, aiOff, allOn]) x.dom.window.close();
  console.log('PASS the header Investigator menu hides Chat with and Ask the investigator while Chat is off or not set up, and shows the server off text while the AI investigator is switched off');
}

async function aCardLinkCutByACharacterStillOpensTheCard() {
  //given
  const {dom, doc, w, errors} = await boot({mode: 'live', enabled: true, stopped: null, spentToday: 0, cap: 20}, true, false, '#a=' + encodeURIComponent(KEY).slice(0, -2));
  const card = [...doc.querySelectorAll('#anoms article.inc')].find((c) => c.dataset.key === KEY);
  //then
  assert.ok(card, 'the card is on the page');
  assert.ok(!doc.getElementById('link-note'), 'the cut key resolved to the one card whose key starts with it: no "no longer on the page" note');
  assert.strictEqual(w.location.hash, w.cardHash(w.ALL.filter((x) => x.key === KEY)[0]), 'and the address shows the full link of the card it opened');
  assert.strictEqual(w.linkKey('a|b.(c)!'), 'a%7Cb.%28c%29%21', 'Copy link percent-encodes parentheses and the other characters encodeURIComponent leaves');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS a card link cut by a character still opens the card, and Copy link encodes parentheses');
}

(async () => {
  await aCardLinkCutByACharacterStillOpensTheCard();
  await headerInvestigatorMenuHoldsAskAndStop();
  await theHeaderMenuFollowsTheChatAndInvestigatorSwitches();
  await anAutoResolvedCardSaysSoUnderActionTaken();
  await aStoppedCardNobodyActedOnIsJustOpen();
  await askTheInvestigatorOpensAPrivateChatWithTheQuestion();
  await aRefusedAskShowsTheReasonAndKeepsTheDialog();
  await aSlackAskRowShowsTheAskerAndTheThreadLink();
  await cardShowsTheShadowRun();
  await historyListsRunsAndStopsTheInvestigator();
  await resumeNeedsTwoClicksToo();
  await offHidesThePauseControls();
  await guestsSeeTheButtonLocked();
  await investigateAgainNeedsASecondClick();
  await investigateIsAnIconOnlyButtonWithTheSparklesIcon();
  await guestsSeeInvestigateLockedWithItsIcon();
  await takeReticketsAnInProgressCardAndResolveMarksItDone();
  await activityRailStreamsServerAndPodLines();
})().catch((e) => { console.error(e); process.exit(1); });
