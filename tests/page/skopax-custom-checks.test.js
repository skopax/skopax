const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const EVIL = '<img src=x onerror="window.HIT=1">';

function item(over) {
  return Object.assign({id: 'redis-memory', name: 'Redis memory', enabled: true, source: 'prometheus', instance: 'main', datasource: '', query: 'redis_memory_used_bytes / redis_memory_max_bytes * 100',
    op: '>', threshold: 90, baselines: [], baselineMode: 'ratio', severity: 'warn', by: ['instance'], for: 1, title: '{instance} memory at {value} %', text: '',
    createdBy: 'alice@example.com', createdAt: iso(60), updatedBy: 'alice@example.com', updatedAt: iso(30),
    run: {state: 'failing', reason: 'Prometheus main answered HTTP 400: ' + EVIL, at: iso(2), pending: {}}}, over || {});
}

const PRESETS = [
  {id: 'couchbase', name: 'Docstore', summary: 'A Docstore cluster: ' + EVIL, source: 'prometheus', needs: 'A Prometheus instance that scrapes the Docstore exporter.',
    placeholders: [{key: 'instance', kind: 'instance', label: 'Prometheus', help: 'The Prometheus instance.', example: 'main', optional: false, max: 60},
      {key: 'cluster', kind: 'name', label: 'Cluster', help: 'A short name.', example: 'orders-prod', optional: false, max: 20},
      {key: 'selector', kind: 'matchers', label: 'Selector', help: 'Label matchers.', example: 'job="docstore"', default: 'job=~".*docstore.*"', optional: false, max: 100}],
    checks: [{key: 'down', name: 'Docstore <cluster> node down', severity: 'crit'}, {key: 'n1ql-queue', name: 'Docstore <cluster> N1QL queue', severity: 'warn'}]},
  {id: 'managed-redis', name: 'Managed Redis', summary: 'Redis you cannot reach directly.', source: 'grafana', needs: 'A Grafana datasource with redis_exporter series.',
    placeholders: [{key: 'instance', kind: 'instance', label: 'Grafana', help: '', example: 'ops', optional: false, max: 60},
      {key: 'datasource', kind: 'datasource', label: 'Datasource', help: '', example: 'cloud-metrics', optional: false, max: 200},
      {key: 'name', kind: 'name', label: 'Name', help: '', example: 'cache-prod', optional: false, max: 20},
      {key: 'selector', kind: 'matchers', label: 'Selector', help: '', example: 'env="prod"', default: '', optional: true, max: 80}],
    checks: [{key: 'down', name: 'Redis <name> down', severity: 'crit'}]}];

function rendered(values) {
  return [{id: 'cb-' + values.cluster + '-down', name: 'Docstore ' + values.cluster + ' node down', enabled: true, source: 'prometheus', instance: values.instance, datasource: '',
    query: 'max by (instance) (up{' + values.selector + '})', op: '<', threshold: 0.5, baselines: [], baselineMode: 'ratio', severity: 'crit', by: ['instance'], for: 1, title: '{instance} down', text: ''},
  {id: 'cb-' + values.cluster + '-n1ql-queue', name: 'Docstore ' + values.cluster + ' N1QL queue', enabled: true, source: 'prometheus', instance: values.instance, datasource: '',
    query: 'max(max_over_time(n1ql_queued_requests{' + values.selector + '}[60m]))', op: '>', threshold: 20, baselines: [], baselineMode: 'ratio', severity: 'warn', by: [], for: 1, title: '', text: ''}];
}

function listed(items) {
  return {items, presets: [{id: 'couchbase', name: 'Docstore', nameKey: 'cluster'}, {id: 'managed-redis', name: 'Managed Redis', nameKey: 'name'}], sources: {prometheus: ['main'], grafana: ['ops']}, limits: {checks: 50, series: 500, cards: 20, querySeconds: 20, for: 12, split: 5, name: 80, query: 2000, title: 200, text: 1000, periodSeconds: 300},
    choices: {source: ['prometheus', 'grafana'], op: ['>', '>=', '<', '<=', '==', '!='], baselines: ['1d', '1w'], baselineMode: ['ratio', 'difference'], severity: ['info', 'warn', 'crit']}};
}

