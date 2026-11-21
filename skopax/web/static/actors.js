var SYSTEM_ACTOR = 'system';
var BRAND = (function () {
  var out = {name: 'Skopax', slug: 'skopax', version: '0.4.5'};
  try {
    var d = JSON.parse(document.getElementById('brand-data').textContent);
    if (d && d.name) out = d;
  } catch (e) {}
  if (out.attribution === null) return out;
  if (!out.attribution || !out.attribution.text) {
    var v = out.version || '0.4.5';
    out.attribution = {text: out.name === 'Skopax' ? 'Skopax ' + v + ' \u00b7 skopax.io' : out.name + ' \u00b7 powered by Skopax ' + v, href: 'https://skopax.io'};
  }
  return out;
})();
function brandName() { return BRAND.name; }
function investigatorName() { return BRAND.investigator || 'Investigator'; }
function isInvestigatorBy(by) { return by === 'Claude' || (!!BRAND.podBy && by === BRAND.podBy); }
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
function tokenActorLabel(by, legacy) { var t = String(by == null ? '' : by); return t.indexOf('token:') === 0 && t.length > 6 ? t.slice(6) : legacy === undefined ? 'Investigator' : legacy; }
var RES_TIPS = {fixed: 'Fixed: the cause is gone because something was changed, such as a deploy, a config change or a rollback', handled: 'No action needed: harmless or a one-off, nothing was changed',
  duplicate: 'Duplicate: the same problem as another card, which carries it on'};
function resHint(kind) { var t = RES_TIPS[kind] || '', i = t.indexOf(': '); t = i >= 0 ? t.slice(i + 2) : t; return t ? t.charAt(0).toUpperCase() + t.slice(1) + '.' : ''; }
var RES_PLACEHOLDER_WHY = 'Note (optional)', RES_PLACEHOLDER_DUP = 'Card number or key', RES_TITLE_WHY = 'Why it is resolved, becomes the card note (optional)', RES_TITLE_DUP = 'The other card: its number, like 12, or its key';
