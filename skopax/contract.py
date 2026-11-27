import json
import re
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from pymongo.database import Database

from skopax.engine import PassResult, anomaly, fmt, iso
from skopax.errkind import Authored, InvalidInstance, NoAccess, SourceOwned
from skopax.remote import HttpError, Transport, Unreachable, call, send
from skopax.severity import LEVELS as SEVERITIES, from_label as alert_sev

__all__ = [
    'ID',
    'EDITIONS',
    'CATEGORIES',
    'FIELD_KINDS',
    'SETTING_KINDS',
    'TOOL_KINDS',
    'CHANNELS',
    'Capabilities',
    'Field',
    'SuggestContext',
    'Suggestions',
    'SEPARATORS',
    'SUGGEST_KINDS',
    'Setup',
    'InstanceInfo',
    'KEY_SEGMENT',
    'card_key',
    'Peer',
    'TestResult',
    'Digest',
    'Cleared',
    'CLEAR_OUTCOMES',
    'Sweep',
    'Check',
    'CardType',
    'AutoResolve',
    'RuleSet',
    'Setting',
    'Tool',
    'Knowledge',
    'DocRef',
    'DocText',
    'Corpus',
    'Truncated',
    'OrgPack',
    'Panel',
    'Locale',
    'Tag',
    'TagRule',
    'Redaction',
    'Migration',
    'InstanceWriter',
    'InvalidInstance',
    'SourceOwned',
    'Outbound',
    'ECHO_FIELD',
    'ROUTE_ECHO',
    'NODE_FIELD',
    'NODE_VIA',
    'QUESTIONS_FIELD',
    'QUESTIONS',
    'QUESTION_CORE_OWNERS',
    'ASKED_ACCESS',
    'AgentRead',
    'ReadError',
    'SourceDown',
    'Missing',
    'Refused',
    'Denied',
    'CodeHost',
    'SourceData',
    'Hooks',
    'Context',
    'RunState',
    'RUN_STATE_KEYS',
    'RUN_STATE_KEY_MAX',
    'RUN_STATE_VALUE_MAX',
    'KNOWLEDGE_INLINE_MAX',
    'KNOWLEDGE_INLINE_TOTAL',
    'KNOWLEDGE_OWNER_TOTAL',
    'AiClient',
    'Plugin',
    'PassResult',
    'anomaly',
    'SEVERITIES',
    'alert_sev',
    'fmt',
    'iso',
    'Transport',
    'HttpError',
    'Unreachable',
    'call',
    'send',
]

ID = re.compile(r'[a-z][a-z0-9-]{1,30}')
EDITIONS = ('community', 'enterprise')
CATEGORIES = ('metrics', 'logs', 'alerts', 'infrastructure', 'tickets', 'code', 'chat', 'web', 'apps', 'marketing', 'other')
FIELD_KINDS = ('text', 'url', 'secret', 'choice', 'bool', 'int', 'pem', 'regex', 'instance')
SETTING_KINDS = ('int', 'float', 'bool', 'text', 'choice', 'list')
TOOL_KINDS = ('read', 'write', 'propose')
CHANNELS = ('page', 'slack', 'mcp')
KEY_SEGMENT = '@'
NODE_FIELD = 'via-node'
SEPARATORS = ('', ',', ';')
SUGGEST_KINDS = ('text', 'url')
RUN_STATE_KEYS = 500
CLEAR_OUTCOMES = ('fixed', 'handled')
RUN_STATE_KEY_MAX = 200
RUN_STATE_VALUE_MAX = 64 * 1024
KNOWLEDGE_INLINE_MAX = 2000
KNOWLEDGE_INLINE_TOTAL = 6000
KNOWLEDGE_OWNER_TOTAL = 16000


@dataclass(frozen=True)
class Capabilities:
    hosts: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()
    agent_reads: bool = False


@dataclass(frozen=True)
class Setup:
    steps: tuple[str, ...] = ()
    docs: str = ''
    docs_label: str = ''


@dataclass(frozen=True)
class SuggestContext:
    values: Mapping[str, str] = field(repr=False)
    transport: Transport = field(repr=False)
    deadline: float = 0.0


