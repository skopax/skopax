import datetime as dt

from skopax import history, tickets
from skopax import clock
from skopax.contract import Refused as ClientRefused
from skopax.enrich import SourceDown
from skopax.errkind import Authored, text_of
from skopax.jiralinks import ticket_pattern
from skopax.actors import is_token, token_label
from skopax.store import NotFound, Store, iso, utc_now

INSTANT = frozenset({'mute', 'unmute', 'reopen', 'link', 'severity', 'known', 'resolve', 'take', 'group', 'ticket'})
DEFAULT_MUTE_HOURS = 24
RESULT_WHY = 160
HANDLED_SEVERITIES = ('warn', 'info')
SEV_NAMES = {'crit': 'Critical', 'warn': 'Warning', 'info': 'Info', 'auto': 'auto'}


class Refused(Authored, ValueError):
    pass


def note_under(note, why: str, by: str | None, verb: str) -> str:
    finding = str((note.get('text') if isinstance(note, dict) else note) or '').strip()
    who = token_label(by) if is_token(by) else 'Claude' if not by or by == 'Claude' else str(by).split('@')[0]
    return f'{finding}\n\n{verb} by {who}: {why}' if finding else why


def resolution(store: Store, action: dict) -> dict:
    kind = action.get('kind') or 'handled'
    ref = ' '.join(str(action.get('ref') or '').split())
    problem = history.resolve_problem(kind, ref or None)
    if problem:
        raise Refused(problem)
    if kind == 'duplicate':
        try:
            ref = history.duplicate_ref(store, action['key'], ref)
        except ValueError as e:
            raise Refused(text_of(e)) from None
    via = action.get('via') if action.get('via') in history.RESOLVE_VIAS else 'slack' if action.get('source') == 'slack' else 'page'
    return {'kind': kind, 'ref': ref or None, 'by': action.get('requestedBy') or None, 'via': via}


def apply(store: Store, action: dict, jira=None, settings=None, base_url: str = '', notify=None, extra_projects=()) -> str:
    kind, key = action['type'], action['key']
    if kind == 'ticket':
        if jira is None or settings is None:
            raise Refused('Jira is not set up on this service')
        by = (action.get('requestedBy') or '').strip()
        try:
            new_key, warning = tickets.create_task(store, jira, settings, key, action.get('title'), by, (action.get('reason') or '').strip(), base_url or settings.base_url, action.get('description'), action.get('points'), action.get('project'), action.get('epic'), tuple(extra_projects))
        except (ClientRefused, SourceDown) as e:
            raise Refused(e.text) from None
        told = notify(key, new_key, by) if notify else []
        return f'{new_key} created · In progress · {by}' + (f" · notified: {', '.join(told)}" if told else '') + (f' · {warning}' if warning else '')
    if kind == 'mute':
        hours = action.get('hours') or DEFAULT_MUTE_HOURS
        until = utc_now() + dt.timedelta(hours=hours)
        store.mute(key, action.get('reason') or f'muted on the page for {hours:g} h', iso(until), 'mute')
        return f"muted until {clock.date(until)} {clock.label(until)}"
    if kind == 'known':
        store.mute(key, action.get('reason') or 'marked as known noise on the page', None, 'known')
        return 'known noise'
    if kind == 'unmute':
        store.unmute(key)
        return 'unmuted'
    if kind == 'severity':
        sev = action.get('sev')
        if sev not in SEV_NAMES:
            raise Refused('severity is one of crit, warn, info or auto')
        _, auto = store.set_severity(key, sev, action.get('requestedBy'))
        return f'severity back to the signal ({SEV_NAMES[auto]})' if sev == 'auto' else f'severity set to {SEV_NAMES[sev]} by hand (the signal says {SEV_NAMES[auto]})'
    if kind == 'link':
        ticket = (action.get('ticket') or '').strip().upper()
        if not ticket_pattern(()).fullmatch(ticket):
            raise Refused('link needs a Jira key like OPS-123')
        state = store.anomaly(key).get('triage')
        store.triage(key, 'handled' if state == 'handled' else 'progress', ticket=ticket)
        return f'linked to {ticket}' + ('' if state == 'handled' else ' · In progress')
    if kind == 'reopen':
        doc = store.anomaly(key)
        if doc.get('muted'):
            store.unmute(key)
        if doc.get('triage'):
            store.triage(key, None)
        return 'reopened'
    if kind == 'resolve':
        ticket = (action.get('ticket') or '').strip()
        why = (action.get('reason') or '').strip()
        how = resolution(store, action)
        if why:
            store.note(key, note_under(store.anomaly(key).get('note'), why, action.get('requestedBy'), 'Resolved'))
        store.triage(key, 'handled', ticket=ticket, resolution=how)
        return history.kind_text(how) + (f' · {ticket}' if ticket else '') + (f' · {why[:RESULT_WHY]}' if why else '')
    if kind == 'take':
        ticket = (action.get('ticket') or '').strip()
        why = (action.get('reason') or '').strip()
        held = store.anomaly(key).get('owner')
        owner = (action.get('owner') or '').strip() or (None if held else (action.get('requestedBy') or '').strip() or None)
        if why:
            store.note(key, note_under(store.anomaly(key).get('note'), why, action.get('requestedBy'), 'Taken'))
        store.triage(key, 'progress', ticket=ticket or None, owner=owner)
        owner = owner or held
        return 'In progress' + (f' · {owner}' if owner else '') + (f' · {ticket}' if ticket else '') + (f' · {why[:RESULT_WHY]}' if why else '')
    if kind == 'group':
        parent = (action.get('parent') or '').strip()
        if not parent or parent == key:
            raise Refused('pick another card as the parent')
        host, kid = store.anomaly(parent), store.anomaly(key)
        states = (host.get('status'), kid.get('status'))
        if any(s not in ('open', 'cleared') for s in states):
            raise Refused('only open or stopped cards can be grouped')
        if states == ('cleared', 'open'):
            raise Refused(f'{parent} already stopped and {key} is still open; pick an open card as the parent')
        above = host.get('parent')
        if above and above != parent:
            try:
                over = store.anomaly(above)
            except NotFound:
                over = None
            if over and over.get('status') == 'open':
                raise Refused(f'{parent} is already merged under {key}; that grouping stands' if above == key else f'{parent} is itself merged under {above}; group under {above} instead')
        if host.get('triage') == 'handled' and kid.get('sev') not in HANDLED_SEVERITIES:
            raise Refused(f'{parent} is handled and {key} is critical; a critical card stays open for a human, also under a parent')
        if host.get('triage') == 'handled' and kid.get('triage') != 'handled':
            raise Refused(f'{parent} is already resolved (Action taken), so {key} would disappear from Open under it; pick an open card as the parent')
        store.group(parent, [key])
        return f'grouped under {parent}'
    raise ValueError(f'{kind} is not an instant action')
