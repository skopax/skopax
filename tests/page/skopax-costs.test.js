const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const TODAY = '2026-10-24';
const KEY = 'log|checkout-service|ERROR|boom';
const KEY2 = 'cf-status|cloudflare/CDN/Cache';
const PARTS = ['cacheRead', 'input', 'cacheWrite5m', 'cacheWrite1h', 'output', 'webSearch'];
const SOURCES = ['pod', 'page', 'slack', 'assess', 'gate', 'smalltalk'];
const cells = (tr) => [...tr.querySelectorAll('td')].map((td) => td.textContent);
const USAGE = {since: iso(7 * 1440), runs: 4, usd: 2, turns: 30, usage: {input_tokens: 12000, output_tokens: 11800, cache_read_input_tokens: 150000, cache_creation_input_tokens: 28000}, cacheShare: 0.789,
  models: {'claude-opus-5-5': 30}, outcomes: {done: 4}, tools: [], keys: [], slowest: []};

function money(parts) { const m = Object.assign({cacheRead: 0, input: 0, cacheWrite5m: 0, cacheWrite1h: 0, output: 0, webSearch: 0}, parts); m.total = PARTS.reduce((a, p) => a + m[p], 0); return m; }
function zero() { return {pod: 0, page: 0, slack: 0, chat: 0, assess: 0, gate: 0, smalltalk: 0, pagescan: 0, selfreview: 0, mcp: 0}; }
function keysFor(days) {
  if (days === 1) return [...Array(24).keys()].map((h) => TODAY + 'T' + String(h).padStart(2, '0'));
  return [...Array(days).keys()].reverse().map((n) => new Date(Date.UTC(2026, 9, 24 - n)).toISOString().slice(0, 10));
}

function costsBody(days) {
  const keys = keysFor(days), last = keys.length - 1;
  const perBucket = keys.map((t) => Object.assign({t}, zero(), {total: 0}));
  const cache = keys.map((t) => ({t, hit: {pod: null, page: null, slack: null, assess: null, gate: null, smalltalk: null}, writeUsd: zero()}));
  Object.assign(perBucket[last], {pod: 1.5, assess: 0.6, total: 2.1});
  Object.assign(perBucket[last - 1], {slack: 0.25, gate: 0.0003, total: 0.2503});
  cache[last - 3].hit.pod = 0.4;
  cache[last - 2].hit.pod = 0.5;
  cache[last].hit.pod = 0.95;
  cache[last].writeUsd.pod = 0.3;
  cache[last - 1].hit.slack = 0;
  cache[last - 1].writeUsd.slack = 0.2;
  return {days, from: keys[0].slice(0, 10), to: TODAY, bucket: days === 1 ? 'hour' : 'day',
    today: {day: TODAY, spent: 3.4, cap: 20}, total: {usd: 2.7503, calls: 6, runs: 4, cacheHit: 0.93}, avg: {slackAsk: 0.25, podRun: 1.5}, perBucket, cache,
    sources: [
      {source: 'pod', calls: 2, runs: 1, usd: money({cacheRead: 0.6, input: 0.2, cacheWrite5m: 0.3, output: 0.4}), cacheHit: 0.95, avgRun: 1.5, models: {'claude-opus-5-5': 2}},
      {source: 'page', calls: 1, runs: 1, usd: money({input: 0.4}), cacheHit: 0, avgRun: 0.4, models: {'claude-opus-5-5': 1}},
      {source: 'slack', calls: 1, runs: 1, usd: money({input: 0.05, cacheWrite1h: 0.2}), cacheHit: 0, avgRun: 0.25, models: {'claude-sonnet-5-5': 1}},
      {source: 'assess', calls: 1, runs: 1, usd: money({output: 0.6}), cacheHit: null, avgRun: 0.6, models: {'claude-opus-5-5': 1}},
      {source: 'gate', calls: 1, runs: 0, usd: money({input: 0.0002, output: 0.0001}), cacheHit: 0, avgRun: null, models: {'claude-haiku-5-5': 1}},
      {source: 'smalltalk', calls: 0, runs: 0, usd: money({}), cacheHit: null, avgRun: null, models: {}}],
    models: [{model: 'claude-opus-5-5', calls: 4, usd: 2.5}, {model: 'claude-sonnet-5-5', calls: 1, usd: 0.25}, {model: 'claude-haiku-5-5', calls: 1, usd: 0.0003}],
    topCards: [{key: KEY, runs: 3, usd: 1.9503}, {key: KEY2, runs: 1, usd: 0.2}],
    topAskers: [{by: 'dora.lane', runs: 1, calls: 1, usd: 0.4}, {by: 'adam.baker@example.com', runs: 1, calls: 2, usd: 0.2503}, {by: 'token', runs: 1, calls: 1, usd: 0.33}],
    runs: [
      {id: 'r1', at: iso(30), source: 'pod', by: 'pod', trigger: 'cards', outcome: 'done', what: KEY, keys: [KEY], model: 'claude-opus-5-5', effort: 'medium', calls: 2, toolCalls: 2, seconds: 95,
       usd: money({cacheRead: 0.6, input: 0.2, cacheWrite5m: 0.3, output: 0.4}), cacheHit: 0.95},
      {id: 'r4', at: iso(60), source: 'assess', by: 'server', trigger: 'assess', outcome: 'failed', what: '2 deploys, 1 flagged', keys: [], model: 'claude-opus-5-5', effort: 'medium', calls: 1, toolCalls: 1, seconds: 180,
       usd: money({output: 0.6}), cacheHit: null},
      {id: 'r2', at: iso(90), source: 'slack', by: 'adam.baker@example.com', trigger: null, outcome: null, what: KEY, keys: [KEY], model: 'claude-sonnet-5-5', effort: 'low', calls: 1, toolCalls: null, seconds: null,
       usd: money({input: 0.05, cacheWrite1h: 0.2}), cacheHit: 0}]};
}

