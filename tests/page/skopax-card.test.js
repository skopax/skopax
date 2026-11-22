const pageHtml = require('./skopax-page');
const assert = require('assert');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const CARD_PAGE = path.join(path.dirname(TEMPLATE), 'card.html');
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const AT = '2026-11-09T16:32:00Z';
const {short} = require('./skopax-zone');
const STOPPED = '2026-11-09T18:10:00Z';

const BRAND_BLOB = /(<script type="application\/json" id="brand-data">)[\s\S]*?(<\/script>)/;

const statusOf = (doc) => [doc.getElementById('state'), doc.querySelector('.status')].filter(Boolean).map((e) => e.textContent).join(' \u00b7 ');
function cardHtml(brand, blob) {
  const html = pageHtml(CARD_PAGE, brand ? {brand} : undefined);
  if (blob === 'slot') return html.replace('</main>', '<footer class="attribution"><a id="attribution"></a></footer></main>');
  return blob === 'missing' ? html.replace(BRAND_BLOB, '') : html;
}

async function boot(num, answer, who, brand, blob) {
  const errors = [], hits = [], posts = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(cardHtml(brand, blob), {
    url: 'https://skopax.example.com/c/' + num, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.fetch = async (p, opts) => {
        if (String(p) === '/api/me') return {ok: true, status: 200, json: async () => Object.assign({loginEnabled: true, user: null, operator: false, tags: [{id: 'developer', label: 'Developer'}, {id: 'data', label: 'Data'}, {id: 'infra', label: 'Infrastructure'}, {id: 'analyst', label: 'Analyst'}, {id: 'seo', label: 'SEO'}]}, who || {})};
        if (opts && opts.method === 'POST') { posts.push([String(p), JSON.parse(opts.body)]); return {ok: true, status: 201, json: async () => ({})}; }
        if (String(p).startsWith('/api/jira/search')) { hits.push(String(p)); return {ok: true, status: 200, json: async () => [{key: 'OPS-144', summary: 'SEO metadata fetch cache', status: 'In UAT', developer: 'Bob Stone'}]}; }
        hits.push(String(p));
        const body = answer(String(p));
        if (body === 404) return {ok: false, status: 404, json: async () => ({detail: 'no card'})};
        return {ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body))};
      };
    },
  });
  await wait(100);
  return {w: dom.window, doc: dom.window.document, errors, hits, posts};
}

