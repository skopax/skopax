import base64
import json
import logging
import time

import anthropic
import httpx2
import pytest

from tests.fake_anthropic import FakeAnthropic, reply, text, tool, usage
from tests.fake_http import FakeHttp
from tests.fake_openai import FakeOpenAI, error as openai_error, reply as openai_reply, text as openai_text, usage as openai_usage
from skopax import tracing
from skopax.ai import THINK_OFF, Turn, Usage, wrap
from skopax.investigator.runner import BROKEN_TEXT, RunConfig, Runner
from skopax.remote import TimedOut
from skopax.store import Store
from skopax.tracing import FULL, METADATA, OTLP, PROJECTS, Target, Tracer

URL = 'https://langfuse.example.com'
SECRET = 'sk-lf-secret-value'
PUBLIC = 'pk-lf-public'
DAY = '2026-11-23'
PROJECT = 'proj-42'


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        self.now += 0.5
        return self.now


def ok_http(project: str | None = PROJECT) -> FakeHttp:
    return FakeHttp({('POST', URL + OTLP): {}, ('GET', URL + PROJECTS): {'data': [{'id': project, 'name': 'shop'}]} if project else {'data': []}})


def make(http=None, on=True, picked=True, content=METADATA, sample=100, chance=0.5, queue_max=tracing.QUEUE_MAX) -> tuple[Tracer, FakeHttp]:
    http = http if http is not None else ok_http()
    target = Target('main', URL, PUBLIC, SECRET, http)
    found = Tracer(lambda: on, lambda: target if picked else None, lambda: content, lambda: sample, lambda: 'shop-prod', clock=Clock(), chance=lambda: chance,
                   queue_max=queue_max, threaded=False, version='9.9.9')
    return found, http


@pytest.fixture
def installed():
    made = []

    def put(found: Tracer) -> Tracer:
        tracing.install(found)
        made.append(found)
        return found

    yield put
    for found in made:
        tracing.uninstall(found)


def spans(http: FakeHttp) -> list[dict]:
    return [s for _, _, _, body in http.hits(URL + OTLP) for r in body['resourceSpans'] for sc in r['scopeSpans'] for s in sc['spans']]


def attrs(span: dict) -> dict:
    return {a['key']: next(iter(a['value'].values())) for a in span['attributes']}


def one_call(found: Tracer, name: str = 'card run', fail: str | None = None, noted: list | None = None, **meta) -> tracing.Trace:
    handle = tracing.begin(name, **meta)
    if noted is not None:
        handle.trace.on_sent = noted.append
    handle.trace.generation('anthropic', 'turn', 'claude-opus-5-5', 1.0, 2.0)
    if fail:
        handle.trace.fail(fail)
    tracing.end(handle)
    return handle.trace


def test_switched_off_or_without_an_instance_nothing_is_traced_and_no_thread_or_call_happens(installed):
    # given
    off, off_http = make(on=False)
    unpicked, unpicked_http = make(picked=False)
    calls = []

    for found in (off, unpicked):
        installed(found)

        # when
        handle = tracing.begin('card run', runId='r1')
        got = tracing.generation('anthropic', 'once', 'claude-opus-5-5', 'S', [], lambda: calls.append(1) or 'answer')
        tracing.end(handle)

        # then
        assert handle.trace is None and got == 'answer' and tracing.current() is None
        assert found._thread is None and list(found.queue) == []
    assert off_http.calls == [] and unpicked_http.calls == [] and calls == [1, 1]


