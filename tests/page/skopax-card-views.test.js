const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const OLD = 'log|old-service|WARN|gone', NEW = 'log|checkout-service|ERROR|boom', WAIT = 'log|payments-api|ERROR|npe', PROG = 'log|inventory-service|ERROR|x', DONE = 'log|shop-web|WARN|y', MUTED = 'log|bff|ERROR|z';

function state(more) {
  const card = (key, extra) => Object.assign({key, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'ERROR lines', src: 'logs', sig: 'log', now: 1, firstSeen: iso(90), lastSeen: iso(1)}, extra || {});
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(NEW, {sev: 'crit', num: 153, owner: 'alice@example.com', tags: [{id: 'data', by: 'rule', at: iso(5)}]}), card(WAIT, {status: 'cleared', clearedAt: iso(20), num: 150, sevSet: {sev: 'warn', by: 'carol@example.com', at: iso(10), auto: 'crit'}, sevAuto: 'crit'}),
      card(PROG, {triage: 'progress', owner: 'Carol Diaz', num: 149, ticket: 'OPS-1234'}), card(DONE, {triage: 'handled', status: 'cleared', clearedAt: iso(30), num: 140}), card(MUTED, {muted: {until: iso(-600)}, num: 141}),
      card(OLD, {triage: 'handled', status: 'cleared', clearedAt: iso(3 * 24 * 60), triagedAt: iso(3 * 24 * 60), num: 120})].concat(more || []),
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(hash, stored, opts) {
  const errors = [], posted = [];
  const o = opts || {};
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE, {view: o.view || 'list'}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state(o.more))), {
    url: 'https://skopax.example.com/' + (hash || '#t=anoms/all'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      if (stored) Object.keys(stored).forEach((k) => w.localStorage.setItem(k, stored[k]));
      w.fetch = async (path, init) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/actions') && init && init.method === 'POST') { const b = JSON.parse(init.body); posted.push(b); return ok({id: 'x' + posted.length, type: b.type, status: o.refuse ? 'failed' : 'done', result: o.refuse || 'ok'}); }
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: o.guest ? null : 'alice@example.com', operator: o.guest ? false : true, teams: [], tags: [{id: 'developer', label: 'Developer'}, {id: 'data', label: 'Data'}, {id: 'infra', label: 'Infrastructure'}, {id: 'analyst', label: 'Analyst'}], jiraUrl: 'https://jira.example.com', task: {projects: ['OPS']}});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(450);
  return {w: dom.window, doc: dom.window.document, errors, posted};
}

const tileOf = (doc, key) => [...doc.querySelectorAll('#anoms .tile')].find((x) => x.dataset.key === key);
const colEl = (doc, col) => doc.querySelector('#anoms .bcol[data-col="' + col + '"]');
function drag(w, doc, key, col) {
  const t = tileOf(doc, key);
  t.dispatchEvent(new w.Event('dragstart', {bubbles: true}));
  const marks = [...doc.querySelectorAll('#anoms .bcol')].map((c) => c.dataset.col + ':' + (c.classList.contains('drop-ok') ? 'ok' : 'no'));
  const over = new w.Event('dragover', {bubbles: true, cancelable: true});
  colEl(doc, col).dispatchEvent(over);
  const allowed = over.defaultPrevented;
  colEl(doc, col).dispatchEvent(new w.Event('drop', {bubbles: true, cancelable: true}));
  return {marks, allowed};
}

const colOf = (doc, key) => { const t = [...doc.querySelectorAll('#anoms .tile')].find((x) => x.dataset.key === key); return t ? t.closest('.bcol').dataset.col : null; };

async function theBoardIsTheDefaultAndAllTopsTheMenu() {
  //given
  const {w, doc, errors} = await boot('#t=anoms/all', null, {view: 'default'});

  //when
  const pressed = [...doc.querySelectorAll('#card-view button')].map((b) => b.dataset.view + ':' + b.getAttribute('aria-pressed'));
  const menu = [...doc.querySelectorAll('#tab-sub-anoms button[data-f]')].slice(0, 2).map((b) => b.dataset.f);

  //then
  assert.ok(doc.querySelector('#anoms .board') && doc.getElementById('anoms').classList.contains('view-board'), 'with nothing stored the Board shows');
  assert.deepStrictEqual(pressed, ['board:true', 'list:false'], 'Board comes first in the toggle and is pressed');
  assert.deepStrictEqual(menu, ['all', 'open'], 'All tops the filter menu, then Open');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the Board is the default view (List still one click away and remembered), and All tops the filter menu';
}

