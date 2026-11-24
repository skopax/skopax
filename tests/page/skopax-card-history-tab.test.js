const assert = require('assert');
const {boot, card, iso, wait, until, article, expand} = require('./skopax-card-polish.test.js');

const TEMPLATE = process.argv[2];
const KEY = 'log|checkout-service|ERROR|boom';
const T = (min) => iso(min);

function fixture() {
  const c = card(KEY, {num: 12, firstSeen: T(300), tags: [{id: 'seo', by: 'rule', at: T(290)}], business: {text: 'Change the banner.', by: 'alice@example.com', at: T(280)},
    slackPost: {at: T(120), by: 'Claude', permalink: 'https://example.slack.com/archives/C1/p1'}, teamsPosts: [{at: T(110), route: 'seo', by: 'server'}]});
  const data = {
    actions: [
      {id: 'a3', type: 'resolve', target: KEY, status: 'done', requestedBy: 'bob@example.com', requestedAt: T(60), doneAt: T(60), result: 'Fixed', label: 'Resolve checkout-service'},
      {id: 'a2', type: 'severity', sev: 'warn', target: KEY, status: 'done', requestedBy: 'bob@example.com', requestedAt: T(200), doneAt: T(200), result: 'severity set to Warning by hand (the signal says Critical)'},
      {id: 'a1', type: 'take', target: KEY, status: 'done', requestedBy: 'alice@example.com', requestedAt: T(250), doneAt: T(250), label: 'Take checkout-service'},
      {id: 'a0', type: 'ticket', target: KEY, status: 'done', requestedBy: 'alice@example.com', requestedAt: T(240), doneAt: T(240), result: 'OPS-123'}],
    activity: [
      {id: 'v2', kind: 'tags', who: 'alice@example.com', t: T(100), key: KEY, text: 'untagged SEO \u00b7 ' + KEY},
      {id: 'v1', kind: 'assign', who: 'system', t: T(260), key: KEY, text: 'Auto-assigned to Nina White by the SEO tag \u00b7 ' + KEY}],
  };
  const runs = [{id: 'run-9', startedAt: T(230), outcome: 'done', recap: 'one client retried\nmore', keys: [KEY]}];
  return {c, data, runs};
}

async function historyRowsComeNewestFirstWithWhoWhenAndOldToNew() {
  //given
  const {w, errors} = await boot([card(KEY)]);
  const {c, data, runs} = fixture();
  //when
  const rows = JSON.parse(JSON.stringify(w.cardHistoryRows(c, data, runs)));
  const what = rows.map((r) => r.what);
  //then
  assert.deepStrictEqual(rows.map((r) => r.at), rows.map((r) => r.at).slice().sort().reverse(), 'newest first');
  assert.ok(rows.every((r) => r.at && r.who), 'every row says who and when');
  assert.deepStrictEqual(what, ['Resolved: Fixed', 'Untagged SEO', 'Posted to Teams: SEO', 'Posted to Slack', 'Severity changed', 'Investigation run: one client retried', 'Ticket created: OPS-123',
    'Started progress', 'Auto-assigned to Nina White by the SEO tag', 'Business note written', 'Tag added: SEO', 'Fired']);
  const sev = rows.filter((r) => r.kind === 'severity')[0];
  assert.deepStrictEqual([sev.what, sev.from, sev.to, sev.who], ['Severity changed', 'Critical', 'Warning', 'bob']);
  assert.strictEqual(rows.filter((r) => r.kind === 'run')[0].href, '/api/investigations/run-9', 'a run links its Run record');
  assert.strictEqual(rows[rows.length - 1].what, 'Fired', 'the oldest row is when it fired');
  assert.strictEqual(rows.filter((r) => r.what === 'Tag added: SEO').length, 1, 'one row per event');
  assert.ok(rows.some((r) => r.what === 'Auto-assigned to Nina White by the SEO tag' && r.who === 'system'));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS history rows come newest first from what the card records, with who, when and old to new for severity');
}

