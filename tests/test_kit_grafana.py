import json
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from skopax.contract import HttpError, InstanceInfo, Unreachable
from skopax.kit import grafana
from skopax.kit.grafana import LIST_RIGHT, QUERY_RIGHT, Datasource, DatasourceError, client_for, explore
from skopax.remote import JSON_READ, Reply, TimedOut

URL = 'https://grafana.example.com'
TOKEN = 'test-token-1'
LISTED = [{'uid': 'ds-a', 'name': 'cloud-metrics', 'type': 'prometheus'}, {'uid': 'ds-b', 'name': 'edge', 'type': 'prometheus'}, 'junk', {'name': 'no-uid'}]


class Fake:
    def __init__(self, listed=LISTED, answer=None, error: Exception | None = None, list_error: Exception | None = None):
        self.listed = listed
        self.answer = answer if answer is not None else {'status': 'success', 'data': {'resultType': 'vector', 'result': [{'metric': {'instance': 'a'}, 'value': [1, '2.5']}]}}
        self.error = error
        self.list_error = list_error
        self.asked: list[tuple[str, dict, dict, float]] = []

    def __call__(self, method, url, headers, data, timeout):
        parts = urlsplit(url)
        self.asked.append((parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}, headers, timeout))
        if parts.path == '/api/datasources':
            if self.list_error is not None:
                raise self.list_error
            return Reply(self.listed, {})
        if self.error is not None:
            raise self.error
        return Reply(self.answer, {})


def test_a_datasource_is_found_by_its_name_once_and_queried_through_the_proxy_with_the_time_given():
    # given
    fake = Fake()
    d = Datasource(URL + '/', TOKEN, fake, 'edge')

    # when
    first = d.result('query', query='up', time='100.000')
    again = d.result('query', query='up')

    # then
    assert d.uid() == 'ds-b' and d.found == 'ds-b'
    assert [a[0] for a in fake.asked] == ['/api/datasources', '/api/datasources/proxy/uid/ds-b/api/v1/query', '/api/datasources/proxy/uid/ds-b/api/v1/query']
    assert fake.asked[1][1] == {'query': 'up', 'time': '100.000'}
    assert all(a[2]['Authorization'] == f'Bearer {TOKEN}' for a in fake.asked)
    assert first == again == [{'metric': {'instance': 'a'}, 'value': [1, '2.5']}]


def test_a_uid_finds_the_datasource_too_and_a_name_wins_over_a_uid():
    # given
    named_like_uid = LISTED + [{'uid': 'ds-c', 'name': 'ds-a', 'type': 'prometheus'}]

    # when
    by_uid = Datasource(URL, TOKEN, Fake(), 'ds-a').uid()
    by_name = Datasource(URL, TOKEN, Fake(named_like_uid), 'ds-a').uid()

    # then
    assert (by_uid, by_name) == ('ds-a', 'ds-c')


def test_an_unknown_or_renamed_datasource_is_named_in_the_error():
    # given
    d = Datasource(URL, TOKEN, Fake(), 'old-name')

    # when
    with pytest.raises(DatasourceError) as e:
        d.result('query', query='up')

    # then
    assert e.value.reason == 'no Grafana datasource named old-name' and e.value.code is None


@pytest.mark.parametrize('fake, reason, code, right', [
    (Fake(list_error=HttpError(403, URL)), 'Grafana datasources: HTTP 403', 403, LIST_RIGHT),
    (Fake(error=HttpError(403, URL)), 'edge: HTTP 403', 403, QUERY_RIGHT),
    (Fake(error=Unreachable('down')), 'edge: unreachable', None, ''),
    (Fake(listed={'not': 'a list'}), 'Grafana datasources: the answer is not a list', None, ''),
    (Fake(answer={'status': 'error', 'error': 'parse error'}), 'edge: the answer is not a Prometheus success', None, ''),
])
def test_a_refused_unreachable_or_odd_answer_is_one_datasource_error_with_the_right_it_needs(fake, reason, code, right):
    # given
    d = Datasource(URL, TOKEN, fake, 'edge')

    # when
    with pytest.raises(DatasourceError) as e:
        d.result('query', query='up')

    # then
    assert (e.value.reason, e.value.code, e.value.right) == (reason, code, right)


