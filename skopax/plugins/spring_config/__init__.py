import base64
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from urllib.parse import quote, urlsplit

from skopax.contract import HttpError, Plugin, Hooks, Transport, call, send
from skopax.contract import Setup as PluginSetup
from skopax import remote

ID = 'spring-config'
NAME = 'Spring Cloud Config'
ENV_URL = 'SPRING_CONFIG_URL'
ENV_APP = 'SPRING_CONFIG_APP'
ENV_PROFILE = 'SPRING_CONFIG_PROFILE'
ENV_LABEL = 'SPRING_CONFIG_LABEL'
ENV_USER = 'SPRING_CONFIG_USER'
ENV_PASSWORD = 'SPRING_CONFIG_PASSWORD'
ENV_MAP = 'SPRING_CONFIG_MAP'
DEFAULT_PROFILE = 'default'
TIMEOUT_S = 4
REFETCH_S = 15 * 60
RETRY_S = 5 * 60
ANSWER_LIMIT = 4 * 1024 * 1024
VALUE_MAX = 64 * 1024
STEPS_MAX = 2000
DEPTH_MAX = 20
SETTING_ID = re.compile(r'[a-z][a-z0-9-]*(\.[a-z0-9-]+)+')
SEGMENT = re.compile(r'[a-z][a-z0-9-]*')
NAME_SEGMENT = re.compile(r'[a-z0-9][a-z0-9-]*')
IDENTITY = ('plugin', 'name')
OLD_CONNECTION_KEYS = ('gitlab.token', 'gitlab.group', 'jira.url', 'jira.email', 'jira.api-token', 'jira.cloud-id', 'slack.bot-token', 'slack.app-token')
log = logging.getLogger('skopax.plugins.spring_config')


class SpringConfigError(Exception):
    def __init__(self, reason: str = ''):
        super().__init__(reason)
        self.reason = reason


class BadSetup(SpringConfigError):
    pass


class BadMap(BadSetup):
    pass


class Unauthorized(SpringConfigError):
    pass


class Refused(SpringConfigError):
    pass


class Unreachable(SpringConfigError):
    pass


class TimedOut(Unreachable):
    pass


class BadAnswer(SpringConfigError):
    pass


class EmptyAnswer(BadAnswer):
    pass


class Unresolved(Exception):
    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


def map_of(raw: str) -> list[tuple[str, tuple[str, ...]]]:
    out: list[tuple[str, tuple[str, ...]]] = []
    keys: set[str] = set()
    targets: set[tuple[str, ...]] = set()
    for part in (raw or '').split(','):
        entry = part.strip()
        if not entry:
            continue
        key, sep, target = (x.strip() for x in entry.partition('='))
        if not sep or not key or not target:
            raise BadMap(f'{ENV_MAP} entry {entry.split("=")[0].strip() or "(no key)"!r} is not <config key>=<target>')
        if target in OLD_CONNECTION_KEYS:
            raise BadMap(f'{ENV_MAP} key {key!r}: {target} is a connection key that is not read as a setting; map it to <plugin>/<instance>/<field>')
        if SETTING_ID.fullmatch(target):
            where: tuple[str, ...] = ('setting', target)
        else:
            pieces = target.split('/')
            if len(pieces) != 3 or not SEGMENT.fullmatch(pieces[0]) or not NAME_SEGMENT.fullmatch(pieces[1]) or not SEGMENT.fullmatch(pieces[2]):
                raise BadMap(f'{ENV_MAP} key {key!r}: the target {target!r} is not a setting id or <plugin>/<instance>/<field>')
            where = ('instance', *pieces)
        if where[0] == 'instance' and where[3] in IDENTITY:
            raise BadMap(f'{ENV_MAP} key {key!r}: the field {where[3]} names the instance and cannot be mapped')
        if key in keys:
            raise BadMap(f'{ENV_MAP} key {key!r} is mapped more than once')
        if where in targets:
            raise BadMap(f'{ENV_MAP} key {key!r}: the target {target!r} is already mapped')
        keys.add(key)
        targets.add(where)
        out.append((key, where))
    return out


def text_of(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (str, int, float)):
        return str(value)
    return None


