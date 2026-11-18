import importlib.util
import json
import threading
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from skopax import clock, engine, remote, store as store_module
from skopax.app import create_app
from skopax.engine import iso
from skopax.messages import msg
from skopax.plugins import kubernetes
from skopax.plugins.kubernetes import nodes
from skopax.plugins.kubernetes.nodes import heartbeat, nodes_pass
from skopax.settingsources import FILE_ENV, boot
from skopax.sweeps import Sweeper
from tests.conftest import AUTH
from tests.kube_kit import FakeKube, kube_runtime

T0 = 1_762_670_000.0
LOCAL = {'mode': 'in-cluster'}
EAST = {'mode': 'agent', 'url': 'https://east.example.com', 'token': 'east-token-0002'}
HUB = {'mode': 'agent', 'url': 'https://hub.example.com', 'token': 'hub-token-0001'}
NODES = [{'name': 'node-1', 'ready': 'True'}, {'name': 'node-3', 'ready': 'False'}]
INFO = {'version': 'abc12345', 'release': '1.0', 'startedAt': '2026-11-09T06:00:00Z', 'kube': True,
        'cluster': {'total': 6, 'ready': 5, 'notReady': ['east-worker-3'], 'pressure': [], 'unschedulable': [], 'nodes': []}}
DOWN = remote.Unreachable('https://east.example.com/api/agent unreachable (timed out)')
SECRET = 'GET /api/v1/nodes with Bearer abcdefgh12345678 failed'


class Agent:
    def __init__(self, answers):
        self.answers = list(answers)
        self.asked: list[tuple[str, str]] = []

    def __call__(self, method, url, headers, data, timeout):
        self.asked.append((method, url))
        if not url.endswith('/api/agent'):
            raise remote.Unreachable(f'{url} unreachable (no route)')
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return remote.Reply(answer, {})


def runtime_with(store, answers, kube=None, instances=None):
    return kube_runtime(store, instances or {'local': LOCAL, 'east': EAST}, kube=kube if kube is not None else FakeKube(NODES), transport=Agent(answers))


def beats(runtime, times, primary='local') -> list[list[dict]]:
    plugin = runtime.registry.get('kubernetes').plugin
    out, prev = [], None
    for t in times:
        prev = heartbeat(runtime.context(plugin, runtime.instances.get('kubernetes', primary), t), prev)
        out.append(prev)
    return out


def sweeper_of(store, runtime) -> Sweeper:
    def apply(results, now, links, after=None, check=None, screen=None, clusters=None):
        return engine.apply(store, results, now, links, after=after, screen=screen, clusters=clusters)

    return Sweeper(runtime, store, apply)


def test_the_in_cluster_row_is_always_ready_with_its_own_cluster_and_agents_turn_ready_or_notready_keeping_since(store):
    # given
    runtime = runtime_with(store, [INFO, INFO, DOWN, DOWN, INFO])

    # when
    rounds = beats(runtime, [T0, T0 + 60, T0 + 120, T0 + 180, T0 + 240])

    # then
    master = rounds[0][0]
    assert (master['name'], master['role'], master['status'], master['via']) == ('local', 'master', 'Ready', 'in-cluster')
    assert master['clusterSummary']['total'] == 2 and master['clusterSummary']['notReady'] == ['node-3']
    east = [r[1] for r in rounds]
    assert [n['status'] for n in east] == ['Ready', 'Ready', 'NotReady', 'NotReady', 'Ready']
    assert east[1]['since'] == iso(T0) and east[3]['since'] == iso(T0 + 120) and east[4]['since'] == iso(T0 + 240)
    assert east[3]['lastSeen'] == iso(T0 + 60) and east[3]['stale'] is True and east[3]['clusterSummary']['notReady'] == ['east-worker-3']
    assert east[3]['error'] == msg('kubernetes.nodes.no_answer') and east[3]['version'] == 'abc12345'
    assert east[0]['release'] == '1.0' and east[3]['release'] == '1.0', 'a NotReady agent keeps its last release'
    assert all('cluster' not in r for r in rounds[-1]) and 'east-token-0002' not in json.dumps(rounds)


