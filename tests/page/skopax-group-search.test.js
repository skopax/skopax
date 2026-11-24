const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

const card = (num, svc, extra) => Object.assign({key: 'chk|' + num, num, sev: 'warn', status: 'open', svc, ns: 'ns-' + svc, label: 'Latency ' + num, src: 'prom', sig: 'sig-' + num, now: 1, firstSeen: iso(900), lastSeen: iso(1)}, extra || {});

function baseCards() {
  return [
    card(10, 'shop', {ns: 'shop-prod', firstSeen: iso(120)}),
    card(11, 'shop', {ns: 'other', firstSeen: iso(1000)}),
    card(12, 'cart', {ns: 'shop-prod', firstSeen: iso(800)}),
    card(13, 'pay', {firstSeen: iso(110)}),
    card(14, 'zed', {sev: 'crit', firstSeen: iso(3000)}),
    card(15, 'yak', {firstSeen: iso(2000), ticket: 'OPS-123'}),
    card(16, 'xan', {firstSeen: iso(500), owner: 'alice@example.com', ownerName: 'Alice Stone'}),
    card(17, 'i17', {sev: 'info', firstSeen: iso(600)}),
    card(18, 'i18', {sev: 'info', firstSeen: iso(700)}),
    card(19, 'i19', {sev: 'info', firstSeen: iso(800)}),
    card(20, 'i20', {sev: 'info', firstSeen: iso(900), label: 'Caf\u00e9 queue', tags: [{id: 'billing', at: iso(5), by: 'rule'}]}),
    card(21, 'i21', {sev: 'info', firstSeen: iso(1000)}),
    card(22, 'i22', {sev: 'info', firstSeen: iso(1100)}),
  ];
}

async function open(cards, lang) {
  let pending = false;
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: cards, services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const errors = [], posts = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/#t=anoms/all', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      if (lang) {
        w.document.cookie = 'skopax_lang=' + lang + '; path=/';
        w.SKOPAX_I18N = {[lang]: require('../../skopax/locale/' + lang + '/page.json').page};
      }
      w.fetch = async (p, o) => {
        const ok = (b) => ({ok: true, status: 200, json: async () => b, text: async () => JSON.stringify(b)});
        p = String(p);
        if (o && o.method === 'POST' && p.startsWith('/api/actions')) { posts.push(JSON.parse(o.body)); pending = true; return ok({id: 'a1'}); }
        if (p.startsWith('/api/actions') && pending) return ok([{id: 'a1', type: 'group', target: 'chk|10', status: 'requested', requestedAt: iso(0)}]);
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'dora.lane@example.com', operator: true, teams: [], tags: []});
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/nodes')) return ok({nodes: []});
        return ok([]);
      };
    },
  });
  await wait(400);
  const doc = dom.window.document;
  const top = doc.querySelector('#anoms article.inc[data-key="chk|10"]');
  top.querySelector('.fold').click();
  await wait(30);
  top.querySelector('.acts .card-more').click();
  const form = top.querySelector('.reason input[role="combobox"]').closest('.reason');
  const input = form.querySelector('input[role="combobox"]');
  const rows = () => [...form.querySelectorAll('[role="listbox"] [role="option"]')];
  const nums = () => rows().map((r) => r.querySelector('.gp-num').textContent);
  const type = (text) => { input.value = text; input.dispatchEvent(new dom.window.Event('input', {bubbles: true})); };
  const key = (k) => input.dispatchEvent(new dom.window.KeyboardEvent('keydown', {key: k, bubbles: true, cancelable: true}));
  const ok = form.querySelector('button');
  const toggle = top.querySelector('.card-more-box .grp-btn');
  toggle.click();
  return {dom, doc, top, form, input, rows, nums, type, key, ok, errors, posts};
}

