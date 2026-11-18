const fs = require('fs');
const path = require('path');
const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const PANEL_JS = fs.readFileSync(path.join(__dirname, '..', '..', 'skopax', 'plugins', 'airbyte', 'panel.js'), 'utf8');
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const SLOT = '<script type="text/plain" id="plugin-scripts"></script>';
const URL = 'https://airbyte.example.com';
const WS = '0a1b2c3d-0000-4000-8000-00000000a001';
const ORDERS = '1a2b3c4d-1111-4111-8111-000000000001';
const EVENTS = '1a2b3c4d-1111-4111-8111-000000000002';
const CLICKS = '44444444-5555-6666-7777-888888888888';
const SHOP = '11111111-2222-3333-4444-555555555555';
const EVIL = '<img src=x onerror="window.__evil=1">';
const PLUGINS = [{id: 'airbyte', name: 'Airbyte', icon: '/plugins/airbyte/icon.svg', tab: 'airbyte', passes: ['airbyte'], instances: [],
  pages: [{tab: 'airbyte', label: 'Airbyte', group: 'observe', icon: '/plugins/airbyte/icon.svg'}]}];
const timeline = (id) => URL + '/workspaces/' + WS + '/connections/' + id + '/timeline';

function base(extra) {
  return Object.assign({rev: 5, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 14 * 60000).toISOString(), sources: {airbyte: 'ok'},
    summary: {level: 'ok', headline: 'x', points: [], at: iso(1)}, anomalies: [], services: [], history: [], routes: [], deploys: []}, extra);
}

function filled() {
  return base({
    airbyte: [
      {id: EVENTS, name: 'Events to Lake', workspaceId: WS, connStatus: 'active', schedule: null, usualS: 510, usualRows: 475, okRuns: 2, lastOk: iso(50), url: timeline(EVENTS),
        job: 20, jobUrl: timeline(EVENTS) + '?jobId=20&openLogs=true', status: 'succeeded', start: iso(50), end: iso(42), durS: 480, rows: 500, plugin: 'airbyte'},
      {id: ORDERS, name: 'Orders to Warehouse', workspaceId: WS, connStatus: 'active', schedule: 'cron 0 */2 * * *', usualS: 720, usualRows: 900, okRuns: 5, lastOk: iso(120), url: timeline(ORDERS),
        job: 9, jobUrl: timeline(ORDERS) + '?jobId=9&openLogs=true', status: 'running', start: iso(90), end: null, durS: 5400, rows: 1400, plugin: 'airbyte'},
      {id: CLICKS, name: 'Clicks to Lake', workspaceId: WS, connStatus: 'active', schedule: null, usualS: null, usualRows: null, okRuns: 0, lastOk: null, url: timeline(CLICKS),
        job: 41, jobUrl: timeline(CLICKS) + '?jobId=41&openLogs=true', status: 'failed', start: iso(10), end: iso(5), durS: 300, rows: 0, plugin: 'airbyte', cluster: 'east'},
      {id: 'Ledger Changes', name: 'Ledger Changes', workspaceId: null, connStatus: 'not listed', schedule: null, usualS: null, usualRows: null, okRuns: 0, lastOk: null, url: null,
        missing: true, plugin: 'airbyte'},
      {id: SHOP, name: 'Shop to Lake', workspaceId: WS, connStatus: 'active', schedule: null, usualS: null, usualRows: null, okRuns: 0, lastOk: null, url: timeline(SHOP),
        jobsDenied: true, plugin: 'airbyte'},
    ],
    anomalies: [
      {key: 'airbyte|Orders to Warehouse|long-run', src: 'airbyte', sig: 'airbyte-long-run', ns: '-', svc: 'Orders to Warehouse', sub: 'sync', sev: 'warn', status: 'open', label: 'sync running long',
        short: 'WARN Airbyte Orders to Warehouse running 90 min (usual 12 min)', what: 'x', now: 5400, unit: 'dur', firstSeen: iso(30), lastSeen: iso(1)},
      {key: 'airbyte|Clicks to Lake|failed', src: 'airbyte', sig: 'airbyte-failed', ns: '-', svc: 'Clicks to Lake', sub: 'sync', sev: 'warn', status: 'open', label: 'sync failed', cluster: 'east', ranKey: 'airbyte-east',
        short: 'WARN EAST Airbyte Clicks to Lake sync failed after 5 min', what: 'x', now: 300, unit: 'dur', firstSeen: iso(5), lastSeen: iso(1)},
      {key: 'airbyte|Events to Lake|failed', src: 'airbyte', sig: 'airbyte-failed', ns: '-', svc: 'Events to Lake', sub: 'sync', sev: 'warn', status: 'cleared', label: 'sync failed',
        short: 'x', what: 'x', now: 1, unit: 'dur', firstSeen: iso(300), lastSeen: iso(200)},
    ],
  });
}

