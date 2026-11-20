import dataclasses
import logging
import time

import pytest
from fastapi.testclient import TestClient

from skopax import brand
from skopax.app import create_app
from skopax.config import load_settings
from skopax.messages import msg
from skopax.registry import Registry
from skopax.remote import HttpError, NotJson, Reply, Unreachable
from skopax.store import Store
from skopax.versioncheck import LIST_URL, VersionCheck, jitter_of
from tests.conftest import AUTH, settings
from tests.fake_http import FakeHttp

LIST = LIST_URL.split('?')[0]
NOTICES = '/api/admin/notices'
RUNNING = '0.2.0'
PAGE = 'https://github.com/skopax/skopax/releases/tag/'
NOW = 1_800_000_000.0


def release(tag: str, body: str = 'Fixes.', **over) -> dict:
    return {'tag_name': tag, 'html_url': PAGE + tag, 'body': body, 'draft': False, 'prerelease': False, **over}


def github(*releases) -> FakeHttp:
    return FakeHttp({('GET', LIST): list(releases)})


def listing(listed) -> FakeHttp:
    return FakeHttp({('GET', LIST): listed})


def checker(store: Store, http, enabled=True, version: str = RUNNING) -> VersionCheck:
    return VersionCheck(store, http, lambda: enabled, version, lambda: NOW)


def ids(c: TestClient) -> list[str]:
    return [i['id'] for i in c.get(NOTICES, headers=AUTH).json()['items']]


def update_of(c: TestClient) -> dict:
    return next(i for i in c.get(NOTICES, headers=AUTH).json()['items'] if i['id'] == 'update')


def served(store: Store, http, **over) -> TestClient:
    return TestClient(create_app(dataclasses.replace(settings(), **over), store, transport=http, registry=Registry.of()), base_url='https://testserver')


@pytest.mark.parametrize('tag, newer', [('v0.2.1', True), ('v0.4.0', True), ('v1.0.0', True), ('v0.10.0', True), ('v0.2.0', False), ('v0.1.9', False)])
def test_a_release_is_newer_by_semantic_version(store: Store, tag: str, newer: bool):
    # given
    check = checker(store, github(release(tag)))

    # when
    stored = check.run_once()
    notice = check.notice()

    # then
    assert stored['latest'] == tag[1:] and (notice is not None) is newer


def test_the_latest_is_the_highest_version_and_not_the_newest_in_the_list(store: Store):
    # given
    check = checker(store, github(release('v0.2.9'), release('v0.10.0'), release('v0.4.0'), release('v0.9.9', prerelease=True), release('v9.0.0', draft=True)))

    # when
    stored = check.run_once()

    # then
    assert (stored['latest'], stored['url']) == ('0.10.0', PAGE + 'v0.10.0')


@pytest.mark.parametrize('entry', [release('v0.4.0', prerelease=True), release('v0.4.0', draft=True), release('0.4.0'), release('v0.3'), release('v0.4.0-rc.1'), release('v0.03.0'),
                                   release('v0.4.0', html_url='http://evil.example.com/x'), release('v0.4.0', html_url=None), {'tag_name': 3}, 'text', None])
def test_only_a_plain_published_release_counts(store: Store, entry):
    # given
    check = checker(store, github(entry))

    # when
    stored = check.run_once()

    # then
    assert stored is None and store.version_check() is None and check.notice() is None


@pytest.mark.parametrize('version', ['0.4.0rc1', '0.4.0-rc.1', 'dev', '', '1.2', '0.2.0+local'])
def test_a_development_or_unparseable_version_never_asks(store: Store, version: str):
    # given
    http = github(release('v9.9.9'))
    check = checker(store, http, version=version)

    # when
    stored, started, begun = check.run_once(), check.due_at_start(), check.start()

    # then
    assert (stored, started, begun, http.calls, check.notice()) == (None, False, None, [], None)


def test_one_request_asks_for_the_list_and_sends_nothing_but_the_agent_and_accept(store: Store):
    # given
    http = github(release('v0.4.0'), release('v0.2.5'))

    # when
    checker(store, http).run_once()

    # then
    assert [(m, u) for m, u, _, _ in http.calls] == [('GET', LIST_URL)]
    headers, body = http.calls[0][2], http.calls[0][3]
    assert headers['User-Agent'] == f'Skopax/{brand.display_version(RUNNING)}' and body is None
    assert set(headers) <= {'Accept', 'User-Agent', 'X-Skopax-Json-Read'}


