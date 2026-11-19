const pageHtml = require('./skopax-page');
const assert = require('assert');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const CARD_PAGE = path.join(path.dirname(TEMPLATE), 'card.html');
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const NEW = 'log|checkout-service|ERROR|boom', WAIT = 'log|payments-api|ERROR|npe', MUTED = 'log|bff|ERROR|z';
const FIXED = 'log|inventory-service|ERROR|fixed', DUP = 'log|web-frontend|WARN|dup', OLD = 'log|old-service|WARN|old', AUTO = 'log|auto-service|INFO|auto', SEEN = 'log|seen-service|ERROR|seen';
const AT = '2026-11-09T16:32:00Z', STOPPED = '2026-11-09T18:10:00Z';
const {short} = require('./skopax-zone');
const NINA = 'alice@example.com', PAUL = 'carol@example.com';

function state() {
  const card = (key, extra) => Object.assign({key, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'ERROR lines', src: 'logs', sig: 'log', now: 1, firstSeen: iso(90), lastSeen: iso(1)}, extra || {});
  const done = {triage: 'handled', status: 'cleared', clearedAt: iso(30), triagedAt: iso(20)};
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(NEW, {num: 153}), card(WAIT, {status: 'cleared', clearedAt: iso(20), num: 150}), card(MUTED, {muted: {until: iso(-600)}, num: 141}),
      card(FIXED, Object.assign({num: 160, ticket: 'OPS-140', resolution: {kind: 'fixed', ref: 'OPS-140', by: NINA, via: 'slack', at: iso(20)}}, done)),
      card(DUP, Object.assign({num: 161, resolution: {kind: 'duplicate', ref: '#12', by: PAUL, via: 'chat', at: iso(20)}}, done)),
      card(OLD, Object.assign({num: 162}, done)),
      card(AUTO, Object.assign({num: 163, sev: 'info', triagedBy: 'auto', autoResolvedAt: iso(20), resolution: {kind: 'handled', by: 'auto', via: 'system', at: iso(20)}}, done)),
      card(SEEN, {num: 164, seenBefore: {count: 2, lastAt: iso(3000), outcome: 'fixed', ticket: 'OPS-120', resolution: {kind: 'fixed', ref: 'OPS-120', by: NINA, via: 'page', at: iso(3000)}}})],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

const CLOSED_ROWS = [
  {key: 'log|a|ERROR|a', num: 201, svc: 'a', label: 'ERROR pattern', sev: 'warn', firstSeen: iso(3000), lastSeen: iso(2000), outcome: 'fixed', ticket: 'OPS-140',
    resolution: {kind: 'fixed', ref: 'OPS-140', by: NINA, via: 'page', at: iso(2000)}},
  {key: 'log|b|ERROR|b', num: 202, svc: 'b', label: 'ERROR pattern', sev: 'warn', firstSeen: iso(3000), lastSeen: iso(2001), outcome: 'duplicate', resolution: {kind: 'duplicate', ref: '#12', by: 'pod', via: 'system', at: iso(2001)}},
  {key: 'log|c|ERROR|c', num: 203, svc: 'c', label: 'ERROR pattern', sev: 'warn', firstSeen: iso(3000), lastSeen: iso(2002), outcome: 'handled'},
];

