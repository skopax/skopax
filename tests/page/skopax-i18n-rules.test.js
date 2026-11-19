const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const RUNTIME = path.join(path.dirname(TEMPLATE), '..', 'runtime.py');
const PAGE_PACK = /^PAGE_PACK = '(.+)'$/m.exec(fs.readFileSync(RUNTIME, 'utf8'))[1];
const CORE_PACK = /^CORE_PACK = '(.+)'$/m.exec(fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'locales.py'), 'utf8'))[1];
const SLOT = '<script type="text/plain" id="plugin-scripts"></script>';
const DOT = ' \u00b7 ';
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const EVIL = '<img src=x onerror=alert(1)>';

function snippet(lang, pack, wrapper) {
  const args = [JSON.stringify(lang), JSON.stringify(pack).replace(/</g, '\\u003c')];
  return (wrapper || PAGE_PACK).replace(/%s/g, () => args.shift());
}

function served(lang, pack, wrapper) {
  return '<script>' + snippet(lang, pack, wrapper) + '</script>';
}

function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)}, anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(packs, lang, navLang, appearance) {
  const errors = [], warned = [], alerts = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  vc.on('warn', (...args) => warned.push(args.join(' ')));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state())).replace(SLOT, () => packs.map(([l, p, core]) => served(l, p, core ? CORE_PACK : null)).join(''));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=anoms/all', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      if (navLang) {
        const langs = [].concat(navLang);
        Object.defineProperty(w.navigator, 'language', {get: () => langs[0], configurable: true});
        Object.defineProperty(w.navigator, 'languages', {get: () => langs, configurable: true});
      }
      if (lang) w.document.cookie = 'skopax_lang=' + lang + '; path=/';
      if (appearance) w.localStorage.setItem('skopax-appearance', JSON.stringify(appearance));
      w.alert = (x) => alerts.push(x);
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (String(p).startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', operator: true, teams: [], tags: [], chat: true});
        if (String(p).startsWith('/api/actions') || String(p).startsWith('/api/deploy-reports')) return ok([]);
        if (String(p).startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(300);
  return {w: dom.window, doc: dom.window.document, errors, warned, alerts};
}

async function shown(doc, markup) {
  const box = doc.createElement('div');
  box.id = 'probe';
  box.innerHTML = markup;
  doc.body.appendChild(box);
  await wait(20);
  return box;
}

function builtIn(lang) {
  const raw = JSON.parse(fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'locale', lang, 'page.json'), 'utf8'));
  return Object.assign({}, raw.page, {'@rules': raw.rules, '@months': raw.months, '@days': raw.days, '@tails': raw.tails, '@th': raw.headers});
}

async function builtInPacksTranslateTheRoomLinesAndTheSplitTexts() {
  for (const lang of ['tr', 'de', 'es', 'fr']) {
    //given
    const pack = builtIn(lang);
    const {w, doc, errors} = await boot([[lang, pack, true]], lang);
    const room = {id: 'investigator-room-0', name: 'Room 1', takes: 'questions, Investigate clicks and deploy assessments', pod: null, run: null};
    const cards = {id: 'investigator', name: 'Cards', takes: 'new and escalated cards, and the queue when the rooms are full', pod: null, run: null};
    const other = {id: 'investigator-room-9', name: 'Room 9', takes: 'reports', pod: null, run: null};

    //when
    const box = await shown(doc, w.invRoom(room) + w.invRoom(cards) + w.invRoom(other) + '<div class="words"><button type="button">View</button><span class="tag">Open</span><button type="button" class="int-entry-change">Change\u2026</button><span>Change</span></div>');
    const lines = [...box.querySelectorAll('.inv-meta')].map((p) => p.textContent);
    const words = [...box.querySelectorAll('.words > *')].map((e) => e.textContent);

    //then
    assert.deepStrictEqual(lines, [pack['Takes questions, Investigate clicks and deploy assessments.'], pack['Takes new and escalated cards, and the queue when the rooms are full.'],
      pack['@rules'].find((r) => r[0] === '^Takes (.+)\\.$')[1].replace('$1', 'reports')], lang);
    assert.deepStrictEqual(words, [pack['View'], pack['Open'], pack['Change\u2026'], pack['Change']], lang);
    assert.notStrictEqual(pack['View'], pack['Open'], lang + ': the View button and the Open status are told apart');
    assert.notStrictEqual(pack['Change\u2026'], pack['Change'], lang + ': the Change button and the Change header are told apart');
    assert.ok(!lines.concat(words).some((s) => /\bTakes\b/.test(s)), lang);
    assert.deepStrictEqual(errors, []);
    w.close();
  }
  console.log('PASS the built-in packs translate both room lines, fall back to the Takes rule, and keep the View/Open and Change button/header texts apart');
}

