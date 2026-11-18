import asyncio
import dataclasses
import logging
import threading
import time
from types import SimpleNamespace as NS

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient
from pymongo.errors import PyMongoError

from tests.conftest import AUTH, anomaly, settings, state
from tests.fake_anthropic import FakeAnthropic, FakeModels
from tests.fake_slack import fake_slack, slack_instance
from tests.test_investigator import NOW, ask_for, seed
from tests.test_investigator_tools import sources
from tests.test_slack_listener import Factory, wait_for
from skopax.aikey import AiKey, InvalidKey, TestBusy as KeyTestBusy
from skopax.app import create_app
from skopax.investigator import Investigator
from skopax.investigator.gate import Gate, GateDown
from skopax.investigator.smalltalk import SmallTalk
from skopax.investigator.tools import Effects
from skopax.messages import msg
from skopax.sealer import Sealer
from skopax.selfreview import SelfReview
from skopax.store import Store
from skopax.tunables import Live, TunableStore

KEY_A = 'sk-test-' + 'a' * 32
KEY_B = 'sk-test-' + 'b' * 32
PATH = '/api/admin/anthropic-key'
TEST = PATH + '/test'
KEY_ID = 'anthropic.api-key'
MODEL = settings().investigate_model
LEAK = 'upstream said ' + KEY_B
REQUEST = httpx2.Request('GET', 'https://api.example.com')


def status_error(cls, code: int):
    return cls(LEAK, response=httpx2.Response(code, request=REQUEST), body={'error': {'message': LEAK}})


def app(store: Store, key: str = '', factory: FakeModels | None = None, **over):
    return create_app(dataclasses.replace(settings(), anthropic_api_key=key, **over), store, ai_factory=factory or FakeModels())


def everything_stored(store: Store) -> str:
    return '\n'.join(str(doc) for name in store.db.list_collection_names() for doc in store.db[name].find({}))


def admin_lines(store: Store) -> list[str]:
    return [a['text'] for a in store.list_activity(None, 50) if a['kind'] == 'admin']


def test_admin_key_wins_over_env_and_falls_back_when_cleared(store: Store):
    # given
    c = TestClient(app(store, KEY_A))
    aikey = c.app.state.aikey
    before = (aikey.current(), c.get(PATH, headers=AUTH).json()['using'])

    # when
    put = c.put(PATH, json={'key': KEY_B}, headers=AUTH)
    with_admin = (aikey.current(), put.json()['using'])
    deleted = c.delete(PATH, headers=AUTH)
    cleared = (aikey.current(), c.get(PATH, headers=AUTH).json()['using'])
    bare = TestClient(app(store))
    nothing = (bare.app.state.aikey.current(), bare.get(PATH, headers=AUTH).json())

    # then
    assert before == (KEY_A, 'config')
    assert put.status_code == 200 and with_admin == (KEY_B, 'admin')
    assert deleted.status_code == 204 and cleared == (KEY_A, 'config')
    assert nothing[0] == '' and (nothing[1]['using'], nothing[1]['admin'], nothing[1]['config'], nothing[1]['unreadable']) == (None, False, False, False)


def test_the_key_is_sealed_write_only_and_never_leaves_the_server(store: Store, caplog):
    # given
    caplog.set_level(logging.DEBUG)
    c = TestClient(app(store, KEY_A, FakeModels(RuntimeError(LEAK))))

    # when
    put = c.put(PATH, json={'key': KEY_B}, headers=AUTH)
    bodies = [put.text, c.get(PATH, headers=AUTH).text, c.post(TEST, json={}, headers=AUTH).text, c.post(TEST, json={'key': KEY_B}, headers=AUTH).text]
    refused = [c.put(PATH, json={'key': KEY_B + 'c' * 400}, headers=AUTH), c.put(PATH, json={'key': KEY_B, 'note': KEY_B}, headers=AUTH),
               c.post(TEST, json={'key': KEY_B + 'c' * 400}, headers=AUTH), c.put(PATH, json={'key': KEY_B + ' and more'}, headers=AUTH), c.put(PATH, json={KEY_B: 'x'}, headers=AUTH)]
    with pytest.raises(ValueError) as failed:
        c.app.state.aikey.set(KEY_B + '!', 'admin@example.com')
    stored = store.db['admin_secrets'].find_one({'_id': KEY_ID})
    history = c.get('/api/admin/history', headers=AUTH).json()

    # then
    assert put.status_code == 200 and stored['value'].startswith('enc:v1:')
    assert all(KEY_B not in b for b in bodies)
    assert [(r.status_code, r.json()) for r in refused] == [(422, {'detail': msg('aikey.invalid')})] * 5 and all(KEY_B not in r.text for r in refused)
    assert KEY_B not in repr(vars(c.app.state.aikey))
    assert KEY_B not in str(failed.value) and KEY_B not in repr(failed.value)
    assert KEY_B not in everything_stored(store)
    assert all(KEY_B not in r.getMessage() for r in caplog.records)
    assert {k: history[0][k] for k in ('key', 'secret', 'old', 'new')} == {'key': KEY_ID, 'secret': True, 'old': None, 'new': 'set'}