async function assigneeAndTagChipsFilterTheBoardTogetherAndALinkKeepsThem() {
  //given
  const {w, doc, errors} = await boot('#t=anoms/all', {'skopax-card-view': 'board'});
  const chips = () => [...doc.querySelectorAll('#card-qf .qf-c')].map((b) => b.dataset.type + (b.getAttribute('aria-pressed') === 'true' ? '*' : '') + ' ' + b.querySelector('.count').textContent);
  const shown = () => [...doc.querySelectorAll('#anoms .tile')].map((t) => t.dataset.key).sort();
  const chip = (t) => doc.querySelector('#card-qf .qf-c[data-type="' + t + '"]');

  //when
  const listed = chips();
  const colors = [chip('tag:data').classList.contains('t-data'), !!doc.querySelector('#anoms .tile .tag.t-data'), !!doc.querySelector('#tab-sub-anoms .tags-g .t-data'), chip('who:me').firstChild.textContent];
  chip('who:u:alice').click();
  const nina = shown();
  chip('tag:data').click();
  const both = {cards: shown(), hash: w.location.hash, menu: doc.querySelector('#tab-sub-anoms .tags-g [data-type="tag:data"]').getAttribute('aria-pressed')};
  chip('who:none').click();
  const swapped = shown();
  const reload = await boot('#t=anoms/all/' + encodeURIComponent('who:u:alice+tag:data'), {'skopax-card-view': 'board'});

  //then
  assert.deepStrictEqual(listed, ['tag:data 1', 'who:me 1', 'who:none 3', 'who:u:alice 1', 'who:u:carol.diaz 1'], 'the tags in use on their own row on top, then Me, Unassigned and each person with cards');
  assert.deepStrictEqual([...doc.querySelectorAll('#card-qf .qf-g')].map((g) => g.getAttribute('aria-label')), ['Tags', 'Assignee'], 'two rows, Tags first');
  assert.deepStrictEqual(colors, [true, true, true, 'Me'], 'each tag has its own colour on the chip, the tile and the side menu; Mine reads Me');
  assert.deepStrictEqual(nina, [NEW]);
  assert.deepStrictEqual(both, {cards: [NEW], hash: '#t=anoms/all/' + encodeURIComponent('who:u:alice+tag:data'), menu: 'true'}, 'an assignee and a tag combine, the link and the side menu follow');
  assert.deepStrictEqual(swapped, [], 'another assignee replaces the first one and keeps the tag');
  assert.deepStrictEqual([...reload.doc.querySelectorAll('#anoms .tile')].map((t) => t.dataset.key), [NEW], 'a copied link keeps both');
  assert.deepStrictEqual(errors.concat(reload.errors), []);
  w.close(); reload.w.close();
  return 'the toolbar has Assignee chips (Mine, Unassigned, people with initials) and Tag chips; one of each combines, the side menu and the link follow';
}