async function boot(state) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  vc.on('error', (e) => errors.push('console.error ' + e));
  const html = pageHtml(TEMPLATE).replace(SLOT, '<script>\n' + PANEL_JS.replace(/<\/script/gi, '<\\/script') + '</script>').replace('/*__SKOPAX_DATA__*/', JSON.stringify(state).replace(/<\//g, '<\\/'));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=airbyte', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        const s = String(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (s.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', operator: true, admin: false, teams: [], tags: []});
        if (s.startsWith('/api/server')) return ok({plugins: PLUGINS, tz: 'UTC'});
        if (s.startsWith('/api/admin/settings')) return ok({items: [], rev: 1, refreshSeconds: 15});
        if (s.startsWith('/api/actions') || s.startsWith('/api/deploy-reports') || s.startsWith('/api/requests')) return ok([]);
        if (s.startsWith('/api/rev')) return ok({rev: state.rev});
        if (s.startsWith('/api/state')) return ok(state);
        return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
      };
    },
  });
  await wait(400);
  return {dom, w: dom.window, doc: dom.window.document, errors};
}

function cells(tr) {
  return [...tr.querySelectorAll('td')].map((td) => td.textContent.replace(/\s+/g, ' ').trim());
}

async function aFilledStateListsEachSyncWithItsLastRunItsOpenCardsAndItsLinks() {
  //given
  const {dom, doc, errors} = await boot(filled());

  //when
  const section = doc.getElementById('panel-airbyte');
  const rows = [...section.querySelectorAll('tbody tr')];
  const byName = Object.fromEntries(rows.map((tr) => [tr.querySelector('.svc').textContent, tr]));

  //then
  assert.ok(section, 'the plug-in page gets its section');
  assert.deepStrictEqual(Object.keys(byName), ['Events to Lake', 'Orders to Warehouse', 'Clicks to Lake', 'Ledger Changes', 'Shop to Lake'], 'rows keep the order the check stored');
  const ledger = cells(byName['Ledger Changes']);
  assert.deepStrictEqual(ledger.slice(0, 7), ['Not listed', 'Ledger Changes', '\u2013', '\u2013', '\u2013', '\u2013', ''], 'a watched connection the listing lacks shows as not listed, never as not found, with no links');
  assert.strictEqual(byName['Ledger Changes'].querySelector('.state').className, 'state k-warn');
  const shop = cells(byName['Shop to Lake']);
  assert.deepStrictEqual(shop.slice(0, 2), ['No access', 'Shop to Lake'], 'a connection whose jobs the application may not read says so');
  assert.strictEqual(byName['Shop to Lake'].querySelector('.state').className, 'state k-warn');
  const events = cells(byName['Events to Lake']), orders = cells(byName['Orders to Warehouse']), clicks = cells(byName['Clicks to Lake']);
  assert.strictEqual(events[0], 'Succeeded', 'a sync with no open card shows its last job status');
  assert.deepStrictEqual(events.slice(3, 6), ['8 min', '8 min', '500']);
  assert.match(events[2], /^(\d+ [A-Z][a-z]{2} )?\d\d:\d\d\u2013\d\d:\d\d$/);
  assert.strictEqual(orders[0], 'Warning sync running long', 'an open card shows its severity and its label');
  assert.match(orders[2], /^(\d+ [A-Z][a-z]{2} )?\d\d:\d\d\u2013now$/, 'a running job has no end');
  assert.deepStrictEqual(orders.slice(3, 6), ['90 min', '12 min', '1,400']);
  assert.strictEqual(byName['Orders to Warehouse'].querySelector('.ns').textContent, 'cron 0 */2 * * *');
  const tag = byName['Orders to Warehouse'].querySelector('.sig-tags a');
  assert.strictEqual(tag.getAttribute('href'), '#a=' + encodeURIComponent('airbyte|Orders to Warehouse|long-run'), 'the card label opens the card');
  assert.strictEqual(tag.getAttribute('title'), 'WARN Airbyte Orders to Warehouse running 90 min (usual 12 min)');
  const links = [...byName['Orders to Warehouse'].querySelectorAll('td:last-child a')].map((a) => [a.textContent, a.getAttribute('href'), a.getAttribute('target')]);
  assert.deepStrictEqual(links, [['Timeline \u2197', timeline(ORDERS), '_blank'], ['Job 9 \u2197', timeline(ORDERS) + '?jobId=9&openLogs=true', '_blank']]);
  assert.strictEqual(clicks[0], 'Warning sync failed', 'a row of another instance matches only the cards of that instance');
  assert.strictEqual(byName['Clicks to Lake'].querySelector('.ns').textContent, 'east');
  assert.deepStrictEqual(clicks.slice(3, 6), ['5 min', '\u2013', '0']);
  const all = section.querySelector('.panel-note a.lnk');
  assert.strictEqual(all.getAttribute('href'), URL + '/workspaces/' + WS + '/connections');
  assert.strictEqual(section.querySelector('.panel-note span').textContent, 'The last sync job of each Airbyte connection the check watches.');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a filled state lists each sync with its last run, its open cards of the same instance and its timeline and job links';
}

