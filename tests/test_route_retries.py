import json
import threading

import pytest
import time

from fastapi.testclient import TestClient

from skopax import app as app_module
from skopax.app import create_app
from skopax.engine import PassResult, iso as iso_at
from skopax.kube import Kube
from skopax.remote import HttpError, Reply
from skopax.store import Store
from tests.conftest import AUTH, settings, state
from tests.plugins.hook_plugin import Hook, hook_plugin
from tests.teams_kit import SEO, TECH, Transport, add_route, open_card, route, routes_of, tag
from tests.test_routes import outbound_threads

TRIES = 6
NOW = 1_794_240_000.0
SAID = {'text': 'We recommend adding a redirect for the old spring sale page.', 'by': 'pod', 'at': '2026-11-09T16:00:00Z'}


class Gone(Transport):
    def __init__(self, code: int = 404):
        super().__init__()
        self.code = code
        self.down = True

    def __call__(self, method, url, headers, data, timeout):
        super().__call__(method, url, headers, data, timeout)
        if self.down:
            raise HttpError(self.code, url, 'gone')
        return Reply(None, {})


def test_a_failing_tag_route_backs_off_gives_up_after_six_tries_and_never_moves_the_state_rev(store: Store):
    # given
    t = Gone(404)
    routes = routes_of(store, t, route('seo', SEO, tags='seo'), old=False)
    store.save(state(open_card('log|a|ERROR|one', tags=tag('seo'), business=SAID), open_card('log|a|ERROR|two', tags=tag('seo'), business=SAID)), None)
    rev = store.rev()['rev']

    # when
    tried_at = []
    for offset in range(0, 7200, 30):
        before = len(t.calls)
        routes.deliver(store.load()['anomalies'], NOW + offset)
        if len(t.calls) > before:
            tried_at.append((offset, len(t.calls) - before))

    # then
    assert tried_at == [(0, 2), (30, 2), (150, 2), (390, 2), (870, 2), (1830, 2)], 'next pass, then 2, 4, 8 and 16 passes of a minute'
    assert len(t.calls) == 2 * TRIES
    assert store.rev()['rev'] == rev, 'taking a claim back is bookkeeping, not a state change'
    assert sorted((r['key'], r['tries'], r['gaveUp'], r['reason']) for r in store.route_retries()) == [('log|a|ERROR|one', 6, True, 'HTTP 404'), ('log|a|ERROR|two', 6, True, 'HTTP 404')]
    assert routes.gave_up() == [{'id': 'teams/seo', 'plugin': 'Microsoft Teams', 'route': 'seo', 'count': 2, 'reason': 'HTTP 404', 'tries': 6}]
    assert routes.failure('teams', 'seo') == 'error: seo: HTTP 404'


def test_a_failing_key_route_backs_off_the_same_way_and_a_card_sent_by_hand_meanwhile_is_not_posted_again(store: Store):
    # given
    t = Gone(403)
    routes = routes_of(store, t, route('tech', TECH, keys='log|*'), old=False, min_sev='warn')
    store.save(state(open_card('log|a|ERROR|one'), open_card('log|a|ERROR|two')), None)
    cards = store.load()['anomalies']

    # when
    routes.deliver([], NOW, pushed=cards)
    routes.deliver([], NOW + 30)
    held = routes.deliver([], NOW + 60)
    t.down = False
    routes.send('log|a|ERROR|one', 'teams/tech', 'alice@example.com')
    after_hand = len(t.calls)
    retried = routes.deliver([], NOW + 200)

    # then
    assert held == 0 and after_hand == 3, 'two failed tries, then a wait; the hand send is the third call'
    last = json.dumps(t.calls[-1][1])
    assert retried == 1 and len(t.calls) == 4 and 'ERROR%7Ctwo' in last and 'ERROR%7Cone' not in last
    assert [r['key'] for r in store.route_retries()] == [], 'a delivered card and a card sent by hand both drop their retry record'
    assert routes.failure('teams', 'tech') is None


