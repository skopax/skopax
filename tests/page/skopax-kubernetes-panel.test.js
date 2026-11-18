const fs = require('fs');
const path = require('path');
const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const PANEL_JS = fs.readFileSync(path.join(__dirname, '..', '..', 'skopax', 'plugins', 'kubernetes', 'panel.js'), 'utf8');
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const SLOT = '<script type="text/plain" id="plugin-scripts"></script>';
const ICON = '/plugins/kubernetes/icon.svg';
const PLUGINS = [{id: 'kubernetes', name: 'Kubernetes', icon: ICON, tab: 'nodes', passes: ['nodes'], instances: [], pages: [{tab: 'nodes', label: 'Nodes', group: 'observe', icon: ICON}]}];
const EVIL = '<img src=x onerror="window.__evil=1">';

function summary(over) {
  return Object.assign({total: 0, ready: 0, notReady: [], pressure: [], unschedulable: [], nodes: []}, over);
}

function nodeRows() {
  return [
    {name: 'local', role: 'master', status: 'Ready', since: iso(120), lastSeen: iso(1), via: 'in-cluster', plugin: 'kubernetes',
      clusterSummary: summary({total: 6, ready: 5, notReady: ['node-3'], pressure: ['node-2: DiskPressure'],
        nodes: [{name: 'node-3', ready: 'False', readySince: iso(30), roles: ['worker'], kubelet: 'v1.30.1', created: iso(3000)}]})},
    {name: 'hub', role: 'agent', status: 'Ready', since: iso(600), lastSeen: iso(1), via: 'https://hub.example.com', version: 'abc12345', release: '1.0', startedAt: iso(1500),
      latencyMs: 42, relaysFor: ['east'], plugin: 'kubernetes', clusterSummary: summary({total: 2, ready: 2})},
    {name: 'east', role: 'agent', status: 'NotReady', since: iso(12), lastSeen: iso(13), error: 'no answer', via: 'https://east.example.com', relay: 'hub',
      version: 'abc12345', release: '1.0', stale: true, plugin: 'kubernetes', clusterSummary: summary({total: 3, ready: 3, unschedulable: ['east-worker-1']})},
    {name: 'west', role: 'agent', status: 'NotReady', since: iso(30), lastSeen: null, error: 'no answer', via: 'https://west.example.com', plugin: 'kubernetes'},
  ];
}

function stateWith(extra) {
  return Object.assign({rev: 5, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'ok', headline: 'x', points: [], at: iso(1)}, anomalies: [], services: [], history: [], routes: [], deploys: []}, extra);
}

async function boot(state) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  vc.on('error', (e) => errors.push('console.error ' + e));
  const panel = '<script>\n' + PANEL_JS.replace(/<\/script/gi, '<\\/script') + '\n</script>';
  const html = pageHtml(TEMPLATE).replace(SLOT, () => panel).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state).replace(/<\//g, '<\\/'));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=nodes', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
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
  const doc = dom.window.document;
  return {dom, w: dom.window, doc, errors, section: doc.getElementById('panel-nodes')};
}

function push(w, state) {
  w.document.dispatchEvent(new w.CustomEvent('skopax:state', {detail: {state}}));
}

function rowOf(section, name) {
  return [...section.querySelectorAll('tr.node-row')].filter((r) => r.dataset.node === name)[0];
}

function cells(row) {
  return [...row.children].map((td) => td.textContent.trim());
}

async function aFilledStateShowsEveryNodeItsClusterAndAStaleAgent() {
  //given
  const {dom, w, doc, errors, section} = await boot(stateWith({nodes: nodeRows()}));

  //when
  const names = [...section.querySelectorAll('tr.node-row')].map((r) => r.dataset.node);
  const local = cells(rowOf(section, 'local')), hub = cells(rowOf(section, 'hub')), east = cells(rowOf(section, 'east')), west = cells(rowOf(section, 'west'));
  const closed = [...section.querySelectorAll('tr.node-more')].every((r) => r.hidden);
  rowOf(section, 'east').dispatchEvent(new w.MouseEvent('click', {bubbles: true}));
  const opened = rowOf(section, 'east').nextElementSibling;
  rowOf(section, 'local').dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
  const localMore = rowOf(section, 'local').nextElementSibling;

  //then
  assert.deepStrictEqual(names, ['local', 'hub', 'east', 'west']);
  assert.deepStrictEqual([local[1], local[2]], ['master', 'Ready']);
  assert.ok(local[6].includes('5/6 ready') && local[6].includes('1 not ready') && local[6].includes('1 under pressure'), local[6]);
  assert.ok(hub[0].includes('relay') && east[0].includes('through hub'), hub[0] + ' | ' + east[0]);
  assert.deepStrictEqual([east[2], west[2]], ['NotReady', 'Waiting for first contact']);
  assert.ok(east[6].includes('stale') && east[6].includes('1 cordoned') && east[4].includes('abc12345'), east.join(' | '));
  assert.strictEqual(west[5], 'never');
  assert.strictEqual(doc.getElementById('count-nodes').textContent, '2/3', 'Ready out of the nodes that answered at least once');
  assert.ok(doc.getElementById('count-nodes').classList.contains('k-crit') && !doc.getElementById('count-nodes').classList.contains('k-ok'), 'a NotReady agent marks the tab');
  assert.strictEqual(doc.getElementById('count-nodes').title, 'NotReady: east \u00b7 waiting for first contact: west');
  assert.deepStrictEqual([local[3], local[4]], ['\u2013', '0.1.0'], 'the in-cluster row shows the service version from the page and no age');
  assert.ok(closed, 'the detail rows start closed');
  assert.ok(!opened.hidden && rowOf(section, 'east').getAttribute('aria-expanded') === 'true', 'a click opens the detail row');
  assert.ok(opened.textContent.includes('no answer') && opened.textContent.includes('Reached through https://east.example.com by way of the hub agent (relay)'), opened.textContent);
  assert.ok(!localMore.hidden && localMore.textContent.includes('node-3') && localMore.textContent.includes('v1.30.1'), 'Enter opens the in-cluster row with its cluster nodes');
  assert.ok(section.textContent.includes('every 5 min'));
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a filled state lists every node with its cluster, relays and a stale agent, and a click or Enter opens its details';
}

