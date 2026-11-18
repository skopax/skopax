import threading
import time
from collections.abc import Callable

HOOK_TIMEOUT_S = 10


class TimedOut(Exception):
    pass


class Busy(TimedOut):
    pass


class Bound:
    def __init__(self, name: str, clock: Callable[[], float] = time.monotonic):
        self.name = name
        self.clock = clock
        self._guard = threading.Lock()
        self._busy: dict[object, threading.Event] = {}
        self._since: dict[object, float] = {}

    def call(self, key: object, fn: Callable[[], object], timeout: float, wait: bool = False, stuck: float | None = None) -> object:
        end = self.clock() + timeout if wait else None
        done = threading.Event()
        box: dict = {}
        while True:
            with self._guard:
                held = self._busy.get(key)
                if held is None or held.is_set():
                    self._busy[key] = done
                    if wait:
                        self._since[key] = self.clock()
                    break
                since = self._since.get(key)
            if not wait:
                raise TimedOut()
            if stuck is not None and since is not None and self.clock() - since > stuck:
                raise Busy()
            if not held.wait(max(0.0, end - self.clock())):
                raise Busy()

        def go() -> None:
            try:
                box['value'] = fn()
            except BaseException as e:
                box['error'] = e
            finally:
                done.set()

        threading.Thread(target=go, name=self.name, daemon=True).start()
        if not done.wait(max(0.0, end - self.clock()) if wait else timeout):
            raise TimedOut()
        if 'error' in box:
            raise box['error']
        return box.get('value')