def test_consumers_pick_up_a_new_key_without_restart(store: Store, monkeypatch):
    # given
    built = []
    monkeypatch.setattr(anthropic, 'Anthropic', lambda **kw: built.append(kw['api_key']) or FakeAnthropic([]))
    c = TestClient(app(store, KEY_A))
    state = c.app.state

    def clients() -> list:
        return [state.investigator._client(), state.gate._client(), state.investigator.talk._client(), state.self_review._client()]

    first, again = clients(), clients()

    # when
    c.put(PATH, json={'key': KEY_B}, headers=AUTH)
    state.aikey.refresh()
    second = clients()

    # then
    assert built == [KEY_A] * 4 + [KEY_B] * 4
    assert all(a is b for a, b in zip(first, again)) and all(a is not b for a, b in zip(first, second))


def test_an_injected_client_is_never_replaced(store: Store, monkeypatch):
    # given
    built = []
    monkeypatch.setattr(anthropic, 'Anthropic', lambda **kw: built.append(kw['api_key']) or FakeAnthropic([]))
    fake = FakeAnthropic([])
    c = TestClient(create_app(dataclasses.replace(settings(), anthropic_api_key=KEY_A), store, anthropic_client=fake, ai_factory=FakeModels()))
    state = c.app.state

    # when
    c.put(PATH, json={'key': KEY_B}, headers=AUTH)
    got = [state.investigator._client(), state.gate._client(), state.investigator.talk._client(), state.self_review._client()]

    # then
    assert all(g is fake for g in got) and built == []


def test_a_key_set_in_admin_starts_the_investigator_on_an_app_booted_without_one(store: Store, monkeypatch):
    # given
    monkeypatch.setattr(anthropic, 'Anthropic', lambda **kw: FakeAnthropic([]))
    c = TestClient(app(store, '', investigate_mode='shadow'))
    inv, scheduler = c.app.state.investigator, c.app.state.scheduler
    before = (inv.enabled, inv._client(), 'investigate' in scheduler.run_due())

    # when
    put = c.put(PATH, json={'key': KEY_B}, headers=AUTH)
    after = (inv.enabled, inv._client() is not None, 'investigate' in scheduler.run_due())

    # then
    assert before == (False, None, False)
    assert put.status_code == 200 and after == (True, True, True)


def test_a_pod_reads_the_stored_key_when_it_starts(store: Store, monkeypatch):
    # given
    monkeypatch.setattr(anthropic, 'Anthropic', lambda **kw: FakeAnthropic([]))
    TestClient(app(store, KEY_A)).put(PATH, json={'key': KEY_B}, headers=AUTH)
    other = app(store, KEY_A)
    before = other.state.aikey.current()

    # when
    with TestClient(other) as c:
        booted = (c.app.state.aikey.current(), c.get(PATH, headers=AUTH).json()['using'])

    # then
    assert before == KEY_A
    assert booted == (KEY_B, 'admin')


def test_self_review_counts_an_injected_client_as_configured(store: Store):
    # given
    s = dataclasses.replace(settings(), anthropic_api_key='')

    # when
    injected = SelfReview(store, s, lambda now: {}, lambda text: {}, client=FakeAnthropic([]))
    bare = SelfReview(store, s, lambda now: {}, lambda text: {})
    keyed = SelfReview(store, s, lambda now: {}, lambda text: {}, key=lambda: KEY_B)

    # then
    assert (injected.configured, bare.configured, keyed.configured) == (True, False, True)
    assert bare._client() is None


