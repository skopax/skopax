from collections.abc import Mapping
from dataclasses import dataclass, field

from skopax import clock
from skopax.contract import Context, InstanceInfo, PassResult, Sweep, anomaly, iso
from skopax.kit.elastic import (Budget, Elastic, ElasticError, access_card, card_urls, carried, client_for, discover_for, epoch, fields_card, kql_text, no_access, refusal, refused_status,
                                sources_of)

PASS = 'csp'
RULES = {'new_min': 10, 'jump': 5.0, 'jump_min': 100, 'new_days': 7, 'quiet_hours': 24, 'forget_days': 30, 'history_hours': 168, 'min_history': 24}
HELP = {'new_min': 'A host the browser blocked for the first time gets a card once it has this many reports in the hour.',
        'jump': 'A card for all hosts together is raised when the hour\'s reports are this many times the average hour.',
        'jump_min': 'That jump is checked only when the hour has at least this many reports.',
        'new_days': 'A host counts as new for this many days after its first report.',
        'quiet_hours': 'A new-host card stays open until this many hours pass without a report for that host.',
        'forget_days': 'A host with no reports for this many days is forgotten and counts as new if it comes back.',
        'history_hours': 'How many hours of report counts the average hour is worked out from.',
        'min_history': 'The jump is checked only once this many earlier counts exist.'}
SECTION_HELP = {'csp': 'CSP violation reports of the last hour by blocked host, plus the known hosts and the hourly counts the check compares against'}
TOP = 5
HOSTS_KEEP = 500
HOST_MAX = 120
DIRECTIVE_MAX = 60
EXTENSIONS = ('chrome-extension', 'moz-extension', 'safari-extension', 'safari-web-extension')
FIELDS = '\n'.join([
    '| DISSECT message """%{}blockedURL\\":\\"%{b1}\\"%{}"""',
    '| DISSECT message """%{}blocked-uri\\":\\"%{b2}\\"%{}"""',
    '| DISSECT message """%{}effectiveDirective\\":\\"%{d1}\\"%{}"""',
    '| DISSECT message """%{}effective-directive\\":\\"%{d2}\\"%{}"""',
    '| DISSECT message """%{}sourceFile\\":\\"%{s1}\\"%{}"""',
    '| DISSECT message """%{}source-file\\":\\"%{s2}\\"%{}"""',
    '| EVAL blocked = COALESCE(b1, b2, ""), directive = COALESCE(d1, d2, "?"), source = COALESCE(s1, s2, "")',
    '| WHERE NOT (blocked LIKE "*-extension*" OR source LIKE "*-extension*")',
])
REPORTS_KQL = 'message:"blocked-uri" or message:"blockedURL"'
LINK_LABEL = 'CSP reports in Kibana'
NO_DATA = 'csp:no-data'
PART = 'reports'
FIELDS_KEY = 'csp:fields'
ACCESS = 'csp:access:reports'
QUIET = 'ok \u00b7 no data'
NO_MESSAGE = 'The CSP query returns no message column, which the CSP report fields are read from.'
NO_PEER = 'error: no Elasticsearch instance is chosen'


@dataclass(frozen=True)
class Target:
    query: str
    ns: str
    svc: str
    ui: Mapping[str, str] = field(default_factory=dict)


def target_of(config: Mapping[str, str], peer_config: Mapping[str, str]) -> Target:
    return Target(str(config.get('query') or '').strip(), str(config.get('namespace') or '').strip() or '-', str(config.get('service') or '').strip() or 'csp', peer_config)


def query(base: str, hours: float) -> str:
    return (f'{base}\n| WHERE @timestamp >= NOW() - {int(hours * 60)} minutes\n{FIELDS}\n'
            f'| STATS c = COUNT(*), newest = MAX(@timestamp) BY directive, blocked\n| SORT c DESC\n| LIMIT 1000')


def host_of(blocked: str) -> str:
    b = (blocked or '').strip()
    if '://' not in b:
        return b or 'inline'
    rest = b.split('://', 1)[1]
    return rest.split('/', 1)[0].split('?', 1)[0].removesuffix(':443') or b


def extension(row: dict) -> bool:
    return any(e in str(row.get('blocked') or '') for e in EXTENSIONS)