def test_a_trace_is_sent_as_otlp_json_with_basic_auth_the_ingestion_version_and_metadata_on_every_span(installed):
    # given
    found, http = installed(make()[0]), None
    http = found.target().transport

    # when
    noted = []
    sent = one_call(found, noted=noted, runId='run-1', cards='log|checkout-service|ERROR|boom', provider='anthropic')
    assert found.drain() is False

    # then
    method, url, headers, body = http.hits(URL + OTLP)[0]
    assert (method, url) == ('POST', URL + OTLP)
    assert headers['Authorization'] == 'Basic ' + base64.b64encode(f'{PUBLIC}:{SECRET}'.encode()).decode()
    assert headers['x-langfuse-ingestion-version'] == '4'
    resource = body['resourceSpans'][0]
    assert {a['key']: a['value']['stringValue'] for a in resource['resource']['attributes']} == {'service.name': 'skopax', 'service.version': '9.9.9'}
    root, gen = resource['scopeSpans'][0]['spans']
    assert root['traceId'] == gen['traceId'] == sent.id and len(sent.id) == 32 and len(root['spanId']) == 16
    assert 'parentSpanId' not in root and gen['parentSpanId'] == root['spanId']
    assert int(root['endTimeUnixNano']) >= int(root['startTimeUnixNano']) and gen['startTimeUnixNano'] == '1000000000' and gen['endTimeUnixNano'] == '2000000000'
    for span in (root, gen):
        a = attrs(span)
        assert (a['langfuse.trace.name'], a['langfuse.release'], a['langfuse.trace.metadata.runId'], a['langfuse.trace.metadata.workspace'],
                a['langfuse.trace.metadata.provider'], a['langfuse.trace.metadata.skopaxVersion'], a['langfuse.trace.metadata.cards']) == (
            'card run', '9.9.9', 'run-1', 'shop-prod', 'anthropic', '9.9.9', 'log|checkout-service|ERROR|boom')
    assert attrs(root)['langfuse.observation.type'] == 'span' and attrs(gen)['langfuse.observation.type'] == 'generation'
    assert (attrs(gen)['langfuse.observation.model.name'], gen['name'], gen['status']) == ('claude-opus-5-5', 'anthropic turn', {'code': 1})
    assert found.sent == 1 and noted == [{'id': sent.id, 'url': URL, 'project': PROJECT}]
    assert tracing.trace_link(noted[0]) == f'{URL}/project/{PROJECT}/traces/{sent.id}'
    assert len(http.hits(URL + PROJECTS)) == 1


def test_the_project_id_is_read_once_and_a_missing_one_gives_no_link(installed):
    # given
    found, http = make(http=ok_http(None))
    installed(found)
    noted = []

    # when
    one_call(found, noted=noted)
    found.drain()
    one_call(found, noted=noted)
    found.drain()

    # then
    assert [n['project'] for n in noted] == [None, None] and [tracing.trace_link(n) for n in noted] == [None, None]
    assert tracing.trace_link({'id': 'a', 'url': 'javascript:x', 'project': 'p'}) is None and tracing.trace_link(None) is None
    assert len(http.hits(URL + PROJECTS)) == 1, 'a failed project read is not asked again before an hour passed'


def test_sampling_is_decided_once_per_trace_and_failed_traces_are_always_sent(installed):
    # given
    found, http = make(sample=20, chance=0.5)
    installed(found)

    # when
    kept_out = one_call(found, 'card run')
    failed = one_call(found, 'card run', fail='failed')
    found.sample = lambda: 60
    kept_in = one_call(found, 'chat answer')
    found.drain()

    # then
    assert (kept_out.sampled, failed.sampled, kept_in.sampled) == (False, False, True)
    sent = {s['traceId'] for s in spans(http)}
    assert sent == {failed.id, kept_in.id}
    root = next(s for s in spans(http) if s['traceId'] == failed.id and 'parentSpanId' not in s)
    assert root['status'] == {'code': 2, 'message': 'failed'} and attrs(root)['langfuse.observation.level'] == 'ERROR'


def test_a_trace_without_a_model_call_is_not_sent_unless_it_failed(installed):
    # given
    found, http = make()
    installed(found)

    # when
    quiet = tracing.begin('card run')
    tracing.end(quiet)
    broken = tracing.begin('card run')
    broken.trace.fail('no key')
    tracing.end(broken)
    found.drain()

    # then
    assert {s['traceId'] for s in spans(http)} == {broken.trace.id}


