const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const CORE_PACK = /^CORE_PACK = '(.+)'$/m.exec(fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'locales.py'), 'utf8'))[1];
const SLOT = '<script type="text/plain" id="plugin-scripts"></script>';
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const CARD = 'log|checkout-service|ERROR|boom';

function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: iso(-59), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [{key: CARD, sev: 'warn', status: 'open', svc: 'checkout-service', label: 'ERROR lines', src: 'logs', sig: 'log', now: 1, firstSeen: iso(90), lastSeen: iso(1), num: 14}],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

function withHeld(st) {
  const a = st.anomalies[0];
  a.slackDraft = {text: 'ERROR lines went up.', by: 'Claude', at: iso(5)};
  a.autoSend = {at: iso(30), draftAt: a.slackDraft.at, draftHash: 'x', claimedAt: iso(1), failedAt: iso(1), retries: 3, error: 'channel_not_found'};
  return st;
}
function rows(total, unread) {
  const kinds = ['assigned', 'resolved', 'answer', 'fired'];
  return Array.from({length: total}, (_, i) => {
    const kind = kinds[i % kinds.length];
    return kind === 'answer'
      ? {id: 'n' + i, kind, key: '', num: null, title: 'Why did it go up?', note: 'One client.', by: 'Investigator', link: '#t=chat/thread/c9', t: iso(i + 2), read: i >= unread}
      : {id: 'n' + i, kind, key: CARD, num: 14, title: 'checkout-service / ERROR lines', note: kind === 'resolved' ? 'Fixed \u00b7 OPS-12' : '', by: 'paul.stone', t: iso(i + 2), read: i >= unread};
  });
}
function builtIn(lang) {
  const raw = JSON.parse(fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'locale', lang, 'page.json'), 'utf8'));
  return Object.assign({}, raw.page, {'@rules': raw.rules, '@months': raw.months, '@days': raw.days, '@tails': raw.tails, '@th': raw.headers});
}
function served(lang) {
  const args = [JSON.stringify(lang), JSON.stringify(builtIn(lang)).replace(/</g, '\\u003c')];
  return '<script>' + CORE_PACK.replace(/%s/g, () => args.shift()) + '</script>';
}
const newcomer = (id) => ({id, kind: 'fired', key: CARD, num: 14, title: 'checkout-service / ERROR lines', note: '', by: 'paul.stone', t: iso(0), read: false});

async function boot(opts) {
  const o = opts || {};
  const errors = [], calls = [];
  const srv = {list: o.list || rows(o.total || 4, o.unread || 0), gets: 0};
  srv.count = () => srv.list.filter((n) => !n.read).length;
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(o.held ? withHeld(state()) : state())).replace(SLOT, () => (o.lang && o.pack ? served(o.lang) : '')), {
    url: 'https://skopax.example.com/' + (o.hash || '#t=anoms/all'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      if (o.lang) w.document.cookie = 'skopax_lang=' + o.lang;
      w.fetch = async (path, init) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        const no = (code) => ({ok: false, status: code, json: async () => ({detail: 'log in first'})});
        if (p.startsWith('/api/notifications')) {
          calls.push([(init && init.method) || 'GET', p, init && init.body ? JSON.parse(init.body) : null]);
          if (o.guest) return no(401);
          if (p.startsWith('/api/notifications/count')) return ok({unread: srv.count()});
          if (p.startsWith('/api/notifications/read')) {
            const b = JSON.parse(init.body);
            if (b.all && o.holdRead) await o.holdRead;
            const hit = srv.list.filter((n) => !n.read && (b.all || b.ids.indexOf(n.id) >= 0));
            hit.forEach((n) => { n.read = true; });
            return ok({read: hit.length, unread: srv.count()});
          }
          const q = new URL(p, 'https://skopax.example.com').searchParams;
          const from = q.get('before') ? srv.list.findIndex((n) => n.id === q.get('before')) + 1 : 0;
          const body = {items: srv.list.slice(from, from + Number(q.get('limit'))), unread: srv.count()};
          srv.gets += 1;
          if (o.arriveOnGet && srv.gets === 1) srv.list.unshift(newcomer(o.arriveOnGet));
          return ok(body);
        }
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: o.guest ? null : 'nina.white@example.com', operator: !o.guest, teams: [], tags: []});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return no(404);
      };
    },
  });
  await wait(450);
  const arrive = async (id) => { srv.list.unshift(newcomer(id)); await dom.window.__ntCount(); await wait(50); };
  return {w: dom.window, doc: dom.window.document, errors, calls, arrive, srv};
}

