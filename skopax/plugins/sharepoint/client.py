import json
import re
import time
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlencode, urlsplit

from skopax import remote
from skopax.contract import Denied, Missing, SourceDown
from skopax.messages import msg
from skopax.remote import HttpError, Reply, Transport, Unreachable

LOGIN = 'https://login.microsoftonline.com'
GRAPH = 'https://graph.microsoft.com/v1.0'
GRAPH_HOST = 'graph.microsoft.com'
LOGIN_HOST = 'login.microsoftonline.com'
SCOPE = 'https://graph.microsoft.com/.default'
SITE_SUFFIX = '.sharepoint.com'
TOKEN_EARLY_S = 300
PLACE_TTL_S = 3600
WALK_TTL_S = 300
WALK_FOLDERS = 60
WALK_CALLS = 40
WALK_ITEMS = 3000
WALK_DEADLINE_S = 60
ANCESTRY_HOPS = 20
PAGE_SIZE = 200
TEXT_BYTES = 1024 * 1024
DOCX_BYTES = 8 * 1024 * 1024
DOCX_XML_BYTES = 8 * 1024 * 1024
DOCX_ENTRIES = 2000
TEXT_LIMIT = 20000
TIMEOUT_S = 30
DENIED = (401, 403)
BUSY = (429, 503)
MOVED = (301, 302, 303, 307, 308)
GUID = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
ITEM_ID = re.compile(r'(?=[^A-Za-z0-9]*[A-Za-z0-9])[A-Za-z0-9!_.\-]{1,200}')
RAW_SLASH = re.compile(r'%(2f|5c)', re.IGNORECASE)
TEXT_TYPES = ('.md', '.markdown', '.mdx', '.txt', '.rst', '.adoc')
WORD_TYPE = '.docx'
READABLE = TEXT_TYPES + (WORD_TYPE,)


def guid_problem(label: str):
    def check(value: str) -> str | None:
        return None if GUID.fullmatch(str(value or '').strip()) else msg('sharepoint.invalid.guid', field=label)

    return check


def site_problem(value: str) -> str | None:
    try:
        parts = urlsplit(str(value or '').strip())
        host = (parts.hostname or '').lower()
        parts.port
    except ValueError:
        return msg('sharepoint.invalid.site')
    path = parts.path.strip('/')
    if parts.scheme != 'https' or not host.endswith(SITE_SUFFIX) or host == SITE_SUFFIX[1:] or parts.port or '@' in parts.netloc or parts.query or parts.fragment or not path \
            or '..' in path.split('/'):
        return msg('sharepoint.invalid.site')
    return None


def clean_path(value: str) -> str | None:
    text = str(value or '').strip().strip('/')
    parts = text.split('/')
    if not text or '\\' in text or ':' in text or any(p in ('', '.', '..') for p in parts) or any(ord(c) < 32 for c in text):
        return None
    return text


def folder_problem(value: str) -> str | None:
    return None if clean_path(value) is not None else msg('sharepoint.invalid.folder')


def under(path: str, folder: str) -> bool:
    inner, outer = path.casefold().strip('/'), folder.casefold().strip('/')
    return inner == outer or inner.startswith(outer + '/')


def parent_path(item: dict) -> str | None:
    found = (item.get('parentReference') or {}).get('path')
    if not isinstance(found, str) or ':' not in found:
        return None
    raw = found.split(':', 1)[1]
    if RAW_SLASH.search(raw):
        return None
    text = unquote(raw).strip('/')
    if '\\' in text or (text and any(p in ('', '.', '..') for p in text.split('/'))):
        return None
    return text


def place_of(item: dict) -> str | None:
    at = parent_path(item)
    name = item.get('name')
    if at is None or not isinstance(name, str) or not name:
        return None
    return f'{at}/{name}'.strip('/')


class TooLarge(Exception):
    pass


@dataclass(frozen=True)
class Place:
    site: str
    drive: str
    folder_id: str
    folder: str
    library: str


def seg(value: str) -> str:
    return quote(value, safe=',!')


def graph_url(path: str, **params) -> str:
    return f'{GRAPH}{path}' + (f'?{urlencode(params)}' if params else '')


