import datetime as dt
import re
from dataclasses import dataclass, field

from skopax import clock
from skopax.contract import anomaly, fmt, iso
from skopax.kit import access
from skopax.plugins.cloudflare.client import PERMISSIONS, SLUGS, Cloudflare, CloudflareError, Zone, traffic

CERT_WARN_DAYS = 14
CERT_CRIT_DAYS = 3
EDGE_CONN = frozenset({502}) | frozenset(range(520, 527))
EDGE_RULE = dict(min_req=5000, conn_crit_n=200, conn_crit_pct=0.5, conn_warn_n=50, conn_warn_pct=0.05, conn_info_n=20, conn_info_pct=0.02,
                 srv_crit_n=500, srv_crit_pct=1.0, srv_warn_n=200, srv_warn_pct=0.2, srv_info_n=50, srv_info_pct=0.05)
EDGE_HELP = {
    'min_req': 'A zone is judged only when its hour has at least this many requests; a share of a handful of requests says nothing.',
    'conn_crit_n': 'Critical when at least this many requests in the hour end 502 or 520-526 at the edge (Cloudflare could not reach or get an answer from the origin).',
    'conn_crit_pct': 'And those requests are at least this percentage of all requests of the zone (0.5 = 0.5 %).',
    'conn_warn_n': 'High when at least this many requests in the hour end 502 or 520-526 at the edge.',
    'conn_warn_pct': 'And those requests are at least this percentage of all requests of the zone.',
    'conn_info_n': 'Info when at least this many requests in the hour end 502 or 520-526 at the edge.',
    'conn_info_pct': 'And those requests are at least this percentage of all requests of the zone.',
    'srv_crit_n': 'Critical when at least this many requests in the hour end with another 5xx at the edge (500, 503, 504, 530).',
    'srv_crit_pct': 'And those requests are at least this percentage of all requests of the zone (1 = 1 %).',
    'srv_warn_n': 'High when at least this many requests in the hour end with another 5xx at the edge.',
    'srv_warn_pct': 'And those requests are at least this percentage of all requests of the zone.',
    'srv_info_n': 'Info when at least this many requests in the hour end with another 5xx at the edge.',
    'srv_info_pct': 'And those requests are at least this percentage of all requests of the zone.',
}
EDGE_KIND = dict(conn=('edge-conn', '502 / 52x share at the edge', '502 / 52x'), srv=('edge-5xx', 'other 5xx share at the edge', 'other 5xx'))
EDGE_1H = '''query($z:[String!],$a:Time!,$b:Time!){ viewer { zones(filter:{zoneTag_in:$z}) { zoneTag
  httpRequests1hGroups(limit:100, orderBy:[datetime_ASC], filter:{datetime_geq:$a, datetime_lt:$b}) {
    dimensions { datetime } sum { requests cachedRequests threats responseStatusMap { edgeResponseStatus requests } } uniq { uniques } } } } }'''
EDGE_7D = '''query($z:[String!],$a:Time!,$b:Time!){ viewer { zones(filter:{zoneTag_in:$z}) { zoneTag
  httpRequestsAdaptiveGroups(limit:300, filter:{datetime_geq:$a, datetime_lt:$b}) { count dimensions { datetimeHour edgeResponseStatus } } } } }'''
EDGE_DETAIL = '''query($z:[String!],$a:Time!,$b:Time!){ viewer { zones(filter:{zoneTag_in:$z}) { zoneTag
  split: httpRequestsAdaptiveGroups(limit:500, filter:{datetime_geq:$a, datetime_lt:$b, edgeResponseStatus_geq:500}) { count dimensions { datetimeHour edgeResponseStatus originResponseStatus } }
  top: httpRequestsAdaptiveGroups(limit:50, orderBy:[count_DESC], filter:{datetime_geq:$a, datetime_lt:$b, edgeResponseStatus_geq:500}) {
    count dimensions { datetimeHour edgeResponseStatus originResponseStatus clientRequestHTTPHost clientRequestPath userAgent } } } } }'''
