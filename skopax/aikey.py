import hashlib
import logging
import re
import threading
import time
from collections.abc import Callable

import anthropic
import openai

from skopax.adminsecret import REFRESH_S, AdminSecret
from skopax.errkind import kind
from skopax.messages import msg
from skopax.sealer import Sealer

KEY_ID = 'anthropic.api-key'
OPENAI_KEY_ID = 'openai.api-key'
KEY = re.compile(r'[A-Za-z0-9_-]{20,400}')
TEST_TIMEOUT_S = 10
TEST_CAP_S = 15
FAILURES = ((anthropic.AuthenticationError, 'aikey.test_rejected'), (anthropic.PermissionDeniedError, 'aikey.test_forbidden'), (anthropic.NotFoundError, 'aikey.test_no_model'),
            (anthropic.APITimeoutError, 'aikey.test_timeout'), (anthropic.APIConnectionError, 'aikey.test_unreachable'))
OPENAI_FAILURES = ((openai.AuthenticationError, 'openaikey.test_rejected'), (openai.PermissionDeniedError, 'openaikey.test_forbidden'), (openai.NotFoundError, 'openaikey.test_no_model'),
                   (openai.APITimeoutError, 'openaikey.test_timeout'), (openai.APIConnectionError, 'openaikey.test_unreachable'))
KINDS = {'anthropic': (KEY_ID, 'aikey.', 'Anthropic key', 'Anthropic', FAILURES, anthropic.APIStatusError),
         'openai': (OPENAI_KEY_ID, 'openaikey.', 'OpenAI key', 'OpenAI', OPENAI_FAILURES, openai.APIStatusError)}

log = logging.getLogger('skopax.aikey')


class InvalidKey(ValueError):
    def __init__(self, prefix: str = 'aikey.'):
        super().__init__(msg(prefix + 'invalid'))


class TestBusy(Exception):
    def __init__(self, prefix: str = 'aikey.'):
        super().__init__(msg(prefix + 'test_busy'))


def checked(value: str, prefix: str = 'aikey.') -> str:
    key = value.strip()
    if not KEY.fullmatch(key):
        raise InvalidKey(prefix)
    return key


def summary(error: BaseException | None, model: str, provider: str = 'anthropic') -> str:
    _, prefix, _, _, failures, status_error = KINDS[provider]
    if error is None:
        return msg(prefix + 'test_ok', model=model)
    found = next((key for cls, key in failures if isinstance(error, cls)), None)
    if found is not None:
        return msg(found, model=model)
    if isinstance(error, status_error):
        return msg(prefix + 'test_status', status=error.status_code)
    return msg(prefix + 'test_failed')


class AiKey(AdminSecret):
    def __init__(self, db, sealer: Callable[[], Sealer], fallback: Callable[[], str], stored_key: Callable[[], str | None], tunables,
                 factory: Callable[..., object] | None = None, clock: Callable[[], float] = time.monotonic, every: float = REFRESH_S, provider: str = 'anthropic'):
        key_id, self.prefix, what, self.label, _, _ = KINDS[provider]
        super().__init__(db, key_id, sealer, stored_key, tunables, what, lambda: msg(self.prefix + 'unreadable'), log, every)
        self.provider = provider
        self.fallback = fallback
        self.clock = clock
        self.factory = factory or (openai.OpenAI if provider == 'openai' else anthropic.Anthropic)
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
                'note': msg(self.prefix + 'from_config') if using == 'config' else None}

    def set(self, value: str, by: str) -> dict:
        return super().set(checked(value, self.prefix), by)

    def test(self, value: str | None, model: str, used: Callable[[], None] | None = None) -> dict:
        typed = checked(value, self.prefix) if value and value.strip() else ''
        key = typed or self.current()
        if not key:
            return {'ok': False, 'summary': msg(self.prefix + 'test_no_key'), 'ms': 0}
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
                raise TestBusy(self.prefix)
            if not typed and used is not None:
                used()
            began = self.clock()
            self._testing = threading.Thread(target=go, name='aikey-test', daemon=True)
            self._testing.start()
        cap = TEST_CAP_S
        if not done.wait(cap):
            log.info('the %s connection test did not finish within %g s', self.label, cap)
            return {'ok': False, 'summary': msg(self.prefix + 'test_timeout'), 'ms': int(cap * 1000)}
        error = box.get('error')
        if error is not None:
            log.info('the %s connection test failed: %s', self.label, kind(error))
        return {'ok': error is None, 'summary': summary(error, model, self.provider), 'ms': max(0, int((self.clock() - began) * 1000))}


class AiKeys:
    def __init__(self, provider: Callable[[], str], keys: dict[str, AiKey]):
        self.provider = provider
        self.keys = keys

    def active(self) -> AiKey:
        return self.keys.get(self.provider()) or self.keys['anthropic']

    def current(self) -> str:
        return self.active().current()


class ClientBox:
    def __init__(self, key: Callable[[], str], make: Callable[[str], object], client: object | None = None, scope: Callable[[], str] | None = None):
        self.key = key
        self.make = make
        self.client = client
        self.scope = scope or (lambda: '')
        self._built: object | None = None
        self._digest: str | None = None
        self._lock = threading.Lock()

    @property
    def injected(self) -> bool:
        return self.client is not None

    def get(self) -> object | None:
        if self.client is not None:
            return self.client
        scope = self.scope()
        key = self.key()
        if self.scope() != scope:
            scope, key = self.scope(), self.key()
        if not key:
            return None
        digest = hashlib.sha256((scope + '\n' + key).encode()).hexdigest()[:16]
        with self._lock:
            if digest != self._digest:
                self._built, self._digest = (self.make(key, scope) if getattr(self.make, 'scoped', False) else self.make(key)), digest
            return self._built
