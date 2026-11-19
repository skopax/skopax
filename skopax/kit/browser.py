import base64
import json
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from skopax.contract import HttpError, ReadError, Transport, call, iso
from skopax.errkind import Authored, kind
from skopax.kit import web
from skopax.kit.web import API, CHALLENGE, CHALLENGE_PAGE, Site, allowed_host, clean, hits, path_of, refusal, under, user_agent
from skopax.remote import addresses, unproxied

DEFAULT_PORT = 9222
MAX_MESSAGE = 64 * 1024 * 1024
GAP_S = 5.0
CONNECT_S = 3.0
LOAD_S = 15.0
SETTLE_S = 1.0
TIMEOUT_S = 20.0
DISPOSE_S = 3.0
LOOKUP_S = 3.0
MAX_HEIGHT = 2576
QUALITY = 80
MAX_BYTES = 3 * 1024 * 1024
TITLE_LIMIT = 200
BLOCKED_HOSTS = 8
VIEWPORTS = {'desktop': (1280, 800, False), 'phone': (390, 844, True)}
CDN_IMAGES = '/cdn-cgi/image/'
TRACKING_TYPES = frozenset({'Ping', 'CSPViolationReport'})
READ_METHODS = ('GET', 'HEAD')
DESKTOP_UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36 {token}'
PHONE_UA = 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Mobile Safari/537.36 {token}'
STUB = ("(() => { const no = function () { throw new DOMException('blocked by the page check', 'SecurityError'); }; "
        "for (const name of ['WebSocket', 'EventSource', 'Worker', 'SharedWorker']) { try { Object.defineProperty(window, name, {value: no, writable: false, configurable: false}); } catch (e) {} } "
        "try { if (window.ServiceWorkerContainer) { Object.defineProperty(ServiceWorkerContainer.prototype, 'register', {value: () => Promise.reject(new DOMException('blocked by the page check', 'SecurityError')), "
        "writable: false, configurable: false}); } } catch (e) {} })();")
