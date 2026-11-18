var D = {}, ALL = [], S = [], H = [], KIDS = {}, A = [];
var open = [], muted = [], stale = [], cleared = [], inprog = [], taken = [], slackRec = [], recurringL = [], ARCHIVED = [], CARDS = [], worst = 'ok';
var src = {}, down = [], svcOk = 0, deps = [], CH = [];
var filter = 'all', kind = 'all', svcF = null, typeF = '', shownN = 0, typesFold = false;
var TYPE_NAMES = {'page-typos': 'Typos', 'page-layout': 'Layout', log: 'Log errors', alert: 'Alerts', err: 'Error rate', '5xx': '5xx', 'avg-latency': 'Latency', 'apm-p95': 'APM p95',
  'apm-fail': 'APM failures', 'pvc-disk': 'Disk', 'db-disk': 'Host disk', 'hpa-max': 'Autoscalers', 'jvm-heap': 'JVM heap', 'varnish-cache': 'Varnish cache', 'varnish-hit': 'Varnish hit rate',
  'kafka-consume-err': 'Kafka consume errors', 'kafka-lag': 'Kafka lag', 'airbyte-long-run': 'Airbyte long runs', 'airbyte-failed': 'Airbyte failures',
  restarts: 'Pod restarts', waiting: 'Pods waiting', 'no-ready-pods': 'No ready pods', unavailable: 'Unavailable replicas', argo: 'Argo CD', 'integrity-fail': 'Integrity failures',
  'cf-status': 'Cloudflare status', csp: 'CSP', 'node-not-ready': brandName() + ' nodes', 'cert-expiry': 'Certificates', 'ad-link': 'Broken ad links', 'ad-link-moved': 'Ad link redirects',
  'sonar-gate': 'Sonar gate', 'cve-deps': 'Dependency CVEs', 'cve-os': 'OS CVEs', 'cve-scan': 'CVE scans', 'no-access': 'No access',
  'varnish-fields': 'Varnish log fields', 'varnish-cache-fields': 'Varnish cache log fields', 'logflow-fields': 'Log flow fields', 'csp-fields': 'CSP report fields',
  'logs-stopped': 'Logs stopped', 'logs-lag': 'Log pipeline behind', 'lf-errors': 'AI step errors', 'lf-traffic': 'AI traffic', 'lf-latency': 'AI turn time', 'lf-turn-cost': 'AI cost per turn',
  'lf-day-cost': 'AI cost per day', 'cf-health': 'Cloudflare health checks', 'cf-cert': 'Cloudflare certificates', 'cf-incident': 'Cloudflare incidents', 'edge-conn': 'Edge 502 / 52x',
  'edge-5xx': 'Edge 5xx', 'kafka-broker': 'Kafka brokers', 'kafka-exporter': 'Kafka exporter', 'kafka-offline': 'Kafka offline partitions', 'kafka-urp': 'Kafka under-replicated',
  'kafka-disk': 'Kafka disk', 'kafka-restart': 'Kafka restarts', 'kafka-gone': 'Kafka idle consumers', 'kafka-send-err': 'Kafka send errors', 'redis-down': 'Redis down',
  'redis-master': 'Redis master', 'redis-repl': 'Redis replicas', 'redis-evict': 'Redis evictions', 'redis-reject': 'Redis rejections', 'redis-app': 'Redis call time',
  'redis-app-err': 'Redis call errors', 'qan-load': 'Query DB time', 'qan-slow': 'Slow queries', 'qan-stall': 'Query stalls', 'qan-calls': 'Query calls', 'qan-total': 'Server DB time',
  'pg-down': 'Postgres down', 'pg-saturation': 'Postgres connections', 'pg-conn-jump': 'Postgres connection jumps', 'pg-idle-in-tx': 'Postgres idle in transaction',
  'pg-long-tx': 'Postgres long transactions', 'pg-repl-lag': 'Postgres replication lag', 'pg-lock-waits': 'Postgres lock waits', 'pg-deadlocks': 'Postgres deadlocks',
  'mongo-down': 'MongoDB down', 'mongo-state': 'MongoDB member state', 'mongo-repl-lag': 'MongoDB replication lag', 'mongo-saturation': 'MongoDB connections',
  'mongo-conn-jump': 'MongoDB connection jumps', 'mongo-queue': 'MongoDB lock queue', 'mongo-latency': 'MongoDB latency', 'mongo-no-primary': 'MongoDB no primary',
  'mongo-election': 'MongoDB elections', 'traffic-drop': 'Traffic drops', 'traffic-spike': 'Traffic spikes', 'node-pressure': 'Node pressure',
  'umami-redirect': 'Umami events', 'umami-pageviews': 'Umami pageviews', crux: 'Page speed',
  safebrowsing: 'Safe Browsing', 'ga-drop': 'GA4 drops', 'gtm-publish': 'GTM publishes', 'ad-disapproved': 'Disapproved ads', 'browser-down': 'Screenshot browser',
  'store-rating': 'Store ratings', 'play-vitals': 'Play vitals', 'crash-rate': 'Crash rate', 'crash-new': 'New crashes', 'ab-test': 'A/B tests', 'ab-verdict': 'A/B verdicts'};
