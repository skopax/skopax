import dataclasses
import datetime as dt
import time

from fastapi.testclient import TestClient

from skopax import tags, toolusage
from skopax.app import create_app
from skopax.messages import msg
from skopax.plugins import prometheus
from skopax.store import Store, iso, utc_now
from tests.conftest import settings
from tests.plugin_kit import add_instance, prom_tools
from tests.test_investigator_tools import FakeProm

SINCE = '2026-10-22T00:00:00Z'


def prom():
    return prom_tools(FakeProm([]))


def run(run_id: str, at: str, trigger: str, *calls: tuple) -> dict:
    return {'_id': run_id, 'startedAt': at, 'trigger': trigger,
            'toolCalls': [{'name': name, 'ms': ms, 'error': error, 'truncated': truncated} for name, ms, error, truncated in calls]}


def test_each_tool_gets_calls_runs_errors_times_and_run_types():
    # given
    runs = [
        run('r3', '2026-10-28T10:00:00Z', 'slack', ('promql', 900, 'esql refused: no @timestamp bound', False), ('card', 1, None, False)),
        run('r2', '2026-10-27T10:00:00Z', 'cards', ('promql', 100, None, True), ('promql', 300, None, False), ('note', 12, None, False)),
        run('r1', '2026-10-26T10:00:00Z', 'cards', ('promql', 200, 'timeout after 60 s', False)),
        run('r0', '2026-10-25T10:00:00Z', 'ask'),
    ]

    # when
    out = toolusage.summary(runs, 7, SINCE, None, prom())

    # then
    esql = next(t for t in out['tools'] if t['name'] == 'promql')
    assert (esql['kind'], esql['calls'], esql['runs'], esql['errors'], esql['errorRate'], esql['truncated']) == ('read', 4, 3, 2, 0.5, 1)
    assert (esql['p50Ms'], esql['p95Ms'], esql['maxMs'], esql['lastAt']) == (300, 900, 900, '2026-10-28T10:00:00Z')
    assert esql['byTrigger'] == {'slack': 1, 'cards': 3}
    assert [e['runId'] for e in esql['recentErrors']] == ['r3', 'r1']
    assert esql['recentErrors'][0] == {'at': '2026-10-28T10:00:00Z', 'runId': 'r3', 'trigger': 'slack', 'error': 'esql refused: no @timestamp bound'}
    note = next(t for t in out['tools'] if t['name'] == 'note')
    assert (note['kind'], note['calls'], note['errorRate']) == ('write', 1, 0.0)
    assert (out['runs'], out['runsWithTools'], out['calls'], out['errors'], out['toolsUsed']) == (4, 3, 6, 2, 3)
    assert out['days'] == 7 and out['since'] == SINCE


def test_every_defined_tool_is_listed_and_unused_ones_come_last_with_empty_numbers():
    # given
    runs = [run('r1', '2026-10-28T10:00:00Z', 'cards', ('promql', 10, None, False), ('promql', 20, None, False), ('card', 1, None, False))]

    # when
    out = toolusage.summary(runs, 1, SINCE, None, prom())

    # then
    names = [t['name'] for t in out['tools']]
    assert names[:2] == ['promql', 'card']
    assert set(names) == set(toolusage.catalog(prom().catalog())) and out['toolsDefined'] == len(toolusage.catalog(prom().catalog()))
    edge = next(t for t in out['tools'] if t['name'] == 'jira_search')
    assert edge == {'name': 'jira_search', 'kind': 'read', 'calls': 0, 'runs': 0, 'errors': 0, 'errorRate': None, 'truncated': 0,
                    'p50Ms': None, 'p95Ms': None, 'maxMs': None, 'lastAt': None, 'byTrigger': {}, 'recentErrors': [],
                    'held': 0, 'recentHeld': [], 'refused': 0, 'recentRefused': [], 'description': toolusage.descriptions()['jira_search']}
    unused = [t['name'] for t in out['tools'] if t['calls'] == 0]
    assert unused == sorted(unused) and names[-len(unused):] == unused
    assert {t['kind'] for t in out['tools']} == {'read', 'write', 'propose'}


