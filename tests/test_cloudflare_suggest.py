import time
from collections.abc import Callable
from types import MappingProxyType

import pytest

from skopax.contract import ReadError, Refused, SuggestContext
from skopax.plugins import cloudflare
from skopax.plugins.cloudflare.checks import components_of
from skopax.plugins.cloudflare.client import API, STATUS
from skopax.registry import describe
from skopax.remote import HttpError, Unreachable
from tests.fake_http import FakeHttp

TOKEN = 'cf-api-token-3e9a'
ZONES = f'{API}/zones'
TOKYO, OSAKA = 'Tokyo, Japan - (NRT)', 'Osaka, Japan - (KIX)'


def asked(http, seconds: float = 10, **values) -> SuggestContext:
    return SuggestContext(MappingProxyType(values), http, time.monotonic() + seconds)


def fields() -> dict:
    return {f.key: f for f in cloudflare.PLUGIN.fields}


def pages(*names: list[str], before: Callable[[int], object] | None = None):
    def answer(q, _):
        page = int(q['page'][0])
        if before is not None:
            failed = before(page)
            if failed is not None:
                return failed
        return {'success': True, 'result': [{'id': '0' * 32, 'name': n} for n in names[page - 1]], 'result_info': {'page': page, 'total_pages': len(names)}}
    return answer


def test_status_components_and_zones_offer_a_list_joined_by_their_separator():
    # given
    found = fields()

    # when
    views = {f['key']: f for f in describe(cloudflare.PLUGIN)['fields']}

    # then
    assert (found['status-components'].separator, found['zones'].separator) == (';', ',')
    assert (views['status-components']['suggest'], views['zones']['suggest']) == (True, True)
    assert 'pick' in found['status-components'].help.lower() and 'pick' in found['zones'].help.lower()


def test_status_components_lists_every_component_and_group_name_the_check_matches_without_a_token():
    # given
    body = {'components': [{'name': TOKYO, 'status': 'operational', 'group_id': 'g1'}, {'name': 'Asia', 'status': 'operational', 'group': True}, {'name': 'CDN/Cache'},
                           {'name': TOKYO}, {'name': 7}, 'odd', {'status': 'operational'}, {'name': OSAKA}], 'incidents': []}
    http = FakeHttp({('GET', STATUS): body})

    # when
    got = fields()['status-components'].suggest(asked(http))

    # then
    assert got == [TOKYO, 'Asia', 'CDN/Cache', TOKYO, OSAKA]
    assert all(components_of(name) == [name] for name in got)
    assert 'Authorization' not in http.calls[0][2]


def test_status_components_names_a_status_page_that_did_not_answer():
    # given
    http = FakeHttp({('GET', STATUS): Unreachable('down')})

    # when
    with pytest.raises(ReadError) as e:
        fields()['status-components'].suggest(asked(http))

    # then
    assert e.value.status == 502 and e.value.message == 'Cloudflare status page unreachable'


def test_zones_reads_every_page_with_the_token():
    # given
    http = FakeHttp({('GET', ZONES): pages(['shop.example.com', 'blog.example.com'], ['Docs.Example.org', 'not a zone'])})

    # when
    got = fields()['zones'].suggest(asked(http, token=TOKEN))

    # then
    assert got.values == ('shop.example.com', 'blog.example.com', 'docs.example.org') and got.note == ''
    assert len(http.hits(ZONES)) == 2 and all(c[2]['Authorization'] == f'Bearer {TOKEN}' for c in http.hits(ZONES))


def test_zones_without_a_token_asks_for_it_and_calls_nothing():
    # given
    http = FakeHttp({('GET', ZONES): pages(['shop.example.com'])})

    # when
    with pytest.raises(Refused) as e:
        fields()['zones'].suggest(asked(http, token='  '))

    # then
    assert str(e.value) == 'Enter the API token first' and http.calls == []


def test_zones_says_which_permission_is_missing_and_never_echoes_the_upstream_body():
    # given
    denied = FakeHttp({('GET', ZONES): HttpError(403, ZONES, f'{{"errors": [{{"code": 9109, "message": "no {TOKEN}"}}]}}')})
    broken = FakeHttp({('GET', ZONES): HttpError(500, ZONES, f'upstream {TOKEN}')})

    # when
    with pytest.raises(ReadError) as refused:
        fields()['zones'].suggest(asked(denied, token=TOKEN))
    with pytest.raises(ReadError) as failed:
        fields()['zones'].suggest(asked(broken, token=TOKEN))

    # then
    assert refused.value.status == 400 and refused.value.message == 'Cloudflare cannot list the zones: the token lacks Zone Read'
    assert failed.value.status == 502 and failed.value.message == 'Cloudflare API zones HTTP 500'
    assert TOKEN not in refused.value.message + failed.value.message


def test_zones_stops_at_twenty_pages_and_says_only_the_first_are_listed():
    # given
    http = FakeHttp({('GET', ZONES): pages(*[[f'z{p:02d}-{i:02d}.example.com' for i in range(50)] for p in range(21)])})

    # when
    got = fields()['zones'].suggest(asked(http, token=TOKEN))

    # then
    assert len(got.values) == 1000 and len(http.hits(ZONES)) == 20
    assert got.note == 'Only the first 20 pages of zones are read; 1000 zones listed, type a name to add another'


def test_zones_stops_paging_when_the_deadline_comes_and_keeps_what_it_read():
    # given
    http = FakeHttp({('GET', ZONES): pages(['shop.example.com'], ['blog.example.com'], ['docs.example.org'], before=lambda page: time.sleep(0.4))})

    # when
    got = fields()['zones'].suggest(asked(http, 1.2, token=TOKEN))

    # then
    assert got.values == ('shop.example.com',) and len(http.hits(ZONES)) == 1
    assert got.note == 'Cloudflare did not list every zone in time; 1 zone listed, type a name to add another'


def test_a_later_page_that_fails_keeps_the_zones_already_read():
    # given
    http = FakeHttp({('GET', ZONES): pages(['shop.example.com'], ['blog.example.com'], before=lambda page: HttpError(500, ZONES, 'upstream') if page == 2 else None)})

    # when
    got = fields()['zones'].suggest(asked(http, token=TOKEN))

    # then
    assert got.values == ('shop.example.com',)
    assert got.note == 'Cloudflare did not list every zone (Cloudflare API zones HTTP 500); 1 zone listed, type a name to add another'


def test_no_zones_page_may_outlive_the_deadline_and_the_note_counts_only_real_zone_names():
    # given
    limits: list[float] = []

    def transport(method, url, headers, data, timeout):
        limits.append(timeout)
        return http(method, url, headers, data, timeout)

    http = FakeHttp({('GET', ZONES): pages(['shop.example.com', 'not a zone'], ['blog.example.com'], before=lambda page: time.sleep(0.4) if page == 1 else None)})

    # when
    got = cloudflare.suggest_zones(SuggestContext(MappingProxyType({'token': TOKEN}), transport, time.monotonic() + 1.3))

    # then
    assert limits and limits[0] <= 1.0
    assert got.values == ('shop.example.com',) and len(http.hits(ZONES)) == 1
    assert got.note == 'Cloudflare did not list every zone in time; 1 zone listed, type a name to add another'