async function rulesTranslateShapedText() {
  //given
  const {w, doc, errors} = await boot([['de', {'@rules': [['^Seen before: (\\d+) times$', 'Schon $1 mal gesehen']]}]], 'de');

  //when
  const box = await shown(doc, '<p>Seen before: 3 times</p><p>Seen before: often</p>');

  //then
  assert.strictEqual(w.tx('Seen before: 3 times'), 'Schon 3 mal gesehen');
  assert.strictEqual(w.tx('  Seen before: 12 times '), '  Schon 12 mal gesehen ', 'surrounding spaces are kept');
  assert.deepStrictEqual([...box.querySelectorAll('p')].map((p) => p.textContent), ['Schon 3 mal gesehen', 'Seen before: often']);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a page rule turns a shaped English text into the pack language, with its groups put back');
}

async function monthsAreMappedInShapesAndRules() {
  //given
  const pack = {'Last seen {0} {M} {1}': 'Zuletzt {0}. {M} {1}', '@months': {'Mar': 'Mrz'}, '@rules': [['^Stopped (.+)$', 'Gestoppt $1']]};
  const {w, errors} = await boot([['de', pack]], 'de');

  //then
  assert.strictEqual(w.tx('Last seen 3 Mar 10:00'), 'Zuletzt 3. Mrz 10:00');
  assert.strictEqual(w.tx('Last seen 3 Apr 10:00'), 'Zuletzt 3. Apr 10:00', 'a month the pack does not name stays English');
  assert.strictEqual(w.tx('Stopped 3 Mar 10:00'), 'Gestoppt 3 Mrz 10:00');
  assert.strictEqual(w.tx('Marching on'), 'Marching on', 'only whole month names count');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS English month names become {M} in a shape and are mapped through @months, in shapes and in rule output');
}

async function daysAreMappedLikeMonths() {
  //given
  const pack = {'Next run {D} {0}': 'Naechster Lauf {D} {0}', '@days': {'Mon': 'Mo', 'Tue': 'Di'}, '@months': {'Mar': 'Mrz'}, '@rules': [['^Quiet until (.+)$', 'Ruhig bis $1']]};
  const {w, errors} = await boot([['de', pack]], 'de');

  //then
  assert.strictEqual(w.tx('Next run Mon 10:00'), 'Naechster Lauf Mo 10:00');
  assert.strictEqual(w.tx('Quiet until Tue 3 Mar'), 'Ruhig bis Di 3 Mrz');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS English day names become {D} in a shape and are mapped through @days, like months');
}

async function tailsKeepTheTranslatedHead() {
  //given
  const pack = {'Resolved': 'Erledigt', '@tails': [[DOT + 'auto-resolved', DOT + 'automatisch']]};
  const {w, doc, errors} = await boot([['de', pack]], 'de');

  //when
  const box = await shown(doc, '<span>Resolved' + DOT + 'auto-resolved</span>');

  //then
  assert.strictEqual(w.tx('Resolved' + DOT + 'auto-resolved'), 'Erledigt' + DOT + 'automatisch');
  assert.strictEqual(box.textContent, 'Erledigt' + DOT + 'automatisch');
  assert.strictEqual(w.tx('Muted' + DOT + 'auto-resolved'), 'Muted' + DOT + 'auto-resolved', 'a head the pack does not know leaves the text alone');
  assert.strictEqual(w.tx(DOT.trim() + ' auto-resolved'), DOT.trim() + ' auto-resolved');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a tail translates its known head and swaps the suffix');
}

async function tableHeadersUseTheirOverride() {
  //given
  const {w, doc, errors} = await boot([['de', {'@th': {'Requests': 'Anfragen'}}]], 'de');

  //when
  const box = await shown(doc, '<table><thead><tr><th>Requests</th><th><button type="button">Requests</button></th></tr></thead><tbody><tr><td>Requests</td></tr></tbody></table>');

  //then
  assert.deepStrictEqual([...box.querySelectorAll('th')].map((th) => th.textContent), ['Anfragen', 'Anfragen']);
  assert.strictEqual(box.querySelector('td').textContent, 'Requests');
  assert.strictEqual(w.tx('Requests', 'th'), 'Anfragen');
  assert.strictEqual(w.tx('Requests'), 'Requests');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS @th applies to table header text only, never to a cell');
}

