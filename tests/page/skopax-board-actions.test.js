const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const OPEN = 'log|checkout-service|ERROR|boom', PROG = 'log|payments-api|ERROR|npe', MUTED = 'log|cart-service|ERROR|quiet';

function card(key, num, extra) {
  return Object.assign({key, num, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'ERROR lines', src: 'logs', sig: 'log', now: 1, firstSeen: iso(30), lastSeen: iso(1), what: 'text'}, extra || {});
}
function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: iso(-59), sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(OPEN, 11), card(PROG, 12, {triage: 'progress', owner: 'alice@example.com', ticket: 'SHOP-123'}), card(MUTED, 13, {muted: true})],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(me, actions, hold) {
  const errors = [], posts = [];
  const vc = new VirtualConsole(); vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE, {view: 'board'}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state())), {
    url: 'https://skopax.example.com/#t=anoms/all', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {}; w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p, opts) => {
        p = String(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (opts && opts.method === 'POST' && p.startsWith('/api/actions')) { posts.push(JSON.parse(opts.body)); if (hold) await hold; return ok({id: 'x', status: 'requested'}); }
        if (p.startsWith('/api/me')) return ok(Object.assign({loginEnabled: true, user: 'op@example.com', operator: true, jira: true, jiraUrl: 'https://jira.example.com', teams: [], tags: []}, me || {}));
        if (p.startsWith('/api/actions')) return ok(actions || []);
        if (p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/state')) return ok(state());
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors, posts};
}
const tileOf = (doc, key) => [...doc.querySelectorAll('#anoms .tile')].find((t) => t.dataset.key === key);
const names = (root) => [...root.querySelectorAll('button, a')].map((b) => b.getAttribute('aria-label') || b.textContent.trim());

async function theDrawerGroupsTheActions() {
  //given
  const {w, doc, errors} = await boot();

  //when
  tileOf(doc, PROG).click();
  await wait(50);
  const acts = doc.querySelector('.card-drawer .acts');
  const parts = [...acts.children].filter((c) => !c.hidden).map((c) => c.className.split(' ')[0] || c.tagName);

  //then
  assert.ok(acts.classList.contains('acts-grouped'));
  assert.deepStrictEqual(parts, ['card-main', 'asg-msg', 'btn', 'act-foot'], 'the main row, the assign note, More, then the foot; the More box starts hidden');
  const main = acts.querySelector('.card-main');
  assert.deepStrictEqual(names(main), ['Take it', 'Assign', 'Resolve', 'Ask AI'], 'someone else holds this card, so Take it shows');
  const chat = main.querySelector('.chat-btn');
  assert.deepStrictEqual([chat.textContent, chat.getAttribute('href')], ['Ask AI', '/#t=chat/new/12']);
  const box = acts.querySelector('.card-more-box');
  acts.querySelector('button.card-more').click();
  const inv = box.querySelector('.inv-btn');
  assert.deepStrictEqual([inv.querySelector('.lbl').textContent, inv.getAttribute('aria-label')], ['Investigate', 'Investigate'], 'Investigate shows its word under More');
  assert.ok(box.querySelector('.sev-act') && box.querySelector('.jira-acts'), 'Change severity and Jira sit under More');
  const key = box.querySelector('.jira-key');
  assert.deepStrictEqual([key.textContent, key.getAttribute('href'), key.target], ['SHOP-123 \u2197', 'https://jira.example.com/browse/SHOP-123', '_blank']);
  const more = [...box.children].filter((c) => c.matches('.btn, .jira-acts')).map((c) => c.matches('.jira-acts') ? 'jira' : c.getAttribute('aria-label') || c.textContent.trim());
  assert.deepStrictEqual(more, ['Mute 24 h', 'Mute 1 w', 'Known noise', 'Start progress', 'Reopen', 'Investigate', 'Group', 'Change severity', 'jira', 'Copy link']);
  assert.ok(box.querySelector('.notif .slack-btn'), 'Send to Slack and the channels sit under More');
  assert.ok(acts.querySelector('.act-foot .stat') && acts.querySelector('.act-foot .last-act'), 'the last action line and its status sit in the foot');
  assert.ok([...acts.querySelectorAll('svg')].every((s) => s.getAttribute('aria-hidden') === 'true'), 'icons are decoration');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the drawer shows Take it, Assign, Resolve and Ask AI, the rest under More, and a foot';
}

async function theDrawerKeepsTheActionsWorking() {
  //given
  const {w, doc, errors, posts} = await boot();
  tileOf(doc, MUTED).click();
  await wait(50);
  const acts = doc.querySelector('.card-drawer .acts');

  //when
  acts.querySelector('button.card-more').click();
  [...acts.querySelectorAll('.card-more-box > .btn')].find((b) => b.getAttribute('aria-label') === 'Unmute').click();
  await wait(50);

  //then
  assert.deepStrictEqual(posts.map((p) => p.type), ['unmute']);
  assert.ok(!doc.querySelector('#anoms .inc:not(.card-drawer *) .acts.acts-grouped'), 'list rows keep the compact row');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the drawer buttons still act: Unmute under More unmutes a muted card';
}

async function aTileShowsOnlyWhatTheBoardNeeds() {
  //given
  const {w, doc, errors} = await boot();

  //when
  const open = tileOf(doc, OPEN).querySelector('.tile-open[aria-haspopup="dialog"]');
  const owned = tileOf(doc, PROG).querySelector('.tile-top .tile-asg');

  //then
  assert.ok(open.tagName === 'BUTTON', 'the tile title is a real button that opens the card');
  assert.match(open.textContent.replace(/\s+/g, ' ').trim(), /^Warning, #11, 30 min, boomcheckout-service \u00b7 ERROR lines$/, 'its name carries the severity, number and age before the title');
  assert.ok([...doc.querySelectorAll('#anoms .tile')].every((t) => !t.hasAttribute('role') && !t.hasAttribute('tabindex')), 'the tile itself is no button, so its buttons are not nested in one');
  assert.strictEqual(doc.querySelectorAll('.tile-acts, .tile .ta-b, .tile .ta-jira, .tile .ta-copy').length, 0, 'a tile has no hover buttons');
  assert.deepStrictEqual([...tileOf(doc, OPEN).querySelectorAll('.tile-top > *')].map((e) => e.className.split(' ')[0]), ['sev', 'tile-num', 'tile-age']);
  assert.deepStrictEqual([owned.textContent, owned.getAttribute('aria-label')], ['A', 'Assignee: alice'], 'the owner shows as initials in the top line');
  assert.strictEqual(tileOf(doc, OPEN).querySelectorAll('button').length, 1, 'the only button on a tile is its title');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a tile shows the severity dot, title, number, age and owner initials, and no action buttons';
}

async function aClickOnAnyPartOfATileOpensTheDrawer() {
  //given
  const {w, doc, errors, posts} = await boot();

  //when
  tileOf(doc, OPEN).querySelector('.tile-open').click();
  const byTitle = !!doc.querySelector('.card-drawer');
  doc.querySelector('.card-drawer .cd-close').click();
  tileOf(doc, OPEN).querySelector('.tile-top').click();
  const byTop = !!doc.querySelector('.card-drawer');

  //then
  assert.ok(byTitle && byTop, 'the title and the top line both open the drawer, so the overlay can let drags through');
  assert.deepStrictEqual(posts, [], 'opening a tile sends nothing');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a click on the title or the top line of a tile opens the drawer and changes nothing';
}

async function aBusyCardCannotBeDragged() {
  //given
  const queued = [{id: 'q1', type: 'dive', target: OPEN, status: 'requested', requestedAt: iso(1), requestedBy: 'op@example.com', label: 'Investigate'}];
  const {w, doc, errors} = await boot({}, queued);
  const drag = (tile) => {
    const e = new w.Event('dragstart', {bubbles: true, cancelable: true});
    tile.dispatchEvent(e);
    return e.defaultPrevented;
  };

  //when
  const busyBlocked = drag(tileOf(doc, OPEN));
  const busyDragging = tileOf(doc, OPEN).classList.contains('dragging');
  const freeBlocked = drag(tileOf(doc, PROG));

  //then
  assert.strictEqual(tileOf(doc, OPEN).draggable, false, 'a card with a queued action is not draggable');
  assert.strictEqual(tileOf(doc, PROG).draggable, true, 'other cards stay draggable');
  assert.ok(busyBlocked && !busyDragging, 'a drag of the busy card is refused');
  assert.ok(!freeBlocked && tileOf(doc, PROG).classList.contains('dragging'), 'a drag of a free card starts');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a card with a queued action is not draggable, like its tile buttons are disabled';
}

async function aDropOnACardThatBecameBusyMidDragSendsNothing() {
  //given
  const {w, doc, errors, posts} = await boot();
  const col = doc.querySelector('#anoms .bcol[data-col="progress"]');

  //when
  tileOf(doc, OPEN).dispatchEvent(new w.Event('dragstart', {bubbles: true}));
  w.__cardBusy = (k) => k === OPEN;
  col.dispatchEvent(new w.Event('dragover', {bubbles: true, cancelable: true}));
  col.dispatchEvent(new w.Event('drop', {bubbles: true, cancelable: true}));
  await wait(60);

  //then
  assert.deepStrictEqual(posts, [], 'a card that got an action queued while it was dragged is not moved by the drop');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a drop on a card that became busy mid-drag sends nothing';
}

async function theDraggableRuleIsOneFunctionTheGateAndTheTileShare() {
  //given
  const {w, doc, errors} = await boot();

  //when
  w.__cardBusy = (k) => k === OPEN;
  w.__skopaxGate();
  const mid = [tileOf(doc, OPEN).draggable, tileOf(doc, PROG).draggable, w.__tileDraggable(OPEN), w.__tileDraggable(PROG)];
  w.__cardBusy = () => false;
  w.__skopaxGate();
  const after = [tileOf(doc, OPEN).draggable, w.__tileDraggable(OPEN)];

  //then
  assert.deepStrictEqual(mid, [false, true, false, true], 'a tile mid-move is not draggable after a gate pass either');
  assert.deepStrictEqual(after, [true, true]);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the gate and the tile use one draggable rule';
}

function theTileOverlayLetsDragsThrough() {
  //given
  const css = fs.readFileSync(TEMPLATE, 'utf8');

  //then
  assert.ok(/\.tile-open::after \{[^}]*pointer-events: none/.test(css), 'the title overlay passes the pointer to the tile, so a drag can start from it');
  return 'the title overlay lets pointer events through to the draggable tile';
}

async function aViewerCannotDragATile() {
  //given
  const {w, doc, errors} = await boot({operator: false, user: 'viewer@example.com'});

  //when
  const tile = tileOf(doc, OPEN);

  //then
  assert.strictEqual(tile.draggable, false);
  assert.strictEqual(tile.querySelectorAll('button').length, 1, 'a viewer still gets only the title button');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a viewer gets a tile that opens the card but cannot be dragged';
}

function theDrawerGroupsHaveTheirStyles() {
  //given
  const css = fs.readFileSync(TEMPLATE, 'utf8');

  //then
  assert.ok(!/\.tile-acts/.test(css), 'no tile action row styles are left');
  assert.ok(/\.acts-panel \.card-main, \.acts-panel \.card-more-box \{ display: flex; flex-wrap: wrap;/.test(css), 'the main row and the More box wrap on a narrow panel');
  assert.ok(!/\.act-group|\.ag-body/.test(css), 'the old action group styles are gone');
  return 'no tile row styles are left, and the panel rows wrap on a narrow panel';
}

(async () => {
  console.log('PASS ' + await theDrawerGroupsTheActions());
  console.log('PASS ' + await theDrawerKeepsTheActionsWorking());
  console.log('PASS ' + await aTileShowsOnlyWhatTheBoardNeeds());
  console.log('PASS ' + await aClickOnAnyPartOfATileOpensTheDrawer());
  console.log('PASS ' + await aBusyCardCannotBeDragged());
  console.log('PASS ' + await aDropOnACardThatBecameBusyMidDragSendsNothing());
  console.log('PASS ' + await theDraggableRuleIsOneFunctionTheGateAndTheTileShare());
  console.log('PASS ' + theTileOverlayLetsDragsThrough());
  console.log('PASS ' + await aViewerCannotDragATile());
  console.log('PASS ' + theDrawerGroupsHaveTheirStyles());
  process.exit(0);
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
