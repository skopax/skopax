import dataclasses
import json

from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, settings
from tests.fake_anthropic import FakeAnthropic, reply as claude_reply, text as claude_text
from tests.fake_openai import FakeOpenAI, reply, text, usage
from tests.test_auth import ROLE, TokenEndpoint, access_token, app_client, log_in
from tests.test_chat import chat_action
from tests.test_investigator import KEY, NOW, note_and_recap, seed
from tests.test_investigator_slack import slack_action
from tests.test_investigator_tools import sources
from skopax import ai, aiuses, clock
from skopax.ai import Turn, Usage
from skopax.app import create_app
from skopax.investigator import Investigator, pricing
from skopax.investigator.gate import Gate
from skopax.investigator.tools import Effects
from skopax.messages import msg
from skopax.plugai import PluginAi
from skopax.selfreview import SelfReview
from skopax.store import Store

BOTH = {'anthropic_api_key': 'test-anthropic-key', 'openai_api_key': 'test-openai-key'}
USES = ['card', 'critical', 'high', 'slack', 'chat', 'gate', 'review', 'plugin', 'guard']


def effects() -> Effects:
    return Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}, lambda action, outcome, recap: {'ts': '1', 'text': recap})


def gate_answer() -> str:
    return json.dumps({'addressed': True, 'onTopic': True, 'smallTalk': False, 'cardIntent': 'none', 'agrees': False, 'mindBlown': False, 'great': False, 'language': 'en', 'reason': 'card'})


def mixed(**over):
    return dataclasses.replace(settings(), investigate_mode='live', **BOTH, **over)


def setting(c: TestClient, key: str) -> dict:
    return next(i for i in c.get('/api/admin/settings', headers=AUTH).json()['items'] if i['key'] == key)


def test_each_use_can_name_a_different_provider(store: Store):
    # given
    seed(store, anomaly(KEY, firstSeen='2026-10-22T05:00:00Z'))
    chat_action(store)
    claude = FakeAnthropic(note_and_recap())
    gpt = FakeOpenAI([reply(text('One client.'), model='gpt-6.2-sol'), reply(text(gate_answer()), model='gpt-6-luna')])
    s = mixed(chat_model='gpt-6.2-sol', investigate_gate_model='gpt-6-luna')
    inv = Investigator(store, sources(), effects(), s, client={'anthropic': claude, 'openai': gpt}, clock=lambda: NOW)

    # when
    chat = inv.run_once(NOW)
    card = inv.run_once(NOW)
    verdict = Gate(store, s, client={'anthropic': claude, 'openai': gpt}, clock=lambda: NOW).check('is checkout-service ok?', 'page', 'alice')

    # then
    assert (inv.model, inv.chat_model, inv.slack_model) == ('claude-opus-5-5', 'gpt-6.2-sol', 'claude-sonnet-5-5')
    assert (chat['trigger'], chat['outcome'], chat['model']) == ('chat', 'done', 'gpt-6.2-sol')
    assert (card['trigger'], card['outcome'], card['model']) == ('cards', 'done', 'claude-opus-5-5')
    assert [r['model'] for r in gpt.requests] == ['gpt-6.2-sol', 'gpt-6-luna'] and {r['model'] for r in claude.requests} == {'claude-opus-5-5'}
    assert verdict.addressed and 'fallbackFrom' not in chat and 'fallbackFrom' not in card
    assert ai.provider_of_ref('deepseek/deepseek-chat') == 'compat:deepseek' and ai.provider_of_ref('gpt-6-luna') == 'openai' and ai.provider_of_ref('claude-haiku-5-5') == 'anthropic'


def test_a_critical_override_on_another_provider_is_used_when_it_has_a_key(store: Store):
    # given
    seed(store, anomaly(KEY, sev='crit', firstSeen='2026-10-22T05:00:00Z'))
    claude = FakeAnthropic(note_and_recap())
    s = mixed(ai_provider='openai', investigate_model_critical='claude-opus-5')
    inv = Investigator(store, sources(), effects(), s, client={'anthropic': claude, 'openai': FakeOpenAI([])}, clock=lambda: NOW)

    # when
    record = inv.run_once(NOW)

    # then
    assert (inv.model, inv.card_model({'crit'}), inv.card_model({'warn'})) == ('gpt-6.2-astra', 'claude-opus-5', 'gpt-6.2-astra')
    assert (record['outcome'], record['model']) == ('done', 'claude-opus-5') and {r['model'] for r in claude.requests} == {'claude-opus-5'}
    assert {r['provider'] for r in store.ledger.find({'runId': record['id']})} == {'anthropic'}


