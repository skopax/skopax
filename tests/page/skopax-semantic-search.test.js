const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const PLACEHOLDER = 'Search by meaning: cards, runs, changes, pages';

const MEANING = {semantic: true, note: null, rebuilding: null, items: [
  {kind: 'cards', title: 'High CPU', sub: '#41 \u00b7 checkout-service', when: '2026-11-20T09:00:00Z', link: '#c=41', match: 'meaning', score: 0.91},
  {kind: 'runs', title: 'why is checkout slow', sub: 'pool exhausted', when: '2026-11-20T10:00:00Z', link: '/api/investigations/run-1', match: 'meaning', score: 0.88},
  {kind: 'changes', title: 'checkout-service', sub: 'reg/app:1 -> reg/app:2', when: '2026-11-20T08:00:00Z', link: '#rel=d1', match: 'meaning', score: 0.84},
  {kind: 'pages', title: 'Pool notes', sub: 'How the pool is sized', when: '', link: '#t=integrations', match: 'meaning', score: 0.82},
  {kind: 'errors', title: 'pool timeout', sub: '', when: '', link: '', match: 'meaning', score: 0.81},
  {kind: 'pages', title: 'Runbook', sub: '', when: '', link: 'https://wiki.example.com/pool', match: 'meaning', score: 0.8},
  {kind: 'pages', title: 'Bad scheme', sub: '', when: '', link: 'javascript:alert(1)', match: 'meaning', score: 0.8},
  {kind: 'cards', title: 'Offsite card', sub: '', when: '', link: 'https://evil.example.com', match: 'meaning', score: 0.8},
  {kind: 'cards', title: 'Unsafe', sub: '', when: '', link: 'javascript:alert(1)', match: 'meaning', score: 0.8},
]};

function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'ok', headline: 'x', points: [], at: iso(1)}, anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(opts) {
  opts = opts || {};
  const errors = [], asked = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state())), {
    url: 'https://skopax.example.com/' + (opts.hash || '#t=anoms'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      if (opts.pack) { w.SKOPAX_I18N = opts.pack; w.document.cookie = 'skopax_lang=' + Object.keys(opts.pack)[0] + '; path=/'; }
      w.localStorage.setItem('skopax-sidebar', 'full');
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/search')) {
          asked.push(Object.fromEntries(new URL('https://x' + p).searchParams.entries()));
          if (opts.status) return {ok: false, status: opts.status, json: async () => ({})};
          return ok(typeof opts.answer === 'function' ? opts.answer(asked[asked.length - 1]) : opts.answer || MEANING);
        }
        if (p.startsWith('/api/history/cards')) return ok({from: iso(10080), to: iso(0), page: 0, size: 50, total: 0, items: [], outcomes: {}});
        if (p.startsWith('/api/notifications/count')) return ok({unread: 0});
        if (p.startsWith('/api/me')) return ok(Object.assign({loginEnabled: true, user: 'alice@example.com', operator: true, admin: false, teams: [], tags: [], chat: true, features: opts.features || {'semantic-search': true}}, {}));
        if (p.startsWith('/api/server')) return ok({plugins: [], tz: 'UTC'});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
      };
    },
  });
  await wait(350);
  return {dom, w: dom.window, doc: dom.window.document, errors, asked};
}

async function search(t, text) {
  const input = t.doc.getElementById('sem-q');
  input.focus();
  input.value = text;
  input.dispatchEvent(new t.w.Event('input', {bubbles: true}));
  await wait(450);
}

const key = (w, el, k, extra) => {
  const e = new w.KeyboardEvent('keydown', Object.assign({key: k, bubbles: true, cancelable: true}, extra || {}));
  el.dispatchEvent(e);
  return e;
};

async function theBoxIsInTheTopBarOnEveryTabWhenTheFeatureIsOn() {
  for (const hash of ['#t=anoms', '#t=integrations', '#t=activity']) {
    //given
    const {dom, doc, errors} = await boot({hash});
    const box = doc.querySelector('.top #sem-find'), input = doc.getElementById('sem-q');

    //then
    assert.ok(box && !box.hidden, hash + ' shows the box');
    assert.strictEqual(input.getAttribute('placeholder'), PLACEHOLDER);
    assert.strictEqual(input.getAttribute('role'), 'combobox');
    assert.strictEqual(input.getAttribute('aria-controls'), 'sem-list');
    assert.strictEqual(input.getAttribute('aria-expanded'), 'false');
    assert.strictEqual(doc.getElementById('sem-count').getAttribute('aria-live'), 'polite');
    assert.deepStrictEqual(errors, []);
    dom.window.close();
  }
  console.log('PASS the search by meaning box is in the top bar of every tab, with combobox semantics and an aria-live count');
}