@dataclass(frozen=True)
class Suggestions:
    values: tuple[str, ...] = ()
    note: str = ''


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str
    required: bool = False
    default: str = ''
    help: str = ''
    choices: tuple[str, ...] = ()
    low: int | None = None
    high: int | None = None
    show_if: tuple[str, str] | None = None
    ui: bool = False
    address: str = ''
    hosts_of: Callable[[str, Mapping[str, str]], list[str]] | None = None
    choice_labels: Mapping[str, str] = field(default_factory=dict)
    group: str = ''
    check: Callable[[str], str | None] | None = None
    suggest: Callable[[SuggestContext], 'list[str] | Suggestions'] | None = None
    separator: str = ''


@dataclass(frozen=True)
class InstanceInfo:
    name: str
    primary: bool
    segment: str | None
    config: Mapping[str, str] = field(repr=False)
    off_checks: frozenset[str] = frozenset()


def card_key(raised: str, instance: InstanceInfo | None) -> str:
    return raised if instance is None or instance.segment is None else f'{raised}{KEY_SEGMENT}{instance.segment}'


@dataclass(frozen=True)
class Peer:
    info: InstanceInfo
    http: Callable[[], Transport]
    off_checks: frozenset[str] = frozenset()


@dataclass(frozen=True)
class TestResult:
    __test__ = False

    ok: bool
    summary: str
    ms: int


@dataclass(frozen=True)
class Digest:
    title: str
    lines: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Cleared:
    note: str = ''
    outcome: str = ''


@dataclass
class Sweep:
    result: PassResult
    sections: dict[str, list[dict]] = field(default_factory=dict)
    deploys: list[dict] = field(default_factory=list)
    shots: list[dict] = field(default_factory=list)
    posts: list[dict] = field(default_factory=list)
    memos: dict[str, dict] = field(default_factory=dict)
    digest: Digest | None = None
    cleared: dict[str, Cleared] = field(default_factory=dict)


@dataclass(frozen=True)
class Check:
    id: str
    label: str
    period: int
    offset: int
    ran: tuple[str, ...]
    run: Callable[['Context'], Sweep]
    tool: str | None = None
    legacy_alias: bool = False
    sections: tuple[str, ...] = ()
    section_help: Mapping[str, str] = field(default_factory=dict)
    once: bool = False
    timeout: int | None = None
    background: bool = False
    own_findings: bool = False
    daily: bool = False


@dataclass(frozen=True)
class CardType:
    sig: str
    label: str
    alias: str = ''


@dataclass(frozen=True)
class AutoResolve:
    check: str
    sig: str
    hours: int
    setting: str = ''


@dataclass(frozen=True)
class RuleSet:
    id: str
    label: str
    defaults: Mapping[str, float]
    help: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    help: str
    kind: str
    default: str
    low: float | None = None
    high: float | None = None
    choices: tuple[str, ...] = ()
    live: bool = True


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    run: Callable[['Context', dict], object]
    kind: str = 'read'
    instance_input: str | None = 'instance'
    per_run: int | None = None
    reach: tuple[str, ...] = ()
    scan: bool = True
    confirm: Callable[['Context', dict], dict] | None = None
    needs_field: str = ''
    image: bool = False
    available: Callable[[object, object], bool] | None = None


@dataclass(frozen=True)
class Knowledge:
    id: str
    title: str
    text: str
    slack: bool = False
    inline: bool = False


class Truncated(Exception):
    pass


@dataclass(frozen=True)
class DocRef:
    id: str
    title: str
    url: str | None
    modified: str | None
    space: str = ''


@dataclass(frozen=True)
class DocText:
    title: str
    url: str | None
    text: str


@dataclass(frozen=True)
class Corpus:
    list: Callable[['Context', str | None], Iterator[DocRef]]
    read: Callable[['Context', str], DocText]
    max_pages: int
    spaces: Callable[[Mapping[str, str]], tuple[str, ...] | None] | None = None


