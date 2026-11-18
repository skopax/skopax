import concurrent.futures
import dataclasses
import json
import logging
import threading
import time
from pathlib import Path

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient
from pymongo.errors import ServerSelectionTimeoutError

from tests.conftest import AUTH, anomaly, settings
from tests.fake_anthropic import FakeAnthropic, reply, text, tool
from tests.fake_slack import fake_slack
from tests.plugin_kit import KIT_KEY, add_instance, jira_runtime, prom_runtime
from tests.plugins import demo_plugin
from tests.plugins.base import minimal
from tests.test_investigator import seed
from tests.test_investigator_tools import sources
from skopax.app import create_app
from skopax.contract import Capabilities, Check, Field, Tool
from skopax.engine import iso
from skopax.instances import Instances, PluginState
from skopax.investigator import toolset, tools
from skopax.investigator.tools import PASS_TABLES, PROPOSE_CARD_ACTION, PROPOSE_INVESTIGATION, PROPOSE_TICKET, READ_TOOLS, TAG, WRITE_TOOLS, Effects, Toolbox
from skopax.investigator.toolset import SUMMARY_LIMIT, NoProposal, ProposalClosed, Proposals, Run, ToolSet
from skopax.messages import msg
from skopax.operators import Operators
from skopax.plugintools import PluginTools
from skopax.registry import Registry
from skopax.runtime import Runtime
from skopax.sealer import Sealer, master_key
from skopax.slack import PROPOSAL_CONFIRM, answer_blocks
from skopax.slackevents import Clicks, Replier
from skopax.store import Store

NOW = 1_792_224_000.0
KEY = 'log|checkout-service|ERROR|boom'
BASE = 'https://skopax.example.com'
READS = ['card', 'deploys_since', 'jira_issue', 'jira_search', 'open_cards',
         'pass_table', 'past_cards', 'past_runs', 'self_review']
OFFERED = {
    'card live': sorted(READS + ['assess', 'group', 'note', 'slack_draft', 'slack_send', 'slack_update', 'tag', 'triage']),
    'card shadow': sorted(READS + ['assess', 'group', 'note', 'slack_draft', 'slack_send', 'slack_update', 'tag', 'triage']),
    'report': sorted(READS + ['report_brief', 'report_slack_draft']),
    'page ask': sorted(READS + ['assess', 'group', 'note', 'slack_tell', 'slack_update', 'tag', 'triage']),
    'slack ask': sorted(READS + ['propose_card_action', 'propose_ticket']),
    'chat': sorted(READS + ['propose_card_action', 'propose_investigation']),
}
CAPS = {'self_review': 2}
SCANNED = {'jira_search', 'jira_issue', 'pass_table'}
CORE_NAMES = {t['name'] for t in READ_TOOLS + WRITE_TOOLS} | {TAG, PROPOSE_TICKET['name'], PROPOSE_CARD_ACTION['name'], PROPOSE_INVESTIGATION['name']}
DEMO = demo_plugin.PLUGIN
DEMO_DIR = Path(demo_plugin.__file__).parent
URL = 'https://demo.example.com'
SCHEMA = {'type': 'object', 'properties': {'what': {'type': 'string'}}, 'additionalProperties': False}
PROPOSAL = {'tool': 'demo_propose', 'plugin': 'demo', 'instance': 'main', 'args': {'title': 'Restart the demo worker', 'text': 'It is stuck since 14:00.'},
            'title': 'Restart the demo worker', 'text': 'It is stuck since 14:00.'}
RUN_ID = 'run-proposal'


def effects(tell: bool = False) -> Effects:
    return Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}, tell=(lambda *a: {}) if tell else None)


def runs(store: Store, **over) -> dict[str, Toolbox]:
    def box(keys, mode='live', tell=False, **kw):
        return Toolbox(sources(**over), store, effects(tell), list(keys), mode, NOW, BASE, settings=settings(), **kw)

    return {'card live': box([KEY]), 'card shadow': box([KEY], 'shadow'), 'report': box([], report_id='r1'),
            'page ask': box([KEY], tell=True, ask=True, asker='alice@example.com'), 'slack ask': box([KEY], ask=True, read_only=True),
            'chat': box([], ask=True, read_only=True, chat=True)}


def names(b: Toolbox) -> list[str]:
    return sorted(d['name'] for d in b.definitions())


def counted(plugin, name: str, calls: list, raises: Exception | None = None, field: str = 'run'):
    def counting(ctx, args):
        calls.append(dict(args))
        if raises is not None:
            raise raises
        return {'wrote': args.get('text', '')} if field == 'run' else {'ok': True, 'summary': f"demo worker restarted ({args.get('title')})"}

    return dataclasses.replace(plugin, tools=tuple(dataclasses.replace(t, **{field: counting}) if t.name == name else t for t in plugin.tools))


class Rig:
    def __init__(self, store: Store, *plugins):
        self.store = store
        self.registry = Registry.of(*plugins, directory=DEMO_DIR)
        self.instances = Instances(store.db, self.registry, lambda: Sealer('rig-key'))
        self.runtime = Runtime(self.registry, PluginState(store.db), self.instances)
        self.tools = PluginTools(self.runtime)

    def add(self, plugin, name: str = 'main', **values):
        add_instance(self.store, plugin, name, sealer_key='rig-key', primary=True, **({'url': URL} | values))
        self.runtime.refresh()
        return self

    def off(self, plugin_id: str = 'demo', name: str = 'main'):
        self.instances.set_enabled(plugin_id, name, False, 'alice')
        self.runtime.refresh()

    def runs(self) -> dict[str, Toolbox]:
        return runs(self.store, plugin_tools=self.tools)


