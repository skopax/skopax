import dataclasses
import json
import logging
import time
from urllib.parse import quote, urlsplit

from skopax.contract import Sweep, iso
from skopax.errkind import kind as error_kind
from skopax.kit import access
from skopax.plugins.zabbix.tools import AVAILABLE, ERROR_LIMIT, INTERFACE_TYPES
from skopax.plugins.zabbix.zabbix import (HOST_LIMIT, MAX_PROBLEMS, NAME_LIMIT, REASON_LIMIT, SECTION, SEV_MAP, SEVERITY_LABELS, SEVERITY_NAMES, Zabbix, ZabbixError, clean, host_of, is_id,
                                          min_level, names_of, number, problems, scope_of, severity_of, triggers)

PAGE = 'zabbixPage'
PAGE_HELP = 'the Zabbix page: hosts of the watched host groups with availability, maintenance and open problems by severity, and the open problems themselves'
MAX_HOSTS = 300
MAX_ROWS = 200
MAX_HOST_GROUPS = 10
MAX_INTERFACES = 4
GROUP_LIMIT = 80
START_BUDGET_S = 120
MAX_BYTES = 200 * 1024
HIGH = SEVERITY_NAMES.index('high')
FAILED = ('error', 'denied', 'late', 'broken')

log = logging.getLogger('skopax.plugins.zabbix')


def _list(value: object) -> list[dict]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _at(stamp: object) -> str | None:
    value = number(stamp, 0)
    if value <= 0:
        return None
    try:
        return iso(value)
    except (OverflowError, OSError, ValueError):
        return None


def _base(url: str) -> str:
    try:
        parts = urlsplit((url or '').strip())
    except ValueError:
        return ''
    host = parts.netloc.rpartition('@')[2]
    if parts.scheme != 'https' or not host or not parts.hostname:
        return ''
    return f"https://{host}{parts.path.rstrip('/')}"


def _event_link(url: str, trigger: str, event: str) -> str:
    base = _base(url)
    if not base or not trigger.isdigit() or not event.isdigit():
        return ''
    return f'{base}/tr_events.php?triggerid={quote(trigger)}&eventid={quote(event)}'


def _interface(i: dict) -> dict:
    out = {'type': INTERFACE_TYPES.get(str(i.get('type')), 'other'), 'available': AVAILABLE.get(str(i.get('available')), 'unknown'), 'main': str(i.get('main')) == '1'}
    error = clean(i.get('error') or '', ERROR_LIMIT)
    if error:
        out['error'] = error
    return out


def _groups(h: dict, selector: str, watched: set[str] | None) -> list[dict]:
    listed = h.get('hostgroups') if selector == 'selectHostGroups' else h.get('groups')
    found = [g for g in _list(listed) if is_id(g.get('groupid'))]
    if watched is not None:
        found = [g for g in found if str(g.get('groupid')) in watched]
    return [{'id': str(g.get('groupid')), 'name': clean(g.get('name'), GROUP_LIMIT)} for g in found[:MAX_HOST_GROUPS]]


def _hosts(z: Zabbix, groupids: list[str] | None) -> tuple[list[dict], bool, str]:
    selector = z.group_selector()
    params = {'output': ['hostid', 'host', 'name', 'maintenance_status'], selector: ['groupid', 'name'], 'selectInterfaces': ['type', 'available', 'error', 'main'],
              'monitored_hosts': True, 'sortfield': 'name', 'limit': MAX_HOSTS + 1}
    if groupids is not None:
        params['groupids'] = groupids
    found = [h for h in z.listed('host.get', params) if is_id(h.get('hostid'))]
    return found[:MAX_HOSTS], len(found) > MAX_HOSTS, selector


def _problem_rows(rows: list[dict], found: dict[str, dict], url: str) -> tuple[list[dict], dict[str, dict[str, int]]]:
    out, counts = [], {}
    for p in rows:
        trigger_id, event_id = str(p.get('objectid')), str(p.get('eventid'))
        trigger = found.get(trigger_id)
        level = severity_of(p)
        label = SEVERITY_LABELS[SEVERITY_NAMES[level]]
        for h in _list((trigger or {}).get('hosts')):
            if is_id(h.get('hostid')):
                seen = counts.setdefault(str(h.get('hostid')), {})
                seen[label] = seen.get(label, 0) + 1
        tech, visible = host_of(trigger)
        out.append({'part': 'problem', 'eventId': event_id, 'triggerId': trigger_id, 'name': clean(p.get('name'), NAME_LIMIT), 'level': level, 'severity': label,
                    'sev': SEV_MAP[level], 'host': clean(visible, HOST_LIMIT), 'hostTech': clean(tech, HOST_LIMIT), 'at': _at(p.get('clock')),
                    'acknowledged': str(p.get('acknowledged')) == '1', 'link': _event_link(url, trigger_id, event_id)})
    out.sort(key=lambda r: (-r['level'], -number(r['eventId'])))
    return out, counts


