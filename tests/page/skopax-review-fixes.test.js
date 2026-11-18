const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const ADMIN = 'nina.white@example.com';
const KEY = 'log|checkout-service|ERROR|boom';

function card(key, extra) {
  return Object.assign({key, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'ERROR lines', src: 'logs', sig: 'log', now: 3, firstSeen: iso(30), lastSeen: iso(1), ns: 'commerce', what: 'NullPointerException in OrderService'}, extra || {});
}

function dep(id, svc, min) {
  return {id, ns: 'commerce', svc, fromImage: 'aaaaaaaa', toImage: 'bbbbbbbb', at: iso(min), createdAt: iso(min)};
}

async function boot(opts) {
  const o = opts || {};
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: o.anomalies || [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state));
  const items = [
    {key: 'investigate.mode', group: 'Investigator', label: 'Mode', help: 'off, shadow or live', kind: 'choice', live: true, value: 'shadow', base: 'shadow', default: 'shadow', source: 'default', by: null, at: null, choices: ['off', 'shadow', 'live']},
    {key: 'investigate.daily-usd', group: 'Investigator', label: 'Daily limit ($)', help: 'Daily spend cap', kind: 'float', live: true, value: '40', base: '25', default: '25', source: 'admin', by: ADMIN, at: iso(30), low: 0, high: 500},
  ];
  const routes = [{name: 'seo', label: 'SEO', match: ['tag:seo'], minSev: 'info', business: true, webhook: 'prod-00.westeurope.logic.azure.com', source: 'admin', inConfig: false, by: ADMIN, at: iso(60), removed: false}];
  const hist = [{id: 'h1', seq: 1, key: 'investigate.daily-usd', old: null, new: '40', by: ADMIN, at: iso(30)}];
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/' + (o.hash || '#t=anoms'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      if (o.lang) w.document.cookie = 'skopax_lang=' + o.lang + '; path=/';
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, req) => {
        const p = String(path), method = (req && req.method) || 'GET';
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        calls.push([method, p, req && req.body ? JSON.parse(req.body) : null]);
        if (p.startsWith('/api/admin/teams/reveal')) return ok({webhook: 'https://prod-00.westeurope.logic.azure.com/workflows/seo?sig=SEOSECRET'});
        if (p.startsWith('/api/admin/teams')) return ok({routes, tags: [{id: 'seo', label: 'SEO'}], hosts: ['.logic.azure.com']});
        if (p.startsWith('/api/admin/secrets')) return ok({fields: [], canEncrypt: true});
        if (p.startsWith('/api/admin/history')) return ok(hist);
        if (p.startsWith('/api/admin')) return method === 'GET' ? ok({items, rev: 3, refreshSeconds: 15}) : ok({item: items[0], change: {}});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: ADMIN, operator: true, admin: true, teams: [], tags: []});
        if (p.startsWith('/api/deploys')) return ok(o.deploys ? o.deploys() : []);
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

async function linksKeepTheirBalancedParentheses() {
  //given
  const {w, errors} = await boot();

  //when
  const kibana = w.linkify('Kibana: https://k.example/app/discover#/?_a=(query:(language:kuery,query:boom)).');
  const wrapped = w.linkify('(see https://shop.example.com/products)');
  const prom = w.linkify('https://p.example/graph?g0.expr=sum(rate(x[5m]))');

  //then
  assert.strictEqual(kibana, 'Kibana: <a href="https://k.example/app/discover#/?_a=(query:(language:kuery,query:boom))" target="_blank" rel="noopener">https://k.example/app/discover#/?_a=(query:(language:kuery,query:boom))</a>.');
  assert.strictEqual(wrapped, '(see <a href="https://shop.example.com/products" target="_blank" rel="noopener">https://shop.example.com/products</a>)');
  assert.ok(prom.startsWith('<a href="https://p.example/graph?g0.expr=sum(rate(x[5m]))"'), prom);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a link keeps closing parentheses it opened (Kibana, Prometheus) and drops only an unmatched one';
}

