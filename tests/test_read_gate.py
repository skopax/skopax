import asyncio
import dataclasses
import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pymongo.errors import ServerSelectionTimeoutError
from starlette.routing import Mount, WebSocketRoute

from tests.conftest import AUTH, state
from tests.test_auth import app_client, sso_settings
from skopax.apitokens import ApiTokens
from skopax.app import OPEN_PATHS, is_open
from skopax.auth import SESSION_COOKIE
from skopax.messages import msg
from skopax.sessions import Sessions, session_secret
from skopax.store import Store
from skopax.users import Users

CHECK_THEIR_OWN_CREDENTIALS = {
    '/mcp': 'the MCP endpoint takes only identity provider access tokens, parsed by auth.bearer_token; sessions and API tokens never open it',
    '/chat': 'the chat mount keeps its own session check (chatui.needs_session) for every path under it except the shell and its assets',
}
WEBSOCKET_PATHS = {
    '/chat/ws/socket.io': 'websocket upgrades skip HTTP middleware; Chainlit checks its own JWT',
}
FOUND_OPEN = [
    ('DELETE', '/mcp'),
    ('GET', '/.well-known/oauth-protected-resource'),
    ('GET', '/.well-known/oauth-protected-resource/mcp'),
    ('GET', '/actuator/health'),
    ('GET', '/actuator/health/liveness'),
    ('GET', '/actuator/health/readiness'),
    ('GET', '/authorized'),
    ('GET', '/icons/x'),
    ('GET', '/login'),
    ('GET', '/login/sso'),
    ('GET', '/logout'),
    ('GET', '/manifest.webmanifest'),
    ('GET', '/mcp'),
    ('GET', '/plugins/x/x'),
    ('GET', '/setup'),
    ('GET', '/static/x'),
    ('HEAD', '/mcp'),
    ('POST', '/login'),
    ('POST', '/logout'),
    ('POST', '/mcp'),
    ('POST', '/setup'),
]
ANY_METHOD = ('GET', 'POST', 'PUT', 'PATCH', 'DELETE')
CHAT_API_PROBES = ('/chat/user', '/chat/project/settings')
CHAT_SELF_GUARDED_PROBES = CHAT_API_PROBES + ('/chat/thread/aaaabbbb-cccc-4ddd-8eee-ffff00001111',)
NO_LOGIN_REDIRECT = CHAT_API_PROBES + ('/api/admin/mcp-tools/oauth/callback',)


def gated(**over):
    return dataclasses.replace(sso_settings(), public_read=False, **over)


def concrete(path: str) -> str:
    return re.sub(r'\{[^}]*\}', lambda m: '1' if m.group(0) == '{num}' else 'x', path)


def walk(routes, prefix: str = ''):
    for route in routes:
        if isinstance(route, WebSocketRoute):
            yield 'WS', prefix + route.path
        elif isinstance(route, APIRoute):
            for method in sorted(route.methods):
                yield method, prefix + route.path
        elif isinstance(route, Mount):
            yield 'MOUNT', prefix + route.path
            for kind, path in walk(getattr(route.app, 'routes', ()) or (), prefix + route.path):
                if kind == 'WS':
                    yield kind, path
        elif hasattr(route, 'path'):
            for method in sorted(route.methods or set(ANY_METHOD)):
                yield method, prefix + route.path


def login_cookie(store: Store, email: str = 'admin@example.com', role: str = 'admin') -> str:
    return Sessions(session_secret(store.db, sso_settings()), 3600, 7200).issue(email, role, 'local')


class Routed:
    def __init__(self, app):
        self.app = app
        self.scope = None

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            self.scope = scope
        await self.app(scope, receive, send)

    @property
    def reached(self) -> bool:
        return self.scope is not None and 'endpoint' in self.scope