async function aLiveStateRepaintsAndKeepsAnOpenRowOpen() {
  //given
  const {dom, w, doc, errors, section} = await boot(stateWith({nodes: nodeRows()}));
  rowOf(section, 'east').dispatchEvent(new w.MouseEvent('click', {bubbles: true}));

  //when
  const back = nodeRows().map((n) => n.name === 'east' ? Object.assign({}, n, {status: 'Ready', since: iso(0), lastSeen: iso(0), stale: undefined, error: undefined}) : n);
  push(w, stateWith({rev: 6, nodes: back}));

  //then
  const east = rowOf(section, 'east');
  assert.strictEqual(cells(east)[2], 'Ready');
  assert.ok(east.getAttribute('aria-expanded') === 'true' && !east.nextElementSibling.hidden, 'the open row stays open after a repaint');
  assert.strictEqual(doc.getElementById('count-nodes').textContent, '3/3');
  assert.ok(doc.getElementById('count-nodes').classList.contains('k-ok') && !doc.getElementById('count-nodes').classList.contains('k-crit'));
  assert.strictEqual(doc.getElementById('count-nodes').title, 'every node answered its last heartbeat \u00b7 waiting for first contact: west');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a live state repaints the panel and keeps an open row open';
}

async function anAgentKnownFromItsOpenCardIsDownNotWaiting() {
  //given
  const south = {name: 'south', role: 'agent', status: 'NotReady', since: iso(40), lastSeen: null, seenBefore: true, error: 'no answer', via: 'https://south.example.com', plugin: 'kubernetes'};
  const {dom, doc, errors, section} = await boot(stateWith({nodes: [south]}));

  //when
  const row = cells(rowOf(section, 'south'));

  //then
  assert.deepStrictEqual([row[2], row[5]], ['NotReady', '\u2013'], 'an agent with an open card has answered before, so it is down, not waiting');
  assert.strictEqual(doc.getElementById('count-nodes').textContent, '0/1');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'an agent known only from its open card shows NotReady, not waiting for first contact';
}

async function aRowNamedConstructorStartsClosed() {
  //given
  const odd = ['constructor', 'toString', '__proto__'].map((name) => ({name, role: 'agent', status: 'Ready', since: iso(5), lastSeen: iso(1), via: 'https://' + name.replace(/_/g, '') + '.example.com', plugin: 'kubernetes'}));
  const {dom, errors, section} = await boot(stateWith({nodes: odd}));

  //when
  const rows = [...section.querySelectorAll('tr.node-row')];

  //then
  assert.deepStrictEqual(rows.map((r) => [r.dataset.node, r.getAttribute('aria-expanded'), r.nextElementSibling.hidden]),
    [['constructor', 'false', true], ['toString', 'false', true], ['__proto__', 'false', true]]);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'rows named like object members start closed';
}

async function anEmptyStateSaysNoHeartbeatYet() {
  //given
  const {dom, doc, errors, section} = await boot(stateWith({}));

  //then
  assert.ok(section.textContent.includes('No heartbeat yet'), section.textContent);
  assert.strictEqual(section.querySelector('table'), null);
  assert.strictEqual(doc.getElementById('count-nodes').textContent, '\u2013');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a state without node rows says no heartbeat yet and shows no table';
}

async function anEvilRowIsRenderedAsText() {
  //given
  const evil = {name: EVIL, role: 'agent', status: 'NotReady', since: iso(20), lastSeen: iso(21), error: EVIL, via: EVIL, relay: EVIL, version: EVIL, plugin: 'kubernetes',
    clusterSummary: summary({total: 1, ready: 0, notReady: [EVIL], nodes: [{name: EVIL, ready: 'False', kubelet: EVIL, roles: [EVIL]}]})};
  const browser = [{name: 'browser', ok: false, version: EVIL, lastSeen: iso(3), error: EVIL, plugin: 'website'}];
  const {dom, w, errors, section} = await boot(stateWith({nodes: [evil], browser}));

  //when
  rowOf(section, EVIL).dispatchEvent(new w.MouseEvent('click', {bubbles: true}));
  await wait(50);

  //then
  assert.strictEqual(section.querySelector('img'), null, 'no element comes out of a row value');
  assert.strictEqual(section.querySelector('[onerror]'), null);
  assert.ok(section.textContent.includes(EVIL), 'the value shows as text');
  assert.strictEqual(w.__evil, undefined);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'an EVIL row and an EVIL browser row are rendered as text';
}

