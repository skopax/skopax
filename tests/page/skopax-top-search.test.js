const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const PLACEHOLDER = 'Search cards, services, pages and settings';

function card(key, extra) {
  return Object.assign({key, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'ERROR lines', src: 'logs', sig: 'log', now: 1, ns: 'shop', firstSeen: iso(30), lastSeen: iso(1)}, extra || {});
}

function state(cards) {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'ok', headline: 'x', points: [], at: iso(1)}, anomalies: cards || [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(opts) {
  opts = opts || {};
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state(opts.cards))), {
    url: 'https://skopax.example.com/' + (opts.hash || '#t=anoms'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      if (opts.pack) { w.SKOPAX_I18N = opts.pack; w.document.cookie = 'skopax_lang=' + Object.keys(opts.pack)[0] + '; path=/'; }
      Object.keys(opts.stored || {}).forEach((k) => w.localStorage.setItem(k, opts.stored[k]));
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/notifications/count')) return ok({unread: opts.unread || 0});
        if (p.startsWith('/api/me')) return ok(Object.assign({loginEnabled: true, user: 'alice@example.com', operator: true, admin: false, teams: [], tags: [], chat: true, jiraUrl: opts.jira || ''}, opts.anon ? {user: ''} : {}));
        if (p.startsWith('/api/server')) return ok({plugins: [], tz: 'UTC'});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
      };
    },
  });
  await wait(350);
  return {dom, w: dom.window, doc: dom.window.document, errors};
}

const key = (w, el, k, extra) => el.dispatchEvent(new w.KeyboardEvent('keydown', Object.assign({key: k, bubbles: true, cancelable: true}, extra || {})));

async function theFieldIsInTheTopBarOnEveryPage() {
  for (const hash of ['#t=anoms', '#t=integrations', '#t=activity']) {
    //given
    const {dom, doc, errors} = await boot({hash});
    const field = doc.querySelector('.top #top-find #top-q');

    //then
    assert.ok(field, hash + ' has the field in the top bar');
    assert.strictEqual(field.getAttribute('role'), 'combobox');
    assert.strictEqual(field.getAttribute('aria-expanded'), 'false');
    assert.strictEqual(field.getAttribute('aria-controls'), 'pal-list');
    assert.strictEqual(field.getAttribute('placeholder'), PLACEHOLDER);
    assert.strictEqual(doc.querySelector('#top-find kbd').textContent, 'Ctrl K');
    assert.ok(doc.querySelector('#top-find svg'), 'a magnifier');
    assert.ok(doc.querySelector('.top').compareDocumentPosition(field) & 16);
    assert.deepStrictEqual(errors, []);
    dom.window.close();
  }
  console.log('PASS the search field is in the top bar of every page, with combobox semantics, the hint and the placeholder');
}

async function clickTypingArrowAndTheShortcutOpenThePaletteAndTypingGoesToIt() {
  //given
  const {dom, w, doc, errors} = await boot();
  const field = doc.getElementById('top-q'), pal = doc.getElementById('pal'), inner = doc.getElementById('pal-q');

  //when
  field.focus();
  const onFocus = pal.hidden;
  field.click();
  const opened = !pal.hidden;
  const expanded = field.getAttribute('aria-expanded');
  const drop = pal.classList.contains('drop');
  field.value = 'serv';
  field.dispatchEvent(new w.Event('input', {bubbles: true}));
  const typed = inner.value;
  key(w, field, 'Escape');
  const closed = pal.hidden;
  const after = field.getAttribute('aria-expanded');
  const arrow = new w.KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true, cancelable: true});
  field.dispatchEvent(arrow);
  const byArrow = !pal.hidden;
  key(w, field, 'Escape');
  field.value = 'ch';
  field.dispatchEvent(new w.Event('input', {bubbles: true}));
  const byTyping = !pal.hidden && inner.value === 'ch';
  key(w, field, 'Escape');
  doc.body.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'k', ctrlKey: true, bubbles: true, cancelable: true}));
  const byKey = !pal.hidden;

  //then
  assert.ok(onFocus, 'plain focus opens nothing');
  assert.ok(opened && drop && expanded === 'true', 'a click opens the palette as a dropdown under the field');
  assert.strictEqual(typed, 'serv', 'typing in the field types into the palette');
  assert.ok(closed && after === 'false' && byArrow && arrow.defaultPrevented && byTyping && byKey, 'Escape closes it, ArrowDown, typing and the shortcut open it');
  assert.strictEqual(doc.activeElement, field, 'the typing stays in the top field');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS a click, typing, ArrowDown or the shortcut opens the palette under the field, plain focus does not, and typing goes to the palette');
}

