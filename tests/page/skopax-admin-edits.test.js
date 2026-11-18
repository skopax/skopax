const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const ADMIN_USER = 'nina.white@example.com';

function rules(key, label) {
  return {key, group: 'Monitor rules', label, help: 'Thresholds of ' + label, kind: 'rules', live: true, value: '', base: '', default: '', source: 'default', by: null, at: null,
    defaults: {low_warn: 0.5}, rules: {low_warn: 0.5}, baseRules: {low_warn: 0.5}, ruleHelp: {}};
}

function nodes() {
  const cluster = {total: 2, ready: 2, notReady: [], pressure: [], unschedulable: [], nodes: []};
  return {at: iso(0), periodS: 60, configured: ['prod', 'test'], nodes: [
    {name: 'prod', role: 'master', status: 'Ready', since: iso(600), lastSeen: iso(0), startedAt: iso(600), version: 'decaf001', via: 'in-cluster', cluster},
    {name: 'test', role: 'agent', status: 'Ready', since: iso(600), lastSeen: iso(0), startedAt: iso(600), version: 'decaf001', via: 'https://agent.test.example.com', cluster, reads: {}}]};
}

async function boot(hash, extra = []) {
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  let list = [rules('monitor.umami-rules', 'Umami'), rules('monitor.ga-rules', 'GA4'),
    {key: 'investigate.daily-usd', group: 'Investigator', label: 'Daily limit ($)', help: 'Daily spend cap', kind: 'float', live: true, value: '25', base: '25', default: '25', source: 'default', by: null, at: null, low: 0, high: 500}].concat(extra);
  let secret = {key: 'wingify.api-token', label: 'Wingify (VWO) API token', group: 'Wingify', help: 'A token', set: false, by: null, at: null};
  let fields = [{key: 'elastic.logging-url', label: 'Logs Elasticsearch URL', group: 'Logs', secret: false, help: 'The test Elasticsearch', set: false, by: null, at: null, value: ''},
    {key: 'elastic.logging-api-key', label: 'Logs API key', group: 'Logs', secret: true, help: 'A read-only API key', set: false, by: null, at: null}];
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/' + hash, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, opts) => {
        const p = String(path), method = (opts && opts.method) || 'GET', body = opts && opts.body ? JSON.parse(opts.body) : null;
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/admin')) {
          calls.push([method, p, body]);
          if (p.startsWith('/api/admin/agent-sources')) {
            if (method === 'PUT') {
              fields = fields.map((f) => f.key !== body.key ? f : Object.assign({}, f, {set: body.value != null, by: ADMIN_USER, at: iso(0)}, f.secret ? {} : {value: body.value || ''}));
              return ok({change: {}, pushed: true});
            }
            return ok({agents: [{name: 'test', sources: true, fields}], canEncrypt: true});
          }
          if (p.startsWith('/api/admin/secrets')) {
            if (method === 'PUT') { secret = Object.assign({}, secret, {set: body.value != null, by: ADMIN_USER, at: iso(0)}); return ok({change: {}}); }
            return ok({fields: [secret], canEncrypt: true});
          }
          if (p.startsWith('/api/admin/teams')) return ok({routes: [], tags: [{id: 'seo', label: 'SEO'}], hosts: []});
          if (p.startsWith('/api/admin/history')) return ok([]);
          if (p === '/api/admin/notices') return ok({items: []});
          if (p === '/api/admin/features') return ok({edition: 'community', items: []});
          if (p === '/api/admin/plugins') return ok({plugins: []});
          if (method === 'GET') return ok({items: list, rev: 3, refreshSeconds: 15});
          const item = list.find((i) => i.key === body.key);
          const next = Object.assign({}, item, {value: body.value, source: 'admin', by: ADMIN_USER, at: iso(0)}, item.kind === 'rules' ? {rules: Object.assign({}, item.defaults, JSON.parse(body.value))} : {});
          list = list.map((i) => i.key === body.key ? next : i);
          return ok({item: next, change: {}});
        }
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: ADMIN_USER, operator: true, admin: true, teams: [], tags: []});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/nodes')) return ok(nodes());
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors, calls};
}

const typeIn = (w, inp, value) => { inp.value = value; inp.dispatchEvent(new w.Event('input', {bubbles: true})); };

