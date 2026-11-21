const assert = require('assert');
const {boot, card, iso, wait, until, article, expand} = require('./skopax-card-polish.test.js');

const WAITING = 'log|checkout-service|ERROR|boom';
const CALM = 'log|shop-web|INFO|calm';
const KID = 'log|checkout-service|WARN|kid';
const TAKEN = 'log|cart-service|ERROR|taken';
const WORDS = {en: ['Stopped firing \u00b7 awaiting review', 'Stopped firing', 'stopped firing'], tr: ['Sinyal durdu \u00b7 inceleme bekleniyor', 'Sinyal durdu', 'sinyal durdu']};

function cards() {
  return [card(WAITING, {num: 31, status: 'cleared', clearedAt: iso(20), firstSeen: iso(90)}), card(CALM, {num: 32, sev: 'info', status: 'cleared', clearedAt: iso(40), firstSeen: iso(80)}),
    card(KID, {num: 33, status: 'cleared', clearedAt: iso(25), firstSeen: iso(85), parent: WAITING}),
    card(TAKEN, {num: 34, status: 'cleared', clearedAt: iso(30), firstSeen: iso(70), triage: 'progress', triagedBy: 'alice@example.com'})];
}

function timed(text, words) {
  return text.startsWith(words + ' ') && /^\d/.test(text.slice(words.length + 1));
}

async function aStoppedCardReadsTheSlackWordsOnTheBoardInTheChipAndInTheHistory(lang) {
  //given
  const [long, short, lower] = WORDS[lang];
  const {w, doc, errors} = await boot(cards(), Object.assign({history: {actions: [], activity: []}}, lang === 'en' ? {} : {lang}));
  const a = () => article(doc, WAITING);
  const status = (key) => article(doc, key).querySelector('.inc-head .c-status').textContent;
  const rest = (key) => [...article(doc, key).querySelectorAll('.inc-more .inc-tags .tag')].map((t) => t.textContent);
  const chip = doc.querySelector('#tab-sub-anoms button[data-f="cleared"] .lbl');

  //when
  expand(a());
  const kid = [...a().querySelectorAll('.inc-more li .tag.plain')].map((t) => t.textContent);
  a().querySelector('.cd-tab[data-tab="history"]').click();
  await until(() => a().querySelector('.cd-hist li'));
  await wait(30);
  const labels = [...a().querySelectorAll('.cd-hist li .h-l')].map((x) => x.textContent);
  const board = doc.querySelector('#anoms').textContent, row = status(WAITING);
  doc.querySelector('#tab-sub-anoms button[data-f="progress"]').click();
  await wait(60);
  expand(article(doc, TAKEN));
  const taken = rest(TAKEN).filter((t) => t.startsWith(short));
  chip.closest('button').click();
  await wait(60);
  const listed = doc.querySelector('#anoms').textContent, calm = status(CALM);

  //then
  assert.ok(timed(row, long), 'the waiting card on the board: ' + row);
  assert.ok(timed(calm, short) && !calm.startsWith(long), 'an info card that needs nothing: ' + calm);
  assert.ok(taken.length === 1 && timed(taken[0], short) && !taken[0].startsWith(long), 'a card in progress: ' + taken.join(' | '));
  assert.deepStrictEqual(kid, [lower], 'the merged child');
  assert.strictEqual(chip.textContent, short, 'the filter chip keeps the short words');
  assert.strictEqual(chip.closest('button').dataset.f, 'cleared', 'the filter id stays cleared');
  assert.ok(listed.indexOf(short + ' \u00b7 ') >= 0 && article(doc, CALM), 'the chip list leads with the short words: ' + listed.slice(0, 120));
  assert.ok(labels.indexOf(short) >= 0, 'the history row: ' + labels.join(' | '));
  assert.ok(!/Cleared|Normale|quiet|sustu/.test(board + listed), 'no old word is left on the board or in the chip list');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a waiting stopped card reads ' + long + ', an info or taken one ' + short + ' with its time, the chip and history ' + short + ', a merged child ' + lower + ' (' + lang + ')';
}

(async () => {
  for (const lang of ['en', 'tr']) {
    try { console.log('PASS ' + await aStoppedCardReadsTheSlackWordsOnTheBoardInTheChipAndInTheHistory(lang)); } catch (e) {
      console.error('FAIL stopped label ' + lang + ': ' + e.message.replace(/\x1b\[[0-9;]*m/g, '').split('\n').slice(0, 30).join(' | ')); process.exitCode = 1;
    }
  }
  process.exit(process.exitCode || 0);
})();
