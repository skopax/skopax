import asyncio
import collections
import dataclasses
import datetime
import decimal
import logging
import math
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from skopax import remote, sections
from skopax.app import create_app
from skopax.clearnotes import due
from skopax.contract import Check, Field, InstanceInfo, PassResult, ReadError, Sweep
from skopax.engine import anomaly, clearing_key, iso, parse_iso, retire
from skopax.instances import REFRESH_S, Instance, Instances, PluginState
from skopax.kube import Kube
from skopax.messages import msg
from skopax.registry import Registry
from skopax.runtime import RunContext, Runtime
from skopax.scheduler import Job, Scheduler
from skopax.sealer import master_key
from skopax.store import Conflict, Store
from skopax.sweeps import RETIRED_KEEP_S, Reads, Sweeper, scope
from tests.conftest import settings
from tests.plugin_kit import add_instance, no_loop
from tests.plugins import demo_plugin, two_checks_plugin
from tests.plugins.base import minimal
from tests.test_instances import pem

WINDOW = 2 * REFRESH_S

HERE = Path(demo_plugin.__file__).parent
NOW = 1_792_224_000.0
SINCE = '2000-01-01T00:00:00Z'
SCRIPT: dict = {}
POST = {'channel': 'C9', 'ts': '1792224000.000001', 'at': '2026-10-26T08:00:00Z', 'by': 'server', 'draft': False}


CALLS: collections.Counter = collections.Counter()
RELEASE = threading.Event()
STARTED = threading.Event()


def make_card(svc: str, src: str = 'demo'):
    return anomaly(src, 'err', 'shop', svc, '', 'warn', 5, None, None, '', 'ERROR logs', 'five errors')


def pulse(ctx):
    plan = SCRIPT.get(ctx.instance.name, {})
    CALLS[ctx.instance.name] += 1
    if plan.get('action') == 'raise':
        raise RuntimeError('boom')
    if plan.get('action') == 'hang':
        RELEASE.wait(5)
    if plan.get('action') == 'gate':
        STARTED.set()
        RELEASE.wait(5)
    if 'raw' in plan:
        return plan['raw']
    rows = [{'ns': 'shop', 'svc': svc, 'kind': 'app', **plan.get('extra', {})} for svc in plan.get('rows', ())]
    result = PassResult('pulse', True, 'ok', [make_card(svc) for svc in plan.get('cards', ())], plan.get('sections', {}), plan.get('ran', ()))
    return Sweep(result, plan.get('out', {'services': rows}), list(plan.get('deploys', ())))


def other_pulse(ctx):
    plan = SCRIPT.get('other:' + ctx.instance.name, {})
    rows = [{'ns': 'shop', 'svc': svc, 'kind': 'app'} for svc in plan.get('rows', ())]
    return Sweep(PassResult('other', True, 'ok', [make_card(svc, 'other') for svc in plan.get('cards', ())], {}, ()), {'services': rows})


CHECK = Check('pulse', 'Demo pulse', 900, 0, ('demo',), pulse, legacy_alias=True, sections=('services',))
DEMO = dataclasses.replace(demo_plugin.PLUGIN, checks=(CHECK,))
OTHER = minimal('other', 'Other', fields=(Field('url', 'URL', 'url'),))
BOTH = dataclasses.replace(OTHER, checks=(Check('other', 'Other', 900, 0, ('other',), other_pulse, sections=('services',)),))


@pytest.fixture(autouse=True)
def clean_script():
    SCRIPT.clear()
    CALLS.clear()
    RELEASE.clear()
    STARTED.clear()
    two_checks_plugin.reset()
    yield
    RELEASE.set()
    two_checks_plugin.RELEASE.set()
    SCRIPT.clear()


@contextmanager
def running(store, tmp_path, names=('main', 'east'), plugins=(DEMO,), others=()):
    key = master_key(store.db, settings())
    for i, name in enumerate(names):
        add_instance(store, DEMO, name, sealer_key=key, primary=i == 0, url='https://demo.example.com')
    for name in others:
        add_instance(store, OTHER, name, sealer_key=key, primary=name == others[0], url='https://other.example.com')
    with TestClient(create_app(settings(), store, registry=Registry.of(*plugins, directory=HERE), kube=Kube(root=tmp_path))) as c:
        yield c


def cards(store) -> dict:
    return {a['key']: a for a in store.load()['anomalies']}


def key_of(svc: str) -> str:
    return f'err|shop/{svc}'


def seed_legacy(store, **extra) -> dict:
    legacy = {'key': key_of('old'), 'sev': 'warn', 'status': 'open', 'svc': 'old', 'label': 'ERROR logs', 'src': 'demo', 'cluster': 'east', 'now': 5, 'short': 'WARN old ERROR logs',
              'firstSeen': '2026-10-26T07:00:00Z', 'lastSeen': iso(NOW - 600)} | extra
    store.save({'initialized': True, 'anomalies': [legacy], 'sources': {}, 'muted': [], 'sweptIso': iso(NOW - 900)}, None)
    return legacy


class Tick:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, results, now, links, after=None, check=None, screen=None, clusters=None):
        self.calls.append((results, check))
        return None


def test_job_set_follows_switches_without_restart(store, tmp_path):
    # given
    with running(store, tmp_path) as c:
        names = lambda: [j.name for j in c.app.state.scheduler.jobs]

        # when
        first = names()
        c.app.state.plugin_state.set('demo', False, 'alice')
        off = names()
        c.app.state.plugin_state.set('demo', True, 'alice')
        back = names()

    # then
    assert 'demo.pulse' in first
    assert 'demo.pulse' not in off
    assert 'demo.pulse' in back


def test_scheduler_runs_with_no_jobs_at_start(store, tmp_path):
    # given
    scheduler = None

    # when
    with TestClient(create_app(settings(), store, registry=Registry.of(), kube=Kube(root=tmp_path))) as c:
        scheduler = c.app.state.scheduler
        beat = None
        for _ in range(100):
            beat = store.runs.find_one({'_id': f'boot:{scheduler.boot}'})
            if beat:
                break
            time.sleep(0.03)

    # then
    assert [j.name for j in scheduler.jobs] == ['version-check']
    assert beat is not None, 'the loop runs and beats even when no job is on yet'


def test_a_job_that_appears_later_runs_at_the_next_poll(store):
    # given
    runs, box = [], []
    scheduler = Scheduler(store, lambda: list(box), poll=0.02, clock=lambda: NOW)

    async def go():
        loop = asyncio.create_task(scheduler.loop())
        await asyncio.sleep(0.1)
        before = list(runs)
        box.append(Job('demo.pulse', 900, 0, lambda now: runs.append(now)))
        for _ in range(100):
            if runs:
                break
            await asyncio.sleep(0.02)
        loop.cancel()
        return before

    # when
    before = asyncio.run(go())

    # then
    assert before == [] and runs == [NOW]


def test_nudge_runs_a_job_at_the_next_poll(store):
    # given
    runs = []
    scheduler = Scheduler(store, lambda: [Job('demo.pulse', 900, 0, lambda now: runs.append(now))], clock=lambda: NOW + 5)

    # when
    first = scheduler.run_due()
    again = scheduler.run_due()
    scheduler.nudge('demo.pulse')
    nudged = scheduler.run_due()
    after = scheduler.run_due()

    # then
    assert (first, again, nudged, after) == (['demo.pulse'], [], ['demo.pulse'], [])
    assert store.runs.find_one({'_id': f'demo.pulse:nudge:{int(NOW + 5)}'}) is not None
    assert len(runs) == 2


def test_second_instance_results_are_scoped(store, tmp_path):
    # given
    SCRIPT.update(main={'cards': ['checkout-service']}, east={'cards': ['payments']})
    with running(store, tmp_path) as c:
        recorder = Recorder()
        by_run = Sweeper(c.app.state.runtime, store, recorder)

        # when
        by_run.run('demo', 'pulse', NOW)
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        found = cards(store)
        sources = store.load()['sources']

    # then
    results, check = recorder.calls[0]
    named = {r.name: r for r in results}
    assert check == 'pulse'
    assert set(named) == {'pulse', 'pulse-east'}
    assert set(named['pulse-east'].ran) == {'demo-east', 'east'} and named['pulse'].ran == ('demo',)
    east, main = found[key_of('payments') + '@east'], found[key_of('checkout-service')]
    assert east['cluster'] == 'east' and east['ranKey'] == 'demo-east' and east['short'].startswith('WARN EAST ')
    assert 'cluster' not in main and 'ranKey' not in main and main['short'].startswith('WARN checkout-service ')
    assert set(sources) == {'pulse', 'pulse-east'}


def test_legacy_second_instance_cards_still_clear(store, tmp_path):
    # given
    seed_legacy(store)
    SCRIPT.update(main={}, east={})
    with running(store, tmp_path) as c:

        # when
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        after = cards(store)[key_of('old')]

    # then
    assert after['status'] == 'cleared' and not after.get('expired')


def test_the_bare_alias_belongs_only_to_checks_that_ask_for_it(store, tmp_path):
    # given
    seed_legacy(store)
    other = dataclasses.replace(OTHER, checks=(Check('other', 'Other', 900, 0, ('other',), lambda ctx: Sweep(PassResult('other', True, 'ok', [], {}, ())), sections=('services',)),))
    plain_east = InstanceInfo('east', False, 'east', {})

    # when
    plain = set(scope(Sweep(PassResult('other', True, 'ok')), other.checks[0], plain_east).result.ran)
    legacy = set(scope(Sweep(PassResult('pulse', True, 'ok')), CHECK, plain_east).result.ran)
    with running(store, tmp_path, plugins=(DEMO, other), others=('main', 'east')) as c:
        sweeper = c.app.state.sweeper
        sweeper.run('other', 'other', NOW)
        kept = cards(store)[key_of('old')]['status']
        sweeper.retire('other', 'east', NOW)
        kept_after_retire = cards(store)[key_of('old')]['status']
        sweeper.retire('demo', 'east', NOW)
        closed = cards(store)[key_of('old')]

    # then
    assert plain == {'other-east'} and legacy == {'demo-east', 'east'}
    assert (kept, kept_after_retire) == ('open', 'open')
    assert closed['status'] == 'cleared' and closed['expired'] is True


def test_a_raising_or_hanging_instance_fails_alone(store, tmp_path):
    # given
    SCRIPT.update(main={'cards': ['checkout-service'], 'rows': ['checkout-service']}, east={'cards': ['payments'], 'rows': ['payments']}, secondary={'cards': ['orders'], 'rows': ['orders']})
    with running(store, tmp_path, names=('main', 'east', 'secondary')) as c:
        sweeper = c.app.state.sweeper
        sweeper.run('demo', 'pulse', NOW)
        before = store.load()
        sweeper.timeout = 0.2
        SCRIPT.update(main={'cards': ['checkout-service', 'catalog'], 'rows': ['checkout-service']}, east={'action': 'raise'}, secondary={'action': 'hang'})

        # when
        sweeper.run('demo', 'pulse', NOW + 900)
        after = store.load()

    # then
    found = {a['key']: a for a in after['anomalies']}
    assert found[key_of('catalog')]['status'] == 'open'
    assert after['sources']['pulse'] == 'ok'
    assert after['sources']['pulse-east'] == 'error: RuntimeError'
    assert after['sources']['pulse-secondary'] == msg('sweeps.timed_out', seconds=0.2) == 'error: timed out after 0.2 s'
    assert found[key_of('payments') + '@east']['status'] == 'open' and found[key_of('orders') + '@secondary']['status'] == 'open'
    rows = lambda state, who: [r for r in state['services'] if r.get('cluster') == who]
    assert rows(after, 'east') == rows(before, 'east') != []
    assert rows(after, 'secondary') == rows(before, 'secondary') != []


def slow_pulse(ctx):
    time.sleep(0.8)
    return Sweep(PassResult('slow', True, 'ok', [], {}, ('slow',)), {})


