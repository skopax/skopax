const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const NEW = 'log|checkout-service|ERROR|boom', PROG = 'log|inventory-service|ERROR|x', DONE = 'log|shop-web|WARN|y', FREE = 'log|cart-service|WARN|z', OLD = 'log|old-service|WARN|gone';
const TAGS = [['data', 'Data'], ['seo', 'SEO'], ['billing', 'Billing'], ['mobile', 'Mobile'], ['search', 'Search'], ['growth', 'Growth'], ['payments', 'Payments'], ['web', 'Web'], ['ops', 'Ops'], ['legal', 'Legal']].map(([id, label]) => ({id, label}));

function cards(extra) {
  const card = (key, more) => Object.assign({key, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'ERROR lines', src: 'logs', sig: 'log', now: 1, firstSeen: iso(90), lastSeen: iso(1)}, more || {});
  const tag = (id) => ({id, by: 'rule', at: iso(5)});
  return [card(NEW, {sev: 'crit', num: 153, owner: 'alice@example.com', tags: [tag('data')]}), card(PROG, {triage: 'progress', owner: 'Carol Diaz', num: 149, tags: [tag('seo')]}),
    card(DONE, {triage: 'handled', status: 'cleared', clearedAt: iso(30), num: 140, owner: 'Paul Stone'}), card(FREE, {num: 141}),
    card(OLD, {status: 'cleared', clearedAt: iso(3 * 24 * 60), num: 120, lastSeen: iso(60 * 60)})].concat((extra || []).map((e) => card(e.key, e)));
}
function state(rev, extra) {
  return {rev: rev || 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)}, anomalies: cards(extra),
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(hash, stored, view, lang, serve) {
  const errors = [];
  serve = serve || {rev: 1, state: state(1)};
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE, {view: view || 'board'}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(serve.state)), {
    url: 'https://skopax.example.com/' + (hash || '#t=anoms/open'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      if (lang) {
        const raw = JSON.parse(fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'locale', lang, 'page.json'), 'utf8'));
        w.SKOPAX_I18N = {[lang]: Object.assign({}, raw.page, {'@rules': raw.rules, '@months': raw.months, '@days': raw.days, '@tails': raw.tails, '@th': raw.headers})};
        w.document.cookie = 'skopax_lang=' + lang + '; path=/';
      }
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      if (stored) Object.keys(stored).forEach((k) => w.localStorage.setItem(k, stored[k]));
      w.fetch = async (p) => {
        p = String(p);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', operator: true, teams: [], tags: TAGS});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: serve.rev});
        if (p.startsWith('/api/state')) return ok(serve.state);
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(450);
  return {w: dom.window, doc: dom.window.document, errors, serve};
}

const pack = (lang) => JSON.parse(fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'locale', lang, 'page.json'), 'utf8')).page;
const grp = (doc, g) => doc.querySelector('#bf-lists .fb-w[data-g="' + g + '"]');
const btn = (doc, g) => grp(doc, g).querySelector('.fb-b');
const clearX = (doc, g) => grp(doc, g).querySelector('.fb-x');
const pop = (doc, g) => grp(doc, g).querySelector('.fb-pop');
const menu = (doc, g) => grp(doc, g).querySelector('[role="menu"]');
const items = (doc, g) => [...grp(doc, g).querySelectorAll('.fb-i')];
const seen = (doc, g) => items(doc, g).filter((b) => !b.hidden);
const item = (doc, g, id) => items(doc, g).find((b) => (b.dataset.f || b.dataset.type) === id);
const lbl = (b) => b.querySelector('.lbl').textContent;
const checked = (doc, g) => items(doc, g).filter((b) => b.getAttribute('aria-checked') === 'true').map((b) => b.dataset.f || b.dataset.type);
const said = (doc, g) => btn(doc, g).textContent.replace(/\s+/g, ' ').trim();
const isOpen = (doc, g) => btn(doc, g).getAttribute('aria-expanded') === 'true' && !pop(doc, g).hidden;
const chips = (doc) => [...doc.querySelectorAll('#bf-main .qf-c')];
const chip = (doc, name) => chips(doc).find((b) => b.querySelector('.lbl').textContent === name);
const shown = (doc) => [...doc.querySelectorAll('#anoms .tile')].map((t) => t.dataset.key).sort();
const key = (w, el, k) => el.dispatchEvent(new w.KeyboardEvent('keydown', {key: k, bubbles: true, cancelable: true}));
const type = (w, input, text) => { input.value = text; input.dispatchEvent(new w.Event('input', {bubbles: true})); };
const css = () => fs.readFileSync(TEMPLATE, 'utf8');
const rule = (text, sel) => { const m = new RegExp('(?:^|[}\\n])\\s*' + sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ' \\{([^}]*)\\}').exec(text); return m ? m[1] : null; };

