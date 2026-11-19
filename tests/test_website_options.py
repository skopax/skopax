import pytest
from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.kit import web
from skopax.kit.web import PageCheck, Pacer, Site, refusal
from skopax.plugins.website import pagescan
from tests.conftest import AUTH, settings
from tests.fake_cdp import FakeChrome, Site as ChromeSite
from tests.fake_http import FakePages
from tests.test_screenshot import browser
from tests.website_kit import CHAIRS, HOME, SITE, Clock, page, quiet, through

ORIGIN = 'https://shop.example.com'


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    quiet(monkeypatch)


def site(**config) -> Site:
    return Site.of({'url': SITE} | config)


def check(pages: FakePages, **config) -> PageCheck:
    clock = Clock()
    return PageCheck(pages, site(**config), clock=clock, pacer=Pacer(clock=clock, sleep=clock.sleep))


def test_more_tracking_parameters_are_refused_by_exact_name_or_prefix():
    # given
    plain, own = site(), site(**{'tracking-params': 'mc_cid, PK_*'})

    # then
    assert refusal(f'{CHAIRS}?pk_campaign=x', plain) is None and refusal(f'{CHAIRS}?mc_cid=1', plain) is None
    assert 'pk_campaign' in refusal(f'{CHAIRS}?Pk_Campaign=x&page=2', own)
    assert 'mc_cid' in refusal(f'{CHAIRS}?mc_cid=1', own)
    assert refusal(f'{CHAIRS}?mc_cidx=1&color=red', own) is None and refusal(f'{CHAIRS}?color=red', own) is None
    assert 'utm_source' in refusal(f'{CHAIRS}?utm_source=x', own) and 'gclid' in refusal(f'{CHAIRS}?gclid=x', plain)


def test_tracking_names_that_are_not_plain_parameter_names_are_dropped_from_the_list():
    # then
    assert site(**{'tracking-params': 'ok_1, bad name, *, a;b, ' + 'x' * 70 + ', ok_1'}).tracking == ('ok_1',)
    assert site().tracking == ()


def reply_headers(**extra):
    return page(200, b'<title>x</title>', content_type='text/html', cache_control='no-store', x_cache='HIT', x_served_by='cache-1', set_cookie='id=1', authorization='Bearer x', **extra)


def test_more_response_headers_come_back_next_to_the_usual_ones_and_cookies_never_do():
    # given
    pages = FakePages({CHAIRS: reply_headers()})

    # when
    plain = check(pages).get(CHAIRS)['headers']
    own = check(pages, **{'kept-headers': 'X-Cache, x-served-by, set-cookie, authorization, x-missing'}).get(CHAIRS)['headers']

    # then
    assert plain == {'cache-control': 'no-store', 'content-type': 'text/html'}
    assert own == {'cache-control': 'no-store', 'content-type': 'text/html', 'x-cache': 'HIT', 'x-served-by': 'cache-1'}
    assert site(**{'kept-headers': 'set-cookie, cookie, Authorization, bad header, x-ok'}).kept == ('x-ok',)


def test_the_page_scan_skips_the_prefixes_of_the_instance_next_to_its_own():
    # given
    busy = ['/checkout/step-1', '/checkout', '/checkoutx', '/pricing', '/static/app', '/account']

    # when
    plain = pagescan.page_urls(busy, [], 20, site(), ORIGIN)
    own = pagescan.page_urls(busy, [], 20, site(**{'scan-skip': '/checkout, /account/'}), ORIGIN)

    # then
    assert plain == [f'{ORIGIN}/checkout/step-1', f'{ORIGIN}/checkout', f'{ORIGIN}/checkoutx', f'{ORIGIN}/pricing', f'{ORIGIN}/account']
    assert own == [f'{ORIGIN}/checkoutx', f'{ORIGIN}/pricing']
    assert site(**{'scan-skip': 'checkout, /, /a b/../c'}).skip == ('/checkout', '/c')


def user_agent_of(pages: FakePages) -> str:
    return pages.calls[0][1]['User-Agent']


def test_a_custom_user_agent_replaces_the_built_in_one_in_page_requests():
    # given
    built_in, custom = FakePages({CHAIRS: page(200, b'<title>x</title>')}), FakePages({CHAIRS: page(200, b'<title>x</title>')})

    # when
    check(built_in).get(CHAIRS)
    check(custom, **{'user-agent': 'AcmeBot/2.0 (+https://example.com/bot)'}).get(CHAIRS)

    # then
    assert user_agent_of(built_in) == web.user_agent() and user_agent_of(custom) == 'AcmeBot/2.0 (+https://example.com/bot)'


@pytest.mark.parametrize('raw', ['bad\r\nX: y', 'caf\u00e9', 'x' * 201, '\t', ''])
def test_a_user_agent_a_header_cannot_carry_is_ignored(raw):
    # then
    assert site(**{'user-agent': raw}).agent == ''


def test_a_user_agent_is_trimmed_and_kept():
    # then
    assert site(**{'user-agent': '  AcmeBot/2.0  '}).agent == 'AcmeBot/2.0'


def test_the_screenshot_browser_takes_the_custom_user_agent_as_its_product_token(monkeypatch):
    # given
    chrome = FakeChrome({CHAIRS: ChromeSite()})
    try:
        # when
        browser(chrome).shoot(CHAIRS, site())
        browser(chrome).shoot(CHAIRS, site(**{'user-agent': 'AcmeBot/2.0'}), 'phone')
        sent = [p['userAgent'] for p in chrome.params('Emulation.setUserAgentOverride')]
    finally:
        chrome.close()

    # then
    assert sent[0].endswith(web.user_agent()) and 'Chrome/' in sent[0]
    assert sent[1].endswith(' AcmeBot/2.0') and 'Chrome/' in sent[1] and 'Mobile' in sent[1]


def test_the_connection_test_sends_the_custom_user_agent_and_refuses_one_it_cannot_send(store):
    # given
    pages = FakePages({HOME: page(200, b'<title>x</title>', content_type='text/html')})

    def tried(values: dict) -> dict:
        with TestClient(create_app(settings(), store, transport=through(pages))) as c:
            res = c.post('/api/admin/plugins/website/test', json={'values': {'url': SITE} | values}, headers=AUTH)
        assert res.status_code == 200
        return res.json()

    # when
    fine = tried({'user-agent': 'AcmeBot/2.0'})
    refused = tried({'user-agent': 'caf\u00e9'})

    # then
    assert fine['ok'] is True and pages.calls[-1][1]['User-Agent'] == 'AcmeBot/2.0'
    assert refused['ok'] is False and 'User-Agent' in refused['summary'] and len(pages.calls) == 1


def test_names_that_look_like_secrets_are_never_kept_whatever_their_prefix():
    # then
    kept = site(**{'kept-headers': 'x-auth-token, X-Session-Id, x-api-secret, x-csrf-token, x-forwarded-cookie, proxy-authenticate, x-cache, x-served-by'}).kept
    assert kept == ('x-cache', 'x-served-by')


def test_scan_skip_takes_entries_without_a_leading_slash_and_ignores_case():
    # given
    busy = ['/Checkout/step-1', '/pricing', '/account/orders']

    # when
    own = pagescan.page_urls(busy, [], 20, site(**{'scan-skip': 'checkout, /Account'}), ORIGIN)

    # then
    assert site(**{'scan-skip': 'checkout, /Account, a b'}).skip == ('/checkout', '/account', '/a b')
    assert own == [f'{ORIGIN}/pricing']
