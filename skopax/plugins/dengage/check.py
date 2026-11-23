import datetime as dt
import math

from skopax import clock
from skopax.contract import anomaly, iso
from skopax.kit.text import cut
from skopax.plugins.dengage.client import BASE_DAYS, CHANNELS, DAY, HOUR, NEEDED, Dengage, DengageError, DengageRefused, add, blank, count, lacks, share, shown, stamp_of

CHECK = 'dengage'
SECTION = 'dengage'
NO_DATA = f'{CHECK}:no-data'
DOT = ' \u00b7 '
APP = 'https://app.dengage.com/'
READ_DAYS = BASE_DAYS + 1
RECENT_KEPT = 50
BUDGET_S = 200
PERMISSION_LIMIT = 200
RESERVED_ROW_KEYS = ('plugin', 'cluster')
RULES = {'event_min_selected': 1000, 'card_hours': 24, 'delivery_min_selected': 500, 'usual_share': 0.95, 'relay_drop': 0.7, 'blacklist_gap': 0.05,
         'blacklist_floor': 0.05, 'baseline_min_sends': 5, 'silence_hours': 48, 'silence_active_days': 15}
RULE_HELP = {
    'event_min_selected': 'A completed send gets its own info card only when it selected at least this many contacts; smaller sends stay in the dengage tool.',
    'card_hours': 'A send is looked at, and its card stays up, for this many hours after it went out.',
    'delivery_min_selected': 'Delivery is judged only on sends that selected at least this many contacts, and only such large sends make up the usual shares.',
    'usual_share': 'What counts as usual: the share of the channel\'s large sends of the 4 weeks before that a send is compared with (0.95 = the blacklisted share 95 % of them '
                   'stayed under, the relayed share 95 % of them reached), so a campaign part that always loses many contacts is not news.',
    'relay_drop': 'Warning when a send relays less than this share of that usual relayed share (0.7 = under 70 % of it); without a baseline, of the contacts it selected.',
    'blacklist_gap': 'Warning when the blacklisted share of a send is more than this above that usual blacklisted share (0.05 = 5 points more).',
    'blacklist_floor': 'The blacklist warning needs at least this share of the selected contacts dropped (0.05 = 5 %).',
    'baseline_min_sends': 'A channel\'s usual shares count only when the 4 weeks before hold at least this many large sends.',
    'silence_hours': 'Warning when a channel has completed no send for this many hours, and the gap is longer than its longest gap of the 4 weeks before.',
    'silence_active_days': 'Silence is checked only on a channel that sent on at least this many of the 28 days before its last send.',
}


def data_key(channel: str) -> str:
    return f'{CHECK}:{channel}'


def access_key(channel: str) -> str:
    return f'{CHECK}:access:{channel}'


def when(t: float) -> str:
    return f'{clock.date(t)} {clock.label(t)}'


def num(n: int) -> str:
    return f'{n:,}'


def pct(v: float | None) -> str:
    return '?' if v is None else f'{v * 100:.1f} %'


def counts(r: dict) -> str:
    return f"selected {num(r['selected'])}, prepared {num(r['prepared'])}, relayed {num(r['relayed'])}, blacklisted {num(r['blacklisted'])}"


def titled(r: dict) -> str:
    campaign = f" (campaign \"{r['campaign']}\")" if r.get('campaign') else ''
    return f"Dengage {r['channel']} send \"{r['name']}\"{campaign}"


def ranked(values: list[float], at: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, math.ceil(at * len(ordered)) - 1))], 4)


def large_sends(d: Dengage, rows: list[dict], rules: dict) -> list[tuple[dt.date, float, float]]:
    return [(d.day(stamp_of(r['at'])), r['relayed'] / r['selected'], r['blacklisted'] / r['selected']) for r in rows if r['selected'] >= rules['delivery_min_selected']]


def usual_of(large: list[tuple[dt.date, float, float]], before: dt.date, rules: dict) -> dict:
    share_kept = min(max(float(rules['usual_share']), 0.5), 1.0)
    peers = [x for x in large if before - dt.timedelta(days=BASE_DAYS) <= x[0] < before]
    known = len(peers) >= rules['baseline_min_sends']
    return {'sends': len(peers), 'kept': share_kept,
            'relayed': ranked([x[1] for x in peers], 1 - share_kept) if known else None,
            'blacklisted': ranked([x[2] for x in peers], share_kept) if known else None}


def most(usual: dict) -> str:
    return f"{usual['kept'] * 100:g} %"


def keyed(x: dict, channel: str) -> dict:
    x['ranKey'] = data_key(channel)
    return x


def send_card(r: dict, at: float) -> dict:
    label = f"{r['channel'].capitalize()} send went out"
    x = anomaly(CHECK, 'dengage-send', '-', CHECK, f"{r['channel']}-{r['id']}", 'info', r['relayed'], None, None, '', label,
                f"{titled(r)} went out {when(at)}: {counts(r)}. A large send can show up as an app-open or traffic wave and as a burst of user activity shortly after it.",
                DAY, f"INFO {label}: {r['name'][:100]}")
    x.update(channel=r['channel'], sendId=r['id'], sendName=r['name'], sentAt=r['at'], selected=r['selected'], relayed=r['relayed'], blacklisted=r['blacklisted'])
    return keyed(x, r['channel'])


