var COST_SOURCES = [{id: 'pod', label: 'Investigator runs'}, {id: 'page', label: 'Page requests'}, {id: 'slack', label: 'Slack requests'}, {id: 'chat', label: 'Chat'}, {id: 'assess', label: 'Assessments'}, {id: 'gate', label: 'Gate'}, {id: 'smalltalk', label: 'Small talk'}, {id: 'pagescan', label: 'Page scan'}, {id: 'selfreview', label: 'Self-review'}, {id: 'threadsummary', label: 'Thread summary'}, {id: 'mcp', label: 'MCP'}, {id: 'guard', label: 'Guard'}];
var COST_PARTS = [{id: 'cacheRead', label: 'Cache read'}, {id: 'input', label: 'Input'}, {id: 'cacheWrite5m', label: 'Cache write'}, {id: 'cacheWrite1h', label: 'Cache write, long'}, {id: 'output', label: 'Output'}, {id: 'webSearch', label: 'Web search'}];
var COST_RANGES = [1, 7, 30];
var COSTS = null;
var COSTS_SEQ = 0;
var COSTS_OP = null;
var COSTS_LOCKED = false;
var COSTS_FILTER = null;
var COSTS_RUN_CAP = 100;
var COSTS_DAYS = (function () { var d = parseInt(load('skopax-costs-days'), 10); return COST_RANGES.indexOf(d) >= 0 ? d : 7; })();
var COST_SHOWN_BY = {'Claude (self-review)': 'AI (self-review)', 'Claude': 'the investigator'};
function costBy(by) { return isTokenActor(by) ? tokenActorLabel(by, 'API calls') : COST_SHOWN_BY[by] || (isInvestigatorBy(by) ? investigatorName() : by); }
function costLabel(list, id) { var x = list.filter(function (s) { return s.id === id; })[0]; return x ? x.label : id; }
function costPct(v) { return v == null ? '–' : Math.round(v * 100) + ' %'; }
function costUsd(v) { return v == null ? '–' : '$' + (v > 0 && v < 0.01 ? Number(v).toFixed(4) : Number(v).toFixed(2)); }
var COST_GUARD_NOTE = 'With the guard on, questions are refused and answers will be held once the cap is reached.';
function costMeter(t, guard) {
  var share = t.cap > 0 ? t.spent / t.cap : 0;
  var k = share >= 1 ? 'k-crit' : share >= 0.75 ? 'k-warn' : 'k-info';
  return '<div class="ctile ctoday"><span class="cl">Today</span><span class="cv">' + esc(costUsd(t.spent)) + ' <small>of ' + esc(costUsd(t.cap)) + ' cap</small></span>' +
    '<span class="cmeter ' + k + '" role="meter" aria-valuemin="0" aria-valuemax="' + esc(t.cap) + '" aria-valuenow="' + esc(t.spent) + '" aria-label="Spent today against the daily cap"><span style="width:' + Math.min(share * 100, 100).toFixed(1) + '%"></span></span>' + (guard ? '<span class="cn ccap-note">' + esc(COST_GUARD_NOTE) + '</span>' : '') + '</div>';
}
function costTile(label, value, note) {
  return '<div class="ctile"><span class="cl">' + esc(label) + '</span><span class="cv">' + esc(value) + '</span>' + (note ? '<span class="cn">' + esc(note) + '</span>' : '') + '</div>';
}
function costStrip(d) {
  return '<div class="cstrip">' + costMeter(d.today, d.guardOn === true) +
    costTile(d.days === 1 ? 'Today, all sources' : 'Last ' + d.days + ' days', costUsd(d.total.usd), plural(d.total.runs, 'run', 'runs') + ' · ' + plural(d.total.calls, 'call', 'calls')) +
    costTile('Avg per Slack request', costUsd(d.avg.slackAsk)) + costTile('Avg per investigator run', costUsd(d.avg.podRun)) + costTile('Cache hit share', costPct(d.total.cacheHit), 'cache reads over all prompt tokens') + '</div>';
}
var COST_RUN_SOURCES = COST_SOURCES.filter(function (s) { return s.id !== 'gate' && s.id !== 'smalltalk' && s.id !== 'pagescan' && s.id !== 'selfreview' && s.id !== 'threadsummary' && s.id !== 'guard'; });
function costModel(m) { return String(m || '?').replace(/^claude-/, ''); }
function costTick(t, hour) {
  if (hour) return t.slice(11, 13) + ':00';
  var p = t.split('-');
  return Number(p[2]) + ' ' + MONTHS[Number(p[1]) - 1];
}
function costScale(max) {
  if (!(max > 0)) return [0, 1];
  var raw = max / 4, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  var step = [1, 2, 2.5, 5, 10].map(function (m) { return m * mag; }).filter(function (s) { return s >= raw; })[0];
  var ticks = [];
  for (var v = 0; v < max + step / 2 && ticks.length < 6; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
  if (ticks[ticks.length - 1] < max) ticks.push(Math.round((ticks[ticks.length - 1] + step) * 1e6) / 1e6);
  return ticks;
}
function costEvery(n) { return n <= 4 ? 1 : n <= 8 ? 2 : n <= 24 ? 6 : 7; }
function costLegend(list, prefix) {
  return '<div class="clegend">' + list.map(function (s) { return '<span><i class="sw ' + prefix + '-' + s.id + '"></i>' + esc(s.label) + '</span>'; }).join('') + '</div>';
}
function costAxis(ticks, fmt) {
  var top = ticks[ticks.length - 1];
  return '<div class="cy" aria-hidden="true">' + ticks.map(function (t) { return '<span style="bottom:' + (t / top * 100) + '%">' + esc(fmt(t)) + '</span>'; }).join('') + '</div>' +
    ticks.map(function (t) { return '<i class="cgl" style="bottom:' + (t / top * 100) + '%"></i>'; }).join('');
}
function costXAxis(keys, hour) {
  var every = costEvery(keys.length);
  return '<div class="cx" aria-hidden="true">' + keys.map(function (k, i) { return '<span>' + (i % every ? '' : esc(costTick(k, hour))) + '</span>'; }).join('') + '</div>';
}
function costBars(rows, list, value, hour, label) {
  var totals = rows.map(function (r) { return list.reduce(function (a, s) { return a + (value(r, s.id) || 0); }, 0); });
  var ticks = costScale(Math.max.apply(null, totals.concat([0]))), top = ticks[ticks.length - 1];
  var cols = rows.map(function (r, i) {
    var parts = list.filter(function (s) { return value(r, s.id) > 0; });
    var tip = costTick(r.t, hour) + ' · ' + costUsd(totals[i]) + (parts.length ? ' · ' + parts.map(function (s) { return s.label + ' ' + costUsd(value(r, s.id)); }).join(' · ') : '');
    return '<div class="ccol" data-t="' + esc(r.t) + '" title="' + esc(tip) + '">' + parts.map(function (s) {
      return '<b class="s-' + s.id + '" style="height:' + (value(r, s.id) / top * 100).toFixed(2) + '%"></b>';
    }).join('') + '</div>';
  }).join('');
  return '<div class="cchart" role="img" aria-label="' + esc(label) + '">' + '<div class="cplot">' + costAxis(ticks, costUsd) + '<div class="ccols">' + cols + '</div></div>' + costXAxis(rows.map(function (r) { return r.t; }), hour) + '</div>';
}
function costLine(rows, list, hour, label) {
  var n = rows.length, x = function (i) { return (i + 0.5) / n * 100; };
  var lines = list.map(function (s) {
    var runs = [], cur = [];
    rows.forEach(function (r, i) {
      var v = r.hit[s.id];
      if (v == null) {
        if (cur.length) runs.push(cur);
        cur = [];
        return;
      }
      cur.push(x(i).toFixed(2) + ',' + (100 - v * 100).toFixed(2));
    });
    if (cur.length) runs.push(cur);
    return runs.map(function (pts) { return '<polyline class="ln s-' + s.id + '" vector-effect="non-scaling-stroke" points="' + pts.join(' ') + '"></polyline>'; }).join('');
  }).join('');
  var dots = list.map(function (s) {
    return rows.map(function (r, i) { var v = r.hit[s.id]; return v == null ? '' : '<i class="cdot s-' + s.id + '" style="left:' + x(i).toFixed(2) + '%;bottom:' + (v * 100).toFixed(2) + '%"></i>'; }).join('');
  }).join('');
  var hits = rows.map(function (r) {
    var tip = costTick(r.t, hour) + ' · ' + list.map(function (s) { return s.label + ' ' + costPct(r.hit[s.id]); }).join(' · ');
    return '<div class="ccol" data-t="' + esc(r.t) + '" title="' + esc(tip) + '"></div>';
  }).join('');
  return '<div class="cchart" role="img" aria-label="' + esc(label) + '">' + '<div class="cplot">' + costAxis([0, 0.5, 1], costPct) +
    '<svg class="clines" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">' + lines + '</svg>' + dots + '<div class="ccols">' + hits + '</div></div>' +
    costXAxis(rows.map(function (r) { return r.t; }), hour) + '</div>';
}
function costTable(rows, list, value, fmt, hour) {
  return '<details class="ctable"><summary class="chev">Table</summary><div class="box"><table><thead><tr><th>' + (hour ? 'Hour' : 'Day') + '</th>' +
    list.map(function (s) { return '<th class="num">' + esc(s.label) + '</th>'; }).join('') + '</tr></thead><tbody>' +
    rows.map(function (r) { return '<tr><td>' + esc(costTick(r.t, hour)) + '</td>' + list.map(function (s) { return '<td class="num">' + esc(fmt(value(r, s.id))) + '</td>'; }).join('') + '</tr>'; }).join('') +
    '</tbody></table></div></details>';
}
function costAdvisor(s) {
  var a = s.advisor;
  if (!a || !(a.calls > 0)) return '';
  var models = Array.isArray(a.models) ? a.models : [];
  return '<tr class="csrc-advisor" data-advisor="' + esc(s.source) + '"><td class="cadv">advisor</td><td class="num">' + esc(a.calls) + '</td><td class="num">' + esc(a.runs == null ? '–' : a.runs) + '</td><td class="num"><b>' + esc(costUsd(a.usd)) + '</b>' +
    (a.estimatedUsd > 0 ? ' <span class="cest">est.</span>' : '') + '</td>' +
    COST_PARTS.map(function () { return '<td class="num">–</td>'; }).join('') +
    '<td class="num">–</td><td class="num">–</td><td class="cmodels">' + esc(models.map(costModel).join(' · ') || '–') + '</td></tr>';
}
var COST_GUARD_CATEGORIES = [{id: 'injection', label: 'injection'}, {id: 'harmful', label: 'harmful'}, {id: 'leak', label: 'leak'}, {id: 'contradicted', label: 'contradicted'}, {id: 'other', label: 'other'}];
function costObjRuns(d) { return d && Array.isArray(d.objectionRuns) ? d.objectionRuns : null; }
function costFilterIs(kind, value) { return !!COSTS_FILTER && COSTS_FILTER.kind === kind && COSTS_FILTER.value === value; }
function costFilterBtn(kind, value, text, title) {
  return '<button type="button" class="cobj" data-filter="' + esc(kind) + '" data-value="' + esc(value) + '" aria-pressed="' + costFilterIs(kind, value) + '" aria-controls="costs-runs" title="' + esc(tx(title)) + '">' + text + '</button>';
}
function costHasRuns(d, field, value) { return (d.runs || []).some(function (r) { return r && r[field] === value; }); }
var COST_FILTERS = {
  objection: {one: 'run with this objection', many: 'runs with this objection', empty: 'No run in this range had this objection.',
    label: function (v) { return tx(costLabel(COST_GUARD_CATEGORIES, v)); },
    runs: function (d, v) { return (costObjRuns(d) || []).filter(function (r) { return r && r.objections && Number(r.objections[v]) > 0; }); },
    valid: function (d, v) { var g = (d.sources || []).filter(function (s) { return s.source === 'guard'; })[0]; return !!(costObjRuns(d) && g && g.categories && Number(g.categories[v]) > 0); }},
  source: {one: 'run from this source', many: 'runs from this source', empty: 'No runs in this range.', capped: true,
    label: function (v) { return costLabel(COST_SOURCES, v); },
    runs: function (d, v) { return (d.runs || []).filter(function (r) { return r && r.source === v; }); },
    valid: function (d, v) { return costHasRuns(d, 'source', v); }},
  by: {one: 'run by this person', many: 'runs by this person', empty: 'No runs in this range.', capped: true,
    label: function (v) { return costBy(v); },
    runs: function (d, v) { return (d.runs || []).filter(function (r) { return r && r.by === v; }); },
    valid: function (d, v) { return costHasRuns(d, 'by', v); }}
};
function costFiltered(d) { var f = COSTS_FILTER && COST_FILTERS[COSTS_FILTER.kind]; return f ? f.runs(d, COSTS_FILTER.value) : null; }
function costShownRuns(d) { return costFiltered(d) || d.runs || []; }
function costGuard(s, d) {
  var c = s.source === 'guard' && s.categories && typeof s.categories === 'object' ? s.categories : null;
  var found = c ? COST_GUARD_CATEGORIES.filter(function (k) { return Number(c[k.id]) > 0; }) : [];
  if (!found.length) return '';
  var click = !!costObjRuns(d);
  return '<tr class="csrc-guard" data-guard-categories="1"><td class="cadv">' + esc(tx('objections')) + '</td><td class="cmodels" colspan="' + (COST_PARTS.length + 6) + '">' +
    found.map(function (k) {
      var text = esc(tx(k.label)) + ' ' + esc(Math.floor(Number(c[k.id])));
      return click ? costFilterBtn('objection', k.id, text, 'Show the runs with this objection') : text;
    }).join(' \u00b7 ') + '</td></tr>';
}
function costSources(d) {
  return '<div class="box"><table class="csrc"><thead><tr><th>Source</th><th class="num">Calls</th><th class="num">Runs</th><th class="num">Total</th>' +
    COST_PARTS.map(function (p) { return '<th class="num">' + esc(p.label) + '</th>'; }).join('') + '<th class="num">Cache hit</th><th class="num">Avg / run</th><th>Models</th></tr></thead><tbody>' +
    d.sources.map(function (s) {
      return '<tr data-source="' + esc(s.source) + '"><td><i class="sw s-' + esc(s.source) + '"></i>' + esc(costLabel(COST_SOURCES, s.source)) + '</td><td class="num">' + esc(s.calls) + '</td><td class="num">' + (s.runs > 0 && costHasRuns(d, 'source', s.source) ? costFilterBtn('source', s.source, esc(s.runs), 'Show the runs from this source') : esc(s.runs)) + '</td><td class="num"><b>' + esc(costUsd(s.usd.total)) + '</b></td>' +
        COST_PARTS.map(function (p) { return '<td class="num">' + esc(costUsd(s.usd[p.id])) + '</td>'; }).join('') +
        '<td class="num">' + esc(costPct(s.cacheHit)) + '</td><td class="num">' + esc(costUsd(s.avgRun)) + '</td><td class="cmodels">' + esc(Object.keys(s.models).map(function (m) { return costModel(m) + ' ' + s.models[m]; }).join(' · ') || '–') + '</td></tr>' + costAdvisor(s) + costGuard(s, d);
    }).join('') + '</tbody></table></div>';
}
function costCardName(key) {
  var card = CARDS.filter(function (a) { return a.key === key; })[0];
  if (card) return svcName(card) + ' / ' + card.label;
  var p = String(key).split('|');
  return p.length > 1 ? p.slice(1, 3).join(' / ') : String(key);
}
function costCardLink(key) {
  var live = CARDS.some(function (a) { return a.key === key; });
  return '<a' + (live ? '' : ' class="muted"') + ' href="#a=' + esc(linkKey(key)) + '" title="' + esc(key) + '">' + esc(costCardName(key)) + '</a>';
}
function costWhat(r) {
  var keys = r.keys || [];
  if (!keys.length || r.what !== keys.join(', ')) return esc(r.what);
  return keys.map(costCardLink).join(', ');
}
function costTop(d) {
  var cards = d.topCards.length ? d.topCards.map(function (c) { return '<tr><td class="ckey">' + costCardLink(c.key) + '</td><td class="num">' + esc(c.runs) + '</td><td class="num">' + esc(costUsd(c.usd)) + '</td></tr>'; }).join('') : '<tr class="empty"><td colspan="3">No card spend in this range.</td></tr>';
  var people = d.topAskers.length ? d.topAskers.map(function (p) { return '<tr><td>' + (costHasRuns(d, 'by', p.by) ? costFilterBtn('by', p.by, esc(costBy(p.by)), 'Show the runs by this person') : esc(costBy(p.by))) + '</td><td class="num">' + esc(p.runs) + '</td><td class="num">' + esc(p.calls) + '</td><td class="num">' + esc(costUsd(p.usd)) + '</td></tr>'; }).join('') : '<tr class="empty"><td colspan="4">Nobody asked in this range.</td></tr>';
  return '<div class="cpair"><section id="costs-cards"><h3 class="sub-h">Top cards</h3><div class="box"><table><thead><tr><th>Card</th><th class="num">Runs</th><th class="num">Spend</th></tr></thead><tbody>' + cards + '</tbody></table></div></section>' +
    '<section id="costs-people"><h3 class="sub-h">Top askers</h3><div class="box"><table><thead><tr><th>Who</th><th class="num">Runs</th><th class="num">Calls</th><th class="num">Spend</th></tr></thead><tbody>' + people + '</tbody></table></div></section></div>';
}
function costSplit(usd) {
  var parts = COST_PARTS.filter(function (p) { return usd[p.id] > 0; });
  var sum = parts.reduce(function (a, p) { return a + usd[p.id]; }, 0);
  var tip = COST_PARTS.map(function (p) { return p.label + ' ' + costUsd(usd[p.id]); }).join(' · ');
  return '<span class="csplit" title="' + esc(tip) + '">' + (sum > 0 ? parts.map(function (p) {
    return '<b class="p-' + p.id + '" style="width:' + (usd[p.id] / sum * 100).toFixed(1) + '%"></b>';
  }).join('') : '') + '</span>';
}
function costObjTag(r) {
  var v = COSTS_FILTER && COSTS_FILTER.kind === 'objection' ? COSTS_FILTER.value : null;
  var n = v && r.objections ? Math.floor(Number(r.objections[v])) : 0;
  return n > 0 ? ' <span class="tag plain cobj-n">' + esc(tx(costLabel(COST_GUARD_CATEGORIES, v))) + ' ' + esc(n) + '</span>' : '';
}
function costCsvCell(v) {
  var t = v == null ? '' : String(v);
  if (/^[=+\-@\t\r]/.test(t)) t = "'" + t;
  return '"' + t.replace(/"/g, '""') + '"';
}
function costCsv(list) {
  var head = ['Time', 'Source', 'Who', 'Card or question', 'Model', 'Effort', 'Tools', 'Seconds', 'Total USD'];
  return [head].concat(list.map(function (r) {
    return [r.at, costLabel(COST_SOURCES, r.source), costBy(r.by) || '', r.what, r.model ? costModel(r.model) : '', r.effort || '', r.toolCalls, r.seconds, r.usd && r.usd.total];
  })).map(function (row) { return row.map(costCsvCell).join(','); }).join('\r\n') + '\r\n';
}
function costCsvName(d) { var day = function (x) { return String(x || '').replace(/[^0-9-]/g, ''); }; return 'skopax-costs-runs-' + day(d.from) + '-' + day(d.to) + '.csv'; }
function costDownload(d) {
  var url = URL.createObjectURL(new Blob([costCsv(costShownRuns(d))], {type: 'text/csv;charset=utf-8'}));
  var a = document.createElement('a');
  a.href = url;
  a.download = costCsvName(d);
  a.hidden = true;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(function () { URL.revokeObjectURL(url); }, 0);
}
function costRuns(d) {
  var f = COSTS_FILTER && COST_FILTERS[COSTS_FILTER.kind];
  var objs = costFiltered(d);
  var list = objs || d.runs;
  var body = list.length ? list.map(function (r) {
    return '<tr data-run="' + esc(r.id) + '"><td class="when"><a class="run-link" href="/api/investigations/' + encodeURIComponent(r.id) + '" target="_blank" rel="noopener" data-json="Run record" title="Open the full run record">' + esc(dayhm(r.at)) + '</a></td>' +
      '<td>' + esc(costLabel(COST_SOURCES, r.source)) + (r.outcome && r.outcome !== 'done' ? ' <span class="tag plain">' + esc(r.outcome) + '</span>' : '') + costObjTag(r) + '</td><td>' + esc(costBy(r.by) || '–') + '</td>' +
      '<td class="cwhat" title="' + esc(r.what) + '">' + costWhat(r) + '</td><td class="cmodel">' + esc(costModel(r.model) + (r.effort ? ' · ' + r.effort : '')) + (r.advisorCalls > 0 ? ' · <span class="cadv"' + (r.advisorModel ? ' title="' + esc(costModel(r.advisorModel)) + '"' : '') + '>advisor</span> ' + esc(r.advisorCalls) : '') + '</td>' +
      '<td class="num">' + esc(r.toolCalls == null ? '–' : r.toolCalls) + '</td><td class="num">' + esc(r.seconds == null ? '–' : secs(r.seconds)) + '</td><td>' + costSplit(r.usd) + '</td><td class="num"><b>' + esc(costUsd(r.usd.total)) + '</b></td></tr>';
  }).join('') : '<tr class="empty"><td colspan="9">' + esc(f ? tx(f.empty) : 'No runs in this range.') + '</td></tr>';
  var csv = ' <button type="button" class="btn sec small ccsv"' + (list.length ? '' : ' disabled') + '>' + esc(tx('Download CSV')) + '</button>';
  var head = f ? esc(objs.length) + ' ' + esc(tx(objs.length === 1 ? f.one : f.many)) + ': ' + esc(f.label(COSTS_FILTER.value)) + ' <button type="button" class="btn sec small cobj-all">' + esc(tx('Show all runs')) + '</button>' + csv
    : 'Runs <span class="count">' + esc(d.runs.length) + '</span>' + csv;
  var capped = f && f.capped && d.runs.length >= COSTS_RUN_CAP ? '<p class="panel-note muted ccapped">' + esc(tx('Only the newest 100 runs are listed.')) + '</p>' : '';
  return '<section id="costs-runs"' + (f ? ' data-filter="' + esc(COSTS_FILTER.kind) + '"' : '') + '><h3 class="sub-h">' + head + '</h3>' + capped + costLegend(COST_PARTS, 'p') +
    '<div class="box"><table class="cruns"><thead><tr><th>Time</th><th>Source</th><th>Who</th><th>Card or question</th><th>Model · effort</th><th class="num">Tools</th><th class="num">Duration</th><th>Split</th><th class="num">Total</th></tr></thead><tbody>' + body + '</tbody></table></div></section>';
}
function renderCosts(d) {
  var box = document.getElementById('costs');
  if (!box) return;
  COSTS_LOCKED = false;
  if (COSTS_FILTER && !(COST_FILTERS[COSTS_FILTER.kind] && COST_FILTERS[COSTS_FILTER.kind].valid(d, COSTS_FILTER.value))) COSTS_FILTER = null;
  var hour = d.bucket === 'hour';
  var spend = function (r, id) { return r[id]; };
  var writes = function (r, id) { return r.writeUsd[id]; };
  var span = hour ? 'per hour, today' : 'per day, ' + d.days + ' days';
  var writeTotal = d.cache.reduce(function (a, r) { return a + COST_RUN_SOURCES.reduce(function (b, x) { return b + (r.writeUsd[x.id] || 0); }, 0); }, 0);
  box.innerHTML = costStrip(d) +
    '<section id="costs-spend"><h3 class="sub-h">' + (hour ? 'Spend per hour, today' : 'Spend per day') + '</h3>' + costLegend(COST_SOURCES, 's') + costBars(d.perBucket, COST_SOURCES, spend, hour, 'Spend ' + span + ' by source, ' + costUsd(d.total.usd)) + costTable(d.perBucket, COST_SOURCES, spend, costUsd, hour) + '</section>' +
    '<section id="costs-sources"><h3 class="sub-h">By source</h3>' + costSources(d) + '</section>' +
    '<div class="cpair"><section id="costs-hit"><h3 class="sub-h">Cache hit share</h3>' + costLegend(COST_RUN_SOURCES, 's') + costLine(d.cache, COST_RUN_SOURCES, hour, 'Cache hit share ' + span + ' by source') + costTable(d.cache, COST_RUN_SOURCES, function (r, id) { return r.hit[id]; }, costPct, hour) + '</section>' +
    '<section id="costs-writes"><h3 class="sub-h">Cache-write spend</h3>' + costLegend(COST_RUN_SOURCES, 's') + costBars(d.cache, COST_RUN_SOURCES, writes, hour, 'Cache-write spend ' + span + ' by source, ' + costUsd(writeTotal)) + costTable(d.cache, COST_RUN_SOURCES, writes, costUsd, hour) + '</section></div>' +
    '<p class="panel-note">The gate and small talk are not cached, so they are left out of the two cache charts.</p>' +
    costTop(d) + costRuns(d);
  var count = document.getElementById('count-costs');
  if (count) count.textContent = costUsd(d.today.spent);
  costObjWire(box, d);
}
function costFilterFind(box, f) {
  return f ? [].filter.call(box.querySelectorAll('.cobj'), function (b) { return b.dataset.filter === f.kind && b.dataset.value === f.value; })[0] : null;
}
function costObjWire(box, d) {
  [].forEach.call(box.querySelectorAll('.cobj'), function (b) {
    b.addEventListener('click', function () {
      var pick = {kind: b.dataset.filter, value: b.dataset.value};
      COSTS_FILTER = costFilterIs(pick.kind, pick.value) ? null : pick;
      var y = window.scrollY;
      renderCosts(d);
      window.scrollTo(0, y);
      var runs = document.getElementById('costs-runs'), all = runs && runs.querySelector('.cobj-all');
      if (all) { runs.scrollIntoView({block: 'start'}); all.focus({preventScroll: true}); }
      else { var again = costFilterFind(box, pick); if (again) again.focus({preventScroll: true}); }
    });
  });
  var all = box.querySelector('.cobj-all');
  if (all) all.addEventListener('click', function () {
    var was = COSTS_FILTER;
    COSTS_FILTER = null;
    renderCosts(d);
    var back = costFilterFind(box, was);
    if (back) back.focus();
  });
  var csv = box.querySelector('.ccsv');
  if (csv) csv.addEventListener('click', function () { costDownload(d); });
}
function costLocked(code) {
  var box = document.getElementById('costs');
  COSTS_LOCKED = true;
  box.innerHTML = '<p class="panel-note costs-locked">' + (code === 401 ? 'Log in to see costs.' : 'Costs are not open to this account.') + '</p>';
  document.getElementById('count-costs').textContent = '–';
}
function costFailed(box) {
  COSTS = null;
  box.innerHTML = '<p class="muted">Costs are not available right now.</p>';
}
function costsCount() {
  var count = document && document.getElementById('count-costs');
  var spent = INV_INFO && INV_INFO.spentToday;
  if (count && COSTS_OP && !COSTS_LOCKED && typeof spent === 'number') count.textContent = costUsd(spent);
}
function markCostsTab(loggedIn) {
  var tab = document.getElementById('tab-costs');
  tab.hidden = false;
  if (loggedIn) { tab.removeAttribute('aria-disabled'); tab.removeAttribute('title'); return; }
  tab.setAttribute('aria-disabled', 'true');
  tab.title = 'Log in to see costs';
  var count = document.getElementById('count-costs');
  if (count) count.textContent = '–';
}
function setCostsAccess(loggedIn) {
  COSTS_OP = !!loggedIn;
  if (typeof refreshPodUsage === 'function') refreshPodUsage();
  markCostsTab(COSTS_OP);
  if (!COSTS_OP) {
    COSTS = null;
    if (curTab === 'costs') showTab('anoms');
    return;
  }
  costsCount();
  if (curTab === 'costs') loadCosts();
}
function markCostRange() {
  document.querySelectorAll('#costs-range button').forEach(function (b) { b.setAttribute('aria-pressed', String(Number(b.dataset.days) === COSTS_DAYS)); });
}
function loadCosts() {
  var box = document.getElementById('costs');
  if (!box || !COSTS_OP) return;
  var seq = ++COSTS_SEQ;
  markCostRange();
  box.classList.add('loading');
  fetch('/api/costs?days=' + COSTS_DAYS, {credentials: 'same-origin'}).then(function (r) {
    if (r.status === 401 || r.status === 403) return {locked: r.status};
    return r.ok ? r.json() : null;
  }).then(function (d) {
    if (seq !== COSTS_SEQ) return;
    box.classList.remove('loading');
    if (d && d.locked) { COSTS = null; costLocked(d.locked); return; }
    if (!d || !Array.isArray(d.perBucket)) { costFailed(box); return; }
    COSTS = d;
    renderCosts(d);
  }, function () {
    if (seq !== COSTS_SEQ) return;
    box.classList.remove('loading');
    costFailed(box);
  });
}
document.querySelectorAll('#costs-range button').forEach(function (b) {
  b.addEventListener('click', function () {
    COSTS_DAYS = Number(b.dataset.days);
    save('skopax-costs-days', String(COSTS_DAYS));
    loadCosts();
  });
});
markCostsTab(false);
markCostRange();