OFF = 'no browser url is set'
DOWN = 'the screenshot browser does not answer'
REFUSED = 'the screenshot browser refused the request'
NOT_HTTP = 'the browser URL must be http://host:port, the DevTools endpoint of a browser service'
UNREADABLE = 'the browser gave an answer the page check cannot read'
CATEGORIES = ('otherHosts', 'api', 'tracking')
FOCUS_ABOVE = 200
FOCUS_HEIGHT = 700
MARKS_MAX = 10
SELECTOR_MAX = 200
OVERFLOW_MAX = 5
SCROLL_SHOTS = 2
SCROLL_MIN_S = 3.0
REVEALED_MAX = 10
REVEALED_TEXT = 80
VERDICTS = ('scrolls', 'cut', 'page')
PINNED = "(() => { let n = 0; for (const e of document.querySelectorAll('body *')) { if (getComputedStyle(e).position === 'fixed') { e.style.setProperty('visibility', 'hidden', 'important'); n++; } } return n; })()"
FOCUS = r"""((needle) => {
  const key = s => (s || '').replace(/\s+/g, ' ').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
  const want = key(needle);
  let best = null;
  for (const el of document.body.querySelectorAll('*')) {
    if (!key(el.textContent).includes(want)) continue;
    if ([...el.children].some(c => key(c.textContent).includes(want))) continue;
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) { best = el; break; }
  }
  if (!best) return null;
  best.style.outline = '3px solid #e5484d';
  best.style.outlineOffset = '2px';
  const r = best.getBoundingClientRect();
  return {y: Math.round(r.top + window.scrollY), h: Math.round(r.height)};
})"""
MARK = r"""((marks) => {
  const spots = [];
  for (const m of marks) {
    if (typeof m === 'string') {
      let found = [];
      try { found = [...document.querySelectorAll(m)].slice(0, 5); } catch (e) { found = []; }
      for (const el of found) {
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        el.style.outline = '3px solid #e5484d';
        el.style.outlineOffset = '2px';
        spots.push({y: r.top + window.scrollY, h: r.height});
      }
    } else {
      const box = document.createElement('div');
      box.style.cssText = 'position:absolute;pointer-events:none;z-index:2147483647;left:' + m.x + 'px;top:' + m.y + 'px;width:' + m.w + 'px;height:' + m.h + 'px';
      box.style.outline = '3px solid #e5484d';
      box.style.outlineOffset = '2px';
      document.documentElement.appendChild(box);
      spots.push({y: m.y, h: m.h});
    }
  }
  if (!spots.length) return null;
  const top = Math.min(...spots.map(s => s.y)), bottom = Math.max(...spots.map(s => s.y + s.h));
  return {y: Math.round(top), h: Math.round(bottom - top)};
})"""
OVERFLOW = r"""((limit) => {
  const vw = document.documentElement.clientWidth;
  const items = [], taken = [];
  const hides = o => o === 'hidden' || o === 'clip';
  const identity = [[1, 0, 0, 1, 0, 0], [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]];
  const moved = t => {
    if (t === 'none') return false;
    const m = /^matrix(?:3d)?\(([^)]*)\)$/.exec(t);
    if (!m) return true;
    const v = m[1].split(',').map(Number);
    return !identity.some(i => i.length === v.length && i.every((x, n) => x === v[n]));
  };
  const track = q => {
    const kids = [...q.children].map(k => k.getBoundingClientRect()).filter(k => k.width > 0 && k.height > 0);
    if (kids.length < 2) return false;
    const room = Math.min(vw, q.parentElement ? q.parentElement.getBoundingClientRect().width : vw);
    const left = Math.min(...kids.map(k => k.left)), right = Math.max(...kids.map(k => k.right));
    return right - left > room + 2 && kids.every(k => k.width <= room + 2);
  };
  const lost = (el, clips) => {
    const box = el.getBoundingClientRect();
    if (box.width <= 2 || box.height <= 2) return false;
    let lo = 0, hi = vw;
    for (const c of clips) { const k = c.getBoundingClientRect(); lo = Math.max(lo, k.left + c.clientLeft); hi = Math.min(hi, k.left + c.clientLeft + c.clientWidth); }
    if (hi - lo <= 2) return false;
    const out = k => k.width > 0 && k.height > 0 && (k.right > hi + 4 || k.left < lo - 4);
    const calm = n => {
      for (let q = n; q; q = q.parentElement) {
        const qs = getComputedStyle(q);
        if (q.getAttribute('aria-hidden') === 'true' || (qs.textOverflow === 'ellipsis' && qs.overflowX !== 'visible')) return true;
        if (clips.includes(q)) return false;
        if (q instanceof SVGElement || moved(qs.transform) || (qs.transform !== 'none' && track(q)) || qs.animationName !== 'none' || qs.opacity === '0') return true;
        if ((qs.overflowX === 'auto' || qs.overflowX === 'scroll') && !out(q.getBoundingClientRect())) return true;
      }
      return false;
    };
    const controls = 'a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button], [role=link], [tabindex]:not([tabindex="-1"])';
    for (const c of [el, ...el.querySelectorAll(controls)]) {
      if (c.matches(controls) && getComputedStyle(c).visibility === 'visible' && out(c.getBoundingClientRect()) && !calm(c)) return true;
    }
    const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    const range = document.createRange();
    for (let n = walk.nextNode(); n; n = walk.nextNode()) {
      if (!/\S/.test(n.data)) continue;
      range.selectNodeContents(n);
      if (![...range.getClientRects()].some(out)) continue;
      const t = n.parentElement;
      if (t && getComputedStyle(t).visibility === 'visible' && !calm(t)) return true;
    }
    return false;
  };
  let kept = 0, short = false;
  const started = performance.now();
  for (const el of document.body.querySelectorAll('*')) {
    if (kept >= limit) break;
    if (performance.now() - started > 3000) { short = true; break; }
    if (taken.some(t => t.contains(el))) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0 || r.right <= 1 || r.left >= vw - 1) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden') continue;
    const sw = el.scrollWidth, cw = el.clientWidth, ox = style.overflowX;
    const wide = cw > 0 && sw > cw + 1, past = r.right > vw + 1;
    if (!wide && !past) continue;
    if (ox === 'visible' && !past && r.left + sw <= vw + 1) continue;
    const right = ox === 'visible' ? Math.max(r.right, r.left + sw) : r.right;
    let clip = '', pans = false, edge = null;
    const root = document.documentElement, rootVisible = getComputedStyle(root).overflowX === 'visible';
    for (let p = el.parentElement; p; p = p.parentElement) {
      const ps = getComputedStyle(p), o = ps.overflowX;
      if (o === 'visible') continue;
      const pr = p.getBoundingClientRect();
      if (ps.overflowY === 'visible' || right > pr.right + 1 || r.left < pr.left - 1) { clip = o; pans = p === root || (p === document.body && rootVisible); edge = p; break; }
    }
    const self = hides(ox) && wide, outer = hides(clip) && !pans;
    const item = {i: items.length, ox: ox, sw: sw, cw: cw, x: Math.round(r.left + window.scrollX), y: Math.round(r.top + window.scrollY), w: Math.round(r.width), h: Math.round(r.height), clip: clip, pans: pans};
    if (self || outer) item.lost = lost(el, [self ? el : null, outer ? edge : null].filter(c => c));
    el.setAttribute('data-page-check-overflow', String(items.length));
    items.push(item);
    if (item.lost === false) { if (el instanceof SVGElement) taken.push(el); continue; }
    kept++;
    taken.push(el);
  }
  return {vw: vw, items: items, cutShort: short};
})"""
SCROLL = r"""((index) => {
  const el = document.querySelector('[data-page-check-overflow="' + index + '"]');
  if (!el) return null;
  const vw = document.documentElement.clientWidth;
  const cells = () => { const heads = [...el.querySelectorAll('th')]; if (heads.length) return heads; const row = el.querySelector('tr'); return row ? [...row.children] : []; };
  const inside = c => { const r = c.getBoundingClientRect(), b = el.getBoundingClientRect(); return r.width > 0 && r.right > b.left + 1 && r.left < b.right - 1; };
  const seen = c => { const r = c.getBoundingClientRect(), b = el.getBoundingClientRect(); return r.width > 0 && r.right > Math.max(b.left, 0) + 1 && r.left < Math.min(b.right, vw) - 1; };
  const text = c => (c.textContent || '').replace(/\s+/g, ' ').trim();
  const before = new Set(cells().filter(seen));
  const rtl = getComputedStyle(el).direction === 'rtl';
  el.scrollTo({left: rtl ? -el.scrollWidth : el.scrollWidth, behavior: 'instant'});
  el.style.outline = '3px solid #e5484d';
  el.style.outlineOffset = '2px';
  const after = cells();
  const revealed = after.filter(c => !before.has(c) && seen(c)).map(text).filter(t => t);
  const offscreen = after.filter(c => !seen(c) && inside(c)).map(text).filter(t => t);
  const r = el.getBoundingClientRect();
  return {y: Math.round(r.top + window.scrollY), h: Math.round(r.height), revealed: revealed.slice(0, 10), offscreen: offscreen.slice(0, 10)};
})"""