async function theSlashIsLeftAloneWhileTheBoxIsHiddenByCss() {
  //given
  const {dom, w, doc, errors} = await boot();
  doc.getElementById('sem-find').style.display = 'none';

  //when
  const slash = key(w, doc.body, '/');

  //then
  assert.ok(!slash.defaultPrevented, 'the slash is not taken');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS the slash is left alone while the box is not displayed');
}

async function theBoxIsHiddenWhenTheFeatureIsOffOrUnknown() {
  for (const features of [{'semantic-search': false}, {}, undefined]) {
    //given
    const {dom, w, doc, errors} = await boot({features: features || {}});

    //when
    const slash = key(w, doc.body, '/');

    //then
    assert.ok(doc.getElementById('sem-find').hidden, 'hidden');
    assert.ok(!slash.defaultPrevented, 'the slash does nothing');
    assert.deepStrictEqual(errors, []);
    dom.window.close();
  }
  console.log('PASS the box is hidden and the slash does nothing when the feature is off');
}

async function theSlashFocusesTheBoxExceptWhileTyping() {
  //given
  const {dom, w, doc, errors} = await boot();
  const input = doc.getElementById('sem-q'), other = doc.getElementById('top-q');

  //when
  const slash = key(w, doc.body, '/');
  const focused = doc.activeElement === input;
  input.blur();
  other.focus();
  const typing = key(w, other, '/');
  const stayed = doc.activeElement === other;
  other.blur();
  const modified = key(w, doc.body, '/', {ctrlKey: true});

  //then
  assert.ok(slash.defaultPrevented && focused, 'the slash focuses the box');
  assert.ok(!typing.defaultPrevented && stayed, 'the slash types normally in another field');
  assert.ok(!modified.defaultPrevented, 'Ctrl and slash is left alone');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS the slash focuses the box, but not while typing in another field');
}

async function resultsAreGroupedWithChipsLinksAndACount() {
  //given
  const t = await boot();

  //when
  await search(t, 'pool exhausted');
  const doc = t.doc, groups = [...doc.querySelectorAll('#sem-list [role="group"]')].map((g) => g.querySelector('.sem-g').textContent);
  const rows = [...doc.querySelectorAll('#sem-list [role="option"]')];

  //then
  assert.deepStrictEqual(t.asked.map((a) => a.q), ['pool exhausted']);
  assert.deepStrictEqual(groups, ['Cards', 'Runs', 'Changes', 'Pages', 'Errors']);
  assert.strictEqual(rows.length, 6, 'a link that is not a page link is dropped');
  assert.deepStrictEqual(rows.map((r) => r.getAttribute('href')), ['#c=41', '/api/investigations/run-1', '#rel=d1', '#t=integrations', 'https://wiki.example.com/pool', null]);
  assert.strictEqual(rows[5].tagName, 'DIV', 'an error without a card is plain text');
  assert.strictEqual(rows[4].getAttribute('rel'), 'noopener noreferrer');
  assert.strictEqual(rows[4].getAttribute('target'), '_blank');
  assert.ok(rows.every((r) => r.querySelector('.sem-chip').textContent === 'meaning'));
  assert.strictEqual(rows[1].getAttribute('target'), '_blank');
  assert.strictEqual(rows[0].querySelector('b').textContent, 'High CPU');
  assert.strictEqual(doc.getElementById('sem-count').textContent, '6 results');
  assert.strictEqual(doc.getElementById('sem-q').getAttribute('aria-expanded'), 'true');
  assert.ok(doc.getElementById('sem-note').hidden);
  assert.deepStrictEqual(t.errors, []);
  t.dom.window.close();
  console.log('PASS results are grouped Cards, Runs, Changes, Pages, Errors with a meaning chip, links and an aria-live count');
}

async function pagesAndErrorsShowOnlyWhenPresentAndEmptyGroupsSaySo() {
  //given
  const t = await boot({answer: {semantic: true, note: null, rebuilding: null, items: [MEANING.items[0]]}});

  //when
  await search(t, 'pool exhausted');
  const groups = [...t.doc.querySelectorAll('#sem-list [role="group"]')].map((g) => g.querySelector('.sem-g').textContent);
  const none = [...t.doc.querySelectorAll('#sem-list .sem-none')].length;

  //then
  assert.deepStrictEqual(groups, ['Cards', 'Runs', 'Changes']);
  assert.strictEqual(none, 2);
  assert.strictEqual(t.doc.getElementById('sem-count').textContent, '1 result');
  t.dom.window.close();
  console.log('PASS Pages and Errors appear only when present, and an empty group says None');
}

