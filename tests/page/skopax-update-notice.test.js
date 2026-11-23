const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const RELEASE = 'https://github.com/skopax/skopax/releases/tag/v0.4.0';
const INFO = {id: 'update', level: 'info', text: 'Skopax 0.4.0 is available; this server runs 0.2.0.', href: RELEASE, linkText: 'Release notes'};
const WARN = {id: 'update', level: 'warn', text: 'Skopax 0.4.0 is available and fixes a security issue; this server runs 0.2.0. Update soon.', href: RELEASE, linkText: 'Release notes'};

const BLOB = /(<script type="application\/json" id="brand-data">)[\s\S]*?(<\/script>)/;
const HIDDEN = {name: 'Acme Ops', slug: 'acme-ops', version: '0.2.0', attribution: null};
const hide = (html, hidden) => hidden ? html.replace(BLOB, (m, a, b) => a + JSON.stringify(HIDDEN) + b) : html;

async function boot(opts) {
  opts = opts || {};
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const state = {rev: 1, updatedAt: new Date().toISOString(), sweptIso: new Date().toISOString(), nextIso: new Date(Date.now() + 3600000).toISOString(), sources: {prom: 'ok'},
    summary: {level: 'ok', headline: 'x', points: [], at: new Date().toISOString()}, anomalies: [], services: [], history: [], logTop: [], routes: [], cloudflare: {checks: [], edge: [], certs: []}, pmm: [], deploys: []};
  const admin = opts.admin !== false;
  const dom = new JSDOM(hide(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state)), opts.hidden), {
    url: 'https://skopax.example.com/#t=anoms', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p0, o) => {
        const p = String(p0), method = (o && o.method) || 'GET';
        const ok = (b) => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(b)), text: async () => JSON.stringify(b)});
        if (p.startsWith('/api/me')) return ok({loginEnabled: true, user: 'alice@example.com', userRole: admin ? 'admin' : 'viewer', operator: admin, admin, teams: [], tags: []});
        if (p.startsWith('/api/admin')) {
          calls.push([method, p]);
          if (!admin) return {ok: false, status: 403, json: async () => ({detail: 'Admins only'})};
          if (p === '/api/admin/notices') return ok({items: opts.notices || []});
          return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
        }
        if (p.startsWith('/api/actions') || p.startsWith('/api/deploy-reports') || p.startsWith('/api/requests')) return ok([]);
        if (p.startsWith('/api/rev')) return ok({rev: 1});
        return {ok: false, status: 404, json: async () => ({}), text: async () => ''};
      };
    }});
  await wait(300);
  return {dom, w: dom.window, doc: dom.window.document, errors, calls};
}

const dots = (doc) => [...doc.querySelectorAll('.side-attr .attr-up')];

async function aNewerVersionPutsAnInfoDotOnTheVersionLineAndALinkInTheNotice() {
  //given
  const {dom, doc, errors} = await boot({notices: [INFO]});
  const dot = doc.querySelector('#attribution .attr-up');
  const note = doc.querySelector('#adm-notices .adm-notice[data-id="update"]');
  const link = note.querySelector('a');

  //then
  assert.ok(dot && !dot.hidden && dot.getAttribute('role') === 'img' && dot.getAttribute('aria-label') === INFO.text, 'the version line has an accessible dot');
  assert.strictEqual(dot.title, INFO.text, 'hovering the dot names the new version and the running one');
  assert.ok(dot.parentNode.title.endsWith('\n' + INFO.text), 'the version line tooltip names them too: ' + dot.parentNode.title);
  assert.ok(!dot.classList.contains('k-warn'), 'an information notice gives the plain dot');
  assert.ok(note.classList.contains('k-info') && note.querySelector('.adm-notice-t').textContent === INFO.text);
  assert.strictEqual(link.textContent, 'Release notes');
  assert.strictEqual(link.getAttribute('href'), RELEASE);
  assert.strictEqual(link.getAttribute('target'), '_blank');
  assert.strictEqual(link.getAttribute('rel'), 'noopener noreferrer');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a newer version gives admins an Update available dot on the version line and a notice with a Release notes link that opens safely in a new tab';
}

async function aSecurityUpdateGivesAWarningDotAndNotice() {
  //given
  const {dom, doc, errors} = await boot({notices: [WARN]});
  const dot = doc.querySelector('#attribution .attr-up');
  const note = doc.querySelector('#adm-notices .adm-notice[data-id="update"]');

  //then
  assert.ok(!dot.hidden && dot.classList.contains('k-warn') && dot.getAttribute('aria-label') === WARN.text && dot.title === WARN.text);
  assert.ok(note.classList.contains('k-warn') && note.querySelector('.adm-notice-t').textContent === WARN.text);
  assert.ok(!doc.getElementById('side-admin').querySelector('.a-dot').hidden, 'a warning notice also marks the Admin row');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a security update gives the warning colour on the dot, the label Security update available and a warning notice';
}