async function suggestionsListSameServiceThenNamespaceThenTimeThenSeverity() {
  //given
  const t = await open(baseCards());

  //when
  t.input.focus();
  t.input.dispatchEvent(new t.dom.window.Event('focus'));
  await wait(20);

  //then
  assert.deepStrictEqual(t.nums(), ['#11', '#12', '#13', '#14', '#15', '#16', '#22', '#21']);
  assert.strictEqual(t.input.getAttribute('role'), 'combobox');
  assert.strictEqual(t.input.getAttribute('aria-expanded'), 'true');
  assert.strictEqual(t.input.getAttribute('aria-controls'), t.form.querySelector('[role="listbox"]').id);
  assert.ok(t.form.querySelector('.gp-more').textContent.includes('4 more - type to search'));
  assert.ok(t.ok.disabled, 'Group waits for a row');
  assert.deepStrictEqual(t.errors, []);
  t.dom.window.close();
  console.log('PASS the Group picker lists same service, then same namespace, then cards opened close in time, then the rest by severity and age, 8 rows and a more line');
}

async function typingFiltersByNumberServiceTicketOwnerTagAndAccents() {
  //given
  const t = await open(baseCards());
  const found = (q) => { t.type(q); return t.nums(); };

  //when
  //then
  assert.deepStrictEqual(found('#14'), ['#14']);
  assert.deepStrictEqual(found('14'), ['#14']);
  assert.deepStrictEqual(found('YAK'), ['#15']);
  assert.deepStrictEqual(found('ops-123'), ['#15']);
  assert.deepStrictEqual(found('alice stone'), ['#16']);
  assert.deepStrictEqual(found('cafe'), ['#20']);
  assert.deepStrictEqual(found('billing'), ['#20']);
  assert.deepStrictEqual(found('ns-zed'), ['#14']);
  assert.deepStrictEqual(t.errors, []);
  t.dom.window.close();
  console.log('PASS typing filters the Group picker by #number, plain number, service, ticket key, owner, tag, namespace, ignoring case and accents');
}

async function keyboardPicksAndGroupQueuesTheSameActionAsBefore() {
  //given
  const t = await open(baseCards());
  t.input.focus();
  t.input.dispatchEvent(new t.dom.window.Event('focus'));
  await wait(20);

  //when
  t.key('ArrowDown');
  t.key('ArrowDown');
  const active = t.input.getAttribute('aria-activedescendant');
  t.key('Enter');
  await wait(20);

  //then
  assert.ok(active && t.doc.getElementById(active).textContent.includes('#12'));
  assert.ok(!t.ok.disabled, 'a picked row enables Group');
  assert.strictEqual(t.input.getAttribute('aria-expanded'), 'false');

  //when
  t.ok.click();
  await wait(50);

  //then
  assert.deepStrictEqual(t.posts, [{type: 'group', target: 'chk|10', key: 'chk|10', parent: 'chk|12', label: 'Group shop Latency 10 under cart Latency 12'}]);
  assert.deepStrictEqual(t.errors, []);
  t.dom.window.close();
  console.log('PASS arrow keys and Enter pick a parent and Group queues the same group action as before');
}

async function clickPicksAndTypingAgainClearsThePick() {
  //given
  const t = await open(baseCards());
  t.input.focus();
  t.input.dispatchEvent(new t.dom.window.Event('focus'));
  await wait(20);

  //when
  t.rows()[3].dispatchEvent(new t.dom.window.MouseEvent('click', {bubbles: true}));

  //then
  assert.ok(!t.ok.disabled);
  assert.ok(t.input.value.includes('#14'));

  //when
  t.type('zzz');

  //then
  assert.ok(t.ok.disabled, 'editing the text drops the pick');
  t.dom.window.close();
  console.log('PASS a click picks a row and editing the text drops the pick');
}

async function groupStaysDisabledWhileTheActionIsPending() {
  //given
  const t = await open(baseCards());
  t.input.focus();
  t.input.dispatchEvent(new t.dom.window.Event('focus'));
  await wait(20);
  t.rows()[1].dispatchEvent(new t.dom.window.MouseEvent('click', {bubbles: true}));
  t.ok.click();
  await wait(100);

  //then
  assert.ok(t.ok.disabled, 'the sync disables Group while the action is pending');

  //when
  t.type('#14');
  t.rows()[0].dispatchEvent(new t.dom.window.MouseEvent('click', {bubbles: true}));

  //then
  assert.ok(t.ok.disabled, 'picking again does not re-enable Group while pending');
  assert.strictEqual(t.posts.length, 1);
  t.dom.window.close();
  console.log('PASS Group stays disabled after a pick while the group action on the card is pending');
}

