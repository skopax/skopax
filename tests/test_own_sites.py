import dataclasses
import logging

import pytest
from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.config import load_settings
from skopax.investigator import Investigator
from skopax.investigator.tools import Effects
from skopax.investigator.web import DOMAINS, built_in, own_sites_of, web_from
from skopax.messages import msg
from skopax.plugins import website
from skopax.registry import Registry
from tests.conftest import AUTH, settings
from tests.fake_anthropic import FakeAnthropic
from tests.plugin_kit import add_instance
from tests.test_investigator_tools import sources

KEY = 'core.own-sites'
WEB = 'investigate.web-domains'


def entry(client, key):
    return next(i for i in client.get('/api/admin/settings', headers=AUTH).json()['items'] if i['key'] == key)


def admin(store, registry=None, **over):
    return TestClient(create_app(dataclasses.replace(settings(), **over), store, registry=registry or Registry.of(website.PLUGIN)))


def inv(store, **over):
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', **over)
    return Investigator(store, sources(), Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}), s, client=FakeAnthropic([]), clock=lambda: 0)


def test_own_sites_come_from_the_setting():
    # when
    found = own_sites_of(('Example.com', 'example.com', 'shop.example.org', 'not a host'), [{'url': 'https://fallback.example.net'}])

    # then
    assert found == (('example.com', 'shop.example.org'), 'setting')


def test_an_empty_setting_and_no_website_instance_gives_no_own_sites():
    # then
    assert own_sites_of((), []) == ((), 'none')


def test_a_website_instance_gives_the_own_sites_when_the_setting_is_empty():
    # given
    instances = [{'url': 'https://shop.example.com/', 'hosts': 'shop.example.net, Shop.Example.com', 'login-hosts': 'login.example.net'},
                 {'url': 'https://www.example.org'}, {'hosts': 'only-hosts.example.org'}, {'url': ''}]

    # when
    found = own_sites_of((), instances)

    # then
    assert found == (('shop.example.com', 'shop.example.net', 'www.example.org', 'only-hosts.example.org'), 'website')


def test_a_login_host_of_the_website_instance_is_not_an_own_site():
    # then
    assert own_sites_of((), [{'url': 'https://shop.example.com', 'hosts': 'login.example.com, blog.example.com', 'login-hosts': 'login.example.com'}]) == (
        ('shop.example.com', 'blog.example.com'), 'website')


def test_the_setting_wins_over_the_website_instance():
    # then
    assert own_sites_of(('example.com',), [{'url': 'https://shop.example.com'}]) == (('example.com',), 'setting')


def test_the_own_sites_lead_the_built_in_list_and_can_be_dropped():
    # given
    own = ('example.com',)

    # then
    assert web_from('', 3, 4, own).domains == own + DOMAINS
    assert 'example.com' not in web_from('-example.com', 3, 4, own).domains
    assert built_in(own)[0] == 'example.com'


def test_email_domains_alone_no_longer_give_own_sites(store):
    # given
    with admin(store, email_domains=('example.com',)) as client:
        # when
        row, web = entry(client, KEY), entry(client, WEB)

    # then
    assert row['ownFrom'] == 'none' and row['ownHosts'] == []
    assert web['ownDomains'] == [] and web['builtIn'] == list(DOMAINS)
    assert inv(store, email_domains=('example.com',)).own_domains() == ()


def test_the_setting_gives_the_own_sites_to_the_admin_lists(store):
    # given
    with admin(store, own_sites=('example.com',), email_domains=('example.org',)) as client:
        # when
        row, web = entry(client, KEY), entry(client, WEB)

    # then
    assert row['ownFrom'] == 'setting' and row['ownHosts'] == ['example.com'] and row['value'] == 'example.com'
    assert web['ownDomains'] == ['example.com'] and web['builtIn'][0] == 'example.com' and web['builtIn'][1:] == list(DOMAINS)