def delivery_card(r: dict, at: float, problem: str, usual: dict, rules: dict) -> dict:
    relayed, black = share(r['relayed'], r['selected']), share(r['blacklisted'], r['selected'])
    if problem == 'blacklist':
        label = f"{r['channel'].capitalize()} send lost many contacts to the blacklist"
        said = (f"the blacklist dropped {num(r['blacklisted'])} of the {num(r['selected'])} contacts it selected ({pct(black)}); {most(usual)} of this channel's "
                f"{usual['sends']} large sends of the 4 weeks before lost at most {pct(usual['blacklisted'])}")
        value, base = black, usual['blacklisted']
    else:
        label = f"{r['channel'].capitalize()} send reached far fewer contacts than it selected"
        against = (f"{most(usual)} of this channel's {usual['sends']} large sends of the 4 weeks before relayed at least {pct(usual['relayed'])}" if usual['relayed'] is not None
                   else f"there is no 4-week baseline for this channel yet, so the limit is {pct(rules['relay_drop'])} of the selected contacts")
        said = f"it relayed {num(r['relayed'])} of the {num(r['selected'])} contacts it selected ({pct(relayed)}); {against}"
        value, base = relayed, usual['relayed']
    x = anomaly(CHECK, 'dengage-delivery', '-', CHECK, f"{r['channel']}-{r['id']}", 'warn', round((value or 0) * 100, 1), None, None, '%', label,
                f"{titled(r)} went out {when(at)} and {said}. All counts: {counts(r)}. The marketing team checks the send in the Dengage panel.",
                100, f"WARN {label}: {r['name'][:100]}")
    x.update(channel=r['channel'], sendId=r['id'], sendName=r['name'], sentAt=r['at'], selected=r['selected'], relayed=r['relayed'], blacklisted=r['blacklisted'],
             problem=problem, share=value, usualShare=base, usualKept=usual['kept'] if base is not None else None)
    return keyed(x, r['channel'])


def problem_of(r: dict, usual: dict, rules: dict) -> str | None:
    if r['selected'] < rules['delivery_min_selected']:
        return None
    relayed, black = r['relayed'] / r['selected'], r['blacklisted'] / r['selected']
    if relayed < rules['relay_drop'] * (usual['relayed'] if usual['relayed'] is not None else 1):
        return 'relay'
    base = usual['blacklisted']
    if base is not None and black >= rules['blacklist_floor'] and black > base + rules['blacklist_gap']:
        return 'blacklist'
    return None


def quiet(d: Dengage, channel: str, days: dict, last: str | None, now: float, rules: dict) -> dict | None:
    if not last:
        return None
    last_at = stamp_of(last)
    hours = (now - last_at) / HOUR
    today, last_day = d.day(now), d.day(last_at)
    active = sorted(day for day in (dt.date.fromisoformat(k) for k, v in days.items() if count((v or {}).get('n')) > 0)
                    if last_day - dt.timedelta(days=BASE_DAYS) < day <= last_day)
    gap = max([(b - a).days for a, b in zip(active, active[1:])] or [1])
    if hours < rules['silence_hours'] or len(active) < rules['silence_active_days'] or (today - last_day).days <= gap:
        return None
    label = f'No {channel} send for {int(hours)} hours'
    x = anomaly(CHECK, 'dengage-silence', '-', CHECK, channel, 'warn', round(hours, 1), None, None, '', label,
                f"Dengage has completed no {channel} send since {when(last_at)} ({int(hours)} h). In the {BASE_DAYS} days up to then {channel} went out on {len(active)} days and the "
                f"longest gap between send days was {gap} day{'' if gap == 1 else 's'}. Either the campaigns stopped or the sends are not completing; the marketing team checks the Dengage panel.",
                DAY, f'WARN {label}')
    x.update(channel=channel, lastSendAt=last, quietHours=int(hours), activeDays=len(active))
    return keyed(x, channel)


def lacking(channel: str, code: int, permission: str) -> str:
    return f'Dengage cannot read {channel}: {lacks(code, permission)}'


def access_card(channel: str, permission: str, code: int) -> dict:
    named = cut(permission, PERMISSION_LIMIT) or NEEDED
    label = lacking(channel, code, named)
    x = anomaly(CHECK, 'dengage-access', '-', CHECK, channel, 'warn', 1, None, None, '', label,
                f'Dengage refused to give the {channel} sends to the API user of this instance (HTTP {code}), also after a fresh sign-in, so {channel} is not checked: its open '
                f'cards stay as they are and no new send, delivery or silence card can open for it. Give the API user {named} in Dengage, or take {channel} off the channels '
                'of this instance.',
                1, f'WARN {label}')
    x.update(channel=channel, permission=named, ranKey=access_key(channel))
    return x


