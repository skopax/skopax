import base64
import dataclasses
import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from skopax.actors import SYSTEM
from skopax.app import create_app
from skopax.auth import SESSION_COOKIE
from skopax.messages import msg
from skopax.registry import Registry
from skopax.sealer import Sealer
from skopax.sessions import Sessions, session_secret
from skopax.users import Users
from skopax.engine import iso
from skopax.mcptools import PLUGIN_ID, STATE_TTL_S, STATES_PER_INSTANCE, NoTokens, Reconnect, TokenSource
from skopax.mcptools import oauth as oauth_module
from skopax.mcptools import presets, routes, warnings
from skopax.mcptools.catalog import Catalog
from skopax.mcptools.client import Capped, McpFailure, client_for
from skopax.mcptools.oauth import FlowError, McpOAuth
from skopax.plugins import mcp_tools
from tests.conftest import settings
from tests.mcp_fake import AUTH_ORIGIN, SERVER_SECRET, FakeAuth, FakeResolver, FakeTokens

ALICE = 'alice@example.com'
BOB = 'bob@example.com'
URL = 'https://mcp.example.com/mcp'
BASE = 'https://skopax.example.com'
REDIRECT = f'{BASE}/api/admin/mcp-tools/oauth/callback'
LANDING = f'{BASE}/#t=integrations/mcp/main'
INSTANCES = f'/api/admin/plugins/{PLUGIN_ID}/instances'
START = '/api/admin/mcp-tools/instances/main/oauth/start'
CALLBACK = '/api/admin/mcp-tools/oauth/callback'
TEST = '/api/admin/plugins/mcp-tools/test'
OAUTH = {'url': URL, 'auth': 'oauth'}
FAKE_HOSTS = ('mcp.example.com', 'auth.example.com')
SEALER = Sealer('t40-test-key')
HOME = Path(mcp_tools.__file__).parent
DRIVE = next(p for p in presets.load() if p.id == 'google-drive')


@contextmanager
def app(store, fake: FakeAuth, s=None, now: list | None = None):
    s = s or settings()
    users = Users(store.db)
    users.create(ALICE, 'Alice', 'admin')
    users.create(BOB, 'Bob', 'admin')
    with TestClient(create_app(s, store, registry=Registry.of(mcp_tools.PLUGIN, directory=HOME)), base_url='https://testserver') as c:
        c.app.state.mcptools.http = lambda ca: fake.client
        if now is not None:
            c.app.state.mcptools.tokens.clock = lambda: now[0]
        login(c, store, ALICE, s)
        yield c


def login(c, store, who: str, s=None) -> None:
    c.cookies.set(SESSION_COOKIE, Sessions(session_secret(store.db, s or settings()), 3600, 7200).issue(who, 'admin', 'local'))


def add(c, values: dict | None = None):
    return c.post(INSTANCES, json={'name': 'main', 'values': OAUTH if values is None else values, 'enabled': False})


def switched_on(c, store) -> None:
    store.db['instances'].update_one({'_id': f'{PLUGIN_ID}/main'}, {'$set': {'enabled': True}})
    c.app.state.instances.refresh()


def start(c) -> str:
    res = c.post(START)
    assert res.status_code == 200, res.text
    return res.json()['url']


def connect(c, fake: FakeAuth):
    code, state = fake.authorize(start(c))
    return c.get(CALLBACK, params={'code': code, 'state': state}, follow_redirects=False)


def view(c) -> dict:
    return c.get('/api/admin/mcp-tools/instances/main').json()


def checked(c, values: dict | None = None) -> dict:
    return c.post(TEST, json={'name': 'main', 'values': values or {}}).json()


def query(url: str) -> dict:
    return dict(parse_qsl(urlsplit(url).query))


def auth_doc(store) -> dict:
    return store.db['mcp_auth'].find_one({'_id': 'main'}) or {}


def activity(store) -> list[str]:
    return [d['text'] for d in store.activity.find()]


def opened(store, sealer: Sealer = SEALER) -> dict:
    return json.loads(sealer.open(auth_doc(store)['tokens']))


def direct(store, fake: FakeAuth, now: list, base: str = BASE, sealer: Sealer = SEALER) -> tuple[McpOAuth, Catalog, list]:
    notes = []
    catalog = Catalog(store.db)
    auth = McpOAuth(store.db, lambda: sealer, lambda: base, catalog, note=lambda by, text: notes.append((by, text)), http=lambda ca: fake.client, clock=lambda: now[0])
    return auth, catalog, notes


def linked(auth: McpOAuth, fake: FakeAuth, values: dict = OAUTH) -> None:
    code, state = fake.authorize(auth.start('main', values, False, ALICE))
    assert auth.finish(code, state, None, ALICE) == ('main', 'connected')


class Split(httpx.AsyncBaseTransport):
    def __init__(self, fake: FakeAuth):
        self.fake = Capped(httpx.ASGITransport(app=fake.app))
        self.real = client_for()._transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        target = self.fake if request.url.host in FAKE_HOSTS else self.real
        return await target.handle_async_request(request)

    async def aclose(self) -> None:
        await self.real.aclose()


def test_start_discovers_registers_and_builds_a_pkce_url(store):
    # given
    fake = FakeAuth()
    with app(store, fake) as c:
        add(c)

        # when
        res = c.post(START, headers={'host': 'evil.example.com'})
        again = c.post(START)
        shown = view(c)

    # then
    assert res.status_code == 200 and again.status_code == 200
    url = res.json()['url']
    parts, found = urlsplit(url), query(url)
    assert f'{parts.scheme}://{parts.netloc}{parts.path}' == f'{AUTH_ORIGIN}/authorize'
    assert (found['response_type'], found['code_challenge_method'], found['resource'], found['client_id']) == ('code', 'S256', URL, 'client-1')
    assert found['redirect_uri'] == REDIRECT and 'scope' not in found and len(found['code_challenge']) == 43
    states = list(store.db['mcp_oauth_states'].find())
    hashed = hashlib.sha256(found['state'].encode()).hexdigest()
    mine = next(s for s in states if s['_id'] == hashed)
    assert mine['verifier'].startswith('enc:v1:') and (mine['instance'], mine['by']) == ('main', ALICE)
    assert found['state'] not in json.dumps(states, default=str) and found['code_challenge'] not in json.dumps(states, default=str)
    assert fake.registered == [{'redirect_uris': [REDIRECT], 'token_endpoint_auth_method': 'none', 'grant_types': ['authorization_code', 'refresh_token'],
                                'response_types': ['code'], 'client_name': 'Skopax at skopax.example.com'}]
    assert mine['client'].startswith('enc:v1:') and (mine['tokenEndpoint'], mine['resource'], mine['issuer']) == (f'{AUTH_ORIGIN}/token', URL, f'{AUTH_ORIGIN}/')
    doc = auth_doc(store)
    assert doc['registered']['client'].startswith('enc:v1:') and not {'tokens', 'client', 'tokenEndpoint'} & set(doc)
    assert shown['auth']['state'] == 'none'
    assert msg('mcptools.oauth.started', by=ALICE, instance='main') in activity(store)
    assert not [s for s in fake.seen if 'evil' in s[1]]