async function theBarShowsQuickChipsFourListsAndTheViewSwitch() {
  //given
  const {w, doc, errors} = await boot();

  //when
  const row = doc.getElementById('board-filters');
  const parts = [...row.children].map((n) => n.id);
  const groups = ['status', 'tags', 'who', 'type'].map((g) => [said(doc, g), btn(doc, g).getAttribute('aria-haspopup'), btn(doc, g).getAttribute('aria-expanded'), pop(doc, g).hidden, btn(doc, g).getAttribute('aria-controls') === menu(doc, g).id]);
  const gone = ['more-filters', 'more-filters-box', 'bf-rest', 'card-qf'].map((id) => doc.getElementById(id));
  const view = [...doc.querySelectorAll('#card-view button[data-view]')].map((b) => b.textContent);

  //then
  assert.deepStrictEqual(chips(doc).map((b) => b.querySelector('.lbl').textContent), ['Open', 'Mine', 'Critical', 'Done']);
  assert.deepStrictEqual(groups, [['Status', 'menu', 'false', true, true], ['Tags', 'menu', 'false', true, true], ['Assignee', 'menu', 'false', true, true], ['Type', 'menu', 'false', true, true]]);
  assert.deepStrictEqual(parts, ['bf-main', 'bf-lists', 'fb-clear-all', 'card-view'], 'chips, the four lists, Clear all, then the Board/List switch at the row end');
  assert.ok(doc.getElementById('card-search').parentNode.contains(row), 'the row sits with the search box');
  assert.strictEqual(doc.getElementById('fb-clear-all').hidden, true, 'no Clear all with nothing picked');
  assert.deepStrictEqual(view, ['Board', 'List']);
  assert.deepStrictEqual(gone, [null, null, null, null], 'More filters and its box are gone');
  assert.ok(!/moreFilters|MORE_KEY/.test(fs.readFileSync(path.join(path.dirname(TEMPLATE), 'static', 'cards.js'), 'utf8')), 'the old fold key is no longer read');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'one row: Open, Mine, Critical, Done, then Status, Tags, Assignee and Type lists, then the Board/List switch; More filters is gone';
}

async function statusListPicksOneAndClosesAndTheButtonSaysIt() {
  //given
  const {w, doc, errors} = await boot('#t=anoms/open');
  const sections = () => [...menu(doc, 'status').querySelectorAll('[role="group"]')].map((g) => [doc.getElementById(g.getAttribute('aria-labelledby')).textContent, [...g.querySelectorAll('.fb-i')].map(lbl)]);

  //when
  btn(doc, 'status').click();
  const opened = {open: isOpen(doc, 'status'), roles: [...new Set(items(doc, 'status').map((b) => b.getAttribute('role')))], checked: checked(doc, 'status'), sections: sections()};
  const counts = ['open', 'progress', 'muted', 'crit'].map((f) => item(doc, 'status', f).querySelector('.count').textContent);
  const dim = ['muted', 'high'].map((f) => item(doc, 'status', f).classList.contains('zero'));
  item(doc, 'status', 'progress').click();
  const picked = {open: isOpen(doc, 'status'), said: said(doc, 'status'), on: grp(doc, 'status').classList.contains('on'), x: clearX(doc, 'status') && !clearX(doc, 'status').hidden ? clearX(doc, 'status').getAttribute('aria-label') : null, hash: w.location.hash, cards: shown(doc), checked: checked(doc, 'status')};
  chip(doc, 'Critical').click();
  const viaChip = {said: said(doc, 'status'), on: grp(doc, 'status').classList.contains('on'), checked: checked(doc, 'status'), hash: w.location.hash};
  chip(doc, 'Open').click();
  const backOpen = {said: said(doc, 'status'), on: grp(doc, 'status').classList.contains('on')};

  //then
  assert.deepStrictEqual(opened, {open: true, roles: ['menuitemradio'], checked: ['open'], sections: [
    ['State', ['All', 'Open', 'In progress', 'Done', 'Muted', 'Stopped firing', 'Investigating', 'Investigated', 'Recurring', 'Archived', 'Closed']],
    ['Severity', ['Critical', 'High', 'Warning']]]});
  assert.deepStrictEqual(counts, ['3', '1', '0', '1']);
  assert.deepStrictEqual(dim, [true, true], 'an entry with 0 is shown dimmed, not hidden');
  assert.deepStrictEqual(picked, {open: false, said: 'Status: In progress', on: true, x: 'Clear Status filter', hash: '#t=anoms/progress', cards: [PROG], checked: ['progress']});
  assert.deepStrictEqual(viaChip, {said: 'Status: Critical', on: true, checked: ['crit'], hash: '#t=anoms/crit'}, 'a quick chip sets the same filter, and the button says it like a pick in the list');
  assert.deepStrictEqual(backOpen, {said: 'Status', on: false}, 'Open, the default, is no choice');
  assert.match(rule(css(), '.fb-w.on .fb-b'), /background:\s*var\(--accent-soft\)/, 'a button with a choice gets the quiet active fill');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'Status is one choice in State and Severity sections with counts; picking closes it and the button reads Status: In progress';
}

