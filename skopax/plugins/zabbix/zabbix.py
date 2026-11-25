import json
import logging
from urllib.parse import quote

from skopax import clock
from skopax.contract import HttpError, PassResult, Sweep, Transport, Unreachable, anomaly, iso, send
from skopax.kit import access, flags
from skopax.messages import msg
from skopax.redact import redact_secrets
from skopax.remote import JSON_ACCEPT, PAGE_READ, Reply

log = logging.getLogger('skopax.plugins.zabbix')

NAME = 'Zabbix'
PASS = 'zabbix'
SIG = 'zabbix'
NO_DATA = f'{PASS}:no-data'
SECTION = 'zabbixProblems'
PART = 'problems'
PERMISSION = 'read access to the host groups and API access for its user'
API_PATH = '/api_jsonrpc.php'
TIMEOUT = 20
ANSWER_LIMIT = 8 * 1024 * 1024
MAX_PROBLEMS = 500
BODY_AUTH_BEFORE = (6, 4)
GROUP_SELECTOR_FROM = (6, 2)
READ_METHODS = frozenset({'apiinfo.version', 'problem.get', 'trigger.get', 'host.get', 'hostgroup.get', 'event.get', 'history.get', 'item.get'})
SEVERITY_NAMES = ('not-classified', 'information', 'warning', 'average', 'high', 'disaster')
SEVERITY_LABELS = {'not-classified': 'Not classified', 'information': 'Information', 'warning': 'Warning', 'average': 'Average', 'high': 'High', 'disaster': 'Disaster'}
DEFAULT_MIN = 'warning'
SEV_MAP = {0: 'info', 1: 'info', 2: 'warn', 3: 'warn', 4: 'high', 5: 'crit'}
SERVICE_TAGS = ('service', 'component')
HELD = '[held: the text reads like instructions to an AI]'
NAME_LIMIT = 200
LABEL_LIMIT = 60
HOST_LIMIT = 120
OPDATA_LIMIT = 200
TAG_LIMIT = 100
MAX_TAGS = 15
REASON_LIMIT = 200
TAG_EXISTS = 4
TAG_EQUALS = 1
TAGS_ANY = 2
DOT = ' \u00b7 '
DENIED_WORDS = ('not authori', 'session terminated', 're-login', 'api token expired', 'not authenticated')
NO_PERMISSION_WORDS = ('no permissions',)
ID_MAX = 20
CAPPED = f'read {MAX_PROBLEMS} problems, more not read'