var LINK_FILTERS = ['open', 'stale', 'progress', 'taken', 'muted', 'cleared', 'all', 'investigating', 'dived', 'crit', 'warn', 'info', 'slack', 'recurring', 'archived', 'closed'];
var TYPE_ALIAS = {access: 'no-access', 'umami-access': 'no-access'};
function typeOf(a) { var t = a.sig || a.src || 'other'; return TYPE_ALIAS[t] || t; }
var TAG_NAMES = {};
var TECH_TAGS = {developer: 'engineering has to change the code or config of a service', data: 'the data team has to change a pipeline, an Airbyte sync or a DWH table', infra: 'infrastructure has to change (cluster, network, DNS, CI/CD, database server)', analyst: 'an analyst has to decide or specify something first (a requirement, a business rule, a flow or a text)'};
function tagIds(a) { return (a.tags || []).map(function (t) { return t.id; }); }
function tagName(id) { return TAG_NAMES[id] || id; }
function useTagLabels(list) {
  var next = {};
  (Array.isArray(list) ? list : []).forEach(function (t) {
    if (t && typeof t.id === 'string' && typeof t.label === 'string' && t.label) next[t.id] = t.label;
  });
  var changed = JSON.stringify(next) !== JSON.stringify(TAG_NAMES);
  TAG_NAMES = next;
  return changed;
}
function tagBy(t) { return t.by === 'rule' ? 'rule' : t.by === 'pod' ? 'investigator' : draftBy(t.by); }
function isTagF(t) { return String(t).startsWith('tag:'); }
var ME_USER = '';
var ME_CHANNEL = '';
var ME_FEATURES = {}, ME_OFF = {}, ME_CHAT = false, ME_GITLAB = false;
var WHO_NAMES = {'who:any': 'Assigned', 'who:me': 'Assigned to me', 'who:none': 'Unassigned'};
var WHO_CHIPS = {'who:any': 'Assigned', 'who:me': 'Me', 'who:none': 'Unassigned'};
var FOLD_LETTERS = {'\u0131': 'i', '\u0142': 'l', '\u00f8': 'o', '\u0111': 'd', '\u00f0': 'd', '\u00fe': 'th', '\u00df': 'ss', '\u00e6': 'ae', '\u0153': 'oe', '\u0127': 'h'};
var FOLD_RE = new RegExp('[' + Object.keys(FOLD_LETTERS).join('') + ']', 'g');
function foldCase(s) { return String(s == null ? '' : s).normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().replace(FOLD_RE, function (c) { return FOLD_LETTERS[c]; }); }
function personSlug(s) {
  return foldCase(String(s || '').trim()).split('@')[0].split(/[^a-z0-9]+/).filter(Boolean).join('.');
}
function ownerName(o) { return String(o || '').indexOf('@') > 0 ? String(o).split('@')[0] : String(o || ''); }
var SOURCE_NAMES = {page: brandName() + ' page', chat: 'Chat', api: 'API', mcp: 'MCP'};
function sourceLabel(x) {
  var s = (x && x.source) || '', by = String((x && x.requestedBy) || '');
  if (x && x.via === 'mcp') return 'MCP';
  if (s === 'slack') {
    var sl = x.slack || {};
    if (sl.outside) return '#' + (sl.channelName || sl.channel || '?');
    return /^#[^\s#]+$/.test(ME_CHANNEL) ? ME_CHANNEL : 'main channel';
  }
  if (isTokenActor(by)) return 'API';
  if (SOURCE_NAMES[s]) return SOURCE_NAMES[s];
  if (s) return s;
  return by && by.indexOf('@') < 0 ? brandName() : brandName() + ' page';
}
function isMine(a) { return !!a.owner && !!ME_USER && (personSlug(a.owner) === personSlug(ME_USER) || !!(a.assigned && a.assigned.email === ME_USER)); }
function isWhoF(t) { return String(t).startsWith('who:'); }
function isPersonF(t) { return String(t).startsWith('who:u:'); }
function fParts(t) { return String(t || '').split('+').filter(Boolean); }
function fKind(p) { return isTagF(p) ? 'tag' : isWhoF(p) ? 'who' : 'type'; }
function hasF(p) { return fParts(typeF).indexOf(p) >= 0; }
var F_ORDER = {type: 0, who: 1, tag: 2};
function toggledF(p) {
  var parts = fParts(typeF).filter(function (x) { return fKind(x) !== fKind(p); });
  if (!hasF(p)) parts.push(p);
  return parts.sort(function (x, y) { return F_ORDER[fKind(x)] - F_ORDER[fKind(y)]; }).join('+');
}
function typeIs(a, t) { return String(t).indexOf('+') > 0 ? fParts(t).every(function (p) { return typeIs(a, p); }) : isWhoF(t) ? (t === 'who:none' ? !a.owner : t === 'who:any' ? !!a.owner : isPersonF(t) ? !!a.owner && personSlug(a.owner) === t.slice(6) : isMine(a)) : isTagF(t) ? tagIds(a).indexOf(t.slice(4)) >= 0 : typeOf(a) === (TYPE_ALIAS[t] || t); }
function typeName(t) { return String(t).indexOf('+') > 0 ? fParts(t).map(typeName).join(' \u00b7 ') : WHO_NAMES[t] || (isPersonF(t) ? 'Assigned to ' + t.slice(6) : isTagF(t) ? tagName(t.slice(4)) : TYPE_NAMES[TYPE_ALIAS[t] || t] || String(t).replace(/-/g, ' ')); }
function typed(list) { return typeF ? list.filter(function (a) { return typeIs(a, typeF); }) : list; }
var searchQ = '', searchBack = '';
var ASK_MAX = 1000;
function cardText(a) {
  return foldCase([a.num ? '#' + a.num : '', a.svc, a.label, a.sub, a.ns, a.key, a.what, a.owner, a.ticket, tagIds(a).map(tagName).join(' '), a.note && a.note.text].filter(Boolean).join(' '));
}
function cardMatches(a, terms) {
  var text = [a].concat(KIDS[a.key] || []).map(cardText).join(' ');
  return terms.every(function (t) { return /^\d+$/.test(t) ? text.indexOf('#' + t + ' ') >= 0 || (' ' + text + ' ').indexOf(' ' + t + ' ') >= 0 || text.indexOf(t) >= 0 && t.length > 2 : text.indexOf(t) >= 0; });
}
function searched(list) {
  var terms = foldCase(searchQ).split(/\s+/).filter(Boolean);
  return terms.length ? list.filter(function (a) { return cardMatches(a, terms); }) : list;
}
function clearSearchUnless(list) {
  if (!searchQ || searched(list).length) return;
  searchQ = '';
  var box = document.getElementById('card-search');
  if (box) box.value = '';
}
var SEV = {crit: ['Critical', 'k-crit'], warn: ['Warning', 'k-warn'], info: ['Info', 'k-info'], ok: ['Healthy', 'k-ok']};
var SERVER_TZ = '';
var JIRA_BASE = '';
function setJiraBase(u) { JIRA_BASE = String(u || '').replace(/\/+$/, ''); }
function jiraHref(k) { return JIRA_BASE ? JIRA_BASE + '/browse/' + encodeURIComponent(k) : ''; }
var ISSUE_KEY = /\b[A-Z][A-Z0-9]{1,9}-\d+\b/g;
var TICKET_LINKS = null;
function setTicketProjects(list) {
  var keys = (list || []).filter(function (p) { return /^[A-Z][A-Z0-9]{1,9}$/.test(p); });
  TICKET_LINKS = keys.length ? new RegExp('\\b(?:' + keys.join('|') + ')-\\d+\\b', 'g') : null;
}
function jiraLinks(t) { return !TICKET_LINKS ? esc(t) : esc(t).replace(TICKET_LINKS, function (k) { return JIRA_BASE ? '<a href="' + jiraHref(k) + '" target="_blank" rel="noopener">' + k + ' ↗</a>' : k; }); }
function ticketKey(t) { return (String(t || '').match(ISSUE_KEY) || [])[0]; }
function recurs(a) { return !!(a.recurring && a.recurringCount && !(a.triage === 'progress' && ticketKey(a.ticket))); }
var TRI = {investigating: ['Investigating', 'k-act'], dived: ['Investigated', 'k-cleared'], progress: ['In progress', 'k-info'], handled: ['No action needed', 'k-good']};
var RES_KINDS = [['fixed', 'Fixed'], ['handled', 'No action needed'], ['duplicate', 'Duplicate']];
var RES_VIA = {page: 'via page', chat: 'via chat', slack: 'via Slack'};
RES_VIA[SYSTEM_ACTOR] = 'via ' + brandName();
var RES_BY = {pod: 'investigator', auto: 'auto-resolve', 'jira-links': 'Jira link sweep'};
RES_BY[SYSTEM_ACTOR] = 'quality check';
function resolutionOf(x) { var r = x && x.resolution; return r && RES_KINDS.some(function (k) { return k[0] === r.kind; }) ? r : null; }
function resWho(by) { return RES_BY[by] || (isTokenActor(by) ? tokenActorLabel(by, 'Claude') : !by || by === 'Claude' ? 'Claude' : String(by).split('@')[0]); }
function resRefHtml(ref) {
  var t = String(ref || '').trim(), n = /^#?(\d{1,7})$/.exec(t);
  if (n) return '<a href="/c/' + n[1] + '" title="Opens card #' + n[1] + '">#' + n[1] + '</a>';
  if (/^https:\/\/\S+$/.test(t)) return '<a href="' + esc(t) + '" target="_blank" rel="noopener" data-no-tx>' + esc(t.replace(/^https:\/\//, '').slice(0, 60)) + ' ↗</a>';
  return '<span data-no-tx>' + jiraLinks(t) + '</span>';
}
function resKindHtml(r) {
  if (!r) return '<span>Resolved</span>';
  if (r.kind === 'duplicate') return '<span>Duplicate of</span> ' + resRefHtml(r.ref);
  var name = RES_KINDS.filter(function (k) { return k[0] === r.kind; })[0][1];
  return '<span>' + esc(name) + '</span>' + (r.ref ? ' (' + resRefHtml(r.ref) + ')' : '');
}
function resolutionHtml(r) {
  if (!r) return resKindHtml(null);
  return resKindHtml(r) + (RES_VIA[r.via] ? ' · <span>' + esc(RES_VIA[r.via]) + '</span>' : '') + (r.by ? ' · <span data-no-tx>' + esc(resWho(r.by)) + '</span>' : '');
}
function outcomeHtml(outcome, r) {
  return outcome === 'handled' || outcome === 'fixed' || outcome === 'duplicate' ? resolutionHtml(resolutionOf({resolution: r})) : null;
}
var RES_TIPS = {fixed: 'Fixed: the cause is gone because something was changed, such as a deploy, a config change or a rollback', handled: 'No action needed: harmless or a one-off, nothing was changed',
  duplicate: 'Duplicate: the same problem as another card, which carries it on'};
function resKindPicker(picked, onPick) {
  var g = el('div', 'res-kinds');
  g.setAttribute('role', 'group');
  g.setAttribute('aria-label', 'How it was resolved');
  RES_KINDS.forEach(function (k) {
    var b = el('button', 'btn sec small sev-pick res-kind');
    b.type = 'button';
    b.textContent = k[1];
    b.title = RES_TIPS[k[0]];
    b.dataset.kind = k[0];
    b.setAttribute('aria-pressed', String(k[0] === picked));
    b.addEventListener('click', function () {
      g.querySelectorAll('button').forEach(function (x) { x.setAttribute('aria-pressed', String(x === b)); });
      onPick(k[0]);
    });
    g.appendChild(b);
  });
  return g;
}
function resolveBody(kind, why, ref, name) {
  if (kind === 'duplicate') return {type: 'resolve', kind: 'duplicate', ref: ref, label: 'Resolve ' + name + ' (duplicate of ' + ref + ')'};
  var out = {type: 'resolve', label: 'Resolve ' + name + (kind === 'fixed' ? ' (fixed)' : ' (no action needed)')};
  if (kind === 'fixed') out.kind = 'fixed';
  if (why) out.reason = why;
  return out;
}
function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
var SEMI = /;(?![^()]*\))/, COMMA = /,(?![^()]*\))/;
function splitTop(s, sep) { return String(s == null ? '' : s).split(sep).map(function (x) { return x.trim(); }).filter(Boolean); }
function cellLines(items) { return items.length > 1 ? '<div class="cell-lines">' + items.join('') + '</div>' : items.join(''); }
function textLines(s, sep) { return cellLines(splitTop(s, sep).map(function (x) { return '<span>' + esc(x) + '</span>'; })); }
function linkKey(key) { return encodeURIComponent(key).replace(/[()!'*]/g, function (c) { return '%' + c.charCodeAt(0).toString(16).toUpperCase(); }); }
function slackThread(u) {
  try { var x = new URL(String(u || '')); return x.protocol === 'https:' && (x.hostname === 'slack.com' || x.hostname.endsWith('.slack.com')); } catch (e) { return false; }
}
function slackLink(url, text, tip) {
  var t = tip ? ' title="' + esc(tip) + '"' : '';
  if (slackThread(url)) return '<a class="slack-msg" href="' + esc(url) + '" target="_blank" rel="noopener"' + t + '>' + esc(text) + ' ↗</a>';
  return tip ? '<span' + t + '>' + esc(text) + '</span>' : esc(text);
}
function el(tag, cls, html) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html != null) e.innerHTML = html;
  return e;
}
function load(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function save(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
function hm(iso) {
  if (!iso) return '';
  var d = new Date(iso);
  return isNaN(d) ? '' : d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hour12: false});
}
var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
function dayMonth(d) { return d.getDate() + ' ' + MONTHS[d.getMonth()]; }
function dayhm(iso) {
  if (!iso) return '';
  var d = new Date(iso);
  if (isNaN(d)) return '';
  var same = d.toDateString() === new Date().toDateString();
  return (same ? '' : dayMonth(d) + ' ') + hm(iso);
}
function daytime(iso) {
  if (!iso) return '';
  var d = new Date(iso);
  if (isNaN(d)) return '';
  return dayMonth(d) + ' ' + hm(iso);
}
function zoneLabel() {
  try {
    var part = new Intl.DateTimeFormat([], {timeZoneName: 'short'}).formatToParts(new Date()).filter(function (p) { return p.type === 'timeZoneName'; })[0];
    return part ? part.value : '';
  } catch (e) { return ''; }
}
function dayhmZone(iso) {
  var t = dayhm(iso), z = zoneLabel();
  return t && z ? t + ' ' + z : t;
}
function ago(iso) {
  var m = Math.round((Date.now() - new Date(iso)) / 60000);
  if (!(m >= 0)) return '';
  return m < 60 ? m + ' min ago' : m < 1440 ? Math.floor(m / 60) + ' h ' + (m % 60) + ' min ago' : Math.floor(m / 1440) + ' d ago';
}
function secs(v) { var x = Math.round(v); return x < 120 ? x + ' s' : x < 7200 ? Math.floor(x / 60) + ' min' : Math.floor(x / 3600) + ' h' + (x % 3600 >= 60 ? ' ' + Math.floor(x % 3600 / 60) + ' min' : ''); }
function num(v, unit) {
  if (v == null) return '<span class="zero-v">–</span>';
  if (unit === 'dur') return secs(v);
  if (unit === 'ms') return v >= 1000 ? (v / 1000).toFixed(1) + ' s' : Math.round(v) + ' ms';
  if (unit === '%') return (v >= 10 ? Math.round(v) : v.toFixed(1)) + '%';
  return v === 0 ? '<span class="zero-v">0</span>' : (Math.abs(v) >= 1 ? Math.round(v).toLocaleString() : v.toFixed(2));
}
function sev(s) { var x = SEV[s] || [s, 'k-muted']; return '<span class="sev ' + x[1] + '">' + esc(x[0]) + '</span>'; }
function state(s, label) { var x = SEV[s] || [s, 'k-muted']; return '<span class="state ' + x[1] + '">' + esc(label || x[0]) + '</span>'; }
function change(a) {
  var base = Math.max(a.d1 || 0, a.d7 || 0);
  if (a.now == null) return '–';
  if (!base) return a.now ? 'new' : '–';
  if (a.unit === 'ms') { var d = a.now - base; return (d >= 0 ? '+' : '−') + num(Math.abs(d), 'ms'); }
  var r = a.now / base;
  return '×' + (r >= 10 ? Math.round(r) : r.toFixed(1));
}
function linksHtml(list, max) {
  var ls = (list || []).filter(function (l) { return String(l.url).startsWith('https://'); });
  if (max) ls = ls.slice(0, max);
  return ls.length ? '<div class="links">' + ls.map(function (l) { return '<a href="' + esc(l.url) + '" target="_blank" rel="noopener"><span>' + esc(l.label) + '</span><span aria-hidden="true">↗</span></a>'; }).join('') + '</div>' : '';
}

function done(a) { return a.triage === 'progress' || a.triage === 'handled'; }
var ARCHIVE_DEFAULT_H = 48;
function archiveHours() { return Number(D && D.archiveHours) || ARCHIVE_DEFAULT_H; }
function stamp(s) { var v = s ? Date.parse(s) : NaN; return isNaN(v) ? 0 : v; }
function muteAt(a) {
  if (!a.muted) return null;
  var m = ((D && D.muted) || []).filter(function (x) { return x && x.pattern === a.key; })[0];
  return m && m.at;
}
function lastChange(a) { return Math.max(stamp(a.clearedAt), stamp(a.triagedAt), stamp(muteAt(a))); }
function archivedCard(a) {
  if (a.status === 'open' && !a.muted) return false;
  if (a.status === 'cleared' && !a.muted && !done(a) && (a.sev === 'warn' || a.sev === 'crit')) return false;
  var last = lastChange(a);
  return last > 0 && Date.now() - last > archiveHours() * 3600000;
}
function healthCard(a) { return !a.noHealth; }
function bySev(s) { return open.filter(function (a) { return a.sev === s && healthCard(a); }); }
function openAll() { return open.concat(stale.filter(function (a) { return open.indexOf(a) < 0; })); }
function openBySev(s) { return openAll().filter(function (a) { return a.sev === s; }); }
var SEV_ORD = {crit: 0, warn: 1, info: 2};
function bySevOrd(x, y) { return (x.sev in SEV_ORD ? SEV_ORD[x.sev] : 3) - (y.sev in SEV_ORD ? SEV_ORD[y.sev] : 3); }
var SVC_RANK = {crit: 3, warn: 2, info: 1, ok: 0};
var ROW_NS = {};
function ownedBy(a, s) { return a.svc === s.svc && (!ROW_NS[a.ns] || a.ns === s.ns); }
function reqOf(s) { return s.req || s['gw-req'] || s['ing-req'] || 0; }
function clusterTag(x) { var c = x && (x.cluster || ((D.services || []).concat(D.deploys || []).filter(function (r) { return r.cluster && r.ns === x.ns; })[0] || {}).cluster); return c ? ' (' + c + ')' : ''; }
var LG = {};
function srcOpenOf(srcName, filter) { return ALL.filter(function (a) { return a.status === 'open' && !a.muted && !done(a) && a.src === srcName && (!filter || filter(a)); }).sort(bySevOrd); }
function kOf(list, has) { return !has ? 'k-muted' : list.length ? (SEV[list[0].sev] || SEV.info)[1] : 'k-ok'; }
var PLUGIN_TILES = [], SRC_TAB = {}, SOURCE_GROUPS = [], SOURCES = [];
var WARN_PARTS = ['browser down'];
function instWarn(v) { v = String(v || ''); return v.indexOf(' no access to ') >= 0 ? 'no access' : v.split(' \u00b7 ').slice(1).filter(function (p) { return WARN_PARTS.indexOf(p) >= 0; })[0] || ''; }
function instState(i) {
  if (i.state) return i.state === 'off' ? 'none' : i.state;
  var v = String(i.status || '');
  return !i.pass ? 'none' : !v ? 'pending' : v === 'ok' || v.indexOf('ok ') === 0 ? (instWarn(v) ? 'warn' : 'ok') : 'error';
}
function instSaid(i) { return String(i.state ? i.text || '' : i.status || ''); }
function instWarnText(i) { return instWarn(i.status) || instSaid(i).toLowerCase(); }
function tileState(t) {
  var st = (t.instances || []).map(instState);
  return st.indexOf('error') >= 0 ? 'error' : st.indexOf('warn') >= 0 ? 'warn' : st.indexOf('pending') >= 0 ? 'pending' : st.indexOf('ok') >= 0 ? 'ok' : 'none';
}
function srcState(k) { var t = TILES_BY_ID[k]; return t ? tileState(t) : 'pending'; }
var TILES_BY_ID = {};
function srcDetail(k) {
  var t = TILES_BY_ID[k], bad = t ? (t.instances || []).filter(function (i) { return instState(i) === 'error'; }) : [];
  return bad.map(function (i) { var said = instSaid(i).replace(/^error:\s*/, ''); return (i.name && said.indexOf(i.name + ': ') !== 0 ? i.name + ' ' : '') + said; }).join(', ');
}
function isPrimaryName(n) {
  return PLUGIN_TILES.some(function (p) { return (p.instances || []).some(function (i) { return i.primary && i.name === n; }); });
}
function setPlugins(list) {
  PLUGIN_TILES = Array.isArray(list) ? list.filter(function (p) { return p && typeof p.id === 'string'; }) : [];
  TILES_BY_ID = {};
  SRC_TAB = {};
  PLUGIN_TILES.forEach(function (p) {
    TILES_BY_ID[p.id] = p;
    SRC_TAB[p.id] = [String(p.name || p.id), p.tab || 'integrations'];
  });
  SOURCES = PLUGIN_TILES.map(function (p) { return p.id; });
  SOURCE_GROUPS = SOURCES.length ? [{id: 'integrations', label: 'Integrations', srcs: SOURCES}] : [];
  down = SOURCES.filter(function (k) { return srcState(k) === 'error'; });
  if (typeof sidePlugins === 'function') sidePlugins([].concat.apply([], PLUGIN_TILES.map(function (p) { return (Array.isArray(p.pages) ? p.pages : []).map(function (g) { return Object.assign({}, g, {plugin: p.id}); }); })));
}
function sigTags(list) {
  return list.length ? '<div class="sig-tags">' + list.map(function (a) {
    return '<button type="button" class="tag ' + (SEV[a.sev] || SEV.info)[1] + '" data-key="' + esc(a.key) + '" title="' + esc(a.short || 'Open this anomaly') + '">' + esc(a.label || String(a.key).split('|')[2]) + '</button>';
  }).join('') + '</div>' : '';
}
function wireTags(box) { box.querySelectorAll('.sig-tags button').forEach(function (b) { b.addEventListener('click', function () { showLinked(encodeURIComponent(b.dataset.key)); }); }); }
function fineMs(v) { return v == null ? num(null) : v < 0.01 ? '&lt;0.01 ms' : v < 10 ? v.toFixed(2) + ' ms' : num(v, 'ms'); }

function derive(data) {
  D = data || {};
  ALL = D.anomalies || []; S = D.services || []; H = D.history || [];
  KIDS = {};
  var byKey = {};
  ALL.forEach(function (a) { byKey[a.key] = a; });
  var host = function (a) {
    var p = a.parent && a.parent !== a.key && byKey[a.parent];
    return p && (p.status === 'open' || a.status === 'cleared') ? p : null;
  };
  var root = function (a) {
    var seen = {}, r = null, p = host(a);
    while (p && !seen[p.key]) { seen[p.key] = 1; r = p; p = host(p); }
    return r && r !== a ? r : null;
  };
  A = ALL.filter(function (a) {
    var r = root(a);
    if (r) {
      KIDS[r.key] = KIDS[r.key] || [];
      KIDS[r.key].push(a);
    }
    return !r;
  });
  ARCHIVED = A.filter(archivedCard);
  A = A.filter(function (a) { return ARCHIVED.indexOf(a) < 0; });
  CARDS = A.concat(ARCHIVED);
  open = A.filter(function (a) { return a.status === 'open' && !a.muted && !done(a); });
  muted = A.filter(function (a) { return (a.status === 'open' || a.status === 'cleared') && a.muted && !done(a); });
  stale = A.filter(function (a) { return !a.muted && !done(a) && ((a.status === 'cleared' && (a.sev === 'warn' || a.sev === 'crit')) || (a.status === 'open' && a.triage === 'dived')); });
  cleared = A.filter(function (a) { return a.status === 'cleared' && !done(a) && stale.indexOf(a) < 0; });
  inprog = A.filter(function (a) { return a.triage === 'progress'; });
  taken = A.filter(function (a) { return a.triage === 'handled'; });
  slackRec = A.filter(function (a) { return draftOf(a) && !draftSent(a) && draftLive(a); });
  recurringL = A.filter(recurs);
  worst = bySev('crit').length ? 'crit' : bySev('warn').length ? 'warn' : bySev('info').length ? 'info' : 'ok';
  src = D.sources || {};
  down = SOURCES.filter(function (k) { return srcState(k) === 'error'; });
  var live = Array.isArray(D.anomalies);
  ROW_NS = {};
  S.forEach(function (s) { ROW_NS[s.ns] = true; });
  S = S.map(function (s) {
    var by = live ? open.filter(function (a) { return healthCard(a) && ownedBy(a, s); }).sort(bySevOrd) : [];
    return Object.assign({}, s, {status: live ? (by.length ? by[0].sev : 'ok') : s.status || 'ok', drivers: by});
  });
  S.sort(function (x, y) { return (SVC_RANK[y.status] || 0) - (SVC_RANK[x.status] || 0) || (x.kind !== 'app') - (y.kind !== 'app') || reqOf(y) - reqOf(x); });
  svcOk = S.filter(function (s) { return s.status === 'ok'; }).length;
  deps = D.deploys || [];
  LG = D.logins || {};
}

(function () {
  var box = null, timer = null, cur = null, saved = null;
  function target(e) { return e.target && e.target.closest ? e.target.closest('[title], #side [data-label]') : null; }
  function railed(el) { return document.body.classList.contains('is-rail') && !!el.dataset.label && !!el.closest('#side'); }
  function badges(el) {
    var out = '', unread = el.querySelector('.b-num:not([hidden])'), n = unread && unread.firstChild ? unread.firstChild.textContent : '';
    if (el.querySelector('.b-new')) out += ' \u00b7 New';
    if (n) out += ' \u00b7 ' + n + ' unread';
    if (el.querySelector('.b-plug')) out += ' \u00b7 plug-in';
    return out;
  }
  function show(el) {
    var own = el.getAttribute('title'), rail = railed(el), text = rail ? el.dataset.label + badges(el) + (own ? '\n' + own : '') : own;
    box = box || document.getElementById('tipbox');
    if (!text || !box || (el.hasAttribute('aria-haspopup') && el.getAttribute('aria-expanded') === 'true')) return;
    hide();
    cur = el; saved = own;
    if (own != null) el.removeAttribute('title');
    box.textContent = text;
    box.hidden = false;
    var r = el.getBoundingClientRect(), bw = box.offsetWidth, bh = box.offsetHeight, vw = window.innerWidth, vh = window.innerHeight;
    var left = rail ? Math.min(r.right + 10, Math.max(8, vw - bw - 8)) : Math.min(Math.max(8, r.left + r.width / 2 - bw / 2), Math.max(8, vw - bw - 8));
    var top = rail ? Math.max(8, Math.min(r.top + r.height / 2 - bh / 2, vh - bh - 8)) : r.bottom + 7;
    if (!rail && top + bh > vh - 8) top = Math.max(8, r.top - bh - 7);
    box.style.left = left + 'px';
    box.style.top = top + 'px';
  }
  function hide() {
    clearTimeout(timer);
    timer = null;
    if (cur && saved != null && !cur.hasAttribute('title')) cur.setAttribute('title', saved);
    cur = null; saved = null;
    if (box) box.hidden = true;
  }
  document.addEventListener('pointerover', function (e) {
    var el = target(e);
    if (!el || el === cur) return;
    clearTimeout(timer);
    timer = setTimeout(function () { show(el); }, 90);
  });
  document.addEventListener('pointerout', function (e) {
    var el = e.target && e.target.closest ? e.target.closest('[title], [data-tipping]') : null;
    if (cur && (!e.relatedTarget || !cur.contains(e.relatedTarget))) hide();
    else if (!cur && el && timer) { clearTimeout(timer); timer = null; }
  });
  document.addEventListener('focusin', function (e) { var el = target(e); if (el) show(el); });
  document.addEventListener('focusout', function () { hide(); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') hide(); });
  window.addEventListener('scroll', function () { hide(); }, true);
  window.__skopaxTipHide = hide;
})();
