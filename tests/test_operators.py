import dataclasses

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, settings
from tests.fake_slack import fake_slack
from tests.test_slack_clicks import action_ask, action_payload, ephemerals
from tests.test_slackevents import card_with_post
from skopax.app import create_app
from skopax.operators import OperatorUnknown, Operators
from skopax.messages import msg
from skopax.store import Store

NOW = 1_792_224_000.0


class Clock:
    def __init__(self, now: float = NOW):
        self.now = now

    def __call__(self) -> float:
        return self.now


class Lookup:
    def __init__(self, allowed=(), fail: bool = False):
        self.allowed = set(allowed)
        self.fail = fail
        self.calls: list[str] = []

    def __call__(self, email: str) -> bool:
        self.calls.append(email)
        if self.fail:
            raise ConnectionError('users-service answered HTTP 503')
        return email in self.allowed


def operators(lookup, emails=None, fail=None, clock=None, cache_s: int = 600):
    slack, _, api = fake_slack(emails={'U1': 'adam.baker@example.com'} if emails is None else emails, fail=fail)
    return Operators(lookup, slack, dataclasses.replace(settings(), slack_operator_cache_s=cache_s), clock or Clock()), api


def test_any_account_counts_and_the_principal_is_the_login():
    # given
    roles = Lookup({'adam.baker@example.com', 'nina@example.com'})
    ops, _ = operators(roles, emails={'U1': 'Adam.Baker@example.com', 'U2': 'nina@example.com', 'U3': 'nobody@example.com', 'U4': None})

    # when
    got = [ops.login_for(u) for u in ('U1', 'U2', 'U3', 'U4')]

    # then
    assert got == ['adam.baker@example.com', 'nina@example.com', None, None], 'an account in auth is enough; no account or no email stays out'
    assert roles.calls == ['Adam.Baker@example.com', 'adam.baker@example.com', 'nina@example.com', 'nobody@example.com']


def test_the_verdict_and_the_email_are_cached_and_expire():
    # given
    clock = Clock()
    roles = Lookup({'adam.baker@example.com'})
    ops, api = operators(roles, clock=clock)

    # when
    ops.login_for('U1')
    ops.login_for('U1')
    clock.now += 601
    ops.login_for('U1')
    clock.now += 3000
    ops.login_for('U1')

    # then
    assert len(roles.calls) == 3
    assert api.methods().count('users.info') == 2


def test_a_users_info_failure_or_a_down_lookup_is_unknown_and_not_cached():
    # given
    roles = Lookup({'adam.baker@example.com'}, fail=True)
    ops, api = operators(roles, fail={'users.info'})

    # when
    with pytest.raises(OperatorUnknown):
        ops.login_for('U1')
    api.fail.clear()
    with pytest.raises(OperatorUnknown):
        ops.login_for('U1')
    roles.fail = False

    # then
    assert ops.login_for('U1') == 'adam.baker@example.com'


def test_without_a_role_lookup_nobody_is_an_operator():
    # given
    ops, api = operators(None)

    # then
    assert not ops.enabled
    assert ops.login_for('U1') is None and 'users.info' not in api.methods()


def test_the_check_endpoint_says_every_account_can_act(store: Store):
    # given
    roles = Lookup({'adam.baker@example.com', 'mark.young@example.com'})
    client = TestClient(create_app(settings(), store, operators_lookup=roles))

    # when
    yes = client.get('/api/operators/check', params={'email': 'adam.baker@example.com'}, headers=AUTH)
    member = client.get('/api/operators/check', params={'email': 'mark.young@example.com'}, headers=AUTH)
    no = client.get('/api/operators/check', params={'email': 'x@example.com'}, headers=AUTH)
    roles.fail = True
    down = client.get('/api/operators/check', params={'email': 'y@example.com'}, headers=AUTH)
    guest = client.get('/api/operators/check', params={'email': 'adam.baker@example.com'})

    # then
    assert yes.json() == {'email': 'adam.baker@example.com', 'operator': True, 'access': True, 'principal': 'adam.baker@example.com'}
    assert member.json() == {'email': 'mark.young@example.com', 'operator': True, 'access': True, 'principal': 'mark.young@example.com'}
    assert no.json() == {'email': 'x@example.com', 'operator': False, 'access': False, 'principal': None}
    assert (down.status_code, down.json()['detail']) == (503, 'role lookup failed: ConnectionError')
    assert guest.status_code == 401


def test_an_admin_counts_as_operator_for_slack_clicks():
    # given
    roles = Lookup({'adam.baker@example.com'})
    ops, _ = operators(roles)

    # when
    got = ops.login_for('U1')

    # then
    assert got == 'adam.baker@example.com'


def test_slack_actions_refused_for_someone_the_users_store_does_not_know(store: Store):
    # given
    slack, _, api = fake_slack(emails={'U1': 'alice@example.com'})
    card_with_post(store)
    act = action_ask(store)
    app = create_app(settings(), store, slack=slack)

    # when
    access = app.state.operators.access_for('U1')
    got = app.state.clicks.handle(action_payload(act['id'], user='U1'))

    # then
    assert access == (None, False)
    assert got == 'not-operator' and ephemerals(api) == [msg('slackevents.click.action_non_operator') + ' ' + msg('slackevents.click.sign_in_then_ask', url='https://skopax.example.com')], 'an unknown account is refused, it never lets everyone act'
    assert 'cardActionApplied' not in store.get_action(act['id'])