NAMES = r"""((box) => {
  const el = document.querySelector('[data-page-check-overflow="' + box + '"]');
  if (!el) return null;
  let right = document.documentElement.clientWidth, left = 0;
  for (let p = el; p && p !== document.documentElement; p = p.parentElement) {
    if (getComputedStyle(p).overflowX === 'visible') continue;
    const b = p.getBoundingClientRect();
    right = Math.min(right, b.right);
    left = Math.max(left, b.left);
  }
  const heads = [...el.querySelectorAll('th')];
  const row = el.querySelector('tr');
  const cells = heads.length ? heads : row ? [...row.children] : [];
  const text = c => (c.textContent || '').replace(/\s+/g, ' ').trim();
  const offscreen = cells.filter(c => { const r = c.getBoundingClientRect(); return r.width > 0 && (r.right > right + 1 || r.left < left - 1); }).map(text).filter(t => t);
  return {offscreen: offscreen.slice(0, 10)};
})"""


class CdpError(Authored, Exception):
    pass


class ShotError(ReadError):
    def __init__(self, message: str, status: int = 502):
        super().__init__(status, message)


class BrowserDown(ShotError):
    def __init__(self, message: str, code: int | None = None, reason: str = ''):
        super().__init__(message, 503)
        self.code = code
        self.reason = reason


@dataclass
class Picture:
    facts: dict
    data: bytes | None
    mime: str = 'image/jpeg'
    scrolled: list['Picture'] = field(default_factory=list)


def lookup_ip(host: str) -> str:
    found = addresses(host, LOOKUP_S)
    if not found:
        raise OSError('no address')
    return next((a for a in found if ':' not in a), found[0])


def bracketed(ip: str) -> str:
    return f'[{ip}]' if ':' in ip else ip


def endpoint(base_url: str, timeout: float, transport: Transport, lookup: Callable[[str], str] = lookup_ip) -> tuple[str, str]:
    parts = urlsplit(base_url)
    if parts.scheme.lower() != 'http':
        raise BrowserDown(NOT_HTTP, None, 'NotHttp')
    try:
        port = parts.port or DEFAULT_PORT
        ip = bracketed(lookup(parts.hostname or ''))
        reply = call(unproxied(transport), 'GET', f'http://{ip}:{port}/json/version', timeout=timeout)
    except HttpError as e:
        said = REFUSED if e.code in (401, 403) else DOWN
        raise BrowserDown(f'{said} (HTTP {e.code})', e.code, kind(e)) from None
    except Exception as e:
        raise BrowserDown(f'{DOWN} ({kind(e)})', None, kind(e)) from None
    info = reply.body if isinstance(reply.body, dict) else {}
    ws = urlsplit(str(info.get('webSocketDebuggerUrl') or ''))
    if not ws.path.startswith('/devtools/browser/'):
        raise BrowserDown('the browser did not name a DevTools endpoint', None, 'NoEndpoint')
    return f'ws://{ip}:{port}{ws.path}', str(info.get('Browser') or '')


