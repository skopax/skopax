var INFO_ROWS = [];
function infoSource(r) {
  var t = typeof TILES_BY_ID !== 'undefined' && TILES_BY_ID[r.src];
  return t ? String(t.name || r.src) : String(r.src || '');
}
function infoFiring() { return (Array.isArray(D.information) ? D.information : []).filter(function (r) { return r.firing; }); }
function infoRow(r) {
  var li = el('li', 'info-row ' + (r.firing ? 'k-info' : 'k-cleared'));
  var when = r.firing ? '<span class="tag k-info">Still firing</span>' : '<span class="tag k-cleared">Ended ' + esc(dayhm(r.endedAt)) + '</span>';
  var meta = [infoSource(r), r.firstSeen ? 'First seen ' + dayhm(r.firstSeen) : '', r.lastSeen ? 'Last seen ' + dayhm(r.lastSeen) : ''].filter(Boolean);
  li.innerHTML = '<div class="info-head"><span class="svc" data-no-tx>' + esc(r.svc || '-') + '</span><b class="info-label" data-no-tx>' + esc(r.label || '') + '</b>' + when + '</div>' +
    (r.text ? '<p class="info-text" data-no-tx>' + esc(r.text) + '</p>' : '') +
    '<p class="info-meta">' + meta.map(function (m) { return '<span>' + esc(m) + '</span>'; }).join(' \u00b7 ') + '</p>';
  if (r.moved) {
    var hand = el('p', 'info-moved');
    hand.appendChild(el('span', null, esc('Moved to Information by ' + draftBy(r.moved.by) + (r.moved.at ? ' ' + dayhm(r.moved.at) : ''))));
    var b = el('button', 'btn sec small op-btn info-back');
    b.type = 'button';
    b.textContent = 'Back to the board';
    b.dataset.tip = b.title = 'Clears the hand-set severity: the card goes back to the board with the severity its signal gives';
    b.addEventListener('click', function () { infoBack(r, b); });
    hand.appendChild(document.createTextNode(' '));
    hand.appendChild(b);
    li.appendChild(hand);
  }
  return li;
}
async function infoBack(r, b) {
  if (b.getAttribute('aria-disabled') === 'true' || typeof window.__skopaxApi !== 'function') return;
  b.disabled = true;
  try {
    await window.__skopaxApi('POST', '/api/actions', {type: 'severity', key: r.key, target: r.key, sev: 'auto', label: 'Severity back to the signal: ' + (r.svc || '-') + ' / ' + (r.label || '?')});
    if (window.__skopaxPoll) window.__skopaxPoll();
  } catch (e) {
    b.disabled = false;
    if (!e || (e.code !== 401 && e.code !== 403)) b.textContent = e && e.detail ? e.detail : 'Not saved, try again';
  }
}
function renderInfo() {
  INFO_ROWS = Array.isArray(D.information) ? D.information : [];
  var box = document.getElementById('info-list');
  if (!box) return;
  box.innerHTML = '';
  if (!INFO_ROWS.length) {
    box.appendChild(el('li', 'info-empty muted', 'No information right now. Info findings show here while they fire and for a day after they end.'));
    return;
  }
  INFO_ROWS.forEach(function (r) { box.appendChild(infoRow(r)); });
  if (typeof window.__skopaxGate === 'function') window.__skopaxGate();
}