async function theOffNoteAndTheWordsChipShowWhenSemanticIsFalse() {
  //given
  const answer = {semantic: false, note: 'Search by meaning is off: it is turned off in Admin \u203a Features.', rebuilding: null, items: [{kind: 'cards', title: 'checkout CPU', sub: '', when: '', link: '#a=k1', match: 'words'}]};
  const t = await boot({answer});

  //when
  await search(t, 'checkout');
  const note = t.doc.getElementById('sem-note');

  //then
  assert.ok(!note.hidden);
  assert.strictEqual(note.textContent, 'Search by meaning is off: it is turned off in Admin \u203a Features. Showing word matches.');
  assert.strictEqual(t.doc.querySelector('#sem-list .sem-chip').textContent, 'words');
  t.dom.window.close();
  console.log('PASS the off note and the words chip show when semantic is false');
}

async function theNoteAndThePageStringsTranslate() {
  //given
  const pack = {de: {'Showing word matches.': 'Es werden Worttreffer angezeigt.', 'Cards': 'Karten', 'meaning': 'Bedeutung', '{0} results': '{0} Ergebnisse', 'Search by meaning: cards, runs, changes, pages': 'Nach Bedeutung suchen: Karten'}};
  const answer = {semantic: false, note: 'Die Suche nach Bedeutung ist aus: Grund.', rebuilding: null, items: [{kind: 'cards', title: 'checkout CPU', sub: '', when: '', link: '#a=k1', match: 'words'}, {kind: 'cards', title: 'b', sub: '', when: '', link: '#a=k2', match: 'meaning'}]};
  const t = await boot({answer, pack});

  //when
  await search(t, 'checkout');

  //then
  assert.strictEqual(t.doc.getElementById('sem-q').getAttribute('placeholder'), 'Nach Bedeutung suchen: Karten');
  assert.strictEqual(t.doc.getElementById('sem-note').textContent, 'Die Suche nach Bedeutung ist aus: Grund. Es werden Worttreffer angezeigt.');
  assert.strictEqual(t.doc.querySelector('#sem-list .sem-g').textContent, 'Karten');
  assert.strictEqual(t.doc.getElementById('sem-count').textContent, '2 Ergebnisse');
  t.dom.window.close();
  console.log('PASS the placeholder, note, group names and count translate and the server note is left as sent');
}

async function escapeClosesAndArrowsMoveThroughTheRows() {
  //given
  const t = await boot();
  const input = t.doc.getElementById('sem-q'), pop = t.doc.getElementById('sem-pop');
  await search(t, 'pool exhausted');
  const selected = () => [...t.doc.querySelectorAll('#sem-list [role="option"]')].findIndex((r) => r.getAttribute('aria-selected') === 'true');

  //when
  const first = selected();
  key(t.w, input, 'ArrowDown');
  const second = selected(), active = input.getAttribute('aria-activedescendant');
  key(t.w, input, 'ArrowUp');
  key(t.w, input, 'ArrowUp');
  const wrapped = selected();
  const esc = key(t.w, input, 'Escape');

  //then
  assert.ok(first === 0 && second === 1 && active === 'sem-o-1' && wrapped === 5, 'the arrows move and wrap');
  assert.ok(esc.defaultPrevented && pop.hidden && input.getAttribute('aria-expanded') === 'false', 'Escape closes');
  assert.ok(!input.hasAttribute('aria-activedescendant'));
  assert.strictEqual(t.doc.activeElement, input);
  t.dom.window.close();
  console.log('PASS the arrows move through the rows and Escape closes the list');
}

async function enterOpensTheSelectedRowAndAnOutsideClickCloses() {
  //given
  const t = await boot();
  const input = t.doc.getElementById('sem-q'), pop = t.doc.getElementById('sem-pop');
  await search(t, 'pool exhausted');
  let hit = null;
  t.doc.querySelector('#sem-list [role="option"]').addEventListener('click', (e) => { hit = e.currentTarget.getAttribute('href'); e.preventDefault(); });

  //when
  key(t.w, input, 'Enter');
  const closedByEnter = pop.hidden;
  await search(t, 'pool exhausted again');
  const reopened = !pop.hidden;
  t.doc.body.click();

  //then
  assert.strictEqual(hit, '#c=41');
  assert.ok(closedByEnter && reopened && pop.hidden);
  t.dom.window.close();
  console.log('PASS Enter opens the selected row and a click outside closes the list');
}

