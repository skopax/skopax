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
  const parts = [...acts.children].filter((c) => !c.hidden).map((c) => c.classList.contains('act-group') ? doc.getElementById(c.getAttribute('aria-labelledby')).textContent : c.className);

  //then
  assert.ok(acts.classList.contains('acts-grouped'));
  assert.deepStrictEqual(parts, ['AI', 'Status', 'Owner', 'Links', 'act-foot']);
  const chat = acts.querySelector('.act-group.ai .chat-btn');
  assert.deepStrictEqual([chat.textContent, chat.getAttribute('href')], ['Chat about this card', '/#t=chat/new/12']);
  const inv = acts.querySelector('.act-group.ai .inv-btn');
  assert.deepStrictEqual([inv.querySelector('.lbl').textContent, inv.getAttribute('aria-label')], ['Investigate', 'Investigate'], 'Investigate shows its word in the drawer');
  const status = doc.getElementById('ag-status-' + acts.dataset.i).closest('.act-group');
  const seg = [...status.querySelectorAll('.seg > button')].map((b) => [b.textContent, b.getAttribute('aria-pressed')]);
  assert.deepStrictEqual(seg, [['In progress', 'true'], ['Resolve', 'false'], ['Mute 24 h', 'false']], 'the card is in progress, so the pressed first segment says In progress');
  assert.deepStrictEqual([status.querySelector('.seg > button').getAttribute('aria-label'), status.querySelector('.seg > button').title.startsWith('Start progress')], ['In progress', true], 'its name matches the word and the tip stays');
  assert.ok(status.querySelector('.sev-act') && status.querySelector('.acts-tog'), 'Change severity and the More menu sit with the status');
  assert.deepStrictEqual([...status.querySelectorAll('.acts-more > .btn')].map((b) => b.textContent), ['Mute 1 w', 'Known noise', 'Group'], 'the rarer status moves stay behind More');
  const owner = doc.getElementById('ag-owner-' + acts.dataset.i).closest('.act-group');
  assert.ok(owner.querySelector(':scope > .ag-body > .asg-btn') && !status.querySelector('.asg-btn'), 'the assignee has its own group');
  const links = acts.querySelector('.act-group.links');
  const key = links.querySelector('.jira-key');
  assert.deepStrictEqual([key.textContent, key.getAttribute('href'), key.target], ['SHOP-123 \u2197', 'https://jira.example.com/browse/SHOP-123', '_blank']);
  assert.deepStrictEqual(names(links), ['SHOP-123 \u2197', 'Change ticket', 'Copy link']);
  assert.ok(acts.querySelector('.act-foot .stat') && acts.querySelector('.act-foot .last-act'), 'the last action line and its status sit in the foot');
  assert.ok([...acts.querySelectorAll('svg')].every((s) => s.getAttribute('aria-hidden') === 'true'), 'icons are decoration');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the drawer shows Chat and Investigate, then Status (segmented, pressed state, More), Owner and Links groups and a foot';
}

async function theDrawerKeepsTheActionsWorking() {
  //given
  const {w, doc, errors, posts} = await boot();
  tileOf(doc, MUTED).click();
  await wait(50);
  const acts = doc.querySelector('.card-drawer .acts');

  //when
  const seg = [...doc.getElementById('ag-status-' + acts.dataset.i).closest('.act-group').querySelectorAll('.seg > button')];
  seg.find((b) => b.textContent === 'Unmute').click();
  await wait(50);

  //then
  assert.deepStrictEqual(seg.map((b) => [b.textContent, b.getAttribute('aria-pressed')]), [['Start progress', 'false'], ['Resolve', 'false'], ['Unmute', 'true']], 'a muted card shows Unmute pressed');
  assert.deepStrictEqual(posts.map((p) => p.type), ['unmute']);
  assert.ok(!doc.querySelector('#anoms .inc:not(.card-drawer *) .acts.acts-grouped'), 'list rows keep the compact row');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the grouped drawer buttons still act: Unmute unmutes a muted card';
}