def stored_run(store: Store, proposal: dict | None = PROPOSAL, run_id: str = RUN_ID) -> str:
    store.add_investigation({'id': run_id, 'mode': 'live', 'trigger': 'slack', 'keys': [KEY], 'startedAt': iso(NOW), 'outcome': 'done'} | ({'proposal': dict(proposal)} if proposal else {}))
    return run_id


def proposal_app(store: Store, plugin) -> TestClient:
    add_instance(store, plugin, 'main', sealer_key=master_key(store.db, settings()), primary=True, url=URL)
    return create_app(settings(), store, registry=Registry.of(plugin, directory=DEMO_DIR))


def test_core_tools_are_offered_exactly_as_before(store: Store):
    # when
    offered = {run: names(b) for run, b in runs(store).items()}

    # then
    assert offered == OFFERED


def test_core_caps_scan_flags_and_reach_are_unchanged():
    # when
    core = toolset.CORE

    # then
    assert tools.RUN_CALLS == CAPS
    assert set(tools.SOURCE_TOOLS) & CORE_NAMES == SCANNED
    assert set(core) == CORE_NAMES and all(s.owner == 'core' for s in core.values())
    assert {n: s.needs for n, s in core.items() if s.needs is not None} == {'jira_search': 'jira', 'jira_issue': 'jira', 'propose_ticket': 'jira'}
    assert {n: s.per_run for n, s in core.items() if s.per_run is not None} == CAPS
    assert {n for n, s in core.items() if s.scan} == SCANNED
    assert all(s.reach == () for s in core.values())
    assert {n: s.kind for n, s in core.items()} == {t['name']: 'read' for t in READ_TOOLS} | {t['name']: 'write' for t in WRITE_TOOLS} | {TAG: 'write'} \
        | {t['name']: 'propose' for t in (PROPOSE_TICKET, PROPOSE_CARD_ACTION, PROPOSE_INVESTIGATION)}


def test_a_plugin_write_tool_is_offered_where_card_writes_are(store: Store):
    # given
    rig = Rig(store, DEMO).add(DEMO)

    # when
    offered = {run: names(b) for run, b in rig.runs().items()}
    no_card = names(Toolbox(sources(plugin_tools=rig.tools), store, effects(), [], 'live', NOW, BASE, ask=True, settings=settings()))

    # then
    assert {run for run, found in offered.items() if 'demo_write' in found} == {'card live', 'card shadow', 'page ask'}
    assert 'demo_write' not in no_card
    assert all('demo_echo' in found for found in offered.values())


def test_a_plugin_write_tool_never_runs_in_shadow_or_read_only_runs(store: Store):
    # given
    calls = []
    plugin = counted(DEMO, 'demo_write', calls)
    rig = Rig(store, plugin).add(plugin)
    boxes = rig.runs()
    other = Toolbox(sources(plugin_tools=rig.tools), store, effects(), [KEY], 'off', NOW, BASE, settings=settings())

    # when
    shadow = boxes['card shadow'].call('demo_write', {'text': 'x'})
    unknown_mode = other.call('demo_write', {'text': 'x'})
    after_shadow = len(calls)
    read_only = boxes['slack ask'].call('demo_write', {'text': 'x'})
    report = boxes['report'].call('demo_write', {'text': 'x'})
    after_refusals = len(calls)
    live = boxes['card live'].call('demo_write', {'text': 'x'})

    # then
    assert shadow == ('shadow mode: not written', False) and unknown_mode == shadow and after_shadow == 0
    assert boxes['card shadow'].calls[-1]['shadow'] is True and boxes['card shadow'].writes == []
    assert read_only[1] and 'demo_write is not available here: this run can only read' in read_only[0]
    assert report[1] and after_refusals == 0
    assert not live[1] and len(calls) == 1 and 'shadow' not in boxes['card live'].calls[-1]
    assert [(w['tool'], w['proposed']) for w in boxes['card live'].writes] == [('demo_write', False)]


def test_a_plugin_propose_tool_is_offered_in_read_only_asks_only(store: Store):
    # given
    rig = Rig(store, DEMO).add(DEMO)

    # when
    offered = {run: names(b) for run, b in rig.runs().items()}
    boxes = rig.runs()
    refused = boxes['card live'].call('demo_propose', {'title': 't', 'text': 'x'})
    chat = boxes['chat'].call('demo_propose', {'title': 'Restart the demo worker', 'text': 'x'})
    ok = boxes['slack ask'].call('demo_propose', {'title': 'Restart the demo worker', 'text': 'It is stuck.'})
    empty = rig.runs()['slack ask'].call('demo_propose', {'title': '', 'text': 'x'})

    # then
    assert {run for run, found in offered.items() if 'demo_propose' in found} == {'slack ask'}
    assert refused[1] and boxes['card live'].plugin_proposal is None
    assert chat == ('demo_propose is not available in this run', True) and boxes['chat'].plugin_proposal is None
    assert not ok[1] and 'Restart the demo worker' in ok[0]
    assert boxes['slack ask'].plugin_proposal == {'tool': 'demo_propose', 'plugin': 'demo', 'instance': 'main', 'args': {'title': 'Restart the demo worker', 'text': 'It is stuck.'},
                                                  'title': 'Restart the demo worker', 'text': 'It is stuck.'}
    assert empty[1] and 'title' in empty[0]