def test_a_busy_slot_does_not_use_up_a_try(store: Store):
    # given
    gate = threading.Event()
    hook = Hook(hold=gate)
    plugin = hook_plugin(hook)
    store.save(state(open_card('log|a|ERROR|one', tags=tag('seo'), business=SAID)), None)
    routes = routes_of(store, Transport(), (plugin, 'web', {'url': 'https://hooks.example.com/web', 'tags': 'seo'}), plugins=(plugin,), timeout=0.2)

    # when
    routes.deliver(store.load()['anomalies'], NOW)
    routes.deliver(store.load()['anomalies'], NOW + 30)
    tries = [r['tries'] for r in store.route_retries()]
    gate.set()
    outbound_threads()

    # then
    assert tries == [1], 'the hung post is one try; the call refused as busy is not'


def test_a_passing_test_lets_a_given_up_route_try_again(store: Store):
    # given
    t = Gone(404)
    routes = routes_of(store, t, route('seo', SEO, tags='seo'), old=False)
    store.save(state(open_card('log|a|ERROR|one', tags=tag('seo'), business=SAID)), None)
    for offset in range(0, 7200, 30):
        routes.deliver(store.load()['anomalies'], NOW + offset)
    spent = len(t.calls)

    # when
    t.down = False
    still = routes.deliver(store.load()['anomalies'], NOW + 7200)
    routes.reset('teams', 'seo')
    healed = routes.deliver(store.load()['anomalies'], NOW + 7230)

    # then
    assert spent == TRIES and still == 0
    assert healed == 1 and routes.gave_up() == [] and routes.failure('teams', 'seo') is None


def test_the_admin_sees_a_failing_route_on_its_instance_and_a_notice_once_it_gives_up(store: Store, tmp_path):
    # given
    t = Gone(404)
    found = open_card('log|a|ERROR|one', tags=tag('seo'), business=SAID) | {'src': 'log', 'sig': 'err', 'ns': 'shop', 'sub': '', 'now': 5, 'd1': None, 'd7': None, 'unit': '',
                                                                           'short': 'x', 'slack': 1, 'ranKey': 'log'}
    store.save({**state(found), 'initialized': True}, None)

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=t)) as c:
        add_route(c, 'seo', SEO, tags='seo')
        c.app.state.server_apply([PassResult('log', True, 'ok', [found], {}, ('log',))], NOW, None)
        assert c.app.state.routes.drain(5)
        health = next(p for p in c.get('/api/admin/plugins', headers=AUTH).json()['plugins'] if p['id'] == 'teams')['health']
        early = c.get('/api/admin/notices', headers=AUTH).json()['items']
        for offset in range(30, 7200, 30):
            c.app.state.routes.deliver(store.load()['anomalies'], time.time() + offset)
        late = c.get('/api/admin/notices', headers=AUTH).json()['items']
        tested = c.post('/api/admin/plugins/teams/test', json={'name': 'seo', 'values': {}}, headers=AUTH).json()
        cleared = c.get('/api/admin/notices', headers=AUTH).json()['items']

    # then
    assert health['state'] == 'error' and health['instances'][0]['text'] == 'error: seo: HTTP 404'
    assert [i['id'] for i in early if i['id'].startswith('route')] == []
    [notice] = [i for i in late if i['id'].startswith('route')]
    assert notice['id'] == 'route-gave-up:teams/seo' and 'seo' in notice['text'] and 'HTTP 404' in notice['text']
    assert tested['ok'] is False and [i for i in cleared if i['id'].startswith('route')] == [notice], 'a failing Test keeps the notice'


class Slow(Transport):
    def __init__(self, gate: threading.Event):
        super().__init__()
        self.gate = gate

    def __call__(self, method, url, headers, data, timeout):
        self.gate.wait(5)
        return super().__call__(method, url, headers, data, timeout)


def test_a_pass_hands_its_cards_to_the_route_worker_and_returns_without_waiting_for_the_post(store: Store, tmp_path):
    # given
    gate = threading.Event()
    t = Slow(gate)
    found = open_card('log|a|ERROR|one', 'crit') | {'src': 'log', 'sig': 'err', 'ns': 'shop', 'sub': '', 'now': 5, 'd1': None, 'd7': None, 'unit': '', 'short': 'x', 'slack': 1,
                                                    'ranKey': 'log'}
    store.save({**state(), 'initialized': True}, None)

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=t)) as c:
        add_route(c, 'tech', TECH, keys='log|*')
        started = time.monotonic()
        c.app.state.server_apply([PassResult('log', True, 'ok', [found], {}, ('log',))], NOW, None)
        took = time.monotonic() - started
        gate.set()
        assert c.app.state.routes.drain(5)

    # then
    assert took < 1 and [u for u, _ in t.calls] == [TECH]


