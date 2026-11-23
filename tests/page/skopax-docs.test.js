const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const ROOT = path.join(__dirname, '..', '..');
const DOCS_BASE = 'https://github.com/skopax/skopax/blob/main/docs/';
const SECTIONS = ['Get started', "A card's life", 'Card actions', 'The investigator', 'Notifications and messages', 'Links and keyboard', 'For admins', 'Words'];
const LANGS = ['de', 'es', 'fr', 'tr'];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'}, archiveHours: 24,
    summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

function setting(key, value) {
  return {key, group: 'Cards', label: key, help: '', kind: 'int', live: true, value, base: value, default: value, source: 'admin', by: null, at: null};
}

const SETTINGS = {items: [setting('core.auto-resolve-hours-crit', '6'), setting('core.auto-resolve-hours-warn', '12'), setting('core.business-days', 'true'), setting('slack.post-sev', 'warn'), setting('slack.remind-hours', '4')], rev: 1};

async function boot(hash, opts) {
  const o = opts || {};
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state()));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/' + hash, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      if (o.lang) { w.SKOPAX_I18N = {[o.lang]: JSON.parse(fs.readFileSync(path.join(ROOT, 'skopax', 'locale', o.lang, 'page.json'), 'utf8')).page}; w.document.cookie = 'skopax_lang=' + o.lang + '; path=/'; }
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p0) => {
        const p = String(p0);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', operator: true, admin: !!o.admin, teams: [], tags: [], features: o.features || {}});
        if (p.startsWith('/api/admin/settings')) return o.admin ? ok(SETTINGS) : {ok: false, status: 403, json: async () => ({})};
        if (p.startsWith('/api/investigator/status')) return ok(o.status || {mode: 'live', enabled: true, rooms: [], waiting: [], recent: [], limits: {cap: 10, spentToday: 2.5}});
        if (p.startsWith('/api/server')) return ok({investigate: {mode: 'live', stopped: false}});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(500);
  return {w: dom.window, doc: dom.window.document, errors};
}

function slugs(file) {
  const text = fs.readFileSync(path.join(ROOT, 'docs', file), 'utf8');
  const seen = {};
  return text.split('\n').filter((l) => /^#{1,6} /.test(l)).map((l) => {
    const base = l.replace(/^#+ /, '').trim().toLowerCase().replace(/[^\w\- ]/g, '').replace(/ /g, '-');
    const n = seen[base] || 0;
    seen[base] = n + 1;
    return n ? base + '-' + n : base;
  });
}

function shown(el) {
  for (let x = el; x; x = x.parentElement) if (x.hidden) return false;
  return true;
}

function texts(root) {
  const out = [];
  const walk = (n) => {
    if (n.nodeType === 3) { const t = n.nodeValue.trim(); if (t) out.push(t); return; }
    if (n.nodeType !== 1 || n.matches('[data-no-tx], code, script, style')) return;
    ['title', 'placeholder', 'aria-label'].forEach((a) => { const v = n.getAttribute(a); if (v && v.trim()) out.push(v.trim()); });
    n.childNodes.forEach(walk);
  };
  walk(root);
  return out;
}

async function theGuideHasItsEightSectionsInOrderEachAFoldWithAHeading() {
  //given
  const {w, doc, errors} = await boot('#t=docs');

  //when
  const panel = doc.getElementById('panel-docs');
  const secs = [...panel.querySelectorAll('details.docs-sec')];

  //then
  assert.ok(!panel.hidden, 'the Docs page is open');
  assert.deepStrictEqual(secs.map((d) => d.querySelector(':scope > summary h3').textContent.trim()), SECTIONS);
  assert.ok(secs.every((d) => d.querySelectorAll('li').length >= 2), 'each section has a few items');
  assert.ok(secs[0].open && secs.slice(1).every((d) => !d.open), 'Get started is open, the rest are folded so the page scans fast');
  assert.ok(panel.querySelector('a[href="/docs"]'), 'the API reference link stays');
  const gh = [...panel.querySelectorAll('a')].find((a) => a.textContent.trim() === 'Full docs on GitHub');
  assert.ok(gh && gh.getAttribute('href').startsWith('https://github.com/skopax/skopax/'), 'the footer links the full docs on GitHub');
  assert.strictEqual(panel.querySelector('.foot-body'), null, 'the old four dense paragraphs are gone');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the guide has its eight sections in order, each a fold with a heading, and a footer with the API reference and the full docs');
}

