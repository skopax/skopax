const assert = require('assert');
const {boot, card, iso, article, TAGS} = require('./skopax-card-polish.test.js');

const TEMPLATE = process.argv[2];
const KEY = 'log|checkout-service|ERROR|boom';

function fnv(id) {
  let h = 0x811c9dc5;
  for (let i = 0; i < id.length; i++) { h ^= id.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0; }
  return h % 10;
}
function lum(hex) {
  const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)));
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
}
function contrast(a, b) { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); }

async function eachTagGetsAStableTintFromTheHashOfItsId() {
  //given
  const {w, doc, errors} = await boot([card(KEY, {tags: [{id: 'seo', by: 'rule', at: iso(5)}, {id: 'infra', by: 'rule', at: iso(5)}]})]);
  const again = await boot([card(KEY)]);
  //then
  for (const id of ['seo', 'infra', 'bizdev', 'marketing', 'a-custom-team']) assert.strictEqual(w.tagHue(id), fnv(id), 'FNV-1a of ' + id);
  assert.strictEqual(again.w.tagHue('seo'), w.tagHue('seo'), 'the same id gets the same hue on every load');
  assert.ok(new Set(TAGS.map((t) => w.tagHue(t.id))).size >= 5, 'the core tags spread over the palette');
  const tags = [...article(doc, KEY).querySelectorAll('.tech-fold .biz-tag')];
  assert.deepStrictEqual(tags.map((t) => t.classList.contains('th-' + fnv(t.textContent === 'SEO' ? 'seo' : 'infra'))), [true, true]);
  assert.ok(doc.querySelector('#bf-lists .fb-i[data-type="tag:seo"] .fb-dot').classList.contains('th-' + fnv('seo')), 'the Tags list dot uses the same tint');
  assert.ok(doc.getElementById('tag-tones').textContent.includes('.th-' + fnv('seo')), 'the tones are one style block');
  assert.deepStrictEqual(errors.concat(again.errors), []);
  w.close(); again.w.close();
  console.log('PASS each tag gets a stable tint from the FNV-1a hash of its id, the same on cards and filter chips');
}

async function thePaletteIsCalmAndReadableInBothThemes() {
  //given
  const {w, errors} = await boot([card(KEY)]);
  const pal = w.TAG_PALETTE;
  //then
  assert.strictEqual(pal.length, 10);
  for (const p of pal) {
    for (const mode of ['light', 'dark']) {
      const t = p[mode], c = contrast(t.fg, t.bg);
      assert.ok(c >= 4.5, p.name + ' ' + mode + ' text contrast ' + c.toFixed(2) + ' is below WCAG AA');
      assert.ok(contrast(t.line, t.bg) > 1.3, p.name + ' ' + mode + ' border is visible');
    }
    assert.ok(contrast(p.light.bg, '#ffffff') < 1.15, p.name + ' light background stays a faint tint');
    assert.ok(contrast(p.dark.bg, '#171a20') < 1.3, p.name + ' dark background stays a faint tint');
  }
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS ten calm tag hues, each with AA text contrast in light and dark and a faint background');
}

async function anAdminOverrideWinsOverTheHash() {
  //given
  const tags = TAGS.map((t) => (t.id === 'seo' ? Object.assign({color: (fnv('seo') + 3) % 10}, t) : t));
  const {w, doc, errors} = await boot([card(KEY, {tags: [{id: 'seo', by: 'rule', at: iso(5)}]})], {tags});
  //then
  assert.strictEqual(w.tagTone('seo'), (fnv('seo') + 3) % 10);
  assert.ok(article(doc, KEY).querySelector('.biz-tag').classList.contains('th-' + (fnv('seo') + 3) % 10));
  assert.strictEqual(w.tagTone('infra'), fnv('infra'), 'tags without an override keep the hash');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a colour picked in Admin wins over the hash for that tag only');
}

(async () => {
  for (const t of [eachTagGetsAStableTintFromTheHashOfItsId, thePaletteIsCalmAndReadableInBothThemes, anAdminOverrideWinsOverTheHash]) {
    try { await t(); } catch (e) { console.error('FAIL ' + t.name + ': ' + e.message.split('\n')[0]); process.exitCode = 1; }
  }
  process.exit(process.exitCode || 0);
})();