async function shortTextSearchesNothingAndFailuresSaySo() {
  //given
  const t = await boot();
  await search(t, 'ab');
  const none = t.asked.length;
  const limited = await boot({status: 429});
  await search(limited, 'pool exhausted');
  const down = await boot({status: 503});
  await search(down, 'pool exhausted');

  //then
  assert.strictEqual(none, 0, 'under three characters nothing is sent');
  assert.ok(limited.doc.getElementById('sem-list').textContent.includes('Too many searches'));
  assert.ok(down.doc.getElementById('sem-list').textContent.includes('Search is not available right now.'));
  assert.deepStrictEqual(t.errors.concat(limited.errors, down.errors), []);
  [t, limited, down].forEach((x) => x.dom.window.close());
  console.log('PASS short text sends nothing and a throttled or failed search says so');
}

async function theClosedViewOffersTheMeaningMatchesWhenNoWordMatches() {
  //given
  const answer = (q) => ({semantic: true, note: null, rebuilding: null, items: q.kinds === 'cards' ? MEANING.items.filter((i) => i.kind === 'cards').slice(0, 1).concat([{kind: 'cards', title: 'More', sub: '', when: '', link: '#c=42', match: 'meaning'}]) : []});
  const t = await boot({hash: '#t=anoms/closed', answer});
  const search = t.doc.getElementById('cl-q');

  //when
  search.value = 'pool leak';
  search.dispatchEvent(new t.w.Event('input', {bubbles: true}));
  await wait(900);
  const hint = t.doc.getElementById('cl-sim');
  const text = hint && hint.textContent;
  hint.click();
  await wait(100);

  //then
  assert.strictEqual(text, 'No word matches. 2 similar by meaning');
  assert.strictEqual(t.doc.getElementById('sem-q').value, 'pool leak');
  assert.ok(!t.doc.getElementById('sem-pop').hidden, 'the search opens with the same text');
  assert.ok(t.asked.some((a) => a.q === 'pool leak' && a.kinds === 'cards'));
  assert.deepStrictEqual(t.errors, []);
  t.dom.window.close();
  console.log('PASS the Closed view offers the meaning matches when no word matches and opens the search with the same text');
}

async function theClosedHintStaysAwayWhenTheFeatureIsOffOrSearchIsDown() {
  //given
  const off = await boot({hash: '#t=anoms/closed', features: {'semantic-search': false}});
  const down = await boot({hash: '#t=anoms/closed', answer: {semantic: false, note: 'off', rebuilding: null, items: [{kind: 'cards', title: 'x', sub: '', when: '', link: '#c=1', match: 'words'}]}});

  //when
  for (const t of [off, down]) {
    const field = t.doc.getElementById('cl-q');
    field.value = 'pool leak';
    field.dispatchEvent(new t.w.Event('input', {bubbles: true}));
  }
  await wait(900);

  //then
  assert.ok(!off.doc.getElementById('cl-sim') && off.asked.length === 0, 'feature off sends nothing');
  assert.ok(!down.doc.getElementById('cl-sim'), 'search down shows no hint');
  off.dom.window.close();
  down.dom.window.close();
  console.log('PASS the Closed hint stays away when the feature is off or the search is down');
}

async function theHeaderStylesUseTheTokensAndHideOnPhones() {
  //given
  const css = require('fs').readFileSync(TEMPLATE, 'utf8');

  //then
  assert.ok(/@media \(max-width: 760px\) \{[^@]*\.sem-find \{ display: none; \}/.test(css), 'below 760 px the box is left to the icon');
  assert.ok(/\.sem-find \{[^}]*var\(--line\)[^}]*var\(--surface\)/.test(css), 'the box uses the colour tokens');
  console.log('PASS the box uses the colour tokens and hides on phones');
}

(async () => {
  await theBoxIsInTheTopBarOnEveryTabWhenTheFeatureIsOn();
  await theBoxIsHiddenWhenTheFeatureIsOffOrUnknown();
  await theSlashFocusesTheBoxExceptWhileTyping();
  await theSlashIsLeftAloneWhileTheBoxIsHiddenByCss();
  await resultsAreGroupedWithChipsLinksAndACount();
  await pagesAndErrorsShowOnlyWhenPresentAndEmptyGroupsSaySo();
  await theOffNoteAndTheWordsChipShowWhenSemanticIsFalse();
  await theNoteAndThePageStringsTranslate();
  await escapeClosesAndArrowsMoveThroughTheRows();
  await enterOpensTheSelectedRowAndAnOutsideClickCloses();
  await shortTextSearchesNothingAndFailuresSaySo();
  await theClosedViewOffersTheMeaningMatchesWhenNoWordMatches();
  await theClosedHintStaysAwayWhenTheFeatureIsOffOrSearchIsDown();
  await theHeaderStylesUseTheTokensAndHideOnPhones();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