async function boot(opts) {
  opts = opts || {};
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: opts.anomalies || [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const admin = opts.admin !== false;
  const srv = {items: opts.items || [item()], slow: !!opts.slow};
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/' + (opts.hash || '#t=integrations'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p0, o) => {
        const p = String(p0), method = (o && o.method) || 'GET', body = o && o.body ? JSON.parse(o.body) : null;
        const ok = (b, status) => ({ok: true, status: status || 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        const bad = (status, detail) => ({ok: false, status, json: async () => ({detail})});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', userRole: admin ? 'admin' : 'viewer', operator: admin, admin, teams: [], tags: []});
        if (p.startsWith('/api/admin')) {
          calls.push([method, p, body]);
          if (!admin) return bad(403, 'Admins only');
          if (p === '/api/admin/custom-checks/preview') {
            if (body.threshold === 'abc') return bad(422, 'the threshold must be a number');
            if (srv.slow) await new Promise((r) => setTimeout(r, 40));
            if (body.id && /-down$/.test(body.id)) return ok({ok: true, reason: '', series: 3, read: 3, skipped: 0, noBaseline: 0, firing: 1, for: 1,
              cards: [{key: 'k1', sev: 'crit', title: EVIL + ' down', text: 'down', value: 0, d1: null, d7: null, labels: {}, links: [], more: 0}]});
            if (body.id && body.id.indexOf('-n1ql-queue') > 0) return ok({ok: false, reason: 'Prometheus main answered HTTP 400: ' + EVIL, cards: [], series: 0, read: 0, skipped: 0, noBaseline: 0, firing: 0, for: 1});
            return ok({ok: true, reason: '', series: 3, read: 3, skipped: 0, noBaseline: 1, firing: 2, for: body.for,
              cards: [{key: 'k1', sev: 'warn', title: EVIL + ' memory at 95 %', text: 'Redis ' + EVIL + ' is full. Redis memory: 95 > 90.', value: 95, d1: null, d7: null, labels: {instance: EVIL},
                links: [{label: 'Query in Prometheus', url: 'https://prom.example.com/query?g0.expr=up'}, {label: 'bad', url: 'javascript:alert(1)'}], more: 0},
                {key: 'k2|more', sev: 'warn', title: 'Redis memory', text: '1 more label sets', value: 1, d1: null, d7: null, labels: {}, links: [], more: 1}]});
          }
          if (p === '/api/admin/custom-checks/presets' && method === 'GET') return ok({items: PRESETS});
          if (p === '/api/admin/custom-checks/presets/couchbase/render') {
            if (/\}/.test(body.values.selector)) return bad(422, 'Selector must be label matchers such as ' + EVIL);
            return ok({values: body.values, checks: rendered(body.values)});
          }
          if (p === '/api/admin/custom-checks/presets/couchbase' && method === 'POST') {
            if (srv.items.some((x) => x.preset === 'couchbase' && x.presetValues.cluster === body.values.cluster)) return bad(409, 'the preset Docstore is already added with these values: ' + EVIL);
            const made = rendered(body.values).map((d) => Object.assign(d, {preset: 'couchbase', presetValues: body.values, createdBy: 'alice@example.com', createdAt: iso(0)}));
            srv.items = srv.items.concat(made);
            return ok({items: made}, 201);
          }
          if (p === '/api/admin/custom-checks' && method === 'GET') return ok(Object.assign(listed(srv.items), opts.sources ? {sources: opts.sources} : {}));
          if (p === '/api/admin/custom-checks' && method === 'POST') {
            const made = Object.assign({}, body, {id: body.id || 'cache-hits', createdBy: 'alice@example.com', createdAt: iso(0), updatedBy: 'alice@example.com', updatedAt: iso(0)});
            srv.items = srv.items.concat([made]);
            return ok(made, 201);
          }
          if (p.startsWith('/api/admin/custom-checks/') && method === 'PUT') {
            const id = p.split('/').pop();
            srv.items = srv.items.map((x) => x.id === id ? Object.assign({}, body, {id, run: x.run}) : x);
            return ok(srv.items.filter((x) => x.id === id)[0]);
          }
          if (p.startsWith('/api/admin/custom-checks/') && method === 'DELETE') {
            const id = p.split('/').pop();
            srv.items = srv.items.filter((x) => x.id !== id);
            return {ok: true, status: 204, json: async () => null};
          }
          if (p === '/api/admin/notices') return ok({items: []});
          if (p === '/api/admin/plugins') return ok({edition: 'community', secretsKey: 'env', plugins: []});
          if (p === '/api/admin/settings') return ok({items: [], rev: 1, refreshSeconds: 15});
          if (p === '/api/admin/features') return ok({edition: 'community', items: []});
          if (p === '/api/admin/users') return ok([]);
          return bad(404, 'none');
        }
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
      };
    }});
  await wait(opts.settle || 400);
  return {w: dom.window, doc: dom.window.document, errors, calls, srv};
}

const box = (doc) => doc.getElementById('pqc');
const field = (doc, name) => box(doc).querySelector('.pqc-form [name="' + name + '"]');
const type = (w, el, value) => { el.value = value; el.dispatchEvent(new w.Event('input', {bubbles: true})); el.dispatchEvent(new w.Event('change', {bubbles: true})); };

async function theIntegrationsPageListsTheCustomChecksWithTheirStateAsText() {
  //given
  const {w, doc, errors, calls} = await boot();

  //when
  const rows = [...box(doc).querySelectorAll('.pqc-row')];

  //then
  assert.ok(calls.some((c) => c[0] === 'GET' && c[1] === '/api/admin/custom-checks'), 'the list loads with the Integrations page');
  assert.strictEqual(box(doc).querySelector('h3').textContent, 'Custom checks');
  assert.strictEqual(rows.length, 1);
  assert.strictEqual(rows[0].querySelector('.pqc-n').textContent, 'Redis memory');
  assert.match(rows[0].textContent, /prometheus main/);
  assert.match(rows[0].querySelector('.pqc-state').textContent, /failing/);
  assert.ok(rows[0].textContent.indexOf(EVIL) >= 0, 'the reason is shown as text');
  assert.strictEqual(box(doc).querySelectorAll('img').length, 0, 'and never as markup');
  assert.strictEqual(w.HIT, undefined);
  assert.deepStrictEqual(errors, []);
  console.log('PASS the integrations page lists the custom checks with their state as text');
}

