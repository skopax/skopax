const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const NEW = 'log|checkout-service|ERROR|boom', PROG = 'log|inventory-service|ERROR|x', DONE = 'log|shop-web|WARN|y', FREE = 'log|cart-service|WARN|z', OLD = 'log|old-service|WARN|gone', HIGH = 'log|search-service|ERROR|h';
const TAGS = [['data', 'Data'], ['seo', 'SEO']].map(([id, label]) => ({id, label}));

function cards() {
  const card = (key, more) => Object.assign({key, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'ERROR lines', src: 'logs', sig: 'log', now: 1, firstSeen: iso(90), lastSeen: iso(1)}, more || {});
  const tag = (id) => ({id, by: 'rule', at: iso(5)});
  return [card(NEW, {sev: 'crit', num: 153, owner: 'alice@example.com', tags: [tag('data')]}), card(PROG, {triage: 'progress', owner: 'Carol Diaz', num: 149, tags: [tag('seo')]}),
    card(DONE, {triage: 'handled', status: 'cleared', clearedAt: iso(30), num: 140, owner: 'Paul Stone'}), card(FREE, {num: 141}),
    card(OLD, {status: 'cleared', clearedAt: iso(3 * 24 * 60), num: 120, lastSeen: iso(60 * 60)}), card(HIGH, {sev: 'high', num: 150, owner: 'Paul Stone'})];
}
function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)}, anomalies: cards(),
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(hash, lang, phone) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE, {view: 'board'}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state())), {
    url: 'https://skopax.example.com/' + (hash || '#t=anoms/open'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      if (lang) {
        const raw = JSON.parse(fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'locale', lang, 'page.json'), 'utf8'));
        w.SKOPAX_I18N = {[lang]: Object.assign({}, raw.page, {'@rules': raw.rules, '@months': raw.months, '@days': raw.days, '@tails': raw.tails, '@th': raw.headers})};
        w.document.cookie = 'skopax_lang=' + lang + '; path=/';
      }
      if (phone) w.matchMedia = (q) => ({matches: /max-width: 640px/.test(q), media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}});
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        p = String(p);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', operator: true, teams: [], tags: TAGS});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/state')) return ok(state());
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(450);
  return {w: dom.window, doc: dom.window.document, errors};
}

const pack = (lang) => JSON.parse(fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'locale', lang, 'page.json'), 'utf8')).page;
const grp = (doc, g) => doc.querySelector('#bf-lists .fb-w[data-g="' + g + '"]');
const btn = (doc, g) => grp(doc, g).querySelector('.fb-b');
const clearX = (doc, g) => grp(doc, g).querySelector('.fb-x');
const items = (doc, g) => [...grp(doc, g).querySelectorAll('.fb-i')];
const item = (doc, g, id) => items(doc, g).find((b) => (b.dataset.f || b.dataset.type) === id);
const lbl = (b) => b.querySelector('.lbl').textContent;
const count = (b) => b.querySelector('.count').textContent;
const checked = (doc, g) => items(doc, g).filter((b) => b.getAttribute('aria-checked') === 'true').map((b) => b.dataset.f || b.dataset.type);
const said = (doc, g) => btn(doc, g).textContent.replace(/\s+/g, ' ').trim();
const shown = (doc) => [...doc.querySelectorAll('#anoms .tile')].map((t) => t.dataset.key).sort();
const css = () => fs.readFileSync(TEMPLATE, 'utf8');
const rule = (text, sel) => { const m = new RegExp('(?:^|[}\\n])\\s*' + sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ' \\{([^}]*)\\}').exec(text); return m ? m[1] : null; };
const hash = (w) => decodeURIComponent(w.location.hash);

