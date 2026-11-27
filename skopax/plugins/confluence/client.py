import base64
import datetime as dt
import re
import threading
from collections.abc import Iterator, Mapping
from html.parser import HTMLParser
from urllib.parse import quote, urlencode

from skopax.contract import DocRef, DocText, HttpError, ReadError, Transport, Unreachable, call, send
from skopax.messages import msg
from skopax.redact import HTML_BLOCKS, HTML_CELLS, html_columns_masked, redact_secrets

GATEWAY = 'https://api.atlassian.com/ex/confluence'
TENANT_INFO = '/_edge/tenant_info'
TENANT_TIMEOUT_S = 15
CALL_TIMEOUT_S = 30
OPS = ('search', 'page')
DENIED = (401, 403)
PERMISSIONS = {'search results': 'the scope search:confluence', 'pages': 'the scope read:page:confluence', 'spaces': 'the scope read:space:confluence'}
NO_SPACES = 'no Confluence space is allowed yet (the Spaces field is empty), so this service reads none'
NO_CLOUD = 'tenant_info answered without a cloudId'
UNREACHABLE = 'Confluence could not be reached'
SPACES = '/api/v2/spaces'
SPACES_MAX = 250
SCOPE_MISMATCH = 'scope does not match'
TEXT_MIN = 2
TEXT_LIMIT = 200
HITS = 10
EXCERPT_LIMIT = 300
BODY_LIMIT = 12_000
LIST_PAGE = 50
LIST_OVERLAP = dt.timedelta(days=1)
SPACE_KEY = re.compile(r'~?[A-Za-z0-9]\w{0,99}', re.ASCII)
PAGE_ID = re.compile(r'\d{1,20}')
HIGHLIGHT = re.compile(r'@@@(?:end)?hl@@@')
CLOUD_IDS: dict[str, str] = {}
SPACE_KEYS: dict[tuple[str, str], str] = {}
CLOUD_GUARD = threading.Lock()


class Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.tables = 0
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def _out(self, text: str) -> None:
        (self.cell if self.cell is not None else self.parts).append(text)

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.tables += 1
        elif tag == 'tr' and self.tables:
            self.row = []
        elif tag in HTML_CELLS and self.row is not None:
            self.cell = []
        elif tag in HTML_BLOCKS:
            self._out('\n')

    def handle_endtag(self, tag):
        if tag in HTML_CELLS and self.row is not None and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split()))
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            self.parts.append('\n' + ' | '.join(self.row) + '\n')
            self.row = None
        elif tag == 'table' and self.tables:
            self.tables -= 1
            self.parts.append('\n')
        elif tag in HTML_BLOCKS:
            self._out('\n')

    def handle_data(self, data):
        self._out(data)

    def unknown_decl(self, data):
        if data.startswith('CDATA['):
            self._out('\n' + data[6:] + '\n')


def plain(markup: str) -> str:
    reader = Text()
    reader.feed(html_columns_masked(markup or ''))
    reader.close()
    lines = [' '.join(line.split()) for line in ''.join(reader.parts).splitlines()]
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()


def masked(text: str) -> str:
    return redact_secrets(text)


def cql_text(value: str) -> str:
    return value.replace('\\', '\\\\').replace('"', '\\"')


def search_cql(text: str, space: str | None) -> str:
    cql = f'type = page AND text ~ "{cql_text(text)}"'
    return cql + (f' AND space = "{space}"' if space else '')


def since_cql(since: str | None) -> str:
    try:
        when = dt.datetime.fromisoformat(str(since or '').strip().replace('Z', '+00:00'))
    except ValueError:
        return ''
    when = when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)
    return f' AND lastmodified > "{(when.astimezone(dt.timezone.utc) - LIST_OVERLAP).strftime("%Y-%m-%d %H:%M")}"'


def denied_text(part: str, status: int, checked: bool = False) -> str:
    return msg('confluence.denied_invalid' if status == 401 and not checked else 'confluence.denied', part=part, status=status, permission=PERMISSIONS[part])


def spaces_unseen(keys) -> str:
    return msg('confluence.spaces_unseen', spaces=', '.join(keys))


def in_spaces(spaces: tuple[str, ...]) -> str:
    return 'space in (' + ', '.join(f'"{x}"' for x in spaces) + ')'


def spaces_query(keys: tuple[str, ...]) -> dict:
    return ({'keys': ','.join(keys)} if keys else {}) | {'limit': min(max(1, len(keys)), SPACES_MAX)}


def scope_mismatch(body: str) -> bool:
    return SCOPE_MISMATCH in str(body or '').lower()


def space_keys(body: object) -> set[str]:
    found = body.get('results') if isinstance(body, dict) else None
    return {str(s.get('key') or '').upper() for s in found or [] if isinstance(s, dict)}


