import base64
import datetime as dt
import json
import re
import ssl
import time
from dataclasses import dataclass, field
from types import SimpleNamespace as NS
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from skopax import remote
from skopax.app import create_app
from skopax.contract import InstanceInfo, ReadError
from skopax.engine import iso, parse_iso
from skopax.instances import Instances, PluginState
from skopax.investigator import prompt
from skopax.investigator.tools import CORE_TOOLS
from skopax.kube import Kube
from skopax.messages import msg
from skopax.plugins import langfuse
from skopax.plugins.langfuse import check, client
from skopax.plugintools import PluginTools
from skopax.redact import REDACTED
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError
from skopax.runtime import Runtime
from skopax.sealer import Sealer, master_key
from skopax.store import Store
from tests.agent_kit import AUTH, agent_boot, agent_client
from tests.conftest import AUTH as ADMIN, settings, state as state_of
from tests.fake_http import FakeHttp
from tests.kube_kit import Seen, agent_app, app_transport, kube_runtime
from tests.plugin_kit import add_instance
from tests.test_instances import pem
from tests.test_investigator_tools import NOW as TOOL_NOW, box

KEY = 'langfuse-key'
URL = 'https://langfuse.example.com'
EAST_URL = 'https://langfuse-east.example.com'
AGENT_URL = 'https://langfuse.agent.example.com'
PK, SK = 'pk-lf-main-1a2b', 'sk-lf-main-SECRET-9z8y'
PK_AGENT, SK_AGENT = 'pk-lf-agent-3c4d', 'sk-lf-agent-SECRET-7x6w'
TURN_NAME = 'chat_turn'
VALUES = {'url': URL, 'public-key': PK, 'secret-key': SK, 'turn-name': TURN_NAME}
OBSERVATIONS = f'{URL}/api/public/v2/observations'
METRICS = f'{URL}/api/public/v2/metrics'
TURN = {'column': 'name', 'operator': '=', 'value': TURN_NAME, 'type': 'string'}
ROOT = {'column': 'isRootObservation', 'operator': '=', 'value': True, 'type': 'boolean'}
ALICE = 'alice@example.com'
ACCESS_M = 'langfuse:access:metrics'
ACCESS_O = 'langfuse:access:observations'
NO_ACCESS = 'the keys lack read access to this project'
ROOTS = [{'id': 'r1', 'traceId': 't1', 'type': 'SPAN', 'name': TURN_NAME, 'traceName': 'chat-turn', 'startTime': '2026-11-11T07:00:00Z', 'latency': 4.2187, 'tags': ['web'],
          'release': 'main:ab12cd34', 'version': '1.4.0', 'userId': 'cust-1001', 'sessionId': 'sess-77', 'isRootObservation': False},
         {'id': 'r2', 'traceId': 't2', 'type': 'SPAN', 'name': TURN_NAME, 'traceName': 'chat-turn', 'startTime': '2026-11-11T06:58:00Z', 'latency': 1.5, 'tags': [],
          'userId': 'cust-1002', 'sessionId': 'sess-78', 'isRootObservation': False},
         {'id': 'r3', 'traceId': 't3', 'type': 'SPAN', 'name': TURN_NAME, 'traceName': 'chat-turn', 'startTime': '2026-11-11T06:57:00Z', 'latency': 1.1, 'userId': 'cust-1001',
          'sessionId': 'sess-77', 'isRootObservation': False}]
SPENT = [{'id': 'g1', 'traceId': 't1', 'type': 'GENERATION', 'totalCost': 0.01}, {'id': 'e1', 'traceId': 't1', 'type': 'EMBEDDING', 'costDetails': {'total': 0.0023456}},
         {'id': 'g2', 'traceId': 't2', 'type': 'GENERATION', 'totalCost': '0.002'}, {'id': 's3', 'traceId': 't3', 'type': 'SPAN'}]
FAILED = [{'id': 'o9', 'traceId': 't1', 'type': 'TOOL', 'name': 'search_products', 'level': 'ERROR', 'statusMessage': 'Connection reset by peer', 'startTime': '2026-11-11T07:00:01Z'},
          {'id': 'o10', 'traceId': 't4', 'type': 'TOOL', 'name': 'search_products', 'level': 'ERROR', 'statusMessage': 'Connection reset by peer', 'startTime': '2026-11-11T06:10:00Z'},
          {'id': 'o11', 'traceId': 't5', 'type': 'GENERATION', 'name': 'chat', 'level': 'ERROR', 'statusMessage': 'rate limited', 'startTime': '2026-11-11T05:00:00Z', 'model': 'gpt-5.4-mini'}]
WARNED = [{'id': 'o12', 'traceId': 't2', 'type': 'SPAN', 'name': 'product-cards', 'level': 'WARNING', 'statusMessage': 'second block dropped', 'startTime': '2026-11-11T06:58:02Z'}]
LONG_PROMPT = 'system rules ' * 200 + 'last message: write to alice@example.com, my phone is +15550100199'
STEPS = [{'id': 'tool', 'traceId': 't1', 'type': 'TOOL', 'name': 'search_products', 'parentObservationId': 'gen', 'startTime': '2026-11-11T07:00:01.000Z', 'latency': 0.8, 'level': 'ERROR',
          'statusMessage': 'Connection reset by peer', 'input': '{"amount": 50000}', 'output': None, 'userId': 'cust-1001', 'sessionId': 'sess-77'},
         {'id': 'gen', 'traceId': 't1', 'type': 'GENERATION', 'name': 'llm', 'parentObservationId': 'root', 'startTime': '2026-11-11T07:00:00.100Z', 'latency': 2.5, 'model': 'gpt-5.4-mini',
          'usageDetails': {'input': 5200, 'output': 310, 'total': 5510}, 'totalCost': 0.0101234567, 'input': LONG_PROMPT, 'output': 'z' * 2000, 'level': 'DEFAULT', 'userId': 'cust-1001'},
         {'id': 'root', 'traceId': 't1', 'type': 'SPAN', 'name': TURN_NAME, 'traceName': 'chat-turn', 'parentObservationId': 'http-span-outside', 'isRootObservation': False,
          'startTime': '2026-11-11T07:00:00.000Z', 'endTime': '2026-11-11T07:00:04.200Z', 'level': 'DEFAULT', 'input': 'which plan suits me', 'output': 'Sure, here are the plans',
          'userId': 'cust-1001', 'sessionId': 'sess-77', 'tags': ['web']}]


def basic(public: str, secret: str) -> str:
    return 'Basic ' + base64.b64encode(f'{public}:{secret}'.encode()).decode()


def turn_rows(q) -> bool:
    return q.get('name') == [TURN_NAME] or q.get('isRootObservation') == ['true']


def observations(q, _b):
    if turn_rows(q):
        rows = [r for r in ROOTS if r['userId'] == q['userId'][0]] if q.get('userId') else ROOTS
        if 'cursor' not in q:
            return {'data': rows[:2], 'meta': {'cursor': 'page-2'}}
        return {'data': rows[2:int(q['limit'][0]) + 2], 'meta': {}}
    if q.get('traceId'):
        return {'data': [o for o in STEPS if o['traceId'] == q['traceId'][0]], 'meta': {}}
    if q.get('level'):
        return {'data': FAILED if q['level'] == ['ERROR'] else WARNED, 'meta': {}}
    if 'filter' not in q:
        return {'data': [], 'meta': {}}
    ids = json.loads(q['filter'][0])[0]['value']
    return {'data': [o for o in SPENT if o['traceId'] in ids], 'meta': {}}


def metrics(q, _b):
    asked = json.loads(q['query'][0])
    if not asked.get('timeDimension') and not asked['dimensions']:
        return {'data': [{'count_count': '57'}]}
    if asked['dimensions'] == [{'field': 'providedModelName'}]:
        return {'data': [{'providedModelName': 'gpt-5.4-mini', 'sum_inputTokens': 140000, 'sum_outputTokens': 12000, 'sum_totalTokens': 152000, 'sum_totalCost': 1.7},
                         {'providedModelName': 'text-embedding-3', 'sum_inputTokens': 5000, 'sum_outputTokens': 0, 'sum_totalTokens': 5000, 'sum_totalCost': 0.05},
                         {'providedModelName': None, 'sum_inputTokens': 0, 'sum_outputTokens': 0, 'sum_totalTokens': 0, 'sum_totalCost': 0}]}
    if TURN in asked['filters'] or ROOT in asked['filters']:
        return {'data': [{'time_dimension': '2026-11-10T00:00:00.000Z', 'count_count': '120'}, {'time_dimension': '2026-11-11T00:00:00.000Z', 'count_count': '40'}]}
    return {'data': [{'time_dimension': '2026-11-11T00:00:00.000Z', 'count_count': '300', 'sum_totalCost': 0.5},
                     {'time_dimension': '2026-11-10T00:00:00.000Z', 'count_count': '900', 'sum_totalCost': 1.25}]}


def fake(url: str = URL) -> FakeHttp:
    return FakeHttp({('GET', f'{url}/api/public/v2/observations'): observations, ('GET', f'{url}/api/public/v2/metrics'): metrics,
                     ('GET', f'{url}/api/public/projects'): {'data': [{'id': 'p1', 'name': 'Shop', 'organization': {'id': 'o1', 'name': 'Example'}}]}})