const shown = (el) => !!el && !el.hidden;
const posts = (calls) => calls.filter((c) => c[0] === 'POST');
const badge = (doc) => doc.querySelector('#tab-slack #nt-badge');
const show = async (doc, id) => { doc.getElementById('tab-' + id).click(); await wait(80); };
const looks = (doc) => [...doc.querySelectorAll('#nt-list .nt-item')].map((i) => i.dataset.id + (i.classList.contains('unread') ? '*' : ''));
const unreadOnServer = (srv) => srv.list.filter((n) => !n.read).map((n) => n.id);

async function theRowCountsUnreadAndThePanelListsNewestFirst() {
  //given
  const {w, doc, errors, calls} = await boot({unread: 2});
  const b = badge(doc);
  const end = doc.querySelector('#tab-slack .end');

  //when
  const before = {badge: b.firstChild.textContent, sr: b.querySelector('.sr').textContent, panel: shown(doc.getElementById('panel-slack')), listed: doc.querySelectorAll('#nt-list .nt-item').length};
  const onRow = [...end.children].filter((c) => w.getComputedStyle(c).display !== 'none').map((c) => c.id);
  await show(doc, 'slack');
  const items = [...doc.querySelectorAll('#panel-slack #nt-list .nt-item')];
  const first = items[0];

  //then
  assert.deepStrictEqual(['nt-menu', 'nt-btn', 'nt-pop', 'ap-open'].map((id) => doc.getElementById(id)), [null, null, null, null], 'the bell and its popover are retired, and so is the Appearance button');
  assert.ok(b.classList.contains('b-num') && b.closest('.end'), 'the unread count is a badge on the Notifications row');
  assert.ok(doc.getElementById('count-slack'), 'the drafts count keeps its id');
  assert.deepStrictEqual(onRow, ['nt-badge'], 'the row shows only the unread badge');
  assert.deepStrictEqual(before, {badge: '2', sr: ' unread', panel: false, listed: 0}, 'a logged-in person sees the unread count on the row, the feed not loaded yet');
  assert.strictEqual(w.NT_POLL_MS, 30000, 'the count is polled every 30 s');
  const sec = doc.getElementById('nt-sec');
  assert.ok(shown(sec) && sec.querySelector('h3').textContent === 'For you', 'the panel opens on For you');
  assert.strictEqual(items.length, 4);
  assert.deepStrictEqual(items.map((i) => i.classList.contains('unread')), [true, true, false, false], 'unread ones stand out');
  assert.deepStrictEqual(items.map((i) => !!i.querySelector('.sr')), [true, true, false, false]);
  assert.strictEqual(first.querySelector('.tag').textContent, 'Assigned to you');
  assert.strictEqual(first.querySelector('.nt-what').textContent, '#14 checkout-service / ERROR lines');
  assert.strictEqual(first.querySelector('.nt-by').textContent, 'paul.stone');
  assert.strictEqual(first.querySelector('.nt-when').textContent, '2 min ago');
  assert.strictEqual(items[1].querySelector('.nt-note').textContent, 'Fixed \u00b7 OPS-12');
  assert.ok(items[1].querySelector('.tag').classList.contains('k-good') && items[3].querySelector('.tag').classList.contains('k-crit'), 'the tag colour follows the kind');
  assert.ok(!items[2].querySelector('.nt-what').textContent.startsWith('#'), 'a chat answer has no card number');
  assert.deepStrictEqual(calls.map((c) => c[0] + ' ' + c[1]), ['GET /api/notifications/count', 'GET /api/notifications?limit=20', 'POST /api/notifications/read']);
  assert.deepStrictEqual(posts(calls)[0][2], {ids: ['n0', 'n1']}, 'showing the tab marks read what it loaded unread');
  assert.ok(b.hidden, 'and the badge goes');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the Notifications row shows only the unread badge, and its panel lists the newest first under For you with kind, card, note, who and when, then marks read what it showed';
}

