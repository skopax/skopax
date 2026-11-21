const pageHtml = require('./skopax-page');
const assert = require('assert');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const CARD_PAGE = path.join(path.dirname(TEMPLATE), 'card.html');
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const FIRST = iso(90);
const WHAT = 'the 10:00 push campaign send';
const EVENT = 'log|push-service|ERROR|send', PLAIN = 'log|payments-api|ERROR|npe', KID = 'log|push-service|WARN|kid', OLDCAUSE = 'log|mail-service|ERROR|old', CALM = 'log|feed-service|WARN|calm', RECUR = 'log|image-service|ERROR|leak';

function state() {
  const card = (key, extra) => Object.assign({key, sev: 'warn', status: 'cleared', clearedAt: iso(20), svc: key.split('|')[1], label: 'ERROR lines', src: 'logs', sig: 'log', now: 1, firstSeen: FIRST, lastSeen: iso(21)}, extra || {});
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(EVENT, {num: 150, cause: {kind: 'event', what: WHAT, firstSeen: FIRST}}), card(PLAIN, {num: 151}),
      card(KID, {num: 152, parent: EVENT, firstSeen: iso(80)}), card(OLDCAUSE, {num: 153, cause: {kind: 'event', what: 'an old deploy', firstSeen: iso(9000)}}),
      card(CALM, {num: 154, sev: 'info', cause: {kind: 'event', what: '<b>batch</b> job', firstSeen: FIRST}}),
      card(RECUR, {num: 155, cause: {kind: 'recurring', what: 'a leak in the image cache', firstSeen: FIRST}})],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(view, hash) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE, {view}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state())), {
    url: 'https://skopax.example.com/' + (hash || '#t=anoms/all'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (String(p).startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', operator: true, teams: [], tags: []});
        if (String(p).startsWith('/api/actions') || String(p).startsWith('/api/deploy-reports') || String(p).startsWith('/api/requests')) return ok([]);
        if (String(p).startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(450);
  return {w: dom.window, doc: dom.window.document, errors};
}

async function bootCard(card) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(CARD_PAGE), {
    url: 'https://skopax.example.com/c/' + card.num, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.fetch = async (p) => {
        if (String(p) === '/api/me') return {ok: true, status: 200, json: async () => ({loginEnabled: true, user: null, operator: false, tags: []})};
        return {ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(card))};
      };
    },
  });
  await wait(100);
  return {w: dom.window, doc: dom.window.document, errors};
}

async function theListAndItsPanelSayTheCardStoppedAfterTheEvent() {
  //given
  const {w, doc, errors} = await boot('list');
  const row = (key) => [...doc.querySelectorAll('#anoms article.inc')].find((x) => x.dataset.key === key);
  const rowTag = (key) => row(key).querySelector('.inc-head .c-status').textContent;
  const cause = (key) => { const p = row(key).querySelector('.inc-more .cause-line'); return p ? p.textContent : null; };

  //then
  assert.match(rowTag(EVENT), /^Stopped after the event /, 'the event card reads Stopped after the event: ' + rowTag(EVENT));
  assert.match(rowTag(PLAIN), /^Stopped firing \u00b7 unassigned /, 'a card without a cause still reads Stopped firing \u00b7 unassigned');
  assert.strictEqual(row(KID), undefined, 'the merged child sits under its parent');
  assert.deepStrictEqual([...row(EVENT).querySelectorAll('.inc-more li .tag.plain')].map((t) => t.textContent), ['stopped after the event'], 'a merged child takes its parent cause');
  assert.match(rowTag(OLDCAUSE), /^Stopped firing \u00b7 unassigned /, 'a cause from an earlier episode does not count');
  assert.strictEqual(cause(EVENT), 'Cause: an event \u00b7 ' + WHAT);
  assert.strictEqual(cause(PLAIN), null);
  assert.strictEqual(cause(RECUR), 'Cause: recurring \u00b7 a leak in the image cache', 'a recurring cause shows too');
  assert.match(rowTag(RECUR), /^Stopped firing \u00b7 unassigned /, 'a recurring cause does not say it stopped after an event');
  assert.strictEqual(cause(OLDCAUSE), null);
  assert.strictEqual(cause(CALM), 'Cause: an event \u00b7 <b>batch</b> job', 'the cause is text, never markup');
  assert.strictEqual(row(CALM).querySelector('.cause-line b'), null);
  assert.ok(row(EVENT).querySelector('.cause-line span[data-no-tx]'), 'a locale pack translates the lead, never the trigger');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the list row and the panel say Stopped after the event and name the cause, also for a merged child, never for an earlier episode');
}