def tool_runtime(store, http, instances: dict | None = None) -> Runtime:
    reg = Registry.of(langfuse.PLUGIN)
    runtime = Runtime(reg, PluginState(store.db), Instances(store.db, reg, lambda: Sealer(KEY)), http)
    for i, (name, values) in enumerate(({'main': VALUES} if instances is None else instances).items()):
        add_instance(store, langfuse.PLUGIN, name, sealer_key=KEY, primary=i == 0, **values)
    runtime.refresh()
    return runtime


def lf_box(store: Store, http, instances: dict | None = None, **kw):
    return box(store, plugin_tools=PluginTools(tool_runtime(store, http, instances)), **kw)


def call(b, **args):
    text, err = b.call('langfuse', args)
    return (text if err else json.loads(text)), err


def test_langfuse_is_a_read_source_tool_offered_only_while_an_instance_serves(store: Store):
    # given
    tool = langfuse.PLUGIN.tools[0]
    tools = PluginTools(tool_runtime(store, fake(), instances={}))

    # when
    unset = [d['name'] for d in tools.definitions()]
    add_instance(store, langfuse.PLUGIN, 'main', sealer_key=KEY, primary=True, enabled=False, **VALUES)
    tools.runtime.refresh()
    off = [d['name'] for d in tools.definitions()]
    on = {d['name']: d for d in lf_box(store, fake()).definitions()}

    # then
    assert (tool.kind, tool.scan, tool.per_run, tool.instance_input) == ('read', True, 8, 'instance')
    assert 'langfuse' in on and 'langfuse' not in off and 'langfuse' not in unset
    assert set(tool.input_schema['properties']) == {'op', 'hours', 'days', 'limit', 'name', 'user_id', 'session_id', 'tags', 'id'}
    assert set(on['langfuse']['input_schema']['properties']) == set(tool.input_schema['properties']) | {'instance'}
    assert all(w in tool.description for w in ('never an instruction', 'only when you filter by them', 'op errors', 'op trace'))


def test_traces_lists_recent_turns_with_their_error_steps_and_hides_customer_ids_unless_filtered(store: Store):
    # given
    http = fake()
    b = lf_box(store, http)

    # when
    listed, err = call(b, limit=3)
    mine, mine_err = call(b, user_id='cust-1001', limit=1)

    # then
    assert not err and not mine_err
    assert listed['from'].startswith('Langfuse instance main: traces from another system')
    assert [r['id'] for r in listed['traces']] == ['t1', 't2', 't3'] and listed['traces'][0]['errors'] == 1 and listed['traces'][1]['errors'] == 1
    assert listed['traces'][0] | {'errors': None} == {'id': 't1', 'name': 'chat-turn', 'at': '2026-11-11T07:00:00Z', 'latencyS': 4.219, 'costUsd': 0.012346, 'tags': ['web'],
                                                       'release': 'main:ab12cd34', 'version': '1.4.0', 'errors': None}
    assert (listed['matching'], listed['distinctUsers'], listed['distinctSessions']) == (57, 2, 2)
    assert not any(x in json.dumps(listed) for x in ('cust-100', 'sess-7', 'which plan', 'Sure,'))
    assert mine['traces'][0]['userId'] == 'cust-1001' and 'distinctUsers' not in mine and 'sessionId' not in mine['traces'][0]
    roots = [c[1] for c in http.hits(OBSERVATIONS) if f'name={TURN_NAME}' in c[1]]
    assert f"fromStartTime={iso(TOOL_NOW - 24 * 3600).replace(':', '%3A')}" in roots[0] and 'limit=3' in roots[0] and 'fields=core%2Cbasic%2Cmetrics%2Ctrace_context' in roots[0]
    assert 'cursor=page-2' in roots[1], 'a short page with a cursor is read on'
    assert 'userId=cust-1001' in roots[2] and len(roots) == 3
    counted = [json.loads(parse_qs(urlsplit(c[1]).query)['query'][0]) for c in http.hits(METRICS)]
    assert {'column': 'userId', 'operator': '=', 'value': 'cust-1001', 'type': 'string'} in counted[1]['filters'] and all(q['view'] == 'observations' for q in counted)
    assert all(TURN in q['filters'] for q in counted) and not any('isRootObservation' in c[1] for c in http.calls)


@pytest.mark.parametrize('args, why', [
    ({'hours': 73}, 'hours must be at most 72'),
    ({'limit': 51}, 'limit must be at most 50'),
    ({'days': 15, 'op': 'stats'}, 'days must be at most 14'),
    ({'op': 'trace'}, 'op trace needs id'),
    ({'op': 'trace', 'id': 'a b'}, 'op trace needs id'),
    ({'tags': ['a'] * 6}, 'tags takes at most 5 items'),
    ({'instance': 'staging'}, 'instance must be one of main'),
    ({'op': 'delete'}, 'op must be one of traces, trace, errors, stats'),
])
def test_windows_rows_and_bad_inputs_are_refused_before_any_call(store: Store, args, why):
    # given
    http = fake()

    # when
    text, err = lf_box(store, http).call('langfuse', args)

    # then
    assert err and why in text, text
    assert http.calls == []


def test_one_trace_comes_back_as_a_tree_with_cut_and_redacted_texts_and_no_customer_ids(store: Store):
    # given
    b = lf_box(store, fake())

    # when
    out, err = call(b, op='trace', id='t1')
    steps = {s['id']: s for s in out['observations']}

    # then
    assert not err
    assert out['trace']['hasUser'] is True and 'cust-1001' not in json.dumps(out) and 'sess-77' not in json.dumps(out)
    assert [(s['id'], s['depth']) for s in out['observations']] == [('root', 0), ('gen', 1), ('tool', 2)]
    assert steps['gen'] | {'input': None, 'output': None} == {'id': 'gen', 'type': 'GENERATION', 'name': 'llm', 'depth': 1, 'start': '2026-11-11T07:00:00.100Z', 'latencyS': 2.5,
                                                              'model': 'gpt-5.4-mini', 'tokens': {'input': 5200, 'output': 310, 'total': 5510}, 'costUsd': 0.010123, 'input': None, 'output': None}
    assert steps['gen']['input'].startswith('\u2026') and steps['gen']['input'].endswith('my phone is ' + REDACTED) and len(steps['gen']['input']) <= client.IO_LIMIT + 20
    assert ALICE not in json.dumps(out) and 'write to ' + REDACTED in steps['gen']['input']
    assert len(steps['gen']['output']) == client.IO_LIMIT and steps['gen']['output'].endswith('\u2026')
    assert (steps['tool']['level'], steps['tool']['statusMessage'], steps['tool']['input']) == ('ERROR', 'Connection reset by peer', '{"amount": 50000}')
    assert steps['root']['latencyS'] == 4.2 and 'level' not in steps['root']
    assert out['trace'] | {'input': None} == {'id': 't1', 'name': 'chat-turn', 'at': '2026-11-11T07:00:00.000Z', 'latencyS': 4.2, 'costUsd': 0.010123, 'tags': ['web'], 'hasUser': True,
                                              'output': 'Sure, here are the plans', 'input': None}, 'the app root with an outside parent heads the trace'


def test_errors_group_failing_steps_by_name_and_message_with_samples_and_stats_sum_tokens_by_model(store: Store):
    # given
    b = lf_box(store, fake())

    # when
    errs, err = call(b, op='errors', hours=6)
    week, week_err = call(b, op='stats', days=2)

    # then
    assert not err and not week_err
    assert [(g['level'], g['name'], g['statusMessage'], g['count']) for g in errs['groups']] == [('ERROR', 'search_products', 'Connection reset by peer', 2), ('ERROR', 'chat', 'rate limited', 1),
                                                                                              ('WARNING', 'product-cards', 'second block dropped', 1)]
    assert errs['groups'][0] | {'model': None} == {'level': 'ERROR', 'name': 'search_products', 'type': 'TOOL', 'statusMessage': 'Connection reset by peer', 'count': 2,
                                                    'first': '2026-11-11T06:10:00Z', 'last': '2026-11-11T07:00:01Z', 'traces': ['t1', 't4'], 'model': None}
    assert errs['window'] == {'from': iso(TOOL_NOW - 6 * 3600), 'to': iso(TOOL_NOW)} and errs['observations'] == 4
    assert [d['date'] for d in week['days']] == ['2026-11-10', '2026-11-11'] and week['days'][0]['traces'] == 120
    assert week['byModel'][0] == {'model': 'gpt-5.4-mini', 'input': 140000, 'output': 12000, 'total': 152000, 'costUsd': 1.7}


def test_the_keys_go_only_in_the_auth_header_and_never_in_an_answer_or_error(store: Store):
    # given
    http = fake()
    refused = FakeHttp({('GET', OBSERVATIONS): HttpError(401, OBSERVATIONS, f'{{"message": "invalid key {SK}"}}')})
    no_instance = call(lf_box(store, FakeHttp(), instances={}))
    b = lf_box(store, http, run_id='run-1')

    # when
    out, err = call(b)
    denied = call(lf_box(store, refused))
    capped = [call(b)[1] for _ in range(langfuse.PLUGIN.tools[0].per_run)]

    # then
    assert not err and http.calls[0][2]['Authorization'] == basic(PK, SK)
    assert no_instance == ('no tool named langfuse', True)
    assert denied == (f'Langfuse HTTP 401 on /api/public/v2/observations: {NO_ACCESS}', True)
    assert capped == [False] * (langfuse.PLUGIN.tools[0].per_run - 1) + [True]
    assert SK not in json.dumps([out, denied, b.calls, list(store.activity.find({}, {'_id': 0}))], default=str) and PK not in json.dumps(out)