async function showingTheTabMarksTheFeedReadOnce() {
  //given
  const {w, doc, errors, calls} = await boot({unread: 2});

  //when
  await show(doc, 'slack');
  await show(doc, 'hist');
  await show(doc, 'slack');
  const again = looks(doc);

  //then
  assert.strictEqual(posts(calls).length, 1, 'only the first showing has something to mark');
  assert.deepStrictEqual(calls.filter((c) => c[0] === 'GET' && c[1].startsWith('/api/notifications?')).length, 2, 'each showing loads the feed afresh');
  assert.deepStrictEqual(again, ['n0', 'n1', 'n2', 'n3'], 'the next showing has nothing new');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'showing the tab posts /api/notifications/read once, and a later showing finds nothing new';
}

async function showingMarksOnlyWhatItLoaded() {
  //given
  const deep = await boot({total: 25, unread: 25});
  const racing = await boot({unread: 2, arriveOnGet: 'm1'});

  //when
  await show(deep.doc, 'slack');
  const firstPage = {post: posts(deep.calls).map((c) => c[2]), badge: badge(deep.doc).firstChild.textContent, server: unreadOnServer(deep.srv)};
  deep.doc.getElementById('nt-more').click();
  await wait(60);
  const second = looks(deep.doc).slice(20);
  await show(racing.doc, 'slack');

  //then
  assert.deepStrictEqual(firstPage.post, [{ids: Array.from({length: 20}, (_, i) => 'n' + i)}], 'only the 20 shown are marked read');
  assert.deepStrictEqual([firstPage.badge, firstPage.server], ['5', ['n20', 'n21', 'n22', 'n23', 'n24']], 'the second page stays unread on the server and on the badge');
  assert.deepStrictEqual(second, ['n20*', 'n21*', 'n22*', 'n23*', 'n24*'], 'and stands out when Show more lists it');
  assert.deepStrictEqual(posts(racing.calls).map((c) => c[2]), [{ids: ['n0', 'n1']}], 'a notification that arrives between the load and the mark is not in it');
  assert.deepStrictEqual([unreadOnServer(racing.srv), badge(racing.doc).firstChild.textContent, shown(badge(racing.doc))], [['m1'], '1', true], 'so it stays unread and counts on the badge');
  assert.deepStrictEqual(deep.errors.concat(racing.errors), []);
  deep.w.close(); racing.w.close();
  return 'showing the tab marks read only the unread items it loaded: a second page and a late arrival stay unread';
}

async function theUnreadLookLastsUntilTheTabIsLeft() {
  //given
  const {w, doc, errors, arrive} = await boot({unread: 2});
  await show(doc, 'slack');

  //when
  const shownFirst = looks(doc);
  await arrive('m1');
  const afterArrival = looks(doc);
  await show(doc, 'hist');
  await show(doc, 'slack');
  const nextShowing = looks(doc);

  //then
  assert.deepStrictEqual(shownFirst, ['n0*', 'n1*', 'n2', 'n3']);
  assert.deepStrictEqual(afterArrival, ['m1*', 'n0*', 'n1*', 'n2', 'n3'], 'a new arrival reloads the list and what was unread on arrival keeps its look');
  assert.deepStrictEqual(nextShowing, ['m1*', 'n0', 'n1', 'n2', 'n3'], 'the next showing starts afresh');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'what was unread when the tab opened keeps its unread look until the tab is left, also when a new one arrives';
}