SECURE = 'Notes.\nSecurity: fixes a login bug'


@pytest.mark.parametrize('listed, found', [
    ([release('v0.4.0'), release('v0.2.5', body=SECURE), release('v0.2.0')], ['0.2.5']),
    ([release('v0.4.0', body='Security: fixes it'), release('v0.2.5')], ['0.4.0']),
    ([release('v0.4.0', body='Security: a'), release('v0.2.5', body='Security: b'), release('v0.1.0', body='Security: older')], ['0.1.0', '0.2.5', '0.4.0']),
    ([release('v0.4.0', body='security: lower case'), release('v0.2.5', body='This is a Security: not at line start'), release('v0.2.4', body=' Security: indented')], []),
    ([release('v0.4.0'), release('v0.2.9', body='Security: pre-release', prerelease=True), release('v0.2.8', body='Security: draft', draft=True)], []),
    ([release('v0.4.0'), release('0.2.7', body='Security: bad tag'), release('v0.2.6-rc.1', body='Security: rc')], []),
    ([release('v0.4.0'), release('v0.2.1', body='Fixes.\r\nSecurity: windows line ends\r\n')], ['0.2.1']),
    ([release('v0.4.0'), release('v0.2.1', body=None), 'junk', None], []),
])
def test_every_security_release_in_the_window_is_stored(store: Store, listed, found):
    # given
    check = checker(store, listing(listed))

    # when
    check.run_once()

    # then
    assert store.version_check()['securityIn'] == found


@pytest.mark.parametrize('version, level', [('0.2.0', 'warn'), ('0.2.4', 'warn'), ('0.2.5', 'info'), ('0.2.6', 'info')])
def test_the_level_is_decided_by_the_pod_that_shows_the_notice(store: Store, version: str, level: str):
    # given
    http = github(release('v0.4.0'), release('v0.2.5', body=SECURE), release('v0.2.0'))
    checker(store, http, version='0.2.6').run_once()
    other = checker(store, http, version='0.2.0')
    other.run_once()

    # when
    notice = checker(store, FakeHttp(), version=version).notice()

    # then
    assert store.version_check()['securityIn'] == ['0.2.5']
    assert notice['level'] == level


def test_a_check_run_as_an_old_pod_and_shown_by_a_newer_one_is_info(store: Store):
    # given
    checker(store, github(release('v0.4.0'), release('v0.2.5', body=SECURE)), version='0.2.0').run_once()

    # when
    newer = checker(store, FakeHttp(), version='0.2.6').notice()
    older = checker(store, FakeHttp(), version='0.2.0').notice()

    # then
    assert (newer['level'], older['level']) == ('info', 'warn')


def test_a_failed_check_is_silent_and_keeps_the_last_good_result(store: Store, caplog):
    # given
    checker(store, github(release('v0.4.0', body='Security: fix'))).run_once()
    first = store.version_check()
    failures = [
        FakeHttp({('GET', LIST): HttpError(403, 'api.github.com', 'rate limit exceeded')}),
        FakeHttp({('GET', LIST): HttpError(429, 'api.github.com')}),
        FakeHttp({('GET', LIST): NotJson('api.github.com answered without JSON')}),
        FakeHttp({('GET', LIST): Unreachable('api.github.com unreachable')}),
        FakeHttp({('GET', LIST): {'message': 'Not Found'}}),
        FakeHttp({('GET', LIST): Reply(None, {})}),
        FakeHttp({('GET', LIST): []}),
    ]

    # when
    with caplog.at_level(logging.DEBUG, logger='skopax.versioncheck'):
        results = [checker(store, http).run_once() for http in failures]

    # then
    assert results == [None] * len(failures)
    assert store.version_check() == first
    assert checker(store, FakeHttp()).notice()['level'] == 'warn'
    assert [r for r in caplog.records if r.levelno >= logging.INFO] == []


@pytest.mark.parametrize('answer', [{'message': 'Not Found'}, [], None, 'text', [None, 'junk', {'tag_name': 'v1'}]])
def test_a_handled_bad_shape_leaves_no_failure_record(store: Store, caplog, answer):
    # when
    with caplog.at_level(logging.DEBUG, logger='skopax.versioncheck'):
        checker(store, listing(answer)).run_once()

    # then
    assert [r.getMessage() for r in caplog.records if 'did not finish' in r.getMessage()] == []


