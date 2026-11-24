import hashlib
import hmac
import json
import logging
import re
import threading
import time
from contextlib import asynccontextmanager
from fnmatch import fnmatchcase
from typing import Callable, Mapping
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

from skopax import remote
from skopax.agentproto import KEYS_MAX, VALUE_LIMIT
from skopax.contract import AgentRead, ReadError
from skopax.engine import iso
from skopax.errkind import Refusal, kind as error_kind, text_of
from skopax.kube import Kube, KubeError, cluster_summary
from skopax.messages import msg
from skopax.redact import SlowRules, redact, use_rules
from skopax.registry import Registry, redaction_patterns
from skopax.settingsources import Boot, merged
from skopax.version import RELEASE

log = logging.getLogger(__name__)

MODE_ENV = 'SKOPAX_MODE'
TOKEN_ENV = 'SKOPAX_AGENT_TOKEN'
RELAY_ENV = 'SKOPAX_AGENT_RELAY_HOSTS'
TOKEN_KEY = 'agent.token'
RELAY_KEY = 'agent.relay-hosts'
REFRESH_S = 60
BODY_LIMIT = 1024 * 1024
LENGTH_DIGITS = 12
LIVENESS_PATH = '/actuator/health/liveness'
READINESS_PATH = '/actuator/health/readiness'
HEALTH_PATH = '/actuator/health'
INFO_PATH = '/api/agent'
KUBE_PATH = '/api/kube'
READ_PATH = '/api/read'
SOURCES_PATH = '/api/agent/sources'
RELAY_PATH = '/api/relay'
RELAY_TO = 'X-Relay-To'
RELAY_AUTH = 'X-Relay-Authorization'
RELAY_EXTRA_S = 10
INFO_TIMEOUT = 15
KUBE_TIMEOUT = 30
READ_TIMEOUT = 75
ARG_LIMIT = 200
DEFAULT_STATUS = 502
DETAIL_LIMIT = 300
SLOTS_MAX = 32
SLOT_NAME_MAX = 64
RELAYED = {INFO_PATH: ('GET', INFO_TIMEOUT), KUBE_PATH: ('POST', KUBE_TIMEOUT), READ_PATH: ('POST', READ_TIMEOUT), SOURCES_PATH: ('PUT', INFO_TIMEOUT)}
OPS = {
    'pods': ('namespace', 'selector'),
    'pod': ('namespace', 'name'),
    'events': ('namespace', 'object', 'since_minutes'),
    'rollout': ('namespace', 'deployment'),
    'previous_logs': ('namespace', 'pod', 'container', 'tail'),
    'nodes': (),
    'node': ('name',),
    'daemonset': ('namespace', 'name'),
}
REQUIRED = {'pods': ('namespace',), 'pod': ('namespace', 'name'), 'events': ('namespace',), 'rollout': ('namespace', 'deployment'),
            'previous_logs': ('namespace', 'pod'), 'nodes': (), 'node': ('name',), 'daemonset': ('namespace', 'name')}
INTS = ('since_minutes', 'tail')
NOT_JSON = object()
TARGET = re.compile(r'https://([a-z0-9.-]+)(:443)?', re.IGNORECASE | re.ASCII)
OPEN_ROUTES = frozenset({('GET', LIVENESS_PATH), ('GET', READINESS_PATH), ('GET', HEALTH_PATH)})


def agent_mode(env: Mapping[str, str]) -> bool:
    return (env.get(MODE_ENV) or '').strip().lower() == 'agent'


def rev_of(config: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16] if config else ''


class RawBody(str):
    pass


def status_of(code: object) -> int:
    return code if isinstance(code, int) and not isinstance(code, bool) and 400 <= code <= 599 else DEFAULT_STATUS


def slot_of(body: object) -> str | None:
    found = body.get('instance') if isinstance(body, dict) else None
    if found in (None, ''):
        return ''
    return found if isinstance(found, str) and len(found) <= SLOT_NAME_MAX and found.strip() == found else None


