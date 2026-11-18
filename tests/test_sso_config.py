import dataclasses
import html
import logging
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from skopax import clock as clocks
from skopax.app import create_app
from skopax.contract import SourceData
from skopax.config import load_settings
from skopax.messages import msg
from skopax.sealer import META_ID, SEALED, Sealer
from skopax.settingsources import SourceSet
from skopax.sso import SECRET_ID, InvalidSecret, SourceOwned, SsoHttp
from skopax.store import Store
from skopax.tunables import TunableStore
from skopax.users import Users
from tests.conftest import ADMIN_EMAIL, AUTH, TOKEN
from tests.fake_anthropic import FakeAnthropic
from tests.sso_kit import OSCAR, OTHER_KEY, SECRET, FakeIdp, app_client, idp_settings, log_in
from tests.test_auth import Clock

SETTINGS = '/api/admin/settings'
PASSWORD = 'correct horse battery'
ENV = {'MONGO_URI': 'mongodb://unused', 'SKOPAX_TOKEN': TOKEN, 'SKOPAX_BASE_URL': 'https://skopax.example.com', 'SSO_ISSUER': 'https://idp.example.com',
       'SSO_CLIENT_SECRET': SECRET, 'CHAT_UI': 'false'}
SIGN_IN = {'sso.enabled': 'bool', 'sso.issuer': 'text', 'sso.client-id': 'text', 'sso.scope': 'text', 'sso.claims': 'choice', 'sso.client-auth': 'choice',
           'sso.require-verified-email': 'bool', 'sso.authorize-url': 'text', 'sso.token-url': 'text', 'sso.jwks-url': 'text'}


def put(client: TestClient, key: str, value: str):
    return client.put(SETTINGS, json={'key': key, 'value': value}, headers=AUTH)


def reset(client: TestClient, key: str):
    return client.delete(SETTINGS, params={'key': key}, headers=AUTH)


def entries(client: TestClient) -> dict[str, dict]:
    return {i['key']: i for i in client.get(SETTINGS, headers=AUTH).json()['items']}


def authorize_at(client: TestClient) -> str:
    res = client.get('/login/sso', follow_redirects=False)
    return res.headers.get('location', '') if res.status_code == 302 else f'status {res.status_code}'


def endpoint_of(location: str) -> str:
    parts = urlsplit(location)
    return f'{parts.scheme}://{parts.netloc}{parts.path}'


def shows(res, key: str, **values) -> bool:
    return html.escape(msg(key, **values)) in res.text


def signed_in(res) -> bool:
    return any(c.startswith('skopax_session=') and 'max-age=0' not in c.lower() for c in res.headers.get_list('set-cookie'))


def me(client: TestClient) -> str | None:
    return client.get('/api/me').json()['user']


def two_idps(*idps: FakeIdp) -> SsoHttp:
    def pick(url: str) -> FakeIdp:
        return next(i for i in idps if url.startswith(i.issuer.removesuffix('/') + '/'))

    return SsoHttp(fetch=lambda url: pick(url).fetch(url), exchange=lambda url, data, headers: pick(url).exchange(url, data, headers))


def second_idp() -> FakeIdp:
    idp = FakeIdp('https://idp2.example.com', kid='k2')
    idp.key = OTHER_KEY
    return idp


def client_of(store: Store, s, http: SsoHttp, clock=None) -> TestClient:
    return TestClient(create_app(s, store, sso_http=http, clock=clock), base_url='https://testserver')


