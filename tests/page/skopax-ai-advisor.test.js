const assert = require('assert');
const {boot, wait, nameOf} = require('./skopax-ai-kit');

const TEMPLATE = process.argv[2];
const PHONE = '(max-width: 640px), (max-height: 500px) and (pointer: coarse)';
const LOOPS = ['card', 'critical', 'high', 'slack', 'chat'];
const KEYS = {card: 'investigate.model', critical: 'investigate.model-critical', high: 'investigate.model-high', slack: 'investigate.slack-model', chat: 'chat.model'};

const rowOf = (doc, use) => doc.querySelector('#ai-rows .aiu-row[data-use="' + use + '"]');
const adv = (doc, use) => rowOf(doc, use).querySelector('.aiu-adv');
const field = (doc, use, part) => adv(doc, use).querySelector('.aiu-adv-' + part);
const pick = (w, sel, value) => {
  sel.value = value;
  sel.dispatchEvent(new w.Event('input', {bubbles: true}));
  sel.dispatchEvent(new w.Event('change', {bubbles: true}));
};
const puts = (calls) => calls.filter((c) => c[0] === 'PUT').map((c) => [c[1], c[2]]);
const WORDS = ['Advisor', 'Anthropic only', 'Advisor consultations per run', 'Advisor answer size'];

async function everyToolLoopUseGetsAnAdvisorSelectAndOthersDoNot() {
  //given
  const {dom, doc, errors} = await boot(TEMPLATE);

  //when
  const have = [...doc.querySelectorAll('#ai-rows .aiu-row')].filter((r) => r.querySelector('.aiu-adv')).map((r) => r.dataset.use);
  const options = (use) => [...field(doc, use, 'in').options].map((o) => [o.value, o.textContent]);

  //then
  assert.deepStrictEqual(have, LOOPS, 'only the five tool-loop uses have an advisor');
  assert.deepStrictEqual(options('card'), [['', 'None'], ['claude-opus-5-5', 'claude-opus-5-5']], 'None plus the choices the server lists');
  assert.strictEqual(field(doc, 'card', 'in').value, '', 'off by default');
  assert.strictEqual(field(doc, 'card', 'in').disabled, false);
  assert.strictEqual(adv(doc, 'card').querySelector('.aiu-adv-k').textContent, 'Advisor');
  assert.strictEqual(nameOf(doc, field(doc, 'card', 'in')), 'Card runs Advisor', 'named by its use and Advisor');
  assert.ok(field(doc, 'card', 'in').getAttribute('aria-labelledby').split(' ').every((id) => doc.getElementById(id)), 'the select is named by existing elements');
  assert.ok(!adv(doc, 'card').querySelector('.aiu-adv-uses') && !adv(doc, 'card').querySelector('.aiu-adv-tokens'), 'no size fields while no model is picked');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the five tool-loop uses show an Advisor select with None and the offered models, off by default, and no other use does';
}

async function aUseThatIsNotCapableShowsAnthropicOnlyAndADisabledSelect() {
  //given
  const {dom, doc, errors} = await boot(TEMPLATE);

  //when
  const high = field(doc, 'high', 'in'), note = adv(doc, 'high').querySelector('.aiu-adv-note');

  //then
  assert.strictEqual(high.disabled, true);
  assert.deepStrictEqual([...high.options].map((o) => o.value), [''], 'nothing to choose');
  assert.strictEqual(note.textContent, 'Anthropic only');
  assert.ok(high.getAttribute('aria-describedby').split(' ').indexOf(note.id) >= 0, 'the note is tied to the select');
  assert.ok(!adv(doc, 'card').querySelector('.aiu-adv-note'), 'a capable use has no note');
  assert.ok(!adv(doc, 'high').querySelector('.aiu-adv-uses'), 'and no size fields');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a use whose provider lacks the capability shows the select disabled with Anthropic only';
}

async function aPickedModelShowsTheFieldsAndEverySaveGoesThroughTheSettingsApi() {
  //given
  const {dom, w, doc, errors, calls, srv} = await boot(TEMPLATE);

  //when
  pick(w, field(doc, 'critical', 'in'), 'claude-opus-5-5');
  await wait(200);
  const afterPick = [puts(calls), field(doc, 'critical', 'in').value, field(doc, 'critical', 'uses').value, field(doc, 'critical', 'tokens').value];
  const uses = field(doc, 'critical', 'uses');
  uses.value = '5';
  uses.dispatchEvent(new w.Event('change', {bubbles: true}));
  await wait(200);
  const tokens = field(doc, 'critical', 'tokens');
  tokens.value = '1500';
  tokens.dispatchEvent(new w.Event('change', {bubbles: true}));
  await wait(200);
  const afterAll = [field(doc, 'critical', 'uses').value, field(doc, 'critical', 'tokens').value];
  pick(w, field(doc, 'critical', 'in'), '');
  await wait(200);

  //then
  assert.deepStrictEqual(afterPick[0], [['/api/admin/settings', {key: 'investigate.model-critical-advisor', value: 'claude-opus-5-5'}]]);
  assert.deepStrictEqual(afterPick.slice(1), ['claude-opus-5-5', '3', '2000'], 'the fields appear with the defaults');
  assert.deepStrictEqual(puts(calls).slice(1), [['/api/admin/settings', {key: 'investigate.model-critical-advisor-uses', value: '5'}], ['/api/admin/settings', {key: 'investigate.model-critical-advisor-tokens', value: '1500'}],
    ['/api/admin/settings', {key: 'investigate.model-critical-advisor', value: ''}]], 'each field is its own setting write, None writes an empty value');
  assert.deepStrictEqual(afterAll, ['5', '1500']);
  assert.deepStrictEqual(srv.adv.critical, {model: '', maxUses: 5, maxTokens: 1500});
  assert.ok(!adv(doc, 'critical').querySelector('.aiu-adv-uses'), 'None hides the fields again');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'picking a model shows the consultations and answer-size fields with their defaults; each change writes its own setting and None hides the fields';
}

