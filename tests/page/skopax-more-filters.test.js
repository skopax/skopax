const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const NEW = 'log|checkout-service|ERROR|boom', PROG = 'log|inventory-service|ERROR|x', DONE = 'log|shop-web|WARN|y', OLD = 'log|old-service|WARN|gone';

function state() {
  const card = (key, extra) => Object.assign({key, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'ERROR lines', src: 'logs', sig: 'log', now: 1, firstSeen: iso(90), lastSeen: iso(1)}, extra || {});
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(NEW, {sev: 'crit', num: 153, owner: 'alice@example.com', tags: [{id: 'data', by: 'rule', at: iso(5)}]}), card(PROG, {triage: 'progress', owner: 'Carol Diaz', num: 149}),
      card(DONE, {triage: 'handled', status: 'cleared', clearedAt: iso(30), num: 140}), card(OLD, {status: 'cleared', clearedAt: iso(3 * 24 * 60), num: 120, lastSeen: iso(60 * 60)})],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(hash, stored, view, lang) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE, {view: view || 'board'}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state())), {
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
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', operator: true, teams: [], tags: [{id: 'data', label: 'Data'}]});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(450);
  return {w: dom.window, doc: dom.window.document, errors};
}

const label = (b) => (b.querySelector('.lbl') || b).firstChild.textContent;
const box = (doc) => doc.getElementById('more-filters-box');
const outside = (doc) => [...doc.querySelectorAll('#panel-anoms .qf-c')].filter((b) => !box(doc).contains(b));
const inside = (doc) => [...box(doc).querySelectorAll('.qf-c')];
const shown = (doc) => [...doc.querySelectorAll('#anoms .tile')].map((t) => t.dataset.key).sort();

async function fourChipsInView() {
  //given
  const {w, doc, errors} = await boot();

  //when
  const labels = outside(doc).map(label);
  const toggle = doc.getElementById('more-filters');

  //then
  assert.deepStrictEqual(labels, ['Open', 'Mine', 'Critical', 'Done']);
  assert.ok(toggle && !box(doc).contains(toggle), 'More filters sits in view');
  assert.strictEqual(toggle.getAttribute('aria-expanded'), 'false');
  assert.strictEqual(box(doc).hidden, true);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'four chips are in view: Open, Mine, Critical, Done';
}

async function theRestSitInTheFold() {
  //given
  const {w, doc, errors} = await boot('#t=anoms/all');

  //when
  const ids = inside(doc).map((b) => b.dataset.f || b.dataset.type);
  const names = inside(doc).filter((b) => b.dataset.f).map(label);

  //then
  assert.deepStrictEqual(names, ['All', 'In progress', 'Muted', 'Stopped firing', 'Investigating', 'Investigated', 'High', 'Warning', 'Recurring', 'Archived', 'Closed']);
  ['who:none', 'who:u:alice', 'who:u:carol.diaz', 'tag:data'].forEach((t) => assert.ok(ids.indexOf(t) >= 0, t + ' is in the fold'));
  assert.ok(box(doc).querySelector('#card-view button[data-view="board"]') && box(doc).querySelector('#card-view button[data-view="list"]'), 'the Board and List switch is in the fold');
  assert.ok(!inside(doc).some((b) => b.dataset.type === 'who:me'), 'Mine is not repeated in the fold');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the other filters, the people and tags and the Board/List switch sit in the fold';
}

async function anActiveHiddenFilterOpensTheFold() {
  //given
  const filter = await boot('#t=anoms/archived');
  const person = await boot('#t=anoms/open/' + encodeURIComponent('who:u:alice'));
  const list = await boot('#t=anoms/open', null, 'list');
  const plain = await boot('#t=anoms/open');
  const pressed = (t) => [...t.doc.querySelectorAll('#more-filters-box .qf-c[aria-pressed="true"]')].map((b) => b.dataset.f || b.dataset.type);
  const expanded = (t) => t.doc.getElementById('more-filters').getAttribute('aria-expanded');

  //when
  const before = expanded(filter);

  //then
  assert.strictEqual(before, 'true');
  assert.deepStrictEqual(pressed(filter), ['archived']);
  assert.strictEqual(filter.doc.getElementById('more-filters-box').hidden, false);
  assert.strictEqual(expanded(person), 'true');
  assert.deepStrictEqual(pressed(person), ['who:u:alice']);
  assert.strictEqual(expanded(list), 'true', 'the list view lives in the fold, so it opens it');
  assert.strictEqual(expanded(plain), 'false');
  assert.deepStrictEqual([filter, person, list, plain].reduce((all, t) => all.concat(t.errors), []), []);
  [filter, person, list, plain].forEach((t) => t.w.close());
  return 'a saved filter, person chip or list view inside the fold opens it by itself and shows the active chip';
}