def test_unreadable_stored_key_falls_back_and_says_so(store: Store, caplog):
    # given
    caplog.set_level(logging.DEBUG)
    TestClient(app(store, KEY_A, secret_key='k1')).put(PATH, json={'key': KEY_B}, headers=AUTH)
    no_meta_key = store.db['meta'].find_one({'_id': 'secret-key'}) is None
    other = TestClient(app(store, KEY_A, secret_key='k2'))

    # when
    other.app.state.aikey.refresh()
    unreadable = (other.app.state.aikey.current(), other.get(PATH, headers=AUTH).json())
    store.db['admin_secrets'].delete_many({})
    TestClient(app(store, KEY_A)).put(PATH, json={'key': KEY_B}, headers=AUTH)
    first_seal = store.db['admin_secrets'].find_one({'_id': KEY_ID})['value']
    master = TestClient(app(store, KEY_A, secret_key='k1'))
    master.app.state.aikey.refresh()
    resealed = store.db['admin_secrets'].find_one({'_id': KEY_ID})['value']

    # then
    assert no_meta_key
    assert unreadable[0] == KEY_A and (unreadable[1]['unreadable'], unreadable[1]['admin'], unreadable[1]['using']) == (True, False, 'config')
    assert master.app.state.aikey.current() == KEY_B and master.get(PATH, headers=AUTH).json()['unreadable'] is False
    assert resealed != first_seal and Sealer('k1').open(resealed) == KEY_B
    assert any('sealed again' in r.getMessage() for r in caplog.records)
    assert all(KEY_B not in r.getMessage() for r in caplog.records)


def test_a_stored_document_of_another_shape_reads_as_not_set(store: Store, caplog):
    # given
    store.db['admin_secrets'].insert_one({'_id': KEY_ID, 'value': KEY_B, 'updatedBy': 'someone'})
    c = TestClient(app(store, KEY_A))

    # when
    c.app.state.aikey.refresh()
    c.app.state.aikey.refresh()
    seen = c.get(PATH, headers=AUTH)

    # then
    assert c.app.state.aikey.current() == KEY_A
    assert [r.getMessage() for r in caplog.records if r.name == 'skopax.aikey'] == [msg('aikey.unreadable')]
    assert seen.status_code == 200 and (seen.json()['unreadable'], seen.json()['admin'], seen.json()['using']) == (True, False, 'config')
    assert KEY_B not in seen.text


OUTCOMES = [
    (NS(id=MODEL), True, ('aikey.test_ok', {'model': MODEL})),
    (status_error(anthropic.AuthenticationError, 401), False, ('aikey.test_rejected', {})),
    (status_error(anthropic.PermissionDeniedError, 403), False, ('aikey.test_forbidden', {'model': MODEL})),
    (status_error(anthropic.NotFoundError, 404), False, ('aikey.test_no_model', {'model': MODEL})),
    (anthropic.APITimeoutError(request=REQUEST), False, ('aikey.test_timeout', {})),
    (anthropic.APIConnectionError(message=LEAK, request=REQUEST), False, ('aikey.test_unreachable', {})),
    (status_error(anthropic.APIStatusError, 500), False, ('aikey.test_status', {'status': 500})),
    (ValueError(LEAK), False, ('aikey.test_failed', {})),
]


@pytest.mark.parametrize('outcome, ok, summary', OUTCOMES, ids=['ok', 'rejected', 'forbidden', 'no-model', 'timeout', 'unreachable', 'status', 'other'])
def test_test_connection_reports_fixed_texts_and_never_500s(store: Store, outcome, ok: bool, summary: tuple[str, dict]):
    # given
    factory = FakeModels(outcome)
    c = TestClient(app(store, '', factory))

    # when
    res = c.post(TEST, json={'key': KEY_B}, headers=AUTH)

    # then
    assert res.status_code == 200
    assert (res.json()['ok'], res.json()['summary']) == (ok, msg(summary[0], **summary[1])) and isinstance(res.json()['ms'], int)
    assert KEY_B not in res.text and 'upstream said' not in res.text
    assert factory.built == [{'api_key': KEY_B, 'timeout': 10, 'max_retries': 0}] and factory.asked == [MODEL] and factory.closed == 1