def test_a_slack_ask_on_a_chosen_provider_without_a_key_is_held_and_says_so(store: Store):
    # given
    seed(store, anomaly(KEY, firstSeen='2026-10-22T05:00:00Z'), history=[])
    slack_action(store)
    claude = FakeAnthropic([claude_reply(claude_text('One client.'), stop='end_turn')])
    s = dataclasses.replace(settings(), investigate_mode='live', anthropic_api_key='test-anthropic-key', investigate_slack_model='gpt-6.2-sol')
    inv = Investigator(store, sources(), effects(), s, client=claude, clock=lambda: NOW)
    c = TestClient(create_app(s, store))

    # when
    record = inv.run_once(NOW)
    notices = c.get('/api/admin/notices', headers=AUTH).json()['items']
    rows = {r['use']: r for r in c.get('/api/admin/ai-uses', headers=AUTH).json()['uses']}

    # then
    assert claude.requests == [] and inv.slack_model == 'gpt-6.2-sol'
    assert (record['trigger'], record['outcome'], record['model']) == ('slack', 'failed', 'gpt-6.2-sol') and 'fallbackFrom' not in record
    assert [n['id'] for n in notices if n['id'].startswith('ai-use:')] == ['ai-use:slack']
    assert (rows['slack']['provider'], rows['slack']['model'], rows['slack']['problem']) == ('openai', 'gpt-6.2-sol', msg('ai.use_held', owner='OpenAI'))
    assert all(rows[u]['problem'] is None for u in USES if u != 'slack')


def admin_set(store: Store, key: str, value: str) -> None:
    store.db['tunables'].insert_one({'_id': key, 'value': value, 'by': 'alice@example.com', 'at': '2026-11-01T00:00:00Z'})


def test_the_upgrade_keeps_the_models_each_use_billed_before(store: Store):
    # given
    s = dataclasses.replace(settings(), ai_provider='openai', **BOTH, investigate_gate_model='claude-sonnet-5-5', investigate_model_critical='claude-opus-5', investigate_model_high='gpt-6.1-terra')
    admin_set(store, 'investigate.model', 'claude-opus-5')
    admin_set(store, 'chat.model', 'gpt-6.1-terra')

    # when
    with TestClient(create_app(s, store)) as c:
        inv = c.app.state.investigator
        models = (inv.model, inv.card_model({'crit'}), inv.card_model({'high'}), inv.slack_model, inv.chat_model, c.app.state.gate.model, c.app.state.self_review.model)
    changes = {r['key']: (r['old'], r['new']) for r in store.db['tunable_changes'].find({'by': 'upgrade'})}

    # then
    assert changes == {'investigate.model': ('claude-opus-5', 'gpt-6.2-astra'), 'investigate.gate-model': ('claude-sonnet-5-5', 'gpt-6-luna'), 'investigate.model-critical': ('claude-opus-5', '')}
    assert models == ('gpt-6.2-astra', 'gpt-6.2-astra', 'gpt-6.1-terra', 'gpt-6.2-sol', 'gpt-6.1-terra', 'gpt-6-luna', 'gpt-6.2-sol')


def test_the_upgrade_migration_runs_once_and_is_revertable(store: Store):
    # given
    s = dataclasses.replace(settings(), ai_provider='openai', **BOTH)
    admin_set(store, 'investigate.model', 'claude-opus-5')
    with TestClient(create_app(s, store)) as first:
        upgraded = [r for r in first.get('/api/admin/history', headers=AUTH).json() if r.get('by') == 'upgrade']

        # when
        chosen = first.put('/api/admin/settings', json={'key': 'investigate.slack-model', 'value': 'claude-sonnet-5-5'}, headers=AUTH)
        reverted = first.put('/api/admin/settings', json={'key': 'investigate.model', 'value': upgraded[0]['old']}, headers=AUTH)
    with TestClient(create_app(s, store)) as again:
        inv = again.app.state.investigator
        models = (inv.model, inv.slack_model)

    # then
    assert [(r['key'], r['old'], r['new']) for r in upgraded] == [('investigate.model', 'claude-opus-5', 'gpt-6.2-astra')]
    assert chosen.status_code == 200 and reverted.status_code == 200
    assert store.db['tunable_changes'].count_documents({'by': 'upgrade'}) == 1
    assert models == ('claude-opus-5', 'claude-sonnet-5-5')


def test_a_fresh_store_keeps_a_mixed_config_and_sets_the_marker(store: Store):
    # given
    s = dataclasses.replace(mixed(investigate_slack_model='gpt-6.2-sol', chat_model='gpt-6.1-terra'), investigate_mode='off')

    # when
    with TestClient(create_app(s, store)) as c:
        inv = c.app.state.investigator
        models = (inv.model, inv.slack_model, inv.chat_model)

    # then
    assert models == ('claude-opus-5-5', 'gpt-6.2-sol', 'gpt-6.1-terra')
    assert store.db['tunable_changes'].count_documents({'by': 'upgrade'}) == 0
    assert store.db['tunables_rev'].find_one({'_id': 'upgrade:ai-uses'})['fresh'] is True