def test_every_route_is_gated_or_explicitly_open(store: Store):
    # given
    routed = Routed(app_client(store, s=gated(chat_ui=True)).app)
    with TestClient(routed, base_url='https://testserver') as client:
        entries = list(walk(routed.app.routes))
        mounts = sorted(p for kind, p in entries if kind == 'MOUNT')
        sockets = sorted(p for kind, p in entries if kind == 'WS')
        probes = [(m, concrete(p)) for m, p in entries if m not in ('WS', 'MOUNT')]
        probes += [('GET', concrete(p) + '/x') for kind, p in entries if kind == 'MOUNT' and p not in CHECK_THEIR_OWN_CREDENTIALS]
        probes += [('GET', p) for p in CHAT_SELF_GUARDED_PROBES] + [('GET', '/chat'), ('GET', '/chat/assets/x')]

        # when
        open_found, gate_failures, open_by_gate = set(), [], []
        for method, path in probes:
            if is_open(path):
                open_found.add((method, path))
                res = client.request(method, path, follow_redirects=False)
                if not routed.reached:
                    open_by_gate.append((method, path, res.status_code))
                continue
            as_json = client.request(method, path, follow_redirects=False)
            if as_json.status_code != 401 or routed.reached:
                gate_failures.append((method, path, 'json', as_json.status_code))
            elif method != 'HEAD' and as_json.json() != {'detail': msg('access.login_first')} and not path.startswith('/chat'):
                gate_failures.append((method, path, 'detail', as_json.json()))
            if method in ('GET', 'HEAD'):
                as_page = client.request(method, path, headers={'accept': 'text/html'}, follow_redirects=False)
                wanted = 401 if path in NO_LOGIN_REDIRECT else 303
                if as_page.status_code != wanted or wanted == 303 and not as_page.headers['location'].startswith('/login?next='):
                    gate_failures.append((method, path, 'html', as_page.status_code))

    # then
    assert gate_failures == []
    assert open_by_gate == [], 'an open path must reach its route; the gate answered instead'
    assert sorted(open_found) == FOUND_OPEN, 'an open route was added or removed: classify it on purpose'
    assert '/chat' in mounts and set(mounts) <= set(CHECK_THEIR_OWN_CREDENTIALS)
    assert sockets == [] or set(sockets) <= set(WEBSOCKET_PATHS)
    print('open paths:', sorted(open_found))
    print('mounts:', mounts, 'websockets:', sockets)


def test_the_open_list_comes_from_one_definition():
    # then
    assert is_open('/login') and is_open('/static/app.js') and is_open('/actuator/health/liveness')
    assert not is_open('/api/state') and not is_open('/')
    assert '/login' in OPEN_PATHS


def test_open_prefixes_stop_at_a_path_boundary():
    # when
    near_misses = [p for p in ('/loginx', '/statics', '/static', '/iconsx/a.png', '/actuator/healthx', '/actuator/health-check', '/setupx', '/logout2',
                               '/login/x', '/setup/x', '/mcpx', '/mcp/x', '/.well-known/oauth-protected-resourcex') if is_open(p)]
    kept = [p for p in ('/actuator/health', '/actuator/health/liveness', '/static/app.js', '/icons/icon-192.png', '/login/sso',
                        '/.well-known/oauth-protected-resource', '/.well-known/oauth-protected-resource/mcp', '/mcp/', '/login/', '/setup/') if not is_open(p)]

    # then
    assert near_misses == [] and kept == []


def test_public_read_on_allows_reads_not_writes(store: Store):
    # given
    with app_client(store, s=dataclasses.replace(sso_settings(), public_read=True)) as client:
        # when
        read = client.get('/api/rev')
        head = client.head('/')
        write = client.put('/api/state', json={})
        action = client.post('/api/actions', json={})
        page = client.get('/', headers={'accept': 'text/html'}, follow_redirects=False)

    # then
    assert read.status_code == 200
    assert head.status_code not in (401, 303)
    assert page.status_code == 200
    assert write.status_code == 401 and write.json() == {'detail': 'log in first'}
    assert action.status_code == 401


def test_head_is_treated_like_get(store: Store):
    # given
    with app_client(store, s=gated()) as client:
        # when
        head = client.head('/', headers={'accept': 'text/html'}, follow_redirects=False)
        open_head = client.head('/login', follow_redirects=False)

    # then
    assert head.status_code == 303 and head.headers['location'] == '/login?next=%2F'
    assert open_head.status_code != 303 and open_head.status_code != 401