def test_toolset_specs_list_core_and_serving_plugin_tools(store: Store):
    # given
    runtime = jira_runtime(store, plugins=(DEMO,))
    add_instance(store, DEMO, 'main', sealer_key=KIT_KEY, primary=True, url=URL)
    runtime.refresh()
    found = ToolSet(runtime)

    # when
    specs = found.specs()
    ask = found.offered(Run(read_only=True, ask=True, report=False, chat=False, keys=True, asker=False, shadow=False))

    # then
    assert specs['card'] == toolset.CORE['card']
    assert (specs['demo_write'].kind, specs['demo_write'].owner, specs['demo_write'].reach) == ('write', 'demo', ())
    assert specs['demo_propose'].kind == 'propose' and specs['demo_echo'].scan is True
    assert 'demo_propose' in ask and 'demo_write' not in ask and 'propose_ticket' in ask and 'tag' not in ask
    assert found.status('demo_echo', None) == {'state': 'on', 'reason': None}
    assert ToolSet(None).specs() == toolset.CORE


def test_a_proposal_is_stored_on_the_run(store: Store):
    # given
    now = time.time()
    seed(store, anomaly(KEY), history=[])
    add_instance(store, DEMO, 'main', sealer_key=master_key(store.db, settings()), primary=True, url=URL)
    act = store.add_action({'type': 'ask', 'source': 'slack', 'key': 'ask', 'target': 'slack:C1:1', 'text': 'Can you restart it?', 'cards': [KEY], 'requestedBy': 'bob',
                            'slack': {'where': 'card', 'channel': 'C1', 'threadTs': '1792224000.000100', 'ts': '1792224000.000200', 'user': 'U1'}})
    client = FakeAnthropic([reply(tool('demo_propose', {'title': 'Restart the demo worker', 'text': 'It is stuck since 14:00.'})), reply(text('A restart is proposed.'), stop='end_turn')])
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode='live')

    # when
    with TestClient(create_app(s, store, registry=Registry.of(DEMO, directory=DEMO_DIR), anthropic_client=client)) as c:
        record = c.app.state.investigator.run_once(now)
        one = c.get(f"/api/investigations/{record['id']}").json()
        listed = c.get('/api/investigations?limit=5&chats=false').json()

    # then
    assert 'demo_propose' in [t['name'] for t in client.requests[0]['tools']]
    assert one['proposal']['title'] == 'Restart the demo worker' and one['proposal']['plugin'] == 'demo' and one['proposal']['instance'] == 'main'
    assert 'ticketProposal' not in one
    assert next(r for r in listed if r['id'] == record['id'])['proposal']['text'] == 'It is stuck since 14:00.'
    assert store.get_action(act['id'])['slackReply']['confirm'] is True
    assert 'proposal' not in store.get_action(act['id'])['slackReply']


def test_a_proposal_is_confirmed_once_under_a_race(store: Store, monkeypatch):
    # given
    calls = []
    plugin = counted(DEMO, 'demo_propose', calls, field='confirm')
    app = proposal_app(store, plugin)
    stored_run(store)
    meet = threading.Barrier(2, timeout=3)
    atomic = threading.Lock()
    read, claim = Store.run_proposal, Store.claim_proposal

    def read_together(self, run_id):
        found = read(self, run_id)
        try:
            meet.wait()
        except threading.BrokenBarrierError:
            pass
        return found

    def claim_atomically(self, *args):
        with atomic:
            return claim(self, *args)

    monkeypatch.setattr(Store, 'run_proposal', read_together)
    monkeypatch.setattr(Store, 'claim_proposal', claim_atomically)

    # when
    with TestClient(app) as first:
        second = TestClient(app)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            answers = [f.result() for f in [pool.submit(lambda c=c: c.post(f'/api/investigations/{RUN_ID}/proposal', headers=AUTH)) for c in (first, second)]]

    # then
    assert not meet.broken, 'both confirms passed the read before either claimed'
    codes = sorted(a.status_code for a in answers)
    done = next(a for a in answers if a.status_code == 200).json()
    assert codes == [200, 409] and len(calls) == 1
    assert done == {'ok': True, 'summary': 'demo worker restarted (Restart the demo worker)', 'outcome': 'done'}
    assert next(a for a in answers if a.status_code == 409).json() == {'detail': msg('proposal.done')}
    saved = store.investigation(RUN_ID)['proposal']
    assert saved['confirmedBy'] and saved['confirmedAt'] and saved['result'] == done
    assert msg('proposal.confirmed', title='Restart the demo worker', by=saved['confirmedBy']) in [a['text'] for a in store.list_activity(None, 20)]


def test_confirm_refuses_a_gone_plugin_and_never_500s(store: Store, caplog):
    # given
    calls = []
    plugin = counted(DEMO, 'demo_propose', calls, raises=RuntimeError('db password hunter2'), field='confirm')
    app = proposal_app(store, plugin)
    stored_run(store)
    stored_run(store, None, 'run-plain')
    path = f'/api/investigations/{RUN_ID}/proposal'

    # when
    with caplog.at_level(logging.DEBUG), TestClient(app, raise_server_exceptions=False) as c:
        c.app.state.instances.set_enabled('demo', 'main', False, 'alice')
        c.app.state.runtime.refresh()
        gone = c.post(path, headers=AUTH)
        c.app.state.instances.set_enabled('demo', 'main', True, 'alice')
        c.app.state.runtime.refresh()
        failed = c.post(path, headers=AUTH)
        again = c.post(path, headers=AUTH)
        unknown = c.post('/api/investigations/no-such-run/proposal', headers=AUTH)
        plain = c.post('/api/investigations/run-plain/proposal', headers=AUTH)

    # then
    assert (gone.status_code, gone.json()) == (409, {'detail': msg('proposal.gone')})
    assert (failed.status_code, failed.json()) == (200, {'ok': False, 'summary': msg('proposal.failed'), 'outcome': 'failed'})
    assert (again.status_code, again.json()) == (409, {'detail': msg('proposal.done')}) and len(calls) == 1
    assert (unknown.status_code, unknown.json()) == (404, {'detail': msg('proposal.none')})
    assert (plain.status_code, plain.json()) == (404, {'detail': msg('proposal.none')})
    shown = caplog.text + json.dumps(store.list_activity(None, 20), default=str) + json.dumps(store.investigation(RUN_ID), default=str)
    assert 'hunter2' not in shown