def test_a_long_check_uses_its_own_timeout_and_runs_in_the_background(store, tmp_path):
    # given
    slow = Check('slow', 'Slow pulse', 900, 0, ('slow',), slow_pulse, timeout=2, background=True)
    plugin = dataclasses.replace(DEMO, checks=(CHECK, slow))
    SCRIPT.update(main={'action': 'hang'})
    with running(store, tmp_path, names=('main',), plugins=(plugin,)) as c:
        base = c.app.state.sweeper
        sweeper = Sweeper(base.runtime, store, base.apply, timeout=0.5)

        # when
        sweeper.run('demo', 'slow', NOW)
        sweeper.run('demo', 'pulse', NOW)
        jobs = {j.name: j.background for j in sweeper.jobs()}

    # then
    sources = store.load()['sources']
    assert sources['slow'] == 'ok'
    assert sources['pulse'] == msg('sweeps.timed_out', seconds=0.5)
    assert jobs == {'demo.pulse': False, 'demo.slow': True}


def gated_slow(ctx):
    STARTED.set()
    RELEASE.wait(5)
    return Sweep(PassResult('slow', True, 'ok', [], {}, ('slow',)), {})


def test_a_background_check_never_delays_another_job_when_the_scheduler_runs_both(store, tmp_path):
    # given
    slow = Check('slow', 'Slow pulse', 900, 0, ('slow',), gated_slow, timeout=30, background=True)
    plugin = dataclasses.replace(DEMO, checks=(CHECK, slow))
    with running(store, tmp_path, names=('main',), plugins=(plugin,)) as c:
        scheduler = Scheduler(Store(store.db), c.app.state.sweeper.jobs(), clock=lambda: NOW + 5)

        # when
        started = scheduler.run_due()
        STARTED.wait(5)
        while_slow = dict(store.load()['sources'])
        RELEASE.set()
        for _ in range(100):
            if 'slow' in store.load()['sources']:
                break
            time.sleep(0.02)
        after = store.load()['sources']

    # then
    assert sorted(started) == ['demo.pulse', 'demo.slow']
    assert while_slow.get('pulse') == 'ok' and 'slow' not in while_slow, 'the short job finished while the long one still ran'
    assert after['slow'] == 'ok'


@pytest.mark.parametrize('over, what', [({'timeout': 0}, 'timeout'), ({'timeout': -5}, 'timeout'), ({'timeout': 1.5}, 'timeout'), ({'timeout': True}, 'timeout'),
                                        ({'timeout': 3601}, 'timeout'), ({'background': 'yes'}, 'background')])
def test_a_check_timeout_is_a_whole_number_of_seconds_and_background_a_flag(over, what):
    # given
    bad = dataclasses.replace(DEMO, checks=(dataclasses.replace(CHECK, **over),))

    # when
    found = Registry.of(bad, directory=HERE).get('demo')

    # then
    assert found.state == 'broken' and f'check {what}' in found.reason


def test_two_instances_never_share_a_card(store, tmp_path, caplog):
    # given
    SCRIPT.update(main={'cards': ['checkout-service']}, east={'cards': ['checkout-service']})
    main_key, east_key = key_of('checkout-service'), key_of('checkout-service') + '@east'
    with running(store, tmp_path) as c:
        sweeper = c.app.state.sweeper

        # when
        with caplog.at_level(logging.WARNING, logger='skopax.sweeps'):
            sweeper.run('demo', 'pulse', NOW)
        both = cards(store)
        status = store.load()['sources']['pulse-east']
        SCRIPT.update(main={'cards': ['checkout-service']}, east={})
        sweeper.run('demo', 'pulse', NOW + 900)
        east_quiet = {k: a['status'] for k, a in cards(store).items()}
        SCRIPT.update(main={}, east={'cards': ['checkout-service']})
        sweeper.run('demo', 'pulse', NOW + 1800)
        main_quiet = {k: a['status'] for k, a in cards(store).items()}

    # then
    assert set(both) == {main_key, east_key}
    assert 'cluster' not in both[main_key] and 'ranKey' not in both[main_key]
    assert (both[east_key]['cluster'], both[east_key]['ranKey']) == ('east', 'demo-east')
    assert status == 'ok' and not [r for r in caplog.records if r.name == 'skopax.sweeps' and r.levelno == logging.WARNING]
    assert east_quiet == {main_key: 'open', east_key: 'cleared'}
    assert main_quiet == {main_key: 'cleared', east_key: 'open'}


def test_a_second_instance_reads_its_open_cards_with_the_keys_it_raised(store, tmp_path):
    # given
    SCRIPT.update(main={'cards': ['checkout-service']}, east={'cards': ['payments']})
    with running(store, tmp_path) as c:
        c.app.state.sweeper.run('demo', 'pulse', NOW)

        # when
        east = Reads(store, DEMO, CHECK).open_cards(InstanceInfo('east', False, 'east', {}))
        main = Reads(store, DEMO, CHECK).open_cards(InstanceInfo('main', True, None, {}))
        stored = set(cards(store))

    # then
    assert stored == {key_of('checkout-service'), key_of('payments') + '@east'}
    assert [(a['key'], a['cluster'], a['ranKey']) for a in east] == [(key_of('payments'), 'east', 'demo-east')]
    assert [a['key'] for a in main] == [key_of('checkout-service')]


def seed_cards(store, *found: dict) -> None:
    store.save({'initialized': True, 'anomalies': [{'sev': 'warn', 'status': 'open', 'label': 'ERROR logs', 'src': 'demo', 'now': 5, 'short': 'WARN x ERROR logs',
                                                   'firstSeen': iso(NOW - 3600), 'lastSeen': iso(NOW - 600)} | f for f in found],
                'sources': {}, 'muted': [], 'sweptIso': iso(NOW - 900)}, None)


def test_a_no_data_ran_key_clears_no_card_and_a_normal_sub_key_still_does(store, tmp_path):
    # given
    orders = key_of('orders') + '@east'
    seed_cards(store, {'key': key_of('no-data'), 'svc': 'no-data', 'ranKey': 'demo:no-data'}, {'key': key_of('payments'), 'svc': 'payments', 'ranKey': 'demo:payments'},
               {'key': orders, 'svc': 'orders', 'ranKey': 'demo:no-data-east', 'cluster': 'east'})
    SCRIPT.update(main={'ran': ('demo:no-data',)}, east={'ran': ('demo:no-data',)})
    with running(store, tmp_path) as c:
        sweeper = c.app.state.sweeper

        # when
        sweeper.run('demo', 'pulse', NOW)
        quiet = {k: a['status'] for k, a in cards(store).items()}
        SCRIPT.update(main={'ran': ('demo:payments', 'demo:no-data')}, east={'ran': ('demo:no-data',)})
        sweeper.run('demo', 'pulse', NOW + 900)
        normal = {k: a['status'] for k, a in cards(store).items()}

    # then
    assert quiet == {key_of('no-data'): 'open', key_of('payments'): 'open', orders: 'open'}
    assert normal == {key_of('no-data'): 'open', key_of('payments'): 'cleared', orders: 'open'}


def test_a_second_instance_keeps_the_sentinel_and_adds_the_alias_only_for_a_whole_read():
    # given
    east = InstanceInfo('east', False, 'east', {})

    # when
    quiet = scope(Sweep(PassResult('pulse', True, 'ok', [], {}, ('demo:no-data',))), CHECK, east).result.ran
    mixed = scope(Sweep(PassResult('pulse', True, 'ok', [], {}, ('demo:payments', 'demo:no-data'))), CHECK, east).result.ran
    whole = scope(Sweep(PassResult('pulse', True, 'ok', [], {}, ('demo', 'demo:no-data'))), CHECK, east).result.ran

    # then
    assert quiet == ('demo:no-data',)
    assert set(mixed) == {'demo:payments-east', 'demo:no-data'}
    assert set(whole) == {'demo-east', 'demo:no-data', 'east'}


def test_ran_keys_outside_the_check_are_refused(store, tmp_path):
    # given
    other = {'key': 'slack-posts|x', 'sev': 'warn', 'status': 'open', 'svc': 'x', 'label': 'Slack post', 'src': 'slack-posts', 'now': 1, 'short': 'WARN x Slack post',
             'firstSeen': '2026-10-26T07:00:00Z', 'lastSeen': iso(NOW - 600)}
    store.save({'initialized': True, 'anomalies': [other], 'sources': {}, 'muted': [], 'sweptIso': iso(NOW - 900)}, None)
    SCRIPT.update(main={'ran': ('slack-posts',)}, east={})
    with running(store, tmp_path) as c:

        # when
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        state = store.load()

    # then
    assert state['sources']['pulse'].startswith('error: ') and 'slack-posts' in state['sources']['pulse']
    assert state['sources']['pulse-east'] == 'ok'
    assert {a['key']: a['status'] for a in state['anomalies']}['slack-posts|x'] == 'open'


def test_sections_merge_per_instance_and_services_get_status(store, tmp_path):
    # given
    SCRIPT.update(main={'cards': ['checkout-service'], 'rows': ['checkout-service']}, east={'rows': ['checkout-service', 'payments']})
    with running(store, tmp_path) as c:
        sweeper = c.app.state.sweeper
        sweeper.run('demo', 'pulse', NOW)
        first = store.load()['services']
        SCRIPT.update(east={'rows': ['payments']})

        # when
        sweeper.run('demo', 'pulse', NOW + 900)
        second = store.load()['services']

    # then
    view = lambda rows: sorted(((r.get('cluster'), r['svc'], r['status']) for r in rows), key=lambda t: (t[0] or '', t[1]))
    assert view(first) == [(None, 'checkout-service', 'warn'), ('east', 'checkout-service', 'warn'), ('east', 'payments', 'ok')]
    assert view(second) == [(None, 'checkout-service', 'warn'), ('east', 'payments', 'ok')]


def test_deploys_of_an_instance_retired_before_the_apply_are_not_saved(store, tmp_path):
    # given
    doc = {'ns': 'shop', 'svc': 'payments', 'fromImage': '3', 'toImage': '4', 'at': iso(NOW - 50)}
    SCRIPT.update(main={'rows': ['checkout-service']}, east={'deploys': [doc], 'rows': ['payments']})
    with running(store, tmp_path) as c:
        real = c.app.state.server_apply
        box: dict = {}

        def apply(*args, **kwargs):
            box['sweeper'].retire('demo', 'east', NOW)
            return real(*args, **kwargs)

        box['sweeper'] = sweeper = Sweeper(c.app.state.runtime, store, apply, clock=Tick(1000.0))

        # when
        sweeper.run('demo', 'pulse', NOW)
        saved = store.deploys_since(SINCE)

    # then
    assert saved == []


def test_a_pass_is_not_applied_while_the_instance_list_is_older_than_the_window(store, tmp_path, caplog, monkeypatch):
    # given
    SCRIPT.update(main={'cards': ['checkout-service']}, east={'cards': ['payments']})
    monkeypatch.setattr(Runtime, 'loop', no_loop)
    with running(store, tmp_path) as c:
        instances = c.app.state.instances
        later = time.monotonic() + 1000
        instances.clock = lambda: later

        # when
        with caplog.at_level(logging.WARNING):
            stale = c.app.state.sweeper.run('demo', 'pulse', NOW)
            c.app.state.sweeper.run('demo', 'pulse', NOW + 1)
        before = store.load()['anomalies']
        instances.refresh()
        fresh = c.app.state.sweeper.run('demo', 'pulse', NOW + 2)

    # then
    assert stale is None and before == []
    assert caplog.text.count('the instance list was last read') == 1
    assert fresh is not None and len(store.load()['anomalies']) == 2


def test_deploys_of_a_second_instance_carry_its_cluster(store, tmp_path):
    # given
    main_doc = {'ns': 'shop', 'svc': 'checkout-service', 'fromImage': '1', 'toImage': '2', 'at': iso(NOW - 100), 'imageBy': 'alice'}
    east_doc = {'ns': 'shop', 'svc': 'payments', 'fromImage': '3', 'toImage': '4', 'at': iso(NOW - 50)}
    SCRIPT.update(main={'deploys': [main_doc], 'rows': ['checkout-service'], 'extra': {'image': '2'}}, east={'deploys': [east_doc]})
    asked = []
    with running(store, tmp_path) as c:
        sweeper = Sweeper(c.app.state.runtime, store, c.app.state.server_apply, enrich=asked.append)

        # when
        sweeper.run('demo', 'pulse', NOW)
        saved = {d['svc']: d for d in store.deploys_since(SINCE)}
        rows = store.load()['services']

    # then
    assert 'cluster' not in saved['checkout-service'] and saved['payments']['cluster'] == 'east'
    assert asked == [[('shop', 'checkout-service', '2'), ('shop', 'payments', '4')]]
    assert [r['imageBy'] for r in rows] == ['alice']