async function theHistoryListsFollowUpRowsAndKeepOpen() {
  //given
  const {w, errors} = await boot([card(KEY)]);
  const c = card(KEY, {num: 42, status: 'cleared', firstSeen: '2026-11-09T16:32:00Z', clearedAt: '2026-11-09T16:32:00Z'});
  const data = {actions: [{type: 'keep', target: KEY, status: 'done', requestedAt: '2026-11-09T18:00:00Z', doneAt: '2026-11-09T18:00:00Z', requestedBy: 'alice@example.com', result: 'kept open'}],
    activity: [{kind: 'followup', who: 'Claude (pod)', t: '2026-11-09T17:00:00Z', key: KEY, text: 'Follow-up checked: not done yet \u00b7 still 404'},
      {kind: 'followup', who: 'Claude (pod)', t: '2026-11-09T19:00:00Z', key: KEY, text: 'Closed as Fixed after the follow-up check: Verified: answered 301'}]};
  //when
  const rows = JSON.parse(JSON.stringify(w.cardHistoryRows(c, data, []))).map((r) => r.what + (r.data ? ': ' + r.data : ''));
  //then
  assert.ok(rows.some((r) => r.includes('Follow-up checked: not done yet')), 'the check is a row: ' + rows.join(' | '));
  assert.ok(rows.some((r) => r.includes('Closed as Fixed after the follow-up check')), 'the close after verify is a row');
  assert.ok(rows.some((r) => r.includes('Kept open')), 'Keep open is a row');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the History tab lists the follow-up check, the close after verify and Keep open');
}

async function theDetailsAndHistoryTabsSwitchByClickAndKeyboardAndStayAcrossARepaint() {
  //given
  const {c, data} = fixture();
  const {w, doc, errors} = await boot([c], {history: data});
  const a = () => article(doc, KEY);
  expand(a());
  const tabs = () => [...a().querySelectorAll('[role="tablist"] [role="tab"]')];
  const first = tabs().map((t) => [t.textContent, t.getAttribute('aria-selected'), t.tabIndex]);
  const panel = a().querySelector('#' + tabs()[1].getAttribute('aria-controls'));
  //when
  tabs()[0].focus();
  tabs()[0].dispatchEvent(new w.KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true}));
  await until(() => a().querySelector('.cd-hist li'));
  const after = {sel: tabs().map((t) => t.getAttribute('aria-selected')), focus: doc.activeElement === tabs()[1], detailsHidden: a().querySelector('.cd-details').hidden,
    rows: a().querySelectorAll('.cd-hist li').length, first: a().querySelector('.cd-hist li .h-what').textContent, time: a().querySelector('.cd-hist li time').getAttribute('datetime')};
  w.renderAnoms();
  await wait(20);
  const kept = tabs().map((t) => t.getAttribute('aria-selected'));
  tabs()[1].dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Home', bubbles: true}));
  //then
  assert.deepStrictEqual(first, [['Details', 'true', 0], ['History', 'false', -1]]);
  assert.strictEqual(panel.getAttribute('role'), 'tabpanel');
  assert.deepStrictEqual(after.sel, ['false', 'true']);
  assert.strictEqual(after.focus, true, 'the arrow moves the focus');
  assert.strictEqual(after.detailsHidden, true);
  assert.ok(after.rows >= 8, 'rows from the card, its actions and its activity: ' + after.rows);
  assert.strictEqual(after.first, 'Resolved: Fixed');
  assert.strictEqual(after.time, data.actions[0].doneAt);
  assert.deepStrictEqual(kept, ['false', 'true'], 'a live repaint keeps the History tab');
  assert.deepStrictEqual(tabs().map((t) => t.getAttribute('aria-selected')), ['true', 'false'], 'Home goes back to Details');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the card has Details and History tabs, switched by click or arrows, that load the timeline and survive a live repaint');
}

async function historyRowsAreTranslatedButTheirDataStaysAsWritten() {
  //given
  const {c, data} = fixture();
  const {w, doc, errors} = await boot([c], {history: data, lang: 'de'});
  const a = () => article(doc, KEY);
  expand(a());
  //when
  a().querySelector('.cd-tab[data-tab="history"]').click();
  await until(() => a().querySelector('.cd-hist li'));
  await wait(30);
  const first = a().querySelector('.cd-hist li .h-what'), tag = [...a().querySelectorAll('.cd-hist li .h-what')].filter((x) => x.textContent.indexOf('SEO') >= 0).pop();
  //then
  assert.strictEqual(first.querySelector('.h-l').textContent, 'Gel\u00f6st', 'the label is in German');
  assert.strictEqual(first.querySelector('[data-no-tx]').textContent, 'Fixed', 'the result stays as written');
  assert.strictEqual(first.hasAttribute('data-no-tx'), false, 'only the data part is kept from translation');
  assert.strictEqual(tag.textContent, 'Tag hinzugef\u00fcgt: SEO');
  assert.strictEqual(a().querySelector('.cd-tab[data-tab="history"]').textContent, 'Verlauf');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS history rows are translated while tag names, results and people stay as written');
}

