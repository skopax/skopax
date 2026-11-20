const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const NINA = 'nina.white@example.com', ALICE = 'alice@example.com', BOB = 'bob@example.com';
const WARNING = 'Slack app is missing users:read.email; responsibles are named without a ping';
const TAGS = [{id: 'developer', label: 'Developer'}, {id: 'data', label: 'Data'}, {id: 'seo', label: 'SEO'}];

function board(cards) {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: cards || [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

function rulesItem(tag, label) {
  return {key: 'tags.' + tag + '.rules', group: 'Card tags', card: 'Tag rules', order: 9000, label: label, help: 'Cards tagged ' + label + ' by rule', kind: 'tagrules', live: true, value: '', base: '', default: '',
    source: 'default', by: null, at: null, limit: 500, pluginTerms: []};
}

async function boot(opts) {
  opts = opts || {};
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(board(opts.cards)));
  const owners = {developer: [{email: ALICE, name: 'Alice Moss'}], data: [], seo: [{email: 'gone@example.com', name: 'gone', stale: true}]};
  const hist = [];
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/' + (opts.hash || '#t=settings'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, o) => {
        const p = String(path), method = (o && o.method) || 'GET';
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        const m = /^\/api\/admin\/tags\/([a-z-]+)\/responsibles$/.exec(p);
        if (m && method === 'PUT') {
          const body = JSON.parse(o.body);
          calls.push([method, p, body]);
          const named = {[ALICE]: 'Alice Moss', [BOB]: 'Bob Reed', [NINA]: 'Nina White'};
          hist.unshift({id: 'h' + (hist.length + 1), key: 'tags.' + m[1] + '.responsibles', old: owners[m[1]].map((x) => x.email).join(', ') || null, new: body.emails.join(', ') || null,
            by: NINA, at: iso(0), owners: true, tag: m[1]});
          owners[m[1]] = body.emails.map((e) => Object.assign({email: e, name: named[e] || e.split('@')[0]}, named[e] ? {} : {slack: 'unknown'}));
          return ok({id: m[1], label: TAGS.filter((t) => t.id === m[1])[0].label, responsibles: owners[m[1]]});
        }
        if (p === '/api/admin/tags') return ok({tags: TAGS.map((t) => Object.assign({}, t, {responsibles: owners[t.id]})), canEdit: opts.canEdit !== false,
          users: opts.canEdit === false ? [] : [{email: ALICE, name: 'Alice Moss', disabled: false}, {email: BOB, name: 'Bob Reed', disabled: false}, {email: 'gone@example.com', name: 'Gone', disabled: true}],
          reportAt: '10:00', emailScopeMissing: !!opts.scope});
        if (p.startsWith('/api/admin/history')) return ok(hist);
        if (p.startsWith('/api/admin/teams')) return ok({routes: [], tags: [], hosts: []});
        if (p.startsWith('/api/admin/settings')) return ok({items: [{key: 'tags.report-at', group: 'Card tags', card: 'Daily report', order: 0, label: 'Daily report at', help: 'Time of day of the report; Monday to Friday only', kind: 'text', live: true,
          value: '10:00', base: '10:00', default: '10:00', source: 'default', by: null, at: null, limit: 500, problem: opts.scope ? WARNING : undefined}].concat(TAGS.map((t) => rulesItem(t.id, t.label))), rev: 1, refreshSeconds: 15});
        if (p.startsWith('/api/admin/notices')) return ok({items: opts.scope ? [{id: 'tag-report-scope', level: 'warn', text: WARNING}] : []});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: NINA, operator: true, admin: true, teams: [], tags: TAGS});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(450);
  return {w: dom.window, doc: dom.window.document, errors, calls};
}

