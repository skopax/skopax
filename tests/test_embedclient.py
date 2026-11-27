import dataclasses
import json
import logging
import math
import socket

import httpcore2
import httpx2
import pytest

from skopax import embedclient, messages
from skopax.embedclient import Breaker, EmbedClient, EmbedError
from skopax.messages import msg
from tests.conftest import settings

URL = 'http://skopax-embed'
MODEL = 'intfloat/multilingual-e5-small'
KEY = 'k-embed-secret-7731'
PRIVATE = '10.1.2.3'
PUBLIC = '93.184.216.34'


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def resolver(*ips: str):
    def resolve(host, port, *args, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port or 80)) for ip in ips]
    return resolve


@pytest.fixture(autouse=True)
def private_dns(monkeypatch):
    monkeypatch.setattr(embedclient, 'resolve', resolver(PRIVATE))
    embedclient.LEARNED.clear()


def vector(seed: int, dims: int = 384) -> list[float]:
    return [((seed * 31 + k) % 17) / 17 - 0.5 for k in range(dims)]


class Service:
    def __init__(self, status: int = 200, model: str = MODEL, dims: int = 384, body=None, shuffle: bool = False, drop: int = 0, bad: dict | None = None):
        self.status, self.model, self.dims, self.body, self.shuffle, self.drop, self.bad = status, model, dims, body, shuffle, drop, bad or {}
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if isinstance(self.body, Exception):
            raise self.body
        if self.body is not None:
            return httpx2.Response(self.status, content=self.body)
        if self.status != 200:
            return httpx2.Response(self.status, json={'error': f'detail {KEY}', 'error_type': 'x'})
        sent = json.loads(request.content)['input']
        data = [{'object': 'embedding', 'index': i, 'embedding': self.bad.get(i, vector(len(text), self.dims))} for i, text in enumerate(sent)][:len(sent) - self.drop]
        if self.shuffle:
            data.reverse()
        return httpx2.Response(200, json={'object': 'list', 'data': data, 'model': self.model, 'usage': {'prompt_tokens': 1, 'total_tokens': 1}})

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


def make(service, clock: Clock | None = None, breaker: Breaker | None = None, **over) -> EmbedClient:
    s = dataclasses.replace(settings(), embed_url=URL, **over)
    return EmbedClient(lambda: s, breaker=breaker or Breaker(clock or Clock()), transport=httpx2.MockTransport(service))


def failing(client: EmbedClient, texts=('a',), passages: bool = False) -> EmbedError:
    with pytest.raises(EmbedError) as caught:
        (client.embed_passages if passages else client.embed_queries)(list(texts))
    return caught.value


def test_a_query_is_posted_to_the_embeddings_path_with_the_prefix_the_model_and_no_dimensions():
    # given
    service = Service()
    client = make(service)

    # when
    got = client.embed_queries(['disk full', 'timeout'])

    # then
    request = service.requests[0]
    assert (request.method, str(request.url)) == ('POST', f'{URL}/v1/embeddings')
    assert service.bodies() == [{'input': ['query: disk full', 'query: timeout'], 'model': MODEL, 'encoding_format': 'float'}]
    assert 'dimensions' not in service.bodies()[0]
    assert [len(v) for v in got] == [384, 384] and got[0] == vector(len('query: disk full'))


def test_passages_take_the_passage_prefix_and_a_custom_prefix_is_used_as_set():
    # given
    service = Service()
    client = make(service, embed_passage_prefix='doc:')

    # when
    client.embed_passages(['a'])

    # then
    assert service.bodies()[0]['input'] == ['doc: a']


def test_a_model_without_a_prefix_sends_the_text_as_it_is():
    # given
    service = Service(model='org/plain')
    client = make(service, embed_model='org/plain')

    # when
    client.embed_queries(['a'])

    # then
    assert service.bodies()[0]['input'] == ['a'] and service.bodies()[0]['model'] == 'org/plain'


def test_the_bearer_key_is_sent_only_when_one_is_set():
    # given
    keyed, open_ = Service(), Service()

    # when
    make(keyed, embed_api_key=KEY).embed_queries(['a'])
    make(open_).embed_queries(['a'])

    # then
    assert keyed.requests[0].headers['authorization'] == f'Bearer {KEY}' and 'authorization' not in open_.requests[0].headers