def test_a_full_queue_drops_the_oldest_trace_and_counts_it(installed):
    # given
    found, http = make(queue_max=2)
    installed(found)

    # when
    traces = [one_call(found, f'run {n}') for n in range(3)]
    found.drain()

    # then
    assert found.dropped == 1
    assert {s['traceId'] for s in spans(http)} == {traces[1].id, traces[2].id}


def test_a_timeout_or_refusal_drops_the_batch_without_raising_and_logs_only_the_error_kind(installed, caplog):
    # given
    http = FakeHttp({('POST', URL + OTLP): TimedOut(f'{URL} gave no answer {SECRET}')})
    found, _ = make(http=http)
    installed(found)
    one_call(found)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.tracing'):
        failed = found.drain()

    # then
    assert failed is True and found.dropped == 1 and list(found.queue) == [] and found.sent == 0
    assert 'TimedOut' in caplog.text and SECRET not in caplog.text and PUBLIC not in caplog.text and URL not in caplog.text


def test_metadata_mode_sends_no_content_and_full_mode_sends_it_redacted_and_cut(installed):
    # given
    leak = f'password={SECRET} mail alice@example.com'
    long = 'x' * 50_000
    for content in (METADATA, FULL):
        found, http = make(content=content)
        installed(found)

        # when
        handle = tracing.begin('chat answer')
        handle.trace.given('Why did checkout fail? ' + leak)
        handle.trace.answered('Because ' + leak)
        tracing.generation('anthropic', 'turn', 'claude-opus-5-5', 'SYSTEM ' + leak, [{'role': 'user', 'content': [{'type': 'text', 'text': long}]}],
                           lambda: Turn(stop='end_turn', model='claude-opus-5-5', usage=Usage(10, 0, 0, 0, 5), text='the answer ' + leak))
        tracing.tool('promql', 1.0, True, {'expr': leak}, [{'type': 'text', 'text': 'result ' + leak}, {'type': 'image', 'source': {'data': 'AAAA'}}])
        tracing.end(handle)
        found.drain()

        # then
        sent = spans(http)
        values = [attrs(s) for s in sent]
        if content == METADATA:
            assert all('langfuse.observation.input' not in a and 'langfuse.observation.output' not in a for a in values)
            assert 'alice@example.com' not in str(sent) and SECRET not in str(sent)
        else:
            assert SECRET not in str(sent) and 'alice@example.com' not in str(sent)
            assert all(len(a.get('langfuse.observation.input', '')) <= tracing.CONTENT_MAX for a in values)
            tool_span = next(a for a in values if a['langfuse.observation.type'] == 'tool')
            assert tool_span['langfuse.observation.output'].startswith('result ') and 'AAAA' not in tool_span['langfuse.observation.output']
            root = next(a for s, a in zip(sent, values) if 'parentSpanId' not in s)
            assert root['langfuse.observation.input'].startswith('Why did checkout fail?') and '[redacted]' in root['langfuse.observation.input']


@pytest.mark.parametrize('provider', ['anthropic', 'openai'])
def test_both_providers_record_a_generation_with_model_usage_cache_tokens_cost_and_stop(installed, provider):
    # given
    found, http = make()
    installed(found)
    if provider == 'anthropic':
        client = FakeAnthropic([], [reply(text('hi'), stop='end_turn', u=usage(1000, 50, cr=400, cw=100))])
        model = 'claude-haiku-5-5'
    else:
        client = FakeOpenAI([openai_reply(openai_text('hi'), u=openai_usage(1500, 50, cached=400, written=100))])
        model = 'gpt-6.2-astra'

    # when
    turn = wrap(client).once(model, 'S', [{'role': 'user', 'content': 'q'}], 100, 'low', THINK_OFF)
    found.drain()

    # then
    assert turn.text == 'hi'
    root, gen = spans(http)
    a = attrs(gen)
    assert attrs(root)['langfuse.trace.name'] == 'model call' and attrs(root)['langfuse.trace.metadata.provider'] == provider
    assert (a['langfuse.observation.model.name'], gen['name']) == (model, f'{provider} once')
    assert json.loads(a['langfuse.observation.usage_details']) == {'input': 1000, 'output': 50, 'cache_read_input_tokens': 400, 'cache_creation_input_tokens': 100}
    assert json.loads(a['langfuse.observation.cost_details'])['total'] > 0 and a['langfuse.observation.metadata.stop'] == 'end_turn'