async function statusHasNoSeverityAndSeverityIsItsOwnMenuWithCounts() {
  //given
  const {w, doc, errors} = await boot();

  //when
  const status = items(doc, 'status').map(lbl);
  const sections = doc.querySelectorAll('#fb-m-status .fb-sec').length;
  const severity = items(doc, 'sev').map((b) => [b.dataset.type, lbl(b), count(b)]);
  const names = [...doc.querySelectorAll('#bf-lists .fb-b')].map((b) => b.textContent.replace(/\s+/g, ' ').trim());

  //then
  assert.deepStrictEqual(status, ['All', 'Open', 'In progress', 'Done', 'Muted', 'Stopped firing', 'Investigating', 'Investigated', 'Recurring', 'Archived', 'Closed']);
  assert.strictEqual(sections, 0, 'one list of states needs no section heading');
  assert.deepStrictEqual(severity, [['sev:all', 'All', '4'], ['sev:crit', 'Critical', '1'], ['sev:high', 'High', '1'], ['sev:warn', 'Warning', '2']]);
  assert.deepStrictEqual(names, ['Status: Open 4', 'Severity', 'Tags', 'Assignee', 'Type']);
  assert.deepStrictEqual(checked(doc, 'sev'), ['sev:all']);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'Status lists only states, Severity is its own menu with All, Critical, High, Warning and counts, and Status reads Open with its count';
}

async function theTwoMenusCombineWithEachOtherAndWithTheOthers() {
  //given
  const {w, doc, errors} = await boot();
  const pick = (g, id) => { doc.querySelector('#bf-lists .fb-w[data-g="' + g + '"] .fb-b').click(); item(doc, g, id).click(); };

  //when
  pick('sev', 'sev:crit');
  const crit = {cards: shown(doc), hash: hash(w), said: said(doc, 'sev'), x: !clearX(doc, 'sev').hidden, status: said(doc, 'status'), checked: checked(doc, 'sev')};
  pick('sev', 'sev:high');
  const high = {cards: shown(doc), checked: checked(doc, 'sev')};
  pick('status', 'taken');
  const doneHigh = {cards: shown(doc), hash: hash(w)};
  pick('sev', 'sev:warn');
  const doneWarn = {cards: shown(doc), hash: hash(w), status: said(doc, 'status')};
  pick('status', 'open');
  doc.querySelector('#bf-main [data-type="who:me"]').click();
  pick('sev', 'sev:crit');
  const mine = {cards: shown(doc), hash: hash(w)};
  pick('sev', 'sev:all');
  const all = {cards: shown(doc), hash: hash(w), said: said(doc, 'sev')};
  pick('sev', 'sev:crit');
  clearX(doc, 'sev').click();
  const cleared = {hash: hash(w), cards: shown(doc)};

  //then
  assert.deepStrictEqual(crit, {cards: [NEW], hash: '#t=anoms/open/sev:crit', said: 'Severity: Critical', x: true, status: 'Status: Open 4', checked: ['sev:crit']});
  assert.deepStrictEqual(high, {cards: [HIGH], checked: ['sev:high']}, 'a second severity replaces the first');
  assert.deepStrictEqual(doneHigh, {cards: [], hash: '#t=anoms/taken/sev:high'});
  assert.deepStrictEqual(doneWarn, {cards: [DONE], hash: '#t=anoms/taken/sev:warn', status: 'Status: Done 1'});
  assert.deepStrictEqual(mine, {cards: [NEW], hash: '#t=anoms/open/who:me+sev:crit'});
  assert.deepStrictEqual(all, {cards: [NEW], hash: '#t=anoms/open/who:me', said: 'Severity'}, 'All drops the severity and keeps Mine');
  assert.deepStrictEqual(cleared, {hash: '#t=anoms/open/who:me', cards: [NEW]});
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a pick in Status and one in Severity narrow each other and Mine, a second severity replaces the first, All and the cross drop it';
}

