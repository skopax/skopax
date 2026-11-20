import asyncio
import copy
import datetime as dt
import json
import logging
import socket

import jwt
import pytest
from fastapi.testclient import TestClient

from skopax import clock
from skopax.app import create_app
from skopax.contract import InstanceInfo, Peer, ReadError
from skopax.engine import iso
from skopax.instances import Instances, PluginState
from skopax.investigator import prompt
from skopax.investigator.tools import CORE_TOOLS
from skopax.investigator.toolset import ToolSet
from skopax.kit import web
from skopax.kit.google import TOKEN_URL, GoogleAuth
from skopax.kube import Kube
from skopax.messages import msg
from skopax.plugins import google_ads, website
from skopax.plugins.google_ads import engine
from skopax.plugins.google_ads.engine import (ADS_API, BROKEN, MOVED, NEEDS, NO_ACCESS, POLICY_QUERY, SCOPES, AdsError, GoogleAds, read, scrubbed, untracked,
                                              verdict, wanted)
from skopax.plugintools import PluginTools
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError, Unreachable
from skopax.runtime import Runtime
from skopax.sealer import Sealer, master_key
from skopax.store import Store
from tests.conftest import AUTH, settings
from tests.fake_google import ACCESS, KEY, Google
from tests.plugin_kit import add_instance
from tests.test_investigator_tools import NOW, box
from tests.website_kit import Clock, page

CUSTOMER = '1234567890'
SHOWN = '123-456-7890'
TOKEN = 'DEVTOKEN-77aa-shop-0042'
SEARCH = f'{ADS_API}/v26/customers/{CUSTOMER}/googleAds:search'
EAST_SEARCH = f'{ADS_API}/v26/customers/2223334444/googleAds:search'
VALUES = {'customer-id': SHOWN, 'login-customer-id': '987 654 3210', 'developer-token': TOKEN, 'credentials': KEY}
SITE = 'https://shop.example.com'
SEAL = 'ads-key'
ALICE = 'alice@example.com'
NAME = f'Promo test {ALICE}\n+1 415 555 0100'
REDACTED = 'Promo test [redacted] [redacted]'
TODAY = dt.date(2026, 11, 10)
POLICY_GAQL = ("SELECT campaign.id, campaign.name, ad_group.id, ad_group.name, ad_group_ad.ad.id, ad_group_ad.ad.final_urls, ad_group_ad.policy_summary.approval_status, "
               "ad_group_ad.policy_summary.policy_topic_entries FROM ad_group_ad WHERE ad_group_ad.status = 'ENABLED' AND ad_group.status = 'ENABLED' "
               "AND campaign.status = 'ENABLED' AND ad_group_ad.policy_summary.approval_status = 'DISAPPROVED' LIMIT 201")
DOWN = ('DESTINATION_NOT_WORKING', 'PROHIBITED')
MARK = ('TRADEMARKS_IN_AD_TEXT', 'LIMITED')
PUBLIC = '93.184.216.34'


def on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class Names:
    def __init__(self):
        self.private: dict[str, list[str]] = {}
        self.gone: set[str] = set()
        self.asked: list[tuple[str, bool]] = []

    def __call__(self, host: str) -> list[str]:
        self.asked.append((host, on_loop()))
        if host in self.gone:
            raise socket.gaierror(8, 'nodename nor servname provided')
        return self.private.get(host, [PUBLIC])


@pytest.fixture(autouse=True)
def names(monkeypatch) -> Names:
    monkeypatch.setattr(web, 'SPACING_S', 0)
    monkeypatch.setattr(engine, 'GAP_S', 0)
    web._pacers.clear()
    found = Names()
    monkeypatch.setattr(google_ads, 'addresses', found)
    monkeypatch.setattr(web, 'resolve', found)
    return found


CHECKS = {'gads': 'ad-link check', 'gads-policy': 'policy check'}


def row(campaign: str, *urls: str, cid: str = '7', group: str = 'Chairs', gid: str = '70', ad_id: str = '9', approval: str = 'APPROVED', topics: tuple = (),
        status: str = 'ENABLED', running: str = 'ENABLED', grouped: str = 'ENABLED') -> dict:
    policy = {'approvalStatus': approval} | ({'policyTopicEntries': [{'topic': t, 'type': k} for t, k in topics]} if topics else {})
    return {'campaign': {'id': cid, 'name': campaign, 'status': running}, 'adGroup': {'id': gid, 'name': group, 'status': grouped},
            'adGroupAd': {'status': status, 'ad': {'id': ad_id, 'finalUrls': list(urls)}, 'policySummary': policy}}


def gaql(rows: list[dict], name: str = 'Example Shop'):
    def answer(query, body):
        q = body['query']
        if 'FROM customer' in q:
            return {'results': [{'customer': {'resourceName': f'customers/{CUSTOMER}', 'id': CUSTOMER, 'descriptiveName': name}}]}
        kept = [r for r in rows if ("ad_group_ad.status = 'ENABLED'" not in q or r['adGroupAd'].get('status') == 'ENABLED')
                and ("ad_group.status = 'ENABLED'" not in q or r['adGroup'].get('status') == 'ENABLED')
                and ("campaign.status = 'ENABLED'" not in q or r['campaign'].get('status') == 'ENABLED')
                and ("approval_status = 'DISAPPROVED'" not in q or r['adGroupAd']['policySummary'].get('approvalStatus') == 'DISAPPROVED')]
        return {'results': kept}

    return answer


def google(rows: list[dict] | None = None, pages: dict | None = None, over: dict | None = None, token: object = None) -> Google:
    return Google({('POST', SEARCH): gaql([] if rows is None else rows), **{('GET', url): answer for url, answer in (pages or {}).items()}, **(over or {})}, token)


def ads_of(http: Google, token: str = TOKEN, customer: str = SHOWN, login: str = '111-222-3333') -> GoogleAds:
    return GoogleAds(GoogleAuth(KEY, http, lambda: NOW, SCOPES), token, customer, login)


def queries(http: Google, url: str = SEARCH) -> list[str]:
    return [c[3]['query'] for c in http.hits(url)]


def fetched(http: Google) -> list[str]:
    return [c[1] for c in http.calls if c[0] == 'GET']


def links(ctx, at: Clock | None = None):
    at = at or Clock()
    return google_ads.run_links(ctx, clock=at, sleep=at.sleep, wall=lambda: NOW)


def tool_runtime(store, http, instances: dict | None = None) -> Runtime:
    reg = Registry.of(google_ads.PLUGIN)
    runtime = Runtime(reg, PluginState(store.db), Instances(store.db, reg, lambda: Sealer(SEAL)), http)
    for i, (name, values) in enumerate(({'main': VALUES} if instances is None else instances).items()):
        add_instance(store, google_ads.PLUGIN, name, sealer_key=SEAL, primary=i == 0, **values)
    runtime.refresh()
    return runtime


def ads_box(store: Store, http, instances: dict | None = None, **kw):
    return box(store, plugin_tools=PluginTools(tool_runtime(store, http, instances)), **kw)


def call(b, **args):
    text, err = b.call('google_ads', args)
    return (text if err else json.loads(text)), err


def app_with(store, tmp_path, http, instances: dict | None = None, shop: dict | None = None):
    sealing = master_key(store.db, settings())
    plugins = (google_ads.PLUGIN,) if shop is None else (google_ads.PLUGIN, website.PLUGIN)
    if shop is not None:
        add_instance(store, website.PLUGIN, 'shop', sealer_key=sealing, primary=True, **shop)
    for i, (name, values) in enumerate(({'main': VALUES} if instances is None else instances).items()):
        add_instance(store, google_ads.PLUGIN, name, sealer_key=sealing, primary=i == 0, **values)
    return TestClient(create_app(settings(), store, registry=Registry.of(*plugins), transport=http, kube=Kube(root=tmp_path)))


def by_key(state: dict) -> dict:
    return {a['key']: a for a in state['anomalies']}


def link_row(url: str, status: int | None, verdict_of: str | None = None, final: str | None = None) -> dict:
    return {'part': 'url', 'url': url, 'at': iso(NOW - 600), 'status': status, 'final': final or url, 'verdict': verdict_of, 'ads': 1}


def test_tracking_parameters_are_dropped_before_a_link_is_opened():
    # then
    assert untracked(f'{SITE}/sofas?utm_source=google&gclid=x&amount=100') == f'{SITE}/sofas?amount=100'


