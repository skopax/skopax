const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString().replace(/\.\d{3}Z$/, 'Z');
const MAIN = 'log|checkout-service|ERROR|boom', MUTED = 'log|search-api|ERROR|noisy', DIVED = 'log|orders-api|ERROR|stuck', OTHER = 'log|cart-service|ERROR|quiet';
const LONG = 'Shoppers cannot finish checkout because the payment step keeps timing out for most card types during the evening peak';

function card(key, num, extra) {
  return Object.assign({key, num, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'ERROR lines', src: 'logs', sig: 'log', now: 5, d1: 1, d7: 2, firstSeen: iso(30), lastSeen: iso(1), what: 'text'}, extra || {});
}
function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(MAIN, 11, {followUp: {doneWhen: 'errors stay at zero', closeAt: iso(-60), closeKind: 'fixed'}}),
      card(MUTED, 16, {muted: 'too loud'}),
      card(DIVED, 18, {triage: 'dived', triagedBy: 'pod', ticket: 'OPS-123', recurring: true, recurringCount: 3, owner: 'alice@example.com',
        business: {text: LONG, kind: 'info', by: 'rule', at: iso(20)}, tags: [{id: 'team-shop', by: 'rule', at: iso(20)}],
        slackPost: {at: iso(10), by: 'pod', permalink: 'https://chat.example.com/p1', ts: '1.2', stateNote: {at: iso(5), permalink: 'https://chat.example.com/p2', text: 'still on'}}}),
      card(OTHER, 19)],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(opts) {
  opts = opts || {};
  const errors = [], posts = [], sends = [];
  const vc = new VirtualConsole(); vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE, {view: 'board'}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state())), {
    url: 'https://skopax.example.com/' + (opts.hash || '#t=anoms/all'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {}; w.Element.prototype.scrollIntoView = function () {};
      const kept = Object.assign({}, opts.initial || {});
      Object.defineProperty(w, 'localStorage', {value: {getItem: (k) => (k in kept ? kept[k] : null), setItem: (k, v) => { kept[k] = String(v); }, removeItem: (k) => { delete kept[k]; }}, configurable: true});
      w.fetch = async (p, init) => {
        p = String(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (init && init.method === 'POST' && p.startsWith('/api/actions')) { posts.push(JSON.parse(init.body)); return ok({id: 'x', status: 'requested'}); }
        if (init && init.method === 'POST' && p === '/api/routes/send') { sends.push(JSON.parse(init.body)); return opts.sendFails ? {ok: false, status: 500, json: async () => ({})} : ok({ok: true}); }
        if (p === '/api/cards/420') return ok({num: 420, key: 'k420', live: true, svc: 'refunds-api', label: 'Refunds slow', sev: 'warn', status: 'cleared', triage: 'progress', firstSeen: iso(900), lastSeen: iso(800), clearedAt: iso(800)});
        if (p === '/api/cards/412') return ok({num: 412, key: 'k412', live: false, svc: 'payments-api', label: 'Payments slow', sev: 'warn', status: 'cleared', firstSeen: iso(90000), lastSeen: iso(89000)});
        if (p.startsWith('/api/me')) return ok(Object.assign({loginEnabled: true, user: 'op@example.com', operator: true, slack: true, teams: [], tags: [{id: 'team-shop', label: 'Shop team'}],
          routes: [{id: 'ops', name: 'ops-channel'}, {id: 'teams/product', plugin: 'Microsoft Teams', name: 'product'}]}, opts.me || {}));
        if (p.startsWith('/api/actions')) return ok(opts.actions || []);
        if (p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/state')) return ok(state());
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors, posts, sends};
}
const tileOf = (doc, key) => [...doc.querySelectorAll('#anoms .tile')].find((t) => t.dataset.key === key);
async function openCard(doc, key) {
  tileOf(doc, key).click();
  await wait(60);
  return doc.querySelector('.card-drawer article.inc');
}
const said = (n) => (n.getAttribute('aria-label') || n.textContent).trim();
const items = (menu) => [...menu.querySelectorAll('[role="menuitem"]')];
const shown = (menu) => items(menu).filter((n) => !n.hidden);
const groups = (menu) => [...menu.children].reduce((g, n) => { if (n.getAttribute('role') === 'separator') g.push([]); else if (n.getAttribute('role') === 'menuitem' && !n.hidden) g[g.length - 1].push(said(n)); return g; }, [[]]);
const key = (w, el, k) => el.dispatchEvent(new w.KeyboardEvent('keydown', {key: k, bubbles: true, cancelable: true}));

async function moreOpensAShortMenuInGroups() {
  //given
  const {w, doc, errors} = await boot();
  const art = await openCard(doc, MAIN);
  const more = art.querySelector('.acts button.card-more'), menu = art.querySelector('.acts .card-more-box');

  //then
  assert.deepStrictEqual([menu.getAttribute('role'), more.getAttribute('aria-haspopup'), more.getAttribute('aria-expanded'), menu.hidden], ['menu', 'menu', 'false', true]);

  //when
  more.click();

  //then
  assert.deepStrictEqual([more.getAttribute('aria-expanded'), menu.hidden], ['true', false]);
  assert.deepStrictEqual(groups(menu), [
    ['Start progress', 'Mute 24 h', 'Mute 1 w', 'Known noise', 'Keep open'],
    ['Investigate'],
    ['Create Jira task', 'Link ticket'],
    ['Change severity', 'Group', 'Copy link'],
    ['Send to Slack', 'ops-channel', 'Microsoft Teams \u00b7 product']]);
  assert.ok([...menu.children].every((n) => ['menuitem', 'separator'].includes(n.getAttribute('role'))), 'the menu holds only items and separators');
  assert.ok(!menu.querySelector('input, textarea, .reason, .sev-form, .jira-form, .notif, .nl'), 'no form or Notify row inside the menu');
  assert.strictEqual(doc.activeElement, shown(menu)[0], 'the first item takes focus');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'More opens a menu: status, AI, ticket, card and send groups, one action per line';
}

async function theMenuFollowsTheCardState() {
  //given
  const {w, doc, errors, posts} = await boot();

  //when
  const art = await openCard(doc, MUTED);
  art.querySelector('button.card-more').click();
  const menu = art.querySelector('.card-more-box');

  //then
  assert.deepStrictEqual(groups(menu)[0], ['Start progress', 'Unmute', 'Reopen']);

  //when
  shown(menu).find((n) => said(n) === 'Reopen').click();
  await wait(60);

  //then
  assert.deepStrictEqual(posts.map((p) => [p.type, p.key]), [['reopen', MUTED]]);

  //when
  doc.querySelector('.cd-close').click();
  const dived = await openCard(doc, DIVED);
  dived.querySelector('button.card-more').click();
  const dmenu = dived.querySelector('.card-more-box');

  //then
  assert.deepStrictEqual(groups(dmenu)[2], ['OPS-123', 'Change ticket'], 'the ticket group opens or changes the ticket');
  assert.deepStrictEqual(groups(dmenu)[4], ['Send again', 'ops-channel', 'Microsoft Teams \u00b7 product', 'Post update']);

  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the menu shows Unmute and Reopen when they apply, and the ticket and Post update items';
}

async function theMenuWorksFromTheKeyboard() {
  //given
  const {w, doc, errors, posts} = await boot();
  const art = await openCard(doc, MAIN);
  const more = art.querySelector('button.card-more'), menu = art.querySelector('.card-more-box');
  more.click();
  const list = shown(menu);

  //when
  key(w, doc.activeElement, 'ArrowDown');

  //then
  assert.strictEqual(said(doc.activeElement), 'Mute 24 h');

  //when
  key(w, doc.activeElement, 'ArrowUp');
  key(w, doc.activeElement, 'ArrowUp');

  //then
  assert.strictEqual(doc.activeElement, list[list.length - 1], 'ArrowUp from the first item wraps to the last');

  //when
  key(w, doc.activeElement, 'ArrowDown');
  key(w, doc.activeElement, 'ArrowDown');
  key(w, doc.activeElement, 'Enter');
  await wait(60);

  //then
  assert.deepStrictEqual(posts.map((p) => [p.type, p.hours]), [['mute', 24]], 'Enter runs the item');
  assert.strictEqual(menu.hidden, true, 'running an item closes the menu');

  //when
  more.click();
  key(w, doc.activeElement, 'Escape');

  //then
  assert.deepStrictEqual([menu.hidden, doc.activeElement, !!doc.querySelector('.card-drawer')], [true, more, true], 'Esc closes the menu, focuses More and leaves the panel open');

  //when
  more.click();
  art.querySelector('.card-state').click();

  //then
  assert.strictEqual(menu.hidden, true, 'a click outside closes the menu');
  assert.strictEqual(more.getAttribute('aria-expanded'), 'false');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'arrows move, Enter runs, Esc closes back to More and a click outside closes the menu';
}

async function formsOpenAfterTheMenuCloses() {
  //given
  const {w, doc, errors} = await boot();
  const art = await openCard(doc, MAIN);
  const more = art.querySelector('button.card-more'), menu = art.querySelector('.card-more-box');

  //when
  more.click();
  shown(menu).find((n) => said(n) === 'Change severity').click();

  //then
  const sev = art.querySelector('.sev-form');
  assert.deepStrictEqual([menu.hidden, sev.hidden, menu.contains(sev)], [true, false, false], 'Change severity opens its picker beside the menu');
  assert.ok(sev.contains(doc.activeElement), 'focus moves into the picker');

  //when
  more.click();
  shown(menu).find((n) => said(n) === 'Start progress').click();

  //then
  const note = art.querySelector('[id^="take-why-"]');
  assert.ok(note && !note.closest('.reason').hidden && !menu.contains(note), 'Start progress opens its form');

  //when
  more.click();
  shown(menu).find((n) => said(n) === 'Known noise').click();

  //then
  const reason = art.querySelector('[id^="reason-"]');
  assert.deepStrictEqual([!reason.closest('.reason').hidden, doc.activeElement === reason, menu.hidden], [true, true, true]);

  //when
  more.click();
  shown(menu).find((n) => said(n) === 'Link ticket').click();

  //then
  assert.ok(!art.querySelector('.jira-form').hidden && !menu.contains(art.querySelector('.jira-form')), 'Link ticket opens its field');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'Change severity, Start progress, Known noise and Link ticket open their own form after the menu closes';
}

async function aRouteAsksTwiceBeforeItSends() {
  //given
  const {w, doc, errors, sends} = await boot();
  const art = await openCard(doc, MAIN);
  const more = art.querySelector('button.card-more'), menu = art.querySelector('.card-more-box');
  more.click();
  const route = shown(menu).find((n) => said(n) === 'Microsoft Teams \u00b7 product');

  //when
  route.click();

  //then
  assert.strictEqual(menu.hidden, false, 'the menu stays open for the confirm click');
  assert.ok(/^Post to Microsoft Teams/.test(said(route)), said(route));

  //when
  route.click();
  await wait(60);

  //then
  assert.deepStrictEqual(sends, [{key: MAIN, route: 'teams/product'}]);
  assert.strictEqual(menu.hidden, true);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a route in the menu asks once more before it sends, then the menu closes';
}

async function noActionIsFilled() {
  //given
  const {w, doc, errors} = await boot();

  //when
  const art = await openCard(doc, MAIN);
  art.querySelector('button.card-more').click();
  const inv = art.querySelector('.card-more-box .inv-btn');

  //then
  assert.ok(inv.classList.contains('sec'), 'Investigate is a plain item: ' + inv.className);
  const all = [...art.querySelectorAll('.card-main .btn'), ...items(art.querySelector('.card-more-box'))];
  assert.deepStrictEqual(all.filter((b) => !b.classList.contains('sec')).map(said), [], 'every main and menu action is unfilled');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'Investigate looks like every other item and no action uses the accent fill';
}

async function theLastActionShowsOnce() {
  //given
  const done = {id: 'd1', type: 'mute', target: MAIN, key: MAIN, status: 'done', requestedAt: iso(5), doneAt: iso(4), requestedBy: 'server', result: 'muted'};
  const {w, doc, errors} = await boot({actions: [done]});
  const art = await openCard(doc, MAIN);
  await w.__skopaxActions();
  await wait(60);

  //then
  const foot = art.querySelector('.act-foot');
  art.querySelector('.last-act details').open = true;
  assert.strictEqual(art.querySelector('.la-full').textContent, 'by server \u00b7 muted', 'the open fold adds only who and the full result');
  assert.strictEqual(art.querySelector('.acts .stat').textContent, '', 'no second bare status line');
  assert.strictEqual(foot.querySelectorAll('.st').length, 1, 'one status badge, also with the fold open: ' + foot.textContent);
  w.close();

  //given
  const queued = Object.assign({}, done, {id: 'q1', status: 'requested', doneAt: undefined});
  const q = await boot({actions: [queued]});
  const qart = await openCard(q.doc, MAIN);
  await q.w.__skopaxActions();
  await wait(60);

  //then
  const stat = qart.querySelector('.acts .stat');
  assert.strictEqual(stat.querySelectorAll('button').length, 1);
  assert.ok(/^Cancel/.test(said(stat.querySelector('button'))), 'a queued action keeps its Cancel');
  assert.ok(!stat.querySelector('.st'), 'but no second status pill');
  assert.deepStrictEqual(errors.concat(q.errors), []);
  q.w.close();
  return 'the last action shows once, and a queued one keeps only its Cancel beside it';
}

async function theHeaderIsCalm() {
  //given
  const {w, doc, errors} = await boot();

  //when
  const art = await openCard(doc, DIVED);
  const head = art.querySelector('.inc-title');

  //then
  assert.deepStrictEqual([...head.children].map((c) => c.className), ['c c-num', 'c c-sev', 'c c-name', 'c c-status', 'c c-asg']);
  assert.strictEqual(head.querySelector('.c-status').textContent.trim(), 'Investigated', 'the state badge is one word');
  assert.strictEqual(head.querySelector('.c-asg').textContent.trim(), '@alice');
  assert.ok(head.querySelector('.fold').title.includes(LONG), 'the full title is in the title attribute');
  ['.tri-wrap', '.recurring', '.biz-tag', '.slack-st'].forEach((s) => assert.ok(!art.querySelector('.inc-head ' + s), s + ' is not in the header'));
  const fold = art.querySelector('.tech-fold');
  assert.ok(/Investigated/.test(fold.querySelector('.tri-wrap').textContent) && /OPS-123/.test(fold.querySelector('.tri-wrap').textContent), 'Investigated by and the ticket are in Technical details');
  assert.ok(/Recurring \u00d73/.test(fold.querySelector('.recurring').textContent));
  assert.strictEqual(fold.querySelector('.biz-tag').textContent, 'Shop team');
  const links = fold.querySelector('.notif .slack-st');
  assert.ok(links && /Status note/.test(links.textContent), 'the Notify history links are in Technical details');
  assert.ok(!art.querySelector('.acts .notif, .acts .nl'), 'no Notify row on the button area');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the header keeps number, severity, title, state and owner; the rest sits in Technical details';
}

async function theTitleIsClampedToTwoLines() {
  //given
  const css = fs.readFileSync(TEMPLATE, 'utf8');

  //when
  const rule = [...css.matchAll(/(?:^|\n)\.inc-title \.c-name \.fold \{([^}]*)\}/g)].map((m) => m[1]).join(' ');

  //then
  assert.match(rule, /-webkit-line-clamp:\s*2/);
  assert.match(rule, /line-clamp:\s*2/);
  assert.match(rule, /overflow:\s*hidden/);
  return 'the card title is clamped to two lines';
}

async function theListViewSharesTheMenu() {
  //given
  const {w, doc, errors} = await boot({initial: {'skopax-card-view': 'list'}});
  const row = [...doc.querySelectorAll('#anoms article.inc')].find((c) => c.dataset.key === DIVED);

  //when
  row.querySelector('.inc-title .fold').click();
  await wait(60);

  //then
  assert.deepStrictEqual([...row.querySelector('.inc-title').children].map((c) => c.className), ['c c-num', 'c c-sev', 'c c-name', 'c c-status', 'c c-asg', 'c c-age'], 'a list row keeps its Age cell');
  assert.ok(!row.querySelector('.acts-tog, .acts-more'), 'no second kind of menu');
  const more = row.querySelector('.acts button.card-more'), menu = row.querySelector('.acts .card-more-box');

  //when
  more.click();

  //then
  assert.deepStrictEqual([menu.getAttribute('role'), menu.hidden], ['menu', false]);
  assert.ok(shown(menu).some((n) => said(n) === 'Investigate'));
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'list rows get the calm header plus Age, and the same More menu';
}

async function viewersAndPastCardsKeepTheirGates() {
  //given
  const {w, doc, errors} = await boot({me: {user: '', operator: false}});

  //when
  const art = await openCard(doc, MAIN);
  art.querySelector('button.card-more').click();
  const menu = art.querySelector('.card-more-box');

  //then
  assert.ok(!art.querySelector('.login-hint').hidden, 'the log-in hint shows');
  assert.strictEqual(menu.hidden, false, 'a viewer can open the menu');
  assert.ok(shown(menu).filter((n) => said(n) !== 'Copy link').every((n) => n.getAttribute('aria-disabled') === 'true'), 'operator actions stay locked');
  w.close();

  //given
  const past = await boot({hash: '#t=anoms'});

  //when
  past.w.location.hash = '#c=412';
  await wait(150);
  const part = past.doc.querySelector('.card-drawer article.inc');
  part.querySelector('button.card-more').click();

  //then
  assert.deepStrictEqual(shown(part.querySelector('.card-more-box')).map(said), ['Copy link'], 'a past card keeps its reduced set');
  assert.strictEqual(part.querySelector('.card-more-box').getAttribute('role'), 'menu');
  assert.deepStrictEqual(errors.concat(past.errors), []);
  past.w.close();
  return 'viewers see locked items and a log-in hint; a past card keeps only Copy link';
}

async function aQueuedActionLeavesTheMenuUsableByKeyboard() {
  //given
  const queued = {id: 'q1', type: 'dive', target: MAIN, key: MAIN, status: 'requested', requestedAt: iso(1), requestedBy: 'op@example.com', label: 'Investigate'};
  const {w, doc, errors} = await boot({actions: [queued]});
  const art = await openCard(doc, MAIN);
  await w.__skopaxActions();
  await wait(60);
  const more = art.querySelector('button.card-more'), menu = art.querySelector('.card-more-box');

  //when
  more.click();
  const first = doc.activeElement;
  const seen = [said(first)];
  for (let n = 0; n < 4; n++) { key(w, doc.activeElement, 'ArrowDown'); seen.push(said(doc.activeElement)); }

  //then
  assert.ok(menu.contains(first) && !first.disabled, 'focus lands on an item that can run: ' + said(first));
  assert.ok(seen.every((t) => ['Copy link', 'Send to Slack', 'ops-channel', 'Microsoft Teams \u00b7 product'].includes(t)), 'the arrows skip the waiting items: ' + seen.join(', '));

  //when
  key(w, doc.activeElement, 'Escape');
  more.click();
  more.focus();
  key(w, more, 'ArrowDown');

  //then
  assert.ok(menu.contains(doc.activeElement), 'ArrowDown on More moves into the open menu');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'with an action queued the keyboard still reaches Copy link and the send items';
}

async function aFailedSendFromTheClosedMenuSaysSo() {
  //given
  const {w, doc, errors} = await boot({sendFails: true});
  const art = await openCard(doc, MAIN);
  art.querySelector('button.card-more').click();
  const route = shown(art.querySelector('.card-more-box')).find((n) => said(n) === 'Microsoft Teams \u00b7 product');

  //when
  route.click();
  route.click();
  await wait(80);

  //then
  assert.strictEqual(art.querySelector('.card-more-box').hidden, true);
  const toasts = [...doc.querySelectorAll('.fail-toasts .toast')].map((t) => t.textContent);
  assert.ok(toasts.length === 1 && /not saved \(500\)/.test(toasts[0]), 'the failure shows once as a toast though the item sits in a closed menu: ' + toasts.join(' | '));
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a send that fails after the menu closed still shows one fail toast';
}

async function copyAndSendResultsShowAsAToast() {
  //given
  const {w, doc, errors} = await boot();
  const art = await openCard(doc, MAIN);
  const more = art.querySelector('button.card-more'), menu = art.querySelector('.card-more-box');
  const notes = () => [...doc.querySelectorAll('#toasts .toast-t')].map((t) => t.textContent);

  //when
  more.click();
  shown(menu).find((n) => said(n) === 'Copy link').click();
  await wait(20);

  //then
  assert.strictEqual(menu.hidden, true);
  assert.ok(notes().includes('Link is in the address bar'), 'the copy result shows: ' + notes().join(' | '));

  //when
  more.click();
  const route = shown(menu).find((n) => said(n) === 'Microsoft Teams \u00b7 product');
  route.click();
  route.click();
  await wait(80);

  //then
  assert.ok(notes().includes('Sent to Microsoft Teams \u00b7 product'), 'the send result shows: ' + notes().join(' | '));
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'Copy link and a send from the menu show their result in a toast';
}

async function aGroupWithOnlyHiddenItemsHasNoSeparator() {
  //given
  const {w, doc, errors} = await boot({hash: '#t=anoms', me: {user: '', operator: false}});

  //when
  w.location.hash = '#c=420';
  await wait(150);
  const art = doc.querySelector('.card-drawer article.inc');
  art.querySelector('button.card-more').click();
  const menu = art.querySelector('.card-more-box');

  //then
  assert.deepStrictEqual(shown(menu).map(said), ['Copy link'], 'Reopen stays hidden for a viewer');
  assert.deepStrictEqual([...menu.querySelectorAll('[role="separator"]')].filter((n) => !n.hidden).length, 0, 'no separator above the only shown group');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a group whose items are all hidden gets no separator';
}

(async () => {
  for (const t of [moreOpensAShortMenuInGroups, theMenuFollowsTheCardState, theMenuWorksFromTheKeyboard, formsOpenAfterTheMenuCloses, aRouteAsksTwiceBeforeItSends, noActionIsFilled, theLastActionShowsOnce, theHeaderIsCalm, theTitleIsClampedToTwoLines, theListViewSharesTheMenu, viewersAndPastCardsKeepTheirGates, aQueuedActionLeavesTheMenuUsableByKeyboard, aFailedSendFromTheClosedMenuSaysSo, copyAndSendResultsShowAsAToast, aGroupWithOnlyHiddenItemsHasNoSeparator]) {
    try { console.log('PASS ' + await t()); } catch (e) { console.log('FAIL ' + t.name + ': ' + (e && e.stack || e)); process.exitCode = 1; }
  }
  process.exit(process.exitCode || 0);
})();
