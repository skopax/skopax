const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

function card(key, src, sig, extra) {
  return Object.assign({key, src, sig, svc: key.split('|')[1].split('/').pop(), ns: 'web', label: sig, sev: 'warn', status: 'open', now: 1, firstSeen: iso(60), lastSeen: iso(1)}, extra || {});
}

function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'Typos', points: [], at: iso(1)},
    anomalies: [].concat(
      ['a', 'b', 'c', 'd', 'e'].map((x) => card('page-typos|pages//' + x, 'pagescan', 'page-typos')),
      ['f', 'g'].map((x) => card('page-layout|pages//' + x, 'pagescan', 'page-layout')),
      [card('log|members/sms-sender|ERROR', 'logs', 'log', {sev: 'crit'})]),
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(hash) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state()));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/' + hash, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: null, operator: false});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/nodes')) return ok({nodes: []});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(300);
  const doc = dom.window.document;
  return {w: dom.window, doc, errors,
    shown: () => [...doc.querySelectorAll('#anoms article.inc')].map((c) => c.dataset.key),
    chip: (t) => doc.querySelector('#panel-anoms .qf-c[data-type="' + t + '"]'),
    note: () => doc.getElementById('filter-note')};
}


async function aBigListSplitsByTypeAndBigTypesStartFolded() {
  //given
  const {w, doc, errors} = await boot('#t=anoms/open');
  const groups = () => [...doc.querySelectorAll('#anoms details.tgrp')];

  //then
  assert.deepStrictEqual(groups().map((d) => [d.querySelector('.tgrp-n').textContent, d.querySelector('.count').textContent, d.open]),
    [['Log errors', '1', true], ['Typos', '5', false], ['Layout', '2', true]], 'critical type first, five typos folded, small groups open');
  assert.ok(groups()[0].querySelector('.tag.k-crit'), 'a type with a critical card says so on its head');
  assert.strictEqual(doc.querySelectorAll('#anoms article.inc').length, 8, 'every card is still on the page');

  //when
  groups()[1].open = true;
  groups()[1].dispatchEvent(new w.Event('toggle'));
  w.paint(JSON.parse(JSON.stringify(w.D)));
  await wait(20);

  //then
  assert.ok(groups()[1].open, 'a fold the reader opened stays open over a repaint');
  assert.strictEqual(JSON.parse(w.localStorage.getItem('skopax-type-fold'))['open:page-typos'], 1);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a big list splits by type, a big type starts folded and a fold the reader set stays');
}

async function aLinkedCardOpensItsFoldAndATypeFilterKeepsOneList() {
  //given
  const {w, doc, errors} = await boot('#a=' + encodeURIComponent('page-typos|pages//c'));

  //then
  const card = doc.querySelector('#anoms article.inc[data-key="page-typos|pages//c"]');
  assert.ok(card.classList.contains('open'), 'the linked card is expanded');
  assert.ok(card.closest('details.tgrp').open, 'the link opens the fold its card is in');
  w.close();

  //when
  const typed = await boot('#t=anoms/open/page-typos');

  //then
  assert.strictEqual(typed.doc.querySelectorAll('#anoms details.tgrp').length, 0, 'with a type chosen the list stays flat');
  assert.deepStrictEqual(errors, []);
  typed.w.close();
  console.log('PASS a linked card opens its fold and a chosen type keeps one flat list');
}

(async () => {
  await aBigListSplitsByTypeAndBigTypesStartFolded();
  await aLinkedCardOpensItsFoldAndATypeFilterKeepsOneList();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