async function tabLeavesTheFieldAndTypingAgainAfterAPickShowsTheDropdown() {
  //given
  const t = await boot({cards: TICKETS});
  const field = t.doc.getElementById('top-q'), pal = t.doc.getElementById('pal');
  const tab = (shiftKey) => { const e = new t.w.KeyboardEvent('keydown', {key: 'Tab', shiftKey: !!shiftKey, bubbles: true, cancelable: true}); field.dispatchEvent(e); return e; };

  //when
  await typeIn(t, 'zzz');
  const plain = tab(false);
  const closedByTab = pal.hidden;
  await typeIn(t, 'zzz');
  const back = tab(true);
  const closedByShiftTab = pal.hidden;
  await typeIn(t, 'ops-31');
  const completing = tab(false);
  const stillOpen = !pal.hidden;
  key(t.w, field, 'Enter');
  await wait(20);
  const picked = pal.hidden;
  await typeIn(t, 'ops');
  const again = !pal.hidden;

  //then
  assert.ok(closedByTab && !plain.defaultPrevented, 'Tab with nothing to complete closes the dropdown and lets the focus move on');
  assert.ok(closedByShiftTab && !back.defaultPrevented, 'so does Shift+Tab');
  assert.ok(completing.defaultPrevented && stillOpen, 'Tab with a completion row completes and stays');
  assert.ok(picked && again, 'after picking a result, typing again shows the dropdown');
  t.dom.window.close();
  console.log('PASS Tab does not trap the keyboard, and typing after a pick opens the dropdown again');
}

async function clicksInsideTheFieldKeepTheDropdownAndOutsideClosesIt() {
  //given
  const t = await boot();
  const field = t.doc.getElementById('top-q'), box = t.doc.getElementById('top-find'), pal = t.doc.getElementById('pal'), scrim = t.doc.getElementById('pal-scrim');
  box.getBoundingClientRect = () => ({left: 100, right: 400, top: 10, bottom: 44, width: 300, height: 34});
  const click = (x, y) => scrim.dispatchEvent(new t.w.MouseEvent('click', {clientX: x, clientY: y, bubbles: true}));

  //when
  field.click();
  field.value = 'serv';
  field.dispatchEvent(new t.w.Event('input', {bubbles: true}));
  click(200, 20);
  const kept = !pal.hidden && t.doc.getElementById('pal-q').value === 'serv' && t.doc.activeElement === field;
  box.querySelector('kbd').click();
  const wrapper = t.doc.activeElement === field;
  click(700, 300);

  //then
  assert.ok(kept, 'a click on the field itself keeps the dropdown and the query');
  assert.ok(wrapper, 'a click on the wrapper focuses the input');
  assert.ok(pal.hidden, 'a click elsewhere closes it');
  t.dom.window.close();
  console.log('PASS a click inside the field keeps the dropdown and a click outside closes it');
}

async function theSidebarHasNoSearchBoxButTheRailKeepsTheIcon() {
  //given
  const wide = await boot();
  const rail = await boot({stored: {'skopax-sidebar': 'rail'}});
  const show = (t) => t.w.getComputedStyle(t.doc.getElementById('search-btn')).getPropertyValue('display');

  //then
  assert.strictEqual(show(wide), 'none', 'the expanded sidebar has no search box');
  assert.notStrictEqual(show(rail), 'none', 'the rail keeps a search icon');
  rail.doc.getElementById('search-btn').click();
  assert.ok(!rail.doc.getElementById('pal').hidden, 'the rail icon opens the palette');
  assert.deepStrictEqual(wide.errors.concat(rail.errors), []);
  wide.dom.window.close();
  rail.dom.window.close();
  console.log('PASS the expanded sidebar has no search box and the rail keeps a search icon that opens the palette');
}

async function thePhoneHasAnIconThatOpensTheModalPalette() {
  //given
  const {dom, doc, errors} = await boot();
  const btn = doc.getElementById('top-find-btn'), pal = doc.getElementById('pal');

  //when
  btn.click();

  //then
  assert.ok(btn && btn.closest('.top') && btn.getAttribute('aria-controls') === 'pal' && /^Search/.test(btn.getAttribute('aria-label')) && btn.querySelector('svg'));
  assert.ok(!pal.hidden && !pal.classList.contains('drop'), 'the icon opens the centred modal');
  assert.ok(/max-width: 760px\)[^@]*\.top-find \{ display: none; \}/.test(require('fs').readFileSync(TEMPLATE, 'utf8')), 'the field is hidden on a phone');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS a phone shows a search icon in the top bar that opens the modal palette');
}

