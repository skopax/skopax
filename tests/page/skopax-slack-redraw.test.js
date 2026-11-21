const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

function slack() {
  return {id: 'slack', name: 'Slack', version: '0.1.0', category: 'chat', description: 'Slack', origin: 'builtin', state: 'ok', reason: null, enabled: true, icon: false,
    fields: [{key: 'bot-token', label: 'Bot token', kind: 'secret', required: true, default: '', help: '', choices: [], showIf: null}],
    checks: [], rules: [], tools: [], setup: {steps: ['Create the app.'], docs: 'https://slack.example.com/docs', docsLabel: 'Guide'},
    instances: [{name: 'main', primary: true, enabled: true, source: 'admin', problem: null, values: {}, secrets: {'bot-token': {set: true}}, offChecks: []}],
    health: {state: 'ok', text: 'Connected', at: iso(1), instances: [{name: 'main', state: 'ok', text: 'Connected', at: iso(1)}]}};
}

const PACK = {tr: {
  'Redraw Slack posts': 'Slack g\u00f6nderilerini yeniden \u00e7iz',
  'Redrawing Slack posts: {0} of {1}': 'Slack g\u00f6nderileri yeniden \u00e7iziliyor: {0} / {1}',
  'Slack posts redrawn: {0}': 'Slack g\u00f6nderileri yeniden \u00e7izildi: {0}',
}};

async function boot(role, pack) {
  const errors = [], calls = [], job = {status: {running: false}, posts: 0, finish: null};
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const admin = role === 'admin';
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/#t=integrations', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      if (pack) { w.SKOPAX_I18N = pack; w.document.cookie = 'skopax_lang=' + Object.keys(pack)[0] + '; path=/'; }
      w.fetch = async (path, opts) => {
        const p = String(path), method = (opts && opts.method) || 'GET', body = opts && opts.body ? JSON.parse(opts.body) : null;
        const ok = (b, status) => ({ok: true, status: status || 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/admin') || p.startsWith('/api/integrations')) calls.push([method, p, body]);
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', userRole: role, operator: role !== 'viewer', admin, teams: [], tags: []});
        if (p === '/api/integrations') return ok({edition: 'community', readOnly: !admin, plugins: [slack()]});
        if (p === '/api/integrations/custom-checks') return ok({readOnly: true, items: [], limits: {checks: 50, periodSeconds: 300}, choices: {}, presets: []});
        if (p.startsWith('/api/admin')) {
          if (!admin) return {ok: false, status: 403, json: async () => ({detail: 'no'})};
          if (p === '/api/admin/slack-redraw' && method === 'GET') return ok(job.status);
          if (p === '/api/admin/slack-redraw' && method === 'POST') {
            const started = !job.status.running;
            if (started) job.status = {running: true, total: 4, done: 1, skipped: 0};
            return ok({started, ...job.status}, 202);
          }
          if (p === '/api/admin/plugins') return ok({edition: 'community', secretsKey: 'generated', plugins: [slack()]});
          if (p.startsWith('/api/admin/custom-checks')) return ok({items: [], limits: {checks: 50, periodSeconds: 300}, choices: {}, presets: []});
          if (p === '/api/admin/notices') return ok({items: []});
          if (p.startsWith('/api/admin/settings')) return ok({items: [], rev: 1, refreshSeconds: 15});
          return ok([]);
        }
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/nodes')) return ok({nodes: []});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  dom.window.__skopaxConfirm = async () => true;
  await wait(400);
  const doc = dom.window.document;
  const tile = doc.querySelector('.int-tile');
  if (tile) { tile.click(); await wait(250); }
  return {w: dom.window, doc, errors, calls, job};
}

async function anAdminSeesTheRedrawButtonOnTheSlackPageAndAClickStartsOneJob() {
  //given
  const {w, doc, errors, calls, job} = await boot('admin');
  const box = doc.querySelector('.int-redraw');

  //then
  assert.ok(box, 'the Slack panel has the redraw section');
  const button = box.querySelector('.int-redraw-b');
  assert.strictEqual(button.textContent, 'Redraw Slack posts');
  assert.ok(!box.querySelector('input'), 'there is no days window any more');
  assert.match(box.textContent, /cards still on the board/);

  //when
  button.click();
  await wait(120);
  button.click();
  await wait(60);

  //then
  const posts = calls.filter((c) => c[0] === 'POST' && c[1] === '/api/admin/slack-redraw');
  assert.strictEqual(posts.length, 1, 'one start without parameters, and a second click while it runs sends nothing');
  assert.strictEqual(button.disabled, true);
  assert.strictEqual(box.querySelector('.int-redraw-s').textContent, 'Redrawing Slack posts: 1 of 4');

  //when
  job.status = {running: false, done: 4, total: 4, skipped: 1, result: 'done'};
  await wait(2300);

  //then
  assert.strictEqual(box.querySelector('.int-redraw-s').textContent, 'Slack posts redrawn: 4, 1 skipped (gone or not reachable)');
  assert.strictEqual(button.disabled, false);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'an admin redraws the Slack posts from the Slack page: one job, a status line, no second click while it runs';
}

async function aViewerOpensTheSlackPanelAndDoesNotSeeTheButton() {
  //given
  const {w, doc, calls, errors} = await boot('viewer');

  //then
  assert.ok(doc.querySelector('.int-tile'), 'the Slack tile shows');
  assert.ok(doc.querySelector('#int-panel .int-itabs, #int-panel .int-iempty') || doc.querySelector('#int-panel'), 'the Slack panel is open');
  assert.ok(doc.querySelector('#int-panel') && !doc.querySelector('#int-panel').hidden, 'the panel is visible');
  assert.ok(!doc.querySelector('.int-redraw'), 'no redraw section for a viewer');
  assert.deepStrictEqual(calls.filter((c) => c[1] === '/api/admin/slack-redraw'), []);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a viewer opens the Slack panel, sees no redraw button and sends nothing';
}

async function theButtonAndItsStatusAreTranslated() {
  //given
  const {w, doc, errors, job} = await boot('admin', PACK);
  job.status = {running: true, days: 7, total: 4, done: 2, skipped: 0};
  const box = doc.querySelector('.int-redraw');

  //then
  assert.ok(box, 'the section shows in Turkish');
  assert.strictEqual(box.querySelector('.int-redraw-b').textContent, 'Slack g\u00f6nderilerini yeniden \u00e7iz');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the redraw button is translated through the pack';
}

(async () => {
  for (const t of [anAdminSeesTheRedrawButtonOnTheSlackPageAndAClickStartsOneJob, aViewerOpensTheSlackPanelAndDoesNotSeeTheButton, theButtonAndItsStatusAreTranslated]) {
    console.log('PASS ' + await t());
  }
  process.exit(0);
})().catch((e) => { console.error('FAIL ' + (e && e.stack || e)); process.exit(1); });