def flatten(sources: object) -> dict[str, str]:
    out: dict[str, str] = {}
    for found in sources if isinstance(sources, list) else []:
        body = found.get('source') if isinstance(found, dict) else None
        if not isinstance(body, dict):
            continue
        for key, value in body.items():
            text = text_of(value)
            if isinstance(key, str) and key not in out and text is not None:
                out[key] = text
    return out


def closing(text: str, start: int) -> int:
    depth = 1
    i = start
    while i < len(text):
        if text.startswith('${', i):
            depth += 1
            i += 2
            continue
        if text[i] == '}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def split_body(body: str) -> tuple[str, str | None]:
    depth = 0
    i = 0
    while i < len(body):
        if body.startswith('${', i):
            depth += 1
            i += 2
            continue
        if body[i] == '}':
            depth -= 1
        elif body[i] == ':' and depth == 0:
            return body[:i], body[i + 1:]
        i += 1
    return body, None


def relaxed(key: str) -> str:
    return re.sub(r'[^A-Za-z0-9]', '_', key).upper()


def expand(text: str, config: Mapping[str, str], env: Mapping[str, str], stack: tuple[str, ...], budget: list[int]) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        found = text.find('${', i)
        end = closing(text, found + 2) if found >= 0 else -1
        if found < 0 or end < 0:
            out.append(text[i:])
            break
        out.append(text[i:found])
        key, default = split_body(text[found + 2:end])
        key = key.strip()
        budget[0] -= 1
        if budget[0] < 0 or len(stack) > DEPTH_MAX:
            raise Unresolved(key)
        if key in stack:
            raise Unresolved(key)
        if key in config:
            out.append(expand(config[key], config, env, stack + (key,), budget))
        elif key in env:
            out.append(env[key])
        elif relaxed(key) in env:
            out.append(env[relaxed(key)])
        elif default is not None:
            out.append(expand(default, config, env, stack, budget))
        else:
            raise Unresolved(key)
        if sum(len(x) for x in out) > VALUE_MAX:
            raise Unresolved(key)
        i = end + 1
    return ''.join(out)


def resolve_value(text: str, config: Mapping[str, str], env: Mapping[str, str]) -> str:
    return expand(text, config, env, (), [STEPS_MAX])


class Setup:
    def __init__(self, env: Mapping[str, str]):
        self.url = (env.get(ENV_URL) or '').strip().rstrip('/')
        self.app = (env.get(ENV_APP) or '').strip()
        self.profile = (env.get(ENV_PROFILE) or '').strip() or DEFAULT_PROFILE
        self.label = (env.get(ENV_LABEL) or '').strip()
        self.user = env.get(ENV_USER) or ''
        self.password = env.get(ENV_PASSWORD) or ''
        self.mapping = env.get(ENV_MAP) or ''

    def checked(self) -> 'Setup':
        parts = urlsplit(self.url)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username is not None or parts.password is not None or parts.query or parts.fragment:
            raise BadSetup(f'{ENV_URL} must be an http or https address without credentials or a query')
        if not self.app:
            raise BadSetup(f'{ENV_APP} is required')
        if bool(self.user) != bool(self.password):
            raise BadSetup(f'{ENV_USER} and {ENV_PASSWORD} go together')
        if not self.mapping.strip():
            raise BadSetup(f'{ENV_MAP} is required')
        return self

    def identity(self) -> tuple:
        return (self.url, self.app, self.profile, self.label, self.user, self.password, self.mapping)

    def address(self) -> str:
        segments = [self.app, self.profile] + ([self.label.replace('/', '(_)')] if self.label else [])
        return self.url + '/' + '/'.join(quote(s, safe=',()') for s in segments)

    def headers(self) -> dict[str, str]:
        if not self.user:
            return {}
        return {'Authorization': 'Basic ' + base64.b64encode(f'{self.user}:{self.password}'.encode()).decode()}