@dataclass(frozen=True)
class OrgPack:
    process: str = ''
    gate: str = ''
    small_talk: str = ''
    slack_sections: tuple[str, ...] = ()
    plugin_sections: bool = True


@dataclass(frozen=True)
class Panel:
    tab: str
    script: str | None = None
    fragment: str | None = None
    label: str = ''
    group: str = 'observe'


@dataclass(frozen=True)
class Locale:
    lang: str
    page: Mapping[str, str] = field(default_factory=dict)
    messages: Mapping[str, str] = field(default_factory=dict)
    rules: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    months: Mapping[str, str] = field(default_factory=dict)
    tails: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    headers: Mapping[str, str] = field(default_factory=dict)
    writing: str = ''
    days: Mapping[str, str] = field(default_factory=dict)
    chat: Mapping[str, object] = field(default_factory=dict)
    chainlit: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Tag:
    id: str
    label: str


@dataclass(frozen=True)
class TagRule:
    tag: str
    terms: tuple[str, ...]


@dataclass(frozen=True)
class Redaction:
    id: str
    pattern: str


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    run: Callable[[Database], None] | Callable[[Database, 'InstanceWriter'], None]
    needs_instances: bool = False
    required: bool = False


class InstanceWriter(Protocol):
    def adopt_sealed(self, plugin: str, name: str, field: str, sealed: str, by: str) -> None: ...

    def put(self, plugin: str, name: str, values: Mapping[str, str], secrets: Mapping[str, str], by: str, primary: bool = False, sealed: Mapping[str, str] | None = None,
            enabled: bool = True, off_checks: Iterable[str] = ()) -> None: ...

    def get(self, plugin: str, name: str) -> Mapping[str, str] | None: ...


@dataclass(frozen=True)
class Outbound:
    post: Callable[['Context', dict], dict]
    tags_field: str = 'tags'
    mentions_field: str | None = 'mentions'
    link_field: str | None = None
    keys_field: str | None = None
    sev_field: str | None = None
    target_field: str | None = None
    ready: Callable[['Context'], str | None] | None = None


ECHO_FIELD = 'slack-echo'
ROUTE_ECHO = Field(ECHO_FIELD, 'Also post to Slack', 'choice', choices=('default', 'on', 'off'),
                   choice_labels={'default': 'Use the default', 'on': 'On', 'off': 'Off'},
                   help='On posts a card to Slack too when this route sends it, if Slack has not posted it yet, and says in its Slack thread which channel got it; Off never does; Use the default (or not set) follows the install-wide Slack echo setting.')
QUESTIONS_FIELD = 'questions'
QUESTIONS = ('everyone', 'operators', 'none')
QUESTION_CORE_OWNERS = ('jira',)
ASKED_ACCESS = Field(QUESTIONS_FIELD, 'Available in questions', 'choice', default='everyone', choices=QUESTIONS,
                     choice_labels={'everyone': "Card runs + everyone's questions", 'operators': 'Card runs + operator and admin questions', 'none': 'Card runs only'}, group='advanced',
                     help='Card runs always read this connection. This picks whose questions (Slack asks, chat messages, page asks) may read it too.')
NODE_VIA = Field(NODE_FIELD, 'Through agent node', 'instance', choices=('kubernetes',), group='advanced', help='Name of a Kubernetes agent instance that reaches this service. Empty means Skopax connects directly.')


@dataclass(frozen=True)
class AgentRead:
    op: str
    args: tuple[str, ...]
    keys: tuple[str, ...]
    sources: Callable[[Mapping[str, str]], list[str]]
    run: Callable[[Mapping[str, str], dict], object]
    fields: tuple[Field, ...] = ()
    timeout: int = 75
    node_keys: Mapping[str, str] | Callable[[Mapping[str, str]], Mapping[str, str]] | None = None