async function aTileHasAQuietActionRow() {
  //given
  const {w, doc, errors} = await boot();

  //when
  const row = (key) => tileOf(doc, key).querySelector('.tile-acts');

  //then
  const open = tileOf(doc, OPEN).querySelector('.tile-open[aria-haspopup="dialog"]');
  assert.ok(open.tagName === 'BUTTON', 'the tile title is a real button that opens the card');
  assert.match(open.textContent.replace(/\s+/g, ' ').trim(), /^Warning, #11, 30 min, boomcheckout-service \u00b7 ERROR lines$/, 'its name carries the severity, number and age before the title');
  assert.ok([...doc.querySelectorAll('#anoms .tile')].every((t) => !t.hasAttribute('role') && !t.hasAttribute('tabindex')), 'the tile itself is no button, so its buttons are not nested in one');
  assert.deepStrictEqual(names(row(OPEN)), ['Start progress', 'Resolve', 'Mute', 'Copy link']);
  assert.deepStrictEqual(names(row(PROG)), ['Reopen', 'Resolve', 'Mute', 'Open SHOP-123 in Jira', 'Copy link'], 'each button is a board move this card can make, like a drag');
  assert.deepStrictEqual(names(row(MUTED)), ['Reopen', 'Start progress', 'Resolve', 'Copy link']);
  assert.ok([...doc.querySelectorAll('.tile-acts button')].every((b) => b.title && b.getAttribute('aria-label')), 'every tile button has a tip and a name');
  assert.ok([...doc.querySelectorAll('.tile-acts svg')].every((s) => s.getAttribute('aria-hidden') === 'true'));
  const jira = row(PROG).querySelector('a.ta-jira');
  assert.deepStrictEqual([jira.getAttribute('href'), jira.target, !!jira.querySelector('.ext')], ['https://jira.example.com/browse/SHOP-123', '_blank', true]);
  assert.ok(row(PROG).querySelector('.tile-asg'), 'the assignee avatar moves into the row');
  assert.strictEqual(row(PROG).getAttribute('role'), 'group');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a tile ends with a row of icon buttons: status moves, the assignee, then the Jira key and Copy link';
}

async function aTileActionDoesNotOpenTheDrawer() {
  //given
  const {w, doc, errors, posts} = await boot();
  const start = tileOf(doc, OPEN).querySelector('.tile-acts [aria-label="Start progress"]');

  //when
  start.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
  const keyOpened = !!doc.querySelector('.card-drawer');
  start.click();
  await wait(80);

  //then
  assert.strictEqual(keyOpened, false, 'Enter on a tile button does not open the drawer');
  assert.strictEqual(doc.querySelector('.card-drawer'), null, 'a click on a tile button does not open the drawer');
  assert.deepStrictEqual(posts.map((p) => [p.type, p.key]), [['take', OPEN]], 'Start progress moves the card like a drag to In progress');
  tileOf(doc, OPEN).querySelector('.tile-open').click();
  assert.ok(doc.querySelector('.card-drawer'), 'the tile title still opens the drawer');
  doc.querySelector('.card-drawer .cd-close').click();
  tileOf(doc, OPEN).querySelector('.tile-top').click();
  assert.ok(doc.querySelector('.card-drawer'), 'a click on the tile body outside the title opens the drawer too, so the overlay can let drags through');
  doc.querySelector('.card-drawer .cd-close').click();
  tileOf(doc, OPEN).querySelector('.tile-acts').click();
  assert.strictEqual(doc.querySelector('.card-drawer'), null, 'a click in the action row, between buttons, does not open it');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a tile action acts on the card without opening the drawer, by click or key';
}

async function aDoubleClickSendsOneMoveAndTheFocusComesBack() {
  //given
  const {w, doc, errors, posts} = await boot();
  const start = tileOf(doc, MUTED).querySelector('.tile-acts [aria-label="Start progress"]');
  start.focus();

  //when
  start.click();
  const busy = [start.disabled, start.getAttribute('aria-busy')];
  start.click();
  start.dispatchEvent(new w.MouseEvent('click', {bubbles: true}));
  await wait(400);

  //then
  assert.deepStrictEqual(busy, [true, 'true'], 'the button waits while the move runs');
  assert.deepStrictEqual(posts.map((p) => p.type), ['unmute', 'take'], 'two clicks send the move once');
  const now = tileOf(doc, MUTED).querySelector('.tile-acts [aria-label="Start progress"]');
  assert.ok(now && doc.activeElement === now && !now.disabled && !now.hasAttribute('aria-busy'), 'after the refresh the focus is on the same button of the new tile');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a double click on a tile button sends the move once, the button is busy meanwhile, and the focus comes back to it';
}

async function aKeyOnATileButtonStillReachesThePage() {
  //given
  const {w, doc, errors} = await boot();
  const copy = tileOf(doc, OPEN).querySelector('.tile-acts .ta-copy');
  let seen = null;
  doc.addEventListener('keydown', (e) => { seen = e.key; });
  Object.defineProperty(w.navigator, 'clipboard', {value: {writeText: () => Promise.resolve()}, configurable: true});

  //when
  copy.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  copy.click();
  await wait(30);

  //then
  assert.strictEqual(seen, 'Escape', 'a key on a tile button is not swallowed');
  assert.deepStrictEqual([copy.title, doc.getElementById('board-note').textContent, copy.querySelector('svg') && copy.textContent], ['Copied', 'Copied', 'Copy link'], 'Copy link says Copied through its tip and the live note, not inside the small button');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'keys on tile buttons reach the page, and Copy link reports through its tip and the live note';
}

async function aCardWithAQueuedActionHasItsTileMovesDisabled() {
  //given
  const queued = [{id: 'q1', type: 'dive', target: OPEN, status: 'requested', requestedAt: iso(1), requestedBy: 'op@example.com', label: 'Investigate'}];

  //when
  const {w, doc, errors} = await boot({}, queued);
  const moves = (key) => [...tileOf(doc, key).querySelectorAll('.tile-acts .ta-b[data-move]')].map((b) => b.disabled);

  //then
  assert.ok(moves(OPEN).length && moves(OPEN).every(Boolean), 'the busy card cannot be moved from its tile');
  assert.ok(moves(PROG).every((d) => !d), 'other cards stay movable');
  assert.ok(!tileOf(doc, OPEN).querySelector('.ta-copy').disabled, 'Copy link stays usable');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a card with a queued action has its tile move buttons disabled, like the drawer';
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

async function aTileRedrawnMidMoveLooksBusyAndCannotBeDragged() {
  //given
  let release;
  const hold = new Promise((r) => { release = r; });
  const {w, doc, errors, posts} = await boot({}, [], hold);
  const move = (key) => tileOf(doc, key).querySelector('.tile-acts [data-move="progress"]');
  const buttons = (key) => [...tileOf(doc, key).querySelectorAll('.tile-acts .ta-b[data-move]')];

  //when
  move(OPEN).click();
  await wait(30);
  w.bumpRender();
  w.renderAnoms();
  const redrawn = tileOf(doc, OPEN);
  const during = {all: buttons(OPEN).every((b) => b.disabled), busy: move(OPEN).getAttribute('aria-busy'), others: buttons(OPEN).filter((b) => b !== move(OPEN)).some((b) => b.hasAttribute('aria-busy')), draggable: redrawn.draggable};
  const e = new w.Event('dragstart', {bubbles: true, cancelable: true});
  redrawn.dispatchEvent(e);
  move(OPEN).click();
  release();
  await wait(400);

  //then
  assert.deepStrictEqual(during, {all: true, busy: 'true', others: false, draggable: false}, 'the redrawn tile looks busy: buttons disabled, the clicked one marked, not draggable');
  assert.ok(e.defaultPrevented, 'the redrawn tile cannot be dragged while the move runs');
  assert.strictEqual(posts.filter((p) => p.type === 'take').length, 1, 'one move was sent');
  assert.ok(buttons(OPEN).length && buttons(OPEN).every((b) => !b.disabled && !b.hasAttribute('aria-busy')), 'when the move ends the redrawn tile is usable again');
  assert.strictEqual(tileOf(doc, OPEN).draggable, true, 'and draggable again');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a tile redrawn while its move runs looks busy and cannot be dragged, and is usable again when the move ends';
}

function theTileOverlayLetsDragsThrough() {
  //given
  const css = fs.readFileSync(TEMPLATE, 'utf8');

  //then
  assert.ok(/\.tile-open::after \{[^}]*pointer-events: none/.test(css), 'the title overlay passes the pointer to the tile, so a drag can start from it');
  return 'the title overlay lets pointer events through to the draggable tile';
}

async function aViewerOnlyGetsTheLinksOnATile() {
  //given
  const {w, doc, errors} = await boot({operator: false, user: 'viewer@example.com'});

  //when
  const row = tileOf(doc, OPEN).querySelector('.tile-acts');

  //then
  assert.deepStrictEqual(names(row), ['Copy link']);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a viewer gets no status buttons on a tile, only Copy link';
}

function theTileRowAndTheDrawerGroupsHaveTheirStyles() {
  //given
  const css = fs.readFileSync(TEMPLATE, 'utf8');

  //then
  assert.ok(/\.tile-acts \{[^}]*border-top: 1px solid var\(--line\)/.test(css), 'the tile row sits behind a hairline');
  assert.ok(/\.tile-acts \.ta-b:focus-visible \{[^}]*outline/.test(css), 'tile buttons show a focus ring');
  assert.ok(/@container \(max-width: 520px\) \{[^@]*\.acts-grouped \.act-group \{ grid-template-columns: minmax\(0, 1fr\);/.test(css), 'the drawer groups stack on a narrow panel');
  return 'the tile row has a hairline and focus rings, and the drawer groups stack on a narrow panel';
}

(async () => {
  console.log('PASS ' + await theDrawerGroupsTheActions());
  console.log('PASS ' + await theDrawerKeepsTheActionsWorking());
  console.log('PASS ' + await aTileHasAQuietActionRow());
  console.log('PASS ' + await aTileActionDoesNotOpenTheDrawer());
  console.log('PASS ' + await aDoubleClickSendsOneMoveAndTheFocusComesBack());
  console.log('PASS ' + await aKeyOnATileButtonStillReachesThePage());
  console.log('PASS ' + await aCardWithAQueuedActionHasItsTileMovesDisabled());
  console.log('PASS ' + await aBusyCardCannotBeDragged());
  console.log('PASS ' + await aTileRedrawnMidMoveLooksBusyAndCannotBeDragged());
  console.log('PASS ' + theTileOverlayLetsDragsThrough());
  console.log('PASS ' + await aViewerOnlyGetsTheLinksOnATile());
  console.log('PASS ' + theTileRowAndTheDrawerGroupsHaveTheirStyles());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
