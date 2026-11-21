const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'ok', headline: 'Quiet', points: [], at: iso(1)}, anomalies: [], services: [], history: [], logTop: [], routes: [],
    cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

function requests() {
  return [
    {id: 'q1', source: 'slack', text: 'why is checkout-service slow?', status: 'processing', requestedAt: iso(2), requestedBy: 'evan.cross@example.com',
     slack: {channel: 'C2', channelName: 'dev-backend', outside: true, permalink: 'https://example.slack.com/archives/C2/p1'}, cards: [], run: null},
    {id: 'q2', source: 'slack', text: 'what was the 5xx yesterday?', status: 'done', requestedAt: iso(90), requestedBy: 'adam.baker@example.com',
     slack: {channel: 'C1', permalink: 'https://example.slack.com/archives/C1/p2'}, cards: ['5xx|web/web-bff'],
     run: {id: 'r2', outcome: 'done', usd: 0.42, toolCalls: 3, recap: 'Yesterday 14:00 UTC **web-bff** gave 5xx:\n- the orders-db pool was full'}},
    {id: 'q3', source: 'page', text: 'why did kafka lag go up?', status: 'done', requestedAt: iso(300), requestedBy: 'sara.bell@example.com', cards: [],
     run: {id: 'r3', outcome: 'failed', usd: 0.1, toolCalls: 1, recap: ''}, result: 'run failed'},
    {id: 'q4', source: 'slack', text: 'read it with the admin tool and look again', status: 'done', requestedAt: iso(400), requestedBy: 'adam.baker@example.com',
     slack: {channel: 'C1', threadTs: 't9', permalink: 'https://example.slack.com/archives/C1/p4'}, cards: [], run: {id: 'r4', outcome: 'done', usd: 0.2, toolCalls: 2, recap: 'Second answer.'}},
    {id: 'q5', source: 'slack', text: 'what is the problem here', status: 'done', requestedAt: iso(420), requestedBy: 'evan.cross@example.com',
     slack: {channel: 'C1', threadTs: 't9', permalink: 'https://example.slack.com/archives/C1/p5'}, cards: [], run: {id: 'r5', outcome: 'done', usd: 0.3, toolCalls: 4, recap: 'First answer.'}},
  ];
}

async function boot(serve) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const calls = [];
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state()));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/' + (serve.hash || ''), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path) => {
        const p = String(path);
        calls.push(p.split('?')[0]);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok(Object.assign({loginEnabled: true, user: null, operator: false}, serve.me || {slackChannel: '#alerts'}));
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/nodes')) return ok({nodes: []});
        if (p.startsWith('/api/requests')) return serve.requests ? ok(serve.requests) : {ok: false, status: 500, json: async () => ({})};
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(300);
  return {w: dom.window, doc: dom.window.document, errors, calls};
}

