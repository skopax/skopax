import asyncio
import json
import logging
import time

import pytest
from fastapi.testclient import TestClient

from skopax import mcpserver
from skopax.apitokens import ApiTokens
from skopax.app import create_app
from skopax.auth import bearer_token
from skopax.config import load_settings
from skopax import mcpauth
from skopax.features import off_text
from skopax.mcptools.bridge import asker_role
from skopax.messages import msg
from skopax.oidc import AuthError
from skopax.sso import InvalidSecret, SsoHttp
from skopax.store import Store
from skopax.tunables import registry
from skopax.users import LINK_PENDING_S, Users
from tests.conftest import AUTH, TOKEN, ready_app
from tests.plugin_kit import registry_with
from tests.plugins import resolver_plugin
from tests.sso_kit import OTHER_KEY, FakeIdp, app_client, idp_settings, log_in
from tests.test_mcp import HEADERS, board, cards

ALICE = 'alice@example.com'
BOB = 'bob@example.com'
CAROL = 'carol@example.com'
DANA = 'dana.reed@example.com'
ERIN = 'erin@example.com'
MCP_ISSUER = 'https://login.example.com'
NINA = 'nina.white@example.com'
PAUL = 'paul.stone@example.com'
SITE = 'https://skopax.example.com'
SETTINGS = '/api/admin/settings'
ROLE_MAP = json.dumps([{'value': 'ops-admins', 'role': 'admin'}, {'value': 'ops-operators', 'role': 'operator'}, {'value': 'ops-viewers', 'role': 'viewer'}])
VIEWER_TOOLS = ['list_cards', 'get_card', 'get_investigation', 'investigator_status', 'list_deploy_reports', 'get_self_review', 'list_my_asks', 'get_ask', 'list_notifications', 'about']
OPERATOR_TOOLS = ['investigate_card', 'ask', 'take_card', 'resolve_card', 'mute_card', 'reopen_card', 'assign_card', 'link_ticket']
MCP_KEYS = ('mcp.client-id', 'mcp.audience', 'mcp.required-role', 'mcp.issuer', 'mcp.scope', 'mcp.resource-url', 'mcp.allowed-clients')


class Clock:
    def __init__(self):
        self.now = float(int(time.time()))

    def __call__(self) -> float:
        return self.now


def mcp_settings(idp: FakeIdp, **over):
    return idp_settings(idp, **({'sso_role_map': ROLE_MAP} | over))


def mint(idp: FakeIdp, email: str | None = DANA, roles=('SKOPAX_MCP',), aud: str = 'skopax-mcp', **claims) -> str:
    return idp.token(aud=aud, **({'sub': email or 'opaque-subject', 'email': email, 'roles': list(roles)} | claims))


def rpc(client: TestClient, method: str, bearer: str | None, params: dict | None = None):
    headers = HEADERS | ({'Authorization': f'Bearer {bearer}'} if bearer else {})
    return client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}, headers=headers)


def call(client: TestClient, bearer: str, name: str, args: dict | None = None) -> tuple[str, bool]:
    res = rpc(client, 'tools/call', bearer, {'name': name, 'arguments': args or {}})
    assert res.status_code == 200, res.text
    result = res.json()['result']
    return result['content'][0]['text'], bool(result.get('isError'))


def listed(res) -> list[str]:
    assert res.status_code == 200, res.text
    return [t['name'] for t in res.json()['result']['tools']]


def challenge(res) -> str:
    return res.headers.get('www-authenticate', '')


def docs(store: Store) -> list[dict]:
    return list(store.db['users'].find().sort('_id', 1))


def operator_doc(users: Users, email: str, name: str) -> None:
    users.upsert_sso(email, name, 'viewer')
    users.col.update_one({'_id': email}, {'$set': {'role': 'operator', 'roleFrom': 'admin', 'adminRole': 'operator'}})


def test_mcp_token_audience_issuer_and_client_checks(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp, mcp_settings(idp, mcp_issuer='https://login.example.com'))
    refused = {'page client': mint(idp, aud='skopax'), 'other app': mint(idp, aud='other-app'), 'other issuer': mint(idp, iss='https://evil.example.com'),
               'other key': mint(idp, key=OTHER_KEY), 'expired 40 s': mint(idp, ttl=-40)}
    accepted = {'resource url': mint(idp, aud=f'{SITE}/mcp'), 'mcp issuer': mint(idp, iss='https://login.example.com'), 'discovered issuer': mint(idp)}

    # when
    with client as c:
        before = docs(store)
        bad = {name: rpc(c, 'tools/list', t) for name, t in refused.items()}
        untouched = docs(store)
        good = {name: rpc(c, 'tools/list', t) for name, t in accepted.items()}
        pinned = c.put(SETTINGS, json={'key': 'mcp.allowed-clients', 'value': 'skopax-mcp'}, headers=AUTH)
        other_client = rpc(c, 'tools/list', mint(idp, email=PAUL, azp='other-app'))
        no_client = rpc(c, 'tools/list', mint(idp, email=PAUL))
        after_pin = docs(store)
        own_client = rpc(c, 'tools/list', mint(idp, email=PAUL, azp='skopax-mcp'))

    # then
    for name, res in {**bad, 'other azp': other_client, 'no azp': no_client}.items():
        assert (res.status_code, res.json()) == (401, {'detail': msg('mcp.token_invalid')}), name
        assert challenge(res).startswith('Bearer error="invalid_token"'), name
    assert untouched == before and DANA not in [d['_id'] for d in before], 'a refused token writes no user'
    assert {name: res.status_code for name, res in good.items()} == {name: 200 for name in accepted}
    assert pinned.status_code == 200
    assert PAUL not in [d['_id'] for d in after_pin] and DANA in [d['_id'] for d in after_pin], 'a token from another client or no client writes no user'
    assert own_client.status_code == 200