function stateWith() {
  return {rev: 5, updatedAt: iso(0), sweptIso: iso(1), nextIso: iso(120), sweptAt: '21:04', nextRun: '22:04', serverPasses: {prom: {at: iso(7)}},
    sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'h', points: [], at: iso(1)},
    anomalies: [{key: KEY, svc: 'checkout-service', label: 'ERROR pattern', sev: 'warn', status: 'open', now: 40, d1: 5, d7: 4, firstSeen: iso(30), lastSeen: iso(1), ns: '-', src: 'logs'}],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, deploys: []};
}

async function boot(opts) {
  const o = opts || {};
  const errors = [], gets = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = stateWith();
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state).replace(/<\//g, '<\\/'));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/' + (o.hash || ''), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.localStorage.setItem('skopax-live', '1');
      if (o.days) w.localStorage.setItem('skopax-costs-days', o.days);
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        const path = String(p);
        gets.push(path);
        const rec = path.match(/^\/api\/investigations\/([^?]+)$/);
        if (rec && o.records) return o.records[decodeURIComponent(rec[1])] ? {ok: true, status: 200, json: async () => o.records[decodeURIComponent(rec[1])]} : {ok: false, status: 404, json: async () => ({detail: 'no such run'})};
        if (path.startsWith('/api/costs')) {
          if (o.costs) return {ok: false, status: o.costs, json: async () => ({detail: 'no'})};
          const body = costsBody(Number(new URL(path, 'https://skopax.example.com').searchParams.get('days')));
          return {ok: true, status: 200, json: async () => body};
        }
        if (o.hold && path.startsWith('/api/investigations')) await o.hold;
        if (o.noUsage && path.startsWith('/api/investigator/usage')) return {ok: false, status: 404, json: async () => ({})};
        const me = o.guest ? {loginEnabled: true, user: null, operator: false, jira: false}
          : {loginEnabled: true, user: 'dora.lane@example.com', operator: true, jira: true, task: {project: 'OPS', parent: 'OPS-103', points: 1}};
        const body = path.startsWith('/api/me') ? me
          : path.startsWith('/api/rev') ? {rev: state.rev} : path.startsWith('/api/server') ? {investigate: {mode: 'live', enabled: true, stopped: null, spentToday: 3.4, cap: 20}}
          : path.startsWith('/api/investigator/usage') ? USAGE : [];
        return {ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body)};
      };
    },
  });
  await wait(300);
  return {dom, w: dom.window, doc: dom.window.document, errors, gets};
}

const costGets = (gets) => gets.filter((p) => p.startsWith('/api/costs'));

async function theCostsTabSitsNextToHistoryAndLoadsWhenOpened() {
  //given
  const {dom, doc, gets, errors} = await boot();
  const delivery = [...doc.querySelectorAll('#snav a.tab[data-tab]')].map((t) => t.dataset.tab);
  const before = costGets(gets).length;
  const early = doc.getElementById('count-costs').textContent;
  //when
  doc.getElementById('tab-costs').click();
  await wait(100);
  //then
  assert.deepStrictEqual(delivery.slice(delivery.indexOf('acts'), delivery.indexOf('acts') + 3), ['acts', 'costs', 'integrations'], 'Costs sits right after History, the Connect group after Costs');
  assert.strictEqual(before, 0, 'nothing is fetched before the tab opens');
  assert.strictEqual(early, '$3.40', 'the count already shows the cap counter from the investigator info before the tab opens');
  assert.deepStrictEqual(costGets(gets), ['/api/costs?days=7'], 'seven days by default');
  assert.ok(!doc.getElementById('panel-costs').hidden && doc.getElementById('panel-acts').hidden);
  assert.strictEqual(doc.getElementById('count-costs').textContent, '$3.40', 'the tab count is today\'s spend');
  assert.deepStrictEqual([...doc.querySelectorAll('#costs-range button')].map((b) => [b.textContent, b.getAttribute('aria-pressed')]), [['1 d', 'false'], ['7 d', 'true'], ['30 d', 'false']]);
  assert.ok(!doc.getElementById('costs').classList.contains('loading'));
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS the Costs tab sits next to History, loads 7 days only when opened and shows today\'s spend on its count');
}

async function theRangeSwitchRefetchesAndRemembersTheChoice() {
  //given
  const {dom, w, doc, gets} = await boot();
  doc.getElementById('tab-costs').click();
  await wait(50);
  //when
  doc.querySelector('#costs-range button[data-days="30"]').click();
  await wait(100);
  const thirty = [w.localStorage.getItem('skopax-costs-days'), doc.querySelectorAll('#costs .ctile .cl')[1].textContent];
  doc.querySelector('#costs-range button[data-days="1"]').click();
  await wait(100);
  //then
  assert.deepStrictEqual(costGets(gets), ['/api/costs?days=7', '/api/costs?days=30', '/api/costs?days=1']);
  assert.deepStrictEqual(thirty, ['30', 'Last 30 days']);
  assert.strictEqual(doc.querySelectorAll('#costs .ctile .cl')[1].textContent, 'Today, all sources');
  assert.strictEqual(doc.querySelector('#costs-range button[aria-pressed="true"]').dataset.days, '1');
  dom.window.close();
  //given
  const again = await boot({days: '30', hash: '#t=costs'});
  //then
  assert.deepStrictEqual(costGets(again.gets), ['/api/costs?days=30'], 'the range is remembered in this browser');
  assert.strictEqual(again.doc.querySelector('#costs-range button[aria-pressed="true"]').dataset.days, '30');
  again.dom.window.close();
  console.log('PASS the 1 d / 7 d / 30 d switch refetches and the choice is remembered in this browser');
}