async function anEmptyStateSaysTheCheckFillsItAndAFailedCheckSaysSo() {
  //given
  const {dom, w, doc, errors} = await boot(base({}));
  const section = doc.getElementById('panel-airbyte');
  const empty = section.querySelector('tbody').textContent.trim();
  const noLink = section.querySelector('.panel-note a.lnk');

  //when
  w.document.dispatchEvent(new w.CustomEvent('skopax:state', {detail: {state: base({sources: {airbyte: 'error: POST /applications/token HTTP 401'}})}}));
  const failed = section.querySelector('tbody').textContent.trim();
  const stale = filled();
  stale.sources = {airbyte: 'ok', 'airbyte-east': 'error: GET /connections unreachable'};
  w.document.dispatchEvent(new w.CustomEvent('skopax:state', {detail: {state: stale}}));
  const kept = section.querySelector('.panel-note span').textContent;
  const keptRows = section.querySelectorAll('tbody tr').length;

  //then
  assert.strictEqual(empty, 'No Airbyte data yet. The Airbyte check fills this on its next run.');
  assert.strictEqual(noLink, null, 'no rows, no workspace link');
  assert.strictEqual(failed, 'Airbyte did not answer on the last check (error: POST /applications/token HTTP 401).');
  assert.strictEqual(kept, 'The last sync job of each Airbyte connection the check watches. Airbyte did not answer on the last check (east: error: GET /connections unreachable), so these rows are from the check before.');
  assert.strictEqual(keptRows, 5);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'an empty state says the check fills it, a failed check says so and names the instance, and earlier rows stay';
}