async function theBoardTileSaysTheCardStoppedAfterTheEvent() {
  //given
  const {w, doc, errors} = await boot('board');
  const tile = (key) => [...doc.querySelectorAll('#anoms .tile')].find((x) => x.dataset.key === key);
  const tags = (key) => [...tile(key).querySelectorAll('.tile-foot .tag')].map((x) => x.textContent);

  //then
  assert.ok(tags(EVENT)[0].startsWith('Stopped after the event '), 'the waiting event tile: ' + tags(EVENT).join(' | '));
  assert.ok(tile(EVENT).querySelector('.tile-foot .tag.k-cleared').title.startsWith('Stopped after the event \u00b7 unassigned '), 'its tip says so too');
  assert.ok(/^Stopped firing \d/.test(tags(PLAIN)[0]), 'a plain waiting tile reads the short Stopped firing: ' + tags(PLAIN)[0]);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a waiting tile on the board reads Stopped after the event when the investigator tied it to one');
}

async function theCardPageSaysStoppedAfterTheEventAndShowsTheCause() {
  //given
  const base = {key: CALM, svc: 'feed-service', label: 'WARN lines', status: 'cleared', firstSeen: FIRST, clearedAt: iso(20), live: true};
  const calm = await bootCard(Object.assign({num: 154, sev: 'info', cause: {kind: 'event', what: WHAT, firstSeen: FIRST}}, base));
  const waiting = await bootCard(Object.assign({num: 150, sev: 'warn', cause: {kind: 'event', what: WHAT, firstSeen: FIRST}}, base));
  const plain = await bootCard(Object.assign({num: 151, sev: 'info'}, base));
  const recur = await bootCard(Object.assign({num: 155, sev: 'info', cause: {kind: 'recurring', what: 'a leak', firstSeen: FIRST}}, base));
  const status = (doc) => [doc.getElementById('state'), doc.querySelector('.status')].filter(Boolean).map((e) => e.textContent).join(' \u00b7 ');
  const cause = (doc) => { const p = doc.querySelector('.cause'); return p ? p.textContent : null; };

  //then
  assert.ok(status(calm.doc).startsWith('Stopped after the event \u00b7 '), 'a calm event card is not Fixed: ' + status(calm.doc));
  assert.ok(status(waiting.doc).startsWith('Open \u00b7 stopped after the event '), 'a waiting event card says when it stopped after the event: ' + status(waiting.doc));
  assert.ok(status(plain.doc).startsWith('Fixed \u00b7 '), 'a plain calm card stays Fixed');
  assert.strictEqual(cause(calm.doc), 'Cause: an event \u00b7 ' + WHAT);
  assert.strictEqual(cause(plain.doc), null);
  assert.strictEqual(cause(recur.doc), 'Cause: recurring \u00b7 a leak');
  assert.ok(status(recur.doc).startsWith('Fixed \u00b7 '), 'a recurring cause keeps the plain wording');
  assert.deepStrictEqual(calm.errors.concat(waiting.errors, plain.errors, recur.errors), []);
  calm.w.close(); waiting.w.close(); plain.w.close(); recur.w.close();
  console.log('PASS the card page reads Stopped after the event instead of Fixed and shows the cause');
}

(async () => {
  await theListAndItsPanelSayTheCardStoppedAfterTheEvent();
  await theBoardTileSaysTheCardStoppedAfterTheEvent();
  await theCardPageSaysStoppedAfterTheEventAndShowsTheCause();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