def test_switching_an_instance_off_retires_only_its_cards(store, tmp_path):
    # given
    SCRIPT.update(main={'cards': ['checkout-service'], 'rows': ['checkout-service']}, east={'cards': ['payments', 'orders'], 'rows': ['payments']})
    with running(store, tmp_path) as c:
        sweeper = c.app.state.sweeper
        sweeper.run('demo', 'pulse', NOW)
        before = store.load()

        # when
        count = sweeper.retire('demo', 'east', NOW + 60)
        after = store.load()

    # then
    found, kept = {a['key']: a for a in after['anomalies']}, {a['key']: a for a in before['anomalies']}
    assert count == 2
    for svc in ('payments', 'orders'):
        retired = found[key_of(svc) + '@east']
        assert retired['status'] == 'cleared' and retired['expired'] is True
        assert retired['note']['text'] == msg('cards.retired', plugin='Demo', instance='east')
        posted = dict(retired, slackPost=POST)
        by_key = {retired['key']: posted}
        t = parse_iso(retired['clearedAt']) + 1
        assert due(dict(posted, expired=False), by_key, after, t, 0) is True
        assert due(posted, by_key, after, t, 0) is False
    assert found[key_of('checkout-service')] == kept[key_of('checkout-service')]
    assert [r for r in after['services'] if r.get('cluster') == 'east'] == []
    assert [r for r in after['services'] if r.get('cluster') is None] == [r for r in before['services'] if r.get('cluster') is None] != []
    assert set(after['sources']) == {'pulse'} and set(after['serverPasses']) == {'pulse'}
    assert after['sources']['pulse'] == before['sources']['pulse']


def test_switching_a_whole_plugin_off_retires_every_instance(store, tmp_path):
    # given
    SCRIPT.update(main={'cards': ['checkout-service'], 'rows': ['checkout-service']}, east={'cards': ['payments'], 'rows': ['payments']})
    with running(store, tmp_path) as c:
        sweeper = c.app.state.sweeper
        sweeper.run('demo', 'pulse', NOW)

        # when
        count = sweeper.retire('demo', None, NOW + 60)
        after = store.load()

    # then
    assert count == 2
    assert {a['status'] for a in after['anomalies']} == {'cleared'}
    assert after['services'] == [] and after['sources'] == {} and after['serverPasses'] == {}


def test_check_runs_record_the_check_name(store, tmp_path):
    # given
    SCRIPT.update(main={}, east={})
    with running(store, tmp_path) as c:

        # when
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        c.app.state.server_apply([PassResult('slack-posts', True, 'ok')], NOW, lambda a: None)
        runs = store.check_runs_since(SINCE)

    # then
    assert sorted((r['name'], r['check']) for r in runs) == [('pulse', 'pulse'), ('pulse-east', 'pulse'), ('slack-posts', 'slack-posts')]


def test_scope_names_the_primary_without_a_segment():
    # given
    info = InstanceInfo('main', True, None, {})
    sweep = Sweep(PassResult('whatever', True, 'ok', [make_card('checkout-service')], {}, ()))

    # when
    scoped = scope(sweep, CHECK, info)

    # then
    assert scoped.result.name == 'pulse' and scoped.result.ran == ('demo',)
    assert 'cluster' not in scoped.result.found[0] and 'ranKey' not in scoped.result.found[0]


def test_retire_closes_open_cards_by_clearing_key_and_keeps_an_existing_note():
    # given
    base = {'sev': 'warn', 'svc': 'x', 'label': 'l', 'short': 's', 'firstSeen': iso(NOW - 60)}
    state = {'anomalies': [dict(base, key='a', status='open', src='demo', ranKey='demo-east', note={'text': 'mine', 'at': 'earlier'}), dict(base, key='b', status='open', src='demo'),
                           dict(base, key='c', status='open', src='demo', cluster='east'), dict(base, key='d', status='cleared', src='demo', ranKey='demo-east')], 'history': []}

    # when
    closed = retire(state, {'demo-east', 'east'}, 'gone', NOW)

    # then
    by_key = {a['key']: a for a in state['anomalies']}
    assert closed == ['a', 'c']
    assert by_key['a']['note'] == {'text': 'mine', 'at': 'earlier'} and by_key['c']['note']['text'] == 'gone'
    assert by_key['a']['expired'] and by_key['c']['expired'] and by_key['b']['status'] == 'open' and 'expired' not in by_key['d']
    assert clearing_key(by_key['c']) == 'east' and clearing_key(by_key['b']) == 'demo'
    assert [e['ev'] for e in state['history']] == ['cleared', 'cleared']


def test_a_parked_primary_is_never_replaced_by_another_instance(store, tmp_path):
    # given
    key = master_key(store.db, settings())
    add_instance(store, DEMO, 'main', sealer_key=key, primary=True, url='not a url')
    add_instance(store, DEMO, 'east', sealer_key=key, primary=False, url='https://demo.example.com')
    SCRIPT.update(main={'cards': ['checkout-service']}, east={'cards': ['payments']})
    with TestClient(create_app(settings(), store, registry=Registry.of(DEMO, directory=HERE), kube=Kube(root=tmp_path))) as c:
        runtime = c.app.state.runtime
        recorder = Recorder()

        # when
        parked = runtime.instances.get('demo', 'main')
        Sweeper(runtime, store, recorder).run('demo', 'pulse', NOW)
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        found = cards(store)
        sources = store.load()['sources']
        none_for_primary = runtime.links({'key': 'k', 'src': 'demo'})
        for_east = runtime.links({'key': 'k', 'src': 'demo', 'cluster': 'east'})

    # then
    assert parked.problem and parked.primary
    assert [r.name for r in recorder.calls[0][0]] == ['pulse-east']
    assert set(found) == {key_of('payments') + '@east'} and found[key_of('payments') + '@east']['cluster'] == 'east'
    assert set(sources) == {'pulse-east'}
    assert none_for_primary is None and for_east


def east_rows(state, who='east'):
    return [r for r in state.get('services', []) if r.get('cluster') == who]


def test_image_by_of_rows_not_refreshed_this_run_is_kept(store, tmp_path):
    # given
    doc = {'ns': 'shop', 'svc': 'payments', 'fromImage': '3', 'toImage': '4', 'at': iso(NOW - 50), 'imageBy': 'bob'}
    SCRIPT.update(main={'rows': ['checkout-service']}, east={'deploys': [doc], 'rows': ['payments'], 'extra': {'image': '4'}})
    with running(store, tmp_path) as c:
        sweeper = c.app.state.sweeper
        sweeper.run('demo', 'pulse', NOW)
        first = east_rows(store.load())
        SCRIPT.update(east={'action': 'raise'})

        # when
        sweeper.run('demo', 'pulse', NOW + 900)
        second = east_rows(store.load())

    # then
    assert [r['imageBy'] for r in first] == ['bob']
    assert [r['imageBy'] for r in second] == ['bob']


def test_a_retire_during_a_run_is_not_undone(store, tmp_path):
    # given
    SCRIPT.update(main={'cards': ['checkout-service']}, east={'cards': ['payments'], 'rows': ['payments']})
    with running(store, tmp_path) as c:
        sweeper = c.app.state.sweeper
        sweeper.run('demo', 'pulse', NOW)
        SCRIPT.update(east={'action': 'gate', 'cards': ['payments', 'orders'], 'rows': ['payments', 'orders']})
        worker = threading.Thread(target=lambda: sweeper.run('demo', 'pulse', NOW + 900))
        worker.start()
        assert STARTED.wait(5)

        # when
        sweeper.retire('demo', 'east', NOW + 910)
        RELEASE.set()
        worker.join(5)
        state = store.load()
        found = {a['key']: a for a in state['anomalies']}
        SCRIPT.update(east={'cards': ['payments'], 'rows': ['payments']})
        sweeper.run('demo', 'pulse', NOW + 1000)
        inside = cards(store)[key_of('payments') + '@east']['status']
        sweeper.revive('demo', 'east')
        sweeper.run('demo', 'pulse', NOW + 1800)
        later = cards(store)

    # then
    assert found[key_of('payments') + '@east']['status'] == 'cleared' and found[key_of('payments') + '@east']['expired'] is True
    assert key_of('orders') + '@east' not in found
    assert east_rows(state) == [] and 'pulse-east' not in state['sources'] and 'pulse-east' not in state['serverPasses']
    assert found[key_of('checkout-service')]['status'] == 'open'
    assert inside == 'cleared'
    assert later[key_of('payments') + '@east']['status'] == 'open'


def test_an_instance_switched_off_during_a_run_contributes_nothing(store, tmp_path):
    # given
    SCRIPT.update(main={'cards': ['checkout-service']}, east={'action': 'gate', 'cards': ['payments'], 'rows': ['payments']})
    with running(store, tmp_path) as c:
        sweeper = c.app.state.sweeper
        worker = threading.Thread(target=lambda: sweeper.run('demo', 'pulse', NOW))
        worker.start()
        assert STARTED.wait(5)

        # when
        c.app.state.instances.set_enabled('demo', 'east', False, 'alice')
        RELEASE.set()
        worker.join(5)
        state = store.load()

    # then
    assert {a['key'] for a in state['anomalies']} == {key_of('checkout-service')}
    assert set(state['sources']) == {'pulse'} and east_rows(state) == []


def test_a_section_the_check_does_not_declare_is_refused(store, tmp_path):
    # given
    SCRIPT.update(main={'cards': ['checkout-service']}, east={'out': {'hidden': [{'ns': 'a', 'svc': 'b', 'kind': 'app'}]}})
    with running(store, tmp_path) as c:

        # when
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        state = store.load()

    # then
    assert state['sources']['pulse-east'] == msg('sweeps.undeclared_section', name='hidden')
    assert 'hidden' not in state and state['sources']['pulse'] == 'ok'


def test_two_plugins_with_the_same_instance_name_keep_their_own_rows(store, tmp_path):
    # given
    SCRIPT.update(main={'rows': ['checkout-service']}, east={'rows': ['payments']})
    SCRIPT.update({'other:main': {'rows': ['search']}, 'other:east': {'rows': ['orders']}})
    with running(store, tmp_path, plugins=(DEMO, BOTH), others=('main', 'east')) as c:
        sweeper = c.app.state.sweeper
        sweeper.run('demo', 'pulse', NOW)
        sweeper.run('other', 'other', NOW)
        both = store.load()['services']
        SCRIPT.update(east={'rows': ['payments', 'ledger']})
        sweeper.run('demo', 'pulse', NOW + 900)
        again = store.load()['services']
        sweeper.retire('demo', 'east', NOW + 950)
        gone = store.load()['services']

    # then
    view = lambda rows: sorted((r['plugin'], r.get('cluster') or '', r['svc']) for r in rows)
    expected = [('demo', '', 'checkout-service'), ('demo', 'east', 'payments'), ('other', '', 'search'), ('other', 'east', 'orders')]
    assert view(both) == sorted(expected)
    assert view(again) == sorted(expected + [('demo', 'east', 'ledger')])
    assert view(gone) == sorted([e for e in expected if e[:2] != ('demo', 'east')])


def test_rows_without_a_plugin_belong_to_the_prometheus_plugin():
    # given
    legacy = {'ns': 'shop', 'svc': 'old', 'kind': 'app', 'cluster': 'east'}
    fresh = [{'ns': 'shop', 'svc': 'new', 'kind': 'app'}]

    # when
    other = {'services': [dict(legacy)]}
    sections.merge_rows(other, 'services', fresh, 'demo', 'east')
    mine = {'services': [dict(legacy)]}
    sections.merge_rows(mine, 'services', fresh, 'prometheus', 'east')
    dropped = {'services': [dict(legacy)]}
    sections.drop_rows(dropped, 'services', 'demo', 'east')
    dropped_mine = {'services': [dict(legacy)]}
    sections.drop_rows(dropped_mine, 'services', 'prometheus', 'east')

    # then
    assert sorted(r['svc'] for r in other['services']) == ['new', 'old']
    assert [(r['svc'], r['plugin'], r['cluster']) for r in mine['services']] == [('new', 'prometheus', 'east')]
    assert dropped['services'] == [legacy]
    assert dropped_mine['services'] == []