def test_a_server_without_registration_needs_a_client_id(store):
    # given
    fake = FakeAuth(registration=False)
    with app(store, fake) as c:
        add(c)

        # when
        refused = c.post(START)
        c.patch(f'{INSTANCES}/main', json={'values': {'oauth-client-id': 'shop-client', 'oauth-client-secret': 'shop-cl1ent-s3cr3t'}})
        url = start(c)
        code, state = fake.authorize(url)
        landed = c.get(CALLBACK, params={'code': code, 'state': state}, follow_redirects=False)

    # then
    assert (refused.status_code, refused.json()) == (409, {'detail': msg('mcptools.oauth.needs_client')})
    assert fake.registered == [] and query(url)['client_id'] == 'shop-client' and 'shop-cl1ent-s3cr3t' not in url
    exchange = fake.token_calls[0]
    assert exchange['headers']['authorization'].startswith('Basic ') and 'client_secret' not in exchange['form'] and exchange['form']['client_id'] == 'shop-client'
    assert landed.headers['location'] == LANDING and auth_doc(store)['state'] == 'connected'


def test_a_server_without_s256_is_refused(store):
    # given
    fake = FakeAuth(s256=False)
    with app(store, fake) as c:
        add(c)

        # when
        res = c.post(START)

    # then
    assert (res.status_code, res.json()) == (502, {'detail': msg('mcptools.oauth.no_pkce')})
    assert fake.registered == [] and store.db['mcp_oauth_states'].count_documents({}) == 0


@pytest.mark.parametrize('knob', [{'metadata': {'authorization_endpoint': 'https://169.254.169.254/latest'}}, {'metadata': {'token_endpoint': 'https://169.254.169.254/latest'}},
                                  {'metadata': {'registration_endpoint': 'https://169.254.169.254/latest'}}, {'servers': ['https://169.254.169.254']},
                                  {'challenge': 'https://169.254.169.254/.well-known/oauth-protected-resource'}],
                         ids=['authorization-endpoint', 'token-endpoint', 'registration-endpoint', 'authorization-server', 'resource-metadata'])
def test_discovered_endpoints_pass_the_guard(store, knob):
    # given
    fake = FakeAuth(**knob)
    with FakeResolver({}) as resolver, app(store, fake) as c:
        add(c)
        c.app.state.mcptools.http = lambda ca: (lambda: httpx.AsyncClient(transport=Split(fake)))

        # when
        res = c.post(START)

    # then
    assert (res.status_code, res.json()) == (502, {'detail': msg('mcptools.url.blocked')})
    assert resolver.asked == [] and not [s for s in fake.seen if '169.254' in s[1]]
    assert fake.registered == [] and store.db['mcp_oauth_states'].count_documents({}) == 0


@pytest.mark.parametrize('knob, key', [({'prm': False}, 'mcptools.oauth.discovery'), ({'resource': 'https://other.example.com/mcp'}, 'mcptools.oauth.resource'),
                                       ({'metadata': {'issuer': 'https://issuer.example.com'}}, 'mcptools.oauth.issuer'),
                                       ({'register_status': 400}, 'mcptools.oauth.registration')], ids=['no-metadata', 'resource', 'issuer', 'registration'])
def test_discovery_refusals_map_to_fixed_texts(store, knob, key):
    # given
    fake = FakeAuth(**knob)
    with app(store, fake) as c:
        add(c)

        # when
        res = c.post(START)

    # then
    assert (res.status_code, res.json()) == (502, {'detail': msg(key)})
    assert 's3cr3t' not in res.text and store.db['mcp_oauth_states'].count_documents({}) == 0


def test_start_needs_an_oauth_connection_and_a_usable_base_url(store):
    # given
    fake = FakeAuth()
    now = [1_000_000.0]
    bases = {base: direct(store, fake, now, base)[0] for base in ('', 'http://skopax.example.com', 'http://localhost', 'ftp://skopax.example.com',
                                                                  'https://skopax.example.com/', 'http://localhost:8080', 'http://127.0.0.1:8080')}
    with app(store, fake) as c:
        add(c, values={'url': URL, 'auth': 'header', 'header-value': 'Bearer h3ader-s3cr3t'})

        # when
        header = c.post(START)
        missing = c.post('/api/admin/mcp-tools/instances/docs/oauth/start')

    # then
    assert (header.status_code, header.json()) == (422, {'detail': msg('mcptools.oauth.not_oauth'), 'field': 'auth'})
    assert missing.status_code == 404
    refused = {}
    for base, auth in bases.items():
        try:
            refused[base] = auth.redirect_uri()
        except FlowError as e:
            refused[base] = (e.status, e.text)
    assert refused == {'': (409, msg('mcptools.oauth.base_url')), 'http://skopax.example.com': (409, msg('mcptools.oauth.base_url')),
                       'http://localhost': (409, msg('mcptools.oauth.base_url')), 'ftp://skopax.example.com': (409, msg('mcptools.oauth.base_url')),
                       'https://skopax.example.com/': REDIRECT, 'http://localhost:8080': 'http://localhost:8080/api/admin/mcp-tools/oauth/callback',
                       'http://127.0.0.1:8080': 'http://127.0.0.1:8080/api/admin/mcp-tools/oauth/callback'}


