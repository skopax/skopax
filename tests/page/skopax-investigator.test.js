const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const KEY = 'log|checkout-service|ERROR|boom';

function board(over) {
  const run = {id: 'run-1', trigger: 'slack', actionId: 'a1', keys: [KEY], pod: 'skopax-abc:1a2b', room: 'investigator-room-0', startedAt: iso(2), model: 'claude-sonnet-5-5', effort: 'high',
    ask: {text: 'Why did it go up?', by: 'adam.baker@example.com', source: 'slack', thread: 'https://example.slack.com/archives/C1/p1'}, toolCalls: 4, lastTool: 'esql', lastToolAt: iso(0)};
  return Object.assign({at: iso(0), mode: 'live', enabled: true, halted: null,
    rooms: [{id: 'investigator-room-0', name: 'Room 1', takes: 'questions, Investigate clicks and deploy assessments', pod: 'skopax-abc', until: iso(-10), run},
            {id: 'investigator-room-1', name: 'Room 2', takes: 'questions, Investigate clicks and deploy assessments', pod: null, until: null, run: null},
            {id: 'investigator', name: 'Cards', takes: 'new and escalated cards, and the queue when the rooms are full', pod: null, until: null, run: null}],
    unplaced: [],
    waiting: [{id: 'a2', type: 'ask', source: 'slack', by: 'nina.white@example.com', at: iso(1), text: 'And when did it start?', cards: [KEY], thread: 'https://example.slack.com/archives/C1/p2', why: 'behind an earlier question in the same Slack thread'},
              {id: 'a3', type: 'dive', source: 'page', by: 'dora.lane@example.com', at: iso(0), text: 'Investigate', cards: [KEY], thread: null, why: 'next for a free room'}],
    recent: [{id: 'run-0', trigger: 'cards', startedAt: iso(30), outcome: 'done', usd: 0.42, toolCalls: 7, keys: [KEY]},
             {id: 'run-9', trigger: 'slack', startedAt: iso(50), outcome: 'failed', usd: 0.05, toolCalls: 1, keys: [], ask: {text: 'can you see the ios repo?'}},
             {id: 'run-8', trigger: 'chat', startedAt: iso(55), outcome: 'done', usd: 0.12, toolCalls: 2, keys: [], ask: {private: true}}],
    limits: {slackAsks: 10, slackAskCap: 10, gateCalls: 12, gateCap: 60, spentToday: 4.1, cap: 25, askers: [{by: 'adam.baker@example.com', asks: 4}, {by: 'nina.white@example.com', asks: 1}], perPersonCap: 4, pageAsks: 1, pageAskCap: 6}}, over || {});
}

async function boot(serve, hash) {
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [{key: KEY, sev: 'warn', svc: 'checkout-service', label: 'ERROR lines', what: 'x', status: 'open', firstSeen: iso(40), lastSeen: iso(1), src: 'log', sig: 'err'}],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/' + (hash || '#t=inv'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path) => {
        const p = String(path);
        calls.push(p.split('?')[0]);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/investigator/status')) return serve.board ? ok(serve.board) : {ok: false, status: 503, json: async () => ({})};
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: null, operator: false});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/nodes')) return ok({nodes: []});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors, calls};
}