def test_html_get_redirects_with_next(store: Store):
    # given
    with app_client(store, s=gated()) as client:
        # when
        page = client.get('/c/14', headers={'accept': 'text/html'}, follow_redirects=False)
        evil = client.get('https://testserver//evil.example.com/x', headers={'accept': 'text/html'}, follow_redirects=False)
        queried = client.get('/api/cards', params={'q': 'a b&c', 'x': '//evil.example.com'}, headers={'accept': 'text/html'}, follow_redirects=False)
        api = client.get('/api/rev', follow_redirects=False)
        post = client.post('/api/actions', json={}, headers={'accept': 'text/html'}, follow_redirects=False)

    # then
    assert page.status_code == 303 and page.headers['location'] == '/login?next=%2Fc%2F14'
    assert evil.status_code == 303 and evil.headers['location'] == '/login?next=%2F', 'a path that looks like another site never becomes next'
    assert queried.status_code == 303
    assert queried.headers['location'].startswith('/login?next=%2Fapi%2Fcards%3Fq%3D')
    assert '//evil' not in queried.headers['location'].split('next=', 1)[1].replace('%2F%2F', '')
    assert api.status_code == 401 and api.json() == {'detail': 'log in first'}
    assert post.status_code == 401


def test_a_session_passes_the_gate(store: Store):
    # given
    with app_client(store, s=gated()) as client:
        client.cookies.set(SESSION_COOKIE, login_cookie(store))

        # when
        res = client.get('/api/rev')

    # then
    assert res.status_code == 200


def test_bearer_token_passes_the_gate(store: Store):
    # given
    with app_client(store, s=gated()) as client:
        # when
        bootstrap = client.get('/api/rev', headers=AUTH)
        wrong = client.get('/api/rev', headers={'Authorization': 'Bearer nope'})
        made = client.post('/api/admin/tokens', json={'name': 'reader', 'role': 'viewer'}, headers=AUTH)
        issued = made.json().get('token')
        api = client.get('/api/rev', headers={'Authorization': f'Bearer {issued}'})

    # then
    assert bootstrap.status_code == 200
    assert wrong.status_code == 401
    assert made.status_code == 201 and issued
    assert api.status_code == 200


def test_toggle_is_live_without_restart(store: Store):
    # given
    with app_client(store, s=gated()) as client:
        before = client.get('/api/rev')

        # when
        item = client.put('/api/admin/settings', json={'key': 'access.public-read', 'value': 'true'}, headers=AUTH)
        on = client.get('/api/rev')
        write = client.put('/api/state', json={})
        client.put('/api/admin/settings', json={'key': 'access.public-read', 'value': 'false'}, headers=AUTH)
        off = client.get('/api/rev')

    # then
    assert before.status_code == 401
    assert item.status_code == 200 and item.json()['item']['live'] is True
    assert on.status_code == 200 and write.status_code == 401
    assert off.status_code == 401


def test_the_tunable_is_declared_in_the_access_group():
    # given
    from skopax.tunables import registry
    found = {t.key: t for t in registry()}['access.public-read']

    # then
    assert (found.field, found.kind, found.group, found.live) == ('public_read', 'bool', 'Access', True)
    assert found.help == 'Let anyone who can reach the page read the board without logging in. Changes still need a login.'


def test_the_tunable_defaults_to_the_setting(store: Store):
    # given
    on = app_client(store, s=dataclasses.replace(sso_settings(), public_read=True))
    off = app_client(store, s=gated())

    # when
    with on, off:
        got_on = {i['key']: i for i in on.get('/api/admin/settings', headers=AUTH).json()['items']}['access.public-read']
        got_off = {i['key']: i for i in off.get('/api/admin/settings', headers=AUTH).json()['items']}['access.public-read']

    # then
    assert (got_on['value'], got_on['default']) == ('true', 'false')
    assert (got_off['value'], got_off['default']) == ('false', 'false')


class WatchedCollection:
    def __init__(self, db, name):
        self.db = db
        self.name = name

    def __getattr__(self, attr):
        target = getattr(self.db.real[self.name], attr)

        def call(*args, **kwargs):
            try:
                asyncio.get_running_loop()
                self.db.on_loop.append((self.name, attr))
            except RuntimeError:
                pass
            if self.db.down and self.name in self.db.down:
                raise ServerSelectionTimeoutError('mongo is down')
            return target(*args, **kwargs)
        return call


class WatchedDb:
    def __init__(self, real):
        self.real = real
        self.on_loop = []
        self.down = set()

    def __getitem__(self, name):
        return WatchedCollection(self, name)

    def __getattr__(self, name):
        return getattr(self.real, name)


def watched(store: Store) -> WatchedDb:
    db = WatchedDb(store.db)
    store.db = db
    return db


