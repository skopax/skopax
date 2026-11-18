from skopax import clock
from skopax.engine import iso
from skopax.plugins.website import pagescan as ps
from skopax.store import Store
from tests.fake_anthropic import FakeAnthropic, verdict
from tests.fake_cdp import FakeChrome, Site
from tests.fake_slack import fake_slack
from tests.test_page_scan import CHAIRS, NOW, TABLES, answer, probe, site_app, switched_on, typo
from tests.test_page_scan import no_waiting as no_waiting
from tests.test_slackevents import intake, thread_event

CHAIRS_KEY, TABLES_KEY = f'{ps.TYPOS}|shop.example.com/garden/chairs', f'{ps.TYPOS}|shop.example.com/garden/tables'


def digest_posted(store: Store, tmp_path) -> tuple[str, str]:
    chrome = FakeChrome({CHAIRS: Site(probe=probe('Garden Chiars order now')), TABLES: Site(probe=probe('Garden Tabels order now'))})
    fake = FakeAnthropic([], [answer(typo('Garden Chiars', 'Garden Chairs')), answer(typo('Garden Tabels', 'Garden Tables'))])
    slack, _, _ = fake_slack()
    at = clock.midnight(NOW) + 12 * 3600
    try:
        with site_app(store, tmp_path, chrome, ai=fake, slack=slack) as c:
            switched_on(c, f'{CHAIRS}, {TABLES}', digest_hour='0')
            c.app.state.sweeper.run('website', 'pagescan', at)
            c.app.state.sweeper.run('website', 'pagescan-digest', at + 60)
    finally:
        chrome.close()
    post = store.anomaly(CHAIRS_KEY)['slackPost']
    return post['channel'], post['ts']


def test_the_page_scan_digest_thread_belongs_to_no_single_card(store: Store, tmp_path):
    # given
    channel, ts = digest_posted(store, tmp_path)

    # when
    found = store.anomaly_by_post(channel, ts)

    # then
    assert {store.anomaly(k)['slackPost']['ts'] for k in (CHAIRS_KEY, TABLES_KEY)} == {ts}
    assert found is None


def test_a_reply_in_the_digest_thread_is_never_read_as_about_one_of_its_pages(store: Store, tmp_path):
    # given
    channel, ts = digest_posted(store, tmp_path)
    box, f = intake(store, gate_replies=[verdict(card_intent='resolve'), verdict()])

    # when
    untagged = box.handle(thread_event('we fixed that page', '1792224000.101000', root=ts, channel=channel))
    tagged = box.handle(thread_event('<@UBOT> where is the mistake on this page?', '1792224000.101100', root=ts, channel=channel))

    # then
    assert (untagged, tagged) == (None, 'accepted')
    [ask] = store.list_actions('requested', 5)
    assert (ask['slack']['where'], ask['slack']['threadTs'], ask['cards']) == ('channel', ts, [])
    assert not any(a['type'].endswith('-offer') for a in store.list_actions(None, 20))
    assert all(store.anomaly(k).get('triage') is None and not store.anomaly(k).get('resolveOffered') for k in (CHAIRS_KEY, TABLES_KEY))


def test_a_card_post_shared_by_no_other_root_card_still_names_its_card(store: Store):
    # given
    store.save({'initialized': True, 'muted': [], 'anomalies': [{'key': CHAIRS_KEY, 'sev': 'warn', 'status': 'open', 'svc': 'shop.example.com/garden/chairs'},
                                                                {'key': TABLES_KEY, 'sev': 'warn', 'status': 'open', 'svc': 'shop.example.com/garden/tables'}]}, None)
    store.slack_posted(CHAIRS_KEY, {'channel': 'C1', 'ts': '1792224000.000900', 'at': iso(NOW), 'by': 'server'})
    store.slack_posted(TABLES_KEY, {'channel': 'C1', 'ts': '1792224000.000900', 'at': iso(NOW), 'by': 'server', 'threadOf': CHAIRS_KEY})

    # when
    found = store.anomaly_by_post('C1', '1792224000.000900')

    # then
    assert found['key'] == CHAIRS_KEY


def test_a_card_that_opens_after_a_digest_on_a_host_the_digest_named_is_never_threaded_under_it(store: Store, tmp_path):
    # given
    digest_posted(store, tmp_path)
    card = {'key': 'safebrowsing|-/shop.example.com|listed', 'svc': store.anomaly(CHAIRS_KEY)['svc'], 'ns': store.anomaly(CHAIRS_KEY)['ns']}

    # when
    found = store.related_post(card, iso(NOW - 86400))

    # then
    assert found is None