async function thePageShowsRoomsTheQueueLimitsAndRecentRuns() {
  //given
  const {w, doc, errors, calls} = await boot({board: board()});

  //then
  assert.ok(!doc.getElementById('panel-inv').hidden && calls.includes('/api/investigator/status'));
  const rooms = [...doc.querySelectorAll('#inv .inv-room')];
  assert.deepStrictEqual(rooms.map((r) => r.querySelector('b').textContent + ':' + r.querySelector('.tag').textContent), ['Room 1:Running', 'Room 2:Free', 'Cards:Free']);
  const busy = rooms[0].textContent;
  assert.ok(busy.includes('Why did it go up?') && busy.includes('Slack question') && busy.includes('asked by adam.baker') && /4 tool calls, last esql \d+ s ago/.test(busy) && busy.includes('sonnet-5-5 \u00b7 high') && busy.includes('skopax-abc'));
  assert.ok(/running for 2 min \d+ s/.test(busy), busy);
  assert.strictEqual(rooms[0].querySelector('.inv-links a').href, 'https://example.slack.com/archives/C1/p1');
  assert.ok(rooms[2].textContent.includes('Takes new and escalated cards'));
  const waits = [...doc.querySelectorAll('#inv .inv-wait')].map((x) => x.textContent);
  assert.ok(waits.length === 2 && waits[0].includes('And when did it start?') && waits[0].includes('nina.white') && waits[0].includes('behind an earlier question in the same Slack thread'));
  assert.ok(waits[1].startsWith('2Investigate checkout-service / ERROR lines') && waits[1].includes('Investigate click') && waits[1].includes('Skopax page') && waits[1].includes('next for a free room'));
  const lims = [...doc.querySelectorAll('#inv .inv-lim')];
  assert.deepStrictEqual(lims.map((l) => l.textContent), ['Slack questions this hour10 of 10', 'Page questions this hour1 of 6', 'Topic checks this hour12 of 60', 'Spend today ($)4.10 of 25.00']);
  assert.ok(lims[0].classList.contains('full') && !lims[1].classList.contains('full') && !lims[3].classList.contains('full'), 'only a limit that is used up is marked');
  assert.ok(doc.getElementById('inv').textContent.includes('Asked in Slack this hour: adam.baker 4/4 \u00b7 nina.white 1/4'));
  const recent = [...doc.querySelectorAll('#inv .inv-recent tbody tr')].map((r) => [...r.children].map((c) => c.textContent));
  assert.deepStrictEqual(recent.map((r) => r.slice(1)), [['New cards', 'checkout-service / ERROR lines', 'done', '7', '$0.42'], ['Slack question', 'can you see the ios repo?', 'failed', '1', '$0.05'], ['Chat message', 'private chat message', 'done', '2', '$0.12']], 'a chat run says it is private instead of a dash');
  const tab = doc.getElementById('tab-inv');
  assert.ok(doc.getElementById('count-inv').textContent === '1+2' && tab.classList.contains('hot') && tab.title === '1 running, 2 waiting');
  assert.strictEqual(doc.querySelector('#inv .inv-banner'), null);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the Investigator page shows each room with what it runs, the queue in order with why each waits, the limits with a used-up one marked, and the recent runs; the tab counts running+waiting');
}

async function aStoppedInvestigatorAndAnIdleOneAndAFailedLoadSaySo() {
  //given
  const stopped = await boot({board: board({halted: 'stopped', rooms: board().rooms.map((r) => Object.assign({}, r, {run: null})),
    waiting: [{id: 'a2', type: 'ask', source: 'slack', by: 'x@example.com', at: iso(1), text: 'q', cards: [], thread: null, why: 'the investigator is stopped'}]})}, '#t=anoms');
  const idle = await boot({board: board({rooms: board().rooms.map((r) => Object.assign({}, r, {run: null})), waiting: [], recent: []})});
  const down = await boot({});

  //then
  assert.ok(stopped.doc.querySelector('#inv .inv-banner').textContent.startsWith('The investigator is stopped'));
  const tab = stopped.doc.getElementById('tab-inv');
  assert.ok(tab.classList.contains('k-warn') && tab.title === 'the investigator is stopped' && stopped.doc.getElementById('count-inv').textContent === '0+1', 'the tab shows it on any page');
  assert.ok(idle.doc.getElementById('inv').textContent.includes('Nothing is waiting.') && idle.doc.getElementById('inv').textContent.includes('No runs yet.'));
  assert.ok(idle.doc.getElementById('count-inv').textContent === '' && !idle.doc.getElementById('tab-inv').classList.contains('hot'));
  assert.strictEqual(down.doc.getElementById('inv').textContent, 'The investigator status is unavailable right now.');
  assert.deepStrictEqual(stopped.errors.concat(idle.errors, down.errors), []);
  [stopped, idle, down].forEach((x) => x.w.close());
  console.log('PASS a stopped investigator shows a banner and an amber tab on any page, an idle one says nothing is waiting, and a failed load says the status is unavailable');
}