async function aGuestGetsTheReasonAndNoNumbers() {
  //given
  const guest = await boot({costs: 401});
  const other = await boot({costs: 403});
  //when
  guest.doc.getElementById('tab-costs').click();
  other.doc.getElementById('tab-costs').click();
  await wait(100);
  //then
  assert.strictEqual(guest.doc.getElementById('costs').textContent, 'Log in to see costs.');
  assert.strictEqual(other.doc.getElementById('costs').textContent, 'Costs are not open to this account.');
  assert.ok(!guest.doc.querySelector('#costs .ctile') && guest.doc.getElementById('count-costs').textContent === '\u2013', 'no numbers and no count');
  assert.deepStrictEqual(guest.errors.concat(other.errors), []);
  guest.dom.window.close();
  other.dom.window.close();
  console.log('PASS a guest or a non-operator gets the reason instead of numbers');
}

async function aFailedRangeSwitchShowsTheUnavailableNoteInsteadOfTheOldNumbers() {
  //given
  const opts = {};
  const {dom, doc, errors} = await boot(opts);
  doc.getElementById('tab-costs').click();
  await wait(100);
  const before = doc.querySelectorAll('#costs .ctile').length;
  //when
  opts.costs = 500;
  doc.querySelector('#costs-range button[data-days="30"]').click();
  await wait(100);
  //then
  assert.ok(before > 0);
  assert.strictEqual(doc.getElementById('costs').textContent, 'Costs are not available right now.');
  assert.ok(!doc.querySelector('#costs .ctile') && !doc.getElementById('costs').classList.contains('loading'));
  assert.strictEqual(doc.querySelector('#costs-range button[aria-pressed="true"]').dataset.days, '30');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS a failed range switch replaces the old numbers with the unavailable note');
}

async function aGuestFollowingACostsLinkLandsOnTheDefaultTab() {
  //given
  const {dom, w, doc} = await boot({guest: true});
  //when
  w.location.hash = '#t=costs';
  await wait(100);
  //then
  assert.ok(doc.getElementById('panel-costs').hidden, 'the Costs panel stays closed');
  assert.ok(!doc.getElementById('panel-anoms').hidden, 'the guest lands on the default tab');
  dom.window.close();
  console.log('PASS a guest following a Costs link lands on the default tab');
}

async function costsAreForOperatorsOnlyAndAGuestNeverSeesTheTab() {
  //given
  const guest = await boot({guest: true});
  const guestLink = await boot({guest: true, hash: '#t=costs'});
  const operator = await boot();
  //then
  const gt = guest.doc.getElementById('tab-costs');
  assert.ok(!gt.hidden, 'a guest still sees the Costs tab');
  assert.strictEqual(gt.getAttribute('aria-disabled'), 'true', 'but disabled');
  assert.strictEqual(gt.title, 'Log in to see costs', 'every login sees costs since the operator role went away; a guest is asked to log in');
  assert.strictEqual(guest.doc.getElementById('count-costs').textContent, '\u2013', 'and no count');
  gt.click();
  gt.dispatchEvent(new guest.dom.window.KeyboardEvent('keydown', {key: ' ', bubbles: true, cancelable: true}));
  await wait(100);
  assert.ok(guest.doc.getElementById('panel-costs').hidden && !guest.doc.getElementById('panel-anoms').hidden, 'a click or Space leaves the panel closed');
  assert.deepStrictEqual(costGets(guest.gets), [], 'and no costs request');
  assert.ok(guestLink.doc.getElementById('panel-costs').hidden && !guestLink.doc.getElementById('tab-costs').hidden, 'a Costs link does not open the panel for a guest');
  assert.deepStrictEqual(costGets(guestLink.gets), []);
  const ot = operator.doc.getElementById('tab-costs');
  assert.ok(!ot.hidden && !ot.hasAttribute('aria-disabled') && ot.title === '', 'an operator sees the tab enabled');
  assert.strictEqual(operator.doc.getElementById('count-costs').textContent, '$3.40');
  assert.deepStrictEqual(guest.errors.concat(guestLink.errors, operator.errors), []);
  guest.dom.window.close();
  guestLink.dom.window.close();
  operator.dom.window.close();
  console.log('PASS costs are for operators only: a guest gets no tab, no count and no request');
}

async function theStripShowsTodayAgainstTheCapAndTheRange() {
  //given
  const {dom, w, doc} = await boot();
  doc.getElementById('tab-costs').click();
  await wait(100);
  //when
  const labels = [...doc.querySelectorAll('#costs .ctile .cl')].map((x) => x.textContent);
  const values = [...doc.querySelectorAll('#costs .ctile .cv')].map((x) => x.textContent);
  const notes = [...doc.querySelectorAll('#costs .ctile .cn')].map((x) => x.textContent);
  const meter = doc.querySelector('#costs .cmeter');
  const calm = [meter.className, meter.firstChild.style.width];
  const body = costsBody(7);
  w.renderCosts(Object.assign({}, body, {today: {day: TODAY, spent: 16, cap: 20}}));
  const warn = doc.querySelector('#costs .cmeter').className;
  w.renderCosts(Object.assign({}, body, {today: {day: TODAY, spent: 21, cap: 20}}));
  const over = [doc.querySelector('#costs .cmeter').className, doc.querySelector('#costs .cmeter').firstChild.style.width];
  //then
  assert.deepStrictEqual(labels, ['Today', 'Last 7 days', 'Avg per Slack request', 'Avg per investigator run', 'Cache hit share']);
  assert.deepStrictEqual(values, ['$3.40 of $20.00 cap', '$2.75', '$0.25', '$1.50', '93 %']);
  assert.deepStrictEqual(notes, ['4 runs \u00b7 6 calls', 'cache reads over all prompt tokens']);
  assert.deepStrictEqual(calm, ['cmeter k-info', '17%'], 'the meter fills to the share of the cap');
  assert.strictEqual(warn, 'cmeter k-warn', 'from 75 % of the cap it turns to warning');
  assert.deepStrictEqual(over, ['cmeter k-crit', '100%'], 'at the cap it is critical and full');
  assert.strictEqual(meter.getAttribute('role'), 'meter');
  dom.window.close();
  console.log('PASS the top strip shows today against the cap as a meter, the range total, the two averages and the cache hit share');
}

