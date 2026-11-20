const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const svg = (doc, html) => { const box = doc.createElement('div'); box.innerHTML = html; return box.firstChild.outerHTML; };
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const EVIL = '<img src=x onerror=alert(1)>"\'';
const FEATURE_SRC = fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'features.py'), 'utf8');
const TABLE = [...FEATURE_SRC.matchAll(/Feature\('([a-z-]+)', '([a-z.-]+)', '([^']+)',\s*'([^']*)', '([a-z]+)'(?:, edition='([a-z]+)')?/g)].map((m) => ({id: m[1], key: m[2], label: m[3], help: m[4], icon: m[5], edition: m[6] || 'community'}));
const SUBNAV = [['tab-settings', 'General', 'gear'], ['tab-access', 'Access & sign-in', 'lock'], ['tab-features', 'Features', 'toggle'], ['tab-ai', 'Investigator', 'spark'], ['tab-changes', 'Changes', 'history']];

function setting(key, group, label, extra) {
  return Object.assign({key, group, label, help: label + ' help', kind: 'bool', live: true, value: 'true', base: 'true', default: 'true', source: 'default', by: null, at: null}, extra || {});
}

function settings() {
  return [
    setting('investigate.model', 'Investigator', 'Card model', {kind: 'choice', value: 'claude-opus-5-5', base: 'claude-opus-5-5', default: 'claude-opus-5-5', choices: ['claude-opus-5-5', 'claude-sonnet-5-5']}),
    setting('slack.channel', 'Slack', 'Channel', {kind: 'text', value: 'C0123', base: '', default: '', source: 'admin', by: 'alice@example.com', at: iso(20)}),
    setting('access.public-read', 'Access', 'Read without login', {value: 'false', base: 'false', default: 'false'}),
    setting('access.session-hours', 'Access', 'Session length (h)', {kind: 'int', value: '12', base: '12', default: '12', low: 1, high: 720}),
    setting('core.investigator', 'Features', 'AI investigator'),
    setting('self-review.enabled', 'Features', 'Self-review', {value: 'false', base: 'false', default: 'false'}),
    setting('core.release-settle', 'Features', 'Release settle', {live: false}),
  ];
}

function features() {
  return TABLE.map((f) => {
    const off = ['public-read', 'self-review', 'hide-attribution'].indexOf(f.id) >= 0;
    const locked = f.edition !== 'community';
    return {id: f.id, key: f.key, label: f.label, help: f.help, icon: f.icon, on: !off, stored: !off, locked, edition: f.edition, needs: [], restart: null, source: 'default',
      reason: locked ? f.label + ' needs the Enterprise edition.' : null};
  });
}

function plugins() {
  return {edition: 'community', secretsKey: 'env',
    plugins: [{id: 'acme', name: 'Acme metrics', state: 'ok', enabled: true, fields: [], instances: []}, {id: 'beta', name: 'Beta logs', state: 'ok', enabled: true, fields: [], instances: []},
      {id: 'gold', name: 'Gold board', state: 'locked', enabled: true, reason: 'Gold board needs the Enterprise edition of Skopax', fields: [], instances: []},
      {id: 'quiet', name: 'Quiet', state: 'ok', enabled: false, fields: [], instances: []}]};
}

async function boot(opts) {
  opts = opts || {};
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const admin = opts.admin !== false;
  const srv = {notices: opts.notices || [], history: opts.history || [], items: opts.items || settings(), features: features(), users: opts.users || []};
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/' + (opts.hash || '#t=anoms'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      Object.keys(opts.stored || {}).forEach((k) => w.localStorage.setItem(k, opts.stored[k]));
      w.fetch = async (p0, o) => {
        const p = String(p0), method = (o && o.method) || 'GET';
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        const bad = (status, detail) => ({ok: false, status, json: async () => ({detail})});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', userRole: admin ? 'admin' : 'viewer', operator: admin, admin, teams: [], tags: []});
        if (p.startsWith('/api/admin')) {
          calls.push([method, p]);
          if (!admin) return bad(403, 'Admins only');
          if (p === '/api/admin/notices') return ok({items: srv.notices});
          if (p === '/api/admin/settings') return ok({items: srv.items, rev: 1, refreshSeconds: 15});
          if (p.startsWith('/api/admin/history')) return ok(srv.history);
          if (p === '/api/admin/features') return ok({edition: 'community', items: srv.features});
          if (p === '/api/admin/plugins') return ok(plugins());
          if (p === '/api/admin/anthropic-key') return ok({using: null, admin: false, config: false, unreadable: false, updatedBy: null, updatedAt: null, note: null});
          if (p === '/api/admin/users') return ok(srv.users);
          if (p === '/api/admin/tokens') return ok([]);
          if (p.startsWith('/api/admin/small-talk')) return bad(404, 'none');
        }
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
      };
    }});
  await wait(opts.settle || 300);
  return {dom, w: dom.window, doc: dom.window.document, errors, calls, srv};
}

