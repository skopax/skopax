import dataclasses
import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from pymongo.errors import AutoReconnect

from tests.conftest import AUTH, settings
from tests.test_auth import Clock
from tests.test_slack_listener import Factory, listening_app, wait_for
from skopax.app import create_app
from skopax.messages import msg
from skopax.registry import Registry
from skopax.settingsources import SourceSet
from skopax.store import Store

NOTICES = '/api/admin/notices'
LIVENESS = '/actuator/health/liveness'
FORWARDED = {'X-Forwarded-For': '203.0.113.9'}
PEER = 'testclient'
PROXY_ID = 'proxy-warning'
HOUR_S = 3600
DAY = dt.timedelta(hours=24)
BROKEN = 'tests.plugins.broken_plugin'
SOURCE = 'config file'
PLAIN_KEY = 'sk-plain-' + 'q' * 30


def served(store: Store, clock: Clock | None = None, registry: Registry | None = None, **over):
    s = dataclasses.replace(settings(), **over)
    return create_app(s, store, clock=clock, registry=registry if registry is not None else Registry.of())


def client_of(app) -> TestClient:
    return TestClient(app, base_url='https://testserver')


def spy_writes(store: Store, monkeypatch) -> list[tuple[str, str]]:
    writes = []
    note = store.note_proxy
    monkeypatch.setattr(store, 'note_proxy', lambda peer, at: writes.append((peer, at)) or note(peer, at))
    return writes


def parsed(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace('Z', '+00:00'))


def ids(c: TestClient) -> list[str]:
    return [item['id'] for item in c.get(NOTICES, headers=AUTH).json()['items']]


def failed_saves(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if 'proxy sighting' in r.getMessage()]


def test_untrusted_forwarding_is_shown_in_admin(store: Store, monkeypatch):
    # given
    clock = Clock()
    app = served(store, clock)
    c = client_of(app)
    writes = spy_writes(store, monkeypatch)

    # when
    c.get(LIVENESS, headers=FORWARDED)
    first = app.state.proxy_write
    first.result(timeout=2)
    c.get(LIVENESS, headers=FORWARDED)
    within_the_hour = (len(writes), app.state.proxy_write is first)
    clock.advance(HOUR_S)
    c.get(LIVENESS, headers=FORWARDED)
    app.state.proxy_write.result(timeout=2)
    stored = store.db['meta'].find_one({'_id': PROXY_ID})
    shown = c.get(NOTICES, headers=AUTH).json()['items']
    other_pod = ids(client_of(served(store)))
    seen_at = parsed(stored['at'])
    monkeypatch.setattr('skopax.app.utc_now', lambda: seen_at + DAY - dt.timedelta(seconds=1))
    last_second = ids(c)
    monkeypatch.setattr('skopax.app.utc_now', lambda: seen_at + DAY)
    a_day_later = ids(c)

    # then
    assert within_the_hour == (1, True), 'a second request within the hour writes nothing'
    assert [peer for peer, _ in writes] == [PEER, PEER], 'a new hour writes the sighting again'
    assert (stored['peer'], stored['at']) == (PEER, writes[-1][1]) and set(stored) == {'_id', 'peer', 'at'}
    assert store.db['state'].find_one({'_id': PROXY_ID}) is None, 'the sighting is not a board state document'
    assert shown[0] == {'id': 'proxy', 'level': 'warn', 'text': msg('proxy.untrusted', peer=PEER), 'at': stored['at']}
    assert 'proxy' in other_pod, 'every pod reads the sighting from the database'
    assert 'proxy' in last_second and 'proxy' not in a_day_later


def test_notices_list_generated_key_source_errors_unreadable_ai_key_and_broken_plugins(store: Store):
    # given
    def down():
        raise ConnectionError('refused')

    registry = Registry.load({'SKOPAX_PLUGINS': f'tests.plugins.demo_plugin,tests.plugins.enterprise_plugin,{BROKEN}'}, builtin=(), entry_points=False)
    sources = SourceSet([(SOURCE, down)], {SOURCE: None}, failed={SOURCE: 'ConnectionError'})
    store.db['admin_secrets'].insert_one({'_id': 'anthropic.api-key', 'value': PLAIN_KEY, 'updatedBy': 'alice@example.com'})
    app = create_app(settings(), store, registry=registry, sources=sources)

    # when
    with TestClient(app, base_url='https://testserver') as c:
        c.get(LIVENESS, headers=FORWARDED)
        app.state.proxy_write.result(timeout=2)
        res = c.get(NOTICES, headers=AUTH)
    items = res.json()['items']
    by_id = {item['id']: item for item in items}

    # then
    assert res.status_code == 200 and registry.get('enterprise-demo').state == 'locked'
    assert [(item['id'], item['level']) for item in items] == [('proxy', 'warn'), ('secret-key', 'info'), (f'source:{SOURCE}', 'warn'), ('ai-key', 'warn'), (f'plugin:{BROKEN}', 'warn')]
    assert by_id['secret-key'] == {'id': 'secret-key', 'level': 'info', 'text': msg('secrets.generated_key')}
    assert by_id[f'source:{SOURCE}'] == {'id': f'source:{SOURCE}', 'level': 'warn', 'text': msg('settings.source_failed', source=SOURCE, error='ConnectionError'),
                                         'at': sources.status()[SOURCE]['since']}
    assert by_id['ai-key'] == {'id': 'ai-key', 'level': 'warn', 'text': msg('aikey.unreadable')}
    plugin = by_id[f'plugin:{BROKEN}']['text']
    assert plugin == registry.get(BROKEN).reason and plugin.count('was not loaded') == 1 and 'ImportError' in plugin
    assert PLAIN_KEY not in res.text


