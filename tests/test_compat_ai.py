import dataclasses
import json
import logging
import socket

import httpcore2
import httpx2
import openai
import pytest
from fastapi.testclient import TestClient
from openai.types.chat import ChatCompletion

from tests.conftest import AUTH, settings
from tests.fake_anthropic import FakeModels
from tests.test_investigator_runner import FakeBox
from tests.test_login import PASSWORD, make_client, post_login, with_password
from tests.test_spend_backfill import USAGE, record
from skopax import ai, backfill
from skopax.ai import FETCH_TYPE, SEARCH_TYPE, ToolCall, Usage, compat_ai
from skopax.app import create_app
from skopax.investigator import pricing
from skopax.investigator.runner import Result, RunConfig, Runner
from skopax.investigator.gate import Gate
from skopax.investigator.smalltalk import SmallTalk
from skopax.plugai import AiCall, PluginAi
from skopax.selfreview import SelfReview
from skopax.threadsummary import ThreadSummaries
from skopax.users import Users
from skopax.sealer import Sealer
from skopax.messages import msg
from skopax.store import Store
from tests.fake_slack import fake_slack

KEY = 'compat-key-' + 'z' * 24
PATH = '/api/admin/ai-connections'
REF = 'deepseek/deepseek-chat'
REQUEST = httpx2.Request('POST', 'https://api.example.com/v1/chat/completions')
LEAK = 'upstream said ' + KEY
PUBLIC = '93.184.215.14'
DAY = '2026-11-25'


def usage(prompt: int = 1000, completion: int = 50, cached: int = 0) -> dict:
    return {'prompt_tokens': prompt, 'completion_tokens': completion, 'total_tokens': prompt + completion, 'prompt_tokens_details': {'cached_tokens': cached}}


def completion(text: str | None = None, calls: tuple = (), finish: str = 'stop', u: dict | None = None, model: str = 'deepseek-chat', bare: bool = False) -> ChatCompletion:
    message = {'role': 'assistant', 'content': text, 'tool_calls': [{'id': cid, 'type': 'function', 'function': {'name': name, 'arguments': args}} for cid, name, args in calls] or None}
    return ChatCompletion.model_validate({'id': 'c1', 'object': 'chat.completion', 'created': 1, 'model': model, 'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
                                          'usage': None if bare else u if u is not None else usage()})


class FakeChat:
    def __init__(self, replies: list):
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.chat = self
        self.completions = self
        self.closed = False

    def close(self):
        self.closed = True

    def create(self, **kw):
        self.requests.append(json.loads(json.dumps(kw, default=str)))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class Maker:
    def __init__(self, *replies):
        self.fake = FakeChat(list(replies))
        self.built: list[dict] = []

    def __call__(self, base_url: str, key: str, allow_private, timeout: float, retries: int):
        self.built.append({'base_url': base_url, 'key': key, 'timeout': timeout, 'retries': retries})
        return self.fake


def resolver(*ips: str):
    def resolve(host, port, *args, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port or 443)) for ip in ips]
    return resolve


def status_error(cls, code: int):
    return cls(LEAK, response=httpx2.Response(code, request=REQUEST), body={'error': {'message': LEAK}})


def app(store: Store, maker: Maker | None = None, **over):
    return create_app(dataclasses.replace(settings(), anthropic_api_key='', **over), store, ai_factory=FakeModels(), compat_factory=maker or Maker())


def connection(**over) -> dict:
    return {'baseUrl': 'https://api.example.com/v1', 'key': KEY, 'models': [{'id': 'deepseek-chat', 'input': 0.27, 'output': 1.10}, {'id': 'deepseek-reasoner'}]} | over


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(compat_ai, 'resolve', resolver(PUBLIC))