def test_a_gone_or_failing_page_is_broken_a_redirect_to_another_page_is_moved_and_a_challenge_proves_nothing():
    # then
    assert verdict({'status': 404, 'chain': [{'url': f'{SITE}/a'}]}, f'{SITE}/a') == (BROKEN, 404, f'{SITE}/a')
    assert verdict({'status': 503, 'chain': [{'url': f'{SITE}/a'}]}, f'{SITE}/a')[0] == BROKEN
    assert verdict({'status': 200, 'chain': [{'url': f'{SITE}/a', 'location': '/b'}, {'url': f'{SITE}/b'}]}, f'{SITE}/a') == (MOVED, 200, f'{SITE}/b')
    assert verdict({'status': 200, 'chain': [{'url': f'{SITE}/a/'}]}, f'{SITE}/a')[0] is None, 'a trailing slash is the same page'
    assert verdict({'challenge': 'x', 'status': None}, f'{SITE}/a')[0] == 'unknown'
    assert verdict({'stopped': 'x', 'status': 301, 'chain': [{'url': f'{SITE}/a'}]}, f'{SITE}/a')[0] == 'unknown'


def test_the_pass_opens_each_ad_link_once_raises_cards_and_clears_links_no_ad_uses_any_more():
    # given
    rows = [row('Sofa Search', f'{SITE}/garden/sofas?utm_source=g', ad_id='1'), row('Sofa Brand', f'{SITE}/garden/sofas', ad_id='2'),
            row('Benches', f'{SITE}/benches/old', ad_id='3'), row('Lamps', f'{SITE}/lamps', ad_id='4'), row('App', 'https://apps.example.org/app/id1', ad_id='5'),
            row('Flaky', f'{SITE}/slow', ad_id='6')]
    http = google(rows, {f'{SITE}/garden/sofas': page(404), f'{SITE}/benches/old': page(301, location='/benches'), f'{SITE}/benches': page(200),
                         f'{SITE}/lamps': page(200), f'{SITE}/slow': Unreachable('shop.example.com unreachable (timed out)')})
    removed = {'src': 'gads', 'sig': BROKEN, 'url': f'{SITE}/removed-ad', 'ranKey': f'gads:{SITE}/removed-ad'}

    # when
    swept = links(Ctx(VALUES, http, cards=[removed], site={'url': SITE}))

    # then
    result = swept.result
    opened = [r['url'] for r in swept.sections['adLinks'] if r['part'] == 'url']
    assert result.ok and result.status == 'ok \u00b7 read 3 of 4 pages' and opened == [f'{SITE}/benches/old', f'{SITE}/garden/sofas', f'{SITE}/lamps', f'{SITE}/slow']
    assert sorted(fetched(http)) == [f'{SITE}/benches', f'{SITE}/benches/old', f'{SITE}/garden/sofas', f'{SITE}/lamps', f'{SITE}/slow'], 'the app store link is not on the site'
    broken, moved = sorted(result.found, key=lambda c: c['sig'])
    assert (broken['key'], broken['sev'], broken['httpStatus'], broken['adCampaigns'], broken['adCount']) == (
        'ad-link|ads/shop.example.com/garden/sofas', 'warn', 404, ['Sofa Search', 'Sofa Brand'], 2)
    assert (moved['key'], moved['sev'], moved['finalUrl'], moved['ranKey']) == ('ad-link-moved|ads/shop.example.com/benches/old', 'info', f'{SITE}/benches', f'gads:{SITE}/benches/old')
    assert f'gads:{SITE}/slow' not in result.ran, 'a link that did not answer keeps its card as it was'
    assert f'gads:{SITE}/lamps' in result.ran and f'gads:{SITE}/removed-ad' in result.ran and 'gads:access:account' in result.ran
    summary = [r for r in swept.sections['adLinks'] if r['part'] == 'summary']
    assert summary == [{'part': 'summary', 'at': iso(NOW), 'urls': 5, 'opened': 4}]


def test_ad_link_cards_carry_the_campaigns_status_and_new_address_for_marketing_work():
    # given
    http = google([row('Sofa Search', f'{SITE}/garden/sofas'), row('Spring Sale', f'{SITE}/old')],
                  {f'{SITE}/garden/sofas': page(404), f'{SITE}/old': page(301, location='/new'), f'{SITE}/new': page(200)})

    # when
    broken, moved = sorted(links(Ctx(VALUES, http)).result.found, key=lambda c: c['sig'])

    # then
    assert broken['what'] == (f'1 ad in Sofa Search sends visitors to {SITE}/garden/sofas, which answers HTTP 404: '
                              'paid clicks land on a broken page until the ad link is changed.')
    assert moved['what'] == (f'1 ad in Spring Sale sends visitors to {SITE}/old, which now redirects to {SITE}/new: '
                             'point the ads at the new address so the click lands without a redirect.')
    assert (broken['url'], broken['adCampaigns'], moved['finalUrl'], moved['adCampaigns']) == (f'{SITE}/garden/sofas', ['Sofa Search'], f'{SITE}/new', ['Spring Sale'])


def test_without_a_website_peer_any_https_final_url_host_is_opened_and_with_one_only_the_site():
    # given
    rows = [row('Shop', f'{SITE}/chairs'), row('Click-out', f'{SITE}/out/x'), row('App', 'https://apps.example.org/app/id1'),
            row('Plain', 'http://plain.example.net/x'), row('Away', 'https://away.example.org/promo')]
    pages = {f'{SITE}/chairs': page(200), f'{SITE}/out/x': page(200), 'https://apps.example.org/app/id1': page(200),
             'https://away.example.org/promo': page(301, location='https://www.example.net/promo')}

    # when
    open_any = google(rows, pages)
    alone = links(Ctx(VALUES, open_any))
    site_only = google(rows, pages)
    with_site = links(Ctx(VALUES, site_only, site={'url': SITE, 'refused-paths': 'out'}))

    # then
    assert sorted(fetched(open_any)) == ['https://apps.example.org/app/id1', 'https://away.example.org/promo', f'{SITE}/chairs', f'{SITE}/out/x']
    assert 'gads:https://away.example.org/promo' not in alone.result.ran, 'a redirect to another host is not followed, so its card is held'
    assert fetched(site_only) == [f'{SITE}/chairs']
    assert with_site.result.ran == (f'gads:{SITE}/chairs', 'gads:access:account')


def test_a_link_the_site_now_refuses_closes_its_card_even_on_a_cut_listing(monkeypatch):
    # given
    monkeypatch.setattr(engine, 'MAX_ROWS', 1)
    http = google([row('Click-out', f'{SITE}/out/x', ad_id='1'), row('Shop', f'{SITE}/chairs', ad_id='2')], {f'{SITE}/chairs': page(200)})
    stale = {'src': 'gads', 'sig': BROKEN, 'url': f'{SITE}/out/x'}

    # when
    out = links(Ctx(VALUES, http, cards=[stale], site={'url': SITE, 'refused-paths': 'out'})).result

    # then
    assert f'gads:{SITE}/out/x' in out.ran and fetched(http) == [], 'nothing re-judges a refused link, so its card closes'


def test_the_policy_card_reads_what_the_ad_link_check_stored_for_its_url(store: Store, tmp_path):
    # given
    http = google([row('Chairs', f'{SITE}/gone?utm_source=g', approval='DISAPPROVED', topics=(DOWN,))], {f'{SITE}/gone': page(404)})

    # when
    with app_with(store, tmp_path, http) as c:
        c.app.state.sweeper.run('google-ads', 'gads', NOW)
        c.app.state.sweeper.run('google-ads', 'gads-policy', NOW + 30)
    card = by_key(store.load())['gads-policy|7']

    # then
    [check] = card['linkChecks']
    assert (check['url'], check['status'], check['verdict'], check['card']) == (f'{SITE}/gone', 404, BROKEN, 'ad-link|ads/shop.example.com/gone')
    assert 'ad-link|ads/shop.example.com/gone' in card['what']


def test_a_website_peer_that_does_not_serve_fails_the_pass_and_opens_nothing():
    # given
    http = google([row('Shop', f'{SITE}/chairs')])

    # when
    out = links(Ctx(VALUES, http, site=ReadError(503, 'the Website instance shop is switched off'))).result

    # then
    assert (out.ok, out.status) == (False, 'error: the Website instance shop is switched off') and http.calls == []