const statusOf = (doc) => [doc.getElementById('state'), doc.querySelector('.status')].filter(Boolean).map((e) => e.textContent).join(' · ');
async function boot(hash, opts) {
  const errors = [], posted = [], asked = [];
  const o = opts || {};
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE, {view: o.view || 'list'}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state())), {
    url: 'https://skopax.example.com/' + (hash || '#t=anoms/all'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      if (o.lang) w.document.cookie = 'skopax_lang=' + o.lang + '; path=/';
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p, init) => {
        const url = String(p);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (url.startsWith('/api/actions') && init && init.method === 'POST') { const b = JSON.parse(init.body); posted.push(b); return ok({id: 'x' + posted.length, type: b.type, status: 'done', result: 'ok'}); }
        if (url.startsWith('/api/history/cards')) { asked.push(Object.fromEntries(new URL('https://x' + url).searchParams.entries())); return ok({from: iso(10080), to: iso(0), page: 0, size: 50, total: 3, items: CLOSED_ROWS, outcomes: {}}); }
        if (url.startsWith('/api/me')) return ok({loginEnabled: true, user: NINA, operator: true, teams: [], tags: [], jiraUrl: 'https://jira.example.com', task: {projects: ['OPS']}});
        if (url.startsWith('/api/actions') || url.startsWith('/api/deploy-reports') || url.startsWith('/api/requests')) return ok([]);
        if (url.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(450);
  return {w: dom.window, doc: dom.window.document, errors, posted, asked};
}

const article = (doc, key) => [...doc.querySelectorAll('#anoms article.inc')].find((c) => c.dataset.key === key);
const iconOf = (card, t) => [...card.querySelectorAll('.acts button.icon')].find((b) => b.textContent === t);
const chips = (box) => [...box.querySelectorAll('.res-kind')].map((b) => b.dataset.kind + ':' + b.getAttribute('aria-pressed'));
const tileOf = (doc, key) => [...doc.querySelectorAll('#anoms .tile')].find((x) => x.dataset.key === key);
const colEl = (doc, col) => doc.querySelector('#anoms .bcol[data-col="' + col + '"]');
function drag(w, doc, key, col) {
  tileOf(doc, key).dispatchEvent(new w.Event('dragstart', {bubbles: true}));
  colEl(doc, col).dispatchEvent(new w.Event('dragover', {bubbles: true, cancelable: true}));
  colEl(doc, col).dispatchEvent(new w.Event('drop', {bubbles: true, cancelable: true}));
}

async function theResolveControlOffersThreeKindsAndSendsTheOnePicked() {
  //given
  const {w, doc, errors, posted} = await boot('#t=anoms/all');
  const card = article(doc, NEW), other = article(doc, WAIT);

  //when
  iconOf(card, 'Resolve').click();
  const form = card.querySelector('.res-form');
  const first = {chips: chips(form), why: !form.querySelector('input[id^="why-"]').hidden, dup: !form.querySelector('input[id^="dup-"]').hidden, open: !form.hidden};
  form.querySelector('.res-kind[data-kind="fixed"]').click();
  form.querySelector('input[id^="why-"]').value = 'pool raised in OPS-140';
  [...form.querySelectorAll('button')].find((b) => b.textContent === 'Resolve').click();
  await wait(80);
  const fixed = posted.splice(0);
  form.querySelector('.res-kind[data-kind="duplicate"]').click();
  const dupShown = {chips: chips(form), why: !form.querySelector('input[id^="why-"]').hidden, dup: !form.querySelector('input[id^="dup-"]').hidden};
  [...form.querySelectorAll('button')].find((b) => b.textContent === 'Resolve').click();
  await wait(50);
  const empty = {sent: posted.length, invalid: form.querySelector('input[id^="dup-"]').getAttribute('aria-invalid')};
  form.querySelector('input[id^="dup-"]').value = ' 12 ';
  [...form.querySelectorAll('button')].find((b) => b.textContent === 'Resolve').click();
  await wait(80);
  const dup = posted.splice(0);
  iconOf(other, 'Resolve').click();
  other.querySelector('.res-form .res-kind[data-kind="handled"]').click();
  [...other.querySelectorAll('.res-form button')].find((b) => b.textContent === 'Resolve').click();
  await wait(80);
  const plain = posted.splice(0);

  //then
  assert.deepStrictEqual(first, {chips: ['fixed:true', 'handled:false', 'duplicate:false'], why: true, dup: false, open: true}, 'Fixed is picked first, with the why field');
  assert.ok(form.querySelector('.res-kind').classList.contains('sev-pick'), 'the chips look like the severity chips');
  assert.deepStrictEqual(fixed, [{type: 'resolve', target: NEW, key: NEW, kind: 'fixed', reason: 'pool raised in OPS-140', label: 'Resolve checkout-service ERROR lines (fixed)'}]);
  assert.deepStrictEqual(dupShown, {chips: ['fixed:false', 'handled:false', 'duplicate:true'], why: false, dup: true}, 'Duplicate swaps the why for the other card');
  assert.deepStrictEqual(empty, {sent: 0, invalid: 'true'}, 'a duplicate needs the other card before it goes');
  assert.deepStrictEqual(dup, [{type: 'resolve', target: NEW, key: NEW, kind: 'duplicate', ref: '12', label: 'Resolve checkout-service ERROR lines (duplicate of 12)'}]);
  assert.deepStrictEqual(plain, [{type: 'resolve', target: WAIT, key: WAIT, label: 'Resolve payments-api ERROR lines (no action needed)'}], 'No action needed sends no kind, so older servers and checks see the same body');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS Resolve offers Fixed (picked first), No action needed and Duplicate as chips; Fixed and No action needed keep the why, Duplicate needs the other card');
}

async function draggingToDoneAsksHowAndUnmutesFirst() {
  //given
  const {w, doc, errors, posted} = await boot('#t=anoms/all', {view: 'board'});

  //when
  drag(w, doc, WAIT, 'done');
  const dialog = doc.querySelector('.resolve-pick');
  const offered = chips(dialog);
  dialog.querySelector('.res-kind[data-kind="duplicate"]').click();
  dialog.querySelector('.rp-yes').click();
  const stillOpen = !!doc.querySelector('.resolve-pick') && dialog.querySelector('.rp-dup').getAttribute('aria-invalid');
  dialog.querySelector('.rp-dup').value = '#140';
  dialog.querySelector('.rp-yes').click();
  await wait(80);
  const dup = posted.splice(0);
  drag(w, doc, MUTED, 'done');
  doc.querySelector('.resolve-pick .res-kind[data-kind="fixed"]').click();
  doc.querySelector('.resolve-pick .rp-why').value = 'rolled back';
  doc.querySelector('.resolve-pick .rp-yes').click();
  await wait(80);
  const fixed = posted.splice(0);
  drag(w, doc, NEW, 'done');
  doc.querySelector('.resolve-pick .rp-no').click();
  await wait(30);

  //then
  assert.deepStrictEqual(offered, ['fixed:true', 'handled:false', 'duplicate:false']);
  assert.strictEqual(stillOpen, 'true', 'the dialog stays until the other card is named');
  assert.deepStrictEqual(dup, [{target: WAIT, key: WAIT, type: 'resolve', kind: 'duplicate', ref: '#140', label: 'Resolve payments-api ERROR lines (duplicate of #140)'}]);
  assert.deepStrictEqual(fixed.map((b) => [b.type, b.kind, b.reason]), [['unmute', undefined, undefined], ['resolve', 'fixed', 'rolled back']], 'a muted card is unmuted first, as before');
  assert.ok(!doc.querySelector('.resolve-pick') && posted.length === 0, 'Cancel moves nothing');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS dragging a card to Done asks Fixed, No action needed or Duplicate in a dialog like the Muted one; a muted card is unmuted first and Cancel moves nothing');
}

async function aResolvedCardSaysHowViaAndWhoAndAnOldOneReadsResolved() {
  //given
  const en = await boot('#t=anoms/all');
  const tag = (doc, key) => article(doc, key).querySelector('.res-tag');
  const seen = (doc) => { const c = article(doc, SEEN); c.querySelector('.inc-title .fold').click(); return c.querySelector('.seen-line'); };

  //when
  const english = [FIXED, DUP, OLD].map((k) => tag(en.doc, k).textContent);
  const auto = [...article(en.doc, AUTO).querySelectorAll('.tag')].map((t) => t.textContent).find((t) => t.startsWith('No action needed'));
  const line = seen(en.doc);

  //then
  assert.deepStrictEqual(english, ['Fixed (OPS-140 \u2197) \u00b7 via Slack \u00b7 alice', 'Duplicate of #12 · via chat · carol', 'Resolved']);
  assert.strictEqual(tag(en.doc, DUP).querySelector('a').getAttribute('href'), '/c/12', 'the other card links to its page');
  assert.strictEqual(auto, 'No action needed · auto-resolved', 'auto-resolved keeps its own tag');
  assert.ok(/^Seen before: 2 times, last .+, resolved as Fixed \(OPS-120 \u2197\) · via page · alice$/.test(line.textContent), line.textContent);
  assert.deepStrictEqual(en.errors, []);
  en.w.close();
  console.log('PASS a resolved card shows Fixed, Duplicate of #n or No action needed with via and who, in English; an old one reads Resolved and auto-resolved keeps its tag');
}

async function theClosedViewFiltersByTheNewKindsAndSaysHowEachRowEnded() {
  //given
  const {w, doc, errors, asked} = await boot('#t=anoms/closed');

  //when
  const outcomes = [...doc.querySelectorAll('.cl-out button[data-outcome]')].map((b) => b.dataset.outcome + ':' + b.textContent);
  const rows = [...doc.querySelectorAll('#cl-body .cl-row .cl-res')].map((r) => r.textContent);
  doc.querySelector('.cl-out button[data-outcome="duplicate"]').click();
  await wait(50);

  //then
  assert.deepStrictEqual(outcomes.slice(0, 5), [':Every outcome', 'fixed:Fixed', 'handled:No action needed', 'duplicate:Duplicate', 'auto-resolved:Auto-resolved']);
  assert.deepStrictEqual(rows, ['Fixed (OPS-140 \u2197) · via page · alice', 'Duplicate of #12 · via Skopax · investigator', 'Resolved'], 'the ticket is not repeated when it is the fix');
  assert.strictEqual(asked[asked.length - 1].outcome, 'duplicate');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the Closed view has Fixed and Duplicate chips and each row says how it ended, via and who');
}

async function cardPage(num, card, who) {
  const errors = [], posts = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(CARD_PAGE), {
    url: 'https://skopax.example.com/c/' + num, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.fetch = async (p, opts) => {
        if (String(p) === '/api/me') return {ok: true, status: 200, json: async () => Object.assign({loginEnabled: true, user: null, operator: false}, who || {})};
        if (opts && opts.method === 'POST') { posts.push(JSON.parse(opts.body)); return {ok: true, status: 201, json: async () => ({})}; }
        return {ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(card))};
      };
    },
  });
  await wait(100);
  return {w: dom.window, doc: dom.window.document, errors, posts};
}