def test_a_server_error_body_reaches_the_tool_with_the_instance_keys_masked(store: Store):
    # given
    said = FakeHttp({('GET', OBSERVATIONS): HttpError(400, OBSERVATIONS, f'{{"message": "Invalid filter for key {SK} and {PK}"}}')})

    # when
    text, err = call(lf_box(store, said), op='errors')

    # then
    assert err and text == 'Langfuse HTTP 400 on /api/public/v2/observations: Invalid filter for key *** and ***'


def test_the_environment_field_narrows_every_read(store: Store):
    # given
    http = fake()
    b = lf_box(store, http, instances={'main': dict(VALUES, environment='production')})
    env_metric = {'column': 'environment', 'operator': '=', 'value': 'production', 'type': 'string'}
    env_list = {'column': 'environment', 'operator': 'any of', 'value': ['production'], 'type': 'stringOptions'}

    # when
    call(b, limit=3)
    call(b, name='chat-turn', limit=3)
    call(b, op='errors')
    call(b, op='stats', days=2)

    # then
    asked = [json.loads(parse_qs(urlsplit(c[1]).query)['query'][0]) for c in http.hits(METRICS)]
    assert asked and all(env_metric in q['filters'] for q in asked)
    plain = [c[1] for c in http.hits(OBSERVATIONS) if 'traceId=' not in c[1] and 'filter=' not in c[1]]
    assert plain and all('environment=production' in u for u in plain)
    narrowed = [json.loads(parse_qs(urlsplit(c[1]).query)['filter'][0]) for c in http.hits(OBSERVATIONS) if f'name={TURN_NAME}' in c[1] and 'filter=' in c[1]]
    assert narrowed and all(env_list in f and TURN in f for f in narrowed), 'a filter array carries the turn and the environment itself'


def test_without_a_turn_name_every_trace_root_is_a_turn(store: Store):
    # given
    http = fake()
    b = lf_box(store, http, instances={'main': dict(VALUES, **{'turn-name': ''})})

    # when
    listed, err = call(b, limit=2)

    # then
    assert not err and [r['id'] for r in listed['traces']] == ['t1', 't2']
    roots = [c[1] for c in http.hits(OBSERVATIONS) if 'isRootObservation=true' in c[1]]
    assert roots and not any('name=' in u for u in roots)
    counted = [json.loads(parse_qs(urlsplit(c[1]).query)['query'][0]) for c in http.hits(METRICS)]
    assert ROOT in counted[0]['filters'] and TURN not in counted[0]['filters']


def test_the_tool_reads_the_instance_it_is_asked_for(store: Store):
    # given
    http = fake()
    http.routes.update(fake(EAST_URL).routes)
    b = lf_box(store, http, instances={'main': VALUES, 'east': dict(VALUES, url=EAST_URL)})

    # when
    east, east_err = call(b, op='errors', instance='east')
    main, main_err = call(b, op='errors', instance='main')

    # then
    assert not east_err and not main_err
    assert east['from'].startswith('Langfuse instance east:') and main['from'].startswith('Langfuse instance main:')
    hosts = [urlsplit(c[1]).hostname for c in http.calls]
    assert hosts[:2] == ['langfuse-east.example.com'] * 2 and hosts[2:] == ['langfuse.example.com'] * 2


def test_a_metrics_row_key_that_echoes_the_secret_is_masked_in_the_tool_error(store: Store):
    # given
    echoed = FakeHttp({('GET', METRICS): {'data': [{f'count_{SK}': 1, 'echo': PK}]}})

    # when
    text, err = call(lf_box(store, echoed), op='stats')
    blind = langfuse.run(Ctx(echoed)).result

    # then
    assert err and text == 'Langfuse metrics answered without count_count, sum_totalCost, time_dimension; a row holds count_***, echo'
    assert SK not in text and blind.status == 'error: metrics: Langfuse metrics answered without count_count, p95_latency'


def test_the_app_hands_its_langfuse_instance_to_the_investigator(store: Store, tmp_path):
    # given
    http = fake()

    # when
    with app_with(store, tmp_path, http) as c:
        tools = c.app.state.investigator.sources.plugin_tools
        offered = [d['name'] for d in tools.definitions()]
        listed_now = tools.call('langfuse', {'op': 'errors'}, TOOL_NOW)

    # then
    assert 'langfuse' in offered and tools.cap('langfuse') == 8 and tools.scan('langfuse')
    assert listed_now['groups'][0]['name'] == 'search_products'


NOW = parse_iso('2026-11-11T09:00:00Z')
DAY = 86400
CHECK_VALUES = dict(VALUES, namespace='shop', service='shop-assistant')


def which(start: str) -> str:
    at = parse_iso(start)
    return '1d' if abs(at - (NOW - DAY - 3600)) < 60 else '7d' if abs(at - (NOW - 7 * DAY - 3600)) < 60 else 'now'


def today_cost() -> dict:
    return {str(dt.datetime.fromtimestamp(NOW, dt.timezone.utc).date()): 1.2}


def api(windows: dict, days: dict | None = None, steps: list | None = None, url: str = URL) -> FakeHttp:
    def metrics_answer(q, _b):
        asked = json.loads(q['query'][0])
        if asked.get('timeDimension'):
            return {'data': [{'time_dimension': f'{d}T00:00:00.000Z', 'sum_totalCost': c} for d, c in (today_cost() if days is None else days).items()]}
        w = windows[which(asked['fromTimestamp'])]
        if [d['field'] for d in asked['dimensions']] == ['level']:
            return {'data': [{'level': k, 'count_count': str(v)} for k, v in w['errors'].items()] + ([{'level': 'DEFAULT', 'count_count': str(w.get('plain', 900))}] if w.get('plain', 900) else [])}
        if asked['filters'] in ([TURN], [ROOT]):
            return {'data': [{'count_count': str(w['traces']), 'p95_latency': w['latency'] * 1000}]}
        return {'data': [{'sum_totalCost': w['cost'] * w['traces']}]}

    def observations_answer(q, _b):
        level = q.get('level', [None])[0]
        return {'data': [o for o in steps or [] if o['level'] == level], 'meta': {}}

    return FakeHttp({('GET', f'{url}/api/public/v2/metrics'): metrics_answer, ('GET', f'{url}/api/public/v2/observations'): observations_answer})


def calm(**over) -> dict:
    return {'traces': 120, 'errors': {'ERROR': 2, 'WARNING': 1}, 'latency': 6.0, 'cost': 0.01} | over


def empty() -> dict:
    return {'traces': 0, 'errors': {'ERROR': 0, 'WARNING': 0}, 'latency': 0.0, 'cost': 0.0, 'plain': 0}


@dataclass
class Ctx:
    transport: object
    config: dict = field(default_factory=lambda: dict(CHECK_VALUES))
    now: float = NOW
    name: str = 'main'
    rules_given: dict = field(default_factory=dict)
    cards: list = field(default_factory=list)

    @property
    def instance(self) -> InstanceInfo:
        return InstanceInfo(self.name, True, None, self.config)

    def http(self):
        return self.transport

    def rules(self, set_id: str) -> dict:
        assert set_id == 'rules'
        return {**check.RULES, **self.rules_given}

    def open_cards(self, plugin_wide: bool = False) -> list[dict]:
        return list(self.cards)


def found(http: FakeHttp, rules: dict | None = None, config: dict | None = None):
    sweep = langfuse.run(Ctx(http, dict(CHECK_VALUES) if config is None else config, rules_given=rules or {}))
    return sweep, {a['sig']: a for a in sweep.result.found}


def test_a_turn_is_the_named_step_when_turn_name_is_set_and_the_logical_root_otherwise():
    # given
    named = api({'now': calm(traces=19), '1d': calm(traces=0), '7d': calm(traces=0)})
    rooted = api({'now': calm(traces=19), '1d': calm(traces=0), '7d': calm(traces=0)})

    # when
    sweep, _ = found(named)
    found(rooted, config=dict(CHECK_VALUES, **{'turn-name': ''}))
    asked = [json.loads(parse_qs(urlsplit(c[1]).query)['query'][0]) for c in named.hits(METRICS)]
    asked_rooted = [json.loads(parse_qs(urlsplit(c[1]).query)['query'][0]) for c in rooted.hits(METRICS)]

    # then
    assert not any(f.get('column') == 'isRootObservation' for q in asked for f in q['filters'])
    assert sum(TURN in q['filters'] for q in asked) == 3
    assert sum(ROOT in q['filters'] for q in asked_rooted) == 3 and not any(TURN in q['filters'] for q in asked_rooted)
    row = sweep.sections['langfuseCheck'][0]
    assert (row['part'], row['turns'], row['steps']) == ('summary', 19, 903)


def test_a_calm_hour_raises_nothing_and_the_pass_reads_the_three_windows():
    # given
    http = api({'now': calm(), '1d': calm(), '7d': calm()})

    # when
    sweep, cards = found(http)
    result = sweep.result

    # then
    assert result.name == 'langfuse' and result.ok and result.status == 'ok' and cards == {} and result.ran == ('langfuse', ACCESS_M, ACCESS_O)
    assert sweep.sections['langfuseCheck'] == [{'part': 'summary', 'at': '2026-11-11T09:00:00Z', 'turns': 120, 'steps': 903, 'errorSteps': {'ERROR': 2, 'WARNING': 1}, 'p95S': 6.0,
                                                'turnCostUsd': 0.01, 'turns1d': 120, 'turns7d': 120}]
    assert {c[1].split('?')[0].rsplit('/', 1)[-1] for c in http.calls} == {'metrics', 'observations'}
    assert all('/api/public/v2/' in c[1] for c in http.calls)
    assert all(c[2]['Authorization'] == basic(PK, SK) for c in http.calls)