async function aLinkThatIsNotHttpsIsNeverRenderedAndTextStaysText() {
  //given
  const evil = '<img src=x onerror=alert(1)>';
  const errors = [];
  for (const href of ['javascript:alert(1)', 'http://github.com/skopax/skopax/releases/tag/v0.4.0']) {
    const t = await boot({notices: [{id: 'update', level: 'info', text: evil, href, linkText: 'Release notes'}]});
    const note = t.doc.querySelector('#adm-notices .adm-notice[data-id="update"]');

    //then
    assert.strictEqual(note.querySelector('.adm-notice-t').textContent, evil);
    assert.strictEqual(note.querySelector('img'), null, 'the text is not parsed as markup');
    assert.strictEqual(note.querySelector('a'), null, 'a link that is not https is dropped: ' + href);
    errors.push(...t.errors);
    t.dom.window.close();
  }
  const plain = await boot({notices: [{id: 'other', level: 'info', text: 'No link here'}]});
  assert.strictEqual(plain.doc.querySelector('#adm-notices a'), null, 'a notice without a link has none');
  assert.ok(dots(plain.doc).every((d) => d.hidden), 'and no update means no dot');
  assert.deepStrictEqual(errors.concat(plain.errors), []);
  plain.dom.window.close();
  return 'a notice link must be https and the notice text is shown as text';
}

async function theRailMarkCarriesTheDotAndAHiddenLineMovesItToTheMenuOrAdminRow() {
  //given
  const shown = await boot({notices: [WARN]});
  const mark = shown.doc.querySelector('#side-attr .attr-mark .attr-up');
  const phone = shown.doc.querySelector('#end-attr .attr-up');
  const info = await boot({notices: [INFO], hidden: true});
  const secure = await boot({notices: [WARN], hidden: true});
  const menu = (t) => t.doc.querySelector('#user-btn .u-dot');

  //then
  assert.ok(mark && !mark.hidden && mark.classList.contains('k-warn') && mark.getAttribute('aria-label') === WARN.text && mark.title === WARN.text, 'the collapsed rail mark has the same dot');
  assert.ok(phone && !phone.hidden && phone.getAttribute('aria-label') === WARN.text, 'the phone line has the dot too');
  assert.strictEqual(info.doc.querySelectorAll('.attr-up').length, 0, 'a hidden attribution has no version line');
  assert.ok(!menu(info).hidden && menu(info).classList.contains('plain') && menu(info).getAttribute('aria-label') === INFO.text, 'the user menu shows a plain dot for an information update');
  assert.ok(!info.doc.querySelector('#um-admin').hidden && info.doc.querySelector('#um-admin').title === 'Admin \u00b7 ' + INFO.text);
  assert.ok(!menu(secure).hidden && !menu(secure).classList.contains('plain') && menu(secure).getAttribute('aria-label') === '1 warning', 'a security update is a warning and is not plain');
  assert.deepStrictEqual(shown.errors.concat(info.errors, secure.errors), []);
  shown.dom.window.close();
  info.dom.window.close();
  secure.dom.window.close();
  return 'the rail mark and the phone line carry the dot, and a hidden attribution puts a plain dot for information and a warning for security on the user menu';
}

async function aViewerSeesNeitherTheNoticeNorTheDot() {
  //given
  const {dom, w, doc, calls, errors} = await boot({admin: false, notices: [WARN]});

  //when
  w.ADMIN_NOTES = [WARN];
  w.sideAdmin();

  //then
  assert.ok(dots(doc).every((d) => d.hidden && !d.hasAttribute('aria-label')), 'the dot stays hidden for a viewer even when notices are present');
  assert.ok(doc.querySelector('#user-btn .u-dot') === null || doc.querySelector('#user-btn .u-dot').hidden, 'and so does the user menu dot');
  assert.ok(doc.getElementById('side-admin').querySelector('.a-dot').hidden);
  assert.deepStrictEqual(calls.filter((c) => c[1] === '/api/admin/notices'), [], 'a viewer never asks for admin notices');
  assert.deepStrictEqual(errors, []);
  dom.window.close();
  return 'a viewer sees no update dot even when notices are present, and never asks for the admin notices';
}

(async () => {
  console.log('PASS ' + await aNewerVersionPutsAnInfoDotOnTheVersionLineAndALinkInTheNotice());
  console.log('PASS ' + await aSecurityUpdateGivesAWarningDotAndNotice());
  console.log('PASS ' + await aLinkThatIsNotHttpsIsNeverRenderedAndTextStaysText());
  console.log('PASS ' + await theRailMarkCarriesTheDotAndAHiddenLineMovesItToTheMenuOrAdminRow());
  console.log('PASS ' + await aViewerSeesNeitherTheNoticeNorTheDot());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
