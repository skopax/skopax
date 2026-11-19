import base64

from skopax.remote import HttpError, Reply
from tests.fake_http import FakeHttp

API = 'https://api.github.com'
GRAPHQL = f'{API}/graphql'
WEB_BASE = 'https://github.com'
GHES = 'https://git.example.com'
GHES_API = f'{GHES}/api/v3'
GHES_GRAPHQL = f'{GHES}/api/graphql'
TOKEN = 'fake-github-token-for-tests'
VALUES = {'url': WEB_BASE, 'token': TOKEN, 'owner': 'acme'}
FULL = '2b2b2b2b' + 'a' * 32
OLD = '1a1a1a1a' + 'b' * 32
CART = 'acme/checkout-service'
CART_API = f'{API}/repos/{CART}'
CART_WEB = f'{WEB_BASE}/{CART}'


def repo_json(name: str, rid: int, owner: str = 'acme', branch: str = 'main', base: str = WEB_BASE, private: bool = False, archived: bool = False) -> dict:
    return {'id': rid, 'node_id': f'R_{rid}', 'name': name, 'full_name': f'{owner}/{name}', 'private': private, 'owner': {'login': owner, 'id': 9, 'type': 'Organization'},
            'html_url': f'{base}/{owner}/{name}', 'description': None, 'fork': False, 'url': f'{API}/repos/{owner}/{name}', 'archived': archived, 'disabled': False,
            'default_branch': branch, 'visibility': 'private' if private else 'public', 'pushed_at': '2026-10-15T07:00:00Z'}


def commit_json(sha: str, message: str, author: str, parents: int = 1, date: str = '2026-10-15T07:01:02Z', web: str = CART_WEB) -> dict:
    return {'sha': sha, 'node_id': 'C_x', 'url': f'{API}/repos/{CART}/commits/{sha}', 'html_url': f'{web}/commit/{sha}',
            'commit': {'author': {'name': author, 'email': f'{author.lower()}@example.com', 'date': date}, 'committer': {'name': 'GitHub', 'email': 'web@example.com', 'date': date},
                       'message': message, 'tree': {'sha': 't' * 40}, 'comment_count': 0},
            'author': {'login': author.lower(), 'id': 1, 'type': 'User'}, 'committer': {'login': 'web-flow', 'id': 2, 'type': 'User'},
            'parents': [{'sha': f'{i}' * 40, 'url': '', 'html_url': ''} for i in range(parents)]}


def pull_json(number: int, title: str, head: str, state: str = 'closed', merged: str | None = '2026-10-15T07:00:00Z', author: str = 'nina', web: str = CART_WEB) -> dict:
    return {'id': 1000 + number, 'number': number, 'state': state, 'locked': False, 'title': title, 'user': {'login': author, 'id': 3, 'type': 'User'}, 'body': 'secret plan',
            'created_at': '2026-10-14T09:00:00Z', 'updated_at': '2026-10-15T07:00:00Z', 'closed_at': merged, 'merged_at': merged, 'merge_commit_sha': FULL, 'draft': False,
            'head': {'ref': head, 'sha': FULL}, 'base': {'ref': 'main', 'sha': OLD}, 'html_url': f'{web}/pull/{number}'}


def listing(*names: str) -> list[dict]:
    return [repo_json(n, 100 + i) for i, n in enumerate(names)]


def content_json(path: str, text: str) -> dict:
    return {'type': 'file', 'encoding': 'base64', 'size': len(text), 'name': path.rsplit('/', 1)[-1], 'path': path, 'sha': 'f' * 40,
            'content': base64.encodebytes(text.encode()).decode(), 'html_url': f'{CART_WEB}/blob/main/{path}'}


def blame_json(ranges: list[tuple[int, int, str, str, str]]) -> dict:
    return {'data': {'repository': {'object': {'blame': {'ranges': [
        {'startingLine': a, 'endingLine': b, 'age': 1, 'commit': {'oid': sha, 'message': message, 'committedDate': '2026-10-01T10:00:00Z', 'authoredDate': '2026-10-01T09:00:00Z',
                                                                'author': {'name': author}}} for a, b, sha, author, message in ranges]}}}}}