const navRows = (doc) => [...doc.querySelectorAll('#adm-nav a.tab')];
const currentIds = (doc) => [...doc.querySelectorAll('[aria-current="page"]')].map((a) => a.id);

async function theSubNavListsTheSixSectionsWithTheirIconsAndCounts() {
  //given
  const {dom, w, doc, errors} = await boot({hash: '#t=settings'});
  const nav = doc.getElementById('adm-nav');

  //then
  assert.ok(nav.matches('nav.adm-nav') && nav.getAttribute('aria-label') === 'Admin sections', 'one Admin sections navigation');
  assert.deepStrictEqual(navRows(doc).map((a) => [a.id, a.querySelector('.lbl').textContent]), SUBNAV.map((r) => [r[0], r[1]]));
  assert.deepStrictEqual(navRows(doc).map((a) => a.querySelector('svg.i').outerHTML), SUBNAV.map((r) => svg(doc, w.navIcon(r[2]))), 'each section has the mockup icon');
  assert.deepStrictEqual(navRows(doc).map((a) => a.getAttribute('href')), SUBNAV.map((r) => '#t=' + r[0].slice(4)));
  assert.strictEqual(doc.getElementById('count-features').textContent, '9/12', 'Features counts the switches that are on out of all of them');
  assert.ok(!doc.getElementById('adm-shell').hidden && doc.getElementById('tab-settings').getAttribute('aria-current') === 'page');
  assert.strictEqual(doc.getElementById('crumb-page').textContent, 'Admin \u203a General');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the Admin sub-nav lists General, Access & sign-in, Features, Investigator and Changes with their icons, and Features counts the features that are on';
}

async function aLinkToAccessShowsItWithTheAdminRowCurrentAndOtherTabsHideTheShell() {
  //given
  const {dom, w, doc, errors, calls} = await boot({hash: '#t=access'});

  //then
  assert.ok(!doc.getElementById('panel-access').hidden && doc.getElementById('panel-settings').hidden, 'the link opens Access');
  assert.deepStrictEqual(currentIds(doc).sort(), ['side-admin', 'tab-access', 'user-btn'], 'the user menu button, the hidden Admin row and the Access section are current');
  assert.strictEqual(doc.getElementById('crumb-page').textContent, 'Admin \u203a Access & sign-in');
  assert.ok(calls.some((c) => c[1] === '/api/admin/users'), 'Access loads its users');

  //when
  doc.getElementById('tab-anoms').click();
  await wait(30);

  //then
  assert.ok(doc.getElementById('adm-shell').hidden && !doc.getElementById('panel-anoms').hidden, 'the admin shell hides on the board');
  assert.ok(!doc.getElementById('side-admin').hasAttribute('aria-current'));
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a #t=access link opens Access with the Admin row and its section current, and the shell hides on any other page';
}