def count(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def grouped(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        if not isinstance(r, dict) or extension(r):
            continue
        directive = str(r.get('directive') or '?')[:DIRECTIVE_MAX]
        host = host_of(str(r.get('blocked') or ''))[:HOST_MAX]
        g = out.setdefault(f'{directive} {host}', {'directive': directive, 'host': host, 'c': 0, 'newest': ''})
        g['c'] += count(r.get('c'))
        g['newest'] = max(g['newest'], str(r.get('newest') or ''))
    return out


def seeded(rows: list[dict], now: float, rules: Mapping[str, float]) -> dict[str, dict]:
    old = iso(now - rules['new_days'] * 86400)
    return {name: {'first': old, 'last': g['newest'] or iso(now)} for name, g in grouped(rows).items()}


def at(value: object) -> float:
    found = epoch(value)
    return found if found is not None else 0.0


def remember(hosts: dict[str, dict], hour: dict[str, dict], flagged: set[str], now: float, rules: Mapping[str, float]) -> dict[str, dict]:
    out = {k: dict(v) for k, v in hosts.items()}
    for name in hour:
        out.setdefault(name, {'first': iso(now)})['last'] = iso(now)
    for name, v in out.items():
        if name in flagged:
            v.setdefault('flagged', iso(now))
        else:
            v.pop('flagged', None)
    cutoff = now - rules['forget_days'] * 86400
    kept = {k: v for k, v in out.items() if at(v.get('last')) >= cutoff}
    return dict(sorted(kept.items(), key=lambda kv: str(kv[1].get('last') or ''), reverse=True)[:HOSTS_KEEP])


def packed(hosts: dict[str, dict]) -> list[dict]:
    return [{'name': name, **{k: v[k] for k in ('first', 'last', 'flagged') if isinstance(v.get(k), str)}} for name, v in hosts.items()]


def unpacked(rows: object) -> dict[str, dict] | None:
    if not isinstance(rows, list):
        return None
    return {h['name']: {k: h[k] for k in ('first', 'last', 'flagged') if isinstance(h.get(k), str)} for h in rows if isinstance(h, dict) and isinstance(h.get('name'), str)}


def summary_of(rows: list | None) -> dict:
    return next((r for r in rows or [] if isinstance(r, dict) and r.get('part') == 'summary'), {})


def shown(groups: list[dict]) -> str:
    return '; '.join(f"{g['host']} ({g['directive']}) {g['c']}" for g in groups[:TOP])


def blocked_hosts(hour: dict[str, dict], hosts: dict[str, dict], now: float, rules: Mapping[str, float]) -> list[tuple[str, dict, str]]:
    young = now - rules['new_days'] * 86400
    quiet = now - rules['quiet_hours'] * 3600
    out = []
    for name in sorted(set(hour) | {k for k, v in hosts.items() if v.get('flagged')}, key=lambda k: -(hour.get(k) or {}).get('c', 0)):
        info = hosts.get(name) or {}
        directive, _, host = name.partition(' ')
        g = hour.get(name) or {'directive': directive, 'host': host, 'c': 0}
        if info.get('flagged'):
            if g['c'] or (info.get('last') and at(info['last']) >= quiet):
                out.append((name, g, info.get('first') or iso(now)))
        elif g['c'] >= rules['new_min'] and not (info.get('first') and at(info['first']) <= young):
            out.append((name, g, info.get('first') or iso(now)))
    return out


def since_text(value: str, now: float) -> str:
    t = epoch(value)
    t = now if t is None else t
    return f'{clock.date(t)} {clock.label(t)}'


def reports_link(target: Target, host: str | None) -> list[dict]:
    kql = f'({REPORTS_KQL}) and message:{kql_text(host)}' if host else REPORTS_KQL
    return card_urls(LINK_LABEL, discover_for(target.ui, kql, 'now-2h', columns=('message',)))


def evaluate(hour: dict[str, dict], hosts: dict[str, dict], history: list[dict], now: float, rules: Mapping[str, float], target: Target | None = None) -> tuple[list[dict], set[str]]:
    target = target or Target('', '-', 'csp')
    found, flagged = [], set()
    for name, g, since in blocked_hosts(hour, hosts, now, rules):
        flagged.add(name)
        what = (f"Browsers blocked {g['host']} under {g['directive']} {g['c']} times in the last hour, a host the CSP reports had not shown before {since_text(since, now)}. "
                'A new vendor script (often added through a tag manager), an injected script, or an allowance missing after a CSP change: check who loads it (sourceFile, the page) '
                f"before adding it to the policy. The card stays open until {rules['quiet_hours']:g} h pass without a report for this host.")
        x = anomaly(PASS, PASS, target.ns, target.svc, name, 'warn', g['c'], None, None, '', 'CSP blocks a new host', what, 1, f"WARN CSP blocks {g['host']} ({g['directive']}): {g['c']}/h")
        x.update(ranKey=PASS, host=g['host'], directive=g['directive'])
        urls = reports_link(target, g['host'])
        if urls:
            x['urls'] = urls
        found.append(x)
    total = sum(g['c'] for g in hour.values())
    past = [count(h.get('c')) for h in history if h.get('at') and at(h['at']) >= now - rules['history_hours'] * 3600]
    if past and len(past) >= rules['min_history']:
        avg = sum(past) / len(past)
        if total >= rules['jump_min'] and total >= rules['jump'] * max(avg, 1):
            top = sorted(hour.values(), key=lambda g: -g['c'])
            hours = rules['history_hours']
            what = (f'CSP violation reports jumped to {total} in the last hour against an average of {avg:.0f} an hour over the last {hours:g} h. Top blocked hosts: {shown(top)}. '
                    'A policy change that dropped a host, a new script on the site, or a browser change: compare the directives with the last change to the policy.')
            x = anomaly(PASS, PASS, target.ns, target.svc, 'reports', 'warn', total, None, round(avg, 1), '', 'CSP violation reports', what, 1, f'WARN CSP reports {total}/h ({hours:g} h avg {avg:.0f}/h)')
            x.update(ranKey=PASS)
            urls = reports_link(target, None)
            if urls:
                x['urls'] = urls
            found.append(x)
    return found, flagged


def kept_history(history: list[dict], now: float, rules: Mapping[str, float]) -> list[dict]:
    since = now - rules['history_hours'] * 3600
    return [h for h in history if at(h.get('at')) >= since][-max(1, int(rules['history_hours'] * 4) + 1):]


def shown_sources(target: Target) -> str:
    return ', '.join(sources_of(target.query)) or 'the indices of the CSP query'


def run_pass(es: Elastic, now: float, previous: list | None, rules: Mapping[str, float], target: Target, budget: Budget | None = None) -> Sweep:
    budget = budget or Budget()
    summary = summary_of(previous)
    try:
        hosts = unpacked(summary.get('hosts'))
        if hosts is None:
            hosts = seeded(es.query(query(target.query, rules['new_days'] * 24), timeout=budget.left()), now, rules)
        hour = grouped(es.query(query(target.query, 1), timeout=budget.left()))
    except ElasticError as e:
        if refusal(es, e, sources_of(target.query), budget):
            return no_access(PASS, access_card(PASS, 'CSP reports', PART, shown_sources(target), refused_status(e), bool(es.api_key)))
        if e.missing:
            return Sweep(PassResult(PASS, True, QUIET, [], {}, (NO_DATA, FIELDS_KEY, ACCESS)))
        if 'message' in e.unknown:
            card = fields_card(PASS, PASS, 'CSP report', target.ns, target.svc, ('message',), shown_sources(target))
            return Sweep(PassResult(PASS, True, QUIET, [card], {}, (NO_DATA, FIELDS_KEY, ACCESS)))
        return Sweep(PassResult(PASS, False, f'error: {e.reason}'))
    history = [h for h in summary.get('history') or [] if isinstance(h, dict)]
    found, flagged = evaluate(hour, hosts, history, now, rules, target)
    total = sum(g['c'] for g in hour.values())
    history = kept_history(history + [{'at': iso(now), 'c': total}], now, rules)
    top = [{'part': 'top', 'directive': g['directive'], 'host': g['host'], 'c': g['c']} for g in sorted(hour.values(), key=lambda g: -g['c'])[:TOP]]
    kept = {'part': 'summary', 'at': iso(now), 'total': total, 'hosts': packed(remember(hosts, hour, flagged, now, rules)), 'history': history}
    return Sweep(PassResult(PASS, True, 'ok', found, {}, (PASS, FIELDS_KEY, ACCESS)), sections={'csp': top + [kept]})


def run_csp(ctx: Context) -> Sweep:
    peer = ctx.peer('elasticsearch')
    if peer is None:
        return Sweep(PassResult(PASS, False, NO_PEER))
    target = target_of(ctx.instance.config, peer.info.config)
    if not target.query:
        return Sweep(PassResult(PASS, False, 'error: the CSP query is empty'))
    return run_pass(client_for(peer.info, peer.http()), ctx.now, ctx.previous('csp'), ctx.rules('rules'), target)


def links(card: dict, info: InstanceInfo) -> list[dict] | None:
    return carried(card, PASS)