async function assigneeListPicksSeveralStaysOpenAndShowsTheCount() {
  //given
  const {w, doc, errors} = await boot('#t=anoms/all');

  //when
  btn(doc, 'who').click();
  const listed = items(doc, 'who').map((b) => [b.dataset.type, lbl(b), b.querySelector('.qf-av') ? b.querySelector('.qf-av').textContent : '', b.querySelector('.count').textContent]);
  const roles = [...new Set(items(doc, 'who').map((b) => b.getAttribute('role')))];
  item(doc, 'who', 'who:u:paul.stone').click();
  const one = {open: isOpen(doc, 'who'), said: said(doc, 'who'), cards: shown(doc)};
  item(doc, 'who', 'who:u:carol.diaz').click();
  const two = {open: isOpen(doc, 'who'), said: said(doc, 'who'), cards: shown(doc), hash: decodeURIComponent(w.location.hash)};
  item(doc, 'who', 'who:none').click();
  const three = {said: said(doc, 'who'), cards: shown(doc), checked: checked(doc, 'who')};
  const note = doc.querySelector('#filter-note .fn-cur').textContent;
  chip(doc, 'Mine').click();
  const mine = {said: said(doc, 'who'), cards: shown(doc), pressed: chip(doc, 'Mine').getAttribute('aria-pressed')};
  item(doc, 'who', 'who:u:paul.stone').click();
  const back = {said: said(doc, 'who'), pressed: chip(doc, 'Mine').getAttribute('aria-pressed')};

  //then
  assert.deepStrictEqual(listed, [['who:any', 'Assigned', '', '3'], ['who:none', 'Unassigned', '', '2'], ['who:u:alice', 'alice', 'A', '1'], ['who:u:carol.diaz', 'Carol Diaz', 'CD', '1'], ['who:u:paul.stone', 'Paul Stone', 'PS', '1']], 'Assigned, Unassigned, then every person with initials; Mine stays a chip');
  assert.deepStrictEqual(roles, ['menuitemcheckbox']);
  assert.deepStrictEqual(one, {open: true, said: 'Assignee: Paul Stone', cards: [DONE]});
  assert.deepStrictEqual(two, {open: true, said: 'Assignee (2)', cards: [DONE, PROG].sort(), hash: '#t=anoms/all/who:u:paul.stone+who:u:carol.diaz'}, 'two people show the cards of either');
  assert.deepStrictEqual(three, {said: 'Assignee (3)', cards: [DONE, FREE, OLD, PROG].sort(), checked: ['who:none', 'who:u:carol.diaz', 'who:u:paul.stone']});
  assert.ok(note.includes('Paul Stone or Carol Diaz or Unassigned'), 'the filter line says the picks of one list are either: ' + note);
  assert.deepStrictEqual(mine, {said: 'Assignee', cards: [NEW], pressed: 'true'}, 'Mine replaces the picked people, as it did');
  assert.deepStrictEqual(back, {said: 'Assignee: Paul Stone', pressed: 'false'}, 'a person replaces Mine');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'Assignee takes several people, stays open while picking and reads Assignee (2); Mine stays the chip it was';
}

async function tagsListShowsColourDotsNotColouredPillsAndSearchNarrowsIt() {
  //given
  const {w, doc, errors} = await boot('#t=anoms/all');
  const q = () => pop(doc, 'tags').querySelector('input.fb-q');

  //when
  btn(doc, 'tags').click();
  const clickFocus = doc.activeElement === q();
  const dots = items(doc, 'tags').map((b) => [lbl(b), !!b.querySelector('.fb-dot'), b.querySelector('.fb-dot') ? [...b.querySelector('.fb-dot').classList].some((c) => /^th-\d$/.test(c)) : false, [...b.classList].some((c) => c === 'tcol' || /^th-/.test(c))]);
  const box = {placeholder: q() && q().getAttribute('placeholder'), focused: doc.activeElement === q()};
  type(w, q(), 'se');
  const narrowed = seen(doc, 'tags').map(lbl);
  item(doc, 'tags', 'tag:seo').click();
  const one = {said: said(doc, 'tags'), open: isOpen(doc, 'tags'), search: q().value, cards: shown(doc)};
  type(w, q(), '');
  item(doc, 'tags', 'tag:data').click();
  const two = {said: said(doc, 'tags'), cards: shown(doc)};
  item(doc, 'tags', 'tag:-').click();
  const untagged = shown(doc);
  btn(doc, 'who').click();
  const who = {tagsOpen: isOpen(doc, 'tags'), search: !pop(doc, 'who').querySelector('input.fb-q').hidden};
  chip(doc, 'Critical').click();
  const chipCloses = isOpen(doc, 'who');

  //then
  assert.strictEqual(dots.length, 11, 'ten tags and Untagged');
  assert.ok(dots.slice(0, 10).every((d) => d[1] && d[2] && !d[3]), 'each tag is a neutral row with a dot of its tone: ' + JSON.stringify(dots));
  assert.deepStrictEqual(dots[10], ['Untagged', false, false, false]);
  assert.deepStrictEqual(box, {placeholder: 'Search tags', focused: false}, 'more than 8 entries get a search box; a click does not focus it, so a phone keyboard does not cover the list');
  assert.strictEqual(clickFocus, false);
  assert.deepStrictEqual(narrowed, ['SEO', 'Search']);
  assert.deepStrictEqual(one, {said: 'Tags: SEO', open: true, search: 'se', cards: [PROG]});
  assert.deepStrictEqual(two, {said: 'Tags (2)', cards: [NEW, PROG].sort()});
  assert.deepStrictEqual(untagged, [DONE, FREE, NEW, OLD, PROG].sort());
  assert.deepStrictEqual(who, {tagsOpen: false, search: false}, 'one list open at a time; five assignee entries need no search');
  assert.strictEqual(chipCloses, false, 'a click on a quick chip, which redraws itself, still closes an open list');
  assert.match(rule(css(), '.fb-dot'), /background:\s*var\(--tf\)/);
  assert.strictEqual(rule(css(), '.fb-i.tcol'), null, 'no coloured pill rule for list rows');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'Tags rows are neutral with a small dot of the tag colour, a search box narrows them, and two picked read Tags (2)';
}