async function aBusinessCardLeadsWithTheActionAndFoldsTheTechnicalPart() {
  //given
  let card = {num: 482, key: 'page-typos|example.com/orders', svc: 'example.com/orders', label: 'Typos', sev: 'warn', status: 'open', firstSeen: AT, live: true,
    url: 'https://example.com/orders', tags: [{id: 'seo'}], business: {text: 'example.com/orders page: change "Carte" to "Card" in the heading.', why: 'Visitors see the wrong spelling.'},
    what: '1 typo on https://example.com/orders', now: 1, d1: 0, d7: 0, screenshots: [{id: 's1', url: 'https://example.com/orders', viewport: 'phone', at: AT}], note: {text: 'checked the page', at: AT},
    merged: [{num: 483, key: 'page-typos|example.com/cards', svc: 'example.com/cards', label: 'Typos', url: 'https://example.com/cards', business: {text: 'example.com/cards page: change "a" to "b" in the heading.'}}]};
  const {w, doc, errors, hits} = await boot(482, () => card);

  //then
  assert.deepStrictEqual(hits, ['/api/cards/482', '/api/investigator/status']);
  assert.strictEqual(doc.title, '#482 \u00b7 example.com/orders / Typos \u00b7 Skopax');
  assert.strictEqual(doc.getElementById('num').textContent, '#482');
  assert.deepStrictEqual([...doc.querySelectorAll('.head .pill')].map((p) => p.textContent), ['Warning', 'SEO']);
  assert.ok(doc.querySelector('.head').firstElementChild.id === 'state' && doc.getElementById('state').classList.contains('state-pill'), 'the status leads the head row as a large pill');
  assert.strictEqual(doc.querySelector('h1').textContent, 'example.com/orders / Typos');
  assert.strictEqual(statusOf(doc), 'Open \u00b7 first seen ' + short(AT));
  assert.strictEqual(doc.querySelector('.todo h2').textContent, 'Recommended');
  assert.strictEqual(doc.querySelector('.todo p.rec').textContent, card.business.text);
  assert.strictEqual(doc.querySelector('.todo .why').textContent, 'Why: Visitors see the wrong spelling.');
  assert.ok(doc.querySelector('.todo .why').compareDocumentPosition(doc.querySelector('.todo p.rec')) & 4, 'the why shows first');
  assert.ok(!doc.getElementById('card').textContent.includes('To do'));
  assert.strictEqual(doc.getElementById('open-page').getAttribute('href'), 'https://example.com/orders');
  const chat = doc.getElementById('chat-about');
  assert.ok(chat.classList.contains('seg-b') && chat.closest('.act-group.ai') && chat.textContent === 'Chat about this card', 'Chat leads the actions as the first AI segment');
  assert.match(chat.getAttribute('href'), /^\/#t=chat\/new\/\d+$/, 'it opens a new chat with this card attached: ' + chat.getAttribute('href'));
  assert.ok(chat.compareDocumentPosition(doc.querySelector('.act-group.links')) & 4, 'above the other buttons');
  assert.strictEqual(doc.getElementById('board').getAttribute('href'), '/#a=page-typos%7Cexample.com%2Forders');
  assert.strictEqual(doc.getElementById('board').textContent, 'Open on the board');
  assert.strictEqual(doc.querySelector('.merged h2').textContent, 'Merged topics (1)');
  assert.ok(doc.querySelector('.merged .m-todo').textContent.startsWith('Recommended: example.com/cards'));
  assert.strictEqual(doc.querySelector('.merged a[href="/c/483"]').textContent, '#483');
  assert.strictEqual(doc.querySelector('.shots img').getAttribute('src'), '/api/screenshots/s1', 'the stored screenshot object gives its id');
  assert.strictEqual(doc.querySelector('.shot figcaption').textContent, 'Screenshot \u00b7 phone \u00b7 ' + short(AT));
  assert.ok(doc.querySelector('.shot').classList.contains('phone'));
  assert.ok(!doc.querySelector('details.tech').open, 'the technical part starts folded when there is an action');
  assert.ok(doc.querySelector('.tech-body').textContent.includes('Now 1 \u00b7 yesterday 0 \u00b7 last week 0'));
  assert.strictEqual(doc.getElementById('full').getAttribute('href'), '/#a=page-typos%7Cexample.com%2Forders');

  //when
  card = Object.assign({}, card, {status: 'cleared', clearedAt: '2026-11-09T18:10:00Z'});
  doc.querySelector('details.tech').open = true;
  doc.querySelector('details.tech').dispatchEvent(new w.Event('toggle'));
  await w.__cardLoad();

  //then
  assert.strictEqual(statusOf(doc), 'Open \u00b7 stopped ' + short(STOPPED) + ' \u00b7 waiting to be addressed', 'a stopped warn card nobody acted on is still open, as on the board');
  assert.ok(doc.getElementById('state').classList.contains('warn'), 'the status pill carries the severity colour');
  assert.ok(doc.querySelector('details.tech').open, 'an opened technical part stays open on refresh');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a business card shows the action, why, page button, screenshot and status, folds the technical part, and refreshes in place to open and waiting');
}

async function aTechnicalCardOpensItsDetailsAndAMissingOneSaysSo() {
  //given
  const tech = await boot(7, () => ({num: 7, key: 'log|checkout-service|ERROR|x', svc: 'checkout-service', label: 'ERROR lines', sev: 'crit', status: 'open', triage: 'progress', ticket: 'OPS-12', firstSeen: AT, live: true, what: 'NPE'}));
  const gone = await boot(9, () => ({num: 9, key: 'k', svc: 'old', label: 'x', sev: 'info', live: false, lastSeen: AT}));
  const none = await boot(77, () => 404);
  const calm = await boot(11, () => ({num: 11, key: 'i', svc: 'web', label: 'x', sev: 'info', status: 'cleared', firstSeen: AT, clearedAt: AT, live: true}));
  const fixing = await boot(12, () => ({num: 12, key: 'p', svc: 'web', label: 'x', sev: 'warn', status: 'cleared', triage: 'progress', ticket: 'OPS-12', firstSeen: AT, clearedAt: AT, live: true}));
  const dived = await boot(13, () => ({num: 13, key: 'd', svc: 'web', label: 'x', sev: 'crit', status: 'open', triage: 'dived', firstSeen: AT, live: true}));
  const refired = await boot(14, () => ({num: 14, key: 'r', svc: 'web', label: 'x', sev: 'warn', status: 'open', firstSeen: '2026-11-12T04:24:00Z', live: true, last: {type: 'resolve', status: 'done', requestedBy: 'bob@example.com', requestedAt: AT, doneAt: AT}}));

  //then
  assert.ok(tech.doc.querySelector('details.tech').open, 'no action, so the details are the content');
  assert.strictEqual(statusOf(tech.doc), 'In progress \u00b7 OPS-12 \u00b7 first seen ' + short(AT));
  assert.strictEqual(tech.doc.querySelector('.todo'), null);
  assert.strictEqual(statusOf(gone.doc), 'Closed \u00b7 last seen ' + short(AT));
  assert.strictEqual(none.doc.querySelector('.empty').textContent, 'Card #77 not found.');
  assert.strictEqual(statusOf(calm.doc), 'Fixed \u00b7 ' + short(AT), 'a stopped info card is simply fixed');
  assert.strictEqual(statusOf(fixing.doc), 'In progress \u00b7 OPS-12 \u00b7 stopped ' + short(AT), 'a stopped card in progress stays in progress');
  assert.strictEqual(statusOf(dived.doc), 'Open \u00b7 first seen ' + short(AT) + ' \u00b7 investigated \u00b7 waiting to be addressed', 'the investigation is a property of an open card');
  assert.ok(dived.doc.getElementById('state').classList.contains('crit'));
  assert.strictEqual(refired.doc.querySelector('.act-last').textContent, 'Reopened ' + short('2026-11-12T04:24:00Z') + ' \u00b7 fired againBefore that: Resolve \u00b7 done \u00b7 ' + short(AT) + ' \u00b7 bob', 'a re-fired card leads with Reopened, then the resolve before it');
  assert.ok(statusOf(refired.doc).startsWith('Open \u00b7 first seen ' + short('2026-11-12T04:24:00Z')));
  [tech, gone, none, calm, fixing, dived, refired].forEach((x) => { assert.deepStrictEqual(x.errors, []); x.w.close(); });
  console.log('PASS a technical card opens its details, an in-progress one names its ticket, a closed one says when it was last seen, and an unknown number says so');
}

async function operatorsGetTheCardActionsAndOthersAReadOnlyCard() {
  //given
  let card = {num: 7, key: 'log|checkout-service|ERROR|x', svc: 'checkout-service', label: 'ERROR lines', sev: 'warn', status: 'open', firstSeen: AT, live: true, last: null};
  const op = await boot(7, () => card, {user: 'bob@example.com', operator: true, jira: true});
  const guest = await boot(7, () => card);
  const noRole = await boot(7, () => card, {user: 'x@example.com', operator: false});
  const d = op.doc;

  //then
  assert.deepStrictEqual([...d.querySelectorAll('.acts.op button')].map((b) => b.id), ['act-dive', 'act-take', 'act-resolve', 'act-mute', 'take-go', 'resolve-fixed', 'resolve-handled', 'resolve-duplicate', 'resolve-go', 'act-assign', 'assign-go', 'assign-me', 'act-link', 'copy-link', 'link-go']);
  assert.strictEqual(d.getElementById('act-link').textContent, 'Link Jira ticket', 'a card without a ticket offers Link Jira ticket as text');
  assert.strictEqual(d.getElementById('act-dive').getAttribute('aria-label'), 'Investigate');
  assert.deepStrictEqual(['act-take', 'act-resolve', 'act-mute'].map((id) => d.getElementById(id).textContent), ['Start progress', 'Resolve', 'Mute 24 h'], 'the operator buttons are English');
  assert.strictEqual(guest.doc.querySelector('#act-dive'), null);
  assert.ok(guest.doc.querySelector('.act-note a[href="/login?next=%2Fc%2F7"]'), 'a guest gets a login link back to this card');
  assert.strictEqual(noRole.doc.querySelector('.act-note').textContent, 'Actions need the operator role.');
  assert.ok(!noRole.doc.getElementById('act-assign') && !guest.doc.getElementById('act-assign'), 'a viewer is read-only like a guest: no Assign');
  assert.ok(guest.doc.getElementById('copy-link') && noRole.doc.getElementById('copy-link'), 'anyone can copy the link');

  //when
  card = Object.assign({}, card, {last: {type: 'dive', status: 'requested', requestedBy: 'bob@example.com', requestedAt: AT}});
  d.getElementById('act-dive').click();
  await wait(50);

  //then
  assert.deepStrictEqual(op.posts[0], ['/api/actions', {type: 'dive', label: 'Investigate checkout-service / ERROR lines', key: card.key, target: card.key}]);
  assert.strictEqual(d.querySelector('.act-last').textContent, 'Last action: Investigation \u00b7 queued \u00b7 ' + short(AT) + ' \u00b7 bob');
  assert.ok(d.getElementById('act-take').disabled, 'buttons wait while an action is queued');

  //when
  card = Object.assign({}, card, {last: {type: 'dive', status: 'done', requestedBy: 'bob@example.com', requestedAt: AT}});
  await op.w.__cardLoad(true);
  d.getElementById('act-take').click();
  const tk = d.getElementById('take-ticket');
  tk.value = 'seo cache';
  tk.dispatchEvent(new op.w.Event('input'));
  await wait(350);
  const shown = [...d.querySelectorAll('#take-ticket-suggest li')].map((l) => l.textContent);
  d.querySelector('#take-ticket-suggest li[data-i="0"]').dispatchEvent(new op.w.MouseEvent('mousedown', {bubbles: true}));
  const picked = tk.value;
  d.getElementById('take-ticket').value = 'OPS-12';
  d.getElementById('take-why').focus();
  d.getElementById('take-why').value = 'looking into it';
  await op.w.__cardLoad();
  const keptOpen = !d.getElementById('form-take').hidden && d.getElementById('take-ticket').value === 'OPS-12';
  d.getElementById('take-go').click();
  await wait(50);

  //then
  assert.ok(keptOpen, 'a refresh waits while someone types');
  assert.ok(op.hits.includes('/api/jira/search?q=seo%20cache'));
  assert.deepStrictEqual(shown, ['OPS-144 SEO metadata fetch cacheIn UAT \u00b7 Bob Stone']);
  assert.strictEqual(picked, 'OPS-144', 'picking a suggestion fills the key');
  assert.deepStrictEqual(op.posts[1][1], {type: 'take', ticket: 'OPS-12', reason: 'looking into it', label: 'Take checkout-service / ERROR lines with OPS-12', key: card.key, target: card.key});
  [op, guest, noRole].forEach((x) => { assert.deepStrictEqual(x.errors, []); x.w.close(); });
  console.log('PASS operators get Investigate, Take (with Jira search by key or task name), Resolve, Mute and the last action on the card page; a guest gets a login link back, an account without the role a note, and a refresh waits while someone types');
}

async function theCardPageShowsTimesInTheViewersOwnZone() {
  //given
  const {w, doc, errors} = await boot(21, () => ({num: 21, key: 'log|checkout-service|ERROR|x', svc: 'checkout-service', label: 'ERROR lines', sev: 'warn', status: 'open', firstSeen: '2026-07-01T10:00:00Z', live: true}));
  const zone = process.env.TZ === 'Asia/Tokyo' ? '1 Jul 19:00' : '1 Jul 10:00';

  //then
  assert.ok(['UTC', 'Asia/Tokyo'].includes(process.env.TZ), 'the suite runs in UTC or Asia/Tokyo: ' + process.env.TZ);
  assert.strictEqual(statusOf(doc), 'Open \u00b7 first seen ' + zone);
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the card page shows times in the viewer own zone, ' + zone + ' for a card first seen at 10:00 UTC');
}

async function theCardPageHasAHistoryTabWithTheTimeline() {
  //given
  const key = 'log|checkout-service|ERROR|x';
  const hist = {actions: [{id: 'a1', type: 'take', target: key, status: 'done', requestedBy: 'alice@example.com', requestedAt: '2026-07-01T11:00:00Z', doneAt: '2026-07-01T11:00:00Z'}],
    activity: [{id: 'v1', kind: 'tags', who: 'alice@example.com', t: '2026-07-01T12:00:00Z', key, text: 'tagged SEO \u00b7 ' + key}]};
  const {w, doc, errors, hits} = await boot(21, (p) => (p.startsWith('/api/card-history') ? hist : p.startsWith('/api/investigations') ? [] :
    {num: 21, key, svc: 'checkout-service', label: 'ERROR lines', sev: 'warn', status: 'open', firstSeen: '2026-07-01T10:00:00Z', live: true}));
  const tabs = () => [...doc.querySelectorAll('[role="tablist"] [role="tab"]')];
  //when
  tabs()[0].dispatchEvent(new w.KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true}));
  await wait(60);
  //then
  assert.deepStrictEqual(tabs().map((t) => [t.textContent, t.getAttribute('aria-selected')]), [['Details', 'false'], ['History', 'true']]);
  assert.strictEqual(doc.getElementById('cd-details').hidden, true);
  assert.deepStrictEqual([...doc.querySelectorAll('#cd-hist li .h-what')].map((x) => x.textContent), ['Tagged SEO', 'Started progress', 'Fired']);
  assert.ok(hits.some((h) => h === '/api/card-history?key=' + encodeURIComponent(key) + '&limit=100'));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the card page has Details and History tabs and History lists the card timeline newest first');
}

