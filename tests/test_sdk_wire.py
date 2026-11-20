import json
import os

import anthropic
import httpx2
import pytest

from tests.conftest import settings
from tests.test_investigator_tools import sources
from skopax.aikey import AiKey
from skopax.investigator import Investigator
from skopax.investigator.gate import Gate
from skopax.investigator.runner import RunConfig, Runner
from skopax.investigator.smalltalk import SmallTalk
from skopax.investigator.tools import Effects
from skopax.messages import msg
from skopax.plugai import PluginAi
from skopax.sealer import Sealer
from skopax.selfreview import SelfReview
from skopax.store import Store
from skopax.tunables import TunableStore

REAL = anthropic.Anthropic
KEY = 'sk-wire-' + 'w' * 32
MODEL = 'claude-opus-5-5'
MESSAGE = {'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'model': MODEL, 'content': [{'type': 'text', 'text': 'ok'}], 'stop_reason': 'end_turn', 'stop_sequence': None,
           'usage': {'input_tokens': 3, 'output_tokens': 1, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}}
MODEL_INFO = {'type': 'model', 'id': MODEL, 'display_name': 'Claude', 'created_at': '2026-01-01T00:00:00Z'}


class Wire:
    def __init__(self, answer=None):
        self.answer = answer or (lambda request: httpx2.Response(200, json=MODEL_INFO if request.url.path.startswith('/v1/models') else MESSAGE))
        self.built: list[dict] = []
        self.requests: list[httpx2.Request] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.answer(request)

    def make(self, **kw):
        self.built.append({k: v for k, v in kw.items() if k != 'api_key'})
        return REAL(http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(self.handle)), **kw)


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch, tmp_path):
    for name in [n for n in os.environ if n.startswith('ANTHROPIC_')]:
        monkeypatch.delenv(name)
    monkeypatch.setenv('HOME', str(tmp_path))


def no_effects() -> Effects:
    return Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {})


CONSUMERS = [
    ('investigator', lambda store: Investigator(store, sources(), no_effects(), settings(), key=lambda: KEY), 240.0, 2),
    ('gate', lambda store: Gate(store, settings(), key=lambda: KEY), 10.0, 0),
    ('small-talk', lambda store: SmallTalk(store, settings(), key=lambda: KEY), 10.0, 0),
    ('self-review', lambda store: SelfReview(store, settings(), lambda now: {}, lambda text: {}, key=lambda: KEY), 120.0, 1),
    ('plug-in-ai', lambda store: PluginAi(store, lambda: KEY, lambda now: None), 90.0, 1),
]


@pytest.mark.parametrize('build, timeout, retries', [c[1:] for c in CONSUMERS], ids=[c[0] for c in CONSUMERS])
def test_each_client_factory_sends_the_key_as_a_bearer_token_with_its_timeout_and_retries(store: Store, monkeypatch, build, timeout: float, retries: int):
    # given
    wire = Wire()
    monkeypatch.setattr(anthropic, 'Anthropic', wire.make)
    client = build(store).box.get()

    # when
    reply = client.messages.create(model=MODEL, max_tokens=16, messages=[{'role': 'user', 'content': 'hi'}])

    # then
    sent = wire.requests[0]
    assert wire.built == [{'timeout': timeout, 'max_retries': retries}]
    assert (client.timeout, client.max_retries) == (timeout, retries)
    assert sent.headers['authorization'] == 'Bearer ' + KEY and 'x-api-key' not in sent.headers
    assert sent.extensions['timeout'] == dict.fromkeys(('connect', 'read', 'write', 'pool'), timeout)
    assert (sent.method, sent.url.path, reply.content[0].text) == ('POST', '/v1/messages', 'ok')


def test_the_investigator_beta_call_carries_the_fallback_beta_header_and_body(store: Store, monkeypatch):
    # given
    wire = Wire()
    monkeypatch.setattr(anthropic, 'Anthropic', wire.make)
    client = Investigator(store, sources(), no_effects(), settings(), key=lambda: KEY).box.get()

    # when
    reply = Runner(client, store, RunConfig(model=MODEL, effort='high', max_tool_calls=5)).request('system', [], [{'role': 'user', 'content': [{'type': 'text', 'text': 'q'}]}])

    # then
    sent = wire.requests[0]
    body = json.loads(sent.content)
    assert (sent.method, sent.url.path) == ('POST', '/v1/messages')
    assert sent.headers['anthropic-beta'] == 'server-side-fallback-2026-07-01'
    assert sent.headers['authorization'] == 'Bearer ' + KEY and 'x-api-key' not in sent.headers
    assert (body['fallbacks'], body['model'], body['thinking'], body['output_config']) == ('default', MODEL, {'type': 'adaptive'}, {'effort': 'high'})
    assert 'betas' not in body
    assert (reply.text, reply.stop, reply.usage.input) == ('ok', 'end_turn', 3)


def status(code: int):
    return lambda request: httpx2.Response(code, json={'type': 'error', 'error': {'type': 'error', 'message': 'upstream said no'}})


def raises(error):
    def answer(request):
        raise error('upstream did not answer', request=request)
    return answer


OUTCOMES = [
    ('ok', None, True, ('aikey.test_ok', {'model': MODEL})),
    ('rejected', status(401), False, ('aikey.test_rejected', {})),
    ('forbidden', status(403), False, ('aikey.test_forbidden', {'model': MODEL})),
    ('no-model', status(404), False, ('aikey.test_no_model', {'model': MODEL})),
    ('status', status(500), False, ('aikey.test_status', {'status': 500})),
    ('timeout', raises(httpx2.ReadTimeout), False, ('aikey.test_timeout', {})),
    ('unreachable', raises(httpx2.ConnectError), False, ('aikey.test_unreachable', {})),
]


@pytest.mark.parametrize('answer, ok, summary', [o[1:] for o in OUTCOMES], ids=[o[0] for o in OUTCOMES])
def test_the_key_test_maps_what_the_real_sdk_raises_to_its_fixed_texts(store: Store, answer, ok: bool, summary: tuple[str, dict]):
    # given
    wire = Wire(answer)
    aikey = AiKey(store.db, lambda: Sealer('k1'), lambda: '', lambda: None, TunableStore(store.db), factory=wire.make)

    # when
    result = aikey.test(KEY, MODEL)

    # then
    sent = wire.requests[0]
    assert (result['ok'], result['summary']) == (ok, msg(summary[0], **summary[1]))
    assert wire.built == [{'timeout': 10, 'max_retries': 0}] and len(wire.requests) == 1
    assert (sent.method, sent.url.path) == ('GET', '/v1/models/' + MODEL)
    assert sent.headers['authorization'] == 'Bearer ' + KEY and 'x-api-key' not in sent.headers