async function anAdminFillsInANewCheckPreviewsItAndSavesIt() {
  //given
  const {w, doc, errors, calls} = await boot({items: []});
  box(doc).querySelector('.pqc-add').click();
  await wait(20);

  //when
  type(w, field(doc, 'name'), 'Cache hits');
  type(w, field(doc, 'source'), 'grafana');
  const datasourceShown = !field(doc, 'datasource').closest('.pqc-f').hidden;
  type(w, field(doc, 'instance'), 'ops');
  type(w, field(doc, 'datasource'), 'cloud-metrics');
  type(w, field(doc, 'query'), 'sum by (instance) (rate(redis_keyspace_misses_total[5m]))');
  type(w, field(doc, 'op'), '>=');
  type(w, field(doc, 'threshold'), '0.5');
  field(doc, 'baseline-1d').checked = true;
  type(w, field(doc, 'baselineMode'), 'difference');
  type(w, field(doc, 'severity'), 'crit');
  type(w, field(doc, 'by'), 'instance, job');
  type(w, field(doc, 'for'), '3');
  type(w, field(doc, 'title'), '{instance} misses');
  type(w, field(doc, 'text'), 'Misses at {value}');
  box(doc).querySelector('.pqc-preview-btn').click();
  await wait(60);
  const preview = box(doc).querySelector('.pqc-preview');
  const shown = {cards: [...preview.querySelectorAll('.pqc-card')].map((c) => c.querySelector('.pqc-t').textContent), links: [...preview.querySelectorAll('a')].map((a) => a.getAttribute('href')),
    summary: preview.querySelector('.pqc-sum').textContent, imgs: preview.querySelectorAll('img').length};
  box(doc).querySelector('.pqc-save').click();
  await wait(60);

  //then
  const sent = calls.filter((c) => c[0] === 'POST').map((c) => [c[1], c[2]]);
  const body = {name: 'Cache hits', enabled: true, source: 'grafana', instance: 'ops', datasource: 'cloud-metrics', query: 'sum by (instance) (rate(redis_keyspace_misses_total[5m]))', op: '>=', threshold: '0.5',
    baselines: ['1d'], baselineMode: 'difference', severity: 'crit', by: ['instance', 'job'], for: 3, title: '{instance} misses', text: 'Misses at {value}'};
  assert.ok(datasourceShown, 'a Grafana source asks for the datasource');
  assert.deepStrictEqual(sent, [['/api/admin/custom-checks/preview', body], ['/api/admin/custom-checks', body]]);
  assert.deepStrictEqual(shown.cards, [EVIL + ' memory at 95 %', 'Redis memory']);
  assert.deepStrictEqual(shown.links, ['https://prom.example.com/query?g0.expr=up'], 'only an https link is a link');
  assert.match(shown.summary, /3 series, 2 match, 2 cards/);
  assert.match(shown.summary, /after 3 passes in a row/);
  assert.match(shown.summary, /1 group had no baseline value/);
  assert.strictEqual(shown.imgs, 0);
  assert.strictEqual(w.HIT, undefined);
  assert.strictEqual(box(doc).querySelector('.pqc-form'), null, 'the editor closes after a save');
  assert.deepStrictEqual([...box(doc).querySelectorAll('.pqc-row .pqc-n')].map((n) => n.textContent), ['Cache hits']);
  assert.match(box(doc).querySelector('.adm-msg').textContent, /Saved/);
  assert.deepStrictEqual(errors, []);
  console.log('PASS an admin fills in a new check, previews it and saves it');
}

async function aRefusedPreviewShowsTheReasonAndAnEditSavesWithPut() {
  //given
  const {w, doc, errors, calls} = await boot();
  box(doc).querySelector('.pqc-row .pqc-edit').click();
  await wait(20);
  const filled = [field(doc, 'name').value, field(doc, 'query').value, field(doc, 'by').value, field(doc, 'id')];

  //when
  type(w, field(doc, 'threshold'), 'abc');
  box(doc).querySelector('.pqc-preview-btn').click();
  await wait(60);
  const said = box(doc).querySelector('.pqc-preview').textContent;
  type(w, field(doc, 'threshold'), '80');
  type(w, field(doc, 'op'), '<');
  type(w, field(doc, 'enabled'), 'off');
  box(doc).querySelector('.pqc-save').click();
  await wait(60);

  //then
  assert.deepStrictEqual(filled, ['Redis memory', item().query, 'instance', null], 'the id of a saved check cannot change');
  assert.match(said, /threshold must be a number/);
  const put = calls.filter((c) => c[0] === 'PUT');
  assert.strictEqual(put.length, 1);
  assert.strictEqual(put[0][1], '/api/admin/custom-checks/redis-memory');
  assert.strictEqual(put[0][2].op, '<');
  assert.strictEqual(put[0][2].threshold, '80');
  assert.strictEqual(put[0][2].enabled, false);
  assert.deepStrictEqual(errors, []);
  console.log('PASS a refused preview shows the reason and an edit saves with PUT');
}

