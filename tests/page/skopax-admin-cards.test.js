const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

function item(key, group, label, kind, value, extra) {
  return Object.assign({key, group, label, help: label + ' help text', kind, live: true, value, base: value, default: value, source: 'default', by: null, at: null}, extra || {});
}

function items() {
  return [
    item('slack.channel', 'Slack', 'Channel', 'text', 'C0123', {card: 'Channel and posting', order: 0, channelName: 'alerts'}),
    item('slack.thread-hours', 'Slack', 'Thread window (h)', 'float', '6', {card: 'Threads and reminders', order: 200, unit: 'h', shortLabel: 'Thread window', default: '2', base: '2', source: 'admin', by: 'dora.lane@example.com', at: iso(5), low: 0, high: 72}),
    item('slack.ask-cap', 'Slack', 'Slack requests per hour', 'int', '10', {card: 'Questions', order: 100, unit: 'per hour', low: 0, high: 100}),
    item('slack.dm-chats', 'Slack', 'Direct message chats', 'bool', 'true', {card: 'Questions', order: 103}),
    item('slack.state-notes', 'Slack', 'Status notes in threads', 'bool', 'false', {card: 'Threads and reminders', order: 203}),
    item('general.timezone', 'General', 'Timezone', 'text', 'UTC', {card: 'Time', order: 100}),
    item('investigate.daily-usd', 'Investigator', 'Daily limit ($)', 'float', '25', {card: 'Spend and limits', order: 100, unit: '$', low: 0, high: 500}),
    item('investigate.model', 'Investigator', 'Card model', 'choice', 'claude-opus-5-5', {card: 'Models', order: 0, choices: ['claude-opus-5-5', 'claude-sonnet-5-5']}),
  ];
}

async function boot(hash) {
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  let list = items();
  const hist = [];
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/' + hash, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, opts) => {
        const p = String(path), method = (opts && opts.method) || 'GET';
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/admin')) {
          calls.push([method, p, opts && opts.body ? JSON.parse(opts.body) : null]);
          if (p.startsWith('/api/admin/history')) return ok(hist);
          if (p === '/api/admin/notices') return ok({items: []});
          if (p === '/api/admin/features') return ok({edition: 'community', items: []});
          if (p === '/api/admin/anthropic-key') return ok({using: null, admin: false, config: false, unreadable: false, updatedBy: null, updatedAt: null, note: null});
          if (p.startsWith('/api/admin/small-talk')) return {ok: false, status: 404, json: async () => ({})};
          if (p.startsWith('/api/admin/settings') && method === 'GET') return ok({items: list, rev: 3, refreshSeconds: 15});
          if (p.startsWith('/api/admin/settings')) {
            const b = JSON.parse(opts.body), was = list.find((i) => i.key === b.key);
            const next = Object.assign({}, was, {value: b.value, source: 'admin', by: 'dora.lane@example.com', at: iso(0)});
            list = list.map((i) => i.key === b.key ? next : i);
            hist.unshift({id: 'h' + (hist.length + 1), seq: hist.length + 1, key: b.key, old: was.value, new: b.value, by: 'dora.lane@example.com', at: iso(0)});
            return ok({item: next, change: {}});
          }
          return ok([]);
        }
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'dora.lane@example.com', userRole: 'admin', operator: true, admin: true, teams: [], tags: []});
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/nodes')) return ok({nodes: []});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors, calls};
}

async function eachSectionSplitsIntoTitledCardsInTableOrder() {
  //given
  const {w, doc, errors} = await boot('#t=settings');
  const slack = doc.querySelector('#admin details.adm-grp[data-group="Slack"]');

  //then
  assert.deepStrictEqual([...slack.querySelectorAll('.adm-card-h')].map((h) => h.firstChild.textContent), ['Channel and posting', 'Questions', 'Threads and reminders']);
  assert.deepStrictEqual([...slack.querySelectorAll('.adm-card')].map((c) => [...c.querySelectorAll('.adm-row')].map((r) => r.dataset.key)),
    [['slack.channel'], ['slack.ask-cap', 'slack.dm-chats'], ['slack.thread-hours', 'slack.state-notes']]);
  const general = doc.querySelector('#admin details.adm-grp[data-group="General"]');
  assert.ok(general.querySelector('.adm-card.plain') && !general.querySelector('.adm-card-h'), 'a section with one card shows no card title');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'each Admin section splits into titled cards in the order the server gives, and a one-card section shows no title';
}