async function aBadRuleIsSkippedNotFatal() {
  //given
  const pack = {'@rules': [['(', 'x'], ['^Seen before: (\\d+) times$', 'Schon $1 mal gesehen']]};
  const {w, doc, errors, warned} = await boot([['de', pack]], 'de');

  //when
  const first = w.tx('Seen before: 3 times');
  w.eval(snippet('de', {'@rules': [['^Gone (\\d+)$', 'Weg $1']]}));
  const merged = w.tx('Gone 3');
  const again = w.tx('Seen before: 4 times');
  const box = await shown(doc, '<p>Seen before: 5 times</p>');

  //then
  assert.strictEqual(first, 'Schon 3 mal gesehen');
  assert.strictEqual(merged, 'Weg 3', 'a pack merged after the first translation is compiled in');
  assert.strictEqual(w.SKOPAX_I18N.de['@rules'].length, 3);
  assert.strictEqual(again, 'Schon 4 mal gesehen');
  assert.strictEqual(box.textContent, 'Schon 5 mal gesehen');
  assert.strictEqual(warned.filter((t) => t.includes('(')).length, 1, 'one warning for the bad pattern, even after the rules are compiled again: ' + JSON.stringify(warned));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a rule the browser cannot compile is skipped with one warning and the later rules still apply');
}

async function aRuleIsAppliedOnceToANodeAddedLater() {
  //given
  const {w, doc, errors} = await boot([['de', {'@rules': [['^(x{1,5})$', '$1x']]}]], 'de');

  //when
  const box = await shown(doc, '<p>x</p><button type="button" title="x">b</button>');
  await wait(50);

  //then
  assert.strictEqual(box.querySelector('p').textContent, 'xx');
  assert.strictEqual(box.querySelector('button').getAttribute('title'), 'xx');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the page never translates its own translation again, in text and in attributes');
}

async function aSelfMatchingRuleLetsThePageSettle() {
  //given
  const {w, doc, errors} = await boot([['de', {'@rules': [['^Total (.+)$', 'Total : $1']]}]], 'de');

  //when
  const box = await shown(doc, '<p>Total 5</p><button type="button" title="Total 5">b</button>');
  const once = [box.querySelector('p').textContent, box.querySelector('button').getAttribute('title')];
  box.querySelector('p').firstChild.nodeValue = 'Total 6';
  box.querySelector('button').setAttribute('title', 'Total 7');
  await wait(20);
  const changed = [box.querySelector('p').textContent, box.querySelector('button').getAttribute('title')];
  await wait(50);
  const later = [box.querySelector('p').textContent, box.querySelector('button').getAttribute('title')];

  //then
  assert.deepStrictEqual(once, ['Total : 5', 'Total : 5']);
  assert.deepStrictEqual(changed, ['Total : 6', 'Total : 7'], 'a new English value is translated once');
  assert.deepStrictEqual(later, changed, 'and then the page settles');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a rule whose output matches it again is applied once, so the page settles');
}

async function namesAreMappedOnlyInsideCapturedGroups() {
  //given
  const pack = {'@days': {'Mon': 'lun.'}, '@months': {'Mar': 'mars'}, '@rules': [['^Board: (.+)$', 'Mon tableau: $1'], ['^Due in (\\d+)$', 'Mar $1 jours']]};
  const {w, errors} = await boot([['fr', pack]], 'fr');

  //then
  assert.strictEqual(w.tx('Board: Mon 3 Mar'), 'Mon tableau: lun. 3 mars');
  assert.strictEqual(w.tx('Due in 4'), 'Mar 4 jours', 'the translated text of a rule is never remapped');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS month and day names are mapped inside the captured groups of a rule, never in its replacement text');
}

async function aFlatKeyWithADayNameStillMatches() {
  //given
  const pack = {'Every Mon at {0}': 'Jeden Montag um {0}', 'Next run {D} {0}': 'Naechster Lauf {D} {0}', '@days': {'Mon': 'Mo'}};
  const {w, errors} = await boot([['de', pack]], 'de');

  //then
  assert.strictEqual(w.tx('Every Mon at 10'), 'Jeden Montag um 10', 'the number-only key comes first');
  assert.strictEqual(w.tx('Next run Mon 10:00'), 'Naechster Lauf Mo 10:00', 'then the key with names');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS an old flat key that spells a day or month name still matches before the {M} / {D} shape');
}