def test_error_steps_jumping_raise_a_card_that_names_the_top_failing_steps_without_customer_data():
    # given
    steps = [{'level': 'ERROR', 'name': 'search_products', 'statusMessage': 'tool timeout after 30 s for +15550100199', 'traceId': 'a'}] * 4 + \
            [{'level': 'WARNING', 'name': 'product-cards', 'statusMessage': 'block dropped', 'traceId': 'b'}]
    http = api({'now': calm(errors={'ERROR': 40, 'WARNING': 5}), '1d': calm(), '7d': calm()}, steps=steps)

    # when
    _, cards = found(http)

    # then
    card = cards['lf-errors']
    assert card['key'] == 'lf-errors|shop/shop-assistant' and card['sev'] == 'crit' and (card['now'], card['d1'], card['d7']) == (45, 3, 3)
    assert 'ERROR search_products \u00d74: tool timeout after 30 s' in card['what'] and 'WARNING product-cards \u00d71' in card['what']
    assert '+15550100199' not in card['what'] and REDACTED in card['what']
    assert card['what'].startswith('Langfuse counted 45 error / warning steps in the shop-assistant traces in the hour to 11 Nov 09:00 UTC (errors 40, warnings 5)')
    assert card['short'] == 'CRIT shop-assistant 45 error steps in the last hour (1d 3 / 7d 3)'
    assert (card['src'], card['ns'], card['svc'], card['label']) == ('langfuse', 'shop', 'shop-assistant', 'step errors')
    assert 'cluster' not in card and 'plugin' not in card
    assert all(ord(ch) < 128 for a in cards.values() for ch in a['what'].replace('\u00d7', '') + a['short'])


def test_traffic_falling_to_almost_nothing_is_critical_and_a_quiet_night_is_left_alone():
    # given
    outage = api({'now': calm(traces=2), '1d': calm(traces=150), '7d': calm(traces=140)})
    night = api({'now': calm(traces=1), '1d': calm(traces=8), '7d': calm(traces=6)})

    # when
    _, down = found(outage)
    _, quiet = found(night)

    # then
    assert down['lf-traffic']['sev'] == 'crit' and down['lf-traffic']['short'].startswith('CRIT shop-assistant 2 traces in the last hour (1d 150 / 7d 140)')
    assert 'lf-traffic' not in quiet


def test_slower_turns_and_dearer_turns_raise_warnings_with_their_numbers():
    # given
    http = api({'now': calm(latency=22.0, cost=0.05), '1d': calm(latency=7.0, cost=0.012), '7d': calm(latency=6.5, cost=0.011)})

    # when
    _, cards = found(http)

    # then
    assert (cards['lf-latency']['now'], cards['lf-latency']['d1'], cards['lf-latency']['unit']) == (22.0, 7.0, 's')
    assert cards['lf-turn-cost']['short'] == 'WARN shop-assistant $0.0500 per turn in the last hour (1d $0.0120 / 7d $0.0110)'


def test_yesterday_costing_far_more_than_the_week_before_raises_one_card_per_day():
    # given
    today = dt.datetime.fromtimestamp(NOW, dt.timezone.utc).date()
    days = {str(today - dt.timedelta(days=1)): 42.0} | {str(today - dt.timedelta(days=k)): 10.0 for k in range(2, 9)}
    http = api({'now': calm(), '1d': calm(), '7d': calm()}, days=days)

    # when
    _, cards = found(http)

    # then
    card = cards['lf-day-cost']
    assert card['key'] == f'lf-day-cost|shop/shop-assistant|{today - dt.timedelta(days=1)}'
    assert (card['now'], card['d1']) == (42.0, 10.0)


def test_the_rules_move_the_thresholds_a_failed_read_fails_the_pass_without_the_keys_and_cards_link_langfuse():
    # given
    http = api({'now': calm(errors={'ERROR': 9, 'WARNING': 0}), '1d': calm(), '7d': calm()})
    down = FakeHttp({})
    info = InstanceInfo('main', True, None, {'url': URL + '/'})

    # when
    _, loose_cards = found(http)
    _, strict_cards = found(http, {'errors_warn_times': 2, 'errors_floor': 5})
    failed = langfuse.run(Ctx(down)).result

    # then
    assert 'lf-errors' not in loose_cards and strict_cards['lf-errors']['sev'] == 'high'
    assert not failed.ok and failed.status == 'error: metrics: Langfuse HTTP 404 on /api/public/v2/metrics: not found'
    assert SK not in failed.status and PK not in failed.status
    assert langfuse.links({'src': 'langfuse'}, info) == [{'label': 'Langfuse traces', 'url': URL + '/'}] and langfuse.links({'src': 'airbyte'}, info) is None


def test_a_metrics_answer_without_the_asked_numbers_fails_the_pass_instead_of_reading_zero_and_a_refused_query_names_its_code(store: Store):
    # given
    renamed = FakeHttp({('GET', METRICS): {'data': [{'countCount': 120, 'p95Latency': 6000}]}})
    refused = FakeHttp({('GET', METRICS): HttpError(400, 'x', '{"message": "Invalid filter column isRootObservation", "error": "InvalidRequestError"}')})

    # when
    blind = langfuse.run(Ctx(renamed)).result
    bad = langfuse.run(Ctx(refused)).result
    told_blind = call(lf_box(store, renamed), op='stats')
    told_bad = call(lf_box(store, refused), op='stats')

    # then
    assert not blind.ok and blind.status == 'error: metrics: Langfuse metrics answered without count_count, p95_latency'
    assert not bad.ok and bad.status == 'error: metrics: Langfuse HTTP 400 on /api/public/v2/metrics'
    assert told_blind == ('Langfuse metrics answered without count_count, sum_totalCost, time_dimension; a row holds countCount, p95Latency', True)
    assert told_bad == ('Langfuse HTTP 400 on /api/public/v2/metrics: Invalid filter column isRootObservation', True)


def test_a_langfuse_with_nothing_traced_is_ok_with_no_data_and_closes_no_card(store: Store, tmp_path):
    # given
    steps = [{'level': 'ERROR', 'name': 'search_products', 'statusMessage': 'tool timeout', 'traceId': 'a'}]
    busy = api({'now': calm(errors={'ERROR': 40, 'WARNING': 5}), '1d': calm(), '7d': calm()}, steps=steps)
    silent = api({'now': empty(), '1d': empty(), '7d': empty()}, days={})

    # when
    with app_with(store, tmp_path, busy) as c:
        c.app.state.sweeper.run('langfuse', 'langfuse', NOW)
        before = by_key(store.load())
        busy.routes.update(silent.routes)
        c.app.state.sweeper.run('langfuse', 'langfuse', NOW + 900)
    state = store.load()
    quiet = langfuse.run(Ctx(silent)).result

    # then
    assert before['lf-errors|-/langfuse']['status'] == 'open'
    assert (quiet.ok, quiet.status, quiet.ran, quiet.found) == (True, 'ok \u00b7 no data', ('langfuse:no-data', ACCESS_M), [])
    assert state['sources']['langfuse'] == 'ok \u00b7 no data'
    assert by_key(state)['lf-errors|-/langfuse']['status'] == 'open', 'a pass that read nothing closes no card (P5-C82)'


def test_an_empty_part_is_named_in_the_status_and_the_pass_still_clears():
    # given
    no_roots = api({'now': calm(traces=0), '1d': calm(traces=0), '7d': calm(traces=0)})
    no_days = api({'now': calm(), '1d': calm(), '7d': calm()}, days={})

    # when
    turns = langfuse.run(Ctx(no_roots, dict(CHECK_VALUES, **{'turn-name': ''}))).result
    days = langfuse.run(Ctx(no_days)).result

    # then
    assert (turns.status, turns.ran) == ('ok \u00b7 no data for turns', ('langfuse', ACCESS_M, ACCESS_O)), 'a one-ran-key plug-in clears on a partly empty pass (P5-C109)'
    assert (days.status, days.ran) == ('ok \u00b7 no data for daily costs', ('langfuse', ACCESS_M, ACCESS_O))


def test_a_refused_metrics_read_keeps_the_cards_open_and_raises_an_access_card_until_access_returns(store: Store, tmp_path):
    # given
    slow = api({'now': calm(latency=22.0), '1d': calm(latency=7.0), '7d': calm(latency=6.5)})
    readable = dict(slow.routes)
    forbidden = HttpError(403, METRICS, '{"message": "forbidden"}')

    # when
    with app_with(store, tmp_path, slow) as c:
        c.app.state.sweeper.run('langfuse', 'langfuse', NOW)
        slow.routes[('GET', METRICS)] = forbidden
        c.app.state.sweeper.run('langfuse', 'langfuse', NOW)
        refused = store.load()
        slow.routes.update(readable)
        c.app.state.sweeper.run('langfuse', 'langfuse', NOW)
    back = store.load()
    unit = langfuse.run(Ctx(FakeHttp({('GET', METRICS): HttpError(401, METRICS)}))).result

    # then
    cards = by_key(refused)
    access = cards['no-access|-/langfuse|metrics']
    assert refused['sources']['langfuse'] == 'ok \u00b7 no access to metrics'
    assert cards['lf-latency|-/langfuse']['status'] == 'open', 'a refused read is never read as none: its cards stay open (P5-C111)'
    assert (access['status'], access['sev'], access['ranKey'], access['label']) == ('open', 'warn', ACCESS_M, f'Langfuse cannot read metrics: {NO_ACCESS}')
    assert 'HTTP 403' in access['what'] and access['short'] == f'WARN Langfuse cannot read metrics: {NO_ACCESS}'
    assert by_key(back)['no-access|-/langfuse|metrics']['status'] != 'open' and back['sources']['langfuse'] == 'ok'
    assert (unit.ok, unit.status, unit.ran) == (True, 'ok \u00b7 no access to metrics', (ACCESS_M,)) and [a['sig'] for a in unit.found] == ['no-access']
    assert 'HTTP 401' in unit.found[0]['what'] and unit.found[0]['label'] == 'Langfuse cannot read metrics: the keys are invalid or lack read access to this project'