def test_mcp_needs_the_required_role_claim_at_the_configured_path(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp, mcp_settings(idp, sso_roles_claim='realm_access.roles'))
    flat = mint(idp, roles=('SKOPAX_MCP',))
    nested = mint(idp, roles=(), realm_access={'roles': ['SKOPAX_MCP']})
    bare = mint(idp, email=PAUL, roles=())

    # when
    with client as c:
        before = docs(store)
        wrong_path = rpc(c, 'tools/list', flat)
        untouched = docs(store)
        right_path = rpc(c, 'tools/list', nested)
        gated = rpc(c, 'tools/list', bare)
        opened = c.put(SETTINGS, json={'key': 'mcp.required-role', 'value': ''}, headers=AUTH)
        ungated = rpc(c, 'tools/list', mint(idp, email=PAUL, roles=(), name='Paul'))

    # then
    assert (wrong_path.status_code, wrong_path.json()) == (403, {'detail': msg('mcp.role_missing', role='SKOPAX_MCP')})
    assert challenge(wrong_path).startswith('Bearer error="insufficient_scope"')
    assert untouched == before
    assert right_path.status_code == 200
    assert (gated.status_code, gated.json()) == (403, {'detail': msg('mcp.role_missing', role='SKOPAX_MCP')}) and opened.status_code == 200
    assert ungated.status_code == 200, 'an empty required role accepts any token'


def test_mcp_follows_disable_and_demotion_within_the_cache_window(store: Store):
    # given
    idp, clock, other_clock = FakeIdp(), Clock(), Clock()
    users = Users(store.db)
    operator_doc(users, NINA, 'Nina')
    board(store, *cards())
    first = app_client(store, idp, mcp_settings(idp), clock=clock)
    second = app_client(store, idp, mcp_settings(idp), clock=other_clock)
    tok = mint(idp, email=NINA)

    # when
    with first as a, second as b:
        took, took_failed = call(a, tok, 'take_card', {'card': 14})
        warm, warm_failed = call(b, tok, 'list_cards', {'filter': 'all'})
        other_clock.now += 19
        log_in(b, idp, sub=NINA, email=NINA, roles=[])
        page_user = b.get('/api/me').json()['user']
        other_clock.now += 1
        disabled = a.patch(f'/api/admin/users/{NINA}', json={'disabled': True}, headers=AUTH)
        here = rpc(a, 'tools/list', tok)
        other_clock.now += 5
        cached = rpc(b, 'tools/list', tok)
        other_clock.now += 6
        expired = rpc(b, 'tools/list', tok)
        a.patch(f'/api/admin/users/{NINA}', json={'disabled': False}, headers=AUTH)
        warm_again, _ = call(a, tok, 'list_cards', {'filter': 'all'})
        demoted = a.patch(f'/api/admin/users/{NINA}', json={'role': 'viewer'}, headers=AUTH)
        queued = len(store.list_actions(None, 50))
        refused, refused_failed = call(a, tok, 'take_card', {'card': 15})
        still, still_failed = call(a, tok, 'list_cards', {'filter': 'all'})
        queued_after = len(store.list_actions(None, 50))

    # then
    assert not took_failed and 'In progress' in took and not warm_failed and '#14' in warm
    assert page_user == NINA, 'the other pod holds the page user cache too'
    assert disabled.status_code == 200
    assert (here.status_code, here.json()) == (403, {'detail': msg('mcp.disabled')}), 'the pod that served the change drops its cache at once'
    assert cached.status_code == 200, 'another pod serves its cache entry up to 30 s'
    assert (expired.status_code, expired.json()) == (403, {'detail': msg('mcp.disabled')}), '30 s from the cache entry, not stacked on the page user cache'
    assert '#14' in warm_again and demoted.status_code == 200
    assert (refused, refused_failed) == (msg('mcp.needs_role', role='operator'), True)
    assert not still_failed and '#14' in still
    assert queued_after == queued, 'a refused tool queues nothing'


def test_viewers_see_and_call_only_read_tools(store: Store):
    # given
    idp = FakeIdp()
    board(store, *cards())
    client = app_client(store, idp, mcp_settings(idp))
    viewer = mint(idp, email=DANA)
    operator = mint(idp, email=PAUL, roles=('SKOPAX_MCP', 'ops-operators'))

    # when
    with client as c:
        seen = listed(rpc(c, 'tools/list', viewer))
        resolved, resolved_failed = call(c, viewer, 'resolve_card', {'card': 14})
        queued = store.list_actions(None, 10)
        everything = listed(rpc(c, 'tools/list', operator))
        done, done_failed = call(c, operator, 'resolve_card', {'card': 14})

    # then
    assert seen == VIEWER_TOOLS
    assert (resolved, resolved_failed) == (msg('mcp.needs_role', role='operator'), True)
    assert queued == []
    assert everything == VIEWER_TOOLS + OPERATOR_TOOLS
    assert not done_failed and 'No action needed' in done


