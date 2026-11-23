const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(check, ms) {
  const end = Date.now() + (ms || 5000);
  while (Date.now() < end && !check()) await wait(5);
}
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const KEY = 'log|checkout-service|ERROR|boom';

function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [{key: KEY, num: 14, sev: 'warn', status: 'open', svc: 'checkout-service', label: 'ERROR lines', src: 'logs', sig: 'log', now: 1, firstSeen: iso(30), lastSeen: iso(1)}],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(hash, me) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state()));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/' + hash, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.__posted = [];
      w.fetch = async (path, init) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok(Object.assign({loginEnabled: true, user: 'alice@example.com', operator: true, teams: [], tags: [], chat: true}, me || {}));
        if (p.startsWith('/api/chats') && init && init.method === 'POST') { w.__posted.push(JSON.parse(init.body)); return ok({id: 'c0ffee12', cards: [], messages: []}); }
        if (p.startsWith('/api/investigator/status')) return ok({mode: 'live'});
        if (p.startsWith('/api/server')) return ok({investigate: {mode: 'live', stopped: false}});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(450);
  return {w: dom.window, doc: dom.window.document, errors};
}

async function aCardLinkOpensTheChatFrameWithThatCardAndTheFrameStaysAcrossTabSwitches() {
  //given
  const {w, doc, errors} = await boot('#t=anoms');

  //then
  assert.strictEqual(doc.querySelector('#anoms article.inc .chat-link'), null, 'the card has one way to ask, its Ask box; the separate chat button is gone');
  assert.ok(doc.getElementById('tab-chat'), 'the side menu has a Chat tab');

  //when
  w.location.hash = '#t=chat/new/14';
  w.dispatchEvent(new w.HashChangeEvent('hashchange'));
  await wait(150);
  const frame = doc.querySelector('#chat iframe.chat-frame');

  //then
  assert.ok(!doc.getElementById('panel-chat').hidden);
  assert.strictEqual(frame.getAttribute('src'), '/chat/?card=14');
  assert.strictEqual(doc.querySelector('#chat .chat-open a').getAttribute('href'), '/chat/');
  assert.strictEqual(doc.getElementById('chat-lang'), null, 'the language is a global setting, not a chat control');
  assert.deepStrictEqual([...doc.querySelectorAll('#panel-prefs [data-ap="lang"] button')].map((x) => [x.textContent, x.getAttribute('aria-pressed')]), [['Auto', 'true'], ['English', 'false']]);
  doc.querySelector('#panel-prefs [data-ap="lang"] button[data-v="en"]').click();
  await wait(150);
  assert.ok(/skopax_lang=en/.test(doc.cookie), 'the choice is kept in a cookie the chat reads: ' + doc.cookie);
  assert.strictEqual(doc.querySelector('#panel-prefs [data-ap="lang"] button[data-v="en"]').getAttribute('aria-pressed'), 'true');
  assert.ok(errors.some((e) => /navigation/i.test(e)), 'the page reloads itself in the new language: ' + errors.join(' | '));
  errors.length = 0;
  const frame2 = frame;
  assert.ok(decodeURIComponent(w.location.hash).endsWith('t=chat'), 'the card is used once: ' + w.location.hash);

  //when
  doc.getElementById('tab-anoms').click();
  await wait(60);
  doc.getElementById('tab-chat').click();
  await wait(150);

  //then
  assert.strictEqual(doc.querySelector('#chat iframe.chat-frame'), frame2, 'coming back to the tab keeps the open chat');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a card links to the Chat tab, which opens the chat frame with that card attached once and keeps it when you come back to the tab');
}

async function aGuestIsAskedToLogInAndAServiceWithoutTheChatSaysSo() {
  //given
  const guest = await boot('#t=chat', {user: null, operator: false});
  const off = await boot('#t=chat', {chat: false});
  const plain = await boot('#t=chat');

  //then
  assert.strictEqual(guest.doc.querySelector('#chat iframe'), null);
  assert.ok(guest.doc.querySelector('#chat .chat-login a').getAttribute('href').startsWith('/login?next='));
  assert.ok(off.doc.querySelector('#chat .chat-login').textContent.includes('not set up'));
  assert.strictEqual(plain.doc.querySelector('#chat iframe.chat-frame').getAttribute('src'), '/chat/');
  assert.deepStrictEqual(guest.errors.concat(off.errors, plain.errors), []);
  guest.w.close(); off.w.close(); plain.w.close();
  console.log('PASS a guest is asked to log in, a service without the chat says so, and a logged-in person gets the chat frame');
}