async function theSearchKeepsOnlyMatchingItemsAndSaysWhenNothingMatches() {
  //given
  const {w, doc, errors} = await boot('#t=docs');
  const find = doc.getElementById('docs-find');
  const none = doc.getElementById('docs-none');
  const items = () => [...doc.querySelectorAll('#panel-docs details.docs-sec li')];

  //then
  assert.ok(find && find.getAttribute('aria-label'), 'a labelled search box sits at the top');
  assert.ok(none.hidden);

  //when
  find.value = 'mute';
  find.dispatchEvent(new w.Event('input'));
  const left = items().filter(shown);

  //then
  assert.ok(left.length >= 2, 'some items mention mute');
  assert.ok(left.every((li) => /mute/i.test(li.textContent)), 'only items with mute stay: ' + left.map((li) => li.textContent).filter((t) => !/mute/i.test(t)).join(' | '));
  assert.ok(items().some((li) => !shown(li)), 'the others are hidden');
  assert.ok([...doc.querySelectorAll('#panel-docs details.docs-sec')].filter(shown).every((d) => d.open), 'a section with a hit opens');
  assert.ok(none.hidden);

  //when
  find.value = 'qwzxv';
  find.dispatchEvent(new w.Event('input'));

  //then
  assert.ok(!none.hidden && none.textContent.trim() === 'Nothing matches', 'gibberish says Nothing matches');
  assert.ok(items().every((li) => !shown(li)));

  //when
  find.value = '';
  find.dispatchEvent(new w.Event('input'));
  const secs = [...doc.querySelectorAll('#panel-docs details.docs-sec')];

  //then
  assert.ok(none.hidden && items().every(shown), 'clearing the search shows everything again');
  assert.ok(secs[0].open && secs.slice(1).every((d) => !d.open), 'and puts the folds back as they were');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the search keeps only matching items, opens their sections, says Nothing matches for gibberish and restores the page when cleared');
}

async function theAdminLinksPointAtHeadingsThatExistInTheDocs() {
  //given
  const {w, doc, errors} = await boot('#t=docs');

  //when
  const admin = [...doc.querySelectorAll('#panel-docs details.docs-sec')][6];
  const links = [...admin.querySelectorAll('a[href^="https://github.com/"]')].map((a) => a.getAttribute('href'));

  //then
  assert.ok(links.length >= 6, 'each admin place links its docs: ' + links.join(' '));
  links.forEach((href) => {
    assert.ok(href.startsWith(DOCS_BASE), href);
    const m = /^([\w-]+\.md)#([\w-]+)$/.exec(href.slice(DOCS_BASE.length));
    assert.ok(m, 'a docs file with an anchor: ' + href);
    assert.ok(fs.existsSync(path.join(ROOT, 'docs', m[1])), 'the file exists: ' + m[1]);
    assert.ok(slugs(m[1]).indexOf(m[2]) >= 0, 'the anchor is a heading of ' + m[1] + ': #' + m[2]);
  });
  ['Connect', 'Settings', 'Investigator', 'Access', 'Workspaces', 'Set up email', 'single sign-on'].forEach((place) => assert.ok(admin.textContent.indexOf(place) >= 0, 'For admins names ' + place));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS every admin link points at a docs file and heading that exist');
}

async function heldSlackPostsPointAtHistoryNotNotifications() {
  //given
  const {w, doc, errors} = await boot('#t=docs');

  //when
  const panel = doc.getElementById('panel-docs');
  const held = doc.getElementById('docs-held');

  //then
  assert.ok(held && held.textContent.indexOf('Not posted to Slack') >= 0, 'the guide says where held posts are');
  assert.ok(held.textContent.indexOf('History') >= 0 && held.textContent.indexOf('Activity') >= 0, 'on History, under Activity in the menu');
  assert.ok(held.textContent.indexOf('Notifications') < 0, 'not on the Notifications page any more');
  assert.ok(panel.textContent.indexOf('unless held') < 0 && panel.textContent.indexOf('within 30 s') < 0, 'the old draft countdown is gone');
  assert.ok(panel.textContent.indexOf('Notifications') >= 0, 'the Notifications page is still described for what it holds');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS held Slack posts point at History under Activity, not at Notifications');
}