def test_a_route_whose_webhook_changed_is_not_held_by_the_retries_of_the_old_one(store: Store):
    # given
    t = Gone(404)
    routes = routes_of(store, t, route('seo', SEO, tags='seo'), old=False)
    store.save(state(open_card('log|a|ERROR|one', tags=tag('seo'), business=SAID)), None)
    for offset in range(0, 7200, 30):
        routes.deliver(store.load()['anomalies'], NOW + offset)
    store.route_tries.update_many({}, {'$set': {'link': 'an-older-webhook'}})
    store.route_health.update_many({}, {'$set': {'link': 'an-older-webhook'}})

    # when
    t.down = False
    sent = routes.deliver(store.load()['anomalies'], NOW + 7200)

    # then
    assert sent == 1 and routes.gave_up() == [] and routes.failure('teams', 'seo') is None


@pytest.mark.parametrize('heal', ['test', 'webhook'])
def test_a_key_route_that_gave_up_posts_its_cards_again_after_a_passing_test_or_a_new_webhook(store: Store, heal):
    # given
    t = Gone(404)
    routes = routes_of(store, t, route('tech', TECH, keys='log|*'), old=False, min_sev='warn')
    store.save(state(open_card('log|a|ERROR|one')), None)
    routes.deliver([], NOW, pushed=store.load()['anomalies'])
    for offset in range(30, 7200, 30):
        routes.deliver([], NOW + offset)
    spent = len(t.calls)

    # when
    t.down = False
    if heal == 'test':
        routes.reset('teams', 'tech')
    else:
        store.route_tries.update_many({}, {'$set': {'link': 'an-older-webhook'}})
    sent = routes.deliver([], NOW + 7200)

    # then
    assert spent == TRIES and sent == 1 and [r['key'] for r in store.route_retries()] == []


def test_a_page_save_loaded_while_a_post_was_failing_does_not_bring_its_claim_back(store: Store):
    # given
    key = 'log|a|ERROR|one'
    store.save(state(open_card(key, tags=tag('seo'), business=SAID)), None)
    post = {'route': 'seo', 'plugin': 'teams', 'at': '2026-11-09T16:00:00Z', 'by': 'server'}
    store.teams_claim(key, 'seo', '', post, plugin='teams', rev=False)
    page = store.load()
    store.teams_unclaim(key, post, rev=False)

    # when
    store.save_state(page, page['rev'])

    # then
    assert store.anomaly(key).get('teamsPosts') == [], 'the page never writes the posts list, so its stale copy of a failed claim is not kept'


def test_a_due_retry_posts_once_when_two_tag_changes_both_ask_for_delivery(store: Store, tmp_path, monkeypatch):
    # given
    t = Gone(404)
    found = open_card('log|a|ERROR|one', 'crit') | {'src': 'log', 'sig': 'err', 'ns': 'shop', 'sub': '', 'now': 5, 'd1': None, 'd7': None, 'unit': '', 'short': 'x', 'slack': 1,
                                                    'ranKey': 'log'}
    store.save({**state(), 'initialized': True}, None)
    posting, release = threading.Event(), threading.Event()
    record = store.teams_delivered

    def held(key, post):
        if not posting.is_set():
            posting.set()
            release.wait(5)
        return record(key, post)

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=t)) as c:
        add_route(c, 'tech', TECH, keys='log|*')
        c.app.state.server_apply([PassResult('log', True, 'ok', [found], {}, ('log',))], time.time(), None)
        assert c.app.state.routes.drain(5)
        failed = len(t.calls)
        store.route_tries.update_many({}, {'$set': {'due': 0}})
        t.down = False
        monkeypatch.setattr(store, 'teams_delivered', held)
        first = threading.Thread(target=lambda: c.post('/api/tags', json={'key': found['key'], 'tag': 'developer', 'on': True}, headers=AUTH))
        first.start()
        assert posting.wait(5)
        c.post('/api/tags', json={'key': found['key'], 'tag': 'developer', 'on': False}, headers=AUTH)
        release.set()
        first.join(5)
        assert c.app.state.routes.drain(5)

    # then
    assert failed == 1 and len(t.calls) == 2, 'the due retry went out once, not once per delivery'
    assert [p['by'] for p in store.anomaly(found['key'])['teamsPosts']] == ['token:bootstrap'], 'the worker keeps who asked for the delivery'


