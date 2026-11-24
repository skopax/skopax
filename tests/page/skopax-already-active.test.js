const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const CARD_PAGE = path.join(path.dirname(TEMPLATE), 'card.html');
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const SINCE = '2026-10-01T05:32:00Z';
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const LAST_YEAR = new Date(new Date().getFullYear() - 1, 3, 6, 9, 15).toISOString();
const OLD = 'zabbix|web-01|Swap is low|1004';
const QUIET = 'zabbix|web-01|High CPU utilization|1001', LOUD = 'zabbix|db-01|Disk space is low|1002', BROKEN = 'zabbix|web-01|Agent is not available|1003';
const MARK = {since: SINCE, source: 'Zabbix'};

function shown(at) {
  const d = new Date(at);
  const parts = new Intl.DateTimeFormat('en-GB', {hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}).formatToParts(d);
  const part = (t) => parts.find((p) => p.type === t).value;
  const year = d.getFullYear() !== new Date().getFullYear() ? ' ' + d.getFullYear() : '';
  return {day: String(d.getDate()), month: MONTHS[d.getMonth()], year, time: part('hour') + ':' + part('minute')};
}
const english = (at) => 'Already active when Zabbix was connected (since ' + at.day + ' ' + at.month + at.year + ' ' + at.time + ')';

function card(key, num, extra) {
  return Object.assign({key, num, sev: 'high', status: 'open', svc: key.split('|')[1], label: key.split('|')[2], src: 'zabbix', sig: 'zabbix', now: 1, firstSeen: iso(30), lastSeen: iso(1), what: 'Zabbix problem.'}, extra || {});
}

function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: iso(-59), sources: {zabbix: 'ok'}, summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(QUIET, 61, {alreadyActive: MARK}), card(LOUD, 62), card(BROKEN, 63, {alreadyActive: {since: 'not a time', source: 'Zabbix'}}), card(OLD, 64, {alreadyActive: {since: LAST_YEAR, source: 'Zabbix'}})],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

function pack(w, lang) {
  if (!lang || lang === 'en') return;
  const raw = JSON.parse(fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'locale', lang, 'page.json'), 'utf8'));
  w.SKOPAX_I18N = {[lang]: Object.assign({}, raw.page, {'@rules': raw.rules, '@months': raw.months, '@days': raw.days, '@tails': raw.tails, '@th': raw.headers})};
  w.document.cookie = 'skopax_lang=' + lang + '; path=/';
}

async function boot(lang) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE, {view: 'list'}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state())), {
    url: 'https://skopax.example.com/#t=anoms/all', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      pack(w, lang);
      w.scrollTo = () => {}; w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        p = String(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'op@example.com', operator: true, teams: [], tags: []});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/state')) return ok(state());
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(450);
  return {w: dom.window, doc: dom.window.document, errors};
}

async function bootCard(found, lang) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(CARD_PAGE), {
    url: 'https://skopax.example.com/c/' + found.num, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      pack(w, lang);
      w.fetch = async (p) => {
        if (String(p) === '/api/me') return {ok: true, status: 200, json: async () => ({loginEnabled: true, user: null, operator: false, tags: []})};
        return {ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(found))};
      };
    },
  });
  await wait(150);
  return {w: dom.window, doc: dom.window.document, errors};
}

const lineOf = (doc, key) => {
  const row = [...doc.querySelectorAll('#anoms article.inc')].find((x) => x.dataset.key === key);
  const p = row ? row.querySelector('.inc-more .already-active') : null;
  return p ? p.textContent : null;
};

async function theBoardSaysTheProblemWasAlreadyActiveWhenTheSourceWasConnected() {
  //given
  const at = shown(SINCE);
  const {w, doc, errors} = await boot('en');

  //then
  assert.strictEqual(lineOf(doc, QUIET), english(at));
  assert.strictEqual(lineOf(doc, OLD), english(shown(LAST_YEAR)), 'a start in an earlier year names the year');
  assert.ok(/ \d{4} /.test(lineOf(doc, OLD)));
  assert.strictEqual(lineOf(doc, LOUD), null, 'a card opened as today has no such line');
  assert.strictEqual(lineOf(doc, BROKEN), null, 'a mark without a readable time shows nothing');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the board shows the already-active line in the viewer time zone, only on a marked card');
}

async function theLineIsTranslatedByTheLocaleRules() {
  //given
  const at = shown(SINCE);
  const months = JSON.parse(fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'locale', 'de', 'page.json'), 'utf8')).months;
  const {w, doc, errors} = await boot('de');
  await wait(100);

  //then
  const old = shown(LAST_YEAR);
  assert.strictEqual(lineOf(doc, QUIET), 'Bereits aktiv, als Zabbix verbunden wurde (seit ' + at.day + '. ' + months[at.month] + at.year + ' ' + at.time + ')');
  assert.strictEqual(lineOf(doc, OLD), 'Bereits aktiv, als Zabbix verbunden wurde (seit ' + old.day + '. ' + months[old.month] + old.year + ' ' + old.time + ')');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS the German pack translates the line and its month');
}

async function theCardPageShowsTheSameLine() {
  //given
  const at = shown(SINCE);
  const base = {svc: 'web-01', label: 'High CPU utilization', status: 'open', sev: 'high', firstSeen: iso(30), live: true, what: 'Zabbix problem.'};
  const quiet = await bootCard(Object.assign({num: 61, key: QUIET, alreadyActive: MARK}, base));
  const loud = await bootCard(Object.assign({num: 62, key: LOUD}, base));
  const line = (doc) => { const p = doc.querySelector('.already-active'); return p ? p.textContent : null; };

  //then
  const old = await bootCard(Object.assign({num: 64, key: OLD, alreadyActive: {since: LAST_YEAR, source: 'Zabbix'}}, base));
  const french = await bootCard(Object.assign({num: 61, key: QUIET, alreadyActive: MARK}, base), 'fr');
  const months = JSON.parse(fs.readFileSync(path.join(path.dirname(TEMPLATE), '..', 'locale', 'fr', 'page.json'), 'utf8')).months;
  await wait(100);
  assert.strictEqual(line(quiet.doc), english(at));
  assert.strictEqual(line(old.doc), english(shown(LAST_YEAR)));
  assert.strictEqual(line(french.doc), 'D\u00e9j\u00e0 actif quand Zabbix a \u00e9t\u00e9 connect\u00e9 (depuis le ' + at.day + ' ' + months[at.month] + at.year + ' ' + at.time + ')');
  assert.strictEqual(line(loud.doc), null);
  assert.deepStrictEqual(quiet.errors.concat(loud.errors, old.errors, french.errors), []);
  quiet.w.close(); loud.w.close(); old.w.close(); french.w.close();
  console.log('PASS the card page shows the already-active line');
}

(async () => {
  await theBoardSaysTheProblemWasAlreadyActiveWhenTheSourceWasConnected();
  await theLineIsTranslatedByTheLocaleRules();
  await theCardPageShowsTheSameLine();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