def hanging(release: threading.Event):
    return dataclasses.replace(DEMO, tools=tuple(dataclasses.replace(t, confirm=lambda ctx, args: release.wait(5) and {'ok': True, 'summary': 'late'}) if t.name == 'demo_propose' else t
                                                for t in DEMO.tools))


def test_a_confirm_past_its_deadline_is_stored_as_timed_out_not_failed(store: Store):
    # given
    release = threading.Event()
    plugin = hanging(release)
    rig = Rig(store, plugin).add(plugin)
    stored_run(store)

    # when
    started = time.monotonic()
    out = Proposals(store, rig.runtime, timeout=0.2).confirm(RUN_ID, 'bob@example.com')
    took = time.monotonic() - started
    release.set()

    # then
    assert out == {'ok': False, 'summary': msg('proposal.timed_out'), 'outcome': 'timeout'} and took < 2
    assert msg('proposal.timed_out') == 'The action did not finish in time and may or may not have happened: someone should check it, as this proposal cannot be confirmed again'
    assert store.investigation(RUN_ID)['proposal']['result'] == out
    line = msg('proposal.confirmed', title='Restart the demo worker', by='bob@example.com') + ' \u00b7 ' + msg('proposal.timed_out')
    assert line in [a['text'] for a in store.list_activity(None, 20)]
    with pytest.raises(ProposalClosed) as again:
        Proposals(store, rig.runtime).confirm(RUN_ID, 'bob@example.com')
    assert again.value.text == msg('proposal.done')
    with pytest.raises(NoProposal):
        Proposals(store, rig.runtime).confirm('no-such-run', 'bob@example.com')


def test_an_outcome_that_may_have_happened_says_to_check_and_never_offers_a_second_confirm():
    # when
    texts = {key: msg(key) for key in ('proposal.failed', 'proposal.timed_out', 'proposals.failed', 'proposals.timed_out')}

    # then
    assert texts['proposal.failed'] == 'The confirmed action failed and may or may not have happened: someone should check it, as this proposal cannot be confirmed again'
    assert (texts['proposals.failed'], texts['proposals.timed_out']) == (texts['proposal.failed'], texts['proposal.timed_out']), 'the Slack twins say the same'
    assert [key for key, text in texts.items() if 'again' in text.replace('cannot be confirmed again', '') or 'may or may not' not in text] == []


def test_a_confirm_that_is_not_ok_puts_its_summary_in_the_activity_line_and_marks_it(store: Store):
    # given
    def answer(ctx, args):
        if args['title'] == 'boom':
            raise RuntimeError('db password hunter2')
        return {'ok': args['title'] == 'yes', 'summary': f"the plug-in said {args['title']}"}

    plugin = dataclasses.replace(DEMO, tools=tuple(dataclasses.replace(t, confirm=answer) if t.name == 'demo_propose' else t for t in DEMO.tools))
    rig = Rig(store, plugin).add(plugin)
    for said in ('yes', 'no', 'boom'):
        stored_run(store, PROPOSAL | {'args': {'title': said, 'text': 'x'}}, f'run-{said}')

    # when
    out = {said: Proposals(store, rig.runtime).confirm(f'run-{said}', 'bob@example.com') for said in ('yes', 'no', 'boom')}
    lines = {a['runId']: a for a in store.list_activity(None, 20) if a['kind'] == 'proposal'}

    # then
    head = msg('proposal.confirmed', title='Restart the demo worker', by='bob@example.com')
    assert [out[said]['outcome'] for said in ('yes', 'no', 'boom')] == ['done', 'done', 'failed'] and out['no']['ok'] is False
    assert {run: (a['text'], a.get('error')) for run, a in lines.items()} == {'run-yes': (head, None), 'run-no': (head + ' \u00b7 the plug-in said no', 'declined'),
                                                                               'run-boom': (head + ' \u00b7 ' + msg('proposal.failed'), 'failed')}
    assert 'hunter2' not in json.dumps(list(lines.values()), default=str)


def test_a_declined_summary_is_redacted_on_the_activity_line(store: Store):
    # given
    said = 'no: Authorization: Bearer abcdefgh12345678 for alice@example.com'
    plugin = dataclasses.replace(DEMO, tools=tuple(dataclasses.replace(t, confirm=lambda ctx, args: {'ok': False, 'summary': said}) if t.name == 'demo_propose' else t
                                                for t in DEMO.tools))
    rig = Rig(store, plugin).add(plugin)
    stored_run(store)

    # when
    out = Proposals(store, rig.runtime).confirm(RUN_ID, 'bob@example.com')
    [line] = [a['text'] for a in store.list_activity(None, 20) if a['kind'] == 'proposal']

    # then
    assert out['summary'] == said, 'the run keeps the summary as the plug-in gave it'
    assert '[redacted]' in line and 'abcdefgh12345678' not in line and 'alice@example.com' not in line, line


@pytest.mark.parametrize('said, secret', [pytest.param('no: Authorization: Bearer\tabcdefgh12345678', 'abcdefgh12345678', id='tab-separated-token'),
                                          pytest.param('no: ' + 'x' * 276 + ' alice.smith@example.com', 'alice.smith', id='cut-inside-an-address')])
