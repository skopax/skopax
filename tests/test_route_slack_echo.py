import dataclasses
import datetime as dt
import logging
import threading
import time

import mongomock
import pytest
from pymongo.errors import PyMongoError
from fastapi.testclient import TestClient

from skopax import tunables
from skopax.app import create_app
from skopax.autosend import draft_hash
from skopax.config import Settings, load_settings
from skopax.kube import Kube
from skopax.messages import msg
from skopax.plugins import teams
from skopax.registry import Registry
from skopax.routes import Echo, Route
from skopax.slack import Slack
from skopax.store import Store, iso, utc_now
from tests.conftest import AUTH, settings, state
from tests.fake_slack import FakeSlackApi
from tests.plugins.base import minimal
from tests.plugins.hook_plugin import Hook, hook_plugin
from tests.teams_kit import AT, BIZDEV, SEO, TECH, Transport, add_route, open_card, route, routes_of, tag

KEY = 'log|checkout-service|ERROR|boom'
NOW = 1_794_240_000.0
SAID = {'text': 'We recommend adding a redirect for the old spring sale page.', 'why': 'Visitors from the old campaign land on an empty page.', 'by': 'pod', 'at': AT}
INFO = {'text': 'Newsletter sign-ups fail right now; engineering is fixing it.', 'kind': 'info', 'by': 'pod', 'at': AT}
THREAD = {'channel': 'C123', 'ts': '1791900000.000001', 'at': AT}


def outbound_threads() -> None:
    for t in threading.enumerate():
        if t.name == 'outbound':
            t.join(5)


class Fake:
    def __init__(self, store: Store, configured: bool = True, fail_say: bool = False, gate: threading.Event | None = None, slow: str | None = None, open_: bool = True, fail_post: bool = False):
        self.store = store
        self.configured = configured
        self.fail_say = fail_say
        self.gate = gate
        self.slow = slow
        self.open = open_
        self.fail_post = fail_post
        self.attempts: list[str] = []
        self.posted: list[str] = []
        self.said: list[tuple[dict, str]] = []

    def post(self, key: str) -> dict | None:
        if not self.configured:
            return None
        card = self.store.anomaly(key)
        if (card.get('slackPost') or {}).get('ts'):
            return card['slackPost']
        self.attempts.append(key)
        if self.fail_post:
            raise RuntimeError('slack refused secret-token-123')
        self.posted.append(key)
        self.store.slack_posted(key, THREAD)
        return self.store.anomaly(key)['slackPost']

    def ready(self, card: dict) -> bool:
        return self.configured and self.open and not card.get('muted')

    def say(self, post: dict, text: str) -> None:
        if self.fail_say:
            raise RuntimeError('slack said no secret-token-123')
        if self.gate is not None and (self.slow is None or post['ts'] == self.slow):
            self.gate.wait()
        self.said.append((post, text))

    @property
    def echo(self) -> Echo:
        return Echo(self.post, self.say, self.ready)


def at(routes, when: float) -> None:
    routes.clock = lambda: when


def seed(store: Store, *cards: dict) -> None:
    store.save(state(*cards), None)


def echoing(store: Store, fake: Fake, *specs, core: bool = False, transport: Transport | None = None, plugins=(teams.PLUGIN,), **values):
    routes = routes_of(store, transport or Transport(), *specs, plugins=plugins, **values)
    routes.settings = dataclasses.replace(routes.settings, routes_slack_echo=core)
    routes.echo = fake.echo
    return routes


def seo_info(key: str = KEY, **extra) -> dict:
    return open_card(key, 'warn', **({'tags': tag('seo')} | extra), business=INFO)


def with_echo(spec: tuple[str, dict], mode: str) -> tuple[str, dict]:
    return spec[0], {**spec[1], 'slack-echo': mode}


def test_default_off_posts_nothing_to_slack_and_teams_posts_as_before(store: Store):
    # given
    seed(store, seo_info())
    fake, t = Fake(store), Transport()
    routes = echoing(store, fake, route('seo', SEO, tags='seo'), transport=t)

    # when
    sent = routes.deliver(store.load()['anomalies'], NOW, store.load()['anomalies'])

    # then
    assert sent == 1 and [u for u, _ in t.calls] == [SEO]
    assert fake.posted == [] and fake.said == []


@pytest.mark.parametrize('core, mode, echoed', [(True, '', True), (True, 'default', True), (True, 'off', False), (False, 'on', True), (False, '', False), (False, 'off', False), (True, 'on', True),
                                                (False, 'nonsense', False)])
def test_the_core_setting_is_the_default_and_the_route_instance_overrides_it(store: Store, core, mode, echoed):
    # given
    seed(store, seo_info())
    fake = Fake(store)
    routes = echoing(store, fake, with_echo(route('seo', SEO, tags='seo'), mode) if mode else route('seo', SEO, tags='seo'), core=core)

    # when
    routes.deliver(store.load()['anomalies'], NOW, store.load()['anomalies'])

    # then
    assert (fake.posted == [KEY] and len(fake.said) == 1) is echoed
    assert echoed or (fake.posted == [] and fake.said == [])


def test_the_line_names_the_route_the_plugin_and_the_matching_tags(store: Store):
    # given
    seed(store, seo_info(tags=tag('seo', 'developer')))
    fake = Fake(store)
    routes = echoing(store, fake, route('seo-team', SEO, tags='seo, developer'), core=True)

    # when
    routes.deliver(store.load()['anomalies'], NOW, store.load()['anomalies'])

    # then
    assert fake.said == [(THREAD, 'Sent to seo-team on Microsoft Teams (tags: seo, developer)')]
    assert fake.said[0][1] == msg('routes.echo_line', routes=msg('routes.echo_item', route='seo-team', plugin='Microsoft Teams', tags=msg('routes.echo_tags', tags='seo, developer')))


