const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const NOW = Date.now();
const iso = (min) => new Date(NOW - min * 60000).toISOString();
const FAILED = 'log|checkout-service|ERROR|failed';
const GONE = 'log|shop-web|ERROR|gone';
const BOTH = 'log|orders-api|ERROR|both';
const CLEAN = 'log|billing-api|ERROR|clean';
const MUTED = 'log|search-api|ERROR|muted';
const DONE = 'log|mail-api|ERROR|done';

function card(key, num, extra) {
  return Object.assign({key, num, sev: 'warn', status: 'open', svc: key.split('|')[1], label: key.split('|')[2], src: 'logs', sig: 'log', now: 1, firstSeen: iso(90), lastSeen: iso(1), what: 'text'}, extra || {});
}
const draft = () => ({text: 'Checkout errors are up', at: iso(30), by: 'investigator'});
const failedPost = () => ({slackDraft: draft(), autoSend: {at: iso(20), claimedAt: iso(19), failedAt: iso(18), error: 'channel_not_found'}});
const gone = () => ({gone: {status: 404, since: iso(40)}});
const back = () => ({previousResolution: {kind: 'handled', at: iso(500)}});
function cards(failedExtra) {
  return [card(FAILED, 51, failedExtra || failedPost()),
    card(GONE, 52, gone()),
    card(BOTH, 53, Object.assign(failedPost(), gone())),
    card(CLEAN, 54),
    card(MUTED, 55, {muted: 'noise'}),
    card(DONE, 56, Object.assign({status: 'cleared', clearedAt: iso(10), triage: 'handled'}, gone(), back()))];
}
function state(rev, list) {
  return {rev, updatedAt: iso(0), sweptIso: iso(1), nextIso: iso(-59), sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: list, services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(view, serve, lang) {
  serve = serve || {rev: 1, list: cards()};
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE, {view}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state(serve.rev, serve.list))), {
    url: 'https://skopax.example.com/#t=anoms/all', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      if (lang) {
        const raw = JSON.parse(fs.readFileSync(require('path').join(require('path').dirname(TEMPLATE), '..', 'locale', lang, 'page.json'), 'utf8'));
        w.SKOPAX_I18N = {[lang]: Object.assign({}, raw.page, {'@rules': raw.rules, '@months': raw.months, '@days': raw.days, '@tails': raw.tails, '@th': raw.headers})};
        w.document.cookie = 'skopax_lang=' + lang + '; path=/';
      }
      w.scrollTo = () => {}; w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        p = String(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'op@example.com', operator: true, teams: [], tags: []});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: serve.rev});
        if (p.startsWith('/api/state')) return ok(state(serve.rev, serve.list));
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors, serve};
}

const tileOf = (doc, key) => [...doc.querySelectorAll('#anoms .tile')].find((t) => t.dataset.key === key);
const rowOf = (doc, key) => doc.querySelector('#anoms article.inc[data-key="' + key + '"]');
const markOf = (el) => el.querySelector('.warn-mark');

async function aTileWithAFailedSlackPostShowsTheWarningMark() {
  //given
  const {doc, errors} = await boot('board');

  //when
  const tile = tileOf(doc, FAILED);
  const mark = markOf(tile);

  //then
  assert.ok(mark, 'the tile has a mark');
  assert.ok(mark.closest('.tile-top'), 'the mark sits in the top line');
  assert.deepStrictEqual([...tile.querySelectorAll('.tile-top > *')].map((e) => e.className.split(' ')[0]), ['sev', 'tile-num', 'warn-mark', 'tile-age']);
  assert.strictEqual(mark.getAttribute('role'), 'img');
  assert.strictEqual(mark.getAttribute('aria-label'), 'Warning: Slack post failed: channel_not_found');
  assert.ok(mark.getAttribute('title').startsWith('Slack post failed: channel_not_found'), mark.getAttribute('title'));
  assert.ok(mark.getAttribute('title').indexOf('Checkout errors are up') >= 0, 'the tip follows the text');
  assert.ok(tile.querySelector('.tile-open .sr').textContent.indexOf('Warning: Slack post failed: channel_not_found') >= 0, 'the screen reader text carries it');
  assert.deepStrictEqual(errors, []);
}