async function anAdminAddsAndRemovesResponsiblesOnTheCardTagsRows() {
  //given
  const {w, doc, errors, calls} = await boot();
  const box = (tag) => doc.querySelector('#admin .adm-row[data-key="tags.' + tag + '.rules"] .adm-owners');
  const chips = (tag) => [...box(tag).querySelectorAll('.adm-owner .adm-owner-n')].map((n) => n.textContent);
  const first = {dev: chips('developer'), data: box('data').querySelector('.adm-owner-none').textContent, label: box('developer').querySelector('.adm-owners-l').textContent,
    offered: [...box('developer').querySelectorAll('datalist option')].map((o) => o.value), labels: [...box('developer').querySelectorAll('datalist option')].map((o) => o.textContent),
    kind: box('developer').querySelector('.adm-owner-pick').tagName + '/' + box('developer').querySelector('.adm-owner-pick').getAttribute('aria-label'), linked: box('developer').querySelector('.adm-owner-pick').getAttribute('list') === box('developer').querySelector('datalist').id,
    addOff: box('developer').querySelector('.adm-owner-go').disabled};

  //when
  const pick = box('developer').querySelector('.adm-owner-pick');
  pick.value = BOB;
  pick.dispatchEvent(new w.Event('input'));
  const addOn = !box('developer').querySelector('.adm-owner-go').disabled;
  box('developer').querySelector('.adm-owner-go').click();
  await wait(80);
  const added = {chips: chips('developer'), said: box('developer').querySelector('.adm-owner-msg').textContent};
  box('developer').querySelector('.adm-owner[data-email="' + ALICE + '"] .adm-owner-x').click();
  await wait(80);
  const removed = chips('developer');
  const histRows = [...doc.querySelectorAll('#changes .adm-hist tbody tr')].map((tr) => [tr.children[2].textContent, !!tr.querySelector('.adm-revert')]);

  //then
  assert.deepStrictEqual(first, {dev: ['Alice Moss'], data: 'No one yet', label: 'Responsibles', offered: [BOB], labels: ['Bob Reed \u00b7 ' + BOB], kind: 'INPUT/Add a responsible', linked: true, addOff: true},
    'the chips show, disabled people and present ones are not suggested');
  assert.strictEqual(addOn, true, 'typing enables Add');
  assert.deepStrictEqual(calls.map((c) => [c[1], c[2].emails]), [['/api/admin/tags/developer/responsibles', [ALICE, BOB]], ['/api/admin/tags/developer/responsibles', [BOB]]]);
  assert.deepStrictEqual(added, {chips: ['Alice Moss', 'Bob Reed'], said: 'Added'});
  assert.deepStrictEqual(removed, ['Bob Reed']);
  assert.deepStrictEqual(histRows, [['Responsibles of Developer', false], ['Responsibles of Developer', false]], 'Changes names the tag and offers no Revert');
  assert.strictEqual(doc.querySelector('#admin .adm-row[data-key="tags.developer.rules"] .adm-save').disabled, true, 'the rules Save stays off');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'Admin \u00b7 Card tags: each tag row lists its responsibles as chips, adds a person picked from the users and removes one with the cross, and each change is a Changes row without Revert';
}

async function anyAddressTypedAndEnteredIsAddedAndAnUnknownOneShowsTheSlackWarning() {
  //given
  const {w, doc, errors, calls} = await boot();
  const box = () => doc.querySelector('#admin .adm-row[data-key="tags.data.rules"] .adm-owners');
  const pick = box().querySelector('.adm-owner-pick');

  //when
  pick.value = '  Carol.Outside@Example.com ';
  pick.dispatchEvent(new w.Event('input'));
  pick.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
  await wait(80);

  //then
  assert.deepStrictEqual(calls.map((c) => c[2].emails), [['carol.outside@example.com']]);
  assert.deepStrictEqual([...box().querySelectorAll('.adm-owner-n')].map((n) => n.textContent), ['carol.outside']);
  assert.deepStrictEqual([...box().querySelectorAll('.adm-owner-warn')].map((n) => n.textContent), ['Not found in Slack: shown as plain text in the report']);
  assert.strictEqual(box().querySelector('.adm-owner-msg').textContent, 'Added');
  assert.strictEqual(doc.activeElement, box().querySelector('.adm-owner-pick'), 'focus returns to the input');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'Enter in the responsible field adds any typed e-mail, lower-cased, and a person Slack does not know carries the plain-text warning';
}

