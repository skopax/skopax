import datetime as dt
import logging
import re

from skopax import history, information, severity, tickets
from skopax import clock
from skopax.contract import Refused as ClientRefused
from skopax.enrich import SourceDown
from skopax.engine import parent_name
from skopax.errkind import Authored, kind as error_kind, text_of
from skopax.jiralinks import ticket_pattern
from skopax.messages import msg
from skopax.ownwords import KNOWN_REASON, KNOWN_REASONS, REASONS
from skopax.actors import is_token, token_label
from skopax.store import NotFound, Store, iso, utc_now

log = logging.getLogger('skopax.actions')
ANY_RECORD = frozenset({'severity', 'assess', 'postdeploy', 'unmute'})
INSTANT = frozenset({'mute', 'unmute', 'reopen', 'link', 'severity', 'known', 'resolve', 'take', 'group', 'ticket'})
DEFAULT_MUTE_HOURS = 24
RESULT_WHY = 160
SEV_NAMES = {**severity.NAMES, 'auto': 'auto'}
PRESET = 'presetReason'


class Refused(Authored, ValueError):
    pass


SYSTEM_NOTE_BY = ('system', 'server', 'auto')
CLOSE_NOTES = ('cards.retired', 'cards.retired_plugin', 'cards.retired_check')
NOT_SEEN = 'Closed: not seen since '


def close_note_patterns() -> list[re.Pattern]:
    found = []
    for key in CLOSE_NOTES:
        text = msg(key, plugin='\x00', instance='\x01', check='\x02')
        found.append(re.compile(re.escape(text).replace('\x00', '.+').replace('\x01', '.+').replace('\x02', '.+')))
    return found


def system_note(doc: dict) -> bool:
    note = doc.get('note')
    text = str(note.get('text') or '') if isinstance(note, dict) else ''
    if not text:
        return False
    if doc.get('noteBy'):
        return doc['noteBy'] in SYSTEM_NOTE_BY
    return bool(doc.get('expired')) and (text.startswith(NOT_SEEN) or any(p.fullmatch(text) for p in close_note_patterns()))


def note_under(note, why: str, by: str | None, verb: str) -> str:
    finding = str((note.get('text') if isinstance(note, dict) else note) or '').strip()
    who = token_label(by) if is_token(by) else 'Investigator' if not by or by == 'Claude' else str(by).split('@')[0]
    return f'{finding}\n\n{verb} by {who}: {why}' if finding else why


def default_reason(action: dict) -> str:
    if action.get('type') == 'mute':
        return f'muted on the page for {(action.get("hours") or DEFAULT_MUTE_HOURS):g} h'
    return KNOWN_REASONS.get(action.get('via'), KNOWN_REASON)


def preset_reason(action: dict) -> bool:
    return not action.get('reason') or action.get(PRESET) is True or str(action.get('reason')).strip() in REASONS


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


def information_item(store: Store, key: str) -> bool:
    try:
        return information.is_item(store.anomaly(key))
    except NotFound:
        return False


def display_name(owner: str | None, name_of) -> str | None:
    if not owner or '@' not in owner or name_of is None:
        return None
    try:
        name = str(name_of(owner) or '').strip()
    except Exception as e:
        log.warning('the display name of whoever took a card was not read: %s', error_kind(e))
        return None
    return name if name and '@' not in name else None


def apply(store: Store, action: dict, jira=None, settings=None, base_url: str = '', notify=None, extra_projects=(), name_of=None) -> str:
    kind, key = action['type'], action['key']
    if kind not in ANY_RECORD and information_item(store, key):
        raise Refused(msg('cards.information_item', key=key))
    if kind == 'ticket':
        if jira is None or settings is None:
            raise Refused('Jira is not set up on this service')
        by = (action.get('requestedBy') or '').strip()
        try:
            new_key, warning = tickets.create_task(store, jira, settings, key, action.get('title'), by, (action.get('reason') or '').strip(), base_url or settings.base_url, action.get('description'), action.get('points'), action.get('project'), action.get('epic'), tuple(extra_projects), display_name(by, name_of))
        except (ClientRefused, SourceDown) as e:
            raise Refused(e.text) from None
        told = notify(key, new_key, by) if notify else []
        return f'{new_key} created · In progress · {by}' + (f" · notified: {', '.join(told)}" if told else '') + (f' · {warning}' if warning else '')
    if kind == 'mute':
        hours = action.get('hours') or DEFAULT_MUTE_HOURS
        until = utc_now() + dt.timedelta(hours=hours)
        store.mute(key, action.get('reason') or default_reason(action), iso(until), 'mute', preset=preset_reason(action))
        return f"muted until {clock.date(until)} {clock.label(until)}"
    if kind == 'known':
        store.mute(key, action.get('reason') or default_reason(action), None, 'known', preset=preset_reason(action))
        return 'known noise'
    if kind == 'unmute':
        store.unmute(key)
        return 'unmuted'
    if kind == 'severity':
        sev = action.get('sev')
        if sev not in SEV_NAMES:
            raise Refused('severity is one of crit, high, warn, info or auto')
        _, auto = store.set_severity(key, sev, action.get('requestedBy'))
        store.number_card(key)
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
        if system_note(doc):
            store.drop_note(key)
        store.set_tagging(key, {'reopenedAt': iso(utc_now())})
        return 'reopened'
    if kind == 'resolve':
        ticket = (action.get('ticket') or '').strip()
        why = (action.get('reason') or '').strip()
        how = resolution(store, action)
        if why:
            store.note(key, note_under(store.anomaly(key).get('note'), why, action.get('requestedBy'), 'Resolved'), by=history.PERSON)
        store.triage(key, 'handled', ticket=ticket, resolution=how)
        return history.kind_text(how) + (f' · {ticket}' if ticket else '') + (f' · {why[:RESULT_WHY]}' if why else '')
    if kind == 'take':
        ticket = (action.get('ticket') or '').strip()
        why = (action.get('reason') or '').strip()
        held = store.anomaly(key).get('owner')
        owner = (action.get('owner') or '').strip() or (None if held else (action.get('requestedBy') or '').strip() or None)
        if why:
            store.note(key, note_under(store.anomaly(key).get('note'), why, action.get('requestedBy'), 'Taken'), by=history.PERSON)
        store.triage(key, 'progress', ticket=ticket or None, owner=owner, owner_name=display_name(owner, name_of))
        store.mark_hand_assigned(key)
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
        if host.get('triage') == 'handled' and not information.calm(kid):
            word = severity.WORDS.get(kid.get('sev'), 'critical')
            raise Refused(f'{parent} is handled and {key} is {word}; a {word} card stays open for a human, also under a parent')
        if host.get('triage') == 'handled' and kid.get('triage') != 'handled':
            raise Refused(f'{parent} is already resolved (Done), so {key} would disappear from Open under it; pick an open card as the parent')
        store.group(parent, [key])
        return f"grouped under {parent_name(host) if host else parent}"
    raise ValueError(f'{kind} is not an instant action')