def test_the_callback_exchanges_once_and_seals_the_tokens(store):
    # given
    fake = FakeAuth()
    with app(store, fake) as c:
        add(c)
        before = store.db['mcp_tools'].find_one({'_id': 'main'})['account']
        code, state = fake.authorize(start(c))

        # when
        first = c.get(CALLBACK, params={'code': code, 'state': state}, follow_redirects=False)
        second = c.get(CALLBACK, params={'code': code, 'state': state}, follow_redirects=False)
        shown = view(c)
        sealer = c.app.state.mcptools.tokens.sealer()

    # then
    assert (first.status_code, first.headers['location'], first.content) == (303, LANDING, b'')
    assert (second.status_code, second.headers['location']) == (303, f'{BASE}/#t=integrations/signin-expired'), 'a used state lands on the expired note'
    assert fake.exchanges == 1
    form = fake.token_calls[0]['form']
    assert (form['grant_type'], form['redirect_uri'], form['resource'], form['client_id']) == ('authorization_code', REDIRECT, URL, 'client-1') and form['code_verifier']
    doc = auth_doc(store)
    assert doc['tokens'].startswith('enc:v1:') and 'access-1' not in json.dumps(doc, default=str) and (doc['state'], doc['rev']) == ('connected', 1)
    assert opened(store, sealer) | {'expires_at': None} == {'access_token': 'access-1-s3cr3t', 'refresh_token': 'refresh-1-s3cr3t', 'expires_at': None, 'scope': 'read', 'token_type': 'Bearer'}
    assert store.db['mcp_tools'].find_one({'_id': 'main'})['account'] != before
    auth = shown['auth']
    assert (auth['mode'], auth['state'], auth['connectedBy'], auth['last']['result'], auth['last']['text']) == ('oauth', 'connected', ALICE, 'connected', msg('mcptools.oauth.result.connected'))
    assert auth['expiresAt'] and auth['connectedAt'] and 's3cr3t' not in json.dumps(shown)
    assert msg('mcptools.oauth.connected', by=ALICE, instance='main') in activity(store)


def test_the_callback_never_echoes_code_state_or_error(store, caplog):
    # given
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.WARNING, logger='httpx')
    fake = FakeAuth()
    with app(store, fake) as c:
        add(c)
        _, denied_state = fake.authorize(start(c))
        _, failed_state = fake.authorize(start(c))

        # when
        denied = c.get(CALLBACK, params={'error': 'access_denied', 'error_description': '<script>s3cr3t</script>', 'state': denied_state}, follow_redirects=False)
        after_denied = view(c)['auth']['last']
        failed = c.get(CALLBACK, params={'code': 'c0de-s3cr3t', 'state': failed_state}, follow_redirects=False)
        shown = view(c)
        notices = c.get('/api/admin/notices').json()

    # then
    for res in (denied, failed):
        assert (res.status_code, res.headers['location'], res.content) == (303, LANDING, b'')
    assert (after_denied['result'], after_denied['text']) == ('denied', msg('mcptools.oauth.result.denied'))
    assert (shown['auth']['last']['result'], shown['auth']['last']['text']) == ('failed', msg('mcptools.oauth.exchange'))
    kept = json.dumps([[dict(r.headers) for r in (denied, failed)], caplog.text, activity(store), notices, shown, list(store.db['mcp_auth'].find()),
                       list(store.db['mcp_oauth_states'].find())], default=str)
    for secret in ('s3cr3t', 'c0de', 'script', 'access_denied', denied_state, failed_state):
        assert secret not in kept


def test_a_callback_without_a_session_answers_401_and_never_moves_its_query(store):
    # given
    fake = FakeAuth()
    closed = dataclasses.replace(settings(), public_read=False)
    with app(store, fake, s=closed) as c:
        add(c)
        code, state = fake.authorize(start(c))
        c.cookies.clear()

        # when
        page = c.get(CALLBACK, params={'code': code, 'state': state}, headers={'accept': 'text/html'}, follow_redirects=False)
        api = c.get(CALLBACK, params={'code': code, 'state': state}, follow_redirects=False)

    # then
    assert (page.status_code, api.status_code) == (401, 401)
    assert 'location' not in page.headers and code not in page.text and state not in page.text
    assert fake.exchanges == 0 and store.db['mcp_oauth_states'].count_documents({}) == 1


def test_the_callback_needs_the_admin_who_started(store):
    # given
    fake = FakeAuth()
    with app(store, fake) as c:
        add(c)
        code, state = fake.authorize(start(c))
        login(c, store, BOB)

        # when
        other = c.get(CALLBACK, params={'code': code, 'state': state}, follow_redirects=False)
        login(c, store, ALICE)
        again = c.get(CALLBACK, params={'code': code, 'state': state}, follow_redirects=False)
        shown = view(c)

    # then
    assert (other.status_code, other.headers['location']) == (303, LANDING)
    assert again.headers['location'] == f'{BASE}/#t=integrations/signin-expired'
    assert (shown['auth']['state'], shown['auth']['last']['result']) == ('none', 'failed')
    assert fake.exchanges == 0 and store.db['mcp_oauth_states'].count_documents({}) == 0


def test_an_expired_state_is_refused_before_the_ttl_monitor_runs(store):
    # given
    now = [1_000_000.0]
    fake = FakeAuth()
    auth, _, _ = direct(store, fake, now)
    code, state = fake.authorize(auth.start('main', OAUTH, False, ALICE))
    now[0] += STATE_TTL_S + 1

    # when
    result = auth.finish(code, state, None, ALICE)

    # then
    assert result == ('main', 'expired') and fake.exchanges == 0 and auth.state('main') == 'none'
    assert store.db['mcp_oauth_states'].count_documents({}) == 0 and auth.view('main')['last']['result'] == 'expired'


def test_at_most_five_states_wait_per_connection(store):
    # given
    now = [1_000_000.0]
    fake = FakeAuth()
    auth, _, _ = direct(store, fake, now)
    started = []
    for _ in range(STATES_PER_INSTANCE + 2):
        started.append(fake.authorize(auth.start('main', OAUTH, False, ALICE)))
        now[0] += 1

    # when
    oldest = auth.finish(*started[0], None, ALICE)
    newest = auth.finish(*started[-1], None, ALICE)

    # then
    assert store.db['mcp_oauth_states'].count_documents({}) == STATES_PER_INSTANCE - 1
    assert (oldest, newest) == ((None, 'expired'), ('main', 'connected'))


def test_tokens_refresh_before_expiry_and_rotation_is_atomic(store):
    # given
    now = [1_000_000.0]
    fake = FakeAuth(hang_s=0.2)
    auth, _, _ = direct(store, fake, now)
    linked(auth, fake)
    now[0] += 3600 - 200
    gate = threading.Barrier(2)
    got = []

    def call() -> None:
        gate.wait()
        got.append(auth.token('main', OAUTH))

    threads = [threading.Thread(target=call) for _ in range(2)]

    # when
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    # then
    assert fake.refreshes == 1 and got == ['access-2-s3cr3t', 'access-2-s3cr3t']
    assert opened(store)['refresh_token'] == fake.refresh_token == 'refresh-2-s3cr3t' and auth_doc(store)['rev'] == 2
    assert fake.token_calls[-1]['form'] == {'grant_type': 'refresh_token', 'refresh_token': 'refresh-1-s3cr3t', 'client_id': 'client-1', 'resource': URL}
    assert set(auth.secrets('main')) == {'access-2-s3cr3t', 'refresh-2-s3cr3t'}