def test_a_primary_change_retires_the_old_primary_and_the_promoted_segment(store, tmp_path):
    # given
    SCRIPT.update(main={'cards': ['checkout-service'], 'rows': ['checkout-service']}, east={'cards': ['payments'], 'rows': ['payments']})
    with running(store, tmp_path) as c:
        sweeper, instances = c.app.state.sweeper, c.app.state.instances
        clock = Tick(1000.0)
        sweeper.clock = clock
        sweeper.run('demo', 'pulse', NOW)
        old, new = instances.get('demo', 'main').info(), instances.get('demo', 'east').info()
        instances.make_primary('demo', 'east', 'alice')
        promoted = instances.get('demo', 'east').info()

        # when
        count = sweeper.primary_changed('demo', old, promoted, NOW + 60)
        closed = store.load()
        clock.t += WINDOW + 1
        sweeper.run('demo', 'pulse', NOW + 900)
        after = cards(store)
        rows = store.load()['services']
        sources = set(store.load()['sources'])

    # then
    assert count == 2 and promoted.primary and not new.primary
    notes = {a['key']: a['note']['text'] for a in closed['anomalies']}
    assert notes == {key_of('checkout-service'): msg('cards.rescoped', plugin='Demo', instance='main'), key_of('payments') + '@east': msg('cards.rescoped', plugin='Demo', instance='east')}
    assert {a['status'] for a in closed['anomalies']} == {'cleared'} and all(a['expired'] for a in closed['anomalies'])
    assert closed['services'] == [] and closed['sources'] == {} and closed['serverPasses'] == {}
    assert 'cluster' not in after[key_of('payments')] and after[key_of('payments')]['status'] == 'open'
    assert after[key_of('checkout-service') + '@main']['cluster'] == 'main' and after[key_of('checkout-service') + '@main']['status'] == 'open'
    assert sorted((r.get('cluster'), r['svc']) for r in rows if r.get('cluster')) == [('main', 'checkout-service')]
    assert sources == {'pulse', 'pulse-main'}


def test_retire_takes_the_last_known_info_of_a_deleted_primary_with_no_heir(store, tmp_path):
    # given
    SCRIPT.update(main={'cards': ['checkout-service'], 'rows': ['checkout-service']})
    with running(store, tmp_path, names=('main',)) as c:
        sweeper, instances = c.app.state.sweeper, c.app.state.instances
        sweeper.run('demo', 'pulse', NOW)
        old = instances.get('demo', 'main').info()
        instances.delete('demo', 'main', 'alice')
        c.app.state.runtime.refresh()

        # when
        count = sweeper.retire('demo', old, NOW + 60)
        state = store.load()

    # then
    found = {a['key']: a for a in state['anomalies']}
    assert old.primary and instances.primary('demo') is None and count == 1
    assert found[key_of('checkout-service')]['status'] == 'cleared' and found[key_of('checkout-service')]['expired'] is True
    assert [r['svc'] for r in state['services'] if not r.get('cluster')] == []
    assert 'pulse' not in state['sources'] and 'pulse' not in state['serverPasses']


def test_a_deleted_primary_hands_its_cards_to_the_heir_even_when_a_refresh_healed_first(store, tmp_path):
    # given
    SCRIPT.update(main={'cards': ['checkout-service'], 'rows': ['checkout-service']}, east={'cards': ['payments'], 'rows': ['payments']})
    with running(store, tmp_path) as c:
        sweeper, instances = c.app.state.sweeper, c.app.state.instances
        sweeper.run('demo', 'pulse', NOW)
        old = instances.get('demo', 'main').info()
        instances.delete('demo', 'main', 'alice')
        heir = instances.primary('demo').info()
        c.app.state.runtime.refresh()
        healed = store.load()

        # when
        count = sweeper.primary_changed('demo', old, heir, NOW + 60)
        state = store.load()

    # then
    before = {a['key']: a for a in healed['anomalies']}
    found = {a['key']: a for a in state['anomalies']}
    assert heir.name == 'east' and heir.primary and count == 2
    assert east_rows(healed) == [] and 'pulse-east' not in healed['sources'] and before[key_of('payments') + '@east']['status'] == 'open'
    assert {k: (a['status'], a['expired'], a['note']['text']) for k, a in found.items()} == {
        key_of('checkout-service'): ('cleared', True, msg('cards.rescoped', plugin='Demo', instance='main')),
        key_of('payments') + '@east': ('cleared', True, msg('cards.rescoped', plugin='Demo', instance='east'))}
    assert state['services'] == [] and state['sources'] == {} and state['serverPasses'] == {}


def test_a_hanging_instance_is_not_run_again_while_it_is_still_going(store, tmp_path):
    # given
    SCRIPT.update(main={'cards': ['checkout-service']}, east={'action': 'hang'})
    with running(store, tmp_path) as c:
        sweeper = c.app.state.sweeper
        sweeper.timeout = 0.2

        # when
        sweeper.run('demo', 'pulse', NOW)
        first = store.load()['sources']['pulse-east']
        sweeper.run('demo', 'pulse', NOW + 900)
        second = store.load()['sources']['pulse-east']
        threads = [t for t in threading.enumerate() if t.name.startswith('sweep-')]
        RELEASE.set()
        for t in threads:
            t.join(5)
        SCRIPT.update(east={})
        sweeper.run('demo', 'pulse', NOW + 1800)
        third = store.load()['sources']['pulse-east']

    # then
    assert first == msg('sweeps.timed_out', seconds=0.2) and second == msg('sweeps.still_running')
    assert threads and all(t.daemon for t in threads)
    assert CALLS['east'] == 2 and third == 'ok'


SHAPES = [
    ('card', Sweep(PassResult('pulse', True, 'ok', [{'key': 'x'}], {}, ()), {})),
    ('card', Sweep(PassResult('pulse', True, 'ok', [dict(make_card('a'), sev='fatal')], {}, ()), {})),
    ('card', Sweep(PassResult('pulse', True, 'ok', ['text'], {}, ()), {})),
    ('card', Sweep(PassResult('pulse', True, 'ok', [dict(make_card('a'), now='high')], {}, ()), {})),
    ('section', Sweep(PassResult('pulse', True, 'ok', [], {}, ()), {'services': 'nope'})),
    ('row', Sweep(PassResult('pulse', True, 'ok', [], {}, ()), {'services': [{'svc': 'a'}]})),
    ('row', Sweep(PassResult('pulse', True, 'ok', [], {}, ()), {'services': ['text']})),
    ('deploy', Sweep(PassResult('pulse', True, 'ok', [], {}, ()), {}, [{'ns': 'a', 'svc': 'b'}])),
    ('deploy', Sweep(PassResult('pulse', True, 'ok', [], {}, ()), {}, 'nope')),
]


@pytest.mark.parametrize('what,raw', SHAPES)
def test_one_malformed_result_fails_only_its_instance(store, tmp_path, what, raw):
    # given
    SCRIPT.update(main={'cards': ['checkout-service'], 'rows': ['checkout-service']}, east={'raw': raw})
    with running(store, tmp_path) as c:

        # when
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        state = store.load()

    # then
    assert state['sources']['pulse-east'] == msg('sweeps.bad_result', what=what)
    assert state['sources']['pulse'] == 'ok'
    assert key_of('checkout-service') in {a['key'] for a in state['anomalies']}


def test_a_result_that_is_not_a_sweep_fails_only_its_instance(store, tmp_path):
    # given
    SCRIPT.update(main={'cards': ['checkout-service']}, east={'raw': 'nope'})
    with running(store, tmp_path) as c:

        # when
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        state = store.load()

    # then
    assert state['sources']['pulse-east'] == msg('sweeps.no_result') and state['sources']['pulse'] == 'ok'


def test_rows_in_the_pass_result_are_refused_with_a_reason(store, tmp_path):
    # given
    SCRIPT.update(main={'cards': ['checkout-service']}, east={'sections': {'routes': []}})
    with running(store, tmp_path) as c:

        # when
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        state = store.load()

    # then
    assert state['sources']['pulse-east'] == msg('sweeps.pass_sections') and 'routes' not in state


def test_a_card_whose_clearing_key_is_not_in_ran_is_refused(store, tmp_path):
    # given
    stray = Sweep(PassResult('pulse', True, 'ok', [make_card('a', 'elsewhere')], {}, ()), {})
    SCRIPT.update(main={'cards': ['checkout-service']}, east={'raw': stray})
    with running(store, tmp_path) as c:

        # when
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        state = store.load()

    # then
    assert state['sources']['pulse-east'] == msg('sweeps.card_not_ran')
    assert {a['key'] for a in state['anomalies']} == {key_of('checkout-service')}


def test_retire_matches_the_cluster_and_the_clearing_key_together(store, tmp_path):
    # given
    base = {'sev': 'warn', 'status': 'open', 'label': 'l', 'src': 'demo', 'now': 1, 'short': 'WARN s', 'firstSeen': '2026-10-26T07:00:00Z', 'lastSeen': iso(NOW - 60)}
    own = dict(base, key='own', svc='a', cluster='east', ranKey='demo')
    scoped = dict(base, key='scoped', svc='b', cluster='east', ranKey='demo-east')
    plain = dict(base, key='plain', svc='c')
    elsewhere = dict(base, key='elsewhere', svc='d', cluster='north', ranKey='demo-east')
    store.save({'initialized': True, 'anomalies': [own, scoped, plain, elsewhere], 'sources': {}, 'muted': [], 'sweptIso': iso(NOW - 900)}, None)
    with running(store, tmp_path) as c:
        sweeper = c.app.state.sweeper

        # when
        sweeper.retire('demo', 'east', NOW)
        after_east = {k: a['status'] for k, a in cards(store).items()}
        sweeper.retire('demo', 'main', NOW + 1)
        after_main = {k: a['status'] for k, a in cards(store).items()}

    # then
    assert after_east == {'own': 'cleared', 'scoped': 'cleared', 'plain': 'open', 'elsewhere': 'open'}
    assert after_main == {'own': 'cleared', 'scoped': 'cleared', 'plain': 'cleared', 'elsewhere': 'open'}


def test_a_job_appearing_in_the_running_app_runs_at_the_next_poll(store, tmp_path):
    # given
    SCRIPT.update(main={'cards': ['checkout-service']})
    app = create_app(settings(), store, registry=Registry.of(DEMO, directory=HERE), kube=Kube(root=tmp_path))
    app.state.scheduler.poll = 0.05

    # when
    with TestClient(app) as c:
        none_at_first = [j.name for j in c.app.state.scheduler.jobs if j.name.startswith('demo.')]
        time.sleep(0.2)
        before = cards(store)
        c.app.state.instances.create('demo', 'main', {'url': 'https://demo.example.com'}, True, 'alice')
        for _ in range(100):
            if key_of('checkout-service') in cards(store):
                break
            time.sleep(0.05)
        found = cards(store)

    # then
    assert none_at_first == [] and before == {}
    assert key_of('checkout-service') in found


def make_runtime(store, transport=None):
    registry = Registry.of()
    return Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: None), transport)


def an_instance(**values) -> Instance:
    return Instance('demo', 'main', True, True, values, {}, 'admin', frozenset(), None)


def test_a_context_serves_the_injected_transport_else_a_cached_sender_per_pem(store):
    # given
    one, two = pem(), pem()
    sent = lambda *a, **k: None
    plain, injected = make_runtime(store), make_runtime(store, sent)

    # when
    no_pem = RunContext(plain, DEMO, an_instance(), NOW).http()
    first = RunContext(plain, DEMO, an_instance(ca=one), NOW).http()
    again = RunContext(plain, DEMO, an_instance(ca=one), NOW).http()
    other = RunContext(plain, DEMO, an_instance(ca=two), NOW).http()
    pinned = RunContext(injected, DEMO, an_instance(ca=one), NOW).http()
    bare = RunContext(plain, DEMO, None, NOW).http()

    # then
    assert no_pem is remote.send and bare is remote.send
    assert first is again and first is not other and first is not remote.send
    assert pinned is sent


def test_a_context_reads_rules_and_settings_of_its_plugin_and_an_agent_read_of_an_unknown_op_is_refused(store):
    # given
    runtime = make_runtime(store)
    runtime.live = SimpleNamespace(plugin_value=lambda key: ('value', key), rules_of=lambda key: {'key': key})
    info = an_instance(url='https://demo.example.com')

    # when
    ctx = runtime.context(DEMO, info, NOW)
    bare = runtime.context(DEMO, None, NOW)

    # then
    assert ctx.setting('depth') == ('value', 'demo.depth') and ctx.rules('limits') == {'key': 'demo.limits'}
    assert ctx.now == NOW and ctx.instance.name == 'main' and ctx.instance.config['url'] == 'https://demo.example.com' and bare.instance is None
    with pytest.raises(ReadError) as caught:
        ctx.agent_read('a', 'op', {})
    assert caught.value.status == 404


