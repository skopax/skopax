const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const PANEL = fs.readFileSync(path.join(__dirname, '..', '..', 'skopax', 'plugins', 'cloudwatch', 'panel.js'), 'utf8');
const SLOT = '<script type="text/plain" id="plugin-scripts"></script>';
const ICON = '/plugins/cloudwatch/icon.svg';
const PLUGINS = [{id: 'cloudwatch', name: 'CloudWatch', icon: ICON, tab: 'cloudwatch', passes: ['cloudwatch', 'cloudwatch-logs'], instances: [],
  pages: [{tab: 'cloudwatch', label: 'CloudWatch', group: 'observe', icon: ICON}]}];
const EU = 'eu-west-1', CN = 'cn-north-1';
const EU_CONSOLE = 'https://eu-west-1.console.aws.amazon.com/cloudwatch/home?region=eu-west-1';
const CN_CONSOLE = 'https://cn-north-1.console.amazonaws.cn/cloudwatch/home?region=cn-north-1';
const EVIL = '<img src=x onerror="window.__evil=1">';
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString().slice(0, 19) + 'Z';
const pad = (n) => (n < 10 ? '0' : '') + n;
const CHECKED = iso(3), RAISED = iso(600), LOGS_AT = iso(4);

function shown(at) {
  const d = new Date(at);
  return (d.toDateString() === new Date().toDateString() ? '' : d.getDate() + ' ' + MONTHS[d.getMonth()] + ' ') + pad(d.getHours()) + ':' + pad(d.getMinutes());
}

function summary(region, extra) {
  return Object.assign({part: 'summary', at: CHECKED, region, alarms: 2, cards: 2, capped: false, insufficientData: false, alarmsCut: false,
    counts: {ALARM: 2, INSUFFICIENT_DATA: 1, OK: 40}, countsCut: false, listCut: false, plugin: 'cloudwatch'}, extra || {});
}

function alarmRow(name, extra) {
  return Object.assign({part: 'alarm', name, type: 'metric', state: 'ALARM', reason: 'Threshold Crossed: 3 datapoints were greater than 80.0', since: RAISED,
    metric: 'metric AWS/RDS CPUUtilization (DBInstanceIdentifier=orders-db) Average', plugin: 'cloudwatch'}, extra || {});
}

function groupRow(group, extra) {
  return Object.assign({part: 'group', group, n: 40, hour: 70, minutes: 60,
    windows: [{from: 1, to: 2, n: 30, sample: 'ERROR payment failed'}, {from: 2, to: 3, n: 40, sample: 'ERROR timeout'}],
    plugin: 'cloudwatch'}, extra || {});
}

function card(key, sev, extra) {
  return Object.assign({key, src: key.indexOf('cloudwatch-logs') === 0 ? 'cloudwatch-logs' : 'cloudwatch', sig: 'cloudwatch', ns: '-', svc: '-', sev, status: 'open',
    label: key.split('|')[2], short: sev.toUpperCase() + ' CloudWatch ' + key.split('|')[2], now: 1, firstSeen: iso(30), lastSeen: iso(1)}, extra || {});
}

function stateWith(over) {
  return Object.assign({rev: 3, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 14 * 60000).toISOString(),
    sources: {prom: 'ok', cloudwatch: 'ok', 'cloudwatch-logs': 'ok \u00b7 2 log groups'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], routes: [], deploys: [], cloudwatchAlarms: [], cloudwatchLogs: []}, over || {});
}

