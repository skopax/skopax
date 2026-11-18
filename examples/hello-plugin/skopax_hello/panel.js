(function () {
  var TAB = 'hello';
  var state = null, section = null;

  function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }

  function render() {
    if (!section) return;
    var rows = state && Array.isArray(state[TAB]) ? state[TAB] : [];
    var open = ((state && state.anomalies) || []).filter(function (a) { return a && a.src === TAB && a.status === 'open' && !a.muted && a.triage !== 'progress' && a.triage !== 'handled'; });
    var html = '<h3 class="sec-t">Hello</h3><p>' + (open.length ? esc(open.length) + ' open card(s)' : 'No open cards.') + '</p>';
    html += rows.length ? '<table><thead><tr><th>Address</th><th>Answer</th><th>Asked</th></tr></thead><tbody>' + rows.map(function (r) {
      return '<tr><td>' + esc(r.url) + '</td><td>' + esc(r.said) + '</td><td>' + esc(r.at) + '</td></tr>';
    }).join('') + '</tbody></table>' : '<p>No pass yet. The Hello check fills this on its next run.</p>';
    section.innerHTML = html;
  }

  document.addEventListener('skopax:state', function (e) { state = e.detail && e.detail.state; render(); });
  document.addEventListener('skopax:page', function (e) {
    if (!e.detail || e.detail.tab !== TAB) return;
    section = e.detail.section;
    render();
  });
})();