def test_links_give_the_owner_the_instance_of_the_card_and_join_every_plugin_with_an_answer(store, tmp_path):
    # given
    def broken(card, inst):
        raise RuntimeError('nope')

    def by_a(card, inst):
        return [{'label': 'a:' + inst.name, 'url': 'https://example.com/a'}] if card.get('src') == 'lnka' else None

    def by_b(card, inst):
        return [{'label': 'b:' + inst.name, 'url': 'https://example.com/b'}] if card.get('src') in ('lnka', 'lnkb') else None

    first = minimal('lnk0', 'First', links=broken)
    a = minimal('lnka', 'A', links=by_a, fields=(Field('url', 'URL', 'url'),))
    b = minimal('lnkb', 'B', links=by_b)
    key = master_key(store.db, settings())
    for name, primary in (('main', True), ('east', False)):
        store.db['instances'].replace_one({'_id': f'lnka/{name}'}, {'_id': f'lnka/{name}', 'plugin': 'lnka', 'name': name, 'values': {'url': 'https://a.example.com'}, 'secrets': {}, 'primary': primary}, upsert=True)

    # when
    with TestClient(create_app(settings(), store, registry=Registry.of(first, a, b, directory=HERE), kube=Kube(root=tmp_path))) as c:
        runtime = c.app.state.runtime
        label = lambda card: [x['label'] for x in runtime.links(card) or []]
        primary, east, plain_b, none, unknown = label({'src': 'lnka'}), label({'src': 'lnka', 'cluster': 'east'}), label({'src': 'lnkb'}), runtime.links({'src': 'z'}), label({'src': 'lnka', 'cluster': 'ghost'})
        c.app.state.instances.set_enabled('lnka', 'east', False, 'alice')
        off = label({'src': 'lnka', 'cluster': 'east'})

    # then
    assert primary == ['a:main', 'b:lnkb'] and east == ['a:east', 'b:lnkb'] and plain_b == ['b:lnkb'] and none is None
    assert unknown == ['b:lnkb'] and off == ['b:lnkb']


WIDE = dataclasses.replace(DEMO, checks=(dataclasses.replace(CHECK, sections=('services', 'deploys', 'routes', 'extra')),))
NO_CARDS = PassResult('pulse', True, 'ok', [], {}, ())
GOOD_ROW = {'ns': 'shop', 'svc': 'payments', 'kind': 'app'}
SENTINEL = 'https://alice:hunter2-SENTINEL@demo.example.com/x?token=SENTINEL'

MALFORMED = [
    ('card', Sweep(PassResult('pulse', True, 'ok', [dict(make_card('a'), sev=['warn'])], {}, ()), {})),
    ('card', Sweep(PassResult('pulse', True, 'ok', [dict(make_card('a'), ranKey=['x'])], {}, ()), {})),
    ('card', Sweep(PassResult('pulse', True, 'ok', [dict(make_card('a'), now=math.nan)], {}, ()), {})),
    ('row', Sweep(NO_CARDS, {'services': [dict(GOOD_ROW, req='many')]})),
    ('row', Sweep(NO_CARDS, {'services': [dict(GOOD_ROW, image=['x'])]})),
    ('row', Sweep(NO_CARDS, {'services': [dict(GOOD_ROW, **{'gw-req': 'x'})]})),
    ('row', Sweep(NO_CARDS, {'deploys': [{'ns': 'shop', 'svc': 'payments', 'kind': 'app'}]})),
    ('row', Sweep(NO_CARDS, {'extra': ['text']})),
    ('ran', Sweep(PassResult('pulse', True, 'ok', [], {}, [['x']]), {})),
    ('ran', Sweep(PassResult('pulse', True, 'ok', [], {}, 'demo'), {})),
]


@pytest.mark.parametrize('what,raw', MALFORMED)
def test_each_malformed_shape_fails_its_instance_alone(store, tmp_path, what, raw):
    # given
    SCRIPT.update(main={'cards': ['checkout-service'], 'rows': ['checkout-service']}, east={'raw': raw})
    with running(store, tmp_path, plugins=(WIDE,)) as c:

        # when
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        state = store.load()
        served = c.get('/api/state')

    # then
    assert state['sources']['pulse-east'] == msg('sweeps.bad_result', what=what)
    assert state['sources']['pulse'] == 'ok' and key_of('checkout-service') in {a['key'] for a in state['anomalies']}
    assert served.status_code == 200


def test_prometheus_shaped_rows_pass_on_a_second_instance(store, tmp_path):
    # given
    routes = [{'ns': 'shop', 'gw': 'gw', 'route': '/pay', 'req': 5, 'p95': 10, 'd1': 8, 'd7': 9}]
    deploys = [{'ns': 'shop', 'svc': 'payments', 'old': '3', 'new': '4', 'at': '2026-10-26T07:00:00Z', 'warm': False}]
    SCRIPT.update(main={}, east={'raw': Sweep(NO_CARDS, {'routes': routes, 'deploys': deploys, 'services': [dict(GOOD_ROW, req=3, image='4')]})})
    with running(store, tmp_path, plugins=(WIDE,)) as c:

        # when
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        state = store.load()

    # then
    assert state['sources']['pulse-east'] == 'ok'
    assert [(r['plugin'], r['cluster'], r['route']) for r in state['routes']] == [('demo', 'east', '/pay')]
    assert [(r['plugin'], r['cluster'], r['svc']) for r in state['deploys']] == [('demo', 'east', 'payments')]


def test_a_retire_on_one_pod_screens_a_stale_pod_and_a_revive_lifts_it(store, tmp_path):
    # given
    key = master_key(store.db, settings())
    for i, name in enumerate(('main', 'east')):
        add_instance(store, DEMO, name, sealer_key=key, primary=i == 0, url='https://demo.example.com')
    SCRIPT.update(main={'cards': ['checkout-service']}, east={'cards': ['payments'], 'rows': ['payments']})
    registry = lambda: Registry.of(DEMO, directory=HERE)
    with TestClient(create_app(settings(), store, registry=registry(), kube=Kube(root=tmp_path))) as a, TestClient(create_app(settings(), store, registry=registry(), kube=Kube(root=tmp_path))) as b:
        one, two = a.app.state.sweeper, b.app.state.sweeper
        one.clock, two.clock = Tick(1000.0), Tick(1005.0)
        one.run('demo', 'pulse', NOW)

        # when
        a.app.state.instances.set_enabled('demo', 'east', False, 'alice')
        one.retire('demo', 'east', NOW + 10)
        two.run('demo', 'pulse', NOW + 20)
        stale = store.load()
        two.clock.t += WINDOW
        b.app.state.instances.refresh()
        two.run('demo', 'pulse', NOW + 900)
        healed = store.load()
        a.app.state.instances.set_enabled('demo', 'east', True, 'alice')
        one.revive('demo', 'east')
        b.app.state.instances.refresh()
        two.run('demo', 'pulse', NOW + 1800)
        back = store.load()

    # then
    assert {a_['key']: a_['status'] for a_ in stale['anomalies']}[key_of('payments') + '@east'] == 'cleared'
    assert east_rows(stale) == [] and 'pulse-east' not in stale['sources']
    assert east_rows(healed) == [] and 'pulse-east' not in healed['sources']
    assert [r['svc'] for r in east_rows(back)] == ['payments'] and back['sources']['pulse-east'] == 'ok'
    assert {a_['key']: a_['status'] for a_ in back['anomalies']}[key_of('payments') + '@east'] == 'open'


def test_retire_then_a_run_then_a_delete_ends_clean(store, tmp_path):
    # given
    SCRIPT.update(main={'cards': ['checkout-service']}, east={'cards': ['payments'], 'rows': ['payments']})
    with running(store, tmp_path) as c:
        sweeper, instances = c.app.state.sweeper, c.app.state.instances
        clock = Tick(1000.0)
        sweeper.clock = clock
        sweeper.run('demo', 'pulse', NOW)
        info = instances.get('demo', 'east').info()

        # when
        sweeper.retire('demo', info, NOW + 10)
        sweeper.run('demo', 'pulse', NOW + 20)
        instances.delete('demo', 'east', 'alice')
        clock.t += WINDOW + 1
        sweeper.run('demo', 'pulse', NOW + 900)
        found = cards(store)[key_of('payments') + '@east']
        state = store.load()

    # then
    assert found['status'] == 'cleared' and found['expired'] is True
    assert east_rows(state) == [] and set(state['sources']) == {'pulse'} and set(state['serverPasses']) == {'pulse'}


def clean_end(store, mains='main'):
    state = store.load()
    pay, check = cards(store)[key_of('payments')], cards(store)[key_of('checkout-service') + '@main']
    return (sorted((str(r.get('cluster')), r['svc']) for r in state['services']), sorted(state['sources']), sorted(state['serverPasses']),
            (pay['status'], pay.get('cluster')), (check['status'], check.get('cluster')))


CLEAN_AFTER_PROMOTION = ([('None', 'payments'), ('main', 'checkout-service')], ['pulse', 'pulse-main'], ['pulse', 'pulse-main'], ('open', None), ('open', 'main'))


def promoted_app(store, tmp_path):
    SCRIPT.update(main={'cards': ['checkout-service'], 'rows': ['checkout-service']}, east={'cards': ['payments'], 'rows': ['payments']})
    return running(store, tmp_path)


def test_a_run_between_the_write_and_a_primary_change_ends_clean(store, tmp_path):
    # given
    with promoted_app(store, tmp_path) as c:
        sweeper, instances = c.app.state.sweeper, c.app.state.instances
        clock = Tick(1000.0)
        sweeper.clock = clock
        sweeper.run('demo', 'pulse', NOW)
        old = instances.get('demo', 'main').info()

        # when
        instances.make_primary('demo', 'east', 'alice')
        sweeper.run('demo', 'pulse', NOW + 120)
        sweeper.primary_changed('demo', old, instances.get('demo', 'east').info(), NOW + 180)
        clock.t += WINDOW + 1
        sweeper.run('demo', 'pulse', NOW + 900)

        # then
        assert clean_end(store) == CLEAN_AFTER_PROMOTION


def test_a_run_inside_the_window_after_a_primary_change_applies_nothing(store, tmp_path):
    # given
    with promoted_app(store, tmp_path) as c:
        sweeper, instances = c.app.state.sweeper, c.app.state.instances
        clock = Tick(1000.0)
        sweeper.clock = clock
        sweeper.run('demo', 'pulse', NOW)
        old = instances.get('demo', 'main').info()
        instances.make_primary('demo', 'east', 'alice')
        sweeper.primary_changed('demo', old, instances.get('demo', 'east').info(), NOW + 60)
        quiet = store.load()

        # when
        clock.t += WINDOW - 1
        sweeper.run('demo', 'pulse', NOW + 120)
        inside = store.load()
        clock.t += 2
        sweeper.run('demo', 'pulse', NOW + 900)

        # then
        assert inside['anomalies'] == quiet['anomalies'] and inside['services'] == [] and inside['sources'] == {} and inside['serverPasses'] == {}
        assert clean_end(store) == CLEAN_AFTER_PROMOTION


def test_a_run_between_a_primary_change_and_the_write_ends_clean(store, tmp_path):
    # given
    with promoted_app(store, tmp_path) as c:
        sweeper, instances = c.app.state.sweeper, c.app.state.instances
        clock = Tick(1000.0)
        sweeper.clock = clock
        sweeper.run('demo', 'pulse', NOW)
        old, new = instances.get('demo', 'main').info(), instances.get('demo', 'east').info()

        # when
        sweeper.primary_changed('demo', old, new, NOW + 60)
        sweeper.run('demo', 'pulse', NOW + 120)
        instances.make_primary('demo', 'east', 'alice')
        clock.t += WINDOW + 1
        sweeper.run('demo', 'pulse', NOW + 900)
        sweeper.run('demo', 'pulse', NOW + 1800)

        # then
        assert clean_end(store) == CLEAN_AFTER_PROMOTION


