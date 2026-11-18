import asyncio
import threading

from fastapi.testclient import TestClient
from pymongo.errors import AutoReconnect

from tests.conftest import settings
from skopax.app import create_app
from skopax.plugins.jira.client import Jira
from skopax.plugins.gitlab.client import GitLab
from skopax.kube import Kube
from skopax.scheduler import RETAKES, STALE_S, Job, Scheduler
from skopax.store import Store

SLOT = 1_792_224_000.0

SERVER_PASS_PERIOD = 900
PROMETHEUS_OFFSET = 120


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_two_replicas_run_a_job_once_per_slot(store: Store):
    # given
    runs: list[tuple[str, float]] = []
    clock = Clock(SLOT + PROMETHEUS_OFFSET + 5)
    job = lambda pod: Job('cloudflare', SERVER_PASS_PERIOD, PROMETHEUS_OFFSET, lambda now: runs.append((pod, now)))
    pod_a, pod_b = Scheduler(store, [job('a')], clock=clock), Scheduler(Store(store.db), [job('b')], clock=clock)

    # when
    first = [pod_a.run_due(), pod_b.run_due()]
    clock.t += 600
    same_slot = [pod_b.run_due(), pod_a.run_due()]
    clock.t += SERVER_PASS_PERIOD
    next_slot = [pod_b.run_due(), pod_a.run_due()]

    # then
    assert first == [['cloudflare'], []]
    assert same_slot == [[], []]
    assert next_slot == [['cloudflare'], []]
    assert [r[0] for r in runs] == ['a', 'b']


def test_the_slot_starts_at_its_offset():
    # given
    job = Job('cloudflare', SERVER_PASS_PERIOD, PROMETHEUS_OFFSET, lambda now: None)

    # then
    assert job.slot(SLOT + PROMETHEUS_OFFSET - 1) == job.slot(SLOT) == job.slot(SLOT - 1)
    assert job.slot(SLOT + PROMETHEUS_OFFSET) == job.slot(SLOT) + 1


def test_a_failing_job_does_not_stop_the_others_and_disabled_jobs_never_run(store: Store):
    # given
    runs: list[str] = []

    def boom(now: float) -> None:
        raise RuntimeError('Cloudflare API healthchecks HTTP 403')

    s = Scheduler(store, [Job('a', 300, 0, boom), Job('b', 300, 0, lambda now: runs.append('b')), Job('c', 300, 0, lambda now: runs.append('c'), enabled=False),
                          Job('d', 300, 0, lambda now: runs.append('d'), enabled=lambda: False)], clock=Clock(SLOT))

    # when
    ran = s.run_due()

    # then
    assert ran == ['b']
    assert runs == ['b']
    assert [j.name for j in s.jobs] == ['a', 'b']


def test_a_job_switch_given_as_a_function_is_read_again_at_every_run_due(store: Store):
    # given
    runs: list[str] = []
    switched = {'on': False}
    clock = Clock(SLOT)
    s = Scheduler(store, [Job('a', 300, 0, lambda now: runs.append('a'), enabled=lambda: switched['on'])], clock=clock)

    # when
    while_off = s.run_due()
    switched['on'] = True
    turned_on = s.run_due()
    clock.t += 300
    switched['on'] = False
    off_again = s.run_due()

    # then
    assert (while_off, turned_on, off_again) == ([], ['a'], [])
    assert runs == ['a']


def test_a_run_cut_short_by_a_restart_starts_again_on_another_pod_once_the_old_pod_stops_beating(store: Store):
    # given
    clock = Clock(SLOT + 5)
    started, release, runs = threading.Event(), threading.Event(), []

    def long_scan(now: float) -> None:
        started.set()
        release.wait(5)

    old = Scheduler(store, [Job('pagescan', 6 * 3600, 0, long_scan, background=True)], clock=clock, boot='old')
    new = Scheduler(Store(store.db), [Job('pagescan', 6 * 3600, 0, lambda now: runs.append(now))], clock=clock, boot='new')
    third = Scheduler(Store(store.db), [Job('pagescan', 6 * 3600, 0, lambda now: runs.append(-1))], clock=clock, boot='third')
    old.beat()

    # when
    first = old.run_due()
    started.wait(5)
    clock.t += 60
    new.beat()
    while_old_beats = new.run_due()
    clock.t += STALE_S
    new.beat()
    after_old_stopped = new.run_due()
    third.beat()
    later = [new.run_due(), third.run_due()]
    release.set()
    for _ in range(50):
        if not any(t.name == 'pagescan' for t in threading.enumerate()):
            break
        threading.Event().wait(0.02)
    claim = store.runs.find_one({'_id': f'pagescan:{Job("pagescan", 6 * 3600, 0, long_scan).slot(clock.t)}'})

    # then
    assert first == ['pagescan']
    assert while_old_beats == []
    assert after_old_stopped == ['pagescan']
    assert later == [[], []]
    assert runs == [SLOT + 5 + 60 + STALE_S]
    assert (claim['boot'], claim['done'], claim['retakes']) == ('new', True, 1)


