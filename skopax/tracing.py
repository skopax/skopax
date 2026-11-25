import base64
import contextlib
import contextvars
import json
import logging
import random
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from skopax import __version__
from skopax.errkind import kind as error_kind
from skopax.redact import redact_secrets
from skopax.remote import NotJson, Transport, call

log = logging.getLogger('skopax.tracing')

PLUGIN = 'langfuse'
TRACED_SETTING = f'{PLUGIN}.traced-instance'
DEFAULT_URL = 'https://cloud.langfuse.com'
OTLP = '/api/public/otel/v1/traces'
PROJECTS = '/api/public/projects'
INGESTION_VERSION = '4'
METADATA = 'metadata'
FULL = 'full'
CONTENTS = (METADATA, FULL)
SERVICE = 'skopax'
QUEUE_MAX = 200
SPANS_MAX = 300
BATCH_MAX = 20
SPANS_PER_POST = 500
TIMEOUT_S = 5.0
WAIT_S = 2.0
PAUSE_S = 30.0
PROJECT_RETRY_S = 3600.0
TARGET_CACHE_S = 10.0
JOIN_S = 1.0
CONTENT_MAX = 4000
RAW_MAX = 4 * CONTENT_MAX
META_MAX = 200
OFFERED_MAX = 50
CALLS_MAX = 20
UNKNOWN = 'unknown'
OTHER = 'other'
STOPS = frozenset({'end_turn', 'tool_use', 'max_tokens', 'stop_sequence', 'refusal', 'pause_turn', 'stop', 'length', 'tool_calls', 'content_filter', 'model_context_window_exceeded'})
OUTCOMES = frozenset({'done', 'skipped', 'failed', 'capped', 'refused', 'stopped', 'off', 'abandoned'})
SYSTEM_MAX = 1000
LAST_MAX = 2600
LOG_EVERY = 100
MORE = '\u2026'
OK, ERROR = 1, 2
GENERATION, TOOL, SPAN = 'generation', 'tool', 'span'
TOOL_FAILED = 'the tool answered with an error'


def span_id() -> str:
    return uuid.uuid4().hex[:16]


def nanos(at: float) -> str:
    return str(int(at * 1_000_000_000))


def cut(text: str, limit: int = CONTENT_MAX) -> str:
    return text if len(text) <= limit else text[:limit - 1] + MORE


def plain_text(content) -> str:
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        kind = content.get('type')
        if kind == 'text':
            return str(content.get('text') or '')
        if kind == 'tool_result':
            return plain_text(content.get('content'))
        if kind == 'tool_use':
            return json.dumps({'tool': content.get('name'), 'input': content.get('input')}, default=str)
        return ''
    if isinstance(content, (list, tuple)):
        return '\n'.join(t for t in (plain_text(c) for c in content) if t)
    text = getattr(content, 'text', None)
    return text if isinstance(text, str) else ''