async function thereAreNoStatusChipsAndOldLinksOpenWithTheMenuValues() {
  //given
  const plain = await boot();

  //when
  const chips = [...plain.doc.querySelectorAll('#bf-main .qf-c')].map(lbl);
  const stateChips = plain.doc.querySelectorAll('#bf-main [data-f]').length;
  const old = {};
  for (const [name, h] of [['crit', '#t=anoms/crit'], ['done', '#t=anoms/taken'], ['critMine', '#t=anoms/crit/who:me'], ['high', '#t=anoms/high/tag%3Adata'], ['junk', '#t=anoms/open/sev%3Anope']]) {
    const t = await boot(h);
    old[name] = {status: said(t.doc, 'status'), sev: checked(t.doc, 'sev'), cards: shown(t.doc), hash: hash(t.w), errors: t.errors};
    t.w.close();
  }

  //then
  assert.deepStrictEqual(chips, ['Mine']);
  assert.strictEqual(stateChips, 0, 'no Open, Critical or Done chip');
  assert.deepStrictEqual(old.crit, {status: 'Status: Open 4', sev: ['sev:crit'], cards: [NEW], hash: '#t=anoms/open/sev:crit', errors: []});
  assert.deepStrictEqual(old.done, {status: 'Status: Done 1', sev: ['sev:all'], cards: [DONE], hash: '#t=anoms/taken', errors: []});
  assert.deepStrictEqual(old.critMine, {status: 'Status: Open 4', sev: ['sev:crit'], cards: [NEW], hash: '#t=anoms/open/who:me+sev:crit', errors: []});
  assert.deepStrictEqual(old.high, {status: 'Status: Open 4', sev: ['sev:high'], cards: [], hash: '#t=anoms/open/tag:data+sev:high', errors: []});
  assert.deepStrictEqual(old.junk.sev, ['sev:all'], 'an unknown severity in a link is dropped');
  plain.w.close();
  return 'the Open, Critical and Done chips are gone, Mine stays, and old chip links open with the matching menu values';
}

async function aTipShowsBesideTheMenuAndNeverOverAnOption() {
  //given
  const {w, doc, errors} = await boot();
  let popLeft = 100;
  const at = (l, t, r, b) => ({left: l, top: t, right: r, bottom: b, width: r - l, height: b - t, x: l, y: t});
  w.Element.prototype.getBoundingClientRect = function () {
    if (this.classList.contains('fb-pop')) return at(popLeft, 80, popLeft + 240, 320);
    if (this.classList.contains('fb-i')) { const n = [...this.parentNode.children].indexOf(this); return at(popLeft + 6, 86 + n * 30, popLeft + 234, 116 + n * 30); }
    if (this.id === 'tipbox') return at(parseFloat(this.style.left), parseFloat(this.style.top), parseFloat(this.style.left) + 200, parseFloat(this.style.top) + 40);
    return at(0, 0, 0, 0);
  };
  const box = doc.getElementById('tipbox');
  Object.defineProperty(box, 'offsetWidth', {value: 200});
  Object.defineProperty(box, 'offsetHeight', {value: 40});
  const meets = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
  async function hover(g, id) {
    btn(doc, g).click();
    const it = item(doc, g, id);
    it.dispatchEvent(new w.MouseEvent('pointerover', {bubbles: true}));
    await wait(160);
    const tip = box.getBoundingClientRect();
    return {shown: !box.hidden, text: box.textContent, own: it.querySelector('.fb-d').textContent, over: [grp(doc, g).querySelector('.fb-pop'), ...items(doc, g)].filter((n) => meets(tip, n.getBoundingClientRect())).length, tip, pop: grp(doc, g).querySelector('.fb-pop').getBoundingClientRect(), count: doc.querySelectorAll('.tipbox:not([hidden])').length};
  }

  //when
  const right = await hover('status', 'progress');
  popLeft = 900;
  const left = await hover('sev', 'sev:high');
  popLeft = 100;
  const tags = await hover('tags', 'tag:data');
  const it = item(doc, 'status', 'muted');
  btn(doc, 'status').click();
  it.focus();
  const focused = {shown: !box.hidden, text: box.textContent, own: it.querySelector('.fb-d').textContent, over: [...items(doc, 'status')].filter((n) => meets(box.getBoundingClientRect(), n.getBoundingClientRect())).length};

  //then
  assert.ok(right.shown && right.text === right.own && right.text.length > 10, right.text);
  assert.strictEqual(right.over, 0, 'the tip does not touch the menu or any option');
  assert.ok(right.tip.left >= right.pop.right, 'with room it opens to the right of the menu');
  assert.ok(left.shown && left.over === 0 && left.tip.right <= left.pop.left, 'with no room on the right it opens to the left');
  assert.ok(tags.shown && tags.over === 0 && tags.tip.left >= tags.pop.right, 'Tags uses the same placement');
  assert.deepStrictEqual([right.count, left.count, tags.count], [1, 1, 1], 'one tooltip at a time');
  assert.deepStrictEqual(focused, {shown: true, text: focused.own, own: focused.own, over: 0}, 'keyboard focus shows the same text beside the menu');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a filter-menu tip opens to the right of the menu, to the left when there is no room, never over an option, one at a time, also on focus and in Tags';
}

