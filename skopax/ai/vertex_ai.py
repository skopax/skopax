import dataclasses
import hashlib
import json
import logging
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

import anthropic
import httpx2

from skopax import tracing
from skopax.ai import ADVISOR, CAPABILITIES, SEARCH_TYPE, THINK_AUTO, THINK_OFF, Turn
from skopax.ai.anthropic_ai import THINKING_ROOM, AnthropicAi, turn_of
from skopax.ai.compat_ai import Refused
from skopax.errkind import kind
from skopax.instances import NAME
from skopax.messages import msg

log = logging.getLogger('skopax.vertex')

PREFIX = 'vertex'
GLOBAL = 'global'
MULTI_HOSTS = {'us': 'aiplatform.us.rep.googleapis.com', 'eu': 'aiplatform.eu.rep.googleapis.com'}
REGIONAL = ('us-west1', 'us-west4', 'us-central1', 'us-east1', 'us-east4', 'us-east5', 'us-south1', 'northamerica-northeast1', 'southamerica-east1',
            'europe-west2', 'europe-west1', 'europe-west4', 'europe-west6', 'europe-west3', 'europe-north1', 'europe-central2', 'europe-west8', 'europe-southwest1', 'europe-west9',
            'asia-south1', 'asia-southeast1', 'asia-east2', 'asia-east1', 'asia-northeast1', 'australia-southeast1', 'asia-northeast3', 'me-west1', 'me-central1', 'me-central2')
