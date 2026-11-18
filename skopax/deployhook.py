import logging
import math
import threading
import time
from typing import TYPE_CHECKING

from skopax.errkind import kind
from skopax.messages import msg

if TYPE_CHECKING:
    from skopax.runtime import Runtime

log = logging.getLogger('skopax.deployhook')

TEXT_MAX = 2000
FLAGS_MAX = 10
FLAG_MAX = 100


def answer_of(value: object) -> dict | None:
    if not isinstance(value, dict):
        raise TypeError(type(value).__name__)
    wait, text, flags = value.get('wait_until'), value.get('text'), value.get('flags', [])
    if wait is not None and (isinstance(wait, bool) or not isinstance(wait, (int, float)) or not math.isfinite(wait)):
        raise ValueError('wait_until')
    if not isinstance(text, str) or not text.strip():
        raise ValueError('text')
    if not isinstance(flags, (list, tuple)) or len(flags) > FLAGS_MAX or not all(isinstance(f, str) and f.strip() and len(f) <= FLAG_MAX and f.isprintable() for f in flags):
        raise ValueError('flags')
    return {'wait_until': None if wait is None else float(wait), 'text': text.strip()[:TEXT_MAX], 'flags': [f.strip() for f in flags]}


class DeployHooks:
    def __init__(self, runtime: 'Runtime'):
        self.runtime = runtime
        self._guard = threading.Lock()
        self._runs: dict[str, tuple[dict, threading.Event]] = {}
        self._failed: set[str] = set()

    def ask(self, deploys: list[dict], started_at: float, now: float) -> list[dict]:
        hooked = [plugin for plugin, _ in self.runtime.serving() if plugin.deploy_report is not None]
        deadline = time.monotonic() + self.runtime.hook_timeout
        runs = [(plugin, self._start(plugin, deploys, started_at, now)) for plugin in hooked]
        out = []
        for plugin, run in runs:
            try:
                got = self._result(run, deadline)
            except Exception as e:
                self._fail(plugin, kind(e))
                continue
            with self._guard:
                self._failed.discard(plugin.id)
            if got is not None:
                out.append({'id': plugin.id, 'plugin': self.runtime.plugin_name(plugin.id), **got})
        return out

    def _start(self, plugin, deploys: list[dict], started_at: float, now: float):
        box: dict = {}
        done = threading.Event()
        with self._guard:
            held = self._runs.get(plugin.id)
            if held is not None and not held[1].is_set():
                return None
            self._runs[plugin.id] = (box, done)
        found = self.runtime.instances.primary(plugin.id) if plugin.fields else None
        rows = [dict(d) for d in deploys]

        def go() -> None:
            try:
                box['value'] = plugin.deploy_report(self.runtime.context(plugin, found, now), rows, started_at)
            except BaseException as e:
                box['error'] = kind(e)
            finally:
                done.set()

        threading.Thread(target=go, name=f'plugin-deploy-report-{plugin.id}', daemon=True).start()
        return box, done

    def _result(self, run, deadline: float) -> dict | None:
        if run is None or not run[1].wait(max(0.0, deadline - time.monotonic())):
            raise TimeoutError('TimedOut')
        box = run[0]
        if 'error' in box:
            raise RuntimeError(box['error'])
        return None if box.get('value') is None else answer_of(box['value'])

    def _fail(self, plugin, why: str) -> None:
        with self._guard:
            first = plugin.id not in self._failed
            self._failed.add(plugin.id)
        if first:
            log.warning('%s', msg('postdeploy.plugin_failed', plugin=plugin.id, error=why))