def test_test_connection_stops_at_the_cap_and_says_when_there_is_no_key(store: Store, monkeypatch):
    # given
    monkeypatch.setattr('skopax.aikey.TEST_CAP_S', 0.2)
    release = threading.Event()
    hung = TestClient(app(store, '', FakeModels(lambda: release.wait(5))))
    bare = TestClient(app(store, '', FakeModels()))

    # when
    began = time.monotonic()
    capped = hung.post(TEST, json={'key': KEY_B}, headers=AUTH)
    took = time.monotonic() - began
    release.set()
    no_key = bare.post(TEST, json={}, headers=AUTH)

    # then
    assert capped.status_code == 200 and (capped.json()['ok'], capped.json()['summary']) == (False, msg('aikey.test_timeout')) and took < 2
    assert no_key.status_code == 200 and (no_key.json()['ok'], no_key.json()['summary']) == (False, msg('aikey.test_no_key'))


def test_test_connection_uses_the_typed_key_or_the_current_one(store: Store):
    # given
    factory = FakeModels()
    c = TestClient(app(store, KEY_A, factory))

    # when
    typed = c.post(TEST, json={'key': KEY_B}, headers=AUTH)
    typed_lines = admin_lines(store)
    configured = c.post(TEST, json={}, headers=AUTH)
    c.put(PATH, json={'key': KEY_B}, headers=AUTH)
    stored = c.post(TEST, json={'key': None}, headers=AUTH)

    # then
    assert [r.json()['ok'] for r in (typed, configured, stored)] == [True, True, True]
    assert [b['api_key'] for b in factory.built] == [KEY_B, KEY_A, KEY_B]
    assert typed_lines == []
    assert admin_lines(store) == [msg('aikey.test_used'), msg('aikey.set'), msg('aikey.test_used')]


@pytest.mark.parametrize('typed', ['short', 'a b' * 10, '\u00e9' * 25], ids=['short', 'space', 'accent'])
def test_key_input_is_validated(store: Store, typed: str):
    # given
    c = TestClient(app(store, KEY_A))

    # when
    res = c.put(PATH, json={'key': typed}, headers=AUTH)

    # then
    assert (res.status_code, res.json()) == (422, {'detail': msg('aikey.invalid')})
    assert store.db['admin_secrets'].count_documents({}) == 0 and c.get('/api/admin/history', headers=AUTH).json() == []
    assert c.app.state.aikey.current() == KEY_A


def test_features_needs_follow_the_admin_key(store: Store):
    # given
    c = TestClient(app(store, '', investigate_mode='shadow'))

    def needs() -> dict[str, list[str]]:
        return {i['id']: i['needs'] for i in c.get('/api/admin/features', headers=AUTH).json()['items']}

    before = needs()

    # when
    c.put(PATH, json={'key': KEY_B}, headers=AUTH)
    after = needs()

    # then
    assert before['investigator'] == [msg('features.needs.anthropic_key')] and msg('features.needs.anthropic_key') in before['self-review']
    assert after['investigator'] == [] and msg('features.needs.anthropic_key') not in after['self-review']


def test_clearing_writes_history_and_activity_once(store: Store):
    # given
    c = TestClient(app(store, KEY_A))
    c.put(PATH, json={'key': KEY_B}, headers=AUTH)

    # when
    first = c.delete(PATH, headers=AUTH)
    again = c.delete(PATH, headers=AUTH)
    history = c.get('/api/admin/history', headers=AUTH).json()

    # then
    assert (first.status_code, again.status_code) == (204, 204)
    assert [(h['key'], h['secret'], h['old'], h['new']) for h in history] == [(KEY_ID, True, 'set', None), (KEY_ID, True, None, 'set')]
    assert admin_lines(store) == [msg('aikey.cleared'), msg('aikey.set')]


def listening(store: Store, factory: Factory):
    s = dataclasses.replace(settings(), anthropic_api_key='', investigate_mode='shadow', slack_listen='shadow')
    slack, _, _ = fake_slack()
    app = create_app(s, store, slack=slack, anthropic_client=FakeAnthropic([]), operators_lookup=lambda email: False, slack_socket=factory, ai_factory=FakeModels())
    slack_instance(app, app_token='xapp-test')
    return app


def listener_card(c: TestClient) -> dict:
    return {i['id']: i for i in c.get('/api/admin/features', headers=AUTH).json()['items']}['slack-listener']


def buttons(store: Store) -> int:
    return len([a for a in store.list_actions(None, 20) if a['type'] == 'post-buttons'])


