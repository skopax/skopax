const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const NUM_KEY = 'log|orders-service|ERROR|boom';
const BARE_KEY = 'log|checkout-service|ERROR|slow (x)';

function state() {
  const card = (key, svc, num) => Object.assign({key, sev: 'crit', status: 'open', svc, label: 'ERROR lines', src: 'logs', sig: 'log', now: 1, firstSeen: iso(30), lastSeen: iso(1)}, num ? {num} : {});
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(NUM_KEY, 'orders-service', 7), card(BARE_KEY, 'checkout-service')], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(view, hash) {
  const errors = [];
  const vc = new VirtualConsole(); vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE, {view}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state())), {
    url: 'https://skopax.example.com/' + hash, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {}; w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        p = String(p);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', operator: true, slack: true, teams: [], tags: [], features: {}});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(350);
  const w = dom.window, doc = w.document;
  return {
    w, doc, errors, dom,
    hash: () => w.location.hash,
    len: () => w.history.length,
    tile: (key) => [].filter.call(doc.querySelectorAll('#anoms .tile'), (t) => t.dataset.key === key)[0],
    drawer: () => doc.querySelector('#anoms .card-drawer'),
    card: (key) => [].filter.call(doc.querySelectorAll('#anoms article.inc'), (c) => c.dataset.key === key && !c.closest('.card-drawer'))[0],
  };
}
const encoded = (key) => encodeURIComponent(key).replace(/[()!'*]/g, (c) => '%' + c.charCodeAt(0).toString(16).toUpperCase());

async function check(name, fn) {
  await fn();
  console.log('PASS ' + name);
}

(async () => {
  await check('a board tile click puts #c=<num> in the address bar as a new history entry, a second click on it adds none, Close returns to the tab path', async () => {
    //given
    const p = await boot('board', '#t=anoms/all');
    const before = p.len();

    //when
    p.tile(NUM_KEY).click();
    const opened = {hash: p.hash(), len: p.len(), drawer: p.drawer() && p.drawer().dataset.key};
    p.tile(NUM_KEY).click();
    const again = p.len();
    p.drawer().querySelector('.cd-close').click();

    //then
    assert.deepStrictEqual(opened, {hash: '#c=7', len: before + 1, drawer: NUM_KEY});
    assert.strictEqual(again, before + 1);
    assert.strictEqual(p.drawer(), null);
    assert.strictEqual(p.hash(), '#t=anoms/all');
    assert.strictEqual(p.len(), before + 1);
    assert.deepStrictEqual(p.errors, []);
    p.dom.window.close();
  });

  await check('Back after opening a board card closes the drawer and shows the tab path, Forward opens it again', async () => {
    //given
    const p = await boot('board', '#t=anoms/all');
    p.tile(NUM_KEY).click();

    //when
    p.w.history.back();
    await wait(80);
    const back = {hash: p.hash(), drawer: p.drawer()};
    p.w.history.forward();
    await wait(80);

    //then
    assert.deepStrictEqual(back, {hash: '#t=anoms/all', drawer: null});
    assert.strictEqual(p.hash(), '#c=7');
    assert.strictEqual(p.drawer() && p.drawer().dataset.key, NUM_KEY);
    assert.deepStrictEqual(p.errors, []);
    p.dom.window.close();
  });

  await check('Back also closes the drawer when the page was opened with no hash at all', async () => {
    //given
    const p = await boot('board', '');
    p.tile(NUM_KEY).click();

    //when
    p.w.history.back();
    await wait(80);

    //then
    assert.strictEqual(p.drawer(), null);
    assert.ok(!/^#c=/.test(p.hash()));
    p.dom.window.close();
  });

  await check('opening another card pushes again so Back returns to the previous card', async () => {
    //given
    const p = await boot('board', '#t=anoms/all');
    p.tile(NUM_KEY).click();
    p.tile(BARE_KEY).click();

    //when
    p.w.history.back();
    await wait(80);

    //then
    assert.strictEqual(p.hash(), '#c=7');
    assert.strictEqual(p.drawer().dataset.key, NUM_KEY);
    p.dom.window.close();
  });

  await check('a card with no number uses #a=<encoded key>', async () => {
    //given
    const p = await boot('board', '#t=anoms/all');

    //when
    p.tile(BARE_KEY).click();

    //then
    assert.strictEqual(p.hash(), '#a=' + encoded(BARE_KEY));
    p.dom.window.close();
  });

  await check('re-renders keep the card hash while the card is open, switching tab clears it', async () => {
    //given
    const p = await boot('board', '#t=anoms/all');
    p.tile(NUM_KEY).click();
    const len = p.len();

    //when
    p.w.setFilter('all');
    const kept = {hash: p.hash(), len: p.len(), drawer: !!p.drawer()};
    p.w.showTab('svcs');

    //then
    assert.deepStrictEqual(kept, {hash: '#c=7', len, drawer: true});
    assert.strictEqual(p.hash(), '#t=svcs');
    p.dom.window.close();
  });

  await check('expanding a list card pushes #c=<num>, collapsing it returns to the tab path, Back after expanding collapses it', async () => {
    //given
    const p = await boot('list', '#t=anoms/all');
    const before = p.len();
    const fold = () => p.card(NUM_KEY).querySelector('.inc-title .fold');

    //when
    fold().click();
    const opened = {hash: p.hash(), len: p.len(), open: p.card(NUM_KEY).classList.contains('open')};
    fold().click();
    const closed = {hash: p.hash(), open: p.card(NUM_KEY).classList.contains('open')};
    fold().click();
    p.w.history.back();
    await wait(80);

    //then
    assert.deepStrictEqual(opened, {hash: '#c=7', len: before + 1, open: true});
    assert.deepStrictEqual(closed, {hash: '#t=anoms/all', open: false});
    assert.strictEqual(p.hash(), '#t=anoms/all');
    assert.strictEqual(p.card(NUM_KEY).classList.contains('open'), false);
    assert.deepStrictEqual(p.errors, []);
    p.dom.window.close();
  });

  await check('a #c=<num> link opens that card and keeps the hash, in the board and the list', async () => {
    //given
    const board = await boot('board', '#c=7');
    const list = await boot('list', '#c=7');

    //then
    assert.strictEqual(board.drawer() && board.drawer().dataset.key, NUM_KEY);
    assert.strictEqual(board.hash(), '#c=7');
    assert.ok(list.card(NUM_KEY).classList.contains('open'));
    assert.strictEqual(list.hash(), '#c=7');
    assert.deepStrictEqual(board.errors.concat(list.errors), []);
    board.dom.window.close();
    list.dom.window.close();
  });

  await check('an #a=<key> link still opens the card and is normalised to #c=<num>, a card with no number keeps #a=', async () => {
    //given
    const num = await boot('board', '#a=' + encoded(NUM_KEY));
    const bare = await boot('board', '#a=' + encoded(BARE_KEY));

    //then
    assert.strictEqual(num.drawer().dataset.key, NUM_KEY);
    assert.strictEqual(num.hash(), '#c=7');
    assert.strictEqual(bare.drawer().dataset.key, BARE_KEY);
    assert.strictEqual(bare.hash(), '#a=' + encoded(BARE_KEY));
    num.dom.window.close();
    bare.dom.window.close();
  });

  await check('an unknown #c= number shows the note and falls back to the tab path', async () => {
    //given
    const p = await boot('board', '#c=99');

    //then
    assert.ok(p.doc.getElementById('link-note'));
    assert.strictEqual(p.drawer(), null);
    assert.strictEqual(p.hash(), '#t=anoms');
    p.dom.window.close();
  });

  await check('a click on a link to the card already in the address bar shows that card again, in the list and the board', async () => {
    //given
    const list = await boot('list', '#t=anoms/all');
    const board = await boot('board', '#t=anoms/all');
    list.card(BARE_KEY).querySelector('.inc-title .fold').click();
    board.tile(NUM_KEY).click();
    const link = (p, href) => { const a = p.doc.createElement('a'); a.href = href; a.textContent = 'card'; p.doc.body.appendChild(a); return a; };
    const shown = [];
    list.w.Element.prototype.scrollIntoView = function () { shown.push(this.dataset.key); };
    board.drawer().remove();
    const lenList = list.len(), lenBoard = board.len();

    //when
    link(list, '#a=' + encoded(BARE_KEY)).click();
    link(board, '#c=7').click();
    await wait(80);

    //then
    assert.ok(list.card(BARE_KEY).classList.contains('linked'), 'the list card is ringed again');
    assert.deepStrictEqual(shown, [BARE_KEY], 'the list scrolls to the card');
    assert.strictEqual(list.hash(), '#a=' + encoded(BARE_KEY));
    assert.strictEqual(board.drawer() && board.drawer().dataset.key, NUM_KEY, 'the board opens the drawer again');
    assert.strictEqual(board.hash(), '#c=7');
    assert.deepStrictEqual([list.len(), board.len()], [lenList, lenBoard], 'no history entry is added');
    assert.deepStrictEqual(list.errors.concat(board.errors), []);
    list.dom.window.close();
    board.dom.window.close();
  });
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