class ZabbixError(Exception):
    def __init__(self, reason: str, code: int | None = None, answered: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.answered = answered


def flat(text: object) -> str:
    return ' '.join(str(text if text is not None else '').split())


def masked(text: object, limit: int) -> str:
    return redact_secrets(flat(text))[:limit]


def planted(text: str) -> bool:
    return bool(flags.scan(PASS, text, skip=flags.REQUESTS))


def clean(text: object, limit: int) -> str:
    shown = masked(text, limit)
    return HELD if planted(shown) else shown


def version_of(text: object) -> tuple[int, ...]:
    parts = []
    for piece in str(text or '').split('.')[:3]:
        digits = ''.join(c for c in piece if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def number(value: object, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def is_id(value: object) -> bool:
    text = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ''
    return 0 < len(text) <= ID_MAX and text.isascii() and text.isdigit()


def denied_text(said: str) -> bool:
    lowered = said.lower()
    return any(w in lowered for w in DENIED_WORDS)


class Zabbix:
    def __init__(self, url: str, token: str, transport: Transport = send, timeout: float = TIMEOUT):
        self.url = (url or '').rstrip('/')
        self.api = self.url + API_PATH
        self.token = token
        self.transport = transport
        self.timeout = timeout
        self.version = ''
        self.body_auth = False
        self._id = 0

    def connect(self) -> str:
        found = self.call('apiinfo.version', {}, auth=False)
        if not isinstance(found, str) or not version_of(found):
            raise ZabbixError('Zabbix apiinfo.version: the answer is not a version', answered=True)
        self.version = found
        self.body_auth = version_of(found) < BODY_AUTH_BEFORE
        return found

    def at_least(self, wanted: tuple[int, ...]) -> bool:
        return version_of(self.version) >= wanted

    def call(self, method: str, params: dict | list, auth: bool = True) -> object:
        if method not in READ_METHODS:
            raise ZabbixError(f'{method} is not a read method; the Zabbix plug-in only reads')
        self._id += 1
        body: dict = {'jsonrpc': '2.0', 'method': method, 'params': params, 'id': self._id}
        headers = {'Accept': JSON_ACCEPT, 'Content-Type': 'application/json-rpc', PAGE_READ: str(ANSWER_LIMIT)}
        if auth:
            headers['Authorization'] = f'Bearer {self.token}'
            if self.body_auth:
                body['auth'] = self.token
        what = f'Zabbix {method}'
        try:
            reply = self.transport('POST', self.api, headers, json.dumps(body).encode(), self.timeout)
        except HttpError as e:
            raise self._status(what, e.code, (e.headers or {}).get('location', '')) from None
        except Unreachable:
            raise ZabbixError(f'{what}: unreachable') from None
        return self._read(what, reply)

    def _status(self, what: str, code: int | None, location: str = '') -> ZabbixError:
        if code in (301, 302, 303, 307, 308):
            where = f' to {masked(location, REASON_LIMIT).split("?")[0]}' if location else ''
            return ZabbixError(f'{what}: HTTP {code}, a redirect{where} that is not followed; set URL to the final Zabbix address', code, answered=True)
        if access.denied(code):
            return ZabbixError(f'{what}: HTTP {code}, the token was refused', code, answered=True)
        if code == 404:
            return ZabbixError(f'{what}: HTTP 404, no Zabbix API at {self.api}; set URL to the Zabbix frontend address', code, answered=True)
        return ZabbixError(f'{what}: HTTP {code}', code, answered=True)

    def _read(self, what: str, reply: Reply) -> object:
        status = reply.status if isinstance(reply.status, int) else 200
        if status != 200:
            raise self._status(what, status, (reply.headers or {}).get('location', ''))
        raw = reply.body
        if isinstance(raw, (bytes, str)):
            if len(raw) > ANSWER_LIMIT:
                raise ZabbixError(f'{what}: the answer is larger than {ANSWER_LIMIT // (1024 * 1024)} MB', answered=True)
            try:
                raw = json.loads(raw or b'null')
            except ValueError:
                raise ZabbixError(msg('connection.not_api'), answered=True) from None
        if not isinstance(raw, dict) or ('result' not in raw and 'error' not in raw):
            raise ZabbixError(msg('connection.not_api'), answered=True)
        if 'error' in raw:
            error = raw['error'] if isinstance(raw['error'], dict) else {}
            said = flat(f"{error.get('message') or ''} {error.get('data') or ''}")
            shown = masked(said.replace(self.token, '[redacted]') if self.token else said, REASON_LIMIT)
            if denied_text(said):
                raise ZabbixError(f'{what}: {shown}', 401, answered=True)
            if any(w in said.lower() for w in NO_PERMISSION_WORDS):
                raise ZabbixError(f'{what}: {shown}', 403, answered=True)
            raise ZabbixError(f'{what}: {shown}', answered=True)
        return raw['result']

    def listed(self, method: str, params: dict) -> list:
        found = self.call(method, params)
        if not isinstance(found, list):
            raise ZabbixError(f'Zabbix {method}: the answer is not a list', answered=True)
        return [x for x in found if isinstance(x, dict)]

    def count(self, method: str, params: dict) -> int:
        return number(self.call(method, params | {'countOutput': True}))

    def group_selector(self) -> str:
        return 'selectHostGroups' if self.at_least(GROUP_SELECTOR_FROM) else 'selectGroups'


def client_for(config, transport: Transport, timeout: float = TIMEOUT) -> Zabbix:
    return Zabbix(config.get('url') or '', config.get('token') or '', transport, timeout)


def names_of(raw: str) -> list[str]:
    return list(dict.fromkeys(n.strip() for n in (raw or '').split(',') if n.strip()))


def tag_filters(raw: str) -> list[dict]:
    out = []
    for item in names_of(raw):
        name, sep, value = item.partition(':')
        name, value = name.strip(), value.strip()
        if not name:
            continue
        out.append({'tag': name, 'value': value, 'operator': TAG_EQUALS} if sep and value else {'tag': name, 'operator': TAG_EXISTS})
    return out


def min_level(raw: str) -> int:
    value = (raw or DEFAULT_MIN).strip().lower()
    return SEVERITY_NAMES.index(value) if value in SEVERITY_NAMES else SEVERITY_NAMES.index(DEFAULT_MIN)


class Scope:
    def __init__(self, groupids: list[str] | None, missing: list[str], tags: list[dict], severities: list[int]):
        self.groupids = groupids
        self.missing = missing
        self.tags = tags
        self.severities = severities

    def params(self) -> dict:
        out: dict = {'source': 0, 'object': 0, 'recent': False, 'suppressed': False, 'severities': self.severities}
        if self.groupids is not None:
            out['groupids'] = self.groupids
        if self.tags:
            out.update(tags=self.tags, evaltype=TAGS_ANY)
        return out


def scope_of(z: Zabbix, config) -> Scope:
    wanted = names_of(config.get('host-groups') or '')
    groupids, missing = None, []
    if wanted:
        found = z.listed('hostgroup.get', {'output': ['groupid', 'name'], 'filter': {'name': wanted}})
        named = {str(g.get('name')): str(g.get('groupid')) for g in found if g.get('groupid') is not None}
        groupids = [named[n] for n in wanted if n in named]
        missing = [n for n in wanted if n not in named]
        if not groupids:
            raise ZabbixError(f"Zabbix has no host group named {', '.join(masked(n, HOST_LIMIT) for n in missing)}; check Host groups", answered=True)
    level = min_level(config.get('min-severity') or '')
    return Scope(groupids, missing, tag_filters(config.get('tags') or ''), list(range(level, len(SEVERITY_NAMES))))


def problems(z: Zabbix, scope: Scope) -> tuple[list[dict], bool]:
    params = scope.params() | {'output': ['eventid', 'objectid', 'name', 'severity', 'clock', 'acknowledged', 'opdata'], 'selectTags': ['tag', 'value'],
                               'sortfield': ['eventid'], 'sortorder': 'DESC', 'limit': MAX_PROBLEMS + 1}
    found = z.listed('problem.get', params)
    return [p for p in found[:MAX_PROBLEMS] if is_id(p.get('eventid')) and is_id(p.get('objectid'))], len(found) > MAX_PROBLEMS


def triggers(z: Zabbix, ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    found = z.listed('trigger.get', {'triggerids': ids, 'output': ['triggerid', 'description', 'priority'], 'selectHosts': ['hostid', 'host', 'name']})
    return {str(t.get('triggerid')): t for t in found if is_id(t.get('triggerid'))}


def host_of(trigger: dict | None) -> tuple[str, str]:
    hosts = [h for h in ((trigger or {}).get('hosts') or []) if isinstance(h, dict)]
    if not hosts:
        return '-', '-'
    first = sorted(hosts, key=lambda h: str(h.get('host') or ''))[0]
    tech = str(first.get('host') or first.get('name') or '-')
    return tech, str(first.get('name') or tech)


def tags_of(p: dict) -> list[tuple[str, str]]:
    out = []
    for t in p.get('tags') or []:
        if isinstance(t, dict) and t.get('tag'):
            out.append((clean(t.get('tag'), TAG_LIMIT), clean(t.get('value') or '', TAG_LIMIT)))
    return out[:MAX_TAGS]


def service_of(tags: list[tuple[str, str]]) -> str | None:
    for wanted in SERVICE_TAGS:
        for name, value in tags:
            if name.lower() == wanted and value and value != HELD:
                return value
    return None


def since_text(at: object) -> str:
    stamp = number(at, -1)
    if stamp <= 0:
        return ''
    try:
        return f' since {clock.date(stamp)} {clock.label(stamp)}'
    except (OverflowError, OSError, ValueError):
        return ''


def started_of(rows: list[dict]) -> str | None:
    starts = [number(p.get('clock'), -1) for p in rows]
    if not starts or min(starts) <= 0:
        return None
    try:
        return iso(max(starts))
    except (OverflowError, OSError, ValueError):
        return None


def severity_of(p: dict) -> int:
    level = number(p.get('severity'), 0)
    return level if 0 <= level < len(SEVERITY_NAMES) else 0


def card_of(rows: list[dict], trigger: dict | None) -> dict:
    top = sorted(rows, key=lambda p: (severity_of(p), number(p.get('eventid'))), reverse=True)[0]
    level = severity_of(top)
    sev = SEV_MAP[level]
    tech, visible = host_of(trigger)
    host_key = clean(tech, HOST_LIMIT)
    host = clean(visible, HOST_LIMIT)
    name = clean(top.get('name'), NAME_LIMIT)
    trigger_id = str(top.get('objectid'))
    described = clean((trigger or {}).get('description'), NAME_LIMIT)
    key_name = f'trigger {trigger_id}' if described in (HELD, '') else described
    key_host = '-' if host_key == HELD else host_key
    tags = tags_of(top)
    svc = service_of(tags) or (key_host if host_key != HELD else '-')
    acknowledged = any(str(p.get('acknowledged')) == '1' for p in rows)
    opdata = clean(top.get('opdata') or '', OPDATA_LIMIT)
    parts = [f'Zabbix problem {name} on {host} ({SEVERITY_LABELS[SEVERITY_NAMES[level]]}){since_text(top.get("clock"))}']
    if acknowledged:
        parts.append('acknowledged in Zabbix')
    if opdata:
        parts.append(f'operational data: {opdata}')
    if tags:
        parts.append('tags: ' + ', '.join(f'{k}={v}' if v else k for k, v in tags))
    if len(rows) > 1:
        parts.append(f'{len(rows)} open problems on this trigger')
    label = name[:LABEL_LIMIT]
    x = anomaly(PASS, SIG, '-', svc, key_name, sev, 1, None, None, '', label, '; '.join(parts) + '.')
    x.update(key=f'{SIG}|{key_host}|{key_name}|{trigger_id}', ranKey=PASS, triggerId=trigger_id, eventId=str(top.get('eventid') or ''), host=host,
             zabbixSeverity=SEVERITY_LABELS[SEVERITY_NAMES[level]], acknowledged=acknowledged, tags=[f'{k}={v}' if v else k for k, v in tags],
             short=f'{sev.upper()} Zabbix {label} on {host}')
    started = started_of(rows)
    if started:
        x['startedAt'] = started
    return x


def cards_of(rows: list[dict], found: dict[str, dict]) -> list[dict]:
    by_trigger: dict[str, list[dict]] = {}
    for p in rows:
        by_trigger.setdefault(str(p.get('objectid')), []).append(p)
    return [card_of(group, found.get(trigger_id)) for trigger_id, group in by_trigger.items()]


def sweep(z: Zabbix, config, now: float, last_status: str | None, instance: str) -> Sweep:
    try:
        z.connect()
        scope = scope_of(z, config)
        rows, capped = problems(z, scope)
        found = triggers(z, sorted({str(p.get('objectid')) for p in rows if p.get('objectid') is not None}))
    except ZabbixError as e:
        if access.denied(e.code):
            card = access.card(PASS, NAME, PART, PERMISSION, e.code)
            return Sweep(PassResult(PASS, True, access.status('ok', [PART]), [card], {}, (access.ran_key(PASS, PART),)))
        return Sweep(PassResult(PASS, False, f'error: {clean(e.reason, REASON_LIMIT)}'))
    cards = cards_of(rows, found)
    tails = []
    if scope.missing:
        tails.append('no host group named ' + ', '.join(masked(n, HOST_LIMIT) for n in scope.missing))
    if capped:
        tails.append(CAPPED)
        if CAPPED not in (last_status or ''):
            log.warning('a Zabbix pass read only the newest %d problems; older problems keep their cards and none clear until it is under the cap', MAX_PROBLEMS)
    status = DOT.join(['ok'] + tails)
    summary = {'part': 'summary', 'at': iso(now), 'version': masked(z.version, 40), 'problems': len(rows), 'cards': len(cards), 'capped': capped,
               'missingGroups': [masked(n, HOST_LIMIT) for n in scope.missing]}
    keys = ((NO_DATA,) if capped else (PASS,)) + (access.ran_key(PASS, PART),)
    return Sweep(PassResult(PASS, True, status, cards, {}, keys), sections={SECTION: [summary]})


def link_for(card: dict, url: str) -> list[dict] | None:
    if card.get('src') != PASS or card.get('sig') == access.SIG or not url:
        return None
    trigger = str(card.get('triggerId') or '')
    base = url.rstrip('/') + '/zabbix.php?action=problem.view'
    return [dict(label='Problems in Zabbix', url=base + (f'&triggerids%5B%5D={quote(trigger)}' if trigger.isdigit() else ''))]