def test_no_session_or_token_lookup_runs_on_the_event_loop(store: Store):
    # given
    db = watched(store)
    with app_client(store, s=gated()) as client:
        cookie = login_cookie(store)
        made = client.post('/api/admin/tokens', json={'name': 'reader', 'role': 'viewer'}, headers=AUTH).json()['token']
        db.on_loop.clear()

        # when
        client.cookies.set(SESSION_COOKIE, cookie)
        by_session = client.get('/api/rev')
        client.cookies.clear()
        by_token = client.get('/api/rev', headers={'Authorization': f'Bearer {made}'})
        by_bad = client.get('/api/rev', headers={'Authorization': 'Bearer nope'})
        anonymous = client.get('/api/rev')

    # then
    assert (by_session.status_code, by_token.status_code, by_bad.status_code, anonymous.status_code) == (200, 200, 401, 401)
    assert db.on_loop == []


def test_a_cookie_is_resolved_once_per_request(store: Store, monkeypatch):
    # given
    monkeypatch.setattr('skopax.app.USER_CACHE_SECONDS', 0)
    db = watched(store)
    calls = []
    original = Users.get
    monkeypatch.setattr(Users, 'get', lambda self, name: calls.append(name) or original(self, name))
    with app_client(store, s=gated()) as client:
        client.cookies.set(SESSION_COOKIE, login_cookie(store))
        client.get('/api/rev')

        # when
        seen = {}
        for path in ('/api/rev', '/api/chats'):
            calls.clear()
            db.on_loop.clear()
            res = client.get(path)
            seen[path] = (res.status_code, len(calls), list(db.on_loop))

    # then
    assert seen == {'/api/rev': (200, 1, []), '/api/chats': (200, 1, [])}


def test_a_bearer_token_is_resolved_once_per_request(store: Store, monkeypatch):
    # given
    calls = []
    original = ApiTokens.resolve
    monkeypatch.setattr(ApiTokens, 'resolve', lambda self, given, **kw: calls.append(given) or original(self, given, **kw))
    with app_client(store, s=gated()) as client:
        made = client.post('/api/admin/tokens', json={'name': 'writer', 'role': 'operator'}, headers=AUTH).json()['token']
        bearer = {'Authorization': f'Bearer {made}'}

        # when
        seen = {}
        for name, call in (('login', lambda: client.get('/api/chats', headers=bearer)), ('writer', lambda: client.put('/api/state', json=state(), headers=bearer))):
            calls.clear()
            res = call()
            seen[name] = (res.status_code, len(calls))

    # then
    assert seen == {'login': (200, 1), 'writer': (200, 1)}


def broken_key(monkeypatch):
    def down(*_):
        raise ServerSelectionTimeoutError('down')
    monkeypatch.setattr('skopax.app.session_secret', down)


def test_a_paused_session_lookup_answers_503_for_api_and_page(store: Store, monkeypatch):
    # given
    cookie = login_cookie(store)
    broken_key(monkeypatch)
    with app_client(store, s=gated()) as client:
        client.cookies.set(SESSION_COOKIE, cookie)

        # when
        api = client.get('/api/rev')
        page = client.get('/', headers={'accept': 'text/html'}, follow_redirects=False)
        head = client.head('/', headers={'accept': 'text/html'}, follow_redirects=False)
        write = client.post('/api/actions', json={})

    # then
    assert api.status_code == 503 and api.json() == {'detail': msg('db.unavailable')}
    assert page.status_code == 503 and msg('db.page_unavailable') in page.text
    assert head.status_code == 503
    assert write.status_code == 503 and write.json() == {'detail': msg('db.unavailable')}


def test_a_token_is_checked_before_a_paused_cookie(store: Store, monkeypatch):
    # given
    cookie = login_cookie(store)
    broken_key(monkeypatch)
    with app_client(store, s=gated()) as client:
        client.cookies.set(SESSION_COOKIE, cookie)

        # when
        valid = client.get('/api/rev', headers=AUTH)
        wrong = client.get('/api/rev', headers={'Authorization': 'Bearer nope'})
        client.cookies.set(SESSION_COOKIE, 'garbage')
        garbage = client.get('/api/rev', headers=AUTH)
        client.cookies.clear()
        anonymous = client.get('/api/rev')

    # then
    assert valid.status_code == 200
    assert garbage.status_code == 200
    assert wrong.status_code == 401 and wrong.json() == {'detail': msg('access.token_invalid')}
    assert anonymous.status_code == 401 and anonymous.json() == {'detail': msg('access.login_first')}


