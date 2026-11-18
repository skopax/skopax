import datetime as dt
import math
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

from skopax import clock
from skopax.agent import AgentError, RemoteAgent
from skopax.contract import Context, HttpError, PassResult, Peer, ReadError, SourceDown, Sweep, Unreachable, anomaly, iso
from skopax.kit import access, connection
from skopax.kube import OFF, Kube, KubeError, cluster_summary
from skopax.messages import msg

ID = 'kubernetes'
PASS = 'nodes'
PERIOD = 300
NOT_READY_S = 300
CRIT_S = 1800
FIRST_CONTACT_S = 24 * 3600
ERROR_LIMIT = 300
VERSION_LIMIT = 40
RELEASE_LIMIT = 20
WORKERS = 8
SUMMARY_KEYS = ('total', 'ready', 'notReady', 'pressure', 'unschedulable', 'nodes')
KEPT = ('startedAt', 'version', 'release', 'clusterSummary', 'seenBefore')
NAME = 'Kubernetes'
NODES_RIGHT = 'list on nodes'
ACCESS = f'{PASS}:access:'


class NodesContext(Context, Protocol):
    def local_kube(self) -> Kube: ...

    def agent_of(self, peer: Peer) -> RemoteAgent: ...


def by_name(rows: list[dict] | None) -> dict[str, dict]:
    return {r['name']: r for r in rows or [] if isinstance(r, dict) and r.get('name')}


def parsed(at: object) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(at).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def since(prev: dict | None, status: str, now: float) -> str:
    if prev and prev.get('status') == status and prev.get('since'):
        return prev['since']
    return iso(now)


def text(value: object, limit: int) -> str:
    return str(value or '')[:limit]


def local_nodes(client: Kube) -> list[dict]:
    if not client.enabled:
        raise SourceDown(OFF)
    try:
        return client.nodes()
    except KubeError as e:
        if e.code:
            raise HttpError(e.code, ID) from None
        raise Unreachable() from None


def reason(e: ReadError) -> str:
    if not isinstance(e, AgentError) or e.code is None:
        return msg('kubernetes.nodes.no_answer')
    if e.status == 401:
        return msg('kubernetes.nodes.refused')
    return msg('kubernetes.nodes.status', status=e.status)


def summary(found: dict) -> dict:
    return {k: found[k] for k in SUMMARY_KEYS if k in found}


def carded(cards: list[dict]) -> dict[str, dict]:
    out = {}
    for card in cards or []:
        key = card.get('ranKey') if isinstance(card, dict) else None
        if isinstance(key, str) and key.startswith(f'{PASS}:') and key != f'{PASS}:' and not key.startswith(ACCESS):
            out[key.split(':', 1)[1]] = card
    return out


def from_card(card: dict | None, now: float) -> dict:
    if card is None:
        return {}
    never = card.get('unit') == 'h'
    last, value = parsed(card.get('lastSeen')), card.get('now')
    if last is not None and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        start = last - value * (3600 if never else 60)
    else:
        first = parsed(card.get('firstSeen'))
        start = now if first is None else first
    floor = FIRST_CONTACT_S if never else CRIT_S if card.get('sevAuto', card.get('sev')) == 'crit' else NOT_READY_S
    start = min(start, now - floor)
    return {'since': iso(start)} if never else {'since': iso(start), 'seenBefore': True}


def master_row(ctx: NodesContext, peer: Peer, prev: dict | None) -> dict:
    row = {'name': peer.info.name, 'role': 'master', 'status': 'Ready', 'since': since(prev, 'Ready', ctx.now), 'lastSeen': iso(ctx.now), 'via': 'in-cluster'}
    try:
        row['clusterSummary'] = cluster_summary(local_nodes(ctx.local_kube()))
    except (SourceDown, HttpError, Unreachable) as e:
        row['clusterError'] = connection.failure(e)
        if isinstance(e, HttpError) and access.denied(e.code):
            row['refused'] = e.code
        elif not isinstance(e, SourceDown):
            row['failed'] = True
    return row


def agent_row(ctx: NodesContext, peer: Peer, prev: dict | None, card: dict | None = None, agents: frozenset[str] = frozenset()) -> dict:
    config = peer.info.config
    relay = config.get('via') or ''
    row = {'name': peer.info.name, 'role': 'agent', 'via': config.get('url') or ''} | ({'relay': relay} if relay else {})
    began = time.monotonic()
    try:
        info = ctx.agent_of(peer).info()
    except ReadError as e:
        error = msg('kubernetes.nodes.relay_off', relay=relay) if relay and relay not in agents else reason(e)
        row |= {'status': 'NotReady', 'since': since(prev, 'NotReady', ctx.now), 'error': error[:ERROR_LIMIT], 'lastSeen': (prev or {}).get('lastSeen')}
        row |= {k: prev[k] for k in KEPT if (prev or {}).get(k) is not None}
        if prev is None:
            row |= from_card(card, ctx.now)
        if row.get('clusterSummary'):
            row['stale'] = True
        return row
    info = info if isinstance(info, dict) else {}
    row |= {'status': 'Ready', 'since': since(prev, 'Ready', ctx.now), 'lastSeen': iso(ctx.now), 'latencyMs': round((time.monotonic() - began) * 1000),
            'startedAt': text(info.get('startedAt'), VERSION_LIMIT) or None, 'version': text(info.get('version'), VERSION_LIMIT), 'release': text(info.get('release'), RELEASE_LIMIT)}
    if not info.get('kube'):
        row['clusterError'] = msg('kubernetes.nodes.no_token')
    elif isinstance(info.get('cluster'), dict):
        row['clusterSummary'] = summary(info['cluster'])
    elif info.get('clusterError'):
        row['clusterError'] = msg('kubernetes.nodes.no_cluster')
    return row