def test_sso_settings_are_live_with_source_labels(store: Store):
    # given
    idp, idp2 = FakeIdp(), second_idp()
    base = dataclasses.replace(load_settings(env=ENV), mongo_uri='mongodb://unused', public_read=True)
    client = client_of(store, base, two_idps(idp, idp2))

    # when
    from_env = entries(client)
    first = authorize_at(client)
    changed = put(client, 'sso.issuer', idp2.issuer)
    by_admin = entries(client)['sso.issuer']
    moved = authorize_at(client)
    public_lines = [a['text'] for a in client.get('/api/activity').json()]
    admin_lines = [(a['kind'], a['text']) for a in client.get('/api/activity', headers=AUTH).json()]
    refused = {url: put(client, 'sso.issuer', url) for url in ('http://idp.example.com', 'https://user@idp.example.com', 'https://idp.example.com/#x')}
    after_refusals = entries(client)['sso.issuer']['value']
    blank_override = put(client, 'sso.token-url', '')
    back = reset(client, 'sso.issuer')
    restored = entries(client)['sso.issuer']
    again = authorize_at(client)

    # then
    shown = {k: v for k, v in from_env.items() if v['group'] == 'Sign-in'}
    assert {k: shown[k]['kind'] for k in SIGN_IN} == SIGN_IN and all(shown[k]['live'] for k in SIGN_IN)
    assert shown['sso.claims']['choices'] == ['id-token', 'access-token'] and shown['sso.client-auth']['choices'] == ['auto', 'post', 'basic']
    assert (shown['sso.issuer']['value'], shown['sso.issuer']['source'], shown['sso.issuer']['from']) == (idp.issuer, 'config', 'environment')
    assert 'sso.client-secret' not in from_env
    assert endpoint_of(first) == idp.discovery['authorization_endpoint']
    assert changed.status_code == 200 and (by_admin['value'], by_admin['source'], by_admin['base']) == (idp2.issuer, 'admin', idp.issuer)
    assert endpoint_of(moved) == idp2.discovery['authorization_endpoint']
    assert not any('sso.issuer' in line for line in public_lines)
    assert ('access', msg('sso.setting_changed', label='Issuer URL', key='sso.issuer', before=idp.issuer, after=idp2.issuer)) in admin_lines
    label = shown['sso.issuer']['label']
    assert {u: (r.status_code, r.json()['detail']) for u, r in refused.items()} == {
        'http://idp.example.com': (422, msg('sso.url_scheme', field=label)),
        'https://user@idp.example.com': (422, msg('sso.url_userinfo', field=label)),
        'https://idp.example.com/#x': (422, msg('sso.url_fragment', field=label))}
    assert after_refusals == idp2.issuer and blank_override.status_code == 200
    assert back.status_code == 200 and (restored['value'], restored['source'], restored['from']) == (idp.issuer, 'config', 'environment')
    assert endpoint_of(again) == idp.discovery['authorization_endpoint']


def test_the_client_secret_is_sealed_and_a_source_secret_is_read_only(store: Store):
    # given
    idp = FakeIdp()
    admin_set = app_client(store, idp, idp_settings(idp, sso_client_secret='', secret_key='k1'))
    held = admin_set.app.state.sso_secret
    before = (held.current(), admin_set.app.state.signon.active(), held.status())

    # when
    shown = held.set('admin-secret-value', ADMIN_EMAIL)
    doc = store.db['admin_secrets'].find_one({'_id': SECRET_ID})
    change = store.db['tunable_changes'].find_one({'key': SECRET_ID})
    login = log_in(admin_set, idp)
    with pytest.raises(InvalidSecret):
        held.set('x' * 2001, ADMIN_EMAIL)
    sourced_client = app_client(store, idp, idp_settings(idp, sso_client_secret='env-secret-value', secret_key='k1'))
    sourced = sourced_client.app.state.sso_secret
    sourced.refresh()
    with pytest.raises(SourceOwned) as owned:
        sourced.set('another-secret-value', ADMIN_EMAIL)
    with pytest.raises(SourceOwned):
        sourced.clear(ADMIN_EMAIL)
    kept = store.db['admin_secrets'].find_one({'_id': SECRET_ID})

    # then
    assert before == ('', False, {'set': False, 'from': None, 'unreadable': False, 'updatedBy': None, 'updatedAt': None})
    assert doc['value'].startswith(SEALED) and 'admin-secret-value' not in str(doc) and Sealer('k1').open(doc['value']) == 'admin-secret-value'
    assert held.current() == 'admin-secret-value' and admin_set.app.state.signon.active()
    assert (shown['set'], shown['from'], shown['unreadable'], shown['updatedBy']) == (True, None, False, ADMIN_EMAIL) and shown['updatedAt']
    assert 'admin-secret-value' not in str(shown)
    assert {k: change[k] for k in ('key', 'secret', 'old', 'new', 'by')} == {'key': SECRET_ID, 'secret': True, 'old': None, 'new': 'set', 'by': ADMIN_EMAIL}
    assert login.status_code == 303 and idp.exchanges[-1]['data']['client_secret'] == 'admin-secret-value'
    assert sourced.status() == {'set': True, 'from': 'environment', 'unreadable': False, 'updatedBy': None, 'updatedAt': None}
    assert str(owned.value) == msg('sso.secret_source_owned', source='environment')
    assert sourced.current() == 'env-secret-value'
    assert kept['value'] == doc['value'] and store.db['tunable_changes'].count_documents({'key': SECRET_ID}) == 1


