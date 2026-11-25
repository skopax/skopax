import hashlib
import json
import re
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpcore2
import httpx2
import openai

from skopax import tracing
from skopax.ai import THINK_OFF, ToolCall, Turn, Usage
from skopax.errkind import Authored, kind
from skopax.instances import NAME
from skopax.mcptools.client import PRIVATE_NETS, address, embedded, refused as metadata
from skopax.messages import msg

PREFIX = 'compat'
TOOLS = 'tools'
IMAGES = 'images'
NO_IMAGES = 'images not sent: this model does not read images'
ERROR_MARK = 'Error: '
SCHEMES = ('http', 'https')
MODELS_MAX = 50
MODEL_ID = re.compile(r'[\x21-\x7e]{1,200}')
KEY = re.compile(r'[\x21-\x7e]{1,400}')
URL_MAX = 500
PRICE_MAX = 1000.0
NO_KEY = 'none'
TIMEOUT_S = 120
RETRIES = 2
TEST_TIMEOUT_S = 10
POOL_CONNECTIONS = 10
KEEPALIVE_S = 5
ANSWER_ASK = 'Reply with the single word ok.'
ECHO_ASK = 'Call the echo tool with the text ok.'
ECHO = {'type': 'function', 'function': {'name': 'echo', 'description': 'Repeats the text it is given', 'parameters': {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']}}}
FORCED = {'type': 'function', 'function': {'name': 'echo'}}
OWN_TOOLS = (None, 'custom')
JSON_OBJECT = {'type': 'json_object'}
FORMAT_WORDS = ('response_format', 'json_schema')

resolve = socket.getaddrinfo


class Refused(Authored, ValueError):
    pass


class EmptyAnswer(openai.OpenAIError):
    def __init__(self):
        super().__init__('the response held no answer')


ERRORS = ((httpcore2.ConnectTimeout, httpx2.ConnectTimeout), (httpcore2.ReadTimeout, httpx2.ReadTimeout), (httpcore2.WriteTimeout, httpx2.WriteTimeout),
          (httpcore2.PoolTimeout, httpx2.PoolTimeout), (httpcore2.TimeoutException, httpx2.TimeoutException), (httpcore2.ConnectError, httpx2.ConnectError),
          (httpcore2.ReadError, httpx2.ReadError), (httpcore2.WriteError, httpx2.WriteError), (httpcore2.NetworkError, httpx2.NetworkError),
          (httpcore2.RemoteProtocolError, httpx2.RemoteProtocolError), (httpcore2.LocalProtocolError, httpx2.LocalProtocolError), (httpcore2.ProtocolError, httpx2.ProtocolError))


@dataclass(frozen=True)
class Model:
    id: str
    input: float | None = None
    output: float | None = None


@dataclass(frozen=True)
class Connection:
    name: str
    base_url: str
    key: str = field(default='', repr=False)
    models: tuple[Model, ...] = ()
    tools: bool = True
    images: bool = False
    by: str | None = None
    at: str | None = None
    last_test: dict | None = None
    unreadable: bool = False

    @property
    def provider(self) -> str:
        return provider_id(self.name)

    @property
    def company(self) -> str:
        return urlsplit(self.base_url).hostname or ''

    @property
    def capabilities(self) -> frozenset[str]:
        return capabilities_of(self.tools, self.images)

    def ref(self, model: str) -> str:
        return f'{self.name}/{model}'

    def view(self) -> dict:
        return {'name': self.name, 'provider': self.provider, 'baseUrl': self.base_url, 'company': self.company,
                'models': [{'id': m.id, 'ref': self.ref(m.id), 'input': m.input, 'output': m.output} for m in self.models], 'tools': self.tools, 'images': self.images,
                'key': {'set': bool(self.key), 'unreadable': self.unreadable}, 'updatedBy': self.by, 'updatedAt': self.at, 'lastTest': self.last_test}


def origin(url: str) -> tuple:
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        port = None
    return parts.scheme.lower(), (parts.hostname or '').lower(), port


def moved(url: str, before: str | None) -> bool:
    return before is not None and origin(url) != origin(before)


def provider_id(name: str) -> str:
    return f'{PREFIX}:{name}'


def name_of(provider: str) -> str:
    return provider[len(PREFIX) + 1:] if provider.startswith(PREFIX + ':') else provider


def parse(ref: object) -> tuple[str, str] | None:
    if not isinstance(ref, str) or '/' not in ref:
        return None
    name, model = ref.split('/', 1)
    return (name, model) if NAME.fullmatch(name) and MODEL_ID.fullmatch(model) else None


def capabilities_of(tools: bool, images: bool) -> frozenset[str]:
    return frozenset(c for c, on in ((TOOLS, tools), (IMAGES, images)) if on)


def checked_name(name: object) -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise Refused(msg('compat.name_invalid'))
    return name


def checked_key(value: object) -> str:
    key = value.strip() if isinstance(value, str) else ''
    if not KEY.fullmatch(key):
        raise Refused(msg('compat.key_invalid'))
    return key


def priced(item: dict) -> tuple[float | None, float | None]:
    given, taken = price(item.get('input')), price(item.get('output'))
    if (given is None) != (taken is None):
        raise Refused(msg('compat.price_half'))
    return given, taken


def price(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= PRICE_MAX:
        raise Refused(msg('compat.models_invalid'))
    return float(value)


def checked_models(items: object) -> tuple[Model, ...]:
    if not isinstance(items, (list, tuple)) or len(items) > MODELS_MAX:
        raise Refused(msg('compat.models_invalid'))
    out = []
    for item in items:
        mid = item.get('id') if isinstance(item, dict) else None
        if not isinstance(mid, str) or not MODEL_ID.fullmatch(mid) or any(m.id == mid for m in out):
            raise Refused(msg('compat.models_invalid'))
        out.append(Model(mid, *priced(item)))
    return tuple(out)


def inside(ip) -> bool:
    return any(x.is_private or x.is_loopback or getattr(x, 'is_site_local', False) or any(x in net for net in PRIVATE_NETS) for x in embedded(ip))


def blocked(ip) -> bool:
    return metadata(ip) or any(x.is_link_local or x.is_multicast or x.is_unspecified for x in embedded(ip))


def addresses(host: str, port: int | None) -> list:
    literal = address(host)
    if literal is not None:
        return [literal]
    return [address(str(info[4][0])) for info in resolve(host, port, type=socket.SOCK_STREAM) or ()]


def check_addresses(found: list, allow_private: bool) -> None:
    if not found:
        raise Refused(msg('compat.url_unresolved'))
    if any(ip is None or blocked(ip) for ip in found):
        raise Refused(msg('compat.url_blocked'))
    if not allow_private and any(inside(ip) for ip in found):
        raise Refused(msg('compat.url_private'))


def private_target(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return any(ip is not None and inside(ip) for ip in addresses(parts.hostname or '', parts.port))
    except Exception:
        return True


def check_url(url: object, allow_private: bool) -> str:
    text = url.strip() if isinstance(url, str) else ''
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise Refused(msg('compat.url_shape')) from None
    if parts.scheme.lower() not in SCHEMES:
        raise Refused(msg('compat.url_scheme'))
    if '@' in parts.netloc or parts.username is not None or parts.password is not None:
        raise Refused(msg('compat.url_credentials'))
    if not parts.hostname or parts.query or parts.fragment or '?' in text or '#' in text or len(text) > URL_MAX or any(c.isspace() for c in text):
        raise Refused(msg('compat.url_shape'))
    try:
        found = addresses(parts.hostname, port)
    except (OSError, UnicodeError):
        raise Refused(msg('compat.url_unresolved')) from None
    check_addresses(found, allow_private)
    return text.rstrip('/')


class GuardedBackend(httpcore2.NetworkBackend):
    def __init__(self, allow_private: Callable[[], bool]):
        self.allow_private = allow_private
        self.backend = httpcore2.SyncBackend()

    def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None, socket_options=None) -> httpcore2.NetworkStream:
        try:
            found = addresses(host, port)
        except (OSError, UnicodeError):
            raise httpcore2.ConnectError() from None
        try:
            check_addresses(found, bool(self.allow_private()))
        except Refused as e:
            raise httpcore2.ConnectError(e.text) from e
        failed: Exception = httpcore2.ConnectError()
        for ip in found:
            try:
                return self.backend.connect_tcp(str(ip), port, timeout=timeout, local_address=local_address, socket_options=socket_options)
            except (httpcore2.ConnectError, httpcore2.ConnectTimeout) as e:
                failed = e
        raise failed

    def sleep(self, seconds: float) -> None:
        self.backend.sleep(seconds)


def mapped(error: Exception, request: httpx2.Request) -> Exception:
    found = next((theirs for ours, theirs in ERRORS if isinstance(error, ours)), None)
    return error if found is None else found(kind(error), request=request)


class GuardedStream(httpx2.SyncByteStream):
    def __init__(self, stream, request: httpx2.Request):
        self.stream = stream
        self.request = request

    def __iter__(self):
        try:
            yield from self.stream
        except Exception as e:
            raise mapped(e, self.request) from e

    def close(self) -> None:
        closing = getattr(self.stream, 'close', None)
        if callable(closing):
            closing()


class GuardedTransport(httpx2.BaseTransport):
    def __init__(self, allow_private: Callable[[], bool]):
        self.backend = GuardedBackend(allow_private)
        self.pool = httpcore2.ConnectionPool(ssl_context=httpx2.create_ssl_context(), network_backend=self.backend, max_connections=POOL_CONNECTIONS,
                                             keepalive_expiry=KEEPALIVE_S)

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        sent = httpcore2.Request(method=request.method, url=httpcore2.URL(scheme=request.url.raw_scheme, host=request.url.raw_host, port=request.url.port, target=request.url.raw_path),
                                 headers=request.headers.raw, content=request.stream, extensions=request.extensions)
        try:
            got = self.pool.handle_request(sent)
        except Exception as e:
            raise mapped(e, request) from e
        return httpx2.Response(status_code=got.status, headers=got.headers, stream=GuardedStream(got.stream, request), extensions=got.extensions)

    def close(self) -> None:
        self.pool.close()


def client_for(base_url: str, key: str, allow_private: Callable[[], bool], timeout: float = TIMEOUT_S, retries: int = RETRIES,
               transport: httpx2.BaseTransport | None = None) -> openai.OpenAI:
    http = httpx2.Client(transport=transport or GuardedTransport(allow_private), timeout=httpx2.Timeout(timeout), follow_redirects=False, trust_env=False)
    client = openai.OpenAI(api_key=key or NO_KEY, base_url=base_url, timeout=timeout, max_retries=retries, http_client=http)
    client.organization = None
    client.project = None
    return client


def part(obj, name: str):
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def image_part(block) -> dict:
    source = part(block, 'source') or {}
    return {'type': 'image_url', 'image_url': {'url': f"data:{part(source, 'media_type')};base64,{part(source, 'data')}"}}


def flat_text(content) -> str:
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    return '\n'.join(str(part(b, 'text') or '') for b in content if part(b, 'type') == 'text')


def has_images(content) -> bool:
    return isinstance(content, list) and any(part(b, 'type') == 'image' for b in content)


def arguments(raw) -> dict | None:
    if raw is None or raw == '':
        return {}
    try:
        found = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def function_of(tool: dict) -> dict:
    return {'type': 'function', 'function': {'name': tool['name'], 'description': tool.get('description', ''), 'parameters': tool.get('input_schema') or {'type': 'object', 'properties': {}}}}


def usage_of(resp) -> Usage:
    u = part(resp, 'usage')
    if u is None:
        return Usage()
    cached = int(part(part(u, 'prompt_tokens_details'), 'cached_tokens') or 0)
    return Usage(input=max(0, int(part(u, 'prompt_tokens') or 0) - cached), cache_read=cached, output=int(part(u, 'completion_tokens') or 0))


def turn_of(resp, ref: str) -> Turn:
    choice = next(iter(part(resp, 'choices') or []), None)
    if choice is None:
        raise EmptyAnswer()
    message = part(choice, 'message')
    finish = part(choice, 'finish_reason')
    said = part(message, 'content')
    text = said if isinstance(said, str) else ''
    calls = []
    for tc in part(message, 'tool_calls') or []:
        fn = part(tc, 'function')
        found = arguments(part(fn, 'arguments'))
        calls.append(ToolCall(str(part(tc, 'id') or ''), str(part(fn, 'name') or ''), found or {}, found is None))
    if part(message, 'refusal') or finish == 'content_filter':
        stop = 'refusal'
    elif finish == 'length':
        stop = 'max_tokens'
    else:
        stop = 'tool_use' if calls else 'end_turn'
    native = ([{'type': 'text', 'text': text}] if text else []) + [{'type': 'tool_use', 'id': c.id, 'name': c.name, 'input': c.input} for c in calls]
    return Turn(stop=stop, model=ref, usage=usage_of(resp), texts=[text] if text else [], text=text.strip(), calls=calls, native=native, server=[])


def about_format(error: openai.BadRequestError) -> bool:
    said = json.dumps([getattr(error, 'message', None), getattr(error, 'body', None)], default=repr).lower()
    return any(word in said for word in FORMAT_WORDS)


class CompatAi:
    bad_request = openai.BadRequestError
    api_error = (openai.APIError, EmptyAnswer)

    def __init__(self, client, name: str, tools: bool = True, images: bool = False):
        self.client = client
        self.name = name
        self.tools = tools
        self.images = images
        self.provider = provider_id(name)
        self.capabilities = capabilities_of(tools, images)

    def wire(self, ref: str) -> str:
        found = parse(ref)
        return found[1] if found is not None and found[0] == self.name else ref

    def user_messages(self, content) -> list[dict]:
        if isinstance(content, str):
            return [{'role': 'user', 'content': content}]
        out, pieces, dropped = [], [], False
        for b in content or []:
            kind = part(b, 'type')
            if kind == 'tool_result':
                inner = part(b, 'content')
                said = flat_text(inner)
                out.append({'role': 'tool', 'tool_call_id': part(b, 'tool_use_id'), 'content': ERROR_MARK + said if part(b, 'is_error') else said})
                if has_images(inner):
                    if self.images:
                        pieces += [image_part(i) for i in inner if part(i, 'type') == 'image']
                    else:
                        dropped = True
            elif kind == 'text':
                pieces.append({'type': 'text', 'text': part(b, 'text') or ''})
            elif kind == 'image':
                if self.images:
                    pieces.append(image_part(b))
                else:
                    dropped = True
        if dropped:
            pieces.append({'type': 'text', 'text': NO_IMAGES})
        return out + ([{'role': 'user', 'content': pieces}] if pieces else [])

    def assistant_messages(self, content) -> list[dict]:
        if isinstance(content, str):
            return [{'role': 'assistant', 'content': content}]
        said = ''.join(str(part(b, 'text') or '') for b in content or [] if part(b, 'type') == 'text')
        calls = [{'id': part(b, 'id'), 'type': 'function', 'function': {'name': part(b, 'name'), 'arguments': json.dumps(part(b, 'input') or {})}} for b in content or [] if part(b, 'type') == 'tool_use']
        if not said and not calls:
            return []
        return [{'role': 'assistant', 'content': said or None} | ({'tool_calls': calls} if calls else {})]

    def messages_of(self, system: str, messages: list[dict]) -> list[dict]:
        out = [{'role': 'system', 'content': system}] if system else []
        for m in messages:
            out += self.assistant_messages(m.get('content')) if m.get('role') == 'assistant' else self.user_messages(m.get('content'))
        return out

    def tools_of(self, tools: list[dict]) -> list[dict]:
        if not self.tools:
            return []
        return [function_of(t) for t in tools or [] if t.get('type') in OWN_TOOLS and t.get('name')]

    def turn(self, model: str, effort: str, system: str, tools: list[dict], messages: list[dict], max_tokens: int, cache_ttl: str | None = None, thinking: str = THINK_OFF,
             fallbacks: bool = False) -> Turn:
        offered = self.tools_of(tools)
        extra = {'tools': offered} if offered else {}
        return tracing.generation(self.provider, 'turn', model, system, messages, lambda: turn_of(self.client.chat.completions.create(
            model=self.wire(model), messages=self.messages_of(system, messages), max_tokens=max_tokens, **extra), model), tools)

    def once(self, model: str, system: str, messages: list[dict], max_tokens: int, effort: str, thinking: str = THINK_OFF, schema: dict | None = None, fallbacks: bool = False) -> Turn:
        sent = self.messages_of(system, messages)

        def ask(form: dict | None):
            return self.client.chat.completions.create(model=self.wire(model), messages=sent, max_tokens=max_tokens, **({'response_format': form} if form else {}))

        def call():
            if schema is None:
                return turn_of(ask(None), model)
            try:
                return turn_of(ask({'type': 'json_schema', 'json_schema': {'name': 'answer', 'schema': schema}}), model)
            except openai.BadRequestError as e:
                if not about_format(e):
                    raise
                return turn_of(ask(JSON_OBJECT), model)
        return tracing.generation(self.provider, 'once', model, system, messages, call)


def chain(error: BaseException | None):
    seen = 0
    while error is not None and seen < 10:
        yield error
        error, seen = error.__cause__ or error.__context__, seen + 1


def failure(error: BaseException, model: str) -> str:
    if any(isinstance(e, Refused) for e in chain(error)):
        return msg('compat.test_refused')
    if isinstance(error, EmptyAnswer):
        return msg('compat.test_silent')
    if isinstance(error, openai.AuthenticationError):
        return msg('compat.test_rejected')
    if isinstance(error, openai.PermissionDeniedError):
        return msg('compat.test_forbidden', model=model)
    if isinstance(error, openai.NotFoundError):
        return msg('compat.test_no_model', model=model)
    if isinstance(error, openai.APITimeoutError):
        return msg('compat.test_timeout')
    if isinstance(error, openai.APIConnectionError):
        return msg('compat.test_unreachable')
    if isinstance(error, openai.APIStatusError):
        return msg('compat.test_status', status=error.status_code)
    return msg('compat.test_failed')


def probe(client, model: str) -> dict:
    try:
        first = turn_of(client.chat.completions.create(model=model, messages=[{'role': 'user', 'content': ANSWER_ASK}], max_tokens=20), model)
    except Exception as e:
        return {'ok': False, 'tools': False, 'summary': failure(e, model), 'error': e}
    if not first.text:
        return {'ok': False, 'tools': False, 'summary': msg('compat.test_silent')}
    try:
        second = turn_of(client.chat.completions.create(model=model, messages=[{'role': 'user', 'content': ECHO_ASK}], tools=[ECHO], tool_choice=FORCED, max_tokens=100), model)
    except (openai.BadRequestError, EmptyAnswer):
        return {'ok': False, 'tools': False, 'summary': msg('compat.test_no_tools')}
    except Exception as e:
        return {'ok': False, 'tools': False, 'summary': failure(e, model), 'error': e}
    if not any(c.name == 'echo' for c in second.calls):
        return {'ok': False, 'tools': False, 'summary': msg('compat.test_no_tools')}
    return {'ok': True, 'tools': True, 'summary': msg('compat.test_tools')}


class Bound:
    def __init__(self):
        self.connections = None
        self.make: Callable[..., object] = client_for
        self.allow_private: Callable[[], bool] = lambda: False
        self._built: dict[tuple[str, float, int], tuple[str, CompatAi]] = {}
        self._lock = threading.Lock()

    def get(self, name: str) -> Connection | None:
        return None if self.connections is None else self.connections.get(name)

    def all(self) -> list[Connection]:
        return [] if self.connections is None else self.connections.all()


BOUND = Bound()


def bind(connections, allow_private: Callable[[], bool], make: Callable[..., object] | None = None) -> None:
    from skopax.investigator import pricing
    with BOUND._lock:
        BOUND.connections, BOUND.allow_private, BOUND.make, BOUND._built = connections, allow_private, make or client_for, {}
    pricing.COMPAT[:] = [compat_models]


def unbind() -> None:
    from skopax.investigator import pricing
    with BOUND._lock:
        BOUND.connections, BOUND.allow_private, BOUND.make, BOUND._built = None, (lambda: False), client_for, {}
    pricing.COMPAT.clear()


def forget(connections, names: set[str]) -> None:
    with BOUND._lock:
        if BOUND.connections is connections:
            BOUND._built = {k: v for k, v in BOUND._built.items() if k[0] in names}


def compat_models() -> dict[str, dict | None]:
    out: dict[str, dict | None] = {}
    for c in BOUND.all():
        for m in c.models:
            out[c.ref(m.id)] = None if m.input is None or m.output is None else {'input_tokens': m.input, 'output_tokens': m.output}
    return out


def factory(provider: str, timeout: float = TIMEOUT_S, retries: int = RETRIES) -> CompatAi | None:
    found = BOUND.get(name_of(provider))
    if found is None or found.unreadable:
        return None
    digest = hashlib.sha256('\n'.join((found.base_url, found.key, str(found.tools), str(found.images), str(timeout), str(retries))).encode()).hexdigest()[:16]
    with BOUND._lock:
        held = BOUND._built.get((found.name, timeout, retries))
        if held is not None and held[0] == digest:
            return held[1]
        made = CompatAi(BOUND.make(found.base_url, found.key, BOUND.allow_private, timeout=timeout, retries=retries), found.name, found.tools, found.images)
        BOUND._built[(found.name, timeout, retries)] = (digest, made)
        return made


def capabilities(provider: str) -> frozenset[str]:
    found = BOUND.get(name_of(provider))
    return frozenset() if found is None else found.capabilities


def models() -> tuple[str, ...]:
    return tuple(c.ref(m.id) for c in BOUND.all() for m in c.models)


def company(ref: str) -> str:
    found = parse(ref)
    connection = None if found is None else BOUND.get(found[0])
    return '' if connection is None else connection.company


def register(ai_module) -> None:
    ai_module.register_provider(PREFIX, factory, capabilities, models, company)