async function deleteAsksOnceMoreBeforeItDeletes() {
  //given
  const {doc, errors, calls} = await boot();
  box(doc).querySelector('.pqc-row .pqc-edit').click();
  await wait(20);
  const del = () => box(doc).querySelector('.pqc-delete');

  //when
  del().click();
  await wait(20);
  const asked = [del().textContent, calls.filter((c) => c[0] === 'DELETE').length];
  del().click();
  await wait(60);

  //then
  assert.deepStrictEqual(asked, ['Delete for good?', 0]);
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'DELETE').map((c) => c[1]), ['/api/admin/custom-checks/redis-memory']);
  assert.strictEqual(box(doc).querySelectorAll('.pqc-row').length, 0);
  assert.deepStrictEqual(errors, []);
  console.log('PASS delete asks once more before it deletes');
}

async function aBoardCardFromACustomCheckShowsItsLabelValuesAsText() {
  //given
  const card = {key: 'custom-checks|redis-memory|0123456789abcdef', src: 'custom-checks', sig: 'custom-check', ns: '-', svc: 'Redis memory', sub: '', sev: 'warn', now: 95, d1: null, d7: null,
    unit: '', label: EVIL + ' memory at 95 %', what: 'Redis ' + EVIL + ' is full. Redis memory: 95 > 90.', short: 'WARN ' + EVIL + ' memory at 95 % 95', status: 'open', firstSeen: iso(10), lastSeen: iso(1),
    ranKey: 'custom-checks:redis-memory', checkId: 'redis-memory', checkLabels: {instance: EVIL}, links: [{label: 'Query in Prometheus', url: 'https://prom.example.com/query?g0.expr=up'}]};

  //when
  const {w, doc, errors} = await boot({hash: '#t=anoms', anomalies: [card]});
  const board = doc.getElementById('panel-anoms');

  //then
  assert.ok(board.textContent.indexOf(EVIL + ' memory at 95 %') >= 0, 'the card shows its title');
  assert.strictEqual(board.querySelectorAll('img').length, 0, 'a label value is never markup on the board');
  assert.strictEqual(w.HIT, undefined);
  assert.deepStrictEqual(errors, []);
  console.log('PASS a board card from a custom check shows its label values as text');
}

async function anOpenEditorKeepsWhatWasTypedWhenTheIntegrationsPageLoadsAgain() {
  //given
  const {w, doc, errors} = await boot({items: []});
  box(doc).querySelector('.pqc-add').click();
  await wait(20);
  type(w, field(doc, 'query'), 'up == 0');

  //when
  w.adminShow('integrations');
  await wait(80);

  //then
  assert.strictEqual(field(doc, 'query').value, 'up == 0');
  assert.deepStrictEqual(errors, []);
  console.log('PASS an open editor keeps what was typed when the integrations page loads again');
}

async function aViewerNeverLoadsTheChecks() {
  //given
  const {doc, calls} = await boot({admin: false});

  //then
  assert.deepStrictEqual(calls.filter((c) => c[1].startsWith('/api/admin/custom-checks')), []);
  assert.strictEqual(box(doc).textContent, '');
  console.log('PASS a viewer never loads the checks');
}