def test_the_admin_row_names_the_website_integration_when_it_gives_the_sites(store):
    # given
    add_instance(store, website.PLUGIN, 'main', sealer_key='k', url='https://shop.example.com', hosts='shop.example.net')
    with admin(store) as client:
        # when
        row, web = entry(client, KEY), entry(client, WEB)

        # then
        assert row['ownFrom'] == 'website' and row['ownHosts'] == ['shop.example.com', 'shop.example.net'] and row['value'] == ''
        assert web['ownDomains'] == ['shop.example.com', 'shop.example.net']

        # when
        saved = client.put('/api/admin/settings', json={'key': KEY, 'value': 'example.com'}, headers=AUTH)

        # then
        assert saved.status_code == 200
        assert (entry(client, KEY)['ownFrom'], entry(client, KEY)['ownHosts']) == ('setting', ['example.com'])
        assert entry(client, WEB)['ownDomains'] == ['example.com']


def test_a_disabled_website_instance_gives_no_own_sites(store):
    # given
    add_instance(store, website.PLUGIN, 'main', sealer_key='k', enabled=False, url='https://shop.example.com')
    with admin(store) as client:
        # then
        assert entry(client, KEY)['ownFrom'] == 'none'


def test_the_investigator_allow_list_reads_the_own_sites(store):
    # given
    box = inv(store, own_sites=('example.com', 'shop.example.org'), email_domains=('example.net',))

    # when
    own = box.own_domains()

    # then
    assert own == ('example.com', 'shop.example.org')
    assert web_from('', 3, 4, own).domains == own + DOMAINS


def test_the_investigator_asks_the_live_source_when_the_app_gives_one(store):
    # given
    live = {'sites': ('example.com',)}
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test')
    box = Investigator(store, sources(own_sites=lambda: live['sites']), Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}), s, client=FakeAnthropic([]), clock=lambda: 0)

    # when
    first = box.own_domains()
    live['sites'] = ('shop.example.org',)

    # then
    assert (first, box.own_domains()) == (('example.com',), ('shop.example.org',))


def test_the_env_name_sets_the_own_sites_with_commas_or_spaces():
    # when
    s = load_settings(lambda: {}, env={'SKOPAX_OWN_SITES': ' Example.com, shop.example.org  example.net,,'})

    # then
    assert s.own_sites == ('example.com', 'shop.example.org', 'example.net')
    assert load_settings(lambda: {}, env={}).own_sites == ()


@pytest.mark.parametrize('given', ['https://example.com', 'example.com/shop', 'example.com:8443', 'localhost', 'not a host_'])
def test_a_bad_entry_in_the_env_is_dropped_with_a_warning_and_the_valid_ones_stay(given, caplog):
    # when
    with caplog.at_level(logging.WARNING):
        s = load_settings(lambda: {}, env={'SKOPAX_OWN_SITES': f'example.com {given} shop.example.org'})

    # then
    assert s.own_sites == ('example.com', 'shop.example.org')
    assert 'SKOPAX_OWN_SITES' in caplog.text and given.split()[0] in caplog.text


@pytest.mark.parametrize('given', ['https://example.com', 'example.com/shop', 'example.com:8443', 'localhost', 'example.com, -shop.example.org', 'any', 'off'])
def test_the_admin_refuses_anything_but_plain_host_names(store, given):
    # given
    with admin(store) as client:
        # when
        saved = client.put('/api/admin/settings', json={'key': KEY, 'value': given}, headers=AUTH)

    # then
    assert saved.status_code == 422
    assert msg('items.why.own-site') in saved.text


def test_the_admin_takes_host_names_separated_by_commas_or_spaces(store):
    # given
    with admin(store) as client:
        # when
        saved = client.put('/api/admin/settings', json={'key': KEY, 'value': 'example.com shop.example.org, example.net'}, headers=AUTH)

        # then
        assert saved.status_code == 200
        assert entry(client, KEY)['ownHosts'] == ['example.com', 'shop.example.org', 'example.net']


def test_the_admin_row_carries_the_chip_editor_rules_and_the_help(store):
    # given
    with admin(store) as client:
        # when
        row = entry(client, KEY)

    # then
    assert row['label'] == 'Your websites' and row['items']['kind'] == 'own-site' and 'minus' not in row['items'] and 'keywords' not in row['items']
    assert row['help'].startswith('The sites of your company the investigator may read and search, such as example.com')
    assert row['group'] == 'Investigator'
