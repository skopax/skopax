const PROP = '-webkit-text-security';
const DYNAMIC = /:(?:focus-visible|focus-within|focus|hover|active)(?![\w-])/g;
const ON_SCREEN = /^@media\s+(?:all|screen)$/i;

function skipString(css, i) {
  const end = css.indexOf(css[i], i + 1);
  return end < 0 ? css.length : end + 1;
}

function closing(css, i) {
  let depth = 1, j = i + 1;
  while (j < css.length && depth) {
    if (css[j] === '"' || css[j] === "'") { j = skipString(css, j); continue; }
    if (css[j] === '{') depth++;
    else if (css[j] === '}') depth--;
    j++;
  }
  return j;
}

function declared(body) {
  let flat = body, prev;
  do { prev = flat; flat = flat.replace(/\{[^{}]*\}/g, ';'); } while (flat !== prev);
  let value;
  flat.split(';').forEach((part) => {
    const at = part.indexOf(':');
    if (at > 0 && part.slice(0, at).trim().toLowerCase() === PROP) value = part.slice(at + 1).trim();
  });
  return value;
}

function rules(css, within, out) {
  let i = 0, start = 0;
  while (i < css.length) {
    const c = css[i];
    if (c === '"' || c === "'") { i = skipString(css, i); continue; }
    if (c === ';' || c === '}') { start = ++i; continue; }
    if (c !== '{') { i++; continue; }
    const head = css.slice(start, i).trim().replace(/\s+/g, ' '), end = closing(css, i), body = css.slice(i + 1, end - 1);
    if (!head.startsWith('@')) out.push({selector: head, within, value: declared(body)});
    if (head.startsWith('@') || body.indexOf('{') >= 0) rules(body, within.concat([head]), out);
    start = i = end;
  }
  return out;
}

function hits(el, selector) {
  try { return el.matches(selector) || el.matches(selector.replace(DYNAMIC, '')); } catch (e) { return false; }
}

function masks(doc, el) {
  const css = [...doc.querySelectorAll('style')].map((s) => s.textContent).join('\n').replace(/\/\*[\s\S]*?\*\//g, '');
  return rules(css, [], []).filter((r) => r.value !== undefined && hits(el, r.selector)).map((r) => ({onScreen: r.within.every((h) => ON_SCREEN.test(h)), value: r.value}));
}

function inlineMask(el) {
  const value = declared(el.getAttribute('style') || '');
  return value === undefined ? null : value;
}

module.exports = {masks, inlineMask};
