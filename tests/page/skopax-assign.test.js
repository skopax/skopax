const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const PICKED = 'log|checkout-service|ERROR|boom';
const MINE = 'log|payments-api|ERROR|npe';
const FREE = 'log|shop-web|ERROR|other';
const ME = 'paul.stone@example.com';

function state() {
  const card = (key, extra) => Object.assign({key, sev: 'warn', status: 'open', svc: key.split('|')[1], label: 'x', src: 'logs', sig: 'log', now: 1, firstSeen: iso(30), lastSeen: iso(1)}, extra || {});
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'warn', headline: 'x', points: [], at: iso(1)},
    anomalies: [card(PICKED, {owner: 'Nina White', assigned: {by: 'zoe.hart@example.com', at: iso(20)}}), card(MINE, {owner: 'Paul Stone', ticket: 'OPS-1'}), card(FREE)],
    services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

async function boot(hash, who) {
  const errors = [], calls = [], asked = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state()));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/' + (hash || '#t=anoms'), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path, opts) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (opts && (opts.method === 'POST' || opts.method === 'PUT')) {
          const body = opts.body ? JSON.parse(opts.body) : null;
          calls.push([opts.method, p, body]);
          if (p === '/api/assignee') {
            const name = body.me ? ME : body.name;
            return ok({rev: 2, owner: name || null, assigned: name ? {by: ME, at: iso(0)} : null, dm: !name ? null : body.me ? 'self' : body.email ? 'sent' : 'none', thread: !!body.email});
          }
          return ok({});
        }
        if (p.startsWith('/api/people')) { asked.push(p); return ok([{name: 'Zoe Hart', email: 'zoe.hart@example.com'}, {name: 'Zoe Lane', email: null}]); }
        if (p.startsWith('/api/me')) return ok(Object.assign({loginEnabled: true, user: ME, operator: true, teams: [], tags: []}, who || {}));
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/nodes')) return ok({nodes: []});
        if (p.startsWith('/api/requests')) return ok([]);
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors, calls, asked};
}

const cardOf = (doc, k) => doc.querySelector('#anoms article.inc[data-key="' + k + '"]');
const shown = (doc) => [...doc.querySelectorAll('#anoms article.inc')].map((c) => c.dataset.key);

async function everyCardShowsItsAssigneeOnTheRowAndTheSideMenuFiltersAssignedMineAndUnassigned() {
  //given
  const {w, doc, errors} = await boot();
  const row = doc.querySelector('#tab-sub-anoms .who-g');
  const chips = () => [...doc.querySelectorAll('#tab-sub-anoms .who-g button.tab-sub-i:not(.child)')].map((b) => b.querySelector('.lbl').textContent + ' ' + b.querySelector('.count').textContent);
  const acts = (k) => [...cardOf(doc, k).querySelectorAll('.acts > button')].map((b) => b.textContent.trim());
  const form = (k) => [...cardOf(doc, k).querySelectorAll('.acts .asg-form button')].map((b) => b.textContent.trim());

  //then
  assert.strictEqual(row.querySelector('.tab-sub-l').textContent, 'Assignee');
  assert.deepStrictEqual(chips(), ['Assigned 2', 'Me 1', 'Unassigned 1']);
  assert.strictEqual(cardOf(doc, PICKED).querySelector('.inc-title .asg-tag').textContent, '@Nina White');
  assert.ok(cardOf(doc, PICKED).querySelector('.asg-tag').title.includes('set by zoe.hart'));
  assert.strictEqual(cardOf(doc, FREE).querySelector('.asg-tag'), null);
  assert.deepStrictEqual(acts(FREE).slice(0, 2), ['Investigate', 'Assign'], 'Assign sits in the action row, right after Investigate');
  assert.strictEqual(cardOf(doc, FREE).querySelector('.asg'), null, 'no assignee box in the middle of the card');
  assert.ok(cardOf(doc, PICKED).querySelector('.asg-btn').title.includes('Assignee: Nina White'));
  assert.deepStrictEqual(form(FREE), ['Assign to me', 'Assign'], 'the popover lists Assign to me first, Assign closes it at the bottom');
  assert.deepStrictEqual(form(PICKED), ['Assign to me', 'Remove assignee', 'Assign'], 'an assigned card also offers Remove assignee');
  const mineBtn = cardOf(doc, MINE).querySelector('.asg-me');
  assert.ok(!mineBtn || mineBtn.hidden, 'no Assign to me on a card that is already yours');

  //when
  doc.querySelector('#tab-sub-anoms .who-g [data-type="who:none"]').click();
  await wait(20);
  const unassigned = shown(doc);
  doc.querySelector('#tab-sub-anoms .who-g [data-type="who:any"]').click();
  await wait(20);
  const assigned = shown(doc);
  doc.querySelector('#tab-sub-anoms .who-g [data-type="who:me"]').click();
  await wait(20);

  //then
  assert.deepStrictEqual(unassigned, [FREE]);
  assert.deepStrictEqual(assigned.sort(), [MINE, PICKED].sort());
  assert.deepStrictEqual(shown(doc), [MINE]);
  assert.ok(decodeURIComponent(w.location.hash).endsWith('/who:me'), w.location.hash);
  assert.ok(doc.querySelector('.filter-note').textContent.includes('Assigned to me'));
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS every card shows its assignee as a chip on the row, Assign sits in the action row, and the Assignee group filters to Assigned, Mine and Unassigned');
}