def test_every_mcp_tool_has_a_role_class():
    # given
    roles = mcpserver.TOOL_ROLES
    hinted = {t.name: bool(t.annotations and t.annotations.readOnlyHint) for t in mcpserver.TOOLS}

    # then
    assert sorted(roles) == sorted(t.name for t in mcpserver.TOOLS)
    assert [name for name, read_only in hinted.items() if read_only and roles[name] != 'viewer'] == []
    assert [name for name, read_only in hinted.items() if not read_only and roles[name] != 'operator'] == []
    assert sorted(name for name, role in roles.items() if role == 'viewer') == sorted(VIEWER_TOOLS)


def test_mcp_applies_the_same_takeover_and_email_rules(store: Store):
    # given
    idp = FakeIdp()
    users = Users(store.db)
    users.create(ALICE, 'Alice', 'admin', 'a-long-enough-password')
    users.create(BOB, 'Bob', 'operator', 'a-long-enough-password')
    users.create(CAROL, 'Carol', 'operator', 'a-long-enough-password')
    users.create(ERIN, 'Erin', 'operator', 'a-long-enough-password')
    client = app_client(store, idp, mcp_settings(idp, mcp_issuer=MCP_ISSUER))
    unlinked = mint(idp, email=ALICE, email_verified=True)

    # when
    with client as c:
        before = docs(store)
        local = rpc(c, 'tools/list', unlinked)
        local_after = docs(store)
        users.set_sso_link(ALICE, True, 'admin@example.com')
        users.bind_sso(ALICE, idp.issuer, 'alice-subject', time.time())
        users.set_sso_link(BOB, True, 'admin@example.com')
        users.set_sso_link(CAROL, True, 'admin@example.com')
        users.col.update_one({'_id': CAROL}, {'$set': {'ssoLink.atTs': time.time() - LINK_PENDING_S - 60}})
        users.set_sso_link(ERIN, True, 'admin@example.com')
        users.bind_sso(ERIN, 'https://other-idp.example.com', 'erin-subject', time.time())
        linked_before = docs(store)
        bound = mint(idp, email=ALICE, sub='alice-subject', email_verified=True)
        same_subject = rpc(c, 'tools/list', bound)
        other_subject = rpc(c, 'tools/list', mint(idp, email=ALICE, sub='mallory-subject', email_verified=True))
        pending = rpc(c, 'tools/list', mint(idp, email=BOB, sub='bob-subject', email_verified=True))
        expired = rpc(c, 'tools/list', mint(idp, email=CAROL, sub='carol-subject', email_verified=True))
        alias_issuer = rpc(c, 'tools/list', mint(idp, email=ALICE, sub='alice-subject', iss=MCP_ISSUER, email_verified=True))
        other_issuer = rpc(c, 'tools/list', mint(idp, email=ERIN, sub='erin-subject', email_verified=True))
        linked_after = docs(store)
        unverified = {value: rpc(c, 'tools/list', mint(idp, email=DANA, email_verified=value)) for value in (False, 'false', 'FALSE')}
        no_email = rpc(c, 'tools/list', mint(idp, email=None, preferred_username='dana', sub='opaque-subject'))
        refused_after = docs(store)
        strict = c.put(SETTINGS, json={'key': 'sso.require-verified-email', 'value': 'true'}, headers=AUTH)
        absent = rpc(c, 'tools/list', mint(idp, email=PAUL))
        verified = rpc(c, 'tools/list', mint(idp, email=DANA, email_verified=True))
        unlink = c.patch(f'/api/admin/users/{ALICE}', json={'ssoLink': False}, headers=AUTH)
        after_unlink = rpc(c, 'tools/list', bound)

    # then
    assert (local.status_code, local.json()) == (403, {'detail': msg('mcp.local_account')})
    assert challenge(local).startswith('Bearer error="insufficient_scope"')
    assert local_after == before, 'a token never writes to a local account'
    assert listed(same_subject) == VIEWER_TOOLS + OPERATOR_TOOLS, 'the bound subject runs with the stored local role'
    for name, res in {'other subject': other_subject, 'pending': pending, 'expired': expired, 'alias issuer': alias_issuer, 'bound under another issuer': other_issuer}.items():
        assert (res.status_code, res.json()) == (403, {'detail': msg('mcp.local_account')}), name
    assert linked_after == linked_before, 'MCP never binds a link and never stamps a local account'
    for value, res in unverified.items():
        assert (res.status_code, res.json()) == (403, {'detail': msg('mcp.unverified')}), value
    assert (no_email.status_code, no_email.json()) == (403, {'detail': msg('mcp.no_email')})
    assert refused_after == linked_after
    assert strict.status_code == 200
    assert (absent.status_code, absent.json()) == (403, {'detail': msg('mcp.unverified')}), 'absent is refused once the switch is on'
    assert verified.status_code == 200
    assert unlink.status_code == 200 and (after_unlink.status_code, after_unlink.json()) == (403, {'detail': msg('mcp.local_account')}), 'an unlink drops the cached MCP identity'


