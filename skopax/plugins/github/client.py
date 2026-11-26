import base64
import math
import posixpath
import re
import time
from urllib.parse import quote, unquote, urlencode, urlsplit

from skopax import remote
from skopax.contract import Denied, Missing, SourceDown
from skopax.errkind import NoAccess
from skopax.messages import msg
from skopax.remote import HttpError, Transport, Unreachable

GITHUB_URL = 'https://github.com'
GITHUB_HOST = 'github.com'
GITHUB_API = 'https://api.github.com'
DOTCOM = ('github.com', 'www.github.com')
CLOUD_SUFFIX = '.ghe.com'
API_VERSION = '2022-11-28'
JSON_TYPE = 'application/vnd.github+json'
MATCH_TYPE = 'application/vnd.github.text-match+json'
DEFAULT_BRANCH = 'main'
PER_PAGE = 100
SEARCH_HITS = 20
MAX_PAGES = 30
COMPARE_PAGES = 10
REPOS_TTL_S = 3600
UNKNOWN_TTL_S = 60
UNKNOWN_MAX = 500
DENIED = (401, 403)
MOVED = (301, 302, 307, 308)
RATE_PAUSE_S = 60
RATE_MAX_S = 3600
NAME = re.compile(r'[A-Za-z0-9._-]{1,100}')
LINK_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="next"')
CODE = 'Contents: read'
PERMISSIONS = {'repositories': 'Metadata: read on the repository', 'code': CODE, 'pull requests': 'Pull requests: read', 'checks': 'Checks: read and Commit statuses: read',
               'deployments': 'Deployments: read', 'releases': CODE, 'owner': 'Metadata: read'}
BLAME_QUERY = ('query($owner: String!, $name: String!, $ref: String!, $path: String!) { repository(owner: $owner, name: $name) { object(expression: $ref) { '
               '... on Commit { blame(path: $path) { ranges { startingLine endingLine commit { oid message committedDate authoredDate author { name } } } } } } } }')


def api_of(base: str) -> str:
    base = (base or GITHUB_URL).rstrip('/')
    host = host_of(base)
    if host in DOTCOM:
        return GITHUB_API
    if host.endswith(CLOUD_SUFFIX):
        return f'https://api.{host}'
    return f'{base}/api/v3'


def graphql_of(base: str) -> str:
    base = (base or GITHUB_URL).rstrip('/')
    host = host_of(base)
    if host in DOTCOM or host.endswith(CLOUD_SUFFIX):
        return f'{api_of(base)}/graphql'
    return f'{base}/api/graphql'


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or '').lower()
    except ValueError:
        return ''


def denied_text(part: str, status: int) -> str:
    return msg('github.denied_invalid' if status == 401 else 'github.denied', part=part, status=status, permission=PERMISSIONS[part])


def rate_wait(e: HttpError, now: float) -> float | None:
    head, body = e.headers, (e.body or '').lower()
    limited = e.code == 429 or (e.code == 403 and (head.get('x-ratelimit-remaining') == '0' or 'retry-after' in head or 'rate limit' in body))
    if not limited:
        return None
    return reset_wait(head, now)


def reset_wait(head: dict, now: float) -> float:
    wait = None
    retry = head.get('retry-after', '').strip()
    if retry.isdigit():
        wait = float(retry)
    elif head.get('x-ratelimit-remaining') == '0' and head.get('x-ratelimit-reset', '').strip().isdigit():
        wait = float(head['x-ratelimit-reset']) - now
    if wait is None or wait <= 0:
        wait = RATE_PAUSE_S
    return min(wait, RATE_MAX_S)


def same_origin(url: str, other: str) -> bool:
    try:
        a, b = urlsplit(url), urlsplit(other)
        return (a.scheme, (a.hostname or '').lower(), a.port) == (b.scheme, (b.hostname or '').lower(), b.port)
    except ValueError:
        return False


def next_page(link: str | None) -> str | None:
    found = LINK_NEXT.search(link or '')
    return found.group(1) if found else None


