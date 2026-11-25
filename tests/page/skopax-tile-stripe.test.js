const assert = require('assert');
const fs = require('fs');
const {boot} = require('./skopax-closed.test.js');

const TEMPLATE = process.argv[2];

function rule(css, selector) {
  const m = css.match(new RegExp('(?:^|\\n)' + selector.replace(/[.]/g, '\\.') + ' \\{([^}]*)\\}'));
  return m ? m[1] : '';
}

function aTileHasNoSeverityStripe() {
  //given
  const css = fs.readFileSync(TEMPLATE, 'utf8');
  const tile = rule(css, '.tile'), hover = rule(css, '.tile:hover');
  //then
  assert.ok(tile.includes('border: 1px solid var(--line);'), 'tile has the plain border');
  assert.ok(!/border-left/.test(tile) && !/var\(--c/.test(tile), 'no stripe on the tile');
  assert.ok(hover.includes('border-color: var(--line-2);') && !/border-left|var\(--c/.test(hover), 'hover is plain');
  assert.ok(/\.sev::before \{[^}]*background: var\(--c, var\(--mute\)\)/.test(css), 'the severity badge stays coloured');
  console.log('PASS a tile has no severity stripe and the severity badge keeps its colour');
}

async function aClosedRowShowsItsSeverityBadge() {
  //given
  const {w, doc, errors} = await boot('#t=anoms/closed');
  const row = doc.querySelectorAll('.cl-row');
  const css = fs.readFileSync(TEMPLATE, 'utf8');
  //then
  const crit = row[0].querySelector('.sev'), warn = row[1].querySelector('.sev');
  assert.ok(crit && crit.classList.contains('k-crit') && crit.textContent === 'Critical', 'crit row badge');
  assert.ok(warn && warn.classList.contains('k-warn') && warn.textContent === 'Warning', 'warn row badge');
  assert.ok(row[0].firstElementChild.contains(crit), 'the badge comes before the number');
  assert.ok(!doc.querySelector('.cl-sev') && !/\.cl-sev/.test(css), 'still no stripe');
  assert.deepStrictEqual(errors, []);
  w.close();
  console.log('PASS a closed row shows its severity badge and no stripe');
}

(async () => {
  for (const t of [aTileHasNoSeverityStripe, aClosedRowShowsItsSeverityBadge]) {
    try { await t(); } catch (e) { console.error('FAIL ' + t.name + ': ' + e.message.split('\n')[0]); process.exitCode = 1; }
  }
  process.exit(process.exitCode || 0);
})();
