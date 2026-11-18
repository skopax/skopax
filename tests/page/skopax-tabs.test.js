const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
async function run(lang, hash, saved, meDelay) {
  const errors = [];
  const vc = new VirtualConsole(); vc.on('jsdomError', (e) => errors.push(String(e.message || e).slice(0, 120)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3e6).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const dom = new JSDOM(pageHtml(process.argv[2]).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {url: 'https://skopax.example.com/' + hash, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      if (lang) w.document.cookie = 'skopax_lang=' + lang + '; path=/';
      if (saved) w.localStorage.setItem('skopax-tab', saved);
      w.scrollTo = () => {}; w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        p = String(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me')) { await wait(meDelay); return ok({loginEnabled: true, user: 'nina.white@example.com', operator: true, admin: true, teams: [], tags: [], chat: true}); }
        if (p.startsWith('/api/admin/settings')) return ok({groups: [], history: []});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  const w = dom.window, doc = w.document, out = [];
  const cur = () => [...doc.querySelectorAll('#side a.tab[data-tab][aria-current="page"]')].map((b) => b.id).join(',');
  await wait(60); out.push('early ' + cur());
  doc.getElementById('tab-chat').click(); await wait(40); out.push('after chat click ' + cur());
  await wait(meDelay + 300); out.push('after me ' + cur());
  doc.getElementById('tab-chat').click(); await wait(300); out.push('chat again ' + cur());
  doc.getElementById('tab-anoms').click(); await wait(300); out.push('anoms ' + cur() + ' hash ' + w.location.hash + ' saved ' + w.localStorage.getItem('skopax-tab'));
  w.close();
  return {out, errors};
}
(async () => {
  //when
  const lastWasSettings = await run('en', '', 'settings', 400);
  const linkToChat = await run('nl', '#t=chat', 'settings', 400);
  const linkToSettings = await run('en', '#t=settings', 'anoms', 200);

  //then
  assert.deepStrictEqual(lastWasSettings.out.slice(0, 3), ['early tab-anoms', 'after chat click tab-chat', 'after me tab-chat'], 'a tab picked while the login check loads is kept: ' + lastWasSettings.out.join(' | '));
  assert.deepStrictEqual(linkToChat.out.slice(0, 3), ['early tab-chat', 'after chat click tab-chat', 'after me tab-chat'], 'a link to another tab is not overridden by the last tab');
  assert.ok(linkToSettings.out[0] === 'early tab-anoms' && /saved anoms/.test(linkToSettings.out[4]), 'Settings is never saved as the last tab: ' + linkToSettings.out.join(' | '));
  assert.deepStrictEqual(lastWasSettings.errors.concat(linkToChat.errors, linkToSettings.errors), []);
  console.log('PASS an admin whose last tab was Settings is not pulled back to it after clicking another tab or opening a link, and Settings is not remembered as the last tab');
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
