var TAG_PALETTE = [
  {name: 'Slate', light: {bg: '#eef2fa', line: '#9db0d6', fg: '#2e4573'}, dark: {bg: '#1c2230', line: '#4a5f8a', fg: '#b3c4ea'}},
  {name: 'Teal', light: {bg: '#e8f5f3', line: '#8cc7bd', fg: '#1f5c54'}, dark: {bg: '#15262a', line: '#3e7a72', fg: '#9fd8cf'}},
  {name: 'Green', light: {bg: '#edf5ea', line: '#a3c99a', fg: '#34612b'}, dark: {bg: '#1a261a', line: '#4f7a47', fg: '#b0d9a6'}},
  {name: 'Olive', light: {bg: '#f3f3e4', line: '#c2c08a', fg: '#575522'}, dark: {bg: '#24241a', line: '#77753f', fg: '#d3d19b'}},
  {name: 'Sand', light: {bg: '#f6efe7', line: '#cdb092', fg: '#6a4a2b'}, dark: {bg: '#2a221b', line: '#866547', fg: '#e0c3a4'}},
  {name: 'Plum', light: {bg: '#f6edf3', line: '#cfa0c1', fg: '#6b3460'}, dark: {bg: '#2a1c27', line: '#85507a', fg: '#e2b3d6'}},
  {name: 'Violet', light: {bg: '#f1eefa', line: '#b4a6dd', fg: '#4c3b86'}, dark: {bg: '#221e33', line: '#65579a', fg: '#c6baf0'}},
  {name: 'Dusk', light: {bg: '#eef0f6', line: '#a9b0c9', fg: '#3e4566'}, dark: {bg: '#1f212b', line: '#5b6283', fg: '#c0c5df'}},
  {name: 'Cyan', light: {bg: '#e9f4f8', line: '#90c2d4', fg: '#1f5a6e'}, dark: {bg: '#16252b', line: '#3f7a8f', fg: '#a2d3e4'}},
  {name: 'Clay', light: {bg: '#f7eeee', line: '#cfa7a7', fg: '#6e3d3d'}, dark: {bg: '#2b1f1f', line: '#875959', fg: '#e5bcbc'}}
];
var TAG_TONE_SET = {};
function tagHue(id) {
  var h = 0x811c9dc5, s = String(id == null ? '' : id);
  for (var i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0; }
  return h % TAG_PALETTE.length;
}
function tagTone(id) {
  var n = TAG_TONE_SET[id];
  return typeof n === 'number' && n >= 0 && n < TAG_PALETTE.length && n === Math.floor(n) ? n : tagHue(id);
}
function tagClass(id) { return 'tcol th-' + tagTone(id); }
function useTagTones(list) {
  var next = {};
  (Array.isArray(list) ? list : []).forEach(function (t) { if (t && typeof t.id === 'string' && typeof t.color === 'number') next[t.id] = t.color; });
  var changed = JSON.stringify(next) !== JSON.stringify(TAG_TONE_SET);
  TAG_TONE_SET = next;
  return changed;
}
(function () {
  if (typeof document === 'undefined' || document.getElementById('tag-tones')) return;
  var vars = function (mode) {
    return TAG_PALETTE.map(function (p, i) { var t = p[mode]; return '--th' + i + 'b: ' + t.bg + '; --th' + i + 'l: ' + t.line + '; --th' + i + 'f: ' + t.fg + ';'; }).join(' ');
  };
  var css = ':root { ' + vars('light') + ' }\n@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { ' + vars('dark') + ' } }\n:root[data-theme="dark"] { ' + vars('dark') + ' }\n' +
    TAG_PALETTE.map(function (p, i) { return '.th-' + i + ' { --tb: var(--th' + i + 'b); --tl: var(--th' + i + 'l); --tf: var(--th' + i + 'f); }'; }).join('\n');
  var s = document.createElement('style');
  s.id = 'tag-tones';
  s.textContent = css;
  (document.head || document.documentElement).appendChild(s);
})();