async function clearingOneGroupAndClearAll() {
  //given
  const {w, doc, errors} = await boot('#t=anoms/progress/' + encodeURIComponent('who:u:paul.stone+tag:data'));
  const all = doc.getElementById('fb-clear-all');
  const labels = () => ['status', 'tags', 'who', 'type'].map((g) => said(doc, g));
  const xs = () => ['status', 'tags', 'who', 'type'].filter((g) => clearX(doc, g) && !clearX(doc, g).hidden);

  //when
  const before = {labels: labels(), xs: xs(), all: all.hidden};
  clearX(doc, 'tags').click();
  const xFocus = doc.activeElement === btn(doc, 'tags');
  const one = {labels: labels(), xs: xs(), all: all.hidden, hash: decodeURIComponent(w.location.hash)};
  all.focus();
  all.click();
  const allFocus = doc.activeElement === chip(doc, 'Open');
  const none = {labels: labels(), xs: xs(), all: all.hidden, hash: w.location.hash};
  const mine = await boot('#t=anoms/archived/' + encodeURIComponent('who:me+tag:data'));
  mine.doc.getElementById('fb-clear-all').click();
  const kept = {hash: decodeURIComponent(mine.w.location.hash), pressed: chip(mine.doc, 'Mine').getAttribute('aria-pressed'), all: mine.doc.getElementById('fb-clear-all').hidden};

  //then
  assert.deepStrictEqual(before, {labels: ['Status: In progress', 'Tags: Data', 'Assignee: Paul Stone', 'Type'], xs: ['status', 'tags', 'who'], all: false});
  assert.deepStrictEqual(one, {labels: ['Status: In progress', 'Tags', 'Assignee: Paul Stone', 'Type'], xs: ['status', 'who'], all: false, hash: '#t=anoms/progress/who:u:paul.stone'});
  assert.deepStrictEqual(none, {labels: ['Status', 'Tags', 'Assignee', 'Type'], xs: [], all: true, hash: '#t=anoms'});
  assert.strictEqual(xFocus, true, 'the x hands focus to its list button');
  assert.strictEqual(allFocus, true, 'Clear all hides itself and hands focus to the Open chip');
  assert.deepStrictEqual(kept, {hash: '#t=anoms/open/who:me', pressed: 'true', all: true}, 'Clear all clears the lists, Mine is a chip and stays');
  assert.deepStrictEqual(errors.concat(mine.errors), []);
  w.close(); mine.w.close();
  return 'the small x clears one list, Clear all clears every list and then hides';
}