async function rowsShowUnitsSwitchesAChangedDotAndOneMetaColumn() {
  //given
  const {w, doc, errors} = await boot('#t=settings');
  const row = (key) => doc.querySelector('#admin .adm-row[data-key="' + key + '"]');

  //then
  assert.strictEqual(row('slack.thread-hours').querySelector('.adm-num .adm-unit').textContent, 'h');
  const name = row('slack.thread-hours').querySelector('.adm-n b');
  assert.deepStrictEqual([name.firstChild.textContent, name.querySelector('.sr').textContent, name.textContent], ['Thread window', ' (h)', 'Thread window (h)'], 'the unit shows once, beside the field; screen readers still hear the full label');
  assert.strictEqual(row('slack.ask-cap').querySelector('.adm-unit').textContent, 'per hour');
  const sw = row('slack.dm-chats').querySelector('input.adm-in[type="checkbox"][role="switch"]');
  assert.ok(sw && sw.checked && !row('slack.state-notes').querySelector('.adm-sw').checked, 'bool settings are switches');
  assert.ok(sw.getAttribute('aria-labelledby') === 'adm-slack-dm-chats-l', 'the switch is named by its label');
  assert.ok(row('slack.thread-hours').querySelector('.adm-n .adm-dot').getAttribute('aria-label').endsWith(': 2'), 'a value changed from the default has a dot');
  assert.ok(!row('slack.channel').querySelector('.adm-dot'), 'a default value has none');
  assert.strictEqual(row('slack.channel').querySelector('.adm-channel-name').textContent, 'Posts to #alerts', 'a channel id shows the channel name under it');
  const meta = row('slack.thread-hours').querySelector('.adm-m');
  assert.ok(meta.querySelector('.tag.k-act') && meta.querySelector('.adm-reset'), 'source tag and Reset sit in one column');
  assert.ok(row('slack.channel').querySelector('.adm-m .tag.plain'), 'a default row has its source tag there too');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'rows show the unit beside a number, bool settings as switches, a dot when changed from the default, and source tag with Reset in one column';
}

async function aSwitchSavesLikeAnyOtherRowAndSurvivesARepaint() {
  //given
  const {w, doc, errors, calls} = await boot('#t=settings');
  const row = (key) => doc.querySelector('#admin .adm-row[data-key="' + key + '"]');
  doc.querySelector('#admin details.adm-grp[data-group="Slack"]').open = true;
  const save = row('slack.state-notes').querySelector('.adm-save');

  //when
  row('slack.state-notes').querySelector('.adm-sw').click();
  row('slack.thread-hours').querySelector('.adm-in').value = '8';
  row('slack.thread-hours').querySelector('.adm-in').dispatchEvent(new w.Event('input'));
  const enabled = !save.disabled;
  row('slack.thread-hours').querySelector('.adm-save').click();
  await wait(60);

  //then
  assert.ok(enabled, 'flipping a switch enables Save');
  assert.ok(row('slack.state-notes').querySelector('.adm-sw').checked, 'the unsaved switch keeps its new state through the repaint after another save');
  assert.ok(!row('slack.state-notes').querySelector('.adm-save').disabled);

  //when
  row('slack.state-notes').querySelector('.adm-save').click();
  await wait(60);

  //then
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'PUT').map((c) => c[2]), [{key: 'slack.thread-hours', value: '8'}, {key: 'slack.state-notes', value: 'true'}]);
  assert.strictEqual(row('slack.state-notes').querySelector('.adm-msg').textContent, 'Saved, live within 15 s on every pod');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a switch enables Save, keeps an unsaved flip through a repaint, and saves true or false';
}

