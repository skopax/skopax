from skopax.messages import msg
from skopax.sso import FIELDS
from skopax.store import Store
from tests.conftest import ADMIN_EMAIL, AUTH
from tests.sso_kit import FakeIdp, app_client, idp_settings

SSO = '/api/admin/sso'
SETTINGS = '/api/admin/settings'


def view(client) -> dict:
    return client.get(SSO, headers=AUTH).json()


def test_the_sso_view_names_the_layer_under_each_admin_override(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp, idp_settings(idp))
    before = view(client)

    # when
    saved = client.put(SSO, json={'values': {'issuer': 'https://idp2.example.com', 'scope': 'openid email'}}, headers=AUTH)
    switched = client.put(SETTINGS, json={'key': 'sso.enabled', 'value': 'false'}, headers=AUTH)
    after = view(client)
    dropped = client.delete(SETTINGS, params={'key': 'sso.enabled'}, headers=AUTH)
    back = view(client)

    # then
    assert set(before['bases']) == set(FIELDS) - {'client-secret'}
    assert (before['bases']['issuer'], before['bases']['scope']) == ('environment', 'default')
    assert before['switch'] == {'value': 'true', 'source': 'default', 'from': None, 'base': 'default', 'baseValue': True}
    assert before['secretNote'] is None
    assert saved.status_code == 200 and switched.status_code == 200 and dropped.status_code == 200
    assert (after['values']['issuer']['source'], after['bases']['issuer']) == ('admin', 'environment')
    assert (after['values']['scope']['source'], after['bases']['scope']) == ('admin', 'default')
    assert after['switch'] == {'value': 'false', 'source': 'admin', 'from': None, 'base': 'default', 'baseValue': True}
    assert after['enabled'] is False
    assert back['switch'] == before['switch'] and back['enabled'] is True


def test_the_switch_names_its_configured_layer(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp, idp_settings(idp, sso_on=False))

    # when
    shown = view(client)
    client.put(SETTINGS, json={'key': 'sso.enabled', 'value': 'true'}, headers=AUTH)
    over = view(client)

    # then
    assert shown['switch'] == {'value': 'false', 'source': 'config', 'from': 'environment', 'base': 'environment', 'baseValue': False}
    assert over['switch'] == {'value': 'true', 'source': 'admin', 'from': None, 'base': 'environment', 'baseValue': False}


def test_an_unreadable_secret_has_its_own_note(store: Store):
    # given
    idp = FakeIdp()
    first = app_client(store, idp, idp_settings(idp, sso_client_secret='', secret_key='k1'))
    first.app.state.sso_secret.set('admin-secret-value', ADMIN_EMAIL)
    readable = view(first)

    # when
    other = app_client(store, idp, idp_settings(idp, sso_client_secret='', secret_key='k2'))
    other.app.state.sso_secret.refresh()
    parked = view(other)

    # then
    assert readable['secretNote'] is None and readable['secret']['set'] is True
    assert (parked['secret']['set'], parked['secret']['unreadable']) == (False, True)
    assert parked['secretNote'] == msg('sso.secret_unreadable_note')
    assert 'admin-secret-value' not in str(parked)