def test_the_activity_line_redacts_the_whole_summary_before_it_cuts_it(store: Store, said: str, secret: str, monkeypatch):
    # given
    plugin = dataclasses.replace(DEMO, tools=tuple(dataclasses.replace(t, confirm=lambda ctx, args: {'ok': False, 'summary': said}) if t.name == 'demo_propose' else t
                                                for t in DEMO.tools))
    rig = Rig(store, plugin).add(plugin)
    stored_run(store)
    built, add = [], store.add_activity
    monkeypatch.setattr(store, 'add_activity', lambda kind, who, text, **extra: (built.append(text) if kind == 'proposal' else None, add(kind, who, text, **extra))[1])

    # when
    out = Proposals(store, rig.runtime).confirm(RUN_ID, 'bob@example.com')
    [line] = built

    # then
    assert out['summary'] == ''.join(c for c in said if c.isprintable())[:SUMMARY_LIMIT], 'the run keeps its own summary as before'
    assert '[redacted]' in line and secret not in line, line


def test_a_timed_out_slack_confirm_says_it_may_still_complete(store: Store):
    # given
    release = threading.Event()
    plugin = hanging(release)
    rig = Rig(store, plugin).add(plugin)
    stored_run(store)
    slack, post, api = fake_slack(emails={'U1': 'adam.baker@example.com'})
    box = Clicks(store, slack, Operators(lambda email: True, slack, settings(), lambda: NOW), lambda action: 'done', confirm=Proposals(store, rig.runtime, timeout=0.2).confirm,
                 background=lambda job: job())
    blocks = answer_blocks('A restart is proposed.', None, None, confirm={'run': RUN_ID, 'title': 'Restart the demo worker'})

    # when
    box.handle({'type': 'block_actions', 'user': {'id': 'U1'}, 'channel': {'id': 'C1'}, 'message': {'ts': '1792224000.000300', 'thread_ts': '1792224000.000100', 'text': 'x', 'blocks': blocks},
                'actions': [{'action_id': PROPOSAL_CONFIRM, 'value': RUN_ID}]})
    release.set()

    # then
    assert [p['text'] for p in post.posted] == [msg('proposals.timed_out')]


def test_slack_confirm_button_runs_the_same_confirm_for_operators_only(store: Store):
    # given
    calls = []
    plugin = counted(DEMO, 'demo_propose', calls, field='confirm')
    rig = Rig(store, plugin).add(plugin)
    stored_run(store)
    slack, post, api = fake_slack(emails={'U1': 'adam.baker@example.com', 'U9': 'carol@example.com'})
    operators = Operators(lambda email: email == 'adam.baker@example.com', slack, settings(), lambda: NOW)
    box = Clicks(store, slack, operators, lambda action: 'done', confirm=Proposals(store, rig.runtime).confirm, background=lambda job: job())
    blocks = answer_blocks('A restart is proposed.', None, None, confirm={'run': RUN_ID, 'title': 'Restart the demo worker'})

    def click(user: str) -> str:
        return box.handle({'type': 'block_actions', 'user': {'id': user}, 'channel': {'id': 'C1'},
                           'message': {'ts': '1792224000.000300', 'thread_ts': '1792224000.000100', 'text': 'A restart is proposed.', 'blocks': blocks},
                           'actions': [{'action_id': PROPOSAL_CONFIRM, 'value': RUN_ID}]})

    # when
    viewer = click('U9')
    before = len(calls)
    operator = click('U1')
    twice = click('U1')

    # then
    ephemeral = [p['text'] for m, p in api.calls if m == 'chat.postEphemeral']
    assert viewer == 'not-operator' and before == 0
    assert ephemeral[0] == msg('proposals.non_operator') == 'Only Skopax operators can confirm proposals.'
    assert operator == 'confirming' and len(calls) == 1 and twice == 'confirming'
    assert ephemeral[1] == msg('proposals.done')
    assert [(p['channel'], p['thread_ts'], p['text']) for p in post.posted] == [('C1', '1792224000.000100', 'demo worker restarted (Restart the demo worker)')]
    update = next(p for m, p in api.calls if m == 'chat.update')
    assert all(b['type'] != 'actions' for b in update['blocks'])
    assert store.investigation(RUN_ID)['proposal']['confirmedBy'] == 'adam.baker@example.com'


def test_a_failed_slack_confirm_closes_the_proposal_and_says_so_in_the_thread(store: Store):
    # given
    calls = []
    plugin = counted(DEMO, 'demo_propose', calls, raises=RuntimeError('db password hunter2'), field='confirm')
    rig = Rig(store, plugin).add(plugin)
    stored_run(store)
    slack, post, api = fake_slack(emails={'U1': 'adam.baker@example.com'})
    box = Clicks(store, slack, Operators(lambda email: True, slack, settings(), lambda: NOW), lambda action: 'done', confirm=Proposals(store, rig.runtime).confirm,
                 background=lambda job: job())
    blocks = answer_blocks('A restart is proposed.', None, None, confirm={'run': RUN_ID, 'title': 'Restart the demo worker'})
    click = {'type': 'block_actions', 'user': {'id': 'U1'}, 'channel': {'id': 'C1'}, 'message': {'ts': '1792224000.000300', 'thread_ts': '1792224000.000100', 'text': 'x', 'blocks': blocks},
             'actions': [{'action_id': PROPOSAL_CONFIRM, 'value': RUN_ID}]}

    # when
    first = box.handle(click)
    second = box.handle(click)

    # then
    ephemeral = [p['text'] for m, p in api.calls if m == 'chat.postEphemeral']
    assert (first, second) == ('confirming', 'confirming') and len(calls) == 1
    assert [p['text'] for p in post.posted] == [msg('proposals.failed')] and ephemeral == [msg('proposals.done')]
    assert all(b['type'] != 'actions' for b in next(p for m, p in api.calls if m == 'chat.update')['blocks'])
    assert 'hunter2' not in json.dumps(api.calls) + json.dumps(post.posted)