def in_cluster(peer: Peer) -> bool:
    return (peer.info.config.get('mode') or 'agent') == 'in-cluster'


def heartbeat(ctx: NodesContext, prev: list[dict] | None, cards: list[dict] | None = None) -> list[dict]:
    before = by_name(prev)
    held = carded(cards or [])
    peers = ctx.peers(ID)
    agents = frozenset(p.info.name for p in peers if not in_cluster(p))

    def row_of(peer: Peer) -> dict:
        name = peer.info.name
        if in_cluster(peer):
            return master_row(ctx, peer, before.get(name))
        return agent_row(ctx, peer, before.get(name), held.get(name), agents)

    with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix='nodes') as pool:
        rows = list(pool.map(row_of, peers))
    named = {r['name']: r for r in rows}
    for r in rows:
        if r.get('relay') in named:
            named[r['relay']].setdefault('relaysFor', []).append(r['name'])
    return rows


def never_answered(r: dict, down: float) -> dict:
    hours = round(down / 3600)
    error = r.get('error') or msg('kubernetes.nodes.no_answer')
    x = anomaly(PASS, 'node-not-ready', '-', f"agent-{r['name']}", '', 'warn', hours, None, None, 'h', msg('kubernetes.nodes.never_label'),
                msg('kubernetes.nodes.never', name=r['name'], hours=hours, error=error), 1, msg('kubernetes.nodes.never_short', name=r['name'], hours=hours))
    x['ranKey'] = f"{PASS}:{r['name']}"
    return x


def not_ready(r: dict, down: float) -> dict:
    minutes = round(down / 60)
    sev = 'crit' if down >= CRIT_S else 'warn'
    last = parsed(r.get('lastSeen'))
    seen = msg('kubernetes.nodes.seen', at=f'{clock.hm(last)} {clock.label(last)}') if last is not None else msg('kubernetes.nodes.unseen')
    error = r.get('error') or msg('kubernetes.nodes.no_answer')
    x = anomaly(PASS, 'node-not-ready', '-', f"agent-{r['name']}", '', sev, minutes, None, None, 'min', msg('kubernetes.nodes.down_label'),
                msg('kubernetes.nodes.down', name=r['name'], minutes=minutes, seen=seen, error=error), 1, msg('kubernetes.nodes.down_short', sev=sev.upper(), name=r['name'], minutes=minutes))
    x['ranKey'] = f"{PASS}:{r['name']}"
    return x


def part_of(row: dict) -> str:
    return f"nodes of {row['name']}"


def nodes_pass(rows: list[dict], now: float, gone: tuple[str, ...] = ()) -> PassResult:
    found = []
    for r in rows:
        if r.get('role') != 'agent' or r.get('status') != 'NotReady':
            continue
        began = parsed(r.get('since'))
        down = now - (now if began is None else began)
        if not r.get('lastSeen') and not r.get('seenBefore'):
            if down >= FIRST_CONTACT_S:
                found.append(never_answered(r, down))
            continue
        if down >= NOT_READY_S:
            found.append(not_ready(r, down))
    agents = [r['name'] for r in rows if r.get('role') == 'agent']
    masters = [r for r in rows if r.get('role') == 'master' and (r.get('refused') or r.get('clusterSummary'))]
    found += [access.card(PASS, NAME, part_of(r), NODES_RIGHT, r['refused']) for r in masters if r.get('refused')]
    ran = tuple(f'{PASS}:{name}' for name in agents + [g for g in gone if g not in agents]) + tuple(access.ran_key(PASS, part_of(r)) for r in masters)
    failed = [f"{part_of(r)}: {r.get('clusterError') or ''}" for r in rows if r.get('role') == 'master' and r.get('failed')]
    status = access.status('error: ' + '; '.join(failed) if failed else 'ok', [part_of(r) for r in masters if r.get('refused')])
    return PassResult(PASS, True, status, found, {}, ran)


def run(ctx: NodesContext) -> Sweep:
    cards = ctx.open_cards()
    rows = heartbeat(ctx, ctx.previous(PASS), cards)
    serving = {r['name'] for r in rows if r.get('role') == 'agent'}
    gone = tuple(sorted(name for name in carded(cards) if name not in serving))
    return Sweep(nodes_pass(rows, ctx.now, gone), {PASS: rows})