def test_passages_are_split_by_the_batch_size_and_come_back_in_order():
    # given
    service = Service()
    client = make(service, embed_batch=3)
    texts = [str(n) * (n + 1) for n in range(7)]

    # when
    got = client.embed_passages(texts)

    # then
    assert [len(b['input']) for b in service.bodies()] == [3, 3, 1]
    assert got == [vector(len('passage: ' + t)) for t in texts]


def test_queries_use_the_short_timeout_and_batches_the_long_one():
    # given
    seen = []

    def watch(request):
        seen.append(request.extensions['timeout'])
        return Service()(request)
    client = make(watch, embed_timeout_seconds=2, embed_batch_timeout_seconds=45)

    # when
    client.embed_queries(['a'])
    client.embed_passages(['a'])

    # then
    assert seen[0]['read'] == 2 and seen[1]['read'] == 45


def test_no_texts_send_nothing():
    # given
    service = Service()

    # then
    assert make(service).embed_queries([]) == [] and make(service).embed_passages([]) == [] and service.requests == []


def test_an_answer_out_of_order_is_put_back_by_index():
    # given
    client = make(Service(shuffle=True))

    # when
    got = client.embed_queries(['a', 'bb', 'ccc'])

    # then
    assert got == [vector(len('query: ' + t)) for t in ('a', 'bb', 'ccc')]


@pytest.mark.parametrize('status, kind', [(401, 'auth'), (403, 'auth'), (422, 'bad_answer'), (400, 'bad_answer'), (413, 'bad_answer'), (500, 'down'), (502, 'down'), (503, 'down'), (429, 'down'), (404, 'bad_answer')])
def test_a_status_maps_to_a_kind_with_a_redacted_message(status, kind):
    # given
    client = make(Service(status=status), embed_api_key=KEY)

    # when
    error = failing(client)

    # then
    assert error.kind == kind and error.text == msg(f'embed.{kind}') and KEY not in error.text and 'detail' not in error.text


def test_a_timeout_and_a_refused_connection_are_down():
    # given
    slow = make(Service(body=httpx2.ReadTimeout('late')))
    shut = make(Service(body=httpx2.ConnectError('no route')))

    # then
    assert failing(slow).kind == 'down' and failing(shut).kind == 'down'


def test_a_name_that_does_not_resolve_is_down(monkeypatch):
    # given
    def nowhere(*a, **k):
        raise socket.gaierror('no such host')
    monkeypatch.setattr(embedclient, 'resolve', nowhere)

    # then
    assert failing(make(Service())).kind == 'down'


@pytest.mark.parametrize('body', [b'<html>proxy</html>', b'', b'[1, 2]', b'{"data": "x", "model": "' + MODEL.encode() + b'"}', b'{"model": "' + MODEL.encode() + b'"}'])
def test_an_answer_that_is_not_the_expected_json_is_a_bad_answer(body):
    # then
    assert failing(make(Service(body=body))).kind == 'bad_answer'


def test_the_wrong_count_is_a_bad_answer():
    # then
    assert failing(make(Service(drop=1)), ['a', 'b']).kind == 'bad_answer'


def test_a_repeated_or_missing_index_is_a_bad_answer():
    # given
    answer = json.dumps({'model': MODEL, 'data': [{'index': 0, 'embedding': vector(1)}, {'index': 0, 'embedding': vector(2)}]}).encode()

    # then
    assert failing(make(Service(body=answer)), ['a', 'b']).kind == 'bad_answer'


def test_the_wrong_dimension_is_a_bad_answer_for_the_default_model():
    # then
    assert failing(make(Service(dims=768))).kind == 'bad_answer'


def test_one_short_vector_among_good_ones_is_a_bad_answer():
    # then
    assert failing(make(Service(bad={1: vector(9, 383)})), ['a', 'b', 'c']).kind == 'bad_answer'