async function evilPackValuesAreText() {
  //given
  const pack = {'Open': EVIL, 'Seen {0}': EVIL + '{0}', '@rules': [['^Gone (\\d+)$', EVIL + '$1'], ['^Stopped (.+)$', 'Gestoppt $1']], '@tails': [[DOT + 'x', EVIL]], '@th': {'Requests': EVIL}, '@months': {'Mar': EVIL}};
  const {w, doc, errors, alerts} = await boot([['de', pack]], 'de');

  //when
  const box = await shown(doc, '<p>Open</p><p>Seen 2</p><p>Gone 7</p><p>Open' + DOT + 'x</p><p>Stopped 3 Mar</p><table><tr><th>Requests</th></tr></table><button type="button" title="Open">b</button>');
  await wait(50);

  //then
  assert.deepStrictEqual([...box.querySelectorAll('p')].map((p) => p.textContent), [EVIL, EVIL + '2', EVIL + '7', EVIL + EVIL, 'Gestoppt 3 ' + EVIL]);
  assert.strictEqual(box.querySelector('th').textContent, EVIL);
  assert.strictEqual(box.querySelector('button').getAttribute('title'), EVIL);
  assert.strictEqual(box.querySelectorAll('img').length, 0);
  assert.strictEqual(doc.querySelectorAll('img[src="x"]').length, 0);
  assert.deepStrictEqual(alerts, []);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS pack values from keys, shapes, rules, tails, @months and @th land as text and attribute values, never as markup');
}

async function twoPluginPacksMergeInLoadOrder() {
  //given
  const one = {'Open': 'Offen', 'Board': 'Tafel', '@rules': [['^Seen (\\d+)$', 'Gesehen $1'], ['^Stopped (.+)$', 'Gestoppt $1']], '@tails': [[DOT + 'auto-resolved', DOT + 'automatisch']],
    '@months': {'Mar': 'Mrz'}, '@days': {'Mon': 'Mo'}, '@th': {'Requests': 'Anfragen'}};
  const two = {'Open': 'Auf', '@rules': [['^Seen (\\d+)$', 'Zweimal $1'], ['^Gone (\\d+)$', 'Weg $1']], '@tails': [[DOT + 'by investigator', DOT + 'Ermittler']],
    '@months': {'Dec': 'Dez'}, '@days': {'Tue': 'Di'}, '@th': {'Errors': 'Fehler'}};
  const {w, doc, errors} = await boot([['de', one], ['de', two]], 'de');

  //when
  const box = await shown(doc, '<table><tr><th>Requests</th><th>Errors</th></tr></table>');

  //then
  assert.strictEqual(w.tx('Open'), 'Auf', 'a later plug-in wins a plain key');
  assert.strictEqual(w.tx('Board'), 'Tafel');
  assert.strictEqual(w.tx('Seen 3'), 'Gesehen 3', 'the first plug-in rules come first');
  assert.strictEqual(w.tx('Gone 4'), 'Weg 4');
  assert.strictEqual(w.tx('Stopped 3 Mar Tue'), 'Gestoppt 3 Mrz Di');
  assert.strictEqual(w.tx('Stopped 9 Dec Mon'), 'Gestoppt 9 Dez Mo');
  assert.strictEqual(w.tx('Board' + DOT + 'auto-resolved'), 'Tafel' + DOT + 'automatisch');
  assert.strictEqual(w.tx('Board' + DOT + 'by investigator'), 'Tafel' + DOT + 'Ermittler');
  assert.deepStrictEqual([...box.querySelectorAll('th')].map((th) => th.textContent), ['Anfragen', 'Fehler']);
  assert.deepStrictEqual(w.SKOPAX_I18N.de['@rules'].length, 4);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the served packs of two plug-ins merge in load order: rules and tails add up, months, days and headers merge, later keys win');
}