def test_a_bad_query_answer_carries_the_error_text_of_the_datasource():
    # given
    body = json.dumps({'status': 'error', 'errorType': 'bad_data', 'error': '1:5: parse error: unexpected end of input'})
    d = Datasource(URL, TOKEN, Fake(error=HttpError(400, URL, body)), 'edge')

    # when
    with pytest.raises(DatasourceError) as e:
        d.result('query', query='sum(')

    # then
    assert e.value.code == 400 and e.value.said == '1:5: parse error: unexpected end of input'


def test_vec_keeps_finite_numbers_only():
    # given
    answer = {'status': 'success', 'data': {'result': [{'metric': {'i': 'a'}, 'value': [1, '1']}, {'metric': {'i': 'b'}, 'value': [1, 'NaN']},
                                                       {'metric': {'i': 'c'}, 'value': [1, '+Inf']}, {'metric': {'i': 'd'}, 'value': [1, 'x']}, 'junk']}}

    # when
    found = Datasource(URL, TOKEN, Fake(answer=answer), 'edge').vec('up')

    # then
    assert found == [({'i': 'a'}, 1.0)]


def test_the_client_comes_from_a_grafana_instance_and_the_timeout_reaches_every_call():
    # given
    fake = Fake()
    info = InstanceInfo('main', True, None, {'url': URL, 'token': TOKEN})

    # when
    d = client_for(info, 'edge', fake, timeout=7)
    d.result('query', query='up')

    # then
    assert d.url == URL and d.name == 'edge'
    assert [a[3] for a in fake.asked] == [7, 7]
    assert grafana.TIMEOUT == 20 and Datasource(URL, TOKEN, fake, 'edge').timeout == 20


def test_explore_links_open_an_https_grafana_only():
    # when
    link = explore(URL + '/', 'ds-b', 'up{job="a"}')

    # then
    assert link.startswith(f'{URL}/explore?schemaVersion=1&panes=')
    panes = json.loads(unquote(link.split('panes=', 1)[1]))
    assert panes['a']['queries'][0] == {'refId': 'A', 'expr': 'up{job="a"}', 'datasource': {'type': 'prometheus', 'uid': 'ds-b'}}
    assert explore('http://grafana.local', 'ds-b', 'up') is None and explore(URL, '', 'up') is None


def test_a_listing_read_once_serves_several_datasources_of_one_grafana():
    # given
    fake = Fake()
    listed = Datasource(URL, TOKEN, fake, 'edge').listed()

    # when
    uids = [Datasource(URL, TOKEN, fake, ref).uid(listed) for ref in ('edge', 'cloud-metrics', 'ds-a')]

    # then
    assert uids == ['ds-b', 'ds-a', 'ds-a'] and [a[0] for a in fake.asked] == ['/api/datasources']
    assert grafana.find(listed, 'edge') == 'ds-b'
    with pytest.raises(DatasourceError):
        grafana.find(listed, 'no-uid')


def test_a_size_limit_reaches_every_call():
    # given
    fake = Fake()

    # when
    Datasource(URL, TOKEN, fake, 'edge', limit=4096).result('query', query='up')
    Datasource(URL, TOKEN, fake, 'edge').uid()

    # then
    assert [a[2].get(JSON_READ) for a in fake.asked] == ['4096', '4096', None]


def test_a_prometheus_datasource_wins_a_name_shared_with_another_type_and_a_timeout_says_so():
    # given
    listed = [{'uid': 'loki-1', 'name': 'metrics', 'type': 'loki'}, {'uid': 'prom-1', 'name': 'metrics', 'type': 'prometheus'}]
    d = Datasource(URL, TOKEN, Fake(error=TimedOut('slow')), 'edge')

    # when
    with pytest.raises(DatasourceError) as e:
        d.result('query', query='up')

    # then
    assert grafana.find(listed, 'metrics') == 'prom-1'
    assert (e.value.reason, e.value.late) == ('edge: timed out', True)
