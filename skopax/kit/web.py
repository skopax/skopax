import html
import ipaddress
import itertools
import posixpath
import re
import ssl
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlsplit

from skopax.contract import ReadError, Transport, iso
from skopax.errkind import kind
from skopax.kit.connection import causes
from skopax.messages import msg
from skopax.remote import Unreachable, addresses, call_page

TRACKING = re.compile(r'utm_.*|gclid|fbclid|msclkid')
HOST = re.compile(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}')
API = ('api',)
ACCEPT = 'text/html,application/xhtml+xml;q=0.9,*/*;q=0.8'
KEPT_HEADERS = ('cache-control', 'cf-cache-status', 'age', 'cf-ray', 'content-type', 'content-length', 'location', 'server')
BODY_LIMIT = 2 * 1024 * 1024
TIMEOUT = 10.0
MAX_HOPS = 3
SPACING_S = 1.0
FIND_LIMIT = 200
EXCERPTS = 3
EXCERPT = 200
MATCH_LIMIT = 1000
TEXT_LIMIT = 300
LIST_LIMIT = 20
ELLIPSIS = '\u2026'
CHALLENGE = 'Cloudflare challenged this request'
CHALLENGE_PAGE = re.compile(r'<title>\s*Just a moment|/cdn-cgi/challenge-platform/', re.I)
CHARSET = re.compile(r'charset=["\']?([\w.:-]+)', re.I)
URL_SAFE = "/%:@&=+$,;~!*'()?"
SLASHES = re.compile(r'/{2,}')
PINS = 3
HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,100}")
USER_AGENT = re.compile(r'[\x20-\x7e]{1,200}')
NEVER_KEPT = frozenset({'set-cookie', 'set-cookie2', 'cookie', 'authorization', 'proxy-authorization'})
OPTION_MAX = 20
SECRETISH = re.compile(r'token|secret|session|auth|cookie|password|credential', re.I)
INTERNAL_NAMES = ('.internal', '.local', '.localhost', '.svc', '.cluster.local', '.home.arpa')
NAT64 = ipaddress.ip_network('64:ff9b::/96')
COMPATIBLE = ipaddress.ip_network('::/96')
SUFFIXES = frozenset({'co.uk', 'org.uk', 'me.uk', 'ltd.uk', 'plc.uk', 'net.uk', 'ac.uk', 'gov.uk', 'com.au', 'net.au', 'org.au', 'edu.au', 'gov.au', 'co.nz', 'net.nz',
                      'org.nz', 'co.jp', 'ne.jp', 'or.jp', 'ac.jp', 'go.jp', 'co.kr', 'or.kr', 'com.br', 'net.br', 'org.br', 'com.cn', 'net.cn', 'org.cn', 'com.tr', 'net.tr',
                      'org.tr', 'com.mx', 'co.za', 'org.za', 'com.sg', 'com.hk', 'com.tw', 'co.in', 'net.in', 'org.in', 'co.id', 'com.my', 'com.ph', 'com.vn', 'com.ar',
                      'com.co', 'co.il', 'com.ua', 'com.pl', 'co.at', 'com.es', 'com.pt', 'com.gr', 'co.th', 'com.sa', 'com.eg', 'github.io', 'herokuapp.com', 'vercel.app',
                      'netlify.app', 'pages.dev', 'azurewebsites.net', 'cloudfront.net', 'appspot.com', 'blogspot.com', 'firebaseapp.com', 'web.app', 'workers.dev'})


class PageError(ReadError):
    pass


@dataclass(frozen=True)
class Page:
    status: int
    headers: dict[str, str]
    body: bytes
    timings: dict | None = None