def test_a_first_mcp_call_provisions_the_user_but_never_writes_a_role(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp, mcp_settings(idp))

    # when
    with client as c:
        first = rpc(c, 'tools/list', mint(idp, email=PAUL, name='Paul Stone'))
        made = store.db['users'].find_one({'_id': PAUL})
        admin_token = mint(idp, email=PAUL, name='Someone Else', roles=('SKOPAX_MCP', 'ops-admins'))
        later = rpc(c, 'tools/list', admin_token)
        runs_as = c.app.state.mcp_auth.user(admin_token)
        kept = store.db['users'].find_one({'_id': PAUL})

    # then
    assert first.status_code == 200
    assert (made['source'], made['role'], made['roleFrom'], made['name'], made['disabled']) == ('sso', 'viewer', 'default', 'Paul Stone', False)
    assert listed(later) == VIEWER_TOOLS + OPERATOR_TOOLS
    assert (runs_as.email, runs_as.role, runs_as.admin, runs_as.operator) == (PAUL, 'admin', True, True)
    assert {k: v for k, v in kept.items() if k != 'lastLogin'} == {k: v for k, v in made.items() if k != 'lastLogin'}, 'MCP never writes role, roleFrom or name of a known user'


def test_an_mcp_sign_in_stamps_the_last_login_of_a_known_account_and_nothing_else(store: Store):
    # given
    idp = FakeIdp()
    users = Users(store.db)
    users.upsert_sso(PAUL, 'Paul Stone', 'viewer')
    users.col.update_one({'_id': PAUL}, {'$set': {'lastLogin': None}})
    before = store.db['users'].find_one({'_id': PAUL})
    client = app_client(store, idp, mcp_settings(idp))

    # when
    with client as c:
        res = rpc(c, 'tools/list', mint(idp, email=PAUL, name='Someone Else'))
    after = store.db['users'].find_one({'_id': PAUL})

    # then
    assert res.status_code == 200
    assert before['lastLogin'] is None and after['lastLogin']
    assert {k: v for k, v in after.items() if k != 'lastLogin'} == {k: v for k, v in before.items() if k != 'lastLogin'}, 'no role, role source or name write'


def test_api_tokens_do_not_open_mcp(store: Store, monkeypatch):
    # given
    idp = FakeIdp()
    client = app_client(store, idp, mcp_settings(idp))
    resolved = []
    real = ApiTokens.resolve
    monkeypatch.setattr(ApiTokens, 'resolve', lambda self, token, **kw: resolved.append(token) or real(self, token, **kw))

    # when
    with client as c:
        made = c.post('/api/admin/tokens', json={'name': 'ci', 'role': 'admin'}, headers=AUTH).json()['token']
        resolved.clear()
        api = rpc(c, 'tools/list', made)
        bootstrap = rpc(c, 'tools/list', TOKEN)
        at_mcp = list(resolved)
        core = c.get('/api/admin/users', headers={'Authorization': f'Bearer {made}'})

    # then
    assert made.startswith('skopax_')
    for res in (api, bootstrap):
        assert (res.status_code, res.json()) == (401, {'detail': msg('mcp.token_invalid')})
        assert challenge(res).startswith('Bearer error="invalid_token"')
    assert at_mcp == [], 'no api_tokens lookup at /mcp'
    assert core.status_code == 200 and resolved == [made], 'the same token opens the core API'


def test_mcp_is_off_while_sso_is_inactive(store: Store):
    # given
    idp = FakeIdp()
    clock = Clock()
    client = app_client(store, idp, mcp_settings(idp), clock=clock)
    tok = mint(idp, iat=int(clock.now))

    # when
    with client as c:
        off = c.put(SETTINGS, json={'key': 'sso.enabled', 'value': 'false'}, headers=AUTH)
        endpoint_off = rpc(c, 'tools/list', tok)
        meta_off = c.get('/.well-known/oauth-protected-resource')
        suffixed_off = c.get('/.well-known/oauth-protected-resource/mcp')
        info_off = c.get('/api/mcp/info').json()
        on = c.put(SETTINGS, json={'key': 'sso.enabled', 'value': 'true'}, headers=AUTH)
        endpoint_on = rpc(c, 'tools/list', tok)
        meta_on = c.get('/.well-known/oauth-protected-resource')
        feature_off = c.put('/api/admin/features/mcp', json={'on': False}, headers=AUTH)
        switched = [rpc(c, 'tools/list', tok), c.get('/.well-known/oauth-protected-resource'), c.get('/.well-known/oauth-protected-resource/mcp')]
        c.put(SETTINGS, json={'key': 'sso.enabled', 'value': 'false'}, headers=AUTH)
        both_off = c.get('/.well-known/oauth-protected-resource')

    # then
    assert off.status_code == 200 and on.status_code == 200
    for res in (endpoint_off, meta_off, suffixed_off):
        assert (res.status_code, res.json()) == (404, {'detail': msg('mcp.not_set_up')})
    assert info_off['enabled'] is False
    assert (endpoint_on.status_code, meta_on.status_code) == (200, 200)
    assert feature_off.status_code == 200
    for res in switched:
        assert (res.status_code, res.json()) == (409, {'detail': off_text('mcp')})
    assert (both_off.status_code, both_off.json()) == (409, {'detail': off_text('mcp')}), 'the feature switch answers first, like /mcp'


