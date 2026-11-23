const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const ME = {loginEnabled: true, sso: false, user: 'alice.archer@example.com', name: 'Alice A. Archer', userRole: 'operator', source: 'local', canChangePassword: true, operator: true, admin: false, teams: [], tags: [], chat: true};
const ALL = ['assigned', 'unassigned', 'progress', 'resolved', 'reopened', 'ticket', 'severity', 'merged', 'ungrouped', 'muted', 'known', 'unmuted', 'auto', 'fired', 'escalated', 'stopped', 'run', 'slack', 'thread', 'reminder', 'answer'];

function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

function pack(lang) {
  const raw = JSON.parse(fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'locale', lang, 'page.json'), 'utf8'));
  return {[lang]: Object.assign({}, raw.page, {'@rules': raw.rules, '@months': raw.months, '@days': raw.days, '@tails': raw.tails, '@th': raw.headers})};
}

async function boot(opts) {
  opts = opts || {};
  const errors = [], puts = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  let saved = Object.assign({on: false, kinds: ALL.slice(), all: ALL.slice(), email: 'alice.archer@example.com', ready: true}, opts.mail || {});
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state())), {
    url: 'https://skopax.example.com/#t=prefs', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      if (opts.lang) {
        w.SKOPAX_I18N = pack(opts.lang);
        w.document.cookie = 'skopax_lang=' + opts.lang + '; path=/';
      }
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p, o) => {
        p = String(p);
        const method = (o && o.method) || 'GET';
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p === '/api/me/notice-mail') {
          if (opts.signedOut) return {ok: false, status: 401, json: async () => ({}), text: async () => ''};
          if (method === 'PUT') {
            const sent = JSON.parse(o.body);
            puts.push(sent);
            if (opts.failPut) return {ok: false, status: 503, json: async () => ({detail: 'down'}), text: async () => ''};
            saved = Object.assign({}, saved, sent);
          }
          return ok(saved);
        }
        if (p.startsWith('/api/me')) return ok(opts.signedOut ? {loginEnabled: true, user: null, operator: false, admin: false, teams: [], tags: []} : ME);
        if (p.startsWith('/api/notifications/count')) return ok({unread: 0});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
      };
    },
  });
  await wait(300);
  return {dom, w: dom.window, doc: dom.window.document, errors, puts, setMail: (patch) => { saved = Object.assign({}, saved, patch); }};
}

const kinds = (doc) => [...doc.querySelectorAll('#pf-mail .pf-kinds input[type="checkbox"]')];
const named = (doc) => kinds(doc).map((c) => c.closest('label').textContent.trim());

async function theEmailSectionIsOffByDefaultAndSavesTheSwitchAndEachKind() {
  //given
  const {dom, w, doc, errors, puts} = await boot();
  const sec = doc.getElementById('pf-mail');
  const on = doc.getElementById('pf-mail-on');

  //then
  assert.ok(sec && !sec.hidden, 'the Email section shows for a signed-in person');
  assert.strictEqual(sec.querySelector('h3').textContent, 'Email');
  assert.strictEqual(on.getAttribute('role'), 'switch');
  assert.strictEqual(on.closest('label').textContent.trim(), 'Email me my notifications');
  assert.ok(!on.checked && !on.disabled, 'off by default and usable with a mail server');
  assert.strictEqual(doc.getElementById('pf-mail-to').textContent, 'alice.archer@example.com', 'the account address is shown');
  assert.strictEqual(doc.getElementById('pf-mail-to').tagName, 'SPAN', 'read-only text, not a field');
  assert.ok(doc.getElementById('pf-mail-none').hidden, 'no mail server note with a mail server');
  assert.ok(doc.getElementById('pf-mail-kinds').hidden, 'the kinds show only once the switch is on');

  //when
  on.click();
  await wait(50);

  //then
  assert.deepStrictEqual(puts, [{on: true, kinds: ALL}], 'turning on keeps every kind');
  assert.ok(!doc.getElementById('pf-mail-kinds').hidden);
  assert.strictEqual(kinds(doc).length, ALL.length);
  assert.deepStrictEqual(named(doc).slice(0, 3), ['Assigned to you', 'No longer assigned to you', 'In progress'], 'named as the Notifications page names them');
  assert.ok(named(doc).includes('Reminder sent') && named(doc).includes('Auto-resolved') && named(doc).includes('Chat answer ready'));

  //when
  const reminder = kinds(doc)[ALL.indexOf('reminder')];
  reminder.click();
  await wait(50);

  //then
  assert.deepStrictEqual(puts[1], {on: true, kinds: ALL.filter((k) => k !== 'reminder')});
  assert.ok(!kinds(doc)[ALL.indexOf('reminder')].checked);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the Email section is off by default, shows the account address, and saves the switch and each kind';
}

