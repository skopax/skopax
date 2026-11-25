const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const TYPES = [{sig: 'x-send', label: 'X sends', alias: ''}, {sig: 'x-access', label: 'No access', alias: 'no-access'}];

function tile(types) {
  return {id: 'shop', name: 'Shop', icon: '/plugins/shop/icon.svg', tab: null, passes: [], instances: [], pages: [], queued: false, types};
}

function card(sig, num) {
  return {key: sig + '|-/shop|part-' + num, src: 'shop', sig, ns: '-', svc: 'shop', sub: 'part-' + num, label: sig, sev: 'warn', status: 'open', now: 1, num,
    firstSeen: iso(60), lastSeen: iso(1), what: 'no sends'};
}

function state(extra) {
  return Object.assign({rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {}, summary: {level: 'warn', headline: 'Shop', points: [], at: iso(1)},
    anomalies: [card('x-send', 1), card('x-access', 2), card('no-access', 3)], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []}, extra || {});
}

async function boot(opts) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state(opts.inline ? {plugins: [tile(opts.inline)]} : {})));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/#t=anoms/all', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: null, operator: false});
        if (p.startsWith('/api/server') && opts.served) return ok({plugins: [tile(opts.served)], tz: 'UTC'});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/investigations')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(300);
  const chips = () => [...dom.window.document.querySelectorAll('#bf-lists .fb-i[data-type]')];
  return {w: dom.window, doc: dom.window.document, errors, chips, chip: (t) => chips().filter((b) => b.dataset.type === t)[0]};
}

async function servedTypesNameTheChipsAndAnAliasFoldsUnderItsTarget() {
  //given
  const t = await boot({served: TYPES});

  //when
  const types = t.chips().map((b) => b.dataset.type);
  const sends = t.chip('x-send'), access = t.chip('no-access');

  //then
  assert.ok(sends && sends.textContent.includes('X sends'), 'the plug-in sig shows its label: ' + (sends && sends.textContent));
  assert.ok(access && access.textContent.includes('No access') && access.textContent.includes('2'), 'the alias counts under No access: ' + (access && access.textContent));
  assert.ok(types.indexOf('x-access') < 0, 'the aliased sig has no chip of its own: ' + types.join(', '));
  assert.strictEqual(t.w.typeName('x-access'), 'No access');
  assert.deepStrictEqual([...t.w.ALL].map((a) => a.sig).sort(), ['no-access', 'x-access', 'x-send'], 'the cards keep their own sig');
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS a plug-in sig x-send shows label X sends, and an alias x-access shows under No access');
}

async function typesInTheFirstRenderNameTheChipsBeforeTheServerAnswers() {
  //given
  const t = await boot({inline: TYPES});

  //when
  const sends = t.chip('x-send');

  //then
  assert.ok(sends && sends.textContent.includes('X sends'), 'the first paint already names the type');
  assert.ok(!t.chip('x-access'));
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS types in the first render name the chips before the server answers');
}

async function aServedTypeNeverRenamesACoreTypeAndARemovedOneDrops() {
  //given
  const t = await boot({served: TYPES.concat([{sig: 'no-access', label: 'Hijacked', alias: ''}, {sig: 'log', label: 'Shop logs', alias: 'x-send'}])});

  //when
  const before = [t.w.typeName('no-access'), t.w.typeName('log'), t.w.typeName('x-send')];
  t.w.pluginTilesChanged([tile([])]);
  await wait(50);
  const after = [t.w.typeName('x-send'), t.w.typeName('x-access'), t.w.typeName('no-access')];

  //then
  assert.deepStrictEqual(before, ['No access', 'Log errors', 'X sends']);
  assert.deepStrictEqual(after, ['x send', 'x access', 'No access']);
  assert.ok(t.chip('x-send').textContent.includes('x send') && t.chip('x-access'), 'the board repaints when the served types change: ' + t.chips().map((b) => b.dataset.type).join(', '));
  assert.deepStrictEqual(t.errors, []);
  t.w.close();
  console.log('PASS a served type never renames a core type, and a removed plug-in type drops from the board at once');
}

(async () => {
  await servedTypesNameTheChipsAndAnAliasFoldsUnderItsTarget();
  await typesInTheFirstRenderNameTheChipsBeforeTheServerAnswers();
  await aServedTypeNeverRenamesACoreTypeAndARemovedOneDrops();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