function filled() {
  return stateWith({
    sources: {prom: 'ok', cloudwatch: 'ok', 'cloudwatch-logs': 'ok \u00b7 2 log groups', 'cloudwatch-china': 'ok', 'cloudwatch-logs-china': 'off: no log groups set'},
    anomalies: [card('cloudwatch|' + EU + '|orders-db-cpu', 'crit'), card('cloudwatch-logs|' + EU + '|/ecs/orders', 'high'),
      card('cloudwatch|' + EU + '|api-5xx', 'warn', {status: 'cleared'}), card('cloudwatch|' + CN + '|orders-db-cpu@other', 'crit', {cluster: 'other'}),
      card('cloudwatch|' + CN + '|orders-db-cpu@china', 'high', {cluster: 'china'}), card('cloudwatch|' + EU + '|queue-depth', 'warn', {muted: true})],
    cloudwatchAlarms: [summary(EU), alarmRow('orders-db-cpu'), alarmRow('checkout-down', {type: 'composite', metric: 'composite alarm, rule ALARM(a) OR ALARM(b)'}),
      alarmRow('queue-depth', {state: 'INSUFFICIENT_DATA', reason: 'Insufficient Data'}),
      summary(CN, {cluster: 'china', counts: {ALARM: 1, INSUFFICIENT_DATA: 0, OK: 3}}), alarmRow('orders-db-cpu', {cluster: 'china'})],
    cloudwatchLogs: [{part: 'summary', at: LOGS_AT, region: EU, groups: 3, cards: 1, bytesScanned: 1024, capped: false, missingGroups: [], least: 20, plugin: 'cloudwatch'},
      groupRow('/ecs/orders'), groupRow('/aws/lambda/checkout', {n: 0, hour: 3, minutes: 30, windows: [{from: 1, to: 2, n: 3, sample: 'Exception in handler'}, {from: 2, to: 3, n: 0, sample: ''}]}),
      groupRow('/aws/lambda/payments', {n: 5, hour: 5}), groupRow('/aws/lambda/refunds', {n: 25, hour: 25})],
  });
}

async function boot(state, lang) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  vc.on('error', (e) => errors.push('console.error ' + e));
  const html = pageHtml(TEMPLATE).replace(SLOT, '<script>\n' + PANEL + '</script>').replace('/*__SKOPAX_DATA__*/', JSON.stringify(state).replace(/<\//g, '<\\/'));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=cloudwatch', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      if (lang) {
        const raw = JSON.parse(fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'locale', lang, 'page.json'), 'utf8'));
        w.SKOPAX_I18N = {[lang]: Object.assign({}, raw.page, {'@rules': raw.rules, '@months': raw.months, '@days': raw.days, '@tails': raw.tails, '@th': raw.headers})};
        w.document.cookie = 'skopax_lang=' + lang + '; path=/';
      }
      w.fetch = async (p) => {
        const url = String(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (url.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', operator: true, admin: false, teams: [], tags: []});
        if (url.startsWith('/api/server')) return ok({plugins: PLUGINS, tz: 'UTC'});
        if (url.startsWith('/api/admin/settings')) return ok({items: [], rev: 1, refreshSeconds: 15});
        if (url.startsWith('/api/actions') || url.startsWith('/api/deploy-reports') || url.startsWith('/api/requests')) return ok([]);
        if (url.startsWith('/api/rev')) return ok({rev: state.rev});
        if (url.startsWith('/api/state')) return ok(state);
        return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
      };
    },
  });
  await wait(400);
  return {dom, w: dom.window, doc: dom.window.document, errors, section: dom.window.document.getElementById('panel-cloudwatch')};
}

function cells(row) { return [...row.querySelectorAll('td')].map((td) => td.textContent.trim()); }
function body(section, name) { return [...section.querySelectorAll('table[data-cw="' + name + '"] tbody tr')]; }
function notes(section, attr) { return [...section.querySelectorAll('p[data-cw="' + attr + '"]')].map((p) => p.textContent); }