def fetch(setup: Setup, transport: Transport) -> list:
    try:
        body = call(transport, 'GET', setup.address(), setup.headers(), timeout=TIMEOUT_S, limit=ANSWER_LIMIT).body
    except HttpError as e:
        if e.code in (401, 403):
            raise Unauthorized(f'the server refused the credentials (HTTP {e.code})') from None
        raise Refused(f'the server answered HTTP {e.code}') from None
    except remote.TimedOut:
        raise TimedOut('the server did not answer in time') from None
    except remote.NotJson:
        raise BadAnswer('the server did not answer with JSON') from None
    except remote.Unreachable:
        raise Unreachable('the server could not be reached') from None
    sources = body.get('propertySources') if isinstance(body, dict) else None
    if not isinstance(sources, list):
        raise BadAnswer('the answer has no propertySources list')
    if not any(isinstance(s, dict) and isinstance(s.get('source'), dict) for s in sources):
        raise EmptyAnswer('the answer has no property source')
    return sources


def build(sources: list, mapping: list[tuple[str, tuple[str, ...]]], env: Mapping[str, str]) -> dict:
    config = flatten(sources)
    values: dict[str, str] = {}
    instances: dict[tuple[str, str], dict[str, str]] = {}
    for key, where in mapping:
        if key not in config:
            log.warning('%s: the key %s is not in the config server answer, so it is left out', NAME, key)
            continue
        try:
            value = resolve_value(config[key], config, env)
        except Unresolved:
            log.warning('%s: the key %s is left out because a placeholder in its value cannot be resolved', NAME, key)
            continue
        if where[0] == 'setting':
            values[where[1]] = value
        else:
            instances.setdefault((where[1], where[2]), {'plugin': where[1], 'name': where[2]})[where[3]] = value
    return {'values': values, 'instances': list(instances.values())}


class SpringConfig:
    def __init__(self, transport: Transport = send, clock: Callable[[], float] = time.monotonic):
        self.transport = transport
        self.clock = clock
        self.good: dict | None = None
        self._identity: tuple | None = None
        self._next = 0.0
        self._retry = 0.0
        self._error: type[SpringConfigError] | None = None
        self._said = ''
        self._lock = threading.Lock()

    def __call__(self, env: Mapping[str, str]) -> dict | None:
        if not (env.get(ENV_URL) or '').strip():
            return None
        try:
            setup = Setup(env).checked()
            mapping = map_of(setup.mapping)
        except SpringConfigError as e:
            if e.reason != self._said:
                self._said = e.reason
                log.warning('%s: %s', NAME, e.reason)
            raise
        with self._lock:
            now = self.clock()
            if self._identity != setup.identity():
                self._identity, self.good, self._next, self._retry, self._error = setup.identity(), None, 0.0, 0.0, None
            if self.good is not None and now < self._next:
                return self._copy()
            if self._error is not None and now < self._retry:
                raise self._error('the last read failed; the next try waits')
            try:
                found = build(fetch(setup, self.transport), mapping, env)
            except SpringConfigError as e:
                first = self._error is None
                self._error, self._retry = type(e), now + RETRY_S
                if first:
                    kept = 'keeping the last good copy' if self.good is not None else 'there is no earlier copy'
                    log.warning('%s: reading the config server failed (%s), %s', NAME, type(e).__name__, kept)
                raise
            self.good, self._next, self._error = found, now + REFETCH_S, None
            return self._copy()

    def _copy(self) -> dict:
        return {'values': dict(self.good['values']), 'instances': [dict(i) for i in self.good['instances']]}


PLUGIN = Plugin(
    setup=PluginSetup(
        steps=(
            'This plug-in has no form. Set SPRING_CONFIG_URL to the config server address and SPRING_CONFIG_APP to the application name; SPRING_CONFIG_PROFILE and SPRING_CONFIG_LABEL are optional.',
            'If the server asks for a sign-in, set SPRING_CONFIG_USER and SPRING_CONFIG_PASSWORD together. Set SPRING_CONFIG_MAP to the comma list of config key=Skopax target entries to read, then restart Skopax.',
        ),
        docs='https://docs.spring.io/spring-cloud-config/reference/server.html',
        docs_label='Spring Cloud Config Server',
    ),
    id=ID, name=NAME, version='0.1.0', category='infrastructure', description='Settings and instances read from a Spring Cloud Config server, set by environment variables.',
    icon='icon.svg', author='Skopax', licence='Apache-2.0', core='>=0.2,<0.6', edition='community',
    hooks=Hooks(settings_source=SpringConfig()),
)