def test_one_tag_reads_as_tag_and_a_key_route_names_no_tag(store: Store):
    # given
    seed(store, seo_info(), open_card('log|a|ERROR|keyed', 'crit'))
    fake = Fake(store)
    routes = echoing(store, fake, route('seo-team', SEO, tags='seo'), route('tech', TECH, keys='log|a|*'), core=True)

    # when
    routes.deliver(store.load()['anomalies'], NOW, store.load()['anomalies'])

    # then
    assert sorted(t for _, t in fake.said) == ['Sent to seo-team on Microsoft Teams (tag: seo)', 'Sent to tech on Microsoft Teams']
    assert fake.posted.count(KEY) == 1 and len(fake.posted) == 2


def test_a_card_matching_two_routes_gets_one_line_that_names_both(store: Store):
    # given
    seed(store, open_card(KEY, 'warn', tags=tag('seo', 'developer'), business=SAID))
    fake = Fake(store)
    routes = echoing(store, fake, route('seo', SEO, tags='seo'), route('devs', TECH, tags='developer'), core=True)

    # when
    routes.deliver(store.load()['anomalies'], NOW, store.load()['anomalies'])

    # then
    assert fake.posted == [KEY] and len(fake.said) == 1
    assert fake.said[0][1] == 'Sent to devs on Microsoft Teams (tag: developer); seo on Microsoft Teams (tag: seo)'


def test_only_the_routes_that_echo_are_named(store: Store):
    # given
    seed(store, open_card(KEY, 'warn', tags=tag('seo', 'developer'), business=SAID))
    fake = Fake(store)
    routes = echoing(store, fake, with_echo(route('seo', SEO, tags='seo'), 'off'), route('devs', TECH, tags='developer'), core=True)

    # when
    routes.deliver(store.load()['anomalies'], NOW, store.load()['anomalies'])

    # then
    assert [t for _, t in fake.said] == ['Sent to devs on Microsoft Teams (tag: developer)']


def test_a_card_that_already_has_a_slack_post_is_not_posted_again_but_the_line_still_goes_in_its_thread(store: Store):
    # given
    seed(store, seo_info(slackPost={'channel': 'C9', 'ts': '1791800000.000009', 'at': AT}))
    fake = Fake(store)
    routes = echoing(store, fake, route('seo', SEO, tags='seo'), core=True)

    # when
    routes.deliver(store.load()['anomalies'], NOW, store.load()['anomalies'])

    # then
    assert fake.posted == [] and fake.said == [({'channel': 'C9', 'ts': '1791800000.000009', 'at': AT}, 'Sent to seo on Microsoft Teams (tag: seo)')]


def test_a_repeat_post_to_the_same_route_adds_no_line_but_a_new_route_does(store: Store):
    # given
    seed(store, open_card(KEY, 'warn', tags=tag('seo', 'developer'), business=SAID))
    fake, t = Fake(store), Transport()
    echoing(store, fake, route('seo', SEO, tags='seo'), core=True, transport=t, repeat_days=0).deliver(store.load()['anomalies'], NOW, [])

    # when
    seed(store, open_card(KEY, 'warn', tags=tag('seo', 'developer'), business=SAID, firstSeen='2026-11-12T00:00:00Z'))
    echoing(store, fake, route('seo', SEO, tags='seo'), core=True, transport=t, repeat_days=0).deliver(store.load()['anomalies'], NOW + 3 * 86400, [])
    echoing(store, fake, route('seo', SEO, tags='seo'), route('devs', TECH, tags='developer'), core=True, transport=t, repeat_days=0).deliver(store.load()['anomalies'], NOW + 3 * 86400, [])

    # then
    assert [u for u, _ in t.calls] == [SEO, SEO, TECH]
    assert [text for _, text in fake.said] == ['Sent to seo on Microsoft Teams (tag: seo)', 'Sent to devs on Microsoft Teams (tag: developer)']
    assert fake.posted == [KEY]


def test_a_teams_failure_posts_nothing_on_slack(store: Store):
    # given
    seed(store, seo_info())
    fake = Fake(store)
    routes = echoing(store, fake, route('seo', SEO, tags='seo'), core=True, transport=Transport(fail='sig=SEO'))

    # when
    sent = routes.deliver(store.load()['anomalies'], NOW, store.load()['anomalies'])

    # then
    assert sent == 0 and fake.posted == [] and fake.said == []


def test_only_the_route_that_posted_is_echoed(store: Store):
    # given
    seed(store, open_card(KEY, 'warn', tags=tag('seo', 'developer'), business=SAID))
    fake = Fake(store)
    routes = echoing(store, fake, route('seo', SEO, tags='seo'), route('devs', TECH, tags='developer'), core=True, transport=Transport(fail='sig=TECH'))

    # when
    routes.deliver(store.load()['anomalies'], NOW, store.load()['anomalies'])

    # then
    assert [t for _, t in fake.said] == ['Sent to seo on Microsoft Teams (tag: seo)']


def test_a_slack_failure_never_undoes_or_retries_the_teams_post_and_leaks_no_text(store: Store, caplog):
    # given
    seed(store, seo_info())
    fake, t = Fake(store, fail_say=True), Transport()
    routes = echoing(store, fake, route('seo', SEO, tags='seo'), core=True, transport=t)

    # when
    with caplog.at_level(logging.DEBUG):
        sent = routes.deliver(store.load()['anomalies'], NOW, store.load()['anomalies'])
        routes.deliver(store.load()['anomalies'], NOW + 60, store.load()['anomalies'])

    # then
    assert sent == 1 and [u for u, _ in t.calls] == [SEO]
    assert [p['route'] for p in store.anomaly(KEY)['teamsPosts']] == ['seo']
    assert 'secret-token-123' not in caplog.text and 'slack said no' not in caplog.text and 'RuntimeError' in caplog.text