async function theStylesUseThemeColoursAndStackAtPhoneWidth() {
  //given
  const html = fs.readFileSync(TEMPLATE, 'utf8');
  const css = html.split('<style>')[1].split('</style>')[0];
  const rules = css.split('\n').filter((l) => l.indexOf('.pqc') >= 0);

  //then
  assert.ok(rules.length > 5, 'the custom checks have their own rules');
  assert.ok(rules.every((r) => !/#[0-9a-fA-F]{3,6}\b|rgba?\(/.test(r)), 'colours come from the theme variables, so both themes work');
  assert.ok(rules.some((r) => /@media \(max-width: 768px\).*\.pqc-form \{ grid-template-columns: minmax\(0, 1fr\); \}/.test(r)), 'the editor is one column at phone width');
  console.log('PASS the styles use theme colours and stack at phone width');
}

const pick = (doc) => box(doc).querySelector('.pqc-picker');
const pfield = (doc, name) => box(doc).querySelector('.pqc-preset-form [name="' + name + '"]');

async function anAdminPicksAPresetFillsItsPlaceholdersPreviewsEachCheckAndAddsIt() {
  //given
  const {w, doc, errors, calls} = await boot({items: []});
  box(doc).querySelector('.pqc-from-preset').click();
  await wait(60);
  const offered = [...pick(doc).querySelectorAll('.pqc-preset')].map((li) => [li.querySelector('.pqc-n').textContent, li.querySelector('.pqc-src').textContent]);
  const summary = pick(doc).querySelector('.pqc-preset').textContent;
  pick(doc).querySelector('.pqc-preset .pqc-choose').click();
  await wait(20);
  const form = box(doc).querySelector('.pqc-preset-form');
  const shown = {instances: [...pfield(doc, 'instance').options].map((o) => o.value), selector: pfield(doc, 'selector').value, hint: pfield(doc, 'cluster').placeholder,
    checks: [...form.querySelectorAll('.pqc-preset-checks .pqc-n')].map((n) => n.textContent)};

  //when
  type(w, pfield(doc, 'cluster'), 'orders-prod');
  type(w, pfield(doc, 'selector'), 'job="docstore"');
  form.querySelector('.pqc-preset-preview').click();
  await wait(120);
  const after = [...form.querySelectorAll('.pqc-preset-checks > li')].map((li) => [li.querySelector('.pqc-n').textContent, li.querySelector('.pqc-sum').textContent]);
  const cards = [...form.querySelectorAll('.pqc-card .pqc-t')].map((t) => t.textContent);
  form.querySelector('.pqc-preset-add').click();
  await wait(80);

  //then
  const values = {instance: 'main', cluster: 'orders-prod', selector: 'job="docstore"'};
  assert.deepStrictEqual(offered, [['Docstore', 'Prometheus'], ['Managed Redis', 'Grafana datasource']]);
  assert.ok(summary.indexOf(EVIL) >= 0 && /scrapes the Docstore exporter/.test(summary), 'the picker says what it watches and what it needs, as text');
  assert.deepStrictEqual(shown, {instances: ['main'], selector: 'job=~".*docstore.*"', hint: 'orders-prod', checks: ['Docstore <cluster> node down', 'Docstore <cluster> N1QL queue']});
  const posts = calls.filter((c) => c[0] === 'POST').map((c) => [c[1], c[2]]);
  assert.deepStrictEqual(posts.map((c) => c[0]), ['/api/admin/custom-checks/presets/couchbase/render', '/api/admin/custom-checks/preview', '/api/admin/custom-checks/preview',
    '/api/admin/custom-checks/presets/couchbase']);
  assert.deepStrictEqual(posts[0][1], {values});
  assert.deepStrictEqual([posts[1][1], posts[2][1]], rendered(values), 'each check is previewed with the existing Preview');
  assert.deepStrictEqual(posts[3][1], {values});
  assert.deepStrictEqual(after.map((a) => a[0]), ['Docstore orders-prod node down', 'Docstore orders-prod N1QL queue']);
  assert.match(after[0][1], /^3 series, 1 match, 1 card\. Nothing is saved\.$/);
  assert.match(after[1][1], /would fail: Prometheus main answered HTTP 400/);
  assert.ok(after[1][1].indexOf(EVIL) >= 0);
  assert.deepStrictEqual(cards, [EVIL + ' down']);
  assert.strictEqual(box(doc).querySelector('.pqc-picker'), null, 'the picker closes after the add');
  assert.match(box(doc).querySelector('.adm-msg').textContent, /Added 2 checks from Docstore/);
  assert.deepStrictEqual([...box(doc).querySelectorAll('.pqc-group .pqc-chip')].map((c) => c.textContent), ['Docstore']);
  assert.strictEqual(box(doc).querySelectorAll('img').length, 0);
  assert.strictEqual(w.HIT, undefined);
  assert.deepStrictEqual(errors, []);
  console.log('PASS an admin picks a preset, fills its placeholders, previews each check and adds it');
}

async function refusedPlaceholdersAndATwiceAddedPresetShowTheReasonAsText() {
  //given
  const values = {instance: 'main', cluster: 'orders-prod', selector: 'job="a"'};
  const {w, doc, errors, calls} = await boot({items: rendered(values).map((d) => Object.assign(d, {preset: 'couchbase', presetValues: values}))});
  box(doc).querySelector('.pqc-from-preset').click();
  await wait(60);
  pick(doc).querySelector('.pqc-preset .pqc-choose').click();
  await wait(20);
  const form = () => box(doc).querySelector('.pqc-preset-form');

  //when
  type(w, pfield(doc, 'cluster'), 'orders-prod');
  type(w, pfield(doc, 'selector'), 'job="a"} or up{x="1"');
  form().querySelector('.pqc-preset-preview').click();
  await wait(60);
  const bad = form().querySelector('.adm-msg').textContent;
  const previews = calls.filter((c) => c[1] === '/api/admin/custom-checks/preview').length;
  type(w, pfield(doc, 'selector'), 'job="a"');
  w.adminShow('integrations');
  await wait(80);
  const kept = pfield(doc, 'selector').value;
  form().querySelector('.pqc-preset-add').click();
  await wait(60);
  const twice = form().querySelector('.adm-msg').textContent;

  //then
  assert.match(bad, /Selector must be label matchers/);
  assert.ok(bad.indexOf(EVIL) >= 0 && twice.indexOf(EVIL) >= 0, 'a refusal is shown as text');
  assert.strictEqual(previews, 0, 'nothing is previewed when the values are refused');
  assert.strictEqual(kept, 'job="a"', 'the placeholder form keeps what was typed when the page loads again');
  assert.match(twice, /already added with these values/);
  assert.strictEqual(box(doc).querySelectorAll('img').length, 0);
  assert.strictEqual(w.HIT, undefined);
  assert.deepStrictEqual(errors, []);
  console.log('PASS refused placeholders and a twice added preset show the reason as text');
}

async function theListGroupsPresetChecksUnderAChipAndTheyStayEditable() {
  //given
  const a = {instance: 'main', cluster: 'orders-prod', selector: 'job="a"'}, b = {instance: 'main', cluster: 'search-prod', selector: 'job="b"'};
  const tag = (values) => rendered(values).map((d) => Object.assign(d, {preset: 'couchbase', presetValues: values, run: null}));
  const {doc, errors, calls} = await boot({items: [tag(a)[0], item(), tag(b)[0], tag(a)[1], Object.assign(item({id: 'gone', name: 'Old preset'}), {preset: 'removed', presetValues: {x: 'y'}})]});

  //when
  const parts = [];
  [...box(doc).querySelectorAll('.pqc-list > li')].forEach((li) => {
    if (!li.classList.contains('pqc-group')) { parts.push(li.querySelector('.pqc-n').textContent); return; }
    parts.push(('group ' + li.querySelector('.pqc-chip').textContent + ' ' + li.querySelector('.pqc-gname').textContent).trim());
    li.querySelectorAll('.pqc-grows .pqc-n').forEach((n) => parts.push(n.textContent));
  });
  box(doc).querySelectorAll('.pqc-row .pqc-edit')[1].click();
  await wait(20);
  box(doc).querySelector('.pqc-form [name="threshold"]').value = '0.4';
  box(doc).querySelector('.pqc-save').click();
  await wait(60);

  //then
  assert.deepStrictEqual(parts, ['Redis memory', 'group Docstore orders-prod', 'node down', 'N1QL queue', 'group Docstore search-prod',
    'node down', 'group removed', 'Old preset']);
  const put = calls.filter((c) => c[0] === 'PUT');
  assert.deepStrictEqual(put.map((c) => c[1]), ['/api/admin/custom-checks/cb-orders-prod-down']);
  assert.strictEqual(put[0][2].threshold, '0.4');
  assert.deepStrictEqual(errors, []);
  console.log('PASS the list groups preset checks under a chip and they stay editable');
}

async function theAddFromPresetButtonIsOffAtTheCap() {
  //given
  const many = Array.from({length: 50}, (_, n) => item({id: 'c-' + n, name: 'C ' + n}));

  //when
  const {doc, errors} = await boot({items: many});

  //then
  assert.strictEqual(box(doc).querySelector('.pqc-from-preset').disabled, true);
  assert.deepStrictEqual(errors, []);
  console.log('PASS the add from preset button is off at the cap');
}

async function theKeyboardPathMovesFocusIntoThePickerAndBackOut() {
  //given
  const {w, doc, errors} = await boot({items: []});
  const from = box(doc).querySelector('.pqc-from-preset');
  from.focus();
  from.click();
  await wait(60);
  const opened = doc.activeElement;

  //when
  pick(doc).querySelector('.pqc-preset .pqc-choose').click();
  await wait(20);
  const chosen = doc.activeElement, first = pfield(doc, 'instance');
  box(doc).querySelector('.pqc-preset-form .pqc-back').click();
  await wait(20);
  const back = doc.activeElement;
  pick(doc).dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  await wait(20);

  //then
  assert.strictEqual(opened.tagName, 'H4');
  assert.strictEqual(opened.textContent, 'Add from preset');
  assert.ok(first && chosen === first, 'choosing a preset moves focus to its first field');
  assert.strictEqual(back.tagName, 'H4');
  assert.strictEqual(pick(doc), null, 'Escape closes the picker');
  assert.strictEqual(doc.activeElement, box(doc).querySelector('.pqc-from-preset'), 'and focus returns to the button that opened it');
  assert.deepStrictEqual(errors, []);
  console.log('PASS the keyboard path moves focus into the picker and back out');
}

async function cancelStopsARunningPreviewAndASourceWithoutInstancesSaysSo() {
  //given
  const {w, doc, errors, calls} = await boot({items: [], slow: true});
  box(doc).querySelector('.pqc-from-preset').click();
  await wait(60);
  pick(doc).querySelector('.pqc-preset .pqc-choose').click();
  await wait(20);
  type(w, pfield(doc, 'cluster'), 'orders-prod');

  //when
  box(doc).querySelector('.pqc-preset-preview').click();
  await wait(20);
  box(doc).querySelector('.pqc-preset-form .pqc-cancel').click();
  await wait(150);
  const previews = calls.filter((c) => c[1] === '/api/admin/custom-checks/preview').length;
  const none = await boot({items: [], sources: {prometheus: [], grafana: []}});
  none.doc.querySelector('#pqc .pqc-from-preset').click();
  await wait(60);
  none.doc.querySelector('#pqc .pqc-picker .pqc-preset .pqc-choose').click();
  await wait(20);
  const form = none.doc.querySelector('#pqc .pqc-preset-form');

  //then
  assert.strictEqual(previews, 1, 'the preview stops at the check that was running when Cancel was pressed');
  assert.strictEqual(box(doc).querySelector('.pqc-picker'), null);
  assert.match(form.querySelector('.pqc-noinst').textContent, /No Prometheus instance is set up yet/);
  assert.strictEqual(form.querySelector('.pqc-preset-preview').disabled, true);
  assert.strictEqual(form.querySelector('.pqc-preset-add').disabled, true);
  assert.deepStrictEqual(errors.concat(none.errors), []);
  console.log('PASS cancel stops a running preview and a source without instances says so');
}

const tagged = (values, runs) => rendered(values).map((d, i) => Object.assign(d, {preset: 'couchbase', presetValues: values, run: runs ? runs[i] : {state: 'ok', series: 2, cards: 0, at: iso(1)}}));
const OKV = {instance: 'main', cluster: 'orders-prod', selector: 'job="a"'};
const head = (doc) => box(doc).querySelector('.pqc-ghead');
const rowsOf = (doc) => box(doc).querySelector('.pqc-grows');

async function aPresetGroupIsFoldedWhenAllChecksAreOkAndItsHeaderSummarisesIt() {
  //given
  const {doc, errors} = await boot({items: tagged(OKV)});

  //when
  const h = head(doc);

  //then
  assert.strictEqual(h.tagName, 'BUTTON');
  assert.strictEqual(h.getAttribute('aria-expanded'), 'false');
  assert.strictEqual(h.getAttribute('aria-controls'), rowsOf(doc).id);
  assert.strictEqual(rowsOf(doc).hidden, true);
  assert.strictEqual(h.querySelector('.pqc-chip').textContent, 'Docstore');
  assert.strictEqual(h.querySelector('.pqc-gname').textContent, 'orders-prod');
  assert.strictEqual(h.querySelector('.pqc-gcount').textContent, '2 checks');
  assert.strictEqual(h.querySelector('.pqc-gsum').textContent, 'all ok');
  assert.strictEqual(h.querySelector('.pqc-gcards'), null);
  assert.deepStrictEqual(errors, []);
  const caretCss = fs.readFileSync(TEMPLATE, 'utf8');
  assert.ok(/\.pqc-caret \{[^}]*width: 7px;[^}]*border-right: 2px solid[^}]*rotate\(-45deg\)/.test(caretCss), 'the fold arrow is a visible chevron pointing right');
  assert.ok(/\.pqc-ghead\[aria-expanded="true"\] \.pqc-caret \{[^}]*rotate\(45deg\)/.test(caretCss), 'and points down when open');
  console.log('PASS a preset group is folded when all checks are ok and its header summarises it');
}