def test_a_refresh_that_loses_the_write_uses_the_winners_tokens(store):
    # given
    now = [1_000_000.0]
    fake = FakeAuth()
    auth, _, _ = direct(store, fake, now)
    linked(auth, fake)
    now[0] += 3600 - 200
    winner = {'access_token': 'winner-access', 'refresh_token': 'winner-refresh', 'expires_at': now[0] + 3600, 'scope': 'read', 'token_type': 'Bearer'}

    class Racing(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = Capped(httpx.ASGITransport(app=fake.app))

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == '/token':
                store.db['mcp_auth'].update_one({'_id': 'main'}, {'$set': {'tokens': SEALER.seal(json.dumps(winner)), 'expiresAt': '2099-01-01T00:00:00Z'}, '$inc': {'rev': 1}})
            return await self.inner.handle_async_request(request)

    auth.http = lambda ca: (lambda: httpx.AsyncClient(transport=Racing()))

    # when
    got = auth.token('main', OAUTH)

    # then
    assert got == 'winner-access' and fake.refreshes == 1
    assert opened(store)['access_token'] == 'winner-access' and auth_doc(store)['rev'] == 2


def test_invalid_grant_parks_the_connection(store):
    # given
    now = [time.time()]
    fake = FakeAuth()
    with app(store, fake, now=now) as c:
        add(c)
        connect(c, fake)
        fake.refresh_error = 'invalid_grant'
        now[0] += 3600 - 200
        tokens = c.app.state.mcptools.tokens

        # when
        with pytest.raises(Reconnect):
            tokens.token('main', OAUTH)
        with pytest.raises(Reconnect):
            tokens.token('main', OAUTH)
        switched_on(c, store)
        notices = c.get('/api/admin/notices').json()['items']
        test = checked(c)
        shown = view(c)

    # then
    doc = auth_doc(store)
    assert 'tokens' not in doc and 'expiresAt' not in doc and doc['state'] == 'reconnect' and tokens.state('main') == 'reconnect'
    assert {'id': 'mcp-reconnect:main', 'level': 'warn', 'text': msg('mcptools.oauth.reconnect', instance='main'), 'at': doc['last']['at']} in notices
    assert (shown['auth']['state'], shown['auth']['last']['text']) == ('reconnect', msg('mcptools.oauth.reconnect', instance='main'))
    assert activity(store).count(msg('mcptools.oauth.lost', instance='main')) == 1
    assert fake.refreshes == 1 and (test['ok'], test['summary']) == (False, msg('mcptools.oauth.connect_first'))
    assert 's3cr3t' not in json.dumps([notices, shown, activity(store)])


def test_a_transient_refresh_failure_is_retried_later_not_parked(store):
    # given
    now = [1_000_000.0]
    fake = FakeAuth()
    auth, _, _ = direct(store, fake, now)
    linked(auth, fake)
    fake.token_status = 500
    now[0] += 3600 - 200

    # when
    first = auth.token('main', OAUTH)
    now[0] += 30
    kept = auth.token('main', OAUTH)
    fake.token_status = 200
    now[0] += 31
    fresh = auth.token('main', OAUTH)

    # then
    assert first == 'access-1-s3cr3t', 'the caller whose refresh failed for now keeps its valid token (T40 N4)'
    assert (kept, fresh, fake.refreshes, auth.state('main')) == ('access-1-s3cr3t', 'access-2-s3cr3t', 2, 'connected')


def test_an_expired_token_waits_out_the_retry_without_calling_again(store):
    # given
    now = [1_000_000.0]
    fake = FakeAuth(expires_in=30)
    auth, _, _ = direct(store, fake, now)
    linked(auth, fake)
    fake.token_status = 503

    # when
    first = auth.token('main', OAUTH)
    now[0] += 40
    with pytest.raises(McpFailure) as waiting:
        auth.token('main', OAUTH)

    # then
    assert first == 'access-1-s3cr3t' and waiting.value.text == msg('mcptools.oauth.refresh_failed') and fake.refreshes == 1 and auth.state('main') == 'connected'


def test_an_expired_token_without_a_refresh_token_asks_for_a_new_sign_in(store):
    # given
    now = [time.time()]
    fake = FakeAuth(refresh=False)
    with app(store, fake, now=now) as c:
        add(c)
        connect(c, fake)
        tokens = c.app.state.mcptools.tokens
        now[0] += 3600 - 200
        early = tokens.token('main', OAUTH)
        now[0] += 201

        # when
        with pytest.raises(Reconnect):
            tokens.token('main', OAUTH)
        switched_on(c, store)
        notices = [n['id'] for n in c.get('/api/admin/notices').json()['items']]

    # then
    assert early == 'access-1-s3cr3t' and fake.refreshes == 0
    assert tokens.state('main') == 'reconnect' and 'tokens' not in auth_doc(store) and 'mcp-reconnect:main' in notices
    assert msg('mcptools.oauth.expired', instance='main') in activity(store)


def test_oauth_errors_never_carry_token_text(store, caplog):
    # given
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.WARNING, logger='httpx')
    leaked = json.dumps({'error': 'invalid_request', 'access_token': 'b0dy-s3cr3t'}).encode()
    page = b'<html>b0dy-s3cr3t</html>'
    now = [time.time()]
    fake = FakeAuth()
    with app(store, fake, now=now) as c:
        add(c)
        tokens = c.app.state.mcptools.tokens

        # when
        fake.token_status, fake.token_body = 400, leaked
        refused = connect(c, fake)
        refused_last = view(c)['auth']['last']
        fake.token_status, fake.token_body = 200, page
        html = connect(c, fake)
        html_last = view(c)['auth']['last']
        fake.token_status, fake.token_body = 200, None
        connect(c, fake)
        now[0] += 3600 - 200
        fake.token_status, fake.token_body = 200, page
        later = tokens.token('main', OAUTH)
        now[0] += 61
        fake.token_status, fake.token_body = 400, leaked
        with pytest.raises(Reconnect):
            tokens.token('main', OAUTH)
        shown = view(c)
        notices = c.get('/api/admin/notices').json()

    # then
    assert refused.headers['location'] == html.headers['location'] == LANDING
    assert refused_last['result'] == html_last['result'] == 'failed' and later.startswith('access-')
    assert shown['auth']['state'] == 'reconnect'
    kept = json.dumps([refused_last, html_last, caplog.text, activity(store), notices, shown, list(store.db['mcp_auth'].find())], default=str)
    assert 's3cr3t' not in kept