def test_a_compat_turn_maps_tool_calls_and_usage():
    # given
    fake = FakeChat([completion('Looking at the pods.', (('call_1', 'list_pods', '{"ns": "shop"}'),), 'tool_calls', usage(1000, 50, 200)), completion('All fine.')])
    adapter = compat_ai.CompatAi(fake, 'deepseek')
    tools = [{'name': 'list_pods', 'description': 'Lists pods', 'input_schema': {'type': 'object', 'properties': {'ns': {'type': 'string'}}}}]

    # when
    first = adapter.turn(REF, 'high', 'SYSTEM', tools, [{'role': 'user', 'content': 'BRIEF'}], 4000)
    messages = [{'role': 'user', 'content': 'BRIEF'}, {'role': 'assistant', 'content': first.native},
                {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call_1', 'content': [{'type': 'text', 'text': 'checkout-service Running'}], 'is_error': False}]}]
    second = adapter.turn(REF, 'high', 'SYSTEM', tools, messages, 4000)

    # then
    assert (first.stop, first.model, first.text, first.calls) == ('tool_use', REF, 'Looking at the pods.', [ToolCall('call_1', 'list_pods', {'ns': 'shop'})])
    assert first.usage == Usage(input=800, cache_read=200, output=50)
    assert (second.stop, second.text, second.calls) == ('end_turn', 'All fine.', [])
    sent = fake.requests[1]
    assert sent['model'] == 'deepseek-chat' and sent['max_tokens'] == 4000
    assert sent['tools'] == [{'type': 'function', 'function': {'name': 'list_pods', 'description': 'Lists pods', 'parameters': tools[0]['input_schema']}}]
    assert sent['messages'] == [{'role': 'system', 'content': 'SYSTEM'}, {'role': 'user', 'content': 'BRIEF'},
                                {'role': 'assistant', 'content': 'Looking at the pods.', 'tool_calls': [{'id': 'call_1', 'type': 'function', 'function': {'name': 'list_pods', 'arguments': '{"ns": "shop"}'}}]},
                                {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'checkout-service Running'}]
    assert adapter.provider == 'compat:deepseek' and 'thinking' not in sent and 'extra_body' not in sent


def test_broken_tool_json_becomes_a_broken_call():
    # given
    fake = FakeChat([completion(None, (('call_9', 'list_pods', '{"ns": "sh'),), 'tool_calls'), completion(None, finish='length'), completion('no', finish='content_filter'),
                     completion('ok', bare=True)])
    adapter = compat_ai.CompatAi(fake, 'deepseek')

    # when
    broken = adapter.turn(REF, 'high', 'SYSTEM', [], [{'role': 'user', 'content': 'BRIEF'}], 100)
    replay = adapter.turn(REF, 'high', 'SYSTEM', [], [{'role': 'user', 'content': 'BRIEF'}, {'role': 'assistant', 'content': broken.native}], 100)
    filtered = adapter.once(REF, 'SYSTEM', [{'role': 'user', 'content': 'Q'}], 100, 'low')
    bare = adapter.once(REF, 'SYSTEM', [{'role': 'user', 'content': 'Q'}], 100, 'low')

    # then
    assert broken.calls == [ToolCall('call_9', 'list_pods', {}, True)] and broken.stop == 'tool_use'
    assert fake.requests[1]['messages'][-1]['tool_calls'][0]['function']['arguments'] == '{}'
    assert (replay.stop, filtered.stop, bare.stop, bare.usage) == ('max_tokens', 'refusal', 'end_turn', Usage())


def test_a_model_without_tool_support_fails_the_test_step_plainly():
    # given
    fake = FakeChat([completion('ok'), status_error(openai.BadRequestError, 400)])

    # when
    out = compat_ai.probe(fake, 'deepseek-chat')

    # then
    assert (out['ok'], out['tools'], out['summary']) == (False, False, 'Answers, but did not call the tool: not fit for investigations')
    assert fake.requests[1]['tool_choice'] == {'type': 'function', 'function': {'name': 'echo'}} and fake.requests[1]['tools'][0]['function']['name'] == 'echo'


OUTCOMES = [
    ((completion('ok'), completion(None, (('c', 'echo', '{"text": "ok"}'),), 'tool_calls')), True, True, 'Answers, uses tools'),
    ((completion('ok'), completion('I would echo ok')), False, False, 'Answers, but did not call the tool: not fit for investigations'),
    ((status_error(openai.AuthenticationError, 401),), False, False, msg('compat.test_rejected')),
    ((status_error(openai.NotFoundError, 404),), False, False, msg('compat.test_no_model', model='deepseek-chat')),
    ((openai.APITimeoutError(REQUEST),), False, False, msg('compat.test_timeout')),
    ((openai.APIConnectionError(message=LEAK, request=REQUEST),), False, False, msg('compat.test_unreachable')),
    ((status_error(openai.InternalServerError, 500),), False, False, msg('compat.test_status', status=500)),
    ((completion(''),), False, False, msg('compat.test_silent')),
    ((ValueError(LEAK),), False, False, msg('compat.test_failed')),
]


@pytest.mark.parametrize('replies, ok, tools, summary', OUTCOMES, ids=['tools', 'no-tool', 'rejected', 'no-model', 'timeout', 'unreachable', 'status', 'silent', 'other'])
def test_test_connection_reports_each_outcome_in_plain_words(store: Store, public_dns, replies, ok: bool, tools: bool, summary: str):
    # given
    maker = Maker(*replies)
    c = TestClient(app(store, maker))
    c.put(f'{PATH}/deepseek', json=connection(), headers=AUTH)

    # when
    res = c.post(f'{PATH}/deepseek/test', json={}, headers=AUTH)
    listed = c.get(PATH, headers=AUTH).json()['items'][0]

    # then
    assert res.status_code == 200
    assert (res.json()['ok'], res.json()['tools'], res.json()['summary']) == (ok, tools, summary) and isinstance(res.json()['ms'], int)
    assert KEY not in res.text and 'upstream said' not in res.text
    assert maker.built == [{'base_url': 'https://api.example.com/v1', 'key': KEY, 'timeout': 10, 'retries': 0}]
    assert {k: listed['lastTest'][k] for k in ('ok', 'tools', 'summary', 'model')} == {'ok': ok, 'tools': tools, 'summary': summary, 'model': 'deepseek-chat'}


def test_a_refused_private_address_at_connect_reads_plainly(store: Store, monkeypatch):
    # given
    monkeypatch.setattr(compat_ai, 'resolve', resolver(PUBLIC))
    maker = Maker(openai.APIConnectionError(message=LEAK, request=REQUEST))
    maker.fake.replies[0].__cause__ = compat_ai.Refused(msg('compat.url_private'))
    c = TestClient(app(store, maker))
    c.put(f'{PATH}/deepseek', json=connection(), headers=AUTH)

    # when
    res = c.post(f'{PATH}/deepseek/test', json={}, headers=AUTH)

    # then
    assert (res.json()['ok'], res.json()['summary']) == (False, msg('compat.test_refused'))


def test_a_connection_round_trips_with_its_secret_masked(store: Store, public_dns):
    # given
    c = TestClient(app(store))

    # when
    put = c.put(f'{PATH}/deepseek', json=connection(), headers=AUTH)
    again = c.put(f'{PATH}/deepseek', json=connection(key=None, images=True), headers=AUTH)
    listed = c.get(PATH, headers=AUTH).json()
    stored = c.app.state.ai_connections.get('deepseek')
    known = (pricing.known(REF), pricing.provider_of(REF), pricing.known('deepseek/deepseek-reasoner'), pricing.models_of('compat:deepseek'))
    gone = c.delete(f'{PATH}/deepseek', headers=AUTH)
    after = c.get(PATH, headers=AUTH).json()

    # then
    assert put.status_code == 200 and again.status_code == 200 and gone.status_code == 204
    view = listed['items'][0]
    assert {k: view[k] for k in ('name', 'provider', 'baseUrl', 'company', 'tools', 'images')} == {'name': 'deepseek', 'provider': 'compat:deepseek', 'baseUrl': 'https://api.example.com/v1',
                                                                                                    'company': 'api.example.com', 'tools': True, 'images': True}
    assert view['models'] == [{'id': 'deepseek-chat', 'ref': REF, 'input': 0.27, 'output': 1.10}, {'id': 'deepseek-reasoner', 'ref': 'deepseek/deepseek-reasoner', 'input': None, 'output': None}]
    assert view['key']['set'] is True and KEY not in json.dumps(listed) and stored.key == KEY
    assert known == (True, 'compat:deepseek', True, (REF, 'deepseek/deepseek-reasoner'))
    assert after['items'] == [] and not pricing.known(REF)
    assert store.db['admin_secrets'].find_one({'_id': 'compat.deepseek'}) is None


def test_a_compat_key_never_appears_in_views_or_errors(store: Store, public_dns, caplog):
    # given
    caplog.set_level(logging.DEBUG)
    maker = Maker(status_error(openai.AuthenticationError, 401), openai.APIConnectionError(message=LEAK, request=REQUEST))
    c = TestClient(app(store, maker))

    # when
    put = c.put(f'{PATH}/deepseek', json=connection(), headers=AUTH)
    bodies = [put.text, c.get(PATH, headers=AUTH).text, c.post(f'{PATH}/deepseek/test', json={}, headers=AUTH).text,
              c.post(f'{PATH}/other/test', json=connection(key=KEY + 'x'), headers=AUTH).text, c.get('/api/admin/history', headers=AUTH).text]
    refused = [c.put(f'{PATH}/deepseek', json=connection(key=KEY + ' and more'), headers=AUTH), c.put(f'{PATH}/deepseek', json=connection(key=KEY * 20), headers=AUTH),
               c.put(f'{PATH}/deepseek', json=connection(note=KEY), headers=AUTH), c.put(f'{PATH}/deepseek', json=connection(baseUrl=f'https://u:{KEY}@api.example.com/v1'), headers=AUTH),
               c.post(f'{PATH}/deepseek/test', json={'key': KEY * 20}, headers=AUTH)]
    with pytest.raises(ValueError) as failed:
        c.app.state.ai_connections.put('deepseek', connection(baseUrl=f'https://api.example.com/v1?key={KEY}'), 'admin@example.com')
    stored = store.db['admin_secrets'].find_one({'_id': 'compat.deepseek'})

    # then
    assert put.status_code == 200 and stored['value'].startswith('enc:v1:')
    assert all(KEY not in b for b in bodies) and all('upstream said' not in b for b in bodies)
    assert [r.status_code for r in refused] == [422] * 5 and all(KEY not in r.text for r in refused)
    assert KEY not in str(failed.value) and KEY not in repr(c.app.state.ai_connections.get('deepseek'))
    assert all(KEY not in str(doc) for name in store.db.list_collection_names() for doc in store.db[name].find({}))
    assert all(KEY not in r.getMessage() for r in caplog.records)


def test_images_are_skipped_with_a_line_when_the_model_reads_none():
    # given
    content = [{'type': 'text', 'text': 'What does this graph show?'}, {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'AAAA'}},
               {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'BBBB'}}]
    blind, seeing = FakeChat([completion('ok')]), FakeChat([completion('ok')])

    # when
    compat_ai.CompatAi(blind, 'deepseek').turn(REF, 'high', 'SYSTEM', [], [{'role': 'user', 'content': content}], 100)
    compat_ai.CompatAi(seeing, 'deepseek', images=True).turn(REF, 'high', 'SYSTEM', [], [{'role': 'user', 'content': content}], 100)

    # then
    assert blind.requests[0]['messages'][1]['content'] == [{'type': 'text', 'text': 'What does this graph show?'}, {'type': 'text', 'text': compat_ai.NO_IMAGES}]
    assert compat_ai.NO_IMAGES == 'images not sent: this model does not read images'
    assert [p['type'] for p in seeing.requests[0]['messages'][1]['content']] == ['text', 'image_url', 'image_url']
    assert seeing.requests[0]['messages'][1]['content'][1] == {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}}
    assert 'images' not in compat_ai.CompatAi(blind, 'deepseek').capabilities and 'images' in compat_ai.CompatAi(blind, 'deepseek', images=True).capabilities