def test_an_admin_secret_can_be_cleared_and_says_so_in_history(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp, idp_settings(idp, sso_client_secret='', secret_key='k1'))
    held = client.app.state.sso_secret
    held.set('admin-secret-value', ADMIN_EMAIL)

    # when
    gone = held.clear(ADMIN_EMAIL)
    again = held.clear(ADMIN_EMAIL)

    # then
    rows = [(r['old'], r['new'], r['secret']) for r in store.db['tunable_changes'].find({'key': SECRET_ID}).sort('seq', 1)]
    assert (gone, again) == (True, False)
    assert held.current() == '' and store.db['admin_secrets'].find_one({'_id': SECRET_ID}) is None
    assert rows == [(None, 'set', True), ('set', None, True)]
    assert client.app.state.signon.config().problem() == msg('sso.problem.secret', field='sso.client-secret')


def test_an_unreadable_secret_parks_sso_with_a_reason(store: Store):
    # given
    idp = FakeIdp()
    first = app_client(store, idp, idp_settings(idp, sso_client_secret='', secret_key='k1'))
    first.app.state.sso_secret.set('admin-secret-value', ADMIN_EMAIL)
    no_meta_key = store.db['meta'].find_one({'_id': META_ID}) is None

    # when
    other = app_client(store, idp, idp_settings(idp, sso_client_secret='', secret_key='k2'))
    other.app.state.sso_secret.refresh()
    parked = other.app.state.signon
    login = other.get('/login/sso', follow_redirects=False)
    working = first.get('/login/sso', follow_redirects=False)

    # then
    assert no_meta_key
    assert parked.config().problem() == msg('sso.problem.secret_unreadable') and parked.active() is False
    assert other.app.state.sso_secret.status()['unreadable'] is True
    assert login.status_code == 404 and shows(login, 'login.sso_off')
    assert first.app.state.signon.config().problem() is None and working.status_code == 302


CASES = {
    'no issuer': ({}, {'sso.issuer': ''}, 'sso.problem.issuer', 'sso.issuer'),
    'http issuer': ({'sso_issuer': 'http://idp.example.com'}, {}, 'sso.problem.issuer', 'sso.issuer'),
    'no client id': ({}, {'sso.client-id': ''}, 'sso.problem.client_id', 'sso.client-id'),
    'no secret': ({'sso_client_secret': ''}, {}, 'sso.problem.secret', 'sso.client-secret'),
    'id-token with scope profile': ({}, {'sso.scope': 'profile'}, 'sso.problem.scope', 'sso.scope'),
    'bad override': ({'sso_token_url': 'http://idp.example.com/token'}, {}, 'sso.problem.endpoint', 'sso.token-url'),
    'no redirect': ({'sso_redirect_uri': '', 'base_url': ''}, {}, 'sso.problem.redirect', 'sso.redirect-uri'),
}


@pytest.mark.parametrize('case', list(CASES))
def test_an_incomplete_or_invalid_config_keeps_sso_off_with_a_reason(store: Store, case: str):
    # given
    over, admin, key, field = CASES[case]
    idp = FakeIdp()
    client = app_client(store, idp, idp_settings(idp, **over))
    control = app_client(store, idp)

    # when
    written = [put(client, k, v).status_code for k, v in admin.items()]
    login = client.get('/login/sso', follow_redirects=False)

    # then
    assert control.app.state.signon.active() and control.app.state.signon.config().problem() is None
    assert written == [200] * len(admin)
    assert client.app.state.signon.active() is False
    assert client.app.state.signon.config().problem() == msg(key, field=field)
    assert login.status_code == 404