def test_slack_not_configured_is_silent(store: Store, caplog):
    # given
    seed(store, seo_info())
    fake, t = Fake(store, configured=False), Transport()
    routes = echoing(store, fake, route('seo', SEO, tags='seo'), core=True, transport=t)

    # when
    with caplog.at_level(logging.WARNING):
        sent = routes.deliver(store.load()['anomalies'], NOW, store.load()['anomalies'])

    # then
    assert sent == 1 and fake.said == [] and [r.getMessage() for r in caplog.records if 'teams.min-sev info is retired' not in r.getMessage()] == []


def two_pods(store: Store, fa: Fake | None = None, fb: Fake | None = None):
    fa, fb = fa or Fake(store), fb or Fake(store)
    a = echoing(store, fa, route('seo', SEO, tags='seo'), core=True)
    b = echoing(store, fb, route('seo', SEO, tags='seo'), core=True)
    a._pend({'teams/seo': [KEY]}, a.routes(), NOW)
    return a, b, fa, fb


@pytest.mark.parametrize('has_post', [True, False])
def test_a_second_pod_running_between_the_read_and_the_write_gets_exactly_one_line(store: Store, has_post):
    # given
    seed(store, seo_info(slackPost=THREAD) if has_post else seo_info())
    a, b, fa, fb = two_pods(store)

    class Between:
        def __init__(self, inner):
            self.inner, self.fired = inner, False

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def anomaly(self, key):
            card = self.inner.anomaly(key)
            if not self.fired:
                self.fired = True
                b._echo_one(key, NOW)
            return card
    a.store = Between(store)

    # when
    a._echo_one(KEY, NOW)

    # then
    assert len(fa.said) + len(fb.said) == 1 and len(fa.posted) + len(fb.posted) == (0 if has_post else 1)


def test_a_second_pod_running_inside_the_slack_post_gets_one_post_and_one_line(store: Store):
    # given
    seed(store, seo_info())
    a, b, fa, fb = two_pods(store)

    def slow_post(key: str) -> dict | None:
        fa.posted.append(key)
        b._echo_one(key, NOW)
        store.slack_posted(key, THREAD)
        return store.anomaly(key)['slackPost']
    a.echo = Echo(slow_post, fa.say, fa.ready)

    # when
    a._echo_one(KEY, NOW)

    # then
    assert len(fa.posted) + len(fb.posted) == 1 and len(fa.said) + len(fb.said) == 1 and fb.posted == []


def test_a_claim_made_late_in_a_long_pass_still_holds_for_its_lease(store: Store):
    # given
    seed(store, seo_info())
    a, b, fa, fb = two_pods(store)
    at(a, NOW + 25)
    at(b, NOW + 25)

    def slow_post(key: str) -> dict | None:
        fa.posted.append(key)
        b._echo_one(key, NOW + 25)
        store.slack_posted(key, THREAD)
        return store.anomaly(key)['slackPost']
    a.echo = Echo(slow_post, fa.say, fa.ready)

    # when
    a._echo_one(KEY, NOW)

    # then
    assert len(fa.posted) + len(fb.posted) == 1 and len(fa.said) + len(fb.said) == 1


def test_two_stores_naming_the_same_entry_after_the_lease_fell_through_choose_it_once():
    # given
    db = mongomock.MongoClient(tz_aware=True)['skopax']
    sa, sb = Store(db), Store(db)
    sa.save(state(seo_info()), None)
    sa.route_echo_pend(KEY, [{'id': 'teams/seo', 'route': 'seo', 'plugin': 'T', 'tags': [], 'at': AT}])
    got = {}
    real = sa.anomalies

    class Proxy:
        fired = False

        def __getattr__(self, name):
            return getattr(real, name)

        def interleave(self):
            if not Proxy.fired:
                Proxy.fired = True
                got['b'] = sb.route_echo_name(KEY, '1.1', ['teams/seo'])

        def find_one(self, *args, **kwargs):
            found = real.find_one(*args, **kwargs)
            self.interleave()
            return found

        def find_one_and_update(self, *args, **kwargs):
            self.interleave()
            return real.find_one_and_update(*args, **kwargs)
    sa.anomalies = Proxy()
    for held in (sa, sb):
        held._hold_patiently = lambda: None

    # when
    got['a'] = sa.route_echo_name(KEY, '1.1', ['teams/seo'])

    # then
    assert len(got['a']) + len(got['b']) == 1


def test_the_echo_bookkeeping_never_wipes_a_concurrent_post_claim(store: Store):
    # given
    seed(store, seo_info())
    entry = {'id': 'teams/seo', 'route': 'seo', 'plugin': 'T', 'tags': [], 'at': AT}
    store.route_echo_pend(KEY, [entry])
    assert store.route_echo_claim(KEY, 'pod-a', NOW, 20)

    # when
    store.route_echo_pend(KEY, [{**entry, 'id': 'teams/tech'}])
    store.route_echo_fail(KEY, NOW)
    store.route_echo_drop(KEY, ['teams/tech'])

    # then
    assert not store.route_echo_claim(KEY, 'pod-b', NOW + 1, 20)
    assert [p['id'] for p in store.anomaly(KEY)['routeEcho']['pending']] == ['teams/seo']


