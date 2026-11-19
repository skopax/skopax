from skopax.plugins import cloudflare
from skopax.remote import HttpError
from skopax.store import Store
from tests.test_cloudflare_plugin import (BLOG, BLOG_CERTS, BLOG_CHECKS, BLOG_ID, CERTS, CHECKS, GQL, SHOP, SHOP_ID, app_with, cards_of, cf_runtime, connection_of, fake_cf, gql_posts,
                                          sweep)

BOTH = f'{SHOP}, {BLOG}'


def zones_asked(http) -> list[str]:
    out = []
    for call in gql_posts(http):
        out += call[3]['variables']['z']
    return out


def test_zone_plan_reads_the_checks_each_zone_runs_and_names_the_entries_it_cannot_read():
    # when
    plan, bad = cloudflare.zone_plan('a.com=edge, B.com.=cf+edge, c.com=none, d.com=all, e.com=oops, f.com=, g.com')

    # then
    assert plan == {'a.com': frozenset({'edge'}), 'b.com': frozenset({'cf', 'edge'}), 'c.com': frozenset(), 'd.com': frozenset({'cf', 'edge'})}
    assert bad == ['f.com=', 'g.com', 'e.com=oops']
    assert cloudflare.runs(plan, 'z.com', 'cf') and cloudflare.runs(plan, 'z.com', 'edge')
    assert not cloudflare.runs(plan, 'a.com', 'cf') and cloudflare.runs(plan, 'a.com', 'edge') and not cloudflare.runs(plan, 'c.com', 'edge')


def test_each_zone_runs_only_the_checks_chosen_for_it(store: Store):
    # given
    http = fake_cf()

    # when
    with app_with(store, http, main={'zones': BOTH, 'zone-checks': f'{BLOG}=edge, {SHOP}=cf'}) as c:
        sweep(c, 'cf')
        after_cf = (bool(http.hits(CHECKS)), len(http.hits(BLOG_CHECKS)), len(http.hits(BLOG_CERTS)), len(gql_posts(http)))
        sweep(c, 'edge')

    # then
    assert after_cf == (True, 0, 0, 0)
    assert zones_asked(http) and set(zones_asked(http)) == {BLOG_ID}


def test_without_a_plan_both_checks_run_for_every_zone(store: Store):
    # given
    http = fake_cf()

    # when
    with app_with(store, http, main={'zones': BOTH}) as c:
        sweep(c)

    # then
    assert http.hits(CHECKS) and http.hits(BLOG_CHECKS) and http.hits(CERTS) and http.hits(BLOG_CERTS)
    assert set(zones_asked(http)) == {SHOP_ID, BLOG_ID}


def test_a_zone_set_to_none_is_left_out_of_both_and_a_check_with_no_zone_left_makes_no_zone_call(store: Store):
    # given
    http = fake_cf()

    # when
    with app_with(store, http, main={'zones': SHOP, 'zone-checks': f'{SHOP}=none'}) as c:
        sweep(c)
        state = store.load()

    # then
    assert http.hits(CHECKS) == [] and http.hits(CERTS) == [] and gql_posts(http) == []
    assert state['sources']['cf'] == 'ok' and state['sources']['edge'] == 'ok'


def test_a_card_of_a_zone_that_no_longer_runs_a_check_closes_at_the_next_pass(store: Store):
    # given
    card = {'key': f'cf-cert|{BLOG}|pack', 'src': 'cf', 'sig': 'cf-cert', 'ns': '-', 'svc': BLOG, 'sub': 'pack', 'sev': 'warn', 'status': 'open', 'now': 5, 'label': 'x',
            'what': 'x', 'short': 'WARN x', 'ranKey': f'cf:{BLOG}:certs', 'firstSeen': '2026-10-17T07:00:00Z', 'lastSeen': '2026-10-17T07:45:00Z'}
    store.save_state({'anomalies': [card], 'initialized': True}, None)
    http = fake_cf()

    # when
    with app_with(store, http, main={'zones': BOTH, 'zone-checks': f'{BLOG}=edge'}) as c:
        sweep(c, 'cf')

    # then
    assert cards_of(store)[card['key']]['status'] == 'cleared'


def test_the_test_refuses_a_plan_it_cannot_read(store: Store):
    # given
    http = fake_cf()

    # when
    bad = connection_of(cf_runtime(store, http, main={'zone-checks': 'shop.example.com=sometimes'}))
    fine = connection_of(cf_runtime(store, fake_cf(), main={'zone-checks': f'{SHOP}=cf+edge'}))

    # then
    assert bad.ok is False and 'shop.example.com=sometimes' in bad.summary and fine.ok
    assert GQL not in str(http.calls)


def test_the_test_probes_only_what_each_zone_runs(store: Store):
    # given
    no_health = {('GET', CHECKS): HttpError(403, CHECKS), ('GET', CERTS): HttpError(403, CERTS)}
    no_analytics = {('POST', GQL): HttpError(403, GQL)}

    # when
    edge_only = connection_of(cf_runtime(store, fake_cf(no_health), main={'zone-checks': f'{SHOP}=edge'}))
    cf_only = connection_of(cf_runtime(store, fake_cf(no_analytics), main={'zone-checks': f'{SHOP}=cf'}))
    plain_health = connection_of(cf_runtime(store, fake_cf(no_health), main={}))
    plain_analytics = connection_of(cf_runtime(store, fake_cf(no_analytics), main={}))
    nothing = connection_of(cf_runtime(store, fake_cf({**no_health, **no_analytics}), main={'zone-checks': f'{SHOP}=none'}))

    # then
    assert edge_only.ok and cf_only.ok and nothing.ok
    assert plain_health.ok is False and 'health checks' in plain_health.summary
    assert plain_analytics.ok is False and 'analytics' in plain_analytics.summary


def test_a_zones_field_that_names_no_zone_still_fails_as_before_whatever_the_plan(store: Store):
    # given
    http = fake_cf()

    # when
    with app_with(store, http, main={'zones': ' , ', 'zone-checks': f'{SHOP}=edge'}) as c:
        sweep(c)
        state = store.load()

    # then
    assert state['sources']['cf'].startswith('error: zones: no watched zone found') and state['sources']['edge'].startswith('error: zones: no watched zone found')


def test_a_plan_that_mixes_none_with_a_check_or_names_a_zone_that_is_not_watched_is_refused(store: Store):
    # given
    http = fake_cf()

    # when
    mixed = connection_of(cf_runtime(store, http, main={'zone-checks': f'{SHOP}=cf+none'}))
    stray = connection_of(cf_runtime(store, fake_cf(), main={'zone-checks': f'{SHOP}=cf, other.example.com=edge'}))

    # then
    assert cloudflare.zone_plan('a.com=cf+none')[1] == ['a.com=cf+none']
    assert mixed.ok is False and 'cf+none' in mixed.summary
    assert stray.ok is False and 'other.example.com' in stray.summary and 'not in Zones' in stray.summary
