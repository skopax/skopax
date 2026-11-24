const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();

function item(key, label, value, order) {
  return {key, group: 'Jira tasks', label, help: label + ' help text', kind: 'text', live: true, value, base: '', default: '', source: value ? 'admin' : 'default', by: null, at: null, card: 'Fields', order};
}

const FIELDS = {
  points: {role: 'points', fields: [{id: 'customfield_10200', name: 'Story point estimate', fits: true, label: 'Story point estimate (customfield_10200)'},
    {id: 'customfield_10016', name: 'Story Points', fits: true, label: 'Story Points (customfield_10016)'}, {id: 'summary', name: '<b>Summary</b>', fits: false, label: '<b>Summary</b> (summary)'}],
    suggest: {id: 'customfield_10200', label: 'Story point estimate (customfield_10200)'}},
  sprint: {role: 'sprint', fields: [{id: 'customfield_10020', name: 'Sprint', fits: true, label: 'Sprint (customfield_10020)'}, {id: 'customfield_10021', name: 'Sprint $& goal', fits: true, label: 'Sprint $& goal (customfield_10021)'}],
    suggest: {id: 'customfield_10021', label: 'Sprint $& goal (customfield_10021)'}},
};

async function boot() {
  const errors = [], calls = [], markup = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'}, summary: {level: 'ok', headline: 'x', points: [], at: iso(1)},
    anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const list = [item('jira.points-field', 'Story points field', '', 1), item('jira.sprint-field', 'Sprint field', 'customfield_10020', 3), item('jira.developer-field', 'Developer field', '', 2)];
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), {
    url: 'https://skopax.example.com/#t=settings', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      const inner = Object.getOwnPropertyDescriptor(w.Element.prototype, 'innerHTML');
      Object.defineProperty(w.Element.prototype, 'innerHTML', {configurable: true, get() { return inner.get.call(this); }, set(v) { markup.push(String(v)); inner.set.call(this, v); }});
      w.fetch = async (path, opts) => {
        const p = String(path), method = (opts && opts.method) || 'GET';
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/admin')) {
          calls.push([method, p, opts && opts.body ? JSON.parse(opts.body) : null]);
          if (p.startsWith('/api/admin/jira/fields')) {
            const role = new URL('https://x' + p).searchParams.get('role');
            return FIELDS[role] ? ok(FIELDS[role]) : {ok: false, status: 502, json: async () => ({detail: 'The Jira fields could not be read; type the field id'})};
          }
          if (p.startsWith('/api/admin/history')) return ok([]);
          if (p === '/api/admin/notices') return ok({items: []});
          if (p === '/api/admin/features') return ok({edition: 'community', items: []});
          if (p === '/api/admin/anthropic-key') return ok({using: null, admin: false, config: false, unreadable: false, updatedBy: null, updatedAt: null, note: null});
          if (p.startsWith('/api/admin/small-talk')) return {ok: false, status: 404, json: async () => ({})};
          if (p.startsWith('/api/admin/settings') && method === 'GET') return ok({items: list, rev: 3, refreshSeconds: 15});
          return ok([]);
        }
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'dora.lane@example.com', userRole: 'admin', operator: true, admin: true, teams: [], tags: []});
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        if (p.startsWith('/api/nodes')) return ok({nodes: []});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors, calls, markup};
}

async function theFieldPickerListsJiraFieldsByNameAndOffersTheSuggestionWithoutSaving() {
  //given
  const {w, doc, errors, calls, markup} = await boot();
  const row = (key) => doc.querySelector('#admin .adm-row[data-key="' + key + '"]');
  const input = row('jira.points-field').querySelector('.adm-in');

  //then
  assert.ok(!calls.some((c) => c[1].startsWith('/api/admin/jira/fields')), 'nothing is read from Jira until asked');

  //when
  input.dispatchEvent(new w.Event('focus'));
  await wait(60);
  const options = [...doc.getElementById(input.getAttribute('list')).querySelectorAll('option')].map((o) => [o.value, o.textContent]);
  const note = row('jira.points-field').querySelector('.adm-jira-note');
  const suggested = note.textContent;
  note.querySelector('.adm-jira-use').click();

  //then
  assert.deepStrictEqual(options, [['customfield_10200', 'Story point estimate (customfield_10200)'], ['customfield_10016', 'Story Points (customfield_10016)'], ['summary', '<b>Summary</b> (summary)']], 'names show as text, ids are stored');
  assert.ok(suggested.startsWith('Suggested: Story point estimate (customfield_10200)'), suggested);
  assert.strictEqual(input.value, 'customfield_10200');
  assert.strictEqual(note.textContent, 'In Jira: Story point estimate (customfield_10200)');
  assert.ok(!calls.some((c) => c[0] === 'PUT'), 'a suggestion is never saved by itself');
  assert.ok(!markup.some((m) => m.indexOf('customfield_10200') >= 0 || m.indexOf('Summary') >= 0), 'Jira field names never go through innerHTML');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'the Jira field picker lists fields by name, suggests one, and fills it in without saving';
}

async function aSavedIdShowsItsNameAndAnUnreadListSaysToTypeTheId() {
  //given
  const {w, doc, errors, calls} = await boot();
  const row = (key) => doc.querySelector('#admin .adm-row[data-key="' + key + '"]');

  //when
  row('jira.sprint-field').querySelector('.adm-jira-list').click();
  row('jira.developer-field').querySelector('.adm-jira-list').click();
  await wait(60);

  //then
  assert.strictEqual(row('jira.sprint-field').querySelector('.adm-jira-note').textContent, 'In Jira: Sprint (customfield_10020) \u00b7 Suggested: Sprint $& goal (customfield_10021) Use it', 'a $ in a Jira name is shown as written');
  const again = row('jira.sprint-field').querySelector('.adm-jira-list');
  assert.ok(!again.hidden && again.textContent === 'List again', 'the list can be read again after a load');
  again.click();
  await wait(60);
  assert.strictEqual(calls.filter((c) => c[1] === '/api/admin/jira/fields?role=sprint').length, 2);
  assert.strictEqual(row('jira.developer-field').querySelector('.adm-jira-note').textContent, 'The Jira fields could not be read; type the field id');
  assert.ok(!row('jira.developer-field').querySelector('.adm-jira-list').disabled, 'the list can be asked again');
  assert.deepStrictEqual(errors, []);
  w.close();
  return 'a saved field id shows its Jira name, and a list Jira did not give says to type the id';
}

(async () => {
  for (const t of [theFieldPickerListsJiraFieldsByNameAndOffersTheSuggestionWithoutSaving, aSavedIdShowsItsNameAndAnUnreadListSaysToTypeTheId]) console.log('PASS ' + await t());
})().catch((e) => { console.log('FAIL ' + e.message); console.error(e); process.exit(1); });
