const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const DEFAULT_RULES = 'Reply in one short friendly line in the language of the message.';

async function boot() {
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state));
  const defaults = {enabled: true, rules: DEFAULT_RULES, hourlyCap: 10};
  let talk = Object.assign({}, defaults, {custom: false, updatedAt: null, updatedBy: null, usedThisHour: 2, defaults});
  const hist = [];
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=settings', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, opts) => {
        const p = String(path), method = (opts && opts.method) || 'GET';
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/admin/small-talk')) {
          calls.push([method, opts && opts.body ? JSON.parse(opts.body) : null]);
          if (method === 'PUT') {
            const b = JSON.parse(opts.body);
            if (b.rules !== talk.rules) hist.unshift({id: 'h' + (hist.length + 1), key: 'small-talk.rules', old: talk.rules, new: b.rules, by: 'nina.white@example.com', at: iso(0), talk: true});
            if (b.hourlyCap !== talk.hourlyCap) hist.unshift({id: 'h' + (hist.length + 1), key: 'small-talk.hourly-cap', old: String(talk.hourlyCap), new: String(b.hourlyCap), by: 'nina.white@example.com', at: iso(0), talk: true});
            talk = Object.assign({}, talk, b, {custom: true, updatedBy: 'nina.white@example.com', updatedAt: iso(0)});
          }
          if (method === 'DELETE') talk = Object.assign({}, talk, defaults, {custom: false, updatedBy: null, updatedAt: null});
          return ok(talk);
        }
        if (p.startsWith('/api/admin/history')) return ok(hist);
        if (p.startsWith('/api/admin/teams')) return ok({routes: [], tags: [], hosts: []});
        if (p.startsWith('/api/admin/settings')) return ok({items: [{key: 'investigate.rooms', group: 'Investigator', label: 'Rooms', help: 'Rooms', kind: 'int', live: true, value: '2', base: '2', default: '2', source: 'default', by: null, at: null, low: 1, high: 4}], rev: 1, refreshSeconds: 15});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'nina.white@example.com', operator: true, admin: true, teams: [], tags: []});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(450);
  return {w: dom.window, doc: dom.window.document, errors, calls};
}

const type = (w, el, value) => { el.value = value; el.dispatchEvent(new w.Event('input')); };

async function anAdminEditsTheSmallTalkRulesOnTheAdminPage() {
  //given
  const {w, doc, errors, calls} = await boot();
  const grp = () => doc.querySelector('#admin details.adm-talk');
  const row = () => grp().querySelector('.talk-row');
  const q = (sel) => row().querySelector(sel);
  const first = {title: grp().querySelector('summary h3').textContent, on: q('.talk-on').value, cap: q('.talk-cap').value, rules: q('.talk-rules').value,
    source: q('.adm-n .tag').textContent, used: q('.adm-key').textContent, saveOff: q('.talk-save').disabled, reset: !!q('.talk-reset')};

  //when
  type(w, q('.talk-cap'), '61');
  const tooMany = [q('.talk-save').disabled, q('.adm-msg').textContent];
  type(w, q('.talk-cap'), '4');
  type(w, q('.talk-rules'), '  Answer briefly and kindly.  ');
  const ready = q('.talk-save').disabled;
  q('.talk-save').click();
  await wait(80);
  const after = {source: q('.adm-n .tag').textContent, cap: q('.talk-cap').value, msg: q('.adm-msg').textContent, reset: !!q('.talk-reset')};
  const histRows = [...doc.querySelectorAll('#changes .adm-hist tbody tr')].map((tr) => [tr.children[2].textContent, !!tr.querySelector('.adm-revert')]);
  q('.talk-reset').click();
  const asked = q('.talk-reset').textContent;
  q('.talk-reset').click();
  await wait(80);
  const back = {source: q('.adm-n .tag').textContent, rules: q('.talk-rules').value, reset: !!q('.talk-reset'), msg: q('.adm-msg').textContent};

  //then
  assert.deepStrictEqual(first, {title: 'Small talk', on: 'on', cap: '10', rules: DEFAULT_RULES, source: 'default', used: '2 of 10 replies used this hour', saveOff: true, reset: false});
  assert.deepStrictEqual(tooMany, [true, 'Rules need 1 to 2000 characters and replies per hour 0 to 60']);
  assert.strictEqual(ready, false, 'a valid change turns Save on');
  assert.deepStrictEqual(calls[1], ['PUT', {enabled: true, hourlyCap: 4, rules: 'Answer briefly and kindly.'}], 'Save sends the trimmed rules');
  assert.ok(/^Set by nina\.white/.test(after.source) && after.cap === '4' && after.msg === 'Saved' && after.reset, 'the row says who set it and offers Back to default');
  assert.deepStrictEqual(histRows, [['Small talk replies per hour', false], ['Small talk rules', false]], 'Changes names the change and offers no Revert for it');
  assert.strictEqual(asked, 'Back to default?', 'Back to default asks first');
  assert.deepStrictEqual(calls[calls.length - 1], ['DELETE', null]);
  assert.deepStrictEqual(back, {source: 'default', rules: DEFAULT_RULES, reset: false, msg: 'Back to the default'});
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'Admin \u00b7 General has a Small talk group: on / off, replies per hour and the rules, Save only for a valid change, who set it, history rows without Revert, and a two-step Back to default';
}

(async () => {
  console.log('PASS ' + await anAdminEditsTheSmallTalkRulesOnTheAdminPage());
})().catch((e) => { console.error('FAIL', e); process.exit(1); });
