import asyncio
import hashlib
import logging
import re
import threading
import time
from collections.abc import Callable

import anthropic
import openai

from skopax.adminsecret import COLLECTION, REFRESH_S, AdminSecret
from skopax.ai import compat_ai
from skopax.ai.compat_ai import Connection, Refused
from skopax.errkind import kind
from skopax.messages import msg
from skopax.sealer import Sealer
from skopax.store import iso, utc_now

KEY_ID = 'anthropic.api-key'
OPENAI_KEY_ID = 'openai.api-key'
KEY = re.compile(r'[A-Za-z0-9_-]{20,400}')
TEST_TIMEOUT_S = 10
TEST_CAP_S = 15
COMPAT_ID = 'compat.'
COMPAT_TEST_CAP_S = 25
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


class UnknownConnection(KeyError):
    pass


def origin_text(url: str) -> str:
    scheme, host, port = compat_ai.origin(url)
    shown = f'[{host}]' if ':' in host else host
    return f'{scheme}://{shown}' + (f':{port}' if port is not None else '')


class AiConnections:
    def __init__(self, db, sealer: Callable[[], Sealer], stored_key: Callable[[], str | None], tunables, allow_private: Callable[[], bool],
                 make: Callable[..., object] | None = None, clock: Callable[[], float] = time.monotonic, every: float = REFRESH_S):
        self.col = db[COLLECTION]
        self.sealer = sealer
        self.stored_key = stored_key
        self.tunables = tunables
        self.allow_private = allow_private
        self.make = make or compat_ai.client_for
        self.clock = clock
        self.every = every
        self._held: dict[str, Connection] = {}
        self.changed: Callable[[], None] = lambda: None
        self.taken: Callable[[str], bool] = lambda name: False
        self._lost: set[str] = set()
        self._dropped: set[str] = set()
        self._clashed: set[str] = set()
        self._write = threading.Lock()
        self._testing: threading.Thread | None = None
        self._test_guard = threading.Lock()

    def refresh(self) -> None:
        with self._write:
            self._load()

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(self.every)
            try:
                await asyncio.to_thread(self.refresh)
            except Exception as e:
                log.warning('the stored AI connections were not read again, keeping the last state: %s', kind(e))

    def _open(self, name: str, sealed) -> tuple[str, bool]:
        if not sealed:
            return '', False
        current = self.sealer()
        plain = current.open(sealed)
        if plain is None:
            previous = self.stored_key()
            plain = Sealer(previous).open(sealed) if previous else None
            if plain is not None and current.enabled and self.col.update_one({'_id': COMPAT_ID + name, 'value': sealed}, {'$set': {'value': current.seal(plain)}}).modified_count:
                log.info('the stored key of the AI connection %s was sealed again with the current secret key', name)
        if plain is None:
            if name not in self._lost:
                log.warning('%s', msg('compat.unreadable', name=name))
            return '', True
        return plain, False

    def _load(self) -> None:
        out: dict[str, Connection] = {}
        dropped: set[str] = set()
        clashed: set[str] = set()
        for doc in self.col.find({'_id': {'$regex': '^compat[.]'}}):
            name = str(doc['_id'])[len(COMPAT_ID):]
            if self.col.find_one({'_id': 'vertex.' + name}, {'_id': 1}) is not None:
                clashed.add(name)
                if name not in self._clashed:
                    log.warning('%s', msg('vertex.name_clash', name=name[:40]))
                continue
            try:
                compat_ai.checked_name(name)
                models = compat_ai.checked_models(doc.get('models') or [])
            except Refused as e:
                dropped.add(name)
                if name not in self._dropped:
                    log.warning('a stored AI connection was left out (%r): %s', name[:40], e.text)
                continue
            key, lost = self._open(name, doc.get('value'))
            test = doc.get('lastTest')
            out[name] = Connection(name, str(doc.get('baseUrl') or ''), key, models, doc.get('tools') is not False, doc.get('images') is True,
                                   doc.get('updatedBy') if isinstance(doc.get('updatedBy'), str) else None, doc.get('updatedAt') if isinstance(doc.get('updatedAt'), str) else None,
                                   test if isinstance(test, dict) else None, lost)
        self._lost = {n for n, c in out.items() if c.unreadable}
        self._dropped = dropped
        self._clashed = clashed
        before = self.shape()
        self._held = out
        if self.shape() != before:
            compat_ai.forget(self, set(out))
            try:
                self.changed()
            except Exception as e:
                log.warning('the settings were not applied again after the AI connections changed: %s', kind(e))

    def shape(self) -> tuple:
        return tuple(sorted((n, tuple(m.id for m in c.models), c.unreadable) for n, c in self._held.items()))

    def all(self) -> list[Connection]:
        return sorted(self._held.values(), key=lambda c: c.name)

    def get(self, name: str) -> Connection | None:
        return self._held.get(name)

    def views(self) -> list[dict]:
        return [c.view() for c in self.all()]

    def clashes(self) -> list[str]:
        return sorted(self._clashed)

    def unreadable(self) -> list[str]:
        return [c.name for c in self.all() if c.unreadable]

    def put(self, name: str, body: dict, by: str) -> dict:
        name = compat_ai.checked_name(name)
        if self.taken(name):
            raise Refused(msg('vertex.name_taken'))
        url = compat_ai.check_url(body.get('baseUrl'), bool(self.allow_private()))
        models = compat_ai.checked_models(body.get('models') or [])
        typed = body.get('key')
        key = compat_ai.checked_key(typed) if isinstance(typed, str) and typed.strip() else None
        doc_id = COMPAT_ID + name
        with self._write:
            before = self.col.find_one({'_id': doc_id})
            if key is None and (before or {}).get('value') and compat_ai.moved(url, str(before.get('baseUrl') or '')):
                raise Refused(msg('compat.key_reenter'))
            value = self.sealer().seal(key) if key else (before or {}).get('value')
            listed = [{'id': m.id, 'input': m.input, 'output': m.output} for m in models]
            kept = before is not None and key is None and before.get('baseUrl') == url and before.get('models') == listed and isinstance(before.get('lastTest'), dict)
            doc = {'_id': doc_id, 'baseUrl': url, 'models': listed, 'tools': body.get('tools') is not False,
                   'images': body.get('images') is True, 'updatedBy': by, 'updatedAt': iso(utc_now())} | ({'value': value} if value else {}) | ({'lastTest': before['lastTest']} if kept else {})
            self.col.replace_one({'_id': doc_id}, doc, upsert=True)
            was = str((before or {}).get('baseUrl') or '')
            origins = {'oldOrigin': origin_text(was), 'newOrigin': origin_text(url)} if was and compat_ai.moved(url, was) else {}
            self.tunables.write_change(doc_id, 'set' if before is not None else None, 'set', by, secret=True, **origins)
            self._load()
        return self._held[name].view()

    def delete(self, name: str, by: str) -> bool:
        doc_id = COMPAT_ID + name
        with self._write:
            gone = bool(self.col.delete_one({'_id': doc_id}).deleted_count)
            if gone:
                self.tunables.write_change(doc_id, 'set', None, by, secret=True)
            self._load()
        return gone

    def _record(self, name: str, outcome: dict) -> None:
        with self._write:
            self.col.update_one({'_id': COMPAT_ID + name}, {'$set': {'lastTest': {k: outcome[k] for k in ('ok', 'tools', 'summary', 'model')} | {'at': iso(utc_now())}}})
            self._load()

    def test(self, name: str, body: dict | None, used: Callable[[], None] | None = None) -> dict:
        body = body or {}
        name = compat_ai.checked_name(name)
        stored = self.get(name)
        typed_key = body.get('key')
        key = compat_ai.checked_key(typed_key) if isinstance(typed_key, str) and typed_key.strip() else None
        typed = key is not None or bool(body.get('baseUrl')) or bool(body.get('models'))
        if stored is None and not body.get('baseUrl'):
            raise UnknownConnection(name)
        models = compat_ai.checked_models(body['models']) if body.get('models') else stored.models if stored is not None else ()
        model = body.get('model') if isinstance(body.get('model'), str) and body.get('model') else (models[0].id if models else '')
        if not model:
            return {'ok': False, 'tools': False, 'summary': msg('compat.test_no_model_set'), 'ms': 0}
        compat_ai.checked_models([{'id': model}])
        try:
            url = compat_ai.check_url(body.get('baseUrl') or stored.base_url, bool(self.allow_private()))
        except Refused as e:
            return {'ok': False, 'tools': False, 'summary': e.text, 'ms': 0}
        if key is None and stored is not None and stored.key and compat_ai.moved(url, stored.base_url):
            return {'ok': False, 'tools': False, 'summary': msg('compat.key_reenter'), 'ms': 0}
        secret = key if key is not None else (stored.key if stored is not None else '')
        box: dict = {}
        done = threading.Event()

        def go() -> None:
            client = None
            try:
                client = self.make(url, secret, self.allow_private, timeout=TEST_TIMEOUT_S, retries=0)
                box['out'] = compat_ai.probe(client, model)
            except BaseException as e:
                box['out'] = {'ok': False, 'tools': False, 'summary': msg('compat.test_failed'), 'error': e}
            finally:
                closing = getattr(client, 'close', None)
                if callable(closing):
                    try:
                        closing()
                    except Exception as e:
                        log.info('the connection test client did not close: %s', kind(e))
                with self._test_guard:
                    if self._testing is threading.current_thread():
                        self._testing = None
                done.set()

        with self._test_guard:
            if self._testing is not None and self._testing.is_alive():
                raise TestBusy('compat.')
            if not typed and used is not None:
                used()
            began = self.clock()
            self._testing = threading.Thread(target=go, name='compat-test', daemon=True)
            self._testing.start()
        if not done.wait(COMPAT_TEST_CAP_S):
            log.info('the connection test of %s did not finish within %g s', name, COMPAT_TEST_CAP_S)
            if compat_ai.private_target(url):
                return {'ok': False, 'tools': False, 'summary': msg('compat.test_private_failed'), 'ms': 0}
            return {'ok': False, 'tools': False, 'summary': msg('compat.test_timeout'), 'ms': int(COMPAT_TEST_CAP_S * 1000)}
        out = box['out']
        error = out.pop('error', None)
        if error is not None:
            log.info('the connection test of %s failed: %s', name, kind(error))
        if not out.get('ok') and compat_ai.private_target(url):
            out = out | {'tools': False, 'summary': msg('compat.test_private_failed')}
            if not typed and stored is not None:
                self._record(name, out | {'model': model})
            return out | {'ms': 0}
        if not typed and stored is not None:
            self._record(name, out | {'model': model})
        return out | {'ms': max(0, int((self.clock() - began) * 1000))}