async function everyAdminSectionHasItsOwnLinkAndTheAdminRowOpensTheLastOne() {
  //given
  const fresh = await boot({hash: '#t=anoms'});
  const row = fresh.doc.getElementById('side-admin');
  const first = row.getAttribute('href');

  //when
  row.click();
  await wait(60);

  //then
  assert.strictEqual(first, '#t=settings', 'the first time, the Admin row opens General');
  assert.ok(!fresh.doc.getElementById('panel-settings').hidden);
  fresh.dom.window.close();
  for (const id of ['features', 'ai', 'changes']) {
    //when
    const t = await boot({hash: '#t=' + id});
    const shown = [...t.doc.querySelectorAll('#adm-shell .panel')].filter((p) => !p.hidden).map((p) => p.id);
    t.doc.getElementById('tab-anoms').click();
    await wait(20);

    //then
    assert.deepStrictEqual(shown, ['panel-' + id], 'a #t=' + id + ' link opens that section for an admin');
    assert.strictEqual(t.w.localStorage.getItem('skopax-admin-tab'), id, 'and remembers it');
    assert.strictEqual(t.doc.getElementById('side-admin').getAttribute('href'), '#t=' + id, 'so the Admin row goes back to it');
    assert.strictEqual(t.doc.getElementById('count-settings').textContent, '1', 'and General still counts its settings set here');
    assert.deepStrictEqual(t.errors, []);
    t.dom.window.close();
  }

  //when
  const back = await boot({hash: '#t=anoms', stored: {'skopax-admin-tab': 'ai'}});
  back.doc.getElementById('side-admin').click();
  await wait(60);
  const bogus = await boot({hash: '#t=anoms', stored: {'skopax-admin-tab': 'anoms'}});

  //then
  assert.ok(!back.doc.getElementById('panel-ai').hidden, 'the stored section opens after a reload');
  assert.strictEqual(bogus.doc.getElementById('side-admin').getAttribute('href'), '#t=settings', 'a stored value that is not an admin section is ignored');
  back.dom.window.close(); bogus.dom.window.close();
  return 'deep links open Features, Investigator and Changes; the Admin row opens the last admin section, General the first time';
}

async function generalLeavesOutTheFeaturesTheirKeysAndTheInvestigatorGroup() {
  //given
  const {dom, doc, errors} = await boot({hash: '#t=settings'});
  const keys = [...doc.querySelectorAll('#admin .adm-row')].map((r) => r.dataset.key);
  const groups = [...doc.querySelectorAll('#admin .adm-grp')].map((g) => g.dataset.group);

  //then
  assert.deepStrictEqual(keys, ['slack.channel', 'access.session-hours'], 'only the settings no other section owns');
  assert.deepStrictEqual(groups, ['Slack', 'Access']);
  assert.ok(!doc.querySelector('#admin .adm-hist-grp') && !doc.querySelector('#admin .adm-hist'), 'no Changes fold in General');
  assert.strictEqual(doc.getElementById('count-settings').textContent, '1', 'General counts the settings set here in it');
  assert.ok(/1 set here \u00b7 2 settings/.test(doc.querySelector('#admin .adm-head').textContent));
  assert.ok(doc.getElementById('adm-find'), 'the settings search stays');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'General has no Features-group row, no row the Features tab owns (public read) and no Investigator row, and no Changes fold';
}

async function accessRowsInChangesAreNamedAndHaveNoRevert() {
  //given
  const history = [
    {id: 'a1', key: 'access', access: true, at: iso(3), by: 'Alice Moss', text: 'User Dana Gray created with the viewer role'},
    {id: 'h1', seq: 1, key: 'slack.channel', old: null, new: 'C0123', by: 'alice@example.com', at: iso(20)},
  ];

  //when
  const {dom, doc, errors} = await boot({hash: '#t=changes', history});
  const rows = [...doc.querySelectorAll('#changes .adm-hist tbody tr')];

  //then
  assert.deepStrictEqual([...rows[0].children].slice(1, 4).map((c) => c.textContent), ['Alice Moss', 'People and access', 'User Dana Gray created with the viewer role']);
  assert.ok(!rows[0].querySelector('.adm-revert'), 'an access row has no Revert');
  assert.ok(!rows[0].textContent.includes('@'));
  assert.ok(rows[1].querySelector('.adm-revert'), 'a setting row keeps its Revert');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'an access row in Changes shows who, People and access and the change by name, with no Revert';
}

