const assert = require('assert');
const {boot, card, iso, wait, until, article, expand} = require('./skopax-card-polish.test.js');

const WAITING = 'log|checkout-service|ERROR|boom';
const CALM = 'log|shop-web|INFO|calm';
const KID = 'log|checkout-service|WARN|kid';
const TAKEN = 'log|cart-service|ERROR|taken';
const ASSIGNED = 'log|search-service|ERROR|assigned';
const EVENT_TAKEN = 'log|push-service|ERROR|send';
const WORDS = {en: ['Stopped firing \u00b7 unassigned', 'Stopped firing', 'stopped firing', 'Stopped firing \u00b7 awaiting action \u00b7 assigned to Bob Smith',
  'Stopped firing \u00b7 Alice Smith is on it', 'Stopped after the event \u00b7 carol.jones is on it'],
  tr: ['Sinyal durdu \u00b7 atama bekleniyor', 'Sinyal durdu', 'sinyal durdu', 'Sinyal durdu \u00b7 aksiyon bekleniyor \u00b7 atanan: Bob Smith',
  'Sinyal durdu \u00b7 Alice Smith ilgileniyor', 'Olaydan sonra sinyal durdu \u00b7 carol.jones ilgileniyor']};

function cards() {
  const began = iso(72);
  return [card(WAITING, {num: 31, status: 'cleared', clearedAt: iso(20), firstSeen: iso(90)}), card(CALM, {num: 32, sev: 'info', status: 'cleared', clearedAt: iso(40), firstSeen: iso(80)}),
    card(KID, {num: 33, status: 'cleared', clearedAt: iso(25), firstSeen: iso(85), parent: WAITING}),
    card(TAKEN, {num: 34, status: 'cleared', clearedAt: iso(30), firstSeen: iso(70), triage: 'progress', triagedAt: iso(30), owner: 'alice@example.com', ownerName: 'Alice Smith'}),
    card(ASSIGNED, {num: 35, status: 'cleared', clearedAt: iso(35), firstSeen: iso(75), owner: 'bob.smith@example.com', assigned: {name: 'Bob Smith', by: 'alice@example.com'}}),
    card(EVENT_TAKEN, {num: 36, status: 'cleared', clearedAt: iso(32), firstSeen: began, triage: 'progress', owner: 'carol.jones@example.com', cause: {kind: 'event', what: 'the push send', firstSeen: began}})];
}

function timed(text, words) {
  return text.startsWith(words + ' ') && /^\d/.test(text.slice(words.length + 1));
}

async function aStoppedCardReadsTheSlackWordsOnTheBoardInTheChipAndInTheHistory(lang) {
  //given
  const [long, short, lower, assigned, onIt, eventOnIt] = WORDS[lang];
  const {w, doc, errors} = await boot(cards(), Object.assign({history: {actions: [], activity: []}}, lang === 'en' ? {} : {lang}));
  const a = () => article(doc, WAITING);
  const status = (key) => article(doc, key).querySelector('.inc-more .inc-tags .tag.k-cleared').textContent;
  const every = (key) => [...article(doc, key).querySelectorAll('.tag, .c-status')].map((t) => t.textContent);
  const rest = (key) => [...article(doc, key).querySelectorAll('.inc-more .inc-tags .tag')].map((t) => t.textContent);
  const chip = doc.querySelector('#bf-lists .fb-i[data-f="cleared"] .lbl');

  //when
  expand(a());
  const kid = [...a().querySelectorAll('.inc-more li .tag.plain')].map((t) => t.textContent);
  a().querySelector('.cd-tab[data-tab="history"]').click();
  await until(() => a().querySelector('.cd-hist li'));
  await wait(30);
  const labels = [...a().querySelectorAll('.cd-hist li .h-l')].map((x) => x.textContent);
  const board = doc.querySelector('#anoms').textContent, row = status(WAITING), head = article(doc, WAITING).querySelector('.inc-head .c-status').textContent, owned = status(ASSIGNED);
  doc.querySelector('#bf-lists .fb-i[data-f="progress"]').click();
  await wait(60);
  expand(article(doc, TAKEN));
  const taken = every(TAKEN).filter((t) => t.startsWith(short));
  const tip = article(doc, TAKEN).querySelector('.asg-tag').title;
  article(doc, TAKEN).querySelector('.cd-tab[data-tab="history"]').click();
  await until(() => article(doc, TAKEN).querySelector('.cd-hist li'));
  await wait(30);
  const takenLog = [...article(doc, TAKEN).querySelectorAll('.cd-hist li')].map((x) => x.textContent).join(' | ');
  const eventTaken = every(EVENT_TAKEN).filter((t) => /^\D+\d/.test(t) && t.indexOf(eventOnIt.split(' \u00b7 ')[0]) === 0);
  chip.closest('button').click();
  await wait(60);
  const listed = doc.querySelector('#anoms').textContent, calm = status(CALM);

  //then
  assert.ok(timed(row, long), 'the waiting card on the board: ' + row);
  assert.strictEqual(head, short, 'the header keeps the short state');
  assert.ok(timed(owned, assigned) && owned.indexOf('@') < 0, 'an assigned card names who has it, never an e-mail: ' + owned);
  assert.ok(timed(calm, short) && !calm.startsWith(long), 'an info card that needs nothing: ' + calm);
  assert.ok(taken.length >= 1 && taken.every((t) => timed(t, onIt)), 'a card in progress says who is on it by display name: ' + taken.join(' | '));
  assert.ok(tip.indexOf('set by') < 0 && takenLog.indexOf('Assigned to') < 0 && takenLog.indexOf('Atand') < 0, 'a take is not shown as an assignment: ' + tip + ' / ' + takenLog);
  assert.ok(eventTaken.length >= 1 && eventTaken.every((t) => timed(t, eventOnIt)), 'an event card in progress: ' + eventTaken.join(' | '));
  assert.deepStrictEqual(kid, [lower], 'the merged child');
  assert.strictEqual(chip.textContent, short, 'the filter chip keeps the short words');
  assert.strictEqual(chip.closest('button').dataset.f, 'cleared', 'the filter id stays cleared');
  assert.ok(listed.indexOf(short + ' \u00b7 ') >= 0 && article(doc, CALM), 'the chip list leads with the short words: ' + listed.slice(0, 120));
  assert.ok(labels.indexOf(short) >= 0, 'the history row: ' + labels.join(' | '));
  assert.ok(!/Cleared|Normale|quiet|sustu/.test(board + listed), 'no old word is left on the board or in the chip list');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a waiting stopped card reads ' + long + ', an assigned one ' + assigned + ', a taken one ' + onIt + ', a taken event card ' + eventOnIt + ', an info one ' + short + ' with its time, the chip and history ' + short + ', a merged child ' + lower + ' (' + lang + ')';
}

(async () => {
  for (const lang of ['en', 'tr']) {
    try { console.log('PASS ' + await aStoppedCardReadsTheSlackWordsOnTheBoardInTheChipAndInTheHistory(lang)); } catch (e) {
      console.error('FAIL stopped label ' + lang + ': ' + e.message.replace(/\x1b\[[0-9;]*m/g, '').split('\n').slice(0, 30).join(' | ')); process.exitCode = 1;
    }
  }
  process.exit(process.exitCode || 0);
})();