async function theButtonStaysUsableAndCountsTheHiddenFilters() {
  //given
  const {w, doc, errors} = await boot('#t=anoms/archived/' + encodeURIComponent('who:u:alice+tag:data'));
  const btn = doc.getElementById('more-filters');
  const text = () => btn.textContent.replace(/\s+/g, ' ').trim();

  //when
  const open = {text: text(), expanded: btn.getAttribute('aria-expanded')};
  btn.click();
  const shut = {text: text(), expanded: btn.getAttribute('aria-expanded'), hidden: doc.getElementById('more-filters-box').hidden};
  w.renderAnoms();
  const stays = btn.getAttribute('aria-expanded');
  btn.click();
  const again = btn.getAttribute('aria-expanded');

  //then
  assert.deepStrictEqual(open, {text: 'More filters (3)', expanded: 'true'}, 'the archive list, a person and a tag are three hidden filters');
  assert.deepStrictEqual(shut, {text: 'More filters (3)', expanded: 'false', hidden: true}, 'a click collapses the fold and the count stays on the button');
  assert.strictEqual(stays, 'false', 'a redraw does not reopen a fold the person collapsed');
  assert.strictEqual(again, 'true');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'More filters stays clickable with a hidden filter on, and its count says how many are hidden';
}

async function theClosedListKeepsTheFilterChips() {
  //given
  const {w, doc, errors} = await boot('#t=anoms/closed');

  //when
  const pressed = [...doc.querySelectorAll('#more-filters-box .qf-c[aria-pressed="true"]')].map((b) => b.dataset.f);
  const visible = [...doc.querySelectorAll('#bf-main .qf-c')].length;

  //then
  assert.deepStrictEqual(pressed, ['closed']);
  assert.strictEqual(visible, 4, 'the four chips still lead back to the board');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the Closed list keeps the chips so the board is one click away';
}

async function theFoldHoldsEveryGroupTheSideMenuHad() {
  //given
  const {w, doc, errors} = await boot('#t=anoms/all');

  //when
  const groups = [...doc.querySelectorAll('#more-filters-box .qf-g')].map((g) => g.getAttribute('aria-label'));
  const ids = [...doc.querySelectorAll('#more-filters-box .qf-c')].map((b) => b.dataset.f || b.dataset.type);

  //then
  assert.deepStrictEqual(groups, ['More filters', 'Tags', 'Assignee', 'Types']);
  ['archived', 'closed', 'recurring', 'who:any', 'who:none', 'log'].forEach((t) => assert.ok(ids.indexOf(t) >= 0, t + ' is in the fold'));
  assert.strictEqual(doc.getElementById('tab-sub-anoms'), null, 'the side menu panel is gone');
  assert.strictEqual(doc.getElementById('tabs'), null);
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the fold holds the lists, the assignee, tag and type groups the side menu used to have';
}

async function theButtonAndChipsAreTranslated() {
  //given
  const {w, doc, errors} = await boot('#t=anoms/archived', null, 'board', 'tr');

  //when
  const btn = doc.getElementById('more-filters').textContent.replace(/\s+/g, ' ').trim();
  const mine = doc.querySelector('#bf-main [data-type="who:me"] .lbl').textContent;

  //then
  assert.strictEqual(btn, 'Daha fazla filtre (1)');
  assert.strictEqual(mine, 'Benim');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'More filters and Mine read in Turkish';
}