def test_a_failed_upgrade_write_is_retried_on_the_next_start(store: Store, monkeypatch):
    # given
    from skopax.tunables import TunableStore
    s = dataclasses.replace(settings(), ai_provider='openai', **BOTH, investigate_gate_model='claude-sonnet-5-5')
    admin_set(store, 'investigate.model', 'claude-opus-5')
    real = TunableStore.write
    broken = ['investigate.model']

    def write(self, key, value, by, old, *a, **kw):
        if by == 'upgrade' and key in broken:
            raise RuntimeError('the write was refused')
        return real(self, key, value, by, old, *a, **kw)

    monkeypatch.setattr(TunableStore, 'write', write)

    # when
    with TestClient(create_app(s, store)):
        pending = store.db['tunables_rev'].find_one({'_id': 'upgrade:ai-uses:pending'})
        marked = store.db['tunables_rev'].find_one({'_id': 'upgrade:ai-uses'})
    broken.clear()
    with TestClient(create_app(s, store)) as again:
        model = again.app.state.investigator.model

    # then
    assert pending['keys'] == ['investigate.model'] and marked is None
    assert sorted((r['key'], r['old'], r['new']) for r in store.db['tunable_changes'].find({'by': 'upgrade'})) == [
        ('investigate.gate-model', 'claude-sonnet-5-5', 'gpt-6-luna'), ('investigate.model', 'claude-opus-5', 'gpt-6.2-astra')]
    assert model == 'gpt-6.2-astra' and store.db['tunables_rev'].find_one({'_id': 'upgrade:ai-uses:pending'}) is None
    assert store.db['tunables_rev'].find_one({'_id': 'upgrade:ai-uses'}) is not None


def test_caching_marks_are_unchanged_for_an_anthropic_run(store: Store):
    # given
    seed(store, anomaly(KEY, firstSeen='2026-10-22T05:00:00Z'))
    claude = FakeAnthropic([claude_reply(claude_text('Fine.'), stop='end_turn')])
    inv = Investigator(store, sources(), effects(), mixed(chat_model='gpt-6.2-sol'), client={'anthropic': claude, 'openai': FakeOpenAI([])}, clock=lambda: NOW)

    # when
    record = inv.run_once(NOW)

    # then
    sent = claude.requests[0]
    assert record['outcome'] == 'done' and sent['model'] == 'claude-opus-5-5'
    assert sent['system'][0]['cache_control'] == {'type': 'ephemeral'} and sent['messages'][0]['content'][-1]['cache_control'] == {'type': 'ephemeral'}
    assert (sent['thinking'], sent['betas'], sent['extra_body']) == ({'type': 'adaptive'}, ['server-side-fallback-2026-07-01'], {'fallbacks': 'default'})
    assert {t['type'] for t in sent['tools'] if 'type' in t} == {'web_fetch_20250910', 'web_search_20250305'}


def test_the_ledger_records_the_provider(store: Store):
    # given
    seed(store, anomaly(KEY, firstSeen='2026-10-22T05:00:00Z'))
    claude = FakeAnthropic(note_and_recap())
    gpt = FakeOpenAI([reply(text(gate_answer()), model='gpt-6-luna', u=usage(1500, 40))])
    s = mixed(investigate_gate_model='gpt-6-luna')
    inv = Investigator(store, sources(), effects(), s, client={'anthropic': claude, 'openai': gpt}, clock=lambda: NOW)

    # when
    record = inv.run_once(NOW)
    Gate(store, s, client={'anthropic': claude, 'openai': gpt}, clock=lambda: NOW).check('is checkout-service ok?', 'page', 'alice')

    # then
    rows = list(store.ledger.find({}))
    assert {(r['source'], r['provider'], r['model'], r.get('use')) for r in rows} == {('pod', 'anthropic', 'claude-opus-5-5', 'card'), ('gate', 'openai', 'gpt-6-luna', 'gate')}
    assert all(r['runId'] == record['id'] for r in rows if r['source'] == 'pod')


def test_ai_uses_api_lists_every_use_with_price_and_problem(store: Store):
    # given
    s = dataclasses.replace(settings(), anthropic_api_key='test-anthropic-key', chat_model='gpt-6.1-terra', investigate_model_high='claude-sonnet-5-5')
    day = clock.day(NOW)
    used = Usage(input=1_000_000)
    for run, sev in (('r1', 'card'), ('r1', 'card'), ('r2', 'card'), ('r3', 'high')):
        store.add_ledger({'source': 'pod', 'use': sev, 'day': day, 'provider': 'anthropic', 'model': 'claude-opus-5-5', 'effort': 'medium', 'runId': run, 'actionId': None, 'keys': [], 'by': 'pod',
                          **pricing.split('claude-opus-5-5', used)})
    store.add_ledger({'source': 'threadsummary', 'day': day, 'provider': 'anthropic', 'model': 'claude-haiku-5-5', 'effort': 'low', 'runId': None, 'actionId': None, 'keys': [], 'by': 'pod',
                      **pricing.split('claude-haiku-5-5', used)})
    c = TestClient(create_app(s, store, clock=lambda: NOW))

    # when
    got = c.get('/api/admin/ai-uses', headers=AUTH)
    anon = TestClient(create_app(dataclasses.replace(s, public_read=False), store)).get('/api/admin/ai-uses')
    operator = app_client(store, TokenEndpoint(access_token([ROLE])))
    log_in(operator)
    refused = operator.get('/api/admin/ai-uses')
    rows = {r['use']: r for r in got.json()['uses']}

    # then
    assert got.status_code == 200 and anon.status_code in (401, 403)
    assert (refused.status_code, refused.json()['detail']) == (403, 'This needs the admin role.')
    assert [r['use'] for r in got.json()['uses']] == USES
    assert set(rows['card']) == {'use', 'label', 'setting', 'model', 'provider', 'company', 'price', 'avgCost7d', 'problem', 'fit', 'advisor'}
    assert (rows['card']['label'], rows['card']['setting'], rows['card']['model'], rows['card']['company']) == ('Card runs', 'investigate.model', 'claude-opus-5-5', 'Anthropic')
    assert rows['card']['price'] == {'input': 4.0, 'output': 20.0} and rows['card']['fit'] == 'ok'
    assert rows['card']['avgCost7d'] == 6.0 and rows['high']['avgCost7d'] == 4.0 and rows['critical']['avgCost7d'] is None and rows['gate']['avgCost7d'] == 0.1
    assert (rows['critical']['model'], rows['high']['model']) == ('claude-opus-5-5', 'claude-sonnet-5-5')
    assert (rows['chat']['provider'], rows['chat']['model'], rows['chat']['problem']) == ('openai', 'gpt-6.1-terra', msg('ai.use_held', owner='OpenAI'))
    assert (rows['plugin']['setting'], rows['plugin']['model']) == ('website.model', 'claude-opus-5-5')
    assert (rows['guard']['label'], rows['guard']['setting'], rows['guard']['model'], rows['guard']['problem']) == ('Guard', 'investigate.guard-model', '', None)
    assert 'test-anthropic-key' not in got.text