async function aReadOnlyListShowsChipsWithoutControlsAndTheScopeWarningShows() {
  //given
  const {w, doc, errors} = await boot({canEdit: false, scope: true});
  const box = doc.querySelector('#admin .adm-row[data-key="tags.developer.rules"] .adm-owners');
  const report = doc.querySelector('#admin .adm-row[data-key="tags.report-at"]');

  //then
  assert.deepStrictEqual([...box.querySelectorAll('.adm-owner-n')].map((n) => n.textContent), ['Alice Moss']);
  assert.strictEqual(box.querySelectorAll('button, select, input').length, 0, 'no add or remove control without edit rights');
  const stale = doc.querySelector('#admin .adm-row[data-key="tags.seo.rules"] .adm-owner');
  assert.ok(stale.classList.contains('adm-owner-stale') && stale.title === 'No longer an active user', 'a removed or disabled person is struck through');
  assert.strictEqual(report.querySelector('.adm-problem').textContent, WARNING, 'the warning sits next to the report setting');
  assert.ok([...doc.querySelectorAll('.adm-notice-t')].some((n) => n.textContent === WARNING), 'the notices strip carries it too');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'without edit rights the responsibles are chips only, and a missing users:read.email scope shows next to Daily report at and in the notices strip';
}

function card(key, sev, tag) {
  return {key: key, sev: sev, status: 'open', svc: key.split('|')[1], label: 'x', src: 'logs', sig: 'log', now: 1, firstSeen: iso(30), lastSeen: iso(1), tags: tag ? [{id: tag, by: 'rule', at: iso(20)}] : []};
}

async function aSlackLinkOpensTheBoardOnOpenFilteredToTheTagWithARemovableChip() {
  //given
  const cards = [card('log|checkout-service|ERROR|a', 'crit', 'developer'), card('log|shop-web|ERROR|b', 'warn', 'data'), card('log|payments-api|ERROR|c', 'warn', null)];
  const {w, doc, errors} = await boot({cards: cards, hash: '#t=anoms/open/tag:developer'});
  const shown = () => [...doc.querySelectorAll('#anoms article.inc')].map((a) => a.dataset.key);
  const chip = () => doc.querySelector('#card-qf [data-type="tag:developer"]');
  const first = {shown: shown(), pressed: chip().getAttribute('aria-pressed'), note: doc.querySelector('#filter-note .fn-cur').textContent, hash: decodeURIComponent(w.location.hash)};

  //when
  doc.getElementById('clear-type').click();
  await wait(60);
  const cleared = {shown: shown().length, hash: decodeURIComponent(w.location.hash)};
  chip().click();
  await wait(60);
  const again = {shown: shown(), pressed: chip().getAttribute('aria-pressed')};
  chip().click();
  await wait(60);
  const off = {shown: shown().length, pressed: chip().getAttribute('aria-pressed')};

  //then
  assert.deepStrictEqual(first.shown, ['log|checkout-service|ERROR|a']);
  assert.strictEqual(first.pressed, 'true');
  assert.ok(first.note.startsWith('Showing Open \u00b7 Developer'), first.note);
  assert.strictEqual(first.hash, '#t=anoms/open/tag:developer');
  assert.deepStrictEqual(cleared, {shown: 3, hash: '#t=anoms'}, 'Every type drops the tag filter');
  assert.deepStrictEqual(again, {shown: ['log|checkout-service|ERROR|a'], pressed: 'true'});
  assert.deepStrictEqual(off, {shown: 3, pressed: 'false'}, 'clicking the pressed chip drops the filter');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a fresh load of #t=anoms/open/tag:developer, the link the Slack tag report uses, opens Open filtered to Developer with a pressed chip, and Every type or the chip removes it';
}

(async () => {
  console.log('PASS ' + await anAdminAddsAndRemovesResponsiblesOnTheCardTagsRows());
  console.log('PASS ' + await anyAddressTypedAndEnteredIsAddedAndAnUnknownOneShowsTheSlackWarning());
  console.log('PASS ' + await aReadOnlyListShowsChipsWithoutControlsAndTheScopeWarningShows());
  console.log('PASS ' + await aSlackLinkOpensTheBoardOnOpenFilteredToTheTagWithARemovableChip());
})().catch((e) => { console.error('FAIL', e); process.exit(1); });
