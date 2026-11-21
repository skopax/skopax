(function () {
  var TAB = 'nodes', PERIOD_MIN = 5, DASH = '\u2013', DOT = ' \u00b7 ';
  var state = null, section = null, open = Object.create(null);
  function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
  function hm(iso) {
    var d = iso ? new Date(iso) : null;
    return d && !isNaN(d) ? d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hour12: false}) : '';
  }
  function ago(iso) {
    var m = Math.round((Date.now() - new Date(iso)) / 60000);
    if (!(m >= 0)) return '';
    return m < 60 ? m + ' min ago' : m < 1440 ? Math.floor(m / 60) + ' h ' + (m % 60) + ' min ago' : Math.floor(m / 1440) + ' d ago';
  }
  function list(rows, plugin) {
    return (Array.isArray(rows) ? rows : []).filter(function (r) { return r && typeof r === 'object' && r.plugin === plugin; });
  }
  function names(v) { return Array.isArray(v) ? v.map(String) : []; }
  function age(iso) {
    var m = Math.floor((Date.now() - new Date(iso)) / 60000);
    if (!(m >= 0)) return DASH;
    return m < 60 ? m + 'm' : m < 1440 ? Math.floor(m / 60) + 'h' : Math.floor(m / 1440) + 'd';
  }
  function waiting(n) { return n.role === 'agent' && n.status !== 'Ready' && !n.lastSeen && !n.seenBefore; }
  function ownVersion() { return typeof BRAND !== 'undefined' && BRAND && BRAND.version ? String(BRAND.version) : ''; }
  function tag(ok, text, title, cls) {
    return '<span class="tag ' + (cls || (ok ? 'k-ok' : 'k-crit')) + '"' + (title ? ' title="' + esc(title) + '"' : '') + '>' + esc(text) + '</span>';
  }
  function clusterCell(n) {
    if (n.clusterError) return '<span class="tag k-warn" title="' + esc(n.clusterError) + '">cluster not read</span>';
    var c = n.clusterSummary;
    if (!c || typeof c !== 'object') return '<span class="muted">' + DASH + '</span>';
    var notReady = names(c.notReady), pressure = names(c.pressure), cordoned = names(c.unschedulable);
    var bits = ['<span class="node-ready">' + esc(c.ready + '/' + c.total) + ' ready</span>'];
    if (notReady.length) bits.push('<span class="tag k-crit" title="' + esc(notReady.join('\n')) + '">' + esc(notReady.length + ' not ready') + '</span>');
    if (pressure.length) bits.push('<span class="tag k-warn" title="' + esc(pressure.join('\n')) + '">' + esc(pressure.length + ' under pressure') + '</span>');
    if (cordoned.length) bits.push('<span class="tag k-info" title="' + esc(cordoned.join('\n')) + '">' + esc(cordoned.length + ' cordoned') + '</span>');
    if (n.stale) bits.push('<span class="tag plain" title="' + esc('From the last heartbeat that answered, ' + (n.lastSeen ? ago(n.lastSeen) : '')) + '">stale</span>');
    return bits.join(' ');
  }
  function clusterRow(r) {
    var ready = r.ready === 'True', taints = names(r.taints), pressure = names(r.pressure), roles = names(r.roles);
    var status = (ready ? 'Ready' : 'NotReady') + (r.unschedulable ? ',SchedulingDisabled' : '');
    return '<tr><td><code>' + esc(r.name) + '</code></td><td>' + tag(ready && !r.unschedulable, status, taints.length ? 'Taints: ' + taints.join(', ') : '') + '</td>' +
      '<td title="' + esc(r.readySince || '') + '">' + esc(r.readySince ? hm(r.readySince) + DOT + age(r.readySince) : DASH) + '</td>' +
      '<td>' + (pressure.length ? '<span class="tag k-warn">' + esc(pressure.join(', ')) + '</span>' : '<span class="muted">' + DASH + '</span>') + '</td>' +
      '<td>' + esc(roles.join(', ') || DASH) + '</td><td><code>' + esc(r.kubelet || DASH) + '</code></td><td>' + esc(r.created ? age(r.created) : DASH) + '</td></tr>';
  }
  function relayTag(n) {
    var relays = names(n.relaysFor);
    if (n.relay) return ' <span class="tag node-relay" title="' + esc('Reached through the ' + n.relay + ' agent, which forwards each call with this agent\'s own token') + '">' + esc('through ' + n.relay) + '</span>';
    if (relays.length) return ' <span class="tag node-relay" title="' + esc('Forwards calls to ' + relays.join(', ')) + '">relay</span>';
    return '';
  }
  function versionCell(n) {
    var version = n.via === 'in-cluster' ? ownVersion() : n.version;
    return '<code' + (n.release ? ' title="' + esc('Release ' + n.release) + '"' : '') + '>' + esc(version || DASH) + '</code>';
  }
  function more(n) {
    var parts = [], relays = names(n.relaysFor);
    if (n.error) parts.push('<p class="node-err">' + esc(n.error) + '</p>');
    if (n.clusterError) parts.push('<p class="node-err">' + esc(n.clusterError) + '</p>');
    var how = n.via === 'in-cluster' ? 'Reads its own cluster in-cluster' : 'Reached through ' + (n.via || DASH) + (n.relay ? ' by way of the ' + n.relay + ' agent (relay)' : '');
    if (relays.length) parts.push('<p class="muted">' + esc('Relays calls to ' + relays.join(', ') + ', which cannot be reached directly.') + '</p>');
    parts.push('<p class="muted">' + esc(how + (n.latencyMs != null ? DOT + 'heartbeat ' + n.latencyMs + ' ms' : '') + DOT + n.status + ' since ' + (n.since ? hm(n.since) : DASH) +
      (n.startedAt ? DOT + 'started ' + hm(n.startedAt) + ' (' + age(n.startedAt) + ')' : '')) + '</p>');
    var rows = n.clusterSummary && Array.isArray(n.clusterSummary.nodes) ? n.clusterSummary.nodes.filter(function (r) { return r && typeof r === 'object'; }) : [];
    if (rows.length) {
      parts.push('<table class="ctable"><thead><tr><th>Cluster node</th><th>Status</th><th>Since</th><th>Pressure</th><th>Roles</th><th>Kubelet</th><th>Age</th></tr></thead><tbody>' +
        rows.map(clusterRow).join('') + '</tbody></table>');
    }
    return parts.join('');
  }
  function admin() { return typeof ADMIN_OK !== 'undefined' && !!ADMIN_OK; }
  var asked = false;
  function known(name) {
    if (typeof intFind !== 'function' || typeof INTEG === 'undefined' || !INTEG) return false;
    var p = intFind('kubernetes');
    return !!p && (p.instances || []).some(function (i) { return i && i.name === name; });
  }
  function ensureCatalog() {
    if (asked || !admin() || typeof loadIntegrations !== 'function' || (typeof INTEG !== 'undefined' && INTEG)) return;
    asked = true;
    loadIntegrations().then(function () { draw(); });
  }
  function editCell(n) { return admin() ? '<td>' + (known(n.name) ? '<button type="button" class="btn sec small node-edit" data-edit="' + esc(n.name) + '">Edit</button>' : '') + '</td>' : ''; }
  function cols() { return admin() ? 8 : 7; }
  function nodeRow(n) {
    var shown = !!open[n.name];
    var status = waiting(n) ? tag(false, 'Waiting for first contact', 'Added but it has never answered: no card for the first 24 h (time to install it), then one warning card' + (n.error ? '\n' + n.error : ''), 'k-muted')
      : tag(n.status === 'Ready', n.status, n.status + ' since ' + (n.since ? hm(n.since) : DASH) + (n.error ? '\n' + n.error : ''));
    return '<tr class="node-row" tabindex="0" aria-expanded="' + shown + '" data-node="' + esc(n.name) + '">' +
      '<td><b>' + esc(n.name) + '</b>' + relayTag(n) + '</td><td>' + esc(n.role) + '</td><td>' + status + '</td>' +
      '<td>' + esc(n.startedAt ? age(n.startedAt) : DASH) + '</td><td>' + versionCell(n) + '</td>' +
      '<td title="' + esc(n.lastSeen || '') + '">' + esc(n.lastSeen ? ago(n.lastSeen) || 'just now' : n.seenBefore ? DASH : 'never') + '</td>' +
      '<td>' + clusterCell(n) + '</td>' + editCell(n) + '</tr>' +
      '<tr class="node-more"' + (shown ? '' : ' hidden') + '><td colspan="' + cols() + '">' + more(n) + '</td></tr>';
  }
  function browserRow(b) {
    var label = 'Browser' + (b.cluster ? ' ' + b.cluster : '');
    var tip = (b.ok ? 'Answered ' : 'Not answering since ') + (b.lastSeen ? hm(b.lastSeen) : DASH) + (b.error ? '\n' + b.error : '');
    return '<tr class="node-browser" data-browser="' + esc(b.cluster || b.name || '') + '"><td><b>' + esc(label) + '</b></td><td>browser</td>' +
      '<td>' + tag(!!b.ok, b.ok ? 'reachable' : 'not reachable', tip) + '</td><td>' + DASH + '</td><td><code>' + esc(b.version || DASH) + '</code></td>' +
      '<td title="' + esc(b.lastSeen || '') + '">' + esc(b.lastSeen ? ago(b.lastSeen) || 'just now' : 'never') + '</td><td><span class="muted">' + DASH + '</span></td>' + (admin() ? '<td></td>' : '') + '</tr>';
  }
  function tools() { return admin() ? '<p class="node-tools"><button type="button" class="btn small node-add">+ Add node</button></p>' : ''; }
  function count(rows) {
    var box = document.getElementById('count-' + TAB);
    if (!box) return;
    var seen = rows.filter(function (n) { return !waiting(n); }), ready = seen.filter(function (n) { return n.status === 'Ready'; }).length;
    var down = seen.filter(function (n) { return n.status !== 'Ready'; }).map(function (n) { return n.name; });
    var later = rows.filter(waiting).map(function (n) { return n.name; });
    box.textContent = seen.length ? ready + '/' + seen.length : DASH;
    box.classList.toggle('k-crit', down.length > 0);
    box.classList.toggle('k-ok', seen.length > 0 && !down.length);
    box.title = (!seen.length ? 'no heartbeat yet' : down.length ? 'NotReady: ' + down.join(', ') : 'every node answered its last heartbeat') + (later.length ? DOT + 'waiting for first contact: ' + later.join(', ') : '');
  }
  function newest(rows) {
    return rows.map(function (n) { return n.lastSeen; }).filter(Boolean).sort().pop();
  }
  function draw() {
    if (!state || !section) return;
    ensureCatalog();
    var rows = list(state.nodes, 'kubernetes').filter(function (n) { return typeof n.name === 'string' && n.name; });
    var browsers = list(state.browser, 'website');
    count(rows);
    if (!rows.length && !browsers.length) {
      section.innerHTML = tools() + '<p class="muted">No heartbeat yet: the node check runs every ' + PERIOD_MIN + ' min.</p>';
      return;
    }
    var at = newest(rows), focused = document.activeElement && section.contains(document.activeElement) && document.activeElement.dataset ? document.activeElement.dataset.node : null;
    section.innerHTML = tools() + '<p class="muted node-at">' + esc('Last heartbeat ' + (at ? hm(at) + ' (' + (ago(at) || 'just now') + ')' : DASH) + DOT + 'every ' + PERIOD_MIN + ' min') + '</p>' +
      '<div class="box"><table class="ntable"><thead><tr><th>Name</th><th>Role</th><th>Status</th><th>Age</th><th>Version</th><th>Last heartbeat</th><th>Cluster nodes</th>' + (admin() ? '<th></th>' : '') + '</tr></thead><tbody>' +
      rows.map(nodeRow).join('') + browsers.map(browserRow).join('') + '</tbody></table></div>';
    var back = focused ? [].filter.call(section.querySelectorAll('tr.node-row'), function (r) { return r.dataset.node === focused; })[0] : null;
    if (back) back.focus();
  }
  function toggle(row) {
    var name = row.dataset.node;
    open[name] = !open[name];
    row.setAttribute('aria-expanded', String(open[name]));
    row.nextElementSibling.hidden = !open[name];
  }
  async function openForm(name) {
    if (typeof intOpen !== 'function' || typeof loadIntegrations !== 'function') return;
    if (typeof INTEG === 'undefined' || !INTEG) { if (!(await loadIntegrations())) return; }
    if (typeof intFind !== 'function' || !intFind('kubernetes')) return;
    intOpen('kubernetes', name || null, {});
  }
  function wire(box) {
    box.addEventListener('click', function (e) {
      var edit = e.target.closest('.node-edit'), add = e.target.closest('.node-add');
      if (edit || add) { e.stopPropagation(); openForm(edit ? edit.dataset.edit : null); return; }
      if (e.target.closest('a')) return;
      var row = e.target.closest('tr.node-row');
      if (row) toggle(row);
    });
    box.addEventListener('keydown', function (e) {
      var row = e.target.closest('tr.node-row');
      if (e.target.closest('button')) return;
      if (row && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); toggle(row); }
    });
  }
  document.addEventListener('skopax:admin', function () { draw(); });
  document.addEventListener('skopax:state', function (e) { state = e.detail && e.detail.state; draw(); });
  document.addEventListener('skopax:page', function (e) {
    if (!e.detail || e.detail.tab !== TAB || !e.detail.section) return;
    section = e.detail.section;
    wire(section);
    draw();
  });
})();
