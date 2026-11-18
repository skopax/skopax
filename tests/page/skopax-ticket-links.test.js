const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'ok', headline: 'x', points: [], at: iso(1)}, anomalies: [],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(me) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state()));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=anoms', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok(me);
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, errors};
}

async function onlyConfiguredProjectKeysBecomeLinks() {
  //given
  const text = 'CVE-2024-1234 and SHA-256 and OPS-12';
  const configured = await boot({loginEnabled: false, jiraUrl: 'https://jira.example.com', task: {projects: ['OPS', 'WEB']}});
  const bare = await boot({loginEnabled: false, jiraUrl: 'https://jira.example.com', task: {projects: []}});

  //when
  const withProjects = configured.w.jiraLinks(text);
  const without = bare.w.jiraLinks(text);

  //then
  assert.strictEqual(withProjects, 'CVE-2024-1234 and SHA-256 and <a href="https://jira.example.com/browse/OPS-12" target="_blank" rel="noopener">OPS-12 ↗</a>');
  assert.strictEqual(without, text);
  assert.deepStrictEqual(configured.errors.concat(bare.errors), []);
  configured.w.close();
  bare.w.close();
  console.log('PASS ticket keys link only for the configured Jira projects: CVE-2024-1234 and SHA-256 stay plain text, and with no projects nothing is linked');
}

(async () => {
  await onlyConfiguredProjectKeysBecomeLinks();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
