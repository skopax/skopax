import base64
import collections
import posixpath
import re
import time
from urllib.parse import quote, unquote, urlencode, urlsplit

from skopax import remote
from skopax.contract import Denied, Missing, SourceDown
from skopax.errkind import NoAccess
from skopax.messages import msg
from skopax.remote import HttpError, Transport, Unreachable

GITLAB_URL = 'https://gitlab.com'
GITLAB_HOST = 'gitlab.com'
GITLAB_API = f'{GITLAB_URL}/api/v4'
DEFAULT_BRANCH = 'master'
PER_PAGE = 100
SEARCH_HITS = 20
MAX_PAGES = 30
PROJECTS_TTL_S = 3600
PROJECT_WORDS: dict[str, str] = {}
DENIED = (401, 403)
READ = 'the read_api scope or a role that can read it, such as Reporter'
PERMISSIONS = {'projects': 'the read_api scope or access to the group', 'code': READ, 'merge requests': READ, 'pipelines': READ}


def denied_text(part: str, status: int) -> str:
    return msg('gitlab.denied_invalid' if status == 401 else 'gitlab.denied', part=part, status=status, permission=PERMISSIONS[part])


def hidden_text(group: str) -> str:
    return msg('gitlab.group_hidden', group=group, permission=PERMISSIONS['projects'])


def branch_of(project: dict) -> str:
    return str(project.get('branch') or DEFAULT_BRANCH)


def worded_name(name: str) -> str:
    return '-'.join(PROJECT_WORDS.get(w, w) for w in re.split(r'[-_]', name.strip().lower()))


def target(gitlab, repo: str) -> tuple[str, str]:
    path = str(repo).strip().strip('/')
    group = getattr(gitlab, 'group', '')
    prefix = f'{group}/'
    if group and path.lower().startswith(prefix.lower()):
        path = path[len(prefix):]
    return path, path.split('/')[-1]


def find(gitlab, repo: str) -> dict | None:
    path, name = target(gitlab, repo)
    if '/' in path:
        project = gitlab.project_at(path)
        if project:
            return project
    words = worded_name(name)
    found = gitlab.project(name) or (gitlab.project(words) if words != name else None)
    if found or '/' in path:
        return found
    return gitlab.project_at(path) or worded_project(gitlab, name)


def known_paths(gitlab) -> list[str] | None:
    try:
        gitlab.projects()
        return list(getattr(gitlab, '_paths', {}) or {})
    except NoAccess:
        raise
    except Exception:
        return None


def worded_project(gitlab, name: str) -> dict | None:
    word = worded_name(name)
    paths = known_paths(gitlab)
    if paths is None:
        return None
    hits = [p for p in paths if word and word in re.split(r'[-_]', p.split('/')[-1])]
    picked = hits if len(hits) == 1 else [p for p in hits if p.count('/') == 1]
    return gitlab.project_at(picked[0]) if len(picked) == 1 else None


def similar_projects(gitlab, name: str, limit: int = 5) -> list[str]:
    paths = known_paths(gitlab)
    if paths is None:
        return []
    parts = [PROJECT_WORDS.get(w, w) for w in re.split(r'[-_/]', name.lower())]
    words = [w for w in parts if len(w) > 2]
    names = {name.lower(), '-'.join(parts)}
    scored = []
    for p in paths:
        last = p.split('/')[-1]
        hit = sum(1 for w in words if w in last)
        whole = sum(10 for n in names if n in p)
        if whole or hit:
            scored.append((-(whole + hit), len(p), p))
    return [p for _, _, p in sorted(scored)[:limit]]