async function clickingOneOpensItsCardOrChat() {
  //given
  const {w, doc, errors, calls, arrive} = await boot({unread: 2, hash: '#t=slack'});

  //when
  doc.querySelector('#nt-list .nt-item[data-id="n0"]').click();
  await wait(50);
  const card = {linked: !!doc.querySelector('#anoms article.inc.linked[data-key="' + CARD + '"]'), panel: shown(doc.getElementById('panel-slack')), posts: posts(calls).length};
  await show(doc, 'slack');
  await arrive('n9');
  const fresh = {badge: badge(doc).firstChild.textContent, unread: doc.querySelector('#nt-list .nt-item[data-id="n9"]').classList.contains('unread')};
  doc.querySelector('#nt-list .nt-item[data-id="n9"]').click();
  await wait(50);
  const clickRead = posts(calls).slice(1);
  await show(doc, 'slack');
  doc.querySelector('#nt-list .nt-item[data-id="n2"]').click();
  await wait(50);
  const chat = w.location.hash;

  //then
  assert.deepStrictEqual(posts(calls)[0], ['POST', '/api/notifications/read', {ids: ['n0', 'n1']}], 'a link straight to the tab marks the feed read once it is known');
  assert.deepStrictEqual(card, {linked: true, panel: false, posts: 1}, 'it opens the card, the board replaces the panel, and an item already marked is not marked again');
  assert.deepStrictEqual(fresh, {badge: '1', unread: true}, 'a notification that arrives while the tab is open shows on the row and stands out');
  assert.deepStrictEqual(clickRead, [['POST', '/api/notifications/read', {ids: ['n9']}]], 'clicking it marks it read at once, before the tab is shown again');
  assert.strictEqual(chat, '#t=chat/thread/c9', 'a chat answer opens its chat');
  assert.strictEqual(posts(calls).length, 2, 'an already read one is not marked again');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'clicking a notification opens its card, or its chat for a chat answer, and marks it read when the showing did not';
}

async function markAllReadAndOlderPages() {
  //given
  const {w, doc, errors, calls, arrive} = await boot({total: 23, unread: 12});

  //when
  const capped = badge(doc).firstChild.textContent;
  await show(doc, 'slack');
  const more = shown(doc.getElementById('nt-more'));
  const moreText = doc.getElementById('nt-more').textContent;
  doc.getElementById('nt-more').click();
  await wait(50);
  const listed = doc.querySelectorAll('#nt-list .nt-item').length;
  const idle = doc.getElementById('nt-read-all').disabled;
  await arrive('n99');
  const ready = doc.getElementById('nt-read-all').disabled;
  doc.getElementById('nt-read-all').click();
  await wait(50);
  const after = {badge: shown(badge(doc)), unread: doc.querySelectorAll('#nt-list .nt-item.unread').length, disabled: doc.getElementById('nt-read-all').disabled};

  //then
  assert.strictEqual(capped, '9+', 'the badge stops at 9+');
  assert.ok(more && moreText === 'Show more' && listed === 23, 'a full page offers older ones, which load after the last');
  assert.ok(calls.some((c) => c[1] === '/api/notifications?limit=20&before=n19'));
  assert.deepStrictEqual([idle, ready], [false, false], 'Mark all read stays usable while the list still shows unread ones');
  assert.deepStrictEqual(posts(calls).map((c) => c[2]), [{ids: Array.from({length: 12}, (_, i) => 'n' + i)}, {all: true}], 'the showing, then Mark all read');
  assert.deepStrictEqual(after, {badge: false, unread: 0, disabled: true});
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'Show more pages by the last id, Mark all read clears what arrived since, and the badge caps at 9+';
}

async function guestsSeeNoFeedAndAnotherLanguageKeepsCardTitles() {
  //given
  const guest = await boot({guest: true, hash: '#t=slack'});
  const de = await boot({unread: 1, lang: 'de', hash: '#t=slack'});

  //then
  assert.ok(!shown(guest.doc.getElementById('nt-sec')), 'no For you without a login');
  assert.ok(!shown(badge(guest.doc)), 'and no badge');
  assert.ok(shown(guest.doc.getElementById('panel-slack')) && !guest.doc.querySelector('#panel-slack #slackq'), 'the page shows and has no Slack tables');
  assert.deepStrictEqual(posts(guest.calls), []);
  assert.strictEqual(de.doc.querySelector('#nt-list .nt-what').textContent, '#14 checkout-service / ERROR lines', 'card titles stay as they are');
  assert.deepStrictEqual(guest.errors.concat(de.errors), []);
  guest.w.close(); de.w.close();
  return 'a guest gets no feed and no badge, and with another language picked the card titles stay as they are';
}

