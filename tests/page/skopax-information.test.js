const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const CARD = 'log|checkout-service|ERROR|boom';
const MOVED = 'lat|shop/cart';

function state(information) {
  const card = {key: CARD, sev: 'warn', status: 'open', svc: 'checkout-service', label: 'ERROR lines', src: 'logs', sig: 'log', ns: 'shop', now: 1, firstSeen: iso(30), lastSeen: iso(1), num: 3};
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)}, anomalies: [card], information: information,
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

const ROWS = [
  {key: 'log|shop|INFO|new', svc: 'shop', label: 'INFO lines', src: 'logs', text: 'INFO lines went up', firstSeen: iso(20), lastSeen: iso(1), firing: true},
  {key: MOVED, svc: 'cart', label: 'Latency', src: 'prom', text: 'cart latency is up', firstSeen: iso(90), lastSeen: iso(1), firing: true, moved: {by: 'alice@example.com', at: iso(10)}},
  {key: 'log|pay|INFO|ended', svc: 'pay', label: 'INFO lines', src: 'logs', text: 'gone quiet', firstSeen: iso(300), lastSeen: iso(60), firing: false, endedAt: iso(60)},
];

async function boot(information, operator) {
  const errors = [], posts = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state(information)));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=info', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, opt) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (opt && opt.method === 'POST' && p === '/api/actions') { posts.push(JSON.parse(opt.body)); return ok({id: 'a1', status: 'done'}); }
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'nina.white@example.com', operator: operator, teams: [], tags: []});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors, posts};
}

const rows = (doc) => [...doc.querySelectorAll('#info-list li.info-row')];

async function informationPageListsItemsReadOnlyWithoutNumbersAndCountsTheFiringOnes() {
  //given
  const {w, doc, errors} = await boot(ROWS, true);

  //when
  const listed = rows(doc).map((li) => li.querySelector('.svc').textContent);
  const tags = rows(doc).map((li) => li.querySelector('.tag').textContent);

  //then
  assert.strictEqual(doc.getElementById('panel-info').hidden, false, '#t=info opens the Information page');
  assert.deepStrictEqual(listed, ['shop', 'cart', 'pay'], 'in the order the server gives: still firing first, newest first');
  assert.deepStrictEqual(tags.map((t) => t.split(' ')[0]), ['Still', 'Still', 'Ended']);
  assert.strictEqual(doc.getElementById('count-info').textContent, '2', 'the sidebar counts the items still firing');
  const row = doc.getElementById('tab-info');
  assert.ok(row.closest('[data-grp="observe"]'), 'the row sits in Observe');
  assert.strictEqual(row.previousElementSibling.id, 'tab-svcs', 'right after Services');
  assert.ok(!/#\d/.test(doc.getElementById('info-list').textContent), 'information carries no numbers');
  assert.match(rows(doc)[0].querySelector('.info-meta').textContent, /^logs \u00b7 First seen .+ \u00b7 Last seen /);
  assert.strictEqual(rows(doc)[0].querySelector('.info-text').textContent, 'INFO lines went up');
  assert.strictEqual(doc.querySelectorAll('#info-list button').length, 1, 'only the item moved by hand has a button');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the Information page lists the items read-only, without numbers, and the sidebar counts the firing ones';
}

async function aHandMoveShowsWhoMovedItAndBackToTheBoardClearsTheHandSeverity() {
  //given
  const {w, doc, errors, posts} = await boot(ROWS, true);
  const moved = rows(doc)[1];

  //when
  const said = moved.querySelector('.info-moved').textContent;
  moved.querySelector('button.info-back').click();
  await wait(50);

  //then
  assert.match(said, /^Moved to Information by alice .+ Back to the board$/);
  assert.deepStrictEqual(posts.map((p) => [p.type, p.key, p.sev]), [['severity', MOVED, 'auto']]);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a hand move names who moved it and when, and Back to the board sets the severity back to the signal';
}

async function aViewerSeesTheButtonLockedAndItSendsNothing() {
  //given
  const {w, doc, errors, posts} = await boot(ROWS, false);
  const b = rows(doc)[1].querySelector('button.info-back');

  //when
  b.click();
  await wait(50);

  //then
  assert.strictEqual(b.getAttribute('aria-disabled'), 'true');
  assert.strictEqual(b.title, 'Needs the operator role: ask an admin');
  assert.deepStrictEqual(posts, []);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a viewer sees Back to the board locked and nothing is sent';
}

async function theBoardHasNoInfoFilterAndAnEmptyPageSaysSo() {
  //given
  const {w, doc, errors} = await boot([], true);

  //when
  const chips = [...doc.querySelectorAll('#tab-sub-anoms button')].map((b) => b.dataset.f);

  //then
  assert.ok(chips.indexOf('warn') >= 0 && chips.indexOf('info') < 0, 'the board has no Info filter');
  assert.match(doc.querySelector('#info-list .info-empty').textContent, /^No information right now\./);
  assert.strictEqual(doc.getElementById('count-info').textContent, '0');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the board has no Info filter and an empty Information page says so';
}

(async () => {
  for (const t of [informationPageListsItemsReadOnlyWithoutNumbersAndCountsTheFiringOnes, aHandMoveShowsWhoMovedItAndBackToTheBoardClearsTheHandSeverity,
    aViewerSeesTheButtonLockedAndItSendsNothing, theBoardHasNoInfoFilterAndAnEmptyPageSaysSo]) console.log('PASS ' + await t());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
