import dataclasses
import hashlib
import hmac
import json

import mongomock

from tests.conftest import settings
from skopax.sessions import LOGIN_KIND, SESSION_KIND, Sessions, User, b64, derive_secret, role_at_least, session_secret, unb64

DAY = 24 * 3600
SECRET = derive_secret('test-key')


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def sessions(clock: Clock | None = None, secret: bytes = SECRET, idle: int = 3 * DAY, cap: int = 7 * DAY) -> Sessions:
    return Sessions(secret, idle, cap, clock=clock or Clock())


def payload_of(sealed: str) -> dict:
    return json.loads(unb64(sealed.split('.')[0]))


def test_seal_round_trip_and_tamper():
    # given
    s = sessions()
    sealed = s.seal({'name': 'alice@example.com'}, 60, SESSION_KIND)
    body, sig = sealed.split('.')
    forged = b64(json.dumps({'name': 'bob@example.com', 'kind': SESSION_KIND, 'exp': 9999999999}).encode())

    # when
    good = s.unseal(sealed, SESSION_KIND)
    swapped = s.unseal(f'{forged}.{sig}', SESSION_KIND)
    other_key = sessions(secret=derive_secret('another-key')).unseal(sealed, SESSION_KIND)
    non_ascii = s.unseal('abc.d' + chr(233) + 'f', SESSION_KIND)
    non_ascii_renewed = s.renew('abc.d' + chr(233) + 'f')

    # then
    assert good['name'] == 'alice@example.com'
    assert (swapped, other_key, non_ascii, non_ascii_renewed) == (None, None, None, None)
    assert s.unseal(None, SESSION_KIND) is None and s.unseal('nodot', SESSION_KIND) is None


def test_wrong_kind_and_expired_are_rejected():
    # given
    clock = Clock()
    s = sessions(clock)
    login = s.seal({'state': 'abc'}, 60, LOGIN_KIND)

    # when
    as_session = s.unseal(login, SESSION_KIND)
    as_login = s.unseal(login, LOGIN_KIND)
    clock.advance(61)
    expired = s.unseal(login, LOGIN_KIND)

    # then
    assert as_session is None
    assert as_login['state'] == 'abc'
    assert expired is None


def test_a_login_cookie_from_before_the_kind_field_is_no_session_but_still_a_login():
    # given
    s = sessions()
    body = b64(json.dumps({'state': 'abc', 'verifier': 'xyz', 'exp': int(s.clock()) + 600}, separators=(',', ':')).encode())
    old = body + '.' + b64(hmac.new(s.secret, body.encode(), hashlib.sha256).digest())

    # when
    as_session = s.unseal(old, SESSION_KIND)
    as_login = s.unseal(old, LOGIN_KIND)

    # then
    assert as_session is None
    assert as_login['verifier'] == 'xyz'


def test_issue_carries_role_and_source():
    # given
    clock = Clock()
    s = sessions(clock)

    # when
    sealed = s.issue('alice@example.com', 'operator', 'sso')

    # then
    payload = s.unseal(sealed, SESSION_KIND)
    assert payload['name'] == 'alice@example.com'
    assert payload['role'] == 'operator'
    assert payload['src'] == 'sso'
    assert payload['login'] == payload['iat'] == int(clock.now)
    assert payload['exp'] == int(clock.now) + 3 * DAY


def test_renew_slides_within_max():
    # given
    clock = Clock()
    s = sessions(clock)
    sealed = s.issue('alice@example.com', 'viewer', 'local')
    login = int(clock.now)

    # when
    clock.advance(4 * 60)
    early = s.renew(sealed)
    clock.advance(2 * 60)
    renewed = s.renew(sealed)

    # then
    assert early is None
    cookie, seconds = renewed
    payload = payload_of(cookie)
    assert seconds == 3 * DAY
    assert payload['exp'] == login + 6 * 60 + 3 * DAY
    assert payload['login'] == login
    assert payload['iat'] == login + 6 * 60
    assert payload['role'] == 'viewer'


def test_renew_is_capped_by_the_maximum_and_stops_after_it():
    # given
    clock = Clock()
    s = sessions(clock)
    cookie = s.issue('alice@example.com', 'viewer', 'local')
    cap = int(clock.now) + 7 * DAY
    for _ in range(3):
        clock.advance(DAY)
        cookie = s.renew(cookie)[0]

    # when
    clock.advance(2 * DAY)
    shortened = s.renew(cookie)
    clock.advance(2 * DAY + 1)
    after_cap = s.renew(shortened[0])

    # then
    assert shortened[1] == 2 * DAY
    assert payload_of(shortened[0])['exp'] == cap
    assert after_cap is None


def test_an_eight_hour_cookie_without_login_and_iat_counts_from_its_expiry():
    # given
    clock = Clock()
    s = sessions(clock)
    issued_at = int(clock.now)
    old = s.seal({'name': 'alice@example.com', 'op': True}, 8 * 3600, SESSION_KIND)
    clock.advance(6 * 60)

    # when
    cookie, seconds = s.renew(old)

    # then
    assert payload_of(cookie)['login'] == issued_at
    assert payload_of(cookie)['exp'] == issued_at + 6 * 60 + 3 * DAY
    assert seconds == 3 * DAY


def test_generated_key_is_shared_and_survives_restart():
    # given
    db = mongomock.MongoClient(tz_aware=True)['skopax']
    keyless = settings()

    # when
    first = session_secret(db, keyless)
    second = session_secret(db, keyless)
    cookie = Sessions(first, DAY, DAY).issue('alice@example.com', 'admin', 'local')

    # then
    assert first == second
    assert len(first) == 32
    assert Sessions(second, DAY, DAY).unseal(cookie, SESSION_KIND)['name'] == 'alice@example.com'
    assert db['meta'].count_documents({'_id': 'secret-key'}) == 1


def test_env_key_wins_over_stored():
    # given
    db = mongomock.MongoClient(tz_aware=True)['skopax']
    stored = session_secret(db, settings())

    # when
    keyed = session_secret(db, dataclasses.replace(settings(), secret_key='from-env'))

    # then
    assert keyed == derive_secret('from-env')
    assert keyed != stored


def test_role_order_and_user_flags():
    # given
    users = {r: User('alice@example.com', r) for r in ('viewer', 'operator', 'admin')}

    # then
    assert role_at_least('operator', 'viewer') and role_at_least('operator', 'operator')
    assert not role_at_least('viewer', 'operator') and not role_at_least(None, 'viewer') and not role_at_least('root', 'viewer')
    assert [users[r].operator for r in users] == [False, True, True]
    assert [users[r].admin for r in users] == [False, False, True]
