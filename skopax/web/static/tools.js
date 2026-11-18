var TOOL_RANGES = [1, 7, 30];
var TOOL_KINDS = {read: 'read', write: 'write', propose: 'proposal', gone: 'removed', mcp: 'MCP server'};
var TOOL_TRIGGERS = {check: 'Checks', cards: 'Cards', action: 'Investigate', ask: 'Page asks', slack: 'Slack asks', chat: 'Chat', assess: 'Assessments', other: 'Other'};
var TOOL_COLUMNS = [
  {id: 'name', label: 'Tool'}, {id: 'calls', label: 'Calls', num: true}, {id: 'runs', label: 'Runs', num: true}, {id: 'errors', label: 'Errors', num: true},
  {id: 'errorRate', label: 'Error rate', num: true}, {id: 'p50Ms', label: 'p50', num: true}, {id: 'p95Ms', label: 'p95', num: true}, {id: 'maxMs', label: 'Max', num: true},
  {id: 'lastAt', label: 'Last used'}, {id: 'byTrigger', label: 'By run type', fixed: true}];
var TOOLS = null;
var TOOLS_SEQ = 0;
var TOOLS_SORT = {id: 'calls', desc: true};
var TOOLS_OPEN = {};
var TOOLS_FIND = '';
var TOOLS_DAYS = (function () { var d = parseInt(load('skopax-tools-days'), 10); return TOOL_RANGES.indexOf(d) >= 0 ? d : 7; })();
function toolMs(v) { return v == null ? '–' : v >= 10000 ? Math.round(v / 1000) + ' s' : v >= 1000 ? (v / 1000).toFixed(1) + ' s' : v + ' ms'; }
function toolRate(t) {
  if (t.errorRate == null) return '<td class="num">–</td>';
  if (!t.errors) return '<td class="num">0 %</td>';
  var c = t.errorRate >= 0.1 ? 'var(--crit)' : 'var(--warn)';
  return '<td class="num hot" style="--c: ' + c + '">' + esc(String(Math.round(t.errorRate * 1000) / 10)) + ' %</td>';
}
function toolTriggers(t) {
  var parts = Object.keys(t.byTrigger || {}).sort(function (a, b) { return t.byTrigger[b] - t.byTrigger[a]; })
    .map(function (k) { return esc(TOOL_TRIGGERS[k] || k) + ' ' + esc(String(t.byTrigger[k])); });
  return parts.length ? parts.join(' · ') : '–';
}
function toolSorted(list) {
  var s = TOOLS_SORT, used = list.filter(function (t) { return t.calls; }), unused = list.filter(function (t) { return !t.calls; });
  used.sort(function (a, b) {
    var x = a[s.id], y = b[s.id];
    if (x == null && y == null) return a.name < b.name ? -1 : 1;
    if (x == null) return 1;
    if (y == null) return -1;
    var r = typeof x === 'string' ? (x < y ? -1 : x > y ? 1 : 0) : x - y;
    return (s.desc ? -r : r) || (a.name < b.name ? -1 : 1);
  });
  return used.concat(unused);
}
function toolMore(t) {
  var errors = (t.recentErrors || []).map(function (e) {
    var when = e.runId ? '<a class="run-link" href="/api/investigations/' + encodeURIComponent(e.runId) + '" target="_blank" rel="noopener" data-json="Run record" title="Open the full run record">' + esc(dayhmZone(e.at)) + '</a>' : esc(dayhmZone(e.at));
    return '<li>' + when + ' · ' + esc(TOOL_TRIGGERS[e.trigger] || e.trigger) + (e.check ? ' · ' + esc(e.check) : '') + ' · <code>' + esc(e.error) + '</code></li>';
  }).join('');
  var trunc = t.truncated ? '<p class="muted">' + esc(plural(t.truncated, 'call', 'calls')) + ' returned more rows than the tool hands back and were cut.</p>' : '';
  var head = t.errors ? 'Latest errors (' + Math.min(t.errors, (t.recentErrors || []).length) + ' of ' + t.errors + ')' : 'No errors in this window.';
  var what = t.description ? '<p class="sub-h">What the investigator is told</p><p class="tdesc">' + esc(t.description) + '</p>' : '';
  var by = t.byCluster || {}, where = Object.keys(by).map(function (n) { return n + ': ' + plural(by[n].calls, 'call', 'calls') + (by[n].errors ? ', ' + plural(by[n].errors, 'error', 'errors') : ''); });
  var clusters = where.length ? '<p class="sub-h">By cluster</p><p class="tdesc">' + esc(where.join(' · ')) + '</p>' : '';
  var bc = t.byCheck || {}, checks = Object.keys(bc).sort(function (a, b) { return a < b ? -1 : a > b ? 1 : 0; }).map(function (n) { return n + ': ' + plural(bc[n].runs, 'run', 'runs') + (bc[n].errors ? ', ' + plural(bc[n].errors, 'error', 'errors') : ''); });
  var byCheck = checks.length ? '<p class="sub-h">Scheduled checks on the same source</p><p class="tdesc">' + esc(checks.join(' · ')) + '</p>' : '';
  var s = t.status, why = s && s.state !== 'on' && s.reason ? '<p class="sub-h">' + esc(s.state === 'off' ? 'Off' : 'Unreachable') + '</p><p class="tdesc tstate-why">' + esc(toolStateTip(s)) + '</p>' : '';
  return why + what + clusters + byCheck + '<p class="sub-h">' + esc(head) + '</p>' + (errors ? '<ul>' + errors + '</ul>' : '') + trunc;
}
function toolClusters(t) {
  var by = t.byCluster || {}, names = Object.keys(by).sort(function (a, b) { return (isPrimaryName(a) ? 0 : 1) - (isPrimaryName(b) ? 0 : 1) || (a < b ? -1 : a > b ? 1 : 0); });
  if (!names.length) return '';
  var tip = names.map(function (n) { return n + ': ' + plural(by[n].calls, 'call', 'calls') + (by[n].errors ? ', ' + plural(by[n].errors, 'error', 'errors') : ''); }).join('\n');
  return '<span class="tclusters" title="' + esc(tip) + '">' + names.map(function (n) { return esc(n) + ' ' + esc(String(by[n].calls)) + (by[n].errors ? '<span class="terr"> (' + esc(String(by[n].errors)) + ' err)</span>' : ''); }).join(' · ') + '</span>';
}
function toolStateTip(s) { return (s.reason || '') + (s.at ? ' (checked ' + dayhmZone(s.at) + ')' : ''); }
function toolState(t) {
  var s = t.status;
  if (!s || s.state === 'on') return '';
  var label = s.state === 'off' ? 'off' : 'unreachable';
  return '<span class="tstate tstate-' + esc(s.state) + '" title="' + esc(toolStateTip(s)) + '">' + esc(label) + '</span>';
}
function toolRow(t) {
  var open = !!TOOLS_OPEN[t.name];
  var last = t.lastAt ? '<span title="' + esc(ago(t.lastAt)) + '">' + esc(dayhmZone(t.lastAt)) + '</span>' : '–';
  var row = '<tr class="tool-row' + (t.calls ? '' : ' tool-unused') + '" data-tool="' + esc(t.name) + '" tabindex="0" aria-expanded="' + open + '">' +
    '<td><code>' + esc(t.name) + '</code><span class="tkind">' + esc(TOOL_KINDS[t.kind] || t.kind) + '</span>' + toolState(t) + toolClusters(t) + '</td>' +
    '<td class="num">' + esc(String(t.calls)) + '</td><td class="num">' + esc(String(t.runs)) + '</td><td class="num">' + esc(String(t.errors)) + '</td>' + toolRate(t) +
    '<td class="num">' + esc(toolMs(t.p50Ms)) + '</td><td class="num">' + esc(toolMs(t.p95Ms)) + '</td><td class="num">' + esc(toolMs(t.maxMs)) + '</td>' +
    '<td class="when">' + last + '</td><td class="ttrig">' + toolTriggers(t) + '</td></tr>';
  return row + '<tr class="tool-more"' + (open ? '' : ' hidden') + '><td colspan="' + TOOL_COLUMNS.length + '">' + toolMore(t) + '</td></tr>';
}
function toolHead() {
  return '<tr>' + TOOL_COLUMNS.map(function (c) {
    if (c.fixed) return '<th>' + esc(c.label) + '</th>';
    var on = TOOLS_SORT.id === c.id;
    var sort = on ? (TOOLS_SORT.desc ? 'descending' : 'ascending') : 'none';
    return '<th class="' + (c.num ? 'num' : '') + '" aria-sort="' + sort + '"><button type="button" data-sort="' + c.id + '">' + esc(c.label) + (on ? '<span aria-hidden="true">' + (TOOLS_SORT.desc ? ' \u2193' : ' \u2191') + '</span>' : '') + '</button></th>';
  }).join('') + '</tr>';
}
function toolStrip(d) {
  var slow = d.tools.filter(function (t) { return t.p95Ms != null; }).sort(function (a, b) { return b.p95Ms - a.p95Ms; })[0];
  var rate = d.calls ? Math.round(d.errors / d.calls * 1000) / 10 + ' % of calls' : '';
  return '<div class="cstrip">' +
    costTile(d.days === 1 ? 'Calls today' : 'Calls, last ' + d.days + ' days', String(d.calls), 'in ' + plural(d.runsWithTools, 'run', 'runs') + ' of ' + d.runs + (d.checkRuns ? ' + ' + plural(d.checkRuns, 'check run', 'check runs') : '')) +
    costTile('Errors', String(d.errors), rate) +
    costTile('Tools used', d.toolsUsed + ' of ' + d.toolsDefined, d.toolsDefined - d.toolsUsed ? (d.toolsDefined - d.toolsUsed) + ' never called' : 'every tool called') +
    costTile('Slowest p95', slow ? slow.name : '–', slow ? toolMs(slow.p95Ms) : '') + '</div>';
}
function toolMatches(t, q) {
  if (!q) return true;
  var hay = [t.name, t.description || '', TOOL_KINDS[t.kind] || t.kind || ''].concat(Object.keys(t.byCheck || {})).join(' ');
  return foldCase(hay).indexOf(q) >= 0;
}
function renderTools(d) {
  var box = document.getElementById('tools');
  if (!box) return;
  var q = foldCase(TOOLS_FIND.trim()), shown = d.tools.filter(function (t) { return toolMatches(t, q); });
  var rows = shown.length ? toolSorted(shown).map(toolRow).join('') : '<tr><td class="muted" colspan="' + TOOL_COLUMNS.length + '">No tool matches “' + esc(TOOLS_FIND.trim()) + '”.</td></tr>';
  var note = document.getElementById('tools-shown');
  if (note) note.textContent = q ? shown.length + ' of ' + d.tools.length : '';
  box.innerHTML = toolStrip(d) + '<div class="box"><table class="ttable"><thead>' + toolHead() + '</thead><tbody>' + rows + '</tbody></table></div>';
  var count = document.getElementById('count-tools');
  if (count) count.textContent = String(d.toolsUsed);
}
function markToolRange() {
  document.querySelectorAll('#tools-range button').forEach(function (b) { b.setAttribute('aria-pressed', String(Number(b.dataset.days) === TOOLS_DAYS)); });
}
function loadTools() {
  var box = document.getElementById('tools');
  if (!box) return;
  var seq = ++TOOLS_SEQ;
  markToolRange();
  box.classList.add('loading');
  fetch('/api/tool-usage?days=' + TOOLS_DAYS, {credentials: 'same-origin'}).then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
    if (seq !== TOOLS_SEQ) return;
    box.classList.remove('loading');
    if (!d || !Array.isArray(d.tools)) { box.innerHTML = '<p class="muted">Tool use is unavailable right now.</p>'; return; }
    TOOLS = d;
    renderTools(d);
  }, function () {
    if (seq !== TOOLS_SEQ) return;
    box.classList.remove('loading');
    box.innerHTML = '<p class="muted">Tool use is unavailable right now.</p>';
  });
}
function toggleToolRow(row) {
  var name = row.dataset.tool;
  TOOLS_OPEN[name] = !TOOLS_OPEN[name];
  row.setAttribute('aria-expanded', String(TOOLS_OPEN[name]));
  row.nextElementSibling.hidden = !TOOLS_OPEN[name];
}
document.getElementById('tools').addEventListener('click', function (e) {
  var sort = e.target.closest('button[data-sort]');
  if (sort) {
    var id = sort.dataset.sort;
    TOOLS_SORT = TOOLS_SORT.id === id ? {id: id, desc: !TOOLS_SORT.desc} : {id: id, desc: id !== 'name'};
    if (TOOLS) renderTools(TOOLS);
    return;
  }
  if (e.target.closest('a')) return;
  var row = e.target.closest('tr.tool-row');
  if (row) toggleToolRow(row);
});
document.getElementById('tools').addEventListener('keydown', function (e) {
  var row = e.target.closest('tr.tool-row');
  if (row && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); toggleToolRow(row); }
});
document.querySelectorAll('#tools-range button').forEach(function (b) {
  b.addEventListener('click', function () {
    TOOLS_DAYS = Number(b.dataset.days);
    save('skopax-tools-days', String(TOOLS_DAYS));
    loadTools();
  });
});
var toolsFind = document.getElementById('tools-find');
if (toolsFind) toolsFind.addEventListener('input', function () { TOOLS_FIND = toolsFind.value; if (TOOLS) renderTools(TOOLS); });
markToolRange();
var MCP_SEQ = 0;
function mcpCommand(info) {
  return 'claude mcp add --scope user --transport http --client-id ' + (info.clientId || 'skopax-mcp') + ' ' + brandSlug() + ' ' + (info.url || location.origin + '/mcp');
}
function mcpStateHtml(info) {
  var off = info.on === false, ready = !off && info.enabled;
  var pill = off ? 'Off' : ready ? 'On' : 'Not set up yet';
  var note = off ? 'The MCP server is turned off in Admin \u203a Features; it refuses every call until an admin turns it back on.' : ready ? 'Server address ' + info.url : 'MCP answers once single sign-on is set up and on (Admin \u203a Access & sign-in).';
  return '<p class="mcp-state"><span class="st ' + (ready ? 'k-ok' : 'k-warn') + '">' + esc(pill) + '</span><span class="muted">' + esc(note) + '</span></p>';
}
function mcpInfoHtml(info) {
  var tools = info.tools || [];
  var row = function (t) { return '<tr><td>' + esc(t.name) + '</td><td>' + esc(t.what) + '</td><td>' + (t.readOnly ? 'read' : 'change <span class="tag plain mcp-op">operators and admins</span>') + '</td></tr>'; };
  return '<p class="mcp-lead">Use ' + esc(brandName()) + ' from an MCP client such as Claude Code, with your own login. Viewers get the read tools: cards, investigations, deploy reports, asks and notifications. ' +
    'Operators and admins also run investigations, ask the investigator, and take, resolve, mute and assign cards; those tools are marked below.</p>' +
    mcpStateHtml(info) +
    '<section><h3>Add it to your MCP client</h3><ol>' +
      '<li><b>Claude Code, by hand.</b> Run this once, then <code>/mcp</code> › ' + esc(brandSlug()) + ' › Authenticate:' +
        '<div class="mcp-cmd"><pre id="mcp-cmd">' + esc(mcpCommand(info)) + '</pre><button type="button" class="btn sec small" id="mcp-copy">Copy</button></div></li>' +
      '<li><b>Another client.</b> Add a streamable HTTP server at <code>' + esc(info.url || location.origin + '/mcp') + '</code> with OAuth client id <code>' + esc(info.clientId || 'skopax-mcp') + '</code> (PKCE, no secret). It works only when the client logs in with that fixed client id and a callback on your own machine (localhost, any port); a client that registers its own login app or returns to an app link such as cursor:// cannot log in yet. Only Claude Code is tested.</li>' +
    '</ol></section>' +
    '<section><h3>Log in</h3><p>A browser opens: sign in with your account. Your client keeps the token and renews it. Clients that connect from a vendor’s cloud, such as claude.ai and the Claude Desktop connectors, cannot reach ' + esc(brandName()) + '.</p></section>' +
    '<section><h3>What it can do</h3>' + (tools.length ? '<div class="mcp-tools-box"><table class="mcp-tools"><thead><tr><th>Tool</th><th>What it does</th><th></th></tr></thead><tbody>' + tools.map(row).join('') + '</tbody></table></div>' : '<p class="muted">No tools listed.</p>') + '</section>' +
    '<section><h3>Limits</h3><ul>' +
      '<li>Questions are private chats: they show on your Chat page and count toward your chat messages per day.</li>' +
      '<li>Investigate: ' + esc(String(info.diveCap)) + ' an hour per person, one open investigation per card.</li>' +
      '<li>A call waits up to ' + esc(String(info.waitSeconds)) + ' s for the answer; a longer one hands back an id that get_ask or get_investigation reads.</li>' +
      '<li>Known noise is not offered: it silences a card for good, so it stays a page and Slack button a person confirms.</li>' +
      '<li>Every change is recorded under your name and marked MCP; its cost shows as MCP on Costs.</li>' +
    '</ul></section>';
}
async function loadMcpInfo() {
  var box = document.getElementById('mcp-info');
  if (!box) return;
  var seq = ++MCP_SEQ;
  try {
    var r = await fetch('/api/mcp/info', {credentials: 'same-origin', headers: {'Accept': 'application/json'}});
    if (!r.ok) throw new Error(String(r.status));
    var info = await r.json();
    if (seq !== MCP_SEQ) return;
    box.innerHTML = mcpInfoHtml(info);
    var copy = document.getElementById('mcp-copy');
    copy.addEventListener('click', function () {
      var text = document.getElementById('mcp-cmd').textContent;
      var done = function () { copy.textContent = 'Copied'; setTimeout(function () { copy.textContent = 'Copy'; }, 2000); };
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, function () { copy.textContent = 'Select the command to copy'; });
      else copy.textContent = 'Select the command to copy';
    });
  } catch (e) {
    if (seq === MCP_SEQ) box.innerHTML = '<p class="muted">The connection details did not load; open the tab again.</p>';
  }
}