def test_a_config_change_drops_the_cached_discovery_and_keys(store: Store):
    # given
    idp, idp2 = FakeIdp(), second_idp()
    client = client_of(store, idp_settings(idp), two_idps(idp, idp2))
    first = log_in(client, idp)

    # when
    put(client, 'sso.issuer', idp2.issuer)
    params = idp2.authorize(client.get('/login/sso', follow_redirects=False).headers['location'])
    old_token = idp.token(nonce=params['nonce'], email=OSCAR)
    idp2.response = {'access_token': old_token, 'id_token': old_token, 'token_type': 'Bearer'}
    refused = client.get('/authorized', params={'code': 'auth-code', 'state': params['state']}, follow_redirects=False)
    idp2.response = None
    second = log_in(client, idp2)
    log_in(client, idp2)

    # then
    assert first.status_code == 303 and signed_in(first)
    assert refused.status_code == 401 and shows(refused, 'login.failed') and not signed_in(refused)
    assert second.status_code == 303 and signed_in(second)
    assert idp2.calls[idp2.discovery_url] == 1 and idp.calls[idp.discovery_url] == 1
    assert idp2.calls[idp2.jwks_url] >= 1


def test_a_login_started_before_an_issuer_change_must_start_again(store: Store):
    # given
    idp, idp2 = FakeIdp(), second_idp()
    client = client_of(store, idp_settings(idp), two_idps(idp, idp2))
    control = log_in(client, idp)
    params = idp.authorize(client.get('/login/sso', follow_redirects=False).headers['location'])

    # when
    put(client, 'sso.issuer', idp2.issuer)
    res = client.get('/authorized', params={'code': 'auth-code', 'state': params['state']}, follow_redirects=False)

    # then
    assert control.status_code == 303 and len(idp.exchanges) == 1
    assert res.status_code == 400 and shows(res, 'login.restart') and not signed_in(res)
    assert len(idp.exchanges) == 1 and idp2.exchanges == []


def test_switching_sso_off_ends_sso_sessions_but_not_local_ones(store: Store):
    # given
    idp, clock = FakeIdp(), Clock()
    Users(store.db).set_password(ADMIN_EMAIL, PASSWORD)
    app = create_app(idp_settings(idp), store, sso_http=SsoHttp(idp.fetch, idp.exchange), clock=clock)
    sso_side, local_side, admin = (TestClient(app, base_url='https://testserver') for _ in range(3))
    log_in(sso_side, idp)
    local_side.post('/login', data={'email': ADMIN_EMAIL, 'password': PASSWORD})
    before = (me(sso_side), me(local_side))

    # when
    switched_off = put(admin, 'sso.enabled', 'false')
    clock.advance(31)
    off = (me(sso_side), me(local_side), sso_side.get('/login/sso', follow_redirects=False).status_code)
    put(admin, 'sso.enabled', 'true')
    fresh = TestClient(app, base_url='https://testserver')
    relogin = log_in(fresh, idp)

    # then
    assert before == (OSCAR, ADMIN_EMAIL)
    assert switched_off.status_code == 200
    assert off == (None, ADMIN_EMAIL, 404)
    assert relogin.status_code == 303 and me(fresh) == OSCAR


def test_an_idp_outage_does_not_end_sessions(store: Store):
    # given
    idp, clock = FakeIdp(), Clock()
    client = app_client(store, idp, clock=clock)
    log_in(client, idp)
    before = me(client)

    # when
    idp.fail = True
    clock.advance(31)
    after = me(client)

    # then
    assert before == after == OSCAR


def test_the_default_scope_is_openid_email_profile(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp)

    # when
    scope = parse_qs(urlsplit(authorize_at(client)).query)['scope'][0]
    shown = entries(client)['sso.scope']

    # then
    assert load_settings(env={}).sso_scope == 'openid email profile'
    assert scope == 'openid email profile'
    assert (shown['default'], shown['value'], shown['source']) == ('openid email profile', 'openid email profile', 'default')


