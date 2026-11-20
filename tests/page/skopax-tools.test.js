const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

function tool(name, extra) {
  return Object.assign({name, kind: 'read', calls: 0, runs: 0, errors: 0, errorRate: null, truncated: 0, p50Ms: null, p95Ms: null, maxMs: null, lastAt: null, byTrigger: {}, recentErrors: []}, extra || {});
}

const usage = {days: 7, since: iso(7 * 1440), runs: 5, runsWithTools: 4, calls: 7, errors: 1, toolsUsed: 2, toolsDefined: 3, tools: [
  tool('kube', {calls: 5, runs: 3, errors: 1, errorRate: 0.2, p50Ms: 60, p95Ms: 15000, maxMs: 15000, lastAt: iso(10), byTrigger: {cards: 5},
    byCluster: {east: {calls: 3, errors: 1}, prod: {calls: 2, errors: 0}}, description: 'Read-only Kubernetes API'}),
  tool('esql', {calls: 2, runs: 2, errorRate: 0, p50Ms: 9, p95Ms: 9, maxMs: 9, lastAt: iso(20), byTrigger: {slack: 2},
    status: {state: 'unreachable', reason: 'Skopax cannot reach <b>logs-east</b>: every probe of them failed', at: iso(3)}}),
  tool('wingify', {calls: 4, runs: 0, errors: 1, errorRate: 0.25, p50Ms: 1800, p95Ms: 2100, maxMs: 2100, lastAt: iso(5), byTrigger: {check: 4}, description: 'VWO A/B test results',
    byCheck: {'wingify': {runs: 3, errors: 0}, 'wingify-verdict': {runs: 1, errors: 1}}, recentErrors: [{at: iso(5), check: 'wingify-verdict', trigger: 'check', error: 'error: HTTP 401'}]}),
  tool('argo')]};

async function boot(pack) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: iso(-59), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'Quiet', points: [], at: iso(1)}, anomalies: [],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=tools', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      if (pack) { w.SKOPAX_I18N = pack; w.document.cookie = 'skopax_lang=' + Object.keys(pack)[0] + '; path=/'; }
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/tool-usage')) return ok(usage);
        if (p.startsWith('/api/server')) return ok({plugins: [{id: 'kubernetes', name: 'Kubernetes', icon: '/plugins/kubernetes/icon.svg', tab: null, passes: [], instances: [{name: 'prod', primary: true, pass: null, status: null}, {name: 'east', primary: false, pass: null, status: null}]}]});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: null});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/nodes')) return ok({nodes: []});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(300);
  return {w: dom.window, doc: dom.window.document, errors};
}

async function kubeCallsSplitByCluster() {
  //given
  const {w, doc, errors} = await boot();
  const row = (name) => doc.querySelector('#tools tr.tool-row[data-tool="' + name + '"]');

  //when
  row('kube').click();

  //then
  const split = row('kube').querySelector('.tclusters');
  assert.strictEqual(split.textContent, 'prod 2 · east 3 (1 err)', 'the primary first, from the plug-in data, then the others, errors marked');
  assert.strictEqual(split.title, 'prod: 2 calls\neast: 3 calls, 1 error');
  assert.ok(!row('esql').querySelector('.tclusters'), 'other tools have no split');
  assert.ok(/By cluster\s*east: 3 calls, 1 error · prod: 2 calls/.test(row('kube').nextElementSibling.textContent), 'the opened row says it in words');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the kube row splits its calls by cluster with errors, in the row and when opened');
}