def same_graph(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == 'https' and (parts.hostname or '').lower() == GRAPH_HOST and parts.port in (None, 443)


def error_code(e: HttpError) -> str:
    try:
        body = json.loads(e.body or '{}')
    except ValueError:
        return 'no error code'
    code = body.get('error') if isinstance(body, dict) else None
    return code if isinstance(code, str) and re.fullmatch(r'[a-z_]{1,60}', code) else 'no error code'


class Graph:
    def __init__(self, tenant: str, client_id: str, secret: str, site: str, library: str, folder: str, transport: Transport = remote.send, clock=time.monotonic):
        self.tenant = str(tenant or '').strip()
        self.client_id = str(client_id or '').strip()
        self.secret = str(secret or '')
        self.site = str(site or '').strip().rstrip('/')
        self.library = str(library or '').strip()
        self.folder = clean_path(folder) or ''
        self.transport = transport
        self.clock = clock
        self._token = ''
        self._until = 0.0
        self._place: tuple[float, Place] | None = None
        self._walked: tuple[float, list[dict], bool] | None = None
        self._folders: set[str] = set()

    @property
    def enabled(self) -> bool:
        return bool(self.tenant and self.client_id and self.secret and self.site and self.folder)

    @property
    def host(self) -> str:
        return (urlsplit(self.site).hostname or '').lower()

    def token(self) -> str:
        if self._token and self.clock() < self._until:
            return self._token
        url = f'{LOGIN}/{quote(self.tenant, safe="")}/oauth2/v2.0/token'
        form = urlencode({'client_id': self.client_id, 'client_secret': self.secret, 'scope': SCOPE, 'grant_type': 'client_credentials'}).encode()
        try:
            reply = self.transport('POST', url, {'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}, form, TIMEOUT_S)
        except HttpError as e:
            if e.code in (400, 401):
                raise Denied(msg('sharepoint.token_refused', status=e.code, error=error_code(e)), 'token', 'Client secret') from None
            raise SourceDown(f'Microsoft Entra HTTP {e.code}') from None
        except Unreachable:
            raise SourceDown(msg('enrich.unreachable', source='Microsoft Entra')) from None
        body = reply.body if isinstance(reply.body, dict) else {}
        token, life = body.get('access_token'), body.get('expires_in')
        if not isinstance(token, str) or not token:
            raise SourceDown(msg('sharepoint.no_token'))
        seconds = float(life) if isinstance(life, (int, float)) and not isinstance(life, bool) else 0.0
        self._token, self._until = token, self.clock() + max(seconds - TOKEN_EARLY_S, 0.0)
        return token

    def _failed(self, e: HttpError, part: str):
        if e.code == 404 or e.code in MOVED:
            return Missing('')
        if e.code in DENIED:
            if e.code == 401:
                self._token, self._until = '', 0.0
                return Denied(msg('sharepoint.denied_token', part=part), part, 'Sites.Selected')
            return Denied(msg('sharepoint.denied', part=part, status=e.code), part, 'Sites.Selected')
        if e.code in BUSY:
            wait = e.headers.get('retry-after', '').strip()
            return SourceDown(msg('sharepoint.throttled', seconds=wait if wait.isdigit() else '60'))
        return SourceDown(f'Microsoft Graph HTTP {e.code}')

    def get(self, url: str, part: str) -> dict:
        try:
            reply = remote.call(self.transport, 'GET', url, {'Authorization': f'Bearer {self.token()}'}, timeout=TIMEOUT_S)
        except HttpError as e:
            raise self._failed(e, part) from None
        except Unreachable:
            raise SourceDown(msg('enrich.unreachable', source='Microsoft Graph')) from None
        return reply.body if isinstance(reply.body, dict) else {}

    def place(self) -> Place:
        if self._place is not None and self.clock() - self._place[0] < PLACE_TTL_S:
            return self._place[1]
        parts = urlsplit(self.site)
        try:
            site = self.get(graph_url(f'/sites/{quote(self.host, safe="")}:{quote(parts.path.rstrip("/"), safe="/")}'), 'the site')
        except Missing:
            raise Missing(msg('sharepoint.no_site', site=self.site)) from None
        site_id = str(site.get('id') or '')
        if not site_id:
            raise Missing(msg('sharepoint.no_site', site=self.site))
        at = quote(self.folder, safe='/')
        if self.library:
            drives = self.get(graph_url(f'/sites/{seg(site_id)}/drives'), 'the libraries').get('value') or []
            drive = next((d for d in drives if isinstance(d, dict) and str(d.get('name') or '').casefold() == self.library.casefold()), None)
            if drive is None or not drive.get('id'):
                raise Missing(msg('sharepoint.no_library', library=self.library))
            where = graph_url(f'/drives/{seg(str(drive["id"]))}/root:/{at}')
        else:
            where = graph_url(f'/sites/{seg(site_id)}/drive/root:/{at}')
        try:
            item = self.get(where, 'the folder')
        except Missing:
            raise Missing(msg('sharepoint.no_folder', folder=self.folder)) from None
        if not isinstance(item.get('folder'), dict):
            raise Missing(msg('sharepoint.not_folder', folder=self.folder))
        drive_id = str((item.get('parentReference') or {}).get('driveId') or '')
        path = place_of(item)
        if not drive_id or not item.get('id') or path is None:
            raise Missing(msg('sharepoint.no_folder', folder=self.folder))
        found = Place(site_id, drive_id, str(item['id']), path, self.library)
        if self._place is None or self._place[1] != found:
            self._folders = {found.folder_id}
        self._place = (self.clock(), found)
        return found

    def inside(self, item: dict, place: Place) -> str | None:
        if not isinstance(item, dict) or item.get('remoteItem') is not None:
            return None
        if (item.get('parentReference') or {}).get('driveId') != place.drive:
            return None
        path = place_of(item)
        if path is None or not under(path, place.folder) or path.casefold() == place.folder.casefold():
            return None
        return path[len(place.folder):].strip('/')

    def children(self, place: Place, item_id: str, pages: int) -> tuple[list[dict], int, bool]:
        url: str | None = graph_url(f'/drives/{seg(place.drive)}/items/{seg(item_id)}/children', **{'$top': PAGE_SIZE})
        out: list[dict] = []
        used = 0
        while url and used < pages:
            body = self.get(url, 'the folder')
            used += 1
            out += [v for v in body.get('value') or [] if isinstance(v, dict)]
            following = body.get('@odata.nextLink')
            if isinstance(following, str) and following and not same_graph(following):
                return out, used, True
            url = following if isinstance(following, str) and following else None
        return out, used, bool(url)

    def first_page(self) -> tuple[int, bool]:
        place = self.place()
        found, _, more = self.children(place, place.folder_id, 1)
        return len(found), more

    def walk(self) -> tuple[list[dict], bool]:
        if self._walked is not None and self.clock() - self._walked[0] < WALK_TTL_S:
            return self._walked[1], self._walked[2]
        place = self.place()
        ends = self.clock() + WALK_DEADLINE_S
        todo, files, calls, folders, partial = [place.folder_id], [], 0, 0, False
        while todo:
            if folders >= WALK_FOLDERS or calls >= WALK_CALLS or len(files) >= WALK_ITEMS or self.clock() >= ends:
                partial = True
                break
            listed, used, more = self.children(place, todo.pop(0), WALK_CALLS - calls)
            calls, folders, partial = calls + used, folders + 1, partial or more
            for item in listed:
                path = self.inside(item, place)
                if path is None:
                    continue
                if isinstance(item.get('folder'), dict) and item.get('id'):
                    self._folders.add(str(item['id']))
                    todo.append(str(item['id']))
                elif isinstance(item.get('file'), dict):
                    if len(files) >= WALK_ITEMS:
                        partial = True
                        break
                    files.append({'path': path, 'id': item.get('id'), 'url': item.get('webUrl'), 'name': item.get('name')})
        self._walked = (self.clock(), files, partial)
        return files, partial

    def item(self, path: str | None = None, item_id: str | None = None) -> dict:
        place = self.place()
        if item_id is not None:
            url = graph_url(f'/drives/{seg(place.drive)}/items/{seg(item_id)}')
        else:
            url = graph_url(f'/drives/{seg(place.drive)}/items/{seg(place.folder_id)}:/{quote(path, safe="/")}:')
        return self.get(url, 'the file')

    def descends(self, item: dict, place: Place) -> bool:
        parent, chain = item.get('parentReference') or {}, []
        for _ in range(ANCESTRY_HOPS + 1):
            pid = parent.get('id')
            if not isinstance(pid, str) or not pid or parent.get('driveId') != place.drive:
                return False
            if pid in self._folders:
                self._folders.update(chain)
                return True
            if len(chain) >= ANCESTRY_HOPS:
                return False
            try:
                above = self.get(graph_url(f'/drives/{seg(place.drive)}/items/{seg(pid)}'), 'the folder')
            except Missing:
                return False
            if above.get('id') != pid or not isinstance(above.get('folder'), dict) or above.get('root') is not None or above.get('remoteItem') is not None:
                return False
            chain.append(pid)
            parent = above.get('parentReference') or {}
        return False

    def content(self, item_id: str, limit: int) -> bytes:
        place = self.place()
        url = graph_url(f'/drives/{seg(place.drive)}/items/{seg(item_id)}/content')
        try:
            reply = remote.call_page(self.transport, url, {'Authorization': f'Bearer {self.token()}'}, timeout=TIMEOUT_S, limit=limit)
            if reply.status in MOVED:
                where = reply.headers.get('location', '')
                if not self.download_host(where):
                    raise SourceDown(msg('sharepoint.no_download'))
                reply = remote.call_page(self.transport, where, {}, timeout=TIMEOUT_S, limit=limit)
        except Unreachable:
            raise SourceDown(msg('enrich.unreachable', source='Microsoft Graph')) from None
        body = self._body(reply, 'the file')
        if len(body) > limit:
            raise TooLarge()
        return body

    def download_host(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            return False
        host = (parts.hostname or '').lower()
        return parts.scheme == 'https' and port in (None, 443) and '@' not in parts.netloc and (host == self.host or host.endswith(SITE_SUFFIX))

    def _body(self, reply: Reply, part: str) -> bytes:
        status = reply.status if reply.status is not None else 200
        if status >= 400:
            raise self._failed(HttpError(status, part, '', reply.headers), part)
        if status != 200 or not isinstance(reply.body, bytes):
            raise SourceDown(msg('sharepoint.no_download'))
        return reply.body
