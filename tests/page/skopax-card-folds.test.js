const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString().replace(/\.\d{3}Z$/, 'Z');
const MAIN = 'log|checkout-service|ERROR|boom', STOP = 'log|search-api|ERROR|slow', TAGGY = 'log|orders-api|ERROR|stuck';
const SAID = '\ud83d\udfe1 Stopped firing: *checkout-service / 5xx responses* \u00b7 calm since 02:17, it lasted 1 h 15 min. Resolve it if nothing is needed.';
const TEAMS = [['team-a', 'Shop team'], ['team-b', 'Payments team'], ['team-c', 'Search team'], ['team-d', 'A team with a very long name that does not fit on one line']];

function card(key, num, extra) {
  return Object.assign({key, num, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'ERROR lines', src: 'logs', sig: 'log', now: 5, d1: 1, d7: 2, firstSeen: iso(30), lastSeen: iso(1), what: 'text'}, extra || {});
}
function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(MAIN, 11, {followUp: {doneWhen: 'errors stay at zero', checkedAt: iso(10), lastResult: 'not_done'}, note: {text: 'the 10:02 deploy broke the payment client', at: iso(8)},
      business: {text: 'Some shoppers cannot pay.', kind: 'action', by: 'rule', at: iso(20)}, tags: [{id: 'team-a', by: 'rule', at: iso(20)}],
      clearNote: {text: 'calm since the rollback', by: 'pod', at: iso(4)}, slackDraft: {text: 'Checkout errors are up', by: 'pod', at: iso(5)}}),
      card(STOP, 12, {status: 'cleared', clearedAt: iso(20), owner: 'bob.smith@example.com', assigned: {name: 'Bob Smith', by: 'alice@example.com', at: iso(25)}}),
      card(TAGGY, 13, {gone: {status: 404, since: iso(10)}, triage: 'dived', triagedBy: 'pod', recurring: true, recurringCount: 3, tags: TEAMS.map(([id]) => ({id, by: 'rule', at: iso(20)}))})],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(opts) {
  opts = opts || {};
  const errors = [];
  const vc = new VirtualConsole(); vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const kept = Object.assign({}, opts.initial || {});
  const dom = new JSDOM(pageHtml(TEMPLATE, {view: 'board'}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state())), {
    url: 'https://skopax.example.com/#t=anoms/all', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {}; w.Element.prototype.scrollIntoView = function () {};
      Object.defineProperty(w, 'localStorage', {value: {getItem: (k) => (k in kept ? kept[k] : null), setItem: (k, v) => { kept[k] = String(v); }, removeItem: (k) => { delete kept[k]; }}, configurable: true});
      w.fetch = async (p, init) => {
        p = String(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (init && init.method === 'POST') return ok({id: 'x', status: 'requested'});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'op@example.com', operator: true, teams: [], routes: [], tags: TEAMS.map(([id, label]) => ({id, label}))});
        if (p.startsWith('/api/actions')) return ok(opts.actions || []);
        if (p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/state')) return ok(state());
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors, kept};
}
const tileOf = (doc, key) => [...doc.querySelectorAll('#anoms .tile')].find((t) => t.dataset.key === key);
async function openCard(doc, key) {
  if (doc.querySelector('.cd-close')) doc.querySelector('.cd-close').click();
  tileOf(doc, key).click();
  await wait(60);
  return doc.querySelector('.card-drawer article.inc');
}
const toggle = (w, d, on) => { d.open = on; d.dispatchEvent(new w.Event('toggle')); };

async function theLastActionIsOneShortLine() {
  //given
  const act = {id: 'a1', type: 'clear-note', target: MAIN, key: MAIN, status: 'done', requestedAt: iso(5), doneAt: iso(4), requestedBy: 'server', result: SAID};
  const {w, doc, errors, kept} = await boot({actions: [act]});
  const art = await openCard(doc, MAIN);
  await w.__skopaxActions();
  await wait(60);

  //then
  const fold = art.querySelector('.last-act details.la-fold');
  assert.ok(fold, 'the last action is a fold');
  assert.strictEqual(fold.open, false, 'closed by default');
  assert.strictEqual(fold.querySelector('summary').textContent, 'Done ' + w.dayhm(act.doneAt) + ' \u00b7 Stopped firing');
  assert.ok(fold.querySelector('summary').classList.contains('chev'), 'the summary shows a chevron');
  assert.strictEqual(fold.querySelector('.la-full').textContent, 'by server \u00b7 ' + SAID, 'who did it and the full text stay in the fold, with no repeated badge');

  //when
  toggle(w, fold, true);
  const again = await openCard(doc, MAIN);
  await w.__skopaxActions();
  await wait(60);

  //then
  assert.strictEqual(kept['skopax.fold.last'], '1');
  assert.strictEqual(again.querySelector('.last-act details.la-fold').open, true, 'the choice is remembered per browser');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the last action reads "Done hh:mm \u00b7 stopped firing" with the full text in a fold, closed by default';
}

async function theGistIsShortAndPlain() {
  //given
  const {w, errors} = await boot();

  //then
  const a = {svc: 'checkout-service', key: MAIN};
  assert.strictEqual(w.lastGist(SAID, a), 'Stopped firing');
  assert.strictEqual(w.lastGist('Same PayCo outage: 27 ERRORs between 12:10 and 12:20', a), 'Same PayCo outage');
  assert.strictEqual(w.lastGist('Shoppers cannot pay', {svc: 'shop', key: 'log|shop|ERROR|x'}), 'Shoppers cannot pay', 'the service name goes only as a whole word');
  assert.strictEqual(w.lastGist('3,400 errors since 10:02', a), '3,400 errors since 10:02', 'numbers and times are not split');
  assert.strictEqual(w.lastGist('calm since 02:17', a), 'calm since 02:17');
  assert.strictEqual(w.lastGist('PayCo is down again', a), 'PayCo is down again', 'the first letter keeps its case');
  assert.strictEqual(w.lastGist('muted until later', a), 'muted until later');
  assert.strictEqual(w.lastGist('the checkout-service pods ran out of memory again after the 10:02 deploy and restarted', a), 'the pods ran out of memory\u2026');
  assert.strictEqual(w.lastGist('', a), '');
  assert.strictEqual(w.lastGist('Slack said channel_not_found for _this_ post', a), 'Slack said channel_not_found for this post');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the gist drops emoji, markdown and the service name and keeps at most six words';
}

async function theStatusLineIsStateAndOwner() {
  //given
  const {w, doc, errors} = await boot();

  //when
  const stop = await openCard(doc, STOP);
  const line = stop.querySelector('.card-state');

  //then
  assert.strictEqual(line.textContent, 'Stopped firing \u00b7 Bob Smith');
  assert.ok(/waiting to be addressed/.test(line.title) && /stopped/.test(line.title), 'the times and the waiting note sit in the tooltip: ' + line.title);

  //when
  const open = await openCard(doc, MAIN);

  //then
  assert.strictEqual(open.querySelector('.card-state').textContent, 'Open');
  assert.ok(/first seen/.test(open.querySelector('.card-state').title));
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the status line reads the state and the owner; times sit in its tooltip';
}

async function longTagRowsShowThreeAndAPlusChip() {
  //given
  const {w, doc, errors} = await boot();
  const art = await openCard(doc, TAGGY);
  const row = art.querySelector('.tech-tags');
  const warn = art.querySelector('.inc-tags .page-gone');
  assert.ok(warn && !art.querySelector('.tech-tags .page-gone, .tech-tags .k-warn:not(.recurring)'), 'a warning tag stays in view, not in Technical details');
  const visible = () => [...row.querySelectorAll('.tag')].filter((t) => !t.closest('[hidden]') && !t.classList.contains('tag-more'));

  //then
  assert.strictEqual(visible().length, 3, 'three chips show');
  const more = row.querySelector('.tag-more');
  assert.deepStrictEqual([more.textContent, more.getAttribute('aria-expanded'), more.tagName], ['+3', 'false', 'BUTTON']);
  assert.ok([...row.querySelectorAll('.tag:not(.tag-more)')].every((t) => t.title), 'every chip carries its full text in the title');

  //when
  more.click();

  //then
  assert.strictEqual(visible().length, 6, 'all chips show in place');
  assert.ok(!row.querySelector('.tag-more') || row.querySelector('.tag-more').hidden, 'the +N chip goes');
  const css = fs.readFileSync(TEMPLATE, 'utf8');
  const rule = [...css.matchAll(/(?:^|\n)\.inc-tags \.tag, \.tech-tags \.tag \{([^}]*)\}/g)].map((m) => m[1]).join(' ');
  assert.match(rule, /white-space:\s*nowrap/);
  assert.match(rule, /text-overflow:\s*ellipsis/);
  assert.match(rule, /max-width:/);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a long tag row shows three chips and a +N chip that opens the rest in place; chips stay on one line';
}