def test_a_busy_card_key_never_writes_the_marker_and_loses_the_line(store: Store):
    # given
    gate = threading.Event()
    seed(store, seo_info(slackPost=THREAD))
    fake = Fake(store)
    routes = echoing(store, fake, route('seo', SEO, tags='seo'), core=True)
    routes._pend({'teams/seo': [KEY]}, routes.routes(), NOW)
    holder = threading.Thread(target=lambda: routes._bound.call(('echo', KEY), lambda: gate.wait(5), 5), daemon=True)
    holder.start()
    while not routes._bound._busy.get(('echo', KEY)):
        pass

    # when
    routes._echo_one(KEY, NOW)
    waiting = [p['id'] for p in store.anomaly(KEY)['routeEcho']['pending']]
    gate.set()
    holder.join(5)
    routes._echo_one(KEY, NOW)

    # then
    assert waiting == ['teams/seo'] and [t for _, t in fake.said] == ['Sent to seo on Microsoft Teams (tag: seo)']


def test_the_oldest_pending_echo_is_drained_first(store: Store):
    # given
    seed(store, seo_info('log|a|ERROR|a'), seo_info('log|a|ERROR|z'))
    fake = Fake(store, open_=False)
    routes = echoing(store, fake, route('seo', SEO, tags='seo'), core=True)
    routes.deliver([store.anomaly('log|a|ERROR|z')], NOW, [])
    routes.deliver([store.anomaly('log|a|ERROR|a')], NOW + 10, [])
    fake.open = True

    # when
    routes.deliver([], NOW + 100, [])

    # then
    assert fake.posted == ['log|a|ERROR|z', 'log|a|ERROR|a']


def test_a_failing_slack_post_backs_off_per_card(store: Store):
    # given
    seed(store, seo_info())
    fake = Fake(store, fail_post=True)
    routes = echoing(store, fake, route('seo', SEO, tags='seo'), core=True)

    # when
    at(routes, NOW)
    routes.deliver(store.load()['anomalies'], NOW, [])
    for i in range(1, 5):
        at(routes, NOW + i * 60)
        routes.deliver([], NOW + i * 60, [])

    # then
    assert len(fake.attempts) == 1 and [p['id'] for p in store.anomaly(KEY)['routeEcho']['pending']] == ['teams/seo']


def test_a_card_that_gets_its_slack_post_during_a_backoff_hears_the_line_at_once(store: Store):
    # given
    seed(store, seo_info())
    fake = Fake(store, fail_post=True)
    routes = echoing(store, fake, route('seo', SEO, tags='seo'), core=True)
    at(routes, NOW)
    routes.deliver(store.load()['anomalies'], NOW, [])

    # when
    store.slack_posted(KEY, THREAD)
    at(routes, NOW + 60)
    routes.deliver([], NOW + 60, [])

    # then
    assert len(fake.attempts) == 1 and [t for _, t in fake.said] == ['Sent to seo on Microsoft Teams (tag: seo)']


def test_five_failed_slack_posts_drop_the_entry_with_an_info_log(store: Store, caplog):
    # given
    seed(store, seo_info())
    fake = Fake(store, fail_post=True)
    routes = echoing(store, fake, route('seo', SEO, tags='seo'), core=True)

    # when
    with caplog.at_level(logging.INFO):
        at(routes, NOW)
        routes.deliver(store.load()['anomalies'], NOW, [])
        for later in (300, 900, 2100, 4500, 9000):
            at(routes, NOW + later)
            routes.deliver([], NOW + later, [])

    # then
    assert len(fake.attempts) == 5 and not store.anomaly(KEY)['routeEcho']['pending']
    assert 'after 5 failed tries' in caplog.text and 'secret-token-123' not in caplog.text


def test_a_hung_slack_call_does_not_hold_the_route_worker(store: Store):
    # given
    gate = threading.Event()
    seed(store, seo_info(slackPost=THREAD))
    fake = Fake(store, gate=gate)
    routes = echoing(store, fake, route('seo', SEO, tags='seo'), core=True)
    routes.timeout = 0.05

    # when
    started = time.monotonic()
    routes.deliver(store.load()['anomalies'], NOW, [])
    took = time.monotonic() - started

    # then
    assert took < 1.0 and fake.said == []
    gate.set()
    outbound_threads()


def test_a_slow_echo_of_one_card_does_not_drop_the_echo_of_the_next(store: Store):
    # given
    gate = threading.Event()
    first, second = 'log|a|ERROR|first', 'log|a|ERROR|second'
    seed(store, seo_info(first, slackPost={'channel': 'C1', 'ts': 'A', 'at': AT}), seo_info(second, slackPost={'channel': 'C1', 'ts': 'B', 'at': AT}))
    fake = Fake(store, gate=gate, slow='A')
    routes = echoing(store, fake, route('seo', SEO, tags='seo'), core=True)
    routes.timeout = 0.05

    # when
    started = time.monotonic()
    routes.deliver(store.load()['anomalies'], NOW, [])
    took = time.monotonic() - started

    # then
    assert took < 1.0 and [post['ts'] for post, _ in fake.said] == ['B']
    gate.set()
    outbound_threads()


def test_a_line_is_said_at_most_once_after_a_late_timeout(store: Store):
    # given
    gate = threading.Event()
    seed(store, open_card(KEY, 'warn', tags=tag('seo'), business=SAID, slackPost=THREAD))
    fake, t = Fake(store, gate=gate), Transport()
    routes = echoing(store, fake, route('seo', SEO, tags='seo'), core=True, transport=t, repeat_days=0)
    routes.timeout = 0.05
    routes.deliver(store.load()['anomalies'], NOW, [])
    gate.set()
    outbound_threads()

    # when
    seed(store, open_card(KEY, 'warn', tags=tag('seo'), business=SAID, firstSeen='2026-11-12T00:00:00Z'))
    routes.deliver(store.load()['anomalies'], NOW + 3 * 86400, [])
    outbound_threads()

    # then
    assert [u for u, _ in t.calls] == [SEO, SEO] and len(fake.said) == 1


