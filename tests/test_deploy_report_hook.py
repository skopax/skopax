import dataclasses
import logging
import threading

from skopax.contract import Plugin
from skopax.instances import Instances, PluginState
from skopax.postdeploy import PostDeploy, WAIT_WINDOW_S
from skopax.registry import Registry
from skopax.runtime import Runtime
from skopax.sealer import Sealer
from skopax.store import Store
from tests.plugins.base import minimal
from tests.test_admin_plugins import HERE
from tests.test_postdeploy import FakeJira, NOW, at, only, prom, settled_action


def hooked(id: str, hook) -> Plugin:
    return dataclasses.replace(minimal(id, id.title()), deploy_report=hook)


def runtime_of(store: Store, *plugins: Plugin, timeout: float = 5.0) -> Runtime:
    registry = Registry.of(*plugins)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer('k')), None)
    runtime.hook_timeout = timeout
    runtime.refresh()
    return runtime


def deployed(store: Store, requested_ago: float = 0) -> dict:
    store.add_deploys([{'ns': 'shop', 'svc': 'checkout-service', 'fromImage': 'aaaaaaaa', 'toImage': 'bbbbbbbb', 'at': at(1800)}])
    act = settled_action(store, 1800, 1800)
    store.actions.update_one({'_id': act['id']}, {'$set': {'requestedAt': at(requested_ago)}})
    return store.get_action(act['id'])


def run(store: Store, runtime: Runtime | None, now: float = NOW):
    reports = None if runtime is None else runtime.deploy_reports
    return PostDeploy(store, only(prom({})), FakeJira([], enabled=False), clock=lambda: now, plugin_reports=reports).run_once(now)


def test_a_deploy_report_hook_adds_its_text_and_flags_to_the_post_deploy_report(store: Store):
    # given
    seen = []

    def tell(ctx, deploys, started_at):
        seen.append(([d['svc'] for d in deploys], started_at, ctx.plugin.id))
        return {'wait_until': None, 'text': 'Cache warmed in 40 s.', 'flags': ['cache cold']}

    deployed(store)
    runtime = runtime_of(store, hooked('warm', tell), hooked('quiet', lambda ctx, deploys, started_at: None))

    # when
    saved = run(store, runtime)
    report = store.deploy_report(saved['id'])

    # then
    assert seen == [(['checkout-service'], NOW - 1800, 'warm')]
    assert report['plugins'] == {'warm': 'Cache warmed in 40 s.'}
    assert report['flags'] == ['Warm: cache cold']
    assert report['brief']['level'] == 'warn' and 'Warm: cache cold' in report['brief']['points']
    assert 'Warm' in report['text'] and 'Cache warmed in 40 s.' in report['text']
    assert 'plugins' not in report or 'quiet' not in report['plugins']
    assert 'Warm: cache cold' in report['slackDraft']['text'], 'the Slack draft shows the plug-in flags too'


def test_the_report_waits_for_the_latest_wait_until_capped_by_the_core_window(store: Store):
    # given
    act = deployed(store)
    early = hooked('early', lambda ctx, deploys, started_at: {'wait_until': NOW + 60, 'text': 'early', 'flags': []})
    late = hooked('late', lambda ctx, deploys, started_at: {'wait_until': NOW + 600, 'text': 'late', 'flags': []})
    far = hooked('far', lambda ctx, deploys, started_at: {'wait_until': NOW + 10 * WAIT_WINDOW_S, 'text': 'far', 'flags': []})
    runtime = runtime_of(store, early, late)

    # when
    first = run(store, runtime)
    waiting = store.get_action(act['id'])['status']
    midway = run(store, runtime, NOW + 300)
    after = run(store, runtime, NOW + 601)
    deployed(store)
    capped_runtime = runtime_of(store, far)
    started = run(store, capped_runtime)
    held = run(store, capped_runtime, NOW + WAIT_WINDOW_S - 1)
    released = run(store, capped_runtime, NOW + WAIT_WINDOW_S + 1)

    # then
    assert first is None and midway is None and waiting == 'requested'
    assert after is not None and set(after['plugins']) == {'early', 'late'}
    assert started is None and held is None, 'a wait_until far beyond the window still stops at the window'
    assert released is not None and set(released['plugins']) == {'far'}