def test_the_ad_link_pass_stops_at_its_own_deadline_keeps_unread_links_open_and_their_last_rows():
    # given
    at = Clock()
    urls = [f'{SITE}/p{i}' for i in range(5)]

    def slow(query, body):
        at.now += 300
        return page(200)

    http = google([row(f'C{i}', u, ad_id=str(i)) for i, u in enumerate(urls)], {u: slow for u in urls})
    earlier = link_row(urls[4], 404, BROKEN)

    # when
    swept = links(Ctx(VALUES, http, rows={'adLinks': [earlier | {'plugin': 'google-ads'}]}), at)

    # then
    assert engine.DEADLINE_S == 0.8 * engine.TIMEOUT_S == 720
    assert swept.result.status == 'ok \u00b7 read 3 of 5 pages'
    assert swept.result.ran == tuple(f'gads:{u}' for u in urls[:3]) + ('gads:access:account',)
    kept = [r for r in swept.sections['adLinks'] if r['part'] == 'url']
    assert [r['url'] for r in kept] == urls[:3] + [urls[4]] and kept[-1] == earlier, 'the last row of a link not reached is kept, without core keys'


def test_more_ad_links_than_the_cap_are_read_n_of_m(monkeypatch):
    # given
    monkeypatch.setattr(engine, 'MAX_URLS', 2)
    urls = [f'{SITE}/p{i}' for i in range(3)]
    http = google([row(f'C{i}', u, ad_id=str(i)) for i, u in enumerate(urls)], {u: page(200) for u in urls})

    # when
    out = links(Ctx(VALUES, http)).result

    # then
    assert out.status == 'ok \u00b7 read 2 of 3 pages' and f'gads:{urls[2]}' not in out.ran


def test_a_cut_ad_listing_clears_no_link_card(monkeypatch):
    # given
    monkeypatch.setattr(engine, 'MAX_ROWS', 2)
    urls = [f'{SITE}/p{i}' for i in range(3)]
    http = google([row(f'C{i}', u, ad_id=str(i)) for i, u in enumerate(urls)], {u: page(200) for u in urls})
    held = {'src': 'gads', 'sig': BROKEN, 'url': f'{SITE}/p2'}

    # when
    out = links(Ctx(VALUES, http, cards=[held])).result

    # then
    assert queries(http)[0].endswith(' LIMIT 3')
    assert out.status == 'ok \u00b7 read 2 pages, more not read'
    assert out.ran == (f'gads:{urls[0]}', f'gads:{urls[1]}', 'gads:access:account'), 'an ad past the cut may still use the link'


def test_no_enabled_ads_is_no_data_and_clears_the_links_no_ad_uses():
    # given
    http = google([row('Paused', f'{SITE}/old', status='PAUSED')])
    stale = {'src': 'gads', 'sig': MOVED, 'url': f'{SITE}/old'}
    access = {'src': 'gads', 'sig': 'no-access', 'ranKey': 'gads:access:account'}

    # when
    out = links(Ctx(VALUES, http, cards=[stale, access])).result

    # then
    assert (out.ok, out.status, out.found) == (True, 'ok \u00b7 no data', [])
    assert out.ran == (f'gads:{SITE}/old', 'gads:no-data', 'gads:access:account') and fetched(http) == []


@pytest.mark.parametrize('check, run', [('gads', links), ('gads-policy', google_ads.run_policy)])
def test_a_refused_account_is_never_read_as_no_ads_and_gets_a_warn_card_naming_read_access(check, run):
    # given
    denied = HttpError(403, SEARCH, '{"error": {"code": 403, "message": "The caller does not have permission", "status": "PERMISSION_DENIED"}}')
    http = google(over={('POST', SEARCH): denied})

    # when
    swept = run(Ctx(VALUES, http))

    # then
    result = swept.result
    assert (result.ok, result.status, result.ran) == (True, f'ok \u00b7 no access to the account {SHOWN}', (f'{check}:access:account',))
    assert swept.sections == {} and fetched(http) == []
    [card] = result.found
    assert (card['key'], card['sev'], card['ranKey']) == (f'no-access|-/{check}|account', 'warn', f'{check}:access:account')
    assert card['label'] == f"Google Ads cannot read the account {SHOWN} for the {CHECKS[check]}: the service account lacks {NEEDS}"
    assert 'does not have permission' not in card['what'] + card['label']


@pytest.mark.parametrize('check, run', [('gads', links), ('gads-policy', google_ads.run_policy)])
def test_a_refused_key_in_a_pass_is_a_warn_card_naming_both_causes(check, run):
    # given
    http = google(token=HttpError(400, TOKEN_URL, '{"error": "invalid_grant"}'))

    # when
    swept = run(Ctx(VALUES, http))

    # then
    assert (swept.result.ok, swept.result.ran) == (True, (f'{check}:access:account',))
    assert swept.result.found[0]['label'] == f"Google Ads cannot read the account {SHOWN} for the {CHECKS[check]}: the service account is invalid or lacks {NEEDS}"


@pytest.mark.parametrize('check, run', [('gads', links), ('gads-policy', google_ads.run_policy)])
def test_a_server_error_fails_the_pass_with_a_fixed_reason(check, run):
    # given
    http = google(over={('POST', SEARCH): HttpError(500, SEARCH, f'{{"error": {{"message": "Backend failure for {TOKEN}"}}}}')})

    # when
    swept = run(Ctx(VALUES, http))

    # then
    assert (swept.result.ok, swept.result.status) == (False, 'error: Google Ads HTTP 500')


@pytest.mark.parametrize('values, text', [({'customer-id': 'abc'}, engine.BAD_CUSTOMER), ({'customer-id': '12345'}, engine.BAD_CUSTOMER),
                                          ({'login-customer-id': '12'}, engine.BAD_LOGIN), ({'api-version': 'v26/../x'}, engine.BAD_VERSION),
                                          ({'developer-token': 'two words'}, engine.BAD_TOKEN), ({'developer-token': 'x'}, engine.BAD_TOKEN),
                                          ({'developer-token': 'DEVTOKEN1'}, engine.BAD_TOKEN)])
def test_a_bad_id_version_or_token_fails_both_checks_before_any_call(values, text):
    # given
    http = google()

    # when
    out = [run(Ctx(VALUES | values, http)).result for run in (links, google_ads.run_policy)]

    # then
    assert [(r.ok, r.status) for r in out] == [(False, f'error: {text}')] * 2 and http.calls == []


def test_a_disapproved_ad_raises_one_warn_card_per_campaign_naming_its_topics():
    # given
    http = google([row('Chairs Search', f'{SITE}/garden/chairs?utm_source=g', group='Garden', ad_id='901', approval='DISAPPROVED', topics=(DOWN,)),
                   row('Chairs Search', f'{SITE}/garden/tables', group='Tables', gid='71', ad_id='902', approval='DISAPPROVED', topics=(DOWN, MARK)),
                   row('Chairs Search', f'{SITE}/garden/benches', ad_id='903')])

    # when
    result = google_ads.run_policy(Ctx(VALUES, http)).result

    # then
    assert (result.ok, result.status, result.ran) == (True, 'ok', ('gads-policy', 'gads-policy:access:account'))
    [card] = result.found
    assert (card['key'], card['ranKey'], card['sev'], card['src'], card['svc']) == ('gads-policy|7', 'gads-policy', 'warn', 'gads-policy', 'Chairs Search')
    assert card['label'] == '2 ads disapproved: Destination not working, Trademarks in ad text'
    assert (card['adsCustomer'], card['campaignId'], card['adCount']) == (CUSTOMER, '7', 2)
    assert card['ads'] == [{'adGroup': 'Garden', 'adId': '901', 'topics': [{'topic': 'Destination not working', 'type': 'PROHIBITED'}],
                            'finalUrls': [f'{SITE}/garden/chairs?utm_source=g']},
                           {'adGroup': 'Tables', 'adId': '902', 'topics': [{'topic': 'Destination not working', 'type': 'PROHIBITED'},
                                                                          {'topic': 'Trademarks in ad text', 'type': 'LIMITED'}], 'finalUrls': [f'{SITE}/garden/tables']}]
    assert f'Ad group Garden, ad 901: Destination not working (prohibited); final URL {SITE}/garden/chairs?utm_source=g.' in card['what']
    assert f'Ad group Tables, ad 902: Destination not working (prohibited), Trademarks in ad text (limited); final URL {SITE}/garden/tables.' in card['what']
    assert f'policy manager of account {SHOWN}' in card['what']


def test_two_campaigns_give_two_cards_and_names_are_shown_redacted():
    # given
    http = google([row(NAME, f'{SITE}/a', cid='7', approval='DISAPPROVED', topics=(DOWN,)),
                   row('Tables', f'{SITE}/b', cid='8', group=f'Group {ALICE}', approval='DISAPPROVED', topics=(MARK,))])

    # when
    found = google_ads.run_policy(Ctx(VALUES, http)).result.found

    # then
    assert [(c['key'], c['svc'], c['label']) for c in found] == [('gads-policy|7', REDACTED, '1 ad disapproved: Destination not working'),
                                                                 ('gads-policy|8', 'Tables', '1 ad disapproved: Trademarks in ad text')]
    assert ALICE not in json.dumps(found)