async function theFieldsKeepTheirBounds() {
  //given
  const {dom, doc, errors} = await boot(TEMPLATE, {advisor: {card: {model: 'claude-opus-5-5', maxUses: 4, maxTokens: 2000}}});

  //when
  const uses = field(doc, 'card', 'uses'), tokens = field(doc, 'card', 'tokens');

  //then
  assert.deepStrictEqual([uses.type, uses.min, uses.max, uses.value], ['number', '1', '10', '4']);
  assert.deepStrictEqual([tokens.type, tokens.min, tokens.max, tokens.value], ['number', '1024', '4096', '2000']);
  assert.ok(uses.id && tokens.id && doc.querySelector('label[for="' + uses.id + '"]').textContent === 'Advisor consultations per run' && doc.querySelector('label[for="' + tokens.id + '"]').textContent === 'Advisor answer size');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the consultation field is 1 to 10, the size field 1024 to 4096, and both have labels';
}

async function aValueOutsideTheBoundsIsRefusedOnThePageWithTheTranslatedReason() {
  //given
  const {dom, w, doc, errors, calls} = await boot(TEMPLATE, {advisor: {card: {model: 'claude-opus-5-5', maxUses: 4, maxTokens: 2000}}});
  const de = await boot(TEMPLATE, {lang: 'de', advisor: {card: {model: 'claude-opus-5-5'}}});
  const type = async (d, w2, part, value) => {
    const f = field(d, 'card', part);
    f.value = value;
    f.dispatchEvent(new w2.Event('change', {bubbles: true}));
    await wait(100);
    return [adv(d, 'card').querySelector('.aiu-adv-msg').textContent, field(d, 'card', part).value];
  };

  //when
  const got = [await type(doc, w, 'uses', '0'), await type(doc, w, 'uses', '11'), await type(doc, w, 'uses', '2.5'), await type(doc, w, 'tokens', '1023'), await type(doc, w, 'tokens', '4097'), await type(doc, w, 'tokens', '')];
  const german = await type(de.doc, de.w, 'tokens', '10');

  //then
  assert.deepStrictEqual(got, [['Enter a whole number from 1 to 10', '4'], ['Enter a whole number from 1 to 10', '4'], ['Enter a whole number from 1 to 10', '4'],
    ['Enter a whole number from 1024 to 4096', '2000'], ['Enter a whole number from 1024 to 4096', '2000'], ['Enter a whole number from 1024 to 4096', '2000']], 'each bad value shows the range and the field returns to the saved value');
  assert.strictEqual(puts(calls).length, 0, 'nothing was sent to the server');
  assert.ok(adv(doc, 'card').querySelector('.aiu-adv-msg').classList.contains('bad'));
  assert.ok(german[0] !== 'Enter a whole number from 1024 to 4096' && german[0].indexOf('1024') >= 0 && german[0].indexOf('4096') >= 0, 'the German pack translates the reason');
  assert.strictEqual(puts(de.calls).length, 0);
  assert.deepStrictEqual(errors.concat(de.errors), []);
  dom.window.close();
  de.dom.window.close();
  return 'a consultation count outside 1 to 10 or an answer size outside 1024 to 4096 is refused on the page with a translated range message and nothing is sent';
}

async function aRefusedAdvisorSaveSaysWhyAndKeepsTheOldValue() {
  //given
  const {dom, w, doc, errors, srv} = await boot(TEMPLATE);
  srv.refuse = 'This model cannot advise that executor';

  //when
  pick(w, field(doc, 'card', 'in'), 'claude-opus-5-5');
  await wait(200);

  //then
  assert.strictEqual(adv(doc, 'card').querySelector('.aiu-adv-msg').textContent, 'This model cannot advise that executor');
  assert.ok(adv(doc, 'card').querySelector('.aiu-adv-msg').classList.contains('bad'));
  assert.strictEqual(field(doc, 'card', 'in').value, '', 'the select returns to what the server holds');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a refused advisor save shows the server reason and the select returns to the saved value';
}

