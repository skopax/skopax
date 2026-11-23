import ipaddress
import logging
import math
import re
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from pathlib import Path
from urllib.parse import quote, urlsplit

from skopax import remote
from skopax.contract import Denied, HttpError, Hooks, Plugin, SourceDown, Transport, call
from skopax.contract import Setup as PluginSetup

ID = 'vault'
NAME = 'Vault'
ENV_ADDR = 'VAULT_ADDR'
ENV_NAMESPACE = 'VAULT_NAMESPACE'
ENV_CACERT = 'VAULT_CACERT'
ENV_AUTH = 'VAULT_AUTH'
ENV_TOKEN = 'VAULT_TOKEN'
ENV_TOKEN_FILE = 'VAULT_TOKEN_FILE'
ENV_K8S_ROLE = 'VAULT_K8S_ROLE'
ENV_K8S_TOKEN_FILE = 'VAULT_K8S_TOKEN_FILE'
ENV_K8S_MOUNT = 'VAULT_K8S_MOUNT'
ENV_ROLE_ID = 'VAULT_ROLE_ID'
ENV_SECRET_ID = 'VAULT_SECRET_ID'
ENV_SECRET_ID_FILE = 'VAULT_SECRET_ID_FILE'
ENV_APPROLE_MOUNT = 'VAULT_APPROLE_MOUNT'
ENV_KV_MOUNT = 'VAULT_KV_MOUNT'
ENV_PATH = 'VAULT_SKOPAX_PATH'
AUTH_TOKEN = 'token'
AUTH_K8S = 'kubernetes'
AUTH_APPROLE = 'approle'
AUTH_MODES = (AUTH_TOKEN, AUTH_K8S, AUTH_APPROLE)
K8S_JWT = '/var/run/secrets/kubernetes.io/serviceaccount/token'
DEFAULT_KV_MOUNT = 'secret'
DEFAULT_PATH = 'skopax'
DEFAULT_K8S_MOUNT = 'kubernetes'
DEFAULT_APPROLE_MOUNT = 'approle'
DEADLINE_S = 8.0
CALL_S = 4.0
CALL_MIN_S = 0.25
TTL_MARGIN_S = 60
LEASE_FLOOR_S = 30
WORKERS = 8
SAID_MAX = 200
NAME_SHOWN = 100
ANSWER_LIMIT = 8 * 1024 * 1024
FILE_MAX = 64 * 1024
SETTINGS_MAX = 500
INSTANCES_MAX = 200
FIELDS_MAX = 100
VALUE_MAX = 16 * 1024
SETTINGS = 'settings'
INSTANCES = 'instances'
IDENTITY = ('plugin', 'name')
MOUNT_SEGMENT = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]*')
PLUGIN_SEGMENT = re.compile(r'[a-z][a-z0-9-]*')
NAME_SEGMENT = re.compile(r'[a-z0-9][a-z0-9-]*')
log = logging.getLogger('skopax.plugins.vault')


class VaultError(SourceDown):
    pass


class BadSetup(VaultError):
    pass


class Unreachable(VaultError):
    pass


class TimedOut(Unreachable):
    pass


class BadAnswer(VaultError):
    pass


class TooMuch(BadAnswer):
    pass


class EmptyAnswer(BadAnswer):
    pass


class VaultDenied(Denied):
    pass


def transport_for(pem: str, unproxied: bool = False) -> Transport:
    if pem:
        return remote.pem_sender(pem, follow=False, limit=ANSWER_LIMIT, proxied=not unproxied)
    return remote.sender(follow=False, limit=ANSWER_LIMIT, proxied=not unproxied)


def shown(name: str) -> str:
    return repr(name[:NAME_SHOWN])


def file_text(path: str, variable: str) -> str:
    try:
        with Path(path).open('rb') as f:
            raw = f.read(FILE_MAX + 1)
        text = raw.decode('utf-8').strip()
    except (OSError, UnicodeError):
        raise BadSetup(f'{variable} names a file that cannot be read') from None
    if len(raw) > FILE_MAX:
        raise BadSetup(f'{variable} names a file over {FILE_MAX // 1024} KB')
    if not text:
        raise BadSetup(f'{variable} names an empty file')
    return text