def test_an_approved_limited_ad_raises_no_card_but_the_check_still_clears():
    # given
    limited = row('Chairs Search', f'{SITE}/a', approval='APPROVED_LIMITED', topics=(MARK,))
    http = google(over={('POST', SEARCH): {'results': [limited]}})

    # when
    result = google_ads.run_policy(Ctx(VALUES, http)).result

    # then
    assert (result.ok, result.status, result.found, result.ran) == (True, 'ok', [], ('gads-policy', 'gads-policy:access:account'))


def test_the_policy_gaql_and_its_filters_are_pinned():
    # given
    http = google()

    # when
    google_ads.run_policy(Ctx(VALUES, http))

    # then
    assert POLICY_QUERY == POLICY_GAQL and queries(http) == [POLICY_GAQL]
    assert http.hits(SEARCH)[0][2]['developer-token'] == TOKEN and http.hits(SEARCH)[0][2]['login-customer-id'] == '9876543210'


def test_a_policy_read_past_its_cap_clears_nothing():
    # given
    rows = [row(f'C{i}', f'{SITE}/p{i}', cid=str(100 + i), ad_id=str(i), approval='DISAPPROVED', topics=(DOWN,)) for i in range(201)]
    http = google(over={('POST', SEARCH): {'results': rows}})

    # when
    result = google_ads.run_policy(Ctx(VALUES, http)).result

    # then
    assert result.status == 'ok \u00b7 read 200 disapproved ads, more not read' and len(result.found) == 200
    assert result.ran == ('gads-policy:access:account',), 'a campaign past the cap keeps its card'


def test_destination_not_working_links_the_card_to_the_ad_link_result_for_its_url():
    # given
    http = google([row('Chairs', f'{SITE}/answers?utm_source=g', ad_id='1', approval='DISAPPROVED', topics=(DOWN,)),
                   row('Chairs', f'{SITE}/broken', ad_id='2', approval='DISAPPROVED', topics=(DOWN,)),
                   row('Chairs', f'{SITE}/unseen', ad_id='3', approval='DISAPPROVED', topics=(DOWN,)),
                   row('Chairs', f'{SITE}/trademark', ad_id='4', approval='DISAPPROVED', topics=(MARK,))])
    seen = [link_row(f'{SITE}/answers', 200), link_row(f'{SITE}/broken', 404, BROKEN), link_row(f'{SITE}/trademark', 200),
            {'part': 'summary', 'at': iso(NOW - 600), 'urls': 4, 'opened': 3}]
    broken = {'src': 'gads', 'sig': BROKEN, 'key': 'ad-link|ads/shop.example.com/broken', 'url': f'{SITE}/broken'}

    # when
    [card] = google_ads.run_policy(Ctx(VALUES, http, rows={'adLinks': seen}, cards=[broken])).result.found

    # then
    checks = {c['url']: c for c in card['linkChecks']}
    assert sorted(checks) == [f'{SITE}/answers', f'{SITE}/broken', f'{SITE}/unseen'], 'only the destination-not-working links'
    assert (checks[f'{SITE}/answers']['status'], checks[f'{SITE}/answers']['card']) == (200, None)
    assert (checks[f'{SITE}/broken']['verdict'], checks[f'{SITE}/broken']['card']) == (BROKEN, 'ad-link|ads/shop.example.com/broken')
    assert checks[f'{SITE}/unseen'] == {'url': f'{SITE}/unseen', 'at': None, 'status': None, 'final': None, 'verdict': None, 'error': None, 'card': None}
    assert f'The ad-link check (gads) opened {SITE}/answers at ' in card['what'] and 'answers from the server that runs the checks' in card['what']
    assert 'ad-link|ads/shop.example.com/broken' in card['what'] and f'The ad-link check (gads) has not opened {SITE}/unseen yet.' in card['what']


@pytest.mark.parametrize('change', ['approved', 'ad paused', 'campaign paused'])
def test_a_policy_card_clears_when_the_ad_is_approved_or_paused_on_the_next_check(store: Store, tmp_path, change):
    # given
    rows = [row('Chairs Search', f'{SITE}/a', approval='DISAPPROVED', topics=(DOWN,))]
    http = google(rows)

    # when
    with app_with(store, tmp_path, http) as c:
        c.app.state.sweeper.run('google-ads', 'gads-policy', NOW)
        before = by_key(store.load())
        if change == 'approved':
            rows[0]['adGroupAd']['policySummary'] = {'approvalStatus': 'APPROVED'}
        elif change == 'ad paused':
            rows[0]['adGroupAd']['status'] = 'PAUSED'
        else:
            rows[0]['campaign']['status'] = 'PAUSED'
        c.app.state.sweeper.run('google-ads', 'gads-policy', NOW + 900)
        after = by_key(store.load())

    # then
    assert before['gads-policy|7']['status'] == 'open' and before['gads-policy|7']['links'] == [
        {'label': f'Policy manager, account {SHOWN}', 'url': 'https://ads.google.com/aw/policymanager'}]
    assert after['gads-policy|7']['status'] != 'open'


@pytest.mark.parametrize('answer', [HttpError(403, SEARCH), HttpError(500, SEARCH, '{"error": {"message": "Backend failure."}}')], ids=['refused', 'server error'])
def test_an_api_error_leaves_the_policy_and_link_cards_open(store: Store, tmp_path, answer):
    # given
    rows = [row('Chairs Search', f'{SITE}/a', approval='DISAPPROVED', topics=(DOWN,))]
    http = google(rows, {f'{SITE}/a': page(404)})

    # when
    with app_with(store, tmp_path, http) as c:
        for check in ('gads', 'gads-policy'):
            c.app.state.sweeper.run('google-ads', check, NOW)
        http.routes[('POST', SEARCH)] = answer
        for check in ('gads', 'gads-policy'):
            c.app.state.sweeper.run('google-ads', check, NOW + 900)
    state = store.load()

    # then
    cards = by_key(state)
    assert cards['gads-policy|7']['status'] == 'open' and cards['ad-link|ads/shop.example.com/a']['status'] == 'open', 'no false clear'
    assert state['adLinks'][0]['url'] == f'{SITE}/a', 'the last rows are kept'


def test_both_checks_raise_their_own_access_card_and_both_clear_once_the_account_reads_again(store: Store, tmp_path):
    # given
    answers = {'refuse': True}
    ads = gaql([row('Chairs', f'{SITE}/a')])
    http = google(over={('POST', SEARCH): lambda q, b: HttpError(403, SEARCH) if answers['refuse'] else ads(q, b)}, pages={f'{SITE}/a': page(200)})

    # when
    with app_with(store, tmp_path, http) as c:
        for check in ('gads', 'gads-policy'):
            c.app.state.sweeper.run('google-ads', check, NOW)
        refused = by_key(store.load())
        answers['refuse'] = False
        for check in ('gads', 'gads-policy'):
            c.app.state.sweeper.run('google-ads', check, NOW + 900)
        read_again = by_key(store.load())

    # then
    keys = ('no-access|-/gads|account', 'no-access|-/gads-policy|account')
    assert [refused[k]['status'] for k in keys] == ['open', 'open']
    assert [read_again[k]['status'] != 'open' for k in keys] == [True, True]
    sources = store.load()['sources']
    assert (sources['gads'], sources['gads-policy']) == ('ok', 'ok')


def test_the_app_runs_both_checks_in_the_background_with_their_own_scope_and_reads_the_website_peer(store: Store, tmp_path):
    # given
    http = google([row('Shop', f'{SITE}/chairs'), row('App', 'https://apps.example.org/app/id1')], {f'{SITE}/chairs': page(404)})
    values = VALUES | {'website': 'shop'}

    # when
    with app_with(store, tmp_path, http, instances={'main': values}, shop={'url': SITE}) as c:
        jobs = {j.name: j for j in c.app.state.scheduler.jobs}
        tiles = {p['id']: p for p in c.get('/api/server').json()['plugins']}
        c.app.state.sweeper.run('google-ads', 'gads', NOW)
    state = store.load()

    # then
    assert [(jobs[n].period, jobs[n].offset, jobs[n].background) for n in ('google-ads.gads', 'google-ads.gads-policy')] == [(900, 750, True), (900, 780, True)]
    assert tiles['google-ads']['passes'] == ['gads', 'gads-policy'] and state['sources']['gads'] == 'ok'
    card = by_key(state)['ad-link|ads/shop.example.com/chairs']
    assert card['links'] == [{'label': 'Open the ad link', 'url': f'{SITE}/chairs'}, {'label': 'Google Ads', 'url': 'https://ads.google.com/aw/ads'}]
    assert fetched(http) == [f'{SITE}/chairs'], 'the website peer keeps the app store link closed'
    assert jwt.decode(http.forms[0]['assertion'], options={'verify_signature': False})['scope'] == 'https://www.googleapis.com/auth/adwords'