class Connection:
    def __init__(self, ws, clock: Callable[[], float] = time.monotonic):
        self.ws = ws
        self.clock = clock
        self.next_id = 0
        self.handlers: dict[str, Callable[[dict, str | None], None]] = {}

    @classmethod
    def open(cls, url: str, timeout: float) -> 'Connection':
        try:
            return cls(connect(url, open_timeout=timeout, max_size=MAX_MESSAGE, close_timeout=2, legacy=True, proxy=None))
        except (OSError, WebSocketException) as e:
            raise BrowserDown(f'{DOWN} ({kind(e)})', None, kind(e)) from None

    def on(self, method: str, handler: Callable[[dict, str | None], None]) -> None:
        self.handlers[method] = handler

    def send(self, method: str, params: dict | None = None, session: str | None = None) -> int:
        self.next_id += 1
        out = {'id': self.next_id, 'method': method, 'params': params or {}}
        if session:
            out['sessionId'] = session
        try:
            self.ws.send(json.dumps(out))
        except (OSError, WebSocketException) as e:
            raise CdpError(f'the browser connection broke ({kind(e)})') from None
        return self.next_id

    def call(self, method: str, params: dict | None = None, session: str | None = None, deadline: float | None = None) -> dict:
        sent = self.send(method, params, session)
        answer: dict = {}
        self.pump(lambda: bool(answer), deadline if deadline is not None else self.clock() + 10, want=sent, into=answer)
        if not answer:
            raise CdpError(f'{method} timed out')
        if 'error' in answer:
            raise CdpError(f"{method}: {str((answer['error'] or {}).get('message') or 'error')[:200]}")
        return answer.get('result') or {}

    def pump(self, until: Callable[[], bool], deadline: float, want: int | None = None, into: dict | None = None) -> bool:
        while not until():
            left = deadline - self.clock()
            if left <= 0:
                return False
            try:
                raw = self.ws.recv(timeout=left)
            except TimeoutError:
                return until()
            except (OSError, WebSocketException) as e:
                raise CdpError(f'the browser connection broke ({kind(e)})') from None
            try:
                got = json.loads(raw)
            except ValueError:
                raise CdpError('the browser sent a frame the page check cannot read') from None
            if 'id' in got:
                if got['id'] == want and into is not None:
                    into.update(got)
                continue
            handler = self.handlers.get(got.get('method') or '')
            if handler:
                handler(got.get('params') or {}, got.get('sessionId'))
        return True

    def close(self) -> None:
        try:
            self.ws.close()
        except (OSError, WebSocketException):
            pass


def navigation_refusal(url: str, site: Site) -> str | None:
    why = refusal(url, site)
    if why or site.internal:
        return why
    host = (urlsplit(url).hostname or '').rstrip('.').lower()
    if site.exact(host):
        return None
    try:
        found = web.checked(host, web.resolve)
    except (OSError, UnicodeError):
        return f'{host} could not be looked up'
    return None if found is not None else f'{host} is not a public host: set Subdomains may be internal on the Website instance to open it'


def request_refusal(url: str, method: str, resource_type: str, site: Site) -> tuple[str, str] | None:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme in ('data', 'blob'):
        return None
    host = (parts.hostname or '').rstrip('.').lower()
    try:
        port = parts.port
    except ValueError:
        port = -1
    if scheme != 'https' or port not in (None, 443) or not allowed_host(host, site.hosts) or (site.login_hosts and under(host, site.login_hosts)):
        return 'otherHosts', host or url[:80]
    path = path_of(url)
    if hits(path, API + site.api_paths):
        return 'api', path
    if method.upper() not in READ_METHODS or resource_type in TRACKING_TYPES:
        return 'tracking', path
    if path.startswith('/cdn-cgi/') and not path.startswith(CDN_IMAGES):
        return 'tracking', path
    return None


def number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def verdict(item: object, vw: float) -> str | None:
    found = judge(item, vw)
    return None if found == 'cut' and isinstance(item, dict) and item.get('lost') is False else found