async function theCardTagsUseTheServerLabelsAndAnUnknownIdStaysText() {
  //given
  const odd = '<i>odd</i>';
  const card = {num: 490, key: 'log|checkout-service|ERROR|boom', svc: 'checkout-service', label: 'boom', sev: 'warn', status: 'open', firstSeen: AT, live: true,
    tags: [{id: 'infra'}, {id: odd}, {id: 'seo'}], what: 'x', now: 1, d1: 0, d7: 0};
  const core = [{id: 'developer', label: 'Developer'}, {id: 'data', label: 'Data'}, {id: 'infra', label: 'Infrastructure'}, {id: 'analyst', label: 'Analyst'}];

  //when
  const {w, doc, errors} = await boot(490, () => card, {tags: core});

  //then
  assert.deepStrictEqual([...doc.querySelectorAll('.head .pill')].map((p) => p.textContent), ['Warning', 'Infrastructure', odd, 'seo']);
  assert.strictEqual(doc.querySelectorAll('.head .pill i').length, 0, 'an unknown tag id stays text');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the card page names tags with the server labels only and shows an unknown id as escaped text');
}

async function theCardPageFollowsTheAttributionRule() {
  //given
  const card = {num: 491, key: 'log|checkout-service|ERROR|boom', svc: 'checkout-service', label: 'boom', sev: 'warn', status: 'open', firstSeen: AT, live: true, what: 'x', now: 1, d1: 0, d7: 0};
  const hiddenBrand = {name: 'Acme Ops', slug: 'acme-ops', version: '0.1.0', attribution: null};
  const hidden = await boot(491, () => card, null, hiddenBrand);
  const slotted = await boot(491, () => card, null, hiddenBrand, 'slot');
  const missing = await boot(491, () => card, null, {name: 'Skopax', slug: 'skopax', version: '0.1.0', attribution: {text: '', href: 'https://skopax.io'}}, 'missing');
  const copy = hidden.doc.documentElement.cloneNode(true);
  copy.querySelectorAll('script, style').forEach((s) => s.remove());
  const slot = slotted.doc.getElementById('attribution');

  //then
  assert.deepStrictEqual(hidden.errors, []);
  assert.strictEqual(hidden.doc.getElementById('attribution'), null);
  assert.ok(!/skopax\.io|powered by/i.test(copy.outerHTML), 'no element on the card page carries the line');
  assert.strictEqual(hidden.doc.getElementById('full').textContent, 'The whole Acme Ops');
  assert.strictEqual(hidden.w.BRAND.attribution, null);
  assert.deepStrictEqual(slotted.errors, []);
  assert.strictEqual(slot.textContent, '', 'the card page script leaves an empty attribution slot empty when the brand data says null');
  assert.strictEqual(slot.getAttribute('href'), null);
  slotted.w.close();
  assert.deepStrictEqual(missing.errors, []);
  assert.strictEqual(missing.doc.getElementById('attribution').textContent, 'Skopax 0.5.0 \u00b7 skopax.io');
  assert.strictEqual(missing.doc.getElementById('attribution').getAttribute('href'), 'https://skopax.io');
  hidden.w.close();
  missing.w.close();
  console.log('PASS the card page draws no line for a null attribution and the fallback line when the brand blob is missing');
}