async function bodyBlocksStartFoldedAndRemember() {
  //given
  const {w, doc, errors, kept} = await boot();
  const art = await openCard(doc, MAIN);
  const block = (sel) => art.querySelector(sel);

  //then
  for (const [sel, name] of [['details.follow-up', 'follow'], ['details.dive', 'dive'], ['details.card-runs', 'runs'], ['details.biz-note', 'biz'], ['details.clear-note', 'clear'], ['details.sdraft', 'draft']]) {
    const d = block(sel);
    assert.ok(d, sel + ' is a fold');
    assert.strictEqual(d.open, false, sel + ' starts folded');
    assert.strictEqual(d.dataset.fold, name);
    assert.ok(d.querySelector(':scope > summary').textContent.trim(), sel + ' keeps its first line');
  }
  assert.ok(!block('.card-what').closest('details') && !block('.card-todo').closest('details'), 'What happened and What to do stay open');
  assert.ok(/errors stay at zero/.test(block('details.follow-up').textContent) && /payment client/.test(block('details.dive').textContent), 'nothing is removed, only folded');

  //when
  toggle(w, block('details.follow-up'), true);
  const again = await openCard(doc, MAIN);

  //then
  assert.strictEqual(kept['skopax.fold.follow'], '1');
  assert.strictEqual(again.querySelector('details.follow-up').open, true, 'remembered per browser');
  assert.strictEqual(again.querySelector('details.dive').open, false);

  //when
  again.querySelector('.biz-edit').click();

  //then
  assert.ok(again.querySelector('details.biz-note').open && !again.querySelector('.biz-form').hidden, 'editing the note opens its fold');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'follow-up, investigation, runs and the business note start folded to their first line and remember the choice';
}