async function aTileForAGonePageShowsTheWarningMark() {
  //given
  const {doc, errors} = await boot('board');

  //when
  const mark = markOf(tileOf(doc, GONE));

  //then
  assert.ok(mark, 'the tile has a mark');
  assert.strictEqual(mark.getAttribute('aria-label'), 'Warning: Page gone \u00b7 404');
  assert.match(mark.getAttribute('title'), /The page answers HTTP 404/);
  assert.deepStrictEqual(errors, []);
}

async function aCardWithTwoWarningsSaysHowMany() {
  //given
  const {doc, errors} = await boot('board');

  //when
  const mark = markOf(tileOf(doc, BOTH));

  //then
  assert.strictEqual(mark.getAttribute('aria-label'), '2 warnings: Slack post failed: channel_not_found, Page gone \u00b7 404');
  const lines = mark.getAttribute('title').split('\n');
  assert.strictEqual(lines[0], 'Slack post failed: channel_not_found');
  assert.strictEqual(lines[1], 'Page gone \u00b7 404');
  assert.deepStrictEqual(errors, []);
}

async function aCardWithNoWarningShowsNoMark() {
  //given
  const {doc, errors} = await boot('board');

  //when
  const tiles = [CLEAN, MUTED, DONE].map((k) => tileOf(doc, k));

  //then
  tiles.forEach((t, i) => {
    assert.ok(t, 'the tile ' + i + ' is on the board');
    assert.strictEqual(markOf(t), null, 'tile ' + i + ' shows no mark');
  });
  assert.deepStrictEqual(errors, []);
}

async function aMutedCardWhosePageIsGoneKeepsTheMark() {
  //given
  const list = cards();
  list.push(card('log|shop-web|ERROR|mutedgone', 57, Object.assign({muted: 'noise'}, gone())));
  const {doc, errors} = await boot('board', {rev: 1, list});

  //when
  const tile = tileOf(doc, 'log|shop-web|ERROR|mutedgone');

  //then
  assert.ok(tile, 'the muted tile is on the board');
  assert.strictEqual(markOf(tile).getAttribute('aria-label'), 'Warning: Page gone \u00b7 404');
  assert.deepStrictEqual(errors, []);
}

async function theGoneWarningIsTranslatedInTurkish() {
  //given
  const {doc, errors} = await boot('board', null, 'tr');

  //when
  const mark = markOf(tileOf(doc, GONE));

  //then
  assert.ok(/Sayfa yok \u00b7 404/.test(mark.getAttribute('aria-label')), mark.getAttribute('aria-label'));
  assert.ok(!/Page gone/.test(mark.getAttribute('aria-label')));
  assert.deepStrictEqual(errors, []);
}

async function aListRowShowsTheMarkAndTheOpenedPanelHeaderDoesNot() {
  //given
  const {doc, errors} = await boot('list');

  //when
  const row = rowOf(doc, FAILED);
  const mark = markOf(row.querySelector('.inc-title'));
  const clean = markOf(rowOf(doc, CLEAN));
  const mutedRow = rowOf(doc, MUTED), doneRow = rowOf(doc, DONE);
  row.querySelector('.inc-title .fold').click();
  await wait(60);
  const opened = [...doc.querySelectorAll('.card-drawer')].length;

  //then
  assert.ok(mark, 'the row has a mark');
  assert.strictEqual(mark.parentNode.className.split(' ').indexOf('c-status') >= 0, true, 'the mark sits in the state cell');
  assert.strictEqual(mark.getAttribute('aria-label'), 'Warning: Slack post failed: channel_not_found');
  assert.strictEqual(clean, null);
  assert.ok(mutedRow, 'the muted row is listed');
  assert.ok(doneRow, 'the resolved row is listed');
  assert.strictEqual(markOf(mutedRow), null, 'muted row: no mark');
  assert.strictEqual(markOf(doneRow), null, 'resolved row: no mark');
  assert.ok(row.querySelector('.inc-tags .tag.not-posted'), 'the tag row still has the warning');
  assert.strictEqual(opened, 0);
  assert.deepStrictEqual(errors, []);
}

