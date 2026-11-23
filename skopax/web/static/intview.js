var INT_SIGNED = null, INT_WANTED = false, INT_PREVIEW = false, INT_RO_PATH = '/api/integrations', INT_RO_NOTE = 'Only admins can change integrations.';
var INT_PREVIEW_NOTE = 'Preview: this is the page people without the admin role see. Only admins can change integrations.';
function intAdmin() { return typeof ADMIN_OK !== 'undefined' && !!ADMIN_OK; }
function intRO() { return !!INT_SIGNED && (!intAdmin() || INT_PREVIEW); }
function setIntView(signedIn, admin) {
  INT_SIGNED = !!signedIn;
  var row = document.getElementById('tab-integrations');
  if (row) row.hidden = !INT_SIGNED;
  if (typeof renderMeta === 'function') renderMeta();
  if (!INT_SIGNED) {
    INT_WANTED = false;
    INTEG = null;
    if (typeof curTab !== 'undefined' && curTab === 'integrations') showTab('anoms');
    return;
  }
  if (INT_WANTED) { INT_WANTED = false; showTab('integrations'); return; }
  if (typeof curTab !== 'undefined' && curTab === 'integrations') intShow();
}
function intShow() {
  if (!INT_SIGNED) return;
  INT_PREVIEW = false;
  if (typeof ADMIN_OK !== 'undefined' && ADMIN_OK && typeof adminNotices === 'function') adminNotices();
  loadIntegrations();
  if (typeof pqcLoad === 'function') pqcLoad();
}
function intPreviewToggle() {
  var b = accText('button', 'btn sec small int-preview', INT_PREVIEW ? 'Back to editing' : 'View as read-only');
  b.type = 'button';
  b.id = 'int-preview';
  b.setAttribute('aria-pressed', INT_PREVIEW ? 'true' : 'false');
  b.title = INT_PREVIEW ? 'Show the page you can edit again' : 'Shows this page as operators and viewers see it, from the same data they get';
  b.addEventListener('click', function () {
    INT_PREVIEW = !INT_PREVIEW;
    if (typeof intPopClose === 'function') intPopClose(false);
    loadIntegrations();
  });
  return b;
}
function intViewNote(cls) {
  var p = accText('p', 'int-ro-note ' + (cls || ''), INT_PREVIEW && intAdmin() ? INT_PREVIEW_NOTE : INT_RO_NOTE);
  p.setAttribute('role', 'note');
  return p;
}
