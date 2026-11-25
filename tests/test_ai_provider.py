import dataclasses
import json
import logging
from types import SimpleNamespace as NS

import httpx2
import openai
import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, settings
from tests.fake_anthropic import FakeAnthropic, FakeModels, reply as claude_reply, text as claude_text
from tests.fake_openai import FakeOpenAI, call, reply, text, usage
from tests.test_investigator import KEY, NOW, seed
from tests.test_investigator_tools import sources
from skopax import clock
from skopax.app import create_app
from skopax.investigator import Investigator
from skopax.investigator.gate import Gate
from skopax.investigator.smalltalk import SmallTalk
from skopax.investigator.tools import Effects
from skopax.messages import msg
from skopax.plugai import PluginAi
from skopax.selfreview import SelfReview
from skopax.store import Store

OPENAI_KEY = 'test-openai-key-' + 'q' * 8
PATH = '/api/admin/openai-key'
TEST = PATH + '/test'
LEAK = 'upstream said ' + OPENAI_KEY
REQUEST = httpx2.Request('GET', 'https://api.example.com')


def on_openai(**over):
    keys = {'openai_api_key': 'test-key', 'anthropic_api_key': ''}
    return dataclasses.replace(settings(), ai_provider='openai', **keys, **over)


def effects() -> Effects:
    return Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {})


def gate_answer() -> str:
    return json.dumps({'addressed': True, 'onTopic': True, 'smallTalk': False, 'cardIntent': 'none', 'agrees': False, 'mindBlown': False, 'great': False, 'language': 'en', 'reason': 'card'})


def app(store: Store, factory: FakeModels | None = None, **over):
    return create_app(dataclasses.replace(settings(), anthropic_api_key='', **over), store, ai_factory=factory or FakeModels())


def setting(c: TestClient, key: str) -> dict:
    return next(i for i in c.get('/api/admin/settings', headers=AUTH).json()['items'] if i['key'] == key)


def test_a_card_run_on_openai_uses_the_openai_model_answers_and_spends(store: Store):
    # given
    seed(store, anomaly(KEY, firstSeen='2026-10-22T05:00:00Z'))
    fake = FakeOpenAI([reply(call('note', {'key': KEY, 'text': 'one client'})), reply(text('Checked checkout-service.'), u=usage(2000, 100, cached=1000))])
    inv = Investigator(store, sources(), effects(), on_openai(investigate_mode='live'), client=fake, clock=lambda: NOW)

    seen = []
    import skopax.investigator as investigator_module
    real = investigator_module.web_from
    monkeypatched = lambda domains, searches, fetches, own=(): seen.append((searches, fetches)) or real(domains, searches, fetches, own)
    investigator_module.web_from = monkeypatched

    # when
    try:
        record = inv.run_once(NOW)
    finally:
        investigator_module.web_from = real

    # then
    assert seen == [(3, 0)], 'the toolbox gets web search but no web page reads'
    assert [t['type'] for t in fake.requests[0]['tools'] if t['type'] != 'function'] == ['web_search']
    assert (record['outcome'], record['model']) == ('done', 'gpt-6.2-astra')
    assert [r['model'] for r in fake.requests] == ['gpt-6.2-astra', 'gpt-6.2-astra']
    assert store.spent(clock.day(NOW)) > 0
    assert {r['provider'] for r in store.ledger.find({'runId': record['id']})} == {'openai'}


def test_the_daily_limit_stops_openai_runs_before_any_call(store: Store):
    # given
    seed(store, anomaly(KEY, firstSeen='2026-10-22T05:00:00Z'))
    fake = FakeOpenAI([reply(text('never'))])
    inv = Investigator(store, sources(), effects(), on_openai(investigate_mode='live', investigate_daily_usd=1.0), client=fake, clock=lambda: NOW)
    store.add_spend(clock.day(NOW), 1.0)

    # when
    record = inv.run_once(NOW)

    # then
    assert record is None and fake.requests == []


def test_a_stored_claude_model_falls_back_to_the_openai_default_and_web_page_reads_are_off(store: Store):
    # given
    inv = Investigator(store, sources(), effects(), on_openai(investigate_model='claude-opus-5-5', investigate_slack_model='claude-sonnet-5-5', chat_model='gpt-6.1-terra'))

    # then
    assert (inv.model, inv.slack_model, inv.chat_model) == ('gpt-6.2-astra', 'gpt-6.2-sol', 'gpt-6.1-terra')
    assert inv.key() == 'test-key' and inv.label == 'OpenAI'