def test_a_slack_answer_with_a_proposal_carries_one_confirm_button(store: Store):
    # given
    slack, post, api = fake_slack()
    replier = Replier(store, slack, dataclasses.replace(settings(), slack_listen='live'))
    act = store.add_action({'type': 'ask', 'source': 'slack', 'key': 'ask', 'target': 'slack:C1:1', 'text': 'restart it', 'cards': [],
                            'slack': {'where': 'channel', 'channel': 'C1', 'threadTs': '1792224000.000100', 'ts': '1792224000.000200', 'user': 'U1'}})
    long = 'Restart the <demo> worker & ' + 'x' * 200

    # when
    replier.reply(act | {'runProposal': {'run': RUN_ID, 'title': long}}, 'done', 'A restart is proposed.')
    failed = replier.reply(act | {'runProposal': {'run': RUN_ID, 'title': long}}, 'failed', '')

    # then
    buttons = [e for b in post.posted[0]['blocks'] if b['type'] == 'actions' for e in b['elements']]
    assert [(b['action_id'], b['value']) for b in buttons] == [(PROPOSAL_CONFIRM, RUN_ID)]
    label = buttons[0]['text']['text']
    assert label.startswith('Confirm: Restart the') and len(label) <= 75
    assert not [b for b in post.posted[1]['blocks'] if b['type'] == 'actions'] and 'button' not in failed


def test_a_core_tool_that_needs_a_plugin_is_hidden_while_it_does_not_serve(store: Store, monkeypatch):
    # given
    monkeypatch.setitem(toolset.CORE, 'card', dataclasses.replace(toolset.CORE['card'], needs='demo'))
    rig = Rig(store, DEMO).add(DEMO)

    # when
    serving = names(rig.runs()['card live'])
    on = ToolSet(rig.runtime).status('card', None)
    rig.off()
    stopped = names(rig.runs()['card live'])
    off = ToolSet(rig.runtime).status('card', None)
    called = rig.runs()['card live'].call('card', {'key': KEY})

    # then
    assert 'card' in serving and 'card' not in stopped
    assert on['state'] == 'on' and off['state'] == 'off' and off['reason']
    assert called[1] and 'card is off' in called[0]


def test_pass_table_offers_plugin_check_sections(store: Store):
    # given
    served = runs(store, plugin_tools=PluginTools(prom_runtime(store, {'main': {}})))['card live']
    plain = runs(store)['card live']

    # when
    table = next(d for d in served.definitions() if d['name'] == 'pass_table')
    empty = next(d for d in plain.definitions() if d['name'] == 'pass_table')
    refused = plain.call('pass_table', {'source': 'services'})

    # then
    assert PASS_TABLES == {}
    assert table['input_schema']['properties']['source']['enum'] == ['deploys', 'routes', 'services']
    assert 'deploys' in table['description'] and 'Prometheus' in table['description'] and 'no outside call' in table['description']
    assert 'routes (top gateway routes of the Prometheus pass; saved by the prom check of Prometheus)' in table['description']
    assert 'services (per-service rows of the Prometheus pass; saved by the prom check of Prometheus)' in table['description']
    assert 'enum' not in empty['input_schema']['properties']['source'] and 'No pass tables are available now' in empty['description']
    assert refused == ('No pass tables are available', True)
    assert 'listed' not in next(t for t in READ_TOOLS if t['name'] == 'pass_table')['description']


def test_a_section_meaning_must_name_a_section_the_check_writes(store: Store):
    # given
    good = minimal('good', 'Good', checks=(Check('good', 'Good', 900, 0, ('good',), lambda ctx: None, sections=('rows',), section_help={'rows': 'one row per job'}),))
    stray = minimal('stray', 'Stray', checks=(Check('stray', 'Stray', 900, 0, ('stray',), lambda ctx: None, sections=('rows',), section_help={'other': 'not written'}),))
    long = minimal('long', 'Long', checks=(Check('long', 'Long', 900, 0, ('long',), lambda ctx: None, sections=('rows',), section_help={'rows': 'two\nlines'}),))

    # when
    registry = Registry.of(good, stray, long, directory=DEMO_DIR)

    # then
    assert registry.get('good').state == 'ok'
    assert registry.get('stray').state == 'broken' and msg('plugins.why.section_help', id='stray', limit=160) in registry.get('stray').reason
    assert registry.get('long').state == 'broken'


def test_tool_descriptions_fill_field_placeholders(store: Store):
    # given
    fielded = minimal('fielded', 'Fielded', capabilities=Capabilities(secrets=('token',)), fields=(Field('url', 'URL', 'url', required=True), Field('token', 'Token', 'secret')),
                      tools=(Tool('fielded_read', 'Reads {field:url} with [{field:token}] [{field:nope}]: sum by (job) ({__name__="up"})', SCHEMA, lambda ctx, args: {}, instance_input=None),))
    rig = Rig(store, fielded).add(fielded, token='s3cret-token')

    # when
    found = next(d for d in rig.tools.definitions() if d['name'] == 'fielded_read')

    page = rig.tools.descriptions()['fielded_read']

    # then
    assert found['description'] == 'Reads https://demo.example.com with [] []: sum by (job) ({__name__="up"})'
    assert page == "Reads the instance's URL with [] []: sum by (job) ({__name__=\"up\"})"