def test_an_html_head_on_a_chat_thread_redirects(store: Store):
    # given
    thread = '/chat/thread/aaaabbbb-cccc-4ddd-8eee-ffff00001111'
    with app_client(store, s=gated(chat_ui=True)) as client:
        # when
        head = client.head(thread, headers={'accept': 'text/html'}, follow_redirects=False)
        get = client.get(thread, headers={'accept': 'text/html'}, follow_redirects=False)
        api = client.get(thread, follow_redirects=False)

    # then
    assert head.status_code == 303 and head.headers['location'] == f'/login?next={thread}'
    assert get.status_code == 303 and get.headers['location'] == head.headers['location']
    assert api.status_code == 401


def test_a_database_failure_at_the_gate_answers_503_not_401(store: Store):
    # given
    db = watched(store)
    with app_client(store, s=gated()) as client:
        made = client.post('/api/admin/tokens', json={'name': 'reader', 'role': 'viewer'}, headers=AUTH).json()['token']
        db.down = {'api_tokens'}

        # when
        by_token = client.get('/api/rev', headers={'Authorization': f'Bearer {made}'})
        page = client.get('/', headers={'Authorization': f'Bearer {made}', 'accept': 'text/html'})

    # then
    assert by_token.status_code == 503
    assert by_token.json() == {'detail': msg('db.unavailable')}
    assert page.status_code == 503 and msg('db.page_unavailable') in page.text


BASIC = {'Authorization': 'Basic YTpi'}


def test_a_basic_header_does_not_hide_a_valid_session(store: Store):
    # given
    with app_client(store, s=gated()) as client:
        client.cookies.set(SESSION_COOKIE, login_cookie(store))

        # when
        api = client.get('/api/rev', headers=BASIC)
        page = client.get('/', headers={**BASIC, 'accept': 'text/html'}, follow_redirects=False)
        guarded = client.get('/api/admin/settings', headers=BASIC)

    # then
    assert api.status_code == 200
    assert page.status_code == 200 and 'text/html' in page.headers['content-type']
    assert guarded.status_code == 200


def test_a_basic_header_without_a_session_counts_as_anonymous(store: Store):
    # given
    with app_client(store, s=gated()) as client:
        # when
        api = client.get('/api/rev', headers=BASIC)
        page = client.get('/', headers={**BASIC, 'accept': 'text/html'}, follow_redirects=False)
        write = client.post('/api/actions', json={}, headers=BASIC)

    # then
    assert api.status_code == 401 and api.json() == {'detail': msg('access.login_first')}
    assert page.status_code == 303 and page.headers['location'] == '/login?next=%2F'
    assert write.status_code == 401 and write.json() == {'detail': msg('access.login_first')}


def test_a_basic_header_and_a_valid_session_still_cannot_use_a_machine_route(store: Store):
    # given
    with app_client(store, s=gated()) as client:
        client.cookies.set(SESSION_COOKIE, login_cookie(store))

        # when
        res = client.put('/api/state', json=state(), headers=BASIC)

    # then
    assert res.status_code == 401 and res.json() == {'detail': msg('access.token_invalid')}


def test_a_failed_bearer_header_fails_even_with_a_valid_session(store: Store):
    # given
    with app_client(store, s=gated()) as client:
        client.cookies.set(SESSION_COOKIE, login_cookie(store))

        # when
        res = client.get('/api/rev', headers={'Authorization': 'Bearer nope'})

    # then
    assert res.status_code == 401 and res.json() == {'detail': msg('access.token_invalid')}


@pytest.mark.parametrize('value', ['Bearer', 'Bearer ', 'bearer', 'Digest abc', ''])
def test_an_empty_or_foreign_authorization_value_is_absent(store: Store, value: str):
    # given
    with app_client(store, s=gated()) as client:
        client.cookies.set(SESSION_COOKIE, login_cookie(store))

        # when
        with_session = client.get('/api/rev', headers={'Authorization': value})
        client.cookies.clear()
        anonymous = client.get('/api/rev', headers={'Authorization': value})

    # then
    assert with_session.status_code == 200
    assert anonymous.status_code == 401 and anonymous.json() == {'detail': msg('access.login_first')}


def test_the_bearer_scheme_is_case_insensitive(store: Store):
    # given
    with app_client(store, s=gated()) as client:
        # when
        res = client.get('/api/rev', headers={'Authorization': 'bEaReR test-token'})

    # then
    assert res.status_code == 200