async function anAdminSeesTheLiveAutoResolveHoursAndOthersSeeNoMadeUpNumber() {
  //given
  const admin = await boot('#t=docs', {admin: true});

  //when
  const line = admin.doc.getElementById('docs-auto').textContent;
  const post = admin.doc.getElementById('docs-postsev').textContent;
  const remind = admin.doc.getElementById('docs-remind').textContent;

  //then
  assert.ok(/\b6 h\b/.test(line) && /\b12 h\b/.test(line), 'the critical and warning hours from the settings: ' + line);
  assert.ok(/Monday to Friday/.test(line), 'business days on: ' + line);
  assert.ok(/Warning/.test(post), 'the Slack post-at-once severity: ' + post);
  assert.ok(/\b4 h\b/.test(remind), 'the reminder hours: ' + remind);
  assert.deepStrictEqual(admin.errors, []);
  admin.w.close();

  //given
  const viewer = await boot('#t=docs');

  //when
  const plain = viewer.doc.getElementById('docs-auto').textContent;

  //then
  assert.ok(plain.trim() && !/\d/.test(plain), 'without the admin settings the line has no number: ' + plain);
  assert.ok(!/\d/.test(viewer.doc.getElementById('docs-remind').textContent));
  assert.deepStrictEqual(viewer.errors, []);
  viewer.w.close();
  console.log('PASS an admin sees the live auto-resolve, Slack and reminder values, and a non-admin a line with no made-up number');
}

async function theInvestigatorLinesShowItsModeAndDailyCapForEveryone() {
  //given
  const {w, doc, errors} = await boot('#t=docs', {status: board({mode: 'shadow'})});
  const line = () => doc.getElementById('docs-mode').textContent + ' | ' + doc.getElementById('docs-cap').textContent;

  //then
  assert.ok(/shadow mode now/.test(line()) && line().indexOf('$10.00') >= 0, 'the current mode and the daily limit: ' + line());

  //when
  w.docsInv(board({enabled: false, problem: 'key'}));

  //then
  assert.ok(/does not run/.test(line()) && /no AI key yet/.test(line()) && /Admin > Investigator/.test(line()) && !/runs in/.test(line()), 'no key says it does not run, why and where to fix it: ' + line());

  //when
  w.docsInv(board({enabled: false, problem: 'model'}));

  //then
  assert.ok(/does not run/.test(line()) && /model is not known/.test(line()) && /Admin > Investigator/.test(line()), 'an unknown model: ' + line());

  //when
  w.docsInv(board({enabled: false}));

  //then
  assert.ok(/does not run/.test(line()) && !/runs in/.test(line()), 'disabled for another reason: ' + line());

  //when
  w.docsInv(board({halted: 'capped', limits: {cap: 0, spentToday: 0}}));

  //then
  assert.strictEqual((line().match(/does not run/g) || []).length, 1, 'a limit of 0 says it does not run once: ' + line());
  assert.ok(line().indexOf("reached today's spend limit") < 0 && !/runs in/.test(line()), 'and not that the limit was reached today: ' + line());

  //when
  w.docsInv(board({halted: 'capped', limits: {cap: 10, spentToday: 10}}));

  //then
  assert.ok(line().indexOf("reached today's spend limit") >= 0, 'a real cap reached today says so: ' + line());
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the investigator lines show its mode and daily limit, and say plainly when and why it does not run');
}

async function slackAskingIsClaimedOnlyWithTheListenerOn() {
  //given
  const on = await boot('#t=docs', {features: {'slack-listener': true}});
  const off = await boot('#t=docs', {features: {'slack-listener': false}});

  //when
  const said = on.doc.getElementById('docs-slack').textContent, plain = off.doc.getElementById('docs-slack').textContent;

  //then
  assert.ok(/mention the Skopax app/.test(said), 'with the listener on: ' + said);
  assert.ok(!/mention|tag @/.test(plain) && /Slack listener/.test(plain), 'with it off, no claim, only what it needs: ' + plain);
  assert.deepStrictEqual(on.errors.concat(off.errors), []);
  on.w.close();
  off.w.close();
  console.log('PASS asking in Slack is claimed only while the Slack listener is on');
}