async function keyboardOpensMovesTogglesAndEscReturnsFocus() {
  //given
  const {w, doc, errors} = await boot('#t=anoms/all');
  const at = () => doc.activeElement;

  //when
  btn(doc, 'who').focus();
  key(w, btn(doc, 'who'), 'Enter');
  const opened = {open: isOpen(doc, 'who'), focus: at().dataset.type};
  key(w, at(), 'ArrowDown');
  const down = at().dataset.type;
  key(w, at(), 'End');
  const end = at().dataset.type;
  key(w, at(), 'ArrowDown');
  const wrap = at().dataset.type;
  key(w, at(), 'ArrowUp');
  key(w, at(), 'Home');
  const home = at().dataset.type;
  key(w, at(), 'ArrowDown');
  const target = at();
  key(w, target, ' ');
  const toggled = {checked: target.getAttribute('aria-checked'), open: isOpen(doc, 'who'), same: at() === target, connected: target.isConnected};
  key(w, at(), 'Enter');
  const again = {checked: target.getAttribute('aria-checked'), open: isOpen(doc, 'who')};
  key(w, at(), ' ');
  key(w, at(), 'Escape');
  const esc = {open: isOpen(doc, 'who'), focus: at() === btn(doc, 'who'), hash: decodeURIComponent(w.location.hash)};
  key(w, btn(doc, 'who'), ' ');
  const space = isOpen(doc, 'who');
  key(w, btn(doc, 'who'), 'Enter');
  const enterShuts = {open: isOpen(doc, 'who'), focus: at() === btn(doc, 'who')};
  key(w, btn(doc, 'who'), ' ');
  key(w, at(), 'Tab');
  const tab = isOpen(doc, 'who');
  key(w, btn(doc, 'type'), 'ArrowDown');
  const arrow = {open: isOpen(doc, 'type'), role: at().getAttribute('role')};
  doc.getElementById('reco').click();
  const outside = isOpen(doc, 'type');
  key(w, btn(doc, 'status'), 'Enter');
  const statusFocus = at().dataset.f;
  key(w, at(), 'ArrowDown');
  key(w, at(), 'ArrowDown');
  key(w, at(), 'Enter');
  const chose = {open: isOpen(doc, 'status'), focus: at() === btn(doc, 'status'), hash: decodeURIComponent(w.location.hash)};
  key(w, btn(doc, 'tags'), 'Enter');
  const search = pop(doc, 'tags').querySelector('input.fb-q');
  const keyFocus = at() === search;
  type(w, search, 'pay');
  key(w, search, 'ArrowDown');
  const fromSearch = at().dataset.type;
  key(w, at(), 'Escape');
  const tagsEsc = {open: isOpen(doc, 'tags'), focus: at() === btn(doc, 'tags')};

  //then
  assert.deepStrictEqual(opened, {open: true, focus: 'who:any'}, 'Enter opens and focuses the first entry');
  assert.deepStrictEqual([down, end, wrap, home], ['who:none', 'who:u:paul.stone', 'who:any', 'who:any']);
  assert.deepStrictEqual(toggled, {checked: 'true', open: true, same: true, connected: true}, 'Space toggles, the list stays open and focus stays on the entry');
  assert.deepStrictEqual(again, {checked: 'false', open: true}, 'Enter toggles too');
  assert.deepStrictEqual(esc, {open: false, focus: true, hash: '#t=anoms/all/who:none'}, 'Esc closes, returns focus to the button and keeps the filter');
  assert.strictEqual(space, true, 'Space on the button opens');
  assert.deepStrictEqual(enterShuts, {open: false, focus: true}, 'Enter on the button of an open list closes it');
  assert.strictEqual(tab, false, 'Tab closes');
  assert.deepStrictEqual(arrow, {open: true, role: 'menuitemcheckbox'}, 'ArrowDown on the button opens');
  assert.strictEqual(outside, false, 'a click outside closes');
  assert.strictEqual(statusFocus, 'all', 'Status opens on the checked entry');
  assert.deepStrictEqual(chose, {open: false, focus: true, hash: '#t=anoms/progress/who:none'}, 'choosing in Status closes it');
  assert.strictEqual(keyFocus, true, 'opened by keyboard, a list with search focuses its search box');
  assert.deepStrictEqual(fromSearch, 'tag:payments', 'typing narrows and ArrowDown goes to the first match');
  assert.deepStrictEqual(tagsEsc, {open: false, focus: true});
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the lists work with the keyboard: Enter, Space or ArrowDown open, arrows, Home and End move, Space and Enter pick, Esc and Tab close';
}

async function aLiveRefreshKeepsAnOpenListItsSearchAndFocus() {
  //given
  const serve = {rev: 1, state: state(1)};
  const {w, doc, errors} = await boot('#t=anoms/all', null, 'board', null, serve);
  btn(doc, 'tags').click();
  const q = pop(doc, 'tags').querySelector('input.fb-q');
  type(w, q, 'se');
  key(w, q, 'ArrowDown');
  const focused = doc.activeElement;

  //when
  serve.rev = 2;
  serve.state = state(2, [{key: 'log|search-api|ERROR|new', num: 160, tags: [{id: 'seo', by: 'rule', at: iso(1)}]}]);
  w.document.dispatchEvent(new w.Event('visibilitychange'));
  await wait(400);

  //then
  assert.ok(shown(doc).includes('log|search-api|ERROR|new'), 'the live update arrived');
  assert.strictEqual(isOpen(doc, 'tags'), true, 'the list stays open');
  assert.strictEqual(pop(doc, 'tags').querySelector('input.fb-q'), q, 'the same search box');
  assert.strictEqual(q.value, 'se');
  assert.deepStrictEqual(seen(doc, 'tags').map(lbl), ['SEO', 'Search'], 'still narrowed');
  assert.strictEqual(doc.activeElement, focused, 'focus stays on the entry');
  assert.strictEqual(focused.dataset.type, 'tag:seo');
  assert.strictEqual(focused.querySelector('.count').textContent, '2', 'with the new count');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a live refresh updates counts in place: an open list keeps its search text and focus';
}

