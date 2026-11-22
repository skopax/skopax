import logging

from tests.conftest import AUTH, settings
from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.investigator.web import DOMAINS, built_in, own_domains, web_from


def test_an_empty_setting_gives_the_built_in_sites():
    # then
    assert web_from('', 3, 4).domains == DOMAINS


def test_a_listed_host_is_added_to_the_built_in_sites():
    # when
    web = web_from('example.com', 3, 4)

    # then
    assert web.domains == DOMAINS + ('example.com',)


def test_a_dash_host_removes_a_built_in_and_a_unknown_one_is_ignored_with_a_warning(caplog):
    # when
    with caplog.at_level(logging.WARNING):
        web = web_from('-blog.google, example.com, -nowhere.example.org', 3, 4)

    # then
    assert web.domains == tuple(d for d in DOMAINS if d != 'blog.google') + ('example.com',)
    assert 'left out' in caplog.text and '-nowhere.example.org' in caplog.text


def test_any_and_off_behave_as_before():
    # then
    assert web_from('any', 3, 4).domains is None
    assert web_from('off', 3, 4) is None and web_from('none', 3, 4) is None


def test_the_own_domains_join_the_built_in_list_first_and_can_be_dropped():
    # given
    own = own_domains(('Example.com', 'example.com', 'not a host'), ('shop.example.org',))

    # then
    assert own == ('example.com', 'shop.example.org')
    assert web_from('', 3, 4, own).domains == own + DOMAINS
    assert 'example.com' not in web_from('-example.com', 3, 4, own).domains
    assert web_from('', 3, 4).domains == DOMAINS and own_domains((), ()) == ()


def test_admin_exposes_the_built_in_list_with_the_own_domains(store):
    # given
    import dataclasses
    s = dataclasses.replace(settings(), own_sites=('example.com',))

    # when
    with TestClient(create_app(s, store)) as client:
        items = client.get('/api/admin/settings', headers=AUTH).json()['items']

    # then
    entry = next(i for i in items if i['key'] == 'investigate.web-domains')
    assert entry['builtIn'] == list(built_in(('example.com',))) and entry['ownDomains'] == ['example.com']
    assert entry['builtIn'][0] == 'example.com' and 'Your sites are added to the built-in list' in entry['help']


def test_a_host_both_added_and_removed_is_removed():
    # when
    web = web_from('example.com, -example.com, -kubernetes.io, kubernetes.io', 3, 4)

    # then
    assert 'example.com' not in web.domains and 'kubernetes.io' not in web.domains


def test_the_allowed_list_is_cut_at_a_hundred_hosts_and_the_cut_is_logged_once(caplog):
    # given
    many = ', '.join(f'h{i}.example.com' for i in range(150))

    # when
    with caplog.at_level(logging.WARNING):
        first = web_from(many, 3, 4)
        second = web_from(many, 3, 4)

    # then
    assert len(first.domains) == 100 and first.domains[:len(DOMAINS)] == DOMAINS and first == second
    assert caplog.text.count('allowed sites') == 1


def test_a_dash_host_that_the_same_value_also_adds_gives_no_unknown_warning(caplog):
    # when
    with caplog.at_level(logging.WARNING):
        web = web_from('example.org, -example.org', 3, 4)
        other = web_from('-nowhere.example.net', 3, 4)

    # then
    assert 'example.org' not in web.domains and other.domains == DOMAINS
    assert '-example.org' not in caplog.text and '-nowhere.example.net' in caplog.text