def test_web_tools_are_dropped_for_a_compat_model():
    # given
    fake = FakeChat([completion('ok'), completion('ok')])
    tools = [{'type': SEARCH_TYPE, 'name': 'web_search', 'max_uses': 3}, {'type': FETCH_TYPE, 'name': 'web_fetch', 'max_uses': 3},
             {'name': 'list_pods', 'description': 'Lists pods', 'input_schema': {'type': 'object', 'properties': {}}}]

    # when
    compat_ai.CompatAi(fake, 'deepseek').turn(REF, 'high', 'SYSTEM', tools, [{'role': 'user', 'content': 'BRIEF'}], 100)
    compat_ai.CompatAi(fake, 'deepseek', tools=False).turn(REF, 'high', 'SYSTEM', tools, [{'role': 'user', 'content': 'BRIEF'}], 100)

    # then
    assert [t['function']['name'] for t in fake.requests[0]['tools']] == ['list_pods']
    assert 'tools' not in fake.requests[1]
    assert compat_ai.CompatAi(fake, 'deepseek').capabilities == frozenset({'tools'})
    assert compat_ai.CompatAi(fake, 'deepseek', tools=False, images=True).capabilities == frozenset({'images'})


@pytest.mark.parametrize('url, ips, allow, problem', [
    ('https://api.example.com/v1', (PUBLIC,), False, None),
    ('http://ollama.example.com:11434/v1', ('10.0.0.5',), False, 'compat.url_private'),
    ('http://ollama.example.com:11434/v1', ('10.0.0.5',), True, None),
    ('http://localhost:11434/v1', ('127.0.0.1',), False, 'compat.url_private'),
    ('http://localhost:11434/v1', ('127.0.0.1',), True, None),
    ('http://localhost:11434/v1', ('::1', '127.0.0.1'), True, None),
    ('http://[::1]:11434/v1', (), True, None),
    ('http://[::1]:11434/v1', (), False, 'compat.url_private'),
    ('http://[::ffff:192.168.1.9]:11434/v1', (), False, 'compat.url_private'),
    ('http://metadata.example.com', ('169.254.169.254',), True, 'compat.url_blocked'),
    ('http://ollama.example.com', (PUBLIC, '172.16.0.2'), False, 'compat.url_private'),
    ('ftp://api.example.com/v1', (PUBLIC,), True, 'compat.url_scheme'),
    ('https://alice:secret@api.example.com/v1', (PUBLIC,), True, 'compat.url_credentials'),
    ('https://api.example.com/v1?key=abc', (PUBLIC,), True, 'compat.url_shape'),
    ('https://api.example.com/v1#frag', (PUBLIC,), True, 'compat.url_shape'),
    ('https:///v1', (PUBLIC,), True, 'compat.url_shape'),
    ('https://nowhere.example.com/v1', (), False, 'compat.url_unresolved'),
    ('http://[fec0::1]:11434/v1', (), False, 'compat.url_private'),
    ('http://[fec0::1]:11434/v1', (), True, None),
    ('http://[64:ff9b::a00:5]/v1', (), False, 'compat.url_private'),
    ('http://[64:ff9b::5db8:d70e]/v1', (), False, None),
    ('http://[2002:c0a8:109::1]/v1', (), False, 'compat.url_private'),
    ('http://100.64.0.1/v1', (), False, 'compat.url_private'),
    ('http://0.0.0.0:11434/v1', (), True, 'compat.url_blocked'),
    ('http://[fe80::1]:11434/v1', (), True, 'compat.url_blocked'),
], ids=['public', 'private-off', 'private-on', 'loopback-off', 'loopback-on', 'loopback-both-on', 'v6-loopback-on', 'v6-loopback-off', 'mapped-literal', 'metadata', 'mixed', 'scheme', 'userinfo', 'query', 'fragment', 'no-host', 'unresolved', 'site-local-off',
                                     'site-local-on', 'nat64-private', 'nat64-public', '6to4-private', 'cgnat', 'unspecified-on', 'link-local-on'])
def test_a_private_base_url_needs_the_switch(monkeypatch, url: str, ips: tuple, allow: bool, problem: str | None):
    # given
    monkeypatch.setattr(compat_ai, 'resolve', resolver(*ips) if ips else (lambda *a, **k: (_ for _ in ()).throw(socket.gaierror('no such host'))))

    # when
    try:
        compat_ai.check_url(url, allow)
        found = None
    except compat_ai.Refused as e:
        found = str(e)

    # then
    assert found == (msg(problem) if problem else None)


def test_the_address_is_checked_again_on_each_connect_against_dns_rebinding(monkeypatch):
    # given
    answers = [resolver(PUBLIC), resolver('10.1.2.3')]
    monkeypatch.setattr(compat_ai, 'resolve', lambda *a, **k: answers.pop(0)(*a, **k))
    compat_ai.check_url('https://api.example.com/v1', False)
    backend = compat_ai.GuardedBackend(lambda: False)

    # when
    with pytest.raises(httpcore2.ConnectError) as refused:
        backend.connect_tcp('api.example.com', 443, timeout=1)

    # then
    assert isinstance(refused.value.__cause__, compat_ai.Refused) and str(refused.value.__cause__) == msg('compat.url_private')


def test_the_private_switch_is_a_default_off_core_setting(store: Store):
    # given
    c = TestClient(app(store))

    # when
    item = next(i for i in c.get('/api/admin/settings', headers=AUTH).json()['items'] if i['key'] == 'core.allow-private-ai')

    # then
    assert settings().allow_private_ai is False
    assert (item['help'], item['kind']) == ('Lets an OpenAI-compatible AI connection point at a private or loopback address, for example a local Ollama. It adds nothing by itself: add the model under Admin \u203a Investigator as a compatible connection. Link-local and cloud metadata addresses stay refused', 'bool')


def test_a_private_base_url_is_refused_on_save_until_the_switch_is_on(store: Store, monkeypatch):
    # given
    monkeypatch.setattr(compat_ai, 'resolve', resolver('192.168.1.20'))
    off, on = TestClient(app(store)), TestClient(app(store, allow_private_ai=True))

    # when
    refused = off.put(f'{PATH}/ollama', json=connection(baseUrl='http://ollama.example.com:11434/v1', key=None), headers=AUTH)
    saved = on.put(f'{PATH}/ollama', json=connection(baseUrl='http://ollama.example.com:11434/v1', key=None), headers=AUTH)

    # then
    assert (refused.status_code, refused.json()) == (422, {'detail': msg('compat.url_private')})
    assert saved.status_code == 200 and saved.json()['key']['set'] is False


