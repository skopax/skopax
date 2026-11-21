const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

function items() {
  return [
    {key: 'investigate.model', group: 'Investigator', label: 'Card model', help: 'Model for card runs', kind: 'choice', live: true, value: 'claude-opus-5-5', base: 'claude-opus-5-5', default: 'claude-opus-5-5', source: 'default', by: null, at: null,
      choices: ['claude-opus-5-5', 'claude-sonnet-5-5']},
    {key: 'investigate.daily-usd', group: 'Investigator', label: 'Daily limit ($)', help: 'Daily spend cap', kind: 'float', live: true, value: '40', base: '25', default: '25', source: 'admin', by: 'dora.lane@example.com', at: iso(30), low: 0, high: 500},
    {key: 'pagescan.hours', group: 'Page scan', label: 'Batch every (h)', help: 'Hours between batches', kind: 'float', live: false, value: '6', base: '6', default: '6', source: 'default', by: null, at: null, low: 1, high: 48},
    {key: 'monitor.umami-rules', group: 'Monitor rules', label: 'Umami', help: 'Thresholds of the Umami cards', kind: 'rules', live: true, value: '', base: '', default: '', source: 'default', by: null, at: null,
      defaults: {redirect_warn: 0.5, type_warn: 0.4}, rules: {redirect_warn: 0.5, type_warn: 0.4}, baseRules: {redirect_warn: 0.5, type_warn: 0.4},
      ruleHelp: {redirect_warn: 'Warning when redirects fall below this share.', type_warn: 'Warning when one product type falls below this share.'}},
  ];
}

async function boot(admin, hash, history, extra) {
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state));
  let list = items().concat(extra || []);
  let routes = [{name: 'tech', label: 'tech', match: ['*'], minSev: 'crit', business: false, webhook: 'default.environment.api.powerplatform.com', source: 'config', inConfig: true, by: null, at: null, removed: false}];
  let secret = {key: 'wingify.api-token', label: 'Wingify (VWO) API token', group: 'Wingify', help: 'A token with Browse permission', set: false, by: null, at: null};
  const hist = history || [{id: 'h1', seq: 1, key: 'investigate.daily-usd', old: null, new: '40', by: 'dora.lane@example.com', at: iso(30)}];
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/' + (hash || '#t=anoms'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, opts) => {
        const p = String(path), method = (opts && opts.method) || 'GET';
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        const bad = (status, detail) => ({ok: false, status, json: async () => ({detail})});
        if (p.startsWith('/api/admin')) {
          calls.push([method, p, opts && opts.body ? JSON.parse(opts.body) : null]);
          if (!admin) return bad(403, 'This needs the admin role.');
          if (p.startsWith('/api/admin/teams')) {
            if (method === 'GET') return ok({routes, tags: [{id: 'seo', label: 'SEO'}], hosts: ['.logic.azure.com']});
            if (p.startsWith('/api/admin/teams/test')) return ok({sent: true});
            if (p.startsWith('/api/admin/teams/reveal')) return ok({webhook: 'https://default.environment.api.powerplatform.com/workflows/tech?sig=TECHSECRET'});
            if (method === 'PUT') {
              const b = JSON.parse(opts.body);
              if (!b.webhook && !routes.some((r) => r.name === b.name)) return bad(422, 'a new route needs its Teams Workflows webhook');
              routes = routes.filter((r) => r.name !== b.name).concat([{name: b.name, label: b.name === 'seo' ? 'SEO' : b.name, match: (b.match || 'tag:' + b.name).split(',').map((x) => x.trim()).filter(Boolean),
                minSev: b.minSev || 'info', business: true, webhook: 'prod-00.westeurope.logic.azure.com', source: 'admin', inConfig: false, by: 'dora.lane@example.com', at: iso(0), removed: false}]);
              return ok({route: {}, change: {}});
            }
            if (method === 'DELETE') { routes = routes.map((r) => r.name === 'tech' ? Object.assign({}, r, {removed: true}) : r); return ok({change: {}}); }
          }
          if (p.startsWith('/api/admin/secrets')) {
            if (method === 'PUT') {
              const b = JSON.parse(opts.body);
              secret = Object.assign({}, secret, {set: b.value != null, by: b.value != null ? 'dora.lane@example.com' : null, at: b.value != null ? iso(0) : null});
              hist.unshift({id: 'h' + (hist.length + 1), seq: hist.length + 1, key: b.key, old: null, new: b.value == null ? null : 'secret set', by: 'dora.lane@example.com', at: iso(0), secret: true});
              return ok({change: {}});
            }
            return ok({fields: [secret], canEncrypt: true});
          }
          if (p.startsWith('/api/admin/history')) return ok(hist);
          if (p === '/api/admin/notices') return ok({items: []});
          if (p === '/api/admin/features') return ok({edition: 'community', items: []});
          if (p === '/api/admin/plugins') return ok({plugins: []});
          if (p === '/api/admin/anthropic-key') return ok({using: null, admin: false, config: false, unreadable: false, updatedBy: null, updatedAt: null, note: null});
          if (p === '/api/admin/users' || p === '/api/admin/tokens') return ok([]);
          if (p.startsWith('/api/admin/small-talk')) return ok({enabled: true, rules: 'Reply in one short line.', hourlyCap: 10, custom: false, updatedAt: null, updatedBy: null, usedThisHour: 0, defaults: {enabled: true, rules: 'Reply in one short line.', hourlyCap: 10}});
          if (method === 'GET') return ok({items: list, rev: 3, refreshSeconds: 15});
          const key = method === 'DELETE' ? decodeURIComponent(p.split('key=')[1]) : JSON.parse(opts.body).key;
          const value = method === 'DELETE' ? null : JSON.parse(opts.body).value;
          if (key === 'investigate.daily-usd' && value === '900') return bad(422, 'Daily limit ($) must be between 0 and 500');
          const item = list.find((i) => i.key === key);
          const next = Object.assign({}, item, value == null ? {value: item.base, source: 'default', by: null, at: null} : {value, source: 'admin', by: 'dora.lane@example.com', at: iso(0)});
          if (item.kind === 'rules') next.rules = Object.assign({}, item.defaults, value ? JSON.parse(value) : {});
          list = list.map((i) => i.key === key ? next : i);
          hist.unshift({id: 'h' + (hist.length + 1), seq: hist.length + 1, key, old: item.source === 'admin' ? item.value : null, new: value, by: 'dora.lane@example.com', at: iso(0)});
          return ok({item: next, change: {}});
        }
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'dora.lane@example.com', operator: true, admin, teams: [], tags: []});
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

