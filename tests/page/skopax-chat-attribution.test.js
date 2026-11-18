const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM, VirtualConsole} = require('jsdom');

const SCRIPT = fs.readFileSync(path.join(__dirname, '..', '..', 'skopax', 'chatui', 'public', 'skopax-chat.js'), 'utf8');
const BRAND_JSON = '/chat/public/skopax-brand.json';
const LINE = {text: 'Skopax 0.1.0 \u00b7 skopax.io', href: 'https://skopax.io'};
const EVIL = '<img src=x onerror=alert(1)>"\'';
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const unhandled = [];
process.on('unhandledRejection', (e) => unhandled.push(String(e && e.message || e)));

function answer(body) {
  return async () => ({ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body))});
}

async function boot(reply, opts) {
  const framed = !!(opts && opts.framed), runs = (opts && opts.runs) || 1;
  const errors = [], calls = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
  const html = framed ? '<!doctype html><html><head></head><body><iframe></iframe></body></html>' : '<!doctype html><html><head></head><body><div id="root"></div></body></html>';
  const dom = new JSDOM(html, {url: 'https://skopax.example.com/chat/', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc});
  const w = framed ? dom.window.document.querySelector('iframe').contentWindow : dom.window;
  w.fetch = (url, init) => { calls.push([String(url), init && init.credentials]); return reply(url, init); };
  for (let i = 0; i < runs; i++) {
    const tag = w.document.createElement('script');
    tag.textContent = SCRIPT;
    w.document.head.appendChild(tag);
  }
  await wait(60);
  return {w, doc: w.document, errors, calls};
}

async function theChatDrawsOneAttributionLinkFromTheBrandJson() {
  //given
  const {doc, errors, calls} = await boot(answer({attribution: LINE}));

  //when
  const feet = doc.querySelectorAll('#skopax-attribution'), link = doc.querySelector('#skopax-attribution a');

  //then
  assert.deepStrictEqual(errors, []);
  assert.deepStrictEqual(calls, [[BRAND_JSON, 'same-origin']]);
  assert.strictEqual(feet.length, 1);
  assert.strictEqual(feet[0].tagName, 'FOOTER');
  assert.strictEqual(feet[0].className, 'skopax-attribution');
  assert.strictEqual(feet[0].parentNode, doc.body, 'the line sits on the body, outside the chat app root');
  assert.strictEqual(link.textContent, LINE.text);
  assert.strictEqual(link.getAttribute('href'), 'https://skopax.io');
  assert.strictEqual(link.getAttribute('rel'), 'noopener noreferrer');
  assert.ok(doc.documentElement.classList.contains('dark'), 'the theme code still runs');
  console.log('PASS the chat draws one attribution link with the text, href and rel from the brand JSON');
}

async function aHostileDisplayNameIsShownAsText() {
  //given
  const text = EVIL + ' \u00b7 powered by Skopax 0.1.0';
  const {doc, errors} = await boot(answer({attribution: {text, href: 'https://skopax.io'}}));

  //then
  assert.deepStrictEqual(errors, []);
  assert.strictEqual(doc.querySelector('#skopax-attribution a').textContent, text);
  assert.strictEqual(doc.querySelectorAll('#skopax-attribution *').length, 1, 'only the link, no element from the name');
  console.log('PASS a hostile display name in the chat line is shown as text');
}

async function insideTheDashboardFrameTheChatDrawsNoLineAndFetchesNothing() {
  //given
  const {doc, errors, calls} = await boot(answer({attribution: LINE}), {framed: true});

  //then
  assert.deepStrictEqual(errors, []);
  assert.ok(doc.documentElement.classList.contains('in-frame'));
  assert.strictEqual(doc.getElementById('skopax-attribution'), null);
  assert.deepStrictEqual(calls, []);
  console.log('PASS inside the dashboard frame the chat draws no line, the sidebar carries it');
}

async function aNullAttributionDrawsNothing() {
  //given
  const {doc, errors, calls} = await boot(answer({attribution: null}));

  //then
  assert.deepStrictEqual(errors, []);
  assert.strictEqual(calls.length, 1);
  assert.strictEqual(doc.getElementById('skopax-attribution'), null);
  assert.ok(!/skopax\.io|powered by/i.test(doc.body.innerHTML));
  console.log('PASS a null attribution draws no line in the chat');
}

async function aFailedFetchDrawsNothingAndRaisesNothing() {
  //given
  const down = await boot(async () => { throw new TypeError('network down'); });
  const refused = await boot(async () => ({ok: false, status: 401, json: async () => ({detail: 'login first'})}));
  const garbled = await boot(async () => ({ok: true, status: 200, json: async () => { throw new SyntaxError('bad json'); }}));
  const odd = await boot(answer({attribution: {text: '', href: 'https://skopax.io'}}));

  //then
  for (const run of [down, refused, garbled, odd]) {
    assert.deepStrictEqual(run.errors, []);
    assert.strictEqual(run.doc.getElementById('skopax-attribution'), null);
  }
  assert.deepStrictEqual(unhandled, []);
  console.log('PASS a failed, refused or unreadable brand fetch draws nothing and raises nothing');
}

async function runningTheScriptTwiceLeavesOneLine() {
  //given
  const {doc, errors, calls} = await boot(answer({attribution: LINE}), {runs: 2});

  //then
  assert.deepStrictEqual(errors, []);
  assert.ok(calls.length >= 1);
  assert.strictEqual(doc.querySelectorAll('#skopax-attribution').length, 1);
  assert.strictEqual(doc.querySelectorAll('.skopax-attribution a').length, 1);
  console.log('PASS running the chat script twice leaves one attribution line');
}

(async () => {
  try {
    await theChatDrawsOneAttributionLinkFromTheBrandJson();
    await aHostileDisplayNameIsShownAsText();
    await insideTheDashboardFrameTheChatDrawsNoLineAndFetchesNothing();
    await aNullAttributionDrawsNothing();
    await aFailedFetchDrawsNothingAndRaisesNothing();
    await runningTheScriptTwiceLeavesOneLine();
    process.exit(0);
  } catch (e) {
    console.log('FAIL ' + (e && e.message));
    console.log(e && e.stack);
    process.exit(1);
  }
})();