async function thePlaceholderTranslates() {
  //given
  const {dom, doc, errors} = await boot({pack: {de: {'Search cards, services, pages and settings': 'Karten, Services, Seiten und Einstellungen suchen'}}});

  //then
  assert.strictEqual(doc.getElementById('top-q').getAttribute('placeholder'), 'Karten, Services, Seiten und Einstellungen suchen');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  console.log('PASS the placeholder translates with a pack');
}


const TICKETS = [
  card('log|checkout-service|ERROR|a', {num: 14, ticket: 'OPS-3130'}),
  card('log|cart-service|ERROR|b', {num: 15, ticket: 'OPS-3101'}),
  card('log|auth-service|ERROR|c', {num: 16, ticket: 'OPS-3188', status: 'cleared', sev: 'info'}),
  card('log|search-api|ERROR|d', {num: 17, ticket: 'OPS-1310'}),
  card('log|payments-api|ERROR|e', {num: 18}),
];

async function typeIn(t, text) {
  const field = t.doc.getElementById('top-q');
  field.focus();
  field.value = text;
  field.dispatchEvent(new t.w.Event('input', {bubbles: true}));
  await wait(20);
}
const sugg = (doc) => [...doc.querySelectorAll('#pal-g-tickets ~ .pal-o')].map((o) => o.querySelector('.lbl').textContent);
const cardRows = (doc) => [...doc.querySelectorAll('#pal-g-cards ~ .pal-o')].map((o) => o.querySelector('.lbl').textContent);

async function aTicketPrefixListsMatchingCardsAndCompletionsRankedAndHighlighted() {
  //given
  const t = await boot({cards: TICKETS, jira: 'https://jira.example.com'});

  //when
  await typeIn(t, 'ops-31');
  const completions = sugg(t.doc);
  const rows = cardRows(t.doc);
  const marked = [...t.doc.querySelectorAll('#pal-g-tickets ~ .pal-o mark')].map((m) => m.textContent);
  const opens = [...t.doc.querySelectorAll('#pal-g-tickets ~ .pal-o')].map((o) => o.querySelector('.lbl').textContent).filter((x) => /in Jira/.test(x));

  //then
  assert.deepStrictEqual(completions.slice(0, 3), ['OPS-3101', 'OPS-3130', 'OPS-3188'], 'prefix matches, open and newest first');
  assert.deepStrictEqual(opens, ['Open OPS-3101 in Jira', 'Open OPS-3130 in Jira', 'Open OPS-3188 in Jira'], 'each ticket has its own open row');
  assert.ok(rows[0].startsWith('OPS-3101') && rows[0].includes('#15') && rows[0].includes('cart-service'), rows.join('|'));
  assert.deepStrictEqual(rows.map((r) => r.slice(0, 8)), ['OPS-3101', 'OPS-3130', 'OPS-3188']);
  assert.ok(marked.length && marked.every((m) => m.toLowerCase() === 'ops-31'), 'the typed part is highlighted: ' + marked);
  assert.strictEqual(t.doc.querySelector('.pal-tk'), null, 'no link nested in an option');
  assert.deepStrictEqual(t.errors, []);
  t.dom.window.close();
  console.log('PASS a ticket prefix lists the matching cards and completions, ranked and highlighted, with open-ticket rows');
}

async function aBareNumberMatchesTheNumberPartAndPrefixBeatsSubstring() {
  //given
  const t = await boot({cards: TICKETS});

  //when
  await typeIn(t, '31');
  const rows = cardRows(t.doc).map((r) => r.slice(0, 8));

  //then
  assert.deepStrictEqual(rows, ['OPS-3101', 'OPS-3130', 'OPS-3188', 'OPS-1310']);
  assert.ok(!sugg(t.doc).some((x) => /in Jira/.test(x)), 'no open rows without a Jira address');
  t.dom.window.close();

  //given
  const lower = await boot({cards: TICKETS});

  //when
  await typeIn(lower, 'OPS-13');

  //then
  assert.deepStrictEqual(cardRows(lower.doc).map((r) => r.slice(0, 8)), ['OPS-1310']);
  lower.dom.window.close();
  console.log('PASS a bare number matches the number part of tickets, prefix before substring, case-insensitive');
}