def judge(item: object, vw: float) -> str | None:
    if not isinstance(item, dict) or not isinstance(item.get('i'), int) or isinstance(item.get('i'), bool):
        return None
    sw, cw, x, w = (number(item.get(k)) for k in ('sw', 'cw', 'x', 'w'))
    if None in (sw, cw, x, w):
        return None
    ox, clip = str(item.get('ox') or 'visible'), str(item.get('clip') or '')
    if x + w <= 1 or x >= vw - 1:
        return None
    wide = sw > cw + 1
    pans = item.get('pans') is True
    if x + w > vw + 1 and clip not in ('auto', 'scroll'):
        return 'cut' if clip in ('hidden', 'clip') and not pans else 'page'
    if wide and ox in ('auto', 'scroll'):
        return 'scrolls'
    if wide and ox in ('hidden', 'clip'):
        return 'cut'
    if clip in ('auto', 'scroll'):
        return None
    if x + w > vw + 1 or (ox == 'visible' and x + sw > vw + 1):
        return 'cut' if clip in ('hidden', 'clip') and not pans else 'page'
    return None


def marks_of(marks: object) -> list:
    out: list = []
    for m in marks if isinstance(marks, (list, tuple)) else ():
        if isinstance(m, str) and 0 < len(m) <= SELECTOR_MAX:
            out.append(m)
        elif isinstance(m, dict) and all(number(m.get(k)) is not None for k in ('x', 'y', 'w', 'h')) and m['w'] > 0 and m['h'] > 0:
            out.append({k: m[k] for k in ('x', 'y', 'w', 'h')})
        if len(out) == MARKS_MAX:
            break
    return out


def box_of(item: dict) -> dict | None:
    found = {k: number(item.get(k)) for k in ('x', 'y', 'w', 'h')}
    return {k: round(v) for k, v in found.items()} if None not in found.values() and found['w'] > 0 and found['h'] > 0 else None


def scrolls_inside(item: dict) -> bool:
    sw, cw = number(item.get('sw')), number(item.get('cw'))
    return sw is not None and cw is not None and sw > cw + 1 and str(item.get('ox') or '') in ('auto', 'scroll')


def revealed_of(found: object, key: str = 'revealed') -> list[str]:
    texts = found.get(key) if isinstance(found, dict) else None
    out = []
    for t in texts if isinstance(texts, list) else ():
        text = ' '.join(t.split())[:REVEALED_TEXT] if isinstance(t, str) else ''
        if text and text not in out:
            out.append(text)
    return out[:REVEALED_MAX]