def test_a_manual_send_answers_and_clears_its_retry_before_slack_is_told(store: Store):
    # given
    seed(store, seo_info())
    fake = Fake(store)
    routes = echoing(store, fake, route('seo', SEO, tags='seo'), core=True)

    # when
    answer = routes.send(KEY, 'teams/seo', 'tester')

    # then
    assert answer == {'ok': True, 'sent': ['teams/seo']} and (fake.posted, fake.said) == ([], [])
    assert ('teams/seo', KEY) not in routes._records(time.time())
    assert [p['id'] for p in store.anomaly(KEY)['routeEcho']['pending']] == ['teams/seo']
    routes.deliver([], NOW, [])
    assert [t for _, t in fake.said] == ['Sent to seo on Microsoft Teams (tag: seo)'] and fake.posted == [KEY]


def test_echo_waits_for_slack_and_expires_after_a_day_with_an_info_log(store: Store, caplog):
    # given
    seed(store, seo_info())
    fake = Fake(store, open_=False)
    routes = echoing(store, fake, route('seo', SEO, tags='seo'), core=True)
    routes.deliver(store.load()['anomalies'], NOW, [])

    # when
    with caplog.at_level(logging.INFO):
        routes.deliver([], NOW + 25 * 3600, [])
    fake.open = True
    routes.deliver([], NOW + 26 * 3600, [])

    # then
    assert 'not told to Slack within a day' in caplog.text and fake.posted == [] and fake.said == []
    assert not store.anomaly(KEY).get('routeEcho', {}).get('pending')


def test_the_pass_budget_leaves_the_rest_pending(store: Store):
    # given
    seed(store, seo_info(slackPost=THREAD))
    fake = Fake(store)
    routes = echoing(store, fake, route('seo', SEO, tags='seo'), core=True)
    routes.echo_budget = 0.0

    # when
    routes.deliver(store.load()['anomalies'], NOW, [])
    assert fake.said == []
    routes.echo_budget = 5.0
    routes.deliver([], NOW + 60, [])

    # then
    assert [t for _, t in fake.said] == ['Sent to seo on Microsoft Teams (tag: seo)']


def test_markdown_and_channel_mentions_in_a_route_name_stay_inert(store: Store):
    # given
    fake = Fake(store)
    routes = echoing(store, fake, route('seo', SEO, tags='seo'), core=True)
    odd = {'id': 'teams/x', 'route': '*loud* <!channel> _x_ <@U1> `c` ~s~ &', 'plugin': 'Microsoft Teams', 'tags': ['seo'], 'at': AT}

    # when
    text = routes.echo_text([odd])

    # then
    assert text == 'Sent to \u2217loud\u2217 &lt;!channel&gt; \u02cdx\u02cd &lt;@U1&gt; \u02cbc\u02cb \u223cs\u223c &amp; on Microsoft Teams (tag: seo)'
    assert routes.inert('data_team') == 'data\u02cdteam'


def test_a_long_line_is_cut_without_ending_inside_an_entity(store: Store):
    # given
    routes = echoing(store, Fake(store), route('seo', SEO, tags='seo'), core=True)
    long = {'id': 'teams/x', 'route': '&' * 3000, 'plugin': 'Microsoft Teams', 'tags': [], 'at': AT}

    # when
    text = routes.echo_text([long])

    # then
    assert len(text) <= 2900 and text.endswith('\u2026') and not text[:-1].endswith(('&', '&a', '&am', '&amp')) and text[:-1].endswith('&amp;')


def test_a_renamed_route_instance_is_named_by_its_current_name(store: Store):
    # given
    seed(store, seo_info())
    fake = Fake(store)
    routes = echoing(store, fake, route('search-team', SEO, tags='seo'), core=True)

    # when
    routes.deliver(store.load()['anomalies'], NOW, store.load()['anomalies'])

    # then
    assert fake.said[0][1] == 'Sent to search-team on Microsoft Teams (tag: seo)'


def test_a_muted_card_is_neither_posted_to_teams_nor_slack(store: Store):
    # given
    seed(store, seo_info(muted={'kind': 'mute', 'at': AT}))
    fake, t = Fake(store), Transport()
    routes = echoing(store, fake, route('seo', SEO, tags='seo'), core=True, transport=t)

    # when
    routes.deliver(store.load()['anomalies'], NOW, store.load()['anomalies'])

    # then
    assert t.calls == [] and fake.posted == [] and fake.said == []


def test_the_fixed_post_is_not_echoed(store: Store):
    # given
    closed = open_card(KEY, 'warn', tags=tag('seo'), business=INFO, status='cleared', clearedAt='2026-11-09T11:00:00Z', teamsPosts=[{'route': 'seo', 'plugin': 'teams', 'at': '2026-11-09T10:30:00Z', 'by': 'x'}])
    seed(store, closed)
    fake = Fake(store)
    routes = echoing(store, fake, route('seo', SEO, tags='seo'), core=True, fixed_posts=True)

    # when
    sent = routes.deliver(store.load()['anomalies'], NOW, store.load()['anomalies'])

    # then
    assert sent == 1 and fake.said == []


