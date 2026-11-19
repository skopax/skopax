import dataclasses

import pytest

from skopax.config import csv_of
from skopax.investigator.tools import allowed_link_hosts, draft_problem, link_host, link_problem
from tests.conftest import settings


def hosts_of(*entries: str) -> tuple[str, ...]:
    return allowed_link_hosts(dataclasses.replace(settings(), link_hosts=entries))


@pytest.mark.parametrize('url', ['https://example.com/a', 'https://example.com', 'http://EXAMPLE.com/a?x=1', 'https://example.com:8443/a', 'example.com/a'])
def test_an_exact_entry_allows_that_host_and_only_that_host(url):
    # then
    assert link_problem(url, hosts_of('=example.com')) is None


@pytest.mark.parametrize('url', ['https://sub.example.com/a', 'https://example.com.evil.test/a', 'https://evilexample.com/a', 'https://a.b.example.com/', 'https://example.com@evil.test/a',
                                 'https://evil.test/example.com', 'https://example.com./a'])
def test_an_exact_entry_rejects_subdomains_and_look_alikes(url):
    # then
    assert 'links may only point at' in (link_problem(url, hosts_of('=example.com')) or '')


@pytest.mark.parametrize('url, ok', [('https://example.com/a', True), ('https://sub.example.com/a', True), ('https://a.b.example.com/', True), ('https://evilexample.com/a', False),
                                     ('https://example.com.evil.test/a', False)])
def test_a_plain_entry_keeps_allowing_the_host_and_its_subdomains_as_before(url, ok):
    # then
    assert (link_problem(url, hosts_of('example.com')) is None) is ok


def test_entries_mix_and_a_plain_entry_for_the_same_host_keeps_its_wider_reach():
    # given
    both = hosts_of('=example.com', 'example.com')
    mixed = hosts_of('=example.com', 'corp.test')

    # then
    assert link_problem('https://sub.example.com/a', both) is None
    assert link_problem('https://sub.example.com/a', mixed) is not None and link_problem('https://x.corp.test/a', mixed) is None and link_problem('https://example.com/a', mixed) is None


def test_the_exact_form_is_read_from_the_setting_like_any_other_entry():
    # given
    entries = csv_of(' =example.com , corp.test, = spaced.example.org ')

    # then
    assert entries == ('=example.com', 'corp.test', '= spaced.example.org')
    assert hosts_of(*entries)[-3:] == ('=example.com', 'corp.test', '=spaced.example.org')
    assert link_host('=https://Example.com/path') == '=example.com' and link_host('=') == '' and link_host('=  ') == ''
    assert link_host('https://Example.com/path') == 'example.com' and link_host(' example.com ') == 'example.com'


def test_a_draft_that_links_a_subdomain_of_an_exact_entry_is_refused_with_the_entry_named_exactly():
    # given
    hosts = hosts_of('=example.com')

    # when
    problem = draft_problem('See https://sub.example.com/runbook', hosts)

    # then
    assert 'example.com (exactly)' in problem and 'sub.example.com is not one of them' in problem
    assert draft_problem('See https://example.com/runbook', hosts) is None


def test_the_default_message_for_plain_entries_is_unchanged():
    # then
    assert link_problem('https://evil.test/x', ('example.com', 'corp.test')) == 'links may only point at example.com, corp.test; evil.test is not one of them'


@pytest.mark.parametrize('dot', ['\u3002', '\uff0e', '\uff61'])
def test_an_exact_entry_rejects_a_bare_link_with_an_ideographic_dot(dot):
    # given
    hosts = hosts_of('=example.com')

    # then
    for text in (f'see sub.example{dot}com/x', f'see sub{dot}example{dot}com/x', f'see example{dot}com.evil.test/x'):
        assert 'links may only point at' in (link_problem(text, hosts) or ''), text
    assert link_problem(f'see example{dot}com/x', hosts) is None