async function changesListsTheHistoryAndASecretRowHasNoRevert() {
  //given
  const history = [
    {id: 'h2', seq: 2, key: 'anthropic.api-key', old: null, new: 'set', by: 'alice@example.com', at: iso(5), secret: true},
    {id: 'h1', seq: 1, key: 'slack.channel', old: null, new: 'C0123', by: 'alice@example.com', at: iso(20)},
  ];
  const {dom, doc, errors, calls} = await boot({hash: '#t=changes', history});
  const rows = [...doc.querySelectorAll('#changes .adm-hist tbody tr')];

  //then
  assert.ok(calls.some((c) => c[1] === '/api/admin/history?limit=200'), 'Changes asks for the latest 200 changes');
  assert.strictEqual(rows.length, 2);
  assert.ok(/not set/.test(rows[0].children[3].textContent) && /\bset\b/.test(rows[0].children[3].textContent.replace('not set', '')), 'the secret row says not set and set: ' + rows[0].children[3].textContent);
  assert.ok(!rows[0].querySelector('.adm-revert'), 'a secret row has no Revert');
  assert.ok(rows[1].querySelector('.adm-revert') && rows[1].children[2].textContent === 'Channel', 'a setting row is named and can be reverted');
  assert.ok(/2 changes/.test(doc.querySelector('#changes .adm-head').textContent), 'the head counts the changes');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'Changes lists the history; the AI key row reads not set to set and has no Revert';
}

async function noticesAreTextAndOnlyAWarningPutsADotOnTheAdminRow() {
  //given
  const info = [{id: 'secret-key', level: 'info', text: 'Secrets are sealed with a generated key.'}];
  const warned = info.concat([{id: 'proxy', level: 'warn', text: EVIL, at: iso(3)}, {id: 'plugin:bad', level: 'warn', text: 'bad was not loaded: ' + EVIL}]);
  const calm = await boot({hash: '#t=anoms', notices: info});
  const loud = await boot({hash: '#t=features', notices: warned});
  const strip = loud.doc.getElementById('adm-notices');
  const dot = (t) => t.doc.querySelector('#side-admin .a-dot');

  //then
  assert.ok(calm.calls.some((c) => c[1] === '/api/admin/notices'), 'notices load for an admin on any page');
  assert.ok(dot(calm).hidden, 'an info notice alone puts no dot on the Admin row');
  assert.strictEqual(calm.doc.getElementById('adm-notices').textContent, info[0].text, 'but it is in the strip');
  assert.ok(!dot(loud).hidden && /2 warnings/.test(loud.doc.getElementById('side-admin').title), 'warnings put a dot on the Admin row: ' + loud.doc.getElementById('side-admin').title);
  assert.ok(!strip.hidden && strip.closest('#adm-shell') && strip.compareDocumentPosition(loud.doc.getElementById('panel-features')) & loud.w.Node.DOCUMENT_POSITION_FOLLOWING, 'the strip sits above the admin section');
  assert.deepStrictEqual([...strip.querySelectorAll('.adm-notice')].map((n) => [n.dataset.id, n.classList.contains('k-warn')]), [['secret-key', false], ['proxy', true], ['plugin:bad', true]]);
  assert.strictEqual(strip.querySelectorAll('img').length, 0, 'a hostile notice text is never markup');
  assert.strictEqual(strip.querySelector('[data-id="proxy"] .adm-notice-t').textContent, EVIL);
  assert.strictEqual(strip.querySelector('[data-id="plugin:bad"] .adm-notice-t').textContent, 'bad was not loaded: ' + EVIL);

  //when
  const before = loud.calls.filter((c) => c[1] === '/api/admin/notices').length;
  loud.srv.notices = info;
  loud.doc.getElementById('tab-ai').click();
  await wait(60);
  const after = loud.calls.filter((c) => c[1] === '/api/admin/notices').length;
  const cleared = dot(loud).hidden;
  loud.srv.notices = warned;
  loud.doc.getElementById('tab-changes').click();
  await wait(60);
  loud.w.setAdminAccess(false);
  await wait(20);

  //then
  assert.strictEqual(after, before + 1, 'each admin section asks for the notices again');
  assert.ok(cleared, 'and the dot goes once the warnings are gone');
  assert.ok(dot(loud).hidden && loud.doc.getElementById('side-admin').hidden && loud.doc.getElementById('adm-notices').hidden, 'losing the role clears the dot and the strip');
  assert.ok(!loud.doc.getElementById('panel-anoms').hidden, 'and leaves the admin pages');
  assert.deepStrictEqual(calm.errors.concat(loud.errors), []);
  calm.dom.window.close(); loud.dom.window.close();
  return 'notices show as text in one strip above every admin section, are fetched again on each section, and only a warning puts a dot on the Admin row';
}