async function tabCompletesTheFieldToTheFullKeyAndEnterOpensTheCard() {
  //given
  const t = await boot({cards: TICKETS});
  const field = t.doc.getElementById('top-q'), pal = t.doc.getElementById('pal');

  //when
  await typeIn(t, 'ops-31');
  key(t.w, field, 'Tab');
  await wait(20);
  const completed = field.value;
  const innerValue = t.doc.getElementById('pal-q').value;
  key(t.w, field, 'Enter');
  await wait(20);

  //then
  assert.strictEqual(completed, 'OPS-3101');
  assert.strictEqual(innerValue, 'OPS-3101');
  assert.ok(pal.hidden, 'Enter opens the card and closes the palette');
  const linked = t.doc.querySelector('#anoms article.inc.linked');
  assert.ok(linked && linked.dataset.key === 'log|cart-service|ERROR|b', 'the card is opened');
  t.dom.window.close();
  console.log('PASS Tab completes the field to the full ticket key and Enter opens the card');
}

async function noTicketMatchShowsNoSuggestions() {
  //given
  const t = await boot({cards: TICKETS});

  //when
  await typeIn(t, 'ops-99');

  //then
  assert.deepStrictEqual(sugg(t.doc), []);
  assert.ok(/Nothing matches/.test(t.doc.getElementById('pal-list').textContent));
  t.dom.window.close();
  console.log('PASS a ticket-like query with no match shows no suggestions');
}

async function theBellIsInTheTopBarWithTheUnreadCountAndOpensNotifications() {
  //given
  const t = await boot({unread: 3});
  const bell = t.doc.getElementById('top-bell'), badge = t.doc.getElementById('top-bell-badge');
  const picked = [];
  t.w.pickTab = (id) => picked.push(id);

  //when
  bell.click();

  //then
  assert.ok(bell.closest('.top-r') && bell.classList.contains('ibtn') && bell.querySelector('svg') && bell.nextElementSibling.id === 'theme-btn');
  assert.ok(!bell.hidden && !badge.hidden && badge.textContent.trim().startsWith('3') && /unread/.test(badge.textContent));
  assert.strictEqual(bell.getAttribute('aria-label'), 'Notifications, 3 unread');
  assert.strictEqual(t.doc.getElementById('nt-badge').firstChild.textContent, '3', 'the sidebar badge agrees');
  assert.deepStrictEqual(picked, ['slack']);
  t.dom.window.close();

  //given
  const none = await boot({unread: 0});
  const many = await boot({unread: 25});

  //then
  assert.ok(!none.doc.getElementById('top-bell').hidden && none.doc.getElementById('top-bell-badge').hidden);
  assert.strictEqual(none.doc.getElementById('top-bell').getAttribute('aria-label'), 'Notifications');
  assert.strictEqual(many.doc.getElementById('top-bell-badge').firstChild.textContent, many.doc.getElementById('nt-badge').firstChild.textContent);
  none.dom.window.close();
  many.dom.window.close();

  //given
  const out = await boot({anon: true, unread: 5});

  //then
  assert.ok(!out.doc.getElementById('top-bell').hidden && out.doc.getElementById('top-bell-badge').hidden, 'a signed-out page still has the bell, without a badge');
  assert.deepStrictEqual(out.errors, []);
  out.dom.window.close();
  console.log('PASS the top bar bell shows the unread count like the sidebar badge, opens Notifications and stays when signed out without a badge');
}

async function theFoldedActivitySummaryCountsUnreadNotificationsNotDrafts() {
  //given
  const draft = card('log|checkout-service|ERROR|a', {num: 14, slackDraft: {text: 'Draft', at: iso(5), by: 'pod'}});
  const quiet = await boot({cards: [draft], unread: 0});
  const loud = await boot({cards: [draft], unread: 2});
  const fold = (t) => {
    const box = t.doc.querySelector('#snav > .s-grp[data-grp="activity"]');
    if (!box.classList.contains('folded')) box.querySelector('.g-btn').click();
    return box;
  };
  const read = (box) => ({cls: box.querySelector('.g-sum').className, text: box.querySelector('.g-sum').textContent, title: box.querySelector('.g-btn').title});

  //when
  const q = read(fold(quiet));
  const l = read(fold(loud));

  //then
  assert.ok(!/hot/.test(q.cls) && !/Notifications/.test(q.title), 'drafts alone make nothing hot: ' + JSON.stringify(q));
  assert.ok(/hot/.test(l.cls) && l.title.includes('Notifications 2'), 'unread makes it hot and named: ' + JSON.stringify(l));
  assert.ok(!loud.doc.getElementById('nt-badge').hidden && loud.doc.getElementById('nt-badge').firstChild.textContent === '2', 'the expanded row shows the same unread badge');
  assert.deepStrictEqual(quiet.errors.concat(loud.errors), []);
  quiet.dom.window.close();
  loud.dom.window.close();
  console.log('PASS the folded Activity summary counts unread notifications, never the hidden drafts');
}