def test_a_finished_run_or_one_claimed_before_this_version_is_not_run_again_after_a_deploy(store: Store):
    # given
    clock = Clock(SLOT + 5)
    runs: list[str] = []
    old = Scheduler(store, [Job('self-review', 86400, 0, lambda now: runs.append('old'))], clock=clock, boot='old')
    old.beat()
    old.run_due()
    scan = Job('pagescan', 6 * 3600, 0, lambda now: runs.append('scan'))
    store.claim_run(f'pagescan:{scan.slot(clock.t)}')
    new = Scheduler(Store(store.db), [Job('self-review', 86400, 0, lambda now: runs.append('new')), scan], clock=clock, boot='new')

    # when
    clock.t += STALE_S + 60
    new.beat()
    ran = new.run_due()

    # then
    assert ran == []
    assert runs == ['old']


def test_a_run_that_keeps_stopping_its_pod_is_taken_over_at_most_twice(store: Store):
    # given
    clock = Clock(SLOT + 5)
    run_id = f"pagescan:{Job('pagescan', 6 * 3600, 0, lambda now: None).slot(clock.t)}"
    store.claim_run(run_id, 'pod-0')

    # when
    taken = []
    for i in range(1, 5):
        clock.t += STALE_S + 1
        taken.append(store.retake_run(run_id, f'pod-{i}', clock.t - STALE_S, RETAKES))

    # then
    assert RETAKES == 2
    assert taken == [True, True, False, False]


def test_without_tokens_the_app_starts_no_server_job(store: Store, tmp_path):
    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path))) as c:
        info = c.get('/api/server').json()
        jobs = c.app.state.scheduler.jobs

    # then
    assert jobs == []
    assert {k: v for k, v in info.items() if k not in ('displayName', 'product', 'version')} == {'enrich': {'gitlab': False, 'jira': False}, 'settle': False, 'jiraLinks': False, 'postdeploy': False, 'push': False, 'plugins': [],
                    'kube': False, 'slack': {'listen': 'off', 'socket': False}, 'investigate': {'mode': 'off', 'enabled': False, 'model': 'claude-opus-5-5', 'effort': 'medium',
                    'maxToolCalls': 30, 'rooms': 2, 'slackModel': 'claude-sonnet-5-5', 'slackEffort': 'medium', 'slackMaxToolCalls': 10, 'stopped': None, 'spentToday': 0.0, 'cap': 25.0, 'lastRun': None},
                    'passes': {}, 'pushes': [], 'tz': 'UTC', 'probe': False, 'reach': None}


def test_each_token_switches_on_only_its_own_job(store: Store, tmp_path):
    # when
    with TestClient(create_app(settings(), store, gitlab=GitLab('', group='acme'), jira=Jira('bot@example.com', 'jt', site='https://jira.example.com'), kube=Kube(root=tmp_path))) as c:
        names = [j.name for j in c.app.state.scheduler.jobs]
        info = c.get('/api/server').json()

    # then
    assert names == ['enrich', 'jira-links']
    assert (info['enrich'], info['settle']) == ({'gitlab': False, 'jira': True}, False)


def test_the_job_list_can_be_a_function_read_again_at_every_poll(store: Store):
    # given
    runs: list[str] = []
    listed = [Job('a', 300, 0, lambda now: runs.append('a'))]
    s = Scheduler(store, lambda: list(listed), clock=Clock(SLOT))

    # when
    first = s.run_due()
    listed.append(Job('b', 300, 0, lambda now: runs.append('b')))
    listed.append(Job('c', 300, 0, lambda now: runs.append('c'), enabled=False))
    second = s.run_due()

    # then
    assert (first, second) == (['a'], ['b'])
    assert [j.name for j in s.jobs] == ['a', 'b']


