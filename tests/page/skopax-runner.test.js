const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const {spawnSync} = require('child_process');

const SUITES = {
  'skopax-empty.test.js': '',
  'skopax-crash.test.js': "throw new Error('crashed before any check');\n",
  'skopax-fine.test.js': "console.log('PASS one real check');\n",
};

function runOn(names, shard) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'skopax-runner-'));
  try {
    ['run.js', 'clock.js'].forEach((f) => fs.copyFileSync(path.join(__dirname, f), path.join(dir, f)));
    names.forEach((n) => fs.writeFileSync(path.join(dir, n), SUITES[n]));
    const r = spawnSync(process.execPath, [path.join(dir, 'run.js')], {cwd: dir, encoding: 'utf8', env: Object.assign({}, process.env, {SKOPAX_TEST_TZ: 'UTC'}, {SKOPAX_TEST_SHARD: shard || '1/1'})});
    return {code: r.status, out: r.stdout + r.stderr};
  } finally {
    fs.rmSync(dir, {recursive: true, force: true});
  }
}
const block = (out, name) => {
  const lines = out.split('\n'), at = lines.findIndex((l) => l.indexOf(name) >= 0);
  const rest = lines.slice(at + 1), end = rest.findIndex((l) => !l.startsWith('     '));
  return [lines[at]].concat(end < 0 ? rest : rest.slice(0, end)).join('\n');
};

(async () => {
  //given
  const names = Object.keys(SUITES);

  //when
  const all = runOn(names);
  const fine = runOn(['skopax-fine.test.js']);
  const empty = runOn(['skopax-empty.test.js']);

  //then
  assert.deepStrictEqual([empty.code, /^FAIL skopax-empty\.test\.js \(0 passed/m.test(empty.out), /0\/1 suites ok/.test(empty.out)], [1, true, true], 'a suite with no checks fails the run on its own');
  assert.notStrictEqual(all.code, 0);
  assert.ok(/^FAIL skopax-empty\.test\.js \(0 passed/m.test(all.out) && /FAIL no checks ran/.test(block(all.out, 'skopax-empty.test.js')), 'the empty suite fails with no checks ran');
  const crash = block(all.out, 'skopax-crash.test.js');
  assert.ok(/^FAIL skopax-crash\.test\.js/.test(crash) && /crashed before any check/.test(crash), 'a crashed suite fails with its own error');
  assert.ok(!/no checks ran/.test(crash), 'and no second, misleading no checks ran line');
  assert.ok(/^ok   skopax-fine\.test\.js \(1 passed/m.test(all.out));
  assert.ok(/1\/3 suites ok, 1 checks passed/.test(all.out));
  assert.deepStrictEqual([fine.code, /1\/1 suites ok/.test(fine.out)], [0, true], 'a suite with a real check passes');
  const fine3 = ['a', 'b', 'c'].map((n) => 'skopax-fine-' + n + '.test.js');
  fine3.forEach((n) => { SUITES[n] = SUITES['skopax-fine.test.js']; });
  const parts = ['1/2', '2/2'].map((x) => runOn(fine3, x));
  const listed = parts.map((r) => (r.out.match(/^ok   (\S+)/gm) || []).map((l) => l.slice(5)).sort());
  assert.deepStrictEqual(listed, [[fine3[0], fine3[2]], [fine3[1]]], 'each shard runs its own files and together they run every file once');
  assert.strictEqual(runOn(fine3, '3/2').code, 1, 'a shard past the last one is refused');
  console.log('PASS the page runner fails a suite that runs no checks, reports a crash once, and passes a suite with a real check');
})().catch((e) => { console.error('FAIL', e.message); console.error(e); process.exit(1); });
