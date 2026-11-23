import dataclasses

from skopax.engine import iso as iso_at
from skopax.plugins import teams
from skopax.routes import Routes
from skopax.store import Store
from tests.conftest import state
from tests.plugin_kit import KIT_KEY, add_instance
from tests.teams_kit import AT, SEO, TECH, Transport, open_card, route, routes_of, tag

KEY = 'log|checkout-service|ERROR|boom'
NOW = 1_794_240_000.0
SAID = {'text': 'We recommend adding a redirect for the old spring sale page.', 'why': 'Visitors from the old campaign land on an empty page.', 'by': 'pod', 'at': AT}


def card(pushed: float, sev: str = 'high', key: str = KEY, **extra) -> dict:
    return open_card(key, sev, firstSeen=iso_at(pushed), pushedAt=iso_at(pushed), pushedSev=sev, **extra)


def only_new(routes: Routes) -> Routes:
    routes.only_new = True
    return routes


def keys_route(store: Store, t: Transport) -> Routes:
    return only_new(routes_of(store, t, route('tech', TECH, keys='log|*'), old=False))


def tag_route(store: Store, t: Transport, tags: str = 'developer') -> Routes:
    return only_new(routes_of(store, t, route('dev', SEO, tags=tags), old=False))


def cards(store: Store) -> list[dict]:
    return store.load()['anomalies']


def pushed_again(store: Store, at: float, **fields) -> None:
    given = store.load()
    given['anomalies'][0] |= {'pushedAt': iso_at(at)} | fields
    store.save(given, None)


def test_a_new_key_route_does_not_send_a_card_that_was_open_before_it(store: Store):
    # given
    t = Transport()
    store.save(state(card(NOW - 5 * 86400)), None)
    routes = keys_route(store, t)

    # when
    first = routes.deliver(cards(store), NOW, cards(store))
    later = routes.deliver(cards(store), NOW + 3600, cards(store))

    # then
    assert (first, later) == (0, 0) and t.calls == []


def test_a_new_tag_route_does_not_back_fill_open_cards(store: Store):
    # given
    t = Transport()
    store.save(state(card(NOW - 86400, tags=tag('developer'), triage='progress')), None)
    routes = tag_route(store, t)

    # when
    first = routes.deliver(cards(store), NOW)

    # then
    assert first == 0 and t.calls == []


def test_a_new_business_route_does_not_back_fill_cards_with_a_sentence(store: Store):
    # given
    t = Transport()
    store.save(state(card(NOW - 86400, tags=tag('seo'), business=SAID)), None)
    routes = tag_route(store, t, 'seo')

    # when
    first = routes.deliver(cards(store), NOW)

    # then
    assert first == 0 and t.calls == []


def test_a_card_first_seen_after_the_route_is_set_up_is_sent(store: Store):
    # given
    t = Transport()
    store.save(state(), None)
    routes = keys_route(store, t)
    routes.deliver([], NOW)
    store.save(state(card(NOW + 60)), None)

    # when
    sent = routes.deliver(cards(store), NOW + 60, cards(store))

    # then
    assert sent == 1 and len(t.calls) == 1


def test_editing_the_tags_of_a_route_does_not_back_fill(store: Store):
    # given
    t = Transport()
    store.save(state(card(NOW + 60, tags=tag('data'))), None)
    routes = tag_route(store, t)
    routes.deliver(cards(store), NOW)
    routes.deliver(cards(store), NOW + 60)

    # when
    add_instance(store, teams.PLUGIN, 'dev', sealer_key=KIT_KEY, primary=True, webhook=SEO, tags='developer, data')
    routes.runtime.refresh()
    after_edit = routes.deliver(cards(store), NOW + 600)
    pushed_again(store, NOW + 660, sev='crit', pushedSev='crit')
    escalated = routes.deliver(cards(store), NOW + 660)

    # then
    assert (after_edit, escalated) == (0, 1) and len(t.calls) == 1


def test_a_rescore_lift_does_not_send(store: Store):
    # given
    t = Transport()
    store.save(state(), None)
    routes = keys_route(store, t)
    routes.deliver([], NOW)
    store.save(state(card(NOW + 60, 'warn')), None)
    pushed_warn = routes.deliver(cards(store), NOW + 60, cards(store))

    # when
    lifted = store.lift_warnings(iso_at(NOW))
    sent = routes.deliver(cards(store), NOW + 120)

    # then
    assert lifted == 1 and store.anomaly(KEY)['sev'] == 'high' and store.anomaly(KEY)['pushedAt'] == iso_at(NOW + 60)
    assert (pushed_warn, sent) == (0, 0) and t.calls == []