def test_an_agent_notready_for_five_minutes_raises_a_warn_card_that_turns_crit_after_half_an_hour_and_clears_when_ready():
    # given
    rows = lambda since, status='NotReady': [{'name': 'local', 'role': 'master', 'status': 'Ready'},
                                             {'name': 'east', 'role': 'agent', 'status': status, 'since': iso(since), 'lastSeen': iso(since - 60), 'error': msg('kubernetes.nodes.no_answer')}]

    # when
    early = nodes_pass(rows(T0 - 120), T0)
    warn = nodes_pass(rows(T0 - 600), T0)
    crit = nodes_pass(rows(T0 - 2400), T0)
    back = nodes_pass(rows(T0 - 60, 'Ready'), T0)

    # then
    assert early.found == [] and back.found == [] and early.ran == ('nodes:east',) and back.ran == ('nodes:east',)
    assert warn.found[0]['sev'] == 'warn' and warn.found[0]['key'] == 'node-not-ready|-/agent-east' and warn.found[0]['ranKey'] == 'nodes:east'
    assert 'not answered for 10 min' in warn.found[0]['what'] and 'cannot read pods, events, rollouts or nodes on east' in warn.found[0]['what'] and 'Prometheus' not in warn.found[0]['what']
    assert f'last heard from at {clock.hm(T0 - 660)} {clock.label(T0 - 660)}' in warn.found[0]['what']
    assert crit.found[0]['sev'] == 'crit'


def test_an_agent_that_never_answered_raises_no_card_until_it_has_answered_once():
    # given
    never = [{'name': 'local', 'role': 'master', 'status': 'Ready'},
             {'name': 'east-data', 'role': 'agent', 'status': 'NotReady', 'since': iso(T0 - 2400), 'lastSeen': None, 'error': msg('kubernetes.nodes.status', status=404)}]
    once = [never[0], {**never[1], 'lastSeen': iso(T0 - 2460)}]

    # when
    waiting = nodes_pass(never, T0)
    lost = nodes_pass(once, T0)

    # then
    assert waiting.found == [] and waiting.ran == ('nodes:east-data',), 'an agent added before it was installed is waiting for first contact, not down'
    assert lost.found[0]['sev'] == 'crit', 'once it has answered, losing it is a card as before'

    # when
    day = nodes_pass([never[0], {**never[1], 'since': iso(T0 - 25 * 3600)}], T0)

    # then
    assert [(f['sev'], f['key']) for f in day.found] == [('warn', 'node-not-ready|-/agent-east-data')], 'a day without first contact is a warn card, never crit'
    assert 'never answered' in day.found[0]['what'] and 'token matches' in day.found[0]['what']


@pytest.mark.parametrize('name', ['test', 'test-east'])
def test_an_agent_named_test_is_watched_like_any_other(name):
    # given
    rows = [{'name': 'local', 'role': 'master', 'status': 'Ready'},
            {'name': name, 'role': 'agent', 'status': 'NotReady', 'since': iso(T0 - 2400), 'lastSeen': iso(T0 - 2460), 'error': msg('kubernetes.nodes.no_answer')}]

    # when
    result = nodes_pass(rows, T0)

    # then
    assert [f['key'] for f in result.found] == [f'node-not-ready|-/agent-{name}'] and result.ran == (f'nodes:{name}',)


def test_an_in_cluster_row_outside_a_cluster_and_an_agent_without_a_token_say_so_instead_of_a_node_list(store):
    # given
    off = FakeKube(NODES)
    off.enabled = False
    runtime = runtime_with(store, [{**INFO, 'kube': False, 'cluster': None}], kube=off)

    # when
    master, east = beats(runtime, [T0])[0]

    # then
    assert 'not running in a cluster' in master['clusterError'] and 'clusterSummary' not in master
    assert east['status'] == 'Ready' and east['clusterError'] == msg('kubernetes.nodes.no_token')