async function theOpenedPanelHeaderHasNoMark() {
  //given
  const {w, doc, errors} = await boot('board');

  //when
  tileOf(doc, FAILED).querySelector('.tile-open').click();
  await wait(80);
  const panel = doc.querySelector('.card-drawer article.inc');

  //then
  assert.ok(panel, 'the panel is open');
  assert.strictEqual(panel.querySelector('.inc-title .warn-mark'), null);
  assert.ok(panel.querySelector('.inc-tags .tag.not-posted'), 'the tag row shows the warning');
  assert.deepStrictEqual(errors, []);
  w.close();
}

async function theMarkGoesAwayWhenThePostSucceedsOnTheNextRender() {
  //given
  const serve = {rev: 1, list: cards()};
  const {w, doc, errors} = await boot('board', serve);
  const before = !!markOf(tileOf(doc, FAILED));

  //when
  serve.rev = 2;
  serve.list = cards({slackDraft: draft(), slackPost: {at: iso(1), by: 'investigator', permalink: 'https://example.slack.com/archives/C1/p1'}, autoSend: {at: iso(20), claimedAt: iso(19)}});
  w.document.dispatchEvent(new w.Event('visibilitychange'));
  await wait(400);

  //then
  assert.strictEqual(before, true);
  assert.ok(tileOf(doc, FAILED), 'the tile is still there');
  assert.strictEqual(markOf(tileOf(doc, FAILED)), null);
  assert.deepStrictEqual(errors, []);
}

async function theWarningsStillShowInTheOpenedCardTagRow() {
  //given
  const {w, errors} = await boot('board');

  //when
  const tags = w.cardTags(w.CARDS.filter((x) => x.key === BOTH)[0]);
  const warns = w.cardWarns(w.CARDS.filter((x) => x.key === BOTH)[0]);
  const none = w.cardWarns(w.CARDS.filter((x) => x.key === CLEAN)[0]);

  //then
  assert.ok(/not-posted/.test(tags.rest) && /page-gone/.test(tags.rest), tags.rest);
  assert.deepStrictEqual([...warns.map((x) => x.text)], ['Slack post failed: channel_not_found', 'Page gone \u00b7 404']);
  assert.ok(warns.every((x) => x.tip), 'every warning has a tip');
  assert.strictEqual(none.length, 0);
  assert.deepStrictEqual(errors, []);
}

async function aBackTagIsAWarningToo() {
  //given
  const {w, errors} = await boot('board');
  const c = card('log|shop-web|ERROR|back', 61, {previousResolution: {kind: 'fixed', at: iso(900), ref: 'OPS-123'}, cameBack: true});

  //when
  const warns = w.cardWarns(c);

  //then
  assert.deepStrictEqual([...warns.map((x) => x.text)], ['Came back after fix OPS-123']);
  assert.match(warns[0].tip, /then it fired again/);
  assert.match(w.cardTags(c).rest, /came-back/);
  assert.deepStrictEqual(errors, []);
}

async function aCameBackAfterNoActionTagSaysSoAndNamesBothTimes() {
  //given
  const {w, errors} = await boot('board');
  const c = card('log|shop-web|ERROR|noaction', 63, {previousResolution: {kind: 'handled', at: iso(900)}, firstSeen: iso(30)});

  //when
  const warns = w.cardWarns(c);

  //then
  assert.deepStrictEqual([...warns.map((x) => x.text)], ['Came back after No action needed']);
  assert.strictEqual(warns[0].tip, 'Resolved as no action needed on ' + w.daytime(iso(900)) + ', then it fired again at ' + w.daytime(iso(30)));
  assert.match(w.cardTags(c).rest, /Came back after No action needed/);
  assert.ok(!/Was no action needed/.test(w.cardTags(c).rest));
  assert.deepStrictEqual(errors, []);
}