def test_a_failing_resolver_refuses_mcp_with_503(store: Store):
    # given
    resolver_plugin.reset()
    idp = FakeIdp()
    users = Users(store.db)
    operator_doc(users, NINA, 'Nina')
    users.create(ERIN, 'Erin', 'operator', 'a-long-enough-password')
    users.set_sso_link(ERIN, True, 'admin@example.com')
    users.bind_sso(ERIN, idp.issuer, 'erin-subject', time.time())
    client = TestClient(create_app(mcp_settings(idp), store, registry=registry_with(resolver_plugin), sso_http=SsoHttp(fetch=idp.fetch, exchange=idp.exchange)),
                        base_url='https://testserver')
    client.app.state.resolvers.timeout = 0.2
    answers = {ALICE: 'raise', BOB: 'bad', CAROL: 'sleep', NINA: 'raise'}
    resolver_plugin.ANSWERS.update({(email, 'mcp'): answer for email, answer in answers.items()} | {(ERIN, 'mcp'): 'raise'})

    try:
        # when
        with client as c:
            before = docs(store)
            failed = {email: rpc(c, 'tools/list', mint(idp, email=email)) for email in answers}
            after = docs(store)
            local = rpc(c, 'tools/list', mint(idp, email=ERIN, sub='erin-subject', email_verified=True))
            local_asked = [user for user, channel in resolver_plugin.SEEN if user.get('email') == ERIN]
            resolver_plugin.ANSWERS.clear()
            recovered = rpc(c, 'tools/list', mint(idp, email=NINA))
            resolver_plugin.ANSWERS[(DANA, 'mcp')] = 'viewer'
            answered = c.app.state.mcp_auth.user(mint(idp, email=DANA, roles=('SKOPAX_MCP', 'ops-admins')))
    finally:
        resolver_plugin.reset()

    # then
    assert {email: (res.status_code, res.json()) for email, res in failed.items()} == {email: (503, {'detail': msg('mcp.roles_unavailable')}) for email in answers}
    assert after == before, 'no insert and no write while the resolver fails'
    assert local.status_code == 200 and local_asked == [], 'a linked local account is never sent to a resolver, so a failing one cannot lock it out'
    assert recovered.status_code == 200, 'the failure is not cached'
    assert answered.role == 'viewer', 'the resolver comes before the claim'


@pytest.mark.parametrize('header', ['Bearer t', 'bearer t', 'Bearer\tt', 'Bearer ', 'Basic dTpw', '', 'Token t'])
def test_the_mcp_and_core_bearer_parse_alike(header: str):
    # given
    seen = []

    class Auth:
        enabled = True

        def challenge(self, *why):
            return 'Bearer'

        def user(self, token):
            seen.append(token)
            raise AuthError('refused')

    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {'type': 'http.disconnect'}

    scope = {'type': 'http', 'method': 'POST', 'path': '/mcp', 'headers': [(b'authorization', header.encode())]}

    # when
    asyncio.run(mcpserver.Endpoint(Auth(), None)(scope, receive, send))
    core = bearer_token(header)

    # then
    assert seen == ([core] if core else [])
    assert sent[0]['status'] == 401
    assert json.loads(sent[1]['body']) == {'detail': msg('mcp.token_invalid') if core else msg('mcp.login_first')}


def test_the_mcp_settings_are_live_tunables_in_their_own_group_with_validation(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp, mcp_settings(idp))
    known = {t.key: t for t in registry()}
    bad = {'mcp.audience': ('x' * 1001, 'MCP audience is longer than 1000 characters'),
           'mcp.allowed-clients': (','.join(['skopax-mcp'] * 400), 'MCP allowed clients is longer than 4000 characters'),
           'mcp.issuer': ('http://login.example.com', msg('sso.url_scheme', field='MCP issuer')),
           'mcp.resource-url': ('ftp://skopax.example.com', msg('sso.url_scheme', field='Skopax address for MCP (without /mcp)'))}
    loaded = load_settings(env={'MCP_AUDIENCE': 'api://skopax', 'MCP_ALLOWED_CLIENTS': 'skopax-mcp, other-app'})

    # when
    with client as c:
        refused = {key: c.put(SETTINGS, json={'key': key, 'value': value}, headers=AUTH) for key, (value, _) in bad.items()}
        written = list(store.db['tunable_changes'].find())
        audience = c.put(SETTINGS, json={'key': 'mcp.audience', 'value': 'api://skopax'}, headers=AUTH)
        allowed = c.put(SETTINGS, json={'key': 'mcp.allowed-clients', 'value': 'skopax-mcp, other-app'}, headers=AUTH)
        auth = c.app.state.mcp_auth
        audiences = auth.audiences()

    # then
    assert {key: known[key].group for key in MCP_KEYS} == dict.fromkeys(MCP_KEYS, 'MCP')
    assert known['mcp.allowed-clients'].kind == 'list' and all(known[key].live for key in MCP_KEYS)
    assert (known['mcp.dive-cap'].group, known['mcp.wait-seconds'].group) == ('Investigator', 'Investigator')
    assert {key: (res.status_code, res.json()['detail']) for key, res in refused.items()} == {key: (422, text) for key, (_, text) in bad.items()}
    assert written == []
    assert audience.status_code == 200 and allowed.status_code == 200
    assert audiences == ('api://skopax', f'{SITE}/mcp')
    assert (loaded.mcp_audience, loaded.mcp_allowed_clients) == ('api://skopax', ('skopax-mcp', 'other-app'))
    assert not hasattr(loaded, 'mcp_enabled') and not hasattr(loaded, 'mcp_token_issuers')