async function draggingATileMovesTheCardAndOnlyMutedAsksHowLong() {
  //given
  const {w, doc, errors, posted} = await boot('#t=anoms/all', {'skopax-card-view': 'board'});

  //when
  const toProgress = drag(w, doc, NEW, 'progress');
  await wait(80);
  const toOpenFromProgress = drag(w, doc, PROG, 'open');
  await wait(80);
  const doneToOpen = drag(w, doc, DONE, 'open');
  await wait(80);
  const mutedToDone = drag(w, doc, MUTED, 'done');
  doc.querySelector('.resolve-pick .rp-yes').click();
  await wait(80);
  const note = doc.getElementById('board-note').textContent;
  const sent = posted.splice(0).map((b) => b.type + ' ' + b.key);
  const toMuted = drag(w, doc, WAIT, 'muted');
  await wait(30);
  const dialog = doc.querySelector('.mute-pick');
  const options = [...dialog.querySelectorAll('input[name="mp"]')].map((r) => r.value);
  dialog.querySelector('input[value="known"]').click();
  dialog.querySelector('input[value="known"]').dispatchEvent(new w.Event('change', {bubbles: true}));
  dialog.querySelector('.mp-why').value = 'month end batch';
  dialog.querySelector('.mp-yes').click();
  await wait(80);
  const muted = posted.splice(0);
  drag(w, doc, NEW, 'muted');
  await wait(30);
  doc.querySelector('.mute-pick .mp-no').click();
  await wait(30);
  const cancelled = {sent: posted.length, open: !!doc.querySelector('.mute-pick')};
  const blocked = [drag(w, doc, NEW, 'open').allowed, drag(w, doc, DONE, 'muted').allowed];
  await wait(30);

  //then
  assert.deepStrictEqual(toProgress.marks, ['open:no', 'progress:ok', 'done:ok', 'muted:ok'], 'while dragging, the columns it can go to are marked');
  assert.ok(toProgress.allowed && toOpenFromProgress.allowed && doneToOpen.allowed && mutedToDone.allowed);
  assert.deepStrictEqual(sent, ['take ' + NEW, 'reopen ' + PROG, 'reopen ' + DONE, 'unmute ' + MUTED, 'resolve ' + MUTED], 'In progress takes, back to Open reopens, a muted card is unmuted first; only Done asks how (skopax-resolve-kinds)');
  assert.ok(note.startsWith('Moved #141 bff ERROR lines to Done'), note);
  assert.ok(toMuted.allowed && dialog, 'Muted needs a duration, so it asks');
  assert.deepStrictEqual(options, ['24', '168', 'known']);
  assert.deepStrictEqual(muted.map((b) => [b.type, b.key, b.reason]), [['known', WAIT, 'month end batch']]);
  assert.deepStrictEqual(cancelled, {sent: 0, open: false}, 'Cancel moves nothing');
  assert.deepStrictEqual(blocked, [false, false], 'an open card is already in Open, and a done card cannot be muted');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'dragging a tile to In progress takes it, to Done resolves it after asking how, back to Open reopens it (unmuting first when muted), and Muted asks for 24 h, 1 week or known noise with a reason';
}

async function guestsCannotDragAndARefusedMoveSaysWhy() {
  //given
  const guest = await boot('#t=anoms/all', {'skopax-card-view': 'board'}, {guest: true});
  const refused = await boot('#t=anoms/all', {'skopax-card-view': 'board'}, {refuse: 'PROG is critical'});

  //when
  const draggable = [...guest.doc.querySelectorAll('#anoms .tile')].map((t) => t.draggable);
  drag(refused.w, refused.doc, NEW, 'done');
  refused.doc.querySelector('.resolve-pick .rp-yes').click();
  await wait(80);
  const note = refused.doc.getElementById('board-note');

  //then
  assert.ok(draggable.length && draggable.every((d) => d === false), 'without a login no tile can be dragged');
  assert.deepStrictEqual([note.textContent, note.classList.contains('bad')], ['Not moved: PROG is critical', true]);
  assert.deepStrictEqual(guest.errors.concat(refused.errors), []);
  guest.w.close(); refused.w.close();
  return 'tiles are draggable only with the operator role, and a move the server refuses shows its reason above the board';
}

async function jiraCreateAndLinkShowAsTextButtonsOutsideTheFoldedActions() {
  //given
  const {w, doc, errors, posted} = await boot('#t=anoms/all');
  const acts = (key) => [...doc.querySelectorAll('#anoms article.inc')].find((c) => c.dataset.key === key).querySelector('.acts');

  //when
  const free = acts(NEW).querySelector('.jira-acts');
  const labels = [...free.querySelectorAll('.jira-act')].map((b) => b.textContent);
  const folded = !!free.closest('.acts-more');
  [...free.querySelectorAll('.jira-act')].find((b) => b.textContent === 'Link ticket').click();
  const form = acts(NEW).querySelector('.jira-form');
  const shown = !form.hidden;
  const input = form.querySelector('input');
  input.value = 'disk is full';
  form.querySelector('.jira-go').click();
  const refused = {msg: form.querySelector('.jira-msg').textContent, sent: posted.length};
  input.value = 'ops-145';
  form.querySelector('.jira-go').click();
  await wait(80);
  const linked = acts(PROG).querySelector('.jira-acts');
  const open = linked.querySelector('a.jira-key');

  //then
  assert.deepStrictEqual(labels, ['Create Jira task', 'Link ticket'], 'a card without a ticket shows both as text buttons, and Link names no Jira while Jira is off');
  assert.strictEqual(folded, false, 'they stay out of the More actions fold');
  assert.ok(shown, 'Link opens a field for the key');
  assert.deepStrictEqual(refused, {msg: 'Type the ticket key, like OPS-123', sent: 0}, 'words with no key are not sent (no Jira search on this service, so it asks for the key)');
  assert.deepStrictEqual(posted.map((b) => [b.type, b.key, b.ticket]), [['link', NEW, 'OPS-145']]);
  assert.deepStrictEqual([open.textContent, open.getAttribute('href'), [...linked.querySelectorAll('.jira-act')].map((b) => b.textContent)], ['OPS-1234 ↗', 'https://jira.example.com/browse/OPS-1234', ['Change ticket']], 'a card with a ticket links it and offers Change ticket');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'Create Jira task and Link ticket sit as text buttons next to Investigate and Assign; Link takes a key (or a pick from the Jira search) and a card with a ticket shows it with Change ticket';
}