async function aFilledPageShowsCountsAlarmsAndLogGroupsWithTheirCards() {
  //given
  const state = filled();

  //when
  const {dom, errors, section} = await boot(state);
  const counts = body(section, 'counts'), alarms = body(section, 'alarms'), groups = body(section, 'logs');
  const consoles = [...section.querySelectorAll('table[data-cw="alarms"] a.lnk')].map((a) => a.getAttribute('href'));
  const tags = [...section.querySelectorAll('table[data-cw="alarms"] a.tag')];

  //then
  assert.ok(section, 'the page adds the CloudWatch section');
  assert.deepStrictEqual(counts.map(cells), [[EU, 'Primary', '2', '1', '40', shown(CHECKED)], [CN, 'china', '1', '0', '3', shown(CHECKED)]]);
  assert.deepStrictEqual(alarms.map((r) => cells(r)[0]), ['ALARM', 'ALARM', 'INSUFFICIENT_DATA', 'ALARM']);
  assert.deepStrictEqual(alarms.map((r) => r.querySelector('.svc').textContent), ['orders-db-cpu', 'checkout-down', 'queue-depth', 'orders-db-cpu']);
  assert.deepStrictEqual(alarms.map((r) => cells(r)[2]), ['Metric', 'Composite', 'Metric', 'Metric']);
  assert.strictEqual(cells(alarms[0])[4], shown(RAISED), 'since uses the local clock of the page');
  assert.strictEqual(alarms[3].querySelector('.ns').textContent, 'metric AWS/RDS CPUUtilization (DBInstanceIdentifier=orders-db) Average \u00b7 china');
  assert.deepStrictEqual(consoles, [EU_CONSOLE + '#alarmsV2:alarm/orders-db-cpu', EU_CONSOLE + '#alarmsV2:alarm/checkout-down', EU_CONSOLE + '#alarmsV2:alarm/queue-depth',
    CN_CONSOLE + '#alarmsV2:alarm/orders-db-cpu']);
  assert.deepStrictEqual(tags.map((a) => a.getAttribute('href')), ['#a=' + encodeURIComponent('cloudwatch|' + EU + '|orders-db-cpu'), '#a=' + encodeURIComponent('cloudwatch|' + EU + '|queue-depth'),
    '#a=' + encodeURIComponent('cloudwatch|' + CN + '|orders-db-cpu@china')], 'every open card of the same instance is a tag, a muted one too');
  assert.deepStrictEqual(alarms.map((r) => r.querySelector('.state').className), ['state k-crit', 'state k-crit', 'state k-warn', 'state k-high'], 'a muted card does not colour its row');
  assert.deepStrictEqual(groups.map((r) => r.querySelector('.svc').textContent), ['/ecs/orders', '/aws/lambda/checkout', '/aws/lambda/payments', '/aws/lambda/refunds']);
  assert.deepStrictEqual(groups.map((r) => cells(r)[0]), ['High', 'Healthy', 'Below the card line', 'At or above the card line']);
  assert.ok(groups[0].querySelector('.state').classList.contains('k-high'));
  assert.deepStrictEqual(groups.map((r) => cells(r)[2]), ['40', '0', '5', '25']);
  assert.deepStrictEqual(groups.map((r) => cells(r)[3]), ['70', '3over 30 min', '5', '25']);
  assert.deepStrictEqual([...groups[0].querySelectorAll('td.msg .msg')].map((d) => d.textContent), ['ERROR timeout', 'ERROR payment failed'], 'newest sample first');
  assert.strictEqual(groups[0].querySelector('a.lnk').getAttribute('href'), EU_CONSOLE + '#logsV2:log-groups/log-group/$252Fecs$252Forders');
  assert.deepStrictEqual(groups.map((r) => cells(r)[5]), [shown(LOGS_AT), shown(LOGS_AT), shown(LOGS_AT), shown(LOGS_AT)], 'the log table says when it was checked');
  assert.deepStrictEqual(notes(section, 'logs-off').length, 1, 'the china instance has Logs off');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a filled page shows the counts per region, the ALARM and INSUFFICIENT_DATA alarms with console links, and the log groups with their cards';
}

async function theFiltersAndTheInstanceSwitcherNarrowTheListsAndKeepTheSearchFocus() {
  //given
  const {dom, w, doc, errors, section} = await boot(filled());
  const names = () => body(section, 'alarms').map((r) => r.querySelector('.svc') ? r.querySelector('.svc').textContent + '@' + (cells(r)[1].indexOf('china') >= 0 ? 'china' : '') : r.textContent);

  //when
  section.querySelector('[data-cw="state"] button[data-v="INSUFFICIENT_DATA"]').click();
  const insufficient = names();
  section.querySelector('[data-cw="state"] button[data-v="*"]').click();
  const region = section.querySelector('select[data-cw="region"]');
  region.value = CN;
  region.dispatchEvent(new w.Event('change'));
  const china = names();
  const countsChina = body(section, 'counts').map((r) => cells(r)[0]);
  region.value = '*';
  region.dispatchEvent(new w.Event('change'));
  const find = section.querySelector('input[data-cw="find"]');
  find.focus();
  find.value = 'composite';
  find.dispatchEvent(new w.Event('input'));
  const searched = names();
  doc.dispatchEvent(new w.CustomEvent('skopax:state', {detail: {state: filled()}}));
  const kept = [doc.activeElement === section.querySelector('input[data-cw="find"]'), section.querySelector('input[data-cw="find"]').value, names()];
  find.value = 'nothing-like-this';
  find.dispatchEvent(new w.Event('input'));
  const none = body(section, 'alarms').map((r) => r.textContent);
  find.value = '';
  find.dispatchEvent(new w.Event('input'));
  section.querySelector('[data-cw="inst"] button[data-v="china"]').click();
  const onlyChina = [names(), body(section, 'logs').map((r) => r.textContent), section.querySelector('[data-cw="inst"] button[data-v="china"]').getAttribute('aria-pressed')];

  //then
  assert.deepStrictEqual(insufficient, ['queue-depth@']);
  assert.deepStrictEqual(china, ['orders-db-cpu@china']);
  assert.deepStrictEqual(countsChina, [CN]);
  assert.deepStrictEqual(searched, ['checkout-down@']);
  assert.deepStrictEqual(kept, [true, 'composite', ['checkout-down@']], 'a live update keeps the typed text, the focus and the filter');
  assert.deepStrictEqual(none, ['No alarm matches the filters.']);
  assert.deepStrictEqual(onlyChina, [['orders-db-cpu@china'], ['CloudWatch Logs is off on every instance.'], 'true']);
  assert.deepStrictEqual([...section.querySelectorAll('[data-cw="inst"] button')].map((b) => b.textContent), ['All instances', 'Primary', 'china']);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'region, state and text filters and the instance switcher narrow the lists, and a live update keeps the search and its focus';
}