def test_an_admin_set_secret_mounts_the_chat_at_the_next_boot_and_not_before(store: Store):
    # given
    idp = FakeIdp()
    ready = idp_settings(idp, sso_client_secret='', secret_key='k1', chat_ui=True, anthropic_api_key='sk-test', investigate_mode='live')
    http = SsoHttp(idp.fetch, idp.exchange)
    with TestClient(create_app(ready, store, sso_http=http, anthropic_client=FakeAnthropic([])), base_url='https://testserver') as booted_without:
        booted_without.app.state.sso_secret.set('admin-secret-value', ADMIN_EMAIL)
        log_in(booted_without, idp)
        late = (booted_without.app.state.signon.active(), booted_without.get('/api/me').json()['chat'], features_of(booted_without)['chat']['restart'],
                booted_without.get('/chat-continue/some-chat', follow_redirects=False).headers['location'])

    # when
    with TestClient(create_app(ready, store, sso_http=http, anthropic_client=FakeAnthropic([])), base_url='https://testserver') as restarted:
        log_in(restarted, idp)
        mounted = (restarted.get('/api/me').json()['chat'], features_of(restarted)['chat'], restarted.get('/chat-continue/some-chat', follow_redirects=False).headers['location'])

    # then
    assert late == (True, False, msg('features.restart'), '/')
    assert mounted[0] is True and (mounted[1]['needs'], mounted[1]['restart']) == ([], None)
    assert not mounted[2].startswith('/login')


def features_of(c: TestClient) -> dict[str, dict]:
    return {i['id']: i for i in c.get('/api/admin/features', headers=AUTH).json()['items']}


AFTER_ID = 'sso.sessions-after'


def test_an_sso_session_from_before_a_switch_off_stays_ended_after_switching_on(store: Store):
    # given
    idp, clock, pod_clock = FakeIdp(), Clock(), Clock()
    Users(store.db).set_password(ADMIN_EMAIL, PASSWORD)
    s, http = idp_settings(idp), SsoHttp(idp.fetch, idp.exchange)
    app = create_app(s, store, sso_http=http, clock=clock)
    old_side, local_side, admin = (TestClient(app, base_url='https://testserver') for _ in range(3))
    log_in(old_side, idp)
    local_side.post('/login', data={'email': ADMIN_EMAIL, 'password': PASSWORD})
    other_pod = create_app(s, store, sso_http=http, clock=clock)
    other_pod.state.live.clock = pod_clock
    other = TestClient(other_pod, base_url='https://testserver')
    other.cookies.set('skopax_session', old_side.cookies.get('skopax_session'))
    before = (me(old_side), me(local_side), me(other))

    # when
    clock.advance(5)
    switched_at = clock()
    put(admin, 'sso.enabled', 'false')
    clock.advance(31)
    put(admin, 'sso.enabled', 'false')
    put(admin, 'sso.enabled', 'true')
    switched_on_at = clock()
    clock.advance(31)
    other_before_refresh = me(other)
    pod_clock.advance(16)
    fresh = TestClient(app, base_url='https://testserver')
    relogin = log_in(fresh, idp)
    after = (me(old_side), me(local_side), me(other), me(fresh))
    stamp = store.db['tunables'].find_one({'_id': AFTER_ID})

    # then
    assert before == (OSCAR, ADMIN_EMAIL, OSCAR)
    assert other_before_refresh == OSCAR
    assert relogin.status_code == 303
    assert after == (None, ADMIN_EMAIL, None, OSCAR)
    assert clocks.stamp(switched_at) <= stamp['value'] <= clocks.stamp(switched_on_at)
    assert AFTER_ID not in entries(admin) and reset(admin, AFTER_ID).status_code == 404


def test_a_broken_sso_config_ends_no_session(store: Store):
    # given
    idp, clock = FakeIdp(), Clock()
    client = app_client(store, idp, clock=clock)
    admin = TestClient(client.app, base_url='https://testserver')
    log_in(client, idp)
    before = me(client)

    # when
    broken = put(admin, 'sso.client-id', '')
    clock.advance(31)
    after = me(client)
    login = client.get('/login/sso', follow_redirects=False)

    # then
    assert broken.status_code == 200 and client.app.state.signon.active() is False
    assert login.status_code == 404
    assert before == after == OSCAR
    assert store.db['tunables'].find_one({'_id': AFTER_ID}) is None