REGIONS = (GLOBAL, *MULTI_HOSTS, *REGIONAL)
GLOBAL_HOST = 'aiplatform.googleapis.com'
SCOPES = ['https://www.googleapis.com/auth/cloud-platform']
MODES = ('key', 'federation', 'pod')
KEY_MODE, FEDERATION_MODE, POD_MODE = MODES
PROJECT = re.compile(r'[a-z][a-z0-9-]{4,28}[a-z0-9]')
MODEL_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}')
VERTEX_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._@-]{0,99}')
MODELS_MAX = 20
PRICE_MAX = 1000.0
KEY_MAX = 20000
FEDERATION_MAX = 8000
TOKEN_URI = 'https://oauth2.googleapis.com/token'
STS_URL = 'https://sts.googleapis.com/v1/token'
UNIVERSE = 'googleapis.com'
EMAIL = re.compile(r'[A-Za-z0-9._%+-]{1,100}@[A-Za-z0-9.-]{1,100}')
AUDIENCE = re.compile(r'//iam\.googleapis\.com/projects/[0-9]{1,20}/locations/global/workloadIdentityPools/[A-Za-z0-9._-]{1,100}/providers/[A-Za-z0-9._-]{1,100}')
IMPERSONATION = re.compile(r'https://iamcredentials\.googleapis\.com/v1/projects/-/serviceAccounts/[A-Za-z0-9._%@-]{1,200}:generateAccessToken')
SUBJECT_TYPES = ('urn:ietf:params:oauth:token-type:jwt', 'urn:ietf:params:oauth:token-type:id_token')
KEY_FIELDS = ('type', 'project_id', 'private_key_id', 'private_key', 'client_email', 'client_id', 'token_uri', 'universe_domain')
FEDERATION_FIELDS = frozenset(('type', 'audience', 'subject_token_type', 'token_url', 'service_account_impersonation_url', 'credential_source', 'universe_domain'))
SOURCE_FIELDS = frozenset(('file', 'format'))
FORMAT_FIELDS = frozenset(('type', 'subject_token_field_name'))
TOKEN_DIR = '/var/run/secrets/tokens'
TIMEOUT_S = 120
RETRIES = 2
TEST_TIMEOUT_S = 10
PROBE_TOKENS = 20
TOOL_TOKENS = 100
ANSWER_ASK = 'Reply with the single word ok.'
ECHO_ASK = 'Call the echo tool with the text ok.'
ECHO = {'name': 'echo', 'description': 'Repeats the text it is given', 'input_schema': {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']}}
FORCED = {'type': 'tool', 'name': 'echo'}
OWN_TOOLS = (None, 'custom')
VERTEX_CAPABILITIES = CAPABILITIES - {'web_fetch', 'fallbacks', ADVISOR}


class CredentialError(anthropic.AnthropicError):
    pass


@dataclass(frozen=True)
class Model:
    id: str
    vertex_id: str
    input: float | None = None
    output: float | None = None


@dataclass(frozen=True)
class Connection:
    name: str
    project: str
    region: str
    mode: str
    key: str = field(default='', repr=False)
    federation: dict | None = field(default=None, repr=False)
    models: tuple[Model, ...] = ()
    by: str | None = None
    at: str | None = None
    last_test: dict | None = None
    unreadable: bool = False

    @property
    def provider(self) -> str:
        return provider_id(self.name)

    @property
    def company(self) -> str:
        return msg('vertex.company', region=self.region)

    @property
    def credentials_set(self) -> bool:
        if self.mode == KEY_MODE:
            return bool(self.key) and not self.unreadable
        if self.mode == FEDERATION_MODE:
            return self.federation is not None
        return True

    def ref(self, model: str) -> str:
        return f'{self.name}/{model}'

    def view(self) -> dict:
        return {'name': self.name, 'project': self.project, 'region': self.region, 'credentials': {'mode': self.mode, 'set': self.credentials_set},
                'models': [{'id': m.id, 'vertexId': m.vertex_id, 'input': m.input, 'output': m.output} for m in self.models], 'lastTest': self.last_test, 'company': self.company}


def provider_id(name: str) -> str:
    return f'{PREFIX}:{name}'


def name_of(provider: str) -> str:
    return provider[len(PREFIX) + 1:] if provider.startswith(PREFIX + ':') else provider


def host_of(region: str) -> str:
    if region == GLOBAL:
        return GLOBAL_HOST
    if region in MULTI_HOSTS:
        return MULTI_HOSTS[region]
    if region in REGIONAL:
        return f'{region}-aiplatform.googleapis.com'
    raise Refused(msg('vertex.region_invalid'))


def checked_project(value: object) -> str:
    text = value.strip() if isinstance(value, str) else ''
    if not PROJECT.fullmatch(text):
        raise Refused(msg('vertex.project_invalid'))
    return text


def checked_region(value: object) -> str:
    text = value.strip() if isinstance(value, str) else ''
    if text not in REGIONS:
        raise Refused(msg('vertex.region_invalid'))
    return text


def checked_mode(value: object) -> str:
    if value not in MODES:
        raise Refused(msg('vertex.mode_invalid'))
    return value


def price(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= PRICE_MAX:
        raise Refused(msg('vertex.models_invalid'))
    return float(value)


def listed_price(vertex_id: str) -> tuple[float | None, float | None]:
    from skopax.investigator.pricing import PRICES
    found = PRICES.get(vertex_id.partition('@')[0])
    return (None, None) if found is None else (found['input_tokens'], found['output_tokens'])


def checked_models(items: object) -> tuple[Model, ...]:
    if not isinstance(items, (list, tuple)) or len(items) > MODELS_MAX:
        raise Refused(msg('vertex.models_invalid'))
    out = []
    for item in items:
        if not isinstance(item, dict):
            raise Refused(msg('vertex.models_invalid'))
        mid, vid = item.get('id'), item.get('vertexId')
        if not isinstance(mid, str) or not MODEL_ID.fullmatch(mid) or not isinstance(vid, str) or not VERTEX_ID.fullmatch(vid) or any(m.id == mid for m in out):
            raise Refused(msg('vertex.models_invalid'))
        given, taken = price(item.get('input')), price(item.get('output'))
        if (given is None) != (taken is None):
            raise Refused(msg('vertex.price_half'))
        if given is None:
            given, taken = listed_price(vid)
        out.append(Model(mid, vid, given, taken))
    return tuple(out)


def parse(ref: object) -> tuple[str, str] | None:
    if not isinstance(ref, str) or '/' not in ref:
        return None
    name, model = ref.split('/', 1)
    return (name, model) if NAME.fullmatch(name) and MODEL_ID.fullmatch(model) else None


def as_object(value: object, limit: int, refused: str) -> dict:
    if isinstance(value, dict):
        if len(json.dumps(value, default=str)) > limit:
            raise Refused(msg(refused))
        found = value
    elif isinstance(value, str) and value.strip() and len(value) <= limit:
        try:
            found = json.loads(value)
        except ValueError:
            raise Refused(msg(refused)) from None
    else:
        raise Refused(msg(refused))
    if not isinstance(found, dict):
        raise Refused(msg(refused))
    return found


def checked_key(value: object) -> str:
    info = as_object(value, KEY_MAX, 'vertex.key_invalid')
    if info.get('type') != 'service_account' or info.get('token_uri', TOKEN_URI) != TOKEN_URI or info.get('universe_domain', UNIVERSE) != UNIVERSE:
        raise Refused(msg('vertex.key_invalid'))
    email, pem = info.get('client_email'), info.get('private_key')
    if not isinstance(email, str) or not EMAIL.fullmatch(email) or not isinstance(pem, str) or 'PRIVATE KEY' not in pem or len(pem) > KEY_MAX:
        raise Refused(msg('vertex.key_invalid'))
    kept = {k: info[k] for k in KEY_FIELDS if isinstance(info.get(k), str)} | {'token_uri': TOKEN_URI}
    try:
        from google.oauth2 import service_account
        service_account.Credentials.from_service_account_info(kept, scopes=SCOPES)
    except Exception:
        raise Refused(msg('vertex.key_invalid')) from None
    return json.dumps(kept, separators=(',', ':'))


def token_dir_ok(root: object) -> bool:
    if not isinstance(root, str) or not root or '\x00' in root or not os.path.isabs(root) or '..' in root.split('/'):
        return False
    return len([part for part in os.path.normpath(root).split('/') if part]) >= 2


def token_path_ok(path: object, root: str) -> bool:
    if not token_dir_ok(root):
        return False
    if not isinstance(path, str) or not path or '\x00' in path or not os.path.isabs(path) or '..' in path.split('/'):
        return False
    base = os.path.normpath(root)
    clean = os.path.normpath(path)
    if not clean.startswith(base.rstrip('/') + '/'):
        return False
    real_base = os.path.realpath(base)
    return os.path.realpath(clean).startswith(real_base.rstrip('/') + '/')


def checked_federation(value: object, root: str | None) -> dict:
    info = as_object(value, FEDERATION_MAX, 'vertex.federation_invalid')
    if set(info) - FEDERATION_FIELDS or info.get('type') != 'external_account' or info.get('universe_domain', UNIVERSE) != UNIVERSE:
        raise Refused(msg('vertex.federation_invalid'))
    if not isinstance(info.get('audience'), str) or not AUDIENCE.fullmatch(info['audience']) or info.get('subject_token_type') not in SUBJECT_TYPES or info.get('token_url') != STS_URL:
        raise Refused(msg('vertex.federation_invalid'))
    link = info.get('service_account_impersonation_url')
    if link is not None and (not isinstance(link, str) or not IMPERSONATION.fullmatch(link)):
        raise Refused(msg('vertex.federation_invalid'))
    source = info.get('credential_source')
    if not isinstance(source, dict) or set(source) - SOURCE_FIELDS or not isinstance(source.get('file'), str):
        raise Refused(msg('vertex.federation_source'))
    form = source.get('format')
    if form is not None and (not isinstance(form, dict) or set(form) - FORMAT_FIELDS or form.get('type') not in ('text', 'json')
                             or not isinstance(form.get('subject_token_field_name', ''), str)):
        raise Refused(msg('vertex.federation_invalid'))
    if root is not None and not token_path_ok(source['file'], root):
        raise Refused(msg('vertex.federation_path', root=root))
    clean = {k: info[k] for k in FEDERATION_FIELDS if k in info}
    try:
        from google.auth import identity_pool
        identity_pool.Credentials.from_info(clean, scopes=SCOPES)
    except Exception:
        raise Refused(msg('vertex.federation_invalid')) from None
    return clean


def verified_pool(root: str):
    from google.auth import exceptions, identity_pool

    class Verified(identity_pool.Credentials):
        def refresh(self, request):
            if not token_path_ok(self._credential_source_file, root):
                raise exceptions.RefreshError(msg('vertex.federation_path', root=root))
            super().refresh(request)

    return Verified


def google_credentials(conn: Connection, token_dir: str):
    if conn.mode == KEY_MODE:
        from google.oauth2 import service_account
        return service_account.Credentials.from_service_account_info(json.loads(conn.key), scopes=SCOPES)
    if conn.mode == FEDERATION_MODE:
        info = checked_federation(conn.federation, token_dir)
        return verified_pool(token_dir).from_info(info, scopes=SCOPES)
    return None


class Locked(httpx2.BaseTransport):
    def __init__(self, host: str, inner: httpx2.BaseTransport):
        self.host = host
        self.inner = inner

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        url = request.url
        if url.scheme != 'https' or url.host != self.host or url.port not in (None, 443):
            raise httpx2.ConnectError(msg('vertex.endpoint_blocked'), request=request)
        return self.inner.handle_request(request)

    def close(self) -> None:
        self.inner.close()


def client_for(conn: Connection, token_dir: str, timeout: float = TIMEOUT_S, retries: int = RETRIES, transport: httpx2.BaseTransport | None = None, access_token: str | None = None):
    host = host_of(conn.region)
    locked = Locked(host, transport or httpx2.HTTPTransport())
    http = httpx2.Client(transport=locked, timeout=httpx2.Timeout(timeout), follow_redirects=False)
    credentials = None if access_token else google_credentials(conn, token_dir)
    client = anthropic.AnthropicVertex(region=conn.region, project_id=conn.project, base_url=f'https://{host}/v1', credentials=credentials, access_token=access_token, http_client=http,
                                       timeout=timeout, max_retries=retries)
    client.locked = locked
    return client


class VertexAi(AnthropicAi):
    capabilities = VERTEX_CAPABILITIES
    api_error = (anthropic.APIError, CredentialError)

    def __init__(self, client, name: str, models: dict[str, str]):
        super().__init__(client)
        self.name = name
        self.provider = provider_id(name)
        self.models = dict(models)

    def wire(self, ref: str) -> str:
        found = parse(ref)
        wired = self.models.get(found[1]) if found is not None and found[0] == self.name else None
        if wired is None:
            raise Refused(msg('vertex.model_unknown'))
        return wired

    def tools_of(self, tools: list[dict]) -> list[dict]:
        return [t for t in tools or [] if t.get('type') in OWN_TOOLS or t.get('type') == SEARCH_TYPE]

    def guarded(self, call: Callable[[], object]):
        try:
            return call()
        except anthropic.AnthropicError:
            raise
        except Exception as e:
            if type(e).__module__.startswith('google.'):
                raise CredentialError(kind(e)) from None
            raise

    def turn(self, model: str, effort: str, system: str, tools: list[dict], messages: list[dict], max_tokens: int, cache_ttl: str | None = None, thinking: str = THINK_AUTO,
             fallbacks: bool = False) -> Turn:
        wired = self.wire(model)
        offered = self.tools_of(tools)
        args = self.turn_args(wired, effort, system, offered, messages, max_tokens, cache_ttl, thinking)
        if not offered:
            args.pop('tools')
        return tracing.generation(self.provider, 'turn', model, system, messages, lambda: self.served(self.guarded(lambda: self.client.messages.create(**args)), model), tools)

    def once(self, model: str, system: str, messages: list[dict], max_tokens: int, effort: str, thinking: str = THINK_OFF, schema: dict | None = None, fallbacks: bool = False) -> Turn:
        args = self.once_args(self.wire(model), system, messages, max_tokens, effort, thinking, schema)
        if args['thinking'].get('type') == 'between_tools':
            args['thinking'] = {'type': 'adaptive'}
            args['max_tokens'] += THINKING_ROOM
        return tracing.generation(self.provider, 'once', model, system, messages, lambda: self.served(self.guarded(lambda: self.client.messages.create(**args)), model))

    def served(self, resp, ref: str) -> Turn:
        return dataclasses.replace(turn_of(resp, ref), model=ref)


def failure(error: BaseException, model: str, region: str, project: str) -> str:
    if any(isinstance(e, Refused) for e in chain(error)):
        return msg('vertex.test_refused')
    if isinstance(error, CredentialError) or type(error).__module__.startswith('google.'):
        return msg('vertex.test_credentials')
    if isinstance(error, anthropic.AuthenticationError):
        return msg('vertex.test_rejected')
    if isinstance(error, anthropic.PermissionDeniedError):
        return msg('vertex.test_forbidden', model=model, project=project)
    if isinstance(error, anthropic.NotFoundError):
        return msg('vertex.test_no_model', model=model, region=region)
    if isinstance(error, anthropic.RateLimitError):
        return msg('vertex.test_quota', region=region)
    if isinstance(error, anthropic.APITimeoutError):
        return msg('vertex.test_timeout')
    if isinstance(error, anthropic.APIConnectionError):
        return msg('vertex.test_unreachable')
    if isinstance(error, anthropic.APIStatusError):
        return msg('vertex.test_status', status=error.status_code)
    return msg('vertex.test_failed')


def chain(error: BaseException | None):
    seen = 0
    while error is not None and seen < 10:
        yield error
        error, seen = error.__cause__ or error.__context__, seen + 1


def stops_test(error: BaseException) -> bool:
    return isinstance(error, (CredentialError, anthropic.AuthenticationError, anthropic.APIConnectionError, anthropic.APITimeoutError)) or type(error).__module__.startswith('google.')


def ask(client, model: Model, text: str, extra: dict, tokens: int):
    wired = {'model': model.vertex_id, 'max_tokens': tokens, 'messages': [{'role': 'user', 'content': text}]} | extra
    try:
        return turn_of(client.messages.create(**wired), model.id), None
    except Exception as e:
        return None, e


def probe(client, conn: Connection) -> dict:
    models, first_error, tools, stopped = [], None, None, False
    for found in conn.models:
        if stopped:
            models.append({'id': found.id, 'ok': False, 'summary': msg('vertex.test_skipped')})
            continue
        got, error = ask(client, found, ANSWER_ASK, {}, PROBE_TOKENS)
        if error is None and not got.text:
            models.append({'id': found.id, 'ok': False, 'summary': msg('vertex.test_silent', model=found.id)})
            continue
        if error is not None:
            first_error = first_error or error
            models.append({'id': found.id, 'ok': False, 'summary': failure(error, found.id, conn.region, conn.project)})
            stopped = stops_test(error)
            continue
        models.append({'id': found.id, 'ok': True, 'summary': msg('vertex.test_answers')})
        if tools is None:
            call, error = ask(client, found, ECHO_ASK, {'tools': [ECHO], 'tool_choice': FORCED}, TOOL_TOKENS)
            tools = error is None and any(c.name == 'echo' for c in call.calls)
            if error is not None:
                first_error = first_error or error
                stopped = stops_test(error)
    done = bool(models) and all(m['ok'] for m in models) and bool(tools)
    if done:
        summary = msg('vertex.test_ok', count=len(models), region=conn.region)
    elif not models:
        summary = msg('vertex.test_no_model_set')
    else:
        summary = next((m['summary'] for m in models if not m['ok']), msg('vertex.test_no_tools'))
    out = {'ok': done, 'tools': bool(tools), 'summary': summary, 'models': models}
    return out | ({'error': first_error} if first_error is not None else {})


class Bound:
    def __init__(self):
        self.connections = None
        self.make: Callable[..., object] = client_for
        self.token_dir: Callable[[], str] = lambda: TOKEN_DIR
        self._built: dict[tuple[str, float, int], tuple[str, VertexAi]] = {}
        self._lock = threading.Lock()

    def get(self, name: str) -> Connection | None:
        return None if self.connections is None else self.connections.get(name)

    def all(self) -> list[Connection]:
        return [] if self.connections is None else self.connections.all()


BOUND = Bound()


def bind(connections, token_dir: Callable[[], str], make: Callable[..., object] | None = None) -> None:
    from skopax.investigator import pricing
    with BOUND._lock:
        BOUND.connections, BOUND.token_dir, BOUND.make, BOUND._built = connections, token_dir, make or client_for, {}
    pricing.VERTEX[:] = [vertex_models]


def unbind() -> None:
    from skopax.investigator import pricing
    with BOUND._lock:
        BOUND.connections, BOUND.token_dir, BOUND.make, BOUND._built = None, (lambda: TOKEN_DIR), client_for, {}
    pricing.VERTEX.clear()


def forget(connections, names: set[str]) -> None:
    with BOUND._lock:
        if BOUND.connections is connections:
            BOUND._built = {k: v for k, v in BOUND._built.items() if k[0] in names}


def vertex_models() -> dict[str, dict | None]:
    out: dict[str, dict | None] = {}
    for c in BOUND.all():
        for m in c.models:
            out[c.ref(m.id)] = None if m.input is None or m.output is None else {'input_tokens': m.input, 'output_tokens': m.output}
    return out


def usable(found: Connection | None, token_dir: str) -> bool:
    if found is None or not found.credentials_set:
        return False
    if found.mode == FEDERATION_MODE:
        source = (found.federation or {}).get('credential_source') or {}
        return token_path_ok(source.get('file'), token_dir)
    return True


def factory(provider: str, timeout: float = TIMEOUT_S, retries: int = RETRIES) -> VertexAi | None:
    found = BOUND.get(name_of(provider))
    token_dir = BOUND.token_dir()
    if not usable(found, token_dir):
        return None
    digest = hashlib.sha256('\n'.join((found.project, found.region, found.mode, found.key, json.dumps(found.federation, sort_keys=True), token_dir, str(timeout), str(retries))).encode()).hexdigest()[:16]
    with BOUND._lock:
        held = BOUND._built.get((found.name, timeout, retries))
        if held is not None and held[0] == digest:
            return held[1]
        try:
            client = BOUND.make(found, token_dir, timeout=timeout, retries=retries)
        except Exception as e:
            log.warning('the Vertex AI connection %s could not be built: %s', found.name, kind(e))
            return None
        made = VertexAi(client, found.name, {m.id: m.vertex_id for m in found.models})
        BOUND._built[(found.name, timeout, retries)] = (digest, made)
        return made


def capabilities(provider: str) -> frozenset[str]:
    return VERTEX_CAPABILITIES if BOUND.get(name_of(provider)) is not None else frozenset()


def models() -> tuple[str, ...]:
    return tuple(c.ref(m.id) for c in BOUND.all() for m in c.models)


def company(ref: str) -> str:
    found = parse(ref)
    connection = None if found is None else BOUND.get(found[0])
    return '' if connection is None else connection.company


def owns(name: str) -> bool:
    return BOUND.get(name) is not None


def register(ai_module) -> None:
    ai_module.register_provider(PREFIX, factory, capabilities, models, company, owns)
