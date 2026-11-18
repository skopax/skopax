var SKOPAX_LANG_TAG = '[a-z]{2,3}(-[A-Za-z0-9]{2,8})?';
var SKOPAX_LANGS = (function () {
  var m = new RegExp('(?:^|;\\s*)skopax_lang=(' + SKOPAX_LANG_TAG + ')(?:;|$)').exec(document.cookie || '');
  var browser = navigator.languages && navigator.languages.length ? [].slice.call(navigator.languages) : [navigator.language || 'en'];
  return (m ? [m[1]] : []).concat(browser).map(function (tag) { return String(tag || '').trim(); }).filter(Boolean);
})();
var I18N_NUM = /\d+(?:[.,:]\d+)*/g;
var I18N_MONTHS = /\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b/g;
var I18N_DAYS = /\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b/g;
var I18N_ATTRS = ['title', 'placeholder', 'aria-label', 'data-tip'];
var I18N_SKIP = 'script, style, textarea, pre, code, iframe, .keyline, .svc, .sig, .sdraft-t, .biz-txt, .biz-why, .req-ans, .review-body, .hbody, .live-rows, [data-no-tx]';
var I18N_RULES = {from: null, size: -1, list: []};
var I18N_BAD = new Set();
var I18N_TEXT = new WeakMap();
var I18N_ATTR = new WeakMap();
function txKey(all, tag) {
  var want = String(tag).toLowerCase(), keys = Object.keys(all);
  for (var i = 0; i < keys.length; i++) if (keys[i].toLowerCase() === want) return keys[i];
  return null;
}
function txPrimary(tag) { return String(tag).split('-')[0].toLowerCase(); }
function txLang() {
  var all = window.SKOPAX_I18N;
  if (!all || typeof all !== 'object') return null;
  var keys = Object.keys(all).sort(function (a, b) { var x = a.toLowerCase(), y = b.toLowerCase(); return x < y ? -1 : x > y ? 1 : 0; });
  for (var i = 0; i < SKOPAX_LANGS.length; i++) {
    var tag = SKOPAX_LANGS[i], first = txPrimary(tag);
    var exact = Object.hasOwn(all, tag) ? tag : txKey(all, tag);
    if (exact) return exact;
    if (first === 'en') return null;
    for (var k = 0; k < keys.length; k++) if (txPrimary(keys[k]) === first) return keys[k];
  }
  return null;
}
function txPack() {
  var lang = txLang();
  var pack = lang ? window.SKOPAX_I18N[lang] : null;
  return pack && typeof pack === 'object' ? pack : null;
}
function txIn(map, key) {
  return map && typeof map === 'object' && Object.hasOwn(map, key) && typeof map[key] === 'string' ? map[key] : null;
}
function txNames(s, pack) {
  return s.replace(I18N_MONTHS, function (m) { return txIn(pack['@months'], m) || m; }).replace(I18N_DAYS, function (d) { return txIn(pack['@days'], d) || d; });
}
function txTail(core, pack) {
  var tails = Array.isArray(pack['@tails']) ? pack['@tails'] : [];
  for (var i = 0; i < tails.length; i++) {
    var t = tails[i];
    if (!Array.isArray(t) || typeof t[0] !== 'string' || typeof t[1] !== 'string' || !t[0] || core.length <= t[0].length || !core.endsWith(t[0])) continue;
    var head = txIn(pack, core.slice(0, core.length - t[0].length));
    if (head !== null) return head + t[1];
  }
  return null;
}
function txShape(core, pack) {
  var nums = [], months = [], days = [];
  var plain = core.replace(I18N_NUM, function (m) { nums.push(m); return '{' + (nums.length - 1) + '}'; });
  var named = plain.replace(I18N_MONTHS, function (m) { months.push(m); return '{M}'; }).replace(I18N_DAYS, function (d) { days.push(d); return '{D}'; });
  var out = nums.length ? txIn(pack, plain) : null;
  if (out === null && (months.length || days.length)) out = txIn(pack, named);
  if (out === null) return null;
  var m = 0, d = 0;
  return out.replace(/\{(\d+|M|D)\}/g, function (all, at) {
    if (at === 'M') { var month = months[m++]; return month ? txIn(pack['@months'], month) || month : all; }
    if (at === 'D') { var day = days[d++]; return day ? txIn(pack['@days'], day) || day : all; }
    return nums[+at] != null ? nums[+at] : all;
  });
}
function txRules(pack) {
  var from = Array.isArray(pack['@rules']) ? pack['@rules'] : [];
  if (I18N_RULES.from === from && I18N_RULES.size === from.length) return I18N_RULES.list;
  var list = [];
  from.forEach(function (r) {
    if (!Array.isArray(r) || typeof r[0] !== 'string' || typeof r[1] !== 'string') return;
    try {
      list.push([new RegExp(r[0]), r[1], new RegExp(r[0] + '|').exec('').length - 1]);
    } catch (e) {
      if (!I18N_BAD.has(r[0])) { I18N_BAD.add(r[0]); console.warn('Page language rule skipped, the browser cannot read its pattern: ' + r[0]); }
    }
  });
  I18N_RULES = {from: from, size: from.length, list: list};
  return list;
}
function txRule(core, pack) {
  var rules = txRules(pack);
  for (var i = 0; i < rules.length; i++) {
    var rule = rules[i];
    if (!rule[0].test(core)) continue;
    return core.replace(rule[0], function () {
      var groups = [].slice.call(arguments, 1, 1 + rule[2]);
      return rule[1].replace(/\$([1-9])/g, function (all, n) {
        if (+n > groups.length) return all;
        return groups[+n - 1] == null ? '' : txNames(groups[+n - 1], pack);
      });
    });
  }
  return null;
}
function tx(s, where) {
  var text = String(s == null ? '' : s), pack = txPack();
  if (!pack) return text;
  var core = text.trim();
  if (!core) return text;
  var out = where === 'th' ? txIn(pack['@th'], core) : null;
  if (out === null) out = txIn(pack, core);
  if (out === null) out = txTail(core, pack);
  if (out === null) out = txShape(core, pack);
  if (out === null) out = txRule(core, pack);
  return out === null ? text : text.replace(core, function () { return out; });
}
function txNode(node) {
  if (node.nodeType === 3) {
    var up = node.parentNode;
    if (I18N_TEXT.get(node) === node.nodeValue || (up && up.closest && up.closest(I18N_SKIP))) return;
    var said = tx(node.nodeValue, up && up.closest && up.closest('th') ? 'th' : '');
    if (said !== node.nodeValue) { I18N_TEXT.set(node, said); node.nodeValue = said; }
    return;
  }
  if (node.nodeType !== 1 || (node.matches && node.matches(I18N_SKIP))) return;
  I18N_ATTRS.forEach(function (name) {
    var v = node.getAttribute(name), wrote = I18N_ATTR.get(node);
    if (!v || (wrote && wrote[name] === v)) return;
    var t = tx(v);
    if (t === v) return;
    if (!wrote) I18N_ATTR.set(node, wrote = {});
    wrote[name] = t;
    node.setAttribute(name, t);
  });
  for (var c = node.firstChild; c; c = c.nextSibling) txNode(c);
}
function startLocalize() {
  if (!txPack() || !document.body) return;
  document.documentElement.lang = txLang();
  txNode(document.body);
  new MutationObserver(function (changes) {
    changes.forEach(function (m) {
      if (m.type === 'characterData' || m.type === 'attributes') txNode(m.target);
      else [].forEach.call(m.addedNodes, txNode);
    });
  }).observe(document.body, {childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: I18N_ATTRS});
}