def compare_json(commits: list[dict], files: list[dict] | None = None, status: str = 'ahead') -> dict:
    return {'url': f'{CART_API}/compare/x', 'html_url': f'{CART_WEB}/compare/{OLD[:8]}...{FULL[:8]}', 'status': status, 'ahead_by': len(commits), 'behind_by': 0,
            'total_commits': len(commits), 'commits': commits, 'files': files or [],
            'merge_base_commit': {'sha': OLD}, 'base_commit': {'sha': OLD}}


def deployment_json(did: int, sha: str, env: str = 'production', at: str = '2026-10-15T08:00:00Z', by: str = 'deploy-bot') -> dict:
    return {'id': did, 'node_id': f'DE_{did}', 'sha': sha, 'ref': 'main', 'task': 'deploy', 'payload': {'token': 'x'}, 'environment': env, 'description': 'cd',
            'creator': {'login': by, 'id': 7, 'type': 'Bot'}, 'created_at': at, 'updated_at': at, 'statuses_url': f'{CART_API}/deployments/{did}/statuses'}


def deployment_status_json(state: str, at: str = '2026-10-15T08:03:00Z') -> dict:
    return {'id': 5, 'state': state, 'description': 'rollout', 'environment': 'production', 'created_at': at, 'updated_at': at,
            'log_url': f'{CART_WEB}/actions/runs/77', 'environment_url': 'https://shop.example.com', 'creator': {'login': 'deploy-bot'}}


def release_json(rid: int, tag: str, name: str, at: str = '2026-10-15T09:00:00Z', draft: bool = False) -> dict:
    return {'id': rid, 'tag_name': tag, 'target_commitish': 'main', 'name': name, 'draft': draft, 'prerelease': False, 'created_at': at, 'published_at': None if draft else at,
            'author': {'login': 'nina', 'id': 3}, 'html_url': f'{CART_WEB}/releases/tag/{tag}', 'body': 'notes'}


def rate_limited(url: str, code: int = 429, retry: str | None = '30', remaining: str | None = None, reset: str | None = None, body: str = '') -> HttpError:
    headers = {k: v for k, v in (('retry-after', retry), ('x-ratelimit-remaining', remaining), ('x-ratelimit-reset', reset)) if v is not None}
    return HttpError(code, url, body or '{"message": "API rate limit exceeded"}', headers)


def github_http(routes: dict | None = None, names: tuple[str, ...] = ('checkout-service', 'users-service', 'orders-service')) -> FakeHttp:
    base = {
        ('GET', f'{API}/user/repos'): listing(*names),
        ('GET', CART_API): repo_json('checkout-service', 100),
        ('GET', f'{CART_API}/commits/2b2b2b2b'): commit_json(FULL, 'Merge pull request #12 from acme/OPS-115\n\nOPS-115 bundle calc', 'Oscar', 2),
        ('GET', f'{CART_API}/commits/{FULL}'): commit_json(FULL, 'Merge pull request #12 from acme/OPS-115\n\nOPS-115 bundle calc', 'Oscar', 2),
        ('GET', f'{CART_API}/compare/1a1a1a1a...{FULL}'): compare_json([commit_json('c1' * 20, 'WEB-203 bot session, see OPS-106', 'Adam'), commit_json('c2' * 20, 'OPS-115 bundle calc', 'Nina'),
                                                                     commit_json('c3' * 20, 'Merge pull request #12 from acme/OPS-115', 'Oscar', 2)]),
    }
    base[('GET', f'{CART_API}/compare/1a1a1a1a...2b2b2b2b')] = base[('GET', f'{CART_API}/compare/1a1a1a1a...{FULL}')]
    base.update(routes or {})
    return FakeHttp(base)


def paged(first: list, rest: list, url: str):
    def answer(q, _):
        if q.get('page', ['1']) == ['1']:
            return Reply(first, {'link': f'<{url}?per_page=100&page=2>; rel="next", <{url}?per_page=100&page=2>; rel="last"'})
        return Reply(rest, {'link': f'<{url}?per_page=100&page=1>; rel="prev"'})
    return answer