def stamp_or_none(text: object) -> str | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        stamp_of(text)
    except (ValueError, OverflowError):
        return None
    return text


def kept_row(old: dict | None, channel: str) -> dict:
    base = {k: v for k, v in (old or {}).items() if k not in RESERVED_ROW_KEYS}
    return base | {'part': 'channel', 'channel': channel, 'last': stamp_or_none(base.get('last'))}


def status_of(unknown: tuple[str, ...], refused: list[str], capped: list[tuple[str, int]], empty: list[str], read: list[str], errors: tuple[str, ...] = ()) -> str:
    plural = 's' if len(unknown) > 1 else ''
    parts = [f"unknown channel{plural} {', '.join(unknown)}"] if unknown else []
    if refused:
        parts.append(f"no access to {', '.join(refused)}")
    parts += [f'read {n} {ch} sends, more not read' for ch, n in capped]
    if empty:
        parts.append(f"no data for {', '.join(empty)}" if read or refused or capped else 'no data')
    return ('error: ' + '; '.join(errors) if errors else 'ok') + ''.join(f'{DOT}{p}' for p in parts)


def run_pass(d: Dengage, prior: list | None, now: float, rules: dict) -> tuple[list[dict], list[dict], tuple[str, ...], str]:
    rules = {**RULES, **rules}
    seen = {r.get('channel'): r for r in prior or [] if isinstance(r, dict) and r.get('part') == 'channel'}
    begin = d.midnight(d.day(now) - dt.timedelta(days=READ_DAYS))
    cards, channel_rows, recent, empty, refused, read, capped, errors = [], [], [], [], [], [], [], []
    d.signed_in()
    for ch in d.watched:
        old = seen.get(ch) if isinstance(seen.get(ch), dict) else None
        try:
            sent = d.sends(ch, begin, now + DAY)
        except DengageRefused as e:
            refused.append(ch)
            cards.append(access_card(ch, e.permission, e.code))
            channel_rows.append(kept_row(old, ch) | {'noAccess': cut(e.permission, PERMISSION_LIMIT) or NEEDED})
            continue
        except DengageError as e:
            errors.append(f'{ch}: {e.reason}')
            channel_rows.append(kept_row(old, ch))
            continue
        rows = [r for r in sent if begin <= stamp_of(r['at']) <= now]
        if ch in d.capped:
            capped.append((ch, len(sent)))
        elif rows:
            read.append(ch)
        else:
            empty.append(ch)
        days: dict[str, dict] = {}
        for r in rows:
            add(days.setdefault(d.day(stamp_of(r['at'])).isoformat(), blank()), r)
        newest = max(rows, key=lambda r: r['at'], default=None)
        before = stamp_or_none((old or {}).get('last'))
        last = max(newest['at'], before or '') if newest else before
        large = large_sends(d, rows, rules)
        fresh = sorted((r for r in rows if now - stamp_of(r['at']) < rules['card_hours'] * HOUR), key=lambda r: r['at'], reverse=True)
        for r in fresh:
            at = stamp_of(r['at'])
            usual = usual_of(large, d.day(at), rules)
            problem = problem_of(r, usual, rules)
            if problem:
                cards.append(delivery_card(r, at, problem, usual, rules))
            elif r['selected'] >= rules['event_min_selected']:
                cards.append(send_card(r, at))
        silent = quiet(d, ch, days, last, now, rules)
        if silent:
            cards.append(silent)
        channel_rows.append({'part': 'channel', 'channel': ch, 'last': last, 'sends24h': sum(1 for r in rows if now - stamp_of(r['at']) < DAY), 'days': dict(sorted(days.items()))}
                            | ({'moreNotRead': True} if ch in d.capped else {}))
        recent += fresh
    recent.sort(key=lambda r: r['at'], reverse=True)
    section = [{'part': 'summary', 'checkedAt': iso(now)}] + channel_rows + [{'part': 'recent'} | shown(r) for r in recent[:RECENT_KEPT]]
    off = () if d.unknown else tuple(c for c in CHANNELS if c not in d.watched)
    failed = {e.split(':', 1)[0] for e in errors}
    if errors and not (read or refused or capped or empty):
        raise DengageError('; '.join(errors))
    covered = [c for c in CHANNELS if (c in d.watched or c in off) and c not in failed]
    whole = (CHECK,) if all(c in read or c in off for c in CHANNELS) else ()
    quiet_pass = (NO_DATA,) if empty and not (read or refused or capped) else ()
    ran = whole + tuple(data_key(c) for c in CHANNELS if c in read or c in off) + tuple(access_key(c) for c in covered) + quiet_pass
    return cards, section, ran, status_of(d.unknown, refused, capped, empty, read, tuple(errors))


def links_for(card: dict) -> list[dict] | None:
    if card.get('src') != CHECK:
        return None
    return [{'label': 'Dengage', 'url': APP}]
