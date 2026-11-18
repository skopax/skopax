const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const KEY = 'log|checkout-service|ERROR|boom';

async function boot() {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [{key: KEY, sev: 'warn', svc: 'checkout-service', label: 'ERROR lines', what: 'x', short: 'ERROR burst', status: 'open', firstSeen: iso(40), lastSeen: iso(1), src: 'log', sig: 'err', ns: 'shop', now: 40, d1: 5, d7: 4}],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=anoms', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'nina.white@example.com', operator: true, jira: true, teams: [], tags: [],
          task: {project: 'OPS', projects: ['OPS', 'WEB'], parent: '', points: 1, techProject: 'INFRA', techAssignee: true}});
        if (p.startsWith('/api/jira/search')) return ok(p.includes('q=zzz') ? [] : [{key: 'WEB-201', summary: 'Audit History', status: 'In Progress', developer: null}]);
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/nodes')) return ok({nodes: []});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors};
}

async function escapeInTheEpicSuggestionsClosesOnlyTheListAndKeepsTheEditedTask() {
  //given
  const {w, doc, errors} = await boot();
  doc.querySelector('.jira-btn').click();
  const wrap = doc.getElementById('task-wrap'), desc = doc.getElementById('task-desc'), proj = doc.getElementById('task-project'), epic = doc.getElementById('task-epic');
  const list = () => doc.getElementById('task-epic-suggest');
  const esc = () => epic.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Escape', bubbles: true, cancelable: true}));
  const type = async (q) => { epic.value = q; epic.dispatchEvent(new w.Event('input', {bubbles: true})); await wait(350); };
  desc.value = 'my own words';
  proj.value = 'WEB';
  proj.dispatchEvent(new w.Event('change', {bubbles: true}));

  //when
  await type('audit');
  const listed = !list().hidden && list().querySelectorAll('li[data-i]').length === 1;
  esc();
  const afterList = [wrap.hidden, list().hidden, desc.value];
  await type('zzz');
  const noMatch = !list().hidden && !!list().querySelector('.tsg-none');
  esc();
  const afterNote = [wrap.hidden, list().hidden, desc.value];
  esc();

  //then
  assert.ok(listed, 'the epic search lists a match');
  assert.deepStrictEqual(afterList, [false, true, 'my own words'], 'Escape closes the suggestions, the dialog stays with the edited description');
  assert.ok(noMatch, 'a search without a match shows its note');
  assert.deepStrictEqual(afterNote, [false, true, 'my own words'], 'Escape closes the no-match note, the dialog stays');
  assert.ok(wrap.hidden, 'Escape with no list open closes the dialog');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS Escape in the epic suggestions (with matches or the no-match note) closes only the list and keeps the edited task; the next Escape closes the dialog');
}

(async () => {
  await escapeInTheEpicSuggestionsClosesOnlyTheListAndKeepsTheEditedTask();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