async function aViewerHasNoAdminRowAndAdminLinksFallBack() {
  //given
  const t = await boot({admin: false, hash: '#t=features'});

  //then
  assert.ok(t.doc.getElementById('side-admin').hidden, 'no Admin row');
  assert.ok(!t.doc.getElementById('panel-anoms').hidden && t.doc.getElementById('panel-features').hidden && t.doc.getElementById('adm-shell').hidden, '#t=features opens the board instead');
  assert.ok(navRows(t.doc).every((a) => a.hidden && a.getAttribute('aria-disabled') === 'true'), 'every admin section link is hidden and disabled');

  //when
  t.w.location.hash = '#t=changes';
  await wait(60);
  t.doc.getElementById('tab-ai').click();
  await wait(30);

  //then
  assert.ok(t.doc.getElementById('panel-changes').hidden && t.doc.getElementById('panel-ai').hidden, 'no admin page opens');
  assert.deepStrictEqual(t.calls, [], 'and the admin API is never called');
  assert.deepStrictEqual(t.errors, []);
  t.dom.window.close();
  return 'a viewer has no Admin row, its links are disabled, and #t=features or #t=changes fall back to the board without calling the admin API';
}

async function theSubNavIsAColumnOnWideContentAndAChipRowOnNarrow() {
  //given
  const css = fs.readFileSync(TEMPLATE, 'utf8');
  const rule = (text, sel) => (new RegExp('(^|\\n)\\s*' + sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ' \\{([^}]*)\\}').exec(text) || [])[2] || '';
  const narrow = /@container content \(max-width: 899px\) \{\n([\s\S]*?)\n\}/.exec(css);

  //then
  assert.match(rule(css, '.adm-shell'), /grid-template-columns: 178px minmax\(0, 1fr\)/, 'a 178 px column beside the section');
  assert.match(rule(css, '.adm-nav'), /flex-direction: column/);
  assert.ok(narrow, 'the narrow rule follows the content width, not the window');
  assert.match(rule(narrow[1], '.adm-shell'), /grid-template-columns: minmax\(0, 1fr\)/, 'below 900 px the section takes the whole width');
  assert.match(rule(narrow[1], '.adm-nav'), /flex-direction: row;.*overflow-x: auto/, 'and the sub-nav is a row that scrolls sideways');
  assert.match(rule(narrow[1], '.adm-nav .tab'), /border-radius: 99px/, 'of chips');
  return 'the sub-nav is a 178 px column while the content is 900 px or wider, and a sideways-scrolling chip row below that';
}

async function generalCountsRightOnEverySectionAndListsAreFetchedOnce() {
  //given
  const items = settings().map((i) => i.key === 'access.public-read' ? Object.assign({}, i, {value: 'true', source: 'admin', by: 'alice@example.com', at: iso(10)}) : i);
  const t = await boot({hash: '#t=features', items});
  const counts = [t.doc.getElementById('count-settings').textContent];

  //when
  for (const id of ['ai', 'changes', 'access', 'integrations']) {
    t.doc.getElementById('tab-' + id).click();
    await wait(60);
    counts.push(t.doc.getElementById('count-settings').textContent);
  }
  const fetched = (path) => t.calls.filter((c) => c[1] === path).length;

  //then
  assert.deepStrictEqual(counts, ['1', '1', '1', '1', '1'], 'General counts only its own row set here (the channel), never public read, on every section');
  assert.strictEqual(fetched('/api/admin/features'), 1, 'the features list is fetched once for the counts and kept');
  assert.strictEqual(fetched('/api/admin/plugins'), 1, 'the plug-ins are fetched only by the Integrations page itself, never for an Admin count');
  assert.deepStrictEqual(t.errors, []);
  t.dom.window.close();
  return 'General counts its own settings set here on every section, with public read set here, and the lists fetched for the counts are kept';
}

