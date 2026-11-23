from skopax.store import Store
from tests.conftest import state
from tests.teams_kit import SEO, TECH, Transport, open_card, route, routes_of, tag
from tests.test_routes import KEY, NOW, SAID


def seeded(store: Store, sev: str) -> list[dict]:
    store.save(state(open_card(KEY, sev, tags=tag('seo'), business=SAID)), None)
    return store.load()['anomalies']


def test_a_tag_route_with_no_lowest_severity_keeps_the_warning_floor(store: Store):
    # given
    cards = seeded(store, 'warn')
    routes = routes_of(store, Transport(), route('seo', SEO, tags='seo'), old=False)

    # when
    sent = routes.tagged(cards, now=NOW)

    # then
    assert sent == {'teams/seo': [KEY]}


def test_a_key_route_with_no_lowest_severity_starts_at_high(store: Store):
    # given
    routes = routes_of(store, Transport(), route('tech', TECH, keys='*'), old=False)

    # when
    warn = routes.push([open_card(KEY, 'warn')], now=NOW)
    high = routes.push([open_card(KEY, 'high')], now=NOW)

    # then
    assert (warn, high) == ({}, {'teams/tech': [KEY]})
    assert [r.min_sev for r in routes.routes()] == ['high']


def test_an_explicit_lowest_severity_holds_for_tag_routes_too(store: Store):
    # given
    cards = seeded(store, 'warn')
    routes = routes_of(store, Transport(), route('seo', SEO, tags='seo'), old=False, min_sev='high')

    # when
    sent = routes.tagged(cards, now=NOW)

    # then
    assert sent == {}

