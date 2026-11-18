const pageHtml = require('./skopax-page');
const assert = require('assert');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const CARD_PAGE = path.join(path.dirname(TEMPLATE), 'card.html');
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const AT = '2026-11-09T16:32:00Z';
const {short} = require('./skopax-zone');

async function boot(card, who) {
  const errors = [], puts = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(CARD_PAGE), {
    url: 'https://skopax.example.com/c/' + card.num, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.fetch = async (p, opts) => {
        const url = String(p);
        if (url === '/api/me') return {ok: true, status: 200, json: async () => Object.assign({loginEnabled: true, user: 'alice@example.com', operator: true, jira: true}, who || {})};
        if (url.startsWith('/api/people')) return {ok: true, status: 200, json: async () => [{name: 'Carol Diaz', email: 'carol@example.com'}]};
        if (url === '/api/assignee' && opts && opts.method === 'PUT') {
          const body = JSON.parse(opts.body);
          puts.push(body);
          if (body.name === 'Nobody') return {ok: false, status: 422, json: async () => ({detail: 'no such person in Jira'})};
          card.owner = body.me ? 'alice@example.com' : body.name || undefined;
          card.assigned = card.owner ? {name: card.owner, by: 'alice@example.com', at: AT} : undefined;
          return {ok: true, status: 200, json: async () => ({})};
        }
        return {ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(card))};
      };
    },
  });
  await wait(150);
  return {w: dom.window, doc: dom.window.document, errors, puts};
}

const typeIn = (w, inp, value) => { inp.value = value; inp.dispatchEvent(new w.Event('input', {bubbles: true})); };

async function anOperatorAssignsTheCardFromItsOwnPage() {
  //given
  const card = {num: 153, key: 'alert|ContainerOOMKilled|ns=analytics', svc: 'kube-state-metrics', label: 'ContainerOOMKilled', sev: 'crit', status: 'open', firstSeen: AT, live: true};
  const {w, doc, errors, puts} = await boot(card);

  //when
  const before = {owner: doc.querySelector('.owner'), label: doc.getElementById('act-assign').textContent, hidden: doc.getElementById('form-assign').hidden};
  doc.getElementById('act-assign').click();
  const shown = !doc.getElementById('form-assign').hidden;
  typeIn(w, doc.getElementById('assign-who'), 'Nobody');
  doc.getElementById('assign-go').click();
  await wait(100);
  const refused = doc.getElementById('assign-msg').textContent;
  typeIn(w, doc.getElementById('assign-who'), 'Carol Diaz');
  doc.getElementById('assign-go').click();
  await wait(150);
  const after = {owner: doc.querySelector('.owner').textContent, tip: doc.querySelector('.owner').title, label: doc.getElementById('act-assign').textContent};
  doc.getElementById('act-assign').click();
  doc.getElementById('assign-me').click();
  await wait(150);
  const meNow = {owner: doc.querySelector('.owner').textContent, noMe: !doc.getElementById('assign-me')};
  doc.getElementById('act-assign').click();
  doc.getElementById('assign-clear').click();
  await wait(150);

  //then
  assert.deepStrictEqual(before, {owner: null, label: 'Assign', hidden: true});
  assert.ok(shown, 'Assign opens the form');
  assert.strictEqual(refused, 'Could not assign: no such person in Jira', 'a refusal says why');
  assert.deepStrictEqual(after, {owner: 'Assignee: Carol Diaz', tip: 'Assigned by: alice \u00b7 ' + short(AT), label: 'Change assignee'});
  assert.deepStrictEqual(meNow, {owner: 'Assignee: alice', noMe: true}, 'Assign to me assigns me and then hides itself');
  assert.deepStrictEqual(puts.map((p) => [p.key, p.name, p.me || false]), [[card.key, 'Nobody', false], [card.key, 'Carol Diaz', false], [card.key, undefined, true], [card.key, '', false]]);
  assert.strictEqual(doc.querySelector('.owner'), null, 'Remove assignee clears it');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'an operator assigns the card on its own page (a person, themselves, or nobody), sees who it is assigned to, and a refusal says why';
}

async function aViewerAndAGuestOnlySeeTheAssignee() {
  //given
  const card = {num: 154, key: 'k', svc: 'checkout-service', label: 'ERROR pattern', sev: 'warn', status: 'open', firstSeen: AT, live: true, owner: 'Carol Diaz'};

  //when
  const viewer = await boot(Object.assign({}, card), {operator: false});
  const guest = await boot(Object.assign({}, card), {operator: false, user: null});

  //then
  for (const one of [viewer, guest]) {
    assert.strictEqual(one.doc.querySelector('.owner').textContent, 'Assignee: Carol Diaz');
    assert.strictEqual(one.doc.getElementById('act-assign'), null, 'no Assign button without the operator role');
    assert.strictEqual(one.doc.getElementById('act-take'), null, 'no other action either');
  }
  assert.deepStrictEqual(viewer.puts.concat(guest.puts), []);
  assert.deepStrictEqual(viewer.errors.concat(guest.errors), []);
  viewer.w.close(); guest.w.close();
  return 'a viewer is read-only like a guest on the card page: both see the assignee and have no Assign button';
}

(async () => {
  console.log('PASS ' + await anOperatorAssignsTheCardFromItsOwnPage());
  console.log('PASS ' + await aViewerAndAGuestOnlySeeTheAssignee());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