def test_switching_the_provider_mid_run_runs_the_default_model_of_the_client_in_hand(store: Store):
    # given
    seed(store, anomaly(KEY, firstSeen='2026-10-22T05:00:00Z'))
    s = dataclasses.replace(settings(), anthropic_api_key='test-anthropic-key', investigate_mode='live')
    fake = FakeOpenAI([reply(text('x'))])
    inv = Investigator(store, sources(), effects(), s, client=fake, clock=lambda: NOW)

    # when
    record = inv.run_once(NOW)

    # then
    assert record['outcome'] == 'done' and record['error'] is None and fake.requests[0]['model'] == 'gpt-6.2-astra'


def test_the_gate_and_small_talk_run_on_openai(store: Store):
    # given
    fake = FakeOpenAI([reply(text(gate_answer()), model='gpt-6-luna', u=usage(1500, 40)), reply(text('Hello there.'), model='gpt-6-luna', u=usage(300, 10))])
    s = on_openai(investigate_gate_model='claude-haiku-5-5')

    # when
    verdict = Gate(store, s, client=fake, clock=lambda: NOW).check('is checkout-service ok?', 'page', 'oscar.green')
    said = SmallTalk(store, s, client=fake, clock=lambda: NOW).reply('hello', 'page', 'oscar.green@example.com')

    # then
    gate_sent, talk_sent = fake.requests
    assert verdict.addressed and verdict.usd > 0 and said.text == 'Hello there.'
    assert (gate_sent['model'], gate_sent['reasoning'], gate_sent['text']['format']['strict']) == ('gpt-6-luna', {'effort': 'none'}, True)
    assert (talk_sent['model'], talk_sent['reasoning']) == ('gpt-6-luna', {'effort': 'none'})
    assert [(r['source'], r['provider'], r['model']) for r in store.ledger.find({}).sort('source')] == [('gate', 'openai', 'gpt-6-luna'), ('smalltalk', 'openai', 'gpt-6-luna')]


def test_the_self_review_runs_on_openai(store: Store):
    # given
    fake = FakeOpenAI([reply(text('All quiet.'), model='gpt-6.2-sol')])
    saved = []
    review = SelfReview(store, on_openai(), lambda now: {}, lambda t: saved.append(t) or {'review': {'text': t}}, client=fake, clock=lambda: NOW)

    # when
    done = review.run(NOW)

    # then
    assert saved == ['All quiet.'] and done['usd'] > 0
    assert (fake.requests[0]['model'], fake.requests[0]['reasoning']) == ('gpt-6.2-sol', {'effort': 'medium'})
    assert store.ledger.find_one({'source': 'selfreview'})['provider'] == 'openai'


def test_a_plugin_model_call_on_openai_uses_the_fallback_model_and_the_schema(store: Store):
    # given
    fake = FakeOpenAI([reply(text('{"typos": []}'), model='gpt-6.2-astra')])
    ai = PluginAi(store, lambda: 'test-key', lambda now: None, client=fake, clock=lambda: NOW, provider=lambda: 'openai')
    plugin, check = NS(id='website'), NS(id='pagescan')

    # when
    made = ai.for_check(plugin, check, None, 'claude-opus-5-5')
    out = made.complete('SYSTEM', [{'role': 'user', 'content': [{'type': 'text', 'text': 'read this page'}]}], 1000, {'type': 'object'}, 'high')

    # then
    assert made.model == 'gpt-6.2-astra' and out['text'] == '{"typos": []}' and out['usd'] > 0
    assert (fake.requests[0]['reasoning'], fake.requests[0]['input']) == ({'effort': 'high'}, [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'read this page'}]}])
    assert store.ledger.find_one({'source': 'pagescan'})['provider'] == 'openai'


