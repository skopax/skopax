const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM} = require('jsdom');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', '..', 'skopax', 'kit', 'browser.py'), 'utf8');
const FOCUS = /FOCUS = r"""([\s\S]*?)"""/.exec(SOURCE)[1];

const BANNER = '<div><img id="banner" src="x.jpg" alt="Summer sale ends today" style="height:120px;object-fit:cover"><p id="caption">Summer sale ends today</p></div>';

function probe(html, needle, about) {
  const win = new JSDOM('<!doctype html><body>' + html + '</body>', {runScripts: 'outside-only'}).window;
  win.Element.prototype.getBoundingClientRect = function () {
    const gone = this.hasAttribute('hidden') || /display:\s*none/.test(this.getAttribute('style') || '');
    return gone ? {top: 0, left: 0, width: 0, height: 0} : {top: 10, left: 0, width: 100, height: 50};
  };
  const args = about === undefined ? [needle] : [needle, about];
  const spot = win.eval('(' + FOCUS + ')')(...args);
  const outlined = [...win.document.querySelectorAll('*')].filter((e) => e.style.outline).map((e) => e.id || e.tagName.toLowerCase());
  win.close();
  return {spot, outlined};
}

const CASES = [
  ['a cropped image finding boxes the img, not the caption with the same words', BANNER, 'Summer sale ends today', 'The headline in the banner image is cropped at phone width', ['banner']],
  ['a German image finding boxes the img', BANNER, 'Summer sale ends today', 'Das Banner-Bild ist abgeschnitten', ['banner']],
  ['a German image finding without the word banner boxes the img', BANNER, 'Summer sale ends today', 'Das Bild ist abgeschnitten', ['banner']],
  ['a Spanish image finding boxes the img', BANNER, 'Summer sale ends today', 'La imagen est\u00e1 recortada', ['banner']],
  ['an image word with accented letters boxes the img', BANNER, 'Summer sale ends today', 'Kampanya g\u00f6rselindeki ba\u015fl\u0131k kesilmi\u015f', ['banner']],
  ['a finding with no image word still boxes the img when image and caption sit together', BANNER, 'Summer sale ends today', 'The headline is cut off on the phone view', ['banner']],
  ['a text typo boxes the text', '<div><img id="banner" src="x.jpg" alt="Garden"><p id="caption">Garden Chiars</p></div>', 'Garden Chiars', undefined, ['caption']],
  ['a typo whose words an image alt repeats still boxes the text', BANNER, 'Summer sale ends today', undefined, ['caption']],
  ['a finding with no match draws no box', BANNER, 'Winter clearance', 'The banner image is cropped', []],
  ['a background-image banner is boxed', '<div id="hero" style="background-image:url(x.jpg);height:120px" aria-label="Autumn offers"></div><p id="caption">Autumn offers</p>', 'Autumn offers', 'The banner is cropped on the phone', ['hero']],
  ['words only an image alt holds box the image', '<div><img id="logo" src="x.png" alt="Example Shop"><p id="other">Welcome</p></div>', 'Example Shop', 'Something looks wrong', ['logo']],
  ['an image finding whose words sit only in a caption boxes the image beside it', '<figure><img id="photo" src="x.jpg" alt="Chair"><figcaption id="caption">Order now</figcaption></figure>', 'Order now', 'The picture is cropped', ['photo']],
  ['a hidden img with a matching alt is never boxed', '<div><img id="ghost" hidden src="x.jpg" alt="Summer sale ends today"><p id="caption">Summer sale ends today</p></div>', 'Summer sale ends today', 'The banner image is cropped', ['caption']],
  ['a layout finding with an image far from the text boxes the text', '<div><img id="far" alt="Summer sale ends today"></div><section><div><div><div><p id="caption">Summer sale ends today</p></div></div></div></section>', 'Summer sale ends today', 'The headline is cut off', ['caption']],
  ['the one-argument call still boxes the text', '<p id="caption">Order now</p>', 'Order now', undefined, ['caption']],
];

let failed = false;
for (const [name, html, needle, about, want] of CASES) {
  try {
    const got = probe(html, needle, about);
    assert.deepStrictEqual(got.outlined, want);
    assert.strictEqual(got.spot === null, want.length === 0);
    console.log('PASS ' + name);
  } catch (e) {
    console.error('FAIL ' + name + ': ' + e.message.split('\n')[0]);
    failed = true;
  }
}
process.exit(failed ? 1 : 0);