class GitLab:
    name = 'GitLab'

    def __init__(self, token: str, transport: Transport = remote.send, clock=time.monotonic, group: str = '', base: str = GITLAB_URL):
        self.token = token
        self.group = group
        self.base = (base or GITLAB_URL).rstrip('/')
        self.api = f'{self.base}/api/v4'
        self.transport = transport
        self.clock = clock
        self._projects: dict[str, list[dict]] = {}
        self._deeper: dict[str, list[dict]] = {}
        self._paths: dict[str, dict] = {}
        self._ids: dict = {}
        self._loaded = None

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.group)

    @property
    def host(self) -> str:
        try:
            return (urlsplit(self.base).hostname or '').lower()
        except ValueError:
            return ''

    @property
    def root(self) -> str:
        try:
            return urlsplit(self.base).path.rstrip('/')
        except ValueError:
            return ''

    def _get(self, path: str, part: str = 'code', **params) -> remote.Reply:
        url = f'{self.api}{path}' + (f'?{urlencode(params)}' if params else '')
        try:
            return remote.call(self.transport, 'GET', url, {'PRIVATE-TOKEN': self.token}, timeout=30)
        except HttpError as e:
            if e.code == 404:
                raise Missing(path) from None
            if e.code in DENIED:
                raise Denied(denied_text(part, e.code), part, PERMISSIONS[part]) from None
            raise SourceDown(f'GitLab HTTP {e.code}') from None
        except Unreachable:
            raise SourceDown(msg('enrich.unreachable', source='GitLab')) from None

    def _pages(self, path: str, part: str = 'code', **params) -> list[dict]:
        out, page = [], 1
        for _ in range(MAX_PAGES):
            reply = self._get(path, part, **params, per_page=PER_PAGE, page=page)
            out += reply.body or []
            nxt = reply.headers.get('x-next-page')
            if not nxt:
                break
            page = int(nxt)
        return out

    def _group(self) -> str:
        return quote(self.group.strip('/'), safe='')

    @staticmethod
    def _pid(repo) -> object:
        if isinstance(repo, dict):
            return repo['id']
        if isinstance(repo, str):
            return quote(repo.strip('/'), safe='')
        return repo

    def projects(self) -> dict[str, list[dict]]:
        if self._loaded is None or self.clock() - self._loaded > PROJECTS_TTL_S:
            by, deeper, paths, ids = collections.defaultdict(list), collections.defaultdict(list), {}, {}
            try:
                listed = self._pages(f'/groups/{self._group()}/projects', 'projects', include_subgroups='true', archived='false', simple='true', order_by='id', sort='asc')
            except Missing:
                raise Denied(hidden_text(self.group), 'projects', PERMISSIONS['projects']) from None
            prefix = self.group.strip('/').lower() + '/'
            for p in listed:
                full = str(p.get('path_with_namespace') or '')
                if not full.lower().startswith(prefix) or p.get('archived'):
                    continue
                rel = full[len(prefix):]
                parts = rel.split('/')
                if not all(parts):
                    continue
                found = {'id': p['id'], 'web': (p.get('web_url') or '').rstrip('/'), 'path': rel, 'full': full, 'branch': p.get('default_branch') or DEFAULT_BRANCH}
                paths[rel.lower()] = found
                ids[p['id']] = rel
                if len(parts) == 2:
                    by[parts[1]].append(found)
                elif len(parts) > 2:
                    deeper[parts[-1]].append(found)
            self._projects, self._deeper, self._paths, self._ids, self._loaded = dict(by), dict(deeper), paths, ids, self.clock()
        return self._projects

    def project(self, name: str) -> dict | None:
        hits = self.projects().get(name) or []
        if not hits:
            hits = self._deeper.get(name) or []
        return hits[0] if len(hits) == 1 else None

    def path_of(self, pid) -> str | None:
        self.projects()
        return self._ids.get(pid)

    def project_at(self, path: str) -> dict | None:
        self.projects()
        return self._paths.get(path.strip('/').lower())

    def repo(self, name_or_path: str) -> dict | None:
        return find(self, name_or_path)

    def similar(self, name: str, limit: int = 5) -> list[str]:
        return similar_projects(self, name, limit)

    def commit(self, repo, ref: str) -> dict:
        if isinstance(repo, dict) and ref == branch_of(repo):
            return self._get(f'/projects/{self._pid(repo)}/repository/branches/{quote(ref, safe="")}').body['commit']
        return self._get(f'/projects/{self._pid(repo)}/repository/commits/{quote(ref, safe="")}').body

    def commits(self, repo, old: str, new: str) -> list[dict]:
        return [c for c in self._pages(f'/projects/{self._pid(repo)}/repository/commits', ref_name=f'{old}..{new}') if len(c.get('parent_ids') or []) <= 1]

    def commits_since(self, repo, ref: str, since: str) -> list[dict]:
        return [c for c in self._pages(f'/projects/{self._pid(repo)}/repository/commits', ref_name=ref, since=since) if len(c.get('parent_ids') or []) <= 1]

    def pipelines_since(self, repo, ref: str, since: str) -> list[dict]:
        return self._pages(f'/projects/{self._pid(repo)}/pipelines', 'pipelines', ref=ref, updated_after=since)

    def branch_sha(self, repo, branch: str = DEFAULT_BRANCH) -> str:
        return self._get(f'/projects/{self._pid(repo)}/repository/branches/{quote(branch, safe="")}').body['commit']['id']

    def file(self, repo, path: str, ref: str) -> str:
        body = self._get(f'/projects/{self._pid(repo)}/repository/files/{quote(path, safe="")}', ref=ref).body or {}
        return base64.b64decode(body.get('content') or '').decode('utf-8', 'replace')

    def tree(self, repo, path: str, ref: str, limit: int) -> list[dict]:
        url = f'/projects/{self._pid(repo)}/repository/tree'
        params = {'ref': ref, 'per_page': limit} | ({'path': path} if path else {})
        try:
            return remote.call(self.transport, 'GET', f'{self.api}{url}?{urlencode(params)}', {'PRIVATE-TOKEN': self.token}, timeout=30).body or []
        except HttpError as e:
            if e.code == 404:
                raise Missing(url) from None
            if e.code in DENIED:
                raise Denied(denied_text('code', e.code), 'code', PERMISSIONS['code']) from None
            raise SourceDown(f'GitLab HTTP {e.code}') from None
        except Unreachable:
            raise SourceDown(msg('enrich.unreachable', source='GitLab')) from None

    def blame(self, repo, path: str, ref: str, first: int, last: int) -> list[dict]:
        params = {'ref': ref, 'range[start]': first, 'range[end]': last}
        return self._get(f'/projects/{self._pid(repo)}/repository/files/{quote(path, safe="")}/blame', **params).body or []

    def search(self, repo, query: str, ref: str = DEFAULT_BRANCH) -> list[dict]:
        return self._get(f'/projects/{self._pid(repo)}/search', scope='blobs', search=query, ref=ref, per_page=SEARCH_HITS).body or []

    def group_search(self, query: str) -> list[dict]:
        return self._get(f'/groups/{self._group()}/search', scope='blobs', search=query, per_page=SEARCH_HITS).body or []

    def compare(self, repo, old: str, new: str) -> dict:
        return self._get(f'/projects/{self._pid(repo)}/repository/compare', **{'from': old, 'to': new}).body or {}

    def commit_mrs(self, repo, sha: str) -> list[dict]:
        return self._get(f'/projects/{self._pid(repo)}/repository/commits/{quote(sha, safe="")}/merge_requests', 'merge requests').body or []

    def merge_requests(self, repo, state: str | None, limit: int, **params) -> list[dict]:
        state_param = {'state': state} if state else {}
        return self._get(f'/projects/{self._pid(repo)}/merge_requests', 'merge requests', **state_param, **params, order_by='created_at', sort='desc', per_page=limit).body or []

    def pipelines(self, repo, ref: str, limit: int) -> list[dict]:
        return self._get(f'/projects/{self._pid(repo)}/pipelines', 'pipelines', ref=ref, order_by='id', sort='desc', per_page=limit).body or []

    def pipeline(self, repo, pipeline_id: int) -> dict:
        return self._get(f'/projects/{self._pid(repo)}/pipelines/{int(pipeline_id)}', 'pipelines').body or {}

    def pipeline_jobs(self, repo, pipeline_id: int) -> list[dict]:
        return self._get(f'/projects/{self._pid(repo)}/pipelines/{int(pipeline_id)}/jobs', 'pipelines', per_page=PER_PAGE).body or []

    def pipeline_bridges(self, repo, pipeline_id: int) -> list[dict]:
        return self._get(f'/projects/{self._pid(repo)}/pipelines/{int(pipeline_id)}/bridges', 'pipelines', per_page=PER_PAGE).body or []

    @staticmethod
    def commit_url(repo: dict, sha: str) -> str:
        return f"{repo['web']}/-/commit/{sha}"

    @staticmethod
    def compare_url(repo: dict, old: str, new: str) -> str:
        return f"{repo['web']}/-/compare/{old}...{new}"

    @staticmethod
    def file_url(repo: dict, sha: str, path: str, first: int, last: int) -> str:
        return f"{repo['web']}/-/blob/{sha}/{path}#L{first}" + (f'-{last}' if last != first else '')

    def link_problem(self, url: str) -> str | None:
        mine = self.host
        if not self.group or not mine:
            return None
        parts = urlsplit(url if '://' in url else '//' + url)
        host = parts.hostname or ''
        if host != mine and not (mine == GITLAB_HOST and host.endswith('.' + mine)):
            return None
        prefix = f'{self.root}/{self.group}/'
        path = posixpath.normpath(unquote(parts.path)) + '/'
        if not path.lower().startswith(prefix.lower()):
            return f'gitlab links may only point under {mine}{prefix}; {url} is not'
        return None
