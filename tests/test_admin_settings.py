import dataclasses
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, settings, state
from tests.test_auth import ROLE, TokenEndpoint, access_token, app_client, log_in
from skopax.app import create_app
from skopax.kube import Kube
from skopax import clock, tags
from skopax.store import Store
from skopax.tunables import Invalid, Live, TunableStore, View, apply, parse, registry

ADMIN = 'SKOPAX_ADMIN'


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def known(key: str):
    return {t.key: t for t in registry()}[key]


def test_values_are_checked_against_their_type_range_and_choices():
    # given
    base = settings()

    # when
    errors = []
    for key, raw in (('investigate.daily-usd', '-1'), ('investigate.rooms', '9'), ('investigate.rooms', '2.5'), ('investigate.model', 'gpt-5'),
                     ('investigate.strict-tools', 'yes')):
        with pytest.raises(Invalid) as e:
            parse(known(key), raw, base)
        errors.append(str(e.value))

    # then
    assert errors[0] == 'Daily limit ($) must be between 0 and 500'
    assert errors[2] == 'Parallel rooms must be a whole number'
    assert errors[3].startswith('Card model must be one of claude-opus-5-5')
    assert parse(known('investigate.daily-usd'), '12.5', base) == 12.5 and parse(known('investigate.strict-tools'), 'true', base) is True
    assert parse(known('tags.seo.rules'), ' type:page-typos , gone ', base) == ('type:page-typos', 'gone')


def test_overrides_reach_plain_fields_business_tag_rules_and_page_routes_and_a_bad_stored_value_is_skipped():
    # given
    base = dataclasses.replace(settings(), tag_rules=tags.RULES)

    # when
    now = apply(base, {'investigate.model': 'claude-sonnet-5-5', 'investigate.daily-usd': '40', 'tags.seo.rules': 'type:page-typos', 'investigate.rooms': '99', 'gone.key': 'x'})

    # then
    assert (now.investigate_model, now.investigate_daily_usd, now.investigate_rooms) == ('claude-sonnet-5-5', 40.0, base.investigate_rooms)
    assert dict(now.tag_rules)['seo'] == ('type:page-typos',) and dict(now.tag_rules)['marketing'] == dict(tags.RULES)['marketing']
    assert base.investigate_model != now.investigate_model


def test_another_pod_sees_a_change_after_its_refresh_window(store: Store):
    # given
    clock = Clock()
    here, there = Live(settings(), TunableStore(store.db), every=15, clock=clock), Live(settings(), TunableStore(store.db), every=15, clock=clock)
    view = View(there)
    before = view.investigate_model

    # when
    here.set('investigate.model', 'claude-sonnet-5-5', 'oscar.green@example.com')
    clock.now += 10
    soon = view.investigate_model
    clock.now += 6
    later = view.investigate_model

    # then
    assert (before, soon, later) == ('claude-opus-5-5', 'claude-opus-5-5', 'claude-sonnet-5-5')
    with pytest.raises(AttributeError):
        view.investigate_model = 'x'


def test_only_admins_and_the_token_read_and_change_settings_and_admin_counts_as_operator(store: Store):
    # given
    store.save(state(), None)
    guest = app_client(store)
    operator = app_client(store, TokenEndpoint(access_token([ROLE])))
    admin = app_client(store, TokenEndpoint(access_token([ADMIN], sub='admin.user@example.com')))
    log_in(operator)
    log_in(admin)

    # when
    codes = [guest.get('/api/admin/settings').status_code, operator.get('/api/admin/settings').status_code, operator.put('/api/admin/settings', json={'key': 'investigate.rooms', 'value': '3'}).status_code]
    me = admin.get('/api/me').json()
    listed = admin.get('/api/admin/settings')
    queued = admin.post('/api/actions', json={'type': 'dive', 'key': 'k'})
    by_token = guest.get('/api/admin/settings', headers=AUTH)

    # then
    assert codes == [401, 403, 403]
    assert operator.get('/api/admin/settings').json()['detail'] == 'This needs the admin role.'
    assert (me['admin'], me['operator'], me['userRole']) == (True, True, 'admin')
    assert listed.status_code == 200 and by_token.status_code == 200 and queued.status_code == 201
    items = {i['key']: i for i in listed.json()['items']}
    assert items['investigate.model'] == {'key': 'investigate.model', 'group': 'Investigator', 'label': 'Card model', 'help': items['investigate.model']['help'], 'kind': 'choice', 'live': True,
                                          'value': 'claude-opus-5-5', 'base': 'claude-opus-5-5', 'default': 'claude-opus-5-5', 'source': 'default', 'by': None, 'at': None, 'card': 'Models', 'order': 100,
                                          'choices': ['claude-opus-5-5', 'claude-opus-5', 'claude-opus-4-8', 'claude-sonnet-5-5', 'claude-haiku-5-5']}
    assert items['slack.remind-hours']['live'] is False
    endpoints = {'sso.authorize-url', 'sso.token-url', 'sso.jwks-url'}
    assert {k for k, i in items.items() if i['group'] == 'Sign-in' and k.endswith('-url')} == endpoints and all(items[k]['kind'] == 'text' for k in endpoints)
    assert not any('secret' in k or 'token' in k or 'webhook' in k or k.endswith('.url') for k in items if k not in endpoints)


def test_the_last_known_role_setting_belongs_to_the_sign_in_group():
    # given
    found = known('roles.stale-max-minutes')

    # when
    group = found.group

    # then
    assert group == 'Sign-in'


