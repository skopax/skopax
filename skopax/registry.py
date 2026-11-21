import importlib
import importlib.metadata
import json
import logging
import re
import string
import sys
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import regex
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

import skopax
from skopax.bounded import HOOK_TIMEOUT_S, Bound
from skopax.chattext import STARTERS_MAX as CHAT_STARTERS_MAX, TEXT_MAX as CHAT_TEXT_MAX, chat_kept, chat_problem, tree_problem
from skopax.contract import CATEGORIES, ECHO_FIELD, ROUTE_ECHO, EDITIONS, FIELD_KINDS, ID, SEPARATORS, SUGGEST_KINDS, KNOWLEDGE_INLINE_MAX, TOOL_KINDS, CardType, Check, Knowledge, Locale, OrgPack, Outbound, Panel, Plugin, Redaction, Setup, Tag, TagRule, Tool
from skopax.errkind import Authored, kind as exc_kind
from skopax.messages import LANG_TAG, msg, refusal as message_refusal

log = logging.getLogger(__name__)

GROUP = 'skopax.plugins'
HOSTS_ADDRESS = 'hosts'
ALONE_ADDRESS = 'alone'
FIELD_ADDRESS = {HOSTS_ADDRESS: 'text', ALONE_ADDRESS: 'url'}
ENV = 'SKOPAX_PLUGINS'
ATTR = 'PLUGIN'
BUILTIN: tuple[str, ...] = (
    'skopax.plugins.gitlab',
    'skopax.plugins.github',
    'skopax.plugins.jira',
    'skopax.plugins.slack',
    'skopax.plugins.prometheus',
    'skopax.plugins.kubernetes',
    'skopax.plugins.teams',
    'skopax.plugins.confluence',
    'skopax.plugins.argocd',
    'skopax.plugins.capacity',
    'skopax.plugins.grafana',
    'skopax.plugins.kafka',
    'skopax.plugins.redis',
    'skopax.plugins.airbyte',
    'skopax.plugins.langfuse',
    'skopax.plugins.pmm',
    'skopax.plugins.cloudflare',
    'skopax.plugins.elasticsearch',
    'skopax.plugins.csp',
    'skopax.plugins.varnish',
    'skopax.plugins.website',
    'skopax.plugins.browser',
    'skopax.plugins.umami',
    'skopax.plugins.crux',
    'skopax.plugins.safe_browsing',
    'skopax.plugins.app_stores',
    'skopax.plugins.crashlytics',
    'skopax.plugins.ga4',
    'skopax.plugins.gtm',
    'skopax.plugins.google_ads',
    'skopax.plugins.vwo',
    'skopax.plugins.mcp_tools',
    'skopax.plugins.spring_config',
    'skopax.plugins.sonarqube',
    'skopax.plugins.sharepoint',
)
CORE_PREFIXES = (
    'core',
    'general',
    'access',
    'sso',
    'investigate',
    'chat',
    'page',
    'mcp',
    'cards',
    'recurring',
    'history',
    'screenshot',
    'self-review',
    'tags',
    'routes',
    'mongo',
    'anthropic',
    'openai',
    'agent',
    'small-talk',
)
CORE_RAN = ('manual', 'slack-posts', 'selfreview', 'certs', 'jira-links', 'custom-checks')
BRIDGE_PREFIX = 'mcp_'
CORE_STATE = frozenset({'anomalies', 'history', 'muted', 'retired', 'warned', 'summary', 'sources', 'serverPushes', 'serverPasses', 'initialized', 'conflict', 'rev', 'updatedAt', 'archiveHours', '_id'})
TOOL_NAME = re.compile(r'[a-zA-Z0-9_-]{1,64}')
KNOWLEDGE_ID = re.compile(r'[a-z][a-z0-9-]{0,39}')
TITLE_MAX = 80
NAME_MAX = 60
SECTION_MAX = 8000
CHECK_TIMEOUT_MAX = 3600
SCHEMA_KEYS = frozenset({'type', 'description', 'enum', 'minimum', 'maximum', 'minLength', 'maxLength', 'items', 'properties', 'required', 'additionalProperties', 'minItems', 'maxItems'})
SCHEMA_TYPES = frozenset({'string', 'integer', 'number', 'boolean', 'array', 'object', 'null'})
SCHEMA_DEPTH = 6
NUMBER_TYPES = frozenset({'integer', 'number'})
RESERVED_KEYS = frozenset({'name', 'primary', 'enabled', 'plugin'})
INSTANCE_WORDS = ('primary', 'all', 'no-data')
FIELD_KEY = re.compile(r'[a-z][a-z0-9-]{0,30}')
FIELD_GROUP = re.compile(r'[a-z][a-z0-9-]{0,23}')
CHOICE_LABEL_MAX = 40
SECTION_NAME = re.compile(r'[a-z][a-zA-Z0-9-]{0,30}')
AGENT_FIELD_KEY = re.compile(r'[a-z][a-z0-9.-]{0,60}')
AGENT_HOST_FIELDS = ('mode', 'url', 'token', 'via')
VERSION = re.compile(r'\d+\.\d+\.\d+')
ICON_MAX_BYTES = 20 * 1024
PATTERN_MAX = 500
TAG_ID = re.compile(r'[a-z0-9-]{1,30}')
LANG = LANG_TAG
WRITING_MAX = 1000
RULES_MAX = 200
RULE_PATTERN_MAX = 300
RULE_REPLACEMENT_MAX = 500
TAILS_MAX = 50
HEADERS_MAX = 200
MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
DAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
LOCALE_PARTS = ('page', 'messages', 'writing', 'rules', 'tails', 'months', 'days', 'headers', 'chat', 'chainlit')
LOCALE_ENTRIES = ('page', 'messages', 'rules', 'tails', 'months', 'days', 'headers')
LOCALE_LISTS = ('rules', 'tails')
LOCALE_LIMITS = {'rules': RULES_MAX, 'tails': TAILS_MAX, 'headers': HEADERS_MAX}
LOCALE_NAMES = {'months': MONTHS, 'days': DAYS}
LOOSE_DOLLAR = re.compile(r'\$(?![1-9])')
SHOWN_MAX = 60
PANEL_TAB = re.compile(r'[a-z][a-z0-9-]{1,30}')
CORE_TABS = ('anoms', 'reqs', 'chat', 'inv', 'svcs', 'info', 'lat', 'rels', 'reps', 'slack', 'hist', 'acts', 'costs', 'tools', 'mcp', 'review', 'docs', 'prefs', 'settings', 'access', 'workspaces', 'integrations', 'features', 'ai', 'changes')
NAV_GROUPS = ('main', 'observe', 'activity', 'connect', 'more')
RESERVED_TABS = ('sub-anoms', 'fold-anoms')
PAGE_LABEL_MAX = 24
STANDING = {'broken': 'failed'}
ORIGIN_WORDS = {'builtin': 'plugins.why.origin_builtin', 'entry-point': 'plugins.why.origin_package', 'env': 'plugins.why.origin_env'}
PROCESS_MAX = 200_000
PACK_TEXT_MAX = 40_000
PACK_FIELDS = ('product', 'org')
SECTION_ANCHOR = re.compile(r'^## [^\n]*?\{#([a-z-]+)\}[ \t]*$', re.M)
HELP_MAX = 160
SETUP_STEPS_MAX = 10
SETUP_STEP_MAX = 400
SETUP_DOCS_MAX = 500
SETUP_LABEL_MAX = 60
DROPS_MAX = 50
SEVERITIES = ('info', 'warn', 'crit')
CARD_LABEL_MAX = 40
CORE_SIGS = frozenset((
    'page-typos', 'page-layout', 'log', 'alert', 'err', '5xx', 'avg-latency', 'apm-p95', 'apm-fail', 'pvc-disk', 'db-disk', 'hpa-max', 'jvm-heap', 'varnish-cache', 'varnish-hit',
    'kafka-consume-err', 'kafka-lag', 'airbyte-long-run', 'airbyte-failed', 'restarts', 'waiting', 'no-ready-pods', 'unavailable', 'argo', 'integrity-fail', 'cf-status', 'csp',
    'node-not-ready', 'cert-expiry', 'ad-link', 'ad-link-moved', 'sonar-gate', 'cve-deps', 'cve-os', 'cve-scan', 'no-access', 'varnish-fields', 'varnish-cache-fields',
    'logflow-fields', 'csp-fields', 'logs-stopped', 'logs-lag', 'lf-errors', 'lf-traffic', 'lf-latency', 'lf-turn-cost', 'lf-day-cost', 'cf-health', 'cf-cert', 'cf-incident',
    'edge-conn', 'edge-5xx', 'kafka-broker', 'kafka-exporter', 'kafka-offline', 'kafka-urp', 'kafka-disk', 'kafka-restart', 'kafka-gone', 'kafka-send-err', 'redis-down',
    'redis-master', 'redis-repl', 'redis-evict', 'redis-reject', 'redis-app', 'redis-app-err', 'qan-load', 'qan-slow', 'qan-stall', 'qan-calls', 'qan-total', 'pg-down',
    'pg-saturation', 'pg-conn-jump', 'pg-idle-in-tx', 'pg-long-tx', 'pg-repl-lag', 'pg-lock-waits', 'pg-deadlocks', 'mongo-down', 'mongo-state', 'mongo-repl-lag',
    'mongo-saturation', 'mongo-conn-jump', 'mongo-queue', 'mongo-latency', 'mongo-no-primary', 'mongo-election', 'traffic-drop', 'traffic-spike', 'node-pressure',
    'umami-redirect', 'umami-pageviews', 'crux', 'safebrowsing', 'ga-drop', 'gtm-publish', 'ad-disapproved', 'browser-down', 'store-rating', 'play-vitals', 'crash-rate',
    'crash-new', 'ab-test', 'ab-verdict', 'access', 'umami-access', 'other',
))


