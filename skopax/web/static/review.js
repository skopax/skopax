var REVIEW_SEQ = 0;
function reviewLine(line) {
  var text = esc(line).replace(/\*\*([^*]+)\*\*|\*([^*]+)\*/g, function (m, a, b) { return '<strong>' + (a || b) + '</strong>'; }).replace(/`([^`]+)`/g, '<code>$1</code>');
  var bullet = /^\s*[-•]\s+/.test(line);
  return bullet ? '<li>' + text.replace(/^\s*[-•]\s+/, '') + '</li>' : (line.trim() ? '<p>' + text + '</p>' : '');
}
function reviewBody(text) {
  var out = [], list = [];
  String(text || '').split('\n').forEach(function (line) {
    if (/^\s*[-•]\s+/.test(line)) { list.push(reviewLine(line)); return; }
    if (list.length) { out.push('<ul>' + list.join('') + '</ul>'); list = []; }
    out.push(reviewLine(line));
  });
  if (list.length) out.push('<ul>' + list.join('') + '</ul>');
  return out.join('');
}
function renderReview(d) {
  var box = document.getElementById('review');
  if (!box) return;
  var rows = (d && d.reviews) || [];
  if (!rows.length) { box.innerHTML = '<p class="muted">No self-review yet. A Claude session writes one each morning.</p>'; return; }
  box.innerHTML = rows.map(function (r, i) {
    var when = dayhmZone(r.at);
    var dm = r.dm && r.dm.length ? ' · DM ' + esc(r.dm.join(', ')) : '';
    return '<details class="box review"' + (i === 0 ? ' open' : '') + '><summary class="chev"><strong>' + esc(when) + '</strong><span class="muted">' + dm + '</span></summary><div class="review-body">' + reviewBody(r.text) + '</div></details>';
  }).join('');
}
function loadReview() {
  var box = document.getElementById('review');
  if (!box) return;
  var seq = ++REVIEW_SEQ;
  fetch('/api/self-review', {credentials: 'same-origin'}).then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
    if (seq !== REVIEW_SEQ) return;
    if (!d) { box.innerHTML = '<p class="muted">The self-review is unavailable right now.</p>'; return; }
    renderReview(d);
  }, function () {
    if (seq !== REVIEW_SEQ) return;
    box.innerHTML = '<p class="muted">The self-review is unavailable right now.</p>';
  });
}