def test_cluster_and_agent_failures_show_fixed_text_never_the_error_text(store):
    # given
    answers = [{**INFO, 'cluster': None, 'clusterError': SECRET}, remote.HttpError(503, '/api/agent', json.dumps({'error': SECRET})), remote.HttpError(401, '/api/agent', '')]
    runtime = runtime_with(store, answers, kube=FakeKube(fail=SECRET, code=403))

    # when
    first, second, third = beats(runtime, [T0, T0 + 300, T0 + 600])

    # then
    assert first[0]['clusterError'] == msg('connection.forbidden', status=403) and first[1]['clusterError'] == msg('kubernetes.nodes.no_cluster')
    assert second[1]['error'] == msg('kubernetes.nodes.status', status=503) and third[1]['error'] == msg('kubernetes.nodes.refused')
    assert 'abcdefgh' not in json.dumps([first, second, third])


def test_an_agent_whose_relay_is_off_is_notready_and_the_others_still_beat(store):
    # given
    runtime = runtime_with(store, [INFO], instances={'local': LOCAL, 'hub': HUB, 'east': EAST | {'via': 'hub'}})
    runtime.instances.set_enabled('kubernetes', 'hub', False, 'alice')
    runtime.refresh()

    # when
    rows = beats(runtime, [T0])[0]

    # then
    assert [(r['name'], r['status']) for r in rows] == [('local', 'Ready'), ('east', 'NotReady')]
    assert rows[1]['error'] == msg('kubernetes.nodes.relay_off', relay='hub') and rows[1]['relay'] == 'hub'


def test_a_relay_lists_the_agents_it_relays_for(store):
    # given
    runtime = runtime_with(store, [INFO, INFO], instances={'local': LOCAL, 'hub': HUB, 'east': EAST | {'via': 'hub'}})

    # when
    rows = {r['name']: r for r in beats(runtime, [T0])[0]}

    # then
    assert rows['east']['relay'] == 'hub' and rows['hub']['relaysFor'] == ['east'] and 'relay' not in rows['hub']
    assert (rows['east']['via'], rows['hub']['via']) == ('https://east.example.com', 'https://hub.example.com')


def test_node_cards_keep_their_old_keys_across_instances(store):
    # given
    store.save({'initialized': True, 'anomalies': [], 'muted': []}, None)
    runtime = runtime_with(store, [INFO, DOWN, DOWN])
    sweeper = sweeper_of(store, runtime)

    # when
    sweeper.run('kubernetes', 'nodes', T0)
    sweeper.run('kubernetes', 'nodes', T0 + 300)
    stale = {r['name']: r for r in store.load()['nodes']}
    sweeper.run('kubernetes', 'nodes', T0 + 600)
    state = store.load()

    # then
    assert stale['east']['status'] == 'NotReady' and stale['east']['stale'] is True and stale['east']['clusterSummary'] == INFO['cluster']
    assert stale['local']['clusterSummary']['notReady'] == ['node-3'] and all(r['plugin'] == 'kubernetes' and 'cluster' not in r for r in stale.values())
    cards = [(a['key'], a['ranKey'], a['src'], a.get('cluster')) for a in state['anomalies'] if a['status'] == 'open']
    assert cards == [('node-not-ready|-/agent-east', 'nodes:east', 'nodes', None)], 'no card for the NotReady node of local: Prometheus raises that one'
    assert set(state['serverPasses']) == {'nodes'} and state['serverPasses']['nodes']['status'] == 'ok'


def open_cards(store) -> list[tuple]:
    return [(a['key'], a['ranKey'], a['sev'], a['label']) for a in store.load()['anomalies'] if a['status'] == 'open']


def without_rows(store) -> None:
    state = store.load()
    store.save({**state, 'nodes': []}, state['rev'])


def test_a_down_agent_with_an_open_card_and_no_previous_row_keeps_its_card_and_escalates(store):
    # given
    store.save({'initialized': True, 'anomalies': [], 'muted': []}, None)
    runtime = runtime_with(store, [INFO, DOWN, DOWN, DOWN, DOWN])
    sweeper = sweeper_of(store, runtime)
    for step in range(3):
        sweeper.run('kubernetes', 'nodes', T0 + 300 * step)
    opened = open_cards(store)
    without_rows(store)

    # when
    sweeper.run('kubernetes', 'nodes', T0 + 900)
    kept = open_cards(store)
    east = next(r for r in store.load()['nodes'] if r['name'] == 'east')
    sweeper.run('kubernetes', 'nodes', T0 + 2100)
    escalated = open_cards(store)
    what = next(a['what'] for a in store.load()['anomalies'] if a['status'] == 'open')

    # then
    assert opened == [('node-not-ready|-/agent-east', 'nodes:east', 'warn', msg('kubernetes.nodes.down_label'))]
    assert kept == opened, 'an open card counts as an earlier answer: the first-contact wait never clears it'
    assert (east['status'], east['lastSeen'], east['seenBefore']) == ('NotReady', None, True)
    assert escalated == [('node-not-ready|-/agent-east', 'nodes:east', 'crit', msg('kubernetes.nodes.down_label'))]
    assert msg('kubernetes.nodes.unseen') in what