class Narrow:
    provider = 'anthropic'
    capabilities = frozenset({'tools'})
    bad_request = ValueError
    api_error = RuntimeError

    def __init__(self, answer: str = 'One client.'):
        self.turns = []
        self.onces = []
        self.answer = answer

    def turn(self, model, effort, system, tools, messages, max_tokens, cache_ttl=None, **extra):
        self.turns.append({'model': model, 'tools': tools, 'messages': messages, 'extra': extra})
        return Turn(stop='end_turn', model=model, usage=Usage(input=10, output=5), texts=[self.answer], text=self.answer, native=[{'type': 'text', 'text': self.answer}])

    def once(self, model, system, messages, max_tokens, effort, thinking=ai.THINK_OFF, schema=None):
        self.onces.append({'model': model, 'thinking': thinking})
        return Turn(stop='end_turn', model=model, usage=Usage(input=10, output=5), texts=[self.answer], text=self.answer)


class Toolless(Narrow):
    capabilities = frozenset({'images'})


def test_capabilities_drop_web_tools_and_images(store: Store):
    # given
    seed(store, anomaly(KEY), history=[])
    slack_action(store, images=[{'name': 'error.png', 'mime': 'image/png', 'size': 2048, 'url': 'https://files.slack.com/files-pri/T1-F1/error.png'}])
    read = []
    narrow, toolless = Narrow(), Toolless()
    s = dataclasses.replace(settings(), anthropic_api_key='test-anthropic-key', investigate_mode='live', self_project='platform/skopax')
    fx = Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}, lambda action, outcome, recap: {'ts': '1', 'text': recap}, slack_file=lambda url: read.append(url) or (b'png', 'image/png'))
    inv = Investigator(store, sources(), fx, s, client=narrow, clock=lambda: NOW)
    review = SelfReview(store, s, lambda now: {}, lambda text: {'review': {'text': text}}, client=narrow, clock=lambda: NOW)

    # when
    record = inv.run_once(NOW)
    slack_action(store)
    refused = Investigator(store, sources(), fx, s, client=toolless, clock=lambda: NOW).run_once(NOW)
    review.run(NOW)

    # then
    sent = narrow.turns[0]
    assert record['outcome'] == 'done' and read == [] and 'images' not in record
    assert not [t for t in sent['tools'] if t.get('type') in (ai.SEARCH_TYPE, ai.FETCH_TYPE)]
    assert isinstance(sent['messages'][0]['content'], str) and ai.IMAGES_OFF in sent['messages'][0]['content']
    assert ai.IMAGES_OFF in [a['text'] for a in store.list_activity(None, 20) if a.get('runId') == record['id']]
    assert sent['extra'] == {'thinking': ai.THINK_OFF, 'fallbacks': False} and narrow.onces == [{'model': 'claude-sonnet-5-5', 'thinking': ai.THINK_OFF}]
    assert (refused['outcome'], refused['error'], toolless.turns) == ('failed', msg('ai.no_tools'), [])
    assert any(msg('ai.no_tools') in a['text'] for a in store.list_activity(None, 30) if a.get('runId') == refused['id'])
    assert aiuses.problem_of('chat', 'anthropic', 'claude-sonnet-5-5', None, 'anthropic', frozenset({'images'})) == msg('ai.no_tools')
    assert aiuses.problem_of('gate', 'anthropic', 'claude-haiku-5-5', None, 'anthropic', frozenset({'images'})) is None
    assert ai.capabilities_of(FakeAnthropic([])) == ai.CAPABILITIES and ai.capabilities_of(FakeOpenAI([])) == frozenset({'tools', 'images', 'web_search', 'thinking', 'cache'})