def test_a_refused_observations_read_still_counts_the_errors_and_raises_its_own_access_card():
    # given
    http = api({'now': calm(errors={'ERROR': 40, 'WARNING': 5}), '1d': calm(), '7d': calm()})
    http.routes[('GET', OBSERVATIONS)] = HttpError(403, OBSERVATIONS, '{"message": "forbidden"}')

    # when
    sweep, cards = found(http)

    # then
    assert (sweep.result.status, sweep.result.ran) == ('ok \u00b7 no access to observations', ('langfuse', ACCESS_M, ACCESS_O))
    assert cards['lf-errors']['sev'] == 'crit' and f'Top steps: not readable, {NO_ACCESS}' in cards['lf-errors']['what']
    assert (cards['no-access']['key'], cards['no-access']['ranKey']) == ('no-access|-/langfuse|observations', ACCESS_O)
    assert cards['no-access']['label'] == f'Langfuse cannot read observations: {NO_ACCESS}'


def test_an_observations_access_card_clears_on_the_first_calm_pass_after_access_returns(store: Store, tmp_path):
    # given
    steps = [{'level': 'ERROR', 'name': 'search_products', 'statusMessage': 'tool timeout', 'traceId': 'a'}]
    busy = api({'now': calm(errors={'ERROR': 40, 'WARNING': 5}), '1d': calm(), '7d': calm()}, steps=steps)
    busy.routes[('GET', OBSERVATIONS)] = HttpError(403, OBSERVATIONS, '{"message": "forbidden"}')
    quiet = api({'now': calm(errors={'ERROR': 0, 'WARNING': 0}), '1d': calm(), '7d': calm()})

    # when
    with app_with(store, tmp_path, busy) as c:
        c.app.state.sweeper.run('langfuse', 'langfuse', NOW)
        lost = by_key(store.load())
        busy.routes.clear()
        busy.routes.update(quiet.routes)
        first = len(busy.calls)
        c.app.state.sweeper.run('langfuse', 'langfuse', NOW)
        back = store.load()
        probes = [u for _, u, _, _ in busy.calls[first:] if u.split('?')[0] == OBSERVATIONS]
        c.app.state.sweeper.run('langfuse', 'langfuse', NOW)
        later = [u for _, u, _, _ in busy.calls[first:] if u.split('?')[0] == OBSERVATIONS]

    # then
    assert lost['no-access|-/langfuse|observations']['status'] == 'open' and lost['lf-errors|-/langfuse']['status'] == 'open'
    assert by_key(back)['no-access|-/langfuse|observations']['status'] != 'open', 'the first calm pass reads one observation and clears the card (P5-C111)'
    assert back['sources']['langfuse'] == 'ok'
    assert len(probes) == 1 and 'limit=1' in probes[0] and 'level=' not in probes[0]
    assert later == probes, 'with no open access card a calm pass reads no observation'


def test_an_open_observations_access_card_is_probed_and_kept_while_still_refused():
    # given
    http = api({'now': calm(errors={'ERROR': 0, 'WARNING': 0}), '1d': calm(), '7d': calm()})
    http.routes[('GET', OBSERVATIONS)] = HttpError(401, OBSERVATIONS)
    held = [{'key': 'no-access|-/langfuse|observations', 'src': 'langfuse', 'sig': 'no-access', 'sub': 'observations', 'ranKey': ACCESS_O, 'status': 'open'}]

    # when
    refused = langfuse.run(Ctx(http, cards=held)).result
    unprobed = langfuse.run(Ctx(http)).result

    # then
    assert (refused.status, refused.ran) == ('ok \u00b7 no access to observations', ('langfuse', ACCESS_M, ACCESS_O))
    assert [a['key'] for a in refused.found] == ['no-access|-/langfuse|observations']
    assert (unprobed.status, unprobed.ran) == ('ok', ('langfuse', ACCESS_M))


def test_an_idle_hour_is_plain_no_data_whether_or_not_a_turn_name_is_set():
    # given
    idle = api({'now': empty(), '1d': empty(), '7d': empty()})

    # when
    named = langfuse.run(Ctx(idle)).result
    rooted = langfuse.run(Ctx(idle, dict(CHECK_VALUES, **{'turn-name': ''}))).result

    # then
    assert (named.status, named.ran) == ('ok \u00b7 no data', ('langfuse:no-data', ACCESS_M)), 'no observation at all is plain no data and names no turn (P5-C109a)'
    assert (rooted.status, rooted.ran) == ('ok \u00b7 no data', ('langfuse:no-data', ACCESS_M))


def test_a_refused_tool_read_names_the_missing_permission(store: Store):
    # given
    forbidden = FakeHttp({('GET', METRICS): HttpError(403, METRICS, '{"message": "forbidden"}')})

    # when
    text, err = call(lf_box(store, forbidden), op='stats')

    # then
    assert (text, err) == (f'Langfuse HTTP 403 on /api/public/v2/metrics: {NO_ACCESS}: forbidden', True)


def test_a_turn_name_that_matches_no_observation_closes_no_card(store: Store, tmp_path):
    # given
    slow = api({'now': calm(latency=22.0), '1d': calm(latency=7.0), '7d': calm(latency=6.5)})
    steps = [{'level': 'ERROR', 'name': 'search_products', 'statusMessage': 'tool timeout', 'traceId': 'a'}]
    renamed = api({'now': calm(traces=0, errors={'ERROR': 40, 'WARNING': 5}), '1d': calm(traces=0), '7d': calm(traces=0)}, steps=steps)
    both = api({'now': calm(traces=0), '1d': calm(traces=0), '7d': calm(traces=0)}, days={})

    # when
    with app_with(store, tmp_path, slow) as c:
        c.app.state.sweeper.run('langfuse', 'langfuse', NOW)
        before = by_key(store.load())
        slow.routes.update(renamed.routes)
        c.app.state.sweeper.run('langfuse', 'langfuse', NOW)
    state = store.load()
    after = by_key(state)
    quiet = langfuse.run(Ctx(renamed)).result
    with_days = langfuse.run(Ctx(both)).result

    # then
    assert before['lf-latency|-/langfuse']['status'] == 'open'
    assert state['sources']['langfuse'] == f'ok \u00b7 no data for {TURN_NAME}'
    assert (quiet.ok, quiet.status, quiet.ran) == (True, f'ok \u00b7 no data for {TURN_NAME}', ('langfuse:no-data', ACCESS_M, ACCESS_O))
    assert after['lf-latency|-/langfuse']['status'] == 'open', 'a turn name that matches nothing closes no card (P5-C109)'
    assert after['lf-errors|-/langfuse']['status'] == 'open', 'the steps were read, so a step-error spike still opens its card'
    assert with_days.status == f'ok \u00b7 no data for {TURN_NAME}, daily costs' and with_days.ran == ('langfuse:no-data', ACCESS_M, ACCESS_O)


def test_a_pass_that_outruns_its_budget_stops_with_a_fixed_reason(monkeypatch):
    # given
    http = api({'now': calm(), '1d': calm(), '7d': calm()})
    clock = [1000.0]
    timeouts: list[float] = []

    def slow(method, url, headers, data, timeout):
        timeouts.append(round(timeout, 3))
        clock[0] += 0.15
        return http(method, url, headers, data, timeout)

    monkeypatch.setattr(check, 'BUDGET_S', 0.4)
    monkeypatch.setattr(client, 'now', lambda: clock[0])

    # when
    sweep = langfuse.run(Ctx(slow))

    # then
    assert (sweep.result.ok, sweep.result.status) == (False, 'error: metrics: Langfuse reads stopped after 3 calls: the 0.4 s budget is spent')
    assert timeouts == [0.4, 0.25, 0.1] and sweep.sections == {}
    assert check.BUDGET_S < 240


def test_cards_take_the_service_and_namespace_fields_with_neutral_fallbacks():
    # given
    http = api({'now': calm(traces=2), '1d': calm(traces=150), '7d': calm(traces=140)})

    # when
    _, plain = found(http, config=dict(VALUES))
    _, cleared = found(http, config=dict(VALUES, service='', namespace=''))

    # then
    assert plain['lf-traffic']['key'] == cleared['lf-traffic']['key'] == 'lf-traffic|-/langfuse'


