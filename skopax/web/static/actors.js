var SYSTEM_ACTOR = 'system';
var BRAND = (function () {
  var out = {name: 'Skopax', slug: 'skopax', version: '0.1.0-rc.1'};
  try {
    var d = JSON.parse(document.getElementById('brand-data').textContent);
    if (d && d.name) out = d;
  } catch (e) {}
  if (out.attribution === null) return out;
  if (!out.attribution || !out.attribution.text) {
    var v = out.version || '0.1.0-rc.1';
    out.attribution = {text: out.name === 'Skopax' ? 'Skopax ' + v + ' \u00b7 skopax.io' : out.name + ' \u00b7 powered by Skopax ' + v, href: 'https://skopax.io'};
  }
  return out;
})();
function brandName() { return BRAND.name; }
function brandSlug() { return BRAND.slug || 'skopax'; }
function applyBrand() {
  var home = document.querySelector('h1 a.home'), top = document.querySelector('a.brand'), full = document.getElementById('full'), at = document.getElementById('attribution');
  if (home) { if (!home.querySelector('.wordmark')) home.textContent = BRAND.name; document.title = BRAND.name; }
  if (top && !top.querySelector('.wordmark')) top.textContent = BRAND.name;
  if (full) full.textContent = 'The whole ' + BRAND.name;
  if (at && BRAND.attribution && !at.textContent.trim()) { at.textContent = BRAND.attribution.text; at.setAttribute('href', BRAND.attribution.href); }
}
applyBrand();
function isTokenActor(by) { var t = String(by == null ? '' : by); return t === 'token' || t.indexOf('token:') === 0; }
function tokenActorLabel(by, legacy) { var t = String(by == null ? '' : by); return t.indexOf('token:') === 0 && t.length > 6 ? t.slice(6) : legacy === undefined ? 'Claude' : legacy; }