class Compat(Narrow):
    provider = 'compat:x'
    capabilities = frozenset({'tools'})


def test_a_registered_compat_provider_serves_a_use_end_to_end(store: Store, monkeypatch):
    # given
    seed(store, anomaly(KEY, firstSeen='2026-10-22T05:00:00Z'))
    monkeypatch.setitem(pricing.PRICES, 'x/m', {'input_tokens': 0.5, 'output_tokens': 1.0, 'cache_read_input_tokens': 0.05, 'cache_creation_input_tokens': 0.6})
    compat = Compat(gate_answer())
    built = []
    s = dataclasses.replace(settings(), investigate_mode='live', anthropic_api_key='', investigate_model='x/m', investigate_gate_model='x/m')
    c = TestClient(create_app(dataclasses.replace(s, investigate_mode='off'), store))
    ai.register_provider('compat', lambda name, timeout, retries: built.append(name) or (compat if name == 'x' else None), {'tools'}, models=lambda: ('x/m',),
                         company=lambda ref: 'llm.example.com')
    try:
        inv = Investigator(store, sources(), effects(), s, clock=lambda: NOW)
        gate = Gate(store, s, clock=lambda: NOW)

        # when
        ready = (inv.enabled, gate.enabled)
        record = inv.run_once(NOW)
        verdict = gate.check('is checkout-service ok?', 'page', 'alice')
        put = c.put('/api/admin/settings', json={'key': 'chat.model', 'value': 'x/m'}, headers=AUTH)
        chat = setting(c, 'chat.model')
        rows = {r['use']: r for r in c.get('/api/admin/ai-uses', headers=AUTH).json()['uses']}
    finally:
        ai.unregister_provider('compat')

    # then
    assert ready == (True, True) and (record['outcome'], record['model']) == ('done', 'x/m') and verdict.addressed
    assert {(r['source'], r['provider'], r['model']) for r in store.ledger.find({})} == {('pod', 'compat:x', 'x/m'), ('gate', 'compat:x', 'x/m')}
    assert put.status_code == 200 and chat['value'] == 'x/m' and chat['choiceGroups']['x/m'] == 'x'
    assert (rows['card']['provider'], rows['card']['company'], rows['card']['fit'], rows['card']['problem']) == ('compat:x', 'llm.example.com', 'cheap', None)
    assert (rows['chat']['provider'], rows['chat']['company'], rows['chat']['fit']) == ('compat:x', 'llm.example.com', 'ok')


def test_an_unknown_or_unpriced_override_runs_the_card_model_and_says_so_on_the_run_and_the_row(store: Store):
    # given
    seed(store, anomaly(KEY, sev='crit', firstSeen='2026-10-22T05:00:00Z'))
    s = dataclasses.replace(settings(), investigate_mode='live', anthropic_api_key='test-anthropic-key', investigate_model_critical='claude-x', investigate_model_high='gpt-6.2-sol')
    inv = Investigator(store, sources(), effects(), s, client=FakeAnthropic(note_and_recap()), clock=lambda: NOW)
    c = TestClient(create_app(s, store))

    # when
    record = inv.run_once(NOW)
    rows = {r['use']: r for r in c.get('/api/admin/ai-uses', headers=AUTH).json()['uses']}
    notices = {n['id']: n['text'] for n in c.get('/api/admin/notices', headers=AUTH).json()['items'] if n['id'].startswith('ai-use:')}

    # then
    assert (record['model'], record['fallbackFrom']) == ('claude-opus-5-5', 'claude-x')
    assert {(r['use'], r['fallbackFrom']) for r in store.ledger.find({'runId': record['id']})} == {('card', 'claude-x')}
    assert rows['critical']['problem'] == msg('ai.model_unknown_fallback', model='claude-x', used='claude-opus-5-5') and rows['critical']['model'] == 'claude-opus-5-5'
    assert rows['high']['problem'] == msg('ai.use_held', owner='OpenAI') and (rows['high']['provider'], rows['high']['model']) == ('openai', 'gpt-6.2-sol')
    assert set(notices) == {'ai-use:critical', 'ai-use:high'} and notices['ai-use:critical'] == msg('ai.use_problem', use='Critical cards', problem=rows['critical']['problem'])


def test_a_plugin_model_of_a_provider_without_a_key_is_held_and_shows_a_notice(store: Store):
    # given
    from types import SimpleNamespace as NS
    s = dataclasses.replace(settings(), anthropic_api_key='test-anthropic-key')
    c = TestClient(create_app(s, store))
    keys = {'anthropic': lambda: 'test-anthropic-key', 'openai': lambda: ''}
    plug = PluginAi(store, None, lambda now: None, client=FakeAnthropic([], gate_replies=[claude_reply(claude_text('ok'), stop='end_turn')]), clock=lambda: NOW, keys=keys)

    # when
    put = c.put('/api/admin/settings', json={'key': 'website.model', 'value': 'gpt-6.2-sol'}, headers=AUTH)
    notices = {n['id']: n['text'] for n in c.get('/api/admin/notices', headers=AUTH).json()['items'] if n['id'].startswith('ai-')}
    made = plug.for_check(NS(id='website'), NS(id='pagescan'), None, 'gpt-6.2-sol')

    # then
    assert put.status_code == 200 and 'ai-plugin:website.model' in notices
    assert 'the chosen provider is not usable; runs of this use are held' in notices['ai-plugin:website.model']
    assert made is None and store.ledger.count_documents({}) == 0