def test_pass_table_offers_the_langfuse_summary_while_an_instance_serves(store: Store):
    # given
    rows = [{'part': 'summary', 'at': '2026-11-11T09:00:00Z', 'turns': 120, 'plugin': 'langfuse'}]
    store.save_state({**state_of(), 'serverPasses': {'langfuse': {'at': '2026-11-11T09:00:00Z', 'status': 'ok'}}, 'langfuseCheck': rows}, None)
    b = lf_box(store, fake())

    # when
    tool = next(t for t in b.definitions() if t['name'] == 'pass_table')
    out, err = b.call('pass_table', {'source': 'langfuseCheck'})

    # then
    assert 'langfuseCheck' in tool['input_schema']['properties']['source']['enum']
    assert not err and json.loads(out) == {'source': 'langfuseCheck', 'passes': {'langfuse': {'at': '2026-11-11T09:00:00Z', 'status': 'ok'}}, 'rows': rows}


def app_with(store, tmp_path, http, instances: dict | None = None):
    sealing = master_key(store.db, settings())
    for i, (name, values) in enumerate(({'main': VALUES} if instances is None else instances).items()):
        add_instance(store, langfuse.PLUGIN, name, sealer_key=sealing, primary=i == 0, **values)
    return TestClient(create_app(settings(), store, registry=Registry.of(langfuse.PLUGIN), transport=http, kube=Kube(root=tmp_path)))


def by_key(state: dict) -> dict:
    return {a['key']: a for a in state['anomalies']}


def test_the_app_runs_the_langfuse_check_once_an_instance_serves(store: Store, tmp_path):
    # given
    steps = [{'level': 'ERROR', 'name': 'search_products', 'statusMessage': 'tool timeout', 'traceId': 'a'}]
    http = api({'now': calm(errors={'ERROR': 40, 'WARNING': 5}), '1d': calm(), '7d': calm()}, steps=steps)

    # when
    with app_with(store, tmp_path, http) as c:
        jobs = {j.name: j for j in c.app.state.scheduler.jobs}
        tiles = {p['id']: p for p in c.get('/api/server').json()['plugins']}
        c.app.state.sweeper.run('langfuse', 'langfuse', NOW)
    state = store.load()
    card = by_key(state)['lf-errors|-/langfuse']

    # then
    assert (jobs['langfuse.langfuse'].period, jobs['langfuse.langfuse'].offset) == (900, 810)
    assert tiles['langfuse']['passes'] == ['langfuse'] and tiles['langfuse']['pages'] == []
    assert state['sources']['langfuse'] == 'ok' and card['status'] == 'open' and card['links'] == [{'label': 'Langfuse traces', 'url': URL + '/'}]
    assert [(r['part'], r['plugin'], 'cluster' in r) for r in state['langfuseCheck']] == [('summary', 'langfuse', False)]


def test_without_an_instance_no_langfuse_job_is_registered(store: Store, tmp_path):
    # when
    with app_with(store, tmp_path, fake(), instances={}) as c:
        names = [j.name for j in c.app.state.scheduler.jobs]

    # then
    assert 'langfuse.langfuse' not in names