def test_a_later_good_check_replaces_the_result(store: Store):
    # given
    checker(store, github(release('v0.4.0'))).run_once()

    # when
    checker(store, github(release('v0.3.1', body='Security: x'))).run_once()

    # then
    found = store.version_check()
    assert (found['latest'], found['securityIn'], found['url']) == ('0.3.1', ['0.3.1'], PAGE + 'v0.3.1')


def test_the_off_switch_makes_no_request_and_shows_no_notice(store: Store):
    # given
    http = github(release('v0.4.0'))
    store.note_version_check({'latest': '0.4.0', 'url': PAGE + 'v0.4.0', 'securityIn': ['0.4.0'], 'at': '2026-11-19T00:00:00Z'})
    off = checker(store, http, enabled=False)

    # when
    stored, started, begun, notice = off.run_once(), off.due_at_start(), off.start(), off.notice()

    # then
    assert (stored, started, begun, notice, http.calls) == (None, False, None, None, [])


def test_the_notice_levels_texts_and_link(store: Store):
    # given
    info = checker(store, github(release('v0.4.0')))
    info.run_once()

    # when
    plain = info.notice()
    checker(store, github(release('v0.4.0', body='Security: fix'))).run_once()
    warn = info.notice()

    # then
    link = {'href': PAGE + 'v0.4.0', 'linkText': 'Release notes'}
    assert plain == {'id': 'update', 'level': 'info', 'text': 'Skopax 0.4.0 is available; this server runs 0.2.0.', **link}
    assert warn == {'id': 'update', 'level': 'warn', 'text': 'Skopax 0.4.0 is available and fixes a security issue; this server runs 0.2.0. Update soon.', **link}
    assert plain['text'] == msg('update.available', upstream='Skopax', latest='0.4.0', current='0.2.0')


def test_the_notice_goes_away_once_the_server_runs_the_new_version(store: Store):
    # given
    checker(store, github(release('v0.4.0'))).run_once()

    # when
    after_upgrade = checker(store, FakeHttp(), version='0.4.0').notice()

    # then
    assert after_upgrade is None


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'http://github.com/skopax/skopax/releases/tag/v0.4.0', 'https://evil.example.com/x'])
def test_a_stored_link_that_is_not_https_github_is_not_shown(store: Store, url: str):
    # given
    store.note_version_check({'latest': '0.4.0', 'url': url, 'securityIn': [], 'at': '2026-11-19T00:00:00Z'})

    # then
    assert checker(store, FakeHttp()).notice() is None


def test_the_first_check_waits_for_a_stale_result_only(store: Store):
    # given
    check = checker(store, github(release('v0.4.0')))
    empty = check.due_at_start()
    check.run_once()

    # when
    fresh = check.due_at_start()
    later = VersionCheck(store, FakeHttp(), lambda: True, RUNNING, lambda: NOW + 21 * 3600).due_at_start()

    # then
    assert (empty, fresh, later) == (True, False, True)


def test_the_scheduled_run_skips_a_fresh_result(store: Store):
    # given
    http = github(release('v0.4.0'))
    check = checker(store, http)

    # when
    first = check.scheduled(NOW)
    second = check.scheduled(NOW)

    # then
    assert first is not None and second is None and len(http.calls) == 1


def test_the_jitter_is_a_few_minutes_and_the_same_for_one_install():
    # then
    assert jitter_of('mongodb://a/db') == jitter_of('mongodb://a/db')
    assert {jitter_of(f'mongodb://host-{n}/db') for n in range(30)} != {0}
    assert all(0 <= jitter_of(f'mongodb://host-{n}/db') < 600 for n in range(30))


def test_the_daily_job_stores_the_result_and_every_pod_shows_the_same_notice(store: Store):
    # given
    http = github(release('v99.0.0', body='Security: fix'))
    other_http = FakeHttp({('GET', LIST): [release('v99.0.0')]})
    here, there = served(store, http), served(store, other_http)

    # when
    before = ids(here)
    ran = here.app.state.scheduler.run_due()
    second = there.app.state.scheduler.run_due()
    shown, other = update_of(here), update_of(there)

    # then
    assert 'update' not in before and 'version-check' in ran and 'version-check' not in second
    assert shown == other and shown['level'] == 'warn' and shown['href'] == PAGE + 'v99.0.0'
    assert len(http.hits(LIST)) == 1 and other_http.calls == []