const row = (doc, key) => doc.querySelector('#admin .adm-row[data-key="' + key + '"]');
const airow = (doc, key) => doc.querySelector('#ai-rows .adm-row[data-key="' + key + '"]');

async function theAdminTabIsHiddenForEveryoneElse() {
  //given
  const {w, doc, errors, calls} = await boot(false, '#t=settings');

  //then
  const tab = doc.getElementById('tab-settings');
  assert.ok(doc.getElementById('side-admin').hidden, 'no Admin row in the sidebar');
  assert.strictEqual(tab.getAttribute('aria-disabled'), 'true', 'the admin Settings tab is greyed out without the role');
  assert.strictEqual(tab.title, 'Admins only: needs the admin role');
  tab.click();
  assert.ok(doc.getElementById('panel-settings').hidden, 'a click does nothing');
  assert.strictEqual(doc.getElementById('panel-settings').hidden, true);
  assert.ok(!doc.getElementById('panel-anoms').hidden, 'a link to the Admin tab opens Anomalies instead');
  assert.deepStrictEqual(calls, [], 'nothing asks the admin API');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS without the admin role the sidebar has no Admin row, the admin Settings tab is greyed out with the role in its tooltip, a click or a link opens nothing, and the admin API is never called');
}

async function anAdminSeesTheSettingsByGroupAndSavesResetsAndReverts() {
  //given
  const {w, doc, errors, calls} = await boot(true, '#t=settings');

  //then
  assert.ok(!doc.getElementById('panel-settings').hidden && !doc.getElementById('tab-settings').hasAttribute('aria-disabled'), 'a link opens Admin \u00b7 General once the role is known');
  assert.deepStrictEqual([...doc.querySelectorAll('#adm-nav .tab')].map((t) => t.querySelector('.lbl').textContent), ['Settings', 'Access & sign-in', 'Workspaces', 'Features', 'Investigator', 'Changes']);
  assert.ok(doc.getElementById('side-admin').hidden && !doc.getElementById('um-admin').hidden && doc.getElementById('user-btn').getAttribute('aria-current') === 'page', 'the Admin item shows in the user menu and the user button is current');
  assert.ok(!doc.querySelector('#tabs-anomalies [data-tab="settings"]'), 'not under the anomalies menu');
  assert.ok(doc.getElementById('reco').hidden, 'no summary on the General page');
  assert.ok(doc.getElementById('live').hidden && doc.getElementById('live').parentNode.classList.contains('no-live'), 'no Activity feed on the General page');
  assert.strictEqual(doc.querySelector('#panel-settings .q'), null, 'no help icon');
  assert.deepStrictEqual([...doc.querySelectorAll('#admin .adm-grp h3')].map((h) => h.textContent), ['Page scan', 'Monitor rules', 'Small talk'], 'General keeps every group but the Investigator one');
  assert.strictEqual(doc.getElementById('count-settings').textContent, '0');
  const tips = [...row(doc, 'monitor.umami-rules').querySelectorAll('.adm-rules .adm-rule-q')];
  assert.deepStrictEqual(tips.map((q) => q.title), ['Warning when redirects fall below this share.', 'Warning when one product type falls below this share.'], 'every threshold has a ? that says what it is for');
  assert.ok(tips[0].getAttribute('aria-label').startsWith('What redirect_warn is for: ') && tips[0].tabIndex === 0);
  assert.ok(row(doc, 'pagescan.hours').textContent.includes('after restart'));
  assert.ok(row(doc, 'pagescan.hours').querySelector('.adm-save').disabled, 'Save waits for a change');

  //when
  const rules = row(doc, 'monitor.umami-rules');
  const input = rules.querySelector('input[data-rule="type_warn"]');
  input.value = '0.3';
  input.dispatchEvent(new w.Event('input'));
  rules.querySelector('.adm-save').click();
  await wait(40);

  //then
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'PUT').map((c) => c[2]), [{key: 'monitor.umami-rules', value: '{"type_warn":0.3}'}]);
  assert.ok(row(doc, 'monitor.umami-rules').textContent.includes('default 0.4'));
  assert.strictEqual(row(doc, 'monitor.umami-rules').querySelector('.adm-msg').textContent, 'Saved, live within 15 s on every pod');
  assert.strictEqual(doc.getElementById('count-settings').textContent, '1');

  //when
  doc.getElementById('tab-ai').click();
  await wait(60);

  //then
  assert.ok(airow(doc, 'investigate.daily-usd').querySelector('.tag.k-act').textContent.startsWith('Set by dora.lane'), 'the Investigator settings sit on Admin \u00b7 Investigator');
  assert.ok(airow(doc, 'investigate.daily-usd').querySelector('.adm-reset'));
  assert.strictEqual(airow(doc, 'investigate.model').querySelector('.tag.plain').textContent, 'default');
  assert.ok(!airow(doc, 'investigate.model').textContent.includes('after restart'));
  assert.ok(airow(doc, 'investigate.model').querySelector('.adm-save').disabled, 'Save waits for a change');

  //when
  const usd = airow(doc, 'investigate.daily-usd');
  usd.querySelector('.adm-in').value = '900';
  usd.querySelector('.adm-in').dispatchEvent(new w.Event('input'));
  usd.querySelector('.adm-save').click();
  await wait(30);

  //then
  assert.strictEqual(airow(doc, 'investigate.daily-usd').querySelector('.adm-msg').textContent, 'Daily limit ($) must be between 0 and 500');

  //when
  const model = airow(doc, 'investigate.model');
  model.querySelector('select').value = 'claude-sonnet-5-5';
  model.querySelector('select').dispatchEvent(new w.Event('input'));
  model.querySelector('.adm-save').click();
  await wait(40);

  //then
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'PUT').map((c) => c[2]).slice(-2), [{key: 'investigate.daily-usd', value: '900'}, {key: 'investigate.model', value: 'claude-sonnet-5-5'}]);
  assert.strictEqual(airow(doc, 'investigate.model').querySelector('.adm-msg').textContent, 'Saved, live within 15 s on every pod');
  assert.strictEqual(doc.getElementById('count-settings').textContent, '1', 'General counts only its own settings');

  //when
  airow(doc, 'investigate.daily-usd').querySelector('.adm-reset').click();
  await wait(40);

  //then
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'DELETE').map((c) => c[1]), ['/api/admin/settings?key=investigate.daily-usd']);
  assert.strictEqual(airow(doc, 'investigate.daily-usd').querySelector('.tag.plain').textContent, 'default');

  //when
  doc.getElementById('tab-changes').click();
  await wait(60);
  const revert = doc.querySelector('#changes .adm-hist tbody tr .adm-revert');
  revert.click();
  await wait(40);

  //then
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'PUT').map((c) => c[2]).slice(-1), [{key: 'investigate.daily-usd', value: '40'}], 'Revert on Changes puts back the value before that change');
  assert.strictEqual(doc.getElementById('adm-ch-msg').textContent, 'Reverted Daily limit ($)');

  //when
  doc.getElementById('tab-settings').click();
  await wait(40);
  const find = doc.getElementById('adm-find');
  find.value = 'batch';
  find.dispatchEvent(new w.Event('input'));

  //then
  assert.deepStrictEqual([...doc.querySelectorAll('#admin .adm-row')].filter((r) => !r.hidden).map((r) => r.dataset.key), ['pagescan.hours']);
  assert.deepStrictEqual([...doc.querySelectorAll('#admin .adm-grp')].filter((g) => !g.hidden).map((g) => g.dataset.group), ['Page scan']);
  doc.getElementById('tab-anoms').click();
  assert.ok(!doc.getElementById('reco').hidden, 'the summary comes back on other tabs');
  assert.ok(!doc.getElementById('live').hidden && !doc.getElementById('live').parentNode.classList.contains('no-live'), 'and so does the Activity feed');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS Admin \u00b7 General sits in its own group with no summary or help icon; an admin sees the settings by group with where each value comes from, saves with checks, edits a rule table, resets on Investigator, reverts from Changes and finds a setting');
}