def test_an_admin_only_key_opens_the_slack_socket_at_the_next_boot(store: Store, monkeypatch):
    # given
    store.save(state(anomaly('k1')), None)
    deaf, heard = Factory(), Factory()
    stopped = []
    with TestClient(listening(store, deaf)) as c:
        monkeypatch.setattr(c.app.state.listener, 'stop', lambda: stopped.append('deaf'))
        deaf_slack = c.get('/api/server').json()['slack']
        before = (deaf_slack['socket'], listener_card(c)['restart'])
        c.post('/api/slack-posts', json={'key': 'k1'}, headers=AUTH)
        deaf_buttons = buttons(store)
        c.put(PATH, json={'key': KEY_B}, headers=AUTH)
        promised = listener_card(c)

    # when
    with TestClient(listening(store, heard)) as c:
        wait_for(lambda: bool(heard.made) and heard.made[0].connected.is_set())
        info = c.get('/api/server').json()['slack']
        card = listener_card(c)
        c.post('/api/slack-posts', json={'key': 'k1'}, headers=AUTH)

    # then
    assert deaf.tokens == [] and before == (False, None) and deaf_buttons == 0 and buttons(store) == 1
    assert 'link' not in deaf_slack and stopped == [], 'a listener that never started shows no link and is not stopped at shutdown'
    assert (promised['needs'], promised['restart']) == ([], msg('features.restart'))
    assert heard.tokens == ['xapp-test'] and info['socket'] is True and info['link']['connected'] is True
    assert (card['needs'], card['restart']) == ([], None)
    assert heard.made[0].closed == 1


def test_the_key_in_use_reads_no_settings(store: Store, monkeypatch):
    # given
    c = TestClient(app(store, KEY_A))

    def no_read(self):
        raise AssertionError('the key in use read the live settings')

    monkeypatch.setattr(Live, 'current', no_read)

    # when
    got = (c.app.state.aikey.current(), c.app.state.aikey.status()['using'])

    # then
    assert got == (KEY_A, 'config')


def test_shutdown_cancels_the_key_refresh_before_the_investigator_is_freed(store: Store, monkeypatch):
    # given
    order = []
    made = app(store, KEY_A)
    aikey = made.state.aikey
    loop = aikey.loop

    async def watched():
        try:
            await loop()
        except asyncio.CancelledError:
            order.append('cancelled')
            raise

    aikey.loop = watched

    # when
    with TestClient(made) as c:
        monkeypatch.setattr(c.app.state.investigator, 'shutdown', lambda: order.append('freed'))

    # then
    assert order == ['cancelled', 'freed']


class Flaky:
    def __init__(self, col, failures: int = 1):
        self.col = col
        self.failures = failures

    def find_one(self, *args, **kwargs):
        if self.failures:
            self.failures -= 1
            raise PyMongoError('the database blinked')
        return self.col.find_one(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self.col, name)


class Racing:
    def __init__(self, col, meanwhile):
        self.col = col
        self.meanwhile = meanwhile

    def find_one(self, *args, **kwargs):
        doc = self.col.find_one(*args, **kwargs)
        self.meanwhile()
        return doc

    def __getattr__(self, name: str):
        return getattr(self.col, name)


def test_one_failed_refresh_does_not_stop_the_key_refresh(store: Store, caplog):
    # given
    store.db['admin_secrets'].insert_one({'_id': KEY_ID, 'value': Sealer('k1').seal(KEY_B)})
    flaky = Flaky(store.db['admin_secrets'])
    aikey = AiKey({'admin_secrets': flaky}, lambda: Sealer('k1'), lambda: '', lambda: None, TunableStore(store.db), every=0.01)

    async def scenario():
        task = asyncio.create_task(aikey.loop())
        deadline = time.monotonic() + 2
        while aikey.current() != KEY_B and time.monotonic() < deadline and not task.done():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    # when
    asyncio.run(scenario())

    # then
    assert flaky.failures == 0 and aikey.current() == KEY_B
    assert any('not read again' in r.getMessage() for r in caplog.records)


def test_a_reseal_never_overwrites_a_key_set_meanwhile(store: Store):
    # given
    col = store.db['admin_secrets']
    col.insert_one({'_id': KEY_ID, 'value': Sealer('meta-key').seal(KEY_B)})
    newer = Sealer('k1').seal(KEY_A)
    aikey = AiKey({'admin_secrets': Racing(col, lambda: col.update_one({'_id': KEY_ID}, {'$set': {'value': newer}}))}, lambda: Sealer('k1'), lambda: '', lambda: 'meta-key', TunableStore(store.db))

    # when
    aikey.refresh()

    # then
    assert col.find_one({'_id': KEY_ID})['value'] == newer and Sealer('k1').open(newer) == KEY_A