def test_a_rescore_lift_does_not_make_a_tag_route_send_an_old_warning_card(store: Store):
    # given
    t = Transport()
    store.save(state(card(NOW - 86400, 'warn', tags=tag('developer'))), None)
    routes = only_new(routes_of(store, t, ('dev', {'webhook': SEO, 'tags': 'developer', 'min-sev': 'high'}), old=False))
    first = routes.deliver(cards(store), NOW)

    # when
    lifted = store.lift_warnings(iso_at(NOW))
    sent = routes.deliver(cards(store), NOW + 60)

    # then
    assert lifted == 1 and store.anomaly(KEY)['sev'] == 'high'
    assert (first, sent) == (0, 0) and t.calls == []

def test_a_delivery_of_one_card_that_stopped_days_ago_does_not_anchor_the_route_in_the_past(store: Store):
    # given
    t = Transport()
    old = card(NOW - 3 * 86400, key='log|a|ERROR|stopped', tags=tag('developer'), lastSeen=iso_at(NOW - 3 * 86400))
    recent = card(NOW - 86400, key='log|a|ERROR|recent', tags=tag('developer'), lastSeen=iso_at(NOW))
    store.save(state(old, recent), None)
    routes = tag_route(store, t)

    # when
    first = routes.deliver([store.anomaly('log|a|ERROR|stopped')], NOW)
    later = routes.deliver(cards(store), NOW + 60)

    # then
    assert (first, later) == (0, 0) and t.calls == []
    assert [r['since'] for r in store.route_marks.find({})] == [iso_at(NOW - 120)]


def test_an_escalation_after_the_route_is_set_up_sends(store: Store):
    # given
    t = Transport()
    store.save(state(card(NOW - 86400)), None)
    routes = keys_route(store, t)
    routes.deliver(cards(store), NOW, cards(store))

    # when
    pushed_again(store, NOW + 60, sev='crit', pushedSev='crit')
    sent = routes.deliver(cards(store), NOW + 60, cards(store))

    # then
    assert sent == 1 and len(t.calls) == 1


def test_a_reopen_after_the_route_is_set_up_sends(store: Store):
    # given
    t = Transport()
    store.save(state(card(NOW - 86400, tags=tag('developer'))), None)
    routes = tag_route(store, t)
    routes.deliver(cards(store), NOW)

    # when
    pushed_again(store, NOW + 600, firstSeen=iso_at(NOW + 600))
    sent = routes.deliver(cards(store), NOW + 600)

    # then
    assert sent == 1 and len(t.calls) == 1


def test_two_pods_share_the_moment_a_route_was_set_up(store: Store):
    # given
    t = Transport()
    store.save(state(), None)
    one, two = keys_route(store, t), keys_route(store, t)
    one.deliver([], NOW)
    two.deliver([], NOW + 600)
    store.save(state(card(NOW + 300)), None)

    # when
    sent = two.deliver(cards(store), NOW + 900, cards(store)) + one.deliver(cards(store), NOW + 960)

    # then
    assert sent == 1 and len(t.calls) == 1
    assert [r['since'] for r in store.route_marks.find({})] == [iso_at(NOW - 120)]


def test_without_the_rule_routes_send_as_before(store: Store):
    # given
    t = Transport()
    store.save(state(card(NOW - 86400)), None)
    routes = routes_of(store, t, route('tech', TECH, keys='log|*'), old=False)

    # when
    sent = routes.deliver(cards(store), NOW, cards(store))

    # then
    assert sent == 1


def test_a_qualifying_push_still_waits_for_the_investigation(store: Store):
    # given
    t = Transport()
    store.save(state(), None)
    routes = keys_route(store, t)
    routes.settings = dataclasses.replace(routes.settings, slack_post_sev='crit')
    routes.investigating = lambda: True
    routes.deliver([], NOW)
    store.save(state(card(NOW + 60)), None)

    # when
    early = routes.deliver(cards(store), NOW + 60, cards(store))
    store.note(KEY, 'The checkout pods ran out of memory after the deploy.', by='pod')
    then = routes.deliver(cards(store), NOW + 120)

    # then
    assert (early, then) == (0, 1) and len(t.calls) == 1


def test_turning_the_only_route_off_and_on_does_not_back_fill(store: Store):
    # given
    t = Transport()
    store.save(state(), None)
    routes = tag_route(store, t)
    routes.deliver([], NOW)
    add_instance(store, teams.PLUGIN, 'dev', sealer_key=KIT_KEY, primary=True, enabled=False, webhook=SEO, tags='developer')
    routes.runtime.refresh()
    off = routes.deliver([], NOW + 60)
    store.save(state(card(NOW + 120, tags=tag('developer'))), None)

    # when
    add_instance(store, teams.PLUGIN, 'dev', sealer_key=KIT_KEY, primary=True, enabled=True, webhook=SEO, tags='developer')
    routes.runtime.refresh()
    back = routes.deliver(cards(store), NOW + 600)

    # then
    assert routes.runtime.routes() != [] and (off, back) == (0, 0) and t.calls == []