async function rememberedFiltersAndViewComeBackAfterReload() {
  //given
  const first = await boot('#t=anoms/open', {'skopax.moreFilters': 'open'});
  const legacy = isOpen(first.doc, 'status') || isOpen(first.doc, 'tags') || isOpen(first.doc, 'who') || isOpen(first.doc, 'type');

  //when
  btn(first.doc, 'status').click();
  item(first.doc, 'status', 'all').click();
  btn(first.doc, 'who').click();
  item(first.doc, 'who', 'who:u:paul.stone').click();
  item(first.doc, 'who', 'who:u:carol.diaz').click();
  btn(first.doc, 'tags').click();
  item(first.doc, 'tags', 'tag:seo').click();
  first.doc.querySelector('#card-view [data-view="list"]').click();
  const hash = first.w.location.hash, view = first.w.localStorage.getItem('skopax-card-view');
  const again = await boot(hash, {'skopax-card-view': view}, 'default');
  const old = await boot('#t=anoms/open/' + encodeURIComponent('who:u:paul.stone'));

  //then
  assert.strictEqual(legacy, false, 'the old fold key opens nothing');
  assert.strictEqual(view, 'list');
  assert.deepStrictEqual(['status', 'tags', 'who'].map((g) => said(again.doc, g)), ['Status: All', 'Tags: SEO', 'Assignee (2)']);
  assert.deepStrictEqual(checked(again.doc, 'who'), ['who:u:carol.diaz', 'who:u:paul.stone']);
  assert.strictEqual(again.doc.querySelector('#card-view [data-view="list"]').getAttribute('aria-pressed'), 'true');
  assert.strictEqual(said(old.doc, 'who'), 'Assignee: Paul Stone', 'an old one-person link still works');
  assert.deepStrictEqual(first.errors.concat(again.errors, old.errors), []);
  [first, again, old].forEach((t) => t.w.close());
  return 'the filter, the picked lists and the view come back after a reload, and old links keep working';
}

function theCardSearchRuleLeavesTheListSearchAlone(t) {
  //given
  const text = css();
  const m = /(?:^|\n)([^{}\n]+) \{[^}]*flex:\s*0 1 26rem/.exec(text);
  const q = pop(t.doc, 'tags').querySelector('input.fb-q');

  //when
  const sel = m[1].trim();

  //then
  assert.ok(t.doc.getElementById('card-search').matches(sel), sel + ' sizes the card search');
  assert.strictEqual(q.matches(sel), false, sel + ' must not reach the list search, it made it 416px tall in a column flex box');
  assert.match(rule(text, '.fb-q'), /flex:\s*none/);
  return 'the card search sizing rule does not reach the list search box, which keeps its own height';
}

async function aListSearchDoesNotHoldBackALiveUpdate() {
  //given
  const serve = {rev: 1, state: state(1)};
  const {w, doc, errors} = await boot('#t=anoms/all', null, 'board', null, serve);
  key(w, btn(doc, 'tags'), 'Enter');
  const q = pop(doc, 'tags').querySelector('input.fb-q');
  type(w, q, 'se');

  //when
  serve.rev = 2;
  serve.state = state(2, [{key: 'log|search-api|ERROR|new', num: 160, tags: [{id: 'seo', by: 'rule', at: iso(1)}]}]);
  w.document.dispatchEvent(new w.Event('visibilitychange'));
  await wait(400);

  //then
  assert.strictEqual(doc.activeElement, q, 'typing goes on');
  assert.strictEqual(item(doc, 'tags', 'tag:seo').querySelector('.count').textContent, '2', 'the counts follow while typing in a list search');
  assert.strictEqual(q.value, 'se');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'typing in a list search does not hold back a live update';
}

async function aListNearTheRightEdgeOpensLeftwards() {
  //given
  const {w, doc, errors} = await boot('#t=anoms/all');
  const at = (g, left) => { btn(doc, g).getBoundingClientRect = () => ({left, right: left + 80, top: 0, bottom: 30, width: 80, height: 30}); };
  Object.defineProperty(w, 'innerWidth', {configurable: true, value: 800});

  //when
  at('type', 600);
  btn(doc, 'type').click();
  const right = pop(doc, 'type').classList.contains('fb-end');
  at('status', 100);
  btn(doc, 'status').click();
  const left = pop(doc, 'status').classList.contains('fb-end');

  //then
  assert.deepStrictEqual([right, left], [true, false], 'a button in the right half opens its list towards the left');
  assert.match(rule(css(), '.fb-pop.fb-end'), /left:\s*auto;\s*right:\s*0/);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a list whose button sits in the right half opens leftwards, so it never runs off the right edge';
}