def test_a_connection_test_started_right_after_another_ends_is_not_refused(store: Store, monkeypatch):
    # given
    class Lingering(threading.Thread):
        def run(self):
            super().run()
            if self.name == 'aikey-test':
                time.sleep(0.3)

    monkeypatch.setattr('skopax.aikey.threading.Thread', Lingering)
    c = TestClient(app(store, KEY_A, FakeModels()))

    # when
    first = c.post(TEST, json={}, headers=AUTH)
    second = c.post(TEST, json={}, headers=AUTH)

    # then
    assert (first.status_code, first.json()['ok']) == (200, True)
    assert (second.status_code, second.json()['ok']) == (200, True), second.json()


def test_a_second_connection_test_while_one_runs_is_refused(store: Store, monkeypatch):
    # given
    monkeypatch.setattr('skopax.aikey.TEST_CAP_S', 0.2)
    release = threading.Event()
    factory = FakeModels(lambda: release.wait(5))
    c = TestClient(app(store, KEY_A, factory))
    first = c.post(TEST, json={'key': KEY_B}, headers=AUTH)

    # when
    busy = c.post(TEST, json={}, headers=AUTH)
    release.set()
    again = busy
    deadline = time.monotonic() + 2
    while again.status_code == 409 and time.monotonic() < deadline:
        time.sleep(0.02)
        again = c.post(TEST, json={'key': KEY_B}, headers=AUTH)

    # then
    assert first.json()['summary'] == msg('aikey.test_timeout')
    assert (busy.status_code, busy.json()) == (409, {'detail': msg('aikey.test_busy')})
    assert again.status_code == 200 and again.json()['ok'] is True
    assert len(factory.built) == 2 and admin_lines(store) == []


def test_a_refused_key_is_not_echoed_under_a_root_path(store: Store):
    # given
    c = TestClient(app(store, KEY_A), root_path='/x')

    # when
    res = c.put('/x' + PATH, json={'key': [KEY_B]}, headers=AUTH)

    # then
    assert (res.status_code, res.json()) == (422, {'detail': msg('aikey.invalid')}) and KEY_B not in res.text


def test_the_key_routes_answer_their_fixed_texts_whatever_the_error_carries(store: Store, monkeypatch):
    # given
    class Hinting(InvalidKey):
        def __str__(self):
            return LEAK

    class Busy(KeyTestBusy):
        def __str__(self):
            return LEAK

    def raising(error):
        def run(*args, **kwargs):
            raise error
        return run

    c = TestClient(app(store, KEY_A))

    # when
    monkeypatch.setattr(c.app.state.aikey, 'set', raising(Hinting()))
    put = c.put(PATH, json={'key': KEY_B}, headers=AUTH)
    monkeypatch.setattr(c.app.state.aikey, 'test', raising(Hinting()))
    tested = c.post(TEST, json={'key': KEY_B}, headers=AUTH)
    monkeypatch.setattr(c.app.state.aikey, 'test', raising(Busy()))
    busy = c.post(TEST, json={'key': KEY_B}, headers=AUTH)

    # then
    assert (put.status_code, put.json()) == (422, {'detail': msg('aikey.invalid')})
    assert (tested.status_code, tested.json()) == (422, {'detail': msg('aikey.invalid')})
    assert (busy.status_code, busy.json()) == (409, {'detail': msg('aikey.test_busy')})
    assert all(KEY_B not in r.text for r in (put, tested, busy))