def test_a_nudge_survives_a_claim_error_and_runs_at_the_next_poll(store: Store):
    # given
    runs: list[float] = []
    s = Scheduler(store, [Job('demo.pulse', 900, 0, lambda now: runs.append(now))], clock=Clock(SLOT + 5))
    s.run_due()
    claim = store.claim_run
    failures = [1]

    def flaky(name, boot=None):
        if failures:
            failures.pop()
            raise AutoReconnect('down')
        return claim(name, boot)

    store.claim_run = flaky
    s.nudge('demo.pulse')

    # when
    lost = s.run_due()
    later = s.run_due()
    after = s.run_due()

    # then
    assert (lost, later, after) == ([], ['demo.pulse'], [])
    assert len(runs) == 2


def test_a_nudged_run_takes_the_slot_so_the_regular_run_does_not_follow(store: Store):
    # given
    runs: list[float] = []
    s = Scheduler(store, [Job('demo.pulse', 900, 0, lambda now: runs.append(now))], clock=Clock(SLOT + 5))
    s.nudge('demo.pulse')

    # when
    nudged = s.run_due()
    regular = s.run_due()

    # then
    assert (nudged, regular) == (['demo.pulse'], []) and len(runs) == 1


def test_a_nudge_for_a_job_that_is_not_listed_yet_waits_for_it(store: Store):
    # given
    runs: list[float] = []
    listed: list[Job] = []
    s = Scheduler(store, lambda: list(listed), clock=Clock(SLOT + 5))
    s.nudge('demo.pulse')

    # when
    first = s.run_due()
    listed.append(Job('demo.pulse', 900, 0, lambda now: runs.append(now)))
    second = s.run_due()

    # then
    assert (first, second) == ([], ['demo.pulse'])


def test_due_jobs_run_side_by_side_so_a_stuck_one_delays_no_other(store: Store):
    # given
    stuck, order = threading.Event(), []

    def slow(now: float) -> None:
        stuck.wait(5)
        order.append('slow')

    def quick(now: float) -> None:
        order.append('quick')
        stuck.set()

    s = Scheduler(store, [Job('slow', 300, 0, slow), Job('quick', 300, 0, quick)], clock=Clock(SLOT))

    # when
    ran = s.run_due()

    # then
    assert order == ['quick', 'slow']
    assert ran == ['slow', 'quick']


def test_at_most_four_due_jobs_run_at_once(store: Store):
    # given
    live, peak, guard, four = [0], [0], threading.Lock(), threading.Event()

    def job(now: float) -> None:
        with guard:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
            if live[0] == 4:
                four.set()
        four.wait(5)
        threading.Event().wait(0.05)
        with guard:
            live[0] -= 1

    s = Scheduler(store, [Job(f'j{i}', 300, 0, job) for i in range(9)], clock=Clock(SLOT))

    # when
    ran = s.run_due()

    # then
    assert len(ran) == 9 and 1 < peak[0] <= 4


def test_a_lost_nudge_claim_consumes_the_nudge(store: Store):
    # given
    runs: list[float] = []
    s = Scheduler(store, [Job('demo.pulse', 900, 0, lambda now: runs.append(now))], clock=Clock(SLOT + 5))
    s.run_due()
    taken = f'demo.pulse:nudge:{int(SLOT + 5)}'
    store.claim_run(taken)
    s.nudge('demo.pulse')

    # when
    lost = s.run_due()
    store.runs.delete_one({'_id': taken})
    later = s.run_due()

    # then
    assert (lost, later) == ([], []) and len(runs) == 1