async function picksStopAtFortyAndALongLinkKeepsTheFirstForty() {
  //given
  const many = Array.from({length: 41}, (_, i) => 'kind-' + i);
  const long = await boot('#t=anoms/all/' + encodeURIComponent(many.join('+')));
  const full = await boot('#t=anoms/all/' + encodeURIComponent(many.slice(0, 40).join('+')));

  //when
  const kept = long.w.eval('typeF').split('+');
  btn(full.doc, 'tags').click();
  item(full.doc, 'tags', 'tag:data').click();
  const after = full.w.eval('typeF').split('+');

  //then
  assert.deepStrictEqual(kept, many.slice(0, 40), 'a link with 41 picks keeps the first 40');
  assert.strictEqual(after.length, 40, 'a 41st pick is not added');
  assert.ok(!after.includes('tag:data'));
  assert.deepStrictEqual(long.errors.concat(full.errors), []);
  long.w.close(); full.w.close();
  return 'picks stop at 40, and a longer link keeps its first 40 instead of dropping them all';
}

async function aServiceViewDoesNotLightStatus() {
  //given
  const {w, doc, errors} = await boot('#t=anoms/open');

  //when
  w.eval("svcF = {svc: 'checkout-service', ns: ''}; setFilter('svc');");
  const got = {said: said(doc, 'status'), on: grp(doc, 'status').classList.contains('on'), all: doc.getElementById('fb-clear-all').hidden};

  //then
  assert.deepStrictEqual(got, {said: 'Status', on: false, all: true}, 'the service view has no Status row, so Status stays plain');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the service view does not light the Status button';
}

async function aListALiveUpdateHidesHandsFocusToOpen() {
  //given
  const serve = {rev: 1, state: state(1)};
  const {w, doc, errors} = await boot('#t=anoms/all', null, 'board', null, serve);
  key(w, btn(doc, 'type'), 'Enter');
  const before = doc.activeElement.getAttribute('role');

  //when
  serve.rev = 2;
  serve.state = Object.assign(state(2), {anomalies: []});
  w.document.dispatchEvent(new w.Event('visibilitychange'));
  await wait(400);

  //then
  assert.strictEqual(before, 'menuitemcheckbox');
  assert.strictEqual(grp(doc, 'type').hidden, true, 'no card, no Type list');
  assert.strictEqual(doc.activeElement, chip(doc, 'Open'), 'focus goes to the Open chip, not the page');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'when a live update hides an open list, focus goes to the Open chip';
}