def test_the_openai_key_is_sealed_write_only_and_falls_back_to_the_environment(store: Store, caplog):
    # given
    caplog.set_level(logging.DEBUG)
    c = TestClient(app(store, openai_api_key='test-env-key-' + 'e' * 8))

    # when
    before = c.get(PATH, headers=AUTH).json()
    put = c.put(PATH, json={'key': OPENAI_KEY}, headers=AUTH)
    after = c.get(PATH, headers=AUTH).json()
    refused = c.put(PATH, json={'key': OPENAI_KEY + ' and more'}, headers=AUTH)
    stored = store.db['admin_secrets'].find_one({'_id': 'openai.api-key'})
    deleted = c.delete(PATH, headers=AUTH)

    # then
    assert (before['using'], before['note']) == ('config', msg('openaikey.from_config'))
    assert put.status_code == 200 and after['using'] == 'admin' and stored['value'].startswith('enc:v1:')
    assert (refused.status_code, refused.json()) == (422, {'detail': msg('openaikey.invalid')})
    assert deleted.status_code == 204 and c.get(PATH, headers=AUTH).json()['using'] == 'config'
    assert all(OPENAI_KEY not in t for t in (put.text, json.dumps(after), refused.text))
    assert all(OPENAI_KEY not in r.getMessage() for r in caplog.records)
    assert [a['text'] for a in store.list_activity(None, 10) if a['kind'] == 'admin'][:2] == [msg('openaikey.cleared'), msg('openaikey.set')]


def status_error(cls, code: int):
    return cls(LEAK, response=httpx2.Response(code, request=REQUEST), body={'error': {'message': LEAK}})


OUTCOMES = [
    (None, True, ('openaikey.test_ok', {'model': 'gpt-6.2-astra'})),
    (status_error(openai.AuthenticationError, 401), False, ('openaikey.test_rejected', {})),
    (status_error(openai.PermissionDeniedError, 403), False, ('openaikey.test_forbidden', {'model': 'gpt-6.2-astra'})),
    (status_error(openai.NotFoundError, 404), False, ('openaikey.test_no_model', {'model': 'gpt-6.2-astra'})),
    (status_error(openai.RateLimitError, 429), False, ('openaikey.test_status', {'status': 429})),
    (openai.APITimeoutError(request=REQUEST), False, ('openaikey.test_timeout', {})),
    (openai.APIConnectionError(message=LEAK, request=REQUEST), False, ('openaikey.test_unreachable', {})),
    (ValueError(LEAK), False, ('openaikey.test_failed', {})),
]


@pytest.mark.parametrize('outcome, ok, summary', OUTCOMES, ids=['ok', 'rejected', 'forbidden', 'no-model', 'rate-limit', 'timeout', 'unreachable', 'other'])
def test_the_openai_connection_test_reports_fixed_texts_for_the_openai_card_model(store: Store, outcome, ok: bool, summary):
    # given
    factory = FakeModels(outcome)
    c = TestClient(create_app(dataclasses.replace(settings(), anthropic_api_key=''), store, openai_factory=factory))

    # when
    res = c.post(TEST, json={'key': OPENAI_KEY}, headers=AUTH)

    # then
    assert res.status_code == 200 and (res.json()['ok'], res.json()['summary']) == (ok, msg(summary[0], **summary[1]))
    assert OPENAI_KEY not in res.text and 'upstream said' not in res.text
    assert factory.built == [{'api_key': OPENAI_KEY, 'timeout': 10, 'max_retries': 0}] and factory.asked == ['gpt-6.2-astra']


def test_switching_the_provider_in_admin_moves_models_key_and_choices(store: Store):
    # given
    c = TestClient(create_app(dataclasses.replace(settings(), anthropic_api_key='test-anthropic-key', openai_api_key='test-key', investigate_mode='shadow'), store))
    inv = c.app.state.investigator

    # when
    before = (inv.provider, inv.model, inv.key(), setting(c, 'investigate.model')['choices'])
    put = c.put('/api/admin/settings', json={'key': 'investigate.provider', 'value': 'openai'}, headers=AUTH)
    c.app.state.live.refresh()
    after = (inv.provider, inv.model, inv.key(), setting(c, 'investigate.model')['choices'])

    # then
    assert put.status_code == 200
    assert before == ('anthropic', 'claude-opus-5-5', 'test-anthropic-key', ['claude-opus-5-5', 'claude-opus-5', 'claude-opus-4-8', 'claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-fable-5-2', 'claude-mythos-5-2'])
    assert after == ('openai', 'gpt-6.2-astra', 'test-key', ['gpt-6.2-astra', 'gpt-6.2-sol', 'gpt-6.1-terra', 'gpt-6-luna'])