def test_a_passing_test_of_another_webhook_keeps_the_stored_route_s_retries_and_failure(store: Store, tmp_path):
    # given
    t = Transport(fail='sig=SEO')
    found = open_card('log|a|ERROR|one', tags=tag('seo'), business=SAID) | {'src': 'log', 'sig': 'err', 'ns': 'shop', 'sub': '', 'now': 5, 'd1': None, 'd7': None, 'unit': '',
                                                                           'short': 'x', 'slack': 1, 'ranKey': 'log'}
    store.save({**state(found), 'initialized': True}, None)

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=t)) as c:
        add_route(c, 'seo', SEO, tags='seo')
        c.app.state.server_apply([PassResult('log', True, 'ok', [found], {}, ('log',))], NOW, None)
        assert c.app.state.routes.drain(5)
        other = c.post('/api/admin/plugins/teams/test', json={'name': 'seo', 'values': {'webhook': TECH}}, headers=AUTH).json()
        kept = ([r['tries'] for r in store.route_retries()], c.app.state.routes.failure('teams', 'seo'))
        t.fail = None
        saved = c.post('/api/admin/plugins/teams/test', json={'name': 'seo', 'values': {'webhook': SEO, 'tags': 'seo'}}, headers=AUTH).json()
        cleared = ([(r['tries'], r['due']) for r in store.route_retries()], c.app.state.routes.failure('teams', 'seo'))

    # then
    assert other['ok'] is True and kept == ([1], 'error: seo: HTTP 400'), 'a Test of form values the admin did not save says nothing about the stored webhook'
    assert saved['ok'] is True and cleared == ([(0, 0)], None), 'a Test of the stored webhook lets the route try again'


class Hung(Transport):
    def __init__(self, gate: threading.Event):
        super().__init__()
        self.gate = gate

    def __call__(self, method, url, headers, data, timeout):
        self.gate.wait(20)
        return super().__call__(method, url, headers, data, timeout)


def pushed_card(key: str) -> dict:
    at = iso_at(time.time() - 60)
    return open_card(key, 'crit', firstSeen=at, lastSeen=at, pushedAt=at, pushedSev='crit', pushedPeak=1)


def test_a_push_queued_at_shutdown_is_kept_as_a_retry_and_the_next_start_posts_it_once(store: Store, tmp_path, monkeypatch):
    # given
    monkeypatch.setattr(app_module, 'ROUTES_STOP_S', 0.5)
    gate = threading.Event()
    t = Hung(gate)
    first, second = pushed_card('log|a|ERROR|first'), pushed_card('log|a|ERROR|second')

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=t)) as c:
        add_route(c, 'tech', TECH, keys='log|*')
        for initialized, cards in ((False, []), (True, []), (True, [first]), (True, [first, second])):
            assert c.put('/api/state', json={'initialized': initialized, 'anomalies': cards, 'sweptIso': iso_at(time.time())}, headers=AUTH).status_code == 200
        old = c.app.state.routes
    kept = sorted((r['key'], r['tries'], r['gaveUp']) for r in store.route_retries())
    gate.set()
    assert old.drain(5)
    posted_before = [json.dumps(b) for _, b in t.calls]
    fresh = Transport()
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=fresh)) as c:
        sent = c.app.state.routes.deliver([], time.time())

    # then
    assert kept == [('log|a|ERROR|first', 0, False), ('log|a|ERROR|second', 0, False)], 'the post in flight and the queued push are both on record, and neither used a try'
    assert len(posted_before) == 1 and 'ERROR%7Cfirst' in posted_before[0], 'the post in flight still went out; the queued push did not'
    assert sent == 1 and len(fresh.calls) == 1 and 'ERROR%7Csecond' in json.dumps(fresh.calls[0][1]), 'the next start posts the queued push once and not the one already sent'
    assert store.route_retries() == []