def test_an_empty_model_from_the_environment_keeps_that_use_off(store: Store):
    # given
    from skopax.config import load_settings
    base = load_settings(env={'SELF_REVIEW_MODEL': '', 'INVESTIGATE_GATE_MODEL': ''})
    s = dataclasses.replace(base, anthropic_api_key='test-anthropic-key', self_project='platform/skopax')

    # when
    review = SelfReview(store, s, lambda now: {}, lambda text: {})
    gate = Gate(store, s)

    # then
    assert (review.model, review.configured, gate.model, gate.enabled) == ('', False, '', False)


def test_off_hints_and_probe_targets_follow_the_routed_provider(store: Store):
    # given
    from skopax.probe import core_targets
    s = dataclasses.replace(mixed(investigate_model='gpt-6.2-sol'), investigate_mode='off')
    c = TestClient(create_app(s, store))

    # when
    fetches = setting(c, 'investigate.web-fetches')
    gate_effort = setting(c, 'investigate.gate-effort')
    targets = core_targets(False, ('anthropic', 'openai'), True, {}, [], '')

    # then
    assert fetches['off'] == msg('ai.off_openai') and 'off' not in gate_effort
    assert [t['instance'] for t in targets] == ['anthropic', 'openai']


def test_a_pending_retry_never_overwrites_an_admin_edit_made_in_between(store: Store, monkeypatch):
    # given
    from skopax.tunables import TunableStore
    s = dataclasses.replace(settings(), ai_provider='openai', **BOTH)
    admin_set(store, 'investigate.model', 'claude-opus-5')
    admin_set(store, 'chat.model', 'claude-sonnet-5-5')
    real = TunableStore.write
    broken = ['investigate.model', 'chat.model']

    def write(self, key, value, by, old, *a, **kw):
        if by == 'upgrade' and key in broken:
            raise RuntimeError('the write was refused')
        return real(self, key, value, by, old, *a, **kw)

    monkeypatch.setattr(TunableStore, 'write', write)
    with TestClient(create_app(s, store)) as first:
        pending = store.db['tunables_rev'].find_one({'_id': 'upgrade:ai-uses:pending'})
        broken.clear()
        edited = first.put('/api/admin/settings', json={'key': 'investigate.model', 'value': 'claude-fable-5-2'}, headers=AUTH)

    # when
    with TestClient(create_app(s, store)) as again:
        inv = again.app.state.investigator
        models = (inv.model, inv.chat_model)

    # then
    assert edited.status_code == 200 and pending['values'] == {'investigate.model': 'claude-opus-5', 'chat.model': 'claude-sonnet-5-5'}
    assert models == ('claude-fable-5-2', 'gpt-6.2-sol')
    assert [(r['key'], r['old'], r['new']) for r in store.db['tunable_changes'].find({'by': 'upgrade'})] == [('chat.model', 'claude-sonnet-5-5', 'gpt-6.2-sol')]
    assert store.db['tunables_rev'].find_one({'_id': 'upgrade:ai-uses'}) is not None


def test_a_card_whose_chosen_models_have_no_usable_provider_is_never_run_on_the_default_provider(store: Store):
    # given
    seed(store, anomaly(KEY, sev='crit', firstSeen='2026-10-22T05:00:00Z'))
    claude = FakeAnthropic(note_and_recap())
    s = dataclasses.replace(settings(), investigate_mode='live', anthropic_api_key='test-anthropic-key', investigate_model='gpt-6.2-sol', investigate_model_critical='gpt-6.2-astra')
    inv = Investigator(store, sources(), effects(), s, client=claude, clock=lambda: NOW)

    # when
    record = inv.run_once(NOW)

    # then
    assert record is None and claude.requests == [] and store.ledger.count_documents({}) == 0
    assert inv.card_route({'crit'})[:3] == ('openai', 'gpt-6.2-astra', None) and not inv.enabled


class Opened:
    def __init__(self):
        self.metas = []

    def open(self, name, meta):
        self.metas.append((name, meta))
        return None

    def active(self):
        return False

    def clock(self):
        return NOW