def test_a_deleted_primary_with_auto_promotion_ends_clean(store, tmp_path):
    # given
    with promoted_app(store, tmp_path) as c:
        sweeper, instances = c.app.state.sweeper, c.app.state.instances
        clock = Tick(1000.0)
        sweeper.clock = clock
        sweeper.run('demo', 'pulse', NOW)
        old = instances.get('demo', 'main').info()

        # when
        instances.delete('demo', 'main', 'alice')
        sweeper.retire('demo', old, NOW + 60)
        sweeper.primary_changed('demo', old, instances.get('demo', 'east').info(), NOW + 61)
        clock.t += WINDOW + 1
        sweeper.run('demo', 'pulse', NOW + 900)
        state = store.load()

        # then
        assert sorted((str(r.get('cluster')), r['svc']) for r in state['services']) == [('None', 'payments')]
        assert sorted(state['sources']) == ['pulse'] and cards(store)[key_of('payments')]['status'] == 'open'


def test_a_whole_plugin_retire_screens_every_instance_inside_the_window(store, tmp_path):
    # given
    SCRIPT.update(main={'cards': ['checkout-service']}, east={'cards': ['payments']})
    with running(store, tmp_path) as c:
        sweeper = c.app.state.sweeper
        clock = Tick(1000.0)
        sweeper.clock = clock
        sweeper.run('demo', 'pulse', NOW)
        sweeper.retire('demo', None, NOW + 60)

        # when
        sweeper.run('demo', 'pulse', NOW + 120)
        inside = store.load()
        sweeper.revive('demo', None)
        sweeper.run('demo', 'pulse', NOW + 180)
        after = store.load()

    # then
    assert {a['status'] for a in inside['anomalies']} == {'cleared'} and inside['sources'] == {}
    assert {a['status'] for a in after['anomalies']} == {'open'} and set(after['sources']) == {'pulse', 'pulse-east'}


def test_the_window_opens_exactly_when_it_ends_and_a_revive_ends_it_early(store, tmp_path):
    # given
    SCRIPT.update(main={'cards': ['checkout-service']}, east={'cards': ['payments'], 'rows': ['payments']})
    with running(store, tmp_path) as c:
        sweeper = c.app.state.sweeper
        clock = Tick(1000.0)
        sweeper.clock = clock
        sweeper.run('demo', 'pulse', NOW)
        sweeper.retire('demo', 'east', NOW + 10)
        sweeper.retire('demo', 'main', NOW + 10)

        # when
        clock.t = 1000.0 + WINDOW - 0.5
        sweeper.run('demo', 'pulse', NOW + 20)
        before = store.load()['sources']
        sweeper.revive('demo', 'east')
        sweeper.run('demo', 'pulse', NOW + 30)
        east_only = store.load()['sources']
        clock.t = 1000.0 + WINDOW
        sweeper.run('demo', 'pulse', NOW + 40)
        after = store.load()['sources']
        sweeper.revive('demo', 'ghost')

    # then
    assert before == {} and set(east_only) == {'pulse-east'} and set(after) == {'pulse', 'pulse-east'}


def test_a_screened_instance_stores_no_deploys(store, tmp_path):
    # given
    doc = {'ns': 'shop', 'svc': 'payments', 'fromImage': '3', 'toImage': '4', 'at': iso(NOW - 50)}
    SCRIPT.update(main={}, east={'deploys': [doc]})
    with running(store, tmp_path) as c:
        sweeper = c.app.state.sweeper
        sweeper.clock = Tick(1000.0)
        sweeper.retire('demo', 'east', NOW)

        # when
        sweeper.run('demo', 'pulse', NOW + 20)
        screened = store.deploys_since(SINCE)
        sweeper.revive('demo', 'east')
        sweeper.run('demo', 'pulse', NOW + 40)
        revived = store.deploys_since(SINCE)

    # then
    assert screened == [] and [d['svc'] for d in revived] == ['payments']


def test_a_run_drops_rows_and_passes_of_instances_that_no_longer_serve(store, tmp_path):
    # given
    SCRIPT.update(main={'cards': ['checkout-service'], 'rows': ['checkout-service']}, east={'rows': ['payments']})
    ghost = {'plugin': 'demo', 'cluster': 'ghost', 'ns': 'shop', 'svc': 'orders', 'kind': 'app'}
    legacy = {'cluster': 'ghost', 'ns': 'shop', 'svc': 'legacy', 'kind': 'app'}
    foreign = {'plugin': 'other', 'cluster': 'ghost', 'ns': 'shop', 'svc': 'search', 'kind': 'app'}
    stray = dict(make_card('orders'), cluster='ghost', ranKey='demo-ghost', status='open', firstSeen='2026-10-26T07:00:00Z', lastSeen=iso(NOW - 60))
    store.save({'initialized': True, 'anomalies': [stray], 'services': [ghost, legacy, foreign], 'sources': {'pulse-ghost': 'ok', 'other-ghost': 'ok', 'pulse': 'ok'},
                'serverPasses': {'pulse-ghost': {'at': 'x', 'status': 'ok'}}, 'muted': [], 'sweptIso': iso(NOW - 900)}, None)
    with running(store, tmp_path) as c:

        # when
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        state = store.load()

    # then
    assert sorted((r.get('plugin') or '', r['svc']) for r in state['services'] if r.get('cluster') == 'ghost') == [('', 'legacy'), ('other', 'search')]
    assert set(state['sources']) == {'pulse', 'pulse-east', 'other-ghost'} and 'pulse-ghost' not in state['serverPasses']
    assert cards(store)[key_of('orders')]['status'] == 'open'


def test_a_parked_primary_leaves_no_primary_rows_behind(store, tmp_path):
    # given
    key = master_key(store.db, settings())
    add_instance(store, DEMO, 'main', sealer_key=key, primary=True, url='not a url')
    add_instance(store, DEMO, 'east', sealer_key=key, primary=False, url='https://demo.example.com')
    SCRIPT.update(east={'rows': ['payments']})
    left = {'plugin': 'demo', 'ns': 'shop', 'svc': 'checkout-service', 'kind': 'app'}
    store.save({'initialized': True, 'anomalies': [], 'services': [left], 'sources': {'pulse': 'ok'}, 'muted': [], 'sweptIso': iso(NOW - 900)}, None)
    with running_registry(store, tmp_path) as c:

        # when
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        state = store.load()

    # then
    assert [(r.get('cluster'), r['svc']) for r in state['services']] == [('east', 'payments')] and set(state['sources']) == {'pulse-east'}


@contextmanager
def running_registry(store, tmp_path):
    with TestClient(create_app(settings(), store, registry=Registry.of(DEMO, directory=HERE), kube=Kube(root=tmp_path))) as c:
        yield c


def seed_retired(store, entries):
    for _ in range(20):
        loaded = store.load()
        try:
            store.save_state(dict(loaded, retired=entries), loaded['rev'])
            return
        except Conflict:
            continue
    raise AssertionError('the tombstones were never seeded: the state kept changing')


def test_old_tombstones_are_pruned_by_a_run_and_by_a_retire(store, tmp_path):
    # given
    SCRIPT.update(main={'cards': ['checkout-service']}, east={})
    with running(store, tmp_path) as c:
        sweeper = c.app.state.sweeper
        clock = Tick(10_000_000.0)
        sweeper.clock = clock
        sweeper.run('demo', 'pulse', NOW)
        old = {'plugin': 'demo', 'name': 'ghost', 'at': clock.t - RETIRED_KEEP_S - 5}
        recent = {'plugin': 'demo', 'name': 'ghost', 'at': clock.t - RETIRED_KEEP_S + 5}
        seed_retired(store, [old, recent])

        # when
        sweeper.run('demo', 'pulse', NOW + 900)
        after_run = store.load()['retired']
        seed_retired(store, [old, recent])
        sweeper.retire('demo', 'east', NOW + 950)
        after_retire = store.load()['retired']

    # then
    assert after_run == [recent]
    assert after_retire == [recent, {'plugin': 'demo', 'name': 'east', 'at': clock.t}]


def test_malformed_tombstones_are_skipped_and_cleaned(store, tmp_path):
    # given
    SCRIPT.update(main={'cards': ['checkout-service']}, east={'cards': ['payments']})
    with running(store, tmp_path) as c:
        sweeper = c.app.state.sweeper
        sweeper.run('demo', 'pulse', NOW)
        seed_retired(store, [{'name': 'east'}, 'junk', {'plugin': 'demo', 'name': 'east', 'at': 'late'}, {'plugin': 'demo', 'name': 7, 'at': 1.0}, None])

        # when
        sweeper.run('demo', 'pulse', NOW + 900)
        ran = store.load()
        count = sweeper.retire('demo', 'east', NOW + 1000)
        retired = store.load()['retired']
        sweeper.revive('demo', 'east')

    # then
    assert set(ran['sources']) == {'pulse', 'pulse-east'} and count == 1
    assert [(t['plugin'], t['name']) for t in retired] == [('demo', 'east')]


def test_a_failure_shows_only_its_type_and_leaks_nowhere(store, tmp_path, caplog):
    # given
    def leaky(ctx):
        if ctx.instance.name == 'east':
            raise RuntimeError(f'login to {SENTINEL} refused')
        return pulse(ctx)

    demo = dataclasses.replace(DEMO, checks=(dataclasses.replace(CHECK, run=leaky),))
    SCRIPT.update(main={'cards': ['checkout-service']})
    caplog.set_level(logging.DEBUG)
    with running(store, tmp_path, plugins=(demo,)) as c:

        # when
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        state = store.load()
        runs = list(store.db['check_runs'].find({}, {'_id': 0}))
        activity = list(store.db['activity'].find({}, {'_id': 0}))

    # then
    assert state['sources']['pulse-east'] == 'error: RuntimeError' and state['serverPasses']['pulse-east']['status'] == 'error: RuntimeError'
    assert [r['error'] for r in runs if r['name'] == 'pulse-east'] == ['error: RuntimeError']
    assert any('pulse-east error: RuntimeError' in a['text'] for a in activity)
    assert 'SENTINEL' not in str(state) + str(runs) + str(activity) + caplog.text


def test_a_read_error_message_passes_capped_and_without_control_characters(store, tmp_path):
    # given
    def refuses(ctx):
        if ctx.instance.name == 'east':
            raise ReadError(502, 'upstream\x1b[31m said\nno ' + 'x' * 300)
        return pulse(ctx)

    demo = dataclasses.replace(DEMO, checks=(dataclasses.replace(CHECK, run=refuses),))
    SCRIPT.update(main={})
    with running(store, tmp_path, plugins=(demo,)) as c:

        # when
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        shown = store.load()['sources']['pulse-east']

    # then
    assert shown.startswith('error: upstream') and len(shown) == len('error: ') + 120
    assert all(ch.isprintable() for ch in shown)


def test_a_plugins_own_failed_status_is_capped_and_cleaned(store, tmp_path):
    # given
    own = Sweep(PassResult('pulse', False, 'bad\x00thing\n' + SENTINEL + 'y' * 300, [make_card('a')], {}, ()), {})
    SCRIPT.update(main={}, east={'raw': own})
    with running(store, tmp_path) as c:

        # when
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        shown = store.load()['sources']['pulse-east']

    # then
    assert shown.startswith('badthing') and len(shown) == 120 and all(ch.isprintable() for ch in shown)
    assert key_of('a') not in cards(store)


def test_a_card_carrying_its_own_cluster_is_refused_by_its_clearing_key(store, tmp_path):
    # given
    stray = Sweep(PassResult('pulse', True, 'ok', [dict(make_card('a'), cluster='zzz')], {}, ()), {})
    SCRIPT.update(main={'raw': stray}, east={})
    with running(store, tmp_path) as c:

        # when
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        state = store.load()

    # then
    assert state['sources']['pulse'] == msg('sweeps.card_not_ran') and state['sources']['pulse-east'] == 'ok'
    assert key_of('a') not in cards(store)


def test_primary_rows_are_replaced_by_their_producing_segment_not_their_own_cluster():
    # given
    state = {}

    # when
    for _ in range(3):
        sections.merge_rows(state, 'routes', [{'ns': 'a', 'svc': 'b', 'kind': 'k', 'cluster': 'zzz'}], 'demo', None)

    # then
    assert state['routes'] == [{'ns': 'a', 'svc': 'b', 'kind': 'k', 'plugin': 'demo'}]