def test_a_hung_route_post_holds_shutdown_only_for_the_drain_bound(store: Store, tmp_path, monkeypatch):
    # given
    bound = app_module.ROUTES_STOP_S
    monkeypatch.setattr(app_module, 'ROUTES_STOP_S', 0.5)
    gate = threading.Event()
    t = Hung(gate)

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=t)) as c:
        add_route(c, 'tech', TECH, keys='log|*')
        for initialized, cards in ((False, []), (True, []), (True, [pushed_card('log|a|ERROR|first')])):
            c.put('/api/state', json={'initialized': initialized, 'anomalies': cards, 'sweptIso': iso_at(time.time())}, headers=AUTH)
        old = c.app.state.routes
        started = time.monotonic()
    took = time.monotonic() - started
    gate.set()
    assert old.drain(5)

    # then
    assert bound == 5, 'shutdown waits at most 5 s for the route worker'
    assert 0.4 < took < 8, 'the drain waits for its bound, not for the 10 s post timeout'


class Turns(Transport):
    def __init__(self, count: int):
        super().__init__()
        self.entered = [threading.Event() for _ in range(count)]
        self.gates = [threading.Event() for _ in range(count)]

    def __call__(self, method, url, headers, data, timeout):
        turn = len(self.calls)
        out = super().__call__(method, url, headers, data, timeout)
        if turn < len(self.gates):
            self.entered[turn].set()
            self.gates[turn].wait(20)
        return out


def test_every_worker_batch_not_finished_at_stop_is_kept_as_a_retry(store: Store):
    # given
    t = Turns(2)
    routes = routes_of(store, t, route('tech', TECH, keys='log|*'), old=False, timeout=30)
    routes.autostart = True
    store.save(state(*(open_card(f'log|a|ERROR|{k}', 'crit') for k in 'zab')), None)
    card = {a['key'].rsplit('|', 1)[1]: a for a in store.load()['anomalies']}
    routes.later([], NOW, [card['z']], by='z')
    assert t.entered[0].wait(5)
    routes.later([], NOW, [card['a']], by='a')
    routes.later([], NOW, [card['b']], by='b')
    t.gates[0].set()
    assert t.entered[1].wait(5)

    # when
    kept = routes.stop(0.3)
    t.gates[1].set()
    assert routes.drain(5)

    # then
    assert kept == 2 and sorted(r['key'] for r in store.route_retries()) == ['log|a|ERROR|a', 'log|a|ERROR|b'], 'the batch in flight and the one waiting behind it are both kept'
    assert len(t.calls) == 2, 'a batch taken over by the stop is not posted after it'


def test_a_retried_card_and_a_new_push_to_the_same_route_in_one_pass_are_both_counted(store: Store):
    # given
    t = Gone(403)
    routes = routes_of(store, t, route('tech', TECH, keys='log|*'), old=False, min_sev='warn')
    store.save(state(open_card('log|a|ERROR|one'), open_card('log|a|ERROR|two')), None)
    one, two = store.load()['anomalies']
    routes.deliver([], NOW, pushed=[one])
    t.down = False

    # when
    sent = routes.deliver([], NOW + 30, pushed=[two])

    # then
    assert sent == 2, 'the retry of one and the push of two both went to teams/tech'
    assert [r['key'] for r in store.route_retries()] == []


def test_a_retry_is_dropped_once_its_card_is_marked_already_active(store: Store):
    # given
    t = Gone(403)
    routes = routes_of(store, t, route('tech', TECH, keys='log|*'), old=False, min_sev='warn')
    store.save(state(open_card('log|a|ERROR|one')), None)
    routes.deliver([], NOW, pushed=store.load()['anomalies'])
    routes.deliver([], NOW + 30)
    store.anomalies.update_one({'_id': 'log|a|ERROR|one'}, {'$set': {'alreadyActive': {'since': '2026-10-01T00:00:00Z', 'source': 'Zabbix'}}})
    t.down = False
    tried = len(t.calls)

    # when
    sent = routes.deliver([], NOW + 600)

    # then
    assert sent == 0 and len(t.calls) == tried
    assert store.route_retries() == []
