import hashlib
import logging
import re
import threading
import time
from collections.abc import Callable

import anthropic

from skopax.adminsecret import REFRESH_S, AdminSecret
from skopax.errkind import kind
from skopax.messages import msg
from skopax.sealer import Sealer

KEY_ID = 'anthropic.api-key'
KEY = re.compile(r'[A-Za-z0-9_-]{20,400}')
TEST_TIMEOUT_S = 10
TEST_CAP_S = 15
FAILURES = ((anthropic.AuthenticationError, 'aikey.test_rejected'), (anthropic.PermissionDeniedError, 'aikey.test_forbidden'), (anthropic.NotFoundError, 'aikey.test_no_model'),
            (anthropic.APITimeoutError, 'aikey.test_timeout'), (anthropic.APIConnectionError, 'aikey.test_unreachable'))

log = logging.getLogger('skopax.aikey')


class InvalidKey(ValueError):
    def __init__(self):
        super().__init__(msg('aikey.invalid'))


class TestBusy(Exception):
    def __init__(self):
        super().__init__(msg('aikey.test_busy'))


def checked(value: str) -> str:
    key = value.strip()
    if not KEY.fullmatch(key):
        raise InvalidKey()
    return key


def summary(error: BaseException | None, model: str) -> str:
    if error is None:
        return msg('aikey.test_ok', model=model)
    found = next((key for cls, key in FAILURES if isinstance(error, cls)), None)
    if found is not None:
        return msg(found, model=model)
    if isinstance(error, anthropic.APIStatusError):
        return msg('aikey.test_status', status=error.status_code)
    return msg('aikey.test_failed')


class AiKey(AdminSecret):
    def __init__(self, db, sealer: Callable[[], Sealer], fallback: Callable[[], str], stored_key: Callable[[], str | None], tunables,
                 factory: Callable[..., object] | None = None, clock: Callable[[], float] = time.monotonic, every: float = REFRESH_S):
        super().__init__(db, KEY_ID, sealer, stored_key, tunables, 'Anthropic key', lambda: msg('aikey.unreadable'), log, every)
        self.fallback = fallback
        self.clock = clock
        self.factory = factory or anthropic.Anthropic
        self._testing: threading.Thread | None = None
        self._test_guard = threading.Lock()

    def current(self) -> str:
        held = self.held
        return held.value if held.value is not None else self.fallback()

    def status(self) -> dict:
        held = self.held
        admin, config = held.value is not None, bool(self.fallback())
        using = 'admin' if admin else 'config' if config else None
        return {'using': using, 'admin': admin, 'config': config, 'unreadable': held.unreadable, 'updatedBy': held.by, 'updatedAt': held.at,
                'note': msg('aikey.from_config') if using == 'config' else None}

    def set(self, value: str, by: str) -> dict:
        return super().set(checked(value), by)

    def test(self, value: str | None, model: str, used: Callable[[], None] | None = None) -> dict:
        typed = checked(value) if value and value.strip() else ''
        key = typed or self.current()
        if not key:
            return {'ok': False, 'summary': msg('aikey.test_no_key'), 'ms': 0}
        box: dict = {}
        done = threading.Event()

        def go() -> None:
            try:
                with self.factory(api_key=key, timeout=TEST_TIMEOUT_S, max_retries=0) as client:
                    client.models.retrieve(model)
            except BaseException as e:
                box['error'] = e
            finally:
                with self._test_guard:
                    if self._testing is threading.current_thread():
                        self._testing = None
                done.set()

        with self._test_guard:
            if self._testing is not None and self._testing.is_alive():
                raise TestBusy()
            if not typed and used is not None:
                used()
            began = self.clock()
            self._testing = threading.Thread(target=go, name='aikey-test', daemon=True)
            self._testing.start()
        cap = TEST_CAP_S
        if not done.wait(cap):
            log.info('the Anthropic connection test did not finish within %g s', cap)
            return {'ok': False, 'summary': msg('aikey.test_timeout'), 'ms': int(cap * 1000)}
        error = box.get('error')
        if error is not None:
            log.info('the Anthropic connection test failed: %s', kind(error))
        return {'ok': error is None, 'summary': summary(error, model), 'ms': max(0, int((self.clock() - began) * 1000))}


class ClientBox:
    def __init__(self, key: Callable[[], str], make: Callable[[str], object], client: object | None = None):
        self.key = key
        self.make = make
        self.client = client
        self._built: object | None = None
        self._digest: str | None = None
        self._lock = threading.Lock()

    @property
    def injected(self) -> bool:
        return self.client is not None

    def get(self) -> object | None:
        if self.client is not None:
            return self.client
        key = self.key()
        if not key:
            return None
        digest = hashlib.sha256(key.encode()).hexdigest()[:16]
        with self._lock:
            if digest != self._digest:
                self._built, self._digest = self.make(key), digest
            return self._built