function atPhoneWidthTheRowWrapsWithoutHorizontalScroll() {
  //given
  const text = css();
  const at = text.indexOf('@media (max-width: 640px), (max-height: 500px) and (pointer: coarse) {');
  const phone = text.slice(at, text.indexOf('\n}\n', at));

  //when
  const row = rule(text, '.bf');
  const pop = rule(text, '.fb-pop');
  const label = rule(text, '.fb-l');
  const phoneWrap = rule(phone, '.fb-w');
  const phonePop = rule(phone, '.fb-pop');

  //then
  assert.match(row, /flex-wrap:\s*wrap/, 'the row wraps');
  assert.match(pop, /max-width:\s*min\([^;]*100vw[^;]*\)/, 'a list is never wider than the screen');
  assert.match(label, /text-overflow:\s*ellipsis/, 'a long choice is cut, not widened');
  assert.match(phoneWrap, /position:\s*static/);
  assert.match(phonePop, /left:\s*0/);
  assert.match(phonePop, /right:\s*0/);
  assert.ok(!/#card-qf|#more-filters/.test(text), 'no rule for the old fold is left');
  assert.ok(!/(#board-filters|\.bf|\.fb-[a-z]+)[^{]*\{[^}]*(nowrap|overflow-x:\s*auto)/.test(phone), 'nothing in the row scrolls sideways on a phone');
  return 'at phone width the row wraps, lists span the row and long labels are cut, so nothing scrolls sideways';
}

async function theListsAreTranslated() {
  //given
  const tr = pack('tr');
  const {w, doc, errors} = await boot('#t=anoms/all/' + encodeURIComponent('who:u:paul.stone+who:u:carol.diaz'), null, 'board', 'tr');

  //when
  const names = ['status', 'tags', 'type'].map((g) => said(doc, g));
  const who = said(doc, 'who');
  const x = clearX(doc, 'who').getAttribute('aria-label');
  const group = doc.getElementById('bf-lists').getAttribute('aria-label');
  const note = doc.querySelector('#filter-note .fn-cur').textContent;
  const mine = doc.querySelector('#bf-main [data-type="who:me"] .lbl').textContent;
  btn(doc, 'tags').click();
  const q = pop(doc, 'tags').querySelector('input.fb-q').getAttribute('placeholder');
  btn(doc, 'status').click();
  const secs = [...menu(doc, 'status').querySelectorAll('.fb-sec')].map((s) => s.textContent);

  //then
  assert.deepStrictEqual(names, [tr['Status'] + ': ' + tr['All'], tr['Tags'], tr['Type']]);
  assert.strictEqual(who, tr['{0} ({1})'].replace('{0}', tr['Assignee']).replace('{1}', '2'));
  assert.strictEqual(x, tr['Clear {0} filter'].replace('{0}', tr['Assignee']));
  assert.strictEqual(mine, tr['Mine']);
  assert.strictEqual(group, tr['Filter lists']);
  assert.ok(note.includes(' ' + tr['or'] + ' '), note);
  assert.strictEqual(q, tr['Search tags']);
  assert.deepStrictEqual(secs, [tr['State'], tr['Severity']]);
  ['de', 'es', 'fr', 'tr'].forEach((l) => ['Status', 'Tags', 'Assignee', 'Type', 'State', 'Severity', 'Clear all', 'Clear {0} filter', 'Search tags', 'Search people', '{0} ({1})', 'Filter lists', 'or'].forEach((s) => assert.ok(typeof pack(l)[s] === 'string', l + ' has ' + s)));
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the lists, their labels, sections and search boxes read in the page language';
}

async function theClosedListKeepsTheChipsAndStatus() {
  //given
  const {w, doc, errors} = await boot('#t=anoms/closed');

  //when
  const status = said(doc, 'status');
  const checkedNow = checked(doc, 'status');

  //then
  assert.strictEqual(status, 'Status: Closed');
  assert.deepStrictEqual(checkedNow, ['closed']);
  assert.strictEqual(chips(doc).length, 4, 'the four chips still lead back to the board');
  assert.match(css(), /\.closed-on \.fb-multi \{\s*display:\s*none/, 'the closed list hides Tags, Assignee and Type, which do not apply to it');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the Closed list keeps the chips and the Status list so the board is one click away';
}

async function mineCriticalAndDoneAreTheExistingFilters() {
  //given
  const {w, doc, errors} = await boot();

  //when
  chip(doc, 'Mine').click();
  const mine = {cards: shown(doc), hash: decodeURIComponent(w.location.hash), pressed: chip(doc, 'Mine').getAttribute('aria-pressed')};
  chip(doc, 'Mine').click();
  const cleared = shown(doc).length;
  chip(doc, 'Critical').click();
  const crit = {cards: shown(doc), hash: w.location.hash};
  chip(doc, 'Critical').click();
  const back = chip(doc, 'Open').getAttribute('aria-pressed');
  chip(doc, 'Done').click();
  const done = {cards: shown(doc), hash: w.location.hash};

  //then
  assert.deepStrictEqual(mine, {cards: [NEW], hash: '#t=anoms/open/who:me', pressed: 'true'});
  assert.ok(cleared > 1, 'pressing Mine again drops it');
  assert.deepStrictEqual(crit, {cards: [NEW], hash: '#t=anoms/crit'});
  assert.strictEqual(back, 'true', 'pressing Critical again returns to Open');
  assert.deepStrictEqual(done, {cards: [DONE], hash: '#t=anoms/taken'});
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'Mine, Critical and Done are the chips they were: same cards, same links, same toggle';
}

(async () => {
  console.log('PASS ' + await theBarShowsQuickChipsFourListsAndTheViewSwitch());
  console.log('PASS ' + await statusListPicksOneAndClosesAndTheButtonSaysIt());
  console.log('PASS ' + await assigneeListPicksSeveralStaysOpenAndShowsTheCount());
  console.log('PASS ' + await tagsListShowsColourDotsNotColouredPillsAndSearchNarrowsIt());
  console.log('PASS ' + await clearingOneGroupAndClearAll());
  console.log('PASS ' + await keyboardOpensMovesTogglesAndEscReturnsFocus());
  console.log('PASS ' + await aLiveRefreshKeepsAnOpenListItsSearchAndFocus());
  console.log('PASS ' + await rememberedFiltersAndViewComeBackAfterReload());
  console.log('PASS ' + atPhoneWidthTheRowWrapsWithoutHorizontalScroll());
  console.log('PASS ' + await theListsAreTranslated());
  console.log('PASS ' + await theClosedListKeepsTheChipsAndStatus());
  console.log('PASS ' + await mineCriticalAndDoneAreTheExistingFilters());
  const t = await boot('#t=anoms/all');
  console.log('PASS ' + theCardSearchRuleLeavesTheListSearchAlone(t));
  t.w.close();
  console.log('PASS ' + await aListSearchDoesNotHoldBackALiveUpdate());
  console.log('PASS ' + await aListNearTheRightEdgeOpensLeftwards());
  console.log('PASS ' + await picksStopAtFortyAndALongLinkKeepsTheFirstForty());
  console.log('PASS ' + await aServiceViewDoesNotLightStatus());
  console.log('PASS ' + await aListALiveUpdateHidesHandsFocusToOpen());
  process.exit(0);
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