async function sendToRouteListsTheRoutesAndPosts() {
  //given
  const EVIL = '<img src=x onerror="window.__evil=1">';
  const card = {num: 7, key: 'log|checkout-service|ERROR|boom', svc: 'checkout-service', label: 'ERROR lines', sev: 'warn', status: 'open', firstSeen: AT, live: true, tags: [{id: 'seo'}],
    business: {text: 'We recommend checking the checkout page.', why: 'Shoppers cannot pay.'}, what: 'x', now: 1, d1: 0, d7: 0};
  const routes = [{id: 'teams/seo', name: 'seo', tags: ['seo'], tagged: true, business: true, plugin: 'Microsoft Teams'}, {id: 'teams/biz', name: 'biz', tags: ['bizdev'], tagged: true, business: true, plugin: 'Microsoft Teams'},
    {id: 'teams/evil', name: EVIL, tags: [], tagged: false, business: false, plugin: EVIL}, {id: 'teams/mixed', name: 'mixed', tags: ['bizdev'], tagged: false, business: false, plugin: 'Microsoft Teams'}];
  const {w, doc, errors, posts} = await boot(7, () => card, {user: 'nina.white@example.com', operator: true, routes});
  const none = await boot(7, () => card, {user: 'nina.white@example.com', operator: true, routes: []});
  const guest = await boot(7, () => card, {routes});

  //when
  doc.getElementById('act-route').click();
  const listed = [...doc.querySelectorAll('#form-route .route-go')];
  listed[0].click();
  await wait(30);

  //then
  assert.deepStrictEqual(listed.map((b) => b.dataset.route), ['teams/seo', 'teams/evil', 'teams/mixed'], 'the routes that take the card: its tag route first, then the routes that take card keys, also one that names tags');
  assert.strictEqual(listed[0].textContent, 'Microsoft Teams \u00b7 seo');
  assert.strictEqual(listed[1].textContent, EVIL + ' \u00b7 ' + EVIL);
  assert.strictEqual(doc.querySelectorAll('#card img').length, 0, 'a route name is text');
  assert.ok(!doc.getElementById('form-route').hidden);
  assert.deepStrictEqual(posts, [['/api/routes/send', {key: card.key, route: 'teams/seo'}]]);
  assert.strictEqual(doc.getElementById('route-msg').textContent, 'Sent to Microsoft Teams \u00b7 seo');
  assert.strictEqual(none.doc.getElementById('act-route'), null, 'no route, no button');
  assert.strictEqual(guest.doc.getElementById('act-route'), null, 'actions need the operator role');
  assert.strictEqual(w.__evil, undefined);
  assert.deepStrictEqual(errors.concat(none.errors, guest.errors), []);
  w.close(); none.w.close(); guest.w.close();
  console.log('PASS the card page lists the routes that take the card, keeps a route name as text, and posts the card to the one chosen');
}