async function oneDayIsCutIntoHoursAndThirtyDaysLabelEveryFifthDay() {
  //given
  const day = await boot({days: '1'});
  const month = await boot({days: '30'});
  //when
  day.doc.getElementById('tab-costs').click();
  month.doc.getElementById('tab-costs').click();
  await wait(100);
  const hourCols = [...day.doc.querySelectorAll('#costs-spend .ccol')];
  //then
  assert.strictEqual(day.doc.querySelector('#costs-spend h3').textContent, 'Spend per hour, today');
  assert.strictEqual(hourCols.length, 24);
  assert.deepStrictEqual([...day.doc.querySelectorAll('#costs-spend .cx span')].slice(0, 7).map((s) => s.textContent), ['00:00', '', '', '', '', '', '06:00'], 'every sixth hour is labelled');
  assert.strictEqual(hourCols[23].title, '23:00 \u00b7 $2.10 \u00b7 Investigator runs $1.50 \u00b7 Assessments $0.60');
  assert.strictEqual(month.doc.querySelector('#costs-spend h3').textContent, 'Spend per day');
  assert.strictEqual(month.doc.querySelectorAll('#costs-spend .ccol').length, 30);
  assert.deepStrictEqual([...month.doc.querySelectorAll('#costs-spend .cx span')].slice(0, 8).map((s) => s.textContent), ['25 Sep', '', '', '', '', '', '', '2 Oct'], 'every seventh day is labelled, day and English month');
  day.dom.window.close();
  month.dom.window.close();
  console.log('PASS one day is cut into 24 local hours with every sixth hour labelled, thirty days label every seventh day');
}

async function spendPerDayStacksEverySourceWithALegendTooltipsAndATable() {
  //given
  const {dom, doc} = await boot();
  doc.getElementById('tab-costs').click();
  await wait(100);
  //when
  const cols = [...doc.querySelectorAll('#costs-spend .ccol')];
  const last = cols[6];
  //then
  assert.strictEqual(cols.length, 7);
  assert.deepStrictEqual([...last.children].map((b) => b.className), ['s-pod', 's-assess'], 'sources stack in their fixed order, the first on the baseline');
  assert.deepStrictEqual([...last.children].map((b) => b.style.height), ['50%', '20%'], 'heights follow the axis top of $3');
  assert.strictEqual(last.title, '24 Oct \u00b7 $2.10 \u00b7 Investigator runs $1.50 \u00b7 Assessments $0.60');
  assert.strictEqual(cols[5].title, '23 Oct \u00b7 $0.25 \u00b7 Slack requests $0.25 \u00b7 Gate $0.0003', 'a sub-cent gate cost keeps four decimals');
  assert.strictEqual(last.getAttribute('tabindex'), null, 'no tab stops inside the chart: the table view serves keyboard users');
  assert.strictEqual(doc.querySelector('#costs-spend .cchart').getAttribute('aria-label'), 'Spend per day, 7 days by source, $2.75');
  assert.strictEqual(doc.querySelector('#costs-hit .cchart').getAttribute('aria-label'), 'Cache hit share per day, 7 days by source');
  assert.strictEqual(doc.querySelector('#costs-writes .cchart').getAttribute('aria-label'), 'Cache-write spend per day, 7 days by source, $0.50');
  assert.deepStrictEqual([...doc.querySelectorAll('#costs-spend .clegend span')].map((s) => [s.textContent, s.querySelector('.sw').className]),
    [['Investigator runs', 'sw s-pod'], ['Page requests', 'sw s-page'], ['Slack requests', 'sw s-slack'], ['Chat', 'sw s-chat'], ['Assessments', 'sw s-assess'], ['Gate', 'sw s-gate'], ['Small talk', 'sw s-smalltalk'], ['Page scan', 'sw s-pagescan'], ['Self-review', 'sw s-selfreview'], ['MCP', 'sw s-mcp']], 'the legend always lists all ten sources in slot order');
  assert.deepStrictEqual([...doc.querySelectorAll('#costs-spend .cy span')].map((s) => s.textContent), ['$0.00', '$1.00', '$2.00', '$3.00']);
  assert.deepStrictEqual([...doc.querySelectorAll('#costs-spend .cx span')].map((s) => s.textContent), ['18 Oct', '', '20 Oct', '', '22 Oct', '', '24 Oct'], 'seven days label every second one');
  const rows = [...doc.querySelectorAll('#costs-spend .ctable tbody tr')];
  assert.strictEqual(rows.length, 7);
  assert.deepStrictEqual(cells(rows[6]), ['24 Oct', '$1.50', '$0.00', '$0.00', '$0.00', '$0.60', '$0.00', '$0.00', '$0.00', '$0.00', '$0.00'], 'the table view carries every value, MCP last');
  dom.window.close();
  console.log('PASS spend per day stacks the ten sources in fixed colours with a legend, an axis, per-column tooltips and a table view');
}

async function theBySourceTableShowsSplitHitShareAverageAndModels() {
  //given
  const {dom, doc} = await boot();
  doc.getElementById('tab-costs').click();
  await wait(100);
  //when
  const rows = [...doc.querySelectorAll('#costs-sources tbody tr')];
  //then
  assert.deepStrictEqual([...doc.querySelectorAll('#costs-sources thead th')].map((th) => th.textContent),
    ['Source', 'Calls', 'Runs', 'Total', 'Cache read', 'Input', 'Cache write', 'Cache write, long', 'Output', 'Web search', 'Cache hit', 'Avg / run', 'Models']);
  assert.deepStrictEqual(rows.map((r) => r.dataset.source), SOURCES);
  assert.deepStrictEqual(cells(rows[0]), ['Investigator runs', '2', '1', '$1.50', '$0.60', '$0.20', '$0.30', '$0.00', '$0.40', '$0.00', '95 %', '$1.50', 'opus-5-5 2']);
  assert.deepStrictEqual(cells(rows[2]).slice(7), ['$0.20', '$0.00', '$0.00', '0 %', '$0.25', 'sonnet-5-5 1']);
  assert.deepStrictEqual(cells(rows[3]).slice(10), ['\u2013', '$0.60', 'opus-5-5 1'], 'no prompt tokens, no hit share');
  assert.deepStrictEqual(cells(rows[4]).slice(1, 4).concat(cells(rows[4]).slice(11)), ['1', '0', '$0.0003', '\u2013', 'haiku-5-5 1'], 'the gate has calls but no runs');
  assert.deepStrictEqual(cells(rows[5]).slice(0, 4), ['Small talk', '0', '0', '$0.00'], 'small talk has its own row, with calls but never runs');
  assert.strictEqual(rows[0].querySelector('.sw').className, 'sw s-pod', 'each source keeps its chart colour in the table');
  dom.window.close();
  console.log('PASS the by-source table shows calls, runs, the five-part split, cache hit share, average per run and the model mix');
}

