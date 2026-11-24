import collections
import copy
import fnmatch
import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from datetime import tzinfo
from typing import Callable, Mapping

from pymongo.errors import PyMongoError

from skopax import causes, clock, history, information, rescore, severity, summary, tags
from skopax.actors import SYSTEM
from skopax.businesstime import elapsed
from skopax.errkind import kind as error_kind
from skopax.jiralinks import ticket_pattern
from skopax.messages import ENGLISH, msg, speaking
from skopax.store import MUTED_PRESET, Conflict, Store, glob_match, mute_matches

log = logging.getLogger('skopax.engine')

REOPEN_QUIET_S = 2 * 3600
PUSH_SVC_QUIET_S = 30 * 60
CLEARED_KEEP_S = information.ENDED_KEEP_S
KEEP_UNTIL_ADDRESSED = severity.CARD_LEVELS
ADDRESSED_TRIAGE = ('progress', 'handled')
HANDLED = ('progress', 'handled')
POD_REOPEN = ('dived',) + HANDLED
HISTORY_KEEP = 300
SEV_RANK = severity.RANK
OFFER_MARKS = ('takeOffered', 'resolveOffered', 'knownOffered', 'muteOffered', 'ticketOffered')
LIFTED = 'sevUp'
AGE_UNITS = ('dur', 'min', 'h')
KEPT_WARNING = 'Warning: listed in Alerts kept at Warning.'
FIXED = 'fixed'
NO_ACTION = 'handled'
CARRY = ('note', 'suspects', 'recommend', 'assessedAt', 'triage', 'ticket', 'owner', 'absorb', 'code', 'slackDraft', 'slackPost', 'autoSend', 'slackFallback', 'triagedBy', 'investigator', 'teamsPosts', 'tags', 'tagsOff', 'business', 'tagsAt', 'fixPosts', 'num', 'assigned', 'sevSet', 'resolution', 'cause', 'noteBy', 'recommendBy', 'noteFlagged', 'recommendFlagged', 'autoAssigned', 'handAssigned', 'ownerName', LIFTED, 'sevBefore', 'previousResolution', 'cameBack', 'followUp')
POST_LISTS = ('teamsPosts', 'fixPosts')
WHEN_SET = POST_LISTS + ('followUp',)
SHOTS = 'screenshots'
NO_SERVICE = (None, '', '-', '?')
PUSH_LIMIT = 200
PUSH_FRESH_S = 3600
SERVER_PUSH_KEEP = 10
APPLY_ATTEMPTS = 5
AUTO_RESOLVE_HOURS = 6
FOLLOW_UP_DAYS = 7
FOLLOW_CLOSE = ('closedAt', 'closeAt', 'closeKind', 'closeRef', 'announcedAt', 'announced', 'basis', 'cancelled')
DAY_S = 24 * 3600
RESOLVED_KEEP_S = 24 * 3600
AUTO_RESOLVED_BY = 'auto'
SYSTEM_VIA = SYSTEM
BY_SYSTEM = ('pod', AUTO_RESOLVED_BY)
BACK_AFTER_RESOLVE = 'backAfterResolve'
DUPLICATE_MUTE_S = 24 * 3600
STALE_S = 24 * 3600
STALE_DAILY_S = 3 * 24 * 3600
STALE_MANUAL_S = 7 * 24 * 3600
DAILY_KEYS = ('crux', 'ga', 'varnish-cache')
PLUGIN_DAILY: frozenset[str] = frozenset()
SEVERITIES = severity.CARD_LEVELS
NO_DATA = ':no-data'


def no_data(key: object) -> bool:
    return isinstance(key, str) and key.endswith(NO_DATA)


def iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_iso(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()


def fmt(v: float, unit: str = '') -> str:
    if unit == '%':
        return f'{v:.1f}%' if v >= 1 or v == 0 else f'{v:.2f}%'
    if unit == 'ms':
        return f'{v:.0f} ms' if v < 1000 else f'{v / 1000:.1f} s'
    return f'{v:.0f}' if abs(v) >= 1 or v == 0 else f'{v:.2f}'


def anomaly(src: str, sig: str, ns: str, svc: str, sub: str, sev: str, now: float, d1: float | None, d7: float | None, unit: str, label: str, what: str, slack: float = 1, short: str | None = None) -> dict:
    key = f'{sig}|{ns}/{svc}' + (f'|{sub}' if sub else '')
    target = svc + (f' {sub}' if sub else '')
    base = '' if d1 is None else f' (1d {fmt(d1, unit)}' + ('' if d7 is None else f' / 7d {fmt(d7, unit)}') + ')'
    value = f' {fmt(now, unit)}' if d1 is not None or unit else ''
    return dict(key=key, src=src, sig=sig, ns=ns, svc=svc, sub=sub, sev=sev, now=round(now, 3), d1=None if d1 is None else round(d1, 3),
                d7=None if d7 is None else round(d7, 3), unit=unit, label=label, what=what,
                short=short or f'{sev.upper()} {target} {label}{value}{base}', slack=slack)


def lift(a: dict) -> None:
    floor = a.get(LIFTED)
    if a.get('keptWarning'):
        return
    if (a.get('sev') in SEVERITIES or a.get('cameBack')) and severity.higher(floor, a.get('sev')):
        a['sev'] = floor


def came_back(a: dict, followed: str | None, closed_at: str | None) -> dict | None:
    how = a.get('resolution')
    if a.get('triage') != 'handled' or followed is not None or a.get('muted') or a.get('triagedBy') in BY_SYSTEM or not isinstance(how, dict) or how.get('kind') not in (FIXED, NO_ACTION):
        return None
    return {'kind': how['kind'], 'ref': how.get('ref'), 'by': how.get('by') or a.get('triagedBy'), 'at': how.get('at') or closed_at}


def reopened_after(a: dict, back: dict, t: str) -> None:
    a['previousResolution'] = back
    if back['kind'] != FIXED:
        a.pop('cameBack', None)
        return
    a['cameBack'] = {'ref': back.get('ref'), 'at': t}
    if severity.rank(a.get('sev')) < severity.rank(severity.HIGH):
        a[LIFTED] = severity.HIGH
    person = str(back.get('by') or '')
    held = a.get('assigned') if isinstance(a.get('assigned'), dict) else {}
    if '@' in person and str(held.get('email') or '').lower() != person.lower():
        a['assignBack'] = person


def ticket_owned(a: dict | None) -> bool:
    a = a or {}
    return a.get('triage') == 'progress' and bool(ticket_pattern(()).fullmatch(str(a.get('ticket') or '')))


def push_group(a: dict) -> tuple | None:
    if a.get('svc') in NO_SERVICE:
        return None
    return (a['svc'],)


def mute_live(m: dict, now: float) -> bool:
    return m.get('until') is None or parse_iso(m['until']) > now


def mute_for(state: dict, key: str, now: float) -> dict | None:
    for m in state.get('muted', []):
        if mute_live(m, now) and mute_matches(m, key):
            return m
    return None


def flap_cause(a: dict, now: float, t: str) -> dict | None:
    cause = causes.own(a)
    if cause is None or not a.get('clearedAt') or now - parse_iso(a['clearedAt']) > REOPEN_QUIET_S:
        return None
    return cause | {'firstSeen': t}


def set_muted(a: dict, m: dict | None) -> None:
    a['muted'] = m['reason'] if m else None
    if m and m.get('preset'):
        a[MUTED_PRESET] = True
    else:
        a.pop(MUTED_PRESET, None)


def duplicate_of(a: dict, prev: dict) -> dict | None:
    r = a.get('resolution') if isinstance(a.get('resolution'), dict) else {}
    ref = str(r.get('ref') or '') if r.get('kind') == 'duplicate' else ''
    if ref.startswith('#') and ref[1:].isdigit():
        n = int(ref[1:])
        return next((p for p in prev.values() if p is not a and p.get('num') == n), None)
    hit = prev.get(ref) if ref else None
    return hit if hit is not a else None


def follow_original(state: dict, a: dict, original: dict, firing: set, now: float) -> str | None:
    ref = f"#{original['num']}" if original.get('num') else original['key']
    live = original.get('status') == 'open' or original['key'] in firing
    om = mute_for(state, original['key'], now)
    if om and live:
        cap = now + DUPLICATE_MUTE_S
        until = om.get('until') if om.get('until') and parse_iso(om['until']) <= cap else iso(cap)
        entry = {'pattern': a['key'], 'exact': True, 'until': until, 'reason': f"follows {ref}: {om.get('reason') or 'muted'}", 'kind': 'mute', 'at': iso(now)} | ({'preset': True} if om.get('preset') else {})
        state['muted'] = [m for m in state['muted'] if m.get('pattern') != a['key']] + [entry]
        set_muted(a, entry)
        return 'muted'
    if original.get('triage') == 'progress':
        a.update(triage='progress', ticket=original.get('ticket'), owner=original.get('owner'), triagedBy=original.get('triagedBy'), resolution=None)
        if live:
            a['parent'] = original['key']
        return 'progress'
    if live and original.get('triage') != 'handled':
        a['parent'] = original['key']
        return 'merged'
    return None


def reported(reach: bool | frozenset | None, a: dict) -> bool:
    if reach is True:
        return True
    cluster = a.get('cluster') or None
    return bool(reach) and (cluster is None or isinstance(cluster, str)) and cluster in reach


def merge(state: dict, found: list[dict], ran: dict[str, bool | frozenset], now: float) -> tuple[list[dict], list[dict], bool]:
    t = iso(now)
    first = not state.get('initialized')
    prev = {a['key']: a for a in state.get('anomalies', [])}
    state['muted'] = [m for m in state.get('muted', []) if m.get('until') is None or parse_iso(m['until']) > now]
    events, push, seen = [], [], set()
    firing = {f['key'] for f in found}
    recent: dict[tuple, int] = {}
    for p in prev.values():
        group = push_group(p)
        if group and p.get('pushedAt') and p.get('pushedSev') in SEV_RANK and now - parse_iso(p['pushedAt']) < PUSH_SVC_QUIET_S:
            recent[group] = max(recent.get(group, -1), SEV_RANK[p['pushedSev']])
    for f in found:
        k = f['key']
        if k in seen or log_owned_elsewhere(prev.get(k), f):
            continue
        seen.add(k)
        a = prev.get(k)
        back = None
        set_muted(f, mute_for(state, k, now))
        f.pop('cause', None)
        if a is None:
            a = dict(f, firstSeen=t, lastSeen=t, peak=f['now'], status='open', pushedAt=None, pushedSev=None, pushedPeak=None, note=None)
            prev[k] = a
            ev = 'new'
        elif a['status'] == 'cleared':
            carried = {x: a.get(x) for x in CARRY if x not in WHEN_SET or a.get(x) is not None}
            carried['cause'] = flap_cause(a, now, t)
            pushed = (a.get('pushedAt'), a.get('pushedSev'), a.get('pushedPeak'))
            closed_at = a.get('triagedAt')
            a.clear()
            a.update(f, **carried)
            a.update(firstSeen=t, lastSeen=t, peak=f['now'], status='open', pushedAt=pushed[0], pushedSev=pushed[1], pushedPeak=pushed[2])
            original = duplicate_of(a, prev) if a.get('triage') == 'handled' else None
            followed = follow_original(state, a, original, firing, now) if original else None
            back = came_back(a, followed, closed_at)
            if a.get('triage') == 'handled' and not back:
                a.pop('cameBack', None)
                a.pop('previousResolution', None)
            if a.get('triage') == 'handled' and followed != 'muted':
                a.pop(LIFTED, None)
                if a.get('triagedBy') not in BY_SYSTEM:
                    a[BACK_AFTER_RESOLVE] = t
                a['triage'] = None
                a['triagedBy'] = None
                a['resolution'] = None
            if back:
                reopened_after(a, back, t)
            if isinstance(a.get('followUp'), dict) and a['followUp'].get('closedAt'):
                a['followUp'] = {x: v for x, v in a['followUp'].items() if x not in FOLLOW_CLOSE}
            ev = 'reopened'
        else:
            keep = {x: a.get(x) for x in ('firstSeen', 'pushedAt', 'pushedSev', 'pushedPeak', 'peak', 'parent', BACK_AFTER_RESOLVE, SHOTS) + OFFER_MARKS + CARRY
                    if x not in WHEN_SET + OFFER_MARKS + (BACK_AFTER_RESOLVE, SHOTS) or a.get(x) is not None}
            a.clear()
            a.update(f, **keep)
            a['lastSeen'] = t
            a['status'] = 'open'
            a['peak'] = max(a.get('peak') or 0, f['now'])
            ev = None
        regressed = ev == 'reopened' and bool(back) and back['kind'] == FIXED
        quiet_back = ev == 'reopened' and bool(back) and back['kind'] == NO_ACTION
        manual = a.get('sevSet')
        hand = isinstance(manual, dict) and manual.get('sev') in SEV_RANK
        lift(a)
        if hand:
            a['sevAuto'] = a['sev']
            a['sev'] = manual['sev']
        if ev and not a.get('parent'):
            host = next((p for p in prev.values() if p is not a and p['status'] == 'open' and p.get('triage') != 'handled' and SEV_RANK[a['sev']] <= SEV_RANK.get(p.get('sev'), 0)
                         and any(glob_match(g, k) for g in p.get('absorb') or [])), None)
            if host:
                a['parent'] = host['key']
        pushable = not first and not a['muted'] and SEV_RANK[a['sev']] >= 1
        should = False
        if a.get('parent') and ev and not information.under_information(a, prev):
            a['pushedAt'], a['pushedSev'], a['pushedPeak'] = t, a['sev'], a['now']
            pushable = False
        if pushable:
            doubled = a.get('pushedPeak') is not None and a['now'] >= 2 * a['pushedPeak'] and a['now'] - a['pushedPeak'] >= a.get('slack', 1)
            if doubled and ev != 'new' and not hand and not a.get('keptWarning') and a.get('unit') not in AGE_UNITS and severity.up(a['sev']) != a['sev']:
                a[LIFTED] = a['sev'] = severity.up(a['sev'])
            higher = a.get('pushedSev') not in SEV_RANK or SEV_RANK[a['sev']] > SEV_RANK[a['pushedSev']]
            quiet = a.get('pushedAt') is None or now - parse_iso(a['pushedAt']) > REOPEN_QUIET_S
            if ev == 'new':
                should = True
            elif ev == 'reopened':
                should = regressed or (higher if quiet_back else higher or (doubled if a.get('triage') in HANDLED else quiet))
            else:
                should = higher or doubled
                if should:
                    ev = 'escalated'
        if should and a.get('triagedBy') in BY_SYSTEM and a.get('triage') in POD_REOPEN:
            a['triage'] = None
            a['triagedBy'] = None
            a['resolution'] = None
        if first and SEV_RANK[a['sev']] >= 1:
            a['pushedAt'], a['pushedSev'], a['pushedPeak'] = t, a['sev'], a['now']
        if ev:
            events.append(dict(t=t, key=k, ev=ev, sev=a['sev'], text=a['short'], muted=bool(a['muted'])) | ({'cameBack': str(back.get('ref') or '')} if regressed else {}))
        if should:
            a['pushedAt'], a['pushedSev'], a['pushedPeak'] = t, a['sev'], a['now']
            if regressed or SEV_RANK[a['sev']] > recent.get(push_group(a), -1):
                push.append(a)
    for k, a in prev.items():
        if k in seen or a['status'] != 'open' or not reported(ran.get(clearing_key(a)), a):
            continue
        a['status'] = 'cleared'
        a['clearedAt'] = t
        events.append(dict(t=t, key=k, ev='cleared', sev=a['sev'], text=a['short'], muted=bool(a.get('muted'))))
    for k, a in prev.items():
        if k in seen or a['status'] != 'open' or not a.get('lastSeen') or now - parse_iso(a['lastSeen']) <= stale_after(a):
            continue
        expire(a, t, f"Closed: not seen since {clock.date(parse_iso(a['lastSeen']))}, its check no longer reports it")
        events.append(dict(t=t, key=k, ev='cleared', sev=a['sev'], text=a['short'], muted=bool(a.get('muted'))))
    for k, a in prev.items():
        if a['status'] == 'cleared':
            set_muted(a, mute_for(state, k, now))

    def settled(a: dict) -> bool:
        m = mute_for(state, a['key'], now)
        return a.get('triage') in ADDRESSED_TRIAGE or bool(m and m.get('until') is None)

    def addressed(a: dict) -> bool:
        p = prev.get(a.get('parent'))
        return settled(a) or bool(p and p is not a and settled(p))

    def resolved_kept(a: dict | None) -> bool:
        a = a or {}
        stamps = (a.get('autoResolvedAt'), a.get('triagedAt') if a.get('triage') == 'handled' else None)
        return any(now - parse_iso(at) < RESOLVED_KEEP_S for at in stamps if at)

    keep = [a for a in prev.values() if a['status'] == 'open' or (a['sev'] in KEEP_UNTIL_ADDRESSED and not addressed(a)) or now - parse_iso(a.get('clearedAt') or t) < CLEARED_KEEP_S
            or any(resolved_kept(c) or ticket_owned(c) for c in (a, prev.get(a.get('parent'))))]
    keep.sort(key=lambda a: (a['status'] != 'open', bool(a.get('muted')), -SEV_RANK[a['sev']], a['firstSeen']))
    state['anomalies'] = keep
    state['history'] = (events[::-1] + state.get('history', []))[:HISTORY_KEEP]
    state['initialized'] = True
    return events, push, first


def log_owned_elsewhere(a: dict | None, f: dict) -> bool:
    if a is None or a.get('src') not in rescore.LOG_SOURCES or f.get('src') not in rescore.LOG_SOURCES or a.get('src') == f.get('src'):
        return False
    return a.get('status') == 'open'


def clearing_key(a: dict) -> str:
    return a.get('ranKey') or a.get('cluster') or a.get('src')


def put_note(a: dict, text: str, t: str, by: str | None = None) -> None:
    a['note'] = {'text': text, 'at': t}
    a.pop('noteBy', None)
    if by:
        a['noteBy'] = by


def expire(a: dict, t: str, note: str) -> None:
    a.update(status='cleared', clearedAt=t, expired=True)
    if not a.get('note'):
        put_note(a, note, t, 'system')


ANY_CLUSTER = object()


def retire(state: dict, keys: set[str], note: str, now: float, cluster: str | None | object = ANY_CLUSTER) -> list[str]:
    t = iso(now)
    closed = []
    for a in state.get('anomalies') or []:
        if a.get('status') != 'open' or clearing_key(a) not in keys:
            continue
        if cluster is not ANY_CLUSTER and (a.get('cluster') or None) != cluster:
            continue
        expire(a, t, note)
        closed.append(a['key'])
        state['history'] = [dict(t=t, key=a['key'], ev='cleared', sev=a['sev'], text=a['short'], muted=bool(a.get('muted')))] + state.get('history', [])
    state['history'] = state.get('history', [])[:HISTORY_KEEP]
    return closed


def daily_keys_of(plugins) -> frozenset[str]:
    return frozenset(k for p in plugins for c in p.checks if c.daily is True for k in (c.id, *c.ran) if isinstance(k, str))


def set_daily(keys) -> None:
    global PLUGIN_DAILY
    PLUGIN_DAILY = frozenset(keys)


def daily_names(a: dict) -> set:
    key = a.get('ranKey') or a.get('src')
    names = {key, a.get('src')}
    if isinstance(key, str):
        cluster = a.get('cluster')
        base = key[:-len(cluster) - 1] if isinstance(cluster, str) and cluster and key.endswith(f'-{cluster}') else key
        names |= {base, base.split(':', 1)[0]}
    return names


def stale_after(a: dict) -> float:
    key = a.get('ranKey') or a.get('src')
    if key == 'manual':
        return STALE_MANUAL_S
    return STALE_DAILY_S if key in DAILY_KEYS or a.get('src') in DAILY_KEYS or PLUGIN_DAILY & daily_names(a) else STALE_S


def auto_resolution(at: str) -> dict:
    return {'kind': 'handled', 'by': AUTO_RESOLVED_BY, 'via': SYSTEM_VIA, 'at': at}


def follow_parents(state: dict, now: float) -> list[dict]:
    t = iso(now)
    cards = state.get('anomalies') or []
    by_key = {a['key']: a for a in cards}
    out = []
    for a in cards:
        pk = a.get('parent')
        if not pk or pk == a['key'] or a.get('status') != 'cleared' or a.get('triage') in ADDRESSED_TRIAGE + ('investigating',) or a.get('muted') or information.is_item(a):
            continue
        p = by_key.get(pk)
        if p is not None and p.get('triage') not in ADDRESSED_TRIAGE:
            continue
        triage = p['triage'] if p is not None else 'handled'
        a.update(triage=triage, triagedBy=AUTO_RESOLVED_BY, triagedAt=t)
        if p is not None and p.get('ticket'):
            a['ticket'] = p['ticket']
        if triage == 'handled':
            a['autoResolvedAt'] = t
            a['resolution'] = auto_resolution(t)
        named = pk if p is None else parent_name(p)
        if triage == 'progress':
            ticket = f" ({p['ticket']})" if p is not None and p.get('ticket') else ''
            head, why = 'In progress with its parent', f'{named}{ticket}'
        else:
            head, why = 'Auto-resolved', f'its parent {named} was ' + ('handled and has left the page' if p is None else 'marked no action needed')
        if not a.get('note'):
            put_note(a, f'{head}: {why}', t)
        row = {'key': a['key'], 'text': f"{head.lower()} {a.get('svc')} {a.get('label')} \u00b7 {why}", 'since': 'parent'}
        out.append(row | ({'progress': True} if triage == 'progress' else {'why': parent_reason(p)}))
    return out


def parent_name(p: dict) -> str:
    return ' '.join(f"{'#' + str(p['num']) + ' ' if p.get('num') else ''}{p.get('svc') or '-'} / {p.get('label') or '?'}".split())


def parent_reason(p: dict | None) -> str:
    if p is None:
        return PARENT_GONE
    name = parent_name(p)
    room = REASON_LIMIT - len(PARENT_HEAD) - len(PARENT_MARKED)
    return PARENT_HEAD + (name if len(name) <= room else name[:room - 1].rstrip() + '\u2026') + PARENT_MARKED


def window_text(hours: int, business: bool = False, words: str = '') -> str:
    words = words or ('engine.window.business_' if business else 'engine.window.')
    if hours > 0 and hours % 24 == 0:
        days = hours // 24
        return msg(words + ('day' if days == 1 else 'days'), n=days)
    return msg(words + ('hour' if hours == 1 else 'hours'), n=hours)


def english_window(hours: int, business: bool = False) -> str:
    with speaking(ENGLISH):
        return window_text(hours, business=business)


def business_mark(business: tzinfo | None) -> dict:
    return {'businessTime': True} if business is not None else {}


def window_for(a: dict, windows: tuple) -> int | None:
    names = daily_names(a)
    return next((hours for found, sig, hours in windows if a.get('sig') == sig and found & names), None)


OWNED_WINDOWS = 3
REASON_LIMIT = 160
PARENT_HEAD = 'its parent '
PARENT_MARKED = ' was marked no action needed'
PARENT_GONE = 'its parent was handled and has left the page'


def has_owner(a: dict) -> bool:
    return bool(a.get('owner') or a.get('assigned'))


def wait_start(a: dict, cleared: float) -> float:
    assigned = a.get('assigned') if isinstance(a.get('assigned'), dict) else {}
    marks = [cleared]
    for at in (assigned.get('at'), a.get('reopenedAt')):
        if isinstance(at, str) and at:
            try:
                marks.append(parse_iso(at))
            except ValueError:
                continue
    return max(marks)


def acted_on(a: dict) -> bool:
    return bool(a.get('triage') in ADDRESSED_TRIAGE + ('investigating',) or a.get('ticket') or a.get('owner') or a.get('muted'))


def auto_resolve_kinds(state: dict, now: float, windows: tuple, business: tzinfo | None = None) -> list[dict]:
    if not windows:
        return []
    t = iso(now)
    out = []
    for a in state.get('anomalies') or []:
        hours = window_for(a, windows)
        if not hours or hours <= 0 or acted_on(a) or a.get('parent') or not a.get('firstSeen') or information.is_item(a):
            continue
        first = parse_iso(a['firstSeen'])
        if elapsed(first, now, business) <= hours * 3600:
            continue
        why = f"first seen {clock.date(first)}, nobody acted in {english_window(hours, business is not None)}"
        a.update(triage='handled', triagedBy=AUTO_RESOLVED_BY, autoResolvedAt=t, resolution=auto_resolution(t))
        if not a.get('note'):
            put_note(a, f'Auto-resolved: {why}', t)
        out.append({'key': a['key'], 'text': f"auto-resolved {a.get('svc')} {a.get('label')} \u00b7 {why}", 'why': why, 'hours': hours, 'since': 'first'} | business_mark(business))
    return out


def pending_done(a: dict, now: float, days: int) -> bool:
    fu = a.get('followUp')
    if not isinstance(fu, dict) or not fu.get('doneWhen') or fu.get('flagged') or fu.get('gaveUp') or fu.get('untold') or fu.get('closedAt'):
        return False
    try:
        return now - parse_iso(str(fu.get('setAt'))) < days * DAY_S
    except ValueError:
        return False


def auto_resolve(state: dict, now: float, hours: int, windows: tuple = (), by_severity: Mapping[str, int] | None = None, business: tzinfo | None = None,
                 follow_up_days: int = FOLLOW_UP_DAYS) -> list[dict]:
    t = iso(now)
    out = []
    cards = state.get('anomalies') or []
    by_key = {a['key']: a for a in cards}
    for a in summary.waiting_cards(cards):
        window = (by_severity or {}).get(a.get('sev'), hours)
        if window <= 0 or a.get('triage') == 'investigating' or a.get('autoResolvedAt') or not a.get('clearedAt') or window_for(a, windows) or pending_done(a, now, follow_up_days):
            continue
        window *= OWNED_WINDOWS if has_owner(a) else 1
        cleared = parse_iso(a['clearedAt'])
        event = causes.event(a, by_key) and not a.get('ticket')
        waited = window + (causes.EVENT_EXTRA_HOURS if event else 0)
        if elapsed(wait_start(a, cleared), now, business) <= waited * 3600:
            continue
        why = msg('engine.auto.event') if event else f"cleared {clock.date(cleared)}, nobody acted in {english_window(window, business is not None)}"
        a.update(triage='handled', triagedBy=AUTO_RESOLVED_BY, autoResolvedAt=t, resolution=auto_resolution(t))
        if not a.get('note'):
            put_note(a, why if event else f'Auto-resolved: {why}', t)
        out.append({'key': a['key'], 'text': f"auto-resolved {a.get('svc')} {a.get('label')} \u00b7 {why}", 'why': why, 'hours': waited} | ({'event': True} if event else {}) | business_mark(business))
    return out


SIBLING_ORDINAL = re.compile(r'-\d+$')


def group_alert_twins(state: dict, twins: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    cards = state.get('anomalies') or []
    hosts = [a for a in cards if information.is_card(a) and a.get('status') == 'open' and a.get('src') not in ('alert', 'grafana') and not a.get('muted') and not a.get('parent') and a.get('ns') not in (None, '', '-')]
    out = []
    for a in cards:
        parts = a['key'].split('|')
        if a.get('src') != 'alert' or len(parts) < 2 or parts[1] not in twins or a.get('status') != 'open':
            continue
        if a.get('parent') or a.get('triage') or a.get('slackPost') or a.get('slackDraft') or a.get('muted'):
            continue
        same = sorted((h for h in hosts if h.get('ns') == a.get('ns')), key=lambda h: (-SEV_RANK.get(h.get('sev'), 0), h.get('firstSeen') or ''))
        if same:
            a['parent'] = same[0]['key']
            out.append((a['key'], same[0]['key']))
    return out


def sibling_stem(a: dict) -> str | None:
    svc = a.get('svc') or ''
    m = SIBLING_ORDINAL.search(svc)
    if not m or svc not in a['key']:
        return None
    return '|'.join((a.get('src') or '', a.get('ns') or '', a['key'].replace(svc, svc[:m.start()])))


def posted_this_time(a: dict) -> bool:
    at = (a.get('slackPost') or {}).get('at')
    return bool(at and a.get('firstSeen') and at >= a['firstSeen'])


def group_siblings(state: dict) -> list[tuple[str, str]]:
    cards = [a for a in state.get('anomalies') or [] if a.get('status') == 'open' and not a.get('muted') and not a.get('parent')]
    sets: dict[str, list[dict]] = {}
    for a in cards:
        stem = sibling_stem(a)
        if stem:
            sets.setdefault(stem, []).append(a)
    out = []
    for same in sets.values():
        if len(same) < 2:
            continue
        host = sorted(same, key=lambda h: (-SEV_RANK.get(h.get('sev'), 0), h.get('firstSeen') or '', h['key']))[0]
        for a in same:
            if a is host or a.get('triage') in ADDRESSED_TRIAGE + ('investigating',) or a.get('slackDraft') or posted_this_time(a):
                continue
            if host.get('triage') == 'handled' and not information.calm(a):
                continue
            a['parent'] = host['key']
            out.append((a['key'], host['key']))
    return out


def push_line(push: list[dict]) -> str:
    if not push:
        return ''
    push = sorted(push, key=lambda a: -SEV_RANK[a['sev']])
    line = msg('push.prefix') + '; '.join(a['short'] for a in push)
    return line if len(line) < PUSH_LIMIT else line[:PUSH_LIMIT - 4] + '…'


def new_pushes(before: dict[str, str | None], items: list[dict], now: float) -> list[dict]:
    out = []
    by_key = {x.get('key'): x for x in items}
    for a in items:
        at = a.get('pushedAt')
        if not at or at == before.get(a['key']) or a.get('muted') or a.get('sev') not in severity.CARD_LEVELS:
            continue
        if a.get('parent') and a.get('firstSeen') == at and not information.under_information(a, by_key):
            continue
        try:
            fresh = now - parse_iso(at) < PUSH_FRESH_S
        except (TypeError, ValueError):
            fresh = False
        if fresh:
            out.append(a)
    return out


@dataclass
class PassResult:
    name: str
    ok: bool
    status: str
    found: list[dict] = field(default_factory=list)
    sections: dict = field(default_factory=dict)
    ran: tuple[str, ...] = ()


@dataclass
class Applied:
    rev: int
    events: list[dict]
    push: list[dict]
    resolved: list[dict] = field(default_factory=list)
    cards: list[dict] = field(default_factory=list)
    first: bool = False


def reach_of(results: list[PassResult], clusters: Mapping[str, tuple[str | None, ...]] | None = None) -> dict[str, bool | frozenset]:
    out: dict[str, bool | frozenset] = {}
    for r in results:
        if not r.ok:
            continue
        mine = None if clusters is None else clusters.get(r.name)
        for k in r.ran or (r.name,):
            if no_data(k):
                continue
            seen = out.get(k)
            out[k] = True if mine is None or seen is True else frozenset(seen or ()) | frozenset(mine)
    return out


def number_cards(store: Store, cards: list[dict]) -> None:
    missing = [a for a in cards if not a.get('num') and information.is_card(a)]
    if not missing:
        return
    try:
        known = store.card_numbers([a['key'] for a in missing])
        for a in missing:
            a['num'] = known.get(a['key']) or store.card_number(a['key'])
    except PyMongoError as e:
        log.warning('card numbers not given this pass: %s', error_kind(e))


def kept_at_warning(found: list[dict], patterns: tuple[str, ...]) -> list[dict]:
    if not patterns:
        return found
    for x in found:
        name = x.get('alertName')
        if x.get('sig') == 'alert' and x.get('sev') == severity.HIGH and x.get('alertLabel') in severity.ALERT_WARNING and isinstance(name, str) and any(fnmatch.fnmatchcase(name, p) for p in patterns):
            x['sev'] = severity.WARN
            x['short'] = re.sub(r'^HIGH ', 'WARN ', str(x.get('short') or ''))
            x['what'] = f"{x.get('what') or ''} {KEPT_WARNING}".strip()
            x['keptWarning'] = True
    return found


def _merged(state: dict, all_results: list[PassResult], now: float, alert_twins: tuple[str, ...], screen, clusters, alerts_warn: tuple[str, ...] = ()) -> tuple:
    results = screen(state, all_results) if screen else all_results
    found = kept_at_warning(copy.deepcopy([x for r in results if r.ok for x in r.found]), alerts_warn)
    events, push, first = merge(state, copy.deepcopy(found), reach_of(results, clusters), now)
    twins = group_alert_twins(state, alert_twins)
    siblings = group_siblings(state)
    if twins or siblings:
        grouped = {k for k, _ in twins + siblings}
        push = [a for a in push if a.get('key') not in grouped]
    return results, events, push, twins, siblings, first


def apply(store: Store, results: list[PassResult], now: float, links: Callable[[dict], list[dict] | None] | None = None, dm: bool = False,
          attempts: int = APPLY_ATTEMPTS, after: Callable[[dict], None] | None = None, auto_resolve_hours: int = AUTO_RESOLVE_HOURS,
          recurring_count: int = history.RECURRING_COUNT, recurring_days: int = history.RECURRING_DAYS, tag_rules: tuple | None = None,
          alert_twins: tuple[str, ...] = (), screen: Callable[[dict, list[PassResult]], list[PassResult]] | None = None,
          clusters: Mapping[str, tuple[str | None, ...]] | None = None, kind_windows: tuple = (),
          severity_hours: Mapping[str, int] | None = None, business: tzinfo | None = None, alerts_warn: tuple[str, ...] = (), follow_up_days: int = FOLLOW_UP_DAYS) -> Applied | None:
    all_results = results
    hooks = tags.text_hooks() if tag_rules is not None else []
    texts = None
    if hooks:
        ahead = store.load()
        _merged(ahead, all_results, now, alert_twins, screen, clusters, alerts_warn)
        texts = tags.texts_with(ahead['anomalies'], tag_rules, iso(now), hooks)
    for attempt in range(attempts):
        state = store.load()
        loaded = copy.deepcopy(state)
        base = state.get('rev', 0)
        results, events, push, twins, siblings, first = _merged(state, all_results, now, alert_twins, screen, clusters, alerts_warn)
        if tag_rules is not None:
            tags.apply_rules(state['anomalies'], tag_rules, iso(now), texts if hooks else tags.texts_with(state['anomalies'], tag_rules, iso(now), hooks))
        number_cards(store, state['anomalies'])
        if links:
            for a in state['anomalies']:
                got = links(a)
                if got is not None:
                    a['links'] = got
        sources = dict(state.get('sources') or {})
        runs = dict(state.get('serverPasses') or {})
        for r in results:
            sources[r.name] = r.status
            runs[r.name] = {'at': iso(now), 'status': r.status}
            if r.ok:
                state.update(r.sections)
        state['sources'] = sources
        state['serverPasses'] = runs
        if after:
            after(state)
        given = dict(severity_hours or {})
        given.setdefault(severity.HIGH, given.get(severity.WARN, auto_resolve_hours))
        by_severity = {sev: given.get(sev, auto_resolve_hours) for sev in SEVERITIES}
        resolved = (auto_resolve_kinds(state, now, kind_windows, business) + auto_resolve(state, now, auto_resolve_hours, kind_windows, by_severity, business, follow_up_days)
                    + (follow_parents(state, now) if any(h > 0 for h in by_severity.values()) else []))
        rows = store.history_rows([a['key'] for a in state['anomalies']])
        if rows is not None:
            try:
                history.annotate(state['anomalies'], rows, now, recurring_count, recurring_days, ticket_owned)
            except Exception as e:
                log.warning('seen-before and recurring marks skipped this pass: %s', error_kind(e))
        state['summary'] = summary.build(state, store, now)
        line = push_line(push)
        if line:
            state['serverPushes'] = ([{'at': iso(now), 'line': line, 'keys': [a['key'] for a in push], 'dm': dm}] + (state.get('serverPushes') or []))[:SERVER_PUSH_KEEP]
        try:
            rev, _ = store.save_state(state, base, loaded)
        except Conflict:
            log.info('server passes met a newer state (attempt %d), merging again', attempt + 1)
            continue
        if results:
            store.add_activity('pass', 'server', ' · '.join(f'{r.name} {r.status}' for r in results), error=' · '.join(f'{r.name} {r.status}' for r in results if not r.ok) or None)
        if events:
            counts = collections.Counter(e['ev'] for e in events)
            store.add_activity('merge', 'server', ' · '.join(f'{n} {ev}' for ev, n in sorted(counts.items())))
        for r in resolved:
            store.add_activity('auto', SYSTEM, r['text'], key=r['key'])
            log.info('%s', r['text'])
        hosts = {a['key']: a for a in state['anomalies']}
        for key, host in twins:
            store.add_activity('auto', SYSTEM, msg('engine.grouped.twin', key=key, host=parent_name(hosts[host]) if host in hosts else host), key=key)
        for key, host in siblings:
            store.add_activity('auto', SYSTEM, msg('engine.grouped.sibling', key=key, host=parent_name(hosts[host]) if host in hosts else host), key=key)
        return Applied(rev, events, push, resolved, state['anomalies'], first)
    raise Conflict(store.rev()['rev'])