def test_an_unpriced_compat_model_uses_the_highest_rate(store: Store, public_dns):
    # given
    c = TestClient(app(store))
    c.put(f'{PATH}/deepseek', json=connection(), headers=AUTH)
    adapter = compat_ai.CompatAi(FakeChat([]), 'deepseek')
    runner = Runner(adapter, store, RunConfig('deepseek/deepseek-reasoner', 'high', 5), lambda: 0.0, tag={'runId': 'r1', 'source': 'pod'})
    priced = Runner(adapter, store, RunConfig(REF, 'high', 5), lambda: 0.0, tag={'runId': 'r2', 'source': 'pod'})
    used = Usage(input=1000, output=100)
    turn = ai.Turn(stop='end_turn', model='deepseek/deepseek-reasoner', usage=used)

    # when
    result = Result()
    runner.count(result, turn, DAY)
    priced.count(Result(), dataclasses.replace(turn, model=REF), DAY)
    rows = {r['runId']: r for r in store.ledger.find({})}

    # then
    top = max(pricing.ALL_MODELS, key=lambda m: pricing.usd(m, used))
    assert rows['r1']['pricedAs'] == top and rows['r1']['provider'] == 'compat:deepseek' and result.usd == pytest.approx(pricing.usd(top, used))
    assert 'pricedAs' not in rows['r2'] and rows['r2']['usd']['total'] == pytest.approx((1000 * 0.27 + 100 * 1.10) / 1_000_000)


def test_register_hands_the_provider_hook_a_factory_and_capabilities(store: Store, public_dns):
    # given
    seen = []
    shim = type('Shim', (), {'register_provider': staticmethod(lambda prefix, factory, capabilities, models=None, company=None: seen.append((prefix, factory, capabilities, models, company)))})
    maker = Maker()
    c = TestClient(app(store, maker))
    c.put(f'{PATH}/deepseek', json=connection(images=True), headers=AUTH)

    # when
    compat_ai.register(shim)
    prefix, factory, capabilities, models, company = seen[0]
    made = factory('deepseek', 30.0, 1)
    again = factory('compat:deepseek', 30.0, 1)

    # then
    assert prefix == 'compat'
    assert isinstance(made, compat_ai.CompatAi) and made.provider == 'compat:deepseek' and made.client is maker.fake and again is made
    assert capabilities('compat:deepseek') == frozenset({'tools', 'images'}) and capabilities('deepseek') == frozenset({'tools', 'images'})
    assert factory('missing', 30.0, 1) is None and capabilities('compat:missing') == frozenset()
    assert models() == ('deepseek/deepseek-chat', 'deepseek/deepseek-reasoner')
    assert company('deepseek/deepseek-chat') == 'api.example.com' and company('missing/m') == ''
    assert maker.built == [{'base_url': 'https://api.example.com/v1', 'key': KEY, 'timeout': 30.0, 'retries': 1}]


def test_the_real_client_refuses_a_private_address_before_it_connects(monkeypatch):
    # given
    monkeypatch.setattr(compat_ai, 'resolve', resolver('10.0.0.5'))
    client = compat_ai.client_for('http://ollama.example.com:11434/v1', '', lambda: False, timeout=2, retries=0)

    # when
    with pytest.raises(openai.APIConnectionError) as failed:
        client.chat.completions.create(model='llama3', messages=[{'role': 'user', 'content': 'hi'}])

    # then
    assert compat_ai.failure(failed.value, 'llama3') == msg('compat.test_refused')


def test_a_moved_host_never_gets_the_saved_key(store: Store, monkeypatch):
    # given
    monkeypatch.setattr(compat_ai, 'resolve', resolver(PUBLIC))
    maker = Maker(completion('ok'), completion(None, (('c', 'echo', '{"text": "ok"}'),), 'tool_calls'))
    c = TestClient(app(store, maker))
    c.put(f'{PATH}/deepseek', json=connection(), headers=AUTH)
    moved = 'https://collector.example.org/v1'

    # when
    tested = c.post(f'{PATH}/deepseek/test', json={'baseUrl': moved}, headers=AUTH)
    saved = c.put(f'{PATH}/deepseek', json=connection(baseUrl=moved, key=None), headers=AUTH)
    path_only = c.put(f'{PATH}/deepseek', json=connection(baseUrl='https://api.example.com/v2', key=None), headers=AUTH)
    retyped = c.post(f'{PATH}/deepseek/test', json={'baseUrl': moved, 'key': 'other-' + KEY}, headers=AUTH)

    # then
    assert (tested.status_code, tested.json()) == (200, {'ok': False, 'tools': False, 'summary': msg('compat.key_reenter'), 'ms': 0})
    assert (saved.status_code, saved.json()) == (422, {'detail': msg('compat.key_reenter')})
    assert path_only.status_code == 200 and c.app.state.ai_connections.get('deepseek').key == KEY
    assert retyped.json()['ok'] is True
    assert maker.built == [{'base_url': moved, 'key': 'other-' + KEY, 'timeout': 10, 'retries': 0}]


def test_a_card_run_on_a_compat_model_calls_a_tool_and_answers(store: Store, public_dns):
    # given
    TestClient(app(store)).put(f'{PATH}/deepseek', json=connection(), headers=AUTH)
    fake = FakeChat([completion(None, (('call_1', 'promql', '{}'),), 'tool_calls'), completion('The checkout-service pods restarted after OPS-123.')])
    box = FakeBox({'promql': ('up 1', False)})
    runner = Runner(compat_ai.CompatAi(fake, 'deepseek'), store, RunConfig(REF, 'high', 5), lambda: 0.0)

    # when
    result = runner.run(box, 'SYSTEM', 'BRIEF', DAY, lambda: None)

    # then
    assert (result.outcome, result.turns, result.recap) == ('done', 2, 'The checkout-service pods restarted after OPS-123.')
    assert box.seen == [('promql', {})] and result.usd > 0 and fake.requests[0]['model'] == 'deepseek-chat'
    assert fake.requests[1]['messages'][-1] == {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'up 1'}