def test_a_hook_that_raises_or_answers_malformed_or_late_leaves_the_report_as_it_was(store: Store, caplog):
    # given
    def boom(ctx, deploys, started_at):
        raise RuntimeError('no')

    gate = threading.Event()

    def slow(ctx, deploys, started_at):
        gate.wait(5)
        return {'wait_until': None, 'text': 'too late', 'flags': ['late']}

    deployed(store)
    answers = [boom, slow, lambda *a: 'text', lambda *a: {'wait_until': 'soon', 'text': 'x', 'flags': []}, lambda *a: {'wait_until': None, 'text': 'x', 'flags': 'one'},
               lambda *a: {'wait_until': None, 'text': 7, 'flags': []}, lambda *a: {'wait_until': float('inf'), 'text': 'x', 'flags': []}]
    runtime = runtime_of(store, *[hooked(f'bad{i}', hook) for i, hook in enumerate(answers)], timeout=0.2)
    plain = run(store, None)
    deployed(store)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.deployhook'):
        saved = run(store, runtime)
        deployed(store)
        again = run(store, runtime)
    gate.set()
    warned = [r.getMessage() for r in caplog.records if 'deploy report' in r.getMessage()]

    # then
    assert 'plugins' not in saved and 'flags' not in saved
    assert saved['text'] == plain['text'] and saved['brief']['level'] == plain['brief']['level'] and saved['summary'] == plain['summary']
    assert len(warned) == len(answers), warned
    assert again is not None and 'plugins' not in again and len(warned) == len(answers), 'each plug-in is logged once'


def test_an_older_report_that_waits_never_delays_a_newer_one(store: Store):
    # given
    older = deployed(store, requested_ago=120)
    newer = deployed(store)
    asked = []

    def hook(ctx, deploys, started_at):
        asked.append(len(asked))
        return {'wait_until': NOW + 86400, 'text': 'still warming', 'flags': []} if len(asked) == 1 else None

    runtime = runtime_of(store, hooked('warm', hook))

    # when
    saved = run(store, runtime)

    # then
    assert saved is not None and saved['actionId'] == newer['id']
    assert store.get_action(older['id'])['status'] == 'requested'
    assert store.get_action(older['id'])['requestedAt'] == at(120), 'handing the claim back keeps the request time'
    assert store.get_action(newer['id'])['status'] == 'done'


def test_the_wait_cap_holds_when_the_wait_mark_cannot_be_saved(store: Store, monkeypatch):
    # given
    act = deployed(store)
    far = hooked('far', lambda ctx, deploys, started_at: {'wait_until': NOW + 10 * WAIT_WINDOW_S, 'text': 'far', 'flags': []})
    runtime = runtime_of(store, far)

    def broken(*_a, **_k):
        raise RuntimeError('write refused')

    monkeypatch.setattr(store, 'set_action_fields', broken)

    # when
    held = [run(store, runtime, NOW + t) for t in (0, 600, WAIT_WINDOW_S - 1)]
    released = run(store, runtime, NOW + WAIT_WINDOW_S + 1)

    # then
    assert held == [None, None, None]
    assert released is not None and released['actionId'] == act['id']


def test_the_hooks_are_asked_only_for_the_action_this_pod_claimed(store: Store, monkeypatch):
    # given
    act = deployed(store)
    asked = []
    runtime = runtime_of(store, hooked('warm', lambda ctx, deploys, started_at: asked.append(1) or None))
    real = store.update_action

    def taken(action_id, status, result, expect, report=None):
        if status == 'processing':
            store.actions.update_one({'_id': action_id}, {'$set': {'status': 'processing'}})
        return real(action_id, status, result, expect, report)

    monkeypatch.setattr(store, 'update_action', taken)

    # when
    saved = run(store, runtime)

    # then
    assert saved is None and asked == [], 'another pod took the action, so this one never asks the hooks'
    assert store.get_action(act['id'])['status'] == 'processing'


def test_a_deploy_report_that_is_not_callable_breaks_the_plugin():
    # when
    found = Registry.of(minimal('shop', 'Shop', deploy_report='report it'), directory=HERE).get('shop')

    # then
    assert found.state == 'broken' and 'deploy_report' in found.reason


def test_the_report_results_are_keyed_by_plugin_id_so_two_plugins_with_one_name_both_count(store: Store):
    # given
    deployed(store)
    one = dataclasses.replace(minimal('warm-a', 'Warm'), deploy_report=lambda ctx, deploys, started_at: {'wait_until': None, 'text': 'first', 'flags': []})
    two = dataclasses.replace(minimal('warm-b', 'Warm'), deploy_report=lambda ctx, deploys, started_at: {'wait_until': None, 'text': 'second', 'flags': []})
    runtime = runtime_of(store, one, two)

    # when
    saved = run(store, runtime)

    # then
    assert saved['plugins'] == {'warm-a': 'first', 'warm-b': 'second'}