async function severityChangesFromTheCardAndAHandSetOneSaysSo() {
  //given
  const {w, doc, errors, posted} = await boot('#t=anoms/all');
  const inc = (key) => [...doc.querySelectorAll('#anoms article.inc')].find((c) => c.dataset.key === key);

  //when
  const open = inc(NEW).querySelector('.acts .sev-act');
  const folded = !!open.closest('.acts-more');
  open.click();
  const picks = [...inc(NEW).querySelectorAll('.sev-form .sev-pick')].map((b) => b.textContent);
  [...inc(NEW).querySelectorAll('.sev-pick')].find((b) => b.dataset.sev === 'info').click();
  await wait(80);
  const hand = inc(WAIT).querySelector('.tag.sev-hand');
  const handPicks = [...inc(WAIT).querySelectorAll('.sev-form .sev-pick')].map((b) => b.dataset.sev + (b.getAttribute('aria-pressed') === 'true' ? '*' : ''));

  //then
  assert.ok(open.textContent === 'Change severity' && !folded, 'Change severity is a text button outside the More actions fold');
  assert.deepStrictEqual(picks, ['Critical', 'Warning', 'Info'], 'no Back to the signal while the signal decides');
  assert.deepStrictEqual(posted.map((b) => [b.type, b.key, b.sev]), [['severity', NEW, 'info']]);
  assert.ok(hand && /set by hand by carol/.test(hand.title) && /the signal says Critical/.test(hand.title), 'a hand-set severity shows a tag saying who set it and what the signal says');
  assert.deepStrictEqual(handPicks, ['crit', 'warn*', 'info', 'auto'], 'it can go back to the signal');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'Change severity offers Critical, Warning and Info (plus Back to the signal once set by hand), and a hand-set card carries a Severity by hand tag naming who and the signal\'s own severity';
}

async function tilesAndRowsTitleACardByItsMessageOrItsBusinessSentence() {
  //given
  const {w, doc, errors} = await boot('#t=anoms/all', {'skopax-card-view': 'board'});
  const title = (card) => w.cardTitle(card);

  //when
  const got = [
    title({key: 'log|payments-api|WARN|Store not found.(error.store.not-found)'}),
    title({key: 'log|web-bff|WARN|Store not found.(Store not found.)'}),
    title({key: 'log|shop-web|WARN|OrderDetailPage - Api: getStoreDetails - Request data: {"slug":"brightshop"} - service error:'}),
    title({key: 'log|shop-web|WARN|Error in HTTP response: {"url":"http://api-gateway-public/x"}'}),
    title({key: 'page-typos|example.com/x', svc: 'example.com/x', tags: [{id: 'bizdev'}], business: {text: 'example.com/x: change "a.b" to "a. b" in the copy.'}}),
    title({key: 'integrity-fail|identity/session-service|ANDROID', svc: 'session-service', label: 'integrity failure share'}),
  ];
  const tileOfNew = [...doc.querySelectorAll('#anoms .tile')].find((t) => t.dataset.key === NEW);
  const info = title({key: 'page-typos|example.com/y', svc: 'example.com/y', tags: [{id: 'bizdev'}], business: {text: 'The sale page is down for visitors.', kind: 'info'}});
  const east = w.tile({key: 'no-access|-/argo|applications', svc: 'argo', label: 'Argo CD cannot read applications', sev: 'warn', status: 'open', cluster: 'east', firstSeen: new Date().toISOString()});

  //then
  assert.deepStrictEqual(got, ['Store not found', 'Store not found', 'OrderDetailPage \u00b7 getStoreDetails', 'Error in HTTP response', 'Recommended: change "a.b" to "a. b" in the copy.', '']);
  assert.strictEqual(info, 'The sale page is down for visitors.', 'an info sentence is not a recommendation');
  assert.ok(east.querySelector('.tile-sig').textContent.endsWith('instance east') && east.title.includes('instance east'), 'the tile names the instance');
  assert.deepStrictEqual([tileOfNew.querySelector('.tile-title').textContent, tileOfNew.querySelector('.tile-sig').textContent], ['boom', 'checkout-service · ERROR lines'], 'the tile leads with the message and puts the service and signal under it');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a card is titled by its log message (no request data, JSON or repeated code) or, for a business team, by its sentence without the page address; others keep service and label';
}