async function theFilterHidesEmptyCardsAndSaysWhenNothingMatches() {
  //given
  const {w, doc, errors} = await boot('#t=settings');
  const find = doc.getElementById('adm-find');

  //when
  find.value = 'per hour';
  find.dispatchEvent(new w.Event('input'));

  //then
  const seen = [...doc.querySelectorAll('#admin .adm-row')].filter((r) => !r.hidden).map((r) => r.dataset.key);
  assert.deepStrictEqual(seen, ['slack.ask-cap']);
  assert.deepStrictEqual([...doc.querySelectorAll('#admin .adm-card')].filter((c) => !c.hidden).map((c) => c.dataset.card), ['Questions']);
  assert.ok(doc.getElementById('adm-none').hidden);

  //when
  find.value = 'zzz nothing';
  find.dispatchEvent(new w.Event('input'));

  //then
  const none = doc.getElementById('adm-none');
  assert.ok(!none.hidden && none.textContent.startsWith('No setting matches'), none.textContent);

  //when
  find.value = 'slack.thread';
  find.dispatchEvent(new w.Event('input'));

  //then
  assert.deepStrictEqual([...doc.querySelectorAll('#admin .adm-row')].filter((r) => !r.hidden).map((r) => r.dataset.key), ['slack.thread-hours'], 'the key matches too');
  assert.ok(doc.getElementById('adm-none').hidden);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the settings filter matches label, key and help across cards, hides empty cards and says when nothing matches';
}

async function theInvestigatorTabGroupsItsRowsIntoCards() {
  //given
  const {w, doc, errors} = await boot('#t=ai');

  //then
  assert.deepStrictEqual([...doc.querySelectorAll('#ai-rows .adm-card-h')].map((h) => h.firstChild.textContent), ['Models', 'Spend and limits']);
  assert.strictEqual(doc.querySelector('#ai-rows .adm-row[data-key="investigate.daily-usd"] .adm-num.pre .adm-unit').textContent, '$');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'Admin Investigator shows its settings in titled cards, a dollar limit with the $ before the field';
}


async function aHistoryLineRevertsTheRowInItsCard() {
  //given
  const {w, doc, errors, calls} = await boot('#t=settings');
  const row = () => doc.querySelector('#admin .adm-row[data-key="slack.thread-hours"]');
  row().querySelector('.adm-in').value = '8';
  row().querySelector('.adm-in').dispatchEvent(new w.Event('input'));
  row().querySelector('.adm-save').click();
  await wait(60);

  //when
  doc.getElementById('tab-changes').click();
  await wait(80);
  const line = doc.querySelector('#changes .adm-hist tbody tr');
  const named = line.children[2].textContent;
  line.querySelector('.adm-revert').click();
  await wait(60);

  //then
  assert.strictEqual(named, 'Thread window (h)', 'the History line names the setting by its full label');
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'PUT').map((c) => c[2]), [{key: 'slack.thread-hours', value: '8'}, {key: 'slack.thread-hours', value: '6'}]);
  assert.strictEqual(row().closest('.adm-card').dataset.card, 'Threads and reminders');
  assert.strictEqual(row().querySelector('.adm-in').value, '6', 'Revert repaints the row inside its card');
  assert.strictEqual(doc.getElementById('adm-ch-msg').textContent, 'Reverted Thread window (h)');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a History line names the setting and its Revert repaints the row inside its card';
}

(async () => {
  for (const t of [eachSectionSplitsIntoTitledCardsInTableOrder, rowsShowUnitsSwitchesAChangedDotAndOneMetaColumn, aSwitchSavesLikeAnyOtherRowAndSurvivesARepaint,
    theFilterHidesEmptyCardsAndSaysWhenNothingMatches, theInvestigatorTabGroupsItsRowsIntoCards, aHistoryLineRevertsTheRowInItsCard]) console.log('PASS ' + await t());
})().catch((e) => { console.log('FAIL ' + e.message); console.error(e); process.exit(1); });
