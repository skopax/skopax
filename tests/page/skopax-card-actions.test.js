const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const CARD_PAGE = path.join(path.dirname(TEMPLATE), 'card.html');
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const AT = '2026-11-09T16:32:00Z';
const OPERATOR = {loginEnabled: true, user: 'alice@example.com', operator: true, jira: true, jiraUrl: 'https://jira.example.com', routes: [{id: 'r1', name: '#checkout-alerts', plugin: 'Slack'}]};

async function boot(card, who) {
  const errors = [], posts = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(CARD_PAGE), {
    url: 'https://skopax.example.com/c/' + card.num, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.fetch = async (p, opts) => {
        if (String(p) === '/api/me') return {ok: true, status: 200, json: async () => Object.assign({}, who)};
        if (opts && opts.method === 'POST') { posts.push([String(p), JSON.parse(opts.body)]); return {ok: true, status: 201, json: async () => ({})}; }
        return {ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(card))};
      };
    },
  });
  await wait(120);
  return {w: dom.window, doc: dom.window.document, errors, posts};
}

const base = {num: 42, key: 'prom|shop/checkout|latency|p95', svc: 'checkout', label: 'p95 latency', sev: 'crit', status: 'open', firstSeen: AT, live: true};
const groupNames = (doc) => [...doc.querySelectorAll('.acts .act-group')].map((g) => doc.getElementById(g.getAttribute('aria-labelledby')).textContent);
const pressed = (doc) => [...doc.querySelectorAll('.acts .act-group:not(.ai) .seg button')].map((b) => [b.id, b.textContent, b.getAttribute('aria-pressed')]);

async function anOperatorSeesTheActionsGroupedInOrder() {
  //given
  const card = Object.assign({}, base, {triage: 'progress', owner: 'alice@example.com', ticket: 'SHOP-123', last: {type: 'dive', status: 'done', requestedAt: AT, requestedBy: 'bob@example.com'}});

  //when
  const {w, doc, errors} = await boot(card, OPERATOR);
  const acts = doc.querySelector('.acts.op');
  const order = [...acts.querySelectorAll('.act-group.ai .seg > *, .act-group, .act-foot')].map((n) => n.id || n.className);

  //then
  assert.deepStrictEqual(order, ['act-group ai', 'chat-about', 'act-dive', 'act-group', 'act-group', 'act-group links', 'act-foot']);
  assert.deepStrictEqual(groupNames(doc), ['AI', 'Status', 'Owner', 'Links']);
  assert.ok([...acts.querySelectorAll('.act-group')].every((g) => g.getAttribute('role') === 'group'), 'each group is a labelled group');
  assert.strictEqual(doc.getElementById('act-dive').textContent, 'Investigate', 'Investigate shows its name next to the sparkle');
  assert.deepStrictEqual(pressed(doc), [['act-take', 'In progress', 'true'], ['act-resolve', 'Resolve', 'false'], ['act-mute', 'Mute 24 h', 'false']], 'the card is in progress, so the pressed first segment says In progress');
  assert.ok(doc.getElementById('act-take').title.startsWith('The card goes In progress'), 'it keeps the Start progress tip');
  assert.deepStrictEqual([doc.querySelector('.acts .owner.chip').textContent, doc.querySelector('.acts .owner.chip').dataset.ini, doc.getElementById('act-assign').textContent], ['Assignee: alice', 'A', 'Change assignee']);
  const jira = doc.getElementById('jira-open');
  assert.deepStrictEqual([jira.textContent, jira.getAttribute('href'), jira.target, doc.getElementById('act-link').textContent], ['SHOP-123\u2197', 'https://jira.example.com/browse/SHOP-123', '_blank', 'Change ticket'], 'the Jira key opens the ticket, with Change ticket beside it');
  assert.deepStrictEqual([...doc.querySelectorAll('.act-group.links .btn')].map((b) => b.id), ['jira-open', 'act-link', 'act-route', 'copy-link', 'board']);
  assert.ok(doc.querySelector('.act-foot .act-last svg'), 'the last action line has a history icon');
  assert.strictEqual(doc.querySelector('.act-last').textContent.startsWith('Last action: Investigation'), true);
  assert.strictEqual(doc.querySelector('p.owner'), null, 'the assignee is shown once, in the Owner group');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'an operator sees Chat and Investigate first, then Status, Owner and Links groups and a last action line, in that order';
}

