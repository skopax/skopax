import asyncio
import logging
import threading
import time
from collections.abc import Callable

from skopax.adminsecret import COLLECTION, REFRESH_S
from skopax.aikey import TestBusy, UnknownConnection
from skopax.ai import compat_ai, vertex_ai
from skopax.ai.compat_ai import Refused
from skopax.ai.vertex_ai import FEDERATION_MODE, KEY_MODE, Connection
from skopax.errkind import kind
from skopax.messages import msg
from skopax.sealer import Sealer
from skopax.store import iso, utc_now

log = logging.getLogger('skopax.vertex')

VERTEX_ID = 'vertex.'
TEST_CAP_S = 60
TEST_KEYS = ('ok', 'tools', 'summary', 'models', 'ms')


def mode_of(credentials: object) -> str:
    return vertex_ai.checked_mode(credentials.get('mode') if isinstance(credentials, dict) else None)


class VertexConnections:
    def __init__(self, db, sealer: Callable[[], Sealer], stored_key: Callable[[], str | None], tunables, token_dir: Callable[[], str],
                 make: Callable[..., object] | None = None, clock: Callable[[], float] = time.monotonic, every: float = REFRESH_S):
        self.col = db[COLLECTION]
        self.sealer = sealer
        self.stored_key = stored_key
        self.tunables = tunables
        self.token_dir = token_dir
        self.make = make or vertex_ai.client_for
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
                log.warning('the stored Vertex AI connections were not read again, keeping the last state: %s', kind(e))

    def _open(self, name: str, sealed) -> tuple[str, bool]:
        if not sealed:
            return '', False
        current = self.sealer()
        plain = current.open(sealed)
        if plain is None:
            previous = self.stored_key()
            plain = Sealer(previous).open(sealed) if previous else None
            if plain is not None and current.enabled and self.col.update_one({'_id': VERTEX_ID + name, 'value': sealed}, {'$set': {'value': current.seal(plain)}}).modified_count:
                log.info('the stored key of the Vertex AI connection %s was sealed again with the current secret key', name)
        if plain is None:
            if name not in self._lost:
                log.warning('%s', msg('vertex.unreadable', name=name))
            return '', True
        return plain, False

    def _load(self) -> None:
        out: dict[str, Connection] = {}
        dropped: set[str] = set()
        clashed: set[str] = set()
        for doc in self.col.find({'_id': {'$regex': '^vertex[.]'}}):
            name = str(doc['_id'])[len(VERTEX_ID):]
            if self.col.find_one({'_id': 'compat.' + name}, {'_id': 1}) is not None:
                clashed.add(name)
                if name not in self._clashed:
                    log.warning('%s', msg('vertex.name_clash', name=name[:40]))
                continue
            try:
                compat_ai.checked_name(name)
                project = vertex_ai.checked_project(doc.get('project'))
                region = vertex_ai.checked_region(doc.get('region'))
                mode = vertex_ai.checked_mode(doc.get('mode'))
                models = vertex_ai.checked_models(doc.get('models') or [])
                federation = vertex_ai.checked_federation(doc.get('federation'), None) if mode == FEDERATION_MODE else None
            except Refused as e:
                dropped.add(name)
                if name not in self._dropped:
                    log.warning('a stored Vertex AI connection was left out (%r): %s', name[:40], e.text)
                continue
            key, lost = self._open(name, doc.get('value')) if mode == KEY_MODE else ('', False)
            test = doc.get('lastTest')
            out[name] = Connection(name, project, region, mode, key, federation, models, doc.get('updatedBy') if isinstance(doc.get('updatedBy'), str) else None,
                                   doc.get('updatedAt') if isinstance(doc.get('updatedAt'), str) else None, test if isinstance(test, dict) else None, lost or (mode == KEY_MODE and not key))
        self._lost = {n for n, c in out.items() if c.unreadable}
        self._dropped = dropped
        self._clashed = clashed
        before = self.shape()
        self._held = out
        if self.shape() != before:
            vertex_ai.forget(self, set(out))
            try:
                self.changed()
            except Exception as e:
                log.warning('the settings were not applied again after the Vertex AI connections changed: %s', kind(e))

    def shape(self) -> tuple:
        return tuple(sorted((n, c.region, tuple(m.id for m in c.models), c.unreadable) for n, c in self._held.items()))

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

    def blocked(self) -> list[str]:
        root = self.token_dir()
        return [c.name for c in self.all() if c.mode == FEDERATION_MODE and not vertex_ai.usable(c, root)]

    def _secrets(self, stored: Connection | None, project: str, region: str, mode: str, credentials: dict) -> tuple[str, dict | None]:
        moved = stored is not None and (stored.project != project or stored.region != region)
        if mode == KEY_MODE:
            typed = credentials.get('key')
            if isinstance(typed, str) and typed.strip() or isinstance(typed, dict):
                return vertex_ai.checked_key(typed), None
            if stored is None or stored.mode != KEY_MODE or not stored.key:
                raise Refused(msg('vertex.key_needed'))
            if moved:
                raise Refused(msg('vertex.key_reenter'))
            return stored.key, None
        if mode == FEDERATION_MODE:
            typed = credentials.get('config')
            if typed is not None and typed != '':
                return '', vertex_ai.checked_federation(typed, self.token_dir())
            if stored is None or stored.mode != FEDERATION_MODE or stored.federation is None:
                raise Refused(msg('vertex.federation_needed'))
            return '', vertex_ai.checked_federation(stored.federation, self.token_dir())
        return '', None

    def _candidate(self, name: str, body: dict, stored: Connection | None, by: str | None) -> tuple[Connection, bool]:
        project = vertex_ai.checked_project(body.get('project') if body.get('project') is not None else stored.project if stored is not None else None)
        region = vertex_ai.checked_region(body.get('region') if body.get('region') is not None else stored.region if stored is not None else None)
        credentials = body.get('credentials')
        if credentials is None and stored is not None:
            credentials = {'mode': stored.mode}
        mode = mode_of(credentials)
        models = vertex_ai.checked_models(body['models']) if body.get('models') is not None else stored.models if stored is not None else ()
        key, federation = self._secrets(stored, project, region, mode, credentials)
        fresh = bool(credentials.get('key') or credentials.get('config')) or stored is None or (stored.mode, stored.project, stored.region, stored.models) != (mode, project, region, models)
        return Connection(name, project, region, mode, key, federation, models, by, iso(utc_now()), None if fresh else stored.last_test), fresh

    def put(self, name: str, body: dict, by: str) -> dict:
        name = compat_ai.checked_name(name)
        if self.taken(name):
            raise Refused(msg('vertex.name_taken'))
        doc_id = VERTEX_ID + name
        with self._write:
            stored = self._held.get(name)
            if body.get('credentials') is None or body.get('project') is None or body.get('region') is None:
                raise Refused(msg('vertex.invalid'))
            found, fresh = self._candidate(name, body, stored, by)
            before = self.col.find_one({'_id': doc_id})
            value = self.sealer().seal(found.key) if found.key else None
            listed = [{'id': m.id, 'vertexId': m.vertex_id, 'input': m.input, 'output': m.output} for m in found.models]
            doc = ({'_id': doc_id, 'project': found.project, 'region': found.region, 'mode': found.mode, 'models': listed, 'updatedBy': by, 'updatedAt': found.at}
                   | ({'value': value} if value else {}) | ({'federation': found.federation} if found.federation is not None else {})
                   | ({'lastTest': found.last_test} if found.last_test is not None else {}))
            self.col.replace_one({'_id': doc_id}, doc, upsert=True)
            moved = {'oldRegion': before.get('region'), 'newRegion': found.region} if before and before.get('region') != found.region else {}
            moved |= {'oldProject': before.get('project'), 'newProject': found.project} if before and before.get('project') != found.project else {}
            self.tunables.write_change(doc_id, 'set' if before is not None else None, 'set', by, secret=True, **moved)
            self._load()
        return self._held[name].view()

    def delete(self, name: str, by: str) -> bool:
        doc_id = VERTEX_ID + name
        with self._write:
            gone = bool(self.col.delete_one({'_id': doc_id}).deleted_count)
            if gone:
                self.tunables.write_change(doc_id, 'set', None, by, secret=True)
            self._load()
        return gone

    def _record(self, name: str, outcome: dict) -> None:
        with self._write:
            self.col.update_one({'_id': VERTEX_ID + name}, {'$set': {'lastTest': {k: outcome[k] for k in TEST_KEYS} | {'at': iso(utc_now())}}})
            self._load()

    def test(self, name: str, body: dict | None, used: Callable[[], None] | None = None) -> dict:
        body = body or {}
        name = compat_ai.checked_name(name)
        stored = self.get(name)
        typed = any(body.get(k) is not None for k in ('project', 'region', 'credentials', 'models'))
        if stored is None and not typed:
            raise UnknownConnection(name)
        found, _ = self._candidate(name, body, stored, None)
        if not found.models:
            return {'ok': False, 'tools': False, 'summary': msg('vertex.test_no_model_set'), 'ms': 0}
        box: dict = {}
        done = threading.Event()

        def go() -> None:
            client = None
            try:
                client = self.make(found, self.token_dir(), timeout=vertex_ai.TEST_TIMEOUT_S, retries=0)
                box['out'] = vertex_ai.probe(client, found)
            except BaseException as e:
                box['out'] = {'ok': False, 'tools': False, 'summary': msg('vertex.test_failed'), 'models': [], 'error': e}
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
                raise TestBusy('vertex.')
            if not typed and used is not None:
                used()
            began = self.clock()
            self._testing = threading.Thread(target=go, name='vertex-test', daemon=True)
            self._testing.start()
        if not done.wait(TEST_CAP_S):
            log.info('the connection test of %s did not finish within %g s', name, TEST_CAP_S)
            return {'ok': False, 'tools': False, 'summary': msg('vertex.test_timeout'), 'ms': int(TEST_CAP_S * 1000)}
        out = box['out']
        error = out.pop('error', None)
        if error is not None:
            log.info('the connection test of %s failed: %s', name, kind(error))
        out = out | {'ms': max(0, int((self.clock() - began) * 1000))}
        if not typed and stored is not None:
            self._record(name, out)
        return out