def test_rows_of_two_plugins_with_the_same_key_both_survive_finish():
    # given
    state = {'anomalies': [], 'services': [{'plugin': 'a', 'ns': 'shop', 'svc': 'x', 'kind': 'app'}, {'plugin': 'b', 'ns': 'shop', 'svc': 'x', 'kind': 'app'}]}

    # when
    sections.finish(state)

    # then
    assert sorted(r['plugin'] for r in state['services']) == ['a', 'b']


def test_retiring_an_unknown_name_closes_only_that_segment(store, tmp_path):
    # given
    base = {'sev': 'warn', 'status': 'open', 'label': 'l', 'src': 'demo', 'now': 1, 'short': 'WARN s', 'firstSeen': '2026-10-26T07:00:00Z', 'lastSeen': iso(NOW - 60)}
    ghost = dict(base, key='ghost', svc='a', cluster='ghost', ranKey='demo-ghost')
    plain = dict(base, key='plain', svc='b')
    store.save({'initialized': True, 'anomalies': [ghost, plain], 'sources': {}, 'muted': [], 'sweptIso': iso(NOW - 900)}, None)
    with running(store, tmp_path) as c:

        # when
        count = c.app.state.sweeper.retire('demo', 'ghost', NOW)

    # then
    assert count == 1 and {k: a['status'] for k, a in cards(store).items()} == {'ghost': 'cleared', 'plain': 'open'}


def test_a_primary_pass_keeps_the_ran_keys_the_result_names(store):
    # given
    check = Check('pulse', 'Pulse', 900, 0, ('demo', 'extra'), pulse, legacy_alias=True)
    primary, east = InstanceInfo('main', True, None, {}), InstanceInfo('east', False, 'east', {})
    sweep = Sweep(PassResult('x', True, 'ok', [], {}, ('demo',)))
    whole = Sweep(PassResult('x', True, 'ok', [], {}, ()))

    # when
    named, spread = scope(sweep, check, primary), scope(sweep, check, east)
    default, default_east = scope(whole, check, primary), scope(whole, check, east)

    # then
    assert named.result.ran == ('demo',) and spread.result.ran == ('demo-east', 'east')
    assert default.result.ran == ('demo', 'extra') and default_east.result.ran == ('demo-east', 'extra-east', 'east')


def test_a_retire_that_closes_cards_writes_one_activity_line(store, tmp_path):
    # given
    SCRIPT.update(main={'cards': ['checkout-service']}, east={'cards': ['payments']})
    with running(store, tmp_path) as c:
        sweeper = c.app.state.sweeper
        sweeper.run('demo', 'pulse', NOW)

        # when
        first = sweeper.retire('demo', 'east', NOW + 10)
        second = sweeper.retire('demo', 'east', NOW + 20)
        lines = [a['text'] for a in store.db['activity'].find({'kind': 'auto'})]

    # then
    assert (first, second) == (1, 0) and lines == [msg('sweeps.retired', what='Demo east', count=1)]


def test_reviving_the_whole_plugin_lifts_every_instance_tombstone(store, tmp_path):
    # given
    SCRIPT.update(main={'cards': ['checkout-service']}, east={'cards': ['payments']})
    with running(store, tmp_path) as c:
        sweeper = c.app.state.sweeper
        sweeper.clock = Tick(1000.0)
        sweeper.run('demo', 'pulse', NOW)
        sweeper.retire('demo', 'east', NOW + 10)
        sweeper.retire('demo', 'main', NOW + 10)

        # when
        sweeper.revive('demo', None)
        sweeper.run('demo', 'pulse', NOW + 20)
        state = store.load()

    # then
    assert state['retired'] == [] and set(state['sources']) == {'pulse', 'pulse-east'}


def test_a_result_that_breaks_while_it_is_read_fails_only_its_instance(store, tmp_path):
    # given
    class Unprintable:
        def __str__(self):
            raise RuntimeError('no text')

    broken = Sweep(PassResult('pulse', False, Unprintable(), [], {}, ()), {})
    SCRIPT.update(main={'cards': ['checkout-service']}, east={'raw': broken})
    with running(store, tmp_path) as c:

        # when
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        state = store.load()

    # then
    assert state['sources']['pulse-east'] == msg('sweeps.bad_result', what='result') and state['sources']['pulse'] == 'ok'


class Serving:
    def __init__(self, plugin, *instances):
        self.found = [(plugin, list(instances))]

    def serving(self):
        return self.found


def healed(plugin, state, *instances):
    Sweeper(Serving(plugin, *instances), None, lambda *a, **k: None)._heal(state, plugin)
    return state


def test_a_heal_never_drops_a_core_pass_that_shares_a_prefix():
    # given
    check = Check('slack', 'Slack', 900, 0, ('slack',), lambda ctx: Sweep(NO_CARDS, {}), sections=('services',))
    plug = dataclasses.replace(DEMO, checks=(check,))
    passes = {'slack-posts': 'ok', 'selfreview': 'ok', 'slack': 'ok'}
    state = {'sources': dict(passes), 'serverPasses': {k: {'at': 'x', 'status': v} for k, v in passes.items()}}

    # when
    healed(plug, state, SimpleNamespace(name='main', primary=True))

    # then
    assert set(state['sources']) == set(state['serverPasses']) == {'slack-posts', 'selfreview', 'slack'}


def test_a_heal_gives_a_pass_to_the_check_with_the_longest_matching_id():
    # given
    one = Check('pulse', 'Pulse', 900, 0, ('demo',), lambda ctx: Sweep(NO_CARDS, {}), sections=('services',))
    two = Check('pulse-x', 'Pulse X', 900, 0, ('demo-x',), lambda ctx: Sweep(NO_CARDS, {}), sections=('services',))
    plug = dataclasses.replace(DEMO, checks=(one, two))
    state = {'sources': {'pulse': 'ok', 'pulse-x': 'ok', 'pulse-gone': 'ok'}, 'serverPasses': {'pulse': {}, 'pulse-x': {}, 'pulse-gone': {}}}

    # when
    healed(plug, state, SimpleNamespace(name='main', primary=True))

    # then
    assert set(state['sources']) == set(state['serverPasses']) == {'pulse', 'pulse-x'}


ODD = [('Decimal', decimal.Decimal('1.5')), ('set', {1, 2}), ('date', datetime.date(2026, 1, 1)), ('int key', {1: 'a'}), ('huge int', 2 ** 70)]


@pytest.mark.parametrize('label,value', ODD)
@pytest.mark.parametrize('where', ['row', 'card'])
def test_a_value_the_database_cannot_store_fails_its_instance_alone(store, tmp_path, label, value, where):
    # given
    if where == 'row':
        raw = Sweep(NO_CARDS, {'routes': [{'ns': 'shop', 'gw': 'gw', 'route': '/pay', 'odd': value}]})
    else:
        raw = Sweep(PassResult('pulse', True, 'ok', [dict(make_card('a'), now=value if label == 'huge int' else 1, odd=value)], {}, ()), {})
    SCRIPT.update(main={'cards': ['checkout-service']}, east={'raw': raw})
    with running(store, tmp_path, plugins=(WIDE,)) as c:

        # when
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        state = store.load()

    # then
    assert state['sources']['pulse-east'] == msg('sweeps.bad_result', what=where)
    assert state['sources']['pulse'] == 'ok' and key_of('checkout-service') in {a['key'] for a in state['anomalies']}


def test_a_deploy_with_a_value_the_database_cannot_store_fails_its_instance_alone(store, tmp_path):
    # given
    doc = {'ns': 'shop', 'svc': 'payments', 'fromImage': '3', 'toImage': '4', 'at': '2026-10-26T07:00:00Z', 'odd': decimal.Decimal('1')}
    SCRIPT.update(main={'cards': ['checkout-service']}, east={'deploys': [doc]})
    with running(store, tmp_path) as c:

        # when
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        state = store.load()

    # then
    assert state['sources']['pulse-east'] == msg('sweeps.bad_result', what='deploy') and state['sources']['pulse'] == 'ok'
    assert store.deploys_since(SINCE) == []


def test_a_lone_primary_that_gets_parked_loses_its_pass_on_the_next_refresh(store, tmp_path):
    # given
    SCRIPT.update(main={'cards': ['checkout-service']})
    with running(store, tmp_path, names=('main',)) as c:
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        before = dict(store.load()['sources'])
        store.db['instances'].update_one({'_id': 'demo/main'}, {'$set': {'values.url': 'not a url'}})

        # when
        c.app.state.runtime.refresh()
        after = store.load()

    # then
    assert before.get('pulse') == 'ok'
    assert 'pulse' not in after['sources'] and 'pulse' not in after.get('serverPasses', {})


def test_the_refresh_heal_leaves_passes_alone_while_the_instance_list_is_stale(store, tmp_path, monkeypatch):
    # given
    SCRIPT.update(main={'cards': ['checkout-service']})
    monkeypatch.setattr(Runtime, 'loop', no_loop)
    with running(store, tmp_path, names=('main',)) as c:
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        state = store.load()
        state['sources']['pulse-east'] = 'ok'
        store.save_state(state, state.get('rev', 0))
        later = time.monotonic() + 1000
        c.app.state.instances.clock = lambda: later

        # when
        c.app.state.sweeper.heal()
        kept = store.load()['sources']

    # then
    assert kept.get('pulse-east') == 'ok' and kept.get('pulse') == 'ok'


def test_deploy_enrichment_runs_after_the_apply_never_inside_it(store, tmp_path):
    # given
    doc = {'ns': 'shop', 'svc': 'payments', 'fromImage': '3', 'toImage': '4', 'at': iso(NOW - 50)}
    SCRIPT.update(main={'deploys': [doc], 'rows': ['payments']})
    with running(store, tmp_path, names=('main',)) as c:
        real = c.app.state.server_apply
        inside = []
        calls = []

        def apply(*args, **kwargs):
            inside.append(True)
            try:
                return real(*args, **kwargs)
            finally:
                inside.pop()

        sweeper = Sweeper(c.app.state.runtime, store, apply, enrich=lambda keys: calls.append((list(keys), bool(inside))))

        # when
        sweeper.run('demo', 'pulse', NOW)

    # then
    assert calls == [([('shop', 'payments', '4')], False)]


PAIR = two_checks_plugin.PLUGIN
def pair_key(check: str, name: str) -> str:
    return two_checks_plugin.key_of(check, name) + ('' if name == 'main' else f'@{name}')


@contextmanager
def watching(store, tmp_path, names=('main', 'east')):
    key = master_key(store.db, settings())
    for i, name in enumerate(names):
        add_instance(store, PAIR, name, sealer_key=key, primary=i == 0, url='https://demo.example.com')
    with TestClient(create_app(settings(), store, registry=Registry.of(PAIR, directory=HERE), kube=Kube(root=tmp_path))) as c:
        yield c


def both_ran(sweeper, now):
    sweeper.run('demo', 'pulse', now)
    sweeper.run('demo', 'beat', now)


def statuses(state) -> dict:
    return {a['key']: a['status'] for a in state['anomalies']}


def rows_of(state, section, who):
    return sorted(r['svc'] for r in state.get(section) or [] if r.get('plugin') == 'demo' and (r.get('cluster') or None) == who)


def test_unchecking_one_check_of_an_instance_retires_only_that_checks_cards_rows_and_pass(store, tmp_path):
    # given
    with watching(store, tmp_path) as c:
        sweeper = c.app.state.sweeper
        both_ran(sweeper, NOW)
        before = store.load()

        # when
        c.app.state.instances.set_checks('demo', 'east', ['pulse'], 'alice')
        count = sweeper.retire('demo', 'east', NOW + 60, 'pulse')
        after = store.load()
        lines = [d['text'] for d in store.activity.find({'kind': 'auto'})]

    # then
    found, was = {a['key']: a for a in after['anomalies']}, {a['key']: a for a in before['anomalies']}
    gone = found.pop(pair_key('pulse', 'east'))
    assert count == 1 and gone['status'] == 'cleared' and gone['expired'] is True
    assert gone['note']['text'] == msg('cards.retired_check', plugin='Demo', instance='east', check='Demo pulse')
    assert found == {k: a for k, a in was.items() if k != pair_key('pulse', 'east')} and {a['status'] for a in found.values()} == {'open'}
    assert rows_of(after, 'routes', 'east') == [] and rows_of(after, 'beats', 'east') == ['east-beat'] and rows_of(after, 'services', 'east') == ['east-beat']
    for section in ('services', 'routes', 'beats'):
        assert rows_of(after, section, None) == rows_of(before, section, None) != []
    assert set(after['sources']) == set(after['serverPasses']) == {'pulse', 'beat', 'beat-east'}
    assert [(t['plugin'], t['name'], t.get('check')) for t in after['retired']] == [('demo', 'east', 'pulse')]
    assert lines == [msg('sweeps.retired_check', what='Demo east', check='Demo pulse', count=1)]


