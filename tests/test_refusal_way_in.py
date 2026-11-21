import dataclasses

import pytest

from tests.conftest import settings
from tests.fake_slack import fake_slack
from tests.test_operators import NOW
from tests.test_slack_clicks import action_ask, action_payload, drafted_ask, ephemerals, payload
from tests.test_slack_layout import known_pick, menu_ask
from tests.test_slackevents import card_with_post
from skopax import locales, messages
from skopax.app import create_app
from skopax.messages import msg
from skopax.operators import Operators
from skopax.slack import PROPOSAL_CONFIRM
from skopax.slackevents import Clicks, way_in
from skopax.store import Store

BASE = 'https://skopax.example.com'
LANGS = ('de', 'es', 'fr', 'tr')
WAY_IN = ('slackevents.click.sign_in', 'slackevents.click.sign_in_bare', 'slackevents.click.ask_admin', 'slackevents.click.ask_add', 'slackevents.click.sign_in_then_ask',
          'slackevents.click.sign_in_then_ask_bare', 'slackevents.click.ask_access')
OPERATOR_DEFAULT = dataclasses.replace(settings(), sso_default_role='operator')
THEN_ASK = 'Sign in to Skopax once, then ask an admin for operator access'


def boxed(store: Store, hint: str):
    slack, _, api = fake_slack(emails={'U9': 'x@example.com'})
    box = Clicks(store, slack, Operators(lambda email: False, slack, settings(), lambda: NOW), lambda action: 'done', way_in=lambda user: hint, background=lambda job: job())
    return box, api


def proposal_click(user: str = 'U9') -> dict:
    return {'type': 'block_actions', 'user': {'id': user}, 'channel': {'id': 'C1'}, 'message': {'ts': '1792224000.000300', 'thread_ts': '1792224000.000100', 'text': 'x', 'blocks': []},
            'actions': [{'action_id': PROPOSAL_CONFIRM, 'value': 'run-1'}]}


def test_the_way_in_fits_how_someone_can_get_operator_access():
    # when
    got = [way_in(True, False, BASE), way_in(True, False, ''), way_in(True, True, BASE), way_in(False, True, BASE), way_in(False, False, BASE),
           way_in(True, False, BASE, added_only=True), way_in(True, False, BASE, viewer_default=True), way_in(True, False, '', viewer_default=True),
           way_in(True, True, BASE, added_only=True, viewer_default=True), way_in(True, False, BASE, mapped=True), way_in(True, True, BASE, mapped=True),
           way_in(False, False, BASE, mapped=True), way_in(True, False, BASE, closed=True), way_in(True, True, BASE, closed=True)]

    # then
    assert got == ['Sign in to Skopax once to get access: ' + BASE, 'Sign in to Skopax once to get access.', 'Ask an admin for operator access.', 'Ask an admin for operator access.', '',
                   'Ask an admin to add you to Skopax.', THEN_ASK + ': ' + BASE, THEN_ASK + '.', 'Ask an admin for operator access.',
                   'Ask an admin for access.', 'Ask an admin for access.', '', '', '']


@pytest.mark.parametrize(('make', 'refused'), [
    (lambda store: (payload(drafted_ask(store)['id'], user='U9')), 'slackevents.click.non_operator'),
    (lambda store: (action_payload(action_ask(store)['id'], user='U9')), 'slackevents.click.action_non_operator'),
    (lambda store: proposal_click(), 'proposals.non_operator'),
    (lambda store: known_pick(menu_ask(store)['id'], user='U9'), 'slackevents.click.action_non_operator'),
])
def test_every_refused_click_adds_the_way_in(store: Store, make, refused):
    # given
    card_with_post(store)
    hint = msg('slackevents.click.sign_in', url=BASE)
    box, api = boxed(store, hint)

    # when
    got = box.handle(make(store))

    # then
    assert got == 'not-operator'
    assert ephemerals(api) == [f'{msg(refused)} {hint}']


def test_a_refused_click_without_a_way_in_keeps_the_plain_line(store: Store):
    # given
    card_with_post(store)
    box, api = boxed(store, '')

    # when
    got = box.handle(action_payload(action_ask(store)['id'], user='U9'))

    # then
    assert got == 'not-operator' and ephemerals(api) == [msg('slackevents.click.action_non_operator')]


def refused_in_app(store: Store, configured, viewer: bool = False, fields: dict | None = None, resolvers=None) -> list[str]:
    slack, _, api = fake_slack(emails={'U1': 'vera@example.com'})
    card_with_post(store)
    act = action_ask(store)
    app = create_app(configured, store, slack=slack)
    if viewer:
        app.state.signin.users.add_sso('vera@example.com', 'Vera', 'viewer')
    if fields:
        app.state.signin.users.update('vera@example.com', fields)
    if resolvers is not None:
        app.state.runtime.role_resolvers_named = lambda: resolvers
    assert app.state.clicks.handle(action_payload(act['id'], user='U1')) == 'not-operator'
    return ephemerals(api)