def test_a_late_opened_crit_card_stays_crit_after_the_rows_are_lost(store):
    # given
    store.save({'initialized': True, 'anomalies': [], 'muted': []}, None)
    runtime = runtime_with(store, [INFO] + [DOWN] * 6)
    sweeper = sweeper_of(store, runtime)
    for at in (T0, T0 + 300, T0 + 1800, T0 + 2100):
        sweeper.run('kubernetes', 'nodes', at)
    crit = [(a['sev'], a['now'], a['firstSeen']) for a in store.load()['anomalies'] if a['status'] == 'open']
    without_rows(store)

    # when
    sweeper.run('kubernetes', 'nodes', T0 + 2400)
    after = [(a['sev'], a['now']) for a in store.load()['anomalies'] if a['status'] == 'open']

    # then
    assert crit == [('crit', 30, iso(T0 + 1800))], 'the card opened 25 min into the outage and turned crit at 30 min'
    assert after == [('crit', 35)], 'the outage start comes from the card\'s last update, so it is never downgraded'


def test_a_never_answered_card_without_a_previous_row_stays_a_warn_never_answered_card(store):
    # given
    store.save({'initialized': True, 'anomalies': [], 'muted': []}, None)
    runtime = runtime_with(store, [DOWN, DOWN, DOWN, DOWN])
    sweeper = sweeper_of(store, runtime)
    sweeper.run('kubernetes', 'nodes', T0)
    sweeper.run('kubernetes', 'nodes', T0 + 25 * 3600)
    opened = open_cards(store)
    without_rows(store)

    # when
    sweeper.run('kubernetes', 'nodes', T0 + 25 * 3600 + 300)
    sweeper.run('kubernetes', 'nodes', T0 + 26 * 3600)

    # then
    assert opened == [('node-not-ready|-/agent-east', 'nodes:east', 'warn', msg('kubernetes.nodes.never_label'))]
    assert open_cards(store) == opened


def test_a_down_agent_switched_off_and_on_again_between_passes_keeps_its_card(store):
    # given
    store.save({'initialized': True, 'anomalies': [], 'muted': []}, None)
    runtime = runtime_with(store, [INFO, DOWN, DOWN, DOWN])
    sweeper = sweeper_of(store, runtime)
    for step in range(3):
        sweeper.run('kubernetes', 'nodes', T0 + 300 * step)

    # when
    runtime.instances.set_enabled('kubernetes', 'east', False, 'alice')
    runtime.refresh()
    runtime.instances.set_enabled('kubernetes', 'east', True, 'alice')
    runtime.refresh()
    sweeper.run('kubernetes', 'nodes', T0 + 900)

    # then
    assert [(a['key'], a['status']) for a in store.load()['anomalies']] == [('node-not-ready|-/agent-east', 'open')]


@pytest.mark.parametrize('how', ['switched off', 'deleted'])
def test_the_card_of_a_switched_off_or_deleted_agent_closes_at_the_next_pass(store, how):
    # given
    store.save({'initialized': True, 'anomalies': [], 'muted': []}, None)
    runtime = runtime_with(store, [INFO, DOWN, DOWN])
    sweeper = sweeper_of(store, runtime)
    for step in range(3):
        sweeper.run('kubernetes', 'nodes', T0 + 300 * step)
    opened = [(a['key'], a['status']) for a in store.load()['anomalies']]

    # when
    if how == 'deleted':
        runtime.instances.delete('kubernetes', 'east', 'alice')
    else:
        runtime.instances.set_enabled('kubernetes', 'east', False, 'alice')
    runtime.refresh()
    sweeper.run('kubernetes', 'nodes', T0 + 900)
    state = store.load()

    # then
    assert opened == [('node-not-ready|-/agent-east', 'open')]
    assert [(a['key'], a['status']) for a in state['anomalies']] == [('node-not-ready|-/agent-east', 'cleared')]
    assert [r['name'] for r in state['nodes']] == ['local']