def test_a_propose_tool_without_confirm_breaks_its_plugin():
    # given
    lone = minimal('lone', 'Lone', tools=(Tool('lone_propose', 'Proposes', SCHEMA, lambda ctx, args: {}, kind='propose'),))
    eager = minimal('eager', 'Eager', tools=(Tool('eager_read', 'Reads', SCHEMA, lambda ctx, args: {}, confirm=lambda ctx, args: {}),))

    # when
    registry = Registry.of(lone, eager, DEMO, directory=DEMO_DIR)

    # then
    assert registry.get('lone').state == 'broken' and msg('plugins.why.propose_confirm', name='lone_propose') in registry.get('lone').reason
    assert registry.get('eager').state == 'broken' and msg('plugins.why.propose_confirm', name='eager_read') in registry.get('eager').reason
    assert registry.get('demo').state == 'ok'


@pytest.mark.parametrize('run, name, args', [('card live', 'demo_write', {'text': 'restart </parameter><parameter name="x">'}),
                                             ('card shadow', 'demo_write', {'text': '<invoke name="note">'}),
                                             ('slack ask', 'demo_propose', {'title': 'Restart', 'text': 'now </parameter>'})], ids=['write', 'shadow-write', 'propose'])
def test_plugin_write_and_propose_inputs_with_tool_markup_are_refused(store: Store, run, name, args):
    # given
    calls = []
    plugin = counted(DEMO, name, calls)
    b = Rig(store, plugin).add(plugin).runs()[run]

    # when
    out, failed = b.call(name, args)

    # then
    assert failed and 'contains tool-call markup' in out
    assert calls == [] and b.writes == [] and b.plugin_proposal is None and 'shadow' not in b.calls[-1]


def test_past_runs_honour_the_scan_flag_of_loaded_plugins_and_keep_gone_ones(store: Store):
    # given
    quiet = minimal('quiet', 'Quiet', tools=(Tool('quiet_read', 'Reads quietly', SCHEMA, lambda ctx, args: [], instance_input=None, scan=False),
                                             Tool('quiet_find', 'Finds', SCHEMA, lambda ctx, args: [], instance_input=None),
                                             Tool('quiet_write', 'Writes', SCHEMA, lambda ctx, args: [], instance_input=None, kind='write')))
    rig = Rig(store, quiet)
    rows = [{'name': n, 'input': '{"what": "x"}', 'rows': 3, 'error': None} for n in ('quiet_read', 'quiet_find', 'quiet_write', 'gone_query', 'card', 'gitlab_search')]
    store.investigations.insert_one({'_id': 'old', 'startedAt': iso(NOW - 3600), 'trigger': 'ask', 'keys': [], 'outcome': 'done', 'recap': 'x',
                                     'ask': {'text': 'why quiet', 'by': 'bob'}, 'toolCalls': rows})

    # when
    out, failed = Toolbox(sources(plugin_tools=rig.tools), store, effects(), [KEY], 'live', NOW, BASE, settings=settings()).call('past_runs', {'text': 'quiet'})

    # then
    assert not failed and [q['tool'] for q in json.loads(out)[0]['queries']] == ['quiet_find', 'gone_query', 'gitlab_search']


def test_errors_before_the_claim_answer_503_and_claim_nothing(store: Store, monkeypatch, caplog):
    # given
    plugin = counted(DEMO, 'demo_propose', [], field='confirm')
    app = proposal_app(store, plugin)
    stored_run(store)

    def down(self, run_id):
        raise ServerSelectionTimeoutError('mongo at 10.0.0.7 is down, password hunter2')

    # when
    with caplog.at_level(logging.DEBUG), TestClient(app, raise_server_exceptions=False) as c:
        monkeypatch.setattr(Store, 'run_proposal', down)
        read_down = c.post(f'/api/investigations/{RUN_ID}/proposal', headers=AUTH)
        monkeypatch.undo()
        monkeypatch.setattr(Proposals, '_serving', lambda self, proposal: {}['missing'])
        bug = c.post(f'/api/investigations/{RUN_ID}/proposal', headers=AUTH)

    # then
    assert (read_down.status_code, read_down.json()) == (503, {'detail': msg('proposal.unavailable')})
    assert (bug.status_code, bug.json()) == (503, {'detail': msg('proposal.unavailable')})
    assert 'confirmedAt' not in store.investigation(RUN_ID)['proposal']
    assert 'hunter2' not in caplog.text and '10.0.0.7' not in caplog.text


def test_the_route_catch_all_answers_failed_without_the_error_text(store: Store, monkeypatch, caplog):
    # given
    app = proposal_app(store, DEMO)
    stored_run(store)

    def broken(self, run_id, by):
        raise RuntimeError('secret token hunter2')

    monkeypatch.setattr(Proposals, 'confirm', broken)

    # when
    with caplog.at_level(logging.DEBUG), TestClient(app, raise_server_exceptions=False) as c:
        answer = c.post(f'/api/investigations/{RUN_ID}/proposal', headers=AUTH)

    # then
    assert (answer.status_code, answer.json()) == (200, {'ok': False, 'summary': msg('proposal.failed'), 'outcome': 'failed'})
    assert 'hunter2' not in caplog.text and 'RuntimeError' in caplog.text


