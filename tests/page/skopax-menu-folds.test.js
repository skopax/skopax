const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const A1 = 'log|checkout-service|ERROR|boom', A2 = 'log|payments-api|ERROR|npe', M1 = 'log|inventory-service|ERROR|x', FREE = 'log|shop-web|ERROR|other';

function state() {
  const card = (key, extra) => Object.assign({key, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'x', src: 'logs', sig: 'log', now: 1, firstSeen: iso(30), lastSeen: iso(1)}, extra || {});
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(A1, {owner: 'Nina White', tags: [{id: 'developer', by: 'pod', at: iso(5)}]}), card(A2, {owner: 'nina.white@example.com'}), card(M1, {owner: 'Paul Stone', tags: [{id: 'data', by: 'rule', at: iso(5)}]}), card(FREE)],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(hash, stored) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state()));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/' + (hash || '#t=anoms'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      if (stored) Object.keys(stored).forEach((k) => w.localStorage.setItem(k, stored[k]));
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'nina.white@example.com', operator: true, teams: [], tags: [{id: 'developer', label: 'Developer'}, {id: 'data', label: 'Data'}, {id: 'infra', label: 'Infrastructure'}, {id: 'analyst', label: 'Analyst'}]});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors};
}

const shown = (doc) => [...doc.querySelectorAll('#anoms article.inc')].map((c) => c.dataset.key).sort();
const items = (doc, cls) => [...doc.querySelectorAll('#tab-sub-anoms .' + cls + ' button.tab-sub-i')].map((b) => b.querySelector('.lbl').textContent + ' ' + b.querySelector('.count').textContent);
const fold = (doc, id) => doc.querySelector('#tab-sub-anoms [data-fold="' + id + '"]');

async function everyAssigneeIsAFilterUnderAssignee() {
  //given
  const {w, doc, errors} = await boot('#t=anoms');

  //when
  const listed = items(doc, 'who-g');
  doc.querySelector('#tab-sub-anoms .who-g [data-type="who:u:paul.stone"]').click();
  const paul = {cards: shown(doc), hash: w.location.hash, pressed: doc.querySelector('#tab-sub-anoms .who-g [data-type="who:u:paul.stone"]').getAttribute('aria-pressed')};
  doc.querySelector('#tab-sub-anoms .who-g [data-type="who:u:nina.white"]').click();
  const nina = shown(doc);
  doc.querySelector('#tab-sub-anoms .who-g [data-type="who:u:nina.white"]').click();
  const back = shown(doc);
  const reload = await boot('#t=anoms/open/who%3Au%3Apaul.stone');

  //then
  assert.deepStrictEqual(listed, ['Assigned 3', 'Me 2', 'Unassigned 1', 'Nina White 2', 'Paul Stone 1'], 'after Assigned / Mine / Unassigned each person with cards, most first; a name and an email of one person count once');
  assert.deepStrictEqual(paul, {cards: [M1], hash: '#t=anoms/open/who%3Au%3Apaul.stone', pressed: 'true'});
  assert.deepStrictEqual(nina, [A2, A1].sort(), 'both spellings of one person are theirs');
  assert.deepStrictEqual(back, [A1, A2, FREE, M1].sort(), 'a second click shows everyone again');
  assert.deepStrictEqual(shown(reload.doc), [M1], 'a copied link keeps the person');
  assert.deepStrictEqual(errors.concat(reload.errors), []);
  w.close(); reload.w.close();
  return 'the Assignee group lists every person with cards as a filter (name and email of one person merged), and a link keeps it';
}

async function theSmallMenuGroupsFoldAndRememberIt() {
  //given
  const {w, doc, errors} = await boot('#t=anoms');
  const heads = [...doc.querySelectorAll('#tab-sub-anoms button.types-fold')].map((b) => b.dataset.fold);

  //when
  fold(doc, 'lists').click();
  const lists = {open: fold(doc, 'lists').getAttribute('aria-expanded'), rows: items(doc, 'tab-sub-g:has([data-fold="lists"])').length, focus: doc.activeElement === fold(doc, 'lists')};
  doc.querySelector('#tab-sub-anoms .tags-g [data-type="tag:data"]').click();
  fold(doc, 'tags').click();
  const tags = items(doc, 'tags-g');
  fold(doc, 'who').click();
  const who = items(doc, 'who-g');
  const saved = JSON.parse(w.localStorage.getItem('skopax-sub-fold'));
  const again = await boot('#t=anoms', {'skopax-sub-fold': JSON.stringify(saved)});
  const kept = ['lists', 'tags', 'who'].map((id) => fold(again.doc, id).getAttribute('aria-expanded'));
  fold(again.doc, 'lists').click();
  const reopened = items(again.doc, 'tab-sub-g:has([data-fold="lists"])').length;

  //then
  assert.deepStrictEqual(heads, ['lists', 'who', 'tags', 'types'], 'Lists, Assignee, Tags and Types each have a fold arrow');
  assert.deepStrictEqual(lists, {open: 'false', rows: 0, focus: true}, 'a folded group hides its items and keeps the focus on its arrow');
  assert.deepStrictEqual(tags, ['Data 1'], 'a folded group still shows the item that is on');
  assert.deepStrictEqual(who, [], 'and nothing when none of its items is on');
  assert.deepStrictEqual(saved, {lists: 1, tags: 1, who: 1});
  assert.deepStrictEqual(kept, ['false', 'false', 'false'], 'the folds survive a reload in this browser');
  assert.ok(reopened > 0, 'the arrow opens it again');
  assert.deepStrictEqual(errors.concat(again.errors), []);
  w.close(); again.w.close();
  return 'Lists, Assignee and Tags fold like Types, keep the item that is on visible, and remember the fold per browser';
}

(async () => {
  console.log('PASS ' + await everyAssigneeIsAFilterUnderAssignee());
  console.log('PASS ' + await theSmallMenuGroupsFoldAndRememberIt());
})().catch((e) => { console.error('FAIL', e); process.exit(1); });