def test_a_tool_no_longer_defined_is_kept_as_gone_and_only_three_recent_errors_are_kept():
    # given
    runs = [run(f'r{i}', f'2026-10-2{8 - i}T10:00:00Z', 'cards', ('old_tool', 5, f'boom {i}', False)) for i in range(5)]

    # when
    out = toolusage.summary(runs, 7, SINCE)

    # then
    old = next(t for t in out['tools'] if t['name'] == 'old_tool')
    assert old['kind'] == 'gone' and old['errors'] == 5
    assert [e['error'] for e in old['recentErrors']] == ['boom 0', 'boom 1', 'boom 2']


def test_the_endpoint_reads_runs_inside_the_window_only(store: Store, client: TestClient):
    # given
    now = utc_now()
    store.add_investigation({'id': 'new', 'trigger': 'cards', 'startedAt': iso(now - dt.timedelta(hours=2)), 'outcome': 'done',
                             'toolCalls': [{'name': 'esql', 'input': '{"query": "FROM x"}', 'rows': 3, 'truncated': False, 'error': None, 'ms': 40}]})
    store.add_investigation({'id': 'old', 'trigger': 'cards', 'startedAt': iso(now - dt.timedelta(days=3)), 'outcome': 'done',
                             'toolCalls': [{'name': 'esql', 'input': '{}', 'rows': None, 'truncated': False, 'error': 'refused', 'ms': 5}]})

    # when
    day = client.get('/api/tool-usage?days=1')
    week = client.get('/api/tool-usage')

    # then
    assert day.status_code == 200
    esql_day = next(t for t in day.json()['tools'] if t['name'] == 'esql')
    esql_week = next(t for t in week.json()['tools'] if t['name'] == 'esql')
    assert (esql_day['calls'], esql_day['errors'], day.json()['runs']) == (1, 0, 1)
    assert (esql_week['calls'], esql_week['errors'], week.json()['days']) == (2, 1, 7)
    assert 'input' not in str(week.json())


def test_the_window_is_one_to_thirty_days(client: TestClient):
    # then
    assert client.get('/api/tool-usage?days=0').status_code == 422
    assert client.get('/api/tool-usage?days=31').status_code == 422
    assert client.get('/api/tool-usage?days=30').status_code == 200


def test_the_endpoint_puts_the_status_on_read_tools_only(store: Store, client: TestClient):
    # given
    store.save_probe({'at': iso(utc_now()), 'targets': [{'name': 'status', 'plugin': 'status', 'instance': 'main', 'url': 'https://status.example.com/actuator/health', 'ok': False,
                                                         'error': 'dns: gaierror'}]})

    # when
    tools = {t['name']: t for t in client.get('/api/tool-usage').json()['tools']}

    # then
    assert tools['card']['status'] == {'state': 'on', 'reason': None}
    assert 'status' not in tools['note']


def test_a_plugin_tool_is_unreachable_when_the_probe_cut_off_what_it_reaches(store: Store):
    # given
    at = iso(utc_now())
    target = {'name': 'prometheus-main', 'plugin': 'prometheus', 'instance': 'main', 'url': 'https://prom.example.com/-/ready', 'status': None}
    add_instance(store, prometheus.PLUGIN, 'main', primary=True, url='https://prom.example.com')

    # when
    with TestClient(create_app(dataclasses.replace(settings(), probe=True), store)) as c:
        deadline = time.monotonic() + 3
        while store.last_probe() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        store.save_probe({'at': at, 'targets': [{**target, 'ok': False, 'error': 'dns: gaierror'}]})
        cut = {t['name']: t for t in c.get('/api/tool-usage').json()['tools']}
        store.save_probe({'at': at, 'targets': [{**target, 'ok': True, 'status': 200, 'error': None}]})
        fine = {t['name']: t for t in c.get('/api/tool-usage').json()['tools']}

    # then
    assert cut['promql']['status'] == {'state': 'unreachable', 'reason': msg('tools.unreachable', targets='prometheus-main'), 'at': at}
    assert 'prom.example.com' not in cut['promql']['status']['reason'] and 'gaierror' not in cut['promql']['status']['reason']
    assert fine['promql']['status'] == {'state': 'on', 'reason': None}


