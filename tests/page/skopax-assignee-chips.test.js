const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const A1 = 'log|checkout-service|ERROR|boom', A2 = 'log|payments-api|ERROR|npe', M1 = 'log|inventory-service|ERROR|x', FREE = 'log|shop-web|ERROR|other';

function state() {
  const card = (key, extra) => Object.assign({key, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'x', src: 'logs', sig: 'log', now: 1, firstSeen: iso(30), lastSeen: iso(1)}, extra || {});
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(A1, {owner: 'Nina White', tags: [{id: 'developer', by: 'pod', at: iso(5)}]}), card(A2, {owner: 'nina.white@example.com'}), card(M1, {owner: 'Paul Stone', tags: [{id: 'data', by: 'rule', at: iso(5)}]}), card(FREE)],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(hash, stored) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state()));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/' + (hash || '#t=anoms'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      if (stored) Object.keys(stored).forEach((k) => w.localStorage.setItem(k, stored[k]));
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'nina.white@example.com', operator: true, teams: [], tags: [{id: 'developer', label: 'Developer'}, {id: 'data', label: 'Data'}, {id: 'infra', label: 'Infrastructure'}, {id: 'analyst', label: 'Analyst'}]});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors};
}

const shown = (doc) => [...doc.querySelectorAll('#anoms article.inc')].map((c) => c.dataset.key).sort();
const name = (b) => b.querySelector('.lbl').textContent;
const items = (doc) => ['who:any', 'who:me', 'who:none'].map((t) => doc.querySelector('#panel-anoms [data-type="' + t + '"]')).concat([...doc.querySelectorAll('#fb-m-who .fb-i[data-type^="who:u:"]')]).map((b) => name(b) + ' ' + b.querySelector('.count').textContent);

async function everyAssigneeIsAFilterUnderAssignee() {
  //given
  const {w, doc, errors} = await boot('#t=anoms');

  //when
  const listed = items(doc);
  doc.querySelector('#bf-lists .fb-i[data-type="who:u:paul.stone"]').click();
  const paul = {cards: shown(doc), hash: w.location.hash, pressed: doc.querySelector('#bf-lists .fb-i[data-type="who:u:paul.stone"]').getAttribute('aria-checked')};
  doc.querySelector('#bf-lists .fb-i[data-type="who:u:nina.white"]').click();
  const nina = shown(doc);
  doc.querySelector('#bf-lists .fb-i[data-type="who:u:nina.white"]').click();
  const paulAgain = shown(doc);
  doc.querySelector('#bf-lists .fb-i[data-type="who:u:paul.stone"]').click();
  const back = shown(doc);
  const reload = await boot('#t=anoms/open/who%3Au%3Apaul.stone');

  //then
  assert.deepStrictEqual(listed, ['Assigned 3', 'Mine 2', 'Unassigned 1', 'Nina White 2', 'Paul Stone 1'], 'after Assigned / Mine / Unassigned each person with cards, most first; a name and an email of one person count once');
  assert.deepStrictEqual(paul, {cards: [M1], hash: '#t=anoms/open/who%3Au%3Apaul.stone', pressed: 'true'});
  assert.deepStrictEqual(nina, [A2, A1, M1].sort(), 'a second person adds their cards, both spellings of one person are theirs');
  assert.deepStrictEqual(paulAgain, [M1], 'a second click drops that person');
  assert.deepStrictEqual(back, [A1, A2, FREE, M1].sort(), 'dropping the last person shows everyone again');
  assert.deepStrictEqual(shown(reload.doc), [M1], 'a copied link keeps the person');
  assert.deepStrictEqual(errors.concat(reload.errors), []);
  w.close(); reload.w.close();
  return 'the Assignee group lists every person with cards as a filter (name and email of one person merged), and a link keeps it';
}

(async () => {
  console.log('PASS ' + await everyAssigneeIsAFilterUnderAssignee());
})().catch((e) => { console.error('FAIL', e); process.exit(1); });