async function aCardWithNoChangeFor48HoursMovesToArchivedAndShowsNowhereElse() {
  //given
  const {w, doc, errors} = await boot('#t=anoms/all', {'skopax-card-view': 'board'});
  const tiles = () => [...doc.querySelectorAll('#anoms .tile')].map((t) => t.dataset.key);

  //when
  const onBoard = tiles().includes(OLD);
  const chip = doc.querySelector('#tab-sub-anoms button[data-f="archived"]');
  const counted = chip.querySelector('.count').textContent;
  chip.click();
  const archivedView = {tiles: tiles(), hash: w.location.hash};
  w.location.hash = '#a=' + encodeURIComponent(OLD);
  await wait(200);
  const linked = doc.querySelector('#anoms .card-drawer article.inc');
  const rules = [
    w.archivedCard({status: 'cleared', sev: 'warn', clearedAt: iso(3 * 24 * 60)}),
    w.archivedCard({status: 'open', sev: 'warn', firstSeen: iso(9 * 24 * 60), triagedAt: iso(3 * 24 * 60), triage: 'progress'}),
    w.archivedCard({status: 'cleared', sev: 'info', clearedAt: iso(47 * 60)}),
    w.archivedCard({status: 'cleared', sev: 'info', clearedAt: iso(49 * 60)}),
  ];
  w.D.archiveHours = 100;
  const longer = w.archivedCard({status: 'cleared', sev: 'info', clearedAt: iso(49 * 60)});

  //then
  assert.strictEqual(onBoard, false, 'a card resolved three days ago is off the board');
  assert.strictEqual(counted, '1', 'Archived counts it');
  assert.deepStrictEqual(archivedView, {tiles: [OLD], hash: '#t=anoms/archived'}, 'the Archived list shows only archived cards, and a link keeps it');
  assert.ok(linked && linked.dataset.key === OLD, 'a card link to an archived card opens it under Archived');
  assert.deepStrictEqual(rules, [false, false, false, true], 'a waiting card nobody acted on and a still-firing card never archive; 48 h is the line');
  assert.strictEqual(longer, false, 'the hours come from the Admin setting in the state');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a card that is not firing (or is muted) and had no change for 48 h (Admin setting) leaves the board, lists and counts for Archived, which a link still opens; waiting and firing cards stay';
}

async function theListHasColumnsAndARowStillOpensForDetails() {
  //given
  const {w, doc, errors} = await boot('#t=anoms/all');

  //when
  const head = doc.querySelector('#anoms .grp .lhead');
  const cols = [...head.querySelectorAll('.c')].map((c) => c.textContent);
  const card = [...doc.querySelectorAll('#anoms article.inc')].find((c) => c.dataset.key === NEW);
  const cells = Object.fromEntries([...card.querySelectorAll('.inc-title > .c')].map((c) => [c.className.split(' ')[1], c.textContent.trim()]));
  const pressed = doc.querySelector('#card-view [data-view="list"]').getAttribute('aria-pressed');
  card.querySelector('.inc-title').click();

  //then
  assert.deepStrictEqual(cols, ['#', 'Sev', 'Card', 'Status', 'Assignee', 'Tags', 'Change', 'Age']);
  assert.strictEqual(pressed, 'true', 'List shows when it was picked');
  assert.ok(doc.getElementById('anoms').classList.contains('view-list'));
  assert.ok(cells['c-num'].startsWith('#153') && cells['c-name'] === 'checkout-service/boom' && cells['c-status'] === 'Open' && cells['c-asg'] === '@alice' && cells['c-tags'] === 'Data' && cells['c-age'] === '1 h', JSON.stringify(cells));
  assert.ok(card.classList.contains('open') && !card.querySelector('.inc-more').hidden, 'a row still opens for its details');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the List view has one header per group with #, Sev, Card, Status, Assignee, Tags, Change and Age, each card fills those columns, and a row still opens for details';
}