class Shot:
    def __init__(self, browser: 'Browser', url: str, site: Site, viewport: str, full: bool, probe: str | None = None, capture: bool = True, headers: dict[str, str] | None = None,
                 focus: str | None = None, mark: list | None = None, overflow: bool = False):
        self.browser = browser
        self.url = url
        self.site = site
        self.viewport = viewport
        self.full = full
        self.probe = probe
        self.capture = capture
        self.headers = headers or {}
        self.focus = focus
        self.mark = mark
        self.overflow = overflow
        self.blocked = dict.fromkeys(CATEGORIES, 0)
        self.hosts: set[str] = set()
        self.main: str | None = None
        self.session: str | None = None
        self.left: str | None = None
        self.committed = False
        self.loaded = False
        self.status: int | None = None
        self.final = url
        self.challenge_script = False
        self.mitigated = False
        self.dialogs: list[str] = []
        self.geometry: dict = {}
        self.cut_short = False

    def go_on(self, conn: Connection, rid: object, req: dict, url: str) -> None:
        names = {k.lower() for k in self.headers}
        if not names:
            conn.send('Fetch.continueRequest', {'requestId': rid}, self.session)
            return
        given = {str(k): str(v) for k, v in (req.get('headers') or {}).items() if str(k).lower() not in names}
        host = (urlsplit(url).hostname or '').rstrip('.').lower()
        sent = given | (self.headers if self.site.exact(host) else {})
        conn.send('Fetch.continueRequest', {'requestId': rid, 'headers': [{'name': k, 'value': v} for k, v in sent.items()]}, self.session)

    def dialog(self, conn: Connection, p: dict, session: str | None) -> None:
        if len(self.dialogs) < BLOCKED_HOSTS:
            self.dialogs.append(str(p.get('type') or 'alert')[:20])
        conn.send('Page.handleJavaScriptDialog', {'accept': False}, session or self.session)

    def paused(self, conn: Connection, p: dict) -> None:
        req = p.get('request') or {}
        url, method, rtype, rid = str(req.get('url') or ''), str(req.get('method') or 'GET'), str(p.get('resourceType') or ''), p.get('requestId')
        if rtype == 'Document' and p.get('frameId') == self.main:
            why = navigation_refusal(url, self.site)
            if why:
                if self.left is None:
                    lead = f'{self.url} redirected to {url}' if p.get('redirectedRequestId') else f'the page went to {url}' if self.committed else url
                    self.left = f'{lead}: {why}'
                conn.send('Fetch.failRequest', {'requestId': rid, 'errorReason': 'BlockedByClient'}, self.session)
                return
            if self.committed:
                self.loaded = False
            self.go_on(conn, rid, req, url)
            return
        found = request_refusal(url, method, rtype, self.site)
        if found is None and rtype == 'Document' and refusal(url, self.site):
            found = ('tracking', path_of(url))
        if found is None:
            self.go_on(conn, rid, req, url)
            return
        category, detail = found
        self.blocked[category] += 1
        if category == 'otherHosts':
            self.hosts.add(detail)
        if '/cdn-cgi/challenge-platform/' in url:
            self.challenge_script = True
        conn.send('Fetch.failRequest', {'requestId': rid, 'errorReason': 'BlockedByClient'}, self.session)

    def answered_from(self, p: dict) -> None:
        response = p.get('response') or {}
        url = str(response.get('url') or '')
        host = (urlsplit(url).hostname or '').rstrip('.').lower()
        ip = web.address_of(str(response.get('remoteIPAddress') or '').strip('[]'))
        if self.site.internal or not host or self.site.exact(host) or ip is None or web.global_address(ip) or self.left is not None:
            return
        self.left = f'{url}: {host} is not a public host: set Subdomains may be internal on the Website instance to open it'

    def responded(self, p: dict, _session) -> None:
        if p.get('type') == 'Document':
            self.answered_from(p)
        if p.get('type') == 'Document' and p.get('frameId') == self.main:
            response = p.get('response') or {}
            self.status = response.get('status')
            found = {str(k).lower(): str(v) for k, v in (response.get('headers') or {}).items()}
            self.mitigated = found.get('cf-mitigated', '').strip().lower() == 'challenge'

    def challenge(self, title: str) -> bool:
        return self.mitigated or bool(CHALLENGE_PAGE.search(f'<title>{title}')) or (self.challenge_script and self.status in (403, 503))

    def navigated(self, p: dict, _session) -> None:
        frame = p.get('frame') or {}
        if frame.get('id') == self.main and not frame.get('parentId'):
            self.final = str(frame.get('url') or self.final)
            self.committed = True

    def load(self, _p: dict, _session) -> None:
        self.loaded = True

    def gone(self) -> None:
        if self.left is not None:
            raise ShotError(self.left, 400)

    @staticmethod
    def value(conn: Connection, expression: str, session: str, deadline: float) -> object:
        found = conn.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True}, session, deadline)
        return None if found.get('exceptionDetails') else (found.get('result') or {}).get('value')

    def spot(self, conn: Connection, s: str, deadline: float, base: dict) -> dict | None:
        found = None
        if self.mark is not None:
            got = self.value(conn, f'{MARK}({json.dumps(marks_of(self.mark))})', s, deadline)
            found = got if isinstance(got, dict) else None
        if self.focus and found is None:
            got = self.value(conn, f'{FOCUS}({json.dumps(self.focus)})', s, deadline)
            base['focused'] = isinstance(got, dict)
            found = got if isinstance(got, dict) else None
        if self.mark is not None or self.focus:
            base['marked'] = found is not None
        return found

    def crop(self, spot: dict, content: int, height: int) -> tuple[int, int]:
        top = max(0, min(int(number(spot.get('y')) or 0) - FOCUS_ABOVE, content - FOCUS_HEIGHT))
        return top, min(FOCUS_HEIGHT, max(content, height))

    def image(self, conn: Connection, s: str, deadline: float, width: int, top: int, tall: int, beyond: bool) -> bytes:
        got = conn.call('Page.captureScreenshot', {'format': 'jpeg', 'quality': QUALITY, 'clip': {'x': 0, 'y': top, 'width': width, 'height': tall, 'scale': 1},
                                                   'captureBeyondViewport': beyond, 'fromSurface': True}, s, deadline)
        try:
            return base64.b64decode(got.get('data') or '')
        except ValueError:
            return b''

    def boxes(self, conn: Connection, s: str, deadline: float) -> list[tuple[int, str, bool]] | None:
        found = self.value(conn, f'{OVERFLOW}({OVERFLOW_MAX})', s, deadline)
        vw = number(found.get('vw')) if isinstance(found, dict) else None
        if vw is None or not isinstance(found.get('items'), list):
            return None
        judged = [(item['i'], v, scrolls_inside(item)) for item in found['items'] for v in (verdict(item, vw),) if v]
        self.cut_short = found.get('cutShort') is True
        self.geometry = {item['i']: g for item in found['items'] if isinstance(item, dict) for g in (box_of(item),) if g is not None}
        return judged[:OVERFLOW_MAX]

    def scrolls(self, conn: Connection, s: str, deadline: float, boxes: list[tuple[int, str, bool]], entries: list[dict], base: dict, shape: tuple[int, int, int],
                hidden: bool) -> list['Picture']:
        width, height, content = shape
        out: list[Picture] = []
        for (index, _, inner), entry in zip(boxes, entries):
            if not inner:
                continue
            if len(out) == SCROLL_SHOTS or deadline - time.monotonic() < SCROLL_MIN_S:
                break
            try:
                found = self.value(conn, f'{SCROLL}({index})', s, deadline)
                if not isinstance(found, dict):
                    continue
                entry['revealed'] = revealed_of(found)
                entry['offscreen'] = revealed_of(found, 'offscreen')
                if not hidden:
                    self.value(conn, PINNED, s, deadline)
                    hidden = True
                top, tall = self.crop(found, content, height)
                data = self.image(conn, s, deadline, width, top, tall, True)
            except CdpError:
                break
            if data and len(data) <= MAX_BYTES:
                kept = {k: x for k, x in base.items() if k not in ('focused', 'marked', 'pinnedHidden')}
                out.append(Picture(kept | {'full': False, 'cut': False, 'width': width, 'height': tall, 'marked': True, 'overflow': [entry]}, data))
        return out

    def names(self, conn: Connection, s: str, deadline: float, boxes: list[tuple[int, str, bool]], entries: list[dict]) -> None:
        for (index, judged, inner), entry in zip(boxes, entries):
            if judged != 'cut' or inner:
                continue
            try:
                found = self.value(conn, f'{NAMES}({index})', s, deadline)
            except CdpError:
                return
            entry['offscreen'] = revealed_of(found, 'offscreen')

    def take(self) -> Picture:
        b = self.browser
        width, height, mobile = VIEWPORTS[self.viewport]
        ws, version = endpoint(b.url, CONNECT_S, b.transport, b.resolve)
        conn = Connection.open(ws, CONNECT_S)
        version = version.split('/', 1)[-1] or '0'
        deadline = time.monotonic() + TIMEOUT_S
        context = None
        try:
            context = conn.call('Target.createBrowserContext', {'disposeOnDetach': True}, deadline=deadline)['browserContextId']
            target = conn.call('Target.createTarget', {'url': 'about:blank', 'browserContextId': context}, deadline=deadline)['targetId']
            self.session = s = conn.call('Target.attachToTarget', {'targetId': target, 'flatten': True}, deadline=deadline)['sessionId']
            conn.on('Fetch.requestPaused', lambda p, _: self.paused(conn, p))
            conn.on('Network.responseReceived', self.responded)
            conn.on('Page.frameNavigated', self.navigated)
            conn.on('Page.loadEventFired', self.load)
            conn.on('Page.javascriptDialogOpening', lambda p, session: self.dialog(conn, p, session))
            ua = (PHONE_UA if mobile else DESKTOP_UA).format(version=version, token=self.site.agent or user_agent())
            for method, params in (('Page.enable', {}), ('Network.enable', {}), ('Network.setBypassServiceWorker', {'bypass': True}),
                                   ('Emulation.setDeviceMetricsOverride', {'width': width, 'height': height, 'deviceScaleFactor': 1, 'mobile': mobile}),
                                   ('Emulation.setScrollbarsHidden', {'hidden': True}),
                                   ('Emulation.setUserAgentOverride', {'userAgent': ua}), ('Page.addScriptToEvaluateOnNewDocument', {'source': STUB})):
                conn.call(method, params, s, deadline)
            self.main = conn.call('Page.getFrameTree', {}, s, deadline)['frameTree']['frame']['id']
            conn.call('Fetch.enable', {'patterns': [{'urlPattern': '*'}]}, s, deadline)
            load_by = min(deadline, time.monotonic() + b.load_s())
            at = iso(b.wall())
            nav = conn.call('Page.navigate', {'url': self.url}, s, load_by)
            self.gone()
            if nav.get('errorText'):
                raise ShotError(f"{self.url} did not load: {str(nav['errorText'])[:200]}")
            conn.pump(lambda: self.loaded or self.left is not None, load_by)
            self.gone()
            conn.pump(lambda: self.left is not None, min(deadline, time.monotonic() + b.settle_s()))
            self.gone()
            if not self.loaded:
                conn.pump(lambda: self.loaded or self.left is not None, load_by)
                self.gone()
            title = str(self.value(conn, 'document.title', s, deadline) or '')
            base = {'url': self.url, 'at': at, 'finalUrl': self.final, 'viewport': self.viewport, 'blocked': dict(self.blocked),
                    'blockedHosts': sorted(self.hosts)[:BLOCKED_HOSTS]} | ({'dialogs': list(self.dialogs)} if self.dialogs else {})
            if self.challenge(title):
                return Picture(base | {'status': None, 'challenge': CHALLENGE}, None)
            if self.probe:
                base['probe'] = self.value(conn, self.probe, s, deadline)
            if not self.capture:
                return Picture(base | {'status': self.status, 'title': title[:TITLE_LIMIT], 'width': width}, None)
            metrics = conn.call('Page.getLayoutMetrics', {}, s, deadline)
            content = int(number((metrics.get('cssContentSize') or {}).get('height')) or height)
            boxes = self.boxes(conn, s, deadline) if self.overflow else None
            shot_height = min(max(content, height), MAX_HEIGHT) if self.full else height
            top, beyond = 0, self.full
            spot = self.spot(conn, s, deadline, base)
            if spot is not None:
                top, shot_height = self.crop(spot, content, height)
                beyond = True
            hidden_done = False
            if beyond:
                hidden = self.value(conn, PINNED, s, deadline)
                base['pinnedHidden'] = hidden if isinstance(hidden, int) and not isinstance(hidden, bool) else 0
                hidden_done = True
            data = self.image(conn, s, deadline, width, top, shot_height, beyond)
            if not data:
                raise ShotError('the browser returned no image')
            if len(data) > MAX_BYTES:
                raise ShotError(f'the image is {len(data) // 1024} KB, over the {MAX_BYTES // 1024} KB limit; take the first screen instead of the full page', 413)
            facts = base | {'status': self.status, 'title': title[:TITLE_LIMIT], 'full': self.full, 'cut': self.full and content > MAX_HEIGHT, 'width': width, 'height': shot_height}
            if boxes is None:
                return Picture(facts, data)
            entries = [{'verdict': judged, 'revealed': [], 'offscreen': []} | ({'box': self.geometry[i]} if judged in ('cut', 'page') and i in self.geometry else {}) for i, judged, _ in boxes]
            self.names(conn, s, deadline, boxes, entries)
            facts['overflow'] = entries
            if self.cut_short:
                facts['overflowCutShort'] = True
            shown = base | {'status': self.status, 'title': title[:TITLE_LIMIT]}
            return Picture(facts, data, scrolled=self.scrolls(conn, s, deadline, boxes, entries, shown, (width, height, content), hidden_done))
        except CdpError as e:
            raise ShotError(f'the browser failed: {e.text}') from None
        except (KeyError, TypeError, ValueError):
            raise ShotError(UNREADABLE) from None
        finally:
            if context:
                try:
                    conn.call('Target.disposeBrowserContext', {'browserContextId': context}, deadline=time.monotonic() + DISPOSE_S)
                except CdpError:
                    pass
            conn.close()