async function aPluginPackWinsTextByTextOverTheCorePack() {
  //given
  const base = {'Open': 'Core open', 'Board': 'Core board', '@rules': [['^Seen (\\d+)$', 'Core seen $1'], ['^Gone (.+)$', 'Core gone $1']],
    '@tails': [[DOT + 'auto-resolved', DOT + 'core auto']], '@months': {'Mar': 'CoreMar', 'Dec': 'CoreDec'}, '@th': {'Requests': 'Core requests'}};
  const one = {'Open': 'Plugin open', '@rules': [['^Seen (\\d+)$', 'Plugin seen $1']], '@tails': [[DOT + 'auto-resolved', DOT + 'plugin auto']], '@months': {'Mar': 'PlugMar'}};
  const two = {'@rules': [['^Seen (\\d+)$', 'Second seen $1'], ['^Left (\\d+)$', 'Second left $1']]};
  const {w, errors} = await boot([['de', base, true], ['de', one], ['de', two]], 'de');

  //then
  assert.strictEqual(w.tx('Open'), 'Plugin open', 'a plug-in key wins over the core key');
  assert.strictEqual(w.tx('Board'), 'Core board', 'a core key the plug-in leaves out stays');
  assert.strictEqual(w.tx('Seen 3'), 'Plugin seen 3', 'plug-in rules come before core rules, in plug-in load order');
  assert.strictEqual(w.tx('Gone 4'), 'Core gone 4');
  assert.strictEqual(w.tx('Left 5'), 'Second left 5');
  assert.strictEqual(w.tx('Board' + DOT + 'auto-resolved'), 'Core board' + DOT + 'plugin auto', 'a plug-in tail comes before the core tail');
  assert.strictEqual(w.tx('Gone 1 Mar'), 'Core gone 1 PlugMar');
  assert.strictEqual(w.tx('Gone 1 Dec'), 'Core gone 1 CoreDec');
  assert.strictEqual(w.tx('Requests', 'th'), 'Core requests');
  assert.deepStrictEqual(Array.from(w.SKOPAX_I18N.de['@rules'], (r) => r[1]), ['Plugin seen $1', 'Second seen $1', 'Second left $1', 'Core seen $1', 'Core gone $1']);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a plug-in pack wins text by text over the core pack that loads first: keys, rules, tails and names');
}

async function theStepsRunInTheirOrder() {
  //given
  const pack = {'Done 3': 'Genau', 'Done {0}': 'Form {0}', 'Resolved': 'Erledigt', 'Resolved {0}': 'Form {0}', '@tails': [[' 5', ' fuenf']], '@rules': [['^Done (\\d+)$', 'Regel $1'], ['^Late (\\d+)$', 'Regel $1']]};
  const {w, errors} = await boot([['de', pack]], 'de');

  //then
  assert.strictEqual(w.tx('Done 3'), 'Genau', 'the exact key first');
  assert.strictEqual(w.tx('Resolved 5'), 'Erledigt fuenf', 'then a tail');
  assert.strictEqual(w.tx('Done 4'), 'Form 4', 'then the shape');
  assert.strictEqual(w.tx('Late 4'), 'Regel 4', 'then the rules');
  assert.strictEqual(w.tx('Nothing here'), 'Nothing here', 'else unchanged');
  assert.strictEqual(w.tx('@rules'), '@rules', 'a reserved key is never a translation');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the exact key wins, then a tail, then the shape, then the first matching rule, else the text stays');
}

async function aRegionalTagPicksItsOwnPackBeforeThePrimaryLanguage() {
  //given
  const both = [['pt', {'Open': 'Aberto'}], ['pt-BR', {'Open': 'Aberto BR'}]];
  const exact = await boot(both, 'pt-BR');
  const primary = await boot(both, 'pt-PT');
  const browser = await boot(both, '', 'pt-BR');
  const onlyRegional = await boot([['pt-BR', {'Open': 'Aberto BR'}]], 'pt');
  const lower = await boot([['pt-br', {'Open': 'Aberto br'}]], 'pt-BR');
  const lowerBrowser = await boot([['pt-br', {'Open': 'Aberto br'}]], '', 'pt-BR');

  //then
  assert.strictEqual(exact.w.tx('Open'), 'Aberto BR');
  assert.strictEqual(exact.doc.documentElement.lang, 'pt-BR');
  assert.strictEqual(primary.w.tx('Open'), 'Aberto', 'with no pack for the exact tag, the primary language pack serves');
  assert.strictEqual(primary.doc.documentElement.lang, 'pt');
  assert.strictEqual(browser.w.tx('Open'), 'Aberto BR');
  assert.strictEqual(onlyRegional.w.tx('Open'), 'Aberto BR', 'a plain language tag reaches a regional pack of that language, as in the chat');
  assert.strictEqual(lower.w.tx('Open'), 'Aberto br', 'tags compare without case');
  assert.strictEqual(lower.doc.documentElement.lang, 'pt-br', 'the page names the pack it picked');
  assert.strictEqual(lowerBrowser.w.tx('Open'), 'Aberto br');
  assert.deepStrictEqual(exact.errors.concat(primary.errors, browser.errors, onlyRegional.errors, lower.errors, lowerBrowser.errors), []);
  [exact, primary, browser, onlyRegional, lower, lowerBrowser].forEach((b) => b.w.close());
  console.log('PASS the page picks the pack of the exact language tag first and of its primary language second');
}