def rows_of(z: Zabbix, config, now: float, instance: str) -> list[dict]:
    summary: dict = {'part': 'summary', 'at': iso(now), 'instance': instance, 'minSeverity': SEVERITY_LABELS[SEVERITY_NAMES[min_level(config.get('min-severity') or '')]]}
    try:
        return _read(z, config, summary)
    except ZabbixError as e:
        if access.denied(e.code):
            return [summary | {'denied': True}]
        return [summary | {'error': clean(e.reason, REASON_LIMIT)}]
    except Exception as e:
        log.warning('the Zabbix page rows were not built, the last ones stay (%s)', error_kind(e))
        return [summary | {'broken': True}]


def _read(z: Zabbix, config, summary: dict) -> list[dict]:
    scope = scope_of(z, config)
    hosts, hosts_capped, selector = _hosts(z, scope.groupids)
    listed, problems_capped = problems(z, scope)
    found = triggers(z, sorted({str(p.get('objectid')) for p in listed}))
    watched = set(scope.groupids) if scope.groupids is not None else None
    shown, counts = _problem_rows(listed, found, config.get('url') or '')
    out = []
    for h in hosts:
        host_id = str(h.get('hostid'))
        tech = clean(h.get('host'), HOST_LIMIT)
        out.append({'part': 'host', 'hostId': host_id, 'host': tech, 'name': clean(h.get('name') or h.get('host'), HOST_LIMIT), 'groups': _groups(h, selector, watched),
                    'maintenance': str(h.get('maintenance_status')) == '1', 'problems': counts.get(host_id, {}),
                    'interfaces': [_interface(i) for i in _list(h.get('interfaces'))[:MAX_INTERFACES]]})
    summary.update(hosts=len(hosts), hostsCapped=hosts_capped, problems=len(listed), problemsCapped=problems_capped, rowsCapped=len(shown) > MAX_ROWS, maxRows=MAX_ROWS, maxProblems=MAX_PROBLEMS,
                   maxHosts=MAX_HOSTS, watchedGroups=[clean(n, GROUP_LIMIT) for n in names_of(config.get('host-groups') or '') if n not in scope.missing] if watched is not None else [])
    return out + shown[:MAX_ROWS] + [summary]


def _kept(previous: object, summary: dict) -> list[dict]:
    old = _list(previous)
    kept = [{k: v for k, v in r.items() if k not in ('plugin', 'cluster')} for r in old if r.get('part') in ('host', 'problem')]
    if not kept:
        return [summary]
    last = next((r for r in old if r.get('part') == 'summary'), {})
    carried = {k: v for k, v in last.items() if k not in FAILED + ('at', 'rowsAt', 'plugin', 'cluster')}
    return kept + [carried | summary | {'rowsAt': last.get('rowsAt') or last.get('at')}]


def _size(row: dict) -> int:
    return len(json.dumps(row, separators=(',', ':'), ensure_ascii=False).encode()) + 1


def fit(rows: list[dict], limit: int = MAX_BYTES) -> list[dict]:
    body, summary = rows[:-1], dict(rows[-1])
    total = sum(_size(r) for r in rows)
    if total <= limit:
        return rows
    back = range(len(body) - 1, -1, -1)
    low = [i for i in back if body[i].get('part') == 'problem' and number(body[i].get('level'), 0) < HIGH]
    high = [i for i in back if body[i].get('part') == 'problem' and number(body[i].get('level'), 0) >= HIGH]
    order = low + [i for i in back if body[i].get('part') != 'problem'] + high
    dropped = set()
    for i in order:
        if total <= limit:
            break
        total -= _size(body[i])
        dropped.add(i)
    summary['trimmed'] = True
    return [r for i, r in enumerate(body) if i not in dropped] + [summary]


def extend(got: Sweep, z: Zabbix, config, now: float, instance: str, started: float | None = None, previous: object = None) -> Sweep:
    if SECTION not in got.sections:
        return got
    if started is not None and time.monotonic() - started > START_BUDGET_S:
        rows = [{'part': 'summary', 'at': iso(now), 'instance': instance, 'late': True}]
    else:
        rows = rows_of(z, config, now, instance)
    if any(rows[-1].get(k) for k in FAILED):
        rows = _kept(previous, rows[-1])
    return dataclasses.replace(got, sections=got.sections | {PAGE: fit(rows)})