class Browser:
    def __init__(self, url: str, transport: Transport, gap: float | None = None, settle: float | None = None, load: float | None = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, wall: Callable[[], float] = time.time, resolve: Callable[[str], str] | None = None):
        self.url = (url or '').strip()
        self.transport = transport
        self.gap = gap
        self.settle = settle
        self.load = load
        self.clock = clock
        self.sleep = sleep
        self.wall = wall
        self.resolve = resolve or lookup_ip
        self.lock = threading.Lock()
        self.last: float | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def gap_s(self) -> float:
        return GAP_S if self.gap is None else self.gap

    def settle_s(self) -> float:
        return SETTLE_S if self.settle is None else self.settle

    def load_s(self) -> float:
        return LOAD_S if self.load is None else self.load

    def version(self) -> str:
        return endpoint(self.url, CONNECT_S, self.transport, self.resolve)[1]

    def shoot(self, url: str, site: Site, viewport: str = 'desktop', full: bool = False, probe: str | None = None, capture: bool = True, headers: dict[str, str] | None = None,
              focus: str | None = None, mark: list | None = None, overflow: bool = False) -> Picture:
        if not self.enabled:
            raise ShotError(OFF, 503)
        if viewport not in VIEWPORTS:
            raise ShotError(f"viewport is one of {', '.join(VIEWPORTS)}", 400)
        why = navigation_refusal(url, site)
        if why:
            raise ShotError(why, 400)
        with self.lock:
            if self.last is not None:
                wait = self.last + self.gap_s() - self.clock()
                if wait > 0:
                    self.sleep(wait)
            self.last = self.clock()
            return Shot(self, clean(url), site, viewport, bool(full), probe, capture, headers, focus, mark, overflow).take()