def test_admin_marks_web_page_reads_unavailable_and_a_set_model_of_the_other_provider(store: Store):
    # given
    c = TestClient(create_app(on_openai(investigate_model='claude-opus-5'), store))
    anthropic_app = TestClient(create_app(dataclasses.replace(settings(), anthropic_api_key='test-anthropic-key'), store))

    # when
    fetches, model, slack = setting(c, 'investigate.web-fetches'), setting(c, 'investigate.model'), setting(c, 'investigate.slack-model')
    notices = c.get('/api/admin/notices', headers=AUTH).json()['items']

    # then
    assert fetches['off'] == msg('ai.off_openai') and 'off' not in setting(anthropic_app, 'investigate.web-fetches')
    assert model['problem'] == msg('ai.model_other', model='claude-opus-5', owner='Anthropic', provider='OpenAI', used='gpt-6.2-astra') and model['value'] == 'claude-opus-5'
    assert 'problem' not in slack and slack['value'] == 'gpt-6.2-sol' and slack['source'] == 'default'
    assert model['choices'] == ['gpt-6.2-astra', 'gpt-6.2-sol', 'gpt-6.1-terra', 'gpt-6-luna', 'claude-opus-5'] and model['choiceLabels']['claude-opus-5'] == 'claude-opus-5 (Anthropic, not used)'
    assert [n['text'] for n in notices if n['id'].startswith('ai-model:')] == [msg('ai.model_other', model='claude-opus-5', owner='Anthropic', provider='OpenAI', used='gpt-6.2-astra')]


def test_features_need_the_key_of_the_active_provider(store: Store):
    # given
    c = TestClient(create_app(dataclasses.replace(settings(), anthropic_api_key='test-anthropic-key', ai_provider='openai', investigate_mode='shadow'), store))

    # when
    needs = {i['id']: i['needs'] for i in c.get('/api/admin/features', headers=AUTH).json()['items']}

    # then
    assert needs['investigator'] == [msg('features.needs.openai_key')]


def test_the_provider_comes_from_the_environment_and_a_wrong_value_falls_back(monkeypatch):
    # given
    from skopax.config import load_settings
    monkeypatch.setenv('SKOPAX_AI_PROVIDER', 'openai')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')

    # when
    chosen = load_settings()
    monkeypatch.setenv('SKOPAX_AI_PROVIDER', 'other')
    wrong = load_settings()

    # then
    assert (chosen.ai_provider, chosen.openai_api_key) == ('openai', 'test-key') and wrong.ai_provider == 'anthropic'
    assert 'test-key' not in repr(chosen)


def test_an_anthropic_run_is_unchanged_by_the_provider_layer(store: Store):
    # given
    seed(store, anomaly(KEY, firstSeen='2026-10-22T05:00:00Z'))
    fake = FakeAnthropic([claude_reply(claude_text('Fine.'), stop='end_turn')])
    inv = Investigator(store, sources(), effects(), dataclasses.replace(settings(), anthropic_api_key='test-anthropic-key', investigate_mode='live'), client=fake, clock=lambda: NOW)

    # when
    record = inv.run_once(NOW)

    # then
    sent = fake.requests[0]
    assert record['outcome'] == 'done' and sent['model'] == 'claude-opus-5-5'
    assert (sent['thinking'], sent['output_config'], sent['betas'], sent['extra_body']) == ({'type': 'adaptive'}, {'effort': 'medium'}, ['server-side-fallback-2026-07-01'], {'fallbacks': 'default'})
    assert sent['system'][0]['cache_control'] == {'type': 'ephemeral'} and sent['messages'][0]['content'][-1]['cache_control'] == {'type': 'ephemeral'}
    assert {t['type'] for t in sent['tools'] if 'type' in t} == {'web_fetch_20250910', 'web_search_20250305'}
    assert store.ledger.find_one({'runId': record['id']})['provider'] == 'anthropic'