def test_a_pod_that_stops_mid_run_frees_the_run_after_the_key_was_cleared(store: Store, monkeypatch):
    # given
    monkeypatch.setattr(anthropic, 'Anthropic', lambda **kw: FakeAnthropic([]))
    card = 'log|checkout-service|ERROR|boom'
    with TestClient(app(store, '', investigate_mode='live')) as c:
        c.put(PATH, json={'key': KEY_B}, headers=AUTH)
        seed(store, anomaly(card, triage='investigating', investigator={'runId': 'r1', 'at': '2026-10-10T09:00:00Z'}))
        act = ask_for(store, 'why?')
        store.update_action(act['id'], 'processing', None, 'requested')
        store.add_investigation({'id': 'r1', 'mode': 'live', 'keys': [card], 'startedAt': '2026-10-10T09:00:00Z', 'outcome': 'running'})
        inv = c.app.state.investigator
        inv._runs['r1'] = {'claimed': [card], 'action': store.get_action(act['id']), 'lease': ('investigator-room-0', 'room-owner')}

        # when
        c.delete(PATH, headers=AUTH)
        cleared = inv.enabled

    # then
    run = store.investigations.find_one({'_id': 'r1'})
    assert cleared is False
    assert run['outcome'] == 'abandoned' and store.get_action(act['id'])['status'] == 'requested' and store.anomaly(card).get('triage') is None


def once(value: str):
    values = iter([value])
    return lambda: next(values, '')


def test_a_key_lost_after_the_enabled_check_means_not_configured(store: Store, caplog):
    # given
    s = dataclasses.replace(settings(), anthropic_api_key='', investigate_mode='live')
    gate = Gate(store, s, key=once(KEY_B))
    talk = SmallTalk(store, s, key=once(KEY_B))
    review = SelfReview(store, s, lambda now: {}, lambda text: {}, key=lambda: '')
    inv = Investigator(store, sources(), Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}), s, clock=lambda: NOW, key=once(KEY_B))
    act = ask_for(store, 'why is checkout-service slow?')

    # when
    with pytest.raises(GateDown):
        gate.check('x')
    said = talk.reply('x', 'page')
    reviewed = review.run(NOW)
    ran = inv.run_once(NOW)

    # then
    assert said is None and not any('call failed' in r.getMessage() for r in caplog.records)
    assert reviewed is None and msg('selfreview.no_key') in [a['text'] for a in store.list_activity(None, 10)]
    assert ran is None and store.get_action(act['id'])['status'] == 'requested' and store.list_investigations(None, 5) == []


def unbuildable(key: str):
    raise RuntimeError('the client could not be built')


def test_a_client_that_cannot_be_built_fails_like_an_api_error(store: Store, caplog):
    # given
    s = dataclasses.replace(settings(), anthropic_api_key='', investigate_mode='live')
    gate = Gate(store, s, key=lambda: KEY_B)
    talk = SmallTalk(store, s, key=lambda: KEY_B)
    review = SelfReview(store, s, lambda now: {}, lambda text: {}, key=lambda: KEY_B)
    inv = Investigator(store, sources(), Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}), s, clock=lambda: NOW, key=lambda: KEY_B)
    for consumer in (gate, talk, review, inv):
        consumer.box.make = unbuildable
    act = ask_for(store, 'why is checkout-service slow?')

    # when
    with pytest.raises(GateDown):
        gate.check('x')
    said = talk.reply('x', 'page')
    review._safe_run(NOW)
    ran = inv.run_once(NOW)

    # then
    assert said is None
    assert 'Self-review failed: RuntimeError' in [a['text'] for a in store.list_activity(None, 20)]
    assert ran is not None and ran['outcome'] == 'failed' and store.get_action(act['id'])['status'] == 'failed'
    assert [r['outcome'] for r in store.list_investigations(None, 5)] == ['failed']
    assert all(KEY_B not in r.getMessage() for r in caplog.records)


def test_the_status_says_where_a_config_key_comes_from_and_never_more(store: Store):
    # given
    configured = TestClient(app(store, KEY_A))
    bare = TestClient(app(store))

    # when
    from_config = configured.get(PATH, headers=AUTH).json()
    nothing = bare.get(PATH, headers=AUTH).json()
    set_here = configured.put(PATH, json={'key': KEY_B}, headers=AUTH).json()

    # then
    assert (from_config['using'], from_config['note']) == ('config', msg('aikey.from_config'))
    assert (nothing['using'], nothing['note']) == (None, None)
    assert (set_here['using'], set_here['note']) == ('admin', None)
    assert all(KEY_A not in str(v) and KEY_B not in str(v) for v in (from_config, nothing, set_here))


def test_no_openai_route_exists(store: Store):
    # given
    c = TestClient(app(store, KEY_A))

    # when
    paths = [getattr(route, 'path', '') for route in c.app.routes]
    got = c.get('/api/admin/openai-key', headers=AUTH)

    # then
    assert [p for p in paths if 'openai' in p.lower()] == []
    assert got.status_code == 404