EDGE_TOP = 2
EDGE_PROBE = '''query($z:[String!],$a:Time!,$b:Time!){ viewer { zones(filter:{zoneTag_in:$z}) { zoneTag
  httpRequests1hGroups(limit:1, filter:{datetime_geq:$a, datetime_lt:$b}) { dimensions { datetime } } } } }'''
LOCATION = re.compile(r'\([A-Z]{3}\)\s*$')
COMPONENTS = re.compile(r'[;\n]')
MID = ' \u00b7 '
TIMES = '\u00d7'
NO_DATA = 'ok \u00b7 no data'
PACK_ID = re.compile(r'[A-Za-z0-9-]{1,64}')
ACCESS_PARTS = {'cf': ('zones', 'health checks', 'certificates'), 'edge': ('zones', 'analytics')}


@dataclass
class Pass:
    found: list[dict] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    ran: list[str] = field(default_factory=list)
    read: dict[str, bool] = field(default_factory=dict)
    denied: dict[str, list[str]] = field(default_factory=dict)
    bare: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    why: dict[str, str] = field(default_factory=dict)
    invalid: dict[str, bool] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    reached: list[str] = field(default_factory=list)

    def deny(self, part: str, why: str, zone: str | None = None, invalid: bool = False) -> None:
        self.denied.setdefault(part, [])
        self.why.setdefault(part, why)
        self.invalid[part] = self.invalid.get(part, False) or invalid
        self.reach(part)
        if zone:
            self.denied[part].append(zone)

    def fail(self, part: str, reason: str) -> None:
        self.errors.setdefault(part, reason)

    def reach(self, part: str) -> None:
        if part not in self.reached:
            self.reached.append(part)

    def access(self, check: str) -> list[dict]:
        return [access_card(check, part, zones, self.why[part], self.invalid.get(part, False)) for part, zones in self.denied.items()]

    def no_access(self) -> list[dict]:
        return [dict(part=part, permission=PERMISSIONS[part], zones=zones, invalid=self.invalid.get(part, False)) for part, zones in self.denied.items()]


def components_of(text: str | None) -> list[str]:
    return list(dict.fromkeys(c.strip() for c in COMPONENTS.split(text or '') if c.strip()))