async function savingOneSettingKeepsTheOtherUnsavedEditsOnTheAdminTab() {
  //given
  const {w, doc, errors, calls} = await boot('#t=settings');
  const row = (key) => doc.querySelector('#admin .adm-row[data-key="' + key + '"], #ai-rows .adm-row[data-key="' + key + '"]');
  typeIn(w, row('monitor.umami-rules').querySelector('input[data-rule="low_warn"]'), '0.3');
  typeIn(w, row('monitor.ga-rules').querySelector('input[data-rule="low_warn"]'), '0.2');

  //when
  row('monitor.umami-rules').querySelector('.adm-save').click();
  await wait(60);

  //then
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'PUT').map((c) => c[2]), [{key: 'monitor.umami-rules', value: '{"low_warn":0.3}'}]);
  assert.strictEqual(row('monitor.umami-rules').querySelector('.adm-msg').textContent, 'Saved, live within 15 s on every pod');
  assert.strictEqual(row('monitor.ga-rules').querySelector('input[data-rule="low_warn"]').value, '0.2', 'the other rule table keeps its edit');
  assert.ok(!row('monitor.ga-rules').querySelector('.adm-save').disabled, 'and can still be saved');
  assert.ok(row('investigate.daily-usd').closest('#ai-rows'), 'the Investigator setting lives on Admin \u00b7 Investigator');
  assert.strictEqual(row('investigate.daily-usd').querySelector('.adm-in').value, '25', 'an untouched field shows the stored value');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS on the Admin tab, saving one setting keeps the other unsaved edits (another rule table) ready to save');
}

function textEntry(key, group, label, kind, limit) {
  return {key, group, label, help: label, kind, live: true, value: '', base: '', default: '', source: 'default', by: null, at: null, limit};
}

async function aTextSettingTakesItsLengthLimitFromTheEntry() {
  //given
  const extra = [textEntry('core.org-about', 'Investigator', 'About your company', 'text', 300), textEntry('investigate.web-domains', 'Investigator', 'Web sites', 'text', 1000),
    textEntry('general.timezone', 'General', 'Timezone', 'text', 1000), textEntry('jira.projects', 'Jira tasks', 'Projects', 'list', 1000)];
  const ai = await boot('#t=ai', extra);
  const {w, doc, errors} = await boot('#t=settings', extra);

  //when
  const box = (d, key) => d.querySelector('.adm-row[data-key="' + key + '"] input.adm-in');

  //then
  assert.strictEqual(box(ai.doc, 'core.org-about').getAttribute('maxlength'), '300', 'About your company stops at the 300 characters the server takes');
  assert.strictEqual(box(ai.doc, 'investigate.web-domains').getAttribute('maxlength'), '1000', 'another Investigator text setting keeps the limit its entry gives');
  assert.strictEqual(box(doc, 'general.timezone').getAttribute('maxlength'), '1000', 'a text setting on Settings too');
  assert.strictEqual(box(doc, 'jira.projects').getAttribute('maxlength'), '1000', 'and a list setting');
  assert.deepStrictEqual(errors.concat(ai.errors), []);
  w.close();
  ai.w.close();
  console.log('PASS a text setting takes its length limit from its entry: About your company stops at 300 characters');
}

async function aLongTextSettingIsATextAreaThatKeepsItsLineBreaks() {
  //given
  const {w, doc, errors, calls} = await boot('#t=settings', [textEntry('website.scan-prompt', 'Website', 'Scan prompt', 'text', 20000), textEntry('general.timezone', 'General', 'Timezone', 'text', 1000)]);
  const row = (key) => doc.querySelector('.adm-row[data-key="' + key + '"]');
  const area = row('website.scan-prompt').querySelector('.adm-in');

  //when
  typeIn(w, area, 'Proofread this page.\nReport only real typos.');
  row('website.scan-prompt').querySelector('.adm-save').click();
  await wait(60);

  //then
  assert.strictEqual(area.tagName, 'TEXTAREA', 'a text setting longer than one line is a text area');
  assert.strictEqual(area.getAttribute('maxlength'), '20000', 'that stops at the limit its entry gives');
  assert.strictEqual(row('general.timezone').querySelector('.adm-in').tagName, 'INPUT', 'a short text setting stays one line');
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'PUT').map((c) => c[2]), [{key: 'website.scan-prompt', value: 'Proofread this page.\nReport only real typos.'}]);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a long text setting such as the scan prompt is a text area that keeps its line breaks when saved');
}

(async () => {
  await savingOneSettingKeepsTheOtherUnsavedEditsOnTheAdminTab();
  await aTextSettingTakesItsLengthLimitFromTheEntry();
  await aLongTextSettingIsATextAreaThatKeepsItsLineBreaks();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
