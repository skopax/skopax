import logging
import math
import re
import threading
import time
from collections.abc import Callable

from skopax.enrich import JiraRefused
from skopax.errkind import kind

log = logging.getLogger('skopax.jirafields')
TTL_S = 300
RETRY_S = 30
NAME_MAX = 200
VALUE_MAX = 255
TEXT_MAX = 4000
OPTIONS_MAX = 1000
OPTION_MAX = 1000
FILLED = ('summary', 'description', 'project', 'issuetype', 'parent', 'reporter', 'assignee')
SPRINT_SCHEMA = 'com.pyxis.greenhopper.jira:gh-sprint'
TEXTAREA = 'com.atlassian.jira.plugin.system.customfieldtypes:textarea'
POINT_NAMES = ('story point estimate', 'story points')
SUPPORTED = ('option', 'options', 'string', 'textarea', 'number', 'user')
ROLES = {'scope': 'jira_scope_field', 'points': 'jira_points_field', 'developer': 'jira_developer_field', 'sprint': 'jira_sprint_field'}
ACCOUNT_ID = re.compile(r'\d{1,12}:[A-Za-z0-9-]{4,64}|[0-9a-f]{24}')
EMAIL = re.compile(r'[^@\s]{1,64}@[^@\s]{1,190}')
FIELD_ID = re.compile(r'customfield_\d{1,12}|[a-z][A-Za-z0-9_]{0,40}')


def field_id_ok(text: str) -> bool:
    return not text or (bool(FIELD_ID.fullmatch(text)) and text not in FILLED)


def kind_of(schema: object) -> str:
    schema = schema if isinstance(schema, dict) else {}
    t, items, custom = schema.get('type'), schema.get('items'), schema.get('custom')
    if t == 'option':
        return 'option'
    if t == 'array' and items == 'option':
        return 'options'
    if t == 'string':
        return 'textarea' if custom == TEXTAREA else 'string'
    if t == 'number':
        return 'number'
    if t == 'user':
        return 'user'
    return 'other'


def options_of(raw: object) -> list[str]:
    out = []
    for v in raw if isinstance(raw, list) else []:
        if isinstance(v, dict) and isinstance(v.get('value'), str) and v['value'].strip() and not v.get('disabled'):
            out.append(v['value'][:OPTION_MAX])
    return list(dict.fromkeys(out))[:OPTIONS_MAX]


def create_field(raw: dict) -> dict | None:
    fid = str(raw.get('fieldId') or raw.get('key') or '')
    if not fid:
        return None
    k = kind_of(raw.get('schema'))
    return {'id': fid, 'name': str(raw.get('name') or fid)[:NAME_MAX], 'required': bool(raw.get('required')), 'hasDefault': bool(raw.get('hasDefaultValue')), 'kind': k,
            'options': options_of(raw.get('allowedValues')) if k in ('option', 'options') else None}


def create_meta(raw: list[dict] | None) -> dict[str, dict] | None:
    if raw is None:
        return None
    return {f['id']: f for f in (create_field(r) for r in raw) if f}


def own_ids(settings) -> set[str]:
    return set(FILLED) | {getattr(settings, a) for a in ROLES.values() if getattr(settings, a, '')}


def asked(meta: dict[str, dict], settings) -> list[dict]:
    own = own_ids(settings)
    return [f for f in meta.values() if f['required'] and not f['hasDefault'] and f['id'] not in own]


def dialog_view(meta: dict[str, dict] | None, settings) -> dict:
    if meta is None:
        return {'readable': False, 'scope': None, 'asked': [], 'unsupported': []}
    scope = meta.get(settings.jira_scope_field) if settings.jira_scope_field else None
    wanted = asked(meta, settings)
    return {'readable': True,
            'scope': {'name': scope['name'], 'options': scope['options'] or [], 'required': scope['required'], 'multi': scope['kind'] == 'options'} if scope and scope['kind'] in ('option', 'options') else None,
            'asked': [{k: f[k] for k in ('id', 'name', 'kind', 'options')} for f in wanted if f['kind'] in SUPPORTED],
            'unsupported': [f['name'] for f in wanted if f['kind'] not in SUPPORTED]}


def texts(value: object) -> list[str]:
    items = value if isinstance(value, list) else [value]
    return list(dict.fromkeys(str(v).strip() for v in items if isinstance(v, str) and v.strip()))


def option_value(field: dict | None, name: str, values: list[str]) -> list[dict] | dict:
    if field is not None and field['options'] is not None:
        allowed = {o.strip(): o for o in field['options']}
        bad = [v for v in values if v.strip() not in allowed]
        if bad:
            raise JiraRefused(f"{name}: {', '.join(bad)} is not an allowed value; it is one of {', '.join(field['options'])}")
        values = [allowed[v.strip()] for v in values]
    if field is not None and field['kind'] == 'option':
        if len(values) > 1:
            raise JiraRefused(f'{name} takes one value')
        return {'value': values[0]}
    return [{'value': v} for v in values]