async function settingsStartFoldedAndChangesShowTenAtATime() {
  //given
  const longRules = JSON.stringify({redirect_warn: 0.5, type_warn: 0.4, note: 'x'.repeat(300)});
  const many = Array.from({length: 35}, (_, i) => ({id: 'm' + i, seq: 100 - i, key: 'investigate.daily-usd', old: String(20 + i), new: String(21 + i), by: 'dora.lane@example.com', at: iso(60 + i)}));
  many[0] = {id: 'm0', seq: 100, key: 'monitor.umami-rules', old: null, new: longRules, by: 'dora.lane@example.com', at: iso(60)};
  const {w, doc, errors, calls} = await boot(true, '#t=settings', many);
  const groups = () => [...doc.querySelectorAll('#admin details.adm-grp')];

  //then
  assert.ok(groups().length >= 3 && groups().every((d) => !d.open), 'every settings group starts folded: ' + groups().map((d) => d.dataset.group + '=' + d.open).join(', '));
  assert.ok(!doc.querySelector('#admin details.adm-hist-grp'), 'Changes is not a fold of General any more');

  //when
  const rulesGrp = groups().find((d) => d.dataset.group === 'Monitor rules');
  rulesGrp.open = true;
  await wait(20);
  doc.getElementById('tab-changes').click();
  await wait(60);
  const rows = () => doc.querySelectorAll('#changes .adm-hist tbody tr');

  //then
  assert.ok(!doc.getElementById('panel-changes').hidden, 'Changes is its own admin section');
  assert.ok(/^35 changes/.test(doc.querySelector('#changes .adm-head').textContent), 'its head counts the changes');
  assert.ok(calls.some((c) => c[1] === '/api/admin/history?limit=200'), 'the page asks for the latest 200 changes');
  assert.strictEqual(rows().length, 10, 'the newest 10 show first');
  const clip = rows()[0].querySelector('code.adm-clip');
  assert.ok(clip && clip.textContent.length === 140 && clip.textContent.endsWith('\u2026') && clip.title === longRules, 'a long value is cut at 140 characters, the full text in its tooltip');
  assert.strictEqual(doc.querySelector('#changes .adm-more').textContent, 'Show 20 more');
  assert.match(doc.querySelector('#changes .adm-hist-more').textContent, /10 of 35$/);

  //when
  doc.querySelector('#changes .adm-more').click();
  await wait(20);

  //then
  assert.strictEqual(rows().length, 30, 'Show more adds 20 rows');
  assert.strictEqual(doc.activeElement, rows()[10], 'and moves the focus to the first new row');
  assert.strictEqual(doc.querySelector('#changes .adm-more').textContent, 'Show 5 more');

  //when
  doc.querySelector('#changes .adm-more').click();
  await wait(20);
  doc.getElementById('tab-settings').click();
  await wait(40);
  const find = doc.getElementById('adm-find');
  find.value = 'batch every';
  find.dispatchEvent(new w.Event('input', {bubbles: true}));
  await wait(20);
  const searched = groups().filter((d) => !d.hidden).map((d) => d.dataset.group + '=' + d.open);
  find.value = '';
  find.dispatchEvent(new w.Event('input', {bubbles: true}));
  await wait(20);

  //then
  assert.strictEqual(rows().length, 35, 'all changes listed');
  assert.ok(!doc.querySelector('#changes .adm-more'), 'no more button once everything shows');
  assert.deepStrictEqual(searched, ['Page scan=true'], 'a search opens the matching group');
  assert.deepStrictEqual(groups().map((d) => d.dataset.group + '=' + d.open).filter((x) => /^(Monitor rules|Page scan)=/.test(x)), ['Page scan=false', 'Monitor rules=true'], 'clearing it folds the group back and keeps the one the reader opened');
  assert.deepStrictEqual(errors, []);
  w.close();

  //when
  const again = await boot(true, '#t=settings', many);

  //then
  assert.ok([...again.doc.querySelectorAll('#admin details.adm-grp')].every((d) => !d.open), 'a reload folds everything again');
  again.w.close();
  console.log('PASS General starts with every group folded, Changes is its own section that lists 10 changes and 20 more per click with long values cut, a search opens and then refolds groups, and a reload folds everything again');
}