@dataclass(frozen=True)
class Drop:
    plugin: str
    lang: str
    part: str
    key: str
    reason: str


@dataclass
class Loaded:
    id: str
    module: str
    origin: str
    plugin: Plugin | None
    state: str
    reason: str | None
    directory: Path | None
    drops: tuple[Drop, ...] = ()
    dropped: tuple[tuple[str, int], ...] = ()
    dist: tuple[str, str] | None = None


class _Reject(Authored, Exception):
    pass


def _why(code: str, /, **values) -> _Reject:
    return _Reject(msg('plugins.why.' + code, **values))


def _read_icon(directory: Path | None, icon: object) -> str | None:
    if not isinstance(icon, str) or not icon or directory is None:
        return None
    try:
        root = directory.resolve()
        path = (root / icon).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > ICON_MAX_BYTES:
            return None
        text = path.read_text(encoding='utf-8')
    except (OSError, UnicodeError, ValueError):
        return None
    head = text.lstrip()
    return text if head.startswith(('<svg', '<?xml')) and '<svg' in head else None


def _home(plugin: object) -> tuple[str | None, Path | None]:
    if not isinstance(plugin, Plugin):
        return None, None
    hooks = plugin.hooks
    calls = [c.run for c in plugin.checks] + [t.run for t in plugin.tools] + [plugin.test, plugin.env_instances, plugin.links, plugin.row_links, plugin.probe_targets, plugin.releases, plugin.deploy_report,
                                                                             hooks.role_resolver, hooks.settings_source, hooks.oidc_token_request, hooks.org_validators, hooks.entitlement]
    for call in calls:
        module = sys.modules.get(getattr(call, '__module__', None) or '')
        if module is not None and getattr(module, '__file__', None):
            return module.__name__, Path(module.__file__).parent
    return None, None


def _texts(value: object) -> bool:
    return isinstance(value, (tuple, list)) and all(isinstance(v, str) for v in value)


def _text_entry(key: object, value: object) -> bool:
    return isinstance(key, str) and isinstance(value, str)


def _text_map(value: object) -> bool:
    return isinstance(value, Mapping) and all(_text_entry(k, v) for k, v in value.items())


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _text_pair(pair: object) -> bool:
    return isinstance(pair, (tuple, list)) and len(pair) == 2 and _texts(pair) and bool(pair[0])


def _text_pairs(value: object) -> bool:
    return isinstance(value, (tuple, list)) and all(_text_pair(p) for p in value)


def _compiles(pattern: str) -> bool:
    try:
        re.compile(pattern)
    except (re.error, OverflowError, RecursionError):
        return False
    return True


def _locale_problem(item: Locale) -> str | None:
    lang = item.lang
    for what, ok in (('rules', _text_pairs), ('tails', _text_pairs), ('months', _text_map), ('days', _text_map), ('headers', _text_map)):
        if not ok(getattr(item, what)):
            return msg('plugins.why.locale_shape', lang=lang, what=what)
    for what, limit in (('rules', RULES_MAX), ('tails', TAILS_MAX), ('headers', HEADERS_MAX)):
        if len(getattr(item, what)) > limit:
            return msg('plugins.why.locale_many', lang=lang, count=len(getattr(item, what)), what=what, limit=limit)
    reserved = next((key for key in item.page if key.startswith('@')), None)
    if reserved is not None:
        return msg('plugins.why.locale_reserved', lang=lang, key=reserved[:SHOWN_MAX])
    for what, names in (('months', MONTHS), ('days', DAYS)):
        stray = next((key for key in getattr(item, what) if key not in names), None)
        if stray is not None:
            return msg('plugins.why.locale_name', lang=lang, key=stray[:SHOWN_MAX], names=', '.join(names))
    for pattern, replacement in item.rules:
        found = _rule_problem(lang, pattern, replacement)
        if found is not None:
            return found[1]
    if tree_problem(item.chainlit):
        return msg('plugins.why.locale_chainlit', lang=lang)
    return _chat_problem(item)


def _because(code: str, /, **values) -> tuple[str, str]:
    return code, msg(code, **values)


def _rule_problem(lang: str, pattern: str, replacement: str) -> tuple[str, str] | None:
    if '(?<' in pattern:
        return _because('plugins.why.locale_lookbehind', lang=lang, pattern=pattern[:SHOWN_MAX])
    if len(pattern) > RULE_PATTERN_MAX or not _compiles(pattern):
        return _because('plugins.why.locale_rule', lang=lang, pattern=pattern[:SHOWN_MAX], limit=RULE_PATTERN_MAX)
    if re.search(pattern, ''):
        return _because('plugins.why.locale_empty', lang=lang, pattern=pattern[:SHOWN_MAX])
    if len(replacement) > RULE_REPLACEMENT_MAX or LOOSE_DOLLAR.search(replacement):
        return _because('plugins.why.locale_replacement', lang=lang, text=replacement[:SHOWN_MAX], limit=RULE_REPLACEMENT_MAX)
    return None


def _chat_problem(item: Locale) -> str | None:
    found = chat_problem(item.chat)
    return None if found is None else _chat_reason(item.lang, *found)[1]


def _chat_reason(lang: str, what: str, key: str, entry: str) -> tuple[str, str]:
    code = 'plugins.why.locale_chat' if what == 'chat' else f'plugins.why.locale_chat_{what}'
    return _because(code, lang=lang, key=key[:SHOWN_MAX], entry=entry[:SHOWN_MAX], limit=CHAT_TEXT_MAX, count=CHAT_STARTERS_MAX)


def _shown(key: object) -> str:
    return repr(key[:SHOWN_MAX]) if isinstance(key, str) else f'<{type(key).__name__}>'


def _chat_part(what: str, key: str, entry: object, index: int | None) -> str:
    if what == 'chat':
        return 'chat'
    return f'chat[{_shown(key)}]' + (f'[{_shown(entry)}]' if what == 'entry' else '') + ('' if index is None else f'[{index}]')


def _bad_shape(what: str, lang: str, malformed: tuple[str, str]) -> tuple[str, str]:
    return malformed if what in ('page', 'messages') else _because('plugins.why.locale_shape', lang=lang, what=what)