def test_a_poll_does_not_wait_for_a_slow_job_and_a_running_job_is_skipped(store: Store):
    # given
    stuck, runs = threading.Event(), []

    def slow(now: float) -> None:
        runs.append('slow')
        stuck.wait(5)

    clock = Clock(SLOT)
    s = Scheduler(store, [Job('slow', 300, 0, slow), Job('quick', 300, 0, lambda now: runs.append('quick'))], clock=clock)

    # when
    first = s.run_due(join=False)
    s._running['quick'].join(5)
    clock.t += 300
    second = s.run_due(join=False)
    stuck.set()
    for worker in list(s._running.values()):
        worker.join(5)
    clock.t += 300
    third = s.run_due(join=False)
    for worker in list(s._running.values()):
        worker.join(5)

    # then
    assert first == ['slow', 'quick'] and second == ['quick'] and third == ['slow', 'quick']
    assert runs.count('slow') == 2 and runs.count('quick') == 3


def test_a_full_set_of_running_jobs_leaves_the_rest_for_the_next_poll(store: Store):
    # given
    release, started = threading.Event(), []

    def hold(now: float) -> None:
        started.append(now)
        release.wait(5)

    clock = Clock(SLOT)
    s = Scheduler(store, [Job(f'j{i}', 300, 0, hold) for i in range(6)], clock=clock)

    # when
    first = s.run_due(join=False)
    again = s.run_due(join=False)
    release.set()
    for worker in list(s._running.values()):
        worker.join(5)
    rest = s.run_due(join=False)
    for worker in list(s._running.values()):
        worker.join(5)

    # then
    assert len(first) == 4 and again == [] and sorted(first + rest) == [f'j{i}' for i in range(6)]


def test_the_loop_polls_without_waiting_for_the_jobs_it_starts(store: Store):
    # given
    calls: list[bool] = []
    polled = threading.Event()
    s = Scheduler(store, [], poll=0.01, clock=Clock(SLOT))
    s.run_due = lambda join=True: calls.append(join) or polled.set() or []

    async def go():
        loop = asyncio.create_task(s.loop())
        await asyncio.to_thread(polled.wait, 5)
        loop.cancel()

    # when
    asyncio.run(go())

    # then
    assert calls and set(calls) == {False}


def test_a_nudged_job_starts_first_and_reads_queued_until_a_slot_is_free(store: Store):
    # given
    release = threading.Event()
    clock = Clock(SLOT)
    s = Scheduler(store, [Job(f'j{i}', 300, 0, lambda now: release.wait(5)) for i in range(6)], clock=clock)
    first = s.run_due(join=False)
    s.nudge('j5')
    s.nudge('gone')

    # when
    waiting = s.queued()
    again = s.run_due(join=False)
    still = s.queued()
    release.set()
    for worker in list(s._running.values()):
        worker.join(5)
    clock.t += 300
    later = s.run_due(join=False)
    for worker in list(s._running.values()):
        worker.join(5)

    # then
    assert first == ['j0', 'j1', 'j2', 'j3'] and again == []
    assert waiting == still == {'j5'}, 'a nudge for a job that is not listed is not shown as queued'
    assert later[0] == 'j5' and s.queued() == set()


def test_the_job_due_longest_starts_first_when_slots_are_short(store: Store, monkeypatch):
    # given
    from skopax import scheduler
    monkeypatch.setattr(scheduler, 'MAX_PARALLEL', 1)
    release = threading.Event()
    s = Scheduler(store, [Job('fresh', 300, 290, lambda now: release.wait(5)), Job('old', 900, 0, lambda now: release.wait(5))], clock=Clock(SLOT + 295))

    # when
    first = s.run_due(join=False)
    release.set()
    for worker in list(s._running.values()):
        worker.join(5)

    # then
    assert first == ['old']


def test_a_job_left_without_a_slot_keeps_its_place_by_how_long_it_has_waited(store: Store, monkeypatch):
    # given
    from skopax import scheduler
    monkeypatch.setattr(scheduler, 'MAX_PARALLEL', 1)
    release = threading.Event()
    clock = Clock(SLOT + 10)
    later = Job('later', 900, 200, lambda now: None)
    store.claim_run(f'later:{later.slot(SLOT + 10)}')
    s = Scheduler(store, [Job('long', 900, 0, lambda now: release.wait(5)), Job('short', 300, 0, lambda now: None), later], clock=clock)
    first = s.run_due(join=False)
    release.set()
    for worker in list(s._running.values()):
        worker.join(5)

    # when
    clock.t = SLOT + 310
    second = s.run_due(join=False)

    # then
    assert first == ['long'] and second[0] == 'short', 'short waited since the first poll, later became due after it'
