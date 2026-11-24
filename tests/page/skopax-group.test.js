const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

async function nestedMergesShowUnderTheTopCardAndCopyLinkCopiesTheCardPage() {
  //given
  const card = (key, extra) => Object.assign({key, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'Typos', src: 'pagescan', sig: 'page-typos', now: 1, firstSeen: iso(30), lastSeen: iso(1)}, extra || {});
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card('page-typos|a', {num: 2, parent: 'page-typos|b', url: 'https://shop.example.com/a', what: '1 typo on https://shop.example.com/a', business: {text: 'Fix the heading on page A.'}}), card('page-typos|b', {num: 3, parent: 'page-typos|c'}), card('page-typos|c', {num: 5}),
      card('page-layout|r', {num: 4, label: 'Layout', triage: 'handled'})],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const errors = [], copied = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/#t=anoms/all', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      Object.defineProperty(w.navigator, 'clipboard', {value: {writeText: async (t) => { copied.push(t); }}});
      w.fetch = async (p) => {
        const ok = (b) => ({ok: true, status: 200, json: async () => b, text: async () => JSON.stringify(b)});
        p = String(p);
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'dora.lane@example.com', operator: true, teams: [], tags: []});
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/nodes')) return ok({nodes: []});
        return ok([]);
      };
    },
  });
  await wait(400);
  const doc = dom.window.document;
  const cards = [...doc.querySelectorAll('#anoms article.inc')].map((c) => c.dataset.key);
  const top = doc.querySelector('#anoms article.inc[data-key="page-typos|c"]');

  //then
  assert.ok(!cards.includes('page-typos|a') && !cards.includes('page-typos|b'), 'nested cards are not separate rows');
  assert.ok(top.querySelector('.inc-head').textContent.includes('+2 merged') || top.textContent.includes('+2 merged'), 'the top card holds both, the nested one too');
  assert.strictEqual(top.querySelector('a.num-link').getAttribute('href'), '#c=5');
  const kid = [...top.querySelectorAll('.kids li')].find((l) => l.textContent.includes('1 typo on'));
  assert.ok(kid && kid.querySelector('a.num-link[href="#c=2"]') && kid.querySelector('a.open-page') && kid.querySelector('.kid-todo').textContent === 'Recommended: Fix the heading on page A.', 'a merged card shows its finding, page link, number and action');

  const acts = top.querySelector('.acts');
  const inv = acts.querySelector('.inv-btn'), tog = acts.querySelector('.card-more');
  assert.ok(inv.closest('.card-more-box') && inv.querySelector('.lbl'), 'Investigate is a labelled item in the More menu');
  assert.ok(acts.querySelector('.card-more-box').hidden && tog.getAttribute('aria-expanded') === 'false', 'the other actions start folded');
  tog.click();
  assert.ok(!acts.querySelector('.card-more-box').hidden && tog.getAttribute('aria-expanded') === 'true', 'More shows them');

  //when
  top.querySelector('.fold').click();
  await wait(30);
  const copy = [...top.querySelectorAll('.acts button')].find((b) => b.textContent.includes('Copy link'));
  const groupOptions = [...top.querySelectorAll('[role="listbox"] [role="option"] .gp-num')].map((o) => o.textContent);
  copy.click();
  await wait(30);

  //then
  assert.deepStrictEqual(copied, ['https://skopax.example.com/c/5']);
  assert.ok(copy.dataset.tip.includes('card #5') || (copy.title || '').includes('card #5'));
  assert.ok(!groupOptions.includes('#4'), 'a resolved card is not offered as a parent');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS Investigate and the other actions fold behind the More menu; cards merged in a chain show under the top card, Copy link copies the card page link, and a resolved card is never offered as a parent');
}

(async () => {
  await nestedMergesShowUnderTheTopCardAndCopyLinkCopiesTheCardPage();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