def test_the_website_plugin_offers_every_priced_model_and_the_openai_sdk_logs_quietly():
    # given
    from skopax import logs
    from skopax.investigator.pricing import ALL_MODELS
    from skopax.plugins import website

    # then
    assert website.MODELS == ALL_MODELS
    assert 'openai' in logs.QUIET_LIBRARIES


WIRE_OUTCOMES = [
    ('ok', None, True, ('openaikey.test_ok', {'model': 'gpt-6.2-astra'})),
    ('rejected', 401, False, ('openaikey.test_rejected', {})),
    ('forbidden', 403, False, ('openaikey.test_forbidden', {'model': 'gpt-6.2-astra'})),
    ('no-model', 404, False, ('openaikey.test_no_model', {'model': 'gpt-6.2-astra'})),
    ('rate-limit', 429, False, ('openaikey.test_status', {'status': 429})),
    ('status', 500, False, ('openaikey.test_status', {'status': 500})),
]


@pytest.mark.parametrize('code, ok, summary', [o[1:] for o in WIRE_OUTCOMES], ids=[o[0] for o in WIRE_OUTCOMES])
def test_the_openai_key_test_maps_what_the_real_sdk_raises_to_its_fixed_texts(store: Store, code, ok: bool, summary):
    # given
    from skopax.aikey import AiKey
    from skopax.sealer import Sealer
    from skopax.tunables import TunableStore
    seen = []

    def handle(request):
        seen.append(request)
        if code is None:
            return httpx2.Response(200, json={'id': 'gpt-6.2-astra', 'object': 'model', 'created': 1, 'owned_by': 'system'})
        return httpx2.Response(code, json={'error': {'message': LEAK, 'type': 'x', 'code': None}})

    def make(**kw):
        return openai.OpenAI(http_client=openai.DefaultHttpxClient(transport=httpx2.MockTransport(handle)), **kw)

    key = AiKey(store.db, lambda: Sealer('k1'), lambda: '', lambda: None, TunableStore(store.db), factory=make, provider='openai')

    # when
    result = key.test(OPENAI_KEY, 'gpt-6.2-astra')

    # then
    assert (result['ok'], result['summary']) == (ok, msg(summary[0], **summary[1]))
    assert (seen[0].method, seen[0].url.path, seen[0].url.host) == ('GET', '/v1/models/gpt-6.2-astra', 'api.openai.com')
    assert seen[0].headers['authorization'] == 'Bearer ' + OPENAI_KEY and OPENAI_KEY not in result['summary'] and len(seen) == 1


@pytest.mark.parametrize('module', ['skopax.ai', 'skopax.ai.anthropic_ai', 'skopax.ai.openai_ai', 'skopax.investigator.pricing', 'skopax.config', 'skopax.selfreview', 'skopax.plugai',
                                    'skopax.aikey', 'skopax.investigator.gate'])
def test_each_module_that_makes_model_calls_imports_on_its_own(module: str):
    # given
    import subprocess
    import sys

    # when
    done = subprocess.run([sys.executable, '-B', '-c', f'import sys, {module}; print(sorted(m for m in sys.modules if m.startswith("skopax.investigator")))'], capture_output=True, text=True, timeout=60)

    # then
    assert done.returncode == 0, done.stderr[-500:]
    if module.startswith('skopax.ai'):
        assert done.stdout.strip() == '[]', 'the provider layer loads without the investigator package'


def test_topic_gate_effort_is_marked_unavailable_with_openai(store: Store):
    # given
    c = TestClient(create_app(on_openai(), store))
    claude = TestClient(create_app(dataclasses.replace(settings(), anthropic_api_key='test-anthropic-key'), store))

    # then
    assert setting(c, 'investigate.gate-effort')['off'] == msg('ai.off_openai_gate_effort')
    assert 'off' not in setting(claude, 'investigate.gate-effort') and 'off' not in setting(c, 'investigate.effort')


def test_the_costs_page_counts_openai_rows_and_their_cache_share(store: Store):
    # given
    from skopax.costs import summary
    from skopax.investigator import pricing
    used = pricing.Usage(input=1_000, cache_read=3_000, output=100)
    day = clock.day(NOW)
    store.add_ledger({'source': 'pod', 'day': day, 'provider': 'openai', 'model': 'gpt-6.2-astra', 'effort': 'medium', 'runId': 'r1', 'actionId': None, 'keys': [], 'by': 'pod',
                      **pricing.split('gpt-6.2-astra', used)})

    # when
    out = summary(store.ledger_since(day), {}, day, 1, 0.0, 25.0)

    # then
    assert out['total']['cacheHit'] == 0.75
    assert out['total']['usd'] == pytest.approx(round((1_000 * 8 + 3_000 * 0.4 + 100 * 40) / 1e6, 4))