async function anOperatorAssignsAPersonFromTheJiraSearchThemselvesAndNobody() {
  //given
  const {w, doc, errors, calls, asked} = await boot('#t=anoms', {jira: true});
  const acts = (k) => cardOf(doc, k).querySelector('.acts');

  //when
  acts(FREE).querySelector('.asg-btn').click();
  const inp = acts(FREE).querySelector('.asg-form input');
  const opened = !acts(FREE).querySelector('.asg-form').hidden;
  inp.value = 'zo';
  inp.dispatchEvent(new w.Event('input', {bubbles: true}));
  await wait(400);
  const options = [...acts(FREE).querySelectorAll('.tsg-list li')].map((li) => li.textContent.trim());
  acts(FREE).querySelector('.tsg-list li').dispatchEvent(new w.MouseEvent('mousedown', {bubbles: true}));
  acts(FREE).querySelector('.asg-save').click();
  await wait(30);
  const said = cardOf(doc, FREE).querySelector('.asg-msg').textContent;
  cardOf(doc, FREE).querySelector('.asg-me').click();
  await wait(30);
  cardOf(doc, PICKED).querySelector('.asg-clear').click();
  await wait(30);

  //then
  assert.ok(opened, 'the Assign button opens its form');
  assert.deepStrictEqual(asked, ['/api/people?q=zo']);
  assert.deepStrictEqual(options, ['Zoe Hart zoe.hart@example.com', 'Zoe Lane']);
  assert.deepStrictEqual(calls, [['PUT', '/api/assignee', {key: FREE, name: 'Zoe Hart', email: 'zoe.hart@example.com'}],
    ['PUT', '/api/assignee', {key: FREE, me: true}], ['PUT', '/api/assignee', {key: PICKED, name: ''}]]);
  assert.strictEqual(said, 'Assigned to Zoe Hart \u00b7 told on Slack \u00b7 noted in the Slack thread');
  assert.strictEqual(cardOf(doc, PICKED).querySelector('.asg-msg').textContent, 'Assignee removed');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS an operator assigns a person picked from the Jira people search (told on Slack, thread noted), assigns themselves and clears an assignee');
}