def test_a_context_failure_runs_in_the_guarded_thread_and_a_lost_result_write_keeps_the_answer(store: Store, monkeypatch):
    # given
    calls = []
    plugin = counted(DEMO, 'demo_propose', calls, field='confirm')
    rig = Rig(store, plugin).add(plugin)
    stored_run(store)
    stored_run(store, run_id='run-two')
    real = rig.runtime.context

    def no_context(*args):
        raise RuntimeError('context down')

    def no_write(self, run_id, result):
        raise ServerSelectionTimeoutError('mongo is down')

    # when
    monkeypatch.setattr(rig.runtime, 'context', no_context)
    lost_context = Proposals(store, rig.runtime).confirm(RUN_ID, 'bob@example.com')
    monkeypatch.setattr(rig.runtime, 'context', real)
    monkeypatch.setattr(Store, 'proposal_result', no_write)
    lost_write = Proposals(store, rig.runtime).confirm('run-two', 'bob@example.com')

    # then
    assert lost_context == {'ok': False, 'summary': msg('proposal.failed'), 'outcome': 'failed'} and calls == [{'title': 'Restart the demo worker', 'text': 'It is stuck since 14:00.'}]
    assert lost_write == {'ok': True, 'summary': 'demo worker restarted (Restart the demo worker)', 'outcome': 'done'}
    assert any(a.get('runId') == RUN_ID and a.get('outcome') == 'failed' for a in store.list_activity(None, 20))


def test_a_run_that_does_not_finish_keeps_no_proposal(store: Store):
    # given
    now = time.time()
    seed(store, anomaly(KEY), history=[])
    add_instance(store, DEMO, 'main', sealer_key=master_key(store.db, settings()), primary=True, url=URL)
    act = store.add_action({'type': 'ask', 'source': 'slack', 'key': 'ask', 'target': 'slack:C1:1', 'text': 'Can you restart it?', 'cards': [KEY], 'requestedBy': 'bob',
                            'slack': {'where': 'card', 'channel': 'C1', 'threadTs': '1792224000.000100', 'ts': '1792224000.000200', 'user': 'U1'}})
    down = anthropic.APIConnectionError(request=httpx2.Request('POST', 'https://api.anthropic.com/v1/messages'))
    client = FakeAnthropic([reply(tool('demo_propose', {'title': 'Restart the demo worker', 'text': 'It is stuck.'})), down])
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode='live')

    # when
    with TestClient(create_app(s, store, registry=Registry.of(DEMO, directory=DEMO_DIR), anthropic_client=client)) as c:
        record = c.app.state.investigator.run_once(now)
        one = c.get(f"/api/investigations/{record['id']}").json()

    # then
    assert one['outcome'] == 'failed' and 'proposal' not in one
    assert 'confirm' not in (store.get_action(act['id']).get('slackReply') or {})


def test_specs_never_call_the_probe_targets_hook_and_unknown_names_say_so(store: Store):
    # given
    release, calls = threading.Event(), []
    stuck = minimal('stuck', 'Stuck', probe_targets=lambda info: calls.append(info) or release.wait(5) and [], tools=(Tool('stuck_read', 'Reads', SCHEMA, lambda ctx, args: {}, instance_input=None),))
    rig = Rig(store, stuck)
    before = len(calls)

    # when
    started = time.monotonic()
    specs = ToolSet(rig.runtime).specs()
    took = time.monotonic() - started
    release.set()
    unknown = ToolSet(rig.runtime).status('no_such_tool', None)

    # then
    assert len(calls) == before and took < 2 and specs['stuck_read'].reach == ()
    assert unknown == {'state': 'off', 'reason': msg('toolset.unknown', name='no_such_tool')}


def test_a_slack_confirm_runs_off_the_intake_worker(store: Store):
    # given
    release = threading.Event()
    plugin = hanging(release)
    rig = Rig(store, plugin).add(plugin)
    stored_run(store)
    slack, post, api = fake_slack(emails={'U1': 'adam.baker@example.com'})
    box = Clicks(store, slack, Operators(lambda email: True, slack, settings(), lambda: NOW), lambda action: 'done', confirm=Proposals(store, rig.runtime).confirm)
    blocks = answer_blocks('A restart is proposed.', None, None, confirm={'run': RUN_ID, 'title': 'Restart the demo worker'})

    # when
    started = time.monotonic()
    decision = box.handle({'type': 'block_actions', 'user': {'id': 'U1'}, 'channel': {'id': 'C1'},
                           'message': {'ts': '1792224000.000300', 'thread_ts': '1792224000.000100', 'text': 'x', 'blocks': blocks},
                           'actions': [{'action_id': PROPOSAL_CONFIRM, 'value': RUN_ID}]})
    took = time.monotonic() - started
    before = list(post.posted)
    release.set()
    deadline = time.monotonic() + 3
    while not post.posted and time.monotonic() < deadline:
        time.sleep(0.02)

    # then
    assert decision == 'confirming' and took < 1 and before == []
    assert [p['text'] for p in post.posted] == ['late']


def test_with_strict_tools_plugin_write_and_propose_tools_are_strict_too(store: Store):
    # given
    rig = Rig(store, DEMO).add(DEMO)

    def box(**kw):
        return {d['name']: d for d in Toolbox(sources(plugin_tools=rig.tools), store, effects(), [KEY], 'live', NOW, BASE, settings=settings(), strict=True, **kw).definitions()}

    # when
    card, ask = box(), box(ask=True, read_only=True)

    # then
    assert card['demo_write']['strict'] is True and card['demo_write']['input_schema']['additionalProperties'] is False
    assert ask['demo_propose']['strict'] is True and ask['demo_propose']['input_schema']['additionalProperties'] is False
    assert 'strict' not in card['demo_echo'] and 'strict' not in ask['demo_echo']
    assert 'additionalProperties' not in next(t for t in DEMO.tools if t.name == 'demo_write').input_schema