async function theChatTabHidesSummaryActivityAndTheMenuWhichFoldsAwayOnAnyTab() {
  //given
  const {w, doc, errors} = await boot('#t=chat');
  const layout = doc.querySelector('.layout');
  const off = () => layout.classList.contains('nav-off');

  //then
  assert.ok(doc.getElementById('reco').hidden && doc.getElementById('reco').parentNode.hidden, 'no Summary on the Chat tab');
  assert.ok(doc.getElementById('live').hidden && layout.classList.contains('no-live'), 'no Activity on the Chat tab');
  assert.ok(off() && doc.body.classList.contains('nav-off'), 'the menu starts folded away on Chat');
  assert.strictEqual(doc.getElementById('nav-fold').title, 'Show the menu');

  //when
  doc.getElementById('tab-anoms').click();
  await wait(60);
  const onAnoms = off();
  const summaryBack = !doc.getElementById('reco').hidden && !doc.getElementById('live').hidden;
  const onTitle = doc.getElementById('nav-fold').title;
  doc.getElementById('nav-fold').click();
  const hidden = off();
  const offTitle = doc.getElementById('nav-fold').title;
  doc.getElementById('nav-fold').click();
  const shown = !off();
  doc.getElementById('tab-chat').click();
  await wait(120);
  const chatAgain = off();
  doc.getElementById('nav-fold').click();
  const chatOpened = !off();
  doc.getElementById('tab-anoms').click();
  await wait(60);
  doc.getElementById('tab-chat').click();
  await wait(120);

  //then
  assert.ok(!onAnoms && summaryBack, 'other tabs keep the menu, Summary and Activity');
  assert.ok(hidden && shown, 'the fold arrow folds the menu away and the same arrow brings it back');
  assert.deepStrictEqual([onTitle, offTitle], ['Hide the menu', 'Show the menu']);
  assert.strictEqual(doc.querySelector('#tabs .nav-hide'), null, 'no second button at the bottom of the menu');
  assert.ok(chatAgain && chatOpened, 'Chat folds it again and the button opens it');
  assert.ok(!off(), 'a menu opened on Chat stays open the next time');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the Chat tab shows no Summary or Activity and starts with the menu folded away; one fold arrow folds and opens it on any tab, and Chat remembers an opened menu');
}

async function theChatLoadsBehindASpinnerInThePageThemeAndFadesInWhenItsInputIsThere() {
  //given
  const {w, doc, errors} = await boot('#t=anoms');
  w.document.documentElement.dataset.theme = 'light';

  //when
  w.location.hash = '#t=chat';
  await wait(300);
  const wrap = doc.querySelector('#chat .chat-wrap');
  const frame = wrap.querySelector('iframe.chat-frame');
  const before = {ready: wrap.classList.contains('ready'), loading: wrap.querySelector('.chat-loading').textContent, role: wrap.querySelector('.chat-loading').getAttribute('role'),
    theme: w.localStorage.getItem('vite-ui-theme')};
  await wait(400);
  const stillLoading = wrap.classList.contains('ready');
  const inner = frame.contentDocument;
  (inner.body || inner.documentElement || inner.appendChild(inner.createElement('html'))).appendChild(inner.createElement('textarea'));
  await wait(300);

  //then
  assert.deepStrictEqual(before, {ready: false, loading: 'Loading the chat\u2026', role: 'status', theme: 'light'}, 'the chat starts hidden behind a loading line and takes the page theme');
  assert.strictEqual(stillLoading, false, 'it stays hidden while the chat has no input yet');
  assert.ok(wrap.classList.contains('ready'), 'the chat fades in once its input is there');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the Chat tab loads the chat behind a spinner, in the page theme, and shows it once its input is there');
}