async function aViewerIsReadOnlyOnTheBoardWhileAGuestIsAskedToLogIn() {
  //given
  const viewer = await boot('#t=anoms', {operator: false});
  const guest = await boot('#t=anoms', {operator: false, user: null});
  const first = (doc) => doc.querySelector('#anoms article.inc .acts');

  //when
  const asg = (doc) => first(doc).querySelector('.asg-btn');
  const inv = (doc) => first(doc).querySelector('.inv-btn');

  //then
  assert.deepStrictEqual([asg(viewer.doc), inv(viewer.doc)].map((b) => [b.getAttribute('aria-disabled'), b.title]),
    [['true', 'Needs the operator role: ask an admin'], ['true', 'Needs the operator role: ask an admin']], 'a viewer can neither assign nor investigate');
  assert.deepStrictEqual([asg(guest.doc).getAttribute('aria-disabled'), asg(guest.doc).title], ['true', 'Log in to assign'], 'a guest is asked to log in');
  assert.deepStrictEqual(viewer.errors.concat(guest.errors), []);
  viewer.w.close(); guest.w.close();
  console.log('PASS a viewer is read-only on the board (Assign and Investigate say the operator role is needed), and a guest is asked to log in');
}

async function theSidePanelNamesJiraOnlyWhileJiraServes() {
  //given
  const off = await boot('#t=anoms', {jira: false});
  const on = await boot('#t=anoms', {jira: true, jiraUrl: 'https://jira.example.com', task: {projects: ['OPS']}});
  const words = ({doc}) => {
    const acts = cardOf(doc, FREE).querySelector('.acts');
    const take = acts.querySelector('input[id^="ticket-"]');
    const start = [...acts.querySelectorAll('button')].find((b) => b.getAttribute('aria-label') === 'Start progress');
    return {buttons: [...acts.querySelectorAll('.jira-act')].map((b) => b.textContent), link: acts.querySelector('.jira-form input').placeholder,
      assign: acts.querySelector('.asg-form input').placeholder, take: take ? take.placeholder : null, tip: start.title};
  };

  //when
  const typed = cardOf(off.doc, FREE).querySelector('.acts .asg-form input');
  typed.value = 'zo';
  typed.dispatchEvent(new off.w.Event('input', {bubbles: true}));
  await wait(400);
  [...cardOf(off.doc, MINE).querySelectorAll('.acts button')].find((b) => b.getAttribute('aria-label') === 'Start progress').click();
  [...cardOf(off.doc, MINE).querySelectorAll('.acts .reason button')].find((b) => b.textContent === 'Start progress').click();
  await wait(30);

  //then
  assert.deepStrictEqual(words(off), {buttons: ['Create Jira task', 'Link ticket'], link: 'Ticket key, like OPS-123', assign: 'Name', take: null,
    tip: 'Start progress: the card goes In progress with you as owner'}, 'with Jira off the panel names no Jira search');
  assert.deepStrictEqual(words(on), {buttons: ['Create Jira task', 'Link Jira ticket'], link: 'Jira key, or words from the task title', assign: 'Search people in Jira', take: 'Jira key or task name to search (optional)',
    tip: 'Start progress: the card goes In progress with you as owner, ticket optional'});
  assert.deepStrictEqual(off.calls.map((c) => [c[1], c[2].type, c[2].ticket]), [['/api/actions', 'take', undefined]], 'Start progress with Jira off sends no ticket field, so the server keeps the card\'s OPS-1');
  assert.deepStrictEqual(off.asked, [], 'with Jira off the Name field asks no Jira people search');
  assert.deepStrictEqual(off.errors.concat(on.errors), []);
  off.w.close(); on.w.close();
  console.log('PASS the side panel names Jira only while Jira serves: Link ticket, a Name field to assign, and no ticket field on Start progress with Jira off');
}

(async () => {
  await everyCardShowsItsAssigneeOnTheRowAndTheSideMenuFiltersAssignedMineAndUnassigned();
  await aViewerIsReadOnlyOnTheBoardWhileAGuestIsAskedToLogIn();
  await anOperatorAssignsAPersonFromTheJiraSearchThemselvesAndNobody();
  await theSidePanelNamesJiraOnlyWhileJiraServes();
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