class ReadError(Exception):
    def __init__(self, status: int, message: str, room: int | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.room = room


class Planted(ReadError):
    def __init__(self, phrases: tuple[str, ...]):
        super().__init__(422, f"held: the text sent to the model reads like instructions to it ({', '.join(phrases)})")
        self.phrases = tuple(phrases)


class SourceDown(Authored, Exception):
    pass


class Missing(Exception):
    pass


class Refused(Authored, Exception):
    pass


class Denied(SourceDown, NoAccess):
    def __init__(self, text: str, part: str = '', permission: str = ''):
        super().__init__(text)
        self.part = part
        self.permission = permission


def _scalar(where: str, value: object) -> str:
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (str, int, float)):
        return str(value)
    raise ValueError(f'{where} must be a scalar')


def _strings(where: str, raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError(f'{where} must be a dict')
    if any(not isinstance(k, str) for k in raw):
        raise ValueError(f'{where} keys must be strings')
    return {k: _scalar(f'{where}.{k}', v) for k, v in raw.items()}


@dataclass(frozen=True)
class SourceData:
    label: str
    values: Mapping[str, str] = field(default_factory=dict, repr=False)
    instances: tuple[Mapping[str, str], ...] = field(default=(), repr=False)
    left_out: tuple[str, ...] = ()
    left_out_more: int = 0

    @classmethod
    def of(cls, raw: dict, default_label: str) -> 'SourceData':
        if not isinstance(raw, dict):
            raise ValueError('a settings source must return a dict')
        label = default_label if raw.get('label') in (None, '') else raw['label']
        if not isinstance(label, str):
            raise ValueError('label must be a string')
        values = _strings('values', {} if raw.get('values') is None else raw['values'])
        listed = () if raw.get('instances') is None else raw['instances']
        if not isinstance(listed, (list, tuple)):
            raise ValueError('instances must be a list')
        left = () if raw.get('leftOut') is None else raw['leftOut']
        more = 0 if raw.get('leftOutMore') is None else raw['leftOutMore']
        if not isinstance(left, (list, tuple)) or not all(isinstance(k, str) for k in left):
            raise ValueError('leftOut must be a list of strings')
        if isinstance(more, bool) or not isinstance(more, int) or more < 0:
            raise ValueError('leftOutMore must be a count')
        return cls(label, values, tuple(_strings(f'instances[{i}]', item) for i, item in enumerate(listed)), tuple(left), more)


@dataclass(frozen=True)
class Hooks:
    role_resolver: Callable[[dict, str], str | None] | None = None
    settings_source: Callable[..., dict] | None = None
    oidc_token_request: Callable[[dict], dict] | None = None
    org_validators: Callable[[], dict] | None = None
    entitlement: Callable[[], str] | None = None


class RunState:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._held: dict[str, object] = {}

    @staticmethod
    def _key(key: object) -> str:
        if not isinstance(key, str) or not key or len(key) > RUN_STATE_KEY_MAX:
            raise ValueError(f'a run state key is a text of 1 to {RUN_STATE_KEY_MAX} characters')
        return key

    def _keep(self, key: str, value: object) -> None:
        self._held.pop(key, None)
        self._held[key] = value
        while len(self._held) > RUN_STATE_KEYS:
            del self._held[next(iter(self._held))]

    def get(self, key: str, default: object = None) -> object:
        with self._guard:
            return self._held.get(self._key(key), default)

    def put(self, key: str, value: object) -> None:
        key = self._key(key)
        try:
            written = json.dumps(value, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError(f'a run state value is JSON data (dicts with text keys, lists, texts, numbers, true, false, null); {key} is not') from None
        if len(written) > RUN_STATE_VALUE_MAX:
            raise ValueError(f'a run state value is at most 64 KiB once written as JSON; {key} is larger')
        with self._guard:
            self._keep(key, json.loads(written))

    def count(self, key: str, by: int = 1) -> int:
        key = self._key(key)
        if not isinstance(by, int) or isinstance(by, bool):
            raise ValueError('a run state count goes up by a whole number')
        with self._guard:
            had = self._held.get(key, 0)
            if not isinstance(had, int) or isinstance(had, bool):
                raise ValueError(f'the run state key {key} holds something other than a count')
            if by:
                self._keep(key, had + by)
            return had + by

    def __len__(self) -> int:
        with self._guard:
            return len(self._held)


class AiClient(Protocol):
    def complete(self, system: str, messages: list[dict], max_tokens: int, schema: dict | None = None, effort: str = 'medium') -> dict | None: ...


class Context(Protocol):
    now: float
    instance: InstanceInfo | None
    run_state: RunState

    def http(self) -> Transport: ...

    def rules(self, set_id: str) -> dict[str, float]: ...

    def setting(self, key: str) -> object: ...

    def agent_read(self, agent: str, op: str, args: dict) -> object: ...

    def node_read(self, op: str, args: dict) -> object: ...

    def peer(self, field: str) -> Peer | None: ...

    def peers(self, plugin_id: str) -> list[Peer]: ...

    def users(self) -> list[InstanceInfo]: ...

    def previous(self, section: str) -> list | None: ...

    def deploys(self, since: float) -> list[dict]: ...

    def last_status(self) -> str | None: ...

    def open_cards(self, plugin_wide: bool = False) -> list[dict]: ...

    def client(self) -> object | None: ...

    def ai(self, model: str) -> AiClient | None: ...

    def memo(self, key: str) -> dict | None: ...


class CodeHost(Protocol):
    @property
    def enabled(self) -> bool: ...

    def repo(self, name_or_path: str) -> dict | None: ...

    def similar(self, name: str, limit: int = 5) -> list[str]: ...

    def commit(self, repo: dict | str, ref: str) -> dict: ...

    def commits(self, repo: dict | str, old: str, new: str) -> list[dict]: ...

    def commits_since(self, repo: dict | str, ref: str, since: str) -> list[dict]: ...

    def file(self, repo: dict | str, path: str, ref: str) -> str: ...

    def blame(self, repo: dict | str, path: str, ref: str, first: int, last: int) -> list[dict]: ...

    def commit_url(self, repo: dict, sha: str) -> str: ...

    def compare_url(self, repo: dict, old: str, new: str) -> str: ...

    def file_url(self, repo: dict, sha: str, path: str, first: int, last: int) -> str: ...

    def link_problem(self, url: str) -> str | None: ...


@dataclass(frozen=True)
class Plugin:
    id: str
    name: str
    version: str
    category: str
    description: str
    icon: str
    author: str
    licence: str
    core: str
    edition: str = 'community'
    capabilities: Capabilities = field(default_factory=Capabilities)
    after: tuple[str, ...] = ()
    uses: tuple[str, ...] = ()
    client: Callable[[InstanceInfo, Transport], object] | None = None
    max_instances: int | None = None
    fields: tuple[Field, ...] = ()
    env_instances: Callable[[Mapping[str, str]], list[dict]] | None = None
    test: Callable[[Context], TestResult] | None = None
    checks: tuple[Check, ...] = ()
    rules: tuple[RuleSet, ...] = ()
    settings: tuple[Setting, ...] = ()
    tools: tuple[Tool, ...] = ()
    links: Callable[[dict, InstanceInfo], list[dict] | None] | None = None
    row_links: Callable[[dict, dict, InstanceInfo], dict[str, str]] | None = None
    probe_targets: Callable[[InstanceInfo], list[dict]] | None = None
    outbound: Outbound | None = None
    releases: Callable[[list[dict], float], list[dict]] | None = None
    deploy_report: Callable[[Context, list[dict], float], dict | None] | None = None
    tags: tuple[Tag, ...] = ()
    tag_rules: tuple[TagRule, ...] = ()
    panels: tuple[Panel, ...] = ()
    knowledge: tuple[Knowledge, ...] = ()
    org_pack: OrgPack | None = None
    locales: tuple[Locale, ...] = ()
    hooks: Hooks = field(default_factory=Hooks)
    migrations: tuple[Migration, ...] = ()
    agent_reads: tuple[AgentRead, ...] = ()
    redactions: tuple[Redaction, ...] = ()
    card_types: tuple[CardType, ...] = ()
    card_text: Callable[[dict], str | None] | None = None
    auto_resolve: tuple[AutoResolve, ...] = ()
    setup: Setup | None = None
    company_knowledge: str = ''
    knowledge_also: bool = False
    corpus: Corpus | None = None
    test_posts: bool = False