async function anOpenTicketRowOpensTheJiraLinkAndAUnsafeBaseGivesNone() {
  //given
  const t = await boot({cards: TICKETS, jira: 'https://jira.example.com'});
  const opened = [];
  t.w.HTMLAnchorElement.prototype.click = function () { opened.push(this.href + '|' + this.target + '|' + this.rel); };

  //when
  await typeIn(t, 'ops-3130');
  const rows = [...t.doc.querySelectorAll('#pal-g-tickets ~ .pal-o')];
  const open = rows.filter((o) => /Open OPS-3130 in Jira/.test(o.textContent))[0];
  open.click();
  await wait(20);

  //then
  assert.ok(open && open.getAttribute('role') === 'option');
  assert.deepStrictEqual(opened, ['https://jira.example.com/browse/OPS-3130|_blank|noopener noreferrer']);
  t.dom.window.close();

  //given
  const bad = await boot({cards: TICKETS, jira: 'javascript:alert(1)'});

  //when
  await typeIn(bad, 'ops-31');

  //then
  assert.ok(!sugg(bad.doc).some((x) => /in Jira/.test(x)) && sugg(bad.doc).length === 3, 'a base that is not http(s) gives no open rows');
  bad.dom.window.close();
  console.log('PASS an open-ticket row opens the Jira link, and an unsafe base gives none');
}

async function theBellGoesToNotificationsWhenSignedInAndToLoginWhenSignedOut() {
  //given
  const inn = await boot({unread: 1});
  const out = await boot({anon: true});
  const went = [];
  out.w.__skopaxGo = (u) => went.push(u);
  const picked = [];
  inn.w.pickTab = (id) => picked.push(id);
  out.w.pickTab = (id) => picked.push('out:' + id);

  //when
  inn.doc.getElementById('top-bell').click();
  out.doc.getElementById('top-bell').click();

  //then
  assert.deepStrictEqual(picked, ['slack']);
  assert.ok(/^\/login\?next=/.test(went[0]), went[0]);
  inn.dom.window.close();
  out.dom.window.close();
  console.log('PASS the bell opens Notifications when signed in and goes to login when signed out');
}

async function theTopBarNarrowsAndSwitchesToTheIconBelowTheWidthsItFits() {
  //given
  const css = require('fs').readFileSync(TEMPLATE, 'utf8');

  //then
  assert.ok(/@media \(max-width: 900px\) \{[^@]*\.top-find \{[^}]*min-width: 140px[^}]*\}[^@]*\.top \.last-check \{ display: none; \}/.test(css), 'below 900 px the field narrows and the last check hides');
  assert.ok(/@media \(max-width: 760px\) \{[^@]*\.top-find \{ display: none; \}[^@]*#top-find-btn \{ display: inline-grid; \}/.test(css), 'below 760 px the icon replaces the field');
  console.log('PASS the top bar narrows below 900 px and uses the icon below 760 px');
}

(async () => {
  await theFieldIsInTheTopBarOnEveryPage();
  await clickTypingArrowAndTheShortcutOpenThePaletteAndTypingGoesToIt();
  await tabLeavesTheFieldAndTypingAgainAfterAPickShowsTheDropdown();
  await clicksInsideTheFieldKeepTheDropdownAndOutsideClosesIt();
  await theSidebarHasNoSearchBoxButTheRailKeepsTheIcon();
  await thePhoneHasAnIconThatOpensTheModalPalette();
  await thePlaceholderTranslates();
  await aTicketPrefixListsMatchingCardsAndCompletionsRankedAndHighlighted();
  await aBareNumberMatchesTheNumberPartAndPrefixBeatsSubstring();
  await tabCompletesTheFieldToTheFullKeyAndEnterOpensTheCard();
  await noTicketMatchShowsNoSuggestions();
  await anOpenTicketRowOpensTheJiraLinkAndAUnsafeBaseGivesNone();
  await theBellGoesToNotificationsWhenSignedInAndToLoginWhenSignedOut();
  await theTopBarNarrowsAndSwitchesToTheIconBelowTheWidthsItFits();
  await theBellIsInTheTopBarWithTheUnreadCountAndOpensNotifications();
  await theFoldedActivitySummaryCountsUnreadNotificationsNotDrafts();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