async function aGroupOpensByItselfOnAFailingCheckOrAnOpenCard() {
  //given
  const failing = boot({items: tagged(OKV, [{state: 'failing', reason: 'HTTP 400', at: iso(1), pending: {}}, {state: 'ok', series: 1, cards: 0, at: iso(1)}])});
  const carded = boot({items: tagged(OKV, [{state: 'ok', series: 1, cards: 1, at: iso(1)}, {state: 'ok', series: 1, cards: 0, at: iso(1)}])});
  const idle = boot({items: tagged(OKV, [null, null])});

  //when
  const [a, b, c] = [await failing, await carded, await idle];

  //then
  assert.strictEqual(head(a.doc).getAttribute('aria-expanded'), 'true');
  assert.strictEqual(rowsOf(a.doc).hidden, false);
  assert.strictEqual(head(a.doc).querySelector('.pqc-gsum').textContent, '1 failing');
  assert.strictEqual(head(b.doc).getAttribute('aria-expanded'), 'true');
  assert.strictEqual(head(b.doc).querySelector('.pqc-gsum').textContent, 'all ok');
  assert.strictEqual(head(b.doc).querySelector('.pqc-gcards').textContent, '1 card');
  assert.deepStrictEqual([head(a.doc).querySelector('.pqc-gsum').classList.contains('bad'), head(b.doc).querySelector('.pqc-gsum').classList.contains('bad')], [true, false], 'only a failing check colours the summary; an open card colours only its own count');
  assert.strictEqual(head(c.doc).getAttribute('aria-expanded'), 'false');
  assert.strictEqual(head(c.doc).querySelector('.pqc-gsum').textContent, '2 not run yet');
  assert.deepStrictEqual(a.errors.concat(b.errors, c.errors), []);
  console.log('PASS a group opens by itself on a failing check or an open card');
}