def test_a_source_supplied_secret_carries_its_label(store: Store):
    # given
    idp = FakeIdp()
    data = SourceData('vault', {SECRET_ID: 'vault-secret-value'})
    sources = SourceSet([('vault', lambda: data)], {'vault': data})
    app = create_app(idp_settings(idp, sso_client_secret='vault-secret-value'), store, sso_http=SsoHttp(idp.fetch, idp.exchange), sources=sources)
    held = app.state.sso_secret

    # when
    shown = held.status()
    with pytest.raises(SourceOwned) as owned:
        held.set('another-secret-value', ADMIN_EMAIL)

    # then
    assert (shown['set'], shown['from']) == (True, 'vault')
    assert str(owned.value) == msg('sso.secret_source_owned', source='vault') and 'vault' in str(owned.value)
    assert held.current() == 'vault-secret-value'


def test_an_unreadable_admin_secret_is_not_reported_while_a_base_layer_secret_is_in_use(store: Store, caplog):
    # given
    idp = FakeIdp()
    app_client(store, idp, idp_settings(idp, sso_client_secret='', secret_key='k1')).app.state.sso_secret.set('admin-secret-value', ADMIN_EMAIL)
    sourced = app_client(store, idp, idp_settings(idp, sso_client_secret='env-secret-value', secret_key='k2')).app.state.sso_secret
    bare = app_client(store, idp, idp_settings(idp, sso_client_secret='', secret_key='k2')).app.state.sso_secret
    caplog.set_level(logging.DEBUG)

    # when
    sourced.refresh()
    quiet = [r.getMessage() for r in caplog.records if r.name == 'skopax.sso']
    bare.refresh()
    loud = [r.getMessage() for r in caplog.records if r.name == 'skopax.sso']

    # then
    assert quiet == [] and sourced.current() == 'env-secret-value'
    assert loud == [msg('sso.problem.secret_unreadable')]


def wait_until(check, seconds: float = 5) -> bool:
    deadline = time.monotonic() + seconds
    while not check() and time.monotonic() < deadline:
        time.sleep(0.02)
    return check()


def test_a_settings_source_switching_sso_off_ends_sessions_for_good(store: Store):
    # given
    idp, clock = FakeIdp(), Clock()
    box = {'data': SourceData('vault', {'sso.enabled': 'true'})}
    sources = SourceSet([('vault', lambda: box['data'])], {'vault': box['data']}, every=0.01)
    app = create_app(idp_settings(idp), store, sso_http=SsoHttp(idp.fetch, idp.exchange), clock=clock, sources=sources)
    with TestClient(app, base_url='https://testserver') as old_side:
        log_in(old_side, idp)
        before = me(old_side)

        # when
        clock.advance(5)
        switched_at = clock()
        box['data'] = SourceData('vault', {'sso.enabled': 'false'})
        went_off = wait_until(lambda: not app.state.live.base.sso_on)
        off = me(old_side)
        clock.advance(31)
        box['data'] = SourceData('vault', {'sso.enabled': 'true'})
        came_on = wait_until(lambda: app.state.live.base.sso_on)
        clock.advance(31)
        after = me(old_side)
        fresh = TestClient(app, base_url='https://testserver')
        relogin = log_in(fresh, idp)
        stamp = store.db['tunables'].find_one({'_id': AFTER_ID})

    # then
    assert (went_off, came_on) == (True, True)
    assert (before, off, after) == (OSCAR, None, None)
    assert relogin.status_code == 303 and me(fresh) == OSCAR
    assert stamp['value'] >= clocks.stamp(switched_at)
    assert store.db['tunable_changes'].count_documents({'key': AFTER_ID}) == 0


def test_advance_replaces_any_value_that_is_not_a_real_stamp_and_never_lowers_a_real_one(store: Store):
    # given
    tunables = TunableStore(store.db)
    now, older, newer = '2026-11-17T10:00:00Z', '2026-11-17T09:00:00Z', '2026-11-17T11:00:00Z'
    seeded = {'not-a-stamp': 'not-a-stamp', 'shape-only': '2026-99-99T00:00:00Z', 'number': 20261117, 'none': None, 'newer': newer, 'older': older}

    # when
    for name, value in seeded.items():
        store.db['tunables'].replace_one({'_id': name}, {'_id': name, 'value': value}, upsert=True)
    store.db['tunables'].insert_one({'_id': 'no-value'})
    for name in [*seeded, 'no-value', 'missing']:
        tunables.advance(name, now)
    found = {d['_id']: d.get('value') for d in store.db['tunables'].find()}

    # then
    assert found == {'not-a-stamp': now, 'shape-only': now, 'number': now, 'none': now, 'newer': newer, 'older': now, 'no-value': now, 'missing': now}