async function historyEntriesShowTheirFirstLine() {
  //given
  const {w, doc, errors, kept} = await boot();
  const box = doc.createElement('div');
  doc.body.appendChild(box);
  const rows = [{at: iso(5), label: 'Note', data: 'a long note '.repeat(12), who: 'alice', kind: 'row'}];

  //when
  box.innerHTML = w.cardHistoryHtml(rows, w.dayhm, '');
  const list = box.querySelector('.h-rows'), tog = box.querySelector('.h-tog');

  //then
  assert.ok(list.classList.contains('h-clamp'), 'entries start on their first line');
  w.histOverflow(box);
  assert.strictEqual(tog.hidden, true, 'no toggle while no row is cut');
  Object.defineProperty(box.querySelector('.h-body'), 'scrollHeight', {value: 60, configurable: true});
  w.histOverflow(box);
  assert.deepStrictEqual([tog.hidden, tog.textContent, tog.getAttribute('aria-expanded')], [false, 'Show more', 'false'], 'the toggle shows once a row is cut');

  //when
  tog.click();

  //then
  assert.deepStrictEqual([list.classList.contains('h-clamp'), tog.textContent, tog.getAttribute('aria-expanded'), kept['skopax.fold.hist']], [false, 'Show less', 'true', '1']);
  box.innerHTML = w.cardHistoryHtml(rows, w.dayhm, '');
  assert.ok(!box.querySelector('.h-rows').classList.contains('h-clamp'), 'the choice is remembered');
  const css = fs.readFileSync(TEMPLATE, 'utf8');
  assert.match(css, /\.h-rows\.h-clamp \.h-body \{[^}]*-webkit-line-clamp:\s*1/);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'history entries start on their first line with a Show more toggle that is remembered';
}

async function theStatusTooltipIsTranslated() {
  //given
  const polish = require('./skopax-card-polish.test.js');
  const k = 'log|search-api|ERROR|slow';
  const {w, doc, errors} = await polish.boot([polish.card(k, {num: 41, status: 'cleared', clearedAt: polish.iso(20), owner: 'bob.smith@example.com', assigned: {name: 'Bob Smith', by: 'alice@example.com'}})], {lang: 'tr'});

  //when
  const art = polish.article(doc, k);
  polish.expand(art);
  await polish.wait(30);
  const line = art.querySelector('.card-state');

  //then
  assert.strictEqual(line.textContent, 'Sinyal durdu \u00b7 Bob Smith');
  assert.ok(/ilgilenilmeyi bekliyor/.test(line.title) && !/waiting to be addressed|stopped/.test(line.title), 'the tooltip is translated too: ' + line.title);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the short status line and its tooltip follow the page language';
}

(async () => {
  for (const t of [theLastActionIsOneShortLine, theGistIsShortAndPlain, theStatusLineIsStateAndOwner, longTagRowsShowThreeAndAPlusChip, bodyBlocksStartFoldedAndRemember, historyEntriesShowTheirFirstLine, theStatusTooltipIsTranslated]) {
    try { console.log('PASS ' + await t()); } catch (e) { console.log('FAIL ' + t.name + ': ' + (e && e.stack || e)); process.exitCode = 1; }
  }
  process.exit(process.exitCode || 0);
})();