def test_gate_and_small_talk_rows_carry_no_effort_under_openai_and_keep_it_under_anthropic(store: Store):
    # given
    fake = FakeOpenAI([reply(text(gate_answer()), model='gpt-6-luna'), reply(text('Hi.'), model='gpt-6-luna')])
    claude = FakeAnthropic([], gate_replies=[claude_reply(claude_text(gate_answer()), stop='end_turn')])

    # when
    Gate(store, on_openai(), client=fake, clock=lambda: NOW).check('is checkout-service ok?', 'page', 'oscar.green')
    SmallTalk(store, on_openai(), client=fake, clock=lambda: NOW).reply('hello', 'page', 'oscar.green@example.com')
    Gate(store, dataclasses.replace(settings(), anthropic_api_key='test-anthropic-key'), client=claude, clock=lambda: NOW).check('is checkout-service ok?', 'slack', 'oscar.green')

    # then
    assert sorted((r['source'], r['provider'], r['effort']) for r in store.ledger.find({})) == [('gate', 'anthropic', 'low'), ('gate', 'openai', None), ('smalltalk', 'openai', None)]


def test_a_plugin_call_made_before_a_provider_switch_uses_the_model_of_the_client_it_gets(store: Store):
    # given
    fake = FakeOpenAI([reply(text('ok'), model='gpt-6.2-astra')])
    provider = ['anthropic']
    ai = PluginAi(store, lambda: 'test-key', lambda now: None, client=fake, clock=lambda: NOW, provider=lambda: provider[0])
    made = ai.for_check(NS(id='website'), NS(id='pagescan'), None, 'claude-opus-5-5')

    # when
    provider[0] = 'openai'
    out = made.complete('SYSTEM', [{'role': 'user', 'content': 'read'}], 500)

    # then
    assert out['text'] == 'ok' and fake.requests[0]['model'] == 'gpt-6.2-astra'
    assert store.ledger.find_one({'source': 'pagescan'})['model'] == 'gpt-6.2-astra'


def test_saving_a_model_of_the_provider_not_in_use_is_refused_and_reset_names_the_model_that_runs(store: Store):
    # given
    c = TestClient(create_app(on_openai(), store))

    # when
    refused = c.put('/api/admin/settings', json={'key': 'investigate.model', 'value': 'claude-opus-5-5'}, headers=AUTH)
    taken = c.put('/api/admin/settings', json={'key': 'investigate.model', 'value': 'gpt-6.1-terra'}, headers=AUTH)
    model = setting(c, 'investigate.model')

    # then
    assert (refused.status_code, refused.json()['detail']) == (422, msg('ai.model_refused', model='claude-opus-5-5', owner='Anthropic', provider='OpenAI'))
    assert taken.status_code == 200 and model['value'] == 'gpt-6.1-terra'
    assert (model['base'], model['default']) == ('gpt-6.2-astra', 'gpt-6.2-astra')


def test_the_chat_list_names_the_chat_model_that_will_run(store: Store):
    # given
    c = TestClient(create_app(on_openai(chat_model='claude-sonnet-5-5'), store))

    # when
    got = c.get('/api/chats', headers=AUTH).json()

    # then
    assert got['model'] == 'gpt-6.2-sol'


def test_the_card_model_item_tells_the_page_each_models_provider_and_each_providers_card_default(store: Store):
    # given
    c = TestClient(create_app(on_openai(investigate_model='claude-opus-5-5'), store))

    # when
    item = setting(c, 'investigate.model')

    # then
    assert item['modelProviders']['claude-fable-5-2'] == 'anthropic'
    assert item['modelProviders']['gpt-6.2-astra'] == 'openai'
    assert item['cardDefaults'] == {'anthropic': 'claude-opus-5-5', 'openai': 'gpt-6.2-astra'}
    assert 'modelProviders' not in setting(c, 'investigate.model-critical')
