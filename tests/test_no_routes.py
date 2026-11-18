from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.kube import Kube
from skopax.routes import Route
from skopax.store import Store
from tests.conftest import AUTH, settings, state
from tests.teams_kit import AT, SEO, Transport, add_route, open_card, tag

NOW = 1_794_240_000.0
SAID = {'text': 'We recommend checking the product page.', 'by': 'pod', 'at': AT}


def route(**over) -> Route:
    return Route('teams/seo', 'seo', 'teams', 'seo', frozenset({'seo'}), (), None, **over)


def test_a_no_routes_card_is_never_auto_posted_but_can_be_sent_by_hand(store: Store, tmp_path):
    # given
    plain = open_card('log|a|ERROR|plain', 'crit', tags=tag('seo'), business=SAID)
    quiet = open_card('log|a|ERROR|quiet', 'crit', tags=tag('seo'), business=SAID, noRoutes=True)
    fixed = open_card('log|a|ERROR|fixed', 'crit', tags=tag('seo'), business={**SAID, 'kind': 'info'}, noRoutes=True, status='cleared', clearedAt='2026-11-08T00:00:00Z')
    store.save({**state(plain, quiet, fixed), 'initialized': True}, None)
    t = Transport()

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=t)) as c:
        add_route(c, 'seo', SEO, tags='seo')
        by_hand = c.post('/api/routes/send', json={'key': 'log|a|ERROR|quiet', 'route': 'teams/seo'}, headers=AUTH)
    live = route()
    held = dict(fixed, noRoutes=False)

    # then
    assert live.due(plain) is True and live.due(quiet) is False
    assert live.fixed_due(held, NOW) is True and live.fixed_due(fixed, NOW) is False
    assert by_hand.json() == {'ok': True, 'sent': ['teams/seo']}
    assert live.takes(quiet) is True