def raw_of(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    return text[:RAW_MAX]


@dataclass(frozen=True)
class Target:
    name: str
    url: str
    public: str
    secret: str = field(repr=False)
    transport: Transport = field(repr=False)

    @property
    def key(self) -> tuple[str, str]:
        return self.url, self.public

    def headers(self) -> dict[str, str]:
        basic = base64.b64encode(f'{self.public}:{self.secret}'.encode()).decode()
        return {'Authorization': f'Basic {basic}', 'x-langfuse-ingestion-version': INGESTION_VERSION}


@dataclass
class Span:
    id: str
    parent: str | None
    name: str
    kind: str
    start: float
    end: float
    attrs: dict = field(default_factory=dict)
    error: str | None = None
    input: str | None = None
    output: str | None = None
    brief_in: dict | None = None
    brief_out: dict | None = None


class Trace:
    def __init__(self, name: str, meta: dict, sampled: bool, full: bool, started: float):
        self.id = uuid.uuid4().hex
        self.root = span_id()
        self.name = name
        self.meta = {k: cut(redact_secrets(str(v)), META_MAX) for k, v in meta.items() if v not in (None, '')}
        self.sampled = sampled
        self.full = full
        self.started = started
        self.ended: float | None = None
        self.spans: list[Span] = []
        self.dropped = 0
        self.error: str | None = None
        self.input: str | None = None
        self.output: str | None = None
        self.calls = 0
        self.closed = False
        self.on_sent: Callable[[dict], None] | None = None
        self.brief: dict = {}
        self.outcome: str | None = None
        self.offered: dict[str, frozenset[str]] = {}

    @property
    def failed(self) -> bool:
        return self.error is not None

    @property
    def sends(self) -> bool:
        return self.failed or (self.sampled and self.calls > 0)

    def fail(self, why: str) -> None:
        self.error = cut(redact_secrets(str(why or 'failed')), META_MAX)

    def note_brief(self, **fields) -> None:
        self.brief.update({k: v for k, v in fields.items() if isinstance(v, (str, int, float, bool))})

    def finished(self, outcome: str) -> None:
        self.outcome = outcome if outcome in OUTCOMES else OTHER

    def offer(self, tools) -> None:
        self.offered.update(offered_schemas(tools))

    def root_brief_in(self) -> dict | None:
        return None if self.full else {'kind': self.name} | self.brief

    def root_brief_out(self) -> dict | None:
        if self.full:
            return None
        return {'outcome': self.outcome or ('failed' if self.failed else 'done'), 'model_calls': self.calls, 'tool_calls': sum(1 for s in self.spans if s.kind == TOOL)}

    def given(self, value) -> None:
        if self.full and value not in (None, ''):
            self.input = raw_of(value)

    def answered(self, value) -> None:
        if self.full and value not in (None, ''):
            self.output = raw_of(value)

    def add(self, span: Span) -> None:
        if len(self.spans) >= SPANS_MAX:
            self.dropped += 1
            return
        self.spans.append(span)

    def generation(self, provider: str, kind: str, model: str, started: float, ended: float, usage=None, cost: float | None = None, stop: str | None = None,
                   error: str | None = None, given=None, said=None, brief_in: dict | None = None, brief_out: dict | None = None) -> None:
        self.calls += 1
        attrs = {'langfuse.observation.model.name': model, 'langfuse.observation.metadata.provider': provider, 'langfuse.observation.metadata.call': kind}
        if usage is not None:
            attrs['langfuse.observation.usage_details'] = json.dumps({'input': usage.input, 'output': usage.output, 'cache_read_input_tokens': usage.cache_read,
                                                                      'cache_creation_input_tokens': usage.cache_write + usage.cache_write_long})
        if cost is not None:
            attrs['langfuse.observation.cost_details'] = json.dumps({'total': round(cost, 6)})
        if stop:
            attrs['langfuse.observation.metadata.stop'] = stop
        self.add(Span(span_id(), self.root, f'{provider} {kind}', GENERATION, started, ended, attrs, error,
                      raw_of(given) if self.full and given is not None else None, raw_of(said) if self.full and said is not None else None,
                      None if self.full else brief_in, None if self.full else brief_out))

    def tool(self, name: str, started: float, ended: float, ok: bool, given=None, said=None) -> None:
        if self.full:
            self.add(Span(span_id(), self.root, str(name)[:META_MAX], TOOL, started, ended, {}, None if ok else TOOL_FAILED,
                          raw_of(given) if given is not None else None, raw_of(plain_text(said)) if said is not None else None))
            return
        try:
            brief_in = tool_input_brief(name, given, self.offered)
        except Exception as e:
            log.warning('tool call brief not made: %s', error_kind(e))
            brief_in = {'tool': UNKNOWN, 'args': []}
        self.add(Span(span_id(), self.root, brief_in['tool'], TOOL, started, ended, {}, None if ok else TOOL_FAILED, None, None, brief_in, tool_output_brief(ok, said)))


def brief_text(found: dict | None) -> str | None:
    if found is None:
        return None
    try:
        return cut(json.dumps(found, default=str))
    except Exception:
        return None


def count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def capped_names(names: list[str], most: int) -> list[str]:
    return names if len(names) <= most else names[:most] + [f'+{len(names) - most}']


def name_of(tool) -> str | None:
    if not isinstance(tool, dict):
        return None
    name = tool.get('name')
    if isinstance(name, str) and name:
        return name[:META_MAX]
    kind = tool.get('type')
    return kind[:META_MAX] if isinstance(kind, str) and kind else None


def tool_names(tools) -> list[str]:
    return [n for n in (name_of(t) for t in (tools if isinstance(tools, (list, tuple)) else [])) if n]


def offered_schemas(tools) -> dict[str, frozenset[str]]:
    out = {}
    for t in tools if isinstance(tools, (list, tuple)) else []:
        name = name_of(t)
        if not name:
            continue
        schema = t.get('input_schema') if isinstance(t.get('input_schema'), dict) else {}
        props = schema.get('properties') if isinstance(schema.get('properties'), dict) else {}
        out[name] = frozenset(k for k in props if isinstance(k, str))
    return out


def safe_name(name, allowed) -> str:
    return name if isinstance(name, str) and name in allowed else UNKNOWN


def prompt_chars(messages) -> int:
    rows = messages if isinstance(messages, (list, tuple)) else []
    total = 0
    for m in rows:
        try:
            total += len(plain_text(m.get('content') if isinstance(m, dict) else m))
        except Exception:
            continue
    return total


def asked_brief(system, messages, tools, usage) -> dict:
    rows = messages if isinstance(messages, (list, tuple)) else []
    found = {'messages': len(rows), 'system_chars': len(system) if isinstance(system, str) else 0, 'prompt_chars': prompt_chars(rows)}
    if usage is not None:
        try:
            found['input_tokens'] = count(usage.input) + count(usage.cache_read) + count(usage.cache_write) + count(usage.cache_write_long)
        except Exception:
            pass
    return found | {'tools_offered': capped_names(tool_names(tools), OFFERED_MAX)}


def answer_brief(turn, tools=None) -> dict:
    allowed = set(tool_names(tools))
    calls = getattr(turn, 'calls', None)
    names = [safe_name(getattr(c, 'name', None), allowed) for c in calls] if isinstance(calls, (list, tuple)) else []
    said = getattr(turn, 'text', None)
    stop = getattr(turn, 'stop', None)
    return {'stop_reason': None if stop is None else stop if stop in STOPS else OTHER, 'tool_calls': capped_names(names, CALLS_MAX), 'answer_chars': len(said) if isinstance(said, str) else 0}


def tool_input_brief(name, args, offered: dict | None = None) -> dict:
    offered = offered or {}
    known = isinstance(name, str) and name in offered
    allowed = offered.get(name, frozenset()) if known else frozenset()
    keys = [k for k in args if isinstance(k, str) and k in allowed] if isinstance(args, dict) else []
    other = (len(args) - len(keys)) if isinstance(args, dict) else 0
    return {'tool': name if known else UNKNOWN, 'args': sorted(keys)} | ({'other': other} if other else {})


def tool_output_brief(ok: bool, said) -> dict:
    try:
        size = len(plain_text(said))
    except Exception:
        size = 0
    return {'ok': bool(ok)} | ({} if ok else {'error': TOOL_FAILED}) | {'result_chars': size}


def shown(text: str | None) -> str | None:
    return None if text is None else cut(redact_secrets(text[:RAW_MAX]))


def value_of(v) -> dict:
    if isinstance(v, bool):
        return {'boolValue': v}
    if isinstance(v, int):
        return {'intValue': str(v)}
    if isinstance(v, float):
        return {'doubleValue': v}
    return {'stringValue': str(v)}


def attributes(found: dict) -> list[dict]:
    return [{'key': k, 'value': value_of(v)} for k, v in found.items() if v is not None]


def otlp_span(trace: Trace, span: Span, common: dict) -> dict:
    attrs = dict(common) | {'langfuse.observation.type': span.kind} | span.attrs
    if span.error:
        attrs |= {'langfuse.observation.level': 'ERROR', 'langfuse.observation.status_message': span.error}
    attrs['langfuse.observation.input'] = shown(span.input) if span.input is not None else brief_text(span.brief_in)
    attrs['langfuse.observation.output'] = shown(span.output) if span.output is not None else brief_text(span.brief_out)
    out = {'traceId': trace.id, 'spanId': span.id, 'name': span.name, 'kind': 1, 'startTimeUnixNano': nanos(span.start), 'endTimeUnixNano': nanos(max(span.end, span.start)),
           'attributes': attributes(attrs), 'status': {'code': ERROR, 'message': span.error} if span.error else {'code': OK}}
    if span.parent:
        out['parentSpanId'] = span.parent
    return out


def encoded(traces: list[Trace], version: str = __version__) -> dict:
    spans = []
    for trace in traces:
        common = {'langfuse.trace.name': trace.name, 'langfuse.release': version} | {f'langfuse.trace.metadata.{k}': v for k, v in trace.meta.items()}
        root = Span(trace.root, None, trace.name, SPAN, trace.started, trace.ended or trace.started, {}, trace.error, trace.input, trace.output, trace.root_brief_in(), trace.root_brief_out())
        if trace.dropped:
            root.attrs['langfuse.observation.metadata.droppedSpans'] = trace.dropped
        spans += [otlp_span(trace, s, common) for s in [root] + trace.spans]
    return {'resourceSpans': [{'resource': {'attributes': attributes({'service.name': SERVICE, 'service.version': version})},
                               'scopeSpans': [{'scope': {'name': SERVICE, 'version': version}, 'spans': spans}]}]}


class Tracer:
    def __init__(self, on: Callable[[], bool], target: Callable[[], Target | None], content: Callable[[], str] = lambda: METADATA, sample: Callable[[], float] = lambda: 100,
                 workspace: Callable[[], str] = lambda: '', clock: Callable[[], float] = time.time, chance: Callable[[], float] = random.random, queue_max: int = QUEUE_MAX,
                 threaded: bool = True, version: str = __version__):
        self.on = on
        self.target = target
        self.content = content
        self.sample = sample
        self.workspace = workspace
        self.clock = clock
        self.chance = chance
        self.queue_max = queue_max
        self.threaded = threaded
        self.version = version
        self.queue: deque[Trace] = deque()
        self.projects: dict[tuple[str, str], tuple[str | None, float]] = {}
        self._looking: set[tuple[str, str]] = set()
        self.dropped = 0
        self.sent = 0
        self.failures = 0
        self._lock = threading.Lock()
        self._flushing = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def switched_on(self) -> bool:
        try:
            return bool(self.on())
        except Exception as e:
            log.warning('tracing switch not read, nothing is sent: %s', error_kind(e))
            return False

    def _drop(self, count: int, failed: bool = False) -> int:
        with self._lock:
            self.dropped += count
            if failed:
                self.failures += 1
            return self.failures

    def active(self) -> bool:
        try:
            return bool(self.on()) and self.target() is not None
        except Exception as e:
            log.warning('tracing settings not read, nothing is traced: %s', error_kind(e))
            return False

    def open(self, name: str, meta: dict) -> Trace | None:
        if not self.active():
            return None
        try:
            share = max(0.0, min(100.0, float(self.sample())))
            full = self.content() == FULL
        except Exception as e:
            log.warning('tracing settings not read, nothing is traced: %s', error_kind(e))
            return None
        base = {'workspace': self.workspace(), 'skopaxVersion': self.version}
        return Trace(name, base | meta, self.chance() * 100 < share, full, self.clock())

    def close(self, trace: Trace) -> bool:
        if trace.closed:
            return False
        trace.closed = True
        trace.ended = self.clock()
        if not trace.sends:
            return False
        if not self.switched_on():
            self._drop(1)
            self._wake.set()
            return False
        self.put(trace)
        return True

    def put(self, trace: Trace) -> None:
        with self._lock:
            if len(self.queue) >= self.queue_max:
                self.queue.popleft()
                self.dropped += 1
            self.queue.append(trace)
        self._started()
        self._wake.set()

    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def _started(self) -> None:
        if not self.threaded or self._stop.is_set():
            return
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name='skopax-tracing', daemon=True)
                self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(WAIT_S)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                failed = self.drain()
            except Exception as e:
                log.warning('traces not sent: %s', error_kind(e))
                failed = True
            if not self.switched_on():
                with self._lock:
                    if not self.queue:
                        self._thread = None
                        return
            if failed:
                self._stop.wait(PAUSE_S)

    def _take(self) -> list[Trace]:
        taken, spans = [], 0
        with self._lock:
            while self.queue and len(taken) < BATCH_MAX:
                size = len(self.queue[0].spans) + 1
                if taken and spans + size > SPANS_PER_POST:
                    break
                taken.append(self.queue.popleft())
                spans += size
        return taken

    def drain(self) -> bool:
        with self._flushing:
            while True:
                batch = self._take()
                if not batch:
                    return False
                if not self._send(batch):
                    return True

    def _send(self, batch: list[Trace]) -> bool:
        if not self.switched_on():
            self._drop(len(batch))
            return True
        try:
            target = self.target()
        except Exception as e:
            target = None
            log.warning('tracing target not read: %s', error_kind(e))
        if target is None:
            self._drop(len(batch))
            return True
        try:
            body = encoded(batch, self.version)
            call(target.transport, 'POST', target.url + OTLP, target.headers(), body, timeout=TIMEOUT_S)
        except NotJson:
            pass
        except Exception as e:
            failures = self._drop(len(batch), failed=True)
            if failures == 1 or failures % LOG_EVERY == 0:
                log.warning('%d traces not sent to Langfuse %s (failure %d): %s', len(batch), target.name, failures, error_kind(e))
            return False
        with self._lock:
            back, self.failures = self.failures, 0
            self.sent += len(batch)
        if back:
            log.info('traces reach Langfuse %s again', target.name)
        self._project(target)
        self._told(batch, target)
        return True

    def _told(self, batch: list[Trace], target: Target) -> None:
        sent = {'url': target.url, 'project': (self.projects.get(target.key) or (None, 0))[0]}
        for trace in batch:
            if trace.on_sent is None:
                continue
            try:
                trace.on_sent(sent | {'id': trace.id})
            except Exception as e:
                log.warning('the trace of a run not noted: %s', error_kind(e))

    def _project(self, target: Target) -> None:
        known = self.projects.get(target.key)
        if known is not None and (known[0] is not None or self.clock() - known[1] < PROJECT_RETRY_S):
            return
        found = None
        try:
            data = call(target.transport, 'GET', target.url + PROJECTS, target.headers(), timeout=TIMEOUT_S).body
            rows = data.get('data') if isinstance(data, dict) else None
            first = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
            found = first.get('id') if isinstance(first.get('id'), str) and first.get('id') else None
        except Exception as e:
            log.warning('the Langfuse project of %s not read, runs show no trace link: %s', target.name, error_kind(e))
        self.projects[target.key] = (found, self.clock())

    def project_url(self) -> str | None:
        try:
            target = self.target() if self.on() else None
        except Exception as e:
            log.warning('tracing settings not read, no project link: %s', error_kind(e))
            return None
        if target is None:
            return None
        known = self.projects.get(target.key)
        if known is None and self.threaded:
            self._look(target)
        base = project_base(target.url, known[0] if known else None)
        return f'{base}/traces' if base else None

    def _look(self, target: Target) -> None:
        with self._lock:
            if target.key in self._looking:
                return
            self._looking.add(target.key)

        def read() -> None:
            try:
                self._project(target)
            finally:
                with self._lock:
                    self._looking.discard(target.key)

        threading.Thread(target=read, name='skopax-tracing-project', daemon=True).start()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(JOIN_S)