async function theFoldRemembers() {
  //given
  const first = await boot();

  //when
  first.doc.getElementById('more-filters').click();
  const opened = first.doc.getElementById('more-filters').getAttribute('aria-expanded');
  const key = first.w.localStorage.getItem('skopax.moreFilters');
  const again = await boot('#t=anoms/open', {'skopax.moreFilters': 'open'});
  again.doc.getElementById('more-filters').click();
  const closedKey = again.w.localStorage.getItem('skopax.moreFilters');
  const reload = await boot('#t=anoms/open', {'skopax.moreFilters': closedKey});

  //then
  assert.strictEqual(opened, 'true');
  assert.strictEqual(key, 'open');
  assert.strictEqual(closedKey, 'closed');
  assert.strictEqual(again.doc.getElementById('more-filters').getAttribute('aria-expanded'), 'false');
  assert.strictEqual(reload.doc.getElementById('more-filters').getAttribute('aria-expanded'), 'false');
  assert.strictEqual((await boot('#t=anoms/open', {'skopax.moreFilters': 'open'})).doc.getElementById('more-filters').getAttribute('aria-expanded'), 'true');
  assert.deepStrictEqual(first.errors.concat(again.errors, reload.errors), []);
  [first, again, reload].forEach((t) => t.w.close());
  return 'the fold keeps open or closed per browser, and only a hidden active filter overrides a closed choice';
}

async function mineIsTheExistingMeFilter() {
  //given
  const {w, doc, errors} = await boot();
  const mine = () => outside(doc).find((b) => label(b) === 'Mine');

  //when
  mine().click();
  const viaMine = {cards: shown(doc), hash: w.location.hash, pressed: mine().getAttribute('aria-pressed')};
  mine().click();
  const cleared = shown(doc);
  const viaHash = await boot('#t=anoms/open/' + encodeURIComponent('who:me'));
  const viaOld = {cards: shown(viaHash.doc), hash: viaHash.w.location.hash};
  viaHash.w.close();

  //then
  assert.deepStrictEqual(viaMine.cards, [NEW]);
  assert.strictEqual(viaMine.pressed, 'true');
  assert.deepStrictEqual(viaOld.cards, viaMine.cards, 'the old Me link shows the same cards');
  assert.ok(decodeURIComponent(viaMine.hash).endsWith('who:me'), viaMine.hash);
  assert.strictEqual(cleared.length > 1, true, 'pressing Mine again drops it');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'Mine is the Me assignee filter: same cards, same link, same toggle';
}

async function criticalAndDoneAreTheExistingFilters() {
  //given
  const {w, doc, errors} = await boot();
  const chip = (name) => outside(doc).find((b) => label(b) === name);

  //when
  chip('Critical').click();
  const crit = {cards: shown(doc), hash: w.location.hash};
  chip('Critical').click();
  const back = chip('Open').getAttribute('aria-pressed');
  chip('Done').click();
  const done = {cards: shown(doc), hash: w.location.hash};
  inside(doc).find((b) => b.dataset.f === 'muted').click();
  const muted = w.location.hash;

  //then
  assert.deepStrictEqual(crit, {cards: [NEW], hash: '#t=anoms/crit'});
  assert.strictEqual(back, 'true');
  assert.deepStrictEqual(done.hash, '#t=anoms/taken');
  assert.deepStrictEqual(done.cards, [DONE]);
  assert.strictEqual(muted, '#t=anoms/muted');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'Critical and Done are the existing chips, pressing Critical again returns to Open';
}

async function theOpenButtonUsesTheQuietActiveFill() {
  //given
  const css = fs.readFileSync(TEMPLATE, 'utf8');

  //when
  const m = /#more-filters\[aria-expanded="true"\] \{([^}]*)\}/.exec(css);

  //then
  assert.ok(m, 'an open More filters button has its own rule');
  assert.match(m[1], /background:\s*var\(--accent-soft\)/);
  assert.doesNotMatch(m[1], /--btn-bg/);
  return 'the open More filters button uses the quiet active fill';
}

(async () => {
  console.log('PASS ' + await fourChipsInView());
  console.log('PASS ' + await theRestSitInTheFold());
  console.log('PASS ' + await anActiveHiddenFilterOpensTheFold());
  console.log('PASS ' + await theButtonStaysUsableAndCountsTheHiddenFilters());
  console.log('PASS ' + await theClosedListKeepsTheFilterChips());
  console.log('PASS ' + await theFoldHoldsEveryGroupTheSideMenuHad());
  console.log('PASS ' + await theButtonAndChipsAreTranslated());
  console.log('PASS ' + await theFoldRemembers());
  console.log('PASS ' + await mineIsTheExistingMeFilter());
  console.log('PASS ' + await criticalAndDoneAreTheExistingFilters());
  console.log('PASS ' + await theOpenButtonUsesTheQuietActiveFill());
  process.exit(0);
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