async function accessIsCountedBeforeItsTabIsOpened() {
  //given
  const user = (email, role) => ({email, name: email.split('@')[0], role, source: 'local', disabled: false, lastLogin: null});
  const t = await boot({hash: '#t=settings', users: [user('alice@example.com', 'admin'), user('bob@example.com', 'viewer')]});
  const counts = [t.doc.getElementById('count-access').textContent];

  //when
  for (const id of ['ai', 'changes', 'access', 'features']) {
    t.doc.getElementById('tab-' + id).click();
    await wait(60);
    counts.push(t.doc.getElementById('count-access').textContent);
  }
  const fetched = t.calls.filter((c) => c[1] === '/api/admin/users').length;

  //then
  assert.deepStrictEqual(counts, ['2', '2', '2', '2', '2'], 'Access & sign-in shows its user count on the first Admin section opened, before its own tab, and keeps it');
  assert.strictEqual(fetched, 2, 'the users are fetched once for the count and once by Access itself');
  assert.deepStrictEqual(t.errors, []);
  t.dom.window.close();
  return 'the Access & sign-in count is known on the first Admin section opened, with no dash, and the users are fetched once for it';
}

async function aShortTextWithLinesIsAMultiLineBoxAndAPlainOneStaysOneLine() {
  //given
  const items = settings().concat([setting('slack.about', 'Slack', 'About', {kind: 'text', value: 'Shop team', base: '', default: '', limit: 300, lines: 4})]);
  const {dom, doc, errors} = await boot({hash: '#t=settings', items});

  //when
  const about = doc.querySelector('#admin .adm-row[data-key="slack.about"] textarea');
  const channel = doc.querySelector('#admin .adm-row[data-key="slack.channel"]');

  //then
  assert.ok(about, 'a text setting with lines is a text box');
  assert.strictEqual(about.getAttribute('rows'), '4');
  assert.strictEqual(about.getAttribute('maxlength'), '300');
  assert.strictEqual(about.value, 'Shop team');
  assert.ok(about.classList.contains('adm-long'), 'it takes the full width');
  assert.ok(!channel.querySelector('textarea') && channel.querySelector('input'), 'a plain short text stays one line');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a short text setting that asks for lines gets a 4-row full-width box, other short texts stay one line';
}

(async () => {
  console.log('PASS ' + await theSubNavListsTheSixSectionsWithTheirIconsAndCounts());
  console.log('PASS ' + await aLinkToAccessShowsItWithTheAdminRowCurrentAndOtherTabsHideTheShell());
  console.log('PASS ' + await everyAdminSectionHasItsOwnLinkAndTheAdminRowOpensTheLastOne());
  console.log('PASS ' + await generalLeavesOutTheFeaturesTheirKeysAndTheInvestigatorGroup());
  console.log('PASS ' + await changesListsTheHistoryAndASecretRowHasNoRevert());
  console.log('PASS ' + await accessRowsInChangesAreNamedAndHaveNoRevert());
  console.log('PASS ' + await noticesAreTextAndOnlyAWarningPutsADotOnTheAdminRow());
  console.log('PASS ' + await aViewerHasNoAdminRowAndAdminLinksFallBack());
  console.log('PASS ' + await theSubNavIsAColumnOnWideContentAndAChipRowOnNarrow());
  console.log('PASS ' + await generalCountsRightOnEverySectionAndListsAreFetchedOnce());
  console.log('PASS ' + await accessIsCountedBeforeItsTabIsOpened());
  console.log('PASS ' + await aShortTextWithLinesIsAMultiLineBoxAndAPlainOneStaysOneLine());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