async function checksCountAndASearchNarrowsTheList() {
  //given
  const {w, doc, errors} = await boot();
  const row = (name) => doc.querySelector('#tools tr.tool-row[data-tool="' + name + '"]');
  const names = () => [...doc.querySelectorAll('#tools tr.tool-row')].map((r) => r.dataset.tool);
  const find = doc.getElementById('tools-find');
  const type = (text) => { find.value = text; find.dispatchEvent(new w.Event('input', {bubbles: true})); };

  //when
  row('wingify').click();
  const more = row('wingify').nextElementSibling;
  type('verdict');
  const byCheck = names();
  type('KUBERNETES');
  const byDescription = names();
  type('nothing like this');
  const none = doc.querySelector('#tools tbody').textContent;
  const shown = doc.getElementById('tools-shown').textContent;
  type('');
  const all = names();

  //then
  assert.strictEqual(row('wingify').querySelector('.ttrig').textContent, 'Checks 4', 'scheduled check runs show as their own run type');
  assert.ok(/Scheduled checks on the same source\s*wingify: 3 runs · wingify-verdict: 1 run, 1 error/.test(more.textContent), 'the opened row names the checks behind the calls');
  assert.ok(!more.querySelector('a.run-link') && /Checks · wingify-verdict · error: HTTP 401/.test(more.textContent), 'a check error has no run record to open');
  assert.deepStrictEqual(byCheck, ['wingify'], 'the search finds a tool by its check names');
  assert.deepStrictEqual(byDescription, ['kube'], 'and by its description, whatever the case');
  assert.ok(none.includes('No tool matches') && shown === '0 of 4', none + ' / ' + shown);
  assert.deepStrictEqual(all, ['kube', 'wingify', 'esql', 'argo'], 'clearing the search shows every tool again');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS scheduled checks count as calls under their own run type with the checks named, and a search narrows the list by name, description or check');
}

async function anUnreachableToolSaysWhyInItsOpenedRow() {
  //given
  const {w, doc, errors} = await boot();
  const row = (name) => doc.querySelector('#tools tr.tool-row[data-tool="' + name + '"]');
  const reason = 'Skopax cannot reach <b>logs-east</b>: every probe of them failed';

  //when
  row('esql').click();
  const more = row('esql').nextElementSibling;
  const why = more.querySelector('.tstate-why');

  //then
  assert.ok(!more.hidden, 'the row opens');
  assert.ok(why && why.textContent.startsWith(reason + ' (checked '), 'the opened row repeats the reason the badge only shows on hover');
  assert.strictEqual(why.textContent, row('esql').querySelector('.tstate').title);
  assert.strictEqual(more.querySelectorAll('b').length, 0, 'the reason is text');
  assert.ok(!row('kube').nextElementSibling.querySelector('.tstate-why'), 'a tool that is on has no reason');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS an unreachable tool names its failing targets in the opened row, as text, not only in the hover title');
}

async function aSortedHeaderTranslatesWithItsArrowApart() {
  //given
  const {w, doc, errors} = await boot({de: {'@th': {'Calls': 'Aufrufe', 'Runs': 'Laeufe'}}});
  const button = (id) => doc.querySelector('#tools th button[data-sort="' + id + '"]');

  //when
  const first = [button('calls').textContent, button('runs').textContent];
  const arrow = button('calls').querySelector('span[aria-hidden="true"]');
  button('runs').click();
  await wait(50);
  const second = [button('calls').textContent, button('runs').textContent];

  //then
  assert.deepStrictEqual(first, ['Aufrufe \u2193', 'Laeufe'], 'the sorted header translates; its arrow stays apart');
  assert.ok(arrow && arrow.textContent === ' \u2193', 'the arrow has its own hidden span');
  assert.deepStrictEqual(second, ['Aufrufe', 'Laeufe \u2193'], 'the arrow moves with the sort and both headers stay translated');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a sorted Tools header translates through @th, with the sort arrow in its own hidden span');
}

async function aBridgedMcpToolReadsAsAnMcpServerTool() {
  //given
  usage.tools.push(tool('mcp_main_search', {kind: 'mcp', calls: 1, runs: 1, errorRate: 0, p50Ms: 40, p95Ms: 40, maxMs: 40, lastAt: iso(1), byTrigger: {cards: 1}}));
  const {w, doc, errors} = await boot();

  //when
  const kind = doc.querySelector('#tools tr.tool-row[data-tool="mcp_main_search"] .tkind').textContent;

  //then
  assert.strictEqual(kind, 'MCP server', 'a bridged tool is not shown as removed');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a bridged MCP tool reads as an MCP server tool, not a removed one');
}