function autoRows() {
  const auto = (id, num, note) => ({id, kind: 'auto', key: CARD + num, num, title: 'checkout-service / ERROR lines', note, by: 'Skopax', t: iso(num), read: true});
  return [auto('a1', 21, 'Cleared 17 Oct 01:00, nobody acted in 6 hours'), auto('a2', 22, 'First seen 16 Oct 02:00, nobody acted in 2 business hours'),
    auto('a3', 23, 'Stopped after the event; nobody opened a task'), auto('a4', 24, 'Its parent #9 checkout-service / ERROR lines was marked no action needed'),
    auto('a5', 25, 'Its parent was handled and has left the page'), auto('a6', 26, 'Its parent #9 <img src=x onerror=alert(1)> / ERROR was marked no action needed'),
    auto('a7', 27, '<img src=x onerror=alert(1)>'), {id: 'r1', kind: 'resolved', key: CARD, num: 14, title: 'checkout-service / ERROR lines', note: 'Fixed \u00b7 OPS-12', by: 'paul.stone', t: iso(30), read: true}];
}

async function anAutoResolveShowsWhyUnderTheTitleInEnglishAndTurkish() {
  //given
  const en = await boot({list: autoRows(), hash: '#t=slack'});
  const tr = await boot({list: autoRows(), lang: 'tr', pack: true, hash: '#t=slack'});
  const notes = (doc) => [...doc.querySelectorAll('#nt-list .nt-item')].map((i) => [i.querySelector('.nt-what').textContent, (i.querySelector('.nt-note') || {}).textContent]);

  //then
  assert.deepStrictEqual(notes(en.doc), [
    ['#21 checkout-service / ERROR lines', 'Cleared 17 Oct 01:00, nobody acted in 6 hours'],
    ['#22 checkout-service / ERROR lines', 'First seen 16 Oct 02:00, nobody acted in 2 business hours'],
    ['#23 checkout-service / ERROR lines', 'Stopped after the event; nobody opened a task'],
    ['#24 checkout-service / ERROR lines', 'Its parent #9 checkout-service / ERROR lines was marked no action needed'],
    ['#25 checkout-service / ERROR lines', 'Its parent was handled and has left the page'],
    ['#26 checkout-service / ERROR lines', 'Its parent #9 <img src=x onerror=alert(1)> / ERROR was marked no action needed'],
    ['#27 checkout-service / ERROR lines', '<img src=x onerror=alert(1)>'],
    ['#14 checkout-service / ERROR lines', 'Fixed \u00b7 OPS-12']]);
  assert.strictEqual(en.doc.querySelector('#nt-list img'), null, 'the reason is text, never markup');
  assert.deepStrictEqual(notes(tr.doc).map((n) => n[1]), [
    '17 Eki 01:00 normale d\u00f6nd\u00fc, 6 saat boyunca kimse ilgilenmedi',
    '\u0130lk g\u00f6r\u00fclme 16 Eki 02:00, 2 i\u015f saati boyunca kimse ilgilenmedi',
    'Olaydan sonra durdu; kimse g\u00f6rev a\u00e7mad\u0131',
    '\u00dcst kart #9 checkout-service / ERROR lines \'aksiyon gerekmiyor\' olarak i\u015faretlendi',
    '\u00dcst kart \u00e7\u00f6z\u00fcld\u00fc ve sayfadan kalkt\u0131',
    '\u00dcst kart #9 <img src=x onerror=alert(1)> / ERROR \'aksiyon gerekmiyor\' olarak i\u015faretlendi',
    '<img src=x onerror=alert(1)>',
    'Fixed \u00b7 OPS-12']);
  assert.strictEqual(tr.doc.querySelector('#nt-list img'), null, 'markup in the part a rule puts back stays text');
  assert.strictEqual(tr.doc.querySelector('#nt-list .nt-item .nt-what').textContent, '#21 checkout-service / ERROR lines', 'the card title stays as it is');
  assert.deepStrictEqual(en.errors.concat(tr.errors), []);
  en.w.close(); tr.w.close();
  return 'an auto-resolve notice shows why under the card title, escaped, and in Turkish with the Turkish pack, markup in a rule\'s part included';
}