@pytest.mark.parametrize('provider', ['anthropic', 'openai'])
def test_a_failed_model_call_is_traced_as_an_error_and_the_original_error_still_raises(installed, provider):
    # given
    found, http = make()
    installed(found)
    if provider == 'anthropic':
        boom = anthropic.APIConnectionError(request=httpx2.Request('POST', 'https://api.anthropic.com/v1/messages'))
        client, model = FakeAnthropic([], [boom]), 'claude-haiku-5-5'
    else:
        import openai
        boom = openai_error(openai.APIConnectionError)
        client, model = FakeOpenAI([boom]), 'gpt-6.2-astra'

    # when
    with pytest.raises(type(boom)) as raised:
        wrap(client).once(model, 'S', [{'role': 'user', 'content': 'q'}], 100, 'low', THINK_OFF)
    found.drain()

    # then
    assert raised.value is boom
    root, gen = spans(http)
    assert gen['status']['code'] == 2 and attrs(gen)['langfuse.observation.level'] == 'ERROR' and attrs(gen)['langfuse.observation.status_message'] == 'APIConnectionError'
    assert root['status']['code'] == 2, 'a failed one-off call is always sent'


def test_a_broken_tracer_never_breaks_the_model_call(installed):
    # given
    found, http = make()
    installed(found)
    handle = tracing.begin('card run')
    handle.trace.generation = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('tracer bug'))

    # when
    turn = wrap(FakeAnthropic([], [reply(text('hi'), stop='end_turn')])).once('claude-haiku-5-5', 'S', [{'role': 'user', 'content': 'q'}], 100, 'low', THINK_OFF)
    tracing.end(handle)

    # then
    assert turn.text == 'hi'


class Box:
    def __init__(self, answers):
        self.answers = answers

    def definitions(self):
        return [{'name': 'promql', 'description': 'x', 'input_schema': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}}]

    def call(self, name, args):
        return self.answers[name]


def test_the_runner_traces_each_turn_and_each_tool_call_but_not_a_call_that_never_ran(installed, store: Store):
    # given
    found, http = make()
    installed(found)
    client = FakeAnthropic([reply(tool('promql', {'expr': 'up'}, 'a'), tool('esql', {}, 'b')), reply(tool('promql', '{"cut', 'c')),
                            reply(text('recap'), stop='end_turn')])
    runner = Runner(client, store, RunConfig('claude-opus-5-5', 'medium', 30), lambda: 0.0)
    box = Box({'promql': ('42', False), 'esql': ('index missing', True)})
    broken = {'n': 0}
    original = runner.request

    def request(system, tools, messages):
        turn = original(system, tools, messages)
        if broken['n'] == 1:
            turn.calls = [type(turn.calls[0])('c', 'promql', {}, True)]
        broken['n'] += 1
        return turn

    runner.request = request

    # when
    handle = tracing.begin('card run', runId='run-7')
    result = runner.run(box, 'SYSTEM', 'BRIEF', DAY, lambda: None)
    tracing.end(handle)
    found.drain()

    # then
    assert result.outcome == 'done'
    sent = spans(http)
    kinds = [(attrs(s)['langfuse.observation.type'], s['name'], s['status']['code']) for s in sent if 'parentSpanId' in s]
    assert kinds == [('generation', 'anthropic turn', 1), ('tool', 'promql', 1), ('tool', 'esql', 2), ('generation', 'anthropic turn', 1), ('generation', 'anthropic turn', 1)]
    assert BROKEN_TEXT not in str(sent)


