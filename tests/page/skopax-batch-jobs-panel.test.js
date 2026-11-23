const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const COPIES = [
  {tab: 'batch-jobs', panel: fs.readFileSync(path.join(__dirname, '..', '..', 'skopax', 'plugins', 'batch_jobs', 'panel.js'), 'utf8')},
  {tab: 'example-batch-jobs', panel: fs.readFileSync(path.join(__dirname, '..', '..', 'examples', 'batch-jobs-plugin', 'skopax_batch_jobs', 'panel.js'), 'utf8')},
];
let copy = COPIES[0];
const AT = '2026-10-17T08:00:00Z';
const NOW = Date.parse(AT) / 1000;

function job(group, name, extra) {
  return Object.assign({part: 'job', name: 'shop-jobs', group, job: name, runs: 12, runsD1: 12, runsD7: 12, fails: 0, failsD1: 0, failsD7: 0, avg: 30, avgD1: 30, avgD7: 30,
    max: 40, interval: 300, last: NOW - 120}, extra || {});
}

function card(subject, sev, extra) {
  const own = extra && extra.key ? Object.assign({}, extra, {key: extra.key.replace(/^batch-jobs\|/, copy.tab + '|')}) : extra;
  return Object.assign({key: copy.tab + '|shop-jobs|' + subject + '|failing', src: copy.tab, status: 'open', sev, label: 'failed runs', short: 'x'}, own || {});
}

function render(rows, anomalies) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM('<section id="s"></section><script>' + copy.panel + '</script>', {runScripts: 'dangerously', virtualConsole: vc});
  const w = dom.window;
  const section = w.document.getElementById('s');
  w.document.dispatchEvent(new w.CustomEvent('skopax:state', {detail: {state: {[copy.tab]: rows, anomalies}}}));
  w.document.dispatchEvent(new w.CustomEvent('skopax:page', {detail: {tab: copy.tab, section}}));
  return {dom, section, errors};
}

function statusOf(section, name) {
  const row = [...section.querySelectorAll('tbody tr')].find((r) => r.querySelector('.svc').textContent === name);
  return row.querySelector('td .state').textContent;
}

function groupsAndRowsRenderWithTheirCards() {
  //given
  const rows = [job('shop', 'export-orders'), job('shop', 'send-invoices'), job('__proto__', 'cleanup-sessions'), job('constructor', 'rebuild-search-index'),
    {part: 'summary', name: 'shop-jobs', jobs: 4, groups: 3, metric: 'batch_job_duration_seconds', selector: '', at: AT}];
  const anomalies = [card('shop/export-orders', 'crit'), card('__proto__/cleanup-sessions', 'warn'), card('shop/send-invoices', 'crit', {status: 'cleared'}),
    card('shop', 'warn', {key: 'batch-jobs|shop-jobs|shop|missed', label: 'jobs missed'})];

  //when
  const {dom, section, errors} = render(rows, anomalies);
  const heads = [...section.querySelectorAll('h4')].map((h) => h.textContent);

  //then
  assert.deepStrictEqual(heads, ['__proto__', 'constructor', 'shop']);
  assert.strictEqual(statusOf(section, 'export-orders'), 'Critical');
  assert.strictEqual(statusOf(section, 'send-invoices'), 'OK', 'a cleared card does not count');
  assert.strictEqual(statusOf(section, 'cleanup-sessions'), 'Warning');
  assert.strictEqual(statusOf(section, 'rebuild-search-index'), 'OK');
  assert.ok(section.textContent.includes('jobs missed'), 'the group card shows at its group');
  assert.ok(section.textContent.includes('2 min ago'));
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'groups named like object members render, and each job follows its own open cards';
}

function aSlashOrBarInANameMatchesOnlyItsOwnCard() {
  //given
  const rows = [job('shop', 'export/orders'), job('shop/export', 'orders'), job('shop|eu', 'send-invoices'),
    {part: 'summary', name: 'shop-jobs', jobs: 3, groups: 3, metric: 'batch_job_duration_seconds', selector: '', at: AT}];
  const anomalies = [card('shop/export%2Forders', 'crit'), card('shop%7Ceu/send-invoices', 'warn')];

  //when
  const {dom, section, errors} = render(rows, anomalies);
  const states = [...section.querySelectorAll('tbody tr')].map((r) => r.querySelector('.svc').textContent + ' ' + r.querySelector('td .state').textContent);

  //then
  assert.deepStrictEqual(states, ['export/orders Critical', 'orders OK', 'send-invoices Warning']);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a slash or bar in a group or job is escaped like the card key, so only its own card matches';
}

function aPercentSignIsEscapedSoNamesThatLookEscapedStayApart() {
  //given
  const rows = [job('shop', '50%'), job('shop', 'a/b'), job('shop', 'a%2Fb'),
    {part: 'summary', name: 'shop-jobs', jobs: 3, groups: 1, metric: 'batch_job_duration_seconds', selector: '', at: AT}];
  const anomalies = [card('shop/50%25', 'warn'), card('shop/a%252Fb', 'crit')];

  //when
  const {dom, section, errors} = render(rows, anomalies);
  const states = [...section.querySelectorAll('tbody tr')].map((r) => r.querySelector('.svc').textContent + ' ' + r.querySelector('td .state').textContent);

  //then
  assert.deepStrictEqual(states, ['50% Warning', 'a/b OK', 'a%2Fb Critical']);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a percent sign is escaped like the card key, so a name that looks escaped keeps its own card';
}

try {
  for (copy of COPIES) {
    for (const t of [groupsAndRowsRenderWithTheirCards, aSlashOrBarInANameMatchesOnlyItsOwnCard, aPercentSignIsEscapedSoNamesThatLookEscapedStayApart]) console.log('PASS ' + copy.tab + ': ' + t());
  }
} catch (e) {
  console.error('FAIL', e.message);
  process.exit(1);
}