async function eachTopicIsSaidOnceAndTheLinesMatchTheirSettings() {
  //given
  const {w, doc, errors} = await boot('#t=docs', {admin: true});
  const items = [...doc.querySelectorAll('#panel-docs details.docs-sec li')];
  const find = doc.getElementById('docs-find');

  //when
  const muteFull = items.filter((li) => /24 hours or/.test(li.textContent));
  const knownFull = items.filter((li) => /for good, with a reason/.test(li.textContent) && li.closest('details').querySelector('h3').textContent !== 'Words');
  find.value = 'read more';
  find.dispatchEvent(new w.Event('input'));

  //then
  assert.strictEqual(muteFull.length, 1, 'Mute is explained once: ' + muteFull.map((li) => li.textContent).join(' | '));
  assert.strictEqual(knownFull.length, 1, 'Known noise is explained once outside Words');
  assert.ok(!doc.getElementById('docs-none').hidden, 'the Read more link text is not searched');
  assert.ok(/no change for 24 hours/.test(doc.getElementById('docs-arch').textContent), 'Archived reads as the setting means: ' + doc.getElementById('docs-arch').textContent);
  assert.ok(/every 4 h/.test(doc.getElementById('docs-remind').textContent) && /restart/.test(doc.getElementById('docs-remind').textContent), 'the reminder hours apply after a restart');
  assert.ok(items.some((li) => /each card's Notify row/.test(li.textContent)), 'routes by hand from each card\'s Notify row');
  assert.strictEqual(doc.querySelector('details.foot'), null);
  const tpl = fs.readFileSync(path.join(ROOT, 'skopax', 'web', 'template.html'), 'utf8');
  assert.ok(tpl.indexOf('details.foot') < 0, 'the dead foot CSS is gone');
  LANGS.forEach((l) => {
    const p = JSON.parse(fs.readFileSync(path.join(ROOT, 'skopax', 'locale', l, 'page.json'), 'utf8')).page;
    assert.ok(!Object.hasOwn(p, 'How this works') && !Object.hasOwn(p, 'Links and Slack'), l + ' drops the dead texts');
  });
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS Mute and Known noise are explained once, Read more is not searched, and Archived, reminders and Notify read as the code works');
}

function shape(t) {
  let i = 0;
  return t.replace(/\d+(?:[.,:]\d+)*/g, () => '{' + i++ + '}');
}

function board(extra) {
  return Object.assign({mode: 'live', enabled: true, halted: null, problem: null, rooms: [], waiting: [], recent: [], limits: {cap: 10, spentToday: 2.5}}, extra);
}

const VARIANTS = [
  {admin: [['core.auto-resolve-hours-crit', '6'], ['core.auto-resolve-hours-warn', '0'], ['core.business-days', 'false'], ['slack.post-sev', 'crit'], ['slack.remind-hours', '0']], inv: board({halted: 'capped', limits: {cap: 0, spentToday: 0}})},
  {admin: [['core.auto-resolve-hours-crit', '0'], ['core.auto-resolve-hours-warn', '12'], ['core.business-days', 'true'], ['slack.post-sev', 'off'], ['slack.remind-hours', '1.5']], inv: board({mode: 'shadow', halted: 'stopped', limits: {cap: 10, spentToday: 1}})},
  {admin: [['core.auto-resolve-hours-crit', '0'], ['core.auto-resolve-hours-warn', '0'], ['slack.post-sev', 'warn']], inv: board({halted: 'capped', limits: {cap: 10, spentToday: 10}})},
  {admin: [['core.auto-resolve-hours-crit', '6'], ['core.auto-resolve-hours-warn', '6']], inv: board({enabled: false, halted: 'off'})},
  {admin: [['slack.post-sev', 'warn']], inv: board({mode: 'shadow', limits: {cap: 5, spentToday: 0}})},
  {admin: null, inv: board({mode: 'off', enabled: false})},
  {admin: null, inv: board({enabled: false, problem: 'key'})},
  {admin: null, inv: board({enabled: false, problem: 'model'})},
  {admin: null, inv: board({enabled: false})},
  {admin: null, inv: null, archive: 0, features: {'slack-listener': true}},
];

async function everyGuideTextHasATranslationInEachCorePack() {
  //given
  const {w, doc} = await boot('#t=docs', {admin: true});
  const panel = doc.getElementById('panel-docs');
  const packs = LANGS.map((l) => [l, JSON.parse(fs.readFileSync(path.join(ROOT, 'skopax', 'locale', l, 'page.json'), 'utf8')).page]);
  const live = ['docs-auto', 'docs-remind', 'docs-postsev', 'docs-mode', 'docs-cap', 'docs-arch', 'docs-slack'];
  const all = new Set(texts(panel));

  //when
  VARIANTS.forEach((v) => {
    w.docsAdmin(v.admin ? {items: v.admin.map(([k, x]) => setting(k, x))} : null);
    w.docsInv(v.inv);
    if ('archive' in v) { w.D.archiveHours = v.archive; w.docsArchive(); }
    if (v.features) { w.ME_FEATURES = v.features; w.docsSlack(); }
    live.forEach((id) => texts(doc.getElementById(id)).forEach((t) => all.add(t)));
  });
  const missing = packs.map(([l, p]) => [l, [...all].filter((t) => !Object.hasOwn(p, t) && !Object.hasOwn(p, shape(t)))]).filter(([, m]) => m.length);
  const shapes = packs.map(([l, p]) => [l, [...all].filter((t) => Object.hasOwn(p, shape(t)) && (p[shape(t)].match(/\{\d+\}/g) || []).sort().join() !== (shape(t).match(/\{\d+\}/g) || []).sort().join())]).filter(([, m]) => m.length);

  //then
  assert.ok(all.size > 100, 'the guide has its texts, live variants included: ' + all.size);
  assert.deepStrictEqual(missing, [], 'every text of the guide is in every core pack');
  assert.deepStrictEqual(shapes, [], 'each translated live line keeps its numbers');
  w.close();
  console.log('PASS every text of the guide, each live variant included, has a translation in each core pack');
}

async function aTranslatedPageFiltersOnTheTranslatedText() {
  //given
  const {w, doc, errors} = await boot('#t=docs', {lang: 'de'});
  const pack = JSON.parse(fs.readFileSync(path.join(ROOT, 'skopax', 'locale', 'de', 'page.json'), 'utf8')).page;
  const find = doc.getElementById('docs-find');
  const term = pack['Known noise'].toLowerCase();

  //when
  const heads = [...doc.querySelectorAll('#panel-docs details.docs-sec h3')].map((h) => h.textContent.trim());
  find.value = term;
  find.dispatchEvent(new w.Event('input'));
  const left = [...doc.querySelectorAll('#panel-docs details.docs-sec li')].filter(shown);

  //then
  assert.deepStrictEqual(heads, SECTIONS.map((t) => pack[t]), 'the section names are translated');
  assert.strictEqual(find.getAttribute('placeholder'), pack['Search the guide']);
  assert.ok(left.length >= 2 && left.every((li) => li.textContent.toLowerCase().indexOf(term) >= 0), 'the search matches the translated text: ' + term);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS under a language pack the guide is translated and the search matches the translated text');
}

(async () => {
  await theGuideHasItsEightSectionsInOrderEachAFoldWithAHeading();
  await theSearchKeepsOnlyMatchingItemsAndSaysWhenNothingMatches();
  await theAdminLinksPointAtHeadingsThatExistInTheDocs();
  await heldSlackPostsPointAtHistoryNotNotifications();
  await anAdminSeesTheLiveAutoResolveHoursAndOthersSeeNoMadeUpNumber();
  await theInvestigatorLinesShowItsModeAndDailyCapForEveryone();
  await slackAskingIsClaimedOnlyWithTheListenerOn();
  await eachTopicIsSaidOnceAndTheLinesMatchTheirSettings();
  await everyGuideTextHasATranslationInEachCorePack();
  await aTranslatedPageFiltersOnTheTranslatedText();
})().catch((e) => { console.error(e); process.exit(1); });
