import dataclasses

from skopax.engine import iso as iso_at
from skopax.plugins import teams
from skopax.remote import HttpError, Reply
from skopax.routes import Routes
from skopax.store import Store
from tests.conftest import state
from tests.plugin_kit import KIT_KEY, add_instance
from tests.teams_kit import SEO, TECH, Transport, open_card, route, routes_of, tag

KEY = 'log|a|ERROR|held'
NOW = 1_794_240_000.0


def card(pushed: float, sev: str = 'high', key: str = KEY, **extra) -> dict:
    return open_card(key, sev, firstSeen=iso_at(pushed), pushedAt=iso_at(pushed), lastSeen=iso_at(pushed), pushedSev=sev, **extra)


def waiting(routes: Routes) -> Routes:
    routes.only_new = True
    routes.settings = dataclasses.replace(routes.settings, slack_post_sev='crit', routes_wait_minutes=30)
    routes.investigating = lambda: True
    return routes


def tag_route(store: Store, t: Transport) -> Routes:
    return waiting(routes_of(store, t, route('dev', SEO, tags='developer'), old=False))


def keys_route(store: Store, t: Transport) -> Routes:
    return waiting(routes_of(store, t, route('tech', TECH, keys='log|*'), old=False))


def cards(store: Store) -> list[dict]:
    return store.load()['anomalies']


def changed(store: Store, **fields) -> None:
    given = store.load()
    given['anomalies'][0] |= fields
    store.save(given, None)


def test_a_held_tag_route_card_survives_an_edit_of_the_route(store: Store):
    # given
    t = Transport()
    store.save(state(), None)
    routes = tag_route(store, t)
    routes.deliver([], NOW)
    store.save(state(card(NOW + 60, tags=tag('developer'))), None)
    held = routes.deliver(cards(store), NOW + 60)

    # when
    add_instance(store, teams.PLUGIN, 'dev', sealer_key=KIT_KEY, primary=True, webhook=SEO, tags='developer, data')
    routes.runtime.refresh()
    changed(store, lastSeen=iso_at(NOW + 120))
    edited = routes.deliver(cards(store), NOW + 120)
    fallback = routes.deliver(cards(store), NOW + 60 + 31 * 60)
    again = routes.deliver(cards(store), NOW + 60 + 32 * 60)

    # then
    assert (held, edited, fallback, again) == (0, 0, 1, 0) and len(t.calls) == 1
    assert store.route_retries() == []


def test_a_held_key_route_card_pushed_again_in_the_same_delivery_is_sent_once(store: Store):
    # given
    t = Transport()
    store.save(state(), None)
    routes = keys_route(store, t)
    routes.deliver([], NOW)
    store.save(state(card(NOW + 60)), None)
    held = routes.deliver([], NOW + 60, [store.anomaly(KEY)])

    # when
    changed(store, sev='crit', pushedSev='crit', pushedAt=iso_at(NOW + 120), lastSeen=iso_at(NOW + 120))
    sent = routes.deliver(cards(store), NOW + 120, [store.anomaly(KEY)])

    # then
    assert (held, sent) == (0, 1) and len(t.calls) == 1
    assert len(store.anomaly(KEY)['teamsPosts']) == 1


def test_two_pods_given_the_same_push_send_it_once(store: Store):
    # given
    t = Transport()
    store.save(state(), None)
    one, two = keys_route(store, t), keys_route(store, t)
    one.deliver([], NOW)
    store.save(state(card(NOW + 60, 'crit')), None)

    # when
    sent = one.deliver([], NOW + 60, [store.anomaly(KEY)]) + two.deliver([], NOW + 61, [store.anomaly(KEY)])

    # then
    assert sent == 1 and len(t.calls) == 1


def test_a_held_key_route_card_that_stops_firing_unresolved_is_sent_plainly_at_once(store: Store):
    # given
    t = Transport()
    store.save(state(), None)
    routes = keys_route(store, t)
    routes.deliver([], NOW)
    store.save(state(card(NOW + 60)), None)
    routes.deliver([], NOW + 60, [store.anomaly(KEY)])

    # when
    changed(store, status='cleared', clearedAt=iso_at(NOW + 120))
    sent = routes.deliver([], NOW + 180)

    # then
    assert sent == 1 and len(t.calls) == 1 and store.route_retries() == []