async function aChoiceWithNamedValuesShowsTheNamesAndSavesTheValue() {
  //given
  const postSev = {key: 'slack.post-sev', group: 'Slack', label: 'Post at once from', help: 'Cards at or above this severity post at once', kind: 'choice', live: true, value: 'warn', base: 'warn', default: 'warn',
    source: 'default', by: null, at: null, choices: ['crit', 'warn', 'info', 'off'], choiceLabels: {crit: 'Critical', warn: 'Warning', info: 'Info', off: 'Off'}};
  const {w, doc, errors, calls} = await boot(true, '#t=settings', null, [postSev]);
  const select = () => row(doc, 'slack.post-sev').querySelector('select');

  //then
  assert.deepStrictEqual([...select().options].map((o) => o.value + '=' + o.textContent), ['crit=Critical', 'warn=Warning', 'info=Info', 'off=Off']);
  assert.strictEqual(select().value, 'warn');

  //when
  select().value = 'crit';
  select().dispatchEvent(new w.Event('input'));
  row(doc, 'slack.post-sev').querySelector('.adm-save').click();
  await wait(40);

  //then
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'PUT').map((c) => c[2]), [{key: 'slack.post-sev', value: 'crit'}]);
  assert.strictEqual(select().selectedOptions[0].textContent, 'Critical');
  assert.deepStrictEqual(errors, []);
  w.close();

  //given
  const fromOld = Object.assign({}, postSev, {value: 'off', source: 'admin', by: 'alice@example.com', at: iso(60), oldSwitch: 'slack.crit-now'});
  const old = await boot(true, '#t=settings', null, [fromOld]);
  const oldRow = row(old.doc, 'slack.post-sev');

  //then
  const tag = oldRow.querySelector('.adm-m .tag');
  assert.strictEqual(tag.textContent, 'old switch');
  assert.ok(tag.title.startsWith('From the old Critical cards at once switch (slack.crit-now), set by ') && tag.title.endsWith('Reset or saving a value here clears it'), tag.title);
  assert.strictEqual(oldRow.querySelector('select').selectedOptions[0].textContent, 'Off');

  //when
  oldRow.querySelector('.adm-reset').click();
  await wait(40);

  //then
  assert.deepStrictEqual(old.calls.filter((c) => c[0] === 'DELETE').map((c) => c[1]), ['/api/admin/settings?key=slack.post-sev']);
  assert.deepStrictEqual(old.errors, []);
  old.w.close();

  //given
  const hist = [{id: 'h2', seq: 2, key: 'slack.crit-now', old: 'false', new: null, by: 'dora.lane@example.com', at: iso(1), legacy: true},
    {id: 'h1', seq: 1, key: 'slack.post-sev', old: 'off', new: 'info', by: 'dora.lane@example.com', at: iso(1)}];
  const changes = await boot(true, '#t=settings', hist, [postSev]);

  //when
  changes.doc.getElementById('tab-changes').click();
  await wait(60);

  //then
  const rows = [...changes.doc.querySelectorAll('#changes .adm-hist tbody tr')];
  assert.deepStrictEqual(rows.map((r) => !!r.querySelector('.adm-revert')), [false, true], 'the cleared old switch row offers no Revert, the post-sev row does');
  assert.deepStrictEqual(changes.errors, []);
  changes.w.close();
  console.log('PASS a choice setting with named values, such as Post at once from, shows Critical, Warning, Info and Off and saves crit, warn, info or off; a value from the old switch is tagged old switch and Reset clears it');
}

(async () => {
  await theAdminTabIsHiddenForEveryoneElse();
  await aChoiceWithNamedValuesShowsTheNamesAndSavesTheValue();
  await anAdminSeesTheSettingsByGroupAndSavesResetsAndReverts();
  await settingsStartFoldedAndChangesShowTenAtATime();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