async function cacheEfficiencyIsTwoChartsAHitShareLineAndWriteBars() {
  //given
  const {dom, doc} = await boot();
  doc.getElementById('tab-costs').click();
  await wait(100);
  //when
  const lines = [...doc.querySelectorAll('#costs-hit polyline')];
  const dots = [...doc.querySelectorAll('#costs-hit .cdot')];
  const hits = [...doc.querySelectorAll('#costs-hit .ccol')];
  const writes = [...doc.querySelectorAll('#costs-writes .ccol')];
  //then
  assert.deepStrictEqual(lines.map((l) => l.getAttribute('class')), ['ln s-pod', 'ln s-pod', 'ln s-slack'], 'a day without prompt tokens breaks the line');
  assert.strictEqual(lines[0].getAttribute('points'), '50.00,60.00 64.29,50.00');
  assert.strictEqual(lines[0].getAttribute('vector-effect'), 'non-scaling-stroke', 'the 2 px line stays 2 px at any width');
  assert.deepStrictEqual(dots.map((d) => [d.className, d.style.left, d.style.bottom]), [['cdot s-pod', '50%', '40%'], ['cdot s-pod', '64.29%', '50%'], ['cdot s-pod', '92.86%', '95%'], ['cdot s-slack', '78.57%', '0%']]);
  assert.strictEqual(hits[6].title, '24 Oct \u00b7 Investigator runs 95 % \u00b7 Page requests \u2013 \u00b7 Slack requests \u2013 \u00b7 Chat \u2013 \u00b7 Assessments \u2013 \u00b7 MCP \u2013', 'one tooltip lists every source at that day');
  assert.deepStrictEqual([...doc.querySelectorAll('#costs-hit .cy span')].map((s) => s.textContent), ['0 %', '50 %', '100 %']);
  assert.deepStrictEqual([...doc.querySelectorAll('#costs-hit .clegend span')].map((s) => s.textContent), ['Investigator runs', 'Page requests', 'Slack requests', 'Chat', 'Assessments', 'MCP'], 'the gate and small talk are not cached and not drawn');
  assert.deepStrictEqual([writes[5], writes[6]].map((c) => [...c.children].map((b) => b.className)), [['s-slack'], ['s-pod']]);
  assert.ok(!doc.querySelector('#costs-hit .ccol > b') && !doc.querySelector('#costs-writes polyline'), 'one measure per chart: no dual axis');
  assert.match(doc.getElementById('costs').textContent, /The gate and small talk are not cached, so they are left out of the two cache charts\./);
  dom.window.close();
  console.log('PASS cache efficiency is two charts: a hit-share line per source that breaks on empty days, and cache-write spend bars');
}

async function topCardsAndTopAskersSitSideBySide() {
  //given
  const {dom, doc} = await boot();
  doc.getElementById('tab-costs').click();
  await wait(100);
  //when
  const cards = [...doc.querySelectorAll('#costs-cards tbody tr')];
  const people = [...doc.querySelectorAll('#costs-people tbody tr')];
  //then
  assert.ok(doc.querySelector('.cpair #costs-cards') && doc.querySelector('#costs-cards').parentElement === doc.querySelector('#costs-people').parentElement, 'the two tables share one row');
  assert.deepStrictEqual(cards.map(cells), [['checkout-service / ERROR pattern', '3', '$1.95'], ['cloudflare/CDN/Cache', '1', '$0.20']]);
  assert.strictEqual(cards[0].querySelector('a').getAttribute('href'), '#a=log%7Ccheckout-service%7CERROR%7Cboom', 'a card opens on the Anomalies tab');
  assert.deepStrictEqual(people.map(cells), [['dora.lane', '1', '1', '$0.40'], ['adam.baker@example.com', '1', '2', '$0.25'], ['API calls', '1', '1', '$0.33']], 'the service token reads API calls');
  dom.window.close();
  console.log('PASS top cards and top askers sit side by side, and a card links to itself');
}