@pytest.mark.parametrize('broken', [float('nan'), float('inf'), None, 'x', True])
def test_a_vector_that_is_not_finite_numbers_is_a_bad_answer(broken):
    # given
    bad = vector(1)
    bad[5] = broken
    body = json.dumps({'model': MODEL, 'data': [{'index': 0, 'embedding': bad}]}, allow_nan=True).encode()

    # then
    assert failing(make(Service(body=body))).kind == 'bad_answer'


def test_a_model_that_is_not_the_one_set_is_a_mismatch():
    # given
    client = make(Service(model='org/other'))

    # when
    error = failing(client)

    # then
    assert error.kind == 'mismatch' and error.text == msg('embed.mismatch') and 'org/other' not in error.text


def test_an_answer_without_a_model_is_a_mismatch():
    # given
    body = json.dumps({'data': [{'index': 0, 'embedding': vector(1)}]}).encode()

    # then
    assert failing(make(Service(body=body))).kind == 'mismatch'


def test_an_unknown_model_learns_its_dimension_from_the_first_good_answer_and_holds_it():
    # given
    first = Service(model='org/plain', dims=10)
    client = make(first, embed_model='org/plain')
    client.embed_queries(['a'])
    second = make(Service(model='org/plain', dims=12), embed_model='org/plain')

    # then
    assert embedclient.dims_of('org/plain') == 10 and failing(second).kind == 'bad_answer'


def test_the_default_model_is_expected_at_384_without_a_first_answer():
    # then
    assert embedclient.dims_of(MODEL) == 384 and embedclient.dims_of('org/never-seen') is None


def test_vectors_of_different_length_in_the_first_answer_of_an_unknown_model_are_refused():
    # given
    client = make(Service(model='org/plain', bad={1: vector(1, 7)}, dims=8), embed_model='org/plain')

    # then
    assert failing(client, ['a', 'b']).kind == 'bad_answer' and embedclient.dims_of('org/plain') is None


def test_a_redirect_is_not_followed():
    # given
    service = Service(status=302)

    # then
    assert failing(make(service)).kind == 'bad_answer' and len(service.requests) == 1


def test_an_empty_url_is_off_and_sends_nothing():
    # given
    service = Service()
    client = EmbedClient(lambda: dataclasses.replace(settings(), embed_url=''), breaker=Breaker(Clock()), transport=httpx2.MockTransport(service))

    # when
    error = failing(client)

    # then
    assert error.kind == 'off' and error.text == msg('embed.reason.no_url') and service.requests == []


def test_a_public_address_is_refused_before_anything_is_sent(monkeypatch):
    # given
    monkeypatch.setattr(embedclient, 'resolve', resolver(PUBLIC))
    service = Service()

    # when
    error = failing(make(service))

    # then
    assert error.kind == 'refused' and error.text == msg('embed.refused') and service.requests == []


def test_a_public_address_is_allowed_when_the_deployment_allows_it(monkeypatch):
    # given
    monkeypatch.setattr(embedclient, 'resolve', resolver(PUBLIC))
    service = Service()

    # when
    got = make(service, embed_allow_public=True).embed_queries(['a'])

    # then
    assert len(got) == 1 and len(service.requests) == 1


@pytest.mark.parametrize('ip', ['169.254.169.254', '169.254.1.1', 'fe80::1', 'fd00:ec2::254', '100.100.100.200', '0.0.0.0', '::ffff:169.254.169.254'])
@pytest.mark.parametrize('allow_public', [False, True])
def test_metadata_and_link_local_addresses_are_always_refused(monkeypatch, ip, allow_public):
    # given
    monkeypatch.setattr(embedclient, 'resolve', resolver(ip))
    service = Service()

    # when
    error = failing(make(service, embed_allow_public=allow_public))

    # then
    assert error.kind == 'refused' and error.text == msg('embed.blocked') != msg('embed.refused') and 'public' not in error.text and service.requests == []


def test_one_bad_address_among_good_ones_refuses_the_name(monkeypatch):
    # given
    monkeypatch.setattr(embedclient, 'resolve', resolver(PRIVATE, '169.254.169.254'))

    # then
    assert failing(make(Service())).kind == 'refused'