def parse_time(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()


def access_keys(check: str, reached: list[str]) -> list[str]:
    return [f'{check}:access:{SLUGS[part]}' for part in ACCESS_PARTS[check] if part in reached]


def access_card(check: str, part: str, zones: list[str], why: str, invalid: bool = False) -> dict:
    where = f" of {', '.join(zones)}" if zones else ''
    what = (f'Cloudflare refused to read the {part}{where} ({why}). Until the token gets {PERMISSIONS[part]}, '
            f'the cards of the {part} it cannot read stay as they are and are not checked again.')
    return access.card(check, 'Cloudflare', SLUGS[part], PERMISSIONS[part], access.INVALID if invalid else access.FORBIDDEN, name=part, what=what)


def outcome(check: str, got: Pass, ran: list[str], unseen: list[str]) -> tuple[str, tuple[str, ...]]:
    empty = [name for name, has in got.read.items() if not has and name != 'status'] + unseen
    keys = list(ran)
    if got.read and not any(got.read.values()) and not unseen and not got.errors:
        base = NO_DATA
        keys.append(f'{check}:no-data')
    elif empty:
        base = f"{NO_DATA} for {', '.join(empty)}"
    else:
        base = 'ok'
    if got.errors:
        base = 'error: ' + '; '.join(f'{part}: {why}' for part, why in got.errors.items()) + (base.removeprefix('ok') if base != 'ok' else '')
    return access.status(base, got.denied), tuple(dict.fromkeys(keys + access_keys(check, got.reached)))


def _health(zone: Zone, h: dict) -> tuple[dict, dict | None]:
    config = h.get('http_config') if isinstance(h.get('http_config'), dict) else {}
    path = str(config.get('path') or '')
    name, status = str(h.get('name') or '?'), h.get('status')
    row = dict(part='checks', zone=zone.name, name=name, path=path, status=status, reason=h.get('failure_reason'))
    if h.get('suspended') or status == 'healthy':
        return row, None
    sev = 'crit' if status == 'unhealthy' else 'warn'
    x = anomaly('cf', 'cf-health', 'cloudflare', name, zone.name, sev, 1, None, None, '', 'health check',
                f"Cloudflare health check {name} ({h.get('address')}{path}) is {status}: {h.get('failure_reason')}")
    x['ranKey'] = f'cf:{zone.name}:checks'
    return row, x


def _expiry(p: dict) -> float | None:
    found = []
    for c in p.get('certificates') or []:
        try:
            found.append(parse_time(c['expires_on']))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return min(found) if found else None


def _keyed(x: dict, zone: Zone, pack: str) -> dict:
    x['key'] = f'{x["key"]}|{pack}'
    x['ranKey'] = f'cf:{zone.name}:certs'
    return x


def _cert(zone: Zone, p: dict, now: float, index: int) -> tuple[dict, dict | None]:
    kind = str(p.get('type') or '?')
    pack = p['id'] if isinstance(p.get('id'), str) and PACK_ID.fullmatch(p['id']) else f'pack-{index}'
    exp = _expiry(p)
    row = dict(part='certs', zone=zone.name, id=pack, type=kind, status=p.get('status'), expires=iso(exp) if exp is not None else None)
    name = f'{kind} certificate pack of {zone.name}'
    days = (exp - now) / 86400 if exp is not None else None
    if p.get('status') != 'active':
        hosts = [str(h) for h in p.get('hosts') or []][:3]
        return row, _keyed(anomaly('cf', 'cf-cert', 'cloudflare', kind, zone.name, 'warn', 0, None, None, '', 'certificate pack',
                                   f"{name} ({', '.join(hosts)}) has status {p.get('status')}"), zone, pack)
    if days is not None and days < CERT_WARN_DAYS:
        sev = 'crit' if days < CERT_CRIT_DAYS else 'warn'
        return row, _keyed(anomaly('cf', 'cf-cert', 'cloudflare', kind, zone.name, sev, round(days, 1), None, None, '', 'days to certificate expiry',
                                   f'{name} expires in {days:.1f} days', 1, f'{sev.upper()} Cloudflare {name} expires in {days:.0f} days'), zone, pack)
    return row, None


def _status(s: dict, watched: list[str]) -> tuple[list[dict], list[dict], list[str]]:
    found, rows, seen = [], [], set()
    for c in s.get('components') or []:
        if not isinstance(c, dict) or not isinstance(c.get('name'), str) or c['name'] not in watched:
            continue
        name, status = c['name'], str(c.get('status') or '')
        seen.add(name)
        rows.append(dict(part='components', name=name, status=status))
        if status == 'operational':
            continue
        sev = 'warn' if status in ('major_outage', 'partial_outage') else 'info'
        what = f"Cloudflare reports {name} as {status.replace('_', ' ')}" + ('; traffic through that data center is re-routed or degraded' if LOCATION.search(name) else '')
        x = anomaly('cf', 'cf-status', 'cloudflare', name, '', sev, 1, None, None, '', 'Cloudflare status', what)
        x['ranKey'] = 'cf:status'
        found.append(x)
    for i in s.get('incidents') or []:
        if not isinstance(i, dict):
            continue
        names = {c['name'] for c in i.get('components') or [] if isinstance(c, dict) and isinstance(c.get('name'), str)} & set(watched)
        if names:
            title = str(i.get('name') or '?')
            x = anomaly('cf', 'cf-incident', 'cloudflare', title[:60], '', 'warn', 1, None, None, '', 'Cloudflare incident',
                        f"Cloudflare incident ({i.get('status')}, {i.get('impact')}) on {', '.join(sorted(names))}: {title}")
            x['key'] = f"cf-incident|{i.get('id')}"
            x['ranKey'] = 'cf:status'
            found.append(x)
    return found, rows, [n for n in watched if n not in seen]


def _listing(client: Cloudflare, out: Pass, zone: Zone, part: str, path: str, none_when_unavailable: bool = False) -> list[dict] | None:
    try:
        found = client.listing(path)
    except CloudflareError as e:
        if e.denied:
            out.deny(part, e.reason, zone.name, e.invalid)
            return None
        if none_when_unavailable and e.unavailable():
            out.bare.append(zone.name)
            out.reach(part)
            return []
        out.fail(part, e.reason)
        return None
    out.reach(part)
    return found


def cf_pass(client: Cloudflare, zones: list[Zone], components: list[str], now: float, out: Pass | None = None) -> Pass:
    out = out or Pass()
    for zone in zones:
        listed = _listing(client, out, zone, 'health checks', f'/zones/{zone.id}/healthchecks', none_when_unavailable=True)
        if listed is not None:
            out.read['health checks'] = True
            out.ran.append(f'cf:{zone.name}:checks')
            for h in listed:
                row, card = _health(zone, h)
                out.rows.append(row)
                out.found += [card] if card else []
        packs = _listing(client, out, zone, 'certificates', f'/zones/{zone.id}/ssl/certificate_packs')
        if packs is not None:
            out.read['certificates'] = True
            out.ran.append(f'cf:{zone.name}:certs')
            for index, p in enumerate(packs):
                row, card = _cert(zone, p, now, index)
                out.rows.append(row)
                out.found += [card] if card else []
    if components:
        try:
            page = client.status()
        except CloudflareError as e:
            out.fail('status', e.reason)
            return out
        cards, status_rows, out.absent = _status(page, components)
        out.found += cards
        out.rows += status_rows
        out.read['status'] = bool(status_rows)
        if status_rows:
            out.ran.append('cf:status')
    else:
        out.ran.append('cf:status')
    return out


def edge_hour(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).replace(minute=0, second=0, microsecond=0)


def edge_t(d: dt.datetime) -> str:
    return d.strftime('%Y-%m-%dT%H:%M:%SZ')


def edge_pass(client: Cloudflare, zones: list[Zone], now: float) -> dict:
    h = edge_hour(now)
    tags = [z.id for z in zones]
    hours = {z.get('zoneTag'): z.get('httpRequests1hGroups') or [] for z in
             client.graphql(EDGE_1H, z=tags, a=edge_t(h - dt.timedelta(days=1, hours=1)), b=edge_t(h + dt.timedelta(hours=1)))}
    week = dict(rows={}, error=None)
    try:
        for z in client.graphql(EDGE_7D, z=tags, a=edge_t(h - dt.timedelta(days=7, hours=1)), b=edge_t(h - dt.timedelta(days=6, hours=23))):
            agg = {}
            for r in z.get('httpRequestsAdaptiveGroups') or []:
                d = r['dimensions']
                a = agg.setdefault(d['datetimeHour'], dict(req=0, codes={}))
                a['req'] += r['count']
                if d['edgeResponseStatus'] >= 500:
                    a['codes'][d['edgeResponseStatus']] = a['codes'].get(d['edgeResponseStatus'], 0) + r['count']
            week['rows'][z.get('zoneTag')] = agg
    except CloudflareError as e:
        week['error'] = e.reason
    detail = dict(rows={}, error=None)
    try:
        for z in client.graphql(EDGE_DETAIL, z=tags, a=edge_t(h - dt.timedelta(hours=1)), b=edge_t(h + dt.timedelta(hours=1))):
            detail['rows'][z.get('zoneTag')] = dict(split=z.get('split') or [], top=z.get('top') or [])
    except CloudflareError as e:
        detail['error'] = e.reason
    return dict(hour=edge_t(h), hours=hours, week=week, detail=detail)


def probe_analytics(client: Cloudflare, zones: list[Zone], now: float) -> None:
    h = edge_hour(now)
    client.graphql(EDGE_PROBE, z=[z.id for z in zones], a=edge_t(h - dt.timedelta(hours=1)), b=edge_t(h))


def edge_codes(bucket: dict) -> dict:
    return {s['edgeResponseStatus']: s['requests'] for s in (bucket['sum']['responseStatusMap'] or [])}


def edge_split(codes: dict) -> tuple[int, int]:
    conn = sum(v for k, v in codes.items() if int(k) in EDGE_CONN)
    return conn, sum(v for k, v in codes.items() if 500 <= int(k) < 600) - conn


def edge_share(n: int, req: int | None) -> float | None:
    return None if not req else round(n / req * 100, 5)


def edge_sev(kind: str, n: int, req: int | None, rules: dict) -> str | None:
    if not req or req < rules['min_req']:
        return None
    for sev in ('crit', 'warn', 'info'):
        if n >= rules[f'{kind}_{sev}_n'] and n / req * 100 >= rules[f'{kind}_{sev}_pct']:
            return 'high' if sev == 'warn' else sev
    return None


def edge_mine(x: dict, at: str, kind: str) -> bool:
    d = x['dimensions']
    return d['datetimeHour'] == at and (int(d['edgeResponseStatus']) in EDGE_CONN) == (kind == 'conn')


def edge_origin(detail: dict | None, tag: str, at: str, kind: str) -> dict | None:
    d = ((detail or {}).get('rows') or {}).get(tag)
    split = [x for x in (d or {}).get('split') or [] if edge_mine(x, at, kind)]
    total = sum(x['count'] for x in split)
    if not total:
        return None
    origin = sum(x['count'] for x in split if x['dimensions']['originResponseStatus'] == x['dimensions']['edgeResponseStatus'])
    top = sorted((x for x in d.get('top') or [] if edge_mine(x, at, kind)), key=lambda x: -x['count'])[:EDGE_TOP]
    return dict(total=total, origin=origin, pct=round(origin / total * 100), top=top)


def edge_why(kind: str, o: dict | None) -> str:
    if not o:
        return ('502 can come from our origin (Cloudflare passes it on, so it is in our ingress logs too) or from Cloudflare itself; '
                '520\u2013526 are Cloudflare failing to reach or get a timely answer from our origin, which never shows in our ingress logs.'
                if kind == 'conn' else 'These are 5xx the edge served that are not connectivity (500, 503, 504, 530); an app 500 that reaches the origin is also counted at ingress.')
    if o['origin'] * 2 >= o['total']:
        why = f"Origin status (sampled): {o['pct']} % were sent by our origin itself and Cloudflare passed them on, so they are in our ingress logs too; look there first."
    else:
        why = (f"Origin status (sampled): {100 - o['pct']} % were made at the Cloudflare edge with no answer from our origin, so those are not in our ingress logs"
               + (f"; the other {o['pct']} % came from our origin." if o['origin'] else '.'))
    top = '; '.join(f"{x['count']:,} {TIMES} {x['dimensions']['edgeResponseStatus']} {x['dimensions']['clientRequestHTTPHost']}{x['dimensions']['clientRequestPath'][:80]}"
                    f"{MID}{(x['dimensions']['userAgent'] or 'no user agent')[:60]}" for x in o['top'])
    return why + (f' Top (sampled): {top}.' if top else '')


def edge_item(name: str, kind: str, sev: str, share: float, d1: float | None, d7: float | None, what: str, short: str, link: str | None) -> dict:
    sig = EDGE_KIND[kind][0]
    x = anomaly('cf', sig, 'cloudflare', name, '', sev, share, d1, d7, '%', EDGE_KIND[kind][1], what, 0.05, short)
    x['key'] = f'edge|{name}|{sig}'
    x['ranKey'] = f'edge:{name}'
    if link:
        x['urls'] = [dict(label=f'Cloudflare traffic analytics for {name}', url=link)]
    return x


def _window(at: str, running: bool) -> str:
    t = parse_time(at)
    return f'the {clock.hm(t)} {clock.label(t)} hour' + (' (still filling; Cloudflare publishes the running hour a few minutes behind)' if running else ' (complete)')


def evaluate_edge(data: dict, zones: list[Zone], now: float, rules: dict, account: str = '') -> tuple[list[dict], list[dict]]:
    found, rows = [], []
    week = (data.get('week') or {}).get('rows') or {}
    cur_h = parse_time(data['hour'])
    for zone in zones:
        tag, name = zone.id, zone.name
        buckets = {b['dimensions']['datetime']: b for b in (data['hours'].get(tag) or [])}
        prev_iso = edge_t(edge_hour(cur_h) - dt.timedelta(hours=1))
        cur, prev = buckets.get(data['hour']), buckets.get(prev_iso)
        thin = not cur or cur['sum']['requests'] < rules['min_req']
        use_prev = bool(prev) and (not cur or (thin and prev['sum']['requests'] >= rules['min_req']))
        b, at = (prev, prev_iso) if use_prev else (cur, data['hour'])
        link = traffic(account or zone.account, name)
        r = dict(zone=name, tag=tag, at=at, running=not use_prev, mins=round((now - cur_h) / 60) if not use_prev else 60)
        if link:
            r['link'] = link
        rows.append(r)
        if not b:
            r.update(req=None, evaluated=False)
            continue
        req, cached = b['sum']['requests'], b['sum']['cachedRequests']
        codes = edge_codes(b)
        conn, srv = edge_split(codes)
        at_1d = buckets.get(edge_t(edge_hour(parse_time(at)) - dt.timedelta(days=1)))
        w = (week.get(tag) or {}).get(edge_t(edge_hour(parse_time(at)) - dt.timedelta(days=7)))
        base = {}
        for lab, src in (('d1', at_1d), ('d7', w)):
            if not src:
                base[lab] = dict(req=None, conn=None, srv=None)
                continue
            c = edge_codes(src) if lab == 'd1' else {int(k): v for k, v in src['codes'].items()}
            q = src['sum']['requests'] if lab == 'd1' else src['req']
            cn, sv = edge_split(c)
            base[lab] = dict(req=q, conn=edge_share(cn, q), srv=edge_share(sv, q), conn_n=cn, srv_n=sv)
        r.update(req=req, cached=cached, cachedPct=edge_share(cached, req), uniq=(b.get('uniq') or {}).get('uniques'),
                 conn=conn, srv=srv, connShare=edge_share(conn, req), srvShare=edge_share(srv, req),
                 codes={str(k): v for k, v in sorted(codes.items()) if k >= 500},
                 d1Req=base['d1']['req'], d7Req=base['d7']['req'],
                 connD1=base['d1']['conn'], connD7=base['d7']['conn'], srvD1=base['d1']['srv'], srvD7=base['d7']['srv'],
                 evaluated=req >= rules['min_req'], weekError=(data.get('week') or {}).get('error'))
        for kind, n in (('conn', conn), ('srv', srv)):
            sev = edge_sev(kind, n, req, rules)
            if not sev:
                continue
            share, lab = edge_share(n, req), EDGE_KIND[kind][2]
            codes_txt = ', '.join(f'{k} {TIMES} {v:,}' for k, v in sorted(codes.items())
                                  if (int(k) in EDGE_CONN) == (kind == 'conn') and 500 <= int(k) < 600)
            o = edge_origin(data.get('detail'), tag, at, kind)
            trend = MID.join(x for x in (
                f"1 d {fmt(base['d1'][kind], '%')}" if base['d1'][kind] is not None else '',
                f"7 d {fmt(base['d7'][kind], '%')} (sampled dataset)" if base['d7'][kind] is not None else '') if x)
            found.append(edge_item(name, kind, sev, share, base['d1'][kind], base['d7'][kind],
                                   f"{name}: {n:,} of {req:,} edge requests in {_window(at, r['running'])} ended {lab} at Cloudflare \u2014 {fmt(share, '%')} of requests"
                                   + (f' ({trend})' if trend else '') + f'. Codes: {codes_txt}. {edge_why(kind, o)}',
                                   f"{sev.upper()} Cloudflare {name} {lab} {fmt(share, '%')} of edge requests ({n:,} in the hour"
                                   + (f", {o['pct']} % from our origin)" if o else ')'), link))
    rows.sort(key=lambda r: -(r.get('req') or 0))
    return found, rows