async function theCameBackAfterNoActionTagIsTranslated() {
  //given
  const c = card('log|shop-web|ERROR|noaction', 63, {previousResolution: {kind: 'handled', at: iso(900)}, firstSeen: iso(30)});
  const out = {};

  //when
  for (const lang of ['de', 'es', 'fr', 'tr']) {
    const {w, errors} = await boot('board', undefined, lang);
    const tag = w.backTag(c);
    const host = w.document.createElement('div');
    host.innerHTML = tag;
    const part = (iso) => { const m = /^(\d+) (\w+) (\S+)$/.exec(w.daytime(iso)); return {d: m[1], mon: m[2], t: m[3]}; };
    out[lang] = {at: part(iso(900)), again: part(iso(30)), raw: host.firstChild.textContent, text: w.tx(host.firstChild.textContent), tip: w.tx(host.firstChild.getAttribute('title')), errors};
  }

  //then
  const months = JSON.parse(fs.readFileSync(require('path').join(require('path').dirname(TEMPLATE), '..', 'locale', 'de', 'page.json'), 'utf8')).months;
  const at = out.de.at, again = out.de.again;
  assert.strictEqual(out.de.tip, 'Am ' + at.d + '. ' + months[at.mon] + ' ' + at.t + ' als kein Handlungsbedarf gel\u00f6st, dann am ' + again.d + '. ' + months[again.mon] + ' ' + again.t + ' wieder aktiv');
  for (const lang of Object.keys(out)) {
    assert.strictEqual(out[lang].raw, 'Came back after No action needed');
    assert.notStrictEqual(out[lang].text, 'Came back after No action needed', lang + ' label is translated');
    assert.ok(!/Resolved as no action needed|then it fired again/.test(out[lang].tip), lang + ' tip is translated: ' + out[lang].tip);
    assert.deepStrictEqual(out[lang].errors, []);
  }
}

function everyIconInTheTagRowsIsSmall() {
  //given
  const css = fs.readFileSync(TEMPLATE, 'utf8');
  const withTeams = card('log|shop-web|ERROR|teams', 62, {teamsPosts: [{at: iso(5), route: 'seo', by: 'server'}]});

  //when
  const rule = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].filter((m) => m[1].split(',').map((s) => s.trim()).indexOf('.inc-tags .tag svg') >= 0 && m[1].split(',').map((s) => s.trim()).indexOf('.tech-tags .tag svg') >= 0).map((m) => m[2]).join(' ');

  //then
  assert.ok(withTeams.teamsPosts.length === 1);
  assert.match(rule, /width:\s*11px/);
  assert.match(rule, /height:\s*11px/);
  assert.match(rule, /flex:\s*none/);
}

async function theTeamsTagInsideTheOpenedCardCarriesAnIconTheRuleSizes() {
  //given
  const list = cards();
  list.push(card('log|shop-web|ERROR|teams', 62, {teamsPosts: [{at: iso(5), route: 'seo', by: 'server'}]}));
  const {doc, errors} = await boot('list', {rev: 1, list});

  //when
  const row = rowOf(doc, 'log|shop-web|ERROR|teams');
  const icon = row.querySelector('.tech-tags .tag.teams-sent svg, .inc-tags .tag.teams-sent svg');

  //then
  assert.ok(icon, 'the teams tag has an icon inside a tag row');
  assert.deepStrictEqual(errors, []);
}

(async () => {
  const jobs = [
    ['failed post tile', aTileWithAFailedSlackPostShowsTheWarningMark],
    ['gone tile', aTileForAGonePageShowsTheWarningMark],
    ['two warnings', aCardWithTwoWarningsSaysHowMany],
    ['no warning no mark', aCardWithNoWarningShowsNoMark],
    ['muted gone keeps mark', aMutedCardWhosePageIsGoneKeepsTheMark],
    ['gone warning in tr', theGoneWarningIsTranslatedInTurkish],
    ['list row', aListRowShowsTheMarkAndTheOpenedPanelHeaderDoesNot],
    ['panel header', theOpenedPanelHeaderHasNoMark],
    ['goes away', theMarkGoesAwayWhenThePostSucceedsOnTheNextRender],
    ['tag row unchanged', theWarningsStillShowInTheOpenedCardTagRow],
    ['back tag', aBackTagIsAWarningToo],
    ['came back after no action', aCameBackAfterNoActionTagSaysSoAndNamesBothTimes],
    ['came back after no action translated', theCameBackAfterNoActionTagIsTranslated],
    ['tag row icons small', async () => everyIconInTheTagRowsIsSmall()],
    ['teams icon in tag row', theTeamsTagInsideTheOpenedCardCarriesAnIconTheRuleSizes],
  ];
  for (const [name, job] of jobs) {
    try { await job(); console.log('PASS ' + name); } catch (e) {
      console.error('FAIL ' + name + ': ' + e.message.replace(/\x1b\[[0-9;]*m/g, '').split('\n').slice(0, 12).join(' | ')); process.exitCode = 1;
    }
  }
  process.exit(process.exitCode || 0);
})();