async function theCardPageKeepsItsDomWhileTheCardIsUnchanged() {
  //given
  const data = {num: 153, key: NEW, svc: 'checkout-service', label: 'ERROR lines', sev: 'warn', status: 'open', firstSeen: AT, live: true};
  const t = await cardPage(153, data, {user: NINA, operator: true});
  const first = t.doc.querySelector('h1');

  //when
  t.doc.getElementById('upd').textContent = '';
  await t.w.__cardLoad();
  const same = t.doc.querySelector('h1') === first;
  const stamped = /^Updated /.test(t.doc.getElementById('upd').textContent);
  data.label = 'WARN lines';
  await t.w.__cardLoad();

  //then
  assert.strictEqual(same, true, 'a poll with the same card redraws nothing');
  assert.ok(stamped, 'but the Updated time moves');
  assert.notStrictEqual(t.doc.querySelector('h1'), first, 'a changed card is drawn again');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS the card page redraws only when the card changed');
}

async function theCardPageSaysHowAndAsksTheKind() {
  //given
  const live = await cardPage(160, {num: 160, key: FIXED, svc: 'inventory-service', label: 'ERROR lines', sev: 'warn', status: 'cleared', clearedAt: '2026-11-09T18:10:00Z', firstSeen: AT, live: true,
    triage: 'handled', resolution: {kind: 'fixed', ref: 'OPS-140', by: NINA, via: 'page', at: AT}});
  const gone = await cardPage(161, {num: 161, key: DUP, svc: 'web-frontend', label: 'WARN lines', sev: 'warn', live: false, lastSeen: AT, outcome: 'duplicate',
    resolution: {kind: 'duplicate', ref: '#12', by: 'pod', via: 'system', at: AT}});
  const old = await cardPage(162, {num: 162, key: OLD, svc: 'old-service', label: 'WARN lines', sev: 'warn', status: 'cleared', clearedAt: '2026-11-09T18:10:00Z', firstSeen: AT, live: true, triage: 'handled'});
  const op = await cardPage(153, {num: 153, key: NEW, svc: 'checkout-service', label: 'ERROR lines', sev: 'warn', status: 'open', firstSeen: AT, live: true, last: null}, {user: NINA, operator: true});
  const d = op.doc;

  //when
  d.getElementById('act-resolve').click();
  const first = [...d.querySelectorAll('#form-resolve .res-kind')].map((b) => b.dataset.kind + ':' + b.getAttribute('aria-pressed'));
  const cardForm = [d.getElementById('resolve-hint').textContent, d.getElementById('resolve-why').placeholder, d.getElementById('resolve-dup').placeholder, d.getElementById('resolve-why').title.length > 20];
  d.getElementById('resolve-duplicate').click();
  const dupHint = d.getElementById('resolve-hint').textContent;
  const swapped = [d.getElementById('resolve-why').hidden, d.getElementById('resolve-dup').hidden];
  d.getElementById('resolve-go').click();
  await wait(30);
  const empty = [op.posts.length, d.getElementById('resolve-dup').getAttribute('aria-invalid')];
  d.getElementById('resolve-dup').value = '12';
  d.getElementById('resolve-go').click();
  await wait(50);

  //then
  assert.strictEqual(statusOf(live.doc), 'Fixed (OPS-140) \u00b7 from the page \u00b7 alice \u00b7 fixed ' + short(STOPPED));
  assert.strictEqual(statusOf(gone.doc), 'Duplicate: #12 \u00b7 by Skopax \u00b7 investigator \u00b7 last seen ' + short(AT));
  assert.strictEqual(statusOf(old.doc), 'Resolved \u00b7 fixed ' + short(STOPPED), 'an old resolve without a kind reads Resolved');
  assert.deepStrictEqual(first, ['fixed:true', 'handled:false', 'duplicate:false']);
  assert.deepStrictEqual(swapped, [true, false]);
  assert.deepStrictEqual(cardForm, ['The cause is gone because something was changed, such as a deploy, a config change or a rollback.', 'Note (optional)', 'Card number or key', true], 'the card page form reads like the menu form');
  assert.strictEqual(dupHint, 'The same problem as another card, which carries it on.');
  assert.deepStrictEqual(empty, [0, 'true']);
  assert.deepStrictEqual(op.posts, [{type: 'resolve', kind: 'duplicate', ref: '12', label: 'Resolve checkout-service / ERROR lines (duplicate of 12)', key: NEW, target: NEW}]);
  [live, gone, old, op].forEach((x) => { assert.deepStrictEqual(x.errors, []); x.w.close(); });
  console.log('PASS the card page says Fixed, Duplicate or No action needed with where and who, an old resolve reads Resolved, and its Resolve asks the kind');
}