@pytest.mark.parametrize('url', ['ftp://skopax-embed', 'http://user:pass@skopax-embed', 'http://skopax-embed/?x=1', 'skopax-embed', 'http://', 'http://skopax-embed/#f'])
def test_a_url_of_the_wrong_shape_is_refused(url):
    # given
    service = Service()
    client = EmbedClient(lambda: dataclasses.replace(settings(), embed_url=url), breaker=Breaker(Clock()), transport=httpx2.MockTransport(service))

    # then
    assert failing(client).kind == 'refused' and service.requests == []


@pytest.mark.parametrize('ip, allow_public, refused', [(PRIVATE, False, False), ('127.0.0.1', False, False), ('192.168.1.5', False, False), (PUBLIC, False, True), (PUBLIC, True, False),
                                                      ('169.254.169.254', True, True), ('fe80::1', True, True)])
def test_the_address_is_checked_again_on_each_connect_against_dns_rebinding(monkeypatch, ip, allow_public, refused):
    # given
    monkeypatch.setattr(embedclient, 'resolve', resolver(ip))
    connected = []

    class Inner:
        def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            connected.append(host)
            return 'stream'
    backend = embedclient.EmbedBackend(lambda: allow_public)
    backend.backend = Inner()

    # when
    if refused:
        with pytest.raises(embedclient.RefusedAddress):
            backend.connect_tcp('skopax-embed', 80, timeout=1)
    else:
        assert backend.connect_tcp('skopax-embed', 80, timeout=1) == 'stream'

    # then
    assert bool(connected) is not refused


def test_a_refusal_at_connect_is_a_refused_error_not_a_breaker_failure():
    # given
    breaker = Breaker(Clock())
    client = make(Service(body=embedclient.RefusedAddress()), breaker=breaker)

    # when
    errors = [failing(client) for _ in range(5)]

    # then
    assert {e.kind for e in errors} == {'refused'} and breaker.state()['state'] == 'closed'


def test_the_key_is_not_in_the_repr_the_logs_or_any_error_text(caplog):
    # given
    clients = [make(Service(status=s), embed_api_key=KEY) for s in (401, 500, 422)] + [make(Service(model='x'), embed_api_key=KEY), make(Service(body=b'nope'), embed_api_key=KEY)]

    # when
    with caplog.at_level(logging.DEBUG):
        errors = [failing(c) for c in clients]
        make(Service(), embed_api_key=KEY).embed_queries(['a'])

    # then
    shown = ' '.join([repr(c) for c in clients] + [str(e) + e.text + repr(e) for e in errors] + [caplog.text] + [repr(settings())])
    assert KEY not in shown and 'detail' not in caplog.text


def test_three_failures_in_a_row_open_the_breaker_and_no_call_is_made_while_it_is_open():
    # given
    clock, service = Clock(), Service(status=503)
    client = make(service, clock)

    # when
    for _ in range(3):
        failing(client)
    calls = len(service.requests)
    error = failing(client)

    # then
    assert calls == 3 and len(service.requests) == 3 and error.kind == 'down'


def test_a_success_resets_the_count_of_failures():
    # given
    breaker = Breaker(Clock())
    down, up = Service(status=503), Service()

    # when
    for _ in range(2):
        failing(make(down, breaker=breaker))
    make(up, breaker=breaker).embed_queries(['a'])
    for _ in range(2):
        failing(make(down, breaker=breaker))

    # then
    assert breaker.state()['state'] == 'closed' and breaker.state()['failures'] == 2


def test_the_breaker_stays_open_for_30_seconds_then_lets_one_probe_through():
    # given
    clock, service = Clock(), Service(status=503)
    client = make(service, clock)
    for _ in range(3):
        failing(client)

    # when
    clock.now += 29
    failing(client)
    before = len(service.requests)
    clock.now += 2
    failing(client)

    # then
    assert before == 3 and len(service.requests) == 4