def test_a_401_from_the_server_refreshes_once_then_retries_once(store):
    # given
    fake = FakeAuth()
    with app(store, fake) as c:
        add(c)
        connect(c, fake)
        fake.revoke()
        base = fake.rejected

        # when
        healed = checked(c)
        healed_counts = (fake.refreshes, fake.rejected - base)
        issue = fake.issue

        def revoked() -> dict:
            found = issue()
            fake.revoke()
            return found

        fake.issue = revoked
        fake.revoke()
        base = fake.rejected
        stuck = checked(c)
        stuck_counts = (fake.refreshes, fake.rejected - base)

    # then
    assert (healed['ok'], healed['summary']) == (True, 'fake \u00b7 1 tools') and healed_counts == (1, 1)
    assert (stuck['ok'], stuck['summary']) == (False, msg('connection.rejected', status=401)) and stuck_counts == (2, 2)


def test_the_token_goes_only_to_its_own_resource(store):
    # given
    fake = FakeAuth()
    with app(store, fake) as c:
        add(c)
        connect(c, fake)

        # when
        moved = checked(c, {'url': 'https://other.example.com/mcp'})
        kept = checked(c)

    # then
    assert (moved['ok'], moved['summary']) == (False, msg('mcptools.oauth.connect_first'))
    assert kept['ok'] is True and auth_doc(store)['state'] == 'connected'
    assert not [s for s in fake.seen if s[1] == 'other.example.com']


def test_a_slow_refresh_keeps_the_test_inside_its_budget(store, monkeypatch):
    # given
    monkeypatch.setattr(routes, 'TEST_DEADLINE_S', 1.0)
    now = [time.time()]
    fake = FakeAuth()
    with app(store, fake, now=now) as c:
        add(c)
        connect(c, fake)
        fake.hang_s = 5.0
        now[0] += 3600 - 200
        began = time.monotonic()

        # when
        slow = checked(c)
        took = time.monotonic() - began
        after = checked(c)

    # then
    assert (slow['ok'], slow['summary']) == (False, msg('connection.timeout')) and took < 1.0 + 2 + 1.5, 'the old token is kept, and the listing then runs out of the budget'
    assert after['ok'] is True and fake.refreshes == 1


def test_a_new_connect_voids_the_confirmation(store):
    # given
    fake = FakeAuth()
    with app(store, fake) as c:
        add(c)
        connect(c, fake)
        listed = c.post('/api/admin/mcp-tools/instances/main/tools/refresh')
        confirmed = c.post('/api/admin/mcp-tools/instances/main/confirm', json={'understood': True, 'version': warnings.WARNINGS_VERSION})
        before = view(c)['confirm']['current']

        # when
        connect(c, fake)
        after = view(c)['confirm']['current']

    # then
    assert (listed.status_code, confirmed.status_code, before, after) == (200, 200, True, False)


def test_disconnect_drops_the_tokens_and_voids_the_confirmation(store):
    # given
    fake = FakeAuth()
    with app(store, fake) as c:
        add(c)
        connect(c, fake)
        account = store.db['mcp_tools'].find_one({'_id': 'main'})['account']
        code, state = fake.authorize(start(c))

        # when
        gone = c.delete('/api/admin/mcp-tools/instances/main/oauth')
        late = c.get(CALLBACK, params={'code': code, 'state': state}, follow_redirects=False)
        missing = c.delete('/api/admin/mcp-tools/instances/docs/oauth')
        shown = view(c)

    # then
    assert (gone.status_code, gone.content, missing.status_code) == (204, b'', 404)
    assert late.headers['location'] == f'{BASE}/#t=integrations/signin-expired' and fake.exchanges == 1
    assert store.db['mcp_auth'].count_documents({}) == 0 and store.db['mcp_oauth_states'].count_documents({}) == 0
    assert shown['auth']['state'] == 'none' and store.db['mcp_tools'].find_one({'_id': 'main'})['account'] != account
    assert msg('mcptools.oauth.disconnected', by=ALICE, instance='main') in activity(store)


def test_the_drive_preset_asks_for_offline_access_and_consent(store):
    # given
    fake = FakeAuth(registration=False)
    drive = {'url': DRIVE.url, 'auth': 'oauth', 'preset': DRIVE.id, 'oauth-client-id': 'drive-client', 'oauth-client-secret': 'drive-cl1ent-s3cr3t', 'oauth-scopes': DRIVE.scopes}
    with app(store, fake) as c:
        add(c, values=drive)

        # when
        found = query(start(c))

    # then
    assert (found['access_type'], found['prompt'], found['scope'], found['resource']) == ('offline', 'consent', DRIVE.scopes, DRIVE.url)
    assert DRIVE.auth_params == (('access_type', 'offline'), ('prompt', 'consent'))
    assert all(p.auth_params == () for p in presets.load() if p.id != DRIVE.id)


def test_auth_params_cannot_override_the_flow():
    # given
    data = json.loads(presets.FILE.read_text(encoding='utf-8'))
    drive = next(i for i in data if i['id'] == DRIVE.id)
    bad = [{**drive, 'authParams': {name: 'x'}} for name in ('state', 'redirect_uri', 'client_id', 'code_challenge', 'code_challenge_method', 'resource', 'response_type', 'scope')]
    bad += [{**drive, 'authParams': ['access_type']}, {**drive, 'authParams': {'access_type': 1}}]

    # then
    for item in bad:
        with pytest.raises(presets.PresetError):
            presets.parse([i if i is not drive else item for i in data])


def test_sealed_values_open_with_the_stored_key_after_the_key_changes(store):
    # given
    now = [1_000_000.0]
    fake = FakeAuth()
    store.db['meta'].update_one({'_id': 'secret-key'}, {'$set': {'key': 'stored-key'}}, upsert=True)
    before = McpOAuth(store.db, lambda: Sealer('stored-key'), lambda: BASE, Catalog(store.db), http=lambda ca: fake.client, clock=lambda: now[0])
    linked(before, fake)
    after = McpOAuth(store.db, lambda: Sealer('configured-key'), lambda: BASE, Catalog(store.db), http=lambda ca: fake.client, clock=lambda: now[0])

    # when
    got = after.token('main', OAUTH)
    kept = after.secrets('main')

    # then
    assert got == 'access-1-s3cr3t' and set(kept) == {'access-1-s3cr3t', 'refresh-1-s3cr3t'}