async function theBoardPutsCardsInStatusColumnsAndATileOpensTheCardInASidePanel() {
  //given
  const {w, doc, errors} = await boot('#t=anoms/all');

  //when
  doc.querySelector('#card-view [data-view="board"]').click();
  await wait(50);
  const cols = [...doc.querySelectorAll('#anoms .bcol')].map((c) => c.dataset.col + ' ' + c.querySelector('.count').textContent);
  const placed = [NEW, WAIT, PROG, DONE, MUTED].map((k) => colOf(doc, k));
  const saved = w.localStorage.getItem('skopax-card-view');
  const tile = [...doc.querySelectorAll('#anoms .tile')].find((t) => t.dataset.key === NEW);
  tile.click();
  await wait(50);
  const drawer = doc.querySelector('#anoms .card-drawer');
  const inDrawer = drawer && drawer.querySelector('article.inc');
  const info = {role: drawer.getAttribute('role'), title: drawer.querySelector('.cd-t').textContent, open: inDrawer.classList.contains('open'), key: inDrawer.dataset.key, focus: doc.activeElement.className};
  drawer.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  await wait(20);
  const after = {gone: !doc.querySelector('#anoms .card-drawer'), focus: doc.activeElement === [...doc.querySelectorAll('#anoms .tile')].find((t) => t.dataset.key === NEW)};
  const again = await boot('#t=anoms/all', {'skopax-card-view': 'board'});

  //then
  assert.deepStrictEqual(cols, ['open 2', 'progress 1', 'done 1', 'muted 1']);
  assert.deepStrictEqual(placed, ['open', 'open', 'progress', 'done', 'muted'], 'each card sits in the column of its status; a stopped card nobody acted on is still Open');
  assert.strictEqual(saved, 'board', 'the choice is kept in this browser');
  assert.deepStrictEqual(info, {role: 'dialog', title: 'Card #153', open: true, key: NEW, focus: 'btn sec small cd-close'}, 'a tile opens the full card, expanded, in a side panel');
  assert.deepStrictEqual(after, {gone: true, focus: true}, 'Escape closes the panel and gives the focus back to the tile');
  assert.ok(again.doc.querySelector('#anoms .board') && again.doc.querySelector('#card-view [data-view="board"]').getAttribute('aria-pressed') === 'true', 'a reload keeps the Board');
  assert.deepStrictEqual(errors.concat(again.errors), []);
  w.close(); again.w.close();
  return 'the Board puts every card in its status column (Open, In progress, Done, Muted), a tile opens the full card in a side panel that Escape closes, and the choice survives a reload';
}

async function theFiltersStillNarrowTheBoardAndACardLinkOpensItsPanel() {
  //given
  const {w, doc, errors} = await boot('#t=anoms/open', {'skopax-card-view': 'board'});

  //when
  const openOnly = [...doc.querySelectorAll('#anoms .tile')].map((t) => t.dataset.key).sort();
  const search = doc.getElementById('card-search');
  search.value = 'inventory';
  search.dispatchEvent(new w.Event('input', {bubbles: true}));
  await wait(400);
  const searched = [...doc.querySelectorAll('#anoms .tile')].map((t) => t.dataset.key);
  search.value = '';
  search.dispatchEvent(new w.Event('input', {bubbles: true}));
  await wait(400);
  w.location.hash = '#a=' + encodeURIComponent(PROG);
  await wait(200);
  const linked = doc.querySelector('#anoms .card-drawer article.inc');

  //then
  assert.deepStrictEqual(openOnly, [NEW, WAIT].sort(), 'the Open list shows only open and waiting cards on the Board too');
  assert.deepStrictEqual(searched, [PROG], 'search narrows the Board');
  assert.ok(linked && linked.dataset.key === PROG, 'a card link opens that card in the side panel');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the menu lists and search still decide which cards the Board shows, and a card link opens its side panel';
}