async function aBrowserRowFromTheWebsiteSectionAndNoneWithoutIt() {
  //given
  const nodes = nodeRows().slice(0, 1);
  const up = [{name: 'browser', ok: true, version: 'HeadlessChrome/131.0.6778.85', lastSeen: iso(2), plugin: 'website'}, {name: 'browser', ok: true, version: 'Other/1.0', lastSeen: iso(2), plugin: 'other'}];
  const {dom, w, errors, section} = await boot(stateWith({nodes, browser: up}));
  const first = [...section.querySelectorAll('tr.node-browser')].map(cells);

  //when
  push(w, stateWith({rev: 6, nodes, browser: [{name: 'browser', ok: false, version: 'HeadlessChrome/131.0.6778.85', lastSeen: iso(9), error: 'Unreachable', plugin: 'website'}]}));
  const down = [...section.querySelectorAll('tr.node-browser')];
  const downCells = down.map(cells);
  const tip = down[0].children[2].querySelector('[title]').getAttribute('title');
  push(w, stateWith({rev: 7, nodes}));
  const gone = section.querySelectorAll('tr.node-browser').length;

  //then
  assert.strictEqual(first.length, 1, 'only the Website plug-in\'s browser row shows');
  assert.deepStrictEqual([first[0][0], first[0][1], first[0][2], first[0][4], first[0][5]], ['Browser', 'browser', 'reachable', 'HeadlessChrome/131.0.6778.85', '2 min ago']);
  assert.deepStrictEqual([downCells[0][2], downCells[0][5]], ['not reachable', '9 min ago']);
  assert.ok(tip.includes('Unreachable'), tip);
  assert.strictEqual(gone, 0, 'no browser section, no browser row');
  assert.strictEqual(rowOf(section, 'local') != null, true);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a browser row from the website section, and none without it';
}

async function aRelayTagIsColouredLikeTheOtherTagsInBothThemes() {
  //given
  const {dom, doc, errors, section} = await boot(stateWith({nodes: nodeRows()}));
  const css = fs.readFileSync(TEMPLATE, 'utf8');

  //then
  const rules = [...doc.styleSheets].flatMap((sh) => [...sh.cssRules]).filter((r) => /\.node-relay\b/.test(r.selectorText || ''));
  assert.deepStrictEqual(rules.map((r) => r.selectorText), ['.tag.node-relay']);
  assert.match(css, /\.tag\.node-relay \{ --c: var\(--info\); --cb: var\(--info-bg\); \}/);
  assert.ok([...css.matchAll(/--info: #[0-9a-f]{6}; --info-bg: [^;]+;/g)].length >= 3, 'the light theme and both dark themes define the colours the rule uses');
  const tags = [...section.querySelectorAll('.node-relay')];
  assert.deepStrictEqual(tags.map((t) => [t.className, t.textContent]), [['tag node-relay', 'relay'], ['tag node-relay', 'through hub']]);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a relay tag has its own colour rule like the other tags, from tokens both themes define';
}

async function aRepaintKeepsTheKeyboardFocusOnItsRowAndThePanelNeedsNoPageHelpers() {
  //given
  const {dom, w, doc, errors, section} = await boot(stateWith({nodes: nodeRows()}));
  rowOf(section, 'east').focus();

  //when
  push(w, stateWith({rev: 7, nodes: nodeRows()}));

  //then
  assert.strictEqual(doc.activeElement, rowOf(section, 'east'), 'the focused row keeps the keyboard focus after a live repaint');
  for (const name of ['esc', 'hm', 'ago']) assert.ok(new RegExp('function ' + name + '\\(').test(PANEL_JS), name + ' is defined by the panel itself');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a live repaint keeps the keyboard focus on its row, and the panel defines its own helpers';
}

(async () => {
  for (const t of [aFilledStateShowsEveryNodeItsClusterAndAStaleAgent, aLiveStateRepaintsAndKeepsAnOpenRowOpen, aRepaintKeepsTheKeyboardFocusOnItsRowAndThePanelNeedsNoPageHelpers, anAgentKnownFromItsOpenCardIsDownNotWaiting, aRowNamedConstructorStartsClosed,
    anEmptyStateSaysNoHeartbeatYet, anEvilRowIsRenderedAsText, aBrowserRowFromTheWebsiteSectionAndNoneWithoutIt, aRelayTagIsColouredLikeTheOtherTagsInBothThemes]) {
    console.log('PASS ' + (await t()));
  }
})().catch((e) => { console.error('FAIL', e.message); process.exit(1); });