def test_a_refresh_for_an_old_address_never_writes_into_a_new_connection(store):
    # given
    now = [1_000_000.0]
    fake = FakeAuth()
    auth, _, _ = direct(store, fake, now)
    linked(auth, fake)
    now[0] += 3600 - 200
    moved = {'access_token': 'new-access', 'refresh_token': 'new-refresh', 'expires_at': None, 'scope': 'read', 'token_type': 'Bearer'}

    class Moving(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = Capped(httpx.ASGITransport(app=fake.app))

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == '/token':
                store.db['mcp_auth'].replace_one({'_id': 'main'}, {'_id': 'main', 'state': 'connected', 'rev': 1, 'resource': 'https://other.example.com/mcp',
                                                                  'tokens': SEALER.seal(json.dumps(moved))})
            return await self.inner.handle_async_request(request)

    auth.http = lambda ca: (lambda: httpx.AsyncClient(transport=Moving()))

    # when
    with pytest.raises(Reconnect):
        auth.token('main', OAUTH)

    # then
    doc = auth_doc(store)
    assert fake.refreshes == 1 and (doc['state'], doc['rev']) == ('connected', 1) and opened(store)['access_token'] == 'new-access'


def racing(fake: FakeAuth, before) -> Callable:
    class Racing(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = Capped(httpx.ASGITransport(app=fake.app))

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == '/token':
                before()
            return await self.inner.handle_async_request(request)

    return lambda ca: (lambda: httpx.AsyncClient(transport=Racing()))


def test_callers_queued_behind_a_failed_refresh_keep_the_old_token(store):
    # given
    now = [1_000_000.0]
    fake = FakeAuth()
    auth, _, _ = direct(store, fake, now)
    linked(auth, fake)
    fake.token_status, fake.hang_s = 500, 0.3
    now[0] += 3600 - 200
    gate = threading.Barrier(3)
    got = []

    def call() -> None:
        gate.wait()
        try:
            got.append(auth.token('main', OAUTH))
        except McpFailure as e:
            got.append(e.text)

    threads = [threading.Thread(target=call) for _ in range(3)]

    # when
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    # then
    assert fake.refreshes == 1 and auth.state('main') == 'connected'
    assert got == ['access-1-s3cr3t'] * 3, 'the caller that ran the failed refresh keeps the valid token too (T40 N4)'


@pytest.mark.parametrize('status, wait_header, wait', [(429, '120', 120), (429, '999999', 3600), (429, 'soon', 60), (429, '\u00b2', 60), (429, '12abc', 60),
                                                       (408, None, 60), (425, None, 60)],
                         ids=['429-retry-after', '429-capped', '429-unreadable', '429-non-ascii-digit', '429-junk', '408', '425'])
def test_a_rate_limited_refresh_is_temporary(store, status, wait_header, wait):
    # given
    now = [1_000_000.0]
    fake = FakeAuth()
    auth, _, notes = direct(store, fake, now)
    linked(auth, fake)
    fake.token_status, fake.token_headers = status, ({'retry-after': wait_header} if wait_header else {})
    now[0] += 3600 - 200

    # when
    limited = auth.token('main', OAUTH)

    # then
    assert limited == 'access-1-s3cr3t' and auth.state('main') == 'connected'
    assert auth_doc(store)['retryAt'] == iso(now[0] + wait) and not [t for _, t in notes if 'needs a new sign-in' in t]


def test_a_retry_after_holds_the_old_token_until_it_passes(store):
    # given
    now = [1_000_000.0]
    fake = FakeAuth(token_headers={'retry-after': '120'})
    auth, _, _ = direct(store, fake, now)
    linked(auth, fake)
    fake.token_status = 429
    now[0] += 3600 - 200

    # when
    first = auth.token('main', OAUTH)
    now[0] += 119
    kept = auth.token('main', OAUTH)
    fake.token_status = 200
    now[0] += 2
    fresh = auth.token('main', OAUTH)

    # then
    assert first == 'access-1-s3cr3t'
    assert (kept, fresh, fake.refreshes, auth.state('main')) == ('access-1-s3cr3t', 'access-2-s3cr3t', 2, 'connected')


@pytest.mark.parametrize('status, error', [(400, 'invalid_grant'), (401, None), (403, None)], ids=['400-invalid-grant', '401', '403'])
def test_other_refusals_of_a_refresh_still_park(store, status, error):
    # given
    now = [1_000_000.0]
    fake = FakeAuth()
    auth, _, notes = direct(store, fake, now)
    linked(auth, fake)
    if error:
        fake.refresh_error = error
    else:
        fake.token_status = status
    now[0] += 3600 - 200

    # when
    with pytest.raises(Reconnect):
        auth.token('main', OAUTH)

    # then
    assert auth.state('main') == 'reconnect' and (SYSTEM, msg('mcptools.oauth.lost', instance='main')) in notes


def test_park_is_on_the_token_source_and_marks_reconnect_with_a_fixed_reason(store):
    # given
    now = [1_000_000.0]
    fake = FakeAuth()
    auth, _, notes = direct(store, fake, now)
    linked(auth, fake)
    recorded, stub = FakeTokens(), NoTokens()

    # when
    auth.park('main', 'moved')
    auth.park('main', 'moved')
    stub.park('main', 'moved')
    recorded.park('main', 'moved')
    with pytest.raises(ValueError):
        auth.park('main', 'because the server said so')

    # then
    methods = ('state', 'token', 'after_unauthorized', 'forget', 'secrets', 'park')
    assert all(m in TokenSource.__dict__ and callable(getattr(source, m)) for m in methods for source in (auth, stub, recorded))
    assert auth.state('main') == 'reconnect' and auth.view('main')['last']['text'] == msg('mcptools.oauth.reconnect', instance='main')
    assert [t for _, t in notes].count(msg('mcptools.oauth.moved', instance='main')) == 1 and 'tokens' not in auth_doc(store)
    assert recorded.parked == [('main', 'moved')] and stub.state('main') == 'none'
    with pytest.raises(Reconnect):
        auth.token('main', OAUTH)


@pytest.mark.parametrize('race', ['disconnect', 'new-address'])
def test_a_callback_racing_a_disconnect_or_a_new_address_writes_nothing(store, race):
    # given
    now = [1_000_000.0]
    fake = FakeAuth()
    auth, catalog, notes = direct(store, fake, now)
    catalog.reset('main')
    account = catalog.get('main')['account']
    code, state = fake.authorize(auth.start('main', OAUTH, False, ALICE))

    def before() -> None:
        auth.forget('main')
        if race == 'new-address':
            store.db['mcp_auth'].insert_one({'_id': 'main', 'epoch': 'another', 'rev': 0, 'resource': 'https://other.example.com/mcp'})

    auth.http = racing(fake, before)

    # when
    result = auth.finish(code, state, None, ALICE)

    # then
    doc = auth_doc(store)
    assert result == ('main', 'failed') and fake.exchanges == 1 and 'tokens' not in doc and auth.state('main') == 'none'
    assert catalog.get('main')['account'] == account and not [t for _, t in notes if t == msg('mcptools.oauth.connected', by=ALICE, instance='main')]


def test_a_registered_client_refreshes_with_its_current_secret_and_a_new_id_asks_for_a_sign_in(store):
    # given
    now = [1_000_000.0]
    fake = FakeAuth(registration=False)
    auth, _, notes = direct(store, fake, now)
    values = {**OAUTH, 'oauth-client-id': 'shop-client', 'oauth-client-secret': 'old-cl1ent-s3cr3t'}
    linked(auth, fake, values)
    rotated = {**values, 'oauth-client-secret': 'new-cl1ent-s3cr3t'}
    now[0] += 3600 - 200

    # when
    auth.token('main', rotated)
    sent = fake.token_calls[-1]['headers']['authorization']
    now[0] += 3600 - 200
    with pytest.raises(Reconnect):
        auth.token('main', {**rotated, 'oauth-client-id': 'other-client'})

    # then
    assert base64.b64decode(sent.split()[1]).decode() == 'shop-client:new-cl1ent-s3cr3t'
    assert fake.refreshes == 1 and auth.state('main') == 'reconnect'
    assert (SYSTEM, msg('mcptools.oauth.client_changed', instance='main')) in notes


def test_an_abandoned_reconnect_keeps_the_live_client_and_endpoints(store):
    # given
    now = [1_000_000.0]
    fake = FakeAuth()
    auth, _, _ = direct(store, fake, now)
    linked(auth, fake)
    live = auth_doc(store)
    fake.metadata = {'token_endpoint': f'{AUTH_ORIGIN}/token?v=2'}

    # when
    abandoned = auth.start('main', OAUTH, False, ALICE)
    kept = auth_doc(store)
    now[0] += 3600 - 200
    auth.token('main', OAUTH)
    linked(auth, fake)

    # then
    shared = ('tokenEndpoint', 'authorizationEndpoint', 'issuer', 'client', 'clientKind', 'audience', 'resource', 'redirectUri')
    assert abandoned and {k: kept.get(k) for k in shared} == {k: live.get(k) for k in shared}
    assert fake.token_calls[-2]['query'] == '' and fake.token_calls[-1]['query'] == 'v=2' and auth_doc(store)['tokenEndpoint'] == f'{AUTH_ORIGIN}/token?v=2'


def test_unreadable_sealed_tokens_park_the_connection(store):
    # given
    now = [1_000_000.0]
    fake = FakeAuth()
    store.db['meta'].update_one({'_id': 'secret-key'}, {'$set': {'key': 'stored-key'}}, upsert=True)
    first, _, _ = direct(store, fake, now)
    linked(first, fake)
    other, _, notes = direct(store, fake, now, sealer=Sealer('third-key'))

    # when
    with pytest.raises(Reconnect):
        other.token('main', OAUTH)
    with pytest.raises(Reconnect):
        other.token('main', OAUTH)

    # then
    assert other.state('main') == 'reconnect' and fake.refreshes == 0
    assert notes == [(SYSTEM, msg('mcptools.oauth.unreadable', instance='main'))]


def test_the_sign_in_routes_use_their_own_handle_and_the_auth_view_reads_once(store, monkeypatch):
    # given
    fake = FakeAuth()
    reads = []
    with app(store, fake) as c:
        add(c)
        connect(c, fake)
        c.app.state.mcptools.oauth.park('main', 'moved')
        switched_on(c, store)
        original = McpOAuth._doc
        monkeypatch.setattr(McpOAuth, '_doc', lambda self, name: reads.append(name) or original(self, name))

        # when
        notices = c.get('/api/admin/notices').json()['items']
        noticed = len(reads)
        reads.clear()
        shown = view(c)
        viewed = len(reads)
        c.app.state.mcptools.tokens = FakeTokens(state='reconnect')
        started = c.post(START)

    # then
    assert (noticed, viewed) == (1, 1) and 'mcp-reconnect:main' in [n['id'] for n in notices] and shown['auth']['state'] == 'reconnect'
    assert started.status_code == 200 and started.json()['url'].startswith(f'{AUTH_ORIGIN}/authorize?')


def test_start_has_one_total_budget(store, monkeypatch):
    # given
    monkeypatch.setattr(oauth_module, 'START_DEADLINE_S', 0.5)
    fake = FakeAuth(prm=False, slow_s=0.4)
    with app(store, fake) as c:
        add(c)
        began = time.monotonic()

        # when
        res = c.post(START)
        took = time.monotonic() - began

    # then
    assert (res.status_code, res.json()) == (502, {'detail': msg('connection.timeout')}) and took < 0.5 + 1.5, 'the text tells one budget from the old discovery path; the time only bounds it'


SIGN_IN = msg('mcptools.credential.oauth')


@pytest.mark.parametrize('scope, permission', [('files.read', msg('mcptools.access.scope', scope='files.read')), ('', msg('mcptools.access.unknown'))], ids=['named', 'unnamed'])
def test_a_missing_scope_fails_the_test_naming_the_scope(store, scope, permission):
    # given
    fake = FakeAuth()
    with app(store, fake) as c:
        add(c)
        connect(c, fake)
        fake.missing_scope = scope

        # when
        test = checked(c)
        listed = c.post('/api/admin/mcp-tools/instances/main/tools/refresh')
        tools = view(c)['tools']

    # then
    expected = msg('mcptools.access.lacks', credential=SIGN_IN, permission=permission)
    assert (test['ok'], test['summary']) == (False, expected)
    assert (listed.status_code, listed.json()['detail'], tools) == (502, expected, [])


def test_a_scope_that_echoes_the_token_is_scrubbed(store):
    # given
    fake = FakeAuth()
    with app(store, fake) as c:
        add(c)
        connect(c, fake)
        fake.missing_scope = fake.access

        # when
        test = checked(c)

    # then
    assert test['ok'] is False and 's3cr3t' not in json.dumps(test) and test['summary'] == msg('mcptools.access.lacks', credential=SIGN_IN, permission=msg('mcptools.access.scope', scope='***'))


def test_a_token_endpoint_missing_scope_names_it_in_the_test(store):
    # given
    now = [time.time()]
    fake = FakeAuth()
    with app(store, fake, now=now) as c:
        add(c)
        connect(c, fake)
        fake.token_status, fake.token_headers = 403, {'www-authenticate': 'Bearer error="insufficient_scope", scope="offline_access"'}
        now[0] += 3600 - 200

        # when
        test = checked(c)

    # then
    assert (test['ok'], test['summary']) == (False, msg('mcptools.access.lacks', credential=SIGN_IN, permission=msg('mcptools.access.scope', scope='offline_access')))
    assert auth_doc(store)['state'] == 'reconnect'


@pytest.mark.parametrize('outcome', ['succeeds', 'refused', 'temporary'])
def test_a_late_refresh_never_touches_a_same_address_reconnect(store, outcome):
    # given
    now = [1_000_000.0]
    fake = FakeAuth()
    auth, _, notes = direct(store, fake, now)
    linked(auth, fake)
    now[0] += 3600 - 200
    fresh = {'access_token': 'new-access', 'refresh_token': 'new-refresh', 'expires_at': None, 'scope': 'read', 'token_type': 'Bearer'}
    if outcome == 'refused':
        fake.refresh_error = 'invalid_grant'
    if outcome == 'temporary':
        fake.token_status = 500

    def before() -> None:
        auth.forget('main')
        store.db['mcp_auth'].insert_one({'_id': 'main', 'epoch': 'fresh', 'rev': 1, 'state': 'connected', 'resource': URL, 'tokens': SEALER.seal(json.dumps(fresh)),
                                         'expiresAt': '2099-01-01T00:00:00Z'})

    auth.http = racing(fake, before)

    # when
    try:
        got = auth.token('main', OAUTH)
    except (McpFailure, Reconnect):
        got = None

    # then
    doc = auth_doc(store)
    assert fake.refreshes == 1 and (doc['epoch'], doc['rev'], doc['state']) == ('fresh', 1, 'connected') and 'retryAt' not in doc
    assert opened(store)['access_token'] == 'new-access' and got in (None, 'new-access')
    assert not [t for _, t in notes if 'needs a new sign-in' in t]


def test_a_transient_refresh_failure_keeps_the_first_caller_on_its_valid_token(store):
    # given
    now = [1_000_000.0]
    fake = FakeAuth()
    auth, _, _ = direct(store, fake, now)
    linked(auth, fake)
    fake.token_status = 503
    now[0] += 3600 - 200

    # when
    first = auth.token('main', OAUTH)
    with pytest.raises(McpFailure) as forced:
        auth.after_unauthorized('main', OAUTH)

    # then
    assert first == 'access-1-s3cr3t' and fake.refreshes == 1 and forced.value.text == msg('mcptools.oauth.refresh_failed')


def test_a_retry_date_in_the_minus_zero_zone_is_read_as_utc(monkeypatch):
    # given
    import email.utils
    import os
    before = os.environ.get('TZ')
    os.environ['TZ'] = 'Asia/Tokyo'
    time.tzset()
    now = 1_000_000_000.0
    stamp = email.utils.formatdate(now + 600, usegmt=True)
    request = httpx.Request('POST', 'https://auth.example.com/token')

    # when
    try:
        gmt = oauth_module.retry_after(httpx.Response(429, request=request, headers={'retry-after': stamp}), now)
        minus = oauth_module.retry_after(httpx.Response(429, request=request, headers={'retry-after': stamp.replace('GMT', '-0000')}), now)
    finally:
        if before is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = before
        time.tzset()

    # then
    assert gmt == minus == 600.0


def test_a_same_rev_reconnect_before_the_lock_is_not_refreshed_again(store):
    # given
    now = [1_000_000.0]
    fake = FakeAuth()
    auth, _, _ = direct(store, fake, now)
    linked(auth, fake)
    now[0] += 3600 - 200
    real = auth.col.find_one({'_id': 'main'})
    store.db['mcp_auth'].update_one({'_id': 'main'}, {'$set': {'epoch': 'fresh-epoch', 'expiresAt': iso(now[0] + 3600)}})
    reads = []
    original = McpOAuth._doc

    def first_old(self, name):
        reads.append(name)
        return dict(real) if len(reads) == 1 else original(self, name)

    auth._doc = first_old.__get__(auth)

    # when
    got = auth.token('main', OAUTH)

    # then
    assert got == 'access-1-s3cr3t' and fake.refreshes == 0


def test_a_late_callback_of_an_old_flow_leaves_the_fresh_connection_text(store):
    # given
    now = [1_000_000.0]
    fake = FakeAuth()
    auth, _, _ = direct(store, fake, now)
    linked(auth, fake)
    _, state = fake.authorize(auth.start('main', OAUTH, False, ALICE))
    store.db['mcp_oauth_states'].update_many({'instance': 'main'}, {'$set': {'epoch': 'old-epoch'}})

    # when
    result = auth.finish(None, state, 'access_denied', ALICE)

    # then
    assert result == ('main', 'denied') and auth.view('main')['last']['result'] == 'connected'


def test_a_refresh_claimed_by_another_pod_is_waited_for_not_sent_again(store):
    # given
    now = [1_000_000.0]
    fake = FakeAuth()
    auth, _, _ = direct(store, fake, now)
    linked(auth, fake)
    now[0] += 3600 - 200
    store.db['mcp_auth'].update_one({'_id': 'main'}, {'$set': {'refreshing': now[0] + 15}})

    def winner():
        time.sleep(0.3)
        store.db['mcp_auth'].update_one({'_id': 'main'}, {'$inc': {'rev': 1}, '$set': {'expiresAt': iso(now[0] + 3600)}, '$unset': {'refreshing': ''}})

    peer = threading.Thread(target=winner)
    peer.start()

    # when
    got = auth.token('main', OAUTH)
    peer.join()

    # then
    assert got == 'access-1-s3cr3t' and fake.refreshes == 0 and 'refreshing' not in auth_doc(store)


def test_a_sign_in_whose_state_is_gone_lands_on_the_expired_note(store):
    # given
    fake = FakeAuth()
    with app(store, fake) as c:
        add(c)

        # when
        gone = c.get(CALLBACK, params={'code': 'abc', 'state': 'swept-away'}, follow_redirects=False)
        bare = c.get(CALLBACK, follow_redirects=False)

    # then
    assert (gone.status_code, gone.headers['location']) == (303, f'{BASE}/#t=integrations/signin-expired')
    assert bare.headers['location'] == f'{BASE}/#t=integrations/signin-expired'