async function failedAndCancelledActionsReadInTheReadersLanguage() {
  //given
  const c = card(KEY, {num: 12, firstSeen: T(300)});
  const data = {actions: [
    {id: 'f1', type: 'resolve', target: KEY, status: 'failed', requestedBy: 'bob@example.com', requestedAt: T(60), failedAt: T(60), result: 'Jira refused it'},
    {id: 'f2', type: 'mute', target: KEY, status: 'cancelled', requestedBy: 'bob@example.com', requestedAt: T(50), cancelledAt: T(50)},
    {id: 'f3', type: 'take', target: KEY, status: 'failed', requestedBy: 'bob@example.com', requestedAt: T(40), failedAt: T(40)}], activity: []};
  const en = await boot([c], {history: data});
  const labels = (x) => [...x.doc.querySelectorAll('.cd-hist li .h-l')].map((l) => l.textContent);
  expand(article(en.doc, KEY));
  article(en.doc, KEY).querySelector('.cd-tab[data-tab="history"]').click();
  await until(() => en.doc.querySelector('.cd-hist li'));
  const english = labels(en);
  const de = await boot([c], {history: data, lang: 'de'});
  expand(article(de.doc, KEY));
  article(de.doc, KEY).querySelector('.cd-tab[data-tab="history"]').click();
  await until(() => de.doc.querySelector('.cd-hist li'));
  await wait(30);
  const german = labels(de);
  const tr = await boot([c], {history: data, lang: 'tr'});
  expand(article(tr.doc, KEY));
  article(tr.doc, KEY).querySelector('.cd-tab[data-tab="history"]').click();
  await until(() => tr.doc.querySelector('.cd-hist li'));
  await wait(30);
  const turkish = labels(tr);

  //then
  assert.deepStrictEqual(english.slice(0, 3), ['Start progress failed', 'Mute cancelled', 'Resolve failed']);
  assert.deepStrictEqual(german.slice(0, 3), ['Bearbeitung beginnen fehlgeschlagen', 'Stummschalten abgebrochen', 'L\u00f6sen fehlgeschlagen']);
  assert.deepStrictEqual(turkish.slice(0, 3), ['\u0130\u015fe ba\u015flama ba\u015far\u0131s\u0131z', 'Sessize alma iptal edildi', '\u00c7\u00f6zme ba\u015far\u0131s\u0131z']);
  assert.deepStrictEqual([en.errors, de.errors, tr.errors], [[], [], []]);
  en.w.close(); de.w.close(); tr.w.close();
  console.log('PASS failed and cancelled actions read in the reader\'s language');
}

async function anAssignmentRowReadsAssignedToNameWithNoColon() {
  //given
  const c = card(KEY, {num: 12, firstSeen: T(300), owner: 'nina.white@example.com', assigned: {by: 'alice@example.com', at: T(100)}});
  const {w, doc, errors} = await boot([c], {history: {actions: [], activity: []}});
  const a = () => article(doc, KEY);
  expand(a());

  //when
  a().querySelector('.cd-tab[data-tab="history"]').click();
  await until(() => a().querySelector('.cd-hist li'));
  const row = [...a().querySelectorAll('.cd-hist li .h-what')].find((x) => /Assigned/.test(x.textContent));
  const rows = JSON.parse(JSON.stringify(w.cardHistoryRows(c, {actions: [], activity: []}, [])));

  //then
  assert.strictEqual(row.textContent.replace(/\s+alice$/, ''), 'Assigned to nina.white');
  assert.ok(rows.some((r) => r.what === 'Assigned to nina.white'), 'the row text has no colon: ' + rows.map((r) => r.what).join('|'));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS an assignment row reads Assigned to name, with no colon');
}

async function emailPostsReadAsEmailAndTeamsPostsStillReadTeams() {
  //given
  const {w, errors} = await boot([card(KEY)]);
  const posts = [{at: T(110), route: 'seo', by: 'server'}, {at: T(100), route: 'ops', plugin: 'email', by: 'server'}, {at: T(90), route: 'growth', plugin: 'teams', by: 'server'}];
  const c = card(KEY, {num: 12, firstSeen: T(300), teamsPosts: posts});

  //when
  const rows = JSON.parse(JSON.stringify(w.cardHistoryRows(c, {actions: [], activity: []}, []))).filter((r) => r.kind === 'teams');
  const box = w.document.createElement('div');
  box.innerHTML = w.teamsTag(posts);
  const tags = [...box.querySelectorAll('.teams-sent')];

  //then
  assert.deepStrictEqual(rows.map((r) => r.what), ['Posted to Teams: growth', 'Sent by email: ops', 'Posted to Teams: SEO']);
  assert.strictEqual(tags.length, 2, 'one tag per plug-in');
  assert.ok(tags[0].textContent.startsWith('Sent to Teams \u00b7 SEO, growth ') && tags[1].textContent.startsWith('Sent by email \u00b7 ops '), tags.map((t) => t.textContent).join('|'));
  assert.ok(tags[0].title.startsWith('Posted to Teams:\n') && tags[1].title.startsWith('Sent by email:\n'));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS email posts read as email and Teams posts still read Teams');
}

