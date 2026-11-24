const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const LIVE = 'log|checkout-service|ERROR|boom';
const MUTED = 'log|shop-front|ERROR|quiet';

function state() {
  const card = (key, extra) => Object.assign({key, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'x', src: 'logs', sig: 'log', now: 1, firstSeen: iso(30), lastSeen: iso(1)}, extra || {});
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(LIVE, {ns: 'shop', label: 'ERROR lines'}), card(MUTED, {ns: 'shop', label: 'ERROR lines', muted: true})],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(hash, saved) {
  const errors = [], copied = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state()));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/' + (hash || ''), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      if (saved) w.localStorage.setItem('skopax-tab', saved);
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      Object.defineProperty(w.navigator, 'clipboard', {value: {writeText: (t) => { copied.push(t); return Promise.resolve(); }}});
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'nina.white@example.com', operator: true, teams: [], tags: []});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors, copied};
}

const shown = (doc) => [...doc.querySelectorAll('#anoms article.inc')].map((c) => c.dataset.key).sort();
const pressed = (doc) => [...doc.querySelectorAll('#panel-anoms .qf-c[aria-pressed="true"]')].map((b) => b.dataset.f);
const chip = (doc, f) => doc.querySelector('#panel-anoms .qf-c[data-f="' + f + '"]');

(async () => {
  //given
  const first = await boot('');

  //then
  assert.deepStrictEqual(pressed(first.doc), ['open'], 'a first load opens the Open filter');
  assert.deepStrictEqual(shown(first.doc), [LIVE], 'and shows only the open cards');
  assert.strictEqual(first.doc.getElementById('clear-all'), null, 'there is no way back while on the default');
  assert.deepStrictEqual(first.errors, []);

  //when
  const bare = await boot('#t=anoms');

  //then
  assert.deepStrictEqual(pressed(bare.doc), ['open'], 'a Board link with no filter opens Open');
  assert.deepStrictEqual(shown(bare.doc), [LIVE]);
  assert.strictEqual(bare.w.location.hash, '#t=anoms', 'and the address stays short for the default');

  //when
  const all = await boot('#t=anoms/all');

  //then
  assert.deepStrictEqual(pressed(all.doc), ['all'], 'an old All link still opens All');
  assert.deepStrictEqual(shown(all.doc), [LIVE, MUTED].sort(), 'with every card');
  assert.strictEqual(all.w.location.hash, '#t=anoms/all', 'and the address says so');
  assert.strictEqual(all.doc.getElementById('clear-all').textContent, 'Clear filters \u2715', 'the way back leads to Open');
  const copy = all.doc.getElementById('copy-f');
  assert.ok(copy, 'All offers Copy link');

  //when
  copy.click();
  await wait(30);

  //then
  assert.deepStrictEqual(all.copied, ['https://skopax.example.com/#t=anoms/all'], 'the All link is written with /all');

  //when
  all.doc.getElementById('clear-all').click();
  await wait(30);

  //then
  assert.deepStrictEqual(pressed(all.doc), ['open'], 'Back to Open goes to Open');
  assert.deepStrictEqual(shown(all.doc), [LIVE]);
  assert.strictEqual(all.doc.getElementById('clear-all'), null);
  assert.strictEqual(all.w.location.hash, '#t=anoms');

  //when
  const open = await boot('#t=anoms/open');

  //then
  assert.deepStrictEqual(pressed(open.doc), ['open'], 'an Open link still opens Open');

  //when
  chip(open.doc, 'all').click();
  await wait(30);

  //then
  assert.deepStrictEqual(shown(open.doc), [LIVE, MUTED].sort(), 'the All chip still shows every card');
  assert.strictEqual(open.doc.getElementById('clear-all').textContent, 'Clear filters \u2715');

  //when
  const box = open.doc.getElementById('card-search');
  box.value = 'checkout';
  box.dispatchEvent(new open.w.Event('input', {bubbles: true}));
  await wait(30);
  box.value = '';
  box.dispatchEvent(new open.w.Event('input', {bubbles: true}));
  await wait(30);

  //then
  assert.deepStrictEqual(pressed(open.doc), ['all'], 'clearing a search returns to the filter it started from');

  //when
  const searched = await boot('');
  const sbox = searched.doc.getElementById('card-search');
  sbox.value = 'checkout';
  sbox.dispatchEvent(new searched.w.Event('input', {bubbles: true}));
  await wait(30);
  const during = pressed(searched.doc);
  sbox.value = '';
  sbox.dispatchEvent(new searched.w.Event('input', {bubbles: true}));
  await wait(30);

  //then
  assert.deepStrictEqual(during, ['all'], 'a search shows every card');
  assert.deepStrictEqual(pressed(searched.doc), ['open'], 'clearing it from Open goes back to Open');

  //when
  const fresh = await boot('', 'anoms');
  chip(fresh.doc, 'all').click();
  fresh.doc.getElementById('tab-svcs').click();
  fresh.doc.getElementById('tab-anoms').click();
  await wait(30);

  //then
  assert.deepStrictEqual(pressed(fresh.doc), ['open'], 'the sidebar Board link goes back to Open');

  //when
  chip(fresh.doc, 'all').click();
  fresh.doc.querySelector('a.s-mark').dispatchEvent(new fresh.w.MouseEvent('click', {bubbles: true, cancelable: true, button: 0}));
  await wait(30);

  //then
  assert.deepStrictEqual(pressed(fresh.doc), ['open'], 'so does the logo');
  assert.strictEqual(fresh.w.localStorage.getItem('skopax-tab'), 'anoms', 'and the Board is remembered as the last page');
  assert.deepStrictEqual(fresh.errors, []);
  [first, bare, all, open, searched, fresh].forEach((x) => x.w.close());
  console.log('PASS the Board opens on Open by default, All is explicit in links, and Back to Open leads home');
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
