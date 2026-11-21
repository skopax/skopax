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
        const W = w;
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
          if (W.__failPut) return {ok: false, status: 500, json: async () => ({detail: 'store unavailable'})};
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

function textEntry(key, group, label, kind, limit) {
  return {key, group, label, help: label, kind, live: true, value: '', base: '', default: '', source: 'default', by: null, at: null, limit};
}


async function savingWithAClasslessFieldEditedInAnotherRowDoesNotStick() {
  //given
  const {w, doc, errors, calls} = await boot('#t=ai', [textEntry('investigate.model', 'Investigator', 'Model', 'text', 100)]);
  const row = (key) => doc.querySelector('.adm-row[data-key="' + key + '"]');
  const bare = doc.createElement('input');
  row('investigate.model').appendChild(bare);
  typeIn(w, bare, 'kept');
  typeIn(w, row('investigate.daily-usd').querySelector('.adm-in'), '30');

  //when
  row('investigate.daily-usd').querySelector('.adm-save').click();
  await wait(80);

  //then
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'PUT').map((c) => c[2]), [{key: 'investigate.daily-usd', value: '30'}]);
  assert.strictEqual(row('investigate.daily-usd').querySelector('.adm-msg').textContent, 'Saved, live within 15 s on every pod');
  assert.deepStrictEqual(errors, [], 'no selector error');
  assert.strictEqual(doc.querySelectorAll('.toast.fail').length, 0, 'no failure toast on a good save');
  w.close();
  console.log('PASS saving the daily limit while a field without a class is edited in another row finishes and shows Saved');
}

async function aFailedSaveAlwaysShowsAFailureToast() {
  //given
  const {w, doc, errors} = await boot('#t=ai');
  const row = (key) => doc.querySelector('.adm-row[data-key="' + key + '"]');
  w.__failPut = true;
  typeIn(w, row('investigate.daily-usd').querySelector('.adm-in'), '40');

  //when
  row('investigate.daily-usd').querySelector('.adm-save').click();
  await wait(80);

  //then
  const toasts = [...doc.querySelectorAll('.toast.fail .toast-t')].map((t) => t.textContent);
  assert.deepStrictEqual(toasts, ['Not done: store unavailable (500).']);
  assert.strictEqual(doc.getElementById('fail-toasts').getAttribute('role'), 'alert');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a failed save always shows a failure toast with the server reason');
}

(async () => {
  await savingWithAClasslessFieldEditedInAnotherRowDoesNotStick();
  await aFailedSaveAlwaysShowsAFailureToast();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