def test_someone_with_no_account_is_told_to_sign_in_once_at_the_base_url(store: Store):
    # when
    said = refused_in_app(store, OPERATOR_DEFAULT)

    # then
    assert said == [msg('slackevents.click.action_non_operator') + ' Sign in to Skopax once to get access: ' + BASE]


def test_a_viewer_is_told_to_ask_an_admin(store: Store):
    # when
    said = refused_in_app(store, settings(), viewer=True)

    # then
    assert said == [msg('slackevents.click.action_non_operator') + ' Ask an admin for operator access.']


def test_with_no_known_base_url_the_sign_in_line_has_no_link(store: Store):
    # when
    said = refused_in_app(store, dataclasses.replace(OPERATOR_DEFAULT, base_url='', sso_redirect_uri=''))

    # then
    assert said == [msg('slackevents.click.action_non_operator') + ' Sign in to Skopax once to get access.']


def test_with_viewer_as_the_sign_in_default_someone_with_no_account_is_told_to_sign_in_then_ask_an_admin(store: Store):
    # when
    said = refused_in_app(store, dataclasses.replace(settings(), sso_default_role='viewer'))

    # then
    assert said == [msg('slackevents.click.action_non_operator') + ' ' + THEN_ASK + ': ' + BASE]


def test_when_only_added_people_may_sign_in_someone_with_no_account_is_told_to_ask_an_admin_to_add_them(store: Store):
    # when
    said = refused_in_app(store, dataclasses.replace(OPERATOR_DEFAULT, sso_new_accounts='added-only'))

    # then
    assert said == [msg('slackevents.click.action_non_operator') + ' Ask an admin to add you to Skopax.']


def test_with_a_role_map_someone_with_no_account_is_only_told_to_ask_an_admin_for_access(store: Store):
    # when
    said = refused_in_app(store, dataclasses.replace(OPERATOR_DEFAULT, sso_role_map='[{"value": "ops", "role": "operator"}]'))

    # then
    assert said == [msg('slackevents.click.action_non_operator') + ' Ask an admin for access.']


def test_with_a_role_resolver_a_viewer_is_only_told_to_ask_an_admin_for_access(store: Store):
    # when
    said = refused_in_app(store, OPERATOR_DEFAULT, viewer=True, resolvers=[('directory', lambda user, channel: None)])

    # then
    assert said == [msg('slackevents.click.action_non_operator') + ' Ask an admin for access.']


def test_a_viewer_whose_role_came_from_the_sign_in_provider_is_only_told_to_ask_an_admin_for_access(store: Store):
    # when
    said = refused_in_app(store, OPERATOR_DEFAULT, viewer=True, fields={'roleFrom': 'claim'})

    # then
    assert said == [msg('slackevents.click.action_non_operator') + ' Ask an admin for access.']


def test_someone_outside_the_allowed_email_domains_gets_the_plain_line(store: Store):
    # when
    said = refused_in_app(store, dataclasses.replace(OPERATOR_DEFAULT, sso_allowed_domains=('shop.example.com',)))

    # then
    assert said == [msg('slackevents.click.action_non_operator')]


def test_a_disabled_account_gets_the_plain_line(store: Store):
    # when
    said = refused_in_app(store, OPERATOR_DEFAULT, viewer=True, fields={'disabled': True})

    # then
    assert said == [msg('slackevents.click.action_non_operator')]


def test_without_single_sign_on_someone_with_no_account_gets_the_plain_line(store: Store):
    # when
    said = refused_in_app(store, dataclasses.replace(settings(), sso_on=False))

    # then
    assert said == [msg('slackevents.click.action_non_operator')]


@pytest.mark.parametrize('lang', LANGS)
def test_every_pack_says_the_way_in(lang):
    # given
    pack = locales.read(lang)
    messages.set_catalogs([(lang, dict(pack.messages))])

    # when
    with messages.speaking(lang):
        said = [msg(key, url=BASE) if key.endswith(('sign_in', 'then_ask')) else msg(key) for key in WAY_IN]

    # then
    assert all(key in pack.messages for key in WAY_IN)
    assert BASE in said[0] and BASE in said[4] and all('Skopax' in said[n] for n in (0, 1, 3, 4, 5)) and BASE not in said[1] + said[5] + said[6]
    assert said == [pack.messages[key].format(product='Skopax', url=BASE) for key in WAY_IN]