def target_of(plugin, info, transport_for: Callable) -> Target | None:
    config = info.config
    url = (config.get('url') or DEFAULT_URL).strip().rstrip('/')
    public, secret = (config.get('public-key') or '').strip(), (config.get('secret-key') or '').strip()
    if not (url.startswith(('https://', 'http://')) and public and secret):
        return None
    return Target(info.name, url, public, secret, transport_for(plugin, info))


TRACER: list[Tracer | None] = [None]
CURRENT: contextvars.ContextVar[Trace | None] = contextvars.ContextVar('skopax_trace', default=None)


def install(tracer: Tracer | None) -> None:
    TRACER[0] = tracer


def uninstall(tracer: Tracer) -> None:
    if TRACER[0] is tracer:
        TRACER[0] = None


def installed() -> Tracer | None:
    return TRACER[0]


def current() -> Trace | None:
    return CURRENT.get()


def now() -> float:
    found = TRACER[0]
    return found.clock() if found is not None else time.time()


@dataclass
class Handle:
    trace: Trace | None
    token: contextvars.Token | None
    tracer: Tracer | None


def begin(name: str, **meta) -> Handle:
    found = TRACER[0]
    trace = None
    if found is not None:
        try:
            trace = found.open(name, meta)
        except Exception as e:
            log.warning('trace not started: %s', error_kind(e))
    return Handle(trace, CURRENT.set(trace) if trace is not None else None, found)