def test_the_wait_doubles_on_each_failed_probe_up_to_ten_minutes():
    # given
    clock = Clock()
    breaker = Breaker(clock)
    service = Service(status=503)
    client = make(service, clock, breaker)
    waits = []

    # when
    for _ in range(3):
        failing(client)
    waits.append(breaker.state()['retry_in'])
    for _ in range(8):
        clock.now += waits[-1] + 0.1
        failing(client)
        waits.append(breaker.state()['retry_in'])

    # then
    assert waits[:6] == [30, 60, 120, 240, 480, 600] and max(waits) == 600 and waits[-1] == 600


def test_only_one_probe_runs_at_a_time_while_half_open():
    # given
    clock = Clock()
    breaker = Breaker(clock)
    service = Service(status=503)
    client = make(service, clock, breaker)
    for _ in range(3):
        failing(client)
    clock.now += 31

    # when
    first = breaker.allow()
    second = breaker.allow()

    # then
    assert (first, second) == (True, False) and breaker.state()['state'] == 'half-open'


def test_a_successful_probe_closes_the_breaker_and_resets_the_wait():
    # given
    clock = Clock()
    breaker = Breaker(clock)
    for _ in range(3):
        failing(make(Service(status=503), clock, breaker))
    clock.now += 31
    failing(make(Service(status=503), clock, breaker))
    clock.now += 61

    # when
    make(Service(), clock, breaker).embed_queries(['a'])
    for _ in range(3):
        failing(make(Service(status=503), clock, breaker))

    # then
    assert breaker.state()['state'] == 'open' and breaker.state()['retry_in'] == 30


def test_a_request_shaped_refusal_does_not_count_against_the_breaker():
    # given
    breaker = Breaker(Clock())
    client = make(Service(status=422), breaker=breaker)

    # when
    for _ in range(6):
        failing(client)

    # then
    assert breaker.state()['state'] == 'closed' and breaker.state()['failures'] == 0


def test_a_failed_batch_stops_the_rest_and_the_breaker_is_shared_by_clients_of_one_process():
    # given
    breaker = Breaker(Clock())
    service = Service(status=503)

    # when
    for _ in range(3):
        failing(make(service, breaker=breaker, embed_batch=2), ['a', 'b', 'c', 'd', 'e'], passages=True)
    other = failing(make(Service(), breaker=breaker))

    # then
    assert len(service.requests) == 3 and other.kind == 'down'


def test_clients_share_one_breaker_by_default():
    # then
    assert EmbedClient(lambda: settings()).breaker is embedclient.BREAKER


def test_the_status_is_up_with_host_model_and_dims_and_no_key_or_path():
    # given
    breaker = Breaker(Clock())
    s = dataclasses.replace(settings(), embed_url='http://user.invalid-not-used.example.com:8080/secret/path?token=' + KEY, embed_api_key=KEY)
    client = EmbedClient(lambda: s, breaker=breaker, transport=httpx2.MockTransport(Service()))

    # when
    found = embedclient.status(s, breaker)

    # then
    assert set(found) == {'up', 'kind', 'message', 'model', 'dims', 'host'}
    assert (found['model'], found['dims'], found['host']) == (MODEL, 384, 'user.invalid-not-used.example.com:8080') and KEY not in json.dumps(found) and 'secret' not in json.dumps(found)
    assert found['up'] is None and found['kind'] == 'unknown' and found['message'] == '' and client.breaker is breaker


def test_the_status_says_down_after_a_failure_and_up_again_after_a_success():
    # given
    breaker = Breaker(Clock())
    s = dataclasses.replace(settings(), embed_url=URL)

    # when
    failing(make(Service(status=503), breaker=breaker))
    down = embedclient.status(s, breaker)
    make(Service(), breaker=breaker).embed_queries(['a'])
    up = embedclient.status(s, breaker)

    # then
    assert (down['up'], down['kind'], down['message']) == (False, 'down', msg('embed.down')) and (up['up'], up['kind'], up['message']) == (True, None, '')


@pytest.mark.parametrize('service, kind', [(Service(status=401), 'auth'), (Service(model='other'), 'mismatch'), (Service(body=b'x'), 'bad_answer')])
def test_the_status_carries_the_last_kind_of_failure(service, kind):
    # given
    breaker = Breaker(Clock())
    failing(make(service, breaker=breaker))

    # then
    assert embedclient.status(dataclasses.replace(settings(), embed_url=URL), breaker)['kind'] == kind


