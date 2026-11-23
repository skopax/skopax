const {spawn} = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const DIR = __dirname;
const TEMPLATE = path.join(DIR, '..', '..', 'skopax', 'web', 'template.html');
const LIMIT_MS = 300000;
const PAGE_TZ = process.env.SKOPAX_TEST_TZ || 'UTC';

function noonShift() {
  const now = Date.now();
  const parts = new Intl.DateTimeFormat('en-GB', {timeZone: PAGE_TZ, hourCycle: 'h23', hour: '2-digit', minute: '2-digit', second: '2-digit'}).formatToParts(new Date(now));
  const at = (t) => Number(parts.find((p) => p.type === t).value);
  const sinceMidnight = ((at('hour') * 60 + at('minute')) * 60 + at('second')) * 1000 + (now % 1000);
  return 12 * 3600000 - sinceMidnight;
}
const PARALLEL = Math.max(1, Math.min(4, os.cpus().length));

function runSuite(file) {
  return new Promise((resolve) => {
    const started = Date.now();
    const child = spawn(process.execPath, ['--require', path.join(DIR, 'clock.js'), path.join(DIR, file), TEMPLATE], {cwd: DIR, env: {...process.env, TZ: PAGE_TZ, PAGE_CLOCK_SHIFT_MS: String(SHIFT_MS)}});
    let out = '';
    child.stdout.on('data', (d) => { out += d; });
    child.stderr.on('data', (d) => { out += d; });
    const timer = setTimeout(() => { out += '\nFAIL timed out after ' + LIMIT_MS / 1000 + ' s'; child.kill('SIGKILL'); }, LIMIT_MS);
    child.on('close', (code) => {
      clearTimeout(timer);
      const passed = (out.match(/^PASS /gm) || []).length;
      if (!passed && code === 0 && !/^FAIL/m.test(out)) out += '\nFAIL no checks ran';
      const failed = code !== 0 || /^FAIL/m.test(out);
      resolve({file, passed, failed, out, seconds: Math.round((Date.now() - started) / 100) / 10});
    });
  });
}

const SHIFT_MS = noonShift();

async function main() {
  const only = process.argv.slice(2);
  const shard = /^([1-9]\d*)\/([1-9]\d*)$/.exec(process.env.SKOPAX_TEST_SHARD || '1/1');
  if (!shard || Number(shard[1]) > Number(shard[2])) {
    console.log('FAIL SKOPAX_TEST_SHARD must look like 2/4');
    process.exit(1);
  }
  const matched = fs.readdirSync(DIR).filter((f) => f.endsWith('.test.js') && (!only.length || only.some((o) => f.includes(o)))).sort();
  const files = matched.filter((f, i) => i % Number(shard[2]) === Number(shard[1]) - 1);
  const queue = files.slice();
  const results = [];
  await Promise.all(Array.from({length: PARALLEL}, async () => {
    while (queue.length) {
      const r = await runSuite(queue.shift());
      results.push(r);
      console.log((r.failed ? 'FAIL ' : 'ok   ') + r.file + ' (' + r.passed + ' passed, ' + r.seconds + ' s)');
      if (r.failed) console.log(r.out.trim().split('\n').map((l) => '     ' + l).join('\n'));
    }
  }));
  const bad = results.filter((r) => r.failed);
  const total = results.reduce((n, r) => n + r.passed, 0);
  console.log('\n' + (results.length - bad.length) + '/' + results.length + ' suites ok, ' + total + ' checks passed');
  process.exit(bad.length || !matched.length ? 1 : 0);
}

main();