def end(handle: Handle) -> bool:
    if handle.token is not None:
        try:
            CURRENT.reset(handle.token)
        except (ValueError, RuntimeError):
            CURRENT.set(None)
    if handle.trace is None or handle.tracer is None:
        return False
    try:
        return handle.tracer.close(handle.trace)
    except Exception as e:
        log.warning('trace not queued: %s', error_kind(e))
        return False


@contextlib.contextmanager
def opened(name: str, **meta) -> Iterator[Trace | None]:
    handle = begin(name, **meta)
    try:
        yield handle.trace
    except BaseException as e:
        failed(handle.trace, e)
        raise
    finally:
        end(handle)


def failed(trace: Trace | None, e: BaseException) -> None:
    if trace is None:
        return
    try:
        trace.fail(error_kind(e))
    except Exception as bad:
        log.warning('trace not marked failed: %s', error_kind(bad))


def trace_link(found) -> str | None:
    if not isinstance(found, dict):
        return None
    trace_id = found.get('id')
    base = project_base(found.get('url'), found.get('project'))
    return f'{base}/traces/{trace_id}' if base and isinstance(trace_id, str) and trace_id else None


def project_base(url, project) -> str | None:
    if not all(isinstance(v, str) and v for v in (url, project)) or not url.startswith(('https://', 'http://')):
        return None
    return f'{url}/project/{project}'


