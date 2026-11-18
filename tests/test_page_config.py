from dataclasses import replace

from fastapi.testclient import TestClient

from tests.conftest import settings
from tests.fake_http import FakeHttp
from tests.plugin_kit import serve_jira
from skopax.app import create_app
from skopax.store import Store


def client_for(store: Store, transport=None, **changes) -> TestClient:
    return TestClient(create_app(replace(settings(), **changes), store, transport=transport))


def test_api_server_names_the_configured_timezone(store: Store):
    # given
    with client_for(store, timezone='Europe/Berlin') as c:
        # when
        body = c.get('/api/server').json()

    # then
    assert body['tz'] == 'Europe/Berlin'


def test_api_server_defaults_to_utc(store: Store):
    # given
    with client_for(store) as c:
        # when
        body = c.get('/api/server').json()

    # then
    assert body['tz'] == 'UTC'


def test_api_me_carries_the_jira_base_url_without_a_trailing_slash(store: Store):
    # given
    serve_jira(store, settings(), url='https://jira.example.com/')
    with client_for(store, FakeHttp()) as c:
        # when
        body = c.get('/api/me').json()

    # then
    assert body['jiraUrl'] == 'https://jira.example.com'


def test_api_me_has_no_jira_url_when_jira_is_not_configured(store: Store):
    # given
    with client_for(store) as c:
        # when
        body = c.get('/api/me').json()

    # then
    assert body['jiraUrl'] == ''
