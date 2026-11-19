from tests.tagdefs import apply_team_tags
from skopax import chatui

apply_team_tags()
chatui.prepare('http://testserver')

import dataclasses
import sys
import threading

import chainlit.data
import mongomock
import pytest
from fastapi.testclient import TestClient

from skopax import agent, app as app_module, brand, clock, engine, messages, probe, redact, sections, tags, versioncheck
from skopax.remote import Unreachable
from skopax.app import create_app
from skopax.chatui.text import reset as reset_chat_texts
from skopax.investigator import prompt, select
from skopax.config import Settings
from skopax.jiralinks import JiraLinks
from skopax.store import Store
from skopax.users import Users

RELEASE_IGNORE = ('help-ui', 'tools-gateway', 'tools-mcp')
PROM_UI = 'https://prometheus.example.com'
TOKEN = 'test-token'
AUTH = {'Authorization': f'Bearer {TOKEN}'}
ADMIN_EMAIL = 'admin@example.com'
AGENT_THREAD = 'agent-sources'


def settings(token: str = TOKEN, language: str = 'en', public_read: bool = True) -> Settings:
    return Settings(mongo_uri='mongodb://unused', mongo_db='skopax', token=token, commit_id='test', investigate_language=language, public_read=public_read,
                    jira_project='OPS', jira_projects=('OPS', 'WEB', 'APP', 'MOB'), jira_tech_project='INFRA', jira_tech_assignee='712020:tech',
                    jira_developer_field='customfield_4', jira_scope_field='customfield_1', jira_points_field='customfield_2', jira_sprint_field='customfield_3', base_url='https://skopax.example.com',
                    self_project='platform/skopax', self_log_index='logs-platform-*', self_deploy='platform/skopax', release_ignore=RELEASE_IGNORE)


READY = {'anthropic_api_key': 'sk-test', 'investigate_mode': 'live'}


def ready(s: Settings, **over) -> Settings:
    return dataclasses.replace(s, **READY, **over)


def ready_app(s: Settings, store, **kw):
    from tests.fake_anthropic import FakeAnthropic
    app = create_app(ready(s), store, anthropic_client=kw.pop('anthropic_client', None) or FakeAnthropic([]), **kw)
    app.state.investigator.kick = lambda now: None
    return app


def jira_links(store, jira, **kw) -> JiraLinks:
    return JiraLinks(store, jira, base_url='https://skopax.example.com', developer_field='customfield_4', **kw)


@pytest.fixture(autouse=True)
def default_brand():
    brand.use('Skopax')
    brand.use_hidden(None)
    yield
    brand.use('Skopax')
    brand.use_hidden(None)


@pytest.fixture(autouse=True)
def reset_plugin_rules():
    redact.use_rules(())
    tags.use_rules({})
    tags.use_tags({})
    tags.use_texts(())
    select.use_own_findings(())
    engine.set_daily(())
    chatui.set_translations(())
    chatui.FAILED_LOCALES.clear()
    messages.reset()
    prompt.use_writing(None)
    reset_chat_texts()
    sections.NOT_LISTS.clear()
    yield
    redact.use_rules(())
    tags.use_rules({})
    tags.use_tags({})
    tags.use_texts(())
    select.use_own_findings(())
    engine.set_daily(())
    chatui.set_translations(())
    chatui.FAILED_LOCALES.clear()
    messages.reset()
    prompt.use_writing(None)
    reset_chat_texts()


def agent_threads() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if t.name == AGENT_THREAD and t.is_alive()}


@pytest.fixture(autouse=True)
def no_agent_threads(monkeypatch):
    before = agent_threads()
    started: list[agent.Refresher] = []
    start = agent.Refresher.start

    def tracked(refresher: agent.Refresher) -> None:
        started.append(refresher)
        start(refresher)

    monkeypatch.setattr(agent.Refresher, 'start', tracked)
    yield
    leaked = agent_threads() - before
    for refresher in started:
        if refresher._thread in leaked:
            refresher.stop()
    assert not leaked, 'a test left an agent-sources thread running'


@pytest.fixture(autouse=True)
def offline_version_check(monkeypatch):
    def refuse(*args, **kw):
        raise Unreachable('no network in tests')

    monkeypatch.setattr(versioncheck, 'send', refuse)


@pytest.fixture(autouse=True)
def offline_probe(monkeypatch):
    monkeypatch.setattr(app_module, 'Probe', lambda targets, **kw: probe.Probe(targets, checker=lambda target, timeout: {'error': 'dns: gaierror'}))


@pytest.fixture(autouse=True)
def utc_clock():
    clock.set_zone('UTC')
    yield
    clock.set_zone('UTC')


@pytest.fixture(autouse=True)
def fresh_chainlit(monkeypatch):
    monkeypatch.setattr(chainlit.data, '_data_layer', None)
    monkeypatch.setattr(chainlit.data, '_data_layer_initialized', False)
    server = sys.modules.get('chainlit.server')
    if server is not None:
        monkeypatch.setattr(server.app, 'middleware_stack', None)
        monkeypatch.setattr(server.app, 'user_middleware', list(server.app.user_middleware))


@pytest.fixture
def store() -> Store:
    made = Store(mongomock.MongoClient(tz_aware=True)['skopax'], release_ignore=RELEASE_IGNORE, self_deploy='platform/skopax')
    Users(made.db).create(ADMIN_EMAIL, 'Admin', 'admin')
    return made


@pytest.fixture
def client(store: Store) -> TestClient:
    with TestClient(create_app(settings(), store)) as c:
        yield c


def anomaly(key: str, sev: str = 'warn', status: str = 'open', **extra) -> dict:
    return {'key': key, 'sev': sev, 'status': status, 'svc': 'checkout-service', 'label': 'ERROR pattern', **extra}


def state(*anomalies: dict, swept: str = '2026-10-10T09:04:00Z') -> dict:
    return {'anomalies': list(anomalies), 'sweptIso': swept, 'nextIso': '2026-10-10T10:04:00Z', 'sources': {'prom': 'ok'}, 'history': []}


@pytest.fixture(autouse=True)
def no_google_retry_pause(monkeypatch):
    from skopax.kit import google
    monkeypatch.setattr(google, 'RETRY_PAUSE', 0)