def test_a_search_posts_gaql_with_the_developer_token_and_manager_id_and_follows_pages():
    # given
    def answer(query, body):
        return {'results': [{'a': 2}]} if body.get('pageToken') == 'p2' else {'results': [{'a': 1}], 'nextPageToken': 'p2'}

    http = google(over={('POST', SEARCH): answer})

    # when
    rows, cut = ads_of(http).search('SELECT campaign.name FROM campaign')

    # then
    assert rows == [{'a': 1}, {'a': 2}] and cut is False
    method, url, headers, body = http.hits(SEARCH)[1]
    assert (method, url, body) == ('POST', SEARCH, {'query': 'SELECT campaign.name FROM campaign', 'pageToken': 'p2'})
    assert (headers['developer-token'], headers['login-customer-id'], headers['Authorization']) == (TOKEN, '1112223333', f'Bearer {ACCESS}')


def test_inputs_are_checked_and_a_raw_query_is_one_read_only_select_with_a_limit():
    # then
    assert wanted({'op': 'query', 'query': 'SELECT campaign.name FROM campaign'})['query'] == 'SELECT campaign.name FROM campaign LIMIT 200'
    assert wanted({'op': 'query', 'query': 'SELECT x FROM y LIMIT 5'})['query'] == 'SELECT x FROM y LIMIT 5'
    for bad, why in (({'op': 'query', 'query': 'DELETE x'}, 'one GAQL SELECT'), ({'op': 'query', 'query': 'SELECT a FROM b; SELECT c FROM d'}, 'without ;'), ({'op': 'nope'}, 'op is one of')):
        with pytest.raises(AdsError) as e:
            wanted(bad)
        assert why in e.value.message


def test_campaigns_daily_and_ads_come_back_as_plain_rows_with_cost_in_the_account_currency():
    # given
    campaign_rows = {'results': [{'campaign': {'id': '7', 'name': 'Chairs Search', 'status': 'ENABLED', 'advertisingChannelType': 'SEARCH'},
                                  'metrics': {'costMicros': '1234560000', 'clicks': '420', 'impressions': '9000', 'conversions': 31.5}}]}
    daily_rows = {'results': [{'segments': {'date': '2026-11-08'}, 'metrics': {'costMicros': '1000000', 'clicks': '3', 'impressions': '10', 'conversions': 1}},
                              {'segments': {'date': '2026-11-08'}, 'metrics': {'costMicros': '500000', 'clicks': '1', 'impressions': '5', 'conversions': 0}}]}
    ad_rows = {'results': [{'campaign': {'name': 'Chairs'}, 'adGroup': {'name': 'Garden'}, 'adGroupAd': {'ad': {'id': '9', 'finalUrls': [f'{SITE}/garden/chairs-oak']},
                                                                                                     'policySummary': {'approvalStatus': 'APPROVED_LIMITED'}}},
                           {'campaign': {'name': 'Tables'}, 'adGroup': {'name': 'x'}, 'adGroupAd': {'ad': {'id': '10', 'finalUrls': [f'{SITE}/tables']}}}]}
    answers = [campaign_rows, daily_rows, ad_rows]
    http = google(over={('POST', SEARCH): lambda q, b: answers.pop(0)})
    client = ads_of(http)

    # when
    camps = read(client, wanted({'op': 'campaigns', 'days': 7}), TODAY)
    daily = read(client, wanted({'op': 'daily', 'days': 3, 'campaign': "Chairs'; DROP"}), TODAY)
    found = read(client, wanted({'op': 'ads', 'url_contains': 'oak'}), TODAY)

    # then
    assert camps == {'from': '2026-11-03', 'to': '2026-11-09', 'truncated': False,
                     'campaigns': [{'id': '7', 'name': 'Chairs Search', 'status': 'ENABLED', 'channel': 'SEARCH', 'cost': 1234.56, 'clicks': 420, 'impressions': 9000, 'conversions': 31.5}]}
    assert daily['days'] == [{'date': '2026-11-08', 'cost': 1.5, 'clicks': 4, 'impressions': 15, 'conversions': 1}]
    assert "LIKE '%Chairs DROP%'" in queries(http)[1], 'quotes never reach the GAQL'
    assert found['ads'] == [{'campaign': 'Chairs', 'adGroup': 'Garden', 'adId': '9', 'finalUrls': [f'{SITE}/garden/chairs-oak'], 'approval': 'APPROVED_LIMITED'}]


def test_an_ads_error_never_carries_the_developer_token_even_where_the_message_is_cut():
    # given
    short = HttpError(400, SEARCH, json.dumps({'error': {'message': f'developer token {TOKEN} is not approved'}}))
    long = HttpError(400, SEARCH, json.dumps({'error': {'message': 'x' * 190 + TOKEN}}))

    # when
    errors = []
    for answer in (short, long):
        with pytest.raises(ReadError) as e:
            ads_of(google(over={('POST', SEARCH): answer})).search('SELECT campaign.name FROM campaign')
        errors.append(e.value)

    # then
    assert errors[0].message == 'Google Ads HTTP 400: developer token *** is not approved'
    assert all(TOKEN[:8] not in e.message + e.reason for e in errors), 'the token is masked before the cut, so no part of it is left'


def test_without_a_given_day_the_window_ends_on_the_day_before_in_the_configured_zone():
    # given
    clock.set_zone('Asia/Tokyo')
    http = google(over={('POST', SEARCH): {'results': []}})
    late = dt.datetime(2026, 3, 4, 22, 30, tzinfo=dt.timezone.utc).timestamp()

    # when
    out = google_ads.ads_tool(Ctx(VALUES, http, now=late), {'op': 'daily', 'days': 1})

    # then
    assert (out['from'], out['to']) == ('2026-03-04', '2026-03-04')
    assert "segments.date BETWEEN '2026-03-04' AND '2026-03-04'" in queries(http)[0]


def test_google_ads_campaign_and_ad_group_names_are_redacted_and_final_urls_stay_whole():
    # given
    record = {'campaign': {'id': '7', 'name': NAME, 'status': 'ENABLED'}, 'adGroup': {'name': f'group {ALICE}'},
              'adGroupAd': {'ad': {'id': '9', 'finalUrls': [f'{SITE}/sofas?ref={ALICE}']}}, 'metrics': {'costMicros': '1000000'}}

    # when
    camps = read(ads_of(google(over={('POST', SEARCH): {'results': [record]}})), wanted({'op': 'campaigns'}), TODAY)
    found = read(ads_of(google(over={('POST', SEARCH): {'results': [record]}})), wanted({'op': 'ads'}), TODAY)
    raw = read(ads_of(google(over={('POST', SEARCH): {'results': [record]}})), wanted({'op': 'query', 'query': 'SELECT campaign.name FROM ad_group_ad'}), TODAY)

    # then
    assert camps['campaigns'][0]['name'] == REDACTED
    assert (found['ads'][0]['campaign'], found['ads'][0]['adGroup'], found['ads'][0]['finalUrls']) == (REDACTED, 'group [redacted]', [f'{SITE}/sofas?ref={ALICE}'])
    assert raw['rows'][0]['campaign']['name'] == REDACTED and raw['rows'][0]['adGroup']['name'] == 'group [redacted]'
    assert raw['rows'][0]['adGroupAd']['ad']['finalUrls'] == [f'{SITE}/sofas?ref={ALICE}'] and raw['rows'][0]['metrics'] == {'costMicros': '1000000'}


def test_the_google_ads_tool_is_offered_once_an_instance_serves_and_reads_with_its_values(store: Store):
    # given
    http = google([row('Chairs', f'{SITE}/a')])

    # when
    off = ToolSet(tool_runtime(store, http, instances={})).status('google_ads', None)
    on = ToolSet(tool_runtime(store, http)).status('google_ads', None)
    found, err = call(ads_box(store, http), op='ads')

    # then
    assert (off['state'], on) == ('off', {'state': 'on', 'reason': None}) and google_ads.PLUGIN.tools[0].scan
    assert not err and found['ads'][0]['finalUrls'] == [f'{SITE}/a']
    assert http.hits(SEARCH)[0][2]['login-customer-id'] == '9876543210'