def short(commit: dict) -> dict:
    inner = commit.get('commit') or {}
    message = inner.get('message') or ''
    author = inner.get('author') or {}
    committer = inner.get('committer') or {}
    sha = commit.get('sha') or ''
    out = {'id': sha, 'short_id': sha[:8], 'title': message.split('\n')[0], 'message': message, 'author_name': author.get('name'),
           'committed_date': committer.get('date') or author.get('date'), 'authored_date': author.get('date'), 'parent_ids': [p.get('sha') for p in commit.get('parents') or [] if isinstance(p, dict)]}
    return out | ({'web_url': commit['html_url']} if commit.get('html_url') else {})


def single(commits: list[dict]) -> list[dict]:
    return [short(c) for c in commits if isinstance(c, dict) and len(c.get('parents') or []) <= 1]


def split_name(owner: str, repo: str) -> tuple[str, str] | None:
    path = str(repo).strip().strip('/')
    parts = path.split('/')
    if len(parts) == 1:
        parts = [owner, parts[0]]
    if len(parts) != 2 or not all(NAME.fullmatch(p) for p in parts) or parts[0].lower() != owner.lower() or parts[1] in ('.', '..'):
        return None
    return parts[0], parts[1]


class GitHub:
    name = 'GitHub'

    def __init__(self, token: str, transport: Transport = remote.send, owner: str = '', base: str = GITHUB_URL, clock=time.monotonic, wall=time.time, docs: str = ''):
        self.token = token
        self.owner = str(owner or '').strip().strip('/')
        self.base = (base or GITHUB_URL).rstrip('/')
        self.api = api_of(self.base)
        self.graphql = graphql_of(self.base)
        self.transport = transport
        self.clock = clock
        self.wall = wall
        self.docs = str(docs or '').strip().strip('/')
        self._repos: dict[str, dict] = {}
        self._loaded = None
        self._unknown: dict[str, float] = {}
        self._paused = 0.0
        self._wait = 0
        self._kind: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.owner)

    @property
    def host(self) -> str:
        return host_of(self.base)

    @property
    def root(self) -> str:
        try:
            return urlsplit(self.base).path.rstrip('/')
        except ValueError:
            return ''

    def _headers(self, accept: str = JSON_TYPE) -> dict[str, str]:
        return {'Authorization': f'Bearer {self.token}', 'Accept': accept, 'X-GitHub-Api-Version': API_VERSION}

    def _held(self) -> None:
        left = self._paused - self.clock()
        if left > 0:
            raise SourceDown(msg('github.rate_limited', seconds=max(1, math.ceil(left))))

    def _limit(self, e: HttpError) -> None:
        wait = rate_wait(e, self.wall())
        if wait is None:
            return
        self._wait = int(round(wait))
        self._paused = self.clock() + wait
        raise SourceDown(msg('github.rate_limited', seconds=self._wait)) from None

    def _send(self, method: str, url: str, part: str, body: object = None, accept: str = JSON_TYPE, moved: bool = False) -> remote.Reply:
        self._held()
        try:
            return remote.call(self.transport, method, url, self._headers(accept), body, timeout=30)
        except HttpError as e:
            self._limit(e)
            where = e.headers.get('location', '')
            if e.code in MOVED and not moved and where and same_origin(where, self.api):
                return self._send(method, where, part, body, accept, True)
            if e.code == 404 or e.code in MOVED:
                raise Missing(urlsplit(url).path) from None
            if e.code in DENIED:
                raise Denied(denied_text(part, e.code), part, PERMISSIONS[part]) from None
            raise SourceDown(f'GitHub HTTP {e.code}') from None
        except Unreachable:
            raise SourceDown(msg('enrich.unreachable', source='GitHub')) from None

    def _get(self, path: str, part: str = 'code', accept: str = JSON_TYPE, **params) -> remote.Reply:
        return self._send('GET', f'{self.api}{path}' + (f'?{urlencode(params)}' if params else ''), part, accept=accept)

    def _pages(self, path: str, part: str = 'code', key: str | None = None, limit: int = MAX_PAGES, **params) -> list:
        out: list = []
        url: str | None = f'{self.api}{path}?{urlencode(params | {"per_page": PER_PAGE})}'
        for _ in range(limit):
            reply = self._send('GET', url, part)
            body = reply.body.get(key) if key and isinstance(reply.body, dict) else reply.body
            out += body if isinstance(body, list) else []
            url = next_page(reply.headers.get('link'))
            if not url or not same_origin(url, self.api):
                break
        return out

    def _parts(self, repo) -> tuple[str, str]:
        found = split_name(self.owner, repo['full'] if isinstance(repo, dict) else repo)
        if found is None:
            raise Missing(str(repo.get('full') if isinstance(repo, dict) else repo)[:200])
        return found

    def _full(self, repo) -> str:
        return '/'.join(self._parts(repo))

    def _at(self, repo) -> str:
        owner, name = self._parts(repo)
        return f'/repos/{quote(owner, safe="")}/{quote(name, safe="")}'

    def _row(self, r: dict) -> dict | None:
        full = str(r.get('full_name') or '')
        login = str((r.get('owner') or {}).get('login') or full.split('/')[0])
        if not full or login.lower() != self.owner.lower() or r.get('archived'):
            return None
        return {'id': r.get('id'), 'web': str(r.get('html_url') or f'{self.base}/{full}').rstrip('/'), 'path': full, 'full': full, 'branch': r.get('default_branch') or DEFAULT_BRANCH,
                'name': full.split('/')[-1]}

    def repos(self) -> list[dict]:
        if self._loaded is None or self.clock() - self._loaded > REPOS_TTL_S:
            listed = self._pages('/user/repos', 'repositories', affiliation='owner,collaborator,organization_member', sort='full_name')
            found = {}
            for r in listed:
                row = self._row(r) if isinstance(r, dict) else None
                if row:
                    found[row['name'].lower()] = row
            self._repos, self._loaded = found, self.clock()
            self._unknown.clear()
        return list(self._repos.values())

    def repo(self, name_or_path: str) -> dict | None:
        parts = split_name(self.owner, name_or_path)
        if parts is None or not self.enabled:
            return None
        self.repos()
        known = self._repos.get(parts[1].lower())
        if known:
            return known
        key = parts[1].lower()
        missed = self._unknown.get(key)
        if missed is not None and self.clock() - missed < UNKNOWN_TTL_S:
            return None
        try:
            body = self._get(f'/repos/{quote(parts[0], safe="")}/{quote(parts[1], safe="")}', 'repositories').body
        except Missing:
            if len(self._unknown) >= UNKNOWN_MAX:
                self._unknown.clear()
            self._unknown[key] = self.clock()
            return None
        return self._row(body) if isinstance(body, dict) else None

    def similar(self, name: str, limit: int = 5) -> list[str]:
        try:
            listed = self.repos()
        except NoAccess:
            raise
        except Exception:
            return []
        words = [w for w in re.split(r'[-_/]', str(name).lower()) if len(w) > 2]
        scored = []
        for r in listed:
            last = r['name'].lower()
            hit = sum(1 for w in words if w in last) + (10 if str(name).lower().split('/')[-1] in last else 0)
            if hit:
                scored.append((-hit, len(r['full']), r['full']))
        return [full for _, _, full in sorted(scored)[:limit]]

    def owner_kind(self) -> str:
        if self._kind is None:
            body = self._get(f'/users/{quote(self.owner, safe="")}', 'owner').body or {}
            self._kind = 'organization' if str(body.get('type') or '').lower() == 'organization' else 'user'
        return self._kind

    def user(self) -> dict:
        return self._get('/user', 'owner').body or {}

    def version(self) -> str | None:
        if self.host == GITHUB_HOST:
            return None
        body = self._get('/meta', 'owner').body
        found = body.get('installed_version') if isinstance(body, dict) else None
        return found if isinstance(found, str) and found else None

    def commit(self, repo, ref: str) -> dict:
        return short(self._get(f'{self._at(repo)}/commits/{quote(str(ref), safe="")}').body or {})

    def commits(self, repo, old: str, new: str) -> list[dict]:
        listed = self._pages(f'{self._at(repo)}/compare/{quote(str(old), safe="")}...{quote(str(new), safe="")}', key='commits', limit=COMPARE_PAGES)
        return list(reversed(single(listed)))

    def commits_since(self, repo, ref: str, since: str) -> list[dict]:
        return single(self._pages(f'{self._at(repo)}/commits', sha=ref, since=since))

    def raw_file(self, repo, path: str, ref: str) -> dict:
        body = self._get(f'{self._at(repo)}/contents/{quote(str(path).strip("/"), safe="/")}', ref=ref).body
        if not isinstance(body, dict) or body.get('type') != 'file':
            raise Missing(path)
        return body

    def listing(self, repo, path: str, ref: str) -> list[dict]:
        folder = str(path).strip('/')
        body = self._get(f'{self._at(repo)}/contents' + (f'/{quote(folder, safe="/")}' if folder else ''), ref=ref).body
        if not isinstance(body, list):
            raise Missing(folder)
        return [r for r in body if isinstance(r, dict)]

    def tags(self, repo, limit: int = 100) -> list[str]:
        rows = self._get(f'{self._at(repo)}/tags', per_page=limit).body
        return [str(r['name']) for r in rows if isinstance(r, dict) and r.get('name')] if isinstance(rows, list) else []

    def file(self, repo, path: str, ref: str) -> str:
        return self.decoded(self.raw_file(repo, path, ref), path)

    def decoded(self, body: dict, path: str) -> str:
        if body.get('encoding') != 'base64':
            raise SourceDown(msg('github.file_large', path=path))
        return base64.b64decode(body.get('content') or '').decode('utf-8', 'replace')

    def blame(self, repo, path: str, ref: str, first: int, last: int) -> list[dict]:
        owner, name = self._parts(repo)
        reply = self._send('POST', self.graphql, 'code', {'query': BLAME_QUERY, 'variables': {'owner': owner, 'name': name, 'ref': ref, 'path': path}})
        body = reply.body if isinstance(reply.body, dict) else {}
        kinds = {str(e.get('type') or '') for e in body.get('errors') or [] if isinstance(e, dict)}
        if 'RATE_LIMITED' in kinds:
            wait = reset_wait(reply.headers or {}, self.wall())
            self._wait, self._paused = int(round(wait)), self.clock() + wait
            raise SourceDown(msg('github.rate_limited', seconds=self._wait))
        if 'FORBIDDEN' in kinds:
            raise Denied(denied_text('code', 403), 'code', PERMISSIONS['code'])
        found = (((body.get('data') or {}).get('repository') or {}).get('object') or {}).get('blame')
        if not isinstance(found, dict):
            raise Missing(path)
        out = []
        for r in found.get('ranges') or []:
            lo, hi = max(int(r.get('startingLine') or 0), first), min(int(r.get('endingLine') or 0), last)
            if lo > hi:
                continue
            c = r.get('commit') or {}
            out.append({'commit': {'id': c.get('oid'), 'author_name': (c.get('author') or {}).get('name'), 'committed_date': c.get('committedDate'), 'authored_date': c.get('authoredDate'),
                                   'message': c.get('message') or ''}, 'lines': list(range(lo, hi + 1))})
        return out

    def search(self, query: str, repo=None, path: str = '') -> list[dict]:
        scope = f'repo:{self._full(repo)}' if repo is not None else f'{"org" if self.owner_kind() == "organization" else "user"}:{self.owner}'
        q = f'{query} {scope}' + (f' path:{path}' if path else '')
        body = self._get('/search/code', 'code', accept=MATCH_TYPE, q=q, per_page=SEARCH_HITS).body or {}
        out = []
        for h in body.get('items') or []:
            full = str((h.get('repository') or {}).get('full_name') or '')
            if full.split('/')[0].lower() != self.owner.lower() or (repo is not None and full.lower() != self._full(repo).lower()):
                continue
            text = ' \u2026 '.join(str(m.get('fragment') or '') for m in h.get('text_matches') or [] if isinstance(m, dict))
            out.append({'repo': full, 'path': h.get('path'), 'url': h.get('html_url'), 'text': text[:400]})
        return out

    def compare(self, repo, old: str, new: str) -> dict:
        reply = self._get(f'{self._at(repo)}/compare/{quote(str(old), safe="")}...{quote(str(new), safe="")}', per_page=PER_PAGE)
        body = dict(reply.body) if isinstance(reply.body, dict) else {}
        commits = list(body.get('commits') or [])
        url = next_page(reply.headers.get('link'))
        for _ in range(COMPARE_PAGES - 1):
            if not url or not same_origin(url, self.api):
                break
            more = self._send('GET', url, 'code')
            commits += (more.body.get('commits') or []) if isinstance(more.body, dict) else []
            url = next_page(more.headers.get('link'))
        return body | {'commits': commits}

    def commit_pulls(self, repo, sha: str) -> list[dict]:
        return self._get(f'{self._at(repo)}/commits/{quote(str(sha), safe="")}/pulls', 'pull requests').body or []

    def pulls(self, repo, state: str, limit: int, head: str | None = None) -> list[dict]:
        extra = {'head': f'{self.owner}:{head}'} if head else {}
        return self._get(f'{self._at(repo)}/pulls', 'pull requests', state=state, sort='created', direction='desc', per_page=limit, **extra).body or []

    def search_pulls(self, repo, words: str, qualifiers: str, limit: int) -> list[dict]:
        full = self._full(repo)
        q = f'repo:{full} is:pr in:title {qualifiers} {words}'.replace('  ', ' ')
        items = (self._get('/search/issues', 'pull requests', q=q, sort='created', order='desc', per_page=limit).body or {}).get('items') or []
        own = f'/repos/{full}'.lower()
        return [i for i in items if isinstance(i, dict) and str(i.get('repository_url') or '').lower().endswith(own)]

    def repo_count(self) -> tuple[int, bool]:
        url = f'{self.api}/user/repos?{urlencode({"affiliation": "owner,collaborator,organization_member", "per_page": PER_PAGE})}'
        reply = self._send('GET', url, 'repositories')
        rows = [r for r in reply.body if isinstance(r, dict) and self._row(r)] if isinstance(reply.body, list) else []
        return len(rows), bool(next_page(reply.headers.get('link')))

    def check_runs(self, repo, ref: str) -> dict:
        return self._get(f'{self._at(repo)}/commits/{quote(str(ref), safe="")}/check-runs', 'checks', per_page=PER_PAGE).body or {}

    def status(self, repo, ref: str) -> dict:
        return self._get(f'{self._at(repo)}/commits/{quote(str(ref), safe="")}/status', 'checks', per_page=PER_PAGE).body or {}

    def deployments(self, repo, limit: int) -> list[dict]:
        return self._get(f'{self._at(repo)}/deployments', 'deployments', per_page=limit).body or []

    def deployment_state(self, repo, deployment_id: int) -> dict | None:
        found = self._get(f'{self._at(repo)}/deployments/{int(deployment_id)}/statuses', 'deployments', per_page=1).body or []
        return found[0] if found and isinstance(found[0], dict) else None

    def releases(self, repo, limit: int) -> list[dict]:
        return self._get(f'{self._at(repo)}/releases', 'releases', per_page=limit).body or []

    @staticmethod
    def commit_url(repo: dict, sha: str) -> str:
        return f"{repo['web']}/commit/{sha}"

    @staticmethod
    def compare_url(repo: dict, old: str, new: str) -> str:
        return f"{repo['web']}/compare/{old}...{new}"

    @staticmethod
    def file_url(repo: dict, sha: str, path: str, first: int, last: int) -> str:
        return f"{repo['web']}/blob/{sha}/{path}#L{first}" + (f'-L{last}' if last != first else '')

    def link_problem(self, url: str) -> str | None:
        mine = GITHUB_HOST if self.host in DOTCOM else self.host
        if not self.owner or not mine:
            return None
        parts = urlsplit(url if '://' in url else '//' + url)
        host = (parts.hostname or '').lower()
        if host != mine and not (mine == GITHUB_HOST and host.endswith('.' + mine)):
            return None
        prefix = f'{self.root}/{self.owner}/'
        path = posixpath.normpath(unquote(parts.path) or '/') + '/'
        if not path.lower().startswith(prefix.lower()):
            return msg('github.link_outside', host=mine, prefix=prefix, url=url)
        return None