async function aChatThatNeverGetsItsInputIsReloadedOnceThenShown() {
  //given
  const {w, doc, errors} = await boot('#t=anoms');
  w.CHAT_READY_MS = 150;

  //when
  w.location.hash = '#t=chat';
  await until(() => doc.querySelector('#chat .chat-wrap iframe.chat-frame'));
  const wrap = doc.querySelector('#chat .chat-wrap');
  const first = wrap.querySelector('iframe.chat-frame');
  await until(() => wrap.querySelector('iframe.chat-frame') !== first);
  const second = wrap.querySelector('iframe.chat-frame');
  const retry = {fresh: second !== first && !first.isConnected, src: second.getAttribute('src'), note: wrap.querySelector('.chat-loading').textContent, ready: wrap.classList.contains('ready')};
  await until(() => wrap.classList.contains('ready'));

  //then
  assert.deepStrictEqual(retry, {fresh: true, src: '/chat/', note: 'Reconnecting the chat\u2026', ready: false}, 'a chat stuck without its input (e.g. a deploy replaced the pods) gets a fresh frame once, behind "Reconnecting the chat\u2026"');
  assert.ok(wrap.classList.contains('ready') && wrap.querySelector('iframe.chat-frame') === second, 'after one retry it shows whatever loaded instead of reloading again');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a chat that never shows its input is reloaded once behind "Reconnecting the chat\u2026", then shown');
}

async function theCardAskBoxStartsAPrivateChatWithTheCardAndOpensIt() {
  //given
  const {w, doc, errors} = await boot('#t=anoms');
  const card = doc.querySelector('#anoms article.inc');
  card.querySelector('.inc-title .fold').click();
  await wait(60);
  const box = card.querySelector('.card-ask');

  //when
  box.querySelector('input').value = 'Why was this card opened?';
  box.querySelector('input').dispatchEvent(new w.Event('input', {bubbles: true}));
  box.querySelector('.ask-go').click();
  await wait(200);
  const frame = doc.querySelector('#chat iframe.chat-frame');

  //then
  assert.deepStrictEqual(w.__posted, [{text: 'Why was this card opened?', cards: [card.dataset.key]}], 'the question goes to the chat API with the card attached, not to the page ask queue');
  assert.ok(!doc.getElementById('panel-chat').hidden, 'the Chat tab opens');
  assert.strictEqual(frame.getAttribute('src'), '/chat/thread/c0ffee12', 'on the new chat, where the answer arrives');
  assert.ok(box.querySelector('.ask-go').title.startsWith('Opens a private chat about this card'));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the card Ask box starts a private chat with the card attached and this question, and opens the Chat tab on it');
}

async function theChatAndDocsNotesPointWhereThingsLiveNow() {
  //given
  const {w, doc, errors} = await boot('#t=docs');

  //when
  const chatNote = doc.querySelector('#panel-chat .panel-note .q').getAttribute('title');
  const ask = [...doc.querySelectorAll('#panel-docs li')].find((li) => li.textContent.indexOf('Ask the investigator') === 0);

  //then
  assert.ok(chatNote.indexOf('A daily message cap per person applies (Admin \u203a Investigator).') >= 0, 'the chat cap is set in Admin \u203a Investigator, not on the personal Settings page');
  assert.ok(chatNote.indexOf('(Settings)') < 0);
  assert.ok(ask, 'Docs has a line for Ask the investigator');
  assert.ok(ask.textContent.indexOf('the Investigator menu at the top or on Requests') >= 0 && ask.textContent.indexOf('private chat') >= 0, 'Docs says where Ask the investigator is and where its answer lands: ' + ask.textContent);
  assert.ok(ask.textContent.indexOf('under Investigator below') < 0 && ask.textContent.indexOf('Activity') < 0, 'not the old board layout, and not Activity, which leaves private chats out');
  assert.ok([...doc.querySelectorAll('#panel-docs li')].every((d) => !/\b[A-Z][a-z]+(?: [a-z]+)? tabs?\b|Tabs,|its tab\b/.test(d.textContent)), 'Docs names sidebar pages, not tabs');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the chat note puts the daily cap under Admin \u203a Investigator, and Docs puts Ask the investigator in the Investigator menu or on Requests with its answer in a private chat');
}

(async () => {
  await aChatThatNeverGetsItsInputIsReloadedOnceThenShown();
  await theChatLoadsBehindASpinnerInThePageThemeAndFadesInWhenItsInputIsThere();
  await aCardLinkOpensTheChatFrameWithThatCardAndTheFrameStaysAcrossTabSwitches();
  await theChatTabHidesSummaryActivityAndTheMenuWhichFoldsAwayOnAnyTab();
  await aGuestIsAskedToLogInAndAServiceWithoutTheChatSaysSo();
  await theCardAskBoxStartsAPrivateChatWithTheCardAndOpensIt();
  await theChatAndDocsNotesPointWhereThingsLiveNow();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