async function cardsInBothTablesShowTheirReadableNameLinkToTheCardAndMuteTheGoneOnes() {
  //given
  const {dom, w, doc, errors} = await boot();
  doc.getElementById('tab-costs').click();
  await wait(100);
  const body = costsBody(7);
  const GONE = 'log|old-service|WARN|gone';
  body.runs = [
    Object.assign({}, body.runs[0], {id: 'm1', what: KEY + ', ' + GONE, keys: [KEY, GONE]}),
    Object.assign({}, body.runs[2], {id: 'm2', source: 'page', what: 'Why did it go up?', keys: [KEY]}),
    Object.assign({}, body.runs[1], {id: 'm3', what: '2 deploys, 1 flagged', keys: []})];
  body.topCards = [{key: KEY, runs: 1, usd: 1}, {key: GONE, runs: 1, usd: 0.5}];
  //when
  w.renderCosts(body);
  const top = [...doc.querySelectorAll('#costs-cards tbody td.ckey a')];
  const runs = [...doc.querySelectorAll('#costs-runs tbody tr')];
  const pair = [...runs[0].querySelectorAll('.cwhat a')];
  //then
  assert.deepStrictEqual(top.map((a) => [a.textContent, a.title, a.getAttribute('href'), a.classList.contains('muted')]),
    [['checkout-service / ERROR pattern', KEY, '#a=log%7Ccheckout-service%7CERROR%7Cboom', false], ['old-service / WARN', GONE, '#a=log%7Cold-service%7CWARN%7Cgone', true]], 'name from the card or the key, key as tooltip, gone cards muted but linked');
  assert.deepStrictEqual(pair.map((a) => [a.textContent, a.title, a.getAttribute('href'), a.classList.contains('muted')]),
    [['checkout-service / ERROR pattern', KEY, '#a=log%7Ccheckout-service%7CERROR%7Cboom', false], ['old-service / WARN', GONE, '#a=log%7Cold-service%7CWARN%7Cgone', true]], 'a run with two cards lists two links');
  assert.strictEqual(runs[0].querySelector('.cwhat').textContent, 'checkout-service / ERROR pattern, old-service / WARN', 'comma-separated');
  assert.strictEqual(runs[1].querySelector('.cwhat').textContent, 'Why did it go up?', 'an ask keeps its text');
  assert.strictEqual(runs[1].querySelectorAll('.cwhat a').length, 0);
  assert.strictEqual(runs[2].querySelector('.cwhat').textContent, '2 deploys, 1 flagged', 'a report summary keeps its text');
  assert.strictEqual(runs[2].querySelectorAll('.cwhat a').length, 0);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS cards in Top cards and Runs show svc / signal with the key as tooltip, link to the card and mute the gone ones');
}

async function clickingACardLinkInCostsOpensTheAnomaliesTabOnThatCardRinged() {
  //given
  const {dom, doc, errors} = await boot();
  doc.getElementById('tab-costs').click();
  await wait(100);
  //when
  doc.querySelector('#costs-cards tbody td.ckey a').click();
  await wait(150);
  const card = doc.querySelector('#anoms article.inc[data-key="' + KEY + '"]');
  //then
  assert.ok(doc.getElementById('panel-costs').hidden && !doc.getElementById('panel-anoms').hidden, 'the Anomalies tab is shown');
  assert.ok(card && card.classList.contains('linked'), 'the card is ringed');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  //given
  const second = await boot();
  second.doc.getElementById('tab-costs').click();
  await wait(100);
  //when
  second.doc.querySelector('#costs-runs tbody tr .cwhat a').click();
  await wait(150);
  //then
  assert.ok(!second.doc.getElementById('panel-anoms').hidden && second.doc.querySelector('#anoms article.inc.linked'), 'a run row link does the same');
  second.dom.window.close();
  console.log('PASS clicking a card link in Costs shows the Anomalies tab with that card ringed');
}

async function everyChartColourIsDefinedForLightAndBothDarkScopes() {
  //given
  const css = fs.readFileSync(TEMPLATE, 'utf8');
  const all = ['pod', 'page', 'slack', 'chat', 'assess', 'gate', 'smalltalk', 'pagescan', 'selfreview', 'mcp'];
  const tokens = all.map((s) => '--s-' + s).concat(PARTS.map((p) => '--p-' + p));
  //when
  const counts = tokens.map((t) => (css.match(new RegExp(t + ':', 'g')) || []).length);
  const pod = (css.match(/--s-pod: (#[0-9a-f]{6})/g) || []).map((m) => m.slice(-7));
  //then
  assert.deepStrictEqual(counts, tokens.map(() => 3), 'each colour is set in :root, the dark media query and [data-theme="dark"]');
  assert.deepStrictEqual(pod, ['#2a78d6', '#3987e5', '#3987e5'], 'dark mode has its own validated steps');
  assert.deepStrictEqual(all.filter((s) => !css.includes('.s-' + s + ' { --v: var(--s-' + s + '); }')), [], 'every source the server sends has a class that sets its chart colour, Chat and Self-review included');
  const lum = (h) => { const c = [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4))); return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]; };
  const light = (s) => css.match(new RegExp('--s-' + s + ': (#[0-9a-f]{6})'))[1];
  assert.ok(['slack', 'assess'].every((s) => 1.05 / (lum(light(s)) + 0.05) >= 3), 'the thin line sources reach 3:1 on the light surface');
  console.log('PASS every chart colour has a light value and the same dark value under the media query and the theme switch');
}

async function theStoredModelAuthorsShowAProviderNeutralName() {
  //given
  const {dom, w, doc, errors} = await boot();
  doc.getElementById('tab-costs').click();
  await wait(100);
  const body = costsBody(7);
  body.topAskers = [{by: 'Claude (self-review)', runs: 1, calls: 1, usd: 0.1}, {by: 'Claude', runs: 1, calls: 1, usd: 0.1}];

  //when
  w.renderCosts(body);
  const shown = [...doc.querySelectorAll('#costs-people tbody tr')].map((r) => r.querySelector('td').textContent);

  //then
  assert.deepStrictEqual(shown, ['AI (self-review)', 'the investigator']);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS the stored model authors show a provider-neutral name');
}