async function aRouteSendInTheActivityShowsOnceInTheHistoryTab() {
  //given
  const sentLine = (route, at) => ({id: 'r-' + route, kind: 'routes', who: 'server', t: at, keys: [KEY], text: 'Email route post \u00b7 ' + route + ' \u00b7 1 card(s)'});
  const c = card(KEY, {num: 12, firstSeen: T(300), teamsPosts: [{at: T(100), route: 'ops', plugin: 'email', by: 'server'}]});
  const data = {actions: [], activity: [sentLine('ops', T(100)), sentLine('oncall', T(80))]};
  const {w, doc, errors} = await boot([c], {history: data});
  const a = () => article(doc, KEY);
  expand(a());
  //when
  const rows = JSON.parse(JSON.stringify(w.cardHistoryRows(c, data, []))).filter((r) => r.kind === 'teams' || r.kind === 'routes');
  a().querySelectorAll('[role="tablist"] [role="tab"]')[1].click();
  await until(() => a().querySelector('.cd-hist li'));
  await wait(30);
  const shown = [...a().querySelectorAll('.cd-hist li .h-what')].map((x) => x.textContent);
  //then
  assert.deepStrictEqual(rows.map((r) => r.what), ['Email route post \u00b7 oncall \u00b7 1 card(s)', 'Sent by email: ops'], 'a send the card also records reads once, a send only the activity has still shows');
  assert.ok(shown.indexOf('Email route post \u00b7 oncall \u00b7 1 card(s)') >= 0, shown.join('|'));
  assert.strictEqual(shown.filter((x) => x.indexOf('ops') >= 0).length, 1, shown.join('|'));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a route send from the activity shows in the History tab once, next to the posts the card records');
}

async function anUnknownRoutePluginNamesItsIdAsARouteInTheReadersLanguage() {
  //given
  const posts = [{at: T(100), route: 'ops', plugin: 'pager', by: 'server'}];
  const c = card(KEY, {num: 12, firstSeen: T(300), teamsPosts: posts});
  const en = await boot([c], {history: {actions: [], activity: []}});
  const de = await boot([c], {history: {actions: [], activity: []}, lang: 'de'});
  const opened = async (x) => {
    const a = () => article(x.doc, KEY);
    expand(a());
    a().querySelector('.cd-tab[data-tab="history"]').click();
    await until(() => a().querySelector('.cd-hist li.h-teams'));
    await wait(30);
    return {label: a().querySelector('.cd-hist li.h-teams .h-l').textContent, tag: a().querySelector('.teams-sent').textContent};
  };

  //when
  const said = {en: await opened(en), de: await opened(de)};

  //then
  assert.strictEqual(said.en.label, 'Posted by the pager route');
  assert.ok(said.en.tag.startsWith('Sent by the pager route \u00b7 ops '), said.en.tag);
  assert.strictEqual(said.de.label, 'Gepostet \u00fcber die Route pager');
  assert.ok(said.de.tag.startsWith('Gesendet \u00fcber die Route pager \u00b7 ops '), said.de.tag);
  assert.deepStrictEqual(en.errors.concat(de.errors), []);
  en.w.close();
  de.w.close();
  console.log('PASS a post by an unknown route plug-in names its id as a route, in the reader language')
}

(async () => {
  for (const t of [aRouteSendInTheActivityShowsOnceInTheHistoryTab, anUnknownRoutePluginNamesItsIdAsARouteInTheReadersLanguage, emailPostsReadAsEmailAndTeamsPostsStillReadTeams, failedAndCancelledActionsReadInTheReadersLanguage, anAssignmentRowReadsAssignedToNameWithNoColon, historyRowsComeNewestFirstWithWhoWhenAndOldToNew, historyRowsAreTranslatedButTheirDataStaysAsWritten, theHistoryListsFollowUpRowsAndKeepOpen, theDetailsAndHistoryTabsSwitchByClickAndKeyboardAndStayAcrossARepaint]) {
    try { await t(); } catch (e) { console.error('FAIL ' + t.name + ': ' + e.message.replace(/\x1b\[[0-9;]*m/g, '').split('\n').slice(0, 30).join(' | ')); process.exitCode = 1; }
  }
  process.exit(process.exitCode || 0);
})();
