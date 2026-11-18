const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');

const TEMPLATE = process.argv[2];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (min) => new Date(Date.now() - min * 60000).toISOString();
const MARK = 'cf-row-shown-nowhere';

function stateWith(cloudflare) {
  return {rev: 5, updatedAt: iso(0), sweptIso: iso(1), nextIso: new Date(Date.now() + 59 * 60000).toISOString(), sources: {cf: 'ok'},
    summary: {level: 'ok', headline: 'h', points: [], at: iso(1)}, anomalies: [], services: [], history: [], logTop: [], routes: [], deploys: [], cloudflare};
}

async function boot(state) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', JSON.stringify(state).replace(/<\//g, '<\\/'));
  const answer = (body) => ({ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body)});
  const dom = new JSDOM(html, {
    url: 'https://skopax.example.com/', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () {};
      w.fetch = async (p) => {
        const path = String(p);
        if (path.startsWith('/api/me')) return answer({loginEnabled: false});
        if (path.startsWith('/api/rev')) return answer({rev: state.rev});
        return answer([]);
      };
    },
  });
  await wait(300);
  return {dom, doc: dom.window.document, errors};
}

async function theCorePageReadsNothingOfACloudflareSection() {
  //given
  const shapes = [{checks: {}}, {checks: [{name: MARK, status: 'healthy'}], edge: [], certs: []}, [{part: 'checks', name: MARK, status: 'healthy', plugin: 'cloudflare'}], 'x'];

  for (const shape of shapes) {
    //when
    const {dom, doc, errors} = await boot(stateWith(shape));

    //then
    assert.deepStrictEqual(errors, [], 'no error for ' + JSON.stringify(shape));
    assert.ok(!doc.body.textContent.includes(MARK), 'nothing of the section is drawn for ' + JSON.stringify(shape));
    dom.window.close();
  }
  return 'the core page reads nothing of a cloudflare state section, whatever its shape; the plug-in panel draws it';
}

(async () => {
  for (const t of [theCorePageReadsNothingOfACloudflareSection]) {
    console.log('PASS ' + (await t()));
  }
})().catch((e) => { console.error('FAIL', e.message); process.exit(1); });