async function hostileTextIsRenderedAsTextAndEmptyOrZeroDataNeverProducesNaN() {
  //given
  const {dom, w, doc, errors} = await boot();
  doc.getElementById('tab-costs').click();
  await wait(100);
  const evil = '<img src=x onerror="window.pwned=1">&"';
  const body = costsBody(7);
  body.topCards = [{key: evil, runs: 1, usd: 0.1}];
  body.topAskers = [{by: evil, runs: 1, calls: 1, usd: 0.1}];
  body.sources[0].models = {[evil]: 1};
  const bad = () => [...doc.querySelectorAll('#costs [style], #costs svg *')].filter((e) => /NaN|Infinity|undefined/.test((e.getAttribute('style') || '') + (e.getAttribute('points') || '') + e.outerHTML)).length;
  //when
  w.renderCosts(body);
  const injected = doc.querySelectorAll('#costs img').length;
  const cardText = doc.querySelector('#costs-cards .ckey').textContent;
  const askerText = doc.querySelector('#costs-people tbody td').textContent;
  const modelText = doc.querySelector('#costs-sources .cmodels').textContent;
  const empty = Object.assign({}, costsBody(7), {perBucket: [], cache: [], topCards: [], topAskers: []});
  w.renderCosts(empty);
  const emptyBad = bad();
  const emptyCols = doc.querySelectorAll('#costs .ccol').length;
  const zero = Object.assign({}, costsBody(7));
  zero.perBucket = [Object.assign({t: '2026-10-24', total: 0}, {pod: 0, page: 0, slack: 0, assess: 0, gate: 0, smalltalk: 0})];
  zero.cache = [{t: '2026-10-24', hit: {pod: null, page: null, slack: null, assess: null, gate: null, smalltalk: null}, writeUsd: {pod: 0, page: 0, slack: 0, assess: 0, gate: 0, smalltalk: 0}}];
  w.renderCosts(zero);
  const zeroBad = bad();
  const zeroCols = doc.querySelectorAll('#costs-spend .ccol').length;
  //then
  assert.strictEqual(injected, 0, 'no element is injected');
  assert.strictEqual(w.pwned, undefined);
  assert.deepStrictEqual([cardText, askerText, modelText.includes(evil)], [evil, evil, true], 'hostile strings are shown as text');
  assert.strictEqual(emptyBad, 0);
  assert.strictEqual(emptyCols, 0, 'no rows, no columns');
  assert.strictEqual(zeroBad, 0, 'one all-zero bucket draws without NaN or Infinity');
  assert.strictEqual(zeroCols, 1);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS hostile card, asker and model strings render as text, and empty or all-zero data draws no NaN');
}

async function theRunListShowsTheSplitBarAndLinksTheFullRunRecord() {
  //given
  const {dom, doc} = await boot();
  doc.getElementById('tab-costs').click();
  await wait(100);
  //when
  const rows = [...doc.querySelectorAll('#costs-runs tbody tr')];
  const link = rows[0].querySelector('a.run-link');
  const split = rows[0].querySelector('.csplit');
  //then
  assert.deepStrictEqual(rows.map((r) => r.dataset.run), ['r1', 'r4', 'r2']);
  assert.deepStrictEqual(cells(rows[0]).slice(1), ['Investigator runs', 'pod', 'checkout-service / ERROR pattern', 'opus-5-5 \u00b7 medium', '2', '95 s', '', '$1.50']);
  assert.deepStrictEqual([link.getAttribute('href'), link.getAttribute('target'), link.getAttribute('rel')], ['/api/investigations/r1', '_blank', 'noopener'], 'the time opens the full run record');
  assert.deepStrictEqual([...split.children].map((b) => [b.className, b.style.width]), [['p-cacheRead', '40%'], ['p-input', '13.3%'], ['p-cacheWrite5m', '20%'], ['p-output', '26.7%']]);
  assert.strictEqual(split.title, 'Cache read $0.60 \u00b7 Input $0.20 \u00b7 Cache write $0.30 \u00b7 Cache write, long $0.00 \u00b7 Output $0.40 \u00b7 Web search $0.00');
  assert.deepStrictEqual(cells(rows[1]).slice(1, 2), ['Assessments failed'], 'a run that did not finish says so');
  assert.deepStrictEqual(cells(rows[2]).slice(5, 7), ['\u2013', '\u2013'], 'an expired run record leaves tools and duration empty');
  assert.deepStrictEqual([...doc.querySelectorAll('#costs-runs .clegend span')].map((s) => [s.textContent, s.querySelector('.sw').className]),
    [['Cache read', 'sw p-cacheRead'], ['Input', 'sw p-input'], ['Cache write', 'sw p-cacheWrite5m'], ['Cache write, long', 'sw p-cacheWrite1h'], ['Output', 'sw p-output'], ['Web search', 'sw p-webSearch']], 'the split legend runs from the cheapest token to the dearest, then the search fee');
  dom.window.close();
  console.log('PASS the run list shows source, who, card or question, model, tools, duration, a $-split bar and the total, and links the full run record');
}

async function aRunLinkOpensTheRecordInTheJsonViewerAndCostsStays() {
  //given
  const RECORD = {id: 'r1', usage: {input_tokens: 10}, calls: []};
  const {dom, w, doc, gets, errors} = await boot({records: {r1: RECORD}});
  doc.getElementById('tab-costs').click();
  await wait(100);
  const link = () => doc.querySelector('#costs-runs tr[data-run="r1"] a.run-link');
  const wrap = doc.getElementById('json-wrap');
  const where = w.location.href;
  //when
  const notPrevented = link().dispatchEvent(new w.MouseEvent('click', {bubbles: true, cancelable: true, button: 0}));
  await wait(50);
  const shown = [!wrap.hidden, doc.getElementById('json-title').textContent, doc.getElementById('json-body').textContent];
  doc.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  //then
  assert.strictEqual(notPrevented, false, 'the click stays in the page');
  assert.deepStrictEqual(shown, [true, 'Run record', JSON.stringify(RECORD, null, 2)]);
  assert.ok(gets.includes('/api/investigations/r1'));
  assert.ok(wrap.hidden && !doc.getElementById('panel-costs').hidden, 'Esc closes the viewer and Costs is still open');
  assert.strictEqual(doc.activeElement, link(), 'focus goes back to the run time');
  assert.strictEqual(w.location.href, where, 'the address stays on #t=costs');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS a run link opens its record in the JSON viewer (Close / Esc, Open raw) and the Costs tab stays where it was');
}