async function anAdvisorProblemShowsOnTheRowLikeOtherProblems() {
  //given
  const problem = 'The advisor setting is ignored: this provider cannot use an advisor';
  const {dom, doc, errors} = await boot(TEMPLATE, {advisor: {high: {model: 'claude-opus-5-5', problem}}});

  //when
  const row = rowOf(doc, 'high'), note = row.querySelector('.aiu-adv .aiu-problem');

  //then
  assert.strictEqual(note.textContent, problem);
  assert.ok(note.classList.contains('bad') && row.classList.contains('aiu-bad'));
  assert.ok(field(doc, 'high', 'in').getAttribute('aria-describedby').split(' ').indexOf(note.id) >= 0);
  assert.strictEqual(field(doc, 'high', 'in').disabled, true, 'a stored advisor on an incapable use never becomes editable');
  assert.ok(!doc.querySelector('#ai-rows .aiu-row[data-use="card"] .aiu-problem'));
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'an advisor problem is a red line on its use row, tied to the select';
}

async function aHostileModelOrProblemIsText() {
  //given
  const evil = '<img src=x onerror=alert(1)>';
  const {dom, doc, errors} = await boot(TEMPLATE, {advisor: {card: {model: evil, choices: [evil, 'claude-opus-5-5'], problem: evil}}});

  //when
  const row = rowOf(doc, 'card');

  //then
  assert.strictEqual(row.querySelectorAll('img').length, 0);
  assert.ok([...field(doc, 'card', 'in').options].some((o) => o.textContent === evil));
  assert.strictEqual(row.querySelector('.aiu-adv .aiu-problem').textContent, evil);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'hostile advisor strings are drawn as text';
}

async function theAdvisorWordsAreTranslated() {
  //given
  const packs = {};
  for (const lang of ['de', 'es', 'fr', 'tr']) packs[lang] = await boot(TEMPLATE, {lang, advisor: {card: {model: 'claude-opus-5-5'}}});

  //when
  const seen = (lang) => {
    const d = packs[lang].doc;
    return [adv(d, 'card').querySelector('.aiu-adv-k').textContent, adv(d, 'high').querySelector('.aiu-adv-note').textContent,
      d.querySelector('label[for="' + field(d, 'card', 'uses').id + '"]').textContent, d.querySelector('label[for="' + field(d, 'card', 'tokens').id + '"]').textContent, [...field(d, 'card', 'in').options][0].textContent];
  };

  //then
  for (const lang of Object.keys(packs)) {
    const got = seen(lang);
    WORDS.forEach((word, n) => assert.notStrictEqual(got[n], word, lang + ' translates ' + word));
    assert.deepStrictEqual(packs[lang].errors, []);
    packs[lang].dom.window.close();
  }
  return 'the Advisor label, the Anthropic only note and the two field labels are translated into de, es, fr and tr';
}

async function phoneWidthLetsTheAdvisorFillTheCard() {
  //given
  const {dom, doc, errors} = await boot(TEMPLATE);
  const top = [...doc.querySelector('style').sheet.cssRules];
  const phone = top.filter((r) => r.media && r.media.mediaText === PHONE).flatMap((r) => [...r.cssRules]).filter((r) => r.selectorText);
  const media = top.filter((r) => r.media && r.media.mediaText.indexOf('max-width: 640px') >= 0 && r.media.mediaText !== PHONE && /aiu-adv/.test(r.cssText));
  const value = (selector, prop) => phone.filter((r) => r.selectorText.split(',').map((s) => s.trim()).indexOf(selector) >= 0).map((r) => r.style.getPropertyValue(prop)).filter(Boolean).slice(-1)[0] || '';

  //when
  const got = [value('.aiu-adv select', 'width'), value('.aiu-adv input', 'min-height')];

  //then
  assert.deepStrictEqual(got, ['100%', '44px'], 'on a phone the advisor select and fields fill the card with a 44 px touch height');
  assert.deepStrictEqual(media.length, 0, 'no second phone block');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'on a phone the advisor controls are full width with touch-sized heights, in the single phone block';
}

(async () => {
  console.log('PASS ' + await everyToolLoopUseGetsAnAdvisorSelectAndOthersDoNot());
  console.log('PASS ' + await aUseThatIsNotCapableShowsAnthropicOnlyAndADisabledSelect());
  console.log('PASS ' + await aPickedModelShowsTheFieldsAndEverySaveGoesThroughTheSettingsApi());
  console.log('PASS ' + await theFieldsKeepTheirBounds());
  console.log('PASS ' + await aValueOutsideTheBoundsIsRefusedOnThePageWithTheTranslatedReason());
  console.log('PASS ' + await aRefusedAdvisorSaveSaysWhyAndKeepsTheOldValue());
  console.log('PASS ' + await anAdvisorProblemShowsOnTheRowLikeOtherProblems());
  console.log('PASS ' + await aHostileModelOrProblemIsText());
  console.log('PASS ' + await theAdvisorWordsAreTranslated());
  console.log('PASS ' + await phoneWidthLetsTheAdvisorFillTheCard());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
