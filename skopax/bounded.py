import threading
from collections.abc import Callable

HOOK_TIMEOUT_S = 10


class TimedOut(Exception):
    pass


class Bound:
    def __init__(self, name: str):
        self.name = name
        self._guard = threading.Lock()
        self._busy: dict[object, threading.Event] = {}

    def call(self, key: object, fn: Callable[[], object], timeout: float) -> object:
        done = threading.Event()
        box: dict = {}
        with self._guard:
            held = self._busy.get(key)
            if held is not None and not held.is_set():
                raise TimedOut()
            self._busy[key] = done

        def go() -> None:
            try:
                box['value'] = fn()
            except BaseException as e:
                box['error'] = e
            finally:
                done.set()

        threading.Thread(target=go, name=self.name, daemon=True).start()
        if not done.wait(timeout):
            raise TimedOut()
        if 'error' in box:
            raise box['error']
        return box.get('value')