async function waitingAndInvestigatedAreTagsOnOpenCards() {
  //given
  const DIVED = 'log|orders-service|ERROR|dived', LOOKING = 'log|profile-service|WARN|looking';
  const more = [
    {key: DIVED, sev: 'warn', status: 'open', svc: 'orders-service', label: 'ERROR lines', src: 'logs', sig: 'log', now: 3, firstSeen: iso(80), lastSeen: iso(1), num: 160, triage: 'dived', note: {text: 'PayCo 502s, nothing to fix on our side', at: iso(10)}},
    {key: LOOKING, sev: 'warn', status: 'open', svc: 'profile-service', label: 'WARN lines', src: 'logs', sig: 'log', now: 2, firstSeen: iso(70), lastSeen: iso(2), num: 161, triage: 'investigating'},
  ];
  const {w, doc, errors} = await boot('#t=anoms/all', null, {more});
  doc.querySelector('#card-view [data-view="board"]').click();
  await wait(50);
  const tags = (key) => [...tileOf(doc, key).querySelectorAll('.tile-foot .tag')].map((x) => x.textContent);
  const order = [...colEl(doc, 'open').querySelectorAll('.tile')].map((x) => x.dataset.key);

  //then
  assert.deepStrictEqual([NEW, LOOKING, DIVED, WAIT].map((k) => colOf(doc, k)), ['open', 'open', 'open', 'open'], 'firing, investigated and stopped cards nobody acted on are all Open');
  assert.deepStrictEqual([...doc.querySelectorAll('#anoms .bcol')].map((c) => c.dataset.col), ['open', 'progress', 'done', 'muted'], 'no New or Waiting column');
  assert.strictEqual(colEl(doc, 'open').querySelector('.bcol-h').title, 'firing, or stopped with nobody acting on it yet; critical first, firing before stopped. The tags say whether it was investigated and whether it stopped');
  assert.deepStrictEqual(order, [NEW, DIVED, LOOKING, WAIT], 'critical first, then the firing ones, the stopped one last');
  assert.deepStrictEqual(tags(DIVED), ['Investigated'], 'the investigation is a tag, not a column');
  assert.deepStrictEqual(tags(LOOKING), ['Investigating']);
  assert.ok(tags(WAIT)[0].startsWith('Cleared '), 'a stopped card says when it stopped');
  assert.ok(tileOf(doc, WAIT).querySelector('.tile-foot .tag.k-crit').title.endsWith('nobody acted yet: waiting to be addressed'), 'and that it waits to be addressed');
  assert.deepStrictEqual(tags(NEW), ['Data']);
  assert.ok(colEl(doc, 'done').querySelector('.bcol-h svg.bcol-tick') && [...doc.querySelectorAll('.bcol-h svg.bcol-tick')].length === 1, 'only the Done column carries the green tick');
  assert.strictEqual(colEl(doc, 'done').querySelector('.bcol-h h3').textContent, 'Done');
  assert.strictEqual(doc.querySelector('#tab-sub-anoms button[data-f="stale"]'), null, 'no separate Waiting item in the side menu');
  assert.strictEqual(doc.querySelector('#tab-sub-anoms button[data-f="open"] .count').textContent, '4', 'Open counts them all, the investigated and the stopped card included');
  assert.strictEqual(doc.getElementById('count-anoms').textContent, '4', 'the Anomalies tab shows one Open count');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS on the board waiting and investigated cards stay Open with an Investigated, Investigating or Cleared tag; the side menu and the tab show one Open count');
}

(async () => {
  console.log('PASS ' + await theBoardIsTheDefaultAndAllTopsTheMenu());
  console.log('PASS ' + await assigneeAndTagChipsFilterTheBoardTogetherAndALinkKeepsThem());
  console.log('PASS ' + await draggingATileMovesTheCardAndOnlyMutedAsksHowLong());
  console.log('PASS ' + await guestsCannotDragAndARefusedMoveSaysWhy());
  console.log('PASS ' + await jiraCreateAndLinkShowAsTextButtonsOutsideTheFoldedActions());
  console.log('PASS ' + await severityChangesFromTheCardAndAHandSetOneSaysSo());
  console.log('PASS ' + await tilesAndRowsTitleACardByItsMessageOrItsBusinessSentence());
  console.log('PASS ' + await aCardWithNoChangeFor48HoursMovesToArchivedAndShowsNowhereElse());
  console.log('PASS ' + await theListHasColumnsAndARowStillOpensForDetails());
  console.log('PASS ' + await theBoardPutsCardsInStatusColumnsAndATileOpensTheCardInASidePanel());
  console.log('PASS ' + await theFiltersStillNarrowTheBoardAndACardLinkOpensItsPanel());
  await waitingAndInvestigatedAreTagsOnOpenCards();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