def test_mcp_tokens_from_before_the_sso_stop_are_refused_and_setting_writes_clear_the_cache(store: Store):
    # given
    idp = FakeIdp()
    first = app_client(store, idp, mcp_settings(idp))
    second = app_client(store, idp, mcp_settings(idp))
    old = mint(idp, iat=int(time.time()) - 100)

    # when
    with first as a, second as b:
        before_stop = rpc(a, 'tools/list', old)
        cached_elsewhere = rpc(b, 'tools/list', old)
        a.put(SETTINGS, json={'key': 'sso.enabled', 'value': 'false'}, headers=AUTH)
        a.put(SETTINGS, json={'key': 'sso.enabled', 'value': 'true'}, headers=AUTH)
        b.app.state.live.refresh(force=True)
        here = rpc(a, 'tools/list', old)
        there = rpc(b, 'tools/list', old)
        fresh = mint(idp, email=PAUL, iat=int(time.time()) + 2)
        after_stop = rpc(a, 'tools/list', fresh)
        a.put(SETTINGS, json={'key': 'mcp.allowed-clients', 'value': 'other-app'}, headers=AUTH)
        pinned = rpc(a, 'tools/list', fresh)
        a.put(SETTINGS, json={'key': 'mcp.allowed-clients', 'value': ''}, headers=AUTH)
        reopened = rpc(a, 'tools/list', fresh)
        saved = a.put('/api/admin/sso', json={'values': {'roles-claim': 'groups'}}, headers=AUTH)
        moved = rpc(a, 'tools/list', fresh)
        a.put(SETTINGS, json={'key': 'sso.roles-claim', 'value': 'roles'}, headers=AUTH)
        back = rpc(a, 'tools/list', fresh)
        a.put(SETTINGS, json={'key': 'sso.require-verified-email', 'value': 'true'}, headers=AUTH)
        strict = rpc(a, 'tools/list', fresh)

    # then
    assert (before_stop.status_code, cached_elsewhere.status_code) == (200, 200)
    for name, res in {'serving pod': here, 'cached on another pod': there, 'client pinned': pinned}.items():
        assert (res.status_code, res.json()) == (401, {'detail': msg('mcp.token_invalid')}), name
        assert challenge(res).startswith('Bearer error="invalid_token"'), name
    assert after_stop.status_code == 200 and reopened.status_code == 200, 'a token issued after the stop passes'
    assert saved.status_code == 200
    assert (moved.status_code, moved.json()) == (403, {'detail': msg('mcp.role_missing', role='SKOPAX_MCP')}), 'a save on the sign-in card clears the cache'
    assert back.status_code == 200
    assert (strict.status_code, strict.json()) == (403, {'detail': msg('mcp.unverified')}), 'an sso.* write on the settings route clears the cache'


def test_a_viewer_listing_tools_keeps_input_validation_for_everyone(store: Store, caplog):
    # given
    idp = FakeIdp()
    board(store, *cards())
    client = app_client(store, idp, mcp_settings(idp))
    viewer = mint(idp, email=DANA)
    operator = mint(idp, email=PAUL, roles=('SKOPAX_MCP', 'ops-operators'))

    # when
    with client as c:
        listed(rpc(c, 'tools/list', viewer))
        refused, refused_failed = call(c, viewer, 'resolve_card', {'card': 14})
        listed(rpc(c, 'tools/list', viewer))
        bad, bad_failed = call(c, operator, 'mute_card', {'card': 14, 'hours': 1000})
        everything = listed(rpc(c, 'tools/list', operator))

    # then
    assert bad_failed and 'Input validation error' in bad
    assert (refused, refused_failed) == (msg('mcp.needs_role', role='operator'), True)
    assert [r for r in caplog.records if 'no validation will be performed' in r.getMessage()] == []
    assert everything == VIEWER_TOOLS + OPERATOR_TOOLS
    assert store.list_actions(None, 10) == []