def test_agents_are_asked_side_by_side_and_keep_their_order(store):
    # given
    barrier = threading.Barrier(3, timeout=5)
    seen = []

    def send(method, url, headers, data, timeout):
        seen.append(url)
        barrier.wait()
        return remote.Reply(INFO, {})

    agents = {name: {'mode': 'agent', 'url': f'https://{name}.example.com', 'token': f'{name}-token-0003'} for name in ('east', 'north', 'west')}
    runtime = kube_runtime(store, {'local': LOCAL} | agents, kube=FakeKube(NODES), transport=send)

    # when
    rows = beats(runtime, [T0])[0]

    # then
    assert [(r['name'], r['status']) for r in rows] == [('local', 'Ready'), ('east', 'Ready'), ('north', 'Ready'), ('west', 'Ready')]
    assert len(seen) == 3


def test_the_check_runs_once_every_five_minutes_and_declares_its_section():
    # given
    check = next(c for c in kubernetes.PLUGIN.checks if c.id == 'nodes')

    # then
    assert (check.period, check.offset, check.ran, check.sections, check.once, check.tool) == (300, 165, ('nodes',), ('nodes',), True, None)
    assert (nodes.PERIOD, nodes.NOT_READY_S, nodes.CRIT_S, nodes.FIRST_CONTACT_S) == (300, 300, 1800, 24 * 3600)


def test_the_app_runs_the_heartbeat_once_a_cluster_or_an_agent_exists_and_keeps_the_rows(store, monkeypatch, tmp_path):
    # given
    (tmp_path / 'token').write_text('t')
    monkeypatch.setattr(kubernetes, 'SA_ROOT', tmp_path)
    path = tmp_path / 'skopax.yaml'
    path.write_text(json.dumps({'plugins': {'kubernetes': {'instances': [{'name': 'east', **EAST}]}}}), encoding='utf-8')
    b = boot({FILE_ENV: str(path)})

    # when
    with TestClient(create_app(replace(b.settings, token=AUTH['Authorization'].split()[1]), store, registry=b.registry, sources=b.source_set(), kube=FakeKube(NODES), transport=Agent([INFO]))) as c:
        job = next(j for j in c.app.state.scheduler.jobs if j.name == 'kubernetes.nodes')
        job.run(T0)
        tile = {t['id']: t for t in c.get('/api/server', headers=AUTH).json()['plugins']}['kubernetes']
        served = c.get('/api/state', headers=AUTH).json()

    # then
    assert job.period == 300 and tile['passes'] == ['nodes'] and tile['tab'] == 'nodes'
    assert [i['name'] for i in tile['instances']] == ['local', 'east']
    assert [(r['name'], r['role'], r['status']) for r in served['nodes']] == [('local', 'master', 'Ready'), ('east', 'agent', 'Ready')]


def test_the_heartbeat_job_raises_the_card_and_clears_it_from_the_stored_cards_once_the_agent_answers(store, monkeypatch, tmp_path):
    # given
    (tmp_path / 'token').write_text('t')
    monkeypatch.setattr(kubernetes, 'SA_ROOT', tmp_path)
    path = tmp_path / 'skopax.yaml'
    path.write_text(json.dumps({'plugins': {'kubernetes': {'instances': [{'name': 'east', **EAST}]}}}), encoding='utf-8')
    b = boot({FILE_ENV: str(path)})
    store.save({'initialized': True, 'anomalies': [], 'sources': {'prom': 'ok'}, 'sweptIso': iso(T0 - 300), 'muted': []}, None)
    down = remote.HttpError(503, '/api/agent', json.dumps({'error': 'the agent has no token (agent.token), so it answers no reads'}))

    # when
    with TestClient(create_app(replace(b.settings, token=AUTH['Authorization'].split()[1]), store, registry=b.registry, sources=b.source_set(), kube=FakeKube(NODES), transport=Agent([INFO, down, down, INFO]))) as c:
        job = next(j for j in c.app.state.scheduler.jobs if j.name == 'kubernetes.nodes')
        for i in range(3):
            job.run(T0 + 300 * i)
        fired = [(a['key'], a['status']) for a in store.load()['anomalies'] if a.get('src') == 'nodes']
        job.run(T0 + 300 * 3)
        after = [(a['key'], a['status']) for a in store.load()['anomalies'] if a.get('src') == 'nodes']

    # then
    assert fired == [('node-not-ready|-/agent-east', 'open')]
    assert after == [('node-not-ready|-/agent-east', 'cleared')]