async function everyOptionHasItsExplanationAsADescription() {
  //given
  const {w, doc, errors} = await boot();

  //when
  const all = ['status', 'sev', 'tags', 'who', 'type'].flatMap((g) => items(doc, g));
  const bad = all.filter((b) => { const d = doc.getElementById(b.getAttribute('aria-describedby') || ''); return !d || !d.textContent || d.textContent !== b.getAttribute('title') || !b.contains(d); });
  const ids = new Set(all.map((b) => b.getAttribute('aria-describedby')));
  const named = all.filter((b) => { const ids = (b.getAttribute('aria-labelledby') || '').split(' ').map((i) => doc.getElementById(i)); return ids.length !== 2 || !ids[0] || !ids[1] || !ids[0].classList.contains('lbl') || !ids[1].classList.contains('count') || !b.contains(ids[0]) || !b.contains(ids[1]) || ids.some((n) => n.closest('.fb-d')); });

  //then
  assert.ok(all.length > 20);
  assert.deepStrictEqual(named.map(lbl), [], 'the name is the label and the count only, so the explanation is not read twice');
  assert.deepStrictEqual(bad.map(lbl), []);
  assert.strictEqual(ids.size, all.length, 'every description has its own id');
  assert.match(rule(css(), '.fb-d'), /clip:\s*rect\(0 0 0 0\)/, 'on a desktop the description is for screen readers only');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'each option carries its explanation as a described-by line for screen readers';
}

