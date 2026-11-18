const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const SLOT = '<script type="text/plain" id="plugin-scripts"></script>';
const PLUGINS = [{id: 'demo', name: 'Demo', icon: '/plugins/demo/icon.svg', tab: 'demo', passes: [], instances: [], pages: [{tab: 'demo', label: 'Demo', group: 'observe', icon: '/plugins/demo/icon.svg'}]}];

const PANEL = `<script>
(function () {
  var state = null, section = null;
  window.__seen = [];
  function render() {
    if (!state || !section) return;
    section.textContent = (state.demo || []).map(function (r) { return r.pvc + ' at ' + r.pct + ' %'; }).join(', ');
  }
  document.addEventListener('skopax:state', function (e) { state = e.detail.state; window.__seen.push({event: 'state', rev: state.rev, demo: state.demo}); render(); });
  document.addEventListener('skopax:page', function (e) { if (e.detail.tab !== 'demo') return; section = e.detail.section; window.__seen.push({event: 'page'}); render(); });
})();
</script>`;

function stateAt(rev, pct) {
  return {rev, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'ok', headline: 'x', points: [], at: iso(1)}, anomalies: [], services: [], history: [], routes: [], deploys: [],
    demo: [{part: 'volumes', ns: 'shop', pvc: 'data-0', pct, plugin: 'demo'}]};
}

async function boot(serve) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  vc.on('error', (e) => errors.push('console.error ' + e));
  const html = pageHtml(TEMPLATE).replace(SLOT, PANEL).replace('/*__SKOPAX_DATA__*/', JSON.stringify(serve.state).replace(/<\//g, '<\\/'));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=demo', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', operator: true, admin: false, teams: [], tags: []});
        if (p.startsWith('/api/server')) return ok({plugins: PLUGINS, tz: 'UTC'});
        if (p.startsWith('/api/admin/settings')) return ok({items: [], rev: 1, refreshSeconds: 15});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: serve.rev});
        if (p.startsWith('/api/state')) return ok(serve.state);
        return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
      };
    },
  });
  await wait(400);
  return {dom, w: dom.window, doc: dom.window.document, errors};
}

async function aPanelRepaintsFromTheStateOfEveryLiveUpdate() {
  //given
  const serve = {rev: 5, state: stateAt(5, 40)};
  const {dom, w, doc, errors} = await boot(serve);
  const section = doc.getElementById('panel-demo');
  const first = [...w.__seen].map((s) => s.event);
  const before = section && section.textContent;

  //when
  serve.rev = 6;
  serve.state = stateAt(6, 91);
  w.document.dispatchEvent(new w.Event('visibilitychange'));
  await wait(300);

  //then
  assert.deepStrictEqual(first, ['state', 'page'], 'the page-load paint tells the panel the state before its section arrives: ' + first.join());
  assert.strictEqual(before, 'data-0 at 40 %', 'the panel renders the page-load state into its section');
  const states = [...w.__seen].filter((s) => s.event === 'state');
  assert.strictEqual(states.length, 2, 'a live update fires a second skopax:state');
  assert.strictEqual(states[1].rev, 6);
  assert.deepStrictEqual([...states[1].demo].map((r) => r.pct), [91], 'its detail.state is the new state');
  assert.strictEqual(section.textContent, 'data-0 at 91 %', 'and the panel repaints from it');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a plug-in panel repaints from detail.state of skopax:state, once at page load and again after a live update';
}

(async () => {
  for (const t of [aPanelRepaintsFromTheStateOfEveryLiveUpdate]) {
    console.log('PASS ' + (await t()));
  }
})().catch((e) => { console.error('FAIL', e.message); process.exit(1); });