def test_an_admin_change_is_live_at_once_kept_in_history_and_reverted(store: Store):
    # given
    store.save(state(), None)
    admin = app_client(store, TokenEndpoint(access_token([ADMIN])))
    log_in(admin)

    # when
    first = admin.put('/api/admin/settings', json={'key': 'investigate.model', 'value': 'claude-sonnet-5-5'})
    info = admin.get('/api/server').json()['investigate']
    capped = admin.put('/api/admin/settings', json={'key': 'investigate.daily-usd', 'value': '0'})
    bad = admin.put('/api/admin/settings', json={'key': 'investigate.daily-usd', 'value': 'lots'})
    unknown = admin.put('/api/admin/settings', json={'key': 'mongo.uri', 'value': 'mongodb://x'})
    reset = admin.delete('/api/admin/settings', params={'key': 'investigate.model'})
    history = admin.get('/api/admin/history').json()
    after = admin.get('/api/server').json()['investigate']

    # then
    assert first.status_code == 200 and first.json()['item']['source'] == 'admin' and first.json()['item']['by'] == 'oscar.green@example.com'
    assert info['model'] == 'claude-sonnet-5-5' and after['model'] == 'claude-opus-5-5' and after['cap'] == 0.0
    assert capped.json()['change']['before'] == '25' and capped.json()['change']['after'] == '0'
    assert (bad.status_code, unknown.status_code) == (422, 404) and bad.json()['detail'] == 'Daily limit ($) must be a number'
    assert reset.json()['item']['source'] == 'default'
    assert [(h['key'], h['old'], h['new']) for h in history] == [('investigate.model', 'claude-sonnet-5-5', None),
                                                                 ('investigate.daily-usd', None, '0'), ('investigate.model', None, 'claude-sonnet-5-5')]
    lines = [a['text'] for a in store.list_activity(None, 50) if a['kind'] == 'admin']
    assert lines[-1] == 'Card model (investigate.model): claude-opus-5-5 → claude-sonnet-5-5'


def test_a_restart_setting_says_so(store: Store, tmp_path):
    # given
    store.save({**state(), 'initialized': True}, None)

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path))) as c:
        restart = c.put('/api/admin/settings', json={'key': 'slack.remind-hours', 'value': '3'}, headers=AUTH).json()

    # then
    assert restart['item']['live'] is False
    assert [a['text'] for a in store.list_activity(None, 10) if a['kind'] == 'admin'][0].endswith('· applies after a restart')


def test_timezone_setting_rejects_unknown_zone(store: Store):
    # given
    store.save(state(), None)
    admin = app_client(store, TokenEndpoint(access_token([ADMIN])))
    log_in(admin)

    # when
    bad = admin.put('/api/admin/settings', json={'key': 'general.timezone', 'value': 'Mars/Olympus'})
    good = admin.put('/api/admin/settings', json={'key': 'general.timezone', 'value': 'Europe/Berlin'})

    # then
    assert (bad.status_code, bad.json()['detail']) == (422, 'Timezone must be an IANA timezone name, such as UTC or Europe/Berlin')
    assert 'Mars/Olympus' not in bad.text, 'the 422 names the field and the rule, never the submitted value'
    assert good.status_code == 200 and good.json()['item']['value'] == 'Europe/Berlin'
    assert clock.label(1_784_000_000) == 'CEST'


def test_the_tunable_store_builds_without_touching_mongo_and_makes_its_index_on_request():
    # given
    from unittest.mock import MagicMock
    from pymongo.errors import ServerSelectionTimeoutError
    db = MagicMock()
    db['tunable_changes'].create_index.side_effect = ServerSelectionTimeoutError('down')

    # when
    tunables = TunableStore(db)

    # then
    db['tunable_changes'].create_index.assert_not_called()
    with pytest.raises(ServerSelectionTimeoutError):
        tunables.ensure_indexes()


OLD_TAB = re.compile(r'Admin\s+(?:\u203a|\\u203[aA]|&rsaquo;|&#8250;)\s+General\b')
PACKAGE = Path(__file__).resolve().parent.parent / 'skopax'


def test_no_text_points_at_the_admin_tab_by_its_old_name():
    # given
    files = [p for p in sorted(PACKAGE.rglob('*')) if p.is_file() and p.suffix in ('.py', '.js', '.html', '.md', '.json')]

    # when
    hits = [f'{p.relative_to(PACKAGE)}:{n}' for p in files for n, line in enumerate(p.read_text(encoding='utf-8').splitlines(), 1) if OLD_TAB.search(line)]

    # then
    assert files and hits == []


def test_no_text_calls_the_chat_page_a_tab():
    # given
    files = [p for p in sorted(PACKAGE.rglob('*')) if p.is_file() and p.suffix in ('.py', '.js', '.html', '.md', '.json')]

    # when
    hits = [f'{p.relative_to(PACKAGE)}:{n}' for p in files for n, line in enumerate(p.read_text(encoding='utf-8').splitlines(), 1) if re.search(r'\bChat tab\b', line, re.IGNORECASE)]

    # then
    assert files and hits == []


def test_the_old_tab_name_matcher_finds_every_way_of_writing_it():
    # given
    written = ['Admin \u203a General \u203a Slack', 'on Admin \\u203a General.', 'Admin \\u203A General', 'Admin &rsaquo; General', 'Admin &#8250; General']
    kept = ['Admin \u203a Settings \u203a Slack', 'Admin \u203a Investigator', 'the admin General tab', 'Admin \u203a General2']

    # then
    assert [bool(OLD_TAB.search(t)) for t in written + kept] == [True] * len(written) + [False] * len(kept)