async function onAPhoneTheExplanationIsASecondLineAndNoTipShows() {
  //given
  const {w, doc, errors} = await boot('#t=anoms/open', null, true);
  const text = css();
  const at = text.indexOf('@media (max-width: 640px), (max-height: 500px) and (pointer: coarse) {');
  const phone = text.slice(at, text.indexOf('\n}\n', at));

  //when
  btn(doc, 'sev').click();
  const it = item(doc, 'sev', 'sev:crit');
  it.dispatchEvent(new w.MouseEvent('pointerover', {bubbles: true}));
  it.focus();
  await wait(160);

  //then
  assert.strictEqual(doc.getElementById('tipbox').hidden, true, 'no floating tip on a phone');
  assert.ok(it.querySelector('.fb-d').textContent.length > 10);
  assert.match(rule(phone, '.fb-d'), /position:\s*static/);
  assert.match(rule(phone, '.fb-d'), /color:\s*var\(--muted\)/);
  assert.match(rule(phone, '.fb-i'), /flex-wrap:\s*wrap/);
  assert.strictEqual((text.match(/@media \(max-width: 640px\), \(max-height: 500px\) and \(pointer: coarse\) \{/g) || []).length, 1, 'the phone rules stay in the one main block');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'on a phone the explanation is a small muted second line under the option and the floating tip stays hidden';
}

async function theMenusAreTranslated() {
  //given
  const out = [];

  //when
  for (const l of ['de', 'es', 'fr', 'tr']) {
    const t = await boot('#t=anoms/open', l);
    const p = pack(l);
    out.push([l, said(t.doc, 'sev'), items(t.doc, 'sev').map(lbl), said(t.doc, 'status'), p, t.errors]);
    t.w.close();
  }

  //then
  out.forEach(([l, sev, labels, status, p, errors]) => {
    assert.strictEqual(sev, p['Severity'], l);
    assert.deepStrictEqual(labels, [p['All'], p['Critical'], p['High'], p['Warning']], l);
    assert.strictEqual(status, p['Status'] + ': ' + p['Open'] + ' 4', l);
    assert.deepStrictEqual(errors, []);
  });
  return 'the Severity menu, its options and the Status button read in de, es, fr and tr';
}

async function everyExplanationReadsInThePageLanguage() {
  //given
  const en = await boot();
  const base = {};
  const groups = ['status', 'sev', 'tags', 'who', 'type'];
  groups.forEach((g) => items(en.doc, g).forEach((b) => { base[g + '/' + (b.dataset.f || b.dataset.type)] = b.getAttribute('title'); }));
  const out = [];

  //when
  for (const l of ['de', 'es', 'fr', 'tr']) {
    const t = await boot('#t=anoms/open', l);
    const same = [], split = [];
    groups.forEach((g) => items(t.doc, g).forEach((b) => {
      const k = g + '/' + (b.dataset.f || b.dataset.type);
      if (b.getAttribute('title') === base[k]) same.push(k);
      if (b.getAttribute('title') !== b.querySelector('.fb-d').textContent) split.push(k);
    }));
    const p = pack(l);
    const tip = (g, id) => item(t.doc, g, id).getAttribute('title');
    out.push({l, same, split, errors: t.errors, got: [tip('sev', 'sev:crit'), tip('sev', 'sev:all'), tip('status', 'progress'), tip('who', 'who:none'), tip('tags', 'tag:data'), tip('status', 'open')], want: [
      p['cards of {0} severity'].replace('{0}', p['Critical']), p['cards of every severity'], p['opens Board filtered to {0}'].replace('{0}', p['In progress']),
      p['cards with no assignee'], p['cards tagged {0}: {1} to fix them'].replace('{0}', 'Data').replace('{1}', p['the data team has to change a pipeline, an Airbyte sync or a DWH table']),
      p['firing, or stopped with nobody acting on it yet'] + ' \u00b7 ' + p['opens Board filtered to {0}'].replace('{0}', p['Open'])]});
    t.w.close();
  }

  //then
  assert.ok(Object.keys(base).length > 20);
  out.forEach((o) => {
    assert.deepStrictEqual(o.same, [], o.l + ' leaves explanations in English');
    assert.deepStrictEqual(o.split, [], o.l + ' text matches the tooltip');
    assert.deepStrictEqual(o.errors, []);
    assert.deepStrictEqual(o.got, o.want, o.l);
  });
  return 'every option explanation in Status, Severity, Tags, Assignee and Type reads in de, es, fr and tr';
}

async function clearFiltersLabelAndTheFallbackAndTablet() {
  //given
  const {w, doc, errors} = await boot('#t=anoms/taken/sev%3Awarn');
  const label = () => doc.getElementById('clear-type').textContent;

  //when
  const sevOnly = label();
  doc.getElementById('clear-type').click();
  const after = {hash: hash(w), status: said(doc, 'status'), sev: said(doc, 'sev')};
  const typeOnly = await boot('#t=anoms/all/log');
  const typeLabel = typeOnly.doc.getElementById('clear-type').textContent;
  typeOnly.w.close();
  const tr = await boot('#t=anoms/taken/sev%3Awarn', 'tr');
  const trLabel = tr.doc.getElementById('clear-type').textContent;
  tr.w.close();

  //then
  assert.strictEqual(sevOnly, 'Clear filters \u203a');
  assert.deepStrictEqual(after, {hash: '#t=anoms/taken', status: 'Status: Done 1', sev: 'Severity'}, 'it clears every pick but Status');
  assert.strictEqual(typeLabel, 'Every type \u203a', 'a type alone keeps its own words');
  assert.strictEqual(trLabel, pack('tr')['Clear filters \u203a']);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the note button says Clear filters when the picks are not only a type, and clears everything but Status';
}

async function aTipWithNoRoomStaysInsideTheScreenAndATabletKeepsTheTip() {
  //given
  const {w, doc, errors} = await boot();
  const at = (l, t, r, b) => ({left: l, top: t, right: r, bottom: b, width: r - l, height: b - t, x: l, y: t});
  w.Element.prototype.getBoundingClientRect = function () {
    if (this.classList.contains('fb-pop')) return at(20, 500, 1000, 745);
    if (this.classList.contains('fb-i')) return at(26, 506, 994, 536);
    return at(0, 0, 0, 0);
  };
  const box = doc.getElementById('tipbox');
  Object.defineProperty(box, 'offsetWidth', {value: 200});
  Object.defineProperty(box, 'offsetHeight', {value: 40});
  btn(doc, 'status').click();

  //when
  item(doc, 'status', 'progress').dispatchEvent(new w.MouseEvent('pointerover', {bubbles: true}));
  await wait(160);
  const top = parseFloat(box.style.top);
  const query = w.eval('BOARD_Q');
  const text = css();
  const media = (width, height, coarse) => (q) => q.split(',').some((leg) => leg.split(' and ').every((c) => { const m = /\((max-width|max-height|pointer): *(\d+|coarse)(?:px)?\)/.exec(c.trim()); return m[1] === 'max-width' ? width <= Number(m[2]) : m[1] === 'max-height' ? height <= Number(m[2]) : coarse === (m[2] === 'coarse'); }));
  const phone = media(800, 400, true), tablet = media(1024, 768, true);
  const tabletBoot = await boot('#t=anoms/open');
  tabletBoot.w.matchMedia = (q) => ({matches: tablet(q), media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}});
  btn(tabletBoot.doc, 'status').click();
  item(tabletBoot.doc, 'status', 'progress').dispatchEvent(new tabletBoot.w.MouseEvent('pointerover', {bubbles: true}));
  await wait(160);
  const tabletShown = !tabletBoot.doc.getElementById('tipbox').hidden;
  tabletBoot.w.matchMedia = (q) => ({matches: phone(q), media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}});
  item(tabletBoot.doc, 'status', 'muted').dispatchEvent(new tabletBoot.w.MouseEvent('pointerover', {bubbles: true}));
  await wait(160);
  const landscapeShown = !tabletBoot.doc.getElementById('tipbox').hidden;

  //then
  assert.ok(top >= 8 && top + 40 <= 768 - 8, 'the fallback keeps the tip inside the screen: ' + top);
  assert.ok(text.includes('@media ' + query + ' {'), 'the phone rules and the tip rule use the same query');
  assert.strictEqual(phone(query) && !tablet(query), true, 'a landscape phone counts as a phone and a wide coarse tablet does not');
  assert.strictEqual(tabletShown, true, 'a wide tablet keeps the floating tip');
  assert.strictEqual(landscapeShown, false, 'a short coarse screen gets the second line instead');
  assert.deepStrictEqual(errors.concat(tabletBoot.errors), []);
  w.close(); tabletBoot.w.close();
  return 'with no room beside the menu the tip stays inside the screen, and only a narrow or short coarse screen swaps it for the second line';
}

(async () => {
  console.log('PASS ' + await statusHasNoSeverityAndSeverityIsItsOwnMenuWithCounts());
  console.log('PASS ' + await theTwoMenusCombineWithEachOtherAndWithTheOthers());
  console.log('PASS ' + await thereAreNoStatusChipsAndOldLinksOpenWithTheMenuValues());
  console.log('PASS ' + await aTipShowsBesideTheMenuAndNeverOverAnOption());
  console.log('PASS ' + await everyOptionHasItsExplanationAsADescription());
  console.log('PASS ' + await onAPhoneTheExplanationIsASecondLineAndNoTipShows());
  console.log('PASS ' + await theMenusAreTranslated());
  console.log('PASS ' + await everyExplanationReadsInThePageLanguage());
  console.log('PASS ' + await clearFiltersLabelAndTheFallbackAndTablet());
  console.log('PASS ' + await aTipWithNoRoomStaysInsideTheScreenAndATabletKeepsTheTip());
  process.exit(0);
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