def test_every_tool_row_carries_what_the_investigator_is_told_and_a_removed_tool_none():
    # given
    runs = [run('r1', '2026-10-28T10:00:00Z', 'cards', ('promql', 900, None, False), ('old_tool', 5, None, False))]

    # when
    tools = {t['name']: t for t in toolusage.summary(runs, 1, SINCE, None, prom())['tools']}

    # then
    assert 'give range_minutes for a range query' in tools['promql']['description']
    assert tools['note']['description'].startswith("Replace the card's investigation note")
    assert tools['old_tool']['kind'] == 'gone' and 'description' not in tools['old_tool']


class Primary:
    def __init__(self, name):
        self.name = name

    def catalog(self):
        return {'kube': 'read'}

    def descriptions(self):
        return {}

    def check_tools(self):
        return {}

    def default_instance(self, tool):
        return self.name if tool == 'kube' else None


def kube_runs():
    return [{'_id': 'r2', 'startedAt': '2026-11-09T06:00:00Z', 'trigger': 'cards', 'toolCalls': [
                {'name': 'kube', 'ms': 80, 'cluster': 'east', 'op': 'daemonset', 'error': None},
                {'name': 'kube', 'ms': 15000, 'cluster': 'east', 'op': 'nodes', 'error': 'the east agent is unreachable: timed out'},
                {'name': 'kube', 'ms': 40, 'cluster': 'local', 'op': 'pods', 'error': None}]},
            {'_id': 'r1', 'startedAt': '2026-11-01T06:00:00Z', 'trigger': 'slack', 'toolCalls': [{'name': 'kube', 'ms': 30, 'error': None}, {'name': 'esql', 'ms': 9, 'error': None}]}]


def test_kube_calls_are_split_by_cluster_with_their_errors_and_older_calls_count_as_the_primary_instance():
    # when
    out = {t['name']: t for t in toolusage.summary(kube_runs(), 30, SINCE, None, Primary('local'))['tools']}
    other = {t['name']: t for t in toolusage.summary(kube_runs(), 30, SINCE, None, Primary('main'))['tools']}

    # then
    assert out['kube']['byCluster'] == {'east': {'calls': 2, 'errors': 1}, 'local': {'calls': 2, 'errors': 0}}
    assert other['kube']['byCluster'] == {'east': {'calls': 2, 'errors': 1}, 'local': {'calls': 1, 'errors': 0}, 'main': {'calls': 1, 'errors': 0}}
    assert 'byCluster' not in out['esql']


def test_older_kube_calls_without_a_known_primary_never_count_as_prod():
    # when
    out = {t['name']: t for t in toolusage.summary(kube_runs(), 30, SINCE)['tools']}

    # then
    assert out['kube']['byCluster'] == {'east': {'calls': 2, 'errors': 1}, 'local': {'calls': 1, 'errors': 0}, 'primary': {'calls': 1, 'errors': 0}}
    assert 'prod' not in out['kube']['byCluster']


def test_scheduled_checks_count_as_calls_of_the_tool_for_their_source():
    # given
    runs = [run('r1', '2026-10-23T10:00:00Z', 'cards', ('promql', 300, None, False))]
    checks = [{'name': 'prom', 'ok': True, 'ms': 900, 'at': '2026-10-23T10:15:00Z'},
              {'name': 'prom', 'ok': False, 'ms': 50, 'error': 'error: timed out', 'at': '2026-10-23T10:30:00Z'},
              {'name': 'certs', 'ok': True, 'ms': 10, 'at': '2026-10-23T10:15:00Z'}]

    # when
    out = toolusage.summary(runs, 7, SINCE, checks, prom())
    by = {t['name']: t for t in out['tools']}

    # then
    assert (out['calls'], out['checkRuns'], out['runs']) == (3, 2, 1), 'certs reads no tool source and is left out'
    assert (by['promql']['calls'], by['promql']['runs'], by['promql']['errors'], by['promql']['byTrigger']) == (3, 1, 1, {'cards': 1, 'check': 2})
    assert by['promql']['byCheck'] == {'prom': {'runs': 2, 'errors': 1}}
    assert by['promql']['recentErrors'] == [{'at': '2026-10-23T10:30:00Z', 'check': 'prom', 'trigger': 'check', 'error': 'error: timed out'}]
    assert by['promql']['lastAt'] == '2026-10-23T10:30:00Z'


def test_every_check_maps_to_a_tool_the_investigator_has():
    # then
    assert set(toolusage.CHECK_TOOLS.values()) <= set(toolusage.catalog())