async function aRefusedReadIsNamedInTheNote() {
  //given
  const state = filled();
  state.sources = {airbyte: 'ok \u00b7 no access to jobs', 'airbyte-east': 'ok'};

  //when
  const {dom, doc, errors} = await boot(state);
  const note = doc.getElementById('panel-airbyte').querySelector('.panel-note span').textContent;

  //then
  assert.strictEqual(note, 'The last sync job of each Airbyte connection the check watches. Airbyte refused a read on the last check (ok \u00b7 no access to jobs), so some rows may be from the check before.');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a refused read on the last check is named in the note';
}

async function anEvilRowIsShownAsText() {
  //given
  const state = base({
    airbyte: [{id: EVIL, name: EVIL, schedule: EVIL, url: 'javascript:alert(1)', job: EVIL, jobUrl: 'javascript:alert(2)', status: EVIL, start: EVIL, end: null, durS: EVIL, usualS: null, rows: EVIL, okRuns: EVIL, usualRows: EVIL, cluster: EVIL}],
    anomalies: [{key: 'airbyte|' + EVIL + '|failed', src: 'airbyte', sig: 'airbyte-failed', ns: '-', svc: EVIL, sub: 'sync', sev: 'warn', status: 'open', label: EVIL, short: EVIL, cluster: EVIL,
      what: 'x', now: 1, unit: 'dur', firstSeen: iso(5), lastSeen: iso(1)}],
    sources: {airbyte: 'ok', 'airbyte-x': EVIL},
  });

  //when
  const {dom, w, doc, errors} = await boot(state);
  const section = doc.getElementById('panel-airbyte');

  //then
  assert.strictEqual(section.querySelectorAll('img').length, 0, 'no markup from the state reaches the page');
  assert.strictEqual(w.__evil, undefined);
  assert.ok(section.querySelector('.svc').textContent === EVIL && section.querySelector('.sig-tags a').textContent === EVIL);
  assert.ok(section.querySelector('.panel-note span').textContent.includes(EVIL));
  assert.deepStrictEqual([...section.querySelectorAll('a')].map((a) => a.getAttribute('href')).filter((h) => !h.startsWith('#a=')), [], 'a link that is not https is dropped');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'an evil row, card and status are rendered as text and a link that is not https is left out';
}

async function aRefusedFirstListingAndAnUnreadConnectionSayTheyWereNotRead() {
  //given
  const refused = await boot(base({sources: {airbyte: 'ok \u00b7 no access to connections'}, airbyte: []}));
  const partly = filled();
  partly.sources = {airbyte: 'error: jobs: GET /jobs HTTP 500'};
  partly.airbyte[0] = Object.assign({}, partly.airbyte[0], {job: undefined, status: undefined, start: undefined, end: undefined, durS: undefined, rows: undefined, jobsFailed: true});
  const read = await boot(partly);

  //when
  const empty = refused.doc.getElementById('panel-airbyte').querySelector('tbody').textContent.trim();
  const note = read.doc.getElementById('panel-airbyte').querySelector('.panel-note span').textContent;
  const first = read.doc.getElementById('panel-airbyte').querySelector('tbody tr td').textContent.trim();

  //then
  assert.strictEqual(empty, 'Not read: no access to connections.');
  assert.strictEqual(note, 'The last sync job of each Airbyte connection the check watches. Airbyte did not answer some reads on the last check (error: jobs: GET /jobs HTTP 500); a connection it could not read shows Not read.');
  assert.strictEqual(first, 'Not read');
  assert.deepStrictEqual(refused.errors.concat(read.errors), []);
  refused.dom.window.close();
  read.dom.window.close();
  return 'a refused first listing and a connection whose jobs read failed say they were not read';
}

(async () => {
  for (const t of [aFilledStateListsEachSyncWithItsLastRunItsOpenCardsAndItsLinks, anEmptyStateSaysTheCheckFillsItAndAFailedCheckSaysSo, aRefusedReadIsNamedInTheNote, anEvilRowIsShownAsText, aRefusedFirstListingAndAnUnreadConnectionSayTheyWereNotRead]) {
    console.log('PASS ' + (await t()));
  }
})().catch((e) => { console.error('FAIL', e.message); process.exit(1); });
