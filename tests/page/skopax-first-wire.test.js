const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const FREE = 'log|checkout-service|ERROR|boom';
const MINE = 'log|payments-api|ERROR|npe';
const ME = 'paul.stone@example.com';

function state() {
  const card = (key, extra) => Object.assign({key, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'x', src: 'logs', sig: 'log', now: 1, firstSeen: iso(30), lastSeen: iso(1)}, extra || {});
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(FREE), card(MINE, {owner: 'Paul Stone'})],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(hash) {
  const errors = [], searched = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state()));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/' + hash, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: ME, operator: true, jira: true, slack: true, slackChannel: '#ops', tags: [],
          routes: [{id: 'teams/tech', name: 'tech', tags: [], tagged: false, business: false, plugin: 'Microsoft Teams'}]});
        if (p.startsWith('/api/jira/search')) { searched.push(p); return ok([{key: 'OPS-144', summary: 'SEO metadata fetch cache', status: 'In UAT'}]); }
        if (p.startsWith('/api/investigations') || p.startsWith('/api/server')) throw new Error('offline');
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors, searched};
}

const cardOf = (doc, k) => doc.querySelector('#anoms article.inc[data-key="' + k + '"]');

async function cardsPaintedBeforeTheLoginAnswerGetJiraSearchTeamsButtonsAndNoAssignToMeOnMyCard() {
  for (const hash of ['#t=anoms', '#a=' + encodeURIComponent(FREE)]) {
    //given
    const {w, doc, errors, searched} = await boot(hash);
    const free = cardOf(doc, FREE);

    //when
    free.querySelector('.acts .btn[aria-label="Start progress"]').click();
    const tin = free.querySelector('input[id^="ticket-"]');
    tin.value = 'seo cache';
    tin.dispatchEvent(new w.Event('input'));
    await wait(350);

    //then
    assert.ok(!free.querySelector('.acts').hidden, hash + ': the action row shows');
    assert.ok(tin.parentNode.classList.contains('tsg'), hash + ': the ticket field of Take searches Jira');
    assert.deepStrictEqual(searched, ['/api/jira/search?q=seo%20cache'], hash);
    assert.deepStrictEqual([...free.querySelectorAll('.tsg-list li')].map((li) => li.querySelector('b').textContent), ['OPS-144'], hash);
    assert.deepStrictEqual([...free.querySelectorAll('.acts .card-more-box .teams-btn')].map((b) => b.dataset.route), ['teams/tech'], hash + ': the More menu has the route button');
    const mineBtn = cardOf(doc, MINE).querySelector('.asg-me');
    assert.ok(!mineBtn || mineBtn.hidden, hash + ': no Assign to me on a card that is already mine');
    assert.ok(cardOf(doc, FREE).querySelector('.asg-me') && !cardOf(doc, FREE).querySelector('.asg-me').hidden, hash + ': Assign to me on a card of nobody');
    assert.deepStrictEqual(errors, []);
    w.close();
  }
  console.log('PASS cards painted before /api/me answered get the Jira search on Take, the Teams buttons, and no Assign to me on a card that is already mine, on the list and on a #a= link');
}

(async () => {
  await cardsPaintedBeforeTheLoginAnswerGetJiraSearchTeamsButtonsAndNoAssignToMeOnMyCard();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