def _whole_problem(what: str, value: object, lang: str, malformed: tuple[str, str]) -> tuple[str, str] | None:
    if not isinstance(value, (tuple, list) if what in LOCALE_LISTS else Mapping):
        return _bad_shape(what, lang, malformed)
    limit = LOCALE_LIMITS.get(what)
    if limit is not None and len(value) > limit:
        return _because('plugins.why.locale_many', lang=lang, count=len(value), what=what, limit=limit)
    return None


def _pair_problem(what: str, pair: object, lang: str) -> tuple[str, str] | None:
    if not _text_pair(pair):
        return _because('plugins.why.locale_shape', lang=lang, what=what)
    return _rule_problem(lang, *pair) if what == 'rules' else None


def _map_entry_problem(what: str, key: object, text: object, lang: str, malformed: tuple[str, str]) -> tuple[str, str] | None:
    if not _text_entry(key, text):
        return _bad_shape(what, lang, malformed)
    if what == 'page' and key.startswith('@'):
        return _because('plugins.why.locale_reserved', lang=lang, key=key[:SHOWN_MAX])
    if what == 'messages' and (why := message_refusal(lang, key, text)) is not None:
        return _because('plugins.why.locale_message', lang=lang, key=key[:SHOWN_MAX], reason=why)
    names = LOCALE_NAMES.get(what)
    if names is not None and key not in names:
        return _because('plugins.why.locale_name', lang=lang, key=key[:SHOWN_MAX], names=', '.join(names))
    return None


def _text_count(value: object) -> int:
    return max(1, len(value)) if isinstance(value, (Mapping, tuple, list)) else 1


def _pack_texts(item: object) -> int:
    if not isinstance(item, Locale):
        return 1
    held = sum(len(value) for value in (getattr(item, what, None) for what in LOCALE_ENTRIES + ('chat', 'chainlit')) if isinstance(value, (Mapping, tuple, list)))
    return max(1, held + (1 if isinstance(getattr(item, 'writing', ''), str) and item.writing else 0))


def _part_kept(parts: dict, what: str, lang: str, malformed: tuple[str, str]) -> list[tuple[str, str, tuple[str, str], int]]:
    value = parts[what]
    whole = _whole_problem(what, value, lang, malformed)
    if whole is not None:
        parts[what] = () if what in LOCALE_LISTS else {}
        return [(what, what, whole, _text_count(value))]
    if what in LOCALE_LISTS:
        judged = [(f'{what}[{index}]', pair, _pair_problem(what, pair, lang)) for index, pair in enumerate(value)]
        parts[what] = tuple(pair for _, pair, found in judged if found is None)
    else:
        judged = [(f'{what}[{_shown(key)}]', (key, text), _map_entry_problem(what, key, text, lang, malformed)) for key, text in value.items()]
        parts[what] = dict(entry for _, entry, found in judged if found is None)
    return [(what, label, found, 1) for label, _, found in judged if found is not None]


def _plain_tree(tree: Mapping) -> dict:
    return {key: _plain_tree(value) if isinstance(value, Mapping) else value for key, value in tree.items()}


def _kept_locale(item: object, index: int) -> tuple[Locale | None, list[tuple[str, str, tuple[str, str], int]]]:
    malformed = _because('plugins.why.locale', limit=WRITING_MAX)
    if not isinstance(item, Locale) or not isinstance(item.lang, str) or not LANG.fullmatch(item.lang):
        return None, [('locales', f'locales[{index}]', malformed, _pack_texts(item))]
    parts = {what: getattr(item, what) for what in LOCALE_PARTS}
    dropped = [found for what in LOCALE_ENTRIES for found in _part_kept(parts, what, item.lang, malformed)]
    if not isinstance(parts['writing'], str) or len(parts['writing']) > WRITING_MAX:
        parts['writing'] = ''
        dropped.append(('writing', 'writing', malformed, 1))
    if tree_problem(parts['chainlit']):
        dropped.append(('chainlit', 'chainlit', _because('plugins.why.locale_chainlit', lang=item.lang), _text_count(parts['chainlit'])))
        parts['chainlit'] = {}
    elif parts['chainlit']:
        parts['chainlit'] = _plain_tree(parts['chainlit'])
    whole_chat = parts['chat']
    parts['chat'], found = chat_kept(whole_chat)
    dropped += [('chat', _chat_part(what, key, entry, at), _chat_reason(item.lang, what, key, str(entry)), _text_count(whole_chat) if what == 'chat' else 1) for what, key, entry, at in found]
    return (replace(item, **parts) if dropped or parts['chainlit'] is not item.chainlit else item), dropped


def _kept_pack(item: object, index: int) -> tuple[Locale | None, list[tuple[str, str, tuple[str, str], int]], str]:
    lang = '?'
    try:
        if isinstance(item, Locale) and isinstance(item.lang, str):
            lang = str(item.lang)[:SHOWN_MAX]
        return *_kept_locale(item, index), lang
    except Exception as exc:
        return None, [('locales', f'locales[{index}]', _because('plugins.why.failed', error=exc_kind(exc)), 1)], lang


def _escaped(text: str) -> str:
    return ''.join(ch if ch.isprintable() else repr(ch)[1:-1] for ch in text)


def kept_packs(owner: str, packs: Sequence[object]) -> tuple[list[Locale], list[Drop], dict[str, int]]:
    kept: list[Locale] = []
    drops: list[Drop] = []
    counts: dict[str, int] = {}
    for index, item in enumerate(packs):
        clean, dropped, lang = _kept_pack(item, index)
        shown = _escaped(lang)
        for part, label, (code, reason), size in dropped:
            log.warning('%s', _escaped(msg('locales.part_dropped', lang=lang, plugin=owner, part=label, reason=reason)))
            counts[shown] = counts.get(shown, 0) + size
            if len(drops) < DROPS_MAX:
                drops.append(Drop(owner, shown, part, _escaped(label), code))
        if clean is not None:
            kept.append(clean)
    return kept, drops, counts


def _kept_locales(plugin: Plugin) -> tuple[Plugin, tuple[Drop, ...], tuple[tuple[str, int], ...]]:
    if not isinstance(plugin.locales, (tuple, list)):
        return plugin, (), ()
    kept, drops, counts = kept_packs(plugin.id, plugin.locales)
    changed = counts or any(a is not b for a, b in zip(kept, plugin.locales))
    return (replace(plugin, locales=tuple(kept)) if changed else plugin), tuple(drops), tuple(counts.items())


def locale_drops(found: Loaded) -> list[tuple[str, int, list[str]]]:
    return [(lang, count, [d.key for d in found.drops if d.lang == lang]) for lang, count in found.dropped]


def _schema_ok(s: object, depth: int = 0) -> bool:
    if not isinstance(s, dict) or depth > SCHEMA_DEPTH or not set(s) <= SCHEMA_KEYS:
        return False
    t = s.get('type')
    if isinstance(t, str):
        t = [t]
    if t is not None and (not isinstance(t, list) or not t or not all(isinstance(x, str) and x in SCHEMA_TYPES for x in t)):
        return False
    if not all(isinstance(s.get(k, ''), str) for k in ('description',)):
        return False
    if 'enum' in s and not isinstance(s['enum'], list):
        return False
    if not all(_number(s[k]) for k in ('minimum', 'maximum', 'minLength', 'maxLength', 'minItems', 'maxItems') if k in s):
        return False
    plain = set(t or ()) - {'null'}
    if ('minimum' in s or 'maximum' in s) and not (plain and plain <= NUMBER_TYPES):
        return False
    if ('minLength' in s or 'maxLength' in s) and plain != {'string'}:
        return False
    if 'additionalProperties' in s and not isinstance(s['additionalProperties'], bool):
        return False
    if 'required' in s and not _texts(s['required']):
        return False
    if 'properties' in s:
        props = s['properties']
        if not isinstance(props, dict) or not all(isinstance(k, str) and _schema_ok(v, depth + 1) for k, v in props.items()):
            return False
    if 'items' in s and not _schema_ok(s['items'], depth + 1):
        return False
    return True


def redaction_patterns(plugins: Iterable[Plugin]) -> tuple[tuple[tuple[str, str], regex.Pattern], ...]:
    return tuple(((p.id, r.id), regex.compile(r.pattern)) for p in plugins for r in p.redactions if problem('redactions', r) is None)