@dataclass(frozen=True)
class Site:
    hosts: tuple[str, ...]
    login_hosts: tuple[str, ...] = ()
    login_paths: tuple[str, ...] = ()
    api_paths: tuple[str, ...] = ()
    refused_paths: tuple[str, ...] = ()
    internal: bool = False
    tracking: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    skip: tuple[str, ...] = ()
    agent: str = ''

    @classmethod
    def of(cls, config: Mapping[str, str]) -> 'Site':
        base = host_of(config.get('url') or '')
        hosts = tuple(dict.fromkeys(([base] if base else []) + [h for h in host_list(config.get('hosts')) if h not in SUFFIXES]))
        return cls(hosts, tuple(host_list(config.get('login-hosts'))), path_list(config.get('login-paths')), path_list(config.get('api-paths')),
                   path_list(config.get('refused-paths')), str(config.get('internal-subdomains') or '').strip().lower() == 'true',
                   name_list(config.get('tracking-params'), param_ok), header_list(config.get('kept-headers')), prefix_list(config.get('scan-skip')), agent_of(config.get('user-agent')))

    def exact(self, host: str) -> bool:
        return str(host or '').rstrip('.').lower() in self.hosts


def host_of(url: str) -> str:
    try:
        host = (urlsplit(str(url or '').strip()).hostname or '').rstrip('.').lower()
    except ValueError:
        return ''
    return host if HOST.fullmatch(host) else ''