async function everyIconIsHiddenAndEveryControlHasAName() {
  //given
  const card = Object.assign({}, base, {url: 'https://shop.example.com/checkout'});

  //when
  const {w, doc, errors} = await boot(card, OPERATOR);
  const controls = [...doc.querySelectorAll('.acts a, .acts button')];

  //then
  assert.ok([...doc.querySelectorAll('.acts svg')].length >= 9, 'the actions carry icons');
  assert.ok([...doc.querySelectorAll('.acts svg')].every((s) => s.getAttribute('aria-hidden') === 'true'), 'icons are decoration');
  assert.deepStrictEqual(controls.filter((c) => !(c.getAttribute('aria-label') || c.textContent.trim())).map((c) => c.id), [], 'every control has a name');
  const page = doc.getElementById('open-page');
  assert.deepStrictEqual([page.textContent, page.target, page.closest('.act-group').classList.contains('links')], ['Open the page\u2197', '_blank', true], 'the external page link sits in Links with an external mark');
  assert.ok(doc.querySelector('#open-page .ext[aria-hidden="true"]'));
  assert.deepStrictEqual([doc.querySelector('.acts .chip.none').textContent, doc.getElementById('act-assign').textContent], ['Unassigned', 'Assign']);
  assert.strictEqual(doc.getElementById('act-link').textContent, 'Link Jira ticket');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'every icon is aria-hidden, every action has a name, and the external page link sits in Links with an external mark';
}

async function mutedAndHandledCardsShowTheirStateInTheSegment() {
  //given
  const muted = Object.assign({}, base, {muted: true});
  const handled = Object.assign({}, base, {triage: 'handled', resolution: {kind: 'fixed', by: 'alice@example.com', at: AT}});

  //when
  const m = await boot(muted, OPERATOR);
  const h = await boot(handled, OPERATOR);
  m.doc.getElementById('act-unmute').click();
  await wait(50);

  //then
  assert.deepStrictEqual(pressed(m.doc), [['act-take', 'Start progress', 'false'], ['act-resolve', 'Resolve', 'false'], ['act-unmute', 'Unmute', 'true']], 'a muted card shows Unmute pressed');
  assert.deepStrictEqual(m.posts.map((p) => p[1].type), ['unmute'], 'Unmute still unmutes');
  assert.deepStrictEqual(pressed(h.doc).map((p) => p[0]), ['act-take', 'act-mute'], 'a handled card has no Resolve, as before');
  assert.deepStrictEqual(m.errors.concat(h.errors), []);
  m.w.close(); h.w.close();
  return 'a muted card shows Unmute pressed and still unmutes; a handled card keeps hiding Resolve';
}

async function copyLinkKeepsItsIconWhileSayingCopied() {
  //given
  const {w, doc, errors} = await boot(Object.assign({}, base), OPERATOR);
  Object.defineProperty(w.navigator, 'clipboard', {value: {writeText: () => Promise.resolve()}, configurable: true});
  const b = doc.getElementById('copy-link');

  //when
  b.click();
  await wait(20);

  //then
  assert.deepStrictEqual([b.textContent, !!b.querySelector('svg')], ['Copied', true]);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'Copy link says Copied and keeps its icon';
}