def _plain_json(value: object) -> bool:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return False
    return True


def _name_ok(name: object) -> bool:
    return isinstance(name, str) and bool(str.strip(name)) and len(name) <= NAME_MAX and str.isprintable(name)


def problem(kind: str, item: object) -> str | None:
    try:
        if kind == 'tools':
            if not isinstance(item, Tool) or not isinstance(item.name, str) or not TOOL_NAME.fullmatch(item.name):
                return msg('plugins.why.tool_name', name=str(getattr(item, 'name', ''))[:40])
            if not isinstance(item.description, str) or not item.description.strip():
                return msg('plugins.why.tool_description', name=item.name)
            if not _schema_ok(item.input_schema) or item.input_schema.get('type') != 'object' or not _plain_json(item.input_schema):
                return msg('plugins.why.tool_schema', name=item.name)
            if (item.kind == 'propose') != (item.confirm is not None):
                return msg('plugins.why.propose_confirm', name=item.name)
            if item.name.startswith(BRIDGE_PREFIX):
                return msg('plugins.why.tool_prefix', name=item.name, prefix=BRIDGE_PREFIX)
            if not isinstance(item.image, bool):
                return msg('plugins.why.shape', what=f'tool {item.name} image')
            return None
        if kind == 'knowledge':
            if not isinstance(item, Knowledge) or not isinstance(item.id, str) or not KNOWLEDGE_ID.fullmatch(item.id):
                return msg('plugins.why.knowledge_id', id=str(getattr(item, 'id', ''))[:40])
            title = item.title
            if not isinstance(title, str) or not title.strip() or len(title) > TITLE_MAX or not title.isprintable():
                return msg('plugins.why.knowledge_title', id=item.id, limit=TITLE_MAX)
            if not isinstance(item.text, str):
                return msg('plugins.why.knowledge_text', id=item.id)
            if len(item.text) > SECTION_MAX:
                return msg('plugins.why.knowledge_long', id=item.id, limit=SECTION_MAX)
            if not isinstance(item.inline, bool) or not isinstance(item.slack, bool):
                return msg('plugins.why.shape', what=f'knowledge {item.id} flags')
            if item.inline and len(item.text) > KNOWLEDGE_INLINE_MAX:
                return msg('plugins.why.knowledge_inline_long', id=item.id, limit=KNOWLEDGE_INLINE_MAX)
            return None
        if kind == 'panels':
            ok = isinstance(item, Panel) and isinstance(item.tab, str) and isinstance(item.script, (str, type(None))) and isinstance(item.fragment, (str, type(None))) \
                and isinstance(item.label, str) and isinstance(item.group, str)
            if not ok:
                return msg('plugins.why.panel')
            if item.tab in CORE_TABS:
                return None if not (item.script or item.fragment or item.label) else msg('plugins.why.panel_core', tab=item.tab)
            if item.tab in RESERVED_TABS:
                return msg('plugins.why.panel_reserved', tab=item.tab)
            if not PANEL_TAB.fullmatch(item.tab):
                return msg('plugins.why.panel_tab', tab=item.tab[:40])
            if item.group not in NAV_GROUPS:
                return msg('plugins.why.panel_group', tab=item.tab, group=item.group[:40], groups=', '.join(NAV_GROUPS))
            if item.label and not (0 < len(item.label.strip()) <= PAGE_LABEL_MAX and item.label.strip().isprintable()):
                return msg('plugins.why.panel_label', tab=item.tab, limit=PAGE_LABEL_MAX)
            if item.label and not item.script:
                return msg('plugins.why.panel_script', tab=item.tab)
            return None
        if kind == 'redactions':
            if not isinstance(item, Redaction) or not isinstance(item.id, str) or not isinstance(item.pattern, str):
                return msg('plugins.why.redaction', id=getattr(item, 'id', ''))
            if len(item.pattern) > PATTERN_MAX:
                return msg('plugins.why.redaction_long', id=item.id, limit=PATTERN_MAX)
            try:
                compiled = regex.compile(item.pattern)
            except (regex.error, RecursionError):
                return msg('plugins.why.redaction', id=item.id)
            if compiled.search(''):
                return msg('plugins.why.redaction_empty', id=item.id)
            return None
        if kind == 'tag_rules':
            ok = isinstance(item, TagRule) and isinstance(item.tag, str) and _texts(item.terms)
            return None if ok else msg('plugins.why.tag_rule')
        if kind == 'tags':
            ok = isinstance(item, Tag) and isinstance(item.id, str) and bool(TAG_ID.fullmatch(item.id)) and isinstance(item.label, str) and bool(' '.join(item.label.split()))
            return None if ok else msg('plugins.why.tag')
        if kind == 'locales':
            ok = isinstance(item, Locale) and isinstance(item.lang, str) and bool(LANG.fullmatch(item.lang)) and _text_map(item.page) and _text_map(item.messages) and isinstance(item.writing, str) and len(item.writing) <= WRITING_MAX
            if ok:
                json.dumps(dict(item.page))
            return _locale_problem(item) if ok else msg('plugins.why.locale', limit=WRITING_MAX)
    except (TypeError, ValueError, RecursionError):
        return msg('plugins.why.shape', what=kind)
    return None


def _strings(what: str, value: object) -> None:
    if not isinstance(value, (tuple, list)) or not all(isinstance(v, str) for v in value):
        raise _why('shape', what=what)


def _shape(plugin: Plugin) -> None:
    for what in ('fields', 'checks', 'rules', 'settings', 'tools', 'tags', 'tag_rules', 'panels', 'knowledge', 'locales', 'migrations', 'agent_reads', 'redactions'):
        if not isinstance(getattr(plugin, what), (tuple, list)):
            raise _why('shape', what=what)
    for what in ('panels', 'redactions', 'tags', 'tag_rules', 'tools', 'knowledge'):
        for item in getattr(plugin, what):
            found = problem(what, item)
            if found:
                raise _Reject(found)
    _strings('after', plugin.after)
    _strings('capabilities.hosts', plugin.capabilities.hosts)
    _strings('capabilities.secrets', plugin.capabilities.secrets)
    for check in plugin.checks:
        if not isinstance(check.id, str):
            raise _why('shape', what='check id')
        _strings('ran', check.ran)
        _strings('sections', check.sections)
        helps = check.section_help
        if not _text_map(helps) or not set(helps) <= set(check.sections) or not all(0 < len(v) <= HELP_MAX and v.isprintable() for v in helps.values()):
            raise _why('section_help', id=check.id, limit=HELP_MAX)
        for flag in ('once', 'legacy_alias', 'background', 'daily'):
            if not isinstance(getattr(check, flag), bool):
                raise _why('shape', what=f'check {flag}')
        if check.timeout is not None and (not isinstance(check.timeout, int) or isinstance(check.timeout, bool) or not 1 <= check.timeout <= CHECK_TIMEOUT_MAX):
            raise _why('shape', what='check timeout')
    for read in plugin.agent_reads:
        if not isinstance(read.op, str):
            raise _why('shape', what='agent read op')
    for f in tuple(plugin.fields) + tuple(f for read in plugin.agent_reads for f in read.fields):
        if not isinstance(f.key, str):
            raise _why('shape', what='field key')


def _outbound_ok(hook: object, fields: dict) -> bool:
    if not isinstance(hook, Outbound) or not callable(hook.post):
        return False
    named = (hook.tags_field, hook.mentions_field, hook.link_field, hook.keys_field, hook.sev_field)
    if not isinstance(hook.tags_field, str) or any(k is not None and not isinstance(k, str) for k in named):
        return False
    texts = [k for k in (hook.tags_field, hook.mentions_field, hook.keys_field) if k is not None]
    if any(k not in fields or fields[k].kind != 'text' for k in texts):
        return False
    if hook.link_field is not None and hook.link_field not in fields:
        return False
    sev = fields.get(hook.sev_field) if hook.sev_field is not None else None
    return hook.sev_field is None or (sev is not None and sev.kind == 'choice' and set(sev.choices) <= set(SEVERITIES))