def test_a_refused_tool_call_names_the_missing_permission(store: Store):
    # when
    refused, err = call(ads_box(store, google(over={('POST', SEARCH): HttpError(403, SEARCH)})), op='campaigns')
    key, key_err = call(ads_box(store, google(token=HttpError(400, TOKEN_URL, '{"error": "invalid_grant"}'))), op='campaigns')

    # then
    assert err and NO_ACCESS in refused and 'HTTP 403' in refused
    assert key_err and f'the service account is invalid or lacks {NEEDS}' in key


def test_the_ids_and_token_are_read_from_the_instance_so_an_edit_applies_without_a_restart(store: Store, tmp_path):
    # given
    http = google([row('Chairs', f'{SITE}/a')])

    # when
    with app_with(store, tmp_path, http, instances={}) as c:
        c.app.state.scheduler.nudge = lambda name: None
        made = c.post('/api/admin/plugins/google-ads/instances', json={'name': 'main', 'values': VALUES, 'enabled': True}, headers=AUTH)
        b = box(store, plugin_tools=PluginTools(c.app.state.runtime))
        first = b.call('google_ads', {'op': 'ads'})
        changed = c.patch('/api/admin/plugins/google-ads/instances/main', json={'values': {'developer-token': 'DEVTOKEN-new-0001'}}, headers=AUTH)
        second = b.call('google_ads', {'op': 'ads'})

    # then
    assert (made.status_code, changed.status_code, first[1], second[1]) == (201, 200, False, False)
    sent = [(h[2]['developer-token'], h[2]['login-customer-id']) for h in http.hits(SEARCH)]
    assert sent == [(TOKEN, '9876543210'), ('DEVTOKEN-new-0001', '9876543210')]


def test_admins_set_the_customer_id_as_a_field_and_the_developer_token_as_a_secret(store: Store, tmp_path):
    # given
    http = google()

    # when
    with app_with(store, tmp_path, http, instances={}) as c:
        c.app.state.scheduler.nudge = lambda name: None
        made = c.post('/api/admin/plugins/google-ads/instances', json={'name': 'main', 'values': VALUES, 'enabled': True}, headers=AUTH)
        listing = c.get('/api/admin/plugins', headers=AUTH)
    stored = repr(list(store.db['instances'].find()))

    # then
    [shown] = [p for p in listing.json()['plugins'] if p['id'] == 'google-ads'][0]['instances']
    assert made.status_code == 201 and shown['values']['customer-id'] == SHOWN
    assert shown['secrets'] == {'developer-token': {'set': True, 'from': None}, 'credentials': {'set': True, 'from': None}}
    assert TOKEN not in made.text + listing.text + stored