def address_of(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(str(text).split('%')[0])
    except ValueError:
        return None


def embedded(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if not isinstance(ip, ipaddress.IPv6Address):
        return None
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip in NAT64 or (ip in COMPATIBLE and int(ip) > 1):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def global_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None) -> bool:
    if ip is None or not ip.is_global:
        return False
    inner = embedded(ip)
    return inner is None or inner.is_global


def checked(host: str, lookup: Callable[[str], list[str]] | None = None) -> list[str] | None:
    name = str(host or '').strip().strip('[]').rstrip('.').lower()
    literal = address_of(name)
    if literal is not None:
        return [str(literal)] if global_address(literal) else None
    if '.' not in name or name.endswith(INTERNAL_NAMES):
        return None
    if lookup is None:
        return []
    found = [address_of(a) for a in lookup(name)]
    return [str(a) for a in found] if found and all(global_address(a) for a in found) else None


def public_host(host: str, lookup: Callable[[str], list[str]] | None = None) -> bool:
    return checked(host, lookup) is not None


def resolve(host: str) -> list[str]:
    return addresses(host)


def suffix_entries(raw: object) -> list[str]:
    return [h for h in host_list(raw) if h in SUFFIXES]


def host_list(raw: object) -> list[str]:
    found = []
    for item in str(raw or '').split(','):
        host = item.strip().lower().lstrip('*').lstrip('.').rstrip('.')
        if HOST.fullmatch(host) and host not in found:
            found.append(host)
    return found[:LIST_LIMIT]


def path_list(raw: object) -> tuple[str, ...]:
    found = []
    for item in str(raw or '').split(','):
        text = decoded(item.strip())
        path = normal(text.strip('/')) if text.startswith('/') or '/' in text.strip('/') else text
        if path and path not in ('/', '.') and path not in found:
            found.append(path)
    return tuple(found[:LIST_LIMIT])


def param_ok(name: str) -> bool:
    return bool(re.fullmatch(r'[a-z0-9_.\-]{1,60}\*?', name))


def name_list(raw: object, ok: Callable[[str], bool]) -> tuple[str, ...]:
    found = []
    for item in str(raw or '').split(','):
        name = item.strip().lower()
        if name and ok(name) and name not in found:
            found.append(name)
    return tuple(found[:OPTION_MAX])


def header_list(raw: object) -> tuple[str, ...]:
    return name_list(raw, lambda n: HEADER_NAME.fullmatch(n) is not None and n not in NEVER_KEPT and SECRETISH.search(n) is None)


def prefix_list(raw: object) -> tuple[str, ...]:
    found = []
    for item in str(raw or '').split(','):
        text = decoded(item.strip())
        path = normal(text.strip('/')) if text.strip('/') else ''
        if path and path != '/' and path not in found:
            found.append(path)
    return tuple(found[:OPTION_MAX])


def agent_of(raw: object) -> str:
    text = str(raw or '').strip()
    return text if USER_AGENT.fullmatch(text) else ''


def decoded(text: str) -> str:
    for _ in range(3):
        once = unquote(text)
        if once == text:
            break
        text = once
    return text.lower()


def allowed_host(host: str, hosts: tuple[str, ...]) -> bool:
    host = host.lower().rstrip('.')
    return bool(HOST.fullmatch(host)) and any(host == z or host.endswith('.' + z) for z in hosts)


def under(host: str, hosts: tuple[str, ...]) -> bool:
    return any(host == h or host.endswith('.' + h) for h in hosts)


def normal(path: str) -> str:
    bare = '/'.join(s.split(';')[0] for s in path.replace('\\', '/').split('/'))
    return posixpath.normpath(SLASHES.sub('/', '/' + bare))


def path_of(url: str) -> str:
    return normal(decoded(urlsplit(url).path) or '/')


def segments_of(path: str) -> set[str]:
    return {s.split(';')[0] for s in path.replace('\\', '/').split('/')}


def hits(path: str, items: tuple[str, ...]) -> bool:
    parts = segments_of(path)
    return any(path == i or path.startswith(i + '/') if i.startswith('/') else i in parts for i in items)


def shown(items: tuple[str, ...]) -> str:
    return ', '.join(i if i.startswith('/') else f'/{i}/' for i in items)


def refusal(url: str, site: Site) -> str | None:
    parts = urlsplit(url)
    if parts.scheme.lower() != 'https':
        return 'https only'
    if parts.username is not None or parts.password is not None:
        return 'no user or password in the url'
    try:
        port = parts.port
    except ValueError:
        return 'port 443 only'
    if port not in (None, 443):
        return 'port 443 only'
    host = (parts.hostname or '').rstrip('.')
    if not site.hosts:
        return 'no site is set up: the url field is empty'
    if not allowed_host(host, site.hosts):
        return f"{host or 'the host'} is not one of the site hosts ({', '.join(site.hosts)} and their subdomains)"
    if site.login_hosts and under(host.lower(), site.login_hosts):
        return f"login pages are refused ({', '.join(site.login_hosts)}{', ' + shown(site.login_paths) if site.login_paths else ''})"
    path = path_of(url)
    if hits(path, API + site.api_paths):
        return f'API paths are refused ({shown(API + site.api_paths)}): token mints and API calls'
    if site.login_paths and hits(path, site.login_paths):
        return f'login pages are refused ({shown(site.login_paths)})'
    if site.refused_paths and hits(path, site.refused_paths):
        return f'pages whose every load counts as a click are refused ({shown(site.refused_paths)})'
    tracked = sorted({decoded(k) for k, _ in parse_qsl(parts.query, keep_blank_values=True) if tracking(decoded(k), site.tracking)})
    if tracked:
        return 'ad-tracking parameters are refused: ' + ', '.join(tracked)
    return None


def tracking(name: str, extra: tuple[str, ...] = ()) -> bool:
    return TRACKING.fullmatch(name) is not None or any(name == x or (x.endswith('*') and name.startswith(x[:-1])) for x in extra)


def clean(url: str) -> str:
    parts = urlsplit(url)
    return f"https://{(parts.hostname or '').rstrip('.')}{quote(parts.path or '/', safe=URL_SAFE)}" + (f'?{quote(parts.query, safe=URL_SAFE)}' if parts.query else '')


def user_agent() -> str:
    return ' '.join(msg('website.user_agent').split())


def headers(agent: str = '') -> dict[str, str]:
    return {'User-Agent': agent or user_agent(), 'Accept': ACCEPT, 'Accept-Encoding': 'identity'}


def reason(error: BaseException) -> str:
    chain = causes(error)
    if any(isinstance(x, ssl.SSLError) for x in chain):
        return 'the TLS handshake failed'
    if any(isinstance(x, TimeoutError) for x in chain):
        return f'no answer within {TIMEOUT:g} s'
    return f'could not be reached ({kind(error)})'


def cut(text: str | None) -> str | None:
    text = ' '.join((text or '').split())
    return (text[:TEXT_LIMIT] + ELLIPSIS if len(text) > TEXT_LIMIT else text) or None


class Facts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found: dict[str, str | None] = {}
        self.inside: str | None = None
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): v or '' for k, v in attrs}
        if tag in ('title', 'h1') and tag not in self.found and self.inside is None:
            self.inside, self.text = tag, []
        elif tag == 'meta' and a.get('name', '').lower() == 'robots':
            self.found.setdefault('robots', cut(a.get('content')))
        elif tag == 'link' and 'canonical' in a.get('rel', '').lower().split():
            self.found.setdefault('canonical', cut(a.get('href')))

    def handle_endtag(self, tag):
        if tag == self.inside:
            self.found[tag] = cut(''.join(self.text))
            self.inside = None

    def handle_data(self, data):
        if self.inside:
            self.text.append(data)


