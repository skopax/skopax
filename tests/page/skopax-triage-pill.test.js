const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

function card(key, extra) {
  return Object.assign({key, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'ERROR lines', src: 'logs', sig: 'log', now: 1, ns: 'shop', firstSeen: iso(30), lastSeen: iso(1)}, extra || {});
}

function state(cards) {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'ok', headline: 'x', points: [], at: iso(1)}, anomalies: cards || [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(opts) {
  opts = opts || {};
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state(opts.cards))), {
    url: 'https://skopax.example.com/' + (opts.hash || '#t=anoms'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      if (opts.pack) { w.SKOPAX_I18N = opts.pack; w.document.cookie = 'skopax_lang=' + Object.keys(opts.pack)[0] + '; path=/'; }
      Object.keys(opts.stored || {}).forEach((k) => w.localStorage.setItem(k, opts.stored[k]));
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/notifications/count')) return ok({unread: opts.unread || 0});
        if (p.startsWith('/api/me')) return ok(Object.assign({loginEnabled: true, user: 'alice@example.com', operator: true, admin: false, teams: [], tags: [], chat: true, jiraUrl: opts.jira || '', task: {projects: ['OPS']}}, opts.anon ? {user: ''} : {}));
        if (p.startsWith('/api/server')) return ok({plugins: [], tz: 'UTC'});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
      };
    },
  });
  await wait(350);
  return {dom, w: dom.window, doc: dom.window.document, errors};
}


async function theTriagePillKeepsItsTicketLinkAndItsWholeLabel() {
  //given
  const t = await boot({hash: '#t=anoms', jira: 'https://jira.example.com', cards: [
    card('log|checkout-service|ERROR|a', {num: 14, triage: 'progress', ticket: 'OPS-3130', triagedBy: 'alice@example.com'}),
    card('log|cart-service|ERROR|b', {num: 15, triage: 'progress', ticket: 'OPS-3101'}),
  ]});
  t.w.setFilter('all');
  await wait(20);

  //when
  const pills = [...t.doc.querySelectorAll('#anoms article.inc .tag.tri-wrap')];
  const css = [...t.doc.querySelectorAll('style')].map((s) => s.textContent).join('\n');
  const rule = (css.match(/\.inc-tags \.tag, \.tech-tags \.tag \{[^}]*\}/) || [''])[0];

  //then
  assert.strictEqual(pills.length, 2, 'both cards show the pill');
  pills.forEach((pill, n) => {
    const link = pill.querySelector('a');
    const ticket = n === 0 ? 'OPS-3130' : 'OPS-3101';
    assert.ok(link && link.getAttribute('href') === 'https://jira.example.com/browse/' + ticket, 'the ticket is a link to Jira');
    assert.ok(link.textContent.startsWith(ticket), 'the link names the ticket');
    assert.ok(/^In progress/.test(pill.textContent.trim()) && pill.textContent.includes(ticket), 'the label is whole: ' + pill.textContent);
  });
  assert.ok(/white-space: nowrap;/.test(rule) && /text-overflow: ellipsis;/.test(rule), 'the pill is a one-line chip that ends in an ellipsis');
  pills.forEach((pill) => assert.ok(/^In progress/.test(pill.title) && /OPS-31/.test(pill.title), 'its title carries the whole label: ' + pill.title));
  assert.deepStrictEqual(t.errors, []);
  t.dom.window.close();
  console.log('PASS the triage pill keeps its ticket link, and its whole label in the title');
}

(async () => {
  await theTriagePillKeepsItsTicketLinkAndItsWholeLabel();
  process.exit(0);
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