def test_a_held_tag_route_card_that_stops_firing_unresolved_is_sent_plainly_at_once(store: Store):
    # given
    t = Transport()
    store.save(state(), None)
    routes = tag_route(store, t)
    routes.deliver([], NOW)
    store.save(state(card(NOW + 60, tags=tag('developer'))), None)
    routes.deliver(cards(store), NOW + 60)

    # when
    changed(store, status='cleared', clearedAt=iso_at(NOW + 120))
    sent = routes.deliver(cards(store), NOW + 180)
    again = routes.deliver(cards(store), NOW + 240)

    # then
    assert (sent, again) == (1, 0) and len(t.calls) == 1 and store.route_retries() == []


def test_a_held_card_that_is_resolved_or_muted_is_dropped_on_both_route_kinds(store: Store):
    # given
    t = Transport()
    store.save(state(), None)
    routes = waiting(routes_of(store, t, route('dev', SEO, tags='developer'), route('tech', TECH, keys='log|*'), old=False))
    routes.deliver([], NOW)
    store.save(state(card(NOW + 60, tags=tag('developer')), card(NOW + 60, key='log|b|ERROR|muted', tags=tag('developer'))), None)
    routes.deliver(cards(store), NOW + 60, cards(store))
    given = store.load()
    given['anomalies'][0] |= {'status': 'cleared', 'clearedAt': iso_at(NOW + 120), 'triage': 'handled'}
    given['anomalies'][1] |= {'muted': True}
    store.save(given, None)

    # when
    sent = routes.deliver(cards(store), NOW + 180) + routes.deliver(cards(store), NOW + 60 + 31 * 60)

    # then
    assert sent == 0 and t.calls == []


def test_a_store_error_on_the_route_marks_keeps_key_route_pushes(store: Store, monkeypatch):
    # given
    t = Transport()
    store.save(state(card(NOW + 60, 'crit')), None)
    routes = keys_route(store, t)

    def down():
        raise RuntimeError('store down')

    monkeypatch.setattr(store, 'route_since', down)

    # when
    sent = routes.deliver([], NOW + 60, [store.anomaly(KEY)])

    # then
    assert sent == 1 and len(t.calls) == 1


def test_a_store_error_on_the_route_marks_never_back_fills_a_tag_route(store: Store, monkeypatch):
    # given
    t = Transport()
    old = open_card('log|a|ERROR|old', 'high', firstSeen=iso_at(NOW - 3 * 86400), pushedAt=iso_at(NOW - 3 * 86400), lastSeen=iso_at(NOW), pushedSev='high', tags=tag('developer'))
    store.save(state(old), None)
    routes = routes_of(store, t, route('dev', SEO, tags='developer'), old=False)
    routes.only_new = True
    first = routes.deliver(cards(store), NOW)

    def down():
        raise RuntimeError('store down')

    monkeypatch.setattr(store, 'route_since', down)

    # when
    later = routes.deliver(cards(store), NOW + 60)

    # then
    assert (first, later) == (0, 0) and t.calls == []


def test_a_held_key_route_card_is_dropped_when_the_route_no_longer_takes_its_key(store: Store):
    # given
    t = Transport()
    store.save(state(), None)
    routes = keys_route(store, t)
    routes.deliver([], NOW)
    store.save(state(card(NOW + 60)), None)
    held = routes.deliver([], NOW + 60, [store.anomaly(KEY)])

    # when
    add_instance(store, teams.PLUGIN, 'tech', sealer_key=KIT_KEY, primary=True, webhook=TECH, keys='other|*')
    routes.runtime.refresh()
    after = routes.deliver([], NOW + 60 + 31 * 60)

    # then
    assert (held, after) == (0, 0) and t.calls == [] and store.route_retries() == []


def test_a_held_key_route_card_is_dropped_when_the_route_raises_its_lowest_severity(store: Store):
    # given
    t = Transport()
    store.save(state(), None)
    routes = keys_route(store, t)
    routes.deliver([], NOW)
    store.save(state(card(NOW + 60)), None)
    held = routes.deliver([], NOW + 60, [store.anomaly(KEY)])

    # when
    add_instance(store, teams.PLUGIN, 'tech', sealer_key=KIT_KEY, primary=True, webhook=TECH, keys='log|*', **{'min-sev': 'crit'})
    routes.runtime.refresh()
    after = routes.deliver([], NOW + 60 + 31 * 60)

    # then
    assert (held, after) == (0, 0) and t.calls == []