class AgentError(ReadError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(status_of(code), message)
        self.code = code


class NotConfigured(Exception):
    def __init__(self, which: str):
        super().__init__(which)
        self.which = which


def run_kube(kube, op: str, args: dict, now: float):
    if op == 'pods':
        return kube.pods(args['namespace'], args.get('selector'))
    if op == 'pod':
        return kube.pod(args['namespace'], args['name'])
    if op == 'events':
        return kube.events(args['namespace'], args.get('object'), int(args.get('since_minutes') or 60), now)
    if op == 'rollout':
        return kube.rollout(args['namespace'], args['deployment'])
    if op == 'previous_logs':
        return kube.previous_logs(args['namespace'], args['pod'], args.get('container'), int(args.get('tail') or 100))
    if op == 'nodes':
        return kube.nodes()
    if op == 'node':
        return kube.node(args['name'], now)
    return kube.daemonset(args['namespace'], args['name'])


def checked_args(op: str, given) -> dict:
    allowed = OPS[op]
    if not isinstance(given, dict):
        raise Refusal(msg('agent.args_object'))
    unknown = [k for k in given if k not in allowed]
    if unknown:
        raise Refusal(msg('agent.args_only', op=op, allowed=', '.join(allowed) or msg('agent.args_none'), given=', '.join(map(str, unknown))))
    out = {}
    for k, v in given.items():
        if v is None:
            continue
        if k in INTS:
            if not isinstance(v, int) or isinstance(v, bool):
                raise Refusal(msg('agent.arg_whole', key=k))
        elif not isinstance(v, str) or len(v) > ARG_LIMIT:
            raise Refusal(msg('agent.arg_text', key=k))
        out[k] = v
    missing = [k for k in REQUIRED[op] if not out.get(k)]
    if missing:
        raise Refusal(msg('agent.arg_missing', op=op, keys=', '.join(missing)))
    return out


def declared(key: str, patterns: tuple[str, ...]) -> bool:
    return any(re.fullmatch(pattern, key) for pattern in patterns)


def parse_body(raw: bytes) -> object:
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return NOT_JSON


class AgentCore:
    def __init__(self, registry: Registry, kube, token: Callable[[], str], relay_hosts: Callable[[], tuple[str, ...]], own_config: Callable[[], dict[str, str]],
                 transport: remote.Transport = remote.relay_send, clock: Callable[[], float] = time.time, version: str = '',
                 relay_setting: Callable[[], str] = lambda: RELAY_ENV, waiting: Callable[[], list[str]] = lambda: []):
        self.registry = registry
        self.kube = kube
        self.token = token
        self.relay_hosts = relay_hosts
        self.own_config = own_config
        self.transport = transport
        self.clock = clock
        self.version = version
        self.relay_setting = relay_setting
        self.waiting = waiting
        self.started = clock()
        self._open = set(OPEN_ROUTES)
        self._table = {('GET', LIVENESS_PATH): self._liveness, ('GET', READINESS_PATH): self._liveness, ('GET', HEALTH_PATH): self._health, ('GET', INFO_PATH): self._info, ('POST', KUBE_PATH): self._kube,
                       ('POST', READ_PATH): self._read, ('PUT', SOURCES_PATH): self._sources}
        self._lock = threading.Lock()
        self._pushed: dict[str, str] = {}
        self._rev = ''
        self._scoped: dict[tuple[str, str], tuple[str, dict[str, str]]] = {}

    def _reads(self) -> dict[str, AgentRead]:
        return {read.op: read for plugin in self.registry.usable() for read in plugin.agent_reads}

    def _patterns(self) -> tuple[str, ...]:
        return tuple(key for read in self._reads().values() for key in read.keys)

    def _config(self, read: AgentRead, instance: str = '') -> dict[str, str]:
        if instance:
            with self._lock:
                held = self._scoped.get((read.op, instance))
            return {} if held is None else {k: v for k, v in held[1].items() if declared(k, read.keys)}
        with self._lock:
            pushed = {k: v for k, v in self._pushed.items() if declared(k, read.keys)}
        if pushed:
            return pushed
        try:
            own = dict(self.own_config())
        except Exception as e:
            log.warning('agent own config not read: %s', type(e).__name__)
            return {}
        return {k: v for k, v in own.items() if declared(k, read.keys)}

    def _pending(self, read: AgentRead, instance: str = '') -> list[str]:
        if instance:
            return []
        with self._lock:
            if any(declared(k, read.keys) for k in self._pushed):
                return []
        return list(self.waiting())

    def _names(self, read: AgentRead, instance: str = '') -> list[str]:
        if self._pending(read, instance):
            return []
        config = self._config(read, instance)
        if read.keys and not config:
            return []
        try:
            return sorted(read.sources(config))
        except Exception as e:
            log.warning('agent read %s sources failed: %s', read.op, type(e).__name__)
            return []

    def reads(self) -> dict[str, list[str]]:
        return {op: self._names(read) for op, read in self._reads().items()}

    def push(self, rev: str, config: dict[str, str], instance: str = '', op: str = '') -> str:
        if instance:
            with self._lock:
                self._scoped.pop((op, instance), None)
                while config and len(self._scoped) >= SLOTS_MAX:
                    self._scoped.pop(next(iter(self._scoped)))
                if config:
                    self._scoped[(op, instance)] = (str(rev or '')[:64], dict(config))
            log.info('source settings %s for %s %s taken from the master: %d keys', str(rev or '')[:64] or 'cleared', op, instance, len(config))
            return str(rev or '')[:64]
        with self._lock:
            self._pushed, self._rev = dict(config), str(rev or '')[:64]
        log.info('source settings %s taken from the master: %d keys', self._rev or 'cleared', len(config))
        return self._rev

    def _refused(self, authorization: str, relay: bool = False) -> tuple[int, dict] | None:
        key = self.token()
        if not key:
            return (401, {'error': 'unauthorized', 'relay': True}) if relay else (503, {'error': msg('agent.no_token')})
        if not authorization.startswith('Bearer ') or not hmac.compare_digest(authorization[7:].encode(), key.encode()):
            return (401, {'error': 'unauthorized', 'relay': True}) if relay else (401, {'error': 'unauthorized'})
        return None

    def routes(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._table)

    def gate(self, method: str, path: str, authorization: str) -> tuple[int, dict] | None:
        if path.startswith(RELAY_PATH + '/'):
            return self._refused(authorization, relay=True)
        if (method, path) in self._table and (method, path) not in self._open:
            return self._refused(authorization)
        return None

    def dispatch(self, method: str, path: str, authorization: str, body: object, relay_to: str = '', relay_auth: str = '', gated: bool = False) -> tuple[int, dict]:
        if not gated:
            stop = self.gate(method, path, authorization)
            if stop is not None:
                return stop
        if path.startswith(RELAY_PATH + '/'):
            if method not in ('GET', 'POST', 'PUT'):
                return 404, {'error': msg('agent.relay_routes', routes=self._routes())}
            return self._relay(method, path[len(RELAY_PATH):], body, relay_to, relay_auth)
        handler = self._table.get((method, path))
        if handler is None:
            return 404, {'error': 'not found'}
        return handler(body)

    def _liveness(self, body: object) -> tuple[int, dict]:
        return 200, {'status': 'UP'}

    def _health(self, body: object) -> tuple[int, dict]:
        return 200, {'status': 'UP', 'mode': 'agent', 'kube': bool(self.kube.enabled), 'token': bool(self.token())}

    def _info(self, body: object) -> tuple[int, dict]:
        out = {'version': self.version, 'release': RELEASE, 'startedAt': iso(self.started), 'kube': bool(self.kube.enabled), 'reads': self.reads(), 'sourcesRev': self._rev}
        try:
            out['cluster'] = cluster_summary(self.kube.nodes())
        except KubeError as e:
            out['clusterError'] = e.message
        return 200, out

    def _kube(self, body: object) -> tuple[int, dict]:
        if body is NOT_JSON:
            return 400, {'error': msg('agent.body_op')}
        op = body.get('op') if isinstance(body, dict) else None
        if op not in OPS:
            return 404, {'error': msg('agent.op_unknown', ops=', '.join(OPS))}
        try:
            args = checked_args(op, body.get('args') or {})
        except ValueError as e:
            return 422, {'error': text_of(e)}
        try:
            data = run_kube(self.kube, op, args, self.clock())
        except KubeError as e:
            return 400, {'error': e.message, 'code': e.code}
        log.info('kube read %s %s', op, ' '.join(f'{k}={v}' for k, v in args.items()))
        return 200, {'data': data}

    def _read(self, body: object) -> tuple[int, dict]:
        if body is NOT_JSON:
            return 400, {'error': msg('agent.body_op')}
        op = body.get('op') if isinstance(body, dict) else None
        args = (body.get('args') or {}) if isinstance(body, dict) else {}
        instance = slot_of(body)
        if instance is None:
            return 422, {'error': msg('agent.instance_name', max=SLOT_NAME_MAX)}
        reads = self._reads()
        read = reads.get(op) if isinstance(op, str) else None
        if read is None:
            return 404, {'error': msg('agent.op_unknown', ops=', '.join(reads)) if reads else msg('agent.no_reads')}
        if not isinstance(args, dict):
            return 422, {'error': msg('agent.args_object')}
        unknown = [k for k in args if k not in read.args]
        if unknown:
            return 422, {'error': msg('agent.args_only', op=op, allowed=', '.join(read.args) or msg('agent.args_none'), given=', '.join(map(str, unknown)))}
        pending = self._pending(read, instance)
        if pending:
            return 503, {'error': msg('agent.source_pending', source=', '.join(pending))}
        if not self._names(read, instance):
            return 503, {'error': msg('agent.read_off', op=op)}
        try:
            data = read.run(self._config(read, instance), args)
        except ReadError as e:
            return status_of(e.status), {'error': e.message}
        except Exception as e:
            log.warning('agent read %s failed: %s', op, type(e).__name__)
            return 500, {'error': msg('agent.read_failed')}
        log.info('source read %s', op)
        return 200, {'data': data}

    def _sources(self, body: object) -> tuple[int, dict]:
        if body is NOT_JSON:
            return 400, {'error': msg('agent.body_sources')}
        given = body.get('config') if isinstance(body, dict) else None
        if not isinstance(given, dict) or len(given) > KEYS_MAX:
            return 422, {'error': msg('agent.push_shape', max=KEYS_MAX)}
        instance = slot_of(body)
        if instance is None:
            return 422, {'error': msg('agent.instance_name', max=SLOT_NAME_MAX)}
        reads = self._reads()
        op = body.get('op') if instance else ''
        if instance and (not isinstance(op, str) or op not in reads):
            return 422, {'error': msg('agent.instance_op', ops=', '.join(reads) or msg('agent.args_none'))}
        patterns = reads[op].keys if instance else self._patterns()
        unknown = [str(k)[:60] for k in given if not isinstance(k, str) or not declared(k, patterns)]
        if unknown:
            return 422, {'error': msg('agent.push_unknown', keys=', '.join(unknown))}
        if any(not isinstance(v, str) or len(v) > VALUE_LIMIT for v in given.values()):
            return 422, {'error': msg('agent.push_value', max=VALUE_LIMIT)}
        rev = self.push(str(body.get('rev') or ''), given, instance, op)
        return 200, {'rev': rev, 'reads': self.reads()} | ({'instance': instance} if instance else {})

    @staticmethod
    def _routes() -> str:
        return ', '.join(f'{m} {p}' for p, (m, _) in RELAYED.items())

    def _target(self, given: str) -> str | None:
        found = TARGET.fullmatch((given or '').strip().rstrip('/'))
        if found is None:
            return None
        host = found[1].lower()
        if not any(fnmatchcase(host, glob.lower()) for glob in self.relay_hosts()):
            return None
        return f'https://{host}{found[2] or ""}'

    def _relay(self, method: str, path: str, body: object, relay_to: str, relay_auth: str) -> tuple[int, dict]:
        wanted, timeout = RELAYED.get(path, (None, 0))
        if method != wanted:
            return 404, {'error': msg('agent.relay_routes', routes=self._routes())}
        target = self._target(relay_to)
        if target is None:
            return 400, {'error': msg('agent.relay_to', setting=self.relay_setting()), 'relay': True}
        if not relay_auth.startswith('Bearer ') or len(relay_auth) <= 7:
            return 400, {'error': msg('agent.relay_auth'), 'relay': True}
        if method != 'GET' and body is NOT_JSON:
            return 400, {'error': msg('agent.body_json')}
        host = urlsplit(target).hostname
        try:
            got = remote.call(self.transport, method, target + path, {'Authorization': relay_auth}, None if method == 'GET' else body, timeout)
        except remote.HttpError as e:
            log.info('relayed %s %s to %s: HTTP %s', method, path, host, e.code)
            try:
                said = json.loads(e.body or 'null')
            except ValueError:
                said = None
            return e.code, said if isinstance(said, dict) else RawBody(e.body or '')
        except RecursionError:
            return 400, {'error': msg('agent.body_json')}
        except remote.Unreachable as e:
            log.warning('relay to %s failed: %s', host, type(e).__name__)
            return 502, {'error': msg('agent.relay_unreachable', host=host), 'relay': True}
        log.info('relayed %s %s to %s', method, path, host)
        return 200, got.body if got.body is not None else {}


class TokenSource:
    def __init__(self, env: Mapping[str, str], values: Callable[[], dict[str, str]]):
        self.env = env
        self.values = values
        self._missing = False

    def __call__(self) -> str:
        value = (self.env.get(TOKEN_ENV) or '').strip() or str(self.values().get(TOKEN_KEY) or '').strip()
        if not value:
            self._missing = True
        elif self._missing:
            self._missing = False
            log.info('agent token found; reads are open to the master')
        return value


class Refresher:
    def __init__(self, sources, every: float | None):
        self.sources = sources
        self.every = every
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failing = False

    def run_once(self) -> None:
        try:
            self.sources.refresh()
        except Exception as e:
            if not self._failing:
                log.warning('agent settings not refreshed: %s', type(e).__name__)
            self._failing = True
        else:
            self._failing = False

    def start(self) -> None:
        if not self.every or self._thread is not None:
            return

        stop = self._stop = threading.Event()

        def loop():
            while not stop.wait(self.every):
                self.run_once()

        self._thread = threading.Thread(target=loop, name='agent-sources', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(5)
            self._thread = None


def create_agent(boot: Boot, kube=None, transport: remote.Transport = remote.relay_send, clock: Callable[[], float] = time.time, refresh_every: float | None = REFRESH_S) -> FastAPI:
    env = boot.env
    sources = boot.source_set()

    def values() -> dict[str, str]:
        return merged(sources.current())

    tokens = TokenSource(env, values)
    if not tokens():
        log.warning('%s%s', msg('agent.no_token'), f'; trying the settings again every {refresh_every} s' if refresh_every else '')
    if refresh_every:
        for label, error in boot.failed.items():
            log.warning('%s', msg('settings.source.retry', source=label, error=error, every=refresh_every))

    def relay_hosts() -> tuple[str, ...]:
        raw = (env.get(RELAY_ENV) or '').strip() or values().get(RELAY_KEY) or ''
        return tuple(part.strip() for part in raw.split(',') if part.strip())

    def relay_setting() -> str:
        if (env.get(RELAY_ENV) or '').strip():
            return RELAY_ENV
        return RELAY_KEY if values().get(RELAY_KEY) else f'{RELAY_ENV} or {RELAY_KEY}'

    use_rules(redaction_patterns(boot.registry.usable()), SlowRules(log))
    core = AgentCore(boot.registry, kube or Kube(), tokens, relay_hosts, values, transport, clock, version=env.get('COMMIT_ID', ''), relay_setting=relay_setting, waiting=sources.unanswered)
    refresher = Refresher(sources, refresh_every)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        refresher.start()
        yield
        await run_in_threadpool(refresher.stop)

    app = FastAPI(title='agent', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.core = core
    app.state.refresher = refresher

    async def read_body(request: Request) -> bytes | None:
        declared = request.headers.get('content-length', '')
        if declared.isascii() and declared.isdigit() and (len(declared) > LENGTH_DIGITS or int(declared) > BODY_LIMIT):
            return None
        out = bytearray()
        async for chunk in request.stream():
            out += chunk
            if len(out) > BODY_LIMIT:
                return None
        return bytes(out)

    async def answer(request: Request, with_body: bool = False) -> JSONResponse:
        auth = request.headers.get('authorization', '')
        stop = core.gate(request.method, request.url.path, auth)
        if stop is not None:
            return JSONResponse(stop[1], stop[0])
        body = None
        if with_body:
            raw = await read_body(request)
            if raw is None:
                too_large = {'error': msg('agent.body_large')}
                return JSONResponse(too_large | {'relay': True} if request.url.path.startswith(RELAY_PATH + '/') else too_large, 413)
            body = parse_body(raw)
        code, out = await run_in_threadpool(core.dispatch, request.method, request.url.path, auth, body, request.headers.get(RELAY_TO, ''), request.headers.get(RELAY_AUTH, ''), True)
        if isinstance(out, RawBody):
            return Response(content=str(out), status_code=code, media_type='application/json')
        return JSONResponse(out, code)

    def endpoint(with_body: bool):
        async def handle(request: Request):
            return await answer(request, with_body)

        return handle

    for method, path in core.routes():
        app.add_api_route(path, endpoint(method != 'GET'), methods=[method])

    @app.api_route(RELAY_PATH + '/{rest:path}', methods=['GET', 'POST', 'PUT'])
    async def relay(rest: str, request: Request):
        return await answer(request, request.method != 'GET')

    return app


class RemoteAgent:
    def __init__(self, link: Callable[[str, str, dict | None, float], dict], label: str, relay: str | None = None):
        self.link = link
        self.label = label
        self.relay = getattr(link, 'relay', '') if relay is None else relay

    def info(self) -> dict:
        return self._send('GET', INFO_PATH, None, INFO_TIMEOUT)

    def kube(self, op: str, **args):
        return self._send('POST', KUBE_PATH, {'op': op, 'args': {k: v for k, v in args.items() if v is not None}}, KUBE_TIMEOUT).get('data')

    def read(self, op: str, args: dict, timeout: float | None = None, instance: str = ''):
        return self._send('POST', READ_PATH, {'op': op, 'args': {k: v for k, v in (args or {}).items() if v is not None}} | ({'instance': instance} if instance else {}), timeout or READ_TIMEOUT).get('data')

    def push_sources(self, rev: str, config: dict[str, str], instance: str = '', op: str = '') -> dict:
        return self._send('PUT', SOURCES_PATH, {'rev': rev, 'config': config} | ({'instance': instance, 'op': op} if instance else {}), INFO_TIMEOUT)

    def _failed(self, e: remote.HttpError) -> AgentError:
        try:
            said = json.loads(e.body or '{}')
        except ValueError:
            said = {}
        said = said if isinstance(said, dict) else {}
        detail = redact(str(said.get('error') or ''))[:DETAIL_LIMIT]
        if said.get('relay') and self.relay:
            if e.code == 401:
                return AgentError(msg('agent.relay_refused', relay=self.relay, label=self.label), 401)
            return AgentError(msg('agent.relay_failed', relay=self.relay, label=self.label, detail=detail or f'HTTP {e.code}'), e.code)
        if e.code == 400 and detail:
            return AgentError(detail, said.get('code'))
        if e.code == 401:
            return AgentError(msg('agent.refused', label=self.label), 401)
        if detail:
            return AgentError(msg('agent.answered_with', label=self.label, code=e.code, detail=detail), e.code)
        return AgentError(msg('agent.answered', label=self.label, code=e.code), e.code)

    def _send(self, method: str, path: str, body: dict | None, timeout: float) -> dict:
        try:
            return self.link(method, path, body, timeout) or {}
        except NotConfigured as e:
            if e.which == 'relay':
                raise AgentError(msg('agent.relay_not_configured', label=self.label, relay=self.relay)) from None
            raise AgentError(msg('agent.not_configured', label=self.label)) from None
        except remote.HttpError as e:
            raise self._failed(e) from None
        except remote.Unreachable as e:
            where = msg('agent.through', relay=self.relay) if self.relay else ''
            said = msg('agent.unreachable', label=self.label, where=where)
            log.warning('%s: %s', said, error_kind(e))
            raise AgentError(said) from None


def http_link(url: str, token: str, transport: remote.Transport = remote.send, via: tuple[str, str] | None = None, via_label: str = '') -> Callable[[str, str, dict | None, float], dict]:
    base = (url or '').rstrip('/')
    relay_name = '' if via is None else via_label or urlsplit(via[0]).hostname or ''

    def link(method: str, path: str, body: dict | None, timeout: float) -> dict:
        if not (base and token):
            raise NotConfigured('agent')
        if via is None:
            target, headers = base + path, {'Authorization': 'Bearer ' + token}
        else:
            relay_url, relay_token = via
            if not (relay_url and relay_token):
                raise NotConfigured('relay')
            target, headers, timeout = relay_url.rstrip('/') + RELAY_PATH + path, {'Authorization': 'Bearer ' + relay_token, RELAY_TO: base, RELAY_AUTH: 'Bearer ' + token}, timeout + RELAY_EXTRA_S
        return remote.call(transport, method, target, headers, body, timeout=timeout).body or {}

    link.relay = relay_name
    return link