async function theBrowserLanguagesAreWalkedInOrderAsTheChatDoes() {
  //given
  const de = [['de', {'Open': 'Offen'}]];
  const ptBR = [['pt-BR', {'Open': 'Aberto BR'}]];
  const second = await boot(de, '', ['fr-FR', 'de']);
  const regional = await boot(ptBR, '', ['pt-PT']);
  const englishFirst = await boot(de, '', ['en-US', 'de']);
  const chosenEnglish = await boot(de, 'en', ['de']);
  const chosenWithoutPack = await boot(de, 'fr', ['fr-FR', 'de-AT']);

  //then
  assert.strictEqual(second.w.tx('Open'), 'Offen', 'fr has no pack, so the next browser language, de, serves');
  assert.strictEqual(second.doc.documentElement.lang, 'de');
  assert.strictEqual(regional.w.tx('Open'), 'Aberto BR', 'pt-PT reaches the pt-BR pack');
  assert.strictEqual(regional.doc.documentElement.lang, 'pt-BR');
  assert.strictEqual(englishFirst.w.tx('Open'), 'Open', 'an English entry ends the walk, so de after it is not used');
  assert.strictEqual(chosenEnglish.w.tx('Open'), 'Open', 'English chosen in Settings beats a de browser');
  assert.strictEqual(chosenWithoutPack.w.tx('Open'), 'Offen', 'a chosen language with no pack falls through to the browser languages');
  const all = [second, regional, englishFirst, chosenEnglish, chosenWithoutPack];
  assert.deepStrictEqual(all.flatMap((b) => b.errors), []);
  all.forEach((b) => b.w.close());
  console.log('PASS the page walks the chosen language and then every browser language in order, as the chat does: exact tag, then the plain language or a regional pack of it, English ending the walk');
}

async function aRegionalTagIsKeptInTheLanguageCookie() {
  //given
  const regional = await boot([], '', '', {lang: 'pt-BR'});
  const bad = await boot([], '', '', {lang: 'pt_BR'});

  //then
  assert.ok(/(?:^|;\s*)skopax_lang=pt-BR(?:;|$)/.test(regional.doc.cookie), 'the language choice keeps a regional tag: ' + regional.doc.cookie);
  assert.ok(!/skopax_lang=/.test(bad.doc.cookie), 'a malformed tag is not stored: ' + bad.doc.cookie);
  assert.deepStrictEqual(regional.errors.concat(bad.errors), []);
  regional.w.close(); bad.w.close();
  console.log('PASS the language choice stores any tag the one language-tag pattern accepts, regional tags included');
}

(async () => {
  await rulesTranslateShapedText();
  await monthsAreMappedInShapesAndRules();
  await daysAreMappedLikeMonths();
  await tailsKeepTheTranslatedHead();
  await tableHeadersUseTheirOverride();
  await aBadRuleIsSkippedNotFatal();
  await aRuleIsAppliedOnceToANodeAddedLater();
  await aSelfMatchingRuleLetsThePageSettle();
  await namesAreMappedOnlyInsideCapturedGroups();
  await aFlatKeyWithADayNameStillMatches();
  await evilPackValuesAreText();
  await twoPluginPacksMergeInLoadOrder();
  await aPluginPackWinsTextByTextOverTheCorePack();
  await theStepsRunInTheirOrder();
  await aRegionalTagPicksItsOwnPackBeforeThePrimaryLanguage();
  await theBrowserLanguagesAreWalkedInOrderAsTheChatDoes();
  await aRegionalTagIsKeptInTheLanguageCookie();
  await builtInPacksTranslateTheRoomLinesAndTheSplitTexts();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