async function aSignedOutBoardNeverAsksForTheCount() {
  //given
  const {w, doc, errors, calls} = await boot({guest: true});

  //when
  doc.dispatchEvent(new w.Event('visibilitychange'));
  await show(doc, 'slack');
  await wait(50);

  //then
  assert.deepStrictEqual(calls, [], 'signed out, the page asks for no notifications at all, so no 401 reaches the console');
  assert.ok(!shown(badge(doc)) && !shown(doc.getElementById('nt-sec')));
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a signed-out board never asks for the notification count';
}

async function theNotificationsPageShowsOnlyNotifications() {
  //given
  const signed = await boot({hash: '#t=slack'});
  const guest = await boot({guest: true, hash: '#t=slack'});
  const panel = signed.doc.getElementById('panel-slack');

  //then
  assert.deepStrictEqual([...panel.querySelectorAll('h3')].map((h) => h.textContent), ['For you']);
  assert.ok(!panel.querySelector('#slackq, #slack-sent, #sq-h'), 'no Slack tables on the Notifications page');
  assert.ok(signed.doc.querySelector('#panel-acts #slackq') && signed.doc.querySelector('#panel-acts #slack-sent'), 'they sit on the Activity page');
  assert.strictEqual(panel.querySelectorAll('#nt-list .nt-item').length, 4, 'the notifications are still listed');
  assert.ok(signed.doc.getElementById('nt-read-all') && signed.doc.getElementById('nt-more'), 'Mark all read and Show more stay');
  assert.ok(!shown(signed.doc.getElementById('nt-none')), 'no empty line while the list shows');
  assert.ok(!shown(guest.doc.getElementById('nt-sec')) && shown(guest.doc.getElementById('nt-none')), 'a signed-out viewer gets a short empty line, not a blank page');
  assert.deepStrictEqual(signed.errors.concat(guest.errors), []);
  signed.w.close(); guest.w.close();
  return 'the Notifications page holds only the notification list, and says so when there is none to show';
}

async function aHeldPostCountsOnActivityNotOnNotifications() {
  //given
  const held = await boot({hash: '#t=acts', held: true});
  const none = await boot({hash: '#t=acts'});

  //then
  assert.ok(/1 not posted/.test(held.doc.getElementById('count-acts').textContent), 'Activity carries the count: ' + held.doc.getElementById('count-acts').textContent);
  assert.ok(/1 not posted/.test(held.doc.getElementById('tab-acts').title), 'and its tooltip');
  assert.strictEqual(held.doc.getElementById('count-slack').textContent, '', 'Notifications carries no held-post count');
  assert.ok(!/not posted/.test(held.doc.getElementById('tab-slack').title), 'nor in its tooltip');
  assert.strictEqual(held.doc.querySelectorAll('#panel-acts #slackq tbody tr:not(.empty)').length, 1, 'the held post is listed on Activity');
  assert.ok(!/not posted/.test(none.doc.getElementById('count-acts').textContent), 'no count without a held post');
  assert.ok(held.doc.getElementById('tab-acts').classList.contains('k-warn'), 'a held post turns Activity to the warning colour: ' + held.doc.getElementById('tab-acts').className);
  assert.ok(!none.doc.getElementById('tab-acts').classList.contains('k-warn'), 'no warning colour without a held post');
  assert.deepStrictEqual(held.errors.concat(none.errors), []);
  held.w.close(); none.w.close();
  return 'one held post shows its count on Activity and not on Notifications';
}