def test_langfuse_identity_and_manifest():
    # given
    registry = Registry.load({}, entry_points=False, core_tools=CORE_TOOLS, core_knowledge=prompt.core_anchors())

    # when
    got = registry.get('langfuse')
    manifest = describe(langfuse.PLUGIN, icon=True)
    fields = {f.key: f for f in langfuse.PLUGIN.fields}
    probe, tool, rules = langfuse.PLUGIN.checks[0], langfuse.PLUGIN.tools[0], langfuse.PLUGIN.rules[0]
    read = langfuse.PLUGIN.agent_reads[0]

    # then
    assert 'skopax.plugins.langfuse' in BUILTIN and got is not None and got.state == 'ok' and got.plugin is langfuse.PLUGIN
    assert json.loads(json.dumps(manifest, allow_nan=False)) == manifest
    assert (manifest['id'], manifest['name'], manifest['version'], manifest['category'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition']) == (
        'langfuse', 'Langfuse', '0.1.0', 'other', 'Skopax', 'Apache-2.0', '>=0.1,<0.6', 'community')
    assert manifest['capabilities'] == {'hosts': ['@url'], 'secrets': ['secret-key', 'langfuse.secret-key'], 'agentReads': True} and manifest['uses'] == []
    assert manifest['agentReads'] == [{'op': 'langfuse', 'args': ['ask'], 'keys': [r'langfuse\.(url|public-key|secret-key)']}]
    assert [(f.key, f.kind, f.required, f.ui) for f in fields.values()] == [('url', 'url', True, True), ('public-key', 'text', True, False), ('secret-key', 'secret', True, False),
                                                                             ('environment', 'text', False, False), ('namespace', 'text', False, False), ('service', 'text', False, False),
                                                                             ('turn-name', 'text', False, False), ('ca', 'pem', False, False), ('ca-pinned', 'bool', False, False)]
    assert (probe.id, probe.period, probe.offset, probe.ran, probe.sections, probe.tool, probe.once) == ('langfuse', 900, 810, ('langfuse',), ('langfuseCheck',), 'langfuse', False)
    assert probe.section_help['langfuseCheck']
    assert (rules.id, rules.label, set(rules.help) == set(rules.defaults)) == ('rules', 'Langfuse', True)
    assert (tool.name, tool.kind, tool.per_run, tool.scan, tool.instance_input, tool.reach) == ('langfuse', 'read', 8, True, 'instance', ())
    assert (read.op, read.args, read.timeout) == ('langfuse', ('ask',), 75)
    assert manifest['panels'] == [] and registry.icon('langfuse').startswith('<svg')
    assert [k.id for k in langfuse.PLUGIN.knowledge] == ['langfuse'] and len(langfuse.PLUGIN.knowledge[0].text.splitlines()) <= 40
    assert langfuse.PLUGIN.client is None and langfuse.PLUGIN.max_instances is None


def test_langfuse_defaults_are_neutral():
    # then
    assert {f.key: f.default for f in langfuse.PLUGIN.fields} == {'url': 'https://cloud.langfuse.com', 'public-key': '', 'secret-key': '', 'environment': '', 'namespace': '',
                                                                  'service': 'langfuse', 'turn-name': '', 'ca': '', 'ca-pinned': 'false'}
    assert {f.key: f.default for f in langfuse.PLUGIN.agent_reads[0].fields} == {'langfuse.url': '', 'langfuse.public-key': '', 'langfuse.secret-key': ''}
    assert {s.key: s.default for s in langfuse.PLUGIN.settings} == {}
    assert dict(langfuse.PLUGIN.rules[0].defaults) == {'errors_warn_times': 3, 'errors_crit_times': 10, 'errors_gap': 5, 'errors_floor': 5, 'errors_crit_floor': 20,
                                                       'traffic_warn': 0.3, 'traffic_crit': 0.05, 'traffic_floor': 20,
                                                       'latency_times': 1.5, 'latency_gap_s': 5, 'latency_min_turns': 20,
                                                       'turn_cost_times': 1.6, 'turn_cost_gap_usd': 0.01, 'turn_cost_min_turns': 20,
                                                       'day_cost_times': 1.5, 'day_cost_gap_usd': 5, 'day_cost_floor_usd': 5}


def test_langfuse_connection_test_reports_ok_and_failures(store):
    # given
    projects = f'{URL}/api/public/projects'
    no_metrics, no_observations, bad_key = fake(), fake(), fake()
    no_metrics.routes[('GET', METRICS)] = HttpError(403, METRICS, '{"message": "forbidden"}')
    no_observations.routes[('GET', OBSERVATIONS)] = HttpError(403, OBSERVATIONS, '{"message": "forbidden"}')
    bad_key.routes[('GET', OBSERVATIONS)] = HttpError(401, OBSERVATIONS, '{"message": "unauthorized"}')
    cases = {'ok': fake(), 'refused': FakeHttp({('GET', projects): HttpError(401, projects)}), 'unreachable': FakeHttp({('GET', projects): remote.Unreachable('down')}),
             'none': FakeHttp({('GET', projects): {'data': []}}), 'no-metrics': no_metrics, 'no-observations': no_observations,
             'bad-key': bad_key}

    # when
    got = {}
    for name, http in cases.items():
        got[name] = tool_runtime(store, http, instances={}).connection_test(langfuse.PLUGIN, None, dict(VALUES), 5, NOW)

    # then
    assert (got['ok']['ok'], got['ok']['summary']) == (True, 'Langfuse project Shop')
    assert (got['refused']['ok'], got['refused']['summary']) == (False, msg('connection.rejected', status=401))
    assert (got['unreachable']['ok'], got['unreachable']['summary']) == (False, msg('connection.unreachable'))
    assert (got['none']['ok'], got['none']['summary']) == (False, 'GET /api/public/projects answered without a project')
    assert (got['no-metrics']['ok'], got['no-metrics']['summary']) == (False, f'Langfuse refused the metrics read (HTTP 403): {NO_ACCESS}')
    assert (got['no-observations']['ok'], got['no-observations']['summary']) == (False, f'Langfuse refused the observations read (HTTP 403): {NO_ACCESS}')
    assert (got['bad-key']['ok'], got['bad-key']['summary']) == (False, 'Langfuse refused the observations read (HTTP 401): the keys are invalid or lack read access to this project'), \
        'the projects call worked with the same keys, so a later 401 is two-sided (P5-C127)'
    asked = cases['ok'].hits(projects)
    assert len(asked) == 1 and asked[0][2]['Authorization'] == basic(PK, SK)
    assert SK not in json.dumps(got)


def test_langfuse_connection_test_fits_inside_core_wait(store):
    # given
    http = fake()
    timeouts: list[float] = []

    def timed(method, url, headers, data, timeout):
        timeouts.append(timeout)
        return http(method, url, headers, data, timeout)

    # when
    got = tool_runtime(store, timed, instances={}).connection_test(langfuse.PLUGIN, None, dict(VALUES), 15, NOW)

    # then
    assert got['ok'] and len(timeouts) == 3 and all(0 < t <= langfuse.TEST_BUDGET_S < 15 for t in timeouts)


def test_langfuse_custom_ca_reaches_the_transport_and_the_probe_target(store, monkeypatch):
    # given
    cert = pem()
    runtime = tool_runtime(store, None, instances={'main': dict(VALUES, ca=cert)})
    real, seen = ssl.create_default_context, []
    monkeypatch.setattr(ssl, 'create_default_context', lambda *a, **k: seen.append(k.get('cadata')) or real(*a, **k))

    # when
    ctx = runtime.context(langfuse.PLUGIN, runtime.instances.get('langfuse', 'main'), NOW)
    sender = ctx.http()
    targets = runtime.probe_targets()

    # then
    assert seen == [cert] and sender is not remote.send
    assert [(t['name'], t.get('pem')) for t in targets] == [('langfuse-main', cert)]


def test_langfuse_second_instance_is_scoped(store: Store, tmp_path):
    # given
    steps = [{'level': 'ERROR', 'name': 'search_products', 'statusMessage': 'tool timeout', 'traceId': 'a'}]
    http = api({'now': calm(), '1d': calm(), '7d': calm()})
    http.routes.update(api({'now': calm(errors={'ERROR': 40, 'WARNING': 5}), '1d': calm(), '7d': calm()}, steps=steps, url=EAST_URL).routes)

    # when
    with app_with(store, tmp_path, http, instances={'main': VALUES, 'east': dict(VALUES, url=EAST_URL)}) as c:
        c.app.state.sweeper.run('langfuse', 'langfuse', NOW)
    state = store.load()
    east = by_key(state)['lf-errors|-/langfuse@east']

    # then
    assert (east['cluster'], east['ranKey']) == ('east', 'langfuse-east')
    assert state['sources']['langfuse-east'] == 'ok' and state['sources']['langfuse'] == 'ok'
    assert east['links'] == [{'label': 'Langfuse traces', 'url': EAST_URL + '/'}]
    assert sorted((r.get('cluster') or '', r['part']) for r in state['langfuseCheck']) == [('', 'summary'), ('east', 'summary')]


def test_two_instances_on_the_default_service_keep_their_own_cards_and_a_service_change_moves_only_east(store: Store, tmp_path):
    # given
    steps = [{'level': 'ERROR', 'name': 'search_products', 'statusMessage': 'tool timeout', 'traceId': 'a'}]
    http = api({'now': calm(errors={'ERROR': 40, 'WARNING': 5}), '1d': calm(), '7d': calm()}, steps=steps)
    http.routes.update(api({'now': calm(errors={'ERROR': 30, 'WARNING': 5}), '1d': calm(), '7d': calm()}, steps=steps, url=EAST_URL).routes)

    # when
    with app_with(store, tmp_path, http, instances={'main': VALUES, 'east': dict(VALUES, url=EAST_URL)}) as c:
        c.app.state.sweeper.run('langfuse', 'langfuse', NOW)
        both = sorted((a['key'], a.get('cluster') or '', a['now']) for a in store.load()['anomalies'])
        c.app.state.runtime.instances.update('langfuse', 'east', {'service': 'east-assistant'}, ALICE)
        c.app.state.sweeper.run('langfuse', 'langfuse', NOW)
        renamed = sorted((a['key'], a.get('cluster') or '', a['status']) for a in store.load()['anomalies'])

    # then
    assert both == [('lf-errors|-/langfuse', '', 45), ('lf-errors|-/langfuse@east', 'east', 35)], 'two instances never share a card, even on the same service'
    assert renamed == [('lf-errors|-/east-assistant@east', 'east', 'open'), ('lf-errors|-/langfuse', '', 'open'), ('lf-errors|-/langfuse@east', 'east', 'cleared')]


def test_langfuse_rules_come_from_the_rule_set(store: Store, tmp_path):
    # given
    http = api({'now': calm(errors={'ERROR': 9, 'WARNING': 0}), '1d': calm(), '7d': calm()})

    # when
    with app_with(store, tmp_path, http) as c:
        c.app.state.sweeper.run('langfuse', 'langfuse', NOW)
        before = set(by_key(store.load()))
        c.app.state.runtime.live.set('langfuse.rules', '{"errors_warn_times": 2}', ALICE)
        c.app.state.sweeper.run('langfuse', 'langfuse', NOW)
        after = by_key(store.load())

    # then
    assert 'lf-errors|-/langfuse' not in before
    assert after['lf-errors|-/langfuse']['status'] == 'open' and after['lf-errors|-/langfuse']['sev'] == 'high'


def test_langfuse_probe_target_is_the_public_health_endpoint_of_each_instance(store):
    # given
    runtime = tool_runtime(store, fake(), instances={'main': VALUES, 'east': dict(VALUES, url=EAST_URL + '/')})

    # when
    targets = sorted(runtime.probe_targets(), key=lambda t: t['name'])

    # then
    assert [(t['name'], t['url'], t['instance']) for t in targets] == [('langfuse-east', f'{EAST_URL}/api/public/health', 'east'), ('langfuse-main', f'{URL}/api/public/health', 'main')]


AGENT_VALUES = {'langfuse.url': AGENT_URL, 'langfuse.public-key': PK_AGENT, 'langfuse.secret-key': SK_AGENT}
EAST_AGENT = {'mode': 'agent', 'url': 'https://east.example.com', 'token': 'east-token-0002'}


def test_agent_read_keys_match_v1():
    # given
    read = langfuse.PLUGIN.agent_reads[0]
    v1 = ('langfuse.url', 'langfuse.public-key', 'langfuse.secret-key')

    # then
    assert read.keys == (r'langfuse\.(url|public-key|secret-key)',)
    assert tuple(f.key for f in read.fields) == v1 and [f.kind for f in read.fields] == ['url', 'text', 'secret']
    assert all(any(re.fullmatch(k, key) for k in read.keys) for key in v1)
    assert not any(re.fullmatch(k, key) for k in read.keys for key in ('langfuse.environment', 'langfuse.url2', 'elastic.logging-url'))


def test_the_agent_read_is_on_with_the_url_and_both_keys_and_refuses_a_plain_http_url_by_name(monkeypatch):
    # given
    http = FakeHttp({})
    monkeypatch.setattr(remote, 'send', http)
    plain_config = dict(AGENT_VALUES, **{'langfuse.url': 'http://langfuse.monitoring.svc:3000'})
    agent = agent_client(agent_boot(values=plain_config, plugins=(langfuse,)))

    # when
    with pytest.raises(ReadError) as off:
        langfuse.agent_langfuse({'langfuse.public-key': PK_AGENT, 'langfuse.secret-key': SK_AGENT}, {'ask': {}})
    with pytest.raises(ReadError) as plain:
        langfuse.agent_langfuse(plain_config, {'ask': {}})
    told = agent.post('/api/read', json={'op': 'langfuse', 'args': {'ask': {'op': 'errors'}}}, headers=AUTH)

    # then
    assert langfuse.agent_sources(AGENT_VALUES) == ['langfuse'] and langfuse.agent_sources(plain_config) == ['langfuse']
    assert [langfuse.agent_sources({k: v for k, v in AGENT_VALUES.items() if k != gone}) for gone in AGENT_VALUES] == [[], [], []]
    assert off.value.status == 503 and 'langfuse.url' in off.value.message
    assert (plain.value.status, plain.value.message) == (503, msg('langfuse.agent_https'))
    assert (told.status_code, told.json()['error']) == (503, msg('langfuse.agent_https')), 'the agent names the https need instead of the generic read-off text'
    assert 'https' in msg('langfuse.agent_https') and http.calls == [], 'the keys never go to a plain http address'


def test_the_agent_reads_langfuse_with_its_own_keys(monkeypatch):
    # given
    http = fake(AGENT_URL)
    monkeypatch.setattr(remote, 'send', http)
    agent = agent_client(agent_boot(values=AGENT_VALUES, plugins=(langfuse,)))
    bare = agent_client(agent_boot(values={}, plugins=(langfuse,)))

    # when
    out = agent.post('/api/read', json={'op': 'langfuse', 'args': {'ask': {'op': 'errors', 'hours': 2}}}, headers=AUTH)
    info = agent.get('/api/agent', headers=AUTH).json()
    off = bare.post('/api/read', json={'op': 'langfuse', 'args': {'ask': {}}}, headers=AUTH)

    # then
    data = out.json()['data']
    assert out.status_code == 200 and data['from'].startswith('Langfuse through the agent:') and data['groups'][0]['name'] == 'search_products'
    assert http.calls and all(c[1].startswith(AGENT_URL + '/api/public/v2/observations') for c in http.calls)
    assert http.calls[0][2]['Authorization'] == basic(PK_AGENT, SK_AGENT)
    assert info['reads']['langfuse'] == ['langfuse'] and bare.get('/api/agent', headers=AUTH).json()['reads']['langfuse'] == []
    assert off.status_code == 503 and off.json()['error'] == msg('agent.read_off', op='langfuse')
    assert SK_AGENT not in json.dumps([data, info, off.json()])


@pytest.mark.parametrize('args, status, said', [
    ({'ask': {'op': 'delete'}}, 422, 'op is one of traces, trace, errors, stats'),
    ({'ask': {'hours': 73}}, 422, 'hours must be a whole number from 1 to 72'),
    ({'ask': 'traces'}, 422, 'ask must be an object'),
    ({'ask': {'env': 'test'}}, 422, 'langfuse takes only op, hours, days, limit, name, user_id, session_id, tags, id, not env'),
    ({'ask': {}, 'cluster': 'logs'}, 422, 'takes only ask'),
])
def test_the_agent_refuses_langfuse_reads_it_cannot_take(monkeypatch, args, status, said):
    # given
    http = fake(AGENT_URL)
    monkeypatch.setattr(remote, 'send', http)
    agent = agent_client(agent_boot(values=AGENT_VALUES, plugins=(langfuse,)))

    # when
    r = agent.post('/api/read', json={'op': 'langfuse', 'args': args}, headers=AUTH)

    # then
    assert r.status_code == status and said in r.json()['error'], r.json()
    assert http.calls == []


def test_a_langfuse_error_through_the_agent_comes_back_with_the_agent_keys_masked(monkeypatch):
    # given
    path = f'{AGENT_URL}/api/public/v2/observations'
    monkeypatch.setattr(remote, 'send', FakeHttp({('GET', path): HttpError(400, path, f'{{"message": "bad filter for {SK_AGENT}"}}')}))
    agent = agent_client(agent_boot(values=AGENT_VALUES, plugins=(langfuse,)))

    # when
    r = agent.post('/api/read', json={'op': 'langfuse', 'args': {'ask': {'op': 'errors'}}}, headers=AUTH)

    # then
    assert (r.status_code, r.json()['error']) == (400, 'Langfuse HTTP 400 on /api/public/v2/observations: bad filter for ***')


def test_the_main_pushes_the_agent_instance_keys_and_reads_langfuse_through_the_agent(store, monkeypatch):
    # given
    http = fake(AGENT_URL)
    monkeypatch.setattr(remote, 'send', http)
    far = agent_app('east-token-0002', plugins=(langfuse,))
    seen = Seen()
    runtime = kube_runtime(store, {'east': EAST_AGENT | AGENT_VALUES, 'bare': EAST_AGENT | {'url': 'https://bare.example.com'}}, plugins=(langfuse.PLUGIN,),
                           transport=app_transport({'east.example.com': far, 'bare.example.com': agent_app('east-token-0002', plugins=(langfuse,))}, seen))
    fields = {f.key: f for f in runtime.agent_fields('kubernetes')}

    # when
    out = runtime.agent_read('east', 'langfuse', {'ask': {'op': 'errors', 'hours': 2}})
    with pytest.raises(ReadError) as off:
        runtime.agent_read('bare', 'langfuse', {'ask': {}})

    # then
    assert out['from'].startswith('Langfuse through the agent:') and out['groups'][0]['name'] == 'search_products'
    pushes = [json.loads(c[4]) for c in seen.calls if c[0] == 'PUT']
    assert [p['config'] for p in pushes] == [AGENT_VALUES]
    assert all(c[1].startswith(AGENT_URL) for c in http.calls) and http.calls[0][2]['Authorization'] == basic(PK_AGENT, SK_AGENT)
    assert off.value.status == 503
    assert [(fields[k].kind, fields[k].required, fields[k].show_if) for k in AGENT_VALUES] == [('url', False, ('mode', 'agent')), ('text', False, ('mode', 'agent')),
                                                                                           ('secret', False, ('mode', 'agent'))]
    assert SK_AGENT not in json.dumps(list(runtime.instances.col.find({}, {'_id': 0})), default=str)


def test_a_failed_observations_listing_still_judges_the_metrics_and_names_the_part():
    # given
    http = api({'now': calm(errors={'ERROR': 40, 'WARNING': 5}), '1d': calm(), '7d': calm()})
    http.routes[('GET', OBSERVATIONS)] = HttpError(500, OBSERVATIONS)

    # when
    sweep, cards = found(http)

    # then
    assert (sweep.result.ok, sweep.result.status) == (True, 'error: observations: Langfuse HTTP 500 on /api/public/v2/observations')
    assert sweep.result.ran == ('langfuse', ACCESS_M), 'the failed part keeps its access key out (P5-C128)'
    assert cards['lf-errors']['sev'] == 'crit' and 'Top steps: not read on this pass' in cards['lf-errors']['what']


def test_the_langfuse_status_names_empty_parts_before_refused_ones():
    # given
    http = api({'now': calm(errors={'ERROR': 40, 'WARNING': 5}), '1d': calm(), '7d': calm()}, days={})
    http.routes[('GET', OBSERVATIONS)] = HttpError(403, OBSERVATIONS, '{"message": "forbidden"}')

    # when
    sweep, _ = found(http)

    # then
    assert sweep.result.status == 'ok \u00b7 no data for daily costs \u00b7 no access to observations'


def test_the_basic_header_a_server_echoes_is_masked_too(store: Store):
    # given
    basic = base64.b64encode(f'{PK}:{SK}'.encode()).decode()
    said = FakeHttp({('GET', OBSERVATIONS): HttpError(400, OBSERVATIONS, f'{{"message": "bad header Basic {basic}"}}')})

    # when
    text, err = call(lf_box(store, said), op='errors')

    # then
    assert err and basic not in text and text.endswith('bad header Basic ***')


def test_the_connection_test_reads_metrics_with_the_turn_filter_the_check_uses(store):
    # given
    http = fake()

    # when
    tested = tool_runtime(store, http, instances={}).connection_test(langfuse.PLUGIN, None, dict(VALUES, **{'turn-name': ''}), 5, NOW)
    asked = [json.loads(parse_qs(urlsplit(c[1]).query)['query'][0]) for c in http.hits(METRICS)]

    # then
    assert tested['ok'] and asked and ROOT in asked[0]['filters'], 'a Langfuse that does not know isRootObservation fails at Test, not at every pass'


@pytest.mark.parametrize('traced', [True, False])
def test_the_instance_that_receives_skopax_traces_is_not_watched_while_tracing_is_on(store: Store, tmp_path, traced: bool):
    # given
    steps = [{'level': 'ERROR', 'name': 'search_products', 'statusMessage': 'tool timeout', 'traceId': 'a'}]
    http = api({'now': calm(errors={'ERROR': 40, 'WARNING': 5}), '1d': calm(), '7d': calm()}, steps=steps)
    http.routes.update(api({'now': calm(errors={'ERROR': 30, 'WARNING': 5}), '1d': calm(), '7d': calm()}, steps=steps, url=EAST_URL).routes)

    # when
    with app_with(store, tmp_path, http, instances={'main': VALUES, 'east': dict(VALUES, url=EAST_URL)}) as c:
        picked = c.put('/api/admin/settings', json={'key': 'langfuse.trace-instance', 'value': 'east'}, headers=ADMIN)
        switched = c.put('/api/admin/features/langfuse-tracing', json={'on': traced}, headers=ADMIN)
        c.app.state.sweeper.run('langfuse', 'langfuse', NOW)
    keys = sorted(a['key'] for a in store.load()['anomalies'])
    sources = store.load()['sources']
    east_reads = [u for _, u, _, _ in http.calls if u.startswith(EAST_URL)]

    # then
    assert (picked.status_code, switched.status_code) == (200, 200)
    if traced:
        assert keys == ['lf-errors|-/langfuse'] and east_reads == []
        assert (sources['langfuse-east'], sources['langfuse']) == (langfuse.TRACED_STATUS, 'ok') and langfuse.TRACED_STATUS == "Receives Skopax's own traces, so it is not watched"
    else:
        assert keys == ['lf-errors|-/langfuse', 'lf-errors|-/langfuse@east'] and east_reads and sources['langfuse-east'] == 'ok'


def test_a_skipped_traced_instance_also_settles_its_access_cards():
    # given
    ctx = NS(instance=NS(name='east', config={}), setting=lambda key: 'east' if key == 'traced-instance' else '')

    # when
    swept = langfuse.run(ctx)

    # then
    assert swept.result.found == [] and swept.result.status == langfuse.TRACED_STATUS
    assert set(swept.result.ran) == {'langfuse', 'langfuse:access:metrics', 'langfuse:access:observations'}


def test_an_open_card_of_the_instance_closes_once_it_receives_skopax_traces(store: Store, tmp_path):
    # given
    steps = [{'level': 'ERROR', 'name': 'search_products', 'statusMessage': 'tool timeout', 'traceId': 'a'}]
    http = api({'now': calm(), '1d': calm(), '7d': calm()})
    http.routes.update(api({'now': calm(errors={'ERROR': 30, 'WARNING': 5}), '1d': calm(), '7d': calm()}, steps=steps, url=EAST_URL).routes)

    with app_with(store, tmp_path, http, instances={'main': VALUES, 'east': dict(VALUES, url=EAST_URL)}) as c:
        c.app.state.sweeper.run('langfuse', 'langfuse', NOW)
        before = by_key(store.load())['lf-errors|-/langfuse@east']['status']

        # when
        c.put('/api/admin/settings', json={'key': 'langfuse.trace-instance', 'value': 'east'}, headers=ADMIN)
        c.put('/api/admin/features/langfuse-tracing', json={'on': True}, headers=ADMIN)
        c.app.state.sweeper.run('langfuse', 'langfuse', NOW + 900)
    after = by_key(store.load())['lf-errors|-/langfuse@east']['status']

    # then
    assert (before, after) == ('open', 'cleared')
