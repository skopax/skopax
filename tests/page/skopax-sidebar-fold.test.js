const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const KEY = 'skopax-sidebar-folds';
async function open(hash, folds, width) {
  const errors = [];
  const vc = new VirtualConsole(); vc.on('jsdomError', (e) => errors.push(String(e.message || e).slice(0, 120)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3e6).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const dom = new JSDOM(pageHtml(process.argv[2]).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {url: 'https://skopax.example.com/' + hash, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      if (folds) w.localStorage.setItem(KEY, folds);
      w.localStorage.setItem('skopax-sidebar', width || 'full');
      w.scrollTo = () => {}; w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        p = String(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'nina.white@example.com', operator: true, admin: true, teams: [], tags: [], chat: true});
        if (p.startsWith('/api/admin/settings')) return ok({groups: [], history: []});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(200);
  return {dom, w: dom.window, doc: dom.window.document, errors};
}
const grp = (doc, id) => doc.querySelector('#snav > .s-grp[data-grp="' + id + '"]');
const btn = (doc, id) => grp(doc, id).querySelector('.g-btn');
const seen = (w, doc, tab) => w.getComputedStyle(doc.getElementById('tab-' + tab)).display !== 'none';
async function moreFoldsLikeTheOthers() {
  //given
  const fresh = await open('#t=anoms');
  const doc = fresh.doc, w = fresh.w;
  const label = (x) => btn(x.doc, 'more').getAttribute('aria-expanded');
  const startedFolded = [label(fresh), seen(w, doc, 'review'), seen(w, doc, 'docs')];

  //when
  doc.getElementById('tab-review').classList.add('k-crit', 'hot');
  await wait(60);
  const crit = [seen(w, doc, 'review'), seen(w, doc, 'docs'), btn(doc, 'more').querySelector('.g-sum').textContent, btn(doc, 'more').querySelector('.g-sum').className];
  btn(doc, 'more').click();
  await wait(30);
  const opened = [label(fresh), seen(w, doc, 'docs')];
  const stored = w.localStorage.getItem(KEY);
  const again = await open('#t=anoms', stored);

  //then
  assert.deepStrictEqual(startedFolded, ['false', false, false], 'More starts folded');
  assert.deepStrictEqual(crit, [true, false, '1', 'g-sum hot k-crit'], 'a crit row stays visible in a folded More and the heading counts it');
  assert.deepStrictEqual(opened, ['true', true]);
  assert.deepStrictEqual([label(again), seen(again.w, again.doc, 'docs')], ['true', true], 'an open More stays open after a reload');
  assert.ok(!doc.querySelector('#snav button.tab.more'), 'no second fold mechanism');
  assert.deepStrictEqual(fresh.errors.concat(again.errors), []);
  fresh.w.close(); again.w.close();
  console.log('PASS More folds like the other groups: crit rows stay visible, the heading counts, the open state survives a reload');
}

async function theHeadingFoldsFromTheKeyboardAndTheRailIgnoresIt() {
  //given
  const t = await open('#t=anoms');
  const doc = t.doc, w = t.w;
  const b = btn(doc, 'observe');
  const press = (k, type) => { const e = new w.KeyboardEvent(type || 'keydown', {key: k, bubbles: true, cancelable: true}); b.dispatchEvent(e); return e; };

  //when
  b.focus();
  const enter = press('Enter');
  const afterEnter = b.getAttribute('aria-expanded');
  const space = press(' ');
  press(' ', 'keyup');
  const afterSpace = b.getAttribute('aria-expanded');
  doc.getElementById('tab-lat').classList.add('k-warn');
  doc.getElementById('tab-hist') && doc.getElementById('tab-lat').setAttribute('hidden', '');
  b.click();
  await wait(40);
  const count = b.querySelector('.g-sum').textContent;
  const label = b.title;
  const rail = await open('#t=anoms', null, 'rail');
  const rb = btn(rail.doc, 'observe');
  rb.click();

  //then
  assert.deepStrictEqual([enter.defaultPrevented, afterEnter, space.defaultPrevented, afterSpace], [true, 'false', true, 'true'], 'Enter and Space toggle once');
  assert.strictEqual(count, '4', 'the folded count leaves out the hidden row: ' + count);
  assert.ok(/^Observe folded/.test(label), 'the tooltip names the group, not its id: ' + label);
  assert.deepStrictEqual([rb.getAttribute('tabindex'), rb.getAttribute('aria-expanded'), rb.getAttribute('aria-hidden')], ['-1', 'true', 'true'], 'in the rail the heading is not a tab stop and does not toggle');
  assert.deepStrictEqual(t.errors.concat(rail.errors), []);
  t.w.close(); rail.w.close();
  console.log('PASS the heading folds from Enter and Space, counts only visible rows, names the group in its tooltip, and is inert in the rail');
}

(async () => {
  //given
  const fresh = await open('#t=anoms');
  const doc = fresh.doc, w = fresh.w;
  const defaults = ['observe', 'activity', 'connect', 'more'].map((id) => btn(doc, id).getAttribute('aria-expanded'));
  const mainHasButton = !!grp(doc, 'main').querySelector('.g-btn');

  //when
  btn(doc, 'observe').focus();
  const focused = doc.activeElement === btn(doc, 'observe') && btn(doc, 'observe').tagName === 'BUTTON';
  btn(doc, 'observe').click();
  await wait(30);
  const folded = [btn(doc, 'observe').getAttribute('aria-expanded'), seen(w, doc, 'svcs'), seen(w, doc, 'slack'), seen(w, doc, 'anoms'), btn(doc, 'observe').querySelector('.g-sum').textContent];
  btn(doc, 'observe').click();
  await wait(30);
  const reopened = [btn(doc, 'observe').getAttribute('aria-expanded'), seen(w, doc, 'svcs')];
  btn(doc, 'observe').click();
  await wait(30);
  const stored = w.localStorage.getItem(KEY);

  //then
  assert.deepStrictEqual(defaults, ['true', 'true', 'true', 'false'], 'More folded, the others open');
  assert.strictEqual(mainHasButton, false, 'the unlabelled group stays as it is');
  assert.ok(focused, 'the heading is a focusable button');
  assert.deepStrictEqual(folded, ['false', false, true, true, '5'], 'Observe folds alone and its heading counts its rows');
  assert.deepStrictEqual(reopened, ['true', true]);
  assert.deepStrictEqual(JSON.parse(stored), {observe: 'folded'});

  //when
  const again = await open('#t=anoms', stored);
  const rem = [btn(again.doc, 'observe').getAttribute('aria-expanded'), seen(again.w, again.doc, 'svcs')];

  //then
  assert.deepStrictEqual(rem, ['false', false], 'a reload keeps Observe folded');

  //when
  const onRow = await open('#t=svcs', stored);

  //then
  assert.deepStrictEqual([seen(onRow.w, onRow.doc, 'svcs'), seen(onRow.w, onRow.doc, 'lat')], [true, false], 'the active row stays visible in a folded group');

  //when
  const lat = again.doc.getElementById('tab-lat');
  lat.classList.add('k-crit', 'hot');
  await wait(60);
  const crit = [seen(again.w, again.doc, 'lat'), seen(again.w, again.doc, 'rels'), btn(again.doc, 'observe').querySelector('.g-sum').textContent, btn(again.doc, 'observe').querySelector('.g-sum').className];

  //then
  assert.deepStrictEqual(crit, [true, false, '1', 'g-sum hot k-crit'], 'a crit row stays visible and the heading counts it');

  //when
  const rail = await open('#t=anoms', stored, 'rail');
  const inRail = [seen(rail.w, rail.doc, 'svcs'), seen(rail.w, rail.doc, 'lat')];

  //then
  assert.deepStrictEqual(inRail, [true, true], 'the narrow rail shows every icon');
  assert.deepStrictEqual(fresh.errors.concat(again.errors, onRow.errors, rail.errors), []);
  [fresh, again, onRow, rail].forEach((x) => x.w.close());
  console.log('PASS the labelled sidebar groups fold from a heading button, remember it, keep the active and hot rows visible, and the rail ignores folds');
  await moreFoldsLikeTheOthers();
  await theHeadingFoldsFromTheKeyboardAndTheRailIgnoresIt();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