async function historyUsageIsOneLineThatOpensCosts() {
  //given
  const {dom, doc, gets} = await boot();
  doc.getElementById('tab-acts').click();
  await wait(50);
  const line = doc.querySelector('#pod-ctl .pod-usage');
  //when
  line.querySelector('a').click();
  await wait(100);
  //then
  assert.strictEqual(line.textContent, 'Usage \u00b7 7 d \u00b7 4 runs \u00b7 $2.00 \u00b7 cache 79 % \u00b7 Open Costs \u203a');
  assert.strictEqual(line.querySelector('a').getAttribute('href'), '#t=costs');
  assert.ok(!doc.getElementById('panel-costs').hidden, 'Open Costs \u203a switches to the Costs tab');
  assert.deepStrictEqual(costGets(gets), ['/api/costs?days=7']);
  dom.window.close();
  console.log('PASS History keeps one usage line whose Open Costs \u203a link opens the Costs tab');
}

async function theSplitBarScalesFromThePartsAndAnIdThatNeedsEncodingStillLinks() {
  //given
  const {dom, w, doc} = await boot();
  doc.getElementById('tab-costs').click();
  await wait(100);
  const body = costsBody(7);
  body.runs = [
    Object.assign({}, body.runs[0], {id: 'a/b', usd: Object.assign(money({cacheRead: 0.6, output: 0.2}), {total: 1})}),
    Object.assign({}, body.runs[0], {id: 'z', usd: Object.assign(money({}), {total: 0})})];
  //when
  w.renderCosts(body);
  const rows = [...doc.querySelectorAll('#costs-runs tbody tr')];
  const widths = [...rows[0].querySelectorAll('.csplit > b')].map((b) => b.style.width);
  //then
  assert.deepStrictEqual(widths, ['75%', '25%'], 'the bar fills from the parts, not from the stored total');
  assert.strictEqual(rows[0].querySelector('a.run-link').getAttribute('href'), '/api/investigations/a%2Fb', 'the id is encoded');
  assert.strictEqual(rows[0].children[8].textContent, '$1.00', 'the number stays the run total');
  assert.strictEqual(rows[1].querySelectorAll('.csplit > b').length, 0, 'all parts zero draws an empty bar');
  assert.ok(!/NaN|Infinity/.test(rows[1].outerHTML));
  dom.window.close();
  console.log('PASS the split bar scales from the parts, an all-zero run draws an empty bar and a run id is encoded in its link');
}

async function theUsageLineFollowsWhoLooksAndWhetherUsageIsThere() {
  //given
  const guest = await boot({guest: true});
  const bare = await boot({noUsage: true});
  const bareGuest = await boot({guest: true, noUsage: true});
  //when
  const lines = [guest, bare, bareGuest].map((b) => b.doc.querySelector('#pod-ctl .pod-usage'));
  //then
  assert.strictEqual(lines[0].textContent, 'Usage \u00b7 7 d \u00b7 4 runs \u00b7 $2.00 \u00b7 cache 79 %', 'a guest sees the usage text');
  assert.ok(!lines[0].querySelector('a.costs-link'), 'and no link to a tab they do not have');
  assert.strictEqual(lines[1].textContent, 'Open Costs \u203a', 'an operator without usage numbers still gets the link');
  assert.strictEqual(lines[2], null, 'a guest without usage numbers gets no line');
  [guest, bare, bareGuest].forEach((b) => b.dom.window.close());
  console.log('PASS the usage line shows the text to everyone, the link to operators only, and nothing when there is neither');
}

async function accessResolvingBeforeTheInvestigationsDoesNotRenderAnEmptyHistory() {
  //given
  let release;
  const hold = new Promise((r) => { release = r; });
  const {dom, doc} = await boot({hold});
  //when
  const before = [doc.getElementById('pod-ctl').textContent, doc.getElementById('pod-list').textContent, doc.querySelectorAll('#pod-ctl .costs-link').length];
  release();
  await wait(100);
  //then
  assert.deepStrictEqual(before, ['', 'Loading investigations\u2026', 0], 'the loading note stays: no "not set up" and no "No investigations yet." while the data is still on its way');
  assert.strictEqual(doc.querySelectorAll('#pod-ctl .costs-link').length, 1, 'the link appears once the data is in');
  assert.strictEqual(doc.querySelectorAll('#pod-ctl .pod-usage').length, 1);
  dom.window.close();
  console.log('PASS operator access arriving before the investigations never renders an empty History');
}

(async () => {
  await theCostsTabSitsNextToHistoryAndLoadsWhenOpened();
  await theRangeSwitchRefetchesAndRemembersTheChoice();
  await aGuestGetsTheReasonAndNoNumbers();
  await aFailedRangeSwitchShowsTheUnavailableNoteInsteadOfTheOldNumbers();
  await aGuestFollowingACostsLinkLandsOnTheDefaultTab();
  await costsAreForOperatorsOnlyAndAGuestNeverSeesTheTab();
  await theStripShowsTodayAgainstTheCapAndTheRange();
  await oneDayIsCutIntoHoursAndThirtyDaysLabelEveryFifthDay();
  await spendPerDayStacksEverySourceWithALegendTooltipsAndATable();
  await theBySourceTableShowsSplitHitShareAverageAndModels();
  await cacheEfficiencyIsTwoChartsAHitShareLineAndWriteBars();
  await topCardsAndTopAskersSitSideBySide();
  await cardsInBothTablesShowTheirReadableNameLinkToTheCardAndMuteTheGoneOnes();
  await clickingACardLinkInCostsOpensTheAnomaliesTabOnThatCardRinged();
  await everyChartColourIsDefinedForLightAndBothDarkScopes();
  await theRunListShowsTheSplitBarAndLinksTheFullRunRecord();
  await aRunLinkOpensTheRecordInTheJsonViewerAndCostsStays();
  await historyUsageIsOneLineThatOpensCosts();
  await theSplitBarScalesFromThePartsAndAnIdThatNeedsEncodingStillLinks();
  await theUsageLineFollowsWhoLooksAndWhetherUsageIsThere();
  await accessResolvingBeforeTheInvestigationsDoesNotRenderAnEmptyHistory();
  await hostileTextIsRenderedAsTextAndEmptyOrZeroDataNeverProducesNaN();
  await theStoredModelAuthorsShowAProviderNeutralName();
})().catch((e) => { console.error(e); process.exit(1); });