async function withJiraOffTheCardHidesTheJiraControlsAndTheRouteFormTakesTheFocus() {
  //given
  const card = {num: 9, key: 'no-access|-/argo|applications', svc: 'argo', label: 'Argo CD cannot read applications', sev: 'warn', status: 'open', firstSeen: AT, live: true, cluster: 'east',
    what: 'x', now: 1, d1: 0, d7: 0};
  const routes = [{id: 'teams/ops', name: 'ops', tags: [], tagged: false, business: false, plugin: 'Microsoft Teams'}];
  const off = await boot(9, () => card, {user: 'nina.white@example.com', operator: true, routes, jira: false});
  const on = await boot(9, () => card, {user: 'nina.white@example.com', operator: true, routes, jira: true});

  //when
  const toggle = off.doc.getElementById('act-route');
  const before = toggle.getAttribute('aria-expanded');
  toggle.click();

  //then
  assert.strictEqual(off.doc.querySelector('h1').textContent, 'argo / Argo CD cannot read applications \u00b7 instance east', 'the title names the instance the card belongs to');
  assert.ok(off.doc.getElementById('act-link').textContent === 'Link ticket' && /nothing closes the card/.test(off.doc.getElementById('act-link').title), 'with Jira off a typed key still links, and the tip says nothing closes it');
  assert.ok(!/Jira/.test(off.doc.getElementById('act-take').title) && off.doc.getElementById('take-ticket') === null);
  assert.strictEqual(off.doc.getElementById('assign-who').placeholder, 'Name');
  assert.ok(on.doc.getElementById('act-link') && /Jira/.test(on.doc.getElementById('act-take').title) && on.doc.getElementById('assign-who').placeholder === 'Name, searched in Jira');
  assert.deepStrictEqual([before, toggle.getAttribute('aria-expanded')], ['false', 'true']);
  assert.strictEqual(off.doc.activeElement, off.doc.querySelector('#form-route .route-go'), 'the first route takes the focus');
  assert.deepStrictEqual(off.errors.concat(on.errors), []);
  off.w.close(); on.w.close();
  console.log('PASS with Jira off the card page offers no Jira control, names the card instance, and the route form takes the keyboard focus');
}