async function heldAndRefusedShowApartFromErrors() {
  //given
  const held = {what: 'cause', tools: ['past_cards'], phrases: ['known noise']};
  usage.held = 1;
  usage.refused = 1;
  usage.tools.push(tool('note', {calls: 2, runs: 1, errorRate: 0, p50Ms: 5, p95Ms: 5, maxMs: 5, lastAt: iso(2), byTrigger: {cards: 2}, held: 1, recentHeld: [{at: iso(2), runId: 'r1', trigger: 'cards', held}],
    refused: 1, recentRefused: [{at: iso(3), runId: 'r2', trigger: 'cards', error: 'cluster must be one of prod, edge, got "edge-data"'}]}));
  const de = JSON.parse(require('fs').readFileSync(require('path').join(__dirname, '..', '..', 'skopax', 'locale', 'de', 'page.json'), 'utf8'));
  const pack = Object.assign({}, de.page, {'@rules': de.rules});
  const plain = await boot();
  const german = await boot({de: pack});
  const row = (d) => d.doc.querySelector('#tools tr.tool-row[data-tool="note"]');

  //when
  row(plain).click();
  row(german).click();
  const more = row(plain).nextElementSibling;
  const mehr = row(german).nextElementSibling;
  const tiles = [...plain.doc.querySelectorAll('#tools .ctile .cl')].map((e) => e.textContent);

  //then
  assert.ok(tiles.includes('Errors') && tiles.includes('Held') && tiles.includes('Refused input'), 'all three counts show: ' + tiles);
  assert.ok(!/Latest errors/.test(more.textContent) && /No errors in this window/.test(more.textContent), 'a held call and a refused input are not errors');
  assert.ok(/Held: the run read text that looks planted \(known noise\) in past_cards; the cause was left out and the note names the trigger/.test(more.textContent), more.textContent);
  assert.ok(/Refused input \(1 of 1\)[\s\S]*cluster must be one of prod, edge, got "edge-data"/.test(more.textContent), 'the refused input is listed on its own');
  assert.ok(/Zur\u00fcckgehalten: Der Lauf hat Text gelesen, der eingeschleust wirkt \(known noise\) in past_cards/.test(mehr.textContent) && /die Ursache wurde weggelassen/.test(mehr.textContent), mehr.textContent);
  assert.deepStrictEqual(plain.errors.concat(german.errors), []);
  plain.w.close();
  german.w.close();
  console.log('PASS a held call and a refused input are listed apart from errors, with their own counts, in operator words and in German');
}

async function heldAndRefusedHaveTheirOwnColumnsAndTheHelpExplainsThem() {
  //given
  const {w, doc, errors} = await boot();
  const heads = [...doc.querySelectorAll('#tools thead th button')].map((b) => b.dataset.sort);
  const cell = (name, id) => {
    const at = heads.indexOf(id);
    return doc.querySelector('#tools tr.tool-row[data-tool="' + name + '"]').children[at].textContent;
  };
  const help = doc.querySelector('#panel-tools .q').title;

  //then
  assert.ok(heads.indexOf('held') > heads.indexOf('errors') && heads.indexOf('refused') > heads.indexOf('held'), 'Held and Refused columns follow Errors: ' + heads);
  assert.strictEqual(cell('note', 'errors'), '0', 'a held call and a refused input are not errors');
  assert.strictEqual(cell('note', 'held'), '1');
  assert.strictEqual(cell('note', 'refused'), '1');
  assert.strictEqual(cell('kube', 'held'), '0', 'a tool with no held calls shows 0');
  assert.ok(/held/i.test(help) && /planted/.test(help) && /refused/i.test(help) && /not (?:an |a )?errors?|not failures/i.test(help), 'the help explains held and refused next to errors: ' + help);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS held and refused have their own Tools columns and the help explains them');
}

(async () => {
  await kubeCallsSplitByCluster();
  await checksCountAndASearchNarrowsTheList();
  await anUnreachableToolSaysWhyInItsOpenedRow();
  await aSortedHeaderTranslatesWithItsArrowApart();
  await aBridgedMcpToolReadsAsAnMcpServerTool();
  await heldAndRefusedShowApartFromErrors();
  await heldAndRefusedHaveTheirOwnColumnsAndTheHelpExplainsThem();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