def test_google_ads_identity_and_manifest():
    # given
    registry = Registry.load({}, entry_points=False, core_tools=CORE_TOOLS, core_knowledge=prompt.core_anchors())

    # when
    found = registry.get('google-ads')
    manifest = describe(google_ads.PLUGIN, icon=True)
    gads, policy = google_ads.PLUGIN.checks
    tool = google_ads.PLUGIN.tools[0]

    # then
    assert 'skopax.plugins.google_ads' in BUILTIN and found is not None and found.state == 'ok' and found.plugin is google_ads.PLUGIN
    assert json.loads(json.dumps(manifest, allow_nan=False)) == manifest
    assert (manifest['id'], manifest['name'], manifest['version'], manifest['category'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition']) == (
        'google-ads', 'Google Ads', '0.1.0', 'marketing', 'Skopax', 'Apache-2.0', '>=0.1,<0.4', 'community')
    assert manifest['capabilities'] == {'hosts': ['googleads.googleapis.com', 'oauth2.googleapis.com', '*'], 'secrets': ['developer-token', 'credentials'], 'agentReads': False}
    assert 'only that site' in next(f for f in google_ads.PLUGIN.fields if f.key == 'website').help
    assert manifest['uses'] == ['website']
    assert [(f.key, f.kind, f.required) for f in google_ads.PLUGIN.fields] == [('customer-id', 'text', True), ('login-customer-id', 'text', False),
                                                                            ('developer-token', 'secret', False), ('credentials', 'secret', True),
                                                                            ('api-version', 'text', False), ('website', 'instance', False)]
    assert (gads.id, gads.period, gads.offset, gads.ran, gads.sections, gads.tool, gads.once, gads.timeout, gads.background) == (
        'gads', 900, 750, ('gads',), ('adLinks',), 'google_ads', False, 900, True)
    assert (policy.id, policy.period, policy.offset, policy.ran, policy.sections, policy.tool, policy.once, policy.timeout, policy.background) == (
        'gads-policy', 900, 780, ('gads-policy',), (), 'google_ads', False, None, True)
    assert (tool.name, tool.kind, tool.per_run, tool.scan, tool.instance_input) == ('google_ads', 'read', 6, True, 'instance')
    assert google_ads.PLUGIN.rules == () and google_ads.PLUGIN.releases is None and registry.icon('google-ads').startswith('<svg')
    assert [k.id for k in google_ads.PLUGIN.knowledge] == ['google-ads'] and len(google_ads.PLUGIN.knowledge[0].text.splitlines()) <= 40


def test_google_ads_defaults_are_neutral():
    # then
    assert {f.key: f.default for f in google_ads.PLUGIN.fields} == {'customer-id': '', 'login-customer-id': '', 'developer-token': '', 'credentials': '',
                                                                   'api-version': 'v26', 'website': ''}
    assert {s.key: s.default for s in google_ads.PLUGIN.settings} == {'auto-resolve-hours': '24'}


def test_google_ads_connection_test_reports_ok_and_failures(store):
    # given
    unauthorised = HttpError(401, SEARCH, '{"error": {"message": "Request had invalid authentication credentials.", "status": "UNAUTHENTICATED"}}')
    cases = {'ok': (google(), VALUES), 'unnamed': (google(over={('POST', SEARCH): gaql([], name='')}), VALUES),
             'refused': (google(over={('POST', SEARCH): unauthorised}), VALUES), 'forbidden': (google(over={('POST', SEARCH): HttpError(403, SEARCH)}), VALUES),
             'key refused': (google(token=HttpError(401, TOKEN_URL, '{"error": "invalid_client"}')), VALUES),
             'unreachable': (google(token=Unreachable('https://oauth2.googleapis.com/token unreachable (timed out)')), VALUES),
             'bad id': (google(), dict(VALUES, **{'customer-id': '12-34'})), 'bad version': (google(), dict(VALUES, **{'api-version': '26'}))}

    # when
    got = {name: tool_runtime(store, http, instances={}).connection_test(google_ads.PLUGIN, None, dict(values), 5, NOW) for name, (http, values) in cases.items()}

    # then
    assert (got['ok']['ok'], got['ok']['summary']) == (True, 'Google Ads \u00b7 Example Shop')
    assert (got['unnamed']['ok'], got['unnamed']['summary']) == (True, f'Google Ads \u00b7 account {SHOWN}')
    assert queries(cases['ok'][0]) == ['SELECT customer.id, customer.descriptive_name FROM customer LIMIT 1']
    assert (got['refused']['ok'], got['refused']['summary']) == (
        False, f'the account {SHOWN}: the service account is invalid or lacks {NEEDS} (HTTP 401: Request had invalid authentication credentials.)')
    assert (got['forbidden']['ok'], got['forbidden']['summary']) == (False, f'the account {SHOWN}: {NO_ACCESS} (HTTP 403)')
    assert (got['key refused']['ok'], got['key refused']['summary']) == (False, msg('connection.rejected', status=401))
    assert (got['unreachable']['ok'], got['unreachable']['summary']) == (False, msg('connection.unreachable'))
    assert (got['bad id']['ok'], got['bad id']['summary']) == (False, engine.BAD_CUSTOMER)
    assert (got['bad version']['ok'], got['bad version']['summary']) == (False, engine.BAD_VERSION)


def test_google_ads_second_instance_is_scoped(store: Store, tmp_path):
    # given
    east = {'customer-id': '222-333-4444', 'developer-token': TOKEN, 'credentials': KEY}
    http = google([row('Main', f'{SITE}/main')], {f'{SITE}/main': page(200), f'{SITE}/east': page(404)},
                  over={('POST', EAST_SEARCH): gaql([row('East', f'{SITE}/east', cid='31', approval='DISAPPROVED', topics=(DOWN,))])})

    # when
    with app_with(store, tmp_path, http, instances={'main': VALUES, 'east': east}) as c:
        for check in ('gads', 'gads-policy'):
            c.app.state.sweeper.run('google-ads', check, NOW)
    state = store.load()
    policy = next(a for a in state['anomalies'] if a['sig'] == 'ad-disapproved')
    broken = next(a for a in state['anomalies'] if a['sig'] == BROKEN)

    # then
    assert (policy['cluster'], policy['ranKey'], broken['cluster'], broken['ranKey']) == ('east', 'gads-policy-east', 'east', f'gads:{SITE}/east-east')
    assert {k: state['sources'][k] for k in ('gads', 'gads-east', 'gads-policy', 'gads-policy-east')} == dict.fromkeys(('gads', 'gads-east', 'gads-policy', 'gads-policy-east'), 'ok')
    assert broken['key'].endswith('@east') and policy['linkChecks'][0]['card'] == broken['key'], 'the policy card names the link card by the key it is stored under'
    assert f"(card {broken['key']})" in policy['what']


def test_the_instance_name_policy_is_refused_because_it_shadows_the_policy_check():
    # given
    registry = Registry.of(google_ads.PLUGIN)

    # then
    assert registry.shadowed('google-ads', 'policy') and not registry.shadowed('google-ads', 'east')


def test_google_ads_logs_and_state_carry_no_key_or_token(store: Store, tmp_path, caplog):
    # given
    http = google(over={('POST', SEARCH): HttpError(500, SEARCH, f'{{"error": {{"message": "boom {ACCESS} {TOKEN}"}}}}')})

    # when
    with caplog.at_level(logging.DEBUG):
        with app_with(store, tmp_path, http) as c:
            for check in ('gads', 'gads-policy'):
                c.app.state.sweeper.run('google-ads', check, NOW)
            text, err = box(store, plugin_tools=PluginTools(c.app.state.runtime)).call('google_ads', {'op': 'campaigns'})
    state = store.load()

    # then
    assert (state['sources']['gads'], state['sources']['gads-policy'], err) == ('error: Google Ads HTTP 500', 'error: Google Ads HTTP 500', True)
    seen = caplog.text + json.dumps(state, default=str) + text + repr(list(store.activity.find()))
    assert ACCESS not in seen and TOKEN not in seen and 'PRIVATE KEY' not in seen


PRIVATE = ['https://10.0.0.12/x', 'https://169.254.169.254/latest/meta-data', 'https://metadata.google.internal/computeMetadata/v1',
           'https://redis.default.cluster.local/x', 'https://[fd00::1]/x']


def test_without_a_website_peer_the_link_check_never_opens_a_private_address_or_an_internal_name(names):
    # given
    names.private['intranet.example.com'] = ['fd00::5']
    urls = PRIVATE + ['https://intranet.example.com/x']
    http = google([row(f'P{i}', u, ad_id=str(i)) for i, u in enumerate(urls)] + [row('Shop', f'{SITE}/chairs', ad_id='9')], {f'{SITE}/chairs': page(200)})
    stale = [{'src': 'gads', 'sig': BROKEN, 'url': u} for u in ('https://10.0.0.12/x', 'https://intranet.example.com/x')]

    # when
    swept = links(Ctx(VALUES, http, cards=stale))

    # then
    out = swept.result
    assert fetched(http) == [f'{SITE}/chairs'] and out.found == []
    assert out.status == ('ok \u00b7 not opened, not a public host: https://10.0.0.12/x, https://169.254.169.254/latest/meta-data, https://[fd00::1]/x '
                          'and 3 more')
    assert out.ran == ('gads:https://intranet.example.com/x', f'gads:{SITE}/chairs', 'gads:https://10.0.0.12/x', 'gads:access:account'), 'a refused link gets no card'
    assert sorted(h for h, _ in names.asked) == ['intranet.example.com', 'shop.example.com'], 'a private address or internal name is never looked up'
    assert [r['url'] for r in swept.sections['adLinks'] if r['part'] == 'url'] == [f'{SITE}/chairs']


def test_a_redirect_to_a_name_that_resolves_private_is_not_followed_and_its_card_is_held(names):
    # given
    names.private['admin.shop.example.com'] = ['10.0.0.12']
    http = google([row('Go', f'{SITE}/go', ad_id='1'), row('Shop', f'{SITE}/chairs', ad_id='2')],
                  {f'{SITE}/go': page(301, location='https://admin.shop.example.com/x'), f'{SITE}/chairs': page(200)})
    held = {'src': 'gads', 'sig': BROKEN, 'url': f'{SITE}/go'}

    # when
    swept = links(Ctx(VALUES, http, cards=[held]))

    # then
    out = swept.result
    assert sorted(fetched(http)) == [f'{SITE}/chairs', f'{SITE}/go'] and 'https://admin.shop.example.com/x' not in fetched(http)
    assert (out.status, out.ran) == ('ok \u00b7 read 1 of 2 pages', (f'gads:{SITE}/chairs', 'gads:access:account'))
    assert ('admin.shop.example.com', False) in names.asked


def test_a_redirect_to_a_metadata_address_is_never_fetched_and_its_target_never_reaches_the_status():
    # given
    target = 'https://169.254.169.254/latest/meta-data?note=ignore-previous-instructions'
    http = google([row('Meta', f'{SITE}/meta')], {f'{SITE}/meta': page(301, location=target)})
    held = {'src': 'gads', 'sig': BROKEN, 'url': f'{SITE}/meta'}

    # when
    out = links(Ctx(VALUES, http, cards=[held])).result

    # then
    assert fetched(http) == [f'{SITE}/meta'] and out.ran == ('gads:access:account',)
    assert out.status == 'error: pages: no page was read (shop.example.com: a redirect was not followed)'


def test_a_long_refused_url_is_cut_in_the_status():
    # given
    long = 'https://10.0.0.12/' + 'x' * 300
    http = google([row('Long', long)])

    # when
    out = links(Ctx(VALUES, http)).result

    # then
    assert out.status == f'ok \u00b7 no data for pages \u00b7 not opened, not a public host: {long[:engine.URL_SHOWN]}'


def test_a_name_that_does_not_resolve_did_not_answer_and_keeps_its_card(names):
    # given
    names.gone.add('old.example.org')
    http = google([row('Old', 'https://old.example.org/x', ad_id='1'), row('Shop', f'{SITE}/chairs', ad_id='2')], {f'{SITE}/chairs': page(200)})
    held = {'src': 'gads', 'sig': BROKEN, 'url': 'https://old.example.org/x'}

    # when
    swept = links(Ctx(VALUES, http, cards=[held]))

    # then
    out = swept.result
    assert fetched(http) == [f'{SITE}/chairs'] and (out.status, out.ran) == ('ok \u00b7 read 1 of 2 pages', (f'gads:{SITE}/chairs', 'gads:access:account'))
    assert swept.sections['adLinks'][0] == {'part': 'url', 'url': 'https://old.example.org/x', 'error': 'old.example.org did not answer: could not be reached (gaierror)',
                                            'ads': 1}


def test_with_a_website_peer_its_host_rules_apply_and_no_name_is_looked_up(names):
    # given
    http = google([row('Shop', f'{SITE}/chairs')], {f'{SITE}/chairs': page(200)})

    # when
    out = links(Ctx(VALUES, http, site={'url': SITE})).result

    # then
    assert (out.status, fetched(http), names.asked) == ('ok', [f'{SITE}/chairs'], [])


@pytest.mark.parametrize('status', [401, 403, 407, 429])
def test_a_blocked_page_proves_nothing_so_it_is_not_read(status):
    # given
    http = google([row('Shop', f'{SITE}/chairs', ad_id='1'), row('Lamps', f'{SITE}/lamps', ad_id='2')], {f'{SITE}/chairs': page(status), f'{SITE}/lamps': page(200)})

    # when
    swept = links(Ctx(VALUES, http))

    # then
    assert verdict({'status': status, 'chain': [{'url': f'{SITE}/a'}]}, f'{SITE}/a') == ('unknown', status, f'{SITE}/a')
    assert (swept.result.status, swept.result.ran) == ('ok \u00b7 read 1 of 2 pages', (f'gads:{SITE}/lamps', 'gads:access:account'))
    assert swept.sections['adLinks'][0]['status'] == status and swept.result.found == []


def test_a_broken_link_card_stays_open_when_the_site_starts_refusing_the_check(store: Store, tmp_path):
    # given
    http = google([row('Shop', f'{SITE}/gone', ad_id='1'), row('Lamps', f'{SITE}/lamps', ad_id='2')], {f'{SITE}/gone': page(404), f'{SITE}/lamps': page(200)})

    # when
    with app_with(store, tmp_path, http) as c:
        c.app.state.sweeper.run('google-ads', 'gads', NOW)
        before = by_key(store.load())
        http.routes[('GET', f'{SITE}/gone')] = page(403)
        c.app.state.sweeper.run('google-ads', 'gads', NOW + 900)
    state = store.load()

    # then
    key = 'ad-link|ads/shop.example.com/gone'
    assert before[key]['status'] == 'open' and by_key(state)[key]['status'] == 'open', 'a 403 is no proof that the page works'
    assert state['sources']['gads'] == 'ok \u00b7 read 1 of 2 pages'


def test_a_pass_where_no_page_answered_fails_the_pages_part_and_holds_every_link_card(store: Store, tmp_path, names):
    # given
    down = Unreachable('shop.example.com unreachable (timed out)')
    http = google([row('Shop', f'{SITE}/gone', ad_id='1'), row('Lamps', f'{SITE}/lamps', ad_id='2')], {f'{SITE}/gone': page(404), f'{SITE}/lamps': page(200)})

    # when
    with app_with(store, tmp_path, http) as c:
        c.app.state.sweeper.run('google-ads', 'gads', NOW)
        http.routes.update({('GET', f'{SITE}/gone'): down, ('GET', f'{SITE}/lamps'): down})
        swept = links(Ctx(VALUES, http))
        c.app.state.sweeper.run('google-ads', 'gads', NOW + 900)
    state = store.load()

    # then
    reason = 'shop.example.com did not answer: could not be reached (Unreachable)'
    assert (swept.result.ok, swept.result.status, swept.result.ran) == (True, f'error: pages: no page was read ({reason})', ('gads:access:account',))
    assert state['sources']['gads'] == f'error: pages: no page was read ({reason})'
    assert by_key(state)['ad-link|ads/shop.example.com/gone']['status'] == 'open', 'a page nobody saw never clears its card'
    assert [r.get('error') for r in state['adLinks'] if r['part'] == 'url'] == [reason, reason]
    assert names.asked and not any(loop for _, loop in names.asked), 'names are looked up in the pass worker, never on the event loop'


def test_every_ad_link_refused_by_the_site_is_no_data_for_pages():
    # given
    http = google([row('App', 'https://apps.example.org/app/id1'), row('Click-out', f'{SITE}/out/x')])

    # when
    out = links(Ctx(VALUES, http, site={'url': SITE, 'refused-paths': 'out'})).result

    # then
    assert (out.ok, out.status, out.ran, fetched(http)) == (True, 'ok \u00b7 no data for pages', ('gads:no-data', 'gads:access:account'), [])


def test_with_a_website_peer_the_link_check_and_the_page_tool_space_their_reads_with_one_pacer():
    # given
    http = google([row('Shop', f'{SITE}/chairs')], {f'{SITE}/chairs': page(200)})

    # when
    links(Ctx(VALUES, http, site={'url': SITE}))
    after_links = dict(web._pacers)
    website.page_tool(Ctx({'url': SITE}, http), {'url': f'{SITE}/chairs'})

    # then
    assert len(after_links) == 1 and after_links == web._pacers, 'one pacer per site for both plug-ins'
    assert next(iter(after_links.values())) is web.site_pacer(SITE)


def test_a_disapproved_ad_in_a_paused_ad_group_raises_no_card():
    # given
    http = google([row('Chairs', f'{SITE}/a', approval='DISAPPROVED', topics=(DOWN,), grouped='PAUSED')])

    # when
    result = google_ads.run_policy(Ctx(VALUES, http)).result

    # then
    assert (result.status, result.found) == ('ok', [])


def test_the_policy_card_names_the_ad_link_card_only_while_one_is_open():
    # given
    http = google([row('Chairs', f'{SITE}/broken', approval='DISAPPROVED', topics=(DOWN,))])
    seen = [link_row(f'{SITE}/broken', 404, BROKEN)]
    other = {'src': 'gads', 'sig': BROKEN, 'key': 'ad-link|ads/shop.example.com/else', 'url': f'{SITE}/else'}

    # when
    [card] = google_ads.run_policy(Ctx(VALUES, http, rows={'adLinks': seen}, cards=[other])).result.found

    # then
    assert card['linkChecks'][0]['card'] is None
    assert f'opened {SITE}/broken at ' in card['what'] and 'and got HTTP 404: the page fails from here too.' in card['what'] and '(card ' not in card['what']


def test_a_blocked_link_is_named_as_one_on_the_policy_card():
    # given
    http = google([row('Chairs', f'{SITE}/blocked', approval='DISAPPROVED', topics=(DOWN,))])
    seen = [link_row(f'{SITE}/blocked', 403, 'unknown')]

    # when
    [card] = google_ads.run_policy(Ctx(VALUES, http, rows={'adLinks': seen})).result.found

    # then
    assert 'and got HTTP 403: the site refused the check, so it could not tell whether the page works.' in card['what']


def test_a_query_value_that_starts_with_a_url_and_goes_on_is_redacted():
    # then
    assert scrubbed(f'{SITE}/sofas?ref={ALICE}') == f'{SITE}/sofas?ref={ALICE}'
    assert scrubbed(f'{SITE}/sofas see {ALICE}') == f'{SITE}/sofas see [redacted]'


class Ctx:
    def __init__(self, config: dict, transport, name: str = 'main', primary: bool = True, now: float = NOW, rows: dict | None = None, cards: list | None = None,
                 site: dict | Exception | None = None):
        self.config = config
        self.transport = transport
        self.now = now
        self.name = name
        self.primary = primary
        self.rows = rows or {}
        self.cards = cards or []
        self.site = site

    @property
    def instance(self) -> InstanceInfo:
        return InstanceInfo(self.name, self.primary, None if self.primary else self.name, self.config)

    def http(self):
        return self.transport

    def previous(self, section: str) -> list | None:
        assert section == 'adLinks'
        return copy.deepcopy(self.rows.get(section))

    def open_cards(self, plugin_wide: bool = False) -> list[dict]:
        return copy.deepcopy(self.cards)

    def peer(self, field: str) -> Peer | None:
        assert field == 'website'
        if isinstance(self.site, Exception):
            raise self.site
        return None if self.site is None else Peer(InstanceInfo('shop', True, None, self.site), lambda: self.transport)


def test_ads_in_a_paused_ad_group_are_neither_opened_nor_listed():
    # given
    rows = [row('Chairs', f'{SITE}/garden/chairs', ad_id='1'), row('Chairs', f'{SITE}/garden/paused', ad_id='2', grouped='PAUSED')]
    http = google(rows, {f'{SITE}/garden/chairs': page(200)})

    # when
    swept = links(Ctx(VALUES, http, site={'url': SITE}))
    listed = read(ads_of(http), wanted({'op': 'ads'}), TODAY)

    # then
    assert [r['url'] for r in swept.sections['adLinks'] if r['part'] == 'url'] == [f'{SITE}/garden/chairs']
    assert [a['adId'] for a in listed['ads']] == ['1']
    assert "ad_group.status = 'ENABLED'" in engine.ads_query('', 10)


def test_no_developer_token_works_through_both_checks_and_sends_no_header():
    # given
    http = google()
    values = {k: v for k, v in VALUES.items() if k != 'developer-token'}

    # when
    out = [run(Ctx(values, http)).result for run in (links, google_ads.run_policy)]

    # then
    assert all(r.ok for r in out)
    assert http.hits(SEARCH) and all('developer-token' not in h[2] for h in http.hits(SEARCH))


def test_connection_test_without_a_developer_token_is_ok_and_a_malformed_one_is_refused(store):
    # given
    values = {k: v for k, v in VALUES.items() if k != 'developer-token'}
    http = google()

    # when
    ok = tool_runtime(store, http, instances={}).connection_test(google_ads.PLUGIN, None, dict(values), 5, NOW)
    bad = tool_runtime(store, google(), instances={}).connection_test(google_ads.PLUGIN, None, dict(values, **{'developer-token': 'two words'}), 5, NOW)

    # then
    assert (ok['ok'], ok['summary']) == (True, 'Google Ads \u00b7 Example Shop')
    assert http.hits(SEARCH) and all('developer-token' not in h[2] for h in http.hits(SEARCH))
    assert (bad['ok'], bad['summary']) == (False, engine.BAD_TOKEN)