async function anUnconfirmedStopDisarmsAndAConfirmSendsWhatTheButtonSaid() {
  //given
  const {w, doc, errors} = await boot();
  const sent = [];
  w.__skopaxApi = (method, path, body) => { sent.push([method, path, body]); return Promise.resolve({mode: 'live', stopped: body.stopped ? {by: ADMIN, at: iso(0)} : null}); };
  w.eval("INV_INFO = {mode: 'live', stopped: null}");
  const b = doc.createElement('button');
  doc.body.appendChild(b);

  //when
  w.podToggle(b);
  const armed = [b.dataset.confirm, b.textContent];
  await wait(5200);
  const disarmed = [b.dataset.confirm, b.textContent, !!doc.querySelector('[data-confirm]')];
  w.podToggle(b);
  w.eval("INV_INFO = {mode: 'live', stopped: {by: 'paul.stone@example.com', at: '" + iso(0) + "'}}");
  w.podToggle(b);
  await wait(30);

  //then
  assert.deepStrictEqual(armed, ['stop', 'Click again to confirm']);
  assert.deepStrictEqual(disarmed, [undefined, 'Stop investigator', false], 'after 5 s the button is back and nothing holds the live updates');
  assert.deepStrictEqual(JSON.parse(JSON.stringify(sent)), [['PUT', '/api/investigate', {stopped: true}]], 'the confirm sends the stop it showed, even though someone stopped it meanwhile');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'an unconfirmed Stop investigator disarms after 5 s, and a confirm sends what the armed button said';
}

async function anInvestigatorRepaintKeepsTheTextTypedInACardForm() {
  //given
  const {w, doc, errors} = await boot({anomalies: [card(KEY, {num: 7})]});
  const cardEl = () => doc.querySelector('#anoms article.inc[data-key="' + KEY + '"]');
  const form = cardEl().querySelector('.reason');
  const input = form.querySelector('input, textarea');
  const prefix = input.id.split('-')[0];
  form.hidden = false;
  input.value = 'typed before the update';
  const other = cardEl().querySelector('button');
  other.dataset.confirm = '1';

  //when
  w.repaintCards();
  const held = cardEl().querySelector('.reason') === form;
  delete other.dataset.confirm;
  await wait(1100);
  const after = cardEl().querySelector('.reason [id^="' + prefix + '-"]');

  //then
  assert.ok(held, 'an armed confirm on a card holds the repaint');
  assert.ok(after && after !== input, 'the cards were rebuilt once the confirm ended');
  assert.ok(!after.closest('.reason').hidden && after.value === 'typed before the update', 'the open form and its text came back');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'an investigator repaint waits for an armed confirm and keeps an open card form with its text';
}

async function theNewestReleaseStaysOpenAcrossARefresh() {
  //given
  let deploys = [dep('d3', 'checkout-service', 30), dep('d2', 'orders-service', 40), dep('d1', 'shop-web', 3 * 24 * 60)];
  const {w, doc, errors} = await boot({hash: '#t=rels', deploys: () => deploys});
  const newest = () => doc.querySelector('#panel-rels details.rel');
  const id = newest().id;

  //when
  newest().open = true;
  newest().dispatchEvent(new w.Event('toggle'));
  deploys = deploys.concat([dep('d0', 'batch-service', 30 * 24 * 60)]);
  await w.reports.refresh();
  await wait(50);

  //then
  assert.strictEqual(newest().id, id, 'the same release is on top');
  assert.ok(newest().open, 'the release the reader opened stays open after the list is rebuilt');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the newest release keeps the open state the reader gave it when a refresh rebuilds the list';
}

(async () => {
  for (const t of [linksKeepTheirBalancedParentheses,
    anUnconfirmedStopDisarmsAndAConfirmSendsWhatTheButtonSaid, anInvestigatorRepaintKeepsTheTextTypedInACardForm, theNewestReleaseStaysOpenAcrossARefresh]) {
    console.log('PASS ' + await t());
  }
})().catch((e) => { console.error('FAIL', e); process.exit(1); });
