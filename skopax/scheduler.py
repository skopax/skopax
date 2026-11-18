import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from pymongo.errors import PyMongoError

from skopax.errkind import kind as error_kind
from skopax.store import Store

log = logging.getLogger('skopax.scheduler')

POLL_SECONDS = 60
STALE_S = 300
RETAKES = 2
MAX_PARALLEL = 4
SHUTDOWN_WAIT_S = 30


@dataclass(frozen=True)
class Job:
    name: str
    period: int
    offset: int | Callable[[float], int]
    run: Callable[[float], object]
    enabled: bool | Callable[[], bool] = True
    background: bool = False

    def on(self) -> bool:
        return bool(self.enabled() if callable(self.enabled) else self.enabled)

    def offset_at(self, now: float) -> int:
        return self.offset(now) if callable(self.offset) else self.offset

    def slot(self, now: float) -> int:
        return int((now - self.offset_at(now)) // self.period)

    def due_at(self, now: float) -> float:
        return self.slot(now) * self.period + self.offset_at(now)


class Scheduler:
    def __init__(self, store: Store, jobs: list[Job] | Callable[[], list[Job]], poll: float = POLL_SECONDS, clock: Callable[[], float] = time.time, boot: str | None = None):
        self.store = store
        self._jobs = jobs
        self.poll = poll
        self.clock = clock
        self.boot = boot or uuid.uuid4().hex
        self._nudged: set[str] = set()
        self._waiting: dict[str, float] = {}
        self._guard = threading.Lock()
        self._listed: tuple[str, ...] | None = None
        self._running: dict[str, threading.Thread] = {}
        self._ok: dict[str, bool] = {}
        self._slots = threading.BoundedSemaphore(MAX_PARALLEL)

    @property
    def jobs(self) -> list[Job]:
        listed = self._jobs() if callable(self._jobs) else self._jobs
        return [j for j in listed if j.on()]

    def nudge(self, name: str) -> None:
        with self._guard:
            self._nudged.add(name)

    def queued(self) -> set[str]:
        listed = {j.name for j in self.jobs}
        with self._guard:
            return self._nudged & listed

    def note_jobs(self, jobs: list[Job]) -> None:
        names = tuple(j.name for j in jobs)
        if names != self._listed:
            self._listed = names
            log.info('server jobs: %s', ', '.join(names) or 'none, no integration is configured (set PROMETHEUS_URL)')

    def beat(self) -> None:
        try:
            self.store.beat_run(self.boot, self.clock())
        except PyMongoError as e:
            log.warning('scheduler beat not saved: %s', error_kind(e))

    def claim(self, job: Job, run_id: str, now: float) -> bool:
        if self.store.claim_run(run_id, self.boot):
            return True
        if self.store.retake_run(run_id, self.boot, now - STALE_S, RETAKES):
            log.info('%s: the run of a stopped pod starts again here', job.name)
            return True
        return False

    def run_due(self, join: bool = True) -> list[str]:
        jobs = self.jobs
        self.note_jobs(jobs)
        with self._guard:
            pending = set(self._nudged)
        at = self.clock()
        started = []
        for job in sorted(jobs, key=lambda j: (j.name not in pending, self._waiting.get(j.name, j.due_at(at)))):
            with self._guard:
                busy = self._running.get(job.name)
            if busy is not None and busy.is_alive():
                continue
            takes_slot = not job.background
            if takes_slot and not self._slots.acquire(blocking=join):
                self._waiting.setdefault(job.name, job.due_at(at))
                continue
            self._waiting.pop(job.name, None)
            now = self.clock()
            nudged = job.name in pending
            run_id = f'{job.name}:nudge:{int(now)}' if nudged else f'{job.name}:{job.slot(now)}'
            try:
                won = self.claim(job, run_id, now)
                if won and nudged:
                    self.store.claim_run(f'{job.name}:{job.slot(now)}')
            except PyMongoError as e:
                log.warning('%s run not claimed: %s', job.name, error_kind(e))
                if takes_slot:
                    self._slots.release()
                continue
            if nudged:
                with self._guard:
                    self._nudged.discard(job.name)
            if not won:
                if takes_slot:
                    self._slots.release()
                continue
            worker = threading.Thread(target=self._work, args=(job, run_id, now, takes_slot), name=job.name if job.background else f'job-{job.name}', daemon=True)
            with self._guard:
                self._running[job.name] = worker
            worker.start()
            started.append((job, worker))
        if join:
            for job, worker in started:
                if not job.background:
                    worker.join()
        return [job.name for job, _ in started if job.background or not join or self._ok.pop(job.name, False)]

    def _work(self, job: Job, run_id: str, now: float, takes_slot: bool) -> None:
        try:
            self._ok[job.name] = self.finish(job, run_id, now)
        finally:
            if takes_slot:
                self._slots.release()

    def finish(self, job: Job, run_id: str, now: float) -> bool:
        try:
            job.run(now)
            return True
        except Exception as e:
            log.warning('%s run failed: %s', job.name, error_kind(e))
            return False
        finally:
            try:
                self.store.finish_run(run_id, self.boot)
            except PyMongoError as e:
                log.warning('%s run not marked done: %s', job.name, error_kind(e))

    def drain(self, wait: float = SHUTDOWN_WAIT_S) -> None:
        deadline = time.monotonic() + wait
        with self._guard:
            workers = list(self._running.values())
        for worker in workers:
            left = deadline - time.monotonic()
            if left <= 0:
                return
            worker.join(left)

    async def beats(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.beat)
            except Exception as e:
                log.warning('scheduler beat failed: %s', error_kind(e))
            await asyncio.sleep(self.poll)

    async def loop(self) -> None:
        beating = asyncio.create_task(self.beats())
        try:
            while True:
                await asyncio.sleep(self.poll)
                try:
                    await asyncio.to_thread(self.run_due, False)
                except Exception as e:
                    log.warning('scheduler pass failed: %s', error_kind(e))
        finally:
            beating.cancel()
            await asyncio.to_thread(self.drain)