def test_the_old_node_store_and_build_helpers_are_gone():
    # then
    assert importlib.util.find_spec('skopax.builds') is None
    assert not hasattr(store_module.Store, 'save_nodes') and not hasattr(store_module.Store, 'last_nodes') and not hasattr(store_module, 'NODES_ID')


def test_a_refused_node_list_is_a_warn_card_that_closes_once_the_list_is_read(store):
    # given
    store.save({'initialized': True, 'anomalies': [], 'muted': []}, None)
    kube = FakeKube(NODES)
    runtime = runtime_with(store, [INFO] * 4, kube=kube)
    sweeper = sweeper_of(store, runtime)
    card = 'no-access|-/nodes|nodes of local'
    test_of = lambda: kubernetes.test(runtime.context(runtime.registry.get('kubernetes').plugin, runtime.instances.get('kubernetes', 'local'), T0))

    # when
    sweeper.run('kubernetes', 'nodes', T0)
    kube.fail, kube.code = SECRET, 403
    sweeper.run('kubernetes', 'nodes', T0 + 300)
    refused = {a['key']: a for a in store.load()['anomalies'] if a['status'] == 'open'}
    refused_status = store.load()['serverPasses']['nodes']['status']
    lacking = test_of()
    kube.code = 401
    rejected = test_of()
    sweeper.run('kubernetes', 'nodes', T0 + 450)
    invalid = {a['key']: a['label'] for a in store.load()['anomalies'] if a['status'] == 'open'}
    kube.fail, kube.code = None, None
    sweeper.run('kubernetes', 'nodes', T0 + 600)
    state = store.load()

    # then
    assert set(refused) == {card} and refused_status == 'ok \u00b7 no access to nodes of local'
    assert (refused[card]['sev'], refused[card]['ranKey'], refused[card]['label']) == ('warn', 'nodes:access:nodes of local', 'Kubernetes cannot read nodes of local: the token lacks list on nodes')
    assert (lacking.ok, lacking.summary) == (False, 'Kubernetes cannot read nodes of local: the token lacks list on nodes')
    assert (rejected.ok, rejected.summary) == (False, msg('connection.rejected', status=401))
    assert invalid == {card: 'Kubernetes cannot read nodes of local: the token is invalid or lacks list on nodes'}
    assert {a['key']: a['status'] for a in state['anomalies']} == {card: 'cleared'} and state['serverPasses']['nodes']['status'] == 'ok'
    assert [r['name'] for r in state['nodes']] == ['local', 'east']


def test_a_node_list_that_fails_otherwise_names_its_part_in_an_error_status_and_keeps_the_access_card(store):
    # given
    store.save({'initialized': True, 'anomalies': [], 'muted': []}, None)
    kube = FakeKube(NODES, fail=SECRET, code=403)
    runtime = runtime_with(store, [INFO] * 2, kube=kube)
    sweeper = sweeper_of(store, runtime)
    card = 'no-access|-/nodes|nodes of local'

    # when
    sweeper.run('kubernetes', 'nodes', T0)
    kube.code = 503
    sweeper.run('kubernetes', 'nodes', T0 + 300)
    state = store.load()

    # then
    assert state['serverPasses']['nodes']['status'] == 'error: nodes of local: ' + msg('connection.status', status=503)
    assert {a['key']: a['status'] for a in state['anomalies']} == {card: 'open'}
