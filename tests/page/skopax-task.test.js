const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const KEY = 'log|checkout-service|ERROR|boom';

async function boot(stored, prefix) {
  const errors = [], calls = [];
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
      if (stored) w.localStorage.setItem('skopax-task-project', stored);
      w.fetch = async (path, opts) => {
        const p = String(path), method = (opts && opts.method) || 'GET';
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        calls.push([method, p, opts && opts.body ? JSON.parse(opts.body) : null]);
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'dora.lane@example.com', operator: true, jira: true, teams: [], tags: [],
          task: {project: 'OPS', projects: ['OPS', 'WEB', 'APP', 'MOB'], parent: '', points: 1, techProject: 'INFRA', techAssignee: true, titlePrefix: prefix === undefined ? 'OPS: ' : prefix}});
        if (p.startsWith('/api/jira/search')) return ok([{key: 'WEB-201', summary: 'Audit History', status: 'In Progress', developer: null}]);
        if (p.startsWith('/api/actions') && method === 'POST') return ok({id: 'a1', status: 'done', result: 'WEB-202 created \u00b7 In progress'});
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

function openDialog(doc) {
  const b = doc.querySelector('.jira-btn');
  assert.ok(b, 'the card has a Create task button');
  b.click();
}

async function theDialogAsksForTheProjectAndTakesAnEpicFromJiraSearch() {
  //given
  const {w, doc, errors, calls} = await boot(null);
  const row = doc.querySelector('#anoms .row, #anoms summary');
  if (row) row.click();
  await wait(50);
  openDialog(doc);
  const proj = doc.getElementById('task-project'), epic = doc.getElementById('task-epic'), err = doc.getElementById('task-err'), meta = doc.getElementById('task-meta');

  //then
  assert.deepStrictEqual([...proj.options].map((o) => o.textContent), ['Choose\u2026', 'OPS', 'WEB', 'APP', 'MOB', 'INFRA (Infra)']);
  assert.ok(proj.value === '' && epic.value === '' && meta.textContent.startsWith('Pick the project this task goes to'), 'no project and no epic are preset');

  //when
  doc.getElementById('task-yes').click();
  const refused = err.textContent;
  proj.value = 'WEB';
  proj.dispatchEvent(new w.Event('change', {bubbles: true}));
  epic.value = 'audit hist';
  epic.dispatchEvent(new w.Event('input', {bubbles: true}));
  await wait(400);
  const option = doc.querySelector('#task-epic-suggest li[data-i]');
  option.dispatchEvent(new w.MouseEvent('mousedown', {bubbles: true, cancelable: true}));
  epic.dispatchEvent(new w.Event('input', {bubbles: true}));
  const picked = epic.value, said = meta.textContent;
  doc.getElementById('task-yes').click();
  await wait(60);

  //then
  assert.strictEqual(refused, 'Pick the project this task goes to.');
  assert.ok(calls.some((c) => c[1] === '/api/jira/search?q=' + encodeURIComponent('audit hist') + '&type=epic&project=WEB'), 'the epic search is limited to epics of the chosen project');
  assert.ok(picked === 'WEB-201' && said.startsWith('Project WEB \u00b7 Epic WEB-201 \u00b7 Active sprint'));
  const post = calls.filter((c) => c[0] === 'POST' && c[1] === '/api/actions')[0][2];
  assert.ok(post.type === 'ticket' && post.project === 'WEB' && post.epic === 'WEB-201' && post.points === 1 && post.title === 'OPS: checkout-service ERROR lines');
  assert.strictEqual(w.localStorage.getItem('skopax-task-project'), 'WEB');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS Create task lists the projects with none preset, refuses without one, searches epics of the chosen project, and sends the project and epic');
}

async function theLastProjectIsRememberedAndInfraNeedsNoEpic() {
  //given
  const {w, doc, errors, calls} = await boot('APP');
  const row = doc.querySelector('#anoms .row, #anoms summary');
  if (row) row.click();
  await wait(50);
  openDialog(doc);
  const proj = doc.getElementById('task-project');

  //then
  assert.strictEqual(proj.value, 'APP', 'the project picked last time is preselected');

  //when
  proj.value = 'INFRA';
  proj.dispatchEvent(new w.Event('change', {bubbles: true}));
  const hidden = doc.getElementById('task-epic-f').hidden && doc.getElementById('task-points-f').hidden, title = doc.getElementById('task-title').value;
  doc.getElementById('task-yes').click();
  await wait(60);

  //then
  assert.ok(hidden && title === 'checkout-service ERROR lines', 'an Infra task has no epic, no points and no title prefix');
  const post = calls.filter((c) => c[0] === 'POST' && c[1] === '/api/actions')[0][2];
  assert.ok(post.project === 'INFRA' && !('epic' in post) && !('points' in post));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the project picked last time is preselected, and an Infra task goes out without epic or points');
}

async function withoutATitlePrefixTheTitleIsTheCardAlone() {
  //given
  const {w, doc, errors} = await boot(null, '');
  const row = doc.querySelector('#anoms .row, #anoms summary');
  if (row) row.click();
  await wait(50);
  openDialog(doc);
  const proj = doc.getElementById('task-project'), title = doc.getElementById('task-title');
  const opened = title.value;

  //when
  proj.value = 'INFRA';
  proj.dispatchEvent(new w.Event('change', {bubbles: true}));
  const tech = title.value;
  proj.value = 'WEB';
  proj.dispatchEvent(new w.Event('change', {bubbles: true}));

  //then
  assert.deepStrictEqual([opened, tech, title.value], ['checkout-service ERROR lines', 'checkout-service ERROR lines', 'checkout-service ERROR lines']);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS with no title prefix set, the task title is the card alone for every project');
}

(async () => {
  await theDialogAsksForTheProjectAndTakesAnEpicFromJiraSearch();
  await theLastProjectIsRememberedAndInfraNeedsNoEpic();
  await withoutATitlePrefixTheTitleIsTheCardAlone();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
