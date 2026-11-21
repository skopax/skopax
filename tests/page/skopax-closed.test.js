const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString().replace(/\.\d{3}Z$/, 'Z');

function state() {
  return {rev: 1, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'ok', headline: 'Quiet', points: [], at: iso(1)}, anomalies: [], services: [], history: [], logTop: [], routes: [],
    cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
}

function rows(n, from) {
  const out = [];
  for (let i = 0; i < n; i++) {
    out.push({key: 'log|svc-' + (from + i) + '|ERROR|boom', num: 100 + from + i, svc: 'svc-' + (from + i), label: 'ERROR pattern', sev: i === 0 ? 'crit' : 'warn',
      firstSeen: iso(3000 + i), lastSeen: iso(2000 + i), clearedAt: iso(2000 + i), outcome: i === 0 ? 'handled' : 'cleared', ticket: i === 0 ? 'OPS-140' : undefined,
      owner: i === 0 ? 'alice@example.com' : undefined, note: i === 0 ? 'Supplier answered 503 for ten minutes' : undefined});
  }
  return out;
}

async function boot(hash, lang) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const asked = [];
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state()));
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/' + (hash || ''), runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      if (lang) Object.defineProperty(w.document, 'cookie', {get: () => 'skopax_lang=' + lang, set: () => {}, configurable: true});
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (path) => {
        const p = String(path);
        const ok = (body) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body)});
        if (p.startsWith('/api/history/cards')) {
          const q = new URL('https://x' + p).searchParams;
          asked.push(Object.fromEntries(q.entries()));
          const page = Number(q.get('page') || 0), size = Number(q.get('size') || 50);
          if (q.get('q') === 'nothing') return ok({from: iso(10080), to: iso(0), page, size, total: 0, items: [], outcomes: {}});
          const total = 60, start = page * size;
          return ok({from: iso(10080), to: iso(0), page, size, total, items: rows(Math.min(size, total - start), start), outcomes: {}});
        }
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: null, operator: false, jiraUrl: 'https://jira.example.com', task: {projects: ['OPS']}});
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({})};
      };
    },
  });
  await wait(400);
  return {w: dom.window, doc: dom.window.document, errors, asked};
}

async function closedListsCardsThatLeftThePageWithPagingAndFilters() {
  //given
  const {w, doc, errors, asked} = await boot('#t=anoms/closed');

  //when
  const view = doc.getElementById('closed-view');
  const list = [...doc.querySelectorAll('#cl-body .cl-row')];
  const first = list[0];
  doc.querySelector('#cl-body .cl-pager button[data-page="1"]').click();
  await wait(50);
  const second = [...doc.querySelectorAll('#cl-body .cl-row')].map((r) => r.querySelector('.cl-num a').textContent);
  doc.querySelector('.cl-out button[data-outcome="muted"]').click();
  await wait(50);
  doc.querySelector('.cl-range button[data-range="30d"]').click();
  await wait(50);
  const search = doc.getElementById('cl-q');
  search.value = 'svc-3';
  search.dispatchEvent(new w.Event('input'));
  await wait(450);

  //then
  assert.ok(view && doc.getElementById('panel-anoms').classList.contains('closed-on'), '#t=anoms/closed opens the Closed view and hides the live card search');
  assert.strictEqual(doc.querySelector('#tab-sub-anoms button[data-f="closed"]').getAttribute('aria-pressed'), 'true');
  assert.strictEqual(list.length, 50);
  assert.strictEqual(first.querySelector('.cl-num a').getAttribute('href'), '/c/100');
  assert.ok(first.classList.contains('k-crit'));
  assert.strictEqual(first.querySelector('.cl-svc').textContent, 'svc-0');
  assert.ok(first.querySelector('.cl-res').textContent.includes('Resolved') && first.querySelector('.cl-ticket').getAttribute('href') === 'https://jira.example.com/browse/OPS-140');
  assert.strictEqual(first.querySelector('.cl-note').textContent, 'Supplier answered 503 for ten minutes');
  assert.ok(doc.querySelector('.cl-pager').textContent.includes('1\u201350 of 60'));
  assert.deepStrictEqual(second.slice(0, 2), ['#150', '#151'], 'Older shows the next page');
  assert.deepStrictEqual(asked[0], {page: '0', size: '50', days: '7'});
  assert.deepStrictEqual(asked[1], {page: '1', size: '50', days: '7'});
  assert.deepStrictEqual(asked[2], {page: '0', size: '50', days: '7', outcome: 'muted'}, 'an outcome chip goes back to the first page');
  assert.deepStrictEqual(asked[3], {page: '0', size: '50', days: '30', outcome: 'muted'});
  assert.deepStrictEqual(asked[4], {page: '0', size: '50', days: '30', q: 'svc-3', outcome: 'muted'});
  assert.strictEqual(doc.querySelector('#tab-sub-anoms button[data-f="closed"] .count').textContent, '60');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS #t=anoms/closed lists cards that left the page, newest first, 50 a page with Older / Newer, each number opening /c/<n>; outcome, range and search narrow it and go back to page one');
}

async function customDatesEmptyStateAndALiveRepaintKeepTheFilters() {
  //given
  const {w, doc, errors, asked} = await boot('#t=anoms/closed');

  //when
  doc.querySelector('.cl-range button[data-range="custom"]').click();
  await wait(50);
  const days = doc.querySelector('.cl-days');
  doc.getElementById('cl-from').value = '2026-11-01';
  doc.getElementById('cl-to').value = '2026-11-05';
  days.dispatchEvent(new w.Event('change', {bubbles: true}));
  await wait(50);
  const search = doc.getElementById('cl-q');
  search.value = 'nothing';
  search.dispatchEvent(new w.Event('input'));
  await wait(450);
  const empty = doc.querySelector('#cl-body').textContent;
  const before = asked.length;
  w.paint(Object.assign(state(), {rev: 2}));
  await wait(50);

  //then
  const custom = asked.filter((a) => a.from)[0];
  assert.ok(!days.hidden, 'Custom shows the two date fields');
  assert.ok(/^2026-1[01]-\d\dT\d\d:00:00Z$/.test(custom.from) && /^2026-11-0\dT\d\d:59:59Z$/.test(custom.to) && !custom.days, 'local days go out as UTC times');
  assert.ok(empty.includes('No closed card matches.'));
  assert.strictEqual(asked.length, before, 'a live repaint redraws from what is loaded, no new request');
  assert.strictEqual(doc.getElementById('cl-q').value, 'nothing');
  assert.strictEqual(doc.querySelector('.cl-range button[aria-pressed="true"]').dataset.range, 'custom');
  assert.strictEqual(doc.getElementById('cl-from').value, '2026-11-01');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS Custom sends the picked local days as UTC times, an empty result says so, and a live repaint keeps range, dates and search without asking again');
}

(async () => {
  await closedListsCardsThatLeftThePageWithPagingAndFilters();
  await customDatesEmptyStateAndALiveRepaintKeepTheFilters();
})().catch((e) => { console.error(e); process.exit(1); });