def test_a_key_route_push_is_echoed_too(store: Store):
    # given
    seed(store, open_card(KEY, 'crit'))
    fake = Fake(store)
    routes = echoing(store, fake, route('tech', TECH, keys='*'), core=True)

    # when
    routes.deliver([], NOW, store.load()['anomalies'])

    # then
    assert fake.posted == [KEY] and [t for _, t in fake.said] == ['Sent to tech on Microsoft Teams']


def test_a_future_route_plugin_gets_the_echo_with_its_own_name_and_no_new_code(store: Store):
    # given
    hook = Hook()
    plugin = hook_plugin(hook)
    seed(store, open_card(KEY, 'warn', tags=tag('seo'), business=INFO))
    fake = Fake(store)
    routes = echoing(store, fake, (plugin, 'web', {'url': 'https://hooks.example.com/web', 'tags': 'seo', 'slack-echo': 'on'}), plugins=(teams.PLUGIN, plugin))

    # when
    routes.deliver(store.load()['anomalies'], NOW, store.load()['anomalies'])

    # then
    assert [t for _, t in fake.said] == ['Sent to web on Hook (tag: seo)']


def test_every_outbound_plugin_carries_the_route_field_and_others_do_not():
    # given
    plain = minimal('plain', 'Plain')
    registry = Registry.of(teams.PLUGIN, hook_plugin(Hook()), plain)

    # when
    fields = {p.id: {f.key: f for f in p.fields} for p in registry.usable()}

    # then
    echo = fields['teams']['slack-echo']
    assert (echo.kind, echo.choices, echo.default) == ('choice', ('default', 'on', 'off'), '')
    assert echo.choice_labels == {'default': 'Use the default', 'on': 'On', 'off': 'Off'}
    assert 'slack-echo' in fields['hook'] and 'slack-echo' not in fields['plain']


def test_the_core_setting_exists_off_by_default_in_the_slack_group():
    # given
    found = [t for t in tunables.registry() if t.key == 'routes.slack-echo']

    # when
    base = Settings.__dataclass_fields__['routes_slack_echo'].default

    # then
    assert len(found) == 1 and found[0].kind == 'bool' and found[0].field == 'routes_slack_echo' and found[0].group == tunables.SLACK
    assert base is False


def app_with(store: Store, tmp_path, slack_t, teams_t, **over) -> TestClient:
    s = dataclasses.replace(settings(), **over) if over else settings()
    return TestClient(create_app(s, store, kube=Kube(root=tmp_path), transport=teams_t, slack=Slack('xoxb-test', '#ops', slack_t, FakeSlackApi())), base_url='https://skopax.dev.example.com')


class SlackT:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, token: str, payload: dict) -> dict:
        self.calls.append(payload)
        return {'ok': True, 'channel': 'C123', 'ts': f'1791900000.{len(self.calls):06d}'}


DRAFT = {'text': 'Checkout is failing for some visitors.', 'at': AT, 'by': 'pod'}
LINE = 'Sent to seo-team on Microsoft Teams (tag: seo)'


def armed(**auto) -> dict:
    return {'slackDraft': DRAFT, 'autoSend': {'draftAt': AT, 'draftHash': draft_hash(DRAFT['text']), **auto}}


def send_by_hand(c, route_name: str = 'seo-team') -> None:
    sent = c.post('/api/routes/send', json={'key': KEY, 'route': f'teams/{route_name}'}, headers=AUTH)
    assert sent.status_code == 200, sent.text


def test_in_the_app_a_route_post_reaches_slack_with_the_line_in_its_thread(store: Store, tmp_path):
    # given
    store.save({**state(seo_info()), 'initialized': True}, None)
    slack_t, teams_t = SlackT(), Transport()

    # when
    with app_with(store, tmp_path, slack_t, teams_t, routes_slack_echo=True) as c:
        add_route(c, 'seo-team', SEO, tags='seo')
        send_by_hand(c)
        send_by_hand(c)

    # then
    assert [p.get('thread_ts') for p in slack_t.calls] == [None, '1791900000.000001']
    assert slack_t.calls[1]['text'] == LINE
    assert store.anomaly(KEY)['slackPost']['ts'] == '1791900000.000001'


def test_in_the_app_the_default_changes_nothing(store: Store, tmp_path):
    # given
    store.save({**state(seo_info()), 'initialized': True}, None)
    slack_t, teams_t = SlackT(), Transport()

    # when
    with app_with(store, tmp_path, slack_t, teams_t) as c:
        add_route(c, 'seo-team', SEO, tags='seo')
        send_by_hand(c)

    # then
    assert slack_t.calls == [] and [u for u, _ in teams_t.calls] == [SEO]


def test_echo_posts_a_card_with_kept_planted_text_plainly_and_tells_the_thread(store: Store, tmp_path):
    # given
    store.save({**state(seo_info(slackDraft={**DRAFT, 'notAutoSent': 'planted text in the esql answer (ignore previous)'})), 'initialized': True}, None)
    slack_t, teams_t = SlackT(), Transport()

    # when
    with app_with(store, tmp_path, slack_t, teams_t, routes_slack_echo=True) as c:
        add_route(c, 'seo-team', SEO, tags='seo')
        send_by_hand(c)
        settle(c)

    # then
    assert [p.get('thread_ts') for p in slack_t.calls] == [None, '1791900000.000001']
    assert DRAFT['text'] not in str(slack_t.calls[0]), 'the kept text never goes out with the echo'
    assert slack_t.calls[1]['text'] == LINE