async function escClosesTheListThenLeavesTheField() {
  //given
  const t = await open(baseCards());
  t.input.focus();
  t.input.dispatchEvent(new t.dom.window.Event('focus'));
  await wait(20);
  const more = t.top.querySelector('.acts .card-more');
  assert.strictEqual(t.doc.activeElement, t.input);

  //when
  t.key('Escape');

  //then
  assert.strictEqual(t.input.getAttribute('aria-expanded'), 'false');
  assert.ok(t.form.querySelector('[role="listbox"]').hidden);
  assert.ok(!t.form.hidden, 'the first Esc keeps the form');
  assert.strictEqual(t.doc.activeElement, t.input, 'focus stays in the field');

  //when
  t.key('Escape');

  //then
  assert.ok(t.form.hidden, 'the second Esc hides the form');
  assert.strictEqual(t.doc.activeElement, more, 'focus returns to More, which holds Group');
  t.dom.window.close();
  console.log('PASS Esc closes the list and a second Esc leaves the field');
}

async function noMatchShowsAMessageAndEscapesLabels() {
  //given
  const cards = baseCards();
  cards.push(card(23, '-', {ns: 'billing-ns', label: '<img src=x onerror=alert(1)>', firstSeen: iso(1200)}));
  const t = await open(cards);

  //when
  t.type('qqqq');

  //then
  assert.strictEqual(t.rows().length, 0);
  assert.ok(t.form.querySelector('.gp-empty').textContent.includes('No card matches'));

  //when
  t.type('#23');

  //then
  const row = t.rows()[0];
  assert.ok(row && !row.querySelector('img'), 'a label with HTML stays text');
  assert.ok(row.textContent.includes('<img src=x onerror=alert(1)>'));
  assert.ok(row.querySelector('.gp-name').textContent === 'billing-ns', 'name falls back to the namespace');
  assert.deepStrictEqual(t.errors, []);
  t.dom.window.close();
  console.log('PASS a search without hits says so, a label with HTML is shown as text and the name falls back to the namespace');
}

async function rowsMarkStoppedCardsAndShowAge() {
  //given
  const cards = baseCards();
  cards[0].status = 'cleared';
  cards[1].status = 'cleared';
  const t = await open(cards);

  //when
  t.type('#11');

  //then
  const row = t.rows()[0];
  assert.ok(row.querySelector('.gp-stop'), 'a stopped card is marked');
  assert.ok(/\d/.test(row.querySelector('.gp-age').textContent));
  assert.ok(row.querySelector('.sev'), 'a severity chip is shown');
  t.dom.window.close();
  console.log('PASS rows carry the severity chip, the stopped mark and the age');
}

async function placeholderAndMoreLineAreTranslated() {
  //given
  const t = await open(baseCards(), 'tr');

  //when
  t.input.focus();
  t.input.dispatchEvent(new t.dom.window.Event('focus'));
  await wait(60);

  //then
  assert.strictEqual(t.input.getAttribute('placeholder'), 'Numara, servis, etiket veya ticket ile ara');
  assert.strictEqual(t.form.querySelector('[role="listbox"]').getAttribute('aria-label'), 'Gruplanacak kartlar');
  assert.strictEqual(t.form.querySelector('.gp-more').textContent, '4 tane daha - aramak i' + '\u00e7in yaz\u0131n');
  t.dom.window.close();
  console.log('PASS the placeholder and the more line follow the page language');
}

(async () => {
  await suggestionsListSameServiceThenNamespaceThenTimeThenSeverity();
  await typingFiltersByNumberServiceTicketOwnerTagAndAccents();
  await keyboardPicksAndGroupQueuesTheSameActionAsBefore();
  await clickPicksAndTypingAgainClearsThePick();
  await groupStaysDisabledWhileTheActionIsPending();
  await escClosesTheListThenLeavesTheField();
  await noMatchShowsAMessageAndEscapesLabels();
  await rowsMarkStoppedCardsAndShowAge();
  await placeholderAndMoreLineAreTranslated();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
