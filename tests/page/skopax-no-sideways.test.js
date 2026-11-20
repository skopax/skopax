const pageHtml = require('./skopax-page');
const assert = require('assert');
const {JSDOM} = require('jsdom');

const TEMPLATE = process.argv[2];
const CHECKS = [
  ['Connect MCP at 1024 px: the info grid sizes to the panel, not to the setup command', '.mcp-info', 'grid-template-columns', 'minmax(0px, 1fr)'],
  ['Connect MCP at 1024 px: so do its numbered steps', '.mcp-info ol', 'grid-template-columns', 'minmax(0px, 1fr)'],
  ['Connect MCP at 1024 px: and its lists', '.mcp-info ul', 'grid-template-columns', 'minmax(0px, 1fr)'],
  ['Connect MCP on a phone: the tools table scrolls inside its own box', '.mcp-tools-box', 'overflow-x', 'auto'],
  ['Connect MCP on a phone: and the box is never wider than the panel', '.mcp-tools-box', 'max-width', '100%'],
  ['a long source pill on a phone: the pill is never wider than its row', '.sg-pill', 'max-width', '100%'],
  ['a long source pill on a phone: the pill may shrink', '.sg-pill', 'min-width', '0px'],
  ['a long source pill on a phone: its problem text shrinks', '.sg-bad', 'min-width', '0px'],
  ['a long source pill on a phone: and is cut', '.sg-bad', 'overflow', 'hidden'],
  ['a long source pill on a phone: with an ellipsis', '.sg-bad', 'text-overflow', 'ellipsis'],
  ['the card drawer on a phone: its width holds the border and the padding', '.cd-panel', 'box-sizing', 'border-box'],
  ['the card drawer on a phone: 94 % of the screen at most, and 760 px of content on a wide one', '.cd-panel', 'width', 'min(793px, 94vw)'],
];

function rules(list) {
  return [...list].flatMap((r) => r.cssRules && !r.selectorText ? rules(r.cssRules) : r.selectorText ? [r] : []);
}

function declared(all, selector, prop) {
  return all.filter((r) => r.selectorText.split(',').map((s) => s.trim()).indexOf(selector) >= 0).map((r) => r.style.getPropertyValue(prop)).filter(Boolean);
}

async function everySurfaceOnTheListHasTheRulesThatKeepItFromScrollingSideways() {
  //given
  const dom = new JSDOM(pageHtml(TEMPLATE).replace('/*__SKOPAX_DATA__*/', '{}'));
  const all = rules(dom.window.document.querySelector('style').sheet.cssRules);

  //when
  const missing = CHECKS.filter((c) => declared(all, c[1], c[2]).slice(-1)[0] !== c[3]).map((c) => c[0] + ' (' + c[1] + ' { ' + c[2] + ': ' + c[3] + ' })');

  //then
  assert.ok(all.length > 500, 'the page styles are read: ' + all.length + ' rules');
  assert.deepStrictEqual(missing, [], 'surfaces that would scroll sideways');
  dom.window.close();
  return 'Connect MCP (its grid at 1024 px and its tools table on a phone), the source pills and the card drawer keep the rules that stop them scrolling sideways';
}

(async () => {
  console.log('PASS ' + await everySurfaceOnTheListHasTheRulesThatKeepItFromScrollingSideways());
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