def test_an_unpriced_compat_model_is_charged_the_highest_rate_in_every_use(store: Store, public_dns):
    # given
    TestClient(app(store)).put(f'{PATH}/deepseek', json=connection(), headers=AUTH)
    unpriced = 'deepseek/deepseek-reasoner'
    answer = json.dumps({'text': 'The pool was fixed.', 'bullets': ['The pool was fixed in OPS-123.']})
    verdict = json.dumps({'addressed': True, 'onTopic': True, 'smallTalk': False, 'cardIntent': 'none', 'agrees': False, 'mindBlown': False, 'great': False, 'language': 'en', 'reason': 'card'})
    fake = FakeChat([completion(t, u=usage(1000, 100)) for t in ('{"typos": []}', answer, 'All quiet.', verdict, 'Hello there.')])
    adapter = compat_ai.CompatAi(fake, 'deepseek')
    used = Usage(input=1000, output=100)
    top = max(pricing.ALL_MODELS, key=lambda m: pricing.usd(m, used))
    plug = AiCall(PluginAi(store, lambda: 'k', lambda now: None, client=adapter, clock=lambda: 1_790_000_000.0), 'website', '-', unpriced, 'pagescan')
    slack, _, _ = fake_slack()
    threads = ThreadSummaries(store, slack, settings(), client=adapter, clock=lambda: 1_790_000_000.0)
    threads.model = unpriced
    review = SelfReview(store, settings(), lambda now: {}, lambda t: {'review': {'text': t}}, client=adapter, clock=lambda: 1_790_000_000.0)
    review.model = unpriced
    gate = Gate(store, settings(), client=adapter, key=lambda: 'k', clock=lambda: 1_790_000_000.0)
    gate.model = unpriced
    talk = SmallTalk(store, settings(), client=adapter, key=lambda: 'k', clock=lambda: 1_790_000_000.0)
    talk.model = unpriced

    # when
    out = plug.complete('SYSTEM', [{'role': 'user', 'content': [{'type': 'text', 'text': 'read this page'}]}], 1000, {'type': 'object'}, 'high')
    summary = threads._ask({'key': 'checkout-service:ERR', 'title': 'checkout-service errors'}, [{'who': 'alice', 'text': 'fixed'}], [], 1_790_000_000.0)
    reviewed = review.run(1_790_000_000.0)
    checked = gate.check('why is checkout-service slow?')
    said = talk.reply('hi', 'slack')
    rows = {r['source']: r for r in store.ledger.find({})}

    # then
    assert summary is not None and reviewed is not None and checked.on_topic and said is not None and out['usd'] == pytest.approx(pricing.usd(top, used))
    assert {s: (r['pricedAs'], r['provider'], round(r['usd']['total'], 9)) for s, r in rows.items()} == {
        s: (top, 'compat:deepseek', round(pricing.usd(top, used), 9)) for s in ('pagescan', 'threadsummary', 'selfreview', 'gate', 'smalltalk')}
    assert pricing.charge(REF, 'compat:deepseek', used) == (pytest.approx((1000 * 0.27 + 100 * 1.10) / 1_000_000), None)


def test_a_half_filled_price_is_refused_plainly(store: Store, public_dns):
    # given
    c = TestClient(app(store))

    # when
    half = [c.put(f'{PATH}/deepseek', json=connection(models=[m]), headers=AUTH) for m in ({'id': 'deepseek-chat', 'input': 0.27}, {'id': 'deepseek-chat', 'output': 1.1})]

    # then
    assert [(r.status_code, r.json()) for r in half] == [(422, {'detail': msg('compat.price_half')})] * 2