def test_a_set_up_install_with_its_own_secret_key_has_no_notices(store: Store):
    # given
    c = client_of(served(store, secret_key='k1'))

    # when
    res = c.get(NOTICES, headers=AUTH)

    # then
    assert (res.status_code, res.json()) == (200, {'items': []})


def test_a_sighting_document_of_another_shape_shows_no_notice(store: Store):
    # given
    c = client_of(served(store, secret_key='k1'))
    shapes = [{'peer': PEER, 'at': 'yesterday'}, {'peer': PEER}, {'peer': 7, 'at': '2026-11-15T08:00:00Z'}, {'at': '2026-11-15T08:00:00Z'}, {'peer': PEER, 'at': '9999-12-31T23:59:59-01:00'}]

    # when
    seen = []
    for shape in shapes:
        store.db['meta'].replace_one({'_id': PROXY_ID}, {'_id': PROXY_ID, **shape}, upsert=True)
        res = c.get(NOTICES, headers=AUTH)
        seen.append((res.status_code, res.json()))

    # then
    assert seen == [(200, {'items': []})] * len(shapes)


def test_a_sighting_that_cannot_be_saved_is_logged_once_and_the_request_still_answers(store: Store, monkeypatch, caplog):
    # given
    caplog.set_level(logging.WARNING)
    app = served(store)
    c = client_of(app)

    def refuse(peer, at):
        raise AutoReconnect('mongo down')

    monkeypatch.setattr(store, 'note_proxy', refuse)

    # when
    res = c.get(LIVENESS, headers=FORWARDED)
    outcome = app.state.proxy_write.result(timeout=2)

    # then
    assert (res.status_code, outcome) == (200, None)
    said = failed_saves(caplog)
    assert len(said) == 1 and 'AutoReconnect' in said[0] and 'mongo down' not in said[0]
    assert store.db['meta'].find_one({'_id': PROXY_ID}) is None


def test_a_sighting_after_the_lifespan_ended_is_logged_and_the_request_still_answers(store: Store, monkeypatch, caplog):
    # given
    caplog.set_level(logging.WARNING)
    clock = Clock()
    app = served(store, clock)
    writes = spy_writes(store, monkeypatch)
    with TestClient(app, base_url='https://testserver') as c:
        c.get(LIVENESS, headers=FORWARDED)
        app.state.proxy_write.result(timeout=2)
    clock.advance(HOUR_S)

    # when
    again = client_of(app).get(LIVENESS, headers=FORWARDED)

    # then
    assert again.status_code == 200
    assert len(writes) == 1, 'the executor was shut down when the first lifespan ended'
    said = failed_saves(caplog)
    assert len(said) == 1 and 'RuntimeError' in said[0]


def test_a_future_dated_sighting_is_clamped_to_now_and_ages_out(store: Store, monkeypatch):
    # given
    c = client_of(served(store, secret_key='k1'))
    now = dt.datetime(2026, 11, 15, 8, 0, tzinfo=dt.timezone.utc)
    store.note_proxy(PEER, '2099-01-01T00:00:00Z')
    monkeypatch.setattr('skopax.app.utc_now', lambda: now)

    # when
    first = c.get(NOTICES, headers=AUTH).json()['items']
    stored = store.db['meta'].find_one({'_id': PROXY_ID})['at']
    monkeypatch.setattr('skopax.app.utc_now', lambda: now + DAY - dt.timedelta(seconds=1))
    last_second = ids(c)
    monkeypatch.setattr('skopax.app.utc_now', lambda: now + DAY)
    a_day_later = ids(c)

    # then
    assert first == [{'id': 'proxy', 'level': 'warn', 'text': msg('proxy.untrusted', peer=PEER), 'at': '2026-11-15T08:00:00Z'}]
    assert stored == '2026-11-15T08:00:00Z', 'the clamp is kept, so the day counts from the first read'
    assert 'proxy' in last_second and 'proxy' not in a_day_later


def test_a_listener_that_fails_to_stop_still_lets_the_sighting_writer_shut_down(store: Store, monkeypatch, caplog):
    # given
    caplog.set_level(logging.WARNING)
    shut = []

    class Pool(ThreadPoolExecutor):
        def shutdown(self, *args, **kwargs):
            shut.append(True)
            super().shutdown(*args, **kwargs)

    monkeypatch.setattr('skopax.app.ThreadPoolExecutor', Pool)
    factory = Factory()
    app = listening_app(store, factory)

    # when
    with TestClient(app) as c:
        wait_for(lambda: bool(factory.made) and factory.made[0].connected.is_set())
        stop = app.state.listener.stop

        def failing() -> None:
            stop()
            raise ConnectionError('socket close failed for xapp-test')

        monkeypatch.setattr(app.state.listener, 'stop', failing)
        c.get(LIVENESS, headers=FORWARDED)
        app.state.proxy_write.result(timeout=2)

    # then
    assert shut == [True], 'the sighting writer is shut down even though the listener failed to stop'
    said = [r.getMessage() for r in caplog.records if 'listener' in r.getMessage() and 'stop' in r.getMessage()]
    assert len(said) == 1 and 'ConnectionError' in said[0] and 'xapp-test' not in said[0]