function solid(doc) {
  const colours = {'--btn-bg': '#111111', '--btn-fg': '#eeeeee', '--surface': '#ffffff', '--line-2': '#cccccc', '--fg': '#222222', '--raised': '#f4f4f4'};
  doc.querySelectorAll('style').forEach((st) => { st.textContent = st.textContent.replace(/var\((--[a-z0-9-]+)\)/g, (m, name) => colours[name] || m); });
}

async function thePickedOutcomeLooksPickedAndTheFormIsSegmentedHintedAndFullWidth() {
  //given
  const {w, doc, errors, posted} = await boot('#t=anoms/all');
  const card = article(doc, NEW);
  const bg = (el) => w.getComputedStyle(el).backgroundColor;
  const css = require('fs').readFileSync(TEMPLATE, 'utf8');
  solid(doc);

  //when
  iconOf(card, 'Resolve').click();
  const form = card.querySelector('.res-form');
  const fixedBtn = form.querySelector('.res-kind[data-kind="fixed"]'), handled = form.querySelector('.res-kind[data-kind="handled"]'), dupBtn = form.querySelector('.res-kind[data-kind="duplicate"]');
  const look = [bg(fixedBtn) !== bg(handled), bg(fixedBtn) === bg(dupBtn) ? 'same' : 'differs'];
  const hintFixed = form.querySelector('.res-hint').textContent;
  dupBtn.click();
  const afterDup = [bg(dupBtn) !== bg(fixedBtn), form.querySelector('.res-hint').textContent, form.querySelector('input[id^="dup-"]').placeholder, form.querySelector('input[id^="dup-"]').hidden, form.querySelector('input[id^="why-"]').hidden];
  const why = form.querySelector('input[id^="why-"]');
  form.querySelector('.res-kind[data-kind="handled"]').click();
  const bodies = {};
  why.value = 'harmless';
  [...form.querySelectorAll('button')].find((b) => b.textContent === 'Resolve').click();
  await wait(60);
  bodies.handled = posted.splice(0);

  //then
  assert.deepStrictEqual(look, [true, 'differs'], 'the picked outcome is drawn differently from the others');
  assert.ok(/^The cause is gone/.test(hintFixed), hintFixed);
  assert.deepStrictEqual(afterDup, [true, 'The same problem as another card, which carries it on.', 'Card number or key', false, true], 'Duplicate shows the card field and hides the note');
  assert.strictEqual(why.placeholder, 'Note (optional)');
  assert.ok(why.title.length > why.placeholder.length, 'the longer hint is the title');
  assert.deepStrictEqual(bodies.handled.map((b) => [b.kind, b.reason]), [[undefined, 'harmless']], 'No action needed sends the note and no kind');
  assert.ok(/\.res-kinds \{[^}]*grid-template-columns: repeat\(3, minmax\(0, 1fr\)\)/.test(css), 'three equal parts');
  assert.ok(/\.res-form > input, \.res-form > \.btn \{[^}]*width: 100%/.test(css), 'the note and the submit are full width');
  assert.ok(/\.btn\.sev-pick\[aria-pressed="true"\]/.test(css), 'the pressed style outranks .btn.sec');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the Resolve form draws the picked outcome, shows a hint, swaps note and card field, and sends each outcome');
}