def test_compat_requests_carry_no_openai_account_headers_and_follow_no_redirects(monkeypatch):
    # given
    monkeypatch.setenv('OPENAI_ORG_ID', 'org-example')
    monkeypatch.setenv('OPENAI_PROJECT_ID', 'proj-example')
    monkeypatch.setenv('OPENAI_API_KEY', 'env-' + KEY)
    seen = []

    def answer(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx2.Response(200, json=json.loads(completion('ok').model_dump_json()))
        return httpx2.Response(302, headers={'location': 'http://10.0.0.5/v1/chat/completions'})
    client = compat_ai.client_for('https://api.example.com/v1', '', lambda: False, timeout=2, retries=0, transport=httpx2.MockTransport(answer))

    # when
    first = client.chat.completions.create(model='llama3', messages=[{'role': 'user', 'content': 'hi'}])
    with pytest.raises(openai.APIStatusError):
        client.chat.completions.create(model='llama3', messages=[{'role': 'user', 'content': 'hi'}])

    # then
    headers = {k.lower(): v for k, v in seen[0].headers.items()}
    assert first.choices[0].message.content == 'ok' and len(seen) == 2
    assert headers['authorization'] == 'Bearer none'
    assert 'openai-organization' not in headers and 'openai-project' not in headers


class Dialled(httpcore2.MockBackend):
    def __init__(self, answer: bytes | None = None, error: Exception | None = None):
        super().__init__([] if answer is None else [b'HTTP/1.1 200 OK\r\n', b'Content-Type: application/json\r\n', f'Content-Length: {len(answer)}\r\n\r\n'.encode(), answer])
        self.error = error
        self.dialled: list[tuple[str, int]] = []

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.dialled.append((host, port))
        if self.error is not None:
            raise self.error
        return super().connect_tcp(host, port, timeout, local_address, socket_options)


def by_host(host, port, *args, **kw):
    return resolver(PUBLIC if host == 'api.example.com' else '203.0.113.9')(host, port)


def test_env_proxies_are_not_used_and_the_checked_address_is_dialled(monkeypatch):
    # given
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.example.com:3128')
    monkeypatch.setenv('HTTP_PROXY', 'http://proxy.example.com:3128')
    monkeypatch.setattr(compat_ai, 'resolve', by_host)
    wire = Dialled(completion('ok').model_dump_json().encode())
    monkeypatch.setattr(httpcore2, 'SyncBackend', lambda: wire)
    client = compat_ai.client_for('http://api.example.com/v1', '', lambda: False, timeout=2, retries=0)

    # when
    said = client.chat.completions.create(model='llama3', messages=[{'role': 'user', 'content': 'hi'}])

    # then
    assert said.choices[0].message.content == 'ok' and wire.dialled == [(PUBLIC, 80)]


def test_a_request_goes_through_the_guarded_transport_and_a_connect_timeout_reads_as_a_timeout(monkeypatch):
    # given
    monkeypatch.setattr(compat_ai, 'resolve', resolver(PUBLIC))
    good, slow = compat_ai.GuardedTransport(lambda: False), compat_ai.GuardedTransport(lambda: False)
    good.backend.backend = Dialled(completion('ok').model_dump_json().encode())
    slow.backend.backend = Dialled(error=httpcore2.ConnectTimeout())
    fine = compat_ai.client_for('http://api.example.com/v1', '', lambda: False, timeout=2, retries=0, transport=good)
    late = compat_ai.client_for('http://api.example.com/v1', '', lambda: False, timeout=2, retries=0, transport=slow)

    # when
    said = compat_ai.CompatAi(fine, 'local').turn('local/llama3', 'low', 'SYSTEM', [], [{'role': 'user', 'content': 'hi'}], 50)
    with pytest.raises(openai.APITimeoutError) as failed:
        late.chat.completions.create(model='llama3', messages=[{'role': 'user', 'content': 'hi'}])

    # then
    assert said.text == 'ok' and good.backend.backend.dialled == [(PUBLIC, 80)]
    assert compat_ai.failure(failed.value, 'llama3') == msg('compat.test_timeout')


def test_a_turn_on_a_replaced_adapter_still_completes(store: Store, public_dns):
    # given
    maker = Maker(completion('first'), completion('second'))
    c = TestClient(app(store, maker))
    c.put(f'{PATH}/deepseek', json=connection(), headers=AUTH)
    old = compat_ai.factory('deepseek', 30.0, 1)
    c.put(f'{PATH}/deepseek', json=connection(key='other-' + KEY), headers=AUTH)

    # when
    new = compat_ai.factory('deepseek', 30.0, 1)
    said = old.turn(REF, 'high', 'SYSTEM', [], [{'role': 'user', 'content': 'BRIEF'}], 100)

    # then
    assert new is not old and said.text == 'first' and maker.fake.closed is False
    assert [b['key'] for b in maker.built] == [KEY, 'other-' + KEY]


def test_only_a_format_refusal_falls_back_to_json_object():
    # given
    other = openai.BadRequestError('context too long', response=httpx2.Response(400, request=REQUEST), body={'error': {'message': 'maximum context length exceeded'}})
    fake = FakeChat([other])

    # when
    with pytest.raises(openai.BadRequestError):
        compat_ai.CompatAi(fake, 'deepseek').once(REF, 'Answer in JSON.', [{'role': 'user', 'content': 'Q'}], 100, 'low', schema={'type': 'object'})

    # then
    assert len(fake.requests) == 1


def test_an_answer_with_no_choices_is_a_plain_failure():
    # given
    empty = ChatCompletion.model_validate({'id': 'c1', 'object': 'chat.completion', 'created': 1, 'model': 'm', 'choices': []})
    adapter = compat_ai.CompatAi(FakeChat([empty]), 'deepseek')

    # when
    with pytest.raises(compat_ai.EmptyAnswer) as failed:
        adapter.turn(REF, 'high', 'SYSTEM', [], [{'role': 'user', 'content': 'BRIEF'}], 100)

    # then
    assert isinstance(failed.value, adapter.api_error) and compat_ai.failure(failed.value, 'm') == msg('compat.test_silent')
    assert compat_ai.probe(FakeChat([empty]), 'm')['summary'] == msg('compat.test_silent')
    assert compat_ai.probe(FakeChat([completion('ok'), empty]), 'm')['summary'] == msg('compat.test_no_tools')


def test_a_schema_call_falls_back_to_json_object_once_when_refused():
    # given
    refused = openai.BadRequestError('bad', response=httpx2.Response(400, request=REQUEST), body={'error': {'message': "Invalid parameter: 'response_format' of type 'json_schema' is not supported"}})
    fake = FakeChat([refused, completion('{"ok": true}')])

    # when
    said = compat_ai.CompatAi(fake, 'deepseek').once(REF, 'Answer in JSON.', [{'role': 'user', 'content': 'Q'}], 100, 'low', schema={'type': 'object'})

    # then
    assert said.text == '{"ok": true}'
    assert [r['response_format']['type'] for r in fake.requests] == ['json_schema', 'json_object']


def test_the_key_is_sealed_again_after_the_secret_key_rotates(store: Store, public_dns):
    # given
    c = TestClient(app(store))
    c.put(f'{PATH}/deepseek', json=connection(), headers=AUTH)
    old = store.db['meta'].find_one({'_id': 'secret-key'})['key']
    connections = c.app.state.ai_connections
    store.db['admin_secrets'].update_one({'_id': 'compat.deepseek'}, {'$set': {'value': Sealer(old).seal(KEY)}})
    rotated = Sealer('rotated-' + 'r' * 32)
    connections.sealer = lambda: rotated
    connections.stored_key = lambda: old

    # when
    connections.refresh()
    stored = store.db['admin_secrets'].find_one({'_id': 'compat.deepseek'})['value']

    # then
    assert connections.get('deepseek').key == KEY and rotated.open(stored) == KEY and Sealer(old).open(stored) is None


def test_a_bad_stored_connection_is_left_out_with_one_warning(store: Store, public_dns, caplog):
    # given
    c = TestClient(app(store))
    store.db['admin_secrets'].insert_one({'_id': 'compat.Bad Name', 'baseUrl': 'https://api.example.com/v1', 'value': 'enc:v1:' + KEY})
    caplog.set_level(logging.WARNING)

    # when
    c.app.state.ai_connections.refresh()
    c.app.state.ai_connections.refresh()

    # then
    lines = [r.getMessage() for r in caplog.records if 'left out' in r.getMessage()]
    assert len(lines) == 1 and KEY not in lines[0] and c.app.state.ai_connections.all() == []


def test_the_last_test_is_cleared_when_models_or_the_base_url_change(store: Store, public_dns):
    # given
    maker = Maker(*[completion('ok'), completion(None, (('c', 'echo', '{}'),), 'tool_calls')] * 2)
    c = TestClient(app(store, maker))
    c.put(f'{PATH}/deepseek', json=connection(), headers=AUTH)
    c.post(f'{PATH}/deepseek/test', json={}, headers=AUTH)

    # when
    same = c.put(f'{PATH}/deepseek', json=connection(key=None), headers=AUTH).json()['lastTest']
    other_models = c.put(f'{PATH}/deepseek', json=connection(key=None, models=[{'id': 'deepseek-chat'}]), headers=AUTH).json()['lastTest']
    retested = c.post(f'{PATH}/deepseek/test', json={}, headers=AUTH).json()['ok']
    kept = c.get(PATH, headers=AUTH).json()['items'][0]['lastTest']
    other_url = c.put(f'{PATH}/deepseek', json=connection(key=None, models=[{'id': 'deepseek-chat'}], baseUrl='https://api.example.com/v2'), headers=AUTH).json()['lastTest']

    # then
    assert same is not None and same['ok'] is True and other_models is None
    assert retested is True and kept is not None and other_url is None


def test_a_refused_base_url_on_the_test_route_reads_plainly(store: Store, monkeypatch):
    # given
    monkeypatch.setattr(compat_ai, 'resolve', resolver('10.0.0.5'))
    c = TestClient(app(store))

    # when
    res = c.post(f'{PATH}/ollama/test', json={'baseUrl': 'http://ollama.example.com/v1', 'models': [{'id': 'llama3'}]}, headers=AUTH)

    # then
    assert (res.status_code, res.json()) == (200, {'ok': False, 'tools': False, 'summary': msg('compat.url_private'), 'ms': 0})


ROUTES = [('GET', PATH), ('PUT', f'{PATH}/deepseek'), ('DELETE', f'{PATH}/deepseek'), ('POST', f'{PATH}/deepseek/test')]


def test_the_connection_routes_need_a_signed_in_admin_from_this_site(store: Store, public_dns):
    # given
    with_password(store)
    Users(store.db).create('olive@example.com', 'Olive', 'operator', PASSWORD)
    anonymous, operator, admin = make_client(store), make_client(store), make_client(store)
    post_login(operator, 'olive@example.com')
    post_login(admin)
    body = connection()

    # when
    nobody = [anonymous.request(m, p, json=body).status_code for m, p in ROUTES]
    plain = [operator.request(m, p, json=body, headers={'origin': 'https://testserver'}) for m, p in ROUTES]
    cross = [admin.request(m, p, json=body, headers={'origin': 'https://evil.example.com'}) for m, p in ROUTES]

    # then
    assert nobody == [401] * 4
    assert [(r.status_code, r.json()['detail']) for r in plain] == [(403, msg('access.need_admin'))] * 4
    assert [(r.status_code, r.json()['detail']) for r in cross] == [(403, msg('login.cross_site'))] * 4
    assert store.db['admin_secrets'].find_one({'_id': 'compat.deepseek'}) is None


def test_a_backfilled_run_on_an_unpriced_compat_model_is_priced_at_the_highest_rate(store: Store, public_dns):
    # given
    TestClient(app(store)).put(f'{PATH}/deepseek', json=connection(), headers=AUTH)
    store.add_investigation(record(model='deepseek/deepseek-reasoner', models=['deepseek/deepseek-reasoner']))

    # when
    backfill.run(store)

    # then
    row = store.ledger.find_one({'runId': 'r1'})
    assert row['pricedAs'] == max(pricing.priced_models('compat:deepseek'), key=lambda m: pricing.usd(m, USAGE)) and row['usd']['total'] == pytest.approx(1.5)


def test_a_plugin_ai_call_on_a_compat_model_reaches_the_compat_adapter(store: Store, public_dns):
    # given
    maker = Maker(completion('{"typos": []}', u=usage(1000, 100)))
    TestClient(app(store, maker)).put(f'{PATH}/deepseek', json=connection(), headers=AUTH)
    plug = PluginAi(store, None, lambda now: None, clock=lambda: 1_790_000_000.0, keys={'anthropic': lambda: '', 'openai': lambda: ''})

    # when
    kept = plug.model_of(REF, 'website')
    ready = plug.ready(REF, 'website')
    out = AiCall(plug, 'website', '-', str(kept), 'pagescan', REF).complete('SYSTEM', [{'role': 'user', 'content': [{'type': 'text', 'text': 'read this page'}]}], 1000, {'type': 'object'}, 'high')
    rows = list(store.ledger.find({}))

    # then
    assert kept == REF and ready and out is not None
    assert [r['model'] for r in maker.fake.requests] == ['deepseek-chat']
    assert [(r['source'], r['use'], r['provider'], r['model'], 'pricedAs' in r) for r in rows] == [('pagescan', 'plugin', 'compat:deepseek', REF, False)]


def stubbed_http(seen: list):
    verdict = json.dumps({'addressed': True, 'onTopic': True, 'smallTalk': False, 'cardIntent': 'none', 'agrees': False, 'mindBlown': False, 'great': False, 'language': 'en', 'reason': 'card'})

    def answer(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        seen.append((request.url.host, request.headers.get('authorization'), body['model'], 'response_format' in body))
        text = verdict if 'response_format' in body else 'Checked checkout-service: one client.'
        return httpx2.Response(200, json=completion(text, u=usage(1000, 100), model=body['model']).model_dump(mode='json'))

    def make(base_url: str, key: str, allow_private, timeout: float, retries: int):
        return compat_ai.client_for(base_url, key, allow_private, timeout, retries, transport=httpx2.MockTransport(answer))
    return make


def test_a_compat_connection_serves_the_gate_and_card_runs_end_to_end(store: Store, public_dns):
    # given
    from tests.test_investigator import KEY as CARD, NOW, seed
    from tests.conftest import anomaly
    seen: list = []
    seed(store, anomaly(CARD, firstSeen='2026-10-22T05:00:00Z'))
    c = TestClient(create_app(dataclasses.replace(settings(), anthropic_api_key='', investigate_mode='live'), store, ai_factory=FakeModels(), compat_factory=stubbed_http(seen)))
    saved = c.put(f'{PATH}/deepseek', json=connection(), headers=AUTH)
    unpriced = 'deepseek/deepseek-reasoner'
    picked = [c.put('/api/admin/settings', json={'key': key, 'value': value}, headers=AUTH).status_code for key, value in (('investigate.model', unpriced), ('investigate.gate-model', REF))]
    inv, gate = c.app.state.investigator, c.app.state.gate

    # when
    enabled = (inv.enabled, gate.enabled)
    record = inv.run_once(NOW)
    verdict = gate.check('is checkout-service ok?', 'page', 'alice')
    rows = {r['use']: r for r in c.get('/api/admin/ai-uses', headers=AUTH).json()['uses']}
    ledger = {r['source']: r for r in store.ledger.find({})}

    # then
    assert saved.status_code == 200 and picked == [200, 200] and enabled == (True, True)
    assert (record['outcome'], record['model']) == ('done', unpriced) and verdict.addressed
    assert {host for host, _, _, _ in seen} == {'api.example.com'} and {auth for _, auth, _, _ in seen} == {f'Bearer {KEY}'}
    assert ('deepseek-reasoner', False) in {(m, f) for _, _, m, f in seen} and ('deepseek-chat', True) in {(m, f) for _, _, m, f in seen}
    assert (ledger['pod']['provider'], ledger['pod']['use'], ledger['pod']['model']) == ('compat:deepseek', 'card', unpriced) and ledger['pod']['pricedAs'] in pricing.ALL_MODELS
    assert (ledger['gate']['provider'], ledger['gate']['model'], 'pricedAs' in ledger['gate']) == ('compat:deepseek', REF, False)
    assert {u: (rows[u]['provider'], rows[u]['model'], rows[u]['company']) for u in ('card', 'gate')} == {'card': ('compat:deepseek', unpriced, 'api.example.com'), 'gate': ('compat:deepseek', REF, 'api.example.com')}
    assert (rows['gate']['price'], rows['card']['price']) == ({'input': 0.27, 'output': 1.10}, None)


def test_the_card_model_summary_names_each_models_provider_and_the_providers_with_a_key(store: Store, public_dns):
    # given
    c = TestClient(app(store, openai_api_key='test-openai-key'))
    c.put(f'{PATH}/deepseek', json=connection(), headers=AUTH)

    # when
    item = next(i for i in c.get('/api/admin/settings', headers=AUTH).json()['items'] if i['key'] == 'investigate.model')

    # then
    assert (item['modelProviders'][REF], item['modelProviders']['gpt-6.2-sol'], item['modelProviders']['claude-opus-5-5']) == ('compat:deepseek', 'openai', 'anthropic')
    assert item['cardDefaults'] == {'anthropic': 'claude-opus-5-5', 'openai': 'gpt-6.2-astra'}
    assert item['keyedProviders'] == ['openai', 'compat:deepseek']


def test_the_factory_keeps_one_client_per_connection_and_timeout_across_alternating_callers(store: Store, public_dns):
    # given
    maker = Maker()
    TestClient(app(store, maker)).put(f'{PATH}/deepseek', json=connection(), headers=AUTH)

    # when
    made = [compat_ai.factory('deepseek', timeout, retries) for timeout, retries in ((10.0, 0), (90.0, 1), (10.0, 0), (90.0, 1), (10.0, 0))]

    # then
    assert made[0] is made[2] is made[4] and made[1] is made[3] and made[0] is not made[1]
    assert [(b['timeout'], b['retries']) for b in maker.built] == [(10.0, 0), (90.0, 1)]


def test_a_keyless_connection_serves_a_use_and_one_with_an_unreadable_key_is_held(store: Store, public_dns):
    # given
    from tests.test_investigator import KEY as CARD, NOW, seed
    from tests.conftest import anomaly
    seed(store, anomaly(CARD, firstSeen='2026-10-22T05:00:00Z'))
    c = TestClient(app(store, Maker(), investigate_mode='live', investigate_model='ollama/llama3'))
    c.put(f'{PATH}/ollama', json={'baseUrl': 'https://llm.example.com/v1', 'key': '', 'models': [{'id': 'llama3'}]}, headers=AUTH)
    inv = c.app.state.investigator

    # when
    keyless = (inv.keyed(), inv.use_route('card'))
    found = c.app.state.ai_connections.get('ollama')
    unreadable = dataclasses.replace(found, key='', unreadable=True)
    c.app.state.ai_connections.get = lambda name: unreadable if name == 'ollama' else None
    broken = inv.use_route('card')
    row = next(r for r in c.get('/api/admin/ai-uses', headers=AUTH).json()['uses'] if r['use'] == 'card')

    # then
    assert keyless == (True, ('compat:ollama', 'ollama/llama3', None))
    assert broken == ('compat:ollama', 'ollama/llama3', None) and not inv.keyed()
    assert (row['provider'], row['model'], row['problem']) == ('compat:ollama', 'ollama/llama3', msg('ai.use_held', owner='ollama'))


def test_a_stored_compat_model_survives_a_restart(store: Store, public_dns):
    # given
    first = TestClient(app(store, Maker()))
    first.put(f'{PATH}/deepseek', json=connection(), headers=AUTH)
    picked = [first.put('/api/admin/settings', json={'key': key, 'value': REF}, headers=AUTH).status_code for key in ('investigate.model', 'investigate.gate-model')]
    compat_ai.unbind()
    ai.unregister_provider('compat')
    second = app(store, Maker())

    # when
    at_create = (second.state.investigator.model, second.state.gate.model)
    with TestClient(second):
        booted = (second.state.investigator.model, second.state.gate.model)
    stored = {k: (v['value'], v.get('by')) for k, v in second.state.live.tunables.all().items() if k in ('investigate.model', 'investigate.gate-model')}

    # then
    assert picked == [200, 200]
    assert at_create == booted == (REF, REF)
    assert {k: v for k, (v, _) in stored.items()} == {'investigate.model': REF, 'investigate.gate-model': REF} and 'upgrade' not in {by for _, by in stored.values()}


def test_a_connection_loaded_late_brings_its_stored_model_back(store: Store, public_dns):
    # given
    first = TestClient(app(store, Maker()))
    first.put(f'{PATH}/deepseek', json=connection(), headers=AUTH)
    first.put('/api/admin/settings', json={'key': 'investigate.model', 'value': REF}, headers=AUTH)
    saved = store.db['admin_secrets'].find_one({'_id': 'compat.deepseek'})
    store.db['admin_secrets'].delete_one({'_id': 'compat.deepseek'})
    compat_ai.unbind()
    ai.unregister_provider('compat')
    second = app(store, Maker())
    before = (second.state.investigator.model, second.state.investigator.keyed())

    # when
    store.db['admin_secrets'].insert_one(saved)
    second.state.ai_connections.refresh()
    after = (second.state.investigator.model, second.state.investigator.keyed())

    # then
    assert before == (REF, False)
    assert after == (REF, True)


def test_a_removed_connection_leaves_the_factory_cache(store: Store, public_dns):
    # given
    c = TestClient(app(store, Maker()))
    c.put(f'{PATH}/deepseek', json=connection(), headers=AUTH)
    compat_ai.factory('deepseek', 10.0, 0)

    # when
    c.delete(f'{PATH}/deepseek', headers=AUTH)
    c.app.state.ai_connections.refresh()

    # then
    assert compat_ai.BOUND._built == {}


def test_the_page_kit_shapes_match_the_real_ai_uses_and_connection_routes(store: Store, public_dns):
    # given
    from pathlib import Path
    shapes = json.loads((Path(__file__).resolve().parent / 'page' / 'skopax-ai-shapes.json').read_text())
    maker = Maker(completion('pong'), completion(None, (('call_1', 'echo', '{}'),), 'tool_calls'))
    c = TestClient(app(store, maker))
    c.put(f'{PATH}/deepseek', json=connection(), headers=AUTH)

    # when
    uses = c.get('/api/admin/ai-uses', headers=AUTH).json()
    listed = c.get(PATH, headers=AUTH).json()
    tested = c.post(f'{PATH}/deepseek/test', json={}, headers=AUTH).json()

    # then
    assert list(uses) == ['uses'] and all(sorted(row) == shapes['uses'] for row in uses['uses'])
    assert sorted(listed) == shapes['connections'] and sorted(listed['items'][0]) == shapes['connection']
    assert all(sorted(m) == shapes['model'] for m in listed['items'][0]['models'])
    assert sorted(tested) == shapes['test']


@pytest.mark.parametrize('gone', ['deleted', 'unreadable'])
def test_a_chat_on_a_connection_that_is_gone_or_unreadable_fails_closed(store: Store, public_dns, gone: str):
    # given
    from tests.conftest import anomaly
    from tests.fake_anthropic import FakeAnthropic, reply as claude_reply, text as claude_text
    from tests.test_chat import chat_action
    from tests.test_investigator import KEY as CARD, NOW, seed
    seed(store, anomaly(CARD, firstSeen='2026-10-22T05:00:00Z'), history=[])
    c = TestClient(create_app(dataclasses.replace(settings(), anthropic_api_key='test-anthropic-key', investigate_mode='live'), store, ai_factory=FakeModels(), compat_factory=Maker()))
    c.put(f'{PATH}/local', json={'baseUrl': 'https://llm.example.com/v1', 'key': KEY, 'models': [{'id': 'm', 'input': 1.0, 'output': 2.0}]}, headers=AUTH)
    c.put('/api/admin/settings', json={'key': 'chat.model', 'value': 'local/m'}, headers=AUTH)
    if gone == 'deleted':
        c.delete(f'{PATH}/local', headers=AUTH)
    else:
        found = c.app.state.ai_connections.get('local')
        c.app.state.ai_connections.get = lambda name: dataclasses.replace(found, key='', unreadable=True) if name == 'local' else None
    claude = FakeAnthropic([claude_reply(claude_text('One client.'), stop='end_turn')])
    inv = c.app.state.investigator
    inv.client = {'anthropic': claude}
    chat_action(store, cards=())

    # when
    record = inv.run_once(NOW)

    # then
    assert claude.requests == []
    assert (record['trigger'], record['outcome'], record['model']) == ('chat', 'failed', 'local/m')
    assert 'the chosen provider is not usable; runs of this use are held' in record['error']


def test_the_oracle_cloud_metadata_address_is_refused_everywhere():
    # given
    from skopax.mcptools.client import address, refused

    # when
    blocked = []
    for allow in (True, False):
        try:
            compat_ai.check_addresses([address('192.0.0.192')], allow)
        except compat_ai.Refused as e:
            blocked.append(e.text)

    # then
    assert blocked == [msg('compat.url_blocked')] * 2
    assert refused(address('192.0.0.192')) and not refused(address('192.0.0.193'))


@pytest.mark.parametrize('replies', [r for r, *_ in OUTCOMES[1:]], ids=['no-tool', 'rejected', 'no-model', 'timeout', 'unreachable', 'status', 'silent', 'other'])
def test_a_failed_test_on_a_private_address_reads_the_same_whatever_failed(store: Store, monkeypatch, replies):
    # given
    monkeypatch.setattr(compat_ai, 'resolve', resolver('10.0.0.5'))
    c = TestClient(app(store, Maker(*replies), allow_private_ai=True))
    c.put(f'{PATH}/local', json=connection(baseUrl='http://llm.example.com:11434/v1'), headers=AUTH)
    ticks = iter(range(0, 1000, 7))
    c.app.state.ai_connections.clock = lambda: float(next(ticks))

    # when
    got = c.post(f'{PATH}/local/test', json={}, headers=AUTH).json()

    # then
    assert (got['ok'], got['tools'], got['summary'], got['ms']) == (False, False, msg('compat.test_private_failed'), 0)


def test_a_moved_base_url_is_recorded_as_old_and_new_origin_without_the_key(store: Store, public_dns):
    # given
    c = TestClient(app(store, Maker()))
    c.put(f'{PATH}/deepseek', json=connection(), headers=AUTH)

    # when
    moved = c.put(f'{PATH}/deepseek', json=connection(baseUrl='https://llm.example.com:8443/v2', key=KEY + 'x'), headers=AUTH)
    path = c.put(f'{PATH}/deepseek', json={k: v for k, v in connection(baseUrl='https://llm.example.com:8443/v3').items() if k != 'key'}, headers=AUTH)
    rows = sorted(store.db['tunable_changes'].find({'key': 'compat.deepseek'}), key=lambda r: r['seq'])

    # then
    assert (moved.status_code, path.status_code) == (200, 200)
    assert [(r.get('oldOrigin'), r.get('newOrigin')) for r in rows] == [(None, None), ('https://api.example.com', 'https://llm.example.com:8443'), (None, None)]
    assert all(KEY not in json.dumps(r, default=str) for r in rows)