async function viewersGetLinksAndTheOwnerButNoStatusOrAssign() {
  //given
  const card = Object.assign({}, base, {owner: 'Carol Diaz'});

  //when
  const {w, doc, errors} = await boot(card, {loginEnabled: true, user: 'x@example.com', operator: false});

  //then
  assert.deepStrictEqual(groupNames(doc), ['AI', 'Owner', 'Links']);
  assert.deepStrictEqual([...doc.querySelectorAll('.acts button, .acts a')].map((b) => b.id), ['chat-about', 'copy-link', 'board']);
  assert.strictEqual(doc.querySelector('.owner').textContent, 'Assignee: Carol Diaz');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a viewer gets Chat, the owner chip and the links, and still no status or assign controls';
}

async function chatAndInvestigateAreAnAiSegmentLikeStatus() {
  //given
  const card = Object.assign({}, base, {triage: 'progress'});

  //when
  const op = await boot(card, OPERATOR);
  const segs = [...op.doc.querySelectorAll('.acts .act-group.ai .seg > .seg-b')].map((b) => [b.id, b.tagName.toLowerCase(), b.getAttribute('aria-label') || b.textContent, !!b.querySelector('svg[aria-hidden="true"]'), b.hasAttribute('aria-pressed')]);
  const label = op.doc.getElementById(op.doc.querySelector('.acts .act-group.ai').getAttribute('aria-labelledby')).textContent;
  const chat = op.doc.getElementById('chat-about');
  const filled = op.doc.querySelectorAll('.acts .act-primary, .acts .btn.big').length;
  const viewer = await boot(card, {loginEnabled: true, user: 'x@example.com', operator: false});
  const viewerSegs = [...viewer.doc.querySelectorAll('.acts .act-group.ai .seg > .seg-b')].map((b) => b.id);

  //then
  assert.strictEqual(label, 'AI', 'the group is labelled AI like Status');
  assert.deepStrictEqual(segs, [['chat-about', 'a', 'Chat about this card', true, false], ['act-dive', 'button', 'Investigate', true, false]], 'Chat and Investigate are icon and word segments with their names, and no toggle state');
  assert.strictEqual(chat.getAttribute('href'), '/#t=chat/new/42', 'Chat still opens a new chat with the card');
  assert.strictEqual(filled, 0, 'no filled primary button is left');
  assert.deepStrictEqual(viewerSegs, ['chat-about'], 'a viewer gets the AI group with Chat only');
  assert.deepStrictEqual(op.errors.concat(viewer.errors), []);
  op.w.close();
  viewer.w.close();
  return 'the card page shows Chat and Investigate as an AI segment group like Status, with their ids and names, and no filled button';
}

function phoneWidthStacksTheGroups() {
  //given
  const css = fs.readFileSync(CARD_PAGE, 'utf8');

  //when
  const narrow = /\.acts \{[^}]*container-type: inline-size/.test(css) && /@container \(max-width: 520px\) \{([\s\S]*?)\n\}/.exec(css);

  //then
  assert.ok(narrow, 'the action block is a size container with a narrow layout');
  assert.ok(/\.act-group \{ grid-template-columns: minmax\(0, 1fr\);/.test(narrow[1]), 'groups stack their label over the controls');
  assert.ok(/\.seg \{ display: grid; grid-auto-flow: column; grid-auto-columns: minmax\(0, 1fr\); width: 100%; \}/.test(narrow[1]), 'the segmented control splits the width instead of overflowing');
  return 'at phone width the groups stack and the segmented control shares the width';
}

(async () => {
  console.log('PASS ' + await anOperatorSeesTheActionsGroupedInOrder());
  console.log('PASS ' + await everyIconIsHiddenAndEveryControlHasAName());
  console.log('PASS ' + await mutedAndHandledCardsShowTheirStateInTheSegment());
  console.log('PASS ' + await copyLinkKeepsItsIconWhileSayingCopied());
  console.log('PASS ' + await viewersGetLinksAndTheOwnerButNoStatusOrAssign());
  console.log('PASS ' + await chatAndInvestigateAreAnAiSegmentLikeStatus());
  console.log('PASS ' + phoneWidthStacksTheGroups());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