@pytest.mark.parametrize('stale', ['not-a-stamp', {'$gt': ''}])
def test_advance_never_lowers_a_stamp_another_pod_wrote_after_the_bad_value_was_read(store: Store, monkeypatch, stale):
    # given
    tunables = TunableStore(store.db)
    newer = '2026-11-17T11:00:00Z'
    store.db['tunables'].insert_one({'_id': AFTER_ID, 'value': newer})
    monkeypatch.setattr(tunables.values, 'find_one', lambda *args, **kwargs: {'_id': AFTER_ID, 'value': stale})

    # when
    tunables.advance(AFTER_ID, '2026-11-17T10:00:00Z')

    # then
    assert [d['value'] for d in store.db['tunables'].find({'_id': AFTER_ID})] == [newer]


def test_the_stamp_never_moves_backwards_when_two_pods_write(store: Store):
    # given
    idp, ahead, behind = FakeIdp(), Clock(), Clock()
    ahead.advance(100)
    off = idp_settings(idp, sso_on=False)
    first = create_app(off, store, sso_http=SsoHttp(idp.fetch, idp.exchange), clock=ahead).state.live
    second = create_app(off, store, sso_http=SsoHttp(idp.fetch, idp.exchange), clock=behind).state.live
    stamp = lambda: store.db['tunables'].find_one({'_id': AFTER_ID})['value']

    # when
    first.refresh(force=True)
    by_first = stamp()
    second.refresh(force=True)
    after_behind = stamp()
    behind.advance(200)
    second.refresh(force=True)
    moved = stamp()
    first.refresh(force=True)
    after_stale = stamp()

    # then
    assert by_first == after_behind == clocks.stamp(ahead())
    assert moved == after_stale == clocks.stamp(behind()) and moved > by_first


def test_while_sso_is_on_a_refresh_writes_no_stamp(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp)
    live = client.app.state.live
    writes = []
    advance = live.tunables.advance
    live.tunables.advance = lambda key, value: writes.append(key) or advance(key, value)

    # when
    for _ in range(5):
        live.refresh(force=True)
        client.get('/api/me')
    quiet = (len(writes), store.db['tunables'].find_one({'_id': AFTER_ID}))
    put(client, 'sso.enabled', 'false')
    live.refresh(force=True)

    # then
    assert quiet == (0, None)
    assert writes and set(writes) == {AFTER_ID}


@pytest.mark.parametrize('odd', ['not-a-stamp', '2026-99-99T00:00:00Z'])
def test_an_unreadable_stop_stamp_ends_no_session_and_a_later_stop_still_ends_them(store: Store, caplog, odd):
    # given
    idp, clock = FakeIdp(), Clock()
    app = create_app(idp_settings(idp), store, sso_http=SsoHttp(idp.fetch, idp.exchange), clock=clock)
    side, admin = (TestClient(app, base_url='https://testserver') for _ in range(2))
    log_in(side, idp)
    store.db['tunables'].replace_one({'_id': AFTER_ID}, {'_id': AFTER_ID, 'value': odd}, upsert=True)
    app.state.live.refresh(force=True)

    # when
    with caplog.at_level(logging.WARNING):
        kept = (me(side), me(side))
    warned = [r for r in caplog.records if r.getMessage() == msg('sso.stop_stamp_odd', key=AFTER_ID)]
    clock.advance(5)
    put(admin, 'sso.enabled', 'false')
    put(admin, 'sso.enabled', 'true')
    ended = me(side)
    stamp = store.db['tunables'].find_one({'_id': AFTER_ID})['value']

    # then
    assert kept == (OSCAR, OSCAR), 'an unreadable stamp ends no session'
    assert len(warned) == 1
    assert ended is None, 'a real stop after it still ends the session'
    assert stamp == clocks.stamp(clock())