def test_inside_the_window_the_other_checks_of_the_instance_still_apply(store, tmp_path):
    # given
    with watching(store, tmp_path) as c:
        sweeper = c.app.state.sweeper
        c.app.state.instances.set_checks('demo', 'east', ['pulse'], 'alice')
        sweeper.retire('demo', 'east', NOW, 'pulse')

        # when
        both_ran(sweeper, NOW + 10)
        state = store.load()

    # then
    assert statuses(state) == {pair_key('pulse', 'main'): 'open', pair_key('beat', 'main'): 'open', pair_key('beat', 'east'): 'open'}
    assert rows_of(state, 'beats', 'east') == rows_of(state, 'services', 'east') == ['east-beat'] and rows_of(state, 'routes', 'east') == []
    assert set(state['sources']) == {'pulse', 'beat', 'beat-east'}


def test_a_run_leaves_out_the_instances_that_do_not_run_the_check(store, tmp_path):
    # given
    with watching(store, tmp_path, names=('main', 'east', 'west')) as c:
        instances = c.app.state.instances
        recorder = Recorder()
        by_run = Sweeper(c.app.state.runtime, store, recorder)
        instances.set_checks('demo', 'main', ['pulse'], 'alice')
        instances.set_checks('demo', 'east', ['pulse'], 'alice')

        # when
        by_run.run('demo', 'pulse', NOW)
        by_run.run('demo', 'beat', NOW)
        instances.set_checks('demo', 'west', ['pulse'], 'alice')
        none = by_run.run('demo', 'pulse', NOW)

    # then
    assert [sorted(r.name for r in results) for results, _ in recorder.calls] == [['pulse-west'], ['beat', 'beat-east', 'beat-west']]
    assert none is None


def test_a_check_retire_on_one_pod_screens_only_that_check_on_a_stale_pod(store, tmp_path):
    # given
    key = master_key(store.db, settings())
    for i, name in enumerate(('main', 'east')):
        add_instance(store, PAIR, name, sealer_key=key, primary=i == 0, url='https://demo.example.com')
    registry = lambda: Registry.of(PAIR, directory=HERE)
    with TestClient(create_app(settings(), store, registry=registry(), kube=Kube(root=tmp_path))) as a, TestClient(create_app(settings(), store, registry=registry(), kube=Kube(root=tmp_path))) as b:
        one, two = a.app.state.sweeper, b.app.state.sweeper

        # when
        a.app.state.instances.set_checks('demo', 'east', ['pulse'], 'alice')
        one.retire('demo', 'east', NOW, 'pulse')
        stale = b.app.state.instances.get('demo', 'east').off_checks
        both_ran(two, NOW + 10)
        state = store.load()
        b.app.state.instances.refresh()
        fresh = b.app.state.instances.get('demo', 'east').off_checks

    # then
    assert (stale, fresh) == (frozenset(), frozenset({'pulse'}))
    assert statuses(state) == {pair_key('pulse', 'main'): 'open', pair_key('beat', 'main'): 'open', pair_key('beat', 'east'): 'open'}
    assert rows_of(state, 'routes', 'east') == [] and rows_of(state, 'beats', 'east') == ['east-beat']
    assert set(state['sources']) == {'pulse', 'beat', 'beat-east'}


def test_a_check_unchecked_during_a_run_contributes_nothing_for_that_instance(store, tmp_path):
    # given
    two_checks_plugin.PLAN['pulse:east'] = {'gate': True}
    with watching(store, tmp_path) as c:
        sweeper = c.app.state.sweeper
        worker = threading.Thread(target=lambda: sweeper.run('demo', 'pulse', NOW))
        worker.start()
        assert two_checks_plugin.STARTED.wait(5)

        # when
        c.app.state.instances.set_checks('demo', 'east', ['pulse'], 'alice')
        two_checks_plugin.RELEASE.set()
        worker.join(5)
        state = store.load()

    # then
    assert statuses(state) == {pair_key('pulse', 'main'): 'open'}
    assert set(state['sources']) == {'pulse'} and rows_of(state, 'routes', 'east') == rows_of(state, 'services', 'east') == []


def test_unchecking_a_check_of_the_primary_closes_its_unscoped_cards_rows_and_pass(store, tmp_path):
    # given
    with watching(store, tmp_path) as c:
        sweeper = c.app.state.sweeper
        both_ran(sweeper, NOW)

        # when
        c.app.state.instances.set_checks('demo', 'main', ['pulse'], 'alice')
        count = sweeper.retire('demo', 'main', NOW + 60, 'pulse')
        both_ran(sweeper, NOW + 900)
        after = store.load()

    # then
    assert count == 1 and statuses(after) == {pair_key('pulse', 'main'): 'cleared', pair_key('pulse', 'east'): 'open', pair_key('beat', 'main'): 'open', pair_key('beat', 'east'): 'open'}
    assert rows_of(after, 'routes', None) == [] and rows_of(after, 'routes', 'east') == ['east-pulse'] and rows_of(after, 'beats', None) == rows_of(after, 'services', None) == ['main-beat']
    assert set(after['sources']) == {'pulse-east', 'beat', 'beat-east'}


def test_a_shared_section_keeps_its_last_writers_rows_until_a_still_watched_check_writes_it_again(store, tmp_path):
    # given
    with watching(store, tmp_path) as c:
        sweeper = c.app.state.sweeper
        both_ran(sweeper, NOW)
        instances = c.app.state.instances

        # when
        instances.set_checks('demo', 'east', ['beat'], 'alice')
        sweeper.retire('demo', 'east', NOW + 60, 'beat')
        kept = store.load()
        sweeper.run('demo', 'pulse', NOW + 900)
        rewritten = store.load()
        instances.set_checks('demo', 'east', ['beat', 'pulse'], 'alice')
        sweeper.retire('demo', 'east', NOW + 960, 'pulse')
        gone = store.load()

    # then
    assert rows_of(kept, 'services', 'east') == ['east-beat'] and rows_of(kept, 'beats', 'east') == [] and rows_of(kept, 'routes', 'east') == ['east-pulse']
    assert rows_of(rewritten, 'services', 'east') == ['east-pulse']
    assert rows_of(gone, 'services', 'east') == rows_of(gone, 'routes', 'east') == rows_of(gone, 'beats', 'east') == []
    assert rows_of(gone, 'services', None) == ['main-pulse']


def test_a_revive_lifts_only_the_tombstones_of_its_own_scope(store, tmp_path):
    # given
    with watching(store, tmp_path) as c:
        sweeper = c.app.state.sweeper
        sweeper.retire('demo', 'east', NOW)
        sweeper.retire('demo', 'east', NOW, 'pulse')
        sweeper.retire('demo', 'east', NOW, 'beat')
        sweeper.retire('demo', 'main', NOW)
        scopes = lambda: sorted((t['name'], t.get('check') or '') for t in store.load()['retired'])

        # when
        sweeper.revive('demo', 'east')
        instance = scopes()
        sweeper.revive('demo', 'east', 'pulse')
        check = scopes()
        sweeper.revive('demo', None)
        plugin = scopes()

    # then
    assert instance == [('east', 'beat'), ('east', 'pulse'), ('main', '')]
    assert check == [('east', 'beat'), ('main', '')]
    assert plugin == [('east', 'beat')]


def test_a_tombstone_with_a_malformed_check_is_skipped_and_cleaned(store, tmp_path):
    # given
    with watching(store, tmp_path) as c:
        sweeper = c.app.state.sweeper
        clock = Tick(10_000_000.0)
        sweeper.clock = clock
        seed_retired(store, [{'plugin': 'demo', 'name': 'east', 'check': 7, 'at': clock.t}, {'plugin': 'demo', 'name': 'east', 'check': ['beat'], 'at': clock.t}])

        # when
        both_ran(sweeper, NOW)
        ran = store.load()
        sweeper.retire('demo', 'east', NOW + 60, 'pulse')
        retired = store.load()['retired']

    # then
    assert set(ran['sources']) == {'pulse', 'pulse-east', 'beat', 'beat-east'}
    assert retired == [{'plugin': 'demo', 'name': 'east', 'check': 'pulse', 'at': clock.t}]


def test_a_plugin_without_fields_whose_instances_all_skip_the_check_runs_nothing(store, tmp_path):
    # given
    solo = minimal('solo', 'Solo')
    add_instance(store, solo, 'main', primary=True)
    store.db['instances'].update_one({'_id': 'solo/main'}, {'$set': {'offChecks': ['solo']}})
    with TestClient(create_app(settings(), store, registry=Registry.of(solo, directory=HERE), kube=Kube(root=tmp_path))) as c:
        recorder = Recorder()

        # when
        result = Sweeper(c.app.state.runtime, store, recorder).run('solo', 'solo', NOW)

    # then
    assert result is None and recorder.calls == []


def test_a_check_retire_needs_an_instance(store, tmp_path):
    # given
    with watching(store, tmp_path) as c:
        sweeper = c.app.state.sweeper
        both_ran(sweeper, NOW)
        before = store.load()

        # when
        count = sweeper.retire('demo', None, NOW + 60, 'pulse')
        after = store.load()

    # then
    assert count == 0 and statuses(after) == statuses(before) and after.get('retired', []) == before.get('retired', []) and set(after['sources']) == set(before['sources'])


def strip_leftovers(store, keep: str) -> None:
    for _ in range(20):
        state = store.load()
        if keep != 'pass':
            for table in ('sources', 'serverPasses'):
                state[table].pop('pulse-east', None)
        if keep != 'card':
            state['anomalies'] = [a for a in state['anomalies'] if a['key'] != pair_key('pulse', 'east')]
        if keep != 'rows':
            state['routes'] = [r for r in state['routes'] if r.get('cluster') != 'east']
        try:
            store.save_state(state, state['rev'])
            return
        except Conflict:
            continue
    raise AssertionError('the leftovers were never stripped: the state kept changing')


@pytest.mark.parametrize('keep', ['pass', 'card', 'rows'])
def test_a_heal_finishes_an_uncheck_whose_retire_never_ran_whatever_was_left_over(store, tmp_path, keep):
    # given
    with watching(store, tmp_path) as c:
        sweeper = c.app.state.sweeper
        both_ran(sweeper, NOW)
        c.app.state.instances.set_checks('demo', 'east', ['pulse'], 'alice')
        strip_leftovers(store, keep)
        before = store.load()

        # when
        sweeper.heal()
        healed = store.load()
        rev = store.rev()['rev']
        sweeper.heal()
        again = store.rev()['rev']

    # then
    found = statuses(healed)
    assert 'pulse-east' not in healed['sources'] and 'pulse-east' not in healed['serverPasses'] and rows_of(healed, 'routes', 'east') == []
    assert found.get(pair_key('pulse', 'east'), 'cleared') == 'cleared' and found[pair_key('beat', 'east')] == found[pair_key('pulse', 'main')] == 'open'
    assert rows_of(healed, 'beats', 'east') == rows_of(before, 'beats', 'east') == ['east-beat'] and set(healed['sources']) == {'pulse', 'beat', 'beat-east'}
    if keep == 'card':
        card = {a['key']: a for a in healed['anomalies']}[pair_key('pulse', 'east')]
        assert card['expired'] is True and card['note']['text'] == msg('cards.retired_check', plugin='Demo', instance='east', check='Demo pulse')
    assert again == rev


def test_a_heal_with_nothing_left_over_writes_nothing(store, tmp_path):
    # given
    with watching(store, tmp_path) as c:
        sweeper = c.app.state.sweeper
        both_ran(sweeper, NOW)
        c.app.state.instances.set_checks('demo', 'east', ['pulse'], 'alice')
        sweeper.retire('demo', 'east', NOW + 60, 'pulse')
        rev, lines = store.rev()['rev'], store.activity.count_documents({})

        # when
        sweeper.heal()

    # then
    assert (store.rev()['rev'], store.activity.count_documents({})) == (rev, lines)