def test_a_server_with_the_check_off_never_asks_and_lists_no_job(store: Store):
    # given
    http = github(release('v0.4.0'))
    c = served(store, http, update_check=False)

    # when
    ran = c.app.state.scheduler.run_due()

    # then
    assert 'version-check' not in ran and http.calls == [] and 'update' not in ids(c)


def test_only_an_admin_sees_the_update_notice(store: Store):
    # given
    checker(store, github(release('v99.0.0'))).run_once()
    c = served(store, FakeHttp())

    # when
    anonymous = c.get(NOTICES)

    # then
    assert anonymous.status_code in (401, 403) and 'update' not in anonymous.text
    assert 'update' in ids(c)


def test_the_feature_is_listed_and_on_by_default(store: Store):
    # given
    c = served(store, FakeHttp())

    # when
    item = next(i for i in c.get('/api/admin/features', headers=AUTH).json()['items'] if i['id'] == 'update-check')

    # then
    assert (item['key'], item['label'], item['on'], item['icon']) == ('core.update-check', 'Check for new versions', True, 'tag')
    assert item['help'] == 'Asks GitHub once a day for the latest Skopax release and shows an admin notice when a newer one exists'


@pytest.mark.parametrize('env, expected', [({}, True), ({'SKOPAX_UPDATE_CHECK': 'false'}, False), ({'SKOPAX_UPDATE_CHECK': 'FALSE'}, False), ({'SKOPAX_UPDATE_CHECK': 'true'}, True)])
def test_the_environment_variable_sets_the_start_value(env: dict, expected: bool):
    # then
    assert load_settings(env=env).update_check is expected


def test_the_switch_in_admin_turns_the_notice_off_and_on(store: Store):
    # given
    checker(store, github(release('v99.0.0'))).run_once()
    c = served(store, FakeHttp())

    # when
    c.put('/api/admin/features/update-check', json={'on': False}, headers=AUTH)
    off = ids(c)
    c.put('/api/admin/features/update-check', json={'on': True}, headers=AUTH)

    # then
    assert 'update' not in off and 'update' in ids(c)


def test_a_rebranded_install_still_names_the_upstream_product(store: Store):
    # given
    checker(store, github(release('v99.0.0'))).run_once()
    c = served(store, FakeHttp(), display_name='Acme Ops')

    # when
    shown = update_of(c)

    # then
    assert brand.product() == 'Acme Ops'
    assert shown['text'] == f'Skopax 99.0.0 is available; this server runs {brand.display_version()}.'


def waited(http: FakeHttp, count: int, seconds: float = 3.0) -> None:
    end = time.monotonic() + seconds
    while len(http.calls) < count and time.monotonic() < end:
        time.sleep(0.02)
    time.sleep(0.15)


def stored_at(store: Store, seconds_ago: float) -> None:
    at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - seconds_ago))
    store.note_version_check({'latest': '0.4.0', 'url': PAGE + 'v0.4.0', 'securityIn': [], 'at': at})


@pytest.mark.parametrize('stored, calls', [(None, 1), (25 * 3600, 1), (3600, 0)])
def test_the_start_check_runs_once_for_a_missing_or_stale_result_and_not_for_a_fresh_one(store: Store, monkeypatch, stored, calls: int):
    # given
    monkeypatch.setattr('skopax.app.VERSION_START_DELAY', 0)
    http = github(release('v0.4.0'))
    if stored is not None:
        stored_at(store, stored)

    # when
    with served(store, http):
        waited(http, calls)

    # then
    assert len(http.hits(LIST)) == calls


def test_the_start_check_makes_no_request_while_the_switch_is_off(store: Store, monkeypatch):
    # given
    monkeypatch.setattr('skopax.app.VERSION_START_DELAY', 0)
    http = github(release('v0.4.0'))

    # when
    with served(store, http, update_check=False):
        waited(http, 1)

    # then
    assert http.calls == []


def test_two_pods_starting_together_make_one_start_request(store: Store, monkeypatch):
    # given
    monkeypatch.setattr('skopax.app.VERSION_START_DELAY', 0)
    http = github(release('v0.4.0'))

    # when
    with served(store, http), served(store, http):
        waited(http, 2)

    # then
    assert len(http.hits(LIST)) == 1


def test_leaving_the_app_during_the_start_delay_does_not_hang(store: Store):
    # given
    http = github(release('v0.4.0'))
    began = time.monotonic()

    # when
    with served(store, http):
        pass

    # then
    assert time.monotonic() - began < 10 and http.calls == []