async function aToggleWinsOverTheAutoRuleAndIsRememberedAcrossRerenders() {
  //given
  const {w, doc, errors} = await boot({items: tagged(OKV).concat(tagged({instance: 'main', cluster: 'search-prod', selector: 'job="b"'}, [{state: 'failing', reason: 'x', at: iso(1), pending: {}}, {state: 'ok', series: 1, cards: 0, at: iso(1)}]))});
  const heads = () => [...box(doc).querySelectorAll('.pqc-ghead')];
  const before = heads().map((h) => h.getAttribute('aria-expanded'));

  //when
  heads()[0].click();
  heads()[1].click();
  const toggled = heads().map((h) => h.getAttribute('aria-expanded'));
  w.adminShow('integrations');
  await wait(80);
  const kept = heads().map((h) => h.getAttribute('aria-expanded'));
  const hiddenAfter = [...box(doc).querySelectorAll('.pqc-grows')].map((u) => u.hidden);

  //then
  assert.deepStrictEqual(before, ['false', 'true']);
  assert.deepStrictEqual(toggled, ['true', 'false']);
  assert.deepStrictEqual(kept, ['true', 'false'], 'the choice survives a re-render and beats the auto rule');
  assert.deepStrictEqual(hiddenAfter, [false, true]);
  assert.deepStrictEqual(errors, []);
  console.log('PASS a toggle wins over the auto rule and is remembered across re-renders');
}

