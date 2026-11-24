const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

function media(width) {
  return (q) => {
    const parts = String(q).split(/\s+and\s+/);
    const matches = parts.every((part) => {
      const m = /\((min|max)-width:\s*(\d+)px\)/.exec(part);
      if (!m) return false;
      return m[1] === 'min' ? width >= Number(m[2]) : width <= Number(m[2]);
    });
    return {matches, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}};
  };
}

async function boot(opts) {
  opts = opts || {};
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3e6).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/#t=anoms', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.matchMedia = media(opts.width);
      if (opts.stored) w.localStorage.setItem('skopax-sidebar', opts.stored);
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', operator: true, admin: false, teams: [], tags: [], chat: true});
        if (p.startsWith('/api/server')) return ok({plugins: [], tz: 'UTC'});
        if (p.startsWith('/api/admin/settings')) return ok({items: [], rev: 1, refreshSeconds: 15});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
      };
    },
  });
  await wait(250);
  return {dom, doc: dom.window.document, w: dom.window, errors};
}

(async () => {
  //given
  const fresh = await boot({width: 1440});
  const full = await boot({width: 1440, stored: 'full'});
  const rail = await boot({width: 1440, stored: 'rail'});
  const phone = await boot({width: 400});

  //when
  const isRail = (x) => x.doc.body.classList.contains('is-rail');
  const before = isRail(fresh);
  fresh.doc.getElementById('side-rail').click();
  const afterOpen = [isRail(fresh), fresh.w.localStorage.getItem('skopax-sidebar')];

  //then
  assert.strictEqual(before, true, 'no saved choice on a desktop starts as the rail');
  assert.deepStrictEqual(afterOpen, [false, 'full'], 'opening the menu is remembered');
  assert.strictEqual(isRail(full), false, 'a saved full menu stays full');
  assert.strictEqual(isRail(rail), true, 'a saved rail stays a rail');
  assert.strictEqual(isRail(phone), false, 'the phone layout is unchanged: no rail, the menu is a drawer');
  assert.deepStrictEqual([fresh, full, rail, phone].flatMap((x) => x.errors), []);
  for (const x of [fresh, full, rail, phone]) x.w.close();
  console.log('PASS the menu starts collapsed on a desktop until the reader opens it, and the phone layout is unchanged');
})().catch((e) => { console.log('FAIL ' + (e.stack || e)); process.exit(1); });
