import asyncio
import dataclasses
import threading
import time

from fastapi.testclient import TestClient

from skopax import scheduler
from skopax.app import create_app
from skopax.scheduler import Job, Scheduler
from tests.conftest import settings
from tests.fake_anthropic import FakeAnthropic

SLOT = 1_792_224_000.0


def test_drain_waits_for_a_running_job_up_to_the_limit(store):
    # given
    release, done = threading.Event(), []

    def slow(now):
        release.wait(5)
        done.append(1)

    s = Scheduler(store, [Job('slow', 300, 0, slow)], clock=lambda: SLOT)
    s.run_due(join=False)
    began = time.monotonic()
    threading.Timer(0.2, release.set).start()

    # when
    s.drain(5)
    waited = time.monotonic() - began

    # then
    assert done == [1] and 0.15 <= waited < 4


def test_drain_gives_up_after_its_limit(store):
    # given
    release = threading.Event()
    s = Scheduler(store, [Job('stuck', 300, 0, lambda now: release.wait(5))], clock=lambda: SLOT)
    s.run_due(join=False)

    # when
    began = time.monotonic()
    s.drain(0.2)
    waited = time.monotonic() - began
    release.set()
    s.drain(5)

    # then
    assert waited < 2


def test_stopping_the_loop_waits_for_the_running_jobs(store):
    # given
    started, release, done = threading.Event(), threading.Event(), []

    def slow(now):
        started.set()
        release.wait(5)
        done.append(1)

    s = Scheduler(store, [Job('slow', 300, 0, slow)], poll=0.01, clock=lambda: SLOT)

    async def scenario():
        task = asyncio.create_task(s.loop())
        assert await asyncio.to_thread(started.wait, 5)
        threading.Timer(0.2, release.set).start()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return list(done)

    # when
    finished = asyncio.run(scenario())

    # then
    assert finished == [1]
    assert scheduler.SHUTDOWN_WAIT_S == 30


def test_the_app_shutdown_waits_for_the_scheduler_drain_before_it_closes_the_investigator(store):
    # given
    order = []
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode='shadow')
    app = create_app(s, store, anthropic_client=FakeAnthropic([]))

    def slow_drain(wait=5):
        time.sleep(0.3)
        order.append('drained')

    app.state.scheduler.drain = slow_drain
    app.state.investigator.shutdown = lambda: order.append('investigator')

    # when
    with TestClient(app):
        pass

    # then
    assert order == ['drained', 'investigator']
