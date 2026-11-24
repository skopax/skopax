const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const KEY = 'log|checkout-service|ERROR|boom';

function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [{key: KEY, num: 14, sev: 'warn', status: 'open', svc: 'checkout-service', label: 'ERROR lines', src: 'logs', sig: 'log', now: 1, firstSeen: iso(30), lastSeen: iso(5), what: 'text'}],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(answer) {
  const errors = [], posts = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(pageHtml(TEMPLATE, {view: 'board'}).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state())), {
    url: 'https://skopax.example.com/#c=14', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, init) => {
        const p = String(path);
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', operator: true, teams: [], tags: []});
        if (p.startsWith('/api/actions') && init && init.method === 'POST') {
          posts.push(JSON.parse(init.body));
          return {ok: false, status: answer, json: async () => ({detail: answer === 500 ? null : 'no'})};
        }
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/state')) return ok(state());
        return {ok: false, status: 404, json: async () => ({})};
      };
    }});
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors, posts};
}

const mute = (doc) => [...doc.querySelectorAll('.card-drawer .acts button')].find((b) => b.textContent.trim() === 'Mute 24 h' || b.getAttribute('aria-label') === 'Mute for 24 hours');
const toasts = (doc) => [...doc.querySelectorAll('#fail-toasts .toast-t')].map((t) => t.textContent);

async function anActionAfterTheSessionEndedSaysSignInAndLocksTheButtons() {
  //given
  const {w, doc, errors, posts} = await boot(401);

  //when
  mute(doc).click();
  await wait(60);

  //then
  assert.strictEqual(posts.length, 1);
  assert.deepStrictEqual(toasts(doc), ['Not done: you are signed out; sign in and try again.']);
  assert.strictEqual(mute(doc).getAttribute('aria-disabled'), 'true', 'the panel buttons lock with the reason');
  assert.deepStrictEqual(errors, []);
  w.close();
}

async function anActionWithoutTheRoleAnymoreSaysSo() {
  //given
  const {w, doc, errors} = await boot(403);

  //when
  mute(doc).click();
  await wait(60);

  //then
  assert.deepStrictEqual(toasts(doc), ['Not done: you are not allowed to do this.']);
  assert.strictEqual(mute(doc).getAttribute('aria-disabled'), 'true');
  assert.deepStrictEqual(errors, []);
  w.close();
}

async function aFailingActionKeepsItsButtonAndSaysTryAgain() {
  //given
  const {w, doc, errors} = await boot(500);

  //when
  mute(doc).click();
  await wait(60);
  const b = [...doc.querySelectorAll('.card-drawer .acts button')].find((x) => x.textContent.trim() === 'Not saved, try again');
  b.click();
  await wait(60);

  //then
  assert.ok(b && !b.disabled, 'the button can be tried again');
  assert.strictEqual(doc.querySelectorAll('#fail-toasts .toast').length, 1, 'one toast however often it fails');
  assert.deepStrictEqual(errors, []);
  w.close();
}

(async () => {
  await anActionAfterTheSessionEndedSaysSignInAndLocksTheButtons();
  console.log('PASS an action in the panel after the session ended (401) says to sign in and locks the buttons');
  await anActionWithoutTheRoleAnymoreSaysSo();
  console.log('PASS a refused action in the panel (403) says it is not allowed and locks the buttons');
  await aFailingActionKeepsItsButtonAndSaysTryAgain();
  console.log('PASS a failing action in the panel keeps its button, says try again and shows one toast');
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
