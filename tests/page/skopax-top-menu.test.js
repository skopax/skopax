const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const MENU = ['slack', 'hist', 'acts', 'costs', 'integrations', 'tools', 'mcp', 'review'];

async function boot(hash, me) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e).slice(0, 160)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3e6).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const dom = new JSDOM(pageHtml(process.argv[2]).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {url: 'https://skopax.example.com/' + (hash || ''), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        p = String(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me')) return ok(Object.assign({loginEnabled: true, user: 'alice@example.com', operator: true, admin: true, teams: [], tags: [], chat: true}, me || {}));
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/mcp/info')) return ok({enabled: true, url: 'https://skopax.example.com/mcp', clientId: 'skopax-mcp', waitSeconds: 45, diveCap: 6, tools: [{name: 'list_cards', title: 'List cards', what: 'List Skopax cards like the Anomalies tab.', readOnly: true}, {name: 'take_card', title: 'Take card', what: 'Take a card: it goes In progress with you as owner.', readOnly: false}]});
        return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
      };
    }});
  await wait(300);
  return {dom, w: dom.window, doc: dom.window.document, errors};
}

const current = (doc) => [...doc.querySelectorAll('#side a.tab[data-tab][aria-current="page"]')].map((a) => a.dataset.tab);

async function theOldSkopaxMenuTabsSitInTheActivityAndConnectGroups() {
  //given
  const {dom, w, doc, errors} = await boot();
  const rows = (grp) => [...doc.querySelectorAll('#snav .s-grp[data-grp="' + grp + '"] a.tab[data-tab]')].map((t) => t.dataset.tab);

  //then
  assert.deepStrictEqual(rows('activity').concat(rows('connect'), rows('more')), MENU.concat(['docs']), 'the old Skopax menu tabs keep their order across Activity, Connect and More, with Docs last');
  assert.ok(!doc.getElementById('menu-product') && !doc.getElementById('tabs-product') && !doc.getElementById('toptabs'), 'no top-bar menu any more');
  assert.ok(!doc.getElementById('tabs-misc') && MENU.every((id) => !doc.querySelector('#tabs [data-tab="' + id + '"]')), 'the filter column has no Misc group');
  assert.deepStrictEqual([...doc.querySelectorAll('#tabs .tabs-l')].map((l) => l.textContent), ['Anomaly filters'], 'the filter column keeps only the anomaly filters');

  //when
  doc.getElementById('tab-costs').click();
  await wait(50);

  //then
  assert.ok(!doc.getElementById('panel-costs').hidden && doc.getElementById('tab-costs').getAttribute('aria-current') === 'page', 'picking Costs opens the Costs tab');
  assert.strictEqual(doc.getElementById('crumb-page').textContent, 'Costs', 'and the crumb names it');
  assert.strictEqual(w.location.hash, '#t=costs');

  //when
  doc.getElementById('tab-anoms').click();
  await wait(50);

  //then
  assert.ok(!doc.getElementById('tab-costs').hasAttribute('aria-current') && doc.getElementById('crumb-page').textContent === 'Board', 'another row clears the mark');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS the old Skopax menu tabs sit in order in the Activity, Connect and More groups; a pick opens the tab, marks its row and names it in the crumb, and another row clears the mark');
}

async function aRowKeepsItsTooltipOnFocus() {
  //given
  const {dom, doc, errors} = await boot();
  const row = doc.getElementById('tab-acts'), tip = doc.getElementById('tipbox');

  //when
  row.focus();
  await wait(20);

  //then
  assert.ok(!tip.hidden && tip.textContent === 'nothing queued', 'a row shows its tooltip on focus: ' + tip.textContent);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS a sidebar row still shows its tooltip on focus');
}

async function aLinkToAFormerMenuTabOpensItAndTheCrumbNamesIt() {
  //given
  const {dom, doc, errors} = await boot('#t=hist');

  //then
  assert.ok(!doc.getElementById('panel-hist').hidden, 'the Events link opens the Events tab');
  assert.deepStrictEqual(current(doc), ['hist']);
  assert.strictEqual(doc.getElementById('crumb-page').textContent, 'Events');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS a #t= link to a tab of the old Skopax menu opens it, marks its row and names it in the crumb');
}

async function aFoldedMoreCountsItsHotRowsAndNamesThemInItsTooltip() {
  //given
  const {dom, doc, errors} = await boot();
  const more = doc.getElementById('side-more'), sum = () => more.querySelector('.g-sum');
  const plain = [sum().textContent, sum().className];

  //when
  doc.getElementById('tab-review').className = 'tab k-warn hot';
  doc.getElementById('count-review').textContent = '2';
  doc.getElementById('tab-docs').className = 'tab k-crit hot';
  await wait(20);

  //then
  assert.deepStrictEqual(plain, ['2', 'g-sum'], 'a folded More counts its rows while nothing inside needs attention');
  assert.deepStrictEqual([sum().textContent, sum().className], ['2', 'g-sum hot k-crit'], 'it counts its hot rows in the colour of the most urgent');
  assert.strictEqual(more.title, 'More folded: Review 2, Docs', 'the tooltip names the group and the rows that need attention');

  //when
  more.click();
  const open = sum().textContent;
  more.click();
  doc.getElementById('tab-review').className = 'tab k-act';
  doc.getElementById('tab-docs').className = 'tab';
  await wait(20);

  //then
  assert.strictEqual(open, '', 'an open More shows its rows instead');
  assert.deepStrictEqual([sum().textContent, sum().className], ['2', 'g-sum'], 'and goes plain again');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS a folded More counts its rows or its hot rows and its tooltip names the rows that need attention');
}