def test_full_mode_keeps_the_newest_message_even_under_a_long_system_prompt(installed):
    # given
    found, http = make(content=FULL)
    installed(found)

    # when
    with tracing.opened('chat answer'):
        tracing.generation('anthropic', 'turn', 'claude-opus-5-5', 'S' * 50_000, [{'role': 'user', 'content': 'first'}, {'role': 'user', 'content': 'why did checkout fail at 13:05'}],
                           lambda: Turn(stop='end_turn', model='claude-opus-5-5', usage=Usage(10, 0, 0, 0, 5), text='ok'))
    found.drain()

    # then
    gen = next(attrs(s) for s in spans(http) if attrs(s)['langfuse.observation.type'] == 'generation')
    given = gen['langfuse.observation.input']
    assert 'why did checkout fail at 13:05' in given and 'first' not in given and len(given) <= tracing.CONTENT_MAX


def test_one_post_holds_a_bounded_number_of_spans(installed):
    # given
    found, http = make()
    installed(found)
    for _ in range(3):
        handle = tracing.begin('card run')
        for n in range(200):
            handle.trace.generation('anthropic', 'turn', 'claude-opus-5-5', 1.0, 2.0)
        tracing.end(handle)

    # when
    found.drain()

    # then
    sizes = [sum(len(sc['spans']) for r in body['resourceSpans'] for sc in r['scopeSpans']) for _, _, _, body in http.hits(URL + OTLP)]
    assert sizes == [402, 201] and all(n <= tracing.SPANS_PER_POST for n in sizes)


def test_a_tracer_that_cannot_open_a_trace_still_runs_the_one_off_call(installed):
    # given
    found, http = make()
    found.sample = lambda: (_ for _ in ()).throw(ValueError('bad sample'))
    installed(found)

    # when
    got = tracing.generation('anthropic', 'once', 'claude-haiku-5-5', 'S', [], lambda: 'answer')
    found.drain()

    # then
    assert got == 'answer' and http.calls == []


def test_a_dropped_batch_never_notes_its_traces(installed):
    # given
    found, _ = make(http=FakeHttp({('POST', URL + OTLP): TimedOut('slow')}))
    installed(found)
    noted = []
    one_call(found, noted=noted)

    # when
    found.drain()

    # then
    assert noted == [] and found.dropped == 1 and found.failures == 1


def test_switching_tracing_off_mid_run_sends_nothing_and_the_thread_exits(installed):
    # given
    http = ok_http()
    target = Target('main', URL, PUBLIC, SECRET, http)
    switch = {'on': True}
    found = installed(Tracer(lambda: switch['on'], lambda: target, threaded=True))
    one_call(found)
    deadline = time.monotonic() + 5
    while found.sent < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    started = found.running()
    posts = len(http.hits(URL + OTLP))

    # when
    handle = tracing.begin('card run')
    handle.trace.generation('anthropic', 'turn', 'claude-opus-5-5', 1.0, 2.0)
    switch['on'] = False
    tracing.end(handle)
    deadline = time.monotonic() + 5
    while found.running() and time.monotonic() < deadline:
        time.sleep(0.01)

    # then
    assert started is True and posts == 1
    assert found.running() is False and len(http.hits(URL + OTLP)) == 1 and found.dropped == 1 and list(found.queue) == []
    switch['on'] = True
    one_call(found)
    deadline = time.monotonic() + 5
    while found.sent < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    found.shutdown()
    assert found.sent == 2, 'switched on again, the thread starts again'


def test_a_queued_batch_is_dropped_when_tracing_is_switched_off_before_it_goes_out(installed):
    # given
    switch = {'on': True}
    found, http = make()
    found.on = lambda: switch['on']
    installed(found)
    one_call(found)

    # when
    switch['on'] = False
    found.drain()

    # then
    assert http.calls == [] and found.dropped == 1


def test_a_failing_fail_mark_never_hides_the_original_error(installed):
    # given
    found, _ = make()
    installed(found)

    # when
    with pytest.raises(KeyError) as raised:
        with tracing.opened('check') as trace:
            trace.fail = lambda why: (_ for _ in ()).throw(RuntimeError('tracer bug'))
            raise KeyError('original')

    # then
    assert raised.value.args == ('original',) and tracing.current() is None