def test_an_ask_over_mcp_runs_with_the_per_call_role(store: Store):
    # given
    idp = FakeIdp()
    users = Users(store.db)
    users.upsert_sso(PAUL, 'Paul', 'viewer')
    users.upsert_sso(BOB, 'Bob', 'viewer')
    users.col.update_one({'_id': BOB}, {'$set': {'role': 'admin', 'roleFrom': 'admin', 'adminRole': 'admin'}})
    client = TestClient(ready_app(mcp_settings(idp), store, sso_http=SsoHttp(fetch=idp.fetch, exchange=idp.exchange)), base_url='https://testserver')

    # when
    with client as c:
        raised, raised_failed = call(c, mint(idp, email=PAUL, roles=('SKOPAX_MCP', 'ops-admins')), 'ask', {'question': 'Why is checkout-service slow?', 'wait_seconds': 0})
        lowered, lowered_failed = call(c, mint(idp, email=BOB, roles=('SKOPAX_MCP', 'ops-operators')), 'ask', {'question': 'Why is checkout-service slow?', 'wait_seconds': 0})
    asks = {a['requestedBy']: a for a in store.list_actions('requested', 10) if a['type'] == 'ask'}
    page_ask = {'type': 'ask', 'requestedBy': BOB, 'source': 'chat'}

    # then
    assert not raised_failed and not lowered_failed
    assert (asks[PAUL]['mcpRole'], asker_role(asks[PAUL], users)) == ('admin', 'admin'), 'the MCP role raises the stored viewer for this call'
    assert (asks[BOB]['mcpRole'], asker_role(asks[BOB], users)) == ('operator', 'operator'), 'the MCP role lowers the stored admin for this call'
    assert asker_role(page_ask, users) == 'admin', 'a page ask keeps the stored role'
    assert users.get(PAUL)['role'] == 'viewer' and users.get(BOB)['role'] == 'admin'


def test_forget_wins_over_a_cache_miss_in_flight(store: Store):
    # given
    idp = FakeIdp()
    users = Users(store.db)
    operator_doc(users, NINA, 'Nina')
    client = app_client(store, idp, mcp_settings(idp))
    tok = mint(idp, email=NINA)

    # when
    with client as c:
        auth = c.app.state.mcp_auth
        real = auth.signin

        class Racing:
            def mcp(self, claims, issuer):
                found = real.mcp(claims, issuer)
                c.patch(f'/api/admin/users/{NINA}', json={'disabled': True}, headers=AUTH)
                return found

        auth.signin = Racing()
        raced = rpc(c, 'tools/list', tok)
        auth.signin = real
        after = rpc(c, 'tools/list', tok)

    # then
    assert raced.status_code == 200, 'the call that read the doc before the change still answers'
    assert (after.status_code, after.json()) == (403, {'detail': msg('mcp.disabled')}), 'its answer was not cached past the forget'


def test_a_concurrent_first_mcp_call_never_rewrites_a_known_user(store: Store, monkeypatch):
    # given
    idp = FakeIdp()
    users = Users(store.db)
    users.upsert_sso(PAUL, 'Paul Stone', 'viewer')
    users.col.update_one({'_id': PAUL}, {'$set': {'lastLogin': '2026-01-01T00:00:00Z'}})
    before = store.db['users'].find_one({'_id': PAUL})
    real = Users.get
    missed = []

    def racing(self, email):
        if email == PAUL and not missed:
            missed.append(email)
            return None
        return real(self, email)

    client = app_client(store, idp, mcp_settings(idp))

    # when
    with client as c:
        monkeypatch.setattr(Users, 'get', racing)
        res = rpc(c, 'tools/list', mint(idp, email=PAUL, name='Someone Else'))

    # then
    assert missed == [PAUL] and res.status_code == 200
    after = store.db['users'].find_one({'_id': PAUL})
    assert {k: v for k, v in after.items() if k != 'lastLogin'} == {k: v for k, v in before.items() if k != 'lastLogin'}, 'insert only: no name write'


@pytest.mark.parametrize('doc', [
    {'_id': CAROL, 'email': CAROL, 'name': 'Carol', 'role': 'admin', 'source': 'local', 'disabled': False, 'lastLogin': None},
    {'_id': CAROL, 'email': CAROL, 'name': 'Carol', 'role': 'admin', 'disabled': False},
], ids=['local', 'no-source'])
def test_a_local_account_created_mid_mcp_call_is_not_taken_over(store: Store, monkeypatch, doc: dict):
    # given
    idp = FakeIdp()
    store.db['users'].insert_one(dict(doc))
    real = Users.get
    missed = []

    def racing(self, email):
        if email == CAROL and not missed:
            missed.append(email)
            return None
        return real(self, email)

    client = app_client(store, idp, mcp_settings(idp))

    # when
    with client as c:
        monkeypatch.setattr(Users, 'get', racing)
        res = rpc(c, 'tools/list', mint(idp, email=CAROL, name='Oscar Green', roles=('SKOPAX_MCP', 'ops-admins')))
        other = rpc(c, 'tools/list', mint(idp, email=BOB))

    # then
    assert missed == [CAROL]
    assert (res.status_code, res.json()) == (403, {'detail': msg('mcp.local_account')})
    assert store.db['users'].find_one({'_id': CAROL}) == doc
    assert other.status_code == 200