async function connectClaudeExplainsTheMcpServerAndHowToAddIt() {
  //given
  const {dom, w, doc, errors} = await boot('#t=mcp');
  let copied = null;
  w.navigator.clipboard = {writeText: async (t) => { copied = t; }};
  const box = doc.getElementById('mcp-info');

  //when
  const cmd = doc.getElementById('mcp-cmd').textContent;
  const tools = [...box.querySelectorAll('.mcp-tools tbody tr')].map((r) => [...r.children].map((c) => c.textContent));
  doc.getElementById('mcp-copy').click();
  await wait(20);

  //then
  assert.ok(!doc.getElementById('panel-mcp').hidden && doc.getElementById('crumb-page').textContent === 'Connect MCP' && current(doc).join() === 'mcp', 'Connect MCP is its own row');
  assert.strictEqual(doc.getElementById('count-mcp').textContent, '', 'no count next to it');
  assert.deepStrictEqual([...doc.querySelectorAll('.tab .b-new')], [], 'no row is flagged new');
  assert.ok(!/plugin/i.test(box.textContent), 'no plug-in install route is described');
  assert.ok(box.querySelector('ol li b').textContent.startsWith('Claude Code, by hand'), 'the manual command is the first way');
  assert.strictEqual(cmd, 'claude mcp add --scope user --transport http --client-id skopax-mcp skopax https://skopax.example.com/mcp', 'the manual command uses the server address and client id');
  assert.strictEqual(copied, cmd, 'Copy puts the command on the clipboard');
  assert.match(box.textContent, /sign in with your account/);
  assert.deepStrictEqual(tools, [['list_cards', 'List Skopax cards like the Anomalies tab.', 'read'], ['take_card', 'Take a card: it goes In progress with you as owner.', 'change operators and admins']], 'the tool list comes from the server, a write tool marked for operators and admins');
  assert.match(box.textContent, /6 an hour per person/);
  assert.match(box.textContent, /Known noise is not offered/);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS Connect MCP explains the MCP server, the team plugin and a copyable manual command, the login, the tools from the server and the limits');
}

async function theSettingsGearIsAnInlineIcon() {
  //given
  const {dom, doc, errors} = await boot();
  const gear = doc.getElementById('tab-prefs');

  //then
  assert.ok(gear.querySelector('svg.i[aria-hidden="true"]'), 'the settings gear is an inline svg from icons.js');
  assert.strictEqual(gear.querySelector('.lbl').textContent, 'Preferences', 'its name is the row label');
  assert.ok(!doc.documentElement.outerHTML.includes('--gear'), 'the old mask variable is gone');
  assert.strictEqual(doc.getElementById('count-chat').textContent, '', 'and Chat has no count dash: it has nothing to count');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS the settings gear is an inline svg with the row label as its name, and Chat has no count dash');
}

async function theLogoAndTitleOpenTheMainPage() {
  //given
  const {dom, doc, errors} = await boot('#t=chat');

  //when
  const title = doc.querySelector('#side .s-top h1 a.home');
  const mark = doc.querySelector('#side a.mark');

  //then
  assert.ok(title && title.getAttribute('href') === '/' && title.textContent === 'Skopax', 'the Skopax title links to the main page');
  assert.strictEqual(mark, null, 'no separate logo tile: the wordmark in the title is the link');
  assert.strictEqual(title.querySelector('.wordmark').getAttribute('role'), 'img');
  assert.strictEqual(title.querySelector('.wordmark').getAttribute('aria-label'), 'Skopax');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS the Skopax wordmark at the top of the sidebar links to the main page');
}

async function theEnvironmentLabelNamesTheInstanceInTheSwitcher() {
  //given
  const plain = await boot('', {instances: {current: {name: 'skopax.example.com', letter: 'S'}, links: []}});
  const labelled = await boot('', {envLabel: 'STAGING', instances: {current: {name: 'STAGING', letter: 'S'}, links: []}});
  const name = (x) => x.doc.querySelector('#side-top #inst-btn .inst-name').textContent;

  //then
  assert.deepStrictEqual([plain, labelled].map((x) => x.doc.querySelector('header.top .env')), [null, null], 'no chip in the top bar any more');
  assert.strictEqual(labelled.doc.querySelector('header.top').firstElementChild.nextElementSibling.id, 'crumb', 'the crumb follows the hamburger');
  assert.deepStrictEqual([name(plain), name(labelled)], ['skopax.example.com', 'STAGING'], 'the label from /api/me names the current instance in the sidebar switcher, else the host');
  assert.deepStrictEqual(plain.errors.concat(labelled.errors), []);
  plain.dom.window.close();
  labelled.dom.window.close();
  console.log('PASS the environment label names the current instance in the sidebar switcher instead of a chip in front of the crumb');
}

async function aLoginWithoutTheRoleIsToldWhichLocalRoleItNeeds() {
  //given
  const {dom, doc, errors} = await boot('', {operator: false, admin: false});

  //then
  const me = doc.getElementById('user-btn');
  assert.ok(me, 'the user button shows the login');
  assert.strictEqual(me.title, 'alice@example.com \u00b7 viewer: actions need the operator role, ask an admin');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS a login without the operator role is told it needs the local operator role and to ask an admin');
}

(async () => {
  await theLogoAndTitleOpenTheMainPage();
  await theEnvironmentLabelNamesTheInstanceInTheSwitcher();
  await aLoginWithoutTheRoleIsToldWhichLocalRoleItNeeds();
  await theOldSkopaxMenuTabsSitInTheActivityAndConnectGroups();
  await aRowKeepsItsTooltipOnFocus();
  await aLinkToAFormerMenuTabOpensItAndTheCrumbNamesIt();
  await aFoldedMoreCountsItsHotRowsAndNamesThemInItsTooltip();
  await connectClaudeExplainsTheMcpServerAndHowToAddIt();
  await theSettingsGearIsAnInlineIcon();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