async function requestsSitApartFromAnomaliesWithWhereWhoAndTheAnswer() {
  //given
  const {w, doc, errors, calls} = await boot({requests: requests(), hash: '#t=reqs'});

  //then
  const tab = doc.getElementById('tab-reqs');
  assert.ok(tab && tab.textContent.startsWith('Requests'), 'a Requests tab sits next to Anomalies');
  assert.ok(!doc.getElementById('panel-reqs').hidden, 'the link opens it');
  assert.strictEqual(doc.getElementById('count-reqs').textContent, '1', 'one request is running');
  assert.ok(tab.className.includes('k-act'));
  assert.ok(calls.includes('/api/requests'));
  const rows = [...doc.querySelectorAll('#reqs details.req')];
  assert.deepStrictEqual(rows.map((r) => r.querySelector('.tag').textContent), ['Running', 'Answered', 'Not answered \u00b7 failed', 'Answered']);
  assert.deepStrictEqual(rows.map((r) => r.querySelector('.req-q').textContent), ['why is checkout-service slow?', 'what was the 5xx yesterday?', 'why did kafka lag go up?', 'what is the problem here']);
  assert.ok(/^evan\.cross \u00b7 #dev-backend \u00b7 /.test(rows[0].querySelector('.req-meta').textContent), rows[0].querySelector('.req-meta').textContent);
  assert.ok(/^adam\.baker \u00b7 #alerts \u00b7 .* \u00b7 \$0\.42 \u00b7 3 tool calls$/.test(rows[1].querySelector('.req-meta').textContent), rows[1].querySelector('.req-meta').textContent);
  assert.ok(/^sara\.bell \u00b7 Skopax page \u00b7 /.test(rows[2].querySelector('.req-meta').textContent));
  assert.ok(rows[0].open && !rows[1].open, 'a running one is open, an answered one folded');
  assert.ok(/investigator is on it/.test(rows[0].querySelector('.req-ans').textContent));
  assert.strictEqual(rows[1].querySelector('.req-ans strong').textContent, 'web-bff');
  assert.strictEqual(rows[1].querySelector('.req-ans li').textContent, 'the orders-db pool was full');
  assert.deepStrictEqual([...rows[1].querySelectorAll('.req-links a')].map((a) => a.getAttribute('href')), ['https://example.slack.com/archives/C2/p2'.replace('C2', 'C1'), '#a=5xx%7Cweb%2Fweb-bff']);
  assert.strictEqual(rows[2].querySelector('.req-ans').textContent.trim(), 'run failed');
  assert.ok(doc.getElementById('req-ask'), 'the tab has its own Ask the investigator');
  assert.strictEqual(rows.length, 4, 'two asks in one thread fold into one entry');
  const thread = rows[3];
  assert.ok(thread.classList.contains('req-thread') && !thread.open);
  assert.strictEqual(thread.querySelector('.req-q').textContent, 'what is the problem here', 'the thread shows its first question');
  assert.strictEqual(thread.querySelector('.req-n').textContent, '2 in thread');
  assert.ok(/^evan\.cross, adam\.baker \u00b7 #alerts \u00b7 /.test(thread.querySelector('.req-meta').textContent), thread.querySelector('.req-meta').textContent);
  assert.deepStrictEqual([...thread.querySelectorAll('.req-item .req-iq strong')].map((x) => x.textContent), ['what is the problem here', 'read it with the admin tool and look again'], 'oldest first inside');
  assert.deepStrictEqual([...thread.querySelectorAll('.req-item .req-ans')].map((x) => x.textContent.trim()), ['First answer.', 'Second answer.']);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS requests sit apart from anomalies with where they came from, who asked, the state and the answer');
}

async function aMainChannelAskShowsTheChannelNameOnlyWhenItIsOneAndNeverTheDisplayName() {
  //given
  const named = await boot({requests: requests(), hash: '#t=reqs', me: {slackChannel: '#alerts'}});
  const byId = await boot({requests: requests(), hash: '#t=reqs', me: {slackChannel: 'C123'}});
  const empty = await boot({requests: requests(), hash: '#t=reqs', me: {slackChannel: ''}});

  //when
  const meta = (page) => [...page.doc.querySelectorAll('#reqs details.req .req-meta')].map((m) => m.textContent).find((t) => t.startsWith('adam.baker'));

  //then
  assert.ok(/ \u00b7 #alerts \u00b7 /.test(meta(named)), meta(named));
  for (const page of [byId, empty]) {
    assert.ok(/ \u00b7 main channel \u00b7 /.test(meta(page)), meta(page));
    assert.ok(!/#skopax|C123/.test(meta(page)), meta(page));
  }
  for (const page of [named, byId, empty]) { assert.deepStrictEqual(page.errors, []); page.w.close(); }
  console.log('PASS a main channel ask shows #name from the setting or neutral text, never the display name');
}

async function aFoldStaysAsTheReaderLeftItAndAFailedLoadSaysSo() {
  //given
  const {w, doc, errors} = await boot({requests: requests(), hash: '#t=reqs'});
  const first = () => doc.querySelector('#reqs details.req[data-id="q1"]');

  //when
  first().open = false;
  first().dispatchEvent(new w.Event('toggle'));
  w.loadRequests();
  await wait(50);

  //then
  assert.ok(!first().open, 'a refresh keeps the fold the reader chose');
  assert.deepStrictEqual(errors, []);
  w.close();

  //when
  const broken = await boot({requests: null, hash: '#t=reqs'});

  //then
  assert.strictEqual(broken.doc.getElementById('reqs').textContent.trim(), 'Requests are unavailable right now.');
  broken.w.close();

  //when
  const empty = await boot({requests: [], hash: '#t=reqs'});

  //then
  assert.ok(/No requests yet\. Tag @Skopax/.test(empty.doc.getElementById('reqs').textContent));
  assert.strictEqual(empty.doc.getElementById('tab-reqs').className, 'tab');
  empty.w.close();
  console.log('PASS a fold stays as the reader left it, a failed load says so and an empty list says how to ask');
}

async function questionsAboutOneCardAreGroupedUnderThatCardWhicheverThreadTheyCameFrom() {
  //given
  const card = 'log|web-bff|WARN|Rest error occurred';
  const rows = [
    {id: 'c3', source: 'page', text: 'who is on it?', status: 'processing', requestedAt: iso(2), requestedBy: 'zoe.hart@example.com', cards: [card], run: null},
    {id: 'c2', source: 'slack', text: 'is it fixed', status: 'done', requestedAt: iso(40), requestedBy: 'paul.stone@example.com',
     slack: {channel: 'C1', threadTs: 't2', permalink: 'https://example.slack.com/archives/C1/p2'}, cards: [card], run: {id: 'r2', outcome: 'done', usd: 0.27, toolCalls: 4, recap: 'Not fixed.'}},
    {id: 'n1', source: 'page', text: 'why did kafka lag go up?', status: 'done', requestedAt: iso(60), requestedBy: 'nina.white@example.com', cards: [], run: {id: 'r0', outcome: 'done', usd: 0.1, toolCalls: 1, recap: 'It went up.'}},
    {id: 'c1', source: 'slack', text: 'can you give me a link', status: 'done', requestedAt: iso(90), requestedBy: 'nina.white@example.com',
     slack: {channel: 'C2', channelName: 'dev-backend', outside: true, threadTs: 't1', permalink: 'https://example.slack.com/archives/C2/p1'}, cards: [card], run: {id: 'r1', outcome: 'done', usd: 0.5, toolCalls: 3, recap: 'Here is the link.'}}];
  const {w, doc, errors} = await boot({requests: rows, hash: '#t=reqs'});
  const boxes = [...doc.querySelectorAll('#reqs > details.req')];

  //then
  assert.strictEqual(boxes.length, 2, 'three questions about one card are one group; the question without a card stands alone');
  const group = boxes[0];
  assert.strictEqual(group.querySelector('.req-q').textContent, 'web-bff \u00b7 WARN \u00b7 Rest error occurred', 'the card is the heading');
  assert.strictEqual(group.querySelector('.req-n').textContent, '3 about this card');
  assert.ok(group.hasAttribute('open'), 'a group with a running question starts open');
  assert.deepStrictEqual([...group.querySelectorAll('.req-item .req-iq strong')].map((x) => x.textContent), ['can you give me a link', 'is it fixed', 'who is on it?'], 'oldest first, across threads and the page');
  assert.ok(/nina\.white, paul\.stone, zoe\.hart \u00b7 #dev-backend, #alerts, Skopax page \u00b7 /.test(group.querySelector('.req-meta').textContent), group.querySelector('.req-meta').textContent);
  assert.strictEqual(group.querySelector('.req-card a').getAttribute('href'), '#a=' + encodeURIComponent(card));
  assert.strictEqual(boxes[1].querySelector('.req-q').textContent, 'why did kafka lag go up?');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS questions about one card are grouped under that card, oldest first, whichever Slack thread or page they came from; a question without a card stands alone');
}

(async () => {
  await requestsSitApartFromAnomaliesWithWhereWhoAndTheAnswer();
  await questionsAboutOneCardAreGroupedUnderThatCardWhicheverThreadTheyCameFrom();
  await aMainChannelAskShowsTheChannelNameOnlyWhenItIsOneAndNeverTheDisplayName();
  await aFoldStaysAsTheReaderLeftItAndAFailedLoadSaysSo();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