def text_of(page: Page, body: bytes) -> str:
    m = CHARSET.search(page.headers.get('content-type', ''))
    try:
        return body.decode(m.group(1) if m else 'utf-8', 'replace')
    except LookupError:
        return body.decode('utf-8', 'replace')


def is_html(page: Page) -> bool:
    found = page.headers.get('content-type', '').lower()
    return not found or 'html' in found


def challenged(page: Page) -> bool:
    if page.headers.get('cf-mitigated', '').strip().lower() == 'challenge':
        return True
    return page.status in (403, 503) and bool(CHALLENGE_PAGE.search(page.body[:BODY_LIMIT].decode('utf-8', 'replace')))


def body_facts(page: Page, text: str) -> dict:
    out = {'bytes': min(len(page.body), BODY_LIMIT)} | ({'cut': True} if len(page.body) > BODY_LIMIT else {})
    if is_html(page):
        facts = Facts()
        facts.feed(text)
        out |= {k: facts.found[k] for k in ('title', 'robots', 'canonical', 'h1') if facts.found.get(k)}
    return out


def find_in(text: str, needle: str, cut_short: bool) -> dict:
    body = html.unescape(text)
    found = list(itertools.islice(re.finditer(re.escape(needle), body, re.I), MATCH_LIMIT))
    excerpts = []
    for m in found[:EXCERPTS]:
        start = max(0, m.start() - max(0, EXCERPT - len(m.group())) // 2)
        excerpts.append(' '.join(body[start:start + EXCERPT].split()))
    out = {'text': needle, 'found': bool(found), 'matches': len(found), 'excerpts': excerpts}
    return out | ({'note': 'searched the first 2 MB of the body only'} if cut_short and not found else {})


def hop(url: str, page: Page) -> dict:
    out = {'url': url, 'status': page.status}
    if 300 <= page.status < 400 and page.headers.get('location'):
        out['location'] = page.headers['location']
    return out | ({'timings': page.timings} if page.timings else {})


def kept(found: dict[str, str], extra: tuple[str, ...] = ()) -> dict[str, str]:
    return {k: found[k] for k in (*KEPT_HEADERS, *(x for x in extra if x not in KEPT_HEADERS)) if k in found}


class Pacer:
    def __init__(self, gap: float | None = None, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.gap = gap
        self.clock = clock
        self.sleep = sleep
        self.lock = threading.Lock()
        self.last: float | None = None

    def reserve(self) -> float:
        gap = SPACING_S if self.gap is None else self.gap
        with self.lock:
            now = self.clock()
            start = now if self.last is None else max(now, self.last + gap)
            self.last = start
        return start - now

    def wait(self) -> None:
        left = self.reserve()
        if left > 0:
            self.sleep(left)

    def done(self) -> None:
        with self.lock:
            now = self.clock()
            self.last = now if self.last is None else max(self.last, now)


_pacers: dict[str, Pacer] = {}
_pacers_guard = threading.Lock()


def pacer_for(key: str) -> Pacer:
    with _pacers_guard:
        return _pacers.setdefault(key, Pacer())


def site_pacer(url: str) -> Pacer:
    return pacer_for(f"website:{clean(str(url or '').strip())}")


class PageCheck:
    def __init__(self, transport: Transport, site: Site, clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time, pacer: Pacer | None = None,
                 lookup: Callable[[str], list[str]] | None = None, extra: Mapping[str, str] | None = None):
        self.transport = transport
        self.site = site
        self.clock = clock
        self.wall = wall
        self.pacer = pacer or Pacer(clock=clock)
        self.lookup = lookup
        self.extra = dict(extra or {})

    def public(self, url: str) -> list[str | None]:
        host = (urlsplit(url).hostname or '').rstrip('.')
        lookup = self.lookup
        if lookup is None:
            if self.site.internal or self.site.exact(host):
                return [None]
            lookup = resolve
        try:
            found = checked(host, lookup)
        except (OSError, UnicodeError) as e:
            why = reason(e)
            raise PageError(502, f'{host or url} did not answer: {why}') from None
        if found is None:
            raise PageError(400, f'{host or url} is not a public host' + ('' if self.lookup is not None else ': set Subdomains may be internal on the Website instance to open it'))
        return found[:PINS] or [None]

    def fetch(self, url: str, pin: str | None = None) -> tuple[Page, str]:
        self.pacer.wait()
        at = iso(self.wall())
        started = self.clock()
        try:
            own = self.site.exact(urlsplit(url).hostname or '')
            reply = call_page(self.transport, url, headers(self.site.agent) | (self.extra if own else {}), TIMEOUT, BODY_LIMIT, pin)
        finally:
            self.pacer.done()
        ms = round((self.clock() - started) * 1000)
        return Page(reply.status or 0, dict(reply.headers), reply.body, {'totalMs': ms}), at

    def _fetched(self, url: str) -> tuple[Page, str]:
        pins = self.public(url)
        try:
            for pin in pins[:-1]:
                try:
                    return self.fetch(url, pin)
                except Unreachable:
                    continue
            return self.fetch(url, pins[-1])
        except Exception as e:
            why = reason(e)
            raise PageError(502, f"{urlsplit(url).hostname or url} did not answer: {why}") from None

    def get(self, url: str, find: str | None = None) -> dict:
        problem = refusal(url, self.site)
        if problem:
            raise PageError(400, problem)
        current = clean(url)
        page, at = self._fetched(current)
        out: dict = {'url': current, 'at': at}
        chain: list[dict] = []
        while True:
            if challenged(page):
                chain.append({'url': current, 'challenge': f'HTTP {page.status}'})
                return out | {'status': None, 'challenge': f"{CHALLENGE} (HTTP {page.status}): that is the answer of Cloudflare to the page check, not the page's status",
                              'chain': chain, 'headers': kept(page.headers, self.site.kept)}
            chain.append(hop(current, page))
            location = chain[-1].get('location')
            if not location:
                break
            target = urljoin(current, location)
            problem = refusal(target, self.site)
            if problem:
                out['stopped'] = f'redirect to {target} not fetched: {problem}'
                break
            if len(chain) > MAX_HOPS:
                out['stopped'] = f'more than {MAX_HOPS} redirects: {target} not fetched'
                break
            try:
                nxt, _ = self._fetched(clean(target))
            except PageError as e:
                out['stopped'] = f'the request to {clean(target)} failed: {e.message}'
                break
            current, page = clean(target), nxt
        text = text_of(page, page.body[:BODY_LIMIT])
        out |= {'status': page.status, 'chain': chain, 'headers': kept(page.headers, self.site.kept)} | ({'timings': page.timings} if page.timings else {})
        out['body'] = body_facts(page, text)
        if find:
            out['find'] = find_in(text, find, len(page.body) > BODY_LIMIT)
        return out