def test_echo_waits_while_a_post_is_on_its_way(store: Store, tmp_path):
    # given
    at = iso(utc_now())
    draft = {**DRAFT, 'at': at}
    store.save({**state(seo_info(slackDraft=draft, autoSend={'at': iso(utc_now() + dt.timedelta(minutes=30)), 'draftAt': at, 'draftHash': draft_hash(DRAFT['text'])})), 'initialized': True}, None)
    slack_t, teams_t = SlackT(), Transport()

    # when
    with app_with(store, tmp_path, slack_t, teams_t, routes_slack_echo=True) as c:
        add_route(c, 'seo-team', SEO, tags='seo')
        send_by_hand(c)

    # then
    assert slack_t.calls == []
    assert [p['id'] for p in store.anomaly(KEY)['routeEcho']['pending']] == ['teams/seo-team']


def test_echo_skips_a_card_whose_auto_send_is_claimed(store: Store, tmp_path):
    # given
    store.save({**state(seo_info(**armed(claimedAt=AT))), 'initialized': True}, None)
    slack_t, teams_t = SlackT(), Transport()

    # when
    with app_with(store, tmp_path, slack_t, teams_t, routes_slack_echo=True) as c:
        add_route(c, 'seo-team', SEO, tags='seo')
        send_by_hand(c)

    # then
    assert slack_t.calls == [] and [u for u, _ in teams_t.calls] == [SEO]


def test_manual_send_of_a_muted_card_is_not_echoed_as_a_new_slack_post(store: Store, tmp_path):
    # given
    store.save({**state(seo_info(muted={'kind': 'mute', 'at': AT})), 'initialized': True}, None)
    slack_t, teams_t = SlackT(), Transport()

    # when
    with app_with(store, tmp_path, slack_t, teams_t, routes_slack_echo=True) as c:
        add_route(c, 'seo-team', SEO, tags='seo')
        send_by_hand(c)

    # then
    assert slack_t.calls == [] and [u for u, _ in teams_t.calls] == [SEO]


def test_echo_waits_while_slack_autosend_is_off_and_tells_the_thread_after_a_manual_post(store: Store, tmp_path):
    # given
    store.save({**state(seo_info()), 'initialized': True}, None)
    slack_t, teams_t = SlackT(), Transport()

    # when
    with app_with(store, tmp_path, slack_t, teams_t, routes_slack_echo=True, slack_autosend=False) as c:
        add_route(c, 'seo-team', SEO, tags='seo')
        send_by_hand(c)
        c.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)
        send_by_hand(c)

    # then
    assert [p.get('thread_ts') for p in slack_t.calls] == [None, '1791900000.000001']
    assert slack_t.calls[1]['text'] == LINE


def test_a_live_admin_override_of_the_core_setting_reaches_the_routes(store: Store, tmp_path):
    # given
    store.save({**state(seo_info(slackPost={'channel': 'C123', 'ts': '1791800000.000009', 'at': AT})), 'initialized': True}, None)
    slack_t, teams_t = SlackT(), Transport()

    # when
    with app_with(store, tmp_path, slack_t, teams_t) as c:
        add_route(c, 'seo-team', SEO, tags='seo')
        saved = c.put('/api/admin/settings', json={'key': 'routes.slack-echo', 'value': 'true'}, headers=AUTH)
        send_by_hand(c)

    # then
    assert saved.status_code == 200 and [(p['thread_ts'], p['text']) for p in slack_t.calls] == [('1791800000.000009', LINE)]


def test_the_core_setting_is_read_from_config_and_the_environment():
    # given
    given = {'mongo.uri': 'mongodb://unused'}

    # when
    off = load_settings(lambda: given, env={})
    from_config = load_settings(lambda: {**given, 'routes.slack-echo': 'true'}, env={})
    from_env = load_settings(lambda: given, env={'ROUTES_SLACK_ECHO': 'true'})

    # then
    assert (off.routes_slack_echo, from_config.routes_slack_echo, from_env.routes_slack_echo) == (False, True, True)


def test_the_instances_api_saves_on_and_refuses_anything_else(store: Store, tmp_path):
    # given
    slack_t, teams_t = SlackT(), Transport()

    # when
    with app_with(store, tmp_path, slack_t, teams_t) as c:
        saved = c.post('/api/admin/plugins/teams/instances', json={'name': 'seo', 'values': {'webhook': SEO, 'tags': 'seo', 'slack-echo': 'on'}, 'enabled': True}, headers=AUTH)
        refused = c.post('/api/admin/plugins/teams/instances', json={'name': 'tech', 'values': {'webhook': TECH, 'tags': 'seo', 'slack-echo': 'maybe'}, 'enabled': True}, headers=AUTH)

    # then
    assert saved.status_code == 201 and saved.json()['values']['slack-echo'] == 'on'
    assert refused.status_code == 422


def settle(c) -> None:
    assert c.app.state.routes.drain(10)


@pytest.mark.parametrize('kind', ['handled', 'merged'])
def test_a_handled_card_and_a_merged_child_are_not_posted_to_slack_by_the_echo(store: Store, tmp_path, kind):
    # given
    host = open_card('log|a|ERROR|host', 'warn')
    card = seo_info(**({'triage': 'handled'} if kind == 'handled' else {'parent': host['key']}))
    store.save({**state(host, card), 'initialized': True}, None)
    slack_t, teams_t = SlackT(), Transport()

    # when
    with app_with(store, tmp_path, slack_t, teams_t, routes_slack_echo=True) as c:
        add_route(c, 'seo-team', SEO, tags='seo')
        send_by_hand(c)
        settle(c)

    # then
    assert slack_t.calls == [] and [p['id'] for p in store.anomaly(KEY)['routeEcho']['pending']] == ['teams/seo-team']