def test_a_check_run_is_recorded_when_its_results_are_applied_and_the_endpoint_adds_it(store: Store, client: TestClient):
    # given
    store.add_check_runs([{'name': 'prom', 'ok': True, 'ms': 1200}, {'name': 'umami', 'ok': False, 'ms': 300, 'error': 'error: HTTP 503'}])
    store.add_check_runs([{'name': 'prom', 'ok': True, 'ms': 900}], at=utc_now() - dt.timedelta(days=3))

    # when
    day = client.get('/api/tool-usage?days=1').json()
    week = client.get('/api/tool-usage').json()

    # then
    air_day = next(t for t in day['tools'] if t['name'] == 'promql')
    umami = next(t for t in day['tools'] if t['name'] == 'umami')
    assert (air_day['calls'], air_day['byTrigger'], day['checkRuns']) == (1, {'check': 1}, 2)
    assert (umami['errors'], umami['recentErrors'][0]['error'], umami['recentErrors'][0]['check']) == (1, 'error: HTTP 503', 'umami')
    assert next(t for t in week['tools'] if t['name'] == 'promql')['calls'] == 2


def test_the_tag_tool_is_described_only_when_tags_are_set(monkeypatch):
    # given
    with_tags = toolusage.descriptions()
    monkeypatch.setattr(tags, 'TAGS', {})

    # when
    without = toolusage.descriptions()

    # then
    assert 'tag' in with_tags and 'tag' not in without
    assert toolusage.catalog()['tag'] == 'write'
    assert '{group}' not in ' '.join(without.values())


def test_a_bridged_mcp_tool_reads_as_an_outside_tool_not_a_removed_one(store: Store, client: TestClient):
    # given
    store.investigations.insert_one(run('r1', iso(utc_now()), 'cards', ('mcp_main_search', 40, None, False)))
    runs = [run('r2', '2026-10-28T10:00:00Z', 'cards', ('mcp_docs_search_files', 5, None, False), ('old_tool', 5, None, False))]

    # when
    out = {t['name']: t for t in toolusage.summary(runs, 7, SINCE)['tools']}
    served = {t['name']: t for t in client.get('/api/tool-usage').json()['tools']}

    # then
    assert out['mcp_docs_search_files']['kind'] == 'mcp' and out['old_tool']['kind'] == 'gone'
    assert served['mcp_main_search']['kind'] == 'mcp' and 'status' not in served['mcp_main_search']


def test_an_mcp_name_the_model_invented_is_not_counted_as_an_mcp_tool():
    # given
    from skopax.investigator.tools import NO_TOOL
    runs = [run('r1', '2026-10-28T10:00:00Z', 'cards', ('mcp_main_made_up', 5, NO_TOOL.format(name='mcp_main_made_up'), False),
                ('mcp_main_search', 5, NO_TOOL.format(name='mcp_main_search'), False), ('mcp_main_search', 5, None, False))]

    # when
    out = toolusage.summary(runs, 7, SINCE)
    kinds = {t['name']: t['kind'] for t in out['tools']}

    # then
    assert kinds['mcp_main_made_up'] == 'gone', 'a name that was never offered is not an MCP server tool'
    assert kinds['mcp_main_search'] == 'mcp', 'one refused call among working ones keeps it an MCP server tool'
    assert out['toolsDefined'] == len(toolusage.catalog()) + 1


def test_an_invented_mcp_name_too_long_for_the_stored_error_still_counts_as_unknown():
    # given
    from skopax.investigator.tools import ERROR_LIMIT, NO_TOOL
    name = 'mcp_main_' + 'x' * 400
    runs = [run('r1', '2026-10-28T10:00:00Z', 'cards', (name, 5, NO_TOOL.format(name=name)[:ERROR_LIMIT], False))]

    # when
    out = toolusage.summary(runs, 7, SINCE)

    # then
    assert {t['name']: t['kind'] for t in out['tools']}[name] == 'gone'


def test_bridged_tools_count_as_defined_so_used_never_exceeds_defined():
    # given
    runs = [run('r1', '2026-10-28T10:00:00Z', 'cards', ('mcp_main_search', 5, None, False), ('mcp_main_lookup', 5, None, False))]

    # when
    out = toolusage.summary(runs, 7, SINCE)

    # then
    assert out['toolsUsed'] <= out['toolsDefined'] and out['toolsDefined'] == len(toolusage.catalog()) + 2