async function theRepeatedPrefixIsDroppedButTheFullNameStaysInTitleAndAccessibleNameAndNeverEmpty() {
  //given
  const lone = Object.assign(tagged({instance: 'main', cluster: 'solo', selector: 'x'})[0], {name: 'Docstore solo'});
  const same = tagged({instance: 'main', cluster: 'twin', selector: 'y'}).map((d) => Object.assign(d, {name: 'Docstore twin'}));
  const {doc, errors} = await boot({items: tagged(OKV).concat([lone], same)});

  //when
  const groups = [...box(doc).querySelectorAll('.pqc-group')];
  const first = groups[0].querySelector('.pqc-row');
  const names = groups.map((g) => [...g.querySelectorAll('.pqc-n')].map((n) => n.textContent));

  //then
  assert.deepStrictEqual(names, [['node down', 'N1QL queue'], ['Docstore solo'], ['Docstore twin', 'Docstore twin']]);
  assert.strictEqual(first.querySelector('.pqc-n').title, 'Docstore orders-prod node down');
  assert.strictEqual(first.querySelector('.pqc-edit').getAttribute('aria-label'), 'Edit Docstore orders-prod node down');
  assert.strictEqual(first.querySelector('.pqc-sr').textContent, 'Docstore orders-prod ');
  assert.strictEqual(groups[1].querySelector('.pqc-n').title, '');
  assert.deepStrictEqual(errors, []);
  console.log('PASS the repeated prefix is dropped, the full name stays in title and accessible name, never empty');
}

async function handWrittenChecksStayFlatAndNoEmptyGapElementSitsBeforeTheList() {
  //given
  const {doc, errors} = await boot({items: [item()]});
  const css = fs.readFileSync(TEMPLATE, 'utf8').split('<style>')[1].split('</style>')[0];

  //when
  const kids = [...box(doc).children].map((c) => c.tagName + '.' + c.className);
  const row = box(doc).querySelector('.pqc-list > .pqc-row');

  //then
  assert.ok(row && row.querySelector('.pqc-n').textContent === 'Redis memory' && !row.querySelector('.pqc-sr'));
  assert.strictEqual(box(doc).querySelector('.pqc-group'), null);
  assert.ok(kids.indexOf('UL.pqc-list') === kids.length - 1);
  assert.ok(/\.pqc-box \{[^}]*gap: 0;/.test(css), 'the grid gap no longer pads the section');
  assert.ok(/\.pqc-box > \.adm-msg:empty \{ margin: 0; \}/.test(css), 'the empty status line takes no room');
  assert.ok(/\.pqc-row \{[^}]*padding: 3px 14px; min-height: 32px;/.test(css), 'rows are about 32 px high');
  assert.deepStrictEqual(errors, []);
  console.log('PASS hand-written checks stay flat and no empty gap element sits before the list');
}

(async () => {
  for (const t of [aPresetGroupIsFoldedWhenAllChecksAreOkAndItsHeaderSummarisesIt, aGroupOpensByItselfOnAFailingCheckOrAnOpenCard, aToggleWinsOverTheAutoRuleAndIsRememberedAcrossRerenders,
    theRepeatedPrefixIsDroppedButTheFullNameStaysInTitleAndAccessibleNameAndNeverEmpty, handWrittenChecksStayFlatAndNoEmptyGapElementSitsBeforeTheList, theKeyboardPathMovesFocusIntoThePickerAndBackOut, cancelStopsARunningPreviewAndASourceWithoutInstancesSaysSo, anAdminPicksAPresetFillsItsPlaceholdersPreviewsEachCheckAndAddsIt, refusedPlaceholdersAndATwiceAddedPresetShowTheReasonAsText,
    theListGroupsPresetChecksUnderAChipAndTheyStayEditable, theAddFromPresetButtonIsOffAtTheCap, theIntegrationsPageListsTheCustomChecksWithTheirStateAsText, anAdminFillsInANewCheckPreviewsItAndSavesIt, aRefusedPreviewShowsTheReasonAndAnEditSavesWithPut,
    deleteAsksOnceMoreBeforeItDeletes, aBoardCardFromACustomCheckShowsItsLabelValuesAsText, anOpenEditorKeepsWhatWasTypedWhenTheIntegrationsPageLoadsAgain, aViewerNeverLoadsTheChecks, theStylesUseThemeColoursAndStackAtPhoneWidth]) {
    try { await t(); } catch (e) { console.log('FAIL ' + t.name + '\n' + (e && e.stack || e)); process.exitCode = 1; }
  }
  process.exit(process.exitCode || 0);
})();