def test_the_trace_and_the_probe_name_the_routed_providers(store: Store, monkeypatch):
    # given
    from skopax import tracing
    seed(store, anomaly(KEY, firstSeen='2026-10-22T05:00:00Z'))
    chat_action(store)
    opened = Opened()
    monkeypatch.setattr(tracing, 'TRACER', [opened])
    gpt = FakeOpenAI([reply(text('One client.'), model='gpt-6.2-sol')])
    inv = Investigator(store, sources(), effects(), mixed(chat_model='gpt-6.2-sol'), client={'anthropic': FakeAnthropic([]), 'openai': gpt}, clock=lambda: NOW)

    # when
    record = inv.run_once(NOW)
    c = TestClient(create_app(dataclasses.replace(mixed(chat_model='gpt-6.2-sol'), investigate_mode='off'), store))
    single = TestClient(create_app(dataclasses.replace(settings(), anthropic_api_key='test-anthropic-key'), store))
    instances = [t['instance'] for t in c.app.state.probe.targets() if t.get('plugin') == 'probe' and t['instance'] in ('anthropic', 'openai')]
    alone = [t['instance'] for t in single.app.state.probe.targets() if t.get('plugin') == 'probe' and t['instance'] in ('anthropic', 'openai')]

    # then
    assert record['trigger'] == 'chat' and [m['provider'] for n, m in opened.metas if n == 'chat answer'] == ['openai']
    assert (instances, alone) == (['anthropic', 'openai'], ['anthropic'])


def test_the_upgrade_note_for_0_6_0_names_the_one_time_model_step():
    # given
    from pathlib import Path
    lines = (Path(__file__).resolve().parent.parent / 'docs' / 'configuration.md').read_text().splitlines()

    # when
    note = next(line for line in lines if line.startswith('- **Upgrading to 0.6.0.**'))

    # then
    assert 'AGPL-3.0-only' in note
    assert 'one-time upgrade step' in note and '`upgrade`' in note and 'Changes' in note
    assert 'OpenAI-compatible' in note and 'Admin > Investigator' in note and 'Admin > AI' not in note
    assert '`<0.6`' in note and 'Apache-2.0 with the DCO' in note
    assert 'Available in questions, Everyone by default' in note
    assert 'Free GAQL queries' in note and 'conversion value' in note
    assert 'Conversation text is set to hide by default' in note
    assert 'confidential-data guard' in note and 'scanned once more' in note
    assert 'side by side' in note and 'do not roll back to 0.5.18' in note
    assert 'the chosen provider is not usable; runs of this use are held' in note and 'never moved to another provider' in note
    assert note.isascii() and sum(line.startswith('- **Upgrading to 0.6.0.**') for line in lines) == 1
    assert all(phrase not in note for phrase in ('No migration', 'keep working', 'as before', 'still works'))


def test_a_chosen_model_whose_provider_is_not_usable_fails_closed(store: Store):
    # given
    from tests.test_chat import chat_action as asked
    seed(store, anomaly(KEY, firstSeen='2026-10-22T05:00:00Z'), history=[])
    claude = FakeAnthropic([claude_reply(claude_text('One client.'), stop='end_turn')])
    s = dataclasses.replace(settings(), investigate_mode='live', anthropic_api_key='test-anthropic-key', chat_model='gpt-6.2-sol')
    inv = Investigator(store, sources(), effects(), s, client=claude, clock=lambda: NOW)
    asked(store)
    c = TestClient(create_app(s, store))

    # when
    record = inv.run_once(NOW)
    rows = {r['use']: r for r in c.get('/api/admin/ai-uses', headers=AUTH).json()['uses']}
    notices = {n['id'] for n in c.get('/api/admin/notices', headers=AUTH).json()['items']}

    # then
    assert claude.requests == []
    assert (record['trigger'], record['outcome'], record['model']) == ('chat', 'failed', 'gpt-6.2-sol') and 'fallbackFrom' not in record
    assert 'the chosen provider is not usable; runs of this use are held' in record['error']
    assert (rows['chat']['provider'], rows['chat']['model']) == ('openai', 'gpt-6.2-sol') and 'the chosen provider is not usable; runs of this use are held' in rows['chat']['problem']
    assert 'ai-use:chat' in notices
    assert store.ledger.count_documents({}) == 0


def test_a_use_left_on_its_default_follows_the_default_provider(store: Store):
    # given
    seed(store, anomaly(KEY, firstSeen='2026-10-22T05:00:00Z'), history=[])
    slack_action(store)
    claude = FakeAnthropic([claude_reply(claude_text('One client.'), stop='end_turn')])
    s = dataclasses.replace(settings(), investigate_mode='live', anthropic_api_key='test-anthropic-key')
    inv = Investigator(store, sources(), effects(), s, client=claude, clock=lambda: NOW)

    # when
    record = inv.run_once(NOW)

    # then
    assert (record['trigger'], record['outcome'], record['model']) == ('slack', 'done', 'claude-sonnet-5-5') and len(claude.requests) == 1


def test_a_use_whose_upgrade_write_failed_keeps_billing_the_provider_it_billed_before(store: Store, monkeypatch):
    # given
    from skopax.tunables import TunableStore
    s = dataclasses.replace(settings(), ai_provider='openai', **BOTH)
    admin_set(store, 'investigate.model', 'claude-opus-5')
    real = TunableStore.write

    def write(self, key, value, by, old, *a, **kw):
        if by == 'upgrade':
            raise RuntimeError('the write was refused')
        return real(self, key, value, by, old, *a, **kw)

    monkeypatch.setattr(TunableStore, 'write', write)

    # when
    with TestClient(create_app(s, store)) as c:
        pending = store.db['tunables_rev'].find_one({'_id': 'upgrade:ai-uses:pending'})
        route = c.app.state.investigator.use_route('card')

    # then
    assert pending['keys'] == ['investigate.model']
    assert route == ('openai', 'gpt-6.2-astra', None)