def test_a_clear_wins_over_a_cache_miss_in_flight(store: Store, monkeypatch):
    # given
    idp = FakeIdp()
    client = app_client(store, idp, mcp_settings(idp))
    tok = mint(idp, email=PAUL)
    raced = []

    # when
    with client as c:
        real = mcpauth.claim_values

        def racing(claims, path):
            found = real(claims, path)
            if not raced:
                raced.append(c.put(SETTINGS, json={'key': 'mcp.required-role', 'value': 'SKOPAX_OTHER'}, headers=AUTH).status_code)
            return found

        monkeypatch.setattr(mcpauth, 'claim_values', racing)
        during = rpc(c, 'tools/list', tok)
        monkeypatch.setattr(mcpauth, 'claim_values', real)
        after = rpc(c, 'tools/list', tok)

    # then
    assert raced == [200] and during.status_code == 200, 'the call that read the old settings still answers'
    assert (after.status_code, after.json()) == (403, {'detail': msg('mcp.role_missing', role='SKOPAX_OTHER')}), 'its answer was not cached past the clear'


def test_a_sign_in_card_save_that_fails_on_the_secret_still_clears_the_cache(store: Store, monkeypatch):
    # given
    idp = FakeIdp()
    client = app_client(store, idp, mcp_settings(idp, sso_client_secret=''))
    tok = mint(idp, email=PAUL)

    def refused(value, by):
        raise InvalidSecret()

    # when
    with client as c:
        c.app.state.sso_secret.set('client-secret', 'admin@example.com')
        warm = rpc(c, 'tools/list', tok)
        monkeypatch.setattr(c.app.state.sso_secret, 'set', refused)
        saved = c.put('/api/admin/sso', json={'values': {'roles-claim': 'groups', 'client-secret': 'another-secret'}}, headers=AUTH)
        after = rpc(c, 'tools/list', tok)

    # then
    assert warm.status_code == 200 and saved.status_code == 422
    assert (after.status_code, after.json()) == (403, {'detail': msg('mcp.role_missing', role='SKOPAX_MCP')}), 'the roles-claim write landed, so the cache was cleared'


def test_an_unreachable_provider_is_a_503_for_mcp_and_a_bad_token_stays_401(store: Store, caplog):
    # given
    down = FakeIdp()
    down.fail = True
    keys_down = FakeIdp()
    keys_down.jwks = lambda: (_ for _ in ()).throw(OSError('the keys are unreachable'))
    up = FakeIdp()
    expected = (503, {'detail': msg('mcp.provider_unavailable')})
    before = docs(store)

    # when
    with caplog.at_level(logging.INFO, logger='skopax.mcp'):
        with app_client(store, down, mcp_settings(down)) as c:
            unreachable = rpc(c, 'tools/list', mint(down))
        with app_client(store, keys_down, mcp_settings(keys_down)) as c:
            no_keys = rpc(c, 'tools/list', mint(keys_down))
        written = docs(store)
        with app_client(store, up, mcp_settings(up)) as c:
            bad = rpc(c, 'tools/list', mint(up, key=OTHER_KEY))
            good = rpc(c, 'tools/list', mint(up))
    lines = [(r.levelname, r.getMessage()) for r in caplog.records if r.name == 'skopax.mcp']

    # then
    for name, res in {'discovery down': unreachable, 'keys down': no_keys}.items():
        assert (res.status_code, res.json()) == expected, name
        assert 'www-authenticate' not in res.headers, name
    assert DANA not in [d['_id'] for d in written] and len(written) == len(before)
    assert (bad.status_code, bad.json()) == (401, {'detail': msg('mcp.token_invalid')})
    assert challenge(bad).startswith('Bearer error="invalid_token"')
    assert good.status_code == 200
    assert [level for level, text in lines if 'user check failed' in text] == ['WARNING', 'WARNING']
    assert len([text for _, text in lines if 'rejected' in text]) == 1


def test_an_unreadable_stop_stamp_keeps_mcp_tokens_working_and_a_later_stop_still_ends_them(store: Store):
    # given
    idp = FakeIdp()
    clock = Clock()
    client = app_client(store, idp, mcp_settings(idp), clock=clock)
    tok = mint(idp, iat=int(clock.now) - 100)

    # when
    with client as c:
        store.db['tunables'].replace_one({'_id': 'sso.sessions-after'}, {'_id': 'sso.sessions-after', 'value': 'not-a-stamp'}, upsert=True)
        c.app.state.live.refresh(force=True)
        c.app.state.mcp_auth.clear()
        odd = rpc(c, 'tools/list', tok)
        c.put(SETTINGS, json={'key': 'sso.enabled', 'value': 'false'}, headers=AUTH)
        c.put(SETTINGS, json={'key': 'sso.enabled', 'value': 'true'}, headers=AUTH)
        stopped = rpc(c, 'tools/list', tok)
        fresh = rpc(c, 'tools/list', mint(idp, email=PAUL, iat=int(clock.now)))

    # then
    assert odd.status_code == 200, 'an unreadable stamp never ends an MCP token'
    assert (stopped.status_code, stopped.json()) == (401, {'detail': msg('mcp.token_invalid')}), 'a real stop after it still ends the token'
    assert fresh.status_code == 200