def test_slack_that_is_not_set_up_keeps_the_echo_quiet_in_the_app(store: Store, tmp_path, caplog):
    # given
    store.save({**state(seo_info()), 'initialized': True}, None)
    teams_t = Transport()

    # when
    with caplog.at_level(logging.WARNING):
        with TestClient(create_app(dataclasses.replace(settings(), routes_slack_echo=True), store, kube=Kube(root=tmp_path), transport=teams_t)) as c:
            add_route(c, 'seo-team', SEO, tags='seo')
            send_by_hand(c)
            settle(c)

    # then
    assert [u for u, _ in teams_t.calls] == [SEO] and 'echo' not in caplog.text.lower()


class Blocking(SlackT):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, token: str, payload: dict) -> dict:
        if not self.calls:
            self.entered.set()
            self.release.wait(10)
        return super().__call__(token, payload)


def test_a_slack_post_in_flight_keeps_the_echo_from_posting_a_second_time(store: Store, tmp_path):
    # given
    store.save({**state(seo_info()), 'initialized': True}, None)
    slack_t, teams_t = Blocking(), Transport()

    # when
    with app_with(store, tmp_path, slack_t, teams_t, routes_slack_echo=True) as c:
        add_route(c, 'seo-team', SEO, tags='seo')
        operator = threading.Thread(target=lambda: c.post('/api/slack-posts', json={'key': KEY}, headers=AUTH), daemon=True)
        operator.start()
        assert slack_t.entered.wait(10)
        send_by_hand(c)
        settle(c)
        during = len(slack_t.calls)
        slack_t.release.set()
        operator.join(10)
        send_by_hand(c)
        settle(c)

    # then
    assert during == 0
    assert [p.get('thread_ts') for p in slack_t.calls] == [None, '1791900000.000001'] and slack_t.calls[1]['text'] == LINE


def test_a_cleared_info_card_is_not_posted_to_slack_by_the_echo(store: Store, tmp_path):
    # given
    held = open_card(KEY, 'info', tags=tag('seo'), business=INFO, status='cleared', clearedAt='2026-11-09T11:00:00Z', owner='alice@example.com')
    store.save({**state(held), 'initialized': True}, None)
    slack_t, teams_t = SlackT(), Transport()

    # when
    with app_with(store, tmp_path, slack_t, teams_t, routes_slack_echo=True) as c:
        add_route(c, 'seo-team', SEO, tags='seo')
        send_by_hand(c)
        settle(c)

    # then
    assert slack_t.calls == [] and [p['id'] for p in store.anomaly(KEY)['routeEcho']['pending']] == ['teams/seo-team']


def test_a_slack_post_that_was_not_recorded_counts_as_a_failure_for_the_backoff(store: Store, tmp_path, monkeypatch):
    # given
    store.save({**state(seo_info()), 'initialized': True}, None)
    slack_t, teams_t = SlackT(), Transport()

    def lost(self, key, post):
        raise PyMongoError('write lost')
    monkeypatch.setattr(Store, 'slack_posted', lost)

    # when
    with app_with(store, tmp_path, slack_t, teams_t, routes_slack_echo=True) as c:
        add_route(c, 'seo-team', SEO, tags='seo')
        send_by_hand(c)
        settle(c)
        send_by_hand(c)
        settle(c)

    # then
    assert len(slack_t.calls) == 1 and store.anomaly(KEY)['routeEcho']['fails'] == 1


HOST = 'log|checkout-service|ERROR|first'
PLACED = {'channel': 'C123', 'ts': '1791900000.000001', 'replyTs': '1791900000.000009', 'threadOf': HOST, 'at': AT}


def test_a_line_in_another_cards_thread_names_its_own_card_and_why_it_is_there(store: Store):
    # given
    seed(store, open_card(HOST, num=3, slackPost=THREAD), seo_info(num=5, slackPost=PLACED))
    fake = Fake(store)
    routes = echoing(store, fake, route('seo', SEO, tags='seo'), core=True)

    # when
    routes.deliver([store.anomaly(KEY)], NOW, [store.anomaly(KEY)])

    # then
    assert fake.said == [(PLACED, '<https://skopax.example.com/c/5|#5 checkout-service / ERROR lines> \u00b7 Same service as #3 checkout-service / ERROR lines: '
                                  'Sent to seo on Microsoft Teams (tag: seo)')]


def test_a_line_in_a_shared_digest_thread_names_its_own_card(store: Store):
    # given
    digest = THREAD | {'digest': True}
    seed(store, open_card(HOST, num=3, slackPost=digest), seo_info(num=5, slackPost=digest))
    fake = Fake(store)
    routes = echoing(store, fake, route('seo', SEO, tags='seo'), core=True)

    # when
    routes.deliver([store.anomaly(KEY)], NOW, [store.anomaly(KEY)])

    # then
    assert fake.said == [(digest, '<https://skopax.example.com/c/5|#5 checkout-service / ERROR lines>: Sent to seo on Microsoft Teams (tag: seo)')]


def test_a_host_card_that_cannot_be_read_still_leaves_a_named_line(store: Store, monkeypatch):
    # given
    seed(store, open_card(HOST, num=3, slackPost=THREAD), seo_info(num=5, slackPost=PLACED))
    fake = Fake(store)
    routes = echoing(store, fake, route('seo', SEO, tags='seo'), core=True)
    read = store.anomaly

    def flaky(key):
        if key == HOST:
            raise PyMongoError('mongo is down')
        return read(key)

    monkeypatch.setattr(store, 'anomaly', flaky)

    # when
    routes.deliver([read(KEY)], NOW, [read(KEY)])

    # then
    assert fake.said == [(PLACED, '<https://skopax.example.com/c/5|#5 checkout-service / ERROR lines>: Sent to seo on Microsoft Teams (tag: seo)')]