def priced(model: str, usage) -> float | None:
    try:
        from skopax.investigator.pricing import usd
        return usd(model, usage)
    except Exception:
        return None


def generation(provider: str, kind: str, model: str, system: str, messages: list, call_model: Callable[[], object], tools: list | None = None):
    trace = CURRENT.get()
    if trace is None:
        found = TRACER[0]
        if found is None or not found.active():
            return call_model()
        handle = begin('model call', provider=provider)
        if handle.trace is None:
            return call_model()
        try:
            return generation(provider, kind, model, system, messages, call_model, tools)
        except BaseException as e:
            failed(handle.trace, e)
            raise
        finally:
            end(handle)
    if tools and not trace.full:
        try:
            trace.offer(tools)
        except Exception as e:
            log.warning('offered tools not noted: %s', error_kind(e))
    started = now()
    try:
        turn = call_model()
    except Exception as e:
        try:
            trace.generation(provider, kind, model, started, now(), error=error_kind(e), given=asked(system, messages) if trace.full else None,
                             brief_in=None if trace.full else asked_brief(system, messages, tools, None), brief_out=None if trace.full else {'error': error_kind(e)})
        except Exception as bad:
            log.warning('model call not traced: %s', error_kind(bad))
        raise
    try:
        served = getattr(turn, 'model', None) or model
        usage = getattr(turn, 'usage', None)
        trace.generation(provider, kind, served, started, now(), usage, priced(served, usage) if usage is not None else None, getattr(turn, 'stop', None),
                         given=asked(system, messages) if trace.full else None, said=answer(turn) if trace.full else None,
                         brief_in=None if trace.full else asked_brief(system, messages, tools, usage), brief_out=None if trace.full else answer_brief(turn, tools))
    except Exception as e:
        log.warning('model call not traced: %s', error_kind(e))
    return turn


def asked(system: str, messages: list) -> dict:
    last = messages[-1] if messages else {}
    return {'last': {'role': last.get('role') if isinstance(last, dict) else None, 'text': cut(plain_text(last.get('content') if isinstance(last, dict) else last), LAST_MAX)},
            'system': cut(str(system or ''), SYSTEM_MAX)}


def answer(turn) -> dict:
    calls = [{'tool': c.name, 'input': c.input} for c in getattr(turn, 'calls', None) or []]
    return {'text': getattr(turn, 'text', '') or ''} | ({'calls': calls} if calls else {})


def tool(name: str, started: float, ok: bool, given=None, said=None) -> None:
    trace = CURRENT.get()
    if trace is None:
        return
    try:
        trace.tool(name, started, now(), ok, given, said)
    except Exception as e:
        log.warning('tool call not traced: %s', error_kind(e))