async function theResolveDialogDrawsThePickedOutcomeToo() {
  //given
  const {w, doc, errors} = await boot('#t=anoms/all', {view: 'board'});
  drag(w, doc, WAIT, 'done');
  const dialog = doc.querySelector('.resolve-pick');
  solid(doc);
  const bg = (el) => w.getComputedStyle(el).backgroundColor;

  //when
  const a = dialog.querySelector('.res-kind[data-kind="fixed"]'), b = dialog.querySelector('.res-kind[data-kind="handled"]');
  const before = bg(a) !== bg(b);
  b.click();
  const after = [bg(b) !== bg(a), dialog.querySelector('.res-hint').textContent, dialog.querySelector('.rp-why').placeholder];

  //then
  assert.strictEqual(before, true);
  assert.ok(!a.title && !b.title, 'no tooltip repeats the hint line under the choices');
  assert.deepStrictEqual(after, [true, 'Harmless or a one-off, nothing was changed.', 'Note (optional)']);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the Resolve dialog draws the picked outcome and shows its hint');
}

(async () => {
  await thePickedOutcomeLooksPickedAndTheFormIsSegmentedHintedAndFullWidth();
  await theResolveDialogDrawsThePickedOutcomeToo();
  await theResolveControlOffersThreeKindsAndSendsTheOnePicked();
  await draggingToDoneAsksHowAndUnmutesFirst();
  await aResolvedCardSaysHowViaAndWhoAndAnOldOneReadsResolved();
  await theClosedViewFiltersByTheNewKindsAndSaysHowEachRowEnded();
  await theCardPageKeepsItsDomWhileTheCardIsUnchanged();
  await theCardPageSaysHowAndAsksTheKind();
})().then(() => process.exit(0), (e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