def _setup_ok(setup) -> bool:
    if setup is None:
        return True
    if not isinstance(setup, Setup) or not isinstance(setup.steps, tuple) or not 1 <= len(setup.steps) <= SETUP_STEPS_MAX:
        return False
    if not all(isinstance(s, str) and s.strip() and len(s) <= SETUP_STEP_MAX and s.isprintable() for s in setup.steps):
        return False
    docs, label = setup.docs, setup.docs_label
    if not isinstance(docs, str) or not isinstance(label, str) or len(label) > SETUP_LABEL_MAX or not label.isprintable():
        return False
    if docs and not (docs.startswith('https://') and len(docs) > len('https://') and len(docs) <= SETUP_DOCS_MAX and not any(ch.isspace() for ch in docs) and docs.isprintable()):
        return False
    return not label or (bool(docs) and bool(label.strip()))


def _check(plugin: Plugin, directory: Path | None) -> None:
    if not isinstance(plugin.id, str) or not ID.fullmatch(plugin.id):
        raise _why('id', id=plugin.id)
    if plugin.id in CORE_PREFIXES:
        raise _why('core_prefix', id=plugin.id)
    _shape(plugin)
    if not isinstance(plugin.name, str) or not plugin.name.strip():
        raise _why('missing', what='name')
    if not _name_ok(plugin.name):
        raise _why('name', limit=NAME_MAX)
    if not isinstance(plugin.version, str) or not VERSION.fullmatch(plugin.version):
        raise _why('version', version=plugin.version)
    if plugin.category not in CATEGORIES:
        raise _why('category', value=plugin.category)
    if plugin.edition not in EDITIONS:
        raise _why('edition', value=plugin.edition)
    for what in ('author', 'licence', 'core'):
        value = getattr(plugin, what)
        if not isinstance(value, str) or not value.strip():
            raise _why('missing', what=what)
    if not plugin.icon:
        raise _why('missing', what='icon')
    if _read_icon(directory, plugin.icon) is None:
        raise _why('icon', icon=plugin.icon, kb=ICON_MAX_BYTES // 1024)
    keys = set()
    for f in plugin.fields:
        if not FIELD_KEY.fullmatch(f.key) or f.key in RESERVED_KEYS:
            raise _why('field_key', key=f.key)
        if f.key in keys:
            raise _why('field_repeat', key=f.key)
        keys.add(f.key)
        if f.kind not in FIELD_KINDS:
            raise _why('field_kind', key=f.key, kind=f.kind)
        _field_look(f)
    declared = {f.key: f for f in plugin.fields}
    for read in plugin.agent_reads:
        for f in read.fields:
            if not AGENT_FIELD_KEY.fullmatch(f.key) or f.key in RESERVED_KEYS or f.key in AGENT_HOST_FIELDS:
                raise _why('agent_field', key=f.key)
            if declared.get(f.key, f) != f:
                raise _why('agent_field_repeat', key=f.key)
            declared[f.key] = f
            keys.add(f.key)
            if f.kind not in FIELD_KINDS:
                raise _why('field_kind', key=f.key, kind=f.kind)
            _field_look(f)
    for f in (*plugin.fields, *(f for read in plugin.agent_reads for f in read.fields)):
        if f.check is not None and not callable(f.check):
            raise _why('field_check', key=f.key)
        if f.suggest is not None and (not callable(f.suggest) or f.kind not in SUGGEST_KINDS):
            raise _why('field_suggest', key=f.key, kinds=', '.join(SUGGEST_KINDS))
        if not isinstance(f.separator, str) or f.separator not in SEPARATORS:
            raise _why('field_separator', key=f.key)
    for f in plugin.fields:
        if f.show_if is not None and (len(f.show_if) != 2 or f.show_if[0] not in keys):
            raise _why('show_if', key=f.key, other=f.show_if[0] if f.show_if else '')
        if f.address and FIELD_ADDRESS.get(f.address) != f.kind:
            raise _why('field_address', key=f.key, kind=f.kind, address=str(f.address)[:20])
        if f.hosts_of is not None and (f.address != HOSTS_ADDRESS or not callable(f.hosts_of)):
            raise _why('field_hosts_of', key=f.key)
    _strings('uses', plugin.uses)
    for used in plugin.uses:
        if not ID.fullmatch(used) or used == plugin.id:
            raise _why('uses', id=used[:40])
    for f in plugin.fields:
        if f.kind == 'instance' and (not isinstance(f.choices, (tuple, list)) or len(f.choices) != 1 or f.choices[0] not in plugin.uses):
            raise _why('instance_field', key=f.key)
    for read in plugin.agent_reads:
        for f in read.fields:
            if f.kind == 'instance':
                raise _why('agent_instance', key=f.key)
    limit = plugin.max_instances
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
        raise _why('max_instances')
    if not _setup_ok(plugin.setup):
        raise _why('setup', steps=SETUP_STEPS_MAX, step=SETUP_STEP_MAX, docs=SETUP_DOCS_MAX, label=SETUP_LABEL_MAX)
    if not isinstance(plugin.company_knowledge, str) or (plugin.company_knowledge not in ('', 'always') and plugin.company_knowledge not in {f.key for f in plugin.fields}):
        raise _why('company_knowledge', value=str(plugin.company_knowledge)[:30])
    if not isinstance(plugin.knowledge_also, bool) or (plugin.knowledge_also and not plugin.company_knowledge):
        raise _why('knowledge_also', value=str(plugin.knowledge_also)[:30])
    caps = plugin.capabilities
    for f in tuple(plugin.fields) + tuple(f for read in plugin.agent_reads for f in read.fields):
        if f.kind == 'secret' and f.key not in caps.secrets:
            raise _why('secret', key=f.key)
    if caps.agent_reads != bool(plugin.agent_reads):
        raise _why('agent_reads')
    for read in plugin.agent_reads:
        for key in read.keys:
            try:
                re.compile(key)
            except (re.error, TypeError):
                raise _why('agent_key', key=key) from None
        for f in read.fields:
            if not any(re.fullmatch(key, f.key) for key in read.keys):
                raise _why('agent_field_undeclared', key=f.key, op=read.op)
    urls = {f.key for f in plugin.fields if f.kind == 'url'}
    for host in caps.hosts:
        if host.startswith('@') and host[1:] not in urls:
            raise _why('host', host=host)
    ids = set()
    for check in plugin.checks:
        if check.id in ids:
            raise _why('check_repeat', id=check.id)
        ids.add(check.id)
        if not check.ran:
            raise _why('ran_empty', id=check.id)
        if check.id in CORE_STATE or any(check.id == c or c.startswith(f'{check.id}-') or check.id.startswith(f'{c}-') for c in CORE_RAN):
            raise _why('check_core', id=check.id)
        for section in check.sections:
            if section in CORE_STATE or section in CORE_RAN:
                raise _why('section_core', id=check.id, name=section)
            if not SECTION_NAME.fullmatch(section):
                raise _why('section_name', id=check.id, name=section[:40])
    own = {f.key for f in plugin.fields}
    for tool in plugin.tools:
        if tool.kind not in TOOL_KINDS:
            raise _why('tool_kind', name=tool.name, kind=tool.kind)
        if not isinstance(tool.needs_field, str) or (tool.needs_field and tool.needs_field not in own):
            raise _why('tool_needs_field', name=tool.name, field=str(tool.needs_field)[:40])
    if plugin.outbound is not None and not _outbound_ok(plugin.outbound, {f.key: f for f in plugin.fields}):
        raise _why('outbound')
    if plugin.hooks.entitlement is not None and plugin.edition != 'community':
        raise _why('entitlement')
    _org_pack(plugin.org_pack)
    _card_types(plugin)


def _card_type_ok(item: object) -> bool:
    return (isinstance(item, CardType) and isinstance(item.sig, str) and bool(TAG_ID.fullmatch(item.sig)) and isinstance(item.label, str)
            and 0 < len(item.label.strip()) <= CARD_LABEL_MAX and item.label.isprintable() and isinstance(item.alias, str)
            and (item.alias == '' or (bool(TAG_ID.fullmatch(item.alias)) and item.alias != item.sig)))


def _card_types(plugin: Plugin) -> None:
    for check in plugin.checks:
        if not isinstance(check.own_findings, bool):
            raise _why('shape', what='check own_findings')
    if plugin.card_text is not None and not callable(plugin.card_text):
        raise _why('shape', what='card_text')
    if plugin.deploy_report is not None and not callable(plugin.deploy_report):
        raise _why('shape', what='deploy_report')
    if not isinstance(plugin.card_types, (tuple, list)):
        raise _why('shape', what='card_types')
    seen = set()
    for item in plugin.card_types:
        if not _card_type_ok(item):
            raise _why('card_type', limit=CARD_LABEL_MAX)
        if item.sig in CORE_SIGS:
            raise _why('card_type_core', sig=item.sig)
        if item.sig in seen:
            raise _why('card_type_repeat', sig=item.sig)
        seen.add(item.sig)


def card_types_view(plugin: Plugin) -> list[dict]:
    return [{'sig': t.sig, 'label': ' '.join(t.label.split()), 'alias': t.alias} for t in plugin.card_types]


def section_anchors(text: str) -> list[str]:
    return SECTION_ANCHOR.findall(text)


def _template(what: str, text: str) -> None:
    if len(text) > PACK_TEXT_MAX:
        raise _why('org_pack_long', what=what, limit=PACK_TEXT_MAX)
    allowed = ' and '.join('{' + f + '}' for f in PACK_FIELDS)
    try:
        fields = [(name, spec, conversion) for _, name, spec, conversion in string.Formatter().parse(text) if name is not None]
    except ValueError:
        raise _why('org_pack_format', what=what, allowed=allowed) from None
    if any(spec or conversion for _, spec, conversion in fields):
        raise _why('org_pack_format', what=what, allowed=allowed)
    other = sorted({name for name, _, _ in fields} - set(PACK_FIELDS))
    if other:
        raise _why('org_pack_field', what=what, name='{' + other[0][:40] + '}', allowed=allowed)


def _org_pack(pack: object) -> None:
    if pack is None:
        return
    texts = ('process', 'gate', 'small_talk')
    if not isinstance(pack, OrgPack) or not all(isinstance(getattr(pack, k), str) for k in texts) or not _texts(pack.slack_sections) or not isinstance(pack.plugin_sections, bool):
        raise _why('org_pack')
    if len(pack.process) > PROCESS_MAX:
        raise _why('org_pack_long', what='process', limit=PROCESS_MAX)
    if pack.process:
        anchors = set(section_anchors(pack.process))
        if not anchors:
            raise _why('org_pack_anchor')
        missing = next((s for s in pack.slack_sections if s not in anchors), None)
        if missing is not None:
            raise _why('org_pack_section', id=missing[:40])
    _template('gate', pack.gate)
    _template('small talk', pack.small_talk)


def _claim(plugin: Plugin, claims: dict[str, dict[str, str]], core_tools: frozenset[str], core_knowledge: frozenset[str], core_settings: frozenset[str]) -> dict[str, dict[str, str]]:
    mine: dict[str, dict[str, str]] = {'ran': {}, 'prefix': {}, 'tool': {}, 'knowledge': {}, 'op': {}, 'field': {}, 'page': {}}
    for check in plugin.checks:
        for key in (check.id, *check.ran):
            owner = 'core' if key in CORE_RAN else claims['ran'].get(key)
            if owner is not None:
                raise _why('ran_taken', key=key, owner=owner)
            clash = next((o for p, o in claims['prefix'].items() if key.startswith(p)), None)
            if clash is not None:
                raise _why('ran_prefix', key=key, owner=clash)
            mine['ran'][key] = plugin.id
        prefix = f'{check.id}-'
        clash = next((o for k, o in claims['ran'].items() if k.startswith(prefix)), None)
        if clash is not None:
            raise _why('ran_prefix', key=check.id, owner=clash)
        mine['prefix'][prefix] = plugin.id
    for tool in plugin.tools:
        owner = 'core' if tool.name in core_tools else claims['tool'].get(tool.name) or mine['tool'].get(tool.name)
        if owner is not None:
            raise _why('tool_taken', name=tool.name, owner=owner)
        mine['tool'][tool.name] = plugin.id
    for panel in plugin.panels:
        if not isinstance(panel, Panel) or not isinstance(panel.label, str) or not panel.label:
            continue
        owner = claims['page'].get(panel.tab) or mine['page'].get(panel.tab)
        if owner is not None:
            raise _why('page_taken', tab=panel.tab, owner=owner)
        mine['page'][panel.tab] = plugin.id
    for note in plugin.knowledge:
        owner = 'core' if note.id in core_knowledge else claims['knowledge'].get(note.id) or mine['knowledge'].get(note.id)
        if owner is not None:
            raise _why('knowledge_taken', id=note.id, owner=owner)
        mine['knowledge'][note.id] = plugin.id
    pack = plugin.org_pack
    if pack is not None and not pack.process:
        missing = next((s for s in pack.slack_sections if s not in core_knowledge), None)
        if missing is not None:
            raise _why('org_pack_section', id=missing[:40])
    for read in plugin.agent_reads:
        owner = claims['op'].get(read.op) or mine['op'].get(read.op)
        if owner is not None:
            raise _why('op_taken', op=read.op, owner=owner)
        mine['op'][read.op] = plugin.id
        for f in read.fields:
            owner = claims['field'].get(f.key)
            if owner is not None:
                raise _why('agent_field_taken', key=f.key, owner=owner)
            mine['field'][f.key] = plugin.id
    for key in [f'{plugin.id}.{s.key}' for s in plugin.settings] + [f'{plugin.id}.{r.id}' for r in plugin.rules]:
        if key in core_settings:
            raise _why('setting_taken', key=key)
    return mine


def _find_cycle(start: str, after: Mapping[str, list[str]]) -> list[str] | None:
    seen: set[str] = set()
    stack = [(start, [start])]
    while stack:
        node, path = stack.pop()
        for dep in sorted(after.get(node, ()), reverse=True):
            if dep == start:
                return path + [start]
            if dep in after and dep not in seen:
                seen.add(dep)
                stack.append((dep, path + [dep]))
    return None


def _broken_text(name: str, reason: str) -> str:
    return msg('plugins.broken', name=name, reason=reason)


def _order(items: list[Loaded]) -> list[Loaded]:
    pending = sorted(items, key=lambda x: str(x.id))
    present = {x.id for x in pending if x.state != 'broken'}
    done: set[str] = set()
    out: list[Loaded] = []

    def after(x: Loaded) -> list[str]:
        if x.state == 'broken':
            return []
        try:
            return [d for d in x.plugin.after if d in present and d != x.id]
        except Exception as exc:
            x.state, x.reason = 'broken', _broken_text(_label(x), msg('plugins.why.failed', error=exc_kind(exc)))
            present.discard(x.id)
            return []

    while pending:
        ready = next((x for x in pending if all(d in done or d not in present for d in after(x))), None)
        if ready is not None:
            pending.remove(ready)
            done.add(ready.id)
            out.append(ready)
            continue
        graph = {x.id: after(x) for x in pending if x.state != 'broken'}
        cycles = {x.id: _find_cycle(x.id, graph) for x in pending if x.state != 'broken'}
        members = {k: v for k, v in cycles.items() if v}
        if not members:
            members = {k: [k, k] for k in graph}
        for x in pending:
            if x.id in members:
                x.state = 'broken'
                x.reason = _broken_text(_label(x), msg('plugins.why.cycle', cycle=' -> '.join(members[x.id])))
                present.discard(x.id)
    return out


def _label(x: Loaded) -> str:
    name = getattr(x.plugin, 'name', None)
    return name if _name_ok(name) else str(x.id)


def _names_prerelease(text: str) -> bool:
    try:
        return Version(text).is_prerelease
    except InvalidVersion:
        return False


def _core_fits(spec: SpecifierSet, version: str) -> bool:
    core = Version(version)
    if core.is_prerelease and not any(_names_prerelease(s.version) for s in spec):
        return spec.contains(core.base_version)
    return spec.contains(version, prereleases=True)


def _static(items: list[Loaded]) -> list[Loaded]:
    seen: dict[str, str] = {}
    for x in items:
        if x.state != 'ok':
            continue
        if x.id in seen:
            x.state, x.reason = 'broken', _broken_text(_label(x), msg('plugins.why.duplicate_id', id=x.id, origin=msg(ORIGIN_WORDS[seen[x.id]])))
            continue
        seen[x.id] = x.origin
        try:
            spec = SpecifierSet(x.plugin.core)
        except (InvalidSpecifier, TypeError):
            x.state, x.reason = 'broken', _broken_text(_label(x), msg('plugins.why.core_spec', spec=x.plugin.core))
            continue
        if not _core_fits(spec, skopax.__version__):
            x.state, x.reason = 'incompatible', msg('plugins.incompatible', name=_label(x), core=x.plugin.core, version=skopax.__version__)
    return _order(items)


class Registry:
    def __init__(self, found: list[Loaded], core_tools: frozenset[str] = frozenset(), core_knowledge: frozenset[str] = frozenset(), core_settings: frozenset[str] = frozenset()):
        self._lock = threading.RLock()
        self._warned: set[tuple] = set()
        self._forced: dict[str, str] = {}
        self._core_tools = core_tools
        self._core_knowledge = core_knowledge
        self._core_settings = core_settings
        self._base = _static(list(found))
        self._snapshot: list[Loaded] = []
        self._edition = 'community'
        self._reserved: frozenset[str] = frozenset()
        self._good_edition = 'community'
        self.hook_timeout = HOOK_TIMEOUT_S
        self._calls = Bound('entitlement')
        self.refresh()

    def _marked(self) -> tuple[list[Loaded], set[str]]:
        items = [replace(x) for x in self._base]
        forced = set()
        for x in items:
            if x.state == 'ok' and x.id in self._forced:
                x.state, x.reason = 'broken', _broken_text(_label(x), self._forced[x.id])
                forced.add(x.id)
        return items, forced

    def refresh(self) -> None:
        with self._lock:
            providers = self._providers(self._marked()[0])
        edition = self._provided(providers)
        with self._lock:
            items, forced = self._marked()
            for x in items:
                if (x.state == 'ok' or x.id in forced) and EDITIONS.index(x.plugin.edition) > EDITIONS.index(edition):
                    x.state, x.reason = 'locked', msg('plugins.needs_edition', name=_label(x), edition=x.plugin.edition.capitalize())
            claims: dict[str, dict[str, str]] = {'ran': {}, 'prefix': {}, 'tool': {}, 'knowledge': {}, 'op': {}, 'field': {}, 'page': {}}
            for x in sorted(items, key=lambda y: str(y.id)):
                if x.state != 'ok':
                    continue
                try:
                    mine = _claim(x.plugin, claims, self._core_tools, self._core_knowledge, self._core_settings)
                except _Reject as exc:
                    x.state, x.reason = 'broken', _broken_text(_label(x), exc.text)
                    continue
                except Exception as exc:
                    x.state, x.reason = 'broken', _broken_text(_label(x), msg('plugins.why.failed', error=exc_kind(exc)))
                    continue
                for kind, taken in mine.items():
                    claims[kind].update(taken)
            names = set(CORE_RAN) | set(INSTANCE_WORDS)
            for x in items:
                if x.state != 'broken':
                    for check in x.plugin.checks:
                        names.update((check.id, *check.ran))
            self._snapshot, self._edition, self._reserved = items, edition, frozenset(names)

    @classmethod
    def load(cls, env: Mapping[str, str], builtin: tuple[str, ...] = BUILTIN, entry_points: bool = True, core_tools: frozenset[str] = frozenset(), core_knowledge: frozenset[str] = frozenset(),
             core_settings: frozenset[str] = frozenset()) -> 'Registry':
        found: list[Loaded] = []
        seen: set[str] = set()
        seen_points: set[str] = set()

        def add(label: str, origin: str, load, dist: tuple[str, str] | None = None) -> None:
            try:
                plugin, module = load()
            except (Exception, SystemExit) as exc:
                reason = exc.text if isinstance(exc, _Reject) else msg('plugins.why.import', error=exc_kind(exc))
                found.append(Loaded(label, label, origin, None, 'broken', _broken_text(label, reason), None, dist=dist))
                return
            item = _loaded(plugin, label, origin, module)
            item.dist = dist or (_dist_of(item.module) if origin != 'builtin' else None)
            found.append(item)

        def from_module(path: str):
            def run():
                if ':' in path:
                    raise _why('module_path', module=path)
                module = importlib.import_module(path)
                if not hasattr(module, ATTR):
                    raise _why('no_plugin', module=path, attr=ATTR)
                return getattr(module, ATTR), module
            return run

        for path in builtin:
            if path not in seen:
                seen.add(path)
                add(path, 'builtin', from_module(path))
        if entry_points:
            for point in sorted(importlib.metadata.entry_points(group=GROUP), key=lambda p: p.name):
                if point.value not in seen_points:
                    seen_points.add(point.value)
                    add(point.name, 'entry-point', lambda point=point: (point.load(), sys.modules.get(point.module)), _dist_name(point))
        for path in [p.strip() for p in (env.get(ENV) or '').split(',') if p.strip()]:
            if path not in seen:
                seen.add(path)
                add(path, 'env', from_module(path))
        return cls(found, core_tools, core_knowledge, core_settings)

    @classmethod
    def of(cls, *plugins: Plugin, directory: Path | None = None, core_tools: frozenset[str] = frozenset(), core_knowledge: frozenset[str] = frozenset(),
           core_settings: frozenset[str] = frozenset()) -> 'Registry':
        found = []
        for plugin in plugins:
            module, home = _home(plugin)
            found.append(_loaded(plugin, str(getattr(plugin, 'id', plugin)), 'builtin', directory=directory or home, modname=module))
        return cls(found, core_tools, core_knowledge, core_settings)

    def _providers(self, items: list[Loaded]) -> list[Loaded]:
        return [x for x in items if x.plugin is not None and x.state not in ('broken', 'incompatible') and x.plugin.hooks.entitlement is not None]

    def _provided(self, providers: list[Loaded]) -> str:
        if not providers:
            with self._lock:
                self._good_edition = 'community'
            return 'community'
        for extra in providers[1:]:
            self._warn(('extra', extra.id), f'plug-in {extra.id} provides an entitlement but {providers[0].id} already does; ignoring it')
        first = providers[0]
        with self._lock:
            kept = self._good_edition
        try:
            value = self._calls.call(first.id, first.plugin.hooks.entitlement, self.hook_timeout)
        except Exception as exc:
            self._warn(('failed', first.id, exc_kind(exc)), f'plug-in {first.id}: entitlement failed ({exc_kind(exc)}); keeping the {kept} edition')
            return kept
        if value not in EDITIONS:
            self._warn(('value', first.id, str(value)), f'plug-in {first.id}: entitlement returned {value!r}; keeping the {kept} edition')
            return kept
        with self._lock:
            self._good_edition = value
        return value

    def _warn(self, key: tuple, text: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(text)

    def loaded(self) -> list[Loaded]:
        with self._lock:
            return [replace(x) for x in self._snapshot]

    def usable(self) -> list[Plugin]:
        return [x.plugin for x in self.loaded() if x.state == 'ok']

    def get(self, id: str) -> Loaded | None:
        found = [x for x in self.loaded() if x.id == id]
        return next((x for x in found if x.state != 'broken'), found[0] if found else None)

    def edition(self) -> str:
        with self._lock:
            return self._edition

    def extensions(self) -> list[dict]:
        groups: dict[tuple[str, str], list[str]] = {}
        for x in self.loaded():
            if x.origin == 'builtin' or x.module.split('.')[0] == 'skopax' or (x.dist is not None and x.dist[0].lower().replace('_', '-') == 'skopax'):
                continue
            version = x.plugin.version if x.plugin is not None else ''
            name, shown = x.dist or (x.id, version if isinstance(version, str) else '')
            groups.setdefault((name, shown, x.dist is not None), []).append(x.id)
        return [{'name': name, 'version': version, 'package': package, 'plugins': sorted(ids)} for (name, version, package), ids in sorted(groups.items())]

    def standing(self, enabled: Callable[[str], bool]) -> list[dict]:
        rows = []
        for x in self.loaded():
            rows.append({'id': x.id, 'version': x.plugin.version if x.plugin is not None else '', 'state': standing_of(x, enabled), 'reason': x.reason or ''})
        return sorted(rows, key=lambda r: (r['state'] == 'ok', str(r['id'])))

    def reserved_names(self) -> frozenset[str]:
        with self._lock:
            return self._reserved

    def reserved_prefixes(self) -> frozenset[str]:
        return frozenset(f'{name}-' for name in self.reserved_names())

    def shadowed(self, plugin_id: str, name: str) -> bool:
        found = self.get(plugin_id)
        if found is None or found.state == 'broken':
            return False
        reserved = self.reserved_names()
        keys = {k for check in found.plugin.checks for k in (check.id, *check.ran)}
        twins = {t[len(c) + 1:] for c in keys for t in keys if t.startswith(f'{c}-')}
        return any(f'{k}-{name}' in reserved for k in keys) or any(name.startswith(f'{s}-') for s in twins)

    def icon(self, id: str) -> str | None:
        found = self.get(id)
        return None if found is None or found.state == 'broken' else _read_icon(found.directory, found.plugin.icon)

    def runnable(self) -> list[Plugin]:
        with self._lock:
            return [x.plugin for x in self._snapshot if x.state == 'ok' or (x.plugin is not None and x.id in self._forced and x.reason == _broken_text(_label(x), self._forced[x.id]))]

    def forced(self) -> list[str]:
        with self._lock:
            return list(self._forced)

    def clear_forced(self, ids: list[str]) -> None:
        with self._lock:
            dropped = [i for i in ids if self._forced.pop(i, None) is not None]
        if dropped:
            self.refresh()

    def own_findings(self) -> frozenset[str]:
        with self._lock:
            found = [x.plugin for x in self._snapshot if x.plugin is not None]
        checks = [c for p in found if isinstance(getattr(p, 'checks', None), (tuple, list)) for c in p.checks]
        return frozenset(c.id for c in checks if isinstance(c, Check) and c.own_findings is True and isinstance(c.id, str))

    def mark_broken(self, id: str, reason: str) -> None:
        with self._lock:
            if not any(x.id == id and x.state != 'broken' for x in self._snapshot):
                raise KeyError(id)
            self._forced[id] = reason
        self.refresh()


def standing_of(found: Loaded, enabled: Callable[[str], bool]) -> str:
    state = STANDING.get(found.state, found.state)
    return 'off' if state == 'ok' and not enabled(found.id) else state


def _dist_name(point) -> tuple[str, str] | None:
    try:
        dist = point.dist
        name, version = str(dist.metadata['Name'] or '').strip(), str(dist.version or '').strip()
    except Exception:
        return None
    return (name, version) if name else None


def _dist_of(module: str) -> tuple[str, str] | None:
    try:
        top = str(module).split('.')[0]
        owners = importlib.metadata.packages_distributions().get(top) or []
        path = Path(sys.modules[module].__file__).resolve()
        for owner in owners:
            dist = importlib.metadata.distribution(owner)
            if any(Path(dist.locate_file(f)).resolve() == path for f in dist.files or ()):
                return dist.metadata['Name'], dist.version
    except Exception:
        return None
    return None


def _loaded(plugin: object, label: str, origin: str, module=None, directory: Path | None = None, modname: str | None = None) -> Loaded:
    if module is not None:
        modname = module.__name__
        directory = Path(module.__file__).parent if getattr(module, '__file__', None) else None
    modname = modname or label
    if not isinstance(plugin, Plugin):
        return Loaded(label, modname, origin, None, 'broken', _broken_text(label, msg('plugins.why.not_plugin', where=label)), None)
    try:
        _check(plugin, directory)
        kept, drops, dropped = _kept_locales(plugin)
        if kept.outbound is not None and not any(f.key == ECHO_FIELD for f in kept.fields):
            kept = replace(kept, fields=(*kept.fields, ROUTE_ECHO))
    except _Reject as exc:
        return Loaded(str(plugin.id), modname, origin, plugin, 'broken', _broken_text(_pname(plugin, label), exc.text), directory)
    except Exception as exc:
        return Loaded(str(plugin.id), modname, origin, plugin, 'broken', _broken_text(_pname(plugin, label), msg('plugins.why.failed', error=exc_kind(exc))), directory)
    return Loaded(plugin.id, modname, origin, kept, 'ok', None, directory, drops, dropped)


def _pname(plugin: Plugin, label: str) -> str:
    return plugin.name if _name_ok(plugin.name) else label


def _label_ok(text: object) -> bool:
    return isinstance(text, str) and 1 <= len(text) <= CHOICE_LABEL_MAX and text.isprintable() and bool(text.strip())


def _field_look(f) -> None:
    labels = f.choice_labels
    if not isinstance(labels, Mapping) or (labels and f.kind != 'choice') or any(k not in f.choices or not _label_ok(v) for k, v in labels.items()):
        raise _why('field_labels', key=f.key, limit=CHOICE_LABEL_MAX)
    if not isinstance(f.group, str) or (f.group and not FIELD_GROUP.fullmatch(f.group)):
        raise _why('field_group', key=f.key, group=str(f.group)[:30])


def describe(plugin: Plugin, icon: bool | None = None) -> dict:
    caps = plugin.capabilities
    return {
        'id': plugin.id, 'name': plugin.name, 'version': plugin.version, 'category': plugin.category, 'description': plugin.description, 'author': plugin.author, 'licence': plugin.licence, 'core': plugin.core,
        'edition': plugin.edition, 'after': list(plugin.after), 'uses': list(plugin.uses), 'maxInstances': plugin.max_instances, 'icon': bool(plugin.icon) if icon is None else icon,
        'capabilities': {'hosts': list(caps.hosts), 'secrets': list(caps.secrets), 'agentReads': caps.agent_reads},
        'fields': [field_view(f) for f in plugin.fields],
        'checks': [{'id': c.id, 'label': c.label, 'period': c.period, 'offset': c.offset, 'ran': list(c.ran), **({'once': True} if c.once else {})} for c in plugin.checks],
        'rules': [{'id': r.id, 'label': r.label, 'defaults': dict(r.defaults), 'help': dict(r.help)} for r in plugin.rules],
        'settings': [{'key': s.key, 'label': s.label, 'help': s.help, 'kind': s.kind, 'default': s.default, 'low': s.low, 'high': s.high, 'choices': list(s.choices), 'live': s.live} for s in plugin.settings],
        'tools': [{'name': t.name, 'kind': t.kind, 'description': t.description} for t in plugin.tools],
        'knowledge': [k.id for k in plugin.knowledge],
        'panels': [{'tab': p.tab, 'script': p.script, 'fragment': p.fragment, 'label': p.label, 'group': p.group} for p in plugin.panels],
        'locales': [loc.lang for loc in plugin.locales],
        'hooks': [name for name in ('role_resolver', 'settings_source', 'oidc_token_request', 'org_validators', 'entitlement') if getattr(plugin.hooks, name) is not None],
        'migrations': [m.version for m in plugin.migrations],
        'agentReads': [{'op': r.op, 'args': list(r.args), 'keys': list(r.keys)} for r in plugin.agent_reads],
        'setup': None if plugin.setup is None else {'steps': list(plugin.setup.steps), 'docs': plugin.setup.docs, 'docsLabel': plugin.setup.docs_label},
        'companyKnowledge': plugin.company_knowledge,
        'knowledgeAlso': plugin.knowledge_also,
    }


def field_view(f) -> dict:
    return {'key': f.key, 'label': f.label, 'kind': f.kind, 'required': f.required, 'default': f.default, 'help': f.help, 'choices': list(f.choices), 'low': f.low, 'high': f.high,
            'showIf': list(f.show_if) if f.show_if else None, 'ui': f.ui, 'choiceLabels': dict(f.choice_labels), 'group': f.group,
            **({'suggest': True, 'separator': f.separator} if f.suggest is not None else {})}
