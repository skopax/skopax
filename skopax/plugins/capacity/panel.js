(function () {
  var PLUGIN = 'capacity';
  var SEV = {crit: ['Critical', 'k-crit'], warn: ['Warning', 'k-warn'], info: ['Info', 'k-info'], ok: ['Healthy', 'k-ok']};
  var RANK = {crit: 0, warn: 1, info: 2};
  var NOTES = {
    disk: 'Disk every 15 minutes: how full each persistent volume is and how fast it grows, and the host disks node_exporter reports. ' +
      'A disk warns when it is nearly full or fills within days at the growth of the last day.',
    jvm: 'JVM old-generation memory still live after garbage collection, per service, every 15 minutes (highest pod). ' +
      'Memory that survives GC is in use: a leak or an oversized cache fills it, then long GC pauses and an OutOfMemoryError follow. Raw heap use near the limit is normal garbage and says nothing.',
    cap: 'Autoscalers every 15 minutes, the ones closest to their maximum first. One at its maximum for 30 minutes warns when its CPU is at or above target ' +
      '(it needs more pods than it may add), and is info when CPU is under target. Autoscalers whose minimum equals their maximum are left out.'
  };
  var TABLES = {
    disk: [
      {part: 'volumes', title: 'Volumes', heads: ['Status', 'Volume', 'Used', 'Free', 'Per day', 'Full in'], none: 'No volume data yet. The capacity check fills this on its next run.', quiet: 'The last check found no volumes.', failed: 'The last check could not read the volumes.', refused: 'Not read: no access to the volumes.'},
      {part: 'dbdisk', title: 'Host disks', heads: ['Status', 'Host and disk', 'Used', 'Free', 'Per day', 'Full in'], none: 'No host disk data yet. The capacity check fills this on its next run.', quiet: 'The last check found no host disks.', failed: 'The last check could not read the host disks.', refused: 'Not read: no access to the host disks.', off: 'Reading host disks is switched off.'}
    ],
    jvm: [{part: 'heap', heads: ['Status', 'Service', 'After GC', 'GC'], none: 'No JVM heap data yet.', quiet: 'The last check found no JVM heap series.', failed: 'The last check could not read the JVM heap.', refused: 'Not read: no access to the JVM heap.', off: 'Reading the JVM heap is switched off.'}],
    cap: [{part: 'hpa', heads: ['Status', 'Autoscaler', 'Replicas', 'CPU'], none: 'No autoscaler data yet.', quiet: 'The last check found no autoscalers.', failed: 'The last check could not read the autoscalers.', refused: 'Not read: no access to the autoscalers.'}]
  };
  var PARTS = {volumes: 'volumes', autoscalers: 'hpa', 'JVM heap': 'heap', 'host disks': 'dbdisk'};
  var state = null, sections = {};

  function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }
  function dash() { return '<span class="zero-v">\u2013</span>'; }
  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function num(v) { return isNum(v) ? String(v === Math.round(v) ? v : Math.round(v * 100) / 100) : dash(); }
  function pct(v) { return isNum(v) ? (v >= 10 ? Math.round(v) : v.toFixed(1)) + '%' : dash(); }
  function gb(v) { return isNum(v) ? (v >= 100 ? Math.round(v) : v.toFixed(1)) + ' GB' : dash(); }
  function grow(v) { return isNum(v) ? (v > 0 ? '+' : '') + v.toFixed(1) + ' GB' : dash(); }
  function days(v) { return isNum(v) ? v + ' d' : dash(); }
  function graph(r, text, tip) {
    var url = typeof r.link === 'string' && /^https:\/\/[^\s"'<>]+$/.test(r.link) ? r.link : '';
    return url ? '<a class="lnk" href="' + esc(url) + '" target="_blank" rel="noopener" title="' + esc(tip) + '">' + text + ' \u2197</a>' : text;
  }
  function badge(sev, label) { var x = SEV[sev] || SEV.info; return '<span class="state ' + x[1] + '">' + esc(label || x[0]) + '</span>'; }
  function where(r) { return '<span class="ns">' + esc(r.ns) + (r.cluster ? ' \u00b7 ' + esc(r.cluster) : '') + '</span>'; }

  function rows() {
    var all = state && Array.isArray(state.capacity) ? state.capacity : [];
    return all.filter(function (r) { return r && typeof r === 'object' && r.plugin === PLUGIN; });
  }
  function cards() {
    var all = state && Array.isArray(state.anomalies) ? state.anomalies : [];
    return all.filter(function (a) { return a && a.src === PLUGIN && a.status === 'open' && !a.muted && a.triage !== 'progress' && a.triage !== 'handled'; })
      .sort(function (x, y) { return (RANK[x.sev] == null ? 3 : RANK[x.sev]) - (RANK[y.sev] == null ? 3 : RANK[y.sev]); });
  }
  function owns(a, r) {
    if (a.capacity !== r.part || (a.cluster || '') !== (r.cluster || '')) return false;
    if (r.part === 'volumes') return a.svc === r.pvc && a.ns === r.ns;
    if (r.part === 'hpa') return a.svc === r.hpa && a.ns === r.ns;
    if (r.part === 'heap') return a.svc === r.job && a.ns === r.ns;
    return a.svc === r.node && a.sub === r.mount;
  }
  function status(list, fallback) {
    if (!list.length) return fallback || badge('ok');
    var tags = list.map(function (a) {
      var x = SEV[a.sev] || SEV.info;
      return '<a class="tag ' + x[1] + '" href="#a=' + esc(encodeURIComponent(String(a.key))) + '" title="' + esc(a.short || 'Open this anomaly') + '">' + esc(a.label || a.sig) + '</a>';
    });
    return badge(list[0].sev) + '<div class="sig-tags">' + tags.join('') + '</div>';
  }
  function cellsOf(r, open) {
    var list = open.filter(function (a) { return owns(a, r); });
    if (r.part === 'volumes') {
      return '<td>' + status(list) + '</td><td class="any-wrap"><span class="svc">' + esc(r.pvc) + '</span> ' + where(r) + '</td>' +
        '<td class="num">' + graph(r, pct(r.pct), 'used share over 2 days') + '</td><td class="num">' + gb(r.freeGb) + ' <span class="ns">of ' + gb(r.capGb) + '</span></td>' +
        '<td class="num">' + grow(r.growGbDay) + '</td><td class="num">' + days(r.daysToFull) + '</td>';
    }
    if (r.part === 'dbdisk') {
      return '<td>' + status(list) + '</td><td class="any-wrap"><span class="svc">' + esc(r.node) + '</span> <span class="ns">' + esc(r.mount) + (r.cluster ? ' \u00b7 ' + esc(r.cluster) : '') + '</span></td>' +
        '<td class="num">' + pct(r.pct) + '</td><td class="num">' + gb(r.freeGb) + ' <span class="ns">of ' + gb(r.capGb) + '</span></td>' +
        '<td class="num">' + grow(r.growGbDay) + '</td><td class="num">' + days(r.daysToFull) + '</td>';
    }
    if (r.part === 'hpa') {
      var full = isNum(r.current) && isNum(r.max) && r.current >= r.max;
      return '<td>' + status(list, full ? badge('warn', 'At max') : '') + '</td><td><span class="svc">' + esc(r.hpa) + '</span> ' + where(r) + '</td>' +
        '<td class="num">' + num(r.current) + ' / ' + num(r.max) + ' <span class="ns">min ' + num(r.min) + '</span></td>' +
        '<td class="num">' + pct(r.cpu) + ' <span class="ns">target ' + pct(r.cpuTarget) + '</span></td>';
    }
    return '<td>' + status(list) + '</td><td><span class="svc">' + esc(r.job) + '</span> ' + where(r) + '</td>' +
      '<td class="num">' + graph(r, pct(r.pct), 'old generation live after GC, per pod') + ' <span class="ns">of ' + num(r.maxMb) + ' MB</span></td>' +
      '<td class="num">' + (isNum(r.gcMsPerS) ? esc(r.gcMsPerS) + ' ms/s' : dash()) + '</td>';
  }
  function failures() {
    var src = state && state.sources && typeof state.sources === 'object' ? state.sources : {};
    var texts = [], parts = {};
    Object.keys(src).filter(function (k) {
      var v = String(src[k] || '');
      return (k === PLUGIN || k.indexOf(PLUGIN + '-') === 0) && v && v !== 'ok' && v.indexOf('ok ') !== 0;
    }).sort().forEach(function (k) {
      var why = String(src[k]).replace(/^error:\s*/, '');
      var named = why.split(' \u00b7 ')[0].split('; ').map(function (x) { return PARTS[x.split(':')[0].trim()]; }).filter(Boolean);
      (named.length ? named : Object.keys(PARTS).map(function (x) { return PARTS[x]; })).forEach(function (x) { parts[x] = true; });
      texts.push((k === PLUGIN ? '' : k.slice(PLUGIN.length + 1) + ': ') + why);
    });
    return {texts: texts, parts: parts};
  }
  function refused(part, summaries) {
    return summaries.some(function (r) { return Array.isArray(r.noAccess) && r.noAccess.indexOf(part) >= 0; });
  }
  function switchedOff(part, summaries) {
    return summaries.length > 0 && summaries.every(function (r) { return Array.isArray(r.off) && r.off.indexOf(part) >= 0; });
  }
  function table(spec, mine, summaries, open, bad) {
    var list = mine.filter(function (r) { return r.part === spec.part; });
    var empty = bad.parts[spec.part] ? spec.failed : refused(spec.part, summaries) ? spec.refused : spec.off && switchedOff(spec.part, summaries) ? spec.off : summaries.length ? spec.quiet : spec.none;
    var body = list.length ? list.map(function (r) { return '<tr>' + cellsOf(r, open) + '</tr>'; }).join('') :
      '<tr class="empty"><td colspan="' + spec.heads.length + '">' + esc(empty) + '</td></tr>';
    var head = spec.heads.map(function (h, i) { return '<th' + (i > 1 ? ' class="num"' : '') + '>' + esc(h) + '</th>'; }).join('');
    return (spec.title ? '<h3 class="sec-t">' + esc(spec.title) + '</h3>' : '') +
      '<div class="box"><table data-part="' + esc(spec.part) + '"><thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody></table></div>';
  }
  function draw() {
    if (!state) return;
    var mine = rows(), open = cards(), bad = failures();
    var summaries = mine.filter(function (r) { return r.part === 'summary'; });
    Object.keys(sections).forEach(function (tab) {
      var section = sections[tab];
      if (!section || !TABLES[tab]) return;
      var note = NOTES[tab] + (bad.texts.length ? ' The last capacity check failed partly (' + bad.texts.join('; ') + ').' : '');
      section.innerHTML = '<p class="panel-note">' + esc(note) + '</p>' + TABLES[tab].map(function (spec) { return table(spec, mine, summaries, open, bad); }).join('');
    });
  }

  document.addEventListener('skopax:state', function (e) { state = e.detail && e.detail.state; draw(); });
  document.addEventListener('skopax:page', function (e) {
    var d = e.detail || {};
    if (!TABLES[d.tab]) return;
    sections[d.tab] = d.section;
    draw();
  });
})();