async function withoutAMailServerTheSwitchIsDisabledWithANote() {
  //given
  const {dom, doc, errors, puts} = await boot({mail: {ready: false}});
  const on = doc.getElementById('pf-mail-on');

  //when
  on.click();
  await wait(50);

  //then
  assert.ok(on.disabled, 'the switch is disabled');
  assert.ok(!doc.getElementById('pf-mail-none').hidden);
  assert.strictEqual(doc.getElementById('pf-mail-none').textContent, 'Your admin has not set up a mail server');
  assert.deepStrictEqual(puts, []);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'without a Mail server the switch is disabled and the note says the admin has not set one up';
}

async function aDisabledSwitchIsNeverShownOn() {
  //given
  const {dom, w, doc, errors, puts, setMail} = await boot({mail: {ready: false, on: true}});
  const on = doc.getElementById('pf-mail-on');

  //when
  on.click();
  await wait(50);

  //then
  assert.ok(on.disabled && !on.checked, 'without a mail server the held choice shows off');
  assert.ok(doc.getElementById('pf-mail-kinds').hidden, 'no kinds under a switch shown off');
  assert.deepStrictEqual(puts, [], 'nothing is saved, so the held choice stays on');

  //when
  setMail({ready: true});
  w.showTab('anoms');
  w.showTab('prefs');
  await wait(100);

  //then
  assert.ok(doc.getElementById('pf-mail-on').checked && !doc.getElementById('pf-mail-on').disabled, 'on again once a mail server is set up');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a disabled switch is never shown on, and the held choice comes back with a mail server';
}

async function openingPreferencesAgainReadsAMailServerSetUpMeanwhile() {
  //given
  const {dom, w, doc, errors, setMail} = await boot({mail: {ready: false}});
  assert.ok(!doc.getElementById('pf-mail-none').hidden, 'no mail server yet');

  //when
  setMail({ready: true});
  w.showTab('anoms');
  w.showTab('prefs');
  await wait(100);

  //then
  assert.ok(doc.getElementById('pf-mail-none').hidden, 'the no mail server note is gone without a reload');
  assert.ok(!doc.getElementById('pf-mail-on').disabled, 'the switch can be used');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'opening Preferences again reads a mail server set up meanwhile, without a reload';
}

async function aFailedSaveTurnsTheSwitchBack() {
  //given
  const {dom, doc, errors} = await boot({failPut: true});
  const on = doc.getElementById('pf-mail-on');

  //when
  on.click();
  await wait(50);

  //then
  assert.ok(!on.checked, 'the switch shows what is saved');
  assert.strictEqual(doc.getElementById('pf-mail-note').textContent, 'Not saved, try again');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a failed save turns the switch back and says it was not saved';
}

async function signedOutThereIsNoEmailSection() {
  //given
  const {dom, doc, errors} = await boot({signedOut: true});

  //then
  assert.ok(doc.getElementById('pf-mail').hidden);
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a signed-out page has no Email section';
}

async function theSectionReadsInAPack() {
  //given
  const {dom, doc, errors} = await boot({lang: 'de', mail: {on: true, kinds: ['assigned'], ready: false}});
  await wait(100);
  const sec = doc.getElementById('pf-mail');

  //then
  assert.strictEqual(sec.querySelector('h3').textContent, 'E-Mail');
  assert.strictEqual(doc.getElementById('pf-mail-on').closest('label').textContent.trim(), 'Meine Benachrichtigungen per E-Mail senden');
  assert.strictEqual(doc.getElementById('pf-mail-none').textContent, 'Ihr Admin hat keinen Mailserver eingerichtet');
  assert.strictEqual(named(doc)[0], 'Ihnen zugewiesen', 'the kinds use the Notifications page words of the pack');
  assert.strictEqual(doc.getElementById('pf-mail-to').textContent, 'alice.archer@example.com', 'the address is never translated');
  assert.ok(doc.getElementById('pf-mail-on').disabled && !doc.getElementById('pf-mail-on').checked, 'without a mail server a stored on shows off and disabled');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'the Email section reads in the de pack, the address stays as it is';
}

(async () => {
  console.log('PASS ' + await theEmailSectionIsOffByDefaultAndSavesTheSwitchAndEachKind());
  console.log('PASS ' + await withoutAMailServerTheSwitchIsDisabledWithANote());
  console.log('PASS ' + await aDisabledSwitchIsNeverShownOn());
  console.log('PASS ' + await openingPreferencesAgainReadsAMailServerSetUpMeanwhile());
  console.log('PASS ' + await aFailedSaveTurnsTheSwitchBack());
  console.log('PASS ' + await signedOutThereIsNoEmailSection());
  console.log('PASS ' + await theSectionReadsInAPack());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
