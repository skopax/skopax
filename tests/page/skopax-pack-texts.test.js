const pageHtml = require('./skopax-page');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const SKOPAX = path.join(path.dirname(TEMPLATE), '..');
const CORE_PACK = /^CORE_PACK = '(.+)'$/m.exec(fs.readFileSync(path.join(SKOPAX, 'locales.py'), 'utf8'))[1];
const MESSAGES = fs.readFileSync(path.join(SKOPAX, 'messages.py'), 'utf8');
const SLOT = '<script type="text/plain" id="plugin-scripts"></script>';
const LANGS = ['de', 'es', 'fr', 'tr'];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

function english(key, values) {
  const m = new RegExp("^    '" + key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + "': '((?:[^'\\\\]|\\\\.)*)',$", 'm').exec(MESSAGES);
  assert.ok(m, 'messages.py holds ' + key);
  return m[1].replace(/\{(\w+)\}/g, (all, k) => (k in values ? String(values[k]) : all));
}

function builtIn(lang) {
  const raw = JSON.parse(fs.readFileSync(path.join(SKOPAX, 'locale', lang, 'page.json'), 'utf8'));
  return Object.assign({}, raw.page, {'@rules': raw.rules, '@months': raw.months, '@days': raw.days, '@tails': raw.tails, '@th': raw.headers});
}

function served(lang) {
  const args = [JSON.stringify(lang), JSON.stringify(builtIn(lang)).replace(/</g, '\\u003c')];
  return '<script>' + CORE_PACK.replace(/%s/g, () => args.shift()) + '</script>';
}

function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)}, anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(lang) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state())).replace(SLOT, () => served(lang));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=anoms/all', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.document.cookie = 'skopax_lang=' + lang + '; path=/';
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (String(p).startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', operator: true, teams: [], tags: []});
        if (String(p).startsWith('/api/actions') || String(p).startsWith('/api/deploy-reports')) return ok([]);
        if (String(p).startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(250);
  return {w: dom.window, errors};
}

async function each(fn) {
  for (const lang of LANGS) {
    const {w, errors} = await boot(lang);
    await fn(w, lang);
    assert.deepStrictEqual(errors, [], lang);
    w.close();
  }
}

async function check(name, fn) {
  await fn();
  console.log('PASS ' + name);
}

(async () => {
  await check('the tag API errors shown on the admin page read in every built-in pack, with the tag, address and limit kept', async () => {
    await each(async (w, lang) => {
      //given
      const texts = [english('tags.scope_missing', {}), english('tags.no_tag', {tag: 'payments'}), english('tags.bad_email', {email: 'alice@example'}), english('tags.too_many', {n: 20})];

      //when
      const said = texts.map((t) => w.tx(t));

      //then
      said.forEach((s, i) => assert.notStrictEqual(s, texts[i], lang + ': ' + texts[i]));
      assert.ok(said[1].includes('payments'), lang + ': ' + said[1]);
      assert.ok(said[2].includes('alice@example'), lang + ': ' + said[2]);
      assert.ok(said[3].includes('20'), lang + ': ' + said[3]);
    });
  });
})().catch((e) => { console.log('FAIL', e.message); process.exit(1); });