async function aClearedNoteFromAPluginShowsAsTextOnTheCard() {
  //given
  const card = {num: 21, key: 'deploy-bad|-/shop', svc: 'shop', label: 'Bad deploy', sev: 'warn', status: 'cleared', triage: 'handled', firstSeen: AT, clearedAt: STOPPED, live: true, what: 'x',
    resolution: {kind: 'fixed', by: 'Fixer', via: 'system', at: STOPPED}, clearNote: {text: 'Rolled back <b>now</b> to 1.4.2', by: 'Fixer <i>x</i>', plugin: 'fixer', at: STOPPED}};
  const plain = Object.assign({}, card, {num: 22, clearNote: undefined});
  const noted = await boot(21, () => card);
  const bare = await boot(22, () => plain);

  //then
  const head = [...noted.doc.querySelectorAll('.tech-body h3')].find((h) => h.textContent.startsWith('Stopped firing'));
  assert.strictEqual(head.textContent, 'Stopped firing \u00b7 Fixer <i>x</i> \u00b7 ' + short(STOPPED));
  assert.strictEqual(head.nextElementSibling.textContent, 'Rolled back <b>now</b> to 1.4.2');
  assert.strictEqual(noted.doc.querySelector('.tech-body b'), null, 'plug-in text stays text');
  assert.ok(![...bare.doc.querySelectorAll('.tech-body h3')].some((h) => h.textContent.startsWith('Stopped firing')));
  assert.deepStrictEqual(noted.errors.concat(bare.errors), []);
  noted.w.close(); bare.w.close();
  console.log('PASS a cleared note a plug-in wrote shows on the card as plain text with who wrote it and when');
}

(async () => {
  await theCardPageShowsTimesInTheViewersOwnZone();
  await theCardPageHasAHistoryTabWithTheTimeline();
  await aClearedNoteFromAPluginShowsAsTextOnTheCard();
  await operatorsGetTheCardActionsAndOthersAReadOnlyCard();
  await aBusinessCardLeadsWithTheActionAndFoldsTheTechnicalPart();
  await aTechnicalCardOpensItsDetailsAndAMissingOneSaysSo();
  await theCardTagsUseTheServerLabelsAndAnUnknownIdStaysText();
  await theCardPageFollowsTheAttributionRule();
  await sendToRouteListsTheRoutesAndPosts();
  await withJiraOffTheCardHidesTheJiraControlsAndTheRouteFormTakesTheFocus();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
