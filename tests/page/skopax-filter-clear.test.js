const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const WARN = 'log|checkout-service|ERROR|boom';
const CRIT = 'log|shop-front|ERROR|down';

function state() {
  const card = (key, extra) => Object.assign({key, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'x', src: 'logs', sig: 'log', now: 1, firstSeen: iso(30), lastSeen: iso(1)}, extra || {});
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(WARN, {ns: 'shop', label: 'ERROR lines'}), card(CRIT, {ns: 'shop', label: 'ERROR lines', sev: 'crit'})],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot() {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state())), {
    url: 'https://skopax.example.com/#t=anoms', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'nina.white@example.com', operator: true, teams: [], tags: []});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors};
}

const pressed = (doc) => [...doc.querySelectorAll('#panel-anoms .qf-c[aria-pressed="true"]')].map((b) => b.dataset.f || b.dataset.type);
const chip = (doc, f) => doc.querySelector('#panel-anoms .qf-c[data-f="' + f + '"]');
const who = (doc) => doc.querySelector('#panel-anoms .qf-c[data-type="who:none"]');
const note = (doc) => doc.getElementById('filter-note');

async function turnOn(t, search) {
  if (search) {
    const box = t.doc.getElementById('card-search');
    box.value = search;
    box.dispatchEvent(new t.w.Event('input'));
    await wait(30);
  }
  chip(t.doc, 'warn').click();
  who(t.doc).click();
  await wait(30);
}

(async () => {
  //given
  const a = await boot();
  assert.ok(!a.doc.getElementById('clear-all') && !a.doc.getElementById('all-f'), 'nothing to clear on the default board');

  //when
  await turnOn(a, 'checkout');

  //then
  assert.ok(a.doc.getElementById('clear-all') && a.doc.getElementById('all-f'), 'the line shows All and Clear filters');
  assert.ok(a.doc.getElementById('clear-f'), 'Back to Open stays while it still keeps the chip and the search');

  //when
  a.doc.getElementById('clear-all').click();
  await wait(30);

  //then
  assert.deepStrictEqual(pressed(a.doc).filter((x) => x === 'open' || x === 'warn' || x === 'who:none'), ['open'], 'Clear filters goes back to Open with no chip');
  assert.strictEqual(a.doc.getElementById('card-search').value, '', 'and empties the search');
  assert.strictEqual(a.w.location.hash, '#t=anoms', 'and the address is the plain board');
  assert.ok(!a.doc.getElementById('clear-all'), 'nothing left to clear');
  console.log('PASS Clear filters resets the filter, the chip and the search');

  //given
  const b = await boot();
  await turnOn(b);

  //when
  b.doc.getElementById('all-f').click();
  await wait(30);

  //then
  assert.ok(pressed(b.doc).includes('all') && pressed(b.doc).includes('who:none'), 'and keeps the chip');
  assert.ok(!b.doc.getElementById('all-f'), 'All is not offered on All');
  console.log('PASS All switches to all and keeps the chip');

  //given
  const c = await boot();
  await turnOn(c, 'checkout');

  //when
  c.doc.getElementById('card-search').focus();
  c.doc.getElementById('card-search').dispatchEvent(new c.w.KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  await wait(30);

  //then
  assert.strictEqual(c.doc.getElementById('card-search').value, '', 'Esc in the search box still empties just the box');
  assert.ok(pressed(c.doc).includes('who:none'), 'and keeps the chip');

  //given
  const d = await boot();
  await turnOn(d);

  //when
  d.doc.body.dispatchEvent(new d.w.KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  await wait(30);

  //then
  assert.ok(!pressed(d.doc).includes('who:none') && pressed(d.doc).includes('open') && !pressed(d.doc).includes('warn'), 'Esc on the board clears every filter');
  assert.ok(!d.doc.getElementById('clear-all'));
  const stays = async (setup, why) => {
    const t = await boot();
    await turnOn(t);
    const target = setup(t) || t.doc.body;
    target.dispatchEvent(new t.w.KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
    await wait(30);
    assert.ok(pressed(t.doc).includes('who:none') && pressed(t.doc).includes('warn'), why);
    t.w.close();
  };
  await stays((t) => { t.doc.getElementById('cf-wrap').hidden = false; }, 'Esc with a visible dialog keeps the filters');
  await stays((t) => { t.w.eval("openDrawer('" + WARN + "')"); assert.ok(t.w.eval('DRAWER'), 'the card drawer is open'); }, 'Esc with the card drawer open keeps the filters');
  await stays((t) => { t.doc.body.classList.add('drawer-open'); }, 'Esc with the phone sidebar drawer open keeps the filters');
  await stays((t) => { const input = t.doc.createElement('input'); t.doc.body.appendChild(input); input.focus(); return input; }, 'Esc while typing in another input keeps the filters');
  assert.deepStrictEqual([a.errors, b.errors, c.errors, d.errors], [[], [], [], []]);
  console.log('PASS Esc on the board clears the filters and Esc in the search box keeps its own behaviour');
  process.exit(0);
})().catch((e) => { console.error('FAIL ' + (e && e.stack || e)); process.exit(1); });
