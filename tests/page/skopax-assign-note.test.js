const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const KEY = 'log|checkout-service|ERROR|boom';
const SLACK = 'https://example.slack.com/archives/C1/p11';
const POSTED = iso(20);
const ME = 'nina.white@example.com';

function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [{key: KEY, sev: 'warn', status: 'open', svc: 'checkout-service', label: 'ERROR lines', src: 'logs', sig: 'log', now: 1, firstSeen: iso(30), lastSeen: iso(1),
      slackPost: {channel: 'C1', ts: '1.1', at: POSTED, by: ME, permalink: SLACK}}],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot() {
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state()));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=anoms', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, opts) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (opts && opts.method === 'PUT' && p === '/api/assignee') {
          const body = JSON.parse(opts.body);
          calls.push(body);
          return ok({rev: 2, owner: body.name, assigned: {by: ME, at: iso(0)}, dm: 'none', thread: false});
        }
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: ME, operator: true, teams: [], tags: []});
        if (p.startsWith('/api/actions')) return ok([{id: 'a1', type: 'slack', key: KEY, target: 'slack:' + KEY, status: 'done', requestedBy: ME, requestedAt: POSTED, doneAt: POSTED, slack: {channel: 'C1', ts: '1.1', permalink: SLACK}}]);
        if (p.startsWith('/api/deploy-reports')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors, calls};
}

async function theAssignNoteStaysAfterARepaintAndTheNotifyRowKeepsTheSlackLine() {
  //given
  const {w, doc, errors, calls} = await boot();
  const card = () => doc.querySelector('#anoms article.inc[data-key="' + KEY + '"]');
  const notify = () => card().querySelector('.notif .slack-st');
  const before = notify().textContent;

  //when
  card().querySelector('.asg-btn').click();
  const inp = card().querySelector('.asg-form input');
  inp.value = 'Zoe Lane';
  card().querySelector('.asg-save').click();
  await wait(30);
  const said = card().querySelector('.asg-msg').textContent;
  w.__skopaxRender();
  await w.__skopaxActions();

  //then
  assert.deepStrictEqual(calls, [{key: KEY, name: 'Zoe Lane', email: null}]);
  assert.strictEqual(said, 'Assigned to Zoe Lane \u00b7 no Slack account found for them, not messaged');
  assert.strictEqual(card().querySelector('.asg-msg').textContent, said, 'the note on the Assign row survives the next render');
  assert.ok(!card().querySelector('.asg-msg').classList.contains('slack-st'), 'the note is not a Slack status line');
  assert.strictEqual(before, 'Sent to Slack ' + w.dayhm(POSTED) + ' \u2197 \u00b7 nina.white');
  assert.strictEqual(notify().textContent, before, 'the Slack line stays in the Notify row');
  assert.strictEqual(notify().querySelector('a').getAttribute('href'), SLACK);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the Assign note (no Slack account found) stays on the action row after the next render, and the Slack status goes to the Notify row');
}

(async () => {
  await theAssignNoteStaysAfterARepaintAndTheNotifyRowKeepsTheSlackLine();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