def chat_effort_changes(store: Store) -> list[tuple[str, str, str]]:
    return [(r['key'], r['old'], r['new']) for r in store.db['tunable_changes'].find({'by': 'upgrade', 'key': 'chat.effort'})]


def test_the_upgrade_writes_the_slack_effort_into_an_unset_chat_effort_once_and_the_two_are_independent_after(store: Store):
    # given
    s = dataclasses.replace(settings(), investigate_slack_effort='high')

    # when
    with TestClient(create_app(s, store)) as first:
        first_effort = first.app.state.investigator.chat_effort
        first.put('/api/admin/settings', json={'key': 'investigate.slack-effort', 'value': 'low'}, headers=AUTH)
        second_effort = first.app.state.investigator.chat_effort
    with TestClient(create_app(dataclasses.replace(s, investigate_slack_effort='low'), store)) as again:
        again_effort = again.app.state.investigator.chat_effort

    # then
    assert chat_effort_changes(store) == [('chat.effort', 'medium', 'high')]
    assert (first_effort, second_effort, again_effort) == ('high', 'high', 'high')


def test_the_upgrade_leaves_a_chat_effort_that_was_given_alone(store: Store):
    # given
    admin_set(store, 'chat.effort', 'xhigh')
    s = dataclasses.replace(settings(), investigate_slack_effort='high')

    # when
    with TestClient(create_app(s, store)) as c:
        effort = c.app.state.investigator.chat_effort

    # then
    assert effort == 'xhigh' and chat_effort_changes(store) == []


def test_the_upgrade_turns_a_stored_empty_chat_effort_into_the_slack_effort(store: Store):
    # given
    admin_set(store, 'chat.effort', '')
    s = dataclasses.replace(settings(), investigate_slack_effort='low')

    # when
    with TestClient(create_app(s, store)) as c:
        effort = c.app.state.investigator.chat_effort

    # then
    assert effort == 'low' and chat_effort_changes(store) == [('chat.effort', 'medium', 'low')]


def test_chat_follows_the_slack_effort_until_the_upgrade_step_has_run_and_stays_put_after():
    # given
    from skopax.tunables import Live, TunableStore
    import mongomock

    db = mongomock.MongoClient().db
    live = Live(dataclasses.replace(settings(), investigate_slack_effort='high'), TunableStore(db))
    live.refresh(force=True)

    # when
    before = (live.current().chat_effort_follows, live.current().chat_effort)
    live.upgrade_chat_effort()
    after = (live.current().chat_effort_follows, live.current().chat_effort)

    # then
    assert before == (True, 'medium') and after == (False, 'high')


def test_a_slack_effort_that_is_not_a_level_skips_the_chat_effort_write_with_a_notice_and_does_not_retry(store: Store):
    # given
    s = dataclasses.replace(settings(), investigate_slack_effort='extreme')

    # when
    with TestClient(create_app(s, store)) as c:
        with TestClient(create_app(s, store)) as again:
            notices = again.get('/api/admin/notices', headers=AUTH).json()
    mark = store.db['tunables_rev'].find_one({'_id': 'upgrade:chat-effort'})

    # then
    assert chat_effort_changes(store) == [] and mark['skipped'] is True
    assert 'chat-effort-skipped' in [n['id'] for n in notices['items']]


def test_another_pod_shows_a_just_saved_guard_model_at_once(store: Store):
    # given
    s = dataclasses.replace(settings(), **BOTH)
    with TestClient(create_app(s, store)) as saving, TestClient(create_app(s, store)) as other:
        before = {r['use']: r for r in other.get('/api/admin/ai-uses', headers=AUTH).json()['uses']}['guard']['model']

        # when
        saved = saving.put('/api/admin/settings', json={'key': 'investigate.guard-model', 'value': 'gpt-6.2-sol'}, headers=AUTH)
        after = {r['use']: r for r in other.get('/api/admin/ai-uses', headers=AUTH).json()['uses']}['guard']

    # then
    assert saved.status_code == 200
    assert (before, after['model'], after['provider']) == ('', 'gpt-6.2-sol', 'openai')


def test_another_pod_shows_the_private_address_switch_at_once(store: Store):
    # given
    s = dataclasses.replace(settings(), **BOTH)
    with TestClient(create_app(s, store)) as saving, TestClient(create_app(s, store)) as other:
        before = other.get('/api/admin/ai-connections', headers=AUTH).json()['allowPrivate']

        # when
        saved = saving.put('/api/admin/settings', json={'key': 'core.allow-private-ai', 'value': 'true'}, headers=AUTH)
        after = other.get('/api/admin/ai-connections', headers=AUTH).json()['allowPrivate']

    # then
    assert saved.status_code == 200
    assert (before, after) == (False, True)