async function anEmptyPageSaysThereIsNoDataYet() {
  //given
  const state = stateWith({sources: {prom: 'ok'}});

  //when
  const {dom, errors, section} = await boot(state);
  const empty = [...section.querySelectorAll('tr.empty')].map((r) => r.textContent);

  //then
  assert.deepStrictEqual(empty, ['No CloudWatch alarm data yet. The alarm check fills this on its next run.', 'No CloudWatch alarm data yet. The alarm check fills this on its next run.',
    'No CloudWatch Logs data yet. The Logs check fills this on its next run.']);
  assert.strictEqual(section.querySelectorAll('[data-cw="inst"]').length, 0, 'one or no instance shows no switcher');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'an empty page says there is no CloudWatch data yet';
}

async function failuresCapsAndRefusalsAreClearSentences() {
  //given
  const state = stateWith({
    sources: {prom: 'ok', cloudwatch: 'error: AWS cloudwatch:DescribeAlarms: no answer from monitoring.eu-west-1.amazonaws.com; check Region and the network',
      'cloudwatch-logs': 'ok \u00b7 no access to logs'},
    cloudwatchAlarms: [summary(EU, {alarmsCut: true, countsCut: true, listCut: true, countsError: '', counts: {ALARM: 500, INSUFFICIENT_DATA: 3, OK: 997}})],
    cloudwatchLogs: [{part: 'summary', at: iso(4), region: EU, groups: 1, cards: 0, bytesScanned: 0, capped: true, missingGroups: ['/ecs/gone'], plugin: 'cloudwatch'}],
  });

  //when
  const {dom, errors, section} = await boot(state);
  const counts = body(section, 'counts').map(cells);

  //then
  assert.deepStrictEqual(notes(section, 'failed-alarms'), ['The last CloudWatch alarm check failed: AWS cloudwatch:DescribeAlarms: no answer from monitoring.eu-west-1.amazonaws.com; check Region and the network']);
  assert.deepStrictEqual(notes(section, 'capped'), ['Read 500 alarms in ALARM and stopped, so the ALARM count is at least the number shown.']);
  assert.strictEqual(notes(section, 'counts-cut').length, 1);
  assert.strictEqual(notes(section, 'list-cut').length, 1);
  assert.deepStrictEqual(counts, [[EU, 'Primary', '500+', '3+', '997+', shown(CHECKED)]]);
  assert.deepStrictEqual(notes(section, 'no-access'), ['Not read: no access to logs. The credentials lack the logs:StartQuery and logs:GetQueryResults permissions.']);
  assert.deepStrictEqual(notes(section, 'missing'), ['No log group with these names: /ecs/gone']);
  assert.strictEqual(notes(section, 'logs-cut').length, 1);
  assert.strictEqual(section.querySelectorAll('[data-cw="inst"]').length, 0, 'the cloudwatch-logs source is the Logs check, not an instance named logs');
  assert.ok(!/Traceback|<html|\{"/.test(section.textContent));
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a failed check, the caps, a refused read and missing log groups are named in clear sentences';
}

async function hostileRowsAreShownAsTextAndOnlyHttpsBecomesALink() {
  //given
  const state = stateWith({
    anomalies: [card('cloudwatch|' + EU + '|' + EVIL, 'crit', {label: EVIL, short: EVIL})],
    cloudwatchAlarms: [summary(EVIL), alarmRow(EVIL, {reason: EVIL, metric: EVIL, url: 'javascript:alert(1)'}), alarmRow('db', {url: 'https://x.example.com/"><img src=x onerror="window.__evil=2">'})],
    cloudwatchLogs: [groupRow(EVIL, {windows: [{from: 1, to: 2, n: 1, sample: EVIL}], url: 'http://plain.example.com/'})],
  });

  //when
  const {dom, w, errors, section} = await boot(state);

  //then
  assert.strictEqual(w.__evil, undefined);
  assert.strictEqual(section.querySelectorAll('img').length, 0);
  assert.ok(section.textContent.includes(EVIL));
  assert.deepStrictEqual([...section.querySelectorAll('a.lnk')].map((a) => a.getAttribute('href')), []);
  assert.ok([...section.querySelectorAll('.svc')].every((e) => e.hasAttribute('data-no-tx')), 'plug-in values are kept out of translation');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'hostile names, reasons, samples and links stay text, and only a clean https address becomes a link';
}

async function turkishTranslatesThePageButNotThePluginValues() {
  //given
  const state = filled();

  //when
  const {dom, errors, section} = await boot(state, 'tr');
  await wait(100);
  const heads = [...section.querySelectorAll('h3.sec-t')].map((e) => e.textContent);
  const alarms = body(section, 'alarms');

  //then
  assert.deepStrictEqual(heads, ['B\u00f6lgeye ve duruma g\u00f6re alarmlar', 'ALARM ya da INSUFFICIENT_DATA durumundaki alarmlar', 'CloudWatch Logs hatalar\u0131']);
  assert.deepStrictEqual(alarms.map((r) => cells(r)[0]), ['ALARM', 'ALARM', 'INSUFFICIENT_DATA', 'ALARM']);
  assert.deepStrictEqual(alarms.map((r) => cells(r)[2]), ['Metrik', 'Bile\u015fik', 'Metrik', 'Metrik']);
  assert.strictEqual(alarms[0].querySelector('.msg').textContent, 'Threshold Crossed: 3 datapoints were greater than 80.0');
  assert.strictEqual(section.querySelector('input[data-cw="find"]').getAttribute('placeholder'), 'Alarm, neden ya da log grubu ara');
  assert.strictEqual(cells(body(section, 'logs')[1])[3], '3' + '30 dk \u00fczerinden');
  assert.deepStrictEqual([...section.querySelectorAll('table[data-cw="logs"] th')].map((e) => e.textContent).slice(2, 4), ['Son 15 dk', 'Yakla\u015f\u0131k son bir saat']);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the Turkish pack translates the page texts and leaves alarm names, reasons and states as AWS wrote them';
}

async function anInstanceThatWasNeverReadGetsItsStatusNotAnAllClear() {
  //given
  const state = stateWith({
    sources: {prom: 'ok', cloudwatch: 'ok', 'cloudwatch-logs': 'ok \u00b7 0 log groups', 'cloudwatch-east': 'error: AWS cloudwatch:DescribeAlarms: HTTP 503',
      'cloudwatch-logs-east': 'error: AWS logs:StartQuery: HTTP 503', 'cloudwatch-west': 'ok \u00b7 no access to alarms', 'cloudwatch-logs-west': 'ok \u00b7 no access to logs',
      'cloudwatch-north': 'ok'},
    cloudwatchAlarms: [summary(EU, {counts: {ALARM: 0, INSUFFICIENT_DATA: 0, OK: 4}})],
    cloudwatchLogs: [{part: 'summary', at: LOGS_AT, region: EU, groups: 0, cards: 0, bytesScanned: 0, capped: false, missingGroups: [], least: 20, plugin: 'cloudwatch'}],
  });

  //when
  const {dom, doc, w, errors, section} = await boot(state);
  const all = {counts: body(section, 'counts').map(cells), alarms: body(section, 'alarms').map((r) => r.textContent), logs: body(section, 'logs').map((r) => r.textContent),
    unread: notes(section, 'unread-alarms').concat(notes(section, 'unread-logs'))};
  section.querySelector('[data-cw="inst"] button[data-v="east"]').click();
  const east = {counts: body(section, 'counts').map(cells), alarms: body(section, 'alarms').map((r) => r.textContent), logs: body(section, 'logs').map((r) => r.textContent)};
  section.querySelector('[data-cw="inst"] button[data-v="west"]').click();
  const west = {alarms: body(section, 'alarms').map((r) => r.textContent), access: notes(section, 'no-access')};

  //then
  assert.deepStrictEqual(all.counts, [[EU, 'Primary', '0', '0', '4', shown(CHECKED)], ['\u2013', 'east', 'Not read: the last check failed.'], ['\u2013', 'north', 'Not read yet'], ['\u2013', 'west', 'Not read: no access to alarms.']]);
  assert.deepStrictEqual(all.alarms, ['No alarm is in ALARM or INSUFFICIENT_DATA on the instances read so far.']);
  assert.deepStrictEqual(all.logs, ['No watched log group was read on the instances read so far.']);
  assert.deepStrictEqual(all.unread, ['north: Not read yet: the alarm check has stored no result for this instance.', 'north: Not read yet: the Logs check has stored no result for this instance.'],
    'only the instance with no result at all is called not read yet, not the failed or refused ones');
  assert.deepStrictEqual(east, {counts: [['\u2013', 'east', 'Not read: the last check failed.']], alarms: ['No CloudWatch alarm data yet. The alarm check fills this on its next run.'],
    logs: ['No CloudWatch Logs data yet. The Logs check fills this on its next run.']});
  assert.deepStrictEqual(west.alarms, ['No CloudWatch alarm data yet. The alarm check fills this on its next run.']);
  assert.strictEqual(west.access.length, 2);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'an instance whose first read failed or was refused gets its status sentence, never a healthy empty text';
}

async function linksAreBuiltFromTheStoredNameAndTheRegionAndALogsEuInstanceStaysLogsOfEu() {
  //given
  const odd = 'orders "db" <cpu>/p99 #1', apostrophe = "Bob's-db-cpu (p99)!";
  const state = stateWith({
    sources: {prom: 'ok', cloudwatch: 'ok', 'cloudwatch-logs': 'ok', 'cloudwatch-eu': 'ok', 'cloudwatch-logs-eu': 'error: AWS logs:StartQuery: HTTP 500'},
    cloudwatchAlarms: [summary(EU), alarmRow(odd), alarmRow(apostrophe), alarmRow('[held: the text reads like instructions to an AI]', {noLink: true, keyName: 'alarm 0123456789ab'}),
      summary(CN, {cluster: 'eu'}), alarmRow('cn-cpu', {cluster: 'eu'})],
    cloudwatchLogs: [{part: 'summary', at: LOGS_AT, region: CN, groups: 1, cards: 0, bytesScanned: 0, capped: false, missingGroups: [], least: 20, plugin: 'cloudwatch'},
      groupRow('/ecs/orders'), groupRow('/ecs/masked', {noLink: true})],
    anomalies: [card('cloudwatch|' + EU + '|alarm 0123456789ab', 'warn')],
  });

  //when
  const {dom, errors, section} = await boot(state);
  const alarmLinks = body(section, 'alarms').map((r) => (r.querySelector('a.lnk') || {getAttribute: () => null}).getAttribute('href'));
  const alarmTags = body(section, 'alarms').map((r) => r.querySelectorAll('a.tag').length);
  const logLinks = body(section, 'logs').map((r) => (r.querySelector('a.lnk') || {getAttribute: () => null}).getAttribute('href'));
  const names = [...section.querySelectorAll('[data-cw="inst"] button')].map((b) => b.textContent);

  //then
  assert.deepStrictEqual(alarmLinks, [EU_CONSOLE + '#alarmsV2:alarm/' + encodeURIComponent(odd), EU_CONSOLE + '#alarmsV2:alarm/Bob%27s-db-cpu%20%28p99%29%21', null,
    CN_CONSOLE + '#alarmsV2:alarm/cn-cpu']);
  assert.deepStrictEqual(alarmTags, [0, 0, 1, 0], 'a held alarm still finds its card by the stored key name');
  assert.deepStrictEqual(logLinks, [CN_CONSOLE + '#logsV2:log-groups/log-group/$252Fecs$252Forders', null]);
  assert.deepStrictEqual(names, ['All instances', 'Primary', 'eu']);
  assert.deepStrictEqual(notes(section, 'failed-logs'), ['eu: The last CloudWatch Logs check failed: AWS logs:StartQuery: HTTP 500']);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'console links come from the stored name and the instance region, a masked name gets none, and cloudwatch-logs-eu is the Logs check of eu';
}

function shapes(text) { let i = 0; const shaped = text.replace(/\d+(?:[.,:]\d+)*/g, () => '{' + (i++) + '}'); return [text, shaped]; }
function rendered(section) {
  const out = new Set();
  const walk = (node) => {
    if (node.nodeType === 3) { const t = node.nodeValue.trim(); if (t && /[A-Za-z]/.test(t)) out.add(t); return; }
    if (node.nodeType !== 1 || node.hasAttribute('data-no-tx')) return;
    ['placeholder', 'aria-label', 'title'].forEach((a) => { const v = node.getAttribute(a); if (v) out.add(v); });
    node.childNodes.forEach(walk);
  };
  walk(section);
  return out;
}

async function everyTextThePageRendersHasAKeyInEachPack() {
  //given
  const filledFailing = filled();
  filledFailing.cloudwatchAlarms[0] = summary(EU, {alarmsCut: true, countsCut: true, listCut: true, countsError: 'HTTP 500'});
  filledFailing.cloudwatchLogs[0] = Object.assign({}, filledFailing.cloudwatchLogs[0], {capped: true, rowsCut: true, missingGroups: ['/ecs/gone']});
  filledFailing.sources['cloudwatch-west'] = 'ok \u00b7 no access to alarms';
  filledFailing.sources['cloudwatch-logs-west'] = 'ok \u00b7 no access to logs';
  filledFailing.sources['cloudwatch-east'] = 'error: x';
  filledFailing.sources['cloudwatch-logs-east'] = 'error: y';
  const states = [filledFailing, stateWith({sources: {prom: 'ok'}}), stateWith({sources: {prom: 'ok', cloudwatch: 'ok', 'cloudwatch-logs': 'off: no log groups set'},
    cloudwatchAlarms: [summary(EU, {counts: {ALARM: 0, INSUFFICIENT_DATA: 0, OK: 1}})]})];
  const packs = ['de', 'es', 'fr', 'tr'].map((l) => [l, JSON.parse(fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'locale', l, 'page.json'), 'utf8')).page]);
  const aws = new Set(['ALARM', 'INSUFFICIENT_DATA', 'OK']);
  const seen = new Set();

  //when
  for (const s of states) {
    const {dom, w, section} = await boot(s);
    rendered(section).forEach((t) => seen.add(t));
    const find = section.querySelector('input[data-cw="find"]');
    find.value = 'nothing-matches';
    find.dispatchEvent(new w.Event('input'));
    rendered(section).forEach((t) => seen.add(t));
    const region = section.querySelector('select[data-cw="region"]');
    if (region) {
      region.value = CN;
      region.dispatchEvent(new w.Event('change'));
      section.querySelector('[data-cw="inst"] button[data-v=""]').click();
      rendered(section).forEach((t) => seen.add(t));
    }
    dom.window.close();
  }
  const texts = [...seen].filter((t) => !aws.has(t));
  const missing = packs.map(([l, p]) => [l, texts.filter((t) => !shapes(t).some((k) => Object.hasOwn(p, k)))]);

  //then
  assert.ok(texts.length > 50, String(texts.length));
  assert.ok(texts.includes('Last 15 min') && texts.includes('No instance in the picked region.') && texts.includes('No log group matches the filters.'));
  assert.deepStrictEqual(missing, [['de', []], ['es', []], ['fr', []], ['tr', []]]);
  return 'every text the CloudWatch page renders, outside plug-in values, has a key in the de, es, fr and tr packs';
}

(async () => {
  for (const t of [aFilledPageShowsCountsAlarmsAndLogGroupsWithTheirCards, theFiltersAndTheInstanceSwitcherNarrowTheListsAndKeepTheSearchFocus, anEmptyPageSaysThereIsNoDataYet,
    failuresCapsAndRefusalsAreClearSentences, hostileRowsAreShownAsTextAndOnlyHttpsBecomesALink, turkishTranslatesThePageButNotThePluginValues,
    anInstanceThatWasNeverReadGetsItsStatusNotAnAllClear, linksAreBuiltFromTheStoredNameAndTheRegionAndALogsEuInstanceStaysLogsOfEu, everyTextThePageRendersHasAKeyInEachPack]) {
    console.log('PASS ' + (await t()));
  }
})().catch((e) => { console.error('FAIL', e.message); process.exit(1); });