def tenant(transport: Transport, site: str, timeout: float = TENANT_TIMEOUT_S) -> str:
    body = call(transport, 'GET', f'{site}{TENANT_INFO}', timeout=timeout).body
    cloud = body.get('cloudId') if isinstance(body, dict) else None
    if not cloud:
        raise ReadError(502, NO_CLOUD)
    return str(cloud)


def basic(email: str, token: str) -> dict[str, str]:
    return {'Authorization': 'Basic ' + base64.b64encode(f'{email}:{token}'.encode()).decode()}


def allowed_only(spaces: tuple[str, ...]) -> str:
    return f"this service reads only the spaces {', '.join(spaces)}"


class Confluence:
    def __init__(self, site: str, email: str, token: str, spaces: tuple[str, ...] = (), transport: Transport = send):
        self.site = site.rstrip('/')
        self.email = email
        self.token = token
        self.transport = transport
        self.spaces = tuple(x.strip().upper() for x in spaces if x.strip())

    @classmethod
    def of(cls, config: Mapping[str, str], transport: Transport) -> 'Confluence':
        return cls(config.get('url') or '', config.get('email') or '', config.get('api-token') or '', tuple((config.get('spaces') or '').split(',')), transport)

    @property
    def enabled(self) -> bool:
        return bool(self.site and self.email and self.token and self.spaces)

    @property
    def wiki(self) -> str:
        return f'{self.site}/wiki'

    def _cloud(self) -> str:
        with CLOUD_GUARD:
            known = CLOUD_IDS.get(self.site)
        if known:
            return known
        try:
            cloud = tenant(self.transport, self.site)
        except HttpError as e:
            raise ReadError(502, f'Confluence cloud id: HTTP {e.code} from tenant_info') from None
        except Unreachable:
            raise ReadError(502, UNREACHABLE) from None
        with CLOUD_GUARD:
            CLOUD_IDS[self.site] = cloud
        return cloud

    def _get(self, path: str, part: str, **params) -> dict:
        base = f'{GATEWAY}/{quote(self._cloud(), safe="")}/wiki'
        url = f'{base}{path}' + (f'?{urlencode(params)}' if params else '')
        try:
            body = call(self.transport, 'GET', url, basic(self.email, self.token), timeout=CALL_TIMEOUT_S).body
        except HttpError as e:
            if e.code in DENIED:
                raise ReadError(e.code, denied_text(part, e.code)) from None
            if e.code == 404:
                raise ReadError(404, msg('confluence.nothing_at', path=path)) from None
            raise ReadError(502, f'Confluence HTTP {e.code}') from None
        except Unreachable:
            raise ReadError(502, UNREACHABLE) from None
        return body if isinstance(body, dict) else {}

    def query(self, text: str, space: str | None) -> str:
        text = ' '.join(str(text or '').split())
        if len(text) < TEXT_MIN or len(text) > TEXT_LIMIT:
            raise ReadError(400, f'text must be {TEXT_MIN}-{TEXT_LIMIT} characters')
        key = str(space or '').strip() or None
        if key and not SPACE_KEY.fullmatch(key):
            raise ReadError(400, 'space must be a Confluence space key such as OPS or ~5f1a2b')
        if not self.spaces:
            raise ReadError(403, NO_SPACES)
        if key and key.upper() not in self.spaces:
            raise ReadError(403, allowed_only(self.spaces))
        return search_cql(text, key) if key else search_cql(text, None) + ' AND ' + in_spaces(self.spaces)

    def unseen(self, keys: tuple[str, ...]) -> tuple[str, ...]:
        seen = space_keys(self._get(SPACES, 'spaces', **spaces_query(keys)))
        return tuple(k for k in keys if k not in seen)

    def search(self, text: str, space: str | None) -> dict:
        cql = self.query(text, space)
        body = self._get('/rest/api/search', 'search results', cql=cql, limit=HITS, excerpt='indexed', expand='content.space,content.version')
        hits = []
        for r in (body.get('results') or [])[:HITS]:
            content = r.get('content') or {}
            space_of = (content.get('space') or {}).get('key') or str((r.get('resultGlobalContainer') or {}).get('displayUrl') or '').rsplit('/', 1)[-1] or None
            webui = (content.get('_links') or {}).get('webui') or r.get('url') or ''
            hits.append({'id': content.get('id'), 'title': masked(str(content.get('title') or r.get('title') or ''))[:200], 'space': space_of,
                         'lastModified': (content.get('version') or {}).get('when') or r.get('lastModified'),
                         'url': f'{self.wiki}{webui}' if webui.startswith('/') else None,
                         'excerpt': masked(plain(HIGHLIGHT.sub('', str(r.get('excerpt') or ''))))[:EXCERPT_LIMIT]})
        out = {'cql': cql, 'hits': hits, 'total': body.get('totalSize', len(hits))}
        if hits:
            return out
        wanted = (str(space).strip().upper(),) if str(space or '').strip() else self.spaces
        try:
            hidden = self.unseen(wanted)
        except ReadError as e:
            return out | ({'note': e.message} if e.status in DENIED else {})
        return out | ({'note': spaces_unseen(hidden)} if hidden else {})

    def listing(self, since: str | None, limit: int) -> Iterator[DocRef]:
        if not self.spaces:
            raise ReadError(403, NO_SPACES)
        cql = f'type = page AND {in_spaces(self.spaces)}{since_cql(since)} ORDER BY lastmodified'
        start, seen = 0, 0
        while seen < limit + 1:
            body = self._get('/rest/api/search', 'search results', cql=cql, limit=min(LIST_PAGE, limit + 1 - seen), start=start, expand='content.space,content.version')
            found = [r for r in body.get('results') or [] if isinstance(r, dict)]
            for r in found:
                content = r.get('content') or {}
                space = str((content.get('space') or {}).get('key') or '').strip()
                pid = str(content.get('id') or '')
                if not PAGE_ID.fullmatch(pid) or not SPACE_KEY.fullmatch(space) or space.upper() not in self.spaces:
                    continue
                webui = (content.get('_links') or {}).get('webui') or ''
                yield DocRef(pid, masked(str(content.get('title') or ''))[:200], f'{self.wiki}{webui}' if webui.startswith('/') else None,
                             (content.get('version') or {}).get('when') or r.get('lastModified'), space)
            seen += len(found)
            start += len(found)
            if not found:
                return

    def document(self, page_id: str) -> DocText:
        found = self.page(page_id)
        return DocText(str(found.get('title') or ''), found.get('url'), str(found.get('text') or ''))

    @staticmethod
    def page_id(value: str) -> str:
        pid = str(value or '').strip()
        if not PAGE_ID.fullmatch(pid):
            raise ReadError(400, 'id must be a numeric Confluence page id (search gives it)')
        return pid

    def space_key(self, space_id: str) -> str | None:
        if not PAGE_ID.fullmatch(space_id):
            return None
        with CLOUD_GUARD:
            known = SPACE_KEYS.get((self.site, space_id))
        if known:
            return known
        key = self._get(f'/api/v2/spaces/{space_id}', 'spaces').get('key')
        if not key:
            return None
        with CLOUD_GUARD:
            SPACE_KEYS[(self.site, space_id)] = str(key)
        return str(key)

    def page(self, page_id: str) -> dict:
        pid = self.page_id(page_id)
        if not self.spaces:
            raise ReadError(403, NO_SPACES)
        found = self._get('/rest/api/search', 'search results', cql=f'id = {pid}', limit=1, expand='content.body.storage,content.space,content.version')
        content = next((r.get('content') or {} for r in found.get('results') or [] if str((r.get('content') or {}).get('id')) == pid), None)
        if content is None:
            raise ReadError(404, msg('confluence.no_page', page=pid))
        markup = ((content.get('body') or {}).get('storage') or {}).get('value')
        if markup is None:
            return self.page_v2(pid)
        version = content.get('version') or {}
        return self.shaped(pid, content, (content.get('space') or {}).get('key'), version.get('number'), version.get('when'), markup)

    def page_v2(self, pid: str) -> dict:
        body = self._get(f'/api/v2/pages/{quote(pid)}', 'pages', **{'body-format': 'storage'})
        version = body.get('version') or {}
        space = self.space_key(str(body.get('spaceId') or ''))
        return self.shaped(pid, body, space, version.get('number'), version.get('createdAt'), ((body.get('body') or {}).get('storage') or {}).get('value') or '')

    def shaped(self, pid: str, content: dict, space: str | None, number, when, markup: str) -> dict:
        if not space:
            raise ReadError(403, msg('confluence.space_unread', page=pid, permission=PERMISSIONS['spaces']))
        if str(space or '').upper() not in self.spaces:
            raise ReadError(403, f'page {pid} is in space {space}; {allowed_only(self.spaces)}')
        text = masked(plain(markup))
        webui = (content.get('_links') or {}).get('webui') or ''
        out = {'id': content.get('id') or pid, 'title': masked(str(content.get('title') or ''))[:200], 'space': space, 'version': number, 'lastModified': when,
               'url': f'{self.wiki}{webui}' if webui.startswith('/') else None, 'text': text[:BODY_LIMIT]}
        return out | ({'cut': f'the page text was cut at {BODY_LIMIT:,} of {len(text):,} characters'} if len(text) > BODY_LIMIT else {})