async function aSwitchedOffInvestigatorSaysWhereToTurnItOn() {
  //given
  const off = await boot({board: board({halted: 'off', rooms: board().rooms.map((r) => Object.assign({}, r, {run: null})),
    waiting: [{id: 'a2', type: 'dive', source: 'page', by: 'x@example.com', at: iso(1), text: 'Investigate', cards: [KEY], thread: null, why: 'AI investigator is turned off in Admin \u203a Features.'}]})}, '#t=anoms');

  //then
  const banner = off.doc.querySelector('#inv .inv-banner');
  assert.strictEqual(banner.textContent, 'The AI investigator is turned off in Admin \u203a Features. Nothing new starts until it is turned back on; queued work waits.');
  assert.ok(banner.classList.contains('k-warn'));
  const tab = off.doc.getElementById('tab-inv');
  assert.ok(tab.classList.contains('k-warn') && tab.title === 'the AI investigator is turned off', 'the tab shows it on any page');
  assert.deepStrictEqual(off.errors, []);
  off.w.close();
  console.log('PASS a switched-off investigator says so in its banner and on its tab, naming Admin \u203a Features');
}

async function thePolledRowStaysASidebarTabThatOpeningThePageMarksCurrent() {
  //given
  const {w, doc, errors} = await boot({board: board()}, '#t=anoms');
  const tab = doc.getElementById('tab-inv');
  const polled = [tab.classList.contains('tab'), tab.classList.contains('hot'), tab.hasAttribute('aria-current')];

  //when
  w.location.hash = '#t=inv';
  await wait(200);

  //then
  assert.deepStrictEqual(polled, [true, true, false], 'after a poll the row keeps its tab class beside its state, and is not current while another page is open');
  assert.ok(tab.classList.contains('tab') && tab.getAttribute('aria-current') === 'page', 'opening the Investigator page marks its polled row current');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the polled Investigator row keeps its tab class, so the sidebar marks it current when its page opens');
}

async function aSentRunLinksItsLangfuseTraceAndOthersDoNot() {
  //given
  const traced = board().recent.map((r, i) => Object.assign({}, r, i === 0 ? {traceUrl: 'https://langfuse.example.com/project/p1/traces/abc'} : i === 1 ? {traceUrl: 'javascript:alert(1)'} : {}));
  const {w, doc, errors} = await boot({board: board({recent: traced})});

  //then
  const rows = [...doc.querySelectorAll('#inv .inv-recent tbody tr')];
  const links = rows.map((r) => r.querySelector('a.inv-trace'));
  assert.ok(links[0] && links[0].textContent === 'Open trace \u2197' && links[0].href === 'https://langfuse.example.com/project/p1/traces/abc');
  assert.ok(links[0].target === '_blank' && links[0].rel === 'noopener');
  assert.deepStrictEqual(links.slice(1), [null, null], 'a run that was not sent, or a link that is not http, shows no trace link');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a recent run that was sent to Langfuse shows Open trace in a new tab, and other runs show no link');
}

(async () => {
  await thePageShowsRoomsTheQueueLimitsAndRecentRuns();
  await aStoppedInvestigatorAndAnIdleOneAndAFailedLoadSaySo();
  await aSwitchedOffInvestigatorSaysWhereToTurnItOn();
  await thePolledRowStaysASidebarTabThatOpeningThePageMarksCurrent();
  await aSentRunLinksItsLangfuseTraceAndOthersDoNot();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