def value_of(field: dict, value: object, find_user: Callable[[str], str | None]) -> object:
    name, k = field['name'], field['kind']
    if k in ('option', 'options'):
        picked = texts(value)
        return option_value(field, name, picked) if picked else None
    if k == 'number':
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise JiraRefused(f'{name} is a number')
        return value
    if not isinstance(value, str):
        raise JiraRefused(f'{name} is text')
    text = value.strip()
    if not text:
        return None
    if k == 'string':
        if len(text) > VALUE_MAX:
            raise JiraRefused(f'{name} is at most {VALUE_MAX} characters')
        return text
    if k == 'textarea':
        if len(text) > TEXT_MAX:
            raise JiraRefused(f'{name} is at most {TEXT_MAX} characters')
        return {'type': 'doc', 'version': 1, 'content': [{'type': 'paragraph', 'content': [{'type': 'text', 'text': text}]}]}
    if EMAIL.fullmatch(text):
        account = find_user(text)
        if not account:
            raise JiraRefused(f'no Jira account found for {text} in {name}')
        return {'accountId': account}
    if ACCOUNT_ID.fullmatch(text):
        return {'accountId': text}
    raise JiraRefused(f'{name} takes an e-mail or a Jira account id')


def given_fields(given: dict, meta: dict[str, dict] | None, settings, project: str, find_user: Callable[[str], str | None]) -> dict:
    if meta is None:
        if given:
            raise JiraRefused(f'the {project} create screen could not be read from Jira, so the extra fields were not checked; try again')
        return {}
    own = own_ids(settings)
    out = {}
    for fid, value in given.items():
        field = meta.get(fid)
        if field is None or fid in own or field['kind'] not in SUPPORTED:
            raise JiraRefused(f'{fid} is not a field of the {project} create screen that the dialog fills')
        sent = value_of(field, value, find_user)
        if sent is not None:
            out[fid] = sent
    missing = [f['name'] for f in asked(meta, settings) if f['kind'] in SUPPORTED and f['id'] not in out]
    if missing:
        raise JiraRefused('Jira needs ' + ', '.join(missing))
    return out


def fits(role: str, field: dict) -> bool:
    schema = field.get('schema') if isinstance(field.get('schema'), dict) else {}
    k = kind_of(schema)
    if role == 'sprint':
        return schema.get('custom') == SPRINT_SCHEMA
    if role == 'points':
        return k == 'number'
    if role == 'developer':
        return k == 'user' and bool(field.get('custom'))
    return k in ('option', 'options')


def field_choices(raw: list[dict], role: str) -> dict:
    listed = [f for f in raw if isinstance(f, dict) and isinstance(f.get('id'), str) and f['id']]
    rows = sorted(({'id': f['id'], 'name': str(f.get('name') or f['id'])[:NAME_MAX], 'fits': fits(role, f)} for f in listed), key=lambda r: (not r['fits'], r['name'].lower(), r['id']))
    for r in rows:
        r['label'] = f"{r['name']} ({r['id']})"
    fitting = [r for r in rows if r['fits']]
    if role == 'sprint':
        suggest = fitting[0] if fitting else None
    elif role == 'points':
        named = [r for r in fitting for n in POINT_NAMES if r['name'].strip().lower() == n]
        suggest = sorted(named, key=lambda r: POINT_NAMES.index(r['name'].strip().lower()))[0] if named else None
    else:
        suggest = fitting[0] if len(fitting) == 1 else None
    return {'role': role, 'fields': rows, 'suggest': {'id': suggest['id'], 'label': suggest['label']} if suggest else None}


class JiraFields:
    def __init__(self, jira: object, now: Callable[[], float] = time.monotonic, ttl: float = TTL_S, retry: float = RETRY_S):
        self.jira = jira
        self.now = now
        self.ttl = ttl
        self.retry = retry
        self.held: dict[tuple, tuple[float, object]] = {}
        self.lock = threading.Lock()

    def _read(self, key: tuple, read: Callable[[], object]) -> object:
        at = self.now()
        with self.lock:
            hit = self.held.get(key)
        if hit is not None and hit[0] > at:
            return hit[1]
        try:
            value = read()
            until = at + (self.ttl if value is not None else self.retry)
        except Exception as e:
            log.warning('jira field metadata (%s) not read: %s', key[0], kind(e))
            value, until = None, at + self.retry
        with self.lock:
            self.held[key] = (until, value)
        return value

    def all(self) -> list[dict] | None:
        return self._read(('fields',), lambda: self.jira.fields())

    def field_kind(self, fid: str) -> str | None:
        found = next((f for f in self.all() or [] if isinstance(f, dict) and f.get('id') == fid), None)
        return kind_of(found.get('schema')) if found else None

    def create_meta(self, project: str, issue_type: str) -> dict[str, dict] | None:
        return self._read(('createmeta', project, issue_type), lambda: create_meta(self.jira.create_fields(project, issue_type)))