async function aRefusedMailIsMarkedOnItsNotificationWithTheReason() {
  //given
  const list = rows(2, 0);
  list[0].mail = {state: 'refused', why: '550 mailbox unavailable'};
  const en = await boot({list: JSON.parse(JSON.stringify(list))});
  const de = await boot({list: JSON.parse(JSON.stringify(list)), lang: 'de', pack: true});

  //when
  await show(en.doc, 'slack');
  await show(de.doc, 'slack');
  const marks = (doc) => [...doc.querySelectorAll('#nt-list .nt-item')].map((i) => { const m = i.querySelector('.nt-mail'); return m ? [m.textContent, m.title] : null; });

  //then
  assert.deepStrictEqual(marks(en.doc), [['Email not sent', 'The mail server refused this email: 550 mailbox unavailable'], null]);
  assert.deepStrictEqual(marks(de.doc), [['E-Mail nicht gesendet', 'Der Mailserver hat diese E-Mail abgelehnt: 550 mailbox unavailable'], null]);
  assert.deepStrictEqual(en.errors.concat(de.errors), []);
  en.w.close(); de.w.close();
  return 'a notification whose email the mail server refused says Email not sent, with the reason as its tooltip, also in German';
}

async function markAllReadClearsTheUnreadLookOfWhatIsShowing() {
  // given
  const {w, doc, errors, calls} = await boot({total: 5, unread: 3});
  await show(doc, 'slack');
  await wait(50);
  const looked = doc.querySelectorAll('#nt-list .nt-item.unread').length;
  const usable = !doc.getElementById('nt-read-all').disabled;

  // when
  doc.getElementById('nt-read-all').click();
  await wait(50);

  // then
  assert.strictEqual(looked, 3, 'the shown ones still look unread while the tab is open');
  assert.ok(usable, 'Mark all read is usable while some look unread');
  assert.deepStrictEqual(posts(calls).map((c) => c[2]).slice(-1), [{all: true}]);
  assert.strictEqual(doc.querySelectorAll('#nt-list .nt-item.unread').length, 0, 'nothing looks unread after Mark all read');
  assert.ok(doc.getElementById('nt-read-all').disabled, 'then it waits for something new');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'Mark all read clears the unread look of what is showing, also when the server already counts them read';
}

async function markAllReadCannotRunTwice() {
  //given
  let open;
  const holdRead = new Promise((r) => { open = r; });
  const {w, doc, errors, calls, arrive} = await boot({unread: 2, holdRead});
  await show(doc, 'slack');
  await arrive('n99');
  const all = doc.getElementById('nt-read-all');

  //when
  all.click();
  await wait(20);
  const during = {disabled: all.disabled, bar: !!doc.querySelector('[role="progressbar"]') && !doc.querySelector('[role="progressbar"]').hidden};
  all.disabled = false;
  all.click();
  await w.__ntCount();
  const stillOff = all.disabled;
  open();
  await wait(80);

  //then
  assert.deepStrictEqual(during, {disabled: true, bar: true}, 'Mark all read disables itself and the top bar shows while it runs');
  assert.ok(stillOff, 'a count poll does not enable it while it runs');
  assert.strictEqual(posts(calls).filter((c) => c[2] && c[2].all).length, 1, 'a second click sends nothing');
  assert.ok(all.disabled && doc.querySelector('[role="progressbar"]').hidden, 'after it is done nothing is unread and the bar hides');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'Mark all read cannot run twice and shows the top bar while it runs';
}

(async () => {
  console.log('PASS ' + await aRefusedMailIsMarkedOnItsNotificationWithTheReason());
  console.log('PASS ' + await theRowCountsUnreadAndThePanelListsNewestFirst());
  console.log('PASS ' + await showingTheTabMarksTheFeedReadOnce());
  console.log('PASS ' + await showingMarksOnlyWhatItLoaded());
  console.log('PASS ' + await theUnreadLookLastsUntilTheTabIsLeft());
  console.log('PASS ' + await clickingOneOpensItsCardOrChat());
  console.log('PASS ' + await markAllReadAndOlderPages());
  console.log('PASS ' + await markAllReadClearsTheUnreadLookOfWhatIsShowing());
  console.log('PASS ' + await guestsSeeNoFeedAndAnotherLanguageKeepsCardTitles());
  console.log('PASS ' + await anAutoResolveShowsWhyUnderTheTitleInEnglishAndTurkish());
  console.log('PASS ' + await aSignedOutBoardNeverAsksForTheCount());
  console.log('PASS ' + await theNotificationsPageShowsOnlyNotifications());
  console.log('PASS ' + await aHeldPostCountsOnActivityNotOnNotifications());
  console.log('PASS ' + await markAllReadCannotRunTwice());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