def test_the_status_with_no_url_is_off_and_with_a_refused_address_is_refused(monkeypatch):
    # given
    breaker = Breaker(Clock())
    monkeypatch.setattr(embedclient, 'resolve', resolver(PUBLIC))
    failing(make(Service(), breaker=breaker))

    # then
    assert embedclient.status(dataclasses.replace(settings(), embed_url=''), breaker) == {'up': False, 'kind': 'off', 'message': msg('embed.reason.no_url'), 'model': MODEL, 'dims': 384, 'host': ''}
    assert embedclient.status(dataclasses.replace(settings(), embed_url=URL), breaker)['kind'] == 'refused'


def test_every_kind_has_a_message():
    # then
    assert all(f'embed.{kind}' in messages.MESSAGES for kind in embedclient.KINDS if kind != 'off') and 'embed.reason.no_url' in messages.MESSAGES


def test_the_response_is_capped():
    # given
    big = b'{"model": "' + MODEL.encode() + b'", "data": [], "pad": "' + b'x' * (embedclient.ANSWER_MAX + 10) + b'"}'

    # then
    assert failing(make(Service(body=big))).kind == 'bad_answer'


def test_the_status_is_unknown_until_the_first_call_then_up_after_a_success():
    # given
    breaker = Breaker(Clock())
    s = dataclasses.replace(settings(), embed_url=URL)

    # when
    before = embedclient.status(s, breaker)
    make(Service(), breaker=breaker).embed_queries(['a'])
    after = embedclient.status(s, breaker)

    # then
    assert (before['up'], before['kind']) == (None, 'unknown')
    assert (after['up'], after['kind']) == (True, None)


@pytest.mark.parametrize('code', [400, 413, 422])
def test_a_request_shaped_refusal_neither_counts_nor_resets_the_failures(code):
    # given
    breaker = Breaker(Clock())
    failing(make(Service(status=503), breaker=breaker))
    failing(make(Service(status=503), breaker=breaker))

    # when
    failing(make(Service(status=code), breaker=breaker))

    # then
    assert breaker.state()['failures'] == 2 and breaker.state()['state'] == 'closed'
    failing(make(Service(status=503), breaker=breaker))
    assert breaker.state()['state'] == 'open'


def test_a_success_that_started_before_the_breaker_opened_does_not_close_it():
    # given
    breaker = Breaker(Clock())
    ticket = breaker.admit()
    for _ in range(3):
        failing(make(Service(status=503), breaker=breaker))

    # when
    breaker.success(ticket)

    # then
    assert breaker.state()['state'] == 'open' and breaker.state()['failures'] == 3


def test_a_failure_that_started_before_the_breaker_opened_does_not_trip_the_probe():
    # given
    clock = Clock()
    breaker = Breaker(clock)
    ticket = breaker.admit()
    for _ in range(3):
        failing(make(Service(status=503), clock, breaker))
    clock.now += 31
    probe = breaker.admit()

    # when
    breaker.failure('down', ticket)
    breaker.success(probe)

    # then
    assert breaker.state()['state'] == 'closed'


def test_a_probe_that_started_after_the_breaker_opened_closes_it():
    # given
    clock = Clock()
    breaker = Breaker(clock)
    for _ in range(3):
        failing(make(Service(status=503), clock, breaker))
    clock.now += 31

    # when
    probe = breaker.admit()
    breaker.success(probe)

    # then
    assert breaker.state()['state'] == 'closed' and breaker.admit() is not None


def test_a_connect_time_refusal_cannot_close_an_open_breaker():
    # given
    clock = Clock()
    breaker = Breaker(clock)
    for _ in range(3):
        failing(make(Service(status=503), clock, breaker))

    # when
    error = failing(make(Service(body=embedclient.RefusedAddress()), clock, breaker))
    clock.now += 31
    probe = failing(make(Service(body=embedclient.RefusedAddress()), clock, breaker))

    # then
    assert error.kind == 'down' and probe.kind == 'refused'
    assert breaker.state()['state'] in ('open', 'half-open') and breaker.state()['failures'] == 3