def segments(raw: str, variable: str) -> str:
    parts = raw.strip().strip('/').split('/')
    if not all(MOUNT_SEGMENT.fullmatch(p) for p in parts):
        raise BadSetup(f'{variable} must be a path of letters, digits, dots, dashes and underscores')
    return '/'.join(parts)


def loopback(host: str) -> bool:
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def text_of(where: str, value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        text = 'true' if value else 'false'
    elif isinstance(value, (str, int, float)):
        text = str(value)
    else:
        raise BadAnswer(f'{where} is not a single value')
    if len(text.encode('utf-8')) > VALUE_MAX:
        raise TooMuch(f'{where} is over {VALUE_MAX // 1024} KB')
    return text


class Setup:
    def __init__(self, env: Mapping[str, str]):
        def get(name: str, default: str = '') -> str:
            return (env.get(name) or '').strip() or default

        self.addr = get(ENV_ADDR).rstrip('/')
        self.namespace = get(ENV_NAMESPACE)
        self.cacert = get(ENV_CACERT)
        self.auth = get(ENV_AUTH, AUTH_TOKEN).lower()
        self.token = get(ENV_TOKEN)
        self.token_file = get(ENV_TOKEN_FILE)
        self.k8s_role = get(ENV_K8S_ROLE)
        self.k8s_file = get(ENV_K8S_TOKEN_FILE, K8S_JWT)
        self.k8s_mount = get(ENV_K8S_MOUNT, DEFAULT_K8S_MOUNT)
        self.role_id = get(ENV_ROLE_ID)
        self.secret_id = get(ENV_SECRET_ID)
        self.secret_id_file = get(ENV_SECRET_ID_FILE)
        self.approle_mount = get(ENV_APPROLE_MOUNT, DEFAULT_APPROLE_MOUNT)
        self.kv_mount = get(ENV_KV_MOUNT, DEFAULT_KV_MOUNT)
        self.path = get(ENV_PATH, DEFAULT_PATH)
        self.where = ''
        self.plain = False

    def checked(self) -> 'Setup':
        try:
            parts = urlsplit(self.addr)
            host, _ = parts.hostname, parts.port
        except ValueError:
            raise BadSetup(f'{ENV_ADDR} is not an address') from None
        if parts.scheme not in ('https', 'http') or not host or parts.username is not None or parts.password is not None or parts.path or parts.query or parts.fragment:
            raise BadSetup(f'{ENV_ADDR} must be an https address with no credentials, path or query')
        if parts.scheme == 'http' and not loopback(host):
            raise BadSetup(f'{ENV_ADDR} must use https; plain http is allowed only for a loopback address')
        if self.auth not in AUTH_MODES:
            raise BadSetup(f'{ENV_AUTH} must be one of {", ".join(AUTH_MODES)}')
        if self.auth == AUTH_TOKEN and not (self.token or self.token_file):
            raise BadSetup('VAULT_TOKEN or VAULT_TOKEN_FILE is required for VAULT_AUTH token')
        if self.auth == AUTH_K8S and not self.k8s_role:
            raise BadSetup(f'{ENV_K8S_ROLE} is required for {ENV_AUTH} {AUTH_K8S}')
        if self.auth == AUTH_APPROLE and not (self.role_id and (self.secret_id or self.secret_id_file)):
            raise BadSetup('VAULT_ROLE_ID and VAULT_SECRET_ID or VAULT_SECRET_ID_FILE are required for VAULT_AUTH approle')
        self.kv_mount = segments(self.kv_mount, ENV_KV_MOUNT)
        self.path = segments(self.path, ENV_PATH)
        self.k8s_mount = segments(self.k8s_mount, ENV_K8S_MOUNT)
        self.approle_mount = segments(self.approle_mount, ENV_APPROLE_MOUNT)
        self.where = parts.netloc
        self.plain = parts.scheme == 'http'
        return self

    def identity(self) -> tuple:
        return (self.addr, self.namespace, self.cacert, self.auth, self.token, self.token_file, self.k8s_role, self.k8s_file, self.k8s_mount, self.role_id, self.secret_id,
                self.secret_id_file, self.approle_mount, self.kv_mount, self.path)

    def label(self) -> str:
        return f'{NAME} ({self.where})'

    def url(self, kind: str, rel: str) -> str:
        return f'{self.addr}/v1/{self.kv_mount}/{kind}/{self.path}/{quote(rel)}'

    def headers(self, token: str = '') -> dict[str, str]:
        return {'X-Vault-Request': 'true'} | ({'X-Vault-Namespace': self.namespace} if self.namespace else {}) | ({'X-Vault-Token': token} if token else {})


class Read:
    def __init__(self, owner: 'Vault', setup: Setup, transport: Transport):
        self.owner = owner
        self.setup = setup
        self.transport = transport
        self.deadline = owner.clock() + DEADLINE_S
        self.found = False
        self.lock = threading.Lock()
        self.token: str | None = None
        self.fresh = False
        self.relogged = False

    def timeout(self) -> float:
        left = (self.deadline - self.owner.clock()) / remote.PAGE_DEADLINE
        if left < CALL_MIN_S:
            raise TimedOut(f'{NAME} did not answer within {DEADLINE_S:g} s')
        return min(CALL_S, left)

    def send(self, method: str, url: str, headers: dict[str, str], body: object = None) -> object:
        try:
            return call(self.transport, method, url, headers, body=body, timeout=self.timeout(), limit=ANSWER_LIMIT).body
        except remote.TimedOut:
            raise TimedOut(f'{NAME} did not answer in time') from None
        except remote.TooLarge:
            raise TooMuch(f'{NAME} answered more than {ANSWER_LIMIT // (1024 * 1024)} MB') from None
        except remote.NotJson:
            raise BadAnswer(f'{NAME} did not answer with JSON') from None
        except remote.Unreachable:
            raise Unreachable(f'{NAME} could not be reached') from None
        except (RecursionError, ValueError):
            raise BadAnswer(f'{NAME} did not answer with readable JSON') from None

    def login(self) -> tuple[str, float]:
        s = self.setup
        if s.auth == AUTH_TOKEN:
            return (s.token or file_text(s.token_file, 'VAULT_TOKEN_FILE')), math.inf
        if s.auth == AUTH_K8S:
            mount, body = s.k8s_mount, {'role': s.k8s_role, 'jwt': file_text(s.k8s_file, 'VAULT_K8S_TOKEN_FILE')}
        else:
            mount, body = s.approle_mount, {'role_id': s.role_id, 'secret_id': s.secret_id or file_text(s.secret_id_file, 'VAULT_SECRET_ID_FILE')}
        try:
            answer = self.send('POST', f'{s.addr}/v1/auth/{mount}/login', s.headers(), body)
        except HttpError as e:
            if e.code in (400, 401, 403):
                raise VaultDenied(f'{NAME} refused the {s.auth} login (HTTP {e.code})', f'auth/{mount}', 'login') from None
            raise Unreachable(f'{NAME} answered HTTP {e.code} to the {s.auth} login') from None
        auth = answer.get('auth') if isinstance(answer, dict) else None
        token = auth.get('client_token') if isinstance(auth, dict) else None
        lease = auth.get('lease_duration') if isinstance(auth, dict) else None
        if not isinstance(token, str) or not token or isinstance(lease, bool) or not isinstance(lease, (int, float)):
            raise BadAnswer(f'{NAME} answered the {s.auth} login without a token')
        if not math.isfinite(lease) or lease < LEASE_FLOOR_S:
            return token, LEASE_FLOOR_S
        return token, max(LEASE_FLOOR_S, lease - min(TTL_MARGIN_S, lease / 2))

    def current(self) -> str:
        with self.lock:
            if self.token is None:
                cached = self.owner.cached()
                self.token, self.fresh = (cached, False) if cached else (self.owner.login(self), True)
            return self.token

    def again(self, used: str) -> bool:
        with self.lock:
            if self.token != used:
                return True
            if self.fresh or self.relogged or self.owner.denied_on(used):
                self.owner.held(used, self.setup.auth != AUTH_TOKEN)
                return False
            self.owner.drop()
            self.relogged = True
            self.token, self.fresh = self.owner.login(self), True
            return True

    def get(self, kind: str, rel: str, listing: bool = False) -> object:
        url = self.setup.url(kind, rel) + ('?list=true' if listing else '')
        verb = 'list' if listing else 'read'
        while True:
            token = self.current()
            try:
                return self.send('GET', url, self.setup.headers(token))
            except HttpError as e:
                if e.code == 404:
                    return None
                if e.code != 403:
                    raise Unreachable(f'{NAME} answered HTTP {e.code} to the {verb} of {rel}') from None
                if not self.again(token):
                    raise VaultDenied(f'{NAME} refused to {verb} {rel} (HTTP 403)', rel, verb) from None

    def data(self, rel: str) -> dict | None:
        answer = self.get('data', rel)
        if answer is None:
            return None
        outer = answer.get('data') if isinstance(answer, dict) else None
        inner = outer.get('data') if isinstance(outer, dict) else None
        if inner is None and isinstance(outer, dict):
            return None
        if not isinstance(inner, dict) or not all(isinstance(k, str) for k in inner):
            raise BadAnswer(f'{NAME} answered {rel} without a KV v2 secret')
        return inner

    def keys(self, rel: str) -> list[str]:
        answer = self.get('metadata', rel, listing=True)
        if answer is None:
            return []
        outer = answer.get('data') if isinstance(answer, dict) else None
        found = outer.get('keys') if isinstance(outer, dict) else None
        if not isinstance(found, list) or not all(isinstance(k, str) for k in found):
            raise BadAnswer(f'{NAME} answered the list of {rel} without keys')
        if len(found) > INSTANCES_MAX:
            raise TooMuch(f'{rel} lists more than {INSTANCES_MAX} keys')
        return found

    def settings(self) -> dict[str, str]:
        found = self.data(SETTINGS)
        self.found = self.found or found is not None
        found = found or {}
        if len(found) > SETTINGS_MAX:
            raise TooMuch(f'{SETTINGS} holds more than {SETTINGS_MAX} keys')
        out: dict[str, str] = {}
        for key, value in found.items():
            text = text_of(f'the value of {shown(key)} in {SETTINGS}', value)
            if text is not None:
                out[key] = text
        return out

    def instances(self) -> list[dict[str, str]]:
        named: list[tuple[str, str]] = []
        folders = self.keys(INSTANCES)
        self.found = self.found or bool(folders)
        for folder in folders:
            plugin = folder[:-1]
            if not folder.endswith('/') or not PLUGIN_SEGMENT.fullmatch(plugin):
                self.owner.warn(f'{NAME}: {INSTANCES}/{shown(folder)} is not a plug-in folder, so it is skipped')
                continue
            for name in self.keys(f'{INSTANCES}/{plugin}'):
                if not NAME_SEGMENT.fullmatch(name):
                    self.owner.warn(f'{NAME}: {INSTANCES}/{plugin}/{shown(name)} is not an instance name, so it is skipped')
                    continue
                named.append((plugin, name))
                if len(named) > INSTANCES_MAX:
                    raise TooMuch(f'{INSTANCES} holds more than {INSTANCES_MAX} instances')
        if not named:
            return []
        pool = ThreadPoolExecutor(max_workers=min(WORKERS, len(named)), thread_name_prefix='skopax-vault')
        futures = [pool.submit(self.instance, n) for n in named]
        done, _ = wait(futures, return_when=FIRST_EXCEPTION)
        failed = next((f for f in futures if f in done and f.exception() is not None), None)
        if failed is not None:
            pool.shutdown(wait=True, cancel_futures=True)
            raise failed.exception()
        pool.shutdown(wait=True)
        return [r for r in (f.result() for f in futures) if r is not None]

    def instance(self, named: tuple[str, str]) -> dict[str, str] | None:
        plugin, name = named
        rel = f'{INSTANCES}/{plugin}/{name}'
        found = self.data(rel)
        if found is None:
            return None
        if len(found) > FIELDS_MAX:
            raise TooMuch(f'{rel} holds more than {FIELDS_MAX} fields')
        fields: dict[str, str] = {}
        for key, value in found.items():
            text = text_of(f'the value of {shown(key)} in {rel}', value)
            if key not in IDENTITY and text is not None:
                fields[key] = text
        return {**fields, 'plugin': plugin, 'name': name}


class Vault:
    def __init__(self, transport_for: Callable[[str, bool], Transport] = transport_for, clock: Callable[[], float] = time.monotonic):
        self.transport_for = transport_for
        self.clock = clock
        self._identity: tuple | None = None
        self._token = ''
        self._expires = 0.0
        self._denied = ''
        self._transports: dict[tuple[str, bool], Transport] = {}
        self._said: set[str] = set()
        self._failing = ''
        self._lock = threading.Lock()

    def __call__(self, env: Mapping[str, str]) -> dict | None:
        if not (env.get(ENV_ADDR) or '').strip():
            return None
        with self._lock:
            try:
                setup = Setup(env).checked()
                if self._identity != setup.identity():
                    self._identity, self._token, self._expires, self._denied = setup.identity(), '', 0.0, ''
                read = Read(self, setup, self._transport(setup))
                found = {'label': setup.label(), 'values': read.settings(), 'instances': read.instances()}
                if not read.found:
                    raise EmptyAnswer(f'{NAME} has neither {SETTINGS} nor {INSTANCES} under VAULT_KV_MOUNT {setup.kv_mount} and VAULT_SKOPAX_PATH {setup.path}')
            except VaultError as e:
                if isinstance(e, (BadSetup, BadAnswer)):
                    self.warn(f'{NAME}: {e.text}')
                elif self._failing != type(e).__name__:
                    log.warning('%s: reading failed (%s); core keeps the last good data', NAME, type(e).__name__)
                self._failing = type(e).__name__
                raise
            except VaultDenied as e:
                if self._failing != type(e).__name__:
                    log.warning('%s: %s; core keeps the last good data', NAME, e.text)
                self._failing = type(e).__name__
                raise
            self._failing = ''
            return found

    def _transport(self, setup: Setup) -> Transport:
        pem = file_text(setup.cacert, ENV_CACERT) if setup.cacert else ''
        key = (pem, setup.plain)
        if key not in self._transports:
            try:
                self._transports[key] = self.transport_for(pem, setup.plain)
            except (ValueError, OSError):
                raise BadSetup(f'{ENV_CACERT} does not hold a PEM certificate') from None
        return self._transports[key]

    def cached(self) -> str:
        return self._token if self._token and self.clock() < self._expires else ''

    def login(self, read: Read) -> str:
        started = self.clock()
        token, ttl = read.login()
        self._token, self._expires, self._denied = token, started + ttl, ''
        return token

    def drop(self) -> None:
        self._token, self._expires, self._denied = '', 0.0, ''

    def held(self, token: str, sticky: bool) -> None:
        if sticky and token == self._token:
            self._denied = token
        elif not sticky:
            self.drop()

    def denied_on(self, token: str) -> bool:
        return bool(token) and token == self._denied

    def warn(self, text: str) -> None:
        if text not in self._said:
            if len(self._said) >= SAID_MAX:
                self._said.clear()
            self._said.add(text)
            log.warning('%s', text)


PLUGIN = Plugin(
    setup=PluginSetup(
        steps=(
            'This plug-in has no form and only reads from Vault. Set VAULT_ADDR to the Vault or OpenBao address and VAULT_AUTH to token, kubernetes or approle with its variables, then restart Skopax.',
            'Keep settings in <mount>/<path>/settings and one secret per instance in <mount>/<path>/instances/<plug-in>/<name>; the defaults are secret and skopax.',
        ),
        docs='https://developer.hashicorp.com/vault/docs/secrets/kv/kv-v2',
        docs_label='Vault KV secrets engine, version 2',
    ),
    id=ID, name=NAME, version='0.1.0', category='infrastructure', description='Settings and instances read from HashiCorp Vault or OpenBao (KV v2), set by environment variables.',
    icon='icon.svg', author='Skopax', licence='Apache-2.0', core='>=0.2,<0.6', edition='community',
    hooks=Hooks(settings_source=Vault()),
)