def test_a_tag_route_deleted_and_made_again_days_later_does_not_send_the_card_it_held(store: Store):
    # given
    t = Transport()
    store.save(state(), None)
    routes = tag_route(store, t)
    routes.deliver([], NOW)
    store.save(state(card(NOW + 60, tags=tag('developer'))), None)
    held = routes.deliver(cards(store), NOW + 60)
    routes.runtime.instances.delete('teams', 'dev', 'alice@example.com')
    routes.runtime.refresh()
    gone = routes.deliver(cards(store), NOW + 120)

    # when
    changed(store, lastSeen=iso_at(NOW + 3 * 86400))
    add_instance(store, teams.PLUGIN, 'dev', sealer_key=KIT_KEY, primary=True, webhook=SEO, tags='developer')
    routes.runtime.refresh()
    back = routes.deliver(cards(store), NOW + 3 * 86400)

    # then
    assert (held, gone, back) == (0, 0, 0) and t.calls == []


def test_a_key_route_turned_off_and_on_days_later_does_not_send_the_card_it_held(store: Store):
    # given
    t = Transport()
    store.save(state(), None)
    routes = keys_route(store, t)
    routes.deliver([], NOW)
    store.save(state(card(NOW + 60)), None)
    held = routes.deliver([], NOW + 60, [store.anomaly(KEY)])
    add_instance(store, teams.PLUGIN, 'tech', sealer_key=KIT_KEY, primary=True, enabled=False, webhook=TECH, keys='log|*')
    routes.runtime.refresh()
    routes.deliver([], NOW + 120)

    # when
    add_instance(store, teams.PLUGIN, 'tech', sealer_key=KIT_KEY, primary=True, enabled=True, webhook=TECH, keys='log|*')
    routes.runtime.refresh()
    back = routes.deliver([], NOW + 3 * 86400)

    # then
    assert (held, back) == (0, 0) and t.calls == []


def test_a_held_key_route_card_still_goes_when_a_delivery_runs_late(store: Store):
    # given
    t = Transport()
    store.save(state(), None)
    routes = keys_route(store, t)
    routes.deliver([], NOW)
    store.save(state(card(NOW + 60)), None)
    routes.deliver([], NOW + 60, [store.anomaly(KEY)])

    # when
    late = routes.deliver([], NOW + 60 + 45 * 60)

    # then
    assert late == 1 and len(t.calls) == 1


class Down(Transport):
    down = True

    def __call__(self, method, url, headers, data, timeout):
        self.calls.append((url, data))
        if self.down:
            raise HttpError(503, url, 'down')
        return Reply(None, {})


def test_a_failed_tag_route_post_is_tried_again_after_the_route_is_edited(store: Store):
    # given
    t = Down()
    store.save(state(), None)
    routes = routes_of(store, t, route('dev', SEO, tags='developer'), old=False)
    routes.only_new = True
    routes.deliver([], NOW)
    store.save(state(card(NOW + 60, tags=tag('developer'))), None)
    failed = routes.deliver(cards(store), NOW + 60)
    add_instance(store, teams.PLUGIN, 'dev', sealer_key=KIT_KEY, primary=True, webhook=SEO.replace('sig=SEO', 'sig=SEO2'), tags='developer')
    routes.runtime.refresh()
    t.down = False

    # when
    after = sum(routes.deliver(cards(store), NOW + 60 + 600 * i) for i in range(1, 4))

    # then
    assert (failed, after) == (0, 1)


def test_a_held_tag_route_card_still_goes_when_the_route_was_edited_and_the_delivery_after_the_wait_runs_late(store: Store):
    # given
    t = Transport()
    store.save(state(), None)
    routes = tag_route(store, t)
    routes.deliver([], NOW)
    store.save(state(card(NOW + 60, tags=tag('developer'))), None)
    routes.deliver(cards(store), NOW + 60)
    add_instance(store, teams.PLUGIN, 'dev', sealer_key=KIT_KEY, primary=True, webhook=SEO, tags='developer, data')
    routes.runtime.refresh()
    routes.deliver(cards(store), NOW + 900)

    # when
    late = routes.deliver(cards(store), NOW + 60 + 30 * 60 + 180)

    # then
    assert late == 1 and len(t.calls) == 1


def test_a_held_key_route_card_still_goes_when_the_route_was_edited_and_the_delivery_after_the_wait_runs_late(store: Store):
    # given
    t = Transport()
    store.save(state(), None)
    routes = keys_route(store, t)
    routes.deliver([], NOW)
    store.save(state(card(NOW + 60)), None)
    routes.deliver([], NOW + 60, [store.anomaly(KEY)])
    add_instance(store, teams.PLUGIN, 'tech', sealer_key=KIT_KEY, primary=True, webhook=TECH, keys='log|a|*')
    routes.runtime.refresh()
    routes.deliver([], NOW + 900)

    # when
    late = routes.deliver([], NOW + 60 + 30 * 60 + 180)

    # then
    assert late == 1 and len(t.calls) == 1
